"""업비트 WebSocket 수신 메시지 모델 (DEFAULT 포맷).

공식 문서 (2026-09-22 갱신본):
- 현재가  https://docs.upbit.com/kr/reference/websocket-ticker
- 체결    https://docs.upbit.com/kr/reference/websocket-trade
- 호가    https://docs.upbit.com/kr/reference/websocket-orderbook
- 캔들    https://docs.upbit.com/kr/reference/websocket-candle
- 공통    https://docs.upbit.com/kr/reference/websocket-guide (에러 형식, {"status":"UP"} 상태 메시지)

모든 데이터 메시지는 ``type`` / ``code`` / ``timestamp`` / ``stream_type``(SNAPSHOT|REALTIME) 을 갖는다.
문서에 Deprecated 로 표시된 ``is_trading_suspended``, ``market_warning`` 은 참조하지 않는다 (extra 로만 보존).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.core.exceptions import UpbitResponseError
from app.exchange.models import KST, CandleInterval

StreamType = Literal["SNAPSHOT", "REALTIME"]


class WsModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class WsDataMessage(WsModel):
    """시세 데이터 메시지 공통 필드."""

    type: str
    code: str
    timestamp: int
    stream_type: StreamType

    @property
    def is_snapshot(self) -> bool:
        return self.stream_type == "SNAPSHOT"

    @property
    def received_at_utc(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp / 1000, tz=UTC)


class WsTicker(WsDataMessage):
    """``type: ticker`` — 현재가."""

    opening_price: float
    high_price: float
    low_price: float
    trade_price: float
    prev_closing_price: float
    change: str
    change_price: float
    signed_change_price: float
    change_rate: float
    signed_change_rate: float
    trade_volume: float
    acc_trade_volume: float
    acc_trade_volume_24h: float
    acc_trade_price: float
    acc_trade_price_24h: float
    trade_date: str
    trade_time: str
    trade_timestamp: int
    ask_bid: str
    acc_ask_volume: float
    acc_bid_volume: float
    highest_52_week_price: float | None = None
    highest_52_week_date: str | None = None
    lowest_52_week_price: float | None = None
    lowest_52_week_date: str | None = None
    market_state: str | None = None  # PREVIEW | ACTIVE | DELISTED
    delisting_date: str | None = None

    @property
    def is_tradable(self) -> bool:
        return self.market_state in (None, "ACTIVE")


class WsTrade(WsDataMessage):
    """``type: trade`` — 개별 체결."""

    trade_price: float
    trade_volume: float
    ask_bid: str  # ASK: 매도 체결, BID: 매수 체결
    prev_closing_price: float
    change: str
    change_price: float
    trade_date: str  # yyyy-MM-dd (UTC)
    trade_time: str  # HH:mm:ss (UTC)
    trade_timestamp: int
    sequential_id: int
    best_ask_price: float | None = None
    best_ask_size: float | None = None
    best_bid_price: float | None = None
    best_bid_size: float | None = None

    @property
    def trade_datetime_utc(self) -> datetime:
        return datetime.fromtimestamp(self.trade_timestamp / 1000, tz=UTC)

    @property
    def amount(self) -> float:
        """체결 금액 (가격 × 수량)."""
        return self.trade_price * self.trade_volume


class OrderbookUnit(WsModel):
    ask_price: float
    bid_price: float
    ask_size: float
    bid_size: float


class WsOrderbook(WsDataMessage):
    """``type: orderbook`` — 호가. ``orderbook_units[0]`` 이 최우선 호가."""

    total_ask_size: float
    total_bid_size: float
    orderbook_units: list[OrderbookUnit]
    level: float = 0

    @property
    def best_ask(self) -> OrderbookUnit | None:
        return self.orderbook_units[0] if self.orderbook_units else None

    @property
    def best_ask_price(self) -> float | None:
        return self.orderbook_units[0].ask_price if self.orderbook_units else None

    @property
    def best_bid_price(self) -> float | None:
        return self.orderbook_units[0].bid_price if self.orderbook_units else None

    @property
    def spread(self) -> float | None:
        if not self.orderbook_units:
            return None
        return self.orderbook_units[0].ask_price - self.orderbook_units[0].bid_price

    @property
    def mid_price(self) -> float | None:
        if not self.orderbook_units:
            return None
        return (self.orderbook_units[0].ask_price + self.orderbook_units[0].bid_price) / 2


class WsCandle(WsDataMessage):
    """``type: candle.{unit}`` — 실시간 캔들. 같은 기준 시각의 캔들이 여러 번 오면 마지막 것이 최신이다."""

    candle_date_time_utc: datetime
    candle_date_time_kst: datetime
    opening_price: float
    high_price: float
    low_price: float
    trade_price: float
    candle_acc_trade_volume: float
    candle_acc_trade_price: float

    @field_validator("candle_date_time_utc")
    @classmethod
    def _attach_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @field_validator("candle_date_time_kst")
    @classmethod
    def _attach_kst(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)

    @property
    def interval(self) -> CandleInterval:
        return CandleInterval.parse(self.type.split(".", 1)[1])


class WsStatus(WsModel):
    """``{"status": "UP"}`` — 클라이언트가 "PING" 텍스트를 보냈을 때 서버가 주는 상태 메시지."""

    status: str


class WsErrorMessage(WsModel):
    """``{"error": {"name": ..., "message": ...}}``."""

    name: str
    message: str

    #: 요청 자체가 잘못된 경우 — 재연결해도 같은 결과이므로 즉시 중단해야 한다.
    FATAL_NAMES: ClassVar[frozenset[str]] = frozenset(
        {"INVALID_AUTH", "WRONG_FORMAT", "NO_TICKET", "NO_TYPE", "NO_CODES", "INVALID_PARAM"}
    )

    @property
    def is_fatal(self) -> bool:
        return self.name in self.FATAL_NAMES


WsMessage = WsTicker | WsTrade | WsOrderbook | WsCandle | WsStatus | WsErrorMessage

_TYPE_MODELS: dict[str, type[WsDataMessage]] = {
    "ticker": WsTicker,
    "trade": WsTrade,
    "orderbook": WsOrderbook,
}


def parse_ws_message(raw: str | bytes) -> WsMessage:
    """수신 프레임 하나를 모델로 바꾼다. 형식을 모르면 ``UpbitResponseError``."""
    try:
        data: Any = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise UpbitResponseError(f"WebSocket JSON 해석 실패: {raw[:120]!r}") from exc
    if not isinstance(data, dict):
        raise UpbitResponseError(f"WebSocket 메시지가 객체가 아닙니다: {type(data).__name__}")

    try:
        if "error" in data and isinstance(data["error"], dict):
            return WsErrorMessage.model_validate(data["error"])
        if "status" in data and "type" not in data:
            return WsStatus.model_validate(data)
        msg_type = data.get("type")
        if not isinstance(msg_type, str):
            raise UpbitResponseError(f"WebSocket 메시지에 type 이 없습니다: {list(data)[:8]}")
        if msg_type.startswith("candle."):
            return WsCandle.model_validate(data)
        model = _TYPE_MODELS.get(msg_type)
        if model is None:
            raise UpbitResponseError(f"지원하지 않는 WebSocket 메시지 타입: {msg_type}")
        return model.model_validate(data)
    except ValidationError as exc:
        raise UpbitResponseError(f"WebSocket 메시지 해석 실패({data.get('type')}): {exc}") from exc
