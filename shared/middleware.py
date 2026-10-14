"""세 서비스가 같이 쓰는 미들웨어: X-Request-ID 처리 + 요청 로그, READ_ONLY_MODE 쓰기 차단."""
import logging
import time
import uuid

from fastapi import FastAPI, Request

from shared import config
from shared.errors import error_response
from shared.logs import request_id_var

access_logger = logging.getLogger("access")
# 운영 주소는 probe·모니터링이 몇 초마다 불러서 요청 로그에서 뺀다.
QUIET_PATHS = {"/healthz", "/readyz", "/ready", "/metrics"}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_REQUEST_ID_LENGTH = 64


def install_request_id(app: FastAPI) -> None:
    """X-Request-ID 헤더를 request.state.request_id 에 담고 응답 헤더로 돌려준다. 없거나 너무 길면 새로 만든다.

    요청마다 JSON 한 줄 요청 로그(request_id, method, path, status, duration_ms)를 남긴다.
    경로만 남기고 조회 조건(?q=...)·헤더·본문은 남기지 않는다. 가장 바깥 미들웨어로 등록한다 (처리 시간 전체를 잰다).
    """

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming = (request.headers.get("x-request-id") or "").strip()
        request_id = incoming if 0 < len(incoming) <= MAX_REQUEST_ID_LENGTH else uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500  # 처리 중 예외가 나면 바깥(ServerErrorMiddleware)이 500 으로 응답한다
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            path = request.url.path
            if path not in QUIET_PATHS or status >= 500:
                access_logger.info("request", extra={
                    "request_id": request_id, "method": request.method, "path": path, "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                })
            request_id_var.reset(token)


def install_read_only_guard(app: FastAPI) -> None:
    """READ_ONLY_MODE=true이면 조회(GET 등) 외의 모든 요청을 한 곳에서 503으로 막는다.

    응답은 세 서비스 모두 api-spec.md 형식: {"code": "WRITE_DISABLED", "message": ..., "request_id": ...}
    """

    @app.middleware("http")
    async def block_writes_when_disabled(request: Request, call_next):
        if config.READ_ONLY_MODE and request.method not in SAFE_METHODS:
            return error_response(
                503, "WRITE_DISABLED", "읽기 전용 모드라 쓰기 요청을 처리할 수 없습니다.",
                getattr(request.state, "request_id", None),
            )
        return await call_next(request)
