"""Binance Web3 RWA Data: official mark + official tape for tokenized stocks.
Signed the same way as swap.py (X-OC-APIKEY / X-OC-TIMESTAMP / X-OC-SIGN,
same BINANCE_WEB3_* keys, same base URL).

Smart fallback: if BINANCE_WEB3_API_KEY/SECRET_KEY are unset, or every call
this cycle fails (401, timeout, empty body, non-2xx), board.py falls through
to the existing Gecko+Yahoo path unchanged. A hard auth failure (401/403)
parks RWA Data for COOLDOWN seconds so an unset/wrong key doesn't retry on
every 45s board refresh — mirrors rpc.py's sticky-park pattern.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx

import devlog

log = logging.getLogger("rwa_api")

API_KEY = os.getenv("BINANCE_WEB3_API_KEY", "")
SECRET_KEY = os.getenv("BINANCE_WEB3_SECRET_KEY", "")
BASE_URL = "https://web3.binance.com/build"
COOLDOWN = 300.0  # seconds to stop trying RWA Data after a hard auth failure

_parked_until = 0.0


def configured() -> bool:
    return bool(API_KEY and SECRET_KEY)


def available() -> bool:
    return configured() and time.time() >= _parked_until


def _park(why: str):
    global _parked_until
    _parked_until = time.time() + COOLDOWN
    log.warning("rwa_api: parked for %ss (%s)", COOLDOWN, why)


def _timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(timestamp: str, method: str, path_with_query: str, body: str) -> str:
    pre_hash = timestamp + method + path_with_query + body
    return base64.b64encode(
        hmac.new(SECRET_KEY.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    params = {k: v for k, v in (params or {}).items() if v is not None}
    req = client.build_request("GET", path, params=params)
    signed_path = req.url.raw_path.decode("utf-8")
    timestamp = _timestamp()
    sig = _sign(timestamp, "GET", signed_path, "")
    req.headers["X-OC-APIKEY"] = API_KEY
    req.headers["X-OC-TIMESTAMP"] = timestamp
    req.headers["X-OC-SIGN"] = sig
    full_url = str(req.url)
    with devlog.timed() as t:
        resp = await client.send(req)
    if resp.status_code in (401, 403):
        devlog.log_call(what=f"rwa official {path}", url=full_url, status=resp.status_code, ms=t.ms,
                         expected="200 with data payload", actual="auth rejected — parking RWA Data",
                         body=resp.text)
        _park(f"http {resp.status_code}")
        raise RuntimeError(f"RWA Data auth failed ({resp.status_code})")
    try:
        data = resp.json()
    except Exception:
        devlog.log_call(what=f"rwa official {path}", url=full_url, status=resp.status_code, ms=t.ms,
                         expected="JSON body", actual="non-JSON response", body=resp.text)
        raise RuntimeError(f"RWA Data non-JSON response at {path}")
    ok = resp.status_code == 200 and data.get("code") in (0, None) and data.get("success", True)
    devlog.log_call(
        what=f"rwa official {path}", url=full_url, status=resp.status_code, ms=t.ms,
        expected="code 0 / success true with data payload",
        actual="ok" if ok else f"code={data.get('code')} msg={data.get('msg')}",
        body=None if ok else str(data)[:300],
    )
    if not ok:
        raise RuntimeError(data.get("msg") or f"RWA Data error {data.get('code')} at {path}")
    return data.get("data")


async def platforms(client: httpx.AsyncClient):
    return await _get(client, "/api/v1/dex/market/rwa/platforms")


async def tokens(client: httpx.AsyncClient):
    return await _get(client, "/api/v1/dex/market/rwa/tokens")


async def price(client: httpx.AsyncClient, address: str | None = None, symbol: str | None = None):
    # binanceChainId is mandatory (live log: 40001 "Parameter binanceChainId is required").
    # Sibling endpoints name the address tokenContractAddress, so send both spellings.
    return await _get(client, "/api/v1/dex/market/rwa/price", {
        "tokenAddress": address, "tokenContractAddress": address,
        "tokenContractAddresses": address,
        "binanceChainId": BSC_CHAIN_ID, "symbol": symbol,
    })


async def search(client: httpx.AsyncClient, query: str):
    return await _get(client, "/api/v1/dex/market/rwa/search", {"keyword": query})


BSC_CHAIN_ID = "56"


async def underlying_profile(client: httpx.AsyncClient, address: str, chain_id: str = BSC_CHAIN_ID):
    return await _get(client, "/api/v1/dex/market/rwa/underlying-profile",
                      {"tokenContractAddress": address, "binanceChainId": chain_id})


async def underlying_market(client: httpx.AsyncClient, address: str, chain_id: str = BSC_CHAIN_ID):
    return await _get(client, "/api/v1/dex/market/rwa/underlying-market",
                      {"tokenContractAddress": address, "binanceChainId": chain_id})


def _pos(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


PRICE_GAP = 0.3          # s between price calls; bursts get 429 (code 42900)
PRICE_PARK_SECONDS = 600.0
_price_parked_until = 0.0


async def get_official_snapshot(addresses: list[str]) -> dict | None:
    """One tape+mark pass over the given wrapper addresses via RWA Data.
    Returns {addr_lower: {"price": float|None, "markPrice": float|None,
    "multiplier": float|None}} or None if RWA Data is unset/parked/fails
    entirely this cycle — caller falls back to Gecko+Yahoo on None.
    Paced (PRICE_GAP), retries a 429 once, and parks the price endpoint for
    PRICE_PARK_SECONDS after 3 consecutive non-rate-limit failures."""
    global _price_parked_until
    if not available() or time.time() < _price_parked_until:
        return None
    out = {}
    hard_fails = 0
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=8.0) as client:
            for i, addr in enumerate(addresses):
                if i:
                    await asyncio.sleep(PRICE_GAP)
                row = None
                for attempt in range(2):
                    try:
                        row = await price(client, address=addr)
                        hard_fails = 0
                        break
                    except Exception as e:
                        if "rate limit" in str(e).lower() and attempt == 0:
                            await asyncio.sleep(1.0)
                            continue
                        if "rate limit" not in str(e).lower():
                            hard_fails += 1
                        log.info("rwa_api: price miss for %s: %s", addr, e)
                        break
                if hard_fails >= 3:
                    _price_parked_until = time.time() + PRICE_PARK_SECONDS
                    log.warning("rwa_api: price endpoint failing, parked %ss", PRICE_PARK_SECONDS)
                    break
                if isinstance(row, list):
                    # rwa/price now answers with a list (tokenContractAddresses is plural)
                    row = next((r for r in row if isinstance(r, dict) and
                                str(r.get("tokenContractAddress") or r.get("tokenAddress") or "").lower() == addr.lower()),
                               next((r for r in row if isinstance(r, dict)), None))
                if not isinstance(row, dict) or not row:
                    continue
                out[addr.lower()] = {
                    "price": float(row["tokenPrice"]) if row.get("tokenPrice") else None,
                    "markPrice": _pos(row.get("underlyingPrice")),
                    "multiplier": float(row["multiplier"]) if row.get("multiplier") else None,
                }
    except Exception:
        log.warning("rwa_api: snapshot pass failed, falling back", exc_info=True)
        return None
    return out or None


def _row_from_listing(item: dict) -> dict | None:
    """Normalize one rwa/tokens or rwa/search item to rwa.merge_dynamic()'s
    shape. Skips anything without a real on-chain address."""
    addr = item.get("tokenAddress") or item.get("address")
    und = item.get("underlying") or item.get("underlyingSymbol")
    platform = item.get("platform") or item.get("issuer")
    if not addr or not und or not platform:
        return None
    return {
        "underlying": str(und).upper(),
        "name": item.get("underlyingName") or item.get("name"),
        "yahoo": item.get("yahooTicker") or (str(und).upper() if item.get("assetType") != "index" else None),
        "platform": str(platform).lower(),
        "address": addr,
        "symbol": item.get("symbol"),
        "multiplier": item.get("multiplier"),
    }


async def get_dynamic_universe() -> list[dict] | None:
    """rwa/tokens rows normalized for rwa.merge_dynamic(). None if RWA Data
    is unset/parked/the call fails — caller keeps the static seed as-is."""
    if not available():
        return None
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=8.0) as client:
            rows = await tokens(client)
    except Exception:
        log.info("rwa_api: tokens listing failed", exc_info=True)
        return None
    if not rows:
        return None
    out = [r for r in (_row_from_listing(item) for item in rows) if r]
    return out or None


_devcheck_last = 0.0
DEVCHECK_MIN_INTERVAL = 60.0


async def devcheck(underlying: str = "NVDA", address: str = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436") -> dict:
    """Fire the RWA Data endpoints not used by the board (platforms, search,
    underlying_profile, underlying_market) once so devlog records them.
    Rate-limited; returns per-endpoint ok/error summary."""
    global _devcheck_last
    if not configured():
        return {"ok": False, "error": "BINANCE_WEB3_API_KEY/SECRET_KEY unset"}
    if time.time() < _parked_until:
        return {"ok": False, "error": "parked after auth failure"}
    now = time.time()
    if now - _devcheck_last < DEVCHECK_MIN_INTERVAL:
        return {"ok": False, "error": "rate-limited"}
    _devcheck_last = now
    calls = {
        "platforms": lambda c: platforms(c),
        "search": lambda c: search(c, underlying),
        "underlying_profile": lambda c: underlying_profile(c, address),
        "underlying_market": lambda c: underlying_market(c, address),
    }
    out = {}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=8.0) as client:
        for name, fn in calls.items():
            try:
                data = await fn(client)
                entry = {"ok": True, "items": len(data) if isinstance(data, (list, dict)) else None}
                if name in ("underlying_profile", "underlying_market"):
                    entry["data"] = data
                out[name] = entry
            except Exception as e:
                out[name] = {"ok": False, "error": str(e)[:200]}
    return {"ok": all(v["ok"] for v in out.values()), "results": out}


MCAP_REFRESH_SECONDS = 3600.0
MCAP_RETRY_SECONDS = 300.0
MCAP_GAP = 2.0  # seconds between underlying-market calls; limit is undocumented, so stay conservative


async def _underlying_market_retry(client, addr):
    for attempt in range(4):
        try:
            return await underlying_market(client, addr)
        except Exception as e:
            if "rate limit" in str(e).lower() and attempt < 3:
                await asyncio.sleep(3.0 * (attempt + 1))
                continue
            raise
_mcaps: dict[str, float] = {}
_mcap_last = 0.0


def get_mcap(underlying: str | None) -> float | None:
    """Underlying company's market cap (USD) from RWA Data underlying-market."""
    return _mcaps.get((underlying or "").upper())


