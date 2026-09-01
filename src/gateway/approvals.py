# -*- coding: utf-8 -*-
"""P4-1 승인 API 코어 (gateway) — 결정 검증 + 그래프 재개(resume) + pending 목록.

fastapi 무의존(라우트는 main.py). langgraph는 resume에서 지연 import → `validate_decision`은
무의존 단위 테스트 가능. psycopg2 conn은 주입받는다(크로스-프로세스 resume = 공유 체크포인터).

흐름: UI → POST /approvals/{incident_id} → validate_decision → resume_decision(그래프 재개)
      → approval_gate의 interrupt가 그 자리에서 이어짐 → apply/reject/escalate 노드.
"""

import logging

from ..orchestrator.approval_graph import VALID_ACTIONS   # 4액션 단일 소스(approve/modify/reject/escalate)

log = logging.getLogger("gateway.approvals")


def validate_decision(body: dict) -> dict:
    """승인 요청 body → Command(resume) 페이로드. 잘못되면 ValueError (라우트가 400으로 변환).

    필수: action ∈ VALID_ACTIONS, approver(감사 추적·헌법 1-1). modify는 modified_value 동반.
    """
    body = body or {}
    action = body.get("action")
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)} (got {action!r})")
    if not body.get("approver"):
        raise ValueError("approver required (감사 추적, 헌법 1-1)")
    if action == "modify":
        mv = body.get("modified_value")
        if mv is None:
            raise ValueError("modify requires modified_value")
        if isinstance(mv, bool) or not isinstance(mv, (int, float, dict)):
            raise ValueError("modified_value는 숫자(center 절대값) 또는 {center|ucl|lcl: 숫자} (B5-1 필드규칙)")
        if isinstance(mv, dict):
            bad = [k for k, v in mv.items()
                   if k not in ("center", "ucl", "lcl") or isinstance(v, bool) or not isinstance(v, (int, float))]
            if bad:
                raise ValueError(f"modified_value 허용 키 center/ucl/lcl·값 숫자만: {bad}")
    if action == "reject" and not body.get("reason"):
        raise ValueError("reject requires reason (거부 사유 필수 — 엔지니어 주도)")
    # 처분 선택 (2026-08-10 · PM) — disposition 승인에서 엔지니어가 RELEASE/SCRAP 을 고른다.
    #   선택이 오면 그래프가 에이전트 per_wafer 권고를 이 값으로 덮어쓰고, 권고가 비어 있으면
    #   그 Incident 의 HOLD 잠정 행 전건에 이 판정을 적용한다 (approval_graph.apply_correction).
    #   없으면 기존 동작(권고안 그대로) — 하위 호환. approve 에서만 유효(수정/반려/에스컬레이션은
    #   처분 확정 경로가 아니다).
    dispo = body.get("disposition")
    if dispo is not None:
        if dispo not in ("RELEASE", "SCRAP"):
            raise ValueError("disposition must be RELEASE or SCRAP (엔지니어 처분 선택)")
        if action != "approve":
            raise ValueError("disposition 선택은 approve 에서만 유효 (처분 확정 경로)")
    # 웨이퍼별 처분 (2026-08-11 · PM "결정은 승인 화면에서") — 처분 카드는 에이전트 권고를
    #   프리필해 열리고, 엔지니어가 근거(02)를 보면서 **장별로** 확정값을 고른다. 전건 동일값
    #   토글(위 disposition 필드)로는 5장 중 3 RELEASE·2 SCRAP 같은 실무 케이스가 표현 불가였다.
    per_wafer = body.get("per_wafer")
    if per_wafer is not None:
        if not isinstance(per_wafer, list) or not per_wafer:
            raise ValueError("per_wafer 는 비어 있지 않은 리스트 (처분 카드 전용)")
        if action != "approve":
            raise ValueError("per_wafer 는 approve 에서만 유효 (처분 확정 경로)")
        if len(per_wafer) > 200:
            raise ValueError("per_wafer 는 한 번에 200장까지 (초과분은 나눠 요청)")
        # 'HOLD' 허용 (2026-08-11 PM · 부분 확정) — 카드의 일부만 확정하고 나머지는 판단을
        #   미루는 경우다. 그래프가 HOLD 장을 확정 대상에서 빼고 잠정 행을 그대로 남긴다.
        #   전부 HOLD 면 그래프가 fail-closed(확정 대상 0건 → PENDING 유지).
        seen: set = set()
        for it in per_wafer:
            wid = str((it or {}).get("wafer_id") or "").strip()
            dp = str((it or {}).get("disposition") or "").strip().upper()
            if not wid or dp not in ("RELEASE", "SCRAP", "HOLD"):
                raise ValueError(f"per_wafer 항목은 {{wafer_id, disposition∈RELEASE|SCRAP|HOLD}}: {it!r}")
            if wid in seen:
                raise ValueError(f"per_wafer 에 같은 wafer_id 중복: {wid}")
            seen.add(wid)
        if all(str((it or {}).get("disposition") or "").strip().upper() == "HOLD" for it in per_wafer):
            raise ValueError("전 장 보류 — 확정할 웨이퍼가 없습니다 (승인 대신 요청 취소를 쓰세요)")
    payload = {"action": action, "approver": body.get("approver"),
               "approver_role": body.get("approver_role"), "reason": body.get("reason")}
    if action == "modify":
        payload["modified_value"] = body.get("modified_value")
    if action == "reject":
        payload["reanalyze"] = bool(body.get("reanalyze", True))   # 재분석/종료 = 엔지니어 선택
    if dispo is not None and action == "approve":
        payload["disposition"] = dispo
    if per_wafer is not None and action == "approve":
        payload["per_wafer"] = [{"wafer_id": str(it["wafer_id"]).strip(),
                                 "disposition": str(it["disposition"]).strip().upper()}
                                for it in per_wafer]                       # RELEASE|SCRAP|HOLD
    return payload


