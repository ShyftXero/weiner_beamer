"""Talking to machines.

Controller           - common state, logging, job streaming, fault handling
  GrblController     - character-counting streaming, realtime ? ! ~ ^X
  MarlinController   - ok-counted streaming with N/checksum + Resend, M114 polling
  ManualController   - no link at all; jobs are exported for another sender

Transports are anything with write(bytes) / readline() -> bytes / close():
pyserial URLs (``/dev/ttyUSB0``, ``socket://host:23`` for WiFi boards) or the
simulators in sim.py (port ``sim``).

Fault model: a link that drops, stalls (no ack within ``stall_timeout``), or
raises ALARM/kill ends the current stream with an error and commands the laser
off if the link still works.  The controller then keeps trying to reconnect in
the background.  Callers learn the outcome through ``StreamResult``.
"""

from __future__ import annotations

import collections
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .dialects import Line

log = logging.getLogger("weiner_beamer.ctl")

GRBL_RX = 127  # Grbl serial RX buffer is 128 bytes; keep one spare


class LinkError(RuntimeError):
    pass


def open_transport(port: str, baud: int, firmware: str):
    if port.startswith("sim"):
        from .sim import SimTransport

        parts = port.split(":")
        speed = float(parts[1]) if len(parts) > 1 and parts[1] else 20.0
        corrupt = float(parts[2]) if len(parts) > 2 else 0.0
        return SimTransport(firmware, speedup=speed, corrupt=corrupt)
    import serial

    t = serial.serial_for_url(port, baudrate=baud, timeout=0.1, write_timeout=2)
    return t


@dataclass
class StreamResult:
    ok: bool = False
    canceled: bool = False
    error: str | None = None
    lines_done: int = 0
    last_safe: int = -1  # index of the last acknowledged safe (laser-off) line


@dataclass
class _Pending:
    text: str
    idx: int  # index in current stream, -1 for ad-hoc commands
    n: int = 0  # Marlin line number
    event: threading.Event = field(default_factory=threading.Event)
    reply: str = ""
    resend: bool = False


