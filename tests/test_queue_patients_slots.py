"""7단계: 의사 시간 겹침 409, 예약 가능 시간, 오늘 대기열(R8), 환자 검색·상세·등록(R9·R10·추가)"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from shared.models import AuditLog

URL = "/api/v1/reservations"
DAY = "2026-10-22"            # KST 날짜
KST9 = "2026-10-22T00:00:00Z"  # KST 09:00


def book(client, world, login_id, request_id, at, doctor="dr_im", dept="IM", patient="C-1"):
    payload = {"department_id": world.ids[dept], "doctor_id": world.ids[doctor] if doctor else None,
               "reserved_at": at, "visit_type": "RETURN"}
    if not login_id.startswith("patient"):
        payload["patient_id"] = world.ids[patient]
    return client.post(URL, json=payload, headers=world.h(login_id, request_id))


def audit_actions():
    from shared.db import SessionLocal

    with SessionLocal() as db:
        return [(a.action, a.result, a.target_id) for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]


# ---------- 같은 의사 같은 시간 409 ----------

def test_same_doctor_same_time_is_409(client, world):
    assert book(client, world, "director", "o-1", "2026-10-22T01:00:00Z").status_code == 201
    r = book(client, world, "nurse_im", "o-2", "2026-10-22T01:00:00Z")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert book(client, world, "director", "o-3", "2026-10-22T01:10:00Z").status_code == 409  # 30분 칸과 겹침
    assert book(client, world, "director", "o-4", "2026-10-22T00:31:00Z").status_code == 409
    assert book(client, world, "director", "o-5", "2026-10-22T01:30:00Z").status_code == 201  # 바로 다음 칸
    assert book(client, world, "director", "o-6", "2026-10-22T00:30:00Z").status_code == 201  # 바로 앞 칸
    # 다른 의사·의사 미지정은 같은 시간이어도 된다
    assert book(client, world, "director", "o-7", "2026-10-22T01:00:00Z", doctor="dr_os", dept="OS").status_code == 201
    assert book(client, world, "director", "o-8", "2026-10-22T01:00:00Z", doctor=None).status_code == 201


def test_canceled_reservation_frees_the_time(client, world):
    first = book(client, world, "director", "c-1", "2026-10-22T02:00:00Z").json()
    client.delete(f"{URL}/{first['id']}", headers=world.h("director"))
    assert book(client, world, "director", "c-2", "2026-10-22T02:00:00Z").status_code == 201


def test_same_request_id_retry_is_200_not_409(client, world):
    assert book(client, world, "director", "retry", "2026-10-22T03:00:00Z").status_code == 201
    assert book(client, world, "director", "retry", "2026-10-22T03:00:00Z").status_code == 200


# ---------- 예약 가능 시간 ----------

def slots(client, world, login_id="patient01", doctor="dr_im", day=DAY):
    return client.get("/api/v1/slots", params={"doctor_id": world.ids[doctor], "date": day},
                      headers=world.h(login_id))


def test_slots_all_free_day(client, world):
    r = slots(client, world)
    assert r.status_code == 200
    body = r.json()
    assert body["slot_minutes"] == 30 and body["date"] == DAY
    assert len(body["available"]) == 17  # 09:00 ~ 17:00 시작 (17:30 진료 끝)
    assert body["available"][0] == "2026-10-22T00:00:00.000Z"   # KST 09:00
    assert body["available"][-1] == "2026-10-22T08:00:00.000Z"  # KST 17:00


def test_slots_exclude_booked_and_overlapping(client, world):
    book(client, world, "director", "s-1", "2026-10-22T01:00:00Z")          # KST 10:00 칸
    book(client, world, "director", "s-2", "2026-10-22T03:10:00Z")          # KST 12:10 → 12:00·12:30 칸과 겹침
    canceled = book(client, world, "director", "s-3", "2026-10-22T05:00:00Z").json()
    client.delete(f"{URL}/{canceled['id']}", headers=world.h("director"))  # 취소는 자리를 비운다
    book(client, world, "director", "s-4", "2026-10-22T06:00:00Z", doctor="dr_os", dept="OS")  # 다른 의사
    available = slots(client, world).json()["available"]
    assert "2026-10-22T01:00:00.000Z" not in available
    assert "2026-10-22T03:00:00.000Z" not in available and "2026-10-22T03:30:00.000Z" not in available
    assert "2026-10-22T05:00:00.000Z" in available and "2026-10-22T06:00:00.000Z" in available
    assert len(available) == 14


def test_slots_validation_and_roles(client, world):
    assert slots(client, world, doctor="dr_im_off").status_code == 404  # 비활성 의사
    r = client.get("/api/v1/slots", params={"doctor_id": 9999, "date": DAY}, headers=world.h("tester"))
    assert r.status_code == 404
    assert client.get("/api/v1/slots", params={"date": DAY}, headers=world.h("tester")).status_code == 422
    assert client.get("/api/v1/slots", params={"doctor_id": 1}, headers=world.h("tester")).status_code == 422
    assert client.get("/api/v1/slots", params={"doctor_id": 1, "date": DAY}).status_code == 401
    for login_id in ("patient01", "nurse_im", "head_im", "director", "tester", "doctor", "manager", "admin", "dev"):
        assert slots(client, world, login_id).status_code == 200


# ---------- R8 대기열 ----------

def test_queue_order_scope_and_filters(client, world):
    a = book(client, world, "director", "q-a", "2026-10-22T01:00:00Z").json()["id"]
    b = book(client, world, "director", "q-b", "2026-10-22T02:00:00Z").json()["id"]
    c = book(client, world, "director", "q-c", "2026-10-22T01:00:00Z", doctor="dr_os", dept="OS").json()["id"]
    book(client, world, "director", "q-d", "2026-10-22T03:00:00Z")  # 접수 안 한 예약은 대기열에 없다
    canceled = book(client, world, "director", "q-e", "2026-10-22T04:00:00Z").json()["id"]
    other_day = book(client, world, "director", "q-f", "2026-10-23T01:00:00Z").json()["id"]
    for rid in (b, a, c, canceled, other_day):
        client.post(f"{URL}/{rid}/checkin", headers=world.h("director"))
    client.delete(f"{URL}/{canceled}", headers=world.h("director"))

    r = client.get("/api/v1/queue", params={"date": DAY}, headers=world.h("nurse_im"))
    assert r.status_code == 200
    q = r.json()
    assert q["date"] == DAY and q["department_id"] == world.ids["IM"]
    assert [(i["reservation_id"], i["queue_no"]) for i in q["items"]] == [(b, 1), (a, 2)]  # 접수 순, 대기번호 포함
    assert q["items"][0]["patient_name"] == "환자C-1" and q["items"][0]["doctor_name"] == "dr_im"

    assert client.get("/api/v1/queue", params={"date": DAY}, headers=world.h("head_im")).json()["items"][0][
        "reservation_id"] == b
    r = client.get("/api/v1/queue", params={"date": DAY, "department_id": world.ids["OS"]}, headers=world.h("nurse_im"))
    assert r.status_code == 403
    os_q = client.get("/api/v1/queue", params={"date": DAY, "department_id": world.ids["OS"]},
                      headers=world.h("director")).json()
    assert [i["reservation_id"] for i in os_q["items"]] == [c]
    everything = client.get("/api/v1/queue", params={"date": DAY}, headers=world.h("tester")).json()
    assert everything["department_id"] is None and len(everything["items"]) == 3

    for login_id in ("patient01", "doctor", "manager", "admin", "dev"):
        assert client.get("/api/v1/queue", headers=world.h(login_id)).status_code == 403


def test_queue_defaults_to_today_kst(client, world, frozen_clock):
    later_today = frozen_clock + timedelta(hours=1)  # 고정한 '지금'(KST 10/14 09:00) 의 1시간 뒤
    rid = book(client, world, "director", "today", later_today.strftime("%Y-%m-%dT%H:%M:%SZ"), doctor=None).json()["id"]
    client.post(f"{URL}/{rid}/checkin", headers=world.h("director"))
    items = client.get("/api/v1/queue", headers=world.h("nurse_im")).json()["items"]
    assert [i["reservation_id"] for i in items] == [rid]


# ---------- 환자 등록 ----------

def new_patient(client, world, login_id="nurse_im", **overrides):
    data = {"chart_no": "N-100", "name": "새환자", "birth_date": "1980-05-05", "phone": "010-1234-5678"}
    data.update(overrides)
    return client.post("/api/v1/patients", json=data, headers=world.h(login_id))


def test_create_patient_roles_and_rules(client, world):
    r = new_patient(client, world)
    assert r.status_code == 201
    # 새 환자는 간호사 부서에 예약이 없어 번호가 가려진다 (공개 범위: tests/test_phone_visibility.py)
    assert r.json()["chart_no"] == "N-100" and r.json()["phone"] == "010-****-5678"
    assert r.json()["phone_is_masked"] is True
    assert ("PATIENT_CREATE", "SUCCESS", str(r.json()["id"])) in audit_actions()
    assert new_patient(client, world, "admin", chart_no="N-101").status_code == 201
    assert new_patient(client, world, "director", chart_no="N-102").status_code == 201
    assert new_patient(client, world, "tester", chart_no="N-103").status_code == 201
    for login_id in ("patient01", "head_im", "doctor", "manager", "dev"):
        assert new_patient(client, world, login_id, chart_no=f"X-{login_id}").status_code == 403

    r = new_patient(client, world, chart_no="N-100")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert new_patient(client, world, chart_no="N-200", role="DIRECTOR").status_code == 422
    assert new_patient(client, world, chart_no="N 201").status_code == 422
    assert new_patient(client, world, chart_no="N-202", birth_date="2999-01-01").status_code == 422
    assert new_patient(client, world, chart_no="N-203", phone="call me").status_code == 422
    assert new_patient(client, world, chart_no="N-204", phone=None).status_code == 201


# ---------- R9 검색 · R10 상세 ----------

def test_search_patients(client, world):
    new_patient(client, world, chart_no="K-001", name="김하나", phone="010-1111-2222")
    new_patient(client, world, chart_no="K-002", name="이하나", phone="01033334444")

    r = client.get("/api/v1/patients", params={"q": "하나"}, headers=world.h("nurse_im"))
    assert r.status_code == 200
    page = r.json()
    assert page["total"] == 2 and {i["name"] for i in page["items"]} == {"김하나", "이하나"}
    masked = {i["chart_no"]: i["phone"] for i in page["items"]}
    assert masked == {"K-001": "010-****-2222", "K-002": "*******4444"}  # 예약 없는 환자: 간호사에게도 가림
    assert all(i["phone_is_masked"] for i in page["items"]) and "phone_masked" not in page["items"][0]

    assert client.get("/api/v1/patients", params={"q": "K-00"}, headers=world.h("director")).json()["total"] == 2
    assert client.get("/api/v1/patients", params={"q": "-001"}, headers=world.h("tester")).json()["total"] == 0  # 앞부분만
    # LIKE 특수문자는 글자 그대로
    assert client.get("/api/v1/patients", params={"q": "%"}, headers=world.h("tester")).json()["total"] == 0
    assert client.get("/api/v1/patients", params={"q": "_"}, headers=world.h("tester")).json()["total"] == 0
    assert client.get("/api/v1/patients", params={"q": ""}, headers=world.h("tester")).status_code == 422
    assert client.get("/api/v1/patients", headers=world.h("tester")).status_code == 422
    for login_id in ("patient01", "head_im", "doctor", "admin", "dev"):
        assert client.get("/api/v1/patients", params={"q": "하나"}, headers=world.h(login_id)).status_code == 403
    # 원무과장은 전화번호 확인용으로 검색할 수 있다 (번호는 전체)
    r = client.get("/api/v1/patients", params={"q": "하나"}, headers=world.h("manager"))
    assert r.status_code == 200 and {i["phone"] for i in r.json()["items"]} == {"010-1111-2222", "01033334444"}


def test_patient_detail_scope_and_audit(client, world):
    im = book(client, world, "director", "d-1", "2026-10-22T01:00:00Z").json()["id"]
    os_ = book(client, world, "director", "d-2", "2026-10-23T01:00:00Z", doctor="dr_os", dept="OS").json()["id"]
    pid = world.ids["C-1"]

    full = client.get(f"/api/v1/patients/{pid}", headers=world.h("director")).json()
    assert full["chart_no"] == "C-1" and [r["id"] for r in full["recent_reservations"]] == [os_, im]  # 최신순
    nurse = client.get(f"/api/v1/patients/{pid}", headers=world.h("nurse_im")).json()
    assert [r["id"] for r in nurse["recent_reservations"]] == [im]  # 자기 부서 예약만
    assert ("PATIENT_VIEW", "SUCCESS", str(pid)) in audit_actions()

    assert client.get("/api/v1/patients/99999", headers=world.h("director")).status_code == 404
    for login_id in ("head_im", "doctor", "admin", "dev"):
        assert client.get(f"/api/v1/patients/{pid}", headers=world.h(login_id)).status_code == 403
    # 환자는 본인 기록만 (patient01 = C-1)
    assert client.get(f"/api/v1/patients/{pid}", headers=world.h("patient01")).status_code == 200
    other = world.ids["C-2"]
    assert client.get(f"/api/v1/patients/{other}", headers=world.h("patient01")).status_code == 403


def test_recent_reservations_limited_to_10(client, world):
    for i in range(12):
        t = (datetime(2026, 10, 22) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert book(client, world, "director", f"many-{i}", t).status_code == 201
    detail = client.get(f"/api/v1/patients/{world.ids['C-1']}", headers=world.h("tester")).json()
    assert len(detail["recent_reservations"]) == 10


@pytest.mark.parametrize("phone, masked", [("010-1234-5678", "010-****-5678"), ("02-123-4567", "02-***-4567"),
                                           ("01012345678", "*******5678"), (None, None)])
def test_mask_phone(phone, masked):
    from app.main import mask_phone

    assert mask_phone(phone) == masked


# ---------- 지난 시각 (판단 4번 수정) ----------

def test_create_in_the_past_is_422(client, world, frozen_clock):
    just_before = (frozen_clock - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = book(client, world, "director", "past-1", just_before)
    assert r.status_code == 422 and just_before not in r.text  # 보낸 값은 되돌려 주지 않는다
    assert book(client, world, "director", "past-2", "2026-10-13T08:00:00+09:00").status_code == 422  # 시간대 포함
    assert book(client, world, "patient01", "past-3", "2025-01-01T00:00:00Z").status_code == 422
    assert book(client, world, "director", "now", frozen_clock.strftime("%Y-%m-%dT%H:%M:%SZ")).status_code == 201


def test_slots_today_skip_past_times(client, world, monkeypatch):
    from shared import timeutil

    # KST 2026-10-14 10:10 → 10:30 칸부터 (10:30 ~ 17:00, 14칸)
    monkeypatch.setattr(timeutil, "utcnow_naive", lambda: datetime(2026, 10, 14, 1, 10))
    available = slots(client, world, day="2026-10-14").json()["available"]
    assert available[0] == "2026-10-14T01:30:00.000Z" and len(available) == 14
    # 칸 시작 시각과 '지금'이 같으면 그 칸은 남는다
    monkeypatch.setattr(timeutil, "utcnow_naive", lambda: datetime(2026, 10, 14, 1, 30))
    assert slots(client, world, day="2026-10-14").json()["available"][0] == "2026-10-14T01:30:00.000Z"
    # 진료 시간이 끝난 뒤(KST 17:30)는 빈 목록
    monkeypatch.setattr(timeutil, "utcnow_naive", lambda: datetime(2026, 10, 14, 8, 30))
    assert slots(client, world, day="2026-10-14").json()["available"] == []


def test_slots_past_date_empty_future_date_full(client, world):
    assert slots(client, world, day="2026-10-13").json()["available"] == []   # 어제
    assert slots(client, world, day="2020-01-01").json()["available"] == []
    assert len(slots(client, world, day="2026-10-15").json()["available"]) == 17  # 내일
