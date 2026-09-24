"""리스크 관리 통합 테스트 — 모의매매 엔진의 실시간 청산 감시와 백테스트의 일일 손실 한도."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.backtest import BacktestConfig, BacktestEngine
from app.risk import RiskConfig
from tests.test_backtest import Scripted, ohlc_frame
from tests.test_engine import MARKET, T0, Harness


async def test_engine_stop_loss_exit_from_price_updates(make_settings) -> None:
    buy_t = T0 + timedelta(hours=10)
    h = Harness(make_settings, actions={buy_t: "BUY"})
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)
    pos = h.portfolio.position(MARKET)
    assert pos is not None
    stop_price = pos.avg_price * 0.95

    # 시세가 손절가 위 → 청산 없음
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=106.0, bid=105.0)
    assert await h.engine.check_exits(h.now, force=True) == []
    assert h.portfolio.has_position(MARKET)

    # 시세가 손절가 아래 → 손절 매도 → DB 기록 → 연속 손실 1
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=103.0, bid=102.0)
    assert (103.0 + 102.0) / 2 < stop_price
    orders = await h.engine.check_exits(h.now, force=True)
    assert len(orders) == 1 and orders[0].is_filled
    assert orders[0].reason.startswith("stop_loss")
    assert orders[0].fill_price == pytest.approx(102.0 * (1 - 0.001))
    assert not h.portfolio.has_position(MARKET)
    rt = h.repo.recent_round_trips(1)[0]
    assert rt.exit_reason == "stop_loss" and rt.pnl < 0
    assert h.engine.risk.state.consecutive_losses == 1
    assert h.engine.stats.exits_triggered == 1
    assert h.repo.load_positions() == []

    # 1초 안의 반복 호출은 건너뛴다 (force 없이)
    assert await h.engine.check_exits(h.now) == []

    # 재시작 → 오늘 거래로 리스크 상태 복구
    h2 = Harness(make_settings, actions={}, db=h.db)
    await h2.engine.warmup()
    h2.now = h.now
    h2.engine.rebuild_risk_state(h2.now, h2.portfolio.cash)
    assert h2.engine.risk.state.consecutive_losses == 1
    assert h2.engine.risk.state.daily_realized_pnl == pytest.approx(rt.pnl)


async def test_engine_daily_loss_lock_blocks_new_entry(make_settings) -> None:
    buy_t, later_t = T0 + timedelta(hours=10), T0 + timedelta(hours=11)
    h = Harness(make_settings, actions={buy_t: "BUY", later_t: "BUY"})
    h.engine.risk.config = RiskConfig.unrestricted(position_fraction=0.5, daily_loss_limit_pct=0.02)
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)
    assert h.portfolio.has_position(MARKET)

    # 평가액이 -3% → 일일 손실 잠금
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=100.0, bid=99.0)
    await h.engine.check_exits(h.now, force=True)
    assert h.engine.risk.state.lock_reason and "일일 손실" in h.engine.risk.state.lock_reason
    assert h.engine.stats.risk_locks == 1
    assert any(entry.event == "risk_lock" for entry in h.repo.recent_logs(10))

    # 청산 후 같은 날 BUY 신호 → 차단
    h.portfolio.sell(MARKET, 100.0, time=h.now)
    h.add_candle(12, 100.0)
    h.now = T0 + timedelta(hours=12, seconds=5)
    h.feed_prices(ask=100.0, bid=99.0)
    signals = await h.engine.process_closed_candles(h.now)
    assert [s.action.value for s in signals] == ["BUY"]
    assert h.engine.stats.risk_rejections == 1
    assert not h.portfolio.has_position(MARKET)


def test_backtest_daily_loss_limit_blocks_same_day_entries() -> None:
    # 시각 인덱스 0 = 2026-01-01 00:00 UTC (KST 09:00). 인덱스 15 부터 KST 다음 날.
    closes = [100.0] * 4 + [90.0] * 44
    opens = [100.0] + closes[:-1]
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) * 0.99 for o, c in zip(opens, closes, strict=True)]
    df = ohlc_frame(opens, highs, lows, closes)
    cfg = BacktestConfig(
        check_lookahead=False, slippage_rate=0.001,
        risk=RiskConfig.unrestricted(daily_loss_limit_pct=0.03),
    )
    result = BacktestEngine(cfg).run(df, Scripted({1: "BUY", 5: "SELL", 7: "BUY", 30: "BUY"}))
    reasons = [t.exit_reason for t in result.trades]
    assert reasons == ["signal", "end_of_data"]
    assert result.trades[1].entry_time == df.index[31].to_pydatetime()  # 다음 날 진입은 허용
    assert sum(result.risk_rejections.values()) == 1
    assert any("신규 진입 차단" in key for key in result.risk_rejections)
    assert result.risk_config.daily_loss_limit_pct == 0.03
    assert "일일 손실 한도 3%" in " ".join(f"{k} {v}" for k, v in result.risk_config.describe().items())
