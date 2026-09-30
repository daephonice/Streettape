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

420 calls. 213 missing `binanceChainId`. 207 rate-limited. Official tape and official mark did not come from RWA Data. Gecko (tape) and Yahoo (last cash print) carried the board. After we sent `binanceChainId=56` and `tokenContractAddresses` the call returns 200 ok, but with `tokenPrice` and `referencePrice` only and no `underlyingPrice` (PRE-MARKET pass, 0/20; see Runtime log 2026-09-30). The UI labels every Yahoo print as Yahoo fallback, never as a Binance mark.

### Rate limits recover in under a second and are unpublished

Five RWA calls passed. The next eight, ~80 ms apart, all 429. The ninth, 84 ms later, passed. Spot API limit pages do not cover `web3.binance.com`. Seven `underlying-market` calls in a tight loop lost QQQ and SPCX; a 2 s gap got all seven. App now waits 0.3 s between price calls and retries a 429 once after 1 s.

### `underlying-profile` / `underlying-market` are company data, not token data

Queried NVDAB (`0x02fca66c1d1afb4e2a7884261eb00f63598a7436`):

- `tokenToShareRatio` = `1.000778223752807865`, not `1.0`. Seed catalog stores `multiplier: 1.0` for bStocks. About 0.08% off in share-normalized arb. Now wired into the arb math (see Runtime log, share-normalized rotate).
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

Values at a few dollars: `0.00023` to `0.00301`, sign flips. `_leg_cost_bps` does `abs(x) * 100`, which is correct if the field is a percent (0.004 = 0.004% = 0.4 bps) and understates cost 100x if it is a fraction (needs x10000). Not confirmed from docs or a large-size quote. Net-bps in the agent is provisional until `GET /api/_impactprobe?taker=<wallet>` is run; set `PRICE_IMPACT_UNIT` to its verdict. The 5 bps floor on unknown-impact legs is what keeps the numbers from looking insane.

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

- Session: PRE-MARKET (chip; 09:26 ET / 14:26 WAT). Wallet pass ran again at CASH OPEN (~09:39 ET).
- What we hit: `GET /api/agent/scan`; `GET /api/agent/arb/NVDA?size_usd=50`; `/t/TSLA#rotate` in a wallet dApp browser (Sell TSLAon, Buy TSLAB, Rotate $50, Flatten $10).
- What came back (log, first of each): good quote, LiquidMesh, ~100 ms (NVDAB/TSLAB legs). RFQ without wallet, HTTP 200 `code=40001 userWalletAddress is required for RFQ (Ondo) quote` at 13:23:13 UTC, then fallback to Pancake link. 429 `code=42900 Rate limit exceeded` on NVDAon and SPCX legs in the same scan. `rwa/price` with `tokenAddress`, `tokenContractAddress` and `binanceChainId=56` still returned 200 + `code=40001 Parameter tokenContractAddresses is required` (plural) on all 3 calls, so the price-confirmation item stays open. No 404s. Scan best: TSLAon rich vs TSLAB, gross 1881 bps, net 1876 bps; page showed +1933 bps a minute later.
- What we changed because of it: Sell TSLAon and Flatten did nothing. Server keys prices/assets by uppercase symbol (`TSLAON`); the page looked up `TSLAon`, missed, and the swap sheet returned silently. TSLAB is already uppercase, so Buy worked. Page now resolves the uppercase key, opens a Pancake link if the sheet can't open, and Rotate opens Sell first, Buy on close. Next: send `tokenContractAddresses` to `rwa/price`.

### 2026-09-30 — Fair (synthetic mark)

