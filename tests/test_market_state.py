"""시장 상태(캔들 롤링 저장·시세) 테스트."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.exchange.models import CandleInterval
from app.exchange.ws_models import parse_ws_message
from app.trading.market_state import MarketState, PriceState
from tests.test_strategy_data import make_candle
from tests.test_websocket import ORDERBOOK_JSON, TICKER_JSON, TRADE_JSON

T0 = datetime(2026, 5, 1, tzinfo=UTC)


def hourly(n: int, start: datetime = T0, price: float = 100.0):
    return [make_candle(start + timedelta(hours=i), price + i) for i in range(n)]


class TestCandles:
    def test_merge_reports_new_closed_candles_only(self) -> None:
        state = MarketState(CandleInterval.M60, max_rows=100)
        now = T0 + timedelta(hours=5, minutes=30)  # 05:00 캔들은 진행 중
        first = state.merge_candles("KRW-BTC", hourly(6), now)
        assert [t.hour for t in first] == [0, 1, 2, 3, 4]
        assert state.last_closed_time("KRW-BTC") == T0 + timedelta(hours=4)

        # 같은 데이터 다시 → 새 캔들 없음
        assert state.merge_candles("KRW-BTC", hourly(6), now) == []
        # 시간이 지나 05:00 캔들이 닫힘
        later = T0 + timedelta(hours=6, seconds=3)
        assert [t.hour for t in state.merge_candles("KRW-BTC", hourly(6), later)] == [5]
        df = state.frame("KRW-BTC")
        assert len(df) == 6 and df.attrs == {"market": "KRW-BTC", "interval": "60m"}
        assert df["close"].tolist() == [100, 101, 102, 103, 104, 105]

    def test_last_value_wins_and_rows_are_capped(self) -> None:
        state = MarketState(CandleInterval.M60, max_rows=4)
        now = T0 + timedelta(days=1)
        state.merge_candles("KRW-BTC", hourly(6), now)
        assert len(state.frame("KRW-BTC")) == 4
        revised = [make_candle(T0 + timedelta(hours=5), 999.0)]
        assert state.merge_candles("KRW-BTC", revised, now) == []  # 이미 알던 시각
        assert state.frame("KRW-BTC")["close"].iloc[-1] == 999.0  # 값은 갱신
        assert state.merge_candles("KRW-BTC", [], now) == []
        assert state.frame("KRW-ETH") is None and state.last_closed_time("KRW-ETH") is None


class TestPrices:
    def test_updates_from_ws_messages(self) -> None:
        state = MarketState(CandleInterval.M60)
        assert state.update_price(parse_ws_message(json.dumps(TICKER_JSON))) is True
        p = state.price("KRW-BTC")
        assert p.last_price == TICKER_JSON["trade_price"] and p.best_bid is None
        assert state.update_price(parse_ws_message(json.dumps(ORDERBOOK_JSON))) is True
        assert (p.best_ask, p.best_bid) == (109950000.0, 109880000.0)
        assert p.mark_price == pytest.approx((109950000.0 + 109880000.0) / 2)
        assert state.update_price(parse_ws_message(json.dumps(TRADE_JSON))) is True
        assert p.last_price == TRADE_JSON["trade_price"] and p.best_ask == TRADE_JSON["best_ask_price"]
        assert state.update_price(parse_ws_message('{"status":"UP"}')) is False
        assert state.mark_prices() == {"KRW-BTC": p.mark_price}

    def test_freshness_and_manual_price(self) -> None:
        state = MarketState(CandleInterval.M60)
        now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        assert state.price("KRW-XRP") is None
        state.set_last_price("KRW-XRP", 500.0, now - timedelta(seconds=10))
        p = state.price("KRW-XRP")
        assert p.is_fresh(now, 30) is True and p.is_fresh(now, 5) is False
        assert p.mark_price == 500.0
        assert PriceState("KRW-X").is_fresh(now, 30) is False
