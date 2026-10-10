"""StreetTape bot: Send flow (Step 11). Each screen answers one question.
Transfer command is NOT assumed: set BAW_SEND_CMD (e.g. "wallet transfer") after reading `baw wallet --help`.
Unset = Send shows an Open-StreetTape button instead. Never faked."""
import os
import time
import shlex
import asyncio
import logging
import secrets
from decimal import Decimal, ROUND_DOWN

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

import balances
import prices
import rpc
import tg_ui as ui
import tg_wallet as tw
import tg_trade
from database import SessionLocal
from models import TgRecipient, utcnow

log = logging.getLogger("tg_send")

router = Router()

DAILY_USD = float(os.getenv("TG_SEND_DAILY_USD", "200"))
GAS_RESERVE = float(os.getenv("TG_BNB_GAS_RESERVE", "0.002"))
SEND_CMD = shlex.split(os.getenv("BAW_SEND_CMD", ""))
SEND_FLAGS = os.getenv("BAW_SEND_FLAGS", "--toAddress {to} --token {token} --amount {amount} --binanceChainId {chain}")
TTL = 60.0
STATE_TTL = 300.0
RATE_SECONDS = 10.0
ZERO = "0x" + "0" * 40

_st: dict[int, dict] = {}        # chat -> {"step": to|amt, "sym", "to", "recent", "bal", "mid", "at"}
_pend: dict[str, dict] = {}      # id -> confirm payload
_sent: dict[int, list] = {}      # chat -> [(ts, usd)] rolling 24h (in-memory: resets on redeploy)
_last_exec: dict[int, float] = {}


# ---- helpers ---------------------------------------------------------------

def _dec(x: float) -> str:
    return format(Decimal(str(x)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN), "f").rstrip("0").rstrip(".") or "0"


def _usd(sym: str, qty: float):
    if sym in ("USDT", "USDC"):
        return qty
    p = ((prices.get_prices() or {}).get("prices") or {}).get(sym)
    return qty * p["price"] if p and p.get("price") is not None else None


def _spent_24h(chat_id: int) -> float:
    cut = time.time() - 86400
    _sent[chat_id] = [(t, v) for t, v in _sent.get(chat_id, []) if t > cut]
    return sum(v for _, v in _sent[chat_id])


def _recent(chat_id: int) -> list[str]:
    db = SessionLocal()
    try:
        return list(db.execute(select(TgRecipient.address).where(TgRecipient.chat_id == chat_id)
                               .order_by(TgRecipient.created_at.desc()).limit(3)).scalars().all())
    finally:
        db.close()


def _known(chat_id: int, addr: str) -> bool:
    db = SessionLocal()
    try:
        return db.execute(select(TgRecipient.id).where(TgRecipient.chat_id == chat_id,
                                                       TgRecipient.address == addr.lower())).first() is not None
    finally:
        db.close()


def _remember(chat_id: int, addr: str):
    db = SessionLocal()
    try:
        r = db.execute(select(TgRecipient).where(TgRecipient.chat_id == chat_id,
                                                 TgRecipient.address == addr.lower())).scalar_one_or_none()
        if r:
            r.created_at = utcnow()
        else:
            db.add(TgRecipient(chat_id=chat_id, address=addr.lower()))
        db.commit()
    finally:
        db.close()


def _avail(sym: str, bal: float) -> float:
    return max(0.0, bal - GAS_RESERVE) if sym == "BNB" else bal


def _pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return str(d[k])
    return None


