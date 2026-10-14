"""감사 로그 (audit_logs). 세 서비스가 같이 쓴다.

- write_audit: 요청 처리용 DB 세션과 따로, 짧은 트랜잭션으로 바로 COMMIT 한다 (거부·로그인 실패처럼
  데이터 변경이 없거나 요청이 ROLLBACK 돼도 남아야 하는 기록).
- add_audit: 데이터 변경과 같은 트랜잭션에 넣는다. 감사 로그 저장이 실패하면 변경도 함께 취소된다.
- READ_ONLY_MODE 이거나 조회 전용 계정(stats)이면 DB 에 쓰지 않고 표준 출력 로그로만 남긴다 (docs/erd.md).
- DB 쓰기가 실패해도 요청은 막지 않고 표준 출력으로 남긴다.
- 비밀번호·토큰은 받지 않는다. 함수 인자에 그런 칸이 없다.
"""
import logging

from fastapi import Request
from sqlalchemy import insert
from sqlalchemy.orm import Session

from shared.db import db_writable, engine
from shared.models import AuditLog, utcnow

SUCCESS, DENIED, FAIL = "SUCCESS", "DENIED", "FAIL"

# 표준 출력 JSON 한 줄 (shared/logs.py). 감사 내용은 "audit" 칸에 들어간다.
logger = logging.getLogger("audit")


def client_ip_of(request: Request) -> str | None:
    # 인그레스 뒤에서는 uvicorn --proxy-headers / --forwarded-allow-ips 설정으로 실제 IP 가 들어온다.
    return request.client.host if request.client else None


def service_name_of(request: Request) -> str:
    return getattr(request.app.state, "service_name", "unknown")


def _row(request: Request, action: str, result: str, user_id: int | None,
         target_type: str | None, target_id: str | int | None) -> dict:
    return {
        "user_id": user_id,
        "service": service_name_of(request)[:20],
        "action": action[:50],
        "target_type": target_type[:30] if target_type else None,
        "target_id": str(target_id)[:64] if target_id is not None else None,
        "request_id": (getattr(request.state, "request_id", None) or "")[:64] or None,
        "client_ip": (client_ip_of(request) or "")[:45] or None,
        "result": result,
    }


def add_audit(
    db: Session,
    request: Request,
    action: str,
    result: str,
    user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
) -> None:
    """데이터 변경과 같은 트랜잭션에 감사 로그를 넣는다. COMMIT 은 부르는 쪽이 한다."""
    now = utcnow()
    db.add(AuditLog(**_row(request, action, result, user_id, target_type, target_id), created_at=now, updated_at=now))


def write_audit(
    request: Request,
    action: str,
    result: str,
    user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
) -> None:
    """감사 로그를 따로 바로 저장한다. 실패해도 요청은 막지 않고 표준 출력으로 남긴다."""
    row = _row(request, action, result, user_id, target_type, target_id)
    if db_writable():
        try:
            now = utcnow()
            with engine.begin() as conn:
                conn.execute(insert(AuditLog).values(**row, created_at=now, updated_at=now))
            return
        except Exception as exc:  # 감사 기록 실패로 요청을 실패시키지 않는다.
            logger.warning("audit", extra={"audit": row, "db_error": type(exc).__name__})
            return
    logger.info("audit", extra={"audit": row})
