"""StreetTape bot: Swap, and the shared two-swap executor.

Binance (baw) cannot do BNB <-> Ondo or wrapper <-> wrapper in one swap, so those run X -> USDT -> Y.
Leg 2 starts by itself with the USDT actually received from leg 1 (same engine as Rotate).
Pairs baw accepts directly (stable <-> stable, stable/BNB <-> wrapper) stay one swap with the baw quote.
xStocks never appear: no baw liquidity. Wrapper list = the site's swap list (prices assets), priced only.
Flow: Swap -> pick from -> pick receive -> amount (rotate-style buttons) -> quote -> confirm.
"""
import time
import asyncio
import logging
import secrets

from aiogram import Router
from aiogram.types import CallbackQuery, Message

import balances
import prices
import routing_rules
import rwa
import swap
import tg_adv
import tg_trade as tt
import tg_ui as ui
import tg_wallet as tw

log = logging.getLogger("tg_swap")
router = Router()

COINS = ("USDT", "USDC", "BNB")
COIN_MINT = {"BNB": rwa.NATIVE, "USDC": rwa.USDC, "USDT": rwa.USDT}
BNB_RESERVE = 0.0002            # kept back for gas, same as the desk
TWO_MIN = routing_rules.BOT_TWO_LEG_MIN_USD
PCTS = (25, 50, 75, 100)
TTL = 60.0                      # quote life
FLOW_TTL = 1800.0
BLOCK_RATIO = swap.IMPACT_FLOOR     # output worth under 95% of the input: blocked
WARN_RATIO = 0.98

_sw: dict[int, dict] = {}       # chat -> {"from","to","amt","kind","custom","wait","mid","at"}
_pend: dict[str, dict] = {}     # pid -> quoted swap waiting for Confirm


def _td():
    import tg_desk              # lazy: tg_desk imports this module
    return tg_desk


def _gc():
    now = time.monotonic()
    for k in [k for k, f in _sw.items() if now - f["at"] > FLOW_TTL]:
        _sw.pop(k, None)
    for k in [k for k, p in _pend.items() if now - p["at"] > TTL * 5]:
        _pend.pop(k, None)


def _new(chat_id: int, mid: int) -> dict:
    _gc()
    fl = {"from": None, "to": None, "amt": None, "kind": None, "custom": False, "wait": False,
          "mid": mid, "at": time.monotonic()}
    _sw[chat_id] = fl
    return fl


def _fl(cb: CallbackQuery):
    fl = _sw.get(cb.message.chat.id)
    if fl:
        fl["at"] = time.monotonic()
        fl["mid"] = cb.message.message_id
    return fl


async def _expired(cb: CallbackQuery):
    await cb.answer("That screen expired. Start again.", show_alert=True)
    await ui.show(cb, *(await _td().home(cb.message.chat.id)))


# ---- token list ------------------------------------------------------------

def _tokens() -> dict[str, dict]:
    """Coins first, then Ondo and bStocks wrappers that have a live price (the site swap list). No xStocks."""
    out = {c: {"sym": c, "mint": COIN_MINT[c], "kind": "coin"} for c in COINS}
    px = (prices.get_prices() or {}).get("prices") or {}
    rows = []
    for sym, a in (prices.get_assets().get("assets") or {}).items():
        if (a.get("kind") != "stock" or a.get("platform") not in routing_rules.BOT_ROTATE_FAMILIES
                or not a.get("mint") or sym not in px):
            continue
        rows.append((a.get("underlying") or "", a["platform"], sym, a))
    for u, plat, sym, a in sorted(rows):
        out[sym] = {"sym": sym, "mint": a["mint"], "kind": "wrap", "plat": plat, "u": u}
    return out


def _px(sym: str):
    return _td()._coin_px(sym)


def _rows(btns: list, n: int = 3) -> list:
    return [btns[i:i + n] for i in range(0, len(btns), n)]


# ---- screens ---------------------------------------------------------------

