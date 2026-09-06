# -*- coding: utf-8 -*-
"""滑块验证码缺口识别。不知道验证码是哪一家，只根据你给的背景+滑块识缺口。

识别器：
  shadow   反色亮度 × 滑块 alpha（暗洞最亮）
  rim      滑块描边 × 背景亮边（白描边/浅色幽灵缺口）
  outline  alpha 描边对背景 Canny
  dark     剪影窗口平均亮度（只作参考，不投票）
  ddddocr  可选兜底
  diff     若提供完整原图（第三张），与缺口背景做差找洞
  hole     额外缺口：亮边 / 暗剪影 / 描边 / 覆盖块 至少两路同时认，才计数（不画框）

滑块按 alpha 连通域拆块，每块对齐一个缺口。背景上再找「和已对齐那块同类」的洞：
亮边模板、暗剪影、Canny 描边、覆盖暗块分开提候选，位置靠近的合成一簇，
两路以上同意才算第二个缺口。单路纹理峰不计。不画框、不参与滑动距离。
滑动距离只用滑块块对齐的结果。

投票：剪影 / 亮边 / 描边局部峰，两票以上 x/y 接近取均值；暗区和 ddddocr 不投票。
置信过低返回 None，不乱猜。

    输入支持 bytes / base64 / dataURL / http(s) 图片地址 / 文件路径 / PIL.Image。
"""
import base64
import os
import threading

_DET_LOCK = threading.Lock()

__all__ = ['find_gap', 'find_gap_info', 'save_case', 'save_raw', 'save_boxed',
           'to_bytes', 'render_boxes']

GAP_COLORS = [
    (255, 40, 40),
    (0, 210, 255),
    (255, 210, 0),
    (80, 255, 80),
]


# ---------------------------------------------------------------------------
# 输入归一化
# ---------------------------------------------------------------------------
def _fetch_http(url):
    from urllib.parse import urlparse
    from urllib.request import Request, urlopen
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.netloc:
        raise ValueError('只支持 http/https 图片地址')
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 slider-ocr'})
    with urlopen(req, timeout=20) as r:
        data = r.read(8 * 1024 * 1024)
    if not data:
        raise ValueError('图片地址返回为空')
    if not (data[:8] == b'\x89PNG\r\n\x1a\n'
            or data[:2] == b'\xff\xd8'
            or data[:6] in (b'GIF87a', b'GIF89a')
            or (data[:4] == b'RIFF' and data[8:12] == b'WEBP')):
        raise ValueError('地址不是图片')
    return data


def to_bytes(img):
    """bytes / bytearray / base64 / dataURL / http(s) / 文件路径 / PIL.Image -> bytes。"""
    if img is None:
        return None
    if isinstance(img, (bytes, bytearray)):
        return bytes(img)
    if isinstance(img, str):
        s = img.strip()
        if s.startswith('data:'):
            s = s.split(',', 1)[1]
            return base64.b64decode(s)
        if s.startswith('http://') or s.startswith('https://'):
            return _fetch_http(s)
        if os.path.isfile(s):
            with open(s, 'rb') as f:
                return f.read()
        return base64.b64decode(s)
    try:
        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except Exception:
        pass
    raise TypeError('不支持的图片类型：%r' % type(img))


def _png_bytes(raw, alpha=False):
    """解码后存成 PNG，内存 dataURL 和落盘文件是同一套像素。"""
    from io import BytesIO
    from PIL import Image
    data = raw if isinstance(raw, (bytes, bytearray)) else to_bytes(raw)
    im = Image.open(BytesIO(data))
    has_alpha = (
        im.mode in ('RGBA', 'LA')
        or (im.mode == 'P' and 'transparency' in im.info)
    )
    if alpha or has_alpha:
        im = im.convert('RGBA')
    else:
        im = im.convert('RGB')
    buf = BytesIO()
    im.save(buf, format='PNG')
    return buf.getvalue()


def _np_bg_block(bg_bytes, block_bytes):
    import numpy as np
    from io import BytesIO
    from PIL import Image
    bg = np.asarray(Image.open(BytesIO(bg_bytes)).convert('RGB'))
    blk = np.asarray(Image.open(BytesIO(block_bytes)).convert('RGBA'))
    return bg, blk


def _pair_problem(bg_bytes, block_bytes):
    """同一张图，或背景/滑块放反。返回错误文案；输入可用则 None。"""
    if not bg_bytes or not block_bytes:
        return None
    if bg_bytes == block_bytes:
        return '背景和滑块是同一张图'
    import numpy as np
    from io import BytesIO
    from PIL import Image
    try:
        bg_im = Image.open(BytesIO(bg_bytes))
        sl_im = Image.open(BytesIO(block_bytes))
    except Exception:
        return None
    bw, bh = bg_im.size
    sw, sh = sl_im.size
    if min(bw, bh, sw, sh) < 8:
        return None
    if (bw, bh) == (sw, sh):
        a = np.asarray(bg_im.convert('RGB'), dtype=np.int16)
        b = np.asarray(sl_im.convert('RGB'), dtype=np.int16)
        if float(np.abs(a - b).mean()) <= 8.0:
            return '背景和滑块是同一张图'
    bg_a = np.asarray(bg_im.convert('RGBA'))[:, :, 3]
    sl_a = np.asarray(sl_im.convert('RGBA'))[:, :, 3]
    bg_trans = float((bg_a < 32).mean())
    sl_trans = float((sl_a < 32).mean())
    bg_area, sl_area = bw * bh, sw * sh
    smaller_bg = bg_area < 0.55 * sl_area and bw < int(0.72 * sw)
    piece_as_bg = bg_trans >= 0.22 and sl_trans < 0.10 and sl_area >= bg_area
    if smaller_bg or piece_as_bg:
        return '背景和滑块位置放反了'
    return None


