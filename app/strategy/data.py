"""캔들 데이터 준비: 모델 목록 ↔ DataFrame, CSV 입출력, 무결성 검증, 미완성 캔들 제거.

DataFrame 규약 (전략·백테스트가 공유):
- index: ``time`` — 캔들 시작 시각, tz-aware UTC, 오름차순(과거→최신), 중복 없음
- columns: ``open, high, low, close, volume, value`` (value = 캔들 누적 거래대금)
- ``df.attrs["market"]`` 에 마켓 코드, ``df.attrs["interval"]`` 에 캔들 단위 문자열
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.exceptions import MarketDataError
from app.exchange.models import KST, Candle, CandleInterval

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume", "value"]
CSV_COLUMNS = ["time_utc", "time_kst", *CANDLE_COLUMNS]


def empty_frame(market: str = "", interval: str = "") -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="float64") for c in CANDLE_COLUMNS})
    df.index = pd.DatetimeIndex([], tz="UTC", name="time")
    df.attrs["market"] = market
    df.attrs["interval"] = interval
    return df


def candles_to_dataframe(candles: Iterable[Candle], *, interval: CandleInterval | str | None = None) -> pd.DataFrame:
    """REST/WebSocket 캔들 모델 목록을 규약 DataFrame 으로 바꾼다 (정렬·중복 제거, 마지막 값 우선)."""
    rows = list(candles)
    if not rows:
        return empty_frame(interval=CandleInterval.parse(interval).value if interval else "")
    markets = {c.market for c in rows}
    if len(markets) > 1:
        raise MarketDataError(f"여러 마켓의 캔들이 섞여 있습니다: {sorted(markets)}")
    df = pd.DataFrame(
        {
            "time": [c.candle_date_time_utc for c in rows],
            "open": [c.opening_price for c in rows],
            "high": [c.high_price for c in rows],
            "low": [c.low_price for c in rows],
            "close": [c.trade_price for c in rows],
            "volume": [c.candle_acc_trade_volume for c in rows],
            "value": [c.candle_acc_trade_price for c in rows],
        }
    )
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.drop_duplicates(subset="time", keep="last").sort_values("time").set_index("time")
    df = df.astype("float64")
    df.attrs["market"] = markets.pop()
    unit = rows[0].unit
    df.attrs["interval"] = (
        CandleInterval.parse(interval).value if interval else (f"{unit}m" if unit else "")
    )
    return df


def save_candles_csv(df: pd.DataFrame, path: Path | str) -> Path:
    """``time_utc,time_kst,open,high,low,close,volume,value`` 형식으로 저장한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[CANDLE_COLUMNS].copy()
    out.insert(0, "time_kst", df.index.tz_convert(KST).strftime("%Y-%m-%dT%H:%M:%S"))
    out.insert(0, "time_utc", df.index.tz_convert(UTC).strftime("%Y-%m-%dT%H:%M:%S"))
    out.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")
    return path


def load_candles_csv(path: Path | str, *, market: str = "", interval: str = "") -> pd.DataFrame:
    """``save_candles_csv`` / ``scripts/fetch_candles.py`` 가 만든 CSV 를 읽는다."""
    path = Path(path)
    if not path.exists():
        raise MarketDataError(f"캔들 CSV 가 없습니다: {path}")
    raw = pd.read_csv(path)
    missing = [c for c in ("time_utc", *CANDLE_COLUMNS) if c not in raw.columns]
    if missing:
        raise MarketDataError(f"캔들 CSV 에 열이 없습니다 {missing}: {path}")
    df = raw[["time_utc", *CANDLE_COLUMNS]].copy()
    df["time"] = pd.to_datetime(df["time_utc"], utc=True)
    df = df.drop(columns="time_utc").drop_duplicates(subset="time", keep="last").sort_values("time").set_index("time")
    df = df.astype("float64")
    df.attrs["market"] = market or _guess_market(path)
    df.attrs["interval"] = interval or _guess_interval(path)
    return df


