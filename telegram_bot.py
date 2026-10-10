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

Trading goes through the user's Binance Agentic Wallet (tg_* modules). No URL ever appears in message
text: links are inline buttons only.
"""
import os
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

from sqlalchemy import select

import news
import rwa
import prices
import routing_rules
from database import SessionLocal
from models import Watch
import tg_wallet
import tg_trade
import tg_send
import tg_book
import tg_adv
import tg_swap
import tg_desk
import tg_ui as ui

log = logging.getLogger("telegram_bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

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




# chat_id -> message_id currently showing a token menu (not the live board).
# The live board editor skips these chats until Back is pressed.
_token_menus: dict[int, int] = ui.screens  # shared with tg_ui.show(): every trade/send/book screen holds the board loop
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
        f"<b>{ui.verdict(row['premium'])}</b>",
    ]
    fair = _fair_line(row).strip()
    if fair:
        lines.append(fair)
    if asset.get("description"):
        lines.append(f"\n{asset['description']}")

    item = next((n for n in news.get_news() if n["symbol"].upper() == symbol_u), None)
    if item:
        lines.append(f"\n📰 {item['body']}")
    return "\n".join(lines)


def _token_menu_markup(chat_id: int, symbol: str, rotate=None) -> InlineKeyboardMarkup:
    """Card layout (Step 7): Buy/Sell, Send/Watch, Price check/Open, Contract/Board."""
    symbol_u = symbol.upper()
    watching = _is_watching(chat_id, symbol_u)
    watch_btn = (ui.cb_btn("👁 Unwatch", f"unwatch:{symbol_u}") if watching
                 else ui.cb_btn(ui.WATCH, f"watch:{symbol_u}"))
    if symbol_u == "BNB":
        return ui.kb([ui.cb_btn(ui.SEND, "snd:BNB"), watch_btn], [ui.cb_btn(ui.BOARD, "back:board")])
    row = _row_for(symbol_u) or {}
    u = row.get("underlying") or rwa.resolve_underlying(symbol_u) or symbol_u
    locked = routing_rules.bot_lock(row.get("platform") or routing_rules.family(symbol_u))
    rows = [
        ([ui.cb_btn("🔒 " + ui.BUY, "lk:x"), ui.cb_btn("🔒 " + ui.SELL, "lk:x")] if locked
         else [ui.cb_btn(ui.BUY, f"buy:{u}"), ui.cb_btn(ui.SELL, f"sell:{u}")]),
        [ui.cb_btn(ui.SEND, f"snd:{symbol_u}"), watch_btn],
        [ui.cb_btn(ui.CHECK, f"chk:{u}:10"), ui.link_btn(ui.OPEN_SITE, ui.site(f"/t/{symbol_u}"))],
    ]
    if rotate:
        rows.append([rotate])
    last = [ui.cb_btn(ui.BOARD, "back:board")]
    if row.get("mint"):
        last.insert(0, ui.link_btn("Contract", ui.scan_tok(row["mint"])))
    rows.append(last)
    return ui.kb(*rows)


def _card_symbol(text: str) -> str:
    """tok:X accepts a wrapper symbol, BNB, or an underlying (NVDA -> its first wrapper)."""
    t = (text or "").upper()
    if t == "BNB" or _row_for(t):
        return t
    u = rwa.resolve_underlying(t)
    row = next((x for x in rwa.get_cached_snapshot().get("tokens", []) if x.get("underlying") == u), None)
    return row["symbol"] if row else t


_bg: set[asyncio.Task] = set()


async def _attach_rotate(bot: Bot, chat_id: int, mid: int, symbol: str) -> None:
    """Background: add [Switch to cheaper version] to a card that is still on screen, once the quotes say it nets positive."""
    try:
        row = _row_for(symbol)
        u = row and (row.get("underlying") or rwa.resolve_underlying(symbol))
        if not u:
            return
        btn = await asyncio.wait_for(tg_adv.rotate_btn(chat_id, u), 25)
        if not btn or ui.cards.get(chat_id) != (mid, symbol):
            return
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid,
                                            reply_markup=_token_menu_markup(chat_id, symbol, rotate=btn))
    except Exception:
        log.debug("telegram_bot: rotate button not attached", exc_info=True)


def _arm(bot: Bot, chat_id: int, mid: int, symbol: str) -> None:
    ui.cards[chat_id] = (mid, symbol)
    if chat_id > 0 and _row_for(symbol):
        t = asyncio.create_task(_attach_rotate(bot, chat_id, mid, symbol))
        _bg.add(t)
        t.add_done_callback(_bg.discard)


@router.callback_query(lambda c: c.data and c.data.startswith("tok:"))
async def on_token_tap(callback: CallbackQuery):
    symbol = _card_symbol(callback.data.split(":", 1)[1])
    chat_id = callback.message.chat.id
    ui.clear_input(chat_id)
    if symbol != "BNB":
        u = (_row_for(symbol) or {}).get("underlying") or rwa.resolve_underlying(symbol)
        v = await tg_desk.stock_view(chat_id, u) if u else None
        if v:
            await ui.show(callback, *v)
            await callback.answer()
            return
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
    _arm(callback.bot, chat_id, callback.message.message_id, symbol)
    await callback.answer()


@router.callback_query(lambda c: c.data == "back:board")
async def on_back(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    ui.clear_input(chat_id)
    ui.cards.pop(chat_id, None)
    await callback.answer()
    await ui.show(callback, *(await tg_desk.home(chat_id)))


async def _refresh_token_menu(callback: CallbackQuery, symbol: str) -> None:
    chat_id = callback.message.chat.id
    u = None if symbol.upper() == "BNB" else (rwa.resolve_underlying(symbol) or None)
    v = await tg_desk.stock_view(chat_id, u) if u else None
    if v:
        await ui.show(callback, *v)
        return
    text = _token_menu_text(symbol, chat_id)
    if not text:
        return
    markup = _token_menu_markup(chat_id, symbol)
    try:
        await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    _arm(callback.bot, chat_id, callback.message.message_id, symbol)


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
    chat_id = message.chat.id
    ui.clear_input(chat_id)
    ui.screens.pop(chat_id, None)
    if payload:
        u = rwa.resolve_underlying(payload.lstrip("/"))
        v = await tg_desk.stock_view(chat_id, u) if u else None
        if v:
            await message.answer(v[0], reply_markup=v[1], disable_web_page_preview=True)
            return
    text, markup = await tg_desk.home(chat_id)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


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
    await message.answer(text, reply_markup=ui.kb([ui.link_btn(ui.OPEN_SITE, ui.site("/board"))]))


async def _send_card(message: Message, symbol: str) -> bool:
    """Stock page for a ticker or any wrapper symbol (/t NVDAB, /nvda)."""
    chat_id = message.chat.id
    u = rwa.resolve_underlying(symbol)
    v = await tg_desk.stock_view(chat_id, u) if u else None
    if not v:
        return False
    ui.clear_input(chat_id)
    await message.answer(v[0], reply_markup=v[1], disable_web_page_preview=True)
    return True


@router.message(Command("t"))
async def on_t(message: Message, command: CommandObject):
    symbol = (command.args or "").strip()
    if not symbol:
        await message.answer("Usage: /t NVDAB")
        return
    if not await _send_card(message, symbol):
        await message.answer(f"Unknown symbol: {symbol}")


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
    await message.answer(text or fallback, reply_markup=agent.agent_markup(underlying) if text else None,
                         disable_web_page_preview=True)


@router.message()
async def on_symbol_shortcut(message: Message):
    """Bare /nvda or /nvdab: the token card."""
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return
    await _send_card(message, text[1:].split("@")[0])


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





_bot: Bot | None = None
_dp: Dispatcher | None = None
_task: asyncio.Task | None = None
_digest_task: asyncio.Task | None = None
_keepalive_task: asyncio.Task | None = None


async def _polling_loop():
    global _bot, _dp
    _bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    _dp = Dispatcher()
    _dp.include_router(tg_wallet.router)
    _dp.include_router(tg_desk.router)
    _dp.include_router(tg_trade.router)
    _dp.include_router(tg_send.router)
    _dp.include_router(tg_book.router)
    _dp.include_router(tg_adv.router)
    _dp.include_router(tg_swap.router)
    _dp.include_router(router)  # last: holds the bare /symbol catch-all

    await _set_profile(_bot)
    global _digest_task, _keepalive_task
    if _keepalive_task is None or _keepalive_task.done():
        _keepalive_task = asyncio.create_task(tg_wallet.keepalive_loop(_bot))
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



COMMANDS = [
    ("start", "Open StreetTape"),
    ("board", "Tape vs mark, every stock"),
    ("book", "Your holdings"),
    ("buy", "Buy in one line: /buy NVDA 50"),
    ("send", "Send BNB, USDT or a stock"),
    ("t", "Stock page: /t NVDA"),
    ("agent", "Rotate and flatten ideas"),
    ("watch", "Watch a stock: /watch NVDA"),
    ("unwatch", "Stop watching a stock"),
    ("watches", "Your watch list"),
    ("session", "Market hours and NY close"),
]
DESCRIPTION = ("StreetTape: tokenized stocks on BNB Chain, made simple. Link a Binance Agentic Wallet once, "
               "then buy, sell and send in a few taps. We show tape vs mark and pick the cheapest version for you.")
SHORT_DESCRIPTION = "Buy, sell and send tokenized stocks on BNB Chain. Tape vs mark, one tap."


async def _set_profile(bot: Bot) -> None:
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
        await bot.set_my_description(DESCRIPTION)
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    except Exception:
        log.warning("telegram_bot: could not set command menu / descriptions", exc_info=True)


async def send_alert(chat_id: int, text: str, buttons: InlineKeyboardMarkup | None = None) -> None:
    """Used by agent.py. Reuses the polling bot; a one-shot Bot is the fallback before polling starts."""
    if _bot is not None:
        await ui.say(_bot, chat_id, text, buttons)
        return
    if not BOT_TOKEN:
        return
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await ui.say(bot, chat_id, text, buttons)
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
