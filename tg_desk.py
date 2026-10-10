"""StreetTape bot: the desk flow. Home -> Stocks -> Stock page -> Buy / Sell / Rotate -> Quote -> Confirm.
Same data and rules as the site (board groups, route flag, cross-wrapper gap, swap.quote details, Ondo pairs).
Signing and polling reuse tg_trade (one baw swap per leg, chat lock held end to end). No URL in message text."""
import time
import asyncio
import html
import logging
import secrets

from aiogram import Router
from aiogram.types import CallbackQuery, Message

import agent
import balances
import basket
import news
import prices
import routing_rules
import rwa
import swap
import tg_adv
import tg_trade as tt
import tg_ui as ui
import tg_wallet as tw

log = logging.getLogger("tg_desk")
router = Router()

TOP_N = 9
TTL = 60.0                      # quote life
FLOW_TTL = 1800.0
BUY_USD = (10, 25, 50)
PCTS = (25, 50, 75, 100)
BUY_MIN_USD, BUY_MAX_USD = 1.0, 1000.0
ONDO_MIN_USD = routing_rules.MIN_USD
ROT_MIN_USD = routing_rules.BOT_ROTATE_MIN_USD
BNB_RESERVE = 0.0002            # kept back for gas, same as the site's sheet
COINS = ("BNB", "USDC", "USDT")
COIN_MINT = {"BNB": rwa.NATIVE, "USDC": rwa.USDC, "USDT": rwa.USDT}
PLAT_LABEL = {"xstocks": "x", "ondo": "Ondo", "bstocks": "b"}
PCODE = {"xstocks": "x", "ondo": "o", "bstocks": "b"}
PFROM = {v: k for k, v in PCODE.items()}
SESSIONS = {"CASH OPEN": ("🟢", "Cash Open"), "PRE-MARKET": ("🟡", "Pre Market"),
            "AFTER-HOURS": ("🟠", "After Hours"), "WEEKEND": ("⚪", "Weekend")}
COPY_SHUT = ("At 4pm the stock freezes; the token keeps trading. A frozen print is not a market, so we never "
             "trade a wrapper against it: rotate only swaps one wrapper for another.")
STK, SWP, ROT_BTN, CONTINUE = "Stocks 📉", "Swap 🔄", "🔁 Rotate", "Continue"

_flows: dict[int, dict] = {}    # chat -> buy / sell / rotate form state
_pend: dict[str, dict] = {}     # pid -> quoted trade waiting for Confirm
_arb_cache: dict[str, tuple] = {}


# ---- small helpers ---------------------------------------------------------

def _usd(v) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _tok(v) -> str:
    return f"{float(v):.6g}"


def _price(sym: str):
    p = ((prices.get_prices() or {}).get("prices") or {}).get(sym)
    return p.get("price") if p else None


def _coin_px(coin: str):
    return 1.0 if coin in ("USDT", "USDC") else _price(coin)


def _gc():
    now = time.monotonic()
    for k in [k for k, f in _flows.items() if now - f["at"] > FLOW_TTL]:
        _flows.pop(k, None)
    for k in [k for k, p in _pend.items() if now - p["at"] > TTL * 5]:
        _pend.pop(k, None)


def _group(u: str):
    u = (u or "").upper()
    return next((g for g in rwa.get_cached_snapshot().get("groups") or [] if g["underlying"].upper() == u), None)


def _wrap_list(g: dict) -> list[dict]:
    """Wrappers of one stock, cheapest per share first (no price last), exactly the token page's order."""
    out = []
    for w in g.get("wrappers") or []:
        px, ratio = w.get("tokenPrice"), w.get("tokenToShareRatio")
        out.append({**w, "_ps": (px / ratio) if (px and ratio) else None})
    out.sort(key=lambda w: (w["_ps"] is None, w["_ps"] or 0))
    return out


def _has_tape(w: dict) -> bool:
    return bool(w.get("hasTape") and w.get("mint"))


def _is_ondo(w: dict | None) -> bool:
    return bool(w and w.get("platform") == "ondo")


def _lock_reason(coin: str, w: dict | None) -> str | None:
    """Bot locks (routing_rules): xStocks entirely, BNB with Ondo. USDC and USDT stay open for Ondo."""
    return routing_rules.bot_lock((w or {}).get("platform"), coin)


def _fmt_impact(v) -> str:
    try:
        pct = abs(float(v)) * 100
    except (TypeError, ValueError):
        return "—"
    return "<0.01%" if pct < 0.01 else f"{pct:.2f}%"


def _fmt_fees(o: dict) -> str:
    f = o.get("fees") or {}
    parts = []
    if f.get("gasBnb") and f["gasBnb"] < 0.1:
        parts.append(f"~{f['gasBnb']:.6f} BNB gas")
    if f.get("tradeFeeUsd"):
        parts.append(f"${f['tradeFeeUsd']:.2f} trade fee")
    if o.get("transferFeeBps"):
        parts.append(f"{o['transferFeeBps'] / 100:.2f}% transfer fee")
    return " + ".join(parts) or "—"


def _parse_amount(text: str, side: str):
    """Typed custom amount -> (kind, value) or None. buy: dollars. sell/rotate: 50% | $25 | 0.5 tokens."""
    t = (text or "").strip().replace(",", "")
    try:
        if side == "buy":
            v = float(t.lstrip("$"))
            return ("usd", v) if BUY_MIN_USD <= v <= BUY_MAX_USD else None
        if t.endswith("%"):
            v = float(t[:-1])
            return ("pct", v) if 0 < v <= 100 else None
        if t.startswith("$"):
            v = float(t[1:])
            return ("usd", v) if v > 0 else None
        v = float(t)
        return ("tok", v) if v > 0 else None
    except ValueError:
        return None


