"""시장 상태: 마켓별 닫힌 캔들(롤링 DataFrame)과 최신 시세(현재가·호가).

- 캔들은 REST 로 받은 목록을 ``merge_candles`` 로 합친다 (중복 제거·정렬·진행 중 캔들 제거·최대 행 수 유지).
  새로 확정된(닫힌) 캔들 시각 목록을 돌려주므로 엔진은 그때만 전략을 돌린다.
- 시세는 WebSocket 메시지(ticker / trade / orderbook)로 갱신한다. 가상 체결가와 평가액 계산에 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from app.exchange.models import Candle, CandleInterval
from app.exchange.ws_models import WsMessage, WsOrderbook, WsTicker, WsTrade
from app.strategy.data import candles_to_dataframe, drop_unclosed


@dataclass
class PriceState:
    market: str
    last_price: float | None = None
    last_time: datetime | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    book_time: datetime | None = None
    # 호가가 최신 체결가(또는 REST 보정) 시각보다 이만큼 넘게 오래됐으면 평가에 쓰지 않는다 (감사 CRITICAL-2)
    book_max_age_seconds: float = 30.0

    def is_fresh(self, now: datetime, max_age_seconds: float) -> bool:
        latest = max((t for t in (self.last_time, self.book_time) if t is not None), default=None)
        return latest is not None and (now - latest).total_seconds() <= max_age_seconds

    @property
    def book_usable(self) -> bool:
        """호가를 평가에 써도 되는지 — 호가가 있고, 최신 체결가보다 ``book_max_age_seconds`` 넘게 뒤처지지 않았을 때.

        WebSocket 이 끊겨 호가는 몇 시간 전인데 REST 보정 체결가는 방금이면 호가를 버린다.
        """
        if not (self.best_bid and self.best_ask):
            return False
        if self.book_time is None or self.last_time is None:
            return True
        return (self.last_time - self.book_time).total_seconds() <= self.book_max_age_seconds

    @property
    def mark_price(self) -> float | None:
        """평가용 가격: 최신 호가 중간값 → 마지막 체결가 순. 호가가 오래됐으면 체결가."""
        if self.book_usable:
            return (self.best_bid + self.best_ask) / 2
        return self.last_price


@dataclass
class MarketState:
    interval: CandleInterval
    max_rows: int = 1000
    price_max_age_seconds: float = 30.0  # PriceState.book_max_age_seconds 로 전달
    candles: dict[str, pd.DataFrame] = field(default_factory=dict)
    prices: dict[str, PriceState] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def merge_candles(self, market: str, candles: Iterable[Candle], now: datetime | None = None) -> list[datetime]:
        """닫힌 캔들을 합치고, 처음 보는 닫힌 캔들 시각(오름차순)을 돌려준다."""
        now = now or datetime.now(UTC)
        incoming = candles_to_dataframe(list(candles), interval=self.interval)
        incoming = drop_unclosed(incoming, self.interval, now)
        if incoming.empty:
            return []
        existing = self.candles.get(market)
        if existing is None or existing.empty:
            merged = incoming
            new_times = list(incoming.index)
        else:
            new_times = [t for t in incoming.index if t not in existing.index]
            merged = pd.concat([existing, incoming])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        if len(merged) > self.max_rows:
            merged = merged.iloc[-self.max_rows :]
        merged.attrs["market"] = market
        merged.attrs["interval"] = self.interval.value
        self.candles[market] = merged
        return [t.to_pydatetime() for t in sorted(new_times)]

    def frame(self, market: str) -> pd.DataFrame | None:
        return self.candles.get(market)

    def last_closed_time(self, market: str) -> datetime | None:
        df = self.candles.get(market)
        return df.index[-1].to_pydatetime() if df is not None and not df.empty else None

    # ------------------------------------------------------------------
    def price(self, market: str) -> PriceState | None:
        return self.prices.get(market)

    def _state(self, market: str) -> PriceState:
        state = self.prices.get(market)
        if state is None:
            state = PriceState(market, book_max_age_seconds=self.price_max_age_seconds)
            self.prices[market] = state
        return state

    def update_price(self, message: WsMessage) -> bool:
        """시세 메시지를 반영한다. 반영했으면 True."""
        if isinstance(message, WsTicker):
            state = self._state(message.code)
            state.last_price = message.trade_price
            state.last_time = message.received_at_utc
            return True
        if isinstance(message, WsTrade):
            state = self._state(message.code)
            state.last_price = message.trade_price
            state.last_time = message.trade_datetime_utc
            if message.best_bid_price and message.best_ask_price:
                state.best_bid = message.best_bid_price
                state.best_ask = message.best_ask_price
                state.book_time = message.received_at_utc
            return True
        if isinstance(message, WsOrderbook):
            state = self._state(message.code)
            if message.orderbook_units:
                state.best_bid = message.best_bid_price
                state.best_ask = message.best_ask_price
                state.book_time = message.received_at_utc
            return True
        return False

    def set_last_price(self, market: str, price: float, time: datetime) -> None:
        """REST 현재가로 보정할 때 사용. 보정 시각보다 오래된 호가는 ``mark_price`` 에서 자동으로 무시된다."""
        state = self._state(market)
        state.last_price = price
        state.last_time = time

    def mark_prices(self) -> dict[str, float]:
        return {m: s.mark_price for m, s in self.prices.items() if s.mark_price}
