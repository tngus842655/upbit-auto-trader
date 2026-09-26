"""백테스트 결과 출력·저장.

- ``format_report()``: 콘솔용 한글 보고서 (전략 vs Buy & Hold 비교표, 거래 목록 일부)
- ``save_result()``: ``summary.json`` / ``trades.csv`` / ``equity.csv`` / ``signals.csv`` 저장
  (Phase 8 대시보드에서 재사용)
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.backtest.engine import BacktestResult
from app.backtest.metrics import PerformanceMetrics
from app.exchange.models import KST


def _is_nan(value: float | None) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _pct(value: float | None) -> str:
    """부호 있는 수익률 표기 (+1.23%)."""
    if _is_nan(value):
        return "n/a"
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return f"{value * 100:+.2f}%"


def _ratio(value: float | None) -> str:
    """부호 없는 비율 표기 (승률·노출 등)."""
    if _is_nan(value):
        return "n/a"
    return f"{value * 100:.2f}%"


def _num(value: float | None, digits: int = 2) -> str:
    if _is_nan(value):
        return "n/a"
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return f"{value:.{digits}f}"


def _krw(value: float) -> str:
    return f"{value:,.0f}"


def _kst(value: datetime | None) -> str:
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M") if value else "-"


def format_report(result: BacktestResult, *, max_trades: int = 10) -> str:
    m, b = result.metrics, result.benchmark_metrics
    cfg = result.config
    rc = result.risk_config
    stop = _ratio(rc.stop_loss_pct) if rc.stop_loss_pct else "없음"
    target = _ratio(rc.take_profit_pct) if rc.take_profit_pct else "없음"
    lines: list[str] = [
        f"=== 백테스트: {result.strategy_name} {result.strategy_params} / {result.market} {result.interval} ===",
        f"  기간 {_kst(result.start)} ~ {_kst(result.end)} KST ({m.duration_days:.1f}일, 캔들 {m.periods}개)",
        f"  초기자본 {_krw(cfg.initial_capital)} KRW, 수수료 {cfg.fee_rate * 100:.3f}%, "
        f"슬리피지 {cfg.slippage_rate * 100:.3f}%, 현금 사용 {rc.position_fraction * 100:.0f}%, "
        f"손절 {stop}, 익절 {target}",
        "  리스크: " + ", ".join(f"{k} {v}" for k, v in rc.describe().items()),
        "",
        f"  {'지표':<16} {'전략':>18} {'Buy & Hold':>18}",
    ]
    rows: list[tuple[str, str, str]] = [
        ("최종 자산", _krw(m.final_equity), _krw(b.final_equity)),
        ("총 수익률", _pct(m.total_return), _pct(b.total_return)),
        ("연환산 수익률", _pct(m.cagr), _pct(b.cagr)),
        ("최대 낙폭(MDD)", _pct(m.mdd), _pct(b.mdd)),
        ("연환산 변동성", _ratio(m.volatility), _ratio(b.volatility)),
        ("Sharpe", _num(m.sharpe), _num(b.sharpe)),
        ("Sortino", _num(m.sortino), _num(b.sortino)),
        ("수수료 합계", _krw(m.total_fees), _krw(b.total_fees)),
        ("시장 노출", _ratio(m.exposure), _ratio(b.exposure)),
    ]
    for name, a, c in rows:
        lines.append(f"  {name:<16} {a:>18} {c:>18}")
    lines.append("")
    lines.append(
        f"  거래 {m.total_trades}회 (승 {m.wins} / 패 {m.losses}), 승률 {_ratio(m.win_rate)}, "
        f"Profit Factor {_num(m.profit_factor)}, 기대값 {_krw(m.expectancy)} KRW/거래, "
        f"최대 연속 손실 {m.max_consecutive_losses}회"
    )
    lines.append(
        f"  평균 수익 {_krw(m.avg_win)} KRW ({_pct(m.avg_win_pct)}), "
        f"평균 손실 {_krw(m.avg_loss)} KRW ({_pct(m.avg_loss_pct)}), "
        f"거래당 평균 수익률 {_pct(m.avg_trade_return)}"
    )
    lines.append(f"  실현 손익 {_krw(m.realized_pnl)} KRW (수수료 {_krw(m.total_fees)} KRW·슬리피지 반영 후)")
    if m.mdd_peak_time:
        lines.append(f"  MDD 구간: {_kst(m.mdd_peak_time)} 고점 → {_kst(m.mdd_trough_time)} 저점")
    exits = ", ".join(f"{k} {v}" for k, v in result.exit_reason_counts().items()) or "-"
    extra = ""
    if result.risk_rejections:
        extra += " | 리스크 거부: " + ", ".join(f"{k} {v}" for k, v in result.risk_rejections.items())
    if result.rejected_orders:
        extra += f" | 거부된 주문 {len(result.rejected_orders)}건"
    if result.unfilled_signal_at_end:
        extra += f" | 마지막 캔들 미체결 신호: {result.unfilled_signal_at_end}"
    ignored = f"매수 {result.ignored_buy_signals}, 매도 {result.ignored_sell_signals}"
    lines.append(f"  청산 사유: {exits} | 무시된 신호: {ignored}{extra}")
    if result.trades:
        lines.append("")
        shown = result.trades[-max_trades:]
        lines.append(f"  최근 거래 {len(shown)}건 (총 {len(result.trades)}건):")
        lines.append(
            f"  {'진입(KST)':<17} {'진입가':>14} {'청산(KST)':<17} {'청산가':>14} "
            f"{'손익(KRW)':>13} {'손익률':>9}  사유"
        )
        for t in shown:
            lines.append(
                f"  {_kst(t.entry_time):<17} {t.entry_price:>14,.0f} {_kst(t.exit_time):<17} {t.exit_price:>14,.0f} "
                f"{t.pnl:>13,.0f} {_pct(t.pnl_pct):>9}  {t.exit_reason}"
            )
    lines.append("")
    lines.append(
        "  ※ 과거 데이터에 대한 결과일 뿐 미래 수익을 보장하지 않는다. "
        "파라미터를 결과에 맞춰 조정하면 과최적화된다."
    )
    return "\n".join(lines)


def save_result(result: BacktestResult, out_dir: Path | str) -> Path:
    """결과 파일들을 ``out_dir`` 에 저장하고 그 경로를 돌려준다."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(result.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame([t.to_dict() for t in result.trades]).to_csv(out / "trades.csv", index=False, encoding="utf-8")
    curve = pd.DataFrame(
        {"equity": result.equity, "benchmark": result.benchmark_equity, "in_position": result.in_position}
    )
    curve.index.name = "time"
    curve.to_csv(out / "equity.csv", encoding="utf-8")
    signals = result.signals.copy()
    signals.index.name = "time"
    signals.to_csv(out / "signals.csv", encoding="utf-8")
    return out


def metrics_table(metrics: PerformanceMetrics) -> dict[str, str]:
    """대시보드·로그용 짧은 문자열 표."""
    return {
        "총 수익률": _pct(metrics.total_return),
        "연환산": _pct(metrics.cagr),
        "MDD": _pct(metrics.mdd),
        "Sharpe": _num(metrics.sharpe),
        "거래": str(metrics.total_trades),
        "승률": _ratio(metrics.win_rate),
        "PF": _num(metrics.profit_factor),
        "거래당": _pct(metrics.avg_trade_return),
        "실현손익": _krw(metrics.realized_pnl),
    }
