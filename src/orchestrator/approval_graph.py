# -*- coding: utf-8 -*-
"""P4-1 — 승인 게이트 상태머신 (src/orchestrator, LangGraph).

정본: 기획서 §6-5 (line 892-959) 스켈레톤 + 리뷰 교정(A안, 단일 게이트, 4액션).
    merge_reports(Supervisor·C stub) → approval_gate(interrupt=PENDING)
      → {approve/modify → apply_correction, reject → reject_and_learn, escalate → escalate}
    thread_id = incident_id. 각 종단 → END.

설계 근거:
  · 헌법 1-1  HITL 필수 + approval_records는 체크포인터로 대체 불가 (interrupt 시 PENDING, resume 시 update).
  · 헌법 1-4  게이트는 Supervisor 뒤 **한 곳**. Incident당 동시 PENDING 1건 (open_pending 가드).
  · 계약 §6   4액션 approve/modify/reject/escalate.
  · lifecycle open→analyzing→pending→actioned→verifying→closed/reopened (기획서 line 781).
  · 적용 분기 실력치→correction(limit)→B / recipe→correction(recipe)→시뮬 / 진짜이상→정비+wafer_dispositions
              / escalate→근거패키지·Reopen (기획서 line 654·673).

원자성: 판정 기록 + lifecycle 전이는 `commit_decision` **단일 트랜잭션**. 이벤트는 **커밋 후** 발행
    (dual-write 잔여 창 = 이벤트 유실 가능 → 트랜잭셔널 outbox는 후속 하드닝).
테스트 가능성: **langgraph 의존은 `build_graph`에서만 지연 import.** 노드 로직·라우팅·DB 기록·멱등성은
    langgraph 없이 단위 테스트. 그래프 실행(interrupt/resume·PostgresSaver)은 실 env/docker(#24).
Supervisor(merge_reports)·3-리포트 fan-in은 C(팀원 C) 소관 — 여기선 stub seam.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Callable, Optional, TypedDict

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ [gate] %(message)s")
log = logging.getLogger("approval-gate")

VALID_ACTIONS = {"approve", "modify", "reject", "escalate"}   # 계약 §6
ACTION_STATUS = {"approve": "APPROVED", "modify": "MODIFIED",
                 "reject": "REJECTED", "escalate": "ESCALATED"}


class ApprovalState(TypedDict, total=False):
    """LangGraph 상태 — 체크포인터에 자동 저장됨 (thread_id=incident_id)."""
    incident_id: str
    report: dict           # Supervisor Brief (selected_option·options·recommendation)
    decision: str          # resume action (approve/modify/reject/escalate)
    modified_value: Any
    reanalyze: bool        # reject 시 재분석(True→analyzing)/종료(False→closed) — 엔지니어 선택
    reason: str
    approver: str
    approver_role: str
    disposition: str       # 처분 선택 RELEASE/SCRAP (2026-08-10 — approve+manual_option 전용, 없으면 권고안 그대로)
    # 🔴 웨이퍼별 확정값 [{wafer_id, disposition}] (2026-08-11) — 처분 카드 승인 시점 판정.
    #   이 줄이 없어서 3번의 승인이 조용히 프리필로 확정됐다(8/11 실측): gate_parse 가
    #   per_wafer 를 담아 리턴해도 **상태 스키마에 채널이 없으면 LangGraph 가 그 키를 버린다**
    #   (예외도 안 난다 — 200 + 원장은 권고값). 채널 선언이 곧 계약이다.
    per_wafer: list
    result: dict


# ===========================================================================
# 순수 헬퍼 — langgraph/DB 무관, 단위 테스트 대상
# ===========================================================================
def route_decision(action: Optional[str]) -> str:
    """approval_gate 조건부 엣지 — 4액션 → 다음 노드명 (미지값은 fail-closed=반려)."""
    return {"approve": "apply_correction", "modify": "apply_correction",
            "reject": "reject_and_learn", "escalate": "escalate"}.get(action or "", "reject_and_learn")


def apply_target(option: Optional[str]) -> dict:
    """승인된 4지선다 옵션 → 산출 토픽/이벤트/테이블 (기획서 line 654·계약 §)."""
    return {
        "limit_option":  {"topic": "fdc.correction", "correction_type": "limit",
                          "consumer": "B", "table": "limit_corrections"},
        "recipe_option": {"topic": "fdc.correction", "correction_type": "recipe",
                          "consumer": "simulator", "table": "recipe_corrections"},
        "manual_option": {"event": "MaintenanceApproved", "table": None,
                          "note": "정비 권고 발행만 — 처분(wafer_dispositions)은 disposition_option 전용 카드"},
        #   ↑ 2026-08-12 개명 (구 WaferDispositioned·table=wafer_dispositions — TSR-0006 미해결 2):
        #   8/11 정비/처분 분리 후에도 정비 승인이 처분 이름의 이벤트를 fdc.correction 에 내보냈다.
        #   WaferDispositioned 는 기획서 §7 정의상 "wafer 스크랩/해제 판정 완료" = apply_disposition
        #   전용이다. MaintenanceApproved 는 events.py 라우트 미등재 → DEFAULT(fdc.agent) 의도.
        #   하류 영향 없음 확인: fdc.correction 소비자는 B(limit만)·시뮬레이터(recipe만)·Dashboard
        #   (계약 §0)이고, fdc.agent 는 event_type 필터 관례(§8-B)로 미지 이벤트를 skip 한다.
    }.get(option or "", {"note": "적용 없음 (미지/에스컬레이션 옵션)"})


def request_type_for(option: Optional[str]) -> str:
    """selected_option → approval_records.request_type.

    🔴 2026-08-11 분리 (PM 결정): 구 매핑은 manual(정비)을 'disposition' 으로 적어서
    **정비 카드가 처분 카드를 겸했다** — 승인 한 번이 두 결정(정비 발행 + 웨이퍼 확정)을
    동시에 실행했고, 화면(S4)은 정비 근거 옆에 RELEASE/SCRAP 토글을 띄우는 기형이 됐다
    (8/10 리허설 실측). 이제:
      · manual_option      → 'maintenance'   (정비 — 조치 발행만)
      · disposition_option → 'disposition'   (처분 전용 카드 — S6 발원, 헌법 1-4 예외)
      · 그 외              → 'correction'
    """
    if option == "disposition_option":
        return "disposition"
    return "maintenance" if option == "manual_option" else "correction"


def is_disposition_state(state: "ApprovalState") -> bool:
    """이 스레드가 처분 전용 승인인가 — report.request_type 정본 (2026-08-11)."""
    return ((state.get("report") or {}).get("request_type")) == "disposition"


def dispo_thread_id(incident_id: str) -> str:
    """처분 파생 thread — 헌법 1-4 예외 구현 (2026-08-11 PM 결정).

    LangGraph 한 thread 는 interrupt 를 하나만 들 수 있어, 본선(정비/보정) PENDING 과
    처분 PENDING 이 공존하려면 thread 가 갈라져야 한다. CT²(ct2_deploy)와 같은 수법이되
    **그래프는 본편을 재사용**한다 — 예외조항이지 새 그래프가 아니다.
    """
    return f"dispo::{incident_id}"


MODIFIED_FIELD_MAP = {"center": "center_modified", "ucl": "ucl_modified", "lcl": "lcl_modified"}


def resolve_modified_fields(option: Optional[str], modified_value: Any) -> dict:
    """modify 수정값 → limit_corrections `*_modified` 컬럼 dict (B5-1 필드규칙 안ⓐ, 2026-07-23).

    limit_option 한정. 스칼라 = center 수정(sigma 그룹), dict = {center|ucl|lcl: 절대값}
    (분위수 그룹 경계 직접·부분 수정). 미지 키·비수치는 무시+경고 — 구조 가드는 B Layer1 몫.
    recipe_option 수정값의 DB 반영은 B5-4 후속 — 빈 dict 반환(페이로드 정보성 전달만).
    """
    if option != "limit_option" or modified_value is None:
        return {}
    if isinstance(modified_value, (int, float)) and not isinstance(modified_value, bool):
        return {"center_modified": float(modified_value)}
    if isinstance(modified_value, dict):
        out = {}
        for k, v in modified_value.items():
            col = MODIFIED_FIELD_MAP.get(k)
            if col is None or isinstance(v, bool) or not isinstance(v, (int, float)):
                log.warning(f"modify 수정값 필드 무시 (미지 키/비수치): {k}={v!r}")
                continue
            out[col] = float(v)
        return out
    log.warning(f"modify 수정값 형식 미지원 — DB 미기입 (스칼라/dict만): {type(modified_value).__name__}")
    return {}


def build_correction_payload(incident_id: str, option: Optional[str],
                             report: Optional[dict] = None, modified_value: Any = None) -> dict:
    """CorrectionApplied(fdc.correction) 페이로드 (계약 §6). 값은 승인된 리포트(LIM/RCP)에서 매핑,
    미사용 필드는 null 유지(이름 삭제 금지 — 계약 line 285). manual은 MaintenanceApproved
    이벤트(fdc.agent — 2026-08-12 개명, 구 WaferDispositioned 는 처분 확정 전용으로 환원).

    실값 소스(C의 리포트)가 아직이면 해당 필드 null — 형태(계약 준수)는 지금 확정.
    """
    report = report or {}
    t = apply_target(option)
    if t.get("topic") != "fdc.correction":
        return {"event": t.get("event"), "incident_id": incident_id, "table": t.get("table")}
    ctype = t["correction_type"]
    payload = {"incident_id": incident_id, "correction_type": ctype, "effective_from": None,
               "sensor": None, "limit_version": None,          # limit형 (계약 §6)
               "correction_id": None,                           # limit형 — limit_corrections PK (B apply_approved 참조, 2026-07-21)
               "parameter_id": None, "delta_pct": None,         # recipe형
               "recipe_correction_id": None,                    # recipe형 — recipe_corrections PK (대칭)
               "value_proposed": modified_value}                # 정보성(표시·감사) — B 적용 정본은 limit_corrections.*_modified (COALESCE, B5-1 필드규칙 2026-07-23)
    if ctype == "limit":
        la = report.get("limit_analysis") or {}
        payload["sensor"] = la.get("sensor")
        payload["limit_version"] = la.get("limit_version_current")
        payload["correction_id"] = la.get("correction_id")      # B 채번 승계 — 재채번 금지 (헌법 6-4)
        if payload["value_proposed"] is None:
            payload["value_proposed"] = la.get("center_proposed")
    else:  # recipe
        rt = report.get("recipe_tuning") or {}
        payload["parameter_id"] = rt.get("parameter_id")
        payload["delta_pct"] = rt.get("delta_pct")
        payload["recipe_correction_id"] = rt.get("recipe_correction_id")
        if payload["value_proposed"] is None:
            payload["value_proposed"] = rt.get("value_proposed")
    return payload


def emit_event(event_type: str, payload: dict, producer: Optional[Callable] = None) -> None:
    """Kafka 이벤트 발행 훅 (계약 §7). producer 미주입 시 로그 stub. **커밋 후 호출.**"""
    if producer is not None:
        producer(event_type, payload)
    else:
        log.info(f"[event·stub] {event_type} — {payload.get('incident_id')}")


def _is_unique_violation(exc: BaseException) -> bool:
    """예외가 Postgres UniqueViolation(SQLSTATE 23505)인가 — 라이브러리 무의존 판별.

    psycopg2 예외는 `pgcode` 속성을 갖는다. 클래스로 isinstance 검사하면 psycopg2 를
    import 해야 하고, 그러면 langgraph 지연 import 로 지켜온 단위 테스트 격리가 깨진다.
    래핑된 예외도 있으므로 원인 체인(__cause__)까지 한 겹 본다.
    """
    for e in (exc, getattr(exc, "__cause__", None)):
        if e is not None and getattr(e, "pgcode", None) == "23505":
            return True
    return False


# ===========================================================================
# DB 저장소 — approval_records(기록) + incidents(lifecycle). psycopg2 (grouper와 동일).
# 판정 기록+lifecycle 전이는 단일 트랜잭션(commit_decision).
# ===========================================================================
class ApprovalStore:
    """approval_records/incidents DML. 노드에 주입 (테스트는 동일 인터페이스 FakeStore).

    conn(단일) 또는 pool(psycopg2 ThreadedConnectionPool) 주입 — pool이면 op당 acquire/release로
    동시 resume(멀티스레드 gateway) 안전 (A6). conn 단독 모드는 기존과 동일 동작.
    """

    def __init__(self, conn=None, pool=None):
        if conn is None and pool is None:
            raise ValueError("ApprovalStore: conn 또는 pool 필요")
        self._conn = conn
        self._pool = pool

    def _acquire(self):
        return self._pool.getconn() if self._pool is not None else self._conn

    def _release(self, conn):
        if self._pool is not None:
            self._pool.putconn(conn)

    @contextmanager
    def _txn(self):
        """커넥션 획득 → 트랜잭션(with conn) → 커서 → 반납. pool/단일 공통."""
        conn = self._acquire()
        try:
            with conn, conn.cursor() as cur:
                yield cur
        finally:
            self._release(conn)

    @contextmanager
    def read_conn(self):
        """읽기 전용 커넥션 대여 — merge_reports 경로③(agent_reports 수합)용.

        2026-07-31: 게이트웨이는 pool 로 이 store 를 빌드하는데, 경로③은 `store.conn`
        시임만 봐서 **pool 모드에서 죽어 있었다** — 자동시작 폴러가 연 PENDING 의
        original_value 가 전부 {} 로 적재된 원인 (start_approval CLI 만 몽키패치
        `store.conn = conn` 으로 살아 있었음). pool/단일 공통으로 대여한다.
        반납 전 rollback — SELECT 도 암묵 트랜잭션을 열므로 pool 반납 위생.
        """
        conn = self._acquire()
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            except Exception:                        # noqa: BLE001 — 죽은 커넥션은 pool 재접속 몫
                pass
            self._release(conn)

    def set_lifecycle(self, incident_id: str, lifecycle: str) -> None:
        """incidents.lifecycle 전이 (전이 주체 = 이 게이트, 기획서 line 781)."""
        with self._txn() as cur:
            cur.execute("UPDATE incidents SET lifecycle = %s, updated_at = NOW() "
                        "WHERE incident_id = %s", (lifecycle, incident_id))

    def finalize_dispositions(self, incident_id: str, per_wafer: list,
                              approver: Optional[str], approver_role: Optional[str]) -> int:
        """per_wafer 순회 → wafer 처분 행 **잠정→확정 전이** (P6-1 커밋2 · 계약 §4-B/§6).

        행이 있으면(그루퍼 B9 잠정 HOLD 선행) UPDATE — status·system_recommendation·
        recommendation_basis·decided_* 를 확정값으로 갱신. 없으면 INSERT(비-B9 경로 — 확정
        상태로 생성). 단일 트랜잭션·멱등(같은 확정값 재기입 무해 — resume 재시도 안전).
        per_wafer[].reason = C 코드 조립 정형 문구(수치 인용) → recommendation_basis 정본.
        반환 = 처리(전이) 건수.
        """
        if not per_wafer:
            return 0
        n = 0
        with self._txn() as cur:
            cur.execute("SELECT chamber_id FROM incidents WHERE incident_id = %s", (incident_id,))
            row = cur.fetchone()
            chamber_id = row[0] if row else None
            for item in per_wafer:
                wid = item.get("wafer_id")
                rec = item.get("recommendation")
                if not wid or rec not in ("SCRAP", "RELEASE", "HOLD"):
                    log.warning(f"per_wafer 항목 무시 (wafer_id/판정값 무효): {item}")
                    continue
                cur.execute(
                    """UPDATE wafer_dispositions
                          SET status = %s, system_recommendation = %s,
                              recommendation_basis = %s, decided_by = %s, decided_at = NOW()
                        WHERE wafer_id = %s AND incident_id = %s""",
                    (rec, rec, item.get("reason"), approver, wid, incident_id))
                if cur.rowcount == 0:              # 잠정 행 없음(비-B9) → 확정 상태로 생성
                    cur.execute(
                        """INSERT INTO wafer_dispositions
                               (wafer_id, chamber_id, incident_id, status,
                                system_recommendation, recommendation_basis,
                                decided_by, decided_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
                        (wid, chamber_id, incident_id, rec, rec, item.get("reason"), approver))
                n += 1
        return n

    def list_hold_wafers(self, incident_id: str) -> list:
        """이 Incident 의 **미확정 HOLD 잠정 행** wafer_id 목록 (2026-08-10 · 처분 선택용).

        `finalize_dispositions` 의 대상 후보 조회 — 엔지니어가 RELEASE/SCRAP 을 선택했는데
        에이전트 per_wafer 권고가 비어 있을 때(예: B9 비활성 회차) 이 목록이 확정 대상이 된다.
        decided_by IS NULL 조건으로 이미 확정된 행을 재확정하지 않는다 (멱등 안전).
        """
        with self._txn() as cur:
            cur.execute(
                """SELECT wafer_id FROM wafer_dispositions
                    WHERE incident_id = %s AND status = 'HOLD' AND decided_by IS NULL
                    ORDER BY id""", (incident_id,))
            return [r[0] for r in cur.fetchall()]

    def open_pending(self, incident_id: str, request_type: str,
                     selected_option: Optional[str], original_value: dict) -> bool:
        """PENDING 없으면 [INSERT PENDING + lifecycle=pending] 원자 실행(True), 있으면 skip(False).

        interrupt 노드 재실행/동시 승인 요청에 멱등 (헌법 1-4 '동시 1건').

        멱등 방어는 2층이다 (2026-07-31 — `db/migrations/0001` 부분 유니크 인덱스 도입 선행 조건):
          ① SELECT 선검사 — 단일 프로세스·순차 재실행을 걸러내는 통상 경로
          ② UniqueViolation(23505) 흡수 — ①과 INSERT 사이의 **경합 창**은 SELECT 로는
             못 막는다(두 스레드가 동시에 "없음"을 보고 둘 다 INSERT). 인덱스가 걸린 뒤에는
             진 쪽이 예외를 받는데, 그건 실패가 아니라 "다른 요청이 이미 열었다" = False 다.
             인덱스가 아직 없는 DB 에서도 무해하다(예외 자체가 안 난다).
        23505 판별은 pgcode 속성으로 한다 — psycopg2 를 import 하지 않아 테스트 대역(FakeStore)
        격리가 유지된다. 다른 무결성 위반(FK 등)은 삼키지 않고 그대로 올린다.
        """
        # 헌법 1-4 예외 (2026-08-11 PM 결정) — '동시 PENDING 1건' 은 **클래스별**로 센다:
        #   본선(정비/보정) 1건 + 처분(disposition) 1건 은 공존 가능. 처분은 파생 thread
        #   (dispo_thread_id) 로 돌고, 정비 승인과 웨이퍼 확정이 한 클릭에 묶이던 결함의 해소가
        #   이 예외의 존재 이유다. 처분 PENDING 은 본선 lifecycle 을 건드리지 않는다
        #   (pending 전이는 본선 소유 — 처분이 verifying 인 Incident 를 되돌리면 안 된다).
        is_dispo = (request_type == "disposition")
        try:
            with self._txn() as cur:
                cur.execute("SELECT 1 FROM approval_records "
                            "WHERE incident_id = %s AND status = 'PENDING' "
                            "  AND (request_type = 'disposition') = %s", (incident_id, is_dispo))
                if cur.fetchone():
                    return False
                cur.execute(
                    """
                    INSERT INTO approval_records
                        (incident_id, request_type, status, selected_option, original_value)
                    VALUES (%s, %s, 'PENDING', %s, %s::jsonb)
                    """,
                    (incident_id, request_type, selected_option,
                     json.dumps(original_value, ensure_ascii=False)),
                )
                if not is_dispo:
                    cur.execute("UPDATE incidents SET lifecycle = 'pending', updated_at = NOW() "
                                "WHERE incident_id = %s", (incident_id,))
                return True
        except Exception as exc:                      # noqa: BLE001 — 23505 만 흡수, 나머지는 재전파
            if _is_unique_violation(exc):
                # request_type 을 함께 남긴다 (2026-08-11) — 클래스 축이 없는 구 유니크 인덱스
                # (0001)가 걸린 DB 에서는 **처분 행이 본선 행과 충돌**해 여기로 떨어지고, 그러면
                # 카드가 조용히 안 열린다. 마이그레이션 0013 미적용의 유일한 증상이라 로그에
                # 그 안내를 박아 둔다(8/11 실측: 라우트 200 · approval_records 무변화).
                log.info("PENDING 경합 — 이미 열려 있음 (incident=%s · request_type=%s · 23505 흡수). "
                         "처분인데 본선과 충돌하면 db/migrations/0013 미적용을 확인하라.",
                         incident_id, request_type)
                return False
            raise

    def commit_decision(self, incident_id: str, action: str, modified_value: Any,
                        reason: Optional[str], approver: Optional[str], approver_role: Optional[str],
                        new_lifecycle: Optional[str] = None, bump_reopen: bool = False,
                        request_class: str = "main") -> bool:
        """[PENDING→판정 + (옵션)lifecycle 전이 + (옵션)reopen_count++]를 **단일 트랜잭션**으로.

        rowcount>0면 이번 resume가 승자(중복 승인 클릭 멱등 가드). 이벤트는 호출측이 커밋 후 발행.

        request_class (2026-08-11 · 헌법 1-4 예외의 쓰기측): PENDING 이 클래스별 1건씩
        공존하므로 UPDATE 도 클래스로 조준해야 한다 — 안 그러면 처분 승인이 본선(정비)
        PENDING 까지 같이 닫는다 (역방향도 동일).
          · 'main'        = request_type <> 'disposition'  (기존 호출 전부 — 기본값)
          · 'disposition' = request_type =  'disposition'
        """
        status = ACTION_STATUS.get(action, "REJECTED")
        mv = json.dumps(modified_value, ensure_ascii=False) if modified_value is not None else None
        with self._txn() as cur:
            cur.execute(
                """
                UPDATE approval_records
                   SET status = %s, modified_value = %s::jsonb, decision_reason = %s,
                       approver = %s, approver_role = %s, decided_at = NOW()
                 WHERE incident_id = %s AND status = 'PENDING'
                   AND (request_type = 'disposition') = %s
                """,
                (status, mv, reason, approver, approver_role, incident_id,
                 request_class == "disposition"),
            )
            if cur.rowcount == 0:
                return False
            if new_lifecycle:
                if bump_reopen:
                    cur.execute("UPDATE incidents SET lifecycle = %s, "
                                "reopen_count = reopen_count + 1, updated_at = NOW() "
                                "WHERE incident_id = %s", (new_lifecycle, incident_id))
                else:
                    cur.execute("UPDATE incidents SET lifecycle = %s, updated_at = NOW() "
                                "WHERE incident_id = %s", (new_lifecycle, incident_id))
            return True

    def write_modified_fields(self, correction_id: str, fields: dict) -> bool:
        """modify 수정값을 limit_corrections `*_modified`에 기입 — **발행 전 커밋** (순서 규약).

        B `apply_approved`가 COALESCE(수정,원안)으로 읽는 적용 정본 자리 (B5-1 필드규칙 안ⓐ).
        원안(*_after)은 불변. 멱등(같은 값 재기입 무해). 대상 행 없으면 False → 호출측 fail-closed.
        """
        cols = [c for c in fields if c in {"center_modified", "ucl_modified", "lcl_modified"}]
        if not cols:
            return False
        sets = ", ".join(f"{c} = %s" for c in cols)   # cols = 화이트리스트 통과분만 (인젝션 불가)
        with self._txn() as cur:
            cur.execute(f"UPDATE limit_corrections SET {sets} WHERE correction_id = %s",
                        tuple(fields[c] for c in cols) + (correction_id,))
            return cur.rowcount > 0


