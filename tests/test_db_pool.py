"""DB 연결 풀 설정 (shared/db.py): 환경변수로 크기·대기·재사용 시간을 정하고, pool_pre_ping 은 항상 켠다.

shared.config 는 불러올 때 환경변수를 읽으므로 따로 파이썬을 띄워 확인한다 (DB 에 접속하지 않는다).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = (
    "import json; from shared.db import engine as e; p = e.pool; "
    "print(json.dumps({'size': p.size(), 'overflow': p._max_overflow, 'timeout': p._timeout, "
    "'recycle': p._recycle, 'pre_ping': p._pre_ping}))"
)
FAKE_DB = {"DB_URL": "mysql://127.0.0.1:1/x", "DB_USERNAME": "u", "DB_PASSWORD": "p"}  # 만들기만 하고 접속하지 않는다


def probe(**env):
    full = {k: v for k, v in os.environ.items() if not k.startswith("DB_POOL") and k != "DB_MAX_OVERFLOW"}
    full.pop("DATABASE_URL", None)
    full.update(FAKE_DB, **env)
    r = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=full, capture_output=True, text=True)
    return r


def test_pool_defaults():
    r = probe()
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"size": 5, "overflow": 5, "timeout": 5, "recycle": 1800, "pre_ping": True}


def test_pool_from_env():
    r = probe(DB_POOL_SIZE="8", DB_MAX_OVERFLOW="2", DB_POOL_TIMEOUT="3", DB_POOL_RECYCLE="600")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"size": 8, "overflow": 2, "timeout": 3, "recycle": 600, "pre_ping": True}


@pytest.mark.parametrize("env", [{"DB_POOL_SIZE": "abc"}, {"DB_POOL_SIZE": "0"}, {"DB_POOL_TIMEOUT": "-1"}])
def test_pool_bad_env_stops_startup(env):
    r = probe(**env)
    assert r.returncode != 0 and "DB_POOL" in r.stderr
