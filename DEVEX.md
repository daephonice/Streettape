# StreetTape — Developer Experience Report

Mid-build notes, frozen 2026-09-29. Written from live calls, not from the docs after the fact.

This file lives in the repo so a Railway deploy cannot wipe it. The download at `GET /api/_devex` is a *different* file: it is whatever `devlog.py` wrote on that container, and it resets on every deploy. Do not treat `/api/_devex` as this report. When a feature is ready to test, download `/api/_devex`, cut it down to the new calls for that feature, and paste them under **Runtime log** at the bottom.

- Product: https://streettape.up.railway.app
- Agent registration: https://streettape.up.railway.app/agent-registration.json
- Chain: BSC mainnet (`binanceChainId=56`)
- Stance: quote, flag, alert. The user signs. Nothing auto-executes.

---

## Summary

- Built against Binance Web3 quote/RFQ/swap-build, RWA Data, Transaction sim, Address Portfolio, GeckoTerminal tape, Yahoo last cash print, BSC RPC.
- Verified live: aggregator quotes and built SWAP transactions (LiquidMesh), RFQ-requires-wallet, RWA `platforms` / `search` / `underlying-profile` / `underlying-market`, rate-limit behaviour, ERC-8004 registration on BSC, one settled TSLAB buy.
- Called live, failed: `POST /api/v1/dex/aggregator/tx/simulate` HTTP 404; `GET /api/v1/portfolio/tokens` HTTP 202 non-JSON (19/19); `rwa/price` no usable mark on 420/420 calls in the logged window (missing `binanceChainId`, then 429).
- Not verified live: `baw` signing against an MPC session.

---

## Time to first useful call

| Clock | What happened |
|---|---|
| Process start + 5.3 s | Signed `GET /api/v1/dex/market/rwa/platforms`. HTTP 200, 2 platforms, 115 ms. |
| Same minute | `rwa/tokens` and `rwa/search?keyword=NVDA` also 200. |
| Same minute | First `rwa/price` returned HTTP 200 with `code=40001 Parameter binanceChainId is required`. Docs listed `tokenAddress`. They did not list the chain id every sibling endpoint demands. |
| Same session | First aggregator quote that priced a size: BNB → TSLAB via LiquidMesh, ~100 ms. |

First successful signed call was platforms. First useful price was not RWA Data. It was Yahoo + Gecko.

---

## Findings (2026-09-29)

### Region block

Valid keys from Railway's default region returned `Service not available due to compliance restriction` on `platforms`, `search`, `underlying-profile`, and `underlying-market`. Not a 401. Moving the service to Singapore made the same keys work. Docs we used did not list allowed regions next to "create a key." Cost: one failed deploy cycle.

### Errors name one missing field at a time

| Call | First error | What actually worked |
|---|---|---|
| `underlying-profile` | `Parameter tokenContractAddress is required` | ticker is not enough |
| `underlying-market` | `Parameter binanceChainId is required` | send `tokenContractAddress` + `binanceChainId=56` |
| `rwa/price` | `Parameter binanceChainId is required` | we sent `tokenAddress` only. 213 times |

A validation miss is HTTP 200 + `code=40001`. A rate limit is HTTP 429 + `code=42900`. Clients that only look at HTTP status treat the first class as success. Ours did, until we checked `success` / `code`.

### `rwa/price` produced no mark in the logged window

420 calls. 213 missing `binanceChainId`. 207 rate-limited. Official tape and official mark did not come from RWA Data. Gecko (tape) and Yahoo (last cash print) carried the board. After we started sending `binanceChainId=56`, this still needs a clean confirmation pass. Until that pass is green, the UI must not label a Yahoo print as an official Binance mark.

### Rate limits recover in under a second and are unpublished

Five RWA calls passed. The next eight, ~80 ms apart, all 429. The ninth, 84 ms later, passed. Spot API limit pages do not cover `web3.binance.com`. Seven `underlying-market` calls in a tight loop lost QQQ and SPCX; a 2 s gap got all seven. App now waits 0.3 s between price calls and retries a 429 once after 1 s.

