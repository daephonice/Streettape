"""StreetTape bot: Book (Step 12). Holdings and value only. No cost basis, PnL or charts in v1."""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

import balances
import prices
import tg_ui as ui
import tg_wallet as tw
import tg_trade

log = logging.getLogger("tg_book")

router = Router()
DUST_USD = 0.01


def _price(sym: str):
    p = ((prices.get_prices() or {}).get("prices") or {}).get(sym)
    return p.get("price") if p else None


async def render(w):
    """(text, markup) for one linked wallet."""
    hold = (await balances.get_balances(w.address)).get("holdings") or {}
    stocks, cash, total = [], [], 0.0
    for sym, amt in hold.items():
        if not amt or amt <= 0:
            continue
        a = prices.get_asset(sym)
        if not a:
            continue
        px = 1.0 if sym in ("USDT", "USDC") else _price(sym)
        val = amt * px if px is not None else None
        if a.get("kind") == "stock":
            if val is not None and val < DUST_USD:
                continue
            stocks.append((sym, a.get("underlying"), amt * tg_trade.share_ratio(sym), val))
        elif val is None or val >= DUST_USD or sym == "BNB":
            cash.append((sym, amt, val))
        total += val or 0.0
    stocks.sort(key=lambda r: -(r[3] or 0))
    for r in stocks:
        total += r[3] or 0.0

    def usd(v):
        return f"${v:,.2f}" if v is not None else "—"

    lines = [f"{sym:<8}{sh:>10.4g} sh{usd(v):>11}" for sym, _, sh, v in stocks]
    lines += [f"{sym:<8}{amt:>13.4g}{usd(v):>11}" for sym, amt, v in sorted(cash, key=lambda r: r[0] != "USDT")]
    if not lines:
        return (f"<b>Book</b>\nNothing here yet. Send USDT on BNB Smart Chain to:\n<code>{w.address}</code>",
                ui.kb([ui.cb_btn(ui.BOARD, "back:board")]))
    text = f"<b>Book</b> · <code>{usd(total)}</code>\n<code>" + "\n".join(lines) + "</code>"
    sells, seen = [], set()
    for _, u, _, _ in stocks:
        if u and u not in seen:
            seen.add(u)
            sells.append(ui.cb_btn(f"Sell {u}", f"sell:{u}"))
    rows = [sells[i:i + 2] for i in range(0, len(sells), 2)]
    rows.append([ui.cb_btn(ui.SEND, "snd"), ui.cb_btn(ui.BOARD, "back:board")])
    return text, ui.kb(*rows)


@router.callback_query(lambda c: c.data == "book")
@tw.guarded
async def on_book(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    if chat_id <= 0:
        await cb.answer(ui.private_only_text(), show_alert=True)
        return
    w = tw.get_wallet(chat_id)
    await cb.answer()
    if not w:
        await ui.show(cb, ui.link_intro_text(), ui.link_intro_kb())
        return
    await ui.show(cb, *(await render(w)))


@router.message(Command("book"))
@tw.guarded
async def on_book_cmd(message: Message):
    chat_id = message.chat.id
    if chat_id <= 0:
        await ui.say(message.bot, chat_id, ui.private_only_text())
        return
    w = tw.get_wallet(chat_id)
    if not w:
        await ui.say(message.bot, chat_id, ui.link_intro_text(), ui.link_intro_kb())
        return
    m = await ui.say(message.bot, chat_id, *(await render(w)))
    ui.hold(chat_id, m.message_id)