async def _from_view(chat_id: int, w):
    td = _td()
    hold = await td._hold(w)
    lines, btns = [], []
    for sym in _tokens():
        amt = float(hold.get(sym) or 0.0)
        px = _px(sym)
        val = amt * px if px else None
        if not amt or (val is not None and val < 1):
            continue
        lines.append(f"{sym:<8}{td._tok(amt):>12} " + (f"≈ {td._usd(val)}" if val else ""))
        btns.append(ui.cb_btn(sym, f"sw1:{sym}"))
    if not btns:
        return "🔄 <b>Swap</b>\nNothing to swap yet. Fund your wallet first.", ui.kb([ui.cb_btn(ui.BOARD, "hm")])
    text = "🔄 <b>Swap</b>\nPick the coin to swap from.\n\n<b>You hold</b>\n<code>" + "\n".join(lines) + "</code>"
    return text, ui.kb(*_rows(btns), [ui.cb_btn(ui.BOARD, "hm")])


def _to_view(fl: dict):
    toks = _tokens()
    btns = [ui.cb_btn(sym, f"sw2:{sym}") for sym in toks if sym != fl["from"]]
    text = (f"🔄 <b>Swap {fl['from']} → ?</b>\nPick the coin to receive.\n"
            f"<i>BNB ↔ Ondo and wrapper ↔ wrapper run as two swaps via USDT, minimum ${TWO_MIN:g}.</i>")
    return text, ui.kb(*_rows(btns), [ui.cb_btn(ui.BACK, "swb")])


def _min_note(a: dict, b: dict) -> str:
    if routing_rules.needs_two_leg(a["mint"], b["mint"]):
        return f"Two swaps via USDT · minimum ${TWO_MIN:g}"
    fams = {routing_rules.family(a["mint"]), routing_rules.family(b["mint"])}
    if "ondo" in fams or fams == {"stable"}:
        return f"One swap · minimum ${routing_rules.MIN_USD:g}"
    return "One swap · no minimum"


def _amt_label(fl: dict) -> str:
    v, k = fl["amt"], fl["kind"]
    return {"usd": f"${v:g}", "pct": f"{v:g}%", "tok": f"{v:g}"}[k] + " ✏️"


async def _amount_view(chat_id: int, w):
    td = _td()
    fl = _sw[chat_id]
    toks = _tokens()
    a, b = toks.get(fl["from"]), toks.get(fl["to"])
    if not a or not b:
        return "Not available right now.", ui.kb([ui.cb_btn(ui.BACK, "swb")])
    hold = await td._hold(w)
    held = float(hold.get(a["sym"]) or 0.0)
    px = _px(a["sym"])
    text = (f"🔄 <b>Swap {a['sym']} → {b['sym']}</b>\n{_min_note(a, b)}\n\n<b>You hold</b>\n"
            f"<code>{td._tok(held)} {a['sym']}" + (f" ≈ {td._usd(held * px)}" if px else "") + "</code>\n\nHow much?")
    amts = [ui.cb_btn(("✅ " if (not fl["custom"] and fl["amt"] == n) else "") + ("Max" if n == 100 else f"{n}%"), f"swa:{n}")
            for n in PCTS]
    custom = ui.cb_btn(_amt_label(fl) if fl["custom"] else "✏️ Custom", "swx")
    return text, ui.kb(amts[:3], amts[3:] + [custom], [ui.cb_btn("Continue", "swg"), ui.cb_btn(ui.BACK, "swb")])


def _qty(fl: dict, held: float, px):
    k, v = fl["kind"], fl["amt"]
    reserve = BNB_RESERVE if fl["from"] == "BNB" else 0.0
    if k == "pct":
        q = (held - reserve) if v >= 100 else held * v / 100
    elif k == "usd":
        q = v / px if px else 0.0
    else:
        q = v
    return max(0.0, tt._floor8(q))


# ---- two-swap quote + executor ---------------------------------------------

