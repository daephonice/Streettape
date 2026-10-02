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
- Verified live (2026-10-01): one `baw market-order quote` and `/api/swap/order` returned the same out-amount for USDT to TSLAB at 50 (see Runtime log).
- Verified live (2026-10-01): `/api/agent/studio/tick` returned an SPCX arb at 35.9 bps net, proposal-only, and 401 without the token (see Runtime log).
- Verified live (2026-10-01): Ondo and xStock RFQ errors `40368`, `40370`, `40374`, `40375`, all HTTP 200 (see Runtime log).
- Verified live (2026-10-01): PancakeSwap `GET /v1/quote` + `POST /v1/calldata` returned a signable BNB to TSLAx transaction when Binance refused the pair with `40370` (see Runtime log).
- Called live, failed: OpenOcean `swap` / `swap_quote` returns HTTP 403 plain text from the Railway Singapore server while the same URL returns JSON in a phone browser (see Runtime log).
- Not verified live: `baw` signing (`quote --sign`). The agentic wallet balance was empty.

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
- Ondo or xStocks, no wallet: `code=40001 userWalletAddress is required for RFQ (Ondo)` / `(xStock)`. HTTP 200. A 5 bps cost floor is used when impact is missing. That floor is a guess, not a fill. BNB against an xStock (`code=40370`) now falls through to Pancake `GET /v1/quote` plus `POST /v1/calldata`, signed on the site. No Pancake link.
- Same pair with a taker builds a real tx (`from`, `to`, `data`, `value`, `gas`, `gasPrice`). Swap-build ~98 ms.

After-hours `/api/agent/studio/tick` at $50 notionals: cross-wrapper gaps SPCX 3.5%, TSLA 3.1%, META 1.8%, NVDA 1.1%. Wrappers of the same name stop tracking each other off hours. That is the desk.

### `priceImpactPct` is a fraction, and it does not scale with size

Probed BNB to TSLAB (LiquidMesh, taker set, `executionMode=SWAP`) at $50, $5,000 and $20,000. Raw field: `0.00389`, `0.00273`, `0.00185`. Rate drop against the $50 quote: 15 bps at $5,000, 21 bps at $20,000. Read as a fraction the raw value is 19 to 39 bps, the same order as the observed drop. Read as a percent it is 0.2 to 0.4 bps, roughly 50x to 100x too small. Unit settled as a fraction: `_leg_cost_bps` multiplies by 10,000 (`PRICE_IMPACT_UNIT=fraction`). The old x100 understated cost 100x.

Still open: the raw value fell as size grew, while the rate drop grew. On this pair the field behaves like a per-quote figure, not incremental slippage. Its meaning is not in the docs we used. Cost stays at the 5 bps floor for RFQ legs and legs with no impact value.

### Transaction simulate path does not exist on this key

`POST /api/v1/dex/aggregator/tx/simulate` — two calls, real taker, built SWAP tx — HTTP 404 in ~100 ms both times. Early logger treated "no code field" as success. That was our bug. No documented replacement found. Pre-sign check in use is the wallet's own simulation.

After US close the wallet flagged a Binance-built BNB to NVDAB swap "likely to fail." Cancelled. A later TSLAB buy settled; holdings then showed via public RPC. Simulate is parked for an hour after a 404.

### Address Portfolio is not a portfolio

`GET /api/v1/portfolio/tokens` on `web3.binance.com/wallet` — 19/19 HTTP 202, non-JSON, 10-40 ms. Holdings in the UI come from public BSC `eth_call` / `eth_getBalance`. Parked for 10 minutes after a failure.

### Agentic Wallet skill is a CLI, not a library

- `npx skills add binance-agentic-wallet` fails. The CLI wants the GitHub folder path under `binance/binance-skills-hub`.
- On Termux the same command needs `git` on PATH, then a Node/OpenSSL upgrade.
- On Termux the CLI itself (`npm install -g @binance/agentic-wallet`, `baw` 1.10.0) failed first on `keytar` (`Package 'libsecret-1' was not found`). It installed after `pkg install libsecret pkg-config python make clang` and `npm install -g --allow-scripts=@github/keytar @binance/agentic-wallet`.
- `baw auth verify` returned `DNS_RESOLVE_FAILED (www.binance.com)` while `auth signin` and `app.binance.com` worked. Changing the phone's Private DNS fixed it. The pairing code lasts about 5 minutes and the link is a QR to scan with the Binance app.
- `baw market-order quote` returns symbols, amounts and slippage only. No route and no price impact. Those come from the API path.
- The skill wraps `baw market-order quote` / `swap`. It does not sign from Python.
- StreetTape calls `baw` when it is on PATH, else `/api/swap/order`. `--sign` is explicit. A live `baw` quote has been run (Runtime log 2026-10-01). A signed `baw` fill has not.

