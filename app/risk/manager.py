"""리스크 관리자 (RiskManager) — 백테스트와 모의매매·실거래가 같은 규칙을 쓴다.

세 가지 일을 한다.
1. 진입 심사 ``evaluate()``: 신호가 왔을 때 사도 되는지, 얼마나 살지 결정한다.
   순서: 긴급 정지 → 당일 잠금(일일 손실·연속 손실) → HOLD → 이미 보유 → 최대 포지션 수 → 재진입 대기 →
   시세 괴리 → 예산 계산(현금×비율, 거래당 상한, 자산 대비 비율 상한) → 최소 주문 금액.
2. 청산 감시 ``check_exits()``: 보유 포지션에 대해 손절 → 익절 → 추적 손절 순으로 본다.
   백테스트는 캔들 저가·고가를, 실시간은 현재 평가가를 ``low``/``high`` 로 넘긴다.
3. 결과 반영 ``record_trade()`` / ``update_equity()``: 연속 손실·당일 손익을 갱신하고 한도를 넘으면 그날을 잠근다.
   날짜는 KST 기준이며 날짜가 바뀌면 잠금·카운터가 초기화된다. 재시작 시 ``rebuild()`` 로 오늘 거래를 다시 반영한다.

모든 결정은 사유 문자열과 함께 돌려주므로 로그·DB 에 그대로 남긴다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.exchange.models import KST
from app.risk.base import RiskDecision
from app.risk.config import RiskConfig
from app.strategy.base import Action, Signal
from app.trading.market_state import PriceState
from app.trading.portfolio import Portfolio, Position, Trade

log = logging.getLogger(__name__)

EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT = "take_profit"
EXIT_TRAILING_STOP = "trailing_stop"


@dataclass(frozen=True)
class ExitCheck:
    market: str
    reason: str
    trigger_price: float  # 손절가/익절가 (체결가는 엔진이 시가·슬리피지를 반영해 정한다)


@dataclass
class RiskState:
    day: date | None = None
    day_start_equity: float | None = None
    last_equity: float | None = None
    daily_realized_pnl: float = 0.0
    consecutive_losses: int = 0
    lock_reason: str | None = None  # 당일 신규 진입 잠금 사유
    halted: bool = False
    halt_reason: str | None = None
    peak_prices: dict[str, float] = field(default_factory=dict)  # 추적 손절용 보유 중 최고가
    last_exit_at: dict[str, datetime] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat() if self.day else None,
            "day_start_equity": self.day_start_equity,
            "last_equity": self.last_equity,
            "daily_realized_pnl": self.daily_realized_pnl,
            "consecutive_losses": self.consecutive_losses,
            "lock_reason": self.lock_reason,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self.state = RiskState()

    # ------------------------------------------------------------------
    # 날짜·자산 추적
    # ------------------------------------------------------------------
    @staticmethod
    def trading_day(now: datetime) -> date:
        return now.astimezone(KST).date()

    def start_day_if_needed(self, now: datetime, equity: float | None) -> bool:
        """KST 날짜가 바뀌면 당일 카운터·잠금을 초기화한다. 초기화했으면 True."""
        today = self.trading_day(now)
        if self.state.day == today:
            return False
        if self.state.day is not None:
            log.info("리스크 일일 리셋 %s → %s (전일 실현손익 %.0f, 연속손실 %d)", self.state.day, today,
                     self.state.daily_realized_pnl, self.state.consecutive_losses)
        self.state.day = today
        self.state.day_start_equity = equity
        self.state.daily_realized_pnl = 0.0
        self.state.consecutive_losses = 0
        self.state.lock_reason = None
        return True

    def update_equity(self, equity: float, now: datetime) -> str | None:
        """현재 평가 자산을 반영한다. 일일 손실 한도를 넘으면 잠그고 사유를 돌려준다."""
        self.start_day_if_needed(now, equity)
        if self.state.day_start_equity is None:
            self.state.day_start_equity = equity
        self.state.last_equity = equity
        limit = self.config.daily_loss_limit_pct
        if limit is not None and self.state.day_start_equity and self.state.lock_reason is None:
            drawdown = 1 - equity / self.state.day_start_equity
            if drawdown >= limit:
                self.state.lock_reason = (
                    f"일일 손실 한도 초과: 당일 시작 {self.state.day_start_equity:,.0f} → 현재 {equity:,.0f} "
                    f"({drawdown * 100:.2f}% ≥ {limit * 100:g}%)"
                )
                log.warning("리스크 잠금: %s", self.state.lock_reason)
                return self.state.lock_reason
        return None

    def record_trade(self, trade: Trade, now: datetime | None = None) -> str | None:
        """왕복 거래 결과를 반영한다. 연속 손실 한도를 넘으면 잠그고 사유를 돌려준다."""
        now = now or trade.exit_time
        self.start_day_if_needed(now, self.state.last_equity)
        self.state.daily_realized_pnl += trade.pnl
        self.state.consecutive_losses = self.state.consecutive_losses + 1 if trade.pnl <= 0 else 0
        self.state.peak_prices.pop(trade.market, None)
        self.state.last_exit_at[trade.market] = trade.exit_time
        limit = self.config.max_consecutive_losses
        if limit is not None and self.state.consecutive_losses >= limit and self.state.lock_reason is None:
            self.state.lock_reason = f"연속 손실 {self.state.consecutive_losses}회 ≥ 한도 {limit}회"
            log.warning("리스크 잠금: %s", self.state.lock_reason)
            return self.state.lock_reason
        return None

    def rebuild(self, trades: Iterable[Trade], now: datetime, equity: float | None) -> None:
        """재시작 시 오늘(KST) 청산된 거래를 다시 반영한다."""
        self.state = RiskState()
        self.start_day_if_needed(now, equity)
        today = self.trading_day(now)
        for trade in sorted(trades, key=lambda t: t.exit_time):
            if self.trading_day(trade.exit_time) == today:
                self.record_trade(trade, trade.exit_time)
        if equity is not None:
            self.state.last_equity = equity

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        log.error("리스크 긴급 정지: %s", reason)

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = None

    @property
    def entries_blocked_reason(self) -> str | None:
        if self.state.halted:
            return f"긴급 정지: {self.state.halt_reason}"
        return self.state.lock_reason

    # ------------------------------------------------------------------
    # 진입 심사
    # ------------------------------------------------------------------
    def evaluate(
        self,
        signal: Signal,
        portfolio: Portfolio,
        price: PriceState | None,
        now: datetime,
        equity: float | None = None,
    ) -> RiskDecision:
        cfg = self.config
        self.start_day_if_needed(now, equity if equity is not None else self.state.last_equity)
        if signal.action is Action.SELL:
            if not portfolio.has_position(signal.market):
                return RiskDecision(False, "보유 포지션 없음")
            return RiskDecision(True, "매도 승인", quantity=None)
        if signal.action is not Action.BUY:
            return RiskDecision(False, "HOLD 신호")

        blocked = self.entries_blocked_reason
        if blocked:
            return RiskDecision(False, f"신규 진입 차단: {blocked}")
        if portfolio.has_position(signal.market):
            return RiskDecision(False, "이미 보유 중 (추가 매수 없음)")
        if len(portfolio.positions) >= cfg.max_open_positions:
            return RiskDecision(False, f"최대 포지션 수 {cfg.max_open_positions}개 도달")
        last_exit = self.state.last_exit_at.get(signal.market)
        if cfg.cooldown_seconds and last_exit is not None:
            elapsed = (now - last_exit).total_seconds()
            if elapsed < cfg.cooldown_seconds:
                return RiskDecision(False, f"재진입 대기 중 ({elapsed:.0f}/{cfg.cooldown_seconds:g}초)")
        has_mark = price is not None and bool(price.mark_price) and bool(signal.price)
        if cfg.price_deviation_limit is not None and has_mark:
            deviation = abs(price.mark_price / signal.price - 1)
            if deviation > cfg.price_deviation_limit:
                return RiskDecision(
                    False,
                    f"시세 괴리 {deviation * 100:.1f}% > 한도 {cfg.price_deviation_limit * 100:g}% "
                    f"(신호 종가 {signal.price:,.0f}, 현재 {price.mark_price:,.0f})",
                )

        budget = portfolio.cash * cfg.position_fraction
        if cfg.max_order_amount is not None:
            budget = min(budget, cfg.max_order_amount)
        if cfg.max_position_ratio < 1.0:
            total = equity if equity is not None else self.state.last_equity
            if total is None:
                total = portfolio.cash
            invested = total - portfolio.cash
            room = cfg.max_position_ratio * total - invested
            if room <= 0:
                return RiskDecision(False, f"자산 대비 포지션 상한 {cfg.max_position_ratio * 100:g}% 도달")
            budget = min(budget, room)
        if budget < cfg.min_order_amount:
            return RiskDecision(
                False, f"매수 예산 {budget:,.0f} KRW 가 최소 주문 금액 {cfg.min_order_amount:,.0f} 미만"
            )
        return RiskDecision(True, f"진입 승인 (예산 {budget:,.0f} KRW)", amount=budget)

    # ------------------------------------------------------------------
    # 청산 감시
    # ------------------------------------------------------------------
    def check_exits(self, position: Position, *, low: float, high: float, now: datetime) -> ExitCheck | None:
        """손절 → 익절 → 추적 손절 순서. 같은 구간에서 둘 다 닿으면 손절을 먼저 본다(보수적)."""
        cfg = self.config
        market = position.market
        if cfg.stop_loss_pct is not None:
            stop = position.avg_price * (1 - cfg.stop_loss_pct)
            if low <= stop:
                return ExitCheck(market, EXIT_STOP_LOSS, stop)
        if cfg.take_profit_pct is not None:
            target = position.avg_price * (1 + cfg.take_profit_pct)
            if high >= target:
                return ExitCheck(market, EXIT_TAKE_PROFIT, target)
        if cfg.trailing_stop_pct is not None:
            peak = max(self.state.peak_prices.get(market, position.avg_price), high)
            self.state.peak_prices[market] = peak
            trail = peak * (1 - cfg.trailing_stop_pct)
            if low <= trail and peak > position.avg_price:
                return ExitCheck(market, EXIT_TRAILING_STOP, trail)
        return None

    def snapshot(self) -> dict[str, Any]:
        return {"config": self.config.model_dump(), "state": self.state.to_dict()}
