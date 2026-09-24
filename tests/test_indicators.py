"""지표 테스트 — 손계산 값과 비교하고, 모든 지표가 인과적(미래 참조 없음)인지 검사한다."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategy import indicators as ind


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def random_ohlc(n: int = 300, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    high = close * (1 + rng.uniform(0, 0.01, n))
    low = close * (1 - rng.uniform(0, 0.01, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": rng.uniform(1, 10, n)})


def test_sma() -> None:
    out = ind.sma(series([1, 2, 3, 4, 5]), 3)
    assert out.isna().tolist() == [True, True, False, False, False]
    assert out.tolist()[2:] == [2.0, 3.0, 4.0]


def test_ema_matches_recursion() -> None:
    s = series([10, 11, 12, 13, 14, 15])
    out = ind.ema(s, 3)
    alpha = 2 / (3 + 1)
    expected = s.iloc[0]
    for i in range(1, len(s)):
        expected = alpha * s.iloc[i] + (1 - alpha) * expected
    assert out.iloc[-1] == pytest.approx(expected)
    assert out.isna().tolist()[:2] == [True, True]


def test_rsi_against_loop_implementation() -> None:
    rng = np.random.default_rng(3)
    close = series(list(100 + np.cumsum(rng.normal(0, 1, 60))))
    window = 14
    out = ind.rsi(close, window)

    deltas = close.diff().to_numpy()
    avg_gain = avg_loss = None
    expected_last = None
    for i in range(1, len(close)):
        gain = max(deltas[i], 0.0)
        loss = max(-deltas[i], 0.0)
        if avg_gain is None:
            avg_gain, avg_loss = gain, loss
        else:
            avg_gain = (avg_gain * (window - 1) + gain) / window
            avg_loss = (avg_loss * (window - 1) + loss) / window
        if i >= window:
            expected_last = 100 - 100 / (1 + avg_gain / avg_loss)
    assert out.iloc[-1] == pytest.approx(expected_last, rel=1e-9)
    assert out.iloc[:window].isna().all()
    assert ((out.dropna() >= 0) & (out.dropna() <= 100)).all()


def test_rsi_edge_cases() -> None:
    up = ind.rsi(series(list(range(1, 30))), 5)  # 계속 상승 → 100
    assert up.iloc[-1] == 100.0
    flat = ind.rsi(series([5.0] * 30), 5)  # 변화 없음 → 50
    assert flat.iloc[-1] == 50.0


def test_macd_and_bollinger_shapes() -> None:
    df = random_ohlc()
    m = ind.macd(df["close"])
    assert list(m.columns) == ["macd", "signal", "hist"]
    assert m["hist"].iloc[-1] == pytest.approx(m["macd"].iloc[-1] - m["signal"].iloc[-1])
    with pytest.raises(ValueError):
        ind.macd(df["close"], fast=26, slow=12)
    b = ind.bollinger(df["close"], 20, 2.0)
    assert (b["upper"].dropna() >= b["middle"].dropna()).all()
    assert (b["lower"].dropna() <= b["middle"].dropna()).all()


def test_atr_positive_and_true_range() -> None:
    df = random_ohlc()
    tr = ind.true_range(df["high"], df["low"], df["close"])
    assert (tr >= (df["high"] - df["low"]) - 1e-12).all()
    a = ind.atr(df["high"], df["low"], df["close"], 14)
    assert (a.dropna() > 0).all()


def test_rolling_high_excludes_current() -> None:
    s = series([1, 2, 3, 10, 4])
    assert ind.rolling_high(s, 3).iloc[-1] == 10  # 직전 3개 [2,3,10]
    assert ind.rolling_high(s, 3).iloc[3] == 3  # 현재값 10 은 제외
    assert ind.rolling_low(s, 2).iloc[-1] == 3


def test_crossovers() -> None:
    a = series([1, 2, 3, 2, 1])
    b = series([2, 2, 2, 2, 2])
    assert ind.cross_above(a, b).tolist() == [False, False, True, False, False]
    assert ind.cross_below(a, b).tolist() == [False, False, False, False, True]


@pytest.mark.parametrize("window", [0, -1, 2.5])
def test_invalid_window(window) -> None:
    with pytest.raises(ValueError):
        ind.sma(series([1, 2, 3]), window)


@pytest.mark.parametrize(
    "func",
    [
        lambda df: ind.sma(df["close"], 10),
        lambda df: ind.ema(df["close"], 10),
        lambda df: ind.rsi(df["close"], 14),
        lambda df: ind.macd(df["close"])["hist"],
        lambda df: ind.bollinger(df["close"])["upper"],
        lambda df: ind.atr(df["high"], df["low"], df["close"]),
        lambda df: ind.rolling_high(df["close"], 20),
        lambda df: ind.cross_above(ind.sma(df["close"], 5), ind.sma(df["close"], 20)).astype(float),
    ],
)
def test_indicators_are_causal(func) -> None:
    """프리픽스로 계산한 마지막 값 == 전체로 계산한 같은 위치 값 (미래 참조 없음)."""
    df = random_ohlc(200)
    full = func(df)
    for i in [40, 77, 120, 199]:
        partial = func(df.iloc[: i + 1])
        a, b = partial.iloc[-1], full.iloc[i]
        assert (pd.isna(a) and pd.isna(b)) or a == pytest.approx(b, rel=1e-9, abs=1e-9)
