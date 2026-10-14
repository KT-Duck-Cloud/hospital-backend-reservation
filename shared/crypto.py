"""진료기록 암호화·복호화 (AES-256-GCM). ★ 보안 담당 검토 대상 파일 ★

이 파일만 암호 키를 다룬다. 진료기록 평문은 이 파일의 decrypt_field 를 통해서만 나온다.
KMS 로 옮길 때는 이 파일의 _load_key() 만 바꾼다 (부르는 쪽 코드는 그대로).

[알고리즘]
- AES-256-GCM (cryptography 패키지 AESGCM). 키 32바이트, nonce 12바이트, 인증 태그 16바이트.
- 저장 형식 (db/schema.sql medical_records):
    diagnosis_enc / note_enc       = 암호문 + 인증 태그 (AESGCM.encrypt 결과 그대로)
    diagnosis_nonce / note_nonce   = 그 칸을 암호화할 때 쓴 nonce (칸마다·암호화할 때마다 새로 만든다)
    key_version                    = 암호화에 쓴 키 버전
- nonce: os.urandom(12) 로 매번 새로 만든다. 같은 키로 nonce 를 다시 쓰면 GCM 이 깨지므로 재사용하지 않는다.
  (무작위 96비트 nonce 는 키 하나로 약 2^32 번 암호화까지 안전. 그 전에 키를 바꾼다.)
- AAD(추가 인증 데이터): "medical_records|<칸 이름>|<patient_id>|<key_version>"
  암호문을 다른 환자·다른 칸·다른 키 버전 행으로 옮겨 붙이면 복호화가 실패한다.

[키]
- 환경변수 MEDICAL_KEY: base64 로 쓴 32바이트 키 (Secret 으로 주입. 코드·이미지·로그에 넣지 않는다).
- 환경변수 MEDICAL_KEY_VERSION: 지금 키의 버전 (정수, 기본 1). 새로 암호화할 때 이 버전을 쓴다.
- 키 교체: 새 키를 MEDICAL_KEY·MEDICAL_KEY_VERSION 으로 올리고, 옛 키는 MEDICAL_KEY_V<버전> 으로 남겨 둔다.
  복호화는 행의 key_version 에 맞는 키를 찾는다. 다시 암호화(재암호화)는 별도 작업으로 한다.
- 키는 처음 쓸 때 읽고, 값은 어디에도 출력하지 않는다.

[실패]
- 키가 없거나 형식이 틀림 → CryptoUnavailable
- 인증 태그 불일치(암호문·nonce·AAD 변조, 다른 키) → CryptoIntegrityError
- 두 경우 모두 평문을 돌려주지 않는다. API 는 503 (shared/errors.py) 으로 응답하고 이유는 밝히지 않는다.

[부르는 쪽 규칙]
- decrypt_field 는 권한 검사(본인 확인)를 통과한 뒤에만 부른다.
- 복호화한 평문은 로그에 남기지 않는다. 진료기록 조회는 감사 로그 RECORD_VIEW 로 남긴다.
"""
import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
FIELDS = frozenset({"diagnosis", "note"})


class CryptoError(Exception):
    """진료기록 암호화·복호화를 할 수 없음. 메시지에 키·평문을 넣지 않는다."""


class CryptoUnavailable(CryptoError):
    """키가 없거나 형식이 틀림"""


class CryptoIntegrityError(CryptoError):
    """인증 태그 불일치 (변조·다른 키·다른 행에서 옮겨 온 암호문)"""


def current_key_version() -> int:
    raw = os.environ.get("MEDICAL_KEY_VERSION", "1").strip()
    try:
        version = int(raw)
    except ValueError:
        raise CryptoUnavailable("MEDICAL_KEY_VERSION 이 정수가 아닙니다") from None
    if version < 1:
        raise CryptoUnavailable("MEDICAL_KEY_VERSION 은 1 이상이어야 합니다")
    return version


def _load_key(version: int) -> bytes:
    """키 버전에 맞는 키. (KMS 로 바꿀 때 이 함수만 고친다)"""
    raw = os.environ.get(f"MEDICAL_KEY_V{version}", "").strip()
    if not raw and version == current_key_version():
        raw = os.environ.get("MEDICAL_KEY", "").strip()
    if not raw:
        raise CryptoUnavailable(f"키 버전 {version} 의 키가 없습니다")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise CryptoUnavailable(f"키 버전 {version} 의 키가 base64 가 아닙니다") from None
    if len(key) != KEY_BYTES:
        raise CryptoUnavailable(f"키 버전 {version} 의 키가 {KEY_BYTES}바이트가 아닙니다")
    return key


def _aad(field: str, patient_id: int, key_version: int) -> bytes:
    if field not in FIELDS:
        raise ValueError(f"암호화할 수 없는 칸: {field}")
    return f"medical_records|{field}|{int(patient_id)}|{int(key_version)}".encode("ascii")


def encrypt_field(plaintext: str, *, field: str, patient_id: int) -> tuple[bytes, bytes, int]:
    """평문 → (암호문+태그, nonce, key_version). 지금 키 버전으로 암호화한다."""
    version = current_key_version()
    nonce = os.urandom(NONCE_BYTES)  # 매번 새 nonce (재사용 금지)
    ciphertext = AESGCM(_load_key(version)).encrypt(nonce, plaintext.encode("utf-8"),
                                                    _aad(field, patient_id, version))
    return ciphertext, nonce, version


def decrypt_field(ciphertext: bytes, nonce: bytes, *, field: str, patient_id: int, key_version: int) -> str:
    """(암호문+태그, nonce) → 평문. 실패하면 CryptoError (평문을 돌려주지 않는다)."""
    key = _load_key(key_version)
    if not isinstance(nonce, (bytes, bytearray)) or len(nonce) != NONCE_BYTES:
        raise CryptoIntegrityError("nonce 길이가 맞지 않습니다")
    try:
        plaintext = AESGCM(key).decrypt(bytes(nonce), bytes(ciphertext), _aad(field, patient_id, key_version))
    except InvalidTag:
        raise CryptoIntegrityError("인증 태그가 맞지 않습니다") from None
    return plaintext.decode("utf-8")
