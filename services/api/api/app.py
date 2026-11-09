"""
services/api/api/app.py

FastAPI backend for the kiosk UI (checkpoint 5).

    REST  /api/...      info, crates + presets, batch commands, reports, CSV/USB export, system
    WS    /api/ws       live state / events / acks from the vision service, preset changes
    /                   the built UI (services/ui/dist), when present

Rules this follows
  * The API never decides anything about the conveyor. Every batch command goes to the vision
    service over MQTT and the API returns the vision service's own answer (ack), or a timeout.
  * It owns only the crate presets. Production records are read-only here.
  * If the API, the broker or the browser dies, counting and the run permit are unaffected.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.bus import BusUnavailable, CommandTimeout, LiveBus
from api.presets import PresetError, PresetStore
from api.reports import Reports, find_usb_mounts, write_csv_file
from api.settings import AppConfig

log = logging.getLogger("api")

API_VERSION = "1.0"

# Commands the UI may send as-is. select_batch goes through /api/batch/select (uses the preset).
PLAIN_COMMANDS = {"start", "stop", "finish_batch", "cancel_batch", "reset_fault"}
# States in which shutting down / restarting would interrupt a moving belt or an unfinished count.
BUSY_STATES = {"RUNNING", "CLEARING"}


class TargetIn(BaseModel):
    target: int = Field(..., ge=1)


class SelectIn(BaseModel):
    crate: int = Field(..., ge=1)
    target: Optional[int] = Field(None, ge=1)


class ExportIn(BaseModel):
    day: Optional[str] = None


def create_app(cfg: AppConfig, bus: Optional[LiveBus] = None) -> FastAPI:
    site = cfg.site
    api_cfg = site.api
    if bus is None:
        bus = LiveBus(site.mqtt.host, site.mqtt.port, site.mqtt.base_topic, site.mqtt.keepalive_s,
                      site.mqtt.username, site.mqtt.password, client_id=f"api-{site.site_name}".replace(" ", "-"))
    presets = PresetStore(api_cfg.settings_db, site.crates.count, site.crates.default_target, site.crates.max_target)
    reports = Reports(site.storage.db_path, site.site_name, site.timezone, site.reporting.day_start)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.start()
        log.info("API ready: site '%s', %d crates, vision db %s", site.site_name, site.crates.count,
                 site.storage.db_path)
        yield
        bus.stop()

    app = FastAPI(title="Conveyor counter API", version=API_VERSION, lifespan=lifespan)
    app.state.bus, app.state.presets, app.state.reports = bus, presets, reports

    # ───────────────────────── helpers ─────────────────────────
    def crates_payload() -> Dict[str, Any]:
        last = reports.last_completed_per_crate(reports.today())
        return {"crates": [dict(p, last=last.get(p["crate"])) for p in presets.all()]}

    def vision_state() -> Optional[str]:
        return (bus.state or {}).get("state") if bus.state_fresh() else None

    async def send(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            ack = await bus.command(name, payload, api_cfg.command_timeout_s)
        except BusUnavailable as e:
            raise HTTPException(503, str(e))
        except CommandTimeout as e:
            raise HTTPException(504, str(e))
        return {"command": name, "ok": bool(ack.get("ok")), "reason": ack.get("reason", "")}

    # ───────────────────────── info / state ─────────────────────────
    @app.get("/api/info")
    def info() -> Dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "site_name": site.site_name,
            "product": cfg.profile.product,
            "timezone": site.timezone,
            "crates": site.crates.count,
            "default_target": site.crates.default_target,
            "max_target": site.crates.max_target,
            "preview": {"enabled": site.preview.enabled, "port": site.preview.port, "host": site.preview.host},
            "system_control": api_cfg.allow_system_control,
            "frame_source": site.frame_source.type,
            "detector": site.detector.backend,
            "io": site.io.backend,
            "clear_time_s": site.safety.clear_time_s,
        }

    @app.get("/api/state")
    def state() -> Dict[str, Any]:
        return {"bus_connected": bus.connected, "live": bus.state_fresh(), "state": bus.state}

    # ───────────────────────── crates / presets ─────────────────────────
    @app.get("/api/crates")
    def crates() -> Dict[str, Any]:
        return crates_payload()

    @app.put("/api/presets")
    def set_all(body: TargetIn) -> Dict[str, Any]:
        try:
            presets.set_all(body.target)
        except PresetError as e:
            raise HTTPException(422, str(e))
        out = crates_payload()
        bus.broadcast({"type": "crates", "data": out})
        return out

    @app.put("/api/presets/{crate}")
    def set_one(crate: int, body: TargetIn) -> Dict[str, Any]:
        try:
            presets.set(crate, body.target)
        except PresetError as e:
            raise HTTPException(422, str(e))
        out = crates_payload()
        bus.broadcast({"type": "crates", "data": out})
        return out

    # ───────────────────────── batch commands ─────────────────────────
    @app.post("/api/batch/select")
    async def select(body: SelectIn) -> Dict[str, Any]:
        if body.crate > site.crates.count:
            raise HTTPException(422, f"crate must be 1..{site.crates.count}")
        target = body.target if body.target is not None else presets.get(body.crate)
        return await send("select_batch", {"crate": body.crate, "target": target})

    @app.post("/api/command/{name}")
    async def command(name: str) -> Dict[str, Any]:
        if name not in PLAIN_COMMANDS:
            raise HTTPException(404, f"unknown command '{name}'")
        return await send(name, {})

    # ───────────────────────── reports ─────────────────────────
    @app.get("/api/reports/day")
    def day_report(day: Optional[str] = Query(None)) -> Dict[str, Any]:
        try:
            d = reports.parse_day(day)
        except ValueError as e:
            raise HTTPException(422, str(e))
        out = reports.day_report(d)
        out["today"] = reports.today().isoformat()
        return out

    @app.get("/api/reports/day.csv")
    def day_csv(day: Optional[str] = Query(None)) -> Response:
        try:
            d = reports.parse_day(day)
        except ValueError as e:
            raise HTTPException(422, str(e))
        text, _ = reports.csv_text(d)
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{reports.csv_filename(d)}"'})

    @app.get("/api/usb")
    def usb() -> Dict[str, Any]:
        mounts = find_usb_mounts(api_cfg.usb_mount_roots)
        return {"mounts": [str(m) for m in mounts], "fallback_dir": str(api_cfg.export_dir)}

    @app.post("/api/reports/export")
    def export(body: ExportIn = Body(default=ExportIn())) -> Dict[str, Any]:
        try:
            d = reports.parse_day(body.day)
        except ValueError as e:
            raise HTTPException(422, str(e))
        text, rows = reports.csv_text(d)
        mounts = find_usb_mounts(api_cfg.usb_mount_roots)
        target_dir, where = (mounts[0], "usb") if mounts else (api_cfg.export_dir, "local")
        try:
            path = write_csv_file(target_dir, reports.csv_filename(d), text)
        except OSError as e:
            raise HTTPException(500, f"could not write the CSV to {target_dir}: {e}")
        log.info("exported %d batch(es) for %s to %s", rows, d, path)
        return {"ok": True, "where": where, "path": str(path), "rows": rows, "day": d.isoformat()}

    # ───────────────────────── system ─────────────────────────
    @app.post("/api/system/{action}")
    def system(action: str) -> Dict[str, Any]:
        cmds = {"shutdown": api_cfg.shutdown_cmd, "reboot": api_cfg.reboot_cmd, "restart": api_cfg.restart_cmd}
        if action not in cmds:
            raise HTTPException(404, f"unknown action '{action}'")
        if not api_cfg.allow_system_control:
            raise HTTPException(403, "system control is disabled (site.yaml: api.allow_system_control)")
        st = vision_state()
        if st in BUSY_STATES:
            raise HTTPException(409, f"conveyor is {st}: stop it and finish the batch first")
        try:
            r = subprocess.run(cmds[action], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HTTPException(500, f"{action} failed: {e}")
        if r.returncode != 0:
            raise HTTPException(500, f"{action} failed: {(r.stderr or r.stdout).strip()[:300]}")
        log.warning("system action '%s' executed", action)
        return {"ok": True, "action": action}

    # ───────────────────────── live WebSocket ─────────────────────────
    @app.websocket("/api/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        sub = bus.subscribe()
        try:
            await socket.send_json({"type": "hello", "bus": bus.connected, "server_time": time.time()})
            first = bus.state_message()
            if first:
                await socket.send_json(first)
            while True:
                try:
                    msg = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    msg = {"type": "ping", "bus": bus.connected}   # keeps proxies + the UI watchdog happy
                await socket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(sub)

    @app.exception_handler(Exception)
    async def unhandled(request, exc):  # pragma: no cover - last resort, keeps the reason visible
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": f"internal error: {exc}"}, status_code=500)

    # ───────────────────────── built UI ─────────────────────────
    dist = api_cfg.ui_dist
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
        log.info("serving UI from %s", dist)
    else:
        @app.get("/")
        def no_ui() -> Dict[str, str]:
            return {"detail": f"UI not built ({dist}). Dev: cd services/ui && npm run dev"}

    return app
