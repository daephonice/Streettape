"""Binance Web3 Wallet/Portfolio API: token holdings for a BSC address.

Same developer-portal credentials as swap.py (BINANCE_WEB3_API_KEY/SECRET_KEY),
same HMAC scheme, different product base path. Used as a holdings source
ahead of the public-RPC balanceOf loop in balances.py: fewer round trips,
works even when every public RPC in rpc.py is parked.

Path is our best read of the Binance Web3 "Wallet API" / Address Portfolio
family (unverified against a live response as of writing — see DEVEX.md).
Any failure (unset keys, wrong path, non-200, unexpected shape) degrades to
an empty dict so balances.py's RPC loop is always the source of truth on
error — this is a merge-in speed/coverage boost, never a hard dependency.
"""
from __future__ import annotations

import logging

import httpx

import swap  # reuses _sign/_request/API_KEY/SECRET_KEY — same credentials, sibling product
import devlog

log = logging.getLogger("portfolio")

BASE_URL = "https://web3.binance.com/wallet"
CHAIN_ID = "56"  # BSC


async def get_token_holdings(address: str) -> dict[str, float]:
    """{contract_address_lower: ui_amount} for every BEP-20 the Address
    Portfolio API reports for `address` on BSC. Empty dict on any failure
    or if credentials are unset — caller (balances.py) falls back to RPC."""
    if not swap.API_KEY or not swap.SECRET_KEY:
        return {}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
            data = await swap._request(client, "GET", "/api/v1/portfolio/tokens", params={
                "chainId": CHAIN_ID,
                "address": address,
            })
    except Exception:
        log.info("portfolio: lookup failed for %s", address, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    rows = data.get("tokens") or data.get("list") or []
    out: dict[str, float] = {}
    for row in rows:
        contract = (row.get("contractAddress") or row.get("tokenAddress") or "").lower()
        decimals = row.get("decimals")
        raw = row.get("balance") or row.get("amount")
        if not contract or raw is None:
            continue
        try:
            amt = float(raw) / (10 ** decimals) if decimals is not None else float(raw)
        except (TypeError, ValueError):
            continue
        if amt > 0:
            out[contract] = amt
    return out
