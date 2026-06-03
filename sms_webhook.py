"""
sms_webhook.py — Twilio SMS interface for Rowan.

Lets James text Rowan to: send a message to a known contact (draft + confirm),
query project/task status, or add a task. Two hard safety rules:
  1. Only the allowlisted MY_PHONE number is ever obeyed.
  2. Any outbound message to a third party is drafted back to James and
     requires an explicit "SEND" reply before it goes out.
"""
import os
import json
import anthropic
import resend
from datetime import datetime
from fastapi import APIRouter, Request, Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

from db import get_connection

# ---- Config from environment (set in Railway pm_agent service) ----
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_NUMBER = os.getenv("TWILIO_NUMBER", "")
MY_PHONE = os.getenv("MY_PHONE", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Rowan <rowan@miami-coastline.com>")

anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
resend.api_key = os.getenv("RESEND_API_KEY")

router = APIRouter()

# Helper to send an SMS back to James via Twilio
def _send_sms_reply(body: str):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_NUMBER):
        print("[sms] Twilio not fully configured; cannot reply.")
        return
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=body, from_=TWILIO_NUMBER, to=MY_PHONE)
    except Exception as e:
        print(f"[sms] failed to send reply: {e}")


# ---- Pending confirmation store (one row, James only) ----
def _set_pending(action: dict):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sms_pending (
            id INT PRIMARY KEY DEFAULT 1,
            action JSONB,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.commit()
    cur.execute("""
        INSERT INTO sms_pending (id, action, created_at)
        VALUES (1, %s, now())
        ON CONFLICT (id) DO UPDATE SET action = EXCLUDED.action, created_at = now()
    """, (json.dumps(action),))
    conn.commit()
    cur.close()
    conn.close()


def _get_pending():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sms_pending (
            id INT PRIMARY KEY DEFAULT 1,
            action JSONB,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    conn.commit()
    cur.execute("SELECT action, created_at FROM sms_pending WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row[0]:
        return None
    # Expire pending actions older than 15 minutes
    age = (datetime.now(row[1].tzinfo) - row[1]).total_seconds()
    if age > 900:
        _clear_pending()
        return None
    return row[0]


def _clear_pending():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE sms_pending SET action = NULL WHERE id = 1")
    conn.commit()
    cur.close()
    conn.close()


# ---- Contact lookup (allowlist) ----
def _find_contact(name_query):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT name, email, phone FROM contacts
        WHERE LOWER(name) LIKE LOWER(%s)
        LIMIT 5
    """, (f"%{name_query}%",))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{"name": r[0], "email": r[1], "phone": r[2]} for r in rows]


# ---- Intent classification via Claude ----
def _classify(text):
    prompt = f"""You are the intent parser for a construction PM's SMS assistant. The user texted:

"{text}"

Classify into ONE of these intents and return ONLY a JSON object, no other text:

- send_message: user wants to send a message to a named person. Return:
  {{"intent":"send_message","recipient":"<name>","channel":"email|sms|auto","message_gist":"<what to say>"}}
- query: user is asking about tasks/projects/status. Return:
  {{"intent":"query","question":"<the question>"}}
- add_task: user wants to add a task/reminder. Return:
  {{"intent":"add_task","task":"<task text>"}}
- unknown: anything else. Return: {{"intent":"unknown"}}

For channel: if the user says "text" or "sms" use "sms"; if they say "email" use "email"; otherwise "auto".
Return only the JSON."""

    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


# ---- Draft a message body via Claude ----
def _draft_message(recipient_name, gist):
    prompt = f"""Draft a brief, professional message from James Galvin (Miami Coastline Management) to {recipient_name}.
The message should convey: {gist}
Keep it concise and natural — this is a quick business message, not a formal letter. Return only the message text."""
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ---- Query handler ----
def _handle_query(question):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.description, t.status, t.due_date, p.name
        FROM tasks t LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.status != 'complete'
        ORDER BY t.due_date ASC NULLS LAST LIMIT 30
    """)
    tasks = cur.fetchall()
    cur.close()
    conn.close()
    task_lines = "\n".join(f"- {t[0]} ({t[1]}, due {t[2]}, {t[3] or 'no project'})" for t in tasks) or "No open tasks."
    prompt = f"""James texted this question: "{question}"

Here are his current open tasks:
{task_lines}

Answer his question concisely in 1-3 sentences, suitable for an SMS reply. Plain text only."""
    resp = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ---- Add task handler ----
def _handle_add_task(task_text):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks (description, status, priority, project_id)
        VALUES (%s, 'open', 'medium', NULL)
    """, (task_text,))
    conn.commit()
    cur.close()
    conn.close()
    return f"Added task: {task_text}"


# ---- The webhook ----
@router.post("/sms")
async def sms_webhook(request: Request):
    form = await request.form()
    from_number = form.get("From", "")
    body = (form.get("Body", "") or "").strip()

    # SAFETY GUARD 1: verify the request genuinely came from Twilio
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{PUBLIC_BASE_URL}/sms"
    if not validator.validate(url, dict(form), signature):
        print("[sms] signature validation FAILED — rejecting.")
        return Response(status_code=403)

    # SAFETY GUARD 2: only obey James's allowlisted number
    if from_number != MY_PHONE:
        print(f"[sms] message from non-allowlisted number {from_number} — ignoring.")
        return Response(status_code=204)

    # Confirmation path: did James reply SEND / YES / CANCEL?
    upper = body.upper()
    pending = _get_pending()
    if pending:
        if upper in ("SEND", "YES", "CONFIRM"):
            result = _execute_pending(pending)
            _clear_pending()
            _send_sms_reply(result)
            return Response(status_code=204)
        if upper in ("CANCEL", "NO", "STOP"):
            _clear_pending()
            _send_sms_reply("Cancelled. Nothing was sent.")
            return Response(status_code=204)
        # Any other text replaces the pending action — fall through to re-parse.
        _clear_pending()

    # Parse intent
    try:
        parsed = _classify(body)
    except Exception as e:
        print(f"[sms] classify error: {e}")
        _send_sms_reply("Sorry, I couldn't understand that. Try again.")
        return Response(status_code=204)

    intent = parsed.get("intent")

    if intent == "query":
        answer = _handle_query(parsed.get("question", body))
        _send_sms_reply(answer)
        return Response(status_code=204)

    if intent == "add_task":
        result = _handle_add_task(parsed.get("task", body))
        _send_sms_reply(result)
        return Response(status_code=204)

    if intent == "send_message":
        recipient = parsed.get("recipient", "")
        matches = _find_contact(recipient)
        if not matches:
            _send_sms_reply(f"No contact found matching '{recipient}'. I can only message saved contacts.")
            return Response(status_code=204)
        if len(matches) > 1:
            names = ", ".join(m["name"] for m in matches)
            _send_sms_reply(f"Multiple contacts match '{recipient}': {names}. Be more specific.")
            return Response(status_code=204)

        contact = matches[0]
        channel = parsed.get("channel", "auto")
        # Decide channel based on what's stored + what was requested
        if channel == "sms" and not contact["phone"]:
            _send_sms_reply(f"{contact['name']} has no phone on file. Want me to email instead? Re-text with 'email'.")
            return Response(status_code=204)
        if channel == "email" and not contact["email"]:
            _send_sms_reply(f"{contact['name']} has no email on file.")
            return Response(status_code=204)
        if channel == "auto":
            channel = "email" if contact["email"] else ("sms" if contact["phone"] else None)
        if not channel:
            _send_sms_reply(f"{contact['name']} has no email or phone on file.")
            return Response(status_code=204)

        draft = _draft_message(contact["name"], parsed.get("message_gist", ""))
        _set_pending({
            "type": "send",
            "channel": channel,
            "contact": contact,
            "draft": draft,
        })
        _send_sms_reply(
            f"Draft {channel} to {contact['name']}:\n\n{draft}\n\nReply SEND to confirm or CANCEL."
        )
        return Response(status_code=204)

    _send_sms_reply("I can send a message to a contact, answer a question about your tasks, or add a task. What would you like?")
    return Response(status_code=204)


# ---- Execute a confirmed pending action ----
def _execute_pending(pending):
    if pending.get("type") != "send":
        return "Nothing to send."
    contact = pending["contact"]
    draft = pending["draft"]
    channel = pending["channel"]

    if channel == "email":
        try:
            resend.Emails.send({
                "from": EMAIL_FROM,
                "to": contact["email"],
                "subject": f"Message from James Galvin — Miami Coastline",
                "text": draft,
            })
            return f"Sent email to {contact['name']}."
        except Exception as e:
            return f"Failed to send email: {e}"

    if channel == "sms":
        try:
            client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.messages.create(body=draft, from_=TWILIO_NUMBER, to=contact["phone"])
            return f"Sent text to {contact['name']}."
        except Exception as e:
            return f"Couldn't send text (Twilio may still be in trial / pending 10DLC): {e}"

    return "Unknown channel."