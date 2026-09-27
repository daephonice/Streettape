"""StreetTape board: Yahoo mark + GeckoTerminal tape.

Gecko never blocks on Yahoo: the snapshot is published as soon as tape
prices land (mark null), then patched in place once marks arrive. Yahoo
misses fall back query2 -> query1 -> last known mark in price_snapshots,
per-ticker, all run concurrently so one slow/dead host can't stall the rest.
"""
from __future__ import annotations

import os
import asyncio
import logging

import httpx
from sqlalchemy import select

import rwa
import devlog
from database import SessionLocal
from models import PriceSnapshot

log = logging.getLogger("board")
REFRESH_SECONDS = int(os.getenv("BOARD_REFRESH_SECONDS", "45"))
GECKO = "https://api.geckoterminal.com/api/v2"
YAHOO_HOSTS = (
    "https://query2.finance.yahoo.com/v8/finance/chart",
    "https://query1.finance.yahoo.com/v8/finance/chart",
)
YAHOO_TIMEOUT = 3.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_task = None


def _last_known_mark(tkr: str) -> float | None:
    und = next((u["underlying"] for u in rwa.UNIVERSE if u.get("yahoo") == tkr), None)
    if not und:
        return None
    db = SessionLocal()
    try:
        row = db.execute(
            select(PriceSnapshot.mark_price)
            .where(PriceSnapshot.underlying == und, PriceSnapshot.mark_price > 0)
            .order_by(PriceSnapshot.fetched_at.desc())
            .limit(1)
        ).first()
        return float(row[0]) if row and row[0] else None
    except Exception:
        log.warning("board: last-known-mark lookup failed for %s", tkr, exc_info=True)
        return None
    finally:
        db.close()


async def _yahoo_one(client: httpx.AsyncClient, tkr: str) -> tuple[str, float | None]:
    for host in YAHOO_HOSTS:
        url = f"{host}/{tkr}"
        try:
            with devlog.timed() as t:
                resp = await client.get(url, params={"interval": "1d", "range": "5d"}, timeout=YAHOO_TIMEOUT)
            resp.raise_for_status()
            result = (resp.json().get("chart") or {}).get("result") or []
            if not result:
                devlog.log_call(what=f"mark fetch {tkr}", url=url, status=resp.status_code, ms=t.ms,
                                 expected="chart.result[0].meta.regularMarketPrice", actual="empty result array")
                continue
            meta = result[0].get("meta") or {}
            px = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
            devlog.log_call(what=f"mark fetch {tkr}", url=url, status=resp.status_code, ms=t.ms,
                             expected="regularMarketPrice present", actual=f"price={px}")
            if px:
                return tkr, float(px)
        except httpx.TimeoutException:
            devlog.log_call(what=f"mark fetch {tkr}", url=url, status="timeout", ms=YAHOO_TIMEOUT * 1000,
                             error=f"timed out after {YAHOO_TIMEOUT}s")
            log.warning("board: yahoo %s timed out on %s", tkr, host)
        except Exception as e:
            devlog.log_call(what=f"mark fetch {tkr}", url=url, status="exception", ms=0, error=str(e))
            log.warning("board: yahoo %s failed on %s", tkr, host, exc_info=True)
    fallback = await asyncio.to_thread(_last_known_mark, tkr)
    return tkr, fallback


async def _yahoo_marks(client: httpx.AsyncClient) -> dict:
    tickers = sorted({u["yahoo"] for u in rwa.UNIVERSE if u.get("yahoo")})
    results = await asyncio.gather(*(_yahoo_one(client, tkr) for tkr in tickers), return_exceptions=True)
    out = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        tkr, px = r
        if px:
            out[tkr] = px
    return out


_last_tape: dict = {}   # addr(lower) -> {"price": float, "image": str|None} — last good Gecko read
_tape_stale = False     # true when this refresh cycle hit a 429 and fell back to _last_tape


async def _one_gecko(client, addr: str):
    url = f"{GECKO}/networks/bsc/tokens/{addr}"
    with devlog.timed() as t:
        resp = await client.get(url)
    if resp.status_code == 429:
        await asyncio.sleep(2)
        with devlog.timed() as t2:
            resp = await client.get(url)
        if resp.status_code == 429:
            devlog.log_call(what=f"rwa/price tape {addr}", url=url, status=429, ms=t.ms + t2.ms,
                             expected="200 with data.attributes.price_usd", actual="429 rate limited (both tries)")
            return "RATE_LIMITED"
    if resp.status_code != 200:
        devlog.log_call(what=f"rwa/price tape {addr}", url=url, status=resp.status_code, ms=t.ms,
                         expected="200 with data.attributes.price_usd", body=resp.text)
        return None
    attr = resp.json()["data"]["attributes"]
    px = attr.get("price_usd")
    devlog.log_call(what=f"rwa/price tape {addr}", url=url, status=resp.status_code, ms=t.ms,
                     expected="data.attributes.price_usd", actual=f"price_usd={px}")
    return {"price": float(px) if px else None, "image": attr.get("image_url")}