### ERC-8004 identity. No Agent Studio runtime.

Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (BSC, ERC1967 proxy). Three unlabeled `register` overloads on BscScan. Used the `agentURI`-only one. Token / agent id **360062**. Fee ~0.000009 BNB. Registration file is served at `/agent-registration.json`.

Agent Studio full deploy was not built. The special is for a self-funding seller on their runtime (x402, ERC-8183). StreetTape has no paid endpoint and never signs. BNB Agent Studio is a CLI (`bag`), not a dashboard, and has no scheduler that calls an external URL, so no Studio job exists. The scan runs in StreetTape's own loop, and `GET /api/agent/studio/tick` exposes the same scan to any caller (proposal-only, token-gated). Studio was not deployed. Mainnet agent 360062 is the product identity, registered directly on the Identity Registry. `x402Support` stays false in the card.

### Multiplier and missing contracts

- Seed: bStocks `multiplier: 1.0`, xStocks/Ondo `None`. Arb divides only when the value is truthy. `normalized` is true if *either* wrapper has a multiplier, so NVDAB vs NVDAon reports normalized even though one side was raw.
- AAPLB seed address is `0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a` (BSC), taken from Binance's eligible-bStock token list, not from RWA Data discovery (RWA Data never returned one, so `merge_dynamic()` would not have filled it). `multiplier` stays 1.0 and no price is hardcoded. Gecko has a pool for it: tape, a price and $1.37M liquidity on 2026-10-02 (see Runtime log), so the row can enter the arb. A wrapper with no pool would still show "no tape".

---

## How the assets behaved

- **bStocks** quoted and filled on AMM after hours. TSLAB buy settled. NVDAB build after close was flagged likely-to-fail by the wallet. Cause not proven (liquidity vs transfer limits vs stale route).
- **Ondo / xStocks** quotes without a taker are RFQ and die. With a taker, PRE-MARKET Ondo quotes failed on `40368` (USDC), `40375` ($5 minimum) and `40374` (no liquidity), and xStocks failed on `40370` (BNB). No completed RFQ quote or fill yet.
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
- Ask what `priceImpactPct` measures (it falls as size grows on TSLAB). Unit is settled as a fraction.
- Do not retry simulate or Address Portfolio until a documented path exists.
- One signed `baw market-order swap` (`quote --sign`). Needs a funded agentic wallet.

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
- What we changed because of it: Sell TSLAon and Flatten did nothing. Server keys prices/assets by uppercase symbol (`TSLAON`); the page looked up `TSLAon`, missed, and the swap sheet returned silently. TSLAB is already uppercase, so Buy worked. Page now resolves the uppercase key, and Rotate opens Sell first, Buy on close. The Pancake link opened when the sheet could not. That link was removed later: a miss now returns a Pancake transaction or no route. Next: send `tokenContractAddresses` to `rwa/price`.

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

### 2026-10-02 — AAPLB seeded from the eligible-token list

- Session: PRE-MARKET (03:24 ET / 07:24 UTC / 08:24 WAT).
- What we hit: `GET /api/board` after setting the AAPLB seed address to `0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a` (BSC), taken from Binance's eligible-bStock list. RWA Data never returned an address for AAPLB, so `merge_dynamic()` had nothing to fill.
- What came back (board row, 07:24:15 UTC): `hasTape: true`, `tokenPrice` 331.17 against a Yahoo mark of 330.32 (+0.26%), GeckoTerminal liquidity $1,373,875, `tokenToShareRatio` 1.000604, seed `multiplier` 1.0. Before this change the row was "no tape" and had no Buy. AAPLB is now the deepest Apple wrapper: AAPLon showed $12,983 and AAPLx $417 (thin) in the same read. The Gecko call itself is not in `/api/_devex` (only Binance and Yahoo calls are logged), so there is no Gecko status or latency for it.
- What we changed because of it: the seed address, nothing else. No price is hardcoded. The ratio is 0.06% off the seed `1.0`, the same pattern as the other bStocks (NVDAB 1.000778, METAB 1.000548, QQQB 1.000725), and the arb math uses the live ratio, not the seed.

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
- Closed 2026-09-30: `priceImpactPct` unit is a fraction (see Runtime log, cost that matches the quote).

