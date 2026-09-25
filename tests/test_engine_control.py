"""엔진 제어 이음새 테스트 — 일시정지, 명령 큐, 하트비트, LIVE 모드 안전장치."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ConfigError
from app.database import Database, Repository
from app.exchange.ws_models import parse_ws_message
from app.risk import RiskManager
from app.trading.engine import TradingEngine
from app.trading.live_broker import LiveBroker
from app.trading.orders import PaperBroker
from app.trading.portfolio import Portfolio
from tests.test_engine import MARKET, T0, FakeClient, Harness, TimedStrategy
from tests.test_strategy_data import make_candle
from tests.test_websocket import ORDERBOOK_JSON


async def test_pause_blocks_new_entries_but_allows_exits(make_settings) -> None:
    buy_t, sell_t = T0 + timedelta(hours=10), T0 + timedelta(hours=11)
    h = Harness(make_settings, actions={buy_t: "BUY", sell_t: "SELL"})
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)

    h.repo.enqueue_command("pause")
    assert h.engine.poll_commands() == ["pause"]
    assert h.engine.paused is True
    await h.engine.process_closed_candles(h.now)
    assert not h.portfolio.has_position(MARKET)  # 매수 신호 무시
    assert any(entry.event == "paused_skip" for entry in h.repo.recent_logs(10))
    assert h.repo.pending_commands() == []

    h.repo.enqueue_command("resume")
    assert h.engine.poll_commands() == ["resume"] and h.engine.paused is False
    # 일시정지 중에도 청산은 동작: 포지션을 만들고 pause 후 SELL 신호
    h.portfolio.buy(MARKET, 100.0, time=h.now, amount=100_000)
    h.repo.enqueue_command("pause")
    h.engine.poll_commands()
    h.add_candle(12, 111.0)
    h.now = T0 + timedelta(hours=12, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)
    assert not h.portfolio.has_position(MARKET)
    assert h.engine.stats.commands_processed == 3


async def test_stop_halt_and_unknown_commands(make_settings) -> None:
    h = Harness(make_settings)
    h.repo.enqueue_command("halt", {"reason": "테스트"})
    h.repo.enqueue_command("bogus")
    h.repo.enqueue_command("stop")
    handled = h.engine.poll_commands()
    assert handled == ["halt", "bogus", "stop"]
    assert h.engine.risk.state.halted and h.engine.paused
    assert h.engine._stop.is_set()
    results = {c.event: c.message for c in h.repo.recent_logs(10) if c.event == "command"}
    assert "알 수 없는 명령" in results["command"] or any("알 수 없는" in e.message for e in h.repo.recent_logs(10))
    h.repo.enqueue_command("resume_risk")
    h.engine.poll_commands()
    assert h.engine.risk.state.halted is False


async def test_heartbeat_written_and_updated(make_settings) -> None:
    h = Harness(make_settings)
    h.engine.status = "running"
    h.engine.write_heartbeat()
    status = h.repo.read_engine_status()
    assert status is not None and status.status == "RUNNING" and status.mode == "paper"
    assert status.strategy == "timed" and status.markets == [MARKET] and status.interval == "60m"
    assert status.ws_status == "NOT_USED" and status.pid
    h.engine.paused = True
    h.engine.write_heartbeat()
    assert h.repo.read_engine_status().status == "PAUSED"
    h.engine.write_heartbeat("STOPPED")
    assert h.repo.read_engine_status().status == "STOPPED"
    assert h.engine.stats.heartbeats == 3


async def test_run_loop_processes_commands_and_heartbeats(make_settings) -> None:
    h = Harness(make_settings)
    h.engine.clock = lambda: datetime.now(UTC)
    h.engine.command_poll_interval = 0.05
    h.client.candles = [
        __import__("tests.test_strategy_data", fromlist=["make_candle"]).make_candle(
            datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=i), 100.0
        )
        for i in range(1, 12)
    ]
    h.repo.enqueue_command("pause")  # 시작 전에 쌓인 명령은 무시된다 (죽은 엔진에 보낸 명령이 새 실행을 건드리지 않게)

    async def stop_soon() -> None:
        await asyncio.sleep(0.3)
        h.repo.enqueue_command("stop")  # 실행 중 들어온 명령은 처리 → 루프 정지

    stopper = asyncio.create_task(stop_soon())
    stats = await h.engine.run(duration_seconds=5)
    await stopper
    assert stats.commands_processed == 1 and h.engine.status == "stopped" and h.engine.paused is False
    assert h.repo.read_engine_status().status == "STOPPED"
    assert h.repo.pending_commands() == []
    assert any(e.event == "stale_commands" for e in h.repo.recent_logs(20))


async def test_unknown_order_is_recorded_without_touching_portfolio(make_settings) -> None:
    """감사 CRITICAL-1/HIGH-1: UNKNOWN 주문은 계좌를 바꾸지 않고 기록·알림만 남긴다."""
    from app.trading.orders import Order, OrderStatus, OrderType
    from app.trading.portfolio import Side

    h = Harness(make_settings)
    order = Order(
        id="o-unknown", client_id="paper:KRW-BTC:BUY:x", mode="paper", market=MARKET, side=Side.BUY,
        order_type=OrderType.MARKET, amount=100_000, quantity=None, status=OrderStatus.UNKNOWN, created_at=h.now,
        error="주문 응답 없음, 생성 여부 확인 실패 (identifier c1, 운영자 확인 필요)", exchange_identifier="c1",
    )
    h.engine._record_order(order)
    assert h.engine.stats.orders_unknown == 1 and h.engine.stats.orders_filled == 0
    assert h.portfolio.cash == 1_000_000 and not h.portfolio.has_position(MARKET)
    saved = h.repo.recent_orders(1)[0]
    assert saved.status == "UNKNOWN" and saved.client_id == order.client_id
    assert any(e.event == "order_unknown" and e.level == "ERROR" for e in h.repo.recent_logs(5))
    assert h.engine.stats_dict()["orders_unknown"] == 1


class ResolvingPaperBroker(PaperBroker):
    """resolve_order 를 가진 가짜 브로커 — 확정 결과를 큐로 스크립트한다."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.resolutions: list[str] = []  # 'fill' | 'none'
        self.resolve_calls = 0

    async def resolve_order(self, order, now):
        self.resolve_calls += 1
        action = self.resolutions.pop(0) if self.resolutions else "none"
        if action != "fill":
            return None
        fill = self.portfolio.buy(order.market, 110.0, time=now, amount=order.amount, reason="resolved",
                                  enforce_limits=False)
        from app.trading.orders import OrderStatus

        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = fill.price
        order.filled_quantity = fill.quantity
        order.fee = fill.fee
        order.fills.append(fill)
        return order


