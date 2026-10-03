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
import earnings
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
        import earnings
        e = earnings.info(underlying)
        earn = (f"\n⚠ {underlying} reports in {e['hoursUntil']:.0f}h — desk proposes selling into USDT before the print"
                if e["inside24h"] else "")
        text = (
            f"StreetTape alert · {report['session'].get('label')}\n"
            f"<b>{hit['symbol']}</b> {rwa.format_premium(hit['premium'])} vs mark\n"
            f"Tape ${hit['tokenPrice']:.2f} · Mark ${hit['markPrice']:.2f}\n"
            f"{telegram_bot.WEB_PUBLIC_URL}/t/{underlying}"
            f"{cta}{earn}"
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
# Unit of the aggregator's priceImpactPct. Set from /api/_impactprobe verdict.
# "percent": 0.004 = 0.004% = 0.4 bps (x100). "fraction": 0.004 = 0.4% = 40 bps (x10000).
PRICE_IMPACT_UNIT = os.getenv("PRICE_IMPACT_UNIT", "fraction").strip().lower()


def check_cross_arb(underlying: str | None = None, size_usd: float = ARB_USD_SIZE,
                    threshold: float | None = None) -> list[dict]:
    """Share-normalized cross-wrapper gap for every underlying with 2+ tapes
    (or just `underlying` if given). Sell the richest wrapper, buy the
    cheapest, share price = tokenPrice / tokenToShareRatio. Legs without a
    ratio are dropped; <2 ratio-backed legs -> no arb. Proposal only — no auto-trade,
    no sizing beyond the flat USD notional passed in.
    Returns one dict per underlying whose raw gap clears ARB_THRESHOLD,
    sorted richest-gap first. Net-of-cost refusal happens later in
    `_arb_quotes` once real quotes are in (this pass is quote-free)."""
    snap = rwa.get_cached_snapshot()
    groups = snap.get("groups") or []
    if underlying:
        groups = [g for g in groups if g["underlying"].upper() == underlying.upper()]
    min_gap = ARB_THRESHOLD if threshold is None else max(0.0, threshold)

    hits = []
    for g in groups:
        priced = [w for w in g["wrappers"] if w.get("tokenPrice") and w.get("hasTape") and not w.get("thin")]
        if len(priced) < 2:
            continue
        normed = []
        for w in priced:
            ratio = w.get("tokenToShareRatio")  # live underlying-profile only; seed multiplier ignored
            if not ratio or ratio <= 0:
                continue
            px = w["tokenPrice"] / ratio
            normed.append((px, w))
        if len(normed) < 2:
            continue
        cheap_px, cheap = min(normed, key=lambda x: x[0])
        rich_px, rich = max(normed, key=lambda x: x[0])
        if cheap["symbol"] == rich["symbol"]:
            continue
        gap = rich_px / cheap_px - 1
        if gap <= min_gap:
            continue
        hits.append({
            "underlying": g["underlying"],
            "rich": rich, "cheap": cheap,
            "gap": gap,
            "normalized": True,  # both legs are ratio-backed by construction
            "richRatio": rich["tokenToShareRatio"], "cheapRatio": cheap["tokenToShareRatio"],
            "richSharePrice": rich_px, "cheapSharePrice": cheap_px,
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
        return abs(impact) * (10000 if PRICE_IMPACT_UNIT == "fraction" else 100)
    return ARB_COST_FLOOR_BPS


async def impact_probe(taker: str, pair: str = "TSLAB", small_usd: float = 50.0, big_usd: float = 5000.0) -> dict:
    """Settles the priceImpactPct unit: quote BNB -> wrapper at two sizes with a
    taker (real SWAP route), compare the raw field at the big size with the
    economic impact implied by the rate drop. Writes the raw values to DEVEX log."""
    import swap
    import devlog
    tok = rwa.by_symbol(pair)
    mint = (tok or {}).get("address")
    if not mint:
        return {"error": f"no mint for {pair} in snapshot"}
    bnb_px = swap._price_of_mint(rwa.NATIVE)
    if not bnb_px:
        return {"error": "no BNB price"}
    runs = []
    for usd in (small_usd, big_usd):
        amt = usd / bnb_px
        q = await swap.quote(rwa.NATIVE, mint, amt, taker)
        runs.append({"usd": usd, "bnbIn": amt, "mode": q.get("executionMode"), "provider": q.get("provider"),
                     "rawPriceImpactPct": q.get("priceImpactPct"), "uiOutAmount": q.get("uiOutAmount"),
                     "rate": q.get("rate"), "routes": q.get("routes"),
                     "error": q.get("error") or q.get("reason") or q.get("fallbackReason")})
    a, b = runs
    out = {"pair": f"BNB->{pair}", "runs": runs, "verdict": "inconclusive"}
    if not all(r["provider"] == "binance_web3" and r["mode"] == "SWAP" and r["rate"] and r["rawPriceImpactPct"] is not None for r in runs):
        out["note"] = "need provider=binance_web3, mode=SWAP and a raw impact at both sizes (pass a valid taker; RFQ/fallback does not count)"
    else:
        econ = 1 - b["rate"] / a["rate"]  # fraction of value lost going from $50 to $5000
        raw = abs(b["rawPriceImpactPct"])
        out["economicImpactFraction"] = econ
        if econ > 0.0001 and raw > 0:
            ratio = raw / econ
            out["rawOverEconomic"] = ratio
            out["verdict"] = "percent" if ratio > 10 else "fraction"
            out["action"] = ("keep x100 (PRICE_IMPACT_UNIT=percent), delete 'provisional'" if out["verdict"] == "percent"
                             else "set PRICE_IMPACT_UNIT=fraction (x10000), delete 'provisional'")
        else:
            out["note"] = "rate drop between sizes too small/noisy to compare; try a larger big_usd"
    devlog.log_call(what=f"impact probe BNB->{pair}", url="/api/v1/dex/aggregator/swap", status="n/a", ms=0,
                    actual=f"raw50={a['rawPriceImpactPct']} out50={a['uiOutAmount']} raw5000={b['rawPriceImpactPct']} "
                           f"out5000={b['uiOutAmount']} verdict={out['verdict']}")
    return out


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
        "richRatio": hit["richRatio"],
        "cheapRatio": hit["cheapRatio"],
        "richSharePrice": hit["richSharePrice"],
        "cheapSharePrice": hit["cheapSharePrice"],
        "sizeUsd": size_usd,
        "sellLeg": sell_leg,
        "buyLeg": buy_leg,
        "gap": hit["gap"],
        "gapVsOfficial": hit["rich"].get("premiumToOfficial"),
        "gapVsFair": hit["rich"].get("premiumToFair"),
        "grossBps": gap_bps,
        "costBps": cost_bps,
        "netBps": net_bps,
        "viable": net_bps > 0 and _leg_routed(sell_leg) and _leg_routed(buy_leg),
    }


_desk_cache: dict = {}
DESK_TTL = 30.0


async def _all_arbs(underlying: str | None = None) -> list[dict]:
    """Net-of-cost $50 quotes for every underlying with 2+ ratio-backed tapes
    (raw gap > 0), widest gap first. One quote set feeds both `desk_arbs` and
    `desk_book`. Cached DESK_TTL seconds."""
    import time
    key = (underlying or "").upper()
    hit = _desk_cache.get(key)
    if hit and time.monotonic() - hit[0] < DESK_TTL:
        return hit[1]
    sem = asyncio.Semaphore(4)

    async def one(h):
        async with sem:
            try:
                return await net_arb_quote(h)
            except Exception:
                log.warning("agent: arb quote failed %s", h.get("underlying"), exc_info=True)
                return None

    hits = check_cross_arb(underlying, threshold=0.0)
    arbs = [a for a in await asyncio.gather(*(one(h) for h in hits)) if a]
    _desk_cache[key] = (time.monotonic(), arbs)
    return arbs


async def desk_arbs(underlying: str | None = None, limit: int = 5) -> list[dict]:
    """Arbs whose raw gap clears ARB_THRESHOLD, net-of-cost quoted. Shared by
    /api/agent/scan and the studio tick."""
    arbs = await _all_arbs(underlying)
    if underlying:  # an explicit name is asked about directly: show its net result even under the alert threshold
        return arbs[:limit]
    return [a for a in arbs if a["gap"] > ARB_THRESHOLD][:limit]


def _leg_routed(leg: dict | None) -> bool:
    return bool(leg) and not leg.get("noRoute") and not leg.get("unsupported") and bool(leg.get("uiOutAmount"))


async def desk_book() -> list[dict]:
    """The weekend book: one row per underlying, share-normalized gap, Clears
    (both legs quoted, viable) first, then Refused (cost eats the gap, or a leg
    has no route). Names with <2 live ratio-backed non-thin tapes never enter."""
    rows = []
    for a in await _all_arbs():
        sell_ok, buy_ok = _leg_routed(a.get("sellLeg")), _leg_routed(a.get("buyLeg"))
        clears = bool(a["viable"] and sell_ok and buy_ok)
        if clears:
            reason = None
        elif not sell_ok:
            reason = f"no route {a['richSymbol']}"
        elif not buy_ok:
            reason = f"no route {a['cheapSymbol']}"
        else:
            reason = "cost eats gap"
        rows.append({
            "underlying": a["underlying"], "state": "clears" if clears else "refused", "reason": reason,
            "richSymbol": a["richSymbol"], "cheapSymbol": a["cheapSymbol"],
            "richSharePrice": a["richSharePrice"], "cheapSharePrice": a["cheapSharePrice"],
            "gapBps": a["grossBps"], "costBps": a["costBps"] if sell_ok and buy_ok else None,
            "netBps": a["netBps"] if sell_ok and buy_ok else None, "sizeUsd": a["sizeUsd"],
        })
    rows.sort(key=lambda r: (r["state"] != "clears", -r["gapBps"]))
    return rows


last_shut: dict = {}


def _last_ny_close(now: datetime) -> datetime:
    ny = now.astimezone(rwa.NY)
    c = ny.replace(hour=16, minute=0, second=0, microsecond=0)
    if ny < c:
        c -= timedelta(days=1)
    while c.weekday() >= 5:
        c -= timedelta(days=1)
    return c


def record_shut(arbs: list[dict], session: dict | None) -> None:
    """Remember the best gap that cleared cost while cash was shut. Within one
    close, never replace it with a worse net; a new close replaces the old one."""
    global last_shut
    if not session or session.get("cashOpen"):
        return
    best = best_arb(arbs)
    if not best:
        return
    now = datetime.now(timezone.utc)
    close = _last_ny_close(now)
    if last_shut and last_shut.get("closeAt") == close.isoformat() and best["netBps"] <= last_shut["arb"]["netBps"]:
        return
    last_shut = {
        "arb": {k: v for k, v in best.items() if k not in ("sellLeg", "buyLeg")},
        "label": session.get("label"),
        "fetchedAt": now.isoformat(),
        "closeAt": close.isoformat(),
        "hoursSinceClose": round((now - close).total_seconds() / 3600, 1),
    }


def best_arb(arbs: list[dict]) -> dict | None:
    """Biggest gross share-normalized gap that is still positive after cost."""
    viable = [a for a in arbs if a.get("viable")]
    return max(viable, key=lambda a: a["gap"]) if viable else None


def _arb_line(label: str, symbol: str, leg: dict, size_usd: float) -> str:
    if leg.get("uiOutAmount") is not None:
        return f"{label} {symbol} · ${size_usd:.0f} · {leg.get('routeLabel') or leg.get('provider')} quote ~{leg['uiOutAmount']:.4f} out"
    return f"{label} {symbol} · ${size_usd:.0f} · no route"


def official_vs_fair_line(underlying: str) -> str:
    """Official/fair price line for an underlying; fair shown only while cash is shut."""
    snap = rwa.get_cached_snapshot()
    g = next((g for g in snap.get("groups") or [] if g["underlying"].upper() == underlying.upper()), None)
    if not g or g.get("noYahoo") or not g.get("markPrice"):
        return ""
    session = snap.get("session") or rwa.session_now()
    out = f"Official ${g['markPrice']:.2f}"
    if g.get("fairPrice") and not session.get("cashOpen"):
        out += f" · Fair ${g['fairPrice']:.2f} synthetic ({session.get('label')})"
        if g.get("betaStatus") == "estimated":
            out += (f"\nFair inputs: β {g['beta']:.2f} · QQQ {(g.get('indexMove') or 0) * 100:+.2f}% since Fri close"
                    f" · news {(g.get('newsShock') or 0) * 100:+.0f}%")
        else:
            out += f"\nFair = last print. Beta not estimated ({g.get('betaPairs') or 0} pairs)."
    return out + "\n"


def gap_vs_line(priced: dict) -> str:
    """Rich wrapper's gap vs official, and vs fair while cash is shut. Display only:
    the arb threshold and net math stay wrapper vs wrapper."""
    off = priced.get("gapVsOfficial")
    if off is None:
        return ""
    out = f"gap vs official {rwa.format_premium(off)}"
    session = rwa.get_cached_snapshot().get("session") or rwa.session_now()
    fair = priced.get("gapVsFair")
    if fair is not None and not session.get("cashOpen"):
        out += f" · gap vs fair {rwa.format_premium(fair)}"
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
    note = f"\nShare ratios: {priced['richSymbol']} {priced['richRatio']:.6f} · {priced['cheapSymbol']} {priced['cheapRatio']:.6f}"
    viability = (
        f"net +{priced['netBps']:.0f} bps after costs"
        if priced["viable"] else
        f"gross +{priced['grossBps']:.0f} bps, but ~{priced['costBps']:.0f} bps costs eat it — not viable at ${priced['sizeUsd']:.0f}"
    )
    return (
        f"{priced['underlying']} cross-wrapper arb · {rwa.format_premium(priced['gap'])} rich · {viability}\n"
        f"{official_vs_fair_line(priced['underlying'])}"
        f"{gap_vs_line(priced)}"
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
    if leg.get("uiOutAmount") is not None:
        detail = f"{leg.get('routeLabel') or leg.get('provider')} quote ~{leg['uiOutAmount']:.4f} USDT out"
    else:
        detail = "no route"
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


# ---- BNB Agent Studio / ERC-8004 -------------------------------------------
# Same scan logic as the internal loop, exposed as a stateless tick a
# scheduler (Agent Studio job, 60s) can call. Proposal-only: never signs.
STUDIO_TOKEN = os.getenv("AGENT_STUDIO_TOKEN", "")
# Second, tick-only credential (query param `k`). Exists because the Studio platform
# forwards only an allowlist of runtime env names, so the header token cannot reach the job.
TICK_KEY = os.getenv("AGENT_TICK_KEY", "").strip()
ERC8004_REGISTRY = os.getenv("ERC8004_IDENTITY_REGISTRY", "").strip()
ERC8004_AGENT_ID = os.getenv("ERC8004_AGENT_ID", "").strip()
ERC8004_CHAIN_ID = os.getenv("ERC8004_CHAIN_ID", "").strip() or "56"
_studio_seen: dict[str, datetime] = {}


def _public_url() -> str:
    u = os.getenv("WEB_PUBLIC_URL", "").strip().rstrip("/")
    return u if u.startswith("http") else (f"https://{u}" if u else "")


def _studio_runtime(base: str) -> dict:
    """Honest Studio status, driven by env so it flips without a code edit.
    STUDIO_DEPLOYED=1 + STUDIO_JOB_ID => deployed. STUDIO_BAG_ERROR => failed run (command + error)."""
    deployed = os.getenv("STUDIO_DEPLOYED", "").strip() in ("1", "true", "yes")
    job = os.getenv("STUDIO_JOB_ID", "").strip()
    err = os.getenv("STUDIO_BAG_ERROR", "").strip()
    tick = f"{base}/api/agent/studio/tick"
    if deployed and job:
        sched = {"jobId": job, "url": tick, "everySeconds": 60, "header": "X-Studio-Token", "price": "0"}
        note = "Scheduled on Studio: calls the tick every 60s. Free face, price 0: no B402 charge, no escrow."
    else:
        deployed = False
        sched = None
        note = err if err else "Studio job not created yet."
    return {
        "deployed": deployed,
        "scheduler": sched if sched else note,
        "note": note,
        "tick": f"{tick} (X-Studio-Token required; proposal-only, never signs)",
        "x402": "Free face (price 0). No paid endpoint, so x402Support stays false.",
        "signing": "none: the user signs in their own wallet",
    }


def identity_card() -> dict:
    """ERC-8004 registration file. Host it at /agent-registration.json and pass
    that URL to the Identity Registry's register(agentURI)."""
    base = _public_url()
    card = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": "StreetTape Desk",
        "description": "Proposal-only tokenized-stock desk on BNB Chain: flags wrappers rich vs the last cash print, "
                       "finds cross-wrapper rotations net of costs, quotes flatten sells. Never signs or auto-executes.",
        "image": f"{base}/static/img/logos/bnb.png" if base else "",
        "services": [
            {"name": "web", "endpoint": f"{base}/board"},
            {"name": "scan", "endpoint": f"{base}/api/agent/studio/tick"},
            {"name": "skill", "endpoint": f"{base}/api/agent/scan"},
        ],
        "x402Support": False,
        "studioRuntime": _studio_runtime(base),
        "active": True,
        "supportedTrust": ["reputation"],
    }
    if ERC8004_REGISTRY and ERC8004_AGENT_ID.isdigit():
        card["registrations"] = [{
            "agentId": int(ERC8004_AGENT_ID),
            "agentRegistry": f"eip155:{ERC8004_CHAIN_ID}:{ERC8004_REGISTRY}",
        }]
    return card


_ROTATE_KEYS = ("underlying", "richSymbol", "cheapSymbol", "gap", "netBps", "costBps", "viable", "normalized")


async def studio_tick(size_usd: float = ARB_USD_SIZE, threshold: float | None = None,
                      arb_threshold: float | None = None, underlying: str | None = None) -> dict:
    """One scheduled scan: cross-wrapper arbs (net of cost), >2% flatten
    candidates, and cash-shut premium alerts. `new` marks symbols not proposed
    in the last COOLDOWN, so the scheduler can notify only on fresh ones."""
    now = datetime.now(timezone.utc)
    gaps = scan_gaps(threshold)
    session = gaps["session"]
    arbs = []
    for h in check_cross_arb(size_usd=size_usd, threshold=arb_threshold)[:5]:
        q = await net_arb_quote(h, size_usd)
        key = f"arb:{q['underlying']}"
        seen = _studio_seen.get(key)
        q["new"] = bool(q["viable"]) and (seen is None or now - seen >= COOLDOWN)
        if q["new"]:
            _studio_seen[key] = now
        arbs.append(q)
    record_shut(arbs, session)
    alerts = []
    if not session.get("cashOpen"):
        for h in gaps["hits"]:
            key = f"alert:{h['symbol'].upper()}"
            seen = _studio_seen.get(key)
            fresh = seen is None or now - seen >= COOLDOWN
            if fresh:
                _studio_seen[key] = now
            alerts.append({**h, "new": fresh})
    flatten = [
        {"symbol": t["symbol"], "premium": t["premium"], "tokenPrice": t.get("tokenPrice"), "mint": t.get("mint"),
         "sizeUsd": size_usd, "deepLink": t.get("url")}
        for t in sorted(flatten_candidates(), key=lambda t: t["premium"], reverse=True)
    ]
    # The three sold answers. Read-only copies of fields above; the Studio agent repeats them, never computes.
    und = (underlying or "").strip().upper() or None
    rotate = None
    if und:
        named = await desk_arbs(und, limit=1)
        rotate = {k: named[0].get(k) for k in _ROTATE_KEYS} if named else {"underlying": und, "viable": False,
                                                                           "reason": "no two live wrappers"}
    answers = {
        "richVsFriday": [{"symbol": h["symbol"], "underlying": h["underlying"], "premium": h["premium"],
                          "tokenPrice": h["tokenPrice"], "markPrice": h["markPrice"]}
                         for h in gaps["hits"] if h["premium"] > 0][:5],
        "rotate": rotate,
        "earnings": {"underlying": und, **earnings.info(und)} if und else None,
        "earningsInside24h": [e["underlying"] for e in earnings.all_info() if e["inside24h"]],
    }
    return {
        "agent": "streettape-desk",
        "mode": "proposal-only",
        "arbThreshold": ARB_THRESHOLD if arb_threshold is None else arb_threshold,
        "at": now.isoformat(),
        "session": session,
        "answers": answers,
        "arbs": arbs,
        "alerts": alerts,
        "flatten": flatten,
        "note": "Not auto-executed - confirm and sign in your own wallet.",
    }


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
