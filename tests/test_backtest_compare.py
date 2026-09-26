"""전략별 성과 비교 — 같은 캔들·같은 수수료로 여러 전략을 돌리고, 전략별 비교표(CLI·대시보드 공용)를 확인한다."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from app.api.backtests import BacktestRequest, BacktestRunner
from app.backtest import BacktestConfig, format_comparison, run_strategies, save_comparison, summarize_by_strategy
from app.backtest.compare import parse_strategy_names, result_row
from app.core.exceptions import StrategyError
from app.database import Database, Repository
from app.main import build_parser, cmd_backtest
from app.strategy import STRATEGIES, create_strategy
from app.strategy.data import save_candles_csv
from tests.test_backtest_api import fake_loader
from tests.test_strategy import random_frame

ALL = list(STRATEGIES)


def test_parse_strategy_names() -> None:
    assert parse_strategy_names("all") == ALL and parse_strategy_names("") == ALL
    assert parse_strategy_names(" MACD, bollinger,macd ") == ["macd", "bollinger"]
    with pytest.raises(StrategyError, match="nope"):
        parse_strategy_names("macd,nope")


def test_run_strategies_same_data_and_fees() -> None:
    df = random_frame(600, seed=21)
    strategies = [create_strategy(name) for name in ALL]
    config = BacktestConfig(fee_rate=0.001, slippage_rate=0.0005)
    results = run_strategies(df, strategies, config, market="KRW-TEST", interval="60m")
    assert [r.strategy_name for r in results] == ALL
    for r in results:
        assert r.start == results[0].start and r.end == results[0].end and len(r.equity) == 600
        assert r.config.fee_rate == 0.001 and r.config.slippage_rate == 0.0005
        # 수수료는 거래마다 매수·매도 양쪽이 빠지고, 실현 손익은 수수료 차감 후 손익의 합
        assert r.metrics.total_fees == pytest.approx(sum(t.entry_fee + t.exit_fee for t in r.trades))
        assert r.metrics.realized_pnl == pytest.approx(sum(t.pnl for t in r.trades))
        assert r.metrics.final_equity == pytest.approx(r.metrics.initial_capital + r.metrics.realized_pnl)
        if r.trades:
            assert r.metrics.total_fees > 0
            assert r.metrics.avg_trade_return == pytest.approx(sum(t.pnl_pct for t in r.trades) / len(r.trades))
    assert sum(r.metrics.total_trades for r in results) > 50  # 대부분의 전략이 실제로 거래한다


def test_fees_lower_every_strategy_result() -> None:
    df = random_frame(500, seed=4)
    strategies = [create_strategy(name) for name in ALL]
    free = run_strategies(df, strategies, BacktestConfig(fee_rate=0.0, slippage_rate=0.0))
    costly = run_strategies(df, strategies, BacktestConfig(fee_rate=0.002, slippage_rate=0.001))
    for a, b in zip(free, costly, strict=True):
        assert a.metrics.total_trades == b.metrics.total_trades  # 신호는 비용과 무관
        if a.metrics.total_trades:
            assert b.metrics.realized_pnl < a.metrics.realized_pnl and a.metrics.total_fees == 0


def test_summarize_by_strategy_aggregates_markets_and_skips_errors() -> None:
    macd_defaults = create_strategy("macd").params.model_dump(mode="json")
    rows = [
        {"strategy": "macd", "strategy_params": macd_defaults, "metrics": {
            "total_return": 0.10, "total_trades": 4, "wins": 3, "avg_trade_return": 0.03, "mdd": -0.05,
            "max_consecutive_losses": 1, "total_fees": 100.0, "realized_pnl": 1000.0,
            "final_equity": 1_001_000.0, "initial_capital": 1_000_000.0},
         "benchmark": {"total_return": 0.05}},
        {"strategy": "macd", "strategy_params": dict(macd_defaults), "metrics": {
            "total_return": -0.02, "total_trades": 6, "wins": 1, "avg_trade_return": -0.01, "mdd": -0.20,
            "max_consecutive_losses": 4, "total_fees": 150.0, "realized_pnl": -200.0},
         "benchmark": {"total_return": 0.01}},
        # 지표가 생기기 전에 저장된 결과: 실현 손익은 최종 자산 − 초기 자본으로, 거래당 수익률은 빼고 계산
        {"strategy": "rsi", "metrics": {
            "total_return": 0.2, "total_trades": 0, "wins": 0, "mdd": 0.0, "max_consecutive_losses": 0,
            "total_fees": 0.0, "final_equity": 1_200_000.0, "initial_capital": 1_000_000.0, "win_rate": None},
         "benchmark": {"total_return": 0.3}},
        {"strategy": "macd", "market": "KRW-BAD", "error": "없는 마켓"},
    ]
    table = summarize_by_strategy(rows)
    assert [r["strategy"] for r in table] == ["rsi", "macd"]  # 평균 총 수익률 높은 순
    macd = table[1]
    assert macd["runs"] == 2 and macd["trades"] == 10 and macd["wins"] == 4 and macd["win_rate"] == pytest.approx(0.4)
    assert macd["avg_return"] == pytest.approx(0.04) and macd["beat_benchmark"] == 1
    assert macd["avg_trade_return"] == pytest.approx((0.03 * 4 - 0.01 * 6) / 10)  # 거래 수 가중
    assert macd["worst_mdd"] == -0.20 and macd["avg_mdd"] == pytest.approx(-0.125)
    assert macd["max_consecutive_losses"] == 4 and macd["total_fees"] == 250.0 and macd["realized_pnl"] == 800.0
    assert macd["family"] == "TREND" and macd["family_label"] == "추세 추종"
    assert macd["strategy_params"] == macd_defaults and macd["default_params"] is True  # 돌린 파라미터와 출처
    rsi = table[0]
    assert rsi["realized_pnl"] == 200_000.0 and rsi["avg_trade_return"] is None and rsi["win_rate"] is None
    assert rsi["strategy_params"] is None and rsi["default_params"] is None  # 파라미터 기록이 없는 옛 결과
    # 같은 전략이 결과마다 다른 파라미터로 돌았으면 하나로 말할 수 없으므로 None
    mixed = summarize_by_strategy([{**rows[0]}, {**rows[1], "strategy_params": {**macd_defaults, "fast_period": 5}}])
    assert mixed[0]["strategy_params"] is None
    json.dumps(table, allow_nan=False)  # 대시보드 JSON 응답에 NaN·무한대가 섞이지 않는다
    assert summarize_by_strategy([]) == []


def test_format_and_save_comparison(tmp_path) -> None:
    df = random_frame(400, seed=8)
    results = run_strategies(df, [create_strategy(n) for n in ("macd", "bollinger", "obv")],
                             BacktestConfig(fee_rate=0.0005), market="KRW-TEST", interval="60m")
    text = format_comparison(results)
    assert "전략 비교: KRW-TEST 60m" in text and "수수료 0.050%" in text and "단순 보유" in text
    assert all(name in text for name in ("macd", "bollinger", "obv"))
    assert "전략별 파라미터" in text and "[기본값] fast_period=12, slow_period=26" in text
    out = save_comparison(results, tmp_path / "cmp")
    table = pd.read_csv(out / "comparison.csv")
    assert set(table["strategy"]) == {"macd", "bollinger", "obv"} and "total_fees" in table.columns
    assert (out / "macd" / "summary.json").exists() and (out / "obv" / "trades.csv").exists()
    assert result_row(results[0])["metrics"]["total_trades"] == results[0].metrics.total_trades


async def test_cli_backtest_compare(make_settings, tmp_path, capsys) -> None:
    csv = save_candles_csv(random_frame(400, seed=2), tmp_path / "KRW-TEST_60m.csv")
    args = build_parser().parse_args([
        "backtest", "KRW-TEST", "--interval", "60m", "--start", "2026-01-01", "--end", "2026-01-20",
        "--csv", str(csv), "--strategy", "ma_cross", "--params", "short_window=5,long_window=20",
        "--compare", "macd,bollinger,ma_cross", "--out-dir", str(tmp_path / "out"),
    ])
    assert await cmd_backtest(make_settings(), args) == 0
    text = capsys.readouterr().out
    assert "전략 비교: KRW-TEST 60m" in text
    # 기준 전략은 한 번만 (비교 목록의 같은 이름은 빠진다): 비교표 한 줄 + 파라미터 한 줄
    assert text.count("ma_cross ") == 2
    assert "[지정값] short_window=5, long_window=20" in text  # --params 로 넘긴 기준 전략
    assert "[기본값] fast_period=12" in text  # 비교 전략은 기본 파라미터
    saved = list((tmp_path / "out").glob("*_compare"))
    assert len(saved) == 1 and (saved[0] / "comparison.csv").exists() and (saved[0] / "ma_cross").is_dir()
    summary = json.loads((saved[0] / "ma_cross" / "summary.json").read_text(encoding="utf-8"))
    assert summary["strategy_params"]["short_window"] == 5  # 기준 전략은 넘긴 파라미터로


# ---------------------------------------------------------------------------
# 대시보드 비교 요청
# ---------------------------------------------------------------------------
BASE = {"markets": ["KRW-BTC"], "strategy_name": "ma_cross",
        "strategy_params": {"short_window": 5, "long_window": 20, "volume_window": 0, "rsi_window": 0},
        "periods": [{"label": "2026년", "start": "2026-01-01"}], "save": False}


def test_request_compare_strategies_validation() -> None:
    req = BacktestRequest(**BASE, compare_strategies=["MACD", {"name": "bollinger", "params": {"window": 30}},
                                                      "ma_cross", "macd"])
    # 기준 전략과 같은 이름·중복은 빠지고, 파라미터는 기본값까지 채운 정규형
    assert [(s.name, s.params.get("window")) for s in req.compare_strategies] == [("macd", None), ("bollinger", 30)]
    assert req.compare_strategies[0].params["zero_line_filter"] is True
    assert [name for name, _ in req.strategy_runs()] == ["ma_cross", "macd", "bollinger"]
    with pytest.raises(ValueError, match="알 수 없는 전략"):
        BacktestRequest(**BASE, compare_strategies=["nope"])
    with pytest.raises(ValueError, match="fast_period"):
        BacktestRequest(**BASE, compare_strategies=[{"name": "macd", "params": {"fast_period": 40}}])
    assert BacktestRequest(**BASE).compare_strategies == []  # 기존 요청은 그대로


async def test_runner_runs_every_strategy_on_same_candles(make_settings, tmp_path) -> None:
    calls: list[str] = []

    async def counting_loader(settings, market, interval, start, end=None, **kwargs):
        calls.append(market)
        return await fake_loader(settings, market, interval, start, end)

    db = Database("sqlite://")
    db.create_all()
    repo = Repository(db, "paper")
    runner = BacktestRunner(make_settings(), loader=counting_loader, save_dir=tmp_path, repo=repo)
    req = BacktestRequest(**{**BASE, "markets": ["KRW-BTC", "KRW-BAD"]}, compare_strategies=["macd", "bollinger"])
    job = await runner.run_sync(req)
    assert job.status == "done" and job.total == job.progress == 6 == len(job.results)
    assert calls == ["KRW-BTC", "KRW-BAD"]  # 캔들은 (구간, 마켓)마다 한 번만 받는다
    ok = [r for r in job.results if "error" not in r]
    assert [r["strategy"] for r in ok] == ["ma_cross", "macd", "bollinger"]
    assert all(r["fee_rate"] == 0.0005 and "realized_pnl" in r["metrics"] for r in ok)
    bad = [r for r in job.results if "error" in r]
    assert [r["strategy"] for r in bad] == ["ma_cross", "macd", "bollinger"]
    assert all("없는 마켓" in r["error"] for r in bad)

    body = job.to_dict()
    assert body["label"].startswith("ma_cross 외 2개 전략")
    assert [c["name"] for c in body["request"]["compare_strategies"]] == ["macd", "bollinger"]
    assert {row["strategy"] for row in body["comparison"]} == {"ma_cross", "macd", "bollinger"}
    assert all(row["runs"] == 1 for row in body["comparison"])
    # 비교표마다 실제로 돌린 파라미터와 출처: 현재 설정(ma_cross, 기본값 아님) / 비교 전략(기본값)
    by_name = {row["strategy"]: row for row in body["comparison"]}
    assert by_name["ma_cross"]["strategy_params"]["short_window"] == 5
    assert by_name["ma_cross"]["default_params"] is False
    assert by_name["macd"]["default_params"] is True and by_name["macd"]["strategy_params"]["signal_period"] == 9
    json.dumps(body, allow_nan=False)
    # 서버 재시작 뒤 DB 에서 읽어도 비교표가 붙는다
    fresh = BacktestRunner(make_settings(), loader=counting_loader, save_dir=tmp_path, repo=repo)
    assert fresh.get_dict(job.id)["comparison"] == body["comparison"]
