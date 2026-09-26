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
    StrategyFamily,
    available_strategies,
    check_no_lookahead,
    create_strategy,
    strategies_by_family,
    strategy_catalog,
)
from app.strategy.base import ACTION_COLUMN, StrategyParams

ALL_STRATEGIES = [
    "ma_cross", "rsi", "bollinger", "macd", "ema_cross", "volume_breakout",
    "adx_trend", "stochastic", "ichimoku", "obv", "cci", "williams_r",
]


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
        assert list(available_strategies()) == ALL_STRATEGIES  # 기존 2개 + 신규 10개, 드롭다운 순서
        assert available_strategies()["ma_cross"] == "단기/장기 SMA 교차 + 거래량·RSI 필터"
        assert available_strategies()["macd"] == "MACD Signal 교차"
        s = create_strategy("MA_CROSS", {"short_window": 5, "long_window": 10})
        assert isinstance(s, MovingAverageCrossStrategy)
        assert s.params.short_window == 5
        assert isinstance(create_strategy("rsi"), RSIStrategy)
        for name in ALL_STRATEGIES:
            assert create_strategy(name).name == name

    def test_families(self) -> None:
        by_family = {family: set(names) for family, names in strategies_by_family().items()}
        assert by_family == {
            StrategyFamily.TREND: {"ma_cross", "ema_cross", "macd", "ichimoku"},
            StrategyFamily.MEAN_REVERSION: {"rsi", "bollinger", "stochastic", "cci", "williams_r"},
            StrategyFamily.BREAKOUT: {"volume_breakout"},
            StrategyFamily.FILTER: {"adx_trend", "obv"},
        }
        assert StrategyFamily.TREND.label == "추세 추종"

    def test_catalog(self) -> None:
        catalog = strategy_catalog()
        assert [c["name"] for c in catalog] == ALL_STRATEGIES
        ichimoku = next(c for c in catalog if c["name"] == "ichimoku")
        assert ichimoku["family"] == "TREND" and ichimoku["warmup_periods"] == 79 and "구름" in ichimoku["rules"]
        assert all(c["rules"] and c["family_label"] for c in catalog)

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


def golden_frame() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 600)))
    return make_frame(list(close), list(rng.uniform(1, 5, 600)))


# 전략 확장 전 코드로 기록한 기존 전략의 출력 (신호 위치 B=BUY/S=SELL, 마지막 지표값, 워밍업, 백테스트 결과).
# 신규 전략·공용 모듈을 고쳐도 기존 전략의 동작이 한 캔들도 바뀌지 않았는지 확인한다.
GOLDEN = [
    ("ma_cross", {}, "S199 S309 B327 S436 B473 S498 S535 B539 S543 B557 S572",
     {"sma_short": 123.4535297544, "sma_long": 130.1807023835, "volume_ratio": 1.2064580457, "rsi": 45.7075815104},
     61, 4, 962384.605249, 4302.06542),
    ("ma_cross", {"long_window": 20, "short_window": 5},
     "S24 B42 S46 B54 S69 S89 B90 S106 B113 S132 B138 S144 B158 S180 S185 B207 S225 B250 S273 S296 S308 S348 S370 "
     "B371 S391 S423 B454 S485 S502 S529 B549 S565 S589",
     {"sma_short": 120.2159240321, "sma_long": 123.4535297544, "volume_ratio": 1.2064580457, "rsi": 45.7075815104},
     21, 11, 824788.802895, 10028.618614),
    ("ma_cross", {"long_window": 20, "rsi_window": 0, "short_window": 5, "volume_window": 0},
     "S24 B42 S46 B54 S69 B70 S89 B90 S106 B113 S132 B138 S144 B158 S180 B183 S185 B207 S225 B250 S273 B282 S296 "
     "B307 S308 B315 S348 B358 S370 B371 S391 B400 S423 B454 S485 B501 S502 B511 S529 B549 S565 B584 S589",
     {"sma_short": 120.2159240321, "sma_long": 123.4535297544}, 21, 21, 788333.146764, 19714.591776),
    ("rsi", {}, "S21 S76 S166 B230 S257 S261 S263 S339 S343 S414 B570",
     {"rsi": 45.7075815104}, 16, 2, 1056078.91732, 2145.903108),
    ("rsi", {"overbought": 75, "oversold": 25, "window": 7},
     "S9 B30 B33 S76 B150 S159 S166 B192 B197 B231 S253 S263 S326 S343 S382 S415 B428 S561 B571 B593",
     {"rsi": 50.4748266592}, 9, 5, 1112300.449326, 5771.004645),
]


class TestExistingStrategiesRegression:
    @pytest.mark.parametrize(("name", "params", "signals", "last", "warmup", "trades", "equity", "fees"), GOLDEN)
    def test_matches_recorded_behaviour(self, name, params, signals, last, warmup, trades, equity, fees) -> None:
        from app.backtest import BacktestConfig, BacktestEngine

        df = golden_frame()
        s = create_strategy(name, params)
        result = s.evaluate(df)
        got = " ".join(f"{a[0]}{i}" for i, a in enumerate(result[ACTION_COLUMN]) if a != "HOLD")
        assert got == signals
        assert s.warmup_periods == warmup
        assert {k: round(v, 10) for k, v in s.generate_signal(df).indicators.items()} == last
        bt = BacktestEngine(BacktestConfig()).run(df, s, market="KRW-TEST", interval="60m")
        assert bt.metrics.total_trades == trades
        assert bt.metrics.final_equity == pytest.approx(equity, abs=1e-5)
        assert bt.metrics.total_fees == pytest.approx(fees, abs=1e-5)


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