- Session: AFTER-HOURS (23:10 WAT / 18:10 ET), re-run at PRE-MARKET (06:09 WAT / 01:09 ET). Fair is hidden while CASH OPEN, so it could not be tested earlier.
- What we hit: `/` (group cards, hero, desk line); `GET /api/agent/scan`; `GET /api/board`; Telegram `/agent` rotate text.
- What came back (log, first of each): `rwa/price` with `tokenContractAddresses` added: 200, ok (22:26:01 UTC), so the plural-parameter item is closed. 429 `code=42900 Rate limit exceeded` on `rwa/price`, `underlying-market` and `underlying-profile` for the bstocks and Ondo NVDA addresses in the same startup burst; the retry returned 200. Quotes: 200 with `code=40001 userWalletAddress is required for RFQ (Ondo)` at 00:39:05, then Pancake fallback; 429 `code=42900` on an xStock/Ondo leg at 00:39:06, then Pancake fallback; first clean Binance quote at 05:08:59 (AMDB leg, 108 ms). No `mark fetch` (Yahoo) call appears in this process at all: `rwa/price` returned a price for every wrapper, so the board skipped Yahoo for every ticker, but it returned no `markPrice`. Result: `markPrice: null` on all tokens and no Official or Fair row on the first deploy. Card after the fix: Official = stored last print (`markSource: last-print`), Fair equal to it, line under each group reads `β 0.00 · QQQ +0.12% since Fri close · news +0%`. `/api/board` carries `fairPrice`, `beta`, `indexMove`, `newsShock` on every token and group. SpaceX showed Official `—` and no Fair on this pass, because the universe had it as having no Yahoo ticker. `/api/agent/scan` best and first arb (METAon over METAx) carry `gapVsOfficial` 0.91% and `gapVsFair` 0.91%; `netBps` still comes from the wrapper gap. Telegram rotate shows the synthetic Fair line, `Fair inputs: β 0.00 · QQQ -0.01% since Fri close · news +0%`, and `gap vs official +0.8% · gap vs fair +0.8%`.
- What we changed because of it: (1) The Fair row needs an official print, and the board had none. Fair's stored last print is now used as Official when no live mark landed, tagged `last-print`. (2) Yahoo was skipped for any underlying that RWA Data returned a price for, even with no `markPrice`. It is now skipped only when every wrapper has an official `markPrice`. (3) SPCX has traded on Nasdaq since 2026-06-12 and has a Yahoo quote page, so the no-Yahoo flag was stale. SPCX now uses the `SPCX` Yahoo ticker and gets an Official print and Fair like the rest; confirmed live on the board after deploy. (4) METAx tape sat at $573.75 (-21.8% vs the $738.79 print) for hours and drove a false `+2875 bps net` hero rotate. Checked by hand: GeckoTerminal shows the BSC METAx pool at $220 liquidity and $0 24h volume, flagged Low Liquidity Pool; Pancake finds no route for 10 BNB to METAx though it does for METAon; Pancake's own reference price for 1 METAx is $739.24. The pool price is stale, not a real drop. Board now reads `total_reserve_in_usd` and 24h volume from the Gecko token response and marks a wrapper `thin` under $5,000 liquidity: it shows a `thin pool` chip instead of a gap, and is left out of the cross-wrapper spread, the scan and the rotate.

### 2026-09-30 — Official mark (Binance first, Yahoo fallback)

- Session: PRE-MARKET (03:33 ET / 08:33 WAT, 2026-09-30). Cash shut.
- What we hit: `GET /api/_pricepass` (one paced pass, 0.3 s apart, no retries, 20 addresses); `GET /api/board`; the `/` board page on a phone browser.
- What came back (log, first of each): `rwa/price` with `tokenAddress`, `tokenContractAddress`, `tokenContractAddresses` and `binanceChainId=56`: HTTP 200, `ok` (07:17:28 UTC, NVDAx, 100 ms). First 429 `code=42900 Rate limit exceeded` at 07:17:29 UTC on the Ondo NVDA address in the startup burst; the retry returned 200 the next second. The paced pass: 0 marks of 20. First failure, NVDAx `0xc845b2894dBddd03858fd2D643B4eF725fE0849d`: 200 with no `underlyingPrice`. The row held only `binanceChainId`, `tokenContractAddress`, `platformId: null`, `tokenPrice` 229.4663, `referencePrice` 229.4663 and `tokenPriceUpdatedAt`. `referencePrice` equals `tokenPrice` to every digit, so it is the onchain tape, not an issuer mark, and it was not used. No first success exists. `/api/board`: `markSource` is `yahoo` on all 20 tokens (0 `binance`); Official showed `$738.79` on META, `$227.21` on NVDA and `$149.24` on SPCX, all Yahoo prints. The page printed "Yahoo fallback" under Official on every wrapper.
- What we changed because of it: `markSource` is `binance` only for a positive `underlyingPrice`, otherwise `yahoo` (or `last-print`), and the source prints under Official on the row and in `/api/board`. The endpoint returned no mark for this address set in PRE-MARKET, so Yahoo stays and no Yahoo number is labeled official. The park after three hard failures is unchanged; nothing retries in a loop. Not tested: CASH OPEN. An issuer mark may only appear during the cash session, so that pass is still open.