### 2026-09-30 — Cost that matches the quote (`priceImpactPct` unit)

- Session: CASH OPEN (15:25 Lagos / 10:25 New York).
- What we hit: `GET /api/_impactprobe?taker=<wallet>` for BNB to TSLAB at $50 and $5,000, then again at $50 and $20,000. Taker set, so both legs were a real SWAP, not an RFQ rejection. Follow-up `GET /api/agent/arb/{NVDA,TSLA,SPCX,META}?size_usd=50`.
- What came back: all six quotes were `provider=binance_web3`, `mode=SWAP`, route LiquidMesh. The raw values below are from the probe's JSON response; the `/api/_devex` entries were wiped by the redeploy that followed, so they are not pasted here.

  | Size | BNB in | Raw `priceImpactPct` | Out (TSLAB) | Rate (TSLAB per BNB) |
  |---|---|---|---|---|
  | $50 | 0.06538 | 0.0037888 | 0.142574 | 2.18079 |
  | $5,000 | 6.53774 | 0.0027332 | 14.235711 | 2.17747 |
  | $50 (2nd run) | 0.06537 | 0.0038926 | 0.142553 | 2.18077 |
  | $20,000 | 26.14721 | 0.0018531 | 56.902550 | 2.17624 |

  Rate drop against the $50 quote: 0.152% (15 bps) at $5,000, 0.208% (21 bps) at $20,000. Probe verdict both runs: `fraction` (raw over economic 1.8 and 0.9; a percent would give about 100). The four arb calls returned `hit: null`: no cross-wrapper gap above 1% at CASH OPEN, so the corrected cost was not exercised end to end on a live rotate.
- What we changed because of it: `PRICE_IMPACT_UNIT` is now `fraction` and is the code default, so `_leg_cost_bps` is `abs(x) * 10000`. The "provisional" note on the unit is gone. Net bps from SWAP legs is now about 40 to 80 bps for a two-leg rotate at $50, so a rotate needs a gap above about 0.8%. RFQ legs and legs with no impact value keep the 5 bps floor. Raw impact fell as size grew while the rate drop grew, so the field is not slippage on this pair; unresolved.

### 2026-09-30 — skill speaks the four sentences (`streettape.py say`)

- Session: CASH OPEN throughout (15:31–15:50 UTC / 16:31–16:50 WAT / 11:31–11:50 ET). Not run when cash is shut.
- What we hit: `python skills/streettape.py say "<sentence>"` from Termux on a phone, against the deployed app, for all four sentences; plus `flatten --pct 0.01` and a direct `GET /api/board` read to check SPCXx. `baw` is not installed in Termux, so quotes went through `POST /api/swap/order`, never `baw`.
- What came back (first of each outcome; times UTC):
  - `say "what's rich vs Friday"` (15:31:18): `board`, session CASH OPEN, rows carry `symbol`, `tape`, `official`, `fair`, `premium`, `premiumToFair`. SPCXx tape 152.1675, official 150.15, fair 149.985, premium +1.34%, premiumToFair +1.46%. AAPLon +0.38%, NVDAon +0.18%. Closing line printed.
  - `say "rotate into cheapest NVDA"` (15:33:08) and `say "rotate into cheapest SPCX"` (15:39:23): both `arb: null`, closing line printed. No cross-wrapper gap cleared the 1% threshold, so `viable`, `netBps`, both legs and both ratios were not shown. The `viable: false` message did not fire.
  - `say "flatten anything 2% rich"` (15:34:32): `flatten: []`. Nothing was more than 2% over the official print (highest was SPCXx at +1.34%).
  - `flatten --pct 0.01` (15:40:57): `flatten: []` even though the board had shown SPCXx at +1.34% nine minutes earlier. A direct `/api/board` read at 15:47 showed SPCXx +1.24% with a `mint` and a `tokenPrice`, so it qualified. Re-run at 15:48:38 returned one $50 SPCXx sell quote: `provider=pancake`, `uiOutAmount` 50.031, `uiMinReceived` 49.5307, `priceImpactPct` null, `deepLink` present, `fallbackReason` "HTTP 200: userWalletAddress is required for RFQ (xStock) quote".
  - `say "alert only when cash is shut"` (15:49:22): empty output, 0 bytes, as specified while cash is open.
  - `/api/_devex`, first of each outcome: `quote` 200 with `code=40001 userWalletAddress is required for RFQ (xStock) quote` at 15:48:54 (the flatten quote above), then `quote fallback -> pancake`. Same code for an Ondo buy leg at 15:49:32 (`RFQ (Ondo)`), then Pancake fallback; that pair comes from the arb scan, not from `flatten`. A second identical pair at 15:50:41 is a later scan. No Binance quote succeeded on this pass. The one 429 (`code=42900`, `rwa/price`) is at 15:15:03, the deploy startup burst, before any command here.
