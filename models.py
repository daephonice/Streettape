from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Float, DateTime, BigInteger, JSON, Index, Text

from database import Base


def utcnow():
    return datetime.now(timezone.utc)


class PriceSnapshot(Base):
    """Rolling price history per symbol. Enough rows for a 24-72h sparkline.
    Not the live board cache — that lives in memory (see rwa.py)."""
    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    token_price = Column(Float, nullable=False)
    mark_price = Column(Float, nullable=False)
    premium = Column(Float, nullable=True)
    platform = Column(String, nullable=True)
    underlying = Column(String, nullable=True)
    fetched_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_price_snapshots_symbol_fetched", "symbol", "fetched_at"),
    )


class Watch(Base):
    """Telegram chat watching a symbol (hourly digest, see Part 5)."""
    __tablename__ = "watches"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    underlying = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    threshold = Column(Float, nullable=True)
    last_alert_at = Column(DateTime(timezone=True), nullable=True)
    last_alert_premium = Column(Float, nullable=True)

    __table_args__ = (
        Index("ix_watches_chat_symbol", "chat_id", "symbol", unique=True),
    )


class LendPosition(Base):
    """Mock isolated-vault position: one wallet × one tokenized stock × one debt token."""
    __tablename__ = "lend_positions"

    id = Column(Integer, primary_key=True)
    wallet = Column(String, nullable=False, index=True)
    collateral_symbol = Column(String, nullable=False)
    debt_symbol = Column(String, nullable=False)
    col_amount = Column(Float, nullable=False, default=0)
    debt_amount = Column(Float, nullable=False, default=0)
    debt_index = Column(Float, nullable=False, default=1)
    status = Column(String, nullable=False, default="open")
    opened_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_accrued_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_lend_wallet_vault", "wallet", "collateral_symbol", "debt_symbol", unique=True),
    )


class NewsItem(Base):
    """Homepage news feed entry for one tokenized-stock symbol. Price / change / MC
    shown next to it are live, read from the price cache — not stored here."""
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    body = Column(Text, nullable=False)
    published_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