def resume_decision(graph_app, incident_id: str, resume_payload: dict) -> dict:
    """그래프를 thread_id=incident_id로 재개(Command(resume)). 공유 체크포인터라 시작 프로세스와 달라도 OK."""
    from langgraph.types import Command                 # 지연 import
    cfg = {"configurable": {"thread_id": incident_id}}
    return graph_app.invoke(Command(resume=resume_payload), cfg)


def has_resume_point(graph_app, thread_id: str) -> bool | None:
    """이 thread에 **재개 지점이 남아 있는가** (interrupt 대기 중 = True).

    🔴 닫힌 thread(승인 완료)·미접촉 thread는 `get_state().next` 가 비어 있다. LangGraph 는
    재개 지점이 없는 thread 에 `Command(resume)` 을 보내도 **예외 없이 조용히 통과**하고
    과거 상태를 그대로 돌려준다 (C 진단 프로브 실증 — PR #71, 2026-07-30). 따라서
    예외 기반 가드(`except -> 409`)로는 못 잡는다. "에러가 안 나는 것"이 증상이다.

    조회 자체가 실패하면 None — 호출자가 판정을 보류한다 (가드가 정상 승인을 막지 않도록).
    """
    try:
        st = graph_app.get_state({"configurable": {"thread_id": thread_id}})
    except Exception:                                  # noqa: BLE001 — 판정 보류(가드 fail-open)
        return None
    return bool(getattr(st, "next", ()) or ())


