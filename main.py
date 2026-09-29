import logging
import asyncio
import time

import devlog
devlog.t0()

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from database import engine, Base
import models  # noqa: F401
import routes_pages
import routes_api
import board
import prices
import news
import telegram_bot
import agent
import rwa_api


def init_db(retries: int = 8, delay: int = 2):
    for attempt in range(1, retries + 1):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except OperationalError:
            if attempt == retries:
                raise
            time.sleep(delay)


init_db()
app = FastAPI(title="StreetTape")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(routes_pages.router)
app.include_router(routes_api.router)


@app.on_event("startup")
async def _start_background_tasks():
    board.start_board_refresh_task()
    prices.start_price_task()
    news.start_news_task()
    telegram_bot.start_telegram_bot_task()
    agent.start_agent_task()
    asyncio.create_task(rwa_api.devcheck())
