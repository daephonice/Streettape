import os

from fastapi import APIRouter, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse

import prices

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _static_v(rel_path: str) -> str:
    try:
        return str(int(os.path.getmtime(os.path.join("static", rel_path))))
    except OSError:
        return "0"


templates.env.globals["static_v"] = _static_v

TELEGRAM_BOT_URL = (
    os.getenv("TELEGRAM_PUBLIC_URL", "").strip()
    or (f"https://t.me/{u}" if (u := os.getenv("TELEGRAM_BOT_USERNAME", "").strip()) else "")
)
templates.env.globals["telegram_bot_url"] = TELEGRAM_BOT_URL


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@router.get("/stocks")
async def stocks_page():
    return RedirectResponse(url="/", status_code=302)


@router.get("/swap", response_class=HTMLResponse)
async def swap_page(request: Request):
    return templates.TemplateResponse(request, "swap.html", {})


VENUS_URL = "https://app.venus.io/core-pool/markets?chainId=56"


@router.get("/lend", response_class=HTMLResponse)
async def lend_page(request: Request):
    return RedirectResponse(url=VENUS_URL, status_code=302)


@router.get("/lend/{symbol}", response_class=HTMLResponse)
async def lend_vault_page(request: Request, symbol: str):
    return RedirectResponse(url=VENUS_URL, status_code=302)


@router.get("/t/{symbol}", response_class=HTMLResponse)
async def token_page(request: Request, symbol: str):
    import rwa
    sym = (symbol or "").upper()
    underlying = next((u for u in rwa.UNIVERSE if u["underlying"] == sym), None)
    focus_symbol = None
    if underlying is None:
        w = rwa.by_symbol(sym)
        if w is None:
            asset = prices.get_asset(symbol)
            if asset is None:
                await prices.wait_ready()  # cold start: first board refresh may not have landed
                asset = prices.get_asset(symbol)
            if asset is None:
                raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")
            return templates.TemplateResponse(request, "token.html", {"token": asset})
        underlying = next(u for u in rwa.UNIVERSE if u["underlying"] == w["underlying"])
        focus_symbol = w["symbol"]
    return templates.TemplateResponse(request, "token.html", {
        "token": None,
        "underlying": underlying["underlying"],
        "underlying_name": underlying["name"],
        "focus_symbol": focus_symbol,
    })


@router.get("/board", response_class=HTMLResponse)
async def board_page(request: Request):
    import rwa
    import market_stats
    snap = rwa.get_cached_snapshot()
    return templates.TemplateResponse(request, "board.html", {
        "tokens": snap.get("tokens") or [],
        "stats": market_stats.get_stats(),
        "fmt_compact": market_stats.fmt_compact,
        "session": snap.get("session") or rwa.session_now(),
    })
