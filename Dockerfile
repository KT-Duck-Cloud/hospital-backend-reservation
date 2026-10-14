# syntax=docker/dockerfile:1
# 빌드는 저장소 루트에서 한다 (shared/ 를 함께 복사하기 위해):
#   docker build -t reservation-app-reservation:dev .

# ---- 빌드 단계: requirements.txt로 패키지를 가상환경에 설치 ----
FROM python:3.12.15-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && pip uninstall -y pip setuptools wheel

# ---- 실행 단계: 설치된 패키지와 앱 코드만 복사 ----
FROM python:3.12.15-slim

# 이미지 태그와 같은 Git 커밋 값을 빌드 때 받는다: --build-arg APP_VERSION=<commit>
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1

# DB_URL·DB_USERNAME·DB_PASSWORD, READ_ONLY_MODE는 일부러 넣지 않는다. 실행할 때 -e 로 주입한다.

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

# 실행에 필요 없는 패키지 도구(pip 등)는 제거한다. 앱은 /opt/venv 의 패키지만 쓴다.
RUN python -m pip uninstall -y pip setuptools wheel || true

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app shared ./shared
COPY --chown=app:app app ./app

USER app
EXPOSE 8000

# 인그레스(프록시) 뒤의 실제 클라이언트 IP: --proxy-headers 로 X-Forwarded-For 를 읽되,
# FORWARDED_ALLOW_IPS 에 있는 프록시에서 온 값만 믿는다 (기본 127.0.0.1 = 사실상 믿지 않음).
# sh -c 는 환경변수를 넣기 위한 것이고, exec 로 uvicorn 이 PID 1 이 되어 SIGTERM 을 직접 받는다.
# SIGTERM 을 받으면 새 요청을 멈추고 진행 중 요청을 마친 뒤 끝난다 (최대 20초, Helm terminationGracePeriodSeconds 30초 안).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 20 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
