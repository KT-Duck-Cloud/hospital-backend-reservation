"""앱 로그: JSON 한 줄, request_id 로 찾을 수 있고 토큰·환자 정보·조회 조건이 남지 않는다 (shared/logs.py)."""
import json

RESERVED_AT = "2026-10-20T01:00:00Z"


def json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.strip():
            out.append(json.loads(line))  # 모든 줄이 JSON 이어야 한다
    return out


def test_request_log_found_by_request_id_without_secrets(client, world, capsys):
    capsys.readouterr()
    request_id = "log-test-0001"
    headers = world.h("director", request_id)
    token = headers["Authorization"].split(" ", 1)[1]
    memo = "비밀메모-환자상태"
    r = client.post("/api/v1/reservations", headers=headers, json={
        "patient_id": world.ids["C-1"], "department_id": world.ids["IM"], "reserved_at": RESERVED_AT,
        "visit_type": "RETURN", "memo": memo})
    assert r.status_code == 201
    client.get("/api/v1/patients", params={"q": "환자C-1"}, headers=world.h("director", "log-test-0002"))

    out = capsys.readouterr().out
    lines = json_lines(out)
    mine = [ln for ln in lines if ln.get("request_id") == request_id]
    access = [ln for ln in mine if ln["logger"] == "access"]
    assert len(access) == 1
    a = access[0]
    assert a["service"] == "reservation" and a["level"] == "INFO"
    assert a["method"] == "POST" and a["path"] == "/api/v1/reservations" and a["status"] == 201
    assert isinstance(a["duration_ms"], (int, float)) and a["duration_ms"] >= 0
    assert a["ts"].endswith("Z")
    # 같은 요청의 감사 기록도 같은 request_id 로 찾을 수 있다 (표준 출력으로 나간 경우)
    search = [ln for ln in lines if ln.get("request_id") == "log-test-0002" and ln["logger"] == "access"]
    assert search and search[0]["path"] == "/api/v1/patients"  # 조회 조건(?q=이름)은 남기지 않는다

    for secret in (token, token.split(".")[2], memo, "환자C-1"):
        assert secret not in out


def test_db_error_log_has_kind_not_sql(client, world, capsys, monkeypatch):
    """DB 오류 로그: 종류·번호만 (SQL·값·접속 주소 없음) + 같은 request_id."""
    from sqlalchemy.exc import OperationalError

    from app import main

    def broken(*args, **kwargs):
        raise OperationalError("SELECT secret_value FROM patients", {"p": "환자이름"}, Exception("conn lost"))

    monkeypatch.setattr(main, "find_by_request_id", broken)
    capsys.readouterr()
    r = client.post("/api/v1/reservations", headers=world.h("director", "log-db-err"), json={
        "patient_id": world.ids["C-1"], "department_id": world.ids["IM"], "reserved_at": RESERVED_AT,
        "visit_type": "RETURN"})
    assert r.status_code == 503 and r.json()["code"] == "DB_UNAVAILABLE"
    out = capsys.readouterr().out
    errs = [ln for ln in json_lines(out) if ln["msg"] == "db error"]
    assert errs and errs[0]["request_id"] == "log-db-err" and errs[0]["kind"] == "DB_UNAVAILABLE"
    assert "secret_value" not in out and "환자이름" not in out
