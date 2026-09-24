"""알림 기반 모델 — 이벤트 종류, 이벤트, 채널 인터페이스, 메시지 서식 (Phase 9).

지시서 19절: 매수 / 매도 / 주문 체결 / 손절 / 일일 손실 제한 도달 / API 오류 / 봇 중지 / 봇 재시작 을
보낼 수 있어야 하고, Telegram·Discord 같은 채널을 쉽게 추가할 수 있는 구조여야 한다.

- 채널은 ``Notifier`` 프로토콜(``send`` / ``aclose``) 만 구현하면 된다. 실패는 ``NotifyError`` 로 알린다.
- 엔진은 ``NotificationEvent`` 를 만들어 ``NotificationManager.emit`` 에 넘길 뿐, 채널·전송 실패를 모른다.
  알림이 매매를 늦추거나 멈추게 하면 안 되기 때문이다(전송은 백그라운드 큐).
- 이 모듈은 설정·엔진을 import 하지 않는다 (설정 검증에서 역방향으로 쓰기 때문).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.exchange.models import KST


class EventKind(StrEnum):
    BUY = "buy"  # 매수 주문 제출 (리스크 검사 통과)
    SELL = "sell"  # 전략 신호에 의한 매도 주문 제출
    ORDER_FILLED = "order_filled"  # 주문 체결 확인 (매수·매도 공통, 손익 포함)
    ORDER_REJECTED = "order_rejected"  # 주문 거부·미체결
    STOP_LOSS = "stop_loss"  # 손절 청산 발동
    TAKE_PROFIT = "take_profit"  # 익절 청산 발동
    TRAILING_STOP = "trailing_stop"  # 추적 손절 발동
    DAILY_LOSS_LIMIT = "daily_loss_limit"  # 일일 손실 한도 도달 → 당일 신규 진입 잠금
    CONSECUTIVE_LOSS_LIMIT = "consecutive_loss_limit"  # 연속 손실 한도 도달
    RISK_HALT = "risk_halt"  # 긴급 정지·해제
    API_ERROR = "api_error"  # 거래소 API·시세 스트림 오류
    BOT_START = "bot_start"  # 봇 시작·재시작(복구)
    BOT_STOP = "bot_stop"  # 봇 중지 (정상·비정상)
    SETTINGS = "settings"  # 실행 설정 반영·검증 실패


ALL_KINDS: tuple[EventKind, ...] = tuple(EventKind)

LABELS: dict[EventKind, str] = {
    EventKind.BUY: "매수",
    EventKind.SELL: "매도",
    EventKind.ORDER_FILLED: "주문 체결",
    EventKind.ORDER_REJECTED: "주문 거부",
    EventKind.STOP_LOSS: "손절",
    EventKind.TAKE_PROFIT: "익절",
    EventKind.TRAILING_STOP: "추적 손절",
    EventKind.DAILY_LOSS_LIMIT: "일일 손실 한도",
    EventKind.CONSECUTIVE_LOSS_LIMIT: "연속 손실 한도",
    EventKind.RISK_HALT: "긴급 정지",
    EventKind.API_ERROR: "API 오류",
    EventKind.BOT_START: "봇 시작",
    EventKind.BOT_STOP: "봇 중지",
    EventKind.SETTINGS: "설정",
}

EMOJI: dict[EventKind, str] = {
    EventKind.BUY: "🟢",
    EventKind.SELL: "🔴",
    EventKind.ORDER_FILLED: "✅",
    EventKind.ORDER_REJECTED: "⚠️",
    EventKind.STOP_LOSS: "🛑",
    EventKind.TAKE_PROFIT: "🎯",
    EventKind.TRAILING_STOP: "📉",
    EventKind.DAILY_LOSS_LIMIT: "🚫",
    EventKind.CONSECUTIVE_LOSS_LIMIT: "🚫",
    EventKind.RISK_HALT: "⛔",
    EventKind.API_ERROR: "❌",
    EventKind.BOT_START: "▶️",
    EventKind.BOT_STOP: "⏹️",
    EventKind.SETTINGS: "⚙️",
}


@dataclass(slots=True)
class NotificationEvent:
    kind: EventKind
    title: str
    message: str = ""
    mode: str = "paper"
    time: datetime = field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = field(default_factory=dict)
    # 같은 key 의 반복(예: 같은 마켓의 캔들 조회 실패가 매 분 반복)은 쿨다운 동안 한 번만 보낸다
    key: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "label": self.label, "title": self.title, "message": self.message,
            "mode": self.mode, "time": self.time.astimezone(KST).isoformat(), "key": self.key, "data": self.data,
        }


def format_event(event: NotificationEvent, *, max_length: int | None = None) -> str:
    """채널 공통 본문. 첫 줄에 이모지·모드·종류·제목, 둘째 줄에 상세, 마지막 줄에 KST 시각."""
    stamp = event.time.astimezone(KST).strftime("%m-%d %H:%M:%S")
    lines = [f"{EMOJI.get(event.kind, '•')} [{event.mode.upper()}] {event.label} — {event.title}"]
    if event.message:
        lines.append(event.message)
    lines.append(f"{stamp} KST")
    text = "\n".join(lines)
    if max_length is not None and len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return text


class NotifyError(Exception):
    """채널 전송 실패. ``retryable`` 이 False 면(잘못된 토큰·채팅 ID 등) 재시도하지 않는다."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class Notifier(Protocol):
    name: str

    async def send(self, event: NotificationEvent) -> None: ...

    async def aclose(self) -> None: ...


def parse_event_kinds(value: Any) -> list[EventKind]:
    """``NOTIFY_EVENTS`` 해석: ``all`` / ``off``(빈값·none) / ``buy,sell,stop_loss`` 같은 쉼표 목록.

    잘못된 이름은 ValueError.
    """
    if value is None:
        return list(ALL_KINDS)
    items: list[str]
    if isinstance(value, str):
        items = [v.strip().lower() for v in value.split(",")]
    else:
        items = [str(v).strip().lower() for v in value]
    items = [v for v in items if v]
    if not items or items == ["off"] or items == ["none"]:
        return []
    if "all" in items:
        return list(ALL_KINDS)
    valid = {k.value: k for k in EventKind}
    unknown = [v for v in items if v not in valid]
    if unknown:
        raise ValueError(f"알 수 없는 알림 이벤트 {unknown}. 사용 가능: all, off, {', '.join(valid)}")
    return list(dict.fromkeys(valid[v] for v in items))
