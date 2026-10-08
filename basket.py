"""Thematic baskets (ai, semis, defensive): equal weight via the cheapest wrapper that has a tape and a share ratio.
Built from the cached snapshot + one swap.quote(USDT, mint, usd/3) per filled leg. Quote only, never signs."""
from __future__ import annotations

import asyncio
import os
import time

import rwa
import swap

THEMES = {
    "ai": ("AI", ("NVDA", "AMD", "META")),
    "semis": ("Semis", ("NVDA", "AMD", "TSLA")),
    "defensive": ("Defensive", ("QQQ", "AAPL", "META")),
}
THEME = "ai"  # default theme; defensive.py's rotation sells this one
NAMES = THEMES[THEME][1]
DEFAULT_USD = 50.0
_TTL = 30.0
_cache: dict = {}  # (theme, usd) -> (monotonic ts, payload)


def _cheapest(group: dict | None) -> dict | None:
    """Cheapest per-share wrapper with a live tape, a contract and a share ratio. Thin pools never qualify."""
    best, best_px = None, None
    for w in (group or {}).get("wrappers") or []:
        ratio, px = w.get("tokenToShareRatio"), w.get("tokenPrice")
        if not (w.get("hasTape") and w.get("mint") and not w.get("thin") and px and ratio):
            continue
        per_share = px / ratio
        if best_px is None or per_share < best_px:
            best, best_px = w, per_share
    if best is None:
        return None
    return {**best, "_perShare": best_px}


async def build(theme: str = THEME, usd: float = DEFAULT_USD) -> dict:
    label, NAMES = THEMES[theme]
    key = (theme, usd)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]

    snap = rwa.get_cached_snapshot()
    session = snap.get("session") or rwa.session_now()
    cash_open = bool(session.get("cashOpen"))
    groups = {g["underlying"]: g for g in snap.get("groups") or []}
    slot = usd / len(NAMES)

    picks = {n: _cheapest(groups.get(n)) for n in NAMES}
    filled = [n for n in NAMES if picks[n]]
    quotes = await asyncio.gather(
        *(swap.quote(rwa.USDT, picks[n]["mint"], slot) for n in filled), return_exceptions=True
    )
    qmap = dict(zip(filled, quotes))

    legs = []
    for n in NAMES:
        g = groups.get(n) or {}
        pick = picks[n]
        leg = {
            "underlying": n,
            "name": g.get("name"),
            "weight": 1 / len(NAMES),
            "official": g.get("markPrice"),
            "fair": g.get("fairPrice") if not cash_open else None,
            "filled": False,
        }
        if not pick:
            leg["reason"] = "no tape with a share ratio" if g else "not in snapshot yet"
            legs.append(leg)
            continue
        q = qmap[n]
        if isinstance(q, Exception):
            leg["reason"] = f"quote failed: {type(q).__name__}"
            legs.append(leg)
            continue
        if q.get("noRoute") or q.get("uiOutAmount") is None:
            leg["reason"] = "no route quoted (Ondo RFQ needs a connected wallet for a firm quote)" if pick.get("platform") == "ondo" else "no route quoted"
            leg["fallbackReason"] = q.get("fallbackReason")
            legs.append(leg)
            continue
        leg.update({
            "filled": True,
            "symbol": pick["symbol"],
            "platform": pick["platform"],
            "contract": pick["mint"],
            "ratio": pick["tokenToShareRatio"],
            "tape": pick["tokenPrice"],
            "perShare": pick["_perShare"],
            "amountUsd": slot,
            "outAmount": q.get("uiOutAmount"),
            "minReceived": q.get("uiMinReceived"),
            "provider": q.get("provider"),
            "executionMode": q.get("executionMode"),
            "priceImpactPct": q.get("priceImpactPct"),
            "routeLabel": q.get("routeLabel"),
            "fallbackFrom": q.get("fallbackFrom"),
            "fallbackReason": q.get("fallbackReason"),
        })
        legs.append(leg)

    n_filled = sum(1 for l in legs if l["filled"])
    payload = {
        "theme": theme,
        "label": label,
        "names": list(NAMES),
        "sizeUsd": usd,
        "legUsd": slot,
        "from": "USDT",
        "session": session,
        "fetchedAt": snap.get("fetchedAt"),
        "tapeStale": bool(snap.get("tapeStale")),
        "legs": legs,
        "filledUsd": slot * n_filled,
        "unfilledUsd": slot * (len(NAMES) - n_filled),
        "note": "Quotes only. Nothing is signed until you confirm each leg in your own wallet.",
    }
    if n_filled:
        _cache[key] = (time.monotonic(), payload)
    return payload


