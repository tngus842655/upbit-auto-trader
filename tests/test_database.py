"""DB 저장소 테스트 (메모리 SQLite)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database import Database, Repository
from app.database.models import (
    BalanceSnapshot,
    BotLog,
    CandleRecord,
    FillRecord,
    OrderRecord,
    RoundTripRecord,
    from_db_time,
    to_db_time,
)
from app.strategy.base import Action, Signal
from app.trading.orders import Order, OrderStatus, OrderType
from app.trading.portfolio import Portfolio, Side
from tests.test_strategy import make_frame

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def repo() -> Repository:
    db = Database("sqlite://")
    db.create_all()
    return Repository(db, mode="paper")


def test_time_helpers() -> None:
    aware = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    naive = to_db_time(aware)
    assert naive.tzinfo is None and naive.hour == 12
    assert from_db_time(naive) == aware
    assert to_db_time(None) is None and from_db_time(None) is None


def test_signal_unique_per_candle(repo: Repository) -> None:
    signal = Signal(Action.BUY, "KRW-BTC", T0, 100.0, "ma_cross", "골든크로스", {"sma_short": 1.0, "rsi": float("nan")})
    assert repo.save_signal(signal, "60m") is True
    assert repo.save_signal(signal, "60m") is False  # 같은 캔들·전략 → 한 번만
    assert repo.save_signal(signal, "15m") is True  # 다른 단위는 별개
    rows = repo.recent_signals(10)
    assert len(rows) == 2
    assert rows[0].indicators == {"sma_short": 1.0, "rsi": None}
    assert from_db_time(rows[0].time) == T0


def test_order_fill_round_trip_and_portfolio_sync(repo: Repository) -> None:
    portfolio = Portfolio(1_000_000, fee_rate=0.0005)
    fill = portfolio.buy("KRW-BTC", 50_000_000, time=T0, amount=500_000, reason="signal")
    order = Order(
        id="o-1", client_id="paper:KRW-BTC:BUY:x", mode="paper", market="KRW-BTC", side=Side.BUY,
        order_type=OrderType.MARKET, amount=500_000, quantity=None, status=OrderStatus.FILLED, created_at=T0,
        reason="signal", strategy="ma_cross", signal_time=T0 - timedelta(hours=1), filled_at=T0,
        fill_price=fill.price, filled_quantity=fill.quantity, fee=fill.fee, fills=[fill],
    )
    repo.save_order(order)
    repo.save_fill(fill, order_id=order.id, strategy="ma_cross")
    repo.sync_portfolio(portfolio)

    assert repo.count_rows(OrderRecord) == 1 and repo.count_rows(FillRecord) == 1
    assert repo.processed_client_ids() == {"paper:KRW-BTC:BUY:x"}
    saved_order = repo.recent_orders(1)[0]
    assert saved_order.status == "FILLED" and saved_order.fill_price == pytest.approx(fill.price)
    assert from_db_time(saved_order.signal_time) == T0 - timedelta(hours=1)
    fill_row = repo.recent_fills(1)[0]
    assert (fill_row.side, fill_row.order_id, fill_row.strategy) == ("BUY", "o-1", "ma_cross")
    assert fill_row.status == "filled"

    # 재시작 복구
    restored, ok = repo.restore_portfolio(initial_cash=999, fee_rate=0.0005, min_order_amount=5000)
    assert ok is True
    assert restored.initial_cash == 1_000_000
    assert restored.cash == pytest.approx(portfolio.cash)
    assert restored.fees_paid == pytest.approx(portfolio.fees_paid)
    pos = restored.position("KRW-BTC")
    assert pos is not None and pos.quantity == pytest.approx(fill.quantity) and pos.opened_at == T0

    # 청산 후 동기화 → 포지션 삭제, 왕복 거래 저장
    portfolio.sell("KRW-BTC", 55_000_000, time=T0 + timedelta(hours=2), reason="signal")
    repo.save_round_trip(portfolio.trades[0])
    repo.sync_portfolio(portfolio)
    assert repo.load_positions() == []
    assert repo.count_rows(RoundTripRecord) == 1
    rt = repo.recent_round_trips(1)[0]
    assert rt.pnl == pytest.approx(portfolio.trades[0].pnl) and rt.exit_reason == "signal"

    # 주문 갱신은 merge (같은 id 로 덮어쓰기)
    order.status = OrderStatus.REJECTED
    order.error = "테스트"
    repo.save_order(order)
    assert repo.count_rows(OrderRecord) == 1 and repo.recent_orders(1)[0].status == "REJECTED"


def test_restore_without_account_returns_fresh(repo: Repository) -> None:
    portfolio, restored = repo.restore_portfolio(initial_cash=123_456, fee_rate=0.001, min_order_amount=5000)
    assert restored is False and portfolio.cash == 123_456 and portfolio.positions == {}


def test_balance_snapshot_and_logs(repo: Repository) -> None:
    portfolio = Portfolio(1_000_000)
    portfolio.buy("KRW-ETH", 1_000, time=T0, amount=100_000)
    equity = repo.snapshot_balance(portfolio, {"KRW-ETH": 1_100}, T0)
    snap = repo.latest_balance()
    assert snap is not None and snap.equity == pytest.approx(equity)
    assert snap.positions_value == pytest.approx(equity - portfolio.cash)
    assert snap.unrealized_pnl > 0
    repo.log("info", "bot_start", "시작", {"a": 1})
    repo.log("ERROR", "api_error", "실패")
    assert repo.count_rows(BalanceSnapshot) == 1 and repo.count_rows(BotLog) == 2
    assert [entry.event for entry in repo.recent_logs(5, level="error")] == ["api_error"]
    assert repo.recent_logs(1)[0].level in ("INFO", "ERROR")


def test_save_candles_ignores_duplicates(repo: Repository) -> None:
    df = make_frame([100, 101, 102, 103])
    assert repo.save_candles("KRW-TEST", "60m", df) == 4
    assert repo.save_candles("KRW-TEST", "60m", df.iloc[2:]) == 0
    more = make_frame([100, 101, 102, 103, 104])
    assert repo.save_candles("KRW-TEST", "60m", more) == 1
    assert repo.count_rows(CandleRecord) == 5
    times = list(repo.candle_times("KRW-TEST", "60m"))
    assert times == sorted(times) and times[0].tzinfo is UTC


def test_file_database_creates_parent_dir(tmp_path) -> None:
    db = Database(f"sqlite:///{(tmp_path / 'nested' / 'trader.db').as_posix()}")
    db.create_all()
    assert (tmp_path / "nested" / "trader.db").exists()
    assert db.ping() is True
    db.dispose()
