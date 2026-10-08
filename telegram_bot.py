"""StreetTape Telegram bot — optional second door onto the same board data.
Runs as a background asyncio task inside the FastAPI process (started from
main.py's startup event, same pattern as Daephon Casino's telegram_bot).

Commands (spec §2.2):
  /start [SYMBOL]   3-line pitch + site link. With a payload, show that card.
  /board            Grouped list: UNDERLYING mark  b%  on%  x%
  /session          Current market session + NY close countdown
  /t SYMBOL | /symbol   Full wrapper card + site link + PancakeSwap link
  /watch NAME       Persist chat_id+underlying (max 5 per chat). NVDA/NVIDIA/NVDAx/NVDAB all resolve to NVDA.
  /unwatch NAME     Remove
  /watches          List this chat's watches
  /agent [NAME]     Live cross-wrapper arb (net of costs) + flatten list (>2% rich)

Watches currently just mean "included in the hourly digest" (not sent yet).
No premium-threshold alert loop.

Bot never touches wallets or signs anything — swap only happens on the site.
"""
import os
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

from sqlalchemy import select

import news
import rwa
import prices
from database import SessionLocal
from models import Watch

log = logging.getLogger("telegram_bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEB_PUBLIC_URL = os.getenv("WEB_PUBLIC_URL", "").rstrip("/")
PANCAKE_BASE = "https://pancakeswap.finance/swap"

router = Router()

MAX_WATCHES_PER_CHAT = 5


def _row_for(symbol: str) -> dict | None:
    snap = rwa.get_cached_snapshot()
    symbol_u = symbol.upper().lstrip("/")
    return next((t for t in snap.get("tokens", []) if t["symbol"].upper() == symbol_u), None)


def _fair_line(row: dict) -> str:
    """Official vs fair line; fair shown only while cash is shut."""
    if row.get("noYahoo") or not row.get("markPrice"):
        return ""
    session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
    out = f"Official ${row['markPrice']:.2f}"
    if row.get("fairPrice") and not session.get("cashOpen"):
        out += f" · Fair ${row['fairPrice']:.2f} ({rwa.format_premium(row.get('premiumToFair'))})"
    return out + "\n"


def _card_text(row: dict) -> str:
    pct = rwa.format_premium(row["premium"])
    return (
        f"<b>{row['symbol']}</b> — {row.get('name', '')}\n"
        f"{pct} vs mark\n"
        f"Tape ${row['tokenPrice']:.2f} · Mark ${row['markPrice']:.2f}\n"
        f"{_fair_line(row)}"
        f"{WEB_PUBLIC_URL}/t/{row['symbol']}"
    )


def _live_rows() -> list[dict]:
    """One row per tokenized stock for the /start live board: symbol, tape price,
    24h% (falls back to premium if 24h change isn't available yet), sorted
    stable by market cap desc (falls back to the snapshot's existing
    premium-desc order when mc isn't available)."""
    snap = rwa.get_cached_snapshot()
    tokens = snap.get("tokens", [])
    if not tokens:
        return []

    live = prices.get_prices()
    live_prices = (live or {}).get("prices", {})

    rows = []
    for t in tokens:
        sym = t["symbol"].upper()
        p = live_prices.get(sym)
        price = p["price"] if p and p.get("price") is not None else t.get("tokenPrice")
        change24h = p.get("change24h") if p else None
        mc = p.get("mc") if p else None
        rows.append({
            "symbol": t["symbol"],
            "price": price,
            "change24h": change24h,
            "premium": t.get("premium"),
            "mc": mc,
        })

    if any(r["mc"] is not None for r in rows):
        rows.sort(key=lambda r: r["mc"] if r["mc"] is not None else -1, reverse=True)
    # else: keep the snapshot's existing (premium-desc) order — already stable.
    return rows


def _watched_symbols(chat_id: int | None) -> set[str]:
    if chat_id is None:
        return set()
    db = SessionLocal()
    try:
        rows = db.execute(select(Watch.symbol).where(Watch.chat_id == chat_id)).scalars().all()
        return set(rows)
    finally:
        db.close()


def _watched_by_chat(chat_ids: list[int]) -> dict[int, set[str]]:
    if not chat_ids:
        return {}
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Watch.chat_id, Watch.symbol).where(Watch.chat_id.in_(chat_ids))
        ).all()
    finally:
        db.close()
    out: dict[int, set[str]] = {}
    for chat_id, symbol in rows:
        out.setdefault(chat_id, set()).add(symbol)
    return out


