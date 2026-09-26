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
        lambda df: ind.stochastic(df["high"], df["low"], df["close"], 14, 3, 3)["d"],
        lambda df: ind.williams_r(df["high"], df["low"], df["close"], 14),
        lambda df: ind.cci(df["high"], df["low"], df["close"], 20),
        lambda df: ind.adx(df["high"], df["low"], df["close"], 14)["adx"],
        lambda df: ind.adx(df["high"], df["low"], df["close"], 14)["minus_di"],
        lambda df: ind.ichimoku(df["high"], df["low"])["span_a"],
        lambda df: ind.ichimoku(df["high"], df["low"])["span_b"],
        lambda df: ind.obv(df["close"], df["volume"]),
        lambda df: ind.rising_edge(df["close"] > ind.sma(df["close"], 10)).astype(float),
        lambda df: ind.level_cross_up(ind.rsi(df["close"], 14), 50.0).astype(float),
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


# ---------------------------------------------------------------------------
# 전략 확장용 지표 (Stochastic, Williams %R, CCI, ADX, 일목균형표, OBV, 상태 전환)
# ---------------------------------------------------------------------------
HIGH = series([10, 12, 11, 13, 12])
LOW = series([8, 9, 9, 10, 11])
CLOSE = series([9, 11, 10, 12, 11.5])


def test_stochastic_hand_calculated() -> None:
    out = ind.stochastic(HIGH, LOW, CLOSE, k_period=3, d_period=2)
    # i=2: 최고 12, 최저 8, 종가 10 → 50 / i=3: 13, 9, 12 → 75 / i=4: 13, 9, 11.5 → 62.5
    assert out["k"].isna().tolist()[:2] == [True, True]
    assert out["k"].iloc[2:].tolist() == [50.0, 75.0, 62.5]
    assert out["d"].iloc[3:].tolist() == [62.5, 68.75] and pd.isna(out["d"].iloc[2])
    slow = ind.stochastic(HIGH, LOW, CLOSE, k_period=3, d_period=2, smooth_k=2)
    assert slow["k"].iloc[3] == 62.5 and pd.isna(slow["k"].iloc[2])  # %K 를 2개 평균


def test_williams_r_hand_calculated() -> None:
    out = ind.williams_r(HIGH, LOW, CLOSE, 3)
    assert out.iloc[2:].tolist() == [-50.0, -25.0, -37.5]
    assert out.iloc[:2].isna().all()


def test_range_oscillators_on_flat_prices() -> None:
    flat = series([5.0] * 20)
    assert ind.stochastic(flat, flat, flat, 5, 3)["d"].dropna().eq(50.0).all()
    assert ind.williams_r(flat, flat, flat, 5).dropna().eq(-50.0).all()
    assert ind.cci(flat, flat, flat, 5).dropna().eq(0.0).all()
    a = ind.adx(flat, flat, flat, 5)
    assert np.isfinite(a.dropna().to_numpy()).all() and a["adx"].dropna().eq(0.0).all()


def test_cci_hand_calculated_and_against_loop() -> None:
    out = ind.cci(HIGH, LOW, CLOSE, 3)
    # TP = 9, 32/3, 10 → 평균 89/9, 평균절대편차 16/27 → (10 − 89/9) / (0.015 × 16/27) = 12.5
    assert out.iloc[2] == pytest.approx(12.5)
    df = random_ohlc(120)
    got = ind.cci(df["high"], df["low"], df["close"], 20)
    tp = ((df["high"] + df["low"] + df["close"]) / 3).to_numpy()
    for i in (19, 50, 119):
        window = tp[i - 19 : i + 1]
        mad = np.mean(np.abs(window - window.mean()))
        assert got.iloc[i] == pytest.approx((tp[i] - window.mean()) / (0.015 * mad), rel=1e-9)
    assert got.iloc[:19].isna().all()


def test_cci_block_computation_matches_single_block(monkeypatch) -> None:
    df = random_ohlc(300)
    whole = ind.cci(df["high"], df["low"], df["close"], 20)
    monkeypatch.setattr(ind, "_CCI_BLOCK_CELLS", 20 * 7)  # 7행씩 나눠 계산
    pd.testing.assert_series_equal(ind.cci(df["high"], df["low"], df["close"], 20), whole)