def make_unknown(h: Harness, **overrides):
    from app.trading.orders import Order, OrderStatus, OrderType
    from app.trading.portfolio import Side

    base = dict(
        id="o-unknown", client_id="paper:KRW-BTC:BUY:u", mode="paper", market=MARKET, side=Side.BUY,
        order_type=OrderType.MARKET, amount=100_000, quantity=None, status=OrderStatus.UNKNOWN, created_at=h.now,
        error="체결 확인 실패", exchange_order_id="u1",
    )
    base.update(overrides)
    return Order(**base)


async def test_pending_unknown_order_blocks_market_until_resolved(make_settings) -> None:
    """감사 HIGH-1: 미확정 주문이 있는 마켓은 신규 주문·청산을 막고, 확정되면 계좌에 반영한다."""
    h = Harness(make_settings, actions={T0 + timedelta(hours=10): "BUY"})
    broker = ResolvingPaperBroker(h.portfolio, slippage_rate=0.001)
    h.engine.broker = broker
    h.engine.pending_max_age = 24 * 3600  # 이 테스트는 시계를 1시간 넘게 돌리므로 포기 시한을 늘린다
    await h.engine.warmup()
    h.engine._record_order(make_unknown(h))
    assert MARKET in h.engine.pending_orders and h.engine.stats.orders_unknown == 1
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)  # BUY 신호가 나오지만 미확정 주문 때문에 건너뛴다
    assert not h.portfolio.has_position(MARKET)
    assert any(e.event == "pending_order_skip" for e in h.repo.recent_logs(10))
    # 아직 확정 안 됨 → 그대로 대기
    assert await h.engine.resolve_pending_orders(h.now) == 0 and MARKET in h.engine.pending_orders
    # 거래소에서 체결로 확정 → 계좌 반영, 대기 목록 제거, DB 상태 FILLED
    broker.resolutions.append("fill")
    assert await h.engine.resolve_pending_orders(h.now) == 1
    assert MARKET not in h.engine.pending_orders and h.portfolio.has_position(MARKET)
    assert h.repo.recent_orders(1)[0].status == "FILLED" and h.engine.stats.orders_resolved == 1
    assert any(e.event == "order_resolved" for e in h.repo.recent_logs(10))


