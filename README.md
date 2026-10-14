# hospital-backend-reservation

병원 예약 시스템의 예약 서비스 (예약, 대기열, 환자, 진료기록). FastAPI 서비스이며 `shared/` 는 세 백엔드 저장소(auth · reservation · stats)에 같은 내용으로 들어 있다.

## 구조

```
app/            서비스 코드 (uvicorn app.main:app)
shared/         공통 코드 (DB, 인증, 로그, 설정 등)
tests/          pytest (임시 SQLite DB 로 돈다)
Dockerfile      이 폴더 하나로 빌드한다
```

## 테스트

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests
```

## 이미지 빌드 · 실행

```powershell
docker build --build-arg APP_VERSION=$(git rev-parse --short HEAD) -t hospital-backend-reservation:dev .
docker run --rm -p 8000:8000 -e DATABASE_URL=... -e JWT_SECRET=... hospital-backend-reservation:dev
```

DB 와 다른 서비스를 함께 띄우는 로컬 개발 환경은 `hospital-gitops` 의 `docker-compose.yml` 을 쓴다.

## 샘플 데이터 넣기

```powershell
.venv\Scripts\python scripts\seed_users.py     # 샘플 계정 (SEED_PASSWORD)
.venv\Scripts\python scripts\seed_records.py   # 샘플 진료기록 (MEDICAL_KEY, MEDICAL_KEY_VERSION)
```

DB 접속 정보는 `DATABASE_URL`, 없으면 `.env` 의 `APP_RUNTIME_PASSWORD` · `DB_HOST` · `DB_PORT` 를 쓴다.
(`seed_records.py` 가 `seed_users.py` 의 함수를 쓰므로 두 파일이 함께 들어 있다.)

## 선택 테스트

- 실제 MySQL: `TEST_MYSQL_URL` (`TEST_MYSQL_ADMIN_URL`) 을 주면 `@pytest.mark.mysql` 테스트가 돈다.
- 검증 도구 연동(test_data_verifier_e2e): `hospital-gitops` 를 옆 폴더에 clone 하거나 `DATA_VERIFIER_PATH` 로 `tools/data_verifier.py` 경로를 준다. 없으면 건너뛴다.
- DB 재시작(test_recovery): `RECOVERY_COMPOSE_SERVICE=mysql`, `RECOVERY_COMPOSE_DIR=..\hospital-gitops`.

## 환경변수

값(비밀번호·키)은 코드나 이미지에 넣지 않고 실행할 때 주입한다.

| 이름 | 필수 | 설명 |
|---|---|---|
| `DATABASE_URL` | 필수 | `mysql+pymysql://사용자:비밀번호@호스트:3306/hospital?charset=utf8mb4` |
| `JWT_SECRET` | 필수 | JWT 서명 키 (세 서비스 같은 값, 32바이트 이상) |
| `JWT_ISSUER` | | 토큰 발급자 (기본 `hospital-reservation`) |
| `DB_SSL_CA` | | DB TLS CA 인증서 경로 (없으면 암호화만, 검증 안 함) |
| `DB_POOL_SIZE` · `DB_MAX_OVERFLOW` · `DB_POOL_TIMEOUT` · `DB_POOL_RECYCLE` | | DB 연결 풀 (기본 5 · 5 · 5초 · 1800초) |
| `READ_ONLY_MODE` | | `true` 면 쓰기 요청을 503 으로 막음 |
| `SITE_NAME` | | 사이트 이름 (hospital / krdr / aws) |
| `APP_VERSION` | | 버전 표시 (이미지 빌드 때 `--build-arg APP_VERSION`) |
| `FORWARDED_ALLOW_IPS` | | 믿을 프록시 IP (기본 `127.0.0.1`) |
| `MEDICAL_KEY` | | 진료기록 암호화 키 (base64 32바이트). 없으면 내 진료기록(R11)만 503 |
| `MEDICAL_KEY_VERSION` | | 키 버전 (기본 1). 옛 키는 `MEDICAL_KEY_V<버전>` |

## 코드 원본

이 저장소의 코드는 `reservation-app` 에서 `scripts/sync_to_repos.ps1` 로 복사한다.
README.md · .gitignore · .gitattributes · .github/ 는 이 저장소에서 직접 관리한다 (복사 대상 아님).
