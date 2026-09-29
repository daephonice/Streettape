# DEVEX.md — StreetTape build log

One entry per official-API call made while building/testing against real endpoints. Not curated after the fact.

Entries under **Runtime log** append automatically (see `devlog.py`). Everything above it is written by hand, and each item says whether it came from a live response, from docs, or from code.

## Summary

- Deployed on Railway, live at `streettape.up.railway.app`, BNB Chain (56).
- Built against: Binance Web3 quote/RFQ, RWA Data, Transaction sim, Address Portfolio; GeckoTerminal (tape); Yahoo (last cash print); BSC RPC.
- Proposal-only by design: the app quotes, flags and alerts; the user signs in their own wallet.
- Verified against live responses (see below): Binance Web3 quotes and built swap transactions, RFQ wallet requirement, RWA Data (`platforms`, `search`, `underlying-profile`, `underlying-market`), rate limiting, ERC-8004 registration on BSC.
- **Called live, failed:** `POST /api/v1/dex/aggregator/tx/simulate` returned HTTP 404 (path not valid); `GET /api/v1/portfolio/tokens` returned HTTP 202 with a non-JSON body on 19 of 19 calls; `rwa/price` returned no usable result on 420 of 420 calls (our missing parameter, plus rate limiting). See the runtime findings below.
- **Not verified live:** `baw` signing. A TSLAB buy did settle on-chain (see wallet-side result).

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
8. **Transaction sim endpoint returns HTTP 404.** With a real taker and a built transaction, `POST /api/v1/dex/aggregator/tx/simulate` answered `404` in about 100 ms (twice: 10:46:16 and 10:48:20 UTC). Earlier I recorded this as "success envelope with empty data"; that was wrong, our logger treated a 404 with no `code` field as success. The path is not valid, and I could not find the correct one in the docs I searched. The 404 body was not captured. The app now treats HTTP >= 400 as a failure and stops calling simulate for an hour after a 404.
9. **Wallet flagged the built transaction "likely to fail".** At 03:16 WAT (after US close) the wallet's confirmation sheet showed `This transaction is likely to fail` for the Binance-built swap (0.0065 BNB into NVDAB). I cancelled and retried once at 03:20 and it did not go through. **Cause not confirmed.** My working guess is off-hours liquidity or transfer limits on the tokenized stock, since the app itself showed AFTER-HOURS at the time, but I have not reproduced it during market hours and have not decoded the revert. A later buy of TSLAB did complete: `/api/balances/<wallet>` afterwards showed 0.010644 TSLAB and 0.004154 BNB in the test wallet. The Binance quote for that trade returned `binance_web3` via LiquidMesh (see the runtime findings).

### What the runtime log showed (978 entries, 10:35 to 10:53 UTC, 367 KB before condensing)

16. **`rwa/price` never worked: 0 of 420 calls.** 213 came back HTTP 200 with `code=40001 msg=Parameter binanceChainId is required` (our request left the parameter out), and 207 came back HTTP 429 `code=42900 Rate limit exceeded`. The official mark therefore never came from RWA Data; Gecko plus Yahoo carried the app. The missing parameter was our bug. The API-side oddity: a validation failure returns HTTP 200 with an error code, while a rate limit returns a real HTTP 429. Now sending `binanceChainId=56` (and `tokenContractAddress` alongside `tokenAddress`, since the sibling endpoints use that name). Not yet confirmed working.
17. **Rate limiting is a short-burst limit, undocumented.** At startup the first five RWA Data calls passed. The next eight, spaced about 80 ms apart, were all rejected with 429. The ninth, 84 ms after the last rejection, passed again. So the window recovers almost immediately, and a client only needs to pace calls, not back off for long. The app now waits 0.3 s between price calls and retries a 429 once after 1 s.
18. **Quote endpoint outcomes (32 calls).** 13 succeeded, 18 failed with `code=40001 userWalletAddress is required for RFQ (Ondo) quote` (HTTP 200 with an error code), and 1 was HTTP 429 `code=42900`. Every failure fell back to a PancakeSwap deep link. Latency was 89 to 203 ms, except the first two RFQ-without-wallet errors at 2.4 s each.
19. **Swap build works and is fast.** `GET /api/v1/dex/aggregator/swap` returned in about 98 ms twice: BNB to TSLAB (rate about 2.129, `priceImpactPct` 0.003006) and TSLAB to BNB (rate about 0.470, `priceImpactPct` 0.001542), both routed via LiquidMesh. The `priceImpactPct` unit is still unconfirmed.
20. **Our own labelling bug: balance reads were logged as "tx sim".** 323 `eth_call` BEP-20 `balanceOf` reads were labelled `tx sim (eth_call)`. They were RPC balance reads, not simulations. The label is now `rpc eth_call`. The only real Transaction API simulate calls are the two 404s above.
21. **The log itself was mostly noise.** 978 entries in about 18 minutes, dominated by the 45 s price and mark polling loops and the per-balance RPC reads. The logger now keeps the first three of each distinct call and outcome and reports totals in a summary table.

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