class Controller:
    firmware = "base"

    def __init__(self, machine: dict):
        self.machine = machine
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.transport = None
        self.connected = False
        self.ready = False  # handshake finished; safe to stream
        self.state = "Disconnected"
        self.wpos = [0.0, 0.0, 0.0]
        self.last_error: str | None = None
        self.log: collections.deque[str] = collections.deque(maxlen=400)
        self.pending: collections.deque[_Pending] = collections.deque()
        self.streaming = False
        self.progress = (0, 0)  # lines acked, total
        self.paused = False
        self._abort = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._want_connected = False
        self._last_rx = time.monotonic()
        self.info: dict = {}
        self.feed_ov = 100  # live feed override %, relative to the speed baked into the job
        self._inject: collections.deque[str] = collections.deque()  # lines to slip into a running stream

    # ---- lifecycle -------------------------------------------------------
    @property
    def port(self) -> str:
        return self.machine.get("port", "sim")

    def start(self):
        """Connect and keep reconnecting until stop()."""
        self._want_connected = True
        t = threading.Thread(target=self._supervise, name=f"sup-{self.machine['id']}", daemon=True)
        t.start()

    def stop(self):
        self._want_connected = False
        self._stop.set()
        self._drop("stopped")

    def _supervise(self):
        backoff = 1.0
        while self._want_connected and not self._stop.is_set():
            if not self.connected:
                try:
                    self._connect()
                    backoff = 1.0
                except Exception as e:  # noqa: BLE001
                    self._set_error(f"connect failed: {e}")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 15)
                    continue
            self._stop.wait(0.5)

    def _connect(self):
        self._log(f"connecting to {self.port}")
        self.transport = open_transport(self.port, int(self.machine.get("baud", 115200)), self.firmware)
        self._stop.clear()
        with self.lock:
            self.pending.clear()
            self.connected = True
            self.state = "Connecting"
        self._last_rx = time.monotonic()
        r = threading.Thread(target=self._reader, name=f"rx-{self.machine['id']}", daemon=True)
        r.start()
        self._threads = [r]
        self._handshake()
        p = threading.Thread(target=self._poller, name=f"poll-{self.machine['id']}", daemon=True)
        p.start()
        self._threads.append(p)
        self.last_error = None
        self.ready = True
        self._log("connected")

    def _drop(self, why: str):
        with self.lock:
            was = self.connected
            self.connected = False
            self.ready = False
            self.state = "Disconnected"
            for p in self.pending:
                p.reply = f"error:link lost ({why})"
                p.event.set()
            self.pending.clear()
            self.cond.notify_all()
        t, self.transport = self.transport, None
        if t is not None:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        if was:
            self._log(f"link dropped: {why}")

    def _write(self, data: bytes):
        t = self.transport
        if t is None:
            raise LinkError("not connected")
        try:
            t.write(data)
        except Exception as e:  # noqa: BLE001
            self._drop(f"write failed: {e}")
            raise LinkError(str(e)) from e

    def _reader(self):
        while self.connected:
            t = self.transport
            if t is None:
                break
            try:
                raw = t.readline()
            except Exception as e:  # noqa: BLE001
                self._drop(f"read failed: {e}")
                break
            if not raw:
                continue
            self._last_rx = time.monotonic()
            line = raw.decode("ascii", "replace").strip()
            if line:
                try:
                    self._on_line(line)
                except Exception:  # noqa: BLE001
                    log.exception("bad line %r", line)

    # ---- helpers -----------------------------------------------------------
    def _log(self, msg: str):
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def _set_error(self, msg: str):
        self.last_error = msg
        self._log(f"! {msg}")

    def snapshot(self) -> dict:
        return {
            "connected": self.connected,
            "ready": self.ready,
            "state": self.state if self.ready or not self.connected else "Connecting",
            "wpos": [round(v, 3) for v in self.wpos],
            "streaming": self.streaming,
            "paused": self.paused,
            "progress": list(self.progress),
            "last_error": self.last_error,
            "info": self.info,
            "feed_override": self.feed_ov,
        }

    # ---- commands -------------------------------------------------------------
    def command(self, lines: list[str], timeout: float = 30.0) -> list[str]:
        """Send ad-hoc lines (jog, zero...) and wait for each ack.  Refused while
        a job streams, except where the subclass allows it."""
        if self.streaming:
            raise LinkError("machine is running a job")
        replies = []
        for text in lines:
            p = self._send(text, -1)
            if not p.event.wait(timeout):
                raise LinkError(f"no reply to {text!r}")
            replies.append(p.reply)
            if p.reply.lower().startswith(("error", "alarm")):
                raise LinkError(f"{text}: {p.reply}")
        return replies

    def stream(self, lines: list[Line], on_line: Callable[[int, Line], None] | None = None) -> StreamResult:
        """Stream a whole program, blocking until done/failed/canceled."""
        res = StreamResult()
        if not (self.connected and self.ready):
            res.error = "machine not connected"
            return res
        with self.lock:
            if self.streaming:
                res.error = "machine busy"
                return res
            self.streaming = True
            self.paused = False
            self._abort = False
            self.progress = (0, len(lines))
        stall = float(self.machine.get("stall_timeout", 30))
        sent: list[_Pending] = []
        try:
            for i, ln in enumerate(lines):
                if self.paused and self._can_hold(lines, i):
                    self._hold_loop()
                if self._abort:
                    res.canceled = True
                    break
                if not self.connected:
                    res.error = "link lost"
                    break
                while self._inject:
                    sent.append(self._send(self._inject.popleft(), -1))
                sent.append(self._send(ln.text, i))
                self._harvest(sent, lines, res, on_line)
                if res.error:
                    break
            # drain acks
            while not res.error and not res.canceled and sent:
                if self._abort:
                    res.canceled = True
                    break
                if not self.connected:
                    res.error = "link lost"
                    break
                if time.monotonic() - self._last_rx > stall:
                    res.error = f"controller stopped responding for {stall:.0f}s"
                    break
                sent[0].event.wait(0.2)
                self._harvest(sent, lines, res, on_line)
            if not res.error and not res.canceled:
                res.error = self._wait_idle(stall)
                res.ok = res.error is None
        except LinkError as e:
            res.error = str(e)
        finally:
            with self.lock:
                self.streaming = False
                self.paused = False
            if res.error or res.canceled:
                self._safe_stop(res)
        return res

    def _harvest(self, sent, lines, res, on_line):
        while sent and sent[0].event.is_set():
            p = sent.pop(0)
            if p.idx < 0:  # injected (e.g. M220): never fails the job
                if p.reply.lower().startswith("error"):
                    self._log(f"! {p.text}: {p.reply}")
                continue
            if p.reply.lower().startswith(("error", "alarm", "!!")):
                res.error = f"line {p.idx + 1} `{p.text}`: {p.reply}"
                return
            res.lines_done = p.idx + 1
            if lines[p.idx].safe:
                res.last_safe = p.idx
            self.progress = (res.lines_done, len(lines))
            if on_line:
                on_line(p.idx, lines[p.idx])

    def _can_hold(self, lines: list[Line], i: int) -> bool:
        """May the host stop feeding before line i?  Firmware without a realtime
        feed hold must only stop where the laser is known to be off."""
        return True

    def _hold_loop(self):
        while self.paused and not self._abort and self.connected:
            time.sleep(0.1)

    # ---- per-firmware hooks -------------------------------------------------
    def _handshake(self): ...
    def _poller(self): ...
    def _on_line(self, line: str): ...
    def _send(self, text: str, idx: int) -> _Pending: ...
    def _wait_idle(self, stall: float) -> str | None:
        return None
    def _safe_stop(self, res: StreamResult): ...
    def feed_override(self, pct: int):
        """Scale the feed of everything still to run by pct% (live speed dial)."""
        raise LinkError("this machine has no live feed override")
    def pause(self): self.paused = True
    def resume(self): self.paused = False
    def abort(self): self._abort = True; self.paused = False
    def estop(self): self.abort()


