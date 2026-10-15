"""전화번호 공개 범위 (보안 담당 확정). 화면이 아니라 서버 응답을 만들 때 여기서 정한다.

공개 범위
- PATIENT : 본인 번호 + 부서 대표 번호
- MANAGER : 모든 환자 번호 + 직원 업무용 연락처
- NURSE   : 자기 부서에 (취소 아닌) 예약이 있는 환자 번호 + 원무과(ADM) 대표 번호 + 자기 부서 직원 업무용 연락처
- DEPT_HEAD: 자기 부서 직원 업무용 연락처 (환자 번호·부서 대표 번호는 가림)
- DOCTOR  : 자기 부서 부서장의 업무용 연락처만 전체 번호 (환자 번호·부서 대표 번호는 가림)
- DIRECTOR: 부서장 범위(자기 부서 직원) + 모든 부서 부서장의 업무용 연락처 (환자 번호·부서 대표 번호는 가림)
- 그 밖의 역할(ADMIN, DEVELOPER, TESTER)과 위에 없는 번호: 010-****-1234 로 가림
- 병원 대표 번호(HOSPITAL_PHONE)는 병원이 공개하는 번호라 로그인한 모든 역할에 그대로 보인다

전체 번호를 응답에 넣는 곳은 이 파일의 함수를 거친 값뿐이다. 감사 로그(target_id 는 번호가 아닌 id)와
앱 로그(경로만 남김, shared/logs.py)에는 번호를 넣지 않는다.
"""
from shared.permissions import DEPT_HEAD, DIRECTOR, DOCTOR, MANAGER, NURSE, PATIENT

ADM_CODE = "ADM"  # 원무과 부서 코드 (db/seed.sql)


def mask_phone(phone: str | None) -> str | None:
    """010-1234-5678 → 010-****-5678. 형식이 다르면 끝 4자리만 남기고 숫자를 가린다."""
    if not phone:
        return None
    parts = phone.split("-")
    if len(parts) == 3:
        return f"{parts[0]}-{'*' * len(parts[1])}-{parts[2]}"
    digits = sum(c.isdigit() for c in phone)
    seen = 0
    out = []
    for c in phone:
        if c.isdigit():
            seen += 1
            out.append(c if seen > digits - 4 else "*")
        else:
            out.append(c)
    return "".join(out)


def shown(phone: str | None, visible: bool) -> str | None:
    """공개 대상이면 그대로, 아니면 가려서."""
    return phone if visible else mask_phone(phone)


def can_see_patient_phone(role: str, own_patient_id: int | None, patient_id: int,
                          nurse_patient_ids: frozenset[int] | set[int] = frozenset()) -> bool:
    """환자 번호 공개 여부. nurse_patient_ids: 간호사 자기 부서에 예약이 있는 환자 id 들 (reservation 서비스가 조회)."""
    if role == PATIENT:
        return own_patient_id is not None and own_patient_id == patient_id
    if role == MANAGER:
        return True
    if role == NURSE:
        return patient_id in nurse_patient_ids
    return False


def can_see_department_phone(role: str, department_code: str) -> bool:
    """부서 대표 번호 공개 여부: 환자는 전부, 간호사는 원무과만."""
    if role == PATIENT:
        return True
    return role == NURSE and department_code == ADM_CODE


def can_see_staff_phone(role: str, viewer_department_id: int | None, staff_department_id: int | None,
                        staff_role: str | None = None) -> bool:
    """직원 업무용 연락처 공개 여부.

    원무과장은 전부, 간호사·부서장은 자기 부서 직원만, 의사는 자기 부서 부서장만,
    원장은 부서장 범위(자기 부서 직원) + 모든 부서 부서장.
    """
    if role == MANAGER:
        return True
    same_department = viewer_department_id is not None and viewer_department_id == staff_department_id
    if role in (NURSE, DEPT_HEAD):
        return same_department
    if role == DOCTOR:
        return same_department and staff_role == DEPT_HEAD
    if role == DIRECTOR:
        return same_department or staff_role == DEPT_HEAD
    return False
