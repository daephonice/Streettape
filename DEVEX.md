# DEVEX.md — StreetTape build log

One entry per official-API call made while building/testing against real endpoints. Not curated after the fact.

Entries below append automatically at runtime (see `devlog.py`) — run the app against real endpoints (Gecko tape, Yahoo mark, Binance Web3 quote/RFQ/submit, BSC RPC via `balances.py`) and this file fills in. Submit this file as-is via the Google form once you've exercised: first `rwa/price`, first Transaction sim, RFQ vs AMM on the same NVDA, off-hours liquidity, and at least one error case.

## Pending: first Transaction API sim
`swap.simulate_transaction()` now fires automatically on every SWAP-mode
`quote()` call (see `swap.py`), logged under `what="tx sim"`. No entry below
yet because this sandbox has no network access to actually hit
`POST /api/v1/dex/aggregator/tx/simulate` — that path is our best-guess
continuation of the same Binance Web3 Trading API family used by
quote/swap, unverified against a live response. Run a real $5 NVDAx quote
with a connected taker address once network access is available; the
`devlog.log_call(what="tx sim", ...)` entry it produces (success or error)
will append below with the exact response/error body — do not hand-write
one in its place.

## Pending: first Address Portfolio lookup
`portfolio.get_token_holdings()` (used by `balances.py` ahead of the public
RPC `balanceOf` loop) calls `GET /api/v1/portfolio/tokens` on
`https://web3.binance.com/wallet` via `swap._request`, so its result is
logged automatically as `Binance Web3 GET /api/v1/portfolio/tokens`. Path
and response shape (`tokens`/`list`, `contractAddress`/`tokenAddress`,
`balance`/`amount`, `decimals`) are our best-guess reading of the same
Wallet/Address Portfolio product line — unverified against a live call.
Connect a real wallet with holdings once network access is available; the
first real entry replaces this placeholder.
