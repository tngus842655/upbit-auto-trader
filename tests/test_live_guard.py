"""LIVE 안전장치 테스트 — 실수로 실제 주문이 나가지 않는지."""

from __future__ import annotations

import pytest

from app.core.exceptions import LiveTradingDisabledError
from app.exchange.upbit_client import UpbitClient
from app.trading.live_guard import assert_live_order_allowed, is_live_order_allowed


def test_default_settings_block_live_orders(make_settings) -> None:
    settings = make_settings()
    assert is_live_order_allowed(settings) is False
    with pytest.raises(LiveTradingDisabledError, match="TRADING_MODE=PAPER"):
        assert_live_order_allowed(settings)


def test_live_mode_without_flag_blocks(make_settings) -> None:
    settings = make_settings(trading_mode="LIVE")
    with pytest.raises(LiveTradingDisabledError, match="LIVE_TRADING_ENABLED=false"):
        assert_live_order_allowed(settings)


def test_flag_without_live_mode_blocks(make_settings) -> None:
    settings = make_settings(live_trading_enabled=True)
    with pytest.raises(LiveTradingDisabledError):
        assert_live_order_allowed(settings)


def test_both_conditions_pass(make_settings) -> None:
    settings = make_settings(trading_mode="LIVE", live_trading_enabled=True)
    assert is_live_order_allowed(settings) is True
    assert_live_order_allowed(settings)  # 예외 없음


async def test_client_blocks_real_orders_unless_explicitly_allowed(make_settings) -> None:
    """주문 생성·취소는 클라이언트가 allow_orders=True 로 만들어졌을 때만 호출된다 (2차 잠금)."""
    client = UpbitClient(access_key="a" * 20, secret_key="b" * 40)
    try:
        assert client.orders_allowed is False
        with pytest.raises(LiveTradingDisabledError):
            await client.create_order(UpbitClient.market_buy_params("KRW-BTC", 10_000))
        with pytest.raises(LiveTradingDisabledError):
            await client.cancel_order(uuid="x")
    finally:
        await client.aclose()

    default_client = UpbitClient.from_settings(make_settings(upbit_access_key="a" * 20, upbit_secret_key="b" * 40))
    try:
        assert default_client.orders_allowed is False  # PAPER 기본값
    finally:
        await default_client.aclose()

    live_only = UpbitClient.from_settings(
        make_settings(trading_mode="LIVE", upbit_access_key="a" * 20, upbit_secret_key="b" * 40)
    )
    try:
        assert live_only.orders_allowed is False  # 플래그 하나로는 부족
    finally:
        await live_only.aclose()

    armed = UpbitClient.from_settings(
        make_settings(
            trading_mode="LIVE", live_trading_enabled=True, upbit_access_key="a" * 20, upbit_secret_key="b" * 40
        )
    )
    try:
        assert armed.orders_allowed is True
    finally:
        await armed.aclose()


def test_no_withdraw_methods_exist() -> None:
    """출금 API 는 어떤 Phase 에서도 구현하지 않는다."""
    names = [n.lower() for n in dir(UpbitClient) if not n.startswith("__")]
    assert not any("withdraw" in n or "deposit" in n for n in names)
