"""시간 규칙 (docs/api-spec.md 2장 "시간"): 저장·응답은 UTC, 날짜만 받는 값은 병원 기준 KST 날짜."""
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from pydantic import PlainSerializer

KST_OFFSET = timedelta(hours=9)


def _to_utc_iso(value: datetime) -> str:
    """UTC 시각을 '2026-10-05T01:00:00.000Z' 형식 문자열로 바꾼다."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


# 응답에서 시각을 위 형식으로 내보내는 자료형
UtcDateTime = Annotated[datetime, PlainSerializer(_to_utc_iso, return_type=str)]


def kst_day_range_utc(day: date) -> tuple[datetime, datetime]:
    """KST 하루 [00:00, 다음 날 00:00) 를 UTC 시각 범위로 (시간대 정보 없는 UTC 값)"""
    start = datetime(day.year, day.month, day.day) - KST_OFFSET
    return start, start + timedelta(days=1)


def kst_date_of(utc_naive: datetime) -> date:
    """UTC 시각이 속한 KST 날짜"""
    return (utc_naive + KST_OFFSET).date()


def utcnow_naive() -> datetime:
    """지금 UTC 시각 (시간대 정보 없는 값). '지금' 기준 검사(과거 예약 거절 등)는 모두 이 함수를 쓴다.

    다른 모듈은 `timeutil.utcnow_naive()` 로 부른다 (테스트가 시계를 고정할 수 있도록).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_kst() -> date:
    return (utcnow_naive() + KST_OFFSET).date()