## 2026-09-29T13:18:04+00:00 — rwa official /api/v1/dex/market/rwa/platforms
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/platforms`
- Status: `200`  Latency: `172ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok
- Time-to-first-call from process start: `5.8s`


## 2026-09-29T13:18:05+00:00 — rwa official /api/v1/dex/market/rwa/tokens
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/tokens`
- Status: `200`  Latency: `327ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:18:05+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0xc845b2894dBddd03858fd2D643B4eF725fE0849d&tokenContractAddress=0xc845b2894dBddd03858fd2D643B4eF725fE0849d&binanceChainId=56`
- Status: `200`  Latency: `95ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=Parameter tokenContractAddresses is required
- Body: `{'code': 40001, 'msg': 'Parameter tokenContractAddresses is required', 'data': None, 'timestamp': 1790687885098, 'success': False}`


## 2026-09-29T13:18:05+00:00 — rwa official /api/v1/dex/market/rwa/underlying-market
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-market?tokenContractAddress=0xc845b2894dBddd03858fd2D643B4eF725fE0849d&binanceChainId=56`
- Status: `200`  Latency: `101ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:18:05+00:00 — rwa official /api/v1/dex/market/rwa/search
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/search?keyword=NVDA`
- Status: `200`  Latency: `270ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:18:05+00:00 — rwa official /api/v1/dex/market/rwa/underlying-profile
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-profile?tokenContractAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436&binanceChainId=56`
- Status: `429`  Latency: `82ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'msg': 'Rate limit exceeded', 'timestamp': 1790687885229, 'code': 42900, 'data': ''}`


## 2026-09-29T13:18:05+00:00 — rwa official /api/v1/dex/market/rwa/underlying-market
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-market?tokenContractAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436&binanceChainId=56`
- Status: `429`  Latency: `79ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'timestamp': 1790687885309, 'msg': 'Rate limit exceeded', 'data': '', 'code': 42900}`


## 2026-09-29T13:18:05+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0xA9eE28C80f960B889dFbd1902055218cBa016F75&tokenContractAddress=0xA9eE28C80f960B889dFbd1902055218cBa016F75&binanceChainId=56`
- Status: `429`  Latency: `84ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'code': 42900, 'timestamp': 1790687885482, 'msg': 'Rate limit exceeded', 'data': ''}`


## 2026-09-29T13:18:06+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0xA9eE28C80f960B889dFbd1902055218cBa016F75&tokenContractAddress=0xA9eE28C80f960B889dFbd1902055218cBa016F75&binanceChainId=56`
- Status: `200`  Latency: `89ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=Parameter tokenContractAddresses is required
- Body: `{'code': 40001, 'msg': 'Parameter tokenContractAddresses is required', 'data': None, 'timestamp': 1790687886572, 'success': False}`


## 2026-09-29T13:18:06+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436&tokenContractAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436&binanceChainId=56`
- Status: `200`  Latency: `85ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=Parameter tokenContractAddresses is required
- Body: `{'code': 40001, 'msg': 'Parameter tokenContractAddresses is required', 'data': None, 'timestamp': 1790687886958, 'success': False}`


## 2026-09-29T13:18:07+00:00 — rwa official /api/v1/dex/market/rwa/underlying-market
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-market?tokenContractAddress=0x8aD3c73F833d3F9A523aB01476625F269aEB7Cf0&binanceChainId=56`
- Status: `200`  Latency: `97ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:18:09+00:00 — rwa official /api/v1/dex/market/rwa/underlying-market
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-market?tokenContractAddress=0x9d275685dC284C8eB1C79f6ABA7a63Dc75ec890a&binanceChainId=56`
- Status: `200`  Latency: `91ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:18:14+00:00 — mark fetch AAPL
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AAPL`
- Status: `200`  Latency: `25ms`
- Docs said: regularMarketPrice present
- Actually happened: price=338.4


