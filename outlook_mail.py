"""
outlook_mail.py — Microsoft Graph mail layer for Rowan.

Read + send. Kept separate from outlook_reader.py (which only scans for tasks
with a Mail.Read token) so the send path is easy to audit.

Token handling:
- The refresh token lives in the DB table `app_secrets` if present, otherwise
  falls back to the OUTLOOK_REFRESH_TOKEN env var.
- Azure rotates refresh tokens on use. We persist the rotated one back to
  `app_secrets` so the worker doesn't silently expire after 90 days.

Nothing in this module sends mail on its own. send_reply() is only ever called
from an approval route the user has clicked.
"""
import os
import re
import html
import requests
import msal
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

from db import get_connection

load_dotenv()

CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
TENANT_ID = os.getenv("AZURE_TENANT_ID")

GRAPH = "https://graph.microsoft.com/v1.0"

# Delegated scopes needed for read + reply. offline_access is added by MSAL.
SCOPES = ["Mail.ReadWrite", "Mail.Send"]

REFRESH_TOKEN_KEY = "outlook_refresh_token"


# ============================================================
# SECRET STORE (rotating refresh token)
# ============================================================

def _ensure_secrets_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_secrets (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def get_secret(key: str):
    conn = get_connection()
    cur = conn.cursor()
    _ensure_secrets_table(cur)
    conn.commit()
    cur.execute("SELECT value FROM app_secrets WHERE key = %s", (key,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def set_secret(key: str, value: str) -> None:
    conn = get_connection()
    cur = conn.cursor()
    _ensure_secrets_table(cur)
    cur.execute("""
        INSERT INTO app_secrets (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    """, (key, value))
    conn.commit()
    cur.close()
    conn.close()


def _current_refresh_token() -> str:
    try:
        stored = get_secret(REFRESH_TOKEN_KEY)
    except Exception as e:
        print(f"[outlook_mail] Could not read stored refresh token ({e}); using env.")
        stored = None
    return stored or os.getenv("OUTLOOK_REFRESH_TOKEN")


# ============================================================
# AUTH
# ============================================================

def get_access_token(scopes=None) -> str:
    """Exchange the refresh token for an access token, persisting rotation."""
    scopes = scopes or SCOPES
    refresh_token = _current_refresh_token()
    if not refresh_token:
        raise RuntimeError(
            "No Outlook refresh token available. Run outlook_auth.py to mint one."
        )

    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    )
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=scopes)

    if "access_token" not in result:
        raise RuntimeError(
            "Outlook token refresh failed: "
            f"{result.get('error')} — {result.get('error_description')}\n"
            "If this mentions consent or scope, re-run outlook_auth.py to "
            "re-consent with Mail.Send."
        )

    new_refresh = result.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        try:
            set_secret(REFRESH_TOKEN_KEY, new_refresh)
        except Exception as e:
            print(f"[outlook_mail] Could not persist rotated refresh token: {e}")

    return result["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _check(resp, what: str):
    if resp.status_code >= 400:
        raise RuntimeError(f"Graph {what} failed [{resp.status_code}]: {resp.text[:500]}")
    return resp


# ============================================================
# READ
# ============================================================

INBOX_FIELDS = (
    "id,conversationId,subject,bodyPreview,receivedDateTime,isRead,"
    "from,toRecipients,ccRecipients,internetMessageId"
)


def get_inbox_messages(token: str, hours: int = 36, top: int = 25) -> list:
    """Recent inbox messages, newest first."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    resp = requests.get(
        f"{GRAPH}/me/mailFolders/inbox/messages",
        headers=_headers(token),
        params={
            "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime desc",
            "$top": top,
            "$select": INBOX_FIELDS,
        },
        timeout=30,
    )
    _check(resp, "inbox fetch")
    return resp.json().get("value", [])


def get_message_body(token: str, message_id: str) -> str:
    """Plain-text body of one message."""
    resp = requests.get(
        f"{GRAPH}/me/messages/{message_id}",
        headers={**_headers(token), "Prefer": 'outlook.body-content-type="text"'},
        params={"$select": "body"},
        timeout=30,
    )
    _check(resp, "message body fetch")
    return resp.json().get("body", {}).get("content", "") or ""


def get_thread_context(token: str, conversation_id: str, limit: int = 5) -> list:
    """Earlier messages in the same conversation, oldest first, as plain text."""
    if not conversation_id:
        return []
    resp = requests.get(
        f"{GRAPH}/me/messages",
        headers={**_headers(token), "Prefer": 'outlook.body-content-type="text"'},
        params={
            "$filter": f"conversationId eq '{conversation_id}'",
            "$orderby": "receivedDateTime desc",
            "$top": limit,
            "$select": "id,subject,receivedDateTime,from,body",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        # Thread context is a nice-to-have; never fail the pass over it.
        print(f"[outlook_mail] thread context unavailable: {resp.status_code}")
        return []
    msgs = resp.json().get("value", [])
    return list(reversed(msgs))


def get_sent_samples(token: str, top: int = 8) -> list:
    """Recent sent messages, used as voice samples when drafting."""
    resp = requests.get(
        f"{GRAPH}/me/mailFolders/sentitems/messages",
        headers={**_headers(token), "Prefer": 'outlook.body-content-type="text"'},
        params={"$orderby": "sentDateTime desc", "$top": top, "$select": "subject,body"},
        timeout=30,
    )
    if resp.status_code >= 400:
        return []
    return resp.json().get("value", [])


# ============================================================
# SEND
# ============================================================

def text_to_html(text: str) -> str:
    """Plain-text draft -> simple HTML paragraphs, escaped."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    if not blocks:
        return "<div></div>"
    parts = []
    for b in blocks:
        parts.append(
            "<p style=\"margin:0 0 12px 0;\">"
            + html.escape(b).replace("\n", "<br>")
            + "</p>"
        )
    return '<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;">' + "".join(parts) + "</div>"


def _merge_into_draft_body(draft_html: str, our_html: str) -> str:
    """Put our reply above the quoted history Graph generated."""
    if not draft_html:
        return our_html
    match = re.search(r"<body[^>]*>", draft_html, flags=re.IGNORECASE)
    if match:
        idx = match.end()
        return draft_html[:idx] + our_html + draft_html[idx:]
    return our_html + draft_html


def send_reply(message_id: str, body_text: str, reply_all: bool = False) -> str:
    """
    Reply on the original thread and send it.

    Creates a Graph reply draft (correct recipients, subject and threading
    headers), injects the approved body above the quoted history, sends it,
    and returns the draft's message id. Only called from an approved action.
    """
    token = get_access_token()

    action = "createReplyAll" if reply_all else "createReply"
    resp = requests.post(
        f"{GRAPH}/me/messages/{message_id}/{action}",
        headers=_headers(token),
        json={},
        timeout=30,
    )
    _check(resp, action)
    draft = resp.json()
    draft_id = draft["id"]

    merged = _merge_into_draft_body(
        draft.get("body", {}).get("content", ""), text_to_html(body_text)
    )

    resp = requests.patch(
        f"{GRAPH}/me/messages/{draft_id}",
        headers=_headers(token),
        json={"body": {"contentType": "HTML", "content": merged}},
        timeout=30,
    )
    _check(resp, "draft update")

    resp = requests.post(
        f"{GRAPH}/me/messages/{draft_id}/send",
        headers=_headers(token),
        timeout=30,
    )
    _check(resp, "send")
    return draft_id


if __name__ == "__main__":
    tok = get_access_token()
    msgs = get_inbox_messages(tok, hours=48, top=5)
    print(f"Token OK. {len(msgs)} messages in the last 48h:")
    for m in msgs:
        sender = m.get("from", {}).get("emailAddress", {})
        print(f"  - {sender.get('name')} <{sender.get('address')}>: {m.get('subject')}")
