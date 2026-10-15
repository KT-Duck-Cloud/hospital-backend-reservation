"""예약 서비스 R1~R7 테스트 (SQLite). 역할별 허용·거부, 범위, 중복 방지, 쓰기 차단, 오류 응답."""
from datetime import datetime, timedelta

import pymysql
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from shared import config
from shared.models import AuditLog

URL = "/api/v1/reservations"

# 같은 의사·같은 시간 예약은 409 라서, 테스트 예약은 따로 정하지 않으면 30분씩 다른 칸을 쓴다.
# KST 2026-10-22 09:00 (= UTC 00:00) 부터. 테스트마다 처음부터 다시.
_slot = {"n": 0}


@pytest.fixture(autouse=True)
def _reset_slot():
    _slot["n"] = 0


def next_time() -> str:
    t = datetime(2026, 10, 22) + timedelta(minutes=30 * _slot["n"])
    _slot["n"] += 1
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def body(world, **overrides):
    data = {"department_id": world.ids["IM"], "doctor_id": world.ids["dr_im"],
            "reserved_at": next_time(), "visit_type": "FIRST", "memo": "기침"}
    data.update(overrides)
    return data


def create(client, world, login_id, request_id, **overrides):
    payload = body(world, **overrides)
    if login_id != "patient01" and login_id != "patient02" and "patient_id" not in overrides:
        payload["patient_id"] = world.ids["C-1"]
    return client.post(URL, json=payload, headers=world.h(login_id, request_id))


def audits(action=None, result=None):
    from shared.db import SessionLocal

    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    return [a for a in rows if (action is None or a.action == action) and (result is None or a.result == result)]


def count_reservations():
    from app import models
    from shared.db import SessionLocal

    with SessionLocal() as db:
        return len(db.scalars(select(models.Reservation)).all())


# ---------- R1 생성 · X-Request-ID ----------

def test_create_then_duplicate(client, world):
    r = create(client, world, "nurse_im", "req-1", reserved_at="2026-10-22T00:00:00Z")
    assert r.status_code == 201
    out = r.json()
    assert out["status"] == "BOOKED" and out["checkin_status"] == "NOT_CHECKED_IN"
    assert out["reserved_at"] == "2026-10-22T00:00:00.000Z"
    assert out["patient_id"] == world.ids["C-1"] and out["created_by"] == world.ids["nurse_im"]
    r2 = create(client, world, "nurse_im", "req-1", reserved_at="2026-10-22T00:00:00Z")
    assert r2.status_code == 200 and r2.json()["id"] == out["id"]
    assert count_reservations() == 1
    assert len(audits("RESERVATION_CREATE", "SUCCESS")) == 1


def test_request_id_header_rules(client, world):
    assert create(client, world, "nurse_im", None).status_code == 400
    assert create(client, world, "nurse_im", "x" * 65).status_code == 400
    assert create(client, world, "nurse_im", "a").status_code == 201
    assert create(client, world, "nurse_im", "b").status_code == 201
    assert count_reservations() == 2


def test_request_id_of_someone_else_is_409_not_leaked(client, world):
    assert create(client, world, "patient01", "shared-id").status_code == 201
    r = create(client, world, "patient02", "shared-id")
    assert r.status_code == 409 and "patient_id" not in r.text


def test_patient_creates_only_for_self(client, world):
    r = create(client, world, "patient01", "p-1")  # patient_id 를 안 보내면 토큰 값
    assert r.status_code == 201 and r.json()["patient_id"] == world.ids["C-1"]
    assert create(client, world, "patient01", "p-2", patient_id=world.ids["C-1"]).status_code == 201
    r = create(client, world, "patient01", "p-3", patient_id=world.ids["C-2"])
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert audits("ACCESS_DENIED", "DENIED")[-1].user_id == world.ids["patient01"]


def test_role_in_body_or_query_is_never_used(client, world):
    # 본문에 role·user id 를 넣으면 허용 칸이 아니라 422, 아무것도 저장되지 않는다.
    for extra in ({"role": "DIRECTOR"}, {"created_by": 1}, {"user_id": 1}):
        r = client.post(URL, json={**body(world), **extra}, headers=world.h("patient01", "role-1"))
        assert r.status_code == 422
    assert count_reservations() == 0
    # 조회 조건에 role 을 넣어도 422, 헤더에 넣어도 무시 → 환자는 여전히 본인 범위
    assert client.get(URL, params={"role": "DIRECTOR"}, headers=world.h("patient01")).status_code == 422
    create(client, world, "nurse_im", "other", patient_id=world.ids["C-2"])
    headers = {**world.h("patient01"), "X-Role": "DIRECTOR", "X-User-Id": str(world.ids["director"])}
    assert client.get(URL, headers=headers).json()["total"] == 0


