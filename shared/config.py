"""환경변수를 읽어 오는 파일. 비밀 정보(DB 비밀번호 등)는 코드에 쓰지 않고 환경변수로만 받는다."""
import os

# 접속할 DB 주소. 환경변수로만 받는다. 기본값은 없다(비밀번호가 코드·이미지에 들어가면 안 되므로).
# 형식: mysql+pymysql://사용자:비밀번호@호스트:3306/DB이름   (실제 값은 실행할 때 주입한다)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("환경변수 DATABASE_URL이 설정되지 않았습니다.")

# DB TLS CA 인증서 파일 경로. 있으면 CA·서버 이름까지 검증하고, 없으면 암호화(TLS)만 한다 (로컬 compose 용).
DB_SSL_CA = os.getenv("DB_SSL_CA", "").strip()

# DB 연결 풀 (MySQL). Pod 하나가 DB 에 여는 연결은 최대 DB_POOL_SIZE + DB_MAX_OVERFLOW 개다.
# - DB_POOL_TIMEOUT: 풀이 다 찼을 때 빈 연결을 기다리는 시간(초). 넘으면 503 DB_UNAVAILABLE.
# - DB_POOL_RECYCLE: 이 시간(초)보다 오래된 연결은 버리고 새로 연다 (MySQL wait_timeout·방화벽 유휴 끊김보다 짧게).
# 연결을 꺼낼 때마다 살아 있는지 확인한다(pool_pre_ping). DB 가 재시작·승격돼도 다음 요청에서 새로 연결한다.
def _int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise RuntimeError(f"환경변수 {name} 는 정수여야 합니다.") from None
    if value < minimum:
        raise RuntimeError(f"환경변수 {name} 는 {minimum} 이상이어야 합니다.")
    return value


DB_POOL_SIZE = _int_env("DB_POOL_SIZE", 5, minimum=1)
DB_MAX_OVERFLOW = _int_env("DB_MAX_OVERFLOW", 5)
DB_POOL_TIMEOUT = _int_env("DB_POOL_TIMEOUT", 5, minimum=1)
DB_POOL_RECYCLE = _int_env("DB_POOL_RECYCLE", 1800, minimum=1)

# 읽기 전용 모드. true/1/yes(대소문자 무관)이면 쓰기 요청을 모두 503(WRITE_DISABLED)으로 막는다.
READ_ONLY_MODE = os.getenv("READ_ONLY_MODE", "false").strip().lower() in ("true", "1", "yes")

# 앱 버전. /healthz 응답에 포함된다.
APP_VERSION = os.getenv("APP_VERSION", "dev")

# JWT 서명 키 (HS256). 세 서비스가 같은 값을 쓴다. 환경변수(Secret)로만 받고 기본값은 없다.
# 비어 있거나 32바이트보다 짧으면 토큰을 쓰는 서비스가 시작할 때 멈춘다 (shared/auth.py).
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
# 토큰 발급자(iss). 발급·검증 모두 이 값과 같아야 한다.
JWT_ISSUER = os.getenv("JWT_ISSUER", "hospital-reservation").strip()

# bcrypt 비용. 운영 12. 테스트에서만 낮춘다.
BCRYPT_ROUNDS = int(os.getenv("BCRYPT_ROUNDS", "12"))

# 사이트 이름 (hospital / krdr / aws). 시스템 상태(S1) 표시용
SITE_NAME = os.getenv("SITE_NAME", "unknown").strip() or "unknown"
