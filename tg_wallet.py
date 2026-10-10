"""StreetTape bot: Binance Agentic Wallet (`baw`) runner, link/unlink, keep-alive.
Async only. Never subprocess.run inside the bot: it shares the FastAPI event loop."""
import os
import io
import functools
import json
import shutil
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from asyncio.subprocess import PIPE

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

import balances
import tg_ui as ui
from database import SessionLocal
from models import TgWallet

log = logging.getLogger("tg_wallet")

BAW = os.getenv("BAW", "baw")
WALLETS = os.getenv("TG_WALLETS_DIR", "/data/wallets")
CHAIN = "56"
VERIFY_TIMEOUT = 330
KEEPALIVE_EVERY = 15 * 60
KEEPALIVE_STALE = timedelta(hours=6)
REMIND_WITHIN = timedelta(hours=24)

router = Router()
_locks: dict[int, asyncio.Lock] = {}
_connecting: set[int] = set()


class SessionExpired(Exception):
    pass


class BawError(Exception):
    def __init__(self, err=None):
        self.err = err or {}
        super().__init__(str((err or {}).get("message") or err or "baw error"))


def wallet_dir(chat_id: int) -> str:
    return os.path.join(WALLETS, str(int(chat_id)))


def lock(chat_id: int) -> asyncio.Lock:
    return _locks.setdefault(int(chat_id), asyncio.Lock())


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_time(v):
    """signInMaxTime may be epoch s/ms (number or digit string) or ISO text."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, str) and not v.strip().lstrip("-").isdigit():
            return _aware(datetime.fromisoformat(v.strip().replace("Z", "+00:00")))
        n = float(v)
        if n > 1e11:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except Exception:
        return None


async def baw(chat_id: int, *args, timeout: int = 60):
    d = wallet_dir(chat_id)
    os.makedirs(WALLETS, mode=0o700, exist_ok=True)
    os.makedirs(d, mode=0o700, exist_ok=True)
    env = {**os.environ, "BINANCE_BAW_DIR": d}
    p = await asyncio.create_subprocess_exec(BAW, *args, "--json", env=env, stdout=PIPE, stderr=PIPE)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
            pass
        raise BawError({"name": "TIMEOUT", "message": f"baw {args[0]} timed out"})
    try:
        data = json.loads(out)  # parse even when exit code is 1
    except Exception:
        raise BawError({"name": "BAD_OUTPUT", "message": (out or b"")[:200].decode("utf-8", "replace")})
    if not data.get("success"):
        err = data.get("error") or {}
        if err.get("name") in ("NOT_LOGGED_IN", "SESSION_EXPIRED"):
            raise SessionExpired()
        raise BawError(err)
    return data.get("data")


# ---- db --------------------------------------------------------------------

def get_wallet(chat_id: int):
    """Linked means a TgWallet row exists and the chat is private (id > 0)."""
    if int(chat_id) <= 0:
        return None
    db = SessionLocal()
    try:
        w = db.execute(select(TgWallet).where(TgWallet.chat_id == int(chat_id))).scalar_one_or_none()
        if w:
            db.expunge(w)
        return w
    finally:
        db.close()


def linked_set(chat_ids) -> set[int]:
    ids = [int(c) for c in chat_ids if int(c) > 0]
    if not ids:
        return set()
    db = SessionLocal()
    try:
        return set(db.execute(select(TgWallet.chat_id).where(TgWallet.chat_id.in_(ids))).scalars().all())
    finally:
        db.close()


def _upsert(chat_id: int, address: str, session_end):
    db = SessionLocal()
    try:
        w = db.execute(select(TgWallet).where(TgWallet.chat_id == int(chat_id))).scalar_one_or_none()
        now = _utcnow()
        if w is None:
            w = TgWallet(chat_id=int(chat_id), address=address, linked_at=now)
            db.add(w)
        w.address = address
        w.session_end = session_end
        w.last_alive = now
        db.commit()
    finally:
        db.close()


def _drop(chat_id: int):
    db = SessionLocal()
    try:
        w = db.execute(select(TgWallet).where(TgWallet.chat_id == int(chat_id))).scalar_one_or_none()
        if w:
            db.delete(w)
            db.commit()
    finally:
        db.close()


def _touch(chat_id: int, session_end, reminded_for=None):
    db = SessionLocal()
    try:
        w = db.execute(select(TgWallet).where(TgWallet.chat_id == int(chat_id))).scalar_one_or_none()
        if w:
            w.last_alive = _utcnow()
            if session_end is not None:
                w.session_end = session_end
            if reminded_for is not None:
                w.reminded_for = reminded_for
            db.commit()
    finally:
        db.close()


# ---- link / unlink ---------------------------------------------------------

def _qr_png(url: str) -> bytes | None:
    try:
        import segno
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="png", scale=8, border=2)  # local, no third party
        return buf.getvalue()
    except Exception:
        log.warning("tg_wallet: QR failed", exc_info=True)
        return None


async def _finish_link(bot, chat_id: int):
    addrs = (await baw(chat_id, "wallet", "address")).get("addresses") or []
    addr = next((a.get("address") for a in addrs if str(a.get("binanceChainId")) == CHAIN), None)
    if not addr:
        raise BawError({"name": "NO_BSC_ADDRESS", "message": "no BSC address on this wallet"})
    settings = await baw(chat_id, "wallet", "settings")
    _upsert(chat_id, addr, _parse_time((settings or {}).get("signInMaxTime")))
    usdt = None
    try:
        usdt = float((await balances.get_balances(addr)).get("USDT") or 0.0)
    except Exception:
        log.info("tg_wallet: balance read failed after link", exc_info=True)
    await ui.say(bot, chat_id, ui.linked_text(addr, usdt), ui.linked_kb(addr))


async def _verify_task(bot, chat_id: int, qr_id: str):
    try:
        async with lock(chat_id):
            await baw(chat_id, "auth", "verify", "--qrCodeId", qr_id, timeout=VERIFY_TIMEOUT)
            await _finish_link(bot, chat_id)
    except Exception:
        log.info("tg_wallet: link failed for %s", chat_id, exc_info=True)
        await ui.say(bot, chat_id, ui.link_expired_text(), ui.link_intro_kb())
    finally:
        _connecting.discard(chat_id)


async def link_start(bot, chat_id: int):
    if chat_id <= 0:
        await ui.say(bot, chat_id, ui.private_only_text())
        return
    if chat_id in _connecting:
        await ui.say(bot, chat_id, ui.link_busy_text())
        return
    _connecting.add(chat_id)
    handed_off = False
    try:
        async with lock(chat_id):
            d = await baw(chat_id, "auth", "signin")
            if (d or {}).get("status") == "ALREADY_CONNECTED":
                await _finish_link(bot, chat_id)
                return
            url, code, qr_id = d.get("urlForWeb"), d.get("pairingCode"), d.get("qrCodeId")
            if not (url and qr_id):
                raise BawError({"name": "BAD_SIGNIN", "message": "signin returned no link"})
        cap, mk = ui.link_pending_caption(str(code or "")), ui.link_pending_kb(url)
        png = _qr_png(url)
        if png:
            await ui.say_photo(bot, chat_id, png, cap, mk)
        else:
            await ui.say(bot, chat_id, cap, mk)
        asyncio.create_task(_verify_task(bot, chat_id, qr_id))
        handed_off = True
    except Exception:
        log.warning("tg_wallet: signin failed for %s", chat_id, exc_info=True)
        await ui.say(bot, chat_id, ui.link_expired_text(), ui.link_intro_kb())
    finally:
        if not handed_off:
            _connecting.discard(chat_id)


async def unlink(chat_id: int):
    async with lock(chat_id):
        try:
            await baw(chat_id, "auth", "signout")
        except Exception:
            log.info("tg_wallet: signout failed for %s", chat_id, exc_info=True)
        _drop(chat_id)
        shutil.rmtree(wallet_dir(chat_id), ignore_errors=True)


async def on_session_expired(bot, chat_id: int):
    _drop(chat_id)
    await ui.say(bot, chat_id, ui.expired_text(), ui.need_link_kb())


def guarded(fn):
    """Wrap a callback/message handler: SessionExpired unlinks and shows 'link again'."""
    @functools.wraps(fn)
    async def wrapper(event, *a, **kw):
        try:
            return await fn(event, *a, **kw)
        except SessionExpired:
            msg = getattr(event, "message", None) or event
            await on_session_expired(event.bot if hasattr(event, "bot") else msg.bot, msg.chat.id)
    return wrapper


# ---- keep-alive ------------------------------------------------------------

async def keepalive_pass(bot):
    db = SessionLocal()
    try:
        rows = db.execute(select(TgWallet)).scalars().all()
        snap = [(w.chat_id, _aware(w.last_alive), _aware(w.session_end), _aware(w.reminded_for)) for w in rows]
    finally:
        db.close()
    now = _utcnow()
    for chat_id, alive, end, reminded in snap:
        if alive and now - alive < KEEPALIVE_STALE:
            continue
        try:
            async with lock(chat_id):
                s = await baw(chat_id, "wallet", "settings")
        except SessionExpired:
            _drop(chat_id)  # silent
            continue
        except Exception:
            log.info("tg_wallet: keepalive failed for %s", chat_id, exc_info=True)
            continue
        end = _parse_time((s or {}).get("signInMaxTime")) or end
        _touch(chat_id, end)
        if end and end - now <= REMIND_WITHIN and reminded != end:
            try:
                await ui.say(bot, chat_id, ui.renew_text(), ui.renew_kb())
                _touch(chat_id, None, reminded_for=end)
            except Exception:
                log.info("tg_wallet: reminder failed for %s", chat_id, exc_info=True)


async def keepalive_loop(bot):
    while True:
        try:
            await keepalive_pass(bot)
        except Exception:
            log.exception("tg_wallet: keepalive iteration failed")
        await asyncio.sleep(KEEPALIVE_EVERY)


# ---- callbacks (registered before the catch-all in telegram_bot) -----------

def _private(cb: CallbackQuery) -> bool:
    return bool(cb.message and cb.message.chat.id > 0)


@router.callback_query(lambda c: c.data == "link:go")
async def on_link_go(cb: CallbackQuery):
    if not _private(cb):
        await cb.answer(ui.private_only_text(), show_alert=True)
        return
    await cb.answer()
    await link_start(cb.bot, cb.message.chat.id)


@router.callback_query(lambda c: c.data == "link:off")
async def on_link_off(cb: CallbackQuery):
    if not _private(cb):
        await cb.answer(ui.private_only_text(), show_alert=True)
        return
    await cb.answer()
    await unlink(cb.message.chat.id)
    await ui.say(cb.bot, cb.message.chat.id, ui.unlinked_text(), ui.link_intro_kb())


@router.callback_query(lambda c: c.data == "link:renew")
async def on_link_renew(cb: CallbackQuery):
    if not _private(cb):
        await cb.answer(ui.private_only_text(), show_alert=True)
        return
    await cb.answer()
    chat_id = cb.message.chat.id
    async with lock(chat_id):
        try:
            await baw(chat_id, "auth", "signout")
        except Exception:
            log.info("tg_wallet: renew signout failed", exc_info=True)
    await link_start(cb.bot, chat_id)
