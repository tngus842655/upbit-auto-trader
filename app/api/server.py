"""대시보드 API 서버 (FastAPI) — Phase 8.

구조: 브라우저 ↔ FastAPI(이 프로세스) ↔ DB ↔ Trading Engine(별도 프로세스) ↔ Upbit
- 이 서버는 매매 판단을 하지 않는다. 상태·기록은 DB 에서 읽고, 제어는 ``bot_commands`` 큐에 넣고,
  설정은 ``bot_settings`` 에 새 버전으로 저장한다(엔진이 다음 캔들 경계에 반영).
- Start 는 엔진을 분리된 프로세스로 띄운다. 웹 서버가 꺼져도 엔진은 계속 돈다.
- 보안: 기본 127.0.0.1 바인드. ``DASHBOARD_TOKEN`` 을 설정하면 변경·제어 API 에 ``X-Auth-Token`` 이 필요하고,
  설정하지 않으면 로컬 호스트에서만 허용한다. LIVE 시작은 .env 이중 플래그 + 확인 문구가 모두 필요하다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.api.process import engine_is_alive, kill_engine, start_engine
from app.api.services import DashboardService, describe_api_error
from app.config.settings import Settings, TradingMode, get_settings
from app.core.exceptions import TraderError
from app.database.database import Database
from app.database.models import from_db_time
from app.exchange.models import KST, CandleInterval
from app.exchange.upbit_client import UpbitClient
from app.strategy import available_strategies
from app.trading.live_guard import LIVE_CONFIRM_PHRASE
from app.trading.runtime_settings import RuntimeSettings

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
COMMANDS = {"pause", "resume", "stop", "halt", "resume_risk", "reload"}
INTERVALS = [i.value for i in CandleInterval if i.value not in ("1s", "1w", "1M", "1y")]


def create_app(settings: Settings | None = None, *, db: Database | None = None,
               public_client: UpbitClient | None = None, service: DashboardService | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = db or Database(settings.database_url)
    db.create_all()
    public_client = public_client or UpbitClient(base_url=settings.upbit_api_url, timeout=settings.http_timeout_seconds)
    service = service or DashboardService(settings, db, public_client)

    app = FastAPI(title="upbit-auto-trader 대시보드", version="0.8")
    app.state.settings = settings
    app.state.db = db
    app.state.service = service

    # ------------------------------------------------------------------ 보안
    def _token_ok(provided: str | None) -> bool:
        token = settings.dashboard_token
        return token is not None and provided == token.get_secret_value()

    def require_auth(request: Request) -> None:
        if settings.dashboard_token is not None:
            if not _token_ok(request.headers.get("X-Auth-Token") or request.query_params.get("token")):
                raise HTTPException(status_code=401, detail="인증 토큰이 올바르지 않습니다 (X-Auth-Token)")
            return
        host = request.client.host if request.client else ""
        if host not in LOCAL_HOSTS:
            raise HTTPException(
                status_code=403, detail="토큰 없이 원격에서 변경할 수 없습니다. DASHBOARD_TOKEN 을 설정하세요."
            )

    def mode_param(mode: str = Query("paper", pattern="^(paper|live)$")) -> str:
        return mode

    # ------------------------------------------------------------------ 조회
    @app.get("/api/status")
    async def api_status(mode: str = Depends(mode_param)) -> dict[str, Any]:
        data = service.status(mode)
        alive, state = engine_is_alive(service.repo(mode))
        data["engine_alive"] = alive
        data["engine_state"] = state
        return data

    @app.get("/api/balance")
    async def api_balance(mode: str = Depends(mode_param)) -> dict[str, Any]:
        return await service.balance(mode)

    @app.get("/api/positions")
    async def api_positions(mode: str = Depends(mode_param)) -> list[dict[str, Any]]:
        return (await service.balance(mode))["positions"]

    @app.get("/api/performance")
    async def api_performance(mode: str = Depends(mode_param)) -> dict[str, Any]:
        return await service.performance(mode)

    @app.get("/api/recent")
    async def api_recent(mode: str = Depends(mode_param), limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        return service.recent(mode, limit)

    @app.get("/api/orders")
    async def api_orders(mode: str = Depends(mode_param), limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        return service.recent(mode, limit)["orders"]

    @app.get("/api/trades")
    async def api_trades(mode: str = Depends(mode_param), limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        return service.recent(mode, limit)["round_trips"]

    @app.get("/api/signals")
    async def api_signals(
        mode: str = Depends(mode_param), limit: int = Query(50, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        return service.recent(mode, limit)["signals"]

    @app.get("/api/logs")
    async def api_logs(mode: str = Depends(mode_param), limit: int = Query(50, ge=1, le=500),
                       level: str | None = None) -> list[dict[str, Any]]:
        repo = service.repo(mode)
        return [
            {"time": from_db_time(e.time).astimezone(KST).isoformat(), "level": e.level, "event": e.event,
             "message": e.message, "data": e.data}
            for e in repo.recent_logs(limit, level=level)
        ]

    # ------------------------------------------------------------------ 전략 · 설정
    def _current_runtime(mode: str) -> tuple[RuntimeSettings, int]:
        repo = service.repo(mode)
        loaded = repo.load_runtime_settings()
        if loaded is not None:
            data, version = loaded
            try:
                return RuntimeSettings(**data), version
            except ValidationError:
                log.warning("DB 실행 설정 v%d 검증 실패 → .env 값으로 표시", version)
        return RuntimeSettings.from_settings(settings), 0

    @app.get("/api/strategy")
    async def api_strategy(mode: str = Depends(mode_param)) -> dict[str, Any]:
        runtime, version = _current_runtime(mode)
        strategies = available_strategies()
        return {
            "current": runtime.to_dict(), "version": version,
            "available": strategies,
            "schemas": {name: RuntimeSettings.strategy_param_schema(name) for name in strategies},
            "intervals": INTERVALS,
            "risk_schema": type(runtime.risk).model_json_schema(),
        }

    @app.get("/api/settings")
    async def api_settings(mode: str = Depends(mode_param)) -> dict[str, Any]:
        runtime, version = _current_runtime(mode)
        history = [
            {"version": h.version, "note": h.note, "created_at": from_db_time(h.created_at).astimezone(KST).isoformat()}
            for h in service.repo(mode).runtime_settings_history(20)
        ]
        return {
            "version": version, "data": runtime.to_dict(), "history": history,
            "hot_fields": ["strategy_name", "strategy_params", "risk"],
            "restart_fields": ["markets", "candle_interval"],
        }

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    async def api_settings_update(
        payload: dict[str, Any] = Body(...), mode: str = Depends(mode_param)
    ) -> dict[str, Any]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="본문은 {\"data\": {...}, \"note\": \"...\"} 형식이어야 합니다")
        try:
            new = RuntimeSettings(**data)
        except ValidationError as exc:
            errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
            raise HTTPException(status_code=422, detail=errors) from exc
        current, _ = _current_runtime(mode)
        changes = current.changes_vs(new)
        note_text = str(payload.get("note") or "대시보드 저장")
        version = service.repo(mode).save_runtime_settings(new.to_dict(), note=note_text)
        alive, _ = engine_is_alive(service.repo(mode))
        note = "엔진이 다음 캔들 경계에 즉시 반영 항목을 적용합니다."
        if not alive:
            note = "엔진이 꺼져 있어 다음 시작 때 적용됩니다."
        if changes["restart"]:
            note += " 마켓·캔들 단위 변경은 엔진 재시작(Stop → Start)이 필요합니다."
        return {"version": version, "changes": changes, "note": note, "data": new.to_dict()}

    # ------------------------------------------------------------------ 제어
    @app.post("/api/bot/start", dependencies=[Depends(require_auth)])
    async def api_bot_start(
        payload: dict[str, Any] | None = Body(None), mode: str = Depends(mode_param)
    ) -> dict[str, Any]:
        payload = payload or {}
        repo = service.repo(mode)
        alive, state = engine_is_alive(repo)
        if alive:
            raise HTTPException(status_code=409, detail=f"엔진이 이미 실행 중입니다 ({state})")
        confirm = str(payload.get("confirm_live") or "")
        if mode == "live":
            if settings.trading_mode is not TradingMode.LIVE or not settings.is_live_trading_allowed:
                raise HTTPException(
                    status_code=403, detail=".env 에 TRADING_MODE=LIVE 와 LIVE_TRADING_ENABLED=true 가 모두 필요합니다"
                )
            if not settings.has_api_keys:
                raise HTTPException(
                    status_code=403, detail="LIVE 에는 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 가 필요합니다"
                )
            if confirm != LIVE_CONFIRM_PHRASE:
                raise HTTPException(
                    status_code=403, detail=f"확인 문구가 다릅니다. '{LIVE_CONFIRM_PHRASE}' 를 정확히 입력하세요"
                )
        elif settings.trading_mode is TradingMode.LIVE:
            # .env 가 LIVE 여도 대시보드에서 paper 를 고르면 PAPER 로 띄운다 (start_engine 이 TRADING_MODE 를 덮어씀)
            pass
        pid = start_engine(settings, mode, confirm_live=confirm)
        repo.log("INFO", "dashboard_start", f"대시보드에서 엔진 시작 요청 (pid {pid})", {"mode": mode})
        return {"started": True, "pid": pid, "mode": mode}

    @app.post("/api/bot/kill", dependencies=[Depends(require_auth)])
    async def api_bot_kill(mode: str = Depends(mode_param)) -> dict[str, Any]:
        repo = service.repo(mode)
        es = repo.read_engine_status()
        if es is None or not es.pid:
            raise HTTPException(status_code=404, detail="엔진 PID 를 알 수 없습니다")
        ok = kill_engine(int(es.pid))
        repo.write_engine_status(status="STOPPED", message="대시보드에서 강제 종료")
        repo.log("WARNING", "dashboard_kill", f"엔진 강제 종료 (pid {es.pid})")
        return {"killed": ok, "pid": es.pid}

    @app.post("/api/bot/{command}", dependencies=[Depends(require_auth)])
    async def api_bot_command(command: str, payload: dict[str, Any] | None = Body(None),
                              mode: str = Depends(mode_param)) -> dict[str, Any]:
        command = command.replace("-", "_")
        if command not in COMMANDS:
            raise HTTPException(status_code=404, detail=f"알 수 없는 명령: {command}")
        repo = service.repo(mode)
        alive, state = engine_is_alive(repo)
        cmd_id = repo.enqueue_command(command, (payload or {}) or None)
        return {"queued": True, "command": command, "id": cmd_id, "engine_alive": alive, "engine_state": state}

    # ------------------------------------------------------------------ 포켓
    @app.get("/api/pockets", dependencies=[Depends(require_auth)])
    async def api_pockets() -> dict[str, Any]:
        return await service.pockets()

    @app.post("/api/pockets/transfer", dependencies=[Depends(require_auth)])
    async def api_pockets_transfer(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            amount = float(payload.get("amount") or 0)
            result = await service.transfer(
                direction=str(payload.get("direction") or ""), amount=amount,
                currency=str(payload.get("currency") or "KRW"), bot_pocket_uuid=payload.get("bot_pocket_uuid"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TraderError as exc:
            raise HTTPException(status_code=502, detail=describe_api_error(exc)) from exc
        summary = f"{payload.get('direction')} {amount} {payload.get('currency', 'KRW')}"
        service.repo("paper").log("INFO", "pocket_transfer", summary, result)
        return result

    # ------------------------------------------------------------------ 실시간
    @app.websocket("/ws")
    async def ws_live(websocket: WebSocket, mode: str = "paper", token: str | None = None) -> None:
        if settings.dashboard_token is not None and not _token_ok(token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                status = service.status(mode)
                alive, state = engine_is_alive(service.repo(mode))
                status["engine_alive"], status["engine_state"] = alive, state
                balance = await service.balance(mode)
                recent = service.recent(mode, 5)
                await websocket.send_json({"type": "tick", "status": status, "balance": balance,
                                           "latest_signal": recent["signals"][0] if recent["signals"] else None})
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001 - 소켓 오류는 로그만
            log.warning("WebSocket 종료: %s", exc)

    # ------------------------------------------------------------------ 정적 파일
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(TraderError)
    async def trader_error_handler(_: Request, exc: TraderError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": describe_api_error(exc)})

    return app
