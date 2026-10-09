"""Session book: what the tape did while US cash was shut.

One row per underlying for one closed session: Friday official print, each
wrapper's tape high/low/first/last inside the shut window, the widest gap vs
the print, the new cash mark at the open and minutes until the tape came back
within RECONVERGE_PCT of it. Built only from price_snapshots + the live cache
(for the thin flag). No row is invented for a name with no tape.
"""
from __future__ import annotations

import logging
import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import fair
import rwa
from database import SessionLocal
from models import PriceSnapshot, ProposalProof

log = logging.getLogger("sessionbook")

RECONVERGE_PCT = 0.005
POST_OPEN_HOURS = 6.5          # reconvergence search stops at the next cash close
NO_OFFICIAL = {"SPCX"}         # SpaceX has no official print: never shown in the official column
CASH_LOOKBACK_DAYS = 4


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def next_open(closed_at: datetime) -> datetime:
    """Next 09:30 ET cash open after `closed_at`, skipping Saturday and Sunday, in UTC."""
    d = closed_at.astimezone(rwa.NY) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(timezone.utc)


def resolve_close(closed_at: str | None = None, now: datetime | None = None) -> datetime:
    """Default: most recent NY 16:00 strictly before now. With `closed_at`
    (ISO datetime or YYYY-MM-DD): the latest close at or before it."""
    now = now or datetime.now(timezone.utc)
    if not closed_at:
        return fair._friday_close_cutoff(now)
    s = closed_at.strip()
    try:
        if len(s) == 10:
            dt = datetime.fromisoformat(s).replace(hour=23, minute=59, tzinfo=rwa.NY)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=rwa.NY)
    except ValueError:
        raise ValueError("closed_at must be ISO 8601 (e.g. 2026-10-02T16:00:00-04:00) or YYYY-MM-DD")
    return fair._friday_close_cutoff(dt.astimezone(timezone.utc) + timedelta(seconds=1))


def _thin_symbols() -> set[str]:
    try:
        return {t["symbol"] for t in rwa.get_cached_snapshot().get("tokens", []) if t.get("thin")}
    except Exception:
        return set()


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.2f}%"


