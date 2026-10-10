"""Offline matrix for the bot rules (Step 11). Run: python -m pytest test_routing_rules.py -q"""
import pytest

import routing_rules as rr

FAM = {"NVDAx": "xstocks", "NVDAon": "ondo", "NVDAB": "bstocks", "BNB": "stable", "USDC": "stable", "USDT": "stable"}


@pytest.fixture(autouse=True)
def fams(monkeypatch):
    monkeypatch.setattr(rr, "family", lambda s: FAM.get(s, "unknown"))
    monkeypatch.setattr(rr.rwa, "is_bnb", lambda s: s == "BNB")


@pytest.mark.parametrize("a,b,usd,ok", [
    ("USDT", "NVDAx", 100, False), ("NVDAx", "USDC", 100, False),          # xStocks locked
    ("USDT", "NVDAon", 4.99, False), ("USDT", "NVDAon", 5, True),          # Ondo $5, buy
    ("NVDAon", "USDT", 4.99, False), ("NVDAon", "USDT", 5, True),          # Ondo $5, sell
    ("USDC", "NVDAon", 5, True), ("BNB", "NVDAon", 50, False),             # BNB locked for Ondo
    ("NVDAon", "BNB", 50, False),
    ("USDT", "NVDAB", 1, True), ("BNB", "NVDAB", 1, True), ("USDC", "NVDAB", 1, True),   # bStocks open
    ("BNB", "USDC", 4, False), ("USDC", "USDT", 5, True),                  # stable swaps $5
])
def test_bot_allowed(a, b, usd, ok):
    assert rr.bot_allowed(a, b, usd)[0] is ok


def test_bot_locks():
    assert rr.bot_lock("xstocks") and rr.bot_lock("ondo", "BNB")
    assert not rr.bot_lock("ondo", "USDC") and not rr.bot_lock("bstocks", "BNB")


def test_rotate_pairs():
    assert rr.bot_rotate_ok("ondo", "bstocks") and rr.bot_rotate_ok("bstocks", "ondo")
    assert not rr.bot_rotate_ok("xstocks", "bstocks") and not rr.bot_rotate_ok("ondo", "xstocks")
    assert not rr.bot_rotate_ok("ondo", "ondo")
    assert rr.BOT_ROTATE_MIN_USD == 6.0 and rr.MIN_USD == 5.0


@pytest.mark.parametrize("a,b,usd,route", [
    ("USDT", "NVDAx", 1, "pancake"), ("BNB", "NVDAx", 9999, "pancake"),
    ("USDC", "NVDAon", 4, "pancake"), ("USDC", "NVDAon", 5, "binance"),
    ("BNB", "NVDAB", 1, "binance"), ("USDT", "NVDAB", 9999, "binance"),
    ("BNB", "USDC", 4, "pancake"), ("BNB", "USDC", 5, "binance"),
    ("NVDAon", "NVDAB", 1, "pancake"),                                      # rotate
])
def test_site_route(a, b, usd, route):
    assert rr.site_route(a, b, usd) == route