async def test_pending_order_gives_up_after_max_age(make_settings) -> None:
    h = Harness(make_settings)
    h.engine.broker = ResolvingPaperBroker(h.portfolio, slippage_rate=0.001)
    h.engine.pending_max_age = 100
    h.engine._record_order(make_unknown(h))
    assert await h.engine.resolve_pending_orders(h.now + timedelta(seconds=50)) == 0
    assert MARKET in h.engine.pending_orders
    assert await h.engine.resolve_pending_orders(h.now + timedelta(seconds=101)) == 0
    assert MARKET not in h.engine.pending_orders
    assert any(e.event == "order_unresolved" for e in h.repo.recent_logs(10))
    assert "확정 실패" in h.repo.recent_orders(1)[0].error


async def test_load_pending_orders_from_db(make_settings) -> None:
    h = Harness(make_settings)
    h.repo.save_order(make_unknown(h, exchange_order_id=None, exchange_identifier="c9"))
    assert h.engine.load_pending_orders() == 1
    assert h.engine.pending_orders[MARKET].exchange_identifier == "c9"
    assert h.engine.load_pending_orders() == 1 and len(h.engine.pending_orders) == 1  # 중복 적재 없음


async def test_exit_retry_backoff_prevents_storm(make_settings) -> None:
    """청산 주문이 거부되면 마켓별 백오프(5초→10초→…)로 1초마다 재시도하지 않는다."""
    from app.trading.orders import Order, OrderStatus, OrderType

    h = Harness(make_settings, actions={T0 + timedelta(hours=10): "BUY"})
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)
    assert h.portfolio.has_position(MARKET)

    class RejectingBroker(PaperBroker):
        calls = 0

        async def execute(self, request, price, now):
            RejectingBroker.calls += 1
            return Order(
                id=f"rej-{self.calls}", client_id=request.resolved_client_id(self.mode), mode="paper",
                market=request.market, side=request.side, order_type=OrderType.MARKET, amount=None,
                quantity=request.quantity, status=OrderStatus.REJECTED, created_at=now, error="거래소 거부",
            )

    h.engine.broker = RejectingBroker(h.portfolio, slippage_rate=0.001)
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=103.0, bid=102.0)  # 손절선(-5%) 아래
    await h.engine.check_exits(h.now, force=True)
    assert RejectingBroker.calls == 1 and h.engine._exit_failures[MARKET] == 1
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=103.0, bid=102.0)
    await h.engine.check_exits(h.now, force=True)
    assert RejectingBroker.calls == 1  # 5초 백오프 안 → 재시도 없음
    h.now += timedelta(seconds=4)
    h.feed_prices(ask=103.0, bid=102.0)
    await h.engine.check_exits(h.now, force=True)
    assert RejectingBroker.calls == 2 and h.engine._exit_failures[MARKET] == 2  # 다음 백오프 10초


class ScriptedWS:
    """감사 HIGH-2 용 가짜 WebSocket: 메시지 목록을 내보낸 뒤 끝나거나(None) 예외를 낸다."""

    def __init__(self, harness: Harness, books: list[tuple[float, float]], *, error: Exception | None = None) -> None:
        self.h = harness
        self.books = books
        self.error = error
        self.state = type("S", (), {"value": "CONNECTED"})()
        self.closed = False

    async def stream(self):
        for ask, bid in self.books:
            book = {**ORDERBOOK_JSON, "code": MARKET, "timestamp": int(self.h.now.timestamp() * 1000),
                    "orderbook_units": [{"ask_price": ask, "bid_price": bid, "ask_size": 1, "bid_size": 1}]}
            self.h.now += timedelta(seconds=2)
            yield parse_ws_message(json.dumps(book))
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        self.closed = True


class FlakyRepo:
    """save_order 만 SQLite 잠금 오류를 내는 저장소 (감사 HIGH-2 재현 스크립트와 같은 조건)."""

    def __init__(self, real) -> None:
        self.real = real
        self.failures = 0

    def __getattr__(self, name):
        if name in ("save_order", "record_fill"):
            def boom(*_a, **_k):
                self.failures += 1
                raise OperationalError("INSERT INTO orders", {}, Exception("database is locked"))
            return boom
        return getattr(self.real, name)


def stopping_sleep(h: Harness, sleeps: list[float], stop_after: int):
    """백오프 sleep 을 기록하고 n번째에 엔진을 정지시켜 루프를 끝낸다."""

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= stop_after:
            h.engine._stop.set()

    return _sleep


