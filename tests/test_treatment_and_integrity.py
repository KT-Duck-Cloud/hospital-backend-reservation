"""진료 진행 상태(접수 → 진료중 → 진료완료), 대기번호 중복 방지, 예약-진료기록·영수증 환자 일치

SQLite 에서 도는 테스트다. 실제 MySQL 에서의 동시 접수 테스트는 test_mysql.py (TEST_DB_URL 이 있을 때만).
"""
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from shared.models import AuditLog

URL = "/api/v1/reservations"
DAY = "2026-10-22"  # KST 날짜


def book(client, world, request_id, at="2026-10-22T01:00:00Z", doctor="dr_im", dept="IM", patient="C-1"):
    r = client.post(URL, json={"department_id": world.ids[dept], "doctor_id": world.ids[doctor], "reserved_at": at,
                               "visit_type": "RETURN", "patient_id": world.ids[patient]},
                    headers=world.h("director", request_id))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def checked_in(client, world, request_id, **kwargs):
    rid = book(client, world, request_id, **kwargs)
    r = client.post(f"{URL}/{rid}/checkin", headers=world.h("nurse_im" if kwargs.get("dept", "IM") == "IM"
                                                           else "nurse_os"))
    assert r.status_code == 200, r.text
    return rid


def start(client, world, rid, login_id="doctor"):
    return client.post(f"{URL}/{rid}/start-treatment", headers=world.h(login_id))


def complete(client, world, rid, login_id="doctor"):
    return client.post(f"{URL}/{rid}/complete-treatment", headers=world.h(login_id))


def audits(action):
    from shared.db import SessionLocal

    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


# ---------- 1. 진료 진행 상태 ----------

def test_treatment_goes_forward_only(client, world):
    rid = checked_in(client, world, "t-1")
    assert client.get(f"{URL}/{rid}", headers=world.h("nurse_im")).json()["checkin_status"] == "CHECKED_IN"

    r = start(client, world, rid)
    assert r.status_code == 200 and r.json()["checkin_status"] == "IN_TREATMENT"
    r = complete(client, world, rid)
    assert r.status_code == 200 and r.json()["checkin_status"] == "COMPLETED"
    # 같은 단계를 다시 누르면 그대로 200 (버튼을 두 번 눌러도 안전)
    assert complete(client, world, rid).json()["checkin_status"] == "COMPLETED"
    # 되돌리기는 409
    assert start(client, world, rid).status_code == 409
    # 접수를 다시 해도 상태는 뒤로 가지 않는다
    r = client.post(f"{URL}/{rid}/checkin", headers=world.h("nurse_im"))
    assert r.status_code == 200 and r.json()["checkin_status"] == "COMPLETED"


def test_skipping_a_step_is_409(client, world):
    not_checked_in = book(client, world, "t-2")
    assert start(client, world, not_checked_in).status_code == 409      # 접수 전 → 진료중 (건너뜀)
    assert complete(client, world, not_checked_in).status_code == 409   # 접수 전 → 진료완료 (건너뜀)

    waiting = checked_in(client, world, "t-3", at="2026-10-22T02:00:00Z")
    assert complete(client, world, waiting).status_code == 409          # 진료대기 → 진료완료 (건너뜀)
    got = client.get(f"{URL}/{waiting}", headers=world.h("nurse_im")).json()
    assert got["checkin_status"] == "CHECKED_IN"                         # 거절되면 상태는 그대로


def test_canceled_reservation_cannot_be_treated(client, world):
    rid = checked_in(client, world, "t-4")
    assert client.delete(f"{URL}/{rid}", headers=world.h("nurse_im")).status_code == 200
    assert start(client, world, rid).status_code == 409


