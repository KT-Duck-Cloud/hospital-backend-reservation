"""예약 서비스: R1~R7 (docs/api-spec.md). 모든 API 는 로그인 필수 + 권한 표(shared/permissions.py).

역할별 범위 (요청 값이 아니라 토큰·세션의 사용자 정보로 정한다)
- PATIENT          : 본인(patient_id) 예약만
- NURSE, DEPT_HEAD : 자기 부서(department_id) 예약만
- DIRECTOR, TESTER : 전체
- MANAGER(원무과장): 부서와 상관없이 예약 조회·접수·취소 (예약 만들기는 불가). 응답의 예약 메모(memo)는 비운다
범위 밖이면 403 + 감사 로그 DENIED. 예약 생성·취소·접수·진료 시작·진료 완료는 같은 트랜잭션으로 감사 로그를 남긴다.

진료 진행 (reservations.checkin_status): NOT_CHECKED_IN → CHECKED_IN(진료대기) → IN_TREATMENT(진료중) → COMPLETED(진료완료).
앞으로만 간다. 진료 시작·완료는 그 예약의 담당 의사(doctors.user_id = 로그인 계정, doctor_id 일치)만 할 수 있다.
"""
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from app import models, schemas
from shared import audit, config, crypto, timeutil
from shared.auth import CurrentUser, enforce_permissions, get_current_user, require_jwt_secret
from shared.db import get_db
from shared.errors import (
    ApiError, bad_request, conflict, forbidden, install_error_handlers, mysql_error_code, not_found,
)
from shared.middleware import install_read_only_guard, install_request_id
from shared.models import User, utcnow
from shared.ops import install_ops
from shared.timeutil import kst_date_of, kst_day_range_utc, today_kst
from shared.permissions import DEPT_HEAD, DIRECTOR, DOCTOR, MANAGER, NURSE, PATIENT, TESTER
from shared.phone import (  # noqa: F401  (mask_phone: 기존 import 경로 유지)
    can_see_department_phone, can_see_patient_phone, can_see_staff_phone, mask_phone, shown,
)

SERVICE_NAME = "reservation"
MAX_REQUEST_ID_LENGTH = 64

DEPARTMENT_SCOPE_ROLES = frozenset({NURSE, DEPT_HEAD})
ALL_SCOPE_ROLES = frozenset({DIRECTOR, TESTER})
# 원무과장: 예약 조회·접수·취소는 전체 부서. 대기열(R8)·예약 만들기(R1)는 권한 표에서 막혀 있다.
FRONT_DESK_ROLES = frozenset({MANAGER})

require_jwt_secret()  # JWT_SECRET 이 없으면 시작하지 않는다.

app = FastAPI(title="병원 예약 서비스", version=config.APP_VERSION)
api = APIRouter(prefix="/api/v1", dependencies=[Depends(enforce_permissions)])

install_ops(app, SERVICE_NAME)  # /healthz, /readyz, /ready, /metrics
install_error_handlers(app)
install_read_only_guard(app)
install_request_id(app)


# ---------- 역할별 범위 ----------

def deny(request: Request, user: CurrentUser, target_type: str, target_id) -> None:
    """범위 밖 접근: 감사 로그 DENIED 를 남기고 403."""
    audit.write_audit(request, "ACCESS_DENIED", audit.DENIED, user_id=user.id,
                      target_type=target_type, target_id=target_id)
    raise forbidden()


def in_scope(user: CurrentUser, reservation: models.Reservation) -> bool:
    if user.role == PATIENT:
        return user.patient_id is not None and reservation.patient_id == user.patient_id
    if user.role in DEPARTMENT_SCOPE_ROLES:
        return user.department_id is not None and reservation.department_id == user.department_id
    return user.role in ALL_SCOPE_ROLES or user.role in FRONT_DESK_ROLES


def reservation_view(user: CurrentUser, reservation: models.Reservation) -> schemas.ReservationOut:
    """예약 응답. 원무과장에게는 예약 메모(진료 관련 내용이 적힐 수 있음)를 주지 않는다.

    진단·진료 메모(medical_records)는 이 서비스의 R11(PATIENT 본인) 말고는 어떤 응답에도 넣지 않는다.
    """
    view = schemas.ReservationOut.model_validate(reservation)
    if user.role == MANAGER:
        view.memo = None
    return view


