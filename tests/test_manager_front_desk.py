"""원무과장(MANAGER) 권한 (보안 담당 승인): 부서와 상관없이 예약 조회·접수·취소, 환자 검색.

- 허용: GET /reservations(+상세), POST /reservations/{id}/checkin, DELETE /reservations/{id}, GET /patients(+상세)
- 거부: 예약 만들기, 대기열, 진료 시작·완료, 내 진료기록·영수증, 환자 등록 (감사 로그 DENIED). 계정·감사 로그·현황은 다른 서비스의 권한 표로 막힌다
- 응답 어디에도 진단·진료 메모·예약 메모가 없다 (환자 상세 포함)
"""
import base64
import os

import pytest
from sqlalchemy import select

from shared.models import AuditLog

URL = "/api/v1/reservations"
SECRET_MEMO = "SECRET-MEMO-잔기침이-심해요"
SECRET_DIAGNOSIS = "SECRET-DIAGNOSIS-급성기관지염"
SECRET_NOTE = "SECRET-NOTE-항생제-처방"
SECRETS = (SECRET_MEMO, SECRET_DIAGNOSIS, SECRET_NOTE)


@pytest.fixture()
def desk(client, world, monkeypatch):
    """내과(IM)·정형외과(OS) 예약 각 1건 (메모 포함) + C-1 의 암호화된 진료기록 1건."""
    for name in list(os.environ):
        if name.startswith("MEDICAL_KEY"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MEDICAL_KEY", base64.b64encode(os.urandom(32)).decode())

    from datetime import datetime

    from app import models
    from shared import crypto
    from shared.db import SessionLocal

    ids = {}
    for key, patient, dept, doctor, at in (("im", "C-1", "IM", "dr_im", "2026-10-22T01:00:00Z"),
                                           ("os", "C-2", "OS", "dr_os", "2026-10-22T02:00:00Z")):
        r = client.post(URL, headers=world.h("director", f"mgr-{key}"), json={
            "department_id": world.ids[dept], "doctor_id": world.ids[doctor], "reserved_at": at,
            "visit_type": "RETURN", "patient_id": world.ids[patient], "memo": SECRET_MEMO})
        assert r.status_code == 201, r.text
        ids[key] = r.json()["id"]
    with SessionLocal() as db:
        pid = world.ids["C-1"]
        d_enc, d_nonce, version = crypto.encrypt_field(SECRET_DIAGNOSIS, field="diagnosis", patient_id=pid)
        n_enc, n_nonce, _ = crypto.encrypt_field(SECRET_NOTE, field="note", patient_id=pid)
        now = datetime(2026, 10, 1)
        db.add(models.MedicalRecord(patient_id=pid, doctor_id=world.ids["dr_im"], diagnosis_enc=d_enc,
                                    note_enc=n_enc, diagnosis_nonce=d_nonce, note_nonce=n_nonce,
                                    key_version=version, created_at=now, updated_at=now))
        db.commit()
    return ids


def audit_rows(user_id=None):
    from shared.db import SessionLocal

    with SessionLocal() as db:
        stmt = select(AuditLog).order_by(AuditLog.id)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        return [(a.action, a.result, a.target_type, a.target_id) for a in db.scalars(stmt)]


def no_secrets(response):
    for secret in SECRETS:
        assert secret not in response.text, secret


# ---------- 허용 ----------

def test_manager_lists_and_reads_reservations_of_every_department(client, world, desk):
    r = client.get(URL, headers=world.h("manager"))
    assert r.status_code == 200
    got = {i["id"]: i for i in r.json()["items"]}
    assert set(got) == {desk["im"], desk["os"]}                      # 자기 부서(ADM)와 상관없이 전체
    assert all(i["memo"] is None for i in got.values())
    no_secrets(r)
    # 부서·날짜 조건도 쓸 수 있다
    only_os = client.get(URL, params={"department_id": world.ids["OS"]}, headers=world.h("manager"))
    assert [i["id"] for i in only_os.json()["items"]] == [desk["os"]]

    for key in ("im", "os"):
        detail = client.get(f"{URL}/{desk[key]}", headers=world.h("manager"))
        assert detail.status_code == 200
        assert detail.json()["memo"] is None
        no_secrets(detail)

    # 같은 예약을 원장은 메모까지 본다 (메모 자체는 저장돼 있다)
    assert client.get(f"{URL}/{desk['im']}", headers=world.h("director")).json()["memo"] == SECRET_MEMO


def test_manager_checks_in_and_cancels_any_department_with_audit(client, world, desk):
    manager_id = world.ids["manager"]
    r = client.post(f"{URL}/{desk['os']}/checkin", headers=world.h("manager"))
    assert r.status_code == 200, r.text
    assert r.json()["checkin_status"] == "CHECKED_IN" and r.json()["queue_no"] == 1
    assert r.json()["memo"] is None
    no_secrets(r)
    again = client.post(f"{URL}/{desk['os']}/checkin", headers=world.h("manager"))
    assert again.status_code == 200 and again.json()["memo"] is None      # 이미 접수: 그대로 200, 메모는 계속 비어 있음

    c = client.delete(f"{URL}/{desk['im']}", headers=world.h("manager"))
    assert c.status_code == 200 and c.json()["status"] == "CANCELED"
    assert c.json()["memo"] is None
    no_secrets(c)

    rows = audit_rows(manager_id)
    assert ("RESERVATION_CHECKIN", "SUCCESS", "reservation", str(desk["os"])) in rows
    assert ("RESERVATION_CANCEL", "SUCCESS", "reservation", str(desk["im"])) in rows


def test_manager_cannot_cancel_after_treatment_started(client, world, desk):
    assert client.post(f"{URL}/{desk['im']}/checkin", headers=world.h("manager")).status_code == 200
    assert client.post(f"{URL}/{desk['im']}/start-treatment", headers=world.h("doctor")).status_code == 200
    assert client.delete(f"{URL}/{desk['im']}", headers=world.h("manager")).status_code == 409


def test_manager_patient_search_and_detail_have_no_clinical_content(client, world, desk):
    search = client.get("/api/v1/patients", params={"q": "환자C-"}, headers=world.h("manager"))
    assert search.status_code == 200 and search.json()["total"] == 2
    no_secrets(search)

    detail = client.get(f"/api/v1/patients/{world.ids['C-1']}", headers=world.h("manager"))
    assert detail.status_code == 200
    body = detail.json()
    assert [r["id"] for r in body["recent_reservations"]] == [desk["im"]]
    assert all(r["memo"] is None for r in body["recent_reservations"])
    assert not {"diagnosis", "note", "records", "medical_records"} & set(body)
    no_secrets(detail)
    assert ("PATIENT_VIEW", "SUCCESS", "patient", str(world.ids["C-1"])) in audit_rows(world.ids["manager"])


# ---------- 거부 (감사 로그 DENIED) ----------

def test_manager_is_denied_everything_else_and_audited(client, world, desk):
    h = world.h("manager", "mgr-new")
    body = {"department_id": world.ids["IM"], "doctor_id": world.ids["dr_im"], "reserved_at": "2026-10-23T01:00:00Z",
            "visit_type": "RETURN", "patient_id": world.ids["C-1"]}
    attempts = [
        ("POST", URL, {"json": body}),                                           # 예약 만들기
        ("GET", "/api/v1/queue", {}),                                            # 대기열
        ("GET", "/api/v1/doctor/queue", {}),
        ("POST", f"{URL}/{desk['im']}/start-treatment", {}),                     # 진료 시작·완료는 담당 의사만
        ("POST", f"{URL}/{desk['im']}/complete-treatment", {}),
        ("GET", "/api/v1/me/records", {}),                                       # 진단·진료 메모가 있는 API
        ("GET", "/api/v1/me/receipts", {}),
        ("POST", "/api/v1/patients", {"json": {"chart_no": "X-1", "name": "새", "birth_date": "1990-01-01"}}),
    ]
    for method, url, kwargs in attempts:
        r = client.request(method, url, headers=h, **kwargs)
        assert r.status_code == 403, (method, url, r.status_code)
        no_secrets(r)
    denied = [(a, res, t, tid) for a, res, t, tid in audit_rows(world.ids["manager"]) if res == "DENIED"]
    assert len(denied) == len(attempts)
    assert {tid for _, _, _, tid in denied} >= {
        f"POST {URL}", "GET /api/v1/queue", "GET /api/v1/me/records", "POST /api/v1/patients",
    }
    # 거부된 요청은 아무것도 만들지 않았다
    assert client.get(URL, headers=world.h("director")).json()["total"] == 2


def test_other_non_clinical_roles_still_cannot_use_reservation_lookup(client, world, desk):
    for login_id in ("admin", "dev", "doctor"):
        assert client.get(URL, headers=world.h(login_id)).status_code == 403
    # 간호사는 여전히 자기 부서만
    assert client.post(f"{URL}/{desk['os']}/checkin", headers=world.h("nurse_im")).status_code == 403