### `underlying-profile` / `underlying-market` are company data, not token data

Queried NVDAB (`0x02fca66c1d1afb4e2a7884261eb00f63598a7436`):

- `tokenToShareRatio` = `1.000778223752807865`, not `1.0`. Seed catalog stores `multiplier: 1.0` for bStocks. About 0.08% off in share-normalized arb. Not yet wired into the math.
- `marketData.marketCap` is NVIDIA the company (~$5.5T), not the wrapper. Shown in the app as "Underlying mcap."
- `totalShares` is `null` on the bStocks address and `24147000000` on the xStocks address for the same underlying. One wrapper is not a source of truth for a field.
- `statusInfo.reasonCode=TRADING` exists; `nextOpenTime` / `nextCloseTime` were null in the call we have, so session math stays in `rwa.session_now()`.
- `protections.collateralReport.supported` is true; `description` and `url` are null.

### Quotes: AMM works without a wallet. RFQ does not.

- AMM / LiquidMesh, no `userWalletAddress`: price, `priceImpactPct`, `needsWallet: true`, `transaction: null`. Fine for a public board.
- Ondo or xStocks, no wallet: `code=40001 userWalletAddress is required for RFQ (Ondo)` / `(xStock)`. HTTP 200. Fallback is a PancakeSwap link and a 5 bps cost floor. That floor is a guess, not a fill.
- Same pair with a taker builds a real tx (`from`, `to`, `data`, `value`, `gas`, `gasPrice`). Swap-build ~98 ms.

After-hours `/api/agent/studio/tick` at $50 notionals: cross-wrapper gaps SPCX 3.5%, TSLA 3.1%, META 1.8%, NVDA 1.1%. Wrappers of the same name stop tracking each other off hours. That is the desk.

### `priceImpactPct` unit is unverified

Values at a few dollars: `0.00023` to `0.00301`, sign flips. Code comments treat it as a fraction, then does `abs(x) * 100`. If the API returns a fraction, cost is understated 100x. If it returns percent, the conversion is correct for bps. Not confirmed from docs or a large-size quote. Net-bps in the agent is provisional. The 5 bps floor on unknown-impact legs is what keeps the numbers from looking insane.

### Transaction simulate path does not exist on this key

`POST /api/v1/dex/aggregator/tx/simulate` — two calls, real taker, built SWAP tx — HTTP 404 in ~100 ms both times. Early logger treated "no code field" as success. That was our bug. No documented replacement found. Pre-sign check in use is the wallet's own simulation.

After US close the wallet flagged a Binance-built BNB to NVDAB swap "likely to fail." Cancelled. A later TSLAB buy settled; holdings then showed via public RPC. Simulate is parked for an hour after a 404.

### Address Portfolio is not a portfolio

`GET /api/v1/portfolio/tokens` on `web3.binance.com/wallet` — 19/19 HTTP 202, non-JSON, 10-40 ms. Holdings in the UI come from public BSC `eth_call` / `eth_getBalance`. Parked for 10 minutes after a failure.

### Agentic Wallet skill is a CLI, not a library

- `npx skills add binance-agentic-wallet` fails. The CLI wants the GitHub folder path under `binance/binance-skills-hub`.
- On Termux the same command needs `git` on PATH, then a Node/OpenSSL upgrade.
- The skill wraps `baw market-order quote` / `swap`. It does not sign from Python.
- StreetTape calls `baw` when it is on PATH, else `/api/swap/order`. `--sign` is explicit. `baw` against a live MPC session has not been run.

### ERC-8004 identity. No Agent Studio runtime.

Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (BSC, ERC1967 proxy). Three unlabeled `register` overloads on BscScan. Used the `agentURI`-only one. Token / agent id **360062**. Fee ~0.000009 BNB. Registration file is served at `/agent-registration.json`.

