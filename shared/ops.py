"""운영 주소: /healthz, /readyz(/ready), /metrics. 인증 없음. 로그 설정과 종료 처리도 여기서 붙인다."""
import logging
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from sqlalchemy import text

from shared import config
from shared.db import engine
from shared.logs import setup_logging

logger = logging.getLogger("app")

READYZ_TIMEOUT_SECONDS = 1
# DB가 멈춰도 /readyz 요청이 묶이지 않도록, 검사는 별도 스레드에서 돌리고 1초만 기다린다.
_ready_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="readyz")


class HealthOut(BaseModel):
    status: str
    service: str
    version: str


def check_db() -> str | None:
    """DB 검사. 문제가 없으면 None, 있으면 오류 코드를 돌려준다."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            # 읽기 전용 확인은 MySQL에서만 한다. (SQLite는 건너뜀)
            if not config.READ_ONLY_MODE and engine.dialect.name == "mysql":
                if conn.execute(text("SELECT @@read_only")).scalar():
                    return "DB_READ_ONLY"
    except Exception:
        return "DB_UNAVAILABLE"
    return None


def ready_error() -> str | None:
    """/readyz 와 같은 검사 (1초 제한). 준비됐으면 None, 아니면 오류 코드."""
    try:
        return _ready_pool.submit(check_db).result(timeout=READYZ_TIMEOUT_SECONDS)
    except FutureTimeout:
        return "DB_TIMEOUT"


def build_ops_router(service_name: str) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", response_model=HealthOut)
    def healthz():
        """프로그램이 살아 있는지만 확인한다. (DB는 확인하지 않음)"""
        return {"status": "ok", "service": service_name, "version": config.APP_VERSION}

    @router.get("/readyz")
    @router.get("/ready", include_in_schema=False)  # /ready 도 같은 함수로 연결한다.
    def readyz():
        """DB SELECT 1 성공이면 200, 실패·1초 초과·DB가 읽기 전용(쓰기 모드일 때)이면 503."""
        error = ready_error()
        if error:
            return JSONResponse(status_code=503, content={"status": "not_ready", "error": error})
        return {"status": "ready"}

    return router


def install_graceful_shutdown(app: FastAPI) -> None:
    """종료 신호(SIGTERM) 처리.

    uvicorn 이 SIGTERM 을 받으면 새 연결을 받지 않고, 처리 중 요청이 끝나기를 최대 --timeout-graceful-shutdown(20초)
    기다린 뒤 이 종료 단계를 부른다. 여기서 DB 연결 풀을 닫는다 (Helm: preStop 5초 + 20초 < terminationGracePeriodSeconds 30초).
    """
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state:
            yield state
        engine.dispose()
        logger.info("shutdown complete: in-flight requests finished, db pool closed")

    app.router.lifespan_context = lifespan


def install_ops(app: FastAPI, service_name: str) -> None:
    """/healthz, /readyz, /ready, /metrics 를 앱에 붙이고, JSON 로그·종료 처리를 설정한다."""
    setup_logging(service_name)
    install_graceful_shutdown(app)
    app.state.service_name = service_name  # 감사 로그의 service 칸에 쓴다.
    app.include_router(build_ops_router(service_name))
    # /metrics: Prometheus 지표 (GET이라 읽기 전용 모드에서도 열려 있다)
    Instrumentator().instrument(app).expose(app, include_in_schema=False)