async def _gate(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if chat_id <= 0:
        await cb.answer(ui.private_only_text(), show_alert=True)
        return None
    w = tw.get_wallet(chat_id)
    if not w:
        await cb.answer()
        await ui.show(cb, ui.link_intro_text(), ui.link_intro_kb())
        return None
    return w


def _active(chat_id: int, *steps) -> bool:
    s = _st.get(chat_id)
    return bool(s and s["step"] in steps and ui.inputs.get(chat_id) == "send"
                and time.monotonic() - s["at"] <= STATE_TTL)


def _back_kb(data: str = "snd"):
    return ui.kb([ui.cb_btn(ui.BACK, data), ui.cb_btn(ui.BOARD, "back:board")])


# ---- copy ------------------------------------------------------------------

def _fallback():
    return ("Sending needs a browser wallet for now. Open StreetTape and send from there.",
            ui.kb([ui.link_btn("Open StreetTape", ui.site("/wallet"))], [ui.cb_btn(ui.BOARD, "back:board")]))


def _pick_asset_screen(holdings: dict):
    rows, btns = [], []
    for sym in ("BNB", "USDT"):
        btns.append((sym, holdings.get(sym, 0.0)))
    for sym, amt in sorted(holdings.items()):
        a = prices.get_asset(sym)
        if sym not in ("BNB", "USDT") and a and amt and amt > 1e-9:
            btns.append((sym, amt))
    b = [ui.cb_btn(f"{s} {a:.4g}", f"sna:{s}") for s, a in btns]
    rows = [b[i:i + 2] for i in range(0, len(b), 2)]
    rows.append([ui.cb_btn(ui.BOARD, "back:board")])
    return "<b>Send</b>\nWhich asset?", ui.kb(*rows)


def _to_screen(chat_id: int, sym: str, recent: list[str]):
    rows = [[ui.cb_btn(ui.short_addr(a), f"snr:{i}")] for i, a in enumerate(recent)]
    rows.append([ui.cb_btn(ui.BACK, "snd")])
    return f"<b>Send {sym}</b>\nPaste the BSC address.", ui.kb(*rows)


def _amt_screen(sym: str, bal: float):
    return (f"<b>Send {sym}</b>\nHow much? You have <code>{_dec(bal)}</code>.",
            ui.kb([ui.cb_btn("25%", "snm:25"), ui.cb_btn("50%", "snm:50"), ui.cb_btn("Max", "snm:100")],
                  [ui.cb_btn(ui.BACK, f"sna:{sym}")]))


def _confirm_screen(p: dict, pid: str, second: bool = False):
    usd = _usd(p["sym"], p["amount"])
    worth = f" (≈ ${usd:,.2f})" if usd is not None else ""
    if second:
        return (f"<b>New address</b>\nCheck every character:\n<code>{p['to']}</code>\n"
                f"{_dec(p['amount'])} {p['sym']}{worth}\n<i>Sends can't be undone.</i>",
                ui.kb([ui.cb_btn("Yes, this address is right", f"sny:{pid}")], [ui.cb_btn("Cancel", f"snx:{pid}")]))
    return (f"<b>Send</b>\n<code>{_dec(p['amount'])}</code> {p['sym']}{worth}\n"
            f"To <code>{ui.short_addr(p['to'])}</code>\n<i>Sends can't be undone.</i>",
            ui.kb([ui.cb_btn("Send", f"snk:{pid}"), ui.cb_btn("Cancel", f"snx:{pid}")]))


# ---- flow ------------------------------------------------------------------

async def start(bot, chat_id: int, w, cb=None, sym: str | None = None):
    """Entry from callback (cb) or /send. Shows asset pick, or the address step when `sym` is preset."""
    async def out(text, markup):
        if cb:
            await ui.show(cb, text, markup)
        else:
            m = await ui.say(bot, chat_id, text, markup)
            ui.hold(chat_id, m.message_id)
    _st.pop(chat_id, None)
    ui.clear_input(chat_id)
    if not SEND_CMD:
        await out(*_fallback())
        return
    bal = (await balances.get_balances(w.address)).get("holdings") or {}
    if sym:
        await _ask_to(out, chat_id, sym, bal, cb.message.message_id if cb else None)
        return
    await out(*_pick_asset_screen(bal))


async def _ask_to(out, chat_id: int, sym: str, holdings: dict, mid):
    recent = _recent(chat_id)
    _st[chat_id] = {"step": "to", "sym": sym, "recent": recent, "bal": holdings.get(sym, 0.0), "mid": mid,
                    "at": time.monotonic()}
    ui.inputs[chat_id] = "send"
    await out(*_to_screen(chat_id, sym, recent))


async def _ask_amount(out, chat_id: int, to: str):
    s = _st[chat_id]
    s.update(step="amt", to=to, at=time.monotonic())
    ui.inputs[chat_id] = "send"
    await out(*_amt_screen(s["sym"], s["bal"]))


async def _make_confirm(out, chat_id: int, w, amount: float):
    s = _st.get(chat_id)
    if not s:
        return
    sym, avail = s["sym"], _avail(s["sym"], s["bal"])
    amount = float(_dec(min(amount, avail)))
    if amount <= 0:
        await out("Nothing to send after the gas reserve.", _back_kb(f"sna:{sym}"))
        return
    usd = _usd(sym, amount)
    if usd is None:
        await out(f"No price for {sym} right now. Try again in a minute.", _back_kb(f"sna:{sym}"))
        return
    spent = _spent_24h(chat_id)
    if spent + usd > DAILY_USD:
        await out(f"Daily send limit ${DAILY_USD:,.0f} reached. Left today: ${max(0, DAILY_USD - spent):,.2f}.",
                  ui.kb([ui.cb_btn(ui.BOARD, "back:board")]))
        return
    p = {"chat": chat_id, "sym": sym, "to": s["to"], "amount": amount, "usd": usd, "at": time.monotonic(),
         "new": not _known(chat_id, s["to"]), "ack": False}
    pid = secrets.token_urlsafe(4)
    _pend[pid] = p
    _st.pop(chat_id, None)
    ui.clear_input(chat_id)
    await out(*_confirm_screen(p, pid))


async def _execute(cb: CallbackQuery, w, pid: str, p: dict):
    chat_id = cb.message.chat.id
    now = time.monotonic()
    if now - _last_exec.get(chat_id, 0) < RATE_SECONDS:
        await cb.answer("Wait a few seconds.")
        return
    _last_exec[chat_id] = now
    _pend.pop(pid, None)  # consumed: a double tap finds nothing
    await cb.answer()
    await ui.show(cb, ui.working_text("Sending"))
    asset = prices.get_asset(p["sym"]) or {}
    flags = shlex.split(SEND_FLAGS.format(to=p["to"], token=asset.get("mint", ""), symbol=p["sym"],
                                          amount=_dec(p["amount"]), chain=tw.CHAIN))
    try:
        async with tw.lock(chat_id):
            data = await tw.baw(chat_id, *SEND_CMD, *flags, timeout=120)
    except tw.BawError as e:
        msg = str((e.err or {}).get("message") or e)[:140]
        await ui.show(cb, f"Send failed. Nothing moved.\n<code>{msg}</code>", ui.kb(ui.tail_row()))
        return
    tx = _pick(data, "txHash", "hash", "transactionHash")
    ok = None
    if tx:
        for _ in range(tg_trade.POLL_MAX):
            await asyncio.sleep(tg_trade.POLL_EVERY)
            try:
                rc = await rpc.call("eth_getTransactionReceipt", [tx])
            except Exception:
                continue
            if rc:
                ok = rc.get("status") == "0x1"
                break
    balances.invalidate(w.address)
    if ok is False:
        await ui.show(cb, "Send failed on chain.", ui.receipt_kb(tx))
        return
    _remember(chat_id, p["to"])
    _sent.setdefault(chat_id, []).append((time.time(), p["usd"]))
    head = "Sent." if ok else "Submitted. Confirming."
    await ui.show(cb, f"{head} <code>{_dec(p['amount'])}</code> {p['sym']} to <code>{ui.short_addr(p['to'])}</code>",
                  ui.receipt_kb(tx))


# ---- callbacks / commands --------------------------------------------------

@router.callback_query(lambda c: c.data == "snd" or (c.data or "").startswith("snd:"))
@tw.guarded
async def on_send(cb: CallbackQuery):
    w = await _gate(cb)
    if not w:
        return
    await cb.answer()
    sym = cb.data.split(":", 1)[1] if ":" in cb.data else None
    await start(cb.bot, cb.message.chat.id, w, cb=cb, sym=sym)


@router.message(Command("send"))
@tw.guarded
async def on_send_cmd(message: Message):
    if message.chat.id <= 0:
        await ui.say(message.bot, message.chat.id, ui.private_only_text())
        return
    w = tw.get_wallet(message.chat.id)
    if not w:
        await ui.say(message.bot, message.chat.id, ui.link_intro_text(), ui.link_intro_kb())
        return
    await start(message.bot, message.chat.id, w)


@router.callback_query(lambda c: c.data and c.data.startswith("sna:"))
@tw.guarded
async def on_asset(cb: CallbackQuery):
    w = await _gate(cb)
    if not w:
        return
    await cb.answer()
    sym = cb.data.split(":", 1)[1]
    bal = (await balances.get_balances(w.address)).get("holdings") or {}

    async def out(t, m):
        await ui.show(cb, t, m)
    await _ask_to(out, cb.message.chat.id, sym, bal, cb.message.message_id)


async def _got_address(bot, chat_id: int, w, to: str, mid, cb=None):
    async def out(t, m):
        if cb:
            await ui.show(cb, t, m)
        else:
            await ui.edit(bot, chat_id, mid, t, m)
    await _ask_amount(out, chat_id, to)


@router.callback_query(lambda c: c.data and c.data.startswith("snr:"))
@tw.guarded
async def on_recipient(cb: CallbackQuery):
    w = await _gate(cb)
    s = _st.get(cb.message.chat.id)
    if not w or not s or s["step"] != "to":
        await cb.answer()
        return
    await cb.answer()
    try:
        to = s["recent"][int(cb.data.split(":", 1)[1])]
    except (ValueError, IndexError):
        return
    await _got_address(cb.bot, cb.message.chat.id, w, to, cb.message.message_id, cb=cb)


@router.message(lambda m: bool(m.text) and not m.text.startswith("/") and _active(m.chat.id, "to"))
@tw.guarded
async def on_address_typed(message: Message):
    chat_id = message.chat.id
    w = tw.get_wallet(chat_id)
    s = _st.get(chat_id)
    if not w or not s:
        return
    to = message.text.strip()
    if not balances.valid_address(to):
        await ui.say(message.bot, chat_id, "Not a valid BSC address. Paste it again.")
        return
    if to.lower() == w.address.lower():
        await ui.say(message.bot, chat_id, "That is your own wallet. Paste another address.")
        return
    if to.lower() == ZERO:
        await ui.say(message.bot, chat_id, "That is the zero address. Paste another address.")
        return
    await _got_address(message.bot, chat_id, w, to.lower(), s.get("mid"))


@router.callback_query(lambda c: c.data and c.data.startswith("snm:"))
@tw.guarded
async def on_pct(cb: CallbackQuery):
    w = await _gate(cb)
    chat_id = cb.message.chat.id
    s = _st.get(chat_id)
    if not w or not s or s["step"] != "amt":
        await cb.answer()
        return
    await cb.answer()
    pct = int(cb.data.split(":", 1)[1])
    avail = _avail(s["sym"], s["bal"])
    amount = avail if pct >= 100 else min(s["bal"] * pct / 100, avail)

    async def out(t, m):
        await ui.show(cb, t, m)
    await _make_confirm(out, chat_id, w, amount)


@router.message(lambda m: bool(m.text) and not m.text.startswith("/") and _active(m.chat.id, "amt"))
@tw.guarded
async def on_amount_typed(message: Message):
    chat_id = message.chat.id
    w = tw.get_wallet(chat_id)
    s = _st.get(chat_id)
    if not w or not s:
        return
    try:
        amount = float(message.text.strip().replace(",", ""))
    except ValueError:
        amount = 0.0
    avail = _avail(s["sym"], s["bal"])
    if amount <= 0 or amount > avail:
        await ui.say(message.bot, chat_id, f"Enter an amount up to {_dec(avail)}.")
        return

    async def out(t, m):
        await ui.edit(message.bot, chat_id, s.get("mid") or message.message_id, t, m)
    await _make_confirm(out, chat_id, w, amount)


def _live(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    p = _pend.get(pid)
    if not p or p["chat"] != cb.message.chat.id:
        return pid, None
    if time.monotonic() - p["at"] > TTL:
        _pend.pop(pid, None)
        return pid, None
    return pid, p


@router.callback_query(lambda c: c.data and c.data.startswith("snk:"))
@tw.guarded
async def on_confirm(cb: CallbackQuery):
    w = await _gate(cb)
    if not w:
        return
    pid, p = _live(cb)
    if not p:
        await cb.answer("Expired. Start again.", show_alert=True)
        await ui.show(cb, "That send expired. Nothing moved.", ui.kb([ui.cb_btn(ui.SEND, "snd"), ui.cb_btn(ui.BOARD, "back:board")]))
        return
    if p["new"]:  # first time to this address: one more look
        p["ack"], p["at"] = True, time.monotonic()
        await cb.answer()
        await ui.show(cb, *_confirm_screen(p, pid, second=True))
        return
    await _execute(cb, w, pid, p)


@router.callback_query(lambda c: c.data and c.data.startswith("sny:"))
@tw.guarded
async def on_confirm_new(cb: CallbackQuery):
    w = await _gate(cb)
    if not w:
        return
    pid, p = _live(cb)
    if not p or not p.get("ack"):
        await cb.answer("Expired. Start again.", show_alert=True)
        return
    await _execute(cb, w, pid, p)


@router.callback_query(lambda c: c.data and c.data.startswith("snx:"))
async def on_cancel(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    p = _pend.get(pid)
    if p and p["chat"] == cb.message.chat.id:
        _pend.pop(pid, None)
    _st.pop(cb.message.chat.id, None)
    ui.clear_input(cb.message.chat.id)
    await cb.answer()
    await ui.show(cb, "Cancelled.", ui.kb([ui.cb_btn(ui.BOARD, "back:board"), ui.cb_btn(ui.BOOK, "book")]))
