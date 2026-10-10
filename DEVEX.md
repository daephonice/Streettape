# StreetTape - Developer Experience Report

Mid-build notes, last updated 2026-10-10. Written from live calls, not from the docs after the fact.

This file lives in the repo so a Railway deploy cannot wipe it. The download at `GET /api/_devex` is a *different* file: it is whatever `devlog.py` wrote on that container, and it resets on every deploy. Do not treat `/api/_devex` as this report. When a feature is ready to test, download `/api/_devex`, cut it down to the new calls for that feature, and paste them under **Runtime log** at the bottom.

- Product: https://streettape.up.railway.app
- Agent registration: https://streettape.up.railway.app/agent-registration.json
- Chain: BSC mainnet (`binanceChainId=56`)
- Stance: quote, flag, alert. The user signs. Nothing auto-executes.

> ## If I owned the platform
>
> 1. **`binanceChainId` is required on `rwa/price` and the docs must say so**, next to `tokenContractAddresses` (plural), like every sibling endpoint. It cost 213 failed calls.
> 2. **Return non-200 on validation failure.** `40001` arrives as HTTP 200 today, so any client that checks status treats it as success. Use `400`.
> 3. **Publish the rate limit.** `web3.binance.com` has none listed. Observed: 5 pass, next 8 at ~80 ms return `429 code=42900`, recovery in under 1 s.
> 4. **Name the unit of `priceImpactPct` in the field description.** It is a fraction (`0.0005` = 5 bps), not a percent, and on TSLAB it falls as size grows.
> 5. **Whitelist developer-origin cloud IPs, or say outright that serverless deploys cannot call RFQ.** Valid keys on Railway's default region got `compliance restriction`; Singapore fixed it. RFQ also needs `userWalletAddress`; the error should list every missing field and return the AMM quote beside it.

---

## Summary

