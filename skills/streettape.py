#!/usr/bin/env python3
"""streettape-desk tools. Thin client: every number comes from the StreetTape API."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("STREETTAPE_URL", "https://streettape.up.railway.app").rstrip("/")
USDT = "0x55d398326f99059fF775485246999027B3197955"
FLATTEN_MIN = 0.02
NOTE = "not auto-executed, confirm and sign in your own wallet."
# Official `binance-agentic-wallet` skill drives the `baw` CLI
# (npx skills add https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet).
# If `baw` is on PATH it does quote/build/sign; otherwise we fall back to StreetTape /api/swap/order.
BAW = shutil.which("baw")
CHAIN = "56"


def call(path, params=None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(json.dumps({"error": e.code, "detail": e.read().decode()[:500]}))
    except Exception as e:
        sys.exit(json.dumps({"error": str(e)}))


def _baw(*args):
    r = subprocess.run([BAW, *args, "--json"], capture_output=True, text=True, timeout=60)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"baw exit {r.returncode}: {(r.stdout or r.stderr).strip()[:300]}")
    if not d.get("success"):
        raise RuntimeError(str(d)[:300])
    return d.get("data")


def _qty(amount):
    return format(float(amount), ".18f").rstrip("0").rstrip(".") or "0"


def _pick(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def baw_quote(input_mint, output_mint, amount):
    """One `baw market-order quote`. Raises when baw is missing or fails."""
    if not BAW:
        raise RuntimeError("baw not on PATH")
    d = _baw("market-order", "quote", "--fromTokenQty", _qty(amount), "--fromToken", input_mint,
             "--toToken", output_mint, "--binanceChainId", CHAIN)
    imp = _pick(d, "priceImpactPct", "priceImpactPercent", "priceImpact")
    return {"provider": "binance_agentic_wallet", "signed": False, "uiOutAmount": float(d["toCoinAmount"]),
            "route": _pick(d, "route", "routes", "vendorName", "vendor", "provider"),
            "priceImpactPct": float(imp) if imp is not None else None,
            "slippage": d.get("slippage"), "needsWallet": False, "raw": d}


def aw_swap(input_mint, output_mint, amount, taker=None, sign=False):
    """Quote (default) or sign+submit (sign=True, only after user confirmed) via official `baw`.
    Returns None when baw is missing or fails, so callers fall back to the StreetTape API."""
    if not BAW:
        return None
    try:
        if not sign:
            return baw_quote(input_mint, output_mint, amount)
        d = _baw("market-order", "swap", "--fromTokenQty", _qty(amount), "--fromToken", input_mint,
                 "--toToken", output_mint, "--binanceChainId", CHAIN)
        oid = d["orderId"]
        for _ in range(15):  # orderId is only "submitted"; poll to a terminal state
            time.sleep(2)
            o = _baw("market-order", "list", "--orderId", str(oid))["list"][0]
            if o["status"] in ("FINISHED", "FAILED"):
                return {"provider": "binance_agentic_wallet", "signed": True, "orderId": oid,
                        "status": o["status"], "txHash": o.get("txHash")}
        return {"provider": "binance_agentic_wallet", "signed": True, "orderId": oid, "status": "PENDING"}
    except Exception as e:
        print(f"baw failed, using API: {e}", file=sys.stderr)
        return None


def order(input_mint, output_mint, amount, taker=None, sign=False):
    return aw_swap(input_mint, output_mint, amount, taker, sign) or call("/api/swap/order", body={
        "inputMint": input_mint, "outputMint": output_mint, "uiAmount": amount, "taker": taker})


def out(obj, note=True):
    print(json.dumps(obj, indent=2))
    if note:
        print(NOTE)


def cmd_board(a):
    snap = call("/api/board")
    toks = [t for t in snap.get("tokens") or [] if t.get("premium") is not None and abs(t["premium"]) >= a.min]
    toks.sort(key=lambda t: t["premium"], reverse=True)
    out({
        "session": snap.get("session"),
        "fetchedAt": snap.get("fetchedAt"),
        "rich": [{"symbol": t.get("symbol"), "tape": t.get("tokenPrice"), "official": t.get("markPrice"),
                  "fair": t.get("fairPrice"), "premium": t.get("premium"),
                  "premiumToFair": t.get("premiumToFair"), "status": t.get("status")} for t in toks],
    })


def cmd_basket(a):
    b = call("/api/basket/ai", {"usd": a.usd})
    out({"session": b.get("session"), "sizeUsd": b.get("sizeUsd"), "from": b.get("from"),
         "filledUsd": b.get("filledUsd"), "unfilledUsd": b.get("unfilledUsd"),
         "legs": [({"underlying": l["underlying"], "symbol": l["symbol"], "platform": l["platform"],
                    "contract": l["contract"], "ratio": l["ratio"], "tape": l["tape"], "official": l["official"],
                    "fair": l["fair"], "amountUsd": l["amountUsd"], "outAmount": l["outAmount"],
                    "provider": l["provider"], "deepLink": l["deepLink"]} if l.get("filled") else
                   {"underlying": l["underlying"], "unfilled": True, "weight": l["weight"], "reason": l.get("reason")})
                  for l in b.get("legs") or []]})


def cmd_quote(a):
    out(order(a.input_mint, a.output_mint, a.amount, a.taker, a.sign))


def cmd_verify(a):
    """Same pair, same size: official `baw` quote vs StreetTape /api/swap/order. Quote only, never signs."""
    try:
        b = baw_quote(a.input_mint, a.output_mint, a.amount)
    except Exception as e:
        b = {"provider": "binance_agentic_wallet", "error": str(e)}
    t = call("/api/swap/order", body={"inputMint": a.input_mint, "outputMint": a.output_mint,
                                      "uiAmount": a.amount, "taker": a.taker})
    api = {"provider": t.get("provider"), "executionMode": t.get("executionMode"), "uiOutAmount": t.get("uiOutAmount"),
           "route": t.get("routes"), "priceImpactPct": t.get("priceImpactPct"),
           "fallbackReason": t.get("fallbackReason"), "raw": t}
    res = {"pair": {"in": a.input_mint, "out": a.output_mint, "amount": a.amount, "chain": CHAIN, "taker": a.taker},
           "baw": b, "api": api}
    bo, ao = b.get("uiOutAmount"), api.get("uiOutAmount")
    if b.get("error"):
        res["verdict"] = "NO LIVE baw QUOTE: not verified. Use the failure above in DEVEX.md."
    elif api["provider"] != "binance_web3" or not (bo and ao):
        res["verdict"] = f"NOT COMPARABLE: API side is {api['provider']} ({api.get('fallbackReason') or 'no quote'}). Pass --taker, or use a pair with an AMM route."
    else:
        d = abs(bo - ao) / ao * 10000
        res["deltaBps"] = round(d, 2)
        res["verdict"] = "SAME ORDER" if d <= a.tol else f"DIFFERENT: out-amounts {d:.1f} bps apart (> {a.tol})"
    out(res)
    if not str(res["verdict"]).startswith("SAME"):
        sys.exit(1)


def cmd_flatten(a):
    snap = call("/api/board")
    rich = [t for t in snap.get("tokens") or []
            if t.get("premium") is not None and t["premium"] > (a.pct if a.pct is not None else FLATTEN_MIN) and t.get("mint") and t.get("tokenPrice")]
    rich.sort(key=lambda t: t["premium"], reverse=True)
    legs = []
    for t in rich:
        q = order(t["mint"], USDT, a.usd / t["tokenPrice"], a.taker)
        legs.append({"symbol": t["symbol"], "premium": t["premium"], "sizeUsd": a.usd, "quote": q})
    out({"session": snap.get("session"), "flatten": legs})


def cmd_rotate(a):
    u = a.underlying.upper()
    report = call("/api/agent/scan", {"underlying": u})
    arbs = [x for x in report.get("arbs") or [] if x.get("underlying", "").upper() == u]
    if not arbs:
        out({"underlying": u, "arb": None, "session": report.get("session")})
        return
    x = arbs[0]
    res = {"session": report.get("session"), "underlying": u, "viable": x.get("viable"), "netBps": x.get("netBps"),
           "sell": {"symbol": x.get("richSymbol"), "ratio": x.get("richRatio"), "leg": x.get("sellLeg")},
           "buy": {"symbol": x.get("cheapSymbol"), "ratio": x.get("cheapRatio"), "leg": x.get("buyLeg")}}
    if not x.get("viable"):
        res["message"] = "costs eat the gap, not pushing this trade"
    out(res)


def cmd_alerts(a):
    report = call("/api/agent/scan", {"threshold": a.threshold})
    session = report.get("session") or {}
    if session.get("cashOpen"):
        return
    out({"alert": bool(report.get("hits")), "session": session,
         "threshold": report.get("threshold"), "hits": report.get("hits")})


def say(sentence):
    """Phrase entry: four sentences, four commands. No model call; the parser is the feature."""
    t = re.sub(r"\s+", " ", sentence.lower()).strip()
    if "rotate" in t:
        m = re.search(r"(?:cheapest|into|rotate)\s+\$?([a-z][a-z0-9.]{0,9})\b", t)
        skip = {"into", "cheapest", "rotate", "the", "a"}
        words = [w.strip("$.,!?") for w in t.split()]
        tick = next((w for w in reversed(words) if w and w not in skip and w != "rotate"), None)
        tick = m.group(1) if m and m.group(1) not in skip else tick
        if not tick:
            sys.exit(json.dumps({"error": "rotate into which ticker?"}))
        return cmd_rotate(argparse.Namespace(underlying=tick))
    if "basket" in t:
        return cmd_basket(argparse.Namespace(usd=50.0))
    if "flatten" in t:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
        return cmd_flatten(argparse.Namespace(usd=50.0, taker=None, pct=float(m.group(1)) / 100 if m else None))
    if "alert" in t and re.search(r"cash.*(shut|closed)|(shut|closed).*cash", t):
        return cmd_alerts(argparse.Namespace(threshold=None))
    if "rich" in t or "friday" in t:
        return cmd_board(argparse.Namespace(min=0.0))
    sys.exit(json.dumps({"error": "unrecognized", "try": [
        "what's rich vs Friday", "rotate into cheapest NVDA", "flatten anything 2% rich", "alert only when cash is shut", "AI basket"]}))


def cmd_say(a):
    say(" ".join(a.sentence))


def main():
    p = argparse.ArgumentParser(prog="streettape")
    s = p.add_subparsers(dest="cmd", required=True)
    b = s.add_parser("board"); b.add_argument("--min", type=float, default=0.0); b.set_defaults(f=cmd_board)
    q = s.add_parser("quote")
    q.add_argument("input_mint"); q.add_argument("output_mint"); q.add_argument("amount", type=float)
    q.add_argument("--taker")
    q.add_argument("--sign", action="store_true", help="sign via official skill; only after the user confirmed")
    q.set_defaults(f=cmd_quote)
    v = s.add_parser("verify", help="baw quote vs /api/swap/order on the same pair and size (quote only)")
    v.add_argument("input_mint"); v.add_argument("output_mint"); v.add_argument("amount", type=float)
    v.add_argument("--taker"); v.add_argument("--tol", type=float, default=50.0, help="max out-amount gap in bps")
    v.set_defaults(f=cmd_verify)
    f = s.add_parser("flatten"); f.add_argument("--usd", type=float, default=50.0)
    f.add_argument("--taker"); f.add_argument("--pct", type=float, help="min premium as fraction, default 0.02"); f.set_defaults(f=cmd_flatten)
    bk = s.add_parser("basket"); bk.add_argument("--usd", type=float, default=50.0); bk.set_defaults(f=cmd_basket)
    r = s.add_parser("rotate"); r.add_argument("underlying"); r.set_defaults(f=cmd_rotate)
    al = s.add_parser("alerts"); al.add_argument("--threshold", type=float); al.set_defaults(f=cmd_alerts)
    sy = s.add_parser("say"); sy.add_argument("sentence", nargs="+"); sy.set_defaults(f=cmd_say)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
