"""Earnings stand-down calendar. Hourly, per underlying, Yahoo quoteSummary
calendarEvents (public, no key). Stores earningsDate only when Yahoo returns a
timestamp; never inferred from headlines (fair.py's news shock is separate)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

import rwa

log = logging.getLogger("earnings")
REFRESH_SECONDS = int(os.getenv("EARNINGS_REFRESH_SECONDS", "3600"))
WINDOW_HOURS = 24.0
HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

_dates: dict[str, dict] = {}   # underlying -> {"ts": epoch|None, "checkedAt": iso}
_task: asyncio.Task | None = None
_crumb: str | None = None
_diag: dict = {}   # last Yahoo outcome, exposed at /api/earnings?debug=1


async def _get_crumb(client: httpx.AsyncClient) -> str | None:
    global _crumb
    try:
        await client.get("https://fc.yahoo.com", timeout=5)
        r = await client.get(f"{HOSTS[1]}/v1/test/getcrumb", timeout=5)
        _crumb = r.text.strip() if r.status_code == 200 and r.text else None
        _diag["crumb"] = {"status": r.status_code, "ok": bool(_crumb), "body": r.text[:120] if not _crumb else "",
                          "cookies": list(client.cookies.keys())}
    except Exception as ex:
        _crumb = None
        _diag["crumb"] = {"error": f"{type(ex).__name__}: {ex}"[:160]}
    return _crumb


async def _fetch_ts(client: httpx.AsyncClient, tkr: str) -> int | None:
    """Next earnings epoch from Yahoo, or None. Past dates are ignored."""
    for host in HOSTS:
        for attempt in (0, 1):
            params = {"modules": "calendarEvents"}
            if _crumb:
                params["crumb"] = _crumb
            try:
                r = await client.get(f"{host}/v10/finance/quoteSummary/{tkr}", params=params, timeout=5)
            except Exception as ex:
                _diag[tkr] = {"host": host, "error": f"{type(ex).__name__}: {ex}"[:160]}
                break
            _diag[tkr] = {"host": host, "status": r.status_code, "body": r.text[:120] if r.status_code != 200 else ""}
            if r.status_code in (401, 403) and attempt == 0:
                await _get_crumb(client)
                continue
            if r.status_code != 200:
                break
            try:
                res = (r.json().get("quoteSummary") or {}).get("result") or []
                dates = (((res[0] or {}).get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or []
                now = datetime.now(timezone.utc).timestamp()
                future = sorted(int(d["raw"]) for d in dates if isinstance(d, dict) and d.get("raw") and d["raw"] > now)
                return future[0] if future else None
            except Exception:
                return None
    return None


async def refresh() -> None:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        pairs = [(u["underlying"], u["yahoo"]) for u in rwa.UNIVERSE if u.get("yahoo")]
        res = await asyncio.gather(*(_fetch_ts(client, t) for _, t in pairs), return_exceptions=True)
    now = datetime.now(timezone.utc).isoformat()
    for (und, _), ts in zip(pairs, res):
        _dates[und] = {"ts": None if isinstance(ts, Exception) else ts, "checkedAt": now}


def info(underlying: str) -> dict:
    u = (underlying or "").upper()
    row = _dates.get(u) or {}
    ts = row.get("ts")
    hours = None
    iso = None
    if ts:
        iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        hours = round((ts - datetime.now(timezone.utc).timestamp()) / 3600, 2)
    return {
        "underlying": u,
        "earningsDate": iso,
        "hoursUntil": hours,
        "inside24h": hours is not None and 0 <= hours <= WINDOW_HOURS,
        "checkedAt": row.get("checkedAt"),
    }


def diag() -> dict:
    return {"crumbSet": bool(_crumb), **_diag}


def all_info() -> list[dict]:
    return [info(u["underlying"]) for u in rwa.UNIVERSE]


async def _loop():
    await asyncio.sleep(10)
    while True:
        try:
            await refresh()
        except Exception:
            log.exception("earnings: refresh failed")
        await asyncio.sleep(REFRESH_SECONDS)


def start_earnings_task():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
