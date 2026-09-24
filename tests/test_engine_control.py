"""엔진 제어 이음새 테스트 — 일시정지, 명령 큐, 하트비트, LIVE 모드 안전장치."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import ConfigError
from app.database import Database, Repository
from app.risk import RiskManager
from app.trading.engine import TradingEngine
from app.trading.live_broker import LiveBroker
from app.trading.orders import PaperBroker
from app.trading.portfolio import Portfolio
from tests.test_engine import MARKET, T0, FakeClient, Harness, TimedStrategy


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