async def _gecko_prices(client) -> dict:
    global _tape_stale
    out = {}
    rate_limited = False
    addrs = [w["address"] for w in rwa.wrappers() if w.get("address")]
    for i in range(0, len(addrs), 5):
        chunk = addrs[i:i + 5]
        try:
            resp = await client.get(f"{GECKO}/networks/bsc/tokens/multi/{','.join(chunk)}")
            if resp.status_code == 429:
                rate_limited = True
            elif resp.status_code == 200:
                data = resp.json().get("data") or []
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    attr = item.get("attributes") or {}
                    addr = (attr.get("address") or "").lower()
                    px = attr.get("price_usd")
                    out[addr] = {"price": float(px) if px else None, "image": attr.get("image_url")}
                await asyncio.sleep(0.2)
                continue
        except Exception:
            log.info("board: gecko multi miss", exc_info=True)
        for a in chunk:
            try:
                row = await _one_gecko(client, a)
                if row == "RATE_LIMITED":
                    rate_limited = True
                elif row:
                    out[a.lower()] = row
            except Exception:
                log.info("board: gecko %s skipped", a, exc_info=True)
            await asyncio.sleep(0.2)

    # Fall back to last good read per-address on a 429; only mark stale if we
    # actually had to reuse something (an address with no prior tape stays null).
    if rate_limited:
        used_fallback = False
        for a in addrs:
            key = a.lower()
            if key not in out and key in _last_tape:
                out[key] = _last_tape[key]
                used_fallback = True
        _tape_stale = used_fallback
    else:
        _tape_stale = False
    _last_tape.update(out)
    return out


def _group(tokens):
    by = {}
    for t in tokens:
        by.setdefault(t["underlying"], []).append(t)
    groups = []
    for und, rows in by.items():
        priced = [r for r in rows if r.get("tokenPrice")]
        cheapest = min(priced, key=lambda r: r["tokenPrice"]) if priced else None
        richest = max(priced, key=lambda r: r["tokenPrice"]) if priced else None
        groups.append({
            "underlying": und,
            "name": rows[0]["name"],
            "markPrice": rows[0].get("markPrice"),
            "wrappers": rows,
            "cheapest": cheapest["symbol"] if cheapest else None,
            "richest": richest["symbol"] if richest else None,
            "crossSpread": (
                (richest["tokenPrice"] / cheapest["tokenPrice"] - 1)
                if cheapest and richest and cheapest["tokenPrice"] else None
            ),
            "absPremium": max((abs(r["premium"] or 0) for r in rows), default=0),
        })
    groups.sort(key=lambda g: g["absPremium"], reverse=True)
    return groups


def _build_tokens(tapes: dict, marks: dict) -> list[dict]:
    tokens = []
    for w in rwa.wrappers():
        has_addr = bool(w.get("address"))
        tape = (tapes.get(w["address"].lower()) if has_addr else None) or {}
        token_price = tape.get("price")
        mark = marks.get(w["yahoo"]) if w.get("yahoo") else None
        prem = rwa.premium(token_price, mark) if token_price and mark else None
        tokens.append({
            "symbol": w["symbol"],
            "name": w["name"],
            "underlying": w["underlying"],
            "platform": w["platform"],
            "mint": w["address"],
            "address": w["address"],
            "image": tape.get("image"),
            "tokenPrice": token_price if has_addr else None,
            "markPrice": mark,
            "noYahoo": not w.get("yahoo"),
            "premium": prem if has_addr else None,
            "status": rwa.premium_status(prem) if has_addr else "flat",
            "description": (
                f"{w['name']} tokenized equity on BNB Chain via {w['platform']}. Economic exposure only."
                if has_addr else f"{w['name']} has no confirmed {w['platform']} wrapper yet."
            ),
            "url": f"https://pancakeswap.finance/swap?chain=bsc&outputCurrency={w['address']}" if has_addr else None,
            "multiplier": w.get("multiplier"),
        })
    tokens.sort(key=lambda t: abs(t["premium"] or 0), reverse=True)
    return tokens


async def build_snapshot():
    """Tape (Gecko) publishes the snapshot immediately with marks null.
    Marks (Yahoo) patch it in place once they land, so a dead Yahoo never
    blocks stock rows, the session chip's board copy, or news from appearing."""
    async with httpx.AsyncClient(timeout=6, headers=HEADERS) as client:
        tapes = await _gecko_prices(client)
        tokens = _build_tokens(tapes, {})
        snap = rwa.set_cached_snapshot(tokens, _group(tokens), tape_stale=_tape_stale)

        marks = await _yahoo_marks(client)

    tokens = _build_tokens(tapes, marks)
    snap = rwa.set_cached_snapshot(tokens, _group(tokens), tape_stale=_tape_stale)
    _persist(tokens)
    return snap


def _persist(rows):
    db = SessionLocal()
    try:
        # Wrapper snapshots
        for row in rows:
            if not row.get("tokenPrice"):
                continue
            db.add(PriceSnapshot(
                symbol=row["symbol"],
                token_price=row["tokenPrice"],
                mark_price=row.get("markPrice") or 0.0,
                premium=row.get("premium"),
                platform=row.get("platform"),
                underlying=row.get("underlying"),
            ))
        # Cash mark snapshots — one row per underlying where we have a mark
        seen_und = set()
        for row in rows:
            und = row.get("underlying")
            mark = row.get("markPrice")
            if und and mark and und not in seen_und:
                seen_und.add(und)
                db.add(PriceSnapshot(
                    symbol=und,
                    token_price=mark,
                    mark_price=mark,
                    premium=0.0,
                    platform="cash",
                    underlying=und,
                ))
        db.commit()
    except Exception:
        log.warning("board: persist failed", exc_info=True)
        db.rollback()
    finally:
        db.close()


async def _loop():
    while True:
        try:
            await build_snapshot()
        except Exception:
            log.exception("board: refresh failed")
        await asyncio.sleep(REFRESH_SECONDS)


def start_board_refresh_task():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
