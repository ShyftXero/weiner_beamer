"""SQLite persistence: machines, jobs, run history, settings.

Everything the operator cares about survives a crash or restart.  A job that
was running when the process died comes back as ``interrupted`` with the
last completed stroke recorded, so it can be resumed or re-run.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from pathlib import Path

JOB_STATUSES = ("queued", "held", "running", "done", "failed", "canceled", "interrupted")
FINISHED = ("done", "failed", "canceled", "interrupted")

DOG_PRESETS = {
    "regular": {"type": "regular", "length": 150, "diameter": 24},
    "jumbo": {"type": "jumbo", "length": 175, "diameter": 30},
    "footlong": {"type": "footlong", "length": 290, "diameter": 24},
    "cocktail": {"type": "cocktail", "length": 60, "diameter": 14},
    "bratwurst": {"type": "bratwurst", "length": 160, "diameter": 32},
}

MACHINE_DEFAULTS = {
    "name": "New machine",
    "enabled": True,
    "firmware": "grbl",  # grbl | marlin | manual
    "manual_dialect": "grbl",
    "port": "sim",
    "baud": 115200,
    "stall_timeout": 30,
    "laser": {"mode": "m4", "s_max": 1000, "power_scale": 1.0, "max_feed": 3000,
              "pointer_power": 0.01, "frame_feed": 1500},
    "marlin": {"checksums": True, "inflight": 2},
    "dog": {"type": "regular", "length": 150, "diameter": 24, "focus_tolerance": 1.5,
            "end_margin": 3, "wrap": False, "z_follow": False},
    "jig": {"slots": 1, "pitch_x": 0, "pitch_y": 34, "template_power": 0.35, "template_feed": 1500},
    "align": {"angle": 0.0, "end_a": None, "end_b": None},
    "notes": "",
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
  id TEXT PRIMARY KEY, config TEXT NOT NULL, sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '',
  customer TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  params TEXT NOT NULL,
  image BLOB,
  machine_id TEXT,
  dog_type TEXT,
  passes INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'queued',
  position REAL NOT NULL,
  runs INTEGER NOT NULL DEFAULT 0,
  last_machine TEXT,
  last_op INTEGER NOT NULL DEFAULT -1,
  error TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, position);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER, machine_id TEXT, slot INTEGER, kind TEXT,
  started REAL, ended REAL, result TEXT, error TEXT,
  start_op INTEGER DEFAULT 0, last_op INTEGER DEFAULT -1, total_ops INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JOB_COLS = ("id", "title", "customer", "kind", "params", "machine_id", "dog_type", "passes", "status",
            "position", "runs", "last_machine", "last_op", "error", "created", "updated")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
        self.recover()

    def x(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # ---- crash recovery ---------------------------------------------------
    def recover(self):
        now = time.time()
        self.x("UPDATE jobs SET status='interrupted', error='server restarted mid-run', updated=? "
               "WHERE status='running'", (now,))
        self.x("UPDATE runs SET result='interrupted', ended=?, error='server restarted mid-run' "
               "WHERE ended IS NULL", (now,))

    # ---- settings -----------------------------------------------------------
    def get_setting(self, key: str, default=None):
        r = self.x("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value):
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    # ---- machines -------------------------------------------------------------
    def machines(self) -> list[dict]:
        rows = self.x("SELECT id, config FROM machines ORDER BY sort, id").fetchall()
        return [deep_merge(MACHINE_DEFAULTS, {**json.loads(r["config"]), "id": r["id"]}) for r in rows]

    def machine(self, mid: str) -> dict | None:
        r = self.x("SELECT id, config FROM machines WHERE id=?", (mid,)).fetchone()
        return deep_merge(MACHINE_DEFAULTS, {**json.loads(r["config"]), "id": r["id"]}) if r else None

    def save_machine(self, m: dict):
        cfg = {k: v for k, v in m.items() if k != "id"}
        with self.lock:
            sort = self.x("SELECT COALESCE(MAX(sort),0)+1 FROM machines").fetchone()[0]
            self.x("INSERT INTO machines(id,config,sort) VALUES(?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET config=excluded.config", (m["id"], json.dumps(cfg), sort))

    def delete_machine(self, mid: str):
        self.x("DELETE FROM machines WHERE id=?", (mid,))
        self.x("UPDATE jobs SET machine_id=NULL WHERE machine_id=? AND status IN ('queued','held')", (mid,))

    # ---- jobs -------------------------------------------------------------------
    def _job(self, r) -> dict:
        d = {k: r[k] for k in JOB_COLS}
        d["params"] = json.loads(d["params"])
        d["has_image"] = r["has_image"] if "has_image" in r.keys() else None
        return d

    def jobs(self, include_finished: bool = True, limit: int = 500) -> list[dict]:
        sql = f"SELECT {','.join(JOB_COLS)}, image IS NOT NULL AS has_image FROM jobs"
        if not include_finished:
            sql += " WHERE status IN ('queued','held','running')"
        sql += " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'held' THEN 1 ELSE 2 END," \
               " CASE WHEN status IN ('queued','held','running') THEN position ELSE -updated END LIMIT ?"
        return [self._job(r) for r in self.x(sql, (limit,)).fetchall()]

    def job(self, jid: int) -> dict | None:
        r = self.x(f"SELECT {','.join(JOB_COLS)}, image IS NOT NULL AS has_image FROM jobs WHERE id=?",
                   (jid,)).fetchone()
        return self._job(r) if r else None

    def job_image(self, jid: int) -> bytes | None:
        r = self.x("SELECT image FROM jobs WHERE id=?", (jid,)).fetchone()
        return r["image"] if r else None

    def _next_pos(self) -> float:
        return (self.x("SELECT MAX(position) FROM jobs").fetchone()[0] or 0) + 1

    def _first_pos(self) -> float:
        return (self.x("SELECT MIN(position) FROM jobs WHERE status IN ('queued','held')").fetchone()[0] or 1) - 1

    def add_job(self, spec: dict, image: bytes | None = None) -> int:
        now = time.time()
        with self.lock:
            cur = self.x(
                "INSERT INTO jobs(title,customer,kind,params,image,machine_id,dog_type,passes,status,position,created,updated)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (spec.get("title", ""), spec.get("customer", ""), spec["kind"], json.dumps(spec.get("params", {})),
                 image, spec.get("machine_id") or None, spec.get("dog_type") or None, int(spec.get("passes", 1)),
                 "held" if spec.get("hold") else "queued", self._next_pos(), now, now))
            return cur.lastrowid

    def update_job(self, jid: int, **fields):
        allowed = {"title", "customer", "params", "machine_id", "dog_type", "passes", "status", "position",
                   "runs", "last_machine", "last_op", "error"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "params":
                v = json.dumps(v)
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return
        sets.append("updated=?")
        args += [time.time(), jid]
        self.x(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", args)

    def set_image(self, jid: int, image: bytes):
        self.x("UPDATE jobs SET image=?, updated=? WHERE id=?", (image, time.time(), jid))

    def delete_job(self, jid: int):
        self.x("DELETE FROM jobs WHERE id=?", (jid,))

    def duplicate_job(self, jid: int, at_top: bool = False) -> int | None:
        j = self.job(jid)
        if not j:
            return None
        new = self.add_job({**j, "hold": False}, self.job_image(jid))
        if at_top:
            self.update_job(new, position=self._first_pos())
        return new

    def move_job(self, jid: int, where: str):
        with self.lock:
            j = self.job(jid)
            if not j:
                return
            active = [r for r in self.jobs(include_finished=False) if r["status"] in ("queued", "held")]
            ids = [r["id"] for r in active]
            if where == "top":
                self.update_job(jid, position=self._first_pos())
                return
            if where == "bottom":
                self.update_job(jid, position=self._next_pos())
                return
            if jid not in ids:
                return
            i = ids.index(jid)
            k = i - 1 if where == "up" else i + 1
            if 0 <= k < len(ids):
                other = active[k]
                self.update_job(jid, position=other["position"])
                self.update_job(other["id"], position=j["position"])

    # ---- runs --------------------------------------------------------------------
    def start_run(self, job_id: int | None, machine_id: str, slot: int, kind: str, start_op: int, total_ops: int) -> int:
        cur = self.x("INSERT INTO runs(job_id,machine_id,slot,kind,started,start_op,total_ops) VALUES(?,?,?,?,?,?,?)",
                     (job_id, machine_id, slot, kind, time.time(), start_op, total_ops))
        return cur.lastrowid

    def progress_run(self, run_id: int, last_op: int):
        self.x("UPDATE runs SET last_op=? WHERE id=?", (last_op, run_id))

    def end_run(self, run_id: int, result: str, error: str | None, last_op: int):
        self.x("UPDATE runs SET ended=?, result=?, error=?, last_op=? WHERE id=?",
               (time.time(), result, error, last_op, run_id))

    def runs(self, job_id: int | None = None, limit: int = 100) -> list[dict]:
        if job_id is None:
            rows = self.x("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self.x("SELECT * FROM runs WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)).fetchall()
        return [dict(r) for r in rows]
