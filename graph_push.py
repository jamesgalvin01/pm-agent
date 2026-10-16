"""
graph_push.py — real-time email: Microsoft tells Rowan the moment mail lands.

How it works
  1. Rowan keeps a Microsoft Graph *subscription* on James's Inbox. Graph
     subscriptions for mail last at most ~3 days, so a background thread in
     the web service checks every 30 minutes and renews (or recreates) it.
  2. When a new email arrives, Graph POSTs to /graph/notify. Rowan answers at
     once (Graph wants a reply within 3 seconds), then in the background
     triages the email and, if it needs James, drafts a reply
     (email_replies.process_message_id).
  3. In working hours (7am-7pm weekdays) the draft goes straight to Telegram.
     Outside them it waits for the 7am push.

Safety
  - Every notification must carry our secret clientState, or it's ignored.
  - A notification only says "message X arrived". Rowan fetches the message
    itself with its own token, so a forged call can't inject content.
  - Nothing is sent without James's tap; this only prepares drafts.
  - The worker's 15-minute backup pass catches anything a notification missed.

Needs PUBLIC_BASE_URL (already set for magic links) on the web service.
Without it, real time is off and the 15-minute backup does the work.
"""
import hashlib
import hmac
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, BackgroundTasks, Request, Response

router = APIRouter()

GRAPH = "https://graph.microsoft.com/v1.0"
RESOURCE = "me/mailFolders('Inbox')/messages"
LIFETIME_MINUTES = 4000          # Graph's limit for mail is 4230
RENEW_WHEN_LEFT = timedelta(hours=24)
CHECK_EVERY_SECONDS = 1800

SUB_ID_KEY = "graph_inbox_sub_id"
SUB_EXP_KEY = "graph_inbox_sub_expires"


def _base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def notify_url() -> str:
    return f"{_base_url()}/graph/notify"


def client_state() -> str:
    """Shared secret Graph echoes back on every notification (derived; no new env var)."""
    secret = (os.getenv("JWT_SECRET") or os.getenv("TELEGRAM_WEBHOOK_SECRET") or "rowan").encode()
    return hmac.new(secret, b"graph-inbox-notifications", hashlib.sha256).hexdigest()[:64]


def _headers():
    from outlook_mail import get_access_token
    return {"Authorization": f"Bearer {get_access_token()}", "Content-Type": "application/json"}


