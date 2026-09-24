"""WebSocket 클라이언트 테스트 — 가짜 연결로 요청 형식·파싱·재연결·종료를 검증한다."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from app.core.exceptions import UpbitResponseError
from app.exchange.auth import UpbitAuth
from app.exchange.models import KST, CandleInterval
from app.exchange.websocket import (
    PRIVATE_WS_URL,
    PUBLIC_WS_URL,
    ConnectionState,
    Subscription,
    UpbitWebSocket,
    UpbitWebSocketError,
    build_request,
)
from app.exchange.ws_models import (
    WsCandle,
    WsErrorMessage,
    WsOrderbook,
    WsStatus,
    WsTicker,
    WsTrade,
    parse_ws_message,
)
from tests.conftest import FakeClock

TRADE_JSON = {
    "type": "trade", "code": "KRW-BTC", "timestamp": 1787728554853, "trade_date": "2026-08-26",
    "trade_time": "07:15:54", "trade_timestamp": 1787728554797, "trade_price": 109847000.0,
    "trade_volume": 0.00509799, "ask_bid": "ASK", "prev_closing_price": 109165000.0, "change": "RISE",
    "change_price": 682000.0, "sequential_id": 17877285547970000, "best_ask_price": 109867000,
    "best_ask_size": 0.0099545, "best_bid_price": 109847000, "best_bid_size": 0.1583673, "stream_type": "SNAPSHOT",
}
TICKER_JSON = {
    "type": "ticker", "code": "KRW-BTC", "opening_price": 109165000.0, "high_price": 110117000.0,
    "low_price": 108958000.0, "trade_price": 109868000.0, "prev_closing_price": 109165000.0,
    "acc_trade_price": 59726546304.62442, "change": "RISE", "change_price": 703000.0,
    "signed_change_price": 703000.0, "change_rate": 0.006439793, "signed_change_rate": 0.006439793,
    "ask_bid": "BID", "trade_volume": 9.101e-05, "acc_trade_volume": 544.24861805, "trade_date": "20260826",
    "trade_time": "072400", "trade_timestamp": 1787729040682, "acc_ask_volume": 271.9810004,
    "acc_bid_volume": 272.26761765, "highest_52_week_price": 179869000.0, "highest_52_week_date": "2025-10-09",
    "lowest_52_week_price": 88342000.0, "lowest_52_week_date": "2026-08-14", "market_state": "ACTIVE",
    "is_trading_suspended": False, "delisting_date": None, "market_warning": "NONE", "timestamp": 1787729042606,
    "acc_trade_price_24h": 198189163442.74924, "acc_trade_volume_24h": 1807.38027625, "stream_type": "REALTIME",
}
ORDERBOOK_JSON = {
    "type": "orderbook", "code": "KRW-BTC", "timestamp": 1787727947526, "total_ask_size": 18.93431797,
    "total_bid_size": 11.48721726,
    "orderbook_units": [
        {"ask_price": 109950000.0, "bid_price": 109880000.0, "ask_size": 0.00342134, "bid_size": 0.18819864},
        {"ask_price": 109960000.0, "bid_price": 109840000.0, "ask_size": 0.00077053, "bid_size": 0.08051758},
    ],
    "stream_type": "SNAPSHOT", "level": 0,
}
CANDLE_JSON = {
    "type": "candle.1m", "code": "KRW-BTC", "candle_date_time_utc": "2026-08-26T07:24:00",
    "candle_date_time_kst": "2026-08-26T16:24:00", "opening_price": 109860000.0, "high_price": 109870000.0,
    "low_price": 109850000.0, "trade_price": 109868000.0, "candle_acc_trade_volume": 1.234,
    "candle_acc_trade_price": 135000000.0, "timestamp": 1787729042606, "stream_type": "REALTIME",
}


# ----------------------------------------------------------------------
# 가짜 연결
# ----------------------------------------------------------------------
class FakeConnection:
    """``incoming`` 항목을 순서대로 돌려준다. 예외 인스턴스는 그 시점에 raise 되고, 다 떨어지면 정상 종료로 본다."""

    def __init__(self, incoming: list[object]) -> None:
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self.closed:
            raise ConnectionClosedOK(None, None)
        if not self.incoming:
            raise ConnectionClosedOK(None, None)
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item) if not isinstance(item, str) else item

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, connections: list[FakeConnection | Exception]) -> None:
        self.connections = list(connections)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]):
        self.calls.append((url, headers))
        if not self.connections:
            raise AssertionError("준비된 가짜 연결이 더 없습니다")
        item = self.connections.pop(0)

        @asynccontextmanager
        async def _ctx():
            if isinstance(item, Exception):
                raise item
            yield item

        return _ctx()


def make_ws(connector: FakeConnector, subscriptions=None, **kwargs) -> tuple[UpbitWebSocket, list[float], FakeClock]:
    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    ws = UpbitWebSocket(
        subscriptions or [Subscription.trade(["KRW-BTC"])],
        connector=connector, sleep=fake_sleep, clock=clock, **kwargs,
    )
    return ws, sleeps, clock


async def collect(ws: UpbitWebSocket, limit: int = 100) -> list:
    out = []
    async for msg in ws.stream():
        out.append(msg)
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------
# 구독 요청 형식
# ----------------------------------------------------------------------
class TestSubscription:
    def test_build_request_structure(self) -> None:
        subs = [Subscription.trade(["krw-btc", "KRW-ETH"]), Subscription.orderbook(["KRW-BTC"], units=5, level=10000)]
        message = json.loads(build_request(subs, ticket="t-1"))
        assert message[0] == {"ticket": "t-1"}
        assert message[1] == {"type": "trade", "codes": ["KRW-BTC", "KRW-ETH"]}
        assert message[2] == {"type": "orderbook", "codes": ["KRW-BTC.5"], "level": 10000}
        assert message[-1] == {"format": "DEFAULT"}

    def test_ticket_defaults_to_uuid_and_flags(self) -> None:
        message = json.loads(build_request([Subscription.ticker(["KRW-BTC"], is_only_realtime=True)]))
        assert len(message[0]["ticket"]) == 36
        assert message[1]["is_only_realtime"] is True
        assert "is_only_snapshot" not in message[1]

    def test_candle_subscription(self) -> None:
        assert Subscription.candle(["KRW-BTC"], "1h").type == "candle.60m"
        assert Subscription.candle(["KRW-BTC"], CandleInterval.S1).type == "candle.1s"
        with pytest.raises(ValueError):
            Subscription.candle(["KRW-BTC"], "1d")

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: Subscription("ticker", ()),
            lambda: Subscription("unknown", ("KRW-BTC",)),
            lambda: Subscription("candle.2m", ("KRW-BTC",)),
            lambda: Subscription.trade(["KRW-BTC"], is_only_snapshot=True, is_only_realtime=True),
            lambda: Subscription.trade(["KRW-BTC"], level=1000),
            lambda: Subscription.orderbook(["KRW-BTC"], units=7),
        ],
    )
    def test_invalid_subscriptions(self, factory) -> None:
        with pytest.raises(ValueError):
            factory()

    def test_build_request_validation(self) -> None:
        with pytest.raises(ValueError):
            build_request([])
        with pytest.raises(ValueError):
            build_request([Subscription.trade(["KRW-BTC"])], fmt="COMPACT")


# ----------------------------------------------------------------------
# 메시지 파싱
# ----------------------------------------------------------------------
class TestParse:
    def test_trade(self) -> None:
        msg = parse_ws_message(json.dumps(TRADE_JSON))
        assert isinstance(msg, WsTrade)
        assert msg.is_snapshot is True
        assert msg.amount == pytest.approx(109847000.0 * 0.00509799)
        assert msg.trade_datetime_utc.tzinfo is UTC
        assert msg.trade_datetime_utc.strftime("%H:%M:%S") == "07:15:54"

    def test_ticker(self) -> None:
        msg = parse_ws_message(json.dumps(TICKER_JSON).encode())
        assert isinstance(msg, WsTicker)
        assert msg.is_snapshot is False
        assert msg.is_tradable is True
        assert msg.model_extra["market_warning"] == "NONE"  # deprecated 필드는 보존만

    def test_orderbook_properties(self) -> None:
        msg = parse_ws_message(json.dumps(ORDERBOOK_JSON))
        assert isinstance(msg, WsOrderbook)
        assert msg.best_ask_price == 109950000.0
        assert msg.best_bid_price == 109880000.0
        assert msg.spread == 70000.0
        assert msg.mid_price == 109915000.0
        assert len(msg.orderbook_units) == 2

    def test_candle(self) -> None:
        msg = parse_ws_message(json.dumps(CANDLE_JSON))
        assert isinstance(msg, WsCandle)
        assert msg.interval is CandleInterval.M1
        assert msg.candle_date_time_utc.tzinfo is UTC
        assert msg.candle_date_time_kst.tzinfo is KST
        assert msg.candle_date_time_kst == msg.candle_date_time_utc

    def test_status_and_error(self) -> None:
        assert isinstance(parse_ws_message('{"status":"UP"}'), WsStatus)
        err = parse_ws_message('{"error":{"name":"WRONG_FORMAT","message":"bad"}}')
        assert isinstance(err, WsErrorMessage) and err.is_fatal
        assert parse_ws_message('{"error":{"name":"TOO_MANY_REQUEST","message":"x"}}').is_fatal is False

    @pytest.mark.parametrize("raw", ["not json", "[1,2]", '{"type":"mystery"}', '{"foo":1}', '{"type":"trade"}'])
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(UpbitResponseError):
            parse_ws_message(raw)


# ----------------------------------------------------------------------
# 스트림 동작
# ----------------------------------------------------------------------
class TestStream:
    async def test_sends_request_and_yields_messages(self) -> None:
        conn = FakeConnection([TRADE_JSON, {"status": "UP"}, ORDERBOOK_JSON])
        connector = FakeConnector([conn])
        ws, sleeps, _ = make_ws(connector, max_reconnects=0)
        messages = await collect(ws, limit=2)
        await ws.close()

        assert connector.calls[0] == (PUBLIC_WS_URL, {})
        request = json.loads(conn.sent[0])
        assert request[1] == {"type": "trade", "codes": ["KRW-BTC"]}
        assert [type(m) for m in messages] == [WsTrade, WsOrderbook]  # status 는 건너뜀
        assert ws.stats.messages == 2
        assert ws.stats.extra == {"trade": 1, "orderbook": 1}
        assert ws.state is ConnectionState.CLOSED
        assert conn.closed is True
        assert sleeps == []

    async def test_reconnects_with_backoff_and_resubscribes(self) -> None:
        first = FakeConnection([TRADE_JSON, ConnectionClosedError(None, None)])
        second = FakeConnection([TICKER_JSON])
        connector = FakeConnector([first, ConnectionRefusedError("down"), second])
        ws, sleeps, _ = make_ws(connector)
        messages = await collect(ws, limit=2)
        await ws.close()

        assert [type(m) for m in messages] == [WsTrade, WsTicker]
        assert sleeps == [1.0, 2.0]  # 끊김 후 1초, 연결 실패 후 2초
        assert len(second.sent) == 1 and json.loads(second.sent[0])[1]["type"] == "trade"
        assert ws.stats.connects == 2
        assert ws.stats.reconnects == 1

    async def test_backoff_is_capped(self) -> None:
        connector = FakeConnector([OSError("x")] * 7 + [FakeConnection([TRADE_JSON])])
        ws, sleeps, _ = make_ws(connector, reconnect_max_delay=8.0)
        await collect(ws, limit=1)
        await ws.close()
        assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]

    async def test_gives_up_after_max_reconnects(self) -> None:
        connector = FakeConnector([OSError("x"), OSError("x"), OSError("x")])
        ws, sleeps, _ = make_ws(connector, max_reconnects=2)
        with pytest.raises(UpbitWebSocketError, match="RECONNECT_EXHAUSTED"):
            await collect(ws)
        assert sleeps == [1.0, 2.0]
        assert ws.state is ConnectionState.DISCONNECTED

    async def test_fatal_server_error_stops_without_reconnect(self) -> None:
        conn = FakeConnection([{"error": {"name": "WRONG_FORMAT", "message": "bad request"}}])
        connector = FakeConnector([conn])
        ws, sleeps, _ = make_ws(connector)
        with pytest.raises(UpbitWebSocketError, match="WRONG_FORMAT"):
            await collect(ws)
        assert sleeps == []
        assert ws.stats.last_error == "WRONG_FORMAT: bad request"

    async def test_rate_limit_error_reconnects(self) -> None:
        first = FakeConnection([{"error": {"name": "TOO_MANY_REQUEST", "message": "slow down"}}])
        second = FakeConnection([TRADE_JSON])
        connector = FakeConnector([first, second])
        ws, sleeps, _ = make_ws(connector)
        messages = await collect(ws, limit=1)
        await ws.close()
        assert len(messages) == 1
        assert sleeps == [1.0]

    async def test_unparseable_message_is_skipped(self) -> None:
        conn = FakeConnection(["garbage", {"type": "mystery"}, TRADE_JSON])
        ws, _, _ = make_ws(FakeConnector([conn]))
        messages = await collect(ws, limit=1)
        await ws.close()
        assert isinstance(messages[0], WsTrade)
        assert ws.stats.parse_errors == 2

    async def test_server_close_triggers_reconnect(self) -> None:
        first = FakeConnection([TRADE_JSON])  # 항목 소진 → ConnectionClosedOK
        second = FakeConnection([TRADE_JSON])
        ws, sleeps, _ = make_ws(FakeConnector([first, second]))
        messages = await collect(ws, limit=2)
        await ws.close()
        assert len(messages) == 2
        assert sleeps == [1.0]
        assert ws.stats.reconnects == 1

    async def test_private_url_and_auth_header(self) -> None:
        conn = FakeConnection([])
        connector = FakeConnector([conn])
        auth = UpbitAuth("access-key-1234567890", "secret-key-1234567890")
        ws, _, _ = make_ws(connector, subscriptions=[Subscription("myOrder")], auth=auth, max_reconnects=0)
        with pytest.raises(UpbitWebSocketError):
            await collect(ws)
        url, headers = connector.calls[0]
        assert url == PRIVATE_WS_URL
        assert headers["Authorization"].startswith("Bearer ")
        assert json.loads(conn.sent[0])[1] == {"type": "myOrder"}

    def test_private_requires_auth(self) -> None:
        with pytest.raises(ValueError):
            UpbitWebSocket([Subscription("myAsset")])
        with pytest.raises(ValueError):
            UpbitWebSocket([])
