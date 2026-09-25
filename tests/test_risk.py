"""리스크 관리자 테스트."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.risk import RiskConfig, RiskManager
from app.risk.manager import EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, EXIT_TRAILING_STOP
from app.strategy.base import Action, Signal
from app.trading.market_state import PriceState
from app.trading.portfolio import Portfolio, Position, Trade

M = "KRW-BTC"
NOW = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)  # KST 12:00


def buy_signal(price: float = 100.0, market: str = M) -> Signal:
    return Signal(Action.BUY, market, NOW - timedelta(hours=1), price, "test")


def fresh(mark: float, market: str = M) -> PriceState:
    return PriceState(market, last_price=mark, last_time=NOW, best_bid=mark, best_ask=mark, book_time=NOW)


def trade(pnl: float, exit_time: datetime = NOW, market: str = M) -> Trade:
    return Trade(market, exit_time - timedelta(hours=1), 100.0, 1.0, 100.0, 0.05, exit_time, 100.0 + pnl,
                 100.0 + pnl, 0.05, "signal", pnl, pnl / 100.05)


def unrestricted(**kw) -> RiskManager:
    return RiskManager(RiskConfig.unrestricted(**kw))


class TestConfig:
    def test_defaults_and_describe(self) -> None:
        cfg = RiskConfig()
        assert cfg.stop_loss_pct == 0.05 and cfg.daily_loss_limit_pct == 0.03 and cfg.max_open_positions == 1
        assert "손절 5%" in " ".join(f"{k} {v}" for k, v in cfg.describe().items())
        assert RiskConfig.unrestricted().stop_loss_pct is None

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            RiskConfig(max_order_amount=1000, min_order_amount=5000)
        with pytest.raises(ValueError):
            RiskConfig(position_fraction=0)
        with pytest.raises(ValueError):
            RiskConfig(unknown=1)


class TestEntry:
    def test_budget_caps(self) -> None:
        portfolio = Portfolio(1_000_000)
        assert unrestricted(position_fraction=0.5).evaluate(buy_signal(), portfolio, None, NOW).amount == 500_000
        capped = unrestricted(max_order_amount=200_000).evaluate(buy_signal(), portfolio, None, NOW)
        assert capped.approved and capped.amount == 200_000
        tiny = unrestricted(position_fraction=0.001).evaluate(buy_signal(), portfolio, None, NOW)
        assert not tiny.approved and "최소 주문" in tiny.reason

    def test_position_ratio_uses_equity(self) -> None:
        portfolio = Portfolio(1_000_000)
        portfolio.buy("KRW-ETH", 100.0, time=NOW, amount=300_000)  # 코인 약 30만
        rm = unrestricted(max_position_ratio=0.5, max_open_positions=5)
        decision = rm.evaluate(buy_signal(), portfolio, None, NOW, equity=1_000_000)
        assert decision.approved and decision.amount == pytest.approx(500_000 - (1_000_000 - portfolio.cash), rel=1e-6)
        full = unrestricted(max_position_ratio=0.25, max_open_positions=5)
        blocked = full.evaluate(buy_signal(), portfolio, None, NOW, equity=1_000_000)
        assert not blocked.approved and "상한" in blocked.reason

    def test_max_open_positions_and_existing(self) -> None:
        portfolio = Portfolio(1_000_000)
        portfolio.buy("KRW-ETH", 100.0, time=NOW, amount=100_000)
        rm = unrestricted(max_open_positions=1)
        assert "최대 포지션" in rm.evaluate(buy_signal(), portfolio, None, NOW).reason
        assert "이미 보유" in rm.evaluate(buy_signal(market="KRW-ETH"), portfolio, None, NOW).reason
        assert rm.evaluate(Signal(Action.SELL, "KRW-ETH", NOW, 100.0, "t"), portfolio, None, NOW).approved
        assert not rm.evaluate(Signal(Action.SELL, M, NOW, 100.0, "t"), portfolio, None, NOW).approved
        assert not rm.evaluate(Signal(Action.HOLD, M, NOW, 100.0, "t"), portfolio, None, NOW).approved

    def test_price_deviation_guard(self) -> None:
        rm = unrestricted(price_deviation_limit=0.05)
        portfolio = Portfolio(1_000_000)
        assert rm.evaluate(buy_signal(100.0), portfolio, fresh(103.0), NOW).approved
        bad = rm.evaluate(buy_signal(100.0), portfolio, fresh(120.0), NOW)
        assert not bad.approved and "괴리" in bad.reason
        assert rm.evaluate(buy_signal(100.0), portfolio, None, NOW).approved  # 시세 없으면 검사 생략

    def test_cooldown_after_exit(self) -> None:
        rm = unrestricted(cooldown_seconds=600)
        portfolio = Portfolio(1_000_000)
        rm.record_trade(trade(-5.0, NOW - timedelta(minutes=5)), NOW - timedelta(minutes=5))
        assert "재진입 대기" in rm.evaluate(buy_signal(), portfolio, None, NOW).reason
        assert rm.evaluate(buy_signal(), portfolio, None, NOW + timedelta(minutes=6)).approved

    def test_halt_blocks_entries_but_allows_exit(self) -> None:
        rm = unrestricted()
        portfolio = Portfolio(1_000_000)
        rm.halt("테스트 정지")
        assert "긴급 정지" in rm.evaluate(buy_signal(), portfolio, None, NOW).reason
        portfolio.buy(M, 100.0, time=NOW, amount=100_000)
        assert rm.evaluate(Signal(Action.SELL, M, NOW, 100.0, "t"), portfolio, None, NOW).approved
        rm.resume()
        assert rm.entries_blocked_reason is None


class TestDailyLimits:
    def test_daily_loss_lock_and_reset_next_day(self) -> None:
        rm = unrestricted(daily_loss_limit_pct=0.03)
        assert rm.update_equity(1_000_000, NOW) is None
        assert rm.update_equity(980_000, NOW + timedelta(hours=1)) is None
        lock = rm.update_equity(969_000, NOW + timedelta(hours=2))
        assert lock and "일일 손실" in lock
        assert "신규 진입 차단" in rm.evaluate(buy_signal(), Portfolio(969_000), None, NOW + timedelta(hours=2)).reason
        # 다음 날(KST) → 리셋, 시작 자산은 현재 자산
        next_day = NOW + timedelta(days=1)
        assert rm.start_day_if_needed(next_day, 969_000) is True
        assert rm.state.lock_reason is None and rm.state.day_start_equity == 969_000
        assert rm.evaluate(buy_signal(), Portfolio(969_000), None, next_day).approved

    def test_consecutive_losses_lock(self) -> None:
        rm = unrestricted(max_consecutive_losses=2)
        assert rm.record_trade(trade(-1.0)) is None
        assert rm.record_trade(trade(5.0)) is None  # 이익이 나면 초기화
        assert rm.state.consecutive_losses == 0
        rm.record_trade(trade(-1.0))
        lock = rm.record_trade(trade(-2.0))
        assert lock and "연속 손실 2회" in lock
        assert rm.state.daily_realized_pnl == pytest.approx(1.0)
        assert not rm.evaluate(buy_signal(), Portfolio(1_000_000), None, NOW).approved

    def test_rebuild_from_today_trades_only(self) -> None:
        rm = unrestricted(max_consecutive_losses=3)
        yesterday = NOW - timedelta(days=1)
        trades = [trade(-1.0, yesterday), trade(-1.0, yesterday), trade(-1.0, NOW - timedelta(hours=2)),
                  trade(-1.0, NOW - timedelta(hours=1))]
        rm.rebuild(trades, NOW, 990_000)
        assert rm.state.consecutive_losses == 2 and rm.state.daily_realized_pnl == pytest.approx(-2.0)
        assert rm.state.lock_reason is None and rm.state.last_equity == 990_000
        rm.record_trade(trade(-1.0))
        assert rm.state.lock_reason is not None

    def test_snapshot_serializable(self) -> None:
        rm = RiskManager()
        rm.update_equity(1_000_000, NOW)
        snap = rm.snapshot()
        assert snap["state"]["day"] == "2026-05-01" and snap["config"]["stop_loss_pct"] == 0.05


class TestExits:
    def position(self, avg: float = 100.0) -> Portfolio:
        p = Portfolio(1_000_000)
        p.buy(M, avg, time=NOW, amount=100_000)
        return p

    def test_stop_before_take_profit(self) -> None:
        rm = unrestricted(stop_loss_pct=0.05, take_profit_pct=0.10)
        pos = self.position().position(M)
        assert rm.check_exits(pos, low=96.0, high=104.0, now=NOW) is None
        both = rm.check_exits(pos, low=90.0, high=120.0, now=NOW)
        assert both.reason == EXIT_STOP_LOSS and both.trigger_price == pytest.approx(95.0)
        tp = rm.check_exits(pos, low=99.0, high=111.0, now=NOW)
        assert tp.reason == EXIT_TAKE_PROFIT and tp.trigger_price == pytest.approx(110.0)

    def test_trailing_stop_tracks_peak(self) -> None:
        rm = unrestricted(trailing_stop_pct=0.10)
        pos = self.position().position(M)
        assert rm.check_exits(pos, low=112.0, high=120.0, now=NOW) is None  # 최고가 120 기록
        assert rm.state.peak_prices[M] == 120.0
        assert rm.check_exits(pos, low=110.0, high=115.0, now=NOW) is None
        hit = rm.check_exits(pos, low=107.0, high=109.0, now=NOW)
        assert hit.reason == EXIT_TRAILING_STOP and hit.trigger_price == pytest.approx(108.0)
        rm.record_trade(trade(7.0))
        assert M not in rm.state.peak_prices

    def test_disabled_rules_never_exit(self) -> None:
        rm = unrestricted()
        pos = self.position().position(M)
        assert rm.check_exits(pos, low=1.0, high=1000.0, now=NOW) is None

    def test_unknown_cost_adopts_first_price_as_reference(self) -> None:
        """감사 MEDIUM-2 — 평균 매수가 0 인 포지션은 처음 본 시세를 기준가로 삼아 손절이 살아난다."""
        rm = unrestricted(stop_loss_pct=0.05)
        pos = Position(M, 0.01, 0.0, NOW, 0.0, 0.0, cost_known=False)
        assert rm.check_exits(pos, low=1000.0, high=1000.0, now=NOW) is None  # 기준가를 정한 틱: 판정 없음
        assert pos.avg_price == 1000.0 and pos.entry_amount == pytest.approx(10.0) and pos.cost_known is False
        assert rm.check_exits(pos, low=960.0, high=990.0, now=NOW) is None
        hit = rm.check_exits(pos, low=940.0, high=990.0, now=NOW)
        assert hit is not None and hit.reason == EXIT_STOP_LOSS and hit.trigger_price == pytest.approx(950.0)


def test_cash_flow_moves_daily_loss_base() -> None:
    """감사 MEDIUM-4 — 포켓 이전(입출금)은 손실·이익이 아니다: 일일 손실 기준 자산을 같이 옮긴다."""
    rm = unrestricted(daily_loss_limit_pct=0.03)
    assert rm.update_equity(1_000_000, NOW) is None
    assert rm.apply_cash_flow(-100_000, 1, NOW) == 900_000  # 봇 → 메인 10만원 (조치 전엔 즉시 "일일 손실 10%" 잠금)
    assert rm.state.day_cash_flow == -100_000 and rm.state.cash_flow_cursor == 1
    assert rm.update_equity(895_000, NOW + timedelta(minutes=1)) is None  # 실제 손실 0.6%
    lock = rm.update_equity(870_000, NOW + timedelta(minutes=2))  # 3.3%
    assert lock is not None and "입출금 -100,000" in lock
    rm2 = unrestricted(daily_loss_limit_pct=0.03)
    rm2.update_equity(1_000_000, NOW)
    rm2.apply_cash_flow(200_000, 5, NOW)  # 메인 → 봇 20만원 (조치 전엔 수익 20% 로 보임)
    assert rm2.update_equity(1_170_000, NOW + timedelta(minutes=1)) is None  # 기준 120만 대비 -2.5%
    assert rm2.update_equity(1_160_000, NOW + timedelta(minutes=2)) is not None  # -3.3%
    rm2.update_equity(1_160_000, NOW + timedelta(days=1))  # 날짜가 바뀌면 당일 입출금은 0, 커서는 유지
    assert rm2.state.day_cash_flow == 0.0 and rm2.state.cash_flow_cursor == 5
