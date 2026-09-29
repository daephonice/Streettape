#!/usr/bin/env python3
"""streettape-desk tools. Thin client: every number comes from the StreetTape API."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("STREETTAPE_URL", "https://streettape.up.railway.app").rstrip("/")
USDT = "0x55d398326f99059fF775485246999027B3197955"
FLATTEN_MIN = 0.02
# Official `binance-agentic-wallet` skill entrypoint (installed via
# `npx skills add binance-agentic-wallet`). Contract: JSON request on stdin,
# JSON result on stdout. Set to the skill's CLI, e.g. "npx skills run binance-agentic-wallet swap".
AW_CMD = os.getenv("BINANCE_AW_CMD", "").strip()


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


def aw_swap(input_mint, output_mint, amount, taker=None, sign=False):
    """Build (and, only if sign=True, sign) via the official Agentic Wallet skill.
    Falls back to StreetTape /api/swap/order when BINANCE_AW_CMD is unset or fails."""
    if not AW_CMD:
        return None
    req = {"chain": "bsc", "chainId": 56, "fromToken": input_mint, "toToken": output_mint,
           "amount": amount, "wallet": taker, "sign": bool(sign), "dryRun": not sign}
    try:
        r = subprocess.run(shlex.split(AW_CMD), input=json.dumps(req), capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        res = json.loads(r.stdout)
    except Exception:
        return None
    return {"provider": "binance_agentic_wallet", "signed": bool(sign), **(res if isinstance(res, dict) else {"result": res})}


def order(input_mint, output_mint, amount, taker=None, sign=False):
    return aw_swap(input_mint, output_mint, amount, taker, sign) or call("/api/swap/order", body={
        "inputMint": input_mint, "outputMint": output_mint, "uiAmount": amount, "taker": taker})


def out(obj):
    print(json.dumps(obj, indent=2))


def cmd_board(a):
    snap = call("/api/board")
    toks = [t for t in snap.get("tokens") or [] if t.get("premium") is not None and abs(t["premium"]) >= a.min]
    toks.sort(key=lambda t: t["premium"], reverse=True)
    out({
        "session": snap.get("session"),
        "fetchedAt": snap.get("fetchedAt"),
        "rich": [{k: t.get(k) for k in ("symbol", "underlying", "platform", "tokenPrice", "markPrice", "premium", "status")}
                 for t in toks],
    })


def cmd_quote(a):
    out(order(a.input_mint, a.output_mint, a.amount, a.taker, a.sign))


def cmd_flatten(a):
    snap = call("/api/board")
    rich = [t for t in snap.get("tokens") or []
            if t.get("premium") is not None and t["premium"] > FLATTEN_MIN and t.get("mint") and t.get("tokenPrice")]
    rich.sort(key=lambda t: t["premium"], reverse=True)
    legs = []
    for t in rich:
        q = order(t["mint"], USDT, a.usd / t["tokenPrice"], a.taker)
        legs.append({"symbol": t["symbol"], "premium": t["premium"], "sizeUsd": a.usd, "quote": q})
    out({"session": snap.get("session"), "flatten": legs,
         "note": "Not auto-executed — confirm and sign in your own wallet."})


def cmd_rotate(a):
    report = call("/api/agent/scan", {"underlying": a.underlying.upper()})
    arbs = [x for x in report.get("arbs") or [] if x.get("underlying", "").upper() == a.underlying.upper()]
    if not arbs:
        out({"underlying": a.underlying.upper(), "arb": None, "session": report.get("session")})
        return
    out({"session": report.get("session"), "arb": arbs[0],
         "note": "Not auto-executed — confirm and sign in your own wallet."})


def cmd_alerts(a):
    report = call("/api/agent/scan", {"threshold": a.threshold})
    session = report.get("session") or {}
    if session.get("cashOpen"):
        out({"alert": False, "reason": "cash open", "session": session})
        return
    out({"alert": bool(report.get("hits")), "session": session,
         "threshold": report.get("threshold"), "hits": report.get("hits")})


def main():
    p = argparse.ArgumentParser(prog="streettape")
    s = p.add_subparsers(dest="cmd", required=True)
    b = s.add_parser("board"); b.add_argument("--min", type=float, default=0.0); b.set_defaults(f=cmd_board)
    q = s.add_parser("quote")
    q.add_argument("input_mint"); q.add_argument("output_mint"); q.add_argument("amount", type=float)
    q.add_argument("--taker")
    q.add_argument("--sign", action="store_true", help="sign via official skill; only after the user confirmed")
    q.set_defaults(f=cmd_quote)
    f = s.add_parser("flatten"); f.add_argument("--usd", type=float, default=50.0)
    f.add_argument("--taker"); f.set_defaults(f=cmd_flatten)
    r = s.add_parser("rotate"); r.add_argument("underlying"); r.set_defaults(f=cmd_rotate)
    al = s.add_parser("alerts"); al.add_argument("--threshold", type=float); al.set_defaults(f=cmd_alerts)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
