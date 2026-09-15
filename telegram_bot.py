"""
telegram_bot.py — Telegram interface for Rowan's approvals.

Replaces SMS for the email-reply approval loop. No carrier registration, no
10DLC campaign: a bot token and James's chat id are all it takes.

Two hard safety rules, same as the SMS path:
  1. Telegram's webhook secret must match, or the request is rejected.
  2. Only TELEGRAM_CHAT_ID is ever obeyed.

Setup: run telegram_setup.py once. It prints your chat id and registers the
webhook with a secret.
"""
import os
import requests
from fastapi import APIRouter, Request, Response

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

router = APIRouter()


def _keyboard(draft_id):
    return {
        "inline_keyboard": [[
            {"text": "Send", "callback_data": f"send:{draft_id}"},
            {"text": "Discard", "callback_data": f"no:{draft_id}"},
            {"text": "Later", "callback_data": f"later:{draft_id}"},
        ]]
    }


def send_message(text: str, draft_id=None) -> bool:
    """Send James a message. Buttons are attached when it needs an answer."""
    if not (BOT_TOKEN and CHAT_ID):
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; not sending.")
        return False
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if draft_id is not None:
        payload["reply_markup"] = _keyboard(draft_id)
        payload["text"] = text + "\n\nTap a button, or type a rewrite."
    try:
        resp = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
        if resp.status_code >= 400:
            print(f"[telegram] sendMessage failed [{resp.status_code}]: {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[telegram] sendMessage error: {e}")
        return False


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


BUTTON_WORDS = {"send": "SEND", "no": "NO", "later": "LATER"}


@router.post("/telegram")
async def telegram_webhook(request: Request):
    # GUARD 1: the secret Telegram was told to send with every update.
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            print("[telegram] bad webhook secret — rejecting.")
            return Response(status_code=403)

    update = await request.json()

    # A tapped button.
    callback = update.get("callback_query")
    if callback:
        chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
        # GUARD 2
        if chat_id != CHAT_ID:
            print(f"[telegram] callback from non-allowlisted chat {chat_id} — ignoring.")
            return Response(status_code=204)
        data = callback.get("data") or ""
        action = data.split(":", 1)[0]
        word = BUTTON_WORDS.get(action)
        _answer_callback(callback.get("id", ""), "Working on it...")
        if word:
            from approvals import handle_response
            handle_response(word)
        return Response(status_code=204)

    # A typed message.
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = (message.get("text") or "").strip()
    # GUARD 2
    if chat_id != CHAT_ID:
        print(f"[telegram] message from non-allowlisted chat {chat_id} — ignoring.")
        return Response(status_code=204)
    if not text:
        return Response(status_code=204)

    from approvals import handle_response, notify_next_email_draft

    if text.lower() in ("/start", "/help"):
        send_message(
            "Rowan here. When a reply is waiting I'll send it with Send / "
            "Discard / Later buttons. You can also type a rewrite and I'll "
            "read it back before anything goes out.\n\n"
            "/pending - send me the next draft waiting for approval"
        )
        return Response(status_code=204)

    if text.lower() == "/pending":
        if not notify_next_email_draft():
            send_message("Nothing waiting on you.")
        return Response(status_code=204)

    if handle_response(text):
        return Response(status_code=204)

    send_message("Nothing is waiting on your approval right now.")
    return Response(status_code=204)