## 2026-09-29T13:18:14+00:00 — mark fetch AMD
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AMD`
- Status: `200`  Latency: `25ms`
- Docs said: regularMarketPrice present
- Actually happened: price=607.87


## 2026-09-29T13:18:14+00:00 — mark fetch TSLA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/TSLA`
- Status: `200`  Latency: `27ms`
- Docs said: regularMarketPrice present
- Actually happened: price=357.45


## 2026-09-29T13:18:14+00:00 — mark fetch META
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/META`
- Status: `200`  Latency: `33ms`
- Docs said: regularMarketPrice present
- Actually happened: price=715.62


## 2026-09-29T13:18:14+00:00 — mark fetch NVDA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/NVDA`
- Status: `200`  Latency: `38ms`
- Docs said: regularMarketPrice present
- Actually happened: price=228.86


## 2026-09-29T13:18:14+00:00 — mark fetch QQQ
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/QQQ`
- Status: `200`  Latency: `50ms`
- Docs said: regularMarketPrice present
- Actually happened: price=736.53


## 2026-09-29T13:19:06+00:00 — mark fetch TSLA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/TSLA`
- Status: `200`  Latency: `37ms`
- Docs said: regularMarketPrice present
- Actually happened: price=357.45


## 2026-09-29T13:19:06+00:00 — mark fetch AMD
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AMD`
- Status: `200`  Latency: `38ms`
- Docs said: regularMarketPrice present
- Actually happened: price=607.87


## 2026-09-29T13:19:06+00:00 — mark fetch META
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/META`
- Status: `200`  Latency: `38ms`
- Docs said: regularMarketPrice present
- Actually happened: price=715.62


## 2026-09-29T13:19:06+00:00 — mark fetch NVDA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/NVDA`
- Status: `200`  Latency: `38ms`
- Docs said: regularMarketPrice present
- Actually happened: price=228.86


## 2026-09-29T13:19:06+00:00 — mark fetch QQQ
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/QQQ`
- Status: `200`  Latency: `42ms`
- Docs said: regularMarketPrice present
- Actually happened: price=736.53


## 2026-09-29T13:19:06+00:00 — mark fetch AAPL
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AAPL`
- Status: `200`  Latency: `60ms`
- Docs said: regularMarketPrice present
- Actually happened: price=338.4


## 2026-09-29T13:19:59+00:00 — mark fetch META
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/META`
- Status: `200`  Latency: `21ms`
- Docs said: regularMarketPrice present
- Actually happened: price=715.62


## 2026-09-29T13:19:59+00:00 — mark fetch AMD
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AMD`
- Status: `200`  Latency: `26ms`
- Docs said: regularMarketPrice present
- Actually happened: price=607.87


## 2026-09-29T13:19:59+00:00 — mark fetch TSLA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/TSLA`
- Status: `200`  Latency: `26ms`
- Docs said: regularMarketPrice present
- Actually happened: price=357.45


## 2026-09-29T13:19:59+00:00 — mark fetch NVDA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/NVDA`
- Status: `200`  Latency: `40ms`
- Docs said: regularMarketPrice present
- Actually happened: price=228.86


