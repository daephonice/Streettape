"""StreetTape bot: Step 13. One-tap versions of Rotate, AI starter pack and Flatten.
Read-only reuse of agent.net_arb_quote, basket.build/_cheapest and the tg_trade helpers. Every path ends in the
Buy/Sell pipeline: baw quote cross-check, confirm screen, 60 s expiry, one tap consumes the id, chat lock held
end to end, one baw swap + poll per leg. Floor, defensive and earnings stand-down stay on the site."""
import time
import logging

from aiogram import Router
from aiogram.types import CallbackQuery

import agent
import balances
import basket
import prices
import rwa
import tg_trade as tt
import tg_ui as ui
import tg_wallet as tw

log = logging.getLogger("tg_adv")

router = Router()
ROTATE_MIN_USD = 5.0        # Ondo RFQ rejects under $5 (code 40375)
_BAW_ERR = (tw.BawError, KeyError, TypeError, ValueError)


async def _bq(chat_id: int, frm: str, to: str, qty: float) -> float:
    """`baw market-order quote` -> out amount."""
    q = await tw.baw(chat_id, "market-order", "quote", "--fromTokenQty", tt._qty(qty), "--fromToken", frm,
                     "--toToken", to, "--binanceChainId", tt.CHAIN)
    return float(q["toCoinAmount"])


def _kb_tx(*rows_tx):
    tx = [ui.link_btn(label, ui.scan_tx(h)) for label, h in rows_tx if h]
    return ui.kb(tx, ui.tail_row())


# ---- rotate ---------------------------------------------------------------

async def _plan(w, u: str):
    """(plan, None) when the held wrapper of `u` can switch to a cheaper one at a positive net, else (None, why)."""
    h = await tt._holding(w, u)
    if not h:
        return None, "no_holding"
    sym, amt, _, mint = h
    g, hw = tt.find(sym)
    cheap = basket._cheapest(g)
    if not (g and hw and cheap) or cheap["symbol"].upper() == sym.upper():
        return None, "none"
    px, ratio = hw.get("tokenPrice"), tt.share_ratio(sym)
    if not px or not hw.get("hasTape") or hw.get("thin"):
        return None, "none"
    held_ps, cheap_ps = px / ratio, cheap["_perShare"]
    if held_ps <= cheap_ps:
        return None, "none"
    qty = tt._floor8(amt)
    usd = qty * px
    if usd < ROTATE_MIN_USD:
        return None, "small"
    gap = held_ps / cheap_ps - 1
    hit = {"underlying": u, "rich": hw, "cheap": cheap, "gap": gap, "normalized": True,
           "richRatio": ratio, "cheapRatio": cheap["tokenToShareRatio"],
           "richSharePrice": held_ps, "cheapSharePrice": cheap_ps, "sizeUsd": usd}
    net = await agent.net_arb_quote(hit, usd)
    if not net["viable"]:
        return None, "costs"
    return {"u": u, "sym": sym, "mint": mint, "qty": qty, "usd": usd, "gap": gap, "net": net,
            "to": cheap["symbol"], "to_mint": cheap["mint"]}, None


async def rotate_btn(chat_id: int, u: str):
    """[Switch to cheaper version] only when a viable switch exists for this chat's wallet, else None."""
    if chat_id <= 0:
        return None
    w = tw.get_wallet(chat_id)
    if not w:
        return None
    try:
        p, _ = await _plan(w, u)
    except Exception:
        log.debug("tg_adv: rotate plan failed", exc_info=True)
        return None
    return ui.cb_btn(ui.ROTATE, f"rot:{u}") if p else None


async def _prepare_rotate(chat_id: int, w, u: str):
    p, why = await _plan(w, u)
    if not p:
        return ui.rotate_none_text(u, why, ROTATE_MIN_USD), ui.cancelled_kb(u)
    try:
        out = await _bq(chat_id, p["mint"], rwa.USDT, p["qty"])
    except tw.SessionExpired:
        raise
    except _BAW_ERR:
        session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
        if rwa.is_ondo(p["mint"]) and not session.get("cashOpen"):
            return ui.not_open_text(), ui.cancelled_kb(u)
        return ui.no_route_text(), ui.finish_kb(p["sym"], 0, u)
    net = p["net"]
    loss_bps = (1 - out / p["usd"]) * 1e4
    if loss_bps >= net["grossBps"]:     # the real sell quote already eats the whole gap
        return ui.moved_text(), ui.cancelled_kb(u)
    buy_ref = net["buyLeg"].get("uiOutAmount")
    buy = buy_ref * out / p["usd"] if buy_ref else None
    cost = max(0.0, p["usd"] - out) + agent._leg_cost_bps(net["buyLeg"]) / 1e4 * out
    pid = tt._store(kind="rot", chat=chat_id, u=u, sym=p["sym"], mint=p["mint"], qty=p["qty"],
                    to=p["to"], to_mint=p["to_mint"], out=out)
    text = ui.rotate_confirm_text(u, p["sym"], p["to"], p["qty"], out, buy, cost, p["gap"])
    return text, ui.adv_confirm_kb("Confirm · Switch", pid)