def _live_line(row: dict, watched: set[str]) -> str:
    mark = "👁" if row["symbol"].upper() in watched else " "
    price = f"${row['price']:.2f}" if row["price"] is not None else "—"
    if row["change24h"] is not None:
        sign = "+" if row["change24h"] > 0 else ""
        pct = f"{sign}{row['change24h']:.1f}%"
    else:
        pct = rwa.format_premium(row["premium"])
    return f"{mark}{row['symbol']:<10} {price:>10}   {pct:>7}"


def _bnb_line(watched: set[str]) -> str | None:
    live = prices.get_prices()
    sol = (live or {}).get("prices", {}).get("BNB")
    if not sol or sol.get("price") is None:
        return None
    mark = "👁" if "BNB" in watched else " "
    price = f"${sol['price']:.2f}"
    chg = sol.get("change24h")
    pct = f"{'+' if chg and chg > 0 else ''}{chg:.1f}%" if chg is not None else "—"
    return f"{mark}{'BNB':<10} {price:>10}   {pct:>7}"


def _live_board_text(rows: list[dict], chat_id: int | None = None, watched: set[str] | None = None) -> str:
    if watched is None:
        watched = _watched_symbols(chat_id)
    lines = [_live_line(r, watched) for r in rows]
    bnb_line = _bnb_line(watched)
    if bnb_line:
        lines.append(bnb_line)
    body = "\n".join(lines)
    return f"<b>StreetTape — live board</b>\n<code>{body}</code>"


