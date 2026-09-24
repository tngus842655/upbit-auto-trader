"""ORM 모델 (SQLAlchemy 2.x). 프롬프트 16절의 테이블 구성:

- ``strategy_signals`` 전략 신호, ``orders`` 주문(가상/실제 공통 스키마), ``trades`` 체결(주문 1건당 1행 이상)
- ``round_trips`` 왕복 거래 손익, ``positions`` 현재 보유 포지션, ``accounts`` 계좌 현금(재시작 복구용)
- ``balances`` 자산 스냅샷, ``bot_logs`` 봇 이벤트 로그, ``market_data`` 닫힌 캔들

시각은 모두 **UTC naive** 로 저장한다 (SQLite 는 시간대를 저장하지 않는다).
읽을 때 ``from_db_time`` 으로 UTC aware 로 되돌린다.
``mode`` 열(paper/live)로 모의·실거래 기록을 한 DB 에 구분해 담는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def to_db_time(value: datetime | None) -> datetime | None:
    """aware → UTC naive. naive 는 이미 UTC 라고 본다."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def from_db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime, index=True)  # 신호를 만든 캔들 시작 시각(UTC)
    market: Mapped[str] = mapped_column(String(20), index=True)
    interval: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    indicators: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    mode: Mapped[str] = mapped_column(String(10), default="paper")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)

    __table_args__ = (UniqueConstraint("mode", "market", "interval", "strategy", "time", name="uq_signal"),)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(120), unique=True)  # 중복 주문 방지 키
    mode: Mapped[str] = mapped_column(String(10), index=True)
    market: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(10))
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)  # 매수 예산(KRW)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)  # 매도 수량
    status: Mapped[str] = mapped_column(String(12), index=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    strategy: Mapped[str] = mapped_column(String(50), default="")
    signal_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # Phase 7: 업비트 uuid


class FillRecord(Base):
    """체결 로그 — 프롬프트 15절 필수 필드(timestamp, market, side, price, volume, order_id, strategy, fee, status)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime, index=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    market: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    strategy: Mapped[str] = mapped_column(String(50), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="filled")


class RoundTripRecord(Base):
    __tablename__ = "round_trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    market: Mapped[str] = mapped_column(String(20), index=True)
    entry_time: Mapped[datetime] = mapped_column(DateTime)
    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    entry_amount: Mapped[float] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float)
    exit_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    exit_price: Mapped[float] = mapped_column(Float)
    exit_amount: Mapped[float] = mapped_column(Float)
    exit_fee: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(30))
    pnl: Mapped[float] = mapped_column(Float)
    pnl_pct: Mapped[float] = mapped_column(Float)


class PositionRecord(Base):
    __tablename__ = "positions"

    mode: Mapped[str] = mapped_column(String(10), primary_key=True)
    market: Mapped[str] = mapped_column(String(20), primary_key=True)
    quantity: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    entry_amount: Mapped[float] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class AccountRecord(Base):
    __tablename__ = "accounts"

    mode: Mapped[str] = mapped_column(String(10), primary_key=True)
    initial_cash: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class BalanceSnapshot(Base):
    __tablename__ = "balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime, index=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    cash: Mapped[float] = mapped_column(Float)
    positions_value: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)


class BotLog(Base):
    __tablename__ = "bot_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="paper")
    level: Mapped[str] = mapped_column(String(10))
    event: Mapped[str] = mapped_column(String(50), index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class CandleRecord(Base):
    __tablename__ = "market_data"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(20))
    interval: Mapped[str] = mapped_column(String(8))
    time: Mapped[datetime] = mapped_column(DateTime)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    value: Mapped[float] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("market", "interval", "time", name="uq_candle"),
        Index("ix_candle_lookup", "market", "interval", "time"),
    )


class EngineStatus(Base):
    """엔진 하트비트 — 대시보드(Phase 8)가 읽는 단일 진실. 엔진이 주기적으로 갱신한다."""

    __tablename__ = "engine_status"

    mode: Mapped[str] = mapped_column(String(10), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # RUNNING / PAUSED / STOPPED
    strategy: Mapped[str] = mapped_column(String(50), default="")
    markets: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    interval: Mapped[str] = mapped_column(String(8), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)
    api_ok: Mapped[bool] = mapped_column(default=True)
    ws_status: Mapped[str] = mapped_column(String(16), default="DISCONNECTED")
    last_data_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_candle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BotCommand(Base):
    """외부(CLI·대시보드)에서 엔진으로 보내는 명령 큐. 엔진이 폴링해 처리하고 결과를 남긴다."""

    __tablename__ = "bot_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    command: Mapped[str] = mapped_column(String(20))  # pause / resume / stop / halt / resume_risk
    args: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
