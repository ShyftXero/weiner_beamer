"""Firmware dialects: how to say things in G-code for a given controller.

Toolpaths (geometry.py) are firmware-neutral.  A dialect turns them into lines
for one machine, applying the machine's alignment (rotation, jig slot offset),
cylinder compensation and laser power scaling on the way.

Every emitted line is a ``Line``: the text plus a flag saying whether the laser
is guaranteed off after it (a safe place for a host-side pause).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import geometry as g


@dataclass
class Line:
    text: str
    safe: bool = False  # laser is off after this line; host may pause here
    op: int = -1  # global stroke index (pass * n_ops + i); -1 for header/footer


@dataclass
class Emitter:
    machine: dict
    lines: list[Line] = field(default_factory=list)

    # -- helpers ----------------------------------------------------------
    @property
    def laser(self) -> dict:
        return self.machine["laser"]

    def s(self, power: float) -> int:
        s_max = float(self.laser.get("s_max", 1000))
        scale = float(self.laser.get("power_scale", 1.0))
        return int(round(max(0.0, min(1.0, power * scale)) * s_max))

    def f(self, feed: float) -> int:
        return int(round(min(feed, float(self.laser.get("max_feed", 6000)))))

    cur_op: int = -1

    def add(self, text: str, safe: bool = False):
        self.lines.append(Line(text, safe, self.cur_op))


def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def xyz(x: float, y: float, z: float | None) -> str:
    out = f"X{_fmt(x)} Y{_fmt(y)}"
    if z is not None:
        out += f" Z{_fmt(z)}"
    return out


def _pt(pts: list, i: int) -> tuple:
    p = pts[i]
    return (p[0], p[1], p[2] if len(p) > 2 else None)


def place(ops: list[g.Op], machine: dict, slot_xy: tuple[float, float] = (0.0, 0.0)) -> list[dict]:
    """Map dog-frame ops to work coordinates: cylinder wrap / Z-follow, then
    the operator's measured rotation, then the jig slot offset.

    Returns ops whose pts are [x, y, z|None]."""
    dog = machine["dog"]
    r = dog["diameter"] / 2
    wrap = bool(dog.get("wrap"))
    zf = bool(dog.get("z_follow"))
    ang = math.radians(float(machine.get("align", {}).get("angle", 0.0)))
    ca, sa = math.cos(ang), math.sin(ang)
    ox, oy = slot_xy
    out = []
    for o in ops:
        pts = o["pts"]
        if (wrap or zf) and len(pts) > 1:
            dense = [pts[0]]
            for i in range(len(pts) - 1):
                (x0, y0), (x1, y1) = pts[i], pts[i + 1]
                n = max(1, int(abs(y1 - y0) / 0.5))
                dense.extend([x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n] for k in range(1, n + 1))
            pts = dense
        placed = []
        for x, y in pts:
            z = None
            if wrap:
                t = max(-math.pi / 2 * 0.95, min(math.pi / 2 * 0.95, y / r))  # arc length -> angle
                y, zc = r * math.sin(t), r * math.cos(t) - r
                z = zc if zf else None
            elif zf:
                yy = min(abs(y), r * 0.95)
                z = -(r - math.sqrt(r * r - yy * yy))
            placed.append([x * ca - y * sa + ox, x * sa + y * ca + oy, z])
        out.append({"pts": placed, "power": o["power"], "feed": o["feed"]})
    return out


class Dialect:
    name = "base"
    rapid_feed = 3000

    # ---- whole jobs -------------------------------------------------------
    def job(self, ops: list[g.Op], machine: dict, slot_xy=(0.0, 0.0), passes: int = 1,
            start_op: int = 0) -> list[Line]:
        """Program for a whole job.  ``start_op`` skips strokes already burned
        (resume after an interruption); stroke k's last line is safe and
        carries op=k, so the host knows stroke k finished when it is acked."""
        e = Emitter(machine)
        placed = place(ops, machine, slot_xy)
        uses_z = any(p[2] is not None for o in placed for p in o["pts"])
        self.header(e)
        n = len(placed)
        for ps in range(max(1, int(passes))):
            for i, o in enumerate(placed):
                k = ps * n + i
                pts = o["pts"]
                if k < start_op or len(pts) < 2:
                    continue
                e.cur_op = k
                x, y, z = _pt(pts, 0)
                e.add(f"G0 {xyz(x, y, z)}")
                self.burn(e, pts, o["power"], o["feed"])
        e.cur_op = -1
        if uses_z:
            e.add("G0 Z0", safe=True)
        self.footer(e)
        return e.lines

    def frame(self, ops: list[g.Op], machine: dict, slot_xy=(0.0, 0.0), pointer: bool = False) -> list[Line]:
        """Trace the job's bounding box so the operator can see where it lands."""
        placed = place(ops, machine, slot_xy)
        xs = [p[0] for o in placed for p in o["pts"]]
        ys = [p[1] for o in placed for p in o["pts"]]
        if not xs:
            return []
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        e = Emitter(machine)
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        e.add("G90")
        e.add(f"G0 X{_fmt(x0)} Y{_fmt(y0)}")
        feed = float(machine["laser"].get("frame_feed", 1500))
        power = float(machine["laser"].get("pointer_power", 0.01)) if pointer else 0.0
        self.burn(e, corners, power, feed)
        e.add("G0 X0 Y0", safe=True)
        return e.lines

    # ---- dialect-specific pieces -------------------------------------------
    def header(self, e: Emitter): ...
    def footer(self, e: Emitter): ...
    def burn(self, e: Emitter, pts, power: float, feed: float): ...
    def jog(self, dx: float, dy: float, dz: float, feed: float) -> list[str]: ...
    def goto_work(self, x: float, y: float, feed: float = 3000) -> list[str]: ...
    def set_zero(self, axes: str) -> list[str]: ...
    def home(self) -> list[str]: ...
    def unlock(self) -> list[str]: return []
    def pointer(self, on: bool, machine: dict) -> list[str]: ...
    def laser_off(self) -> list[str]: ...


