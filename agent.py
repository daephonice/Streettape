"""Rule agent: watch premium vs mark, alert on Telegram when cash is shut.

No paid LLM. Threshold default 1.5%. Quiet period 60 minutes per watch.
"""
from __future__ import annotations

import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import rwa
from database import SessionLocal
from models import Watch, utcnow

log = logging.getLogger("agent")
SCAN_SECONDS = int(os.getenv("AGENT_SCAN_SECONDS", "60"))
DEFAULT_THRESHOLD = float(os.getenv("AGENT_THRESHOLD", "0.015"))
COOLDOWN = timedelta(minutes=60)
_task = None


def scan_gaps(min_abs=None):
    min_abs = DEFAULT_THRESHOLD if min_abs is None else min_abs
    snap = rwa.get_cached_snapshot()
    session = snap.get("session") or rwa.session_now()
    hits = []
    for t in snap.get("tokens") or []:
        prem = t.get("premium")
        if prem is None or abs(prem) < min_abs:
            continue
        hits.append({
            "symbol": t["symbol"],
            "underlying": t.get("underlying"),
            "platform": t.get("platform"),
            "tokenPrice": t.get("tokenPrice"),
            "markPrice": t.get("markPrice"),
            "premium": prem,
            "status": t.get("status"),
            "session": session.get("label"),
            "cashOpen": session.get("cashOpen"),
            "deepLink": t.get("url"),
        })
    hits.sort(key=lambda h: abs(h["premium"]), reverse=True)
    return {"session": session, "threshold": min_abs, "hits": hits}


def _watches():
    db = SessionLocal()
    try:
        return db.execute(select(Watch)).scalars().all()
    finally:
        db.close()


def _mark_alerted(watch_id, premium):
    db = SessionLocal()
    try:
        w = db.get(Watch, watch_id)
        if not w:
            return
        w.last_alert_at = utcnow()
        w.last_alert_premium = premium
        db.commit()
    except Exception:
        db.rollback()
        log.warning("agent: mark alert failed", exc_info=True)
    finally:
        db.close()


async def fire_due_alerts():
    """Called by telegram_bot so we reuse the live bot instance."""
    import telegram_bot
    if not telegram_bot.BOT_TOKEN:
        return 0
    report = scan_gaps()
    if report["session"].get("cashOpen"):
        return 0
    by_sym = {h["symbol"].upper(): h for h in report["hits"]}
    snap = rwa.get_cached_snapshot()
    groups_by_und = {g["underlying"]: g for g in snap.get("groups") or []}
    sent = 0
    now = datetime.now(timezone.utc)
    for w in _watches():
        thresh = w.threshold if getattr(w, "threshold", None) not in (None, 0) else DEFAULT_THRESHOLD
        underlying = getattr(w, "underlying", None) or (rwa.by_symbol(w.symbol) or {}).get("underlying")
        if not underlying:
            continue
        siblings = [t for t in (snap.get("tokens") or []) if t.get("underlying") == underlying]
        hit = max(
            (t for t in siblings if t.get("premium") is not None and abs(t["premium"]) >= thresh),
            key=lambda t: abs(t["premium"]),
            default=None,
        )
        if not hit:
            continue
        last = w.last_alert_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < COOLDOWN:
                continue
        group = groups_by_und.get(underlying)
        cheapest_sym = group.get("cheapest") if group else None
        cta = ""
        if cheapest_sym and cheapest_sym.upper() != hit["symbol"].upper():
            cheap = by_sym.get(cheapest_sym.upper()) or next(
                (t for t in siblings if t["symbol"].upper() == cheapest_sym.upper()), None
            )
            if cheap and cheap.get("url"):
                cta = f"\nCheaper wrapper: {cheap['symbol']} {rwa.format_premium(cheap['premium'])} — {cheap['url']}"
        text = (
            f"StreetTape alert · {report['session'].get('label')}\n"
            f"<b>{hit['symbol']}</b> {rwa.format_premium(hit['premium'])} vs mark\n"
            f"Tape ${hit['tokenPrice']:.2f} · Mark ${hit['markPrice']:.2f}\n"
            f"{telegram_bot.WEB_PUBLIC_URL}/t/{underlying}"
            f"{cta}"
        )
        try:
            await telegram_bot.send_alert(w.chat_id, text)
            _mark_alerted(w.id, hit["premium"])
            sent += 1
        except Exception:
            log.warning("agent: send failed chat=%s", w.chat_id, exc_info=True)
    return sent


ARB_THRESHOLD = 0.01
ARB_USD_SIZE = 50.0
ARB_COST_FLOOR_BPS = 5.0  # spread/gas noise floor when impact data is missing


