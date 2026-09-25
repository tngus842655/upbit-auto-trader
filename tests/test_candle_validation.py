"""감사 MEDIUM-13 회귀 — 깨진 캔들 응답은 무시하고, 급등락(이상치) 캔들의 신호는 실행하지 않고 기록·알림만 한다."""

from __future__ import annotations

from datetime import timedelta

from tests.test_engine import MARKET, T0, Harness
from tests.test_strategy_data import make_candle


async def test_invalid_candle_batch_is_ignored_and_logged(make_settings) -> None:
    h = Harness(make_settings, actions={T0 + timedelta(hours=12): "BUY"})
    await h.engine.warmup()
    before = len(h.engine.state.frame(MARKET))
    bad = make_candle(T0 + timedelta(hours=12), 112.0).model_copy(update={"high_price": 10.0})  # 고가 < 저가
    h.client.candles.append(bad)
    h.now = T0 + timedelta(hours=13, seconds=5)
    h.feed_prices(ask=113.0, bid=112.0)
    signals = await h.engine.process_closed_candles(h.now)
    assert signals == [] and not h.portfolio.has_position(MARKET)
    assert len(h.engine.state.frame(MARKET)) == before  # 깨진 응답은 지표에 들어가지 않는다
    assert h.engine.stats.invalid_candle_batches == 1 and h.engine.status != "stopped"
    assert any(e.event == "candle_invalid" for e in h.repo.recent_logs(5))


async def test_anomalous_candle_signal_is_held(make_settings) -> None:
    """직전 종가 111 → 170(+53%) 캔들의 BUY 신호는 기록만 하고 주문하지 않는다."""
    h = Harness(make_settings, actions={T0 + timedelta(hours=12): "BUY"})
    await h.engine.warmup()
    h.add_candle(12, 170.0)
    h.now = T0 + timedelta(hours=13, seconds=5)
    h.feed_prices(ask=171.0, bid=170.0)
    signals = await h.engine.process_closed_candles(h.now)
    assert [s.action.value for s in signals][-1] == "BUY" and not h.portfolio.has_position(MARKET)
    assert h.engine.stats.anomalous_signals_held == 1 and h.engine.stats.orders_filled == 0
    held = [e for e in h.repo.recent_logs(5) if e.event == "candle_anomaly"]
    assert len(held) == 1 and "+53" in held[0].message
    assert await h.engine.process_closed_candles(h.now) == []  # 보류한 신호도 처리된 것으로 기록돼 다시 나오지 않는다


async def test_normal_candle_signal_still_executes(make_settings) -> None:
    h = Harness(make_settings, actions={T0 + timedelta(hours=12): "BUY"})
    await h.engine.warmup()
    h.add_candle(12, 112.0)  # +0.9%
    h.now = T0 + timedelta(hours=13, seconds=5)
    h.feed_prices(ask=113.0, bid=112.0)
    await h.engine.process_closed_candles(h.now)
    assert h.portfolio.has_position(MARKET) and h.engine.stats.anomalous_signals_held == 0
