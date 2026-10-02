"""StreetTape board: Yahoo mark + GeckoTerminal tape.

Gecko never blocks on Yahoo: the snapshot is published as soon as tape
prices land (mark null), then patched in place once marks arrive. Yahoo
misses fall back query2 -> query1 -> last known mark in price_snapshots,
per-ticker, all run concurrently so one slow/dead host can't stall the rest.
"""
from __future__ import annotations

import os
import time
import asyncio
import logging

import httpx
from sqlalchemy import select

import rwa
import rwa_api
import fair
import devlog
from logos import logo_for, group_logo
from database import SessionLocal
from models import PriceSnapshot, SessionGap
from datetime import datetime, timezone

log = logging.getLogger("board")
REFRESH_SECONDS = int(os.getenv("BOARD_REFRESH_SECONDS", "45"))
GECKO = "https://api.geckoterminal.com/api/v2"
YAHOO_HOSTS = (
    "https://query2.finance.yahoo.com/v8/finance/chart",
    "https://query1.finance.yahoo.com/v8/finance/chart",
)
YAHOO_TIMEOUT = 3.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_task = None


def _last_known_mark(tkr: str) -> float | None:
    und = next((u["underlying"] for u in rwa.UNIVERSE if u.get("yahoo") == tkr), None)
    if not und:
        return None
    db = SessionLocal()
    try:
        row = db.execute(
            select(PriceSnapshot.mark_price)
            .where(PriceSnapshot.underlying == und, PriceSnapshot.mark_price > 0)
            .order_by(PriceSnapshot.fetched_at.desc())
            .limit(1)
        ).first()
        return float(row[0]) if row and row[0] else None
    except Exception:
        log.warning("board: last-known-mark lookup failed for %s", tkr, exc_info=True)
        return None
    finally:
        db.close()


async def _yahoo_one(client: httpx.AsyncClient, tkr: str) -> tuple[str, float | None]:
    for host in YAHOO_HOSTS:
        url = f"{host}/{tkr}"
        try:
            with devlog.timed() as t:
                resp = await client.get(url, params={"interval": "1d", "range": "5d"}, timeout=YAHOO_TIMEOUT)
            resp.raise_for_status()
            result = (resp.json().get("chart") or {}).get("result") or []
            if not result:
                devlog.log_call(what=f"mark fetch {tkr}", url=url, status=resp.status_code, ms=t.ms,
                                 expected="chart.result[0].meta.regularMarketPrice", actual="empty result array")
                continue
            meta = result[0].get("meta") or {}
            px = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
            devlog.log_call(what=f"mark fetch {tkr}", url=url, status=resp.status_code, ms=t.ms,
                             expected="regularMarketPrice present", actual=f"price={px}")
            if px:
                return tkr, float(px)
        except httpx.TimeoutException:
            devlog.log_call(what=f"mark fetch {tkr}", url=url, status="timeout", ms=YAHOO_TIMEOUT * 1000,
                             error=f"timed out after {YAHOO_TIMEOUT}s")
            log.warning("board: yahoo %s timed out on %s", tkr, host)
        except Exception as e:
            devlog.log_call(what=f"mark fetch {tkr}", url=url, status="exception", ms=0, error=str(e))
            log.warning("board: yahoo %s failed on %s", tkr, host, exc_info=True)
    fallback = await asyncio.to_thread(_last_known_mark, tkr)
    return tkr, fallback


async def _yahoo_marks(client: httpx.AsyncClient, skip_tickers: set | None = None) -> dict:
    skip = skip_tickers or set()
    tickers = sorted({u["yahoo"] for u in rwa.UNIVERSE if u.get("yahoo") and u["yahoo"] not in skip})
    results = await asyncio.gather(*(_yahoo_one(client, tkr) for tkr in tickers), return_exceptions=True)
    out = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        tkr, px = r
        if px:
            out[tkr] = px
    return out


STALE_DEVIATION = 0.15  # Gecko tape >15% off the official mark is a dead pool, not a gap
THIN_LIQUIDITY_USD = 500.0  # a Gecko tape from a pool below this is not a tradable price


def _gecko_row(attr: dict) -> dict:
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    px = attr.get("price_usd")
    return {
        "price": float(px) if px else None,
        "image": attr.get("image_url"),
        "liquidity": _f(attr.get("total_reserve_in_usd")),
        "vol24": _f((attr.get("volume_usd") or {}).get("h24")),
    }


