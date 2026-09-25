"""감사 MEDIUM-6 회귀 — 청산 조건은 연속 관측돼야 실행되고(단일 이상 틱 방어), 캔들 경계의 강제 점검은 즉시 실행된다."""

from __future__ import annotations

from datetime import timedelta

from tests.test_engine import MARKET, T0, Harness


async def _entered(make_settings) -> Harness:
    h = Harness(make_settings, actions={T0 + timedelta(hours=12): "BUY"})
    await h.engine.warmup()
    h.add_candle(12, 112.0)
    h.now = T0 + timedelta(hours=13, seconds=5)
    h.feed_prices(ask=113.0, bid=112.0)
    await h.engine.process_closed_candles(h.now)
    assert h.portfolio.has_position(MARKET) and h.engine.exit_confirm_ticks == 2
    return h


async def test_single_tick_below_stop_does_not_exit(make_settings) -> None:
    h = await _entered(make_settings)
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=103.0, bid=102.0)  # 손절선(-5%) 아래 — 1번째 관측
    assert await h.engine.check_exits(h.now) == [] and h.engine._exit_streak[MARKET][1] == 1
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=113.0, bid=112.0)  # 튄 틱이 돌아옴 → 관측 횟수 초기화
    assert await h.engine.check_exits(h.now) == [] and MARKET not in h.engine._exit_streak
    assert h.portfolio.has_position(MARKET) and h.engine.stats.exits_triggered == 0


async def test_two_consecutive_ticks_exit(make_settings) -> None:
    h = await _entered(make_settings)
    orders = []
    for _ in range(2):
        h.now += timedelta(seconds=2)
        h.feed_prices(ask=103.0, bid=102.0)
        orders = await h.engine.check_exits(h.now)
    assert len(orders) == 1 and orders[0].is_filled and not h.portfolio.has_position(MARKET)
    assert h.engine.stats.exits_triggered == 1 and MARKET not in h.engine._exit_streak


async def test_forced_boundary_check_exits_immediately(make_settings) -> None:
    h = await _entered(make_settings)
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=103.0, bid=102.0)
    orders = await h.engine.check_exits(h.now, force=True)
    assert len(orders) == 1 and not h.portfolio.has_position(MARKET)