def build(closed_at: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    close = resolve_close(closed_at, now)
    open_ = next_open(close)
    search_end = open_ + timedelta(hours=POST_OPEN_HOURS)
    complete = now >= open_
    thin = _thin_symbols()
    wr = rwa.wrappers()
    names = {u["underlying"]: u["name"] for u in rwa.UNIVERSE}
    ny = lambda d: d.astimezone(rwa.NY).isoformat()  # noqa: E731

    db = SessionLocal()
    try:
        cash_rows = db.execute(
            select(PriceSnapshot.symbol, PriceSnapshot.mark_price, PriceSnapshot.fetched_at)
            .where(PriceSnapshot.platform == "cash", PriceSnapshot.mark_price > 0,
                   PriceSnapshot.fetched_at >= close - timedelta(days=CASH_LOOKBACK_DAYS),
                   PriceSnapshot.fetched_at <= search_end)
            .order_by(PriceSnapshot.fetched_at.asc())
        ).all()
        wrap_rows = db.execute(
            select(PriceSnapshot.symbol, PriceSnapshot.underlying, PriceSnapshot.token_price,
                   PriceSnapshot.mark_price, PriceSnapshot.fetched_at)
            .where(PriceSnapshot.platform != "cash", PriceSnapshot.token_price > 0,
                   PriceSnapshot.fetched_at >= close - timedelta(days=CASH_LOOKBACK_DAYS),
                   PriceSnapshot.fetched_at <= search_end)
            .order_by(PriceSnapshot.fetched_at.asc())
        ).all()
    finally:
        db.close()

    cash_by: dict[str, list] = {}
    for sym, mark, at in cash_rows:
        cash_by.setdefault(sym, []).append((_aware(at), float(mark)))
    pre_wrap: dict[str, tuple] = {}     # underlying -> (at, mark) latest wrapper mark <= close
    tape: dict[str, list] = {}          # wrapper symbol -> [(at, tape, mark)] inside close..search_end
    for sym, und, tp, mk, at in wrap_rows:
        at = _aware(at)
        if at <= close:
            if und and mk and mk > 0 and (und not in pre_wrap or at > pre_wrap[und][0]):
                pre_wrap[und] = (at, float(mk))
        else:
            tape.setdefault(sym, []).append((at, float(tp), float(mk or 0)))

    rows, no_friday, no_tape = [], [], []
    for und in dict.fromkeys(w["underlying"] for w in wr):
        # Friday official
        official = source = None
        if und not in NO_OFFICIAL:
            prior = [m for at, m in cash_by.get(und, []) if at <= close]
            if prior:
                official, source = prior[-1], "cash"
            elif und in pre_wrap:
                official, source = pre_wrap[und][1], "fallback"
            else:
                no_friday.append(und)

        # New cash mark at the open
        open_mark = next((m for at, m in cash_by.get(und, [])
                          if open_ <= at <= open_ + timedelta(hours=2)), None) if und not in NO_OFFICIAL else None

        wrappers = []
        for w in (x for x in wr if x["underlying"] == und):
            allr = tape.get(w["symbol"], [])
            shut = [r for r in allr if r[0] <= open_]
            if not shut:
                no_tape.append(w["symbol"])
                continue
            tps = [r[1] for r in shut]
            gap = None
            if official:
                g = max(shut, key=lambda r: abs(r[1] / official - 1))
                gap = {"gap": g[1] / official - 1, "at": ny(g[0]), "tape": g[1]}
            reconv = {"minutes": None, "status": "awaiting open" if now < open_ else "still open"}
            for at, tp, mk in allr:
                if at > open_ and mk > 0 and abs(tp / mk - 1) <= RECONVERGE_PCT and und not in NO_OFFICIAL:
                    reconv = {"minutes": round((at - open_).total_seconds() / 60, 1), "status": "converged", "at": ny(at)}
                    break
            wrappers.append({
                "symbol": w["symbol"], "platform": w["platform"], "thin": w["symbol"] in thin,
                "minTape": min(tps), "maxTape": max(tps), "firstTape": shut[0][1], "lastTape": shut[-1][1],
                "maxGap": gap, "reconverge": reconv, "snapshots": len(shut),
            })
        if not wrappers:
            continue  # no tape for this name in the window: no row

        live = [x for x in wrappers if not x["thin"] and x["maxGap"]]
        widest = None
        if live:
            b = max(live, key=lambda x: abs(x["maxGap"]["gap"]))
            widest = {"wrapper": b["symbol"], **b["maxGap"]}
        conv = [x["reconverge"]["minutes"] for x in wrappers
                if not x["thin"] and x["reconverge"]["status"] == "converged"]
        if conv:
            reconv_u = {"minutes": min(conv), "status": "converged"}
        else:
            reconv_u = {"minutes": None, "status": "awaiting open" if now < open_ else "still open"}
        rows.append({
            "underlying": und, "name": names.get(und, und),
            "official": official, "officialSource": source, "mondayOpen": open_mark,
            "widest": widest, "reconverge": reconv_u, "wrappers": wrappers,
        })

    rows.sort(key=lambda r: abs(r["widest"]["gap"]) if r["widest"] else -1, reverse=True)
    top = next((r for r in rows if r["widest"]), None)
    widest_line = None
    if top:
        w = top["widest"]
        widest_line = (f"{top['underlying']} · official ${top['official']:,.2f} · {w['wrapper']} "
                       f"${w['tape']:,.2f} · {_pct(w['gap'])}")
    return {
        "closedAt": ny(close), "opensAt": ny(open_), "complete": complete,
        "label": "WEEKEND" if close.astimezone(rwa.NY).weekday() == 4 else "AFTER-HOURS",
        "reconvergePct": RECONVERGE_PCT,
        "rows": rows, "widest": ({"underlying": top["underlying"], **top["widest"], "line": widest_line} if top else None),
        "meta": {"snapshots": sum(1 for v in tape.values() for r in v if r[0] <= open_), "cashRows": len(cash_rows),
                 "noFridayCash": no_friday, "noTapeWrappers": no_tape,
                 "fallbackOfficial": [r["underlying"] for r in rows if r["officialSource"] == "fallback"]},
    }


def widest_line() -> dict | None:
    """Widest row of the latest session book, or None. Used by the homepage."""
    try:
        return build().get("widest")
    except Exception:
        log.warning("sessionbook: widest failed", exc_info=True)
        return None


_floor_cache: dict = {"at": 0.0, "book": None}
FLOOR_TTL = 300.0


def _last_weekend_close(now: datetime) -> datetime:
    """Most recent Friday 16:00 NY whose Monday open has already passed."""
    d = now.astimezone(rwa.NY).replace(hour=16, minute=0, second=0, microsecond=0)
    while True:
        if d.weekday() == 4 and d.astimezone(timezone.utc) < now and next_open(d.astimezone(timezone.utc)) <= now:
            return d.astimezone(timezone.utc)
        d -= timedelta(days=1)


def floor_quote(underlying: str, notional: float = 1000.0, floor: float = 0.03) -> dict:
    """Quote-only floor from last weekend's session book: what a -`floor` line under
    `notional` of `underlying` would have paid Friday print -> Monday open (or the
    weekend tape low when no Monday cash mark is stored). No premium, no contract."""
    import time
    und = underlying.upper()
    now = datetime.now(timezone.utc)
    if time.time() - _floor_cache["at"] > FLOOR_TTL or _floor_cache["book"] is None:
        close = _last_weekend_close(now)
        _floor_cache.update(at=time.time(), book=build(close.isoformat()))
    book = _floor_cache["book"]
    row = next((r for r in book["rows"] if r["underlying"] == und), None)
    if not row or not row["official"]:
        return {"underlying": und, "quote": None}
    if row["mondayOpen"]:
        basis, basis_kind = row["mondayOpen"], "open"
    else:
        lows = [w["minTape"] for w in row["wrappers"] if not w["thin"]]
        if not lows:
            return {"underlying": und, "quote": None}
        basis, basis_kind = min(lows), "tapeLow"
    move = basis / row["official"] - 1
    payout = notional * max(0.0, -move - floor)
    return {"underlying": und, "quote": {
        "notional": notional, "floor": floor, "official": row["official"], "basis": basis,
        "basisKind": basis_kind, "move": move, "payout": round(payout, 2),
        "closedAt": book["closedAt"], "opensAt": book["opensAt"]}}


def record_proposal(q: dict) -> dict | None:
    """Hash a rotate proposal (underlying, rich/cheap legs, net bps, quote ids, time) and
    store it. Proof of proposal only: nothing is signed, anchored or broadcast."""
    try:
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = {
            "underlying": q["underlying"], "rich": q["richSymbol"], "cheap": q["cheapSymbol"],
            "netBps": round(float(q["netBps"]), 2), "sizeUsd": q.get("sizeUsd"),
            "quoteIds": [(q.get(k) or {}).get("quoteId") or (q.get(k) or {}).get("requestId")
                         for k in ("sellLeg", "buyLeg")],
            "legs": [{"provider": (q.get(k) or {}).get("provider"), "route": (q.get(k) or {}).get("routeLabel"),
                      "out": (q.get(k) or {}).get("uiOutAmount"), "min": (q.get(k) or {}).get("uiMinReceived")}
                     for k in ("sellLeg", "buyLeg")],
            "at": at,
        }
        payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
        h = "0x" + hashlib.sha256(payload.encode()).hexdigest()
        with SessionLocal() as db:
            db.add(ProposalProof(hash=h, underlying=body["underlying"], rich_symbol=body["rich"],
                                 cheap_symbol=body["cheap"], net_bps=body["netBps"], payload=payload))
            db.commit()
        return {"hash": h, "at": at}
    except Exception:
        log.warning("sessionbook: record_proposal failed", exc_info=True)
        return None


def get_proposal(h: str) -> dict | None:
    with SessionLocal() as db:
        r = db.execute(select(ProposalProof).where(ProposalProof.hash == h)).scalar_one_or_none()
        if not r:
            return None
        return {"hash": r.hash, "payload": r.payload, "createdAt": r.created_at.isoformat(),
                "note": "sha256(payload) == hash. Proof of proposal, not a trade."}
