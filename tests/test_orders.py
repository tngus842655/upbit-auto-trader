"""가상 브로커 테스트."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.trading.market_state import PriceState
from app.trading.orders import OrderRequest, OrderStatus, PaperBroker
from app.trading.portfolio import Portfolio, Side

NOW = datetime(2026, 5, 1, 9, 0, 5, tzinfo=UTC)
SIGNAL_T = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)


def fresh_price(**kwargs) -> PriceState:
    base = {"market": "KRW-BTC", "last_price": 100_000, "last_time": NOW, "best_bid": 99_900, "best_ask": 100_100,
            "book_time": NOW}
    base.update(kwargs)
    return PriceState(**base)


async def test_buy_uses_best_ask_and_sell_uses_best_bid() -> None:
    portfolio = Portfolio(1_000_000, fee_rate=0.0005)
    broker = PaperBroker(portfolio, slippage_rate=0.001)
    buy = await broker.execute(
        OrderRequest(
            "KRW-BTC", Side.BUY, amount=500_000, reason="골든크로스", strategy="ma_cross", signal_time=SIGNAL_T
        ),
        fresh_price(), NOW,
    )
    assert buy.status is OrderStatus.FILLED
    assert buy.fill_price == pytest.approx(100_100 * 1.001)
    assert buy.client_id == f"paper:KRW-BTC:BUY:{SIGNAL_T.isoformat()}"
    assert buy.filled_quantity == portfolio.position("KRW-BTC").quantity
    assert buy.fee == pytest.approx(buy.filled_quantity * buy.fill_price * 0.0005)
    assert buy.reason == "골든크로스 [orderbook]"
    assert buy.filled_at == NOW and len(buy.fills) == 1
    assert buy.to_dict()["status"] == "FILLED"

    sell_request = OrderRequest("KRW-BTC", Side.SELL, signal_time=SIGNAL_T + timedelta(hours=1))
    sell = await broker.execute(sell_request, fresh_price(), NOW)
    assert sell.status is OrderStatus.FILLED
    assert sell.fill_price == pytest.approx(99_900 * 0.999)
    assert portfolio.position("KRW-BTC") is None
    assert len(portfolio.trades) == 1


async def test_falls_back_to_last_trade_when_book_missing_or_stale() -> None:
    broker = PaperBroker(Portfolio(1_000_000), slippage_rate=0.0, max_price_age_seconds=30)
    no_book = fresh_price(best_bid=None, best_ask=None, book_time=None)
    order = await broker.execute(OrderRequest("KRW-BTC", Side.BUY, amount=100_000), no_book, NOW)
    assert order.status is OrderStatus.FILLED and order.fill_price == 100_000 and "[last_trade]" in order.reason

    stale_book = fresh_price(book_time=NOW - timedelta(seconds=60))
    order2 = await broker.execute(OrderRequest("KRW-BTC", Side.SELL, signal_time=SIGNAL_T), stale_book, NOW)
    assert order2.status is OrderStatus.FILLED and order2.fill_price == 100_000


@pytest.mark.parametrize(
    "price, expected",
    [
        (None, "가격 정보 없음"),
        (PriceState("KRW-BTC"), "오래됨"),
        (PriceState("KRW-BTC", last_price=100.0, last_time=NOW - timedelta(minutes=5)), "오래됨"),
    ],
)
async def test_rejects_without_usable_price(price, expected) -> None:
    broker = PaperBroker(Portfolio(1_000_000))
    order = await broker.execute(OrderRequest("KRW-BTC", Side.BUY, amount=100_000), price, NOW)
    assert order.status is OrderStatus.REJECTED and expected in (order.error or "")
    assert broker.processed == set()


async def test_duplicate_client_id_is_rejected_even_after_restart() -> None:
    portfolio = Portfolio(1_000_000)
    broker = PaperBroker(portfolio)
    request = OrderRequest("KRW-BTC", Side.BUY, amount=100_000, signal_time=SIGNAL_T)
    first = await broker.execute(request, fresh_price(), NOW)
    assert first.status is OrderStatus.FILLED
    second = await broker.execute(request, fresh_price(), NOW)
    assert second.status is OrderStatus.REJECTED and "중복 주문" in second.error
    assert portfolio.position("KRW-BTC").quantity == first.filled_quantity

    restarted = PaperBroker(portfolio, processed_client_ids=broker.processed)
    third = await restarted.execute(request, fresh_price(), NOW)
    assert third.status is OrderStatus.REJECTED


async def test_portfolio_errors_become_rejections() -> None:
    portfolio = Portfolio(1_000_000)
    broker = PaperBroker(portfolio)
    sell = await broker.execute(OrderRequest("KRW-BTC", Side.SELL), fresh_price(), NOW)
    assert sell.status is OrderStatus.REJECTED and "포지션" in sell.error
    tiny = await broker.execute(OrderRequest("KRW-BTC", Side.BUY, amount=1_000), fresh_price(), NOW)
    assert tiny.status is OrderStatus.REJECTED and "최소 주문" in tiny.error
    assert portfolio.cash == 1_000_000
    with pytest.raises(ValueError):
        PaperBroker(portfolio, slippage_rate=0.5)


async def test_explicit_client_id_and_random_id_without_signal_time() -> None:
    broker = PaperBroker(Portfolio(1_000_000))
    explicit_request = OrderRequest("KRW-BTC", Side.BUY, amount=50_000, client_id="manual-1")
    explicit = await broker.execute(explicit_request, fresh_price(), NOW)
    assert explicit.client_id == "manual-1"
    anonymous = OrderRequest("KRW-BTC", Side.BUY, amount=50_000)
    assert anonymous.resolved_client_id("paper") != anonymous.resolved_client_id("paper")