- What we changed because of it: nothing in the code. The `say` parser, the extended `board` output and the shared closing line behaved as specified. Two flatten runs seconds apart disagreeing is a live premium moving across the 1% line, not a filter bug. Still not tested: `alerts` when cash is shut (it should print hits, then the closing line); a rotate with a live gap (`viable`, `netBps`, both ratios, and the "costs eat the gap" line); and the `baw` path. All Pancake fallback legs carry `priceImpactPct: null`, so any rotate net figure from them is the flat 5 bps per leg floor.

### 2026-09-30 — AI basket (`/basket/ai`, `/api/basket/ai`, `streettape.py basket`)

- Session: CASH OPEN throughout (20:01–20:21 WAT / 15:01–15:21 ET; log times 19:03–19:21 UTC). Not run while cash is shut, so the Fair row was not exercised.
- What we hit: `/` (hero link); `/basket/ai` in a main browser (19:03 UTC); `GET /api/basket/ai` (19:05); `python skills/streettape.py basket` (19:13) and `say "AI basket"` (19:14) from Termux, no `baw`; `Review & confirm` on the NVDAB and AMDon legs in a wallet dApp browser (19:19–19:21), not signed.
- What came back (log, first of each outcome, UTC):
  - Basket: 3 of 3 legs filled, $50.00 filled, $0.00 unfilled, on the page, the API and both CLI commands. Cheapest by per-share price: NVDAB (ratio 1.000778), AMDon (1.0), METAB (1.000548). `say "AI basket"` returned the same shape as `basket`.
  - Quotes, `USDT -> wrapper`, $16.67, no taker: NVDAB and METAB `GET /aggregator/quote` 200 ok at 19:03:02 (115 ms, 107 ms), `provider=binance_web3`, `mode=SWAP`, price impact 0.0012 and 0.0025 (fractions). AMDon: 200 with `code=40001 userWalletAddress is required for RFQ (Ondo) quote` at 19:03:02 (104 ms), then `quote fallback -> pancake`, so `provider=pancake` and an out-amount estimated from the price cache, not a firm quote. Same AMDon pair at 19:05:12 and 19:13:52. Process summary: 14 ok quotes, 6 Ondo 40001 plus 6 fallbacks (first 3 written).
  - Wallet pass, NVDAB: Review & confirm only landed on `/t/NVDAB#swap`. The Buy sheet did not open. Buy had to be tapped by hand, then the amount typed. Sheet offered BNB and USDC only, never USDT, and the amount was not the basket's $16.67. Built swap `BNB -> NVDAB` 0.05 BNB at 19:19:49 and 0.003 BNB at 19:19:54: 200 ok, LiquidMesh, `mode=SWAP`, rate 3.3163 / 3.3168 NVDAB per BNB, price impact 0.00051 / 0.00119. `tx/simulate` 404 again at 19:19:49. The wallet's own check then showed "This transaction is likely to fail" on the 0.003 BNB buy (balance 0.00536 BNB). Cancelled by hand, not signed. Cause of the warning not established.
  - Wallet pass, AMDon: same, Buy had to be tapped by hand. Sheet offered USDC only, default 10, and showed "Insufficient USDC balance" (balance 0) and "Opens PancakeSwap to complete the swap". Quotes with the real wallet as taker, 19:21:23 (0.003 USDC) and 19:21:44 (10 USDC): 200 with `code=40368 Ondo asset on chain 56 can only pair with allowed stablecoin(s); got: 0x8ac76a51...580d` (USDC), then Pancake fallback. So USDC is not an allowed pair for Ondo.
  - Basket page labels: the AMDon leg read `pancake · SWAP` and an exact out-amount. `_fallback_quote` hard-codes `executionMode: SWAP`, and Ondo is an RFQ asset.
  - Session chip on `/basket/ai` rendered grey while the board chip was green at CASH OPEN. The chip had no state class.
  - Also in the window: `GET /portfolio/tokens` 202 non-JSON at 19:19:24 (wallet connect, known). GeckoTerminal 429 on both tries for six wrapper addresses at 19:11:47-19:12:01, and AMDx `price_usd=None`. The basket reads the cached snapshot, so those did not add calls of their own.
