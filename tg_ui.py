"""StreetTape bot: all copy, keyboards and the no-links send guard (pure helpers).
No URL ever goes in message text. Links are inline URL buttons only."""
import os
import re
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile

log = logging.getLogger("tg_ui")

_URL_RE = re.compile(r"https?://\S+", re.I)

# ---- links (Step 6) --------------------------------------------------------

def site(path: str = "") -> str:
    u = os.getenv("WEB_PUBLIC_URL", "").strip().rstrip("/")
    if not u.startswith("http"):
        u = "https://" + u
    return u + path


def scan_tx(h: str) -> str:
    return f"https://bscscan.com/tx/{h}"


def scan_tok(a: str) -> str:
    return f"https://bscscan.com/token/{a}"


def scan_addr(a: str) -> str:
    return f"https://bscscan.com/address/{a}"


def link_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def cb_btn(text: str, data: str) -> InlineKeyboardButton:
    assert len(data.encode()) <= 64, f"callback data too long: {data}"
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


# ---- guarded senders (Step 6) ---------------------------------------------

def clean(text: str) -> str:
    """Tests (TG_STRICT=1 or pytest) fail loudly; production strips and logs."""
    if "http" not in (text or ""):
        return text
    if os.getenv("TG_STRICT") or os.getenv("PYTEST_CURRENT_TEST"):
        raise AssertionError(f"URL in bot text: {text[:80]!r}")
    log.warning("tg_ui: stripped URL from outgoing text")
    return _URL_RE.sub("", text).strip()


async def say(bot, chat_id: int, text: str, markup=None):
    return await bot.send_message(chat_id, clean(text), reply_markup=markup,
                                  disable_web_page_preview=True)


async def edit(bot, chat_id: int, message_id: int, text: str, markup=None):
    return await bot.edit_message_text(clean(text), chat_id=chat_id, message_id=message_id,
                                       reply_markup=markup, disable_web_page_preview=True)


async def say_photo(bot, chat_id: int, png: bytes, caption: str, markup=None):
    return await bot.send_photo(chat_id, BufferedInputFile(png, filename="link.png"),
                                caption=clean(caption), reply_markup=markup)


# One screen per chat edits the same message the live board uses. While a chat is in
# `screens` the board loop skips it; back:board clears it (telegram_bot aliases this dict).
screens: dict[int, int] = {}


# Which typed reply a chat is expected to send: "usd" (custom buy) or "send" (address / amount).
inputs: dict[int, str] = {}


def clear_input(chat_id: int):
    inputs.pop(int(chat_id), None)


# chat -> (message_id, symbol) while that message shows a token card. Any other screen clears it,
# so a late background edit (Switch button) never lands on the wrong screen.
cards: dict[int, tuple[int, str]] = {}


def hold(chat_id: int, message_id: int):
    screens[int(chat_id)] = message_id


