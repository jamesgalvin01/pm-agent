"""
telegram_setup.py — one-time helper to wire Rowan's Telegram bot.

Before running:
  1. In Telegram, message @BotFather -> /newbot -> name it (e.g. "Rowan").
     He gives you a token like 8123456789:AAH...  Put it in .env as
     TELEGRAM_BOT_TOKEN, and set the same value in Railway.
  2. Open your new bot in Telegram and send it any message ("hi").

Then run:
    cd ~/pm-agent && source venv/bin/activate && python telegram_setup.py

It finds your chat id, registers the webhook with a secret, and prints the
three variables to set on the Railway pm-agent service.
"""
import os
import secrets
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN missing from .env — get one from @BotFather first.")
    if not PUBLIC_BASE_URL:
        sys.exit("PUBLIC_BASE_URL missing from .env "
                 "(e.g. https://pm-agent-production-bd91.up.railway.app)")

    api = f"https://api.telegram.org/bot{BOT_TOKEN}"

    me = requests.get(f"{api}/getMe", timeout=20).json()
    if not me.get("ok"):
        sys.exit(f"Token rejected: {me}")
    print(f"Bot: @{me['result'].get('username')}")

    updates = requests.get(f"{api}/getUpdates", timeout=20).json()
    if not updates.get("ok"):
        sys.exit(f"getUpdates failed: {updates}")

    chats = {}
    for u in updates.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            who = chat.get("username") or chat.get("first_name") or "you"
            chats[str(chat["id"])] = who

    if not chats:
        sys.exit("No messages found. Open the bot in Telegram, send it 'hi', "
                 "then run this again.\n(If you already did, note that Telegram "
                 "drops pending updates once a webhook is registered — send a "
                 "fresh message and retry.)")

    if len(chats) == 1:
        chat_id, who = next(iter(chats.items()))
    else:
        print("\nSeveral chats have messaged this bot:")
        for cid, who in chats.items():
            print(f"  {cid}  ({who})")
        chat_id = input("Which chat id is yours? ").strip()
        who = chats.get(chat_id, "")
    print(f"Chat id: {chat_id}  ({who})")

    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)

    resp = requests.post(
        f"{api}/setWebhook",
        json={
            "url": f"{PUBLIC_BASE_URL}/telegram",
            "secret_token": secret,
            "allowed_updates": ["message", "edited_message", "callback_query"],
            "drop_pending_updates": True,
        },
        timeout=20,
    ).json()
    if not resp.get("ok"):
        sys.exit(f"setWebhook failed: {resp}")
    print(f"Webhook registered: {PUBLIC_BASE_URL}/telegram")

    print("\n" + "=" * 62)
    print("Set these on the Railway pm-agent service, then redeploy:\n")
    print(f"  TELEGRAM_BOT_TOKEN={BOT_TOKEN}")
    print(f"  TELEGRAM_CHAT_ID={chat_id}")
    print(f"  TELEGRAM_WEBHOOK_SECRET={secret}")
    print("  APPROVAL_CHANNEL=telegram")
    print("\nAlso set APPROVAL_CHANNEL=telegram on the worker service, so the")
    print("8:15/1pm draft pass sends you the first one.")
    print("=" * 62)


if __name__ == "__main__":
    main()
