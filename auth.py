"""
auth.py — magic link authentication for Rowan.

Provides:
- create_magic_link_token(email)   -> token (and saves to DB)
- create_login(email)              -> (token, 6-digit code): same sign-in, two ways
- verify_login_code(email, code)   -> email | None (5 tries per code)
- session_needs_refresh(cookie)    -> True when the 30-day session should be renewed
- consume_magic_link_token(token)  -> email | None
- create_session_jwt(email)        -> JWT string
- require_auth                     -> FastAPI dependency that gates routes
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt  # PyJWT
from fastapi import Request, HTTPException, status

from db import get_connection


# ---------- config ----------

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET env var is required")

JWT_ALGORITHM = "HS256"
SESSION_COOKIE_NAME = "rowan_session"
SESSION_DURATION = timedelta(days=30)
MAGIC_LINK_DURATION = timedelta(minutes=15)
MAX_CODE_ATTEMPTS = 5
SESSION_REFRESH_AFTER = timedelta(days=1)
ALLOWED_EMAIL = os.getenv("ALLOWED_EMAIL", "").lower().strip()


# ---------- magic link tokens (DB-backed, one-time use) ----------

def create_magic_link_token(email: str) -> str:
    """Generate and persist a one-time magic link token. Returns the token."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + MAGIC_LINK_DURATION
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO magic_links (token, email, expires_at) VALUES (%s, %s, %s)",
        (token, email.lower().strip(), expires_at),
    )
    conn.commit()
    cur.close()
    conn.close()
    return token


# ---------- 6-digit sign-in codes ----------
# The home-screen app on an iPhone keeps its own cookies, separate from
# Safari. A magic link opens in Safari and signs *Safari* in, not the app.
# So every sign-in email also carries a 6-digit code, typed into the app.

_schema_ready = False


def _ensure_code_columns(cur) -> None:
    global _schema_ready
    if _schema_ready:
        return
    cur.execute("ALTER TABLE magic_links ADD COLUMN IF NOT EXISTS code_hash TEXT")
    cur.execute("ALTER TABLE magic_links ADD COLUMN IF NOT EXISTS code_attempts INT DEFAULT 0")
    _schema_ready = True


def _code_hash(token: str, code: str) -> str:
    return hmac.new(JWT_SECRET.encode(), f"{token}:{code}".encode(), hashlib.sha256).hexdigest()


def create_login(email: str) -> tuple:
    """One sign-in, usable either by link (token) or by typing the code. Returns (token, code)."""
    token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + MAGIC_LINK_DURATION
    conn = get_connection()
    cur = conn.cursor()
    try:
        _ensure_code_columns(cur)
        cur.execute(
            """INSERT INTO magic_links (token, email, expires_at, code_hash, code_attempts)
               VALUES (%s, %s, %s, %s, 0)""",
            (token, email.lower().strip(), expires_at, _code_hash(token, code)),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return token, code


def verify_login_code(email: str, code: str) -> Optional[str]:
    """
    Check a typed code against the newest unused, unexpired sign-in for this
    email. Five wrong tries burn that code. Returns the email or None.
    """
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    email = (email or "").lower().strip()
    conn = get_connection()
    cur = conn.cursor()
    try:
        _ensure_code_columns(cur)
        cur.execute(
            """SELECT token, code_hash, COALESCE(code_attempts, 0) FROM magic_links
                WHERE email = %s AND used_at IS NULL AND expires_at > NOW()
                  AND code_hash IS NOT NULL
                ORDER BY expires_at DESC LIMIT 1 FOR UPDATE""",
            (email,),
        )
        row = cur.fetchone()
        if not row or row[2] >= MAX_CODE_ATTEMPTS:
            conn.commit()
            return None
        token, stored, _ = row
        if hmac.compare_digest(stored, _code_hash(token, code)):
            cur.execute("UPDATE magic_links SET used_at = NOW() WHERE token = %s", (token,))
            conn.commit()
            return email
        cur.execute("UPDATE magic_links SET code_attempts = COALESCE(code_attempts, 0) + 1 WHERE token = %s",
                    (token,))
        conn.commit()
        return None
    finally:
        cur.close()
        conn.close()


def consume_magic_link_token(token: str) -> Optional[str]:
    """
    Validate a magic link token. If valid and unused and unexpired,
    mark it used and return the associated email. Otherwise return None.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE magic_links
           SET used_at = NOW()
         WHERE token = %s
           AND used_at IS NULL
           AND expires_at > NOW()
        RETURNING email
        """,
        (token,),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row[0] if row else None


# ---------- session JWT (stateless cookie) ----------

def create_session_jwt(email: str) -> str:
    """Build a signed JWT for the given email, valid for SESSION_DURATION."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email.lower().strip(),
        "iat": int(now.timestamp()),
        "exp": int((now + SESSION_DURATION).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_session_jwt(token: str) -> Optional[str]:
    """Return the email from a valid session JWT, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def session_needs_refresh(token: str) -> bool:
    """
    True when a valid session cookie is more than a day old. Renewing it keeps
    the phone app signed in for as long as it's used at least once a month.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return False
    issued = datetime.fromtimestamp(int(payload.get("iat", 0)), tz=timezone.utc)
    if datetime.now(timezone.utc) - issued <= SESSION_REFRESH_AFTER:
        return False
    return can_sign_in(payload.get("sub") or "")


def can_sign_in(email: str) -> bool:
    """James, or an active field user (a PM assigned to a project)."""
    email = (email or "").lower().strip()
    if not email:
        return False
    if email == ALLOWED_EMAIL:
        return True
    from field_users import is_field_user
    return is_field_user(email)


# ---------- FastAPI dependency ----------

def require_auth(request: Request) -> str:
    """
    FastAPI dependency: returns the logged-in email or redirects to /login.
    Use as: def my_route(email: str = Depends(require_auth)): ...
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    email = decode_session_jwt(cookie)
    if not email or email != ALLOWED_EMAIL:
        # A signed-in field PM goes to their project page, never James's pages.
        target = "/field" if email and can_sign_in(email) else "/login"
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": target},
        )
    return email


def require_field_access(request: Request) -> dict:
    """
    FastAPI dependency for the /field pages. Lets in James (every project) and
    active field users (only their assigned projects). Returns
      {"email", "name", "admin": bool, "project_ids": list | None (None = all)}
    Checked against the database on every request, so switching a field user
    off locks them out straight away.
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    email = decode_session_jwt(cookie) if cookie else None
    if email and email == ALLOWED_EMAIL:
        return {"email": email, "name": "James", "admin": True, "project_ids": None}
    if email:
        from field_users import get_field_user
        user = get_field_user(email)
        if user:
            return {"email": user["email"], "name": user["name"], "admin": False,
                    "project_ids": user["project_ids"]}
    raise HTTPException(
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Location": "/login"},
    )
