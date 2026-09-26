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
- ADX / ±DI: Wilder 방식 (±DM·TR 을 alpha=1/n 으로 평활, ADX = DX 의 평활)
- Stochastic %K·%D, Williams %R: 현재 캔들을 포함한 최근 n개 고가·저가 범위 안에서 종가의 위치
- CCI: (TP - SMA(TP)) / (0.015 × 평균절대편차), TP = (고가+저가+종가)/3
- 일목균형표: 선행스팬은 ``shift(+displacement)`` (과거에 계산된 구름을 현재 위치로), 후행스팬은 쓰지 않는다
- OBV: 종가 방향(±1)×거래량 누적합
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


def level_cross_up(series: pd.Series, level: float) -> pd.Series:
    """직전에는 ``level`` 미만이었다가 지금 ``level`` 이상이 된 시점 (과매도선 회복)."""
    prev = series.shift(1)
    return ((prev < level) & (series >= level)).fillna(False).astype(bool)


def level_cross_down(series: pd.Series, level: float) -> pd.Series:
    """직전에는 ``level`` 초과였다가 지금 ``level`` 이하가 된 시점 (과매수선 이탈)."""
    prev = series.shift(1)
    return ((prev > level) & (series <= level)).fillna(False).astype(bool)


def rising_edge(cond: pd.Series) -> pd.Series:
    """직전 캔들에는 거짓이었다가 이번 캔들에 참이 된 시점. 첫 캔들은 직전 값을 모르므로 제외한다.

    '정배열이다' 같은 상태 조건을 '정배열이 새로 완성됐다' 는 사건으로 바꿀 때 쓴다 — 조건이 이어지는 동안 같은
    신호를 반복하지 않는다 (교차 신호와 같은 방식이라 보유 중 중복 매수 신호·미보유 매도 신호가 쌓이지 않는다).
    """
    now = cond.fillna(False).astype(bool)
    return now & ~now.shift(1, fill_value=True)


def pct_change(series: pd.Series, periods: int = 1) -> pd.Series:
    return series.pct_change(periods=periods)


def _range_position(close: pd.Series, lowest: pd.Series, highest: pd.Series) -> pd.Series:
    """(종가 − 최저) / (최고 − 최저) ∈ [0, 1]. 폭이 0(가격 정체)이면 0.5, 최고·최저가 아직 없으면(워밍업) NaN."""
    span = highest - lowest
    position = (close - lowest) / span.where(span > 0)
    return position.where(span != 0, 0.5)


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """분모가 0이면 0, 분모가 NaN(워밍업)이면 NaN 인 나눗셈 — 무한대가 신호·DB 기록에 섞이지 않게."""
    ratio = numerator / denominator.where(denominator > 0)
    return ratio.where(denominator != 0, 0.0)


