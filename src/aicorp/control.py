"""The control panel: a small local web app that drives the supervisor. ``python -m aicorp.app`` starts it.

It runs separately from the dashboard so it stays up while the dashboard restarts. Local only (127.0.0.1); every
change needs a per-run token that only the page itself receives, so another web page in the browser cannot press buttons.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import secrets
import signal
import sys
import threading
import time
from dataclasses import asdict

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config, doctor
from .control_page import PAGE
from .supervisor import Supervisor, SupervisorError


def create_control_app(sup: Supervisor, probe: doctor.Probe | None = None, on_exit=None) -> FastAPI:
    app = FastAPI(title="AICorp control panel", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    token = secrets.token_urlsafe(24)
    probe = probe or doctor.Probe(sup.home)

    def guard(request: Request) -> None:
        if request.headers.get("x-control-token") != token:
            raise HTTPException(403, "Missing or wrong control token.")
        origin = request.headers.get("origin")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            raise HTTPException(403, "Requests from other sites are refused.")

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.replace("__TOKEN__", token)

    @app.get("/api/state")
    def state() -> dict:
        return sup.state(tail=300)

    @app.post("/api/start")
    async def start(request: Request) -> JSONResponse:
        guard(request)
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        try:
            sup.start(body.get("mode"))
        except SupervisorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, **sup.state()})

    @app.post("/api/stop")
    def stop(request: Request) -> dict:
        guard(request)
        threading.Thread(target=sup.stop, daemon=True, name="stop").start()
        return {"ok": True}

    @app.post("/api/restart/{name}")
    def restart(name: str, request: Request) -> JSONResponse:
        guard(request)
        try:
            sup.restart(name)
        except SupervisorError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True})

    @app.post("/api/exit")
    def exit_app(request: Request) -> dict:
        guard(request)

        def run() -> None:
            sup.stop()
            time.sleep(1.2)                      # let every viewer receive the last event
            if on_exit:
                on_exit()
        threading.Thread(target=run, daemon=True, name="exit").start()
        return {"ok": True}

    @app.get("/api/doctor")
    def doctor_report() -> dict:
        results = doctor.run_checks(probe)
        return {"summary": doctor.summary(results), "checks": [asdict(r) for r in results],
                "prompt": doctor.help_prompt(results, probe),
                "prompts": {r.id: doctor.help_prompt(results, probe, only=r.id) for r in results if r.status != doctor.OK}}

    @app.post("/api/doctor/fix/{fix_id}")
    def doctor_fix(fix_id: str, request: Request) -> JSONResponse:
        guard(request)
        if fix_id not in doctor.FIXES:
            raise HTTPException(404, "Unknown fix.")
        ok, message = doctor.apply_fix(fix_id, probe)
        return JSONResponse({"ok": ok, "message": message})

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        q = sup.subscribe()

        async def events():
            try:
                yield "retry: 1500\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.get_running_loop().run_in_executor(None, lambda: q.get(timeout=10))
                    except queue.Empty:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                sup.unsubscribe(q)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def serve(app: FastAPI, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="control-server")
    thread.start()
    return server, thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aicorp.app", description="AICorp control panel (local only)")
    parser.add_argument("--mode", choices=["fake", "claude"], default="fake", help="fake = simulated work, claude = real Claude Code")
    parser.add_argument("--control-port", type=int, default=8760)
    parser.add_argument("--no-autostart", action="store_true", help="open the panel but do not start the programs")
    parser.add_argument("--stdin-guard", action="store_true",
                        help="shut everything down when standard input closes (the desktop app uses this so nothing is left running if it dies)")
    args = parser.parse_args(argv)
    home = config.ensure_layout()
    sup = Supervisor(home, mode=args.mode)
    done = threading.Event()
    app = create_control_app(sup, on_exit=done.set)
    port = doctor.find_free_port(doctor.Probe(home), args.control_port)
    if not args.no_autostart:
        try:
            sup.start()
        except SupervisorError as exc:
            print("Not started:", exc)
    server, _ = serve(app, port)
    print(f"AICorp control panel: http://127.0.0.1:{port}/   dashboard: {sup.dashboard_url()}  (Ctrl+C = shut everything down)")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    if args.stdin_guard:
        def watch_parent() -> None:
            try:
                while sys.stdin.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            done.set()
        threading.Thread(target=watch_parent, daemon=True, name="stdin-guard").start()
    done.wait()
    if not sup.shutting_down:
        sup.stop()
    server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