def test_only_the_assigned_doctor_can_treat(client, world):
    rid = checked_in(client, world, "t-5")
    before = len(audits("ACCESS_DENIED"))

    # 다른 과 의사, 의사가 아닌 역할은 모두 403 (진료 상태는 그대로)
    for login_id in ("doctor_os", "nurse_im", "head_im", "director", "tester", "manager", "admin", "dev", "patient01"):
        assert start(client, world, rid, login_id).status_code == 403, login_id
        assert complete(client, world, rid, login_id).status_code == 403, login_id
    assert client.get(f"{URL}/{rid}", headers=world.h("nurse_im")).json()["checkin_status"] == "CHECKED_IN"
    assert len(audits("ACCESS_DENIED")) > before                          # 거부는 감사 로그 DENIED 로 남는다
    denied = audits("ACCESS_DENIED")[-1]
    assert denied.result == "DENIED"

    # 담당 의사는 된다. 없는 예약은 404, 로그인 없으면 401
    assert start(client, world, rid).status_code == 200
    assert start(client, world, 99999).status_code == 404
    assert client.post(f"{URL}/{rid}/start-treatment").status_code == 401


def test_other_doctor_denied_is_audited_with_target(client, world):
    rid = checked_in(client, world, "t-6")
    assert start(client, world, rid, "doctor_os").status_code == 403
    last = audits("ACCESS_DENIED")[-1]
    assert (last.user_id, last.target_type, last.target_id, last.result) == (
        world.ids["doctor_os"], "reservation", str(rid), "DENIED")


def test_treatment_changes_are_audited(client, world):
    rid = checked_in(client, world, "t-7")
    start(client, world, rid)
    complete(client, world, rid)
    started, completed = audits("TREATMENT_START"), audits("TREATMENT_COMPLETE")
    assert [(a.user_id, a.target_id, a.result) for a in started] == [(world.ids["doctor"], str(rid), "SUCCESS")]
    assert [(a.user_id, a.target_id, a.result) for a in completed] == [(world.ids["doctor"], str(rid), "SUCCESS")]


@pytest.mark.parametrize("steps", [1, 2])
def test_in_treatment_or_completed_cannot_be_canceled(client, world, steps):
    rid = checked_in(client, world, f"t-8-{steps}")
    start(client, world, rid)
    if steps == 2:
        complete(client, world, rid)
    for login_id in ("nurse_im", "director"):
        assert client.delete(f"{URL}/{rid}", headers=world.h(login_id)).status_code == 409
    got = client.get(f"{URL}/{rid}", headers=world.h("nurse_im")).json()
    assert got["status"] == "BOOKED" and got["canceled_at"] is None


def test_patient_cannot_cancel_after_treatment_started(client, world):
    rid = checked_in(client, world, "t-9")
    start(client, world, rid)
    assert client.delete(f"{URL}/{rid}", headers=world.h("patient01")).status_code == 409


def test_checked_in_can_still_be_canceled(client, world):
    rid = checked_in(client, world, "t-10")
    assert client.delete(f"{URL}/{rid}", headers=world.h("nurse_im")).status_code == 200


def test_queue_shows_progress_status(client, world):
    a = checked_in(client, world, "q-1", at="2026-10-22T01:00:00Z")
    b = checked_in(client, world, "q-2", at="2026-10-22T02:00:00Z")
    c = checked_in(client, world, "q-3", at="2026-10-22T03:00:00Z")
    book(client, world, "q-4", at="2026-10-22T04:00:00Z")  # 접수 전: 대기열에 없다
    start(client, world, a)
    start(client, world, b)
    complete(client, world, b)

    q = client.get("/api/v1/queue", params={"date": DAY}, headers=world.h("nurse_im")).json()
    assert [(i["reservation_id"], i["checkin_status"]) for i in q["items"]] == [
        (a, "IN_TREATMENT"), (b, "COMPLETED"), (c, "CHECKED_IN")]


def test_doctor_queue_is_only_own_patients(client, world):
    mine = checked_in(client, world, "dq-1")
    checked_in(client, world, "dq-2", doctor="dr_os", dept="OS")
    start(client, world, mine)

    q = client.get("/api/v1/doctor/queue", params={"date": DAY}, headers=world.h("doctor")).json()
    assert [(i["reservation_id"], i["checkin_status"]) for i in q["items"]] == [(mine, "IN_TREATMENT")]
    assert q["department_id"] == world.ids["IM"]
    other = client.get("/api/v1/doctor/queue", params={"date": DAY}, headers=world.h("doctor_os")).json()
    assert len(other["items"]) == 1 and other["items"][0]["reservation_id"] != mine
    for login_id in ("nurse_im", "director", "patient01", "manager"):
        assert client.get("/api/v1/doctor/queue", headers=world.h(login_id)).status_code == 403
    r = client.get("/api/v1/doctor/queue", params={"department_id": world.ids["OS"]}, headers=world.h("doctor"))
    assert r.status_code == 403


