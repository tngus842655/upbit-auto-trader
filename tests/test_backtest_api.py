"""대시보드 백테스트 — 가짜 캔들 로더로 네트워크 없이 요청 검증·실행·결과 직렬화·API 흐름을 확인한다."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api.backtests import BacktestRequest, BacktestRunner, Period, _series_points, parse_kst
from app.api.server import create_app
from app.core.exceptions import MarketDataError
from app.database import Database, Repository
from app.exchange.models import KST, CandleInterval
from app.strategy.data import candles_to_dataframe
from tests.test_api import FakePublicClient
from tests.test_strategy_data import make_candle

FAST_PARAMS = {"short_window": 5, "long_window": 20, "volume_window": 0, "rsi_window": 0}


def wave_frame(start: datetime, count: int = 400) -> pd.DataFrame:
    """사인파 가격 → 골든/데드크로스가 여러 번 나와 거래가 생긴다."""
    candles = [make_candle(start + timedelta(hours=i), 100 + 20 * math.sin(i / 25)) for i in range(count)]
    return candles_to_dataframe(candles, interval=CandleInterval.parse("60m"))


async def fake_loader(settings, market, interval, start, end=None, **_kwargs):
    if market == "KRW-BAD":
        raise MarketDataError("없는 마켓")
    base = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return wave_frame(base)


def test_request_validation_and_period_parsing() -> None:
    req = BacktestRequest(
        markets="krw-btc, KRW-ETH,krw-btc", strategy_name="MA_CROSS", candle_interval="1h",
        periods=[
            {"label": "2025년", "start": "2025-01-01", "end": "2026-01-01"}, {"label": "올해", "start": "2026-01-01"},
        ],
    )
    assert req.markets == ["KRW-BTC", "KRW-ETH"] and req.strategy_name == "ma_cross" and req.candle_interval == "60m"
    assert req.strategy_params["short_window"] == 20  # 기본값까지 채워진 정규형
    assert req.periods[0].start == datetime(2025, 1, 1, tzinfo=KST) and req.periods[1].end is None
    assert req.risk_config().stop_loss_pct is None and req.backtest_config().fee_rate == 0.0005
    assert parse_kst("2025-06-01T09:00:00+09:00") == datetime(2025, 6, 1, 9, tzinfo=KST)

    with pytest.raises(ValueError, match="앞서야"):
        Period(label="x", start="2026-01-01", end="2025-01-01")
    with pytest.raises(ValueError, match="알 수 없는 전략"):
        BacktestRequest(markets=["KRW-BTC"], strategy_name="nope", periods=[{"label": "a", "start": "2026-01-01"}])
    with pytest.raises(ValueError, match="잘못된 마켓"):
        BacktestRequest(markets=["btc"], strategy_name="ma_cross", periods=[{"label": "a", "start": "2026-01-01"}])
    with pytest.raises(ValueError, match="short_window"):
        BacktestRequest(markets=["KRW-BTC"], strategy_name="ma_cross", periods=[{"label": "a", "start": "2026-01-01"}],
                        strategy_params={"short_window": 99, "long_window": 5})


def test_series_points_downsamples_and_keeps_last() -> None:
    idx = pd.date_range("2026-01-01", periods=1000, freq="h", tz="UTC")
    pts = _series_points(pd.Series(range(1000), index=idx, dtype=float), limit=300)
    assert 250 <= len(pts) <= 301 and pts[-1]["value"] == 999.0 and pts[0]["value"] == 0.0
    assert pts[0]["time"].endswith("+09:00")
    assert _series_points(pd.Series([], dtype=float)) == []


async def test_runner_runs_all_combinations(make_settings, tmp_path) -> None:
    runner = BacktestRunner(make_settings(), loader=fake_loader, save_dir=tmp_path / "bt")
    req = BacktestRequest(
        markets=["KRW-BTC", "KRW-BAD"], strategy_name="ma_cross", strategy_params=FAST_PARAMS, fee_rate=0.001,
        periods=[
            {"label": "2025년", "start": "2025-01-01", "end": "2026-01-01"}, {"label": "올해", "start": "2026-01-01"},
        ],
    )
    job = await runner.run_sync(req)
    assert job.status == "done" and job.progress == job.total == 4
    ok = [r for r in job.results if "error" not in r]
    bad = [r for r in job.results if "error" in r]
    assert len(ok) == 2 and len(bad) == 2 and all("없는 마켓" in r["error"] for r in bad)
    first = ok[0]
    assert first["period"] == "2025년" and first["market"] == "KRW-BTC" and first["interval"] == "60m"
    assert first["metrics"]["total_trades"] > 0 and first["metrics"]["total_fees"] > 0  # 수수료 반영
    assert first["fee_rate"] == 0.001
    assert len(first["trades"]) == min(first["trade_count"], 60)  # 화면에는 최근 60건만
    assert len(first["equity"]) == len(first["benchmark_equity"]) <= 301
    assert first["saved_to"] and (tmp_path / "bt").exists()
    assert sum(first["exit_reasons"].values()) == first["metrics"]["total_trades"]
    summary = job.to_dict()
    assert summary["ok"] == 2 and summary["failed"] == 2 and summary["request"]["fee_rate"] == 0.001
    assert summary["request"]["periods"][0]["end"].startswith("2026-01-01")
    assert runner.list_jobs()[0]["id"] == job.id and "results" not in runner.list_jobs()[0]


async def test_runner_applies_risk_rules(make_settings, tmp_path) -> None:
    runner = BacktestRunner(make_settings(), loader=fake_loader, save_dir=tmp_path)
    base = {"markets": ["KRW-BTC"], "strategy_name": "ma_cross", "strategy_params": FAST_PARAMS,
            "periods": [{"label": "a", "start": "2026-01-01"}], "save": False}
    plain = await runner.run_sync(BacktestRequest(**base))
    risk = {"stop_loss_pct": 0.01, "position_fraction": 0.5}
    risky = await runner.run_sync(BacktestRequest(**base, use_risk=True, risk=risk))
    assert plain.results[0]["saved_to"] is None
    assert risky.to_dict()["request"]["risk"]["stop_loss_pct"] == 0.01
    assert "stop_loss" in risky.results[0]["exit_reasons"]


async def test_jobs_persist_in_db(make_settings, tmp_path) -> None:
    db = Database("sqlite://")
    db.create_all()
    repo = Repository(db, "paper")
    runner = BacktestRunner(make_settings(), loader=fake_loader, save_dir=tmp_path, repo=repo, keep=2)
    req = BacktestRequest(markets=["KRW-BTC"], strategy_name="ma_cross", strategy_params=FAST_PARAMS,
                          periods=[{"label": "a", "start": "2026-01-01"}], save=False)
    first = await runner.run_sync(req)
    second = await runner.run_sync(req)
    third = await runner.run_sync(req)
    # 새 실행기(서버 재시작 상황)에서도 목록·결과가 보인다
    fresh = BacktestRunner(make_settings(), loader=fake_loader, save_dir=tmp_path, repo=repo, keep=2)
    listed = fresh.list_jobs()
    assert [j["id"] for j in listed][0] == third.id and all("results" not in j for j in listed)
    got = fresh.get_dict(third.id)
    assert got and got["status"] == "done" and got["ok"] == 1
    assert got["results"][0]["metrics"]["total_trades"] > 0 and got["created_at"].endswith("+09:00")
    assert repo.trim_backtest_jobs(keep=2) == 1 and fresh.get_dict(first.id) is None
    assert await fresh.delete(second.id) is True and fresh.get_dict(second.id) is None
    assert await fresh.delete("nope") is False
    assert repo.count_rows(type(repo.load_backtest_job(third.id))) == 1


def test_backtest_api_flow(make_settings, tmp_path) -> None:
    settings = make_settings(paper_fee_rate=0.0007)
    db = Database("sqlite://")
    db.create_all()
    runner = BacktestRunner(settings, loader=fake_loader, save_dir=tmp_path, repo=Repository(db, "paper"))
    app = create_app(settings, db=db, public_client=FakePublicClient({}), backtests=runner)
    with TestClient(app) as client:
        defaults = client.get("/api/backtest/defaults").json()
        assert defaults["fee_rate"] == 0.0007 and datetime.now(KST).year in defaults["years"]
        assert "60m" in defaults["intervals"]

        bad_body = {"markets": ["KRW-BTC"], "strategy_name": "ma_cross", "periods": []}
        bad = client.post("/api/backtest/jobs", json=bad_body)
        assert bad.status_code == 422 and "periods" in bad.text
        assert client.get("/api/backtest/jobs/nope").status_code == 404

        r = client.post("/api/backtest/jobs", json={
            "markets": ["KRW-BTC"], "strategy_name": "ma_cross", "strategy_params": FAST_PARAMS,
            "candle_interval": "60m", "periods": [{"label": "2025년", "start": "2025-01-01", "end": "2026-01-01"}],
            "fee_rate": 0.0007, "save": False,
        })
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]
        assert r.json()["total"] == 1 and "results" not in r.json()

        deadline = time.time() + 10
        body = None
        while time.time() < deadline:
            body = client.get(f"/api/backtest/jobs/{job_id}").json()
            if body["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert body and body["status"] == "done", body
        assert body["results"][0]["metrics"]["total_trades"] > 0 and body["results"][0]["fee_rate"] == 0.0007
        listed = client.get("/api/backtest/jobs").json()
        assert listed[0]["id"] == job_id and listed[0]["ok"] == 1 and "results" not in listed[0]
        # DB 에 남아 있어 새 실행기(서버 재시작)에서도 같은 결과가 보인다
        fresh = BacktestRunner(settings, loader=fake_loader, save_dir=tmp_path, repo=Repository(db, "paper"))
        assert fresh.get_dict(job_id)["results"][0]["metrics"]["total_trades"] > 0
        assert client.post("/api/backtest/jobs/nope/cancel", json={}).status_code == 404
        assert client.delete(f"/api/backtest/jobs/{job_id}").json()["deleted"] is True
        assert client.get(f"/api/backtest/jobs/{job_id}").status_code == 404
        assert client.delete("/api/backtest/jobs/nope").status_code == 404


async def test_dashboard_jobs_run_one_at_a_time(make_settings, tmp_path) -> None:
    """감사 MEDIUM-12 — 작업을 여러 개 제출해도 한 번에 하나만 돌고(나머지는 queued), 로더 호출이 겹치지 않는다."""
    import asyncio

    active = {"now": 0, "max": 0}
    release = asyncio.Event()

    async def slow_loader(settings, market, interval, start, end=None, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await release.wait()
        active["now"] -= 1
        return await fake_loader(settings, market, interval, start, end)

    runner = BacktestRunner(make_settings(), loader=slow_loader, save_dir=tmp_path / "bt")
    req = BacktestRequest(strategy_name="ma_cross", strategy_params={}, markets=["KRW-BTC"], candle_interval="60m",
                          periods=[Period(label="p", start=datetime(2026, 1, 1, tzinfo=KST),
                                          end=datetime(2026, 1, 20, tzinfo=KST))], save=False)
    first, second = runner.submit(req), runner.submit(req)
    await asyncio.sleep(0.05)
    assert first.status == "running" and second.status == "queued" and active["max"] == 1
    release.set()
    await asyncio.gather(first.task, second.task)
    assert first.status == "done" and second.status == "done" and active["max"] == 1


async def test_dashboard_loader_uses_slow_shared_rate_limiter(make_settings, monkeypatch) -> None:
    """감사 MEDIUM-12 — 대시보드 로더는 REST 조회에 초당 3회 공용 리미터를 넘기고, CLI 로더는 기본 리미터를 쓴다."""
    from app.api import backtests as bt
    from app.backtest import loader as loader_module
    from app.exchange.upbit_client import UpbitClient

    seen: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get_candles_range(self, market, interval, *, start, end):
            raise MarketDataError("stop here")

    monkeypatch.setattr(UpbitClient, "from_settings", classmethod(lambda cls, settings, **ov: FakeClient(**ov)))
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(MarketDataError):
        await bt.dashboard_load_candles(make_settings(), "KRW-BTC", "60m", start, end, use_cache=False)
    with pytest.raises(MarketDataError):
        await loader_module.load_candles(make_settings(), "KRW-BTC", "60m", start, end, use_cache=False)
    assert seen[0]["rate_limiter"] is bt.DASHBOARD_RATE_LIMITER and "rate_limiter" not in seen[1]
    assert bt.DASHBOARD_RATE_LIMITER.limiter("candle").limit == 3  # 초당 3회, 안전 여유 없이 정확히
    assert BacktestRunner(make_settings()).loader is bt.dashboard_load_candles
