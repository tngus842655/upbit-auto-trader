"""업비트 REST 응답 모델 (pydantic).

공식 API Reference 의 OpenAPI 정의를 기준으로 작성했다 (2026-09 기준):
- 페어 목록: https://docs.upbit.com/kr/reference/list-trading-pairs
- 현재가:    https://docs.upbit.com/kr/reference/list-tickers
- 캔들:      https://docs.upbit.com/kr/reference/list-candles-minutes 등
- 잔고:      https://docs.upbit.com/kr/reference/get-balance

숫자 타입 정책:
- 시세·캔들 가격/거래량은 API 가 JSON number 로 주므로 ``float`` (지표 계산·pandas 친화적).
- 잔고(``Account``)는 API 가 문자열 소수로 주며 주문 금액 계산에 쓰이므로 ``Decimal`` 로 유지한다.

알 수 없는 필드는 버리지 않고 보존한다(``extra="allow"``) — API 가 필드를 추가해도 깨지지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

KST = timezone(timedelta(hours=9), name="KST")


class UpbitModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class Market(UpbitModel):
    """``GET /v1/market/all`` 의 원소."""

    market: str
    korean_name: str
    english_name: str
    market_event: dict[str, Any] | None = None

    @property
    def quote_currency(self) -> str:
        """호가 통화 (KRW-BTC → KRW)."""
        return self.market.split("-", 1)[0]

    @property
    def base_currency(self) -> str:
        """거래 자산 (KRW-BTC → BTC)."""
        return self.market.split("-", 1)[1]

    @property
    def has_warning(self) -> bool:
        """유의 종목 지정 여부 (``market_event.warning``)."""
        return bool(self.market_event and self.market_event.get("warning"))


class Ticker(UpbitModel):
    """``GET /v1/ticker`` 의 원소 (현재가 스냅샷)."""

    market: str
    trade_date: str
    trade_time: str
    trade_date_kst: str
    trade_time_kst: str
    trade_timestamp: int
    opening_price: float
    high_price: float
    low_price: float
    trade_price: float
    prev_closing_price: float
    change: str  # EVEN | RISE | FALL
    change_price: float
    change_rate: float
    signed_change_price: float
    signed_change_rate: float
    trade_volume: float
    acc_trade_price: float
    acc_trade_price_24h: float
    acc_trade_volume: float
    acc_trade_volume_24h: float
    highest_52_week_price: float
    highest_52_week_date: str
    lowest_52_week_price: float
    lowest_52_week_date: str
    timestamp: int

    @property
    def trade_datetime_utc(self) -> datetime:
        return datetime.fromtimestamp(self.trade_timestamp / 1000, tz=UTC)

    @property
    def trade_datetime_kst(self) -> datetime:
        return self.trade_datetime_utc.astimezone(KST)


#: 사용자 편의 표기 → CandleInterval 값
_INTERVAL_ALIASES = {"1h": "60m", "4h": "240m", "1mo": "1M", "1month": "1M"}


class CandleInterval(StrEnum):
    """캔들 조회 단위. 값은 설정·CLI 에서 쓰는 짧은 표기."""

    S1 = "1s"
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"
    M30 = "30m"
    M60 = "60m"
    M240 = "240m"
    D1 = "1d"
    W1 = "1w"
    MON1 = "1M"
    Y1 = "1y"

    @classmethod
    def parse(cls, value: str | CandleInterval) -> CandleInterval:
        if isinstance(value, cls):
            return value
        text = str(value).strip()
        text = _INTERVAL_ALIASES.get(text.lower(), text)
        for member in cls:
            if member.value == text:
                return member
        valid = ", ".join(m.value for m in cls)
        raise ValueError(f"지원하지 않는 캔들 단위 '{value}'. 사용 가능: {valid}, 1h, 4h")

    @property
    def path(self) -> str:
        """REST 경로. 분봉만 ``/v1/candles/minutes/{unit}`` 형태다."""
        if self is CandleInterval.S1:
            return "/v1/candles/seconds"
        if self.value.endswith("m"):
            return f"/v1/candles/minutes/{self.minute_unit}"
        return {
            CandleInterval.D1: "/v1/candles/days",
            CandleInterval.W1: "/v1/candles/weeks",
            CandleInterval.MON1: "/v1/candles/months",
            CandleInterval.Y1: "/v1/candles/years",
        }[self]

    @property
    def minute_unit(self) -> int | None:
        return int(self.value[:-1]) if self.value.endswith("m") else None

    @property
    def seconds(self) -> int:
        """한 캔들의 대략적 길이(초). 페이지네이션 계산용."""
        if self is CandleInterval.S1:
            return 1
        if self.minute_unit is not None:
            return self.minute_unit * 60
        return {
            CandleInterval.D1: 86_400,
            CandleInterval.W1: 7 * 86_400,
            CandleInterval.MON1: 30 * 86_400,
            CandleInterval.Y1: 365 * 86_400,
        }[self]


class Candle(UpbitModel):
    """캔들 한 개. 초/분/일/주/월/연 공통 필드 + 종류별 선택 필드.

    업비트 API 는 캔들을 **최신순(내림차순)** 으로 돌려준다.
    시각 문자열(``yyyy-MM-dd'T'HH:mm:ss``)은 시간대가 없으므로 UTC / KST 를 명시적으로 붙인다.
    """

    market: str
    candle_date_time_utc: datetime
    candle_date_time_kst: datetime
    opening_price: float
    high_price: float
    low_price: float
    trade_price: float
    timestamp: int
    candle_acc_trade_price: float
    candle_acc_trade_volume: float
    # 분봉
    unit: int | None = None
    # 일봉
    prev_closing_price: float | None = None
    change_price: float | None = None
    change_rate: float | None = None
    converted_trade_price: float | None = None
    # 주/월/연봉
    first_day_of_period: str | None = None

    @field_validator("candle_date_time_utc")
    @classmethod
    def _attach_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @field_validator("candle_date_time_kst")
    @classmethod
    def _attach_kst(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)

    # 전략·백테스트 코드에서 쓰기 편한 짧은 별칭
    @property
    def open(self) -> float:
        return self.opening_price

    @property
    def high(self) -> float:
        return self.high_price

    @property
    def low(self) -> float:
        return self.low_price

    @property
    def close(self) -> float:
        return self.trade_price

    @property
    def volume(self) -> float:
        return self.candle_acc_trade_volume


class Account(UpbitModel):
    """``GET /v1/accounts`` 의 원소 (포켓 잔고)."""

    currency: str
    balance: Decimal
    locked: Decimal
    avg_buy_price: Decimal
    avg_buy_price_modified: bool
    unit_currency: str

    @property
    def total(self) -> Decimal:
        """주문 가능 수량 + 주문·출금에 묶인 수량."""
        return self.balance + self.locked

    @property
    def is_fiat(self) -> bool:
        return self.currency == "KRW"


class OrderTrade(UpbitModel):
    """주문 1건의 개별 체결 (``GET /v1/order`` 의 ``trades`` 원소)."""

    market: str
    uuid: str
    price: Decimal
    volume: Decimal
    funds: Decimal  # 체결 금액 (price × volume)
    side: str
    created_at: str
    trend: str | None = None


class OrderInfo(UpbitModel):
    """주문 생성/조회/취소 응답 (https://docs.upbit.com/kr/reference/new-order 의 Order 스키마).

    ``state``: wait(체결 대기) / watch(예약 대기) / done(체결 완료) / cancel(취소).
    ``ord_type``: limit(지정가) / price(시장가 매수, price=총액) / market(시장가 매도, volume=수량) / best(최유리).
    목록 조회 응답에는 ``trades`` 가 없고 개별 조회에만 포함된다.
    """

    market: str
    uuid: str
    side: str  # bid(매수) / ask(매도)
    ord_type: str
    state: str
    created_at: str
    executed_volume: Decimal = Decimal("0")
    paid_fee: Decimal = Decimal("0")
    reserved_fee: Decimal = Decimal("0")
    remaining_fee: Decimal = Decimal("0")
    locked: Decimal = Decimal("0")
    trades_count: int = 0
    price: Decimal | None = None
    volume: Decimal | None = None
    remaining_volume: Decimal | None = None
    time_in_force: str | None = None
    identifier: str | None = None
    smp_type: str | None = None
    prevented_volume: Decimal | None = None
    prevented_locked: Decimal | None = None
    trades: list[OrderTrade] = []

    @property
    def is_open(self) -> bool:
        return self.state in ("wait", "watch")

    @property
    def is_final(self) -> bool:
        return self.state in ("done", "cancel")

    @property
    def executed_funds(self) -> Decimal:
        """체결 금액 합계 (trades 기준). 목록 응답처럼 trades 가 없으면 0."""
        return sum((t.funds for t in self.trades), Decimal("0"))

    @property
    def average_price(self) -> Decimal | None:
        if self.executed_volume > 0 and self.trades:
            return self.executed_funds / self.executed_volume
        return None


class OrderChance(UpbitModel):
    """``GET /v1/orders/chance`` — 수수료율, 페어 제약(최소 주문 금액 등), 양쪽 계좌 잔고."""

    bid_fee: Decimal
    ask_fee: Decimal
    maker_bid_fee: Decimal
    maker_ask_fee: Decimal
    market: dict[str, Any]
    bid_account: Account
    ask_account: Account

    def _constraint(self, side: str, key: str) -> Decimal | None:
        block = self.market.get(side) or {}
        value = block.get(key)
        return Decimal(str(value)) if value is not None else None

    @property
    def min_total_bid(self) -> Decimal | None:
        return self._constraint("bid", "min_total")

    @property
    def min_total_ask(self) -> Decimal | None:
        return self._constraint("ask", "min_total")

    @property
    def max_total(self) -> Decimal | None:
        value = self.market.get("max_total")
        return Decimal(str(value)) if value is not None else None

    @property
    def market_state(self) -> str | None:
        return self.market.get("state")

    @property
    def bid_types(self) -> list[str]:
        return list(self.market.get("bid_types") or [])

    @property
    def ask_types(self) -> list[str]:
        return list(self.market.get("ask_types") or [])
