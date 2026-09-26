"""Williams %R 과매도/과매수 전략 (WilliamsRStrategy) — 평균 회귀 계열.

%R = −100 × (최근 ``window`` 개 최고가 − 종가) / (최고가 − 최저가), −100(최저) ~ 0(최고).

- 매수: %R 이 과매도선(``oversold``, 기본 −80) 아래에 있다가 그 선 이상으로 회복
- 매도: %R 이 과매수선(``overbought``, 기본 −20) 위에 있다가 그 선 이하로 내려옴
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
from app.strategy.indicators import level_cross_down, level_cross_up, williams_r


class WilliamsRParams(StrategyParams):
    window: int = param(
        14, ge=2, le=500, label="기간", description="최근 고가·저가 범위 캔들 수",
        help_text="%R = 최근 N개 캔들 고가~저가 범위에서 종가의 위치를 −100(최저)~0(최고)으로 나타냅니다. 보통 14.",
    )
    oversold: float = param(
        -80.0, ge=-99.0, le=-1.0, label="과매도선", description="이 아래에서 위로 회복하면 매수",
        help_text="%R 이 이 값 아래로 내려갔다가 다시 이 값 이상으로 올라오는 캔들에서 매수합니다. 보통 −80.",
    )
    overbought: float = param(
        -20.0, ge=-99.0, le=-1.0, label="과매수선", description="이 위에서 아래로 내려오면 매도",
        help_text="%R 이 이 값 위에 있다가 이 값 이하로 내려오는 캔들에서 매도합니다. 보통 −20.",
    )

    @model_validator(mode="after")
    def _check_levels(self) -> WilliamsRParams:
        if self.oversold >= self.overbought:
            raise ValueError(f"oversold({self.oversold}) 는 overbought({self.overbought}) 보다 작아야 합니다")
        return self


class WilliamsRStrategy(Strategy):
    name: ClassVar[str] = "williams_r"
    description: ClassVar[str] = "Williams %R 과매도/과매수"
    family: ClassVar[StrategyFamily] = StrategyFamily.MEAN_REVERSION
    rules: ClassVar[str] = (
        "매수: %R 이 과매도선(−80) 아래에서 위로 회복\n"
        "매도: %R 이 과매수선(−20) 위에서 아래로 이탈"
    )
    Params: ClassVar[type[StrategyParams]] = WilliamsRParams
    params: WilliamsRParams

    @property
    def warmup_periods(self) -> int:
        return self.params.window + 1

    @property
    def indicator_columns(self) -> list[str]:
        return ["williams_r"]

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df[["close"]].copy()
        out["williams_r"] = williams_r(df["high"], df["low"], df["close"], p.window)
        warm = warmup_mask(df, self.warmup_periods)
        buy = level_cross_up(out["williams_r"], p.oversold) & ~warm
        sell = level_cross_down(out["williams_r"], p.overbought) & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"%R 과매도({p.oversold:g}) 회복"),
            (sell, f"%R 과매수({p.overbought:g}) 이탈"),
        )
        return out