# ===========================================================================
# 노드 로직 — (state, store, emit) 시그니처, 단위 테스트 대상.
# 모든 이벤트는 DB 커밋 이후 발행.
# ===========================================================================
def merge_reports(state: ApprovalState, store) -> dict:
    """Supervisor 통합 — C Brief 수합·검증·변환 (P5-1 어댑터 시임, 2026-07-20).

    경로 ①: state.report가 이미 게이트 형(또는 빈 값)이면 통과 — 기존 stub 동작 보존.
    경로 ②: state.report가 Brief 원문(supervisor_recommendation 보유)이면 검증→변환.
    경로 ③: report 부재 시 agent_reports에서 SUP 행 수합 — store.read_conn(pool/단일 공통,
              2026-07-31) 또는 구 store.conn 시임. FakeStore(둘 다 없음)는 stub 통과.
    검증 실패(필수 위반)는 드랍하지 않고 validation_problems로 게이트에 표기 —
    반려/에스컬 결정은 엔지니어 몫 (헌법 1-1, ADR-31 드랍 정책).
    """
    from . import supervisor_adapter as sup  # 지연 import — 단위 테스트 격리 유지
    report = state.get("report") or {}
    # 처분 파생 thread (2026-08-11) — report 완제품이 실려 오고, 본선 lifecycle 은 처분이
    # 소유하지 않는다 ('analyzing' 되돌림 금지 — 본선이 verifying 중일 수 있다). 즉시 통과.
    if report.get("request_type") == "disposition":
        return {"report": report}
    store.set_lifecycle(state["incident_id"], "analyzing")

    if "supervisor_recommendation" in report:            # 경로 ② — Brief 원문
        problems = sup.validate_brief(report)
        gate = sup.to_gate_report(report,
                                  (report.get("parallel_options") or {}).get("limit_option"),
                                  (report.get("parallel_options") or {}).get("recipe_option"))
        if problems:
            log.warning(f"Brief 검증 위반 {len(problems)}건 — 게이트에 표기 후 진행: {problems}")
            gate["validation_problems"] = problems
        return {"report": gate}

    if not report:                                                # 경로 ③ — DB 수합
        bundle = None
        borrow = getattr(store, "read_conn", None)                # ApprovalStore — pool/단일 공통
        legacy = getattr(store, "conn", None)                     # 구 시임 (CLI 몽키패치) 호환
        try:
            if callable(borrow):
                with borrow() as rconn:
                    bundle = sup.fetch_bundle(rconn, state["incident_id"])
            elif legacy is not None:
                bundle = sup.fetch_bundle(legacy, state["incident_id"])
        except Exception as e:                                    # 수합 실패 = stub 통과 (M2 알파 보호)
            log.warning(f"agent_reports 수합 실패 — stub 경로 유지: {e}")
            bundle = None
        if bundle:
            brief, lim, rcp = bundle
            problems = sup.validate_brief(brief)
            gate = sup.to_gate_report(brief, lim, rcp)
            if problems:
                gate["validation_problems"] = problems
            return {"report": gate}

    return {"report": report}                                     # 경로 ① — stub 통과


