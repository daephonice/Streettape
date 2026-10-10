"""Swap routing rules. The only place these rules and numbers live.

Bot (baw, Binance route only): bot_allowed().  Site (smart routing): site_route() / site_providers() / site_allowed().
The front end reads public_rules() via GET /api/routing-rules, so no number or family list is repeated in JS.

Families come from the wrapper platform (rwa.py / prices assets): xstocks, ondo, bstocks. BNB, USDC, USDT are "stable".
"""
from __future__ import annotations

import prices
import rwa

MIN_USD = 5.0              # Ondo buy/sell and stable/BNB swaps (Binance route minimum)
BOT_ROTATE_MIN_USD = 6.0   # bot Rotate only: $1 buffer so leg-1 USDT output never lands under MIN_USD

STABLE_SYMBOLS = ("BNB", "USDC", "USDT")
WRAPPER_FAMILIES = ("xstocks", "ondo", "bstocks")

SITE_DIRECT_PANCAKE = ("xstocks",)       # always Pancake, Binance never tried
SITE_BINANCE = ("bstocks",)              # always Binance first, fallback on error
SITE_THRESHOLD = ("ondo", "stable")      # < MIN_USD Pancake directly, >= MIN_USD Binance first
BOT_ROTATE_FAMILIES = ("ondo", "bstocks")   # xStocks have no baw liquidity, so the bot never rotates them
BOT_PICK_SKIP = ("xstocks",)             # quick_pick never offers these to the bot
XSTOCKS_LOCK_MSG = "xStocks are locked in the bot (no Binance liquidity). Use the site."
ONDO_BNB_MSG = "BNB can't be swapped with Ondo. Use USDC or USDT."
ONDO_BLOCKED_PAY = ("BNB",)              # BNB <-> Ondo is not swappable (swap.unsupported_pair enforces it)

_STABLE_ADDRS = {rwa.NATIVE.lower(), rwa.WBNB.lower(), rwa.USDC.lower(), rwa.USDT.lower()}


def _platform_of_addr(addr: str) -> str | None:
    for w in rwa.wrappers():
        if (w.get("address") or "").lower() == addr:
            return (w.get("platform") or "").lower() or None
    for meta in (prices.get_assets().get("assets") or {}).values():
        if (meta.get("mint") or "").lower() == addr:
            return (meta.get("platform") or "").lower() or None
    return None


def family(addr_or_symbol: str) -> str:
    """\"xstocks\" | \"ondo\" | \"bstocks\" | \"stable\" | \"unknown\" for a token address or a symbol."""
    s = (addr_or_symbol or "").strip()
    if not s:
        return "unknown"
    if s.lower().startswith("0x"):
        a = s.lower()
        if a in _STABLE_ADDRS:
            return "stable"
        plat = _platform_of_addr(a)
    else:
        if s.upper() in STABLE_SYMBOLS:
            return "stable"
        w = rwa.by_symbol(s)
        plat = (w.get("platform") or "").lower() if w else None
        if not plat:
            meta = (prices.get_assets().get("assets") or {}).get(s.upper()) or {}
            plat = (meta.get("platform") or "").lower() or None
    return plat if plat in WRAPPER_FAMILIES else "unknown"


def usd_value(mint: str, ui_amount: float):
    """USD value of ui_amount of `mint` from the price cache, or None when no price is known."""
    import swap  # lazy: swap imports this module
    px = swap._price_of_mint(mint)
    return (px * ui_amount) if px and ui_amount else None


def bot_allowed(from_: str, to: str, usd) -> tuple[bool, str | None]:
    """Telegram bot (baw, Binance route only). Table 1A."""
    a, b = family(from_), family(to)
    fams = {a, b}
    if "xstocks" in fams:
        return False, XSTOCKS_LOCK_MSG
    if "ondo" in fams:
        if any(rwa.is_bnb(x) or (x or "").upper() == "BNB" for x in (from_, to)):
            return False, ONDO_BNB_MSG
        if usd is not None and usd < MIN_USD:
            return False, f"Minimum is ${MIN_USD:g} for Ondo."
        return True, None
    if "bstocks" in fams:
        return True, None
    if fams == {"stable"} and usd is not None and usd < MIN_USD:
        return False, f"Minimum is ${MIN_USD:g} for this swap."
    return True, None


def bot_lock(platform: str | None, coin: str | None = None) -> str | None:
    """Why a wrapper (with an optional pay/receive coin) is locked in the bot, None when open. For button locks."""
    if platform == "xstocks":
        return XSTOCKS_LOCK_MSG
    if platform == "ondo" and (coin or "").upper() == "BNB":
        return ONDO_BNB_MSG
    return None


def bot_rotate_ok(from_platform: str | None, to_platform: str | None) -> bool:
    """Bot Rotate runs Ondo <-> bStocks only."""
    return (from_platform in BOT_ROTATE_FAMILIES and to_platform in BOT_ROTATE_FAMILIES
            and from_platform != to_platform)


def site_allowed(from_: str, to: str) -> tuple[bool, str | None]:
    """Site pair check (BNB <-> Ondo block). Same rule swap.unsupported_pair enforces."""
    import swap
    msg = swap.unsupported_pair(from_, to)
    return (msg is None), msg


def site_route(from_: str, to: str, usd) -> str:
    """\"pancake\" | \"binance\" for the first provider tried. Table 1B.
    usd None (no price known) is treated as >= MIN_USD: Binance first, Pancake on error."""
    a, b = family(from_), family(to)
    if a in WRAPPER_FAMILIES and b in WRAPPER_FAMILIES:
        return "pancake"  # rotate (wrapper -> wrapper): one-time swap, no minimum
    fams = {a, b}
    if fams & set(SITE_DIRECT_PANCAKE):
        return "pancake"
    small = usd is not None and usd < MIN_USD
    if "ondo" in fams:
        return "pancake" if small else "binance"
    if fams & set(SITE_BINANCE):
        return "binance"
    if fams == {"stable"}:
        return "pancake" if small else "binance"
    return "binance"


def site_providers(from_: str, to: str, usd) -> list[str]:
    """Ordered provider names for swap.quote(). Binance is never a fallback after a direct-Pancake route.
    Ondo < MIN_USD: Pancake only (no silent Binance). OpenOcean never for Ondo."""
    ondo = "ondo" in (family(from_), family(to))
    if site_route(from_, to, usd) == "pancake":
        return ["pancake"] + ([] if ondo else ["openocean"])
    return ["binance_web3", "pancake"] + ([] if ondo else ["openocean"])


def public_rules() -> dict:
    """Rule data for the front end (GET /api/routing-rules)."""
    return {
        "minUsd": MIN_USD,
        "botRotateMinUsd": BOT_ROTATE_MIN_USD,
        "stables": list(STABLE_SYMBOLS),
        "wrappers": list(WRAPPER_FAMILIES),
        "ondoBlockedPay": list(ONDO_BLOCKED_PAY),
        "site": {
            "directPancake": list(SITE_DIRECT_PANCAKE),
            "binance": list(SITE_BINANCE),
            "threshold": list(SITE_THRESHOLD),
            "rotatePancake": True,
        },
    }