# ---- Home ------------------------------------------------------------------

async def session_note(session: dict) -> str:
    if not session.get("cashOpen"):
        return COPY_SHUT
    note = "Cash is open, so tape and official agree."
    try:
        import sessionbook
        w = await asyncio.to_thread(sessionbook.widest_line)
    except Exception:
        w = None
    if w and w.get("line"):
        return f"{note} Widest gap while cash was shut: {html.escape(str(w['line']))}."
    return f"{note} No shut-session tape stored yet."


async def wallet_value(address: str) -> tuple[float | None, dict]:
    try:
        hold = (await balances.get_balances(address)).get("holdings") or {}
    except Exception:
        return None, {}
    total = 0.0
    for sym, amt in hold.items():
        if not amt or amt <= 0:
            continue
        px = _coin_px(sym) if sym in COINS else _price(sym)
        if px:
            total += amt * px
    return total, hold


async def home(chat_id: int):
    s = rwa.session_now()
    icon, label = SESSIONS.get(s["label"], ("⚪", s["label"].title()))
    w = tw.get_wallet(chat_id)
    if w:
        total, _ = await wallet_value(w.address)
        wal = f"💼 <b>{_usd(total)}</b>  wallet value"
    else:
        wal = "💼 <b>$0.00</b>  link a wallet to trade"
    text = (f"<b>StreetTape</b>\n"
            f"<b>{icon} {label.upper()}</b>\n"
            f"🕒 Lagos {s['wat']} · New York {s['et']} · NY close {s['nyCloseAtWat']} WAT\n\n"
            f"<i>{await session_note(s)}</i>\n\n{wal}")
    markup = ui.kb([ui.cb_btn(STK, "stk"), ui.cb_btn(SWP, "swp")],
                   [ui.cb_btn(ui.PACK, "pk:ai:50")],
                   [ui.cb_btn(ui.BOOK, "book"), ui.cb_btn(ui.SEND, "snd"),
                    ui.cb_btn(ui.LINKED if w else ui.LINK, "link:go")])
    return text, markup


# ---- Stocks (top 9) --------------------------------------------------------

def stocks_view():
    top = (rwa.get_cached_snapshot().get("groups") or [])[:TOP_N]
    if not top:
        return "Board is warming up. Try again in a moment.", ui.kb([ui.cb_btn(ui.BOARD, "hm")])
    lines = []
    for g in top:
        if g.get("noYahoo") or not g.get("markPrice"):
            lines.append(f"{g['underlying']:<6}{'Tape only':>12}")
        else:
            lines.append(f"{g['underlying']:<6}{_usd(g['markPrice']):>11} {rwa.format_premium(g.get('premiumToOfficial')):>7}")
    text = ("<b>📉 Stocks</b>\n<i>Official mark, and the cheapest wrapper's gap to it.</i>\n"
            "<code>" + "\n".join(lines) + "</code>\nPick a stock 👇")
    btns = [ui.cb_btn(g["underlying"], f"st:{g['underlying']}") for g in top]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([ui.cb_btn(ui.BOARD, "hm")])
    return text, ui.kb(*rows)


# ---- Stock page ------------------------------------------------------------

async def _route_rows(u: str, g: dict):
    try:
        r = await asyncio.wait_for(basket.route_board(u, 10.0), 6.0)
        rows = {x["symbol"]: x for x in r.get("wrappers") or []}
        return next((s for s, x in rows.items() if x.get("route")), None), rows
    except Exception:
        c = basket._cheapest(g)
        return (c["symbol"] if c else None), {}


async def _arb(u: str):
    hit = _arb_cache.get(u)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    out = None
    try:
        hits = agent.check_cross_arb(u, threshold=0.0)
        if hits:
            try:
                priced = await asyncio.wait_for(agent.net_arb_quote(hits[0], 50.0), 8.0)
            except Exception:
                priced = None
            out = {"hit": hits[0], "priced": priced}
    except Exception:
        log.debug("tg_desk: arb lookup failed", exc_info=True)
    _arb_cache[u] = (time.monotonic(), out)
    return out


def _rotate_line(a) -> str:
    if not a:
        return ""
    hit, p = a["hit"], a["priced"]
    tail = (f"net ≈ {p['netBps']:+.0f} bps · " + ("✅ viable" if p["viable"] else "⛔ not viable at $50")) if p else "quote unavailable"
    return f"🔁 <b>Rotate</b> {hit['rich']['symbol']} → {hit['cheap']['symbol']}\nGap {hit['gap'] * 100:.1f}% · {tail}"


def _news_line(u: str) -> str:
    items = [n for n in news.get_news() if (n.get("underlying") or "").upper() == u]
    if not items:
        return ""
    items.sort(key=lambda n: n.get("publishedAt") or "", reverse=True)
    return f"📰 {html.escape(items[0]['body'])}"


def _watching(chat_id: int, u: str) -> bool:
    import telegram_bot as tb
    return tb._is_watching(chat_id, u)