# ---------- 2. 대기번호 중복 방지 ----------

def reservation_row(rid):
    from app import models
    from shared.db import SessionLocal

    with SessionLocal() as db:
        r = db.get(models.Reservation, rid)
        return r.queue_no, r.queue_date


def test_checkin_stores_queue_date_and_numbers_per_department_day(client, world):
    a = checked_in(client, world, "n-1", at="2026-10-22T01:00:00Z")
    b = checked_in(client, world, "n-2", at="2026-10-22T02:00:00Z")
    c = checked_in(client, world, "n-3", at="2026-10-22T01:00:00Z", doctor="dr_os", dept="OS")   # 다른 진료과
    d = checked_in(client, world, "n-4", at="2026-10-23T01:00:00Z")                              # 다른 날
    # KST 날짜: UTC 2026-10-22T16:00 = KST 10-23 01:00
    e = checked_in(client, world, "n-5", at="2026-10-22T16:00:00Z")
    from datetime import date

    assert reservation_row(a) == (1, date(2026, 10, 22))
    assert reservation_row(b) == (2, date(2026, 10, 22))
    assert reservation_row(c) == (1, date(2026, 10, 22))
    assert reservation_row(d) == (1, date(2026, 10, 23))
    assert reservation_row(e) == (2, date(2026, 10, 23))
    # 다시 접수해도 번호는 그대로
    client.post(f"{URL}/{a}/checkin", headers=world.h("nurse_im"))
    assert reservation_row(a) == (1, date(2026, 10, 22))


def test_db_rejects_duplicate_queue_number(client, world):
    from shared.db import engine

    a = book(client, world, "u-1", at="2026-10-22T01:00:00Z")
    b = book(client, world, "u-2", at="2026-10-22T02:00:00Z")
    sql = text("UPDATE reservations SET queue_no = 1, queue_date = '2026-10-22' WHERE id = :i")
    with engine.begin() as conn:
        conn.execute(sql, {"i": a})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(sql, {"i": b})
    # 접수 전(둘 다 NULL)인 예약은 몇 건이든 겹치지 않는다
    book(client, world, "u-3", at="2026-10-22T03:00:00Z")
    book(client, world, "u-4", at="2026-10-22T04:00:00Z")


def test_checkin_retries_when_number_collides(client, world, monkeypatch):
    """동시 접수가 같은 번호를 읽은 상황을 흉내 낸다: 첫 시도의 번호가 이미 쓰였어도 UNIQUE 가 막고 다시 계산한다."""
    from app import main

    a = checked_in(client, world, "r-1", at="2026-10-22T01:00:00Z")
    b = book(client, world, "r-2", at="2026-10-22T02:00:00Z")
    real = main.next_queue_no
    calls = []

    def stale_then_real(db, department_id, queue_date):
        calls.append(1)
        return 1 if len(calls) == 1 else real(db, department_id, queue_date)  # 첫 번째는 낡은 값 1 (a 가 이미 사용)

    monkeypatch.setattr(main, "next_queue_no", stale_then_real)
    r = client.post(f"{URL}/{b}/checkin", headers=world.h("nurse_im"))
    assert r.status_code == 200, r.text
    assert len(calls) == 2 and r.json()["queue_no"] == 2
    assert reservation_row(a)[0] == 1 and reservation_row(b)[0] == 2
    # 실패한 시도의 감사 로그가 남지 않고, 성공한 접수만 한 번 남는다
    assert [x.target_id for x in audits("RESERVATION_CHECKIN")].count(str(b)) == 1