def test_create_validates_references(client, world):
    assert create(client, world, "director", "v-1", patient_id=999).status_code == 400
    assert create(client, world, "director", "v-2", department_id=999).status_code == 400
    assert create(client, world, "director", "v-3", doctor_id=world.ids["dr_os"]).status_code == 400  # 다른 과
    assert create(client, world, "director", "v-4", doctor_id=world.ids["dr_im_off"]).status_code == 400  # 비활성
    r = client.post(URL, json=body(world), headers=world.h("director", "v-5"))  # 직원은 patient_id 필수
    assert r.status_code == 400
    assert create(client, world, "director", "v-6", visit_type="OTHER").status_code == 422
    assert create(client, world, "director", "v-7", memo="x" * 501).status_code == 422


def test_validation_error_does_not_echo_input(client, world):
    secret_memo = "개인정보" * 200
    r = create(client, world, "director", "e-1", memo=secret_memo)
    assert r.status_code == 422 and "개인정보" not in r.text


# ---------- 역할별 허용·거부 ----------

ROLE_CAN = {
    # login_id: (생성, 목록, 취소, 접수)
    "patient01": (True, True, True, False),
    "nurse_im": (True, True, True, True),
    "head_im": (False, True, False, False),
    "director": (True, True, True, True),
    "tester": (True, True, True, True),
    "doctor": (False, False, False, False),
    "manager": (False, True, True, True),   # 원무과장: 부서와 상관없이 조회·접수·취소 (만들기는 불가)
    "admin": (False, False, False, False),
    "dev": (False, False, False, False),
}


@pytest.mark.parametrize("login_id", list(ROLE_CAN))
def test_role_matrix(client, world, login_id):
    can_create, can_list, can_cancel, can_checkin = ROLE_CAN[login_id]
    target = create(client, world, "director", f"base-{login_id}").json()  # 환자 C-1, 내과

    r = create(client, world, login_id, f"new-{login_id}")
    assert r.status_code == (201 if can_create else 403)
    assert client.get(URL, headers=world.h(login_id)).status_code == (200 if can_list else 403)
    assert client.get(f"{URL}/{target['id']}", headers=world.h(login_id)).status_code == (200 if can_list else 403)
    r = client.post(f"{URL}/{target['id']}/checkin", headers=world.h(login_id))
    assert r.status_code == (200 if can_checkin else 403)
    r = client.delete(f"{URL}/{target['id']}", headers=world.h(login_id))
    assert r.status_code == (200 if can_cancel else 403)
    # 진료과·의사 목록은 로그인한 모두
    assert client.get("/api/v1/departments", headers=world.h(login_id)).status_code == 200
    assert client.get("/api/v1/doctors", headers=world.h(login_id)).status_code == 200


def test_no_token_is_401(client, world):
    for method, path in (("get", URL), ("post", URL), ("get", f"{URL}/1"), ("delete", f"{URL}/1"),
                         ("post", f"{URL}/1/checkin"), ("get", "/api/v1/departments"), ("get", "/api/v1/doctors")):
        assert getattr(client, method)(path).status_code == 401


def test_other_department_is_403(client, world):
    os_res = create(client, world, "director", "os-1", department_id=world.ids["OS"],
                    doctor_id=world.ids["dr_os"]).json()
    for login_id in ("nurse_im", "head_im"):
        assert client.get(f"{URL}/{os_res['id']}", headers=world.h(login_id)).status_code == 403
        r = client.get(URL, params={"department_id": world.ids["OS"]}, headers=world.h(login_id))
        assert r.status_code == 403
    assert client.delete(f"{URL}/{os_res['id']}", headers=world.h("nurse_im")).status_code == 403
    assert client.post(f"{URL}/{os_res['id']}/checkin", headers=world.h("nurse_im")).status_code == 403
    r = create(client, world, "nurse_im", "os-2", department_id=world.ids["OS"], doctor_id=world.ids["dr_os"])
    assert r.status_code == 403
    # 자기 부서는 된다
    assert client.get(f"{URL}/{os_res['id']}", headers=world.h("nurse_os")).status_code == 200
    assert len(audits("ACCESS_DENIED", "DENIED")) >= 6


