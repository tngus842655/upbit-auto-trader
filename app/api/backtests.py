"""대시보드 백테스트 실행기 — 현재 실행 설정(전략·파라미터·캔들·리스크)으로 연도별·구간별 백테스트를
백그라운드로 돌린다.

- 요청 하나 = 작업(job). (구간 × 마켓) 조합을 순서대로 돌리며 진행률을 갱신하고, 화면은 폴링으로 결과를 받는다.
- 캔들은 기존 로더(``app.backtest.loader.load_candles``, data/cache 재사용)로 받고, 엔진은 CLI ``backtest`` 와 같은
  ``BacktestEngine`` 이다. 수수료·슬리피지는 요청값을 그대로 쓴다(거래소 수수료 반영).
- 실제 주문·DB 기록과 무관하다. 결과는 메모리(최근 작업 20개)와 data/backtests/dash_* 폴더에 남는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.backtest import BacktestConfig, BacktestEngine, load_candles, save_result
from app.backtest.engine import BacktestResult
from app.config.settings import PROJECT_ROOT, Settings
from app.core.exceptions import TraderError
from app.database.models import from_db_time
from app.exchange.models import KST, CandleInterval
from app.risk.config import RiskConfig
from app.strategy import STRATEGIES, create_strategy

if TYPE_CHECKING:
    from app.database.repository import Repository

log = logging.getLogger(__name__)

MAX_JOBS = 20
KEEP_JOBS = 100  # DB 에 남기는 최근 작업 수
MAX_EQUITY_POINTS = 300
MAX_TRADES = 60

CandleLoader = Callable[..., Awaitable[pd.DataFrame]]


def parse_kst(value: Any) -> datetime:
    """'2025-01-01' / ISO 문자열 / datetime → 시간대 있는 datetime (naive 는 KST 로 본다)."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("날짜가 비어 있습니다")
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=40)
    start: datetime
    end: datetime | None = None

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        return None if value in (None, "") else parse_kst(value)

    @model_validator(mode="after")
    def _order(self) -> Period:
        if self.end is not None and self.start >= self.end:
            raise ValueError(f"구간 '{self.label}': 시작이 끝보다 앞서야 합니다")
        return self


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markets: list[str] = Field(min_length=1, max_length=20)
    candle_interval: str = "60m"
    strategy_name: str
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    periods: list[Period] = Field(min_length=1, max_length=24)
    initial_capital: float = Field(default=1_000_000.0, gt=0)
    fee_rate: float = Field(default=0.0005, ge=0, lt=0.1, description="편도 수수료율 (업비트 KRW 마켓 0.0005)")
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1)
    use_risk: bool = False
    risk: RiskConfig | None = None
    settings_version: int | None = None
    save: bool = True

    @field_validator("markets", mode="before")
    @classmethod
    def _markets(cls, value: Any) -> list[str]:
        items = [str(v).strip().upper() for v in (value.split(",") if isinstance(value, str) else value)]
        items = [v for v in items if v]
        bad = [v for v in items if not re.match(r"^[A-Z]+-[A-Z0-9]+$", v)]
        if bad:
            raise ValueError(f"잘못된 마켓 코드 {bad}")
        return list(dict.fromkeys(items))

    @field_validator("candle_interval")
    @classmethod
    def _interval(cls, value: str) -> str:
        return CandleInterval.parse(value).value

    @field_validator("strategy_name")
    @classmethod
    def _strategy(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in STRATEGIES:
            raise ValueError(f"알 수 없는 전략 '{value}'")
        return key

    @model_validator(mode="after")
    def _params_ok(self) -> BacktestRequest:
        try:
            strategy = create_strategy(self.strategy_name, self.strategy_params)
        except TraderError as exc:
            raise ValueError(str(exc)) from exc
        self.strategy_params = strategy.params.model_dump(mode="json")
        return self

    def risk_config(self) -> RiskConfig:
        if self.use_risk and self.risk is not None:
            return self.risk
        return RiskConfig.unrestricted()

    def backtest_config(self) -> BacktestConfig:
        risk = self.risk_config()
        return BacktestConfig(
            initial_capital=self.initial_capital, fee_rate=self.fee_rate, slippage_rate=self.slippage_rate,
            position_fraction=risk.position_fraction, risk=risk,
        )


@dataclass
class BacktestJob:
    id: str
    request: BacktestRequest
    created_at: datetime
    status: str = "queued"  # queued / running / done / error / cancelled
    progress: int = 0
    total: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    finished_at: datetime | None = None
    task: asyncio.Task[None] | None = None

    @property
    def label(self) -> str:
        req = self.request
        return f"{req.strategy_name} · {', '.join(req.markets)} · {req.candle_interval} · 구간 {len(req.periods)}개"

    def snapshot(self) -> dict[str, Any]:
        """DB 저장용 스냅샷 (시각은 aware datetime 그대로, request 는 JSON 형태)."""
        d = self.to_dict(with_results=False)
        return {
            "id": self.id, "created_at": self.created_at, "finished_at": self.finished_at, "status": self.status,
            "label": self.label, "progress": self.progress, "total": self.total, "ok": d["ok"], "failed": d["failed"],
            "request": d["request"], "results": self.results, "error": self.error,
        }

    def to_dict(self, *, with_results: bool = True) -> dict[str, Any]:
        req = self.request
        out: dict[str, Any] = {
            "id": self.id, "label": self.label, "status": self.status, "progress": self.progress, "total": self.total,
            "created_at": self.created_at.astimezone(KST).isoformat(),
            "finished_at": self.finished_at.astimezone(KST).isoformat() if self.finished_at else None,
            "error": self.error,
            "request": {
                "markets": req.markets, "candle_interval": req.candle_interval, "strategy_name": req.strategy_name,
                "strategy_params": req.strategy_params,
                "periods": [
                    {"label": p.label, "start": p.start.astimezone(KST).isoformat(),
                     "end": p.end.astimezone(KST).isoformat() if p.end else None} for p in req.periods
                ],
                "initial_capital": req.initial_capital, "fee_rate": req.fee_rate, "slippage_rate": req.slippage_rate,
                "use_risk": req.use_risk, "risk": req.risk_config().model_dump() if req.use_risk else None,
                "settings_version": req.settings_version,
            },
            "ok": sum(1 for r in self.results if "error" not in r),
            "failed": sum(1 for r in self.results if "error" in r),
        }
        if with_results:
            out["results"] = self.results
        return out


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(KST).isoformat() if value else None


def _row_dict(row: dict[str, Any]) -> dict[str, Any]:
    """repository.list_backtest_jobs 행 → API 형태."""
    return {**row, "created_at": _iso(row["created_at"]), "finished_at": _iso(row.get("finished_at"))}


def _record_dict(record: Any, *, with_results: bool) -> dict[str, Any]:
    """BacktestJobRecord → API 형태 (BacktestJob.to_dict 와 같은 키)."""
    out = {
        "id": record.id, "label": record.label, "status": record.status, "progress": record.progress,
        "total": record.total, "created_at": _iso(from_db_time(record.created_at)),
        "finished_at": _iso(from_db_time(record.finished_at)), "error": record.error, "request": record.request,
        "ok": record.ok, "failed": record.failed,
    }
    if with_results:
        out["results"] = list(record.results or [])
    return out


def _series_points(series: pd.Series, limit: int = MAX_EQUITY_POINTS) -> list[dict[str, Any]]:
    """차트용 다운샘플 — 최대 ``limit`` 개 점 (마지막 점은 항상 포함)."""
    n = len(series)
    if n == 0:
        return []
    step = max(1, -(-n // limit))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    out = []
    for i in idx:
        ts = series.index[i]
        stamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        out.append({"time": stamp.astimezone(KST).isoformat(), "value": float(series.iloc[i])})
    return out


def serialize_result(result: BacktestResult, period: Period, *, saved_to: str | None = None) -> dict[str, Any]:
    trades = [t.to_dict() for t in result.trades]
    return {
        "period": period.label, "market": result.market, "interval": result.interval,
        "strategy": result.strategy_name, "strategy_params": result.strategy_params,
        "start": result.start.astimezone(KST).isoformat(), "end": result.end.astimezone(KST).isoformat(),
        "candles": int(len(result.equity)),
        "metrics": result.metrics.to_dict(), "benchmark": result.benchmark_metrics.to_dict(),
        "exit_reasons": result.exit_reason_counts(), "risk_rejections": dict(result.risk_rejections),
        "ignored_buy_signals": result.ignored_buy_signals, "ignored_sell_signals": result.ignored_sell_signals,
        "trade_count": len(trades), "trades": trades[-MAX_TRADES:],
        "equity": _series_points(result.equity), "benchmark_equity": _series_points(result.benchmark_equity),
        "fee_rate": result.config.fee_rate, "slippage_rate": result.config.slippage_rate,
        "saved_to": saved_to,
    }


class BacktestRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        loader: CandleLoader = load_candles,
        save_dir: Path | str | None = None,
        max_jobs: int = MAX_JOBS,
        repo: Repository | None = None,
        keep: int = KEEP_JOBS,
    ) -> None:
        self.settings = settings
        self.loader = loader
        self.repo = repo  # 있으면 작업을 DB 에 남긴다
        self.keep = keep
        self.save_dir = Path(save_dir) if save_dir else PROJECT_ROOT / "data" / "backtests"
        self.max_jobs = max_jobs
        self.jobs: dict[str, BacktestJob] = {}

    # ------------------------------------------------------------------
    def defaults(self) -> dict[str, Any]:
        now = datetime.now(KST)
        return {
            "initial_capital": self.settings.paper_initial_cash,
            "fee_rate": self.settings.paper_fee_rate,
            "slippage_rate": self.settings.paper_slippage_rate,
            "years": list(range(now.year, now.year - 4, -1)),
            "today": now.date().isoformat(),
            "intervals": [i.value for i in CandleInterval if i.value not in ("1s", "1w", "1M", "1y")],
        }

    def submit(self, request: BacktestRequest) -> BacktestJob:
        job = BacktestJob(id=uuid.uuid4().hex[:12], request=request, created_at=datetime.now(UTC))
        job.total = len(request.periods) * len(request.markets)
        self._trim()
        self.jobs[job.id] = job
        self._persist(job)
        if self.repo is not None:
            with contextlib.suppress(Exception):
                self.repo.trim_backtest_jobs(self.keep)
        job.task = asyncio.create_task(self._run(job), name=f"backtest-{job.id}")
        return job

    def get(self, job_id: str) -> BacktestJob | None:
        return self.jobs.get(job_id)

    def get_dict(self, job_id: str) -> dict[str, Any] | None:
        """메모리(실행 중·최근) → DB 순으로 찾는다. 결과 포함."""
        job = self.jobs.get(job_id)
        if job is not None:
            return job.to_dict()
        if self.repo is None:
            return None
        record = self.repo.load_backtest_job(job_id)
        return _record_dict(record, with_results=True) if record is not None else None

    def list_jobs(self) -> list[dict[str, Any]]:
        """DB 목록(서버 재시작 후에도 유지) + 메모리의 실행 중 작업. 결과 본문은 뺀다."""
        live = {j.id: j.to_dict(with_results=False) for j in self.jobs.values()}
        if self.repo is None:
            return sorted(live.values(), key=lambda d: d["created_at"], reverse=True)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in self.repo.list_backtest_jobs(self.keep):
            seen.add(row["id"])
            out.append(live.get(row["id"]) or _row_dict(row))
        out.extend(d for job_id, d in live.items() if job_id not in seen)
        out.sort(key=lambda d: d["created_at"], reverse=True)
        return out

    async def delete(self, job_id: str) -> bool:
        """실행 중이면 중단하고 메모리·DB 에서 지운다."""
        found = job_id in self.jobs
        if found:
            await self.cancel(job_id)
            self.jobs.pop(job_id, None)
        if self.repo is not None:
            found = self.repo.delete_backtest_job(job_id) or found
        return found

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.task is not None and not job.task.done():
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job.task
        if job.status in ("queued", "running"):
            job.status = "cancelled"
            job.finished_at = datetime.now(UTC)
        return True

    async def run_sync(self, request: BacktestRequest) -> BacktestJob:
        """테스트·CLI 용: 백그라운드 없이 끝까지 돌린다."""
        job = BacktestJob(id=uuid.uuid4().hex[:12], request=request, created_at=datetime.now(UTC))
        job.total = len(request.periods) * len(request.markets)
        self.jobs[job.id] = job
        await self._run(job)
        return job

    # ------------------------------------------------------------------
    def _persist(self, job: BacktestJob) -> None:
        if self.repo is None:
            return
        try:
            self.repo.save_backtest_job(job.snapshot())
        except Exception as exc:  # noqa: BLE001 - 기록 실패가 백테스트를 막지 않게
            log.warning("백테스트 작업 저장 실패 %s: %s", job.id, exc)

    def _trim(self) -> None:
        finished = [j for j in self.jobs.values() if j.status not in ("queued", "running")]
        finished.sort(key=lambda j: j.created_at)
        while len(self.jobs) >= self.max_jobs and finished:
            oldest = finished.pop(0)
            self.jobs.pop(oldest.id, None)

    async def _run(self, job: BacktestJob) -> None:
        job.status = "running"
        self._persist(job)
        req = job.request
        interval = CandleInterval.parse(req.candle_interval)
        config = req.backtest_config()
        job_dir = self.save_dir / f"dash_{datetime.now(KST):%Y%m%d_%H%M%S}_{job.id}"
        try:
            for period in req.periods:
                for market in req.markets:
                    try:
                        df = await self.loader(self.settings, market, interval, period.start, period.end)
                        strategy = create_strategy(req.strategy_name, req.strategy_params)
                        result = await asyncio.to_thread(
                            BacktestEngine(config).run, df, strategy, market=market, interval=interval
                        )
                        saved_to: str | None = None
                        if req.save:
                            safe = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", period.label).strip("_") or "period"
                            out = job_dir / f"{safe}_{market}_{interval.value}_{req.strategy_name}"
                            with contextlib.suppress(Exception):
                                save_result(result, out)
                                inside = out.is_relative_to(PROJECT_ROOT)
                                saved_to = str(out.relative_to(PROJECT_ROOT)) if inside else str(out)
                        job.results.append(serialize_result(result, period, saved_to=saved_to))
                    except asyncio.CancelledError:
                        raise
                    except (TraderError, ValueError) as exc:
                        log.warning("백테스트 실패 %s %s: %s", period.label, market, exc)
                        job.results.append({"period": period.label, "market": market, "interval": interval.value,
                                            "error": str(exc)})
                    except Exception as exc:  # noqa: BLE001 - 한 조합의 예외가 작업 전체를 죽이지 않게
                        log.exception("백테스트 예외 %s %s", period.label, market)
                        job.results.append({"period": period.label, "market": market, "interval": interval.value,
                                            "error": f"{type(exc).__name__}: {exc}"})
                    job.progress += 1
                    self._persist(job)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("백테스트 작업 실패 %s", job.id)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = datetime.now(UTC)
            self._persist(job)
