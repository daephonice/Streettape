"""Binance Web3 Trading API: quote + build swap (SWAP or RFQ) on BSC. Fallbacks that return a signable tx: PancakeSwap, then OpenOcean. No route = no route (no links)."""
from __future__ import annotations

import time

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
import rpc
import devlog

log = logging.getLogger("swap")

API_KEY = os.getenv("BINANCE_WEB3_API_KEY", "")
SECRET_KEY = os.getenv("BINANCE_WEB3_SECRET_KEY", "")
BASE_URL = "https://web3.binance.com/build"
CHAIN_ID = "56"  # BSC
SLIPPAGE_PCT = "1"  # 1%

PANCAKE_API = "https://swap.pancakeswap.com"
PANCAKE_API_KEY = os.getenv("PANCAKE_API_KEY", "")
PANCAKE_NATIVE = "0x0000000000000000000000000000000000000000"
OPENOCEAN_API = "https://open-api.openocean.finance/v3/bsc"
OPENOCEAN_GAS_GWEI = "1"
QUOTE_ONLY_ACCOUNT = "0x000000000000000000000000000000000000dEaD"  # price-only fallbacks, never signed


UNSUPPORTED_MSG = "Swaps between this token and real-world assets aren't supported yet. Try using a different token."


def _is_ondo(addr: str) -> bool:
    a = (addr or "").lower()
    if rwa.is_ondo(a):
        return True
    for meta in (prices.get_assets().get("assets") or {}).values():
        if (meta.get("mint") or "").lower() == a and (meta.get("platform") or "").lower() == "ondo":
            return True
    return False


def unsupported_pair(input_addr: str, output_addr: str) -> str | None:
    """BNB <-> Ondo is not swappable (buy or sell). Returns the notice text, else None."""
    if (rwa.is_bnb(input_addr) and _is_ondo(output_addr)) or (rwa.is_bnb(output_addr) and _is_ondo(input_addr)):
        return UNSUPPORTED_MSG
    return None


IMPACT_FLOOR = 0.95


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



def _allowance_revert(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in ("allowance", "transfer amount", "exceeds balance", "insufficient", "stf", "erc20"))


async def _eth_ok(tx: dict, taker: str) -> str:
    """ok, allowance, or revert. A missing approval is not a dead route. An RPC outage is not either."""
    try:
        await rpc.call("eth_call", [{"from": taker, "to": tx["to"], "data": tx["data"], "value": tx.get("value") or "0x0"}, "latest"])
        return "ok"
    except RuntimeError as e:
        msg = str(e)
        if _allowance_revert(msg):
            devlog.log_call(what="eth_call allowance", url="bsc", status="allowance", ms=0, error=msg[:200])
            return "allowance"
        if "revert" in msg.lower() or "execution" in msg.lower():
            devlog.log_call(what="eth_call revert", url="bsc", status="revert", ms=0, error=msg[:200])
            return "revert"
        devlog.log_call(what="eth_call skipped", url="bsc", status="rpc", ms=0, error=msg[:200])
        return "ok"
    except Exception as e:
        devlog.log_call(what="eth_call skipped", url="bsc", status="exception", ms=0, error=str(e)[:200])
        return "ok"


def _thin(input_mint: str, output_mint: str, ui_amount: float, out_ui) -> bool:
    inn = _price_of_mint(input_mint)
    out = _price_of_mint(output_mint)
    if not inn or not out or not out_ui or not ui_amount:
        return False
    return (out * out_ui) < IMPACT_FLOOR * (inn * ui_amount)


