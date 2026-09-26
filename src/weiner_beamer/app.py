"""HTTP API + static web UI."""

from __future__ import annotations

import base64
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import designs, geometry
from .dialects import dialect_for
from .fleet import Fleet, FleetError
from .store import DOG_PRESETS, FINISHED, MACHINE_DEFAULTS, Store

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 10 * 1024 * 1024
KINDS = ("text", "pattern", "image", "testgrid")


def create_app(data_dir: Path | None = None, password: str | None = None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("WB_DATA", "data"))
    password = password if password is not None else os.environ.get("WB_PASSWORD") or None
    fonts_dir = data_dir / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    designs.EXTRA_FONT_DIRS.append(fonts_dir)
    designs.available_fonts.cache_clear()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(data_dir / "weiner_beamer.db")
        fleet = Fleet(store)
        fleet.start()
        app.state.store, app.state.fleet = store, fleet
        yield
        fleet.shutdown()

    app = FastAPI(title="WeinerBeamer", lifespan=lifespan)

    if password:
        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            h = request.headers.get("authorization", "")
            ok = False
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                    ok = secrets.compare_digest(pw, password)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="WeinerBeamer"'})
            return await call_next(request)

    @app.exception_handler(FleetError)
    async def fleet_err(_, e: FleetError):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(designs.DesignError)
    async def design_err(_, e: designs.DesignError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    def F(req: Request) -> Fleet:
        return req.app.state.fleet

    def S(req: Request) -> Store:
        return req.app.state.store

    # ---- helpers ------------------------------------------------------------
    def parse_spec(raw: str | dict) -> dict:
        try:
            spec = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"bad spec json: {e}") from e
        if spec.get("kind") not in KINDS:
            raise HTTPException(400, f"kind must be one of {KINDS}")
        if not isinstance(spec.get("params", {}), dict):
            raise HTTPException(400, "params must be an object")
        spec["title"] = str(spec.get("title", ""))[:80]
        spec["customer"] = str(spec.get("customer", ""))[:80]
        spec["passes"] = max(1, min(int(spec.get("passes", 1) or 1), 10))
        return spec

    async def read_upload(f: UploadFile | None) -> bytes | None:
        if f is None:
            return None
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "image larger than 10 MB")
        return data or None

    def preview_payload(ops, dog, passes=1):
        zw, zh = geometry.safe_zone(dog)
        bb = geometry.bbox(ops)
        stats = geometry.path_stats(ops)
        stats["est_seconds"] = round(stats["est_seconds"] * passes, 1)
        warnings = []
        if bb:
            if bb[2] - bb[0] > zw + 0.01 or bb[3] - bb[1] > zh + 0.01:
                warnings.append(f"design is {bb[2]-bb[0]:.1f} x {bb[3]-bb[1]:.1f} mm; in-focus area is "
                                f"{zw:.1f} x {zh:.1f} mm - edges may be blurry or miss the dog")
            if bb[2] - bb[0] > dog["length"] or bb[3] - bb[1] > dog["diameter"]:
                warnings.append("design is bigger than the hotdog")
        else:
            warnings.append("nothing to burn")
        return {"ops": ops, "bbox": bb, "stats": stats, "dog": dog, "safe_zone": [zw, zh], "warnings": warnings}

    # ---- UI -----------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- state ---------------------------------------------------------------
    @app.get("/api/state")
    def state(req: Request, finished: int = 30):
        fleet, store = F(req), S(req)
        machines = [fleet.machine_view(m) for m in store.machines()]
        active = store.jobs(include_finished=False)
        done = [j for j in store.jobs(include_finished=True, limit=len(active) + finished) if j["status"] in FINISHED]
        return {"machines": machines, "jobs": active + done[:finished]}

    @app.get("/api/meta")
    def meta():
        return {"fonts": list(designs.available_fonts().keys()), "patterns": designs.PATTERNS,
                "dog_presets": DOG_PRESETS, "machine_defaults": MACHINE_DEFAULTS, "kinds": KINDS}

    # ---- jobs --------------------------------------------------------------
    @app.post("/api/preview")
    async def preview(req: Request, spec: str = Form(...), image: UploadFile | None = File(None),
                      job_id: int | None = Form(None)):
        sp = parse_spec(spec)
        img = await read_upload(image)
        if img is None and job_id:
            img = S(req).job_image(job_id)
        dog = F(req).dog_for({"machine_id": sp.get("machine_id"), "dog_type": sp.get("dog_type")})
        ops = designs.build(sp["kind"], sp.get("params", {}), dog, img)
        return preview_payload(ops, dog, sp["passes"])

    @app.post("/api/jobs")
    async def create_job(req: Request, spec: str = Form(...), image: UploadFile | None = File(None)):
        sp = parse_spec(spec)
        img = await read_upload(image)
        dog = F(req).dog_for({"machine_id": sp.get("machine_id"), "dog_type": sp.get("dog_type")})
        designs.build(sp["kind"], sp.get("params", {}), dog, img)  # validate before queueing
        copies = max(1, min(int(sp.get("copies", 1) or 1), 50))
        ids = [S(req).add_job(sp, img) for _ in range(copies)]
        return {"ids": ids}

    @app.get("/api/jobs/{jid}")
    def get_job(req: Request, jid: int):
        j = S(req).job(jid)
        if not j:
            raise HTTPException(404)
        return {**j, "run_history": S(req).runs(jid)}

    @app.post("/api/jobs/{jid}/edit")
    async def edit_job(req: Request, jid: int, spec: str = Form(...), image: UploadFile | None = File(None)):
        store = S(req)
        j = store.job(jid)
        if not j:
            raise HTTPException(404)
        if j["status"] == "running":
            raise HTTPException(409, "can't edit a running job")
        sp = parse_spec(spec)
        img = await read_upload(image) or store.job_image(jid)
        dog = F(req).dog_for({"machine_id": sp.get("machine_id"), "dog_type": sp.get("dog_type")})
        designs.build(sp["kind"], sp.get("params", {}), dog, img)
        if sp["kind"] != j["kind"]:
            raise HTTPException(400, "can't change job kind; make a new job")
        store.update_job(jid, title=sp["title"], customer=sp["customer"], params=sp.get("params", {}),
                         machine_id=sp.get("machine_id") or None, dog_type=sp.get("dog_type") or None,
                         passes=sp["passes"], last_op=-1, error=None,
                         status="queued" if sp.get("requeue", True) else j["status"])
        if img and image is not None:
            store.set_image(jid, img)
        return store.job(jid)

    @app.post("/api/jobs/{jid}/action")
    def job_action(req: Request, jid: int, body: dict = Body(...)):
        store, fleet = S(req), F(req)
        j = store.job(jid)
        if not j:
            raise HTTPException(404)
        a = body.get("action")
        running = j["status"] == "running"
        if a in ("up", "down", "top", "bottom"):
            store.move_job(jid, a)
        elif a == "hold":
            if j["status"] != "queued":
                raise HTTPException(409, "only queued jobs can be held")
            store.update_job(jid, status="held")
        elif a in ("release", "requeue"):
            if running:
                raise HTTPException(409, "job is running")
            store.update_job(jid, status="queued", error=None, last_op=-1 if a == "requeue" else j["last_op"])
            if a == "requeue" and body.get("top"):
                store.move_job(jid, "top")
        elif a == "cancel":
            if running:
                raise HTTPException(409, "job is running - abort it on the machine")
            store.update_job(jid, status="canceled")
        elif a == "duplicate":
            return {"id": store.duplicate_job(jid, at_top=bool(body.get("top")))}
        elif a in ("mark_done", "mark_failed"):
            fleet.finish_manual(jid, a == "mark_done")
        elif a == "assign":
            if running:
                raise HTTPException(409, "job is running")
            store.update_job(jid, machine_id=body.get("machine_id") or None)
        else:
            raise HTTPException(400, f"unknown action {a!r}")
        return store.job(jid)

    @app.delete("/api/jobs/{jid}")
    def delete_job(req: Request, jid: int):
        j = S(req).job(jid)
        if j and j["status"] == "running":
            raise HTTPException(409, "job is running")
        S(req).delete_job(jid)
        return {"ok": True}

    @app.get("/api/jobs/{jid}/toolpath")
    def job_toolpath(req: Request, jid: int, machine: str | None = None):
        j = S(req).job(jid)
        if not j:
            raise HTTPException(404)
        mid = machine or j["machine_id"] or (j["last_machine"] if j["status"] == "running" else None)
        ops = F(req).toolpath(j, mid)
        return preview_payload(ops, F(req).dog_for(j, mid), j["passes"])

    @app.get("/api/jobs/{jid}/gcode")
    def job_gcode(req: Request, jid: int, machine: str, slot: int = 0):
        fleet, store = F(req), S(req)
        j = store.job(jid)
        if not j:
            raise HTTPException(404)
        m = fleet.machine(machine)
        ops = fleet.toolpath(j, machine)
        lines = dialect_for(m).job(ops, m, designs.slot_offset(m, slot), j["passes"])
        head = [f"; WeinerBeamer job #{jid} {j['title']!r} for {m['name']} ({m['firmware']}), slot {slot + 1}",
                "; zero X/Y on the centre of the dog's top, Z at focus height"]
        name = f"weinerbeamer-{jid}-{machine}.gcode"
        return PlainTextResponse("\n".join(head + [ln.text for ln in lines]) + "\n",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/jobs/{jid}/image")
    def job_image(req: Request, jid: int):
        img = S(req).job_image(jid)
        if not img:
            raise HTTPException(404)
        return Response(img, media_type="application/octet-stream")

    @app.get("/api/runs")
    def runs(req: Request, limit: int = 100):
        return S(req).runs(None, limit)

    # ---- machines ----------------------------------------------------------
    @app.post("/api/machines")
    def save_machine(req: Request, cfg: dict = Body(...)):
        return F(req).save_machine(cfg)

    @app.delete("/api/machines/{mid}")
    def delete_machine(req: Request, mid: str):
        F(req).delete_machine(mid)
        return {"ok": True}

    @app.post("/api/machines/{mid}/run")
    def run(req: Request, mid: str, body: dict = Body(default={})):
        plan = F(req).start_run(mid, body.get("job_ids"), body.get("count"), bool(body.get("resume")))
        return {"plan": plan}

    @app.post("/api/machines/{mid}/{action}")
    def machine_action(req: Request, mid: str, action: str, body: dict = Body(default={})):
        fleet = F(req)
        m = fleet.machine(mid)
        c = fleet.ctl.get(mid)
        d = dialect_for(m)
        if action == "pause":
            c.pause()
        elif action == "resume":
            c.resume()
        elif action == "abort":
            c.abort()
        elif action == "estop":
            c.estop()
        elif action == "reconnect":
            fleet.reconnect(mid)
        elif action == "jog":
            fleet.jog(mid, body.get("dx", 0), body.get("dy", 0), body.get("dz", 0), body.get("feed", 1000))
        elif action == "jog_cancel":
            if hasattr(c, "jog_cancel"):
                c.jog_cancel()
        elif action == "home":
            fleet.command(mid, d.home(), timeout=120)
        elif action == "unlock":
            fleet.command(mid, d.unlock())
        elif action == "zero":
            axes = "".join(a for a in str(body.get("axes", "XY")).upper() if a in "XYZ") or "XY"
            fleet.command(mid, d.set_zero(axes))
        elif action == "goto_zero":
            fleet.command(mid, d.goto_work(0, 0))
        elif action == "goto_slot":
            x, y = designs.slot_offset(m, int(body.get("slot", 0)))
            fleet.command(mid, d.goto_work(x, y))
        elif action == "pointer":
            fleet.command(mid, d.pointer(bool(body.get("on")), m))
        elif action == "frame":
            fleet.frame(mid, body.get("job_id"), int(body.get("slot", 0)), bool(body.get("pointer")))
        elif action == "template":
            fleet.burn_template(mid)
        elif action == "mark":
            return fleet.mark_end(mid, str(body.get("end", "")))
        elif action == "center":
            return fleet.center_on_ends(mid)
        elif action == "angle":
            fleet.set_angle(mid, body.get("angle", 0))
        elif action == "console":
            line = str(body.get("line", "")).strip()
            if not line or len(line) > 120 or "\n" in line:
                raise HTTPException(400, "one line, 120 chars max")
            return {"replies": fleet.command(mid, [line])}
        else:
            raise HTTPException(404, f"unknown action {action!r}")
        return {"ok": True}

    @app.post("/api/estop")
    def estop_all(req: Request):
        F(req).estop_all()
        return {"ok": True}

    return app