Agent Studio full deploy was not built. The special is for a self-funding seller on their runtime (x402, ERC-8183). StreetTape has no paid endpoint and never signs.

### Multiplier and missing contracts

- Seed: bStocks `multiplier: 1.0`, xStocks/Ondo `None`. Arb divides only when the value is truthy. `normalized` is true if *either* wrapper has a multiplier, so NVDAB vs NVDAon reports normalized even though one side was raw.
- AAPLB seed address is `None`. `merge_dynamic()` fills it only if RWA Data lists a contract. Until then: no tape, no Buy, not in an arb. We do not invent an address.

---

## How the assets behaved

- **bStocks** quoted and filled on AMM after hours. TSLAB buy settled. NVDAB build after close was flagged likely-to-fail by the wallet. Cause not proven (liquidity vs transfer limits vs stale route).
- **Ondo / xStocks** quotes without a taker are RFQ and die. No completed RFQ fill yet. Only the required-wallet error.
- **xStocks tape on Gecko** is often missing for names that have a contract. No tape means no Buy and no arb leg.
- **SpaceX** has no Yahoo mark. Premium vs official is blank. Cross-wrapper gap still exists (SPCXon rich vs SPCXB).

---

## If I rebuilt the developer platform

1. One landing example: signed GET that returns official mark + on-chain tape + `tokenToShareRatio` + `binanceChainId` already in the query.
2. Allowed-regions line next to "create a key."
3. Error bodies that list every missing field, not one per round trip.
4. HTTP status that matches the body (`400` for `40001`, not `200`).
5. A published burst limit for RWA Data and the aggregator.
6. `priceImpactPct` unit named in the field description.
7. Transaction simulate documented as a real path, or removed from the catalog so people stop guessing `/tx/simulate`.
8. Address Portfolio returns JSON holdings or a JSON error. Not HTTP 202 with an empty body.
9. RFQ error that also returns the AMM quote as a sibling, so a public board does not invent a Pancake link.
10. Registry UX that prints "agent id = token id" instead of making you read it off the ERC-721 transfer.

---

## Open on our side

- Confirm `rwa/price` with `binanceChainId=56` (and `tokenContractAddress` if needed).
- Confirm `priceImpactPct` unit with a large-size quote.
- Wire live `tokenToShareRatio` into arb math. Fix `normalized` so both legs must have a ratio.
- Do not retry simulate or Address Portfolio until a documented path exists.
- One live `baw market-order quote` against a signed-in session.

---

## Runtime log

Empty on purpose.

`/api/_devex` will keep filling with Yahoo polls and `balanceOf` reads on every deploy. That download is disposable.

When a feature is ready to test, run it against the live app, download `/api/_devex`, keep only the calls that belong to that feature (first occurrence of each outcome is enough), and paste a block below.

Template:

```
### YYYY-MM-DD — <feature>

- Session: CASH OPEN | AFTER-HOURS | WEEKEND
- What we hit:
- What came back:
- What we changed because of it:
```

### 2026-09-29 — first live stack pass

Already written up in Findings. Not pasted again. Counts from that container, for the record only: 420 `rwa/price` with no mark, 19 portfolio 202s, 2 simulate 404s, 13 good aggregator quotes, 18 RFQ-without-wallet errors, 2 swap builds, 1 settled TSLAB buy.

### 2026-09-29 — desk front door (`/`, hero rotate line, Lend removed)

- Session: <fill: WEEKEND | AFTER-HOURS | PRE-MARKET | CASH OPEN>
- What we hit: <fill: first `GET /api/agent/scan` (quotes both legs per hit), first `GET /api/agent/arb/{underlying}?size_usd=50`, first swap quote after Rotate>
- What came back: <fill: from /api/_devex — first occurrence of each outcome: good quote, RFQ-needs-wallet 40001, 429, 404>
- What we changed because of it: <fill>