def test_patient_cannot_touch_others_reservation(client, world):
    other = create(client, world, "patient02", "p2-1").json()
    assert client.get(f"{URL}/{other['id']}", headers=world.h("patient01")).status_code == 403
    assert client.delete(f"{URL}/{other['id']}", headers=world.h("patient01")).status_code == 403
    mine = create(client, world, "patient01", "p1-1").json()
    listed = client.get(URL, headers=world.h("patient01")).json()
    assert [i["id"] for i in listed["items"]] == [mine["id"]] and listed["total"] == 1
    denied = audits("ACCESS_DENIED", "DENIED")
    assert denied[-1].target_type == "reservation" and denied[-1].target_id == str(other["id"])


def test_not_found_is_404(client, world):
    assert client.get(f"{URL}/99999", headers=world.h("director")).status_code == 404


# ---------- R2 목록 필터 ----------

def test_list_filters_and_paging(client, world):
    # KST 10/20 = UTC 10/19 15:00 ~ 10/20 15:00
    times = ["2026-10-19T14:59:00Z", "2026-10-19T15:00:00Z", "2026-10-20T01:00:00Z", "2026-10-20T15:00:00Z"]
    ids = [create(client, world, "director", f"f-{i}", reserved_at=t, doctor_id=None).json()["id"]
           for i, t in enumerate(times)]
    client.delete(f"{URL}/{ids[2]}", headers=world.h("director"))

    page = client.get(URL, params={"date_from": "2026-10-20", "date_to": "2026-10-20"},
                      headers=world.h("director")).json()
    assert [i["id"] for i in page["items"]] == [ids[1], ids[2]] and page["total"] == 2
    page = client.get(URL, params={"status": "BOOKED"}, headers=world.h("director")).json()
    assert page["total"] == 3
    page = client.get(URL, params={"size": 2, "page": 2}, headers=world.h("director")).json()
    assert page["page"] == 2 and page["size"] == 2 and page["total"] == 4 and len(page["items"]) == 2
    assert client.get(URL, params={"size": 101}, headers=world.h("director")).status_code == 422
    r = client.get(URL, params={"date_from": "2026-10-21", "date_to": "2026-10-20"}, headers=world.h("director"))
    assert r.status_code == 422
    assert client.get(URL, params={"doctor_id": world.ids["dr_im"]}, headers=world.h("director")).json()["total"] == 0


# ---------- R4 취소 · R5 접수 ----------

def test_cancel_keeps_row_and_is_idempotent(client, world):
    rid = create(client, world, "nurse_im", "c-1").json()["id"]
    r = client.delete(f"{URL}/{rid}", headers=world.h("nurse_im"))
    assert r.status_code == 200 and r.json()["status"] == "CANCELED" and r.json()["canceled_at"]
    again = client.delete(f"{URL}/{rid}", headers=world.h("nurse_im"))
    assert again.status_code == 200 and again.json()["canceled_at"] == r.json()["canceled_at"]
    assert client.get(f"{URL}/{rid}", headers=world.h("nurse_im")).status_code == 200  # 행은 남는다
    assert len(audits("RESERVATION_CANCEL", "SUCCESS")) == 1


def test_checkin_rules_and_queue_number(client, world):
    a = create(client, world, "nurse_im", "q-1").json()["id"]
    b = create(client, world, "nurse_im", "q-2", reserved_at="2026-10-22T05:00:00Z").json()["id"]
    canceled = create(client, world, "nurse_im", "q-3").json()["id"]
    client.delete(f"{URL}/{canceled}", headers=world.h("nurse_im"))

    r = client.post(f"{URL}/{canceled}/checkin", headers=world.h("nurse_im"))
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    first = client.post(f"{URL}/{b}/checkin", headers=world.h("nurse_im")).json()
    second = client.post(f"{URL}/{a}/checkin", headers=world.h("nurse_im")).json()
    assert first["checkin_status"] == "CHECKED_IN" and first["queue_no"] == 1 and second["queue_no"] == 2
    again = client.post(f"{URL}/{b}/checkin", headers=world.h("nurse_im"))
    assert again.status_code == 200 and again.json()["queue_no"] == 1
    assert len(audits("RESERVATION_CHECKIN", "SUCCESS")) == 2


# ---------- R6 · R7 ----------

def test_departments_and_doctors(client, world):
    deps = client.get("/api/v1/departments", headers=world.h("patient01")).json()
    assert [d["code"] for d in deps] == ["IM", "OS", "ADM"]
    docs = client.get("/api/v1/doctors", params={"department_id": world.ids["IM"]},
                      headers=world.h("patient01")).json()
    assert [d["id"] for d in docs] == [world.ids["dr_im"]]  # 비활성 의사는 빠진다
    assert client.get("/api/v1/doctors", params={"x": 1}, headers=world.h("patient01")).status_code == 422


