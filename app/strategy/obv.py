"""OBV 거래량 추세 전략 (OBVStrategy) — 필터 계열.

- 매수: OBV > OBV 이동평균(``obv_window``) **이고** 종가 > 종가 이동평균(``price_window``) 이 새로 성립한 캔들
  (거래량이 매수 쪽으로 쌓이는 중에 가격도 오름세. ``price_window=0`` 이면 가격 조건 없이 OBV 만 본다)
- 매도: OBV 가 자기 이동평균을 위에서 아래로 돌파 (거래량 흐름이 하락으로 전환)

OBV 절대값은 데이터 시작점에 따라 평행 이동하므로(실시간 엔진은 최근 캔들만 보관) OBV 와 그 이동평균의
비교만 쓴다 — 평행 이동해도 둘의 차이는 그대로라 백테스트와 실거래 신호가 같다.
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
from app.strategy.indicators import cross_below, obv, rising_edge, sma


class OBVParams(StrategyParams):
    obv_window: int = param(
        20, ge=2, le=500, label="OBV 이동평균 기간", description="OBV 가 이 평균 위면 매수 쪽 거래량 우세",
        help_text="OBV(오른 캔들 거래량은 더하고 내린 캔들 거래량은 뺀 누적값)의 이동평균 기간입니다.\n"
                  "OBV 가 평균 위로 올라서면 매수 쪽 거래량이 쌓이는 중, 아래로 내려가면 매도 신호입니다. 보통 20.",
    )
    price_window: int = param(
        20, ge=0, le=500, label="가격 이동평균 기간", description="종가 > 이 기간 이동평균일 때만 매수 (0 = 끔)",
        help_text="매수할 때 종가가 이 기간 단순이동평균보다 높아야(가격도 오름세) 합니다.\n"
                  "0 이면 가격 조건 없이 OBV 만 봅니다. 매도에는 적용되지 않습니다.",
    )


class OBVStrategy(Strategy):
    name: ClassVar[str] = "obv"
    description: ClassVar[str] = "OBV 거래량 추세"
    family: ClassVar[StrategyFamily] = StrategyFamily.FILTER
    rules: ClassVar[str] = (
        "매수: OBV > OBV 이동평균 이고 종가 > 가격 이동평균 이 새로 성립 (가격 기간 0 이면 OBV 만)\n"
        "매도: OBV 가 이동평균 아래로 하향 돌파"
    )
    Params: ClassVar[type[StrategyParams]] = OBVParams
    params: OBVParams

    @property
    def warmup_periods(self) -> int:
        p = self.params
        return max(p.obv_window, p.price_window) + 1

    @property
    def indicator_columns(self) -> list[str]:
        cols = ["obv", "obv_ma"]
        if self.params.price_window:
            cols.append("price_ma")
        return cols

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        out = df[["close"]].copy()
        out["obv"] = obv(close, df["volume"])
        out["obv_ma"] = sma(out["obv"], p.obv_window)
        obv_up = (out["obv"] > out["obv_ma"]).fillna(False)
        price_ok = pd.Series(True, index=df.index)
        if p.price_window:
            out["price_ma"] = sma(close, p.price_window)
            price_ok = (close > out["price_ma"]).fillna(False)

        warm = warmup_mask(df, self.warmup_periods)
        buy = rising_edge(obv_up & price_ok) & ~warm
        sell = cross_below(out["obv"], out["obv_ma"]) & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"OBV>OBV{p.obv_window}평균" + (f" + 종가>SMA{p.price_window}" if p.price_window else "")),
            (sell, f"OBV 가 OBV{p.obv_window}평균 하향 돌파"),
            (rising_edge(obv_up) & ~price_ok, f"OBV 상승이지만 종가≤SMA{p.price_window}"),
        )
        return out