def test_checkin_gives_up_with_503_when_it_keeps_colliding(client, world, monkeypatch):
    from app import main

    checked_in(client, world, "g-1", at="2026-10-22T01:00:00Z")
    b = book(client, world, "g-2", at="2026-10-22T02:00:00Z")
    monkeypatch.setattr(main, "next_queue_no", lambda db, department_id, queue_date: 1)  # 항상 겹침
    r = client.post(f"{URL}/{b}/checkin", headers=world.h("nurse_im"))
    assert r.status_code == 503
    assert reservation_row(b) == (None, None)
    got = client.get(f"{URL}/{b}", headers=world.h("nurse_im")).json()
    assert got["checkin_status"] == "NOT_CHECKED_IN"


# ---------- 3. 예약-진료기록·영수증 환자 일치 ----------

RECORD_SQL = text(
    "INSERT INTO medical_records (patient_id, reservation_id, doctor_id, diagnosis_enc, note_enc, diagnosis_nonce, "
    "note_nonce, key_version, created_at, updated_at) "
    "VALUES (:p, :r, :d, x'00', x'00', x'000000000000000000000000', x'000000000000000000000000', 1, "
    "'2026-10-14 00:00:00', '2026-10-14 00:00:00')")
RECEIPT_SQL = text(
    "INSERT INTO receipts (patient_id, reservation_id, amount, pay_status, created_at, updated_at) "
    "VALUES (:p, :r, 1000, 'UNPAID', '2026-10-14 00:00:00', '2026-10-14 00:00:00')")


@pytest.fixture()
def fk_conn():
    """외래키 검사를 켠 연결 (SQLite 는 기본이 꺼져 있다. MySQL 은 항상 켜져 있다)."""
    from shared.db import engine

    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.rollback()
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")


def test_medical_record_must_match_reservation_patient(client, world, fk_conn):
    rid = book(client, world, "m-1")                      # 환자 C-1 의 예약
    c1, c2, doctor = world.ids["C-1"], world.ids["C-2"], world.ids["dr_im"]

    with pytest.raises(IntegrityError):                   # 다른 환자(C-2)의 기록은 거부
        fk_conn.execute(RECORD_SQL, {"p": c2, "r": rid, "d": doctor})
    fk_conn.rollback()

    fk_conn.execute(RECORD_SQL, {"p": c1, "r": rid, "d": doctor})   # 같은 환자는 저장된다
    fk_conn.commit()
    with pytest.raises(IntegrityError):                   # 예약 1건에 기록은 1건
        fk_conn.execute(RECORD_SQL, {"p": c1, "r": rid, "d": doctor})
    fk_conn.rollback()
    # 예약 없이(reservation_id NULL) 저장하는 기록은 환자만 맞으면 된다
    fk_conn.execute(text(RECORD_SQL.text.replace(":r", "NULL")), {"p": c2, "d": doctor})
    fk_conn.commit()


def test_receipt_must_match_reservation_patient(client, world, fk_conn):
    rid = book(client, world, "m-2")
    c1, c2 = world.ids["C-1"], world.ids["C-2"]

    with pytest.raises(IntegrityError):
        fk_conn.execute(RECEIPT_SQL, {"p": c2, "r": rid})
    fk_conn.rollback()

    fk_conn.execute(RECEIPT_SQL, {"p": c1, "r": rid})
    fk_conn.commit()
    with pytest.raises(IntegrityError):                   # 예약 1건에 영수증은 1건
        fk_conn.execute(RECEIPT_SQL, {"p": c1, "r": rid})
    fk_conn.rollback()


def test_reservation_patient_cannot_change_under_a_record(client, world, fk_conn):
    """기록이 붙은 예약의 환자를 바꾸면 기록과 어긋나므로 거부된다."""
    rid = book(client, world, "m-3")
    fk_conn.execute(RECORD_SQL, {"p": world.ids["C-1"], "r": rid, "d": world.ids["dr_im"]})
    fk_conn.commit()
    with pytest.raises(IntegrityError):
        fk_conn.execute(text("UPDATE reservations SET patient_id = :p WHERE id = :r"),
                        {"p": world.ids["C-2"], "r": rid})
    fk_conn.rollback()
