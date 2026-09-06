# -*- coding: utf-8 -*-
"""对照页：读 CapchaImg/{前缀}/。卡片里单独传背景或滑块，点「识别」才出识别图。

    python -m utils.SliderCaptchaOcr
    python -m utils.SliderCaptchaOcr --dir CapchaImg
"""
import json
import os
import re
import shutil
import sys
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse

from . import DEFAULT_ROOT_NAME, _resolve_img_dir, _safe_tag, pick_prefix
from .detector import _write_meta

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, 'Web')
if not os.path.isdir(WEB_DIR):
    WEB_DIR = os.path.join(HERE, 'web')
IMG_ROOT = _resolve_img_dir(HERE, DEFAULT_ROOT_NAME)

_STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
}

_ALIASES = {
    'bg': ('bg.png', 'bg.jpg', 'bg.webp', '背景.png', '背景.jpg'),
    'slider': ('slider.png', 'slider.jpg', '滑块.png', 'block.png', 'piece.png'),
    'boxed': ('boxed.png', 'boxed.jpg', '识别框.png', '识别.png', 'box.png'),
}

_KIND_TAILS = {
    'bg': ('_背景', '_background', '_bg', '_gap'),
    'slider': ('_滑块', '_slider', '_block', '_piece', '_target'),
    'boxed': ('_识别框', '_识别', '_boxed', '_box'),
}

_MISSING_LABEL = {'bg': '背景', 'slider': '滑块'}


def _is_under(root, path):
    try:
        a, b = os.path.abspath(root), os.path.abspath(path)
        return os.path.commonpath([a, b]) == a
    except ValueError:
        return False


def _pick(folder, kind):
    names = os.listdir(folder)
    lower = {n.lower(): n for n in names}
    tails = sorted(_KIND_TAILS[kind], key=len, reverse=True)
    hits = []
    for n in names:
        if n.lower() == 'meta.json':
            continue
        stem, ext = os.path.splitext(n)
        if ext.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'):
            continue
        for t in tails:
            if stem.endswith(t) or stem.lower().endswith(t.lower()):
                hits.append(n)
                break
    if hits:
        if kind == 'boxed':
            pref = [n for n in hits
                    if os.path.splitext(n)[0].endswith('_识别')
                    and not os.path.splitext(n)[0].endswith('_识别框')]
            if pref:
                return sorted(pref)[0]
        return sorted(hits)[0]
    for alias in _ALIASES[kind]:
        if alias.lower() in lower:
            return lower[alias.lower()]
    return None


def _unique_card_name():
    base = 'Card_%s' % time.strftime('%Y%m%d_%H%M%S')
    name = base
    n = 2
    while os.path.isdir(os.path.join(IMG_ROOT, name)):
        name = '%s_%d' % (base, n)
        n += 1
    return name


def _card_time(it):
    """把 created / Card_日期 / Card_时间戳 收成同一套时间串，新卡排在后面。"""
    created = str((it or {}).get('created') or '')
    if created:
        return created
    name = str((it or {}).get('id') or (it or {}).get('group') or '')
    m = re.match(r'^Card_(\d{8})_(\d{6})', name)
    if m:
        d, t = m.group(1), m.group(2)
        return '%s-%s-%sT%s:%s:%s' % (d[:4], d[4:6], d[6:8], t[:2], t[2:4], t[4:6])
    m = re.match(r'^Card_(\d{12,})$', name)
    if m:
        try:
            ts = int(m.group(1))
            if ts > 10 ** 12:
                ts = ts / 1000.0
            return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts))
        except (ValueError, OSError):
            pass
    return created or name


def _order(it):
    name = str(it.get('id') or '')
    if name.startswith('Card_'):
        return (1, _card_time(it), name)
    return (0, name)


def list_cases():
    items = []
    if not os.path.isdir(IMG_ROOT):
        return items
    for name in sorted(os.listdir(IMG_ROOT)):
        folder = os.path.join(IMG_ROOT, name)
        if not os.path.isdir(folder):
            continue
        try:
            files = {k: _pick(folder, k) for k in _ALIASES}
        except OSError:
            continue
        meta = {}
        mp = os.path.join(folder, 'meta.json')
        if os.path.isfile(mp):
            try:
                meta = json.load(open(mp, encoding='utf-8'))
            except Exception:
                meta = {}
        def url(fn):
            if not fn:
                return None
            from urllib.parse import quote
            path = os.path.join(folder, fn)
            t = int(os.path.getmtime(path) * 1000) if os.path.isfile(path) else 0
            return '/img/%s/%s?t=%d' % (quote(name), quote(fn), t)
        has_box = bool(files['boxed']) and meta.get('x') is not None
        err = None if has_box else (meta.get('error') or None)
        items.append({
            'id': name,
            'group': name,
            'created': meta.get('created') or '',
            'x': meta.get('x') if has_box else None,
            'y': meta.get('y') if has_box else None,
            'n_gaps': meta.get('n_gaps') if has_box else None,
            'conf': meta.get('conf') if has_box else None,
            'pad_x': meta.get('pad_x') if has_box else None,
            'ph': meta.get('ph') if has_box else None,
            'sx': meta.get('sx') if has_box else None,
            'sy': meta.get('sy') if has_box else None,
            'tdist': meta.get('tdist') if has_box else None,
            'error': err,
            'bg': url(files['bg']),
            'slider': url(files['slider']),
            'boxed': url(files['boxed']) if has_box else None,
        })
    items.sort(key=_order)
    return items


