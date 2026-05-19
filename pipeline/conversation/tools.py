"""LLM-callable tools used by `ConversationalBackend`.

Each tool is a plain async function that takes a single dict of arguments
(parsed from the model's JSON `arguments` blob) and returns a JSON-serializable
result. The OpenAI-compatible JSON Schema for the tool lives in `SCHEMAS`.

To add a new tool: append its schema to `SCHEMAS` and register the
implementation in `REGISTRY` at the bottom of the file.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, available_timezones

import httpx

log = logging.getLogger(__name__)

# Set by process_utterances around each LLM stream so device-aware tools
# (set_volume, etc.) can read the active Device without an explicit arg.
# Typed as Any to avoid a circular import on pipeline.device.Device.
current_device: ContextVar[Any | None] = ContextVar("onju_current_device", default=None)


# Common city/country aliases that don't match an IANA name directly.
# IANA timezones are usually "Region/City" with the city in English and
# underscores instead of spaces — these are the failure cases worth handling.
_ALIASES: dict[str, str] = {
    "uk": "Europe/London",
    "england": "Europe/London",
    "britain": "Europe/London",
    "uae": "Asia/Dubai",
    "uzbekistan": "Asia/Tashkent",
    "kazakhstan": "Asia/Almaty",
    "russia": "Europe/Moscow",
    "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul",
    "north korea": "Asia/Pyongyang",
    "vietnam": "Asia/Ho_Chi_Minh",
    "china": "Asia/Shanghai",
    "india": "Asia/Kolkata",
    "japan": "Asia/Tokyo",
    "usa": "America/New_York",
    "united states": "America/New_York",
    "us east": "America/New_York",
    "us west": "America/Los_Angeles",
    "germany": "Europe/Berlin",
    "france": "Europe/Paris",
    "spain": "Europe/Madrid",
    "italy": "Europe/Rome",
    "turkey": "Europe/Istanbul",
    "australia": "Australia/Sydney",
    "brazil": "America/Sao_Paulo",
}


def _resolve_zone(location: str) -> tuple[str | None, list[str]]:
    """Return (best_match, all_matches) for a free-form location string."""
    raw = location.strip()
    if not raw:
        return None, []

    lc = raw.lower()
    if lc in _ALIASES:
        return _ALIASES[lc], [_ALIASES[lc]]

    # IANA names use underscores; users say "new york" or "ho chi minh".
    needle_us = lc.replace(" ", "_")
    needle_nosep = lc.replace(" ", "").replace("_", "")

    zones = available_timezones()
    matches = [tz for tz in zones if needle_us in tz.lower()]
    if not matches:
        matches = [tz for tz in zones if needle_nosep in tz.lower().replace("_", "")]
    if not matches:
        return None, []

    # Prefer the shortest match — usually the city-level zone rather than a
    # nested admin region (e.g. "America/Indiana/Indianapolis").
    best = min(matches, key=lambda tz: (tz.count("/"), len(tz)))
    return best, sorted(matches, key=len)


async def find_timezone(args: dict[str, Any]) -> dict[str, Any]:
    """Look up an IANA timezone for a city or country and report the local
    time there right now."""
    location = (args.get("location") or "").strip()
    if not location:
        return {"error": "Missing 'location' argument."}

    best, matches = _resolve_zone(location)
    if not best:
        return {"error": f"No timezone found matching {location!r}."}

    now = datetime.now(ZoneInfo(best))
    result: dict[str, Any] = {
        "location": location,
        "timezone": best,
        "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "utc_offset": now.strftime("%z"),
        "weekday": now.strftime("%A"),
    }
    if len(matches) > 1:
        # Show up to 4 alternates so the model can disambiguate if asked.
        result["alternatives"] = [m for m in matches if m != best][:4]
    log.info(f"TOOL find_timezone({location!r}) -> {best} ({result['current_time']})")
    return result


# ─── get_kunuz_news ─────────────────────────────────────────────────────────

_KUNUZ_SITEMAP_TPL = "http://kun.uz/sitemap/site-map-news_day_{day}_year_{year}_month_{month}.xml"
_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"', re.IGNORECASE)
_OG_DESC_RE = re.compile(r'<meta\s+property="og:description"\s+content="([^"]*)"', re.IGNORECASE)
_LOC_RE = re.compile(r"<loc>([^<]+)</loc>", re.IGNORECASE)
_LASTMOD_RE = re.compile(r"<lastmod>([^<]+)</lastmod>", re.IGNORECASE)
_URL_DATE_RE = re.compile(r"/news/(\d{4})/(\d{2})/(\d{2})/")
# kun.uz returns the homepage HTML (with this og:title prefix) for unknown
# slugs — we use it to recognise and drop those fall-throughs.
_HOMEPAGE_TITLE_MARKER = "O‘zbekiston va jahon yangiliklari"


def _parse_date_arg(date_str: str | None) -> datetime:
    """Parse a user-supplied date string into a date (Tashkent local time).
    Accepts YYYY-MM-DD or 'today' / None → today."""
    tz = ZoneInfo("Asia/Tashkent")
    if not date_str or date_str.strip().lower() in {"today", "bugun"}:
        return datetime.now(tz)
    s = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"Could not parse date {date_str!r}; use YYYY-MM-DD.")


async def _fetch_article(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    """Pull og:title + og:description from a kun.uz article page."""
    try:
        r = await client.get(url, timeout=8.0, follow_redirects=True)
        if r.status_code != 200:
            return None
    except Exception as e:
        log.debug(f"kunuz fetch {url} failed: {e}")
        return None
    body = r.text
    t = _OG_TITLE_RE.search(body)
    d = _OG_DESC_RE.search(body)
    if not t:
        return None
    return {
        "url": url,
        "title": html.unescape(t.group(1)).strip(),
        "summary": html.unescape(d.group(1)).strip() if d else "",
    }


async def _collect_candidates(client: httpx.AsyncClient, target: datetime) -> list[tuple[str, str]]:
    """Walk the kun.uz daily sitemap(s) and return de-duplicated (url, lastmod)
    pairs whose URL path date matches `target`. We scan the target day's
    sitemap and the next day's sitemap because lastmod can drift past
    midnight when articles are edited."""
    target_path = f"/news/{target:%Y/%m/%d}/"
    # Scan target day and day-after (covers late edits crossing midnight).
    days_to_try = [target]
    next_day = target.fromordinal(target.toordinal() + 1).replace(tzinfo=target.tzinfo)
    days_to_try.append(next_day)

    seen_slugs: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for d in days_to_try:
        url = _KUNUZ_SITEMAP_TPL.format(day=d.day, year=d.year, month=d.month)
        try:
            r = await client.get(url, timeout=8.0, follow_redirects=True)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        locs = _LOC_RE.findall(r.text)
        mods = _LASTMOD_RE.findall(r.text)
        for u, lastmod in zip(locs, mods):
            if "/en/news/" in u or target_path not in u:
                continue
            slug = u.rstrip("?./ ").rsplit("/", 1)[-1]
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            # Normalise URL: drop trailing punctuation that breaks fetches.
            u_clean = u.rstrip("?./ ")
            candidates.append((u_clean, lastmod))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


async def get_kunuz_news(args: dict[str, Any]) -> dict[str, Any]:
    """Return latest kun.uz articles for a given date (default today). Each
    item has title and a short Uzbek summary; the LLM is expected to compose
    the final spoken response from these."""
    try:
        target = _parse_date_arg(args.get("date"))
    except ValueError as e:
        return {"error": str(e)}
    limit = max(1, min(int(args.get("limit", 6) or 6), 12))

    async with httpx.AsyncClient(headers={"User-Agent": "onju-voice/1.0"}) as client:
        candidates = await _collect_candidates(client, target)
        if not candidates:
            log.info(f"TOOL get_kunuz_news(date={target:%Y-%m-%d}) -> 0 candidates")
            return {
                "date": target.strftime("%Y-%m-%d"),
                "source": "kun.uz",
                "count": 0,
                "articles": [],
                "note": "No articles found for that date.",
            }
        candidates = candidates[:limit]
        articles = await asyncio.gather(*[_fetch_article(client, u) for u, _ in candidates])

    items = []
    for (url, lastmod), art in zip(candidates, articles):
        if not art:
            continue
        # Skip fall-throughs to the kun.uz homepage (unknown slugs resolve there).
        if _HOMEPAGE_TITLE_MARKER in art["title"]:
            continue
        art["published_at"] = lastmod
        items.append(art)

    log.info(f"TOOL get_kunuz_news(date={target:%Y-%m-%d}, limit={limit}) -> {len(items)} items")
    return {
        "date": target.strftime("%Y-%m-%d"),
        "source": "kun.uz",
        "count": len(items),
        "articles": items,
    }


# ─── set_volume ─────────────────────────────────────────────────────────────

async def set_volume(args: dict[str, Any]) -> dict[str, Any]:
    """Change the playback volume of the currently-connected speaker.

    The user-facing scale is 0..100; the device's native firmware scale is
    0..20 (where 16 = unity gain). We store 0..100 on the Device and map to
    0..20 at TCP send time in process_utterances.
    """
    device = current_device.get()
    if device is None:
        return {"error": "No active device in this turn."}
    raw = args.get("level")
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return {"error": f"level must be an integer 0-100, got {raw!r}"}
    level = max(0, min(100, level))
    prev = getattr(device, "volume", None)
    device.volume = level
    log.info(f"TOOL set_volume {prev} -> {level} on {device.hostname}")
    return {"ok": True, "volume": level, "previous": prev, "scale": "0-100"}


# Public JSON-Schema descriptors handed to the OpenAI-compatible chat API.
SCHEMAS: dict[str, dict[str, Any]] = {
    "find_timezone": {
        "type": "function",
        "function": {
            "name": "find_timezone",
            "description": (
                "Find the IANA timezone for a city, region, or country and "
                "return the current local time there. Use this whenever the "
                "user asks what time it is somewhere, or which timezone a "
                "place is in."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City, country, or region name. Free-form English (e.g. 'Tashkent', 'New York', 'Japan').",
                    }
                },
                "required": ["location"],
            },
        },
    },
    "set_volume": {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": (
                "Set the playback volume of the speaker the user is talking to. "
                "Use whenever the user says something like 'louder', 'quieter', "
                "'too loud', 'baland' / 'past', 'ovozni kamaytir', or asks for "
                "a specific volume. The scale is 0..100 (0 = mute, 100 = max). "
                "After calling, briefly confirm in Uzbek with the new level."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "description": "Volume on a 0-100 scale. 0=mute, 100=max.",
                    }
                },
                "required": ["level"],
            },
        },
    },
    "get_kunuz_news": {
        "type": "function",
        "function": {
            "name": "get_kunuz_news",
            "description": (
                "Fetch the latest news articles from kun.uz (Uzbek news site). "
                "Returns titles and short Uzbek summaries. Use whenever the "
                "user asks about today's news (bugun, bugungi yangiliklar), "
                "news for a specific date, or asks to be caught up on what "
                "happened. IMPORTANT: for 'today' / 'bugun', OMIT the date "
                "argument — the tool resolves today automatically in the "
                "correct timezone. Only pass a date for explicit past dates "
                "the user names. After receiving the results, compose a "
                "brief spoken summary in Uzbek (2-4 sentences) covering "
                "the most notable items, possibly filtered by topic if "
                "the user asked for a specific area (sport, biznes, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Optional date in YYYY-MM-DD format. OMIT for today.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max articles to return (1-12). Default: 6.",
                    },
                },
            },
        },
    },
}


REGISTRY: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "find_timezone": find_timezone,
    "get_kunuz_news": get_kunuz_news,
    "set_volume": set_volume,
}


def schemas_for(names: list[str]) -> list[dict[str, Any]]:
    """Return the JSON schemas for the named tools, skipping unknown names."""
    out = []
    for name in names:
        if name in SCHEMAS:
            out.append(SCHEMAS[name])
        else:
            log.warning(f"Unknown tool name in config: {name!r} (skipped)")
    return out


async def call(name: str, arguments_json: str) -> str:
    """Dispatch a tool call by name. Returns a JSON string suitable for the
    `content` field of a `role=tool` message."""
    fn = REGISTRY.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Bad JSON arguments: {e}"})
    try:
        result = await fn(args)
    except Exception as e:
        log.exception(f"Tool {name} raised")
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    return json.dumps(result)
