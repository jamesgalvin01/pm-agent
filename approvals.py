"""
approvals.py — the approve/deny/modify state machine, shared by every channel.

Rowan drafts a reply (email_replies.py), sends it to James for approval, and
sends the email only when he says so. This module owns:
  - the single-slot pending store (one open question at a time)
  - the email-draft queue (highest urgency first, then oldest)
  - what SEND / NO / LATER / a rewrite each mean

Channels (Telegram, SMS) are thin: they deliver a message and hand replies back
to handle_response(). Which one is live is set by APPROVAL_CHANNEL.

Nothing here sends an email except through outlook_mail.send_reply(), and only
on an explicit approval from James.
"""
import os
import json
from datetime import datetime

from db import get_connection
from outlook_mail import send_reply

# telegram | sms | off
APPROVAL_CHANNEL = os.getenv("APPROVAL_CHANNEL", "off").strip().lower()

# Telegram allows 4096 chars per message. The incoming email and the draft
# share that budget, with room left for headers and the button hint.
MAX_DRAFT_CHARS = 2000
MAX_INCOMING_CHARS = 1400

# How long a pending question stays answerable, by type.
PENDING_TTL_SECONDS = {"send": 900, "email_reply": 43200}   # 15 minutes / 12 hours
DEFAULT_PENDING_TTL = 900

APPROVE_WORDS = {"SEND", "YES", "Y", "CONFIRM", "OK", "OKAY", "GO", "APPROVE"}
REJECT_WORDS = {"NO", "N", "CANCEL", "DISCARD", "DENY", "STOP"}
DEFER_WORDS = {"LATER", "SKIP", "HOLD", "WAIT"}


# ============================================================
# PENDING STORE  (table name kept as sms_pending — pre-existing)
# ============================================================

def _ensure_pending_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sms_pending (
            id INT PRIMARY KEY DEFAULT 1,
            action JSONB,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)


def set_pending(action: dict) -> None:
    conn = get_connection()
    cur = conn.cursor()
    _ensure_pending_table(cur)
    conn.commit()
    cur.execute("""
        INSERT INTO sms_pending (id, action, created_at)
        VALUES (1, %s, now())
        ON CONFLICT (id) DO UPDATE SET action = EXCLUDED.action, created_at = now()
    """, (json.dumps(action),))
    conn.commit()
    cur.close()
    conn.close()