class GrblDialect(Dialect):
    """Grbl 1.1 with laser mode ($32=1).  M4 = dynamic power: the controller
    scales power with actual speed and turns the laser off on G0 and when
    motion stops, so rapids and corners don't over-burn."""

    name = "grbl"

    def header(self, e):
        e.add("G21 G90 G94")
        e.add("M5", safe=True)
        e.add(f"{'M4' if e.laser.get('mode', 'm4') == 'm4' else 'M3'} S0", safe=True)

    def footer(self, e):
        e.add("M5", safe=True)

    def burn(self, e, pts, power, feed):
        s = e.s(power)
        x, y, z = _pt(pts, 1)
        e.add(f"G1 {xyz(x, y, z)} F{e.f(feed)} S{s}")
        for p in pts[2:]:
            x, y, z = p[0], p[1], p[2] if len(p) > 2 else None
            e.add(f"G1 {xyz(x, y, z)}")
        # In laser mode the next G0 turns the beam off, so the line *before*
        # a G0 is not yet "safe"; mark safe on an explicit S0.
        e.add("S0", safe=True)

    def jog(self, dx, dy, dz, feed):
        parts = " ".join(f"{a}{_fmt(v)}" for a, v in (("X", dx), ("Y", dy), ("Z", dz)) if v)
        return [f"$J=G91 G21 {parts} F{int(feed)}"] if parts else []

    def goto_work(self, x, y, feed=3000):
        return ["G90", f"G0 X{_fmt(x)} Y{_fmt(y)}"]

    def set_zero(self, axes):
        return ["G10 L20 P1 " + " ".join(f"{a}0" for a in axes.upper())]

    def home(self):
        return ["$H"]

    def unlock(self):
        return ["$X"]

    def pointer(self, on, machine):
        if not on:
            return ["M5"]
        s = int(round(float(machine["laser"].get("pointer_power", 0.01)) * float(machine["laser"].get("s_max", 1000))))
        # M3 + a G1 modal state makes Grbl's laser mode fire while stationary.
        return ["G1 F100", f"M3 S{s}"]

    def laser_off(self):
        return ["M5"]


