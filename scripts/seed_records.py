"""샘플 진료기록 넣기 (가상 데이터). 진단·소견은 shared/crypto.py 로 AES-256-GCM 암호화해서만 저장한다.

1) patient01 계정에 연결된 환자의 시연 데이터: 지난 진료 완료(COMPLETED) 예약 2건 + 그 진료기록 2건 + 영수증 2건
   + 앞으로의 예약 1건 (db/seed.sql 의 이 환자 예약은 모두 취소라서 따로 넣는다. scripts/seed_users.py 를 먼저 실행해야 한다.)
2) db/seed.sql 의 접수된 예약(CHECKED_IN)마다 진료기록 1건을 넣고, 그 예약을 진료완료(COMPLETED)로 바꾼다.
3) 로그인 계정이 연결된 의사(doctors.user_id, 예: doctor_im01)마다 오늘(KST) 담당 예약 3건: 2건은 접수(CHECKED_IN,
   대기번호 포함), 1건은 접수 전. 의사 화면에 바로 대기 환자가 보인다. request_id 에 의사·날짜·순번이 들어가서 같은 날
   다시 실행해도 쌓이지 않고, 다음 날 실행하면 그날 몫이 새로 생긴다 (이 예약은 2번의 진료기록 만들기에서 제외).
이미 있는 것(같은 request_id 예약, 진료기록·영수증이 이미 있는 예약)은 건너뛴다 (다시 실행해도 쌓이지 않는다).

새 규칙 (db/schema.sql)
  - 대기번호는 queue_date(KST 날짜)와 함께 저장한다: UNIQUE(department_id, queue_date, queue_no)
  - 진료기록·영수증의 환자는 항상 그 예약의 환자 (외래키 (reservation_id, patient_id) → reservations (id, patient_id))
  - 진료기록이 있는 예약은 진료완료(COMPLETED). 예약 1건에 진료기록·영수증은 각각 최대 1건

필요한 값 (화면에 출력하지 않는다)
  - MEDICAL_KEY (base64 32바이트), MEDICAL_KEY_VERSION (기본 1): 환경변수, 없으면 저장소 루트 .env
  - DB: 환경변수 DB_URL·DB_USERNAME·DB_PASSWORD, 없으면 127.0.0.1:3306 · app_runtime · .env 의 APP_RUNTIME_PASSWORD (TLS)
  앱(예약 서비스)과 같은 MEDICAL_KEY 를 써야 R11 에서 복호화된다.

실행 (PowerShell, 저장소 루트):  .venv\\Scripts\\python scripts\\seed_records.py
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from seed_users import read_dotenv_value, set_db_env  # noqa: E402  (같은 .env·DB 연결 규칙)

# 가상 진단·소견 (진료과 코드별)
SAMPLES = {
    "IM": [("급성 상기도 감염", "해열제 3일 처방, 수분 섭취 권고"), ("고혈압 추적 관찰", "혈압 기록 지참하여 4주 후 재진"),
           ("위식도 역류", "식후 바로 눕지 않도록 안내")],
    "OS": [("요추 염좌", "물리치료 주 2회, 무거운 물건 들지 않기"), ("발목 인대 부분 손상", "압박 붕대, 2주 후 재진")],
    "PD": [("급성 중이염", "항생제 5일, 3일 후 경과 확인"), ("영유아 건강검진", "성장 곡선 정상 범위")],
}
DEFAULT_SAMPLE = ("정기 진료", "특이 소견 없음")

# patient01 계정에 연결된 환자의 시연용 데이터 (db/seed.sql 의 이 환자 예약은 모두 취소라 보여 줄 것이 없다).
# request_id 로 이미 있는지 확인하므로 다시 실행해도 쌓이지 않는다. 날짜는 처음 실행한 날(KST) 기준.
DOCTOR_DEMO_PREFIX = "demo-doctor-"
DOCTOR_DEMO_COUNT = 3        # 의사 1명당 오늘 예약 수
DOCTOR_DEMO_CHECKED_IN = 2   # 그중 접수까지 해 두는 수
DEMO_LOGIN_ID = "patient01"
DEMO = [
    # (request_id, 진료과 코드, 오늘 기준 며칠, 초진/재진, 진단, 소견, 영수증 금액)  진단 None = 앞으로의 예약
    ("demo-patient01-past-1", "IM", -7, "FIRST", "급성 기관지염", "기침약 5일 처방, 1주 후 경과 확인", 12000),
    ("demo-patient01-past-2", "OS", -3, "FIRST", "손목 염좌", "보호대 착용 2주, 무리한 사용 피하기", 35000),
    ("demo-patient01-next-1", "IM", 5, "RETURN", None, None, None),
]
SLOT_MINUTES = 30
FIRST_SLOT_KST_HOUR = 10  # 10:00 부터 빈 칸을 찾는다 (17:00 시작 칸까지)


def _free_slot(conn, doctor_id: int, day_start_utc):
    """그 의사의 그날 BOOKED 예약과 겹치지 않는 첫 30분 칸 (예약 서비스의 겹침 규칙과 같음)."""
    booked = [r[0] for r in conn.execute(
        text("SELECT reserved_at FROM reservations WHERE doctor_id = :d AND status = 'BOOKED' "
             "AND reserved_at >= :s AND reserved_at < :e"),
        {"d": doctor_id, "s": day_start_utc, "e": day_start_utc + timedelta(days=1)})]
    slot = timedelta(minutes=SLOT_MINUTES)
    start = day_start_utc + timedelta(hours=FIRST_SLOT_KST_HOUR)
    last = day_start_utc + timedelta(hours=17)
    while start <= last:
        if all(abs(b - start) >= slot for b in booked):
            return start
        start += slot
    sys.exit("시연용 예약을 넣을 빈 시간이 없습니다.")


def seed_demo_patient(conn, crypto) -> tuple[int, int, int]:
    """patient01 환자에게: 지난 접수 완료 예약 2건 + 그 진료기록 2건(암호화) + 영수증 2건 + 앞으로의 예약 1건.

    이미 있는 것(같은 request_id 예약, 같은 예약의 진료기록·영수증)은 건너뛴다. 넣은 (예약, 진료기록, 영수증) 수를 돌려준다.
    """
    from shared.timeutil import kst_date_of, kst_day_range_utc, today_kst

    found = conn.execute(text("SELECT id, patient_id FROM users WHERE login_id = :l"), {"l": DEMO_LOGIN_ID}).first()
    if found is None or found.patient_id is None:
        sys.exit(f"{DEMO_LOGIN_ID} 계정(환자 연결)이 없습니다. scripts/seed_users.py 를 먼저 실행하세요.")
    user_id, patient_id = found.id, found.patient_id
    added = [0, 0, 0]

    for request_id, dept_code, day_offset, visit_type, diagnosis, note, amount in DEMO:
        reservation = conn.execute(
            text("SELECT id, checked_in_at FROM reservations WHERE request_id = :r"), {"r": request_id}).first()
        if reservation is None:
            dept_id, doctor_id = conn.execute(text(
                "SELECT d.id, MIN(doc.id) FROM departments d JOIN doctors doc ON doc.department_id = d.id "
                "AND doc.is_active WHERE d.code = :c GROUP BY d.id"), {"c": dept_code}).one()
            day_start, _ = kst_day_range_utc(today_kst() + timedelta(days=day_offset))
            reserved_at = _free_slot(conn, doctor_id, day_start)
            past = diagnosis is not None
            checked_in_at = reserved_at - timedelta(minutes=10) if past else None
            queue_no = queue_date = None
            if past:  # 그날(KST) 그 진료과의 다음 대기번호 (UNIQUE(department_id, queue_date, queue_no) 를 지킨다)
                queue_date = kst_date_of(reserved_at)
                queue_no = (conn.execute(text(
                    "SELECT COALESCE(MAX(queue_no), 0) FROM reservations WHERE department_id = :d "
                    "AND queue_date = :qd"), {"d": dept_id, "qd": queue_date}).scalar() + 1)
            created = reserved_at - timedelta(days=3)
            conn.execute(text(
                "INSERT INTO reservations (request_id, patient_id, department_id, doctor_id, queue_no, queue_date, "
                "reserved_at, visit_type, status, checkin_status, checked_in_at, memo, created_by, created_at, "
                "updated_at) "
                "VALUES (:r, :p, :d, :doc, :q, :qd, :at, :v, 'BOOKED', :ci, :cia, :memo, :u, :c, :c)"),
                {"r": request_id, "p": patient_id, "d": dept_id, "doc": doctor_id, "q": queue_no, "qd": queue_date,
                 "at": reserved_at, "v": visit_type,
                 "ci": "COMPLETED" if past else "NOT_CHECKED_IN",  # 진료기록이 있는 지난 예약 = 진료완료
                 "cia": checked_in_at, "memo": "시연용 예약", "u": user_id, "c": created})
            reservation = conn.execute(
                text("SELECT id, checked_in_at FROM reservations WHERE request_id = :r"), {"r": request_id}).one()
            added[0] += 1
        if diagnosis is None:
            continue

        reservation_id, checked_in_at = reservation
        has_record = conn.execute(text("SELECT 1 FROM medical_records WHERE reservation_id = :r"),
                                  {"r": reservation_id}).first()
        if not has_record:
            d_enc, d_nonce, version = crypto.encrypt_field(diagnosis, field="diagnosis", patient_id=patient_id)
            n_enc, n_nonce, _ = crypto.encrypt_field(note, field="note", patient_id=patient_id)
            doctor_id = conn.execute(text("SELECT doctor_id FROM reservations WHERE id = :r"),
                                     {"r": reservation_id}).scalar()
            conn.execute(text(
                "INSERT INTO medical_records (patient_id, reservation_id, doctor_id, diagnosis_enc, note_enc, "
                "diagnosis_nonce, note_nonce, key_version, created_at, updated_at) "
                "VALUES (:p, :r, :doc, :de, :ne, :dn, :nn, :v, :c, :c)"),
                {"p": patient_id, "r": reservation_id, "doc": doctor_id, "de": d_enc, "ne": n_enc, "dn": d_nonce,
                 "nn": n_nonce, "v": version, "c": checked_in_at + timedelta(minutes=20)})
            added[1] += 1
        has_receipt = conn.execute(text("SELECT 1 FROM receipts WHERE reservation_id = :r"),
                                   {"r": reservation_id}).first()
        if not has_receipt:
            paid_at = checked_in_at + timedelta(minutes=40)
            conn.execute(text(
                "INSERT INTO receipts (patient_id, reservation_id, amount, pay_status, paid_at, created_at, updated_at) "
                "VALUES (:p, :r, :a, 'PAID', :t, :t, :t)"),
                {"p": patient_id, "r": reservation_id, "a": amount, "t": paid_at})
            added[2] += 1
    return tuple(added)


def seed_doctor_demo(conn) -> tuple[int, int]:
    """로그인 계정이 있는 의사마다 오늘(KST) 담당 예약 DOCTOR_DEMO_COUNT 건 (앞의 DOCTOR_DEMO_CHECKED_IN 건은 접수).

    이미 있는 것(같은 request_id)은 건너뛴다. 넣은 (의사 수, 예약 수)를 돌려준다.
    """
    from shared.models import utcnow
    from shared.timeutil import kst_date_of, kst_day_range_utc, today_kst

    doctors = conn.execute(text(
        "SELECT doc.id, doc.department_id, doc.user_id FROM doctors doc JOIN users u ON u.id = doc.user_id "
        "WHERE doc.is_active AND u.is_active ORDER BY doc.id")).all()
    patients = [r[0] for r in conn.execute(text("SELECT id FROM patients ORDER BY id LIMIT 30"))]
    if not doctors or not patients:
        return 0, 0
    today = today_kst()
    day_start, _ = kst_day_range_utc(today)
    added = 0
    for d_index, (doctor_id, dept_id, user_id) in enumerate(doctors):
        for n in range(DOCTOR_DEMO_COUNT):
            request_id = f"{DOCTOR_DEMO_PREFIX}{doctor_id}-{today:%Y%m%d}-{n + 1}"
            if conn.execute(text("SELECT 1 FROM reservations WHERE request_id = :r"), {"r": request_id}).first():
                continue
            reserved_at = _free_slot(conn, doctor_id, day_start)
            queue_no = queue_date = checked_in_at = None
            status = "NOT_CHECKED_IN"
            if n < DOCTOR_DEMO_CHECKED_IN:  # 오늘(KST) 그 진료과의 다음 대기번호 (UNIQUE(department_id, queue_date, queue_no))
                status, queue_date, checked_in_at = "CHECKED_IN", kst_date_of(reserved_at), utcnow()
                queue_no = conn.execute(text(
                    "SELECT COALESCE(MAX(queue_no), 0) + 1 FROM reservations WHERE department_id = :d "
                    "AND queue_date = :qd"), {"d": dept_id, "qd": queue_date}).scalar()
            now = utcnow()
            conn.execute(text(
                "INSERT INTO reservations (request_id, patient_id, department_id, doctor_id, queue_no, queue_date, "
                "reserved_at, visit_type, status, checkin_status, checked_in_at, memo, created_by, created_at, "
                "updated_at) VALUES (:r, :p, :d, :doc, :q, :qd, :at, 'RETURN', 'BOOKED', :ci, :cia, :memo, :u, "
                ":now, :now)"),
                {"r": request_id, "p": patients[(d_index * DOCTOR_DEMO_COUNT + n) % len(patients)], "d": dept_id,
                 "doc": doctor_id, "q": queue_no, "qd": queue_date, "at": reserved_at, "ci": status,
                 "cia": checked_in_at, "memo": "시연용 예약 (의사 화면)", "u": user_id, "now": now})
            added += 1
    return len(doctors), added


def main() -> None:
    for name in ("MEDICAL_KEY", "MEDICAL_KEY_VERSION"):
        if not os.environ.get(name):
            value = read_dotenv_value(name)
            if value:
                os.environ[name] = value
    set_db_env()

    from shared import crypto
    from shared.db import engine
    from shared.models import utcnow

    try:
        crypto.encrypt_field("확인", field="diagnosis", patient_id=0)  # 키가 쓸 수 있는지 먼저 확인
    except crypto.CryptoError as exc:
        sys.exit(f"진료기록 키를 쓸 수 없습니다: {exc}  (.env 의 MEDICAL_KEY 를 확인하세요)")

    with engine.begin() as conn:  # 전부 성공하면 COMMIT
        # 먼저 patient01 시연 데이터 (그 예약의 진료기록은 시연용 내용으로). 아래 일반 진료기록은 이 예약을 건너뛴다.
        demo = seed_demo_patient(conn, crypto)
        rows = conn.execute(text(
            """
            SELECT r.id, r.patient_id, r.doctor_id, r.checked_in_at, d.code
            FROM reservations r
            JOIN departments d ON d.id = r.department_id
            LEFT JOIN medical_records m ON m.reservation_id = r.id
            WHERE r.checkin_status = 'CHECKED_IN' AND r.status = 'BOOKED' AND r.doctor_id IS NOT NULL
              AND m.id IS NULL AND r.request_id NOT LIKE 'demo-doctor-%'
            ORDER BY r.id
            """
        )).all()
        for i, (reservation_id, patient_id, doctor_id, checked_in_at, code) in enumerate(rows):
            samples = SAMPLES.get(code, [DEFAULT_SAMPLE])
            diagnosis, note = samples[i % len(samples)]
            d_enc, d_nonce, version = crypto.encrypt_field(diagnosis, field="diagnosis", patient_id=patient_id)
            n_enc, n_nonce, _ = crypto.encrypt_field(note, field="note", patient_id=patient_id)
            created = checked_in_at or utcnow()
            conn.execute(
                text(
                    """
                    INSERT INTO medical_records (patient_id, reservation_id, doctor_id, diagnosis_enc, note_enc,
                                                 diagnosis_nonce, note_nonce, key_version, created_at, updated_at)
                    VALUES (:patient_id, :reservation_id, :doctor_id, :d_enc, :n_enc, :d_nonce, :n_nonce, :version,
                            :created, :created)
                    """
                ),
                {"patient_id": patient_id, "reservation_id": reservation_id, "doctor_id": doctor_id,
                 "d_enc": d_enc, "n_enc": n_enc, "d_nonce": d_nonce, "n_nonce": n_nonce, "version": version,
                 "created": created},
            )
        # 진료기록이 있는 접수 예약은 진료완료로 (진료대기 → 진료완료. 상태는 앞으로만 간다. 다시 실행해도 안전)
        conn.execute(text(
            "UPDATE reservations r SET r.checkin_status = 'COMPLETED' WHERE r.checkin_status = 'CHECKED_IN' "
            "AND EXISTS (SELECT 1 FROM medical_records m WHERE m.reservation_id = r.id)"))
        doctor_count, doctor_added = seed_doctor_demo(conn)
    print(f"의사 시연 예약: 계정 연결된 의사 {doctor_count}명, 오늘 예약 {doctor_added}건을 새로 넣었습니다.")
    print(f"{DEMO_LOGIN_ID} 시연 데이터: 예약 {demo[0]}건, 진료기록 {demo[1]}건, 영수증 {demo[2]}건을 새로 넣었습니다.")
    print(f"진료기록 {len(rows)}건을 암호화해서 넣었습니다. (키·평문은 출력하지 않음)")


if __name__ == "__main__":
    main()
