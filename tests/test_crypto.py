"""shared/crypto.py (진료기록 AES-256-GCM) 단위 테스트. 키는 테스트마다 무작위로 만든다."""
import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import crypto  # noqa: E402


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(autouse=True)
def key(monkeypatch):
    for name in list(os.environ):
        if name.startswith("MEDICAL_KEY"):
            monkeypatch.delenv(name)
    k = new_key()
    monkeypatch.setenv("MEDICAL_KEY", k)
    return k


def enc(text="고혈압 추적 관찰", field="diagnosis", patient_id=7):
    return crypto.encrypt_field(text, field=field, patient_id=patient_id)


def dec(ct, nonce, version, field="diagnosis", patient_id=7):
    return crypto.decrypt_field(ct, nonce, field=field, patient_id=patient_id, key_version=version)


def test_round_trip_and_format():
    ct, nonce, version = enc()
    assert version == 1 and len(nonce) == 12
    assert len(ct) == len("고혈압 추적 관찰".encode()) + 16  # 암호문 + 16바이트 인증 태그
    assert "고혈압".encode() not in ct                       # 평문이 그대로 들어 있지 않다
    assert dec(ct, nonce, version) == "고혈압 추적 관찰"


def test_nonce_is_new_every_time():
    results = [enc("같은 내용") for _ in range(50)]
    assert len({n for _, n, _ in results}) == 50
    assert len({c for c, _, _ in results}) == 50  # 같은 평문도 암호문이 매번 다르다


@pytest.mark.parametrize("tamper", ["ciphertext", "tag", "nonce"])
def test_tampering_is_detected(tamper):
    ct, nonce, version = enc()
    if tamper == "ciphertext":
        ct = bytes([ct[0] ^ 1]) + ct[1:]
    elif tamper == "tag":
        ct = ct[:-1] + bytes([ct[-1] ^ 1])
    else:
        nonce = bytes([nonce[0] ^ 1]) + nonce[1:]
    with pytest.raises(crypto.CryptoIntegrityError):
        dec(ct, nonce, version)


def test_ciphertext_cannot_move_to_other_patient_or_field():
    ct, nonce, version = enc(patient_id=7, field="diagnosis")
    with pytest.raises(crypto.CryptoIntegrityError):
        dec(ct, nonce, version, patient_id=8)          # 다른 환자 행에 붙임
    with pytest.raises(crypto.CryptoIntegrityError):
        dec(ct, nonce, version, field="note")          # 다른 칸에 붙임


def test_wrong_key_is_integrity_error(monkeypatch):
    ct, nonce, version = enc()
    monkeypatch.setenv("MEDICAL_KEY", new_key())
    with pytest.raises(crypto.CryptoIntegrityError):
        dec(ct, nonce, version)


@pytest.mark.parametrize("bad_key", ["", "not-base64!!", base64.b64encode(os.urandom(16)).decode()])
def test_missing_or_bad_key_is_unavailable(monkeypatch, bad_key):
    ct, nonce, version = enc()
    monkeypatch.setenv("MEDICAL_KEY", bad_key)
    with pytest.raises(crypto.CryptoUnavailable):
        dec(ct, nonce, version)
    with pytest.raises(crypto.CryptoUnavailable):
        enc()


def test_bad_nonce_length():
    ct, nonce, version = enc()
    with pytest.raises(crypto.CryptoIntegrityError):
        dec(ct, nonce[:8], version)


def test_key_rotation_reads_old_rows(monkeypatch, key):
    old_ct, old_nonce, old_version = enc("옛 키로 저장")
    # 새 키 v2 로 교체, 옛 키는 MEDICAL_KEY_V1 로 남김
    monkeypatch.setenv("MEDICAL_KEY_V1", key)
    monkeypatch.setenv("MEDICAL_KEY", new_key())
    monkeypatch.setenv("MEDICAL_KEY_VERSION", "2")
    new_ct, new_nonce, new_version = enc("새 키로 저장")
    assert (old_version, new_version) == (1, 2)
    assert dec(old_ct, old_nonce, old_version) == "옛 키로 저장"
    assert dec(new_ct, new_nonce, new_version) == "새 키로 저장"
    # 옛 키를 빼면 옛 행은 읽을 수 없다 (평문 대신 오류)
    monkeypatch.delenv("MEDICAL_KEY_V1")
    with pytest.raises(crypto.CryptoUnavailable):
        dec(old_ct, old_nonce, old_version)


def test_key_version_mismatch_is_rejected(monkeypatch):
    ct, nonce, _ = enc()
    with pytest.raises(crypto.CryptoError):
        dec(ct, nonce, 2)  # v2 키 없음 → 사용 불가
    monkeypatch.setenv("MEDICAL_KEY_V2", os.environ["MEDICAL_KEY"])
    with pytest.raises(crypto.CryptoIntegrityError):
        dec(ct, nonce, 2)  # 키가 같아도 AAD 의 버전이 달라 실패


def test_errors_never_contain_key_or_plaintext(monkeypatch, key):
    ct, nonce, version = enc("비밀 진단명")
    monkeypatch.setenv("MEDICAL_KEY", new_key())
    with pytest.raises(crypto.CryptoError) as exc:
        dec(ct, nonce, version)
    assert key not in str(exc.value) and "비밀 진단명" not in str(exc.value)


def test_unknown_field_is_refused():
    with pytest.raises(ValueError):
        crypto.encrypt_field("x", field="ssn", patient_id=1)
