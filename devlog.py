"""DEVEX.md logger. Appends one Markdown entry per real official-API call
(Binance Web3 quote/RFQ/submit, BSC RPC, price marks) as it actually happens
at runtime — never authored after the fact. Call log_call() at the point
where the response/exception is known. Toggle with DEVEX_LOG=1.

t0() marks time-to-first-call from process start; call it once in main.py
before any of the startup tasks fire.
"""
from __future__ import annotations

import os
import re
import time
import logging
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger("devlog")

PATH = os.getenv("DEVEX_LOG_PATH", "DEVEX.md")
ENABLED = os.getenv("DEVEX_LOG", "1") != "0"

_process_start = time.monotonic()
_first_call_logged = False

# Polling loops (price/mark/balance refreshes) repeat identical calls hundreds of
# times. Keep the first MAX_PER_SIGNATURE of each distinct (label, status, outcome)
# and only count the rest; summary_md() reports the counts.
MAX_PER_SIGNATURE = int(os.getenv("DEVEX_MAX_PER_SIGNATURE", "3"))
_sig_counts: Counter = Counter()

_HEADER = (
    "# DEVEX.md — StreetTape build log\n\n"
    "One entry per official-API call made while building/testing against "
    "real endpoints. Not curated after the fact.\n\n"
)


def _ensure_file():
    if not os.path.exists(PATH):
        with open(PATH, "w") as f:
            f.write(_HEADER)


def t0(label: str = "process start"):
    """Call once, as early as possible (top of main.py), to anchor
    time-to-first-call measurements."""
    global _process_start
    _process_start = time.monotonic()


def _snip(body, n=300) -> str:
    if body is None:
        return ""
    s = body if isinstance(body, str) else repr(body)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + f"…[{len(s) - n} more chars]"


def log_call(
    *,
    what: str,
    url: str,
    status,
    ms: float,
    expected: str = "",
    actual: str = "",
    body: str | None = None,
    error: str | None = None,
):
    """Append one entry. Fire-and-forget: logging failures never raise.

    what     - short label, e.g. "rwa/price signed fetch", "RFQ quote NVDA",
               "AMM quote NVDA", "tx sim", "RFQ submit"
    url      - full request URL (or path) actually hit
    status   - HTTP status code, or "n/a" / exception class name
    ms       - elapsed wall time in milliseconds
    expected - one line: what the docs said would happen
    actual   - one line: what actually happened
    body     - raw response/error body, snipped to 300 chars
    error    - traceback-free error string, if any
    """
    if not ENABLED:
        return
    global _first_call_logged
    sig = (what, str(status), re.sub(r"0x[0-9a-fA-F]{6,}|\d+", "#", actual or error or "")[:80])
    _sig_counts[sig] += 1
    if _sig_counts[sig] > MAX_PER_SIGNATURE:
        return
    try:
        _ensure_file()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [f"## {now} — {what}", f"- URL: `{url}`", f"- Status: `{status}`  Latency: `{ms:.0f}ms`"]
        if expected:
            lines.append(f"- Docs said: {expected}")
        if actual:
            lines.append(f"- Actually happened: {actual}")
        if error:
            lines.append(f"- Error: `{_snip(error)}`")
        if body:
            lines.append(f"- Body: `{_snip(body)}`")
        if not _first_call_logged:
            elapsed = time.monotonic() - _process_start
            lines.append(f"- Time-to-first-call from process start: `{elapsed:.1f}s`")
            _first_call_logged = True
        lines.append("")
        with open(PATH, "a") as f:
            f.write("\n".join(lines) + "\n\n")
    except Exception:
        log.warning("devlog write failed", exc_info=True)


class timed:
    """Context manager that measures elapsed ms.
    Usage: with devlog.timed() as t: ... ; log_call(ms=t.ms, ...)"""

    def __enter__(self):
        self._start = time.monotonic()
        self.ms = 0.0
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ms = (time.monotonic() - self._start) * 1000
        return False


def summary_md() -> str:
    """Markdown table of every distinct call signature seen this process, with
    total counts (including the repeats that were not written out)."""
    if not _sig_counts:
        return ""
    rows = ["", "## Call summary (this process, all calls including repeats)", "",
            "| Calls | Label | Status | Outcome |", "|---|---|---|---|"]
    for (what, status, outcome), n in sorted(_sig_counts.items(), key=lambda x: -x[1]):
        rows.append(f"| {n} | {what} | {status} | {outcome or 'ok'} |")
    return "\n".join(rows) + "\n"
