import logging

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import rwa
import balances
import chart
import prices
import send
import news
import swap as swap_mod
import agent
import market_stats
import rwa_api

log = logging.getLogger("routes_api")
router = APIRouter(prefix="/api")


@router.get("/health")
async def health():
    snap = rwa.get_cached_snapshot()
    return {"ok": True, "fetchedAt": snap.get("fetchedAt"), "session": snap.get("session")}


@router.get("/_devcheck")
async def api_devcheck():
    return await rwa_api.devcheck()


@router.get("/_swapcheck")
async def api_swapcheck(input: str = rwa.NATIVE, output: str = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436",
                        amount: float = 0.005, taker: str | None = None):
    return await swap_mod.quote(input, output, amount, taker)


@router.get("/_mcapcheck")
async def api_mcapcheck():
    pairs = [(u["underlying"], next(w["address"] for w in u["wrappers"] if w.get("address")))
             for u in rwa.UNIVERSE if any(w.get("address") for w in u["wrappers"])]
    return {"mcaps": await rwa_api.mcap_report(pairs), "cached": rwa_api._mcaps}


@router.get("/_devex")
async def api_devex():
    import os
    import devlog
    if not os.path.exists(devlog.PATH):
        raise HTTPException(status_code=404, detail="No DEVEX log yet")
    from fastapi.responses import Response
    with open(devlog.PATH, encoding="utf-8") as f:
        body = f.read() + devlog.summary_md()
    return Response(body, media_type="text/markdown",
                    headers={"Content-Disposition": 'attachment; filename="DEVEX.md"'})


@router.get("/board")
async def get_board():
    return rwa.get_cached_snapshot()


@router.get("/session")
async def api_session():
    return rwa.session_now()


@router.get("/market-stats")
async def api_market_stats():
    return market_stats.get_stats()


@router.get("/sparkline/{symbol}")
async def api_sparkline(symbol: str):
    pts = await chart.get_points(symbol.upper(), "1D")
    return {"symbol": symbol.upper(), "points": [p[1] for p in pts]}


@router.get("/prices")
async def api_prices():
    data = prices.get_prices()
    if not data:
        raise HTTPException(status_code=503, detail="Prices unavailable")
    return data


@router.get("/assets")
async def api_assets():
    return prices.get_assets()


@router.get("/balances/{address}")
async def api_balances(address: str):
    try:
        return await balances.get_balances(address)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid address")
    except Exception:
        log.warning("balances failed %s", address, exc_info=True)
        raise HTTPException(status_code=502, detail="Balance lookup failed")


@router.get("/news")
def api_news():
    return {"items": news.get_news()}


@router.get("/chart/{symbol}")
async def get_chart(symbol: str, range: str = "1D"):
    import rwa as _rwa
    sym, rng = symbol.upper(), range.upper()
    if rng not in chart.RANGES:
        raise HTTPException(status_code=400, detail="Bad range")
    # Underlying symbol (e.g. NVDA) → multi-series response
    if any(u["underlying"] == sym for u in _rwa.UNIVERSE):
        data = await chart.get_multi_points(sym, rng)
        return {"symbol": sym, "range": rng, **data}
    # Wrapper or crypto → single-series (existing behaviour)
    if prices.get_asset(sym) is None:
        raise HTTPException(status_code=404, detail="Unknown symbol")
    return {"symbol": sym, "range": rng, "points": await chart.get_points(sym, rng)}


@router.get("/token/{symbol}")
async def get_token(symbol: str):
    snap = rwa.get_cached_snapshot()
    sym = symbol.upper()
    underlying_meta = next((u for u in rwa.UNIVERSE if u["underlying"] == sym), None)
    if underlying_meta is not None:
        und = sym
        focus = None
    else:
        row = next((t for t in snap.get("tokens", []) if t["symbol"].upper() == sym), None)
        if not row:
            raise HTTPException(status_code=404, detail="Unknown symbol")
        und = row["underlying"]
        focus = row["symbol"]
    group = next((g for g in snap.get("groups", []) if g["underlying"] == und), None)
    if not group:
        raise HTTPException(status_code=404, detail="Unknown symbol")
    out = dict(group)
    out["focusSymbol"] = focus
    out["session"] = snap.get("session")
    return out


@router.get("/agent/scan")
async def api_agent_scan(threshold: float | None = None, underlying: str | None = None):
    report = agent.scan_gaps(threshold)
    arbs = await agent.desk_arbs(underlying)
    report["arbs"] = arbs
    report["best"] = agent.best_arb(arbs)
    return report


