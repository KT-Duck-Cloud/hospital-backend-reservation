"""DB 재시작·중단 뒤 자동 재연결 (실제 MySQL + docker compose 에서만 돈다).

- 풀에 연결이 있는 상태에서 DB 컨테이너를 재시작 → 앱을 다시 띄우지 않고 다음 요청이 바로 200.
- DB 가 멈춰 있는 동안 → 오래 묶이지 않고 503 DB_UNAVAILABLE, 다시 켜지면 200.

실행 (저장소 루트, compose 의 mysql 이 떠 있을 때. 테스트용 계정·세션이 DB 에 남는다):
  $env:TEST_DB_URL = "mysql://127.0.0.1:3306/hospital"
  $env:TEST_DB_USERNAME = "app_runtime"
  $env:TEST_DB_PASSWORD = "<비밀번호>"
  $env:RECOVERY_COMPOSE_SERVICE = "mysql"
  .venv\\Scripts\\python -m pytest services/reservation/tests/test_recovery.py
"""
import os
import secrets
import subprocess
import time

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool

from conftest import ROOT, make_token

SERVICE = os.environ.get("RECOVERY_COMPOSE_SERVICE", "").strip()
pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(not SERVICE, reason="RECOVERY_COMPOSE_SERVICE 가 없어 건너뜀 (DB 컨테이너를 재시작하는 테스트)"),
]
DEPARTMENTS = "/api/v1/departments"
DB_WAIT_SECONDS = 180


def compose(*args: str) -> None:
    # docker-compose.yml 이 있는 폴더 (서비스별 저장소에서는 RECOVERY_COMPOSE_DIR=../hospital-gitops 처럼 지정)
    cwd = os.environ.get("RECOVERY_COMPOSE_DIR") or ROOT
    subprocess.run(["docker", "compose", *args], cwd=cwd, check=True, capture_output=True, timeout=300)


def wait_db_up() -> float:
    """앱과 따로, 새 연결로 DB 가 다시 받을 때까지 기다린다. 기다린 초를 돌려준다."""
    from shared.db import ENGINE_URL as url, build_connect_args

    probe = create_engine(url, connect_args=build_connect_args(url), poolclass=NullPool)
    started = time.monotonic()
    try:
        while True:
            try:
                with probe.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return time.monotonic() - started
            except Exception:
                assert time.monotonic() - started < DB_WAIT_SECONDS, "DB 가 다시 켜지지 않음"
                time.sleep(1)
    finally:
        probe.dispose()


@pytest.fixture()
def headers():
    from shared.db import SessionLocal
    from shared.models import User

    with SessionLocal() as db:
        user = User(login_id=f"zz_test_{secrets.token_hex(4)}", password_hash="!", name="복구테스트", role="DIRECTOR")
        db.add(user)
        db.commit()
        return {"Authorization": f"Bearer {make_token(db, user)}"}


def test_first_request_after_db_restart_succeeds(client, headers):
    from shared.db import engine

    for _ in range(3):
        assert client.get(DEPARTMENTS, headers=headers).status_code == 200
    assert engine.pool.checkedin() >= 1  # 풀에 (곧 죽을) 연결이 남아 있다

    compose("restart", SERVICE)
    wait_db_up()

    r = client.get(DEPARTMENTS, headers=headers)  # 다시 시도하지 않고 첫 요청
    assert r.status_code == 200, r.text
    assert client.get("/readyz").status_code == 200


def test_db_down_returns_503_quickly_then_recovers(client, headers):
    assert client.get(DEPARTMENTS, headers=headers).status_code == 200
    compose("stop", SERVICE)
    try:
        started = time.monotonic()
        r = client.get(DEPARTMENTS, headers=headers)
        assert r.status_code == 503 and r.json()["code"] == "DB_UNAVAILABLE"
        assert time.monotonic() - started < 15  # 연결 시간 제한 안에 응답 (묶이지 않음)
        assert client.get("/readyz").status_code == 503
    finally:
        compose("start", SERVICE)
        wait_db_up()
    assert client.get(DEPARTMENTS, headers=headers).status_code == 200


def test_reservation_saved_after_restart(client, headers):
    """재시작 뒤 쓰기(예약 저장)도 바로 된다."""
    from app import models
    from shared.db import SessionLocal

    compose("restart", SERVICE)
    wait_db_up()
    with SessionLocal() as db:
        dept = db.scalar(select(models.Department).where(models.Department.code == "IM"))
        patient = db.scalar(select(models.Patient).order_by(models.Patient.id).limit(1))
    request_id = f"recovery-{secrets.token_hex(6)}"
    r = client.post("/api/v1/reservations", headers={**headers, "X-Request-ID": request_id}, json={
        "patient_id": patient.id, "department_id": dept.id, "reserved_at": "2026-12-01T01:00:00Z", "visit_type": "RETURN"})
    assert r.status_code == 201, r.text