## Transaction API sim: called live, HTTP 404

`swap.simulate_transaction()` fires on every SWAP-mode `quote()` that has a built tx. It called `POST /api/v1/dex/aggregator/tx/simulate` twice with a real taker and got HTTP 404 both times. The path was my best reading of the Trading API family and is **wrong or not exposed to this key**. Not fixed: I have no documented replacement path. The wallet's own simulation (the "likely to fail" warning) is the only pre-sign check in use.

## Address Portfolio: called live, no usable response

`portfolio.get_token_holdings()` calls `GET https://web3.binance.com/wallet/api/v1/portfolio/tokens?chainId=56&address=<wallet>`, signed like the other calls. All 19 calls returned **HTTP 202 with a non-JSON body** in 10 to 40 ms (median 14 ms), so no holdings ever came back from it. Balances shown in the app came from the public BSC RPC fallback. I don't know whether 202 means "accepted, retry later", a gateway rejection, or a wrong path. The wallet-side `query-address-info` skill documents `offset` as a required parameter on its own endpoint; we did not send it, and the app now sends `offset=0` as an experiment. The app also stops calling for 10 minutes after a failure.


## Runtime log (auto-appended, condensed)

The first two occurrences of each distinct call and outcome are kept, in time order. Totals for every call, including the repeats dropped here, are in the table at the end.

## Call summary (runtime log, 10:35 to 10:53 UTC)

| Calls | Label | Status | Outcome |
|---|---|---|---|
| 323 | tx sim (eth_call) | 200 | result=# |
| 213 | rwa official /api/v1/dex/market/rwa/price | 200 | code=# msg=Parameter binanceChainId is required |
| 207 | rwa official /api/v1/dex/market/rwa/price | 429 | code=# msg=Rate limit exceeded |
| 21 | mark fetch NVDA | 200 | price=#.# |
| 21 | mark fetch AMD | 200 | price=#.# |
| 21 | mark fetch TSLA | 200 | price=#.# |
| 21 | mark fetch META | 200 | price=#.# |
| 21 | mark fetch QQQ | 200 | price=#.# |
| 21 | mark fetch AAPL | 200 | price=#.# |
| 19 | Binance Web3 GET /api/v1/portfolio/tokens | 202 | non-JSON response |
| 19 | rpc eth_getBalance | 200 | result=# |
| 18 | Binance Web3 GET /api/v1/dex/aggregator/quote | 200 | code=# msg=userWalletAddress is required for RFQ (Ondo) quote |
| 18 | quote fallback -> pancake | n/a | fell back: RuntimeError: userWalletAddress is required for RFQ (Ondo) quote |
| 13 | Binance Web3 GET /api/v1/dex/aggregator/quote | 200 | ok |
| 7 | rwa official /api/v1/dex/market/rwa/underlying-market | 200 | ok |
| 2 | Binance Web3 GET /api/v1/dex/aggregator/swap | 200 | ok |
| 2 | Binance Web3 POST /api/v1/dex/aggregator/tx/simulate | 404 | ok |
| 2 | tx sim | n/a | code # but empty data payload |
| 1 | rwa official /api/v1/dex/market/rwa/platforms | 200 | ok |
| 1 | rwa official /api/v1/dex/market/rwa/tokens | 200 | ok |
| 1 | rwa official /api/v1/dex/market/rwa/search | 200 | ok |
| 1 | rwa official /api/v1/dex/market/rwa/underlying-profile | 429 | code=# msg=Rate limit exceeded |
| 1 | rwa official /api/v1/dex/market/rwa/underlying-market | 429 | code=# msg=Rate limit exceeded |
| 1 | Binance Web3 GET /api/v1/dex/aggregator/quote | 429 | code=# msg=Rate limit exceeded |
| 1 | quote fallback -> pancake | n/a | fell back: RuntimeError: Rate limit exceeded |
| 1 | quote mode=SWAP 0xEeeeeE->0x5b1910 | n/a | rate=#.# priceImpactPct=#.# vendor=['LiquidMesh'] |
| 1 | quote mode=SWAP 0x5b1910->0xEeeeeE | n/a | rate=#.# priceImpactPct=#.# vendor=['LiquidMesh'] |