def load_in_scope(db: Session, request: Request, user: CurrentUser, reservation_id: int,
                  for_update: bool = False) -> models.Reservation:
    """예약 한 건. 없으면 404, 범위 밖이면 403."""
    stmt = select(models.Reservation).where(models.Reservation.id == reservation_id)
    if for_update:
        stmt = stmt.with_for_update()
    reservation = db.scalar(stmt)
    if reservation is None:
        raise not_found("예약을 찾을 수 없습니다")
    if not in_scope(user, reservation):
        deny(request, user, "reservation", reservation_id)
    return reservation


SLOT = timedelta(minutes=30)  # 진료 한 칸


def doctor_slot_taken(db: Session, doctor_id: int, reserved_at: datetime) -> bool:
    """같은 의사의 BOOKED 예약이 이 시각의 30분 칸과 겹치면 True. 취소된 예약은 자리를 비운다.

    잠금 읽기(FOR UPDATE)로 확인한다: MySQL 기본(REPEATABLE READ)에서 일반 SELECT 는 트랜잭션 앞부분의
    스냅숏을 봐서, 방금 다른 요청이 저장한 예약을 놓칠 수 있다.
    """
    R = models.Reservation
    return db.scalar(
        select(R.id).where(
            R.doctor_id == doctor_id, R.status == "BOOKED",
            R.reserved_at > reserved_at - SLOT, R.reserved_at < reserved_at + SLOT,
        ).limit(1).with_for_update()
    ) is not None


def find_by_request_id(db: Session, request_id: str) -> models.Reservation | None:
    return db.scalar(select(models.Reservation).where(models.Reservation.request_id == request_id))


# ---------- R1 예약 만들기 ----------