def gate_ensure(state: ApprovalState, store, emit) -> bool:
    """approval_gate 전처리 — [PENDING+pending] 원자 생성(멱등) 후 ApprovalRequested 발행."""
    inc = state["incident_id"]
    report = state.get("report", {}) or {}
    option = report.get("selected_option")
    created = store.open_pending(inc, request_type_for(option), option, report)
    if created:
        emit("ApprovalRequested", {"incident_id": inc, "selected_option": option})
    return created


def gate_parse(state: ApprovalState, resume_value: Optional[dict]) -> dict:
    """resume 페이로드(계약 §6) → state. 미지 action은 경고 후 fail-closed(반려)."""
    rv = resume_value or {}
    action = rv.get("action")
    if action not in VALID_ACTIONS:
        log.warning(f"미지/누락 action '{action}' — 반려로 fail-closed (VALID={VALID_ACTIONS})")
    return {"decision": action, "modified_value": rv.get("modified_value"),
            "reanalyze": rv.get("reanalyze", True), "reason": rv.get("reason"),
            "approver": rv.get("approver"), "approver_role": rv.get("approver_role"),
            "disposition": rv.get("disposition"),
            # 처분 카드 — 승인 시점에 엔지니어가 **웨이퍼별로** 고친 판정 (2026-08-11).
            # [{wafer_id, disposition}] · 카드에 실린 목록 위에 덮어쓴다(apply_disposition).
            "per_wafer": rv.get("per_wafer")}


