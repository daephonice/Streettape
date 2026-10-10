"""/probe: quote-only matrix of the Binance web3 route through the user's own linked `baw` wallet.
Signs nothing, moves nothing. Gated by TG_PROBE_IDS (comma-separated chat ids)."""
import os
import json
import asyncio
import logging
from decimal import Decimal

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

import tg_wallet

log = logging.getLogger("tg_probe")
router = Router()

CHAIN = "56"
BNB = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
USDT = "0x55d398326f99059fF775485246999027B3197955"
USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
BASE = {"BNB": BNB, "USDT": USDT, "USDC": USDC}
WRAP = {
    "TSLAB(bStock)": "0x5b1910eaad6450e50f816082aa078c41f10c292f",
    "TSLAon(Ondo)": "0x2494b603319d4D9F9715c9f4496d9E0364B59d93",
    "TSLAx(xStock)": "0x8aD3c73F833d3F9A523aB01476625F269aEB7Cf0",
}
SIZES = [Decimal(s) for s in os.getenv("PROBE_USD", "1,5,10").split(",")]
_running: set[int] = set()


def allowed() -> set[int]:
    return {int(x) for x in os.getenv("TG_PROBE_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}


def f(d) -> str:
    return format(d.normalize(), "f") if d else "0"


async def quote(chat_id: int, qty, a: str, b: str):
    await asyncio.sleep(0.4)  # stay under the rate limit
    try:
        d = await tg_wallet.baw(chat_id, "market-order", "quote", "--fromTokenQty", f(qty),
                                "--fromToken", a, "--toToken", b, "--binanceChainId", CHAIN)
        return True, Decimal(str(d["toCoinAmount"])), ""
    except tg_wallet.SessionExpired:
        raise
    except tg_wallet.BawError as e:
        er = e.err or {}
        return False, None, " ".join(str(x) for x in (er.get("code", ""), er.get("name", ""), er.get("message", e)) if x)[:140]
    except Exception as e:  # bad payload shape etc.
        return False, None, f"{type(e).__name__}: {e}"[:140]


async def run_matrix(q, progress=None):
    """q(qty, from_addr, to_addr) -> (ok, out, err). Returns (rows, bnb_usd)."""
    rows = []

    async def rec(label, usd, ok, out, err):
        rows.append({"pair": label, "usd": str(usd), "ok": ok, "out": f(out) if out else "", "err": err})
        if progress and len(rows) % 10 == 0:
            await progress(len(rows))

    ok, out, err = await q(Decimal("0.01"), BNB, USDT)
    if not ok:
        raise RuntimeError(f"BNB->USDT quote failed: {err}")
    bnb_usd = out / Decimal("0.01")
    qty = lambda s, usd: usd / bnb_usd if s == "BNB" else usd  # USDT/USDC ~ $1
    names = list(BASE)
    for usd in SIZES:
        for a in names:
            for b in names:
                if a != b:
                    ok, out, err = await q(qty(a, usd), BASE[a], BASE[b])
                    await rec(f"{a}->{b}", usd, ok, out, err)
    for w, addr in WRAP.items():
        for p in names:
            for usd in SIZES:
                ok, out, err = await q(qty(p, usd), BASE[p], addr)
                await rec(f"{p}->{w}", usd, ok, out, err)
                if ok:
                    ok2, out2, err2 = await q(out, addr, BASE[p])
                    await rec(f"{w}->{p}", usd, ok2, out2, err2)
    return rows, bnb_usd


def table(rows, bnb_usd) -> str:
    lines = [f"BNB ~ ${bnb_usd:.2f}", ""]
    for r in rows:
        res = ("OK " + r["out"][:12]) if r["ok"] else ("ERR " + r["err"])
        lines.append(f"{r['pair']:26} ${Decimal(r['usd']):>3.0f}  {res}")
    return "\n".join(lines)


@router.message(Command("probe"))
async def probe(m: Message):
    cid = m.chat.id
    if cid not in allowed():
        return await m.answer(f"Not enabled. Your chat id is <code>{cid}</code>. "
                              "Add it to TG_PROBE_IDS and redeploy.")
    if cid <= 0 or not tg_wallet.get_wallet(cid):
        return await m.answer("Link your wallet first (private chat).")
    if cid in _running:
        return await m.answer("A probe is already running.")
    _running.add(cid)
    msg = await m.answer("Probing the Binance route, quote only. About 2 to 4 minutes.")

    async def progress(n):
        try:
            await msg.edit_text(f"Probing... {n} quotes done")
        except Exception:
            pass

    try:
        rows, bnb_usd = await run_matrix(lambda qty, a, b: quote(cid, qty, a, b), progress)
    except tg_wallet.SessionExpired:
        _running.discard(cid)
        return await m.answer("Wallet session expired. Reconnect, then run /probe again.")
    except Exception as e:
        _running.discard(cid)
        log.exception("probe failed")
        return await m.answer(f"Probe stopped: <code>{str(e)[:200]}</code>")
    _running.discard(cid)
    txt = table(rows, bnb_usd)
    await m.answer_document(BufferedInputFile(txt.encode(), "probe_table.txt"),
                            caption=f"{len(rows)} quotes. Send me this file.")
    await m.answer_document(BufferedInputFile(json.dumps(rows, indent=1).encode(), "probe_results.json"))
