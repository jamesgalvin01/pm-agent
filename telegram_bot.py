"""
telegram_bot.py — Rowan on Telegram.

What James can do from @miamirowan_bot:
  - talk to Rowan (the full agent: tasks, projects, people, leads, notes,
    files and his Outlook calendar); changes come back as a proposal with
    Confirm / Cancel buttons
  - send a voice note: transcribed, then turned into proposed tasks and notes
  - send a site photo or a PDF: Rowan reads it, files it to OneDrive under
    the project, and replies (a review, for documents)
  - /task /lead /note: one-line capture, confirmed with one tap
  - approve email replies: Send / Edit / Discard / Later

Safety rules:
  1. Telegram's webhook secret must match, or the request is rejected.
  2. Only TELEGRAM_CHAT_ID is ever obeyed.
  3. Nothing changes (task, lead, event, email) without a Confirm/Send tap
     or a typed go-ahead.
  4. Telegram retries a webhook that doesn't answer quickly, so every update
     is answered at once, slow work runs in the background, and update ids
     are recorded so a retry is never processed twice.

Setup: run telegram_setup.py once. It registers the webhook with a secret.
"""
import json
import os
import threading
import time

import requests
from fastapi import APIRouter, BackgroundTasks, Request, Response

from db import get_connection

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_TEXT = 4000                       # Telegram's hard limit is 4096
EDIT_WINDOW_SECONDS = 600             # after tapping Edit, the next message rewrites the draft
CONFIRM_MARKER = "[CONFIRM]"

router = APIRouter()

HELP_TEXT = (
    "Rowan here. Talk to me like you would on the dashboard: ask what's open, "
    "what's on your calendar, or tell me what to change. I'll show you any change "
    "with Confirm / Cancel buttons before I make it.\n\n"
    "Voice note: I'll transcribe it and pull out tasks and notes.\n"
    "Photo: I'll look at it and file it to OneDrive under the project. Put the project in the caption.\n"
    "PDF: I'll review it as your owner's rep and file it. Add a question in the caption if you have one.\n\n"
    "/task call Greg re: pool tile Friday\n"
    "/lead Jane Doe, developer, Key Largo spec home\n"
    "/note 71 NoBE: owner approved the lobby finish upgrade\n"
    "/scan - check my inbox now and draft anything that needs a reply\n"
    "/pending - show the email draft waiting for approval\n"
    "/new - start a fresh conversation (clears my short-term context)"
)


# ============================================================
# SMALL STATE STORE + UPDATE DEDUP
# ============================================================