def check_cross_arb(underlying: str | None = None, size_usd: float = ARB_USD_SIZE) -> list[dict]:
    """Share-normalized cross-wrapper gap for every underlying with 2+ tapes
    (or just `underlying` if given). Sell the richest wrapper, buy the
    cheapest, both normalized by `multiplier`. Proposal only — no auto-trade,
    no sizing beyond the flat USD notional passed in.
    Returns one dict per underlying whose raw gap clears ARB_THRESHOLD,
    sorted richest-gap first. Net-of-cost refusal happens later in
    `_arb_quotes` once real quotes are in (this pass is quote-free)."""
    snap = rwa.get_cached_snapshot()
    groups = snap.get("groups") or []
    if underlying:
        groups = [g for g in groups if g["underlying"].upper() == underlying.upper()]

    hits = []
    for g in groups:
        priced = [w for w in g["wrappers"] if w.get("tokenPrice") and w.get("hasTape")]
        if len(priced) < 2:
            continue
        normed = []
        for w in priced:
            mult = w.get("multiplier")
            px = w["tokenPrice"] / mult if mult else w["tokenPrice"]
            if px:
                normed.append((px, w))
        if len(normed) < 2:
            continue
        cheap_px, cheap = min(normed, key=lambda x: x[0])
        rich_px, rich = max(normed, key=lambda x: x[0])
        if cheap["symbol"] == rich["symbol"]:
            continue
        gap = rich_px / cheap_px - 1
        if gap <= ARB_THRESHOLD:
            continue
        hits.append({
            "underlying": g["underlying"],
            "rich": rich, "cheap": cheap,
            "gap": gap,
            "normalized": bool(rich.get("multiplier")) or bool(cheap.get("multiplier")),
            "sizeUsd": size_usd,
        })
    hits.sort(key=lambda h: h["gap"], reverse=True)
    return hits


async def _arb_quotes(hit: dict, size_usd: float | None = None) -> tuple[dict, dict]:
    """Two independent legs: sell size_usd of the rich wrapper -> USDT, buy
    size_usd USDT -> the cheap wrapper. Price-only (no taker), so swap.quote()
    falls back to a Pancake deep link whenever the Trading API is unset or
    errors."""
    import swap
    size_usd = ARB_USD_SIZE if size_usd is None else size_usd
    sell_leg = await swap.quote(hit["rich"]["mint"], rwa.USDT, size_usd / (hit["rich"]["tokenPrice"] or 1))
    buy_leg = await swap.quote(rwa.USDT, hit["cheap"]["mint"], size_usd)
    return sell_leg, buy_leg


def _leg_cost_bps(leg: dict) -> float:
    """Cost to charge a leg against the gap: real price impact when the
    quote has one (SWAP mode), else a flat noise floor — RFQ legs are firm
    (no slippage) so they only pay the floor, same as a missing-impact
    fallback quote."""
    impact = leg.get("priceImpactPct")
    if impact is not None:
        return abs(impact) * 100  # priceImpactPct is a fraction (0.004 = 0.4%)
    return ARB_COST_FLOOR_BPS


async def net_arb_quote(hit: dict, size_usd: float | None = None) -> dict:
    """Quotes both legs, subtracts their cost (impact, or a flat floor for
    firm/unknown-impact legs) from the raw gap, and returns the result plus
    a `viable` flag. viable=False means costs eat the gap — proposal should
    say so rather than pushing the trade."""
    size_usd = ARB_USD_SIZE if size_usd is None else size_usd
    sell_leg, buy_leg = await _arb_quotes(hit, size_usd)
    cost_bps = _leg_cost_bps(sell_leg) + _leg_cost_bps(buy_leg)
    gap_bps = hit["gap"] * 10000
    net_bps = gap_bps - cost_bps
    return {
        "underlying": hit["underlying"],
        "richSymbol": hit["rich"]["symbol"],
        "cheapSymbol": hit["cheap"]["symbol"],
        "richPrice": hit["rich"]["tokenPrice"],
        "cheapPrice": hit["cheap"]["tokenPrice"],
        "normalized": hit["normalized"],
        "sizeUsd": size_usd,
        "sellLeg": sell_leg,
        "buyLeg": buy_leg,
        "gap": hit["gap"],
        "grossBps": gap_bps,
        "costBps": cost_bps,
        "netBps": net_bps,
        "viable": net_bps > 0,
    }


def _arb_line(label: str, symbol: str, leg: dict, size_usd: float) -> str:
    if leg.get("provider") == "binance_web3" and leg.get("uiOutAmount") is not None:
        return f"{label} {symbol} · ${size_usd:.0f} · Trading API quote ~{leg['uiOutAmount']:.4f} out"
    return f"{label} {symbol} · ${size_usd:.0f} · PancakeSwap: {leg.get('deepLink') or 'n/a'}"


def official_vs_fair_line(underlying: str) -> str:
    """Official/fair price line for an underlying; fair shown only while cash is shut."""
    snap = rwa.get_cached_snapshot()
    g = next((g for g in snap.get("groups") or [] if g["underlying"].upper() == underlying.upper()), None)
    if not g or g.get("noYahoo") or not g.get("markPrice"):
        return ""
    session = snap.get("session") or rwa.session_now()
    out = f"Official ${g['markPrice']:.2f}"
    if g.get("fairPrice") and not session.get("cashOpen"):
        out += f" · Fair ${g['fairPrice']:.2f} ({session.get('label')})"
    return out + "\n"