def apply_correction(state: ApprovalState, store, emit) -> dict:
    """approve/modify — [수정값 선기입(modify)] → [판정+actioned 원자 커밋] → 적용 신호 발행 → verifying.

    순서 규약 (B5-1 필드규칙, 2026-07-23): `*_modified` 기입 커밋이 fdc.correction 발행보다
    **반드시 앞** — 뒤집히면 B가 원안을 적용하는 레이스. 여기선 판정 커밋보다도 앞에 두어
    기입 실패 시 PENDING 유지(fail-closed) → 엔지니어 재시도 가능.

    P6-1 (2026-07-28): 발행 직후 actioned → **verifying** 전이 — 조치가 나간 시점부터
    효과 관측 창. 이후 무재발 verify_close_sec 경과 시 grouper sweep이 closed,
    관측 창 내 같은 챔버 알람 재병합 시 grouper가 reopened 전이 (B6 재발 감시).
    actioned는 판정 커밋에 감사 기록으로 남고 lifecycle상 순간 상태.
    """
    inc = state["incident_id"]
    action = state.get("decision", "approve")
    report = state.get("report") or {}
    option = report.get("selected_option")
    if is_disposition_state(state):                   # 처분 전용 카드 (2026-08-11) — 아래 본선 경로와 완전 분리
        return apply_disposition(state, store, emit)
    if action == "modify":
        fields = resolve_modified_fields(option, state.get("modified_value"))
        if fields:
            cid = (report.get("limit_analysis") or {}).get("correction_id")
            writer = getattr(store, "write_modified_fields", None)
            if writer is None:
                log.warning("store가 write_modified_fields 미지원 — 수정값 DB 미기입 (구형 stub 호환, 페이로드만)")
            elif not cid:
                log.error(f"modify인데 correction_id 부재 — 발행 중단 (fail-closed, PENDING 유지): {inc}")
                return {"result": {"blocked": "modify without correction_id"}}
            elif not writer(cid, fields):
                log.error(f"수정값 기입 실패 — 발행 중단 (fail-closed, PENDING 유지·재시도 가능): {cid}")
                return {"result": {"blocked": "modified_fields write failed"}}
    if not store.commit_decision(inc, action, state.get("modified_value"), state.get("reason"),
                                 state.get("approver"), state.get("approver_role"),
                                 new_lifecycle="actioned"):
        log.info(f"이미 판정됨 — apply 스킵 (멱등): {inc}")
        return {"result": {"skipped": "already decided"}}
    target = apply_target(option)
    # 🔴 2026-08-11 분리 — 여기 있던 「manual(정비) 승인 → per_wafer 처분 확정」 블록을 뗐다.
    #   그 블록 때문에 정비 승인 한 번이 (a) 정비 조치 발행 + (b) 웨이퍼 처분 확정을 동시에
    #   수행했다: 처분만 하고 싶어도 못 하고, 정비를 승인하면 처분이 자동으로 끌려갔으며,
    #   확정 판정은 Incident 단위 1개 값이라 **웨이퍼별 개별 처분이 원리적으로 불가**했다.
    #   이제 처분은 S6 발원 → `disposition_option` 전용 카드 → `apply_disposition` 한 경로만
    #   원장을 움직인다 (계약 §4-B 의 '누가 확정하나' 는 유지, '언제 묶여 확정되나' 만 분리).
    if option == "manual_option":
        held = len((getattr(store, "list_hold_wafers", lambda _i: [])(inc)) or [])
        log.info("정비 승인 — 처분은 확정하지 않는다 (전용 카드 경로). 미확정 HOLD %d건: %s", held, inc)
    payload = build_correction_payload(inc, option, report=report, modified_value=state.get("modified_value"))
    event = "CorrectionApplied" if target.get("topic") == "fdc.correction" else target.get("event", "Applied")
    emit(event, payload)
    emit("ApprovalCompleted", {"incident_id": inc, "action": action})
    store.set_lifecycle(inc, "verifying")   # P6-1 — 발행 완료 = 관측 창 시작 (docstring 참조)
    return {"result": target}


