"""실제 주문 안전장치 (LIVE guard).

실제 주문 API 를 호출하는 모든 코드는 호출 직전에 반드시 ``assert_live_order_allowed()`` 를 거쳐야 한다.
조건은 두 가지이며 **둘 다** 참이어야 한다:

1. ``TRADING_MODE=LIVE``
2. ``LIVE_TRADING_ENABLED=true``

Phase 1 에는 주문 메서드 자체가 없다. 이 모듈은 Phase 7(실제 주문 모듈)에서 사용할 관문을
미리 고정해 두는 것이며, 테스트로 기본 설정에서는 항상 차단됨을 보장한다.
"""

from __future__ import annotations

import logging

from app.config.settings import Settings, TradingMode
from app.core.exceptions import LiveTradingDisabledError

log = logging.getLogger(__name__)

#: LIVE 실행 시 CLI/대시보드에서 입력해야 하는 확인 문구
LIVE_CONFIRM_PHRASE = "REAL-MONEY"


def is_live_order_allowed(settings: Settings) -> bool:
    return settings.trading_mode is TradingMode.LIVE and settings.live_trading_enabled is True


def assert_live_order_allowed(settings: Settings) -> None:
    """허용되지 않으면 ``LiveTradingDisabledError`` 를 던진다. 절대 조용히 통과시키지 않는다."""
    if settings.trading_mode is not TradingMode.LIVE:
        reason = f"TRADING_MODE={settings.trading_mode.value} (LIVE 아님)"
        log.warning("실제 주문 차단: %s", reason)
        raise LiveTradingDisabledError(f"실제 주문이 차단되었습니다: {reason}")
    if settings.live_trading_enabled is not True:
        reason = "LIVE_TRADING_ENABLED=false"
        log.warning("실제 주문 차단: %s", reason)
        raise LiveTradingDisabledError(f"실제 주문이 차단되었습니다: {reason}")