Rows that show status `404` with outcome `ok`, and the `tx sim` rows saying "empty data payload", come from the old logger treating an HTTP 404 as success. Rows labelled `tx sim (eth_call)` are BSC RPC `balanceOf` reads, mislabelled at the time.

## 2026-09-29T10:35:08+00:00 — rwa official /api/v1/dex/market/rwa/platforms
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/platforms`
- Status: `200`  Latency: `115ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok
- Time-to-first-call from process start: `5.3s`

## 2026-09-29T10:35:08+00:00 — rwa official /api/v1/dex/market/rwa/tokens
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/tokens`
- Status: `200`  Latency: `286ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/search
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/search?keyword=NVDA`
- Status: `200`  Latency: `329ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0xc845b2894dBddd03858fd2D643B4eF725fE0849d`
- Status: `200`  Latency: `203ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=Parameter binanceChainId is required
- Body: `{'code': 40001, 'msg': 'Parameter binanceChainId is required', 'data': None, 'timestamp': 1790678109093, 'success': False}`

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/underlying-market
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-market?tokenContractAddress=0xc845b2894dBddd03858fd2D643B4eF725fE0849d&binanceChainId=56`
- Status: `200`  Latency: `207ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/underlying-profile
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-profile?tokenContractAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436&binanceChainId=56`
- Status: `429`  Latency: `82ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'timestamp': 1790678109170, 'msg': 'Rate limit exceeded', 'data': '', 'code': 42900}`

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0xA9eE28C80f960B889dFbd1902055218cBa016F75`
- Status: `429`  Latency: `81ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'msg': 'Rate limit exceeded', 'data': '', 'code': 42900, 'timestamp': 1790678109176}`

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/underlying-market
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-market?tokenContractAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436&binanceChainId=56`
- Status: `429`  Latency: `84ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'code': 42900, 'timestamp': 1790678109254, 'msg': 'Rate limit exceeded', 'data': ''}`

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0x02fca66c1d1afb4e2a7884261eb00f63598a7436`
- Status: `429`  Latency: `81ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'code': 42900, 'timestamp': 1790678109257, 'msg': 'Rate limit exceeded', 'data': ''}`

## 2026-09-29T10:35:09+00:00 — rwa official /api/v1/dex/market/rwa/price
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/price?tokenAddress=0x96702be57Cd9777f835117a809C7124fe4ec989A`
- Status: `200`  Latency: `83ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=Parameter binanceChainId is required
- Body: `{'code': 40001, 'msg': 'Parameter binanceChainId is required', 'data': None, 'timestamp': 1790678109751, 'success': False}`

## 2026-09-29T10:35:11+00:00 — rwa official /api/v1/dex/market/rwa/underlying-market
- URL: `https://web3.binance.com/build/api/v1/dex/market/rwa/underlying-market?tokenContractAddress=0x8aD3c73F833d3F9A523aB01476625F269aEB7Cf0&binanceChainId=56`
- Status: `200`  Latency: `94ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:35:17+00:00 — mark fetch NVDA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/NVDA`
- Status: `200`  Latency: `26ms`
- Docs said: regularMarketPrice present
- Actually happened: price=228.86

## 2026-09-29T10:35:17+00:00 — mark fetch AMD
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AMD`
- Status: `200`  Latency: `31ms`
- Docs said: regularMarketPrice present
- Actually happened: price=607.87

## 2026-09-29T10:35:17+00:00 — mark fetch TSLA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/TSLA`
- Status: `200`  Latency: `30ms`
- Docs said: regularMarketPrice present
- Actually happened: price=357.45

## 2026-09-29T10:35:17+00:00 — mark fetch META
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/META`
- Status: `200`  Latency: `33ms`
- Docs said: regularMarketPrice present
- Actually happened: price=715.62

## 2026-09-29T10:35:17+00:00 — mark fetch QQQ
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/QQQ`
- Status: `200`  Latency: `52ms`
- Docs said: regularMarketPrice present
- Actually happened: price=736.53

