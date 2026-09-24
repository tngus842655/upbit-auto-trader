"""기록 저장소: 엔진이 만드는 신호·주문·체결·포지션·잔고·로그를 DB 에 쓰고, 재시작 시 상태를 되돌린다."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.database.database import Database
from app.database.models import (
    AccountRecord,
    BalanceSnapshot,
    BotCommand,
    BotLog,
    CandleRecord,
    EngineStatus,
    FillRecord,
    OrderRecord,
    PositionRecord,
    RoundTripRecord,
    SignalRecord,
    from_db_time,
    to_db_time,
)
from app.strategy.base import Signal
from app.trading.orders import Order
from app.trading.portfolio import Fill, Portfolio, Position, Trade

log = logging.getLogger(__name__)


class Repository:
    def __init__(self, db: Database, mode: str = "paper") -> None:
        self.db = db
        self.mode = mode

    # ------------------------------------------------------------------
    # 쓰기
    # ------------------------------------------------------------------
    def save_signal(self, signal: Signal, interval: str) -> bool:
        """같은 (마켓, 단위, 전략, 캔들 시각) 신호는 한 번만 저장한다. 새로 저장했으면 True."""
        record = SignalRecord(
            time=to_db_time(signal.time), market=signal.market, interval=interval, strategy=signal.strategy,
            action=signal.action.value, price=signal.price, reason=signal.reason,
            indicators=signal.to_dict()["indicators"], mode=self.mode,
        )
        try:
            with self.db.session() as s:
                s.add(record)
            return True
        except IntegrityError:
            return False

    def save_order(self, order: Order) -> None:
        with self.db.session() as s:
            s.merge(
                OrderRecord(
                    id=order.id, client_id=order.client_id, mode=order.mode, market=order.market,
                    side=order.side.value, order_type=order.order_type.value, amount=order.amount,
                    quantity=order.quantity, status=order.status.value, fill_price=order.fill_price,
                    filled_quantity=order.filled_quantity, fee=order.fee, reason=order.reason,
                    strategy=order.strategy, signal_time=to_db_time(order.signal_time),
                    created_at=to_db_time(order.created_at), filled_at=to_db_time(order.filled_at),
                    error=order.error, exchange_order_id=order.exchange_order_id,
                )
            )

    def save_fill(self, fill: Fill, *, order_id: str, strategy: str = "") -> None:
        with self.db.session() as s:
            s.add(
                FillRecord(
                    time=to_db_time(fill.time), mode=self.mode, market=fill.market, side=fill.side.value,
                    price=fill.price, quantity=fill.quantity, amount=fill.amount, fee=fill.fee,
                    order_id=order_id, strategy=strategy, reason=fill.reason, status="filled",
                )
            )

    def save_round_trip(self, trade: Trade) -> None:
        with self.db.session() as s:
            s.add(
                RoundTripRecord(
                    mode=self.mode, market=trade.market, entry_time=to_db_time(trade.entry_time),
                    entry_price=trade.entry_price, quantity=trade.quantity, entry_amount=trade.entry_amount,
                    entry_fee=trade.entry_fee, exit_time=to_db_time(trade.exit_time), exit_price=trade.exit_price,
                    exit_amount=trade.exit_amount, exit_fee=trade.exit_fee, exit_reason=trade.exit_reason,
                    pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                )
            )

    def sync_portfolio(self, portfolio: Portfolio) -> None:
        """계좌 현금과 현재 포지션을 DB 와 맞춘다 (닫힌 포지션은 삭제)."""
        with self.db.session() as s:
            s.merge(
                AccountRecord(
                    mode=self.mode, initial_cash=portfolio.initial_cash, cash=portfolio.cash,
                    fees_paid=portfolio.fees_paid,
                )
            )
            open_markets = set(portfolio.positions)
            s.execute(delete(PositionRecord).where(PositionRecord.mode == self.mode))
            for market in open_markets:
                pos = portfolio.positions[market]
                s.add(
                    PositionRecord(
                        mode=self.mode, market=market, quantity=pos.quantity, avg_price=pos.avg_price,
                        entry_amount=pos.entry_amount, entry_fee=pos.entry_fee, opened_at=to_db_time(pos.opened_at),
                    )
                )

    def snapshot_balance(
        self, portfolio: Portfolio, prices: Mapping[str, float], time: datetime | None = None
    ) -> float:
        equity = portfolio.equity(prices)
        positions_value = equity - portfolio.cash
        unrealized = sum(p.unrealized_pnl(prices[m]) for m, p in portfolio.positions.items())
        with self.db.session() as s:
            s.add(
                BalanceSnapshot(
                    time=to_db_time(time or datetime.now(UTC)), mode=self.mode, cash=portfolio.cash,
                    positions_value=positions_value, equity=equity,
                    realized_pnl=sum(t.pnl for t in portfolio.trades), unrealized_pnl=unrealized,
                )
            )
        return equity

    def log(self, level: str, event: str, message: str, data: dict[str, Any] | None = None) -> None:
        with self.db.session() as s:
            s.add(BotLog(mode=self.mode, level=level.upper(), event=event, message=message, data=data))

    def save_candles(self, market: str, interval: str, df: pd.DataFrame) -> int:
        """닫힌 캔들을 저장한다. 이미 있는 시각은 건너뛴다. 저장한 행 수를 돌려준다."""
        if df.empty:
            return 0
        with self.db.session() as s:
            existing = set(
                s.execute(
                    select(CandleRecord.time).where(
                        CandleRecord.market == market, CandleRecord.interval == interval,
                        CandleRecord.time >= to_db_time(df.index[0].to_pydatetime()),
                    )
                ).scalars()
            )
            added = 0
            for ts, row in df.iterrows():
                t = to_db_time(ts.to_pydatetime())
                if t in existing:
                    continue
                s.add(
                    CandleRecord(
                        market=market, interval=interval, time=t, open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
                        value=float(row["value"]),
                    )
                )
                added += 1
        return added

    # ------------------------------------------------------------------
    # 읽기 / 복구
    # ------------------------------------------------------------------
    def load_account(self) -> AccountRecord | None:
        with self.db.session() as s:
            return s.get(AccountRecord, self.mode)

    def load_positions(self) -> list[PositionRecord]:
        with self.db.session() as s:
            return list(s.execute(select(PositionRecord).where(PositionRecord.mode == self.mode)).scalars())

    def restore_portfolio(
        self, *, initial_cash: float, fee_rate: float, min_order_amount: float
    ) -> tuple[Portfolio, bool]:
        """DB 에 계좌가 있으면 현금·포지션을 복구한 Portfolio, 없으면 새 Portfolio. (portfolio, restored)."""
        account = self.load_account()
        portfolio = Portfolio(initial_cash, fee_rate=fee_rate, min_order_amount=min_order_amount)
        if account is None:
            return portfolio, False
        portfolio.initial_cash = account.initial_cash
        portfolio.cash = account.cash
        portfolio.fees_paid = account.fees_paid
        for rec in self.load_positions():
            portfolio.positions[rec.market] = Position(
                market=rec.market, quantity=rec.quantity, avg_price=rec.avg_price,
                opened_at=from_db_time(rec.opened_at) or datetime.now(UTC), entry_amount=rec.entry_amount,
                entry_fee=rec.entry_fee,
            )
        return portfolio, True

    def processed_client_ids(self, limit: int = 5000) -> set[str]:
        with self.db.session() as s:
            rows = s.execute(
                select(OrderRecord.client_id).where(OrderRecord.mode == self.mode)
                .order_by(OrderRecord.created_at.desc()).limit(limit)
            ).scalars()
            return set(rows)

    def recent_signals(self, limit: int = 10, market: str | None = None) -> list[SignalRecord]:
        with self.db.session() as s:
            stmt = select(SignalRecord).where(SignalRecord.mode == self.mode)
            if market:
                stmt = stmt.where(SignalRecord.market == market)
            return list(s.execute(stmt.order_by(SignalRecord.time.desc()).limit(limit)).scalars())

    def recent_orders(self, limit: int = 10) -> list[OrderRecord]:
        with self.db.session() as s:
            stmt = select(OrderRecord).where(OrderRecord.mode == self.mode).order_by(OrderRecord.created_at.desc())
            return list(s.execute(stmt.limit(limit)).scalars())

    def recent_fills(self, limit: int = 10) -> list[FillRecord]:
        with self.db.session() as s:
            stmt = select(FillRecord).where(FillRecord.mode == self.mode).order_by(FillRecord.time.desc())
            return list(s.execute(stmt.limit(limit)).scalars())

    def recent_round_trips(self, limit: int = 10) -> list[RoundTripRecord]:
        with self.db.session() as s:
            stmt = select(RoundTripRecord).where(RoundTripRecord.mode == self.mode)
            return list(s.execute(stmt.order_by(RoundTripRecord.exit_time.desc()).limit(limit)).scalars())

    def load_round_trips(self, since: datetime | None = None) -> list[Trade]:
        """왕복 거래를 Portfolio.Trade 객체로 되돌린다 (리스크 상태 복구용)."""
        with self.db.session() as s:
            stmt = select(RoundTripRecord).where(RoundTripRecord.mode == self.mode)
            if since is not None:
                stmt = stmt.where(RoundTripRecord.exit_time >= to_db_time(since))
            rows = s.execute(stmt.order_by(RoundTripRecord.exit_time)).scalars()
            return [
                Trade(
                    market=r.market, entry_time=from_db_time(r.entry_time), entry_price=r.entry_price,
                    quantity=r.quantity, entry_amount=r.entry_amount, entry_fee=r.entry_fee,
                    exit_time=from_db_time(r.exit_time), exit_price=r.exit_price, exit_amount=r.exit_amount,
                    exit_fee=r.exit_fee, exit_reason=r.exit_reason, pnl=r.pnl, pnl_pct=r.pnl_pct,
                )
                for r in rows
            ]

    def latest_balance(self) -> BalanceSnapshot | None:
        with self.db.session() as s:
            stmt = select(BalanceSnapshot).where(BalanceSnapshot.mode == self.mode)
            stmt = stmt.order_by(BalanceSnapshot.time.desc())
            return s.execute(stmt.limit(1)).scalar_one_or_none()

    def recent_logs(self, limit: int = 10, level: str | None = None) -> list[BotLog]:
        with self.db.session() as s:
            stmt = select(BotLog).where(BotLog.mode == self.mode)
            if level:
                stmt = stmt.where(BotLog.level == level.upper())
            return list(s.execute(stmt.order_by(BotLog.time.desc()).limit(limit)).scalars())

    # ------------------------------------------------------------------
    # 엔진 상태 · 명령 큐 (대시보드 연동용)
    # ------------------------------------------------------------------
    def write_engine_status(self, **fields: Any) -> None:
        for key in ("started_at", "last_data_at", "last_candle_at", "last_trade_at"):
            if key in fields:
                fields[key] = to_db_time(fields[key])
        with self.db.session() as s:
            record = s.get(EngineStatus, self.mode) or EngineStatus(mode=self.mode, status="STOPPED")
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = datetime.now(UTC).replace(tzinfo=None)
            s.merge(record)

    def read_engine_status(self) -> EngineStatus | None:
        with self.db.session() as s:
            return s.get(EngineStatus, self.mode)

    def enqueue_command(self, command: str, args: dict[str, Any] | None = None) -> int:
        with self.db.session() as s:
            record = BotCommand(mode=self.mode, command=command, args=args)
            s.add(record)
            s.flush()
            return int(record.id)

    def pending_commands(self) -> list[BotCommand]:
        with self.db.session() as s:
            stmt = select(BotCommand).where(BotCommand.mode == self.mode, BotCommand.processed_at.is_(None))
            return list(s.execute(stmt.order_by(BotCommand.created_at)).scalars())

    def mark_command(self, command_id: int, result: str) -> None:
        with self.db.session() as s:
            record = s.get(BotCommand, command_id)
            if record is not None:
                record.processed_at = datetime.now(UTC).replace(tzinfo=None)
                record.result = result

    def count_rows(self, model: type) -> int:
        with self.db.session() as s:
            return len(list(s.execute(select(model)).scalars()))

    def candle_times(self, market: str, interval: str) -> Iterable[datetime]:
        with self.db.session() as s:
            stmt = select(CandleRecord.time).where(CandleRecord.market == market, CandleRecord.interval == interval)
            return [from_db_time(t) for t in s.execute(stmt.order_by(CandleRecord.time)).scalars()]
