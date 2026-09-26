"""대시보드 '연도별 최적 설정' 실행기 — 설정 탭의 연도 버튼(2025년, 2024년 …)이 부른다.

- 요청: 전략, 연도, 마켓, 현재 리스크 설정, 수수료·슬리피지·초기 자본(백테스트 탭 값)
- 작업: 그 해(1/1 ~ 12/31, KST) 캔들을 캔들 단위별(60m·240m·1d)로 받아 조합을 모두 백테스트하고
  (``app.backtest.optimize``) 마켓 평균 수익률이 가장 높은 조합을 돌려준다. 같은 설정의 다음 해 성과도 계산한다.
- 캔들은 백테스트 탭과 같은 로더(data/cache 재사용, 대시보드용 낮은 조회 한도)와 같은 연도 구간을 쓴다.
- 결과는 data/optimize/ 에 캐시한다. 지난 해 데이터는 바뀌지 않으므로 같은 조건으로 다시 누르면 바로 나온다
  (다음 해가 올해라 '오늘까지' 성과가 들어가는 경우만 날짜가 바뀌면 다시 계산한다).
- 설정을 저장하지 않는다 — 화면이 결과를 폼에 채우고, 적용은 사용자가 저장해서 한다. 실제 주문·DB 와 무관하다.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.backtests import CandleLoader, dashboard_load_candles
from app.backtest.optimize import (
    EXIT_RULES,
    GRID_VERSION,
    INTERVALS,
    CandidateResult,
    OptimizationOutcome,
    OptimizeCancelled,
    backtest_candidate,
    count_runs,
    optimize_year,
    quiet_backtest_logs,
)
from app.config.settings import PROJECT_ROOT, Settings
from app.core.exceptions import TraderError
from app.exchange.models import KST
from app.risk.config import RiskConfig
from app.strategy import STRATEGIES

log = logging.getLogger(__name__)

FIRST_YEAR = 2018  # 업비트 원화 마켓 캔들이 온전히 있는 첫 해
MAX_MARKETS = 10
MAX_JOBS = 20
EXIT_FIELDS = ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct")


def _now_kst() -> datetime:
    return datetime.now(KST)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_name: str
    year: int
    markets: list[str] = Field(min_length=1, max_length=MAX_MARKETS)
    risk: RiskConfig = Field(default_factory=RiskConfig)  # 손절·익절·추적 손절은 후보별로 바꾸고 나머지는 그대로 쓴다
    initial_capital: float = Field(default=1_000_000.0, gt=0)
    fee_rate: float = Field(default=0.0005, ge=0, lt=0.1)
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1)

    @field_validator("strategy_name")
    @classmethod
    def _strategy(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in STRATEGIES:
            raise ValueError(f"알 수 없는 전략 '{value}'")
        return key

    @field_validator("year")
    @classmethod
    def _year(cls, value: int) -> int:
        current = _now_kst().year
        if not FIRST_YEAR <= value < current:
            raise ValueError(f"연도는 {FIRST_YEAR} ~ {current - 1} 사이여야 합니다 (올해는 한 해가 끝나지 않아 제외)")
        return value

    @field_validator("markets", mode="before")
    @classmethod
    def _markets(cls, value: Any) -> list[str]:
        items = [str(v).strip().upper() for v in (value.split(",") if isinstance(value, str) else value)]
        items = [v for v in items if v]
        bad = [v for v in items if not re.match(r"^[A-Z]+-[A-Z0-9]+$", v)]
        if bad:
            raise ValueError(f"잘못된 마켓 코드 {bad}")
        return list(dict.fromkeys(items))

    def cache_key(self, today: str | None) -> str:
        """결과 캐시 키. 청산 규칙 3개는 후보가 덮어쓰므로 빼고, 다음 해가 올해면 오늘 날짜를 넣는다."""
        risk = {k: v for k, v in self.risk.model_dump().items() if k not in EXIT_FIELDS}
        payload = {
            "v": GRID_VERSION, "strategy": self.strategy_name, "year": self.year, "markets": sorted(self.markets),
            "risk": risk, "capital": self.initial_capital, "fee": self.fee_rate, "slippage": self.slippage_rate,
            "today": today,
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def year_period(year: int) -> tuple[datetime, datetime]:
    """백테스트 탭의 'N년' 구간과 같다: N-01-01 00:00 ~ (N+1)-01-01 00:00 KST."""
    return datetime(year, 1, 1, tzinfo=KST), datetime(year + 1, 1, 1, tzinfo=KST)


@dataclass
class OptimizeJob:
    id: str
    request: OptimizeRequest
    created_at: datetime
    status: str = "queued"  # queued / running / done / error / cancelled
    phase: str = ""  # candles / backtest / verify
    progress: int = 0
    total: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    cached: bool = False
    finished_at: datetime | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None

    def to_dict(self) -> dict[str, Any]:
        req = self.request
        return {
            "id": self.id, "status": self.status, "phase": self.phase, "progress": self.progress, "total": self.total,
            "strategy": req.strategy_name, "year": req.year, "markets": req.markets, "cached": self.cached,
            "result": self.result, "error": self.error,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class OptimizeRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        loader: CandleLoader = dashboard_load_candles,
        cache_dir: Path | str | None = None,
        now: Callable[[], datetime] = _now_kst,
        max_jobs: int = MAX_JOBS,
    ) -> None:
        self.settings = settings
        self.loader = loader
        self.cache_dir = Path(cache_dir) if cache_dir else PROJECT_ROOT / "data" / "optimize"
        self.now = now
        self.max_jobs = max_jobs
        self.jobs: dict[str, OptimizeJob] = {}
        self._gate = asyncio.Semaphore(1)  # 한 번에 한 작업 (CPU·캔들 조회를 몰아 쓰지 않게)

    # ------------------------------------------------------------------
    def submit(self, request: OptimizeRequest) -> OptimizeJob:
        job = self._new_job(request)
        job.task = asyncio.create_task(self._run(job), name=f"optimize-{job.id}")
        return job

    async def run_sync(self, request: OptimizeRequest) -> OptimizeJob:
        """테스트용: 백그라운드 없이 끝까지 돌린다."""
        job = self._new_job(request)
        await self._run(job)
        return job

    def get(self, job_id: str) -> OptimizeJob | None:
        return self.jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return False
        job.cancel_event.set()
        if job.task is not None and not job.task.done():
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job.task
        job.status = "cancelled"
        job.finished_at = job.finished_at or datetime.now(KST)
        return True

    # ------------------------------------------------------------------
    def _new_job(self, request: OptimizeRequest) -> OptimizeJob:
        finished = sorted((j for j in self.jobs.values() if j.status not in ("queued", "running")),
                          key=lambda j: j.created_at)
        while len(self.jobs) >= self.max_jobs and finished:
            self.jobs.pop(finished.pop(0).id, None)
        job = OptimizeJob(id=uuid.uuid4().hex[:12], request=request, created_at=datetime.now(KST))
        self.jobs[job.id] = job
        return job

    def _next_period(self, year: int) -> tuple[datetime, datetime, bool] | None:
        """다음 해 구간 (시작, 끝, 올해라 오늘까지인지). 올해면 오늘 0시까지로 잘라 캐시 키를 하루 동안 고정한다."""
        today = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = year_period(year + 1)
        if end <= today:
            return start, end, False
        if today <= start:
            return None
        return start, today, True

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _load_cache(self, key: str) -> dict[str, Any] | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("최적화 캐시를 읽지 못함 %s: %s", path, exc)
            return None

    def _save_cache(self, key: str, result: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path(key).with_suffix(".tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._cache_path(key))
        except OSError as exc:  # 캐시 실패가 결과를 막지 않게
            log.warning("최적화 캐시 저장 실패: %s", exc)

    async def _run(self, job: OptimizeJob) -> None:
        async with self._gate:
            if job.cancel_event.is_set():
                return
            job.status = "running"
            try:
                await self._run_locked(job)
                job.status = "done"
            except (asyncio.CancelledError, OptimizeCancelled):
                job.status = "cancelled"
            except (TraderError, ValueError) as exc:
                job.status = "error"
                job.error = str(exc)
            except Exception as exc:  # noqa: BLE001 - 예상 못 한 오류도 작업 상태로 알린다
                log.exception("연도별 최적 설정 실패 %s", job.id)
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.finished_at = datetime.now(KST)

    async def _run_locked(self, job: OptimizeJob) -> None:
        req = job.request
        next_period = self._next_period(req.year)
        today = self.now().date().isoformat() if next_period is not None and next_period[2] else None
        key = req.cache_key(today)
        cached = self._load_cache(key)
        if cached is not None:
            job.result, job.cached = cached, True
            return

        # 1) 그 해 캔들: 캔들 단위 × 마켓. 한 단위라도 못 받은 마켓은 빼서 모든 후보를 같은 마켓으로 비교한다
        start, end = year_period(req.year)
        job.phase, job.progress, job.total = "candles", 0, len(INTERVALS) * len(req.markets)
        frames: dict[str, dict[str, pd.DataFrame]] = {interval: {} for interval in INTERVALS}
        skipped: dict[str, str] = {}
        for market in req.markets:
            for interval in INTERVALS:
                if market not in skipped:
                    try:
                        frames[interval][market] = await self.loader(self.settings, market, interval, start, end)
                    except (TraderError, ValueError) as exc:
                        skipped[market] = str(exc)
                job.progress += 1
        used = [m for m in req.markets if m not in skipped]
        if not used:
            detail = "; ".join(f"{m}: {why}" for m, why in skipped.items())
            raise ValueError(f"{req.year}년 캔들이 있는 마켓이 없습니다 ({detail})")
        frames = {interval: {m: frames[interval][m] for m in used} for interval in INTERVALS}

        # 2) 모든 조합 백테스트 (CPU 작업 → 스레드, 진행률·중단은 작업 객체로 주고받는다)
        job.phase, job.progress = "backtest", 0
        job.total = count_runs(req.strategy_name, len(INTERVALS), len(used))

        def search() -> OptimizationOutcome:
            with quiet_backtest_logs():
                return optimize_year(
                    frames, req.strategy_name, req.risk, initial_capital=req.initial_capital, fee_rate=req.fee_rate,
                    slippage_rate=req.slippage_rate, on_progress=lambda n: setattr(job, "progress", n),
                    should_stop=job.cancel_event.is_set,
                )

        outcome = await asyncio.to_thread(search)

        # 3) 같은 설정으로 다음 해 (과거 한 해에 맞춘 값이 다음 해에도 통했는지 — 과최적화 확인용)
        job.phase, job.progress, job.total = "verify", 0, len(used)
        next_year = await self._verify_next_year(job, outcome.best, used, next_period)

        result = {
            "grid_version": GRID_VERSION, "strategy": req.strategy_name, "year": req.year, "markets": used,
            "skipped": [{"market": m, "reason": why} for m, why in skipped.items()],
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "initial_capital": req.initial_capital, "fee_rate": req.fee_rate, "slippage_rate": req.slippage_rate,
            "intervals": list(INTERVALS), "exit_rules": [r.label for r in EXIT_RULES],
            "evaluated": outcome.evaluated, "best": outcome.best.to_dict(),
            "top": [c.to_dict() for c in outcome.top], "next_year": next_year,
            "computed_at": datetime.now(KST).isoformat(),
        }
        self._save_cache(key, result)
        job.result = result

    async def _verify_next_year(self, job: OptimizeJob, best: CandidateResult, markets: list[str],
                                period: tuple[datetime, datetime, bool] | None) -> dict[str, Any] | None:
        if period is None:
            return None
        start, end, partial = period
        req = job.request
        frames: dict[str, pd.DataFrame] = {}
        for market in markets:
            with contextlib.suppress(TraderError, ValueError):  # 다음 해 캔들이 없으면 그 마켓은 뺀다
                frames[market] = await self.loader(self.settings, market, best.interval, start, end)
            job.progress += 1
        if not frames:
            return None
        risk = best.exit.apply(req.risk)

        def run() -> tuple[Any, ...]:
            with quiet_backtest_logs():
                return backtest_candidate(
                    frames, req.strategy_name, best.params, risk, interval=best.interval,
                    initial_capital=req.initial_capital, fee_rate=req.fee_rate, slippage_rate=req.slippage_rate,
                )

        results = await asyncio.to_thread(run)
        return {
            "year": req.year + 1, "partial": partial, "start": start.isoformat(), "end": end.isoformat(),
            "avg_return": sum(r.total_return for r in results) / len(results),
            "avg_benchmark": sum(r.benchmark_return for r in results) / len(results),
            "markets": [r.to_dict() for r in results],
        }
