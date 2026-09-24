"""Rate Limit 모듈 테스트."""

from __future__ import annotations

import pytest

from app.exchange.rate_limiter import (
    RATE_LIMIT_RULES,
    RateLimiter,
    RateLimitRule,
    SlidingWindowLimiter,
    parse_remaining_req,
    rate_limit_group_for,
)
from tests.conftest import FakeClock


@pytest.mark.parametrize(
    "method, path, group",
    [
        ("GET", "/v1/market/all", "market"),
        ("GET", "/v1/candles/minutes/60", "candle"),
        ("GET", "/v1/candles/days", "candle"),
        ("GET", "/v1/trades/ticks", "trade"),
        ("GET", "/v1/ticker", "ticker"),
        ("GET", "/v1/ticker/all", "ticker"),
        ("GET", "/v1/orderbook", "orderbook"),
        ("GET", "/v1/accounts", "default"),
        ("GET", "/v1/orders/open", "default"),
        ("GET", "/v1/orders/chance", "default"),
        ("POST", "/v1/orders", "order"),
        ("POST", "/v1/orders/test", "order-test"),
        ("DELETE", "/v1/orders/open", "order-cancel-all"),
        ("DELETE", "/v1/order", "default"),
    ],
)
def test_group_mapping(method: str, path: str, group: str) -> None:
    assert rate_limit_group_for(method, path) == group


def test_official_limits_are_encoded() -> None:
    assert RATE_LIMIT_RULES["ticker"].max_requests == 10
    assert RATE_LIMIT_RULES["candle"].max_requests == 10
    assert RATE_LIMIT_RULES["default"].max_requests == 30
    assert RATE_LIMIT_RULES["order"].max_requests == 12
    assert RATE_LIMIT_RULES["order-cancel-all"] == RateLimitRule("order-cancel-all", 1, 2.0)


class TestRemainingReq:
    def test_parse_doc_example(self) -> None:
        r = parse_remaining_req("group=default; min=1800; sec=29")
        assert r is not None
        assert (r.group, r.minute, r.second) == ("default", 1800, 29)

    def test_parse_without_min(self) -> None:
        r = parse_remaining_req("group=ticker; sec=0")
        assert r is not None and r.second == 0 and r.minute is None

    @pytest.mark.parametrize("raw", [None, "", "garbage", "min=1; sec=2"])
    def test_parse_invalid(self, raw: str | None) -> None:
        assert parse_remaining_req(raw) is None


class TestSlidingWindowLimiter:
    async def test_allows_limit_minus_margin_without_waiting(self) -> None:
        clock = FakeClock()
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock.advance(s)

        limiter = SlidingWindowLimiter(RateLimitRule("ticker", 10), clock=clock, sleep=fake_sleep)
        assert limiter.limit == 9
        for _ in range(9):
            await limiter.acquire()
        assert sleeps == []
        assert limiter.used == 9

    async def test_waits_when_window_is_full(self) -> None:
        clock = FakeClock()
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock.advance(s)

        limiter = SlidingWindowLimiter(RateLimitRule("ticker", 10), clock=clock, sleep=fake_sleep)
        for _ in range(9):
            await limiter.acquire()
            clock.advance(0.05)
        await limiter.acquire()  # 10번째: 첫 요청이 윈도우 밖으로 나갈 때까지 기다려야 한다
        assert len(sleeps) == 1
        assert sleeps[0] == pytest.approx(1.0 - 9 * 0.05)

    async def test_block_for_delays_next_acquire(self) -> None:
        clock = FakeClock()
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock.advance(s)

        limiter = SlidingWindowLimiter(RateLimitRule("default", 30), clock=clock, sleep=fake_sleep)
        limiter.block_for(2.5)
        await limiter.acquire()
        assert sleeps == [pytest.approx(2.5)]

    def test_small_limits_keep_full_quota(self) -> None:
        limiter = SlidingWindowLimiter(RateLimitRule("order-cancel-all", 1, 2.0))
        assert limiter.limit == 1


class TestRateLimiter:
    async def test_header_with_zero_remaining_blocks_group(self) -> None:
        clock = FakeClock()
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)
            clock.advance(s)

        limiter = RateLimiter(clock=clock, sleep=fake_sleep)
        remaining = limiter.update_from_headers({"Remaining-Req": "group=ticker; min=599; sec=0"})
        assert remaining is not None and remaining.second == 0
        await limiter.acquire("ticker")
        assert sleeps == [pytest.approx(1.0)]
        await limiter.acquire("candle")  # 다른 그룹은 영향 없음
        assert len(sleeps) == 1

    def test_unknown_group_gets_conservative_rule(self) -> None:
        limiter = RateLimiter()
        assert limiter.limiter("something-new").rule.max_requests == 10
