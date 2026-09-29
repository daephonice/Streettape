"""Binance Web3 Trading API: quote + build swap (SWAP or RFQ) on BSC. Falls back to PancakeSwap deep link."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

import prices
import rwa
import devlog

log = logging.getLogger("swap")

API_KEY = os.getenv("BINANCE_WEB3_API_KEY", "")
SECRET_KEY = os.getenv("BINANCE_WEB3_SECRET_KEY", "")
BASE_URL = "https://web3.binance.com/build"
CHAIN_ID = "56"  # BSC
SLIPPAGE_PCT = "1"  # 1%

PANCAKE = "https://pancakeswap.finance/swap"


def pancake_link(input_addr: str, output_addr: str) -> str:
    inn = "BNB" if (input_addr or "").lower() in (rwa.NATIVE.lower(), rwa.WBNB.lower()) else input_addr
    out = "BNB" if (output_addr or "").lower() in (rwa.NATIVE.lower(), rwa.WBNB.lower()) else output_addr
    return f"{PANCAKE}?chain=bsc&inputCurrency={inn}&outputCurrency={out}"


def _price_of_mint(mint: str):
    mint_l = (mint or "").lower()
    data = prices.get_prices() or {}
    assets = prices.get_assets().get("assets") or {}
    for sym, meta in assets.items():
        if (meta.get("mint") or "").lower() == mint_l:
            px = (data.get("prices") or {}).get(sym)
            return px["price"] if px else None
    if mint_l in (rwa.NATIVE.lower(), rwa.WBNB.lower()):
        px = (data.get("prices") or {}).get("BNB")
        return px["price"] if px else None
    return None


def _fallback_quote(input_mint: str, output_mint: str, ui_amount: float, reason: str = "unspecified") -> dict:
    devlog.log_call(what="quote fallback -> pancake", url=f"{BASE_URL}/api/v1/dex/aggregator/quote", status="n/a", ms=0,
                    expected="Binance quote route", actual=f"fell back: {reason}")
    in_px = _price_of_mint(input_mint)
    out_px = _price_of_mint(output_mint)
    out_ui = (ui_amount * in_px / out_px) if in_px and out_px and ui_amount > 0 else None
    return {
        "transaction": None,
        "deepLink": pancake_link(input_mint, output_mint),
        "uiOutAmount": out_ui,
        "uiMinReceived": out_ui * 0.99 if out_ui else None,
        "rate": (out_ui / ui_amount) if out_ui and ui_amount else None,
        "priceImpactPct": None,
        "gasless": False,
        "routes": ["PancakeSwap"],
        "transferFeeBps": 0,
        "provider": "pancake",
        "executionMode": "SWAP",
        "sim": None,
        "fallbackReason": reason,
    }


def _timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(timestamp: str, method: str, path_with_query: str, body: str) -> str:
    pre_hash = timestamp + method + path_with_query + body
    return base64.b64encode(
        hmac.new(SECRET_KEY.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")


async def _request(client: httpx.AsyncClient, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> dict:
    params = {k: v for k, v in (params or {}).items() if v is not None}
    req = client.build_request(method, path, params=params, json=json_body)
    # base_url carries /build, so raw_path is "/build/api/v1/...?query" exactly as sent on
    # the wire — precisely the requestPath the signature must use.
    signed_path = req.url.raw_path.decode("utf-8")
    body_str = req.content.decode("utf-8") if req.content else ""
    timestamp = _timestamp()
    sig = _sign(timestamp, method.upper(), signed_path, body_str)
    req.headers["X-OC-APIKEY"] = API_KEY
    req.headers["X-OC-TIMESTAMP"] = timestamp
    req.headers["X-OC-SIGN"] = sig
    full_url = str(req.url)
    with devlog.timed() as t:
        resp = await client.send(req)
    try:
        data = resp.json()
    except Exception:
        devlog.log_call(what=f"Binance Web3 {method.upper()} {path}", url=full_url, status=resp.status_code, ms=t.ms,
                        expected="JSON body", actual="non-JSON response", body=resp.text)
        raise RuntimeError(f"non-JSON response ({resp.status_code}) at {path}: {resp.text[:150]}")
    ok = data.get("code") in (0, None) and data.get("success", True)
    devlog.log_call(
        what=f"Binance Web3 {method.upper()} {path}", url=full_url, status=resp.status_code, ms=t.ms,
        expected="code 0 / success true with data payload",
        actual="ok" if ok else f"code={data.get('code')} msg={data.get('msg')}",
        body=None if ok else str(data),
    )
    if not ok:
        raise RuntimeError(data.get("msg") or f"Binance Web3 API error {data.get('code')}")
    return data.get("data")


def _to_units(ui_amount: float, decimals: int) -> str:
    return str(int(round(ui_amount * (10 ** decimals))))


def _from_units(raw: str, decimals: int) -> float:
    if raw is None:
        return 0.0
    return int(raw) / (10 ** decimals)


async def quote(input_mint: str, output_mint: str, ui_amount: float, taker: str | None = None) -> dict:
    if not API_KEY or not SECRET_KEY or ui_amount <= 0:
        return _fallback_quote(input_mint, output_mint, ui_amount, "keys unset or amount<=0")

    # Aggregator wants native BNB as the placeholder 0xEeee... address (already rwa.NATIVE).
    from_addr = input_mint
    to_addr = output_mint

    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as client:
            # Need decimals to convert ui_amount -> smallest unit. Ask a cheap quote with amount=1
            # unit first is wasteful; instead fetch decimals via the quote response itself using a
            # provisional integer guess, then re-quote precisely. Simpler: request token info isn't
            # exposed standalone here, so we probe with 18 decimals (true for BNB/USDT/USDC/wrappers
            # on BSC) and correct using the returned fromToken.decimal if different.
            probe_amount = _to_units(ui_amount, 18)
            quote_params = {
                "binanceChainId": CHAIN_ID,
                "amount": probe_amount,
                "fromTokenAddress": from_addr,
                "toTokenAddress": to_addr,
                "userWalletAddress": taker,
            }
            routes = await _request(client, "GET", "/api/v1/dex/aggregator/quote", params=quote_params)
            if not routes:
                return _fallback_quote(input_mint, output_mint, ui_amount, "quote returned no routes")
            best = next((r for r in routes if r.get("isBest")), routes[0])
            from_dec = int(best["fromToken"]["decimal"])
            if from_dec != 18:
                probe_amount = _to_units(ui_amount, from_dec)
                quote_params["amount"] = probe_amount
                routes = await _request(client, "GET", "/api/v1/dex/aggregator/quote", params=quote_params)
                if not routes:
                    return _fallback_quote(input_mint, output_mint, ui_amount, "re-quote returned no routes")
                best = next((r for r in routes if r.get("isBest")), routes[0])

            to_dec = int(best["toToken"]["decimal"])
            out_ui = _from_units(best["toTokenAmount"], to_dec)
            exec_mode = best.get("executionMode", "SWAP")
            quote_id = best["quoteId"]

            if not taker:
                # No connected wallet yet — price-only, can't build a tx without a sender.
                return {
                    "transaction": None,
                    "deepLink": pancake_link(input_mint, output_mint),
                    "uiOutAmount": out_ui,
                    "uiMinReceived": out_ui * 0.99 if out_ui else None,
                    "rate": (out_ui / ui_amount) if out_ui and ui_amount else None,
                    "priceImpactPct": float(best["priceImpactPercent"]) if best.get("priceImpactPercent") else None,
                    "gasless": False,
                    "routes": [best.get("vendorName", "Binance")],
                    "transferFeeBps": 0,
                    "provider": "binance_web3",
                    "executionMode": exec_mode,
                    "needsWallet": True,
                    "sim": None,
                }

            swap_params = {
                "binanceChainId": CHAIN_ID,
                "amount": probe_amount,
                "fromTokenAddress": from_addr,
                "toTokenAddress": to_addr,
                "userWalletAddress": taker,
                "quoteId": quote_id,
                "slippagePercent": SLIPPAGE_PCT,
            }
            swap_data = await _request(client, "GET", "/api/v1/dex/aggregator/swap", params=swap_params)
            if not isinstance(swap_data, dict):
                raise RuntimeError(f"swap endpoint returned empty/invalid data: {str(swap_data)[:150]}")
            router_result = swap_data.get("routerResult")
            if not isinstance(router_result, dict):
                raise RuntimeError(f"swap response missing routerResult: {str(swap_data)[:300]}")
            out_ui = _from_units(router_result["toTokenAmount"], to_dec)
            exec_mode = swap_data.get("executionMode", "SWAP")

            result = {
                "uiOutAmount": out_ui,
                "rate": (out_ui / ui_amount) if out_ui and ui_amount else None,
                "priceImpactPct": float(router_result["priceImpactPercent"]) if router_result.get("priceImpactPercent") else None,
                "gasless": False,
                "routes": [router_result.get("vendorName", "Binance")],
                "transferFeeBps": 0,
                "provider": "binance_web3",
                "executionMode": exec_mode,
                "deepLink": pancake_link(input_mint, output_mint),
            }
            devlog.log_call(
                what=f"quote mode={exec_mode} {input_mint[:8]}->{output_mint[:8]}",
                url=f"{BASE_URL}/api/v1/dex/aggregator/swap", status="n/a", ms=0,
                actual=f"rate={result['rate']} priceImpactPct={result['priceImpactPct']} vendor={result['routes']}",
            )

            if exec_mode == "RFQ":
                rfq = swap_data.get("rfq") or {}
                result["transaction"] = None
                result["typedDataToSign"] = rfq.get("typedDataToSign")
                result["rfqVendor"] = rfq.get("vendor")
                result["signingScheme"] = rfq.get("signingScheme", "EIP712")
                result["requestId"] = str(uuid.uuid4())  # idempotency key for /order/submit
                # /order/submit's `quoteId` field = rfq.orderId per docs; some responses only
                # carry the route's own quoteId, so fall back to that.
                result["quoteId"] = rfq.get("orderId") or quote_id
                result["uiMinReceived"] = out_ui  # RFQ is a firm quote, no slippage
                result["sim"] = {"ok": None, "gas": None, "error": None, "note": "N/A for RFQ — poll /order/{id} after submit"}
            else:
                tx = swap_data.get("tx")
                if not isinstance(tx, dict):
                    raise RuntimeError(f"swap response missing tx: {str(swap_data)[:300]}")
                built_tx = {
                    "from": tx["from"],
                    "to": tx["to"],
                    "data": tx["data"],
                    "value": hex(int(tx.get("value") or "0")),
                }
                if tx.get("gas"):
                    built_tx["gas"] = hex(int(tx["gas"]))
                if tx.get("gasPrice"):
                    built_tx["gasPrice"] = hex(int(tx["gasPrice"]))
                result["transaction"] = built_tx
                result["uiMinReceived"] = _from_units(tx.get("minReceiveAmount"), to_dec) if tx.get("minReceiveAmount") else None
                result["sim"] = await simulate_transaction(built_tx, taker)

            return result
    except Exception as e:
        log.warning("binance web3 quote failed, falling back to pancake", exc_info=True)
        return _fallback_quote(input_mint, output_mint, ui_amount, f"{type(e).__name__}: {str(e)[:300]}")


async def simulate_transaction(built_tx: dict, taker: str) -> dict:
    """Transaction API dry-run of an already-built SWAP tx, before the wallet
    popup. Best-effort: any failure to reach/parse the simulate endpoint
    degrades to {"ok": None, "error": ...} rather than blocking the quote —
    the wallet's own simulation is still the final safety net.
    Path is our best read of the same Binance Web3 "Transaction API" family
    as quote/swap (unverified against a live response as of writing — see
    DEVEX.md for the first real call's exact body)."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as client:
        with devlog.timed() as t:
            try:
                data = await _request(client, "POST", "/api/v1/dex/aggregator/tx/simulate", json_body={
                    "binanceChainId": CHAIN_ID,
                    "from": built_tx.get("from") or taker,
                    "to": built_tx["to"],
                    "data": built_tx["data"],
                    "value": built_tx.get("value", "0x0"),
                })
            except Exception as e:
                devlog.log_call(
                    what="tx sim", url=f"{BASE_URL}/api/v1/dex/aggregator/tx/simulate",
                    status="exception", ms=t.ms,
                    expected="200 with { success, gasUsed } or similar",
                    error=str(e),
                )
                return {"ok": None, "gas": None, "error": str(e)}
    if not isinstance(data, dict):
        devlog.log_call(
            what="tx sim", url=f"{BASE_URL}/api/v1/dex/aggregator/tx/simulate", status="n/a", ms=t.ms,
            expected="200 with { success, gasUsed } or similar",
            actual="code 0 but empty data payload", body=str(data),
        )
        return {"ok": None, "gas": None, "error": "simulate returned empty data"}
    ok = bool(data.get("success", data.get("ok", True)))
    gas = data.get("gasUsed") or data.get("gas")
    err = None if ok else (data.get("error") or data.get("message") or "simulation failed")
    devlog.log_call(
        what="tx sim", url=f"{BASE_URL}/api/v1/dex/aggregator/tx/simulate", status="n/a", ms=t.ms,
        expected="200 with { success, gasUsed } or similar",
        actual=f"ok={ok} gas={gas}" + (f" error={err}" if err else ""),
        body=str(data),
    )
    return {"ok": ok, "gas": gas, "error": err}


async def submit_rfq_order(request_id: str, user_signature: str, vendor: str, quote_id: str, signing_scheme: str = "EIP712") -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as client:
        try:
            return await _request(client, "POST", "/api/v1/dex/aggregator/order/submit", json_body={
                "requestId": request_id,
                "userSignature": user_signature,
                "vendor": vendor,
                "quoteId": quote_id,
                "signingScheme": signing_scheme,
            })
        except Exception as e:
            devlog.log_call(what="RFQ order submit", url=f"{BASE_URL}/api/v1/dex/aggregator/order/submit",
                             status="exception", ms=0, error=str(e))
            raise


async def rfq_order_status(order_id: str) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as client:
        try:
            return await _request(client, "GET", f"/api/v1/dex/aggregator/order/{order_id}")
        except Exception as e:
            devlog.log_call(what="RFQ order status", url=f"{BASE_URL}/api/v1/dex/aggregator/order/{order_id}",
                             status="exception", ms=0, error=str(e))
            raise
