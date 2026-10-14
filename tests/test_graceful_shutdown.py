"""종료 신호(SIGTERM) 중에도 처리 중인 예약 저장은 끝까지 완료되는지.

실제 uvicorn 서버(스레드)로 띄우고, 예약 저장(감사 로그 추가 → COMMIT) 중간에 멈춰 둔 상태에서 종료 신호를 보낸다.
- 새 연결은 받지 않는다.
- 멈춰 있던 요청은 이어서 201 로 끝나고, 예약이 DB 에 남는다.
- 서버는 그 뒤에 종료 단계(DB 풀 닫기)까지 마친다.
"""
import http.client
import json
import signal
import threading
import urllib.parse

import pytest
from sqlalchemy import select

RESERVED_AT = "2026-10-20T01:00:00Z"  # 고정 시계(conftest FROZEN_NOW)보다 뒤


def post_reservation(base_url, headers, body, result):
    u = urllib.parse.urlsplit(base_url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    conn.request("POST", "/api/v1/reservations", body=json.dumps(body),
                 headers={**headers, "Content-Type": "application/json"})
    r = conn.getresponse()
    result["status"], result["body"] = r.status, json.loads(r.read())
    conn.close()


def test_sigterm_finishes_in_flight_reservation(world, live_server, monkeypatch, capsys):
    from app import main, models
    from shared import audit
    from shared.db import SessionLocal

    entered, release = threading.Event(), threading.Event()
    original = audit.add_audit

    def paused_add_audit(*args, **kwargs):
        entered.set()            # 예약 행은 flush 됐고, COMMIT 전이다
        release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "add_audit", paused_add_audit)
    base_url, server = live_server(main.app)

    request_id = "graceful-shutdown-1"
    body = {"patient_id": world.ids["C-1"], "department_id": world.ids["IM"], "reserved_at": RESERVED_AT,
            "visit_type": "RETURN"}
    result = {}
    sender = threading.Thread(target=post_reservation,
                              args=(base_url, world.h("director", request_id), body, result))
    sender.start()
    assert entered.wait(10), "예약 저장이 시작되지 않음"

    server.handle_exit(signal.SIGTERM, None)  # SIGTERM 처리기와 같은 동작

    # 새 연결은 받지 않는다
    u = urllib.parse.urlsplit(base_url)
    with pytest.raises(OSError):
        for _ in range(50):
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=3)
            conn.request("GET", "/healthz")
            conn.getresponse().read()
            conn.close()
            threading.Event().wait(0.1)  # 아직 닫히기 전이면 잠깐 뒤 다시

    release.set()
    sender.join(20)
    assert result.get("status") == 201, result
    server.thread.join(25)
    assert not server.thread.is_alive(), "처리 중 요청이 끝난 뒤 서버가 종료되지 않음"

    with SessionLocal() as db:
        saved = db.scalar(select(models.Reservation).where(models.Reservation.request_id == request_id))
    assert saved is not None and saved.id == result["body"]["id"]
    assert "shutdown complete" in capsys.readouterr().out
