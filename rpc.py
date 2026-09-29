"""BSC JSON-RPC with sticky failover.

Order: BSC_RPC_URL (if set) → official → PublicNode → dRPC → official alt.
A 429 / timeout / 5xx parks that URL for COOLDOWN seconds, then the next one is tried.
"""
from __future__ import annotations

import os
import time
import logging

import httpx

import devlog

log = logging.getLogger("rpc")

_SIM_METHODS = {"eth_call", "eth_estimateGas", "eth_sendRawTransaction"}

PUBLIC_RPCS = (
    "https://bsc-dataseed.binance.org",
    "https://bsc-rpc.publicnode.com",
    "https://bsc.drpc.org",
    "https://bsc-dataseed.bnbchain.org",
)
COOLDOWN = 45.0
_parked = {}
_live = None
_client = None
_id = 0


def endpoints():
    custom = (os.getenv("BSC_RPC_URL") or "").strip().rstrip("/")
    out = []
    if custom:
        out.append(custom)
    for url in PUBLIC_RPCS:
        if url.rstrip("/") not in out:
            out.append(url)
    return out


def _http():
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=8.0)
    return _client


def _ready():
    now = time.time()
    return [u for u in endpoints() if _parked.get(u, 0) <= now]


def _park(url: str, why: str):
    _parked[url] = time.time() + COOLDOWN
    global _live
    if _live == url:
        _live = None
    log.warning("rpc: park %s (%s)", url, why)


def _rate_limited(status: int, body) -> bool:
    if status in (429, 502, 503, 504):
        return True
    if not isinstance(body, dict):
        return False
    err = body.get("error") or {}
    msg = str(err.get("message") or "").lower()
    return any(s in msg for s in ("rate", "limit", "too many", "capacity", "overloaded"))


async def call(method: str, params):
    global _id, _live
    urls = _ready()
    if _live in urls:
        urls = [_live] + [u for u in urls if u != _live]
    if not urls:
        urls = list(endpoints())
        _parked.clear()

    tag = f"rpc {method}"
    last = None
    for url in urls:
        _id += 1
        try:
            with devlog.timed() as t:
                resp = await _http().post(url, json={"jsonrpc": "2.0", "id": _id, "method": method, "params": params})
            try:
                body = resp.json()
            except Exception:
                body = None
            if _rate_limited(resp.status_code, body):
                devlog.log_call(what=tag, url=url, status=resp.status_code, ms=t.ms,
                                 expected="200 with result", actual="rate limited, parking endpoint")
                _park(url, f"http {resp.status_code}")
                last = RuntimeError(f"rpc {resp.status_code} at {url}")
                continue
            resp.raise_for_status()
            if not isinstance(body, dict):
                devlog.log_call(what=tag, url=url, status=resp.status_code, ms=t.ms,
                                 expected="JSON object with result", actual="non-dict body", body=str(body))
                last = RuntimeError(f"bad json at {url}")
                _park(url, "bad json")
                continue
            if body.get("error"):
                devlog.log_call(what=tag, url=url, status=resp.status_code, ms=t.ms,
                                 expected="result, no error", actual="RPC-level error", body=str(body.get("error")))
                last = RuntimeError(body["error"])
                if _rate_limited(200, body):
                    _park(url, "rpc rate limit")
                    continue
                raise last
            devlog.log_call(what=tag, url=url, status=resp.status_code, ms=t.ms,
                             expected="result", actual=f"result={_snip_result(body.get('result'))}")
            _live = url
            return body["result"]
        except httpx.HTTPError as e:
            devlog.log_call(what=tag, url=url, status="exception", ms=0, error=str(e))
            last = e
            _park(url, type(e).__name__)
        except RuntimeError:
            raise
        except Exception as e:
            devlog.log_call(what=tag, url=url, status="exception", ms=0, error=str(e))
            last = e
            _park(url, type(e).__name__)
    raise last or RuntimeError("all BSC RPCs failed")


def _snip_result(r):
    s = str(r)
    return s if len(s) <= 120 else s[:120] + "…"
