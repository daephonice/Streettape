"""BSC native + BEP-20 balances. Address Portfolio API first (fewer round
trips, works when public RPCs are parked), public RPC always runs too and
fills/overrides anything the portfolio call missed or got wrong."""
from __future__ import annotations

import re
import time
import logging

import prices
import rwa
import rpc
import portfolio

log = logging.getLogger("balances")
BALANCE_TTL = 6.0
_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
BALANCE_OF = "0x70a08231"
_balance_cache = {}


def invalidate(address=None):
    if address:
        _balance_cache.pop(address.lower(), None)
    else:
        _balance_cache.clear()


def valid_address(address: str) -> bool:
    return bool(_ADDR_RE.match(address or ""))


async def _rpc(method, params):
    return await rpc.call(method, params)


def _pad(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


async def get_balances(address: str) -> dict:
    if not valid_address(address):
        raise ValueError("Invalid address")
    key = address.lower()
    cached = _balance_cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]

    assets = prices.get_assets().get("assets") or {}
    mint_to_sym = {(meta.get("mint") or "").lower(): sym for sym, meta in assets.items() if meta.get("mint")}

    portfolio_holdings: dict[str, float] = {}
    try:
        by_contract = await portfolio.get_token_holdings(address)
        for contract, amt in by_contract.items():
            sym = mint_to_sym.get(contract)
            if sym:
                portfolio_holdings[sym] = amt
    except Exception:
        log.info("balances: portfolio lookup failed for %s", address, exc_info=True)

    raw = await _rpc("eth_getBalance", [address, "latest"])
    holdings = {"BNB": int(raw, 16) / 1e18}
    holdings.update(portfolio_holdings)  # portfolio result first; RPC below fills/repairs the rest
    for sym, meta in assets.items():
        mint = meta.get("mint") or ""
        if not mint or mint.lower() == rwa.NATIVE.lower() or sym == "BNB" or sym in portfolio_holdings:
            continue
        try:
            res = await _rpc("eth_call", [{"to": mint, "data": BALANCE_OF + _pad(address)}, "latest"])
            if res and res != "0x":
                amt = int(res, 16) / 1e18
                if amt > 0:
                    holdings[sym] = amt
        except Exception:
            log.info("balances: %s failed", sym, exc_info=True)
    try:
        import lend as lend_mod
        lend_mod.overlay_holdings(address, holdings)
    except Exception:
        log.warning("balances: lend overlay failed", exc_info=True)

    price_data = (prices.get_prices() or {}).get("prices") or {}
    holding_marks = {}
    for sym in holdings:
        p = price_data.get(sym)
        if p and (p.get("fairPrice") or p.get("mark")):
            holding_marks[sym] = {
                "premiumToOfficial": p.get("premiumToOfficial"),
                "premiumToFair": p.get("premiumToFair"),
            }

    out = {"address": address, "holdings": holdings, "holdingMarks": holding_marks}
    for sym in ("BNB", "USDT", "USDC"):
        out[sym] = holdings.get(sym, 0.0)
    _balance_cache[key] = (time.time() + BALANCE_TTL, out)
    return out
