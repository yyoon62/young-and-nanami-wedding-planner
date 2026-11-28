"""Private, separately hosted entry point for the shared loan dashboard."""
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import auth
from accounts import router
from storage import Config, initialize, transaction


ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app):
    cfg = Config.load()
    initialize(cfg)
    app.state.cfg = cfg
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)


@app.middleware("http")
async def private_responses(request, call_next):
    cfg = request.app.state.cfg
    if request.method not in {"GET", "HEAD", "OPTIONS"} and (
        request.headers.getlist("origin") != [cfg.origin] or
        request.headers.getlist("x-dashboard-action") != ["1"]
    ):
        response = JSONResponse({"detail": "Same-origin dashboard requests only."}, status_code=403)
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    client_id = request.app.state.cfg.client
    return (ROOT / "dashboard.html").read_text().replace("GOOGLE_CLIENT_ID", escape(client_id, quote=True))


@app.get("/dashboard.js")
def script():
    return FileResponse(ROOT / "dashboard.js", media_type="text/javascript")


class Login(BaseModel):
    credential: str = Field(min_length=1, max_length=16384)


@app.post("/api/login", status_code=204)
def login(body: Login, request: Request, response: Response):
    cfg = request.app.state.cfg
    email, sub, exp = auth.verified(body.credential, cfg)
    with transaction(cfg, True) as db:
        cookie = auth.issue(db, cfg, email, sub, exp, body.credential)
    response.set_cookie(cfg.cookie, cookie, max_age=auth.TTL, httponly=True,
                        secure=cfg.secure, samesite="lax", path="/")


@app.post("/api/logout", status_code=204)
def logout(request: Request, response: Response):
    cfg = request.app.state.cfg
    with transaction(cfg, True) as db:
        row = auth.session(db, cfg, request)
        db.execute("DELETE FROM sessions WHERE digest=?", (row["digest"],))
    response.delete_cookie(cfg.cookie, secure=cfg.secure, httponly=True, samesite="lax", path="/")
