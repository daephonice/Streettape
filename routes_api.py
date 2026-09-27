import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import rwa
import balances
import chart
import prices
import send
import news
import lend as lend_mod
import swap as swap_mod
import agent
import market_stats

log = logging.getLogger("routes_api")
router = APIRouter(prefix="/api")


@router.get("/health")
async def health():
    snap = rwa.get_cached_snapshot()
    return {"ok": True, "fetchedAt": snap.get("fetchedAt"), "session": snap.get("session")}


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
    hits = agent.check_cross_arb(underlying)[:5]  # quoting is a network round-trip per hit
    report["arbs"] = [await agent.net_arb_quote(h) for h in hits]
    return report


@router.get("/agent/arb/{underlying}")
async def api_agent_arb(underlying: str, size_usd: float = 50.0):
    """Single-underlying rotate quote for the token page's 'Rotate $N' button."""
    hits = agent.check_cross_arb(underlying)
    if not hits:
        return {"underlying": underlying.upper(), "hit": None}
    priced = await agent.net_arb_quote(hits[0], size_usd)
    return {"underlying": underlying.upper(), "hit": priced}


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


class LendQuoteRequest(BaseModel):
    collateralSymbol: str
    debtSymbol: str
    colAmount: float = 0
    debtAmount: float = 0
    user: str | None = None


class LendOperateRequest(BaseModel):
    signer: str
    collateralSymbol: str
    debtSymbol: str
    colAmount: float = 0
    debtAmount: float = 0


class LendLiquidateRequest(BaseModel):
    wallet: str
    collateralSymbol: str
    debtSymbol: str


@router.get("/lend/vaults")
async def api_lend_vaults():
    if not prices.get_assets().get("assets"):
        await prices.wait_ready(timeout=1.5)
    return {"demo": True, "vaults": lend_mod.list_vaults()}


@router.get("/lend/positions")
async def api_lend_positions(user: str):
    if not lend_mod.valid_wallet(user):
        raise HTTPException(status_code=400, detail="Invalid address")
    return {"demo": True, "positions": lend_mod.list_positions(user)}


@router.post("/lend/quote")
async def api_lend_quote(body: LendQuoteRequest):
    try:
        return lend_mod.quote(body.collateralSymbol, body.debtSymbol, body.colAmount, body.debtAmount, body.user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/lend/operate")
async def api_lend_operate(body: LendOperateRequest):
    try:
        return lend_mod.operate(body.signer, body.collateralSymbol, body.debtSymbol, body.colAmount, body.debtAmount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/lend/liquidatable")
async def api_lend_liquidatable():
    return {"positions": lend_mod.list_liquidatable()}


@router.post("/lend/liquidate")
async def api_lend_liquidate(body: LendLiquidateRequest):
    try:
        return lend_mod.liquidate(body.wallet, body.collateralSymbol, body.debtSymbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


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
