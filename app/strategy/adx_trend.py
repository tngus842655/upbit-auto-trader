"""ADX 추세 강도 전략 (ADXTrendStrategy) — 필터 계열.

ADX 는 방향이 아니라 **추세의 강도**다. 방향은 +DI / −DI 로 본다.

- 매수: ADX > ``adx_threshold`` (추세가 충분히 강함) **이고** +DI > −DI (상승 쪽) 가 새로 성립한 캔들
- 매도: −DI 가 +DI 를 아래에서 위로 돌파 (하락 쪽으로 전환)

손절·익절·추적 손절 같은 다른 청산은 리스크 관리가 함께 적용한다. 추세 강도 판정(``trend_strength``)은
다른 전략과 조합할 수 있도록 따로 떼어 두었다 — 향후 Strategy Router 가 "추세장/횡보장" 판단에 재사용한다.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

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
from app.strategy.indicators import adx, cross_above, rising_edge


class ADXTrendParams(StrategyParams):
    adx_window: int = param(
        14, ge=2, le=200, label="ADX 기간", description="±DI·ADX 평활 캔들 수",
        help_text="+DI·−DI·ADX 를 계산하는 Wilder 평활 기간입니다. 보통 14.\n"
                  "ADX 는 이 기간의 2배만큼 캔들이 쌓여야 나옵니다.",
    )
    adx_threshold: float = param(
        25.0, ge=1.0, le=100.0, label="ADX 기준값", description="ADX 가 이 값보다 커야 추세로 본다",
        help_text="ADX 가 이 값을 넘으면 '추세가 강하다'고 봅니다. 보통 20~25.\n"
                  "올리면 강한 추세에서만 들어가 거래가 줄고, 내리면 약한 추세에도 들어갑니다.",
    )


def trend_strength(df: pd.DataFrame, window: int, threshold: float) -> pd.DataFrame:
    """ADX·±DI 와 판정 열: ``strong``(ADX > 기준값), ``bullish``(+DI > −DI). 다른 전략의 필터로도 쓴다."""
    out = adx(df["high"], df["low"], df["close"], window)
    out["strong"] = (out["adx"] > threshold).fillna(False)
    out["bullish"] = (out["plus_di"] > out["minus_di"]).fillna(False)
    return out


class ADXTrendStrategy(Strategy):
    name: ClassVar[str] = "adx_trend"
    description: ClassVar[str] = "ADX 추세 강도"
    family: ClassVar[StrategyFamily] = StrategyFamily.FILTER
    rules: ClassVar[str] = (
        "매수: ADX > 기준값 (추세 강함) 이고 +DI > −DI (상승 방향) 가 새로 성립\n"
        "매도: −DI 가 +DI 를 상향 돌파 (하락 방향으로 전환)"
    )
    Params: ClassVar[type[StrategyParams]] = ADXTrendParams
    params: ADXTrendParams

    @property
    def warmup_periods(self) -> int:
        # ADX 는 2×window 번째 캔들부터 나오고, 새로 성립했는지 보려면 직전 캔들이 하나 더 필요
        return 2 * self.params.adx_window + 1

    @property
    def indicator_columns(self) -> list[str]:
        return ["adx", "plus_di", "minus_di"]

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df[["close"]].copy()
        trend = trend_strength(df, p.adx_window, p.adx_threshold)
        out["adx"] = trend["adx"]
        out["plus_di"] = trend["plus_di"]
        out["minus_di"] = trend["minus_di"]

        warm = warmup_mask(df, self.warmup_periods)
        buy = rising_edge(trend["strong"] & trend["bullish"]) & ~warm
        sell = cross_above(out["minus_di"], out["plus_di"]) & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"ADX>{p.adx_threshold:g} 추세 강함 + +DI>−DI"),
            (sell, "−DI 가 +DI 상향 돌파 (하락 전환)"),
            (rising_edge(trend["bullish"]) & ~trend["strong"], f"+DI>−DI 이지만 ADX≤{p.adx_threshold:g} (추세 약함)"),
        )
        return out