def fetch_pending(conn, pending_timeout_sec: float | None = None,
                  cum_warn_sigma: float | None = None,
                  k_sigma: float | None = None) -> list:
    """대시보드 승인 대기 목록 — approval_records PENDING + incidents 조인 (우선순위순).

    P6-1 에이징(2026-07-28, PM 결정: **표시만·자동 조치 없음** — 1-1 HITL): 각 행에
    `age_sec`(요청 후 경과 초)와, pending_timeout_sec 주입 시 `is_aged`(초과 여부)를 동봉.
    UI(S3)가 배지로 노출 — 상태 전이·자동 escalate는 하지 않는다.
    incident_type(P6-1)도 동봉 — S3/S6 분기 소스(crazy_spot=처분 건).

    2026-07-31 추가 — 표시 정합 2종 (리허설 실측: S4 가 score 2100/1072 를 보여줌):
    · `context_score` = incident_alerts max — 화면의 score 는 이걸 쓴다. 구 폴백이던
      priority_score 는 정렬용 합성값(100 초과)이라 점수 자리에 노출되면 오독된다.
    · `brief_*` = agent_reports 최신 SUP 행 LATERAL — 경로③이 죽어 있던 구간(2026-07-31
      이전 게이트웨이 pool 빌드)의 PENDING 은 original_value 가 {} 라, 카드 제목뿐 아니라
      상세 패널(02 판정·03 옵션)도 이 동봉분으로 S4 가 Brief 를 재조립한다.
      brief_selected = supervisor_recommendation 컬럼(선택 옵션명 — verdict 아님).

    2026-08-07 추가 — **A-warn 누적 표류 배지**(`attach_cum_drift`): `cum_from_initial_sigma`·
    `cum_worst_sensor`·`cum_warn_sigma`·`is_cum_warned` 동봉. 승인마다 리셋되는 A3 누적 가드가
    못 보는 "v1 대비 총 표류"를 S4 가 배지로 노출한다. **비차단**(표시만 — 헌법 1-1).

    2026-08-11 추가 — `record_id`(approval_records.id): 이제 한 Incident 에 **본선 카드와
    처분 카드가 공존**한다(헌법 1-4 예외). 화면이 카드를 incident_id 로 식별하면 두 카드가
    같은 키를 갖고, 목록에서 앞선 하나만 선택돼 나머지는 **클릭조차 불가**해진다 (8/11 실측:
    처분 요청 후 자동 이동했는데 정비 카드가 열렸다). 카드 고유 키를 내려보내 그걸 막는다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ar.id AS record_id,                      -- 2026-08-11 · 카드 고유 키
                   ar.incident_id, i.chamber_id, i.incident_type, ar.request_type,
                   ar.selected_option, ar.requested_at, i.priority_score, i.severity_max,
                   ar.original_value,
                   EXTRACT(EPOCH FROM (NOW() - ar.requested_at)) AS age_sec,
                   COALESCE((SELECT max(ia.context_score) FROM incident_alerts ia
                              WHERE ia.incident_id = ar.incident_id), 0) AS context_score,
                   sup.supervisor_reason         AS brief_reason,
                   sup.supervisor_recommendation AS brief_selected,
                   sup.supervisor_confidence     AS brief_confidence,
                   sup.recipe_option             AS brief_recipe_option,
                   sup.limit_option              AS brief_limit_option,
                   sup.manual_option             AS brief_manual_option,
                   sup.supervisor_verdict        AS brief_verdict,
                   sup.supervisor_evidence      AS brief_evidence,
                   sup.supervisor_counter        AS brief_counter
              FROM approval_records ar
              JOIN incidents i ON i.incident_id = ar.incident_id
              LEFT JOIN LATERAL (SELECT ar2.supervisor_reason, ar2.supervisor_recommendation,
                                        ar2.supervisor_confidence, ar2.recipe_option,
                                        ar2.limit_option, ar2.manual_option,
                                        ar2.supervisor_verdict, ar2.supervisor_evidence,
                                        ar2.supervisor_counter
                                   FROM agent_reports ar2
                                  WHERE ar2.incident_id = ar.incident_id
                                    AND ar2.report_id LIKE 'SUP-%'
                                  ORDER BY ar2.id DESC LIMIT 1) sup ON TRUE
             WHERE ar.status = 'PENDING'
             ORDER BY i.priority_score DESC NULLS LAST, ar.requested_at
            """
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["age_sec"] = float(r["age_sec"]) if r.get("age_sec") is not None else None
        if pending_timeout_sec is not None and r["age_sec"] is not None:
            r["is_aged"] = r["age_sec"] >= float(pending_timeout_sec)
    attach_cum_drift(rows, warn_sigma=cum_warn_sigma, k_sigma=k_sigma)
    attach_ct2_brief(rows)
    return rows


# ── CT²(AE) 배포 승인 브리프 평탄화 (2026-08-09 — 기준 개정 v2 §7) ────────────
CT2_REQUEST_TYPE = "ct2_deploy"


