"""실제 주문 브로커 테스트 — 가짜 클라이언트로 안전장치·identifier·폴링·체결 반영·복구를 검증한다. 네트워크 없음."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import LiveTradingDisabledError, UpbitAPIError, UpbitNetworkError
from app.exchange.models import Account, OrderChance, OrderInfo
from app.trading.live_broker import LiveBroker, make_identifier, portfolio_from_accounts
from app.trading.orders import Order, OrderRequest, OrderStatus, OrderType
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
                 allow: bool = True, lookup_results=None) -> None:
        self.chance_obj = chance_obj or chance()
        self.create_results = list(create_results or [])  # OrderInfo 또는 Exception
        self.order_states = list(order_states or [])  # get_order 응답 순서
        self.lookup_results = list(lookup_results or [])  # identifier 조회 응답 순서 (OrderInfo 또는 Exception)
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.identifier_lookups: list[str] = []
        self.uuid_lookups = 0
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
            if self.lookup_results:
                result = self.lookup_results.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            found = [o for o in self.order_states if o.identifier == identifier]
            if not found:
                raise UpbitAPIError(404, "order_not_found", "not found")
            return found[0]
        self.uuid_lookups += 1
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
        """응답 유실 뒤 같은 identifier 재조회가 전부 404 → 미생성 확정 → 재주문 없이 거부."""
        client = FakeClient(create_results=[UpbitNetworkError("timeout")], order_states=[])
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=record_sleep,
                            lookup_attempts=3, lookup_backoff=1.0)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c7"), None, NOW)
        assert order.status is OrderStatus.REJECTED and "미생성" in order.error
        assert len(client.created) == 1 and client.identifier_lookups == ["c7", "c7", "c7"]
        assert sleeps == [1.0, 2.0]  # 수 초 간격을 두고 재조회한 뒤에만 미생성으로 판정
        assert "c7" not in broker.processed

    async def test_network_error_then_lookup_failure_marks_unknown_without_reorder(self, make_settings) -> None:
        """감사 CRITICAL-1: 서버 생성 + 응답 유실 + 조회 실패 → 새 identifier 로 재주문하지 않고 UNKNOWN."""
        lookups = [UpbitNetworkError("GET timeout")] * 3
        client = FakeClient(create_results=[UpbitNetworkError("POST timeout")], lookup_results=lookups)
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep,
                            lookup_attempts=3)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c8"), None, NOW)
        assert order.status is OrderStatus.UNKNOWN and order.exchange_identifier == "c8"
        assert "운영자 확인" in order.error
        assert len(client.created) == 1 and client.identifier_lookups == ["c8", "c8", "c8"]
        assert "c8" in broker.processed  # 확인 전까지 같은 신호 재주문 금지
        assert broker.portfolio.cash == 1_000_000  # 계좌는 건드리지 않는다

    async def test_network_error_then_mixed_404_and_timeout_stays_unknown(self, make_settings) -> None:
        """404 와 조회 실패가 섞이면 미생성으로 확정하지 않는다 (보수적으로 UNKNOWN)."""
        lookups = [UpbitAPIError(404, "order_not_found", "x"), UpbitNetworkError("t"),
                   UpbitAPIError(404, "order_not_found", "x")]
        client = FakeClient(create_results=[UpbitNetworkError("POST timeout")], lookup_results=lookups)
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep,
                            lookup_attempts=3)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c8b"), None, NOW)
        assert order.status is OrderStatus.UNKNOWN and len(client.created) == 1

    async def test_network_error_then_delayed_visibility_adopts_existing_order(self, make_settings) -> None:
        """잠깐 404 였다가 보이면 그 주문을 그대로 이어서 처리한다 (재주문 없음)."""
        done = order_info("u9", side="bid", state="done", executed="0.0001", fee="5",
                          trades=[trade("100000000", "0.0001")], identifier="c9")
        lookups = [UpbitAPIError(404, "order_not_found", "x"), UpbitAPIError(404, "order_not_found", "x"), done]
        client = FakeClient(create_results=[UpbitNetworkError("POST timeout")], lookup_results=lookups,
                            order_states=[done])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep,
                            lookup_attempts=5)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c9"), None, NOW)
        assert order.status is OrderStatus.FILLED and order.exchange_order_id == "u9"
        assert len(client.created) == 1 and client.identifier_lookups == ["c9", "c9", "c9"]
        assert broker.portfolio.cash == pytest.approx(1_000_000 - 10_000 - 5)

    async def test_unfilled_order_is_cancelled_after_timeout(self, make_settings) -> None:
        waiting = order_info("u4", side="bid", state="wait")
        cancelled = order_info("u4", side="bid", state="cancel", executed="0")
        client = FakeClient(create_results=[waiting], order_states=[waiting, waiting, cancelled])
        broker = LiveBroker(client, Portfolio(1_000_000), armed_settings(make_settings), sleep=no_sleep,
                            poll_interval=1.0, fill_timeout=2.0)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=10_000, client_id="c8"), None, NOW)
        assert client.cancelled == ["u4"]
        # 체결 없이 취소 → CANCELLED (HIGH-1)
        assert order.status is OrderStatus.CANCELLED and "체결 없음" in order.error


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


def unknown_order(uuid: str | None, identifier: str | None = None, *, created_at: datetime = NOW) -> Order:
    return Order(
        id=f"o-{uuid or identifier}", client_id=f"c-{uuid or identifier}", mode="live", market=M, side=Side.BUY,
        order_type=OrderType.MARKET, amount=100_000, quantity=None, status=OrderStatus.UNKNOWN, created_at=created_at,
        exchange_order_id=uuid, exchange_identifier=identifier,
    )


class TestUnknownAndResolve:
    """감사 HIGH-1: 체결 확인 실패는 UNKNOWN 으로 남기고 후속 조회로 확정한다."""

    async def test_poll_failure_marks_unknown_with_uuid(self, make_settings) -> None:
        class PollFails(FakeClient):
            async def get_order(self, *, uuid=None, identifier=None):
                raise UpbitNetworkError("GET /v1/order 타임아웃")

        client = PollFails(create_results=[order_info("u1", side="bid", state="wait")])
        portfolio = Portfolio(1_000_000)
        broker = LiveBroker(client, portfolio, armed_settings(make_settings), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=100_000, client_id="c1"), None, NOW)
        assert order.status is OrderStatus.UNKNOWN and order.exchange_order_id == "u1"
        assert len(client.created) == 1 and portfolio.cash == 1_000_000 and "c1" in broker.processed

    async def test_resolve_applies_fill_when_done(self, make_settings) -> None:
        done = order_info("u1", side="bid", state="done", executed="0.001", fee="50",
                          trades=[trade("100000000", "0.001")])
        portfolio = Portfolio(1_000_000)
        broker = LiveBroker(FakeClient(order_states=[done]), portfolio, armed_settings(make_settings), sleep=no_sleep)
        order = unknown_order("u1")
        final = await broker.resolve_order(order, NOW)
        assert final is order and final.status is OrderStatus.FILLED and final.is_filled
        assert portfolio.position(M).quantity == pytest.approx(0.001)
        assert portfolio.cash == pytest.approx(1_000_000 - 100_000 - 50)

    async def test_resolve_partial_cancelled_pending_and_failures(self, make_settings) -> None:
        settings = armed_settings(make_settings)
        # 부분 체결 후 취소 → PARTIAL (체결분 반영, is_filled)
        partial = order_info("u2", side="bid", state="cancel", executed="0.0005", fee="25",
                             trades=[trade("100000000", "0.0005")])
        portfolio = Portfolio(1_000_000)
        broker = LiveBroker(FakeClient(order_states=[partial]), portfolio, settings, sleep=no_sleep)
        final = await broker.resolve_order(unknown_order("u2"), NOW)
        assert final.status is OrderStatus.PARTIAL and final.is_filled and "부분 체결" in final.reason
        assert portfolio.position(M).quantity == pytest.approx(0.0005)
        # 체결 없이 취소 → CANCELLED
        cancelled = order_info("u3", side="bid", state="cancel")
        broker3 = LiveBroker(FakeClient(order_states=[cancelled]), Portfolio(1_000_000), settings, sleep=no_sleep)
        assert (await broker3.resolve_order(unknown_order("u3"), NOW)).status is OrderStatus.CANCELLED
        # 아직 대기 + fill_timeout 경과 → 취소 접수하고 None (다음 호출에서 확정)
        waiting = order_info("u4", side="bid", state="wait")
        client4 = FakeClient(order_states=[waiting])
        broker4 = LiveBroker(client4, Portfolio(1_000_000), settings, sleep=no_sleep, fill_timeout=30)
        stale = unknown_order("u4", created_at=NOW - timedelta(seconds=60))
        assert await broker4.resolve_order(stale, NOW) is None and client4.cancelled == ["u4"]
        # 조회 자체가 실패 → None (UNKNOWN 유지)

        class Down(FakeClient):
            async def get_order(self, *, uuid=None, identifier=None):
                raise UpbitNetworkError("down")

        broker5 = LiveBroker(Down(), Portfolio(1_000_000), settings, sleep=no_sleep)
        assert await broker5.resolve_order(unknown_order("u5"), NOW) is None
        # uuid 없이 identifier 만 있고 404 → 미생성 확정 REJECTED
        broker6 = LiveBroker(FakeClient(order_states=[]), Portfolio(1_000_000), settings, sleep=no_sleep)
        final6 = await broker6.resolve_order(unknown_order(None, "c6"), NOW)
        assert final6.status is OrderStatus.REJECTED and "미생성" in final6.error


class TestFillWithoutTrades:
    """감사 MEDIUM-1 — 체결 목록이 비어 있을 때 시장가 매수의 price(총액)를 단가로 쓰지 않는다."""

    def test_fill_funds_by_order_type(self) -> None:
        buy_done = order_info("u1", side="bid", state="done", executed="0.001", fee="50", price="100000")
        assert buy_done.fill_funds() == Decimal("100000")  # 시장가 매수 done: 총액 전부 사용
        buy_partial = order_info("u2", side="bid", state="cancel", executed="0.0004", fee="20", price="100000")
        assert buy_partial.fill_funds() is None  # 부분 체결 뒤 취소: 쓴 금액을 알 수 없다
        sell_done = order_info("u3", side="ask", state="done", executed="0.001", fee="50")
        assert sell_done.fill_funds() is None  # 시장가 매도: 단가 정보 없음
        limit = OrderInfo.model_validate({
            "market": M, "uuid": "u4", "side": "bid", "ord_type": "limit", "state": "done", "created_at": "x",
            "executed_volume": "0.5", "price": "200", "trades": [],
        })
        assert limit.fill_funds() == Decimal("100")  # 지정가: 단가 × 체결 수량
        with_trades = order_info("u5", side="ask", state="done", executed="0.001",
                                 trades=[trade("100000000", "0.001", "ask")])
        assert with_trades.fill_funds() == Decimal("100000")
        assert order_info("u6", side="bid", state="cancel").fill_funds() == 0

    async def test_market_buy_without_trades_uses_order_total(self, make_settings) -> None:
        """총액 100,000원·체결 0.001 BTC 응답에 trades 가 없어도 단가 1억·현금 차감 100,050원으로 반영한다."""
        done = order_info("u1", side="bid", state="done", executed="0.001", fee="50", price="100000")
        client = FakeClient(create_results=[order_info("u1", side="bid", state="wait", price="100000")],
                            order_states=[done])
        portfolio = Portfolio(1_000_000)
        broker = LiveBroker(client, portfolio, armed_settings(make_settings), sleep=no_sleep,
                            trades_lookup_attempts=2)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=100_000, client_id="c1"), None, NOW)
        assert order.status is OrderStatus.FILLED
        assert order.fill_price == pytest.approx(100_000_000)
        assert portfolio.position(M).avg_price == pytest.approx(100_000_000)
        assert portfolio.cash == pytest.approx(1_000_000 - 100_000 - 50)
        assert client.uuid_lookups == 3  # 폴링 1회 + 체결 목록 재조회 2회

    async def test_market_sell_without_trades_is_unknown_until_trades_arrive(self, make_settings) -> None:
        """시장가 매도는 price 가 없어 체결 목록 없이는 금액을 모른다 → UNKNOWN, 목록이 오면 확정."""
        portfolio = Portfolio(1_000_000)
        portfolio.buy(M, 100_000_000, time=NOW, quantity=0.005, enforce_limits=False)
        no_trades = order_info("u1", side="ask", state="done", executed="0.005", fee="250")
        client = FakeClient(chance_obj=chance(btc="0.005"), create_results=[no_trades], order_states=[no_trades])
        broker = LiveBroker(client, portfolio, armed_settings(make_settings), sleep=no_sleep, trades_lookup_attempts=1)
        order = await broker.execute(OrderRequest(M, Side.SELL, quantity=0.005, client_id="s1"), None, NOW)
        assert order.status is OrderStatus.UNKNOWN and "체결 금액 미확인" in order.error
        assert order.exchange_order_id == "u1" and "s1" in broker.processed
        assert portfolio.position(M).quantity == 0.005 and portfolio.cash == pytest.approx(499_750)  # 반영 안 됨
        # 아직도 목록이 없으면 None (UNKNOWN 유지)
        assert await broker.resolve_order(order, NOW) is None and order.status is OrderStatus.UNKNOWN
        # 체결 목록이 채워지면 확정
        client.order_states = [order_info("u1", side="ask", state="done", executed="0.005", fee="250",
                                          trades=[trade("100000000", "0.005", "ask")])]
        final = await broker.resolve_order(order, NOW)
        assert final is order and final.status is OrderStatus.FILLED and final.fill_price == pytest.approx(100_000_000)
        assert not portfolio.has_position(M) and portfolio.cash == pytest.approx(499_750 + 500_000 - 250)

    async def test_partial_cancelled_buy_without_trades_stays_unknown(self, make_settings) -> None:
        partial = order_info("u2", side="bid", state="cancel", executed="0.0004", fee="20", price="100000")
        broker = LiveBroker(FakeClient(order_states=[partial]), Portfolio(1_000_000), armed_settings(make_settings),
                            sleep=no_sleep, trades_lookup_attempts=1)
        order = unknown_order("u2")
        assert await broker.resolve_order(order, NOW) is None and order.status is OrderStatus.UNKNOWN


async def test_reconcile_aligns_internal_portfolio_to_exchange(make_settings) -> None:
    portfolio = Portfolio(1_000_000)
    portfolio.buy(M, 100_000_000, time=NOW, quantity=0.02, enforce_limits=False)
    broker = LiveBroker(FakeClient(), portfolio, armed_settings(make_settings), sleep=no_sleep)
    diff = await broker.reconcile([M, "KRW-ETH"])
    assert diff["cash"]["exchange"] == 500_000 and diff[M]["exchange_qty"] == 0.01
    assert portfolio.cash == 500_000 and portfolio.position(M).quantity == 0.01
