# -*- coding: utf-8 -*-
"""P4-3 — Incident 그루퍼 (src/orchestrator).

fdc.alert(계약 §3)를 구독해 **같은 chamber_id + 시간창(merge_gap_sec) 근접** 알람을
하나의 Incident로 병합하고 `incidents`(부모) + `incident_alerts`(멤버십 정본)에 upsert한다.

설계 근거(헌법·계약):
  · 계약 §4 "[PM 백엔드] Incident 그룹핑" — 승인은 Incident당 1건(헌법 1-4),
    LangGraph thread_id = incident_id (후행 P4-1).
  · 병합축: mock alert.timestamp = wall-clock-now라 wafer-gap(B5) literal 불가 →
    dev는 시간축(params incident.merge_gap_sec). 실물 정합은 params 주석 참조.
  · 멤버십 정본 = incident_alerts (그룹핑=PM 소관). spc_violations.incident_id는 옵션 스탬프.
  · context_score < B7(context_score_agent_min)도 그룹핑·기록은 함 (agent 트리거만 downstream gating).

헌법 준수: 6-1(param·docstring·logging) / 6-2(graceful shutdown·역직렬화 skip+log) / 6-4(consumer-group-<역할>).
지연 import(psycopg2·confluent_kafka·yaml): 순수 헬퍼는 무거운 의존 없이 단위테스트 가능(P4-3 검증).

실행:
    DATABASE_URL=postgresql://... KAFKA_BOOTSTRAP=localhost:9092 \
        python -m src.orchestrator.incident_grouper
"""

from __future__ import annotations

import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import chamber_inhibit                      # RTD 자동 inhibit (헌법 1-1 예외 4)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ [grouper] %(message)s")
log = logging.getLogger("incident-grouper")

ROOT = Path(__file__).resolve().parents[2]
PARAMS_PATH = ROOT / "config" / "params.yaml"

TOPIC_ALERT = "fdc.alert"                              # 계약 §3 단일 경보 채널 (헌법 1-2)
CONSUMER_GROUP = "consumer-group-incident-grouper"    # 헌법 6-4
SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}

_shutdown = False   # graceful shutdown 플래그 (헌법 6-2)