class MarlinDialect(Dialect):
    """Marlin on a 3D printer / retrofit.

    laser.mode:
      inline - LASER_FEATURE + LASER_POWER_INLINE: ``M3 I`` then S on G1, G0 = off
      m3     - LASER_FEATURE without inline: M3/M5 around each stroke, M400-synced
      fan    - diode driven from the part-cooling fan pin: M106 S / M107, M400-synced
    """

    name = "marlin"

    def mode(self, e):
        return e.laser.get("mode", "fan")

    def header(self, e):
        e.add("G21")
        e.add("G90")
        e.add("M83")  # never let a stray E word move an extruder in absolute mode
        e.add(self.off_cmd(e), safe=True)
        if self.mode(e) == "inline":
            e.add("M3 I S0", safe=True)

    def footer(self, e):
        e.add("M400")
        e.add(self.off_cmd(e), safe=True)
        if self.mode(e) == "inline":
            e.add("M5", safe=True)

    def off_cmd(self, e):
        return "M107" if self.mode(e) == "fan" else "M5"

    def burn(self, e, pts, power, feed):
        s = e.s(power)
        m = self.mode(e)
        x, y, z = _pt(pts, 1)
        if m == "inline":
            e.add(f"G1 {xyz(x, y, z)} F{e.f(feed)} S{s}")
            for p in pts[2:]:
                x, y, z = p[0], p[1], p[2] if len(p) > 2 else None
                e.add(f"G1 {xyz(x, y, z)} S{s}")
            e.add("G1 S0", safe=True)
            return
        on = f"M106 S{s}" if m == "fan" else f"M3 S{s}"
        e.add("M400")
        e.add(on)
        e.add(f"G1 {xyz(x, y, z)} F{e.f(feed)}")
        for p in pts[2:]:
            x, y, z = p[0], p[1], p[2] if len(p) > 2 else None
            e.add(f"G1 {xyz(x, y, z)}")
        e.add("M400")
        e.add(self.off_cmd(e), safe=True)

    def jog(self, dx, dy, dz, feed):
        parts = " ".join(f"{a}{_fmt(v)}" for a, v in (("X", dx), ("Y", dy), ("Z", dz)) if v)
        return ["G91", f"G0 {parts} F{int(feed)}", "G90"] if parts else []

    def goto_work(self, x, y, feed=3000):
        return ["G90", f"G0 X{_fmt(x)} Y{_fmt(y)} F{int(feed)}"]

    def set_zero(self, axes):
        return ["G92 " + " ".join(f"{a}0" for a in axes.upper())]

    def home(self):
        return ["G28 X Y"]  # never auto-home Z on a laser retrofit: no probe, risk of a crash

    def unlock(self):
        return ["M999"]

    def pointer(self, on, machine):
        mode = machine["laser"].get("mode", "fan")
        if not on:
            return ["M107" if mode == "fan" else "M5"]
        s = int(round(float(machine["laser"].get("pointer_power", 0.01)) * float(machine["laser"].get("s_max", 255))))
        s = max(s, 1)
        return [f"M106 S{s}" if mode == "fan" else f"M3 S{s}"]

    def laser_off(self):
        return ["M107", "M5"]


DIALECTS: dict[str, Dialect] = {"grbl": GrblDialect(), "marlin": MarlinDialect(), "manual": GrblDialect()}


def dialect_for(machine: dict) -> Dialect:
    fw = machine.get("firmware", "grbl")
    if fw == "manual":
        return DIALECTS[machine.get("manual_dialect", "grbl")]
    return DIALECTS[fw]