_last_tape: dict = {}   # addr(lower) -> {"price": float, "image": str|None} — last good Gecko read
_tape_stale = False     # true when this refresh cycle hit a 429 and fell back to _last_tape


async def _one_gecko(client, addr: str):
    url = f"{GECKO}/networks/bsc/tokens/{addr}"
    with devlog.timed() as t:
        resp = await client.get(url)
    if resp.status_code == 429:
        await asyncio.sleep(2)
        with devlog.timed() as t2:
            resp = await client.get(url)
        if resp.status_code == 429:
            devlog.log_call(what=f"rwa/price tape {addr}", url=url, status=429, ms=t.ms + t2.ms,
                             expected="200 with data.attributes.price_usd", actual="429 rate limited (both tries)")
            return "RATE_LIMITED"
    if resp.status_code != 200:
        devlog.log_call(what=f"rwa/price tape {addr}", url=url, status=resp.status_code, ms=t.ms,
                         expected="200 with data.attributes.price_usd", body=resp.text)
        return None
    attr = resp.json()["data"]["attributes"]
    px = attr.get("price_usd")
    devlog.log_call(what=f"rwa/price tape {addr}", url=url, status=resp.status_code, ms=t.ms,
                     expected="data.attributes.price_usd", actual=f"price_usd={px}")
    return _gecko_row(attr)


async def _gecko_prices(client, addrs: list | None = None) -> dict:
    global _tape_stale
    out = {}
    rate_limited = False
    addrs = addrs if addrs is not None else [w["address"] for w in rwa.wrappers() if w.get("address")]
    for i in range(0, len(addrs), 5):
        chunk = addrs[i:i + 5]
        try:
            resp = await client.get(f"{GECKO}/networks/bsc/tokens/multi/{','.join(chunk)}")
            if resp.status_code == 429:
                rate_limited = True
            elif resp.status_code == 200:
                data = resp.json().get("data") or []
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    attr = item.get("attributes") or {}
                    addr = (attr.get("address") or "").lower()
                    out[addr] = _gecko_row(attr)
                await asyncio.sleep(0.2)
                continue
        except Exception:
            log.info("board: gecko multi miss", exc_info=True)
        for a in chunk:
            try:
                row = await _one_gecko(client, a)
                if row == "RATE_LIMITED":
                    rate_limited = True
                elif row:
                    out[a.lower()] = row
            except Exception:
                log.info("board: gecko %s skipped", a, exc_info=True)
            await asyncio.sleep(0.2)

    # Fall back to last good read per-address on a 429; only mark stale if we
    # actually had to reuse something (an address with no prior tape stays null).
    if rate_limited:
        used_fallback = False
        for a in addrs:
            key = a.lower()
            if key not in out and key in _last_tape:
                out[key] = _last_tape[key]
                used_fallback = True
        _tape_stale = used_fallback
    else:
        _tape_stale = False
    _last_tape.update(out)
    return out


def _group(tokens):
    by = {}
    for t in tokens:
        by.setdefault(t["underlying"], []).append(t)
    groups = []
    for und, rows in by.items():
        priced = [r for r in rows if r.get("tokenPrice") and not r.get("thin")]
        cheapest = min(priced, key=lambda r: r["tokenPrice"]) if priced else None
        richest = max(priced, key=lambda r: r["tokenPrice"]) if priced else None
        groups.append({
            "underlying": und,
            "name": rows[0]["name"],
            "logo": group_logo(und),
            "markPrice": rows[0].get("markPrice"),
            "wrappers": rows,
            "cheapest": cheapest["symbol"] if cheapest else None,
            "richest": richest["symbol"] if richest else None,
            "crossSpread": (
                (richest["tokenPrice"] / cheapest["tokenPrice"] - 1)
                if cheapest and richest and cheapest["tokenPrice"] else None
            ),
            "absPremium": max((abs(r["premium"] or 0) for r in rows), default=0),
            "fairPrice": rows[0].get("fairPrice"),
            "beta": rows[0].get("fairBeta"),
            "indexMove": rows[0].get("fairIndexMove"),
            "newsShock": rows[0].get("fairNewsShock"),
            "premiumToOfficial": (
                rwa.premium(cheapest["tokenPrice"], rows[0].get("markPrice"))
                if cheapest and rows[0].get("markPrice") else None
            ),
            "premiumToFair": (
                rwa.premium(cheapest["tokenPrice"], rows[0]["fairPrice"])
                if cheapest and rows[0].get("fairPrice") else None
            ),
            "noYahoo": all(r.get("noYahoo") for r in rows),
        })
    groups.sort(key=lambda g: g["absPremium"], reverse=True)
    return groups