- What we changed because of it: (1) `Review & confirm` now links to `/t/{symbol}#swap?pay=USDT&amt=16.67` and the token page opens the Buy sheet with USDT and that amount filled in (wallet must be connected first). (2) The swap sheet now offers USDT. Ondo wrappers pay in USDT only; USDC is removed for them, which corrects the 2026-09-30 share-normalized item 4 ("USDC only"). (3) The basket chip now takes the session class. (4) A leg quoted through the Pancake fallback is labelled "PancakeSwap · estimate, no firm quote" with an approximate out-amount, not `SWAP`. Not yet verified live: any of these four after deploy; whether Ondo accepts USDT (the wallet-less USDT quote stopped at 40001, before any pair check was seen); the likely-to-fail warning on the NVDAB BNB buy, and whether a USDT buy of NVDAB clears it; CASH SHUT (Fair row); an unfilled leg; signing; `baw`.

### 2026-10-01 — Agentic Wallet, one verified quote (`streettape.py verify`)

- Session: AFTER-HOURS (03:13 WAT / 02:13 UTC / 22:13 ET, 2026-09-30). Not run at CASH OPEN.
- What we hit: `baw market-order quote --fromTokenQty 50 --fromToken <USDT> --toToken <TSLAB> --binanceChainId 56 --json` by hand, then `python skills/streettape.py verify <USDT> <TSLAB> 50 --taker 0x6a12...6CAb` from Termux on a phone, against the deployed app. `baw` 1.10.0, signed in (`wallet status` CONNECTED). Quote only, nothing signed.
- What came back:
  - `baw` (JSON): `{"fromCoinSymbol":"USDT","fromCoinAmount":"50","toCoinSymbol":"TSLAB","toCoinAmount":"0.140416698567950691","slippage":0.01}`. No route, no price impact.
  - `/api/swap/order` (JSON, trimmed to the fields compared): `{"provider":"binance_web3","executionMode":"SWAP","uiOutAmount":0.1404166985679507,"routes":["LiquidMesh"],"priceImpactPct":0.000566223,"uiMinReceived":0.13901253158227117,"rate":0.0028083339713590137,"transactionFrom":"0x6a12...6CAb"}`. The full response also carried a built `transaction` (`to` 0xB444...DDA5, `value` 0x0, `gas` 0x6ddd0) and `sim.error: HTTP 404`.
  - `verify`: `deltaBps` 0.0, verdict `SAME ORDER`. Out-amount 0.1404166985679507 TSLAB on both paths. Impact 0.000566223 as a fraction is about 5.7 bps (API side only).
  - `/api/_devex`, first of each outcome (UTC, 02:12:53): `aggregator/quote` 200 ok (104 ms, taker set); `aggregator/swap` 200 ok (780 ms), `mode=SWAP`, vendor LiquidMesh; `tx/simulate` 404 (99 ms), body `{'message': 'No message available', 'status': 404, 'path': 'api/v1/dex/aggregator/tx/simulate', 'error': 'Not Found'}`. Same known simulate failure as 2026-09-29. No RFQ error: a taker was passed.
  - Termux failures before the pass: see Findings, Agentic Wallet skill (keytar/libsecret build, `DNS_RESOLVE_FAILED`). Two `auth verify` calls returned `AUTH_REJECTED` (`QR code does not exist or expired`, code 10002004) before the third succeeded, 1 min 48 s after `signin` (03:06:53 to 03:08:41).
  - `wallet balance` returned an empty list, so no signed fill was run.
- What we changed because of it: added `streettape.py verify` (runs `baw` and the API on the same pair and size, prints both, `deltaBps` and a verdict, exit 1 unless `SAME ORDER`; quote only). `baw` failures now print to stderr instead of falling back silently. Amounts are sent to `baw` without scientific notation. `baw` has no route or impact fields, so those read `null` on its side. Not done: a signed `baw` swap.

### 2026-10-01 — Agent Studio tick (`/api/agent/studio/tick`, identity card)