def apply_disposition(state: ApprovalState, store, emit) -> dict:
    """처분 전용 카드 승인 — 엔지니어가 **웨이퍼별로** 고른 판정을 원장에 확정한다 (2026-08-11).

    본선(apply_correction)과 분리된 이유는 그 함수 주석에 있다. 여기서 하는 일만 적으면:
      ① 판정 커밋 — `request_class='disposition'` 으로 조준(본선 PENDING 을 건드리지 않는다).
      ② `report.wafer_disposition.per_wafer` 확정 — 이 목록은 S6 가 만들고 카드에 실려 온
         **웨이퍼별 선택**이다(장별 RELEASE/SCRAP 혼재 가능). 확정 SQL 은 본선과 동일 함수.
      ③ `WaferDispositioned` 발행 + ApprovalCompleted.
    lifecycle 은 전이하지 않는다 — 처분은 장비 조치가 아니라 재공 판정이고, 본선이
    verifying/closed 인 Incident 를 처분이 되돌리면 감사 이력이 뒤집힌다.

    확정 실패는 발행 전에 걸러 **PENDING 을 유지**한다(fail-closed): 처분은 SCRAP 이 섞이면
    비가역이라, "승인은 났는데 원장은 안 움직인 상태"를 조용히 남기면 안 된다.
    """
    inc = state["incident_id"]
    action = state.get("decision", "approve")
    report = state.get("report") or {}
    pw = ((report.get("wafer_disposition") or {}).get("per_wafer")) or []
    # 승인 시점 엔지니어 판정 덮어쓰기 (2026-08-11 · PM: "결정은 승인 화면에서") —
    #   카드는 에이전트 권고를 프리필해 열리고, 엔지니어가 근거(02)를 보면서 장별로 바꾼다.
    #   바뀐 장은 사유에 **요청 시점 판정과 승인 시점 판정을 함께** 남긴다(감사 추적).
    overrides = {}
    for x in (state.get("per_wafer") or []):
        wid = str((x or {}).get("wafer_id") or "").strip()
        dp = str((x or {}).get("disposition") or "").strip().upper()
        if wid and dp in ("RELEASE", "SCRAP", "HOLD"):
            overrides[wid] = dp
    # 전건 동일값(구 `disposition` 필드 · CLI/스크립트 경로) — per_wafer 가 없을 때만 적용.
    #   게이트웨이가 "확정값 없는 처분 승인"을 막으므로, 여기 오는 blanket 은 명시적 선택이다.
    #   화면(정본)은 per_wafer 를 보낸다.
    _blanket = str(state.get("disposition") or "").strip().upper()
    if not overrides and _blanket in ("RELEASE", "SCRAP"):
        overrides = {str(it.get("wafer_id") or ""): _blanket for it in pw}
    log.info("처분 확정 입력 — 카드 %d장 · 덮어쓰기 %d건 %s", len(pw), len(overrides), inc)
    if overrides:
        merged = []
        for it in pw:
            wid = str(it.get("wafer_id") or "")
            dp = overrides.get(wid)
            if dp and dp != it.get("recommendation"):
                merged.append({**it, "recommendation": dp,
                               "reason": f"승인 시 엔지니어 확정({dp}) — 요청 시점 {it.get('recommendation')}"
                                         + (f" · {it.get('reason')}" if it.get("reason") else "")})
            else:
                merged.append(it)
        pw = merged
    # **부분 확정** (2026-08-11 PM) — 카드에 5장이 실렸어도 그중 일부만 확정하고 나머지는
    #   판단을 미룰 수 있어야 한다("3장만 처리하고 싶을 수도 있잖아"). 'HOLD' 로 표시된 장은
    #   확정 대상에서 빼고 **잠정 행 그대로 남긴다**(decided_by 미기입) — finalize 에 HOLD 를
    #   그대로 넘기면 status='HOLD' + decided_by 기입, 즉 "보류를 확정" 이 되어 다시 못 만진다.
    #   카드는 닫히므로 남은 장은 S6 에서 새 카드로 다시 올린다(Incident 당 처분 1건 유지).
    held_back = [str(it.get("wafer_id")) for it in pw
                 if str(it.get("recommendation") or "").upper() == "HOLD"]
    pw = [it for it in pw if str(it.get("recommendation") or "").upper() != "HOLD"]
    if not pw:
        log.error("처분 카드 확정 대상 0건 (전부 보류/빈 목록) — PENDING 유지: %s", inc)
        return {"result": {"blocked": "no wafer to finalize", "held_back": held_back}}
    fin = getattr(store, "finalize_dispositions", None)
    if fin is None:
        log.error("store 가 finalize_dispositions 미지원 — 처분 확정 불가: %s", inc)
        return {"result": {"blocked": "no finalize_dispositions"}}
    if not store.commit_decision(inc, action, None, state.get("reason"),
                                 state.get("approver"), state.get("approver_role"),
                                 request_class="disposition"):
        log.info("이미 판정됨 — 처분 apply 스킵 (멱등): %s", inc)
        return {"result": {"skipped": "already decided"}}
    try:
        n = fin(inc, pw, state.get("approver"), state.get("approver_role"))
    except Exception:                                  # noqa: BLE001 — 판정은 커밋됐고 원장만 실패
        log.exception("처분 확정 실패 — 잠정 행 유지, 수동 재실행 필요: %s", inc)
        return {"result": {"blocked": "finalize failed", "decided": True}}
    counts: dict = {}
    for it in pw:
        counts[it.get("recommendation")] = counts.get(it.get("recommendation"), 0) + 1
    log.info("🧾 wafer 처분 확정 %d건 %s — %s (엔지니어 %s)%s", n, counts, inc, state.get("approver"),
             f" · 보류 {len(held_back)}장 (잠정 유지)" if held_back else "")
    emit("WaferDispositioned", {"incident_id": inc, "finalized": n, "counts": counts,
                                "held_back": held_back,
                                "approved_by": state.get("approver")})
    emit("ApprovalCompleted", {"incident_id": inc, "action": action})
    return {"result": {"finalized": n, "counts": counts, "held_back": held_back,
                       "table": "wafer_dispositions"}}


