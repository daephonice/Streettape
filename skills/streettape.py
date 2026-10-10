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
TSLAB = "0x5b1910eaad6450e50f816082aa078c41f10c292f"
DEVEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "DEVEX.md")


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


def baw_sign(input_mint, output_mint, amount):
    """One signed `baw market-order swap` after a quote. Never falls back: a failed sign is a failure."""
    q = baw_quote(input_mint, output_mint, amount)
    print(f"quote: {amount} in -> {q['uiOutAmount']} out, slippage {q['slippage']}. Confirm in the Binance app.",
          file=sys.stderr)
    t0 = time.time()
    d = _baw("market-order", "swap", "--fromTokenQty", _qty(amount), "--fromToken", input_mint,
             "--toToken", output_mint, "--binanceChainId", CHAIN)
    submit = round(time.time() - t0, 1)
    if not isinstance(d, dict) or not d.get("orderId"):
        raise RuntimeError(f"swap returned no orderId: {str(d)[:300]}")
    oid = d["orderId"]
    o = {}
    for _ in range(45):  # orderId is only "submitted"; poll to a terminal state (~90 s)
        time.sleep(2)
        rows = (_baw("market-order", "list", "--orderId", str(oid)) or {}).get("list") or []
        o = rows[0] if rows else {}
        if o.get("status") in ("FINISHED", "FAILED"):
            break
    return {"provider": "binance_agentic_wallet", "signed": True, "orderId": oid,
            "status": o.get("status") or "PENDING", "txHash": _pick(o, "txHash", "txHashes", "hash"),
            "submitSec": submit, "totalSec": round(time.time() - t0, 1),
            "quote": {"uiOutAmount": q["uiOutAmount"], "slippage": q["slippage"]},
            "swap": d, "order": o}


def aw_swap(input_mint, output_mint, amount, taker=None, sign=False):
    """Quote via `baw` (default). Returns None when baw is missing or fails, so callers fall back to the API."""
    if not BAW:
        return None
    try:
        return baw_quote(input_mint, output_mint, amount)
    except Exception as e:
        print(f"baw failed, using API: {e}", file=sys.stderr)
        return None


def order(input_mint, output_mint, amount, taker=None, sign=False):
    if sign:
        if not BAW:
            sys.exit(json.dumps({"error": "baw not on PATH: cannot sign"}))
        try:
            return baw_sign(input_mint, output_mint, amount)
        except Exception as e:
            sys.exit(json.dumps({"error": "baw sign failed", "detail": str(e)}))
    return aw_swap(input_mint, output_mint, amount, taker) or call("/api/swap/order", body={
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
                    "provider": l["provider"], "route": l.get("routeLabel")} if l.get("filled") else
                   {"underlying": l["underlying"], "unfilled": True, "weight": l["weight"], "reason": l.get("reason")})
                  for l in b.get("legs") or []]})


def cmd_defensive(a):
    d = call("/api/agent/defensive", {"usd": a.usd})
    if not d.get("triggered"):
        return out({"triggered": False, "qqqMove": d.get("qqqMove"), "threshold": d.get("threshold"),
                    "reason": d.get("reason")})
    sell = d.get("sell") or {}
    out({"triggered": True, "qqqMove": d.get("qqqMove"), "threshold": d.get("threshold"), "viable": d.get("viable"),
         "priceImpactPct": d.get("priceImpactPct"), "routeLabel": d.get("routeLabel"), "session": d.get("session"),
         "sell": {"quotedUsd": sell.get("quotedUsd"), "unfilledUsd": sell.get("unfilledUsd"), "legs": sell.get("legs")},
         "buy": d.get("buy")})


def cmd_quote(a):
    res = order(a.input_mint, a.output_mint, a.amount, a.taker, a.sign)
    out(res, note=not a.sign)
    if a.sign and res.get("status") != "FINISHED":
        sys.exit(1)


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


