"""AICorp dashboard (server-rendered, local use only)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.middleware.trustedhost import TrustedHostMiddleware


from .. import advisor, browser_runner, limits, maintenance, skills, trash, usage, attention, channels, chat, events, impersonation, killswitch, notify, org, projects, redact, scheduler, takeover, tasks
from ..delivery import Notifier
from ..watchdog import watchdog_state
from ..config import db_path, home as default_home, utc_now
from ..db import connect, migrate
from ..live import engine_state, public_event, snapshot
from ..search import search_events

PKG = Path(__file__).resolve().parent
CSRF_COOKIE = "aicorp_csrf"
PAGE_SIZE = 50
NAV = [
    ("/", "Command Center"), ("/employees", "Employees"), ("/teams", "Teams"), ("/roles", "Roles"),
    ("/projects", "Projects"), ("/meetings", "Meetings"), ("/tasks", "Tasks"), ("/advisor", "Advisor"),
    ("/attention", "Attention"), ("/inbox", "Inbox"), ("/logs", "Logs"),
    ("/approvals", "Approvals"), ("/skills", "Skills"), ("/usage", "Usage"), ("/settings", "Settings"),
]


def allowed_hosts() -> list[str]:
    extra = [h.strip() for h in os.environ.get("AICORP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return ["localhost", "127.0.0.1", "testserver", *extra]


def _mark(text: object) -> Markup:
    """Escape text, then turn the search-match sentinels into <mark> tags."""
    return Markup(str(escape(text or "")).replace("\x02", "<mark>").replace("\x03", "</mark>"))


def _short_ts(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return ts[:19].replace("T", " ") + " UTC"


IMP_COOKIE = "aicorp_imp"


class ActingMiddleware:
    """While an impersonation session is open (cookie), every event the Founder writes carries both identities."""

    def __init__(self, app, home: Path) -> None:
        self.app, self.home = app, home

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cookie = ""
        for key, value in scope.get("headers", []):
            if key == b"cookie":
                for part in value.decode("latin-1").split(";"):
                    name, _, val = part.strip().partition("=")
                    if name == IMP_COOKIE:
                        cookie = val
        token = None
        if cookie.startswith("IMP-"):
            con = connect(db_path(self.home))
            try:
                row = impersonation.get(con, cookie)
            finally:
                con.close()
            if row and row["by_type"] == "founder":            # the dashboard is the Founder's; a CEO session is never taken from a cookie
                token = events.set_acting(impersonation.session_for(row))
        try:
            return await self.app(scope, receive, send)
        finally:
            if token is not None:
                events.reset_acting(token)


def create_app(base: Path | None = None) -> FastAPI:
    base = (base or default_home()).resolve()
    app = FastAPI(title="AICorp", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.home = base
    app.add_middleware(ActingMiddleware, home=base)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())
    app.mount("/static", StaticFiles(directory=PKG / "static"), name="static")

    templates = Jinja2Templates(directory=PKG / "templates")
    templates.env.filters["mark"] = _mark
    templates.env.filters["ts"] = _short_ts

    from ..config import ensure_layout
    ensure_layout(base)
    redact.install(base)
    app.state.inboxes = None               # tests may place their own inbound channels here
    app.state.transports = None            # tests may place their own channels here
    boot = connect(db_path(base))
    try:
        migrate(boot)
        org.seed_system(boot, base)
        org.expire_temporary(boot, base)
    finally:
        boot.close()

    def get_con(request: Request):
        con = connect(db_path(request.app.state.home), cross_thread=True)
        try:
            yield con
        finally:
            con.close()

    def render(request: Request, name: str, code: int = 200, **ctx):
        token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        probe = connect(db_path(request.app.state.home))
        try:
            engine = engine_state(probe)
            unread = notify.unread_count(probe)
            watchdog = watchdog_state(probe)
            imp = impersonation.get(probe, request.cookies.get(IMP_COOKIE))
            imp = imp if imp and imp["by_type"] == "founder" else None
        finally:
            probe.close()
        ctx.update(imp=imp, csrf=token, nav=NAV, path=request.url.path, engine=engine, inbox_unread=unread, watchdog=watchdog,
                   kill=killswitch.info(request.app.state.home),
                   ok=request.query_params.get("ok"), err_flash=request.query_params.get("err"))
        response = templates.TemplateResponse(request, name, ctx, status_code=code)
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="strict")
        return response

    async def read_form(request: Request):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in allowed_hosts():
            raise HTTPException(403, "Cross-site request refused")
        form = await request.form()
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not cookie or not hmac.compare_digest(cookie, str(form.get("csrf_token", ""))):
            raise HTTPException(403, "Invalid or missing CSRF token")
        return form

    def done(path: str, message: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?ok={quote(message)}", status_code=303)

    # ------------------------------------------------------------ pages

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok"})

    @app.get("/")
    def index(request: Request):
        return render(request, "command.html")

    @app.get("/api/live")
    def api_live(request: Request, con=Depends(get_con)):
        return JSONResponse(snapshot(con, request.app.state.home), headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ employees

    def employee_values(emp: dict | None) -> dict:
        if emp is None:
            return {"name": "", "kind": "permanent", "reports_to": "", "role_ids": set(), "team_ids": set(),
                    "skills": "", "tools": "", "execute": False, "assign": False, "instructions": "",
                    "notes": "", "expires_at": "", "model": "", "max_turns": "", "timeout": ""}
        return {"name": emp["name"], "kind": emp["kind"], "reports_to": emp["reports_to"] or "",
                "role_ids": {r["role_id"] for r in emp["roles"]}, "team_ids": {t["team_id"] for t in emp["teams"]},
                "skills": ", ".join(emp["skills"]), "tools": ", ".join(emp["profile"].get("tools", [])),
                "execute": bool(emp["profile"].get("capabilities", {}).get("execute")),
                "assign": bool(emp["profile"].get("capabilities", {}).get("assign")),
                "instructions": emp["instructions"], "notes": emp["notes"] or "",
                "expires_at": org.to_local_input(emp["expires_at"]), "model": emp["profile"].get("model", ""),
                "max_turns": emp["profile"].get("max_turns", ""), "timeout": emp["profile"].get("timeout", "")}

    def employee_form_data(form) -> dict:
        return {"name": form.get("name"), "kind": form.get("kind"), "reports_to": form.get("reports_to"),
                "role_ids": form.getlist("role_ids"), "team_ids": form.getlist("team_ids"),
                "skills": form.get("skills"), "tools": form.get("tools"),
                "execute": form.get("execute") == "on", "assign": form.get("assign") == "on",
                "instructions": form.get("instructions"), "notes": form.get("notes"),
                "expires_at": form.get("expires_at"), "model": form.get("model"), "max_turns": form.get("max_turns"),
                "timeout": form.get("timeout")}

    def employee_page(request, con, emp, values, errors, status=200):
        managers = [e for e in org.list_employees(con) if not emp or e["emp_id"] != emp["emp_id"]]
        return render(request, "employee_form.html", code=status, emp=emp, f=values, errors=errors,
                      roles=org.list_roles(con), teams=org.list_teams(con), managers=managers)

    @app.get("/employees")
    def employees(request: Request, con=Depends(get_con), q: str = "", kind: str = "", status: str = "",
                  team: str = "", deleted: str = ""):
        org.expire_temporary(con, request.app.state.home)
        rows = org.list_employees(con, q=q or None, kind=kind or None, status=status or None,
                                  team_id=team or None, include_deleted=bool(deleted))
        return render(request, "employees.html", rows=rows, teams=org.list_teams(con),
                      q=q, kind=kind, status=status, team=team, deleted=deleted)

    @app.get("/employees/new")
    def employee_new(request: Request, con=Depends(get_con)):
        return employee_page(request, con, None, employee_values(None), [])

    @app.post("/employees")
    async def employee_create(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        data = employee_form_data(form)
        try:
            emp_id = org.create_employee(con, request.app.state.home, data)
        except org.ValidationError as err:
            values = {**employee_values(None), **data, "role_ids": set(data["role_ids"]),
                      "team_ids": set(data["team_ids"]), "skills": data["skills"] or "", "tools": data["tools"] or ""}
            return employee_page(request, con, None, values, err.errors, 400)
        return done(f"/employees/{emp_id}", "Employee created")

    @app.get("/employees/{emp_id}")
    def employee_detail(request: Request, emp_id: str, con=Depends(get_con)):
        emp = org.get_employee(con, emp_id)
        if emp is None:
            raise HTTPException(404)
        history = search_events(con, emp_id, limit=10)
        return render(request, "employee_form.html", emp=emp, f=employee_values(emp), errors=[],
                      roles=org.list_roles(con), teams=org.list_teams(con), history=history,
                      managers=[e for e in org.list_employees(con) if e["emp_id"] != emp_id])

    @app.post("/employees/{emp_id}")
    async def employee_update(request: Request, emp_id: str, con=Depends(get_con)):
        form = await read_form(request)
        data = employee_form_data(form)
        emp = org.get_employee(con, emp_id)
        if emp is None:
            raise HTTPException(404)
        try:
            org.update_employee(con, request.app.state.home, emp_id, data)
        except org.ValidationError as err:
            values = {**employee_values(emp), **data, "role_ids": set(data["role_ids"]),
                      "team_ids": set(data["team_ids"]), "skills": data["skills"] or "", "tools": data["tools"] or ""}
            return employee_page(request, con, emp, values, err.errors, 400)
        return done(f"/employees/{emp_id}", "Saved")

    def employee_action(action):
        async def handler(request: Request, emp_id: str, con=Depends(get_con)):
            await read_form(request)
            try:
                action(con, request.app.state.home, emp_id)
            except org.ValidationError as err:
                return RedirectResponse(f"/employees/{emp_id}?err={quote('; '.join(err.errors))}", status_code=303)
            return done("/employees", "Done")
        return handler

    app.post("/employees/{emp_id}/archive")(employee_action(org.archive_employee))
    app.post("/employees/{emp_id}/restore")(employee_action(org.restore_employee))
    app.post("/employees/{emp_id}/delete")(employee_action(org.delete_employee))

    # ------------------------------------------------------------ roles

    def role_form_data(form) -> dict:
        return {"name": form.get("name"), "description": form.get("description"), "skills": form.get("skills"),
                "tools": form.get("tools"), "execute": form.get("execute") == "on", "assign": form.get("assign") == "on"}

    def role_values(role: dict | None) -> dict:
        if role is None:
            return {"name": "", "description": "", "skills": "", "tools": "", "execute": False, "assign": False}
        return {"name": role["name"], "description": role["description"] or "", "skills": ", ".join(role["skills"]),
                "tools": ", ".join(role["tools"]), "execute": bool(role["capabilities"].get("execute")),
                "assign": bool(role["capabilities"].get("assign"))}

    @app.get("/roles")
    def roles(request: Request, con=Depends(get_con)):
        return render(request, "roles.html", rows=org.list_roles(con), system_roles=org.SYSTEM_ROLES)

    @app.get("/roles/new")
    def role_new(request: Request):
        return render(request, "role_form.html", role=None, f=role_values(None), errors=[])

    @app.post("/roles")
    async def role_create(request: Request, con=Depends(get_con)):
        data = role_form_data(await read_form(request))
        try:
            role_id = org.create_role(con, data)
        except org.ValidationError as err:
            return render(request, "role_form.html", code=400, role=None, f=data, errors=err.errors)
        return done(f"/roles/{role_id}", "Role created")

    @app.get("/roles/{role_id}")
    def role_detail(request: Request, role_id: str, con=Depends(get_con)):
        role = org.get_role(con, role_id)
        if role is None:
            raise HTTPException(404)
        return render(request, "role_form.html", role=role, f=role_values(role), errors=[],
                      system_roles=org.SYSTEM_ROLES)

    @app.post("/roles/{role_id}")
    async def role_update(request: Request, role_id: str, con=Depends(get_con)):
        data = role_form_data(await read_form(request))
        role = org.get_role(con, role_id)
        if role is None:
            raise HTTPException(404)
        try:
            org.update_role(con, role_id, data)
        except org.ValidationError as err:
            return render(request, "role_form.html", code=400, role=role, f=data, errors=err.errors,
                          system_roles=org.SYSTEM_ROLES)
        return done(f"/roles/{role_id}", "Saved")

    @app.post("/roles/{role_id}/delete")
    async def role_delete(request: Request, role_id: str, con=Depends(get_con)):
        await read_form(request)
        try:
            org.delete_role(con, role_id)
        except org.ValidationError as err:
            return RedirectResponse(f"/roles/{role_id}?err={quote('; '.join(err.errors))}", status_code=303)
        return done("/roles", "Role deleted")

    # ------------------------------------------------------------ teams

    def team_form_data(form) -> dict:
        return {"name": form.get("name"), "description": form.get("description"),
                "head_emp_id": form.get("head_emp_id"), "member_ids": form.getlist("member_ids")}

    def team_page(request, con, team, values, errors, status=200):
        return render(request, "team_form.html", code=status, team=team, f=values, errors=errors,
                      people=org.list_employees(con))

    @app.get("/teams")
    def teams(request: Request, con=Depends(get_con)):
        return render(request, "teams.html", rows=org.list_teams(con))

    @app.get("/teams/new")
    def team_new(request: Request, con=Depends(get_con)):
        return team_page(request, con, None, {"name": "", "description": "", "head_emp_id": "", "member_ids": []}, [])

    @app.post("/teams")
    async def team_create(request: Request, con=Depends(get_con)):
        data = team_form_data(await read_form(request))
        try:
            team_id = org.create_team(con, data)
        except org.ValidationError as err:
            return team_page(request, con, None, data, err.errors, 400)
        return done(f"/teams/{team_id}", "Team created")

    @app.get("/teams/{team_id}")
    def team_detail(request: Request, team_id: str, con=Depends(get_con)):
        team = org.get_team(con, team_id)
        if team is None or team["deleted_at"]:
            raise HTTPException(404)
        values = {"name": team["name"], "description": team["description"] or "",
                  "head_emp_id": team["head_emp_id"] or "", "member_ids": [m["emp_id"] for m in team["members"]]}
        return team_page(request, con, team, values, [])

    @app.post("/teams/{team_id}")
    async def team_update(request: Request, team_id: str, con=Depends(get_con)):
        data = team_form_data(await read_form(request))
        team = org.get_team(con, team_id)
        if team is None:
            raise HTTPException(404)
        try:
            org.update_team(con, team_id, data)
        except org.ValidationError as err:
            return team_page(request, con, team, data, err.errors, 400)
        return done(f"/teams/{team_id}", "Saved")

    @app.post("/teams/{team_id}/delete")
    async def team_delete(request: Request, team_id: str, con=Depends(get_con)):
        await read_form(request)
        try:
            org.delete_team(con, team_id)
        except org.ValidationError as err:
            return RedirectResponse(f"/teams/{team_id}?err={quote('; '.join(err.errors))}", status_code=303)
        return done("/teams", "Team deleted")

    # ------------------------------------------------------------ tasks

    def task_form_data(form) -> dict:
        return {"title": form.get("title"), "description": form.get("description") or "",
                "owner": (form.get("owner") or "").strip() or None,
                "parent_id": (form.get("parent_id") or "").strip() or None,
                "depends_on": form.getlist("depends_on"), "priority": form.get("priority") or 3,
                "risk_tier": form.get("risk_tier") or 1, "due_at": form.get("due_at")}

    def task_form_page(request, con, values, errors, code=200):
        open_tasks = [t for t in tasks.list_tasks(con, limit=300) if t["status"] not in tasks.TERMINAL]
        return render(request, "task_form.html", code=code, f=values, errors=errors, people=org.list_employees(con),
                      open_tasks=open_tasks)

    @app.get("/tasks")
    def task_list(request: Request, con=Depends(get_con), status: str = "", owner: str = "", q: str = ""):
        rows = tasks.list_tasks(con, status=status or None, owner=owner or None, q=q or None)
        return render(request, "tasks.html", rows=rows, status=status, owner=owner, q=q,
                      people=org.list_employees(con), statuses=tasks.STATUSES)

    @app.get("/tasks/new")
    def task_new(request: Request, con=Depends(get_con)):
        return task_form_page(request, con, {"title": "", "description": "", "owner": None, "parent_id": None,
                                             "depends_on": [], "priority": 3, "risk_tier": 1, "due_at": ""}, [])

    @app.post("/tasks")
    async def task_create(request: Request, con=Depends(get_con)):
        data = task_form_data(await read_form(request))
        try:
            task_id = tasks.create_task(con, **data)
        except PermissionError as err:
            return task_form_page(request, con, data, [str(err)], 403)
        except (org.ValidationError, ValueError) as err:
            errors = err.errors if isinstance(err, org.ValidationError) else ["Priority and risk tier must be numbers"]
            return task_form_page(request, con, data, errors, 400)
        return done(f"/tasks/{task_id}", "Task created")

    @app.get("/tasks/{task_id}")
    def task_detail(request: Request, task_id: str, con=Depends(get_con)):
        task = tasks.get_task(con, task_id)
        if task is None:
            raise HTTPException(404)
        parent = tasks.get_task(con, task["parent_id"]) if task["parent_id"] else None
        challenge = advisor.challenge_note(con, task_id) if task["status"] in ("review", "awaiting_approval") else []
        return render(request, "task_detail.html", task=task, parent=parent, history=tasks.timeline(con, task_id),
                      people=org.list_employees(con), transitions=tasks.TRANSITIONS, challenge=challenge)

    def safe_next(value, default: str) -> str:
        text = str(value or "")
        return text if re.fullmatch(r"/(attention|inbox(/\d+)?|tasks(/T-[0-9a-f]+)?|advisor(/review)?(\?[\w=&.%-]*)?|projects(/P-[0-9a-f]+)?|meetings(/M-[0-9a-f]+)?|approvals|skills(/[a-z0-9._-]+)?)", text) else default

    def task_action(fn):
        async def handler(request: Request, task_id: str, con=Depends(get_con)):
            form = await read_form(request)
            target = safe_next(form.get("next"), f"/tasks/{task_id}")
            try:
                fn(con, task_id, form)
            except (org.ValidationError, PermissionError) as err:
                message = "; ".join(err.errors) if isinstance(err, org.ValidationError) else str(err)
                return RedirectResponse(f"{target}?err={quote(message)}", status_code=303)
            attention.sync(con, request.app.state.home)
            return done(target, "Done")
        return handler

    app.post("/tasks/{task_id}/assign")(task_action(lambda c, t, f: tasks.assign_task(c, t, f.get("owner") or "")))
    app.post("/tasks/{task_id}/resolve")(task_action(lambda c, t, f: tasks.resolve_block(c, t, note=f.get("note") or "")))
    app.post("/tasks/{task_id}/retry")(task_action(lambda c, t, f: tasks.retry_failed(c, t)))
    app.post("/tasks/{task_id}/cancel")(task_action(lambda c, t, f: tasks.cancel_task(c, t, reason=f.get("note") or "")))
    app.post("/tasks/{task_id}/decide")(task_action(
        lambda c, t, f: tasks.decide_approval(c, t, f.get("decision") or "", f.get("note") or "")))

    # ------------------------------------------------------------ impersonation and take-over

    @app.get("/impersonate")
    def impersonate_page(request: Request, con=Depends(get_con), emp: str = ""):
        people = [e for e in org.list_employees(con) if e.get("status") != "archived"]
        return render(request, "impersonate.html", people=people, preselect=emp, history=impersonation.history(con, 20))

    @app.post("/impersonate/start")
    async def impersonate_start(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        override = form.get("override") == "on"
        try:
            imp_id = impersonation.begin(con, org.FOUNDER, str(form.get("employee") or ""), override=override,
                                         confirm=override and str(form.get("confirm") or "").strip() == "OVERRIDE",
                                         reason=str(form.get("reason") or ""))
        except (org.ValidationError, PermissionError) as err:
            message = "; ".join(err.errors) if isinstance(err, org.ValidationError) else str(err)
            return RedirectResponse(f"/impersonate?err={quote(message)}", status_code=303)
        response = done("/", "You are now acting as someone else - the banner at the top shows who")
        response.set_cookie(IMP_COOKIE, imp_id, httponly=True, samesite="strict")
        return response

    @app.post("/impersonate/stop")
    async def impersonate_stop(request: Request, con=Depends(get_con)):
        await read_form(request)
        impersonation.end(con, request.cookies.get(IMP_COOKIE, ""), org.FOUNDER, "stopped by the Founder")
        response = done("/impersonate", "You are acting as yourself again")
        response.delete_cookie(IMP_COOKIE)
        return response

    @app.get("/tasks/{task_id}/takeover")
    def takeover_page(request: Request, task_id: str, con=Depends(get_con)):
        task = tasks.get_task(con, task_id)
        if task is None or task["deleted_at"]:
            raise HTTPException(404)
        return render(request, "takeover.html", task=task, to=takeover.open_for_task(con, task_id),
                      workspace=takeover.workspace_of(con, request.app.state.home, task["owner_emp_id"]).relative_to(request.app.state.home).as_posix()
                      if task["owner_emp_id"] else "")

    def takeover_action(fn, back_to_task: bool = False):
        async def handler(request: Request, ident: str, con=Depends(get_con)):
            form = await read_form(request)
            try:
                target = fn(con, request.app.state.home, ident, form)
            except (org.ValidationError, PermissionError) as err:
                message = "; ".join(err.errors) if isinstance(err, org.ValidationError) else str(err)
                task_id = (con.execute("SELECT task_id FROM takeover WHERE takeover_id = ?", (ident,)).fetchone() or [ident])[0]
                return RedirectResponse(f"/tasks/{task_id}/takeover?err={quote(message)}", status_code=303)
            attention.sync(con, request.app.state.home)
            return done(target, "Done")
        return handler

    def _task_of(con, takeover_id):
        return con.execute("SELECT task_id FROM takeover WHERE takeover_id = ?", (takeover_id,)).fetchone()[0]

    @app.post("/tasks/{task_id}/takeover/start")
    async def takeover_start(request: Request, task_id: str, con=Depends(get_con)):
        await read_form(request)
        try:
            takeover.start(con, request.app.state.home, task_id)
        except (org.ValidationError, PermissionError) as err:
            message = "; ".join(err.errors) if isinstance(err, org.ValidationError) else str(err)
            return RedirectResponse(f"/tasks/{task_id}/takeover?err={quote(message)}", status_code=303)
        return done(f"/tasks/{task_id}/takeover", "Interrupt sent - the agent is stopping")

    app.post("/takeover/{ident}/note")(takeover_action(lambda c, h, i, f: (takeover.add_note(c, i, f.get("text") or ""), f"/tasks/{_task_of(c, i)}/takeover")[1]))
    app.post("/takeover/{ident}/file")(takeover_action(lambda c, h, i, f: (takeover.write_file(c, h, i, f.get("path") or "", f.get("content") or ""), f"/tasks/{_task_of(c, i)}/takeover")[1]))
    app.post("/takeover/{ident}/end")(takeover_action(lambda c, h, i, f: (takeover.end(c, i, f.get("action") or "", result=f.get("result") or ""), f"/tasks/{_task_of(c, i)}")[1]))

    # ------------------------------------------------------------ approvals, skills, usage

    @app.get("/approvals")
    def approvals_page(request: Request, con=Depends(get_con)):
        pending = []
        for row in con.execute("SELECT t.task_id FROM task t JOIN approval a ON a.task_id = t.task_id AND a.decided_at IS NULL "
                               "WHERE t.status = 'awaiting_approval' AND t.deleted_at IS NULL ORDER BY a.requested_at"):
            task = tasks.get_task(con, row["task_id"])
            task["artifacts"] = [dict(a) for a in con.execute("SELECT path, created_at FROM artifact WHERE task_id = ? ORDER BY created_at", (row["task_id"],))]
            task["challenge"] = advisor.challenge_note(con, row["task_id"])
            pending.append(task)
        critical = [tasks.get_task(con, r["task_id"]) for r in con.execute(
            "SELECT task_id FROM task WHERE risk_tier = 4 AND deleted_at IS NULL AND status NOT IN ('done', 'rejected') ORDER BY created_at")]
        history = [dict(r) for r in con.execute(
            "SELECT a.*, t.title FROM approval a JOIN task t ON t.task_id = a.task_id WHERE a.decided_at IS NOT NULL ORDER BY a.decided_at DESC LIMIT 30")]
        return render(request, "approvals.html", pending=pending, critical=critical, history=history)

    @app.get("/skills")
    def skills_page(request: Request, con=Depends(get_con)):
        return render(request, "skills.html", rows=skills.list_skills(request.app.state.home, con))

    @app.get("/skills/new")
    def skill_new(request: Request):
        return render(request, "skill_form.html", s=None, f={"name": "", "description": "", "version": "1", "source": "own", "license": "", "employees": "", "keywords": "", "body": ""})

    @app.get("/skills/{name}")
    def skill_detail(request: Request, name: str, con=Depends(get_con)):
        info = skills.describe(request.app.state.home, con, name)
        if info is None:
            raise HTTPException(404)
        users = [r["name"] for r in con.execute("SELECT name, skills FROM employee WHERE deleted_at IS NULL") if name in json.loads(r["skills"] or "[]")]
        return render(request, "skill_detail.html", s=info, items=skills.checklist(info), users=users)

    @app.post("/skills")
    async def skill_save(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        data = {k: form.get(k) for k in ("name", "description", "version", "source", "license", "employees", "keywords", "body")}
        try:
            name = skills.save(request.app.state.home, con, data)
        except (org.ValidationError, PermissionError) as err:
            return render(request, "skill_form.html", code=400, s=None, f={k: v or "" for k, v in data.items()},
                          errors=err.errors if isinstance(err, org.ValidationError) else [str(err)])
        return done(f"/skills/{name}", "Skill saved" + ("" if (data.get("source") or "own").strip().lower() == "own" else " - not vetted yet, so no agent will see it"))

    def skill_action(fn):
        async def handler(request: Request, name: str, con=Depends(get_con)):
            form = await read_form(request)
            try:
                target = fn(request.app.state.home, con, name, form)
            except (org.ValidationError, PermissionError) as err:
                message = "; ".join(err.errors) if isinstance(err, org.ValidationError) else str(err)
                return RedirectResponse(f"/skills/{quote(name)}?err={quote(message)}", status_code=303)
            return done(target, "Done")
        return handler

    app.post("/skills/{name}/vet")(skill_action(lambda h, c, n, f: (skills.vet(h, c, n, set(f.getlist("confirm")), f.get("note") or ""), f"/skills/{n}")[1]))
    app.post("/skills/{name}/reject")(skill_action(lambda h, c, n, f: (skills.reject(h, c, n, f.get("note") or ""), f"/skills/{n}")[1]))
    app.post("/skills/{name}/remove")(skill_action(lambda h, c, n, f: (skills.remove(h, c, n), "/skills")[1]))

    @app.get("/usage")
    def usage_page(request: Request, con=Depends(get_con), days: int = 30):
        days = days if days in (7, 30, 90) else 30
        return render(request, "usage.html", u=usage.summary(con, days), tiers=limits.tier_settings(con), models=limits.ALLOWED_MODELS,
                      bounds={"turns": limits.TURNS, "timeout": limits.TIMEOUT}, cpt=usage.CHARS_PER_TOKEN)

    @app.post("/usage/limits")
    async def usage_limits(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        saved = limits.save_tier_settings(con, dict(form))
        events.append(con, type="settings.updated", summary="Run limits changed", payload={"limits": saved}, **org.FOUNDER)
        return done("/usage", "Run limits saved (they apply to runs that start from now on)")

    # ------------------------------------------------------------ maintenance

    @app.get("/maintenance")
    def maintenance_page(request: Request, con=Depends(get_con)):
        home = request.app.state.home
        return render(request, "maintenance.html", sizes=maintenance.sizes(con, home), last=notify.get_setting(con, "maintenance_last"),
                      trash=sorted(trash.list_trash(home), key=lambda e: e["expires_at"]), now=datetime.now(timezone.utc).isoformat())

    @app.post("/maintenance/run")
    async def maintenance_run(request: Request, con=Depends(get_con)):
        await read_form(request)
        report = maintenance.run(con, request.app.state.home)
        return done("/maintenance", "Maintenance finished: " + ("; ".join(report["problems"] + report["did"]) or "all fine"))

    @app.post("/maintenance/trash/restore")
    async def maintenance_restore(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        try:
            dest = trash.restore(request.app.state.home, str(form.get("item") or ""))
        except (KeyError, FileExistsError) as err:
            return RedirectResponse(f"/maintenance?err={quote('Cannot restore: ' + ('something is already at the original place' if isinstance(err, FileExistsError) else 'item not found'))}", status_code=303)
        events.append(con, type="trash.restored", summary=f"Restored {dest.name} from the trash", **org.FOUNDER)
        return done("/maintenance", f"Restored {dest.name}")

    @app.post("/maintenance/trash/purge")
    async def maintenance_purge(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        if form.get("confirm") != "on":
            return RedirectResponse(f"/maintenance?err={quote('Tick the box to confirm: purging deletes permanently')}", status_code=303)
        gone = trash.purge_expired(request.app.state.home, confirm=True)
        events.append(con, type="trash.purged", summary=f"Permanently deleted {len(gone)} expired trash item(s)", payload={"items": gone[:50]}, **org.FOUNDER)
        return done("/maintenance", f"Permanently deleted {len(gone)} expired item(s)")

    @app.post("/killswitch/{action}")
    async def kill_switch(request: Request, action: str, con=Depends(get_con)):
        form = await read_form(request)
        if action == "engage":
            killswitch.engage(con, request.app.state.home, org.FOUNDER, str(form.get("reason") or "")[:200])
            return done("/tasks", "Kill switch engaged: all workers are stopping")
        if action == "release":
            killswitch.release(con, request.app.state.home, org.FOUNDER)
            return done("/tasks", "Kill switch released")
        raise HTTPException(404)

    # ------------------------------------------------------------ attention, inbox, settings

    SNOOZE = {"1h": 3600, "4h": 4 * 3600, "1d": 86400, "3d": 3 * 86400}
    INBOX_PAGE = 30

    def inbox_filters(category="", severity="", state="all", q="", since="", until="") -> dict:
        return {"category": category if category in notify.CATEGORIES else "", "severity": severity if severity in notify.SEVERITIES else "",
                "state": state if state in notify.STATES else "all", "q": q[:100], "since": since, "until": until}

    def day(value: str, end: bool = False) -> str | None:
        return f"{value}T{'23:59:59.999999' if end else '00:00:00'}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "") else None

    @app.get("/attention")
    def attention_page(request: Request, con=Depends(get_con)):
        attention.sync(con, request.app.state.home)
        return render(request, "attention.html", items=attention.worklist(con, request.app.state.home))

    @app.get("/inbox")
    def inbox_page(request: Request, con=Depends(get_con), category: str = "", severity: str = "", state: str = "all",
                   q: str = "", since: str = "", until: str = "", page: int = 1):
        attention.sync(con, request.app.state.home)
        f = inbox_filters(category, severity, state, q, since, until)
        page = max(page, 1)
        rows = notify.list_notifications(con, category=f["category"] or None, severity=f["severity"] or None, state=f["state"],
                                         q=f["q"], since=day(f["since"]), until=day(f["until"], True),
                                         limit=INBOX_PAGE + 1, offset=(page - 1) * INBOX_PAGE)
        def page_url(n: int) -> str:
            return "/inbox?" + urlencode({**{k: v for k, v in f.items() if v and not (k == "state" and v == "all")}, "page": n})
        return render(request, "inbox.html", rows=rows[:INBOX_PAGE], more=len(rows) > INBOX_PAGE, page=page, f=f,
                      categories=notify.CATEGORIES, severities=notify.SEVERITIES, states=notify.STATES,
                      newer_url=page_url(page - 1), older_url=page_url(page + 1))

    @app.post("/inbox/action")
    async def inbox_action(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        action = str(form.get("action") or "")
        f = inbox_filters(*(str(form.get(f"f_{k}") or "") for k in ("category", "severity", "state", "q", "since", "until")))
        ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
        target = safe_next(form.get("next"), "/inbox")
        query = urlencode({k: v for k, v in f.items() if v and not (k == "state" and v == "all")})
        back = target + (f"?{query}" if query and target == "/inbox" else "")
        sep = "&" if "?" in back else "?"
        if action == "read_all":
            state = "unread" if f["state"] == "all" else f["state"]
            ids = notify.ids_matching(con, category=f["category"] or None, severity=f["severity"] or None, state=state, q=f["q"],
                                      since=day(f["since"]), until=day(f["until"], True))
            action = "read"
        if not ids:
            return RedirectResponse(f"{back}{sep}err={quote('Nothing selected')}", status_code=303)
        actions = {"read": lambda: notify.mark_read(con, ids, True), "unread": lambda: notify.mark_read(con, ids, False),
                   "snooze": lambda: notify.snooze(con, ids, SNOOZE.get(str(form.get("snooze_for")), 3600)),
                   "unsnooze": lambda: notify.unsnooze(con, ids), "pin": lambda: notify.pin(con, ids, True),
                   "unpin": lambda: notify.pin(con, ids, False), "ack": lambda: notify.acknowledge(con, ids)}
        if action not in actions:
            raise HTTPException(400, "Unknown action")
        count = actions[action]()
        return RedirectResponse(f"{back}{sep}ok={quote(f'{count} notification(s) updated')}", status_code=303)

    @app.get("/inbox/{notification_id}")
    def inbox_detail(request: Request, notification_id: int, con=Depends(get_con)):
        item = notify.get(con, notification_id)
        if item is None:
            raise HTTPException(404)
        con.execute("UPDATE notification SET read_at = COALESCE(read_at, ?) WHERE id = ?", (utc_now(), notification_id))
        related = tasks.timeline(con, item["task_id"], 15) if item["task_id"] else []
        return render(request, "inbox_detail.html", item=item, related=related, snooze_options=list(SNOOZE))

    @app.get("/api/notifications/summary")
    def api_notification_summary(con=Depends(get_con)):
        now = utc_now()
        total = notify.unread_count(con, now)
        critical = len([r for r in notify.list_notifications(con, severity="critical", state="unread", limit=200, now=now)])
        return JSONResponse({"unread": total, "critical": critical}, headers={"Cache-Control": "no-store"})

    @app.get("/api/notifications")
    def api_notifications(con=Depends(get_con), state: str = "unread", limit: int = 6):
        rows = notify.list_notifications(con, state=state if state in notify.STATES else "unread", limit=min(max(limit, 1), 50))
        return JSONResponse([{"id": r["id"], "severity": r["severity"], "category": r["category"], "title": r["title"],
                              "created_at": r["created_at"], "link": f"/inbox/{r['id']}", "read": bool(r["read_at"])} for r in rows],
                            headers={"Cache-Control": "no-store"})

    def settings_context(con, request) -> dict:
        return {"status": channels.status(request.app.state.home), "routing": notify.get_setting(con, "routing"),
                "rate": notify.get_setting(con, "rate_per_hour"), "url": notify.get_setting(con, "dashboard_url"),
                "down_after": notify.get_setting(con, "engine_down_after"), "severities": notify.SEVERITIES,
                "channel_names": ("dashboard", "telegram", "email"),
                "chat": {c: chat.channel_status(con, c) for c in ("telegram", "email")},
                "browser": browser_runner.status(request.app.state.home)}

    @app.get("/settings")
    def settings_page(request: Request, con=Depends(get_con)):
        return render(request, "settings.html", **settings_context(con, request))

    @app.post("/settings/routing")
    async def settings_routing(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        routing = {sev: ["dashboard"] + [c for c in ("telegram", "email") if form.get(f"r_{sev}_{c}") == "on"] for sev in notify.SEVERITIES}
        notify.set_setting(con, "routing", routing)
        events.append(con, type="settings.updated", summary="Alert routing changed", payload={"routing": routing}, **org.FOUNDER)
        return done("/settings", "Alert routing saved")

    @app.post("/settings/general")
    async def settings_general(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        try:
            rate = int(form.get("rate_per_hour") or 0)
            down = int(form.get("engine_down_after") or 0)
        except ValueError:
            rate = down = 0
        url = str(form.get("dashboard_url") or "").strip().rstrip("/")
        if not (1 <= rate <= 200 and 10 <= down <= 600 and re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+", url)):
            return RedirectResponse(f"/settings?err={quote('Check the values: messages per hour 1-200, engine timeout 10-600 seconds, a plain http(s) address')}", status_code=303)
        notify.set_setting(con, "rate_per_hour", rate)
        notify.set_setting(con, "engine_down_after", down)
        notify.set_setting(con, "dashboard_url", url)
        events.append(con, type="settings.updated", summary="Alert settings changed", payload={"rate_per_hour": rate, "engine_down_after": down}, **org.FOUNDER)
        return done("/settings", "Settings saved")

    @app.post("/settings/secret")
    async def settings_secret(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        changed = []
        for key in channels.SECRET_KEYS:
            value = str(form.get(key) or "")
            if value.strip():
                try:
                    channels.update_secret(request.app.state.home, key, value)
                except ValueError:
                    return RedirectResponse(f"/settings?err={quote('Invalid value')}", status_code=303)
                changed.append(key)
        if changed:
            redact.install(request.app.state.home)
            events.append(con, type="settings.updated", summary="Notification credentials changed", payload={"keys": changed}, **org.FOUNDER)
        return done("/settings", f"Saved {len(changed)} value(s)" if changed else "Nothing to change")

    @app.post("/settings/test/{channel}")
    async def settings_test(request: Request, channel: str, con=Depends(get_con)):
        await read_form(request)
        if channel not in ("telegram", "email"):
            raise HTTPException(404)
        transports = request.app.state.transports or channels.load_transports(request.app.state.home)
        notifier = Notifier(transports, scrub=redact.Redactor(redact.load_secret_values(request.app.state.home)).scrub)
        result = notifier.send_direct("[AICorp] Test message", "This is a test from your AICorp dashboard. No action needed.", (channel,))[channel]
        events.append(con, type="settings.test_sent", summary=f"Test message to {channel}: {result[:80]}", payload={"channel": channel}, **org.FOUNDER)
        if result == "sent":
            return done("/settings", f"Test message sent via {channel}")
        return RedirectResponse(f"/settings?err={quote(f'{channel}: {result}')}", status_code=303)

    @app.get("/chat")
    def chat_page(request: Request, con=Depends(get_con), q: str = "", channel: str = ""):
        home = request.app.state.home
        return render(request, "chat.html", rows=chat.history(con, q=q[:100], channel=channel if channel in ("telegram", "email") else ""),
                      q=q[:100], channel=channel, status={c: chat.channel_status(con, c) for c in ("telegram", "email")},
                      allowed={c: bool(v) for c, v in channels.allowed_senders(home).items()})

    @app.post("/settings/browser/close")
    async def settings_browser_close(request: Request, con=Depends(get_con)):
        await read_form(request)
        count = browser_runner.close_all(request.app.state.home)
        events.append(con, type="settings.updated", summary=f"Closed {count} browser window(s) left for hand-off", payload={"count": count}, **org.FOUNDER)
        return done("/settings", f"Closed {count} browser window(s). The blocked tasks stay blocked; Resume starts a fresh browser.")

    @app.post("/settings/chat/test-email")
    async def settings_chat_test(request: Request, con=Depends(get_con)):
        await read_form(request)
        inbox = (request.app.state.inboxes or channels.load_inboxes(request.app.state.home)).get("email")
        if inbox is None:
            return RedirectResponse(f"/settings?err={quote('E-mail is not set up')}", status_code=303)
        try:
            if hasattr(inbox, "check_login"):
                inbox.check_login()
        except channels.DeliveryError as err:
            return RedirectResponse(f"/settings?err={quote(str(err))}", status_code=303)
        events.append(con, type="settings.test_sent", summary="Mailbox (IMAP) login test passed", payload={"channel": "email"}, **org.FOUNDER)
        return done("/settings", "Mailbox login works. Send yourself an e-mail whose subject contains [AICorp] to try the chat.")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        ref = secrets.token_hex(4)
        try:
            con = connect(db_path(request.app.state.home))
            try:
                kind = type(exc).__name__
                events.append(con, type="system.error", summary=f"Dashboard error {ref} on {request.url.path}: {kind}: {str(exc)[:200]}",
                              payload={"ref": ref, "path": request.url.path, "error": kind}, **org.SYSTEM)
                notify.upsert(con, key=f"error:web:{kind}", category="system", severity="critical", title="Internal error in the dashboard",
                              detail=f"{kind} on {request.url.path} (reference {ref}).", link="/logs")
            finally:
                con.close()
        except Exception:
            pass
        return HTMLResponse(f"<!doctype html><title>Error</title><body style='font-family:sans-serif;max-width:40em;margin:3em auto'>"
                            f"<h1>Something went wrong</h1><p>The error was recorded (reference <b>{ref}</b>) and you were notified.</p>"
                            f"<p><a href='/'>Back to the Command Center</a></p></body>", status_code=500)

    # ------------------------------------------------------------ the Advisor, projects, meetings

    def now_utc():
        return datetime.now(timezone.utc)

    def fail(path: str, err) -> RedirectResponse:
        message = "; ".join(err.errors) if isinstance(err, org.ValidationError) else str(err)
        sep = "&" if "?" in path else "?"
        return RedirectResponse(f"{path}{sep}err={quote(message)}", status_code=303)

    @app.get("/advisor")
    def advisor_desk(request: Request, con=Depends(get_con), tab: str = "all", snoozed: str = ""):
        attention.sync(con, request.app.state.home)
        advisor.scan(con, now_utc())                       # the desk is a live view: look again now
        tab = tab if tab in advisor.TABS else "all"
        items = advisor.desk_items(con, now_utc(), include_snoozed=bool(snoozed))
        counts = {t: len(advisor.filter_tab(items, t)) for t in advisor.TABS}
        shown = advisor.filter_tab(items, tab)
        return render(request, "advisor.html", tab=tab, tabs=advisor.TABS, counts=counts, groups=advisor.group_by_project(shown),
                      total=len(shown), monitor=scheduler.monitor_enabled(con), snoozed=snoozed, unreviewed=len(advisor.review_order([i for i in items if not i["ack_at"]])))

    @app.get("/advisor/review")
    def advisor_review(request: Request, con=Depends(get_con), i: int = 0):
        attention.sync(con, request.app.state.home)
        advisor.scan(con, now_utc())
        items = advisor.review_order([x for x in advisor.desk_items(con, now_utc()) if not x["ack_at"]])
        i = max(i, 0)
        item = items[i] if i < len(items) else None
        return render(request, "advisor_review.html", item=item, index=i, total=len(items), next_i=i + 1, prev_i=max(i - 1, 0))

    @app.get("/advisor/schedule")
    def advisor_schedule(request: Request, con=Depends(get_con)):
        scheduler.ensure_defaults(con, now_utc())
        return render(request, "advisor_schedule.html", monitor=scheduler.monitor_enabled(con), jobs=scheduler.status(con),
                      runs=scheduler.history(con, 25), quiet=notify.get_setting(con, "quiet_hours"), cap=notify.get_setting(con, "nag_daily_cap"),
                      stale=notify.get_setting(con, "stale_days"), pstale=notify.get_setting(con, "project_stale_days"),
                      ai=notify.get_setting(con, "ai_summary"), away=notify.get_setting(con, "away_threshold_minutes"),
                      last_tick=notify.get_setting(con, "monitor_last_tick"))

    @app.post("/advisor/monitor")
    async def advisor_monitor(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        scheduler.set_monitor(con, form.get("state") == "on")
        return done(safe_next(form.get("next"), "/advisor"), "Monitor is now " + ("on" if form.get("state") == "on" else "off"))

    @app.post("/advisor/run/{job}")
    async def advisor_run(request: Request, job: str, con=Depends(get_con)):
        await read_form(request)
        try:
            result = scheduler.run_now(con, request.app.state.home, job)
        except ValueError:
            raise HTTPException(404)
        return done("/advisor/schedule", f"{job}: {result['status']}" + (f" — {result['detail']}" if result["detail"] else ""))

    @app.post("/advisor/job/{job}")
    async def advisor_job(request: Request, job: str, con=Depends(get_con)):
        form = await read_form(request)
        try:
            scheduler.set_job(con, job, spec=str(form.get("spec") or "").strip() or None, enabled=form.get("enabled") == "on")
        except ValueError as err:
            return fail("/advisor/schedule", err)
        return done("/advisor/schedule", "Schedule saved")

    @app.post("/advisor/settings")
    async def advisor_settings(request: Request, con=Depends(get_con)):
        form = await read_form(request)
        try:
            cap, stale, pstale, away = (int(form.get(k) or 0) for k in ("nag_daily_cap", "stale_days", "project_stale_days", "away_threshold_minutes"))
            quiet = {"start": str(form.get("quiet_start") or ""), "end": str(form.get("quiet_end") or "")}
            for value in quiet.values():
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
                    raise ValueError
            if not (1 <= cap <= 50 and 1 <= stale <= 60 and 1 <= pstale <= 90 and 5 <= away <= 1440):
                raise ValueError
        except ValueError:
            return RedirectResponse(f"/advisor/schedule?err={quote('Check the values (times like 22:00; limits within sensible ranges)')}", status_code=303)
        for key, value in (("nag_daily_cap", cap), ("stale_days", stale), ("project_stale_days", pstale), ("away_threshold_minutes", away),
                           ("quiet_hours", quiet), ("ai_summary", form.get("ai_summary") == "on")):
            notify.set_setting(con, key, value)
        events.append(con, type="settings.updated", summary="Advisor settings changed", payload={"keys": ["nag", "quiet_hours", "stale", "ai_summary"]}, **org.FOUNDER)
        return done("/advisor/schedule", "Saved")

    @app.get("/advisor/reports")
    def reports_page(request: Request, con=Depends(get_con), kind: str = ""):
        rows = [dict(r) for r in con.execute("SELECT * FROM report" + (" WHERE kind = ?" if kind else "") + " ORDER BY created_at DESC LIMIT 100", ([kind] if kind else []))]
        kinds = [r[0] for r in con.execute("SELECT DISTINCT kind FROM report ORDER BY kind")]
        return render(request, "reports.html", rows=rows, kinds=kinds, kind=kind)

    @app.get("/advisor/reports/{report_id}")
    def report_page(request: Request, report_id: str, con=Depends(get_con)):
        row = con.execute("SELECT * FROM report WHERE report_id = ?", (report_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        path = (request.app.state.home / row["path"]).resolve()
        try:
            path.relative_to((request.app.state.home / "memory" / "reports").resolve())
            text = path.read_text(encoding="utf-8")
        except (ValueError, OSError):
            text = "(the report file is missing)"
        con.execute("UPDATE notification SET read_at = COALESCE(read_at, ?) WHERE entity_type = 'report' AND entity_id = ?", (utc_now(), report_id))
        ai = notify.get_setting(con, f"report_ai:{report_id}")
        return render(request, "report.html", report=dict(row), text=text, ai_task=tasks.get_task(con, ai) if ai else None)

    # ---- projects

    def project_data(form) -> dict:
        return {k: form.get(k) for k in ("name", "goal", "status", "owner_emp_id", "next_review_at", "notes")}

    @app.get("/projects")
    def projects_page(request: Request, con=Depends(get_con)):
        rows = projects.list_projects(con)
        now = now_utc()
        for r in rows:
            r["health"] = projects.project_health(con, r["project_id"], now)
        return render(request, "projects.html", rows=rows)

    @app.get("/projects/new")
    def project_new(request: Request, con=Depends(get_con)):
        return render(request, "project_detail.html", project=None, f={"name": "", "goal": "", "status": "active", "owner_emp_id": "", "next_review_at": "", "notes": ""},
                      errors=[], people=org.list_employees(con), statuses=projects.PROJECT_STATUSES)

    @app.post("/projects")
    async def project_create(request: Request, con=Depends(get_con)):
        data = project_data(await read_form(request))
        try:
            project_id = projects.create_project(con, data)
        except org.ValidationError as err:
            return render(request, "project_detail.html", code=400, project=None, f={**data}, errors=err.errors, people=org.list_employees(con), statuses=projects.PROJECT_STATUSES)
        return done(f"/projects/{project_id}", "Project created")

    @app.get("/projects/{project_id}")
    def project_page(request: Request, project_id: str, con=Depends(get_con)):
        project = projects.get_project(con, project_id)
        if project is None:
            raise HTTPException(404)
        f = {**project, "owner_emp_id": project["owner_emp_id"] or "", "goal": project["goal"] or "", "notes": project["notes"] or "",
             "next_review_at": org.to_local_input(project["next_review_at"])}
        return render(request, "project_detail.html", project=project, f=f, errors=[], people=org.list_employees(con), statuses=projects.PROJECT_STATUSES,
                      health=projects.project_health(con, project_id, now_utc()), project_tasks=[t for t in tasks.list_tasks(con, limit=500) if t["project_id"] == project_id],
                      meetings=projects.list_meetings(con, project_id), decisions=projects.list_decisions(con, project_id=project_id))

    @app.post("/projects/{project_id}")
    async def project_update(request: Request, project_id: str, con=Depends(get_con)):
        data = project_data(await read_form(request))
        try:
            projects.update_project(con, project_id, data)
        except org.ValidationError as err:
            return fail(f"/projects/{project_id}", err)
        return done(f"/projects/{project_id}", "Saved")

    @app.post("/projects/{project_id}/delete")
    async def project_delete(request: Request, project_id: str, con=Depends(get_con)):
        await read_form(request)
        try:
            projects.delete_project(con, project_id)
        except org.ValidationError as err:
            return fail("/projects", err)
        return done("/projects", "Project deleted")

    # ---- meetings

    def meeting_data(form) -> dict:
        return {"title": form.get("title"), "project_id": form.get("project_id"), "scheduled_at": form.get("scheduled_at"),
                "agenda": form.get("agenda"), "attendees": form.getlist("attendees")}

    def meeting_page_ctx(con) -> dict:
        return {"people": org.list_employees(con), "all_projects": projects.list_projects(con, include_done=False)}

    @app.get("/meetings")
    def meetings_page(request: Request, con=Depends(get_con)):
        return render(request, "meetings.html", rows=projects.list_meetings(con), now=utc_now())

    @app.get("/meetings/new")
    def meeting_new(request: Request, con=Depends(get_con)):
        return render(request, "meeting_detail.html", meeting=None, f={"title": "", "project_id": "", "scheduled_at": "", "agenda": "", "attendees": []}, errors=[], **meeting_page_ctx(con))

    @app.post("/meetings")
    async def meeting_create(request: Request, con=Depends(get_con)):
        data = meeting_data(await read_form(request))
        try:
            meeting_id = projects.create_meeting(con, request.app.state.home, data)
        except org.ValidationError as err:
            return render(request, "meeting_detail.html", code=400, meeting=None, f=data, errors=err.errors, **meeting_page_ctx(con))
        return done(f"/meetings/{meeting_id}", "Meeting scheduled")

    @app.get("/meetings/{meeting_id}")
    def meeting_page(request: Request, meeting_id: str, con=Depends(get_con)):
        meeting = projects.get_meeting(con, meeting_id)
        if meeting is None:
            raise HTTPException(404)
        minutes = ""
        if meeting["minutes_path"]:
            try:
                minutes = (request.app.state.home / meeting["minutes_path"]).read_text(encoding="utf-8")
            except OSError:
                pass
        f = {"title": meeting["title"], "project_id": meeting["project_id"] or "", "scheduled_at": org.to_local_input(meeting["scheduled_at"]),
             "agenda": meeting["agenda"] or "", "attendees": [a["id"] for a in meeting["attendees"]]}
        round_task = tasks.get_task(con, meeting["round_task_id"]) if meeting["round_task_id"] else None
        return render(request, "meeting_detail.html", meeting=meeting, f=f, errors=[], minutes=minutes, round_task=round_task, **meeting_page_ctx(con))

    def meeting_action(fn):
        async def handler(request: Request, meeting_id: str, con=Depends(get_con)):
            form = await read_form(request)
            try:
                message = fn(con, request.app.state.home, meeting_id, form) or "Done"
            except (org.ValidationError, PermissionError) as err:
                return fail(f"/meetings/{meeting_id}", err)
            attention.sync(con, request.app.state.home)
            return done(f"/meetings/{meeting_id}", message)
        return handler

    app.post("/meetings/{meeting_id}")(meeting_action(lambda c, h, m, f: projects.update_meeting(c, h, m, meeting_data(f)) or "Saved"))
    app.post("/meetings/{meeting_id}/status")(meeting_action(lambda c, h, m, f: projects.set_meeting_status(c, m, str(f.get("status") or "")) or "Updated"))
    app.post("/meetings/{meeting_id}/minutes")(meeting_action(lambda c, h, m, f: projects.save_minutes(c, h, m, str(f.get("minutes") or "")) and "Minutes saved"))
    app.post("/meetings/{meeting_id}/action")(meeting_action(lambda c, h, m, f: projects.add_action_item(
        c, m, str(f.get("description") or ""), (f.get("owner") or "").strip() or None, f.get("due_at") or None, int(f.get("risk_tier") or 1)) and "Action item added"))
    app.post("/meetings/{meeting_id}/decision")(meeting_action(lambda c, h, m, f: projects.add_decision(
        c, h, {"title": f.get("title"), "text": f.get("text"), "follow_up_by": f.get("follow_up_by"), "meeting_id": m}) and "Decision recorded"))
    app.post("/meetings/{meeting_id}/bot-round")(meeting_action(lambda c, h, m, f: projects.start_bot_meeting(c, m) and "Bot round started: the inputs are now tasks"))
    app.post("/meetings/{meeting_id}/use-minutes")(meeting_action(lambda c, h, m, f: projects.use_round_result_as_minutes(c, h, m) and "Minutes saved from the CEO's summary"))

    @app.post("/decisions/{decision_id}/close")
    async def decision_close(request: Request, decision_id: str, con=Depends(get_con)):
        form = await read_form(request)
        try:
            projects.close_decision(con, decision_id, str(form.get("status") or "followed_up"))
        except org.ValidationError as err:
            return fail("/advisor", err)
        attention.sync(con, request.app.state.home)
        advisor.scan(con, now_utc())
        return done(safe_next(form.get("next"), "/advisor"), "Decision closed")

    # ------------------------------------------------------------ live events

    @app.get("/api/events")
    def api_events(con=Depends(get_con), since: int = 0, limit: int = 100):
        rows = con.execute("SELECT * FROM event WHERE id > ? ORDER BY id LIMIT ?", (since, min(max(limit, 1), 500))).fetchall()
        return JSONResponse([public_event(r) for r in rows])

    @app.get("/api/events/stream")
    async def api_events_stream(request: Request, since: int = -1, timeout: float = 0):
        """Server-sent events. New audit events appear within ~0.5 s. ``since`` (or the
        Last-Event-ID header) replays what was missed. ``timeout`` ends the stream (for tests)."""
        start = since
        if start < 0 and request.headers.get("last-event-id", "").isdigit():
            start = int(request.headers["last-event-id"])

        async def generate():
            con = connect(db_path(request.app.state.home), cross_thread=True)
            try:
                last = start
                if last < 0:
                    last = events.head(con)[0]
                yield "retry: 2000\n\n"
                deadline = time.monotonic() + timeout if timeout > 0 else None
                quiet = 0
                while not await request.is_disconnected():
                    rows = await run_in_threadpool(
                        lambda: con.execute("SELECT * FROM event WHERE id > ? ORDER BY id LIMIT 200", (last,)).fetchall())
                    for row in rows:
                        last = row["id"]
                        yield f"id: {last}\nevent: audit\ndata: {json.dumps(public_event(row), ensure_ascii=False)}\n\n"
                    quiet = 0 if rows else quiet + 1
                    if quiet and quiet % 30 == 0:
                        yield ": keepalive\n\n"
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(0.5)
            finally:
                con.close()

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ logs

    @app.get("/logs")
    def logs(request: Request, con=Depends(get_con), q: str = "", actor: str = "", task: str = "",
             mode: str = "", type: str = "", since: str = "", until: str = "", page: int = 1):
        page = max(page, 1)
        rows = search_events(
            con, q, actor=actor or None, task_id=task or None, mode=mode or None, event_type=type or None,
            since=f"{since}T00:00:00" if since else None, until=f"{until}T23:59:59.999999" if until else None,
            limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
        params = {"q": q, "actor": actor, "task": task, "mode": mode, "type": type, "since": since, "until": until}
        def page_url(n: int) -> str:
            return "/logs?" + urlencode({**{k: v for k, v in params.items() if v}, "page": n})

        return render(request, "logs.html", rows=rows[:PAGE_SIZE], more=len(rows) > PAGE_SIZE, page=page,
                      params=params, newer_url=page_url(page - 1), older_url=page_url(page + 1), chain=events.verify_incremental(con) if page == 1 and not any(params.values()) else None)

    return app
