import os

from fastapi import APIRouter, Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse

import prices
import rwa
import basket
import board
import sessionbook

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _static_v(rel_path: str) -> str:
    try:
        return str(int(os.path.getmtime(os.path.join("static", rel_path))))
    except OSError:
        return "0"


templates.env.globals["static_v"] = _static_v
templates.env.globals["format_premium"] = rwa.format_premium
templates.env.globals["premium_status"] = rwa.premium_status

TELEGRAM_BOT_URL = (
    os.getenv("TELEGRAM_PUBLIC_URL", "").strip()
    or (f"https://t.me/{u}" if (u := os.getenv("TELEGRAM_BOT_USERNAME", "").strip()) else "")
)
templates.env.globals["telegram_bot_url"] = TELEGRAM_BOT_URL


PLATFORM_ORDER = ("xstocks", "ondo", "bstocks")


def _board_context() -> dict:
    snap = rwa.get_cached_snapshot()
    session = snap.get("session") or rwa.session_now()
    return {
        "groups": snap.get("groups") or [],
        "session": session,
        "tape_stale": bool(snap.get("tapeStale")),
        "platform_order": PLATFORM_ORDER,
        "last_session": board.last_sessions(),
        "book_widest": sessionbook.widest_line() if session.get("cashOpen") else None,
    }


@router.get("/", response_class=HTMLResponse)
async def board_page(request: Request):
    return templates.TemplateResponse(request, "board.html", _board_context())


@router.get("/session", response_class=HTMLResponse)
async def session_page(request: Request):
    return templates.TemplateResponse(request, "session.html", {})


@router.get("/basket/ai", response_class=HTMLResponse)
async def basket_ai_page(request: Request):
    return templates.TemplateResponse(request, "basket.html", {"b": await basket.build("ai")})


@router.get("/basket/semis", response_class=HTMLResponse)
async def basket_semis_page(request: Request):
    return templates.TemplateResponse(request, "basket.html", {"b": await basket.build("semis")})


@router.get("/basket/defensive", response_class=HTMLResponse)
async def basket_defensive_page(request: Request):
    return templates.TemplateResponse(request, "basket.html", {"b": await basket.build("defensive")})


@router.get("/board")
async def board_redirect():
    return RedirectResponse(url="/", status_code=302)


@router.get("/wallet", response_class=HTMLResponse)
async def wallet_page(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@router.get("/stocks")
async def stocks_page():
    return RedirectResponse(url="/", status_code=302)


@router.get("/swap", response_class=HTMLResponse)
async def swap_page(request: Request):
    return templates.TemplateResponse(request, "swap.html", {})


@router.get("/t/{symbol}", response_class=HTMLResponse)
async def token_page(request: Request, symbol: str):
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