def cmd_fill(a):
    """Quote, compare with the API, confirm, sign once, print a DEVEX block. No retry, no API fallback."""
    if not BAW:
        sys.exit(json.dumps({"error": "baw not on PATH: cannot sign"}))
    args = (a.input_mint, a.output_mint, a.amount)
    b = baw_quote(*args)
    t = call("/api/swap/order", body={"inputMint": a.input_mint, "outputMint": a.output_mint,
                                      "uiAmount": a.amount, "taker": a.taker})
    ao, bo = t.get("uiOutAmount"), b["uiOutAmount"]
    if not ao:
        sys.exit(json.dumps({"error": "API quote empty", "provider": t.get("provider"),
                             "fallbackReason": t.get("fallbackReason")}))
    d = abs(bo - ao) / ao * 10000
    print(f"baw {bo} | api {ao} ({t.get('provider')}) | delta {d:.2f} bps (tol {a.tol})")
    if d > a.tol:
        sys.exit("quotes differ beyond tolerance: not signing")
    if not a.yes and input(f"Sign {a.amount} in -> ~{bo} out? type yes: ").strip().lower() != "yes":
        sys.exit("cancelled")
    try:
        r = baw_sign(*args)
    except Exception as e:
        r = {"status": "ERROR", "error": str(e)}
    sess = (call("/api/board").get("session") or {}).get("label", "?")
    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    ok = r.get("status") == "FINISHED"
    blk = (f"\n### {now[:10]} \u2014 Signed `baw market-order swap` {'filled' if ok else 'did not finish'} ({a.amount:g} of {a.input_mint[:8]}.. to {a.output_mint[:8]}..)\n\n"
           f"- Session: {sess} ({now}).\n"
           f"- What we hit: `baw market-order quote`, then `/api/swap/order` (delta {d:.2f} bps), then `baw market-order swap`, polled with `market-order list`.\n"
           f"- What came back:\n"
           f"  - Order id `{r.get('orderId')}`, status `{r.get('status')}`, tx `{r.get('txHash')}`.\n"
           f"  - Submit {r.get('submitSec')} s, total {r.get('totalSec')} s. Quote out {bo}, API out {ao}.\n"
           + (f"  - Error or flag: {r.get('error')}\n" if r.get("error") else "")
           + "- What we changed because of it: nothing.\n")
    print(json.dumps(r, indent=2, default=str))
    print(blk)
    if a.write:
        with open(DEVEX, "a") as f:
            f.write(blk)
        print("appended to DEVEX.md", file=sys.stderr)
    if not ok:
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


def _board_wrappers(underlying=None, symbols=None):
    toks = call("/api/board").get("tokens") or []
    return [t for t in toks if t.get("mint") and t.get("tokenPrice")
            and (underlying is None or (t.get("underlying") or "").upper() == underlying)
            and (symbols is None or t.get("symbol") in symbols)]


def _sell_legs(tokens, usd=50.0):
    return [{"symbol": t["symbol"], "premium": t.get("premium"), "sizeUsd": usd,
             "quote": order(t["mint"], USDT, usd / t["tokenPrice"])} for t in tokens]


def cmd_flatten_shut(pct, usd=50.0):
    """Sell quotes only for wrappers richer than pct, and only while cash is shut."""
    rep = call("/api/agent/scan", {"threshold": pct})
    sess = rep.get("session") or {}
    if sess.get("cashOpen"):
        return out({"session": sess, "flatten": [], "message": "cash is open, nothing quoted"})
    rich = {h["symbol"] for h in rep.get("hits") or [] if (h.get("premium") or 0) > pct}
    toks = sorted(_board_wrappers(symbols=rich), key=lambda t: t["premium"], reverse=True) if rich else []
    out({"session": sess, "threshold": pct, "flatten": _sell_legs(toks, usd)})


def cmd_rotate_if(u, min_bps):
    """Print the legs only if viable and netBps >= min_bps."""
    r = call(f"/api/agent/arb/{urllib.parse.quote(u)}")
    x = r.get("hit")
    if not x:
        return out({"underlying": u, "rotate": None, "message": "no arb"})
    net = x.get("netBps")
    if not x.get("viable") or net is None or net < min_bps:
        return out({"underlying": u, "rotate": None, "viable": x.get("viable"), "netBps": net,
                    "minBps": min_bps, "message": "net gap below the bar, not pushing this trade"})
    out({"underlying": u, "viable": True, "netBps": net, "minBps": min_bps,
         "sell": {"symbol": x.get("richSymbol"), "ratio": x.get("richRatio"), "leg": x.get("sellLeg")},
         "buy": {"symbol": x.get("cheapSymbol"), "ratio": x.get("cheapRatio"), "leg": x.get("buyLeg")}})


