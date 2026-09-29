---
name: streettape-desk
description: Use in an agentic wallet for tokenized-stock questions on BNB Chain — "what's rich vs Friday", "quote $50 flatten", "rotate into cheapest NVDA", "alert only cash-shut". Calls the StreetTape APIs; never computes prices itself.
dependencies:
  - binance/binance-skills-hub/skills/binance-web3/binance-agentic-wallet
---

# streettape-desk

Base URL: `https://streettape.up.railway.app` (override with `STREETTAPE_URL`).
All prices, premiums, gaps, costs and quotes come from the API. Never estimate them. Quote, don't sign: trades are confirmed and signed in the user's own wallet.

Tool: `python skills/streettape.py <command>` (stdlib only, prints JSON).
Quote/sign goes through the official `binance-agentic-wallet` skill (`baw market-order quote|swap`) when `baw` is installed and signed in; otherwise StreetTape's `/api/swap/order` builds the quote. Signing only with `quote ... --sign` after the user says yes; it then polls the order to FINISHED/FAILED.

| User says | Command | Endpoint |
|---|---|---|
| "what's rich vs Friday" | `board [--min 0.01]` | `GET /api/board` |
| "quote $50 flatten" | `flatten [--usd 50] [--taker 0x..]` | `GET /api/board` + `POST /api/swap/order` |
| "rotate into cheapest NVDA" | `rotate NVDA` | `GET /api/agent/scan?underlying=NVDA` |
| "alert only cash-shut" | `alerts [--threshold 0.015]` | `GET /api/agent/scan` |
| raw quote | `quote <inputMint> <outputMint> <uiAmount> [--taker 0x..] [--sign]` | `POST /api/swap/order` |

## Rules
- **rich vs Friday**: report `premium` per token (tape vs last cash-print mark; Friday close is the mark while cash is shut). Positive = rich, negative = cheap. Show `session.label`.
- **flatten**: sells every wrapper with `premium > 2%` into USDT, $50 each by default. Show `uiOutAmount`, `provider`, `priceImpactPct`. If `provider` is `pancake`, present `deepLink` instead of a number.
- **rotate**: sell the rich wrapper, buy the cheap one. Use `viable` and `netBps` from the API. If `viable` is false, say costs eat the gap and do not push the trade. If there is no arb, say so.
- **alerts**: only alert when `session.cashOpen` is false. When cash is open, output nothing.
- Cooldown: don't repeat an alert for the same symbol within 60 minutes.
- Never auto-execute. Always end with: confirm and sign in your own wallet.
