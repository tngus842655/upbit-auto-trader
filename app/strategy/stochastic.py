"""스토캐스틱 과매도/과매수 전략 (StochasticStrategy) — 평균 회귀 계열.

- 매수: 직전 캔들에 %K·%D 가 모두 과매도선(``oversold``) 아래에 있다가 %K 가 %D 를 상향 돌파
- 매도: 직전 캔들에 %K·%D 가 모두 과매수선(``overbought``) 위에 있다가 %K 가 %D 를 하향 돌파

교차 직전 캔들을 보는 이유: 교차 캔들에서 %K 가 크게 튀면 %D 도 끌려 올라가 구간 밖에서 교차한 것처럼 보이지만,
실제로는 과매도 구간에서 출발한 반등이기 때문이다. %K 가 %D 이하였다가 올라간 것이므로 "직전 %D < 과매도선" 이면
직전 %K 도 과매도선 아래다.
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
from app.strategy.indicators import cross_above, cross_below, stochastic


class StochasticParams(StrategyParams):
    k_period: int = param(
        14, ge=2, le=200, label="%K 기간", description="최근 고가·저가 범위 캔들 수",
        help_text="%K = 최근 N개 캔들의 고가~저가 범위에서 종가의 위치(0~100)입니다. 보통 14.",
    )
    d_period: int = param(
        3, ge=2, le=50, label="%D 기간", description="%K 의 이동평균",
        help_text="%D = %K 의 이동평균(신호선)입니다. %K 가 %D 를 뚫는 캔들에서 신호가 납니다. 보통 3.",
    )
    smooth_k: int = param(
        1, ge=1, le=20, label="%K 평활", description="1 = Fast(기본), 3 = Slow 스토캐스틱",
        help_text="%K 를 이 기간 이동평균으로 한 번 더 부드럽게 합니다.\n"
                  "1 이면 평활 없음(Fast), 3 이면 Slow 스토캐스틱.\n"
                  "키우면 잔파도에 덜 반응합니다.",
    )
    oversold: float = param(
        20.0, ge=1.0, le=99.0, label="과매도선", description="이 아래에서 상향 돌파하면 매수",
        help_text="직전 캔들에 %K·%D 가 모두 이 값 아래(과매도)에 있다가 %K 가 %D 를 위로 뚫으면 매수합니다. 보통 20.",
    )
    overbought: float = param(
        80.0, ge=1.0, le=99.0, label="과매수선", description="이 위에서 하향 돌파하면 매도",
        help_text="직전 캔들에 %K·%D 가 모두 이 값 위(과매수)에 있다가 %K 가 %D 를 아래로 뚫으면 매도합니다. 보통 80.",
    )

    @model_validator(mode="after")
    def _check_levels(self) -> StochasticParams:
        if self.oversold >= self.overbought:
            raise ValueError(f"oversold({self.oversold}) 는 overbought({self.overbought}) 보다 작아야 합니다")
        return self


class StochasticStrategy(Strategy):
    name: ClassVar[str] = "stochastic"
    description: ClassVar[str] = "Stochastic 과매도/과매수"
    family: ClassVar[StrategyFamily] = StrategyFamily.MEAN_REVERSION
    rules: ClassVar[str] = (
        "매수: 과매도 구간(직전 %K·%D < 과매도선)에서 %K 가 %D 를 상향 돌파\n"
        "매도: 과매수 구간(직전 %K·%D > 과매수선)에서 %K 가 %D 를 하향 돌파"
    )
    Params: ClassVar[type[StrategyParams]] = StochasticParams
    params: StochasticParams

    @property
    def warmup_periods(self) -> int:
        # %K(k_period) → 평활(smooth_k) → %D(d_period) 가 차례로 캔들을 소모하고, 교차 판정에 직전 캔들이 필요
        p = self.params
        return p.k_period + p.smooth_k + p.d_period - 1

    @property
    def indicator_columns(self) -> list[str]:
        return ["stoch_k", "stoch_d"]

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df[["close"]].copy()
        stoch = stochastic(df["high"], df["low"], df["close"], p.k_period, p.d_period, p.smooth_k)
        out["stoch_k"] = stoch["k"]
        out["stoch_d"] = stoch["d"]
        up = cross_above(out["stoch_k"], out["stoch_d"])
        down = cross_below(out["stoch_k"], out["stoch_d"])
        prev_d = out["stoch_d"].shift(1)
        in_oversold = (prev_d < p.oversold).fillna(False)
        in_overbought = (prev_d > p.overbought).fillna(False)

        warm = warmup_mask(df, self.warmup_periods)
        buy = up & in_oversold & ~warm
        sell = down & in_overbought & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"과매도(<{p.oversold:g})에서 %K 가 %D 상향 돌파"),
            (sell, f"과매수(>{p.overbought:g})에서 %K 가 %D 하향 돌파"),
        )
        return out
