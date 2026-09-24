"""응답 모델 테스트 — 공식 문서 예시 응답을 그대로 파싱한다."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.exchange.models import KST, Account, Candle, CandleInterval, Market, Ticker

TICKER_JSON = {
    "market": "KRW-BTC",
    "trade_date": "20240101",
    "trade_time": "120000",
    "trade_date_kst": "20240101",
    "trade_time_kst": "210000",
    "trade_timestamp": 1704110400000,
    "opening_price": 56000000.0,
    "high_price": 57000000.0,
    "low_price": 55000000.0,
    "trade_price": 56500000.0,
    "prev_closing_price": 56000000.0,
    "change": "RISE",
    "change_price": 500000.0,
    "change_rate": 0.0089285714,
    "signed_change_price": 500000.0,
    "signed_change_rate": 0.0089285714,
    "trade_volume": 0.001,
    "acc_trade_price": 1.0e11,
    "acc_trade_price_24h": 2.0e11,
    "acc_trade_volume": 1800.5,
    "acc_trade_volume_24h": 3600.1,
    "highest_52_week_price": 60000000.0,
    "highest_52_week_date": "2023-12-01",
    "lowest_52_week_price": 20000000.0,
    "lowest_52_week_date": "2023-01-01",
    "timestamp": 1704110400123,
    "some_future_field": "kept",
}

CANDLE_MINUTE_JSON = {
    "market": "KRW-BTC",
    "candle_date_time_utc": "2024-01-01T12:00:00",
    "candle_date_time_kst": "2024-01-01T21:00:00",
    "opening_price": 56000000.0,
    "high_price": 56100000.0,
    "low_price": 55900000.0,
    "trade_price": 56050000.0,
    "timestamp": 1704110459999,
    "candle_acc_trade_price": 123456789.0,
    "candle_acc_trade_volume": 2.2,
    "unit": 60,
}

ACCOUNT_JSON = {
    "currency": "BTC",
    "balance": "0.00050000",
    "locked": "0.00000000",
    "avg_buy_price": "145500000",
    "avg_buy_price_modified": False,
    "unit_currency": "KRW",
}


def test_ticker_parses_and_keeps_unknown_fields() -> None:
    t = Ticker.model_validate(TICKER_JSON)
    assert t.trade_price == 56500000.0
    assert t.trade_datetime_utc == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert t.trade_datetime_kst.hour == 21
    assert t.model_extra == {"some_future_field": "kept"}


def test_candle_attaches_timezones_and_aliases() -> None:
    c = Candle.model_validate(CANDLE_MINUTE_JSON)
    assert c.candle_date_time_utc == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert c.candle_date_time_kst.tzinfo is KST
    assert c.candle_date_time_kst.utcoffset().total_seconds() == 9 * 3600
    assert c.candle_date_time_kst == c.candle_date_time_utc  # 같은 순간
    assert (c.open, c.high, c.low, c.close, c.volume) == (56000000.0, 56100000.0, 55900000.0, 56050000.0, 2.2)
    assert c.unit == 60
    assert c.prev_closing_price is None


def test_candle_is_immutable() -> None:
    c = Candle.model_validate(CANDLE_MINUTE_JSON)
    with pytest.raises(ValidationError):
        c.trade_price = 1.0  # type: ignore[misc]


def test_account_uses_decimal() -> None:
    a = Account.model_validate(ACCOUNT_JSON)
    assert a.balance == Decimal("0.00050000")
    assert isinstance(a.avg_buy_price, Decimal)
    assert a.total == Decimal("0.00050000")
    assert a.is_fiat is False
    assert Account.model_validate({**ACCOUNT_JSON, "currency": "KRW", "balance": "1000000"}).is_fiat


def test_market_properties() -> None:
    m = Market.model_validate(
        {"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin",
         "market_event": {"warning": True, "caution": {"PRICE_FLUCTUATIONS": False}}}
    )
    assert (m.quote_currency, m.base_currency) == ("KRW", "BTC")
    assert m.has_warning is True
    assert Market.model_validate({"market": "BTC-ETH", "korean_name": "x", "english_name": "y"}).has_warning is False


class TestCandleInterval:
    @pytest.mark.parametrize(
        "raw, path",
        [
            ("1s", "/v1/candles/seconds"),
            ("1m", "/v1/candles/minutes/1"),
            ("15m", "/v1/candles/minutes/15"),
            ("60m", "/v1/candles/minutes/60"),
            ("1h", "/v1/candles/minutes/60"),
            ("4h", "/v1/candles/minutes/240"),
            ("1d", "/v1/candles/days"),
            ("1w", "/v1/candles/weeks"),
            ("1M", "/v1/candles/months"),
            ("1y", "/v1/candles/years"),
        ],
    )
    def test_parse_and_path(self, raw: str, path: str) -> None:
        assert CandleInterval.parse(raw).path == path

    def test_seconds(self) -> None:
        assert CandleInterval.M1.seconds == 60
        assert CandleInterval.M240.seconds == 4 * 3600
        assert CandleInterval.D1.seconds == 86400

    @pytest.mark.parametrize("raw", ["2m", "7d", "", "minute60"])
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(ValueError):
            CandleInterval.parse(raw)