def _wilder(values: list[float], n: int) -> list[float]:
    out, avg, count = [], None, 0
    for x in values:
        if x != x:  # NaN: 아직 시작 전
            out.append(float("nan"))
            continue
        avg = x if avg is None else avg + (x - avg) / n
        count += 1
        out.append(avg if count >= n else float("nan"))
    return out


def test_adx_against_loop_implementation() -> None:
    df = random_ohlc(150, seed=4)
    h, lo, c = df["high"].tolist(), df["low"].tolist(), df["close"].tolist()
    n = 14
    nan = float("nan")
    plus_dm, minus_dm, tr = [nan], [nan], [nan]
    for i in range(1, len(h)):
        up, down = h[i] - h[i - 1], lo[i - 1] - lo[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))
    atr, sp, sm = _wilder(tr, n), _wilder(plus_dm, n), _wilder(minus_dm, n)
    pdi = [100 * a / b if b == b else nan for a, b in zip(sp, atr, strict=True)]
    mdi = [100 * a / b if b == b else nan for a, b in zip(sm, atr, strict=True)]
    dx = [100 * abs(p - m) / (p + m) if p == p else nan for p, m in zip(pdi, mdi, strict=True)]
    adx_loop = _wilder(dx, n)

    got = ind.adx(df["high"], df["low"], df["close"], n)
    for i in (n, 40, 99, 149):
        assert got["plus_di"].iloc[i] == pytest.approx(pdi[i], rel=1e-9)
        assert got["minus_di"].iloc[i] == pytest.approx(mdi[i], rel=1e-9)
    assert got["adx"].iloc[149] == pytest.approx(adx_loop[149], rel=1e-9)
    assert got["plus_di"].iloc[: n].isna().all() and got["plus_di"].iloc[n:].notna().all()
    assert got["adx"].iloc[: 2 * n - 1].isna().all() and got["adx"].iloc[2 * n - 1 :].notna().all()
    assert ((got.dropna() >= 0) & (got.dropna() <= 100)).all().all()


def test_ichimoku_uses_past_cloud() -> None:
    high = series([float(x) for x in range(10, 30)])
    low = high - 2
    out = ind.ichimoku(high, low, conversion=2, base=3, span_b=4, displacement=2)
    # 전환선(2): i=5 → (최고 15 + 최저 12) / 2 = 13.5, 기준선(3): (15 + 11) / 2 = 13
    assert out["tenkan"].iloc[5] == 13.5 and out["kijun"].iloc[5] == 13.0
    # 선행스팬은 2캔들 전에 계산한 값: span_a[7] = (tenkan[5] + kijun[5]) / 2
    assert out["span_a"].iloc[7] == pytest.approx((13.5 + 13.0) / 2)
    # span_b(4) 는 i=3 에 처음 계산 → 2캔들 뒤인 i=5 부터 현재 위치에 존재
    assert out["span_b"].iloc[:5].isna().all() and out["span_b"].iloc[5] == pytest.approx((13 + 8) / 2)


def test_obv_hand_calculated() -> None:
    close = series([10, 11, 11, 10, 12])
    volume = series([1, 2, 3, 4, 5])
    # 방향: 첫 캔들 0, +1, 0, −1, +1 → 누적 0, 2, 2, −2, 3
    assert ind.obv(close, volume).tolist() == [0.0, 2.0, 2.0, -2.0, 3.0]
    # 앞을 잘라내면 전체가 평행 이동한다 (차이는 일정)
    diff = ind.obv(close, volume).iloc[2:].to_numpy() - ind.obv(close.iloc[2:], volume.iloc[2:]).to_numpy()
    assert np.all(diff == diff[0])


def test_rising_edge_and_level_crosses() -> None:
    cond = pd.Series([False, True, True, False, True])
    assert ind.rising_edge(cond).tolist() == [False, True, False, False, True]
    assert ind.rising_edge(pd.Series([True, True, False])).tolist() == [False, False, False]  # 첫 캔들은 제외
    up = series([-120, -90, -110, -100, -80])
    assert ind.level_cross_up(up, -100).tolist() == [False, True, False, True, False]
    down = series([120, 90, 110, 100, 130])
    assert ind.level_cross_down(down, 100).tolist() == [False, True, False, True, False]