## 2026-09-29T13:19:59+00:00 — mark fetch QQQ
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/QQQ`
- Status: `200`  Latency: `46ms`
- Docs said: regularMarketPrice present
- Actually happened: price=736.53


## 2026-09-29T13:19:59+00:00 — mark fetch AAPL
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AAPL`
- Status: `200`  Latency: `53ms`
- Docs said: regularMarketPrice present
- Actually happened: price=338.4


## 2026-09-29T13:23:13+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=117906468660905536&fromTokenAddress=0x2494b603319d4D9F9715c9f4496d9E0364B59d93&toTokenAddress=0x55d398326f99059fF775485246999027B3197955`
- Status: `200`  Latency: `101ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=userWalletAddress is required for RFQ (Ondo) quote
- Body: `{'code': 40001, 'msg': 'userWalletAddress is required for RFQ (Ondo) quote', 'data': None, 'timestamp': 1790688193795, 'success': False}`


## 2026-09-29T13:23:13+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: HTTP 200: userWalletAddress is required for RFQ (Ondo) quote


## 2026-09-29T13:23:13+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=50000000000000000000&fromTokenAddress=0x55d398326f99059fF775485246999027B3197955&toTokenAddress=0x5b1910eaad6450e50f816082aa078c41f10c292f`
- Status: `200`  Latency: `104ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:23:14+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=69434707117849912&fromTokenAddress=0x7425889fe94f9d693e8daefe88bcced6acfef4c0&toTokenAddress=0x55d398326f99059fF775485246999027B3197955`
- Status: `200`  Latency: `103ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:23:14+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=50000000000000000000&fromTokenAddress=0x55d398326f99059fF775485246999027B3197955&toTokenAddress=0xD7dF5863A3e742F0c767768cDfcb63f09E0422f6`
- Status: `200`  Latency: `97ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=userWalletAddress is required for RFQ (Ondo) quote
- Body: `{'code': 40001, 'msg': 'userWalletAddress is required for RFQ (Ondo) quote', 'data': None, 'timestamp': 1790688194117, 'success': False}`


## 2026-09-29T13:23:14+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: HTTP 200: userWalletAddress is required for RFQ (Ondo) quote


## 2026-09-29T13:23:14+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=216394883761526528&fromTokenAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436&toTokenAddress=0x55d398326f99059fF775485246999027B3197955`
- Status: `200`  Latency: `105ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok


## 2026-09-29T13:23:14+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=50000000000000000000&fromTokenAddress=0x55d398326f99059fF775485246999027B3197955&toTokenAddress=0xA9eE28C80f960B889dFbd1902055218cBa016F75`
- Status: `429`  Latency: `87ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'msg': 'Rate limit exceeded', 'data': '', 'code': 42900, 'timestamp': 1790688194322}`


## 2026-09-29T13:23:14+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: HTTP 429: Rate limit exceeded


## 2026-09-29T13:23:14+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=337966467311257856&fromTokenAddress=0x68fa48b1c2fe52b3d776e1953e0e782b5044ce28&toTokenAddress=0x55d398326f99059fF775485246999027B3197955`
- Status: `429`  Latency: `91ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'msg': 'Rate limit exceeded', 'timestamp': 1790688194420, 'code': 42900, 'data': ''}`


## 2026-09-29T13:23:14+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: HTTP 429: Rate limit exceeded


## 2026-09-29T13:23:14+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=50000000000000000000&fromTokenAddress=0x55d398326f99059fF775485246999027B3197955&toTokenAddress=0xbe9d156892e55e7154bcd3cb0fea677f9d3103e1`
- Status: `429`  Latency: `85ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'msg': 'Rate limit exceeded', 'timestamp': 1790688194512, 'code': 42900, 'data': ''}`


## 2026-09-29T13:23:14+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: HTTP 429: Rate limit exceeded


