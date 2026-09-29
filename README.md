# StreetTape

Mark vs tape for tokenized stocks on BNB Chain.

Tape = GeckoTerminal. Mark = Yahoo last cash print. Swap = PancakeSwap.
Wallet = MetaMask / Binance Web3 / Trust. Agent = premium alerts on Telegram.

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

- Schedule `GET /api/agent/studio/tick` every 60s (header `X-Studio-Token: $AGENT_STUDIO_TOKEN`). Proposal-only.
- Identity file: `/agent-registration.json`. Register on the ERC-8004 Identity Registry with `register("<WEB_PUBLIC_URL>/agent-registration.json")`, then set `ERC8004_IDENTITY_REGISTRY` and `ERC8004_AGENT_ID`.
