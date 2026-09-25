"""백테스트 엔진.

체결 규칙 (Look-ahead 방지):
- 캔들 i 가 닫힌 뒤 전략이 신호를 낸다 → 가장 빠른 체결은 **캔들 i+1 시가**. 종가 체결은 쓰지 않는다.
- 시장가 체결에는 슬리피지를 적용한다: 매수 = 시가 × (1 + s), 매도 = 시가 × (1 − s).
- 손절/익절/추적 손절은 ``RiskManager.check_exits`` 가 보유 중인 캔들의 저가/고가로 판정한다(손절 우선).
  손절가보다 낮게 갭 하락하면 시가에 체결된다. 익절은 지정가로 보고 슬리피지를 적용하지 않되 고가가 목표가를
  **지나가야** 체결로 본다. 추적 손절은 직전 캔들까지의 최고가로 판정한다(같은 캔들의 고가로 먼저 올리지 않음,
  감사 MEDIUM-8).
- 진입 크기·한도(거래당 상한, 자산 대비 비율, 일일 손실, 연속 손실, 최대 포지션 수)는 ``RiskManager.evaluate`` 가
  정한다 — 모의매매·실거래와 **같은 코드** 다.
- 보유 중 BUY 신호와 미보유 SELL 신호는 무시하고 횟수만 센다 (피라미딩 없음, 롱 온리).
- 마지막 캔들에서 보유 중이면 종가로 청산해 성과를 확정한다 (exit_reason = end_of_data).
- Buy & Hold 벤치마크: 첫 캔들 시가에 전액 매수(슬리피지·수수료 포함), 마지막 캔들 종가에 청산.

전략의 ``evaluate(df)`` 는 인과적이어야 하며, 기본적으로 ``check_no_lookahead`` 를 먼저 실행한다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.backtest.metrics import PerformanceMetrics, compute_metrics
from app.exchange.models import CandleInterval
from app.risk.config import RiskConfig
from app.risk.manager import EXIT_TAKE_PROFIT, RiskManager
from app.strategy.base import ACTION_COLUMN, REASON_COLUMN, Action, Signal, Strategy, check_no_lookahead
from app.strategy.data import validate_candles
from app.trading.portfolio import DEFAULT_FEE_RATE, DEFAULT_MIN_ORDER_AMOUNT, Portfolio, PortfolioError, Trade

log = logging.getLogger(__name__)

EXIT_SIGNAL = "signal"
EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT_REASON = EXIT_TAKE_PROFIT
EXIT_END_OF_DATA = "end_of_data"


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_capital: float = Field(default=1_000_000.0, gt=0, description="초기 자본 (KRW)")
    fee_rate: float = Field(
        default=DEFAULT_FEE_RATE, ge=0, lt=0.1, description="편도 수수료율 (업비트 KRW 마켓 0.0005)"
    )
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1, description="시장가 체결 슬리피지 비율")
    position_fraction: float = Field(default=1.0, gt=0, le=1.0, description="매수 시 사용할 현금 비율")
    min_order_amount: float = Field(default=DEFAULT_MIN_ORDER_AMOUNT, ge=0, description="최소 주문 금액 (KRW)")
    stop_loss_pct: float | None = Field(default=None, gt=0, lt=1, description="평균 매수가 대비 손절 비율 (0.05 = -5%)")
    take_profit_pct: float | None = Field(default=None, gt=0, description="평균 매수가 대비 익절 비율 (0.1 = +10%)")
    check_lookahead: bool = Field(default=True, description="실행 전 전략 Look-ahead 검사")
    risk: RiskConfig | None = Field(default=None, description="전체 리스크 설정. 주면 위의 손절·익절·비율보다 우선")

    def risk_config(self) -> RiskConfig:
        if self.risk is not None:
            return self.risk
        return RiskConfig.unrestricted(
            position_fraction=self.position_fraction, min_order_amount=self.min_order_amount,
            stop_loss_pct=self.stop_loss_pct, take_profit_pct=self.take_profit_pct,
        )


@dataclass
class BacktestResult:
    market: str
    interval: str
    strategy_name: str
    strategy_params: dict[str, Any]
    config: BacktestConfig
    start: datetime
    end: datetime
    equity: pd.Series
    in_position: pd.Series
    trades: list[Trade]
    metrics: PerformanceMetrics
    benchmark_equity: pd.Series
    benchmark_metrics: PerformanceMetrics
    signals: pd.DataFrame  # 전략 evaluate 결과 (지표 + action)
    risk_config: RiskConfig = field(default_factory=RiskConfig.unrestricted)
    ignored_buy_signals: int = 0
    ignored_sell_signals: int = 0
    risk_rejections: dict[str, int] = field(default_factory=dict)
    rejected_orders: list[str] = field(default_factory=list)
    unfilled_signal_at_end: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "interval": self.interval,
            "strategy": self.strategy_name,
            "strategy_params": self.strategy_params,
            "config": self.config.model_dump(),
            "risk": self.risk_config.model_dump(),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "candles": int(len(self.equity)),
            "metrics": self.metrics.to_dict(),
            "benchmark": self.benchmark_metrics.to_dict(),
            "ignored_buy_signals": self.ignored_buy_signals,
            "ignored_sell_signals": self.ignored_sell_signals,
            "risk_rejections": dict(self.risk_rejections),
            "rejected_orders": list(self.rejected_orders),
            "unfilled_signal_at_end": self.unfilled_signal_at_end,
            "exit_reasons": self.exit_reason_counts(),
        }

    def exit_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.trades:
            counts[t.exit_reason] = counts.get(t.exit_reason, 0) + 1
        return counts


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(
        self,
        df: pd.DataFrame,
        strategy: Strategy,
        *,
        market: str | None = None,
        interval: CandleInterval | str | None = None,
    ) -> BacktestResult:
        cfg = self.config
        interval_value = interval or df.attrs.get("interval") or ""
        interval_enum = CandleInterval.parse(interval_value)
        market = market or df.attrs.get("market") or "UNKNOWN"
        validate_candles(df, interval=interval_enum)
        if len(df) < 2:
            raise ValueError("백테스트에는 캔들이 2개 이상 필요합니다")
        if cfg.check_lookahead and len(df) > strategy.warmup_periods + 2:
            check_no_lookahead(strategy, df)

        evaluated = strategy.evaluate(df)
        actions = evaluated[ACTION_COLUMN].astype(str).to_numpy()
        reasons = evaluated[REASON_COLUMN].astype(str).to_numpy() if REASON_COLUMN in evaluated else [""] * len(df)
        opens = df["open"].to_numpy(dtype="float64")
        highs = df["high"].to_numpy(dtype="float64")
        lows = df["low"].to_numpy(dtype="float64")
        closes = df["close"].to_numpy(dtype="float64")
        times = [ts.to_pydatetime() for ts in df.index]
        n = len(df)

        risk = RiskManager(cfg.risk_config())
        portfolio = Portfolio(cfg.initial_capital, fee_rate=cfg.fee_rate, min_order_amount=cfg.min_order_amount)
        equity = np.empty(n, dtype="float64")
        in_position = np.zeros(n, dtype=bool)
        pending: tuple[str, float | None] | None = None  # (action, 매수 예산)
        ignored_buy = ignored_sell = 0
        risk_rejections: dict[str, int] = {}
        rejected: list[str] = []
        trades_seen = 0
        s = cfg.slippage_rate
        last_equity = cfg.initial_capital

        def record_new_trades(now: datetime) -> None:
            nonlocal trades_seen
            for trade in portfolio.trades[trades_seen:]:
                lock = risk.record_trade(trade, now)
                if lock:
                    log.info("%s 리스크 잠금: %s", now.isoformat(), lock)
            trades_seen = len(portfolio.trades)

        for i in range(n):
            t = times[i]
            risk.start_day_if_needed(t, last_equity)

            # 1) 직전 캔들 신호를 이번 캔들 시가에 체결
            if pending is not None:
                action, amount = pending
                if action == Action.BUY.value and not portfolio.has_position(market):
                    try:
                        portfolio.buy(market, opens[i] * (1 + s), time=t, amount=amount, reason=EXIT_SIGNAL)
                    except PortfolioError as exc:
                        rejected.append(f"{t.isoformat()} BUY 거부: {exc}")
                elif action == Action.SELL.value and portfolio.has_position(market):
                    portfolio.sell(market, opens[i] * (1 - s), time=t, reason=EXIT_SIGNAL)
                    record_new_trades(t)
                pending = None

            # 2) 손절 / 익절 / 추적 손절 (캔들 저가·고가 기준)
            pos = portfolio.position(market)
            if pos is not None:
                exit_check = risk.check_exits(pos, low=lows[i], high=highs[i], now=t)
                if exit_check is not None:
                    if exit_check.reason == EXIT_TAKE_PROFIT:
                        fill_price = max(opens[i], exit_check.trigger_price)  # 지정가 익절: 슬리피지 없음
                    else:
                        fill_price = min(opens[i], exit_check.trigger_price) * (1 - s)
                    portfolio.sell(market, fill_price, time=t, reason=exit_check.reason)
                    record_new_trades(t)

            # 3) 마지막 캔들이면 청산
            if i == n - 1 and portfolio.has_position(market):
                portfolio.sell(market, closes[i] * (1 - s), time=t, reason=EXIT_END_OF_DATA)
                record_new_trades(t)

            # 4) 평가
            in_position[i] = portfolio.has_position(market)
            equity[i] = portfolio.equity({market: closes[i]})
            last_equity = float(equity[i])
            risk.update_equity(last_equity, t)

            # 5) 이번 캔들 신호 → 리스크 심사 → 다음 캔들 체결 예약
            action = actions[i]
            if action == Action.BUY.value:
                if portfolio.has_position(market):
                    ignored_buy += 1
                else:
                    signal = Signal(Action.BUY, market, t, float(closes[i]), strategy.name, reason=str(reasons[i]))
                    decision = risk.evaluate(signal, portfolio, None, t, equity=last_equity)
                    if decision.approved:
                        pending = (action, decision.amount)
                    else:
                        key = decision.reason.split(":")[0].split("(")[0].strip()
                        risk_rejections[key] = risk_rejections.get(key, 0) + 1
            elif action == Action.SELL.value:
                if portfolio.has_position(market):
                    pending = (action, None)
                else:
                    ignored_sell += 1

        unfilled = pending[0] if pending else None
        equity_series = pd.Series(equity, index=df.index, name="equity")
        in_position_series = pd.Series(in_position, index=df.index, name="in_position")
        metrics = compute_metrics(
            equity_series, portfolio.trades, initial_capital=cfg.initial_capital,
            interval_seconds=interval_enum.seconds, total_fees=portfolio.fees_paid, in_position=in_position_series,
        )
        bench_equity, bench_trades, bench_fees = self._buy_and_hold(df, market)
        bench_metrics = compute_metrics(
            bench_equity, bench_trades, initial_capital=cfg.initial_capital,
            interval_seconds=interval_enum.seconds, total_fees=bench_fees,
            in_position=pd.Series(True, index=df.index),
        )
        log.info(
            "백테스트 완료 %s %s %s: 거래 %d회, 수익률 %.2f%%, MDD %.2f%%, B&H %.2f%%",
            market, interval_enum.value, strategy.name, metrics.total_trades, metrics.total_return * 100,
            metrics.mdd * 100, bench_metrics.total_return * 100,
        )
        return BacktestResult(
            market=market,
            interval=interval_enum.value,
            strategy_name=strategy.name,
            strategy_params=strategy.params.model_dump(),
            config=cfg,
            start=times[0],
            end=times[-1],
            equity=equity_series,
            in_position=in_position_series,
            trades=list(portfolio.trades),
            metrics=metrics,
            benchmark_equity=bench_equity,
            benchmark_metrics=bench_metrics,
            signals=evaluated,
            risk_config=risk.config,
            ignored_buy_signals=ignored_buy,
            ignored_sell_signals=ignored_sell,
            risk_rejections=risk_rejections,
            rejected_orders=rejected,
            unfilled_signal_at_end=unfilled,
        )

    def _buy_and_hold(self, df: pd.DataFrame, market: str) -> tuple[pd.Series, list[Trade], float]:
        cfg = self.config
        portfolio = Portfolio(cfg.initial_capital, fee_rate=cfg.fee_rate, min_order_amount=cfg.min_order_amount)
        opens = df["open"].to_numpy(dtype="float64")
        closes = df["close"].to_numpy(dtype="float64")
        times = [ts.to_pydatetime() for ts in df.index]
        try:
            portfolio.buy(market, opens[0] * (1 + cfg.slippage_rate), time=times[0], reason="buy_and_hold")
        except PortfolioError as exc:
            log.warning("Buy&Hold 매수 실패(자본 부족?): %s", exc)
            flat = pd.Series(cfg.initial_capital, index=df.index, name="benchmark")
            return flat, [], 0.0
        equity = np.array([portfolio.equity({market: c}) for c in closes], dtype="float64")
        portfolio.sell(market, closes[-1] * (1 - cfg.slippage_rate), time=times[-1], reason=EXIT_END_OF_DATA)
        equity[-1] = portfolio.cash
        return pd.Series(equity, index=df.index, name="benchmark"), list(portfolio.trades), portfolio.fees_paid


def format_pct(value: float) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if math.isinf(value):
        return "inf"
    return f"{value * 100:+.2f}%"