@api.post("/reservations", response_model=schemas.ReservationOut, status_code=status.HTTP_201_CREATED)
def create_reservation(
    body: schemas.ReservationCreate,
    request: Request,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_request_id: Annotated[
        str | None,
        Header(description="중복 요청 방지용 ID (1~64자). 같은 ID로 다시 보내면 기존 예약을 200으로 돌려준다."),
    ] = None,
):
    """예약 생성. COMMIT 이 끝난 뒤에 응답한다. 같은 X-Request-ID 면 기존 예약을 200으로 돌려준다."""
    request_id = (x_request_id or "").strip()
    if not request_id or len(request_id) > MAX_REQUEST_ID_LENGTH:
        raise bad_request("X-Request-ID 헤더가 필요합니다 (1~64자)")

    # 누구 이름으로 만드는지: PATIENT 는 토큰의 본인 값만
    if user.role == PATIENT:
        if user.patient_id is None or (body.patient_id is not None and body.patient_id != user.patient_id):
            deny(request, user, "patient", body.patient_id)
        patient_id = user.patient_id
    else:
        if body.patient_id is None:
            raise bad_request("patient_id 가 필요합니다")
        patient_id = body.patient_id
    if user.role in DEPARTMENT_SCOPE_ROLES and body.department_id != user.department_id:
        deny(request, user, "department", body.department_id)

    # 이미 같은 요청이 있으면 새로 저장하지 않는다. 다른 사람(범위 밖)의 요청 ID 면 내용을 보여 주지 않고 409.
    existing = find_by_request_id(db, request_id)
    if existing:
        return _duplicate(existing, user, response)

    if db.get(models.Patient, patient_id) is None:
        raise bad_request("환자를 찾을 수 없습니다")
    if db.get(models.Department, body.department_id) is None:
        raise bad_request("진료과를 찾을 수 없습니다")
    if body.doctor_id is not None:
        # FOR UPDATE: 같은 의사 예약을 한 줄로 세워, 동시에 들어온 같은 시간 예약이 둘 다 저장되지 않게 한다.
        doctor = db.scalar(select(models.Doctor).where(models.Doctor.id == body.doctor_id).with_for_update())
        if doctor is None or not doctor.is_active or doctor.department_id != body.department_id:
            raise bad_request("해당 진료과의 의사가 아닙니다")
        if doctor_slot_taken(db, body.doctor_id, body.reserved_at):
            raise conflict("그 시간에는 이 의사의 예약이 이미 있습니다")

    now = utcnow()
    reservation = models.Reservation(
        request_id=request_id,
        patient_id=patient_id,
        department_id=body.department_id,
        doctor_id=body.doctor_id,
        reserved_at=body.reserved_at,
        visit_type=body.visit_type,
        memo=body.memo or None,
        created_by=user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(reservation)
    try:
        db.flush()  # id 를 받아 감사 로그에 넣는다.
        audit.add_audit(db, request, "RESERVATION_CREATE", audit.SUCCESS, user_id=user.id,
                        target_type="reservation", target_id=reservation.id)
        db.commit()  # 예약과 감사 로그가 함께 저장돼야 201을 돌려준다.
    except IntegrityError:
        # 동시에 같은 request_id 가 들어와 UNIQUE 에 걸린 경우: 취소하고 먼저 저장된 예약을 돌려준다.
        db.rollback()
        existing = find_by_request_id(db, request_id)
        if existing is None:
            raise  # UNIQUE 말고 다른 이유의 오류면 그대로 (공통 오류 처리 → 응답에 내부 문구 없음)
        return _duplicate(existing, user, response)

    db.refresh(reservation)
    return reservation


def _duplicate(existing: models.Reservation, user: CurrentUser, response: Response) -> models.Reservation:
    if not in_scope(user, existing):
        raise conflict("이미 사용된 X-Request-ID 입니다")
    response.status_code = status.HTTP_200_OK
    return existing


# ---------- R2 예약 목록 ----------

@api.get("/reservations", response_model=schemas.ReservationPage)
def list_reservations(
    request: Request,
    filters: Annotated[schemas.ReservationFilter, Query()],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """예약 목록. 역할별 범위가 자동으로 걸린다. 날짜는 KST 기준, 예약 시각 순."""
    R = models.Reservation
    conditions = []
    if user.role == PATIENT:
        if user.patient_id is None:
            deny(request, user, "patient", None)
        conditions.append(R.patient_id == user.patient_id)
    elif user.role in DEPARTMENT_SCOPE_ROLES:
        if user.department_id is None or (
            filters.department_id is not None and filters.department_id != user.department_id
        ):
            deny(request, user, "department", filters.department_id)
        conditions.append(R.department_id == user.department_id)
    elif user.role not in ALL_SCOPE_ROLES and user.role not in FRONT_DESK_ROLES:
        deny(request, user, "reservation", None)

    if filters.department_id is not None:
        conditions.append(R.department_id == filters.department_id)
    if filters.doctor_id is not None:
        conditions.append(R.doctor_id == filters.doctor_id)
    if filters.status:
        conditions.append(R.status == filters.status)
    if filters.checkin_status:
        conditions.append(R.checkin_status == filters.checkin_status)
    if filters.date_from:
        conditions.append(R.reserved_at >= kst_day_range_utc(filters.date_from)[0])
    if filters.date_to:
        conditions.append(R.reserved_at < kst_day_range_utc(filters.date_to)[1])

    total = db.scalar(select(func.count()).select_from(R).where(*conditions))
    items = db.scalars(
        select(R).options(selectinload(R.patient)).where(*conditions).order_by(R.reserved_at, R.id)
        .offset((filters.page - 1) * filters.size).limit(filters.size)
    ).all()
    return {"items": [reservation_view(user, r) for r in items], "page": filters.page, "size": filters.size,
            "total": total}


# ---------- R3 예약 상세 ----------

@api.get("/reservations/{reservation_id}", response_model=schemas.ReservationOut)
def get_reservation(
    reservation_id: int, request: Request,
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
):
    """예약 한 건. 없으면 404, 범위 밖이면 403."""
    return reservation_view(user, load_in_scope(db, request, user, reservation_id))


# ---------- R4 취소 ----------

@api.delete("/reservations/{reservation_id}", response_model=schemas.ReservationOut)
def cancel_reservation(
    reservation_id: int, request: Request,
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
):
    """예약 취소. 행은 지우지 않고 status=CANCELED, canceled_at 기록. 이미 취소면 그대로 200.

    진료중(IN_TREATMENT)·진료완료(COMPLETED) 예약은 취소할 수 없다 (409).
    """
    reservation = load_in_scope(db, request, user, reservation_id, for_update=True)
    if reservation.checkin_status in TREATMENT_STARTED:
        raise conflict("진료가 시작된 예약은 취소할 수 없습니다")
    if reservation.status != "CANCELED":
        now = utcnow()
        reservation.status = "CANCELED"
        reservation.canceled_at = now
        reservation.updated_at = now
        audit.add_audit(db, request, "RESERVATION_CANCEL", audit.SUCCESS, user_id=user.id,
                        target_type="reservation", target_id=reservation.id)
        db.commit()
        db.refresh(reservation)
    return reservation_view(user, reservation)


# ---------- R5 접수 ----------

QUEUE_RETRIES = 5  # 대기번호가 겹치거나 교착이 나면 처음부터 다시 (예약 잠금 → 번호 계산 → 저장)
MYSQL_DEADLOCK = 1213


def next_queue_no(db: Session, department_id: int, queue_date) -> int:
    """그 진료과·그 날짜의 다음 대기번호.

    진료과 행을 먼저 잠가 같은 진료과의 접수를 한 줄로 세운다. 번호가 아직 하나도 없는 날은 MAX 의 FOR UPDATE 가
    빈 구간(gap) 잠금이 되어 동시 접수끼리 교착이 나므로, 존재하는 행(진료과)을 잠그는 것이 안전하다.
    그래도 겹치면 UNIQUE(department_id, queue_date, queue_no) 가 막고, 호출한 쪽이 다시 시도한다.
    """
    db.execute(select(models.Department.id).where(models.Department.id == department_id).with_for_update())
    R = models.Reservation
    last_no = db.scalar(
        select(func.max(R.queue_no)).where(R.department_id == department_id, R.queue_date == queue_date)
        .with_for_update()
    )
    return (last_no or 0) + 1


def is_queue_collision(error: Exception) -> bool:
    """대기번호 UNIQUE 위반 또는 MySQL 교착이면 True (다시 시도하면 되는 오류)."""
    if mysql_error_code(error) == MYSQL_DEADLOCK:
        return True
    return isinstance(error, IntegrityError) and "queue" in str(getattr(error, "orig", error)).lower()


@api.post("/reservations/{reservation_id}/checkin", response_model=schemas.ReservationOut)
def checkin_reservation(
    reservation_id: int, request: Request,
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
):
    """현장 접수. 취소된 예약은 409, 이미 접수(진료중·완료 포함)면 그대로 200.

    대기번호는 진료과·날짜(KST)별 1번부터. 그 날짜가 reservations.queue_date 에 저장된다.
    """
    for _ in range(QUEUE_RETRIES):
        try:
            reservation = load_in_scope(db, request, user, reservation_id, for_update=True)
            if reservation.status == "CANCELED":
                raise conflict("취소된 예약은 접수할 수 없습니다")
            if reservation.checkin_status != "NOT_CHECKED_IN":
                return reservation_view(user, reservation)
            queue_date = kst_date_of(reservation.reserved_at)
            now = utcnow()
            reservation.checkin_status = "CHECKED_IN"
            reservation.checked_in_at = now
            reservation.queue_date = queue_date
            reservation.queue_no = next_queue_no(db, reservation.department_id, queue_date)
            reservation.updated_at = now
            audit.add_audit(db, request, "RESERVATION_CHECKIN", audit.SUCCESS, user_id=user.id,
                            target_type="reservation", target_id=reservation.id)
            db.commit()
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            if not is_queue_collision(exc):
                raise
            continue  # 다른 접수와 번호가 겹침: 취소하고 처음부터 다시
        db.refresh(reservation)
        return reservation_view(user, reservation)
    raise ApiError(503, "DB_UNAVAILABLE", "접수가 몰려 처리하지 못했습니다. 잠시 후 다시 시도하세요")


# ---------- R15 진료 시작 · R16 진료 완료 (담당 의사만) ----------

# 새 상태 → (바로 앞 상태, 감사 로그 action)
TREATMENT_STEPS = {
    "IN_TREATMENT": ("CHECKED_IN", "TREATMENT_START"),
    "COMPLETED": ("IN_TREATMENT", "TREATMENT_COMPLETE"),
}
TREATMENT_STARTED = frozenset({"IN_TREATMENT", "COMPLETED"})


def doctor_of(db: Session, user: CurrentUser) -> models.Doctor | None:
    """로그인 계정에 연결된 의사 (doctors.user_id). 연결이 없으면 None."""
    return db.scalar(select(models.Doctor).where(models.Doctor.user_id == user.id))


def advance_treatment(db: Session, request: Request, user: CurrentUser, reservation_id: int, new_status: str):
    """진료 상태를 한 칸 앞으로. 담당 의사가 아니면 403(+DENIED), 순서가 틀리면 409.

    이미 그 상태면 그대로 200 (접수·취소와 같은 방식). 건너뛰기·되돌리기는 409.
    """
    previous, action = TREATMENT_STEPS[new_status]
    reservation = db.scalar(
        select(models.Reservation).where(models.Reservation.id == reservation_id).with_for_update())
    if reservation is None:
        raise not_found("예약을 찾을 수 없습니다")
    doctor = doctor_of(db, user)
    if doctor is None or reservation.doctor_id != doctor.id:
        deny(request, user, "reservation", reservation_id)
    if reservation.status == "CANCELED":
        raise conflict("취소된 예약은 진료할 수 없습니다")
    if reservation.checkin_status == new_status:
        return reservation
    if reservation.checkin_status != previous:
        raise conflict("진료 상태는 접수 → 진료 시작 → 진료 완료 순서로만 바꿀 수 있습니다")
    reservation.checkin_status = new_status
    reservation.updated_at = utcnow()
    audit.add_audit(db, request, action, audit.SUCCESS, user_id=user.id,
                    target_type="reservation", target_id=reservation.id)
    db.commit()
    db.refresh(reservation)
    return reservation


@api.post("/reservations/{reservation_id}/start-treatment", response_model=schemas.ReservationOut)
def start_treatment(
    reservation_id: int, request: Request,
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
):
    """진료 시작: CHECKED_IN → IN_TREATMENT. 담당 의사만."""
    return advance_treatment(db, request, user, reservation_id, "IN_TREATMENT")


@api.post("/reservations/{reservation_id}/complete-treatment", response_model=schemas.ReservationOut)
def complete_treatment(
    reservation_id: int, request: Request,
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
):
    """진료 완료: IN_TREATMENT → COMPLETED. 담당 의사만."""
    return advance_treatment(db, request, user, reservation_id, "COMPLETED")


# ---------- R6 진료과 / R7 의사 ----------

def department_list(db: Session, user: CurrentUser) -> list[schemas.DepartmentOut]:
    rows = db.scalars(select(models.Department).order_by(models.Department.id)).all()
    out = []
    for d in rows:
        visible = can_see_department_phone(user.role, d.code)
        out.append(schemas.DepartmentOut(id=d.id, code=d.code, name=d.name, phone=shown(d.phone, visible),
                                         phone_is_masked=bool(d.phone) and not visible))
    return out


@api.get("/departments", response_model=list[schemas.DepartmentOut])
def list_departments(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """진료과 목록 (예약 화면 선택용). 대표 번호는 역할에 따라 가린다 (shared/phone.py)."""
    return department_list(db, user)


@api.get("/doctors", response_model=list[schemas.DoctorOut])
def list_doctors(filters: Annotated[schemas.DoctorFilter, Query()], db: Session = Depends(get_db)):
    """의사 목록 (진료 중인 의사만). department_id 로 거를 수 있다."""
    stmt = select(models.Doctor).where(models.Doctor.is_active.is_(True))
    if filters.department_id is not None:
        stmt = stmt.where(models.Doctor.department_id == filters.department_id)
    return db.scalars(stmt.order_by(models.Doctor.id)).all()


# ---------- R8 오늘 대기열 ----------

def queue_items(db: Session, day, department_id: int | None = None,
                doctor_id: int | None = None) -> list[schemas.QueueItem]:
    """그날(KST) 접수된(취소 아님) 예약을 진료과·대기번호 순으로. 진료대기·진료중·진료완료가 모두 나온다."""
    start, end = kst_day_range_utc(day)
    R, P, D = models.Reservation, models.Patient, models.Doctor
    stmt = (
        select(R, P.name, P.chart_no, D.name)
        .join(P, P.id == R.patient_id)
        .outerjoin(D, D.id == R.doctor_id)
        .where(R.reserved_at >= start, R.reserved_at < end,
               R.status == "BOOKED", R.checkin_status != "NOT_CHECKED_IN")
        .order_by(R.department_id, R.queue_no, R.checked_in_at, R.id)
    )
    if department_id is not None:
        stmt = stmt.where(R.department_id == department_id)
    if doctor_id is not None:
        stmt = stmt.where(R.doctor_id == doctor_id)
    return [
        schemas.QueueItem(
            reservation_id=r.id, queue_no=r.queue_no or 0, department_id=r.department_id,
            patient_id=r.patient_id, patient_name=patient_name, chart_no=chart_no,
            doctor_id=r.doctor_id, doctor_name=doctor_name, checkin_status=r.checkin_status,
            reserved_at=r.reserved_at, checked_in_at=r.checked_in_at,
        )
        for r, patient_name, chart_no, doctor_name in db.execute(stmt).all()
    ]


@api.get("/queue", response_model=schemas.QueueOut)
def get_queue(
    request: Request,
    filters: Annotated[schemas.QueueFilter, Query()],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """접수된 예약을 대기번호 순으로 (각 항목의 checkin_status 로 진료대기·진료중·진료완료 구분).

    NURSE·DEPT_HEAD 는 자기 부서만, DIRECTOR·TESTER 는 department_id 로 고른다(없으면 전체).
    """
    if user.role in DEPARTMENT_SCOPE_ROLES:
        if user.department_id is None or (
            filters.department_id is not None and filters.department_id != user.department_id
        ):
            deny(request, user, "department", filters.department_id)
        department_id = user.department_id
    elif user.role in ALL_SCOPE_ROLES:
        department_id = filters.department_id
    else:
        deny(request, user, "queue", None)

    day = filters.date or today_kst()
    return schemas.QueueOut(date=day, department_id=department_id, items=queue_items(db, day, department_id))


@api.get("/doctor/queue", response_model=schemas.QueueOut)
def get_doctor_queue(
    request: Request,
    filters: Annotated[schemas.QueueFilter, Query()],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """의사 화면용 대기열: 로그인한 의사 본인이 담당인 접수된 예약만. department_id 는 받지 않는다."""
    doctor = doctor_of(db, user)
    if doctor is None or filters.department_id is not None:
        deny(request, user, "doctor", filters.department_id)
    day = filters.date or today_kst()
    return schemas.QueueOut(date=day, department_id=doctor.department_id,
                            items=queue_items(db, day, doctor_id=doctor.id))


# ---------- R9 환자 검색 · R10 환자 상세 · 환자 등록 ----------

def nurse_phone_patient_ids(db: Session, user: CurrentUser, patient_ids: list[int]) -> set[int]:
    """간호사 자기 부서에 (취소 아닌) 예약이 있는 환자 id. 간호사가 아니거나 부서가 없으면 빈 집합."""
    if user.role != NURSE or user.department_id is None or not patient_ids:
        return set()
    R = models.Reservation
    return set(db.scalars(
        select(R.patient_id).where(R.patient_id.in_(patient_ids), R.department_id == user.department_id,
                                   R.status == "BOOKED").distinct()
    ).all())


@api.get("/patients", response_model=schemas.PatientPage)
def search_patients(
    filters: Annotated[schemas.PatientSearch, Query()],
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """이름 일부 또는 차트번호 앞부분으로 찾는다. 전화번호는 역할별로 공개하거나 가린다 (shared/phone.py)."""
    P = models.Patient
    cond = P.name.contains(filters.q, autoescape=True) | P.chart_no.startswith(filters.q, autoescape=True)
    total = db.scalar(select(func.count()).select_from(P).where(cond))
    rows = db.scalars(
        select(P).where(cond).order_by(P.name, P.id).offset((filters.page - 1) * filters.size).limit(filters.size)
    ).all()
    open_ids = nurse_phone_patient_ids(db, user, [p.id for p in rows])
    items = []
    for p in rows:
        visible = can_see_patient_phone(user.role, user.patient_id, p.id, open_ids)
        items.append(schemas.PatientSummary(id=p.id, chart_no=p.chart_no, name=p.name, birth_date=p.birth_date,
                                            phone=shown(p.phone, visible),
                                            phone_is_masked=bool(p.phone) and not visible))
    return {"items": items, "page": filters.page, "size": filters.size, "total": total}


RECENT_RESERVATIONS = 10


@api.get("/patients/{patient_id}", response_model=schemas.PatientDetail)
def get_patient(
    patient_id: int, request: Request,
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
):
    """환자 상세 + 최근 예약. NURSE 는 자기 부서 예약만 보인다. PATIENT 는 본인만. 개인정보 조회라 감사 로그를 남긴다.

    MANAGER 는 전체 부서의 최근 예약이 보이지만 예약 메모는 비어 있다. 진단·진료 메모는 어느 역할에도 포함하지 않는다.
    """
    if user.role == PATIENT and patient_id != user.patient_id:
        deny(request, user, "patient", patient_id)
    patient = db.get(models.Patient, patient_id)
    if patient is None:
        raise not_found("환자를 찾을 수 없습니다")
    R = models.Reservation
    stmt = select(R).where(R.patient_id == patient_id)
    if user.role in DEPARTMENT_SCOPE_ROLES:
        stmt = stmt.where(R.department_id == user.department_id)
    recent = db.scalars(stmt.order_by(R.reserved_at.desc(), R.id.desc()).limit(RECENT_RESERVATIONS)).all()
    audit.write_audit(request, "PATIENT_VIEW", audit.SUCCESS, user_id=user.id,
                      target_type="patient", target_id=patient_id)
    return schemas.PatientDetail(**_patient_out(db, user, patient).model_dump(),
                                 recent_reservations=[reservation_view(user, r) for r in recent])


def _patient_out(db: Session, user: CurrentUser, patient: models.Patient) -> schemas.PatientOut:
    visible = can_see_patient_phone(user.role, user.patient_id, patient.id,
                                    nurse_phone_patient_ids(db, user, [patient.id]))
    return schemas.PatientOut(id=patient.id, chart_no=patient.chart_no, name=patient.name,
                              birth_date=patient.birth_date, phone=shown(patient.phone, visible),
                              phone_is_masked=bool(patient.phone) and not visible, created_at=patient.created_at)


@api.post("/patients", response_model=schemas.PatientOut, status_code=status.HTTP_201_CREATED)
def create_patient(
    body: schemas.PatientCreate, request: Request,
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
):
    """환자 등록. 차트번호가 이미 있으면 409. 등록과 감사 로그는 같은 트랜잭션."""
    now = utcnow()
    patient = models.Patient(chart_no=body.chart_no, name=body.name, birth_date=body.birth_date,
                             phone=body.phone, created_at=now, updated_at=now)
    db.add(patient)
    try:
        db.flush()
        audit.add_audit(db, request, "PATIENT_CREATE", audit.SUCCESS, user_id=user.id,
                        target_type="patient", target_id=patient.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise conflict("이미 있는 차트번호입니다") from None
    db.refresh(patient)
    return _patient_out(db, user, patient)


# ---------- R18 직원 연락처 ----------

def staff_contacts(db: Session, user: CurrentUser) -> list[schemas.StaffOut]:
    """역할별로 볼 수 있는 직원 (활성 직원, 환자 계정 제외). 범위는 쿼리에서 거르고, 번호는 shared/phone.py 로 공개.

    MANAGER 전체 직원 / NURSE·DEPT_HEAD 자기 부서 직원 / DOCTOR 자기 부서 부서장만 /
    DIRECTOR 자기 부서 직원(부서가 있으면) + 모든 부서 부서장 / 그 밖의 역할은 직원 목록 없음.
    """
    stmt = select(User).where(User.role != PATIENT, User.is_active.is_(True))
    if user.role in (NURSE, DOCTOR, DEPT_HEAD):
        if user.department_id is None:
            return []
        stmt = stmt.where(User.department_id == user.department_id)
        if user.role == DOCTOR:
            stmt = stmt.where(User.role == DEPT_HEAD)
    elif user.role == DIRECTOR:
        cond = User.role == DEPT_HEAD
        if user.department_id is not None:
            cond = cond | (User.department_id == user.department_id)
        stmt = stmt.where(cond)
    elif user.role != MANAGER:
        return []
    out = []
    for u in db.scalars(stmt.order_by(User.department_id, User.id)).all():
        visible = can_see_staff_phone(user.role, user.department_id, u.department_id, u.role)
        out.append(schemas.StaffOut(id=u.id, name=u.name, role=u.role, department_id=u.department_id,
                                    work_phone=shown(u.work_phone, visible),
                                    work_phone_is_masked=bool(u.work_phone) and not visible))
    return out


@api.get("/staff", response_model=list[schemas.StaffOut])
def list_staff(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """직원 업무용 연락처. 원무과장은 전체 직원, 간호사는 자기 부서 직원만. 번호는 shared/phone.py 규칙으로 공개."""
    return staff_contacts(db, user)


# ---------- R19 연락처 화면 ----------

@api.get("/contacts", response_model=schemas.ContactsOut)
def get_contacts(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """연락처 화면 한 번에: 병원 대표 번호, 진료과 대표 번호, 직원 업무용 연락처 (모두 역할별 공개 범위 적용)."""
    return schemas.ContactsOut(hospital_phone=config.HOSPITAL_PHONE or None,
                               departments=department_list(db, user), staff=staff_contacts(db, user))


# ---------- 예약 가능 시간 (추가 API) ----------

SLOT_DAY_START = timedelta(hours=9)      # KST 09:00 첫 칸
SLOT_DAY_END = timedelta(hours=17, minutes=30)  # KST 17:30 진료 끝 (마지막 칸 17:00~17:30)


@api.get("/slots", response_model=schemas.SlotsOut)
def list_slots(filters: Annotated[schemas.SlotFilter, Query()], db: Session = Depends(get_db)):
    """그 의사의 그날(KST) 30분 단위 예약 가능 시간. 09:00~17:30 중 BOOKED 예약과 겹치는 칸과 지난 칸은 뺀다.

    오늘이면 지금(KST) 이전에 시작하는 칸을 빼고, 지난 날짜면 빈 목록이다 (R1 도 과거 시각은 422).
    """
    doctor = db.get(models.Doctor, filters.doctor_id)
    if doctor is None or not doctor.is_active:
        raise not_found("의사를 찾을 수 없습니다")
    day_start, _ = kst_day_range_utc(filters.date)
    first, last_end = day_start + SLOT_DAY_START, day_start + SLOT_DAY_END
    R = models.Reservation
    booked = db.scalars(
        select(R.reserved_at).where(
            R.doctor_id == filters.doctor_id, R.status == "BOOKED",
            R.reserved_at > first - SLOT, R.reserved_at < last_end,
        )
    ).all()
    now = timeutil.utcnow_naive()
    available = []
    start = first
    while start + SLOT <= last_end:
        if start >= now and all(abs(b - start) >= SLOT for b in booked):  # doctor_slot_taken 과 같은 겹침 기준
            available.append(start)
        start += SLOT
    return schemas.SlotsOut(doctor_id=doctor.id, date=filters.date, slot_minutes=int(SLOT.total_seconds() // 60),
                            available=available)


# ---------- R11 내 진료기록 · R12 내 영수증 (샘플 데이터 조회만) ----------

@api.get("/me/records", response_model=list[schemas.MedicalRecordOut])
def my_records(request: Request, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """본인 진료기록 (최신순). 권한(PATIENT·본인) 확인을 통과한 뒤에만 복호화한다.

    키가 없거나 하나라도 복호화에 실패하면 평문을 하나도 돌려주지 않고 503 (shared/errors.py).
    조회는 성공·실패 모두 감사 로그 RECORD_VIEW 로 남긴다.
    """
    if user.patient_id is None:
        deny(request, user, "patient", None)
    MR, D = models.MedicalRecord, models.Doctor
    rows = db.execute(
        select(MR, D.name).outerjoin(D, D.id == MR.doctor_id)
        .where(MR.patient_id == user.patient_id).order_by(MR.created_at.desc(), MR.id.desc())
    ).all()
    try:
        items = [
            schemas.MedicalRecordOut(
                id=r.id, reservation_id=r.reservation_id, doctor_id=r.doctor_id, doctor_name=doctor_name,
                diagnosis=crypto.decrypt_field(r.diagnosis_enc, r.diagnosis_nonce, field="diagnosis",
                                               patient_id=r.patient_id, key_version=r.key_version),
                note=crypto.decrypt_field(r.note_enc, r.note_nonce, field="note",
                                          patient_id=r.patient_id, key_version=r.key_version),
                created_at=r.created_at,
            )
            for r, doctor_name in rows
        ]
    except crypto.CryptoError:
        audit.write_audit(request, "RECORD_VIEW", audit.FAIL, user_id=user.id,
                          target_type="patient", target_id=user.patient_id)
        raise
    audit.write_audit(request, "RECORD_VIEW", audit.SUCCESS, user_id=user.id,
                      target_type="patient", target_id=user.patient_id)
    return items


@api.get("/me/receipts", response_model=list[schemas.ReceiptOut])
def my_receipts(request: Request, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """본인 영수증 (최신순)."""
    if user.patient_id is None:
        deny(request, user, "patient", None)
    RC = models.Receipt
    return db.scalars(
        select(RC).where(RC.patient_id == user.patient_id).order_by(RC.created_at.desc(), RC.id.desc())
    ).all()


app.include_router(api)


@app.get("/", include_in_schema=False)
def index():
    """1단계 기본 웹 화면 (HTML 한 장). 로그인 화면이 없어 지금은 예약 API 가 401 이다. 화면은 React 로 따로 만든다."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")