def attach_ct2_brief(rows: list) -> None:
    """`request_type='ct2_deploy'` 행에 게이트 브리프 요약을 **평탄한 키**로 동봉한다.

    왜 필요한가: CT² 배포 승인의 근거(게이트 리포트)는 `original_value.report` 에 통째로
    들어 있는데, 그동안 그것을 읽는 코드가 **어디에도 없었다** — 승인 화면에 뜨는 것은
    `승인 대기 · <번들명>` 한 줄뿐이라, 엔지니어는 **어떤 항목이 완화된 채 PASS 했는지**
    (헌법 3-3 ③ ⓑ)를 볼 방법이 없었다. `validate_bundle` 이 "승인 브리프에 노출된다"고
    적어 둔 `_weakened` 는 실제로는 stdout 로그에만 있었다.

    문구·번역은 **여기서 만들지 않는다.** 게이트 항목·임계의 단일 소스가
    `validate_bundle.py` 이므로 사람이 읽는 라벨도 거기서 만들어 `report.approval_brief`
    로 흘려보낸다 — 여기가 번역을 시작하면 임계를 바꿀 때 두 곳을 고쳐야 하고,
    **안 고쳐도 아무 일도 일어나지 않는다** (헌법 7장 "신설하고 하류를 안 따라감").

    구 리포트(`approval_brief` 이전 schema v2 이하)는 브리프가 없다 — 그 사실을
    `ct2_brief_missing` 으로 남긴다. 조용히 빈 화면을 보여주면 "완화 0건"으로 오독된다.
    """
    for r in rows:
        if r.get("request_type") != CT2_REQUEST_TYPE:
            continue
        ov = r.get("original_value")
        rep = (ov or {}).get("report") if isinstance(ov, dict) else None
        if not isinstance(rep, dict):
            r["ct2_brief_missing"] = "게이트 리포트 없음 — 승인 근거 미상 (배포 보류 권고)"
            continue
        r["ct2_bundle"] = rep.get("bundle_name")
        r["ct2_verdict"] = rep.get("gate_verdict")
        r["ct2_schema_version"] = rep.get("gate_schema_version")
        r["ct2_failed_checks"] = rep.get("failed_checks") or []
        r["ct2_skipped_checks"] = rep.get("skipped_checks") or []
        ab = rep.get("approval_brief")
        if isinstance(ab, dict):
            r["ct2_brief"] = ab
        else:
            r["ct2_brief_missing"] = (
                f"구 게이트 리포트(schema v{rep.get('gate_schema_version')}) — 약화 조건·경계 "
                "라벨이 없다. 재채점 후 승인할 것 (기준 개정 v2 §7)")


# ── A-warn: v1 initial 대비 누적 표류 (비차단 경고 — S4 배지) ────────────────
#   B(`correction_history.cum_from_initial`)가 계산을 제공하고 표시층이 소비한다는 계약
#   (2026-07-31 B 명시). 여기가 그 호출자다.
#
#   왜 필요한가: 기존 누적 가드(A3 `reset_cum_max_sigma_per_cycle`)는 **reset_ref 기준**이라
#   승인·incident·qual 마다 기준이 갱신돼 누적이 리셋된다. 그래서 "매번 조금씩 승인했는데
#   총합으로는 크게 밀린" 상황을 못 본다 (B 실측: SIM_CH_3/C11 **11회 승인 → 3.7σ**).
#   A-warn 은 항상 v1 initial 고정 기준이라 그 총 표류를 잡는다.
#
#   **비차단**(헌법 1-1 HITL): 승인을 막지 않고 배지만 띄운다. 판단은 엔지니어 몫이며,
#   조회 실패는 목록 자체를 죽이지 않는다(fail-open — `is_aged` 에이징 표시와 같은 성격).
_PROPOSED_CORRECTIONS_SQL = """
    SELECT incident_id, chamber_id, recipe_id, step, sensor_window, sensor_id,
           COALESCE(center_modified, center_after) AS center
      FROM limit_corrections
     WHERE status = 'PROPOSED' AND incident_id = ANY(:ids)
"""

_cum_engine: list = [None]          # 지연 생성 싱글턴 (None = 미시도 / False = 생성 실패)


