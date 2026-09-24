"""전략 테스트 — 합성 데이터로 신호 위치를 검증하고, 모든 등록 전략에 Look-ahead 검사를 돌린다."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.exceptions import MarketDataError, StrategyError
from app.strategy import (
    STRATEGIES,
    Action,
    MovingAverageCrossStrategy,
    RSIStrategy,
    Strategy,
    available_strategies,
    check_no_lookahead,
    create_strategy,
)
from app.strategy.base import ACTION_COLUMN, StrategyParams


def make_frame(close: list[float], volume: list[float] | None = None, start: datetime | None = None) -> pd.DataFrame:
    n = len(close)
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    idx = pd.DatetimeIndex([start + timedelta(hours=i) for i in range(n)], name="time")
    c = np.asarray(close, dtype="float64")
    df = pd.DataFrame(
        {"open": c, "high": c * 1.001, "low": c * 0.999, "close": c,
         "volume": np.asarray(volume if volume is not None else [1.0] * n, dtype="float64")},
        index=idx,
    )
    df["value"] = df["close"] * df["volume"]
    df.attrs["market"] = "KRW-TEST"
    df.attrs["interval"] = "60m"
    return df


def random_frame(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    return make_frame(list(close), list(rng.uniform(1, 5, n)))


class TestRegistry:
    def test_available_and_create(self) -> None:
        assert set(available_strategies()) == {"ma_cross", "rsi"}
        s = create_strategy("MA_CROSS", {"short_window": 5, "long_window": 10})
        assert isinstance(s, MovingAverageCrossStrategy)
        assert s.params.short_window == 5
        assert isinstance(create_strategy("rsi"), RSIStrategy)

    def test_unknown_strategy(self) -> None:
        with pytest.raises(StrategyError, match="알 수 없는 전략"):
            create_strategy("magic")

    @pytest.mark.parametrize(
        "params",
        [{"short_window": 20, "long_window": 20}, {"short_window": 30, "long_window": 10},
         {"typo_window": 5}, {"short_window": 1}],
    )
    def test_invalid_params(self, params) -> None:
        with pytest.raises(StrategyError):
            MovingAverageCrossStrategy(params)

    def test_rsi_params(self) -> None:
        with pytest.raises(StrategyError):
            RSIStrategy({"oversold": 70, "overbought": 30})


class TestMovingAverageCross:
    def test_golden_cross_emits_buy_once(self) -> None:
        # 20개 하락 후 상승 전환 → 단기(3) 가 장기(6) 를 한 번 상향 돌파
        close = [100 - i for i in range(20)] + [80 + i * 2 for i in range(20)]
        df = make_frame(close)
        s = MovingAverageCrossStrategy({"short_window": 3, "long_window": 6, "volume_window": 0, "rsi_window": 0})
        signals = s.scan(df)
        assert [x.action for x in signals] == [Action.BUY]
        buy = signals[0]
        assert buy.market == "KRW-TEST"
        assert buy.strategy == "ma_cross"
        assert "골든크로스" in buy.reason
        assert buy.indicators["sma_short"] > buy.indicators["sma_long"]
        # 교차 직전 캔들은 HOLD
        result = s.evaluate(df)
        i = list(result.index).index(pd.Timestamp(buy.time))
        assert result[ACTION_COLUMN].iloc[i - 1] == "HOLD"

    def test_dead_cross_emits_sell(self) -> None:
        close = [80 + i * 2 for i in range(20)] + [120 - i * 3 for i in range(20)]
        df = make_frame(close)
        s = MovingAverageCrossStrategy({"short_window": 3, "long_window": 6, "volume_window": 0, "rsi_window": 0})
        assert [x.action for x in s.scan(df)] == [Action.SELL]

    def test_volume_filter_blocks_buy(self) -> None:
        close = [100 - i for i in range(20)] + [80 + i * 2 for i in range(20)]
        volume = [10.0] * 40
        df = make_frame(close, volume)
        s = MovingAverageCrossStrategy(
            {"short_window": 3, "long_window": 6, "volume_window": 5, "volume_factor": 2.0, "rsi_window": 0}
        )
        signals = s.scan(df, include_hold=True)
        blocked = [x for x in signals if "거래량 부족" in x.reason]
        assert len(blocked) == 1 and blocked[0].action is Action.HOLD
        assert not any(x.action is Action.BUY for x in signals)

    def test_rsi_filter_blocks_buy(self) -> None:
        close = [100 - i for i in range(20)] + [80 + i * 2 for i in range(20)]
        df = make_frame(close)
        s = MovingAverageCrossStrategy(
            {"short_window": 3, "long_window": 6, "volume_window": 0, "rsi_window": 5, "rsi_max_for_buy": 50}
        )
        signals = s.scan(df, include_hold=True)
        assert any("RSI>50" in x.reason for x in signals)
        assert not any(x.action is Action.BUY for x in signals)

    def test_warmup_returns_hold(self) -> None:
        s = MovingAverageCrossStrategy({"short_window": 5, "long_window": 10, "volume_window": 0, "rsi_window": 0})
        df = make_frame([100.0] * 8)
        signal = s.generate_signal(df)
        assert signal.action is Action.HOLD
        assert signal.reason.startswith("warmup")
        assert signal.price == 100.0

    def test_generate_signal_equals_last_scan_row(self) -> None:
        df = random_frame()
        s = MovingAverageCrossStrategy({"short_window": 5, "long_window": 20})
        latest = s.generate_signal(df)
        result = s.evaluate(df)
        assert latest.action.value == result[ACTION_COLUMN].iloc[-1]
        assert set(latest.indicators) == {"sma_short", "sma_long", "volume_ratio", "rsi"}
        assert latest.time == df.index[-1].to_pydatetime()

    def test_signal_to_dict_is_json_serializable(self) -> None:
        df = random_frame()
        signal = MovingAverageCrossStrategy({"short_window": 5, "long_window": 20}).generate_signal(df)
        text = json.dumps(signal.to_dict(), ensure_ascii=False)
        assert '"strategy": "ma_cross"' in text


class TestRSIStrategy:
    def test_oversold_exit_buys_and_overbought_exit_sells(self) -> None:
        # 급락(과매도) → 반등(탈출) → 급등(과매수) → 하락(이탈)
        close = ([100.0] + [100 - 3 * i for i in range(1, 15)] + [58 + 2 * i for i in range(1, 30)]
                 + [116 - 2 * i for i in range(1, 15)])
        df = make_frame(close)
        s = RSIStrategy({"window": 5})
        actions = [x.action for x in s.scan(df)]
        assert actions[0] is Action.BUY
        assert Action.SELL in actions
        assert actions.index(Action.SELL) > 0

    def test_indicator_columns(self) -> None:
        signal = RSIStrategy().generate_signal(random_frame())
        assert list(signal.indicators) == ["rsi"]


class TestCommon:
    @pytest.mark.parametrize("name", list(STRATEGIES))
    def test_no_lookahead(self, name: str) -> None:
        check_no_lookahead(create_strategy(name), random_frame(), samples=10)

    @pytest.mark.parametrize("name", list(STRATEGIES))
    def test_empty_and_missing_columns(self, name: str) -> None:
        s = create_strategy(name)
        with pytest.raises(MarketDataError):
            s.generate_signal(random_frame().iloc[0:0])
        with pytest.raises(MarketDataError):
            s.generate_signal(random_frame().drop(columns=["volume"]))

    def test_lookahead_check_catches_future_reference(self) -> None:
        class Cheater(Strategy):
            name = "cheater"
            Params = StrategyParams

            @property
            def warmup_periods(self) -> int:
                return 3

            def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
                out = df[["close"]].copy()
                out["future"] = df["close"].shift(-1)  # 다음 캔들 종가를 훔쳐본다
                out[ACTION_COLUMN] = np.where(out["future"] > out["close"], "BUY", "HOLD")
                out["reason"] = ""
                return out

        with pytest.raises(StrategyError, match="Look-ahead"):
            check_no_lookahead(Cheater(), random_frame(100), samples=5)
