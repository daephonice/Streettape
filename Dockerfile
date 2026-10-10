FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm i -g @binance/agentic-wallet@1.10.0 --ignore-scripts \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV BAW=baw BINANCE_BAW_DIR=/data/baw TG_WALLETS_DIR=/data/wallets

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level warning
