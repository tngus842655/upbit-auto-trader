"""백테스트 엔진·지표·보고서 테스트."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.backtest import (
    EXIT_END_OF_DATA,
    EXIT_SIGNAL,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    BacktestConfig,
    BacktestEngine,
    format_report,
    max_drawdown,
    save_result,
)
from app.backtest.metrics import cagr, compute_metrics, max_consecutive_losses, sharpe_ratio
from app.core.exceptions import StrategyError
from app.strategy import MovingAverageCrossStrategy, Strategy
from app.strategy.base import ACTION_COLUMN, REASON_COLUMN, StrategyParams
from app.trading.portfolio import Trade, floor_quantity
from tests.test_strategy import make_frame, random_frame

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Scripted(Strategy):
    """지정한 인덱스에서 정해진 행동을 내는 전략 (엔진 테스트용)."""

    name = "scripted"
    Params = StrategyParams

    def __init__(self, actions: dict[int, str]) -> None:
        super().__init__()
        self._actions = actions

    @property
    def warmup_periods(self) -> int:
        return 1

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df[["close"]].copy()
        acts = ["HOLD"] * len(df)
        for i, a in self._actions.items():
            if i < len(df):
                acts[i] = a
        out[ACTION_COLUMN] = acts
        out[REASON_COLUMN] = ""
        return out


def ohlc_frame(opens, highs, lows, closes) -> pd.DataFrame:
    n = len(opens)
    idx = pd.DatetimeIndex([T0 + timedelta(hours=i) for i in range(n)], name="time")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1.0] * n, "value": [1.0] * n},
        index=idx, dtype="float64",
    )
    df.attrs["market"] = "KRW-TEST"
    df.attrs["interval"] = "60m"
    return df


def flat_frame(n: int = 10, price: float = 100.0) -> pd.DataFrame:
    return ohlc_frame([price] * n, [price * 1.01] * n, [price * 0.99] * n, [price] * n)


CFG = BacktestConfig(initial_capital=1_000_000, fee_rate=0.0005, slippage_rate=0.001, check_lookahead=False)


class TestEngineFills:
    def test_signal_fills_at_next_open_with_slippage(self) -> None:
        opens = [100, 100, 100, 100, 104, 106, 110, 108, 108, 108]
        df = ohlc_frame(opens, [o * 1.02 for o in opens], [o * 0.98 for o in opens], [o * 1.01 for o in opens])
        result = BacktestEngine(CFG).run(df, Scripted({2: "BUY", 5: "SELL"}))

        assert len(result.trades) == 1
        t = result.trades[0]
        buy_price = 100 * 1.001
        sell_price = 110 * (1 - 0.001)
        qty = floor_quantity(1_000_000 / (buy_price * 1.0005))
        assert t.entry_time == df.index[3].to_pydatetime()
        assert t.entry_price == pytest.approx(buy_price)
        assert t.quantity == pytest.approx(qty)
        assert t.exit_time == df.index[6].to_pydatetime()
        assert t.exit_price == pytest.approx(sell_price)
        assert t.exit_reason == EXIT_SIGNAL
        entry_cost = qty * buy_price
        exit_amount = qty * sell_price
        assert t.pnl == pytest.approx(exit_amount * (1 - 0.0005) - entry_cost * (1 + 0.0005))

        assert result.in_position.tolist() == [False, False, False, True, True, True, False, False, False, False]
        cash_after_buy = 1_000_000 - entry_cost * 1.0005
        assert result.equity.iloc[3] == pytest.approx(cash_after_buy + qty * df["close"].iloc[3])
        assert result.equity.iloc[2] == pytest.approx(1_000_000)
        assert result.equity.iloc[-1] == pytest.approx(result.metrics.final_equity)
        assert result.metrics.total_trades == 1 and result.metrics.total_fees == pytest.approx(t.entry_fee + t.exit_fee)

    def test_ignored_signals_are_counted(self) -> None:
        df = flat_frame(8)
        result = BacktestEngine(CFG).run(df, Scripted({0: "SELL", 1: "BUY", 3: "BUY", 4: "SELL", 6: "SELL"}))
        assert result.ignored_sell_signals == 2  # index 0 (미보유), index 6 (미보유)
        assert result.ignored_buy_signals == 1  # index 3 (보유 중)
        assert len(result.trades) == 1

    def test_end_of_data_liquidation(self) -> None:
        df = flat_frame(6)
        result = BacktestEngine(CFG).run(df, Scripted({2: "BUY"}))
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == EXIT_END_OF_DATA
        assert result.trades[0].exit_time == df.index[-1].to_pydatetime()
        assert result.in_position.iloc[-1] is np.False_ or not result.in_position.iloc[-1]
        assert result.equity.iloc[-1] == pytest.approx(result.metrics.final_equity)

    def test_unfilled_signal_at_last_candle(self) -> None:
        df = flat_frame(5)
        result = BacktestEngine(CFG).run(df, Scripted({4: "BUY"}))
        assert result.trades == []
        assert result.unfilled_signal_at_end == "BUY"

    def test_no_signals_flat_equity(self) -> None:
        df = flat_frame(20)
        result = BacktestEngine(CFG).run(df, Scripted({}))
        assert result.trades == []
        assert (result.equity == 1_000_000).all()
        assert result.metrics.total_return == 0.0
        assert math.isnan(result.metrics.win_rate)
        assert result.metrics.mdd == 0.0
        assert result.benchmark_metrics.total_trades == 1

    def test_position_fraction(self) -> None:
        df = flat_frame(6)
        cfg = CFG.model_copy(update={"position_fraction": 0.5})
        result = BacktestEngine(cfg).run(df, Scripted({1: "BUY"}))
        t = result.trades[0]
        assert t.entry_amount + t.entry_fee <= 500_000 + 1e-6
        assert t.entry_amount > 499_000

    def test_rejected_when_capital_below_min_order(self) -> None:
        df = flat_frame(6)
        cfg = CFG.model_copy(update={"initial_capital": 3_000.0})
        result = BacktestEngine(cfg).run(df, Scripted({1: "BUY"}))
        assert result.trades == []
        assert result.rejected_orders == []
        assert sum(result.risk_rejections.values()) == 1 and any("최소 주문" in k for k in result.risk_rejections)


class TestStopsAndTargets:
    def test_stop_loss_uses_low_and_gap(self) -> None:
        opens = [100, 100, 100, 100, 100, 100]
        lows = [99, 99, 99, 99, 90, 99]  # 4번째 캔들에서 -10%
        df = ohlc_frame(opens, [101] * 6, lows, [100] * 6)
        cfg = CFG.model_copy(update={"stop_loss_pct": 0.05})
        result = BacktestEngine(cfg).run(df, Scripted({1: "BUY"}))
        t = result.trades[0]
        assert t.exit_reason == EXIT_STOP_LOSS
        assert t.exit_time == df.index[4].to_pydatetime()
        stop_price = t.entry_price * 0.95
        assert t.exit_price == pytest.approx(min(100, stop_price) * (1 - 0.001))
        assert t.pnl < 0

        # 갭 하락: 시가가 손절가보다 낮으면 시가 체결
        opens2 = [100, 100, 100, 100, 80, 100]
        df2 = ohlc_frame(opens2, [101, 101, 101, 101, 81, 101], [99, 99, 99, 99, 79, 99], [100, 100, 100, 100, 80, 100])
        result2 = BacktestEngine(cfg).run(df2, Scripted({1: "BUY"}))
        assert result2.trades[0].exit_price == pytest.approx(80 * (1 - 0.001))

    def test_take_profit_uses_high_without_slippage(self) -> None:
        df = ohlc_frame([100] * 6, [101, 101, 101, 101, 120, 101], [99] * 6, [100] * 6)
        cfg = CFG.model_copy(update={"take_profit_pct": 0.10})
        result = BacktestEngine(cfg).run(df, Scripted({1: "BUY"}))
        t = result.trades[0]
        assert t.exit_reason == EXIT_TAKE_PROFIT
        assert t.exit_price == pytest.approx(t.entry_price * 1.10)
        assert t.pnl > 0

    def test_stop_has_priority_over_target_in_same_candle(self) -> None:
        df = ohlc_frame([100] * 6, [101, 101, 101, 101, 130, 101], [99, 99, 99, 99, 80, 99], [100] * 6)
        cfg = CFG.model_copy(update={"stop_loss_pct": 0.05, "take_profit_pct": 0.10})
        result = BacktestEngine(cfg).run(df, Scripted({1: "BUY"}))
        assert result.trades[0].exit_reason == EXIT_STOP_LOSS

    def test_same_candle_as_entry_can_stop_out(self) -> None:
        df = ohlc_frame([100] * 5, [101] * 5, [99, 99, 80, 99, 99], [100] * 5)
        cfg = CFG.model_copy(update={"stop_loss_pct": 0.05})
        result = BacktestEngine(cfg).run(df, Scripted({1: "BUY"}))
        assert result.trades[0].exit_time == df.index[2].to_pydatetime()


class TestBenchmarkAndValidation:
    def test_buy_and_hold_matches_price_change(self) -> None:
        n = 30
        closes = [100 + i for i in range(n)]
        opens = [100] + closes[:-1]
        df = ohlc_frame(opens, [c + 1 for c in closes], [o - 1 for o in opens], closes)
        result = BacktestEngine(CFG).run(df, Scripted({}))
        b = result.benchmark_metrics
        gross = (closes[-1] * (1 - 0.001) * (1 - 0.0005)) / (opens[0] * (1 + 0.001) * (1 + 0.0005))
        assert b.total_return == pytest.approx(gross - 1, abs=2e-3)
        assert b.total_trades == 1 and b.exposure == 1.0
        assert result.benchmark_equity.iloc[0] < result.benchmark_equity.iloc[-1]

    def test_lookahead_check_blocks_cheating_strategy(self) -> None:
        class Cheater(Strategy):
            name = "cheater"
            Params = StrategyParams

            @property
            def warmup_periods(self) -> int:
                return 2

            def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
                out = df[["close"]].copy()
                out[ACTION_COLUMN] = np.where(df["close"].shift(-1) > df["close"], "BUY", "SELL")
                out[REASON_COLUMN] = ""
                return out

        with pytest.raises(StrategyError, match="Look-ahead"):
            BacktestEngine(BacktestConfig()).run(random_frame(120), Cheater())

    def test_config_validation(self) -> None:
        with pytest.raises(ValueError):
            BacktestConfig(stop_loss_pct=1.5)
        with pytest.raises(ValueError):
            BacktestConfig(position_fraction=0)
        with pytest.raises(ValueError):
            BacktestConfig(unknown=1)

    def test_real_strategy_end_to_end_is_deterministic(self) -> None:
        df = random_frame(500, seed=11)
        strategy = MovingAverageCrossStrategy(
            {"short_window": 5, "long_window": 20, "volume_window": 0, "rsi_window": 0}
        )
        engine = BacktestEngine(BacktestConfig(stop_loss_pct=0.05))
        r1 = engine.run(df, strategy)
        r2 = engine.run(df, strategy)
        assert len(r1.equity) == 500
        assert r1.metrics.to_dict() == r2.metrics.to_dict()
        assert r1.metrics.total_trades > 0
        assert r1.metrics.exposure == pytest.approx(r1.in_position.mean())
        assert set(r1.exit_reason_counts()) <= {EXIT_SIGNAL, EXIT_STOP_LOSS, EXIT_END_OF_DATA}


class TestMetrics:
    def test_max_drawdown(self) -> None:
        idx = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
        eq = pd.Series([100, 120, 90, 130, 65], index=idx, dtype="float64")
        mdd, peak, trough = max_drawdown(eq)
        assert mdd == pytest.approx(65 / 130 - 1)
        assert peak == idx[3].to_pydatetime() and trough == idx[4].to_pydatetime()
        assert max_drawdown(pd.Series([1, 2, 3], index=idx[:3], dtype="float64")) == (0.0, None, None)

    def test_cagr_and_sharpe(self) -> None:
        assert cagr(100, 200, 365.25 * 86_400) == pytest.approx(1.0)
        assert cagr(100, 0, 100) == -1.0
        assert math.isnan(cagr(100, 200, 0))
        assert sharpe_ratio(pd.Series([0.01, 0.01, 0.01]), 365) == 0.0
        assert sharpe_ratio(pd.Series([0.01, -0.01, 0.02]), 365) > 0
        assert math.isnan(sharpe_ratio(pd.Series([0.01]), 365))

    def test_trade_statistics(self) -> None:
        def trade(pnl: float, pct: float) -> Trade:
            return Trade("KRW-TEST", T0, 100, 1, 100, 0.05, T0 + timedelta(hours=1), 100 + pnl, 100 + pnl, 0.05,
                         "signal", pnl, pct)

        trades = [trade(10, 0.1), trade(-5, -0.05), trade(-5, -0.05), trade(20, 0.2), trade(-1, -0.01)]
        assert max_consecutive_losses(trades) == 2
        idx = pd.date_range("2026-01-01", periods=4, freq="h", tz="UTC")
        equity = pd.Series([100, 110, 105, 120], index=idx, dtype="float64")
        m = compute_metrics(equity, trades, initial_capital=100, interval_seconds=3600, total_fees=0.5)
        assert m.total_trades == 5 and m.wins == 2 and m.losses == 3
        assert m.win_rate == pytest.approx(0.4)
        assert m.avg_win == pytest.approx(15) and m.avg_loss == pytest.approx(-11 / 3)
        assert m.profit_factor == pytest.approx(30 / 11)
        assert m.expectancy == pytest.approx(19 / 5)
        assert m.total_return == pytest.approx(0.2)
        assert m.total_fees == 0.5
        assert m.duration_days == pytest.approx(4 / 24)
        d = m.to_dict()
        assert d["mdd_peak_time"] is None or isinstance(d["mdd_peak_time"], str)
        json.dumps(d)


class TestReport:
    def test_format_and_save(self, tmp_path) -> None:
        df = random_frame(300, seed=3)
        strategy = MovingAverageCrossStrategy(
            {"short_window": 5, "long_window": 20, "volume_window": 0, "rsi_window": 0}
        )
        result = BacktestEngine(BacktestConfig(check_lookahead=False)).run(df, strategy)
        text = format_report(result, max_trades=3)
        assert "백테스트: ma_cross" in text and "Buy & Hold" in text and "총 수익률" in text and "미래 수익" in text
        out = save_result(result, tmp_path / "bt")
        for name in ("summary.json", "trades.csv", "equity.csv", "signals.csv"):
            assert (out / name).exists()
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert summary["strategy"] == "ma_cross" and summary["candles"] == 300
        assert summary["metrics"]["total_trades"] == len(result.trades)
        curve = pd.read_csv(out / "equity.csv")
        assert list(curve.columns) == ["time", "equity", "benchmark", "in_position"] and len(curve) == 300
        assert make_frame is not None  # 공용 헬퍼 import 확인
