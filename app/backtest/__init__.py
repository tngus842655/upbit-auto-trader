"""백테스트 패키지 (Phase 4)."""

from app.backtest.engine import (
    EXIT_END_OF_DATA,
    EXIT_SIGNAL,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
)
from app.backtest.loader import load_candles
from app.backtest.metrics import PerformanceMetrics, compute_metrics, max_drawdown
from app.backtest.report import format_report, save_result

__all__ = [
    "EXIT_END_OF_DATA",
    "EXIT_SIGNAL",
    "EXIT_STOP_LOSS",
    "EXIT_TAKE_PROFIT",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "PerformanceMetrics",
    "compute_metrics",
    "format_report",
    "load_candles",
    "max_drawdown",
    "save_result",
]
