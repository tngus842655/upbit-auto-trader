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