def reject_and_learn(state: ApprovalState, store, emit) -> dict:
    """reject — [판정 + lifecycle] 커밋 후 사유 기록 + RAG 재투입(seam). 기획서 line 918.

    거취 = **엔지니어 선택**(엔지니어 주도, 2026-07-13): reanalyze=True→analyzing(재분석),
    False→closed(오탐/조치불필요). 자동 루프가드 없음 — 반복 여부는 사람이 통제.

    처분 카드 반려(2026-08-11)는 **원장 무변화 + lifecycle 무전이** 다 — HOLD 잠정 행이
    그대로 남아 다시 판단할 수 있어야 하고, 본선 진행 상태를 처분이 되돌리면 안 된다.
    """
    inc = state["incident_id"]
    dispo = is_disposition_state(state)
    reject_lifecycle = None if dispo else ("analyzing" if state.get("reanalyze", True) else "closed")
    if not store.commit_decision(inc, "reject", None, state.get("reason"),
                                 state.get("approver"), state.get("approver_role"),
                                 new_lifecycle=reject_lifecycle,
                                 request_class="disposition" if dispo else "main"):
        return {"result": {"skipped": "already decided"}}
    emit("RejectionLearned", {"incident_id": inc, "reason": state.get("reason")})
    emit("ApprovalCompleted", {"incident_id": inc, "action": "reject"})
    return {"result": {"rejected": True, "scope": "disposition" if dispo else "main"}}


