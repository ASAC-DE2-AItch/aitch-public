# =============================================================================
# dispo_seed.py   —  [담당: PM]  시연 선심기: 시나리오 주입 → 처분(HOLD) 자동 적재
# =============================================================================
# 왜 있나 (2026-08-11, 리허설 대본):
#   시나리오 2(RTD 스톰) 뒤 대본은 "정지 전에 생산된 wafer 들이 Disposition(S6)에 쌓였다 →
#   예측값을 보니 RELEASE 해도 되겠다 → 처분 승인"으로 이어진다. 그런데 B9 anomaly arm 이
#   잠겨 있어(AE 포화 격리, #174) 자동 격리로는 wafer 가 **안 쌓인다** — 야간 리허설은
#   수동 SQL 로 선심었다. 이 모듈은 그 선심기를 **시나리오 버튼에 결합**한다:
#   S11 에서 해당 시나리오를 주입하면 백그라운드 스레드가 HOLD 행을 자동 적재한다.
#
# 정직성:
#   행 자체는 연출(선심기)이되 **예측값·wafer_id 는 실물**이다 — 최근 `wafer_predictions`
#   에서 그 챔버의 실제 예측을 가져와 `system_recommendation`/`recommendation_basis` 를
#   채우므로, 대본의 "예측값을 보니…"가 실데이터로 성립한다. hold_reason 에 선심기임을
#   명시해 감사에서도 연출과 실측이 구분된다.
#
# 타이밍:
#   주입 직후에는 incident 가 아직 없다(스톰 → 알람 → grouper 경유 수십 초). 그래서
#   스레드가 `RETRY_SEC` 간격으로 그 챔버의 최신 open incident 를 기다렸다가 붙인다
#   (wafer_dispositions.incident_id 가 NOT NULL — 헌법 1-4 Incident 단위 처리와도 정합).
#
# 스위치:
#   SEED_AFTER 에 등록된 시나리오만 발동한다. 기본은 시연 2종. 발표 후 params 이관 검토
#   (6-1 — 지금은 시연 연출 훅이라 코드 상수로 두고 이 주석이 그 기록이다).
# =============================================================================
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("gateway.dispo_seed")

#: 시나리오 id → (챔버, 선심 장수). S11 주입 버튼과 1:1.
SEED_AFTER: dict[str, tuple[str, int]] = {
    "alarm_storm_ch2": ("SIM_CH_2", 9),
    "qual_loud_ch2":   ("SIM_CH_2", 9),
    # 시나리오 2 배지 시나리오 (2026-08-11 리허설 E2E 실측 — ch2 계열만 있어 미발동)
    "qual_loud_ch4":   ("SIM_CH_4", 9),
}
RETRY_SEC = 10.0          # incident 대기 재시도 간격
RETRY_MAX = 30            # 최대 30회(5분) — 스톰이면 수십 초 내 잡힌다. 초과 시 포기+로그
HOLD_REASON = "RTD 정지 구간 생산분 — 처분 심사 대기 (시연 선심기 · dispo_seed)"


def _conn():
    import psycopg2
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL 미설정 — 선심기 불가")
    return psycopg2.connect(dsn)


def _latest_open_incident(cur, chamber: str):
    cur.execute(
        """SELECT incident_id FROM incidents
            WHERE chamber_id = %s AND lifecycle NOT IN ('closed')
            ORDER BY created_at DESC LIMIT 1""", (chamber,))
    row = cur.fetchone()
    return row[0] if row else None


def _seed(chamber: str, count: int, sid: str) -> None:
    """스레드 본체 — incident 를 기다렸다가 실제 예측값으로 HOLD 행을 적재한다."""
    for attempt in range(RETRY_MAX):
        try:
            with _conn() as conn, conn.cursor() as cur:
                inc = _latest_open_incident(cur, chamber)
                if inc is None:
                    time.sleep(RETRY_SEC)
                    continue
                # 실물 예측 — 그 챔버 최근 wafer, 이미 처분 행이 있는 wafer 는 제외(멱등).
                cur.execute(
                    """SELECT p.wafer_id, p.predicted_c65
                         FROM wafer_predictions p
                        WHERE p.chamber_id = %s
                          AND NOT EXISTS (SELECT 1 FROM wafer_dispositions d
                                           WHERE d.wafer_id = p.wafer_id)
                        ORDER BY p.created_at DESC LIMIT %s""", (chamber, count))
                rows = cur.fetchall()
                if not rows:
                    time.sleep(RETRY_SEC)
                    continue
                # P99 임계 — y_thresholds 있으면 그 값, 없으면 계약 fallback 1572.
                cur.execute("SELECT p99 FROM y_thresholds ORDER BY created_at DESC LIMIT 1")
                t = cur.fetchone()
                thr = float(t[0]) if t and t[0] is not None else 1572.0
                for wid, pred in rows:
                    rec = "SCRAP" if (pred is not None and float(pred) > thr) else "RELEASE"
                    basis = (f"predicted_c65={float(pred):.1f} {'>' if rec == 'SCRAP' else '≤'} "
                             f"P99 {thr:.0f} (실측 예측 — dispo_seed)") if pred is not None \
                        else "예측 결측 — 수동 판단 필요"
                    cur.execute(
                        """INSERT INTO wafer_dispositions
                               (wafer_id, chamber_id, incident_id, status, hold_reason,
                                system_recommendation, recommendation_basis)
                           VALUES (%s, %s, %s, 'HOLD', %s, %s, %s)""",
                        (wid, chamber, inc, HOLD_REASON, rec, basis))
                conn.commit()
                log.info("dispo_seed 완료 — %s: HOLD %d장 적재 (incident=%s, 시나리오=%s)",
                         chamber, len(rows), inc, sid)
                return
        except Exception:                                    # noqa: BLE001 — 연출 훅이 본선을 못 죽인다
            log.exception("dispo_seed 시도 %d 실패 — %s", attempt + 1, chamber)
            time.sleep(RETRY_SEC)
    log.warning("dispo_seed 포기 — %s: %d회 내 incident/예측 미확보 (시나리오=%s). "
                "수동 선심기로 진행할 것", chamber, RETRY_MAX, sid)


def maybe_seed(sid: str) -> bool:
    """시나리오 주입 훅 — 등록된 시나리오면 백그라운드 선심기 기동. 반환=발동 여부."""
    plan = SEED_AFTER.get(sid)
    if plan is None:
        return False
    chamber, count = plan
    threading.Thread(target=_seed, args=(chamber, count, sid),
                     name=f"dispo-seed-{sid}", daemon=True).start()
    log.info("dispo_seed 예약 — 시나리오=%s → %s HOLD %d장 (incident 대기 후 적재)",
             sid, chamber, count)
    return True
