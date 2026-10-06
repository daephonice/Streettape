# StreetTape

Same stock, three wrappers, on BSC. Tape is the pool. Official is Friday's print. Fair is where it should be while cash is shut. Rotate stays wrapper vs wrapper.

- Live: https://streettape.up.railway.app
- Agent card: https://streettape.up.railway.app/agent-registration.json (ERC-8004 agent 360062)
- Settled fill: https://bscscan.com/tx/0x6226e2e5e9d4fa646fc2546793dfbed6da085476de7a3115df014d2b8cd372b5
- Report: DEVEX.md (not GET /api/_devex)

Judge clicks: `/` and the four chips, `/basket/defensive`, `/t/NVDA`, a built swap sheet, then DEVEX.md.
Spot only. Nothing auto-executes.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

## Agentic Wallet skill

`skills/` — skill + CLI that call `/api/board`, `/api/agent/scan`, `/api/swap/order`.
Install: `npx skills add <owner>/Streettape --skill streettape-desk` (or copy the folder into your agent's skills dir).
Set `STREETTAPE_URL` to point at another deployment.

Official skill: `npx skills add https://github.com/binance/binance-skills-hub/tree/main/skills/binance-web3/binance-agentic-wallet` (installs the `baw` CLI; sign in via the Binance app). When `baw` is on PATH, `skills/streettape.py` quotes/signs through it; otherwise it uses `/api/swap/order`.

## BNB Agent Studio / ERC-8004

- `GET /api/agent/studio/tick` returns the same scan as the internal 60s loop (header `X-Studio-Token: $AGENT_STUDIO_TOKEN`). Proposal-only. Optional `arb_threshold` (default 0.01) and `underlying` (adds `answers.rotate` and `answers.earnings` for that name; `answers.richVsFriday` and `answers.earningsInside24h` are always present). Studio status is in `/agent-registration.json` under `studioRuntime` (driven by `STUDIO_DEPLOYED`, `STUDIO_JOB_ID`, `STUDIO_BAG_ERROR`).
- Identity file: `/agent-registration.json`. Register on the ERC-8004 Identity Registry with `register("<WEB_PUBLIC_URL>/agent-registration.json")`, then set `ERC8004_IDENTITY_REGISTRY=0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`, `ERC8004_AGENT_ID=360062`, `ERC8004_CHAIN_ID=56`. The card then carries a `registrations` block. `x402Support` stays false: there is no paid endpoint.
