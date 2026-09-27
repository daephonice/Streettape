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


ARB_UNDERLYING = "NVDA"
ARB_RICH_SYMBOL = "NVDAB"   # multiplier-normalized, may be != 1 share
ARB_CHEAP_SYMBOL = "NVDAx"  # multiplier None -> 1:1 share
ARB_THRESHOLD = 0.01
ARB_USD_SIZE = 50.0


def check_nvda_arb() -> dict | None:
    """Hardcoded cross-wrapper rule: NVDAB share-normalized price >1% above
    NVDAx. Not auto-traded — proposal only, sizing is a fixed $50/$50 notional.
    Returns None if either tape is missing or the gap doesn't clear threshold."""
    snap = rwa.get_cached_snapshot()
    tokens = {t["symbol"].upper(): t for t in (snap.get("tokens") or [])}
    rich = tokens.get(ARB_RICH_SYMBOL.upper())
    cheap = tokens.get(ARB_CHEAP_SYMBOL.upper())
    if not rich or not cheap:
        return None
    rich_px, cheap_px = rich.get("tokenPrice"), cheap.get("tokenPrice")
    if not rich_px or not cheap_px:
        return None

    rich_mult = rich.get("multiplier")
    cheap_mult = cheap.get("multiplier")
    normalized = bool(rich_mult) or bool(cheap_mult)
    rich_norm = rich_px / rich_mult if rich_mult else rich_px
    cheap_norm = cheap_px / cheap_mult if cheap_mult else cheap_px
    if not rich_norm or not cheap_norm:
        return None

    gap = rich_norm / cheap_norm - 1
    if gap <= ARB_THRESHOLD:
        return None

    return {
        "underlying": ARB_UNDERLYING,
        "rich": rich, "cheap": cheap,
        "gap": gap,
        "normalized": normalized,
        "sizeUsd": ARB_USD_SIZE,
    }


async def _arb_quotes(hit: dict) -> tuple[dict, dict]:
    """Two independent legs: sell $50 of NVDAB -> USDT, buy $50 USDT -> NVDAx.
    Price-only (no taker), so swap.quote() falls back to a Pancake deep link
    whenever the Trading API is unset or errors."""
    import swap
    sell_leg = await swap.quote(hit["rich"]["mint"], rwa.USDT, ARB_USD_SIZE / (hit["rich"]["tokenPrice"] or 1))
    buy_leg = await swap.quote(rwa.USDT, hit["cheap"]["mint"], ARB_USD_SIZE)
    return sell_leg, buy_leg


def _arb_line(label: str, symbol: str, leg: dict) -> str:
    if leg.get("provider") == "binance_web3" and leg.get("uiOutAmount") is not None:
        return f"{label} {symbol} · ${ARB_USD_SIZE:.0f} · Trading API quote ~{leg['uiOutAmount']:.4f} out"
    return f"{label} {symbol} · ${ARB_USD_SIZE:.0f} · PancakeSwap: {leg.get('deepLink') or 'n/a'}"


async def arb_text() -> str | None:
    """Telegram /agent body, or None if the rule isn't currently firing."""
    import telegram_bot
    hit = check_nvda_arb()
    if not hit:
        return None
    sell_leg, buy_leg = await _arb_quotes(hit)
    note = "" if hit["normalized"] else "\n(no share multiplier on file — raw tape price used)"
    return (
        f"NVDA cross-wrapper arb · {rwa.format_premium(hit['gap'])} rich\n"
        f"{_arb_line('SELL', hit['rich']['symbol'], sell_leg)}\n"
        f"{_arb_line('BUY', hit['cheap']['symbol'], buy_leg)}\n"
        f"{telegram_bot.WEB_PUBLIC_URL}/t/NVDA"
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


def flatten_candidates() -> list[dict]:
    snap = rwa.get_cached_snapshot()
    return [
        t for t in (snap.get("tokens") or [])
        if t.get("premium") is not None and t["premium"] > FLATTEN_THRESHOLD
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


async def flatten_text() -> str | None:
    """Sunday 18:00-18:59 UTC body: every wrapper >2% rich, sell quotes. None if
    nothing clears the bar right now (used by both the weekly loop and any
    manual check)."""
    import telegram_bot
    hits = flatten_candidates()
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
