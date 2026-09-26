"""성과 지표: 자산 곡선(equity curve)과 왕복 거래 목록으로 계산한다.

- 총 수익률, 연환산 수익률(CAGR), 최대 낙폭(MDD)과 시점, 연환산 변동성, Sharpe, Sortino
- 거래 횟수, 승률, 평균 수익/손실(금액·비율), 거래당 평균 수익률, Profit Factor, 기대값, 실현 손익, 최대 연속 손실,
  수수료 합계, 시장 노출 비율
- 암호화폐는 연중무휴 거래되므로 연환산 계수는 ``365.25일 / 캔들 길이`` 로 계산한다. 무위험 수익률은 0 으로 둔다.

이 숫자들은 과거 데이터에 대한 설명일 뿐 미래 수익을 뜻하지 않는다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.trading.portfolio import Trade

SECONDS_PER_YEAR = 365.25 * 86_400


def periods_per_year(interval_seconds: float) -> float:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds 는 0보다 커야 합니다")
    return SECONDS_PER_YEAR / interval_seconds


def max_drawdown(equity: pd.Series) -> tuple[float, datetime | None, datetime | None]:
    """(MDD(음수 비율), 고점 시각, 저점 시각). 낙폭이 없으면 (0, None, None)."""
    if equity.empty:
        return 0.0, None, None
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    mdd = float(drawdown.min())
    if mdd >= 0:
        return 0.0, None, None
    trough = drawdown.idxmin()
    peak_time = equity.loc[:trough].idxmax()
    return mdd, peak_time.to_pydatetime(), trough.to_pydatetime()


def cagr(initial: float, final: float, elapsed_seconds: float) -> float:
    if initial <= 0 or elapsed_seconds <= 0:
        return math.nan
    if final <= 0:
        return -1.0
    years = elapsed_seconds / SECONDS_PER_YEAR
    return (final / initial) ** (1.0 / years) - 1.0


def sharpe_ratio(returns: pd.Series, ppy: float) -> float:
    r = returns.dropna()
    if len(r) < 2:
        return math.nan
    std = float(r.std(ddof=1))
    if std == 0:
        return 0.0
    return float(r.mean()) / std * math.sqrt(ppy)


def sortino_ratio(returns: pd.Series, ppy: float) -> float:
    r = returns.dropna()
    if len(r) < 2:
        return math.nan
    downside = r[r < 0]
    if downside.empty:
        return math.inf if float(r.mean()) > 0 else 0.0
    dd = math.sqrt(float((downside**2).mean()))
    if dd == 0:
        return 0.0
    return float(r.mean()) / dd * math.sqrt(ppy)


def max_consecutive_losses(trades: Sequence[Trade]) -> int:
    worst = run = 0
    for t in trades:
        run = run + 1 if t.pnl <= 0 else 0
        worst = max(worst, run)
    return worst


@dataclass(frozen=True)
class PerformanceMetrics:
    initial_capital: float
    final_equity: float
    total_return: float
    cagr: float
    mdd: float
    mdd_peak_time: datetime | None
    mdd_trough_time: datetime | None
    volatility: float
    sharpe: float
    sortino: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_trade_return: float  # 거래당 평균 수익률 (수수료 포함 pnl_pct 평균, 거래가 없으면 NaN)
    profit_factor: float
    expectancy: float
    realized_pnl: float  # 청산된 거래 손익 합계 (KRW, 수수료 차감 후)
    max_consecutive_losses: int
    total_fees: float
    exposure: float
    duration_days: float
    periods: int

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in list(out.items()):
            if isinstance(value, datetime):
                out[key] = value.isoformat()
            elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                out[key] = None
        return out


def compute_metrics(
    equity: pd.Series,
    trades: Sequence[Trade],
    *,
    initial_capital: float,
    interval_seconds: float,
    total_fees: float,
    in_position: pd.Series | None = None,
) -> PerformanceMetrics:
    if equity.empty:
        raise ValueError("equity 가 비어 있습니다")
    ppy = periods_per_year(interval_seconds)
    final_equity = float(equity.iloc[-1])
    elapsed = (equity.index[-1] - equity.index[0]).total_seconds() + interval_seconds
    returns = equity.pct_change()
    mdd, peak_t, trough_t = max_drawdown(equity)

    pnls = np.array([t.pnl for t in trades], dtype="float64")
    pcts = np.array([t.pnl_pct for t in trades], dtype="float64")
    wins_mask = pnls > 0
    losses_mask = pnls <= 0
    n = len(trades)
    wins = int(wins_mask.sum())
    losses = int(losses_mask.sum())
    gross_profit = float(pnls[wins_mask].sum()) if wins else 0.0
    gross_loss = float(-pnls[losses_mask].sum()) if losses else 0.0
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else math.nan

    return PerformanceMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_return=final_equity / initial_capital - 1.0,
        cagr=cagr(initial_capital, final_equity, elapsed),
        mdd=mdd,
        mdd_peak_time=peak_t,
        mdd_trough_time=trough_t,
        volatility=float(returns.std(ddof=1)) * math.sqrt(ppy) if len(returns.dropna()) > 1 else math.nan,
        sharpe=sharpe_ratio(returns, ppy),
        sortino=sortino_ratio(returns, ppy),
        total_trades=n,
        wins=wins,
        losses=losses,
        win_rate=wins / n if n else math.nan,
        avg_win=float(pnls[wins_mask].mean()) if wins else 0.0,
        avg_loss=float(pnls[losses_mask].mean()) if losses else 0.0,
        avg_win_pct=float(pcts[wins_mask].mean()) if wins else 0.0,
        avg_loss_pct=float(pcts[losses_mask].mean()) if losses else 0.0,
        avg_trade_return=float(pcts.mean()) if n else math.nan,
        profit_factor=profit_factor,
        expectancy=float(pnls.mean()) if n else 0.0,
        realized_pnl=float(pnls.sum()),
        max_consecutive_losses=max_consecutive_losses(trades),
        total_fees=total_fees,
        exposure=float(in_position.mean()) if in_position is not None and len(in_position) else 0.0,
        duration_days=elapsed / 86_400,
        periods=len(equity),
    )
