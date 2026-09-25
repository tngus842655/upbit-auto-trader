"""진입점.

Phase 1 에서는 연결 점검용 명령만 제공한다. 실제 매매 루프(``run``)·백테스트(``backtest``)·API 서버는
이후 Phase 에서 추가된다. **이 파일의 어떤 명령도 주문을 만들지 않는다.**

사용법 (프로젝트 루트에서):
    python -m app.main check                 # 설정·시세·캔들·(키가 있으면) 잔고 점검
    python -m app.main ticker KRW-BTC KRW-ETH
    python -m app.main candles KRW-BTC --interval 60m --count 5
    python -m app.main balance               # 잔고 조회 (API Key 필요)
    python -m app.main stream KRW-BTC --types trade,orderbook --seconds 10   # WebSocket 실시간 시세
    python -m app.main signal KRW-BTC --strategy ma_cross --interval 60m      # 전략 신호 계산 (Phase 3)
    python -m app.main backtest KRW-BTC --interval 60m --start 2026-01-01     # 백테스트 (Phase 4)
    python -m app.main run --interval 60m                                     # 모의매매 (Phase 5, PAPER 전용)
    python -m app.main status [--mode live]                                   # 엔진 상태·계좌 조회
    python -m app.main control pause|resume|stop|halt|resume-risk             # 실행 중 엔진 제어 (Phase 7)
    python -m app.main order-test KRW-BTC --amount 5000                       # 주문 테스트 API (실제 주문 없음)
    python -m app.main notify-test                                            # 알림 채널(Telegram/Discord) 테스트 발송
    python -m app.main run --confirm-live REAL-MONEY                          # LIVE (이중 플래그 + 확인 문구)
    python -m app.main serve                                                  # 대시보드 http://127.0.0.1:8000 (Phase 8)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.backtest import BacktestConfig, BacktestEngine, format_report, load_candles, save_result
from app.config.settings import PROJECT_ROOT, Settings, TradingMode, get_settings, parse_params_text
from app.core.exceptions import ConfigError, TraderError
from app.core.logging import setup_logging
from app.database import Database, Repository
from app.database.models import from_db_time
from app.exchange.models import KST, Account, Candle, CandleInterval, Ticker
from app.exchange.upbit_client import UpbitClient
from app.exchange.websocket import Subscription, UpbitWebSocket
from app.exchange.ws_models import WsCandle, WsMessage, WsOrderbook, WsTicker, WsTrade
from app.notify import EventKind, NotificationEvent, NotifyError, build_notification_manager, discover_chats, get_me
from app.risk import RiskConfig, RiskManager
from app.risk.state_store import RepositoryRiskStateStore
from app.strategy import check_no_lookahead, create_strategy
from app.strategy.data import candles_to_dataframe, detect_price_anomalies, drop_unclosed, validate_candles
from app.trading.engine import TradingEngine
from app.trading.instance_lock import InstanceLock
from app.trading.live_broker import LiveBroker, merge_saved_positions, portfolio_from_accounts
from app.trading.live_guard import LIVE_CONFIRM_PHRASE
from app.trading.orders import PaperBroker
from app.trading.portfolio import DEFAULT_MIN_ORDER_AMOUNT
from app.trading.runtime_settings import RuntimeSettings

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 출력 도우미
# ----------------------------------------------------------------------
def _fmt_price(value: float | Decimal) -> str:
    value = float(value)
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.6f}"


def print_settings(settings: Settings) -> None:
    summary = settings.summary()
    print("=== 설정 ===")
    for key, value in summary.items():
        print(f"  {key:<22} {value}")
    for warning in settings.safety_warnings():
        print(f"  [경고] {warning}")


def print_tickers(tickers: Sequence[Ticker]) -> None:
    print("=== 현재가 ===")
    for t in tickers:
        sign = "+" if t.signed_change_rate >= 0 else ""
        print(
            f"  {t.market:<10} {_fmt_price(t.trade_price):>16}  "
            f"전일대비 {sign}{t.signed_change_rate * 100:.2f}%  "
            f"24h 거래대금 {t.acc_trade_price_24h / 1e8:,.1f}억  "
            f"체결시각(KST) {t.trade_datetime_kst:%Y-%m-%d %H:%M:%S}"
        )


def print_candles(candles: Sequence[Candle], interval: str) -> None:
    print(f"=== 캔들 ({interval}, 최신순) ===")
    print(f"  {'시각(KST)':<20} {'시가':>14} {'고가':>14} {'저가':>14} {'종가':>14} {'거래량':>14}")
    for c in candles:
        print(
            f"  {c.candle_date_time_kst:%Y-%m-%d %H:%M:%S}  "
            f"{_fmt_price(c.open):>14} {_fmt_price(c.high):>14} {_fmt_price(c.low):>14} "
            f"{_fmt_price(c.close):>14} {c.volume:>14.4f}"
        )


def print_accounts(accounts: Sequence[Account]) -> None:
    print("=== 잔고 ===")
    if not accounts:
        print("  (보유 자산 없음)")
        return
    print(f"  {'통화':<8} {'주문가능':>20} {'묶임':>16} {'평균매수가':>16} {'기준통화':>8}")
    for a in accounts:
        print(
            f"  {a.currency:<8} {a.balance:>20} {a.locked:>16} {a.avg_buy_price:>16} {a.unit_currency:>8}"
        )


# ----------------------------------------------------------------------
# 명령
# ----------------------------------------------------------------------
async def cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    print_settings(settings)
    async with UpbitClient.from_settings(settings) as client:
        tickers = await client.get_tickers(settings.markets)
        print_tickers(tickers)

        market = settings.markets[0]
        candles = await client.get_candles(market, args.interval, count=args.count)
        print_candles(candles, f"{market} {args.interval}")

        if client.has_auth:
            try:
                accounts = await client.get_accounts()
            except TraderError as exc:
                print(f"=== 잔고 === 조회 실패: {exc}")
                print("  API Key 권한([자산조회])과 허용 IP 등록 여부를 확인하세요.")
                return 1
            print_accounts(accounts)
        else:
            print("=== 잔고 === API Key 가 없어 건너뜀 (.env 에 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 설정)")
    print("점검 완료: 시세·캔들 API 정상.")
    return 0


async def cmd_ticker(settings: Settings, args: argparse.Namespace) -> int:
    markets = args.markets or settings.markets
    async with UpbitClient.from_settings(settings) as client:
        print_tickers(await client.get_tickers(markets))
    return 0


async def cmd_candles(settings: Settings, args: argparse.Namespace) -> int:
    market = args.market or settings.markets[0]
    async with UpbitClient.from_settings(settings) as client:
        candles = await client.get_candles(market, args.interval, count=args.count)
    print_candles(candles, f"{market} {args.interval}")
    return 0


async def cmd_balance(settings: Settings, args: argparse.Namespace) -> int:
    async with UpbitClient.from_settings(settings) as client:
        print_accounts(await client.get_accounts())
    return 0


def _format_ws_message(msg: WsMessage) -> str:
    tag = "S" if getattr(msg, "is_snapshot", False) else "R"
    if isinstance(msg, WsTrade):
        side = "매수" if msg.ask_bid == "BID" else "매도"
        return (f"[체결 {tag}] {msg.code:<9} {side} {_fmt_price(msg.trade_price):>14} x {msg.trade_volume:<12.8f} "
                f"{msg.trade_datetime_utc.astimezone(KST):%H:%M:%S}")
    if isinstance(msg, WsTicker):
        sign = "+" if msg.signed_change_rate >= 0 else ""
        rate = f"{sign}{msg.signed_change_rate * 100:.2f}%"
        return f"[현재가 {tag}] {msg.code:<9} {_fmt_price(msg.trade_price):>14}  {rate}"
    if isinstance(msg, WsOrderbook):
        best = msg.orderbook_units[0] if msg.orderbook_units else None
        if best is None:
            return f"[호가 {tag}] {msg.code:<9} (비어 있음)"
        return (f"[호가 {tag}] {msg.code:<9} 매도1 {_fmt_price(best.ask_price):>14} ({best.ask_size:.4f})  "
                f"매수1 {_fmt_price(best.bid_price):>14} ({best.bid_size:.4f})  스프레드 {msg.spread:,.0f}")
    if isinstance(msg, WsCandle):
        return (f"[캔들 {tag}] {msg.code:<9} {msg.type:<11} {msg.candle_date_time_kst:%H:%M:%S} "
                f"O {_fmt_price(msg.opening_price)} H {_fmt_price(msg.high_price)} "
                f"L {_fmt_price(msg.low_price)} C {_fmt_price(msg.trade_price)} V {msg.candle_acc_trade_volume:.4f}")
    return f"[{type(msg).__name__}] {msg}"


async def cmd_signal(settings: Settings, args: argparse.Namespace) -> int:
    """최근 캔들로 전략 신호를 계산해 보여준다 (REST 조회만, 주문 없음)."""
    market = args.market or settings.markets[0]
    interval = CandleInterval.parse(args.interval or settings.candle_interval)
    params = dict(settings.strategy_params)
    if args.params:
        params.update(parse_params_text(args.params))
    strategy = create_strategy(args.strategy or settings.strategy_name, params)

    count = max(args.count, strategy.warmup_periods + 5)
    now = datetime.now(UTC)
    start = now - timedelta(seconds=interval.seconds * (count + 2))
    async with UpbitClient.from_settings(settings) as client:
        candles = await client.get_candles_range(market, interval, start=start, end=now)
    df = candles_to_dataframe(candles, interval=interval)
    df = drop_unclosed(df, interval, now)
    validate_candles(df, interval=interval)
    anomalies = detect_price_anomalies(df)
    if anomalies.any():
        print(f"[경고] 직전 대비 30% 이상 튄 캔들 {int(anomalies.sum())}개: {list(df.index[anomalies][:3])}")
    check_no_lookahead(strategy, df)

    signal = strategy.generate_signal(df, market=market)
    print(f"=== 전략 신호: {strategy.name} {strategy.params.model_dump()} / {market} {interval.value} ===")
    first, last = df.index[0].astimezone(KST), df.index[-1].astimezone(KST)
    print(f"  캔들 {len(df)}개 ({first:%Y-%m-%d %H:%M} ~ {last:%Y-%m-%d %H:%M} KST, 닫힌 캔들만)")
    print(f"  마지막 캔들 종가 {_fmt_price(signal.price)}  →  신호 [{signal.action.value}]  {signal.reason}")
    for key, value in signal.indicators.items():
        print(f"    {key:<14} {value:,.4f}")
    events = strategy.scan(df, market=market)
    print(f"=== 구간 내 BUY/SELL 신호 {len(events)}개 (최근 {min(len(events), args.history)}개) ===")
    for s in events[-args.history:]:
        print(f"  {s.time.astimezone(KST):%Y-%m-%d %H:%M}  {s.action.value:<4} {_fmt_price(s.price):>14}  {s.reason}")
    print("Look-ahead 검사 통과. 이 신호는 참고용이며 수익을 보장하지 않습니다.")
    return 0


def parse_kst_datetime(text: str) -> datetime:
    """'2026-01-01' 또는 ISO 8601. 시간대가 없으면 KST 로 해석한다."""
    value = datetime.fromisoformat(text)
    return value.replace(tzinfo=KST) if value.tzinfo is None else value


async def cmd_backtest(settings: Settings, args: argparse.Namespace) -> int:
    """과거 캔들로 전략을 시뮬레이션한다 (실제 주문 없음)."""
    market = args.market or settings.markets[0]
    interval = CandleInterval.parse(args.interval or settings.candle_interval)
    params = dict(settings.strategy_params)
    if args.params:
        params.update(parse_params_text(args.params))
    strategy = create_strategy(args.strategy or settings.strategy_name, params)
    start = parse_kst_datetime(args.start)
    end = parse_kst_datetime(args.end) if args.end else datetime.now(UTC)
    base_risk = settings.risk_config() if args.env_risk else RiskConfig.unrestricted()
    overrides = {
        "position_fraction": args.position_fraction, "stop_loss_pct": args.stop_loss,
        "take_profit_pct": args.take_profit, "trailing_stop_pct": args.trailing_stop,
        "daily_loss_limit_pct": args.daily_loss_limit, "max_consecutive_losses": args.max_consecutive_losses,
        "max_order_amount": args.max_order_amount, "max_position_ratio": args.max_position_ratio,
    }
    risk_cfg = base_risk.model_copy(update={k: v for k, v in overrides.items() if v is not None})
    config = BacktestConfig(
        initial_capital=args.capital, fee_rate=args.fee, slippage_rate=args.slippage,
        position_fraction=risk_cfg.position_fraction, risk=risk_cfg,
    )
    df = await load_candles(settings, market, interval, start, end, csv=args.csv)
    result = BacktestEngine(config).run(df, strategy, market=market, interval=interval)
    print(format_report(result, max_trades=args.trades))
    if not args.no_save:
        out = Path(args.out_dir) / f"{datetime.now(KST):%Y%m%d_%H%M%S}_{market}_{interval.value}_{strategy.name}"
        save_result(result, out)
        print(f"결과 저장: {out} (summary.json, trades.csv, equity.csv, signals.csv)")
    return 0


def _kst_str(value: datetime | None) -> str:
    return from_db_time(value).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S") if value else "-"


def load_runtime_settings(
    settings: Settings, repo: Repository, overrides: dict | None = None
) -> tuple[RuntimeSettings, int]:
    """DB 의 최신 실행 설정을 읽고, 없으면 .env 값으로 버전 1 을 만든다. CLI 로 넘긴 값은 이번 실행에만 덮어쓴다."""
    loaded = repo.load_runtime_settings()
    if loaded is not None:
        data, version = loaded
        try:
            runtime = RuntimeSettings(**data)
        except Exception as exc:  # noqa: BLE001 - 잘못된 저장값은 .env 로 대체
            print(f"[경고] DB 실행 설정 v{version} 이 잘못되어 .env 값을 사용합니다: {exc}", file=sys.stderr)
            runtime = RuntimeSettings.from_settings(settings)
    else:
        runtime = RuntimeSettings.from_settings(settings)
        version = repo.save_runtime_settings(runtime.to_dict(), note=".env 초기값")
    if overrides:
        runtime = RuntimeSettings(**{**runtime.to_dict(), **overrides})
    return runtime, version


def build_paper_components(settings: Settings):
    """DB · 저장소 · 모의 계좌(복구 포함) · 브로커 · 리스크를 만든다."""
    db = Database(settings.database_url)
    db.create_all()
    repo = Repository(db, mode="paper")
    portfolio, restored = repo.restore_portfolio(
        initial_cash=settings.paper_initial_cash, fee_rate=settings.paper_fee_rate,
        min_order_amount=DEFAULT_MIN_ORDER_AMOUNT,
    )
    broker = PaperBroker(
        portfolio, slippage_rate=settings.paper_slippage_rate,
        max_price_age_seconds=settings.price_max_age_seconds, processed_client_ids=repo.processed_client_ids(),
    )
    risk = RiskManager(settings.risk_config())
    return db, repo, portfolio, broker, risk, restored


async def build_live_components(settings: Settings, client: UpbitClient, markets: list[str]):
    """LIVE: 거래소 잔고로 계좌를 만들고 LiveBroker 를 붙인다. 이중 플래그·키가 없으면 여기까지 오지 못한다."""
    db = Database(settings.database_url)
    db.create_all()
    repo = Repository(db, mode="live")
    accounts = await client.get_accounts()
    account_record = repo.load_account()
    reference_prices: dict[str, float] = {}
    try:  # avg_buy_price=0 인 코인의 기준가·먼지 잔고 판정용 현재가 (실패해도 시작은 한다 — 첫 시세에서 보정)
        reference_prices = {t.market: float(t.trade_price) for t in await client.get_tickers(markets)}
    except TraderError as exc:
        log.warning("현재가 조회 실패 (기준가 없이 시작): %s", exc)
    portfolio = portfolio_from_accounts(
        accounts, markets, fee_rate=settings.paper_fee_rate,
        initial_cash=account_record.initial_cash if account_record else None,
        reference_prices=reference_prices, min_order_amount=DEFAULT_MIN_ORDER_AMOUNT,
    )
    if account_record is not None:
        # 재시작: 잔고 스냅샷에는 없는 진입 시각·매수 수수료·기준가를 이전 실행의 DB 포지션에서 되살린다 (감사 LOW-8)
        saved, _ = repo.restore_portfolio(
            initial_cash=account_record.initial_cash, fee_rate=settings.paper_fee_rate,
            min_order_amount=DEFAULT_MIN_ORDER_AMOUNT,
        )
        restored = merge_saved_positions(portfolio, saved.positions)
        if restored:
            log.info("저장된 포지션 정보 복구(진입 시각·수수료·기준가): %s", restored)
    broker = LiveBroker(client, portfolio, settings, processed_client_ids=repo.processed_client_ids())
    risk = RiskManager(settings.risk_config())
    return db, repo, portfolio, broker, risk, account_record is not None


def install_stop_handlers(engine: TradingEngine) -> list[str]:
    """SIGTERM/SIGINT(POSIX)·CTRL_BREAK(Windows)를 받으면 엔진을 **정상 종료**시킨다 (감사 LOW-10).

    정상 종료 = stop 이벤트 → 루프 종료 → finally 에서 마지막 스냅샷·bot_stop 알림·STOPPED 하트비트.
    설치한 시그널 이름을 돌려준다. Windows 의 강제 종료(taskkill /F)는 잡을 수 없으므로 대시보드는 먼저 CTRL_BREAK 를
    보낸다(app.api.process.kill_engine).
    """
    loop = asyncio.get_running_loop()
    installed: list[str] = []

    def request_stop(name: str) -> None:
        log.warning("%s 수신 → 엔진 정상 종료 요청", name)
        engine.stop()

    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, request_stop, sig.name)
                installed.append(sig.name)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
    else:
        for sig in (getattr(signal, "SIGBREAK", None), signal.SIGTERM):
            if sig is None:
                continue
            try:
                signal.signal(sig, lambda *_args, _name=sig.name: loop.call_soon_threadsafe(request_stop, _name))
                installed.append(sig.name)
            except (ValueError, OSError):
                continue
    return installed


def acquire_engine_lock(mode: str, base_dir: Path | None = None) -> InstanceLock | None:
    """같은 모드의 엔진이 이미 떠 있으면 None (감사 HIGH-7). 잠금 파일: data/engine-{mode}.lock"""
    lock = InstanceLock((base_dir or PROJECT_ROOT / "data") / f"engine-{mode}.lock")
    return lock if lock.acquire() else None


async def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    """매매 루프. PAPER 는 가상 체결, LIVE 는 이중 플래그 + --confirm-live 가 모두 있어야 실제 주문."""
    if settings.trading_mode is TradingMode.BACKTEST:
        print("run 은 PAPER/LIVE 전용입니다. BACKTEST 는 backtest 명령을 쓰세요.", file=sys.stderr)
        return 2
    print_settings(settings)
    live = settings.trading_mode is TradingMode.LIVE
    if live:
        if not settings.is_live_trading_allowed:
            print("LIVE 모드는 TRADING_MODE=LIVE 와 LIVE_TRADING_ENABLED=true 가 모두 필요합니다.", file=sys.stderr)
            return 2
        if not settings.has_api_keys:
            print("LIVE 모드에는 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 가 필요합니다.", file=sys.stderr)
            return 2
        if args.confirm_live != LIVE_CONFIRM_PHRASE:
            print(
                f"실제 자금 거래입니다. 실행하려면 --confirm-live {LIVE_CONFIRM_PHRASE} 를 붙이세요 "
                "(백테스트·모의매매 검증을 먼저 마쳤는지 확인).",
                file=sys.stderr,
            )
            return 2
        print("!!! LIVE 모드: 실제 자금이 거래됩니다. 소액으로 시작하고 status/로그를 계속 확인하세요 !!!")

    lock = acquire_engine_lock("live" if live else "paper")
    if lock is None:
        holder = InstanceLock(PROJECT_ROOT / "data" / f"engine-{'live' if live else 'paper'}.lock").holder()
        print(f"같은 모드의 엔진이 이미 실행 중입니다 (pid·시작시각: {holder or '알 수 없음'}). 먼저 정지하세요.",
              file=sys.stderr)
        return 3
    try:
        async with UpbitClient.from_settings(settings) as client:
            mode = "live" if live else "paper"
            settings_db = Database(settings.database_url)
            settings_db.create_all()
            overrides: dict = {}
            if args.markets:
                overrides["markets"] = args.markets
            if args.strategy:
                overrides["strategy_name"] = args.strategy
            if args.interval:
                overrides["candle_interval"] = args.interval
            if args.params:
                overrides["strategy_params"] = parse_params_text(args.params)
            runtime, version = load_runtime_settings(settings, Repository(settings_db, mode=mode), overrides)
            settings_db.dispose()
            markets = runtime.markets
            interval = runtime.interval
            strategy = runtime.build_strategy()
            if live:
                db, repo, portfolio, broker, risk, restored = await build_live_components(settings, client, markets)
            else:
                db, repo, portfolio, broker, risk, restored = build_paper_components(settings)
            risk = RiskManager(runtime.risk, store=RepositoryRiskStateStore(repo))  # 재시작해도 잠금·긴급 정지 유지
            try:
                override_note = ", CLI 값으로 일부 덮어씀" if overrides else ""
                print(f"  실행 설정 v{version} (DB bot_settings{override_note})")
                label = "실거래" if live else "모의매매"
                print(f"=== {label} 시작: {strategy.name} {strategy.params.model_dump()} ===")
                print(f"  대상 {', '.join(markets)} {interval.value}")
                source = "거래소 잔고 기준" if live else ("DB 에서 복구" if restored else "새로 시작")
                print(
                    f"  계좌: 현금 {portfolio.cash:,.0f} KRW, 포지션 {list(portfolio.positions) or '없음'}"
                    f" ({source}), DB {settings.database_url}"
                )
                print("  리스크: " + ", ".join(f"{k} {v}" for k, v in risk.config.describe().items()))
                stop_hint = "  Ctrl+C 로 종료 (control stop 도 가능)"
                if args.duration:
                    stop_hint = f"  {args.duration:.0f}초 후 종료"
                print(stop_hint)
                notifier = build_notification_manager(
                    settings,
                    on_failure=lambda event, error: repo.log(
                        "WARNING", "notify_failed", error, {"kind": event.kind.value, "title": event.title}
                    ),
                )
                if notifier is not None:
                    print(f"  알림: {", ".join(notifier.channels)} (이벤트 {len(notifier.enabled)}종)")
                else:
                    print("  알림: 없음 (NOTIFY_LOG=true 또는 TELEGRAM_* / DISCORD_WEBHOOK_URL 설정 시 발송)")
                engine = TradingEngine(
                    settings, strategy=strategy, portfolio=portfolio, broker=broker, risk=risk, repo=repo,
                    client=client, markets=markets, interval=interval,
                    ws_factory=lambda subs: UpbitWebSocket(subs, url=settings.upbit_ws_url),
                    runtime=runtime, settings_version=version, notifier=notifier,
                )
                installed = install_stop_handlers(engine)
                if installed:
                    log.info("종료 시그널 처리 설치: %s", ", ".join(installed))
                stats = await engine.run(duration_seconds=args.duration or None)
            finally:
                db.dispose()
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    finally:
        lock.release()
    print(f"=== {'실거래' if live else '모의매매'} 종료 ===")
    for key, value in stats.__dict__.items():
        if key != "extra":
            print(f"  {key:<20} {value}")
    print(
        f"  현금 {portfolio.cash:,.0f} KRW, 실현손익 {sum(t.pnl for t in portfolio.trades):,.0f} KRW, "
        f"수수료 {portfolio.fees_paid:,.0f} KRW, 포지션 {list(portfolio.positions) or '없음'}"
    )
    return 0


async def cmd_order_test(settings: Settings, args: argparse.Namespace) -> int:
    """업비트 주문 생성 테스트 API 로 키·권한·파라미터를 검증한다. 실제 주문은 만들지 않는다."""
    if not settings.has_api_keys:
        print("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 가 필요합니다.", file=sys.stderr)
        return 2
    market = args.market or settings.markets[0]
    async with UpbitClient.from_settings(settings) as client:
        chance = await client.get_order_chance(market)
        print(f"=== 주문 가능 정보 {market} ===")
        print(f"  상태 {chance.market_state}, 매수 수수료 {chance.bid_fee}, 매도 수수료 {chance.ask_fee}, "
              f"최소 주문 {chance.min_total_bid} KRW, 매수 유형 {chance.bid_types}, 매도 유형 {chance.ask_types}")
        base = chance.ask_account
        print(f"  KRW 잔고 {chance.bid_account.balance}, {base.currency} 잔고 {base.balance}")
        if args.side == "buy":
            params = client.market_buy_params(market, args.amount)
        else:
            params = client.market_sell_params(market, args.volume)
        print(f"=== 주문 테스트 (POST /v1/orders/test, 실제 주문 아님) === {params}")
        info = await client.test_order(params)
        print(f"  결과: state={info.state} uuid={info.uuid} side={info.side} ord_type={info.ord_type}")
        print("  키·권한·파라미터 검증 통과. 실제 주문 코드는 별도 확인 절차(--confirm-live)를 거쳐야 동작합니다.")
    return 0


async def cmd_control(settings: Settings, args: argparse.Namespace) -> int:
    """실행 중인 엔진에 명령을 보낸다 (DB 큐). 엔진은 2초마다 큐를 읽는다."""
    db = Database(settings.database_url)
    db.create_all()
    repo = Repository(db, mode=args.mode)
    try:
        status = repo.read_engine_status()
        if status is None or status.status in ("STOPPED",):
            last = status.status if status else "없음"
            print(f"[{args.mode}] 실행 중인 엔진 기록이 없습니다 (마지막 상태: {last}). 명령은 큐에 남습니다.")
        cmd_id = repo.enqueue_command(args.command.replace("-", "_"), {"reason": args.reason} if args.reason else None)
        print(f"[{args.mode}] 명령 #{cmd_id} '{args.command}' 를 큐에 넣었습니다. 처리 결과는 status 로 확인하세요.")
    finally:
        db.dispose()
    return 0


async def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    """DB 에 기록된 엔진 상태·계좌·신호·주문을 보여준다."""
    db = Database(settings.database_url)
    db.create_all()
    repo = Repository(db, mode=args.mode)
    try:
        engine_status = repo.read_engine_status()
        print(f"=== 엔진 상태 [{args.mode}] (DB {settings.database_url}) ===")
        if engine_status is None:
            print("  기록 없음")
        else:
            es = engine_status
            print(
                f"  {es.status} | 전략 {es.strategy} {es.markets} {es.interval} | "
                f"WebSocket {es.ws_status} | 갱신 {_kst_str(es.updated_at)}"
            )
            print(
                f"  마지막 시세 {_kst_str(es.last_data_at)} | 마지막 캔들 점검 {_kst_str(es.last_candle_at)} | "
                f"마지막 거래 {_kst_str(es.last_trade_at)} | 평가 {es.equity or 0:,.0f} KRW"
            )
            if es.risk:
                r = es.risk
                print(
                    f"  리스크: 연속손실 {r.get('consecutive_losses')} 당일실현 {r.get('daily_realized_pnl', 0):,.0f} "
                    f"잠금 {r.get('lock_reason') or '없음'} 긴급정지 {r.get('halted')}"
                )
        account = repo.load_account()
        print(f"=== 계좌 [{args.mode}] ===")
        if account is None:
            print("  기록 없음 — 아직 run 을 실행하지 않았습니다.")
            return 0
        print(
            f"  초기 {account.initial_cash:,.0f} KRW → 현금 {account.cash:,.0f} KRW, "
            f"수수료 누계 {account.fees_paid:,.0f} KRW (갱신 {_kst_str(account.updated_at)})"
        )
        positions = repo.load_positions()
        print("=== 보유 포지션 ===")
        if not positions:
            print("  없음")
        for p in positions:
            print(
                f"  {p.market:<10} {p.quantity:.8f} @ {p.avg_price:,.0f} "
                f"(매수 {p.entry_amount:,.0f} KRW, {_kst_str(p.opened_at)})"
            )
        bal = repo.latest_balance()
        if bal:
            print(
                f"=== 최근 자산 스냅샷 {_kst_str(bal.time)} === 평가 {bal.equity:,.0f} KRW "
                f"(현금 {bal.cash:,.0f} + 코인 {bal.positions_value:,.0f}), "
                f"실현 {bal.realized_pnl:,.0f}, 미실현 {bal.unrealized_pnl:,.0f}"
            )
        print("=== 최근 신호 ===")
        for s in repo.recent_signals(args.limit):
            print(f"  {_kst_str(s.time)} {s.market:<9} {s.action:<4} {s.price:,.0f} {s.reason}")
        print("=== 최근 주문 ===")
        for o in repo.recent_orders(args.limit):
            price = f"{o.fill_price:,.0f}" if o.fill_price else "-"
            note = o.error or o.reason
            print(f"  {_kst_str(o.created_at)} {o.market:<9} {o.side:<4} {o.status:<8} {price:>14} {note}")
        print("=== 최근 왕복 거래 ===")
        for r in repo.recent_round_trips(args.limit):
            print(
                f"  {_kst_str(r.entry_time)} → {_kst_str(r.exit_time)} {r.market:<9} "
                f"손익 {r.pnl:,.0f} KRW ({r.pnl_pct * 100:+.2f}%) {r.exit_reason}"
            )
        print("=== 최근 로그 ===")
        for entry in repo.recent_logs(args.limit):
            print(f"  {_kst_str(entry.time)} [{entry.level}] {entry.event}: {entry.message}")
    finally:
        db.dispose()
    return 0


async def cmd_notify_test(settings: Settings, args: argparse.Namespace) -> int:
    """설정된 알림 채널 전부에 테스트 메시지를 보낸다. 실패한 채널이 있으면 종료 코드 1."""
    if args.discover_telegram:
        return await _discover_telegram_chats(settings)
    manager = build_notification_manager(settings, include_log=args.include_log or None)
    if manager is None:
        print("알림 채널이 없습니다. .env 의 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 또는 DISCORD_WEBHOOK_URL 설정 필요")
        return 2
    event = NotificationEvent(
        kind=EventKind.BOT_START, title="알림 테스트", message=args.message or "upbit-auto-trader 알림 채널 연결 확인",
        mode=settings.trading_mode.value.lower(),
    )
    print(f"=== 알림 테스트: {", ".join(manager.channels)} ===")
    try:
        results = await manager.send_now(event)
    finally:
        await manager.close()
    for channel, error in results.items():
        print(f"  {channel:<9} {"OK" if error is None else "실패 — " + error}")
    print(f"  보낼 이벤트: {", ".join(k.value for k in settings.notify_event_kinds()) or "없음(off)"}")
    return 0 if all(error is None for error in results.values()) else 1


async def _discover_telegram_chats(settings: Settings) -> int:
    """TELEGRAM_BOT_TOKEN 만 있으면 봇이 본 채널·그룹·개인 대화의 chat id 를 찾아 준다."""
    if settings.telegram_bot_token is None:
        print("TELEGRAM_BOT_TOKEN 이 .env 에 없습니다. @BotFather 에서 봇을 만들고 토큰을 넣은 뒤 다시 실행하세요.")
        return 2
    token = settings.telegram_bot_token.get_secret_value()
    try:
        me = await get_me(token, timeout_seconds=settings.http_timeout_seconds)
        print(f"토큰의 봇: @{me['username']} ({me['name']}) — 채널 관리자에 추가한 봇과 같은지 확인하세요")
        chats = await discover_chats(token, timeout_seconds=settings.http_timeout_seconds)
    except NotifyError as exc:
        print(f"조회 실패: {exc}")
        return 1
    current = (settings.telegram_chat_id or "").strip()
    if current and not current.lstrip("-").isdigit():
        print(f"경고: 현재 TELEGRAM_CHAT_ID 는 숫자가 아닙니다({len(current)}자). "
              "채널 ID 는 -100 으로 시작하는 숫자입니다")
    if not chats:
        print("봇이 본 대화가 없습니다. 채널이면 봇을 채널 '관리자' 로 추가하고 메시지를 하나 올린 뒤, "
              "개인 대화면 봇에게 /start 를 보낸 뒤 다시 실행하세요.")
        return 1
    print("=== 봇이 본 대화 (TELEGRAM_CHAT_ID 에 넣을 값) ===")
    for chat in chats:
        kinds = {"channel": "채널", "supergroup": "그룹", "group": "그룹", "private": "개인"}
        kind = kinds.get(chat["type"], chat["type"])
        tail = f"  · 최근 메시지: {chat['last_text']}" if chat["last_text"] else ""
        print(f"  {chat['id']:<16} {kind:<4} {chat['title']}{tail}")
    print("원하는 대화의 숫자 ID 를 .env 의 TELEGRAM_CHAT_ID 에 넣고 `notify-test` 로 발송을 확인하세요.")
    return 0


async def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    """대시보드 웹 서버 (엔진과 별도 프로세스). 매매는 하지 않는다."""
    import uvicorn

    from app.api.server import create_app

    host = args.host or settings.dashboard_host
    port = args.port or settings.dashboard_port
    if host not in ("127.0.0.1", "localhost", "::1") and settings.dashboard_token is None:
        print("외부 인터페이스에 바인드하려면 .env 에 DASHBOARD_TOKEN 을 설정하세요.", file=sys.stderr)
        return 2
    print(f"대시보드: http://{host}:{port}  (모드 표시는 화면에서 선택, 엔진 시작은 제어 탭)")
    config = uvicorn.Config(create_app(settings), host=host, port=port, log_level="info", access_log=False)
    await uvicorn.Server(config).serve()
    return 0


async def cmd_stream(settings: Settings, args: argparse.Namespace) -> int:
    """실시간 스트림을 화면에 출력한다 (Public 엔드포인트, 주문 없음)."""
    markets = args.markets or settings.markets
    subscriptions: list[Subscription] = []
    for kind in [t.strip() for t in args.types.split(",") if t.strip()]:
        if kind == "ticker":
            subscriptions.append(Subscription.ticker(markets))
        elif kind == "trade":
            subscriptions.append(Subscription.trade(markets))
        elif kind == "orderbook":
            subscriptions.append(Subscription.orderbook(markets, units=5))
        elif kind.startswith("candle"):
            interval = kind.split(".", 1)[1] if "." in kind else "1m"
            subscriptions.append(Subscription.candle(markets, interval))
        else:
            print(f"알 수 없는 타입: {kind} (ticker, trade, orderbook, candle.1m 등)", file=sys.stderr)
            return 2

    ws = UpbitWebSocket(subscriptions, url=settings.upbit_ws_url)
    types = ", ".join(s.type for s in subscriptions)
    print(f"=== 실시간 스트림 {settings.upbit_ws_url} — {types} / {', '.join(markets)} ===")
    print(f"  {args.seconds}초 또는 {args.max_messages}개 메시지 후 종료 (Ctrl+C 로 중단)")
    deadline = asyncio.get_running_loop().time() + args.seconds if args.seconds > 0 else None
    count = 0
    try:
        async with asyncio.timeout(args.seconds if args.seconds > 0 else None):
            async for msg in ws.stream():
                print(_format_ws_message(msg))
                count += 1
                if args.max_messages and count >= args.max_messages:
                    break
                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    break
    except TimeoutError:
        pass
    finally:
        await ws.close()
    s = ws.stats
    print(
        f"수신 {s.messages}개 (타입별 {s.extra}), 연결 {s.connects}회, 재연결 {s.reconnects}회, "
        f"해석 실패 {s.parse_errors}개"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="upbit-auto-trader", description="업비트 자동매매 (Phase 1: 연결 점검)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="설정·시세·캔들·잔고 점검 (주문 없음)")
    p_check.add_argument("--interval", default="60m", help="캔들 단위 (1s,1m,3m,5m,10m,15m,30m,60m,240m,1d,1w,1M,1y)")
    p_check.add_argument("--count", type=int, default=5)
    p_check.set_defaults(func=cmd_check)

    p_ticker = sub.add_parser("ticker", help="현재가 조회")
    p_ticker.add_argument("markets", nargs="*", help="마켓 코드 (생략 시 설정의 MARKETS)")
    p_ticker.set_defaults(func=cmd_ticker)

    p_candles = sub.add_parser("candles", help="캔들 조회")
    p_candles.add_argument("market", nargs="?", help="마켓 코드 (생략 시 설정의 첫 마켓)")
    p_candles.add_argument("--interval", default="60m")
    p_candles.add_argument("--count", type=int, default=10)
    p_candles.set_defaults(func=cmd_candles)

    p_balance = sub.add_parser("balance", help="잔고 조회 (API Key 필요)")
    p_balance.set_defaults(func=cmd_balance)

    p_signal = sub.add_parser("signal", help="최근 캔들로 전략 신호 계산 (주문 없음)")
    p_signal.add_argument("market", nargs="?", help="마켓 코드 (생략 시 설정의 첫 마켓)")
    p_signal.add_argument("--strategy", help="전략 이름: ma_cross, rsi (생략 시 STRATEGY_NAME)")
    p_signal.add_argument("--interval", help="캔들 단위 (생략 시 CANDLE_INTERVAL)")
    p_signal.add_argument("--count", type=int, default=300, help="조회할 닫힌 캔들 수")
    p_signal.add_argument("--params", help="전략 파라미터: window=10,oversold=25 형식 또는 JSON 객체")
    p_signal.add_argument("--history", type=int, default=10, help="표시할 최근 신호 개수")
    p_signal.set_defaults(func=cmd_signal)

    p_bt = sub.add_parser("backtest", help="과거 캔들로 전략 백테스트 (주문 없음)")
    p_bt.add_argument("market", nargs="?", help="마켓 코드 (생략 시 설정의 첫 마켓)")
    p_bt.add_argument("--strategy", help="전략 이름 (생략 시 STRATEGY_NAME)")
    p_bt.add_argument("--interval", help="캔들 단위 (생략 시 CANDLE_INTERVAL)")
    p_bt.add_argument("--start", required=True, help="시작 (KST), 예: 2026-01-01")
    p_bt.add_argument("--end", help="종료 (KST). 생략 시 현재")
    p_bt.add_argument("--capital", type=float, default=1_000_000, help="초기 자본 KRW (기본 1,000,000)")
    p_bt.add_argument("--fee", type=float, default=0.0005, help="편도 수수료율 (기본 0.0005 = 0.05%%)")
    p_bt.add_argument("--slippage", type=float, default=0.0005, help="시장가 슬리피지 비율 (기본 0.0005)")
    p_bt.add_argument("--position-fraction", type=float, help="매수 시 현금 사용 비율 (기본 1.0)")
    p_bt.add_argument("--stop-loss", type=float, help="손절 비율, 예: 0.05")
    p_bt.add_argument("--take-profit", type=float, help="익절 비율, 예: 0.10")
    p_bt.add_argument("--trailing-stop", type=float, help="추적 손절 비율 (보유 중 최고가 대비), 예: 0.03")
    p_bt.add_argument("--daily-loss-limit", type=float, help="일일 손실 한도 비율, 예: 0.03")
    p_bt.add_argument("--max-consecutive-losses", type=int, help="연속 손실 한도, 예: 3")
    p_bt.add_argument("--max-order-amount", type=float, help="거래당 최대 투자금 KRW")
    p_bt.add_argument("--max-position-ratio", type=float, help="자산 대비 포지션 상한 비율")
    p_bt.add_argument("--env-risk", action="store_true", help=".env 의 RISK_* 설정을 기본으로 사용 (기본은 제한 없음)")
    p_bt.add_argument("--params", help="전략 파라미터: short_window=20,long_window=60 또는 JSON")
    p_bt.add_argument("--csv", help="캔들 CSV 파일 (생략 시 API 조회 + data/cache 캐시)")
    p_bt.add_argument("--out-dir", default="data/backtests", help="결과 저장 폴더")
    p_bt.add_argument("--no-save", action="store_true", help="결과 파일을 저장하지 않음")
    p_bt.add_argument("--trades", type=int, default=10, help="보고서에 표시할 최근 거래 수")
    p_bt.set_defaults(func=cmd_backtest)

    p_run = sub.add_parser("run", help="모의매매 실행 (PAPER 전용, 실제 주문 없음)")
    p_run.add_argument("markets", nargs="*", help="마켓 코드 (생략 시 설정의 MARKETS)")
    p_run.add_argument("--strategy", help="전략 이름 (생략 시 STRATEGY_NAME)")
    p_run.add_argument("--interval", help="캔들 단위 (생략 시 CANDLE_INTERVAL)")
    p_run.add_argument("--params", help="전략 파라미터: key=value,... 또는 JSON")
    p_run.add_argument("--duration", type=float, default=0, help="실행 시간(초). 0 이면 Ctrl+C 까지")
    p_run.set_defaults(func=cmd_run)

    p_run.add_argument("--confirm-live", default="", help=f"LIVE 실행 확인 문구 ({LIVE_CONFIRM_PHRASE})")

    p_status = sub.add_parser("status", help="DB 에 기록된 엔진 상태·계좌 조회")
    p_status.add_argument("--limit", type=int, default=5)
    p_status.add_argument("--mode", choices=["paper", "live"], default="paper")
    p_status.set_defaults(func=cmd_status)

    p_ctl = sub.add_parser("control", help="실행 중인 엔진에 명령 전송 (pause / resume / stop / halt / resume-risk)")
    p_ctl.add_argument("command", choices=["pause", "resume", "stop", "halt", "resume-risk", "reload"])
    p_ctl.add_argument("--mode", choices=["paper", "live"], default="paper")
    p_ctl.add_argument("--reason", help="halt 사유")
    p_ctl.set_defaults(func=cmd_control)

    p_ot = sub.add_parser("order-test", help="주문 생성 테스트 API 로 키·권한 검증 (실제 주문 없음)")
    p_ot.add_argument("market", nargs="?", help="마켓 코드 (생략 시 설정의 첫 마켓)")
    p_ot.add_argument("--side", choices=["buy", "sell"], default="buy")
    p_ot.add_argument("--amount", type=float, default=5000, help="매수 테스트 금액 KRW (기본 5000)")
    p_ot.add_argument("--volume", type=float, default=0.0001, help="매도 테스트 수량")
    p_ot.set_defaults(func=cmd_order_test)

    p_nt = sub.add_parser("notify-test", help="알림 채널(Telegram / Discord) 에 테스트 메시지 발송")
    p_nt.add_argument("--message", help="보낼 본문 (기본: 연결 확인 문구)")
    p_nt.add_argument("--include-log", action="store_true", help="NOTIFY_LOG 설정과 무관하게 로그 채널도 포함")
    p_nt.add_argument("--discover-telegram", action="store_true",
                      help="봇 토큰만으로 채널·그룹 chat id 찾기 (TELEGRAM_CHAT_ID 설정용)")
    p_nt.set_defaults(func=cmd_notify_test)

    p_serve = sub.add_parser("serve", help="대시보드 웹 서버 실행 (기본 http://127.0.0.1:8000)")
    p_serve.add_argument("--host", help="바인드 주소 (기본 DASHBOARD_HOST=127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="포트 (기본 DASHBOARD_PORT=8000)")
    p_serve.set_defaults(func=cmd_serve)

    p_stream = sub.add_parser("stream", help="WebSocket 실시간 시세 출력 (주문 없음)")
    p_stream.add_argument("markets", nargs="*", help="마켓 코드 (생략 시 설정의 MARKETS)")
    p_stream.add_argument(
        "--types", default="trade,orderbook", help="쉼표 구분: ticker, trade, orderbook, candle.1m 등"
    )
    p_stream.add_argument("--seconds", type=float, default=10, help="실행 시간(초). 0 이면 Ctrl+C 까지")
    p_stream.add_argument("--max-messages", type=int, default=0, help="이 개수만큼 받으면 종료 (0 = 제한 없음)")
    p_stream.set_defaults(func=cmd_stream)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except Exception as exc:  # pydantic ValidationError 등 설정 오류
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    log_dir = settings.log_dir if settings.log_dir.is_absolute() else PROJECT_ROOT / settings.log_dir
    setup_logging(settings.log_level, log_dir)
    log.info(
        "시작: command=%s mode=%s live_allowed=%s",
        args.command, settings.trading_mode.value, settings.is_live_trading_allowed,
    )
    try:
        return asyncio.run(args.func(settings, args))
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    except TraderError as exc:
        print(f"실패: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단됨", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 예상 못 한 예외도 추적 로그를 남기고 종료 코드로 알린다
        log.exception("치명적 오류로 종료")
        print(f"치명적 오류로 종료: {type(exc).__name__}: {exc} (logs/ 확인)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