- Built against Binance Web3 quote/RFQ/swap-build, RWA Data, Transaction sim, Address Portfolio, GeckoTerminal tape, Yahoo last cash print, BSC RPC.
- Verified live: aggregator quotes and built SWAP transactions (LiquidMesh), RFQ-requires-wallet, RWA `platforms` / `search` / `underlying-profile` / `underlying-market`, rate-limit behaviour, ERC-8004 registration on BSC, one settled TSLAB buy ([0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5](https://bscscan.com/tx/0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5)).
- Called live, failed: `POST /api/v1/dex/aggregator/tx/simulate` HTTP 404; `GET /api/v1/portfolio/tokens` HTTP 202 non-JSON (19/19); `rwa/price` no usable mark on 420/420 calls in the logged window (missing `binanceChainId`, then 429).
- Verified live (2026-10-01): one `baw market-order quote` and `/api/swap/order` returned the same out-amount for USDT to TSLAB at 50 (see Runtime log).
- Verified live (2026-10-01): `/api/agent/studio/tick` returned an SPCX arb at 35.9 bps net, proposal-only, and 401 without the token (see Runtime log).
- Verified live (2026-10-01): Ondo and xStock RFQ errors `40368`, `40370`, `40374`, `40375`, all HTTP 200 (see Runtime log).
- Verified live (2026-10-01): PancakeSwap `GET /v1/quote` + `POST /v1/calldata` returned a signable BNB to TSLAx transaction when Binance refused the pair with `40370` (see Runtime log).
- Called live, failed: OpenOcean `swap` / `swap_quote` returns HTTP 403 plain text from the Railway Singapore server while the same URL returns JSON in a phone browser (see Runtime log).
- Verified live (2026-10-03): Studio tick from the managed runtime returned 401 every minute until the platform dropped the custom token env; with a tick-only key it returned 200 `mode=proposal-only arbs=1` in 1.9 to 2.3 s, and the runtime stopped about 5 minutes after the last invoke (see Runtime log).
- Verified live (2026-10-10): one signed `baw market-order swap`, 0.0027 BNB to TSLAB, status FINISHED, tx [0x3f2e027a3c356783cb39be21b69cb0959d15fed3c2f13dfc09f79b4ba7d2161a](https://bscscan.com/tx/0x3f2e027a3c356783cb39be21b69cb0959d15fed3c2f13dfc09f79b4ba7d2161a) (see Runtime log).
- Settled rotate (2026-10-10): NVDAon to NVDAB through Pancake, tx [0x160d05c5ea2c347d70b5da4498ac254732f28554ade688008970478d20bbd8b7](https://bscscan.com/tx/0x160d05c5ea2c347d70b5da4498ac254732f28554ade688008970478d20bbd8b7); second run [0x7a0b6cdf2d47f382529693522abc4dba6e7dde0927b3907712d2ad02aa0562f9](https://bscscan.com/tx/0x7a0b6cdf2d47f382529693522abc4dba6e7dde0927b3907712d2ad02aa0562f9) (see Runtime log).

---

## AI stack

- **No model computes a price.** Tape is GeckoTerminal, last cash print is Yahoo, quotes are Binance (Pancake as fallback). Gaps, share ratios and net bps are plain Python arithmetic in the app.
- **The skill is `skills/streettape.py`.** Closed grammar (`say` accepts a fixed set of sentences: rich vs Friday, rotate into cheapest X, flatten anything N% rich, alert only when cash is shut; plus `basket`). Stdlib only. It is a thin client over the StreetTape API and invents nothing.
- **Quote and sign.** `baw market-order quote` when `baw` is on PATH, otherwise `POST /api/swap/order`. Signing is only `quote --sign`, and only after the user says yes. Nothing auto-executes.
- **One live comparison, 2026-10-01.** `baw` and `/api/swap/order` returned the same out-amount (0.1404166985679507 TSLAB, `deltaBps` 0.0) for USDT to TSLAB at $50. Quote only. A signed `baw` fill was **not** run that day: the agentic wallet was empty. It was run on 2026-10-10 (see Runtime log).
- **Studio.** Command: `bag deploy --provider bnb --accept-risk --yes`, agent `01M401H27Z9Q80XTFB0ZW8K5HC`, plus a 60 s `tick.ts` loop calling `GET /api/agent/studio/tick`. Result (2026-10-03): the deployed runtime got 401 every minute because the platform dropped the custom token env; after a tick-only key, 200 `mode=proposal-only arbs=1` in 1.9 to 2.3 s. The runtime stops about 5 minutes after the last invoke, so this is a trial, not a scheduled job. Details in the Runtime log, "Studio tick from the managed runtime."
- **Which file is the report.** This one. `GET /api/_devex` is the container log and resets on deploy.

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

After US close the wallet flagged a Binance-built BNB to NVDAB swap "likely to fail." Cancelled. A later TSLAB buy settled ([0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5](https://bscscan.com/tx/0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5)); holdings then showed via public RPC. Simulate is parked for an hour after a 404.

**Proof of the check is off-chain.** Each quote from `/api/swap/order` carries `checkProof`: sha256 over quote id, both mints, amount, out-amount and per-share prices (payload included, so anyone can recompute it), shown on the swap sheet. It is not anchored on-chain: `tx/simulate` is 404 on this key, so there is no live simulation to attest, and a hash of a quote is not a broadcast. The one settled fill above is the on-chain proof.

### Address Portfolio is not a portfolio

`GET /api/v1/portfolio/tokens` on `web3.binance.com/wallet` — 19/19 HTTP 202, non-JSON, 10-40 ms. Holdings in the UI come from public BSC `eth_call` / `eth_getBalance`. Parked for 10 minutes after a failure.

### Agentic Wallet skill is a CLI, not a library

- `npx skills add binance-agentic-wallet` fails. The CLI wants the GitHub folder path under `binance/binance-skills-hub`.
- On Termux the same command needs `git` on PATH, then a Node/OpenSSL upgrade.
- On Termux the CLI itself (`npm install -g @binance/agentic-wallet`, `baw` 1.10.0) failed first on `keytar` (`Package 'libsecret-1' was not found`). It installed after `pkg install libsecret pkg-config python make clang` and `npm install -g --allow-scripts=@github/keytar @binance/agentic-wallet`.
- `baw auth verify` returned `DNS_RESOLVE_FAILED (www.binance.com)` while `auth signin` and `app.binance.com` worked. Changing the phone's Private DNS fixed it. The pairing code lasts about 5 minutes and the link is a QR to scan with the Binance app.
- `baw market-order quote` returns symbols, amounts and slippage only. No route and no price impact. Those come from the API path.
- The skill wraps `baw market-order quote` / `swap`. It does not sign from Python.
- StreetTape calls `baw` when it is on PATH, else `/api/swap/order`. `--sign` is explicit. A live `baw` quote has been run (Runtime log 2026-10-01). A signed `baw` fill was run on 2026-10-10 (Runtime log).

### ERC-8004 identity. Studio runtime is a trial, not a scheduler.

Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` (BSC, ERC1967 proxy). Three unlabeled `register` overloads on BscScan. Used the `agentURI`-only one. Token / agent id **360062**. Fee ~0.000009 BNB. Registration file is served at `/agent-registration.json`.

Agent Studio was deployed on 2026-10-03 as a 48h BNB trial (agent `01M401H27Z9Q80XTFB0ZW8K5HC`, A2A, price 0, nothing signed). It is not a scheduled job: the managed runtime scales to zero when idle and the 60s tick loop only runs while something invokes it. No Studio scheduler that calls an external URL was found or tested. The scan also runs in StreetTape's own loop, and `GET /api/agent/studio/tick` exposes the same scan (proposal-only, token-gated). StreetTape has no paid endpoint and never signs. See the 2026-10-03 Studio blocks in the Runtime log. Mainnet agent 360062 is the product identity, registered directly on the Identity Registry. `x402Support` stays false in the card.

### Multiplier and missing contracts

- Seed: bStocks `multiplier: 1.0`, xStocks/Ondo `None`. Arb divides only when the value is truthy. `normalized` is true if *either* wrapper has a multiplier, so NVDAB vs NVDAon reports normalized even though one side was raw.
- AAPLB seed address is `0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a` (BSC), taken from Binance's eligible-bStock token list, not from RWA Data discovery (RWA Data never returned one, so `merge_dynamic()` would not have filled it). `multiplier` stays 1.0 and no price is hardcoded. Gecko has a pool for it: tape, a price and $1.37M liquidity on 2026-10-02 (see Runtime log), so the row can enter the arb. A wrapper with no pool would still show "no tape".

---

## How the assets behaved

- **bStocks** quoted and filled on AMM after hours. TSLAB buy settled ([0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5](https://bscscan.com/tx/0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5)). NVDAB build after close was flagged likely-to-fail by the wallet. Cause not proven (liquidity vs transfer limits vs stale route).
- **Ondo / xStocks** quotes without a taker are RFQ and die. With a taker, PRE-MARKET Ondo quotes failed on `40368` (USDC), `40375` ($5 minimum) and `40374` (no liquidity), and xStocks failed on `40370` (BNB). No completed RFQ quote or fill yet.
- **xStocks tape on Gecko** is often missing for names that have a contract. No tape means no Buy and no arb leg.
- **SpaceX** has no Yahoo mark. Premium vs official is blank. Cross-wrapper gap still exists (SPCXon rich vs SPCXB).

---

## If I rebuilt the developer platform (full list)

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
- Done 2026-10-10: one signed `baw market-order swap` (BNB to TSLAB, FINISHED).

---

## Rotate sheet (2026-10-09)

The 2026-09-29 desk pass opened Sell, then Buy on close (`Rotate $50`). That is no longer the control.

- Rotate is a third sheet, same shell as Buy and Sell. Heading `ROTATE {rich} to {cheap}`.
- Size is 25 / 50 / 75 / MAX of the rich wrapper, plus the bar. No dollar chips on this sheet.
- Disconnected, the button says Connect wallet. Connected, it says Rotate.
- One `POST /api/swap/order`, input mint the rich wrapper, output mint the cheap wrapper. The aggregator may hop inside that transaction. The user signs once.
- BNB against Ondo stays blocked (`unsupported_pair`). No route, or output under 95% of expected, leaves the button disabled.
- `#rotate`, the board Rotate link, and the token Rotate button open this sheet. Sell, Buy, and Flatten are unchanged.
- Not signed. No settled rotate.

---

## Runtime log

Only live-key outcomes the file does not already have. One block per outcome, first call only. No redesign, layout, copy or parser sessions. No Yahoo polls, `balanceOf` reads or repeat calls with a status already pasted. `/api/_devex` is disposable.

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
- What we changed because of it: Sell TSLAon and Flatten did nothing. Server keys prices/assets by uppercase symbol (`TSLAON`); the page looked up `TSLAon`, missed, and the swap sheet returned silently. TSLAB is already uppercase, so Buy worked. Page now resolves the uppercase key, and Rotate opens Sell first, Buy on close. Superseded 2026-10-09: Rotate is its own sheet (`side=rotate`), not Sell then Buy. The 2026-09-29 pass is what we hit that day. The Pancake link opened when the sheet could not. That link was removed later: a miss now returns a Pancake transaction or no route. Next: send `tokenContractAddresses` to `rwa/price`.

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
- What we changed because of it: (1) The tick had no entry in the dev log, because the logger only recorded outbound calls. The tick now logs status, mode, session, arb count and top net bps, and the token check uses a constant-time compare. (2) A quiet after-hours window returned `arbs=[]`, which cannot show a net-bps arb. The tick takes an optional `arb_threshold`; the default stays 1%, `viable` is still computed from net bps, and the response reports the threshold used as `arbThreshold`. (3) `.env.example` held a truncated registry address and agent id `360`, and an empty `ERC8004_CHAIN_ID` would have produced `eip155::0x...`. The values are corrected, the chain id falls back to 56, and `registrations` is emitted only for a numeric agent id. (4) The build notes called for a Studio job pointed at the tick. None exists: We did not run Studio (`bag` is not installed). In the Studio docs we read (overview, quickstart, CLI reference, architecture), we found no scheduler that calls an external URL. That absence was not tested. The scan stays in StreetTape's own loop and Studio was not deployed. `new` is true only on the first call per symbol in 60 minutes, so the manual curl used it up.

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

### 2026-10-02 — Defensive rotation, xStock sell leg has no route (`/api/agent/defensive`)

- Session: PRE-MARKET (08:42 ET / 12:42 UTC / 13:42 WAT). QQQB +1.12% since Fri close. `AGENT_THRESHOLD` was lowered to 0.005 for the test, because the default 1.5% was not crossed (+1.19%, `triggered:false`, no quotes).
- What we hit: `GET /api/agent/defensive?usd=50` twice. Three USDT sell quotes at $16.67 each (NVDAx, AMDB, METAB), then one USDT to QQQB buy quote for the dollars the sells quoted.
- What came back (log, first call):
  - NVDAx to USDT, `GET /aggregator/quote`, 12:42:52 UTC, HTTP 200, 161 ms, `code=40001 userWalletAddress is required for RFQ (xStock) quote`. The cheapest NVDA wrapper per share was an xStock this time (NVDAx, ratio 1.0, tape 227.99), not NVDAB, so the sell side hit RFQ. The basket buy side picked NVDAB on 2026-09-30.
  - Fallback on the same leg: Pancake `GET /v1/quote` returned HTTP 200 `ok` in 684 ms, yet our code reported `pancake: no agg route and no pcsx permit`, so the 200 carried no usable route for the sell. OpenOcean `GET /v3/bsc/quote` returned 403 plain text in 69 ms. Until now the 403 was only seen on `swap_quote`, so `/quote` is blocked from the server too.
  - AMDB and METAB: `binance_web3`, LiquidMesh, `priceImpactPct` 0.0 and -0.0. METAB `tokenToShareRatio` 1.000548. QQQB buy: ratio 1.000725, `priceImpactPct` 5.03512e-05 (fraction), $33.31 in, 0.04437 out. These three returned 200 on the first call.
  - Result: NVDAx unfilled ($16.67), quoted $33.31 of $50, `viable:false`. Its weight was not given to AMD or META. Latency 3.4 s for the triggered call, 2.3 s untriggered.
- What we changed because of it: `viable` is now false when any leg is unfilled, including a no-tape name (before, only quoted legs counted). Nothing changed for the RFQ error: the leg stays unfilled and the reason is returned. Not tried: a taker address on the NVDAx sell, or picking the cheapest non-RFQ wrapper when the cheapest one is an xStock.

### 2026-10-02 — Semis and defensive baskets, names missing from the snapshot (`/api/basket/semis`, `/api/basket/defensive`)

- Session: PRE-MARKET (09:27 ET / 13:27 UTC / 14:27 WAT).
- What we hit: `GET /api/_devcheck`, `GET /api/board`, `GET /api/basket/semis?usd=50`, `GET /api/basket/defensive?usd=50`, then `/basket/semis` and `/basket/defensive` in a phone browser.
- What came back (first call of each):
  - `_devcheck`: HTTP 200, all four RWA Data endpoints ok (`platforms` 2 items, `search` 1, `underlying_profile`, `underlying_market`). The key works. The `rwa/tokens` listing call logged HTTP 200 in 540 ms at 13:16:45 UTC.
  - `/api/board`: 7 underlyings. AVGO, KO, PG and JNJ were not among them, although the listing call was ok and `merge_dynamic` had run. So the RWA Data listing, as read here, carries none of the four on BNB Chain. The log records status and latency only, not the listing body, so this is inferred from the snapshot, not read from the response.
  - Semis: HTTP 200. NVDAB (ratio 1.000778) and AMDB (ratio 1.0) filled with LiquidMesh USDT quotes. AVGO unfilled, reason `not in snapshot yet`. $33.33 filled, $16.67 unallocated.
  - Defensive: HTTP 200. KO, PG and JNJ all unfilled, `not in snapshot yet`. $0.00 filled, $50.00 unallocated.
- What we changed because of it: nothing in the data path. A name that is not in the snapshot stays an unfilled leg and its weight is never given to the others. Not verified: whether these tickers are listed on another chain or under another platform; a filled AVGO, KO, PG or JNJ leg.

### 2026-10-02 — Liquid universe, `rwa/tokens` listing read in full (`/api/_boardcheck`)

- Session: CASH OPEN (10:45 ET / 14:45 UTC / 15:45 WAT).
- What we hit: `GET /api/v1/dex/market/rwa/tokens`, parsed for the first time, then one Gecko `tokens/multi` probe over every addressed wrapper to pick the board.
- What came back (first call):
  - `rwa/tokens`: 488 rows, all `binanceChainId` "56", 448 underlyings. Row keys: `tokenContractAddress`, `underlyingTicker`, `platformId`, `tokenSymbol`, `tokenToShareRatio` (a string), `tokenPrice`, `referencePrice`, `volume24H`, `marketCap`, `statusInfo`. The listing carries no `tokenAddress`, `underlying` or `platform` key. Our parser read those three, so it dropped all 488 rows without an error, and the board stayed at the seven seeds. `_devcheck` was green the whole time, since it never calls the listing parser.
  - First row (WOLFon, Ondo): `tokenPrice` and `referencePrice` both "36.1475", the same to every digit, as on `rwa/price`. `platformId` is `ondo`; bStocks come back as `bstock` (singular) and we use `bstocks`.
  - Gecko probe, no 429: of 441 non-seed underlyings, 20 had a wrapper with a tape and at least $500 of liquidity. About 421 had no pool or one under $500. 13 were kept under the 20-name cap; 7 passed but did not fit (MUU, HIMX, LLY, REMX, PLTR, IBM, NTES).
  - `/api/board` after: 20 underlyings, every one with a mark, no thin wrapper outside the seeds. Smallest kept tape is MSTRon at about $2,900; deepest is QQQB at about $7.6M.
  - Corrects the 2026-10-02 baskets entry: AVGO, KO, PG and JNJ are in the listing. They were missing from the snapshot because of our parser. AVGO now has a tape (AVGOB, about $7,400) and sits on the board; KO, PG and JNJ are listed but have no tape of $500 or more.
- What we changed because of it: the parser reads the real keys and keeps chain 56 only. The board is capped at 20 underlyings: seeds always kept, the rest ranked by best-wrapper liquidity, wrappers kept only with a non-thin Gecko tape. Not verified: a Buy on any new name, or whether the 421 "no tape" names have pools Gecko has not indexed.

### 2026-10-03 — `priceImpactPct` re-probed: unit holds, value moves 5x between runs (`/api/_impactprobe`)

- Session: AFTER-HOURS (21:09 ET Fri / 01:09 UTC Sat / 02:09 WAT Sat). Cash shut.
- What we hit: `GET /api/_impactprobe?pair=TSLAB&big_usd=5000` with a taker set. BNB to TSLAB, `executionMode=SWAP`, one call per size.
- What came back (first call of each):
  - $50: provider `binance_web3`, route LiquidMesh, `priceImpactPct` 0.0008153811, rate 2.07126 TSLAB per BNB.
  - $5,000: same provider and route, `priceImpactPct` 0.0005161235, rate 2.07011. The rate drop against the $50 quote is 5.5 bps. The raw field read as a fraction is 5.2 bps, 0.93 of the drop.
  - Against the 2026-10-01 probe on the same pair: raw at $50 was 0.00389, now 0.000815, about 4.8x lower with the same route and size. Raw at $5,000 was 0.00273, now 0.000516.
  - Raw still falls as size grows (8.2 bps to 5.2 bps), as before. This time it lands near the observed rate drop at $5,000; last time it was 19 to 39 bps against a 15 to 21 bps drop.
- What we changed because of it: nothing. `PRICE_IMPACT_UNIT=fraction` is confirmed by the probe's own verdict. The field is a fraction but not stable between runs, so it is not a reliable cost input on its own. Not verified: why the value shifts between runs. TSLAon and TSLAx could not be probed for impact: both refused BNB (see the two blocks below).

### 2026-10-03 — Ondo wrapper refuses BNB with a new message (BNB to TSLAon)

- Session: AFTER-HOURS (Sat, minutes after the TSLAB probe). Cash shut.
- What we hit: `GET /api/_impactprobe?pair=TSLAon&big_usd=5000` with a taker set. BNB to TSLAon at $50 and $5,000.
- What came back (first call of each size): no route, same text both times: `Swaps between this token and real-world assets aren't supported yet. Try using a different token.` No provider, no mode, no impact. Our probe read it from the failed quote, so the HTTP status and any `code` were not captured here.
- Differs from the 2026-10-01 AMDon BNB buy, which returned `code=40368 Ondo asset on chain 56 can only pair with allowed stablecoin(s)`. Same wrapper family and the same BNB input, different wording. The text does not say which tokens are allowed.
- What we changed because of it: the probe now returns the failure reason per run. No change to the quote path. Not verified: the status and `code` for this text; whether it is a second code or the same one with a different message.

### 2026-10-03 — Pancake fallback returned a Cloudflare 502 page (BNB to TSLAx)

- Session: AFTER-HOURS (Sat, same pass). Cash shut.
- What we hit: `GET /api/_impactprobe?pair=TSLAx&big_usd=5000`. BNB to TSLAx at $50 and $5,000. Binance refused first, so the fallback chain ran.
- What came back (first call of each size):
  - Binance: HTTP 200, `xStock token only supports trading with: USDT, USDC.` Already logged as `40370` on 2026-10-01.
  - Pancake: HTTP 502, non-JSON, a Cloudflare HTML error page. On 2026-10-01 the same pair returned a signable transaction from Pancake. This is the first Pancake failure we have logged.
  - OpenOcean: HTTP 403, plain text `Forbidden; you don't have permission to access this resource.` Already logged.
  - All three failed at both sizes, so BNB to TSLAx had no route in this pass.
- What we changed because of it: nothing. A 502 from Pancake is treated like any other failed provider and the next one is tried. Not verified: whether the 502 was a one-off or lasts, or a retry.

### 2026-10-03 — `bag` install and toolchain on Android arm64 (Termux)

- Session: WEEKEND (Sat, cash shut).
- What we hit: `npm install --global @bnbagent/studio-cli`, then `pnpm install` in the scaffold, then `bag deploy`.
- What came back (first call of each outcome):
  - npm: exit 1. `keytar@7.9.0` has no prebuilt binary (`platform=android arch=arm64`), falls back to `node-gyp rebuild`, fails at `node-addon-api/napi.h:1147` ("in-class initializer for static data member is not a constant expression") on node 26.4.0.
  - `--ignore-scripts` install worked: 281 packages in 3 min, `bag` 0.0.14. Not tested: anything that needs the OS keychain.
  - pnpm: `ERR_PNPM_PNPM_ENGINE_NO_NATIVE_BINARY`, `@pnpm/exe@10.24.0` ships no android-arm64 binary. Cause is the `packageManager` pin in the scaffold's `package.json`. Deleting the pin by `sed` left a trailing comma ("trailing comma at line 4 column 1").
  - pnpm then failed with `No space left on device (os error 28)`. Retry with the store warm: 2m11s.
  - `bag deploy` and `bag platform login`: "could not start the lockfile-pinned bnbagent-deploy; install Bun 1.3+". `bag doctor` said "bun not found" with `bun` 1.4.2 on PATH. The missing piece was the `which` command; after `pkg install which`, `bag deploy prepare` reported 0 CRITICAL. The error text does not point at `which`.
  - `bunx --bun @bnbagent/deploy-cli --help`: resolved 599 packages, no output, killed by a 120s timeout. The local copy under `studio-cli/node_modules` ran fine with `bun --bun .../src/cli.ts`.
- What we changed because of it: removed the pnpm pin, installed `which`, freed disk. Not verified: whether the `bunx` hang was the network or Bun on android.

### 2026-10-03 — Platform login and deploy prompts (`bnb` trial)

- Session: WEEKEND (Sat, 04:46 to 05:11 UTC).
- What we hit: calling `deploy-cli` directly (the `bag` override `BNBAGENT_DEPLOY_COMMAND` is refused outside local dev), `bag deploy prepare`, `bag deploy --provider bnb`.
- What came back (first call of each outcome):
  - Direct `--provider bnb login`: "trial platform endpoint is not configured". It needs `BNBAGENT_API_URL=https://bnbagent-api.bnbchain.world` (found in the `bag` bundle, not in the help). With it, GitHub device-code login worked.
  - `prepare`: CRITICAL `commerce_no_rail` until `payments.b402_seller.enabled = true` (price stays 0; erc8183 left off).
  - `bag deploy`: typing `yes` at the trial-terms prompt returned "platform deploy cancelled — the trial terms were not accepted" while stdout was piped through `tee`. `--accept-risk --yes` worked.
  - Deploy: 1m47s first, 1m29s on redeploy. Same agent id, new deployment id, expiry unchanged at 2026-10-05T04:50:01Z.
  - `bag` printed "HTTP 404; no verified x402 payment challenge" for the free x402 route. Card served `skills: []`.
- What we changed because of it: `payments.b402_seller.enabled = true`; deploy with `--accept-risk --yes`. `x402Support` stays false.

### 2026-10-03 — Studio tick from the managed runtime (agent 01M401H27Z9Q80XTFB0ZW8K5HC)

- Session: WEEKEND (Sat).
- What we hit: a `tick.ts` loop in the scaffold calling `GET /api/agent/studio/tick` every 60s with `X-Studio-Token`, price "0".
- What came back (first call of each outcome):
  - Deployed runtime: HTTP 401 every minute (04:51:39 to 04:56:39), 270 to 283 ms. The platform forwarded only 3 secrets (`BNBAGENT_DELIVERABLE_STORAGE_MODE`, `WALLET_KEYSTORE_JSON`, `WALLET_PASSWORD`). The CLI builds the secret list from a fixed allowlist (wallet, LLM, storage, `RPC_URL`, public URL, B402); a custom env name is dropped, with no warning.
  - Local `bag dev`: 200, 906 to 2924 ms from a phone. Runtime after the fix: 200, `mode=proposal-only arbs=1`, 1.9 to 2.3 s.
  - Cold start: the first card request took 6.9 s, 5.4 s and 5.8 s (after ~3 h idle). Logs: "No logs yet — the runtime boots on the FIRST invoke". Status stayed `running`.
  - Idle: no tick lines between 04:56:39 and 05:12:24, and none after ~3 h. The loop does not run unattended. Two wakes, same shape: serving 04:51:39, last tick 04:56:39; serving 08:20:36, last tick 08:25:37 (still the last line at 08:35:37). The runtime stays up about 5 minutes after the last invoke, then stops. The limit is read from log timing only, not from any platform setting.
- What we changed because of it: added a second tick-only credential (`AGENT_TICK_KEY`, query param `k`, tick endpoint only) and baked it into the uploaded bundle, because the header token cannot reach the job. The key is readable by the operator; the endpoint is proposal-only and never signs. `studioRuntime.deployed` stays false (not a scheduled job). Not verified: any platform-side scheduler or keep-warm setting.

### 2026-10-03 — Weekend book: Ondo sells at $50 carry 14 to 19% Pancake impact (`/api/agent/scan`, `/api/agent/arb/{TSM,TSLA,META}`)

- Session: WEEKEND (Sat 06:05 ET / 10:05 UTC / 11:05 WAT). No taker, USDT to wrapper, $50.
- What we hit: `GET /api/agent/scan` (the `book` array), then `GET /api/agent/arb/{TSM,TSLA,META}?size_usd=50`.
- What came back (first call of each outcome):
  - Book: 3 Clears (AAPL net +5.5 bps, NVDA +9.4, QQQ +0.65), 5 Refused (SPCX and META `no route`, TSM, TSLA and AMD `cost eats gap`), `best: null`. QQQ read gap 13 bps, net +5 at 08:54 UTC and gap 10.6 bps, net +0.65 at 10:05 UTC. A clear is thin and does not hold for an hour.
  - TSM and TSLA sell legs: Binance `40001 userWalletAddress is required for RFQ (Ondo) quote` (HTTP 200, 178 ms, 08:53:49 UTC), then Pancake `GET /v1/quote` HTTP 200, 728 to 835 ms. Pancake `priceImpactPct` 0.1926 (TSM) and 0.1484 (TSLA), `priceImpactTooHigh: true`. $50 in gave $40.37 and $42.59 out. The field and the quote agree here, as a fraction: `costBps` 1926 and 1489, AMD 901 in the book. The Binance LiquidMesh buy legs on the same pairs read impact 0.0 and 0.00055.
  - META sell leg (METAon): Binance Ondo `40001`, then Pancake `no agg route and no pcsx permit`. `noRoute: true`, buy leg quoted at the 5 bps floor.
  - SPCX: SPCXon 159.105 against official 158.96 (gap 0.09%), SPCXx tape 149.07, so 673 bps gross and -6.2% to the mark. The cheap leg has no route (Binance xStock `40001`, Pancake `no agg route`, OpenOcean 403, all logged before). On 2026-10-01 the same pair was 45.9 bps and viable.
  - Ratios: TSMon 1.009321, TSMB 1.002110. Ondo ratios earlier in this file were 1.0017 to 1.0041. Raw TSMon token price 477.35 against 472.94 per share once normalized. CBRSB listed `rich` at 176.67 against a 166.43 mark (+6.2%).
- What we changed because of it: nothing. The Refused rows are right as shown. Not verified: whether the 14 to 19% Pancake impact is the real fill cost or a thin-pool quote artifact; with a taker Binance may quote an Ondo sell directly, and that was not run.

### 2026-10-03 — Studio tick latency from a phone (`/api/agent/studio/tick`)

- Session: WEEKEND (Sat, 10:20 UTC).
- What we hit: `GET /api/agent/studio/tick?arb_threshold=0.002&k=<tick key>` twice from Termux, against the deployed app (not the managed runtime).
- What came back: HTTP 200 both times, 5.10 s then 3.97 s total from the phone. Body: `agent streettape-desk`, `mode proposal-only`, session WEEKEND, an SPCX arb row (SPCXon 159.105, SPCXx 149.069). Earlier runs were 1.9 to 2.3 s. The server-side tick line was not in the `/api/_devex` download taken before these calls, so the split between network and fan-out is not known. `bag status` was not run.
- What we changed because of it: nothing.

### 2026-10-03 — Session book: three names had no Friday cash print (`/api/session`)

- Session: WEEKEND (Sat 09:42 ET / 13:42 UTC / 14:42 WAT).
- What we hit: `GET /api/session` and `/session` for the Fri 2026-10-02 16:00 ET close, window open until Mon 09:30 ET.
- What came back (first call of each outcome):
  - 200. 15,499 wrapper snapshots inside the shut window. Every wrapper in the universe printed a tape (`noTapeWrappers: []`).
  - No Friday cash row for AAOI, NBIS and CBRS (all bStocks-only). They have tape (AAOIB 114.60 to 115.22, NBISB 241.57 to 242.23, CBRSB 176.17 to 178.58) but no official, so no gap is computed and no row is invented. No name used the fallback official (`fallbackOfficial: []`).
  - SPCX is held out of the official column by design: SPCXx ranged 149.07 to 159.16 and SPCXon 158.67 to 159.29.
  - Thin xStocks pools (METAx, TSLAx, QQQx, NVDAx, AAPLx, AMDx) printed one flat price all weekend, 0.02% to 21.2% off the print. They are marked thin and excluded from the widest gap. Widest live gaps: TSMon +1.57%, NVDAon +0.64%, QQQon +0.61%.
- What we changed because of it: nothing. Not verified: Monday open mark and reconvergence minutes, which need a Mon 09:30 ET render.

### 2026-10-03 — Free `/x402` route returns a gateway 404 (agent 01M401H27Z9Q80XTFB0ZW8K5HC)

- Session: WEEKEND (Sat, ~21:30 UTC).
- What we hit: `bag deploy --provider bnb` with `payments.seller.price_usd = "0"` (FREE), then `POST` text, `POST` JSON `{"prompt": ...}` and `GET ?prompt=` to `/v1/rt/<agentId>/x402`. The runtime source reads the prompt from exactly those two places.
- What came back (first call of each outcome):
  - Deploy summary: `x402 rail is UNVERIFIED in FREE mode`; `bag deploy info`: `payment rail: unverified — HTTP 404; no verified x402 payment challenge`.
  - All three shapes: HTTP 404 `{"error":{"code":"x402.not_found","message":"x402 endpoint not found."}}` with a `request_id`, 1.6 to 2.8 s.
  - Runtime log after those calls: `serving on 0.0.0.0:9000 (x402: active)` and the 60 s tick lines only. No request line, so the gateway answered and the call never reached the runtime.
  - `B402_SELL_PATHS` in the runtime package is `/x402` and `/mpp`; the stack uses x402. The path and the request shape are correct.
- What we changed because of it: nothing. The free route is not reachable through the gateway as deployed, so no free x402 answer was produced. Not verified: whether the gateway publishes `/x402` only for PAID.

### 2026-10-03 — A2A `tick` skill on the managed runtime, empty agent card

- Session: WEEKEND (Sat, 21:32 UTC).
- What we hit: `bag platform invoke-client new`, `POST /v1/oauth/token` (client_credentials), then `message/send` with `{"skill":"tick"}` twice to `/v1/rt/<agentId>/a2a`.
- What came back (first call of each outcome):
  - Token: 200, 2.30 s.
  - `tick` skill: 200 both times, JSON with the tick fields and the new `answers` block. The runtime's own fetch of the Railway tick took 260 ms and 286 ms. Call wall time from the phone was not captured.
  - Agent card: 200 with `skills: []`. The ERC-8183 rail is off, so an x402-only agent advertises no skills, and `tick` exists only in the executor, not on the card.
  - `bag deploy info --with-curl` prints a `negotiate` example only. A plain text message is rejected with `unknown skill`, so the phrased three answers (LLM path) cannot be asked over A2A without a funded job.
- What we changed because of it: nothing. Not verified: the three phrased answers from the live agent.

### 2026-10-03 — B402 sandbox application blocked (Google Form, external accounts)

- Session: WEEKEND (Sat, ~19:50 to 21:45 UTC).
- What we hit: the application form linked from the B402 docs page, and the Binance page `/en/binancex402` ("Apply for API key") that support pointed to.
- What came back (first call of each outcome):
  - Google Forms: "Can't access item — The organization that owns this item doesn't allow you to access it", for every external Gmail account tried.
  - Support chat: the request was forwarded, answer "a few hours or the next business day (off hours)". The `/binancex402` button leads to the same form; support emailed the team.
  - Result: no sandbox `B402_*` credentials, so `bag x402 sell status` shows all five absent and the route stays FREE. No paid-call receipt exists.
- What we changed because of it: kept `price_usd = "0"` and left `studioRuntime.deployed` and `x402Support` false on the card. Not verified: whether a sandbox application succeeds from an organization-managed account.

### 2026-10-08 — Quote-implied price per share vs tape (`/api/quickbuy/NVDA?usd=10`)

- Session: PRE-MARKET (08:20 ET / 13:20 WAT)
- What we hit: `GET /api/quickbuy/NVDA?usd=10` and Telegram `/buy NVDA 10`, USDT to each wrapper, cash print $237.47.
- What came back: NVDAB tape $234.94/sh (-107 bps vs cash print); the Binance quote implied $235.53/sh (-82 bps). NVDAon tape -101 bps. NVDAon in a later call: tape -91 bps, quote implied $238.83/sh (+57 bps), a 148 bps gap between Ondo's tape and its own quote; NVDAB's gap was 24 bps. NVDAx thin pool, no price. At a 100 bps tape band both priced wrappers were rejected before any quote. Forced guard: with `QUICKBUY_QUOTE_BAND_BPS=1`, NVDAB (-72 bps) and NVDAon (+57 bps) were both rejected with "quote implies $…/sh, … bps from cash print", and no Buy link was returned.
- What we changed because of it: added a second check on the quote's implied price per share (`QUICKBUY_QUOTE_BAND_BPS=300`); raised the tape band to 200 bps (`QUICKBUY_BAND_BPS=200`).

### 2026-10-09 — `underlying-market` `statusInfo.reasonCode` is TRADING with cash shut (`/api/board`)

- Session: PRE-MARKET (00:30 ET / 05:30 WAT, Friday).
- What we hit: `underlying-market` for one wrapper address per underlying (hourly mcap refresh), read back through `GET /api/board`.
- What came back (first call): `statusInfo.reasonCode=TRADING` for all 7 underlyings (SPCX, AMD, NVDA, QQQ, TSLA, AAPL, META), so 21 of 21 addressed wrappers. The NY clock said cash was shut. LIon, which has no address in the catalog, got no code.
- What we changed because of it: the first version of the board row read `TRADING` as "cash open" and printed it at 00:30 ET. Cash open/shut now comes from `rwa.session_now()`. The code is printed as its own fact (`feed TRADING`) and is not used as the state.

### 2026-10-09 — `tokenToShareRatio` is not 1.0 on Ondo or bStocks (`/api/board`)

- Session: PRE-MARKET (00:30 ET / 05:30 WAT).
- What we hit: `underlying-profile` per wrapper address (hourly refresh), read back through `GET /api/board`.
- What came back (first call): Ondo `NVDAon` 1.0017, `QQQon` 1.0041, `AAPLon` 1.0034, `METAon` 1.0028. bStocks `NVDAB` 1.0008, `QQQB` 1.0007, `AAPLB` 1.0006, `METAB` 1.0005. Every xStocks wrapper is exactly 1.0, as are `SPCX*`, `AMD*`, `TSLAB` and `TSLAon`. `LIon` has no ratio. The seed catalog stores `multiplier: 1.0` for bStocks and `None` for Ondo.
- What we changed because of it: nothing in the math, which already divides by the live ratio. The ratio is now printed on each wrapper row (`sh/token`) so a share-normalized gap can be checked by eye.

### 2026-10-09 — Binance route `estimateGasFee` is not a BNB amount (`POST /api/swap/order`, USDT to NVDAB)

- Session: PRE-MARKET (06:42 ET / 11:42 WAT). Buy sheet on `/t/NVDA`, $10 USDT to NVDAB, route Binance Web3 · LiquidMesh, no wallet balance.
- What we hit: the swap sheet's Fees row, filled from the Binance route's `estimateGasFee`.
- What came back (first call): the row read `~250000.000000 BNB gas + $0.01 trade fee`. We read this from the rendered row, not from the raw JSON. Our parser treated any value under 1e6 as already BNB, so the field reached the sheet as 250000. The value matches a gas-unit count, not wei and not BNB. The docs do not give a unit. Price impact on the same quote read under 0.01%.
- What we changed because of it: `_fees` now counts the field as BNB only when it is wei (at least 1e12, divided by 1e18) or already BNB-sized (1 or less). Anything else is dropped, and gas is then filled from the built transaction's `gas` times `gasPrice` when both are present. The sheet also hides any gas figure of 0.1 BNB or more. Not verified: that 250000 is a gas-unit count, since we did not log the raw field.


### 2026-10-10 — Binance refuses Ondo to bStock (wrapper vs wrapper), Rotate falls to Pancake (`POST /api/swap/order`, NVDAon to NVDAB)

- Session: WEEKEND (Sat, 02:17 WAT). Rotate sheet on `/t/NVDA#rotate` in a wallet dApp browser, wallet connected. Repeated from Termux with `curl` at the same size.
- What we hit: `POST /api/swap/order`, NVDAon `0xA9eE28C8...6F75` to NVDAB `0x02fca66c...7436`, `uiAmount` 0.004, `taker` set.
- What came back (first call with a taker): HTTP 200 after 2.49 s for the whole call, Binance attempt plus Pancake fallback. `attempts[0]` is `binance_web3`, `RuntimeError: HTTP 200: Ondo asset on chain 56 can only pair with allowed stablecoin(s); got: 0x02fca66c...7436` (NVDAB). Our error string carries no `code=` here. Then `provider=pancake`, `routes=["PancakeSwap"]`, out 0.00399898 NVDAB, `priceImpactPct` null, an `approval` on NVDAon and a swap `transaction`, `needsWallet: false`. The same call with no taker returned `userWalletAddress is required for RFQ (Ondo) quote` (already recorded). On the sheet the route read `PancakeSwap · PancakeSwap (Binance Web3 quote failed)`, quote ~0.00417624 NVDAB at $230.22/share, price impact and fees `—`.
- What we changed because of it: nothing. A one-quote Ondo to bStock rotate has no Binance route. Only Pancake can do it, so that rotate is signed in the wallet, not through `baw`. Not verified: whether an xStock to Ondo rotate gets the same refusal.

### 2026-10-10 — First settled Rotate: NVDAon to NVDAB through Pancake, approve then swap

- Session: WEEKEND (Sat, 02:14 to 02:17 WAT).
- What we hit: Buy USDT to NVDAon, then the Rotate sheet on `/t/NVDA#rotate`, MAX of NVDAon, MetaMask on BNB Chain.
- What came back (first call): buy USDT 0.9926 to 0.004258 NVDAon confirmed at 02:14, tx `0x9446b7243acbbdd787cb3a60f43bf0ef5c14c34846df73c947d5e726fad2b441`. Rotate needed two wallet signatures, because NVDAon had no allowance for the Pancake router `0x2f68417A...cfF7`. Approve confirmed 02:17, tx `0xabaaafb86c80e01cc716967e585e16ce29e956d94197f2c582ae163a1ba4b76a`, network fee under $0.01. Rotate swap confirmed 02:17, tx `0x160d05c5ea2c347d70b5da4498ac254732f28554ade688008970478d20bbd8b7`, -0.004177 NVDAon, and the wallet then held 0.004176 NVDAB (about $0.96). Sheet quote was ~0.00417624 NVDAB, min received 0.00413448. MetaMask labelled the swap "Contract interaction", not "Swapped". Second Rotate with the fixed sheet, 03:16 WAT, tx `0x7a0b6cdf2d47f382529693522abc4dba6e7dde0927b3907712d2ad02aa0562f9`: no failure message.
- What we changed because of it: the sheet showed "Swap failed on-chain, please try again" after the swap had already confirmed. `/api/swap/execute` answers `status: external` on the Pancake path and the sheet only accepted `success`. The sheet now fails only on a failed status or a reverted receipt, and waits for the approval receipt before the swap. The first-use cost is two signatures; with an existing allowance it is one.

### 2026-10-10 — First signed `baw market-order swap`: 0.0027 BNB to TSLAB, FINISHED (Agentic Wallet)

- Session: WEEKEND (Sat, order booked 10:43:47 WAT). Termux on a phone, `baw` 1.10.0, signed in (next block), wallet funded with BNB only.
- What we hit: `python skills/streettape.py fill 0.0027 --in <native BNB> --out <TSLAB>`, which runs `baw market-order quote`, `/api/swap/order`, then `baw market-order swap`.
- What came back:
  - Quotes: `baw` and the API (`binance_web3`) both 0.005290324264957452 TSLAB, delta 0.00 bps. Native BNB (`0xEeee...EEeE`) was accepted as `--fromToken`.
  - Order `26101000001954698071`, status `FINISHED`, tx `0x3f2e027a3c356783cb39be21b69cb0959d15fed3c2f13dfc09f79b4ba7d2161a`. `toTokenActualQty` 0.005290446631899607 TSLAB, within 1 bps of both quotes (the swap's own quote line said 0.005290130401679719). `bookTime` 10:43:47 and `updatedTime` 10:43:48.
  - Wallet after: 0.00529 TSLAB; BNB 0.007086 to 0.004328.
  - `baw` printed "Confirm in the Binance app", but no confirmation sheet reached the phone holding the wallet. The owner saw only a swap-success toast.
  - Submit and total seconds were not recorded: `fill` crashed after the swap with `<urlopen error [Errno 7] No address associated with hostname>` (phone lost its connection during the `/api/board` session lookup). Status, id and hash above are from `baw market-order list --json`, and this block was written by hand.
- What we changed because of it: `fill` now keeps going if the session lookup fails (label `?`).

### 2026-10-10 — `baw auth signin` with the wallet on a different phone

- Session: WEEKEND (Sat, about 09:40 to 10:40 WAT). `baw` 1.10.0 in Termux on one phone; the Binance account and Agentic Wallet on another phone.
- What we hit: `baw market-order quote` signed out, then `baw auth signin` (plain, `--image`, `--json`), then `baw auth verify`.
- What came back (first call of each):
  - Quote signed out: `code 10003002`, `SESSION_EXPIRED`, `Please log in first.` The skill fell back to the API and printed `baw failed, using API`.
  - `signin`: opened the login page on the same phone, which redirected to that phone's own Binance app, not the other phone where the wallet is. `--image`: the QR image did not render (`Thumbnail not found`), so it could not be screenshotted. `--json`: returned `urlForWeb` and `pairingCode`; opening that link on the phone where the wallet is did not show the auth screen.
  - A QR drawn in Termux with `qrencode` from the same `urlForWeb` and scanned from the screen share did get through to the wallet owner's phone, which then asked me to confirm.
  - `baw auth verify` alone: `required option '--qrCodeId <id>' not specified`. With an id copied by hand some minutes later: `[10002004] QR code does not exist or expired, please try a new code or restart the log in process.` Signin, QR and `verify --qrCodeId` run as one command line: `Authorized, creating wallet...`, then `Login successful! Wallet created`. `baw wallet balance` then showed the funded wallet, BNB 0.007086 ($5.31) on chain 56.
- What we changed because of it: nothing in code.

### 2026-10-10 — `baw market-order quote`: Ondo takes USDT and USDC from $5, refuses $1 and BNB (Agentic Wallet, TSLAon)

- Session: WEEKEND (Sat, cash shut). Telegram bot `/probe`, the linked Agentic Wallet, quote only, nothing signed. BNB $750.82 (from the 0.01 BNB to USDT quote).
- What we hit: USDT, USDC and BNB to TSLAon at $1, $5 and $10; then TSLAon back to the stable at $5 and $10 (sell size is the buy's out-amount, so a failed buy has no sell quote).
- What came back (first call of each):
  - BNB, all three sizes: `103 SERVICE_ERROR Unsupported token pair for Ondo trading, one side must be a supported stablecoin ({0}).` The `{0}` is unfilled, so the allowed stablecoins are not named. A third wording of the BNB refusal (API `40368`; the 2026-10-03 "real-world assets" text).
  - $1 USDT and $1 USDC: `315008 SERVICE_ERROR From token value greater than 5 USD`. $5 and $10 both quoted, so exactly $5 passes despite "greater than". The API-side code for the same floor was `40375`.
  - USDC was accepted: 5 USDC to 0.0128682447 TSLAon. The 2026-09-30 and 2026-10-01 API calls with a taker returned `40368` for USDC. Not explained.
  - Round trip, $5: USDT 5 to 0.0130368233 TSLAon to 4.9799003718 USDT; USDC 5 to 0.0128682447 to 4.8431057821 USDC. $10: USDT 9.9794273689, USDC 9.8603423077.
  - HTTP status and latency were not recorded.
- What we changed because of it: nothing yet.

### 2026-10-10 — `baw market-order quote`: TSLAx has no liquidity from BNB, USDT or USDC (Agentic Wallet)

- Session: WEEKEND (Sat, cash shut). Same `/probe` run, quote only.
- What we hit: BNB, USDT and USDC to TSLAx at $1, $5 and $10 (9 quotes).
- What came back: all 9 returned `100 SERVICE_ERROR No liquidity available, please try again later.` Same code and text for every input and size. The API returned `40370 xStock token only supports trading with: USDT, USDC` for BNB (2026-10-02), so the stablecoin legs are refused here too, with a different message. One ticker only. HTTP status and latency were not recorded.
- What we changed because of it: nothing yet.

### 2026-10-10 — `baw market-order quote`: bStocks buy and sell clean with BNB, USDT, USDC; all six BNB/stable pairs quote (Agentic Wallet, TSLAB)

- Session: WEEKEND (Sat, cash shut). Same `/probe` run, quote only.
- What we hit: BNB, USDT and USDC to TSLAB and back at $1, $5, $10; BNB, USDT and USDC against each other in all six directions at the same sizes.
- What came back: every one of the 18 TSLAB quotes and 18 stable quotes returned OK, none refused at $1. First recorded sell-side bStock quotes. Round trip at $5: TSLAB to USDT 4.9966146835, to USDC 4.9964973831. Stables at $1: USDT to USDC 0.9992345855, USDC to USDT 1.0007797004. HTTP status and latency were not recorded.
- What we changed because of it: nothing yet.