def _ensure_alpha(blk):
    """没有透明通道时，把近白/近黑底抠掉，尽量得到拼图剪影。"""
    import numpy as np
    alpha = blk[:, :, 3]
    if int(alpha.min()) < 32 and int((alpha > 32).sum()) >= 80:
        return blk
    rgb = blk[:, :, :3].astype(np.int16)
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    near_white = (mn >= 245) & (mx >= 250)
    near_black = (mx <= 12)
    flat = (mx - mn) <= 8
    bg_like = near_white | (near_black & flat)
    if int(bg_like.sum()) < 30:
        return blk
    out = blk.copy()
    out[:, :, 3] = np.where(bg_like, 0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------------------
# 图对加载 / 多缺口拆块
# ---------------------------------------------------------------------------
def _trim_piece_fringe(piece, a_cut=80):
    """去掉剪影四周半透明毛边，避免框在右侧多出几像素。"""
    import numpy as np
    a = piece[:, :, 3]
    h, w = a.shape
    if h < 16 or w < 16:
        return piece, 0, 0

    def col_ok(c):
        return int(a[:, c].max()) >= a_cut

    def row_ok(r):
        return int(a[r, :].max()) >= a_cut

    x0, x1, y0, y1 = 0, w, 0, h
    while x0 < w - 8 and not col_ok(x0):
        x0 += 1
    while x1 > x0 + 8 and not col_ok(x1 - 1):
        x1 -= 1
    while y0 < h - 8 and not row_ok(y0):
        y0 += 1
    while y1 > y0 + 8 and not row_ok(y1 - 1):
        y1 -= 1
    if x0 == 0 and x1 == w and y0 == 0 and y1 == h:
        return piece, 0, 0
    return piece[y0:y1, x0:x1].copy(), int(x0), int(y0)


def _solid_piece(piece, cut=160):
    """画框用：去掉半透明光晕，只留实体剪影。匹配仍用带光晕的块，避免数量被带偏。"""
    import numpy as np
    a = piece[:, :, 3]
    ys, xs = np.where(a >= cut)
    if len(xs) < 20:
        return piece, 0, 0
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    if x0 == 0 and x1 == a.shape[1] and y0 == 0 and y1 == a.shape[0]:
        return piece, 0, 0
    return piece[y0:y1, x0:x1].copy(), int(x0), int(y0)


def _peel_halo(gray, piece, x, y, cut=160, max_k=3):
    """画框用：去掉落在缺口外亮边上的光晕像素。匹配仍用原块。

    剪影外沿若比「洞内/洞外」中点更亮，就是贴在洞外的描边，剥掉。
    洞里的暗像素（含凸起的榫）不剥。最多三圈。
    返回裁切后的块和相对原块的 (dx, dy)。
    """
    import numpy as np
    a = piece[:, :, 3]
    ph, pw = a.shape
    if pw < 16 or ph < 12 or gray is None:
        return piece, 0, 0
    gh, gw = gray.shape[:2]
    x, y = int(x), int(y)
    work = a >= cut
    if int(work.sum()) < 20:
        return piece, 0, 0
    y0, y1 = max(0, y), min(gh, y + ph)
    x0, x1 = max(0, x), min(gw, x + pw)
    if y1 <= y0 or x1 <= x0:
        return piece, 0, 0
    loc = np.full((ph, pw), 255.0, np.float32)
    loc[y0 - y:y1 - y, x0 - x:x1 - x] = gray[y0:y1, x0:x1]
    ring = []
    if y0 > 0:
        ring.append(gray[max(0, y0 - 3):y0, x0:x1])
    if y1 < gh:
        ring.append(gray[y1:min(gh, y1 + 3), x0:x1])
    if x0 > 0:
        ring.append(gray[y0:y1, max(0, x0 - 3):x0])
    if x1 < gw:
        ring.append(gray[y0:y1, x1:min(gw, x1 + 3)])
    if not ring:
        return piece, 0, 0
    out = float(np.median(np.concatenate([r.ravel() for r in ring])))
    ys, xs = np.where(work)
    ref = float(np.median(loc[ys, xs]))
    mid = 0.5 * (ref + out)
    for _ in range(max_k):
        up = np.ones((ph, pw), dtype=bool)
        down = np.ones((ph, pw), dtype=bool)
        left = np.ones((ph, pw), dtype=bool)
        right = np.ones((ph, pw), dtype=bool)
        up[1:] = work[:-1]
        down[:-1] = work[1:]
        left[:, 1:] = work[:, :-1]
        right[:, :-1] = work[:, 1:]
        border = work & ~(up & down & left & right)
        drop = border & (loc >= mid)
        if not drop.any():
            break
        work[drop] = False
    ys, xs = np.where(work)
    if len(xs) < 20:
        return piece, 0, 0
    nx0, nx1 = int(xs.min()), int(xs.max()) + 1
    ny0, ny1 = int(ys.min()), int(ys.max()) + 1
    outp = piece[ny0:ny1, nx0:nx1].copy()
    outp[:, :, 3] = np.where(work[ny0:ny1, nx0:nx1], outp[:, :, 3], 0)
    return outp, nx0, ny0


def _robust_min(pts, trim=0.08):
    pts = sorted(int(v) for v in pts)
    if not pts:
        return 0
    k = min(max(0, int(round(trim * len(pts)))), max(0, len(pts) // 8))
    return pts[k]


def _robust_max(pts, trim=0.08):
    pts = sorted(int(v) for v in pts)
    if not pts:
        return 0
    k = min(max(0, int(round(trim * len(pts)))), max(0, len(pts) // 8))
    return pts[-(k + 1)]


def _fit_box(gray, piece, x, y, max_k=4, cut=160):
    """画框用：把 AABB 贴到背景缺口沿。匹配用的 x/y/块尺寸不变。

    对剪影每一行/列最外沿像素，在 ±max_k 里找最强明暗沿；
    用分位数去掉个别纹理误沿。整体最多往里收 2px（不往外扩，避免贴到背景纹理）；
    若收完仍落在细榫上，只留 1px，避免把凸起剪掉。
    返回 (x, y, w, h)。
    """
    import numpy as np
    a = piece[:, :, 3]
    ph, pw = a.shape
    if pw < 16 or ph < 12 or gray is None:
        return int(x), int(y), int(pw), int(ph)
    gh, gw = gray.shape[:2]
    x, y = int(x), int(y)
    if x < 0 or y < 0 or x + 8 >= gw or y + 8 >= gh:
        return x, y, int(pw), int(ph)
    solid = a >= cut
    if int(solid.sum()) < 20:
        return x, y, int(pw), int(ph)
    ns_c = solid.sum(axis=0).astype(np.int32)
    ns_r = solid.sum(axis=1).astype(np.int32)
    body_c = int(ns_c.max()) if ns_c.size else 0
    body_r = int(ns_r.max()) if ns_r.size else 0
    min_e = 6
    shrink, expand = 2, 0

    def snap_axis(horiz, sign):
        pts = []
        n = ph if horiz else pw
        for t in range(n):
            if horiz:
                cols = np.where(solid[t])[0]
                if cols.size == 0:
                    continue
                j = int(cols.min() if sign < 0 else cols.max())
                r, c0 = y + t, x + j
                if not (0 <= r < gh):
                    continue
            else:
                rows = np.where(solid[:, t])[0]
                if rows.size == 0:
                    continue
                i = int(rows.min() if sign < 0 else rows.max())
                r0, c = y + i, x + t
                if not (0 <= c < gw):
                    continue
            orig = c0 if horiz else r0
            best_s, best = -1e9, orig
            for d in range(-max_k, max_k + 1):
                if horiz:
                    c = c0 + d
                    outc = c + sign
                    if c < 0 or c >= gw or outc < 0 or outc >= gw:
                        continue
                    s = float(gray[r, outc]) - float(gray[r, c])
                    pos = c
                else:
                    rr = r0 + d
                    outr = rr + sign
                    if rr < 0 or rr >= gh or outr < 0 or outr >= gh:
                        continue
                    s = float(gray[outr, c]) - float(gray[rr, c])
                    pos = rr
                if s > best_s:
                    best_s, best = s, pos
            pts.append(best if best_s >= min_e else orig)
        return pts

    Lp, Rp = snap_axis(True, -1), snap_axis(True, +1)
    Tp, Bp = snap_axis(False, -1), snap_axis(False, +1)
    L = _robust_min(Lp) if Lp else x
    R = _robust_max(Rp) if Rp else x + pw - 1
    T = _robust_min(Tp) if Tp else y
    B = _robust_max(Bp) if Bp else y + ph - 1
    L = min(max(L, x - expand), x + shrink)
    R = min(max(R, x + pw - 1 - shrink), x + pw - 1 + expand)
    T = min(max(T, y - expand), y + shrink)
    B = min(max(B, y + ph - 1 - shrink), y + ph - 1 + expand)

    def guard_thin(ns, body, orig, new, inward, start):
        if body <= 0 or ns is None or ns.size == 0:
            return new
        if inward > 0 and new <= orig:
            return new
        if inward < 0 and new >= orig:
            return new
        idx = int(new - start)
        if idx < 0 or idx >= len(ns):
            return orig + (1 if inward > 0 else -1)
        if int(ns[idx]) < 0.42 * body:
            if inward > 0:
                return min(new, orig + 1)
            return max(new, orig - 1)
        return new

    L = guard_thin(ns_c, body_c, x, L, +1, x)
    R = guard_thin(ns_c, body_c, x + pw - 1, R, -1, x)
    T = guard_thin(ns_r, body_r, y, T, +1, y)
    B = guard_thin(ns_r, body_r, y + ph - 1, B, -1, y)
    nw, nh = int(R - L + 1), int(B - T + 1)
    if nw < 12 or nh < 12:
        return x, y, int(pw), int(ph)
    return int(L), int(T), nw, nh


def _slice_piece(piece, ox, oy, nx, ny, nw, nh):
    import numpy as np
    out = np.zeros((max(1, nh), max(1, nw), piece.shape[2]), dtype=piece.dtype)
    ix0 = max(nx, ox)
    iy0 = max(ny, oy)
    ix1 = min(nx + nw, ox + piece.shape[1])
    iy1 = min(ny + nh, oy + piece.shape[0])
    if ix1 <= ix0 or iy1 <= iy0:
        return piece
    out[iy0 - ny:iy1 - ny, ix0 - nx:ix1 - nx] = piece[iy0 - oy:iy1 - oy,
                                                      ix0 - ox:ix1 - ox]
    return out


def _load_pair(bg_bytes, block_bytes):
    """bg RGB, 裁剪后的拼图 RGBA, 拼图在滑块图内的 y0/x0。"""
    import numpy as np
    bg, blk = _np_bg_block(bg_bytes, block_bytes)
    blk = _ensure_alpha(blk)
    alpha = blk[:, :, 3]
    ys, xs = np.where(alpha > 32)
    if len(xs) == 0:
        return None
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    piece = blk[y0:y1 + 1, x0:x1 + 1]
    piece, dx, dy = _trim_piece_fringe(piece)
    return bg, piece, y0 + dy, x0 + dx


def _split_pieces(blk, min_area=160):
    """按 alpha 连通域拆出每块拼图。几块就是几个缺口。"""
    import numpy as np
    try:
        import cv2
    except Exception:
        return []
    blk = _ensure_alpha(blk)
    mask = (blk[:, :, 3] > 32).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    pieces = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < min_area or w < 10 or h < 12:
            continue
        piece = blk[y:y + h, x:x + w].copy()
        local = labels[y:y + h, x:x + w]
        piece[local != i] = 0
        if int((piece[:, :, 3] > 32).sum()) < min_area:
            continue
        piece, dx, dy = _trim_piece_fringe(piece)
        if piece.shape[0] < 12 or piece.shape[1] < 10:
            continue
        pieces.append({
            'piece': piece, 'x0': x + dx, 'y0': y + dy,
            'w': int(piece.shape[1]), 'h': piece.shape[0], 'area': area,
        })
    pieces.sort(key=lambda p: (p['y0'], p['x0']))
    return pieces


def _xmin(bg_w, piece_w):
    """原图拼块在左侧，缺口至少在画面约 1/3 之后。"""
    floor = min(96, max(40, bg_w // 4))
    return max(int(piece_w * 1.5), int(bg_w * 0.32), floor)


def _in_range(x, y, bg_w, bg_h, piece_w, piece_h):
    if x is None or y is None:
        return False
    lo, hi = _xmin(bg_w, piece_w), bg_w - max(piece_w, 8) - 2
    if not (lo <= int(x) <= hi):
        return False
    return 0 <= int(y) <= bg_h - max(piece_h, 8) - 1


def _blank_left(res, xmin):
    if xmin > 0:
        res[:, :xmin] = -1
    return res


def _y_band(sl_h, bg_h, piece_h, pad_y):
    """滑块图几乎与背景等高、拼图只占其中一条时，缺口 Y 跟拼图在滑块图里的 y 一致。"""
    if not sl_h or not bg_h or not piece_h:
        return None, None
    if sl_h >= int(0.72 * bg_h) and piece_h <= int(0.62 * sl_h):
        rad = max(10, int(piece_h * 0.22))
        return int(pad_y) - rad, int(pad_y) + rad
    return None, None


def _apply_y_band(res, y_lo, y_hi, mode='max'):
    """把 matchTemplate 结果里超出 y 带的行涂掉。行号 = 匹配框左上角 y。"""
    if res is None or (y_lo is None and y_hi is None):
        return res
    import numpy as np
    h = res.shape[0]
    lo = 0 if y_lo is None else max(0, int(y_lo))
    hi = h if y_hi is None else min(h, int(y_hi) + 1)
    if lo >= hi:
        return res
    fill = -2.0 if mode == 'max' else float(np.max(res)) + 1.0
    if lo > 0:
        res[:lo, :] = fill
    if hi < h:
        res[hi:, :] = fill
    return res


def _y_ok(y, y_lo, y_hi):
    if y is None:
        return False
    if y_lo is not None and int(y) < int(y_lo):
        return False
    if y_hi is not None and int(y) > int(y_hi):
        return False
    return True


# ---------------------------------------------------------------------------
# 单块匹配（返回 x, y, conf）
# ---------------------------------------------------------------------------
def _gap_shadow_arr(bg, piece, y_lo=None, y_hi=None):
    try:
        import cv2
    except Exception:
        return None, None, 0.0
    mask = piece[:, :, 3]
    gh, gw = bg.shape[0], bg.shape[1]
    ph, pw = mask.shape
    if ph > gh or pw > gw:
        return None, None, 0.0
    gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)
    hole = cv2.GaussianBlur(255 - gray, (5, 5), 0)
    tpl = cv2.GaussianBlur(mask, (3, 3), 0)
    res = cv2.matchTemplate(hole, tpl, cv2.TM_CCOEFF_NORMED)
    _blank_left(res, _xmin(gw, pw))
    _apply_y_band(res, y_lo, y_hi, mode='max')
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    x, y = int(max_loc[0]), int(max_loc[1])
    if (not _in_range(x, y, gw, gh, pw, ph) or not _y_ok(y, y_lo, y_hi)
            or max_val < 0.25):
        return None, None, float(max_val)
    return x, y, float(max_val)


def _gap_dark_arr(bg, piece, y_lo=None, y_hi=None):
    try:
        import cv2
        import numpy as np
    except Exception:
        return None, None, 0.0
    mask = piece[:, :, 3] > 32
    gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)
    ph, pw = mask.shape
    gh, gw = gray.shape
    if ph > gh or pw > gw:
        return None, None, 0.0
    xmin = _xmin(gw, pw)
    tpl = np.zeros((ph, pw), dtype=np.uint8)
    m8 = (mask.astype(np.uint8) * 255)
    try:
        res = cv2.matchTemplate(gray, tpl, cv2.TM_SQDIFF, mask=m8)
        res[:, :xmin] = np.max(res) + 1
        _apply_y_band(res, y_lo, y_hi, mode='min')
        _min_val, _, min_loc, _ = cv2.minMaxLoc(res)
        x, y = int(min_loc[0]), int(min_loc[1])
    except Exception:
        return None, None, 0.0
    if not _in_range(x, y, gw, gh, pw, ph) or not _y_ok(y, y_lo, y_hi):
        return None, None, 0.0
    win = gray[y:y + ph, x:x + pw][mask]
    other = gray[:, xmin:min(gw, xmin + pw + max(pw, 40))]
    conf = 0.5
    if win.size and other.size:
        conf = max(0.0, (float(other.mean()) - float(win.mean()))
                   / (float(other.mean()) + 1e-6))
    if conf < 0.05:
        return None, None, conf
    return x, y, float(min(0.99, 0.35 + conf))


def _piece_outline(piece):
    import cv2
    import numpy as np
    mask = piece[:, :, 3]
    kernel = np.ones((3, 3), np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, kernel)


def _bg_rim(bg):
    """亮且不太饱和的描边 + 比周围亮的细边（白圈缺口）。"""
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(bg, cv2.COLOR_RGB2HSV)
    v, s = hsv[:, :, 2], hsv[:, :, 1]
    rim = ((v >= 185) & (s <= 90)).astype(np.uint8) * 255
    gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (7, 7), 0)
    glow = np.clip(gray.astype(np.int16) - blur.astype(np.int16), 0, 255)
    glow = ((glow > 16).astype(np.uint8)) * 255
    out = cv2.bitwise_or(rim, glow)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.morphologyEx(out, cv2.MORPH_CLOSE, k)


def _gap_outline_arr(bg, piece, y_lo=None, y_hi=None):
    try:
        import cv2
        import numpy as np
    except Exception:
        return None, None, 0.0
    mask = piece[:, :, 3]
    gh, gw = bg.shape[0], bg.shape[1]
    ph, pw = mask.shape
    if ph > gh or pw > gw:
        return None, None, 0.0
    outline = _piece_outline(piece)
    bg_edge = cv2.Canny(cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY), 60, 140)
    if int(outline.max() or 0) == 0:
        return None, None, 0.0
    res = cv2.matchTemplate(bg_edge, outline, cv2.TM_CCOEFF_NORMED)
    _blank_left(res, _xmin(gw, pw))
    _apply_y_band(res, y_lo, y_hi, mode='max')
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    x, y = int(max_loc[0]), int(max_loc[1])
    if (not _in_range(x, y, gw, gh, pw, ph) or not _y_ok(y, y_lo, y_hi)
            or max_val < 0.18):
        return None, None, float(max_val)
    return x, y, float(max_val)


def _gap_rim_arr(bg, piece, y_lo=None, y_hi=None):
    """滑块轮廓对背景亮边：白描边缺口、浅色幽灵缺口。"""
    try:
        import cv2
    except Exception:
        return None, None, 0.0
    gh, gw = bg.shape[0], bg.shape[1]
    ph, pw = piece.shape[0], piece.shape[1]
    if ph > gh or pw > gw:
        return None, None, 0.0
    outline = _piece_outline(piece)
    if int(outline.max() or 0) == 0:
        return None, None, 0.0
    rim = _bg_rim(bg)
    res = cv2.matchTemplate(rim, outline, cv2.TM_CCOEFF_NORMED)
    _blank_left(res, _xmin(gw, pw))
    _apply_y_band(res, y_lo, y_hi, mode='max')
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    x, y = int(max_loc[0]), int(max_loc[1])
    if (not _in_range(x, y, gw, gh, pw, ph) or not _y_ok(y, y_lo, y_hi)
            or max_val < 0.18):
        return None, None, float(max_val)
    return x, y, float(max_val)


_dddd = None


def _piece_png(piece):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.fromarray(piece).save(buf, format='PNG')
    return buf.getvalue()


def _gap_ddddocr_arr(bg, piece, bg_bytes, y_lo=None, y_hi=None):
    global _dddd
    try:
        import ddddocr
        if _dddd is None:
            _dddd = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        target = _piece_png(piece)
        res = _dddd.slide_match(target, bg_bytes, simple_target=True)
        box = (res or {}).get('target') or []
        if len(box) < 2:
            return None, None, 0.0
        x, y = int(box[0]), int(box[1]) if len(box) > 1 else 0
        ph, pw = piece.shape[0], piece.shape[1]
        gh, gw = bg.shape[0], bg.shape[1]
        if not _in_range(x, y, gw, gh, pw, ph) or not _y_ok(y, y_lo, y_hi):
            return None, None, 0.0
        return x, y, 0.75
    except Exception:
        return None, None, 0.0


def _gap_diff_arr(bg, full, piece, y_lo=None, y_hi=None):
    """完整原图 - 缺口背景 = 洞。洞的位置再和拼图剪影对一下。"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None, None, 0.0
    if full is None or full.shape[:2] != bg.shape[:2]:
        return None, None, 0.0
    mask = piece[:, :, 3]
    ph, pw = mask.shape
    gh, gw = bg.shape[0], bg.shape[1]
    if ph > gh or pw > gw:
        return None, None, 0.0
    d = cv2.absdiff(cv2.cvtColor(full, cv2.COLOR_RGB2GRAY),
                      cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY))
    hole = cv2.GaussianBlur(d, (5, 5), 0)
    tpl = cv2.GaussianBlur(mask, (3, 3), 0)
    if int(hole.max() or 0) < 8:
        return None, None, 0.0
    res = cv2.matchTemplate(hole, tpl, cv2.TM_CCOEFF_NORMED)
    _blank_left(res, _xmin(gw, pw))
    _apply_y_band(res, y_lo, y_hi, mode='max')
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    x, y = int(max_loc[0]), int(max_loc[1])
    if (not _in_range(x, y, gw, gh, pw, ph) or not _y_ok(y, y_lo, y_hi)
            or max_val < 0.20):
        return None, None, float(max_val)
    return x, y, float(max_val)


def _lock_conf(raw, n_lock):
    """匹配分 0.55～0.70 已经能锁洞，展示时按锁定强度拉开；多路同位置再加一点。"""
    r = max(0.0, min(1.0, float(raw or 0)))
    shown = 1.0 - (1.0 - r) ** 2
    if n_lock >= 2:
        shown = min(0.99, shown + 0.05 * (n_lock - 1))
    return float(min(0.99, shown))


def _n_lock(xs, x, y, tol=6, ytol=8):
    if x is None or y is None or not xs:
        return 0
    n = 0
    for m, xx, yy, c in xs:
        if 'dddd' in str(m):
            continue
        if c is None or float(c) < 0.25:
            continue
        if abs(int(xx) - int(x)) <= tol and abs(int(yy) - int(y)) <= ytol:
            n += 1
    return n


def _agree(xs, tol=6, ytol=8):
    """xs: (method, x, y, conf)。x、y 都接近才算一票。暗区 / ddddocr 不投票。"""
    pool = [(m, x, y, c) for m, x, y, c in xs
            if 'dddd' not in str(m) and str(m) != 'dark']
    if len(pool) < 2:
        return None
    for _m, xa, ya, _c in pool:
        near = [(m, x, y, c) for m, x, y, c in pool
                if abs(x - xa) <= tol and abs(y - ya) <= ytol]
        if len(near) >= 2:
            avg_x = int(round(sum(x for _m, x, _y, _c in near) / len(near)))
            avg_y = int(round(sum(y for _m, _x, y, _c in near) / len(near)))
            return {
                'x': avg_x,
                'y': avg_y,
                'method': '+'.join(m for m, _x, _y, _c in near),
                'conf': max(c for _m, _x, _y, c in near),
            }
    return None


def _pick_best(xs):
    if not xs:
        return None
    pool = [v for v in xs
            if str(v[0]) != 'dark' and 'dddd' not in str(v[0])]
    if not pool:
        pool = list(xs)
    pool = list(pool)
    pool.sort(key=lambda v: (
        0 if v[0] == 'diff' and v[3] >= 0.35 else
        1 if v[0] == 'rim' and v[3] >= 0.28 else
        2 if v[0] == 'shadow' and v[3] >= 0.40 else
        3 if v[0] == 'outline' and v[3] >= 0.18 else
        4 if v[0] == 'dark' and v[3] >= 0.45 else
        5 if v[0] == 'ddddocr' else 6,
        -v[3]))
    m, x, y, c = pool[0]
    if m in ('shadow', 'dark', 'outline', 'diff', 'rim') and c < 0.18:
        return None
    return {'x': int(x), 'y': int(y), 'method': m, 'conf': float(c)}


def _match_piece(bg, piece, bg_bytes=None, full=None, y_lo=None, y_hi=None):
    xs = []
    for name, fn in (
        ('shadow', _gap_shadow_arr),
        ('rim', _gap_rim_arr),
        ('outline', _gap_outline_arr),
        ('dark', _gap_dark_arr),
    ):
        x, y, c = fn(bg, piece, y_lo=y_lo, y_hi=y_hi)
        if x is not None:
            xs.append((name, x, y, c))
    if full is not None:
        x, y, c = _gap_diff_arr(bg, full, piece, y_lo=y_lo, y_hi=y_hi)
        if x is not None:
            xs.append(('diff', x, y, c))
    cands = [(m, int(x), int(y), round(float(c), 3)) for m, x, y, c in xs]
    hit = _agree(xs)
    if not hit:
        hit = _pick_best(xs)
    if hit:
        hit['cands'] = cands
        hit['w'] = int(piece.shape[1])
        hit['h'] = int(piece.shape[0])
        shown = [t for t in xs if str(t[0]) not in ('dark',) and 'dddd' not in str(t[0])]
        hit['conf'] = _lock_conf(hit.get('conf'), _n_lock(shown, hit.get('x'), hit.get('y')))
        return hit
    return {'x': None, 'y': None, 'method': None, 'conf': 0.0,
            'cands': cands, 'w': int(piece.shape[1]), 'h': int(piece.shape[0])}


def _near(ax, ay, bx, by, rad):
    if ax is None or ay is None or bx is None or by is None:
        return False
    return (int(ax) - int(bx)) ** 2 + (int(ay) - int(by)) ** 2 <= int(rad) ** 2


def _hole_heatmap(gray, pw, ph, pad=5):
    """outer 环均值 - inner 块均值：拼图大小的暗洞得分高。"""
    import cv2
    import numpy as np
    g = gray.astype(np.float32)
    inner = np.zeros((ph, pw), np.float32)
    ix, iy = max(3, pw // 8), max(3, ph // 8)
    inner[iy:ph - iy, ix:pw - ix] = 1.0
    s = float(inner.sum())
    if s < 8:
        return None, pad
    inner /= s
    ow, oh = pw + pad * 2, ph + pad * 2
    ring = np.ones((oh, ow), np.float32)
    ring[pad:pad + ph, pad:pad + pw] = 0
    rs = float(ring.sum())
    if rs < 8:
        return None, pad
    ring /= rs
    inn = cv2.matchTemplate(g, inner, cv2.TM_CCORR)
    out = cv2.matchTemplate(g, ring, cv2.TM_CCORR)
    dh = inn.shape[0] - out.shape[0]
    dw = inn.shape[1] - out.shape[1]
    if dh < 0 or dw < 0:
        return None, pad
    inn_c = inn[dh // 2:inn.shape[0] - (dh - dh // 2),
                  dw // 2:inn.shape[1] - (dw - dw // 2)]
    h = min(inn_c.shape[0], out.shape[0])
    w = min(inn_c.shape[1], out.shape[1])
    return out[:h, :w] - inn_c[:h, :w], pad


def _nms_map(res, ph, pw, k=8, xmin=0):
    import cv2
    if res is None or res.size == 0:
        return []
    work = res.copy()
    if xmin > 0:
        work[:, :max(0, int(xmin))] = -1e9
    hits = []
    rad = max(8, int(0.7 * max(ph, pw)))
    for _ in range(k):
        _mn, mv, _ml, ml = cv2.minMaxLoc(work)
        if mv < -1e8:
            break
        x, y = int(ml[0]), int(ml[1])
        hits.append((float(mv), x, y))
        work[max(0, y - rad):y + rad, max(0, x - rad):x + rad] = -1e9
    return hits


def _tighten_hole(gray, x, y, pw, ph):
    """用窗口里最暗的连通域把框收紧到洞本身。"""
    import cv2
    import numpy as np
    gh, gw = gray.shape
    pad = max(6, min(pw, ph) // 6)
    x0, y0 = max(0, int(x) - pad), max(0, int(y) - pad)
    x1, y1 = min(gw, int(x) + pw + pad), min(gh, int(y) + ph + pad)
    win = gray[y0:y1, x0:x1]
    if win.size < 40:
        return int(x), int(y), int(pw), int(ph)
    blur = cv2.GaussianBlur(win, (11, 11), 0)
    mask = (blur.astype(np.int16) - win.astype(np.int16) > 10).astype(np.uint8)
    n, _lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    area0 = max(80, int(pw * ph * 0.22))
    cx, cy = (int(x) - x0) + pw // 2, (int(y) - y0) + ph // 2
    for i in range(1, n):
        bx, by, bw, bh, area = (int(v) for v in stats[i])
        if area < area0 or bw < 14 or bh < 14:
            continue
        if bw > pw * 1.7 or bh > ph * 1.7:
            continue
        dist = abs(bx + bw / 2 - cx) + abs(by + bh / 2 - cy)
        key = (dist, -area)
        if best is None or key < best[0]:
            best = (key, bx, by, bw, bh)
    if not best:
        return int(x), int(y), int(pw), int(ph)
    _k, bx, by, bw, bh = best
    return x0 + bx, y0 + by, bw, bh


def _inner_mean(gray, x, y, pw, ph):
    ix, iy = max(2, pw // 8), max(3, ph // 8)
    win = gray[y:y + ph, x:x + pw]
    core = win[iy:ph - iy, ix:pw - ix]
    if core.size == 0:
        return float(win.mean())
    return float(core.mean())


def _find_bg_holes(bg, pw, ph, xmin, occupied):
    """背景上找拼图大小的暗洞。不看厂商，只看图。

    真缺口 = 比周围明显暗（边界对比 hm 高）且本身够暗（inner 低）的拼图大小区域。
    只在这里数暗洞；浅色轮廓类的洞由滑块匹配负责，避免把背景纹理当成洞。
    返回的每个洞带 score（边界对比强度），供调用方按相对强度再筛一遍。
    """
    try:
        import cv2
    except Exception:
        return []
    if pw < 16 or ph < 16:
        return []
    gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)
    hm, pad = _hole_heatmap(gray, pw, ph)
    if hm is None:
        return []
    rad = max(12, int(0.65 * max(pw, ph)))
    used = list(occupied)
    holes = []
    for score, hx, hy in _nms_map(hm, ph, pw, k=8, xmin=max(0, int(xmin) - pad)):
        x, y = int(hx) + pad, int(hy) + pad
        if x < 0 or y < 0 or x + pw > gray.shape[1] or y + ph > gray.shape[0]:
            continue
        if any(_near(x, y, ox, oy, rad) for ox, oy, _ow, _oh in used):
            continue
        inner = _inner_mean(gray, x, y, pw, ph)
        if score < 40 or inner > 95:
            continue
        tx, ty, tw, th = _tighten_hole(gray, x, y, pw, ph)
        holes.append({
            'x': int(tx), 'y': int(ty), 'w': int(tw), 'h': int(th),
            'conf': float(min(0.99, 0.35 + score / 200.0)),
            'method': 'hole', 'kind': 'hole', 'score': float(score),
        })
        used.append((int(tx), int(ty), int(tw), int(th)))
    return holes


def _find_overlay_holes(bg, pw, ph, xmin, occupied):
    """背景上局部变暗、尺寸接近拼图、比较圆整的覆盖块。

    验证码缺口是盖在照片上的半透明块，四周比洞内亮；树影和字母形状碎、圆整度低。
    只用来计数，不参与选滑动目标。
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    pw, ph = int(pw), int(ph)
    if pw < 16 or ph < 16:
        return []
    gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)
    loc = cv2.GaussianBlur(gray, (21, 21), 0)
    dark = ((loc.astype(np.int16) - gray.astype(np.int16)) > 12).astype(np.uint8) * 255
    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, k3)
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, k5, iterations=2)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(dark)
    rad = max(12, int(0.55 * max(pw, ph)))
    holes = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if x < int(xmin):
            continue
        if not (0.40 * pw <= w <= 1.65 * pw and 0.40 * ph <= h <= 1.65 * ph):
            continue
        ar = w / float(h)
        if ar < 0.55 or ar > 1.75:
            continue
        if area < 0.18 * pw * ph or area > 1.5 * pw * ph:
            continue
        if area / float(w * h) < 0.30:
            continue
        mask = (labels == i).astype(np.uint8) * 255
        ring = cv2.subtract(cv2.dilate(mask, k3, iterations=2), mask)
        inner = gray[mask > 0]
        outer = gray[ring > 0]
        if inner.size < 30 or outer.size < 20:
            continue
        if float(outer.mean()) - float(inner.mean()) < 18:
            continue
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        peri = cv2.arcLength(cnts[0], True)
        circ = 4.0 * np.pi * area / (peri * peri + 1e-6)
        if circ < 0.20:
            continue
        if any(_near(x, y, ox, oy, rad) for ox, oy, _ow, _oh in occupied):
            continue
        holes.append({
            'x': x, 'y': y, 'w': w, 'h': h,
            'method': 'overlay', 'conf': float(circ),
            'cands': [], 'kind': 'hole', 'pad_x': 0, 'pad_y': 0,
        })
    return holes


