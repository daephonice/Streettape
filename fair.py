"""Synthetic mark for when cash is shut.

fair = last_official_print
     x (1 + beta * move(QQQ wrapper since Friday close))
     x (1 + news_shock)

last_official_print: latest PriceSnapshot(platform="cash") mark for the
underlying (falls back to whatever markPrice board.py has live).
beta: sensitivity of this underlying's own tape to the QQQ wrapper's tape,
estimated from price_snapshots while cash was open: each name print is paired
with the nearest QQQ print inside 2 minutes, regression through origin on
log-returns between consecutive pairs, capped to +-BETA_CAP. Under 5 pairs ->
betaStatus "unestimated", beta None (fair = last print x news shock only).
move: QQQB (fallback QQQx/QQQon) tape now vs its snapshot nearest last
Friday 16:00 ET close.
news_shock: +-NEWS_SHOCK_CAP only if the underlying's latest news item is
fresh (<= NEWS_MAX_AGE_HOURS) and its text hits an earnings/halt/guidance
keyword; sign from simple positive/negative wording, else 0.

cashOpen -> synthetic mark = official mark (no adjustment): the whole point
of this module is a stand-in for when there is no live cash print.
"""
from __future__ import annotations

import bisect
import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import rwa
from database import SessionLocal
from models import PriceSnapshot

log = logging.getLogger("fair")

BETA_CAP = 3.0
BETA_LOOKBACK_HOURS = 14 * 24
NEWS_SHOCK_CAP = 0.01
NEWS_MAX_AGE_HOURS = 48
QQQ_WRAPPER_PREF = ("QQQB", "QQQx", "QQQon")

_EVENT_KEYWORDS = ("earnings", "guidance", "halt", "halted", "trading halt")
_POS_WORDS = ("beat", "beats", "raises", "raised", "record", "surge", "surges", "tops", "upgrade")
_NEG_WORDS = ("miss", "misses", "cuts", "cut guidance", "halt", "halted", "plunge", "plunges", "downgrade", "slashes")


def _friday_close_cutoff(now: datetime) -> datetime:
    """Most recent NY 16:00 close strictly before `now`, in UTC."""
    ny = now.astimezone(rwa.NY)
    close_t = ny.replace(hour=16, minute=0, second=0, microsecond=0)
    d = ny if ny > close_t else ny - timedelta(days=1)
    while d.weekday() >= 5:  # walk back to a weekday
        d -= timedelta(days=1)
    d = d.replace(hour=16, minute=0, second=0, microsecond=0)
    return d.astimezone(timezone.utc)


def _qqq_wrapper_symbol() -> str | None:
    syms = {w["symbol"].upper() for w in rwa.wrappers()}
    for s in QQQ_WRAPPER_PREF:
        if s.upper() in syms:
            return s
    return None


def _snapshot_near(db, symbol: str, target: datetime, window_hours: int = 12):
    lo, hi = target - timedelta(hours=window_hours), target + timedelta(hours=window_hours)
    row = db.execute(
        select(PriceSnapshot.token_price, PriceSnapshot.fetched_at)
        .where(PriceSnapshot.symbol == symbol, PriceSnapshot.fetched_at.between(lo, hi))
        .order_by(PriceSnapshot.fetched_at.asc())
    ).all()
    if not row:
        return None
    return min(row, key=lambda r: abs((r[1] - target).total_seconds()))[0]


def _last_cash_mark(db, underlying: str) -> float | None:
    """Latest known mark for `underlying`: prefers the dedicated cash row,
    falls back to any wrapper snapshot's mark_price (same value, just not
    yet given its own cash row) so a cold DB isn't stuck at None."""
    row = db.execute(
        select(PriceSnapshot.mark_price)
        .where(PriceSnapshot.symbol == underlying, PriceSnapshot.platform == "cash", PriceSnapshot.mark_price > 0)
        .order_by(PriceSnapshot.fetched_at.desc())
        .limit(1)
    ).first()
    if row:
        return float(row[0])
    row = db.execute(
        select(PriceSnapshot.mark_price)
        .where(PriceSnapshot.underlying == underlying, PriceSnapshot.mark_price > 0)
        .order_by(PriceSnapshot.fetched_at.desc())
        .limit(1)
    ).first()
    return float(row[0]) if row else None


PAIR_WINDOW_SECONDS = 120
MIN_PAIRS = 5
MAX_STEP_MOVE = 0.05


