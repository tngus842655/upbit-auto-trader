"""MACD 시그널 교차 전략 (MACDStrategy) — 추세 추종 계열.

- 매수: MACD 선이 시그널선을 아래에서 위로 돌파 (0선 필터를 켜면 그 캔들의 MACD > 0 일 때만)
- 매도: MACD 선이 시그널선을 위에서 아래로 돌파 (0선 필터와 무관)

MACD = EMA(``fast_period``) − EMA(``slow_period``), 시그널 = EMA(MACD, ``signal_period``), EMA 는 재귀식(adjust=False).
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd
from pydantic import model_validator

from app.strategy.base import (
    ACTION_COLUMN,
    REASON_COLUMN,
    Strategy,
    StrategyFamily,
    StrategyParams,
    action_series,
    param,
    reasons,
    warmup_mask,
)
from app.strategy.indicators import cross_above, cross_below, macd


class MACDParams(StrategyParams):
    fast_period: int = param(
        12, ge=2, le=200, label="Fast 기간", description="빠른 EMA 캔들 수",
        help_text="MACD 선 = 빠른 EMA − 느린 EMA. 빠른 EMA 기간입니다. 느린 기간보다 작아야 합니다. 보통 12.",
    )
    slow_period: int = param(
        26, ge=3, le=500, label="Slow 기간", description="느린 EMA 캔들 수",
        help_text="느린 EMA 기간입니다. 보통 26. 이 길이 + 시그널 기간만큼 캔들이 쌓여야 첫 신호가 납니다.",
    )
    signal_period: int = param(
        9, ge=2, le=200, label="Signal 기간", description="MACD 의 EMA (시그널선)",
        help_text="시그널선 = MACD 선의 EMA 입니다. 보통 9.\n"
                  "MACD 가 이 선을 위로 뚫으면 매수, 아래로 뚫으면 매도합니다.",
    )
    zero_line_filter: bool = param(
        True, label="0선 필터", description="MACD > 0 일 때만 매수",
        help_text="켜면 상향 돌파 캔들의 MACD 가 0 보다 클 때(상승 추세 구간)만 매수합니다.\n"
                  "0 아래의 반등 돌파는 건너뜁니다. 매도에는 적용되지 않습니다.",
    )

    @model_validator(mode="after")
    def _check_periods(self) -> MACDParams:
        if self.fast_period >= self.slow_period:
            raise ValueError(f"fast_period({self.fast_period}) 는 slow_period({self.slow_period}) 보다 작아야 합니다")
        return self


class MACDStrategy(Strategy):
    name: ClassVar[str] = "macd"
    description: ClassVar[str] = "MACD Signal 교차"
    family: ClassVar[StrategyFamily] = StrategyFamily.TREND
    rules: ClassVar[str] = (
        "매수: MACD 선이 시그널선을 상향 돌파 (0선 필터를 켜면 MACD > 0 일 때만)\n"
        "매도: MACD 선이 시그널선을 하향 돌파"
    )
    Params: ClassVar[type[StrategyParams]] = MACDParams
    params: MACDParams

    @property
    def warmup_periods(self) -> int:
        # MACD 선은 slow 번째 캔들부터, 시그널선은 그 뒤 signal-1 캔들부터 나온다. 교차 판정에 직전 캔들이 하나 더 필요
        return self.params.slow_period + self.params.signal_period

    @property
    def indicator_columns(self) -> list[str]:
        return ["macd", "macd_signal", "macd_hist"]

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df[["close"]].copy()
        m = macd(df["close"], p.fast_period, p.slow_period, p.signal_period)
        out["macd"] = m["macd"]
        out["macd_signal"] = m["signal"]
        out["macd_hist"] = m["hist"]
        golden = cross_above(out["macd"], out["macd_signal"])
        dead = cross_below(out["macd"], out["macd_signal"])
        zero_ok = (out["macd"] > 0).fillna(False) if p.zero_line_filter else pd.Series(True, index=df.index)

        warm = warmup_mask(df, self.warmup_periods)
        buy = golden & zero_ok & ~warm
        sell = dead & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        label = f"MACD({p.fast_period},{p.slow_period},{p.signal_period})"
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"{label} 시그널 상향 돌파" + (" (0선 위)" if p.zero_line_filter else "")),
            (sell, f"{label} 시그널 하향 돌파"),
            (golden & ~zero_ok, "시그널 상향 돌파이지만 MACD ≤ 0 (0선 필터)"),
        )
        return out
