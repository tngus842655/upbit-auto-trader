"""첫 번째 전략: 이동평균 교차 (MovingAverageCrossStrategy).

- 단기 SMA 가 장기 SMA 를 상향 돌파(골든크로스) → BUY, 하향 돌파(데드크로스) → SELL, 그 외 HOLD.
- 선택 필터(매수에만 적용):
  * 거래량: 현재 캔들 거래량 ≥ ``volume_factor`` × 최근 ``volume_window`` 평균 거래량
  * RSI: RSI(``rsi_window``) ≤ ``rsi_max_for_buy`` (과매수 구간 매수 회피)

이 전략은 시스템 전체(데이터 → 신호 → 리스크 → 주문)가 동작하는지 검증하기 위한 기준 전략이며
수익성을 가정하지 않는다. 파라미터를 과거 데이터에 맞춰 과도하게 조정(과최적화)하지 말 것.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd
from pydantic import Field, model_validator

from app.strategy.base import ACTION_COLUMN, REASON_COLUMN, Action, Strategy, StrategyParams, reasons
from app.strategy.indicators import cross_above, cross_below, rsi, sma


class MovingAverageCrossParams(StrategyParams):
    short_window: int = Field(default=20, ge=2, le=500, description="단기 이동평균 기간")
    long_window: int = Field(default=60, ge=3, le=1000, description="장기 이동평균 기간")
    volume_window: int = Field(default=20, ge=0, le=500, description="거래량 평균 기간 (0 = 필터 끔)")
    volume_factor: float = Field(default=1.0, ge=0.0, le=10.0, description="평균 거래량 대비 최소 배수")
    rsi_window: int = Field(default=14, ge=0, le=200, description="RSI 기간 (0 = 필터 끔)")
    rsi_max_for_buy: float = Field(default=70.0, ge=0.0, le=100.0, description="이 값보다 RSI 가 높으면 매수 안 함")

    @model_validator(mode="after")
    def _check_windows(self) -> MovingAverageCrossParams:
        if self.short_window >= self.long_window:
            raise ValueError(f"short_window({self.short_window}) 는 long_window({self.long_window}) 보다 작아야 합니다")
        return self


class MovingAverageCrossStrategy(Strategy):
    name: ClassVar[str] = "ma_cross"
    description: ClassVar[str] = "단기/장기 SMA 교차 + 거래량·RSI 필터"
    Params: ClassVar[type[StrategyParams]] = MovingAverageCrossParams
    params: MovingAverageCrossParams

    @property
    def warmup_periods(self) -> int:
        p = self.params
        return max(p.long_window, p.volume_window, p.rsi_window + 1 if p.rsi_window else 0) + 1

    @property
    def indicator_columns(self) -> list[str]:
        cols = ["sma_short", "sma_long"]
        if self.params.volume_window:
            cols.append("volume_ratio")
        if self.params.rsi_window:
            cols.append("rsi")
        return cols

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        out = df[["close"]].copy()
        out["sma_short"] = sma(close, p.short_window)
        out["sma_long"] = sma(close, p.long_window)
        golden = cross_above(out["sma_short"], out["sma_long"])
        dead = cross_below(out["sma_short"], out["sma_long"])

        volume_ok = pd.Series(True, index=df.index)
        if p.volume_window:
            out["volume_ratio"] = df["volume"] / sma(df["volume"], p.volume_window)
            volume_ok = (out["volume_ratio"] >= p.volume_factor).fillna(False)
        rsi_ok = pd.Series(True, index=df.index)
        if p.rsi_window:
            out["rsi"] = rsi(close, p.rsi_window)
            rsi_ok = (out["rsi"] <= p.rsi_max_for_buy).fillna(False)

        warm = pd.Series(range(len(df)), index=df.index) < (self.warmup_periods - 1)
        buy = golden & volume_ok & rsi_ok & ~warm
        sell = dead & ~warm

        actions = pd.Series(Action.HOLD.value, index=df.index, dtype="object")
        actions[buy] = Action.BUY.value
        actions[sell] = Action.SELL.value
        out[ACTION_COLUMN] = actions
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"골든크로스 SMA{p.short_window}>SMA{p.long_window}"),
            (sell, f"데드크로스 SMA{p.short_window}<SMA{p.long_window}"),
            (golden & ~volume_ok, "골든크로스이지만 거래량 부족"),
            (golden & volume_ok & ~rsi_ok, f"골든크로스이지만 RSI>{p.rsi_max_for_buy:g}"),
        )
        return out
