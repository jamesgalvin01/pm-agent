"""
telegram_setup.py — one-time helper to wire Rowan's Telegram bot.

Before running:
  1. In Telegram, message @BotFather -> /newbot. Put the token in .env as
     TELEGRAM_BOT_TOKEN.
  2. Open your bot in Telegram and send it any message ("hi").

Then run:
    cd ~/pm-agent && source venv/bin/activate && python telegram_setup.py

It finds your chat id, registers the webhook with a secret, and prints the
variables to set on Railway.

Note: the bot token is never printed in an error. Telegram puts it in the URL,
so raw exceptions would leak it.
"""
import os
import secrets
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _scrub(text: str) -> str:
    return str(text).replace(BOT_TOKEN, "<token>") if BOT_TOKEN else str(text)


def call(method: str, payload=None):
    """One Telegram call. Never lets the token reach the console."""
    try:
        if payload is None:
            resp = requests.get(f"{API}/{method}", timeout=20)
        else:
            resp = requests.post(f"{API}/{method}", json=payload, timeout=20)
        return resp.json()
    except Exception as e:
        sys.exit(f"{method} failed: {_scrub(e)}")


def collect_chats(updates):
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats[str(chat["id"])] = (
                chat.get("username") or chat.get("first_name") or "you"
            )
    return chats


def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN missing from .env")
    if not PUBLIC_BASE_URL:
        sys.exit("PUBLIC_BASE_URL missing from .env")

    me = call("getMe")
    if not me.get("ok"):
        sys.exit(f"Token rejected: {_scrub(me.get('description'))}")
    print(f"Bot: @{me['result'].get('username')}")

    # A webhook already pointed somewhere swallows updates, so getUpdates
    # comes back empty. Check before blaming the message.
    info = call("getWebhookInfo").get("result", {}) or {}
    existing = info.get("url") or ""
    pending = info.get("pending_update_count", 0)
    if existing:
        print(f"A webhook is already registered: {_scrub(existing)}")
        print(f"Pending updates held for it: {pending}")
        print("Removing it so I can read your chat id...")
        call("deleteWebhook", {"drop_pending_updates": False})
    if info.get("last_error_message"):
        print(f"Telegram's last delivery error: {_scrub(info['last_error_message'])}")

    updates = call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    if not updates.get("ok"):
        sys.exit(f"getUpdates failed: {_scrub(updates.get('description'))}")
    chats = collect_chats(updates.get("result", []))

    chat_id = ""
    if len(chats) == 1:
        chat_id, who = next(iter(chats.items()))
        print(f"Chat id: {chat_id}  ({who})")
    elif len(chats) > 1:
        print("\nSeveral chats have messaged this bot:")
        for cid, who in chats.items():
            print(f"  {cid}  ({who})")
        chat_id = input("Which chat id is yours? ").strip()
    else:
        print("\nStill no messages queued for this bot.")
        print("Two ways forward:")
        print("  a) Send the bot another message right now, then re-run this.")
        print("  b) Enter your chat id by hand. To find it, message @userinfobot")
        print("     in Telegram - it replies with your numeric Id.")
        chat_id = input("\nChat id (or press Enter to stop and retry later): ").strip()
        if not chat_id:
            sys.exit("Stopped. Nothing was changed.")
    if not chat_id.lstrip("-").isdigit():
        sys.exit(f"'{chat_id}' doesn't look like a chat id (digits only).")

    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)
    resp = call("setWebhook", {
        "url": f"{PUBLIC_BASE_URL}/telegram",
        "secret_token": secret,
        "allowed_updates": ["message", "edited_message", "callback_query"],
        "drop_pending_updates": True,
    })
    if not resp.get("ok"):
        sys.exit(f"setWebhook failed: {_scrub(resp.get('description'))}")
    print(f"Webhook registered: {PUBLIC_BASE_URL}/telegram")

    # Prove the whole path works end to end.
    hello = call("sendMessage", {
        "chat_id": chat_id,
        "text": "Rowan is connected. This is where your reply approvals will land.",
    })
    if hello.get("ok"):
        print("Sent you a test message - check Telegram.")
    else:
        print(f"Could not send a test message: {_scrub(hello.get('description'))}")
        print("(If this says 'chat not found', the chat id is wrong.)")

    print("\n" + "=" * 62)
    print("Set these on the Railway pm-agent service, then redeploy:\n")
    print(f"  TELEGRAM_BOT_TOKEN={BOT_TOKEN}")
    print(f"  TELEGRAM_CHAT_ID={chat_id}")
    print(f"  TELEGRAM_WEBHOOK_SECRET={secret}")
    print("  APPROVAL_CHANNEL=telegram")
    print("\nAlso set APPROVAL_CHANNEL=telegram on the worker service.")
    print("=" * 62)


if __name__ == "__main__":
    main()