async def test_price_loop_survives_exit_check_exception(make_settings) -> None:
    """감사 HIGH-2: check_exits 안의 DB 잠금 예외가 시세 루프를 끝내지 않는다."""
    h = Harness(make_settings)
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.portfolio.buy(MARKET, 100.0, time=h.now, amount=100_000)  # 손절선 95
    real_repo = h.engine.repo
    h.engine.repo = FlakyRepo(real_repo)
    ws = ScriptedWS(h, [(97.0, 96.0), (93.0, 92.0), (91.0, 90.0)])
    h.engine.ws_factory = lambda subs: ws
    sleeps: list[float] = []
    h.engine.sleep = stopping_sleep(h, sleeps, 1)
    await asyncio.wait_for(h.engine._price_loop(), timeout=3)
    assert h.engine.stats.price_updates == 3  # 예외 뒤에도 남은 메시지를 계속 처리했다
    assert h.engine.repo.failures >= 1 and h.engine.stats.errors >= 1
    events = [e.event for e in real_repo.recent_logs(10)]
    assert "exit_check_failed" in events and "price_stream_failed" not in events
    assert sleeps == [1.0] and ws.closed  # 스트림이 끝나면 백오프 후 재연결하려 했다


async def test_price_loop_reconnects_after_stream_error(make_settings) -> None:
    """스트림 예외 → price_stream_failed 기록 후 백오프 재연결, 다시 수신되면 백오프 초기화."""
    h = Harness(make_settings)
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    streams = [ScriptedWS(h, [(101.0, 100.0)], error=RuntimeError("ws down")), ScriptedWS(h, [(102.0, 101.0)])]
    created: list[ScriptedWS] = []

    def factory(subs):
        ws = streams.pop(0)
        created.append(ws)
        return ws

    h.engine.ws_factory = factory
    sleeps: list[float] = []
    h.engine.sleep = stopping_sleep(h, sleeps, 2)
    await asyncio.wait_for(h.engine._price_loop(), timeout=3)
    assert len(created) == 2 and h.engine.stats.price_stream_restarts == 1
    assert h.engine.stats.price_updates == 2 and all(ws.closed for ws in created)
    assert sleeps == [1.0, 1.0]  # 오류 뒤 1초, 정상 수신 후 스트림 종료 → 백오프 초기화된 1초
    assert any(e.event == "price_stream_failed" for e in h.repo.recent_logs(10))


async def test_price_staleness_alert_once_until_recovered(make_settings) -> None:
    h = Harness(make_settings)
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    assert h.engine._check_price_staleness(h.now) is False  # 포지션 없음 → 알림 없음
    h.feed_prices(ask=101.0, bid=100.0)
    h.portfolio.buy(MARKET, 100.0, time=h.now, amount=100_000)
    assert h.engine._check_price_staleness(h.now + timedelta(seconds=30)) is False
    assert h.engine._check_price_staleness(h.now + timedelta(seconds=200)) is True
    assert h.engine._check_price_staleness(h.now + timedelta(seconds=260)) is True  # 반복 알림은 없음
    assert sum(1 for e in h.repo.recent_logs(10) if e.event == "price_stale") == 1
    h.now += timedelta(seconds=300)
    h.feed_prices(ask=101.0, bid=100.0)  # 시세 복구 → 리셋
    assert h.engine._check_price_staleness(h.now) is False and h.engine._price_stale_alerted is False


class FailingRepo:
    """지정한 메서드가 처음 n번 SQLite 잠금 오류를 내는 저장소 (감사 HIGH-6)."""

    def __init__(self, real, method: str, fail_times: int) -> None:
        self.real = real
        self.method = method
        self.fail_times = fail_times
        self.failures = 0

    def __getattr__(self, name):
        attr = getattr(self.real, name)
        if name != self.method:
            return attr

        def wrapped(*a, **k):
            if self.failures < self.fail_times:
                self.failures += 1
                raise OperationalError("SELECT", {}, Exception("database is locked"))
            return attr(*a, **k)

        return wrapped


def realtime_harness(make_settings) -> Harness:
    h = Harness(make_settings)
    h.engine.clock = lambda: datetime.now(UTC)
    h.engine.command_poll_interval = 0.05
    h.engine.heartbeat_interval = 0.05
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    h.client.candles = [make_candle(base - timedelta(hours=i), 100.0) for i in range(1, 12)]
    return h


