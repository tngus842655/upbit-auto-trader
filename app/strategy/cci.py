"""CCI 과매도/과매수 전략 (CCIStrategy) — 평균 회귀 계열.

- 매수: CCI 가 과매도선(``oversold``, 기본 −100) 아래에 있다가 그 선 이상으로 회복
- 매도: CCI 가 과매수선(``overbought``, 기본 +100) 위에 있다가 그 선 이하로 내려옴

선 돌파 판정은 ``rsi`` 전략과 같은 방식(직전 < 선 ≤ 현재 / 직전 > 선 ≥ 현재)이다.
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
from app.strategy.indicators import cci, level_cross_down, level_cross_up


class CCIParams(StrategyParams):
    window: int = param(
        20, ge=2, le=500, label="기간", description="CCI 계산 캔들 수",
        help_text="CCI = (전형가격 − 이동평균) / (0.015 × 평균편차), 전형가격 = (고가+저가+종가)/3. 보통 20.",
    )
    oversold: float = param(
        -100.0, ge=-500.0, le=0.0, label="과매도선", description="이 아래에서 위로 회복하면 매수",
        help_text="CCI 가 이 값 아래로 내려갔다가 다시 이 값 이상으로 올라오는 캔들에서 매수합니다. 보통 −100.",
    )
    overbought: float = param(
        100.0, ge=0.0, le=500.0, label="과매수선", description="이 위에서 아래로 내려오면 매도",
        help_text="CCI 가 이 값 위에 있다가 이 값 이하로 내려오는 캔들에서 매도합니다. 보통 +100.",
    )

    @model_validator(mode="after")
    def _check_levels(self) -> CCIParams:
        if self.oversold >= self.overbought:
            raise ValueError(f"oversold({self.oversold}) 는 overbought({self.overbought}) 보다 작아야 합니다")
        return self


class CCIStrategy(Strategy):
    name: ClassVar[str] = "cci"
    description: ClassVar[str] = "CCI 과매도/과매수"
    family: ClassVar[StrategyFamily] = StrategyFamily.MEAN_REVERSION
    rules: ClassVar[str] = (
        "매수: CCI 가 과매도선(−100) 아래에서 위로 회복\n"
        "매도: CCI 가 과매수선(+100) 위에서 아래로 이탈"
    )
    Params: ClassVar[type[StrategyParams]] = CCIParams
    params: CCIParams

    @property
    def warmup_periods(self) -> int:
        return self.params.window + 1

    @property
    def indicator_columns(self) -> list[str]:
        return ["cci"]

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df[["close"]].copy()
        out["cci"] = cci(df["high"], df["low"], df["close"], p.window)
        warm = warmup_mask(df, self.warmup_periods)
        buy = level_cross_up(out["cci"], p.oversold) & ~warm
        sell = level_cross_down(out["cci"], p.overbought) & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"CCI 과매도({p.oversold:g}) 회복"),
            (sell, f"CCI 과매수({p.overbought:g}) 이탈"),
        )
        return out