# ===========================================================================
# 순수 헬퍼 — DB/Kafka 무관, 단위 테스트 대상 (P4-3 검증)
# ===========================================================================
def load_params() -> dict:
    """config/params.yaml 로드 (없으면 명시적 에러 — 조용한 기본값 금지, 헌법 6-1)."""
    import yaml  # 지연 import
    if not PARAMS_PATH.exists():
        raise FileNotFoundError(f"config/params.yaml 없음: {PARAMS_PATH}")
    with open(PARAMS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def chamber_token(chamber_id: str) -> str:
    """incident_id용 챔버 토큰 — 언더스코어 제거 (SIM_CH_3 → SIMCH3). 계약 예시 정합."""
    return chamber_id.replace("_", "")


def is_crazy_alert(alert: dict) -> bool:
    """B9 crazy wafer 마커 감지 — violations[]에 rule_id='B9' 합성 위반 존재 (P6-1).

    B9는 Nelson 룰이 아니라 **종류 마커**(계약 §3 — B 발행): predicted_c65>P99 또는
    anomaly 초과 wafer 1장 스팟. sensor='C65'는 마커라 control_limits 조회 금지.
    """
    return any(v.get("rule_id") == "B9" for v in alert.get("violations", []) or [])


def crazy_hold_basis(alert: dict) -> str:
    """B9 위반에서 HOLD 근거 문자열 조립 — 걸린 arm의 값·컷을 코드로 생성 (LLM 무관).

    crazy-only 저점수 알람은 agent 미가동이라 C 리포트가 없다 → system_recommendation
    근거는 여기(코드)가 정본. description(arm 명시)+current_value/cut을 그대로 인용한다.
    """
    for v in alert.get("violations", []) or []:
        if v.get("rule_id") == "B9":
            return (f"B9 crazy: {v.get('description', 'arm 미상')} — "
                    f"current={v.get('current_value')}, cut={v.get('control_limit_upper')} "
                    f"[SPC B9]")
    return "B9 crazy: 위반 상세 없음 [SPC B9]"


def severity_rank(sev: Optional[str]) -> int:
    """severity 문자열 → 순위 정수 (미지값 0). WARNING < CRITICAL."""
    return SEVERITY_RANK.get((sev or "").upper(), 0)


def max_severity(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """두 severity 중 높은 쪽 (None은 무시)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if severity_rank(a) >= severity_rank(b) else b


def alert_max_severity(violations: list) -> Optional[str]:
    """violations[] 중 최고 severity (계약 §3 변경금지 필드). 미지 severity는 경고 후 rank 0."""
    best = None
    for v in violations or []:
        sev = v.get("severity")
        if sev is not None and str(sev).upper() not in SEVERITY_RANK:
            log.warning(f"미지 severity '{sev}' — rank 0 처리 (SEVERITY_RANK 확장 검토)")
        best = max_severity(best, sev)
    return best


def compute_priority(severity: Optional[str], context_score: Optional[int]) -> float:
    """결정 큐 정렬용 priority_score (interim).

    severity_rank×1000 + context_score — CRITICAL이 WARNING보다 항상 위, 동급은 context로 tie-break.
    TODO(P5/P6): HOLD 물량(wafer_dispositions)×단가 반영 (헌법 1-4 "severity × HOLD 물량").
    """
    return float(severity_rank(severity) * 1000 + (context_score or 0))


def next_seq(existing_ids: list) -> int:
    """같은 prefix(INC-<day>-<token>-) 기존 id들에서 다음 3자리 SEQ (없으면 1)."""
    mx = 0
    for iid in existing_ids:
        try:
            mx = max(mx, int(str(iid).rsplit("-", 1)[-1]))
        except (ValueError, IndexError):
            continue
    return mx + 1


def make_incident_id(chamber_id: str, seq: int, day: Optional[str] = None) -> str:
    """INC-<YYYYMMDD>-<CHAMBER>-<SEQ> (헌법 6-4 업무 ID 포맷)."""
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"INC-{day}-{chamber_token(chamber_id)}-{seq:03d}"


def parse_ts(ts: str) -> datetime:
    """ISO-8601 timestamp 파싱 (Z 접미사 허용) → aware datetime(UTC)."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def within_merge_gap(last_ts: datetime, alert_ts: datetime, merge_gap_sec: float) -> bool:
    """직전 알람과의 간격이 merge_gap 이내인가 (시각근접 병합 판정)."""
    return abs((alert_ts - last_ts).total_seconds()) <= merge_gap_sec


# ===========================================================================
# 그루퍼 — DB I/O (알람 1건 = 1 트랜잭션)
# ===========================================================================
#: verifying sweep 스로틀 — run 루프에서 이 간격(초)마다만 sweep 쿼리 실행 (poll은 1s)
_SWEEP_INTERVAL_SEC = 15.0


class IncidentGrouper:
    """fdc.alert → incidents/incident_alerts upsert."""

    def __init__(self, conn, merge_gap_sec: float, initial_lifecycle: str, agent_min: int,
                 verify_close_sec: Optional[float] = None,
                 rtd_params: Optional[dict] = None):
        """verify_close_sec: verifying 자동 종결 창(초) — None이면 sweep 비활성(테스트/구형 배선).

        운영 main()은 params.incident.verify_close_sec를 명시 로드해 주입한다 (P6-1,
        PM 결정: 무재발 시 시간 기반 자동 closed — 재발 전이는 병합 경로가 담당).
        rtd_params: params 전체 dict (rtd.auto_inhibit 스위치 — 예외 4 회귀용). None=기본 발동.
        """
        self.conn = conn
        self.merge_gap_sec = merge_gap_sec
        self.initial_lifecycle = initial_lifecycle
        self.agent_min = agent_min
        self.verify_close_sec = verify_close_sec
        self.rtd_params = rtd_params

    def _already_grouped(self, cur, alert_id: str) -> bool:
        """이미 편입된 alert_id면 True (Kafka 재전달 idempotency — 고아 incident 방지)."""
        cur.execute("SELECT 1 FROM incident_alerts WHERE alert_id = %s", (alert_id,))
        return cur.fetchone() is not None

    def _mergeable_incident(self, cur, chamber_id: str, alert_ts: datetime):
        """같은 chamber의 미종료 incident 중 최근 멤버 알람이 merge_gap 이내면 incident_id 반환.

        미종료 = lifecycle <> 'closed' (open/analyzing/pending/verifying/reopened).
        pending/verifying 병합 정교화는 P4-1과 함께 (지금은 같은 에피소드로 흡수).
        """
        cur.execute(
            """
            SELECT i.incident_id, MAX(ia.alert_ts) AS last_ts
              FROM incidents i
              JOIN incident_alerts ia ON ia.incident_id = i.incident_id
             WHERE i.chamber_id = %s AND i.lifecycle <> 'closed'
             GROUP BY i.incident_id
             ORDER BY last_ts DESC
             LIMIT 1
            """,
            (chamber_id,),
        )
        row = cur.fetchone()
        if row and row[1] and within_merge_gap(row[1], alert_ts, self.merge_gap_sec):
            return row[0]
        return None

    def _create_incident(self, cur, chamber_id: str, alert_ts: datetime,
                         severity: Optional[str], priority: float,
                         start_wafer, end_wafer,
                         incident_type: Optional[str] = None) -> str:
        """신규 incident 채번·삽입 (SEQ = 같은 날·챔버 기존 id에서 +1).

        incident_type(P6-1): NULL=일반 / 'crazy_spot'=B9 신규 생성 건. 병합 편입 시엔
        기존 type 유지 (먼저 열린 사건의 성격이 우선 — 후속 알람이 종류를 바꾸지 않음).

        전제: 단일 컨슈머 인스턴스. SELECT+INSERT는 트랜잭션 내지만, 다중 인스턴스면
        incident_id UNIQUE 충돌 가능 → 스케일 시 DB 시퀀스/advisory lock 전환.
        """
        day = alert_ts.astimezone(timezone.utc).strftime("%Y%m%d")
        prefix = f"INC-{day}-{chamber_token(chamber_id)}-"
        cur.execute("SELECT incident_id FROM incidents WHERE incident_id LIKE %s",
                    (prefix + "%",))
        incident_id = make_incident_id(chamber_id, next_seq([r[0] for r in cur.fetchall()]), day)
        cur.execute(
            """
            INSERT INTO incidents
                (incident_id, chamber_id, lifecycle, incident_type, severity_max,
                 priority_score, suspect_window_start, suspect_window_end)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (incident_id, chamber_id, self.initial_lifecycle, incident_type, severity,
             priority, start_wafer, end_wafer),
        )
        return incident_id

    def _add_membership(self, cur, incident_id: str, alert: dict, alert_ts: datetime,
                        severity: Optional[str], sw: dict) -> None:
        """incident_alerts 멤버십 삽입 (alert_id 유니크 — ON CONFLICT 방어).

        raw는 json 문자열 → %s::jsonb 캐스팅 (psycopg2.extras.Json 의존 제거 — 테스트 용이).
        """
        cur.execute(
            """
            INSERT INTO incident_alerts
                (incident_id, alert_id, chamber_id, severity, context_score,
                 alert_ts, suspect_start, suspect_end, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (alert_id) DO NOTHING
            """,
            (incident_id, alert.get("alert_id"), alert.get("chamber_id"), severity,
             alert.get("context_score"), alert_ts,
             sw.get("start_wafer"), sw.get("end_wafer"),
             json.dumps(alert, ensure_ascii=False)),
        )

    def _update_aggregates(self, cur, incident_id: str, severity: Optional[str],
                           priority: float, end_wafer) -> None:
        """병합 시 incident 집계 갱신 — severity_max 상향·priority 최대·suspect_window_end 최신·updated_at.

        suspect_window: start=최초 알람(create 시)·end=최신 알람 = **표시용 span proxy**.
        wafer 순서 불명이라 진짜 min/max 아님 (①시각근접 결정과 동일 전제).
        """
        cur.execute("SELECT severity_max, priority_score FROM incidents WHERE incident_id = %s",
                    (incident_id,))
        cur_sev, cur_pri = cur.fetchone()
        cur.execute(
            """
            UPDATE incidents
               SET severity_max = %s,
                   priority_score = %s,
                   suspect_window_end = COALESCE(%s, suspect_window_end),
                   updated_at = NOW()
             WHERE incident_id = %s
            """,
            (max_severity(cur_sev, severity), max(cur_pri or 0.0, priority), end_wafer, incident_id),
        )

    def _reopen_if_verifying(self, cur, incident_id: str) -> bool:
        """병합 대상이 verifying이면 reopened 전이 + reopen_count++ (P6-1 — B6 재발 감시).

        verifying = 조치 적용 후 관측 창. 이 창에서 같은 챔버 알람이 또 오면(=병합 편입)
        조치 효과 의심 → reopened. WHERE lifecycle='verifying' 가드로 멱등·경합 안전.
        """
        cur.execute(
            """UPDATE incidents
                  SET lifecycle = 'reopened', reopen_count = reopen_count + 1,
                      updated_at = NOW()
                WHERE incident_id = %s AND lifecycle = 'verifying'""",
            (incident_id,))
        return cur.rowcount > 0

    def _hold_crazy_wafer(self, cur, incident_id: str, alert: dict) -> bool:
        """B9 crazy wafer → wafer_dispositions **잠정** 행 INSERT (P6-1 · 처분 행 상태 전이 모델).

        처분 행 생애주기 = 잠정(여기) → 확정(승인 게이트의 per_wafer — 계약 §4-B/§6):
          · 잠정: status='HOLD' + `hold_reason`에 격리 사유·B9 arm 수치(crazy_hold_basis) 기록.
            system_recommendation='SCRAP'은 판정 트리 STEP4와 동일한 코드 판정 후보(창작 아님).
          · **`recommendation_basis`·`decided_*`는 채우지 않는다** — 확정(승인 per_wafer) 몫.
            같은 컬럼을 두 작성자가 경쟁하지 않게 하는 역할 분리 (2026-07-28 설계).
        멱등: 같은 (wafer_id, incident_id) 행 존재 시 skip (Kafka 재전달 방어).
        """
        wafer_id = (alert.get("prediction_context") or {}).get("wafer_id")
        if not wafer_id:
            log.warning(f"B9 alert에 wafer_id 없음 — HOLD 행 생략: {alert.get('alert_id')}")
            return False
        cur.execute("SELECT 1 FROM wafer_dispositions WHERE wafer_id = %s AND incident_id = %s",
                    (wafer_id, incident_id))
        if cur.fetchone() is not None:
            return False                          # 멱등 — 이미 격리됨
        cur.execute(
            """INSERT INTO wafer_dispositions
                   (wafer_id, lot_id, chamber_id, incident_id, status, hold_reason,
                    system_recommendation)
               VALUES (%s, %s, %s, %s, 'HOLD', %s, 'SCRAP')""",
            (wafer_id, alert.get("lot_id"), alert.get("chamber_id"), incident_id,
             f"B9 crazy 자동 격리(잠정 — 승인 전 투입 보류): {crazy_hold_basis(alert)}"))
        return True

    def sweep_verifying(self) -> int:
        """verifying → closed 자동 종결 — 진입(updated_at) 후 verify_close_sec 무재발 시 (P6-1).

        재발이면 병합 경로가 이미 reopened로 전이시켰으므로(=updated_at 갱신 + lifecycle 변경)
        여기 WHERE에 안 걸린다. verify_close_sec=None(미배선)이면 no-op. 반환=닫은 건수.
        PM 결정(2026-07-28): 시간 기반 sweep — B의 30장 관측 이벤트 연동은 후속.
        """
        if self.verify_close_sec is None:
            return 0
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute(
                    """UPDATE incidents
                          SET lifecycle = 'closed', closed_at = NOW(), updated_at = NOW()
                        WHERE lifecycle = 'verifying'
                          AND updated_at < NOW() - make_interval(secs => %s)""",
                    (float(self.verify_close_sec),))
                n = cur.rowcount
        if n:
            log.info(f"verifying 자동 종결: {n}건 (무재발 {self.verify_close_sec}s 경과)")
        return n

    def handle_alert(self, alert: dict) -> Optional[str]:
        """알람 1건 처리 — idempotency 체크 + 병합/신규 결정 + 멤버십 + 집계 (1 트랜잭션).

        P6-1 추가: B9 crazy 마커 인식(신규 생성 시 incident_type='crazy_spot' + HOLD 행)
        · verifying 병합 시 reopened 전이. 반환: 편입된 incident_id (중복이면 None).
        """
        chamber_id = alert["chamber_id"]
        alert_id = alert.get("alert_id", "?")
        alert_ts = parse_ts(alert["timestamp"])
        severity = alert_max_severity(alert.get("violations", []))
        ctx = alert.get("context_score")
        priority = compute_priority(severity, ctx)
        sw = alert.get("suspect_window") or {}
        crazy = is_crazy_alert(alert)

        reopened = held = inhibited = False
        with self.conn:                          # 성공 시 commit / 예외 시 rollback
            with self.conn.cursor() as cur:
                if self._already_grouped(cur, alert_id):
                    log.info(f"중복 alert 스킵 (idempotent): {alert_id}")
                    return None
                incident_id = self._mergeable_incident(cur, chamber_id, alert_ts)
                created = incident_id is None
                if created:
                    incident_id = self._create_incident(
                        cur, chamber_id, alert_ts, severity, priority,
                        sw.get("start_wafer"), sw.get("end_wafer"),
                        incident_type="crazy_spot" if crazy else None)
                self._add_membership(cur, incident_id, alert, alert_ts, severity, sw)
                if not created:
                    self._update_aggregates(cur, incident_id, severity, priority,
                                            sw.get("end_wafer"))
                    reopened = self._reopen_if_verifying(cur, incident_id)
                if crazy:
                    held = self._hold_crazy_wafer(cur, incident_id, alert)
            # RTD 자동 inhibit 기록 (헌법 1-1 예외 4, 2026-08-06) — 같은 트랜잭션 안.
            # 판정은 chamber_inhibit이 2단으로 한다: 사전 필터(should_inhibit — 알람 단건,
            # DB 무관) → 본 판정(persistent_breach — 창 내 지속 이탈률, spc_violations 조회).
            # 후자가 같은 커넥션을 쓰므로 이 트랜잭션 안에서 부르는 것이 읽기 일관성상 맞다.
            # 판별축 근거 = docs/RTD_발동빈도_현업정합_검증_2026-08-06.md 부록(2026-08-06 심야).
            _inh = chamber_inhibit.record_trigger(self.conn, incident_id, alert,
                                                  params=self.rtd_params)
        if _inh is not None:                     # 순서 규약: 기록 → 커밋(위 with 탈출) → 정지
            chamber_inhibit.drop_signal_after_commit(_inh[0], incident_id, _inh[1], _inh[2])
            inhibited = _inh[2]                  # 'chamber' | 'equipment' (로그 표기용)

        gated = "" if (ctx or 0) >= self.agent_min else f" [context<{self.agent_min} 기록만·agent 미가동]"
        extras = ("".join([" [B9 crazy → HOLD]" if held else (" [B9 crazy]" if crazy else ""),
                           " [verifying 재발 → reopened]" if reopened else "",
                           (" [■ RTD 자동 inhibit — 장비 단위]" if inhibited == "equipment"
                            else " [■ RTD 자동 inhibit — 챔버]" if inhibited else "")]))
        log.info(f"{'신규' if created else '병합'} {incident_id} ← {alert_id} "
                 f"(chamber={chamber_id}, sev={severity}, ctx={ctx}){gated}{extras}")
        return incident_id

    def run(self, consumer) -> None:
        """Kafka 소비 루프 — 역직렬화 실패 skip+log, 처리 예외 skip+rollback (헌법 6-2).

        오프셋은 **처리 완료(성공/스킵) 후 수동 커밋**(at-least-once). 성공 후 크래시로 재전달돼도
        alert_id UNIQUE로 idempotent 스킵됨. poison 메시지는 로그 후 커밋해 파티션 루프 방지
        (retry/DLQ 정교화는 후속).
        """
        import time as _time                      # 지연 import — sweep 스로틀 시계
        consumer.subscribe([TOPIC_ALERT])
        log.info(f"구독 시작: {TOPIC_ALERT} (그룹 {CONSUMER_GROUP}, merge_gap={self.merge_gap_sec}s, "
                 f"verify_close={self.verify_close_sec}s)")
        last_sweep = _time.monotonic()
        while not _shutdown:
            msg = consumer.poll(1.0)
            if _time.monotonic() - last_sweep >= _SWEEP_INTERVAL_SEC:   # P6-1 — verifying 자동 종결
                last_sweep = _time.monotonic()
                try:
                    self.sweep_verifying()
                except Exception:                   # noqa: BLE001 — sweep 실패가 소비를 막지 않게
                    log.exception("verifying sweep 실패 — 다음 주기 재시도")
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
            if msg is None:
                continue
            if msg.error():
                log.error(f"Kafka 에러: {msg.error()}")
                continue
            try:
                alert = json.loads(msg.value().decode("utf-8"))
                self.handle_alert(alert)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                log.error(f"역직렬화 실패 — 스킵: {e}")   # 헌법 6-2
            except Exception as e:                      # noqa: BLE001 — 파이프라인 생존 우선
                log.exception(f"알람 처리 실패 — 스킵: {e}")
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            consumer.commit(message=msg, asynchronous=False)   # 처리 완료 후 오프셋 전진


def _handle_signal(signum, _frame) -> None:
    """SIGINT/SIGTERM → graceful shutdown 플래그 (헌법 6-2)."""
    global _shutdown
    _shutdown = True
    log.info(f"시그널 {signum} 수신 — graceful shutdown")


def main() -> None:
    """CLI 진입점 — params/env 로드 → DB·Consumer 연결 → 소비 루프."""
    import psycopg2                              # 지연 import
    from confluent_kafka import Consumer
    try:                                         # .env 자동 로드 (2026-07-24 — B 스크립트들과 관례 통일)
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    params = load_params()
    inc = params.get("incident", {}) or {}
    merge_gap = inc.get("merge_gap_sec")
    if merge_gap is None:
        raise ValueError("params.incident.merge_gap_sec 미설정 (헌법 6-1 조용한 기본값 금지)")
    initial_lifecycle = inc.get("initial_lifecycle", "open")
    agent_min = (params.get("spc", {}) or {}).get("context_score_agent_min", 31)
    verify_close = inc.get("verify_close_sec")   # P6-1 — 없으면 sweep 비활성 (명시 경고)
    if verify_close is None:
        log.warning("params.incident.verify_close_sec 미설정 — verifying 자동 종결 sweep 비활성")

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL 환경변수 필요 (.env.example 참조)")
    bootstrap = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    conn = psycopg2.connect(dsn)
    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": CONSUMER_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,   # at-least-once — DB 성공 후 수동 커밋 (fdc.alert 유실 방지, 헌법 1-2)
    })
    grouper = IncidentGrouper(conn, float(merge_gap), initial_lifecycle, int(agent_min),
                              verify_close_sec=(float(verify_close)
                                                if verify_close is not None else None),
                              rtd_params=params)   # rtd.auto_inhibit 스위치 (헌법 1-1 예외 4)
    try:
        grouper.run(consumer)
    finally:
        consumer.close()
        conn.close()
        log.info("종료 — consumer/DB 정리 완료")


if __name__ == "__main__":
    main()
