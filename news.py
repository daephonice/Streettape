"""Shared hourly news for the 8 tokenized stocks tokens.

One background task fetches fresh headlines at the top of every UTC hour
(00:00, 01:00, ... 23:00) and stores them in `news_items`. /api/news reads
from memory, so every user sees the same feed no matter when they load the
page and no user request ever touches a news API.

A token only gets a new row when a newer article than the stored one is
found; otherwise its previous news stays until the next hour that finds one.

Sources (public, no API key): Google News RSS, GDELT DOC 2.0 as fallback.
"""
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import select, delete

import rwa
from database import SessionLocal
from models import NewsItem

log = logging.getLogger("news")

GOOGLE_RSS = "https://news.google.com/rss/search"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_AGE_DAYS = 14
KEEP_PER_SYMBOL = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StreetTapeNews/1.0)", "Accept": "*/*"}

_cache: list[dict] = []
_task: asyncio.Task | None = None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _query_name(sym: str, name: str | None) -> str:
    n = (name or sym).strip()
    prev = None
    while prev != n:
        prev = n
        n = re.sub(r"(?i)[\s\-(]*\b(pre-?stocks?|pre-?ipo|token)\b[\s)]*$", "", n).strip()
    return n or sym


def _stock_list() -> list[tuple[str, str]]:
    """(wrapper symbol, display name) pairs straight from the static universe —
    no dependency on the board/price snapshot, so news can run even if Yahoo/Gecko are down."""
    seen: dict[str, str] = {}
    for w in rwa.wrappers():
        seen.setdefault(w["symbol"], _query_name(w["symbol"], w.get("name")))
    return list(seen.items())


def _clean_title(title: str, source: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    if source and title.endswith(" - " + source):
        title = title[: -(len(source) + 3)].strip()
    elif " - " in title:
        title = title.rsplit(" - ", 1)[0].strip()
    return title


def _compose(title: str, source: str) -> str:
    title = title.rstrip(".!? ")
    return f"{title}. Source: {source}." if source else f"{title}."


async def _google(client: httpx.AsyncClient, name: str) -> list[dict]:
    resp = await client.get(
        GOOGLE_RSS,
        params={"q": f'"{name}" when:{MAX_AGE_DAYS}d', "hl": "en-US", "gl": "US", "ceid": "US:en"},
    )
    resp.raise_for_status()
    out = []
    for it in ET.fromstring(resp.content).iter("item"):
        source = (it.findtext("source") or "").strip()
        title = _clean_title(it.findtext("title") or "", source)
        try:
            at = parsedate_to_datetime(it.findtext("pubDate") or "")
        except Exception:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if title:
            out.append({"title": title, "source": source, "at": at.astimezone(timezone.utc)})
    return out


async def _gdelt(client: httpx.AsyncClient, name: str) -> list[dict]:
    resp = await client.get(
        GDELT_DOC,
        params={
            "query": f'"{name}" sourcelang:english',
            "mode": "artlist", "format": "json", "sort": "datedesc",
            "maxrecords": "15", "timespan": f"{MAX_AGE_DAYS}d",
        },
    )
    resp.raise_for_status()
    out = []
    for a in resp.json().get("articles", []):
        try:
            at = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        title = re.sub(r"\s+", " ", a.get("title") or "").strip()
        if title:
            out.append({"title": title, "source": a.get("domain") or "", "at": at})
    return out


def _newest(items: list[dict], name: str) -> dict | None:
    now = datetime.now(timezone.utc)
    key = _norm(name)
    ok = [
        i for i in items
        if key in _norm(i["title"])
        and now - timedelta(days=MAX_AGE_DAYS) <= i["at"] <= now + timedelta(hours=1)
    ]
    return max(ok, key=lambda i: i["at"]) if ok else None


async def _find(client: httpx.AsyncClient, name: str) -> dict | None:
    try:
        found = _newest(await _google(client, name), name)
        if found:
            return found
    except Exception:
        log.warning("news: Google RSS failed for %s", name, exc_info=True)
    await asyncio.sleep(6)  # GDELT allows 1 request / 5s
    try:
        return _newest(await _gdelt(client, name), name)
    except Exception:
        log.warning("news: GDELT failed for %s", name, exc_info=True)
        return None


def _store(sym: str, found: dict) -> bool:
    body = _compose(found["title"], found["source"])
    db = SessionLocal()
    try:
        last = db.execute(
            select(NewsItem)
            .where(NewsItem.symbol == sym)
            .order_by(NewsItem.published_at.desc(), NewsItem.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last:
            last_at = last.published_at if last.published_at.tzinfo else last.published_at.replace(tzinfo=timezone.utc)
            if last.body == body or found["at"] <= last_at:
                return False
        db.add(NewsItem(symbol=sym, body=body, published_at=found["at"]))
        db.flush()
        stale = db.execute(
            select(NewsItem.id)
            .where(NewsItem.symbol == sym)
            .order_by(NewsItem.published_at.desc(), NewsItem.id.desc())
            .offset(KEEP_PER_SYMBOL)
        ).scalars().all()
        if stale:
            db.execute(delete(NewsItem).where(NewsItem.id.in_(stale)))
        db.commit()
        return True
    except Exception:
        db.rollback()
        log.warning("news: store failed for %s", sym, exc_info=True)
        return False
    finally:
        db.close()


def _load_cache() -> None:
    """Latest row per current tokenized stocks symbol, in board order."""
    global _cache
    symbols = [s for s, _ in _stock_list()]
    if not symbols:
        return
    db = SessionLocal()
    try:
        rows = db.execute(
            select(NewsItem)
            .where(NewsItem.symbol.in_(symbols))
            .order_by(NewsItem.published_at.desc(), NewsItem.id.desc())
        ).scalars().all()
        latest: dict[str, NewsItem] = {}
        for r in rows:
            latest.setdefault(r.symbol, r)
        _cache = [
            {"id": latest[s].id, "symbol": s.upper(), "body": latest[s].body,
             "publishedAt": latest[s].published_at.isoformat(),
             "underlying": (rwa.by_symbol(s) or {}).get("underlying", s.upper())}
            for s in symbols if s in latest
        ]
    finally:
        db.close()


async def refresh() -> None:
    stocks = _stock_list()
    if not stocks:
        return
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        for sym, name in stocks:
            found = await _find(client, name)
            if found:
                await asyncio.to_thread(_store, sym, found)
            await asyncio.sleep(1)
    await asyncio.to_thread(_load_cache)


async def _loop() -> None:
    try:
        await asyncio.to_thread(_load_cache)
    except Exception:
        log.warning("news: cache load failed", exc_info=True)
    while True:
        try:
            await refresh()
        except Exception:
            log.warning("news: refresh failed", exc_info=True)
        now = datetime.now(timezone.utc)
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        await asyncio.sleep(max(1.0, (nxt - now).total_seconds() + 2))


def start_news_task() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def get_news() -> list[dict]:
    return list(_cache)
