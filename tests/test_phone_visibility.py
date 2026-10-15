"""전화번호 공개 범위 (shared/phone.py, 보안 담당 확정): 역할별로 전체 번호가 보이거나 010-****-1234 로 가려진다.

- PATIENT : 본인 번호 + 부서 대표 번호
- MANAGER : 모든 환자 번호 + 직원 업무용 연락처
- NURSE   : 자기 부서에 (취소 아닌) 예약이 있는 환자 번호 + 원무과 대표 번호 + 자기 부서 직원 업무용 연락처
- DOCTOR  : 자기 부서 부서장 업무용 연락처만 (tests/test_contacts.py). 나머지 번호는 가림
- DIRECTOR: 모든 부서 부서장 업무용 연락처만 (tests/test_contacts.py). 환자 번호·부서 대표 번호는 가림
- DEPT_HEAD: 환자·부서 대표 번호는 가림, 자기 부서 직원 번호는 전체 (/contacts, test_contacts.py)
- ADMIN, DEVELOPER, TESTER: 전부 가림
- 감사 로그·앱 로그에는 전체 번호가 남지 않는다
"""
import pytest
from sqlalchemy import select

from conftest import DEPT_PHONES, PATIENT_PHONES, WORK_PHONES
from shared.models import AuditLog
from shared.phone import mask_phone

URL = "/api/v1/reservations"
C1_FULL, C2_FULL = PATIENT_PHONES["C-1"], PATIENT_PHONES["C-2"]       # 010-1111-1001 / 010-2222-1002
C1_MASKED, C2_MASKED = "010-****-1001", "010-****-1002"


def book(client, world, request_id, at, patient="C-1", dept="IM", doctor="dr_im"):
    payload = {"department_id": world.ids[dept], "doctor_id": world.ids[doctor], "reserved_at": at,
               "visit_type": "RETURN", "patient_id": world.ids[patient]}
    r = client.post(URL, json=payload, headers=world.h("director", request_id))
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def booked(client, world):
    """C-1 은 내과(IM) 예약이 있고, C-2 는 정형외과(OS) 예약만 있다."""
    book(client, world, "ph-1", "2026-10-22T01:00:00Z", patient="C-1", dept="IM", doctor="dr_im")
    book(client, world, "ph-2", "2026-10-22T02:00:00Z", patient="C-2", dept="OS", doctor="dr_os")
    return world


def search(client, world, login_id):
    r = client.get("/api/v1/patients", params={"q": "환자C-"}, headers=world.h(login_id))
    assert r.status_code == 200, (login_id, r.text)
    return {i["chart_no"]: (i["phone"], i["phone_is_masked"]) for i in r.json()["items"]}


def detail_phone(client, world, login_id, chart):
    r = client.get(f"/api/v1/patients/{world.ids[chart]}", headers=world.h(login_id))
    assert r.status_code == 200, (login_id, r.text)
    return r.json()["phone"], r.json()["phone_is_masked"]


def departments(client, world, login_id):
    r = client.get("/api/v1/departments", headers=world.h(login_id))
    assert r.status_code == 200
    return {d["code"]: (d["phone"], d["phone_is_masked"]) for d in r.json()}


ALL_DEPTS_MASKED = {code: (mask_phone(p), True) for code, p in DEPT_PHONES.items()}
ALL_DEPTS_FULL = {code: (p, False) for code, p in DEPT_PHONES.items()}


# ---------- 환자 ----------

def test_patient_sees_own_number_and_department_numbers(client, booked):
    w = booked
    assert detail_phone(client, w, "patient01", "C-1") == (C1_FULL, False)
    assert departments(client, w, "patient01") == ALL_DEPTS_FULL
    # 다른 환자의 정보는 번호뿐 아니라 조회 자체가 안 된다 (+ 감사 로그 DENIED)
    r = client.get(f"/api/v1/patients/{w.ids['C-2']}", headers=w.h("patient01"))
    assert r.status_code == 403
    assert client.get("/api/v1/patients", params={"q": "환자"}, headers=w.h("patient01")).status_code == 403
    assert client.get("/api/v1/staff", headers=w.h("patient01")).status_code == 403
    assert ("ACCESS_DENIED", "DENIED", str(w.ids["C-2"])) in audit_rows()


