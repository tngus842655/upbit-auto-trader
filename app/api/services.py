"""대시보드 서비스 — DB 와 공개 시세로 화면용 데이터를 만든다. **매매 판단은 하지 않는다.**

- 성과: ``balances`` 스냅샷 시계열 + 현재 평가액(포지션 × 현재가)로 누적 수익률, 오늘 수익률, MDD 를 계산한다.
- 현재가: 공개 REST(``/v1/ticker``)를 2초 캐시로 조회한다 (Rate Limit 10회/초 대비 충분히 느림).
- 포켓: 메인포켓 키(포켓관리)가 있으면 포켓 목록·잔고·양방향 이전, 봇 키에 [자산이전] 권한이 있으면 봇→메인 이전.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.backtest.metrics import max_drawdown
from app.config.settings import Settings
from app.core.exceptions import TraderError, UpbitAPIError
from app.database.database import Database
from app.database.models import from_db_time
from app.database.repository import Repository
from app.exchange.models import KST
from app.exchange.upbit_client import UpbitClient

log = logging.getLogger(__name__)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    dt = from_db_time(value) if value.tzinfo is None else value
    return dt.astimezone(KST).isoformat()


class PriceCache:
    """공개 현재가를 짧게 캐시한다."""

    def __init__(self, client: UpbitClient, ttl: float = 2.0) -> None:
        self.client = client
        self.ttl = ttl
        self._prices: dict[str, float] = {}
        self._fetched_at = 0.0
        self._markets: tuple[str, ...] = ()

    async def get(self, markets: list[str]) -> dict[str, float]:
        markets = sorted(set(markets))
        if not markets:
            return {}
        stale = time.monotonic() - self._fetched_at > self.ttl or tuple(markets) != self._markets
        if stale:
            try:
                tickers = await self.client.get_tickers(markets)
                self._prices = {t.market: t.trade_price for t in tickers}
                self._markets = tuple(markets)
                self._fetched_at = time.monotonic()
            except TraderError as exc:
                log.warning("현재가 조회 실패(캐시 사용): %s", exc)
        return dict(self._prices)


class DashboardService:
    def __init__(self, settings: Settings, db: Database, public_client: UpbitClient) -> None:
        self.settings = settings
        self.db = db
        self.prices = PriceCache(public_client)
        self.public_client = public_client
        self._repos: dict[str, Repository] = {}
        self._market_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def repo(self, mode: str) -> Repository:
        if mode not in ("paper", "live"):
            raise ValueError("mode 는 paper 또는 live")
        if mode not in self._repos:
            self._repos[mode] = Repository(self.db, mode=mode)
        return self._repos[mode]

    # ------------------------------------------------------------------
    # 마켓 카탈로그 (설정 탭의 코인 선택 팝업)
    # ------------------------------------------------------------------
    MARKET_CACHE_SECONDS = 60.0

    async def markets(self, quote: str = "KRW", *, force: bool = False) -> dict[str, Any]:
        """거래 가능한 페어 목록 + 현재가·24시간 등락률·거래대금·유의/주의 표시. 60초 캐시.

        시가총액은 업비트 공개 API 가 제공하지 않으므로 24시간 거래대금(유동성)을 대신 보여준다.
        """
        quote = quote.upper()
        now = time.monotonic()
        cached = self._market_cache.get(quote)
        if cached is not None and not force and now - cached[0] < self.MARKET_CACHE_SECONDS:
            return cached[1]
        pairs = [m for m in await self.public_client.get_markets(is_details=True) if m.quote_currency == quote]
        tickers = {t.market: t for t in await self.public_client.get_quote_tickers(quote)} if pairs else {}
        items: list[dict[str, Any]] = []
        for m in pairs:
            t = tickers.get(m.market)
            event = m.market_event or {}
            caution = event.get("caution") or {}
            items.append({
                "market": m.market, "base": m.base_currency, "korean_name": m.korean_name,
                "english_name": m.english_name,
                "trade_price": t.trade_price if t else None,
                "change_rate": t.signed_change_rate if t else None,
                "acc_trade_price_24h": t.acc_trade_price_24h if t else None,
                "acc_trade_volume_24h": t.acc_trade_volume_24h if t else None,
                "warning": bool(event.get("warning")),
                "caution": sorted(k for k, v in caution.items() if v) if isinstance(caution, dict) else [],
            })
        items.sort(key=lambda r: r["acc_trade_price_24h"] or 0.0, reverse=True)
        result = {
            "quote": quote, "count": len(items), "fetched_at": datetime.now(KST).isoformat(), "items": items,
        }
        self._market_cache[quote] = (now, result)
        return result

    # ------------------------------------------------------------------
    def status(self, mode: str) -> dict[str, Any]:
        repo = self.repo(mode)
        es = repo.read_engine_status()
        engine: dict[str, Any] | None = None
        if es is not None:
            updated = from_db_time(es.updated_at)
            engine = {
                "status": es.status, "strategy": es.strategy, "markets": es.markets or [], "interval": es.interval,
                "started_at": _iso(es.started_at), "updated_at": _iso(es.updated_at),
                "heartbeat_age_seconds": (datetime.now(UTC) - updated).total_seconds() if updated else None,
                "api_ok": es.api_ok, "ws_status": es.ws_status, "last_data_at": _iso(es.last_data_at),
                "last_candle_at": _iso(es.last_candle_at), "last_trade_at": _iso(es.last_trade_at),
                "equity": es.equity, "cash": es.cash, "message": es.message, "risk": es.risk, "pid": es.pid,
                "settings_version": es.settings_version, "restart_required": es.restart_required,
            }
        return {
            "mode": mode,
            "env_mode": self.settings.trading_mode.value,
            "live_allowed": self.settings.is_live_trading_allowed,
            "has_api_keys": self.settings.has_api_keys,
            "has_pocket_keys": self.settings.has_pocket_keys,
            "notify_channels": self.settings.notify_channels,
            "engine": engine,
            "settings_version": repo.runtime_settings_version(),
            "server_time": datetime.now(KST).isoformat(),
        }

    async def balance(self, mode: str) -> dict[str, Any]:
        repo = self.repo(mode)
        account = repo.load_account()
        positions = repo.load_positions()
        prices = await self.prices.get([p.market for p in positions])
        rows = []
        positions_value = 0.0
        for p in positions:
            price = prices.get(p.market)
            value = p.quantity * price if price else None
            cost = p.entry_amount + p.entry_fee
            rows.append({
                "market": p.market, "quantity": p.quantity, "avg_price": p.avg_price, "current_price": price,
                "value": value, "unrealized_pnl": (value - cost) if value is not None else None,
                "unrealized_pnl_pct": ((value - cost) / cost) if value is not None and cost else None,
                "opened_at": _iso(p.opened_at), "cost_known": p.cost_known is not False,
            })
            positions_value += value or 0.0
        cash = account.cash if account else None
        equity = (cash + positions_value) if cash is not None else None
        snap = repo.latest_balance()
        return {
            "initial_cash": account.initial_cash if account else None, "cash": cash,
            "positions_value": positions_value, "equity": equity, "positions": rows,
            "fees_paid": account.fees_paid if account else 0.0,
            "snapshot_time": _iso(snap.time) if snap else None,
            "prices": prices,
        }

    async def performance(self, mode: str) -> dict[str, Any]:
        repo = self.repo(mode)
        bal = await self.balance(mode)
        series = repo.balance_series()
        points = [(from_db_time(s.time), s.equity) for s in series]
        equity_now = bal["equity"]
        now = datetime.now(UTC)
        if equity_now is not None:
            points.append((now, equity_now))
        initial = bal["initial_cash"]
        # 포켓 이전(입출금)은 손익이 아니다: 기준 자산에 순입출금을 더해 비교한다 (감사 MEDIUM-4)
        net_flow = repo.net_cash_flow()
        adjusted_initial = (initial + net_flow) if initial is not None else None
        cumulative = (equity_now / adjusted_initial - 1) if equity_now is not None and adjusted_initial else None

        day_start_kst = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        before_today = [e for t, e in points if t < day_start_kst]
        today_points = [e for t, e in points if t >= day_start_kst]
        day_base = before_today[-1] if before_today else (today_points[0] if today_points else None)
        today_flow = repo.net_cash_flow(since=day_start_kst)
        today_return = ((equity_now - today_flow) / day_base - 1) if equity_now is not None and day_base else None

        mdd = 0.0
        peak_t = trough_t = None
        if len(points) >= 2:
            import pandas as pd

            eq = pd.Series([e for _, e in points], index=pd.DatetimeIndex([t for t, _ in points]))
            mdd, peak_t, trough_t = max_drawdown(eq)

        trades = repo.load_round_trips()
        today_trades = [t for t in trades if t.exit_time >= day_start_kst]
        wins = sum(1 for t in trades if t.pnl > 0)
        step = max(1, len(points) // 500)
        return {
            "initial_cash": initial, "equity": equity_now, "cumulative_return": cumulative,
            "net_cash_flow": net_flow, "today_cash_flow": today_flow,
            "today_return": today_return, "today_realized_pnl": sum(t.pnl for t in today_trades),
            "realized_pnl": sum(t.pnl for t in trades), "mdd": mdd,
            "mdd_peak_time": _iso(peak_t) if peak_t else None, "mdd_trough_time": _iso(trough_t) if trough_t else None,
            "trades": len(trades), "wins": wins, "win_rate": (wins / len(trades)) if trades else None,
            "series": [{"time": _iso(t), "equity": e} for t, e in points[::step]],
        }

    def recent(self, mode: str, limit: int = 20) -> dict[str, Any]:
        repo = self.repo(mode)
        return {
            "signals": [
                {"time": _iso(s.time), "market": s.market, "action": s.action, "price": s.price, "reason": s.reason,
                 "strategy": s.strategy, "indicators": s.indicators}
                for s in repo.recent_signals(limit)
            ],
            "orders": [
                {"time": _iso(o.created_at), "market": o.market, "side": o.side, "status": o.status,
                 "fill_price": o.fill_price, "filled_quantity": o.filled_quantity, "fee": o.fee,
                 "reason": o.reason, "error": o.error, "exchange_order_id": o.exchange_order_id}
                for o in repo.recent_orders(limit)
            ],
            "round_trips": [
                {"entry_time": _iso(r.entry_time), "exit_time": _iso(r.exit_time), "market": r.market,
                 "entry_price": r.entry_price, "exit_price": r.exit_price, "quantity": r.quantity,
                 "pnl": r.pnl, "pnl_pct": r.pnl_pct, "exit_reason": r.exit_reason}
                for r in repo.recent_round_trips(limit)
            ],
            "errors": [
                {"time": _iso(e.time), "level": e.level, "event": e.event, "message": e.message}
                for e in repo.recent_logs(limit) if e.level in ("ERROR", "WARNING")
            ],
            "logs": [
                {"time": _iso(e.time), "level": e.level, "event": e.event, "message": e.message}
                for e in repo.recent_logs(limit)
            ],
        }

    # ------------------------------------------------------------------
    # 포켓
    # ------------------------------------------------------------------
    def pocket_client(self) -> UpbitClient | None:
        s = self.settings
        if not s.has_pocket_keys:
            return None
        return UpbitClient(
            base_url=s.upbit_api_url, access_key=s.upbit_pocket_access_key.get_secret_value(),
            secret_key=s.upbit_pocket_secret_key.get_secret_value(), timeout=s.http_timeout_seconds,
        )

    def bot_client(self) -> UpbitClient | None:
        """봇 API Key 클라이언트 — 조회·포켓 이전 전용. .env 가 LIVE 이중 플래그여도 대시보드 프로세스에는 주문
        권한을 주지 않는다 (감사 LOW-13): 주문은 엔진 프로세스만 낸다."""
        if not self.settings.has_api_keys:
            return None
        return UpbitClient.from_settings(self.settings, allow_orders=False)

    async def pockets(self) -> dict[str, Any]:
        """포켓 목록과 잔고. 메인포켓 키가 없으면 봇 포켓 잔고만 보여준다."""
        out: dict[str, Any] = {"universal_transfer": self.settings.has_pocket_keys, "pockets": [], "bot_pocket": None}
        client = self.pocket_client()
        if client is not None:
            try:
                async with client:
                    pockets = await client.get_pockets()
                    for p in pockets:
                        if p.is_main:
                            assets = await client.get_accounts()
                        else:
                            assets = await client.get_pocket_assets(p.uuid)
                        out["pockets"].append({
                            "uuid": p.uuid, "name": p.name, "type": p.type, "is_main": p.is_main,
                            "balances": [
                                {"currency": a.currency, "balance": float(a.balance), "locked": float(a.locked),
                                 "avg_buy_price": float(a.avg_buy_price)} for a in assets
                            ],
                        })
            except TraderError as exc:
                out["error"] = f"포켓 조회 실패: {exc}"
        bot = self.bot_client()
        if bot is not None:
            try:
                async with bot:
                    assets = await bot.get_accounts()
                out["bot_pocket"] = {
                    "balances": [
                        {"currency": a.currency, "balance": float(a.balance), "locked": float(a.locked),
                         "avg_buy_price": float(a.avg_buy_price)} for a in assets
                    ]
                }
            except TraderError as exc:
                out["bot_error"] = f"봇 포켓 잔고 조회 실패: {exc}"
        return out

    async def transfer(self, *, direction: str, amount: float, currency: str = "KRW",
                       bot_pocket_uuid: str | None = None) -> dict[str, Any]:
        """포켓 간 자산 이전. direction: to_bot(메인→봇) / to_main(봇→메인)."""
        if amount <= 0:
            raise ValueError("이전 금액은 0보다 커야 합니다")
        identifier = f"dash-{direction}-{uuid.uuid4().hex[:12]}"
        client = self.pocket_client()
        if client is not None:
            async with client:
                pockets = await client.get_pockets()
                main = next((p for p in pockets if p.is_main), None)
                subs = [p for p in pockets if not p.is_main]
                target = next((p for p in subs if p.uuid == bot_pocket_uuid), None) if bot_pocket_uuid else None
                if target is None and len(subs) == 1:
                    target = subs[0]
                if main is None or target is None:
                    raise ValueError(
                        "메인포켓 또는 봇 포켓(서브포켓)을 특정할 수 없습니다. bot_pocket_uuid 를 지정하세요."
                    )
                if direction == "to_bot":
                    result = await client.transfer_from_main(
                        to=target.uuid, currency=currency, amount=f"{amount:.8f}".rstrip("0").rstrip("."),
                        identifier=identifier,
                    )
                elif direction == "to_main":
                    result = await client.transfer_from_main(
                        to=main.uuid, currency=currency, amount=f"{amount:.8f}".rstrip("0").rstrip("."),
                        identifier=identifier, from_pocket=target.uuid,
                    )
                else:
                    raise ValueError("direction 은 to_bot 또는 to_main")
        else:
            if direction != "to_main":
                raise ValueError(
                    "메인포켓 키(UPBIT_POCKET_*)가 없어 메인→봇 이전은 할 수 없습니다. 봇→메인만 가능합니다."
                )
            bot = self.bot_client()
            if bot is None:
                raise ValueError("봇 API Key 가 없습니다")
            async with bot:
                result = await bot.transfer_from_sub(
                    currency=currency, amount=f"{amount:.8f}".rstrip("0").rstrip("."), identifier=identifier,
                )
        return {"uuid": result.uuid, "state": result.state, "currency": result.currency, "amount": float(result.amount),
                "from": result.from_pocket, "to": result.to, "created_at": result.created_at}


def describe_api_error(exc: Exception) -> str:
    if isinstance(exc, UpbitAPIError):
        return f"업비트 오류 {exc.status_code} [{exc.name}] {exc.message}"
    return str(exc)


def recent_window(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)