def _ncc_peaks(res, xmin, rad, k=6, floor=0.10):
    """NCC 图上取互不重叠的峰。"""
    import cv2
    if res is None or res.size == 0:
        return []
    work = res.copy()
    if xmin > 0:
        work[:, :max(0, int(xmin))] = -1
    out = []
    for _ in range(k):
        _, mv, _, ml = cv2.minMaxLoc(work)
        if mv < floor:
            break
        x, y = int(ml[0]), int(ml[1])
        out.append((x, y, float(mv)))
        work[max(0, y - rad):y + rad + 1, max(0, x - rad):x + rad + 1] = -1
    return out


def _rim_hit_frac(rim, outline, x, y):
    """拼图描边落在背景亮边上的比例。纹理峰这项会明显低于真洞。"""
    ph, pw = outline.shape
    gh, gw = rim.shape[:2]
    x, y = int(x), int(y)
    if x < 0 or y < 0 or x + pw > gw or y + ph > gh:
        return 0.0
    ring = outline > 0
    if int(ring.sum()) < 10:
        return 0.0
    win = rim[y:y + ph, x:x + pw]
    return float((win[ring] > 0).mean())


def _ring_delta(gray, alpha, x, y):
    """洞外一圈均值 − 剪影内均值。正数=洞内更暗。"""
    import numpy as np
    ph, pw = alpha.shape
    gh, gw = gray.shape[:2]
    x, y = int(x), int(y)
    if x < 0 or y < 0 or x + pw > gw or y + ph > gh:
        return 0.0
    solid = alpha > 32
    win = gray[y:y + ph, x:x + pw]
    inner = float(win[solid].mean()) if solid.any() else 0.0
    pad = 3
    y0, x0 = max(0, y - pad), max(0, x - pad)
    y1, x1 = min(gh, y + ph + pad), min(gw, x + pw + pad)
    ring = np.ones((y1 - y0, x1 - x0), np.uint8)
    ring[y - y0:y - y0 + ph, x - x0:x - x0 + pw][solid] = 0
    pix = gray[y0:y1, x0:x1][ring > 0]
    outer = float(pix.mean()) if pix.size else inner
    return outer - inner