# ---------- 원무과장 ----------

def test_manager_sees_all_patient_numbers_and_staff_numbers(client, booked):
    w = booked
    assert search(client, w, "manager") == {"C-1": (C1_FULL, False), "C-2": (C2_FULL, False)}
    assert detail_phone(client, w, "manager", "C-2") == (C2_FULL, False)
    detail = client.get(f"/api/v1/patients/{w.ids['C-1']}", headers=w.h("manager")).json()
    # 원무과장은 부서와 상관없이 예약을 보지만 예약 메모는 비어 있다 (tests/test_manager_front_desk.py)
    assert [r["patient_id"] for r in detail["recent_reservations"]] == [w.ids["C-1"]]
    assert all(r["memo"] is None for r in detail["recent_reservations"])

    staff = client.get("/api/v1/staff", headers=w.h("manager")).json()
    got = {s["name"]: (s["work_phone"], s["work_phone_is_masked"]) for s in staff}
    assert got == {name: (phone, False) for name, phone in WORK_PHONES.items()}
    assert "patient01" not in got

    # 부서 대표 번호는 확정된 공개 범위(환자·간호사)에 없으므로 가린다
    assert departments(client, w, "manager") == ALL_DEPTS_MASKED


# ---------- 간호사 ----------

def test_nurse_sees_numbers_only_for_own_department(client, booked):
    w = booked
    # 내과 간호사: 내과 예약이 있는 C-1 만 전체. 정형외과 예약만 있는 C-2 는 가림
    assert search(client, w, "nurse_im") == {"C-1": (C1_FULL, False), "C-2": (C2_MASKED, True)}
    assert detail_phone(client, w, "nurse_im", "C-1") == (C1_FULL, False)
    assert detail_phone(client, w, "nurse_im", "C-2") == (C2_MASKED, True)
    # 정형외과 간호사는 반대
    assert search(client, w, "nurse_os") == {"C-1": (C1_MASKED, True), "C-2": (C2_FULL, False)}

    # 부서 대표 번호: 원무과만 전체
    expected = {**ALL_DEPTS_MASKED, "ADM": (DEPT_PHONES["ADM"], False)}
    assert departments(client, w, "nurse_im") == expected

    # 직원 업무용 연락처: 자기 부서(IM) 직원만 나오고 전체 번호
    staff = client.get("/api/v1/staff", headers=w.h("nurse_im")).json()
    assert {s["name"] for s in staff} == {"nurse_im", "head_im", "doctor"}
    assert all(s["work_phone"] == WORK_PHONES[s["name"]] and s["work_phone_is_masked"] is False for s in staff)


def test_nurse_does_not_get_number_from_canceled_reservation(client, world):
    rid = book(client, world, "ph-c1", "2026-10-22T01:00:00Z", patient="C-1", dept="IM")
    assert search(client, world, "nurse_im")["C-1"] == (C1_FULL, False)
    assert client.delete(f"{URL}/{rid}", headers=world.h("director")).status_code == 200
    assert search(client, world, "nurse_im")["C-1"] == (C1_MASKED, True)


def test_nurse_new_patient_response_is_masked_until_reservation(client, world):
    r = client.post("/api/v1/patients", headers=world.h("nurse_im"),
                    json={"chart_no": "N-1", "name": "새환자", "birth_date": "1980-05-05", "phone": "010-9999-8888"})
    assert r.status_code == 201
    assert r.json()["phone"] == "010-****-8888" and r.json()["phone_is_masked"] is True
    assert "010-9999-8888" not in r.text


# ---------- 의사·부서장·원장·관리자·개발자·테스터: 전부 가림 ----------

