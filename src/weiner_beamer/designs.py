"""Turn job parameters into toolpaths (see geometry.py for the op format)."""

from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from PIL import Image, ImageOps

from . import geometry as g

FONT_DIRS = [Path("/usr/share/fonts/truetype"), Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
             Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
PREFERRED_FONTS = {
    "Sans Bold": "DejaVuSans-Bold.ttf",
    "Sans": "DejaVuSans.ttf",
    "Serif Bold": "DejaVuSerif-Bold.ttf",
    "Condensed Bold": "DejaVuSansCondensed-Bold.ttf",
    "Mono Bold": "DejaVuSansMono-Bold.ttf",
    "Narrow Bold": "LiberationSansNarrow-Bold.ttf",
    "Italic Bold": "DejaVuSans-BoldOblique.ttf",
}
EXTRA_FONT_DIRS: list[Path] = []  # data/fonts is appended at startup

PATTERNS = ["crosshatch", "diamond", "zigzag", "wave", "stripes", "grill", "border", "checker"]
MAX_IMAGE_PIXELS = 4_000_000


class DesignError(ValueError):
    pass


def _find_font_file(name: str) -> Path | None:
    for d in EXTRA_FONT_DIRS + FONT_DIRS:
        if d.is_dir():
            hit = next(iter(d.rglob(name)), None)
            if hit:
                return hit
    return None


@lru_cache(maxsize=1)
def available_fonts() -> dict[str, str]:
    out = {}
    for label, fname in PREFERRED_FONTS.items():
        p = _find_font_file(fname)
        if p:
            out[label] = str(p)
    for d in EXTRA_FONT_DIRS:
        for p in sorted(d.glob("*.[ot]tf")):
            out[p.stem] = str(p)
    return out


@lru_cache(maxsize=16)
def _load_font(path: str) -> TTFont:
    return TTFont(path, lazy=True)


class _FlattenPen(BasePen):
    """Collects glyph outlines as closed polylines, flattening curves."""

    def __init__(self, glyphset, steps: int = 8):
        super().__init__(glyphset)
        self.polys: list[list[g.Point]] = []
        self.cur: list[g.Point] = []
        self.steps = steps

    def _moveTo(self, p):
        self._flush()
        self.cur = [p]

    def _lineTo(self, p):
        self.cur.append(p)

    def _curveToOne(self, p1, p2, p3):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            mt = 1 - t
            self.cur.append((
                mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0],
                mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1],
            ))

    def _qCurveToOne(self, p1, p2):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            mt = 1 - t
            self.cur.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                             mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))

    def _closePath(self):
        if self.cur:
            self.cur.append(self.cur[0])
        self._flush()

    _endPath = _closePath

    def _flush(self):
        if len(self.cur) > 2:
            self.polys.append(self.cur)
        self.cur = []


def text_polys(text: str, font_label: str, line_spacing: float = 1.15, letter_spacing: float = 0.0) -> list[list[g.Point]]:
    """Outline polygons for (possibly multi-line) text, in font units, y up."""
    fonts = available_fonts()
    path = fonts.get(font_label) or next(iter(fonts.values()), None)
    if not path:
        raise DesignError("no usable TTF fonts found; drop one into data/fonts/")
    font = _load_font(path)
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    hmtx = font["hmtx"]
    lines = text.split("\n")
    polys: list[list[g.Point]] = []
    line_polys: list[tuple[list[list[g.Point]], float]] = []
    for line in lines:
        x = 0.0
        lp: list[list[g.Point]] = []
        for ch in line:
            gname = cmap.get(ord(ch)) or ".notdef"
            if gname not in gs:
                gname = ".notdef"
            pen = _FlattenPen(gs)
            gs[gname].draw(pen)
            lp.extend([(px + x, py) for px, py in poly] for poly in pen.polys)
            x += hmtx[gname][0] + letter_spacing * upm
        line_polys.append((lp, x))
    # centre each line horizontally, stack downward
    for i, (lp, width) in enumerate(line_polys):
        dy = -i * upm * line_spacing
        polys.extend([(px - width / 2, py + dy) for px, py in poly] for poly in lp)
    return polys


def _power(p: dict, key: str = "power", default: float = 0.6) -> float:
    return max(0.0, min(1.0, float(p.get(key, default))))