def get_pending():
    conn = get_connection()
    cur = conn.cursor()
    _ensure_pending_table(cur)
    conn.commit()
    cur.execute("SELECT action, created_at FROM sms_pending WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row[0]:
        return None
    action = row[0] or {}
    ttl = PENDING_TTL_SECONDS.get(action.get("type"), DEFAULT_PENDING_TTL)
    if (datetime.now(row[1].tzinfo) - row[1]).total_seconds() > ttl:
        clear_pending()
        return None
    return row[0]


def clear_pending() -> None:
    conn = get_connection()
    cur = conn.cursor()
    _ensure_pending_table(cur)
    cur.execute("UPDATE sms_pending SET action = NULL WHERE id = 1")
    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# DRAFT QUEUE
# ============================================================

def _draft_row(draft_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT graph_message_id, draft_body, from_name, from_email, subject, status,
               body_preview
          FROM email_drafts WHERE id = %s
    """, (draft_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def _next_untexted_draft():
    """Highest urgency first, then oldest, so nothing starves."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, from_name, from_email, subject, draft_body, urgency,
               body_preview, rationale, received_at
          FROM email_drafts
         WHERE status = 'pending'
           AND draft_body IS NOT NULL
           AND texted_at IS NULL
         ORDER BY (urgency = 'high') DESC, received_at ASC
         LIMIT 1
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def _update(sql, params):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()


def shorten(text: str, limit: int = MAX_DRAFT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[truncated - full text on /emails]"


# ============================================================
# CHANNEL DISPATCH
# ============================================================

def _notify(text: str, draft_id=None) -> None:
    """Deliver a message to James on whichever channel is live."""
    if APPROVAL_CHANNEL == "telegram":
        from telegram_bot import send_message
        send_message(text, draft_id=draft_id)
    elif APPROVAL_CHANNEL == "sms":
        from sms_webhook import _send_sms_reply
        _send_sms_reply(text)
    else:
        print(f"[approvals] APPROVAL_CHANNEL={APPROVAL_CHANNEL!r}; not delivering.")


def _draft_for_message(draft_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, from_name, from_email, subject, draft_body, urgency,
               body_preview, rationale, received_at
          FROM email_drafts WHERE id = %s
    """, (draft_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def _send_draft(row) -> None:
    """Compose and deliver one draft for approval."""
    (draft_id, fname, femail, subject, draft_body, urgency,
     incoming, rationale, received) = row
    who = fname or femail or "unknown sender"
    flag = "⚠️ URGENT\n" if (urgency or "").lower() == "high" else ""
    when = received.strftime("%b %d, %I:%M %p").replace(" 0", " ") if received else ""

    header = f"{flag}From: {who}"
    if femail and fname:
        header += f" <{femail}>"
    header += f"\nSubject: {subject or '(no subject)'}"
    if when:
        header += f"\nReceived: {when}"
    if rationale:
        header += f"\nWhy: {rationale.strip()}"

    _notify(
        f"{header}\n\n"
        "--- THEIR EMAIL ---\n"
        f"{shorten(incoming, MAX_INCOMING_CHARS) or '(no text captured)'}\n\n"
        "--- ROWAN'S REPLY ---\n"
        f"{shorten(draft_body)}",
        draft_id=draft_id,
    )


def resend_pending_draft() -> bool:
    """Re-send whatever is currently awaiting an answer. True if one went out."""
    pending = get_pending()
    if not pending or pending.get("type") != "email_reply":
        return False
    row = _draft_for_message(pending.get("draft_id"))
    if not row:
        return False
    _send_draft(row)
    return True


def notify_next_email_draft() -> bool:
    """Send James the next unreviewed draft. Returns True if one went out."""
    if APPROVAL_CHANNEL not in ("telegram", "sms"):
        return False
    if get_pending():
        # Something is already awaiting his answer; never stack questions.
        return False
    row = _next_untexted_draft()
    if not row:
        return False

    set_pending({"type": "email_reply", "draft_id": row[0]})
    _update("UPDATE email_drafts SET texted_at = NOW() WHERE id = %s", (row[0],))
    _send_draft(row)
    return True


# ============================================================
# RESPONSES
# ============================================================

def handle_response(text: str) -> bool:
    """
    Apply James's answer to the pending email approval.
    Returns True if it was handled here, False if there was nothing pending
    (so a channel can fall through to its own command parsing).
    """
    pending = get_pending()
    if not pending or pending.get("type") != "email_reply":
        return False

    body = (text or "").strip()
    upper = body.upper()
    draft_id = pending.get("draft_id")
    row = _draft_row(draft_id)
    if not row:
        clear_pending()
        _notify("That draft is gone. Nothing was sent.")
        return True

    (graph_message_id, draft_body, fname, femail, _subject,
     status, incoming) = row
    who = fname or femail or "them"

    if status != "pending":
        # Handled on the dashboard while the question was outstanding.
        clear_pending()
        _notify(f"The reply to {who} was already handled. Nothing sent.")
        notify_next_email_draft()
        return True

    if upper in APPROVE_WORDS:
        if not (draft_body or "").strip():
            _notify("That draft is empty. Send me the reply you want to go out.")
            return True
        try:
            sent_id = send_reply(graph_message_id, draft_body)
        except Exception as e:
            _update("UPDATE email_drafts SET error = %s WHERE id = %s",
                    (str(e)[:500], draft_id))
            clear_pending()
            _notify(f"Send failed: {str(e)[:200]}\n\nIt's still waiting on /emails.")
            return True
        _update("""
            UPDATE email_drafts
               SET status = 'sent', sent_message_id = %s, decided_at = NOW(),
                   approved_via = %s, error = NULL
             WHERE id = %s
        """, (sent_id, APPROVAL_CHANNEL, draft_id))
        clear_pending()
        _notify(f"Sent to {who}.")
        notify_next_email_draft()
        return True

    if upper in REJECT_WORDS:
        _update("""
            UPDATE email_drafts
               SET status = 'discarded', decided_at = NOW(), approved_via = %s
             WHERE id = %s
        """, (APPROVAL_CHANNEL, draft_id))
        clear_pending()
        _notify(f"Discarded the reply to {who}. Nothing was sent.")
        notify_next_email_draft()
        return True

    if upper in DEFER_WORDS:
        clear_pending()
        _notify(f"Left the reply to {who} on the dashboard.")
        notify_next_email_draft()
        return True

    # Anything else steers the draft. James types an instruction ("make it
    # firmer", "push it to Monday") as readily as replacement wording, so let
    # Claude work out which and rewrite accordingly. Never sent without a
    # further SEND.
    try:
        from email_replies import revise_reply
        revised = revise_reply(incoming, draft_body, body)
    except Exception as e:
        print(f"[approvals] revise failed ({e}); using the message verbatim.")
        revised = body
    revised = (revised or "").strip() or body

    _update("UPDATE email_drafts SET draft_body = %s WHERE id = %s", (revised, draft_id))
    set_pending({"type": "email_reply", "draft_id": draft_id})   # resets the clock
    _notify(
        f"Updated the reply to {who}:\n\n{shorten(revised)}",
        draft_id=draft_id,
    )
    return True