def _build_tokens(tapes: dict, marks: dict, official: dict | None = None, fair_marks: dict | None = None) -> list[dict]:
    official = official or {}
    fair_marks = fair_marks or {}
    tokens = []
    for w in rwa.wrappers():
        has_addr = bool(w.get("address"))
        addr_l = w["address"].lower() if has_addr else None
        off = official.get(addr_l) or {} if has_addr else {}
        tape = (tapes.get(addr_l) if has_addr else None) or {}
        token_price = off.get("price") if off.get("price") is not None else tape.get("price")
        off_mark = off.get("markPrice")
        off_mark = off_mark if isinstance(off_mark, (int, float)) and off_mark > 0 else None
        mark = off_mark if off_mark is not None else (marks.get(w["yahoo"]) if w.get("yahoo") else None)
        multiplier = off.get("multiplier") if off.get("multiplier") is not None else w.get("multiplier")
        fm = fair_marks.get(w["underlying"]) or {}
        last_print_used = False
        if mark is None and w.get("yahoo") and fm.get("lastPrint"):
            mark = fm["lastPrint"]  # last stored cash print; live fetch missed
            last_print_used = True
        prem = rwa.premium(token_price, mark) if token_price and mark else None
        fair_price = fm.get("fairPrice")
        prem_fair = rwa.premium(token_price, fair_price) if token_price and fair_price else None
        liq = tape.get("liquidity")
        thin = bool(has_addr and liq is not None and liq < THIN_LIQUIDITY_USD)  # RWA Data tokenPrice inherits dead-pool prices too
        if has_addr and prem is not None and abs(prem) > STALE_DEVIATION:
            thin = True  # stale pool price, whether it came from RWA Data or Gecko (METAx: RWA Data tokenPrice was the dead pool)
        if thin:
            prem = prem_fair = None  # a stale thin-pool price is not a real gap
        # No contract for this wrapper (e.g. AAPLB pre-launch): never a Buy,
        # never a Trade link. hasTape is the single flag templates/JS gate on.
        tokens.append({
            "symbol": w["symbol"],
            "name": w["name"],
            "underlying": w["underlying"],
            "platform": w["platform"],
            "mint": w["address"],
            "address": w["address"],
            "hasTape": has_addr,
            "image": (logo_for(w["symbol"], w["underlying"]) if w["symbol"].upper() in ("AAPLB", "AMDX") else (tape.get("image") or logo_for(w["symbol"], w["underlying"]))),
            "tokenPrice": token_price if has_addr else None,
            "markPrice": mark,
            "noYahoo": not w.get("yahoo"),
            "premium": prem if has_addr else None,
            "status": "thin" if thin else (rwa.premium_status(prem) if has_addr else "flat"),
            "thin": thin,
            "liquidityUsd": liq if has_addr else None,
            "description": (
                f"{w['name']} tokenized equity on BNB Chain via {w['platform']}. Economic exposure only."
                if has_addr else f"{w['name']} has no confirmed {w['platform']} wrapper yet."
            ),
            "url": f"https://pancakeswap.finance/swap?chain=bsc&outputCurrency={w['address']}" if has_addr else None,
            "multiplier": multiplier,
            "tokenToShareRatio": rwa_api.get_ratio(w["address"]) if has_addr else None,
            "markSource": "binance" if off_mark is not None else (("last-print" if last_print_used else "yahoo") if mark else None),
            "fairPrice": fair_price,
            "fairBeta": fm.get("beta") if fair_price else None,
            "fairIndexMove": fm.get("indexMove") if fair_price else None,
            "fairNewsShock": fm.get("newsShock") if fair_price else None,
            "premiumToOfficial": prem if has_addr else None,
            "premiumToFair": prem_fair if has_addr else None,
        })
    # Sibling guard (needs no mark, so it also holds during the first publish before
    # Yahoo lands): with 3+ priced wrappers of one underlying, a tape >15% off the
    # median is a dead pool.
    import statistics
    by_und: dict[str, list[dict]] = {}
    for t in tokens:
        if t["tokenPrice"] and not t["thin"]:
            by_und.setdefault(t["underlying"], []).append(t)
    for rows in by_und.values():
        if len(rows) < 3:
            continue
        med = statistics.median(r["tokenPrice"] for r in rows)
        for r in rows:
            if abs(r["tokenPrice"] / med - 1) > STALE_DEVIATION:
                r.update({"thin": True, "status": "thin", "premium": None,
                          "premiumToOfficial": None, "premiumToFair": None})
    tokens.sort(key=lambda t: abs(t["premium"] or 0), reverse=True)
    return tokens