class GrblController(Controller):
    firmware = "grbl"
    STATUS_RE = re.compile(r"<([A-Za-z]+)(?::\d)?\|(.*)>")

    def __init__(self, machine):
        super().__init__(machine)
        self.wco = [0.0, 0.0, 0.0]
        self._welcome = threading.Event()
        self.settings: dict[str, str] = {}

    def _handshake(self):
        self._welcome.clear()
        self._write(b"\r\n\r\n")
        time.sleep(0.2)
        self._write(b"?")
        # an Arduino-based Grbl resets on open and prints a banner ~2s later
        self._welcome.wait(2.5)
        try:
            for r in self.command(["$$"], timeout=5):
                pass
        except LinkError as e:
            self._log(f"$$ failed: {e}")
        s32 = self.settings.get("32")
        self.info = {"firmware": self.info.get("firmware", "Grbl"), "laser_mode": s32 == "1",
                     "s_max": self.settings.get("30")}
        if s32 is not None and s32 != "1":
            self._set_error("Grbl laser mode is OFF ($32=0) - set $32=1 or burns will blob at every corner")

    def _poller(self):
        while self.connected:
            try:
                self._write(b"?")
            except LinkError:
                break
            time.sleep(0.25)

    def _on_line(self, line):
        m = self.STATUS_RE.match(line)
        if m:
            self.state = m.group(1)
            fields = dict(f.split(":", 1) for f in m.group(2).split("|") if ":" in f)
            if "WCO" in fields:
                self.wco = [float(v) for v in fields["WCO"].split(",")[:3]]
            if "MPos" in fields:
                mp = [float(v) for v in fields["MPos"].split(",")[:3]]
                self.wpos = [mp[i] - self.wco[i] for i in range(3)]
            elif "WPos" in fields:
                self.wpos = [float(v) for v in fields["WPos"].split(",")[:3]]
            if "Ov" in fields:
                try:
                    self.feed_ov = int(fields["Ov"].split(",")[0])
                except ValueError:
                    pass
            return
        if line == "ok" or line.startswith("error"):
            with self.lock:
                if self.pending:
                    p = self.pending.popleft()
                    p.reply = line
                    p.event.set()
                    self.cond.notify_all()
            if line.startswith("error"):
                self._log(f"< {line}")
            return
        if line.startswith("ALARM"):
            self.state = "Alarm"
            self._set_error(f"{line} - clear with Unlock ($X) or Home")
            with self.lock:
                for p in self.pending:
                    p.reply = line
                    p.event.set()
                self.pending.clear()
                self.cond.notify_all()
            return
        if line.startswith("Grbl"):
            self.info["firmware"] = line
            self.feed_ov = 100  # a reset clears overrides
            with self.lock:
                for p in self.pending:
                    p.reply = "error:controller reset"
                    p.event.set()
                self.pending.clear()
                self.cond.notify_all()
            self._welcome.set()
            self._log(f"< {line}")
            return
        if line.startswith("$") and "=" in line:
            k, v = line[1:].split("=", 1)
            self.settings[k] = v.split()[0]
            return
        self._log(f"< {line}")

    def _send(self, text, idx):
        text = text.strip()
        p = _Pending(text, idx)
        n = len(text) + 1
        with self.cond:
            while self.connected and sum(len(q.text) + 1 for q in self.pending) + n > GRBL_RX:
                self.cond.wait(0.5)
            if not self.connected:
                raise LinkError("not connected")
            self.pending.append(p)
        self._write((text + "\n").encode())
        return p

    def _wait_idle(self, stall):
        # Wait until the planner has actually finished moving.
        t0 = time.monotonic()
        time.sleep(0.3)
        while self.connected and self.state not in ("Idle",):
            if self._abort:
                return "canceled"
            if self.state == "Alarm":
                return self.last_error or "alarm"
            if time.monotonic() - self._last_rx > stall:
                return "controller stopped responding"
            if time.monotonic() - t0 > 3600:
                return "job never went idle"
            time.sleep(0.1)
        return None if self.connected else "link lost"

    def pause(self):
        super().pause()
        try:
            self._write(b"!")
        except LinkError:
            pass

    def resume(self):
        super().resume()
        try:
            self._write(b"~")
        except LinkError:
            pass

    def abort(self):
        super().abort()
        self._grbl_reset()

    estop = abort

    def _grbl_reset(self):
        """Feed hold, wait for the machine to decelerate, then soft-reset.
        Resetting from Hold keeps position (no ALARM); reset also kills the laser."""
        try:
            self._write(b"!")
            t0 = time.monotonic()
            while time.monotonic() - t0 < 2 and not self.state.startswith("Hold") and self.state != "Idle":
                time.sleep(0.05)
            time.sleep(0.2)
            self._write(b"\x18")
        except LinkError:
            pass

    def feed_override(self, pct):
        """Grbl realtime feed override: reset to 100%, then step by 10s and 1s.
        Takes effect immediately, including on moves already in the planner."""
        pct = max(10, min(200, int(round(pct))))
        delta = pct - 100
        tens, ones = int(delta / 10), delta - int(delta / 10) * 10
        seq = b"\x90" + (b"\x91" if tens > 0 else b"\x92") * abs(tens) + (b"\x93" if ones > 0 else b"\x94") * abs(ones)
        self._write(seq)
        self.feed_ov = pct

    def jog_cancel(self):
        try:
            self._write(b"\x85")
        except LinkError:
            pass

    def _safe_stop(self, res):
        if not res.canceled and self.connected:
            self._grbl_reset()