async def refresh_mcaps(pairs: list[tuple[str, str]]) -> None:
    """pairs: (underlying, one wrapper address). Hourly; keeps old values on failure."""
    global _mcap_last
    if not available() or time.time() - _mcap_last < MCAP_REFRESH_SECONDS:
        return
    _mcap_last = time.time()
    failed = False
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=8.0) as client:
        for i, (und, addr) in enumerate(pairs):
            if i:
                await asyncio.sleep(MCAP_GAP)
            try:
                data = await _underlying_market_retry(client, addr)
                mc = float(((data or {}).get("marketData") or {}).get("marketCap"))
                if mc > 0:
                    _mcaps[und.upper()] = mc
                else:
                    failed = True
            except Exception:
                failed = True
                log.warning("mcap refresh failed for %s", und, exc_info=True)
    if failed:  # retry sooner than the hourly cadence
        _mcap_last = time.time() - MCAP_REFRESH_SECONDS + MCAP_RETRY_SECONDS


RATIO_REFRESH_SECONDS = 3600.0
RATIO_RETRY_SECONDS = 300.0
_ratios: dict[str, float] = {}
_ratio_last = 0.0


def get_ratio(address: str | None) -> float | None:
    """tokenToShareRatio (shares per token) for a wrapper address, from underlying-profile."""
    return _ratios.get((address or "").lower())


