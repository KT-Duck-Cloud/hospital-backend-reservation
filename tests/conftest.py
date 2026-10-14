"""테스트 설정. 두 가지로 돈다.

1) 기본: 임시 SQLite DB (표는 모델로 만들고 끝나면 지운다). @pytest.mark.mysql 테스트는 건너뛴다.
2) MySQL: 환경변수 TEST_MYSQL_URL(app_runtime 계정, schema.sql·seed.sql 이 들어간 버리는 DB)이 있으면
   @pytest.mark.mysql 테스트만 돈다. TEST_MYSQL_ADMIN_URL(관리자)이 있으면 read_only 전환 테스트도 돈다.
   표를 만들거나 지우지 않는다.

서명 키·비밀번호는 실행할 때마다 무작위로 만든다 (파일에 고정값 없음).
"""
import os
import secrets
import sys
import tempfile
from pathlib import Path

# 서비스 폴더 = tests/ 의 부모 (app/ 이 있는 곳). 저장소 루트 = shared/ 가 있는 가장 가까운 폴더.
# reservation-app(services/<이름>/tests) 과 서비스별 저장소(tests/) 두 구조에서 모두 동작한다.
SERVICE_DIR = Path(__file__).resolve().parents[1]
ROOT = next(p for p in [SERVICE_DIR, *SERVICE_DIR.parents] if (p / "shared").is_dir())
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SERVICE_DIR))

MYSQL_URL = os.environ.get("TEST_MYSQL_URL", "").strip()
if MYSQL_URL:
    os.environ["DATABASE_URL"] = MYSQL_URL
else:
    _db = Path(tempfile.mkdtemp()) / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
os.environ["JWT_SECRET"] = secrets.token_urlsafe(48)
os.environ.pop("READ_ONLY_MODE", None)

from datetime import date, datetime  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "mysql: 실제 MySQL(TEST_MYSQL_URL)에서만 도는 테스트")


def pytest_collection_modifyitems(config, items):
    for item in items:
        is_mysql = "mysql" in item.keywords
        if MYSQL_URL and not is_mysql:
            item.add_marker(pytest.mark.skip(reason="MySQL 모드: SQLite 용 테스트는 건너뜀"))
        if not MYSQL_URL and is_mysql:
            item.add_marker(pytest.mark.skip(reason="TEST_MYSQL_URL 이 없어 건너뜀"))


# 테스트의 '지금'. 과거 시각 예약은 422 라서, 날짜를 박아 둔 테스트가 시간이 지나도 깨지지 않게 시계를 고정한다.
# 2026-10-14 00:00 UTC = KST 09:00 (첫 진료 칸)
FROZEN_NOW = datetime(2026, 10, 14, 0, 0)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    from shared import timeutil

    monkeypatch.setattr(timeutil, "utcnow_naive", lambda: FROZEN_NOW)
    return FROZEN_NOW


def make_token(db, user) -> str:
    """로그인 서비스를 거치지 않고 세션을 만들어 출입증을 발급한다 (검증은 앱과 똑같이 한다)."""
    from shared.auth import issue_token, session_timeouts
    from shared.models import UserSession, utcnow

    now = utcnow()
    idle, absolute = session_timeouts(user.role)
    session = UserSession(id=secrets.token_urlsafe(32), user_id=user.id, last_seen_at=now,
                          idle_expires_at=now + idle, absolute_expires_at=now + absolute,
                          created_at=now, updated_at=now)
    db.add(session)
    db.commit()
    return issue_token(user, session.id, now, session.absolute_expires_at)


# SQLite 테스트용 사용자: login_id → (역할, 진료과 코드, 환자 차트번호)
USERS = {
    "patient01": ("PATIENT", None, "C-1"),
    "patient02": ("PATIENT", None, "C-2"),
    "nurse_im": ("NURSE", "IM", None),
    "nurse_os": ("NURSE", "OS", None),
    "head_im": ("DEPT_HEAD", "IM", None),
    "director": ("DIRECTOR", None, None),
    "tester": ("TESTER", None, None),
    "doctor": ("DOCTOR", "IM", None),
    "manager": ("MANAGER", "ADM", None),
    "admin": ("ADMIN", None, None),
    "dev": ("DEVELOPER", None, None),
}


class World:
    """SQLite 테스트 데이터: 진료과·의사·환자 id 와 역할별 Authorization 헤더"""

    def __init__(self, ids, headers):
        self.ids = ids
        self.headers = headers

    def h(self, login_id, request_id=None):
        headers = dict(self.headers[login_id])
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers


@pytest.fixture()
def world():
    from app import models
    from shared.db import Base, SessionLocal, engine
    from shared.models import User

    Base.metadata.create_all(engine)
    ids = {}
    with SessionLocal() as db:
        for code, name in (("IM", "내과"), ("OS", "정형외과"), ("ADM", "원무과")):
            d = models.Department(code=code, name=name)
            db.add(d)
            db.flush()
            ids[code] = d.id
        for key, dept, active in (("dr_im", "IM", True), ("dr_os", "OS", True), ("dr_im_off", "IM", False)):
            doc = models.Doctor(department_id=ids[dept], name=key, room_no="101", is_active=active)
            db.add(doc)
            db.flush()
            ids[key] = doc.id
        for chart in ("C-1", "C-2"):
            p = models.Patient(chart_no=chart, name=f"환자{chart}", birth_date=date(1990, 1, 1))
            db.add(p)
            db.flush()
            ids[chart] = p.id
        headers = {}
        for login_id, (role, dept, chart) in USERS.items():
            u = User(login_id=login_id, password_hash="!", name=login_id, role=role,
                     department_id=ids.get(dept), patient_id=ids.get(chart))
            db.add(u)
            db.flush()
            ids[login_id] = u.id
            headers[login_id] = {"Authorization": f"Bearer {make_token(db, u)}"}
        db.commit()
    yield World(ids, headers)
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def live_server():
    """앱을 실제 uvicorn 서버로 띄운다 (스레드). 돌려주는 함수: start(app) → (주소, uvicorn.Server).

    종료 처리(SIGTERM)는 uvicorn 의 handle_exit 를 그대로 부른다 (신호 처리기가 하는 일과 같다. 스레드에서는 신호를 못 받음).
    """
    import socket
    import threading
    import time

    import uvicorn

    started = []

    def start(app, timeout_graceful_shutdown: int = 20):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # log_config=None: 앱이 설정한 JSON 로그(shared/logs.py)를 uvicorn 이 덮어쓰지 않게 한다 (운영도 같은 결과)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                                               timeout_graceful_shutdown=timeout_graceful_shutdown))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "서버가 시작되지 않음"
            time.sleep(0.05)
        started.append((server, thread))
        server.thread = thread
        return f"http://127.0.0.1:{port}", server

    yield start
    for server, thread in started:
        server.should_exit = True
        thread.join(30)