## 2026-09-29T13:26:57+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=117906468660905536&fromTokenAddress=0x2494b603319d4D9F9715c9f4496d9E0364B59d93&toTokenAddress=0x55d398326f99059fF775485246999027B3197955`
- Status: `200`  Latency: `106ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=userWalletAddress is required for RFQ (Ondo) quote
- Body: `{'code': 40001, 'msg': 'userWalletAddress is required for RFQ (Ondo) quote', 'data': None, 'timestamp': 1790688417418, 'success': False}`


## 2026-09-29T13:26:57+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: HTTP 200: userWalletAddress is required for RFQ (Ondo) quote



## Call summary (this process, all calls including repeats)

| Calls | Label | Status | Outcome |
|---|---|---|---|
| 14 | mark fetch AAPL | 200 | price=#.# |
| 14 | mark fetch AMD | 200 | price=#.# |
| 14 | mark fetch TSLA | 200 | price=#.# |
| 14 | mark fetch META | 200 | price=#.# |
| 14 | mark fetch NVDA | 200 | price=#.# |
| 14 | mark fetch QQQ | 200 | price=#.# |
| 10 | Binance Web3 GET /api/v1/dex/aggregator/quote | 200 | ok |
| 7 | rwa official /api/v1/dex/market/rwa/underlying-market | 200 | ok |
| 7 | Binance Web3 GET /api/v1/dex/aggregator/quote | 200 | code=# msg=userWalletAddress is required for RFQ (Ondo) quote |
| 7 | quote fallback -> pancake | n/a | fell back: RuntimeError: HTTP #: userWalletAddress is required for RFQ (Ondo) qu |
| 7 | Binance Web3 GET /api/v1/dex/aggregator/quote | 429 | code=# msg=Rate limit exceeded |
| 7 | quote fallback -> pancake | n/a | fell back: RuntimeError: HTTP #: Rate limit exceeded |
| 6 | rwa official /api/v1/dex/market/rwa/price | 200 | code=# msg=Parameter tokenContractAddresses is required |
| 1 | rwa official /api/v1/dex/market/rwa/platforms | 200 | ok |
| 1 | rwa official /api/v1/dex/market/rwa/tokens | 200 | ok |
| 1 | rwa official /api/v1/dex/market/rwa/search | 200 | ok |
| 1 | rwa official /api/v1/dex/market/rwa/underlying-profile | 429 | code=# msg=Rate limit exceeded |
| 1 | rwa official /api/v1/dex/market/rwa/underlying-market | 429 | code=# msg=Rate limit exceeded |
| 1 | rwa official /api/v1/dex/market/rwa/price | 429 | code=# msg=Rate limit exceeded |

### 2026-09-30 — share-normalized rotate

- Session: PRE-MARKET throughout (09:50–12:18 WAT / 04:50–07:18 ET). Not run at CASH OPEN.
- What we hit: `GET /api/_ratiocheck`; `GET /api/agent/arb/{NVDA,TSLA,META,SPCX}?size_usd=50`; `GET /api/agent/scan`; `/` hero; `/t/AMD#rotate` in a wallet dApp browser (Rotate $50, Buy sheet, not signed); Telegram `/agent`; GeckoTerminal AMDx page by hand.
- What came back (log, first of each, UTC):
  - `underlying-profile`: 429 `code=42900` at 10:30:51 on the xStocks NVDA address (87 ms) and at 10:30:52 on NVDAB, in the same startup burst as `price` and `underlying-market`. Retries after 3 s returned 200 (10:30:54, 10:30:56, 10:30:59), ~85 ms. Process totals: 40 x 200, 2 x 429. 20 of 20 wrapper addresses got a `tokenToShareRatio`, `missing` empty.
  - Ratios: xStocks all exactly 1.0. bStocks 1.0005–1.0008 (NVDAB 1.000778, METAB 1.000548, QQQB 1.000725, TSLAB/AMDB/SPCXB 1.0). Ondo 1.0017–1.0041 (NVDAon 1.001715, AAPLon 1.003376, METAon 1.002827, QQQon 1.004082, TSLAon/AMDon/SPCXon 1.0). The seed `1.0` for bStocks was wrong for four of six.
  - Share-normalization at work (scan 10:46 UTC): AAPLon 330.47 raw is 329.36 per share (0.34% of a 3.3% gap). AAPLx over AAPLon: gross 327 bps, net 317. TSLAx over TSLAon: gross 194, net 184. NVDA: share prices 229.47 / 228.53 / 228.55, gap 0.4%, under the 1% threshold, `hit: null` (correct).
  - Quotes: every leg fell back to Pancake. 10:46:19 and 11:01:39: HTTP 200 `code=40001 userWalletAddress is required for RFQ (xStock)` / `(Ondo)`; `429 code=42900` on the TSLAon buy leg. Cost is therefore the flat 5 bps floor per leg (10 bps), not a fill. `priceImpactPct` was `null` on every fallback leg. So "viable" is provisional.
  - False arb: METAx `tokenPrice` 573.75 vs 738.79 mark (-22.3%). Hero read `META +2802 bps net`; `/api/agent/arb/META` gross 2810, net 2800. That 573.75 came from RWA Data's own `price`, not the Gecko tape, so the thin-pool check (Gecko liquidity, and only when RWA Data had no price) never ran. `liquidityUsd` was `null` on every token.
  - Second dead pool: AMDx. GeckoTerminal AMDx/USDC (Uniswap V4): price $477.36, liquidity $438.56, 24h volume $0. StreetTape showed 643.48 against a 607.57 mark. The AMD rotate (AMDx over AMDon, gross 611 / net 601 bps, card +666, Telegram +656) rests on that leg, so it is not a real trade.
  - Card (`/t/AMD#rotate`): "Shares per token: AMDx 1 (643.4770 -> 643.4770/sh) · AMDon 1 (602.75 -> 602.7500/sh)". Telegram `/agent` ends with `Share ratios: AMDx 1.000000 · AMDon 1.000000`.
  - Buy sheet, 11:18–11:19 UTC: BNB -> AMDon with a real wallet returned 200 `code=40368 Ondo asset on chain 56 can only pair with allowed stablecoin(s)` (3 of 3), then the Pancake fallback.