def _ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_state (
            key TEXT PRIMARY KEY,
            value JSONB,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_updates (
            update_id BIGINT PRIMARY KEY,
            received_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


_tables_ready = False


def _conn():
    global _tables_ready
    conn = get_connection()
    if not _tables_ready:
        cur = conn.cursor()
        _ensure_tables(cur)
        conn.commit()
        cur.close()
        _tables_ready = True
    return conn


def state_get(key: str):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM telegram_state WHERE key = %s", (key,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def state_set(key: str, value) -> None:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO telegram_state (key, value, updated_at) VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    """, (key, json.dumps(value)))
    conn.commit()
    cur.close()
    conn.close()


def _first_time(update_id) -> bool:
    """True the first time we see this update id. Fails open."""
    if update_id is None:
        return True
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO telegram_updates (update_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (int(update_id),),
        )
        fresh = cur.rowcount == 1
        # Keep the table small.
        cur.execute("DELETE FROM telegram_updates WHERE received_at < NOW() - INTERVAL '3 days'")
        conn.commit()
        cur.close()
        conn.close()
        return fresh
    except Exception as e:
        print(f"[telegram] dedup check failed ({e}); processing anyway.")
        return True


# ============================================================
# SENDING
# ============================================================

def _draft_keyboard(draft_id):
    return {
        "inline_keyboard": [
            [
                {"text": "Send", "callback_data": f"send:{draft_id}"},
                {"text": "Edit", "callback_data": f"edit:{draft_id}"},
            ],
            [
                {"text": "Discard", "callback_data": f"no:{draft_id}"},
                {"text": "Later", "callback_data": f"later:{draft_id}"},
            ],
        ]
    }


def _chunks(text: str) -> list:
    text = text or ""
    parts = []
    while len(text) > MAX_TEXT:
        cut = text.rfind("\n", 0, MAX_TEXT)
        if cut < MAX_TEXT // 2:
            cut = MAX_TEXT
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def send_message(text: str, draft_id=None, reply_markup=None):
    """
    Send James a message. Returns the Telegram message id of the last part
    (truthy) or None. Buttons go on the last part.
    """
    if not (BOT_TOKEN and CHAT_ID):
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; not sending.")
        return None
    if draft_id is not None:
        reply_markup = _draft_keyboard(draft_id)
        text = text + "\n\nTap a button. Edit lets you say what to change."
    parts = _chunks(text)
    message_id = None
    for i, part in enumerate(parts):
        payload = {"chat_id": CHAT_ID, "text": part or "(empty)", "disable_web_page_preview": True}
        if reply_markup and i == len(parts) - 1:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
            if resp.status_code >= 400:
                print(f"[telegram] sendMessage failed [{resp.status_code}]: {resp.text[:300]}")
                return None
            message_id = (resp.json().get("result") or {}).get("message_id")
        except Exception as e:
            print(f"[telegram] sendMessage error: {e}")
            return None
    if draft_id is not None and message_id:
        try:
            state_set("draft_message", {"draft_id": draft_id, "message_id": message_id})
        except Exception as e:
            print(f"[telegram] could not record draft message id: {e}")
    return message_id


def _typing() -> None:
    try:
        requests.post(f"{API}/sendChatAction",
                      json={"chat_id": CHAT_ID, "action": "typing"}, timeout=10)
    except Exception:
        pass


def _answer_callback(callback_id: str, text: str = "") -> None:
    """Clears the spinner on the tapped button."""
    try:
        requests.post(
            f"{API}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:200]},
            timeout=10,
        )
    except Exception as e:
        print(f"[telegram] answerCallbackQuery error: {e}")


def _remove_buttons(message_id) -> None:
    if not message_id:
        return
    try:
        requests.post(
            f"{API}/editMessageReplyMarkup",
            json={"chat_id": CHAT_ID, "message_id": message_id,
                  "reply_markup": {"inline_keyboard": []}},
            timeout=10,
        )
    except Exception:
        pass


def _link_button(url: str, label: str = "Open in OneDrive"):
    return {"inline_keyboard": [[{"text": label, "url": url}]]} if url else None


# ============================================================
# CONVERSATION WITH THE AGENT
# ============================================================

# One agent turn at a time, so two quick messages can't interleave in the
# conversation history.
_turn_lock = threading.Lock()


def _new_conversation() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (title, status) VALUES (%s, %s) RETURNING id",
        ("Telegram", "active"),
    )
    conv_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    state_set("conversation_id", conv_id)
    return conv_id


def _conversation_id() -> int:
    conv_id = state_get("conversation_id")
    if conv_id:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM conversations WHERE id = %s", (conv_id,))
        exists = cur.fetchone()
        cur.close()
        conn.close()
        if exists:
            return int(conv_id)
    return _new_conversation()


def _touch_conversation(conv_id: int) -> None:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE conversations SET last_message_at = NOW() WHERE id = %s", (conv_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[telegram] could not touch conversation: {e}")


