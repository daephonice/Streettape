"""StreetTape bot: buy and sell (Steps 9-10).
Pick = basket.quick_pick (StreetTape's own rule). `baw` only cross-checks, signs and executes."""
import math
import time
import asyncio
import logging
import secrets

from aiogram import Router
from aiogram.types import CallbackQuery, Message

import balances
import basket
import prices
import rwa
import swap
import tg_ui as ui
import tg_wallet as tw

log = logging.getLogger("tg_trade")

router = Router()
CHAIN = tw.CHAIN
TTL = 60.0              # confirm button life
MAX_SLIP_BPS = 50.0     # baw quote may be at most this far below swap.quote
POLL_EVERY, POLL_MAX = 3, 30
TYPING_TTL = 120.0

_pending: dict[str, dict] = {}
_typing: dict[int, dict] = {}   # chat -> {"u", "mid", "at"}: waiting for a typed dollar amount


# ---- helpers ---------------------------------------------------------------

def _qty(x) -> str:
    return format(float(x), ".18f").rstrip("0").rstrip(".") or "0"


def _floor8(x: float) -> float:
    return math.floor(float(x) * 1e8) / 1e8


def find(symbol: str):
    s = (symbol or "").upper()
    for g in rwa.get_cached_snapshot().get("groups") or []:
        for w in g.get("wrappers") or []:
            if (w.get("symbol") or "").upper() == s:
                return g, w
    return None, None


def share_ratio(symbol: str) -> float:
    """Shares per token. Falls back to 1.0 when the snapshot has no ratio."""
    _, w = find(symbol)
    try:
        return float(w["tokenToShareRatio"]) if w and w.get("tokenToShareRatio") else 1.0
    except (TypeError, ValueError):
        return 1.0


def _gc():
    now = time.monotonic()
    for k in [k for k, p in _pending.items() if now - p["at"] > TTL * 5]:
        _pending.pop(k, None)
    for k in [k for k, t in _typing.items() if now - t["at"] > TYPING_TTL]:
        _typing.pop(k, None)


def _store(**d) -> str:
    _gc()
    pid = secrets.token_urlsafe(4)
    _pending[pid] = {**d, "at": time.monotonic()}
    return pid


def _same(row: dict, frm: str, qty: str) -> bool:
    try:
        return (str(row.get("fromToken", "")).lower() == frm.lower()
                and math.isclose(float(row.get("fromTokenQty")), float(qty), rel_tol=1e-6))
    except (TypeError, ValueError):
        return False


async def _gate(cb: CallbackQuery):
    """Private chat + linked wallet, else answer/redirect. Returns the TgWallet or None."""
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


# ---- baw swap + poll -------------------------------------------------------

async def _poll(chat_id: int, order_id, frm: str, to: str, qty: str, t0_ms: int):
    for _ in range(POLL_MAX):
        await asyncio.sleep(POLL_EVERY)
        try:
            rows = ((await tw.baw(chat_id, "market-order", "list", "--orderId", str(order_id))) or {}).get("list") or []
            row = rows[0] if rows else None
            if row is None:  # orderId from swap is sometimes unknown to list: match by pair + size
                rows = ((await tw.baw(chat_id, "market-order", "list", "--binanceChainId", CHAIN, "--toToken", to,
                                      "--startTime", str(t0_ms), "--pageSize", "5")) or {}).get("list") or []
                row = next((r for r in rows if _same(r, frm, qty)), None)
        except tw.SessionExpired:
            raise
        except tw.BawError:
            continue
        if row and row.get("status") in ("FINISHED", "FAILED"):
            return row
    return None


