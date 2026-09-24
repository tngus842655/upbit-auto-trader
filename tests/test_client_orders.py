"""UpbitClient 주문 API 테스트 — MockTransport 로 요청 형식(본문 해시·쿼리)과 응답 파싱을 검증한다."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.core.exceptions import LiveTradingDisabledError, UpbitBadRequestError
from app.exchange.upbit_client import UpbitClient
from tests.conftest import json_response
from tests.test_live_broker import chance as make_chance
from tests.test_upbit_client import decode_bearer

ORDER_JSON = {
    "market": "KRW-BTC", "uuid": "9ca023a5-851b-4fec-9f0a-48cd83c2eaae", "side": "bid", "ord_type": "price",
    "price": "10000", "state": "wait", "created_at": "2026-05-01T12:00:00+09:00", "volume": None,
    "remaining_volume": None, "reserved_fee": "5", "remaining_fee": "5", "paid_fee": "0", "locked": "10005",
    "executed_volume": "0", "trades_count": 0, "identifier": "live:KRW-BTC:BUY:x", "prevented_volume": "0",
    "prevented_locked": "0",
}
DONE_JSON = {
    **ORDER_JSON, "state": "done", "executed_volume": "0.0001", "paid_fee": "5", "trades_count": 1,
    "trades": [{"market": "KRW-BTC", "uuid": "t1", "price": "100000000", "volume": "0.0001", "funds": "10000",
                "side": "bid", "created_at": "2026-05-01T12:00:01+09:00", "trend": "up"}],
}


def armed_client(handler, client_factory):
    h = client_factory(handler, with_auth=True)
    h.client._allow_orders = True  # 테스트용: LIVE 이중 플래그를 통과한 클라이언트와 동일 상태
    return h


async def test_create_order_posts_json_and_hashes_body(client_factory) -> None:
    h = armed_client(lambda req: json_response(ORDER_JSON, 201), client_factory)
    params = UpbitClient.market_buy_params("KRW-BTC", 10_000, identifier="live:KRW-BTC:BUY:x")
    async with h.client as client:
        info = await client.create_order(params)
    req = h.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/orders"
    body = json.loads(req.content)
    assert body == {"market": "KRW-BTC", "side": "bid", "ord_type": "price", "price": "10000",
                    "identifier": "live:KRW-BTC:BUY:x"}
    payload = decode_bearer(req)
    expected = hashlib.sha512(
        b"market=KRW-BTC&side=bid&ord_type=price&price=10000&identifier=live:KRW-BTC:BUY:x"
    ).hexdigest()
    assert payload["query_hash"] == expected
    assert info.uuid == ORDER_JSON["uuid"] and info.is_open and info.identifier == "live:KRW-BTC:BUY:x"
    assert info.average_price is None


async def test_create_order_blocked_without_allow_flag(client_factory) -> None:
    h = client_factory(lambda req: json_response(ORDER_JSON, 201), with_auth=True)
    async with h.client as client:
        with pytest.raises(LiveTradingDisabledError):
            await client.create_order(UpbitClient.market_buy_params("KRW-BTC", 10_000))
    assert h.requests == []  # 네트워크 요청 자체가 없다


async def test_test_order_allowed_without_flag(client_factory) -> None:
    h = client_factory(lambda req: json_response(ORDER_JSON, 201), with_auth=True)
    async with h.client as client:
        info = await client.test_order(UpbitClient.market_sell_params("KRW-BTC", 0.0001))
    assert h.requests[0].url.path == "/v1/orders/test"
    assert json.loads(h.requests[0].content) == {"market": "KRW-BTC", "side": "ask", "ord_type": "market",
                                                 "volume": "0.00010000"}
    assert info.uuid


async def test_get_order_and_cancel_use_query_hash(client_factory) -> None:
    h = armed_client(lambda req: json_response(DONE_JSON), client_factory)
    async with h.client as client:
        info = await client.get_order(uuid=ORDER_JSON["uuid"])
        cancelled = await client.cancel_order(identifier="live:KRW-BTC:BUY:x")
    get_req, cancel_req = h.requests
    assert get_req.method == "GET" and get_req.url.path == "/v1/order"
    assert get_req.url.query == f"uuid={ORDER_JSON['uuid']}".encode()
    assert decode_bearer(get_req)["query_hash"] == hashlib.sha512(f"uuid={ORDER_JSON['uuid']}".encode()).hexdigest()
    assert cancel_req.method == "DELETE" and cancel_req.url.query == b"identifier=live%3AKRW-BTC%3ABUY%3Ax"
    assert decode_bearer(cancel_req)["query_hash"] == hashlib.sha512(b"identifier=live:KRW-BTC:BUY:x").hexdigest()
    assert info.is_final and info.executed_funds == 10000 and info.average_price == 100_000_000
    assert cancelled.uuid == ORDER_JSON["uuid"]


async def test_open_orders_array_param(client_factory) -> None:
    h = client_factory(lambda req: json_response([ORDER_JSON]), with_auth=True)
    async with h.client as client:
        orders = await client.get_open_orders("KRW-BTC")
    req = h.requests[0]
    assert str(req.url).endswith("/v1/orders/open?market=KRW-BTC&states[]=wait&states[]=watch&limit=100")
    assert decode_bearer(req)["query_hash"] == hashlib.sha512(
        b"market=KRW-BTC&states[]=wait&states[]=watch&limit=100"
    ).hexdigest()
    assert len(orders) == 1 and orders[0].trades == []


async def test_order_chance_parsing(client_factory) -> None:
    payload = json.loads(make_chance(krw="250000").model_dump_json())
    h = client_factory(lambda req: json_response(payload), with_auth=True)
    async with h.client as client:
        chance = await client.get_order_chance("KRW-BTC")
    assert h.requests[0].url.query == b"market=KRW-BTC"
    assert chance.min_total_bid == 5000 and chance.market_state == "active"
    assert "price" in chance.bid_types and "market" in chance.ask_types
    assert float(chance.bid_account.balance) == 250_000


async def test_order_error_maps_to_bad_request(client_factory) -> None:
    h = armed_client(lambda req: httpx.Response(400, json={"error": {"name": "under_min_total_bid", "message": "x"}}),
                     client_factory)
    async with h.client as client:
        with pytest.raises(UpbitBadRequestError) as info:
            await client.create_order(UpbitClient.market_buy_params("KRW-BTC", 100))
    assert info.value.name == "under_min_total_bid"
    assert len(h.requests) == 1  # POST 는 재시도하지 않는다


def test_param_builders_validate() -> None:
    with pytest.raises(ValueError):
        UpbitClient.market_buy_params("KRW-BTC", 0)
    with pytest.raises(ValueError):
        UpbitClient.market_sell_params("KRW-BTC", -1)
    assert UpbitClient.market_buy_params("KRW-BTC", 12345.678) == {"market": "KRW-BTC", "side": "bid",
                                                                  "ord_type": "price", "price": "12346"}