CATALOG_REFRESH_SECONDS = int(os.getenv("BOARD_CATALOG_REFRESH_SECONDS", "1800"))  # 30 min
_last_catalog_sync = 0.0


async def _sync_catalog():
    """Fold RWA Data's live listing into rwa.UNIVERSE. Runs on its own slow
    cadence (catalog changes rarely) — separate from the per-cycle price
    refresh. No-ops when RWA Data is unset/parked (get_dynamic_universe
    returns None), leaving the static seed untouched."""
    global _last_catalog_sync
    now = time.monotonic()
    if now - _last_catalog_sync < CATALOG_REFRESH_SECONDS:
        return
    _last_catalog_sync = now
    discovered = await rwa_api.get_dynamic_universe()
    if discovered:
        rwa.merge_dynamic(discovered)


async def build_snapshot():
    """RWA Data (official mark + official tape) tried first per wrapper address.
    Any address it didn't cover this cycle (unset key, parked, timeout, miss)
    falls through to the existing Gecko tape + Yahoo mark path, published
    immediately with marks null then patched in place once Yahoo lands —
    unchanged from before, so a dead Yahoo/RWA Data never blocks rows."""
    await _sync_catalog()
    pairs = [(u["underlying"], next(w["address"] for w in u["wrappers"] if w.get("address")))
             for u in rwa.UNIVERSE if any(w.get("address") for w in u["wrappers"])]
    asyncio.create_task(rwa_api.refresh_mcaps(pairs))
    asyncio.create_task(rwa_api.refresh_ratios(
        [w["address"] for w in rwa.wrappers() if w.get("address")]))

    addrs = [w["address"] for w in rwa.wrappers() if w.get("address")]
    official = await rwa_api.get_official_snapshot(addrs) or {}

    # Only skip a ticker's Yahoo fetch if every wrapper under that underlying
    # already got an official mark — a partially-covered underlying still
    # needs Yahoo for its uncovered wrappers' premium math.
    covered_und = {
        u["underlying"] for u in rwa.UNIVERSE
        if all((((official.get(w["address"].lower()) or {}).get("markPrice") or 0) > 0) for w in u["wrappers"] if w.get("address"))
        and any(w.get("address") for w in u["wrappers"])
    }
    skip_tickers = {u["yahoo"] for u in rwa.UNIVERSE if u["underlying"] in covered_und and u.get("yahoo")}

    cash_open = rwa.session_now()["cashOpen"]
    underlyings = [u["underlying"] for u in rwa.UNIVERSE]
    fair_marks = await asyncio.to_thread(fair.synthetic_marks, underlyings, cash_open)

    async with httpx.AsyncClient(timeout=6, headers=HEADERS) as client:
        tapes = await _gecko_prices(client, addrs)  # all wrappers: liquidity is needed even when RWA Data supplied the price
        tokens = _build_tokens(tapes, {}, official, fair_marks)
        snap = rwa.set_cached_snapshot(tokens, _group(tokens), tape_stale=_tape_stale)

        marks = await _yahoo_marks(client, skip_tickers)

    tokens = _build_tokens(tapes, marks, official, fair_marks)
    snap = rwa.set_cached_snapshot(tokens, _group(tokens), tape_stale=_tape_stale)
    _persist(tokens)
    if not cash_open:
        await asyncio.to_thread(record_session_gaps, tokens)
    return snap


