"""기술적 지표 (pandas Series 입력 → Series/DataFrame 출력).

모든 지표는 **인과적(causal)** 이다: i번째 값은 i번째 이전(포함) 데이터만 사용한다.
``rolling`` / ``ewm`` 만 쓰고 ``center=True``, ``shift(-n)`` 같은 미래 참조는 금지한다.
워밍업 구간(데이터 부족)은 NaN 으로 남겨 전략이 HOLD 로 처리하게 한다.

수식 기준:
- SMA: 단순 이동평균
- EMA: 지수 이동평균, ``adjust=False`` (재귀식, 프리픽스 계산과 전체 계산이 일치)
- RSI: Wilder 방식 (평균 상승/하락폭을 alpha=1/n EMA 로 평활)
- MACD: EMA(fast) - EMA(slow), 시그널 = EMA(MACD, signal), 히스토그램 = MACD - 시그널
- Bollinger: SMA ± k·표준편차(모집단, ddof=0)
- ATR: True Range 의 Wilder EMA
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _require_window(window: int, name: str = "window") -> None:
    if not isinstance(window, int) or window < 1:
        raise ValueError(f"{name} 는 1 이상의 정수여야 합니다: {window!r}")


def sma(series: pd.Series, window: int) -> pd.Series:
    """단순 이동평균."""
    _require_window(window)
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    """지수 이동평균 (adjust=False, span=window)."""
    _require_window(window)
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def wilder_ema(series: pd.Series, window: int) -> pd.Series:
    """Wilder 평활 (alpha = 1/window). RSI·ATR 에 쓴다."""
    _require_window(window)
    return series.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """RSI (0~100). 하락폭 평균이 0이면 100, 상승·하락 모두 0이면 50."""
    _require_window(window)
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = wilder_ema(gain, window)
    avg_loss = wilder_ema(loss, window)
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    out[avg_gain.isna() | avg_loss.isna()] = np.nan
    return out.rename("rsi")


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD. 열: ``macd``, ``signal``, ``hist``."""
    if fast >= slow:
        raise ValueError(f"fast({fast}) 는 slow({slow}) 보다 작아야 합니다")
    line = ema(series, fast) - ema(series, slow)
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": signal_line, "hist": line - signal_line})


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """볼린저 밴드. 열: ``middle``, ``upper``, ``lower``, ``bandwidth``."""
    _require_window(window)
    middle = sma(series, window)
    std = series.rolling(window, min_periods=window).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return pd.DataFrame({"middle": middle, "upper": upper, "lower": lower, "bandwidth": (upper - lower) / middle})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1)
    return ranges.max(axis=1, skipna=False).fillna(high - low)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """평균 실질 범위 (Wilder). 손절폭·포지션 크기 계산에 쓴다."""
    return wilder_ema(true_range(high, low, close), window).rename("atr")


def rolling_high(series: pd.Series, window: int) -> pd.Series:
    """직전 ``window`` 개(현재 캔들 제외)의 최고가 — 돌파(Breakout) 판단용."""
    _require_window(window)
    return series.shift(1).rolling(window, min_periods=window).max()


def rolling_low(series: pd.Series, window: int) -> pd.Series:
    _require_window(window)
    return series.shift(1).rolling(window, min_periods=window).min()


def cross_above(a: pd.Series, b: pd.Series) -> pd.Series:
    """직전에는 a <= b 였다가 지금 a > b 가 된 시점 (골든크로스)."""
    prev = (a.shift(1) <= b.shift(1))
    return (prev & (a > b)).fillna(False).astype(bool)


def cross_below(a: pd.Series, b: pd.Series) -> pd.Series:
    """직전에는 a >= b 였다가 지금 a < b 가 된 시점 (데드크로스)."""
    prev = (a.shift(1) >= b.shift(1))
    return (prev & (a < b)).fillna(False).astype(bool)


def pct_change(series: pd.Series, periods: int = 1) -> pd.Series:
    return series.pct_change(periods=periods)
