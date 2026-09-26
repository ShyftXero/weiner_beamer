"""Toolpath model and geometry helpers.

Design coordinates are millimetres in the *dog frame*: the origin is the centre
of the top of the hotdog, +X runs along its length and +Y runs across it.  The
operator zeroes the machine on that point, so a design never needs to know
where on the bed the dog is.

A toolpath is a list of ops.  Each op is one continuous laser-on polyline::

    {"pts": [[x, y], ...], "power": 0..1, "feed": mm/min}

Travel between ops is always a rapid with the laser off.
"""

from __future__ import annotations

import math
from typing import Iterable

Point = tuple[float, float]
Op = dict


def op(pts: Iterable[Point], power: float, feed: float) -> Op:
    return {"pts": [[round(x, 4), round(y, 4)] for x, y in pts], "power": power, "feed": feed}


def bbox(ops: list[Op]) -> tuple[float, float, float, float] | None:
    xs = [p[0] for o in ops for p in o["pts"]]
    ys = [p[1] for o in ops for p in o["pts"]]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def transform_polys(polys: list[list[Point]], scale: float = 1.0, dx: float = 0.0, dy: float = 0.0) -> list[list[Point]]:
    return [[(x * scale + dx, y * scale + dy) for x, y in poly] for poly in polys]


def polys_bbox(polys: list[list[Point]]) -> tuple[float, float, float, float] | None:
    xs = [x for p in polys for x, _ in p]
    ys = [y for p in polys for _, y in p]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def fit_polys(polys: list[list[Point]], max_w: float, max_h: float) -> list[list[Point]]:
    """Scale uniformly to fit inside max_w x max_h and centre on the origin."""
    bb = polys_bbox(polys)
    if bb is None:
        return polys
    x0, y0, x1, y1 = bb
    w, h = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    s = min(max_w / w, max_h / h)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return transform_polys(polys, s, -cx * s, -cy * s)


def _rot(p: Point, c: float, s: float) -> Point:
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


def rotate_polys(polys: list[list[Point]], angle_deg: float) -> list[list[Point]]:
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return [[_rot(p, c, s) for p in poly] for poly in polys]


def hatch(polys: list[list[Point]], spacing: float, angle_deg: float = 0.0) -> list[list[Point]]:
    """Even-odd scanline fill of closed polygons.  Returns 2-point segments in
    boustrophedon order so the head does not fly back across the part."""
    if spacing <= 0 or not polys:
        return []
    a = math.radians(angle_deg)
    c, s = math.cos(-a), math.sin(-a)
    rpolys = [[_rot(p, c, s) for p in poly] for poly in polys if len(poly) >= 3]
    edges = []
    for poly in rpolys:
        for i in range(len(poly)):
            p, q = poly[i], poly[(i + 1) % len(poly)]
            if p[1] != q[1]:
                edges.append((p, q))
    if not edges:
        return []
    ymin = min(min(p[1], q[1]) for p, q in edges)
    ymax = max(max(p[1], q[1]) for p, q in edges)
    out: list[list[Point]] = []
    ci, si = math.cos(a), math.sin(a)
    y = ymin + spacing / 2
    flip = False
    while y < ymax:
        xs = []
        for p, q in edges:
            lo, hi = (p, q) if p[1] < q[1] else (q, p)
            if lo[1] <= y < hi[1]:
                t = (y - lo[1]) / (hi[1] - lo[1])
                xs.append(lo[0] + t * (hi[0] - lo[0]))
        xs.sort()
        segs = [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2) if xs[i + 1] - xs[i] > 1e-6]
        if flip:
            segs = [(b, a_) for a_, b in reversed(segs)]
        for x0, x1 in segs:
            out.append([_rot((x0, y), ci, si), _rot((x1, y), ci, si)])
        flip = not flip
        y += spacing
    return out


def clip_segment_to_rect(p: Point, q: Point, rect: tuple[float, float, float, float]) -> tuple[Point, Point] | None:
    """Liang-Barsky clip of segment pq to rect (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = rect
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pk == 0:
            if qk < 0:
                return None
        else:
            t = qk / pk
            if pk < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    if t0 > t1:
        return None
    return (p[0] + t0 * dx, p[1] + t0 * dy), (p[0] + t1 * dx, p[1] + t1 * dy)


def clip_polyline_to_rect(poly: list[Point], rect) -> list[list[Point]]:
    out: list[list[Point]] = []
    cur: list[Point] = []
    for i in range(len(poly) - 1):
        seg = clip_segment_to_rect(poly[i], poly[i + 1], rect)
        if seg is None:
            if len(cur) > 1:
                out.append(cur)
            cur = []
            continue
        a, b = seg
        if cur and math.dist(cur[-1], a) < 1e-6:
            cur.append(b)
        else:
            if len(cur) > 1:
                out.append(cur)
            cur = [a, b]
        if math.dist(b, poly[i + 1]) > 1e-6:  # left the rect mid-segment
            out.append(cur)
            cur = []
    if len(cur) > 1:
        out.append(cur)
    return out


def stadium(length: float, width: float, cx: float = 0.0, cy: float = 0.0, n: int = 24) -> list[Point]:
    """Closed outline of a hotdog seen from above (rectangle with round ends)."""
    r = width / 2
    half = max(length / 2 - r, 0)
    pts: list[Point] = []
    for i in range(n + 1):
        t = -math.pi / 2 + math.pi * i / n
        pts.append((cx + half + r * math.cos(t), cy + r * math.sin(t)))
    for i in range(n + 1):
        t = math.pi / 2 + math.pi * i / n
        pts.append((cx - half + r * math.cos(t), cy + r * math.sin(t)))
    pts.append(pts[0])
    return pts


def rect_poly(w: float, h: float, cx: float = 0.0, cy: float = 0.0) -> list[Point]:
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2),
            (cx - w / 2, cy + h / 2), (cx - w / 2, cy - h / 2)]


def safe_zone(dog: dict) -> tuple[float, float]:
    """(length along X, band across Y) of the dog's top surface that stays
    within the laser's depth of focus when the head does not follow Z."""
    r = dog["diameter"] / 2
    tol = min(dog.get("focus_tolerance", 1.5), r)
    band = 2 * math.sqrt(max(r * r - (r - tol) ** 2, 0))
    if dog.get("z_follow"):
        # With Z-follow the head tracks the curve; stay a little off the flanks.
        band = dog["diameter"] * 0.7
    length = max(dog["length"] - dog["diameter"] - 2 * dog.get("end_margin", 3), 5)
    return length, band


def path_stats(ops: list[Op]) -> dict:
    burn = travel = secs = 0.0
    last = (0.0, 0.0)
    for o in ops:
        pts = o["pts"]
        if not pts:
            continue
        t = math.dist(last, pts[0])
        travel += t
        secs += t / 3000 * 60
        for i in range(len(pts) - 1):
            d = math.dist(pts[i], pts[i + 1])
            burn += d
            secs += d / max(o["feed"], 1) * 60
        last = tuple(pts[-1])
    return {"burn_mm": round(burn, 1), "travel_mm": round(travel, 1), "est_seconds": round(secs, 1)}