def escalate(state: ApprovalState, store, emit) -> dict:
    """escalate — [판정+reopened+reopen_count++] 원자 커밋 후 근거 패키지 발행 (기획서 line 673).

    처분 카드 에스컬레이션(2026-08-11)은 lifecycle·reopen_count 를 건드리지 않는다 —
    "이 웨이퍼는 내가 못 정한다"는 신호이지 Incident 재개가 아니다.
    """
    inc = state["incident_id"]
    dispo = is_disposition_state(state)
    if not store.commit_decision(inc, "escalate", None, state.get("reason"),
                                 state.get("approver"), state.get("approver_role"),
                                 new_lifecycle=None if dispo else "reopened",
                                 bump_reopen=not dispo,
                                 request_class="disposition" if dispo else "main"):
        return {"result": {"skipped": "already decided"}}
    emit("Escalated", {"incident_id": inc})            # 근거 패키지 → 공정팀 seam
    emit("ApprovalCompleted", {"incident_id": inc, "action": "escalate"})
    return {"result": {"escalated": True, "scope": "disposition" if dispo else "main"}}


# ===========================================================================
# 그래프 조립 — langgraph 지연 import (프록시 차단 환경 대비).
# ===========================================================================
def build_graph(store: "ApprovalStore", checkpointer, emit: Callable = emit_event):
    """기획서 §6-5 스켈레톤 조립 + A안(4액션·escalate). **checkpointer는 caller가 주입**.

    체크포인터 lifecycle은 caller가 관리한다 (내부 자동생성 금지 — 커넥션 누수/setup 누락 방지).

    prod (Postgres):
        from langgraph.checkpoint.postgres import PostgresSaver
        with PostgresSaver.from_conn_string(DB_URI) as cp:
            cp.setup()                       # 최초 1회 — 체크포인터 테이블 생성
            app = build_graph(store, cp)
            app.invoke(initial_state, {"configurable": {"thread_id": incident_id}})  # with 블록 안에서
    test:
        from langgraph.checkpoint.memory import MemorySaver
        app = build_graph(store, MemorySaver())
    """
    if checkpointer is None:
        raise ValueError("checkpointer 필수 — PostgresSaver를 `with` 컨텍스트로 관리해 주입 "
                         "(docstring 참조). 내부 자동생성은 커넥션 lifecycle 누수 위험이라 금지.")
    from langgraph.graph import StateGraph, START, END      # 지연 import
    from langgraph.types import interrupt

    def _merge(s: ApprovalState) -> dict:
        return merge_reports(s, store)

    def _gate(s: ApprovalState) -> dict:
        gate_ensure(s, store, emit)                          # 멱등: PENDING 1회 + ApprovalRequested
        resume_value = interrupt({"type": "approval",
                                  "incident_id": s["incident_id"],
                                  "report": s.get("report", {})})
        return gate_parse(s, resume_value)

    def _apply(s: ApprovalState) -> dict:
        return apply_correction(s, store, emit)

    def _reject(s: ApprovalState) -> dict:
        return reject_and_learn(s, store, emit)

    def _esc(s: ApprovalState) -> dict:
        return escalate(s, store, emit)

    g = StateGraph(ApprovalState)
    g.add_node("merge_reports", _merge)
    g.add_node("approval_gate", _gate)
    g.add_node("apply_correction", _apply)
    g.add_node("reject_and_learn", _reject)
    g.add_node("escalate", _esc)
    g.add_edge(START, "merge_reports")
    g.add_edge("merge_reports", "approval_gate")
    g.add_conditional_edges(
        "approval_gate",
        lambda s: route_decision(s.get("decision")),
        {"apply_correction": "apply_correction",
         "reject_and_learn": "reject_and_learn",
         "escalate": "escalate"},
    )
    g.add_edge("apply_correction", END)
    g.add_edge("reject_and_learn", END)
    g.add_edge("escalate", END)
    return g.compile(checkpointer=checkpointer)


