# -*- coding: utf-8 -*-
"""滑块验证码识别。

    from utils.SliderCaptchaOcr import recognize, make_trace
    x = recognize(bg, block)
    x = recognize(bg, block, save_local=True, tag='geetest_01')
    pts = make_trace(x)                 # 非匀速贝塞尔
    pts = make_trace(x, uniform=True)   # 匀速
"""
import os
import re
import time

from .detector import find_gap_info, save_case, to_bytes
from .trace import make_trace

__version__ = '1.3.0'
__all__ = ['recognize', 'prefix_from_filename', 'make_trace']

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT_NAME = 'CapchaImg'
_LEGACY_ROOT_NAMES = ('captcha_img', 'captchaimg', 'CaptChaImg')


def _named_dir(here, wanted):
    try:
        for n in os.listdir(here):
            if n.lower() == wanted.lower() and os.path.isdir(os.path.join(here, n)):
                return n
    except OSError:
        pass
    return None


def _resolve_img_dir(here, name=DEFAULT_ROOT_NAME):
    dest = os.path.join(here, name)
    actual = _named_dir(here, name)
    if actual and actual != name:
        tmp = os.path.join(here, name + '.__rename__')
        try:
            os.rename(os.path.join(here, actual), tmp)
            os.rename(tmp, dest)
        except OSError:
            dest = os.path.join(here, actual)
    elif not actual:
        for legacy in _LEGACY_ROOT_NAMES:
            old = _named_dir(here, legacy)
            if not old:
                continue
            try:
                os.rename(os.path.join(here, old), dest)
            except OSError:
                dest = os.path.join(here, old)
            break
    os.makedirs(dest, exist_ok=True)
    return dest


IMG_DIR = _resolve_img_dir(_HERE, DEFAULT_ROOT_NAME)

_STRIP_SUFFIXES = (
    '_识别框', '_识别', '_背景', '_滑块',
    '_background', '_slider', '_boxed', '_block',
    '_piece', '_target', '_gap', '_bg', '_box',
)


def _safe_tag(s):
    s = re.sub(r'[\\/:*?"<>|\s]+', '_', (s or '').strip())
    s = s.strip('._')
    return s or None


def prefix_from_filename(name):
    """geetest_01_滑块.png / geetest_01.png -> geetest_01。"""
    stem = os.path.splitext(os.path.basename(name or ''))[0].strip()
    if not stem:
        return None
    for suf in sorted(_STRIP_SUFFIXES, key=len, reverse=True):
        if stem.endswith(suf):
            stem = stem[:-len(suf)]
            break
        if stem.lower().endswith(suf.lower()):
            stem = stem[:len(stem) - len(suf)]
            break
    return _safe_tag(stem)


def pick_prefix(*names):
    prefs = [prefix_from_filename(n) for n in names]
    prefs = [p for p in prefs if p]
    if not prefs:
        return None
    generic = {'背景', '滑块', '识别', '识别框', 'bg', 'slider', 'boxed', 'block', 'box'}
    ranked = [p for p in prefs if p.lower() not in generic]
    if len(set(ranked)) == 1:
        return ranked[0]
    if ranked:
        return ranked[0]
    return prefs[0]


def _missing_of(bg, block):
    missing = []
    if bg is None:
        missing.append('bg')
    if block is None:
        missing.append('block')
    return missing


def recognize(bg, block, save_local=False, tag='', out_dir=None,
              extra=None, group=''):
    """识别缺口，返回滑动距离 x；缺图或失败返回 None。

    缺背景或缺滑块时不识别。save_local=True 仍可只落已有的那张。
    写到 out_dir/{前缀}/（默认 CapchaImg/geetest_01/）。
    """
    missing = _missing_of(bg, block)
    if missing:
        if save_local and (bg is not None or block is not None or tag or group):
            try:
                from .detector import save_raw
                dest = out_dir or IMG_DIR
                name = (tag or group or pick_prefix(tag, group)
                        or ('Card_%s' % time.strftime('%Y%m%d_%H%M%S')))
                name = _safe_tag(name) or ('Card_%s' % time.strftime('%Y%m%d_%H%M%S'))
                save_raw(None if 'bg' in missing else bg,
                         None if 'block' in missing else block,
                         os.path.join(dest, name), group=name, tag=name)
            except Exception:
                pass
        return None
    try:
        bg_b, block_b = to_bytes(bg), to_bytes(block)
        extra_b = to_bytes(extra) if extra is not None else None
    except Exception:
        return None
    try:
        info = find_gap_info(bg_b, block_b, extra=extra_b)
    except Exception:
        info = None

    if save_local:
        try:
            dest = out_dir or IMG_DIR
            name = (tag or group or pick_prefix(tag, group)
                    or ('Card_%s' % time.strftime('%Y%m%d_%H%M%S')))
            name = _safe_tag(name) or ('Card_%s' % time.strftime('%Y%m%d_%H%M%S'))
            folder = os.path.join(dest, name)
            save_case(bg_b, block_b, info, folder,
                     extra=extra_b, group=name, tag=name)
        except Exception:
            pass
    if not info or info.get('x') is None:
        return None
    return int(info['x'])
