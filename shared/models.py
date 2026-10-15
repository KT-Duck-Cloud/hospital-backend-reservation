"""세 서비스가 같이 쓰는 표의 모델: users, sessions, audit_logs (주인: 인증 서비스).

표 원본은 db/schema.sql 이다. 여기는 앱이 읽고 쓰는 칸만 같은 이름·타입으로 맞춘 것이다.
- ENUM 칸은 String 으로 둔다 (허용 값 검사는 MySQL ENUM 이 한다. 테스트용 SQLite 에서도 동작하도록).
- 다른 표(departments, patients)를 가리키는 FK 는 DB 에만 있다 (SQLite 테스트에서 그 표 없이도 만들 수 있도록).
시각은 모두 UTC (시간대 정보 없는 값).
"""
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from shared.db import Base

# BIGINT 기본키: SQLite는 INTEGER여야 자동 증가가 되므로 SQLite일 때만 바꿔 준다.
IdType = BigInteger().with_variant(Integer, "sqlite")
# DATETIME(6): 마이크로초까지 (db/schema.sql 과 같음)
DateTime6 = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def utcnow() -> datetime:
    """현재 UTC 시각 (저장용으로 시간대 정보는 뗀다)"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    login_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    # 직원 업무용 연락처 (환자 계정은 NULL). 응답에서는 shared/phone.py 규칙으로만 공개한다
    work_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    patient_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)  # 환자 1명에 계정 1개
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_failed_at: Mapped[datetime | None] = mapped_column(DateTime6, nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime6, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime6, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)


class UserSession(Base):
    """로그인 세션 (보안 DEV3). id 는 JWT 의 jti."""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(IdType, ForeignKey("users.id"), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime6, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(IdType, ForeignKey("users.id"), nullable=True)
    service: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    result: Mapped[str] = mapped_column(String(10), nullable=False)  # SUCCESS / DENIED / FAIL
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)
