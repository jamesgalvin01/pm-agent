"""
auth.py — magic link authentication for Rowan.

Provides:
- create_magic_link_token(email)   -> token (and saves to DB)
- consume_magic_link_token(token)  -> email | None
- create_session_jwt(email)        -> JWT string
- require_auth                     -> FastAPI dependency that gates routes
"""
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
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return email
