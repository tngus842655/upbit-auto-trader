"""신규 전략 테스트 — 합성 캔들로 BUY/SELL/HOLD 위치를 확인하고, 모든 신규 전략에 공통 성질을 검사한다.

공통 성질:
- 워밍업 경계: 정확히 ``warmup_periods`` 개면 신호를 판단하고(마지막·직전 캔들 지표가 모두 있음),
  하나 모자라면 HOLD(warmup)
- 데이터 부족: 캔들이 1개여도 오류 없이 HOLD
- Look-ahead: 뒤에 미래 캔들을 붙여도 이미 확정된 과거 행(행동·사유·지표)이 바뀌지 않는다
- NaN/무한대: 워밍업 뒤에는 지표가 모두 유한하고, 신호는 JSON(allow_nan=False)으로 직렬화된다
- 정체장(가격·거래량 불변)에서는 어떤 전략도 매매 신호를 내지 않는다
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.core.exceptions import StrategyError
from app.strategy import STRATEGIES, Action, StrategyFamily, create_strategy
from app.strategy.adx_trend import trend_strength
from app.strategy.base import ACTION_COLUMN, REASON_COLUMN
from tests.test_strategy import make_frame, random_frame

NEW_STRATEGIES = [
    "bollinger", "macd", "ema_cross", "volume_breakout",  # Phase 1
    "adx_trend", "stochastic", "cci", "williams_r",  # Phase 2
    "ichimoku", "obv",  # Phase 3
]


def actions_at(df: pd.DataFrame, strategy_name: str, params: dict | None = None) -> dict[int, str]:
    """{행 번호: BUY/SELL} — HOLD 는 뺀다."""
    result = create_strategy(strategy_name, params).evaluate(df)
    return {i: a for i, a in enumerate(result[ACTION_COLUMN]) if a != Action.HOLD.value}


def reason_at(df: pd.DataFrame, strategy_name: str, i: int, params: dict | None = None) -> str:
    return str(create_strategy(strategy_name, params).evaluate(df)[REASON_COLUMN].iloc[i])


def flat_frame(n: int = 200) -> pd.DataFrame:
    return make_frame([100.0] * n, [1.0] * n)


def ohlc_frame(close: list[float], volume: list[float] | None = None) -> pd.DataFrame:
    """시가 = 직전 종가, 고가·저가 = 시가·종가의 최대·최소 (꼬리 없는 캔들) — 오르는 캔들은 종가가 고가라
    스토캐스틱·%R 이 정확히 극단값(100 / 0)에 붙는다."""
    c = np.asarray(close, dtype="float64")
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.DatetimeIndex([datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i) for i in range(len(c))], name="time")
    df = pd.DataFrame(
        {"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c,
         "volume": np.asarray(volume if volume is not None else [1.0] * len(c), dtype="float64")},
        index=idx,
    )
    df["value"] = df["close"] * df["volume"]
    df.attrs["market"] = "KRW-TEST"
    df.attrs["interval"] = "60m"
    return df


def v_shape_frame() -> pd.DataFrame:
    """1씩 25캔들 하락(0~24) → 2씩 30캔들 상승(25~54) → 2씩 10캔들 하락(55~64). 오실레이터 과매도·과매수 왕복."""
    close = [130 - i for i in range(25)] + [106 + 2 * i for i in range(1, 31)] + [166 - 2 * i for i in range(1, 11)]
    return ohlc_frame(close)


# ---------------------------------------------------------------------------
# 공통 성질
# ---------------------------------------------------------------------------
class TestCommonProperties:
    @pytest.mark.parametrize("name", NEW_STRATEGIES)
    def test_metadata(self, name: str) -> None:
        cls = STRATEGIES[name]
        assert cls.name == name and cls.description and cls.rules
        assert isinstance(cls.family, StrategyFamily)
        schema = cls.Params.model_json_schema()["properties"]
        assert all(prop.get("label") for prop in schema.values())  # 대시보드 폼에 보일 이름
        for prop in schema.values():
            if "depends_on" in prop:
                assert schema[prop["depends_on"]]["type"] == "boolean"

    @pytest.mark.parametrize("name", NEW_STRATEGIES)
    def test_warmup_boundary(self, name: str) -> None:
        s = create_strategy(name)
        df = random_frame(s.warmup_periods + 50)
        exact = df.iloc[: s.warmup_periods]
        signal = s.generate_signal(exact)
        assert not signal.reason.startswith("warmup")
        result = s.evaluate(exact)
        last_two = result[s.indicator_columns].iloc[-2:]
        assert last_two.notna().all().all(), f"{name}: 워밍업 캔들 수가 모자람\n{last_two}"
        # 워밍업은 필요 이상으로 길지 않다 (한 캔들 앞은 지표가 아직 없음)
        assert result[s.indicator_columns].iloc[-3].isna().any()
        short = s.generate_signal(df.iloc[: s.warmup_periods - 1])
        assert short.action is Action.HOLD and short.reason.startswith("warmup")

    @pytest.mark.parametrize("name", NEW_STRATEGIES)
    @pytest.mark.parametrize("count", [1, 2, 5])
    def test_insufficient_data_is_hold(self, name: str, count: int) -> None:
        s = create_strategy(name)
        df = random_frame(count)
        result = s.evaluate(df)
        assert (result[ACTION_COLUMN] == Action.HOLD.value).all()
        assert s.generate_signal(df).action is Action.HOLD
        assert s.scan(df) == []

    @pytest.mark.parametrize("name", NEW_STRATEGIES)
    def test_future_candles_do_not_change_past(self, name: str) -> None:
        s = create_strategy(name)
        df = random_frame(500, seed=3)
        full = s.evaluate(df)
        for cut in (s.warmup_periods, 150, 333, 499):
            past = s.evaluate(df.iloc[:cut])
            pd.testing.assert_series_equal(past[ACTION_COLUMN], full[ACTION_COLUMN].iloc[:cut])
            pd.testing.assert_series_equal(past[REASON_COLUMN], full[REASON_COLUMN].iloc[:cut])
            pd.testing.assert_frame_equal(past[s.indicator_columns], full[s.indicator_columns].iloc[:cut],
                                          rtol=1e-9, atol=1e-9)

    @pytest.mark.parametrize("name", NEW_STRATEGIES)
    def test_indicators_finite_after_warmup(self, name: str) -> None:
        s = create_strategy(name)
        df = random_frame(400, seed=5)
        values = s.evaluate(df)[s.indicator_columns].iloc[s.warmup_periods - 1 :].to_numpy(dtype="float64")
        assert np.isfinite(values).all()
        text = json.dumps(s.generate_signal(df).to_dict(), ensure_ascii=False, allow_nan=False)
        assert f'"strategy": "{name}"' in text

    @pytest.mark.parametrize("name", NEW_STRATEGIES)
    def test_warmup_rows_are_hold_with_reason(self, name: str) -> None:
        s = create_strategy(name)
        result = s.evaluate(random_frame(200))
        warm = result.iloc[: s.warmup_periods - 1]
        assert (warm[ACTION_COLUMN] == "HOLD").all() and (warm[REASON_COLUMN] == "warmup").all()

    @pytest.mark.parametrize("name", [*NEW_STRATEGIES, "ma_cross", "rsi"])
    def test_flat_market_never_trades(self, name: str) -> None:
        assert actions_at(flat_frame(), name) == {}
        indicators = create_strategy(name).generate_signal(flat_frame()).indicators
        assert all(math.isfinite(v) for v in indicators.values())


# ---------------------------------------------------------------------------
# Bollinger
# ---------------------------------------------------------------------------
BOX = [100 + 0.3 * math.sin(i) for i in range(40)]


def bollinger_spike_scenario() -> tuple[pd.DataFrame, int, int]:
    """잔잔한 박스권 → 한 캔들 급락(하단 이탈) → 반등(재진입) → … → 한 캔들 급등(상단 이탈) → 하락(재진입).

    한 캔들짜리 튐이라 재진입 캔들의 RSI 는 50 근처(과매도·과매수 아님)다.
    """
    close = BOX + [94.0, 99.8] + BOX[:20] + [106.0, 100.2] + BOX[:10]
    return make_frame(close), 41, 63


def bollinger_slide_scenario() -> tuple[pd.DataFrame, int, int]:
    """박스권 → 5캔들 연속 하락(하단 이탈) → 반등(재진입, RSI≈28) → … → 5캔들 연속 상승 → 하락(재진입, RSI≈70)."""
    close = BOX + [99.0, 98.0, 97.0, 96.0, 94.5, 95.6] + BOX[:25] + [101.0, 102.0, 103.0, 104.0, 105.5, 104.2] + BOX[:5]
    return make_frame(close), 45, 76


class TestBollinger:
    NO_RSI = {"rsi_filter": False}

    def test_reentry_buy_and_fall_back_sell(self) -> None:
        df, rebound, fall_back = bollinger_spike_scenario()
        assert actions_at(df, "bollinger", self.NO_RSI) == {rebound: "BUY", fall_back: "SELL"}
        result = create_strategy("bollinger", self.NO_RSI).evaluate(df)
        # 규칙 그대로인지 확인: 직전 종가 <= 직전 하단, 현재 종가 > 현재 하단
        assert result["close"].iloc[rebound - 1] <= result["bb_lower"].iloc[rebound - 1]
        assert result["close"].iloc[rebound] > result["bb_lower"].iloc[rebound]
        assert result["close"].iloc[fall_back - 1] >= result["bb_upper"].iloc[fall_back - 1]
        assert result["close"].iloc[fall_back] < result["bb_upper"].iloc[fall_back]
        assert "하단 재진입" in result[REASON_COLUMN].iloc[rebound]

    def test_rsi_filter_blocks_when_rsi_not_extreme(self) -> None:
        df, rebound, fall_back = bollinger_spike_scenario()
        assert actions_at(df, "bollinger") == {}  # 기본값: RSI 필터 켬 (40 / 60)
        assert "RSI≥40" in reason_at(df, "bollinger", rebound)
        assert "RSI≤60" in reason_at(df, "bollinger", fall_back)

    def test_default_rsi_filter_passes_after_slide(self) -> None:
        df, rebound, fall_back = bollinger_slide_scenario()
        assert actions_at(df, "bollinger") == {rebound: "BUY", fall_back: "SELL"}
        result = create_strategy("bollinger").evaluate(df)
        assert result["rsi"].iloc[rebound] < 40 and result["rsi"].iloc[fall_back] > 60

    def test_default_params_and_validation(self) -> None:
        p = create_strategy("bollinger").params
        assert (p.window, p.std_dev, p.rsi_filter) == (20, 2.0, True)
        assert (p.rsi_window, p.oversold, p.overbought) == (14, 40, 60)
        with pytest.raises(StrategyError, match="oversold"):
            create_strategy("bollinger", {"oversold": 70, "overbought": 30})
        with pytest.raises(StrategyError):
            create_strategy("bollinger", {"window": 1})
        assert create_strategy("bollinger", {"rsi_filter": False}).indicator_columns == [
            "bb_upper", "bb_middle", "bb_lower"]


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
class TestMACD:
    def test_cross_up_above_zero_buys_and_cross_down_sells(self) -> None:
        # 정체 → 상승(MACD 가 0 위에서 시그널 상향 돌파) → 하락(하향 돌파)
        close = [100.0] * 40 + [100 + i for i in range(1, 31)] + [130 - 2 * i for i in range(1, 21)]
        df = make_frame(close)
        got = actions_at(df, "macd")
        assert got[40] == "BUY" and list(got.values()).count("BUY") == 1
        sells = [i for i, a in got.items() if a == "SELL"]
        assert len(sells) == 1 and 70 <= sells[0] <= 75  # 고점(69) 직후
        result = create_strategy("macd").evaluate(df)
        assert result["macd"].iloc[40] > 0 and result["macd"].iloc[40] > result["macd_signal"].iloc[40]

    def test_zero_line_filter(self) -> None:
        # 정체 → 하락 → 반등: 시그널 상향 돌파가 0선 아래에서 난다
        close = [100.0] * 40 + [100 - i for i in range(1, 31)] + [70 + 0.5 * i for i in range(1, 11)]
        df = make_frame(close)
        assert "BUY" not in actions_at(df, "macd").values()
        unfiltered = actions_at(df, "macd", {"zero_line_filter": False})
        buys = [i for i, a in unfiltered.items() if a == "BUY"]
        assert len(buys) == 1 and buys[0] >= 70
        assert "0선" in reason_at(df, "macd", buys[0])
        assert create_strategy("macd").evaluate(df)["macd"].iloc[buys[0]] < 0

    def test_default_params_and_validation(self) -> None:
        p = create_strategy("macd").params
        assert (p.fast_period, p.slow_period, p.signal_period, p.zero_line_filter) == (12, 26, 9, True)
        assert create_strategy("macd").warmup_periods == 35
        with pytest.raises(StrategyError, match="fast_period"):
            create_strategy("macd", {"fast_period": 26, "slow_period": 12})


# ---------------------------------------------------------------------------
# EMA Cross
# ---------------------------------------------------------------------------
class TestEMACross:
    def test_alignment_buys_once_and_short_below_medium_sells(self) -> None:
        close = [100.0] * 60 + [100 + i for i in range(1, 41)] + [140 - 2 * i for i in range(1, 21)]
        df = make_frame(close)
        got = actions_at(df, "ema_cross")
        assert got[60] == "BUY" and list(got.values()).count("BUY") == 1  # 정배열이 이어지는 동안 반복 안 함
        sells = [i for i, a in got.items() if a == "SELL"]
        assert len(sells) == 1 and sells[0] > 100
        result = create_strategy("ema_cross").evaluate(df)
        i = sells[0]
        assert result["ema_short"].iloc[i - 1] >= result["ema_medium"].iloc[i - 1]
        assert result["ema_short"].iloc[i] < result["ema_medium"].iloc[i]

    def test_rsi_filter_blocks_weak_momentum(self) -> None:
        # 쉬지 않고 오르면 RSI=100 → 기준 99 도 통과
        steady = make_frame([100.0] * 60 + [100 + i for i in range(1, 41)])
        assert actions_at(steady, "ema_cross", {"rsi_threshold": 99}) == {60: "BUY"}
        # 하락 뒤 +2/-1 로 들쭉날쭉 오르면 정배열이 완성될 때 RSI≈61 → 기본 기준 50 은 통과, 99 는 막힘
        wavy_close = [130 - 0.5 * i for i in range(60)] + [100.5 + sum(2 if k % 2 == 0 else -1 for k in range(i))
                                                           for i in range(1, 61)]
        wavy = make_frame(wavy_close)
        aligned = actions_at(wavy, "ema_cross", {"rsi_filter": False})
        assert list(aligned.values()) == ["BUY"]
        entry = next(iter(aligned))
        assert actions_at(wavy, "ema_cross") == {entry: "BUY"}
        assert actions_at(wavy, "ema_cross", {"rsi_threshold": 99}) == {}
        assert "정배열이지만 RSI≤99" in reason_at(wavy, "ema_cross", entry, {"rsi_threshold": 99})

    def test_validation(self) -> None:
        with pytest.raises(StrategyError, match="short_period"):
            create_strategy("ema_cross", {"short_period": 30, "medium_period": 21})
        with pytest.raises(StrategyError):
            create_strategy("ema_cross", {"medium_period": 60, "long_period": 50})
        assert create_strategy("ema_cross", {"rsi_filter": False}).warmup_periods == 51


# ---------------------------------------------------------------------------
# Volume Breakout
# ---------------------------------------------------------------------------
def breakout_frame(spike_volume: float) -> tuple[pd.DataFrame, int, int]:
    """박스권(99~101, 거래량 1) → 돌파 캔들(105) → 박스권 위 유지 → 급락(10캔들 저가 이탈)."""
    box = [100 + (1 if i % 2 else -1) for i in range(30)]
    close = box + [105.0] + [106.0] * 12 + [90.0, 89.0]
    volume = [1.0] * 30 + [spike_volume] + [1.0] * 14
    return make_frame(close, volume), 30, 43


class TestVolumeBreakout:
    def test_breakout_with_volume_buys_and_channel_exit_sells(self) -> None:
        df, breakout, drop = breakout_frame(3.0)
        assert actions_at(df, "volume_breakout") == {breakout: "BUY", drop: "SELL"}
        result = create_strategy("volume_breakout").evaluate(df)
        # 기준값은 현재 캔들을 뺀 직전 20개: 최고가 = 101 × 1.001, 평균 거래량 = 1 → 비율 정확히 3
        assert result["breakout_high"].iloc[breakout] == pytest.approx(101 * 1.001)
        assert result["volume_ratio"].iloc[breakout] == pytest.approx(3.0)
        assert result["exit_low"].iloc[drop] == pytest.approx(106 * 0.999)

    def test_breakout_without_volume_is_hold(self) -> None:
        df, breakout, drop = breakout_frame(1.2)
        got = actions_at(df, "volume_breakout")
        assert "BUY" not in got.values() and got == {drop: "SELL"}
        assert "거래량 1.5배 미만" in reason_at(df, "volume_breakout", breakout)

    def test_exit_window_zero_leaves_exits_to_risk(self) -> None:
        df, breakout, _drop = breakout_frame(3.0)
        s = create_strategy("volume_breakout", {"exit_window": 0})
        assert actions_at(df, "volume_breakout", {"exit_window": 0}) == {breakout: "BUY"}
        assert "exit_low" not in s.indicator_columns

    def test_default_params(self) -> None:
        p = create_strategy("volume_breakout").params
        assert (p.breakout_window, p.volume_window, p.volume_multiplier, p.exit_window) == (20, 20, 1.5, 10)
        assert create_strategy("volume_breakout").warmup_periods == 22


# ---------------------------------------------------------------------------
# ADX Trend
# ---------------------------------------------------------------------------
def adx_frame() -> pd.DataFrame:
    """횡보(100↔101 반복, ADX<25) 40캔들 → 1씩 40캔들 상승 → 1.5씩 30캔들 하락."""
    close = [100 + (i % 2) for i in range(40)] + [101 + i for i in range(1, 41)] + [141 - 1.5 * i for i in range(1, 31)]
    return make_frame(close)


class TestADXTrend:
    def test_strong_uptrend_buys_once_and_di_flip_sells(self) -> None:
        df = adx_frame()
        assert actions_at(df, "adx_trend") == {48: "BUY", 86: "SELL"}
        r = create_strategy("adx_trend").evaluate(df)
        assert r["adx"].iloc[30:40].max() < 25  # 횡보 구간은 추세 약함
        # 매수 캔들: ADX 가 25 를 넘어선 캔들 (+DI > −DI 는 이미 성립)
        assert r["adx"].iloc[47] <= 25 < r["adx"].iloc[48] and r["plus_di"].iloc[48] > r["minus_di"].iloc[48]
        # 매도 캔들: −DI 가 +DI 위로
        assert r["minus_di"].iloc[85] <= r["plus_di"].iloc[85] and r["minus_di"].iloc[86] > r["plus_di"].iloc[86]

    def test_threshold_blocks_weak_trend(self) -> None:
        strict = {"adx_threshold": 99}
        assert actions_at(adx_frame(), "adx_trend", strict) == {86: "SELL"}
        # 하락 → 상승: +DI 가 −DI 를 넘는 캔들에서 ADX 가 기준(99) 아래 → 매수 없이 사유만 남는다
        df = make_frame([140 - i for i in range(40)] + [100 + i for i in range(1, 41)])
        assert "BUY" not in actions_at(df, "adx_trend", strict).values()
        r = create_strategy("adx_trend", strict).evaluate(df)
        flip = next(i for i in range(41, 80) if r["plus_di"].iloc[i] > r["minus_di"].iloc[i])
        assert r["plus_di"].iloc[flip - 1] <= r["minus_di"].iloc[flip - 1]
        assert "ADX≤99" in r[REASON_COLUMN].iloc[flip]
        assert "BUY" in actions_at(df, "adx_trend").values()  # 기본 기준 25 는 상승 추세가 붙으면 매수

    def test_trend_strength_is_reusable_filter(self) -> None:
        trend = trend_strength(adx_frame(), 14, 25.0)
        assert {"adx", "plus_di", "minus_di", "strong", "bullish"} <= set(trend.columns)
        assert trend["strong"].dtype == bool and not trend["strong"].iloc[30:40].any() and trend["strong"].iloc[60]

    def test_params(self) -> None:
        s = create_strategy("adx_trend")
        assert (s.params.adx_window, s.params.adx_threshold, s.warmup_periods) == (14, 25.0, 29)
        with pytest.raises(StrategyError):
            create_strategy("adx_trend", {"adx_window": 1})


# ---------------------------------------------------------------------------
# Stochastic
# ---------------------------------------------------------------------------
class TestStochastic:
    def test_cross_in_zones(self) -> None:
        df = v_shape_frame()
        assert actions_at(df, "stochastic") == {25: "BUY", 55: "SELL"}
        r = create_strategy("stochastic").evaluate(df)
        assert r["stoch_k"].iloc[24] == 0.0 and r["stoch_k"].iloc[54] == 100.0  # 꼬리 없는 캔들 → 극단값
        assert r["stoch_d"].iloc[24] < 20 and r["stoch_k"].iloc[25] > r["stoch_d"].iloc[25]
        assert r["stoch_d"].iloc[54] > 80 and r["stoch_k"].iloc[55] < r["stoch_d"].iloc[55]

    def test_cross_outside_zone_is_ignored(self) -> None:
        # 상승(과매수) → 짧은 눌림(하향 돌파 = 매도) → 재상승: 상향 돌파가 과매도 구간 밖(중간)에서 나므로 매수 아님
        close = [100 + 2 * i for i in range(30)] + [156, 152, 148] + [150 + 3 * i for i in range(1, 8)]
        got = actions_at(ohlc_frame(close), "stochastic")
        assert got == {30: "SELL"}
        r = create_strategy("stochastic").evaluate(ohlc_frame(close))
        up = [i for i in range(31, 40) if r["stoch_k"].iloc[i - 1] <= r["stoch_d"].iloc[i - 1]
              and r["stoch_k"].iloc[i] > r["stoch_d"].iloc[i]]
        assert up and all(r["stoch_d"].iloc[i - 1] >= 20 for i in up)

    def test_params(self) -> None:
        s = create_strategy("stochastic")
        assert (s.params.k_period, s.params.d_period, s.params.oversold, s.params.overbought) == (14, 3, 20, 80)
        assert s.params.smooth_k == 1 and s.warmup_periods == 17
        assert create_strategy("stochastic", {"smooth_k": 3}).warmup_periods == 19
        with pytest.raises(StrategyError, match="oversold"):
            create_strategy("stochastic", {"oversold": 80, "overbought": 20})
        with pytest.raises(StrategyError):
            create_strategy("stochastic", {"d_period": 1})


# ---------------------------------------------------------------------------
# CCI
# ---------------------------------------------------------------------------
class TestCCI:
    def test_recovery_buy_and_fall_sell(self) -> None:
        df = v_shape_frame()
        assert actions_at(df, "cci") == {26: "BUY", 56: "SELL"}
        r = create_strategy("cci").evaluate(df)
        assert r["cci"].iloc[25] < -100 <= r["cci"].iloc[26]
        assert r["cci"].iloc[55] > 100 >= r["cci"].iloc[56]
        # 일정한 기울기의 추세에서 CCI = 기울기×9.5 / (0.015 × 기울기×5) = 126.67
        assert r["cci"].iloc[24] == pytest.approx(-126.6667, rel=1e-4)
        assert "과매도(-100) 회복" in r[REASON_COLUMN].iloc[26]

    def test_custom_levels(self) -> None:
        df = v_shape_frame()
        # 과매도선 −150: 하락 추세 CCI(−126.7)가 선에 못 미쳐 매수 없음.
        # 과매수선 +150: V자 반등 직후 CCI 가 150 을 넘었다가 추세 값(126.7)으로 내려오는 캔들에서 매도
        got = actions_at(df, "cci", {"oversold": -150, "overbought": 150})
        assert list(got.values()) == ["SELL"]
        i = next(iter(got))
        r = create_strategy("cci").evaluate(df)
        assert 25 < i < 55 and r["cci"].iloc[i - 1] > 150 >= r["cci"].iloc[i]
        with pytest.raises(StrategyError, match="oversold"):
            create_strategy("cci", {"oversold": 0, "overbought": 0})
        assert create_strategy("cci").warmup_periods == 21


# ---------------------------------------------------------------------------
# Williams %R
# ---------------------------------------------------------------------------
class TestWilliamsR:
    def test_recovery_buy_and_fall_sell(self) -> None:
        df = v_shape_frame()
        assert actions_at(df, "williams_r") == {26: "BUY", 57: "SELL"}
        r = create_strategy("williams_r").evaluate(df)
        assert r["williams_r"].iloc[24] == -100.0 and r["williams_r"].iloc[54] == 0.0
        # 26번 캔들: 현재 포함 최근 14개(13~26)의 최고가·최저가로 직접 계산한 값과 같다
        hi = df["high"].iloc[13:27].max()
        lo = df["low"].iloc[13:27].min()
        assert r["williams_r"].iloc[26] == pytest.approx(-100 * (hi - df["close"].iloc[26]) / (hi - lo))
        assert r["williams_r"].iloc[25] < -80 <= r["williams_r"].iloc[26]

    def test_params(self) -> None:
        s = create_strategy("williams_r")
        assert (s.params.window, s.params.oversold, s.params.overbought, s.warmup_periods) == (14, -80, -20, 15)
        with pytest.raises(StrategyError):
            create_strategy("williams_r", {"oversold": -10, "overbought": -90})
        with pytest.raises(StrategyError):
            create_strategy("williams_r", {"overbought": 10})  # %R 은 0 이하


# ---------------------------------------------------------------------------
# Ichimoku
# ---------------------------------------------------------------------------
def ichimoku_frame() -> pd.DataFrame:
    """1씩 100캔들 하락(구름 아래) → 2씩 60캔들 상승 → 2씩 30캔들 하락."""
    close = [250 - i for i in range(100)] + [150 + 2 * i for i in range(1, 61)] + [270 - 2 * i for i in range(1, 31)]
    return make_frame(close)


class TestIchimoku:
    def test_cloud_breakout_buy_and_tk_cross_sell(self) -> None:
        df = ichimoku_frame()
        assert actions_at(df, "ichimoku") == {117: "BUY", 170: "SELL"}
        r = create_strategy("ichimoku").evaluate(df)
        # 전환선은 108 에서 기준선을 넘었지만 가격이 구름 위로 올라선 117 에서야 매수
        assert r["tenkan"].iloc[108] > r["kijun"].iloc[108] and r["tenkan"].iloc[107] <= r["kijun"].iloc[107]
        top = r[["span_a", "span_b"]].max(axis=1)
        assert r["close"].iloc[116] <= top.iloc[116] and r["close"].iloc[117] > top.iloc[117]
        assert r[REASON_COLUMN].iloc[170] == "전환선<기준선"

    def test_cloud_is_displaced_from_the_past(self) -> None:
        """현재 캔들의 구름 = displacement 캔들 전에 계산된 선행스팬 (미래 구름을 당겨 쓰지 않음)."""
        df = ichimoku_frame()
        r = create_strategy("ichimoku").evaluate(df)
        d = 26
        for i in (90, 130, 180):
            assert r["span_a"].iloc[i] == pytest.approx((r["tenkan"].iloc[i - d] + r["kijun"].iloc[i - d]) / 2)
            past = df.iloc[i - d - 51 : i - d + 1]  # 선행스팬 B 를 계산한 캔들까지의 52개
            assert r["span_b"].iloc[i] == pytest.approx((past["high"].max() + past["low"].min()) / 2)

    def test_sell_on_falling_below_cloud(self) -> None:
        # 상승 추세 뒤 한 번에 구름 아래로 급락: 전환선<기준선 보다 구름 이탈이 먼저(같은 캔들) 성립
        close = [100 + i for i in range(120)] + [150.0] * 3
        got = actions_at(make_frame(close), "ichimoku")
        assert got.get(120) == "SELL"
        assert "구름 아래로 이탈" in reason_at(make_frame(close), "ichimoku", 120)

    def test_params(self) -> None:
        s = create_strategy("ichimoku")
        p = s.params
        assert (p.conversion_period, p.base_period, p.span_b_period, p.displacement) == (9, 26, 52, 26)
        assert s.warmup_periods == 79
        with pytest.raises(StrategyError, match="conversion_period"):
            create_strategy("ichimoku", {"conversion_period": 30})
        with pytest.raises(StrategyError):
            create_strategy("ichimoku", {"span_b_period": 20})


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------
def obv_frame() -> pd.DataFrame:
    """1씩 40캔들 하락(거래량 1) → 2씩 20캔들 상승(거래량 3) → 2씩 15캔들 하락(거래량 3)."""
    close = [140 - i for i in range(40)] + [101 + 2 * i for i in range(1, 21)] + [141 - 2 * i for i in range(1, 16)]
    return make_frame(close, [1.0] * 40 + [3.0] * 35)


class TestOBV:
    def test_obv_and_price_trend_buy_and_obv_cross_sell(self) -> None:
        df = obv_frame()
        assert actions_at(df, "obv") == {43: "BUY", 65: "SELL"}
        r = create_strategy("obv").evaluate(df)
        assert r["obv"].iloc[39] == -39.0 and r["obv"].iloc[43] == -27.0  # 하락 1×39, 상승 3×4
        # 42: OBV 는 평균 위로 올라섰지만 종가가 아직 SMA20 아래 → 43 에서 둘 다 성립
        assert r["obv"].iloc[42] > r["obv_ma"].iloc[42] and r["close"].iloc[42] <= r["price_ma"].iloc[42]
        assert "종가≤SMA20" in r[REASON_COLUMN].iloc[42]
        assert r["obv"].iloc[64] >= r["obv_ma"].iloc[64] and r["obv"].iloc[65] < r["obv_ma"].iloc[65]

    def test_price_window_zero_uses_obv_only(self) -> None:
        assert actions_at(obv_frame(), "obv", {"price_window": 0}) == {42: "BUY", 65: "SELL"}
        assert create_strategy("obv", {"price_window": 0}).indicator_columns == ["obv", "obv_ma"]

    def test_signals_do_not_depend_on_data_start(self) -> None:
        """실시간 엔진은 최근 캔들만 들고 있어 OBV 시작점이 백테스트와 다르다 → 신호는 같아야 한다."""
        df = random_frame(500, seed=9)
        s = create_strategy("obv")
        full = s.evaluate(df)
        trimmed = s.evaluate(df.iloc[120:])
        start = 120 + s.warmup_periods
        pd.testing.assert_series_equal(trimmed[ACTION_COLUMN].loc[df.index[start]:],
                                       full[ACTION_COLUMN].loc[df.index[start]:])
        gap_full = (full["obv"] - full["obv_ma"]).iloc[start:]
        gap_trim = (trimmed["obv"] - trimmed["obv_ma"]).loc[gap_full.index]
        np.testing.assert_allclose(gap_trim.to_numpy(), gap_full.to_numpy(), rtol=1e-9, atol=1e-6)
        assert trimmed["obv"].iloc[-1] != pytest.approx(full["obv"].iloc[-1])  # 절대값은 시작점에 따라 다르다

    def test_params(self) -> None:
        s = create_strategy("obv")
        assert (s.params.obv_window, s.params.price_window, s.warmup_periods) == (20, 20, 21)
        with pytest.raises(StrategyError):
            create_strategy("obv", {"obv_window": 1})
