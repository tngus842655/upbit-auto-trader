"""대시보드 API 테스트 — 메모리 DB + 가짜 공개 시세 클라이언트. 실제 네트워크·프로세스 생성 없음."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import server as server_module
from app.api.server import create_app
from app.api.services import DashboardService
from app.database import Database, Repository
from app.exchange.models import KST, Market, Ticker
from app.strategy.base import Action, Signal
from app.trading.portfolio import Portfolio
from tests.test_models import TICKER_JSON

NOW = datetime.now(UTC)


MARKETS_JSON = [
    {"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin",
     "market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": False}}},
    {"market": "KRW-DOGE", "korean_name": "도지코인", "english_name": "Dogecoin",
     "market_event": {"warning": True, "caution": {"PRICE_FLUCTUATIONS": True, "TRADING_VOLUME_SOARING": False}}},
    {"market": "KRW-ETH", "korean_name": "이더리움", "english_name": "Ethereum", "market_event": None},
    {"market": "BTC-ETH", "korean_name": "이더리움", "english_name": "Ethereum", "market_event": None},
]


class FakePublicClient:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.calls = 0
        self.market_calls = 0

    async def get_markets(self, *, is_details: bool = False):
        self.market_calls += 1
        return [Market.model_validate(m) for m in MARKETS_JSON]

    async def get_quote_tickers(self, quote_currencies="KRW"):
        volume = {"KRW-BTC": 3.5e12, "KRW-ETH": 8.2e11, "KRW-DOGE": 1.5e10}
        return [
            Ticker.model_validate({**TICKER_JSON, "market": m, "trade_price": self.prices.get(m, 100.0),
                                   "acc_trade_price_24h": volume[m], "signed_change_rate": 0.01 * i})
            for i, m in enumerate(["KRW-DOGE", "KRW-ETH", "KRW-BTC"])
        ]

    async def get_tickers(self, markets):
        self.calls += 1
        return [
            Ticker.model_validate({**TICKER_JSON, "market": m, "trade_price": self.prices.get(m, 100.0)})
            for m in markets
        ]


@pytest.fixture
def api(make_settings):
    settings = make_settings()
    db = Database("sqlite://")
    db.create_all()
    public = FakePublicClient({"KRW-BTC": 110.0, "KRW-ETH": 50.0})
    app = create_app(settings, db=db, public_client=public)
    return TestClient(app), Repository(db, "paper"), settings, db


def seed(repo: Repository) -> Portfolio:
    portfolio = Portfolio(1_000_000)
    t0 = NOW - timedelta(days=2)
    portfolio.buy("KRW-BTC", 100.0, time=t0, amount=200_000)
    repo.sync_portfolio(portfolio)
    repo.snapshot_balance(portfolio, {"KRW-BTC": 100.0}, t0)
    repo.snapshot_balance(portfolio, {"KRW-BTC": 120.0}, NOW - timedelta(days=1))
    repo.snapshot_balance(portfolio, {"KRW-BTC": 90.0}, NOW - timedelta(hours=1))
    repo.save_signal(Signal(Action.BUY, "KRW-BTC", t0, 100.0, "ma_cross", "골든크로스", {"rsi": 40.0}), "60m")
    repo.log("ERROR", "api_error", "테스트 오류")
    repo.log("INFO", "bot_start", "시작")
    return portfolio


def test_status_without_engine(api) -> None:
    client, repo, settings, _ = api
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] is None and body["engine_alive"] is False and body["engine_state"] == "NONE"
    assert body["env_mode"] == "PAPER" and body["live_allowed"] is False and body["settings_version"] == 0


def test_balance_performance_recent(api) -> None:
    client, repo, _, _ = api
    portfolio = seed(repo)
    bal = client.get("/api/balance").json()
    pos = bal["positions"][0]
    assert pos["market"] == "KRW-BTC" and pos["current_price"] == 110.0
    assert bal["equity"] == pytest.approx(portfolio.cash + pos["quantity"] * 110.0)
    perf = client.get("/api/performance").json()
    assert perf["initial_cash"] == 1_000_000
    assert perf["cumulative_return"] == pytest.approx(bal["equity"] / 1_000_000 - 1)
    assert perf["mdd"] < 0 and perf["mdd_trough_time"]
    assert len(perf["series"]) == 4 and perf["series"][-1]["equity"] == pytest.approx(bal["equity"])
    assert perf["today_return"] is not None
    recent = client.get("/api/recent?limit=5").json()
    assert recent["signals"][0]["action"] == "BUY" and recent["signals"][0]["indicators"] == {"rsi": 40.0}
    assert [e["event"] for e in recent["errors"]] == ["api_error"]
    assert client.get("/api/positions").json()[0]["market"] == "KRW-BTC"
    assert client.get("/api/logs?level=ERROR").json()["items"][0]["event"] == "api_error"
    assert client.get("/api/signals").json()[0]["market"] == "KRW-BTC"


def test_logs_paging_and_filters(api) -> None:
    client, repo, _, _ = api
    for i in range(250):
        repo.log("INFO" if i % 10 else "ERROR", f"evt{i % 3}", f"메시지 {i}")
    page = client.get("/api/logs").json()
    assert len(page["items"]) == 100 and page["has_more"] is True and page["items"][0]["message"] == "메시지 249"
    page2 = client.get(f"/api/logs?before_id={page['next_before_id']}").json()
    assert page2["items"][0]["message"] == "메시지 149" and page2["has_more"] is True
    page3 = client.get(f"/api/logs?before_id={page2['next_before_id']}").json()
    assert len(page3["items"]) == 50 and page3["has_more"] is False and page3["items"][-1]["message"] == "메시지 0"
    assert all(e["level"] == "ERROR" for e in client.get("/api/logs?level=ERROR").json()["items"])
    assert len(client.get("/api/logs?q=메시지 24").json()["items"]) == 11  # 24, 240~249
    today = datetime.now(KST).date().isoformat()
    assert len(client.get(f"/api/logs?date_from={today}&date_to={today}&limit=500").json()["items"]) == 250
    assert client.get("/api/logs?date_from=2026-01-01&date_to=2026-01-02").json()["items"] == []
    assert client.get("/api/logs?date_from=bad").status_code == 422


def test_strategy_meta_and_settings_roundtrip(api) -> None:
    client, repo, _, _ = api
    meta = client.get("/api/strategy").json()
    assert set(meta["available"]) == {"ma_cross", "rsi"} and "60m" in meta["intervals"]
    assert meta["current"]["strategy_name"] == "ma_cross" and meta["version"] == 0
    assert "short_window" in meta["schemas"]["ma_cross"]["properties"]
    assert "stop_loss_pct" in meta["risk_schema"]["properties"]

    current = client.get("/api/settings").json()
    assert current["version"] == 0 and current["history"] == []
    data = dict(current["data"])
    data["strategy_params"] = {"short_window": 5, "long_window": 20}
    data["risk"]["stop_loss_pct"] = 0.03
    data["candle_interval"] = "15m"
    r = client.put("/api/settings", json={"data": data, "note": "테스트"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["changes"] == {"hot": ["strategy_params", "risk"], "restart": ["candle_interval"]}
    assert "재시작" in body["note"]
    assert repo.runtime_settings_version() == 1
    again = client.get("/api/settings").json()
    assert again["version"] == 1 and again["data"]["candle_interval"] == "15m"
    assert again["history"][0]["note"] == "테스트"

    bad_data = {**data, "strategy_params": {"short_window": 99, "long_window": 5}}
    bad = client.put("/api/settings", json={"data": bad_data})
    assert bad.status_code == 422 and "short_window" in bad.text
    assert client.put("/api/settings", json={"nope": 1}).status_code == 400


def test_settings_version_lookup_and_restore(api) -> None:
    client, repo, _, _ = api
    base = client.get("/api/settings").json()["data"]
    first = {"data": {**base, "candle_interval": "15m"}, "note": "첫 저장"}
    second = {"data": {**base, "candle_interval": "240m"}, "note": "둘째"}
    v1 = client.put("/api/settings", json=first).json()["version"]
    v2 = client.put("/api/settings", json=second).json()["version"]
    got = client.get(f"/api/settings/{v1}").json()
    assert got["version"] == v1 and got["data"]["candle_interval"] == "15m" and got["note"] == "첫 저장"
    assert client.get("/api/settings/99").status_code == 404
    hist = client.get("/api/settings").json()["history"]
    assert hist[0]["version"] == v2 and hist[0]["candle_interval"] == "240m" and hist[1]["strategy_name"] == "ma_cross"
    # 되돌리기 = 이전 버전 내용을 새 버전으로 저장 (이력은 그대로)
    restored = client.put("/api/settings", json={"data": got["data"], "note": f"v{v1} 설정 복원"}).json()
    assert restored["version"] == v2 + 1
    assert client.get("/api/settings").json()["data"]["candle_interval"] == "15m"
    assert repo.load_runtime_settings_version(v2).data["candle_interval"] == "240m"


def test_commands_and_start(api, monkeypatch) -> None:
    client, repo, _, _ = api
    r = client.post("/api/bot/pause")
    assert r.status_code == 200 and r.json()["queued"] and r.json()["engine_alive"] is False
    assert [c.command for c in repo.pending_commands()] == ["pause"]
    assert client.post("/api/bot/resume-risk").json()["command"] == "resume_risk"
    assert client.post("/api/bot/bogus").status_code == 404

    started = {}
    monkeypatch.setattr(
        server_module, "start_engine", lambda settings, mode, confirm_live="": started.setdefault("pid", 4242)
    )
    r = client.post("/api/bot/start", json={})
    assert r.status_code == 200 and r.json()["pid"] == 4242 and started["pid"] == 4242

    # 하트비트가 살아 있으면 중복 시작 거부
    repo.write_engine_status(status="RUNNING", pid=4242)
    assert client.post("/api/bot/start", json={}).status_code == 409
    assert client.get("/api/status").json()["engine_alive"] is True

    # LIVE 는 .env 이중 플래그가 없으면 거부
    assert client.post("/api/bot/start?mode=live", json={"confirm_live": "REAL-MONEY"}).status_code == 403


def test_kill_uses_pid(api, monkeypatch) -> None:
    client, repo, _, _ = api
    assert client.post("/api/bot/kill").status_code == 404
    repo.write_engine_status(status="RUNNING", pid=777)
    killed = []
    monkeypatch.setattr(server_module, "kill_engine", lambda pid: killed.append(pid) or True)
    r = client.post("/api/bot/kill")
    assert r.status_code == 200 and killed == [777]
    assert repo.read_engine_status().status == "STOPPED"


def test_token_auth(make_settings) -> None:
    settings = make_settings(dashboard_token="secret-token")
    db = Database("sqlite://")
    db.create_all()
    client = TestClient(create_app(settings, db=db, public_client=FakePublicClient({})))
    assert client.get("/api/status").status_code == 200  # 조회는 자유
    assert client.post("/api/bot/pause").status_code == 401
    assert client.post("/api/bot/pause", headers={"X-Auth-Token": "wrong"}).status_code == 401
    assert client.post("/api/bot/pause", headers={"X-Auth-Token": "secret-token"}).status_code == 200
    with client.websocket_connect("/ws?mode=paper&token=secret-token") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "tick" and "status" in msg and "balance" in msg


def test_pockets_endpoints(api, monkeypatch) -> None:
    client, repo, _, _ = api

    async def fake_pockets(self):
        main_pocket = {"uuid": "m", "name": "메인포켓", "type": "main", "is_main": True, "balances": []}
        return {"universal_transfer": True, "pockets": [main_pocket], "bot_pocket": None}

    async def fake_transfer(self, *, direction, amount, currency="KRW", bot_pocket_uuid=None):
        if direction not in ("to_bot", "to_main"):
            raise ValueError("direction 오류")
        return {"uuid": "t1", "state": "done", "currency": currency, "amount": amount, "from": "m", "to": "s",
                "created_at": "x"}

    monkeypatch.setattr(DashboardService, "pockets", fake_pockets)
    monkeypatch.setattr(DashboardService, "transfer", fake_transfer)
    assert client.get("/api/pockets").json()["universal_transfer"] is True
    r = client.post("/api/pockets/transfer", json={"direction": "to_bot", "amount": 10000})
    assert r.status_code == 200 and r.json()["state"] == "done"
    assert client.post("/api/pockets/transfer", json={"direction": "sideways", "amount": 1}).status_code == 400
    assert any(e.event == "pocket_transfer" for e in repo.recent_logs(5))


def test_markets_catalog(api) -> None:
    client, _, _, _ = api
    body = client.get("/api/markets").json()
    assert body["quote"] == "KRW" and body["count"] == 3  # BTC-ETH 는 제외
    assert [r["market"] for r in body["items"]] == ["KRW-BTC", "KRW-ETH", "KRW-DOGE"]  # 거래대금 순
    btc = body["items"][0]
    assert btc["korean_name"] == "비트코인" and btc["trade_price"] == 110.0 and btc["base"] == "BTC"
    assert btc["acc_trade_price_24h"] == 3.5e12 and btc["warning"] is False and btc["caution"] == []
    doge = body["items"][2]
    assert doge["warning"] is True and doge["caution"] == ["PRICE_FLUCTUATIONS"]
    # 60초 캐시: 두 번째 호출은 업비트에 다시 묻지 않고, refresh 면 다시 묻는다
    public = client.app.state.service.public_client
    client.get("/api/markets")
    assert public.market_calls == 1
    client.get("/api/markets?refresh=true")
    assert public.market_calls == 2
    assert client.get("/api/markets?quote=bad!").status_code == 422


def test_index_and_static(api) -> None:
    client, _, _, _ = api
    r = client.get("/")
    assert r.status_code == 200 and "업비트 자동매매" in r.text
    assert client.get("/static/app.js").status_code == 200
