"""백테스트용 과거 캔들 로더: CSV 파일 또는 REST API(캐시 저장).

- ``csv`` 를 주면 그 파일을 읽어 [start, end] 구간만 자른다.
- 아니면 ``data/cache/{market}_{interval}_{start}_{end}.csv`` 캐시를 찾고, 없으면 REST 로 받아 저장한다.
- 항상 닫힌 캔들만 남기고(``drop_unclosed``) 무결성 검증을 거친다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from app.config.settings import Settings
from app.core.exceptions import MarketDataError
from app.exchange.models import CandleInterval
from app.exchange.upbit_client import UpbitClient
from app.strategy.data import (
    candles_to_dataframe,
    drop_unclosed,
    load_candles_csv,
    save_candles_csv,
    validate_candles,
)

log = logging.getLogger(__name__)
DEFAULT_CACHE_DIR = Path("data") / "cache"


def cache_path(market: str, interval: CandleInterval, start: datetime, end: datetime, cache_dir: Path) -> Path:
    name = f"{market}_{interval.value}_{start.astimezone(UTC):%Y%m%dT%H%M}_{end.astimezone(UTC):%Y%m%dT%H%M}.csv"
    return cache_dir / name


def slice_range(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    out = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))].copy()
    out.attrs = dict(df.attrs)
    return out


async def load_candles(
    settings: Settings,
    market: str,
    interval: CandleInterval | str,
    start: datetime,
    end: datetime | None = None,
    *,
    csv: Path | str | None = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    use_cache: bool = True,
) -> pd.DataFrame:
    interval = CandleInterval.parse(interval)
    if start.tzinfo is None or (end is not None and end.tzinfo is None):
        raise ValueError("start/end 는 시간대가 있는 datetime 이어야 합니다")
    now = datetime.now(UTC)
    end = end or now
    if start >= end:
        raise ValueError("start 는 end 보다 앞서야 합니다")

    if csv is not None:
        df = load_candles_csv(csv, market=market, interval=interval.value)
        df = slice_range(df, start, end)
        if df.empty:
            raise MarketDataError(f"CSV 에 {start:%Y-%m-%d} ~ {end:%Y-%m-%d} 구간 캔들이 없습니다: {csv}")
        source = f"csv:{csv}"
    else:
        path = cache_path(market, interval, start, end, Path(cache_dir))
        if use_cache and path.exists():
            df = load_candles_csv(path, market=market, interval=interval.value)
            source = f"cache:{path}"
        else:
            async with UpbitClient.from_settings(settings) as client:
                candles = await client.get_candles_range(market, interval, start=start, end=end)
            df = candles_to_dataframe(candles, interval=interval)
            df = drop_unclosed(df, interval, now)
            if df.empty:
                raise MarketDataError(f"{market} {interval.value} 구간에 캔들이 없습니다")
            if use_cache:
                save_candles_csv(df, path)
            source = f"api ({len(df)}개 수신, 캐시 저장 {path})"
    df = drop_unclosed(df, interval, now)
    df.attrs["market"] = market
    df.attrs["interval"] = interval.value
    validate_candles(df, interval=interval)
    log.info("캔들 로드: %s %s %d개 (%s)", market, interval.value, len(df), source)
    return df