async def test_run_survives_db_errors_in_loop_steps(make_settings) -> None:
    """감사 HIGH-6: 명령 폴링·하트비트의 OperationalError 가 run() 을 끝내지 않는다."""
    h = realtime_harness(make_settings)
    real_repo = h.engine.repo
    h.engine.repo = FailingRepo(real_repo, "pending_commands", fail_times=3)
    h.engine.loop_error_backoff = 0.0

    async def stop_soon() -> None:
        await asyncio.sleep(0.6)
        real_repo.enqueue_command("stop")

    stopper = asyncio.create_task(stop_soon())
    stats = await h.engine.run(duration_seconds=5)
    await stopper
    assert h.engine.status == "stopped" and h.engine.repo.failures == 3
    assert stats.loop_step_failures == 3 and stats.errors >= 3 and h.engine.safe_mode is False
    events = [e.event for e in real_repo.recent_logs(30)]
    assert events.count("loop_step_failed") == 3 and "bot_stop" in events
    assert real_repo.read_engine_status().status == "STOPPED"


async def test_heartbeat_db_failure_does_not_abort_start(make_settings) -> None:
    h = realtime_harness(make_settings)
    real_repo = h.engine.repo
    h.engine.repo = FailingRepo(real_repo, "write_engine_status", fail_times=2)  # 시작 직후 하트비트 2번 실패

    async def stop_soon() -> None:
        await asyncio.sleep(0.3)
        real_repo.enqueue_command("stop")

    stopper = asyncio.create_task(stop_soon())
    stats = await h.engine.run(duration_seconds=5)
    await stopper
    assert h.engine.status == "stopped" and stats.errors >= 2 and h.engine.repo.failures == 2
    assert real_repo.read_engine_status().status == "STOPPED"  # 나중 하트비트는 정상 기록


async def test_repeated_failures_enter_safe_mode(make_settings) -> None:
    """연속 실패가 한도를 넘으면 안전 모드: 매수 신호를 무시하고 청산 감시는 계속한다."""
    h = Harness(make_settings, actions={T0 + timedelta(hours=10): "BUY"})
    h.engine.safe_mode_after_failures = 3
    h.engine.loop_error_backoff = 0.0
    for _ in range(3):
        assert await h.engine._guarded("테스트", lambda: (_ for _ in ()).throw(RuntimeError("boom"))) is False
    assert h.engine.safe_mode is True and h.engine.stats.loop_step_failures == 3
    assert any(e.event == "safe_mode" for e in h.repo.recent_logs(10))
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)
    assert not h.portfolio.has_position(MARKET)  # 안전 모드에서는 매수하지 않는다
    assert any(e.event == "safe_mode_skip" for e in h.repo.recent_logs(10))
    assert h.engine.stats_dict()["safe_mode"] is True


def test_live_mode_requires_flags_keys_and_live_broker(make_settings) -> None:
    portfolio = Portfolio(100_000)
    client = FakeClient([])
    strategy = TimedStrategy({})
    db = Database("sqlite://")
    db.create_all()
    paper_repo = Repository(db, "paper")
    live_repo = Repository(db, "live")

    with pytest.raises(ConfigError, match="LIVE_TRADING_ENABLED"):
        TradingEngine(make_settings(trading_mode="LIVE"), strategy=strategy, portfolio=portfolio,
                      broker=PaperBroker(portfolio), risk=RiskManager(), repo=paper_repo, client=client)
    armed = make_settings(
        trading_mode="LIVE", live_trading_enabled=True, upbit_access_key="a" * 20, upbit_secret_key="b" * 40
    )
    with pytest.raises(ConfigError, match="LiveBroker"):
        TradingEngine(armed, strategy=strategy, portfolio=portfolio, broker=PaperBroker(portfolio),
                      risk=RiskManager(), repo=live_repo, client=client)
    no_keys = make_settings(trading_mode="LIVE", live_trading_enabled=True)
    with pytest.raises(ConfigError, match="KEY"):
        TradingEngine(no_keys, strategy=strategy, portfolio=portfolio, broker=LiveBroker(client, portfolio, no_keys),
                      risk=RiskManager(), repo=live_repo, client=client)
    with pytest.raises(ConfigError, match="모드"):
        TradingEngine(armed, strategy=strategy, portfolio=portfolio, broker=LiveBroker(client, portfolio, armed),
                      risk=RiskManager(), repo=paper_repo, client=client)
    engine = TradingEngine(armed, strategy=strategy, portfolio=portfolio, broker=LiveBroker(client, portfolio, armed),
                           risk=RiskManager(), repo=live_repo, client=client)
    assert engine.mode == "live"
    with pytest.raises(ConfigError, match="PaperBroker"):
        TradingEngine(make_settings(), strategy=strategy, portfolio=portfolio,
                      broker=LiveBroker(client, portfolio, armed), risk=RiskManager(), repo=paper_repo, client=client)
