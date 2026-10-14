"""토큰 검증·권한 검사 (세 서비스 공통). 인증 서비스를 호출하지 않고 각 서비스가 직접 검증한다.

- JWT(HS256)를 JWT_SECRET 으로 검증 (서명·exp·iss·jti 필수)
- 같은 DB 의 sessions 에서 jti 를 찾아 끊긴 세션·유휴 시간·최대 시간을 확인 (보안 DEV3)
- 권한 표(shared/permissions.py)로 역할 검사. 표에 없는 API 는 403. 거부는 audit_logs 에 DENIED 로 남긴다.

쓰는 법 (서비스 main.py)
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(enforce_permissions)])
    def me(user: CurrentUser = Depends(get_current_user)): ...
    @api.post(..., dependencies=[Depends(require_roles("NURSE", "DIRECTOR"))])
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from fastapi import Depends, Request
from sqlalchemy import update
from sqlalchemy.orm import Session

from shared import audit, config, permissions
from shared.db import db_writable, get_db
from shared.errors import forbidden, unauthorized
from shared.models import User, UserSession, utcnow

JWT_ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32

# 세션 시간 (보안 DEV3): 관리 권한 역할은 더 짧게
ADMIN_SESSION_ROLES = frozenset({permissions.ADMIN, permissions.DIRECTOR})
IDLE_TIMEOUT = timedelta(minutes=30)
IDLE_TIMEOUT_ADMIN = timedelta(minutes=15)
ABSOLUTE_TIMEOUT = timedelta(hours=8)
ABSOLUTE_TIMEOUT_ADMIN = timedelta(hours=4)
# last_seen_at 은 1분에 한 번만 UPDATE (요청마다 쓰지 않도록)
TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class CurrentUser:
    """검증을 통과한 사용자. 역할·소속은 요청 본문이 아니라 DB(세션·계정) 값이다."""
    id: int
    login_id: str
    name: str
    role: str
    department_id: int | None
    patient_id: int | None
    session_id: str


def require_jwt_secret() -> None:
    """토큰을 쓰는 서비스가 시작할 때 부른다. 키가 없거나 짧으면 바로 멈춘다."""
    if len(config.JWT_SECRET.encode("utf-8")) < MIN_SECRET_BYTES:
        raise RuntimeError(f"환경변수 JWT_SECRET 이 없거나 {MIN_SECRET_BYTES}바이트보다 짧습니다.")


def session_timeouts(role: str) -> tuple[timedelta, timedelta]:
    """(유휴 시간, 최대 시간)"""
    if role in ADMIN_SESSION_ROLES:
        return IDLE_TIMEOUT_ADMIN, ABSOLUTE_TIMEOUT_ADMIN
    return IDLE_TIMEOUT, ABSOLUTE_TIMEOUT


def issue_token(user: User, session_id: str, issued_at: datetime, expires_at: datetime) -> str:
    """출입증(JWT) 발급. exp 는 세션 최대 시간과 같다."""
    claims = {
        "sub": str(user.id),
        "role": user.role,
        "department_id": user.department_id,
        "patient_id": user.patient_id,
        "jti": session_id,
        "iss": config.JWT_ISSUER,
        "iat": _epoch(issued_at),
        "exp": _epoch(expires_at),
    }
    return jwt.encode(claims, config.JWT_SECRET, algorithm=JWT_ALGORITHM)


def _epoch(naive_utc: datetime) -> int:
    return int((naive_utc - datetime(1970, 1, 1)).total_seconds())


def _decode(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[JWT_ALGORITHM],  # alg 를 하나로 고정 (none·다른 알고리즘 거부)
            issuer=config.JWT_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "jti"]},
        )
    except jwt.PyJWTError:
        raise unauthorized() from None


def _bearer_token(request: Request) -> str:
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()
    return token


def get_current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    """Authorization: Bearer 토큰 검증 + 세션 확인. 실패하면 모두 같은 401."""
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached

    claims = _decode(_bearer_token(request))
    session = db.get(UserSession, str(claims["jti"]))
    if session is None or session.revoked_at is not None or str(session.user_id) != str(claims["sub"]):
        raise unauthorized()
    now = utcnow()
    if now >= session.idle_expires_at or now >= session.absolute_expires_at:
        raise unauthorized()

    user = db.get(User, session.user_id)
    # 비활성화·역할 변경은 세션을 끊지만, 혹시 남아 있어도 DB 값과 다르면 거절한다.
    if user is None or not user.is_active or user.role != claims.get("role"):
        raise unauthorized()

    current = CurrentUser(
        id=user.id, login_id=user.login_id, name=user.name, role=user.role,
        department_id=user.department_id, patient_id=user.patient_id, session_id=session.id,
    )
    if now - session.last_seen_at >= TOUCH_INTERVAL:
        _touch_session(db, session, user.role, now)

    request.state.current_user = current
    return current


def _touch_session(db: Session, session: UserSession, role: str, now: datetime) -> None:
    """last_seen_at 갱신과 유휴 만료 연장 (최대 시간은 넘지 않음). DB 에 쓸 수 없는 상황이면 건너뛴다."""
    if not db_writable():
        return
    idle, _ = session_timeouts(role)
    try:
        db.execute(
            update(UserSession)
            .where(UserSession.id == session.id, UserSession.revoked_at.is_(None))
            .values(last_seen_at=now, idle_expires_at=min(now + idle, session.absolute_expires_at), updated_at=now)
        )
        db.commit()
    except Exception:
        db.rollback()  # 갱신 실패는 인증 실패가 아니다. 다음 요청에서 다시 시도한다.


def revoke_user_sessions(db: Session, user_id: int) -> int:
    """그 사용자의 살아 있는 세션을 모두 끊는다 (계정 비활성화·역할 변경 때). 끊은 개수를 돌려준다.

    COMMIT 은 부르는 쪽이 한다 (역할 변경과 같은 트랜잭션으로 묶기 위해).
    """
    now = utcnow()
    result = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, updated_at=now)
    )
    return result.rowcount or 0


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


def _deny(request: Request, user: CurrentUser | None) -> None:
    audit.write_audit(
        request, "ACCESS_DENIED", audit.DENIED,
        user_id=user.id if user else None,
        target_type="api", target_id=f"{request.method} {_route_path(request)}",
    )
    raise forbidden()


def enforce_permissions(request: Request, db: Session = Depends(get_db)) -> None:
    """라우터 전체에 거는 권한 검사. 권한 표에 없는 API 는 403, PUBLIC 이 아니면 로그인 필수."""
    roles = permissions.allowed_roles(request.method, _route_path(request))
    if roles == permissions.PUBLIC:
        return
    if roles is None:
        _deny(request, getattr(request.state, "current_user", None))
    user = get_current_user(request, db)
    if user.role not in roles:
        _deny(request, user)


def require_roles(*roles: str):
    """API 하나에 역할 조건을 더 거는 의존성. 역할이 아니면 403 + 감사 로그 DENIED."""
    allowed = frozenset(roles)
    unknown = allowed - permissions.ALL_ROLES
    if not allowed or unknown:
        raise ValueError(f"require_roles 에 잘못된 역할: {sorted(unknown) or '(없음)'}")

    def dependency(request: Request, user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            _deny(request, user)
        return user

    return dependency