def _persist(rows):
    db = SessionLocal()
    try:
        # Wrapper snapshots
        for row in rows:
            if not row.get("tokenPrice"):
                continue
            db.add(PriceSnapshot(
                symbol=row["symbol"],
                token_price=row["tokenPrice"],
                mark_price=row.get("markPrice") or 0.0,
                premium=row.get("premium"),
                platform=row.get("platform"),
                underlying=row.get("underlying"),
            ))
        # Cash mark snapshots — one row per underlying where we have a mark
        seen_und = set()
        for row in rows:
            und = row.get("underlying")
            mark = row.get("markPrice")
            if und and mark and und not in seen_und:
                seen_und.add(und)
                db.add(PriceSnapshot(
                    symbol=und,
                    token_price=mark,
                    mark_price=mark,
                    premium=0.0,
                    platform="cash",
                    underlying=und,
                ))
        db.commit()
    except Exception:
        log.warning("board: persist failed", exc_info=True)
        db.rollback()
    finally:
        db.close()


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def record_session_gaps(tokens, now=None):
    """While cash is shut: per underlying keep the wrapper with the largest
    |premium| vs the last print, one row per (underlying, close). Skips thin
    and unpriced rows. Never called while cash is open."""
    now = now or datetime.now(timezone.utc)
    sess = rwa.session_now(now)
    if sess["cashOpen"]:
        return
    closed_at = fair._friday_close_cutoff(now)
    ny_now, ny_close = now.astimezone(rwa.NY), closed_at.astimezone(rwa.NY)
    label = "WEEKEND" if (sess["label"] == "WEEKEND" or (ny_close.weekday() == 4 and ny_now.weekday() == 0)) else "AFTER-HOURS"
    best = {}
    for t in tokens:
        if not t.get("hasTape") or t.get("thin"):
            continue
        tape, mark, prem = t.get("tokenPrice"), t.get("markPrice"), t.get("premium")
        if not tape or not mark or prem is None:
            continue
        cur = best.get(t["underlying"])
        if cur is None or abs(prem) > abs(cur["premium"]):
            best[t["underlying"]] = {"wrapper": t["symbol"], "tape": tape, "mark": mark, "premium": prem}
    if not best:
        return
    db = SessionLocal()
    try:
        for und, b in best.items():
            row = db.execute(
                select(SessionGap).where(SessionGap.underlying == und, SessionGap.closed_at == closed_at)
            ).scalar_one_or_none()
            if row is None:
                db.add(SessionGap(underlying=und, session_label=label, closed_at=closed_at,
                                  print_price=b["mark"], wrapper=b["wrapper"], tape_price=b["tape"],
                                  premium=b["premium"], seen_at=now))
            elif abs(b["premium"]) > abs(row.premium):
                row.session_label, row.print_price, row.wrapper = label, b["mark"], b["wrapper"]
                row.tape_price, row.premium, row.seen_at = b["tape"], b["premium"], now
        db.commit()
    except Exception:
        log.warning("board: session gap write failed", exc_info=True)
        db.rollback()
    finally:
        db.close()


def last_sessions() -> list[dict]:
    """Latest cash-shut row per underlying, widest gap first."""
    db = SessionLocal()
    try:
        rows = db.execute(select(SessionGap).order_by(SessionGap.closed_at.desc(), SessionGap.seen_at.desc())).scalars().all()
    except Exception:
        log.warning("board: session gap read failed", exc_info=True)
        return []
    finally:
        db.close()
    seen, out = set(), []
    for r in rows:
        if r.underlying in seen:
            continue
        seen.add(r.underlying)
        closed = _aware(r.closed_at).astimezone(rwa.NY)
        at = _aware(r.seen_at).astimezone(rwa.NY)
        out.append({
            "underlying": r.underlying,
            "sessionLabel": r.session_label,
            "closedAt": _aware(r.closed_at).isoformat(),
            "printPrice": r.print_price,
            "wrapper": r.wrapper,
            "tapePrice": r.tape_price,
            "premium": r.premium,
            "seenAt": _aware(r.seen_at).isoformat(),
            "line": (f"{r.underlying} · {closed.strftime('%A')} print ${r.print_price:,.2f} · "
                     f"{r.wrapper} ${r.tape_price:,.2f} · {r.premium * 100:+.1f}% · "
                     f"{at.strftime('%A %H:%M')} ET"),
        })
    out.sort(key=lambda r: abs(r["premium"]), reverse=True)
    return out


async def _loop():
    while True:
        try:
            await build_snapshot()
        except Exception:
            log.exception("board: refresh failed")
        await asyncio.sleep(REFRESH_SECONDS)


def start_board_refresh_task():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