## 2026-09-29T10:35:17+00:00 — mark fetch AAPL
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AAPL`
- Status: `200`  Latency: `230ms`
- Docs said: regularMarketPrice present
- Actually happened: price=338.4

## 2026-09-29T10:36:12+00:00 — mark fetch META
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/META`
- Status: `200`  Latency: `166ms`
- Docs said: regularMarketPrice present
- Actually happened: price=715.62

## 2026-09-29T10:36:12+00:00 — mark fetch AMD
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AMD`
- Status: `200`  Latency: `168ms`
- Docs said: regularMarketPrice present
- Actually happened: price=607.87

## 2026-09-29T10:36:12+00:00 — mark fetch TSLA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/TSLA`
- Status: `200`  Latency: `171ms`
- Docs said: regularMarketPrice present
- Actually happened: price=357.45

## 2026-09-29T10:36:12+00:00 — mark fetch NVDA
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/NVDA`
- Status: `200`  Latency: `179ms`
- Docs said: regularMarketPrice present
- Actually happened: price=228.86

## 2026-09-29T10:36:12+00:00 — mark fetch QQQ
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/QQQ`
- Status: `200`  Latency: `180ms`
- Docs said: regularMarketPrice present
- Actually happened: price=736.53

## 2026-09-29T10:36:12+00:00 — mark fetch AAPL
- URL: `https://query2.finance.yahoo.com/v8/finance/chart/AAPL`
- Status: `200`  Latency: `199ms`
- Docs said: regularMarketPrice present
- Actually happened: price=338.4

## 2026-09-29T10:42:12+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=118053262639304848&fromTokenAddress=0x2494b603319d4D9F9715c9f4496d9E0364B59d93&toTokenAddress=0x55d398326f99059fF775485246999027B3197955`
- Status: `200`  Latency: `2367ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=userWalletAddress is required for RFQ (Ondo) quote
- Body: `{'code': 40001, 'msg': 'userWalletAddress is required for RFQ (Ondo) quote', 'data': None, 'timestamp': 1790678530526, 'success': False}`

## 2026-09-29T10:42:12+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: userWalletAddress is required for RFQ (Ondo) quote

## 2026-09-29T10:42:12+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=23610652527860968&fromTokenAddress=0x2494b603319d4D9F9715c9f4496d9E0364B59d93&toTokenAddress=0x55d398326f99059fF775485246999027B3197955`
- Status: `200`  Latency: `2452ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=40001 msg=userWalletAddress is required for RFQ (Ondo) quote
- Body: `{'code': 40001, 'msg': 'userWalletAddress is required for RFQ (Ondo) quote', 'data': None, 'timestamp': 1790678532902, 'success': False}`

## 2026-09-29T10:42:12+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: userWalletAddress is required for RFQ (Ondo) quote

## 2026-09-29T10:42:12+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=50000000000000000000&fromTokenAddress=0x55d398326f99059fF775485246999027B3197955&toTokenAddress=0x5b1910eaad6450e50f816082aa078c41f10c292f`
- Status: `200`  Latency: `112ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:43:32+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=50000000000000000000&fromTokenAddress=0x55d398326f99059fF775485246999027B3197955&toTokenAddress=0x5b1910eaad6450e50f816082aa078c41f10c292f`
- Status: `200`  Latency: `105ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:43:32+00:00 — Binance Web3 GET /api/v1/dex/aggregator/quote
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote?binanceChainId=56&amount=50000000000000000000&fromTokenAddress=0x55d398326f99059fF775485246999027B3197955&toTokenAddress=0xD7dF5863A3e742F0c767768cDfcb63f09E0422f6`
- Status: `429`  Latency: `88ms`
- Docs said: code 0 / success true with data payload
- Actually happened: code=42900 msg=Rate limit exceeded
- Body: `{'msg': 'Rate limit exceeded', 'data': '', 'code': 42900, 'timestamp': 1790678612792}`

## 2026-09-29T10:43:32+00:00 — quote fallback -> pancake
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/quote`
- Status: `n/a`  Latency: `0ms`
- Docs said: Binance quote route
- Actually happened: fell back: RuntimeError: Rate limit exceeded

## 2026-09-29T10:45:08+00:00 — Binance Web3 GET /api/v1/portfolio/tokens
- URL: `https://web3.binance.com/wallet/api/v1/portfolio/tokens?chainId=56&address=0x4b957dcf5d914e635cbed97b0b55943c653e4331`
- Status: `202`  Latency: `14ms`
- Docs said: JSON body
- Actually happened: non-JSON response