def _impact_block(input_mint, output_mint, ui_amount, out_ui) -> dict:
    return {
        "transaction": None, "typedDataToSign": None, "uiOutAmount": None, "uiMinReceived": None,
        "rate": None, "priceImpactPct": None, "provider": None, "executionMode": None, "routes": [],
        "priceImpactTooHigh": True, "error": "Price impact too high",
        "fallbackReason": f"output worth under {int(IMPACT_FLOOR*100)}% of input",
        "sim": None,
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
    ok = resp.status_code < 400 and data.get("code") in (0, None) and data.get("success", True)
    devlog.log_call(
        what=f"Binance Web3 {method.upper()} {path}", url=full_url, status=resp.status_code, ms=t.ms,
        expected="code 0 / success true with data payload",
        actual="ok" if ok else f"code={data.get('code')} msg={data.get('msg')}",
        body=None if ok else str(data),
    )
    if not ok:
        raise RuntimeError(f"HTTP {resp.status_code}: " + str(data.get("msg") or f"Binance Web3 API error {data.get('code')}"))
    return data.get("data")


def _to_units(ui_amount: float, decimals: int) -> str:
    from decimal import Decimal
    return str(int(Decimal(repr(float(ui_amount))) * (Decimal(10) ** decimals)))


def _from_units(raw: str, decimals: int) -> float:
    if raw is None:
        return 0.0
    return int(raw) / (10 ** decimals)


async def _binance_quote(input_mint: str, output_mint: str, ui_amount: float, taker: str | None = None) -> dict:
    """Raises on any failure; quote() decides the fallback."""
    if not API_KEY or not SECRET_KEY or ui_amount <= 0:
        raise RuntimeError("keys unset or amount<=0")

    # Aggregator wants native BNB as the placeholder 0xEeee... address (already rwa.NATIVE).
    from_addr = input_mint
    to_addr = output_mint

    if True:
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
                raise RuntimeError("quote returned no routes")
            best = next((r for r in routes if r.get("isBest")), routes[0])
            from_dec = int(best["fromToken"]["decimal"])
            if from_dec != 18:
                probe_amount = _to_units(ui_amount, from_dec)
                quote_params["amount"] = probe_amount
                routes = await _request(client, "GET", "/api/v1/dex/aggregator/quote", params=quote_params)
                if not routes:
                    raise RuntimeError("re-quote returned no routes")
                best = next((r for r in routes if r.get("isBest")), routes[0])

            to_dec = int(best["toToken"]["decimal"])
            out_ui = _from_units(best["toTokenAmount"], to_dec)
            exec_mode = best.get("executionMode", "SWAP")
            quote_id = best["quoteId"]

            if not taker:
                # No connected wallet yet — price-only, can't build a tx without a sender.
                return {
                    "transaction": None,
                    "fees": _fees(best),
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
                "fees": _fees(router_result),
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
                sim = await _eth_ok(built_tx, taker)
                if sim == "allowance":
                    result["needsApproval"] = True
                elif sim == "revert":
                    rfq_route = next((r for r in routes if r.get("executionMode") == "RFQ" and r.get("quoteId")), None)
                    if not rfq_route:
                        raise RuntimeError("SWAP eth_call reverted and no RFQ sibling")
                    devlog.log_call(what="drop LiquidMesh", url=f"{BASE_URL}/api/v1/dex/aggregator/swap", status="revert", ms=0,
                                    expected="signable SWAP", actual=f"eth_call reverted, using RFQ {rfq_route.get('quoteId')}")
                    quote_id = rfq_route["quoteId"]
                    swap_params["quoteId"] = quote_id
                    swap_data = await _request(client, "GET", "/api/v1/dex/aggregator/swap", params=swap_params)
                    router_result = (swap_data or {}).get("routerResult") or {}
                    exec_mode = swap_data.get("executionMode", "RFQ")
                    result["executionMode"] = exec_mode
                    result["routes"] = [router_result.get("vendorName") or rfq_route.get("vendorName") or "PcsXRfq"]
                    rfq = swap_data.get("rfq") or {}
                    result["transaction"] = None
                    result["typedDataToSign"] = rfq.get("typedDataToSign")
                    result["rfqVendor"] = rfq.get("vendor")
                    result["signingScheme"] = rfq.get("signingScheme", "EIP712")
                    result["requestId"] = str(uuid.uuid4())
                    result["quoteId"] = rfq.get("orderId") or quote_id
                    result["uiMinReceived"] = out_ui
                    result["sim"] = {"ok": None, "gas": None, "error": None, "note": "SWAP reverted; RFQ sibling"}
                    if not result["typedDataToSign"]:
                        raise RuntimeError("RFQ sibling had no typedDataToSign")
                    return result
                if tx.get("gas"):
                    built_tx["gas"] = hex(int(tx["gas"]))
                if tx.get("gasPrice"):
                    built_tx["gasPrice"] = hex(int(tx["gasPrice"]))
                result["transaction"] = built_tx
                result["uiMinReceived"] = _from_units(tx.get("minReceiveAmount"), to_dec) if tx.get("minReceiveAmount") else None
                result["sim"] = await simulate_transaction(built_tx, taker)
                gp = built_tx.get("gasPrice"); g = built_tx.get("gas")
                if gp and g and result["fees"]["gasBnb"] is None:
                    result["fees"]["gasBnb"] = int(gp, 16) * int(g, 16) / 1e18

            return result


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _fees(d: dict | None) -> dict:
    """Best-effort fee fields from a Binance route object. None when the API did not send them."""
    d = d or {}
    gas = _num(d.get("estimateGasFee"))
    return {"gasBnb": (gas / 1e18) if gas and gas > 1e6 else gas, "tradeFeeUsd": _num(d.get("tradeFee"))}


def _find(d, keys, depth=0):
    if depth > 4 or not isinstance(d, (dict, list)):
        return None
    if isinstance(d, list):
        for x in d:
            r = _find(x, keys, depth + 1)
            if r is not None:
                return r
        return None
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    for v in d.values():
        r = _find(v, keys, depth + 1)
        if r is not None:
            return r
    return None


def _int(v) -> int:
    s = str(v or "0")
    return int(s, 16) if s.startswith("0x") else int(float(s))


def _tx(taker: str, to: str, data: str, value, gas=None) -> dict:
    tx = {"from": taker, "to": to, "data": data, "value": hex(_int(value))}
    if gas:
        tx["gas"] = hex(_int(gas))
    return tx


def _shaped(provider: str, label: str, out_ui: float, ui_amount: float, min_ui, impact, tx, taker, gas_bnb) -> dict:
    return {
        "transaction": tx if taker else None,
        "uiOutAmount": out_ui,
        "uiMinReceived": min_ui,
        "rate": (out_ui / ui_amount) if out_ui and ui_amount else None,
        "priceImpactPct": impact,
        "gasless": False,
        "routes": [label],
        "transferFeeBps": 0,
        "provider": provider,
        "executionMode": "SWAP",
        "needsWallet": not taker,
        "fees": {"gasBnb": gas_bnb, "tradeFeeUsd": None},
        "sim": None,
    }


async def _get_json(client: httpx.AsyncClient, what: str, method: str, url: str, **kw) -> dict:
    with devlog.timed() as t:
        resp = await client.request(method, url, **kw)
    try:
        data = resp.json()
    except Exception:
        devlog.log_call(what=what, url=str(resp.url), status=resp.status_code, ms=t.ms,
                        expected="JSON body", actual="non-JSON response", body=resp.text)
        raise RuntimeError(f"non-JSON response ({resp.status_code}): {resp.text[:120]}")
    ok = resp.status_code < 400
    devlog.log_call(what=what, url=str(resp.url), status=resp.status_code, ms=t.ms,
                    expected="200 with signable calldata", actual="ok" if ok else str(data)[:160],
                    body=None if ok else str(data))
    if not ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {str(data)[:160]}")
    return data


def _pcsx_typed(pcsx: dict):
    return pcsx.get("permitData") or pcsx.get("typedData") or pcsx.get("typedDataToSign")


async def _pancake_quote(input_mint: str, output_mint: str, ui_amount: float, taker: str | None, dec_in: int = 18) -> dict:
    """PancakeSwap. A pcsx permit is preferred over a pool transaction."""
    pn = lambda a: PANCAKE_NATIVE if a.lower() == rwa.NATIVE.lower() else a
    acct = taker or QUOTE_ONLY_ACCOUNT
    headers = {"x-api-key": PANCAKE_API_KEY} if PANCAKE_API_KEY else {}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        q = await _get_json(client, "Pancake GET /v1/quote", "GET", f"{PANCAKE_API}/v1/quote", params={
            "chainId": CHAIN_ID, "tokenIn": pn(input_mint), "tokenOut": pn(output_mint),
            "amount": _to_units(ui_amount, dec_in), "recipient": acct, "slippageTolerance": str(float(SLIPPAGE_PCT) / 100),
        })
        best = q.get("best") if isinstance(q, dict) else None
        if not isinstance(best, dict):
            raise RuntimeError("pancake: empty quote")
        pcsx = best.get("pcsx") if isinstance(best.get("pcsx"), dict) else None
        typed = _pcsx_typed(pcsx) if pcsx else None
        if typed and taker:
            out_raw = _find(best, ("amountOut", "outputAmount", "outAmount", "toAmount"))
            out_ui = _from_units(str(_int(out_raw)), 18) if out_raw is not None else None
            if not out_ui:
                raise RuntimeError("pancake: pcsx quote had no out amount")
            shaped = _shaped("pancake", "PcsXRfq", out_ui, ui_amount, out_ui, None, None, taker, None)
            shaped["executionMode"] = "RFQ"
            shaped["transaction"] = None
            shaped["typedDataToSign"] = typed
            shaped["rfqVendor"] = "PcsX"
            shaped["signingScheme"] = "EIP712"
            shaped["requestId"] = str(uuid.uuid4())
            shaped["quoteId"] = best.get("quoteId")
            shaped["sim"] = {"ok": None, "gas": None, "error": None, "note": "Pancake X permit"}
            return shaped
        if not best.get("agg"):
            raise RuntimeError("pancake: no agg route and no pcsx permit")
        out_raw = _find(best, ("amountOut", "outputAmount", "outAmount", "toAmount", "amountOutRaw"))
        dec_out = int(_find(best, ("decimals",)) or 18)
        out_ui = _from_units(str(_int(out_raw)), dec_out) if out_raw is not None else None
        if not out_ui:
            raise RuntimeError("pancake: quote had no out amount")
        tx = None
        if taker:
            cd = await _get_json(client, "Pancake POST /v1/calldata", "POST", f"{PANCAKE_API}/v1/calldata", json=best)
            cd = cd.get("data", cd) if isinstance(cd, dict) else {}
            if not (cd.get("to") and cd.get("calldata")):
                raise RuntimeError("pancake: calldata response missing to/calldata")
            tx = _tx(taker, cd["to"], cd["calldata"], cd.get("value"))
            sim = await _eth_ok(tx, taker)
            if sim == "revert":
                raise RuntimeError("pancake calldata eth_call reverted")
        shaped = _shaped("pancake", "PancakeSwap", out_ui, ui_amount, out_ui * (1 - float(SLIPPAGE_PCT) / 100), None, tx, taker, None)
        if taker and sim == "allowance":
            shaped["needsApproval"] = True
        return shaped


async def _openocean_quote(input_mint: str, output_mint: str, ui_amount: float, taker: str | None) -> dict:
    """/swap_quote builds the tx (needs the real taker); /quote is price-only. Plain /swap returned NETWORK_ERROR live."""
    path = "swap_quote" if taker else "quote"
    params = {"inTokenAddress": input_mint, "outTokenAddress": output_mint,
              "amount": f"{ui_amount:.18f}".rstrip("0").rstrip("."), "gasPrice": OPENOCEAN_GAS_GWEI}
    if taker:
        params.update({"slippage": SLIPPAGE_PCT, "account": taker})
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Mozilla/5.0 (compatible; StreetTape/1.0)", "Accept": "application/json"}) as client:
        r = await _get_json(client, f"OpenOcean GET /v3/bsc/{path}", "GET", f"{OPENOCEAN_API}/{path}", params=params)
    d = r.get("data") if isinstance(r, dict) else None
    if r.get("code") not in (200, None) or not isinstance(d, dict) or not d.get("outAmount") or (taker and not (d.get("to") and d.get("data"))):
        raise RuntimeError(f"openocean: code={r.get('code')} {str(r.get('message') or r.get('error') or r.get('reason') or 'no route')[:100]}")
    dec_out = int((d.get("outToken") or {}).get("decimals") or 18)
    out_ui = _from_units(str(_int(d.get("outAmount"))), dec_out)
    min_raw = d.get("minOutAmount")
    min_ui = _from_units(str(_int(min_raw)), dec_out) if min_raw else None
    imp = _num(str(d.get("price_impact") or "").replace("%", ""))
    gas = _num(d.get("estimatedGas"))
    gas_bnb = gas * float(OPENOCEAN_GAS_GWEI) * 1e-9 if gas else None
    tx = _tx(taker, d["to"], d["data"], d.get("value"), d.get("estimatedGas")) if taker else None
    return _shaped("openocean", "OpenOcean", out_ui, ui_amount, min_ui, abs(imp) / 100 if imp is not None else None, tx, taker, gas_bnb)


def _no_route(reason: str, attempts: list) -> dict:
    return {"transaction": None, "uiOutAmount": None, "uiMinReceived": None, "rate": None, "priceImpactPct": None,
            "gasless": False, "routes": [], "transferFeeBps": 0, "provider": None, "executionMode": None,
            "noRoute": True, "fallbackReason": reason, "attempts": attempts, "fees": None, "sim": None}


ROUTE_LABELS = {"binance_web3": "Binance Web3", "pancake": "PancakeSwap", "openocean": "OpenOcean"}


async def quote(input_mint: str, output_mint: str, ui_amount: float, taker: str | None = None) -> dict:
    blocked = unsupported_pair(input_mint, output_mint)
    if blocked:
        return {"unsupported": True, "error": blocked, "transaction": None,
                "uiOutAmount": None, "uiMinReceived": None, "rate": None, "priceImpactPct": None,
                "provider": None, "routes": [], "sim": None}
    if ui_amount <= 0:
        return _no_route("amount<=0", [])
    ondo = _is_ondo(input_mint) or _is_ondo(output_mint)
    steps = [("binance_web3", lambda: _binance_quote(input_mint, output_mint, ui_amount, taker)),
             ("pancake", lambda: _pancake_quote(input_mint, output_mint, ui_amount, taker))]
    if not ondo:
        steps.append(("openocean", lambda: _openocean_quote(input_mint, output_mint, ui_amount, taker)))
    attempts: list = []
    for name, fn in steps:
        try:
            res = await fn()
            if not res.get("uiOutAmount"):
                raise RuntimeError("empty quote")
            if _thin(input_mint, output_mint, ui_amount, res.get("uiOutAmount")):
                inn = _price_of_mint(input_mint)
                out = _price_of_mint(output_mint)
                loss = 1 - ((out * res["uiOutAmount"]) / (inn * ui_amount))
                res["priceImpactTooHigh"] = True
                if res.get("priceImpactPct") in (None, ""):
                    res["priceImpactPct"] = loss
            res["routeLabel"] = ROUTE_LABELS[name] + (f" · {res['routes'][0]}" if res.get("routes") else "")
            res["attempts"] = attempts
            if attempts:
                res["fallbackReason"] = attempts[0]["error"]
                res["fallbackFrom"] = ROUTE_LABELS[attempts[0]["provider"]]
            return res
        except Exception as e:
            log.warning("%s quote failed", name, exc_info=True)
            attempts.append({"provider": name, "error": f"{type(e).__name__}: {str(e)[:200]}"})
    reason = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts)
    return _no_route(reason, attempts)


_sim_parked_until = 0.0
SIM_PARK_SECONDS = 3600.0


async def simulate_transaction(built_tx: dict, taker: str) -> dict:
    """Transaction API dry-run of an already-built SWAP tx, before the wallet
    popup. Best-effort: any failure to reach/parse the simulate endpoint
    degrades to {"ok": None, "error": ...} rather than blocking the quote —
    the wallet's own simulation is still the final safety net.
    Path is our best read of the same Binance Web3 "Transaction API" family
    as quote/swap (unverified against a live response as of writing — see
    DEVEX.md for the first real call's exact body)."""
    global _sim_parked_until
    if time.time() < _sim_parked_until:
        return {"ok": None, "gas": None, "error": "simulate endpoint parked (404 earlier)"}
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
                if "HTTP 404" in str(e):
                    _sim_parked_until = time.time() + SIM_PARK_SECONDS
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


async def submit_pancake_rfq(quote_id: str, user_signature: str) -> dict:
    headers = {"x-api-key": PANCAKE_API_KEY} if PANCAKE_API_KEY else {}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        return await _get_json(client, "Pancake POST /v1/submit", "POST", f"{PANCAKE_API}/v1/submit",
                               json={"quoteId": quote_id, "signature": user_signature, "chainId": int(CHAIN_ID)})


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
