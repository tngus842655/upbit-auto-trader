"""모의 계좌(Portfolio) 테스트 — 수수료·수량 내림·손익 계산."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.trading.portfolio import Portfolio, PortfolioError, Side, floor_quantity

T0 = datetime(2026, 1, 1, tzinfo=UTC)
M = "KRW-BTC"


def test_floor_quantity() -> None:
    assert floor_quantity(0.123456789) == 0.12345678
    assert floor_quantity(1.0) == 1.0
    assert floor_quantity(0.1 + 0.2) == 0.3


class TestBuy:
    def test_buy_all_cash_respects_fee(self) -> None:
        p = Portfolio(1_000_000, fee_rate=0.0005)
        fill = p.buy(M, 100_000_000, time=T0, reason="test")
        expected_qty = floor_quantity(1_000_000 / (100_000_000 * 1.0005))
        assert fill.side is Side.BUY
        assert fill.quantity == expected_qty
        assert fill.amount == pytest.approx(expected_qty * 100_000_000)
        assert fill.fee == pytest.approx(fill.amount * 0.0005)
        assert p.cash == pytest.approx(1_000_000 - fill.amount - fill.fee)
        assert p.cash >= 0
        assert p.fees_paid == pytest.approx(fill.fee)
        pos = p.position(M)
        assert pos is not None and pos.quantity == expected_qty and pos.avg_price == 100_000_000
        assert pos.cost_basis == pytest.approx(fill.amount + fill.fee)
        assert p.equity({M: 100_000_000}) == pytest.approx(1_000_000 - fill.fee)

    def test_buy_amount_and_quantity(self) -> None:
        p = Portfolio(1_000_000)
        fill = p.buy(M, 50_000, time=T0, amount=200_000)
        assert fill.amount <= 200_000
        assert fill.amount + fill.fee <= 200_000 + 1e-6
        fill2 = p.buy(M, 60_000, time=T0 + timedelta(hours=1), quantity=1.5)
        assert fill2.quantity == 1.5
        pos = p.position(M)
        assert pos.quantity == pytest.approx(fill.quantity + 1.5)
        assert pos.avg_price == pytest.approx((fill.amount + 1.5 * 60_000) / pos.quantity)

    def test_buy_rejections(self) -> None:
        p = Portfolio(10_000, min_order_amount=5000)
        with pytest.raises(PortfolioError, match="최소 주문"):
            p.buy(M, 100_000, time=T0, amount=4_000)
        with pytest.raises(PortfolioError, match="현금 부족"):
            p.buy(M, 100_000, time=T0, quantity=1.0)
        with pytest.raises(PortfolioError):
            p.buy(M, 0, time=T0)
        assert p.positions == {} and p.fills == []

    def test_tiny_capital_cannot_buy(self) -> None:
        p = Portfolio(3_000)
        with pytest.raises(PortfolioError, match="최소 주문"):
            p.buy(M, 1_000, time=T0)


class TestSell:
    def test_full_round_trip_pnl(self) -> None:
        p = Portfolio(1_000_000, fee_rate=0.001)
        buy = p.buy(M, 100.0, time=T0, quantity=5000)
        sell = p.sell(M, 110.0, time=T0 + timedelta(days=1), reason="signal")
        assert sell.side is Side.SELL and sell.quantity == 5000
        assert sell.amount == pytest.approx(550_000) and sell.fee == pytest.approx(550)
        assert p.position(M) is None
        assert len(p.trades) == 1
        t = p.trades[0]
        assert t.entry_price == 100.0 and t.exit_price == 110.0 and t.exit_reason == "signal"
        assert t.pnl == pytest.approx(550_000 - 550 - 500_000 - 500)
        assert t.pnl_pct == pytest.approx(t.pnl / (500_000 + 500))
        assert t.is_win and t.holding_seconds == 86_400
        assert p.cash == pytest.approx(1_000_000 - 500_000 - 500 + 550_000 - 550)
        assert p.fees_paid == pytest.approx(buy.fee + sell.fee)
        assert p.summary()["realized_pnl"] == pytest.approx(t.pnl)

    def test_partial_sell_keeps_proportions(self) -> None:
        p = Portfolio(1_000_000, fee_rate=0.0)
        p.buy(M, 100.0, time=T0, quantity=1000)
        p.sell(M, 120.0, time=T0, quantity=400)
        pos = p.position(M)
        assert pos.quantity == pytest.approx(600)
        assert pos.entry_amount == pytest.approx(60_000)
        assert p.trades[0].pnl == pytest.approx(400 * 20)
        with pytest.raises(PortfolioError, match="최소 주문"):
            p.sell(M, 1.0, time=T0, quantity=100)  # 부분 매도 100원은 최소 주문 미달
        p.sell(M, 1.0, time=T0)  # 전량 청산은 허용
        assert p.position(M) is None

    def test_sell_without_position(self) -> None:
        p = Portfolio(1_000_000)
        with pytest.raises(PortfolioError, match="포지션"):
            p.sell(M, 100.0, time=T0)

    def test_loss_trade(self) -> None:
        p = Portfolio(1_000_000, fee_rate=0.0005)
        p.buy(M, 100.0, time=T0, quantity=1000)
        p.sell(M, 90.0, time=T0, reason="stop_loss")
        t = p.trades[0]
        assert not t.is_win
        assert t.pnl == pytest.approx(90_000 - 45 - 100_000 - 50)


def test_equity_requires_prices() -> None:
    p = Portfolio(100_000)
    p.buy(M, 10.0, time=T0, quantity=1000)
    with pytest.raises(PortfolioError, match="평가 가격"):
        p.equity({})
    assert p.equity({M: 12.0}) == pytest.approx(p.cash + 12_000)


def test_invalid_portfolio_args() -> None:
    with pytest.raises(ValueError):
        Portfolio(0)
    with pytest.raises(ValueError):
        Portfolio(1000, fee_rate=0.5)
