"""Defensive rotation: when the QQQ wrapper has moved >= threshold since Friday 16:00 ET, quote selling the AI basket
(NVDA/AMD/META, usd/3 each, wrapper picked by basket._cheapest) into USDT, then buying the cheapest QQQ wrapper with the
dollars those sells quoted. Quote only, never signs. A rotation, not an arb: price impact is reported, never netted
against the move."""
from __future__ import annotations

import asyncio
import time

import agent
import basket
import fair
import rwa
import swap

THRESHOLD = agent.DEFAULT_THRESHOLD  # same default as the cash-shut alert (AGENT_THRESHOLD, 0.015)
DEFAULT_USD = 50.0
DEFENSIVE = "QQQ"
_TTL = 30.0
_cache: dict = {}  # usd -> (monotonic ts, payload)


def _routed(q) -> bool:
    return not isinstance(q, Exception) and not q.get("noRoute") and q.get("uiOutAmount") is not None


def _leg_quote(q) -> dict:
    if isinstance(q, Exception):
        return {"reason": f"quote failed: {type(q).__name__}"}
    if not _routed(q):
        return {"reason": "no route quoted", "fallbackReason": q.get("fallbackReason")}
    return {"outAmount": q.get("uiOutAmount"), "minReceived": q.get("uiMinReceived"), "provider": q.get("provider"),
            "executionMode": q.get("executionMode"), "priceImpactPct": q.get("priceImpactPct"),
            "routeLabel": q.get("routeLabel"), "fallbackFrom": q.get("fallbackFrom"),
            "fallbackReason": q.get("fallbackReason")}


def _wrapper(pick: dict) -> dict:
    return {"symbol": pick["symbol"], "platform": pick["platform"], "contract": pick["mint"],
            "ratio": pick["tokenToShareRatio"], "tape": pick["tokenPrice"], "perShare": pick["_perShare"]}


async def build(usd: float = DEFAULT_USD) -> dict:
    hit = _cache.get(usd)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]

    m = await asyncio.to_thread(fair.qqq_move)
    move = m["move"]
    base = {"theme": basket.THEME, "sizeUsd": usd, "qqqSymbol": m["symbol"], "since": m["since"],
            "qqqMove": move, "threshold": THRESHOLD}
    if move is None or abs(move) < THRESHOLD:
        payload = {**base, "triggered": False, "viable": None, "sell": None, "buy": None, "priceImpactPct": None,
                   "reason": "no QQQ tape on both sides of the Friday close" if move is None else "move under threshold"}
        _cache[usd] = (time.monotonic(), payload)
        return payload

    snap = rwa.get_cached_snapshot()
    groups = {g["underlying"]: g for g in snap.get("groups") or []}
    slot = usd / len(basket.NAMES)

    picks = {n: basket._cheapest(groups.get(n)) for n in basket.NAMES}
    picked = [n for n in basket.NAMES if picks[n]]
    sells = await asyncio.gather(
        *(swap.quote(picks[n]["mint"], rwa.USDT, slot / picks[n]["tokenPrice"]) for n in picked),
        return_exceptions=True)
    smap = dict(zip(picked, sells))

    sell_legs, quoted_usd = [], 0.0
    for n in basket.NAMES:
        leg = {"underlying": n, "weight": 1 / len(basket.NAMES), "amountUsd": slot, "filled": False}
        if not picks[n]:
            leg["reason"] = "no tape with a share ratio" if groups.get(n) else "not in snapshot yet"
            sell_legs.append(leg)
            continue
        q = smap[n]
        leg.update(_wrapper(picks[n]), sellTokens=slot / picks[n]["tokenPrice"], **_leg_quote(q))
        if _routed(q):
            leg["filled"] = True
            quoted_usd += float(q["uiOutAmount"])
        sell_legs.append(leg)

    buy = {"underlying": DEFENSIVE, "filled": False, "amountUsd": quoted_usd}
    pick = basket._cheapest(groups.get(DEFENSIVE))
    bq = None
    if not pick:
        buy["reason"] = "no tape with a share ratio" if groups.get(DEFENSIVE) else "not in snapshot yet"
    elif quoted_usd <= 0:
        buy["reason"] = "no sell leg quoted, nothing to rotate"
    else:
        bq = await swap.quote(rwa.USDT, pick["mint"], quoted_usd)
        buy.update(_wrapper(pick), **_leg_quote(bq))
        if _routed(bq):
            buy["filled"] = True
        elif pick.get("platform") == "ondo":
            buy["reason"] = "no route quoted (Ondo RFQ needs a connected wallet for a firm quote)"

    # viable: every leg that was quoted found a route, and at least one sell + the buy did.
    quoted = [l for l in sell_legs if "tape" in l] + ([buy] if "tape" in buy else [])
    viable = bool(quoted_usd > 0 and buy["filled"] and all(l["filled"] for l in quoted))
    impacts = [abs(float(l["priceImpactPct"])) for l in quoted if l.get("priceImpactPct") not in (None, "")]
    payload = {**base, "triggered": True, "viable": viable, "session": snap.get("session"),
               "fetchedAt": snap.get("fetchedAt"), "tapeStale": bool(snap.get("tapeStale")), "from": "AI basket",
               "sell": {"legs": sell_legs, "legUsd": slot, "quotedUsd": quoted_usd,
                        "unfilledUsd": slot * sum(1 for l in sell_legs if not l["filled"])},
               "buy": buy, "priceImpactPct": max(impacts) if impacts else None,
               "note": "Quotes only, a rotation not an arb. Nothing is signed until you confirm each leg in your own wallet."}
    _cache[usd] = (time.monotonic(), payload)
    return payload
