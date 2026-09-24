"""RSI 평균회귀 전략 (RSIStrategy) — 전략 추가가 쉬운지 보여주는 두 번째 예시.

- RSI 가 과매도선(``oversold``)을 아래에서 위로 다시 넘어서면 BUY (과매도 탈출)
- RSI 가 과매수선(``overbought``)을 위에서 아래로 내려오면 SELL (과매수 이탈)
- 그 외 HOLD
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd
from pydantic import Field, model_validator

from app.strategy.base import ACTION_COLUMN, REASON_COLUMN, Action, Strategy, StrategyParams, reasons
from app.strategy.indicators import rsi


class RSIParams(StrategyParams):
    window: int = Field(default=14, ge=2, le=200)
    oversold: float = Field(default=30.0, ge=1.0, le=99.0)
    overbought: float = Field(default=70.0, ge=1.0, le=99.0)

    @model_validator(mode="after")
    def _check_levels(self) -> RSIParams:
        if self.oversold >= self.overbought:
            raise ValueError(f"oversold({self.oversold}) 는 overbought({self.overbought}) 보다 작아야 합니다")
        return self


class RSIStrategy(Strategy):
    name: ClassVar[str] = "rsi"
    description: ClassVar[str] = "RSI 과매도 탈출 매수 / 과매수 이탈 매도"
    Params: ClassVar[type[StrategyParams]] = RSIParams
    params: RSIParams

    @property
    def warmup_periods(self) -> int:
        return self.params.window + 2

    @property
    def indicator_columns(self) -> list[str]:
        return ["rsi"]

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df[["close"]].copy()
        out["rsi"] = rsi(df["close"], p.window)
        prev = out["rsi"].shift(1)
        warm = pd.Series(range(len(df)), index=df.index) < (self.warmup_periods - 1)
        buy = ((prev < p.oversold) & (out["rsi"] >= p.oversold)).fillna(False) & ~warm
        sell = ((prev > p.overbought) & (out["rsi"] <= p.overbought)).fillna(False) & ~warm

        actions = pd.Series(Action.HOLD.value, index=df.index, dtype="object")
        actions[buy] = Action.BUY.value
        actions[sell] = Action.SELL.value
        out[ACTION_COLUMN] = actions
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"RSI 과매도({p.oversold:g}) 탈출"),
            (sell, f"RSI 과매수({p.overbought:g}) 이탈"),
        )
        return out