async def _exec_rotate(cb: CallbackQuery, w, p: dict):
    chat_id = cb.message.chat.id
    await ui.show(cb, ui.working_text("Switching"))
    b = None
    async with tw.lock(chat_id):
        s = await tt._run_swap(chat_id, p["mint"], rwa.USDT, p["qty"], have_lock=True)
        if s["status"] == "FINISHED":
            got = tt._num((s["row"] or {}).get("toTokenActualQty"), p["out"] * 0.99)
            try:
                b = await tt._run_swap(chat_id, rwa.USDT, p["to_mint"], tt._floor8(got), have_lock=True)
            except tw.SessionExpired:
                b = {"status": "FAILED", "row": None, "error": "wallet session ended"}
    balances.invalidate(w.address)
    await ui.show(cb, *_rotate_receipt(p, s, b))


def _rotate_receipt(p: dict, s: dict, b: dict | None):
    tx_s = tt._tx(s["row"])
    if s["status"] == "FAILED":
        return f"Switch failed at the sell. Nothing changed.{ui.sw_error(s.get('error'))}", _kb_tx(("Sell tx", tx_s))
    if s["status"] == "PENDING":
        return "Sell still confirming. No buy was sent. Check Book in a minute.", ui.kb(ui.tail_row())
    got = tt._num((s["row"] or {}).get("toTokenActualQty"), p["out"])
    sold = f"Sold <code>{p['qty']:.6g}</code> {p['sym']} for $<code>{got:,.2f}</code> USDT."
    tx_b = tt._tx((b or {}).get("row"))
    if b["status"] == "FINISHED":
        bought = tt._num(b["row"].get("toTokenActualQty"), 0.0)
        return (f"Switched. {sold}\nBought <code>{bought:.4g}</code> {p['to']}.",
                _kb_tx(("Sell tx", tx_s), ("Buy tx", tx_b)))
    if b["status"] == "PENDING":
        return f"{sold}\nBuy still confirming. Check Book in a minute.", _kb_tx(("Sell tx", tx_s))
    kb = ui.kb([ui.link_btn("Sell tx", ui.scan_tx(tx_s))] if tx_s else [],
               [ui.cb_btn(ui.BUY + f" {p['u']}", f"buy:{p['u']}")], ui.tail_row())
    return f"{sold}\nBuy failed.{ui.sw_error(b.get('error'))} The USDT is in your wallet.", kb


# ---- AI starter pack ---------------------------------------------------------

async def _prepare_pack(chat_id: int, w, theme: str, usd: float):
    b = await basket.build(theme, usd)
    legs, skipped = [], []
    for l in b["legs"]:
        u = l["underlying"]
        if not l.get("filled"):
            skipped.append((u, (l.get("reason") or "no route")[:40]))
            continue
        try:
            out = await _bq(chat_id, rwa.USDT, l["contract"], l["amountUsd"])
        except tw.SessionExpired:
            raise
        except _BAW_ERR:
            skipped.append((u, "no automatic route"))
            continue
        ref = tt._num(l.get("outAmount"), 0.0)
        if ref > 0 and out < ref * (1 - tt.MAX_SLIP_BPS / 1e4):
            skipped.append((u, "price moved"))
            continue
        legs.append({"u": u, "sym": l["symbol"], "mint": l["contract"], "usd": l["amountUsd"], "out": out})
    if not legs:
        return "No leg could be quoted right now.", ui.kb([ui.cb_btn(ui.BOARD, "back:board")])
    need = sum(l["usd"] for l in legs)
    try:
        have = float((await balances.get_balances(w.address)).get("USDT") or 0.0)
    except Exception:
        have = None
    if have is not None and have < need:
        return ui.funds_text(w.address, have, need), ui.kb([ui.cb_btn(ui.BOARD, "back:board")])
    pid = tt._store(kind="pack", chat=chat_id, u=theme.upper(), theme=theme, usd=usd, slot=b["legUsd"], legs=legs, skipped=skipped)
    return (ui.pack_confirm_text(b["label"], usd, legs, skipped),
            ui.adv_confirm_kb(f"Confirm · ${need:.2f}", pid))


