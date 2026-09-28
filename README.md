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
