"""AI basket: equal weight NVDA / AMD / META via the cheapest wrapper that has a tape and a share ratio.
Built from the cached snapshot + one swap.quote(USDT, mint, usd/3) per filled leg. Quote only, never signs."""
from __future__ import annotations

import asyncio
import time

import rwa
import swap

THEME = "ai"
NAMES = ("NVDA", "AMD", "META")
DEFAULT_USD = 50.0
_TTL = 30.0
_cache: dict = {}  # usd -> (monotonic ts, payload)


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


async def build(usd: float = DEFAULT_USD) -> dict:
    hit = _cache.get(usd)
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
        "theme": THEME,
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
        _cache[usd] = (time.monotonic(), payload)
    return payload
