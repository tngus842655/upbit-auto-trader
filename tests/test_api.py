"""대시보드 API 테스트 — 메모리 DB + 가짜 공개 시세 클라이언트. 실제 네트워크·프로세스 생성 없음."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import server as server_module
from app.api.server import create_app
from app.api.services import DashboardService
from app.database import Database, Repository
from app.database.models import AccountRecord
from app.exchange.models import KST, Account, Market, Ticker
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


def test_recent_signal_limit(api) -> None:
    """'최근 신호' 표만 signal_limit 개(대시보드 100)를 받고 주문·오류 표는 limit 을 따른다."""
    client, repo, _, _ = api
    for i in range(30):
        t = NOW - timedelta(minutes=15 * i)
        repo.save_signal(Signal(Action.HOLD, "KRW-BTC", t, 100.0, "ma_cross"), "15m")
        repo.log("ERROR", "api_error", f"오류 {i}")
    repo.save_signal(Signal(Action.BUY, "KRW-BTC", NOW, 101.0, "ma_cross", "골든크로스"), "1m")
    recent = client.get("/api/recent?limit=5&signal_limit=25").json()
    assert len(recent["signals"]) == 25 and len(recent["errors"]) == 5
    same_time = [s for s in recent["signals"] if s["time"] == recent["signals"][0]["time"]]
    assert len(same_time) == 2 and same_time[0]["id"] != same_time[1]["id"]  # 1m·15m 같은 시각도 행 구분
    assert len(client.get("/api/recent?limit=5").json()["signals"]) == 5  # 안 주면 limit 과 같다
    assert client.get("/api/recent?signal_limit=201").status_code == 422


def test_signal_close_time(api) -> None:
    """신호 표는 캔들이 닫혀 판단한 시각을 보여준다 — 15:15 에 시작한 15분봉은 15:30. 단위가 섞이면 판단 순서로 정렬."""
    client, repo, _, _ = api
    start = datetime(2026, 9, 25, 6, 15, tzinfo=UTC)  # 15:15 KST
    repo.save_signal(Signal(Action.HOLD, "KRW-BTC", start, 100.0, "ma_cross"), "15m")
    repo.save_signal(Signal(Action.HOLD, "KRW-BTC", start + timedelta(minutes=5), 100.0, "ma_cross"), "1m")
    rows = client.get("/api/recent?limit=5").json()["signals"]
    assert [(r["interval"], r["time"][11:16], r["close_time"][11:16]) for r in rows] == [
        ("15m", "15:15", "15:30"),  # 시작은 1m 보다 이르지만 판단(15:30)은 더 늦다
        ("1m", "15:20", "15:21"),
    ]


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
    r = client.post("/api/bot/pause", json={})
    assert r.status_code == 200 and r.json()["queued"] and r.json()["engine_alive"] is False
    assert [c.command for c in repo.pending_commands()] == ["pause"]
    assert client.post("/api/bot/resume-risk", json={}).json()["command"] == "resume_risk"
    assert client.post("/api/bot/bogus", json={}).status_code == 404

    started = {}
    monkeypatch.setattr(
        server_module, "start_engine", lambda settings, mode, confirm_live="": started.setdefault("pid", 4242)
    )
    r = client.post("/api/bot/start", json={})
    assert r.status_code == 200 and r.json()["pid"] == 4242 and started["pid"] == 4242
    # 시작 요청 직후 STARTING 하트비트가 기록되어 연타는 409 (감사 HIGH-7)
    assert repo.read_engine_status().status == "STARTING"
    assert client.post("/api/bot/start", json={}).status_code == 409

    # 하트비트가 살아 있으면 중복 시작 거부
    repo.write_engine_status(status="RUNNING", pid=4242)
    assert client.post("/api/bot/start", json={}).status_code == 409
    assert client.get("/api/status").json()["engine_alive"] is True

    # LIVE 는 .env 이중 플래그가 없으면 거부
    assert client.post("/api/bot/start?mode=live", json={"confirm_live": "REAL-MONEY"}).status_code == 403


def test_kill_uses_pid(api, monkeypatch) -> None:
    client, repo, _, _ = api
    assert client.post("/api/bot/kill", json={}).status_code == 404
    repo.write_engine_status(status="RUNNING", pid=777)
    killed = []
    monkeypatch.setattr(server_module, "kill_engine", lambda pid: killed.append(pid) or True)
    # 감사 MEDIUM-5: PID 의 명령줄이 우리 엔진일 때만 종료한다
    monkeypatch.setattr(server_module, "process_cmdline", lambda pid: "C:/py/python.exe -m app.main run")
    r = client.post("/api/bot/kill", json={})
    assert r.status_code == 200 and killed == [777]
    assert repo.read_engine_status().status == "STOPPED"
    # PID 가 다른 프로세스로 재사용됐으면 종료하지 않는다 (409, 상태 유지)
    repo.write_engine_status(status="RUNNING", pid=778)
    monkeypatch.setattr(server_module, "process_cmdline", lambda pid: "notepad.exe C:/memo.txt")
    r = client.post("/api/bot/kill", json={})
    assert r.status_code == 409 and killed == [777] and "엔진 프로세스가 아닙니다" in r.json()["detail"]
    assert repo.read_engine_status().status == "RUNNING"
    # 명령줄 확인 실패("")도 종료하지 않는다
    monkeypatch.setattr(server_module, "process_cmdline", lambda pid: "")
    assert client.post("/api/bot/kill", json={}).status_code == 409 and killed == [777]
    # 프로세스가 이미 없으면 상태만 STOPPED 로 정리
    monkeypatch.setattr(server_module, "process_cmdline", lambda pid: None)
    r = client.post("/api/bot/kill", json={})
    assert r.status_code == 200 and r.json()["killed"] is False and killed == [777]
    assert repo.read_engine_status().status == "STOPPED"


def test_token_auth(make_settings, monkeypatch) -> None:
    """감사 HIGH-5: 토큰이 설정되면 조회를 포함한 모든 API·WebSocket 에 토큰이 필요하다."""
    monkeypatch.setattr(server_module, "AUTH_FAIL_DELAY", 0.0)
    settings = make_settings(dashboard_token="secret-token")
    db = Database("sqlite://")
    db.create_all()
    client = TestClient(create_app(settings, db=db, public_client=FakePublicClient({})))
    ok = {"X-Auth-Token": "secret-token"}
    assert client.get("/api/status").status_code == 401  # 조회도 인증
    assert client.get("/api/status", headers={"X-Auth-Token": "wrong"}).status_code == 401
    assert client.get("/api/status?token=secret-token").status_code == 401  # 쿼리스트링 토큰은 받지 않는다
    assert client.get("/api/status", headers=ok).status_code == 200
    assert client.post("/api/bot/pause", json={}).status_code == 401
    assert client.post("/api/bot/pause", json={}, headers=ok).status_code == 200
    assert client.get("/").status_code == 200  # 화면 자체는 열린다 (토큰은 화면에서 입력)
    with client.websocket_connect("/ws?mode=paper") as ws:
        ws.send_json({"token": "secret-token"})
        msg = ws.receive_json()
        assert msg["type"] == "tick" and "status" in msg and "balance" in msg
    with client.websocket_connect("/ws?mode=paper") as ws:
        ws.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == 4401


def test_local_mode_rejects_cross_site_and_remote(api, monkeypatch) -> None:
    """감사 HIGH-5: 토큰 없는 로컬 모드에서도 외부 출처 폼 POST(CSRF)·원격 요청은 거부한다."""
    client, repo, _, _ = api
    evil = {"Origin": "https://evil.example", "Referer": "https://evil.example/"}
    form = {"Content-Type": "application/x-www-form-urlencoded", **evil}
    assert client.post("/api/bot/stop?mode=live", headers=form, content=b"").status_code == 403
    assert client.post("/api/bot/halt?mode=live", headers=form, content=b"").status_code == 403
    assert client.post("/api/bot/start?mode=paper", headers=form, content=b"").status_code == 403
    assert client.post("/api/bot/halt", headers=evil, json={}).status_code == 403  # JSON 이어도 다른 출처면 거부
    assert client.post("/api/bot/pause", headers={"Sec-Fetch-Site": "cross-site"}, json={}).status_code == 403
    assert client.post("/api/bot/pause", headers={"Content-Type": "text/plain"}, content=b"x").status_code == 403
    assert client.post("/api/bot/pause", headers={"Origin": "http://testserver"}, json={}).status_code == 200
    assert client.get("/api/status", headers=evil).status_code == 200  # 조회는 출처 검사 없음 (로컬)
    assert [c.command for c in repo.pending_commands()] == ["pause"]
    assert Repository(client.app.state.db, "live").pending_commands() == []
    remote = TestClient(client.app, client=("203.0.113.5", 40000))
    assert remote.get("/api/status").status_code == 403
    assert remote.post("/api/bot/pause", json={}).status_code == 403
    with remote.websocket_connect("/ws?mode=paper") as ws, pytest.raises(WebSocketDisconnect) as info:
        ws.receive_json()
    assert info.value.code == 4403


def test_pockets_endpoints(api, monkeypatch) -> None:
    client, repo, _, db = api

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
    # 이전 금액은 LIVE 입출금으로 기록된다 (감사 MEDIUM-4): 메인→봇 +, 봇→메인 −, 실패(400)는 기록 없음
    assert client.post("/api/pockets/transfer", json={"direction": "to_main", "amount": 2500}).status_code == 200
    flows = Repository(db, "live").load_cash_flows()
    assert [f.amount for f in flows] == [10000, -2500] and flows[0].note.startswith("대시보드 포켓 이전")
    assert Repository(db, "paper").load_cash_flows() == []


def test_performance_excludes_cash_flows(api) -> None:
    """감사 MEDIUM-4 — 누적 수익률은 (자산 / (초기 자산 + 순입출금)) − 1."""
    client, repo, _, _ = api
    seed(repo)
    before = client.get("/api/performance?mode=paper").json()
    repo.save_cash_flow(100_000, "입금")
    perf = client.get("/api/performance?mode=paper").json()
    assert perf["net_cash_flow"] == 100_000 and perf["equity"] == before["equity"]
    assert perf["cumulative_return"] == pytest.approx(perf["equity"] / (perf["initial_cash"] + 100_000) - 1)
    assert perf["cumulative_return"] < before["cumulative_return"]


def test_cumulative_return_ignores_deposit_before_first_start(api) -> None:
    """실거래 첫 실행 전 입금은 initial_cash(거래소 잔고)에 이미 들어 있다 — 또 더하면 35,000/70,000 = -50%."""
    client, _, _, db = api
    live = Repository(db, "live")
    live.save_cash_flow(35_000, "메인→봇")  # 엔진 시작 전 포켓 이전
    portfolio = Portfolio(35_000)  # 첫 실행: 거래소 잔고로 계좌를 만든다
    live.sync_portfolio(portfolio)
    live.snapshot_balance(portfolio, {}, NOW)
    perf = client.get("/api/performance?mode=live").json()
    assert perf["initial_cash"] == 35_000 and perf["net_cash_flow"] == 0
    assert perf["cumulative_return"] == pytest.approx(0.0)
    live.save_cash_flow(10_000, "추가 입금")  # 시작 뒤 입금은 기준 자산에 더한다
    portfolio.cash += 10_000
    live.sync_portfolio(portfolio)
    perf = client.get("/api/performance?mode=live").json()
    assert perf["net_cash_flow"] == 10_000 and perf["cumulative_return"] == pytest.approx(0.0)


def test_cumulative_return_base_backfilled_for_old_account(api) -> None:
    """기준점 컬럼이 없던 때 만든 계좌: 첫 잔고 기록 전 입출금을 initial_cash 에 든 것으로 보고 한 번 채워 저장한다."""
    client, _, _, db = api
    live = Repository(db, "live")
    flow_id = live.save_cash_flow(35_000, "메인→봇", time=NOW - timedelta(hours=1))
    portfolio = Portfolio(35_000)
    live.sync_portfolio(portfolio)
    live.snapshot_balance(portfolio, {}, NOW - timedelta(minutes=30))
    with db.session() as s:  # 컬럼 추가 전 계좌 흉내
        s.get(AccountRecord, "live").cash_flow_base_id = None
    assert client.get("/api/performance?mode=live").json()["cumulative_return"] == pytest.approx(0.0)
    assert live.load_account().cash_flow_base_id == flow_id  # 저장됨 — 첫 잔고 기록이 보존 기간으로 지워져도 기준 유지


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


def test_dashboard_bot_client_cannot_place_orders(make_settings) -> None:
    """감사 LOW-13 — 대시보드의 봇 클라이언트는 LIVE 이중 플래그가 켜져 있어도 주문 권한이 없다."""
    settings = make_settings(trading_mode="LIVE", live_trading_enabled=True, upbit_access_key="a" * 20,
                             upbit_secret_key="b" * 40)
    assert settings.is_live_trading_allowed is True
    service = DashboardService(settings, Database("sqlite://"), FakePublicClient({}))
    client = service.bot_client()
    assert client is not None and client.orders_allowed is False  # 조치 전: True (from_settings 가 이중 플래그를 상속)
    assert DashboardService(make_settings(), Database("sqlite://"), FakePublicClient({})).bot_client() is None


def test_live_balance_reads_exchange_before_first_engine_run(api, monkeypatch) -> None:
    """실거래 엔진이 아직 돌지 않아 DB 에 계좌가 없으면 대시보드가 봇 포켓 잔고를 거래소에서 직접 보여 준다."""
    client, _, _, _ = api

    class FakeBot:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get_accounts(self):
            return [
                Account.model_validate({"currency": "KRW", "balance": "35000", "locked": "0", "avg_buy_price": "0",
                                        "avg_buy_price_modified": False, "unit_currency": "KRW"}),
                Account.model_validate({"currency": "BTC", "balance": "0.001", "locked": "0", "avg_buy_price": "100",
                                        "avg_buy_price_modified": False, "unit_currency": "KRW"}),
            ]

    monkeypatch.setattr(DashboardService, "bot_client", lambda self: FakeBot())
    bal = client.get("/api/balance?mode=live").json()
    assert bal["source"] == "exchange" and bal["cash"] == 35_000 and bal["initial_cash"] is None
    assert bal["positions"][0]["market"] == "KRW-BTC" and bal["positions"][0]["current_price"] == 110.0
    assert bal["equity"] == pytest.approx(35_000 + 0.001 * 110.0)
    assert "미실행" in bal["note"]
    assert client.get("/api/balance?mode=paper").json()["source"] == "db"  # 모의매매는 DB 그대로
    perf = client.get("/api/performance?mode=live").json()
    assert perf["equity"] == pytest.approx(35_000.11) and perf["cumulative_return"] is None


def test_today_return_ignores_deposit_already_in_base(api, monkeypatch) -> None:
    """오늘 첫 기록이 입금 뒤의 잔고면 오늘 수익률은 0% (조치 전: 입금액을 통째로 빼서 -100%)."""
    client, repo, _, db = api

    class FakeBot:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get_accounts(self):
            return [Account.model_validate({"currency": "KRW", "balance": "35000", "locked": "0", "avg_buy_price": "0",
                                            "avg_buy_price_modified": False, "unit_currency": "KRW"})]

    monkeypatch.setattr(DashboardService, "bot_client", lambda self: FakeBot())
    Repository(db, "live").save_cash_flow(35_000, "메인→봇")  # 오늘 입금, 아직 스냅샷 없음
    perf = client.get("/api/performance?mode=live").json()
    assert perf["equity"] == 35_000 and perf["today_return"] == pytest.approx(0.0)
    # 어제 스냅샷이 있고 오늘 입금이 있으면: (지금 자산 - 오늘 입금) / 어제 마지막 자산 - 1
    seed(repo)
    repo.save_cash_flow(100_000, "입금")
    before = client.get("/api/balance?mode=paper").json()["equity"]
    perf = client.get("/api/performance?mode=paper").json()
    from app.database.models import from_db_time

    day_start = datetime.now(UTC).astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_last = [s.equity for s in repo.balance_series() if from_db_time(s.time) < day_start][-1]
    assert perf["today_return"] == pytest.approx((before - 100_000) / yesterday_last - 1)