async def stock_view(chat_id: int, u: str):
    g = _group(u)
    if not g:
        return None
    u = g["underlying"]
    (win, rrows), arb = await asyncio.gather(_route_rows(u, g), _arb(u))
    s = rwa.session_now()
    head = f"<b>{u}</b> · {html.escape(g.get('name') or u)}"
    if g.get("noYahoo") or not g.get("markPrice"):
        head += "\nOfficial: tape only"
    else:
        head += f"\nOfficial {_usd(g['markPrice'])} · {rwa.format_premium(g.get('premiumToOfficial'))}"
        if g.get("fairPrice") and not s.get("cashOpen"):
            head += f" · Fair {_usd(g['fairPrice'])}"
    lines = []
    for w in _wrap_list(g):
        lbl = PLAT_LABEL.get(w.get("platform"), "?")
        if w.get("tokenPrice"):
            px = f"{_usd(w['_ps'])}/sh" if w["_ps"] is not None else _usd(w["tokenPrice"])
            body = f"{lbl:<5}{w['symbol']:<7}{px:>11} {rwa.format_premium(w.get('premium')):>7}"
        else:
            body = f"{lbl:<5}{w['symbol']:<7}{'— no tape':>19}"
        r = rrows.get(w.get("symbol")) or {}
        tag = ""
        if w.get("symbol") == win:
            tag = " Route"
        elif r and not r.get("ok") and r.get("perShare") is not None and r.get("reason") and r["reason"] != "no tape":
            tag = " " + (html.escape(r["reason"]) if len(r["reason"]) <= 24 else "fails 95% check")
        lines.append(f"{'🟢' if w.get('symbol') == win else '▫️'} <code>{body}</code>{tag}")
    about = (f"<b>About {html.escape(g.get('name') or u)}</b>\n{html.escape(g.get('name') or u)} is a BNB Chain tokenized "
             "equity. Economic exposure only. No ownership, voting or other legal rights.")
    parts = [head + "\n\n" + "\n".join(lines), f"<i>{await session_note(s)}</i>", _rotate_line(arb), about, _news_line(u)]
    text = "\n\n".join(p for p in parts if p)

    tape = next((w for w in _wrap_list(g) if _has_tape(w)), None)
    watch = (ui.cb_btn("👁 Unwatch", f"uw:{u}") if _watching(chat_id, u) else ui.cb_btn(ui.WATCH, f"wa:{u}"))
    links = [ui.link_btn(ui.OPEN_SITE, ui.site(f"/t/{u}"))]
    mint = (tape or {}).get("mint")
    if mint:
        links.append(ui.link_btn("Contract", ui.scan_tok(mint)))
    markup = ui.kb([ui.cb_btn(ui.BUY, f"bu:{u}"), ui.cb_btn(ui.SELL, f"se:{u}"), ui.cb_btn(ROT_BTN, f"ro:{u}")],
                   [ui.cb_btn(ui.SEND, f"snd:{(tape or {}).get('symbol') or u}"), watch, ui.cb_btn(ui.CHECK, f"chk:{u}:10")],
                   links,
                   [ui.cb_btn(ui.BACK, "stk")])
    return text, markup


# ---- gates -----------------------------------------------------------------

async def _show_stock(cb: CallbackQuery, u: str):
    v = await stock_view(cb.message.chat.id, u)
    if not v:
        await ui.show(cb, "Not available right now.", ui.kb([ui.cb_btn(ui.BACK, "stk")]))
        return
    await ui.show(cb, *v)