async def two_leg_quote(chat_id: int, w, frm: str, to: str, qty: float, fs: str, ts: str):
    """(out, info) or (None, None). Leg 1 = baw quote frm -> USDT. Leg 2 = our estimate on that USDT
    (baw quote, site quote as fallback). Leg 2 is re-priced for real when leg 1 lands."""
    try:
        usdt = await tg_adv._bq(chat_id, frm, rwa.USDT, qty)
    except tw.SessionExpired:
        raise
    except tg_adv._BAW_ERR:
        return None, None
    leg2 = tt._floor8(usdt)
    if not routing_rules.bot_allowed(rwa.USDT, to, leg2)[0]:
        return None, None
    out = None
    try:
        out = await tg_adv._bq(chat_id, rwa.USDT, to, leg2)
    except tw.SessionExpired:
        raise
    except tg_adv._BAW_ERR:
        try:
            out = (await swap.quote(rwa.USDT, to, leg2, taker=w.address)).get("uiOutAmount")
        except Exception:
            out = None
    if not out:
        return None, None
    return float(out), {"two": True, "usdt": usdt, "label": f"{fs} → USDT → {ts}"}


def _tail(p: dict, *tx):
    row = [ui.cb_btn(ui.BACK, f"st:{p['u']}")] if p.get("u") else []
    row += [ui.cb_btn("Swap 🔄", "swp"), ui.cb_btn(ui.BOOK, "book"), ui.cb_btn(ui.BOARD, "hm")]
    txb = [ui.link_btn(label, ui.scan_tx(h)) for label, h in tx if h]
    return ui.kb(txb, row)


def _two_receipt(p: dict, s: dict, b: dict | None, secs: float):
    tx_s = tt._tx(s["row"])
    if s["status"] == "FAILED":
        return f"⛔ Swap failed at step 1. Nothing changed.{ui.sw_error(s.get('error'))}", _tail(p, ("Step 1 tx", tx_s))
    if s["status"] == "PENDING":
        return "⏳ Step 1 still confirming. Step 2 was not sent. Check Book in a minute.", _tail(p)
    got = tt._num((s["row"] or {}).get("toTokenActualQty"), p.get("usdt") or 0.0)
    first = f"Step 1: <code>{p['qty']:.6g}</code> {p['fs']} → <code>{got:,.2f}</code> USDT."
    if b and b["status"] == "FINISHED":
        recv = tt._num(b["row"].get("toTokenActualQty"), 0.0)
        return (f"✅ <b>Transaction successful</b>\n{first}\nStep 2: received <code>{recv:.6g}</code> {p['ts']}.\n"
                f"Time <code>{ui._clock(secs)}</code>"), _tail(p, ("Step 1 tx", tx_s), ("Step 2 tx", tt._tx(b["row"])))
    if b and b["status"] == "PENDING":
        return f"{first}\nStep 2 still confirming. Check Book in a minute.", _tail(p, ("Step 1 tx", tx_s))
    why = ui.sw_error((b or {}).get("error"))
    return (f"{first}\n⛔ Step 2 failed.{why} Your USDT is in your wallet. Tap Swap to finish it."), _tail(p, ("Step 1 tx", tx_s))


async def exec_two_leg(cb: CallbackQuery, w, p: dict):
    """p: frm, to (mints), fs, ts (symbols), qty, usdt (quoted leg-1 output), optional u (stock to go back to)."""
    chat_id = cb.message.chat.id
    st = {"step": 1, "t0": time.monotonic()}
    await ui.show(cb, ui.rotate_progress_text(p["fs"], p["ts"], 1, 0, tg_adv.ROTATE_EST_S))
    ticker = asyncio.create_task(tg_adv._tick(cb, {"sym": p["fs"], "to": p["ts"]}, st))
    s, b = None, None
    try:
        async with tw.lock(chat_id):
            s = await tt._run_swap(chat_id, p["frm"], rwa.USDT, p["qty"], have_lock=True)
            if s["status"] == "FINISHED":     # leg 2 starts by itself, with the USDT actually received
                got = tt._floor8(tt._num((s["row"] or {}).get("toTokenActualQty"), (p.get("usdt") or 0.0) * 0.99))
                st["step"] = 2
                ok, why = routing_rules.bot_allowed(rwa.USDT, p["to"], got)
                if not ok:
                    b = {"status": "FAILED", "row": None, "error": why}
                else:
                    try:
                        b = await tt._run_swap(chat_id, rwa.USDT, p["to"], got, have_lock=True)
                    except tw.SessionExpired:
                        b = {"status": "FAILED", "row": None, "error": "wallet session ended"}
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
    balances.invalidate(w.address)
    await ui.show(cb, *_two_receipt(p, s, b, time.monotonic() - st["t0"]))


