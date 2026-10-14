"""요청/응답의 모양과 입력 검사 규칙. 정해진 칸 외에는 받지 않는다 (extra=forbid → 422).

사용자 id·역할은 요청에서 받지 않는다. 토큰·세션(get_current_user) 값만 쓴다.
"""
import datetime as dt
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from shared import timeutil
from shared.timeutil import UtcDateTime

Id = Annotated[int, Field(ge=1, le=2**63 - 1)]
Memo = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
VisitType = Literal["FIRST", "RETURN"]
Status = Literal["BOOKED", "CANCELED"]
CheckinStatus = Literal["NOT_CHECKED_IN", "CHECKED_IN"]

MAX_PAGE_SIZE = 100


class ReservationCreate(BaseModel):
    """POST /api/v1/reservations 입력 (request_id 는 X-Request-ID 헤더로 받는다)"""
    model_config = ConfigDict(extra="forbid")

    patient_id: Id | None = None   # PATIENT 는 생략하면 본인. 다른 역할은 필수
    department_id: Id
    doctor_id: Id | None = None
    reserved_at: datetime
    visit_type: VisitType
    memo: Memo | None = None

    @field_validator("reserved_at")
    @classmethod
    def normalize_to_utc(cls, v: datetime) -> datetime:
        # 시간대가 없으면 UTC로 보고, 있으면 UTC로 변환한 뒤 시간대 정보를 뗀다.
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        if v < timeutil.utcnow_naive():
            raise ValueError("reserved_at 은 지금보다 뒤여야 합니다")
        return v


class ReservationFilter(BaseModel):
    """GET /api/v1/reservations 조회 조건. 날짜는 병원 기준 날짜(KST)로 해석한다."""
    model_config = ConfigDict(extra="forbid")

    date_from: date | None = None
    date_to: date | None = None
    status: Status | None = None
    checkin_status: CheckinStatus | None = None
    department_id: Id | None = None
    doctor_id: Id | None = None
    page: int = Field(1, ge=1, le=100_000)
    size: int = Field(20, ge=1, le=MAX_PAGE_SIZE)

    @model_validator(mode="after")
    def check_range(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from 은 date_to 보다 늦을 수 없습니다")
        return self


class QueueFilter(BaseModel):
    """GET /api/v1/queue: date 는 KST 날짜 (없으면 오늘). department_id 는 DIRECTOR·TESTER 만 고를 수 있다."""
    model_config = ConfigDict(extra="forbid")

    date: dt.date | None = None
    department_id: Id | None = None


class PatientSearch(BaseModel):
    """GET /api/v1/patients: 이름 일부 또는 차트번호 앞부분"""
    model_config = ConfigDict(extra="forbid")

    q: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    page: int = Field(1, ge=1, le=100_000)
    size: int = Field(20, ge=1, le=MAX_PAGE_SIZE)


class SlotFilter(BaseModel):
    """GET /api/v1/slots: date 는 KST 날짜"""
    model_config = ConfigDict(extra="forbid")

    doctor_id: Id
    date: dt.date


ChartNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20,
                                           pattern=r"^[A-Za-z0-9-]+$")]
PatientName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20, pattern=r"^[0-9-]+$")]


class PatientCreate(BaseModel):
    """POST /api/v1/patients 입력"""
    model_config = ConfigDict(extra="forbid")

    chart_no: ChartNo
    name: PatientName
    birth_date: date
    phone: Phone | None = None

    @field_validator("birth_date")
    @classmethod
    def reasonable_birth_date(cls, v: date) -> date:
        if v < date(1900, 1, 1) or v > date.today():
            raise ValueError("birth_date 가 올바르지 않습니다")
        return v


class DoctorFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    department_id: Id | None = None


class ReservationOut(BaseModel):
    """예약 응답"""
    model_config = ConfigDict(from_attributes=True)  # DB 객체를 바로 응답으로 바꿀 수 있게 함

    id: int
    request_id: str
    patient_id: int
    patient_name: str | None = None   # 화면 표시용 (범위 안의 예약만 보이므로 추가로 드러나는 정보는 없음)
    chart_no: str | None = None
    department_id: int
    doctor_id: int | None
    queue_no: int | None
    reserved_at: UtcDateTime
    visit_type: str
    status: str
    checkin_status: str
    checked_in_at: UtcDateTime | None
    memo: str | None
    created_by: int | None
    canceled_at: UtcDateTime | None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ReservationPage(BaseModel):
    items: list[ReservationOut]
    page: int
    size: int
    total: int


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str


class DoctorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    department_id: int
    name: str
    room_no: str | None


class PatientSummary(BaseModel):
    """환자 검색 결과. 전화번호는 가운데를 가린다."""
    id: int
    chart_no: str
    name: str
    birth_date: date
    phone_masked: str | None


class PatientPage(BaseModel):
    items: list[PatientSummary]
    page: int
    size: int
    total: int


class PatientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chart_no: str
    name: str
    birth_date: date
    phone: str | None
    created_at: UtcDateTime


class PatientDetail(PatientOut):
    recent_reservations: list[ReservationOut]   # 최근 예약 (예약 시각 최신순, 역할별 범위 적용)


class QueueItem(BaseModel):
    reservation_id: int
    queue_no: int
    department_id: int
    patient_id: int
    patient_name: str
    chart_no: str
    doctor_id: int | None
    doctor_name: str | None
    reserved_at: UtcDateTime
    checked_in_at: UtcDateTime


class QueueOut(BaseModel):
    date: dt.date                   # KST 날짜
    department_id: int | None       # DIRECTOR·TESTER 가 고르지 않으면 null (전체)
    items: list[QueueItem]          # 진료과, 대기번호 순


class SlotsOut(BaseModel):
    doctor_id: int
    date: dt.date                   # KST 날짜
    slot_minutes: int
    available: list[UtcDateTime]    # 예약 가능한 시작 시각 (UTC)


class MedicalRecordOut(BaseModel):
    """내 진료기록 (복호화한 값). 본인 확인을 통과한 뒤에만 만든다."""
    id: int
    reservation_id: int | None
    doctor_id: int
    doctor_name: str | None
    diagnosis: str
    note: str
    created_at: UtcDateTime


class ReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reservation_id: int | None
    amount: int
    pay_status: str
    paid_at: UtcDateTime | None
    created_at: UtcDateTime
