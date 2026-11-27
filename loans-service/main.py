"""Private, separately hosted entry point for the shared loan dashboard."""
import os
from html import escape
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth import exceptions
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_oauth2_token


ROOT = Path(__file__).resolve().parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
bearer = HTTPBearer(auto_error=False)


def settings():
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    members = {e.strip().lower() for e in os.environ.get("LOANS_MEMBERS", "").split(",") if e.strip()}
    if not client_id or len(members) != 2:
        raise HTTPException(503, "Dashboard access has not been configured.")
    return client_id, members


def require_member(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    client_id, members = settings()
    if credentials is None:
        raise HTTPException(401, "Sign in with Google.")
    try:
        claims = verify_oauth2_token(credentials.credentials, Request(), audience=client_id)
    except exceptions.TransportError:
        raise HTTPException(503, "Google verification is temporarily unavailable.") from None
    except (ValueError, exceptions.GoogleAuthError):
        raise HTTPException(401, "Sign in again.") from None
    email = (claims.get("email") or "").lower()
    # For third-party mailboxes, verification at Google account creation is
    # not proof of current ownership. Accept Gmail or managed Workspace only.
    authoritative = email.endswith("@gmail.com") or bool(claims.get("hd"))
    if claims.get("email_verified") is not True or not authoritative or email not in members:
        raise HTTPException(403, "This dashboard is restricted to its members.")
    return claims["sub"]


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard():
    client_id, _ = settings()
    return (ROOT / "dashboard.html").read_text().replace("GOOGLE_CLIENT_ID", escape(client_id, quote=True))


@app.get("/dashboard.js")
def script():
    return FileResponse(ROOT / "dashboard.js", media_type="text/javascript")


@app.get("/api/shared-loans")
def shared_loans(member: str = Depends(require_member)):
    # No lender integration yet; never substitute demo balances.
    return {"loans": []}
