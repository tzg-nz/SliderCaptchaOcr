# -*- coding: utf-8 -*-
"""按滑动距离生成拟人拖拽轨迹。"""
import random

__all__ = ['make_trace']


def _bezier3(p0, p1, p2, p3, t):
    u = 1.0 - t
    uu, tt = u * u, t * t
    a, b, c, d = uu * u, 3 * uu * t, 3 * u * tt, tt * t
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _ease_in_out(t):
    """慢起、中间快、慢停。"""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    if t < 0.5:
        return 4.0 * t * t * t
    u = -2.0 * t + 2.0
    return 1.0 - u * u * u / 2.0


def _pt(x, y, t):
    return [round(float(x), 1), round(float(y), 1), int(t)]


def make_trace(distance, uniform=False, duration_ms=None, start=(0, 0),
               start_ts=0):
    """按滑动距离生成轨迹 [[x, y, t], ...]。

    x/y 相对 start（默认原点），t 为累计毫秒（默认从 0）。
    uniform=False（默认）：按下略停 → 三次贝塞尔缓入缓出 → 末端过冲 2~4px 再回拉。
    控制点夹在起点～过冲峰值之间，不会大段绕到落点外侧。
    uniform=True：直线匀速，等间隔采样。

        from utils.SliderCaptchaOcr import recognize, make_trace
        x = recognize(bg, block)
        pts = make_trace(x)
        pts = make_trace(x, uniform=True)
    """
    dist = float(distance or 0)
    if dist < 0:
        dist = 0.0
    x0, y0 = float(start[0]), float(start[1])
    t0 = int(start_ts or 0)

    if duration_ms is None:
        duration_ms = int(480 + dist * 2.4 + random.randint(40, 180))
    duration_ms = max(220, int(duration_ms))

    if uniform:
        x1, y1 = x0 + dist, y0
        n = max(8, min(48, int(12 + dist / 8.0)))
        dt = duration_ms / float(n - 1)
        pts = []
        for i in range(n):
            r = i / float(n - 1)
            pts.append(_pt(x0 + dist * r, y0, t0 + int(round(dt * i))))
        pts[-1][0], pts[-1][1] = round(x1, 1), round(y1, 1)
        return pts

    # 真正松手的位置：识别距离附近漂一点
    land = dist + random.uniform(-0.45, 0.55)
    if land < 0:
        land = 0.0
    x1 = x0 + land
    y1 = y0 + random.uniform(-0.35, 0.35)

    # 末端小过冲再回拉（短距离不做）
    over = random.uniform(1.8, 3.8) if land >= 24 else 0.0
    peak = land + over
    p3 = (x0 + peak, y0 + random.uniform(-0.3, 0.3))
    x_a = x0 + peak * random.uniform(0.22, 0.36)
    x_b = x0 + peak * random.uniform(0.58, 0.76)
    if x_a > x_b:
        x_a, x_b = x_b, x_a
    x_b = min(x_b, p3[0])
    x_a = min(max(x_a, x0), x_b)
    arc = random.uniform(1.4, 3.6) * random.choice((-1.0, 1.0))
    p1 = (x_a, y0 + arc * random.uniform(0.40, 0.85))
    p2 = (
        x_b,
        y0 + arc * random.uniform(0.10, 0.40) * random.choice((-1.0, 1.0)),
    )

    pts = [_pt(x0, y0, t0)]
    t = t0
    # 按下后停几十毫秒再拖
    n_press = random.randint(2, 4)
    for _ in range(n_press):
        t += random.randint(18, 36)
        pts.append(_pt(
            x0 + abs(random.gauss(0.0, 0.12)),
            y0 + random.gauss(0.0, 0.18),
            t,
        ))

    n = max(28, min(56, int(22 + peak / 4.2)))
    tremor_x = 0.0
    tremor_y = 0.0
    hx, hy = float(pts[-1][0]), float(pts[-1][1])
    p0 = (hx, hy)
    for i in range(1, n):
        s = i / float(n - 1)
        u = _ease_in_out(s)
        x, y = _bezier3(p0, p1, p2, p3, u)
        tremor_x = tremor_x * 0.74 + random.gauss(0.0, 0.22)
        tremor_y = tremor_y * 0.80 + random.gauss(0.0, 0.36)
        mid = 4.0 * s * (1.0 - s)
        x += tremor_x * mid * 0.55
        y += tremor_y
        if x < x0:
            x = x0
        if x > x0 + peak:
            x = x0 + peak
        if random.random() < 0.06 and 0.2 < s < 0.85:
            dt = random.randint(38, 68)
        elif mid > 0.55:
            dt = random.randint(12, 20)
        else:
            dt = random.randint(16, 30)
        t += dt
        pts.append(_pt(x, y, t))

    if over > 0:
        ax, ay = pts[-1][0], pts[-1][1]
        n_back = random.randint(2, 4)
        for i in range(1, n_back + 1):
            r = i / float(n_back)
            ease = r * r * (3.0 - 2.0 * r)
            t += random.randint(16, 28)
            x = ax + (x1 - ax) * ease
            y = ay + (y1 - ay) * ease + random.gauss(0.0, 0.22)
            pts.append(_pt(x, y, t))

    n_hold = random.randint(2, 3)
    for _ in range(n_hold):
        t += random.randint(14, 24)
        pts.append(_pt(x1, y1 + random.gauss(0.0, 0.20), t))

    pts[0] = _pt(x0, y0, t0)
    pts[-1] = _pt(x1, y1, pts[-1][2])
    return pts
