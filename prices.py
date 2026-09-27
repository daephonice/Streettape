"""Shared price cache. Wrappers from board snapshot; BNB/USDT/USDC from Binance public data."""
from __future__ import annotations

import json
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

import rwa
from database import SessionLocal
from models import PriceSnapshot

log = logging.getLogger("prices")
REFRESH_SECONDS = 2.0
OPEN_REFRESH_SECONDS = 60.0
BINANCE_BASES = ("https://data-api.binance.vision", "https://api.binance.com")
BINANCE_PAIRS = ("BNBUSDT", "USDCUSDT")

TOKEN_ASSETS = {
    "BNB": {"name": "BNB", "image": "/static/img/bnb.svg", "mint": rwa.NATIVE,
            "url": "https://www.bnbchain.org", "description": "Native asset of BNB Smart Chain. Pays gas."},
    "USDT": {"name": "Tether", "image": "/static/img/usdt.svg", "mint": rwa.USDT,
             "url": "https://tether.to", "description": "USDT on BNB Smart Chain."},
    "USDC": {"name": "USD Coin", "image": "/static/img/usdc.svg", "mint": rwa.USDC,
             "url": "https://www.circle.com/usdc", "description": "USDC on BNB Smart Chain."},
}

_stocks = {}
_tokens = {}
_stock_open = {}
_updated_at = None
_task = None
_tape_stale = False


def _touch():
    global _updated_at
    _updated_at = datetime.now(timezone.utc)


async def _refresh_stocks():
    snap = rwa.get_cached_snapshot()
    records = snap.get("tokens") or []
    global _tape_stale
    _tape_stale = bool(snap.get("tapeStale"))
    for r in records:
        price = r.get("tokenPrice")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        _stocks[r["symbol"].upper()] = {
            "symbol": r["symbol"], "mint": r["mint"], "name": r.get("name") or r["symbol"],
            "image": r.get("image"), "description": r.get("description"), "url": r.get("url"),
            "supply": None, "price": float(price), "mark": r.get("markPrice"),
            "premium": r.get("premium"), "platform": r.get("platform"), "underlying": r.get("underlying"),
            "multiplier": r.get("multiplier"), "noYahoo": r.get("noYahoo", False),
            "fairPrice": r.get("fairPrice"), "premiumToOfficial": r.get("premiumToOfficial"),
            "premiumToFair": r.get("premiumToFair"),
        }
    if records:
        _touch()


async def _refresh_tokens(client):
    params = {"symbols": json.dumps(list(BINANCE_PAIRS), separators=(",", ":"))}
    rows = None
    for base in BINANCE_BASES:
        try:
            resp = await client.get(f"{base}/api/v3/ticker/24hr", params=params)
            resp.raise_for_status()
            rows = {r["symbol"]: r for r in resp.json()}
            bnb_last = float(rows["BNBUSDT"]["lastPrice"])
            bnb_open = float(rows["BNBUSDT"]["openPrice"])
            usdc_last = float(rows["USDCUSDT"]["lastPrice"])
            usdc_open = float(rows["USDCUSDT"]["openPrice"])
            break
        except Exception:
            rows = None
            log.warning("prices: binance %s failed", base, exc_info=True)
    if rows is None:
        return
    _tokens["BNB"] = {"price": bnb_last, "open": bnb_open if bnb_open > 0 else None}
    _tokens["USDC"] = {"price": usdc_last, "open": usdc_open}
    _tokens["USDT"] = {"price": (1 / usdc_last) if usdc_last else 1.0, "open": (1 / usdc_open) if usdc_open else 1.0}
    _touch()


def _load_open_prices(symbols):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    db = SessionLocal()
    try:
        out = {}
        for sym in symbols:
            row = db.execute(
                select(PriceSnapshot.token_price).where(
                    PriceSnapshot.symbol == sym,
                    PriceSnapshot.fetched_at <= cutoff,
                    PriceSnapshot.fetched_at >= cutoff - timedelta(hours=6),
                ).order_by(PriceSnapshot.fetched_at.desc()).limit(1)
            ).first()
            if row is None:
                row = db.execute(
                    select(PriceSnapshot.token_price).where(
                        PriceSnapshot.symbol == sym, PriceSnapshot.fetched_at > cutoff
                    ).order_by(PriceSnapshot.fetched_at.asc()).limit(1)
                ).first()
            if row and row[0] and row[0] > 0:
                out[sym] = float(row[0])
        return out
    finally:
        db.close()


async def _loop():
    last_open = 0.0
    async with httpx.AsyncClient(timeout=8, headers={"Accept": "application/json"}) as client:
        while True:
            started = time.monotonic()
            await asyncio.gather(_refresh_stocks(), _refresh_tokens(client), return_exceptions=True)
            if time.monotonic() - last_open >= OPEN_REFRESH_SECONDS:
                last_open = time.monotonic()
                try:
                    _stock_open.update(await asyncio.to_thread(_load_open_prices, list(_stocks)))
                except Exception:
                    log.warning("prices: 24h open failed", exc_info=True)
            await asyncio.sleep(max(0.0, REFRESH_SECONDS - (time.monotonic() - started)))


def start_price_task():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def _pct(price, open_price):
    if not open_price or open_price <= 0:
        return None
    return (price / open_price - 1) * 100


def get_prices():
    if not _stocks and not _tokens:
        return None
    out = {}
    for sym, t in _tokens.items():
        out[sym] = {"price": t["price"], "change24h": _pct(t["price"], t["open"]), "mark": None, "premium": None}
    for sym, s in _stocks.items():
        out[sym] = {
            "price": s["price"],
            "change24h": _pct(s["price"], _stock_open.get(s["symbol"])),
            "mark": s.get("mark"),
            "premium": s.get("premium"),
            "noYahoo": s.get("noYahoo", False),
            "fairPrice": s.get("fairPrice"),
            "premiumToOfficial": s.get("premiumToOfficial"),
            "premiumToFair": s.get("premiumToFair"),
        }
    return {
        "updatedAt": _updated_at.isoformat() if _updated_at else None,
        "prices": out,
        "session": rwa.session_now(),
        "tapeStale": _tape_stale,
    }


def get_assets():
    assets = {sym: {"name": m["name"], "image": m["image"], "kind": "token", "mint": m["mint"]} for sym, m in TOKEN_ASSETS.items()}
    for sym, s in _stocks.items():
        assets[sym] = {"name": s["name"], "image": s["image"], "kind": "stock", "mint": s["mint"],
                       "platform": s.get("platform"), "underlying": s.get("underlying"),
                       "multiplier": s.get("multiplier")}
    return {"assets": assets}


def stock_mints():
    return {s["mint"].lower(): sym for sym, s in _stocks.items()}


def get_asset(symbol: str):
    sym = (symbol or "").upper()
    t = TOKEN_ASSETS.get(sym)
    if t:
        return {"symbol": sym, "kind": "token", **t}
    s = _stocks.get(sym)
    if s:
        return {"symbol": sym, "kind": "stock", "name": s["name"], "image": s["image"], "mint": s["mint"],
                "description": s["description"], "url": s["url"], "platform": s.get("platform"),
                "underlying": s.get("underlying"), "multiplier": s.get("multiplier")}
    return None


async def wait_ready(timeout: float = 8.0):
    end = time.monotonic() + timeout
    while not _stocks and time.monotonic() < end:
        await asyncio.sleep(0.1)