async def show(cb, text: str, markup=None):
    """Edit the tapped message into a new screen and pause the live board for this chat."""
    try:
        await edit(cb.bot, cb.message.chat.id, cb.message.message_id, text, markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    cards.pop(cb.message.chat.id, None)
    hold(cb.message.chat.id, cb.message.message_id)


# ---- vocabulary (Step 7) ---------------------------------------------------

BOARD = "📊 Board"
BOOK = "📒 Book"
LINK = "🔑 Link wallet"
LINKED = "🔑 Wallet ✓"
UNLINK = "Unlink"
BUY = "🟢 Buy"
SELL = "🔴 Sell"
SEND = "📤 Send"
WATCH = "👁 Watch"
CHECK = "🔍 Price check"
OPEN_SITE = "🌐 Open on StreetTape"
BACK = "◀ Back"
BUY_AMOUNTS = (10, 25, 50, 100)
CUSTOM = "Custom"
VIEW_TX = "View on BscScan"
ROTATE = "🔁 Switch to cheaper version"
PACK = "🧺 AI starter pack · $50"


def confirm_label(usd: float) -> str:
    return f"Confirm · ${usd:g}"


def session_line(session: dict) -> str:
    label = (session or {}).get("label") or "—"
    return f"{label} · cash {'open' if (session or {}).get('cashOpen') else 'shut'}"


def verdict(premium) -> str:
    if premium is None:
        return "No mark yet"
    if premium <= -0.005:
        return "Cheap vs mark"
    if premium >= 0.005:
        return "Rich vs mark"
    return "In line"


def short_addr(a: str) -> str:
    return f"{a[:6]}…{a[-6:]}" if a and len(a) > 14 else (a or "")


# ---- link screens (Step 5 copy) -------------------------------------------

def link_intro_text() -> str:
    return ("<b>Link wallet</b>\n"
            "Connect a Binance Agentic Wallet once. After that you buy, sell and send "
            "here with one tap each. No signing sheet.\n"
            "<i>Private chat only.</i>")


def link_intro_kb():
    return kb([cb_btn(LINK, "link:go")], [cb_btn(BOARD, "back:board")])


def link_pending_caption(code: str) -> str:
    return ("<b>Link wallet</b>\n"
            f"Pairing code <code>{code}</code>\n"
            "Phone: tap Open Binance. Computer: scan the QR with the Binance app.\n"
            "<i>Code lasts about 5 minutes.</i>")


def link_pending_kb(url: str):
    return kb([link_btn("Open Binance", url)])


def link_expired_text() -> str:
    return "That code expired. Nothing was linked. Try again."


def link_busy_text() -> str:
    return "Link already in progress. Finish it in the Binance app."


def linked_text(address: str, usdt: float | None, min_buy: float = 10) -> str:
    t = ("<b>Wallet linked</b>\n"
         f"Deposit address (BNB Smart Chain, tap to copy):\n<code>{address}</code>\n")
    if usdt is not None:
        t += f"USDT <code>{usdt:,.2f}</code>\n"
        if usdt < min_buy:
            t += "Send USDT on BNB Smart Chain to the address above to start."
    return t


def linked_kb(address: str):
    return kb([link_btn("View address", scan_addr(address))],
              [cb_btn(BOARD, "back:board"), cb_btn(BOOK, "book")],
              [cb_btn(UNLINK, "link:off")])


def unlinked_text() -> str:
    return "Wallet unlinked. Funds stay in the wallet."


def expired_text() -> str:
    return "Wallet session ended. Link again to keep trading."


def renew_text() -> str:
    return "Wallet link ends within a day. Refresh it now to keep trading."


def renew_kb():
    return kb([cb_btn("Refresh link", "link:renew")])


def private_only_text() -> str:
    return "Trading is private-chat only. Open the bot directly."


def need_link_kb():
    return kb([cb_btn(LINK, "link:go")])


# ---- buy / sell copy (Steps 9-10) ------------------------------------------

def tail_row(*extra):
    return [*extra, cb_btn(BOOK, "book"), cb_btn(BOARD, "back:board")]


def amount_text(u: str) -> str:
    return f"<b>Buy {u}</b>\nHow many dollars? We pick the best version."


def amount_kb(u: str):
    a = [cb_btn(f"${n}", f"ba:{u}:{n}") for n in BUY_AMOUNTS]
    return kb(a[:2], a[2:], [cb_btn(CUSTOM, f"bc:{u}")], [cb_btn(BACK, f"tok:{u}")])


def custom_text() -> str:
    return "Type a dollar amount, 1 to 1000."


def refuse_kb(u: str, usd: float):
    return kb([cb_btn(CHECK, f"chk:{u}:{usd:g}")], [cb_btn(BACK, f"tok:{u}")])


def no_route_text() -> str:
    return "No automatic route for this one."


def finish_kb(sym: str, usd: float, u: str):
    path = f"/t/{sym}#swap?pay=USDT&amt={usd:g}" if usd else f"/t/{sym}#swap"
    return kb([link_btn("Finish in wallet", site(path))],
              [cb_btn(BACK, f"tok:{u}")])


def moved_text() -> str:
    return "Price moved, try again."


def funds_text(address: str, have: float, need: float) -> str:
    return (f"Not enough USDT. Have <code>{have:,.2f}</code>, need <code>{need:,.2f}</code>.\n"
            f"Deposit on BNB Smart Chain:\n<code>{address}</code>")


def buy_confirm_text(u: str, usd: float, out: float, sym: str, n: int) -> str:
    return (f"<b>Buy ${usd:g} of {u}</b>\n"
            f"You get ≈ <code>{out:.4g}</code> {sym}\n"
            f"Picked: cheapest of {n} versions\n"
            "<i>Quote good for 60s</i>")


def sell_amount_text(u: str, sym: str, shares: float) -> str:
    return f"<b>Sell {u}</b>\nYou hold <code>{shares:.4g}</code> shares as {sym}. How much?"


def sell_amount_kb(u: str):
    return kb([cb_btn("25%", f"sp:{u}:25"), cb_btn("50%", f"sp:{u}:50"), cb_btn("All", f"sp:{u}:100")],
              [cb_btn(BACK, f"tok:{u}")])


def sell_confirm_text(u: str, pct: int, qty: float, sym: str, out: float, warn: str | None) -> str:
    t = (f"<b>Sell {'all' if pct == 100 else str(pct) + '%'} of {u}</b>\n"
         f"Sell <code>{qty:.6g}</code> {sym}\n"
         f"You get ≈ $<code>{out:,.2f}</code>\n")
    if warn:
        t += f"{warn}\n"
    return t + "<i>Quote good for 60s</i>"


def sell_label(out: float, warn: bool) -> str:
    return "Sell anyway" if warn else f"Confirm · ≈${out:,.2f}"


def confirm_kb(label: str, pid: str, u: str):
    return kb([cb_btn(label, f"go:{pid}"), cb_btn("Cancel", f"no:{pid}")])


def cancelled_kb(u: str):
    return kb([cb_btn(BACK, f"tok:{u}"), cb_btn(BOARD, "back:board")])


def working_text(what: str) -> str:
    return f"{what}…"


def receipt_kb(tx: str | None):
    return kb(tail_row(*([link_btn(VIEW_TX, scan_tx(tx))] if tx else [])))


def no_holding_text(u: str) -> str:
    return f"You hold no {u}."


def not_open_text() -> str:
    return "Ondo wrappers only trade while the US market is open."


# ---- one-tap advanced (Step 13) --------------------------------------------

def adv_confirm_kb(label: str, pid: str):
    return kb([cb_btn(label, f"ax:{pid}"), cb_btn("Cancel", f"an:{pid}")])


def rotate_none_text(u: str, why: str, min_usd: float = 5.0) -> str:
    if why == "no_holding":
        return no_holding_text(u)
    if why == "small":
        return f"Too small to switch. Minimum ${min_usd:g}."
    if why == "costs":
        return f"Costs eat the gap on {u} right now. No switch."
    return f"No cheaper version of {u} right now."


def rotate_confirm_text(u: str, sym: str, to: str, qty: float, out: float, buy: float | None,
                        cost: float, gap: float) -> str:
    t = (f"<b>Switch {sym} → {to}</b>\n"
         f"Sell <code>{qty:.6g}</code> {sym} ≈ $<code>{out:,.2f}</code>\n")
    if buy:
        t += f"Buy ≈ <code>{buy:.4g}</code> {to}\n"
    return (t + f"Costs ≈ $<code>{cost:,.2f}</code> · gap <code>{gap * 100:+.1f}%</code>\n"
            f"Same {u} shares, cheaper version. Two swaps via USDT.\n<i>Quote good for 60s</i>")


def _clock(sec: float) -> str:
    m, s = divmod(int(max(sec, 0)), 60)
    return f"{m}:{s:02d}"


def rotate_progress_text(sym: str, to: str, step: int, elapsed: float, est: float) -> str:
    frm, dst = (sym, "USDT") if step == 1 else ("USDT", to)
    return ("⏳ <b>Transaction processing</b>\n"
            f"Step {step}/2: {frm} → {dst}\n"
            f"Time <code>{_clock(elapsed)}</code> · est. <code>~{_clock(est)}</code>\n"
            "<b>DON'T CLOSE THIS MESSAGE</b>")


def pack_confirm_text(label: str, usd: float, legs: list[dict], skipped: list[tuple]) -> str:
    rows = [f"{l['u']:<5} ${l['usd']:>6.2f} ≈ {l['out']:.4g} {l['sym']}" for l in legs]
    rows += [f"{u:<5} skipped: {why}" for u, why in skipped]
    t = f"<b>{label} starter pack · ${usd:g}</b>\n<code>" + "\n".join(rows) + "</code>\n"
    if skipped:
        t += "A skipped leg stays in USDT, never moved to another stock.\n"
    return t + "Buys run in order.\n<i>Quote good for 60s</i>"


def sw_error(e) -> str:
    return (" " + str(e).replace("<", "").replace(">", "")[:100] + ".") if e else ""
