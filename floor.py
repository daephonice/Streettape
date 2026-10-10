"""Floor watch: proposal-only floor defense on the wrappers the wallet already holds. No custody, never signs.

Floor = floor_pct x reference per-share price (default: the official mark, i.e. last cash print). Room = how far the
held per-share tape can still fall before touching it. Linear de-risk: keep = clamp(room / buffer, 0, 1); the sold share
is 1 - keep, taken from the richest per-share wrapper first and quoted into USDT.
"""
from __future__ import annotations

import asyncio

import balances
import rwa
import swap

BUFFER = 0.02  # room under which exposure starts coming off


def _leg(q) -> dict:
    if isinstance(q, Exception):
        return {"reason": f"quote failed: {type(q).__name__}"}
    if q.get("noRoute") or q.get("uiOutAmount") is None:
        return {"reason": "no route quoted", "fallbackReason": q.get("fallbackReason")}
    return {"outAmount": q["uiOutAmount"], "minReceived": q.get("uiMinReceived"), "provider": q.get("provider"),
            "executionMode": q.get("executionMode"), "priceImpactPct": q.get("priceImpactPct"),
            "routeLabel": q.get("routeLabel")}


async def watch(address: str, underlying: str, floor: float, buffer: float = BUFFER, ref: float | None = None) -> dict:
    und = underlying.upper()
    snap = rwa.get_cached_snapshot()
    base = {"underlying": und, "floor": floor, "buffer": buffer, "session": (snap.get("session") or {}).get("label")}
    group = next((g for g in snap.get("groups") or [] if (g.get("underlying") or "").upper() == und), None)
    if not group:
        return {**base, "proposal": None, "reason": "not in snapshot yet"}

    held_all = (await balances.get_balances(address)).get("holdings") or {}
    held = []
    for w in group.get("wrappers") or []:
        amt, px, ratio = held_all.get(w.get("symbol"), 0.0), w.get("tokenPrice"), w.get("tokenToShareRatio")
        if amt > 0 and px and ratio and w.get("hasTape") and w.get("mint"):
            held.append({"w": w, "amt": amt, "shares": amt * ratio, "usd": amt * px, "perShare": px / ratio})
    if not held:
        return {**base, "proposal": None, "reason": "no priced wrapper held for this underlying"}

    mark = ref or group.get("markPrice")
    if not mark or mark <= 0:
        return {**base, "proposal": None, "reason": "no official print or ref to anchor the floor"}
    shares, value = sum(h["shares"] for h in held), sum(h["usd"] for h in held)
    px = value / shares
    floor_px = floor * mark
    room = 1 - floor_px / px
    state = {**base, "reference": "ref" if ref else "official mark", "refPerShare": mark, "floorPerShare": floor_px,
             "tapePerShare": px, "valueUsd": value, "cushionUsd": value - floor_px * shares, "room": room,
             "holds": [{"symbol": h["w"]["symbol"], "amount": h["amt"], "perShare": h["perShare"]} for h in held]}
    keep = max(0.0, min(1.0, room / buffer))
    if keep >= 1:
        return {**state, "proposal": None, "reason": "cushion ok"}

    left = (1 - keep) * shares
    picks = []
    for h in sorted(held, key=lambda h: h["perShare"], reverse=True):
        if left <= 0:
            break
        take = min(h["shares"], left)
        picks.append((h, h["amt"] * take / h["shares"]))
        left -= take
    qs = await asyncio.gather(*(swap.quote(h["w"]["mint"], rwa.USDT, a, address) for h, a in picks),
                              return_exceptions=True)
    legs = [{"symbol": h["w"]["symbol"], "platform": h["w"].get("platform"), "amount": a, "usd": a * h["w"]["tokenPrice"],
             "perShare": h["perShare"], **_leg(q)} for (h, a), q in zip(picks, qs)]
    why = "floor breached" if room <= 0 else f"room {room * 100:.2f}% under the {buffer * 100:g}% buffer"
    return {**state, "proposal": {"action": "sell", "to": "USDT", "sellFraction": 1 - keep, "legs": legs, "reason": why,
                                  "note": "Quotes only. Nothing is signed until you confirm in your own wallet."}}
