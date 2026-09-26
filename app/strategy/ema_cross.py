"""EMA 추세 교차 전략 (EMACrossStrategy) — 추세 추종 계열. SMA 교차(``ma_cross``)와 별개의 전략이다.

- 매수: EMA(단기) > EMA(중기) > EMA(장기) 정배열이 **새로 완성된** 캔들 (RSI 필터를 켜면 RSI > 기준값도 함께)
- 매도: EMA(단기)가 EMA(중기)를 위에서 아래로 돌파

정배열은 상태라서 그대로 쓰면 추세 내내 매수 신호가 반복된다. 조건이 거짓 → 참으로 바뀐 캔들에서만 신호를 낸다.
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
from app.strategy.indicators import cross_below, ema, rising_edge, rsi


class EMACrossParams(StrategyParams):
    short_period: int = param(
        9, ge=2, le=200, label="단기 EMA", description="캔들 수",
        help_text="가장 빠른 지수이동평균 기간입니다. 이 선이 중기선 아래로 내려가면 매도합니다. 보통 9.",
    )
    medium_period: int = param(
        21, ge=3, le=300, label="중기 EMA", description="캔들 수",
        help_text="중간 지수이동평균 기간입니다. 단기보다 크고 장기보다 작아야 합니다. 보통 21.",
    )
    long_period: int = param(
        50, ge=4, le=500, label="장기 EMA", description="캔들 수",
        help_text="가장 느린 지수이동평균 기간입니다. 단기 > 중기 > 장기 정배열이 완성되면 매수합니다. 보통 50.\n"
                  "이 길이만큼 캔들이 쌓여야 첫 신호가 납니다.",
    )
    rsi_filter: bool = param(
        True, label="RSI 필터", description="RSI > 기준값일 때만 매수",
        help_text="켜면 정배열이라도 RSI 가 기준값보다 높을 때(상승 탄력)만 매수합니다. 매도에는 적용되지 않습니다.",
    )
    rsi_window: int = param(
        14, ge=2, le=200, label="RSI 기간", depends_on="rsi_filter",
        help_text="RSI 필터의 계산 기간(캔들 수)입니다. RSI 필터를 끄면 무시됩니다.",
    )
    rsi_threshold: float = param(
        50.0, ge=1.0, le=99.0, label="RSI 기준값", depends_on="rsi_filter",
        help_text="매수하려면 RSI 가 이 값보다 커야 합니다. 50 = 상승 탄력이 하락보다 강한 구간.",
    )

    @model_validator(mode="after")
    def _check_periods(self) -> EMACrossParams:
        if not self.short_period < self.medium_period < self.long_period:
            raise ValueError(
                f"short_period({self.short_period}) < medium_period({self.medium_period}) "
                f"< long_period({self.long_period}) 이어야 합니다"
            )
        return self


class EMACrossStrategy(Strategy):
    name: ClassVar[str] = "ema_cross"
    description: ClassVar[str] = "EMA 추세 교차"
    family: ClassVar[StrategyFamily] = StrategyFamily.TREND
    rules: ClassVar[str] = (
        "매수: EMA 단기 > 중기 > 장기 정배열이 새로 완성될 때 (RSI 필터를 켜면 RSI > 기준값도 함께)\n"
        "매도: EMA 단기가 중기를 하향 돌파"
    )
    Params: ClassVar[type[StrategyParams]] = EMACrossParams
    params: EMACrossParams

    @property
    def warmup_periods(self) -> int:
        p = self.params
        return max(p.long_period, p.rsi_window + 1 if p.rsi_filter else 0) + 1

    @property
    def indicator_columns(self) -> list[str]:
        cols = ["ema_short", "ema_medium", "ema_long"]
        if self.params.rsi_filter:
            cols.append("rsi")
        return cols

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        out = df[["close"]].copy()
        out["ema_short"] = ema(close, p.short_period)
        out["ema_medium"] = ema(close, p.medium_period)
        out["ema_long"] = ema(close, p.long_period)
        aligned = ((out["ema_short"] > out["ema_medium"]) & (out["ema_medium"] > out["ema_long"])).fillna(False)
        rsi_ok = pd.Series(True, index=df.index)
        if p.rsi_filter:
            out["rsi"] = rsi(close, p.rsi_window)
            rsi_ok = (out["rsi"] > p.rsi_threshold).fillna(False)

        warm = warmup_mask(df, self.warmup_periods)
        buy = rising_edge(aligned & rsi_ok) & ~warm
        sell = cross_below(out["ema_short"], out["ema_medium"]) & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        order = f"EMA{p.short_period}>EMA{p.medium_period}>EMA{p.long_period}"
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"{order} 정배열" + (f" + RSI>{p.rsi_threshold:g}" if p.rsi_filter else "")),
            (sell, f"EMA{p.short_period}<EMA{p.medium_period} 하향 돌파"),
            (rising_edge(aligned) & ~rsi_ok, f"정배열이지만 RSI≤{p.rsi_threshold:g}"),
        )
        return out
