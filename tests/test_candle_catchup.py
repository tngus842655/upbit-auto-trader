"""감사 HIGH-4 회귀 — 정체 뒤 한꺼번에 닫힌 캔들은 마지막 신호만 실행하고, 캔들 공백은 다시 받아 메운다."""

from __future__ import annotations

from datetime import timedelta

from tests.test_engine import MARKET, T0, Harness


async def test_stall_executes_only_last_candle_signal(make_settings) -> None:
    """3시간 정체 후 10·11·12시 캔들이 한꺼번에 닫힘: 10시 BUY·11시 SELL 은 기록만, 12시(HOLD)만 실행 대상."""
    h = Harness(make_settings, actions={T0 + timedelta(hours=10): "BUY", T0 + timedelta(hours=11): "SELL"})
    await h.engine.warmup()
    h.add_candle(12, 112.0)
    h.now = T0 + timedelta(hours=13, seconds=5)
    h.feed_prices(ask=150.0, bid=149.0)
    signals = await h.engine.process_closed_candles(h.now)
    assert [(s.time.hour, s.action.value) for s in signals] == [(10, "BUY"), (11, "SELL"), (12, "HOLD")]
    assert h.repo.recent_orders(5) == [] and not h.portfolio.has_position(MARKET) and h.portfolio.trades == []
    assert h.engine.stats.stale_signals_skipped == 2 and h.engine.stats.orders_filled == 0
    skipped = [e for e in h.repo.recent_logs(10) if e.event == "stale_signal_skipped"]
    assert len(skipped) == 2 and "마지막 캔들" in skipped[0].message
    assert await h.engine.process_closed_candles(h.now) == []  # 건너뛴 신호도 처리된 것으로 기록돼 다시 나오지 않는다


async def test_last_candle_signal_executes_when_fresh(make_settings) -> None:
    h = Harness(make_settings, actions={T0 + timedelta(hours=12): "BUY"})
    await h.engine.warmup()
    h.add_candle(12, 112.0)
    h.now = T0 + timedelta(hours=13, seconds=5)  # 12시 캔들이 방금 닫힘
    h.feed_prices(ask=113.0, bid=112.0)
    await h.engine.process_closed_candles(h.now)
    assert h.portfolio.has_position(MARKET) and h.engine.stats.stale_signals_skipped == 2  # 10·11시는 기록만


async def test_stale_last_candle_is_not_executed(make_settings) -> None:
    """마지막 캔들(12시 BUY)조차 현재보다 한 인터벌 넘게 오래됐으면(16시) 실행하지 않는다."""
    h = Harness(make_settings, actions={T0 + timedelta(hours=12): "BUY"})
    await h.engine.warmup()
    h.add_candle(12, 112.0)
    h.now = T0 + timedelta(hours=16, seconds=5)
    h.feed_prices(ask=113.0, bid=112.0)
    await h.engine.process_closed_candles(h.now)
    assert not h.portfolio.has_position(MARKET)
    assert any("한 인터벌 초과" in e.message for e in h.repo.recent_logs(10) if e.event == "stale_signal_skipped")


async def test_candle_gap_is_refilled(make_settings) -> None:
    """refresh 조회(최근 3개)로는 못 채우는 공백이 생기면 공백 시작부터 다시 받아 메운다."""
    h = Harness(make_settings)
    await h.engine.warmup()
    for hour in range(12, 21):
        h.add_candle(hour, 100.0 + hour)
    h.now = T0 + timedelta(hours=21, seconds=5)
    h.feed_prices(ask=121.0, bid=120.0)
    await h.engine.process_closed_candles(h.now)
    df = h.engine.state.frame(MARKET)
    assert list(df.index.hour)[-11:] == list(range(10, 21))  # 10·11시(원래 있던 캔들)와 12~20시가 모두 이어진다
    assert h.engine.state.find_gaps(MARKET) == []
    assert any(e.event == "candle_gap" for e in h.repo.recent_logs(20))
    assert max(count for _, count in h.client.candle_calls) >= 14  # 공백을 덮을 만큼 다시 조회
    assert h.engine.stats.stale_signals_skipped >= 1  # 메운 캔들의 신호는 기록만


async def test_heartbeat_api_ok_reflects_recent_api_errors(make_settings) -> None:
    """감사 LOW-1 — api_ok 는 '오류가 한 번도 없었거나 캔들 점검을 한 적 있음' 이 아니라 최근 5분간 API 오류 유무다."""
    from app.core.exceptions import UpbitNetworkError

    h = Harness(make_settings)
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=13, seconds=5)
    h.engine.write_heartbeat("RUNNING")
    assert h.repo.read_engine_status().api_ok is True and h.engine.api_ok is True

    async def broken(*_a, **_k):
        raise UpbitNetworkError("down")

    h.client.get_candles = broken  # type: ignore[method-assign]
    await h.engine.process_closed_candles(h.now)
    h.engine.write_heartbeat("RUNNING")
    # 조치 전: 캔들 점검 이력만 있으면 True 였다
    assert h.engine.api_ok is False and h.repo.read_engine_status().api_ok is False
    h.now += timedelta(seconds=301)  # 오류 없이 5분이 지나면 다시 정상
    h.engine.write_heartbeat("RUNNING")
    assert h.engine.api_ok is True and h.repo.read_engine_status().api_ok is True
