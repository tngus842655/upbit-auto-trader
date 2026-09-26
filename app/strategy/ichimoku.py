"""일목균형표 구름 돌파 전략 (IchimokuStrategy) — 추세 추종 계열.

- 매수: 종가 > 구름 상단 **이고** 전환선 > 기준선 이 새로 성립한 캔들
- 매도: 종가 < 구름 하단 **또는** 전환선 < 기준선 이 새로 성립한 캔들

구름(선행스팬 A·B)은 ``displacement`` 캔들 **전에** 계산된 값을 현재 위치에 놓은 것이다
(``indicators.ichimoku`` 가 ``shift(+displacement)`` 로 만든다). 차트는 구름을 미래 쪽으로 그리지만, 전략이 그 미래
구름을 보면 아직 오지 않은 캔들을 쓰는 셈이라 Look-ahead 가 된다. 후행스팬도 같은 이유로 쓰지 않는다.
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
from app.strategy.indicators import ichimoku, rising_edge


class IchimokuParams(StrategyParams):
    conversion_period: int = param(
        9, ge=2, le=100, label="전환선 기간", description="최근 N개 고가·저가 중간값",
        help_text="전환선 = 최근 N개 캔들 (최고가 + 최저가) / 2. 빠른 선입니다. 기준선 기간보다 작아야 합니다. 보통 9.",
    )
    base_period: int = param(
        26, ge=3, le=200, label="기준선 기간", description="최근 N개 고가·저가 중간값",
        help_text="기준선 = 최근 N개 캔들 (최고가 + 최저가) / 2. 전환선이 기준선 위에 있으면 상승 쪽입니다. 보통 26.",
    )
    span_b_period: int = param(
        52, ge=3, le=300, label="선행스팬 B 기간", description="구름 한쪽 경계의 중간값 기간",
        help_text="선행스팬 B = 최근 N개 캔들 (최고가 + 최저가) / 2. 선행스팬 A = (전환선 + 기준선) / 2.\n"
                  "두 선 사이가 구름입니다. 기준선 기간 이상이어야 합니다. 보통 52.",
    )
    displacement: int = param(
        26, ge=1, le=100, label="구름 시차", description="구름을 몇 캔들 뒤에 놓는지",
        help_text="선행스팬은 계산한 캔들보다 이만큼 뒤(미래)에 그려집니다.\n"
                  "즉 지금 캔들의 구름은 이만큼 전에 계산된 값입니다.\n"
                  "보통 26. 첫 신호까지 선행스팬 B 기간 + 이 값만큼 캔들이 필요합니다.",
    )

    @model_validator(mode="after")
    def _check_periods(self) -> IchimokuParams:
        if not self.conversion_period < self.base_period <= self.span_b_period:
            raise ValueError(
                f"conversion_period({self.conversion_period}) < base_period({self.base_period}) "
                f"<= span_b_period({self.span_b_period}) 이어야 합니다"
            )
        return self


class IchimokuStrategy(Strategy):
    name: ClassVar[str] = "ichimoku"
    description: ClassVar[str] = "Ichimoku 구름 돌파"
    family: ClassVar[StrategyFamily] = StrategyFamily.TREND
    rules: ClassVar[str] = (
        "매수: 종가 > 구름 상단 이고 전환선 > 기준선 이 새로 성립\n"
        "매도: 종가 < 구름 하단 또는 전환선 < 기준선 이 새로 성립\n"
        "구름은 시차만큼 전에 계산된 값(미래 구름·후행스팬은 쓰지 않음)"
    )
    Params: ClassVar[type[StrategyParams]] = IchimokuParams
    params: IchimokuParams

    @property
    def warmup_periods(self) -> int:
        # 선행스팬 B 는 span_b 번째 캔들에 계산되어 displacement 뒤에 현재 위치에 온다. 새로 성립 판정에 직전 캔들 +1
        p = self.params
        return max(p.conversion_period, p.base_period, p.span_b_period) + p.displacement + 1

    @property
    def indicator_columns(self) -> list[str]:
        return ["tenkan", "kijun", "span_a", "span_b"]

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        out = df[["close"]].copy()
        lines = ichimoku(df["high"], df["low"], p.conversion_period, p.base_period, p.span_b_period, p.displacement)
        for col in self.indicator_columns:
            out[col] = lines[col]
        spans = out[["span_a", "span_b"]]
        cloud_top = spans.max(axis=1, skipna=False)
        cloud_bottom = spans.min(axis=1, skipna=False)
        above = (close > cloud_top).fillna(False)
        below = (close < cloud_bottom).fillna(False)
        tk_up = (out["tenkan"] > out["kijun"]).fillna(False)
        tk_down = (out["tenkan"] < out["kijun"]).fillna(False)

        warm = warmup_mask(df, self.warmup_periods)
        buy = rising_edge(above & tk_up) & ~warm
        sell = rising_edge(below | tk_down) & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, "구름 위 + 전환선>기준선"),
            (sell & below & tk_down, "구름 아래로 이탈 + 전환선<기준선"),
            (sell & below, "구름 아래로 이탈"),
            (sell, "전환선<기준선"),
            (rising_edge(above) & ~tk_up, "구름 위이지만 전환선≤기준선"),
        )
        return out