- Session: AFTER-HOURS (22:37 to 22:56 ET on 2026-09-30; 03:37 to 03:56 WAT on 2026-10-01). Cash shut. Not tested at CASH OPEN.
- What we hit: `GET /api/health`; `GET /agent-registration.json`; `GET /api/_ratiocheck`; `GET /api/agent/scan`; `GET /api/agent/studio/tick` with no token, then with `X-Studio-Token` (curl from Termux), then with `?arb_threshold=0.002`.
- What came back (log label `studio tick`, first of each outcome):
  - No token: HTTP 401 `{"detail":"bad token"}` at 02:38:38 UTC.
  - Token, default threshold: HTTP 200, `mode=proposal-only`, `session=AFTER-HOURS`, `arbs=0 alerts=0 flatten=0` at 02:41:29 UTC. `/api/agent/scan` also returned `hits: []`, `arbs: []`, `best: null`. `/api/_ratiocheck` returned all 20 share ratios with `missing: []`, so the empty list was not a ratio failure. Every xStocks wrapper except SPCXx showed as a thin pool, so only bStocks against Ondo were comparable, and their gap was under the 1% arb floor.
  - Token, `arb_threshold=0.002`: HTTP 200, 232 ms, `arbs=1 new=1` at 02:56:22 UTC. SPCXx rich over SPCXon, gross 45.9 bps, cost 10 bps, **net 35.9 bps, `viable: true`**, gap vs official 0.87%, gap vs fair 0.85%. Both legs fell back to Pancake links (`userWalletAddress is required for RFQ (xStock)` and `(Ondo)`), so the 10 bps cost is the 5 bps floor on each leg, not a fill.
  - `/agent-registration.json`: `x402Support: false`, `registrations` with `agentId 360062` on `eip155:56:0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`.
- What we changed because of it: (1) The tick had no entry in the dev log, because the logger only recorded outbound calls. The tick now logs status, mode, session, arb count and top net bps, and the token check uses a constant-time compare. (2) A quiet after-hours window returned `arbs=[]`, which cannot show a net-bps arb. The tick takes an optional `arb_threshold`; the default stays 1%, `viable` is still computed from net bps, and the response reports the threshold used as `arbThreshold`. (3) `.env.example` held a truncated registry address and agent id `360`, and an empty `ERC8004_CHAIN_ID` would have produced `eip155::0x...`. The values are corrected, the chain id falls back to 56, and `registrations` is emitted only for a numeric agent id. (4) The build notes called for a Studio job pointed at the tick. None exists: Studio is a CLI and has no external scheduler, so the scan stays in StreetTape's own loop and Studio was not deployed. `new` is true only on the first call per symbol in 60 minutes, so the manual curl used it up.

### 2026-10-01 — xStock pairs with BNB (`code=40370`)

- Session: PRE-MARKET (00:46 ET / 05:46 WAT / 04:46 UTC). Cash shut.
- What we hit: the swap sheet in a wallet dApp browser, BNB to TSLAx (buy) then TSLAx to BNB (sell), taker set. Quote only.
- What came back (log, first call): `GET /api/v1/dex/aggregator/quote`, HTTP 200, 776 ms, `code=40370 msg=xStock token only supports trading with: USDT, USDC.` at 04:46:08 UTC. The sell leg returned the same code, 95 ms, at 04:46:43 UTC. This is a third error code on the same endpoint (Ondo is `40368`), and the HTTP status is 200 again. At 04:46 the miss still fell through to a Pancake link. That link is gone.
- What we changed because of it: Binance `40370` now falls through to Pancake `GET /v1/quote` plus `POST /v1/calldata`. The order is signed on the site. See the 07:07 UTC block below for the signable BNB to TSLAx transaction. Nothing signed or sent. The MetaMask eligibility list we followed says BNB is swappable for xStocks, so the Binance aggregator and MetaMask disagree on this pair.

### 2026-10-01 — Ondo pairs only with allowed stablecoins (`code=40368`)

- Session: PRE-MARKET (00:50 ET / 05:50 WAT / 04:50 UTC).
- What we hit: `GET /api/_swapcheck` USDC to TSLAon, 5 USDC, taker set. First without a taker at 04:48:02 UTC, which returned the known `40001 userWalletAddress is required for RFQ (Ondo) quote`.
- What came back (log, first call): HTTP 200, 94 ms, `code=40368 msg=Ondo asset on chain 56 can only pair with allowed stablecoin(s); got: 0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d` at 04:50:49 UTC. USDC is not an allowed stablecoin for Ondo. The message does not list the allowed ones.
- What we changed because of it: `swap.js` already limited Ondo to USDT as the pay token, from the 2026-09-30 live call. This is the first captured body for it. Added a server-side block so BNB and Ondo never reach Binance: `swap.quote` and `/api/swap/order` return the notice, `/api/swap/order` as HTTP 400. The guarded call leaves no entry in this log, which is how we confirmed it. USDC to Ondo is still not blocked server-side.

