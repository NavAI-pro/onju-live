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


# ─── get_weather ────────────────────────────────────────────────────────────

# Compact mapping of Open-Meteo WMO weather codes → short Uzbek descriptor.
# The LLM sees this and phrases the actual sentence naturally.
_WMO_UZ: dict[int, str] = {
    0: "ochiq", 1: "asosan ochiq", 2: "qisman bulutli", 3: "bulutli",
    45: "tumanli", 48: "qirovli tuman",
    51: "yengil yomgir", 53: "yomgir", 55: "kuchli yomgir",
    61: "yomgir", 63: "kuchli yomgir", 65: "juda kuchli yomgir",
    71: "yengil qor", 73: "qor", 75: "kuchli qor",
    77: "qor parchasi",
    80: "yomg'irli", 81: "kuchli yomg'irli", 82: "juda kuchli yomg'irli",
    85: "qorli", 86: "kuchli qorli",
    95: "momaqaldiroq", 96: "do'l bilan momaqaldiroq", 99: "kuchli momaqaldiroq",
}


async def _geocode(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    try:
        r = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en", "format": "json"},
            timeout=6.0,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            return None
        g = results[0]
        return {
            "name": g.get("name"),
            "country": g.get("country"),
            "lat": g["latitude"],
            "lon": g["longitude"],
            "tz": g.get("timezone", "auto"),
        }
    except Exception as e:
        log.debug(f"geocode {name!r} failed: {e}")
        return None


async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
    """Return current conditions + today's / tomorrow's forecast for a city.

    Data source: Open-Meteo (no API key required). LLM is expected to phrase
    the spoken reply in Uzbek with numbers spelled out.
    """
    location = (args.get("location") or "").strip()
    when = (args.get("when") or "today").strip().lower()
    if not location:
        return {"error": "Missing 'location' argument."}
    if when in {"bugun", "today", ""}:
        day_index = 0
        day_label = "bugun"
    elif when in {"ertaga", "tomorrow"}:
        day_index = 1
        day_label = "ertaga"
    else:
        return {"error": f"'when' must be 'today' or 'tomorrow' (got {when!r})."}

    async with httpx.AsyncClient(headers={"User-Agent": "onju-voice/1.0"}) as client:
        geo = await _geocode(client, location)
        if not geo:
            return {"error": f"Joy topilmadi: {location!r}"}

        try:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": geo["lat"],
                    "longitude": geo["lon"],
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
                timeout=8.0,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return {"error": f"Open-Meteo so'rovi muvaffaqiyatsiz: {e}"}

    cur = data.get("current") or {}
    daily = data.get("daily") or {}

    def _daily(key: str):
        arr = daily.get(key) or []
        return arr[day_index] if len(arr) > day_index else None

    code = _daily("weather_code")
    result = {
        "location": geo["name"],
        "country": geo["country"],
        "when": day_label,
        "current": {
            "temperature_c": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "condition_code": cur.get("weather_code"),
            "condition_uz": _WMO_UZ.get(cur.get("weather_code"), "noma'lum"),
        } if day_index == 0 else None,
        "forecast": {
            "date": (daily.get("time") or [None, None, None])[day_index],
            "high_c": _daily("temperature_2m_max"),
            "low_c": _daily("temperature_2m_min"),
            "precipitation_mm": _daily("precipitation_sum"),
            "wind_max_kmh": _daily("wind_speed_10m_max"),
            "condition_code": code,
            "condition_uz": _WMO_UZ.get(code, "noma'lum"),
        },
    }
    log.info(
        f"TOOL get_weather({location!r}, when={day_label}) -> {geo['name']} "
        f"{result['forecast']['low_c']}..{result['forecast']['high_c']}°C "
        f"{result['forecast']['condition_uz']}"
    )
    return result


# ─── telegram ───────────────────────────────────────────────────────────────

async def telegram_find_contact(args: dict[str, Any]) -> dict[str, Any]:
    """Look up Telegram contacts/chats matching a name, @username or phone."""
    from pipeline.services import telegram as tg
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "Missing 'query'."}
    results = await tg.find_contact(query, limit=int(args.get("limit", 5) or 5))
    log.info(f"TOOL telegram_find_contact({query!r}) -> {len(results)} match(es)")
    return {"query": query, "count": len(results), "matches": results}


async def telegram_send_message(args: dict[str, Any]) -> dict[str, Any]:
    """Send a Telegram message AS THE USER. Caller (the LLM) must verbally
    confirm with the user before invoking this."""
    from pipeline.services import telegram as tg
    target = args.get("target")
    text = (args.get("text") or "").strip()
    if not target:
        return {"error": "Missing 'target' (username, phone, or id)."}
    if not text:
        return {"error": "Missing 'text'."}
    res = await tg.send_message(target, text)
    if res.get("ok"):
        log.info(f"TOOL telegram_send_message -> {res['to'].get('name')} ({len(text)} chars)")
    else:
        log.warning(f"TOOL telegram_send_message FAILED: {res.get('error')}")
    return res


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
    "get_weather": {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Get current weather and today's / tomorrow's forecast for a "
                "city. Data is from Open-Meteo. Call whenever the user asks "
                "about ob-havo (weather), harorat (temperature), yomg'ir "
                "(rain), shamol (wind), etc. After the tool returns, compose "
                "a brief Uzbek reply (1-2 sentences) using the returned data; "
                "spell every number in Uzbek words per the voice-output rules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or place name (e.g. 'Toshkent', 'Samarqand', 'Tokyo').",
                    },
                    "when": {
                        "type": "string",
                        "enum": ["today", "tomorrow"],
                        "description": "Default 'today'. Use 'tomorrow' for 'ertaga' / 'kelasi kun'.",
                    },
                },
                "required": ["location"],
            },
        },
    },
    "telegram_find_contact": {
        "type": "function",
        "function": {
            "name": "telegram_find_contact",
            "description": (
                "Look up a Telegram contact or chat by name, @username, or "
                "phone number. Use FIRST when the user wants to message "
                "someone whose Telegram identifier you don't yet know. "
                "Returns up to N matches from recent dialogs + global search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-form name, @username, or phone number.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max matches (1-10). Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    "telegram_send_message": {
        "type": "function",
        "function": {
            "name": "telegram_send_message",
            "description": (
                "Send a Telegram message AS THE USER (not a bot). HIGH-IMPACT: "
                "the message will appear from the user's account in real time, "
                "visible to the recipient. BEFORE calling this, ALWAYS read "
                "back the recipient name and the proposed message text in "
                "Uzbek and wait for an explicit confirmation like 'ha, yubor', "
                "'yes send', or 'oʻq, yubor'. Only call after the user clearly "
                "confirms. If the user asks to change the wording or recipient, "
                "re-propose and re-confirm before sending."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Recipient: @username, phone, or numeric id from telegram_find_contact.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message body, in the language requested by the user.",
                    },
                },
                "required": ["target", "text"],
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
    "get_weather": get_weather,
    "set_volume": set_volume,
    "telegram_find_contact": telegram_find_contact,
    "telegram_send_message": telegram_send_message,
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
