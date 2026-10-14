"""테스트 계정 만들기 (docs/api-spec.md 6장). 로컬·테스트 DB 전용, 가상 데이터.

비밀번호는 코드·파일에 쓰지 않는다. 환경변수 SEED_PASSWORD 에서 읽어 bcrypt 로 해시한 값만 DB 에 넣는다.
모든 테스트 계정이 같은 비밀번호를 쓴다 (비밀번호는 Git 이 아닌 다른 경로로 전달).

DB 연결 (app_runtime 계정, TLS):
  - 환경변수 DATABASE_URL 이 있으면 그대로 쓴다.
  - 없으면 저장소 루트의 .env 에서 APP_RUNTIME_PASSWORD 를 읽어 DB_HOST(기본 127.0.0.1):DB_PORT(기본 3306) 로 붙는다.
    (docker compose 의 mysql 을 PC 에서 바로 쓰는 경우)

다시 실행해도 된다. 없는 계정은 새로 만들고, 이미 있는 계정은 비밀번호(password_hash)만 새 값으로 바꾼다.
(역할·소속·활성 여부·로그인 잠금 상태는 그대로 둔다. 전체가 한 트랜잭션이라 중간에 실패하면 아무것도 바뀌지 않는다.)

실행 (PowerShell, 저장소 루트):
  $s = Read-Host "SEED_PASSWORD" -AsSecureString
  $env:SEED_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
  .venv\\Scripts\\python scripts\\seed_users.py
  Remove-Item Env:SEED_PASSWORD
"""
import os
import sys
from pathlib import Path
from urllib.parse import quote

import bcrypt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MIN_PASSWORD_LENGTH = 10
BCRYPT_MAX_BYTES = 72  # bcrypt 는 72바이트까지만 쓴다. 넘으면 오류로 알린다.
BCRYPT_ROUNDS = 12

# (login_id, 이름, 역할, 부서 코드, 연결할 대상)
#   연결할 대상: ("patient", 차트번호) → users.patient_id, ("doctor", 의사 이름) → doctors.user_id
TEST_ACCOUNTS = [
    ("patient01",   "테스트환자",   "PATIENT",   None,  ("patient", "C2026-0001")),
    ("nurse_im01",  "내과간호사",   "NURSE",     "IM",  None),
    ("nurse_os01",  "정형간호사",   "NURSE",     "OS",  None),
    ("doctor_im01", "내과의사",     "DOCTOR",    "IM",  ("doctor", "김내과")),  # 6장 목록에 없던 DOCTOR 역할용
    ("admin01",     "관리자",       "ADMIN",     None,  None),
    ("office01",    "원무과장",     "MANAGER",   "ADM", None),
    ("head_im01",   "내과부서장",   "DEPT_HEAD", "IM",  None),
    ("director01",  "원장",         "DIRECTOR",  None,  None),
    ("dev01",       "개발자",       "DEVELOPER", None,  None),
    ("tester01",    "테스터",       "TESTER",    None,  None),
]


def read_dotenv_value(key: str) -> str:
    """.env 에서 값 하나만 읽는다 (화면에 출력하지 않음)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    password = read_dotenv_value("APP_RUNTIME_PASSWORD")
    if not password:
        sys.exit("DATABASE_URL 도 없고 .env 의 APP_RUNTIME_PASSWORD 도 없습니다. scripts/make_env.ps1 을 먼저 실행하세요.")
    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = os.environ.get("DB_PORT", "3306")
    return f"mysql+pymysql://app_runtime:{quote(password, safe='')}@{host}:{port}/hospital?charset=utf8mb4"


def read_seed_password() -> bytes:
    password = os.environ.get("SEED_PASSWORD", "")
    if not password:
        sys.exit("환경변수 SEED_PASSWORD 가 없습니다.")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"SEED_PASSWORD 는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.")
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        sys.exit(f"SEED_PASSWORD 는 {BCRYPT_MAX_BYTES}바이트 이하여야 합니다 (bcrypt 제한).")
    return encoded


def main() -> None:
    password = read_seed_password()
    # shared.db 는 import 할 때 DATABASE_URL 을 읽으므로 먼저 넣어 둔다. (TLS 설정을 앱과 똑같이 쓰기 위해)
    os.environ["DATABASE_URL"] = database_url()

    from sqlalchemy import text

    from shared.db import engine

    with engine.begin() as conn:  # 전부 성공하면 COMMIT, 하나라도 실패하면 ROLLBACK
        departments = dict(conn.execute(text("SELECT code, id FROM departments")).all())
        for login_id, name, role, dept_code, link in TEST_ACCOUNTS:
            department_id = None
            if dept_code:
                if dept_code not in departments:
                    sys.exit(f"부서 {dept_code} 가 없습니다. db/seed.sql 이 먼저 들어가야 합니다.")
                department_id = departments[dept_code]

            patient_id = None
            if link and link[0] == "patient":
                patient_id = conn.execute(
                    text("SELECT id FROM patients WHERE chart_no = :chart_no"), {"chart_no": link[1]}
                ).scalar()
                if patient_id is None:
                    sys.exit(f"환자 {link[1]} 가 없습니다. db/seed.sql 이 먼저 들어가야 합니다.")

            password_hash = bcrypt.hashpw(password, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")

            # 이미 있는 계정: 비밀번호만 바꾼다. 역할·소속·활성·잠금 상태는 건드리지 않는다.
            # FOR UPDATE: 확인과 변경 사이에 다른 실행이 끼어들지 않도록 행(없으면 그 자리)을 잠근다.
            user_id = conn.execute(
                text("SELECT id FROM users WHERE login_id = :login_id FOR UPDATE"), {"login_id": login_id}
            ).scalar()
            if user_id is not None:
                conn.execute(
                    text("UPDATE users SET password_hash = :password_hash WHERE id = :id"),
                    {"password_hash": password_hash, "id": user_id},
                )
                print(f"비밀번호 변경: {login_id}")
                continue

            # 없는 계정: 새로 만든다.
            user_id = conn.execute(
                text(
                    """
                    INSERT INTO users (login_id, password_hash, name, role, department_id, patient_id, is_active)
                    VALUES (:login_id, :password_hash, :name, :role, :department_id, :patient_id, TRUE)
                    """
                ),
                {"login_id": login_id, "password_hash": password_hash, "name": name, "role": role,
                 "department_id": department_id, "patient_id": patient_id},
            ).lastrowid

            if link and link[0] == "doctor":
                updated = conn.execute(
                    text("UPDATE doctors SET user_id = :user_id WHERE name = :name AND department_id = :department_id"),
                    {"user_id": user_id, "name": link[1], "department_id": department_id},
                ).rowcount
                if updated != 1:
                    sys.exit(f"의사 {link[1]} 를 찾지 못했습니다. db/seed.sql 이 먼저 들어가야 합니다.")

            print(f"새로 만듦: {login_id} ({role})")

    print(f"테스트 계정 {len(TEST_ACCOUNTS)}개 처리 완료. (비밀번호는 출력하지 않음)")


if __name__ == "__main__":
    main()