def build_text(p: dict, dog: dict) -> list[g.Op]:
    text = str(p.get("text", "")).strip("\n")
    if not text.strip():
        raise DesignError("text is empty")
    if len(text) > 200:
        raise DesignError("text too long (200 chars max)")
    polys = text_polys(text, p.get("font", "Sans Bold"), float(p.get("line_spacing", 1.15)), float(p.get("letter_spacing", 0)))
    if not polys:
        raise DesignError("text produced no outlines (unsupported characters?)")
    zw, zh = g.safe_zone(dog)
    border = bool(p.get("border"))
    margin = 1.6 if border else 0
    size = float(p.get("size_mm") or 0)
    if size > 0:
        bb = g.polys_bbox(polys)
        lines = text.count("\n") + 1
        font_h = (bb[3] - bb[1]) / lines
        s = size / max(font_h, 1e-9)
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        polys = g.transform_polys(polys, s, -cx * s, -cy * s)
    else:
        polys = g.fit_polys(polys, zw - 2 * margin, zh - 2 * margin)
    if p.get("rotate_180"):
        polys = g.rotate_polys(polys, 180)
    mode = p.get("mode", "outline")
    ops: list[g.Op] = []
    power, feed = _power(p), float(p.get("feed", 800))
    if mode in ("fill", "both"):
        fpow, ffeed = _power(p, "fill_power", power), float(p.get("fill_feed", feed * 1.5))
        for seg in g.hatch(polys, float(p.get("hatch_spacing", 0.25)), float(p.get("hatch_angle", 0))):
            ops.append(g.op(seg, fpow, ffeed))
    if mode in ("outline", "both"):
        ops.extend(g.op(poly, power, feed) for poly in polys)
    if border:
        bb = g.polys_bbox(polys)
        w, h = bb[2] - bb[0] + 2 * margin, bb[3] - bb[1] + 2 * margin
        ops.append(g.op(g.stadium(w, h, (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2), power, feed))
    return ops


def build_pattern(p: dict, dog: dict) -> list[g.Op]:
    kind = p.get("pattern", "crosshatch")
    if kind not in PATTERNS:
        raise DesignError(f"unknown pattern {kind!r}")
    zw, zh = g.safe_zone(dog)
    w = float(p.get("width_mm") or zw)
    h = float(p.get("height_mm") or zh)
    spacing = max(float(p.get("spacing", 2.0)), 0.1)
    angle = float(p.get("angle", 45))
    power, feed = _power(p), float(p.get("feed", 900))
    rect = (-w / 2, -h / 2, w / 2, h / 2)
    area = [g.rect_poly(w, h)]
    polys: list[list[g.Point]] = []
    if kind == "crosshatch":
        polys = g.hatch(area, spacing, angle) + g.hatch(area, spacing, angle + 90)
    elif kind == "diamond":
        polys = g.hatch(area, spacing, 60) + g.hatch(area, spacing, 120)
    elif kind == "stripes":
        polys = g.hatch(area, spacing, angle)
    elif kind in ("zigzag", "wave"):
        period = max(float(p.get("period", 6.0)), 0.5)
        rows = max(int(p.get("rows", 1)), 1)
        amp = h / rows / 2 * 0.9
        for r in range(rows):
            cy = -h / 2 + h / rows * (r + 0.5)
            pts: list[g.Point] = []
            if kind == "zigzag":
                n = int(w / (period / 2)) + 1
                for i in range(n + 1):
                    x = -w / 2 + i * period / 2
                    pts.append((min(x, w / 2), cy + (amp if i % 2 else -amp)))
            else:
                n = int(w / 0.5) + 1
                for i in range(n + 1):
                    x = -w / 2 + w * i / n
                    pts.append((x, cy + amp * math.sin(2 * math.pi * x / period)))
            polys.append(pts if r % 2 == 0 else pts[::-1])
    elif kind == "grill":
        # a handful of fat diagonal bars, each drawn as several tight strokes
        bars = max(int(p.get("bars", 5)), 1)
        bar_w = float(p.get("bar_width", 1.6))
        pitch = w / bars
        tan = math.tan(math.radians(angle))
        for b in range(bars):
            cx = -w / 2 + pitch * (b + 0.5)
            k = 0
            off = -bar_w / 2
            while off <= bar_w / 2 + 1e-9:
                x = cx + off
                a_, b_ = (x - h / 2 / tan, -h), (x + h / 2 / tan, h)
                seg = g.clip_segment_to_rect(a_, b_, rect)
                if seg:
                    polys.append(list(seg) if k % 2 == 0 else list(seg)[::-1])
                off += 0.3
                k += 1
    elif kind == "border":
        polys = [g.stadium(w, h)]
        if p.get("double", True):
            polys.append(g.stadium(max(w - 2 * spacing, 1), max(h - 2 * spacing, 1)))
    elif kind == "checker":
        cell = spacing * 2
        nx, ny = max(int(w / cell), 1), max(int(h / cell), 1)
        cw, ch = w / nx, h / ny
        for i in range(nx):
            for j in range(ny):
                if (i + j) % 2 == 0:
                    sq = [g.rect_poly(cw, ch, -w / 2 + cw * (i + 0.5), -h / 2 + ch * (j + 0.5))]
                    polys.extend(g.hatch(sq, float(p.get("hatch_spacing", 0.3)), 0))
    clipped: list[list[g.Point]] = []
    for poly in polys:
        clipped.extend(g.clip_polyline_to_rect(poly, (rect[0] - 1e-6, rect[1] - 1e-6, rect[2] + 1e-6, rect[3] + 1e-6)))
    return [g.op(poly, power, feed) for poly in clipped]


def build_image(p: dict, image_bytes: bytes, dog: dict) -> list[g.Op]:
    """Binary raster engrave.  Photos are dithered, logos thresholded."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as e:  # noqa: BLE001 - Pillow raises many types
        raise DesignError(f"could not read image: {e}") from e
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white")
        bg.alpha_composite(img)
        img = bg
    img = ImageOps.exif_transpose(img).convert("L")
    if p.get("invert"):
        img = ImageOps.invert(img)
    zw, zh = g.safe_zone(dog)
    aspect = img.height / img.width
    w = float(p.get("width_mm") or 0) or min(zw, zh / aspect)
    h = w * aspect
    if h > zh * 1.5 and not p.get("width_mm"):
        h = zh
        w = h / aspect
    interval = max(float(p.get("line_interval", 0.2)), 0.05)
    cols = max(int(round(w / interval)), 1)
    rows = max(int(round(h / interval)), 1)
    img = img.resize((cols, rows), Image.Resampling.LANCZOS)
    if p.get("dither"):
        bw = img.convert("1")  # Floyd-Steinberg
        dark = lambda v: v == 0  # noqa: E731
    else:
        thr = int(p.get("threshold", 128))
        bw = img
        dark = lambda v: v < thr  # noqa: E731
    px = bw.load()
    power, feed = _power(p), float(p.get("feed", 1500))
    ops: list[g.Op] = []
    for r in range(rows):
        y = h / 2 - (r + 0.5) * interval
        runs = []
        c = 0
        while c < cols:
            if dark(px[c, r]):
                start = c
                while c < cols and dark(px[c, r]):
                    c += 1
                runs.append((-w / 2 + start * interval, -w / 2 + c * interval))
            else:
                c += 1
        if r % 2:
            runs = [(b, a) for a, b in reversed(runs)]
        ops.extend(g.op([(a, y), (b, y)], power, feed) for a, b in runs)
    if p.get("rotate_180"):
        for o in ops:
            o["pts"] = [[-x, -y] for x, y in o["pts"]]
    return ops


def build_testgrid(p: dict, dog: dict) -> list[g.Op]:
    """Power x speed matrix of small filled squares for tuning on a spare dog.
    Columns step power left->right, rows step feed bottom->top."""
    powers = [max(0.0, min(1.0, float(v))) for v in p.get("powers", [0.3, 0.45, 0.6, 0.75, 0.9])]
    feeds = [float(v) for v in p.get("feeds", [600, 1200])]
    cell = float(p.get("cell_mm", 4))
    gap = float(p.get("gap_mm", 1.5))
    if not powers or not feeds:
        raise DesignError("need at least one power and one feed")
    nx, ny = len(powers), len(feeds)
    tw, th = nx * cell + (nx - 1) * gap, ny * cell + (ny - 1) * gap
    ops: list[g.Op] = []
    for j, feed in enumerate(feeds):
        for i, pw in enumerate(powers):
            cx = -tw / 2 + i * (cell + gap) + cell / 2
            cy = -th / 2 + j * (cell + gap) + cell / 2
            for seg in g.hatch([g.rect_poly(cell, cell, cx, cy)], float(p.get("hatch_spacing", 0.25)), 0):
                ops.append(g.op(seg, pw, feed))
    return ops


def build_template(machine: dict) -> list[g.Op]:
    """Outline of every jig slot, burned onto cardboard/wood taped to the bed.
    Coordinates are relative to slot 0's centre (the work zero)."""
    dog = machine["dog"]
    jig = machine["jig"]
    power, feed = _power(jig, "template_power", 0.35), float(jig.get("template_feed", 1500))
    ops: list[g.Op] = []
    for i in range(int(jig.get("slots", 1))):
        cx, cy = slot_offset(machine, i)
        ops.append(g.op(g.stadium(dog["length"], dog["diameter"], cx, cy), power, feed))
        half = dog["length"] / 2 + 4
        ops.append(g.op([(cx - half, cy), (cx - dog["length"] / 2 + 2, cy)], power, feed))
        ops.append(g.op([(cx + dog["length"] / 2 - 2, cy), (cx + half, cy)], power, feed))
        ops.append(g.op([(cx, cy - dog["diameter"] / 2 - 3), (cx, cy - dog["diameter"] / 2 + 1.5)], power, feed))
        ops.append(g.op([(cx, cy + dog["diameter"] / 2 - 1.5), (cx, cy + dog["diameter"] / 2 + 3)], power, feed))
        # slot number as tally ticks beside the left end so dogs are loaded in order
        for k in range(i + 1):
            x = cx - half - 3 - k * 1.5
            ops.append(g.op([(x, cy - 2), (x, cy + 2)], power, feed))
    return ops


def slot_offset(machine: dict, slot: int) -> tuple[float, float]:
    jig = machine["jig"]
    return (float(jig.get("pitch_x", 0)) * slot, float(jig.get("pitch_y", 32)) * slot)


def build(kind: str, params: dict, dog: dict, image_bytes: bytes | None = None) -> list[g.Op]:
    if kind == "text":
        return build_text(params, dog)
    if kind == "pattern":
        return build_pattern(params, dog)
    if kind == "image":
        if not image_bytes:
            raise DesignError("image job without an image")
        return build_image(params, image_bytes, dog)
    if kind == "testgrid":
        return build_testgrid(params, dog)
    raise DesignError(f"unknown job kind {kind!r}")
