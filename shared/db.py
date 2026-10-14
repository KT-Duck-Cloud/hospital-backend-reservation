"""DB 연결을 만드는 파일. SQLAlchemy 공통 방식만 써서 SQLite/MySQL 모두 동작한다."""
import ssl

from sqlalchemy import URL, create_engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from shared import config
from shared.config import DB_SSL_CA

# 이 서비스의 DB 계정이 쓰기 권한을 가졌는지. stats 는 조회 전용 계정(app_readonly)이라 False 로 바꾼다.
_account_can_write = True


def use_read_only_account() -> None:
    """조회 전용 DB 계정을 쓰는 서비스(stats)가 시작할 때 부른다. 세션 갱신·감사 로그를 DB 에 쓰지 않는다."""
    global _account_can_write
    _account_can_write = False


def db_writable() -> bool:
    """지금 앱이 DB 에 써도 되는지: READ_ONLY_MODE(DR 대기)가 아니고, 쓰기 권한 있는 계정일 때만."""
    return _account_can_write and not config.READ_ONLY_MODE


def mysql_ssl_context(ca_path: str = DB_SSL_CA) -> ssl.SSLContext:
    """MySQL 연결용 TLS 설정.

    - DB_SSL_CA 가 있으면: 그 CA 로 인증서와 서버 이름(호스트명)을 검증한다. (운영)
    - 없으면: 암호화(TLS)만 하고 인증서는 검증하지 않는다. (로컬 compose: MySQL 이 자동으로 만든 인증서)
    DB 계정은 REQUIRE SSL 이라 TLS 없이 붙으면 서버가 거절한다.
    """
    if ca_path:
        context = ssl.create_default_context(cafile=ca_path)  # 검증 켜짐: CERT_REQUIRED + check_hostname
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def build_database_url(db_url: str, username: str, password: str) -> URL:
    """DB_URL(계정 없는 주소) + DB_USERNAME + DB_PASSWORD → SQLAlchemy 접속 정보.

    - mysql://호스트:3306/hospital → mysql+pymysql://, charset=utf8mb4 (없을 때만). 계정·비밀번호가 반드시 있어야 한다.
    - sqlite:///파일 (테스트) → 계정을 쓰지 않는다.
    돌려주는 URL 은 str()·repr() 해도 비밀번호가 *** 로 가려진다. 오류 메시지에는 값을 넣지 않는다.
    """
    try:
        url = make_url(db_url)
    except ArgumentError:
        raise RuntimeError("환경변수 DB_URL 형식이 잘못되었습니다. 예) mysql://호스트:3306/hospital") from None
    if url.username or url.password:
        raise RuntimeError("DB_URL 에는 계정을 넣지 않습니다. 계정은 DB_USERNAME·DB_PASSWORD 로 넣으세요.")
    backend = url.get_backend_name()
    if backend == "sqlite":
        return url
    if backend != "mysql":
        raise RuntimeError("DB_URL 은 mysql:// 또는 sqlite:// 만 지원합니다.")
    if not username or not password:
        raise RuntimeError("환경변수 DB_USERNAME·DB_PASSWORD 가 필요합니다.")
    query = {"charset": "utf8mb4", **url.query}
    return url.set(drivername="mysql+pymysql", username=username, password=password, query=query)


def build_connect_args(url: str | URL) -> dict:
    """DB 종류별 연결 옵션"""
    url = str(url)  # 비밀번호는 *** 로 가려진 문자열 (드라이버 이름만 본다)
    if url.startswith("sqlite"):
        # SQLite는 기본적으로 한 스레드에서만 쓰게 막혀 있어서, SQLite일 때만 옵션을 푼다.
        return {"check_same_thread": False}
    if url.startswith("mysql"):
        # TLS 연결 + 세션 시간대를 UTC 로 고정 (DATETIME 기본값 CURRENT_TIMESTAMP 가 UTC 가 되도록)
        return {"ssl": mysql_ssl_context(), "init_command": "SET time_zone = '+00:00'"}
    return {}


def build_pool_args(url: str | URL) -> dict:
    """연결 풀 설정 (shared/config.py 의 DB_POOL_*). SQLite(테스트)는 SQLAlchemy 기본 풀을 그대로 쓴다."""
    if str(url).startswith("sqlite"):
        return {}
    return {
        "pool_size": config.DB_POOL_SIZE,
        "max_overflow": config.DB_MAX_OVERFLOW,
        "pool_timeout": config.DB_POOL_TIMEOUT,
        "pool_recycle": config.DB_POOL_RECYCLE,
    }


# 엔진: DB와의 연결 통로 (만들 때는 접속하지 않는다)
# pool_pre_ping: 연결을 꺼낼 때 살아 있는지 확인하고, 죽었으면 버리고 새로 연다 (DB 재시작·승격 뒤 자동 재연결).
# hide_parameters: 오류 메시지·로그에 SQL 값(환자 정보 등)이 찍히지 않게 한다. echo 는 끈다 (요청 값 디버그 로그 금지).
ENGINE_URL = build_database_url(config.DB_URL, config.DB_USERNAME, config.DB_PASSWORD)
engine = create_engine(
    ENGINE_URL, connect_args=build_connect_args(ENGINE_URL), pool_pre_ping=True, hide_parameters=True, echo=False,
    **build_pool_args(ENGINE_URL),
)


def reset_pool() -> None:
    """풀에 쉬고 있는 연결을 모두 닫는다. 지금 쓰는 연결은 요청이 끝나면 버려지고, 다음 요청부터 새로 연결한다."""
    engine.dispose(close=True)

# 세션: DB에 읽고 쓰는 작업 단위 (COMMIT은 우리가 직접 호출한다)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """모든 테이블 모델이 상속받는 기본 클래스"""


def get_db():
    """요청 하나마다 세션을 열고, 끝나면 반드시 닫는다."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