def _cum_sa_engine():
    """A-warn 전용 **SQLAlchemy** 엔진 (지연 생성·읽기 전용). 실패하면 None → 배지 생략.

    ⚠️ **왜 별도 엔진인가** (2026-08-07 배선 중 발견): B 의 `cum_from_initial` 은
    SQLAlchemy Connection 계약이다 — 내부 `_fetch` 가 `conn.execute(text(...)).mappings()`
    를 쓴다. 반면 게이트웨이의 `_gate["conn"]` 은 **psycopg2** 커넥션(`conn.cursor()`)이라
    그대로 넘기면 `AttributeError: 'connection' object has no attribute 'execute'` 가 난다.
    그리고 이 함수의 예외는 배지 생략으로 흡수되므로 **"배지가 영원히 안 뜨는" 형태로만**
    드러난다 — 조용한 실패다. 그래서 타입을 맞춰 별도 엔진을 만든다.
    B 코드(소유: 팀원 B, 헌법 3-1)는 손대지 않는다.
    """
    if _cum_engine[0] is not None:
        return _cum_engine[0] or None
    try:
        import os
        from sqlalchemy import create_engine
        dsn = os.environ.get("DATABASE_URL") or ""
        if not dsn:
            _cum_engine[0] = False
            return None
        if dsn.startswith("postgres://"):            # SQLAlchemy 2 는 postgres:// 를 거부한다
            dsn = "postgresql://" + dsn[len("postgres://"):]
        _cum_engine[0] = create_engine(dsn, pool_size=1, max_overflow=1, pool_pre_ping=True)
        return _cum_engine[0]
    except Exception as e:                           # noqa: BLE001 — 표시용이라 실패해도 목록은 산다
        log.warning("누적 표류 엔진 생성 실패(배지 생략): %s", e)
        _cum_engine[0] = False
        return None


def attach_cum_drift(rows: list, warn_sigma: float | None = None,
                     k_sigma: float | None = None, engine=None) -> list:
    """각 PENDING 행에 v1 대비 누적 표류(σ)를 동봉 — 제자리 수정 후 rows 반환.

    한 Incident 에 관리선 제안이 여러 건(센서별)일 수 있으므로 **최댓값**을 싣는다.
    "이 승인 건에서 가장 많이 밀린 센서"가 배지가 말해야 할 값이기 때문이다.

    동봉 필드 (계약 — 헌법 6-4 snake_case):
      · `cum_from_initial_sigma` : 최대 누적 표류(σ). 산출 불가면 None
      · `cum_worst_sensor`       : 그 최댓값을 만든 sensor_id (배지 툴팁용)
      · `cum_warn_sigma`         : 비교에 쓴 임계 (`limit_engine.reset_cum_from_initial_warn_sigma`)
      · `is_cum_warned`          : 임계 이상 여부 (배지 표시 스위치)

    None 이 정상 값인 경우가 있다 — v1 initial 기저가 없거나(신규 그룹) σ_ref 가 퇴화면
    B 계산이 None 을 돌려준다. **그때는 배지를 띄우지 않는다**(모른다 ≠ 안전하다이지만,
    비차단 경고에서 근거 없는 배지는 신뢰를 깎는다). 산출 실패 건수는 debug 로만 남긴다.
    """
    ids = [r.get("incident_id") for r in rows if r.get("incident_id")]
    for r in rows:                                     # 계약 필드는 항상 존재하게(프론트 분기 단순화)
        r.setdefault("cum_from_initial_sigma", None)
        r.setdefault("cum_worst_sensor", None)
        r["cum_warn_sigma"] = float(warn_sigma) if warn_sigma is not None else None
        r.setdefault("is_cum_warned", False)
    if not ids:
        return rows
    eng = engine if engine is not None else _cum_sa_engine()
    if eng is None:
        return rows
    worst: dict = {}
    try:
        from sqlalchemy import text
        from ..agent_b_spc.correction_history import cum_from_initial   # B 소유·무수정 (3-1)
        with eng.connect() as sconn:
            proposals = [dict(m) for m in
                         sconn.execute(text(_PROPOSED_CORRECTIONS_SQL), {"ids": ids}).mappings()]
            for p in proposals:
                center = p.get("center")
                if center is None:
                    continue
                gk = (p["chamber_id"], p["recipe_id"], p["step"],
                      p["sensor_window"], p["sensor_id"])
                try:
                    sigma = cum_from_initial(sconn, gk, float(center), float(k_sigma or 3.0))
                except Exception as e:                 # noqa: BLE001 — 1그룹 실패가 전체를 죽이지 않게
                    log.debug("누적 표류 산출 실패 %s: %s", gk, e)
                    continue
                if sigma is None:                      # 기저 부재·σ 퇴화 — 배지 없음이 정상
                    continue
                prev = worst.get(p["incident_id"])
                if prev is None or sigma > prev[0]:
                    worst[p["incident_id"]] = (float(sigma), p["sensor_id"])
    except Exception as e:                             # noqa: BLE001 — 표시용이라 실패해도 목록은 산다
        log.warning("누적 표류 조회 실패(배지 생략): %s", e)
        return rows

    for r in rows:
        hit = worst.get(r.get("incident_id"))
        if not hit:
            continue
        r["cum_from_initial_sigma"] = round(hit[0], 2)
        r["cum_worst_sensor"] = hit[1]
        r["is_cum_warned"] = warn_sigma is not None and hit[0] >= float(warn_sigma)
    return rows