@router.callback_query(lambda c: c.data == "hm")
async def on_home(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    ui.clear_input(chat_id)
    _flows.pop(chat_id, None)
    await cb.answer()
    await ui.show(cb, *(await home(chat_id)))


@router.callback_query(lambda c: c.data == "swp")
async def on_swap_soon(cb: CallbackQuery):
    await cb.answer("Swap is coming soon.", show_alert=True)


@router.callback_query(lambda c: c.data == "stk")
async def on_stocks(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    ui.clear_input(chat_id)
    _flows.pop(chat_id, None)
    await cb.answer()
    await ui.show(cb, *stocks_view())


@router.callback_query(lambda c: c.data and c.data.startswith("st:"))
async def on_stock(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    ui.clear_input(chat_id)
    _flows.pop(chat_id, None)
    await cb.answer()
    await _show_stock(cb, cb.data.split(":", 1)[1])


@router.callback_query(lambda c: c.data and c.data.startswith(("wa:", "uw:")))
async def on_watch(cb: CallbackQuery):
    import telegram_bot as tb
    kind, u = cb.data.split(":", 1)
    chat_id = cb.message.chat.id
    if kind == "wa":
        res = tb._add_watch(chat_id, u, underlying=u)
        if res == "cap":
            await cb.answer(f"Watch limit reached ({tb.MAX_WATCHES_PER_CHAT}). Unwatch one first.", show_alert=True)
            return
        await cb.answer("Watching" if res == "added" else "Already watching")
    else:
        await cb.answer("Unwatched" if tb._remove_watch(chat_id, u) else "Not watching")
    await _show_stock(cb, u)


# ---- flow state + screens --------------------------------------------------

def _new_flow(chat_id: int, side: str, u: str, mid: int, **kw) -> dict:
    _gc()
    fl = {"side": side, "u": u, "mid": mid, "at": time.monotonic(), "wrap": None, "coin": "USDT", "amt": None,
          "kind": None, "custom": False, "await": False, **kw}
    _flows[chat_id] = fl
    return fl


def _held(hold: dict, sym: str) -> float:
    return float(hold.get(sym) or 0.0)


def _amt_label(fl: dict) -> str:
    v, k = fl["amt"], fl["kind"]
    return {"usd": f"${v:g}", "pct": f"{v:g}%", "tok": f"{v:g}"}[k] + " ✏️"


def _flow_buttons(fl: dict, wl: list[dict]):
    side = fl["side"]
    sel = next((w for w in wl if w["platform"] == fl["wrap"]), None)
    rows = []
    if side in ("buy", "sell"):
        rows.append([ui.cb_btn(("✅ " if w["platform"] == fl["wrap"] else "") + ("🔒 " if (not _has_tape(w) or routing_rules.bot_lock(w["platform"])) else "") + w["symbol"],
                               f"fw:{PCODE.get(w['platform'], 'x')}") for w in wl])
        rows.append([ui.cb_btn(("✅ " if fl["coin"] == c else "") + ("🔒 " if _lock_reason(c, sel) else "") + c, f"fc:{c}")
                     for c in COINS])
    pick = (lambda n: f"${n}") if side == "buy" else (lambda n: "Max" if n == 100 else f"{n}%")
    vals = BUY_USD if side == "buy" else PCTS
    if side == "buy" and _is_ondo(sel):
        vals = tuple(n for n in vals if n >= ONDO_MIN_USD)
    amts = [ui.cb_btn(("✅ " if (not fl["custom"] and fl["amt"] == n) else "") + pick(n), f"fa:{n}") for n in vals]
    custom = ui.cb_btn(_amt_label(fl) if fl["custom"] else "✏️ Custom", "fx")
    if side == "buy":
        rows.append(amts + [custom])
    else:
        rows += [amts[:3], amts[3:] + [custom]]
    rows.append([ui.cb_btn(CONTINUE, "fg"), ui.cb_btn(ui.BACK, "fb")])
    return rows


def _holdings_block(hold: dict, wl: list[dict] | None = None, coins: bool = True) -> str:
    lines = []
    if wl is not None:
        for w in wl:
            amt = _held(hold, w["symbol"])
            px = w.get("tokenPrice")
            val = f"≈ {_usd(amt * px)}" if (amt and px) else ""
            lines.append(f"{PLAT_LABEL.get(w['platform'], '?'):<5}{w['symbol']:<7}{_tok(amt) if amt else '—':>11} {val}")
    if coins:
        for c in COINS:
            amt = _held(hold, c)
            px = _coin_px(c)
            lines.append(f"{c:<12}{_tok(amt):>12} " + (f"≈ {_usd(amt * px)}" if (px and c == 'BNB') else ""))
    return "<code>" + "\n".join(lines) + "</code>"


async def _hold(w) -> dict:
    try:
        return (await balances.get_balances(w.address)).get("holdings") or {}
    except Exception:
        return {}


async def _flow_view(chat_id: int, w):
    fl = _flows[chat_id]
    side, u = fl["side"], fl["u"]
    g = _group(u)
    if not g:
        return "Not available right now.", ui.kb([ui.cb_btn(ui.BACK, "stk")])
    wl = _wrap_list(g)
    hold = await _hold(w)
    sel = next((x for x in wl if x["platform"] == fl.get("wrap")), None)
    mn = f"\n<i>Ondo minimum ${ONDO_MIN_USD:g}.</i>" if _is_ondo(sel) else ""
    if side == "buy":
        head = (f"🟢 <b>Buy {u}</b>\nPick a wrapper, a coin to pay with, and an amount.{mn}\n\n"
                f"<b>Your coins</b>\n{_holdings_block(hold)}")
    elif side == "sell":
        head = (f"🔴 <b>Sell {u}</b>\nPick the wrapper, the coin you want to receive, and how much.{mn}\n\n"
                f"<b>Your holdings</b>\n{_holdings_block(hold, wl, coins=False)}")
    else:
        frm = next((x for x in wl if x["symbol"] == fl["from"]), None)
        amt = _held(hold, fl["from"])
        px = (frm or {}).get("tokenPrice")
        head = (f"🔁 <b>Rotate {u}</b>\n{fl['from']} → {fl['to']} · gap {fl['gap'] * 100:.1f}%\n\n"
                f"<b>You hold</b>\n<code>{_tok(amt)} {fl['from']}" + (f" ≈ {_usd(amt * px)}" if px else "") + "</code>\n\n"
                f"Same stock, cheaper wrapper. We sell {fl['from']} to USDT, then buy {fl['to']} with it. "
                f"Never traded against a frozen print. Minimum ${ROT_MIN_USD:g}.\n\nHow much?")
    return head, ui.kb(*_flow_buttons(fl, wl))


async def _edit_flow(chat_id: int, bot, w):
    fl = _flows[chat_id]
    text, markup = await _flow_view(chat_id, w)
    await ui.edit(bot, chat_id, fl["mid"], text, markup)


# ---- opening a flow --------------------------------------------------------

@router.callback_query(lambda c: c.data and c.data.startswith(("bu:", "se:")))
@tw.guarded
async def on_open(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    chat_id = cb.message.chat.id
    side = "buy" if cb.data.startswith("bu:") else "sell"
    u = cb.data.split(":", 1)[1]
    g = _group(u)
    if not g:
        await cb.answer("Not available right now.", show_alert=True)
        return
    ui.clear_input(chat_id)
    wl = _wrap_list(g)
    hold = await _hold(w)
    if side == "buy":
        win, _ = await _route_rows(u, g)
        pre = (next((x for x in wl if x["symbol"] == win and not routing_rules.bot_lock(x["platform"])), None)
               or next((x for x in wl if _has_tape(x) and not routing_rules.bot_lock(x["platform"])), None)
               or next((x for x in wl if _has_tape(x)), None))
    else:
        held = [x for x in wl if _held(hold, x["symbol"]) > 0]
        held.sort(key=lambda x: (bool(routing_rules.bot_lock(x["platform"])),
                                 -(_held(hold, x["symbol"]) * (x.get("tokenPrice") or 0))))
        pre = held[0] if held else (next((x for x in wl if _has_tape(x) and not routing_rules.bot_lock(x["platform"])), None)
                                    or next((x for x in wl if _has_tape(x)), None))
    await cb.answer()
    _new_flow(chat_id, side, u, cb.message.message_id, wrap=(pre or {}).get("platform"))
    await ui.show(cb, *(await _flow_view(chat_id, w)))


async def _rot_plan(w, u: str, size_usd: float | None = None):
    """(plan, why). Held wrapper of `u` -> cheapest wrapper, net of both legs' costs (same rule as the site's Rotate)."""
    h = await tt._holding(w, u)
    if not h:
        return None, "no_holding"
    sym, amt, _, mint = h
    g, hw = tt.find(sym)
    cheap = tg_adv._bot_cheapest(g, hw)
    if not (g and hw and cheap) or cheap["symbol"].upper() == sym.upper():
        return None, "none"
    px, ratio = hw.get("tokenPrice"), tt.share_ratio(sym)
    if not px or not hw.get("hasTape") or hw.get("thin"):
        return None, "none"
    held_ps, cheap_ps = px / ratio, cheap["_perShare"]
    if held_ps <= cheap_ps:
        return None, "none"
    qty_full = tt._floor8(amt)
    usd = size_usd if size_usd is not None else qty_full * px
    if usd < ROT_MIN_USD:
        return None, "small"
    gap = held_ps / cheap_ps - 1
    hit = {"underlying": u, "rich": hw, "cheap": cheap, "gap": gap, "normalized": True,
           "richRatio": ratio, "cheapRatio": cheap["tokenToShareRatio"],
           "richSharePrice": held_ps, "cheapSharePrice": cheap_ps, "sizeUsd": usd}
    net = await agent.net_arb_quote(hit, usd)
    if not net["viable"]:
        return None, "costs"
    return {"u": u, "sym": sym, "mint": mint, "held": amt, "px": px, "gap": gap, "net": net,
            "to": cheap["symbol"], "to_mint": cheap["mint"]}, None


_WHY = {"no_holding": "you hold no {u} to rotate.", "none": "no cheaper wrapper right now.",
        "small": f"too small, minimum ${ROT_MIN_USD:g}.", "costs": "costs eat the gap right now."}


@router.callback_query(lambda c: c.data and c.data.startswith("ro:"))
@tw.guarded
async def on_rotate_open(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    if tw.lock(cb.message.chat.id).locked():
        await cb.answer("Another action is running.")
        return
    chat_id = cb.message.chat.id
    u = cb.data.split(":", 1)[1]
    plan, why = await _rot_plan(w, u)
    if not plan:
        await cb.answer("Not viable: " + _WHY.get(why, "not available.").format(u=u), show_alert=True)
        return
    ui.clear_input(chat_id)
    await cb.answer()
    _new_flow(chat_id, "rot", u, cb.message.message_id, **{"from": plan["sym"], "to": plan["to"], "gap": plan["gap"]})
    await ui.show(cb, *(await _flow_view(chat_id, w)))


# ---- form taps -------------------------------------------------------------

def _flow_of(cb: CallbackQuery):
    fl = _flows.get(cb.message.chat.id)
    if fl:
        fl["at"] = time.monotonic()
        fl["mid"] = cb.message.message_id
    return fl


async def _expired(cb: CallbackQuery):
    await cb.answer("That screen expired. Start again.", show_alert=True)
    await ui.show(cb, *(await home(cb.message.chat.id)))


@router.callback_query(lambda c: c.data and c.data.startswith("fw:"))
@tw.guarded
async def on_wrap(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _flow_of(cb)
    if not fl or fl["side"] == "rot":
        await _expired(cb)
        return
    plat = PFROM.get(cb.data.split(":", 1)[1])
    g = _group(fl["u"])
    sel = next((x for x in _wrap_list(g or {}) if x["platform"] == plat), None)
    if not sel or not _has_tape(sel):
        await cb.answer("No tape for this wrapper yet.", show_alert=True)
        return
    lock = routing_rules.bot_lock(plat)
    if lock:
        await cb.answer(lock, show_alert=True)
        return
    fl["wrap"] = plat
    if _lock_reason(fl["coin"], sel):
        fl["coin"] = "USDT"
    await cb.answer()
    await ui.show(cb, *(await _flow_view(cb.message.chat.id, w)))


@router.callback_query(lambda c: c.data and c.data.startswith("fc:"))
@tw.guarded
async def on_coin(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _flow_of(cb)
    if not fl or fl["side"] == "rot":
        await _expired(cb)
        return
    coin = cb.data.split(":", 1)[1]
    sel = next((x for x in _wrap_list(_group(fl["u"]) or {}) if x["platform"] == fl["wrap"]), None)
    why = _lock_reason(coin, sel)
    if why:
        await cb.answer(why, show_alert=True)
        return
    fl["coin"] = coin
    await cb.answer()
    await ui.show(cb, *(await _flow_view(cb.message.chat.id, w)))


@router.callback_query(lambda c: c.data and c.data.startswith("fa:"))
@tw.guarded
async def on_amount(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _flow_of(cb)
    if not fl:
        await _expired(cb)
        return
    fl["amt"], fl["kind"], fl["custom"] = float(cb.data.split(":", 1)[1]), ("usd" if fl["side"] == "buy" else "pct"), False
    await cb.answer()
    await ui.show(cb, *(await _flow_view(cb.message.chat.id, w)))


@router.callback_query(lambda c: c.data == "fx")
async def on_custom(cb: CallbackQuery):
    fl = _flow_of(cb)
    if not fl:
        await _expired(cb)
        return
    fl["await"] = True
    ui.inputs[cb.message.chat.id] = "flow"
    ask = ("Type a dollar amount, 1 to 1000." if fl["side"] == "buy" else
           "Type how much:\n<code>50%</code> a percentage\n<code>$25</code> a dollar amount\n<code>0.5</code> a token amount")
    await cb.answer()
    await ui.show(cb, f"✏️ <b>Custom amount</b>\n{ask}", ui.kb([ui.cb_btn(ui.BACK, "fv")]))


@router.callback_query(lambda c: c.data == "fv")
@tw.guarded
async def on_form_view(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _flow_of(cb)
    if not fl:
        await _expired(cb)
        return
    fl["await"] = False
    ui.clear_input(cb.message.chat.id)
    await cb.answer()
    await ui.show(cb, *(await _flow_view(cb.message.chat.id, w)))


@router.callback_query(lambda c: c.data == "fb")
async def on_form_back(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    fl = _flows.pop(chat_id, None)
    ui.clear_input(chat_id)
    await cb.answer()
    if fl:
        await _show_stock(cb, fl["u"])
    else:
        await ui.show(cb, *stocks_view())


def _typing_flow(m: Message) -> bool:
    fl = _flows.get(m.chat.id)
    return bool(fl and fl.get("await") and m.text and not m.text.startswith("/") and ui.inputs.get(m.chat.id) == "flow")


@router.message(_typing_flow)
@tw.guarded
async def on_typed(message: Message):
    chat_id = message.chat.id
    fl = _flows.get(chat_id)
    w = tw.get_wallet(chat_id)
    if not fl or not w:
        return
    parsed = _parse_amount(message.text, fl["side"])
    try:
        await message.delete()
    except Exception:
        pass
    if not parsed:
        await ui.edit(message.bot, chat_id, fl["mid"],
                      "✏️ <b>Custom amount</b>\nThat did not parse. Try again.",
                      ui.kb([ui.cb_btn(ui.BACK, "fv")]))
        return
    fl["kind"], fl["amt"] = parsed
    fl["custom"], fl["await"], fl["at"] = True, False, time.monotonic()
    ui.clear_input(chat_id)
    await _edit_flow(chat_id, message.bot, w)


# ---- quote -----------------------------------------------------------------

async def _quotes(chat_id: int, w, frm: str, to: str, qty: float):
    """(baw out | None, site quote dict | None). The baw quote is what executes; the site quote supplies the details."""
    async def bq():
        try:
            q = await tw.baw(chat_id, "market-order", "quote", "--fromTokenQty", tt._qty(qty), "--fromToken", frm,
                             "--toToken", to, "--binanceChainId", tt.CHAIN)
            return float(q["toCoinAmount"])
        except tw.SessionExpired:
            raise
        except (tw.BawError, KeyError, TypeError, ValueError):
            return None

    async def sq():
        try:
            q = await swap.quote(frm, to, qty, taker=w.address)
            return q if q.get("uiOutAmount") else None
        except Exception:
            return None
    return await asyncio.gather(bq(), sq())


def _details(o: dict | None) -> str:
    if not o:
        return ""
    return (f"Route      {html.escape(str(o.get('routeLabel') or 'Binance Agentic Wallet'))}\n"
            f"Impact     {_fmt_impact(o.get('priceImpactPct'))}\n"
            f"Fees       {_fmt_fees(o)}\n")


def _qkb(label: str, pid: str | None):
    first = ui.cb_btn(label, f"fk:{pid}") if pid else ui.cb_btn(label, "fn")
    return ui.kb([first, ui.cb_btn(ui.BACK, "fq")])


async def _quote_view(chat_id: int, w):
    fl = _flows[chat_id]
    if fl["side"] == "buy":
        return await _quote_buy(chat_id, w, fl)
    if fl["side"] == "sell":
        return await _quote_sell(chat_id, w, fl)
    return await _quote_rot(chat_id, w, fl)


async def _quote_buy(chat_id: int, w, fl: dict):
    g = _group(fl["u"])
    sel = next((x for x in _wrap_list(g or {}) if x["platform"] == fl["wrap"]), None)
    coin, usd = fl["coin"], fl["amt"]
    px = _coin_px(coin)
    if not px:
        return "Price for that coin is unavailable right now.", ui.kb([ui.cb_btn(ui.BACK, "fq")])
    qty = tt._floor8(usd / px)
    hold = await _hold(w)
    have = _held(hold, coin)
    need = qty + (BNB_RESERVE if coin == "BNB" else 0.0)
    short = have < need
    frm, to = COIN_MINT[coin], sel["mint"]
    bout, sq = await _quotes(chat_id, w, frm, to, qty)
    out = bout if bout is not None else (sq or {}).get("uiOutAmount")
    if out is None:
        session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
        if rwa.is_ondo(to) and not session.get("cashOpen"):
            return ui.not_open_text(), ui.kb([ui.cb_btn(ui.BACK, "fq")])
        return ui.no_route_text(), ui.kb([ui.cb_btn(ui.BACK, "fq")])
    out = float(out)
    sc = swap.share_check(frm, to, qty, {"uiOutAmount": out})
    blocked = bool(sc and sc.get("blocked"))
    ratio = tt.share_ratio(sel["symbol"])
    lines = [f"Pay        {_tok(qty)} {coin} ({_usd(usd)})",
             f"Receive    ≈ {out:.6g} {sel['symbol']}",
             f"Shares     ≈ {out * ratio:.6g} {fl['u']}"]
    if sc and sc.get("perShare"):
        lines.append(f"Per share  {_usd(sc['perShare'])}")
        if sc.get("ref"):
            lines.append(f"vs {sc['ref']['label']:<9}{sc['ref']['devPct']:+.1f}%")
    body = "\n".join(lines) + "\n" + _details(sq)
    if sq and sq.get("uiMinReceived"):
        body += f"Min recv   {float(sq['uiMinReceived']):.6g} {sel['symbol']}\n"
    notes = []
    if short:
        notes.append(f"⛔ <b>Insufficient balance.</b> You have <code>{_tok(have)}</code> {coin}, "
                     f"need <code>{_tok(need)}</code>.\n")
    if blocked:
        notes.append(f"⛔ <b>Blocked:</b> {html.escape(sc.get('reason') or 'output worth under 95% of the spend')}.\n")
    text = "".join(notes) + f"🧾 <b>Quote · Buy {sel['symbol']}</b>\n<code>{body}</code>"
    if bout is None:
        text += "\n⚠️ No firm wallet quote. Estimate only."
    if short or blocked or bout is None:
        return text, _qkb("🔒 Confirm", None)
    pid = secrets.token_urlsafe(4)
    _pend[pid] = {"kind": "buy", "chat": chat_id, "u": fl["u"], "sym": sel["symbol"], "frm": frm, "to": to,
                  "qty": qty, "out": out, "usd": usd, "coin": coin, "at": time.monotonic()}
    return text + "\n<i>Quote good for 60s</i>", _qkb(f"✅ Confirm · {_usd(usd)}", pid)


def _sell_qty(fl: dict, held: float, px: float | None) -> float:
    k, v = fl["kind"], fl["amt"]
    if k == "pct":
        return tt._floor8(held if v >= 100 else held * v / 100)
    if k == "usd":
        return tt._floor8(v / px) if px else 0.0
    return tt._floor8(v)


async def _quote_sell(chat_id: int, w, fl: dict):
    g = _group(fl["u"])
    sel = next((x for x in _wrap_list(g or {}) if x["platform"] == fl["wrap"]), None)
    coin, sym = fl["coin"], sel["symbol"]
    hold = await _hold(w)
    held = _held(hold, sym)
    qty = _sell_qty(fl, held, sel.get("tokenPrice"))
    short = qty > held + 1e-12 or held <= 0
    frm, to = sel["mint"], COIN_MINT[coin]
    bout, sq = await _quotes(chat_id, w, frm, to, qty)
    out = bout if bout is not None else (sq or {}).get("uiOutAmount")
    if out is None:
        session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
        if rwa.is_ondo(frm) and not session.get("cashOpen"):
            return ui.not_open_text(), ui.kb([ui.cb_btn(ui.BACK, "fq")])
        return ui.no_route_text(), ui.kb([ui.cb_btn(ui.BACK, "fq")])
    out = float(out)
    cpx = _coin_px(coin)
    sc = swap.share_check(frm, to, qty, {"uiOutAmount": out})
    lines = [f"Sell       {_tok(qty)} {sym}",
             f"Receive    ≈ {out:.6g} {coin}" + (f" ({_usd(out * cpx)})" if cpx else "")]
    if sc and sc.get("perShare"):
        lines.append(f"Per share  {_usd(sc['perShare'])}")
        if sc.get("ref"):
            lines.append(f"vs {sc['ref']['label']:<9}{sc['ref']['devPct']:+.1f}%")
    body = "\n".join(lines) + "\n" + _details(sq)
    if sq and sq.get("uiMinReceived"):
        body += f"Min recv   {float(sq['uiMinReceived']):.6g} {coin}\n"
    notes = ""
    if short:
        notes += (f"⛔ <b>Insufficient balance.</b> You hold <code>{_tok(held)}</code> {sym}, "
                  f"need <code>{_tok(qty)}</code>.\n")
    if sc and sc.get("warn"):
        notes += f"⚠️ Heads up: {html.escape(sc.get('reason') or 'output looks low vs the tape')}.\n"
    text = notes + f"🧾 <b>Quote · Sell {sym}</b>\n<code>{body}</code>"
    if bout is None:
        text += "\n⚠️ No firm wallet quote. Estimate only."
    if short or bout is None:
        return text, _qkb("🔒 Confirm", None)
    pid = secrets.token_urlsafe(4)
    _pend[pid] = {"kind": "sell", "chat": chat_id, "u": fl["u"], "sym": sym, "frm": frm, "to": to, "qty": qty,
                  "out": out, "coin": coin, "at": time.monotonic()}
    label = "Sell anyway" if (sc and sc.get("warn")) else f"✅ Confirm · ≈ {out:.4g} {coin}"
    return text + "\n<i>Quote good for 60s</i>", _qkb(label, pid)


async def _quote_rot(chat_id: int, w, fl: dict):
    u = fl["u"]
    hold = await _hold(w)
    sym = fl["from"]
    held = _held(hold, sym)
    _, hw = tt.find(sym)
    px = (hw or {}).get("tokenPrice")
    qty = _sell_qty(fl, held, px)
    usd = qty * (px or 0)
    if usd < ROT_MIN_USD:
        return f"Too small to rotate. Minimum ${ROT_MIN_USD:g}.", ui.kb([ui.cb_btn(ui.BACK, "fq")])
    plan, why = await _rot_plan(w, u, size_usd=usd)
    if not plan:
        reason = _WHY.get(why, "not available.").format(u=u)
        return f"⛔ <b>Not viable.</b> {reason[:1].upper() + reason[1:]}", ui.kb([ui.cb_btn(ui.BACK, "fq")])
    short = qty > held + 1e-12
    try:
        out = await tg_adv._bq(chat_id, plan["mint"], rwa.USDT, qty)
    except tw.SessionExpired:
        raise
    except tg_adv._BAW_ERR:
        session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
        if rwa.is_ondo(plan["mint"]) and not session.get("cashOpen"):
            return ui.not_open_text(), ui.kb([ui.cb_btn(ui.BACK, "fq")])
        return ui.no_route_text(), ui.kb([ui.cb_btn(ui.BACK, "fq")])
    net = plan["net"]
    if (1 - out / usd) * 1e4 >= net["grossBps"]:
        return ui.moved_text(), ui.kb([ui.cb_btn(ui.BACK, "fq")])
    buy_ref = net["buyLeg"].get("uiOutAmount")
    buy = buy_ref * out / usd if buy_ref else None
    cost = max(0.0, usd - out) + agent._leg_cost_bps(net["buyLeg"]) / 1e4 * out
    lines = [f"Sell       {_tok(qty)} {sym}", f"Get        ≈ {_usd(out)} USDT"]
    if buy:
        lines.append(f"Buy        ≈ {buy:.4g} {plan['to']}")
    lines += [f"Costs      ≈ {_usd(cost)}", f"Gap        {plan['gap'] * 100:+.1f}%", f"Net        {net['netBps']:+.0f} bps"]
    notes = ""
    if short:
        notes = (f"⛔ <b>Insufficient balance.</b> You hold <code>{_tok(held)}</code> {sym}, "
                 f"need <code>{_tok(qty)}</code>.\n")
    text = notes + f"🧾 <b>Quote · Rotate {sym} → {plan['to']}</b>\n<code>" + "\n".join(lines) + "</code>\nTwo swaps via USDT."
    if short:
        return text, _qkb("🔒 Confirm", None)
    pid = secrets.token_urlsafe(4)
    _pend[pid] = {"kind": "rot", "chat": chat_id, "u": u, "sym": sym, "mint": plan["mint"], "qty": qty,
                  "to": plan["to"], "to_mint": plan["to_mint"], "out": out, "at": time.monotonic()}
    return text + "\n<i>Quote good for 60s</i>", _qkb("✅ Confirm · Rotate", pid)


@router.callback_query(lambda c: c.data == "fg")
@tw.guarded
async def on_continue(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    chat_id = cb.message.chat.id
    fl = _flow_of(cb)
    if not fl:
        await _expired(cb)
        return
    if fl["amt"] is None:
        await cb.answer("Pick an amount first.", show_alert=True)
        return
    if fl["side"] in ("buy", "sell"):
        sel = next((x for x in _wrap_list(_group(fl["u"]) or {}) if x["platform"] == fl["wrap"]), None)
        if not sel or not _has_tape(sel):
            await cb.answer("Pick a wrapper first.", show_alert=True)
            return
        why = _lock_reason(fl["coin"], sel)
        if why:
            await cb.answer(why, show_alert=True)
            return
        if fl["side"] == "buy":
            frm, to, usd = COIN_MINT[fl["coin"]], sel["mint"], (fl["amt"] if fl["kind"] == "usd" else None)
        else:
            held = _held(await _hold(w), sel["symbol"])
            if held <= 0:
                await cb.answer(f"You hold no {sel['symbol']}.", show_alert=True)
                return
            px = sel.get("tokenPrice")
            usd = _sell_qty(fl, held, px) * px if px else None
            frm, to = sel["mint"], COIN_MINT[fl["coin"]]
        ok, why = routing_rules.bot_allowed(frm, to, usd)
        if not ok:
            await cb.answer(why, show_alert=True)
            return
    if tw.lock(chat_id).locked():
        await cb.answer("Another action is running.")
        return
    await cb.answer()
    await ui.show(cb, ui.working_text("Getting your quote"))
    await ui.show(cb, *(await _quote_view(chat_id, w)))


@router.callback_query(lambda c: c.data == "fq")
@tw.guarded
async def on_quote_back(cb: CallbackQuery):
    w = await tt._gate(cb)
    if not w:
        return
    fl = _flow_of(cb)
    if not fl:
        await _expired(cb)
        return
    await cb.answer()
    await ui.show(cb, *(await _flow_view(cb.message.chat.id, w)))


@router.callback_query(lambda c: c.data == "lk:x")
async def on_locked_wrapper(cb: CallbackQuery):
    await cb.answer(routing_rules.XSTOCKS_LOCK_MSG, show_alert=True)


@router.callback_query(lambda c: c.data == "fn")
async def on_locked_confirm(cb: CallbackQuery):
    await cb.answer("Locked. Check the note at the top of the quote.", show_alert=True)


# ---- confirm ---------------------------------------------------------------

def _receipt(p: dict, res: dict):
    row, st = res["row"], res["status"]
    tail = [ui.cb_btn(ui.BACK, f"st:{p['u']}"), ui.cb_btn(ui.BOOK, "book"), ui.cb_btn(ui.BOARD, "hm")]
    txb = [ui.link_btn(ui.VIEW_TX, ui.scan_tx(tt._tx(row)))] if tt._tx(row) else []
    if st == "FAILED":
        why = f" {res['error']}." if res.get("error") else ""
        return f"⛔ Swap failed. Nothing filled.{why}", ui.kb(txb, tail)
    if st == "PENDING":
        return "⏳ Still confirming. Check Book in a minute.", ui.kb(tail)
    got = tt._num((row or {}).get("toTokenActualQty"), p["out"])
    if p["kind"] == "buy":
        sh = got * tt.share_ratio(p["sym"])
        return (f"✅ <b>Filled</b>\n<code>{got:.4g}</code> {p['sym']} for <code>{_tok(p['qty'])}</code> {p['coin']}\n"
                f"≈ <code>{sh:.4g}</code> {p['u']} shares"), ui.kb(txb, tail)
    return (f"✅ <b>Sold</b>\n<code>{_tok(p['qty'])}</code> {p['sym']} for <code>{got:.6g}</code> {p['coin']}"), ui.kb(txb, tail)


@router.callback_query(lambda c: c.data and c.data.startswith("fk:"))
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
        fl = _flows.get(chat_id)
        if not fl:
            await ui.show(cb, "Quote expired. Start again.", ui.kb([ui.cb_btn(ui.BOARD, "hm")]))
            return
        await ui.show(cb, ui.working_text("Re-checking prices"))
        await ui.show(cb, *(await _quote_view(chat_id, w)))
        return
    if p["kind"] == "rot":
        await tg_adv._exec_rotate(cb, w, p)
        _flows.pop(chat_id, None)
        return
    await ui.show(cb, ui.working_text("Buying" if p["kind"] == "buy" else "Selling"))
    res = await tt._run_swap(chat_id, p["frm"], p["to"], p["qty"])
    balances.invalidate(w.address)
    _flows.pop(chat_id, None)
    await ui.show(cb, *_receipt(p, res))