### 2026-10-01 — Ondo RFQ minimum order (`code=40375`)

- Session: PRE-MARKET (00:51 ET / 05:51 WAT / 04:51 UTC).
- What we hit: `GET /api/_swapcheck` USDT to TSLAon, 5 USDT, taker set.
- What came back (log, first call): HTTP 200, 105 ms, `code=40375 msg=Minimum order amount is 5 USD.` at 04:51:55 UTC. 5 USDT was rejected, so the check is on USD value and USDT priced under $1 falls below it. The limit is not in the docs we used.
- What we changed because of it: nothing yet. The swap sheet does not warn about the $5 floor.

### 2026-10-01 — Ondo RFQ liquidity (`code=40374`)

- Session: PRE-MARKET (00:52 ET / 05:52 WAT / 04:52 UTC).
- What we hit: `GET /api/_swapcheck` USDT to TSLAon, 6 USDT, taker set.
- What came back (log, first call): HTTP 200, 323 ms, `code=40374 msg=Insufficient liquidity for a quote. Please decrease the transaction amount or try again later.` at 04:52:53 UTC. Repeated on the next two calls. Above the $5 floor and with a wallet, the RFQ still returned no quote in PRE-MARKET.
- What we changed because of it: nothing. Still no completed Ondo RFQ quote, so no Ondo fill. Not tested at CASH OPEN, when RFQ inventory may exist.

### 2026-10-01 — PancakeSwap `slippageTolerance` is a fraction

- Session: PRE-MARKET (02:33 ET / 06:33 UTC / 07:33 WAT). Cash shut.
- What we hit: `GET https://swap.pancakeswap.com/v1/quote` as the fallback after Binance, `chainId=56`, USDT to a wrapper, `slippageTolerance=1` (we send 1 for 1%, the unit Binance uses).
- What came back (log, first call): HTTP 400, 602 ms, `{'code': 'INVALID_REQUEST', 'message': 'slippageTolerance must be greater than 0 and less than 0.5, got 1'}` at 06:33:16 UTC. Same on every later call until fixed. Clear message, but the unit is a fraction, not a percent, and the two APIs disagree.
- What we changed because of it: Pancake gets `0.01`. Binance still gets `1`.

### 2026-10-01 — PancakeSwap `odd number of digits` (bad address)

- Session: PRE-MARKET (02:37 ET / 06:37 UTC / 07:37 WAT).
- What we hit: `GET /v1/quote` with a native-BNB input address that was 39 hex characters instead of 40 (our own typo in a test URL).
- What came back (log, first call): HTTP 400, 134 ms, `{'code': 'INVALID_REQUEST', 'message': 'odd number of digits'}` at 06:37:39 UTC. It names a hex-decoding fault, not the token field. Binance rejected the same address in 94 ms with `code=40001 Parameter [fromTokenAddress] error: invalid token address, EVM chains require 0x + 40 hex characters`, with an example address. HTTP 200 on the Binance side.
- What we changed because of it: nothing.

### 2026-10-01 — PancakeSwap quote and calldata, BNB to TSLAx

- Session: PRE-MARKET (03:07 ET / 07:07 UTC / 08:07 WAT). Cash shut.
- What we hit: Binance `aggregator/quote` BNB to TSLAx (`0x8aD3...7Cf0`), 0.004 BNB, then the Pancake fallback. Native BNB is `0x0000...0000` on Pancake, not the `0xEeee...` address Binance uses. Quote only, nothing signed.
- What came back (log, first call):
  - Binance: HTTP 200, 94 ms, `code=40370 xStock token only supports trading with: USDT, USDC.` at 06:45:13 UTC.
  - Pancake `GET /v1/quote`, no wallet (recipient was a dummy address): HTTP 200, 724 ms at 06:45:14 UTC; 407 ms on the next call. A quote with a dummy recipient was accepted.
  - Pancake `GET /v1/quote` with the wallet as recipient (`0x4B95...4331`): HTTP 200, 474 ms at 07:07:38 UTC, then `POST /v1/calldata` with the `best` object: HTTP 200, 319 ms at 07:07:39 UTC. Later pairs of calls took 96 and 93 ms.
  - `/api/swap/order` shape: `provider=pancake`, `uiOutAmount` 0.007860 TSLAx, `uiMinReceived` 0.007782, rate 1.965 TSLAx per BNB, `transaction.to` `0x2f68...cfF7`, `value` `0xe35fa931a0000` (0.004 BNB). `priceImpactPct` is `null`: the Pancake response carried no impact field we use. The calldata holds the WBNB, USDT and TSLAx addresses, so the route appears to go through USDT.
  - Not verified: signing or sending this transaction.
