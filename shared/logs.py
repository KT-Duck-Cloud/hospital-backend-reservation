"""앱 로그: JSON 한 줄씩 표준 출력으로 남긴다 (WBS "로그 수집 구성"). 세 서비스가 같이 쓴다.

한 줄 모양 (요청 로그):
  {"ts": "2026-10-15T01:30:00.123Z", "service": "reservation", "level": "INFO", "logger": "access",
   "msg": "request", "request_id": "...", "method": "POST", "path": "/api/v1/reservations",
   "status": 201, "duration_ms": 12.3}

남기지 않는 것
- 비밀번호·토큰·요청/응답 본문·진료기록 본문·DB 접속 정보. 요청 로그에는 조회 조건(?q=환자이름)도 넣지 않는다 (경로만).
- 예외는 종류(exc_type)만 남긴다. 오류 문구·추적 정보에는 SQL·접속 주소가 섞일 수 있다.
- 그래도 섞여 들어온 경우를 대비해 마지막에 토큰·접속 주소 모양의 문자열을 [REDACTED] 로 가린다.
"""
import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

# 지금 처리 중인 요청의 request_id (shared/middleware.py 가 넣는다). 요청 밖이면 None.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

# 로그 한 줄에 그대로 옮기는 추가 칸 (logger.info(..., extra={...}))
EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "audit", "db_error", "kind", "mysql_code", "exc_type")

_REDACT_PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),                    # Authorization 헤더 값
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),     # JWT
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s@]+@"),          # 비밀번호가 든 접속 주소 (mysql+pymysql://u:p@)
    re.compile(r"(?i)(password|passwd|secret|token)(\"?\s*[:=]\s*\"?)[^\s\",}]+"),
]


def redact(text: str) -> str:
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + m.group(2) if m.lastindex == 2 else "") + "[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "service": self.service,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
        }
        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        if record.exc_info and record.exc_info[0] is not None and "exc_type" not in entry:
            entry["exc_type"] = record.exc_info[0].__name__
        return redact(json.dumps(entry, ensure_ascii=False, default=str))


class StdoutHandler(logging.StreamHandler):
    """쓸 때마다 지금의 sys.stdout 에 쓴다 (테스트의 capsys 가 바꾼 stdout 도 따라간다)."""

    def __init__(self):
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value):  # StreamHandler.__init__ 가 넣는 값은 쓰지 않는다
        pass


def setup_logging(service: str, level: int = logging.INFO) -> None:
    """루트·uvicorn 로그를 JSON 한 줄로 바꾼다. 앱을 불러올 때(uvicorn 이 자기 로그 설정을 한 뒤) 부른다."""
    handler = StdoutHandler()
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # uvicorn 기본 접속 로그는 조회 조건(?q=...)까지 찍으므로 끈다. 요청 로그는 미들웨어(access)가 남긴다.
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False
    access.disabled = True
