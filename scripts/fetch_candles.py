"""과거 캔들을 받아 CSV 로 저장한다 (백테스트용 데이터 수집, Phase 4 준비).

사용 예 (프로젝트 루트에서):
    python scripts/fetch_candles.py KRW-BTC --interval 60m --start 2026-01-01 --end 2026-09-01
    python scripts/fetch_candles.py KRW-ETH --interval 1d --start 2024-01-01 --out data/eth_daily.csv

- 시각은 KST 기준 날짜(YYYY-MM-DD) 또는 ISO 8601 로 입력한다. 시간대를 안 쓰면 KST 로 해석한다.
- 200개씩 나눠 받으며 Rate Limit(캔들 그룹 초당 10회)을 자동으로 지킨다.
- 출력 CSV 는 과거→최신 순, 열: time_utc,time_kst,open,high,low,close,volume,value
- 이 스크립트는 공개 시세 API 만 호출한다 (API Key 불필요, 주문 없음).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.exchange.models import KST, CandleInterval  # noqa: E402
from app.exchange.upbit_client import UpbitClient  # noqa: E402
from app.strategy.data import candles_to_dataframe, drop_unclosed, save_candles_csv, validate_candles  # noqa: E402


def parse_datetime(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value.replace(tzinfo=KST) if value.tzinfo is None else value


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    interval = CandleInterval.parse(args.interval)
    start = parse_datetime(args.start)
    end = parse_datetime(args.end) if args.end else datetime.now(UTC)
    out = Path(args.out) if args.out else Path("data") / f"{args.market}_{interval.value}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"{args.market} {interval.value} 캔들 수집: {start.isoformat()} ~ {end.isoformat()}")
    async with UpbitClient.from_settings(settings) as client:
        candles = await client.get_candles_range(args.market, interval, start=start, end=end)

    df = candles_to_dataframe(candles, interval=interval)
    df = drop_unclosed(df, interval)  # 진행 중인 마지막 캔들은 저장하지 않는다
    if df.empty:
        print("받은 캔들이 없습니다.")
        return 1
    validate_candles(df, interval=interval)
    save_candles_csv(df, out)
    print(f"{len(df)}개 저장: {out}")
    first, last = df.index[0].astimezone(KST), df.index[-1].astimezone(KST)
    print(f"  첫 캔들(KST) {first:%Y-%m-%d %H:%M}  마지막(KST) {last:%Y-%m-%d %H:%M}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="업비트 과거 캔들 CSV 수집 (공개 API, 주문 없음)")
    parser.add_argument("market", help="마켓 코드, 예: KRW-BTC")
    parser.add_argument("--interval", default="60m", help="1s,1m,3m,5m,10m,15m,30m,60m,240m,1d,1w,1M,1y")
    parser.add_argument("--start", required=True, help="시작 시각 (KST), 예: 2026-01-01")
    parser.add_argument("--end", help="종료 시각 (KST). 생략 시 현재")
    parser.add_argument("--out", help="출력 CSV 경로. 기본 data/{market}_{interval}.csv")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
