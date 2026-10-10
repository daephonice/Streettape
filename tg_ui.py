"""StreetTape bot: all copy, keyboards and the no-links send guard (pure helpers).
No URL ever goes in message text. Links are inline URL buttons only."""
import os
import re
import logging

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
              [cb_btn(BOARD, "back:board"), cb_btn(BOOK, "book")])


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