# ---------- 쓰기 차단 · DB 오류 ----------

def test_read_only_mode_blocks_writes_with_spec_format(client, world, monkeypatch):
    monkeypatch.setattr(config, "READ_ONLY_MODE", True)
    r = create(client, world, "nurse_im", "ro-1")
    assert r.status_code == 503
    assert r.json() == {"code": "WRITE_DISABLED", "message": r.json()["message"], "request_id": "ro-1"}
    assert client.get(URL, headers=world.h("nurse_im")).status_code == 200  # 조회는 된다
    assert client.get("/readyz").status_code == 200


def _mysql_error(code: int) -> OperationalError:
    return OperationalError("INSERT ... (숨김)", {}, pymysql.err.OperationalError(code, "internal detail"))


@pytest.mark.parametrize("code, expected", [(1290, "WRITE_DISABLED"), (1836, "WRITE_DISABLED"),
                                            (3118, "DB_UNAVAILABLE"), (1045, "DB_UNAVAILABLE"),
                                            (2013, "DB_UNAVAILABLE")])
def test_db_rejections_are_503(client, world, monkeypatch, code, expected):
    from sqlalchemy.orm import Session

    def failing_commit(self):
        raise _mysql_error(code)

    monkeypatch.setattr(Session, "commit", failing_commit)
    r = create(client, world, "nurse_im", f"db-{code}")
    assert r.status_code == 503 and r.json()["code"] == expected
    assert "internal detail" not in r.text and "INSERT" not in r.text


def test_unexpected_error_is_500_without_details(world, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(main, "find_by_request_id", boom)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.post(URL, json={**body(world), "patient_id": world.ids["C-1"]}, headers=world.h("director", "x-1"))
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in r.text


def test_audit_failure_means_no_success(client, world, monkeypatch):
    """감사 로그가 같은 트랜잭션이라, 감사 로그 저장이 실패하면 예약도 저장되지 않고 성공 응답도 없다."""
    from shared import audit

    def broken_audit(db, request, action, result, **kwargs):
        db.add(AuditLog(service="reservation", action=action, result=None))  # NOT NULL 위반

    monkeypatch.setattr(audit, "add_audit", broken_audit)
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(URL, json={**body(world), "patient_id": world.ids["C-1"]}, headers=world.h("director", "au-1"))
    assert r.status_code >= 500
    assert count_reservations() == 0


def test_health_endpoints_unchanged(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["service"] == "reservation"
    assert client.get("/readyz").status_code == 200


# ---------- 인그레스 뒤의 실제 클라이언트 IP ----------

@pytest.mark.parametrize("source, forwarded_allow_ips, expected", [
    ("10.42.0.15", "10.42.0.0/24", "203.0.113.7"),   # 믿는 인그레스 → X-Forwarded-For 의 실제 IP
    ("192.0.2.50", "10.42.0.0/24", "192.0.2.50"),    # 믿지 않는 곳 → 헤더 무시
    ("10.42.0.15", "127.0.0.1", "10.42.0.15"),       # 기본값: 프록시를 믿지 않음
])
def test_denied_audit_uses_real_client_ip(world, source, forwarded_allow_ips, expected):
    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.main import app

    other = create(TestClient(app), world, "patient02", "ip-1").json()["id"]
    with TestClient(ProxyHeadersMiddleware(app, trusted_hosts=forwarded_allow_ips), client=(source, 40000)) as c:
        headers = {**world.h("patient01"), "X-Forwarded-For": "198.51.100.66, 203.0.113.7"}
        assert c.get(f"{URL}/{other}", headers=headers).status_code == 403
    assert audits("ACCESS_DENIED", "DENIED")[-1].client_ip == expected


def test_reservation_responses_include_patient_name_and_chart_no(client, world):
    r = create(client, world, "nurse_im", "name-1")
    assert r.json()["patient_name"] == "환자C-1" and r.json()["chart_no"] == "C-1"
    item = client.get(URL, headers=world.h("nurse_im")).json()["items"][0]
    assert (item["patient_name"], item["chart_no"]) == ("환자C-1", "C-1")
    detail = client.get(f"{URL}/{r.json()['id']}", headers=world.h("patient01")).json()
    assert detail["patient_name"] == "환자C-1"
