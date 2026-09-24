"""감사 CRITICAL-2 회귀 — 오래된 호가를 평가·손절 판정에 쓰지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.exchange.models import CandleInterval
from app.trading.market_state import MarketState, PriceState
from tests.test_engine import MARKET, T0, Harness

NOW = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)


def test_stale_book_falls_back_to_last_price() -> None:
    stale = PriceState("KRW-BTC", last_price=80.0, last_time=NOW, best_bid=99.0, best_ask=101.0,
                       book_time=NOW - timedelta(hours=3))
    assert stale.is_fresh(NOW, 30) is True  # 체결가는 신선하다
    assert stale.book_usable is False and stale.mark_price == 80.0  # 호가는 3시간 전 → 버린다

    fresh_book = PriceState("KRW-BTC", last_price=80.0, last_time=NOW, best_bid=99.0, best_ask=101.0,
                            book_time=NOW - timedelta(seconds=10))
    assert fresh_book.mark_price == 100.0  # 30초 안이면 호가 중간값

    newer_book = PriceState("KRW-BTC", last_price=80.0, last_time=NOW - timedelta(hours=1), best_bid=99.0,
                            best_ask=101.0, book_time=NOW)
    assert newer_book.mark_price == 100.0  # 호가가 더 최신이면 당연히 호가

    assert PriceState("KRW-BTC", best_bid=99.0, best_ask=101.0).mark_price == 100.0  # 시각 정보 없으면 호가
    assert PriceState("KRW-BTC", last_price=80.0, last_time=NOW).mark_price == 80.0  # 호가 없으면 체결가


def test_market_state_passes_max_age_to_price_state() -> None:
    state = MarketState(CandleInterval.M60, price_max_age_seconds=5)
    state.set_last_price("KRW-BTC", 1.0, NOW)
    assert state.price("KRW-BTC").book_max_age_seconds == 5


async def test_engine_stop_loss_uses_rest_price_when_book_is_stale(make_settings) -> None:
    """WebSocket 장애로 호가는 3시간째 그대로, REST 보정 체결가는 -27% → 손절이 체결가 기준으로 발동해야 한다."""
    h = Harness(make_settings, actions={T0 + timedelta(hours=10): "BUY"})
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)  # 호가 book_time = 지금
    await h.engine.process_closed_candles(h.now)
    assert h.portfolio.has_position(MARKET)

    h.now += timedelta(hours=3)  # 호가 갱신 없음
    h.engine.state.set_last_price(MARKET, 80.0, h.now)  # REST 보정
    price = h.engine.state.price(MARKET)
    assert price.is_fresh(h.now, 30) and price.mark_price == 80.0
    assert h.engine.current_equity() < 1_000_000 * 0.8  # 평가액도 체결가 기준
    orders = await h.engine.check_exits(h.now, force=True)
    assert len(orders) == 1 and orders[0].is_filled and orders[0].reason.startswith("stop_loss")
    assert not h.portfolio.has_position(MARKET)