async def _underlying_profile_retry(client, addr):
    for attempt in range(4):
        try:
            return await underlying_profile(client, addr)
        except Exception as e:
            if "rate limit" in str(e).lower() and attempt < 3:
                await asyncio.sleep(3.0 * (attempt + 1))
                continue
            raise


async def refresh_ratios(addresses: list[str]) -> None:
    """Hourly. One underlying-profile call per wrapper address (ratio is per wrapper,
    not per underlying). Keeps old values on failure."""
    global _ratio_last
    if not available() or time.time() - _ratio_last < RATIO_REFRESH_SECONDS:
        return
    _ratio_last = time.time()
    failed = False
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=8.0) as client:
        for i, addr in enumerate(addresses):
            if i:
                await asyncio.sleep(MCAP_GAP)
            try:
                data = await _underlying_profile_retry(client, addr)
                r = _pos((data or {}).get("tokenToShareRatio"))
                if r:
                    _ratios[addr.lower()] = r
                else:
                    failed = True
            except Exception:
                failed = True
                log.warning("ratio refresh failed for %s", addr, exc_info=True)
    if failed:
        _ratio_last = time.time() - RATIO_REFRESH_SECONDS + RATIO_RETRY_SECONDS


async def mcap_report(pairs: list[tuple[str, str]]) -> dict:
    """Per-underlying marketCap (or error) straight from underlying-market. Debug/DevEx aid."""
    if not available():
        return {"ok": False, "error": "unset or parked"}
    out = {}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=8.0) as client:
        for i, (und, addr) in enumerate(pairs):
            if i:
                await asyncio.sleep(MCAP_GAP)
            try:
                data = await _underlying_market_retry(client, addr)
                md = (data or {}).get("marketData") or {}
                out[und] = {"marketCap": md.get("marketCap"), "totalShares": md.get("totalShares"),
                            "openState": ((data or {}).get("statusInfo") or {}).get("openState")}
            except Exception as e:
                out[und] = {"error": str(e)[:200]}
    return out


