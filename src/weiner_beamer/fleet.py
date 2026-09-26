"""The fleet: every machine's controller, job routing, and run bookkeeping.

Routing rule: a queued job may run on machine M when
  - M is enabled,
  - job.machine_id is empty ("any machine") or equals M, and
  - job.dog_type is empty or equals M's loaded dog type (its jig fits that dog).
Order is queue position.  A human always starts a run: nothing burns until the
operator at that machine confirms the dogs are placed.

Designs are generated per machine at run time, so an "any machine" job is
fitted to whichever dog size / laser it actually lands on.
"""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import threading
import time
from collections import OrderedDict

from . import designs, geometry
from .controllers import Controller, LinkError, make_controller
from .dialects import dialect_for
from .store import DOG_PRESETS, MACHINE_DEFAULTS, Store, deep_merge

log = logging.getLogger("weiner_beamer.fleet")

RUNNABLE = ("queued", "held", "done", "failed", "canceled", "interrupted")
CONNECTION_KEYS = ("firmware", "port", "baud", "enabled", "marlin")


class FleetError(RuntimeError):
    pass


def _num(v, lo=None, hi=None, default=0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    if lo is not None:
        f = max(lo, f)
    if hi is not None:
        f = min(hi, f)
    return f


def sanitize_machine(cfg: dict) -> dict:
    m = deep_merge(MACHINE_DEFAULTS, cfg)
    if m["firmware"] not in ("grbl", "marlin", "manual"):
        raise FleetError("firmware must be grbl, marlin or manual")
    m["name"] = str(m["name"])[:60] or "Machine"
    m["port"] = str(m["port"]).strip() or "sim"
    m["baud"] = int(_num(m["baud"], 1200, 2_000_000, 115200))
    m["stall_timeout"] = _num(m["stall_timeout"], 5, 600, 30)
    L = m["laser"]
    valid_modes = {"grbl": ("m4", "m3"), "marlin": ("inline", "m3", "fan"), "manual": ("m4", "m3", "inline", "fan")}
    if L["mode"] not in valid_modes[m["firmware"]]:
        L["mode"] = valid_modes[m["firmware"]][0]
    L["s_max"] = _num(L["s_max"], 1, 100000, 1000)
    L["power_scale"] = _num(L["power_scale"], 0, 2, 1)
    L["max_feed"] = _num(L["max_feed"], 10, 50000, 3000)
    L["pointer_power"] = _num(L["pointer_power"], 0, 0.05, 0.01)  # hard cap: a pointer, not a burn
    L["frame_feed"] = _num(L["frame_feed"], 10, 20000, 1500)
    d = m["dog"]
    for k, lo, hi in (("length", 20, 400), ("diameter", 5, 60), ("focus_tolerance", 0.2, 10), ("end_margin", 0, 50)):
        d[k] = _num(d[k], lo, hi, MACHINE_DEFAULTS["dog"][k])
    d["wrap"], d["z_follow"] = bool(d["wrap"]), bool(d["z_follow"])
    d["type"] = str(d.get("type") or "regular")[:30]
    j = m["jig"]
    j["slots"] = int(_num(j["slots"], 1, 12, 1))
    for k in ("pitch_x", "pitch_y"):
        j[k] = _num(j[k], -400, 400, 0)
    j["template_power"] = _num(j["template_power"], 0, 1, 0.35)
    j["template_feed"] = _num(j["template_feed"], 10, 20000, 1500)
    mk = m["marlin"]
    mk["checksums"] = bool(mk["checksums"])
    mk["inflight"] = int(_num(mk["inflight"], 1, 8, 2))
    m["align"]["angle"] = _num(m["align"].get("angle"), -45, 45, 0)
    return m


class Fleet:
    def __init__(self, store: Store):
        self.store = store
        self.ctl: dict[str, Controller] = {}
        self.activity: dict[str, dict] = {}
        self.lock = threading.RLock()
        self._tp_cache: OrderedDict = OrderedDict()

    # ---- machines ---------------------------------------------------------
    def start(self):
        if not self.store.machines():
            self._seed()
        for m in self.store.machines():
            self._spawn(m)

    def _seed(self):
        self.store.save_machine(sanitize_machine({
            "id": "genmitsu-3018", "name": "Genmitsu 3018 (simulated)", "firmware": "grbl", "port": "sim",
            "jig": {"slots": 3, "pitch_y": 36},
            "notes": "Set port to the real device (e.g. /dev/ttyUSB0) when the machine is plugged in."}))
        self.store.save_machine(sanitize_machine({
            "id": "ender-laser", "name": "Ender retrofit (simulated)", "firmware": "marlin", "port": "sim",
            "laser": {"mode": "fan", "s_max": 255, "max_feed": 2400},
            "notes": "Marlin printer with a diode on the fan header. Example config - edit or delete."}))

    def _spawn(self, m: dict):
        c = make_controller(m)
        self.ctl[m["id"]] = c
        if m.get("enabled", True):
            c.start()

    def shutdown(self):
        for c in self.ctl.values():
            try:
                if c.streaming:
                    c.abort()
                c.stop()
            except Exception:  # noqa: BLE001
                pass

    def machine(self, mid: str) -> dict:
        m = self.store.machine(mid)
        if not m:
            raise FleetError(f"no machine {mid!r}")
        return m

    def save_machine(self, cfg: dict) -> dict:
        mid = cfg.get("id") or ""
        if not mid:
            base = re.sub(r"[^a-z0-9]+", "-", str(cfg.get("name", "machine")).lower()).strip("-") or "machine"
            mid = f"{base[:24]}-{secrets.token_hex(2)}"
        old = self.store.machine(mid)
        m = sanitize_machine({**(old or {}), **cfg, "id": mid})
        with self.lock:
            busy = mid in self.activity
            conn_changed = old is None or any(old.get(k) != m.get(k) for k in CONNECTION_KEYS)
            if busy and conn_changed:
                raise FleetError("machine is running; finish or abort before changing its connection")
            self.store.save_machine(m)
            if conn_changed:
                c = self.ctl.pop(mid, None)
                if c:
                    c.stop()
                self._spawn(m)
            else:
                self.ctl[mid].machine = m
        return m

    def delete_machine(self, mid: str):
        with self.lock:
            if mid in self.activity:
                raise FleetError("machine is running")
            c = self.ctl.pop(mid, None)
            if c:
                c.stop()
            self.store.delete_machine(mid)

    def reconnect(self, mid: str):
        with self.lock:
            if mid in self.activity:
                raise FleetError("machine is running")
            c = self.ctl.pop(mid, None)
            if c:
                c.stop()
            self._spawn(self.machine(mid))

    # ---- routing ----------------------------------------------------------
    def eligible(self, job: dict, m: dict) -> bool:
        return (job["status"] == "queued" and m.get("enabled", True)
                and job["machine_id"] in (None, "", m["id"])
                and (not job["dog_type"] or job["dog_type"] == m["dog"]["type"]))

    def next_jobs(self, mid: str, n: int) -> list[dict]:
        m = self.machine(mid)
        out = []
        for j in self.store.jobs(include_finished=False):
            if self.eligible(j, m):
                out.append(j)
                if len(out) >= n:
                    break
        return out

    def dog_for(self, job: dict | None, machine_id: str | None = None) -> dict:
        mid = machine_id or (job or {}).get("machine_id")
        if mid:
            m = self.store.machine(mid)
            if m:
                return m["dog"]
        t = (job or {}).get("dog_type") or "regular"
        return deep_merge(MACHINE_DEFAULTS["dog"], DOG_PRESETS.get(t, DOG_PRESETS["regular"]))

    def toolpath(self, job: dict, machine_id: str | None = None) -> list[dict]:
        dog = self.dog_for(job, machine_id)
        key = (job["id"], job.get("updated"), json.dumps(dog, sort_keys=True))
        with self.lock:
            if key in self._tp_cache:
                self._tp_cache.move_to_end(key)
                return self._tp_cache[key]
        img = self.store.job_image(job["id"]) if job["kind"] == "image" else None
        ops = designs.build(job["kind"], job["params"], dog, img)
        with self.lock:
            self._tp_cache[key] = ops
            while len(self._tp_cache) > 64:
                self._tp_cache.popitem(last=False)
        return ops

    # ---- running jobs -----------------------------------------------------
    def start_run(self, mid: str, job_ids: list[int] | None = None, count: int | None = None,
                  resume: bool = False) -> list[dict]:
        """Start a batch.  job_ids given -> run exactly those (any finished job
        may be re-run: another dog, or a do-over).  Otherwise take the next
        eligible queued jobs, one per jig slot.  Returns [{job_id, slot}]."""
        m = self.machine(mid)
        c = self.ctl[mid]
        with self.lock:
            if mid in self.activity:
                raise FleetError("machine is already running")
            if m["firmware"] != "manual" and not (c.connected and c.ready):
                raise FleetError(f"{m['name']} is not connected ({c.last_error or c.state})")
            if m["firmware"] != "manual" and c.state == "Alarm":
                raise FleetError("machine is in ALARM - unlock or home it first")
            slots = m["jig"]["slots"]
            if job_ids:
                jobs = [self.store.job(int(j)) for j in job_ids]
                if any(j is None for j in jobs):
                    raise FleetError("unknown job id")
                bad = [j["id"] for j in jobs if j["status"] not in RUNNABLE]
                if bad:
                    raise FleetError(f"job(s) {bad} are already running")
                if len(jobs) > slots:
                    raise FleetError(f"{m['name']} only has {slots} slot(s)")
            else:
                jobs = self.next_jobs(mid, min(count or slots, slots))
                if not jobs:
                    raise FleetError("no queued jobs for this machine")
            if resume:
                if len(jobs) != 1:
                    raise FleetError("resume one job at a time")
                if jobs[0]["last_machine"] != mid:
                    raise FleetError("resume only on the machine (and dog) it was interrupted on")
            plan = [{"job_id": j["id"], "slot": i} for i, j in enumerate(jobs)]
            # Claim the jobs now so a second operator can't grab them.
            for p in plan:
                self.store.update_job(p["job_id"], status="running", last_machine=mid, error=None)
            self.activity[mid] = {"kind": "batch", "plan": plan, "index": 0, "started": time.time(),
                                  "manual": m["firmware"] == "manual"}
        if m["firmware"] == "manual":
            self._start_manual(mid, plan)
        else:
            threading.Thread(target=self._run_batch, args=(mid, plan, resume), daemon=True,
                             name=f"run-{mid}").start()
        return plan

    def _start_manual(self, mid: str, plan: list[dict]):
        act = self.activity[mid]
        act["runs"] = {}
        for p in plan:
            j = self.store.job(p["job_id"])
            try:
                ops = self.toolpath(j, mid)
            except designs.DesignError as e:
                self.store.update_job(j["id"], status="failed", error=str(e))
                continue
            rid = self.store.start_run(j["id"], mid, p["slot"], "manual", 0, len(ops) * j["passes"])
            self.store.update_job(j["id"], runs=j["runs"] + 1)
            act["runs"][j["id"]] = rid
        if not act["runs"]:
            self.activity.pop(mid, None)

    def finish_manual(self, jid: int, ok: bool):
        j = self.store.job(jid)
        if not j or j["status"] != "running":
            raise FleetError("job is not running")
        mid = j["last_machine"]
        act = self.activity.get(mid) or {}
        rid = (act.get("runs") or {}).pop(jid, None)
        if rid:
            self.store.end_run(rid, "done" if ok else "failed", None if ok else "marked failed by operator", -1)
        self.store.update_job(jid, status="done" if ok else "failed", error=None if ok else "marked failed by operator")
        if act.get("manual") and not act.get("runs"):
            self.activity.pop(mid, None)

    def _run_batch(self, mid: str, plan: list[dict], resume: bool):
        c = self.ctl[mid]
        try:
            for i, p in enumerate(plan):
                act = self.activity.get(mid)
                if act is None:
                    break
                act["index"] = i
                ok = self._run_one(mid, c, p["job_id"], p["slot"], resume)
                if not ok:
                    # Hand the untouched jobs back to the queue.
                    for rest in plan[i + 1:]:
                        j = self.store.job(rest["job_id"])
                        if j and j["status"] == "running":
                            self.store.update_job(rest["job_id"], status="queued")
                    break
        except Exception as e:  # noqa: BLE001 - a crashed runner must never wedge the machine
            log.exception("batch on %s crashed", mid)
            for p in plan:
                j = self.store.job(p["job_id"])
                if j and j["status"] == "running":
                    self.store.update_job(p["job_id"], status="interrupted", error=f"runner crashed: {e}")
        finally:
            with self.lock:
                self.activity.pop(mid, None)

    def _run_one(self, mid: str, c: Controller, jid: int, slot: int, resume: bool) -> bool:
        m = self.machine(mid)
        j = self.store.job(jid)
        if j is None:
            return True
        try:
            ops = self.toolpath(j, mid)
        except designs.DesignError as e:
            self.store.update_job(jid, status="failed", error=f"design: {e}")
            return True  # a bad design shouldn't stop the other dogs
        n_total = len(ops) * max(1, j["passes"])
        start_op = j["last_op"] + 1 if resume and 0 <= j["last_op"] < n_total - 1 else 0
        lines = dialect_for(m).job(ops, m, designs.slot_offset(m, slot), j["passes"], start_op)
        stats = geometry.path_stats(ops)
        est = stats["est_seconds"] * max(1, j["passes"]) * (1 - start_op / max(n_total, 1))
        rid = self.store.start_run(jid, mid, slot, "resume" if start_op else "run", start_op, n_total)
        self.store.update_job(jid, runs=j["runs"] + 1, last_op=start_op - 1)
        act = self.activity[mid]
        act.update({"job_id": jid, "slot": slot, "run_id": rid, "start_op": start_op, "total_ops": n_total,
                    "op_done": start_op - 1, "job_started": time.time(), "est_seconds": est, "title": j["title"]})
        last_flush = [0.0]

        def on_line(idx, line):
            if line.safe and line.op >= 0:
                act["op_done"] = line.op
                now = time.monotonic()
                if now - last_flush[0] > 1.0:  # durable progress for crash-resume
                    last_flush[0] = now
                    self.store.progress_run(rid, line.op)
                    self.store.update_job(jid, last_op=line.op)

        res = c.stream(lines, on_line)
        last_op = act["op_done"]
        if res.ok:
            self.store.end_run(rid, "done", None, n_total - 1)
            self.store.update_job(jid, status="done", last_op=n_total - 1, error=None)
            return True
        if res.canceled:
            self.store.end_run(rid, "aborted", "aborted by operator", last_op)
            self.store.update_job(jid, status="interrupted", last_op=last_op, error="aborted by operator")
        else:
            self.store.end_run(rid, "failed", res.error, last_op)
            self.store.update_job(jid, status="interrupted", last_op=last_op, error=res.error)
        return False

    # ---- utility streams (template burn, framing) --------------------------
    def _utility(self, mid: str, label: str, lines):
        c = self.ctl[mid]
        with self.lock:
            if mid in self.activity:
                raise FleetError("machine is busy")
            if not (c.connected and c.ready):
                raise FleetError("machine not connected")
            self.activity[mid] = {"kind": "utility", "label": label, "started": time.time()}

        def go():
            try:
                res = c.stream(lines)
                if not res.ok and not res.canceled:
                    c._set_error(f"{label}: {res.error}")
            finally:
                with self.lock:
                    self.activity.pop(mid, None)

        threading.Thread(target=go, daemon=True, name=f"util-{mid}").start()

    def frame(self, mid: str, job_id: int | None, slot: int = 0, pointer: bool = False):
        m = self.machine(mid)
        if job_id:
            j = self.store.job(int(job_id))
            if not j:
                raise FleetError("unknown job")
            ops = self.toolpath(j, mid)
            label = f"frame job #{job_id}"
        else:
            zw, zh = geometry.safe_zone(m["dog"])
            ops = [geometry.op(geometry.rect_poly(zw, zh), 0, 1000)]
            label = "frame safe zone"
        self._utility(mid, label, dialect_for(m).frame(ops, m, designs.slot_offset(m, slot), pointer))

    def burn_template(self, mid: str):
        m = self.machine(mid)
        ops = designs.build_template(m)
        mm = {**m, "align": {**m["align"], "angle": 0.0}, "dog": {**m["dog"], "wrap": False, "z_follow": False}}
        self._utility(mid, "burn placement template", dialect_for(m).job(ops, mm))

    # ---- alignment & manual control ----------------------------------------
    def command(self, mid: str, lines: list[str], timeout: float = 30) -> list[str]:
        m = self.machine(mid)
        if m["firmware"] == "manual":
            raise FleetError("manual machines have no live link")
        if mid in self.activity:
            raise FleetError("machine is busy")
        try:
            return self.ctl[mid].command(lines, timeout)
        except LinkError as e:
            raise FleetError(str(e)) from e

    def jog(self, mid, dx, dy, dz, feed):
        m = self.machine(mid)
        return self.command(mid, dialect_for(m).jog(_num(dx, -400, 400), _num(dy, -400, 400),
                                                    _num(dz, -50, 50), _num(feed, 10, 10000, 1000)))

    def mark_end(self, mid: str, which: str):
        if which not in ("a", "b"):
            raise FleetError("end must be a or b")
        m = self.machine(mid)
        pos = self.ctl[mid].wpos[:2]
        m["align"][f"end_{which}"] = [round(v, 3) for v in pos]
        self.store.save_machine(m)
        self.ctl[mid].machine = m
        return m["align"]

    def center_on_ends(self, mid: str) -> dict:
        """Operator jogged the pointer to each end of a loose dog.  Move to the
        midpoint, zero X/Y there, and remember the dog's angle so every design
        is rotated to lie along it."""
        m = self.machine(mid)
        a, b = m["align"].get("end_a"), m["align"].get("end_b")
        if not a or not b:
            raise FleetError("mark both ends first")
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 10:
            raise FleetError("ends are less than 10mm apart - mark them again")
        ang = math.degrees(math.atan2(dy, dx))
        if ang > 90:
            ang -= 180
        elif ang < -90:
            ang += 180
        if abs(ang) > 45:
            raise FleetError(f"dog is at {ang:.0f} degrees - lay it roughly along X")
        cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        d = dialect_for(m)
        self.command(mid, d.goto_work(cx, cy) + d.set_zero("XY"))
        m["align"].update({"angle": round(ang, 3), "end_a": None, "end_b": None, "measured_length": round(length, 1)})
        self.store.save_machine(m)
        self.ctl[mid].machine = m
        return m["align"]

    def set_angle(self, mid: str, angle: float):
        m = self.machine(mid)
        m["align"]["angle"] = _num(angle, -45, 45, 0)
        self.store.save_machine(m)
        self.ctl[mid].machine = m

    # ---- views ------------------------------------------------------------------
    def machine_view(self, m: dict) -> dict:
        c = self.ctl.get(m["id"])
        snap = c.snapshot() if c else {}
        act = dict(self.activity.get(m["id"]) or {})
        if act.get("kind") == "batch" and "job_id" in act and c:
            done, total = c.progress
            frac = done / total if total else 0
            el = time.time() - act["job_started"]
            eta = (el / frac - el) if frac > 0.05 else act["est_seconds"] * (1 - frac)
            act.update({"fraction": round(frac, 4), "elapsed": round(el, 1), "eta": round(max(eta, 0), 1)})
        act.pop("runs", None)
        queued = [j["id"] for j in self.store.jobs(include_finished=False) if self.eligible(j, m)]
        return {"config": m, "status": snap, "activity": act or None, "next_jobs": queued[: m["jig"]["slots"]],
                "queued_count": len(queued), "safe_zone": geometry.safe_zone(m["dog"]),
                "log": list(c.log)[-60:] if c else []}

    def estop_all(self):
        for mid, c in list(self.ctl.items()):
            try:
                c.estop()
            except Exception:  # noqa: BLE001
                log.exception("estop %s", mid)
