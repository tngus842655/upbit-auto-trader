"""볼린저 밴드 반전 전략 (BollingerStrategy) — 평균 회귀 계열.

- 매수: 직전 종가 ≤ 하단 밴드 → 현재 종가 > 하단 밴드 (밴드 밖으로 빠졌다가 안으로 재진입)
- 매도: 직전 종가 ≥ 상단 밴드 → 현재 종가 < 상단 밴드 (상단 이탈 후 하락)
- 선택 RSI 필터(기본 켬): 매수는 RSI < ``oversold``, 매도는 RSI > ``overbought`` 일 때만

밴드는 SMA(``window``) ± ``std_dev`` × 표준편차(모집단). 종가는 닫힌 캔들 기준이라 캔들 중간의 꼬리는 보지 않는다.
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
from app.strategy.indicators import bollinger, cross_above, cross_below, rsi


class BollingerParams(StrategyParams):
    window: int = param(
        20, ge=2, le=500, label="기간", description="이동평균·표준편차 캔들 수",
        help_text="밴드 중심선(SMA)과 표준편차를 계산하는 캔들 수입니다. 보통 20.\n"
                  "이 길이만큼 캔들이 쌓여야 첫 신호가 납니다.",
    )
    std_dev: float = param(
        2.0, ge=0.5, le=5.0, label="표준편차 배수", description="밴드 폭 = 중심선 ± 배수 × 표준편차",
        help_text="밴드를 중심선에서 표준편차의 몇 배만큼 벌릴지 정합니다. 보통 2.0.\n"
                  "키우면 밴드가 넓어져 이탈이 드물고 신호가 줄어듭니다.",
    )
    rsi_filter: bool = param(
        True, label="RSI 필터", description="매수 RSI<과매도선, 매도 RSI>과매수선일 때만",
        help_text="켜면 하단 재진입이라도 RSI 가 과매도선보다 낮을 때만 매수하고, "
                  "상단 이탈 후 하락이라도 RSI 가 과매수선보다 높을 때만 매도합니다.",
    )
    rsi_window: int = param(
        14, ge=2, le=200, label="RSI 기간", depends_on="rsi_filter",
        help_text="RSI 필터의 계산 기간(캔들 수)입니다. RSI 필터를 끄면 무시됩니다.",
    )
    oversold: float = param(
        40.0, ge=1.0, le=99.0, label="과매도선 (매수 RSI 상한)", depends_on="rsi_filter",
        help_text="하단 재진입 캔들의 RSI 가 이 값보다 낮을 때만 매수합니다. 과매수선보다 작아야 합니다.",
    )
    overbought: float = param(
        60.0, ge=1.0, le=99.0, label="과매수선 (매도 RSI 하한)", depends_on="rsi_filter",
        help_text="상단 이탈 후 하락한 캔들의 RSI 가 이 값보다 높을 때만 매도합니다.",
    )

    @model_validator(mode="after")
    def _check_levels(self) -> BollingerParams:
        if self.oversold >= self.overbought:
            raise ValueError(f"oversold({self.oversold}) 는 overbought({self.overbought}) 보다 작아야 합니다")
        return self


class BollingerStrategy(Strategy):
    name: ClassVar[str] = "bollinger"
    description: ClassVar[str] = "Bollinger Band 반전"
    family: ClassVar[StrategyFamily] = StrategyFamily.MEAN_REVERSION
    rules: ClassVar[str] = (
        "매수: 직전 종가 ≤ 하단 밴드였다가 현재 종가 > 하단 밴드 (밴드 안으로 재진입)\n"
        "매도: 직전 종가 ≥ 상단 밴드였다가 현재 종가 < 상단 밴드 (상단 이탈 후 하락)\n"
        "RSI 필터: 켜면 매수는 RSI < 과매도선, 매도는 RSI > 과매수선일 때만"
    )
    Params: ClassVar[type[StrategyParams]] = BollingerParams
    params: BollingerParams

    @property
    def warmup_periods(self) -> int:
        p = self.params
        return max(p.window, p.rsi_window + 1 if p.rsi_filter else 0) + 1

    @property
    def indicator_columns(self) -> list[str]:
        cols = ["bb_upper", "bb_middle", "bb_lower"]
        if self.params.rsi_filter:
            cols.append("rsi")
        return cols

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        out = df[["close"]].copy()
        bands = bollinger(close, p.window, p.std_dev)
        out["bb_upper"] = bands["upper"]
        out["bb_middle"] = bands["middle"]
        out["bb_lower"] = bands["lower"]
        reentry = cross_above(close, out["bb_lower"])  # 직전 종가 <= 직전 하단, 현재 종가 > 현재 하단
        fall_back = cross_below(close, out["bb_upper"])  # 직전 종가 >= 직전 상단, 현재 종가 < 현재 상단

        rsi_buy_ok = pd.Series(True, index=df.index)
        rsi_sell_ok = pd.Series(True, index=df.index)
        if p.rsi_filter:
            out["rsi"] = rsi(close, p.rsi_window)
            rsi_buy_ok = (out["rsi"] < p.oversold).fillna(False)
            rsi_sell_ok = (out["rsi"] > p.overbought).fillna(False)

        warm = warmup_mask(df, self.warmup_periods)
        buy = reentry & rsi_buy_ok & ~warm
        sell = fall_back & rsi_sell_ok & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        band = f"BB{p.window}·{p.std_dev:g}σ"
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"{band} 하단 재진입" + (f" (RSI<{p.oversold:g})" if p.rsi_filter else "")),
            (sell, f"{band} 상단 이탈 후 하락" + (f" (RSI>{p.overbought:g})" if p.rsi_filter else "")),
            (reentry & ~rsi_buy_ok, f"하단 재진입이지만 RSI≥{p.oversold:g}"),
            (fall_back & ~rsi_sell_ok, f"상단 이탈 후 하락이지만 RSI≤{p.overbought:g}"),
        )
        return out
