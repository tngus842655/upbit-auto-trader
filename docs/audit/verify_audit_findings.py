r"""2026-09-24 감사 보고서의 핵심 발견 사항을 실제 코드 경로로 재현하는 스크립트.

실행 (프로젝트 루트, 개발 의존성 설치 후):
    PYTHONPATH=. python docs/audit/verify_audit_findings.py            # Linux/macOS
    set PYTHONPATH=. && .venv\Scripts\python.exe docs\audit\verify_audit_findings.py   # Windows

- 네트워크·실제 API Key·실제 DB 파일을 쓰지 않는다. 가짜 클라이언트와 메모리 SQLite 만 사용한다.
- 각 항목은 "재현됨" 또는 "재현 안 됨" 을 출력한다. 수정 후 다시 돌려 전부 "재현 안 됨" 이 되면 조치가 끝난 것이다.
- tests/ 의 헬퍼(Harness, FakeClient 등)를 재사용하므로 tests 패키지가 import 가능해야 한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import warnings
from datetime import UTC, datetime, timedelta

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.CRITICAL)

from sqlalchemy.exc import OperationalError  # noqa: E402

from app.config.settings import Settings  # noqa: E402
from app.core.exceptions import UpbitAPIError, UpbitNetworkError  # noqa: E402
from app.exchange.models import Account, OrderChance, OrderInfo  # noqa: E402
from app.exchange.ws_models import parse_ws_message  # noqa: E402
from app.risk.config import RiskConfig  # noqa: E402
from app.risk.manager import RiskManager  # noqa: E402
from app.trading.live_broker import LiveBroker, portfolio_from_accounts  # noqa: E402
from app.trading.market_state import PriceState  # noqa: E402
from app.trading.orders import Order, OrderRequest, OrderStatus, OrderType  # noqa: E402
from app.trading.portfolio import Portfolio, Side  # noqa: E402
from tests.test_engine import MARKET, T0, Harness  # noqa: E402
from tests.test_live_broker import FakeClient as BrokerFakeClient  # noqa: E402
from tests.test_live_broker import order_info  # noqa: E402
from tests.test_websocket import ORDERBOOK_JSON  # noqa: E402

NOW = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
M = MARKET
RESULTS: list[tuple[str, bool, str]] = []


def record(code: str, reproduced: bool, detail: str) -> None:
    RESULTS.append((code, reproduced, detail))
    print(f"[{code}] {'재현됨' if reproduced else '재현 안 됨'}: {detail}")


def make_settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def live_settings() -> Settings:
    return make_settings(trading_mode="LIVE", live_trading_enabled=True, upbit_access_key="a" * 20,
                         upbit_secret_key="b" * 40)


def chance(krw: str = "1000000", btc: str = "0") -> OrderChance:
    return OrderChance.model_validate({
        "bid_fee": "0.0005", "ask_fee": "0.0005", "maker_bid_fee": "0.0005", "maker_ask_fee": "0.0005",
        "market": {"id": M, "bid_types": ["limit", "price"], "ask_types": ["limit", "market"],
                   "bid": {"currency": "KRW", "min_total": "5000"}, "ask": {"currency": "BTC", "min_total": "5000"},
                   "max_total": "1000000000", "state": "active"},
        "bid_account": {"currency": "KRW", "balance": krw, "locked": "0", "avg_buy_price": "0",
                        "avg_buy_price_modified": False, "unit_currency": "KRW"},
        "ask_account": {"currency": "BTC", "balance": btc, "locked": "0", "avg_buy_price": "100000000",
                        "avg_buy_price_modified": False, "unit_currency": "KRW"},
    })


def done_info(uuid: str, identifier: str | None = None) -> OrderInfo:
    return OrderInfo.model_validate({
        "market": M, "uuid": uuid, "side": "bid", "ord_type": "price", "state": "done",
        "created_at": "2026-05-01T12:00:00+09:00", "executed_volume": "0.0001", "paid_fee": "5", "trades_count": 1,
        "identifier": identifier,
        "trades": [{"market": M, "uuid": "t", "price": "100000000", "volume": "0.0001", "funds": "10000",
                    "side": "bid", "created_at": "x"}],
    })


async def no_sleep(_: float) -> None:
    return None


# ----------------------------------------------------------------------------------------------
# CRITICAL-1  주문 응답 타임아웃 + identifier 조회 실패/404 → 새 identifier 로 재주문 (중복 주문)
# ----------------------------------------------------------------------------------------------
class ServerCreatedButTimedOut:
    """1차 create_order: 서버는 주문을 만들었지만 응답이 타임아웃. identifier 조회는 lookup_error 로 실패."""

    orders_allowed = True
    market_buy_params = staticmethod(lambda market, amount, identifier=None: {
        "market": market, "side": "bid", "ord_type": "price", "price": f"{amount:.0f}"})
    market_sell_params = staticmethod(lambda market, volume, identifier=None: {
        "market": market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"})

    def __init__(self, lookup_error: Exception) -> None:
        self.server_orders: list[dict] = []
        self.lookup_error = lookup_error

    async def get_order_chance(self, market):
        return chance()

    async def create_order(self, params):
        self.server_orders.append(dict(params))          # 서버에는 주문이 생긴다
        if len(self.server_orders) == 1:
            raise UpbitNetworkError("POST /v1/orders 타임아웃")   # 응답만 잃음
        return done_info("u2", params["identifier"])

    async def get_order(self, *, uuid=None, identifier=None):
        if identifier is not None:
            raise self.lookup_error
        return done_info("u2")

    async def cancel_order(self, **kw):
        raise AssertionError("not expected")


async def check_critical_1() -> None:
    for label, err in (("조회도 타임아웃", UpbitNetworkError("GET /v1/order 타임아웃")),
                       ("조회 404", UpbitAPIError(404, "order_not_found", "not yet visible"))):
        client = ServerCreatedButTimedOut(err)
        broker = LiveBroker(client, Portfolio(1_000_000), live_settings(), sleep=no_sleep)
        order = await broker.execute(OrderRequest(M, Side.BUY, amount=300_000, client_id="c1"), None, NOW)
        ids = [o["identifier"] for o in client.server_orders]
        record("CRITICAL-1", len(client.server_orders) >= 2,
               f"{label}: 거래소 주문 {len(client.server_orders)}건 {ids}, 내부 상태 {order.status.value} "
               "(기대: 1건만 생성되거나 UNKNOWN 으로 기록)")


# ----------------------------------------------------------------------------------------------
# CRITICAL-2  오래된 호가를 mark_price 가 우선 → 손절 판정에 옛 가격 사용
# ----------------------------------------------------------------------------------------------
def check_critical_2() -> None:
    s = PriceState(M, last_price=80_000_000.0, last_time=NOW,                          # REST 보정: 방금
                   best_bid=99_990_000.0, best_ask=100_010_000.0, book_time=NOW - timedelta(hours=3))  # 3시간 전 호가
    stale_used = s.is_fresh(NOW, 30) and s.mark_price == 100_000_000.0
    record("CRITICAL-2", stale_used,
           f"is_fresh(30s)={s.is_fresh(NOW, 30)}, mark_price={s.mark_price:,.0f}, 최신 체결가={s.last_price:,.0f} "
           "(기대: 호가가 오래됐으면 최신 체결가 80,000,000 사용)")
    # 같은 상태로 손절 판정
    p = Portfolio(1_000_000)
    p.buy(M, 100_000_000.0, time=NOW, quantity=0.001, enforce_limits=False)
    rm = RiskManager(RiskConfig(stop_loss_pct=0.05))
    hit = rm.check_exits(p.position(M), low=s.mark_price, high=s.mark_price, now=NOW)
    record("CRITICAL-2", hit is None,
           f"실제 -20% 인데 손절 판정={hit} (기대: stop_loss 발동)")


# ----------------------------------------------------------------------------------------------
# HIGH-1  체결 확인 실패(UNKNOWN)가 REJECTED 로 축약되어 계좌 미반영
# ----------------------------------------------------------------------------------------------
async def check_high_1() -> None:
    class PollFails(BrokerFakeClient):
        async def get_order(self, *, uuid=None, identifier=None):
            raise UpbitNetworkError("GET /v1/order 타임아웃")

    client = PollFails(create_results=[order_info("u1", side="bid", state="wait")])
    portfolio = Portfolio(1_000_000)
    broker = LiveBroker(client, portfolio, live_settings(), sleep=no_sleep)
    order = await broker.execute(OrderRequest(M, Side.BUY, amount=100_000, client_id="c1"), None, NOW)
    record("HIGH-1", order.status is OrderStatus.REJECTED and len(client.created) == 1 and portfolio.cash == 1_000_000,
           f"거래소 주문 {len(client.created)}건 생성, 내부 상태 {order.status.value}, 내부 현금 {portfolio.cash:,.0f} "
           "(기대: UNKNOWN 상태로 기록하고 후속 확인)")


# ----------------------------------------------------------------------------------------------
# HIGH-2  check_exits 내부의 비-TraderError 예외로 _price_loop 영구 종료
# ----------------------------------------------------------------------------------------------
async def check_high_2() -> None:
    h = Harness(make_settings)
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.portfolio.buy(M, 100.0, time=h.now, amount=100_000)      # 손절선 95

    real_repo = h.engine.repo

    class FlakyRepo:                                         # save_order 만 'database is locked'
        def __getattr__(self, name):
            if name == "save_order":
                def boom(*a, **k):
                    raise OperationalError("INSERT INTO orders", {}, Exception("database is locked"))
                return boom
            return getattr(real_repo, name)

    h.engine.repo = FlakyRepo()

    class FakeWS:
        state = type("S", (), {"value": "CONNECTED"})()

        async def stream(self):
            for ask, bid in [(97.0, 96.0), (93.0, 92.0), (91.0, 90.0)]:
                book = {**ORDERBOOK_JSON, "code": M, "timestamp": int(h.now.timestamp() * 1000),
                        "orderbook_units": [{"ask_price": ask, "bid_price": bid, "ask_size": 1, "bid_size": 1}]}
                h.now += timedelta(seconds=2)
                yield parse_ws_message(json.dumps(book))
            await asyncio.Event().wait()                     # 실제 스트림처럼 끝나지 않는다

        async def close(self):
            return None

    h.engine.ws_factory = lambda subs: FakeWS()
    task = asyncio.create_task(h.engine._price_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
        ended = True
    except TimeoutError:
        ended = False
        task.cancel()
    logs = [e.event for e in real_repo.recent_logs(10)]
    record("HIGH-2", ended and "price_stream_failed" in logs,
           f"시세 루프 종료={ended}, price_stream_failed 로그={'price_stream_failed' in logs}, "
           f"엔진 status={h.engine.status} (기대: 루프가 재생성되거나 엔진이 감시 중단을 알림)")


# ----------------------------------------------------------------------------------------------
# HIGH-3  재시작 시 day_start_equity 가 현재 자산으로 재설정 → 일일 손실 한도 기준선 리셋
# ----------------------------------------------------------------------------------------------
def check_high_3() -> None:
    day = datetime(2026, 5, 1, 1, 0, tzinfo=UTC)               # KST 10:00
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.03))
    rm.update_equity(1_000_000, day)
    rm.update_equity(975_000, day + timedelta(hours=1))         # -2.5%, 아직 잠금 없음
    rm2 = RiskManager(RiskConfig(daily_loss_limit_pct=0.03))
    rm2.rebuild([], day + timedelta(hours=2), 975_000)          # 재시작 (오늘 청산 거래 없음)
    lock = rm2.update_equity(950_000, day + timedelta(hours=3))  # 당일 누적 -5%
    record("HIGH-3", rm2.state.day_start_equity == 975_000 and lock is None,
           f"재시작 후 day_start={rm2.state.day_start_equity:,.0f}, 당일 -5% 에서 lock={lock} "
           "(기대: day_start 1,000,000 복구, 잠금 발동)")
    rm.halt("수동 긴급 정지")
    rm3 = RiskManager(RiskConfig())
    rm3.rebuild([], day + timedelta(hours=2), 975_000)
    record("HIGH-3", rm3.state.halted is False, f"halt 후 재시작: halted={rm3.state.halted} (기대: 영속되어 True)")


# ----------------------------------------------------------------------------------------------
# HIGH-4  정체 후 누락 캔들의 오래된 신호를 현재가로 일괄 실행
# ----------------------------------------------------------------------------------------------
async def check_high_4() -> None:
    buy_t, sell_t = T0 + timedelta(hours=10), T0 + timedelta(hours=11)
    h = Harness(make_settings, actions={buy_t: "BUY", sell_t: "SELL"})
    await h.engine.warmup()
    h.add_candle(12, 112.0)
    h.now = T0 + timedelta(hours=13, seconds=5)                 # 10,11,12시 캔들이 한꺼번에 닫힘
    h.feed_prices(ask=150.0, bid=149.0)
    signals = await h.engine.process_closed_candles(h.now)
    orders = list(reversed(h.repo.recent_orders(5)))
    filled = [(o.side, o.status) for o in orders]
    record("HIGH-4", len([o for o in orders if o.status == "FILLED"]) >= 2,
           f"3시간 정체 후 신호 {[(s.time.hour, s.action.value) for s in signals]} → 주문 {filled}, "
           f"왕복 손익 {[round(t.pnl) for t in h.portfolio.trades]} (기대: 마지막 캔들 신호만 실행)")


# ----------------------------------------------------------------------------------------------
# HIGH-5  대시보드: 토큰 설정 시에도 조회 API 무인증, 토큰 미설정 시 폼 POST(CSRF) 로 제어 명령 접수
# ----------------------------------------------------------------------------------------------
def check_high_5() -> None:
    from fastapi.testclient import TestClient

    from app.api import server as server_module
    from app.api.server import create_app
    from app.database import Database, Repository

    db = Database("sqlite://")
    db.create_all()
    app = create_app(make_settings(), db=db, public_client=object())
    started: list[str] = []
    server_module.start_engine = lambda settings, mode, confirm_live="": started.append(mode) or 4242
    c = TestClient(app)
    form = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.example",
            "Referer": "https://evil.example/"}
    stop = c.post("/api/bot/stop?mode=live", headers=form, content=b"").status_code
    halt = c.post("/api/bot/halt?mode=live", headers=form, content=b"").status_code
    start = c.post("/api/bot/start?mode=paper", headers=form, content=b"").status_code
    queued = [x.command for x in Repository(db, "live").pending_commands()]
    record("HIGH-5", stop == 200 and halt == 200 and start == 200 and queued == ["stop", "halt"],
           f"외부 Origin 폼 POST: stop={stop}, halt={halt}, start(paper)={start}, live 큐={queued}, "
           f"start_engine 호출={started} (기대: 전부 403)")

    app2 = create_app(make_settings(dashboard_token="s3cret"), db=db, public_client=object())
    c2 = TestClient(app2)
    codes = {p: c2.get(p).status_code for p in ("/api/status", "/api/logs", "/api/settings", "/api/orders")}
    record("HIGH-5", all(v == 200 for v in codes.values()),
           f"토큰 설정 상태 무인증 조회 {codes} (기대: 401)")


# ----------------------------------------------------------------------------------------------
# MEDIUM-1  trades 없는 시장가 매수 응답 → 평균가에 '주문 총액' 사용
# ----------------------------------------------------------------------------------------------
def check_medium_1() -> None:
    p = Portfolio(1_000_000)
    broker = LiveBroker(BrokerFakeClient(), p, live_settings(), sleep=no_sleep)
    order = Order(id="o", client_id="c", mode="live", market=M, side=Side.BUY, order_type=OrderType.MARKET,
                  amount=100_000, quantity=None, status=OrderStatus.NEW, created_at=NOW)
    info = OrderInfo.model_validate({
        "market": M, "uuid": "u9", "side": "bid", "ord_type": "price", "state": "done",
        "created_at": "x", "executed_volume": "0.001", "paid_fee": "50", "trades_count": 0,
        "price": "100000", "trades": [],                       # ord_type=price 의 price 는 총액
    })
    broker._apply_fill(order, info, NOW)
    pos = p.position(M)
    record("MEDIUM-1", pos is not None and pos.avg_price == 100_000.0,
           f"포지션 avg_price={pos.avg_price if pos else None:,.0f}, 현금 차감={1_000_000 - p.cash:,.0f} "
           "(기대: 단가 ≈ 100,000,000, 차감 ≈ 100,000)")


# ----------------------------------------------------------------------------------------------
# MEDIUM-2  avg_buy_price=0 / 먼지 잔고 포지션 편입 → 손절 기준가 0
# ----------------------------------------------------------------------------------------------
def check_medium_2() -> None:
    accounts = [
        Account.model_validate({"currency": "KRW", "balance": "0", "locked": "0", "avg_buy_price": "0",
                                "avg_buy_price_modified": False, "unit_currency": "KRW"}),
        Account.model_validate({"currency": "BTC", "balance": "0.01", "locked": "0", "avg_buy_price": "0",
                                "avg_buy_price_modified": True, "unit_currency": "KRW"}),
    ]
    p = portfolio_from_accounts(accounts, [M], fee_rate=0.0005)
    rm = RiskManager(RiskConfig(stop_loss_pct=0.05))
    hit = rm.check_exits(p.position(M), low=1.0, high=1.0, now=NOW)
    record("MEDIUM-2", p.position(M).avg_price == 0.0 and hit is None,
           f"avg_price={p.position(M).avg_price}, 가격 1원에서 손절={hit} (기대: 기준가 대체 또는 편입 제외)")
    dust = [Account.model_validate({"currency": "BTC", "balance": "0.00000001", "locked": "0",
                                    "avg_buy_price": "100000000", "avg_buy_price_modified": False,
                                    "unit_currency": "KRW"})]
    p2 = portfolio_from_accounts(dust, [M], fee_rate=0.0005)
    record("MEDIUM-2", p2.has_position(M), f"먼지 잔고 1e-8 BTC 포지션 편입={p2.has_position(M)} (기대: False)")


async def main() -> int:
    await check_critical_1()
    check_critical_2()
    await check_high_1()
    await check_high_2()
    check_high_3()
    await check_high_4()
    check_high_5()
    check_medium_1()
    check_medium_2()
    reproduced = sum(1 for _, r, _ in RESULTS if r)
    print(f"\n=== 합계: 검사 {len(RESULTS)}건 중 재현됨 {reproduced}건 ===")
    return 1 if reproduced else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