def _midpoint(high: pd.Series, low: pd.Series, window: int) -> pd.Series:
    """최근 ``window`` 개(현재 포함) 고가 최고와 저가 최저의 중간값 (일목균형표 선들)."""
    _require_window(window)
    return (high.rolling(window, min_periods=window).max() + low.rolling(window, min_periods=window).min()) / 2.0


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3, smooth_k: int = 1
) -> pd.DataFrame:
    """스토캐스틱 (0~100). 열: ``k``(%K), ``d``(%D).

    %K = 100 × (종가 − 최근 k_period 최저가) / (최고가 − 최저가), 현재 캔들 포함. 폭이 0이면 50.
    ``smooth_k`` > 1 이면 %K 를 그 기간 SMA 로 평활한다 (Slow 스토캐스틱). %D = SMA(%K, d_period).
    """
    _require_window(k_period, "k_period")
    _require_window(d_period, "d_period")
    _require_window(smooth_k, "smooth_k")
    lowest = low.rolling(k_period, min_periods=k_period).min()
    highest = high.rolling(k_period, min_periods=k_period).max()
    k = 100.0 * _range_position(close, lowest, highest)
    if smooth_k > 1:
        k = sma(k, smooth_k)
    return pd.DataFrame({"k": k, "d": sma(k, d_period)})


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Williams %R (−100~0) = −100 × (최근 최고가 − 종가) / (최고가 − 최저가), 현재 캔들 포함. 폭이 0이면 −50."""
    _require_window(window)
    lowest = low.rolling(window, min_periods=window).min()
    highest = high.rolling(window, min_periods=window).max()
    return (100.0 * _range_position(close, lowest, highest) - 100.0).rename("williams_r")


_CCI_BLOCK_CELLS = 2_000_000  # 평균절대편차 계산 시 한 번에 만드는 (행 × 기간) 배열 크기 상한 — 메모리 제한


def cci(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20, constant: float = 0.015) -> pd.Series:
    """CCI = (TP − SMA(TP)) / (constant × 평균절대편차(TP)), TP = (고가+저가+종가)/3. 편차가 0(가격 정체)이면 0.

    평균절대편차는 롤링 갱신식이 없어 캔들마다 최근 ``window`` 개를 직접 계산한다 (현재 캔들로 끝나는 구간만 사용).
    """
    _require_window(window)
    tp = ((high + low + close) / 3.0).to_numpy(dtype="float64")
    out = np.full(len(tp), np.nan)
    if len(tp) >= window:
        windows = np.lib.stride_tricks.sliding_window_view(tp, window)
        step = max(1, _CCI_BLOCK_CELLS // window)
        for start in range(0, len(windows), step):
            block = windows[start : start + step]
            mean = block.mean(axis=1)
            mad = np.abs(block - mean[:, None]).mean(axis=1)
            dev = tp[window - 1 + start : window - 1 + start + len(block)] - mean
            flat = mad <= np.abs(mean) * 1e-12  # 부동소수 오차로 0 이 아닌 극소값도 정체로 본다
            values = np.zeros(len(block))
            np.divide(dev, constant * mad, out=values, where=~flat)
            values[np.isnan(mad)] = np.nan
            out[window - 1 + start : window - 1 + start + len(block)] = values
    return pd.Series(out, index=close.index, name="cci")


def adx(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.DataFrame:
    """ADX 와 방향 지표 (0~100). 열: ``plus_di``(+DI), ``minus_di``(−DI), ``adx``.

    Wilder 방식: +DM(고가 상승폭)·−DM(저가 하락폭)과 True Range 를 alpha=1/window 로 평활해 ±DI 를 만들고,
    DX = 100·|+DI − −DI| / (+DI + −DI) 를 다시 평활한 값이 ADX 다. 첫 캔들은 직전 값이 없어 계산에서 빠지므로
    ±DI 는 ``window + 1`` 개, ADX 는 ``2 × window`` 개 캔들부터 나온다. ADX 는 방향이 아니라 추세의 **강도**다.
    """
    _require_window(window)
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index).where(up.notna())
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index).where(down.notna())
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1, skipna=False)
    smoothed_tr = wilder_ema(tr, window)
    plus_di = 100.0 * _safe_ratio(wilder_ema(plus_dm, window), smoothed_tr)
    minus_di = 100.0 * _safe_ratio(wilder_ema(minus_dm, window), smoothed_tr)
    dx = 100.0 * _safe_ratio((plus_di - minus_di).abs(), plus_di + minus_di)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": wilder_ema(dx, window)})


def ichimoku(
    high: pd.Series, low: pd.Series, conversion: int = 9, base: int = 26, span_b: int = 52, displacement: int = 26
) -> pd.DataFrame:
    """일목균형표 중 **현재 캔들 위치에 그려지는** 선. 열: ``tenkan``(전환선), ``kijun``(기준선), ``span_a``·``span_b``.

    - 전환선·기준선 = 최근 ``conversion``/``base`` 개 캔들(현재 포함) 고가 최고와 저가 최저의 중간값
    - 선행스팬 A = (전환선 + 기준선)/2, 선행스팬 B = 최근 ``span_b`` 개 중간값. 둘 다 계산한 캔들보다 ``displacement``
      만큼 **앞(미래)** 에 그리므로, 지금 캔들의 구름은 ``displacement`` 캔들 **전에** 계산된 값이다 →
      ``shift(+displacement)``. 차트처럼 ``shift(-displacement)`` 로 당겨 쓰면 미래 참조(Look-ahead)가 된다.
    - 후행스팬(현재 종가를 과거 위치에 그린 선)은 비교 시점 기준으로 미래 값이라 계산하지 않는다.
    """
    _require_window(displacement, "displacement")
    tenkan = _midpoint(high, low, conversion)
    kijun = _midpoint(high, low, base)
    return pd.DataFrame({
        "tenkan": tenkan,
        "kijun": kijun,
        "span_a": ((tenkan + kijun) / 2.0).shift(displacement),
        "span_b": _midpoint(high, low, span_b).shift(displacement),
    })


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On Balance Volume: 종가가 오른 캔들은 거래량을 더하고 내린 캔들은 뺀다(같으면 0). 첫 캔들은 0.

    OBV 의 절대값은 데이터 시작점에 따라 달라진다 — 앞쪽 캔들을 잘라내면 전체가 같은 값만큼 평행 이동한다
    (실시간 엔진은 최근 캔들만 들고 있다). 그래서 전략은 OBV 와 그 이동평균의 차이·교차처럼 평행 이동에
    영향받지 않는 비교만 써야 백테스트와 실거래 신호가 같아진다.
    """
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum().rename("obv")