QUICKBUY_BAND_BPS = float(os.getenv("QUICKBUY_BAND_BPS", "100"))  # reject a wrapper whose per-share tape is farther than this from the cash print


def _candidate(w: dict, mark: float | None) -> dict:
    """One wrapper row for quick_pick: per-share price, gap to cash print, and why it was rejected (if it was)."""
    ratio, px = w.get("tokenToShareRatio"), w.get("tokenPrice")
    c = {"symbol": w.get("symbol"), "platform": w.get("platform"), "perShare": None, "gapBps": None, "ok": False, "reason": None}
    if not w.get("mint"):
        c["reason"] = "no contract"
    elif not (w.get("hasTape") and px):
        c["reason"] = "no tape"
    elif w.get("thin"):
        c["reason"] = "thin pool"
    elif not ratio:
        c["reason"] = "no share ratio"
    else:
        c["perShare"] = px / ratio
        if mark:
            c["gapBps"] = round((c["perShare"] / mark - 1) * 1e4, 1)
            if abs(c["gapBps"]) > QUICKBUY_BAND_BPS:
                c["reason"] = f"{c['gapBps']:+.0f} bps from cash print, band is {QUICKBUY_BAND_BPS:.0f}"
            else:
                c["ok"] = True
    return c


async def quick_pick(underlying: str, usd: float = 10.0) -> dict:
    """Score every wrapper of one underlying against the cash print, drop any outside the band,
    quote survivors cheapest first, return the winner plus all candidates with reasons. Quote only."""
    u = underlying.upper()
    snap = rwa.get_cached_snapshot()
    g = next((x for x in snap.get("groups") or [] if x["underlying"] == u), None)
    if not g:
        return {"ok": False, "underlying": u, "usd": usd, "candidates": [], "reason": "not on the board"}
    mark = g.get("markPrice")
    wmap = {w.get("symbol"): w for w in g.get("wrappers") or []}
    cands = [_candidate(w, mark) for w in wmap.values()]
    base = {"underlying": u, "usd": usd, "mark": mark, "bandBps": QUICKBUY_BAND_BPS, "candidates": cands}
    if not mark:
        return {**base, "ok": False, "reason": "no cash print to check against"}
    for c in sorted((c for c in cands if c["ok"]), key=lambda c: c["perShare"]):
        try:
            q = await swap.quote(rwa.USDT, wmap[c["symbol"]]["mint"], usd)
        except Exception as e:
            c["ok"], c["reason"] = False, f"quote failed: {type(e).__name__}"
            continue
        if q.get("noRoute") or q.get("uiOutAmount") is None:
            c["ok"], c["reason"] = False, "no route quoted"
            continue
        c["win"] = True
        return {**base, "ok": True, "symbol": c["symbol"], "platform": c["platform"], "perShare": c["perShare"],
                "gapBps": c["gapBps"], "outAmount": q.get("uiOutAmount"), "provider": q.get("provider")}
    return {**base, "ok": False, "reason": "no wrapper passed the cash-print band and had a route"}


def pick_lines(r: dict) -> list[str]:
    """Plain-text lines: every wrapper's per-share price, gap, and verdict. Shared by Telegram."""
    out = []
    for c in r.get("candidates") or []:
        px = f"${c['perShare']:,.2f}/sh" if c.get("perShare") else "no price"
        gap = f" {c['gapBps']:+.0f}bps" if c.get("gapBps") is not None else ""
        mark = "WIN" if c.get("win") else ("ok" if c.get("ok") else f"rejected: {c.get('reason')}")
        out.append(f"{c['symbol']} {px}{gap} - {mark}")
    return out
