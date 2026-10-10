from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Float, DateTime, BigInteger, JSON, Index, Text, UniqueConstraint

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


class NewsItem(Base):
    """Homepage news feed entry for one tokenized-stock symbol. Price / change / MC
    shown next to it are live, read from the price cache — not stored here."""
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    body = Column(Text, nullable=False)
    published_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SessionGap(Base):
    """Widest wrapper-vs-print gap seen while cash was shut, one row per
    (underlying, close). Written only when session.cashOpen is false."""
    __tablename__ = "session_gaps"

    id = Column(Integer, primary_key=True)
    underlying = Column(String, nullable=False, index=True)
    session_label = Column(String, nullable=False)  # AFTER-HOURS | WEEKEND
    closed_at = Column(DateTime(timezone=True), nullable=False)
    print_price = Column(Float, nullable=False)
    wrapper = Column(String, nullable=False)
    tape_price = Column(Float, nullable=False)
    premium = Column(Float, nullable=False)
    seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("underlying", "closed_at", name="uq_session_gaps_und_closed"),
    )


class ProposalProof(Base):
    """Hash of a rotate proposal shown to the user. Proof of proposal, not of a trade."""
    __tablename__ = "proposal_proofs"

    id = Column(Integer, primary_key=True)
    hash = Column(String(66), nullable=False, unique=True)
    underlying = Column(String, nullable=False, index=True)
    rich_symbol = Column(String, nullable=False)
    cheap_symbol = Column(String, nullable=False)
    net_bps = Column(Float, nullable=False)
    payload = Column(Text, nullable=False)  # canonical JSON that was hashed
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class TgWallet(Base):
    """Telegram chat linked to a Binance Agentic Wallet (private chats only)."""
    __tablename__ = "tg_wallets"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, unique=True, index=True)
    address = Column(String, nullable=False)  # agentic wallet BSC address
    linked_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    session_end = Column(DateTime(timezone=True), nullable=True)  # signInMaxTime
    last_alive = Column(DateTime(timezone=True), nullable=True)
    reminded_for = Column(DateTime(timezone=True), nullable=True)  # session_end already warned about


class TgRecipient(Base):
    """Recent send addresses per chat."""
    __tablename__ = "tg_recipients"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    address = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_tg_recipients_chat_addr", "chat_id", "address", unique=True),
    )