async def _run_swap(chat_id: int, frm: str, to: str, qty: float) -> dict:
    """One signed swap, polled to a terminal state. Holds the chat lock end to end.
    Returns {"status": FINISHED|FAILED|PENDING, "row": dict|None, "error": str|None}."""
    q = _qty(qty)
    async with tw.lock(chat_id):
        t0 = int((time.time() - 60) * 1000)
        try:
            d = await tw.baw(chat_id, "market-order", "swap", "--fromTokenQty", q, "--fromToken", frm,
                             "--toToken", to, "--binanceChainId", CHAIN, "--slippage", "1")
        except tw.BawError as e:
            return {"status": "FAILED", "row": None, "error": str((e.err or {}).get("message") or e)[:140]}
        oid = (d or {}).get("orderId") if isinstance(d, dict) else None
        if not oid:
            return {"status": "FAILED", "row": None, "error": "swap returned no order id"}
        row = await _poll(chat_id, oid, frm, to, q, t0)
    if row is None:
        return {"status": "PENDING", "row": None, "error": None}
    return {"status": row["status"], "row": row, "error": None}


def _tx(row) -> str | None:
    for k in ("txHash", "hash", "txHashes"):
        v = (row or {}).get(k)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            return str(v)
    return None


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---- buy -------------------------------------------------------------------

async def _prepare_buy(chat_id: int, wallet, u: str, usd: float):
    """Returns (text, markup). Stores a pending confirm when everything passes."""
    r = await basket.quick_pick(u, usd)
    if not r.get("ok"):
        return (r.get("reason") or "Not available right now."), ui.refuse_kb(u, usd)
    sym, mint = r["symbol"], r["contract"]
    try:
        q = await tw.baw(chat_id, "market-order", "quote", "--fromTokenQty", _qty(usd), "--fromToken", rwa.USDT,
                         "--toToken", mint, "--binanceChainId", CHAIN)
        out = float(q["toCoinAmount"])
    except tw.SessionExpired:
        raise
    except (tw.BawError, KeyError, TypeError, ValueError):
        return ui.no_route_text(), ui.finish_kb(sym, usd, u)
    ref = _num(r.get("outAmount"), 0.0)
    if ref > 0 and out < ref * (1 - MAX_SLIP_BPS / 1e4):
        return ui.moved_text(), ui.refuse_kb(u, usd)
    try:
        have = float((await balances.get_balances(wallet.address)).get("USDT") or 0.0)
    except Exception:
        have = None
    if have is not None and have < usd:
        return ui.funds_text(wallet.address, have, usd), ui.kb([ui.cb_btn(ui.BACK, f"tok:{u}")])
    pid = _store(kind="buy", chat=chat_id, u=u, sym=sym, mint=mint, usd=usd, out=out)
    n = len(r.get("candidates") or []) or 1
    return ui.buy_confirm_text(u, usd, out, sym, n), ui.confirm_kb(ui.confirm_label(usd), pid, u)


async def _holding(wallet, u: str):
    """The held wrapper of `u` with the most shares: (sym, amount, shares, mint) or None."""
    bal = (await balances.get_balances(wallet.address)).get("holdings") or {}
    best = None
    for sym, amt in bal.items():
        a = prices.get_asset(sym)
        if not a or a.get("kind") != "stock" or a.get("underlying") != u or not amt or amt <= 0:
            continue
        sh = amt * share_ratio(sym)
        if best is None or sh > best[2]:
            best = (sym, amt, sh, a["mint"])
    return best


async def _prepare_sell(chat_id: int, wallet, u: str, pct: int):
    h = await _holding(wallet, u)
    if not h:
        return ui.no_holding_text(u), ui.cancelled_kb(u)
    sym, amt, _, mint = h
    qty = _floor8(amt if pct >= 100 else amt * pct / 100)
    if qty <= 0:
        return ui.no_holding_text(u), ui.cancelled_kb(u)
    try:
        q = await tw.baw(chat_id, "market-order", "quote", "--fromTokenQty", _qty(qty), "--fromToken", mint,
                         "--toToken", rwa.USDT, "--binanceChainId", CHAIN)
        out = float(q["toCoinAmount"])
    except tw.SessionExpired:
        raise
    except (tw.BawError, KeyError, TypeError, ValueError):
        session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
        if rwa.is_ondo(mint) and not session.get("cashOpen"):
            return ui.not_open_text(), ui.cancelled_kb(u)
        return ui.no_route_text(), ui.finish_kb(sym, 0, u)
    sc = swap.share_check(mint, rwa.USDT, qty, {"uiOutAmount": out})
    warn = None
    if sc and sc.get("warn"):
        warn = f"Heads up: {sc.get('reason') or 'output looks low vs the tape'}."
    pid = _store(kind="sell", chat=chat_id, u=u, sym=sym, mint=mint, qty=qty, out=out, pct=pct)
    return (ui.sell_confirm_text(u, pct, qty, sym, out, warn),
            ui.confirm_kb(ui.sell_label(out, bool(warn)), pid, u))


