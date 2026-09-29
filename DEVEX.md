# DEVEX.md — StreetTape build log

One entry per official-API call made while building/testing against real endpoints. Not curated after the fact.

Entries under **Runtime log** append automatically (see `devlog.py`). Everything above it is written by hand, and each item says whether it came from a live response, from docs, or from code.

## Summary

- Deployed on Railway, live at `streettape.up.railway.app`, BNB Chain (56).
- Built against: Binance Web3 quote/RFQ, RWA Data, Transaction sim, Address Portfolio; GeckoTerminal (tape); Yahoo (last cash print); BSC RPC.
- Proposal-only by design: the app quotes, flags and alerts; the user signs in their own wallet.
- Verified against live responses (see below): Binance Web3 quotes and built swap transactions, RFQ wallet requirement, RWA Data (`platforms`, `search`, `underlying-profile`, `underlying-market`), rate limiting, ERC-8004 registration on BSC.
- **Called live, unusable result:** Transaction sim returned an empty payload (see 2026-09-29 with-wallet session).
- **Not verified live:** Address Portfolio, `baw` signing, an on-chain swap completing. Marked pending below.

## Live observations (2026-09-29, after-hours session)

Source: one real response from the deployed `/api/agent/studio/tick` (4 arb candidates, 8 leg quotes at $50 size).

1. **RFQ quotes need a wallet.** Sell legs on Ondo and xStock wrappers (SPCXon, TSLAx) failed with `userWalletAddress is required for RFQ (Ondo)` / `(xStock)`. Without a taker address there is no RFQ price at all, so the app falls back to a PancakeSwap deep link with no price impact. Fix on our side: mark these legs as fallback and charge a 5 bps cost floor. Costs us accuracy in the no-wallet case.
2. **AMM quotes work without a wallet.** Binance Web3 legs (provider `binance_web3`, routed via LiquidMesh) return `uiOutAmount`, `priceImpactPct` and `needsWallet: true`, but `transaction: null` until a taker is supplied. Raw `priceImpactPct` values seen: -0.000463 (SPCX buy), 0.000230 (TSLA buy), 0.002520 (META sell). The sign varies between quotes, so we use the absolute value. **Unit unverified:** `agent.py` comments it as a fraction (0.004 = 0.4%) but converts with `abs(x) * 100`, which would be percent-to-bps, not fraction-to-bps (that needs `* 10000`). If the API really returns a fraction, our impact cost is understated 100x; the 5 bps floor per leg is what keeps the net-bps figures plausible. I have not confirmed the unit from Binance docs or a large-size quote.
4. **`sim` is `null` on every leg.** Expected, since the Transaction sim needs a built tx and there is no taker in an unauthenticated scan. So this scan did not exercise the Transaction API at all.
5. **Real gaps exist after hours.** Cross-wrapper gaps of 1.1% to 3.5% on the same underlying (SPCX 3.5%, TSLA 3.1%, META 1.8%, NVDA 1.1%). After-hours wrappers stop tracking each other, which is the whole thesis of the app.

## Live observations (2026-09-29, RWA Data + wallet session)

Source: real responses from the deployed app, hit from a browser. The wallet in these calls is my own test wallet.

### RWA Data: region block, then parameter errors

1. **Compliance block by server region.** With valid keys set, all four calls (`platforms`, `search`, `underlying-profile`, `underlying-market`) returned `Service not available due to compliance restriction` from the deployed app's default Railway region. Keys were fine (an auth failure would be a 401), so the block is on the calling IP's region. The docs page I read did not tell me which regions are excluded. After I switched the Railway service to Singapore and redeployed, `platforms` (2 items) and `search` (1 item) succeeded on the next call. Cost: one failed deploy cycle. A supported-regions line in the dev-portal docs would have avoided it.
2. **`underlying-profile` parameter names were not guessable.** My first call passed the ticker. The API answered `Parameter tokenContractAddress is required`.
3. **`underlying-market` returned a different missing-parameter error.** `Parameter binanceChainId is required`. Same endpoint family, but the error names only one missing field at a time, so it took a round trip per endpoint. Fix: send `tokenContractAddress` plus `binanceChainId=56` to both.
4. **After the fix all four return data.** `platforms` 2 items, `search` 1, `underlying-profile` 9, `underlying-market` 6 (NVDAB, `0x02fca66c1d1afb4e2a7884261eb00f63598a7436`, BSC). I have not yet checked the field-level contents against `board.py`'s expectations.

### RWA Data: what `underlying-profile` and `underlying-market` actually return (NVDAB)

