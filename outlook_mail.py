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

# What outlook_auth.py asks James to consent to: mail plus the OneDrive and
# calendar access Rowan's Telegram features use. Mail calls keep requesting
# only SCOPES, so mail keeps working even before the wider consent is done.
AUTH_SCOPES = SCOPES + ["Files.ReadWrite", "Calendars.ReadWrite"]

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


# ============================================================
# SIGNATURE — added to every email Rowan sends, to match James's Outlook
# ============================================================

SIGNATURE_NAME = "James Galvin"
SIGNATURE_LINES = ["305-407-4898", "James@Miami-Coastline.com", "www.miami-coastline.com"]
DEFAULT_CLOSING = "Best Regards,"
LOGO_CID = "mcm-signature-logo"
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "email-logo.png")
LOGO_WIDTH_PX = 150

_CLOSING_RE = re.compile(
    r"^(best( regards)?|kind regards|warm regards|regards|thanks( again)?|thank you( again)?|"
    r"many thanks|cheers|sincerely|respectfully)\s*[,.!]?\s*(james( galvin)?)?\s*[,.]?$",
    re.IGNORECASE)
_NAME_RE = re.compile(r"^[-–—\s]*(james( galvin)?|jg)\s*[,.]?$", re.IGNORECASE)
_SIG_LINE_RE = re.compile(r"(miami coastline management|305[-. ]?407[-. ]?4898|@miami-coastline\.com|"
                          r"miami-coastline\.com)", re.IGNORECASE)


def split_closing(body_text: str):
    """
    Strip whatever sign-off a draft ends with (closing word, name, any old
    signature lines). Returns (body, closing) where closing is the draft's own
    closing word ("Thanks,") or the default "Best Regards,".
    """
    lines = (body_text or "").rstrip().split("\n")
    closing = None
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if _NAME_RE.match(last) or (_SIG_LINE_RE.search(last) and len(last) < 80):
            lines.pop()
            continue
        if _CLOSING_RE.match(last):
            word = re.sub(r"\s*[,.!]?\s*(james( galvin)?)?\s*[,.]?$", "", last, flags=re.IGNORECASE)
            closing = word[:1].upper() + word[1:] + ","
            lines.pop()
        break
    return "\n".join(lines).rstrip(), closing or DEFAULT_CLOSING


def signature_html(closing: str = DEFAULT_CLOSING, with_logo: bool = True) -> str:
    p = '<p style="margin:0;">'
    esc = html.escape
    parts = [f'{p}{esc(closing)}</p>', f'{p}&nbsp;</p>', f'{p}{esc(SIGNATURE_NAME)}</p>',
             f'{p}{esc(SIGNATURE_LINES[0])}</p>',
             f'{p}<a href="mailto:{esc(SIGNATURE_LINES[1])}">{esc(SIGNATURE_LINES[1])}</a></p>',
             f'{p}<a href="http://{esc(SIGNATURE_LINES[2])}">{esc(SIGNATURE_LINES[2])}</a></p>']
    if with_logo and os.path.exists(LOGO_PATH):
        parts.append(f'{p}<img src="cid:{LOGO_CID}" alt="Miami Coastline Management" '
                     f'width="{LOGO_WIDTH_PX}" style="width:{LOGO_WIDTH_PX}px;height:auto;margin-top:8px;"></p>')
    return '<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;">' + "".join(parts) + "</div>"


def signed_html(body_text: str) -> str:
    """Approved plain-text body -> HTML with James's standard signature and logo."""
    body, closing = split_closing(body_text)
    spacer = '<p style="margin:0 0 12px 0;">&nbsp;</p>' if body else ""
    return text_to_html(body) + spacer + signature_html(closing)


def signed_text(body_text: str) -> str:
    """What the signed email reads like, for previews in Telegram or the dashboard."""
    body, closing = split_closing(body_text)
    return f"{body}\n\n{closing}\n\n{SIGNATURE_NAME}\n" + "\n".join(SIGNATURE_LINES) + "\n[MCM logo]"


def _logo_attachment():
    if not os.path.exists(LOGO_PATH):
        return None
    import base64
    with open(LOGO_PATH, "rb") as f:
        data = f.read()
    return {"@odata.type": "#microsoft.graph.fileAttachment", "name": "mcm-logo.png",
            "contentType": "image/png", "contentBytes": base64.b64encode(data).decode("ascii"),
            "isInline": True, "contentId": LOGO_CID}


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
        draft.get("body", {}).get("content", ""), signed_html(body_text)
    )

    resp = requests.patch(
        f"{GRAPH}/me/messages/{draft_id}",
        headers=_headers(token),
        json={"body": {"contentType": "HTML", "content": merged}},
        timeout=30,
    )
    _check(resp, "draft update")

    logo = _logo_attachment()
    if logo:
        resp = requests.post(
            f"{GRAPH}/me/messages/{draft_id}/attachments",
            headers=_headers(token),
            json=logo,
            timeout=30,
        )
        _check(resp, "signature logo")

    resp = requests.post(
        f"{GRAPH}/me/messages/{draft_id}/send",
        headers=_headers(token),
        timeout=30,
    )
    _check(resp, "send")
    return draft_id


def _recipients(addresses) -> list:
    return [{"emailAddress": {"address": a}} for a in (addresses or [])]


def send_new_mail(to: list, subject: str, body_text: str, cc: list = None,
                  attachments: list = None) -> str:
    """
    Send a brand-new email (not a reply) from James's mailbox.

    Builds a draft first, then sends it, so it lands in Sent Items like any
    email he writes himself. Returns the draft's message id. Only called from
    an approved action. attachments: [(filename, bytes, content_type)], each
    under 3 MB (Graph's inline attachment limit).
    """
    import base64
    token = get_access_token()
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": signed_html(body_text)},
        "toRecipients": _recipients(to),
        "ccRecipients": _recipients(cc),
    }
    logo = _logo_attachment()
    message["attachments"] = [logo] if logo else []
    if attachments:
        message["attachments"] += [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentType": ctype or "application/octet-stream",
            "contentBytes": base64.b64encode(data).decode("ascii"),
        } for name, data, ctype in attachments]
    resp = requests.post(
        f"{GRAPH}/me/messages",
        headers=_headers(token),
        json=message,
        timeout=60,
    )
    _check(resp, "new draft")
    draft_id = resp.json()["id"]

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