def _expiry():
    return (datetime.now(timezone.utc) + timedelta(minutes=LIFETIME_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


# ============================================================
# SUBSCRIPTION
# ============================================================

def _stored():
    from outlook_mail import get_secret
    sub_id = get_secret(SUB_ID_KEY)
    exp = get_secret(SUB_EXP_KEY)
    try:
        exp_dt = datetime.fromisoformat(exp) if exp else None
    except ValueError:
        exp_dt = None
    return sub_id, exp_dt


def _store(sub_id: str, expires: str) -> None:
    from outlook_mail import set_secret
    set_secret(SUB_ID_KEY, sub_id)
    # Graph returns e.g. 2026-10-19T11:16:13.0000000Z (7 decimals, which Python
    # won't parse). Store whole seconds in UTC.
    set_secret(SUB_EXP_KEY, expires[:19] + "+00:00")


def _create() -> str:
    resp = requests.post(f"{GRAPH}/subscriptions", headers=_headers(), timeout=30, json={
        "changeType": "created",
        "notificationUrl": notify_url(),
        "lifecycleNotificationUrl": notify_url(),
        "resource": RESOURCE,
        "expirationDateTime": _expiry(),
        "clientState": client_state(),
    })
    if resp.status_code >= 400:
        raise RuntimeError(f"Graph subscription create failed [{resp.status_code}]: {resp.text[:300]}")
    data = resp.json()
    _store(data["id"], data["expirationDateTime"])
    print(f"[graph_push] subscribed to inbox until {data['expirationDateTime']}", flush=True)
    return data["id"]


def _renew(sub_id: str) -> bool:
    resp = requests.patch(f"{GRAPH}/subscriptions/{sub_id}", headers=_headers(), timeout=30,
                          json={"expirationDateTime": _expiry()})
    if resp.status_code >= 400:
        print(f"[graph_push] renew failed [{resp.status_code}]: {resp.text[:200]}", flush=True)
        return False
    _store(sub_id, resp.json()["expirationDateTime"])
    return True


def _alive(sub_id: str) -> bool:
    """Does Graph still have this subscription, pointed at our current URL?"""
    resp = requests.get(f"{GRAPH}/subscriptions/{sub_id}", headers=_headers(), timeout=30)
    if resp.status_code >= 400:
        return False
    return resp.json().get("notificationUrl") == notify_url()


def ensure_subscription(force: bool = False) -> str:
    """Make sure a live inbox subscription exists. Returns 'ok', 'renewed', 'created' or 'off'."""
    if not _base_url().startswith("https://"):
        return "off"
    sub_id, exp = _stored()
    now = datetime.now(timezone.utc)
    if sub_id and not force and exp and exp - now > RENEW_WHEN_LEFT:
        return "ok"
    if sub_id and not force and _alive(sub_id) and _renew(sub_id):
        return "renewed"
    if sub_id:
        try:
            requests.delete(f"{GRAPH}/subscriptions/{sub_id}", headers=_headers(), timeout=30)
        except Exception:
            pass
    _create()
    return "created"


_started = False


def start_keeper() -> None:
    """Background thread in the web service: keeps the subscription alive."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(20)                      # let the web server come up: Graph validates the URL on create
        while True:
            try:
                state = ensure_subscription()
                if state not in ("ok", "off"):
                    print(f"[graph_push] subscription {state}", flush=True)
                elif state == "off":
                    print("[graph_push] PUBLIC_BASE_URL not https; real-time email is off "
                          "(15-minute backup still runs)", flush=True)
                    return
            except Exception as e:
                print(f"[graph_push] keeper error: {e}", flush=True)
            time.sleep(CHECK_EVERY_SECONDS)

    threading.Thread(target=loop, name="graph-subscription-keeper", daemon=True).start()


# ============================================================
# NOTIFICATIONS
# ============================================================

def handle_new_message(message_id: str) -> None:
    """Background: triage + draft one email, then offer it to James if it's working hours."""
    try:
        from email_replies import process_message_id
        result = process_message_id(message_id)
        if result == "drafted":
            from approvals import notify_next_email_draft
            notify_next_email_draft(respect_hours=True)
    except Exception as e:
        print(f"[graph_push] processing {message_id[:20]}... failed: {e}", flush=True)


def _mark_notified() -> None:
    try:
        from outlook_mail import set_secret
        set_secret("graph_last_notification", datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


@router.post("/graph/notify")
async def graph_notify(request: Request, background: BackgroundTasks):
    # 1. Validation handshake when the subscription is created or renewed.
    token = request.query_params.get("validationToken")
    if token is not None:
        return Response(content=token, media_type="text/plain", status_code=200)

    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400)

    expected = client_state()
    for note in payload.get("value", []):
        if not hmac.compare_digest(str(note.get("clientState") or ""), expected):
            print("[graph_push] notification with wrong clientState — ignored", flush=True)
            continue
        # 2. Lifecycle events: Graph wants the subscription reauthorized / recreated.
        if note.get("lifecycleEvent"):
            print(f"[graph_push] lifecycle event: {note['lifecycleEvent']}", flush=True)
            background.add_task(ensure_subscription, True)
            continue
        # 3. A new email.
        mid = (note.get("resourceData") or {}).get("id")
        if mid:
            background.add_task(_mark_notified)
            background.add_task(handle_new_message, mid)
    return Response(status_code=202, background=background)
