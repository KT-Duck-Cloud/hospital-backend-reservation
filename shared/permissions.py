"""권한 표: (메서드, API 경로) → 허용 역할. 보안 담당은 이 파일만 고치면 권한이 바뀐다.

규칙
- 경로는 FastAPI 에 등록한 모양 그대로 쓴다 (경로 변수 이름까지 같게). 예: "/api/v1/admin/users/{user_id}"
- 표에 없는 API 는 기본 403 (shared/auth.py 의 enforce_permissions).
- PUBLIC: 로그인 없이 호출 가능. LOGGED_IN: 로그인한 모든 역할.
- 운영 주소(/healthz, /readyz, /metrics)는 /api 밖이라 이 표와 관계없다 (인증 없음).
- 역할별 범위(본인·자기 부서만 등)는 이 표가 아니라 각 API 안에서 따로 검사한다.
기준 문서: docs/api-spec.md 3장 "허용 역할"
"""
PATIENT = "PATIENT"
NURSE = "NURSE"
DOCTOR = "DOCTOR"
MANAGER = "MANAGER"          # 원무과장
ADMIN = "ADMIN"
DEPT_HEAD = "DEPT_HEAD"
DIRECTOR = "DIRECTOR"        # 원장 (모든 권한)
DEVELOPER = "DEVELOPER"
TESTER = "TESTER"

ALL_ROLES = frozenset({PATIENT, NURSE, DOCTOR, MANAGER, ADMIN, DEPT_HEAD, DIRECTOR, DEVELOPER, TESTER})

PUBLIC = "PUBLIC"
LOGGED_IN = ALL_ROLES

PERMISSIONS: dict[tuple[str, str], frozenset[str] | str] = {
    # ---- 인증 서비스 (auth) ----
    ("POST", "/api/v1/auth/login"): PUBLIC,                                       # A1
    ("GET", "/api/v1/auth/me"): LOGGED_IN,                                        # A2
    ("POST", "/api/v1/auth/logout"): LOGGED_IN,                                   # A7
    ("GET", "/api/v1/admin/users"): frozenset({ADMIN, DIRECTOR, TESTER}),         # A3
    ("POST", "/api/v1/admin/users"): frozenset({ADMIN, DIRECTOR}),                # A4
    ("PATCH", "/api/v1/admin/users/{user_id}"): frozenset({ADMIN, DIRECTOR}),     # A5
    ("GET", "/api/v1/admin/audit-logs"): frozenset({ADMIN, DIRECTOR}),            # A6
    # ---- 예약 서비스 (reservation) ----
    # 역할별 범위: PATIENT 본인만, NURSE·DEPT_HEAD 자기 부서만, DIRECTOR·TESTER 전체 (예약 서비스 안에서 검사)
    ("POST", "/api/v1/reservations"): frozenset({PATIENT, NURSE, DIRECTOR, TESTER}),                   # R1
    # MANAGER(원무과장): 부서와 상관없이 예약 조회·접수·취소 (보안 담당 승인). 응답에 예약 메모·진단·진료 메모는 없다.
    ("GET", "/api/v1/reservations"): frozenset({PATIENT, NURSE, DEPT_HEAD, MANAGER, DIRECTOR, TESTER}),  # R2
    ("GET", "/api/v1/reservations/{reservation_id}"):
        frozenset({PATIENT, NURSE, DEPT_HEAD, MANAGER, DIRECTOR, TESTER}),                               # R3
    ("DELETE", "/api/v1/reservations/{reservation_id}"):
        frozenset({PATIENT, NURSE, MANAGER, DIRECTOR, TESTER}),                                          # R4
    ("POST", "/api/v1/reservations/{reservation_id}/checkin"):
        frozenset({NURSE, MANAGER, DIRECTOR, TESTER}),                                                   # R5
    # 진료 시작·완료: 담당 의사만 (그 예약의 doctor_id 가 로그인 의사와 같은지는 예약 서비스 안에서 검사)
    ("POST", "/api/v1/reservations/{reservation_id}/start-treatment"): frozenset({DOCTOR}),             # R15
    ("POST", "/api/v1/reservations/{reservation_id}/complete-treatment"): frozenset({DOCTOR}),          # R16
    ("GET", "/api/v1/doctor/queue"): frozenset({DOCTOR}),                                               # R17 의사 본인의 대기열
    ("GET", "/api/v1/departments"): LOGGED_IN,                                                          # R6
    ("GET", "/api/v1/doctors"): LOGGED_IN,                                                              # R7
    ("GET", "/api/v1/queue"): frozenset({NURSE, DEPT_HEAD, DIRECTOR, TESTER}),                          # R8
    # R9·R10: MANAGER 는 부서와 상관없이 환자 검색·상세 (번호 공개 범위는 shared/phone.py, 예약 메모는 가림)
    ("GET", "/api/v1/patients"): frozenset({NURSE, MANAGER, DIRECTOR, TESTER}),                         # R9
    # R10: PATIENT 는 본인(patient_id)만 — 본인 번호를 보는 곳 (다른 환자는 예약 서비스 안에서 403)
    ("GET", "/api/v1/patients/{patient_id}"): frozenset({PATIENT, NURSE, MANAGER, DIRECTOR, TESTER}),   # R10
    ("GET", "/api/v1/staff"): frozenset({NURSE, MANAGER}),                                              # R18 직원 연락처
    # R19 연락처 화면: 병원 대표 번호 + 진료과 대표 번호 + 역할별 직원 연락처 (공개 범위는 shared/phone.py)
    ("GET", "/api/v1/contacts"): LOGGED_IN,                                                             # R19
    ("POST", "/api/v1/patients"): frozenset({NURSE, ADMIN, DIRECTOR, TESTER}),                          # R14 환자 등록 (7단계 추가)
    ("GET", "/api/v1/slots"): LOGGED_IN,                                                                # R13 예약 가능 시간 (7단계 추가)
    ("GET", "/api/v1/me/records"): frozenset({PATIENT}),                                                # R11
    ("GET", "/api/v1/me/receipts"): frozenset({PATIENT}),                                               # R12
    # ---- 현황 서비스 (stats) ----
    ("GET", "/api/v1/system/status"): frozenset({DIRECTOR, DEVELOPER, TESTER}),                         # S1
    ("GET", "/api/v1/stats/departments"): frozenset({DEPT_HEAD, DIRECTOR}),                             # S2
    ("GET", "/api/v1/stats/doctors"): frozenset({DEPT_HEAD, DIRECTOR}),                                 # S3
    ("GET", "/api/v1/stats/hospital"): frozenset({DIRECTOR}),                                           # S4
}


def allowed_roles(method: str, path: str) -> frozenset[str] | str | None:
    """허용 역할. PUBLIC 이면 "PUBLIC", 표에 없으면 None (→ 403)."""
    return PERMISSIONS.get((method.upper(), path))


def _check_table() -> None:
    """오타로 없는 역할을 적으면 서비스가 시작할 때 바로 알 수 있게 한다."""
    for key, roles in PERMISSIONS.items():
        if roles == PUBLIC:
            continue
        unknown = set(roles) - ALL_ROLES
        if unknown:
            raise ValueError(f"권한 표 {key} 에 없는 역할이 있습니다: {sorted(unknown)}")


_check_table()