@router.get("/agent/studio/tick")
async def api_agent_studio_tick(size_usd: float = 50.0, threshold: float | None = None,
                                x_studio_token: str | None = Header(default=None)):
    """Scheduled-job entrypoint for BNB Agent Studio (call every 60s)."""
    if agent.STUDIO_TOKEN and x_studio_token != agent.STUDIO_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    return await agent.studio_tick(size_usd, threshold)


@router.get("/agent/identity")
async def api_agent_identity():
    return agent.identity_card()


@router.get("/agent/arb/{underlying}")
async def api_agent_arb(underlying: str, size_usd: float = 50.0):
    """Single-underlying rotate quote for the token page's 'Rotate $N' button."""
    hits = agent.check_cross_arb(underlying)
    if not hits:
        return {"underlying": underlying.upper(), "hit": None}
    priced = await agent.net_arb_quote(hits[0], size_usd)
    return {"underlying": underlying.upper(), "hit": priced}


@router.get("/agent/flatten/{underlying}")
async def api_agent_flatten(underlying: str, size_usd: float = 10.0):
    """Richest-wrapper sell quote for the token page's 'Flatten $N' button.
    Reuses agent.flatten_candidates' >2% threshold; None if nothing qualifies."""
    import swap as swap_mod
    hits = agent.flatten_candidates(underlying)
    if not hits:
        return {"underlying": underlying.upper(), "hit": None}
    t = max(hits, key=lambda h: h["premium"])
    px = t.get("tokenPrice") or 0
    if px <= 0:
        return {"underlying": underlying.upper(), "hit": None}
    leg = await swap_mod.quote(t["mint"], rwa.USDT, size_usd / px)
    return {
        "underlying": underlying.upper(),
        "hit": {
            "symbol": t["symbol"],
            "premium": t["premium"],
            "tokenPrice": t["tokenPrice"],
            "sizeUsd": size_usd,
            "sellLeg": leg,
        },
    }


class SwapOrderRequest(BaseModel):
    inputMint: str
    outputMint: str
    uiAmount: float
    taker: str | None = None


@router.post("/swap/order")
async def swap_order(body: SwapOrderRequest):
    if body.uiAmount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    return await swap_mod.quote(body.inputMint, body.outputMint, body.uiAmount, body.taker)


class SwapExecuteRequest(BaseModel):
    signedTransaction: str | None = None
    requestId: str | None = None
    provider: str = "pancake"
    txHash: str | None = None
    # RFQ (executionMode=RFQ) fields
    userSignature: str | None = None
    rfqVendor: str | None = None
    quoteId: str | None = None
    signingScheme: str = "EIP712"


@router.post("/swap/execute")
async def swap_execute(body: SwapExecuteRequest):
    if body.provider == "binance_web3" and body.userSignature:
        try:
            order = await swap_mod.submit_rfq_order(
                body.requestId, body.userSignature, body.rfqVendor, body.quoteId, body.signingScheme,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"status": order.get("status", "PENDING_VENDOR"), "provider": "binance_web3", "orderId": order.get("orderId")}
    if body.provider == "binance_web3" and body.txHash:
        return {"status": "success", "provider": "binance_web3", "txHash": body.txHash}
    return {"status": "external", "provider": "pancake", "txHash": body.txHash}


@router.get("/swap/rfq-status/{order_id}")
async def swap_rfq_status(order_id: str):
    try:
        return await swap_mod.rfq_order_status(order_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


class SendBuildRequest(BaseModel):
    fromAddress: str
    toAddress: str
    symbol: str
    amount: str
    sendMax: bool = False


@router.post("/send/build")
async def send_build(body: SendBuildRequest, request: Request):
    try:
        send.check_rate(_ip(request), "build")
        return await send.build_transfer(body.fromAddress, body.toAddress, body.symbol, body.amount, body.sendMax)
    except send.SendError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except Exception:
        log.warning("send_build failed", exc_info=True)
        raise HTTPException(status_code=502, detail="Could not prepare the transfer, try again")


class SendSubmitRequest(BaseModel):
    signedTransaction: str | None = None
    signature: str | None = None
    lastValidBlockHeight: int = 0
    payer: str | None = None


@router.post("/send/submit")
async def send_submit(body: SendSubmitRequest, request: Request):
    try:
        send.check_rate(_ip(request), "submit")
        return await send.submit(body.signedTransaction, body.signature, body.lastValidBlockHeight, body.payer)
    except send.SendError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except Exception:
        log.warning("send_submit failed", exc_info=True)
        raise HTTPException(status_code=502, detail="Could not send the transaction, try again")