async def _exec_pack(cb: CallbackQuery, w, p: dict):
    chat_id = cb.message.chat.id
    await ui.show(cb, ui.working_text("Buying"))
    res = []
    async with tw.lock(chat_id):
        for l in p["legs"]:
            try:
                res.append(await tt._run_swap(chat_id, rwa.USDT, l["mint"], l["usd"], have_lock=True))
            except tw.SessionExpired:
                res.append({"status": "FAILED", "row": None, "error": "wallet session ended"})
                break
    res += [{"status": "FAILED", "row": None, "error": "not run"}] * (len(p["legs"]) - len(res))
    balances.invalidate(w.address)
    lines, txs, left = [], [], 0.0
    for l, r in zip(p["legs"], res):
        if r["status"] == "FINISHED":
            got = tt._num(r["row"].get("toTokenActualQty"), l["out"])
            lines.append(f"✅ {l['u']} <code>{got:.4g}</code> {l['sym']} · ${l['usd']:.2f}")
            tx = tt._tx(r["row"])
            if tx:
                txs.append((f"{l['u']} tx", tx))
        elif r["status"] == "PENDING":
            lines.append(f"⏳ {l['u']} still confirming")
        else:
            lines.append(f"❌ {l['u']} not filled.{ui.sw_error(r.get('error'))}")
            left += l["usd"]
    lines += [f"➖ {u} skipped: {why}" for u, why in p["skipped"]]
    unspent = left + p["slot"] * len(p["skipped"])
    tail = f"\nUnspent ≈ $<code>{unspent:,.2f}</code> stays in USDT." if unspent > 0 else ""
    await ui.show(cb, "<b>Starter pack</b>\n" + "\n".join(lines) + tail, _kb_tx(*txs[:3]))


# ---- flatten ------------------------------------------------------------------

async def flatten_buttons(chat_id: int, cands: list[dict]) -> list:
    """[Sell X to USDT] buttons for the flagged wrappers this chat's wallet actually holds."""
    w = tw.get_wallet(chat_id)
    if not w:
        return []
    try:
        hold = (await balances.get_balances(w.address)).get("holdings") or {}
    except Exception:
        return []
    out = []
    for t in cands:
        sym = t["symbol"].upper()
        if (hold.get(sym) or 0) > 0:
            out.append(ui.cb_btn(f"Sell {t['symbol']} to USDT", f"fl:{sym}"))
    return out[:6]


# ---- callbacks ------------------------------------------------------------------

async def _busy(cb: CallbackQuery) -> bool:
    if tw.lock(cb.message.chat.id).locked():
        await cb.answer("Another action is running.")
        return True
    return False


@router.callback_query(lambda c: c.data and c.data.startswith("rot:"))
@tw.guarded
async def on_rotate(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w or await _busy(cb):
        return
    await cb.answer()
    await ui.show(cb, ui.working_text("Checking prices"))
    await ui.show(cb, *(await _prepare_rotate(cb.message.chat.id, w, cb.data.split(":", 1)[1])))


@router.callback_query(lambda c: c.data and c.data.startswith("pk:"))
@tw.guarded
async def on_pack(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w or await _busy(cb):
        return
    try:
        _, theme, usd = cb.data.split(":")
        usd = float(usd)
        assert theme in basket.THEMES and 1 <= usd <= 1000
    except (ValueError, AssertionError):
        await cb.answer()
        return
    await cb.answer()
    await ui.show(cb, ui.working_text("Checking prices"))
    await ui.show(cb, *(await _prepare_pack(cb.message.chat.id, w, theme, usd)))


@router.callback_query(lambda c: c.data and c.data.startswith("fl:"))
@tw.guarded
async def on_flatten(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w or await _busy(cb):
        return
    sym = cb.data.split(":", 1)[1].upper()
    a = prices.get_asset(sym)
    await cb.answer()
    if not a or a.get("kind") != "stock":
        return
    await ui.show(cb, ui.working_text("Checking prices"))
    await ui.show(cb, *(await tt._prepare_sell(cb.message.chat.id, w, a["underlying"], 100, only=sym)))


@router.callback_query(lambda c: c.data and c.data.startswith("ax:"))
@tw.guarded
async def on_go(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    w = await tt._gate(cb)
    if not w:
        return
    pid = cb.data.split(":", 1)[1]
    p = tt._pending.get(pid)
    if not p or p["chat"] != chat_id or p["kind"] not in ("rot", "pack"):
        await cb.answer("Already handled or expired.")
        return
    if await _busy(cb):
        return
    tt._pending.pop(pid, None)      # one tap consumes the id
    await cb.answer()
    if time.monotonic() - p["at"] > tt.TTL:     # stale: re-quote, never trade on an old price
        await ui.show(cb, ui.working_text("Re-checking prices"))
        if p["kind"] == "rot":
            await ui.show(cb, *(await _prepare_rotate(chat_id, w, p["u"])))
        else:
            await ui.show(cb, *(await _prepare_pack(chat_id, w, p["theme"], p["usd"])))
        return
    await (_exec_rotate if p["kind"] == "rot" else _exec_pack)(cb, w, p)


@router.callback_query(lambda c: c.data and c.data.startswith("an:"))
async def on_cancel(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    p = tt._pending.get(pid)
    await cb.answer()
    if not p or p["chat"] != cb.message.chat.id:
        return
    tt._pending.pop(pid, None)
    kb = ui.kb([ui.cb_btn(ui.BOARD, "back:board")]) if p["kind"] == "pack" else ui.cancelled_kb(p["u"])
    await ui.show(cb, "Cancelled.", kb)
