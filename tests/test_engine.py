"""매매 엔진 테스트 — 가짜 REST 클라이언트·메모리 DB 로 신호→리스크→가상 체결→기록→재시작 복구를 검증한다."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pandas as pd
import pytest

from app.core.exceptions import ConfigError
from app.database import Database, Repository
from app.database.models import BalanceSnapshot, BotLog, CandleRecord, FillRecord, OrderRecord, RoundTripRecord
from app.exchange.models import CandleInterval, Ticker
from app.exchange.ws_models import parse_ws_message
from app.risk import BasicRiskManager, RiskConfig, RiskManager
from app.strategy.base import ACTION_COLUMN, REASON_COLUMN, Strategy, StrategyParams
from app.trading.engine import TradingEngine, next_boundary
from app.trading.orders import PaperBroker
from tests.test_models import TICKER_JSON
from tests.test_strategy_data import make_candle
from tests.test_websocket import ORDERBOOK_JSON

T0 = datetime(2026, 5, 1, tzinfo=UTC)
MARKET = "KRW-BTC"


class TimedStrategy(Strategy):
    """캔들 시각별로 정해진 행동을 내는 전략."""

    name: ClassVar[str] = "timed"
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self, actions: dict[datetime, str]) -> None:
        super().__init__()
        self.actions = actions

    @property
    def warmup_periods(self) -> int:
        return 3

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df[["close"]].copy()
        out[ACTION_COLUMN] = [self.actions.get(ts.to_pydatetime(), "HOLD") for ts in df.index]
        out[REASON_COLUMN] = ["scripted" if a != "HOLD" else "" for a in out[ACTION_COLUMN]]
        return out


class FakeClient:
    """REST 클라이언트 대역: 준비된 캔들을 업비트처럼 최신순으로 돌려준다."""

    def __init__(self, candles: list, ticker_price: float = 100.0, now=None) -> None:
        self.candles = list(candles)
        self.now = now  # 실제 API 는 미래 캔들을 주지 않으므로 now 이후 캔들은 숨긴다
        self.ticker_price = ticker_price
        self.candle_calls: list[tuple[str, int]] = []
        self.ticker_calls = 0

    async def get_candles(self, market: str, interval, *, count: int = 200, to=None):
        self.candle_calls.append((market, count))
        rows = sorted(self.candles, key=lambda c: c.candle_date_time_utc, reverse=True)
        if self.now is not None:
            rows = [c for c in rows if c.candle_date_time_utc <= self.now()]
        if to is not None:
            rows = [c for c in rows if c.candle_date_time_utc < to]
        return rows[:count]

    async def get_tickers(self, markets):
        self.ticker_calls += 1
        return [Ticker.model_validate({**TICKER_JSON, "market": m, "trade_price": self.ticker_price}) for m in markets]


class Harness:
    def __init__(self, make_settings, *, actions: dict[datetime, str] | None = None, db: Database | None = None,
                 candles: list | None = None) -> None:
        self.settings = make_settings(trading_mode="PAPER", markets=[MARKET], candle_interval="60m",
                                      paper_initial_cash=1_000_000, snapshot_interval_seconds=300)
        self.db = db or Database("sqlite://")
        self.db.create_all()
        self.repo = Repository(self.db, mode="paper")
        self.portfolio, self.restored = self.repo.restore_portfolio(initial_cash=1_000_000, fee_rate=0.0005,
                                                                    min_order_amount=5000)
        self.broker = PaperBroker(
            self.portfolio, slippage_rate=0.001, processed_client_ids=self.repo.processed_client_ids()
        )
        self.client = FakeClient(candles if candles is not None else [make_candle(T0 + timedelta(hours=i), 100 + i)
                                                                      for i in range(12)], now=lambda: self.now)
        self.now = T0 + timedelta(hours=10, seconds=5)  # 09:00 캔들까지 닫힘, 10:00 진행 중
        self.strategy = TimedStrategy(actions or {})
        self.engine = TradingEngine(
            self.settings, strategy=self.strategy, portfolio=self.portfolio, broker=self.broker,
            risk=RiskManager(RiskConfig(position_fraction=0.5, stop_loss_pct=0.05, daily_loss_limit_pct=None,
                                        max_consecutive_losses=None, price_deviation_limit=None)),
            repo=self.repo, client=self.client,
            clock=lambda: self.now, warmup_candles=8, refresh_candles=3,
        )

    def feed_prices(self, ask: float = 100.5, bid: float = 100.0) -> None:
        book = {**ORDERBOOK_JSON, "code": MARKET, "timestamp": int(self.now.timestamp() * 1000),
                "orderbook_units": [{"ask_price": ask, "bid_price": bid, "ask_size": 1, "bid_size": 1}]}
        self.engine.state.update_price(parse_ws_message(json.dumps(book)))

    def add_candle(self, hour: int, close: float) -> None:
        self.client.candles.append(make_candle(T0 + timedelta(hours=hour), close))


def test_next_boundary_aligns_to_interval() -> None:
    now = datetime(2026, 5, 1, 9, 17, 30, tzinfo=UTC)
    assert next_boundary(now, 3600, 3) == datetime(2026, 5, 1, 10, 0, 3, tzinfo=UTC)
    assert next_boundary(now, 900, 0) == datetime(2026, 5, 1, 9, 30, tzinfo=UTC)
    on_boundary = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
    assert next_boundary(on_boundary, 3600, 1) == datetime(2026, 5, 1, 11, 0, 1, tzinfo=UTC)


def test_engine_requires_paper_mode(make_settings) -> None:
    h = Harness(make_settings)
    with pytest.raises(ConfigError, match="PAPER"):
        TradingEngine(make_settings(trading_mode="BACKTEST"), strategy=h.strategy, portfolio=h.portfolio,
                      broker=h.broker, risk=BasicRiskManager(), repo=h.repo, client=h.client)


async def test_warmup_loads_closed_candles_and_saves_them(make_settings) -> None:
    h = Harness(make_settings)
    await h.engine.warmup()
    df = h.engine.state.frame(MARKET)
    assert len(df) == 8 and df.index[-1].to_pydatetime() == T0 + timedelta(hours=9)  # 10:00 은 진행 중
    assert h.repo.count_rows(CandleRecord) == 8
    assert h.client.candle_calls[0] == (MARKET, 9)  # 진행 중 캔들 1개 포함


async def test_signal_to_paper_fill_to_db_and_round_trip(make_settings) -> None:
    buy_t, sell_t = T0 + timedelta(hours=10), T0 + timedelta(hours=11)
    h = Harness(make_settings, actions={buy_t: "BUY", sell_t: "SELL"})
    await h.engine.warmup()

    # 10:00 캔들이 아직 진행 중 → 새 캔들 없음, 신호 없음
    assert await h.engine.process_closed_candles(h.now) == []

    # 11:00:05 → 10:00 캔들 확정 → BUY 신호 → 가상 체결 (시세는 처리 직전의 것)
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    signals = await h.engine.process_closed_candles(h.now)
    assert [s.action.value for s in signals] == ["BUY"] and signals[0].time == buy_t
    pos = h.portfolio.position(MARKET)
    assert pos is not None and pos.avg_price == pytest.approx(110.0 * 1.001)
    assert pos.entry_amount + pos.entry_fee <= 500_000 + 1e-6  # position_fraction 0.5
    assert h.repo.count_rows(OrderRecord) == 1 and h.repo.count_rows(FillRecord) == 1
    order = h.repo.recent_orders(1)[0]
    assert order.status == "FILLED" and order.strategy == "timed" and order.client_id.endswith(buy_t.isoformat())
    assert h.repo.load_positions()[0].quantity == pytest.approx(pos.quantity)
    assert h.repo.load_account().cash == pytest.approx(h.portfolio.cash)
    assert h.repo.count_rows(BalanceSnapshot) == 1
    assert {entry.event for entry in h.repo.recent_logs(10)} >= {"order_filled"}
    assert h.engine.stats.orders_filled == 1 and h.engine.stats.closed_candles == 1

    # 같은 시각에 다시 호출 → 새 캔들 없음 → 아무 일도 없음 (중복 방지 1단계)
    assert await h.engine.process_closed_candles(h.now) == []
    assert h.repo.count_rows(OrderRecord) == 1

    # 12:00:05 → 11:00 캔들 확정 → SELL → 왕복 거래 저장, 포지션 삭제
    h.add_candle(12, 112.0)
    h.now = T0 + timedelta(hours=12, seconds=5)
    h.feed_prices(ask=121.0, bid=120.0)
    signals = await h.engine.process_closed_candles(h.now)
    assert [s.action.value for s in signals] == ["SELL"]
    assert h.portfolio.position(MARKET) is None
    assert h.repo.count_rows(RoundTripRecord) == 1 and h.repo.load_positions() == []
    rt = h.repo.recent_round_trips(1)[0]
    assert rt.exit_price == pytest.approx(120.0 * 0.999) and rt.pnl > 0
    assert h.repo.count_rows(CandleRecord) == 10


async def test_restart_recovers_state_and_skips_processed_signal(make_settings) -> None:
    buy_t = T0 + timedelta(hours=10)
    db = Database("sqlite://")
    h1 = Harness(make_settings, actions={buy_t: "BUY"}, db=db)
    await h1.engine.warmup()
    h1.now = T0 + timedelta(hours=11, seconds=5)
    h1.feed_prices(ask=100.0, bid=99.0)
    await h1.engine.process_closed_candles(h1.now)
    cash_after = h1.portfolio.cash
    assert h1.portfolio.has_position(MARKET)

    # "재시작": 같은 DB 로 새 구성 요소 생성
    h2 = Harness(make_settings, actions={buy_t: "BUY"}, db=db)
    assert h2.restored is True
    assert h2.portfolio.cash == pytest.approx(cash_after)
    assert h2.portfolio.has_position(MARKET)
    assert h2.broker.processed  # 처리 이력 복구
    await h2.engine.warmup()
    h2.now = T0 + timedelta(hours=11, seconds=30)
    h2.feed_prices(ask=100.0, bid=99.0)
    signals = await h2.engine.process_closed_candles(h2.now)
    # 새 MarketState 에는 10:00 캔들이 "새로" 보이지만, 신호는 이미 DB 에 있으므로 주문까지 가지 않는다
    assert [s.action.value for s in signals] == ["BUY"]
    assert h2.repo.count_rows(OrderRecord) == 1
    assert h2.engine.stats.orders_filled == 0 and h2.engine.stats.risk_rejections == 0
    assert h2.portfolio.cash == pytest.approx(cash_after)


async def test_risk_rejection_and_missing_price(make_settings) -> None:
    buy_t = T0 + timedelta(hours=10)
    h = Harness(make_settings, actions={buy_t: "BUY"})
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    # 시세를 넣지 않음 → 브로커가 거부 → REJECTED 주문 기록
    await h.engine.process_closed_candles(h.now)
    assert h.engine.stats.orders_rejected == 1
    rejected = h.repo.recent_orders(1)[0]
    assert rejected.status == "REJECTED" and "가격" in rejected.error
    assert {entry.event for entry in h.repo.recent_logs(10)} >= {"order_rejected"}

    # 보유 중 BUY → 리스크 거부
    h.portfolio.buy(MARKET, 100.0, time=h.now, amount=100_000)
    h.add_candle(12, 111.0)
    h.strategy.actions[T0 + timedelta(hours=11)] = "BUY"
    h.now = T0 + timedelta(hours=12, seconds=5)
    h.feed_prices()
    await h.engine.process_closed_candles(h.now)
    assert h.engine.stats.risk_rejections == 1
    assert any(entry.event == "risk_rejected" for entry in h.repo.recent_logs(10))
    assert h.repo.count_rows(OrderRecord) == 1


async def test_run_loop_with_duration_writes_start_stop_logs(make_settings) -> None:
    h = Harness(make_settings)
    h.engine.clock = lambda: datetime.now(UTC)
    old = [make_candle(datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=i), 100.0)
           for i in range(1, 12)]
    h.client.candles = old
    stats = await h.engine.run(duration_seconds=0.3)
    assert h.engine.status == "stopped"
    events = [entry.event for entry in h.repo.recent_logs(20)]
    assert "bot_start" in events and "bot_stop" in events
    assert stats.snapshots >= 2 and stats.last_equity == pytest.approx(1_000_000)
    assert h.client.ticker_calls >= 1
    assert h.repo.count_rows(BotLog) >= 2
    assert CandleInterval.parse("60m") is h.engine.interval
