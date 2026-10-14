"""실제 MySQL 에서만 확인할 수 있는 것 (TEST_MYSQL_URL 이 있을 때만 돈다. conftest.py 참고).

- 같은 X-Request-ID 동시 20건 → 1건만 저장 (UNIQUE + 재조회)
- DB 를 super_read_only 로 바꾸면 예약 생성이 503 WRITE_DISABLED (TEST_MYSQL_ADMIN_URL 필요)
schema.sql·seed.sql 이 들어간 버리는 DB 에서만 돌린다. 테스트용 계정과 예약이 DB 에 남는다.
"""
import os
import secrets
import threading
from collections import Counter

import pytest
from sqlalchemy import create_engine, func, select, text

from conftest import make_token

pytestmark = pytest.mark.mysql

URL = "/api/v1/reservations"


@pytest.fixture()
def director_headers():
    from app import models
    from shared.db import SessionLocal
    from shared.models import User

    with SessionLocal() as db:
        user = User(login_id=f"zz_test_{secrets.token_hex(4)}", password_hash="!", name="테스트원장", role="DIRECTOR")
        db.add(user)
        db.commit()
        dept = db.scalar(select(models.Department).where(models.Department.code == "IM"))
        patient = db.scalar(select(models.Patient).order_by(models.Patient.id).limit(1))
        token = make_token(db, user)
        return {"Authorization": f"Bearer {token}"}, {"department_id": dept.id, "patient_id": patient.id}


def payload(ids):
    return {**ids, "reserved_at": "2026-10-20T01:00:00Z", "visit_type": "RETURN"}


def test_same_request_id_20_concurrent_saves_once(client, director_headers):
    from app import models
    from shared.db import SessionLocal

    headers, ids = director_headers
    request_id = f"concurrent-{secrets.token_hex(8)}"
    barrier = threading.Barrier(20)
    results = []

    def send():
        barrier.wait()  # 20개가 동시에 출발
        r = client.post(URL, json=payload(ids), headers={**headers, "X-Request-ID": request_id})
        results.append((r.status_code, r.json().get("id")))

    threads = [threading.Thread(target=send) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = Counter(code for code, _ in results)
    assert statuses == {201: 1, 200: 19}, statuses
    assert len({rid for _, rid in results}) == 1  # 모두 같은 예약
    with SessionLocal() as db:
        saved = db.scalar(select(func.count()).select_from(models.Reservation)
                          .where(models.Reservation.request_id == request_id))
    assert saved == 1


@pytest.mark.skipif(not os.environ.get("TEST_MYSQL_ADMIN_URL"), reason="TEST_MYSQL_ADMIN_URL 이 없어 건너뜀")
def test_create_is_503_when_db_is_read_only(client, director_headers):
    from shared.db import mysql_ssl_context

    headers, ids = director_headers
    admin = create_engine(os.environ["TEST_MYSQL_ADMIN_URL"], connect_args={"ssl": mysql_ssl_context()},
                          hide_parameters=True)
    with admin.begin() as conn:
        conn.execute(text("SET GLOBAL super_read_only = ON"))
    try:
        r = client.post(URL, json=payload(ids), headers={**headers, "X-Request-ID": f"ro-{secrets.token_hex(8)}"})
        assert r.status_code == 503 and r.json()["code"] == "WRITE_DISABLED", r.text
        assert client.get(URL, headers=headers).status_code == 200  # 조회는 된다
    finally:
        with admin.begin() as conn:
            conn.execute(text("SET GLOBAL super_read_only = OFF"))
            conn.execute(text("SET GLOBAL read_only = OFF"))
        admin.dispose()


def test_checkin_cancel_and_list_on_mysql(client, director_headers):
    headers, ids = director_headers
    tag = secrets.token_hex(4)
    # 아무도 안 쓰는 먼 날짜라 대기번호가 1부터 시작한다 (KST 2030-01-02 10:00, 11:00)
    first = client.post(URL, json={**payload(ids), "reserved_at": "2030-01-02T01:00:00Z"},
                        headers={**headers, "X-Request-ID": f"q1-{tag}"}).json()
    second = client.post(URL, json={**payload(ids), "reserved_at": "2030-01-02T02:00:00Z"},
                         headers={**headers, "X-Request-ID": f"q2-{tag}"}).json()
    a = client.post(f"{URL}/{second['id']}/checkin", headers=headers).json()
    b = client.post(f"{URL}/{first['id']}/checkin", headers=headers).json()
    assert (a["queue_no"], b["queue_no"]) == (1, 2)
    assert a["checked_in_at"].endswith("Z")

    canceled = client.delete(f"{URL}/{first['id']}", headers=headers).json()
    assert canceled["status"] == "CANCELED" and canceled["canceled_at"]

    page = client.get(URL, params={"date_from": "2030-01-02", "date_to": "2030-01-02",
                                   "department_id": ids["department_id"]}, headers=headers).json()
    assert {i["id"] for i in page["items"]} >= {first["id"], second["id"]}


def test_same_doctor_same_time_10_concurrent_only_one_booked(client, director_headers):
    """X-Request-ID 가 다 다른 동시 예약 10건이 같은 의사·같은 시간이면 1건만 201, 나머지 409."""
    from app import models
    from shared.db import SessionLocal

    headers, ids = director_headers
    with SessionLocal() as db:
        doctor_id = db.scalar(select(models.Doctor.id).where(models.Doctor.department_id == ids["department_id"],
                                                             models.Doctor.is_active.is_(True)).limit(1))
    # 실행마다 다른 먼 날짜 (KST 10:00)
    at = f"2031-{secrets.randbelow(12) + 1:02d}-{secrets.randbelow(28) + 1:02d}T01:00:00Z"
    tag = secrets.token_hex(4)
    barrier = threading.Barrier(10)
    codes = []

    def send(i):
        barrier.wait()
        r = client.post(URL, json={**payload(ids), "doctor_id": doctor_id, "reserved_at": at},
                        headers={**headers, "X-Request-ID": f"slot-{tag}-{i}"})
        codes.append(r.status_code)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Counter(codes) == {201: 1, 409: 9}, Counter(codes)
    with SessionLocal() as db:
        booked = db.scalar(select(func.count()).select_from(models.Reservation).where(
            models.Reservation.doctor_id == doctor_id, models.Reservation.status == "BOOKED",
            models.Reservation.request_id.like(f"slot-{tag}-%")))
    assert booked == 1
