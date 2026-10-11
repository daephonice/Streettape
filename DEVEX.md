# StreetTape — Developer Experience Report

Mid-build notes, last updated 2026-10-10. Written from the calls I actually made, not from the docs after the fact.

This file lives in the repo so a Railway deploy cannot wipe it. The download at `GET /api/_devex` is a different file: it is whatever `devlog.py` wrote on that container, and it resets on every deploy. Do not treat `/api/_devex` as this report.

- Product: https://streettape.up.railway.app
- Agent registration: https://streettape.up.railway.app/agent-registration.json
- Chain: BSC mainnet (`binanceChainId=56`)
- Stance: Quote, flag, and alert. The user signs. Nothing auto-executes.

> ## If I owned the platform
>
> 1. **`binanceChainId` is required on `rwa/price` and the docs must say so**, next to `tokenContractAddresses` (plural), like every sibling endpoint. It cost me 213 failed calls.
> 2. **Return non-200 on validation failure.** `40001` arrives as HTTP 200 today, so any client that checks status treats it as success. Use `400`.
> 3. **Publish the rate limit.** `web3.binance.com` has none listed. I observed: 5 pass, next 8 at ~80 ms return `429 code=42900`, recovery in under 1 s.
> 4. **Name the unit of `priceImpactPct` in the field description.** It is a fraction (`0.0005` = 5 bps), not a percent, and on TSLAB it falls as size grows.
> 5. **Whitelist developer-origin cloud IPs, or say outright that serverless deploys cannot call RFQ.** Valid keys on Railway's default region got `compliance restriction`; Singapore fixed it. RFQ also needs `userWalletAddress`; the error should list every missing field and return the AMM quote beside it.

---

## Summary

I built StreetTape against Binance Web3 quote/RFQ/swap-build, RWA Data, Transaction sim, Address Portfolio, GeckoTerminal tape, Yahoo last cash print, and BSC RPC.

What I verified live:

- Aggregator quotes and built SWAP transactions (LiquidMesh).
- RFQ requires a wallet.
- RWA `platforms` / `search` / `underlying-profile` / `underlying-market`.
- Rate-limit behaviour.
- ERC-8004 registration on BSC (agent 360062).
- One settled TSLAB buy: [0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5](https://bscscan.com/tx/0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5).
- One signed `baw market-order swap` (2026-10-10): 0.0027 BNB to TSLAB, status FINISHED, [0x3f2e027a3c356783cb39be21b69cb0959d15fed3c2f13dfc09f79b4ba7d2161a](https://bscscan.com/tx/0x3f2e027a3c356783cb39be21b69cb0959d15fed3c2f13dfc09f79b4ba7d2161a).
- Settled Rotate (2026-10-10): NVDAon to NVDAB through Pancake, [0x160d05c5ea2c347d70b5da4498ac254732f28554ade688008970478d20bbd8b7](https://bscscan.com/tx/0x160d05c5ea2c347d70b5da4498ac254732f28554ade688008970478d20bbd8b7); second run [0x7a0b6cdf2d47f382529693522abc4dba6e7dde0927b3907712d2ad02aa0562f9](https://bscscan.com/tx/0x7a0b6cdf2d47f382529693522abc4dba6e7dde0927b3907712d2ad02aa0562f9).

What I called live and failed:

- `POST /api/v1/dex/aggregator/tx/simulate` — HTTP 404.
- `GET /api/v1/portfolio/tokens` — HTTP 202 non-JSON (19/19).
- `rwa/price` — no usable mark on 420/420 calls in the logged window (missing `binanceChainId`, then 429).

Other verified outcomes:

- 2026-10-01: one `baw market-order quote` and `/api/swap/order` returned the same out-amount for USDT to TSLAB at $50.
- 2026-10-01: `/api/agent/studio/tick` returned an SPCX arb at 35.9 bps net, proposal-only, and 401 without the token.
- 2026-10-01: Ondo and xStock RFQ errors `40368`, `40370`, `40374`, `40375`, all HTTP 200.
- 2026-10-01: PancakeSwap `GET /v1/quote` + `POST /v1/calldata` returned a signable BNB to TSLAx transaction when Binance refused the pair with `40370`.
- OpenOcean `swap` / `swap_quote` returns HTTP 403 plain text from the Railway Singapore server while the same URL returns JSON in a phone browser.
- 2026-10-03: Studio tick from the managed runtime returned 401 every minute until the platform dropped the custom token env; with a tick-only key it returned 200 `mode=proposal-only arbs=1` in 1.9 to 2.3 s, and the runtime stopped about 5 minutes after the last invoke.

---

## AI stack

- **No model computes a price.** Tape is GeckoTerminal, last cash print is Yahoo, quotes are Binance (Pancake as fallback). Gaps, share ratios and net bps are plain Python arithmetic in the app. I do not let a model invent a number.
- **The skill is `skills/streettape.py`.** Closed grammar. `say` accepts a fixed set of sentences: rich vs Friday, rotate into cheapest X, flatten anything N% rich, alert only when cash is shut, basket, defensive, floor watch, earnings stand-down. Stdlib only. It is a thin client over the StreetTape API and invents nothing.
- **Quote and sign.** `baw market-order quote` when `baw` is on PATH, otherwise `POST /api/swap/order`. Signing is only `quote --sign`, and only after the user says yes. Nothing auto-executes.
- **One live comparison, 2026-10-01.** `baw` and `/api/swap/order` returned the same out-amount (0.1404166985679507 TSLAB, `deltaBps` 0.0) for USDT to TSLAB at $50. Quote only that day. I ran the signed `baw` fill on 2026-10-10.
- **Studio.** I deployed with `bag deploy --provider bnb --accept-risk --yes`, agent `01M401H27Z9Q80XTFB0ZW8K5HC`, plus a 60 s tick loop calling `GET /api/agent/studio/tick`. The runtime got 401 every minute because the platform dropped the custom token env; after a tick-only key it returned 200. The runtime stops about 5 minutes after the last invoke, so this is a trial, not a scheduled job. `x402Support` stays false: I have no paid endpoint.

---

## Time to first useful call

| Clock | What happened |
|---|---|
| Process start + 5.3 s | Signed `GET /api/v1/dex/market/rwa/platforms`. HTTP 200, 2 platforms, 115 ms. |
| Same minute | `rwa/tokens` and `rwa/search?keyword=NVDA` also 200. |
| Same minute | First `rwa/price` returned HTTP 200 with `code=40001 Parameter binanceChainId is required`. Docs listed `tokenAddress`. They did not list the chain id every sibling endpoint demands. |
| Same session | First aggregator quote that priced a size: BNB → TSLAB via LiquidMesh, ~100 ms. |

My first successful signed call was platforms. My first useful price was not RWA Data. It was Yahoo + Gecko.

---

## Where I got stuck

### Region block

Valid keys from Railway's default region returned `Service not available due to compliance restriction` on `platforms`, `search`, `underlying-profile`, and `underlying-market`. Not a 401. Moving the service to Singapore made the same keys work. The docs I used did not list allowed regions next to "create a key." Cost: one failed deploy cycle.

### Errors name one missing field at a time

| Call | First error | What actually worked |
|---|---|---|
| `underlying-profile` | `Parameter tokenContractAddress is required` | ticker is not enough |
| `underlying-market` | `Parameter binanceChainId is required` | send `tokenContractAddress` + `binanceChainId=56` |
| `rwa/price` | `Parameter binanceChainId is required` | I sent `tokenAddress` only. 213 times |

A validation miss is HTTP 200 + `code=40001`. A rate limit is HTTP 429 + `code=42900`. Clients that only look at HTTP status treat the first class as success. Mine did, until I checked `success` / `code`.

### `rwa/price` produced no mark in the logged window

420 calls. 213 missing `binanceChainId`. 207 rate-limited. Official tape and official mark did not come from RWA Data. Gecko (tape) and Yahoo (last cash print) carried the board. After I sent `binanceChainId=56` and `tokenContractAddresses` the call returns 200 ok, but with `tokenPrice` and `referencePrice` only and no `underlyingPrice`. I label every Yahoo print as Yahoo fallback, never as a Binance mark.

### Rate limits recover in under a second and are unpublished

Five RWA calls passed. The next eight, ~80 ms apart, all 429. The ninth, 84 ms later, passed. Spot API limit pages do not cover `web3.binance.com`. Seven `underlying-market` calls in a tight loop lost QQQ and SPCX; a 2 s gap got all seven. The app now waits 0.3 s between price calls and retries a 429 once after 1 s.

### `underlying-profile` / `underlying-market` are company data, not token data

I queried NVDAB (`0x02fca66c1d1afb4e2a7884261eb00f63598a7436`):

- `tokenToShareRatio` = `1.000778223752807865`, not `1.0`. My seed catalog stored `multiplier: 1.0` for bStocks. About 0.08% off in share-normalized arb. I wired the live ratio into the arb math.
- `marketData.marketCap` is NVIDIA the company (~$5.5T), not the wrapper. I show it as "Underlying mcap."
- `totalShares` is `null` on the bStocks address and `24147000000` on the xStocks address for the same underlying. One wrapper is not a source of truth for a field.
- `statusInfo.reasonCode=TRADING` exists; `nextOpenTime` / `nextCloseTime` were null in the call I have, so session math stays in `rwa.session_now()`.
- `protections.collateralReport.supported` is true; `description` and `url` are null.

### Quotes: AMM works without a wallet. RFQ does not.

- AMM / LiquidMesh, no `userWalletAddress`: price, `priceImpactPct`, `needsWallet: true`, `transaction: null`. Fine for a public board.
- Ondo or xStocks, no wallet: `code=40001 userWalletAddress is required for RFQ (Ondo)` / `(xStock)`. HTTP 200. I use a 5 bps cost floor when impact is missing. That floor is a guess, not a fill.
- BNB against an xStock (`code=40370`) falls through to Pancake `GET /v1/quote` plus `POST /v1/calldata`. No Pancake link.
- Same pair with a taker builds a real tx (`from`, `to`, `data`, `value`, `gas`, `gasPrice`). Swap-build ~98 ms.

After hours, cross-wrapper gaps at $50 notionals: SPCX 3.5%, TSLA 3.1%, META 1.8%, NVDA 1.1%. Wrappers of the same name stop tracking each other off hours. That is the desk.

### `priceImpactPct` is a fraction, and it does not scale with size

I probed BNB to TSLAB (LiquidMesh, taker set, `executionMode=SWAP`) at $50, $5,000 and $20,000. Raw field: `0.00389`, `0.00273`, `0.00185`. Rate drop against the $50 quote: 15 bps at $5,000, 21 bps at $20,000. Read as a fraction the raw value is 19 to 39 bps, the same order as the observed drop. Read as a percent it is 0.2 to 0.4 bps, roughly 50x to 100x too small. I settled the unit as a fraction: `_leg_cost_bps` multiplies by 10,000. The old x100 understated cost 100x.

Still open: the raw value fell as size grew, while the rate drop grew. On this pair the field behaves like a per-quote figure, not incremental slippage. Its meaning is not in the docs I used. Cost stays at the 5 bps floor for RFQ legs and legs with no impact value.

### Transaction simulate path does not exist on this key

`POST /api/v1/dex/aggregator/tx/simulate` — two calls, real taker, built SWAP tx — HTTP 404 in ~100 ms both times. My early logger treated "no code field" as success. That was my bug. No documented replacement found. Pre-sign check in use is the wallet's own simulation.

After US close the wallet flagged a Binance-built BNB to NVDAB swap "likely to fail." I cancelled. A later TSLAB buy settled; holdings then showed via public RPC. I park simulate for an hour after a 404.

**Proof of the check is off-chain.** Each quote from `/api/swap/order` carries `checkProof`: sha256 over quote id, both mints, amount, out-amount and per-share prices (payload included, so anyone can recompute it), shown on the swap sheet. It is not anchored on-chain: `tx/simulate` is 404 on this key, so there is no live simulation to attest, and a hash of a quote is not a broadcast. The settled fills above are the on-chain proof.

### Address Portfolio is not a portfolio

`GET /api/v1/portfolio/tokens` on `web3.binance.com/wallet` — 19/19 HTTP 202, non-JSON, 10-40 ms. Holdings in the UI come from public BSC `eth_call` / `eth_getBalance`. I park it for 10 minutes after a failure.

### Agentic Wallet skill is a CLI, not a library

- `npx skills add binance-agentic-wallet` fails. The CLI wants the GitHub folder path under `binance/binance-skills-hub`.
- On Termux the same command needs `git` on PATH, then a Node/OpenSSL upgrade.
- On Termux the CLI itself (`npm install -g @binance/agentic-wallet`, `baw` 1.10.0) failed first on `keytar` (`Package 'libsecret-1' was not found`). It installed after `pkg install libsecret pkg-config python make clang` and `npm install -g --allow-scripts=@github/keytar @binance/agentic-wallet`.
- `baw auth verify` returned `DNS_RESOLVE_FAILED (www.binance.com)` while `auth signin` and `app.binance.com` worked. Changing the phone's Private DNS fixed it. The pairing code lasts about 5 minutes and the link is a QR to scan with the Binance app.
- `baw market-order quote` returns symbols, amounts and slippage only. No route and no price impact. Those come from the API path.
- The skill wraps `baw market-order quote` / `swap`. It does not sign from Python.
- StreetTape calls `baw` when it is on PATH, else `/api/swap/order`. `--sign` is explicit. I ran a live `baw` quote on 2026-10-01 and a signed `baw` fill on 2026-10-10.

Signing in when the wallet lives on a different phone was friction. `baw auth signin` on one phone redirected to that phone's own Binance app. A QR drawn from `urlForWeb` and scanned from the wallet owner's phone got through. The pairing code expired if I waited. Once it worked, `baw wallet balance` showed the funded wallet.

### ERC-8004 identity. Studio runtime is a trial, not a scheduler.

Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (BSC, ERC1967 proxy). Three unlabeled `register` overloads on BscScan. I used the `agentURI`-only one. Token / agent id **360062**. Fee ~0.000009 BNB. Registration file is served at `/agent-registration.json`.

Agent Studio was deployed on 2026-10-03 as a 48h BNB trial. It is not a scheduled job: the managed runtime scales to zero when idle and the 60s tick loop only runs while something invokes it. No Studio scheduler that calls an external URL was found or tested. The scan also runs in StreetTape's own loop, and `GET /api/agent/studio/tick` exposes the same scan (proposal-only, token-gated). I have no paid endpoint and I never sign. Mainnet agent 360062 is the product identity. `x402Support` stays false.

---

## How the assets behaved

- **bStocks** quoted and filled on AMM after hours. I settled a TSLAB buy. NVDAB build after close was flagged likely-to-fail by the wallet. Cause not proven (liquidity vs transfer limits vs stale route).
- **Ondo / xStocks** quotes without a taker are RFQ and die. With a taker, PRE-MARKET Ondo quotes failed on `40368` (USDC), `40375` ($5 minimum) and `40374` (no liquidity), and xStocks failed on `40370` (BNB). I later got USDC and USDT quotes for TSLAon at $5 and $10 through baw; $1 and BNB were refused.
- **xStocks tape on Gecko** is often missing for names that have a contract. No tape means no Buy and no arb leg. The bot locks xStocks because baw returns no liquidity.
- **SpaceX** has no reliable Yahoo mark in some sessions. Premium vs official is blank. Cross-wrapper gap still exists.
- **Wrapper vs wrapper.** Binance refused NVDAon to NVDAB. I settled the Rotate through Pancake. The bot runs those pairs as two swaps via USDT so the user taps once.

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

## Open on my side

- Confirm `rwa/price` with `binanceChainId=56` returns a usable underlying mark in more sessions.
- Ask what `priceImpactPct` measures (it falls as size grows on TSLAB). Unit is settled as a fraction.
- Do not retry simulate or Address Portfolio until a documented path exists.

---

## Runtime log

Only live-key outcomes the sections above do not already have. One block per outcome, first call only.

### 2026-10-10 — First signed `baw market-order swap`: 0.0027 BNB to TSLAB, FINISHED

- Session: WEEKEND.
- What I hit: `baw market-order swap`, 0.0027 BNB to TSLAB.
- What came back: status FINISHED, tx `0x3f2e027a3c356783cb39be21b69cb0959d15fed3c2f13dfc09f79b4ba7d2161a`. `toTokenActualQty` 0.005290446631899607 TSLAB, within 1 bps of both quotes. Wallet after: 0.00529 TSLAB; BNB 0.007086 to 0.004328. `baw` printed "Confirm in the Binance app", but no confirmation sheet reached the phone holding the wallet. I saw only a swap-success toast.
- What I changed: `fill` now keeps going if the session lookup fails.

### 2026-10-10 — First settled Rotate: NVDAon to NVDAB through Pancake

- Session: WEEKEND.
- What I hit: `POST /api/swap/order`, NVDAon to NVDAB, taker set.
- What came back: Binance refused the pair. Pancake returned a signable transaction. First run required an approve; second run (allowance already set) settled in one signature.
- What I changed: Rotate is its own sheet. Site uses Pancake for wrapper-vs-wrapper. Bot runs the same pair as two swaps via USDT when baw cannot do it in one.

### 2026-10-10 — `baw auth signin` with the wallet on a different phone

- Session: WEEKEND.
- What I hit: `baw auth signin` on one phone while the Agentic Wallet lived on another.
- What came back: the signin link redirected to the phone that ran the command, not the phone that held the wallet. A QR drawn from `urlForWeb` and scanned from the wallet's phone got through. The pairing code expired if I waited. Once it worked, balance showed BNB 0.007086 ($5.31) on chain 56.
- What I changed: nothing in code. Documented the friction.

### 2026-10-10 — Floor watch and routing rules

- Session: WEEKEND.
- What I hit: `GET /api/agent/floor` with a held address and floor percentage; `GET /api/routing-rules`.
- What came back: proposal-only sell into USDT when room under the buffer is thin; cushion ok otherwise. Routing rules distinguish site (Pancake fallback, Rotate via Pancake) from bot (baw, two-leg via USDT, xStocks locked).
- What I changed: added `floor.py`, the home panel, the closed-grammar sentence, and `routing_rules.py`. Nothing signs.