def _find_rim_holes(bg, piece, xmin, occupied):
    """兼容旧名，转到多路投票。"""
    return _find_extra_holes(bg, piece, xmin, occupied)


def _find_extra_holes(bg, piece, xmin, occupied):
    """背景上再数缺口：多路提候选，两路同意才算。

    通病不是某一张图，是「亮边模板 + 绝对阈值 0.32」：
    真第二洞经常只有 0.34，差 0.02 就漏计；几何纹理的弱峰又会被算进去。
    改成和滑块匹配同一套思路：亮边 / 暗剪影 / Canny / 覆盖块分开看，
    位置靠近合成一簇，至少两路认才计数。亮边全局都很弱时（没有白圈）
    亮边这一路直接关掉，避免把色块当第二洞。
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    ph, pw = int(piece.shape[0]), int(piece.shape[1])
    gh, gw = bg.shape[0], bg.shape[1]
    if pw < 16 or ph < 16 or ph > gh or pw > gw:
        return []
    outline = _piece_outline(piece)
    if int(outline.max() or 0) == 0:
        return []
    gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)
    rim = _bg_rim(bg)
    alpha = piece[:, :, 3]
    hole = cv2.GaussianBlur(255 - gray, (5, 5), 0)
    tpl = cv2.GaussianBlur(alpha, (3, 3), 0)
    xmin = int(xmin)
    rim_map = cv2.matchTemplate(rim, outline, cv2.TM_CCOEFF_NORMED)
    _blank_left(rim_map, xmin)
    sh_map = cv2.matchTemplate(hole, tpl, cv2.TM_CCOEFF_NORMED)
    _blank_left(sh_map, xmin)
    out_map = cv2.matchTemplate(
        cv2.Canny(gray, 60, 140), outline, cv2.TM_CCOEFF_NORMED)
    _blank_left(out_map, xmin)
    rad = max(12, int(0.55 * max(pw, ph)))
    overlays = _find_overlay_holes(bg, pw, ph, xmin, occupied)

    seeds = []
    for x, y, c in _ncc_peaks(rim_map, xmin, rad):
        seeds.append({'x': x, 'y': y, 'rim': c, 'sh': -1.0, 'out': -1.0,
                      'ov': False, 'circ': 0.0})
    for x, y, c in _ncc_peaks(sh_map, xmin, rad):
        seeds.append({'x': x, 'y': y, 'rim': -1.0, 'sh': c, 'out': -1.0,
                      'ov': False, 'circ': 0.0})
    for x, y, c in _ncc_peaks(out_map, xmin, rad):
        seeds.append({'x': x, 'y': y, 'rim': -1.0, 'sh': -1.0, 'out': c,
                      'ov': False, 'circ': 0.0})
    for h in overlays:
        seeds.append({'x': int(h['x']), 'y': int(h['y']),
                      'rim': -1.0, 'sh': -1.0, 'out': -1.0,
                      'ov': True, 'circ': float(h.get('conf') or 0)})

    def _score(s):
        return max(s['rim'], s['sh'], s['out'], 0.40 if s['ov'] else -1.0)

    clusters = []
    for s in seeds:
        hit = None
        for cl in clusters:
            if _near(s['x'], s['y'], cl['x'], cl['y'], rad):
                hit = cl
                break
        if hit is None:
            clusters.append(dict(s))
            continue
        if _score(s) > _score(hit):
            hit['x'], hit['y'] = s['x'], s['y']
        hit['rim'] = max(hit['rim'], s['rim'])
        hit['sh'] = max(hit['sh'], s['sh'])
        hit['out'] = max(hit['out'], s['out'])
        hit['ov'] = hit['ov'] or s['ov']
        hit['circ'] = max(hit['circ'], s['circ'])

    if not clusters:
        return []
    ox = oy = None
    if occupied:
        ox, oy = occupied[0][0], occupied[0][1]
    ref_sup = _rim_hit_frac(rim, outline, ox, oy) if occupied else 0.0
    ref_dlt = _ring_delta(gray, alpha, ox, oy) if occupied else 0.0
    best_rim = max(cl['rim'] for cl in clusters)
    best_sh = max(cl['sh'] for cl in clusters)
    best_out = max(cl['out'] for cl in clusters)

    def contrast_ok(dlt):
        if ref_dlt > 8:
            return dlt > 4
        if ref_dlt < -8:
            return dlt < -4
        return True

    holes = []
    used = list(occupied)
    for cl in clusters:
        x, y = int(cl['x']), int(cl['y'])
        if 0 <= y < rim_map.shape[0] and 0 <= x < rim_map.shape[1]:
            cl['rim'] = max(cl['rim'], float(rim_map[y, x]))
            cl['sh'] = max(cl['sh'], float(sh_map[y, x]))
            cl['out'] = max(cl['out'], float(out_map[y, x]))
        if any(_near(x, y, a, b, rad) for a, b, _w, _h in used):
            continue
        if not _in_range(x, y, gw, gh, pw, ph):
            continue
        sup = _rim_hit_frac(rim, outline, x, y)
        dlt = _ring_delta(gray, alpha, x, y)
        votes = 0
        methods = []
        if (best_rim >= 0.28 and cl['rim'] >= max(0.22, 0.70 * best_rim)
                and sup >= 0.65 * (ref_sup or 0.01) and contrast_ok(dlt)):
            votes += 1
            methods.append('rim')
        if (best_sh >= 0.35 and cl['sh'] >= max(0.25, 0.55 * best_sh)
                and dlt > 5):
            votes += 1
            methods.append('shadow')
        if (best_out >= 0.18 and cl['out'] >= max(0.12, 0.70 * best_out)
                and contrast_ok(dlt)):
            votes += 1
            methods.append('outline')
        if cl['ov']:
            votes += 1
            methods.append('overlay')
            if dlt >= 25 and cl['circ'] >= 0.22:
                votes += 1
        if votes < 2:
            continue
        holes.append({
            'x': x, 'y': y, 'w': pw, 'h': ph,
            'method': '+'.join(methods) or 'hole',
            'conf': float(max(cl['rim'], cl['sh'], cl['out'], cl['circ'])),
            'cands': [], 'kind': 'hole', 'pad_x': 0, 'pad_y': 0,
        })
        used.append((x, y, pw, ph))
    return holes


def _load_full(extra):
    if extra is None:
        return None
    try:
        import numpy as np
        from io import BytesIO
        from PIL import Image
        return np.asarray(Image.open(BytesIO(to_bytes(extra))).convert('RGB'))
    except Exception:
        return None


def find_gap_info(bg, block, extra=None):
    """返回 {x, y, n_gaps, gaps, method, conf, cands, pad_x} 或 None。

    gaps: 每个缺口一块 {x, y, w, h, method, conf, pad_x, pad_y}
    x 是滑动距离（背景图像素，滑块图左缘对齐后的缺口左缘）。
    """
    with _DET_LOCK:
        return _find_gap_info_run(bg, block, extra)


def _find_gap_info_run(bg, block, extra=None):
    bg_bytes, block_bytes = to_bytes(bg), to_bytes(block)
    bad = _pair_problem(bg_bytes, block_bytes)
    if bad:
        return {
            'x': None, 'y': None, 'n_gaps': None, 'gaps': [],
            'method': None, 'conf': 0.0, 'cands': [], 'error': bad,
        }
    pair = _load_pair(bg_bytes, block_bytes)
    if not pair:
        return {
            'x': None, 'y': None, 'n_gaps': None, 'gaps': [],
            'method': None, 'conf': 0.0, 'cands': [],
            'error': '无法识别缺口',
        }
    bg_arr, combined, crop_y0, crop_x0 = pair
    full = _load_full(extra)
    _bg, blk = _np_bg_block(bg_bytes, block_bytes)
    sl_h, bg_h = blk.shape[0], bg_arr.shape[0]
    pieces = _split_pieces(blk)
    if not pieces:
        pieces = [{'piece': combined, 'x0': crop_x0, 'y0': crop_y0,
                   'w': combined.shape[1], 'h': combined.shape[0],
                   'area': int((combined[:, :, 3] > 32).sum())}]

    gaps = []
    inferred = []  # 换算到「整张滑块左缘」的滑动距离
    for i, p in enumerate(pieces):
        y_lo, y_hi = _y_band(sl_h, bg_h, p['h'], p['y0'])
        hit = _match_piece(bg_arr, p['piece'], bg_bytes=bg_bytes, full=full,
                          y_lo=y_lo, y_hi=y_hi)
        gx, gy = hit.get('x'), hit.get('y')
        if gx is None:
            continue  # 识别失败的块不进 gaps，只返回识别成功的缺口
        gaps.append({
            'i': i,
            'x': gx,
            'y': gy,
            'w': p['w'],
            'h': p['h'],
            'pad_x': p['x0'],
            'pad_y': p['y0'],
            'method': hit.get('method'),
            'conf': hit.get('conf') or 0.0,
            'cands': hit.get('cands') or [],
            'kind': 'piece',
        })
        # 整图左缘应对齐到的 x = 本块缺口x - 本块在滑块图里的左偏移
        inferred.append((gx - p['x0'] + crop_x0, gy - p['y0'] + crop_y0,
                         hit.get('conf') or 0.0, hit.get('method')))

    occupied = [(g['x'], g['y'], g.get('w') or 0, g.get('h') or 0)
                for g in gaps if g.get('x') is not None]
    extras = []
    if occupied:
        extras = _find_extra_holes(
            bg_arr, pieces[0]['piece'],
            _xmin(bg_arr.shape[1], pieces[0]['w']), occupied)
    for hole in extras:
        hole['i'] = len(gaps)
        gaps.append(hole)

    # 整块剪影再投一票。单缺口时和唯一那块重复，不再投，避免把错误结果加一票。
    y_lo_c, y_hi_c = _y_band(sl_h, bg_h, combined.shape[0], crop_y0)
    comb = _match_piece(bg_arr, combined, bg_bytes=bg_bytes, full=full,
                         y_lo=y_lo_c, y_hi=y_hi_c)
    if len(pieces) > 1 and comb.get('x') is not None:
        inferred.append((comb['x'], comb['y'], comb.get('conf') or 0.0,
                         'all:' + str(comb.get('method'))))

    vote_xs = [('g%d' % i, int(x), int(y), float(c))
               for i, (x, y, c, _m) in enumerate(inferred)]
    hit = _agree(vote_xs) if len(vote_xs) >= 2 else None
    if not hit:
        hit = _pick_best(vote_xs)
    if not hit and comb.get('x') is not None:
        hit = {'x': comb['x'], 'y': comb['y'],
               'method': comb.get('method'), 'conf': comb.get('conf')}
    n_piece_ok = sum(1 for g in gaps
                      if g.get('x') is not None and g.get('kind') != 'hole')
    n_gaps = len(pieces) + sum(1 for g in gaps if g.get('kind') == 'hole')
    if not n_gaps:
        n_gaps = len(pieces)
    if not hit and n_piece_ok == 0:
        out = {
            'x': None, 'y': None, 'n_gaps': n_gaps, 'gaps': gaps,
            'method': None, 'conf': 0.0, 'cands': comb.get('cands') or [],
            'pad_x': crop_x0, 'pad_y': crop_y0,
            'error': '无法识别缺口',
        }
        return _fill_trace_pose(out, pieces, bg_arr)

    # 没共识时，用已识别滑块块里置信最高的换算（背景多出来的洞不参与滑动距离）
    if not hit:
        pool = [g for g in gaps
                if g.get('x') is not None and g.get('kind') != 'hole']
        if not pool:
            pool = [g for g in gaps if g.get('x') is not None]
        best = max(pool, key=lambda g: g.get('conf') or 0.0)
        hit = {
            'x': int(best['x'] - (best.get('pad_x') or 0) + crop_x0),
            'y': int((best['y'] or 0) - (best.get('pad_y') or 0) + crop_y0),
            'method': best.get('method'),
            'conf': best.get('conf') or 0.0,
        }

    all_cands = []
    for g in gaps:
        all_cands.extend(g.get('cands') or [])
    all_cands.extend(comb.get('cands') or [])

    out = {
        'x': int(hit['x']),
        'y': int(hit.get('y') if hit.get('y') is not None else crop_y0),
        'n_gaps': n_gaps,
        'gaps': gaps,
        'method': hit.get('method'),
        'conf': float(hit.get('conf') or 0.0),
        'cands': all_cands,
        'pad_x': crop_x0,
        'pad_y': crop_y0,
        'combined': comb,
    }
    return _fill_trace_pose(out, pieces, bg_arr)


def find_gap(bg, block, extra=None):
    """只要滑动距离 x；识别不了返回 None。"""
    info = find_gap_info(bg, block, extra=extra)
    if not info or info.get('x') is None:
        return None
    return int(info['x'])


# ---------------------------------------------------------------------------
# 画框
# ---------------------------------------------------------------------------
def _draw_piece_outline(im, piece, x, y, color, cut=160):
    """沿剪影画抗锯齿闭合线，先闭运算抹掉剥光晕留下的毛刺。"""
    from PIL import Image
    try:
        import cv2
        import numpy as np
    except Exception:
        cv2 = None
        np = None
    if cv2 is None:
        import numpy as np
        mask = piece[:, :, 3]
        ph, pw = mask.shape
        w, h = im.size
        ys, xs = np.where(mask >= cut)
        rgb = tuple(int(c) for c in color)
        for yy, xx in zip(ys.tolist(), xs.tolist()):
            edge = (yy == 0 or xx == 0 or yy == ph - 1 or xx == pw - 1
                    or mask[yy - 1, xx] < cut or mask[yy + 1, xx] < cut
                    or mask[yy, xx - 1] < cut or mask[yy, xx + 1] < cut)
            if not edge:
                continue
            px, py = int(x) + xx, int(y) + yy
            if 0 <= px < w and 0 <= py < h:
                im.putpixel((px, py), rgb)
        return im
    mask = (piece[:, :, 3] >= cut).astype(np.uint8) * 255
    if int(mask.sum()) < 20:
        return im
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    mask = cv2.GaussianBlur(mask, (3, 3), 0)
    _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    arr = np.array(im.convert('RGB'))
    rgb = tuple(int(c) for c in color)
    ox, oy = int(x), int(y)
    for c in cnts:
        if cv2.contourArea(c) < 24:
            continue
        peri = cv2.arcLength(c, True)
        poly = cv2.approxPolyDP(c, max(1.0, 0.01 * peri), True)
        poly = poly + np.array([[[ox, oy]]])
        cv2.polylines(arr, [poly], True, rgb, 1, lineType=cv2.LINE_AA)
    return Image.fromarray(arr)


def _paste_overlay(im, piece, x, y, alpha=0.45):
    from PIL import Image
    base = im.convert('RGBA')
    piece_im = Image.fromarray(piece)
    overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
    overlay.paste(piece_im, (int(x), int(y)), piece_im)
    a = overlay.split()[3].point(lambda p: int(p * alpha))
    overlay.putalpha(a)
    return Image.alpha_composite(base, overlay).convert('RGB')


def render_boxes(bg, block, info, extra=None):
    """把每个缺口画框 + 半透明贴上滑块，返回 PIL.Image。"""
    from PIL import Image, ImageDraw
    bg_bytes, block_bytes = to_bytes(bg), to_bytes(block)
    im = Image.open(__import__('io').BytesIO(bg_bytes)).convert('RGB')
    draw = ImageDraw.Draw(im)
    _bg, blk = _np_bg_block(bg_bytes, block_bytes)
    pieces = _split_pieces(blk)
    pair = _load_pair(bg_bytes, block_bytes)
    if not pieces and pair:
        _b, combined, y0, x0 = pair
        pieces = [{'piece': combined, 'x0': x0, 'y0': y0,
                   'w': combined.shape[1], 'h': combined.shape[0]}]

    gaps = (info or {}).get('gaps') or []
    if not gaps and info and info.get('x') is not None and pieces:
        g0 = pieces[0]
        gaps = [{'x': info['x'] + g0['x0'] - (info.get('pad_x') or 0),
                 'y': info.get('y') if info.get('y') is not None else g0['y0'],
                 'w': g0['w'], 'h': g0['h'],
                 'pad_x': g0['x0'], 'pad_y': g0['y0']}]

    gray = None
    try:
        import cv2
        gray = cv2.cvtColor(_bg, cv2.COLOR_RGB2GRAY)
    except Exception:
        gray = None

    for i, g in enumerate(gaps):
        if g.get('x') is None:
            continue
        color = GAP_COLORS[i % len(GAP_COLORS)]
        x, y = int(g['x']), int(g['y'] if g.get('y') is not None else 0)
        if g.get('kind') == 'hole':
            continue
        idx = g.get('i')
        src = (pieces[idx] if isinstance(idx, int) and 0 <= idx < len(pieces)
               else (pieces[0] if pieces else None))
        piece = src['piece'] if src else None
        if piece is not None:
            piece, x, y = _display_piece(gray, piece, x, y)
            pw, ph = piece.shape[1], piece.shape[0]
            try:
                im = _paste_overlay(im, piece, x, y, 0.50)
            except Exception:
                pass
            im = _draw_piece_outline(im, piece, x, y, color)
            draw = ImageDraw.Draw(im)
        else:
            pw, ph = int(g.get('w') or 40), int(g.get('h') or 40)
        draw.rectangle([x, y, x + pw - 1, y + ph - 1],
                       outline=color, width=1)
    return im


def _first_piece_h(info):
    for g in (info or {}).get('gaps') or []:
        if g.get('kind') == 'hole':
            continue
        if g.get('h'):
            return int(g['h'])
    return None


def _display_piece(gray, piece, x, y, clip_box=True):
    """与识别图画框同一套剪影（实心、剥光晕、可选贴缺口），返回 (piece, x, y)。"""
    if piece is None:
        return None, int(x), int(y)
    piece, dx, dy = _solid_piece(piece)
    x, y = int(x) + dx, int(y) + dy
    if gray is not None:
        piece, dx, dy = _peel_halo(gray, piece, x, y)
        x, y = x + dx, y + dy
        if clip_box:
            nx, ny, nw, nh = _fit_box(gray, piece, x, y)
            piece = _slice_piece(piece, x, y, nx, ny, nw, nh)
            x, y = nx, ny
    return piece, x, y


def _shape_edge_anchor(piece, cut=160):
    """显示框左缘中点：与滑块图最左边对齐。"""
    if piece is None or getattr(piece, 'size', 0) == 0:
        return 0, 0
    h = int(piece.shape[0])
    return 0, max(0, h // 2)


def _fill_trace_pose(info, pieces, bg_arr=None):
    """起点/终点在识别框左缘，平移距离不含 pad 重复。"""
    if not info:
        return info
    pad_x = int(info.get('pad_x') or 0)
    dest_x, dest_y = info.get('x'), info.get('y')
    if dest_x is None:
        info['sx'] = pad_x
        info['sy'] = dest_y
        info['tdist'] = None
        return info
    dest_x = int(dest_x)
    dest_y = int(dest_y if dest_y is not None else 0)
    for g in info.get('gaps') or []:
        if g.get('kind') == 'hole' or g.get('x') is None:
            continue
        dest_x = int(g['x'])
        dest_y = int(g['y'] if g.get('y') is not None else dest_y)
        if g.get('pad_x') is not None:
            pad_x = int(g['pad_x'])
        break
    tdist = dest_x - pad_x
    if tdist < 0:
        tdist = 0
    gray = None
    if bg_arr is not None:
        try:
            import cv2
            import numpy as np
            if bg_arr.ndim == 3:
                gray = cv2.cvtColor(np.asarray(bg_arr), cv2.COLOR_RGB2GRAY)
            else:
                gray = bg_arr
        except Exception:
            gray = None
    lx, ly = 0, 0
    px, py = dest_x, dest_y
    if pieces:
        disp, px, py = _display_piece(
            gray, pieces[0].get('piece'), dest_x, dest_y, clip_box=True)
        lx, ly = _shape_edge_anchor(disp)
    info['sx'] = px - tdist + lx
    info['sy'] = py + ly
    info['tdist'] = int(tdist)
    return info


def _write_meta(folder, prefix, info=None, group='', error=None):
    import json
    path = os.path.join(folder, 'meta.json')
    old = {}
    if os.path.isfile(path):
        try:
            old = json.load(open(path, encoding='utf-8')) or {}
        except Exception:
            old = {}
    ok = bool(info and info.get('x') is not None)
    if ok:
        err = None
    elif error:
        err = error
    else:
        err = (info or {}).get('error') or None
    meta = {
        'tag': prefix,
        'group': group or prefix,
        'x': None if not ok else info.get('x'),
        'y': None if not ok else info.get('y'),
        'n_gaps': None if not ok else info.get('n_gaps'),
        'method': None if not ok else info.get('method'),
        'conf': None if not ok else info.get('conf'),
        'pad_x': None if not ok else info.get('pad_x'),
        'pad_y': None if not ok else info.get('pad_y'),
        'ph': None if not ok else _first_piece_h(info),
        'sx': None if not ok else info.get('sx'),
        'sy': None if not ok else info.get('sy'),
        'tdist': None if not ok else info.get('tdist'),
        'error': err,
    }
    created = old.get('created')
    if created:
        meta['created'] = created
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def _is_boxed_filename(name):
    stem = os.path.splitext(name)[0]
    low = name.lower()
    return (stem.endswith('_识别') or stem.endswith('_识别框')
            or stem.lower().endswith('_boxed') or stem.lower().endswith('_box')
            or low in ('boxed.png', '识别.png', '识别框.png'))


def _remove_boxed(folder, keep=None):
    if not os.path.isdir(folder):
        return
    keep = os.path.basename(keep) if keep else None
    for n in os.listdir(folder):
        if not _is_boxed_filename(n):
            continue
        if keep and n == keep:
            continue
        try:
            os.remove(os.path.join(folder, n))
        except OSError:
            pass


def save_raw(bg, block, folder, extra=None, group='', tag='', boxed=None):
    """只存图，不跑识别。背景/滑块可只传一张；传了图则清掉旧识别图。"""
    os.makedirs(folder, exist_ok=True)
    prefix = tag or group or os.path.basename(folder)
    wrote = False
    if bg is not None:
        open(os.path.join(folder, prefix + '_背景.png'), 'wb').write(_png_bytes(bg, alpha=False))
        wrote = True
    if block is not None:
        open(os.path.join(folder, prefix + '_滑块.png'), 'wb').write(_png_bytes(block, alpha=True))
        wrote = True
    boxed_path = os.path.join(folder, prefix + '_识别.png')
    if boxed:
        open(boxed_path, 'wb').write(to_bytes(boxed))
    elif wrote:
        _remove_boxed(folder)
        boxed_path = None
    elif not os.path.isfile(boxed_path):
        boxed_path = None
    _write_meta(folder, prefix, info=None, group=group or prefix)
    return boxed_path


def save_boxed(bg, block, info, folder, extra=None, group='', tag=''):
    """按当前背景/滑块重识别，覆盖 {前缀}_识别.png 和 meta，并清掉旧 boxed/识别框。

    完全失败（放反、同一张图、对不上）不写识别图，只把错误记进 meta。
    """
    os.makedirs(folder, exist_ok=True)
    prefix = tag or group or os.path.basename(folder)
    boxed_path = os.path.join(folder, prefix + '_识别.png')
    if not info or info.get('x') is None:
        _remove_boxed(folder)
        _write_meta(folder, prefix, info=info, group=group or prefix,
                    error=(info or {}).get('error') or '无法识别缺口')
        return None
    bg_b, block_b = to_bytes(bg), to_bytes(block)
    extra_b = to_bytes(extra) if extra is not None else None
    keep = os.path.basename(boxed_path)
    _remove_boxed(folder, keep=keep)
    render_boxes(bg_b, block_b, info, extra=extra_b).save(boxed_path)
    _write_meta(folder, prefix, info=info, group=group or prefix)
    return boxed_path


def save_case(bg, block, info, folder, extra=None, group='', tag=''):
    """一组三张图：背景 / 滑块 / 识别。"""
    save_raw(bg, block, folder, extra=extra, group=group, tag=tag, boxed=None)
    return save_boxed(bg, block, info, folder, extra=extra, group=group, tag=tag)
