# StreetTape

StreetTape is a trading desk for tokenized US stocks on BNB Chain. The same underlying (NVDA, TSLA, AAPL, and others) exists as three wrappers — bStocks, Ondo, and xStocks — that do not always trade at the same price, especially while the US cash market is closed. StreetTape shows the three prices, finds the gap, and lets you rotate from the rich wrapper to the cheap one in one signature.

Live: https://streettape.up.railway.app  
Agent card: https://streettape.up.railway.app/agent-registration.json (ERC-8004 agent 360062)  
Report: DEVEX.md (not `GET /api/_devex`)

Spot only. BSC mainnet only. Nothing auto-executes. The user signs.

## What it is

A mark-vs-tape desk. Tape is the on-chain pool price (GeckoTerminal). Official is the last cash print (Yahoo, or the RWA mark when Binance returns one). Fair is a synthetic reference used only while cash is shut: the last print moved by QQQ and news. Fair explains why a wrapper looks rich; the trade itself stays wrapper versus wrapper.

The product is the comparison, the one-signature Rotate, the baskets, the floor watch, and the Telegram bot that uses the same data.

## What it does

- **Board.** Every underlying shows its wrappers with Tape, Official, and Fair (cash-shut only), the premium or discount, the share ratio, and a thin-pool flag when liquidity is under $5,000. Session chips show whether cash is open or shut, with Lagos and New York times.
- **Rotate.** A third sheet, not Sell then Buy. Heading is `ROTATE {rich} to {cheap}`. Size is 25 / 50 / 75 / MAX of the rich wrapper you hold. One quote, rich mint to cheap mint, one signature once the wrapper has an allowance (first use adds an approve). BNB against Ondo stays blocked on the site.
- **Baskets.** AI (NVDA, AMD, META), SEMIS (NVDA, AMD, TSLA), DEFENSIVE (QQQ, AAPL, META). Equal weight, cheapest wrapper that has a tape and a share ratio. Unfilled weight is never silently reassigned.
- **Floor watch.** Proposal only. Given a wallet and a floor percentage, it reads the wrappers you already hold, computes how far the per-share tape can fall before it touches the floor (anchored to the official mark), and proposes a sell into USDT of the richest held wrapper when the room is under a 2% buffer. Nothing is signed until you confirm.
- **Quick buy.** Rejects a wrapper whose tape or quote-implied per-share price is outside a band versus the cash print.
- **Alerts.** Telegram alerts when cash is shut and a premium exceeds the threshold. Earnings stand-down note when a name reports inside 24 hours.
- **Agent.** ERC-8004 identity 360062. `GET /api/agent/studio/tick` returns the same scan as the internal loop (proposal-only, token-gated). Closed-grammar skill that accepts only fixed sentences and never invents a price.

## How it works

Prices come from three free sources. Tape is GeckoTerminal. Official is Yahoo (fallback to the last stored print). RWA Data supplies platforms, search, underlying-profile (share ratio), and underlying-market when the call succeeds. Quotes and swap builds go through the Binance Web3 Trading API (LiquidMesh for SWAP, RFQ when a wallet is required). When Binance refuses a pair, the site falls back to PancakeSwap calldata. Holdings come from public BSC RPC because Address Portfolio returns non-JSON.

Routing rules live in `routing_rules.py` and are served at `GET /api/routing-rules`.

- Site: xStocks go straight to Pancake. Ondo and BNB/USDC/USDT swaps go to Pancake under $5 and to the Binance route from $5 (Pancake only on error). bStocks use the Binance route. Rotate is Pancake, no minimum. BNB against Ondo is blocked.
- Telegram bot (baw, Binance route only): xStocks are locked. Ondo has a $5 minimum with USDC or USDT. bStocks have no minimum. Rotate and any pair the Binance route cannot do in one swap (BNB ↔ Ondo, wrapper ↔ wrapper) run as two swaps via USDT with a $6 minimum so the first leg never lands under the $5 floor. The user taps once; leg 2 starts with the USDT actually received.

The skill (`skills/streettape.py`) is a thin client. When `baw` is on PATH it quotes and signs through the official Agentic Wallet CLI; otherwise it uses `POST /api/swap/order`. Signing is explicit. The closed grammar accepts only fixed sentences (`what's rich vs Friday`, `rotate into cheapest NVDA`, `watch NVDA at 95% floor`, and the others listed in `skills/SKILL.md`).

## Proofs

- Settled fill: https://bscscan.com/tx/0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5
- Settled Rotate (NVDAon → NVDAB): https://bscscan.com/tx/0x160d05c5ea2c347d70b5da4498ac254732f28554ade688008970478d20bbd8b7
- Settled Rotate, second run (allowance already set): https://bscscan.com/tx/0x7a0b6cdf2d47f382529693522abc4dba6e7dde0927b3907712d2ad02aa0562f9
- Signed Agentic Wallet fill (baw market-order swap, BNB → TSLAB): https://bscscan.com/tx/0x3f2e027a3c356783cb39be21b69cb0959d15fed3c2f13dfc09f79b4ba7d2161a

## Judge path

`/` and the four chips, `/basket/defensive`, `/t/NVDA`, the Rotate sheet at `/t/NVDA#rotate`, the floor-watch panel, then DEVEX.md.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

## Agentic Wallet skill

`skills/` — skill + CLI that call `/api/board`, `/api/agent/scan`, `/api/swap/order`, `/api/agent/floor`.

Install: `npx skills add <owner>/Streettape --skill streettape-desk` (or copy the folder into your agent's skills dir). Set `STREETTAPE_URL` to point at another deployment.

Official skill: `npx skills add https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet`. When `baw` is on PATH, `skills/streettape.py` quotes and signs through it; otherwise it uses `/api/swap/order`.

## BNB Agent Studio / ERC-8004

`GET /api/agent/studio/tick` returns the same scan as the internal 60s loop (header `X-Studio-Token: $AGENT_STUDIO_TOKEN`). Proposal-only. Optional `arb_threshold` (default 0.01) and `underlying`. Studio status is in `/agent-registration.json` under `studioRuntime`. Identity file is served at `/agent-registration.json`. `x402Support` stays false: there is no paid endpoint.

## Telegram bot

Optional second door onto the same board. Started as a background task from `main.py`. Commands include `/start`, `/board`, `/session`, `/t SYMBOL`, `/watch`, `/agent`. Trading goes through the user's Binance Agentic Wallet via the `tg_*` modules. No URL appears in message text; links are inline buttons only. Swap, Buy, Sell, and Rotate on the bot follow the routing rules above, including the two-swap path via USDT when the Binance route cannot do the pair in one transaction.