10. **Profile fields:** `platformId` (`bstock`), `underlyingTicker`, `underlyingFullName`, `assetType`, `tokenToShareRatio` (`1.000778223752807865`), `protections.collateralReport` (`supported: true`, but `description` and `url` both `null`), and `companyInfo` (CEO, website, industry, English and Chinese descriptions, empty `conceptsEn`/`conceptsCn`).
11. **`tokenToShareRatio` is not 1.0.** Our seed catalog carries `multiplier: 1.0` for bStocks. The live ratio for NVDAB is 1.00078, so a price normalized with the seed value is about 0.08% off. Not yet wired into the arb math.
12. **Market data is the underlying's, not the token's.** `marketData.marketCap` is `5526282420000.00` (NVIDIA the company, about $5.5T). `totalShares` is `null`, so a per-token cap cannot be derived. Also present: `high52W`, `low52W`, `volumeShares24H`, `dividendYield`, `latestDividend`, `peRatioTTM`, `pbRatio`. Returned `null`: `referencePrice`, `avgDailyVolume1Y`, `turnoverRate`, `amplitude`. Numbers come back as strings. The app now shows this as "Underlying mcap", refreshed hourly.
13. **Session state is in the response.** `statusInfo` has `openState`, `reasonCode` (`TRADING`), `marketStatus`, `nextOpenTime`, `nextCloseTime`. In this call `nextOpenTime`/`nextCloseTime` were `null`, so we still compute sessions ourselves in `rwa.session_now()`.

14. **`underlying-market` rate-limits short bursts.** Seven back-to-back calls (one per underlying) returned data for NVDA, TSLA, AAPL, META, AMD and `Rate limit exceeded` for QQQ and SPCX, the last two. I could not find a published rate limit for RWA Data (the Binance limits pages I found cover the Spot API only, not `web3.binance.com`). With a 2 s gap between calls and backoff retries, all seven returned: NVDA about $5.53T, TSLA $1.41T, AAPL $4.94T, META $1.82T, AMD $0.99T, SPCX $1.92T, QQQ $494B (an ETF; the response does not say what its `marketCap` represents). The 2 s gap is a guess, since the limit is undocumented.
15. **`totalShares` depends on which wrapper you query.** The NVDAB (bStocks) call returned `totalShares: null`. The same underlying queried through its xStocks address returned `24147000000`. Same company, different payload by wrapper, so a client cannot rely on any one wrapper for a field.

### Trading API: quote and swap build with a taker

5. **Quote without a wallet works.** BNB to NVDAB, 0.005 BNB: provider `binance_web3`, route `LiquidMesh`, `uiOutAmount` about 0.01654, rate about 3.31 NVDAB per BNB, `transaction: null`, `needsWallet: true`.
6. **Quote with a taker builds a real transaction.** Same pair with my wallet as taker returned `transaction` with `from`, `to` (`0xB44446b0c8E56988c34f7Ff73Ae904982b5FdDA5`), `data`, `value` (`0x11c37937e08000`, 0.005 BNB), `gas` (`0x6ddd0`) and `gasPrice`, routed via LiquidMesh. `uiOutAmount` about 0.01651, `uiMinReceived` about 0.01634.
7. **`priceImpactPct` again ambiguous.** Values seen at this roughly $3.8 size: 0.0021223299 and 0.001674117 (positive this time). Still cannot tell fraction from percent from a size this small. Unit remains unverified (see observation 2 in the after-hours session).
8. **Transaction sim returns an empty payload.** With a real taker and a built transaction, `POST /api/v1/dex/aggregator/tx/simulate` returned a success envelope with no `data`. So the endpoint path may be wrong, or it needs different inputs, or it returns nothing on success. The docs I had did not include an example response, so I cannot say which. The app now treats an empty simulate result as `ok: null` and does not block the swap.

### Wallet-side result

9. **Wallet flagged the built transaction "likely to fail".** At 03:16 WAT (after US close) the wallet's confirmation sheet showed `This transaction is likely to fail` for the Binance-built swap (0.0065 BNB into NVDAB). I cancelled and retried once at 03:20 and it did not go through. **Cause not confirmed.** My working guess is off-hours liquidity or transfer limits on the tokenized stock, since the app itself showed AFTER-HOURS at the time, but I have not reproduced it during market hours and have not decoded the revert. No on-chain swap has completed yet.

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

## Transaction API sim: called live, empty result

`swap.simulate_transaction()` fires on every SWAP-mode `quote()` that has a built tx (see `swap.py`), logged as `what="tx sim"`. The live scan without a taker had `sim: null`. With a taker (2026-09-29 session, observation 8) the call went out and came back with an empty `data`. The endpoint path (`POST /api/v1/dex/aggregator/tx/simulate`) is still my best reading of the Trading API family and remains **unverified**: I cannot tell a wrong path from a valid empty response. Real entries for this call are in the runtime log below.

## Pending: first Address Portfolio lookup

`portfolio.get_token_holdings()` calls `GET /api/v1/portfolio/tokens` on `https://web3.binance.com/wallet` and is logged automatically. Path and response shape are unverified against a live call. First real entry replaces this placeholder.

## Runtime log (auto-appended)