class MarlinController(Controller):
    firmware = "marlin"
    POS_RE = re.compile(r"X:(-?[\d.]+)\s*Y:(-?[\d.]+)\s*Z:(-?[\d.]+)")

    def __init__(self, machine):
        super().__init__(machine)
        self.n = 0
        self._resends = 0
        self._started = threading.Event()
        self.checksums = bool(machine.get("marlin", {}).get("checksums", True))
        self.inflight = max(1, int(machine.get("marlin", {}).get("inflight", 2)))

    def _handshake(self):
        self._started.wait(3)  # most boards reset on open and print "start"
        self.n = 0
        if self.checksums:
            self.command(["M110 N0"], timeout=10)
        try:
            self.command(["M115"], timeout=5)
        except LinkError as e:
            self._log(f"M115: {e}")
        try:
            self.command(["M154 S1"], timeout=5)  # position auto-report if compiled in
        except LinkError:
            pass
        self.state = "Idle"

    def _poller(self):
        while self.connected:
            time.sleep(1.0)
            if self.connected and not self.streaming and not self.pending:
                try:
                    self.command(["M114"], timeout=10)
                except LinkError:
                    pass
            if self.streaming:
                self.state = "Hold" if self.paused else "Run"
            elif not self.pending and self.state in ("Run", "Hold", "Connecting"):
                self.state = "Idle"

    def _on_line(self, line):
        low = line.lower()
        m = self.POS_RE.search(line)
        if m and not low.startswith("ok"):
            self.wpos = [float(m.group(i)) for i in (1, 2, 3)]
            if line.startswith("X:"):
                return
        if low.startswith("ok"):
            with self.lock:
                if self.pending:
                    p = self.pending.popleft()
                    if p.resend:
                        # re-send in place: the slot count is unchanged and
                        # later marked lines come back through here in order
                        p.resend = False
                        self.pending.append(p)
                        try:
                            self._write(self._frame(p))
                        except LinkError:
                            pass
                    else:
                        p.reply = p.reply or "ok"
                        p.event.set()
                        if not p.reply.lower().startswith("error"):
                            self._resends = 0
                self.cond.notify_all()
            return
        if low.startswith("resend:") or low.startswith("rs "):
            try:
                n = int(re.findall(r"\d+", line)[0])
            except IndexError:
                return
            # Marlin answers every rejected line with its own Resend + ok, in
            # order, so this Resend belongs to the oldest unacked line.
            with self.lock:
                self._resends += 1
                if self._resends > 20:
                    # no progress: line numbers are out of sync; fail loudly
                    if self.pending:
                        self.pending[0].reply = "error:Marlin resend desync"
                elif self.pending and self.pending[0].n >= n:
                    self.pending[0].resend = True
            self._log(f"< {line}")
            return
        if low.startswith("echo:busy") or low == "wait":
            return
        if low.startswith("start"):
            self._started.set()
            self.feed_ov = 100
            with self.lock:
                for p in self.pending:
                    p.reply = "error:controller reset"
                    p.event.set()
                self.pending.clear()
                self.cond.notify_all()
            self.n = 0
            self._log("< start (controller reset)")
            return
        if low.startswith("firmware_name"):
            self.info["firmware"] = line.split("SOURCE_CODE_URL")[0].strip()
        if low.startswith("error"):
            fatal = any(w in low for w in ("halted", "kill", "stopped", "printer halted"))
            if "line number" in low or "checksum" in low:
                self._log(f"< {line}")
                return  # a Resend follows
            with self.lock:
                if self.pending:
                    self.pending[0].reply = line  # the ok that follows carries it
                if fatal:
                    self.state = "Alarm"
                    for p in self.pending:
                        p.reply = line
                        p.event.set()
                    self.pending.clear()
                    self.cond.notify_all()
            self._set_error(line if not fatal else f"{line} - reset the board (M999 / power cycle)")
            return
        self._log(f"< {line}")

    def _drain_quiet(self, quiet: float = 0.4, limit: float = 5.0):
        """Let replies to already-sent lines arrive and discard them, so a
        following command is not credited with a stale ok."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            with self.lock:
                for p in self.pending:
                    p.reply = p.reply or "error:discarded"
                    p.event.set()
                self.pending.clear()
                self._resends = 0
            if time.monotonic() - self._last_rx > quiet:
                return
            time.sleep(0.05)

    def _frame(self, p: _Pending) -> bytes:
        if not self.checksums:
            return (p.text + "\n").encode()
        body = f"N{p.n} {p.text}"
        cs = 0
        for ch in body.encode():
            cs ^= ch
        return f"{body}*{cs}\n".encode()

    def _send(self, text, idx):
        text = text.split(";")[0].strip()
        with self.cond:
            while self.connected and len(self.pending) >= self.inflight:
                self.cond.wait(0.5)
            if not self.connected:
                raise LinkError("not connected")
            if self.checksums and not text.startswith("M110"):
                self.n += 1
            p = _Pending(text, idx, n=self.n)
            self.pending.append(p)
            self._write(self._frame(p))
        return p

    def _can_hold(self, lines, i):
        return i == 0 or lines[i - 1].safe

    def _hold_loop(self):
        self.state = "Hold"
        super()._hold_loop()

    def _wait_idle(self, stall):
        try:
            p = self._send("M400", -1)
            t0 = time.monotonic()
            while not p.event.wait(0.5):
                if self._abort:
                    return "canceled"
                if not self.connected:
                    return "link lost"
                if time.monotonic() - self._last_rx > max(stall, 60):
                    return "controller stopped responding"
                if time.monotonic() - t0 > 3600:
                    return "job never finished"
            return None
        except LinkError as e:
            return str(e)

    def pause(self):
        # Takes effect at the next laser-off point; see _can_hold.
        self.paused = True

    def abort(self):
        self._abort = True
        self.paused = False

    def feed_override(self, pct):
        """M220 is queued like any command: while streaming it is slipped in
        ahead of the next line and applies once the few moves already in
        Marlin's buffer have run."""
        pct = max(10, min(999, int(round(pct))))
        if self.streaming:
            self._inject.append(f"M220 S{pct}")
        else:
            self.command([f"M220 S{pct}"], timeout=10)
        self.feed_ov = pct

    def estop(self):
        self._abort = True
        try:
            # With EMERGENCY_PARSER these run immediately.  Without it they are
            # queued behind at most `inflight` lines, which is why inflight is small.
            self._write(b"M410\n")
            self._write(b"M107\n")
            self._write(b"M5\n")
        except LinkError:
            pass

    def _safe_stop(self, res):
        if not self.connected:
            return
        self._drain_quiet()
        try:
            # resync line numbers and make sure the laser is off
            self.n = 0
            if self.checksums:
                self.command(["M110 N0"], timeout=10)
            self.command(["M107", "M5"], timeout=10)
        except LinkError as e:
            self._set_error(f"could not confirm laser off: {e}")


class ManualController(Controller):
    """No live link: the operator runs exported G-code in another sender
    (LightBurn, UGS, Pronterface, OctoPrint) and marks the job done."""

    firmware = "manual"

    def start(self):
        self.connected = False
        self.state = "Manual"

    def stop(self):
        pass

    def snapshot(self):
        d = super().snapshot()
        d["state"] = "Manual"
        return d


def make_controller(machine: dict) -> Controller:
    fw = machine.get("firmware", "grbl")
    if fw == "grbl":
        return GrblController(machine)
    if fw == "marlin":
        return MarlinController(machine)
    return ManualController(machine)
