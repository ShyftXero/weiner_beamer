"""End-to-end through HTTP against simulated Grbl and Marlin machines."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from weiner_beamer.app import create_app
from weiner_beamer.store import Store


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, password="")
    with TestClient(app) as c:
        # speed the default simulated machines way up
        for mid in ("genmitsu-3018", "ender-laser"):
            c.post("/api/machines", json={"id": mid, "port": "sim:400"}).raise_for_status()
        wait_ready(c, "genmitsu-3018")
        wait_ready(c, "ender-laser")
        yield c


def machine(c, mid):
    return next(m for m in c.get("/api/state").json()["machines"] if m["config"]["id"] == mid)


def wait_ready(c, mid, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if machine(c, mid)["status"].get("ready"):
            return
        time.sleep(0.05)
    raise AssertionError(f"{mid} never became ready")


def wait_idle(c, mid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if machine(c, mid)["activity"] is None:
            return
        time.sleep(0.05)
    raise AssertionError(f"{mid} still busy")


def add(c, text="Bob", **spec):
    body = {"kind": "text", "title": text, "params": {"text": text, "mode": "both"}, **spec}
    r = c.post("/api/jobs", data={"spec": json.dumps(body)})
    r.raise_for_status()
    return r.json()["ids"]


def job(c, jid):
    return c.get(f"/api/jobs/{jid}").json()


def test_preview_and_meta(client):
    meta = client.get("/api/meta").json()
    assert "Sans Bold" in meta["fonts"] and "crosshatch" in meta["patterns"]
    r = client.post("/api/preview", data={"spec": json.dumps({"kind": "pattern", "params": {"pattern": "zigzag"}})})
    body = r.json()
    assert body["ops"] and body["stats"]["est_seconds"] > 0 and not body["warnings"]
    bad = client.post("/api/preview", data={"spec": json.dumps({"kind": "text", "params": {"text": ""}})})
    assert bad.status_code == 422


def test_batch_fills_slots_then_rerun(client):
    ids = add(client, copies=4)
    r = client.post("/api/machines/genmitsu-3018/run", json={})
    assert [p["job_id"] for p in r.json()["plan"]] == ids[:3]  # 3 slots
    wait_idle(client, "genmitsu-3018")
    assert [job(client, i)["status"] for i in ids] == ["done", "done", "done", "queued"]
    # operator re-runs a finished job on another dog
    client.post("/api/machines/genmitsu-3018/run", json={"job_ids": [ids[0]]}).raise_for_status()
    wait_idle(client, "genmitsu-3018")
    j = job(client, ids[0])
    assert j["status"] == "done" and j["runs"] == 2 and len(j["run_history"]) == 2


def test_routing_respects_target_and_dog_type(client):
    a = add(client, "A", machine_id="genmitsu-3018")[0]
    b = add(client, "B", dog_type="jumbo")[0]
    c_ = add(client, "C")[0]
    r = client.post("/api/machines/ender-laser/run", json={})
    assert [p["job_id"] for p in r.json()["plan"]] == [c_]
    wait_idle(client, "ender-laser")
    assert job(client, a)["status"] == "queued" and job(client, b)["status"] == "queued"
    assert job(client, c_)["status"] == "done"
    nothing = client.post("/api/machines/ender-laser/run", json={})
    assert nothing.status_code == 409


def test_abort_then_resume(client):
    client.post("/api/machines", json={"id": "genmitsu-3018", "port": "sim:3"}).raise_for_status()
    wait_ready(client, "genmitsu-3018")
    jid = add(client, "Resume me please")[0]
    client.post("/api/machines/genmitsu-3018/run", json={"job_ids": [jid]}).raise_for_status()
    t0 = time.time()
    while (machine(client, "genmitsu-3018")["activity"] or {}).get("op_done", -1) < 5:
        assert time.time() - t0 < 30
        time.sleep(0.05)
    client.post("/api/machines/genmitsu-3018/abort")
    wait_idle(client, "genmitsu-3018")
    j = job(client, jid)
    assert j["status"] == "interrupted" and j["last_op"] >= 5
    wait_ready(client, "genmitsu-3018")
    client.post("/api/machines", json={"id": "genmitsu-3018", "port": "sim:400"}).raise_for_status()
    wait_ready(client, "genmitsu-3018")
    client.post("/api/machines/genmitsu-3018/run", json={"job_ids": [jid], "resume": True}).raise_for_status()
    wait_idle(client, "genmitsu-3018")
    j = job(client, jid)
    assert j["status"] == "done"
    assert j["run_history"][0]["start_op"] == j["run_history"][1]["last_op"] + 1


def test_link_loss_interrupts_job_and_reconnects(client):
    client.post("/api/machines", json={"id": "genmitsu-3018", "port": "sim:3"}).raise_for_status()
    wait_ready(client, "genmitsu-3018")
    jid = add(client)[0]
    client.post("/api/machines/genmitsu-3018/run", json={"job_ids": [jid]}).raise_for_status()
    time.sleep(0.5)
    ctl = client.app.state.fleet.ctl["genmitsu-3018"]
    ctl.transport.close()  # yank the USB cable
    wait_idle(client, "genmitsu-3018")
    j = job(client, jid)
    assert j["status"] == "interrupted" and j["error"]
    wait_ready(client, "genmitsu-3018", timeout=20)  # supervisor reconnected


def test_noisy_marlin_line_still_completes(client):
    client.post("/api/machines", json={"id": "ender-laser", "port": "sim:400:0.05",
                                       "laser": {"mode": "inline"}}).raise_for_status()
    wait_ready(client, "ender-laser")
    jid = add(client, "Noise")[0]
    client.post("/api/machines/ender-laser/run", json={"job_ids": [jid]}).raise_for_status()
    wait_idle(client, "ender-laser", timeout=60)
    assert job(client, jid)["status"] == "done"


def test_queue_management(client):
    ids = add(client, copies=3)
    client.post(f"/api/jobs/{ids[2]}/action", json={"action": "top"})
    client.post(f"/api/jobs/{ids[0]}/action", json={"action": "hold"})
    state = client.get("/api/state").json()["jobs"]
    queued = [j["id"] for j in state if j["status"] in ("queued", "held")]
    assert queued[0] == ids[2]
    r = client.post("/api/machines/genmitsu-3018/run", json={"count": 1})
    assert r.json()["plan"][0]["job_id"] == ids[2]
    wait_idle(client, "genmitsu-3018")
    # fix a typo and requeue
    spec = {"kind": "text", "title": "Rob", "params": {"text": "Rob"}}
    client.post(f"/api/jobs/{ids[2]}/edit", data={"spec": json.dumps(spec)}).raise_for_status()
    j = job(client, ids[2])
    assert j["status"] == "queued" and j["params"]["text"] == "Rob"
    dup = client.post(f"/api/jobs/{ids[2]}/action", json={"action": "duplicate"}).json()["id"]
    assert job(client, dup)["params"]["text"] == "Rob"
    gcode = client.get(f"/api/jobs/{ids[2]}/gcode", params={"machine": "ender-laser"}).text
    assert "M106" in gcode and "G92" not in gcode


def test_alignment_center_on_ends(client):
    mid = "genmitsu-3018"
    client.post(f"/api/machines/{mid}/jog", json={"dx": 20, "dy": 10}).raise_for_status()
    time.sleep(0.4)
    client.post(f"/api/machines/{mid}/mark", json={"end": "a"}).raise_for_status()
    client.post(f"/api/machines/{mid}/jog", json={"dx": 140, "dy": 10}).raise_for_status()
    time.sleep(0.6)
    client.post(f"/api/machines/{mid}/mark", json={"end": "b"}).raise_for_status()
    al = client.post(f"/api/machines/{mid}/center").json()
    assert al["angle"] == pytest.approx(4.09, abs=0.3)
    time.sleep(0.5)
    wpos = machine(client, mid)["status"]["wpos"]
    assert abs(wpos[0]) < 0.5 and abs(wpos[1]) < 0.5


def test_restart_marks_running_jobs_interrupted(tmp_path):
    s = Store(tmp_path / "x.db")
    jid = s.add_job({"kind": "text", "params": {"text": "x"}})
    s.update_job(jid, status="running")
    s.start_run(jid, "m", 0, "run", 0, 10)
    s2 = Store(tmp_path / "x.db")
    assert s2.job(jid)["status"] == "interrupted"
    assert s2.runs(jid)[0]["result"] == "interrupted"
