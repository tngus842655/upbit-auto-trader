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
from app.database import Database
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


def test_backtest_api_flow(make_settings, tmp_path) -> None:
    settings = make_settings(paper_fee_rate=0.0007)
    db = Database("sqlite://")
    db.create_all()
    runner = BacktestRunner(settings, loader=fake_loader, save_dir=tmp_path)
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
        assert client.get("/api/backtest/jobs").json()[0]["id"] == job_id
        assert client.delete(f"/api/backtest/jobs/{job_id}").json()["cancelled"] is True
        assert client.delete("/api/backtest/jobs/nope").status_code == 404
