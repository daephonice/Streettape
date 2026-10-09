import re
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


def _baskets(groups: list) -> list[dict]:
    by = {g["underlying"]: g for g in groups}
    out = []
    for key, (label, names) in basket.THEMES.items():
        ready = [n for n in names if basket._cheapest(by.get(n))]
        out.append({"key": key, "label": label, "ready": ready,
                    "missing": [n for n in names if n not in ready]})
    return out


def _now_block(groups: list, session: dict) -> dict | None:
    """Three plain lines + one button for the top of the board, filled from the cached snapshot."""
    by = {g["underlying"]: g for g in groups}
    u = "NVDA" if basket._cheapest(by.get("NVDA")) else next((g["underlying"] for g in groups if basket._cheapest(g)), None)
    if not u:
        return None
    cheap = basket._cheapest(by[u])["symbol"]
    rich = None
    for w in by[u].get("wrappers") or []:
        p, r, px = w.get("premiumToOfficial"), w.get("tokenToShareRatio"), w.get("tokenPrice")
        if w["symbol"] == cheap or not (w.get("hasTape") and not w.get("thin") and p and p > 0 and r and px):
            continue
        if rich is None or px / r > rich[1]:
            rich = (w["symbol"], px / r)
    cash_open = bool(session.get("cashOpen"))
    l1 = ("Cash is open. The reference is the live stock price." if cash_open
          else "Cash is shut. The reference is the last cash print.")
    if rich:
        l2 = f"{rich[0]} is rich versus that price. {cheap} is the cheaper wrapper of the same stock."
    else:
        l2 = f"{cheap} is the cheapest wrapper of {u} right now."
    return {"underlying": u, "l1": l1, "l2": l2,
            "l3": "This builds an unsigned swap. You sign it. Nothing sends itself."}


def _board_context() -> dict:
    snap = rwa.get_cached_snapshot()
    session = snap.get("session") or rwa.session_now()
    return {
        "baskets": _baskets(snap.get("groups") or []),
        "now_block": _now_block(snap.get("groups") or [], session),
        "groups": snap.get("groups") or [],
        "session": session,
        "tape_stale": bool(snap.get("tapeStale")),
        "platform_order": PLATFORM_ORDER,
        "last_session": board.last_sessions(),
        "book_widest": sessionbook.widest_line() if session.get("cashOpen") else None,
        "settled_tx": _settled_tx(),
        "rotate_tx": _rotate_tx(),
    }


def _settled_tx():
    h = (os.environ.get("SETTLED_TX_HASH") or "").strip()
    return h if re.fullmatch(r"0x[0-9a-fA-F]{64}", h) else None


def _rotate_tx():
    out = []
    for k in ("SETTLED_ROTATE_SELL_TX_HASH", "SETTLED_ROTATE_BUY_TX_HASH"):
        h = (os.environ.get(k) or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", h):
            return None
        out.append(h)
    return {"sell": out[0], "buy": out[1]}


@router.get("/", response_class=HTMLResponse)
async def board_page(request: Request):
    return templates.TemplateResponse(request, "board.html", {**_board_context(), "nav": "board"})


@router.get("/session", response_class=HTMLResponse)
async def session_page(request: Request):
    return templates.TemplateResponse(request, "session.html", {"nav": "session"})


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
    return templates.TemplateResponse(request, "home.html", {"nav": "wallet"})


@router.get("/stocks")
async def stocks_page():
    return RedirectResponse(url="/", status_code=302)


@router.get("/swap", response_class=HTMLResponse)
async def swap_page(request: Request):
    return templates.TemplateResponse(request, "swap.html", {"nav": "swap"})


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