## 2026-09-29T10:45:09+00:00 — rpc eth_getBalance
- URL: `https://bsc-mainnet.nodereal.io/v1/af0c96d75dc744049832a80daa8469e8`
- Status: `200`  Latency: `986ms`
- Docs said: result
- Actually happened: result=0x20946154315a00

## 2026-09-29T10:45:09+00:00 — tx sim (eth_call)
- URL: `https://bsc-mainnet.nodereal.io/v1/af0c96d75dc744049832a80daa8469e8`
- Status: `200`  Latency: `75ms`
- Docs said: result
- Actually happened: result=0x0000000000000000000000000000000000000000000000000000000000000000

## 2026-09-29T10:45:09+00:00 — tx sim (eth_call)
- URL: `https://bsc-mainnet.nodereal.io/v1/af0c96d75dc744049832a80daa8469e8`
- Status: `200`  Latency: `584ms`
- Docs said: result
- Actually happened: result=0x0000000000000000000000000000000000000000000000000000000000000000

## 2026-09-29T10:45:34+00:00 — Binance Web3 GET /api/v1/portfolio/tokens
- URL: `https://web3.binance.com/wallet/api/v1/portfolio/tokens?chainId=56&address=0x4b957dcf5d914e635cbed97b0b55943c653e4331`
- Status: `202`  Latency: `13ms`
- Docs said: JSON body
- Actually happened: non-JSON response

## 2026-09-29T10:45:34+00:00 — rpc eth_getBalance
- URL: `https://bsc-mainnet.nodereal.io/v1/af0c96d75dc744049832a80daa8469e8`
- Status: `200`  Latency: `291ms`
- Docs said: result
- Actually happened: result=0x20946154315a00

## 2026-09-29T10:46:15+00:00 — Binance Web3 GET /api/v1/dex/aggregator/swap
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/swap?binanceChainId=56&amount=5000000000000000&fromTokenAddress=0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE&toTokenAddress=0x5b1910eaad6450e50f816082aa078c41f10c292f&userWalletAddress=0x4b957dcf5d914e635cbed97b0b55943c653e4331&quoteId=9ca3cb8e57894980b236470cec6d57bc&slippagePercent=1`
- Status: `200`  Latency: `99ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:46:15+00:00 — quote mode=SWAP 0xEeeeeE->0x5b1910
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/swap`
- Status: `n/a`  Latency: `0ms`
- Actually happened: rate=2.1288724746018195 priceImpactPct=0.003005687 vendor=['LiquidMesh']

## 2026-09-29T10:46:16+00:00 — Binance Web3 POST /api/v1/dex/aggregator/tx/simulate
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/tx/simulate`
- Status: `404`  Latency: `98ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:46:16+00:00 — tx sim
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/tx/simulate`
- Status: `n/a`  Latency: `99ms`
- Docs said: 200 with { success, gasUsed } or similar
- Actually happened: code 0 but empty data payload
- Body: `None`

## 2026-09-29T10:48:20+00:00 — Binance Web3 GET /api/v1/dex/aggregator/swap
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/swap?binanceChainId=56&amount=2661090000000000&fromTokenAddress=0x5b1910eaad6450e50f816082aa078c41f10c292f&toTokenAddress=0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE&userWalletAddress=0x4b957dcf5d914e635cbed97b0b55943c653e4331&quoteId=5ee5bb667d024b0c8bc834fba7da3099&slippagePercent=1`
- Status: `200`  Latency: `97ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:48:20+00:00 — quote mode=SWAP 0x5b1910->0xEeeeeE
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/swap`
- Status: `n/a`  Latency: `0ms`
- Actually happened: rate=0.46998555309963025 priceImpactPct=0.0015417639 vendor=['LiquidMesh']

## 2026-09-29T10:48:20+00:00 — Binance Web3 POST /api/v1/dex/aggregator/tx/simulate
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/tx/simulate`
- Status: `404`  Latency: `113ms`
- Docs said: code 0 / success true with data payload
- Actually happened: ok

## 2026-09-29T10:48:20+00:00 — tx sim
- URL: `https://web3.binance.com/build/api/v1/dex/aggregator/tx/simulate`
- Status: `n/a`  Latency: `113ms`
- Docs said: 200 with { success, gasUsed } or similar
- Actually happened: code 0 but empty data payload
- Body: `None`
