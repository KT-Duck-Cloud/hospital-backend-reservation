"""R11 내 진료기록 (암호문 복호화), R12 내 영수증"""
import base64
import os

import pytest
from sqlalchemy import select

from shared.models import AuditLog

RECORDS = "/api/v1/me/records"
RECEIPTS = "/api/v1/me/receipts"
NON_PATIENTS = ["nurse_im", "head_im", "director", "tester", "doctor", "manager", "admin", "dev"]


@pytest.fixture()
def medical_key(monkeypatch):
    for name in list(os.environ):
        if name.startswith("MEDICAL_KEY"):
            monkeypatch.delenv(name)
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("MEDICAL_KEY", key)
    return key


@pytest.fixture()
def records(world, medical_key):
    """환자 C-1 진료기록 2건, C-2 1건 (crypto 로 암호화해서 저장), 영수증 C-1 2건·C-2 1건"""
    from datetime import datetime

    from app import models
    from shared import crypto
    from shared.db import SessionLocal

    plan = [("C-1", "급성 상기도 감염", "해열제 3일", datetime(2026, 10, 1)),
            ("C-1", "고혈압 추적 관찰", "4주 후 재진", datetime(2026, 10, 5)),
            ("C-2", "요추 염좌", "물리치료", datetime(2026, 10, 3))]
    ids = {}
    with SessionLocal() as db:
        for i, (chart, diagnosis, note, created) in enumerate(plan):
            pid = world.ids[chart]
            d_enc, d_nonce, version = crypto.encrypt_field(diagnosis, field="diagnosis", patient_id=pid)
            n_enc, n_nonce, _ = crypto.encrypt_field(note, field="note", patient_id=pid)
            r = models.MedicalRecord(patient_id=pid, doctor_id=world.ids["dr_im"], diagnosis_enc=d_enc,
                                     note_enc=n_enc, diagnosis_nonce=d_nonce, note_nonce=n_nonce,
                                     key_version=version, created_at=created, updated_at=created)
            db.add(r)
            db.flush()
            ids[f"rec{i}"] = r.id
        for chart, amount, status, created in (("C-1", 15000, "PAID", datetime(2026, 10, 1)),
                                               ("C-1", 8000, "UNPAID", datetime(2026, 10, 5)),
                                               ("C-2", 30000, "REFUNDED", datetime(2026, 10, 3))):
            rc = models.Receipt(patient_id=world.ids[chart], amount=amount, pay_status=status,
                                paid_at=created if status != "UNPAID" else None, created_at=created, updated_at=created)
            db.add(rc)
            db.flush()
            ids[f"{chart}-{amount}"] = rc.id
        db.commit()
    return ids


def record_audits():
    from shared.db import SessionLocal

    with SessionLocal() as db:
        return [(a.result, a.user_id, a.target_id)
                for a in db.scalars(select(AuditLog).where(AuditLog.action == "RECORD_VIEW").order_by(AuditLog.id))]


# ---------- R11 ----------

def test_patient_sees_only_own_records_decrypted(client, world, records):
    r = client.get(RECORDS, headers=world.h("patient01"))
    assert r.status_code == 200
    items = r.json()
    assert [i["diagnosis"] for i in items] == ["고혈압 추적 관찰", "급성 상기도 감염"]  # 최신순
    assert items[0]["note"] == "4주 후 재진" and items[0]["doctor_name"] == "dr_im"
    assert "요추 염좌" not in r.text  # 다른 환자 것은 없다
    other = client.get(RECORDS, headers=world.h("patient02")).json()
    assert [i["diagnosis"] for i in other] == ["요추 염좌"]
    assert record_audits() == [("SUCCESS", world.ids["patient01"], str(world.ids["C-1"])),
                               ("SUCCESS", world.ids["patient02"], str(world.ids["C-2"]))]


