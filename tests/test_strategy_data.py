"""캔들 DataFrame 유틸리티 테스트."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.core.exceptions import MarketDataError
from app.exchange.models import KST, Candle, CandleInterval
from app.strategy.data import (
    CANDLE_COLUMNS,
    candles_to_dataframe,
    detect_price_anomalies,
    drop_unclosed,
    load_candles_csv,
    resample_ohlcv,
    save_candles_csv,
    validate_candles,
)
from tests.test_models import CANDLE_MINUTE_JSON


def make_candle(t: datetime, close: float = 100.0, unit: int = 60) -> Candle:
    return Candle.model_validate(
        {
            **CANDLE_MINUTE_JSON,
            "candle_date_time_utc": t.strftime("%Y-%m-%dT%H:%M:%S"),
            "candle_date_time_kst": (t + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S"),
            "opening_price": close, "high_price": close * 1.01, "low_price": close * 0.99, "trade_price": close,
            "timestamp": int(t.timestamp() * 1000), "unit": unit,
        }
    )


T0 = datetime(2026, 3, 1, tzinfo=UTC)


def test_candles_to_dataframe_sorts_and_dedups() -> None:
    candles = [
        make_candle(T0 + timedelta(hours=2), 102), make_candle(T0, 100), make_candle(T0 + timedelta(hours=1), 101),
        make_candle(T0 + timedelta(hours=1), 111),  # 같은 시각 → 마지막 값 우선
    ]
    df = candles_to_dataframe(candles)
    assert list(df.columns) == CANDLE_COLUMNS
    assert df.index.tz is not None and df.index.is_monotonic_increasing
    assert df["close"].tolist() == [100.0, 111.0, 102.0]
    assert df.attrs == {"market": "KRW-BTC", "interval": "60m"}
    assert candles_to_dataframe([]).empty


def test_mixed_markets_rejected() -> None:
    a = make_candle(T0)
    b = Candle.model_validate({**CANDLE_MINUTE_JSON, "market": "KRW-ETH"})
    with pytest.raises(MarketDataError):
        candles_to_dataframe([a, b])


def test_csv_round_trip(tmp_path) -> None:
    df = candles_to_dataframe([make_candle(T0 + timedelta(hours=i), 100 + i) for i in range(5)])
    path = save_candles_csv(df, tmp_path / "KRW-BTC_60m.csv")
    text = path.read_text(encoding="utf-8").splitlines()
    assert text[0] == "time_utc,time_kst,open,high,low,close,volume,value"
    assert text[1].startswith("2026-03-01T00:00:00,2026-03-01T09:00:00,")
    loaded = load_candles_csv(path)
    pd.testing.assert_frame_equal(loaded, df, check_freq=False)
    assert loaded.attrs == {"market": "KRW-BTC", "interval": "60m"}
    with pytest.raises(MarketDataError):
        load_candles_csv(tmp_path / "missing.csv")


def test_validate_candles_errors() -> None:
    good = candles_to_dataframe([make_candle(T0 + timedelta(hours=i)) for i in range(3)])
    validate_candles(good, interval="60m")
    with pytest.raises(MarketDataError, match="비어"):
        validate_candles(good.iloc[0:0])
    bad = good.copy()
    bad.loc[bad.index[1], "high"] = 1.0  # 고가 < 저가
    with pytest.raises(MarketDataError, match="모순"):
        validate_candles(bad)
    naive = good.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(MarketDataError, match="시간대"):
        validate_candles(naive)
    with pytest.raises(MarketDataError, match="오름차순"):
        validate_candles(good.iloc[::-1])
    dup = pd.concat([good, good.iloc[[0]]]).sort_index()
    with pytest.raises(MarketDataError, match="중복"):
        validate_candles(dup)
    with pytest.raises(MarketDataError, match="간격"):
        validate_candles(good, interval="1d")


def test_detect_price_anomalies() -> None:
    df = candles_to_dataframe([make_candle(T0 + timedelta(hours=i), c) for i, c in enumerate([100, 101, 200, 199])])
    flags = detect_price_anomalies(df, max_pct_change=0.3)
    assert flags.tolist() == [False, False, True, False]


def test_drop_unclosed() -> None:
    df = candles_to_dataframe([make_candle(T0 + timedelta(hours=i)) for i in range(3)])
    now_inside = T0 + timedelta(hours=2, minutes=30)  # 마지막 캔들(02:00) 진행 중
    assert len(drop_unclosed(df, CandleInterval.M60, now_inside)) == 2
    now_after = T0 + timedelta(hours=3)
    assert len(drop_unclosed(df, "60m", now_after)) == 3
    assert drop_unclosed(df, "60m", now_inside).attrs == df.attrs


def test_resample() -> None:
    df = candles_to_dataframe([make_candle(T0 + timedelta(hours=i), 100 + i) for i in range(6)])
    out = resample_ohlcv(df, "3h")
    assert len(out) == 2
    assert out["open"].tolist() == [100.0, 103.0]
    assert out["close"].tolist() == [102.0, 105.0]
    assert out["volume"].iloc[0] == pytest.approx(df["volume"].iloc[:3].sum())
    assert out.index[0].astimezone(KST).hour == 9