def cmd_stand_down(u, hours, usd=50.0):
    """Sell quotes into USDT only if Yahoo's earnings calendar has a date inside `hours`."""
    e = call("/api/earnings", {"underlying": u})
    h = e.get("hoursUntil")
    if h is None or not (0 <= h <= hours):
        return out({"underlying": u, "standDown": False, "earnings": e})
    out({"underlying": u, "standDown": True, "earnings": e, "sell": _sell_legs(_board_wrappers(underlying=u), usd)})


_T = r"\$?([a-z][a-z0-9.]{0,9})"
_N = r"(\d+(?:\.\d+)?)"
GRAMMAR = [
    (r"(?:what(?:'s| is) )?rich (?:vs|versus) friday", "what's rich vs Friday",
     lambda m: cmd_board(argparse.Namespace(min=0.0))),
    (r"rotate into cheapest " + _T, "rotate into cheapest NVDA",
     lambda m: cmd_rotate(argparse.Namespace(underlying=m[1].upper()))),
    (r"alert only while cash is shut", "alert only while cash is shut",
     lambda m: cmd_alerts(argparse.Namespace(threshold=None))),
    (r"flatten anything richer than " + _N + r"% while cash is shut", "flatten anything richer than 2% while cash is shut",
     lambda m: cmd_flatten_shut(float(m[1]) / 100)),
    (r"rotate " + _T + r" if net gap clears " + _N + r"%", "rotate NVDA if net gap clears 1%",
     lambda m: cmd_rotate_if(m[1].upper(), float(m[2]) * 100)),
    (r"buy the ai basket for \$?" + _N, "buy the AI basket for $50",
     lambda m: cmd_basket(argparse.Namespace(usd=float(m[1])))),
    (r"rotate into defensives when volatility spikes", "rotate into defensives when volatility spikes",
     lambda m: cmd_defensive(argparse.Namespace(usd=50.0))),
    (r"stand down " + _T + r" into usdt if earnings are inside " + _N + r"h", "stand down NVDA into USDT if earnings are inside 24h",
     lambda m: cmd_stand_down(m[1].upper(), float(m[2]))),
]


def say(sentence):
    """Closed grammar: exact sentences only, no model call, no free-text fallback."""
    t = re.sub(r"\s+", " ", sentence.lower()).strip().rstrip(".!?")
    for pat, _, fn in GRAMMAR:
        m = re.fullmatch(pat, t)
        if m:
            return fn(m)
    sys.exit(json.dumps({"error": "unrecognized", "accepted": [ex for _, ex, _ in GRAMMAR]}))


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
    fl = s.add_parser("fill", help="signed baw fill after quote check (default USDT to TSLAB, $5)")
    fl.add_argument("amount", type=float, nargs="?", default=5.0)
    fl.add_argument("--in", dest="input_mint", default=USDT); fl.add_argument("--out", dest="output_mint", default=TSLAB)
    fl.add_argument("--taker"); fl.add_argument("--tol", type=float, default=50.0)
    fl.add_argument("--yes", action="store_true"); fl.add_argument("--write", action="store_true", help="append block to DEVEX.md")
    fl.set_defaults(f=cmd_fill)
    f = s.add_parser("flatten"); f.add_argument("--usd", type=float, default=50.0)
    f.add_argument("--taker"); f.add_argument("--pct", type=float, help="min premium as fraction, default 0.02"); f.set_defaults(f=cmd_flatten)
    bk = s.add_parser("basket"); bk.add_argument("--usd", type=float, default=50.0); bk.set_defaults(f=cmd_basket)
    df = s.add_parser("defensive"); df.add_argument("--usd", type=float, default=50.0); df.set_defaults(f=cmd_defensive)
    r = s.add_parser("rotate"); r.add_argument("underlying"); r.set_defaults(f=cmd_rotate)
    al = s.add_parser("alerts"); al.add_argument("--threshold", type=float); al.set_defaults(f=cmd_alerts)
    sy = s.add_parser("say"); sy.add_argument("sentence", nargs="+"); sy.set_defaults(f=cmd_say)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