def _beta(db, underlying: str, qqq_symbol: str, now: datetime) -> tuple[float | None, int]:
    """(beta, n_pairs). Each name print is paired with the QQQ print nearest
    in time inside PAIR_WINDOW_SECONDS; only cash-open pairs count. Regression
    through the origin on log-returns between consecutive pairs:
    beta = sum(x*y) / sum(x*x), capped to +-BETA_CAP. Fewer than MIN_PAIRS
    pairs -> (None, n): not estimated. Empty denominator -> (0.0, n)."""
    since = now - timedelta(hours=BETA_LOOKBACK_HOURS)

    def _series(sym):
        rows = db.execute(
            select(PriceSnapshot.token_price, PriceSnapshot.fetched_at)
            .where(PriceSnapshot.symbol == sym, PriceSnapshot.platform != "cash",
                   PriceSnapshot.fetched_at >= since, PriceSnapshot.token_price > 0)
            .order_by(PriceSnapshot.fetched_at.asc())
        ).all()
        out = []
        for p, t in rows:
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            out.append((t, float(p)))
        return out

    cands = [w["symbol"] for w in rwa.wrappers() if w["underlying"] == underlying]
    if not cands:
        return None, 0
    # Use the wrapper with the liveliest tape: some wrappers (e.g. the x ones)
    # sit on a single flat price, which regresses to a fake beta of 0.
    cnt = dict(db.execute(
        select(PriceSnapshot.symbol, func.count(func.distinct(PriceSnapshot.token_price)))
        .where(PriceSnapshot.symbol.in_(cands), PriceSnapshot.platform != "cash",
               PriceSnapshot.fetched_at >= since, PriceSnapshot.token_price > 0)
        .group_by(PriceSnapshot.symbol)
    ).all())
    und_wrapper = max(cands, key=lambda c: cnt.get(c, 0))
    if cnt.get(und_wrapper, 0) < 3:
        return None, 0
    xs = _series(qqq_symbol)
    ys = _series(und_wrapper)
    if not xs or not ys:
        return None, 0

    xt = [t for t, _ in xs]
    pairs = []  # (t, qqq_price, name_price)
    used = set()
    for t, y in ys:
        i = bisect.bisect_left(xt, t)
        cands = [j for j in (i - 1, i) if 0 <= j < len(xs)]
        j = min(cands, key=lambda k: abs((xt[k] - t).total_seconds()))
        if abs((xt[j] - t).total_seconds()) > PAIR_WINDOW_SECONDS or j in used:
            continue
        if not rwa.session_now(t)["cashOpen"]:
            continue
        used.add(j)
        pairs.append((t, xs[j][1], y))

    n = len(pairs)
    if n < MIN_PAIRS:
        return None, n

    num = den = 0.0
    for (_, x0, y0), (_, x1, y1) in zip(pairs, pairs[1:]):
        dx, dy = math.log(x1 / x0), math.log(y1 / y0)
        if abs(dx) > MAX_STEP_MOVE or abs(dy) > MAX_STEP_MOVE:
            continue  # bad print / stale-pool jump, not a market move
        num += dx * dy
        den += dx * dx
    if den <= 1e-12:
        return 0.0, n
    return max(-BETA_CAP, min(BETA_CAP, num / den)), n


def _news_shock(underlying: str) -> float:
    import news
    now = datetime.now(timezone.utc)
    for item in news.get_news():
        if item.get("underlying") != underlying:
            continue
        try:
            at = datetime.fromisoformat(item["publishedAt"])
        except Exception:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if now - at > timedelta(hours=NEWS_MAX_AGE_HOURS):
            return 0.0
        body = (item.get("body") or "").lower()
        if not any(k in body for k in _EVENT_KEYWORDS):
            return 0.0
        if any(w in body for w in _NEG_WORDS):
            return -NEWS_SHOCK_CAP
        if any(w in body for w in _POS_WORDS):
            return NEWS_SHOCK_CAP
        return 0.0
    return 0.0


def synthetic_marks(underlyings: list[str], cash_open: bool) -> dict[str, dict]:
    """{underlying: {"fairPrice", "beta", "betaPairs", "betaStatus", "indexMove", "newsShock"}}. Empty
    dict for anything with no last official print to anchor on. When
    cash_open, fairPrice mirrors the last official print (no adjustment)."""
    out: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    qqq_symbol = _qqq_wrapper_symbol()
    db = SessionLocal()
    try:
        friday_close = _friday_close_cutoff(now)
        qqq_now = _snapshot_near(db, qqq_symbol, now, window_hours=2) if qqq_symbol else None
        qqq_then = _snapshot_near(db, qqq_symbol, friday_close) if qqq_symbol else None
        index_move = (qqq_now / qqq_then - 1) if qqq_now and qqq_then else 0.0

        for und in underlyings:
            last_print = _last_cash_mark(db, und)
            if not last_print:
                continue
            if cash_open:
                out[und] = {"fairPrice": last_print, "lastPrint": last_print, "beta": None, "betaPairs": None,
                            "betaStatus": "cash-open", "indexMove": None, "newsShock": None}
                continue
            beta, pairs = _beta(db, und, qqq_symbol, now) if qqq_symbol else (None, 0)
            shock = _news_shock(und)
            status = "estimated" if beta is not None else "unestimated"
            move = index_move if beta is not None else 0.0
            fair = last_print * (1 + (beta or 0.0) * move) * (1 + shock)
            out[und] = {"fairPrice": fair, "lastPrint": last_print, "beta": beta, "betaPairs": pairs,
                        "betaStatus": status, "indexMove": move, "newsShock": shock}
    except Exception:
        log.warning("fair: synthetic_marks failed", exc_info=True)
    finally:
        db.close()
    return out
