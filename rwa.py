"""BNB tokenized-stock universe + mark/tape math.
Tape = GeckoTerminal. Mark = Yahoo last cash print. Both free, no key.

UNIVERSE below is the seed catalog (hand-maintained, always present even with
no RWA Data key). merge_dynamic() folds in whatever RWA Data's rwa/tokens +
rwa/search actually list — new underlyings, new wrappers on known underlyings,
and a real `address` for a seed row that had none. It never
invents a contract: a wrapper with no address from either source stays
address: None and callers must treat that as "no tape, no Buy" — see
board.py's hasTape / "— no tape" handling.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
USDT = "0x55d398326f99059fF775485246999027B3197955"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
NY = ZoneInfo("America/New_York")
WAT = ZoneInfo("Africa/Lagos")

UNIVERSE = [
    {"underlying":"NVDA","yahoo":"NVDA","name":"NVIDIA","wrappers":[
        {"symbol":"NVDAx","platform":"xstocks","address":"0xc845b2894dBddd03858fd2D643B4eF725fE0849d","multiplier":None},
        {"symbol":"NVDAon","platform":"ondo","address":"0xA9eE28C80f960B889dFbd1902055218cBa016F75","multiplier":None},
        {"symbol":"NVDAB","platform":"bstocks","address":"0x02fca66c1d1afb4e2a7884261eb00f63598a7436","multiplier":1.0},
    ]},
    {"underlying":"TSLA","yahoo":"TSLA","name":"Tesla","wrappers":[
        {"symbol":"TSLAx","platform":"xstocks","address":"0x8aD3c73F833d3F9A523aB01476625F269aEB7Cf0","multiplier":None},
        {"symbol":"TSLAon","platform":"ondo","address":"0x2494b603319d4D9F9715c9f4496d9E0364B59d93","multiplier":None},
        {"symbol":"TSLAB","platform":"bstocks","address":"0x5b1910eaad6450e50f816082aa078c41f10c292f","multiplier":1.0},
    ]},
    {"underlying":"AAPL","yahoo":"AAPL","name":"Apple","wrappers":[
        {"symbol":"AAPLx","platform":"xstocks","address":"0x9d275685dC284C8eB1C79f6ABA7a63Dc75ec890a","multiplier":None},
        {"symbol":"AAPLon","platform":"ondo","address":"0x390a684EF9cADE28A7AD0DFa61AB1Eb3842618c4","multiplier":None},
        {"symbol":"AAPLB","platform":"bstocks","address":"0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a","multiplier":1.0},
    ]},
    {"underlying":"META","yahoo":"META","name":"Meta","wrappers":[
        {"symbol":"METAx","platform":"xstocks","address":"0x96702be57Cd9777f835117a809C7124fe4ec989A","multiplier":None},
        {"symbol":"METAon","platform":"ondo","address":"0xD7dF5863A3e742F0c767768cDfcb63f09E0422f6","multiplier":None},
        {"symbol":"METAB","platform":"bstocks","address":"0x7425889fe94f9d693e8daefe88bcced6acfef4c0","multiplier":1.0},
    ]},
    {"underlying":"AMD","yahoo":"AMD","name":"AMD","wrappers":[
        {"symbol":"AMDx","platform":"xstocks","address":"0x3522513E5F146a2006e2901b05f16B2821485E19","multiplier":None},
        {"symbol":"AMDon","platform":"ondo","address":"0x9f16E46c73b43BDB70861247d537bEE4eA18F639","multiplier":None},
        {"symbol":"AMDB","platform":"bstocks","address":"0x75fd4cf6f8392e41e70391d60c90c0d5211603a1","multiplier":1.0},
    ]},
    {"underlying":"QQQ","yahoo":"QQQ","name":"Invesco QQQ","wrappers":[
        {"symbol":"QQQx","platform":"xstocks","address":"0xa753A7395cAe905Cd615Da0B82A53E0560f250af","multiplier":None},
        {"symbol":"QQQon","platform":"ondo","address":"0x0cdE6936d305d5B34667fC46425E852efd73559a","multiplier":None},
        {"symbol":"QQQB","platform":"bstocks","address":"0x205812cdbed920aff76c6580abd681a46d11efc7","multiplier":1.0},
    ]},
    {"underlying":"SPCX","yahoo":"SPCX","name":"SpaceX","wrappers":[
        {"symbol":"SPCXx","platform":"xstocks","address":"0x68fa48b1c2fe52b3d776e1953e0e782b5044ce28","multiplier":None},
        {"symbol":"SPCXon","platform":"ondo","address":"0xd0a58BC9D88D3FF48C0294Cb7e45937d0E41A928","multiplier":None},
        {"symbol":"SPCXB","platform":"bstocks","address":"0xbe9d156892e55e7154bcd3cb0fea677f9d3103e1","multiplier":1.0},
    ]},
]

SEEDS = {u["underlying"]: {w["platform"] for w in u["wrappers"]} for u in UNIVERSE}  # frozen at import


def apply_board(live: dict) -> None:
    """Prune UNIVERSE in place to the board set. `live` maps underlying ->
    set of lowercase wrapper addresses that have a usable tape. Underlyings not
    in `live` are dropped; seeds are always kept (and keep their seed wrappers,
    tape or not, so AAPLB still renders as \"no tape\")."""
    UNIVERSE[:] = [
        {**u, "wrappers": [
            w for w in u["wrappers"]
            if w["platform"] in SEEDS.get(u["underlying"], ())
            or (w.get("address") or "").lower() in live[u["underlying"]]
        ]}
        for u in UNIVERSE if u["underlying"] in live
    ]


def wrappers():
    out = []
    for u in UNIVERSE:
        for w in u["wrappers"]:
            out.append({**w, "underlying": u["underlying"], "yahoo": u["yahoo"], "name": u["name"]})
    return out


_PLATFORM_SUFFIX = {"xstocks": "x", "ondo": "on", "bstocks": "B"}


def _guess_symbol(underlying: str, platform: str) -> str:
    return f"{underlying}{_PLATFORM_SUFFIX.get(platform, platform)}"


def merge_dynamic(discovered: list[dict]):
    """Fold RWA Data rows into UNIVERSE in place. Each item:
    {"underlying", "name", "yahoo", "platform", "address", "symbol"(optional),
    "multiplier"(optional)}. Only ever fills in real data:
    - known underlying + known platform + seed row has no address -> patch
      that row's address in (never touches a seed row that already has one)
    - known underlying + new platform -> append a new wrapper row
    - unknown underlying -> append a new UNIVERSE entry
    Never removes a row and never fabricates an address for anything not
    present in `discovered`."""
    by_und = {u["underlying"]: u for u in UNIVERSE}
    for row in discovered:
        und = (row.get("underlying") or "").upper()
        platform = row.get("platform")
        addr = row.get("address")
        if not und or not platform or not addr:
            continue
        entry = by_und.get(und)
        if entry is None:
            entry = {"underlying": und, "yahoo": row.get("yahoo"), "name": row.get("name") or und, "wrappers": []}
            UNIVERSE.append(entry)
            by_und[und] = entry
        existing = next((w for w in entry["wrappers"] if w["platform"] == platform), None)
        if existing is None:
            entry["wrappers"].append({
                "symbol": row.get("symbol") or _guess_symbol(und, platform),
                "platform": platform, "address": addr, "multiplier": row.get("multiplier"),
            })
        elif not existing.get("address"):
            existing["address"] = addr
            if row.get("multiplier") is not None:
                existing["multiplier"] = row.get("multiplier")


def is_bnb(addr) -> bool:
    return (addr or "").lower() in (NATIVE.lower(), WBNB.lower())


def is_ondo(addr) -> bool:
    a = (addr or "").lower()
    return bool(a) and any((w.get("address") or "").lower() == a and w.get("platform") == "ondo" for w in wrappers())


def by_symbol(symbol: str):
    s = (symbol or "").upper()
    return next((w for w in wrappers() if w["symbol"].upper() == s), None)

def by_underlying(underlying: str):
    """Universe entry for an underlying ticker/name, or None. Matches on
    underlying code, Yahoo ticker, or display name (e.g. NVDA / NVDA / NVIDIA)."""
    u = (underlying or "").upper()
    return next((e for e in UNIVERSE if u in {
        (e.get("underlying") or "").upper(),
        (e.get("yahoo") or "").upper(),
        (e.get("name") or "").upper(),
    }), None)

def resolve_underlying(text: str):
    """Free text (ticker, wrapper symbol, or name) -> canonical underlying code,
    or None. NVDA / NVIDIA / NVDAx / NVDAB / NVDAon all -> 'NVDA'."""
    t = (text or "").strip().upper().lstrip("/")
    if not t:
        return None
    if by_underlying(t):
        return by_underlying(t)["underlying"]
    w = by_symbol(t)
    if w:
        return w["underlying"]
    return None

def premium(token_price, mark_price):
    if not mark_price or not token_price:
        return None
    return token_price / mark_price - 1

def format_premium(p) -> str:
    if p is None:
        return "—"
    pct = p * 100
    return f"{'+' if pct > 0 else ''}{pct:.1f}%"

def premium_status(p) -> str:
    if p is None: return "flat"
    if p > 0: return "rich"
    if p < 0: return "cheap"
    return "flat"

def session_now(now=None):
    now = now or datetime.now(timezone.utc)
    ny = now.astimezone(NY)
    open_t = ny.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = ny.replace(hour=16, minute=0, second=0, microsecond=0)
    if ny.weekday() >= 5:
        label = "WEEKEND"
    elif ny < open_t:
        label = "PRE-MARKET"
    elif ny >= close_t:
        label = "AFTER-HOURS"
    else:
        label = "CASH OPEN"
    wat = now.astimezone(WAT)
    utc = now.astimezone(timezone.utc)

    next_close = close_t if ny < close_t else close_t + timedelta(days=1)
    while next_close.weekday() >= 5:
        next_close += timedelta(days=1)
    next_close_wat = next_close.astimezone(WAT)
    ny_close_in_sec = max(0, int((next_close - ny).total_seconds()))

    return {
        "label": label,
        "cashOpen": label == "CASH OPEN",
        "ny": ny.isoformat(),
        "utc": utc.isoformat(),
        "wat": wat.strftime("%H:%M"),
        "et": ny.strftime("%H:%M"),
        "nyCloseAtWat": next_close_wat.strftime("%H:%M"),
        "nyCloseInSec": ny_close_in_sec,
    }

_snapshot_cache = {"fetchedAt": None, "session": None, "tokens": [], "groups": []}

def _earnings_snapshot():
    try:
        import earnings
        return {e["underlying"]: {"earningsDate": e["earningsDate"], "hoursUntil": e["hoursUntil"],
                                  "inside24h": e["inside24h"]} for e in earnings.all_info()}
    except Exception:
        return {}

def get_cached_snapshot():
    return _snapshot_cache

def set_cached_snapshot(tokens, groups=None, tape_stale=False):
    global _snapshot_cache
    _snapshot_cache = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "session": session_now(),
        "tokens": tokens,
        "groups": groups or [],
        "tapeStale": tape_stale,
        "earnings": _earnings_snapshot(),
    }
    return _snapshot_cache
