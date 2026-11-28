import hashlib
import secrets
import time

from fastapi import HTTPException
from google.auth import exceptions
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.id_token import verify_oauth2_token
from itsdangerous import BadData, URLSafeTimedSerializer

TTL = 3600


def verified(credential, cfg):
    try:
        claims = verify_oauth2_token(credential, GoogleRequest(), audience=cfg.client)
    except exceptions.TransportError:
        raise HTTPException(503, "Google verification unavailable.") from None
    except (ValueError, TypeError, KeyError, exceptions.GoogleAuthError):
        raise HTTPException(401, "Invalid credential.") from None
    email = claims.get("email")
    sub = claims.get("sub")
    if not isinstance(email, str) or not isinstance(sub, str) or not sub.strip() or len(sub) > 255:
        raise HTTPException(403, "Invalid identity.")
    email = email.lower()
    if (claims.get("email_verified") is not True or email not in cfg.members or
            not (email.endswith("@gmail.com") or
                 (isinstance(claims.get("hd"), str) and claims["hd"].strip()))):
        raise HTTPException(403, "Not an authorized household member.")
    return email, sub, claims["exp"]


def signer(cfg):
    return URLSafeTimedSerializer(cfg.secret, salt="loans-session-v1",
                                  signer_kwargs={"digest_method": hashlib.sha256})


def digest(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def issue(db, cfg, email, sub, exp, credential):
    db.execute("INSERT OR IGNORE INTO identities(sub,email) VALUES(?,?)", (sub, email))
    if not db.execute("SELECT 1 FROM identities WHERE sub=? AND email=?", (sub, email)).fetchone():
        raise HTTPException(403, "Identity binding changed.")
    now = int(time.time())
    db.execute("DELETE FROM exchanges WHERE expires < ?", (now,))
    # Fingerprint the signed input, not its (potentially noncanonical) base64 signature.
    if not db.execute("INSERT OR IGNORE INTO exchanges VALUES(?,?)",
                      (digest(credential.rsplit(".", 1)[0]), exp)).rowcount:
        raise HTTPException(401, "Credential already exchanged. Sign in again.")
    db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
    cookie = signer(cfg).dumps(secrets.token_urlsafe(32))
    db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(cookie), sub, now + TTL))
    return cookie


def session(db, cfg, request):
    cookie = request.cookies.get(cfg.cookie, "")
    try:
        signer(cfg).loads(cookie, max_age=TTL)
    except BadData:
        raise HTTPException(401, "Sign in again.") from None
    row = db.execute("""SELECT digest, sub, email FROM sessions JOIN identities USING(sub)
        WHERE digest=? AND expires>? AND email IN (?,?)""",
        (digest(cookie), int(time.time()), *cfg.members)).fetchone()
    if not row:
        raise HTTPException(401, "Sign in again.")
    return row
