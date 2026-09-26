"""Simulated Grbl and Marlin controllers, so the whole stack runs with no
hardware attached.  Port syntax: ``sim`` or ``sim:<speedup>[:<corrupt_rate>]``.

The sims model what matters for the host: a small planner buffer (acks arrive
before motion finishes), realtime bytes for Grbl, N/checksum + Resend for
Marlin, and a record of every laser-on segment (``burns``) for tests.
"""

from __future__ import annotations

import math
import queue
import random
import re
import threading
import time

WORD = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")


class SimTransport:
    PLANNER = 15

    def __init__(self, firmware: str, speedup: float = 20.0, corrupt: float = 0.0):
        self.fw = firmware
        self.speedup = max(speedup, 0.1)
        self.corrupt = corrupt
        self.out: queue.Queue[bytes] = queue.Queue()
        self.inbuf = b""
        self.lines: queue.Queue[str] = queue.Queue()
        self.lock = threading.RLock()
        self.planner: list[dict] = []
        self.mpos = [0.0, 0.0, 0.0]
        self.wco = [0.0, 0.0, 0.0]  # work = machine - wco
        self.absolute = True
        self.feed = 1000.0
        self.s = 0.0
        self.laser_on = False  # M3/M4 (grbl) or inline mode (marlin)
        self.fan = 0.0
        self.hold = False
        self.alarm = False
        self.halted = False
        self.state = "Idle"
        self.expected_n = 1
        self.burns: list[tuple] = []
        self.closed = False
        self.gen = 0
        self._start_threads()
        self._boot()

    # ---- transport API -----------------------------------------------------
    def write(self, data: bytes):
        if self.closed:
            raise OSError("closed")
        for b in data:
            ch = bytes([b])
            if self.fw == "grbl" and ch in (b"?", b"!", b"~", b"\x18", b"\x85"):
                self._realtime(ch)
                continue
            self.inbuf += ch
            if ch == b"\n":
                line = self.inbuf.decode("ascii", "replace").strip()
                self.inbuf = b""
                if line:
                    self.lines.put(line)
        return len(data)

    def readline(self) -> bytes:
        try:
            return self.out.get(timeout=0.1)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True

    # ---- internals ------------------------------------------------------------
    def _say(self, s: str):
        self.out.put((s + "\r\n").encode())

    def _boot(self):
        def later():
            time.sleep(0.3)
            self._say("Grbl 1.1h ['$' for help]" if self.fw == "grbl" else "start")
        threading.Thread(target=later, daemon=True).start()

    def _start_threads(self):
        threading.Thread(target=self._proc, daemon=True).start()
        threading.Thread(target=self._exec, daemon=True).start()

    def _realtime(self, ch: bytes):
        if ch == b"?":
            wp = self.mpos
            st = "Alarm" if self.alarm else ("Hold:0" if self.hold else self.state)
            self._say(f"<{st}|MPos:{wp[0]:.3f},{wp[1]:.3f},{wp[2]:.3f}|FS:{int(self.feed)},{int(self.s)}"
                      f"|WCO:{self.wco[0]:.3f},{self.wco[1]:.3f},{self.wco[2]:.3f}>")
        elif ch == b"!":
            self.hold = True
        elif ch == b"~":
            self.hold = False
        elif ch == b"\x18":
            with self.lock:
                moving = self.planner and not self.hold
                self.planner.clear()
                self.gen += 1
                self.laser_on = False
                self.s = 0
                self.hold = False
                self.inbuf = b""
                while not self.lines.empty():
                    self.lines.get_nowait()
                if moving:
                    self.alarm = True
            self._boot()
            if moving:
                self._say("ALARM:3")
        elif ch == b"\x85":
            with self.lock:
                self.planner = [m for m in self.planner if not m.get("jog")]

    def _exec(self):
        while not self.closed:
            with self.lock:
                m = self.planner[0] if self.planner and not self.hold else None
                gen = self.gen
            if m is None:
                if not self.hold and not self.planner:
                    self.state = "Idle"
                time.sleep(0.01)
                continue
            self.state = "Jog" if m.get("jog") else "Run"
            start = list(self.mpos)
            tgt = m["to"]
            dist = math.dist(start, tgt)
            dur = dist / max(m["feed"], 1) * 60 / self.speedup
            t0 = time.monotonic()
            while True:
                if self.gen != gen or self.closed:
                    break
                if self.hold:
                    time.sleep(0.01)
                    t0 += 0.01
                    continue
                k = 1.0 if dur <= 0 else min((time.monotonic() - t0) / dur, 1.0)
                self.mpos = [start[i] + (tgt[i] - start[i]) * k for i in range(3)]
                if k >= 1.0:
                    break
                time.sleep(0.005)
            with self.lock:
                if self.gen != gen:
                    continue
                if m.get("burn") and dist > 0:
                    self.burns.append((tuple(start), tuple(tgt), m["burn"]))
                if self.planner and self.planner[0] is m:
                    self.planner.pop(0)

    def _wait_room(self):
        while len(self.planner) >= self.PLANNER and not self.closed:
            time.sleep(0.005)

    def _wait_empty(self):
        while self.planner and not self.closed:
            time.sleep(0.005)

    def _proc(self):
        while not self.closed:
            try:
                line = self.lines.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if self.fw == "grbl":
                    self._grbl(line)
                else:
                    self._marlin(line)
            except Exception as e:  # noqa: BLE001
                self._say(f"error:sim {e}")

    # ---- Grbl ---------------------------------------------------------------
    def _grbl(self, line: str):
        u = line.upper().replace(" ", "")
        if self.alarm and u not in ("$X", "$H") and not u.startswith("$$"):
            self._say("error:9")
            return
        if u == "$$":
            for k, v in (("30", "1000"), ("31", "0"), ("32", "1"), ("110", "5000"), ("130", "300"), ("131", "180")):
                self._say(f"${k}={v}")
            self._say("ok")
            return
        if u == "$X":
            self.alarm = False
            self._say("[MSG:Caution: Unlocked]")
            self._say("ok")
            return
        if u == "$H":
            self._wait_empty()
            self.mpos = [0.0, 0.0, 0.0]
            self.alarm = False
            self._say("ok")
            return
        if u.startswith("$J="):
            self._motion(u[3:], jog=True)
            self._say("ok")
            return
        if u.startswith("$"):
            self._say("ok")
            return
        self._gcode(u)
        self._say("ok")

    # ---- Marlin -------------------------------------------------------------
    def _marlin(self, line: str):
        if self.corrupt and random.random() < self.corrupt and "*" in line:
            body, _, cs = line.rpartition("*")  # line noise: flip one body char
            i = random.randrange(len(body))
            line = body[:i] + chr(ord(body[i]) ^ 0x01) + body[i + 1:] + "*" + cs
        m = re.match(r"N(\d+)\s*(.*?)\*(\d+)$", line)
        if not m and "*" in line:
            self._say(f"Error:No Line Number with checksum, Last Line: {self.expected_n - 1}")
            self._say(f"Resend: {self.expected_n}")
            self._say("ok")
            return
        if m:
            n, body, cs = int(m.group(1)), m.group(2), int(m.group(3))
            calc = 0
            for ch in f"N{n} {body}".encode():
                calc ^= ch
            if body.upper().startswith("M110"):
                self.expected_n = n + 1
                self._say("ok")
                return
            if calc != cs:
                self._say(f"Error:checksum mismatch, Last Line: {self.expected_n - 1}")
                self._say(f"Resend: {self.expected_n}")
                self._say("ok")
                return
            if n != self.expected_n:
                self._say(f"Error:Line Number is not Last Line Number+1, Last Line: {self.expected_n - 1}")
                self._say(f"Resend: {self.expected_n}")
                self._say("ok")
                return
            self.expected_n += 1
            line = body
        u = line.upper().strip()
        if self.halted and not u.startswith("M999"):
            self._say("Error:Printer halted. kill() called!")
            return
        code = u.split()[0] if u else ""
        if code == "M110":
            nn = WORD.findall(u)
            self.expected_n = int(float(dict(nn).get("N", 0))) + 1
        elif code == "M115":
            self._say("FIRMWARE_NAME:Marlin 2.1.2 SIM SOURCE_CODE_URL:github.com/MarlinFirmware/Marlin")
        elif code == "M114":
            wp = [self.mpos[i] - self.wco[i] for i in range(3)]
            self._say(f"X:{wp[0]:.2f} Y:{wp[1]:.2f} Z:{wp[2]:.2f} E:0.00 Count X:0 Y:0 Z:0")
        elif code == "M400":
            self._wait_empty()
        elif code == "M410":
            with self.lock:
                self.planner.clear()
                self.gen += 1
        elif code == "M112":
            self.halted = True
            with self.lock:
                self.planner.clear()
                self.gen += 1
            self._say("Error:Printer halted. kill() called!")
            return
        elif code == "M999":
            self.halted = False
        elif code == "G28":
            self._wait_empty()
            self.mpos[0] = self.mpos[1] = 0.0
            self.wco[0] = self.wco[1] = 0.0
        else:
            self._gcode(u)
        self._say("ok")

    # ---- shared G-code ------------------------------------------------------
    def _gcode(self, u: str):
        words = WORD.findall(u)
        codes = [(k, v) for k, v in words]
        g = [float(v) for k, v in codes if k == "G"]
        mm = [float(v) for k, v in codes if k == "M"]
        vals = {k: float(v) for k, v in codes if k not in ("G", "M")}
        if 90 in g:
            self.absolute = True
        if 91 in g:
            self.absolute = False
        if 10 in g and vals.get("L") == 20:
            self._wait_empty()
            for i, a in enumerate("XYZ"):
                if a in vals:
                    self.wco[i] = self.mpos[i] - vals[a]
            return
        if 92 in g:
            self._wait_empty()
            for i, a in enumerate("XYZ"):
                if a in vals:
                    self.wco[i] = self.mpos[i] - vals[a]
            return
        for m in mm:
            if m in (3, 4):
                self.laser_on = True
                if "I" in vals and self.fw == "marlin":
                    self.laser_on = "inline"
            elif m == 5:
                self.laser_on = False
                self.s = 0
            elif m == 106:
                self.fan = vals.get("S", 255)
            elif m == 107:
                self.fan = 0
        if "S" in vals and 106 not in mm:
            self.s = vals["S"]
        if "F" in vals:
            self.feed = vals["F"]
        if any(a in vals for a in "XYZ"):
            self._motion(u, rapid=(0 in g and 1 not in g))

    def _motion(self, u: str, rapid: bool = False, jog: bool = False):
        vals = {k: float(v) for k, v in WORD.findall(u) if k in "XYZF"}
        rel = (not self.absolute) or ("G91" in u)
        if jog:
            rel = "G91" in u
        with self.lock:
            base = self.planner[-1]["to"] if self.planner else self.mpos
        tgt = list(base)
        for i, a in enumerate("XYZ"):
            if a in vals:
                tgt[i] = base[i] + vals[a] if rel else vals[a] + self.wco[i]
        feed = 3000.0 if rapid else vals.get("F", self.feed)
        if not jog and "F" in vals:
            self.feed = vals["F"]
        burn = 0.0
        if not rapid and not jog:
            if self.fw == "grbl":
                burn = self.s if self.laser_on else 0
            elif self.laser_on == "inline" or self.laser_on:
                burn = self.s
            if self.fan:
                burn = self.fan
        self._wait_room()
        with self.lock:
            self.planner.append({"to": tgt, "feed": feed, "burn": burn, "jog": jog})