- What we changed because of it: the Pancake fallback now returns a signable transaction instead of a link. `deepLink` is gone from the order response.

### 2026-10-01 — OpenOcean `/swap` NETWORK_ERROR, `/quote` and `/swap_quote` work

- Session: PRE-MARKET, before the 07:07 UTC server checks. These were browser calls by hand, so they are not in `/api/_devex`.
- What we hit: `GET open-api.openocean.finance/v3/bsc/swap`, then `/quote`, then `/swap_quote`, BNB to TSLAx, 0.004, `gasPrice=1`, `slippage=1`.
- What came back (first call of each):
  - `/swap` with a dummy `account`: HTTP body `{"reason":"could not detect network","code":"NETWORK_ERROR","event":"noNetwork"}`. Same twice, and again with the real wallet as `account`. It is an ethers.js error leaking from their backend. No `message` field and no `code` that matches the usual 200/400 shape.
  - `/quote` (no account): `code 200`, `outAmount` 8626886320000001 (0.008627 TSLAx), `estimatedGas` `"174060"` (string), `price_impact` `"-0.17%"` (string, signed, with a percent sign), route BinarySwap (fee 0) plus a PancakeV2 slice.
  - `/swap_quote` with the real wallet: `code 200`, `outAmount` 8630208319999999, `minOutAmount` 8500755195200000, `estimatedGas` `350396` (a number here, a string in `/quote`), `to` `0x6352...4e64`, `value` 4000000000000000, `data`, `gasPrice` `"1000000000"`, `price_impact` `"-0.17%"`.
- What we changed because of it: the adapter calls `/swap_quote` when it has a wallet and `/quote` when it does not. `/swap` is not used. Price impact is parsed from the string and divided by 100 to match our fraction convention.

### 2026-10-01 — OpenOcean 403 from the server only

- Session: PRE-MARKET (03:07 ET / 07:07 UTC / 08:07 WAT).
- What we hit: the same `swap_quote` URL that returned JSON in a phone browser, now from the Railway Southeast Asia (Singapore) container, as the Pancake fallback.
- What came back (log, first call): `GET /v3/bsc/swap` at 06:37:39 UTC, HTTP 403, 105 ms, plain-text body `Forbidden; you don't have permission to access this resource.` That call also carried our 39-character address typo, and the 403 came back before any parameter check. Not JSON, so our parser also logged a non-JSON failure. After we added a browser-style `User-Agent` and `Accept: application/json`, moved to `swap_quote` and fixed the address: HTTP 403, 34 ms at 07:07:57 UTC. A 34 ms rejection looks like an edge rule, not the API.
- What we changed because of it: the User-Agent header, which did not help. The OpenOcean fallback stays in the code but cannot fire from this host. Not tried: another OpenOcean host, an API key, or another region.

### 2026-10-01 — TSLAx price by venue (BNB to TSLAx, 0.004 BNB)

- Session: PRE-MARKET (03:07 ET / 07:07 UTC). Cash shut. One pair, one size, minutes apart.
- What came back: Pancake calldata path returned 0.007860 TSLAx. OpenOcean `swap_quote` (browser) returned 0.008630 TSLAx, about 9.8% more for the same BNB. OpenOcean's own price for TSLAx was $356.70, near the Yahoo print of $354.81. Pancake's rate of 1.965 TSLAx per BNB at BNB near $771 implies about $392 per TSLAx, near the $390 seen on 2026-10-01 at 06:05 WAT.
- What we changed because of it: nothing. The Pancake fallback is live and OpenOcean is not reachable from the server, so the cheaper route is not available to users yet. This agrees with the MetaMask eligibility list: BNB to TSLAx is tradable on Pancake and OpenOcean, just not through Binance's aggregator (`40370`).
