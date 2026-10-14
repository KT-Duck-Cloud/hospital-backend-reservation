"""tools/data_verifier.py 를 실제 예약 서비스(uvicorn)에 붙여 record → verify, 그다음 DB 를 1건 지우고 1건 바꿔 다시 verify.

로그인은 인증 서비스 대신 작은 HTTP 서버가 한다 (같은 DB 에 세션을 만들고 진짜 토큰을 준다. conftest.make_token).
예약 서비스·DB·검증 도구는 실제 것을 쓴다.
"""
import importlib.util
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update

from conftest import ROOT, make_token

# 검증 도구는 reservation-app 의 tools/ 에 있다. 서비스별 저장소에서는 hospital-gitops 의 것을 쓴다
# (DATA_VERIFIER_PATH 로 지정하거나, 옆 폴더 ../hospital-gitops 가 있으면 그것). 없으면 건너뛴다.
_candidates = [Path(os.environ["DATA_VERIFIER_PATH"])] if os.environ.get("DATA_VERIFIER_PATH") else [
    ROOT / "tools" / "data_verifier.py", ROOT.parent / "hospital-gitops" / "tools" / "data_verifier.py"]
_tool = next((c for c in _candidates if c.is_file()), None)
if _tool is None:
    pytest.skip("tools/data_verifier.py 가 없어 건너뜀 (DATA_VERIFIER_PATH 로 지정)", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("data_verifier", _tool)
dv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dv)

PASSWORD = secrets.token_urlsafe(12)
LOGIN_IDS = "patient01,nurse_im,director,tester"   # 예약 생성 권한이 있는 역할 (conftest.USERS)


def start_stub_auth():
    from shared.db import SessionLocal
    from shared.models import User

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with SessionLocal() as db:
                user = db.scalar(select(User).where(User.login_id == body["login_id"]))
                if user is None or body["password"] != PASSWORD:
                    self.send_response(401)
                    self.end_headers()
                    return
                out = {"access_token": make_token(db, user), "token_type": "bearer",
                       "user": {"id": user.id, "role": user.role, "department_id": user.department_id,
                                "patient_id": user.patient_id}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_record_then_verify_detects_deleted_and_changed(world, live_server, monkeypatch, tmp_path):
    import os

    from app import main, models
    from shared.db import SessionLocal

    base_url, _ = live_server(main.app)
    auth = start_stub_auth()
    try:
        monkeypatch.setenv("VERIFIER_BASE_URL", base_url)
        monkeypatch.setenv("VERIFIER_AUTH_URL", f"http://127.0.0.1:{auth.server_address[1]}")
        monkeypatch.setenv("VERIFIER_PASSWORD", PASSWORD)
        monkeypatch.setenv("VERIFIER_LOGIN_IDS", LOGIN_IDS)
        monkeypatch.setenv("VERIFIER_DB_URL", os.environ["DB_URL"])  # 테스트 DB (운영은 app_readonly)
        monkeypatch.setenv("VERIFIER_DB_USERNAME", os.environ.get("DB_USERNAME", ""))
        monkeypatch.setenv("VERIFIER_DB_PASSWORD", os.environ.get("DB_PASSWORD", ""))
        out = tmp_path / "drill.jsonl"

        assert dv.main(["record", "--out", str(out), "--count", "12", "--interval", "0", "--run-id", "e2e", "--seed", "1"]) == 0
    finally:
        auth.shutdown()

    lines = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 12
    assert {ln["role"] for ln in lines} == {"PATIENT", "NURSE", "DIRECTOR", "TESTER"}   # 역할별 계정을 돌아가며
    assert len({ln["request_id"] for ln in lines}) == 12                              # 매 요청 새 X-Request-ID
    assert all(ln["http_status"] == 201 and ln["reservation_id"] for ln in lines), lines
    for ln in lines:
        assert ln["run_id"] == "e2e" and ln["request_at"].endswith("Z") and ln["response_at"].endswith("Z")
        assert ln["patient_id"] and ln["department_id"] and ln["reserved_at"]
    assert all(ln["department_id"] == world.ids["IM"] for ln in lines if ln["role"] == "NURSE")
    assert PASSWORD not in out.read_text(encoding="utf-8")

    # 1) 그대로면 이상 없음
    assert dv.main(["verify", "--in", str(out), "--report", str(tmp_path / "ok")]) == 0
    ok = json.loads((tmp_path / "ok.json").read_text(encoding="utf-8"))
    assert (ok["success"], ok["found"], ok["missing"], ok["duplicate"], ok["mismatch"]) == (12, 12, 0, 0, 0)

    # 2) 일부러 1건 지우고 1건 바꾼다
    R = models.Reservation
    with SessionLocal() as db:
        db.execute(delete(R).where(R.request_id == lines[5]["request_id"]))
        db.execute(update(R).where(R.request_id == lines[8]["request_id"]).values(department_id=world.ids["OS"]
                   if lines[8]["department_id"] != world.ids["OS"] else world.ids["IM"]))
        db.commit()
    assert dv.main(["verify", "--in", str(out), "--report", str(tmp_path / "bad")]) == 1
    bad = json.loads((tmp_path / "bad.json").read_text(encoding="utf-8"))
    assert (bad["missing"], bad["mismatch"], bad["duplicate"]) == (1, 1, 0)
    assert bad["details"]["missing"][0]["request_id"] == lines[5]["request_id"]
    assert bad["details"]["mismatch"][0]["fields"] == ["department_id"]
    assert bad["rpo_seconds"] >= 0
    assert Path(tmp_path / "bad.md").exists()