@pytest.mark.parametrize("login_id", ["director", "tester"])
def test_director_and_tester_see_masked_patient_numbers(client, booked, login_id):
    w = booked
    assert search(client, w, login_id) == {"C-1": (C1_MASKED, True), "C-2": (C2_MASKED, True)}
    assert detail_phone(client, w, login_id, "C-1") == (C1_MASKED, True)
    assert departments(client, w, login_id) == ALL_DEPTS_MASKED
    assert client.get("/api/v1/staff", headers=w.h(login_id)).status_code == 403


@pytest.mark.parametrize("login_id", ["doctor", "head_im", "admin", "dev"])  # 직원 목록은 R19 /contacts (test_contacts.py)
def test_other_roles_see_masked_department_numbers(client, booked, login_id):
    w = booked
    assert departments(client, w, login_id) == ALL_DEPTS_MASKED
    assert client.get("/api/v1/staff", headers=w.h(login_id)).status_code == 403
    assert client.get("/api/v1/patients", params={"q": "환자"}, headers=w.h(login_id)).status_code == 403


def test_admin_created_patient_response_is_masked(client, world):
    r = client.post("/api/v1/patients", headers=world.h("admin"),
                    json={"chart_no": "N-2", "name": "새환자", "birth_date": "1980-05-05", "phone": "010-7777-6666"})
    assert r.status_code == 201
    assert r.json()["phone"] == "010-****-6666" and r.json()["phone_is_masked"] is True
    assert "010-7777-6666" not in r.text
    # 저장된 값은 그대로이고, 원무과장은 전체 번호를 본다 (가림은 응답에서만)
    assert detail_phone(client, world, "manager", "C-1") == (C1_FULL, False)
    found = client.get("/api/v1/patients", params={"q": "N-2"}, headers=world.h("manager")).json()["items"]
    assert found[0]["phone"] == "010-7777-6666"


def test_missing_phone_stays_null_and_not_flagged(client, world):
    r = client.post("/api/v1/patients", headers=world.h("tester"),
                    json={"chart_no": "N-3", "name": "번호없음", "birth_date": "1980-05-05"})
    assert r.status_code == 201
    assert r.json()["phone"] is None and r.json()["phone_is_masked"] is False


# ---------- 로그에 전체 번호가 남지 않는다 ----------

def audit_rows():
    from shared.db import SessionLocal

    with SessionLocal() as db:
        return [(a.action, a.result, a.target_id) for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]


def test_full_numbers_never_appear_in_audit_or_app_logs(client, booked, capsys):
    w = booked
    capsys.readouterr()
    full_numbers = [C1_FULL, C2_FULL, *DEPT_PHONES.values(), *WORK_PHONES.values()]

    search(client, w, "manager")
    detail_phone(client, w, "manager", "C-1")
    detail_phone(client, w, "nurse_im", "C-1")
    departments(client, w, "patient01")
    client.get("/api/v1/staff", headers=w.h("manager"))
    client.get(f"/api/v1/patients/{w.ids['C-2']}", headers=w.h("patient01"))  # 거부
    client.post("/api/v1/patients", headers=w.h("nurse_im"),
                json={"chart_no": "N-9", "name": "로그확인", "birth_date": "1980-05-05", "phone": "010-5555-4444"})
    full_numbers.append("010-5555-4444")

    out = capsys.readouterr().out
    assert out.strip(), "앱 로그가 비어 있다 (테스트가 로그를 못 잡음)"
    from shared.db import SessionLocal

    with SessionLocal() as db:
        audit_text = "\n".join(
            " ".join(str(v) for v in (a.user_id, a.service, a.action, a.target_type, a.target_id,
                                      a.request_id, a.client_ip, a.result))
            for a in db.scalars(select(AuditLog)))
    assert audit_text.strip()
    for number in full_numbers:
        assert number not in out, f"앱 로그에 전체 번호가 남음: {number}"
        assert number not in audit_text, f"감사 로그에 전체 번호가 남음: {number}"
        digits = number.replace("-", "")
        assert digits not in out and digits not in audit_text