def _live_board_markup(rows: list[dict]) -> InlineKeyboardMarkup:
    """2 cols x 4 rows of tokenized stock symbols (same order as the price list),
    plus BNB full-width as the CTA row. tok:SYMBOL opens that token's menu."""
    buttons = [InlineKeyboardButton(text=r["symbol"], callback_data=f"tok:{r['symbol']}") for r in rows]
    kb: list[list[InlineKeyboardButton]] = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    kb.append([InlineKeyboardButton(text="BNB", callback_data="tok:BNB")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# chat_id -> message_id for the one active live board per chat. In-memory
# only (spec: no DB). A new /start (no payload) replaces the old target.
_live_boards: dict[int, int] = {}
_live_board_lock = asyncio.Lock()

# chat_id -> message_id currently showing a token menu (not the live board).
# The live board editor skips these chats until Back is pressed.
_token_menus: dict[int, int] = {}
_token_menus_lock = asyncio.Lock()


def _is_watching(chat_id: int, symbol: str) -> bool:
    db = SessionLocal()
    try:
        return db.execute(
            select(Watch.id).where(Watch.chat_id == chat_id, Watch.symbol == symbol.upper())
        ).first() is not None
    finally:
        db.close()


def _add_watch(chat_id: int, symbol: str, underlying: str | None = None) -> str:
    """Returns 'added' | 'exists' | 'cap'."""
    symbol_u = symbol.upper()
    db = SessionLocal()
    try:
        existing = db.execute(
            select(Watch).where(Watch.chat_id == chat_id, Watch.symbol == symbol_u)
        ).scalar_one_or_none()
        if existing:
            return "exists"
        count = db.execute(select(Watch.id).where(Watch.chat_id == chat_id)).scalars().all()
        if len(count) >= MAX_WATCHES_PER_CHAT:
            return "cap"
        db.add(Watch(chat_id=chat_id, symbol=symbol_u, underlying=underlying))
        db.commit()
        return "added"
    finally:
        db.close()


def _remove_watch(chat_id: int, symbol: str) -> bool:
    symbol_u = symbol.upper()
    db = SessionLocal()
    try:
        existing = db.execute(
            select(Watch).where(Watch.chat_id == chat_id, Watch.symbol == symbol_u)
        ).scalar_one_or_none()
        if not existing:
            return False
        db.delete(existing)
        db.commit()
        return True
    finally:
        db.close()


def _token_menu_text(symbol: str, chat_id: int | None = None) -> str | None:
    symbol_u = symbol.upper()
    watching = _is_watching(chat_id, symbol_u) if chat_id is not None else False
    prefix = "Watching 👁️ " if watching else ""

    if symbol_u == "BNB":
        live = prices.get_prices()
        sol = (live or {}).get("prices", {}).get("BNB")
        asset = prices.get_asset("BNB")
        if not sol or not asset:
            return None
        price = f"${sol['price']:.2f}" if sol.get("price") is not None else "—"
        lines = [f"<b>{prefix}BNB</b> — {asset['name']}", f"Tape {price}"]
        if asset.get("description"):
            lines.append(f"\n{asset['description']}")
        return "\n".join(lines)

    row = _row_for(symbol_u)
    asset = prices.get_asset(symbol_u)
    if not row or not asset:
        return None
    pct = rwa.format_premium(row["premium"])
    lines = [
        f"<b>{prefix}{row['symbol']}</b> — {row.get('name', '')}",
        f"Tape ${row['tokenPrice']:.2f} · Mark ${row['markPrice']:.2f} · {pct}",
    ]
    fair = _fair_line(row).strip()
    if fair:
        lines.append(fair)
    if asset.get("description"):
        lines.append(f"\n{asset['description']}")

    item = next((n for n in news.get_news() if n["symbol"].upper() == symbol_u), None)
    if item:
        lines.append(f"\n📰 {item['body']}")

    jup_link = f"{PANCAKE_BASE}?chain=bsc&inputCurrency=USDT&outputCurrency={row['mint']}"
    lines.append(f"\nSWAP ON PANCAKESWAP\n{jup_link}")
    return "\n".join(lines)


def _token_menu_markup(chat_id: int, symbol: str) -> InlineKeyboardMarkup:
    symbol_u = symbol.upper()
    watch_label = "Watching" if _is_watching(chat_id, symbol_u) else "Watch 👁️"
    kb = [[
        InlineKeyboardButton(text=watch_label, callback_data=f"watch:{symbol_u}"),
        InlineKeyboardButton(text="Unwatch ❌", callback_data=f"unwatch:{symbol_u}"),
    ]]
    if symbol_u != "BNB":
        kb.append([InlineKeyboardButton(text="Open on StreetTape", url=f"{WEB_PUBLIC_URL}/t/{symbol_u}")])
    kb.append([InlineKeyboardButton(text="◀ Back", callback_data="back:board")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("tok:"))
async def on_token_tap(callback: CallbackQuery):
    symbol = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    text = _token_menu_text(symbol, chat_id)
    if not text:
        await callback.answer()
        return
    markup = _token_menu_markup(chat_id, symbol)
    try:
        await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    async with _token_menus_lock:
        _token_menus[chat_id] = callback.message.message_id
    await callback.answer()


@router.callback_query(lambda c: c.data == "back:board")
async def on_back(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    rows = _live_rows()
    if rows:
        text = (
            "StreetTape — mark vs tape for tokenized stocks.\n"
            "We show where the onchain price and the issuer mark disagree, and let you trade the gap.\n\n"
        ) + _live_board_text(rows, chat_id)
        markup = _live_board_markup(rows)
    else:
        text = "Board is warming up — try again in a moment."
        markup = None
    try:
        await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    async with _token_menus_lock:
        _token_menus.pop(chat_id, None)
    async with _live_board_lock:
        _live_boards[chat_id] = callback.message.message_id
    await callback.answer()


async def _refresh_token_menu(callback: CallbackQuery, symbol: str) -> None:
    chat_id = callback.message.chat.id
    text = _token_menu_text(symbol, chat_id)
    if not text:
        return
    markup = _token_menu_markup(chat_id, symbol)
    try:
        await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


@router.callback_query(lambda c: c.data and c.data.startswith("watch:"))
async def on_watch_cb(callback: CallbackQuery):
    symbol = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    result = _add_watch(chat_id, symbol)
    if result == "cap":
        await callback.answer(f"Watch limit reached ({MAX_WATCHES_PER_CHAT}). Unwatch one first.", show_alert=True)
        return
    await _refresh_token_menu(callback, symbol)
    await callback.answer("Watching" if result == "added" else "Already watching")


@router.callback_query(lambda c: c.data and c.data.startswith("unwatch:"))
async def on_unwatch_cb(callback: CallbackQuery):
    symbol = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    removed = _remove_watch(chat_id, symbol)
    await _refresh_token_menu(callback, symbol)
    await callback.answer("Unwatched" if removed else "Not watching")


@router.message(Command("start"))
async def on_start(message: Message, command: CommandObject):
    payload = (command.args or "").strip()
    if payload:
        chat_id = message.chat.id
        text = _token_menu_text(payload, chat_id)
        if text:
            symbol_u = payload.upper().lstrip("/")
            sent = await message.answer(
                text, reply_markup=_token_menu_markup(chat_id, symbol_u), disable_web_page_preview=True
            )
            async with _token_menus_lock:
                _token_menus[chat_id] = sent.message_id
            return

    text = (
        "StreetTape — mark vs tape for tokenized stocks.\n"
        "We show where the onchain price and the issuer mark disagree, and let you trade the gap.\n\n"
    )
    rows = _live_rows()
    if rows:
        text += _live_board_text(rows, message.chat.id)
        markup = _live_board_markup(rows)
    else:
        text += "Board is warming up — try again in a moment."
        markup = None

    sent = await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
    async with _live_board_lock:
        _live_boards[message.chat.id] = sent.message_id


def _wrapper_tag(platform: str) -> str:
    return {"bstocks": "b", "ondo": "on", "xstocks": "x"}.get(platform, platform[:2])


def _group_line(g: dict) -> str:
    tags = "/".join(_wrapper_tag(w["platform"]) for w in g["wrappers"])
    if g.get("noYahoo"):
        return f"{g['underlying']:<6} No Yahoo mark. Tape only.   {tags}"
    off = rwa.format_premium(g.get("premiumToOfficial"))
    fair = rwa.format_premium(g.get("premiumToFair")) if g.get("fairPrice") else "—"
    return f"{g['underlying']:<6} off{off:>7}  fair{fair:>7}  {tags}"


@router.message(Command("board"))
async def on_board(message: Message):
    snap = rwa.get_cached_snapshot()
    groups = snap.get("groups") or []
    if not groups:
        await message.answer("Board is warming up — try again in a moment.")
        return
    lines = [_group_line(g) for g in groups]
    text = "<code>" + "\n".join(lines) + "</code>"
    await message.answer(text)


@router.message(Command("t"))
async def on_t(message: Message, command: CommandObject):
    symbol = (command.args or "").strip()
    if not symbol:
        await message.answer("Usage: /t SPACEX")
        return
    row = _row_for(symbol)
    if not row:
        await message.answer(f"Unknown symbol: {symbol}")
        return
    jup_link = f"{PANCAKE_BASE}?chain=bsc&inputCurrency=USDT&outputCurrency={row['mint']}"
    text = _card_text(row) + f"\nTrade: {WEB_PUBLIC_URL}/t/{row['symbol']}#swap\nPancakeSwap: {jup_link}"
    await message.answer(text, disable_web_page_preview=True)


@router.message(Command("watch"))
async def on_watch(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Usage: /watch NVDA")
        return
    underlying = rwa.resolve_underlying(arg)
    if not underlying:
        await message.answer(f"Unknown symbol: {arg}")
        return

    result = _add_watch(message.chat.id, underlying, underlying=underlying)
    if result == "cap":
        await message.answer(f"Watch limit reached ({MAX_WATCHES_PER_CHAT}). /unwatch one first.")
        return
    entry = rwa.by_underlying(underlying)
    label = entry["name"] if entry else underlying
    verb = "Watching" if result == "added" else "Already watching"
    await message.answer(f"{verb} {underlying} ({label}). /session for market hours, /board for the tape.")


@router.message(Command("unwatch"))
async def on_unwatch(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Usage: /unwatch NVDA")
        return
    underlying = rwa.resolve_underlying(arg)
    if not underlying:
        await message.answer(f"Unknown symbol: {arg}")
        return

    removed = _remove_watch(message.chat.id, underlying)
    await message.answer(f"Unwatched {underlying}." if removed else f"Not watching {underlying}.")


@router.message(Command("watches"))
async def on_watches(message: Message):
    db = SessionLocal()
    try:
        rows = db.execute(select(Watch).where(Watch.chat_id == message.chat.id)).scalars().all()
    finally:
        db.close()
    if not rows:
        await message.answer("No watches yet. /watch NVDA to start.")
        return
    lines = [w.underlying or w.symbol for w in rows]
    await message.answer("\n".join(lines))


@router.message(Command("session"))
async def on_session(message: Message):
    s = rwa.session_now()
    text = (
        f"<b>{s['label']}</b>\n"
        f"ET {s['et']} · WAT {s['wat']} · UTC {s['utc'][11:16]}\n"
        f"Next NY close in {s['nyCloseInSec'] // 3600}h {(s['nyCloseInSec'] % 3600) // 60}m ({s['nyCloseAtWat']} WAT)"
    )
    await message.answer(text)


@router.message(Command("buy"))
async def on_buy(message: Message, command: CommandObject):
    import basket
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Usage: /buy NVDA 50")
        return
    u = rwa.resolve_underlying(parts[0])
    if not u:
        await message.answer(f"Unknown symbol: {parts[0]}")
        return
    try:
        usd = float(parts[1].lstrip("$")) if len(parts) > 1 else 10.0
    except ValueError:
        await message.answer("Usage: /buy NVDA 50")
        return
    if not 1 <= usd <= 1000:
        await message.answer("Dollars must be 1 to 1000.")
        return
    r = await basket.quick_pick(u, usd)
    lines = basket.pick_lines(r)
    if r.get("mark"):
        lines.insert(0, f"{u} cash print ${r['mark']:,.2f}, band +/-{r['bandBps']:.0f} bps, ${usd:g}")
    if not r.get("ok"):
        lines.append(r.get("reason") or "Not available right now")
        await message.answer("<code>" + "\n".join(lines) + "</code>")
        return
    link = f"{WEB_PUBLIC_URL}/t/{r['symbol']}#swap?pay=USDT&amt={usd:g}"
    scan = f"https://bscscan.com/token/{r['contract']}"
    await message.answer("<code>" + "\n".join(lines) + f"</code>\nBuy {r['symbol']}, you sign in your wallet. Open in your wallet's dApp browser and connect first, the Buy sheet opens after connect (Telegram's in-app browser has no wallet):\n{link}\nContract: {scan}", disable_web_page_preview=True)


@router.message(Command("agent"))
async def on_agent(message: Message, command: CommandObject):
    import agent
    arg = (command.args or "").strip()
    underlying = None
    if arg:
        underlying = rwa.resolve_underlying(arg)
        if not underlying:
            await message.answer(f"Unknown symbol: {arg}")
            return
    text = await agent.agent_text(underlying)
    fallback = (
        f"Nothing to act on — {underlying} wrappers are within 1% of each other and none is >2% rich."
        if underlying else
        "Nothing to act on — wrappers are within 1% of each other and none is >2% rich."
    )
    await message.answer(text or fallback, disable_web_page_preview=True)


@router.message()
async def on_symbol_shortcut(message: Message):
    """Bare /spacex style shortcut per spec §2.2."""
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return
    symbol = text[1:].split("@")[0]
    row = _row_for(symbol)
    if not row:
        return
    jup_link = f"{PANCAKE_BASE}?chain=bsc&inputCurrency=USDT&outputCurrency={row['mint']}"
    reply = _card_text(row) + f"\nTrade: {WEB_PUBLIC_URL}/t/{row['symbol']}#swap\nPancakeSwap: {jup_link}"
    await message.answer(reply, disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Hourly per-symbol digest — each of the 9 names gets its own minute
# (index * 7: :00, :07, ... :56). In-memory only: keeps the price from the
# symbol's previous slot (~60m ago) to compute the 1h move. No DB.
# ---------------------------------------------------------------------------

DIGEST_SLOT_MINUTES = 7

_last_slot_price: dict[str, float] = {}  # symbol -> price at its last digest slot
_last_digest_minute: int = -1


def _digest_symbol_order() -> list[str]:
    """Same order as the start board (mc desc), BNB last."""
    rows = _live_rows()
    order = [r["symbol"].upper() for r in rows]
    order.append("BNB")
    return order


def _fmt_mc(mc: float | None) -> str | None:
    if mc is None:
        return None
    if mc >= 1_000_000_000:
        return f"${mc / 1_000_000_000:.2f}B"
    if mc >= 1_000_000:
        return f"${mc / 1_000_000:.2f}M"
    return f"${mc:,.0f}"


def _digest_text(symbol: str) -> str | None:
    live = prices.get_prices()
    entry = (live or {}).get("prices", {}).get(symbol)
    if not entry or entry.get("price") is None:
        return None
    price = entry["price"]

    prev = _last_slot_price.get(symbol)
    if prev and prev > 0:
        change1h = (price / prev - 1) * 100
    else:
        change1h = None
    if change1h is None or change1h == 0:
        return None  # no prior price, or genuinely flat — don't invent a move

    sign = "+" if change1h > 0 else ""
    lines = [f"<b>{symbol}</b>  ${price:.2f}  ({sign}{change1h:.1f}% / 1h)"]
    if entry.get("mark") is not None:
        lines.append(f"Mark ${entry['mark']:.2f}")
    mc = _fmt_mc(entry.get("mc"))
    if mc:
        lines.append(f"Underlying market cap {mc}")
    lines.append(f"{symbol} just moved {sign}{change1h:.1f}% in the last hr")
    return "\n".join(lines)


async def _send_digest_for_symbol(bot: Bot, symbol: str) -> None:
    text = _digest_text(symbol)
    if not text:
        return

    db = SessionLocal()
    try:
        watchers = db.execute(select(Watch).where(Watch.symbol == symbol)).scalars().all()
    finally:
        db.close()
    if not watchers:
        return

    dead_ids: list[int] = []
    for w in watchers:
        try:
            await bot.send_message(w.chat_id, text, disable_web_page_preview=True)
        except (TelegramForbiddenError, TelegramBadRequest):
            dead_ids.append(w.id)
        except Exception:
            log.warning("telegram_bot: digest send failed for chat %s", w.chat_id, exc_info=True)

    if dead_ids:
        db = SessionLocal()
        try:
            rows = db.execute(select(Watch).where(Watch.id.in_(dead_ids))).scalars().all()
            for w in rows:
                db.delete(w)
            db.commit()
        finally:
            db.close()


async def _run_digest_pass(bot: Bot):
    global _last_digest_minute
    now = datetime.now(timezone.utc)
    minute = now.minute
    if minute == _last_digest_minute:
        return
    _last_digest_minute = minute

    if minute % DIGEST_SLOT_MINUTES != 0:
        return
    slot_index = minute // DIGEST_SLOT_MINUTES  # 0..8

    order = _digest_symbol_order()
    if slot_index >= len(order):
        return
    symbol = order[slot_index]

    await _send_digest_for_symbol(bot, symbol)

    live = prices.get_prices()
    entry = (live or {}).get("prices", {}).get(symbol)
    if entry and entry.get("price") is not None:
        _last_slot_price[symbol] = entry["price"]


async def _digest_loop(bot: Bot):
    while True:
        try:
            await _run_digest_pass(bot)
        except Exception:
            log.exception("telegram_bot: digest loop iteration failed")
        await asyncio.sleep(15)


# ---------------------------------------------------------------------------
# Live board loop — edits each chat's /start message every 5s.
# ---------------------------------------------------------------------------

LIVE_BOARD_INTERVAL = 5

_DEAD_MESSAGE_MARKERS = (
    "message to edit not found",
    "message to be edited not found",
    "message can't be found",
    "chat not found",
)


async def _run_live_board_pass(bot: Bot):
    rows = _live_rows()
    if not rows:
        return
    markup = _live_board_markup(rows)

    async with _live_board_lock:
        targets = list(_live_boards.items())
    async with _token_menus_lock:
        on_token_menu = set(_token_menus.keys())
    targets = [(c, m) for c, m in targets if c not in on_token_menu]
    watched_by_chat = _watched_by_chat([c for c, _ in targets])

    dead: list[tuple[int, int]] = []  # (chat_id, message_id) pairs to drop
    for chat_id, message_id in targets:
        text = _live_board_text(rows, watched=watched_by_chat.get(chat_id, set()))
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                continue
            if any(marker in msg for marker in _DEAD_MESSAGE_MARKERS):
                dead.append((chat_id, message_id))
            else:
                log.warning("telegram_bot: live board edit failed for chat %s: %s", chat_id, e)
        except TelegramForbiddenError:
            # Bot was blocked / chat gone.
            dead.append((chat_id, message_id))
        except Exception:
            log.warning("telegram_bot: live board edit failed for chat %s", chat_id, exc_info=True)

    if dead:
        async with _live_board_lock:
            for chat_id, message_id in dead:
                # Only drop if it's still pointing at the message we just
                # failed on — a fresh /start may have replaced it since.
                if _live_boards.get(chat_id) == message_id:
                    del _live_boards[chat_id]


async def _live_board_loop(bot: Bot):
    while True:
        try:
            await _run_live_board_pass(bot)
        except Exception:
            log.exception("telegram_bot: live board loop iteration failed")
        await asyncio.sleep(LIVE_BOARD_INTERVAL)


_bot: Bot | None = None
_dp: Dispatcher | None = None
_task: asyncio.Task | None = None
_live_board_task: asyncio.Task | None = None
_digest_task: asyncio.Task | None = None


async def _polling_loop():
    global _bot, _dp
    _bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    _dp = Dispatcher()
    _dp.include_router(router)

    global _live_board_task, _digest_task
    if _live_board_task is None or _live_board_task.done():
        _live_board_task = asyncio.create_task(_live_board_loop(_bot))
    if _digest_task is None or _digest_task.done():
        _digest_task = asyncio.create_task(_digest_loop(_bot))

    while True:
        try:
            for attempt in range(5):
                try:
                    await _bot.delete_webhook(drop_pending_updates=True)
                    break
                except Exception:
                    log.warning("telegram_bot: delete_webhook attempt %d failed, retrying", attempt + 1)
                    await asyncio.sleep(min(2 ** attempt, 30))
            await _dp.start_polling(_bot, handle_signals=False)
        except Exception:
            log.exception("telegram_bot: polling loop crashed, restarting in 10s")
        await asyncio.sleep(10)



_bot_ref = None

async def send_alert(chat_id: int, text: str) -> None:
    """Used by agent.py. Requires the bot loop to have started."""
    token = BOT_TOKEN
    if not token:
        return
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await bot.send_message(chat_id, text, disable_web_page_preview=True)
    finally:
        await bot.session.close()


def start_telegram_bot_task() -> None:
    """No-ops if TELEGRAM_BOT_TOKEN isn't set — safe to always call from
    main.py's startup event regardless of whether the bot is configured."""
    global _task
    if not BOT_TOKEN:
        log.warning("telegram_bot: TELEGRAM_BOT_TOKEN not set, bot disabled")
        return
    if _task is None or _task.done():
        _task = asyncio.create_task(_polling_loop())
