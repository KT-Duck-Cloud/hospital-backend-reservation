"""예약 서비스가 주인인 표의 모델 (원본: db/schema.sql, docs/erd.md). 시각은 모두 UTC (시간대 정보 없는 값).

ENUM 칸은 String 으로 둔다 (허용 값 검사는 MySQL ENUM 과 요청 검사(schemas.py)가 한다. 테스트용 SQLite 에서도 동작하도록).
"""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, ForeignKeyConstraint, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db import Base
from shared.models import DateTime6, IdType, User, utcnow  # noqa: F401  (users 표를 FK 대상으로 등록)

# request_id: "ABC"와 "abc"를 다른 값으로 취급하도록 MySQL에서만 utf8mb4_bin을 지정한다. (SQLite는 이 규칙을 몰라 오류)
RequestIdType = String(64).with_variant(String(64, collation="utf8mb4_bin"), "mysql")
# INT 기본키 (departments, doctors)
IntIdType = Integer()


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(IntIdType, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 부서 대표 번호
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(IntIdType, primary_key=True, autoincrement=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(IdType, ForeignKey("users.id"), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    room_no: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    chart_no: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        # 같은 진료과·같은 날 대기번호 중복 방지 (접수 전에는 NULL 이라 걸리지 않는다)
        UniqueConstraint("department_id", "queue_date", "queue_no", name="uq_reservations_queue"),
        # 진료기록·영수증이 (예약, 환자) 쌍으로 가리키는 외래키의 대상
        UniqueConstraint("id", "patient_id", name="uq_reservations_id_patient"),
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    # UNIQUE: 같은 X-Request-ID 요청이 두 번 저장되지 않게 DB가 막아 준다.
    request_id: Mapped[str] = mapped_column(RequestIdType, nullable=False, unique=True)
    patient_id: Mapped[int] = mapped_column(IdType, ForeignKey("patients.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True)
    queue_no: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 접수 시 부서별·날짜(KST)별 대기번호
    queue_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # 대기번호를 매긴 날짜 (KST)
    reserved_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False)
    visit_type: Mapped[str] = mapped_column(String(10), nullable=False)          # FIRST / RETURN
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="BOOKED")    # BOOKED / CANCELED
    # NOT_CHECKED_IN → CHECKED_IN(진료대기) → IN_TREATMENT(진료중) → COMPLETED(진료완료). 앞으로만 간다
    checkin_status: Mapped[str] = mapped_column(String(20), nullable=False, default="NOT_CHECKED_IN")
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime6, nullable=True)
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[int | None] = mapped_column(IdType, ForeignKey("users.id"), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime6, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)

    # 응답에 환자 이름·차트번호를 함께 내려 주기 위한 연결 (목록은 selectinload 로 한 번에 읽는다)
    patient: Mapped["Patient"] = relationship(lazy="select")

    @property
    def patient_name(self) -> str | None:
        return self.patient.name if self.patient else None

    @property
    def chart_no(self) -> str | None:
        return self.patient.chart_no if self.patient else None


class MedicalRecord(Base):
    """진료기록. 진단·소견은 AES-256-GCM 암호문으로만 저장한다 (shared/crypto.py). 평문 칸은 없다."""
    __tablename__ = "medical_records"
    __table_args__ = (
        # 예약의 환자와 기록의 환자가 다르면 저장 거부 (reservation_id 가 NULL 이면 검사하지 않음)
        ForeignKeyConstraint(["reservation_id", "patient_id"], ["reservations.id", "reservations.patient_id"],
                             name="fk_medical_records_reservation_patient"),
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(IdType, ForeignKey("patients.id"), nullable=False)
    reservation_id: Mapped[int | None] = mapped_column(IdType, nullable=True, unique=True)  # 예약 1건에 기록 1건
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    diagnosis_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)   # 암호문 + 인증 태그
    note_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    diagnosis_nonce: Mapped[bytes] = mapped_column(VARBINARY(12).with_variant(LargeBinary(12), "sqlite"),
                                                   nullable=False)
    note_nonce: Mapped[bytes] = mapped_column(VARBINARY(12).with_variant(LargeBinary(12), "sqlite"), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (
        ForeignKeyConstraint(["reservation_id", "patient_id"], ["reservations.id", "reservations.patient_id"],
                             name="fk_receipts_reservation_patient"),
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    patient_id: Mapped[int] = mapped_column(IdType, ForeignKey("patients.id"), nullable=False)
    reservation_id: Mapped[int | None] = mapped_column(IdType, nullable=True, unique=True)  # 예약 1건에 영수증 1건
    amount: Mapped[int] = mapped_column(Integer, nullable=False)                 # 원 단위
    pay_status: Mapped[str] = mapped_column(String(10), nullable=False, default="UNPAID")  # PAID / UNPAID / REFUNDED
    paid_at: Mapped[datetime | None] = mapped_column(DateTime6, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime6, nullable=False, default=utcnow, onupdate=utcnow)