def test_stored_values_are_ciphertext(world, records):
    from app import models
    from shared.db import SessionLocal

    with SessionLocal() as db:
        for r in db.scalars(select(models.MedicalRecord)):
            assert "감염".encode() not in r.diagnosis_enc and "염좌".encode() not in r.diagnosis_enc
            assert len(r.diagnosis_nonce) == 12 and r.diagnosis_nonce != r.note_nonce


@pytest.mark.parametrize("login_id", NON_PATIENTS)
def test_non_patients_are_403_and_nothing_is_decrypted(client, world, records, monkeypatch, login_id):
    from shared import crypto

    calls = []
    monkeypatch.setattr(crypto, "decrypt_field", lambda *a, **k: calls.append(1))
    assert client.get(RECORDS, headers=world.h(login_id)).status_code == 403
    assert client.get(RECEIPTS, headers=world.h(login_id)).status_code == 403
    assert calls == []  # 권한 검사 전에 복호화하지 않는다


def test_no_token_is_401_and_nothing_is_decrypted(client, world, records, monkeypatch):
    from shared import crypto

    calls = []
    monkeypatch.setattr(crypto, "decrypt_field", lambda *a, **k: calls.append(1))
    assert client.get(RECORDS).status_code == 401
    assert calls == []


def test_missing_key_is_503_without_plaintext(client, world, records, monkeypatch):
    monkeypatch.delenv("MEDICAL_KEY")
    r = client.get(RECORDS, headers=world.h("patient01"))
    assert r.status_code == 503 and r.json()["code"] == "RECORD_UNAVAILABLE"
    assert "감염" not in r.text and "고혈압" not in r.text
    assert record_audits()[-1][0] == "FAIL"


def test_wrong_key_is_503(client, world, records, monkeypatch):
    monkeypatch.setenv("MEDICAL_KEY", base64.b64encode(os.urandom(32)).decode())
    r = client.get(RECORDS, headers=world.h("patient01"))
    assert r.status_code == 503 and r.json()["code"] == "RECORD_UNAVAILABLE"


def test_one_tampered_record_returns_no_plaintext_at_all(client, world, records):
    """한 건이라도 인증 태그가 틀리면 나머지 정상 기록도 돌려주지 않는다."""
    from app import models
    from shared.db import SessionLocal

    with SessionLocal() as db:
        rec = db.get(models.MedicalRecord, records["rec1"])
        rec.note_enc = rec.note_enc[:-1] + bytes([rec.note_enc[-1] ^ 1])
        db.commit()
    r = client.get(RECORDS, headers=world.h("patient01"))
    assert r.status_code == 503 and "감염" not in r.text and "고혈압" not in r.text
    assert "Traceback" not in r.text and "InvalidTag" not in r.text


def test_record_moved_to_other_patient_is_rejected(client, world, records):
    """다른 환자 행으로 암호문을 옮겨 붙여도(AAD 불일치) 복호화되지 않는다."""
    from app import models
    from shared.db import SessionLocal

    with SessionLocal() as db:
        rec = db.get(models.MedicalRecord, records["rec2"])  # C-2 의 기록
        rec.patient_id = world.ids["C-1"]
        db.commit()
    assert client.get(RECORDS, headers=world.h("patient01")).status_code == 503


def test_patient_without_records_gets_empty_list(client, world, medical_key):
    assert client.get(RECORDS, headers=world.h("patient01")).json() == []


# ---------- R12 ----------

def test_patient_sees_only_own_receipts(client, world, records):
    r = client.get(RECEIPTS, headers=world.h("patient01"))
    assert r.status_code == 200
    items = r.json()
    assert [(i["amount"], i["pay_status"]) for i in items] == [(8000, "UNPAID"), (15000, "PAID")]
    assert items[0]["paid_at"] is None and items[1]["paid_at"] == "2026-10-01T00:00:00.000Z"
    assert [i["amount"] for i in client.get(RECEIPTS, headers=world.h("patient02")).json()] == [30000]


def test_receipts_do_not_need_medical_key(client, world, records, monkeypatch):
    monkeypatch.delenv("MEDICAL_KEY")
    assert client.get(RECEIPTS, headers=world.h("patient01")).status_code == 200
