---
name: streettape-desk
description: Use in an agentic wallet for tokenized-stock questions on BNB Chain via a closed grammar of exact sentences (rich vs Friday, rotate, flatten, AI basket, stand down). Calls the StreetTape APIs; never computes prices itself.
dependencies:
  - binance/binance-skills-hub/skills/binance-web3/binance-agentic-wallet
---

# streettape-desk

Base URL: `https://streettape.up.railway.app` (override with `STREETTAPE_URL`).
All prices, premiums, gaps, costs and quotes come from the API. Never estimate them. Quote, don't sign: trades are confirmed and signed in the user's own wallet.

Tool: `python skills/streettape.py <command>` (stdlib only, prints JSON).
Quote/sign goes through the official `binance-agentic-wallet` skill (`baw market-order quote|swap`) when `baw` is installed and signed in; otherwise StreetTape's `/api/swap/order` builds the quote. Signing only with `quote ... --sign` after the user says yes: it quotes, then signs via `baw` (no API fallback), polls to FINISHED/FAILED, and exits 1 unless FINISHED.

| User says | Command | Endpoint |
|---|---|---|
| "what's rich vs Friday" | `board [--min 0.01]` | `GET /api/board` |
| "quote $50 flatten" | `flatten [--usd 50] [--pct 0.02] [--taker 0x..]` | `GET /api/board` + `POST /api/swap/order` |
| "rotate into cheapest NVDA" | `rotate NVDA` | `GET /api/agent/scan?underlying=NVDA` |
| "alert only cash-shut" | `alerts [--threshold 0.015]` | `GET /api/agent/scan` |
| "AI basket" | `basket [--usd 50]` | `GET /api/basket/ai` |
| closed grammar, exact sentences only (below) | `say "<sentence>"` | dispatches to the rows above |
| "verify the quote" (same pair, `baw` vs API, never signs) | `verify <inputMint> <outputMint> <uiAmount> [--taker 0x..] [--tol 50]` | `baw market-order quote` + `POST /api/swap/order` |
| raw quote | `quote <inputMint> <outputMint> <uiAmount> [--taker 0x..] [--sign]` | `POST /api/swap/order` |

## Closed grammar (`say`)
No model call, no free-text fallback. Anything else exits 1 with `{"error":"unrecognized","accepted":[...]}`. Numbers and tickers may vary inside a pattern.

| Sentence | Calls |
|---|---|
| `what's rich vs Friday` | `GET /api/board` |
| `rotate into cheapest NVDA` | `GET /api/agent/scan?underlying=NVDA` |
| `alert only while cash is shut` | `GET /api/agent/scan` |
| `flatten anything richer than 2% while cash is shut` | `GET /api/agent/scan?threshold=0.02`; sell quotes only for hits above 2% and only if `cashOpen` is false |
| `rotate NVDA if net gap clears 1%` | `GET /api/agent/arb/NVDA`; legs printed only if `viable` and `netBps` >= 100 |
| `buy the AI basket for $50` | `GET /api/basket/ai?usd=50` |
| `stand down NVDA into USDT if earnings are inside 24h` | `GET /api/earnings?underlying=NVDA`; sell quotes only if `hoursUntil` is between 0 and the stated hours (Yahoo calendar date; no date means no stand-down, never inferred from headlines) |

## Rules
- **verify**: prints both quotes (`baw` and API) with `uiOutAmount`, `route`, `priceImpactPct`, `deltaBps` and a verdict. Exit 1 unless `SAME ORDER`. If `baw` fails the verdict says so; never report a verified quote without a live `baw` result.
- **rich vs Friday**: report `symbol`, `tape`, `official`, `fair`, `premium` per token (tape vs last cash-print mark; Friday close is the mark while cash is shut). Positive = rich, negative = cheap. Show `session.label`.
- **flatten**: sells every wrapper with `premium > 2%` into USDT, $50 each by default. Show `uiOutAmount`, `routeLabel`, `priceImpactPct` (fraction). If `noRoute` is true, say there is no route.
- **rotate**: sell the rich wrapper, buy the cheap one. Print `viable`, `netBps`, both legs and both ratios. If `viable` is false, say costs eat the gap and do not push the trade. If there is no arb, say so.
- **alerts**: only alert when `session.cashOpen` is false. When cash is open, output nothing.
- **basket**: equal-weight NVDA/AMD/META, USD split three ways, one USDT buy quote per name into the cheapest wrapper with a tape and a share ratio. Show `symbol`, `platform`, `contract`, `ratio`, `amountUsd`, `outAmount` per leg. A name with no tape is `unfilled`: say so and show its weight as unfilled, never reassign it to the other two. Only this one theme exists.
- Cooldown: don't repeat an alert for the same symbol within 60 minutes.
- Every command ends with: not auto-executed, confirm and sign in your own wallet. (`alerts` prints nothing while cash is open.)
