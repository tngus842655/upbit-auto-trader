"""연도별 최적 설정 — 조합 격자, 최고 수익률 선택(단순 보유보다 낮아도), 로그 억제, 실행기(캐시·다음 해·중단)·API."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import zlib
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api.optimizer import OptimizeRequest, OptimizeRunner, year_period
from app.api.server import create_app
from app.backtest.optimize import (
    EXIT_RULES,
    INTERVALS,
    ExitRule,
    OptimizeCancelled,
    backtest_candidate,
    param_grid,
    quiet_backtest_logs,
)
from app.backtest.optimize import optimize_year as run_optimize
from app.core.exceptions import MarketDataError
from app.database import Database
from app.exchange.models import KST, CandleInterval
from app.risk.config import RiskConfig
from app.strategy import STRATEGIES, create_strategy
from tests.test_api import FakePublicClient

THIS_YEAR = datetime.now(KST).year
COSTS = {"initial_capital": 1_000_000.0, "fee_rate": 0.0005, "slippage_rate": 0.0005}


def synthetic_frame(market: str, interval: str, start: datetime, count: int = 300, drift: float = 0.0) -> pd.DataFrame:
    """캔들 단위 간격에 맞춘 합성 캔들 (마켓·단위·연도마다 다르지만 항상 같은 값)."""
    step = CandleInterval.parse(interval).seconds
    rng = np.random.default_rng(zlib.crc32(f"{market}|{interval}|{start.year}".encode()))
    close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, count)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, count))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, count))
    first = start.astimezone(UTC)
    idx = pd.DatetimeIndex([first + timedelta(seconds=step * i) for i in range(count)], name="time")
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                       "volume": rng.uniform(1, 5, count)}, index=idx)
    df["value"] = df["close"] * df["volume"]
    df.attrs["market"] = market
    df.attrs["interval"] = interval
    return df


class FakeLoader:
    """호출을 세는 가짜 캔들 로더. ``missing`` 마켓은 캔들이 없다고 답한다."""

    def __init__(self, missing: set[str] | None = None, drift: float = 0.0) -> None:
        self.calls: list[tuple[str, str, datetime, datetime | None]] = []
        self.missing = missing or set()
        self.drift = drift

    async def __call__(self, settings, market, interval, start, end=None, **kwargs):
        self.calls.append((market, CandleInterval.parse(interval).value, start, end))
        if market in self.missing:
            raise MarketDataError(f"{market} 구간에 캔들이 없습니다")
        return synthetic_frame(market, CandleInterval.parse(interval).value, start, drift=self.drift)


def frames_for(markets: list[str], intervals: tuple[str, ...] = ("60m", "1d"), drift: float = 0.0):
    start = datetime(2024, 1, 1, tzinfo=KST)
    return {iv: {m: synthetic_frame(m, iv, start, drift=drift) for m in markets} for iv in intervals}


# ---------------------------------------------------------------------------
# 격자·청산 규칙
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(STRATEGIES))
def test_param_grids_are_valid_and_start_with_defaults(name: str) -> None:
    grid = param_grid(name)
    assert grid[0] == create_strategy(name).params.model_dump(mode="json")  # 기본값이 후보에 들어 있다
    assert len(grid) >= 4 and len({json.dumps(g, sort_keys=True) for g in grid}) == len(grid)
    for params in grid:
        assert create_strategy(name, params).params.model_dump(mode="json") == params  # 검증 통과·정규형


def test_exit_rules_only_replace_exit_fields() -> None:
    base = RiskConfig(position_fraction=0.5, daily_loss_limit_pct=0.02, stop_loss_pct=0.05, take_profit_pct=0.2)
    applied = ExitRule("추적", trailing_stop_pct=0.1).apply(base)
    assert (applied.stop_loss_pct, applied.take_profit_pct, applied.trailing_stop_pct) == (None, None, 0.1)
    assert applied.position_fraction == 0.5 and applied.daily_loss_limit_pct == 0.02  # 나머지는 사용자 설정 그대로
    assert len({r.label for r in EXIT_RULES}) == len(EXIT_RULES) == 8
    for rule in EXIT_RULES:
        rule.apply(RiskConfig())  # 모두 유효한 리스크 설정


# ---------------------------------------------------------------------------
# 최고 조합 선택
# ---------------------------------------------------------------------------
def test_optimize_year_picks_highest_average_return() -> None:
    frames = frames_for(["KRW-AAA", "KRW-BBB"])
    grid = param_grid("macd")[:3]
    exits = EXIT_RULES[:3]
    outcome = run_optimize(frames, "macd", RiskConfig.unrestricted(), grid=grid, exits=exits, **COSTS)
    assert outcome.evaluated == len(frames) * len(grid) * len(exits)
    # 모든 후보를 직접 돌려 본 최고 평균 수익률과 같다
    def average(by_market, params, rule, interval) -> float:
        results = backtest_candidate(by_market, "macd", params, rule.apply(RiskConfig.unrestricted()),
                                     interval=interval, **COSTS)
        return sum(r.total_return for r in results) / len(results)

    best = max(average(by_market, params, rule, iv)
               for iv, by_market in frames.items() for params in grid for rule in exits)
    assert outcome.best.avg_return == pytest.approx(best)
    returns = [c.avg_return for c in outcome.top]
    assert returns == sorted(returns, reverse=True) and outcome.top[0] is outcome.best
    d = outcome.best.to_dict()
    assert {m["market"] for m in d["markets"]} == {"KRW-AAA", "KRW-BBB"} and d["interval"] in frames
    assert d["avg_return"] == pytest.approx(sum(m["total_return"] for m in d["markets"]) / 2)
    json.dumps(d, allow_nan=False)


def test_best_is_chosen_even_when_everything_trails_buy_and_hold() -> None:
    # 거의 쉬지 않고 오르는 시장 — 어떤 매매 조합도 처음부터 끝까지 들고 있는 것보다 못하다
    frames = frames_for(["KRW-UP"], drift=0.02)
    outcome = run_optimize(frames, "rsi", RiskConfig.unrestricted(), grid=param_grid("rsi")[:3], exits=EXIT_RULES[:2],
                           **COSTS)
    assert outcome.best.to_dict()["beats_benchmark"] is False
    assert outcome.best.avg_return == max(c.avg_return for c in outcome.top)  # 그래도 그중 최고를 고른다
    assert outcome.best.avg_benchmark > 1.0


def test_progress_and_cancel() -> None:
    frames = frames_for(["KRW-AAA"])
    seen: list[int] = []
    run_optimize(frames, "cci", RiskConfig(), grid=param_grid("cci")[:2], exits=EXIT_RULES[:2],
                 on_progress=seen.append, **COSTS)
    assert seen == list(range(1, 2 * 2 * 2 + 1))  # 캔들 단위 2 × 파라미터 2 × 청산 규칙 2 × 마켓 1
    with pytest.raises(OptimizeCancelled):
        run_optimize(frames, "cci", RiskConfig(), should_stop=lambda: True, **COSTS)
    with pytest.raises(ValueError, match="캔들"):
        run_optimize({"60m": {}}, "cci", RiskConfig(), **COSTS)


def test_quiet_logs_drop_only_this_threads_backtest_logs(caplog) -> None:
    risk_log = logging.getLogger("app.risk.manager")
    caplog.set_level(logging.INFO)
    with quiet_backtest_logs():
        risk_log.info("조용히")
        risk_log.warning("이것도 조용히")
        risk_log.error("오류는 남긴다")
        other = threading.Thread(target=lambda: risk_log.info("다른 스레드는 그대로"))
        other.start()
        other.join()
    risk_log.info("끝난 뒤에는 다시 남긴다")
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["오류는 남긴다", "다른 스레드는 그대로", "끝난 뒤에는 다시 남긴다"]


# ---------------------------------------------------------------------------
# 실행기 (캔들 로드 → 탐색 → 다음 해 확인 → 캐시)
# ---------------------------------------------------------------------------
def request(year: int, **overrides) -> OptimizeRequest:
    return OptimizeRequest(**{"strategy_name": "ichimoku", "year": year, "markets": ["KRW-AAA", "KRW-BBB"],
                              "risk": RiskConfig().model_dump(), **overrides})


async def test_runner_finds_best_verifies_next_year_and_caches(make_settings, tmp_path) -> None:
    loader = FakeLoader()
    fixed_now = datetime(THIS_YEAR, 6, 15, 13, 0, tzinfo=KST)
    runner = OptimizeRunner(make_settings(), loader=loader, cache_dir=tmp_path, now=lambda: fixed_now)
    year = THIS_YEAR - 2
    job = await runner.run_sync(request(year))
    assert job.status == "done" and not job.cached, job.error
    result = job.result
    assert result["year"] == year and result["markets"] == ["KRW-AAA", "KRW-BBB"] and result["skipped"] == []
    assert result["evaluated"] == len(param_grid("ichimoku")) * len(EXIT_RULES) * len(INTERVALS)
    assert result["best"]["interval"] in INTERVALS and "label" in result["best"]["exit"]
    # 그 해 구간은 백테스트 탭의 'N년' 과 같고, 캔들 단위마다 한 번씩만 받는다
    start, end = year_period(year)
    year_calls = [c for c in loader.calls if c[2] == start]
    assert len(year_calls) == len(INTERVALS) * 2 and all(c[3] == end for c in year_calls)
    # 다음 해(지난 해라 1년 전체)를 같은 설정으로 확인
    nxt = result["next_year"]
    assert nxt["year"] == year + 1 and nxt["partial"] is False and len(nxt["markets"]) == 2
    json.dumps(result, allow_nan=False)

    calls = len(loader.calls)
    again = await runner.run_sync(request(year))
    assert again.cached and again.result == result and len(loader.calls) == calls  # 저장된 결과, 캔들 조회 없음
    assert len(list(tmp_path.glob("*.json"))) == 1
    # 청산 규칙 3개는 후보가 덮어쓰므로 캐시 키에서 빠진다 / 나머지 리스크가 바뀌면 다시 계산
    assert (await runner.run_sync(request(year, risk=RiskConfig(stop_loss_pct=0.2).model_dump()))).cached
    assert not (await runner.run_sync(request(year, risk=RiskConfig(daily_loss_limit_pct=0.1).model_dump()))).cached


async def test_runner_next_year_is_this_year_until_today(make_settings, tmp_path) -> None:
    loader = FakeLoader()
    now = {"t": datetime(THIS_YEAR, 3, 10, 9, 30, tzinfo=KST)}
    runner = OptimizeRunner(make_settings(), loader=loader, cache_dir=tmp_path, now=lambda: now["t"])
    job = await runner.run_sync(request(THIS_YEAR - 1))
    nxt = job.result["next_year"]
    assert nxt["year"] == THIS_YEAR and nxt["partial"] is True
    assert datetime.fromisoformat(nxt["end"]) == datetime(THIS_YEAR, 3, 10, tzinfo=KST)  # 오늘 0시까지
    assert (await runner.run_sync(request(THIS_YEAR - 1))).cached
    now["t"] = datetime(THIS_YEAR, 3, 11, 9, 30, tzinfo=KST)  # 날짜가 바뀌면 '오늘까지' 성과를 다시 계산
    assert not (await runner.run_sync(request(THIS_YEAR - 1))).cached


async def test_runner_skips_markets_without_candles(make_settings, tmp_path) -> None:
    runner = OptimizeRunner(make_settings(), loader=FakeLoader(missing={"KRW-NEW"}), cache_dir=tmp_path)
    job = await runner.run_sync(request(THIS_YEAR - 3, markets=["KRW-NEW", "KRW-AAA"]))
    assert job.status == "done" and job.result["markets"] == ["KRW-AAA"]
    assert job.result["skipped"][0]["market"] == "KRW-NEW" and "캔들이 없습니다" in job.result["skipped"][0]["reason"]
    none = await runner.run_sync(request(THIS_YEAR - 3, markets=["KRW-NEW"]))
    assert none.status == "error" and "캔들이 있는 마켓이 없습니다" in none.error


def test_request_validation() -> None:
    assert request(THIS_YEAR - 1, markets="krw-aaa, KRW-BBB").markets == ["KRW-AAA", "KRW-BBB"]
    for year in (THIS_YEAR, 2017):  # 올해(아직 안 끝남)·업비트 이전
        with pytest.raises(ValueError, match="연도"):
            request(year)
    for bad in ({"strategy_name": "nope"}, {"markets": ["btc"]}, {"markets": [f"KRW-C{i}" for i in range(11)]},
                {"markets": []}):
        with pytest.raises(ValueError):
            request(THIS_YEAR - 1, **bad)


async def test_cancel_stops_a_running_job(make_settings, tmp_path) -> None:
    release = asyncio.Event()

    async def slow_loader(settings, market, interval, start, end=None, **kwargs):
        await release.wait()
        return synthetic_frame(market, CandleInterval.parse(interval).value, start)

    runner = OptimizeRunner(make_settings(), loader=slow_loader, cache_dir=tmp_path)
    job = runner.submit(request(THIS_YEAR - 2))
    await asyncio.sleep(0.05)
    assert job.status == "running"
    assert await runner.cancel(job.id) is True
    assert job.status == "cancelled" and job.result is None
    assert await runner.cancel(job.id) is False and await runner.cancel("nope") is False


def test_api_optimize_flow(make_settings, tmp_path) -> None:
    settings = make_settings()
    db = Database("sqlite://")
    db.create_all()
    runner = OptimizeRunner(settings, loader=FakeLoader(), cache_dir=tmp_path)
    app = create_app(settings, db=db, public_client=FakePublicClient({}), optimizer=runner)
    with TestClient(app) as client:
        bad = client.post("/api/optimize", json={"strategy_name": "macd", "year": THIS_YEAR, "markets": ["KRW-BTC"]})
        assert bad.status_code == 422 and "year" in bad.text
        r = client.post("/api/optimize", json={
            "strategy_name": "williams_r", "year": THIS_YEAR - 2, "markets": ["KRW-BTC"],
            "risk": RiskConfig().model_dump(), "fee_rate": 0.0007,
        })
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]
        deadline = time.time() + 30
        body = None
        while time.time() < deadline:
            body = client.get(f"/api/optimize/{job_id}").json()
            if body["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert body and body["status"] == "done", body
        best = body["result"]["best"]
        assert body["result"]["fee_rate"] == 0.0007 and body["result"]["strategy"] == "williams_r"
        assert set(best) >= {"interval", "params", "exit", "avg_return", "avg_benchmark", "beats_benchmark", "trades"}
        assert create_strategy("williams_r", best["params"]).params.model_dump(mode="json") == best["params"]
        assert client.get("/api/optimize/nope").status_code == 404
        assert client.post("/api/optimize/nope/cancel", json={}).status_code == 404