def _latest_assistant_id(conv_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM messages
         WHERE conversation_id = %s AND role = 'assistant'
         ORDER BY created_at DESC, id DESC LIMIT 1
    """, (conv_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def run_agent(text: str) -> None:
    """One agent turn; the reply goes to James, with buttons if it's a proposal."""
    with _turn_lock:
        _typing()
        try:
            from rowan_agent import run_agent_turn
            conv_id = _conversation_id()
            reply = run_agent_turn(conv_id, text, channel="telegram")
            _touch_conversation(conv_id)
        except Exception as e:
            print(f"[telegram] agent turn failed: {e}")
            send_message(f"Something broke on my end: {str(e)[:200]}")
            return

        markup = None
        if CONFIRM_MARKER in reply:
            reply = reply.replace(CONFIRM_MARKER, "").strip()
            msg_id = _latest_assistant_id(conv_id)
            markup = {"inline_keyboard": [[
                {"text": "Confirm", "callback_data": f"ok:{msg_id}"},
                {"text": "Cancel", "callback_data": f"cancel:{msg_id}"},
            ]]}
        send_message(reply, reply_markup=markup)


def confirm_proposal(msg_id: str, approve: bool) -> None:
    """A Confirm/Cancel tap. Only the most recent proposal can be confirmed."""
    try:
        conv_id = _conversation_id()
        latest = _latest_assistant_id(conv_id)
    except Exception as e:
        send_message(f"Couldn't check that proposal: {str(e)[:200]}")
        return
    if str(latest) != str(msg_id):
        send_message("That proposal is out of date, so I didn't act on it. Ask me again.")
        return
    run_agent("yes" if approve else "No, cancel that. Don't make that change.")


# ============================================================
# EMAIL DRAFTS (approve / edit)
# ============================================================

def _edit_target(message: dict):
    """
    If this message is meant for the email draft awaiting approval, return
    that draft id. It is when James tapped Edit in the last 10 minutes, or
    used Telegram's reply on the draft message.
    """
    from approvals import get_pending
    pending = get_pending()
    if not pending or pending.get("type") != "email_reply":
        return None
    draft_id = pending.get("draft_id")

    edit = state_get("edit_mode") or {}
    if edit.get("draft_id") == draft_id and time.time() < float(edit.get("until", 0)):
        return draft_id

    replied_to = (message.get("reply_to_message") or {}).get("message_id")
    draft_msg = state_get("draft_message") or {}
    if replied_to and replied_to == draft_msg.get("message_id"):
        return draft_id
    return None


def apply_draft_answer(text: str) -> None:
    from approvals import handle_response
    state_set("edit_mode", {})
    try:
        if not handle_response(text):
            send_message("That draft isn't waiting any more. Nothing was sent.")
    except Exception as e:
        print(f"[telegram] draft answer failed: {e}")
        send_message(f"Couldn't apply that to the draft: {str(e)[:200]}")


# A scan is slow (Graph, then a Claude call per message), so it runs in the
# background and this lock stops a second one starting.
_scan_lock = threading.Lock()


def run_inbox_scan():
    """Scan the inbox, then send the first draft. Reports back either way."""
    if not _scan_lock.acquire(blocking=False):
        send_message("Already checking your inbox - hang on.")
        return
    try:
        from email_replies import run_reply_draft_pass
        from approvals import get_pending, notify_next_email_draft

        stats = run_reply_draft_pass()

        if notify_next_email_draft():
            return                      # the draft itself is the reply
        if get_pending():
            send_message("Checked. There's still one waiting on your answer above.")
            return
        send_message(
            f"Checked {stats.get('scanned', 0)} messages - nothing needs a reply from you."
            f"\n{stats.get('skipped', 0)} filtered"
            + (f", {stats['errors']} errored" if stats.get("errors") else "")
        )
    except Exception as e:
        print(f"[telegram] inbox scan failed: {e}")
        send_message(f"Inbox check failed: {str(e)[:200]}")
    finally:
        _scan_lock.release()


# ============================================================
# MEDIA
# ============================================================

def handle_voice(file_id: str, file_size: int, filename: str, draft_id) -> None:
    from telegram_media import MediaError, download, transcribe
    _typing()
    try:
        audio, path = download(file_id, file_size)
        transcript = transcribe(audio, filename or path.rsplit("/", 1)[-1] or "voice.ogg")
    except MediaError as e:
        send_message(str(e))
        return
    except Exception as e:
        print(f"[telegram] voice failed: {e}")
        send_message(f"Couldn't process that voice note: {str(e)[:200]}")
        return

    send_message(f"Heard: \"{transcript[:1500]}\"")
    if draft_id is not None:
        apply_draft_answer(transcript)
    else:
        run_agent(f"[Voice note]\n{transcript}")


def handle_file(kind: str, file_id: str, file_size: int, mime: str, filename: str, caption: str) -> None:
    from telegram_media import MediaError, download, handle_document, handle_photo
    with _turn_lock:
        _typing()
        try:
            content, path = download(file_id, file_size)
            if kind == "photo":
                result = handle_photo(content, mime or "image/jpeg", caption)
            else:
                result = handle_document(content, mime, filename or path.rsplit("/", 1)[-1], caption)
        except MediaError as e:
            send_message(str(e))
            return
        except Exception as e:
            print(f"[telegram] {kind} failed: {e}")
            send_message(f"Couldn't process that {kind}: {str(e)[:200]}")
            return

        # Keep it in the conversation so "make a task from that" works next.
        try:
            from rowan_agent import save_exchange
            conv_id = _conversation_id()
            label = "Photo" if kind == "photo" else f"Document: {filename or 'file'}"
            save_exchange(conv_id, f"[{label}] {caption or ''}".strip(),
                          f"{result['summary']}\n\n{result['reply']}")
            _touch_conversation(conv_id)
        except Exception as e:
            print(f"[telegram] could not save exchange: {e}")

        send_message(result["reply"], reply_markup=_link_button(result.get("web_url")))


# ============================================================
# WEBHOOK
# ============================================================

CAPTURE_PREFIX = {
    "task": "Quick capture (task)",
    "lead": "Quick capture (lead)",
    "note": "Quick capture (project note)",
}


def _command(text: str):
    """('task', 'call Greg...') for '/task call Greg...'; a lone word counts as a command too."""
    stripped = text.strip()
    if stripped.startswith("/"):
        head, _, rest = stripped[1:].partition(" ")
        return head.split("@", 1)[0].lower(), rest.strip()
    if " " not in stripped:
        return stripped.lower(), ""
    return None, stripped


@router.post("/telegram")
async def telegram_webhook(request: Request, background: BackgroundTasks):
    # GUARD 1: the secret Telegram was told to send with every update.
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            print("[telegram] bad webhook secret — rejecting.")
            return Response(status_code=403)

    update = await request.json()
    done = Response(status_code=204, background=background)

    # ---------- a tapped button ----------
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        # GUARD 2
        if chat_id != CHAT_ID:
            print(f"[telegram] callback from non-allowlisted chat {chat_id} — ignoring.")
            return Response(status_code=204)
        if not _first_time(update.get("update_id")):
            return Response(status_code=204)

        action, _, ref = (callback.get("data") or "").partition(":")
        _answer_callback(callback.get("id", ""), "Got it")
        _remove_buttons(message.get("message_id"))

        if action in ("send", "no", "later"):
            word = {"send": "SEND", "no": "NO", "later": "LATER"}[action]
            background.add_task(apply_draft_answer, word)
        elif action == "edit":
            try:
                draft_id = int(ref)
            except ValueError:
                draft_id = ref
            state_set("edit_mode", {"draft_id": draft_id, "until": time.time() + EDIT_WINDOW_SECONDS})
            send_message(
                "What should change? Type it or send a voice note. "
                "Tell me how (\"firmer\", \"push it to Monday\") or give me the exact wording.",
                reply_markup={"inline_keyboard": [[
                    {"text": "Send as is", "callback_data": f"send:{ref}"},
                    {"text": "Discard", "callback_data": f"no:{ref}"},
                ]]},
            )
        elif action in ("ok", "cancel"):
            background.add_task(confirm_proposal, ref, action == "ok")
        return done

    # ---------- a message ----------
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    # GUARD 2
    if chat_id != CHAT_ID:
        print(f"[telegram] message from non-allowlisted chat {chat_id} — ignoring.")
        return Response(status_code=204)
    if update.get("edited_message"):
        return Response(status_code=204)        # don't re-run a turn for an edit
    if not _first_time(update.get("update_id")):
        return Response(status_code=204)

    caption = (message.get("caption") or "").strip()

    # Voice note or audio file
    voice = message.get("voice") or message.get("audio")
    if voice:
        background.add_task(
            handle_voice, voice.get("file_id"), voice.get("file_size") or 0,
            voice.get("file_name"), _edit_target(message),
        )
        return done

    # Photo (Telegram sends several sizes; the last is the largest)
    if message.get("photo"):
        photo = message["photo"][-1]
        background.add_task(handle_file, "photo", photo.get("file_id"), photo.get("file_size") or 0,
                            "image/jpeg", None, caption)
        return done

    # Any other file
    doc = message.get("document")
    if doc:
        background.add_task(handle_file, "document", doc.get("file_id"), doc.get("file_size") or 0,
                            doc.get("mime_type") or "", doc.get("file_name"), caption)
        return done

    text = (message.get("text") or "").strip()
    if not text:
        return Response(status_code=204)

    cmd, rest = _command(text)

    if cmd in ("start", "help"):
        send_message(HELP_TEXT)
        return done

    if cmd in ("scan", "check", "inbox"):
        send_message("Checking your inbox...")
        background.add_task(run_inbox_scan)
        return done

    if cmd == "pending":
        from approvals import notify_next_email_draft, resend_pending_draft
        # If one is already awaiting an answer, re-send that rather than
        # claiming there's nothing - the queue only ever holds one open.
        if not (resend_pending_draft() or notify_next_email_draft()):
            send_message("Nothing waiting on you.")
        return done

    if cmd == "new":
        _new_conversation()
        send_message("Fresh start. I've cleared my short-term context; your data is untouched.")
        return done

    if cmd in CAPTURE_PREFIX:
        if not rest:
            send_message(f"Add the details after it, e.g. /{cmd} " + {
                "task": "call Greg re: pool tile Friday",
                "lead": "Jane Doe, developer, Key Largo spec home",
                "note": "71 NoBE: owner approved the lobby finish upgrade",
            }[cmd])
            return done
        background.add_task(run_agent, f"{CAPTURE_PREFIX[cmd]}: {rest}")
        return done

    # An answer to the email draft awaiting approval?
    draft_id = _edit_target(message)
    if draft_id is not None:
        background.add_task(apply_draft_answer, text)
        return done
    if cmd in ("send", "discard", "later"):
        from approvals import get_pending
        pending = get_pending()
        if pending and pending.get("type") == "email_reply":
            background.add_task(apply_draft_answer, {"send": "SEND", "discard": "NO", "later": "LATER"}[cmd])
            return done

    # Everything else is a conversation with Rowan.
    background.add_task(run_agent, text)
    return done