- What we changed because of it:
  1. Arb now divides token price by the live `tokenToShareRatio` from `underlying-profile` (one call per wrapper address, hourly, 2 s apart, 429 retried) and ignores the seed `multiplier`. Legs without a ratio are dropped; fewer than two ratio-backed legs returns no arb. `normalized` is true only when both legs have a ratio. Both ratios show on `/t/{underlying}` and in the Telegram text.
  2. Any wrapper more than 15% off the official mark is marked `thin`, whatever the price source. Verified live: METAx shows the thin pool chip and the META group spread fell from +28.6% to +0.3%. (One earlier screenshot still showed the old card; a hard reload fixed it, so that was probably a stale page. Not proven.)
  3. Not yet verified live: a sibling-median guard (3+ priced wrappers, more than 15% off the median is thin), and Gecko is now read for every wrapper so `liquidityUsd` is filled and a pool under $5,000 is thin even when RWA Data supplied the price. Both are aimed at AMDx, which the 15% mark guard does not catch (+5.9%). Check `/api/board` after deploy: AMDx `liquidityUsd` should be about 438 and `thin` true. If `liquidityUsd` is still `null`, Gecko's multi-token response does not carry it.
  4. Ondo wrappers only pair with stablecoins, so the swap sheet no longer offers BNB for them (USDC only). Not yet verified live.
- Open: `priceImpactPct` unit still unconfirmed (no non-fallback quote at size).

### YYYY-MM-DD — Cost that matches the quote (`priceImpactPct` unit)

- Session: <CASH OPEN | AFTER-HOURS | WEEKEND>
- What we hit: `GET /api/_impactprobe?taker=<wallet>` (BNB to TSLAB, $50 then $5,000, taker set so the route is SWAP).
- What came back: <paste raw50 / out50 / raw5000 / out5000 / verdict from the probe's `_devex` entry>
- What we changed because of it: <`PRICE_IMPACT_UNIT` set to percent|fraction; "provisional" note removed. RFQ legs keep the 5 bps floor.>
