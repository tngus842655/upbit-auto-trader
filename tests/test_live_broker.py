"""실제 주문 브로커 테스트 — 가짜 클라이언트로 안전장치·identifier·폴링·체결 반영·복구를 검증한다. 네트워크 없음."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.exceptions import LiveTradingDisabledError, UpbitAPIError, UpbitNetworkError
from app.exchange.models import Account, OrderChance, OrderInfo
from app.trading.live_broker import LiveBroker, make_identifier, portfolio_from_accounts
from app.trading.orders import OrderRequest, OrderStatus
from app.trading.portfolio import Portfolio, Side

NOW = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
M = "KRW-BTC"


def chance(krw: str = "1000000", btc: str = "0", state: str = "active") -> OrderChance:
    return OrderChance.model_validate(
        {
            "bid_fee": "0.0005", "ask_fee": "0.0005", "maker_bid_fee": "0.0005", "maker_ask_fee": "0.0005",
            "market": {
                "id": M, "name": "BTC/KRW", "order_types": ["limit"], "order_sides": ["ask", "bid"],
                "bid_types": ["limit", "price"], "ask_types": ["limit", "market"],
                "bid": {"currency": "KRW", "price_unit": None, "min_total": "5000"},
                "ask": {"currency": "BTC", "price_unit": None, "min_total": "5000"},
                "max_total": "1000000000", "state": state,
            },
            "bid_account": {"currency": "KRW", "balance": krw, "locked": "0", "avg_buy_price": "0",
                            "avg_buy_price_modified": False, "unit_currency": "KRW"},
            "ask_account": {"currency": "BTC", "balance": btc, "locked": "0", "avg_buy_price": "100000000",
                            "avg_buy_price_modified": False, "unit_currency": "KRW"},
        }
    )


def order_info(uuid: str, *, side: str, state: str, executed: str = "0", fee: str = "0", trades=None, identifier=None,
               price=None) -> OrderInfo:
    return OrderInfo.model_validate(
        {
            "market": M, "uuid": uuid, "side": side, "ord_type": "price" if side == "bid" else "market",
            "state": state, "created_at": "2026-05-01T12:00:00+09:00", "executed_volume": executed, "paid_fee": fee,
            "reserved_fee": "0", "remaining_fee": "0", "locked": "0", "trades_count": len(trades or []),
            "identifier": identifier, "price": price, "trades": trades or [],
        }
    )


def trade(price: str, volume: str, side: str = "bid") -> dict:
    funds = str(Decimal(price) * Decimal(volume))
    return {"market": M, "uuid": "t-1", "price": price, "volume": volume, "funds": funds, "side": side,
            "created_at": "2026-05-01T12:00:01+09:00", "trend": "up"}


class FakeClient:
    """create → (폴링) get_order → 체결 시나리오를 스크립트로 재현한다."""

    def __init__(self, *, chance_obj: OrderChance | None = None, create_results=None, order_states=None,
                 allow: bool = True) -> None:
        self.chance_obj = chance_obj or chance()
        self.create_results = list(create_results or [])  # OrderInfo 또는 Exception
        self.order_states = list(order_states or [])  # get_order 응답 순서
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.identifier_lookups: list[str] = []
        self.allow = allow

    @property
    def orders_allowed(self) -> bool:
        return self.allow

    market_buy_params = staticmethod(lambda market, amount, identifier=None: {
        "market": market, "side": "bid", "ord_type": "price", "price": f"{amount:.0f}"})
    market_sell_params = staticmethod(lambda market, volume, identifier=None: {
        "market": market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"})

    async def get_order_chance(self, market):
        return self.chance_obj

    async def create_order(self, params):
        self.created.append(dict(params))
        result = self.create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def get_order(self, *, uuid=None, identifier=None):
        if identifier is not None:
            self.identifier_lookups.append(identifier)
            found = [o for o in self.order_states if o.identifier == identifier]
            if not found:
                raise UpbitAPIError(404, "order_not_found", "not found")
            return found[0]
        return self.order_states.pop(0) if len(self.order_states) > 1 else self.order_states[0]

    async def cancel_order(self, *, uuid=None, identifier=None):
        self.cancelled.append(uuid)
        return order_info(uuid, side="bid", state="cancel")

    async def get_accounts(self):
        return [
            Account.model_validate({"currency": "KRW", "balance": "500000", "locked": "0", "avg_buy_price": "0",
                                    "avg_buy_price_modified": False, "unit_currency": "KRW"}),
            Account.model_validate({
                "currency": "BTC", "balance": "0.01", "locked": "0", "avg_buy_price": "100000000",
                "avg_buy_price_modified": False, "unit_currency": "KRW",
            }),
        ]


async def no_sleep(_: float) -> None:
    return None


def armed_settings(make_settings):
    return make_settings(
        trading_mode="LIVE", live_trading_enabled=True, upbit_access_key="a" * 20, upbit_secret_key="b" * 40
    )


def test_identifier_length_and_attempts() -> None:
    short = "live:KRW-BTC:BUY:2026-05-01T03:00:00+00:00"
    assert make_identifier(short) == short
    assert make_identifier(short, 2) == short + "-r2"
    long_id = "x" * 100
    ident = make_identifier(long_id, 3)
    assert len(ident) <= 64 and ident.endswith("-r3")
    assert make_identifier(long_id, 3) == ident  # 결정적


def test_portfolio_from_accounts() -> None:
    accounts = [
        Account.model_validate({"currency": "KRW", "balance": "123456.78", "locked": "0", "avg_buy_price": "0",
                                "avg_buy_price_modified": False, "unit_currency": "KRW"}),
        Account.model_validate({"currency": "BTC", "balance": "0.005", "locked": "0", "avg_buy_price": "90000000",
                                "avg_buy_price_modified": False, "unit_currency": "KRW"}),
        Account.model_validate({"currency": "XRP", "balance": "10", "locked": "0", "avg_buy_price": "500",
                                "avg_buy_price_modified": False, "unit_currency": "KRW"}),
    ]
    p = portfolio_from_accounts(accounts, [M, "KRW-ETH"], fee_rate=0.0005)
    assert p.cash == pytest.approx(123456.78)
    assert p.position(M).quantity == 0.005 and p.position(M).avg_price == 90_000_000
    assert "KRW-XRP" not in p.positions  # 거래 대상이 아닌 코인은 무시


class TestGuards:
    async def test_settings_guard_blocks_even_with_fake_client(self, make_settings) -> None:
        broker = LiveBroker(FakeClient(), Portfolio(1_000_000), make_settings(trading_mode="LIVE"), sleep=no_sleep)
        with pytest.raises(LiveTradingDisabledError):
            await broker.execute(OrderRequest(M, Side.BUY, amount=10_000), None, NOW)

    async def test_duplicate_client_id(self, make_settings) -> None:
        client = FakeClient(create_results=[order_info("u1", side="bid", state="done", executed="0.0001", fee="5",
                                                       trades=[trade("100000000", "0.0001")])],
                            order_states=[order_info("u1", side="bid", state="done", executed="0.0001", fee="5",
                                                     trades=[trade("100000000", "0.0001")])])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep,
                            processed_client_ids={"live:KRW-BTC:BUY:x"})
        request = OrderRequest(M, Side.BUY, amount=10_000, client_id="live:KRW-BTC:BUY:x")
        order = await broker.execute(request, None, NOW)
        assert order.status is OrderStatus.REJECTED and "중복" in order.error
        assert client.created == []


class TestBuy:
    async def test_market_buy_fills_and_updates_portfolio(self, make_settings) -> None:
        done = order_info("u1", side="bid", state="done", executed="0.0001", fee="5",
                          trades=[trade("100000000", "0.0001")], identifier="live:KRW-BTC:BUY:sig")
        client = FakeClient(create_results=[order_info("u1", side="bid", state="wait")], order_states=[done])
        portfolio = Portfolio(1_000_000)
        broker = LiveBroker(client, portfolio, armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="live:KRW-BTC:BUY:sig",
                                                  reason="골든크로스"), None, NOW)
        assert order.status is OrderStatus.FILLED
        assert client.created[0] == {"market": M, "side": "bid", "ord_type": "price", "price": "10000",
                                     "identifier": "live:KRW-BTC:BUY:sig"}
        assert order.exchange_order_id == "u1"
        assert order.filled_quantity == pytest.approx(0.0001) and order.fill_price == pytest.approx(100_000_000)
        assert order.fee == 5.0
        assert portfolio.position(M).quantity == pytest.approx(0.0001)
        assert portfolio.cash == pytest.approx(1_000_000 - 10_000 - 5)
        assert portfolio.fees_paid == 5.0
        assert "upbit u1" in order.reason and "골든크로스" in order.reason
        assert "live:KRW-BTC:BUY:sig" in broker.processed

    async def test_budget_capped_by_exchange_balance_and_min_total(self, make_settings) -> None:
        client = FakeClient(chance_obj=chance(krw="8000"), create_results=[order_info("u1", side="bid", state="done",
                            executed="0.00008", fee="4", trades=[trade("100000000", "0.00008")])],
                            order_states=[order_info("u1", side="bid", state="done", executed="0.00008", fee="4",
                                                     trades=[trade("100000000", "0.00008")])])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=50_000, client_id="c1"), None, NOW)
        assert order.status is OrderStatus.FILLED and client.created[0]["price"] == "8000"

        tiny = FakeClient(chance_obj=chance(krw="3000"))
        broker2 = LiveBroker(tiny, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep)
        rejected = await broker2.execute(OrderRequest(M, Side.BUY, amount=50_000, client_id="c2"), None, NOW)
        assert rejected.status is OrderStatus.REJECTED and "최소 주문 금액" in rejected.error
        assert tiny.created == []

    async def test_inactive_market_rejected_before_order(self, make_settings) -> None:
        client = FakeClient(chance_obj=chance(state="delisted"))
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c3"), None, NOW)
        assert order.status is OrderStatus.REJECTED and "거래 불가" in order.error and client.created == []

    async def test_exchange_error_is_not_retried(self, make_settings) -> None:
        client = FakeClient(create_results=[UpbitAPIError(400, "insufficient_funds_bid", "잔고 부족")])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c4"), None, NOW)
        assert order.status is OrderStatus.REJECTED and "insufficient_funds_bid" in order.error
        assert len(client.created) == 1 and "c4" not in broker.processed

    async def test_duplicated_identifier_retries_with_new_suffix(self, make_settings) -> None:
        done = order_info(
            "u2", side="bid", state="done", executed="0.0001", fee="5", trades=[trade("100000000", "0.0001")]
        )
        dup = UpbitAPIError(400, "duplicated_identifier", "dup")
        client = FakeClient(create_results=[dup, done], order_states=[done])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c5"), None, NOW)
        assert order.status is OrderStatus.FILLED
        assert [c["identifier"] for c in client.created] == ["c5", "c5-r2"]

    async def test_network_error_looks_up_identifier_to_avoid_double_order(self, make_settings) -> None:
        done = order_info("u3", side="bid", state="done", executed="0.0001", fee="5",
                          trades=[trade("100000000", "0.0001")], identifier="c6")
        client = FakeClient(create_results=[UpbitNetworkError("timeout")], order_states=[done])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c6"), None, NOW)
        assert order.status is OrderStatus.FILLED and order.exchange_order_id == "u3"
        assert client.identifier_lookups == ["c6"] and len(client.created) == 1

    async def test_network_error_without_order_is_rejected(self, make_settings) -> None:
        errors = [UpbitNetworkError("timeout"), UpbitNetworkError("timeout")]
        client = FakeClient(create_results=errors, order_states=[])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep, max_attempts=2)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c7"), None, NOW)
        assert order.status is OrderStatus.REJECTED and "네트워크" in order.error
        assert client.identifier_lookups == ["c7", "c7-r2"]

    async def test_unfilled_order_is_cancelled_after_timeout(self, make_settings) -> None:
        waiting = order_info("u4", side="bid", state="wait")
        cancelled = order_info("u4", side="bid", state="cancel", executed="0")
        client = FakeClient(create_results=[waiting], order_states=[waiting, waiting, cancelled])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep,
                            poll_interval=1.0, fill_timeout=2.0)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c8"), None, NOW)
        assert client.cancelled == ["u4"]
        assert order.status is OrderStatus.REJECTED and "체결 없음" in order.error


class TestSell:
    async def test_market_sell_uses_position_and_exchange_balance(self, make_settings) -> None:
        portfolio = Portfolio(1_000_000)
        portfolio.buy(M, 100_000_000, time=NOW, quantity=0.0002, enforce_limits=False)
        done = order_info("u5", side="ask", state="done", executed="0.0002", fee="11",
                          trades=[trade("110000000", "0.0002", side="ask")])
        client = FakeClient(chance_obj=chance(btc="0.0002"), create_results=[done], order_states=[done])
        broker = LiveBroker(client, portfolio, armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.SELL, client_id="s1", reason="stop_loss"), None, NOW)
        assert order.status is OrderStatus.FILLED
        assert client.created[0] == {"market": M, "side": "ask", "ord_type": "market", "volume": "0.00020000",
                                     "identifier": "s1"}
        assert portfolio.position(M) is None
        assert portfolio.trades[0].exit_fee == 11.0 and portfolio.trades[0].pnl == pytest.approx(
            110_000_000 * 0.0002 - 11 - 100_000_000 * 0.0002 - 100_000_000 * 0.0002 * 0.0005
        )

    async def test_sell_without_exchange_balance_rejected(self, make_settings) -> None:
        portfolio = Portfolio(1_000_000)
        portfolio.buy(M, 100_000_000, time=NOW, quantity=0.0002, enforce_limits=False)
        client = FakeClient(chance_obj=chance(btc="0"))
        broker = LiveBroker(client, portfolio, armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.SELL, client_id="s2"), None, NOW)
        assert order.status is OrderStatus.REJECTED and "수량이 없습니다" in order.error


async def test_reconcile_aligns_internal_portfolio_to_exchange(make_settings) -> None:
    portfolio = Portfolio(1_000_000)
    portfolio.buy(M, 100_000_000, time=NOW, quantity=0.02, enforce_limits=False)
    broker = LiveBroker(FakeClient(), portfolio, armed_settings(make_settings), sleep=no_sleep)
    diff = await broker.reconcile([M, "KRW-ETH"])
    assert diff["cash"]["exchange"] == 500_000 and diff[M]["exchange_qty"] == 0.01
    assert portfolio.cash == 500_000 and portfolio.position(M).quantity == 0.01