async def arb_text(underlying: str | None = None) -> str | None:
    """Telegram /agent body: best cross-wrapper gap across the whole board
    (or just `underlying` if given), net of estimated costs. None if nothing
    clears the threshold right now."""
    import telegram_bot
    hits = check_cross_arb(underlying)
    if not hits:
        return None
    priced = await net_arb_quote(hits[0])
    note = "" if priced["normalized"] else "\n(no share multiplier on file — raw tape price used)"
    viability = (
        f"net +{priced['netBps']:.0f} bps after costs"
        if priced["viable"] else
        f"gross +{priced['grossBps']:.0f} bps, but ~{priced['costBps']:.0f} bps costs eat it — not viable at ${priced['sizeUsd']:.0f}"
    )
    return (
        f"{priced['underlying']} cross-wrapper arb · {rwa.format_premium(priced['gap'])} rich · {viability}\n"
        f"{official_vs_fair_line(priced['underlying'])}"
        f"{_arb_line('SELL', priced['richSymbol'], priced['sellLeg'], priced['sizeUsd'])}\n"
        f"{_arb_line('BUY', priced['cheapSymbol'], priced['buyLeg'], priced['sizeUsd'])}\n"
        f"{telegram_bot.WEB_PUBLIC_URL}/t/{priced['underlying']}"
        f"{note}\n"
        f"Not auto-executed — confirm and sign in your own wallet."
    )


FLATTEN_THRESHOLD = 0.02
OPS_CHAT_ID = int(os.getenv("OPS_CHAT_ID", "0") or 0)
_last_flatten_week: tuple[int, int] | None = None


def _flatten_chat_ids() -> list[int]:
    ids = {w.chat_id for w in _watches()}
    if OPS_CHAT_ID:
        ids.add(OPS_CHAT_ID)
    return list(ids)


def flatten_candidates(underlying: str | None = None) -> list[dict]:
    snap = rwa.get_cached_snapshot()
    return [
        t for t in (snap.get("tokens") or [])
        if t.get("premium") is not None and t["premium"] > FLATTEN_THRESHOLD
        and (not underlying or (t.get("underlying") or "").upper() == underlying.upper())
    ]


async def _flatten_line(t: dict) -> str:
    import swap
    px = t.get("tokenPrice") or 0
    if px <= 0:
        return f"SELL {t['symbol']} · {rwa.format_premium(t['premium'])} rich · price unavailable"
    leg = await swap.quote(t["mint"], rwa.USDT, 50.0 / px)
    if leg.get("provider") == "binance_web3" and leg.get("uiOutAmount") is not None:
        detail = f"Trading API quote ~{leg['uiOutAmount']:.4f} USDT out"
    else:
        detail = f"PancakeSwap: {leg.get('deepLink') or 'n/a'}"
    return f"SELL {t['symbol']} · {rwa.format_premium(t['premium'])} rich · ${50:.0f} · {detail}"


async def flatten_text(underlying: str | None = None) -> str | None:
    """Sunday 18:00-18:59 UTC body: every wrapper >2% rich, sell quotes. None if
    nothing clears the bar right now (used by both the weekly loop and any
    manual check)."""
    import telegram_bot
    hits = flatten_candidates(underlying)
    if not hits:
        return None
    hits.sort(key=lambda t: t["premium"], reverse=True)
    lines = [await _flatten_line(t) for t in hits]
    return (
        "Weekly flatten · wrappers >2% rich\n"
        + "\n".join(lines)
        + f"\n{telegram_bot.WEB_PUBLIC_URL}/board\n"
        + "Not auto-executed — confirm and sign in your own wallet."
    )


async def agent_text(underlying: str | None = None) -> str | None:
    """Telegram /agent body: live cross-wrapper arb, then the flatten list
    (wrappers >2% rich; scoped to `underlying` if given). None if both are quiet."""
    parts = []
    arb = await arb_text(underlying)
    if arb:
        parts.append(arb)
    flat = await flatten_text(underlying)
    if flat:
        parts.append(flat)
    return "\n\n".join(parts) or None


async def fire_flatten() -> int:
    """Sunday 18:00 UTC window, once per ISO week (in-memory guard, no DB)."""
    global _last_flatten_week
    import telegram_bot
    if not telegram_bot.BOT_TOKEN:
        return 0
    now = datetime.now(timezone.utc)
    if now.isoweekday() != 7 or now.hour != 18:
        return 0
    iso = now.isocalendar()
    week_key = (iso[0], iso[1])
    if _last_flatten_week == week_key:
        return 0

    text = await flatten_text()
    _last_flatten_week = week_key  # mark tried regardless, so a quiet week doesn't retry all hour
    if not text:
        return 0

    sent = 0
    for chat_id in _flatten_chat_ids():
        try:
            await telegram_bot.send_alert(chat_id, text)
            sent += 1
        except Exception:
            log.warning("agent: flatten send failed chat=%s", chat_id, exc_info=True)
    return sent


async def _loop():
    await asyncio.sleep(15)
    while True:
        try:
            await fire_due_alerts()
        except Exception:
            log.exception("agent: scan failed")
        try:
            await fire_flatten()
        except Exception:
            log.exception("agent: flatten check failed")
        await asyncio.sleep(SCAN_SECONDS)


def start_agent_task():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
