# -*- coding: utf-8 -*-
"""P5-1 어댑터 시임 — Supervisor Brief(C 산출, 라이브러리 §4) → 승인 게이트 state.report.

목적: C5-3(팀원 C Supervisor 로직) 도착 시 approval_graph.merge_reports stub을
      "검증 → 변환 → 통과"로 교체하는 지점을 **미리** 완성해 탑재를 시간 단위로 압축.

경계 (RnR·계약):
  · 판단(4지선다·순위·문장)은 C — 이 모듈은 **검증과 변환만** 한다. 수치 재계산 금지('정량은 B').
  · 스키마 정본 = docs/Agent_프롬프트_라이브러리_v1.md §4 (Brief) · §2 (LIM 상세) · §1 (RCP 상세).
  · 게이트 소비 형 = approval_graph.build_correction_payload가 읽는 키:
      report.selected_option / report.limit_analysis{sensor, limit_version_current, center_proposed, correction_id}
      / report.recipe_tuning{parameter_id, value_current, applied_value, delta_pct,
                             value_proposed, recipe_correction_id}
      ※ value_current·applied_value 는 승인 화면(original_value) 표시용이며 계약 §6 페이로드에는
        싣지 않는다 (2026-08-06 — PR #122 리뷰. 상세는 아래 to_gate_report 주석).
  · chamber_id·correction_id (2026-07-21 추가 — C-②·B-② 요청): chamber_id는 Brief 필수
      (agent_reports NOT NULL), correction ID는 B 채번 승계값 (헌법 6-4 — 생성 금지).
  · ADR-31 드랍 정책: 필수 위반 = 드랍(problems 반환 — 호출측이 에스컬/재시도 결정),
      권고 위반 = 경고 로그만. 드랍률 >10% 시 완화 순서 = 재시도 → 정규화 레이어 → 필수/선택 분리.

단위 테스트: tests/test_supervisor_adapter.py (DB·langgraph 무의존).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("supervisor-adapter")

VERDICTS = {"equipment_fault", "process_shift", "baseline_aging", "escalate"}
OPTION_KEYS = ("recipe_option", "limit_option", "manual_option")
# selected 의 값 범위 = **옵션 3종 + "none_executable"** (라이브러리 §4-1, 2026-07-22).
#   · escalate 는 verdict(판정)이지 option(실행안)이 아니다 — parallel_options 에 대응하는
#     escalate_option 이 없으므로 selected 자리에 들어가지 않는다.
#   · "고를 옵션이 없다"는 **null 이 아니라 sentinel 로 명시**된다. C 가 코드로 채우며
#     3경우 모두 정상이다: verdict=escalate / 가드①(실행불가) / 가드②(정합) / §6 fallback.
#   · 따라서 **정상 Brief 는 selected 가 null 이 아니다.** null 이 오면 LLM 이 칸을
#     건너뛴 것이므로 위반으로 잡는 게 맞다 — 침묵을 정상으로 오해하지 않기 위한 설계다.
#     (구 계약에서는 null 이 정상이었다. 하위호환으로 None 도 받되 경고를 남긴다.)
# 라우팅은 selected 단독이 아니라 verdict 와 함께 본다 — verdict 는 항상 채워진다.
SELECTABLE = set(OPTION_KEYS) | {"none_executable", "escalate", None}
_ID_RE = re.compile(r"^SUP-\d{8}-")


# ---------------------------------------------------------------------------
# 1. 검증 — 필수(드랍)와 권고(경고)를 분리 (ADR-31)
# ---------------------------------------------------------------------------
def validate_brief(brief: Any) -> list[str]:
    """Brief 필수 요건 위반 목록 반환 (빈 리스트 = 통과). 권고 위반은 warn 로그만.

    필수: dict 형 / report_id(SUP- 포맷) / incident_id / chamber_id (agent_reports NOT NULL —
          C-② 2026-07-21) / parallel_options 3키 전부 / recommendation.selected ∈ 4지선다
          / verdict ∈ enum / confidence ∈ [0,1].
    권고: 비선택 옵션의 rejected_because / counter_evidence / evidence 2~4개.
    """
    if not isinstance(brief, dict):
        return ["brief가 dict가 아님"]
    problems: list[str] = []

    rid = brief.get("report_id") or ""
    if not _ID_RE.match(str(rid)):
        problems.append(f"report_id 포맷 위반 (SUP-<YYYYMMDD>-... 기대): {rid!r}")
    if not brief.get("incident_id"):
        problems.append("incident_id 누락")
    if not brief.get("chamber_id"):
        problems.append("chamber_id 누락 (agent_reports.chamber_id NOT NULL — 적재 불가, C-② 2026-07-21)")

    po = brief.get("parallel_options")
    if not isinstance(po, dict):
        problems.append("parallel_options 누락/형식 오류")
        po = {}
    for k in OPTION_KEYS:
        if k not in po:
            problems.append(f"parallel_options.{k} 누락 (리포트 3종 병렬 — 헌법 1-4)")

    rec = brief.get("supervisor_recommendation")
    if not isinstance(rec, dict):
        problems.append("supervisor_recommendation 누락")
        rec = {}
    sel = rec.get("selected")
    if sel not in SELECTABLE:
        # sorted() 는 None 과 str 을 비교할 수 없다 — 문자열만 정렬한다.
        expected = sorted(v for v in SELECTABLE if v is not None)
        problems.append(f"selected 미지값: {sel!r} (기대 {expected} 또는 null)")
    elif sel is None:
        # 정상 Brief 는 null 이 아니다(C 가 sentinel 로 채운다). 드랍하진 않되 남긴다 —
        # LLM 이 칸을 건너뛴 신호일 수 있다(구 계약 Brief 면 무해).
        log.warning("selected=null — 구 계약이거나 LLM 누락 (기대: none_executable)")
    verdict = rec.get("verdict")
    if verdict not in VERDICTS:
        problems.append(f"verdict enum 위반: {verdict!r}")
    conf = rec.get("confidence")
    if not (isinstance(conf, (int, float)) and 0.0 <= float(conf) <= 1.0):
        problems.append(f"confidence 범위 위반: {conf!r}")

    # ---- 권고 (경고만 — 드랍 아님) ----
    if isinstance(rec, dict):
        if not rec.get("counter_evidence"):
            log.warning("counter_evidence 없음 — '반증 탐색 실패' 명시 규칙 위반 (라이브러리 §5)")
        ev = rec.get("evidence") or []
        if not (2 <= len(ev) <= 4):
            log.warning(f"evidence {len(ev)}개 — 권고 범위(2~4) 밖")
    for k in OPTION_KEYS:
        o = po.get(k) or {}
        if k != sel and isinstance(o, dict) and not o.get("rejected_because"):
            log.warning(f"{k} 비선택인데 rejected_because 없음 — 기각 사유 권고 위반")
    return problems


# ---------------------------------------------------------------------------
# 2. 변환 — Brief + 리포트 상세(LIM/RCP) → 게이트 state.report
# ---------------------------------------------------------------------------
def to_gate_report(brief: dict, lim_detail: Optional[dict] = None,
                   rcp_detail: Optional[dict] = None) -> dict:
    """게이트/페이로드 빌더가 읽는 내부 형으로 변환. Brief 원문은 brief 키에 보존(대시보드·감사).

    lim_detail/rcp_detail = agent_reports의 옵션 상세(JSONB — 라이브러리 §2/§1 출력 스키마).
    Brief의 옵션 블록은 요약이라 수치 필드는 상세 리포트에서 온다 ('정량은 B' 경로 보존).
    """
    rec = brief.get("supervisor_recommendation") or {}
    sel = rec.get("selected")
    out: dict = {
        "selected_option": sel,
        "verdict": rec.get("verdict"),
        "confidence": rec.get("confidence"),
        "chamber_id": brief.get("chamber_id"),   # C-② (2026-07-21) — alert 승계값
        "brief": brief,                      # 원문 보존 — S4 Brief 렌더·approval_records payload
    }
    lim = lim_detail or {}
    if lim:
        # C 리포트(라이브러리 §2)와 게이트 내부형은 이름이 다르다 — **둘 다 받는다**.
        # 앞이 C 정본 이름, 뒤가 기존 stub·legacy 경로 이름(경로 ① 보존).
        #   sensor_id ↔ sensor · center_after ↔ center_proposed · limit_version ↔ limit_version_current
        # (2026-07-22 C 확인: 이 이름들이 안 맞아 limit_analysis 가 전부 null 이었고,
        #  승인해도 fdc.correction 의 변경 금지 필드 sensor·limit_version 이 비어 나갔다)
        # ⚠️ `or` 폴백을 쓰지 않는다 — falsy-but-valid 값이 사라진다:
        #    center 는 0.0, 문자열은 "" 가 그렇다. "값이 있으나 비었다"와 "키가 없다"는 다르므로
        #    **None 여부**로만 판단한다 (2026-07-22 리뷰 지적).
        def _pick(primary: str, legacy: str) -> Any:
            """C 정본 이름을 우선하되, 없을 때만 게이트 내부형(구 stub·경로 ①)으로 폴백."""
            v = lim.get(primary)
            return v if v is not None else lim.get(legacy)

        out["limit_analysis"] = {
            "sensor": _pick("sensor_id", "sensor"),
            "limit_version_current": _pick("limit_version", "limit_version_current"),
            "center_proposed": _pick("center_after", "center_proposed"),
            "correction_id": lim.get("correction_id"),   # B 채번 승계 (limit_corrections PK — B-② 2026-07-21)
        }
    rcp = rcp_detail or {}
    if rcp:
        # value_current·applied_value 는 **승인 화면 전용**이다 (2026-08-06 추가 — PR #122 리뷰).
        # original_value = 이 report 이므로(open_pending) 여기 실어야 엔지니어가 본다.
        # 계약 §6 `fdc.correction`(build_correction_payload)에는 **넣지 않는다** — B 가 이미
        # 보유한 값이라 불필요하고, 계약 필드 추가는 소비자 리뷰 대상이다(헌법 2-2).
        out["recipe_tuning"] = {
            "parameter_id": rcp.get("parameter_id"),
            "value_current": rcp.get("value_current"),   # 명목값 — 레시피에 적힌 값(누적 계산 기준)
            "applied_value": rcp.get("applied_value"),   # 지금 장비에 실제로 걸린 값. null 이면 명목값만 표시
            "delta_pct": rcp.get("delta_pct"),
            "value_proposed": rcp.get("value_proposed"),
            "recipe_correction_id": rcp.get("recipe_correction_id"),   # RCP 대칭 (recipe_corrections PK)
        }
    return out


# ---------------------------------------------------------------------------
# 3. DB 수합 — agent_reports 1행(Brief 통합형, init.sql 스키마) → (brief, lim, rcp)
# ---------------------------------------------------------------------------
def fetch_bundle(conn, incident_id: str) -> Optional[tuple[dict, Optional[dict], Optional[dict]]]:
    """agent_reports에서 최신 SUP 행을 읽어 (brief, lim_detail, rcp_detail) 반환. 없으면 None.

    C의 적재 계약(§5): 옵션 상세는 recipe_option/limit_option/manual_option JSONB 컬럼.
    Brief 통합 필드가 개별 컬럼(supervisor_recommendation 등)으로 적재된 경우도 조립한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT report_id, incident_id, chamber_id, severity_score, suspected_root_causes, "
            "       recipe_option, limit_option, manual_option, "
            "       supervisor_recommendation, supervisor_reason, supervisor_confidence, "
            "       supervisor_verdict, supervisor_evidence, supervisor_counter "
            "FROM agent_reports WHERE incident_id = %s AND report_id LIKE 'SUP-%%' "
            "ORDER BY id DESC LIMIT 1", (incident_id,))
        row = cur.fetchone()
    if row is None:
        return None
    (rid, inc, chamber, score, causes, rcp, lim, mnt, sel, reason, conf,
     verdict, evid, counter) = row
    brief = {
        "report_id": rid, "incident_id": inc, "chamber_id": chamber, "context_score": score,
        "suspected_root_causes": causes or [],
        "parallel_options": {"recipe_option": rcp or {}, "limit_option": lim or {},
                             "manual_option": mnt or {}},
        "supervisor_recommendation": {
            "decision_frame": "4지선다", "selected": sel, "reason": reason,
            # 정본 = supervisor_verdict 컬럼 (2026-07-31 신설). 구 행(NULL)은 관례 매핑 폴백.
            "verdict": verdict or (lim or {}).get("verdict") or (rcp or {}).get("verdict")
                       or _verdict_from_selected(sel),
            # 정본 = supervisor_evidence 컬럼. `or` 폴백 금지 — evidence=[] 는 "인용 가드 통과
            # 근거 0건"이라는 **유효한 판정**이라, 구 limit_option 근거로 되살리면 신설 컬럼
            # 정본 원칙과 반대로 동작한다 (C 리뷰 #80-⑦). None(신설 이전 구 행)만 폴백.
            "evidence": (evid if evid is not None
                         else ((lim or {}).get("evidence") or [])),
            "counter_evidence": (counter if counter is not None
                                 else (lim or {}).get("counter_evidence")),
            "confidence": conf,
        },
        "approval_status": "PENDING",
    }
    return brief, (lim or None), (rcp or None)


def _verdict_from_selected(sel: Optional[str]) -> Optional[str]:
    """선택 옵션 → verdict 관례 매핑 (C 값이 없을 때의 보수적 기본값)."""
    return {"limit_option": "baseline_aging", "recipe_option": "process_shift",
            "manual_option": "equipment_fault", "escalate": "escalate"}.get(sel or "")
