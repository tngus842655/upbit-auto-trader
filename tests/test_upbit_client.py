"""UpbitClient 테스트 — MockTransport 로 실제 네트워크 없이 요청 형식·인증·오류 처리·재시도를 검증한다."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import jwt
import pytest

from app.core.exceptions import (
    ConfigError,
    UpbitAuthError,
    UpbitBadRequestError,
    UpbitBlockedError,
    UpbitNetworkError,
    UpbitResponseError,
    UpbitServerError,
)
from app.exchange.models import KST, CandleInterval
from app.exchange.upbit_client import MAX_CANDLE_COUNT, UpbitClient, encode_query, format_to_param
from tests.conftest import TEST_ACCESS_KEY, TEST_SECRET_KEY, json_response
from tests.test_models import ACCOUNT_JSON, CANDLE_MINUTE_JSON, TICKER_JSON


def error_response(status: int, name: str | int, message: str = "oops", headers=None) -> httpx.Response:
    return json_response({"error": {"name": name, "message": message}}, status, headers)


def decode_bearer(request: httpx.Request) -> dict:
    token = request.headers["Authorization"].removeprefix("Bearer ")
    return jwt.decode(token, TEST_SECRET_KEY, algorithms=["HS512"])


class TestQuotation:
    async def test_get_tickers_request_and_parse(self, client_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.method == "GET"
            assert str(req.url) == "https://api.upbit.com/v1/ticker?markets=KRW-BTC%2CKRW-ETH"
            assert "Authorization" not in req.headers
            assert req.headers["Accept"] == "application/json"
            assert req.headers["User-Agent"].startswith("upbit-auto-trader")
            return json_response([TICKER_JSON, {**TICKER_JSON, "market": "KRW-ETH"}])

        h = client_factory(handler)
        async with h.client as client:
            tickers = await client.get_tickers(["KRW-BTC", "KRW-ETH"])
        assert [t.market for t in tickers] == ["KRW-BTC", "KRW-ETH"]
        assert h.sleeps == []

    async def test_get_ticker_single(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([TICKER_JSON]))
        async with h.client as client:
            ticker = await client.get_ticker("KRW-BTC")
        assert ticker.trade_price == 56500000.0
        assert h.requests[0].url.query == b"markets=KRW-BTC"

    async def test_get_ticker_empty_response(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]))
        async with h.client as client:
            with pytest.raises(UpbitResponseError):
                await client.get_ticker("KRW-BTC")

    async def test_get_tickers_rejects_empty_input(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]))
        async with h.client as client:
            with pytest.raises(ValueError):
                await client.get_tickers([])
        assert h.requests == []

    async def test_get_candles_minutes_url_and_to(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([CANDLE_MINUTE_JSON]))
        to_kst = datetime(2026, 1, 2, 3, 4, 5, tzinfo=KST)  # UTC 로 바꿔 보낸다
        async with h.client as client:
            candles = await client.get_candles("KRW-BTC", "1h", count=5, to=to_kst)
        req = h.requests[0]
        assert req.url.path == "/v1/candles/minutes/60"
        assert req.url.query == b"market=KRW-BTC&to=2026-01-01T18%3A04%3A05Z&count=5"
        assert candles[0].candle_date_time_utc.tzinfo is UTC

    async def test_get_candles_days_with_converting_unit(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]))
        async with h.client as client:
            await client.get_candles_days("KRW-BTC", count=3, converting_price_unit="KRW")
        assert h.requests[0].url.path == "/v1/candles/days"
        assert h.requests[0].url.query == b"market=KRW-BTC&count=3&converting_price_unit=KRW"

    async def test_get_candles_minutes_helper(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]))
        async with h.client as client:
            await client.get_candles_minutes("KRW-ETH", 15, count=7)
        assert h.requests[0].url.path == "/v1/candles/minutes/15"
        assert h.requests[0].url.query == b"market=KRW-ETH&count=7"

    async def test_get_candles_validation_happens_before_request(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]))
        async with h.client as client:
            with pytest.raises(ValueError):
                await client.get_candles("KRW-BTC", "1m", count=MAX_CANDLE_COUNT + 1)
            with pytest.raises(ValueError):
                await client.get_candles("KRW-BTC", "1m", count=0)
            with pytest.raises(ValueError):
                await client.get_candles("KRW-BTC", "1m", to=datetime(2026, 1, 1))  # 시간대 없음
            with pytest.raises(ValueError):
                await client.get_candles("KRW-BTC", "2m")
        assert h.requests == []

    async def test_get_markets(self, client_factory) -> None:
        h = client_factory(
            lambda req: json_response(
                [{"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin"}]
            )
        )
        async with h.client as client:
            markets = await client.get_markets(is_details=True)
        assert h.requests[0].url.query == b"is_details=true"
        assert markets[0].base_currency == "BTC"

    async def test_non_list_response_is_error(self, client_factory) -> None:
        h = client_factory(lambda req: json_response({"unexpected": 1}))
        async with h.client as client:
            with pytest.raises(UpbitResponseError):
                await client.get_tickers("KRW-BTC")

    async def test_invalid_item_is_error(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([{"market": "KRW-BTC"}]))
        async with h.client as client:
            with pytest.raises(UpbitResponseError):
                await client.get_tickers("KRW-BTC")


class TestCandleRange:
    @staticmethod
    def _make_series(hours: int) -> list[datetime]:
        first = datetime(2026, 1, 1, tzinfo=UTC)
        return [first + timedelta(hours=i) for i in range(hours)]

    @staticmethod
    def _candle(t: datetime) -> dict:
        return {
            **CANDLE_MINUTE_JSON,
            "candle_date_time_utc": t.strftime("%Y-%m-%dT%H:%M:%S"),
            "candle_date_time_kst": (t + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S"),
            "timestamp": int(t.timestamp() * 1000),
        }

    def _handler(self, series: list[datetime], *, inclusive: bool):
        def handler(req: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(req.url.query))
            count = int(params["count"])
            limit = None
            if "to" in params:
                limit = datetime.strptime(params["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            if limit is None:
                selected = series
            elif inclusive:
                selected = [t for t in series if t <= limit]
            else:
                selected = [t for t in series if t < limit]
            newest_first = sorted(selected, reverse=True)[:count]
            return json_response([self._candle(t) for t in newest_first])

        return handler

    async def test_paginates_backwards_and_returns_ascending(self, client_factory) -> None:
        series = self._make_series(457)
        h = client_factory(self._handler(series, inclusive=False))
        start = datetime(2026, 1, 5, tzinfo=UTC)
        end = datetime(2026, 1, 19, 12, tzinfo=UTC)
        async with h.client as client:
            candles = await client.get_candles_range("KRW-BTC", CandleInterval.M60, start=start, end=end)
        times = [c.candle_date_time_utc for c in candles]
        assert times == sorted(times)
        assert len(times) == len(set(times))
        assert times[0] == start
        assert times[-1] == end - timedelta(hours=1)  # `to` 는 그 시각 이전 캔들부터
        assert len(times) == 14 * 24 + 12
        assert len(h.requests) == 2  # 348개 → 200 + 148

    async def test_inclusive_api_does_not_duplicate_or_loop(self, client_factory) -> None:
        series = self._make_series(300)
        h = client_factory(self._handler(series, inclusive=True))
        async with h.client as client:
            candles = await client.get_candles_range(
                "KRW-BTC", "60m", start=series[0], end=series[-1], max_requests=10
            )
        times = [c.candle_date_time_utc for c in candles]
        assert len(times) == len(set(times)) == 300
        assert times[0] == series[0] and times[-1] == series[-1]

    async def test_stops_when_series_is_exhausted(self, client_factory) -> None:
        series = self._make_series(50)
        h = client_factory(self._handler(series, inclusive=False))
        async with h.client as client:
            candles = await client.get_candles_range(
                "KRW-BTC", "1h", start=datetime(2025, 12, 1, tzinfo=UTC), end=datetime(2026, 2, 1, tzinfo=UTC)
            )
        assert len(candles) == 50
        assert len(h.requests) == 1

    async def test_range_validation(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]))
        aware = datetime(2026, 1, 1, tzinfo=UTC)
        async with h.client as client:
            with pytest.raises(ValueError):
                await client.get_candles_range("KRW-BTC", "1h", start=datetime(2026, 1, 1))
            with pytest.raises(ValueError):
                await client.get_candles_range("KRW-BTC", "1h", start=aware, end=aware)
        assert h.requests == []


class TestAuthentication:
    async def test_get_accounts_sends_bearer_jwt_without_query_hash(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([ACCOUNT_JSON]), with_auth=True)
        async with h.client as client:
            assert client.has_auth is True
            accounts = await client.get_accounts()
        req = h.requests[0]
        assert req.url.path == "/v1/accounts"
        assert req.url.query == b""
        payload = decode_bearer(req)
        assert payload["access_key"] == TEST_ACCESS_KEY
        assert payload["nonce"]
        assert "query_hash" not in payload
        assert accounts[0].balance == Decimal("0.00050000")

    async def test_authenticated_get_hashes_unencoded_query(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]), with_auth=True)
        params = [("market", "KRW-BTC"), ("states[]", ["wait", "watch"]), ("limit", 10)]
        async with h.client as client:
            await client._request("GET", "/v1/orders/open", params=params, auth=True)
        req = h.requests[0]
        # 전송 URL: 값은 인코딩, 배열 이름의 [] 는 그대로
        assert str(req.url) == (
            "https://api.upbit.com/v1/orders/open?market=KRW-BTC&states[]=wait&states[]=watch&limit=10"
        )
        payload = decode_bearer(req)
        expected = hashlib.sha512(b"market=KRW-BTC&states[]=wait&states[]=watch&limit=10").hexdigest()
        assert payload["query_hash"] == expected
        assert payload["query_hash_alg"] == "SHA512"

    async def test_post_body_hashed_as_query_string_and_not_retried(self, client_factory) -> None:
        body = {"market": "KRW-BTC", "side": "bid", "volume": "0.001", "price": "50000000", "ord_type": "limit"}
        calls = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            assert req.headers["Content-Type"].startswith("application/json")
            assert json.loads(req.content) == body
            return error_response(500, "server_error")

        h = client_factory(handler, with_auth=True)
        async with h.client as client:
            with pytest.raises(UpbitServerError):
                await client._request("POST", "/v1/orders/test", json_body=body, auth=True)
        assert calls == 1  # 멱등하지 않은 POST 는 재시도하지 않는다
        payload = decode_bearer(h.requests[0])
        expected = hashlib.sha512(
            b"market=KRW-BTC&side=bid&volume=0.001&price=50000000&ord_type=limit"
        ).hexdigest()
        assert payload["query_hash"] == expected

    async def test_auth_required_without_keys_fails_before_request(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]))
        async with h.client as client:
            assert client.has_auth is False
            with pytest.raises(ConfigError):
                await client.get_accounts()
        assert h.requests == []

    async def test_nonce_differs_between_requests(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([]), with_auth=True)
        async with h.client as client:
            await client.get_accounts()
            await client.get_accounts()
        nonces = {decode_bearer(r)["nonce"] for r in h.requests}
        assert len(nonces) == 2


class TestErrorsAndRetries:
    async def test_400_maps_to_bad_request_without_retry(self, client_factory) -> None:
        h = client_factory(lambda req: error_response(400, "invalid_parameter", "잘못된 파라미터"))
        async with h.client as client:
            with pytest.raises(UpbitBadRequestError) as info:
                await client.get_tickers("KRW-BTC")
        assert (info.value.status_code, info.value.name, info.value.message) == (
            400, "invalid_parameter", "잘못된 파라미터",
        )
        assert len(h.requests) == 1
        assert h.sleeps == []

    async def test_quotation_error_name_may_be_integer(self, client_factory) -> None:
        h = client_factory(lambda req: error_response(404, 404, "Code not found"))
        async with h.client as client:
            with pytest.raises(UpbitBadRequestError) as info:
                await client.get_tickers("KRW-NOPE")
        assert info.value.name == 404

    @pytest.mark.parametrize("status, name", [(401, "jwt_verification"), (403, "out_of_scope")])
    async def test_auth_errors_not_retried(self, client_factory, status: int, name: str) -> None:
        h = client_factory(lambda req: error_response(status, name), with_auth=True)
        async with h.client as client:
            with pytest.raises(UpbitAuthError) as info:
                await client.get_accounts()
        assert info.value.name == name
        assert len(h.requests) == 1

    async def test_418_blocked_not_retried(self, client_factory) -> None:
        h = client_factory(lambda req: error_response(418, "too_many_requests", "blocked"))
        async with h.client as client:
            with pytest.raises(UpbitBlockedError):
                await client.get_tickers("KRW-BTC")
        assert len(h.requests) == 1
        assert h.sleeps == []

    async def test_429_is_retried_after_one_second(self, client_factory) -> None:
        responses = [
            error_response(429, "too_many_requests", headers={"Remaining-Req": "group=ticker; min=0; sec=0"}),
            json_response([TICKER_JSON]),
        ]
        h = client_factory(lambda req: responses.pop(0))
        async with h.client as client:
            tickers = await client.get_tickers("KRW-BTC")
        assert len(tickers) == 1
        assert len(h.requests) == 2
        assert h.sleeps == [pytest.approx(1.0)]

    async def test_5xx_retried_with_backoff_then_success(self, client_factory) -> None:
        responses = [
            error_response(500, "server_error"),
            error_response(503, "unavailable"),
            json_response([TICKER_JSON]),
        ]
        h = client_factory(lambda req: responses.pop(0))
        async with h.client as client:
            tickers = await client.get_tickers("KRW-BTC")
        assert len(tickers) == 1
        assert len(h.requests) == 3
        assert h.sleeps == [pytest.approx(0.5), pytest.approx(1.0)]

    async def test_5xx_exhausts_retries(self, client_factory) -> None:
        h = client_factory(lambda req: error_response(500, "server_error"), max_retries=2)
        async with h.client as client:
            with pytest.raises(UpbitServerError):
                await client.get_tickers("KRW-BTC")
        assert len(h.requests) == 3
        assert h.sleeps == [pytest.approx(0.5), pytest.approx(1.0)]

    async def test_network_error_is_retried(self, client_factory) -> None:
        calls = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ConnectError("boom", request=req)
            return json_response([TICKER_JSON])

        h = client_factory(handler)
        async with h.client as client:
            tickers = await client.get_tickers("KRW-BTC")
        assert len(tickers) == 1
        assert calls == 3
        assert h.sleeps == [pytest.approx(0.5), pytest.approx(1.0)]

    async def test_timeout_becomes_network_error_after_retries(self, client_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=req)

        h = client_factory(handler, max_retries=1)
        async with h.client as client:
            with pytest.raises(UpbitNetworkError):
                await client.get_tickers("KRW-BTC")
        assert len(h.requests) == 2

    async def test_error_without_json_body(self, client_factory) -> None:
        h = client_factory(lambda req: httpx.Response(502, text="<html>Bad Gateway</html>"), max_retries=0)
        async with h.client as client:
            with pytest.raises(UpbitServerError) as info:
                await client.get_tickers("KRW-BTC")
        assert info.value.name is None
        assert "Bad Gateway" in info.value.message

    async def test_remaining_req_zero_blocks_next_call(self, client_factory) -> None:
        h = client_factory(
            lambda req: json_response([TICKER_JSON], headers={"Remaining-Req": "group=ticker; min=10; sec=0"})
        )
        async with h.client as client:
            await client.get_tickers("KRW-BTC")
            await client.get_tickers("KRW-BTC")
        assert h.sleeps == [pytest.approx(1.0)]

    async def test_rate_limiter_paces_burst(self, client_factory) -> None:
        h = client_factory(lambda req: json_response([TICKER_JSON]))
        async with h.client as client:
            for _ in range(10):
                await client.get_tickers("KRW-BTC")
        assert len(h.requests) == 10
        assert h.sleeps == [pytest.approx(1.0)]  # 9회는 즉시, 10번째는 윈도우가 지날 때까지 대기


class TestHelpers:
    def test_encode_query_keeps_brackets_and_encodes_values(self) -> None:
        params = [("states[]", ["wait", "watch"]), ("to", "2025-06-24T04:56:53Z"), ("markets", "KRW-BTC,KRW-ETH")]
        assert encode_query(params) == (
            "states[]=wait&states[]=watch&to=2025-06-24T04%3A56%3A53Z&markets=KRW-BTC%2CKRW-ETH"
        )

    def test_format_to_param(self) -> None:
        assert format_to_param(None) is None
        assert format_to_param("2025-06-24T04:56:53Z") == "2025-06-24T04:56:53Z"
        assert format_to_param(datetime(2025, 6, 24, 13, 56, 53, tzinfo=KST)) == "2025-06-24T04:56:53Z"
        with pytest.raises(ValueError):
            format_to_param(datetime(2025, 6, 24))

    async def test_from_settings(self, make_settings) -> None:
        settings = make_settings(
            upbit_access_key="abcd1234", upbit_secret_key="secret", http_timeout_seconds=3,
            upbit_api_url="https://example.test/",
        )
        client = UpbitClient.from_settings(settings)
        try:
            assert client.has_auth is True
            assert str(client._client.base_url).rstrip("/") == "https://example.test"
            assert client._client.timeout.read == 3
        finally:
            await client.aclose()

        anonymous = UpbitClient.from_settings(make_settings())
        try:
            assert anonymous.has_auth is False
        finally:
            await anonymous.aclose()