def _info_to_payload(info, boxed_bytes):
    import base64
    from .detector import _first_piece_h
    ok = bool(info and info.get('x') is not None)
    boxed = None
    if ok and boxed_bytes:
        boxed = 'data:image/png;base64,' + base64.b64encode(boxed_bytes).decode('ascii')
    return {
        'x': None if not ok else info.get('x'),
        'y': None if not ok else info.get('y'),
        'n_gaps': None if not ok else info.get('n_gaps'),
        'method': None if not ok else info.get('method'),
        'conf': None if not ok else info.get('conf'),
        'pad_x': None if not ok else info.get('pad_x'),
        'ph': None if not ok else _first_piece_h(info),
        'sx': None if not ok else info.get('sx'),
        'sy': None if not ok else info.get('sy'),
        'tdist': None if not ok else info.get('tdist'),
        'error': None if ok else ((info or {}).get('error') or '无法识别缺口'),
        'boxed': boxed,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'slider-ocr/1.3'

    def log_message(self, fmt, *args):
        sys.stderr.write('[%s] %s\n' % (self.log_date_time_string(), fmt % args))

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   'application/json; charset=utf-8')

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        static = _STATIC.get(path)
        if static:
            fn, ctype = static
            full = os.path.join(WEB_DIR, fn)
            if not os.path.isfile(full):
                self._send(404, fn + ' 不存在', 'text/plain; charset=utf-8')
                return
            self._send(200, open(full, 'rb').read(), ctype)
            return
        if path == '/api/cases':
            self._json(list_cases())
            return
        if path == '/api/health':
            self._json({
                'ok': True,
                'root': os.path.basename(IMG_ROOT.rstrip('\\/')) or DEFAULT_ROOT_NAME,
            })
            return
        if path.startswith('/img/'):
            rel = path[len('/img/'):].replace('\\', '/')
            full = os.path.abspath(os.path.join(IMG_ROOT, rel))
            if not _is_under(IMG_ROOT, full) or not os.path.isfile(full):
                self.send_error(404)
                return
            ext = os.path.splitext(full)[1].lower()
            ctype = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.webp': 'image/webp', '.gif': 'image/gif', '.json': 'application/json',
            }.get(ext, 'application/octet-stream')
            self._send(200, open(full, 'rb').read(), ctype)
            return
        self.send_error(404)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == '/api/save':
            self._save(self._read_json())
            return
        if path == '/api/new':
            self._new(self._read_json() or {})
            return
        if path == '/api/recognize':
            self._recognize(self._read_json())
            return
        if path == '/api/fetch':
            self._fetch(self._read_json())
            return
        if path == '/api/delete':
            self._delete(self._read_json())
            return
        if path == '/api/rename':
            self._rename(self._read_json())
            return
        if path == '/api/trace':
            self._trace(self._read_json())
            return
        self.send_error(404)

    def _fetch(self, body):
        if body is None:
            return
        url = (body.get('url') or '').strip()
        if not url:
            self._json({'error': '需要 url'}, 400)
            return
        try:
            from .detector import to_bytes
            import base64
            raw = to_bytes(url)
            if raw[:8] == b'\x89PNG\r\n\x1a\n':
                mime = 'image/png'
            elif raw[:2] == b'\xff\xd8':
                mime = 'image/jpeg'
            elif raw[:6] in (b'GIF87a', b'GIF89a'):
                mime = 'image/gif'
            elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
                mime = 'image/webp'
            else:
                mime = 'image/png'
            self._json({
                'data': 'data:%s;base64,%s' % (
                    mime, base64.b64encode(raw).decode('ascii')),
            })
        except Exception as e:
            self._json({'error': str(e)}, 400)

    def _read_json(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n < 0 or n > 20 * 1024 * 1024:
            self._json({'error': '请求过大或为空'}, 413)
            return None
        if n == 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            self._json({'error': 'JSON 无效'}, 400)
            return None

    def _new(self, body):
        if body is None:
            return
        prefix = _safe_tag(body.get('tag') or body.get('id') or '') or _unique_card_name()
        folder = os.path.join(IMG_ROOT, prefix)
        if not _is_under(IMG_ROOT, folder):
            self._json({'error': '组名无效'}, 400)
            return
        try:
            os.makedirs(folder, exist_ok=True)
            _write_meta(folder, prefix, info=None, group=prefix)
            mp = os.path.join(folder, 'meta.json')
            try:
                meta = json.load(open(mp, encoding='utf-8'))
            except Exception:
                meta = {'tag': prefix, 'group': prefix}
            meta['created'] = time.strftime('%Y-%m-%dT%H:%M:%S')
            with open(mp, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._json({'error': str(e)}, 400)
            return
        self._json({'saved': prefix, 'group': prefix, 'id': prefix})

    def _save(self, body):
        if body is None:
            return
        bg = body.get('bg')
        slider = body.get('slider') or body.get('block')
        if not bg and not slider:
            self._json({'error': '需要背景或滑块', 'missing': ['bg', 'slider']}, 400)
            return
        try:
            from .detector import save_raw, to_bytes
            bg_b = to_bytes(bg) if bg else None
            slider_b = to_bytes(slider) if slider else None
            boxed_b = to_bytes(body['boxed']) if body.get('boxed') else None
            prefix = _safe_tag(body.get('id') or '') or pick_prefix(
                body.get('bg_name'), body.get('slider_name'),
                body.get('tag'), body.get('name'),
            )
            prefix = prefix or ('Card_%s' % time.strftime('%Y%m%d_%H%M%S'))
            folder = os.path.join(IMG_ROOT, prefix)
            if not _is_under(IMG_ROOT, folder):
                self._json({'error': '组名无效'}, 400)
                return
            save_raw(bg_b, slider_b, folder, group=prefix, tag=prefix, boxed=boxed_b)
        except Exception as e:
            self._json({'error': str(e)}, 400)
            return
        self._json({'saved': prefix, 'group': prefix, 'id': prefix})

    def _delete(self, body):
        if body is None:
            return
        name = _safe_tag(body.get('id') or body.get('group') or '')
        if not name:
            self._json({'error': '需要组名 id'}, 400)
            return
        folder = os.path.join(IMG_ROOT, name)
        if not _is_under(IMG_ROOT, folder) or not os.path.isdir(folder):
            self._json({'error': '组不存在'}, 404)
            return
        try:
            shutil.rmtree(folder)
        except Exception as e:
            self._json({'error': str(e)}, 400)
            return
        self._json({'deleted': name})

    def _rename(self, body):
        if body is None:
            return
        old = _safe_tag(body.get('id') or body.get('from') or '')
        new = _safe_tag(body.get('name') or body.get('to') or '')
        if not old or not new:
            self._json({'error': '需要原组名和新组名'}, 400)
            return
        if old == new:
            self._json({'id': new, 'group': new, 'saved': new})
            return
        src = os.path.join(IMG_ROOT, old)
        dst = os.path.join(IMG_ROOT, new)
        if not _is_under(IMG_ROOT, src) or not _is_under(IMG_ROOT, dst):
            self._json({'error': '组名无效'}, 400)
            return
        if not os.path.isdir(src):
            self._json({'error': '组不存在'}, 404)
            return
        taken = None
        try:
            for n in os.listdir(IMG_ROOT):
                p = os.path.join(IMG_ROOT, n)
                if os.path.isdir(p) and n.lower() == new.lower() and n != old:
                    taken = n
                    break
        except OSError:
            pass
        if taken:
            self._json({'error': '组名已存在'}, 400)
            return
        meta = {}
        mp = os.path.join(src, 'meta.json')
        if os.path.isfile(mp):
            try:
                meta = json.load(open(mp, encoding='utf-8'))
            except Exception:
                meta = {}
        try:
            if old.lower() == new.lower():
                tmp = src + '.__rename__'
                os.rename(src, tmp)
                os.rename(tmp, dst)
            else:
                os.rename(src, dst)
            for n in os.listdir(dst):
                if n.lower() == 'meta.json':
                    continue
                stem, ext = os.path.splitext(n)
                if stem != old and not stem.startswith(old + '_'):
                    continue
                newn = new + stem[len(old):] + ext
                if newn == n:
                    continue
                a, b = os.path.join(dst, n), os.path.join(dst, newn)
                if os.path.abspath(a) == os.path.abspath(b):
                    continue
                if os.path.exists(b):
                    os.remove(b)
                os.rename(a, b)
            meta['tag'] = new
            meta['group'] = new
            with open(os.path.join(dst, 'meta.json'), 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._json({'error': str(e)}, 400)
            return
        self._json({'id': new, 'group': new, 'saved': new, 'from': old})

    def _trace(self, body):
        if body is None:
            return
        from .trace import make_trace
        items = body.get('items')
        if not isinstance(items, list) or not items:
            if body.get('distance') is None and body.get('x') is None:
                self._json({'error': '需要 distance'}, 400)
                return
            items = [body]
        uniform = bool(body.get('uniform'))
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            dist = it.get('distance')
            if dist is None:
                dist = it.get('x')
            try:
                dist = float(dist)
            except (TypeError, ValueError):
                self._json({'error': 'distance 无效'}, 400)
                return
            if dist > 8000:
                dist = 8000.0
            start = it.get('start') or (0, 0)
            if isinstance(start, (list, tuple)) and len(start) >= 2:
                sx, sy = start[0], start[1]
            else:
                sx, sy = 0, start if isinstance(start, (int, float)) else 0
            try:
                sx, sy = float(sx or 0), float(sy or 0)
            except (TypeError, ValueError):
                sx, sy = 0.0, 0.0
            pts = make_trace(
                dist,
                uniform=bool(it.get('uniform', uniform)),
                start=(sx, sy),
            )
            out.append({'id': it.get('id'), 'points': pts})
        self._json({'traces': out})

    def _recognize(self, body):
        if body is None:
            return
        from .detector import find_gap_info, save_boxed, render_boxes, to_bytes
        from io import BytesIO
        bg_in, sl_in = body.get('bg'), body.get('slider') or body.get('block')
        save = bool(body.get('save'))
        name = _safe_tag(body.get('id') or body.get('tag') or '')
        bg_b = slider_b = None
        if bg_in and sl_in:
            try:
                bg_b, slider_b = to_bytes(bg_in), to_bytes(sl_in)
            except Exception as e:
                self._json({'error': str(e)}, 400)
                return
        else:
            if not name:
                self._json({'error': '需要组名 id'}, 400)
                return
            folder = os.path.join(IMG_ROOT, name)
            if not _is_under(IMG_ROOT, folder) or not os.path.isdir(folder):
                self._json({'error': '组不存在'}, 404)
                return
            bg_fn, sl_fn = _pick(folder, 'bg'), _pick(folder, 'slider')
            missing = []
            if not bg_fn:
                missing.append('bg')
            if not sl_fn:
                missing.append('slider')
            if missing:
                msg = '缺少' + '和'.join(_MISSING_LABEL[m] for m in missing)
                self._json({'error': msg, 'missing': missing}, 400)
                return
            bg_b = open(os.path.join(folder, bg_fn), 'rb').read()
            slider_b = open(os.path.join(folder, sl_fn), 'rb').read()
        try:
            info = find_gap_info(bg_b, slider_b)
            if save and name:
                folder = os.path.join(IMG_ROOT, name)
                if _is_under(IMG_ROOT, folder) and os.path.isdir(folder):
                    save_boxed(bg_b, slider_b, info, folder, group=name, tag=name)
        except Exception as e:
            self._json({'error': str(e)}, 400)
            return
        boxed_bytes = None
        if info and info.get('x') is not None:
            buf = BytesIO()
            render_boxes(bg_b, slider_b, info).save(buf, format='PNG')
            boxed_bytes = buf.getvalue()
        payload = _info_to_payload(info, boxed_bytes)
        if name:
            payload['saved'] = name
            payload['group'] = name
        self._json(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Allow', 'GET, POST, OPTIONS')
        self.end_headers()


def main(argv=None):
    import argparse
    import webbrowser

    p = argparse.ArgumentParser(description='滑块识别对照页')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--dir', default=DEFAULT_ROOT_NAME,
                   help='图片根目录，默认 CapchaImg（相对本模块或绝对路径）')
    p.add_argument('--no-browser', action='store_true')
    args = p.parse_args(argv)
    global IMG_ROOT
    if os.path.isabs(args.dir):
        IMG_ROOT = args.dir
        os.makedirs(IMG_ROOT, exist_ok=True)
    elif args.dir.lower() in (DEFAULT_ROOT_NAME.lower(), 'captcha_img', 'captchaimg'):
        IMG_ROOT = _resolve_img_dir(HERE, DEFAULT_ROOT_NAME)
    else:
        IMG_ROOT = os.path.join(HERE, args.dir)
        os.makedirs(IMG_ROOT, exist_ok=True)
    httpd = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = 'http://127.0.0.1:%d/' % args.port
    print('对照页  %s' % url, flush=True)
    print('本地图  %s' % IMG_ROOT, flush=True)
    if not args.no_browser:
        import threading
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
        httpd.server_close()


if __name__ == '__main__':
    main()
