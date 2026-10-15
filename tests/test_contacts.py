"""R19 GET /api/v1/contacts: 병원 대표 번호 + 진료과 대표 번호 + 역할별 직원 업무용 연락처 (shared/phone.py)

- PATIENT : 병원·진료과 대표 번호, 직원 없음
- NURSE   : 자기 부서 직원 (자기 부서 부서장 포함, 다른 부서 부서장은 없음) + 원무과 대표 번호
- DEPT_HEAD: 자기 부서 직원 (본인 포함)
- DOCTOR  : 자기 부서 부서장만
- DIRECTOR: 부서장 범위(자기 부서 직원, 원장에게 부서가 있을 때) + 모든 부서 부서장 (환자 번호·진료과 대표 번호는 계속 가림)
- MANAGER : 전체 직원
- 그 밖의 역할: 직원 없음
"""
import pytest

from conftest import DEPT_PHONES, WORK_PHONES
from shared import config
from shared.phone import mask_phone

URL = "/api/v1/contacts"
FAKE_HOSPITAL_PHONE = "02-0000-0000"
ALL_DEPTS_MASKED = {code: (mask_phone(p), True) for code, p in DEPT_PHONES.items()}


@pytest.fixture(autouse=True)
def hospital_phone(monkeypatch):
    monkeypatch.setattr(config, "HOSPITAL_PHONE", FAKE_HOSPITAL_PHONE)


def contacts(client, world, login_id):
    r = client.get(URL, headers=world.h(login_id))
    assert r.status_code == 200, (login_id, r.text)
    return r.json()


def staff_of(body):
    return {s["name"]: (s["work_phone"], s["work_phone_is_masked"], s["role"]) for s in body["staff"]}


def depts_of(body):
    return {d["code"]: (d["phone"], d["phone_is_masked"]) for d in body["departments"]}


def test_requires_login(client, world):
    assert client.get(URL).status_code == 401


def test_patient_gets_hospital_and_department_numbers_only(client, world):
    body = contacts(client, world, "patient01")
    assert body["hospital_phone"] == FAKE_HOSPITAL_PHONE
    assert depts_of(body) == {code: (p, False) for code, p in DEPT_PHONES.items()}
    assert body["staff"] == []


def test_hospital_phone_is_null_when_not_configured(client, world, monkeypatch):
    monkeypatch.setattr(config, "HOSPITAL_PHONE", "")
    assert contacts(client, world, "nurse_im")["hospital_phone"] is None


def test_nurse_sees_own_department_staff_including_own_head_only(client, world):
    body = contacts(client, world, "nurse_im")
    assert body["hospital_phone"] == FAKE_HOSPITAL_PHONE
    staff = staff_of(body)
    assert set(staff) == {"nurse_im", "head_im", "doctor"}          # 정형외과 부서장(head_os)은 없다
    assert staff["head_im"] == (WORK_PHONES["head_im"], False, "DEPT_HEAD")
    assert depts_of(body) == {**ALL_DEPTS_MASKED, "ADM": (DEPT_PHONES["ADM"], False)}
    # 정형외과 간호사는 반대
    assert set(staff_of(contacts(client, world, "nurse_os"))) == {"nurse_os", "head_os", "doctor_os"}


@pytest.mark.parametrize("login_id, head", [("doctor", "head_im"), ("doctor_os", "head_os")])
def test_doctor_sees_only_own_department_head(client, world, login_id, head):
    body = contacts(client, world, login_id)
    assert staff_of(body) == {head: (WORK_PHONES[head], False, "DEPT_HEAD")}
    assert depts_of(body) == ALL_DEPTS_MASKED


@pytest.mark.parametrize("login_id, expected", [
    ("head_im", {"head_im", "nurse_im", "doctor"}),
    ("head_os", {"head_os", "nurse_os", "doctor_os"}),
])
def test_dept_head_sees_own_department_staff_only(client, world, login_id, expected):
    body = contacts(client, world, login_id)
    staff = staff_of(body)
    assert set(staff) == expected
    assert all(not masked for _, masked, _ in staff.values())
    assert all(staff[n][0] == WORK_PHONES[n] for n in expected)
    assert depts_of(body) == ALL_DEPTS_MASKED       # 진료과 대표 번호는 계속 가림
    assert body["hospital_phone"] == FAKE_HOSPITAL_PHONE


def test_director_with_department_sees_that_department_plus_all_heads(client, world):
    """원장 = 부서장 범위(자기 부서 직원) + 모든 부서 부서장. 원장에게 부서가 있으면 그 부서 직원도 보인다."""
    from shared.db import SessionLocal
    from shared.models import User

    with SessionLocal() as db:
        db.get(User, world.ids["director"]).department_id = db.get(User, world.ids["head_im"]).department_id
        db.commit()
    names = set(staff_of(contacts(client, world, "director")))
    assert names == {"head_im", "head_os", "nurse_im", "doctor", "director"}
    assert all(not masked for _, masked, _ in staff_of(contacts(client, world, "director")).values())


def test_director_sees_all_department_heads_but_nothing_else_unmasked(client, world):
    body = contacts(client, world, "director")
    assert staff_of(body) == {
        "head_im": (WORK_PHONES["head_im"], False, "DEPT_HEAD"),
        "head_os": (WORK_PHONES["head_os"], False, "DEPT_HEAD"),
    }
    assert depts_of(body) == ALL_DEPTS_MASKED
    # 환자 번호는 원장도 가린다 (기존 공개 범위 그대로)
    r = client.get("/api/v1/patients", params={"q": "환자C-"}, headers=world.h("director"))
    assert all(i["phone_is_masked"] for i in r.json()["items"])


def test_manager_sees_all_staff(client, world):
    body = contacts(client, world, "manager")
    assert staff_of(body) == {name: (phone, False, role) for name, phone, role in (
        (n, WORK_PHONES[n], r) for n, r in _roles(client, world))}
    assert "patient01" not in staff_of(body)
    assert depts_of(body) == ALL_DEPTS_MASKED


def _roles(client, world):
    from sqlalchemy import select

    from shared.db import SessionLocal
    from shared.models import User

    with SessionLocal() as db:
        return [(u.login_id, u.role) for u in db.scalars(select(User).where(User.role != "PATIENT"))]


@pytest.mark.parametrize("login_id", ["admin", "dev", "tester"])
def test_other_roles_get_no_staff_and_masked_departments(client, world, login_id):
    body = contacts(client, world, login_id)
    assert body["staff"] == []
    assert depts_of(body) == ALL_DEPTS_MASKED
    assert body["hospital_phone"] == FAKE_HOSPITAL_PHONE


def test_inactive_staff_are_not_listed(client, world):
    from shared.db import SessionLocal
    from shared.models import User

    with SessionLocal() as db:
        db.get(User, world.ids["head_os"]).is_active = False
        db.commit()
    assert set(staff_of(contacts(client, world, "director"))) == {"head_im"}
    assert set(staff_of(contacts(client, world, "head_im"))) == {"head_im", "nurse_im", "doctor"}
    assert staff_of(contacts(client, world, "doctor_os")) == {}