def _guess_market(path: Path) -> str:
    parts = path.stem.split("_")
    return parts[0] if parts and "-" in parts[0] else ""


def _guess_interval(path: Path) -> str:
    parts = path.stem.split("_")
    if len(parts) >= 2:
        try:
            return CandleInterval.parse(parts[1]).value
        except ValueError:
            return ""
    return ""


def validate_candles(df: pd.DataFrame, *, interval: CandleInterval | str | None = None) -> None:
    """규약 위반·비정상 가격을 찾으면 ``MarketDataError``. 전략·백테스트 입력 전에 호출한다."""
    if df.empty:
        raise MarketDataError("캔들 데이터가 비어 있습니다")
    missing = [c for c in CANDLE_COLUMNS if c not in df.columns]
    if missing:
        raise MarketDataError(f"캔들 열이 없습니다: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise MarketDataError("캔들 index 는 시간대(UTC)가 있는 DatetimeIndex 여야 합니다")
    if not df.index.is_monotonic_increasing:
        raise MarketDataError("캔들이 시간 오름차순이 아닙니다")
    if df.index.has_duplicates:
        raise MarketDataError(f"중복된 캔들 시각이 있습니다: {df.index[df.index.duplicated()][:3].tolist()}")
    ohlc = df[["open", "high", "low", "close"]]
    if ohlc.isna().any().any():
        raise MarketDataError("OHLC 에 결측치가 있습니다")
    if (ohlc <= 0).any().any():
        raise MarketDataError("0 이하의 가격이 있습니다")
    bad_hl = df["high"] < df["low"]
    bad_h = df["high"] < df[["open", "close"]].max(axis=1)
    bad_l = df["low"] > df[["open", "close"]].min(axis=1)
    if (bad_hl | bad_h | bad_l).any():
        idx = df.index[bad_hl | bad_h | bad_l][:3].tolist()
        raise MarketDataError(f"고가/저가가 시가·종가와 모순됩니다: {idx}")
    if (df["volume"] < 0).any():
        raise MarketDataError("음수 거래량이 있습니다")
    if interval is not None and len(df) > 1:
        step = timedelta(seconds=CandleInterval.parse(interval).seconds)
        diffs = pd.Series(df.index).diff().dropna()
        if (diffs < step).any():
            raise MarketDataError(f"캔들 간격이 {step} 보다 짧은 구간이 있습니다")


def detect_price_anomalies(df: pd.DataFrame, *, max_pct_change: float = 0.3) -> pd.Series:
    """직전 종가 대비 ``max_pct_change`` 이상 튄 캔들을 표시한다 (비정상 시세 감지용)."""
    change = df["close"].pct_change().abs()
    return (change > max_pct_change).fillna(False).rename("anomaly")


def drop_unclosed(df: pd.DataFrame, interval: CandleInterval | str, now: datetime | None = None) -> pd.DataFrame:
    """아직 진행 중인(닫히지 않은) 마지막 캔들을 제거한다.

    전략은 **닫힌 캔들만** 봐야 한다. 진행 중 캔들의 종가는 계속 바뀌므로 신호가 흔들리고,
    백테스트에서는 존재하지 않는 정보(Look-ahead)가 된다.
    """
    if df.empty:
        return df
    now = now or datetime.now(UTC)
    step = timedelta(seconds=CandleInterval.parse(interval).seconds)
    last_start = df.index[-1].to_pydatetime()
    if last_start + step > now:
        out = df.iloc[:-1].copy()
        out.attrs = dict(df.attrs)
        return out
    return df


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """더 큰 캔들로 합친다 (예: 1분봉 → '15min'). 시가=첫값, 고가=최대, 저가=최소, 종가=마지막, 거래량·대금=합."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "value": "sum"}
    out = df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open"])
    out.attrs = dict(df.attrs)
    out.attrs["interval"] = rule
    return out


def as_float_array(series: pd.Series) -> np.ndarray:
    return series.to_numpy(dtype="float64")
