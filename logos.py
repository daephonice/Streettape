"""Local circular logos. Base ticker logo is shared by wrappers with no logo of their own."""
from __future__ import annotations

_DIR = "/static/img/logos"
_BASE = {"NVDA": "nvda", "SPCX": "spcx", "QQQ": "qqq", "AAPL": "aapl",
         "META": "meta", "AMD": "amdx", "TSLA": "tsla"}
_OWN = {"AAPLB": "aaplb", "AMDX": "amdx"}


def logo_for(symbol: str | None, underlying: str | None = None) -> str | None:
    s = (symbol or "").upper()
    f = _OWN.get(s) or _BASE.get(s) or _BASE.get((underlying or "").upper())
    return f"{_DIR}/{f}.png" if f else None


def group_logo(underlying: str | None) -> str | None:
    f = _BASE.get((underlying or "").upper())
    return f"{_DIR}/{f}.png" if f else None
