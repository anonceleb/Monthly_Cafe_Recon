"""One shared password, signed session cookie. Off for local runs (no RECON_PASSWORD), mandatory in the cloud (RECON_REQUIRE_AUTH=1)."""
from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import time
from urllib.parse import quote

from fastapi import Form, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

OPEN_PATHS = {"/login", "/healthz"}
MAX_FAILS, WINDOW_S = 5, 300


def password() -> str:
    return os.environ.get("RECON_PASSWORD", "")


def enabled() -> bool:
    return bool(password())


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def check_startup() -> None:
    """Refuse to serve an open app where it is required to be closed (Docker image / Render set RECON_REQUIRE_AUTH=1)."""
    if not _flag("RECON_REQUIRE_AUTH"):
        return
    if len(password()) < 10:
        raise RuntimeError("RECON_REQUIRE_AUTH is set: RECON_PASSWORD must be set and at least 10 characters.")
    if len(os.environ.get("RECON_SECRET", "")) < 24:
        raise RuntimeError("RECON_REQUIRE_AUTH is set: RECON_SECRET must be set (24+ random characters) so sessions survive restarts.")


def safe_next(value: str) -> str:
    """Only ever redirect to a path on this site."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/"


def install(app, templates) -> None:
    fails: dict[str, list[float]] = {}
    templates.env.globals["auth_on"] = enabled

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if not enabled() or request.url.path in OPEN_PATHS or request.session.get("ok"):
            return await call_next(request)
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    # Added after `gate`, so it wraps it and request.session exists by the time `gate` runs.
    app.add_middleware(SessionMiddleware, secret_key=os.environ.get("RECON_SECRET") or secrets.token_urlsafe(32), session_cookie="recon_session",
                       max_age=12 * 3600, same_site="lax", https_only=_flag("RECON_COOKIE_SECURE"))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login")
    def login_form(request: Request, next: str = "/"):
        if not enabled() or request.session.get("ok"):
            return RedirectResponse(safe_next(next), status_code=303)
        return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": None})

    @app.post("/login")
    async def login(request: Request, password_in: str = Form(alias="password", default=""), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < WINDOW_S]
        if len(recent) >= MAX_FAILS:
            return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": "Too many attempts. Wait a few minutes and try again."}, status_code=429)
        if enabled() and hmac.compare_digest(password_in.encode(), password().encode()):
            fails.pop(ip, None)
            request.session.clear()
            request.session["ok"] = True
            return RedirectResponse(safe_next(next), status_code=303)
        fails[ip] = recent + [now]
        await asyncio.sleep(1)                       # slows guessing
        return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "error": "Wrong password."}, status_code=401)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