# APPLY_FAILED 재처리 (P6-1 — B5-1 스펙 §5-3 "재처리 규약"의 오케스트레이터 몫)
_RETRY_UPDATE_SQL = """
    UPDATE limit_corrections
       SET center_modified = COALESCE(%(center)s, center_modified),
           ucl_modified    = COALESCE(%(ucl)s,    ucl_modified),
           lcl_modified    = COALESCE(%(lcl)s,    lcl_modified),
           status = 'PROPOSED'
     WHERE correction_id = %(cid)s AND status = 'APPLY_FAILED'
 RETURNING incident_id, chamber_id, sensor_id, limit_version_before
"""


def retry_failed_correction(conn, correction_id: str, modified_value: dict,
                            approver: str, approver_role: str | None = None,
                            reason: str | None = None) -> dict | None:
    """APPLY_FAILED correction 재처리 — [*_modified 갱신 + status→PROPOSED + 감사 행] 단일 트랜잭션.

    B5-1 스펙 §5-3 규약(2026-07-24 PM 확정)의 오케스트레이터 구현: 엔지니어가 값을 재입력하면
    되돌리고, **호출자가 fdc.correction을 재발행**한다(B는 PROPOSED+*_modified를 다시 읽어
    재검증 — B 코드 무변경). 감사: approval_records에 MODIFIED 행 추가(재입력도 승인 행위 —
    헌법 1-1). 대상이 APPLY_FAILED가 아니면 None(멱등·경합 안전 — 라우트가 409로 변환).
    commit은 호출자(라우트) 몫이 아니라 여기서 즉시(conn.autocommit 아님 전제 → with conn).
    """
    mv = modified_value or {}
    bad = [k for k, v in mv.items()
           if k not in ("center", "ucl", "lcl") or isinstance(v, bool) or not isinstance(v, (int, float))]
    if bad or not mv:
        raise ValueError(f"modified_value는 {{center|ucl|lcl: 숫자}} 1개 이상: {bad or 'empty'}")
    if not approver:
        raise ValueError("approver required (감사 추적, 헌법 1-1)")
    import json                                       # 표준 lib — 감사 스냅샷 직렬화
    with conn:
        with conn.cursor() as cur:
            cur.execute(_RETRY_UPDATE_SQL, {"cid": correction_id,
                                            "center": mv.get("center"),
                                            "ucl": mv.get("ucl"), "lcl": mv.get("lcl")})
            row = cur.fetchone()
            if row is None:
                return None                            # APPLY_FAILED 아님 (이미 처리/오타)
            incident_id, chamber_id, sensor_id, ver = row
            cur.execute(
                """INSERT INTO approval_records
                       (incident_id, request_type, status, selected_option,
                        modified_value, decision_reason, approver, approver_role, decided_at)
                   VALUES (%s, 'correction', 'MODIFIED', 'limit_option', %s::jsonb, %s, %s, %s, NOW())""",
                (incident_id or correction_id, json.dumps(mv),
                 reason or f"APPLY_FAILED 재입력 ({correction_id})", approver, approver_role))
    return {"correction_id": correction_id, "incident_id": incident_id,
            "chamber_id": chamber_id, "sensor_id": sensor_id, "limit_version": ver}