def _receipt(p: dict, res: dict):
    row, st = res["row"], res["status"]
    if st == "FAILED":
        why = f" {res['error']}." if res.get("error") else ""
        return f"Swap failed. Nothing filled.{why}", ui.receipt_kb(_tx(row))
    if st == "PENDING":
        return "Still confirming. Check Book in a minute.", ui.receipt_kb(None)
    got = _num((row or {}).get("toTokenActualQty"), p["out"])
    if p["kind"] == "buy":
        sh = got * share_ratio(p["sym"])
        return (f"Filled. <code>{got:.4g}</code> {p['sym']} for ${p['usd']:.2f}\n"
                f"≈ <code>{sh:.4g}</code> {p['u']} shares"), ui.receipt_kb(_tx(row))
    return f"Sold. <code>{p['qty']:.6g}</code> {p['sym']} for $<code>{got:,.2f}</code> USDT", ui.receipt_kb(_tx(row))


async def _execute(cb: CallbackQuery, wallet, p: dict):
    chat_id = cb.message.chat.id
    await ui.show(cb, ui.working_text("Buying" if p["kind"] == "buy" else "Selling"))
    if p["kind"] == "buy":
        res = await _run_swap(chat_id, rwa.USDT, p["mint"], p["usd"])
    else:
        res = await _run_swap(chat_id, p["mint"], rwa.USDT, p["qty"])
    balances.invalidate(wallet.address)
    text, markup = _receipt(p, res)
    await ui.show(cb, text, markup)


# ---- callbacks -------------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith("buy:"))
async def on_buy_open(cb: CallbackQuery):
    if not await _gate(cb):
        return
    u = cb.data.split(":", 1)[1]
    await cb.answer()
    await ui.show(cb, ui.amount_text(u), ui.amount_kb(u))


@router.callback_query(lambda c: c.data and c.data.startswith("ba:"))
@tw.guarded
async def on_buy_amount(cb: CallbackQuery):
    w = await _gate(cb)
    if not w:
        return
    _, u, usd = cb.data.split(":")
    if tw.lock(cb.message.chat.id).locked():
        await cb.answer("Another action is running.")
        return
    await cb.answer()
    await ui.show(cb, ui.working_text("Checking prices"))
    text, markup = await _prepare_buy(cb.message.chat.id, w, u, float(usd))
    await ui.show(cb, text, markup)


@router.callback_query(lambda c: c.data and c.data.startswith("bc:"))
async def on_buy_custom(cb: CallbackQuery):
    if not await _gate(cb):
        return
    u = cb.data.split(":", 1)[1]
    _gc()
    _typing[cb.message.chat.id] = {"u": u, "mid": cb.message.message_id, "at": time.monotonic()}
    ui.inputs[cb.message.chat.id] = "usd"
    await cb.answer()
    await ui.show(cb, ui.custom_text(), ui.kb([ui.cb_btn(ui.BACK, f"buy:{u}")]))


def _typing_active(m: Message) -> bool:
    t = _typing.get(m.chat.id)
    return bool(t and m.text and not m.text.startswith("/") and ui.inputs.get(m.chat.id) == "usd"
                and time.monotonic() - t["at"] <= TYPING_TTL)


