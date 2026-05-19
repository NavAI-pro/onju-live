"""Telegram MTProto integration via Telethon.

Provides a singleton TelegramClient bound to the user's account, plus two
high-level helpers used by the LLM tools:

  - find_contact(query): look up a user/chat by name, username, or phone.
  - send_message(target, text): send a message as the user.

First-time login is interactive (Telegram sends a code by SMS / official
Telegram client) and must happen via `scripts/telegram_login.py`. After
that the session is persisted to `data/telegram.session` and subsequent
runs reconnect non-interactively.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.types import User, Chat, Channel

log = logging.getLogger(__name__)

SESSION_PATH = "data/telegram.session"

_client: TelegramClient | None = None
_client_lock = asyncio.Lock()


def _creds() -> tuple[int, str] | None:
    """Return (api_id, api_hash) or None if creds aren't configured."""
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        return None
    try:
        return int(api_id), api_hash
    except ValueError:
        log.warning(f"TELEGRAM_API_ID is not an integer: {api_id!r}")
        return None


async def get_client() -> TelegramClient | None:
    """Return a connected, authorised client. None if not yet logged in."""
    global _client
    async with _client_lock:
        if _client is not None and _client.is_connected():
            return _client
        creds = _creds()
        if not creds:
            log.info("Telegram creds missing — tools will report unconfigured.")
            return None
        api_id, api_hash = creds
        os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
        client = TelegramClient(SESSION_PATH, api_id, api_hash)
        try:
            await client.connect()
        except Exception as e:
            log.warning(f"Telegram connect failed: {e}")
            return None
        if not await client.is_user_authorized():
            log.warning(
                "Telegram session not authorised. Run "
                "`python scripts/telegram_login.py` once to log in."
            )
            await client.disconnect()
            return None
        _client = client
        log.info("Telegram client connected.")
        return _client


def _entity_summary(e: Any) -> dict[str, Any]:
    if isinstance(e, User):
        name = " ".join(filter(None, [e.first_name, e.last_name])) or e.username or str(e.id)
        return {
            "id": e.id, "type": "user", "name": name,
            "username": e.username, "phone": e.phone, "bot": e.bot,
        }
    if isinstance(e, (Chat, Channel)):
        return {
            "id": e.id, "type": "channel" if isinstance(e, Channel) else "chat",
            "name": getattr(e, "title", None) or str(e.id),
            "username": getattr(e, "username", None),
        }
    return {"id": getattr(e, "id", None), "type": type(e).__name__,
            "name": str(getattr(e, "title", e) if hasattr(e, "title") else e)}


async def find_contact(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search for users/chats matching `query`. Scans recent dialogs and
    Telegram global search."""
    client = await get_client()
    if client is None:
        return []
    query_lc = query.strip().lower()
    results: dict[int, dict[str, Any]] = {}

    # 1) Direct username/phone lookup (cheap)
    try:
        entity = await client.get_entity(query)
        d = _entity_summary(entity)
        if d.get("id") is not None:
            results[d["id"]] = d
    except Exception:
        pass

    # 2) Recent dialogs (in-cache, fast). Match by case-insensitive substring.
    try:
        async for dlg in client.iter_dialogs(limit=200):
            name = (dlg.name or "").lower()
            ent = dlg.entity
            uname = (getattr(ent, "username", "") or "").lower()
            if query_lc in name or (uname and query_lc in uname):
                d = _entity_summary(ent)
                if d.get("id") is not None:
                    results[d["id"]] = d
                    if len(results) >= limit:
                        break
    except Exception as e:
        log.debug(f"telegram dialog scan failed: {e}")

    return list(results.values())[:limit]


async def _resolve_entity(client: TelegramClient, target: str | int):
    """Resolve a target string/id to a Telethon entity, walking dialogs if
    Telethon's cache doesn't have an access_hash for a bare numeric id yet."""
    # Try direct lookup (works for @username, +phone, or numeric if cached).
    try:
        return await client.get_entity(target)
    except (ValueError, RPCError):
        pass
    # If it's a numeric id, refresh dialog cache once and retry.
    try:
        tid = int(target)
    except (TypeError, ValueError):
        tid = None
    if tid is not None:
        async for dlg in client.iter_dialogs(limit=500):
            ent = dlg.entity
            if getattr(ent, "id", None) == tid:
                return ent
    return None


async def send_message(target: str | int, text: str) -> dict[str, Any]:
    """Send `text` as the user to `target` (username, phone, or numeric id)."""
    client = await get_client()
    if client is None:
        return {"error": "Telegram not configured / not logged in. "
                         "Run scripts/telegram_login.py first."}
    entity = await _resolve_entity(client, target)
    if entity is None:
        return {"error": f"Could not find target {target!r} in recent dialogs."}
    try:
        msg = await client.send_message(entity, text)
    except RPCError as e:
        return {"error": f"send_message failed: {e}"}
    info = _entity_summary(entity)
    return {"ok": True, "to": info, "message_id": msg.id, "text": text}
