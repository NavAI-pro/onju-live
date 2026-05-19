#!/usr/bin/env python3
"""One-time Telegram MTProto login.

Usage:
    python scripts/telegram_login.py

Reads TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE from .env (or env),
prompts for the SMS/Telegram-app code (and 2FA password if set), and writes
the session to data/telegram.session. After this, the pipeline can call
Telegram tools non-interactively.
"""

import asyncio
import getpass
import os
import sys

from telethon import TelegramClient


def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), os.pardir, ".env")
    if not os.path.exists(env_path):
        return
    for line in open(env_path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)


async def main():
    _load_env()
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    phone = os.environ.get("TELEGRAM_PHONE") or input("Phone (e.g. +998...): ").strip()
    if not (api_id and api_hash):
        sys.exit("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")

    os.makedirs("data", exist_ok=True)
    client = TelegramClient("data/telegram.session", int(api_id), api_hash)
    await client.start(
        phone=lambda: phone,
        code_callback=lambda: input("Enter the code Telegram sent: ").strip(),
        password=lambda: getpass.getpass("2FA password (blank if none): "),
    )
    me = await client.get_me()
    name = " ".join(filter(None, [me.first_name, me.last_name])) or me.username
    print(f"Logged in as {name} (id={me.id})")
    print(f"Session saved to data/telegram.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