@router.message(_typing_active)
@tw.guarded
async def on_buy_typed(message: Message):
    chat_id = message.chat.id
    t = _typing.pop(chat_id, None)
    ui.clear_input(chat_id)
    w = tw.get_wallet(chat_id)
    if not t or not w:
        return
    try:
        usd = float(message.text.strip().lstrip("$").replace(",", ""))
    except ValueError:
        usd = 0.0
    if not 1 <= usd <= 1000:
        await ui.say(message.bot, chat_id, "Enter a number from 1 to 1000. Tap Custom to try again.",
                     ui.kb([ui.cb_btn(ui.BACK, f"buy:{t['u']}")]))
        return
    await ui.edit(message.bot, chat_id, t["mid"], ui.working_text("Checking prices"))
    text, markup = await _prepare_buy(chat_id, w, t["u"], usd)
    await ui.edit(message.bot, chat_id, t["mid"], text, markup)


@router.callback_query(lambda c: c.data and c.data.startswith("sell:"))
@tw.guarded
async def on_sell_open(cb: CallbackQuery):
    w = await _gate(cb)
    if not w:
        return
    u = cb.data.split(":", 1)[1]
    await cb.answer()
    h = await _holding(w, u)
    if not h:
        await ui.show(cb, ui.no_holding_text(u), ui.cancelled_kb(u))
        return
    await ui.show(cb, ui.sell_amount_text(u, h[0], h[2]), ui.sell_amount_kb(u))


@router.callback_query(lambda c: c.data and c.data.startswith("sp:"))
@tw.guarded
async def on_sell_pct(cb: CallbackQuery):
    w = await _gate(cb)
    if not w:
        return
    _, u, pct = cb.data.split(":")
    if tw.lock(cb.message.chat.id).locked():
        await cb.answer("Another action is running.")
        return
    await cb.answer()
    await ui.show(cb, ui.working_text("Checking prices"))
    text, markup = await _prepare_sell(cb.message.chat.id, w, u, int(pct))
    await ui.show(cb, text, markup)


@router.callback_query(lambda c: c.data and c.data.startswith("go:"))
@tw.guarded
async def on_go(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    w = await _gate(cb)
    if not w:
        return
    pid = cb.data.split(":", 1)[1]
    p = _pending.get(pid)
    if not p or p["chat"] != chat_id:
        await cb.answer("Already handled or expired.")
        return
    if tw.lock(chat_id).locked():
        await cb.answer("Another action is running.")
        return
    _pending.pop(pid, None)  # one tap consumes the id: a double tap finds nothing
    await cb.answer()
    if time.monotonic() - p["at"] > TTL:  # stale: re-quote, never trade on an old price
        await ui.show(cb, ui.working_text("Re-checking prices"))
        if p["kind"] == "buy":
            text, markup = await _prepare_buy(chat_id, w, p["u"], p["usd"])
        else:
            text, markup = await _prepare_sell(chat_id, w, p["u"], p["pct"])
        await ui.show(cb, text, markup)
        return
    await _execute(cb, w, p)


@router.callback_query(lambda c: c.data and c.data.startswith("no:"))
async def on_cancel(cb: CallbackQuery):
    p = _pending.get(cb.data.split(":", 1)[1])
    if not p or p["chat"] != cb.message.chat.id:
        await cb.answer()
        return
    _pending.pop(cb.data.split(":", 1)[1], None)
    await cb.answer()
    await ui.show(cb, "Cancelled.", ui.cancelled_kb(p["u"]))


@router.callback_query(lambda c: c.data and c.data.startswith("chk:"))
async def on_check(cb: CallbackQuery):
    _, u, usd = cb.data.split(":")
    usd = float(usd)
    await cb.answer()
    await ui.show(cb, ui.working_text("Checking prices"))
    r = await basket.quick_pick(u, usd)
    lines = basket.pick_lines(r)
    if r.get("mark"):
        lines.insert(0, f"{u} cash print ${r['mark']:,.2f}, band +/-{r['bandBps']:.0f} bps, ${usd:g}")
    if not r.get("ok"):
        lines.append(r.get("reason") or "Not available right now")
    await ui.show(cb, "<b>Price check</b>\n<code>" + "\n".join(lines or ["No data yet"]) + "</code>",
                  ui.kb([ui.cb_btn(ui.BACK, f"tok:{u}"), ui.cb_btn(ui.BOARD, "back:board")]))
