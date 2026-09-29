# DEVEX.md — StreetTape build log

One entry per official-API call made while building/testing against real endpoints. Not curated after the fact.

Entries under **Runtime log** append automatically (see `devlog.py`). Everything above it is written by hand, and each item says whether it came from a live response, from docs, or from code.

## Summary

- Deployed on Railway, live at `streettape.up.railway.app`, BNB Chain (56).
- Built against: Binance Web3 quote/RFQ, RWA Data, Transaction sim, Address Portfolio; GeckoTerminal (tape); Yahoo (last cash print); BSC RPC.
- Proposal-only by design: the app quotes, flags and alerts; the user signs in their own wallet.
- Verified against live responses (see below): Binance Web3 quotes, RFQ wallet requirement, rate limiting, ERC-8004 registration on BSC.
- **Not verified live:** Transaction sim, Address Portfolio, `baw` signing. Marked pending below.

## Live observations (2026-09-29, after-hours session)

Source: one real response from the deployed `/api/agent/studio/tick` (4 arb candidates, 8 leg quotes at $50 size).

1. **RFQ quotes need a wallet.** Sell legs on Ondo and xStock wrappers (SPCXon, TSLAx) failed with `userWalletAddress is required for RFQ (Ondo)` / `(xStock)`. Without a taker address there is no RFQ price at all, so the app falls back to a PancakeSwap deep link with no price impact. Fix on our side: mark these legs as fallback and charge a 5 bps cost floor. Costs us accuracy in the no-wallet case.
2. **AMM quotes work without a wallet.** Binance Web3 legs (provider `binance_web3`, routed via LiquidMesh) return `uiOutAmount`, `priceImpactPct` and `needsWallet: true`, but `transaction: null` until a taker is supplied. Raw `priceImpactPct` values seen: -0.000463 (SPCX buy), 0.000230 (TSLA buy), 0.002520 (META sell). The sign varies between quotes, so we use the absolute value. **Unit unverified:** `agent.py` comments it as a fraction (0.004 = 0.4%) but converts with `abs(x) * 100`, which would be percent-to-bps, not fraction-to-bps (that needs `* 10000`). If the API really returns a fraction, our impact cost is understated 100x; the 5 bps floor per leg is what keeps the net-bps figures plausible. I have not confirmed the unit from Binance docs or a large-size quote.
4. **`sim` is `null` on every leg.** Expected, since the Transaction sim needs a built tx and there is no taker in an unauthenticated scan. So this scan did not exercise the Transaction API at all.
5. **Real gaps exist after hours.** Cross-wrapper gaps of 1.1% to 3.5% on the same underlying (SPCX 3.5%, TSLA 3.1%, META 1.8%, NVDA 1.1%). After-hours wrappers stop tracking each other, which is the whole thesis of the app.

## Answers to the specific asks

- **`multiplier` None vs 1.0.** In our seed catalog the bStocks wrappers carry `1.0` and xStocks/Ondo carry `None`. `board.py` prefers RWA Data's multiplier when it is not `None`, else the seed value. In the arb scan we divide price by the multiplier only when it is truthy; `None` means raw tape price is used, and the Telegram message says so. **Known weakness:** the `normalized` flag is true if *either* wrapper has a multiplier, so a pair like NVDAB (1.0) vs NVDAon (None) reports `normalized: true` even though one side was not normalized. Every pair in the tick above showed `normalized: true`.
- **`AAPLB` missing address.** The seed row has `address: None`. `rwa.merge_dynamic()` fills it from RWA Data if that lists a contract; otherwise it stays `None`, the board shows "no tape", there is no Buy button, and it never enters an arb (arbs require `hasTape`). We never invent an address.

## Binance Agentic Wallet skill

- **What I hit:** `npx skills add binance-agentic-wallet` fails with `fatal: repository 'binance-agentic-wallet' does not exist`. The skills CLI wants a GitHub source, not the bare skill name. The working form is the full path to the skill folder in `binance/binance-skills-hub` (`.../skills/binance-web3/binance-agentic-wallet`). Docs and my first guess disagreed, so this cost me a round trip.
- **Missing git:** on a phone (Termux) the same command first failed with `spawn git ENOENT` because the CLI shells out to `git`. Then Termux's Node crashed on an OpenSSL symbol mismatch until `pkg upgrade` ran. Environment friction, not Binance's fault, but it is the first thing a mobile-only developer meets.
- **What the skill actually is:** a wrapper over the `baw` CLI (`market-order quote`, `market-order swap`, `--json` output). It is not a signing library you call from Python.
- **How StreetTape uses it:** `skills/streettape.py` calls `baw market-order quote` when `baw` is on PATH, otherwise falls back to our own `/api/swap/order`. Signing (`swap`) only runs with an explicit `--sign` after the user confirms, and it polls `market-order list` until the order is FINISHED or FAILED, because per the docs a returned `orderId` only means submitted.
- **Not verified:** I have not run `baw` against a real signed-in MPC wallet. The wrapper was only exercised against a stub that mimics the documented JSON shape. Treat the `baw` path as untested on live funds.

## ERC-8004 identity (BSC mainnet)

- Registered manually through BscScan, Write as Proxy, on the Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (ERC1967 proxy, verified).
- The ABI lists **three `register` overloads** (no args; `agentURI` + `metadata`; `agentURI` only). BscScan shows them unlabeled apart from their selectors, so you have to know which one you want. I used the `agentURI`-only one.
- Result: token (agent ID) **360062**, minted from the null address to my wallet. Fee was 0.000008895 BNB (about $0.007).
- The registration file is served by the app at `/agent-registration.json` and, after registering, lists `registrations: [{agentId: 360062, agentRegistry: eip155:56:0x8004…a432}]`.
- Friction: nothing in the transaction page says "agent ID"; you read it off the ERC-721 transfer as the token ID.

## Scoped decision: BNB Agent Studio full deploy — not built

I read the Agent Studio docs and the bounty wording. The special rewards a self-funding seller agent running on Studio's runtime (AWS Bedrock AgentCore, ERC-8183 tasks, x402 payments). StreetTape is deliberately propose-only: it never signs and has no paid endpoint, so there is nothing for x402 to charge for. Building it would mean adding a product feature only to qualify, plus AWS credentials and a Node/`bag` toolchain I could not run from a phone. I did **not** install or run `bag`. I registered the ERC-8004 identity by hand instead (above).

## Pending: first Transaction API sim

`swap.simulate_transaction()` fires on every SWAP-mode `quote()` that has a built tx (see `swap.py`), logged as `what="tx sim"`. No entry below yet: the live scan above has no taker, so `sim` is `null` everywhere. The endpoint path (`POST /api/v1/dex/aggregator/tx/simulate`) is my best reading of the Trading API family and is **unverified** against a real response. The first real entry will appear in the runtime log; do not hand-write one in its place.

## Pending: first Address Portfolio lookup

`portfolio.get_token_holdings()` calls `GET /api/v1/portfolio/tokens` on `https://web3.binance.com/wallet` and is logged automatically. Path and response shape are unverified against a live call. First real entry replaces this placeholder.

## Runtime log (auto-appended)