def start_incident(app, incident_id: str, report: dict) -> None:
    """트리거 — 리포트 3종 수합 완료 시 그래프 시작 (approval_gate에서 interrupt로 멈춤).

    실 배선: C의 agent_service가 '리포트 3종 완료' 신호를 주면 이 함수를 호출 (지금은 진입점 seam).
    app = build_graph(...) 결과, report = Supervisor Brief(selected_option 포함).
    """
    app.invoke({"incident_id": incident_id, "report": report or {}},
               {"configurable": {"thread_id": incident_id}})


def start_disposition(app, incident_id: str, report: dict) -> None:
    """처분 전용 카드 시작 — 본편 그래프 + **파생 thread** (2026-08-11, 헌법 1-4 예외).

    report 는 게이트웨이가 조립한 완제품이다: `request_type='disposition'` ·
    `selected_option='disposition_option'` · `wafer_disposition.per_wafer[]`(웨이퍼별 선택) ·
    Brief 필드(LLM 처분 요약). merge_reports 는 이 request_type 을 보고 즉시 통과한다.
    """
    r = dict(report or {})
    r["request_type"] = "disposition"
    r.setdefault("selected_option", "disposition_option")
    app.invoke({"incident_id": incident_id, "report": r},
               {"configurable": {"thread_id": dispo_thread_id(incident_id)}})
