"""Yoshlar Ishlari Agentligi dashboard (yoshlar.navai.pro) analytics client.

The dashboard is a Vite/React SPA backed by a token-cookie API at
`/api/v1/...`. Auth flow:

  1. GET /api/v1/csrf-token -> sets `navai_csrf_token` cookie + returns the
     token. Subsequent state-changing calls must echo it in the
     `x-csrf-token` header.
  2. POST /api/v1/auth/login {email, password} -> sets a session cookie
     (HttpOnly). expires_in ~ 900s.

We keep one shared httpx.AsyncClient with cookie persistence and lazily log
in on first use / on 401. Read-only analytics calls only need the cookie;
we still send the csrf header (the server accepts but doesn't require it
for GETs on its v1 API).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://yoshlar.navai.pro/api/v1"

_client: httpx.AsyncClient | None = None
_csrf: str | None = None
_lock = asyncio.Lock()


def _creds() -> tuple[str, str] | None:
    email = os.environ.get("YOSHLAR_EMAIL")
    password = os.environ.get("YOSHLAR_PASSWORD")
    if not email or not password:
        return None
    return email, password


async def _ensure_session() -> tuple[httpx.AsyncClient, str] | None:
    """Return (client, csrf_token) — logging in on first use. Returns None if
    credentials aren't configured or login failed."""
    global _client, _csrf
    async with _lock:
        if _client is None:
            _client = httpx.AsyncClient(timeout=15.0)
        creds = _creds()
        if not creds:
            log.info("Yoshlar creds missing — tool will report unconfigured.")
            return None
        if _csrf is None:
            r = await _client.get(f"{BASE}/csrf-token")
            r.raise_for_status()
            _csrf = r.json()["data"]["csrfToken"]
            r = await _client.post(
                f"{BASE}/auth/login",
                json={"email": creds[0], "password": creds[1]},
                headers={"x-csrf-token": _csrf},
            )
            if r.status_code != 200:
                log.warning(f"Yoshlar login failed: {r.status_code} {r.text[:200]}")
                _csrf = None
                return None
            log.info("Yoshlar dashboard session established.")
        return _client, _csrf


async def _get(path: str) -> dict[str, Any] | None:
    """GET an /api/v1 path, re-logging in once on 401."""
    sess = await _ensure_session()
    if sess is None:
        return None
    client, csrf = sess
    r = await client.get(f"{BASE}{path}", headers={"x-csrf-token": csrf})
    if r.status_code == 401:
        # Session expired — drop and retry once.
        global _csrf
        _csrf = None
        sess = await _ensure_session()
        if sess is None:
            return None
        client, csrf = sess
        r = await client.get(f"{BASE}{path}", headers={"x-csrf-token": csrf})
    r.raise_for_status()
    body = r.json()
    if not body.get("success", True):
        log.warning(f"Yoshlar {path}: {body}")
        return None
    return body.get("data", body)


def _shape_summary(d: dict[str, Any]) -> dict[str, Any]:
    m = d.get("metrics", {})
    t = d.get("trend", {})
    p = d.get("period", {})
    return {
        "period": p,
        "total_calls": m.get("total_calls"),
        "avg_duration_seconds": m.get("avg_duration_seconds"),
        "contained_rate": round(m.get("contained_rate", 0) * 100, 1),
        "transfer_rate": round(m.get("transfer_rate", 0) * 100, 1),
        "transferred_count": m.get("transferred_count"),
        "abandoned_rate": round(m.get("abandoned_rate", 0) * 100, 1),
        "avg_wait_time_seconds": m.get("avg_wait_time_seconds"),
        "calls_change_pct": round(t.get("calls_change", 0) * 100, 1),
        "contained_change_pct": round(t.get("contained_change", 0) * 100, 1),
    }


def _shape_topics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted([r for r in rows if r.get("count", 0) > 0],
                  key=lambda r: -r.get("count", 0))
    total = sum(r["count"] for r in rows)
    return {"total_calls": total, "top": rows[:5]}


def _shape_operators(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: -r.get("calls_handled", 0))
    top = [
        {
            "name": r.get("operator_name"),
            "calls_handled": r.get("calls_handled"),
            "avg_duration_seconds": r.get("avg_duration_seconds"),
            "cases_resolved": r.get("cases_resolved"),
        }
        for r in rows[:5]
    ]
    return {"operator_count": len(rows), "top": top}


def _shape_hourly(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: r.get("hour", 0))
    peak = max(rows, key=lambda r: r.get("count", 0)) if rows else None
    total = sum(r.get("count", 0) for r in rows)
    return {
        "total_today": total,
        "peak_hour": peak.get("hour") if peak else None,
        "peak_count": peak.get("count") if peak else None,
        "hourly": rows,
    }


async def get_analytics(scope: str = "summary") -> dict[str, Any]:
    """High-level entry — returns shaped analytics for a single scope."""
    if _creds() is None:
        return {"error": "Yoshlar dashboard not configured "
                         "(set YOSHLAR_EMAIL and YOSHLAR_PASSWORD env vars)."}
    try:
        if scope == "summary":
            data = await _get("/analytics/summary")
            return _shape_summary(data) if data else {"error": "no data"}
        if scope == "topics":
            data = await _get("/analytics/calls-by-topic")
            return _shape_topics(data) if data else {"error": "no data"}
        if scope == "operators":
            data = await _get("/analytics/operator-stats")
            return _shape_operators(data) if data else {"error": "no data"}
        if scope == "hourly":
            data = await _get("/analytics/calls-by-hour")
            return _shape_hourly(data) if data else {"error": "no data"}
        if scope == "fcr":
            data = await _get("/analytics/fcr")
            return data if data else {"error": "no data"}
        return {"error": f"Unknown scope {scope!r}"}
    except httpx.HTTPError as e:
        log.warning(f"Yoshlar analytics fetch failed: {e}")
        return {"error": f"Dashboard request failed: {e}"}