_pass_last = 0.0


async def official_pass(addresses: list[str]) -> dict:
    """One paced pass (PRICE_GAP apart), no retries. Returns first success,
    first failure and per-address result for DEVEX.md Runtime log."""
    global _pass_last
    if not available():
        return {"ok": False, "error": "unset or parked"}
    if time.time() - _pass_last < DEVCHECK_MIN_INTERVAL:
        return {"ok": False, "error": "rate-limited (1 pass / 60s)"}
    _pass_last = time.time()
    first_ok = first_fail = None
    rows = {}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=8.0) as client:
        for i, addr in enumerate(addresses):
            if i:
                await asyncio.sleep(PRICE_GAP)
            try:
                row = await price(client, address=addr)
                if isinstance(row, list):
                    row = next((r for r in row if isinstance(r, dict)), None)
                mark = _pos((row or {}).get("underlyingPrice"))
                rows[addr] = {"markPrice": mark, "row": row}
                if mark is not None and first_ok is None:
                    first_ok = {"address": addr, "row": row}
                if mark is None and first_fail is None:
                    first_fail = {"address": addr, "error": "200 but no underlyingPrice", "row": row}
            except Exception as e:
                rows[addr] = {"markPrice": None, "error": str(e)[:200]}
                if first_fail is None:
                    first_fail = {"address": addr, "error": str(e)[:200]}
    n_ok = sum(1 for v in rows.values() if v["markPrice"] is not None)
    return {"ok": n_ok > 0, "marks": n_ok, "total": len(rows),
            "firstSuccess": first_ok, "firstFailure": first_fail,
            "verdict": "binance" if n_ok else "endpoint returned no mark; board stays on Yahoo"}