# ---- quote -----------------------------------------------------------------

def _back_kb():
    return ui.kb([ui.cb_btn(ui.BACK, "swq")])


async def _quote(chat_id: int, w, fl: dict):
    td = _td()
    toks = _tokens()
    a, b = toks.get(fl["from"]), toks.get(fl["to"])
    if not a or not b:
        return "Not available right now.", _back_kb()
    hold = await td._hold(w)
    held = float(hold.get(a["sym"]) or 0.0)
    px = _px(a["sym"])
    qty = _qty(fl, held, px)
    if qty <= 0:
        return "That amount is too small.", _back_kb()
    usd = qty * px if px else None
    frm, to = a["mint"], b["mint"]
    ok, why = routing_rules.bot_allowed(frm, to, usd)
    if not ok:
        return why, _back_kb()
    reserve = BNB_RESERVE if a["sym"] == "BNB" else 0.0
    short = qty + reserve > held + 1e-12
    two = routing_rules.needs_two_leg(frm, to)
    if two:
        out, info = await two_leg_quote(chat_id, w, frm, to, qty, a["sym"], b["sym"])
        firm = out is not None
    else:
        bout, info = await td._quotes(chat_id, w, frm, to, qty)
        out = bout if bout is not None else (info or {}).get("uiOutAmount")
        firm = bout is not None
    if out is None:
        session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
        if (rwa.is_ondo(frm) or rwa.is_ondo(to)) and not session.get("cashOpen"):
            return ui.not_open_text(), _back_kb()
        return ui.no_route_text(), _back_kb()
    out = float(out)
    pb = _px(b["sym"])
    vout = out * pb if pb else None
    ratio = (vout / usd) if (vout and usd) else None
    blocked = ratio is not None and ratio < BLOCK_RATIO
    warn = ratio is not None and BLOCK_RATIO <= ratio < WARN_RATIO
    lines = [f"Swap       {td._tok(qty)} {a['sym']}" + (f" ({td._usd(usd)})" if usd else ""),
             f"Receive    ≈ {out:.6g} {b['sym']}" + (f" ({td._usd(vout)})" if vout else "")]
    body = "\n".join(lines) + "\n" + td._details(info)
    notes = ""
    if short:
        notes += (f"⛔ <b>Insufficient balance.</b> You hold <code>{td._tok(held)}</code> {a['sym']}, "
                  f"need <code>{td._tok(qty + reserve)}</code>.\n")
    if blocked:
        notes += "⛔ <b>Blocked:</b> output worth under 95% of the input.\n"
    elif warn:
        notes += f"⚠️ Heads up: output is {100 - ratio * 100:.1f}% below the input value.\n"
    text = notes + f"🧾 <b>Quote · Swap {a['sym']} → {b['sym']}</b>\n<code>{body}</code>"
    if not firm:
        text += "\n⚠️ No firm wallet quote. Estimate only."
    if short or blocked or not firm:
        return text, ui.kb([ui.cb_btn("🔒 Confirm", "swn"), ui.cb_btn(ui.BACK, "swq")])
    pid = secrets.token_urlsafe(4)
    _pend[pid] = {"chat": chat_id, "two": two, "frm": frm, "to": to, "fs": a["sym"], "ts": b["sym"], "qty": qty,
                  "out": out, "usdt": (info or {}).get("usdt"), "at": time.monotonic()}
    label = "Swap anyway" if warn else f"✅ Confirm · ≈ {out:.4g} {b['sym']}"
    return text + "\n<i>Quote good for 60s</i>", ui.kb([ui.cb_btn(label, f"swk:{pid}"), ui.cb_btn(ui.BACK, "swq")])


