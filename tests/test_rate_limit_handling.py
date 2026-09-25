"""감사 MEDIUM-14 회귀 — Retry-After 준수, 헤더 없으면 기본 대기, 418 은 그룹 차단, 그룹 매핑(문서 2026-09-25)."""

from __future__ import annotations

import httpx
import pytest

from app.core.exceptions import UpbitBlockedError, make_api_error
from app.exchange.rate_limiter import RateLimiter, parse_retry_after, rate_limit_group_for
from app.exchange.upbit_client import UpbitClient

MARKETS = [{"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin"}]
LIMIT = {"error": {"name": 429, "message": "too many"}}


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after(None) is None and parse_retry_after("") is None and parse_retry_after("abc") is None
    assert parse_retry_after("3") == 3.0 and parse_retry_after("0.5") == 0.5 and parse_retry_after("-1") is None
    assert parse_retry_after("1500") == 1.5  # 밀리초로 보이는 값
    later = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert later is not None and later > 0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # 이미 지난 시각


async def test_retry_delay_prefers_server_hint() -> None:
    async with UpbitClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        assert client._retry_delay(1, make_api_error(429, 429, "limit")) == 1.0  # 헤더 없음: 문서 권고 1초
        assert client._retry_delay(1, make_api_error(429, 429, "limit", retry_after=2.5)) == 2.5
        assert client._retry_delay(1, make_api_error(429, 429, "limit", retry_after=999)) == 60.0  # 상한
        assert client._retry_delay(2, make_api_error(500, 500, "err")) == 1.0  # 지수 백오프 0.5×2


class Env:
    """가짜 전송 계층 + 가짜 시계/대기. 응답은 (status, headers, json) 순서대로."""

    def __init__(self, responses, **client_kwargs) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.sleeps: list[float] = []
        self.now = 1000.0

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

        self.limiter = RateLimiter(clock=lambda: self.now, sleep=fake_sleep)
        self.client = UpbitClient(rate_limiter=self.limiter, transport=httpx.MockTransport(self.handler),
                                  sleep=fake_sleep, max_retries=3, **client_kwargs)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        status, headers, body = self.responses.pop(0)
        return httpx.Response(status, headers=headers, json=body)


async def test_429_waits_retry_after_then_succeeds() -> None:
    env = Env([
        (429, {"Retry-After": "2", "Remaining-Req": "group=market; min=0; sec=0"}, LIMIT),
        (200, {"Remaining-Req": "group=market; min=0; sec=9"}, MARKETS),
    ])
    async with env.client as client:
        markets = await client.get_markets()
    assert [m.market for m in markets] == ["KRW-BTC"] and env.calls == 2
    assert env.sleeps[0] == 2.0  # 재시도 대기는 Retry-After


async def test_429_without_header_waits_one_second() -> None:
    env = Env([(429, {}, LIMIT), (200, {}, MARKETS)])  # Remaining-Req 도 없음 — 정상 처리
    async with env.client as client:
        assert len(await client.get_markets()) == 1
    assert env.calls == 2 and env.sleeps[0] == 1.0


async def test_418_is_not_retried_and_blocks_group() -> None:
    env = Env([(418, {"Retry-After": "30"}, {"error": {"name": 418, "message": "blocked"}})])
    async with env.client as client:
        with pytest.raises(UpbitBlockedError) as info:
            await client.get_markets()
    assert env.calls == 1 and info.value.retry_after == 30.0
    assert env.limiter.limiter("market")._blocked_until >= env.now + 29  # 안내 시간 동안 그 그룹 요청 중단
    env2 = Env([(418, {}, {"error": {"name": 418, "message": "blocked"}})], blocked_cooldown=45.0)
    async with env2.client as client:
        with pytest.raises(UpbitBlockedError):
            await client.get_markets()
    assert env2.limiter.limiter("market")._blocked_until >= env2.now + 44  # 안내 없으면 기본 차단 시간


def test_group_mapping_matches_official_doc() -> None:
    """2026-09-25 reference/rate-limits 원문: 개별 취소·조회는 default, 생성은 order, 일괄 취소만 order-cancel-all."""
    assert rate_limit_group_for("DELETE", "/v1/order") == "default"
    assert rate_limit_group_for("GET", "/v1/order") == "default"
    assert rate_limit_group_for("GET", "/v1/orders/chance") == "default"
    assert rate_limit_group_for("DELETE", "/v1/orders/uuids") == "default"
    assert rate_limit_group_for("POST", "/v1/orders") == "order"
    assert rate_limit_group_for("POST", "/v1/orders/cancel_and_new") == "order"
    assert rate_limit_group_for("POST", "/v1/orders/test") == "order-test"
    assert rate_limit_group_for("DELETE", "/v1/orders/open") == "order-cancel-all"
    assert rate_limit_group_for("GET", "/v1/candles/minutes/60") == "candle"
