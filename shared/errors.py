"""오류 응답 형식 (docs/api-spec.md): {"code": ..., "message": ..., "request_id": ...}

install_error_handlers(app) 가 거는 것
- ApiError                → 지정한 코드 그대로
- DB 가 쓰기를 거부       → 503 WRITE_DISABLED  (read_only / super_read_only, MySQL 1290·1836·1792)
- DB 접속 불가·끊김·잠금  → 503 DB_UNAVAILABLE
- 진료기록 암호 오류      → 503 RECORD_UNAVAILABLE (키 없음·인증 태그 불일치. 평문·이유를 응답에 넣지 않는다)
- 그 밖의 예외            → 500 INTERNAL_ERROR (내부 오류 문구·SQL 은 응답에 넣지 않는다)
- 입력 형식 오류(422)     → FastAPI 기본 모양 {"detail": [...]} 을 쓰되, 보낸 값(input)은 되돌려 주지 않는다
"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc

from shared.crypto import CryptoError
from shared.db import reset_pool

logger = logging.getLogger("app.errors")

# MySQL 오류 번호
MYSQL_WRITE_REJECTED = {
    1290,  # ER_OPTION_PREVENTS_STATEMENT: --read-only / --super-read-only 로 실행 불가
    1792,  # ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION
    1836,  # ER_READ_ONLY_MODE
}
MYSQL_UNAVAILABLE = {
    1040,  # 연결 수 초과
    1044, 1045,  # 접근 거부 (계정·비밀번호·권한)
    1053,  # 서버 종료 중
    1129,  # 호스트 차단
    1203,  # 사용자 연결 수 초과
    1205, 1213,  # 잠금 대기 시간 초과·교착 (잠시 뒤 다시 시도)
    2002, 2003, 2005, 2006, 2013, 2055,  # 접속 불가·연결 끊김
    3118,  # 계정 잠김
    3159,  # TLS 필요 (REQUIRE SSL)
    4031,  # 유휴로 서버가 연결을 끊음
}


def error_response(status_code: int, code: str, message: str, request_id: str | None = None,
                   headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message, "request_id": request_id},
        headers=headers,
    )


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


class ApiError(Exception):
    """api-spec.md 형식으로 응답할 오류. raise ApiError(403, "FORBIDDEN", "권한이 없습니다")"""

    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def unauthorized() -> ApiError:
    # 401 이유(토큰 없음·만료·위조·세션 끊김)는 알려 주지 않는다.
    return ApiError(401, "UNAUTHORIZED", "인증이 필요합니다", headers={"WWW-Authenticate": "Bearer"})


def forbidden() -> ApiError:
    return ApiError(403, "FORBIDDEN", "권한이 없습니다")


def not_found(message: str = "찾을 수 없습니다") -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def bad_request(message: str) -> ApiError:
    return ApiError(400, "BAD_REQUEST", message)


def conflict(message: str) -> ApiError:
    return ApiError(409, "CONFLICT", message)


# 연결이 끊긴 경우 (교착·잠금 대기 1205·1213 은 연결 문제가 아니라 풀을 비우지 않는다)
MYSQL_CONNECTION_LOST = {1053, 2002, 2003, 2005, 2006, 2013, 2055, 4031}


def mysql_error_code(error: BaseException) -> int | None:
    """SQLAlchemy 가 감싼 MySQL 오류에서 오류 번호를 꺼낸다."""
    orig = getattr(error, "orig", None)
    args = getattr(orig, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def classify_db_error(error: BaseException) -> str | None:
    """'WRITE_DISABLED' / 'DB_UNAVAILABLE' / None(그 밖의 DB 오류)"""
    code = mysql_error_code(error)
    if code in MYSQL_WRITE_REJECTED:
        return "WRITE_DISABLED"
    if code in MYSQL_UNAVAILABLE:
        return "DB_UNAVAILABLE"
    if isinstance(error, (sa_exc.OperationalError, sa_exc.DisconnectionError, sa_exc.TimeoutError)):
        return "DB_UNAVAILABLE"
    return None


def needs_new_connections(error: BaseException) -> bool:
    """풀의 연결을 버리고 새로 붙어야 하는 DB 오류인지: 연결 끊김, 또는 DB 가 쓰기를 거부(승격 뒤 옛 원본)."""
    code = mysql_error_code(error)
    return (code in MYSQL_CONNECTION_LOST or code in MYSQL_WRITE_REJECTED
            or bool(getattr(error, "connection_invalidated", False)) or isinstance(error, sa_exc.DisconnectionError))


DB_MESSAGES = {
    "WRITE_DISABLED": "지금은 데이터를 저장할 수 없습니다 (읽기 전용)",
    "DB_UNAVAILABLE": "데이터베이스에 연결할 수 없습니다. 잠시 후 다시 시도하세요",
}


def install_error_handlers(app: FastAPI) -> None:
    """오류를 api-spec.md 형식으로 바꿔 응답한다. 내부 오류 내용은 응답에 넣지 않는다."""

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return error_response(exc.status_code, exc.code, exc.message, request_id_of(request), exc.headers)

    @app.exception_handler(sa_exc.SQLAlchemyError)
    async def handle_db_error(request: Request, exc: sa_exc.SQLAlchemyError):
        kind = classify_db_error(exc)
        # 운영 로그에는 종류·번호만 남긴다 (SQL·값은 남기지 않음)
        logger.warning("db error", extra={"kind": kind or "OTHER", "exc_type": type(exc).__name__,
                                          "mysql_code": mysql_error_code(exc), "request_id": request_id_of(request)})
        if needs_new_connections(exc):
            # DB 가 재시작·승격되면 풀에 남은 연결이 모두 죽어 있거나 옛 원본(read_only)에 붙어 있다.
            # 풀을 비워 다음 요청이 새로 연결하게 한다 (새 연결은 DB 주소를 다시 찾아 새 원본으로 간다).
            reset_pool()
        if kind:
            return error_response(503, kind, DB_MESSAGES[kind], request_id_of(request))
        return error_response(500, "INTERNAL_ERROR", "서버 오류가 발생했습니다", request_id_of(request))

    @app.exception_handler(CryptoError)
    async def handle_crypto_error(request: Request, exc: CryptoError):
        # 어떤 이유인지(키 없음/변조)는 운영 로그에만 남긴다. 키·평문은 남기지 않는다.
        logger.error("crypto error", extra={"exc_type": type(exc).__name__, "request_id": request_id_of(request)})
        return error_response(503, "RECORD_UNAVAILABLE", "지금은 진료기록을 볼 수 없습니다", request_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        details = [{"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        logger.error("unexpected error", extra={"exc_type": type(exc).__name__, "request_id": request_id_of(request)})
        return error_response(500, "INTERNAL_ERROR", "서버 오류가 발생했습니다", request_id_of(request))