# ---- callbacks -------------------------------------------------------------

@router.callback_query(lambda c: c.data == "swp")
@tw.guarded
async def on_swap(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    chat_id = cb.message.chat.id
    ui.clear_input(chat_id)
    _new(chat_id, cb.message.message_id)
    await cb.answer()
    await ui.show(cb, *(await _from_view(chat_id, w)))


@router.callback_query(lambda c: c.data and c.data.startswith("sw1:"))
@tw.guarded
async def on_from(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _fl(cb)
    if not fl:
        await _expired(cb)
        return
    sym = cb.data.split(":", 1)[1]
    if sym not in _tokens():
        await cb.answer("Not available right now.", show_alert=True)
        return
    fl.update({"from": sym, "to": None, "amt": None, "kind": None, "custom": False, "wait": False})
    await cb.answer()
    await ui.show(cb, *_to_view(fl))


@router.callback_query(lambda c: c.data and c.data.startswith("sw2:"))
@tw.guarded
async def on_to(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _fl(cb)
    if not fl or not fl["from"]:
        await _expired(cb)
        return
    sym = cb.data.split(":", 1)[1]
    if sym not in _tokens() or sym == fl["from"]:
        await cb.answer("Pick a different coin.", show_alert=True)
        return
    fl.update({"to": sym, "amt": None, "kind": None, "custom": False, "wait": False})
    await cb.answer()
    await ui.show(cb, *(await _amount_view(cb.message.chat.id, w)))


@router.callback_query(lambda c: c.data == "swb")
@tw.guarded
async def on_back(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    chat_id = cb.message.chat.id
    fl = _fl(cb)
    ui.clear_input(chat_id)
    await cb.answer()
    if not fl:
        await ui.show(cb, *(await _td().home(chat_id)))
    elif fl["to"]:
        fl.update({"to": None, "amt": None, "kind": None, "custom": False, "wait": False})
        await ui.show(cb, *_to_view(fl))
    elif fl["from"]:
        fl["from"] = None
        await ui.show(cb, *(await _from_view(chat_id, w)))
    else:
        await ui.show(cb, *(await _td().home(chat_id)))


@router.callback_query(lambda c: c.data and c.data.startswith("swa:"))
@tw.guarded
async def on_amount(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _fl(cb)
    if not fl or not fl["to"]:
        await _expired(cb)
        return
    fl["amt"], fl["kind"], fl["custom"] = float(cb.data.split(":", 1)[1]), "pct", False
    await cb.answer()
    await ui.show(cb, *(await _amount_view(cb.message.chat.id, w)))


@router.callback_query(lambda c: c.data == "swx")
async def on_custom(cb: CallbackQuery):
    fl = _fl(cb)
    if not fl or not fl["to"]:
        await _expired(cb)
        return
    fl["wait"] = True
    ui.inputs[cb.message.chat.id] = "swap"
    await cb.answer()
    await ui.show(cb, "✏️ <b>Custom amount</b>\nType how much:\n<code>50%</code> a percentage\n"
                      "<code>$25</code> a dollar amount\n<code>0.5</code> a token amount",
                  ui.kb([ui.cb_btn(ui.BACK, "swv")]))


@router.callback_query(lambda c: c.data in ("swv", "swq"))
@tw.guarded
async def on_view(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _fl(cb)
    if not fl or not fl["to"]:
        await _expired(cb)
        return
    fl["wait"] = False
    ui.clear_input(cb.message.chat.id)
    await cb.answer()
    await ui.show(cb, *(await _amount_view(cb.message.chat.id, w)))


def _typing(m: Message) -> bool:
    fl = _sw.get(m.chat.id)
    return bool(fl and fl.get("wait") and m.text and not m.text.startswith("/") and ui.inputs.get(m.chat.id) == "swap")


@router.message(_typing)
@tw.guarded
async def on_typed(message: Message):
    chat_id = message.chat.id
    fl = _sw.get(chat_id)
    w = tw.get_wallet(chat_id)
    if not fl or not w:
        return
    parsed = _td()._parse_amount(message.text, "sell")
    try:
        await message.delete()
    except Exception:
        pass
    if not parsed:
        await ui.edit(message.bot, chat_id, fl["mid"], "✏️ <b>Custom amount</b>\nThat did not parse. Try again.",
                      ui.kb([ui.cb_btn(ui.BACK, "swv")]))
        return
    fl["kind"], fl["amt"] = parsed
    fl["custom"], fl["wait"], fl["at"] = True, False, time.monotonic()
    ui.clear_input(chat_id)
    await ui.edit(message.bot, chat_id, fl["mid"], *(await _amount_view(chat_id, w)))


@router.callback_query(lambda c: c.data == "swg")
@tw.guarded
async def on_continue(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    chat_id = cb.message.chat.id
    fl = _fl(cb)
    if not fl or not fl["to"]:
        await _expired(cb)
        return
    if fl["amt"] is None:
        await cb.answer("Pick an amount first.", show_alert=True)
        return
    if tw.lock(chat_id).locked():
        await cb.answer("Another action is running.")
        return
    await cb.answer()
    await ui.show(cb, ui.working_text("Getting your quote"))
    await ui.show(cb, *(await _quote(chat_id, w, fl)))


@router.callback_query(lambda c: c.data == "swn")
async def on_locked(cb: CallbackQuery):
    await cb.answer("Locked. Check the note at the top of the quote.", show_alert=True)


def _direct_receipt(p: dict, res: dict):
    row, st = res["row"], res["status"]
    if st == "FAILED":
        return f"⛔ Swap failed. Nothing filled.{ui.sw_error(res.get('error'))}", _tail(p, ("Tx", tt._tx(row)))
    if st == "PENDING":
        return "⏳ Still confirming. Check Book in a minute.", _tail(p)
    got = tt._num((row or {}).get("toTokenActualQty"), p["out"])
    return (f"✅ <b>Swapped</b>\n<code>{p['qty']:.6g}</code> {p['fs']} → <code>{got:.6g}</code> {p['ts']}"), _tail(p, ("Tx", tt._tx(row)))


@router.callback_query(lambda c: c.data and c.data.startswith("swk:"))
@tw.guarded
async def on_confirm(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    w = await tt._gate(cb)
    if not w:
        return
    pid = cb.data.split(":", 1)[1]
    p = _pend.get(pid)
    if not p or p["chat"] != chat_id:
        await cb.answer("Already handled or expired.")
        return
    if tw.lock(chat_id).locked():
        await cb.answer("Another action is running.")
        return
    _pend.pop(pid, None)            # one tap consumes the id
    await cb.answer()
    if time.monotonic() - p["at"] > TTL:    # stale: re-quote, never trade on an old price
        fl = _sw.get(chat_id)
        if not fl:
            await ui.show(cb, "Quote expired. Start again.", ui.kb([ui.cb_btn(ui.BOARD, "hm")]))
            return
        await ui.show(cb, ui.working_text("Re-checking prices"))
        await ui.show(cb, *(await _quote(chat_id, w, fl)))
        return
    if p["two"]:
        await exec_two_leg(cb, w, p)
    else:
        await ui.show(cb, ui.working_text("Swapping"))
        res = await tt._run_swap(chat_id, p["frm"], p["to"], p["qty"])
        balances.invalidate(w.address)
        await ui.show(cb, *_direct_receipt(p, res))
    _sw.pop(chat_id, None)
