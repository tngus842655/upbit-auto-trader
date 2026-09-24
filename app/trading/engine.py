"""매매 엔진 (Phase 5: Paper Trading).

흐름:
    REST 캔들(닫힌 것만) ─▶ MarketState ─▶ Strategy.generate_signal ─▶ RiskManager ─▶ Broker ─▶ Repository(DB)
    WebSocket ticker/orderbook ─▶ MarketState.prices (가상 체결가·평가액)

- 캔들 경계(예: 매시 정각 + grace 초)마다 REST 로 최근 캔들을 다시 받아 **닫힌 캔들만** 확정한다.
  WebSocket 캔들 스트림은 체결이 있을 때만 오고 재연결 중 빠질 수 있어 확정 기준으로 쓰지 않는다.
- 같은 캔들에 대한 신호는 DB 유니크 제약과 브로커의 client_id 로 한 번만 처리된다(재시작 후에도).
- 시작 시 DB 에서 계좌 현금·포지션·처리 이력을 복구한다.
- 브로커는 PaperBroker(가상) 또는 LiveBroker(실제)다. LIVE 는 settings 의 이중 플래그가 모두 켜지고 브로커가
  LiveBroker 일 때만 허용된다. 이 파일은 실제 주문 API 를 직접 호출하지 않는다 (LiveBroker 가 담당).
- 대시보드 연동 이음새(Phase 8): ``engine_status`` 하트비트 테이블에 상태를 주기적으로 쓰고, ``bot_commands`` 큐를
  폴링해 pause / resume / stop / halt / resume_risk 를 처리한다. 엔진이 매매의 유일한 주체다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.config.settings import Settings, TradingMode
from app.core.exceptions import ConfigError, TraderError
from app.database.repository import Repository
from app.exchange.models import KST, CandleInterval
from app.exchange.upbit_client import UpbitClient
from app.exchange.websocket import Subscription, UpbitWebSocket
from app.notify.base import EventKind, NotificationEvent
from app.notify.manager import NotificationManager
from app.risk.base import RiskPolicy
from app.risk.manager import RiskManager
from app.strategy.base import Signal, Strategy
from app.trading.live_broker import LiveBroker
from app.trading.market_state import MarketState
from app.trading.orders import Order, OrderRequest, OrderStatus, PaperBroker
from app.trading.portfolio import Portfolio, Side, Trade
from app.trading.runtime_settings import RuntimeSettings

_EXIT_KINDS = (
    ("stop_loss", EventKind.STOP_LOSS), ("take_profit", EventKind.TAKE_PROFIT), ("trailing", EventKind.TRAILING_STOP)
)


def _exit_kind(reason: str) -> EventKind:
    """청산 사유 문자열 → 알림 종류 (손절/익절/추적 손절, 그 외는 매도)."""
    for prefix, kind in _EXIT_KINDS:
        if reason.startswith(prefix):
            return kind
    return EventKind.SELL

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


def next_boundary(now: datetime, interval_seconds: int, grace_seconds: float) -> datetime:
    """다음 캔들 경계 + grace. 캔들은 UTC 기준 간격으로 정렬된다."""
    epoch = int(now.timestamp())
    boundary = (epoch // interval_seconds + 1) * interval_seconds
    return datetime.fromtimestamp(boundary, tz=UTC) + timedelta(seconds=grace_seconds)


@dataclass
class EngineStats:
    started_at: datetime | None = None
    candle_checks: int = 0
    closed_candles: int = 0
    signals: int = 0
    actionable_signals: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    orders_unknown: int = 0  # 거래소 생성 여부 미확인 (운영자 확인 필요)
    orders_resolved: int = 0  # 미확정 주문을 후속 조회로 확정한 수
    price_stream_restarts: int = 0  # 시세 스트림 재연결 횟수
    stale_signals_skipped: int = 0  # 정체 뒤 실행하지 않고 기록만 한 오래된 신호
    risk_rejections: int = 0
    exits_triggered: int = 0
    risk_locks: int = 0
    commands_processed: int = 0
    heartbeats: int = 0
    snapshots: int = 0
    price_updates: int = 0
    errors: int = 0
    last_candle_check: datetime | None = None
    last_equity: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        strategy: Strategy,
        portfolio: Portfolio,
        broker: PaperBroker | LiveBroker,
        risk: RiskPolicy,
        repo: Repository,
        client: UpbitClient,
        markets: Sequence[str] | None = None,
        interval: CandleInterval | str | None = None,
        ws_factory: Callable[[list[Subscription]], UpbitWebSocket] | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        sleep: Sleeper = asyncio.sleep,
        warmup_candles: int | None = None,
        refresh_candles: int = 5,
        runtime: RuntimeSettings | None = None,
        settings_version: int = 0,
        notifier: NotificationManager | None = None,
    ) -> None:
        if settings.trading_mode is TradingMode.PAPER:
            if not isinstance(broker, PaperBroker):
                raise ConfigError("PAPER 모드에서는 PaperBroker 만 사용할 수 있습니다")
        elif settings.trading_mode is TradingMode.LIVE:
            if not settings.is_live_trading_allowed:
                raise ConfigError("LIVE 모드는 TRADING_MODE=LIVE 와 LIVE_TRADING_ENABLED=true 가 모두 필요합니다")
            if not isinstance(broker, LiveBroker):
                raise ConfigError("LIVE 모드에서는 LiveBroker 가 필요합니다")
            if not settings.has_api_keys:
                raise ConfigError("LIVE 모드에는 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 가 필요합니다")
        else:
            raise ConfigError(
                f"TradingEngine 은 PAPER/LIVE 전용입니다 (현재 {settings.trading_mode.value}). "
                "BACKTEST 는 backtest 명령을 쓰세요."
            )
        if repo.mode != broker.mode:
            raise ConfigError(f"저장소 모드({repo.mode})와 브로커 모드({broker.mode})가 다릅니다")
        self.mode = broker.mode
        self.settings = settings
        self.strategy = strategy
        self.portfolio = portfolio
        self.broker = broker
        self.risk = risk
        self.repo = repo
        self.client = client
        self.markets = list(markets or settings.markets)
        self.interval = CandleInterval.parse(interval or settings.candle_interval)
        self.state = MarketState(self.interval, price_max_age_seconds=settings.price_max_age_seconds)
        self.ws_factory = ws_factory
        self.clock = clock
        self.sleep = sleep
        self.warmup_candles = max(warmup_candles or settings.warmup_candles, strategy.warmup_periods + 5)
        self.refresh_candles = refresh_candles
        self.notifier = notifier
        self._stop_error: BaseException | None = None
        self.stats = EngineStats()
        self.status = "created"
        self._stop = asyncio.Event()
        self._ws: UpbitWebSocket | None = None
        self._round_trips_saved = 0
        self._last_exit_check: datetime | None = None
        self.exit_check_interval = 1.0  # 초
        self.paused = False
        self.command_poll_interval = 2.0
        # 미확정(UNKNOWN) 주문: 마켓별로 하나만 두고, 확정될 때까지 그 마켓의 신규 주문·청산을 막는다 (감사 HIGH-1)
        self.pending_orders: dict[str, Order] = {}
        self.pending_max_age = 600.0  # 이 시간 안에 확정 못 하면 거래소 잔고 동기화로 정리
        # 청산 주문 실패 시 마켓별 백오프 (1초마다 재시도해 주문·로그·알림이 폭주하지 않게)
        self._exit_backoff: dict[str, datetime] = {}
        self._exit_failures: dict[str, int] = {}
        self.exit_retry_base = 5.0
        self.exit_retry_max = 300.0
        # 시세 루프: 스트림이 끊기거나 예외가 나도 백오프 후 다시 붙는다 (감사 HIGH-2)
        self.price_loop_backoff = 1.0
        self.price_loop_max_backoff = 60.0
        self.price_stale_alert_seconds = 120.0  # 포지션이 있는데 이만큼 시세가 없으면 알림
        self._price_stale_alerted = False
        self.heartbeat_interval = 10.0
        self._last_command_poll: datetime | None = None
        self._reload_requested = False
        self._last_heartbeat: datetime | None = None
        self.last_trade_at: datetime | None = None
        self.runtime = runtime
        self.settings_version = settings_version
        self.restart_required = False

    # ------------------------------------------------------------------
    # 준비
    # ------------------------------------------------------------------
    async def warmup(self) -> None:
        """전략 워밍업용 과거 캔들을 받아 두고, DB 에도 저장한다."""
        now = self.clock()
        for market in self.markets:
            # +1: 응답에는 진행 중인 캔들이 하나 섞여 있으므로 닫힌 캔들 warmup_candles 개를 확보한다
            candles = await self._fetch_candles(market, self.warmup_candles + 1)
            new_times = self.state.merge_candles(market, candles, now)
            df = self.state.frame(market)
            if df is not None and not df.empty:
                self.repo.save_candles(market, self.interval.value, df)
            log.info("워밍업 %s %s: 닫힌 캔들 %d개 (마지막 %s)", market, self.interval.value, len(new_times),
                     self.state.last_closed_time(market))
        self._round_trips_saved = len(self.portfolio.trades)

    def rebuild_risk_state(self, now: datetime, equity: float | None) -> None:
        """재시작 시 오늘(KST) 청산 거래로 연속 손실·일일 손익을 복구한다."""
        if not isinstance(self.risk, RiskManager):
            return
        day_start = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        trades = self.repo.load_round_trips(since=day_start)
        self.risk.rebuild(trades, now, equity)
        if self.risk.entries_blocked_reason:
            log.warning("리스크 상태 복구: 신규 진입 차단 중 — %s", self.risk.entries_blocked_reason)
            self.repo.log("WARNING", "risk_lock_restored", self.risk.entries_blocked_reason,
                          self.risk.snapshot()["state"])
            self._notify(EventKind.RISK_HALT, "재시작 후 신규 진입 차단 상태", self.risk.entries_blocked_reason)
        log.info("리스크 상태 복구: 오늘 거래 %d건, 연속손실 %d, 실현손익 %.0f", len(trades),
                 self.risk.state.consecutive_losses, self.risk.state.daily_realized_pnl)

    async def _fetch_candles(self, market: str, count: int) -> list:
        remaining = count
        collected: list = []
        to = None
        while remaining > 0:
            requested = min(200, remaining)
            batch = await self.client.get_candles(market, self.interval, count=requested, to=to)
            if not batch:
                break
            collected.extend(batch)
            remaining -= len(batch)
            if len(batch) < requested:
                break
            to = min(c.candle_date_time_utc for c in batch)
        return collected

    # ------------------------------------------------------------------
    # 캔들 처리
    # ------------------------------------------------------------------
    async def process_closed_candles(self, now: datetime | None = None) -> list[Signal]:
        """최근 캔들을 다시 받아 새로 닫힌 캔들마다 전략을 돌린다. 만든 신호 목록을 돌려준다.

        정체(장애·정지) 뒤 여러 캔들이 한꺼번에 닫혔으면 **마지막 캔들의 신호만 실행**하고 이전 캔들 신호는
        기록만 남긴다. 마지막 캔들도 현재보다 한 인터벌 넘게 오래됐으면 실행하지 않는다.
        캔들 공백이 보이면 다시 받아 메운다. (감사 HIGH-4)
        """
        now = now or self.clock()
        self.stats.candle_checks += 1
        self.stats.last_candle_check = now
        produced: list[Signal] = []
        for market in self.markets:
            try:
                candles = await self.client.get_candles(market, self.interval, count=self.refresh_candles)
            except TraderError as exc:
                self.stats.errors += 1
                log.error("%s 캔들 조회 실패: %s", market, exc)
                self.repo.log("ERROR", "candle_fetch_failed", f"{market}: {exc}")
                self._notify(EventKind.API_ERROR, f"캔들 조회 실패 {market}", str(exc), key=f"candle:{market}")
                continue
            new_times = self.state.merge_candles(market, candles, now)
            if not new_times:
                continue
            gaps = self.state.find_gaps(market)
            if gaps:
                new_times = await self._refill_gaps(market, gaps, new_times, now)
            df = self.state.frame(market)
            assert df is not None
            self.repo.save_candles(market, self.interval.value, df.loc[df.index.isin(new_times)])
            last_time = new_times[-1]
            for closed_time in new_times:
                self.stats.closed_candles += 1
                window = df.loc[:closed_time]
                signal = self.strategy.generate_signal(window, market=market)
                produced.append(signal)
                if closed_time != last_time:
                    self._skip_stale_signal(signal, "정체 뒤 한꺼번에 닫힌 캔들 (마지막 캔들 신호만 실행)")
                    continue
                closed_at = closed_time + timedelta(seconds=self.interval.seconds)
                late = (now - closed_at).total_seconds()
                if late > self.interval.seconds + self.settings.candle_grace_seconds:
                    self._skip_stale_signal(signal, f"신호 캔들이 닫힌 지 {late:.0f}초 지남 (한 인터벌 초과)")
                    continue
                await self._on_signal(signal, now)
        return produced

    def _skip_stale_signal(self, signal: Signal, why: str) -> None:
        """오래된 신호는 처리된 것으로 기록만 하고(재시작 후에도 실행되지 않게) 주문하지 않는다."""
        self.stats.signals += 1
        self.stats.stale_signals_skipped += 1
        self.repo.save_signal(signal, self.interval.value)
        log.warning("오래된 신호 건너뜀 %s %s [%s]: %s", signal.market, signal.time.isoformat(),
                    signal.action.value, why)
        if signal.is_actionable:
            self.repo.log("WARNING", "stale_signal_skipped",
                          f"{signal.market} {signal.action.value} @ {signal.time.isoformat()}: {why}", signal.to_dict())

    async def _refill_gaps(
        self, market: str, gaps: list[tuple[datetime, datetime]], new_times: list[datetime], now: datetime
    ) -> list[datetime]:
        """캔들 공백을 발견하면 공백 시작부터 지금까지를 덮을 만큼 다시 받아 합친다.

        새로 닫힌 캔들 목록을 갱신해 돌려준다.
        """
        first_gap = gaps[0]
        span = int((now - first_gap[0]).total_seconds() // self.interval.seconds) + 2
        count = min(self.state.max_rows, max(self.warmup_candles + 1, span))
        log.warning("%s 캔들 공백 %d곳 (%s → %s) → %d개 다시 조회", market, len(gaps),
                    first_gap[0].isoformat(), first_gap[1].isoformat(), count)
        self.repo.log("WARNING", "candle_gap",
                      f"{market}: 공백 {len(gaps)}곳 ({first_gap[0].isoformat()} → {first_gap[1].isoformat()}), "
                      f"{count}개 재조회")
        try:
            candles = await self._fetch_candles(market, count)
        except TraderError as exc:
            self.stats.errors += 1
            log.error("%s 공백 메우기 실패: %s", market, exc)
            return new_times
        more = self.state.merge_candles(market, candles, now)
        return sorted(set(new_times) | set(more))

    async def _on_signal(self, signal: Signal, now: datetime) -> None:
        self.stats.signals += 1
        saved = self.repo.save_signal(signal, self.interval.value)
        if not saved:
            # 재시작 후 같은 캔들을 다시 본 경우: 이미 처리한 신호이므로 주문까지 가지 않는다.
            log.info("신호 %s %s 는 이미 처리됨 → 건너뜀", signal.market, signal.time.isoformat())
            return
        log.info(
            "신호 %s %s [%s] %s 종가 %.0f",
            signal.market, signal.time.astimezone(UTC).strftime("%m-%d %H:%M"), signal.action.value,
            signal.reason, signal.price,
        )
        if not signal.is_actionable:
            return
        self.stats.actionable_signals += 1
        if self.paused and signal.action.value == "BUY":
            log.info("일시정지 중 → 매수 신호 무시 %s", signal.market)
            self.repo.log("INFO", "paused_skip", f"{signal.market} BUY 신호 무시 (일시정지)")
            return
        if signal.market in self.pending_orders:
            log.warning("%s 미확정 주문이 있어 신호 %s 건너뜀", signal.market, signal.action.value)
            self.repo.log("WARNING", "pending_order_skip",
                          f"{signal.market} {signal.action.value}: 미확정 주문 대기 중")
            return
        price = self.state.price(signal.market)
        decision = self.risk.evaluate(signal, self.portfolio, price, now, equity=self.current_equity())
        if not decision.approved:
            self.stats.risk_rejections += 1
            log.info("리스크 거부 %s %s: %s", signal.market, signal.action.value, decision.reason)
            self.repo.log("INFO", "risk_rejected", f"{signal.market} {signal.action.value}: {decision.reason}",
                          {"signal": signal.to_dict()})
            return
        side = Side.BUY if signal.action.value == "BUY" else Side.SELL
        request = OrderRequest(
            market=signal.market, side=side, amount=decision.amount, quantity=decision.quantity,
            reason=signal.reason, strategy=signal.strategy, signal_time=signal.time,
        )
        if side is Side.BUY:
            self._notify(EventKind.BUY, signal.market,
                         f"시장가 매수 {decision.amount or 0:,.0f}원 · 사유: {signal.reason}",
                         {"signal": signal.to_dict(), "amount": decision.amount})
        else:
            self._notify(EventKind.SELL, signal.market,
                         f"시장가 매도 {decision.quantity or 0:.8f}개 · 사유: {signal.reason}",
                         {"signal": signal.to_dict(), "quantity": decision.quantity})
        order = await self.broker.execute(request, price, now)
        self._record_order(order)

    def _notify(self, kind: EventKind, title: str, message: str = "", data: dict[str, Any] | None = None, *,
                key: str | None = None) -> None:
        """알림 이벤트를 큐에 넣는다. 알림 관리자가 없으면 아무 일도 하지 않는다 (매매를 막지 않음)."""
        if self.notifier is None:
            return
        self.notifier.emit(NotificationEvent(kind=kind, title=title, message=message, mode=self.mode,
                                             time=self.clock(), data=dict(data or {}), key=key))

    def _fill_summary(self, order: Order, trades: Sequence[Trade]) -> str:
        qty, price = order.filled_quantity or 0.0, order.fill_price or 0.0
        text = f"{qty:.8f}개 @ {price:,.0f} · 금액 {qty * price:,.0f}원 · 수수료 {order.fee:,.0f}원"
        for trade in trades:
            pct = f" ({trade.pnl / trade.entry_amount * 100:+.2f}%)" if trade.entry_amount else ""
            text += f" · 손익 {trade.pnl:+,.0f}원{pct} [{trade.exit_reason}]"
        return text + f" · 현금 {self.portfolio.cash:,.0f}원"

    def _stop_reason(self) -> str:
        exc = self._stop_error
        if exc is None:
            return "정상 종료"
        if isinstance(exc, KeyboardInterrupt):
            return "사용자 중단 (Ctrl+C)"
        if isinstance(exc, asyncio.CancelledError):
            return "작업 취소"
        return f"비정상 종료: {type(exc).__name__}: {exc}"[:300]

    def _stop_summary(self) -> str:
        realized = sum(t.pnl for t in self.portfolio.trades)
        return (
            f"체결 {self.stats.orders_filled}건 · 오류 {self.stats.errors}건 · 현금 {self.portfolio.cash:,.0f}원"
            f" · 실현손익 {realized:+,.0f}원 · 수수료 {self.portfolio.fees_paid:,.0f}원"
        )

    def _record_order(self, order: Order) -> None:
        if order.error and order.error.startswith("중복 주문"):
            self.stats.orders_rejected += 1
            log.warning("중복 주문 차단 %s %s (%s)", order.market, order.side.value, order.client_id)
            self.repo.log("WARNING", "duplicate_order_blocked", f"{order.market} {order.side.value}", order.to_dict())
            return
        self.repo.save_order(order)
        if order.is_filled:
            self.stats.orders_filled += 1
            self.last_trade_at = order.filled_at or self.clock()
            for fill in order.fills:
                self.repo.save_fill(fill, order_id=order.id, strategy=order.strategy)
            new_trades = self.portfolio.trades[self._round_trips_saved :]
            for trade in new_trades:
                self.repo.save_round_trip(trade)
            self._round_trips_saved = len(self.portfolio.trades)
            self.repo.sync_portfolio(self.portfolio)
            if order.side is Side.SELL and isinstance(self.risk, RiskManager):
                for trade in self.portfolio.trades[-1:]:
                    lock = self.risk.record_trade(trade, order.filled_at or self.clock())
                    if lock:
                        self.stats.risk_locks += 1
                        self.repo.log("WARNING", "risk_lock", lock, self.risk.snapshot()["state"])
                        self._notify(EventKind.CONSECUTIVE_LOSS_LIMIT, "당일 신규 진입 잠금", lock,
                                     self.risk.snapshot()["state"])
            log.info(
                "가상 체결 %s %s %.8f @ %.0f (수수료 %.0f) 현금 %.0f",
                order.market, order.side.value, order.filled_quantity or 0, order.fill_price or 0, order.fee,
                self.portfolio.cash,
            )
            self.repo.log("INFO", "order_filled", f"{order.market} {order.side.value} @ {order.fill_price:.0f}",
                          order.to_dict())
            self._notify(EventKind.ORDER_FILLED, f"{order.market} {'매수' if order.side is Side.BUY else '매도'}",
                         self._fill_summary(order, new_trades), {"order": order.to_dict()})
            self.snapshot(order.filled_at)
        elif order.status is OrderStatus.UNKNOWN:
            # 거래소에 주문이 있을 수 있는데 확인이 안 된 상태. 계좌는 건드리지 않고 기록·알림만 남긴다.
            self.stats.orders_unknown += 1
            self.pending_orders[order.market] = order
            log.error("주문 상태 미확인 %s %s: %s", order.market, order.side.value, order.error)
            self.repo.log("ERROR", "order_unknown", f"{order.market} {order.side.value}: {order.error}",
                          order.to_dict())
            side_label = '매수' if order.side is Side.BUY else '매도'
            self._notify(EventKind.ORDER_REJECTED, f"{order.market} {side_label} 상태 미확인 (운영자 확인 필요)",
                         str(order.error or ""), {"order": order.to_dict()})
        else:
            self.stats.orders_rejected += 1
            log.warning("주문 거부 %s %s: %s", order.market, order.side.value, order.error)
            self.repo.log("WARNING", "order_rejected", f"{order.market} {order.side.value}: {order.error}",
                          order.to_dict())
            self._notify(EventKind.ORDER_REJECTED, f"{order.market} {'매수' if order.side is Side.BUY else '매도'}",
                         str(order.error or "사유 없음"), {"order": order.to_dict()})

    def _note_exit_result(self, market: str, order: Order, now: datetime) -> None:
        """청산 주문 결과에 따라 마켓별 재시도 백오프를 갱신한다 (성공이면 초기화)."""
        if order.is_filled:
            self._exit_backoff.pop(market, None)
            self._exit_failures.pop(market, None)
            return
        failures = self._exit_failures.get(market, 0) + 1
        self._exit_failures[market] = failures
        delay = min(self.exit_retry_base * (2 ** (failures - 1)), self.exit_retry_max)
        self._exit_backoff[market] = now + timedelta(seconds=delay)
        log.warning("%s 청산 주문 실패 %d회 → %.0f초 뒤 재시도", market, failures, delay)

    # ------------------------------------------------------------------
    # 미확정 주문 후속 확정
    # ------------------------------------------------------------------
    def load_pending_orders(self) -> int:
        """DB 의 UNKNOWN 주문을 미확정 목록으로 복구한다 (재시작 대비)."""
        count = 0
        for order in self.repo.load_unknown_orders():
            self.pending_orders.setdefault(order.market, order)
            count += 1
        if count:
            log.warning("미확정 주문 %d건 복구 → 거래소 조회로 확정 예정", count)
        return count

    async def resolve_pending_orders(self, now: datetime | None = None) -> int:
        """미확정(UNKNOWN) 주문을 거래소에서 다시 조회해 확정한다. 확정된 건수를 돌려준다."""
        if not self.pending_orders:
            return 0
        now = now or self.clock()
        resolver = getattr(self.broker, "resolve_order", None)
        resolved = 0
        for market, order in list(self.pending_orders.items()):
            final: Order | None = None
            if resolver is not None:
                try:
                    final = await resolver(order, now)
                except Exception as exc:  # noqa: BLE001 - 확정 실패가 엔진을 멈추지 않게
                    self.stats.errors += 1
                    log.error("미확정 주문 %s 확정 실패: %s", order.id, exc)
            if final is not None:
                del self.pending_orders[market]
                self.stats.orders_resolved += 1
                resolved += 1
                self.repo.log("INFO", "order_resolved", f"{market} {order.side.value} → {final.status.value}",
                              final.to_dict())
                self._record_order(final)
                continue
            if (now - order.created_at).total_seconds() >= self.pending_max_age:
                await self._give_up_pending(market, order)
        return resolved

    async def _give_up_pending(self, market: str, order: Order) -> None:
        """확정 시한을 넘긴 미확정 주문: 기록·알림을 남기고 거래소 잔고 기준으로 계좌를 맞춘다."""
        del self.pending_orders[market]
        order.error = f"{order.error or ''} | {self.pending_max_age:.0f}초 안에 확정 실패 → 거래소 잔고 동기화로 정리"
        self.repo.save_order(order)
        self.repo.log("ERROR", "order_unresolved", f"{market} {order.side.value}: 확정 실패, 잔고 동기화",
                      order.to_dict())
        self._notify(EventKind.API_ERROR, f"{market} 주문 확정 실패",
                     "거래소 잔고 기준으로 계좌를 맞춥니다. 거래소에서 주문 내역을 확인하세요",
                     key=f"unresolved:{market}")
        if isinstance(self.broker, LiveBroker):
            try:
                diff = await self.broker.reconcile(self.markets)
                self.repo.sync_portfolio(self.portfolio)
                self.repo.log("INFO", "reconcile", "미확정 주문 정리 후 잔고 동기화", diff)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                log.error("잔고 동기화 실패: %s", exc)

    # ------------------------------------------------------------------
    # 리스크: 청산 감시
    # ------------------------------------------------------------------
    def current_equity(self) -> float | None:
        prices = self.state.mark_prices()
        if any(m not in prices for m in self.portfolio.positions):
            return None
        return self.portfolio.equity(prices)

    async def check_exits(self, now: datetime | None = None, *, force: bool = False) -> list[Order]:
        """보유 포지션의 손절·익절·추적 손절을 현재 시세로 판정하고 청산 주문을 낸다 (1초에 한 번)."""
        now = now or self.clock()
        if not isinstance(self.risk, RiskManager):
            return []
        recently = self._last_exit_check and (now - self._last_exit_check).total_seconds() < self.exit_check_interval
        if not force and recently:
            return []
        self._last_exit_check = now
        equity = self.current_equity()
        if equity is not None:
            lock = self.risk.update_equity(equity, now)
            if lock:
                self.stats.risk_locks += 1
                self.repo.log("WARNING", "risk_lock", lock, self.risk.snapshot()["state"])
                self._notify(EventKind.DAILY_LOSS_LIMIT, "당일 신규 진입 잠금", lock, self.risk.snapshot()["state"])
        orders: list[Order] = []
        for market, pos in list(self.portfolio.positions.items()):
            if market in self.pending_orders:
                continue  # 미확정 주문 확정 전에는 같은 마켓에 청산 주문을 겹쳐 내지 않는다
            retry_at = self._exit_backoff.get(market)
            if retry_at is not None and now < retry_at:
                continue
            price = self.state.price(market)
            if price is None or not price.is_fresh(now, self.settings.price_max_age_seconds) or not price.mark_price:
                continue
            exit_check = self.risk.check_exits(pos, low=price.mark_price, high=price.mark_price, now=now)
            if exit_check is None:
                continue
            self.stats.exits_triggered += 1
            log.info("청산 조건 %s %s: 기준가 %.0f, 현재 %.0f", market, exit_check.reason, exit_check.trigger_price,
                     price.mark_price)
            change = (price.mark_price / pos.avg_price - 1) * 100 if pos.avg_price else 0.0
            self._notify(
                _exit_kind(exit_check.reason), market,
                f"기준가 {exit_check.trigger_price:,.0f} · 현재 {price.mark_price:,.0f} ({change:+.2f}%)"
                f" · {exit_check.reason}",
                {"reason": exit_check.reason, "trigger_price": exit_check.trigger_price, "price": price.mark_price},
            )
            request = OrderRequest(
                market=market, side=Side.SELL, reason=exit_check.reason, strategy=self.strategy.name,
                signal_time=now, client_id=f"{self.broker.mode}:{market}:SELL:{exit_check.reason}:{now.isoformat()}",
            )
            order = await self.broker.execute(request, price, now)
            self._record_order(order)
            self._note_exit_result(market, order, now)
            orders.append(order)
        return orders

    # ------------------------------------------------------------------
    # 평가·스냅샷
    # ------------------------------------------------------------------
    async def ensure_prices(self, now: datetime | None = None) -> dict[str, float]:
        """보유 마켓의 평가 가격을 확보한다. WebSocket 시세가 오래됐으면 REST 현재가로 보정."""
        now = now or self.clock()
        needed = set(self.portfolio.positions) | set(self.markets)
        stale = [
            m for m in needed
            if (s := self.state.price(m)) is None or not s.is_fresh(now, self.settings.price_max_age_seconds)
        ]
        if stale:
            try:
                for ticker in await self.client.get_tickers(sorted(stale)):
                    self.state.set_last_price(ticker.market, ticker.trade_price, now)
            except TraderError as exc:
                self.stats.errors += 1
                log.error("현재가 보정 실패: %s", exc)
        return self.state.mark_prices()

    def snapshot(self, time: datetime | None = None) -> float | None:
        prices = self.state.mark_prices()
        missing = [m for m in self.portfolio.positions if m not in prices]
        if missing:
            log.warning("평가 가격 없음: %s (스냅샷 건너뜀)", missing)
            return None
        equity = self.repo.snapshot_balance(self.portfolio, prices, time or self.clock())
        self.stats.snapshots += 1
        self.stats.last_equity = equity
        return equity

    # ------------------------------------------------------------------
    # 실행 설정 핫리로드 (대시보드가 저장한 새 버전을 다음 캔들부터 반영)
    # ------------------------------------------------------------------
    async def maybe_reload_settings(self) -> dict[str, list[str]] | None:
        if self.runtime is None:
            return None
        latest = self.repo.runtime_settings_version()
        if latest <= self.settings_version:
            return None
        loaded = self.repo.load_runtime_settings()
        if loaded is None:
            return None
        data, version = loaded
        try:
            new = RuntimeSettings(**data)
        except ValidationError as exc:
            self.settings_version = version  # 잘못된 버전은 건너뛰고 다음 저장을 기다린다
            log.error("설정 v%d 무시(검증 실패): %s", version, exc)
            self.repo.log("ERROR", "settings_invalid", f"v{version} 검증 실패: {exc}")
            self._notify(EventKind.SETTINGS, f"설정 v{version} 검증 실패", str(exc)[:500], key=f"settings:{version}")
            return None
        changes = self.runtime.changes_vs(new)
        if "strategy_name" in changes["hot"] or "strategy_params" in changes["hot"]:
            self.strategy = new.build_strategy()
            self.warmup_candles = max(self.warmup_candles, self.strategy.warmup_periods + 5)
            short = [
                m for m in self.markets
                if (df := self.state.frame(m)) is None or len(df) < self.strategy.warmup_periods + 1
            ]
            if short:
                await self.warmup()
        if "risk" in changes["hot"] and isinstance(self.risk, RiskManager):
            self.risk.config = new.risk
        if changes["restart"]:
            self.restart_required = True
        self.runtime = new
        self.settings_version = version
        message = f"설정 v{version} 반영: 즉시 {changes['hot'] or '없음'}, 재시작 필요 {changes['restart'] or '없음'}"
        log.info(message)
        self.repo.log("INFO", "settings_applied", message, {"version": version, **changes})
        self._notify(EventKind.SETTINGS, f"설정 v{version} 반영", message, {"version": version, **changes})
        self.write_heartbeat()
        return changes

    # ------------------------------------------------------------------
    # 대시보드 이음새: 하트비트 · 명령 큐
    # ------------------------------------------------------------------
    @property
    def ws_status(self) -> str:
        if self.ws_factory is None:
            return "NOT_USED"
        return self._ws.state.value if self._ws is not None else "DISCONNECTED"

    def last_data_at(self) -> datetime | None:
        times = [t for s in self.state.prices.values() for t in (s.last_time, s.book_time) if t is not None]
        return max(times) if times else None

    def write_heartbeat(self, status: str | None = None, message: str = "") -> None:
        if status is None:
            status = "PAUSED" if self.paused else ("RUNNING" if self.status == "running" else self.status.upper())
        equity = self.current_equity()
        risk_snapshot = self.risk.snapshot() if isinstance(self.risk, RiskManager) else None
        self.repo.write_engine_status(
            status=status, strategy=self.strategy.name, markets=list(self.markets), interval=self.interval.value,
            started_at=self.stats.started_at, api_ok=self.stats.errors == 0 or self.stats.last_candle_check is not None,
            ws_status=self.ws_status, last_data_at=self.last_data_at(), last_candle_at=self.stats.last_candle_check,
            last_trade_at=self.last_trade_at, equity=equity, cash=self.portfolio.cash,
            message=message or ("재시작 필요: 마켓/캔들 단위 변경" if self.restart_required else ""),
            risk=risk_snapshot["state"] if risk_snapshot else None, pid=os.getpid(),
            settings_version=self.settings_version, restart_required=self.restart_required,
        )
        self.stats.heartbeats += 1
        self._last_heartbeat = self.clock()

    def poll_commands(self) -> list[str]:
        """DB 명령 큐를 읽어 처리한다. 처리한 명령 이름 목록을 돌려준다."""
        handled: list[str] = []
        for cmd in self.repo.pending_commands():
            name = cmd.command.lower()
            args = cmd.args or {}
            try:
                if name == "pause":
                    self.paused = True
                    result = "일시정지: 신규 매수 중단, 청산·손절은 계속"
                elif name == "resume":
                    self.paused = False
                    result = "재개"
                elif name == "stop":
                    self.stop()
                    result = "정지 요청 접수"
                elif name == "halt":
                    if isinstance(self.risk, RiskManager):
                        self.risk.halt(str(args.get("reason") or "수동 긴급 정지"))
                    self.paused = True
                    result = "긴급 정지: 신규 진입 차단 + 일시정지"
                    self._notify(EventKind.RISK_HALT, "긴급 정지",
                                 f"{args.get('reason') or '수동 긴급 정지'} · 신규 진입 차단 + 일시정지", args)
                elif name == "reload":
                    result = "설정 다시 읽기 예약 (다음 캔들 경계에 반영)"
                    self._reload_requested = True
                elif name == "resume_risk":
                    if isinstance(self.risk, RiskManager):
                        self.risk.resume()
                    result = "리스크 긴급 정지 해제"
                    self._notify(EventKind.RISK_HALT, "긴급 정지 해제", "신규 진입 다시 허용 (일시정지는 resume 필요)")
                else:
                    result = f"알 수 없는 명령: {cmd.command}"
            except Exception as exc:  # noqa: BLE001 - 명령 하나의 실패가 엔진을 멈추지 않게
                result = f"실패: {exc}"
            self.repo.mark_command(cmd.id, result)
            self.repo.log("INFO", "command", f"{name}: {result}", {"id": cmd.id, "args": args})
            log.info("명령 %s → %s", name, result)
            self.stats.commands_processed += 1
            handled.append(name)
        self._last_command_poll = self.clock()
        if handled:
            self.write_heartbeat()
        return handled

    # ------------------------------------------------------------------
    # 실행 루프
    # ------------------------------------------------------------------
    def _log_safely(self, level: str, event: str, message: str, data: dict[str, Any] | None = None) -> None:
        """DB 기록 실패(잠금 등)가 호출자를 죽이지 않게 한다."""
        try:
            self.repo.log(level, event, message, data)
        except Exception as exc:  # noqa: BLE001
            log.warning("로그 기록 실패(%s %s): %s", level, event, exc)

    async def _handle_price_message(self, message: Any) -> None:
        """시세 메시지 하나를 반영하고 청산 감시를 돌린다. 예외는 여기서 격리한다 (DB 잠금 등으로 루프가 죽지 않게)."""
        if not self.state.update_price(message):
            return
        self.stats.price_updates += 1
        if not self.portfolio.positions:
            return
        try:
            await self.check_exits(self.clock())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 한 메시지 처리 실패가 시세 감시를 멈추지 않게
            self.stats.errors += 1
            log.exception("청산 감시 처리 실패 (다음 시세에서 다시 시도)")
            self._log_safely("ERROR", "exit_check_failed", f"{type(exc).__name__}: {exc}")
            self._notify(EventKind.API_ERROR, "청산 감시 처리 실패", f"{type(exc).__name__}: {exc}", key="exit_check")

    async def _price_loop(self) -> None:
        """WebSocket 시세 루프 — 스트림이 끝나거나 예외가 나도 백오프 후 다시 붙고, 정지 명령에만 끝난다.

        (감사 HIGH-2)
        """
        if self.ws_factory is None:
            return
        subs = [Subscription.ticker(self.markets), Subscription.orderbook(self.markets, units=1)]
        backoff = self.price_loop_backoff
        first = True
        while not self._stop.is_set():
            if not first:
                self.stats.price_stream_restarts += 1
                log.info("시세 스트림 재연결 (%d회)", self.stats.price_stream_restarts)
            first = False
            self._ws = self.ws_factory(subs)
            try:
                async for message in self._ws.stream():
                    backoff = self.price_loop_backoff  # 정상 수신 → 백오프 초기화
                    await self._handle_price_message(message)
                log.warning("시세 스트림이 끝남 → %.0f초 뒤 재연결", backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 시세 스트림 장애는 엔진을 죽이지 않는다 (재연결, 그동안 REST 보정)
                self.stats.errors += 1
                log.error("시세 스트림 종료: %s (%.0f초 뒤 재연결, 그동안 REST 현재가로 대체)", exc, backoff)
                self._log_safely("ERROR", "price_stream_failed", str(exc))
                self._notify(EventKind.API_ERROR, "시세 스트림 끊김", f"{exc} ({backoff:.0f}초 뒤 재연결)",
                             key="price_stream")
            finally:
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.close()
            if self._stop.is_set():
                break
            await self.sleep(backoff)
            backoff = min(backoff * 2, self.price_loop_max_backoff)

    def _check_price_staleness(self, now: datetime) -> bool:
        """포지션이 있는데 시세가 price_stale_alert_seconds 넘게 끊겼으면 한 번 알리고 True.

        시세가 복구되면 다시 알릴 수 있게 리셋한다.
        """
        if not self.portfolio.positions:
            self._price_stale_alerted = False
            return False
        last = self.last_data_at()
        age = (now - last).total_seconds() if last is not None else None
        if age is not None and age < self.price_stale_alert_seconds:
            self._price_stale_alerted = False
            return False
        if not self._price_stale_alerted:
            self._price_stale_alerted = True
            text = f"{age:.0f}초" if age is not None else "시작 후 계속"
            log.error("시세 수신 중단 %s — 손절 감시가 REST 보정에만 의존 중", text)
            self._log_safely("ERROR", "price_stale",
                             f"시세 수신 중단 {text} (포지션 {len(self.portfolio.positions)}개)")
            self._notify(EventKind.API_ERROR, "시세 수신 중단", f"{text} 동안 시세가 없습니다. 손절 감시가 약해진 상태",
                         key="price_stale")
        return True

    async def run(self, *, duration_seconds: float | None = None) -> EngineStats:
        self.status = "starting"
        self.stats.started_at = self.clock()
        start_msg = f"{self.mode} 시작: {self.strategy.name} {self.markets} {self.interval.value}"
        start_data = {"params": self.strategy.params.model_dump(), "cash": self.portfolio.cash}
        self.repo.log("INFO", "bot_start", start_msg, start_data)
        # 엔진이 꺼져 있는 동안 큐에 쌓인 명령(예: 죽은 엔진에 보낸 stop)은 새 실행에 적용하지 않는다
        stale = self.repo.discard_pending_commands("무시: 엔진 시작 전에 들어온 명령")
        if stale:
            self.repo.log("WARNING", "stale_commands", f"시작 전 대기 명령 {stale}건 무시", {"count": stale})
            log.warning("엔진 시작 전 대기 명령 %d건 무시", stale)
        if self.notifier is not None:
            await self.notifier.start()
        restored = len(self.portfolio.positions)
        self._notify(
            EventKind.BOT_START, f"{self.strategy.name} · {', '.join(self.markets)} · {self.interval.value}",
            f"현금 {self.portfolio.cash:,.0f}원" + (f" · 보유 포지션 {restored}개 복구 (재시작)" if restored else ""),
            start_data,
        )
        self.write_heartbeat("STARTING")
        await self.warmup()
        if isinstance(self.broker, LiveBroker):
            diff = await self.broker.reconcile(self.markets)
            log.info("거래소 잔고 동기화: %s", diff)
            self.repo.log("INFO", "reconcile", "거래소 잔고와 내부 계좌 동기화", diff)
            self.repo.sync_portfolio(self.portfolio)
        self.load_pending_orders()
        await self.resolve_pending_orders(self.clock())
        await self.ensure_prices()
        equity = self.snapshot()
        self.rebuild_risk_state(self.clock(), equity)
        price_task = asyncio.create_task(self._price_loop(), name="price-loop")
        self.status = "running"
        self.write_heartbeat("RUNNING")
        deadline = self.clock() + timedelta(seconds=duration_seconds) if duration_seconds else None
        last_snapshot = self.clock()
        try:
            while not self._stop.is_set():
                now = self.clock()
                if deadline and now >= deadline:
                    break
                target = next_boundary(now, self.interval.seconds, self.settings.candle_grace_seconds)
                snapshot_at = last_snapshot + timedelta(seconds=self.settings.snapshot_interval_seconds)
                poll_at = now + timedelta(seconds=self.command_poll_interval)
                wake = min(target, snapshot_at, poll_at, deadline) if deadline else min(target, snapshot_at, poll_at)
                wait = max((wake - now).total_seconds(), 0.0)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=wait) if wait > 0 else None
                if self._stop.is_set():
                    break
                now = self.clock()
                self.poll_commands()
                if self._stop.is_set():
                    break
                if self._reload_requested:
                    self._reload_requested = False
                    try:
                        await self.maybe_reload_settings()
                    except TraderError as exc:
                        self.stats.errors += 1
                        log.error("설정 다시 읽기 실패: %s", exc)
                if self.pending_orders:
                    await self.resolve_pending_orders(now)
                self._check_price_staleness(now)
                heartbeat_due = self._last_heartbeat is None or (
                    (now - self._last_heartbeat).total_seconds() >= self.heartbeat_interval
                )
                if heartbeat_due:
                    self.write_heartbeat()
                if now >= target - timedelta(milliseconds=1):
                    try:
                        await self.ensure_prices(now)
                        await self.check_exits(now, force=True)
                        await self.maybe_reload_settings()
                        await self.process_closed_candles(now)
                    except TraderError as exc:
                        self.stats.errors += 1
                        log.error("캔들 처리 실패: %s", exc)
                if now >= snapshot_at - timedelta(milliseconds=1):
                    await self.ensure_prices(now)
                    if isinstance(self.broker, LiveBroker):
                        try:
                            await self.broker.reconcile(self.markets)
                            self.repo.sync_portfolio(self.portfolio)
                        except TraderError as exc:
                            self.stats.errors += 1
                            log.error("잔고 동기화 실패: %s", exc)
                    self.snapshot(now)
                    last_snapshot = now
        except BaseException as exc:  # 비정상 종료·Ctrl+C 도 알림에 남긴다
            self._stop_error = exc
            raise
        finally:
            self.status = "stopping"
            price_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await price_task
            if self._ws is not None:
                await self._ws.close()
            await self.ensure_prices()
            self.snapshot()
            self.repo.sync_portfolio(self.portfolio)
            self.repo.log("INFO", "bot_stop", f"{self.mode} 종료", {"stats": self.stats_dict()})
            self._notify(EventKind.BOT_STOP, self._stop_reason(), self._stop_summary())
            if self.notifier is not None:
                with contextlib.suppress(Exception):
                    await self.notifier.close(drain_seconds=5.0)
                self.stats.extra["notifications"] = self.notifier.stats_dict()
            self.status = "stopped"
            self.write_heartbeat("STOPPED")
        return self.stats

    def stop(self) -> None:
        self._stop.set()

    def stats_dict(self) -> dict[str, Any]:
        s = self.stats
        return {
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "candle_checks": s.candle_checks, "closed_candles": s.closed_candles, "signals": s.signals,
            "actionable_signals": s.actionable_signals, "orders_filled": s.orders_filled,
            "orders_rejected": s.orders_rejected, "orders_unknown": s.orders_unknown,
            "orders_resolved": s.orders_resolved, "price_stream_restarts": s.price_stream_restarts,
            "stale_signals_skipped": s.stale_signals_skipped,
            "risk_rejections": s.risk_rejections,
            "snapshots": s.snapshots, "price_updates": s.price_updates, "errors": s.errors,
            "exits_triggered": s.exits_triggered, "risk_locks": s.risk_locks, "last_equity": s.last_equity,
            "commands_processed": s.commands_processed, "heartbeats": s.heartbeats, "paused": self.paused,
            "mode": self.mode,
            "risk": self.risk.snapshot() if isinstance(self.risk, RiskManager) else None,
        }
