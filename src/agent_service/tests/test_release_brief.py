"""RTD 해제 근거 Brief — 트리거·재료·가드 3종 (인프라 스텝 3 · #140).

정본 = docs/Agent_프롬프트_라이브러리_v1.md §7 · 계약 §8-D-1.

이 스위트는 **DB·Qdrant·게이트웨이를 타지 않는다** — 재료를 주입해 순수 경로만 고정한다.
라이브 왕복(게이트웨이 → 정지 → Brief)은 인프라 스텝 4 「파일 채널 왕복 3종」의 inhibit 채널
확인과 같은 자리에서 본다.

여기서 지키려는 것은 셋이다:
  ⓐ 정지 1건이 Brief 1건이 되는가 (장비 스코프에서 4건으로 불어나지 않는가)
  ⓑ 근거 없는 재가동 권고가 화면까지 가지 않는가 (가드 ②)
  ⓒ LLM 이 신원·회고 수치를 덮어쓰지 못하는가 (코드가 정본)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app import release_brief as rb  # noqa: E402
from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.llm.client import MockBackend  # noqa: E402
from agent_service.app.report_writer import release_brief_to_row  # noqa: E402
from agent_service.app.schemas.release import (  # noqa: E402
    ReleaseBrief,
    make_fallback_release_brief,
    make_release_report_id,
    retrospect_from_materials,
)

SETTINGS = load_settings()
INHIBITED_AT = datetime(2026, 8, 8, 4, 12, 7, tzinfo=timezone.utc)
INCIDENT = "INC-20260808-SIMCH1-003"
ALERT = "ALERT-20260808-SIMCH1-0042"
QUAL = "QUAL-20260801-SIMCH1-0007"


# --- 재료 픽스처 --------------------------------------------------------------
def _materials(*, past: bool = True, violations: bool = True) -> dict:
    return {
        "incident_id": INCIDENT,
        "chamber_id": "SIM_CH_1",
        "chambers": ["SIM_CH_1"],
        "scope": "chamber",
        "equipment_id": None,
        "trigger_alert_id": ALERT,
        "inhibited_at": INHIBITED_AT,
        "trigger_violations": (
            [{"sensor_id": "C17", "rule_id": "N1", "severity": "CRITICAL",
              "current_value": 41.2, "control_limit_upper": 39.8,
              "description": "N1: 관리선 이탈"}] if violations else []
        ),
        "persistent_sensors": [{"sensor_id": "C17", "n1_alerts": 41, "breach_ratio": 0.441}],
        "window_minutes": 60,
        "window_total_alerts": 93,
        "past_releases": (
            [{"chamber_id": "SIM_CH_1", "incident_id": "INC-20260801-SIMCH1-001",
              "inhibited_at": INHIBITED_AT - timedelta(days=7),
              "released_at": INHIBITED_AT - timedelta(days=7) + timedelta(hours=6),
              "released_by": "eng.kim", "release_qual_id": QUAL,
              "scope": "chamber", "downtime_h": 6.0}] if past else []
        ),
    }


def _kb(*, hits: bool = True) -> dict:
    if not hits:
        return {"manual_hits": [], "cases": []}
    return {
        "manual_hits": [{"manual_id": "MAN-OXF-0031", "doc_id": "oxford_100_manual",
                         "text": "히터 서모커플 점검"}],
        "cases": [{"case_id": "CASE-0031", "incident_id": "INC-20260801-SIMCH1-001"}],
    }


def _llm_json(**over) -> str:
    body = {
        "readiness": "ready",
        "summary": "재인증 진행 가능 — 같은 챔버가 지난주 같은 경로로 해제됐다.",
        "evidence": [
            f"발동 알람 {ALERT} 의 C17 N1 CRITICAL [SPC]",
            f"과거 정지가 {QUAL} 로 해제됨 [CASE]",
            "매뉴얼 MAN-OXF-0031 히터 서모커플 점검 [KB]",
        ],
        "counter_evidence": "C17 이탈률 44.1% 는 단일 센서라 원인 미특정 가능성이 남는다.",
        "confidence": 0.82,
        "precheck_items": ["히터 서모커플 점검"],
    }
    body.update(over)
    return json.dumps(body, ensure_ascii=False)


def _generate(llm_response: str, *, materials=None, kb=None) -> ReleaseBrief:
    group = {"incident_id": INCIDENT, "chamber_id": "SIM_CH_1",
             "scope": "chamber", "inhibited_at": INHIBITED_AT, "chambers": ["SIM_CH_1"]}
    return asyncio.run(
        rb.generate(group, SETTINGS, MockBackend([llm_response]),
                    materials=materials or _materials(), kb=kb if kb is not None else _kb())
    )


# =============================================================================
# 1. 트리거 — /chambers/status → Incident 단위 접기
# =============================================================================
def test_equipment_scope_folds_to_one_brief_per_incident(monkeypatch):
    """장비 스코프 정지는 형제 챔버 4행이지만 **Brief 는 1건**이다 (헌법 1-4).

    해제가 같은 incident 단위 일괄이므로(`chamber_inhibit.release`), 챔버마다 만들면 같은
    판단을 4번 묻는 화면이 된다. 대표 챔버는 `incidents.chamber_id`(실제 이상 관측지).
    """
    monkeypatch.setattr(rb, "fetch_inhibited", lambda s: [])   # 미사용 (직접 group 호출)
    monkeypatch.setattr("agent_service.app.db.fetch_incident_chamber", lambda i: "SIM_CH_3")
    items = [
        {"chamber_id": f"SIM_CH_{i}", "inhibited": True, "incident_id": INCIDENT,
         "inhibited_at": (INHIBITED_AT + timedelta(microseconds=i)).isoformat(),
         "scope": "equipment", "equipment_id": "ETCH-01"}
        for i in (1, 2, 3, 4)
    ]
    groups = rb.group_by_incident(items)
    assert len(groups) == 1
    g = groups[0]
    assert g["scope"] == "equipment"
    assert g["chambers"] == ["SIM_CH_1", "SIM_CH_2", "SIM_CH_3", "SIM_CH_4"]
    assert g["chamber_id"] == "SIM_CH_3", "대표 챔버는 Incident 의 챔버여야 한다"
    # 형제 행은 DEFAULT NOW() 라 μs 가 갈린다 — **가장 이른 것**이 정지 시각이다
    # (늦은 쪽을 쓰면 회고 ⓑ 창에 정지 이후의 빈 구간이 섞인다).
    assert g["inhibited_at"] == INHIBITED_AT + timedelta(microseconds=1)


def test_incident_id_missing_is_skipped_not_crashed(monkeypatch):
    """incident_id 없는 행은 건너뛴다 — 응답 형상이 이상해도 폴러는 살아야 한다(6-2)."""
    monkeypatch.setattr("agent_service.app.db.fetch_incident_chamber", lambda i: None)
    groups = rb.group_by_incident([
        {"chamber_id": "SIM_CH_1", "inhibited": True},                      # incident 없음
        {"chamber_id": "SIM_CH_2", "inhibited": True, "incident_id": INCIDENT,
         "inhibited_at": INHIBITED_AT.isoformat()},
    ])
    assert [g["incident_id"] for g in groups] == [INCIDENT]
    assert groups[0]["chamber_id"] == "SIM_CH_2", "Incident 조회 실패 시 정렬 첫 챔버로 폴백"


def test_fetch_inhibited_survives_bad_payload(monkeypatch):
    """응답이 200 이어도 형이 아니면 빈 목록 — "떴으니 붙었다"로 보지 않는다 (헌법 7장)."""
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    import httpx

    for payload in ("123", [1, 2], None, {"items": "nope"}):
        monkeypatch.setattr(httpx, "get", lambda *a, _p=payload, **k: _Resp(_p))
        assert rb.fetch_inhibited(SETTINGS) == []

    # 정상 응답에서는 inhibited=true 만 걸러 낸다
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(
        {"items": [{"chamber_id": "SIM_CH_1", "inhibited": True},
                   {"chamber_id": "SIM_CH_2", "inhibited": False},
                   "형이 아닌 원소"]}))
    got = rb.fetch_inhibited(SETTINGS)
    assert [i["chamber_id"] for i in got] == ["SIM_CH_1"]


def test_gateway_unreachable_skips_tick(monkeypatch):
    """게이트웨이가 죽어 있으면 그 주기만 건너뛴다 — 폴러는 죽지 않는다(6-2)."""
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _boom)
    assert rb.fetch_inhibited(SETTINGS) == []


# =============================================================================
# 2. 신원·회고 — 코드가 정본이다
# =============================================================================
def test_report_id_is_deterministic_and_rls_prefixed():
    """같은 정지 → 같은 report_id. 그래야 UNIQUE + ON CONFLICT 가 중복의 최종 방어선이 된다.

    접두는 **RLS** 다 (헌법 6-4 신설 2026-08-09). 초안의 `QUAL` 재사용은 폐기했다 —
    Brief 와 `quals.qual_id` 는 **다른 대상**이라 6-4 의 접두 공유 선례(LIM)가 성립하지
    않고, S7 승인 화면 한 곳에 `report_id`·`qual_id`·`release_qual_id` 가 셋 다 `QUAL-`
    로 떠 로그 grep 으로도 구분이 안 됐다. **되돌아가지 않게 여기서 잠근다.**
    """
    a = make_release_report_id("SIM_CH_1", INHIBITED_AT, ALERT, INCIDENT)
    b = make_release_report_id("SIM_CH_1", INHIBITED_AT, ALERT, INCIDENT)
    assert a == b == "RLS-20260808-SIMCH1-0042"
    assert not a.startswith("QUAL-"), "QUAL 재사용 회귀 — 6-4 접두 신설분(RLS)을 쓸 것"
    # 재정지(다른 시각)는 다른 ID — "다시 섰다"는 새 판단이다
    assert make_release_report_id(
        "SIM_CH_1", INHIBITED_AT + timedelta(days=1), ALERT, INCIDENT
    ) != a


def test_llm_cannot_overwrite_identity_or_retrospect():
    """LLM 이 신원·회고를 적어 보내도 **코드 값이 이긴다**.

    신원은 예시를 그대로 베끼는 것이 실측됐고(supervisor 와 동일), retrospect 는 DB 조회
    결과다 — 근거가 창작 가능하면 근거가 아니다.
    """
    hostile = _llm_json(
        report_id="RLS-19700101-HACK-0000", incident_id="INC-거짓", chamber_id="SIM_CH_9",
        scope="equipment", trigger_alert_id="ALERT-거짓",
        retrospect={"trigger_violations": [], "persistent_sensors": [],
                    "window_minutes": 9999, "window_total_alerts": 0, "past_releases": []},
        release_authority="anything_goes",
    )
    brief = _generate(hostile)
    assert brief.report_id == "RLS-20260808-SIMCH1-0042"
    assert brief.incident_id == INCIDENT
    assert brief.chamber_id == "SIM_CH_1"
    assert brief.scope == "chamber"
    assert brief.trigger_alert_id == ALERT
    assert brief.retrospect.window_minutes == 60
    assert brief.retrospect.persistent_sensors[0].sensor_id == "C17"
    assert brief.release_authority == "requal_approval_only"


def test_release_authority_is_locked_by_type():
    """`release_authority` 는 타입 상수 — 해제 권한을 C 가 표현할 수 없어야 한다(1-1 예외 4 ⓒ)."""
    import pytest

    with pytest.raises(Exception):
        ReleaseBrief(
            report_id="RLS-20260808-SIMCH1-0042", incident_id=INCIDENT, chamber_id="SIM_CH_1",
            readiness="ready", summary="s", evidence=["a", "b", "c"], counter_evidence="c",
            confidence=0.5, release_authority="auto_release",   # 허용되면 안 된다
        )


# =============================================================================
# 3. 가드 3종
# =============================================================================
def test_guard_evidence_floor_downgrades_ready_without_any_basis():
    """가드 ②: 선례 0건 + KB 0장인데 `ready` → `conditional` 강등.

    근거 0 은 "괜찮다"가 아니라 **모른다**이다. 화면의 "준비됨"은 그 자체로 판단을 민다.
    """
    brief = _generate(_llm_json(), materials=_materials(past=False), kb=_kb(hits=False))
    assert brief.readiness == "conditional"
    assert "근거 없음" in brief.summary
    assert brief.precheck_items, "강등 사유가 확인 항목으로 남아야 화면에서 이유가 보인다"


def test_guard_evidence_floor_keeps_ready_when_precedent_exists():
    """선례가 있으면 `ready` 를 유지한다 — 가드는 근거 없는 건만 내린다."""
    brief = _generate(_llm_json(), materials=_materials(past=True), kb=_kb(hits=False))
    assert brief.readiness == "ready"


def test_guard_precheck_fills_marker_for_conditional():
    """가드 ③: `conditional` 인데 확인 항목이 비면 **미기재 표지**를 채운다(창작 아님)."""
    brief = _generate(_llm_json(readiness="conditional", precheck_items=[]))
    assert brief.readiness == "conditional"
    assert brief.precheck_items == [rb._NO_PRECHECK]


def test_guard_citation_masks_ghost_ids_but_keeps_real_ones():
    """가드 ①: 재료에 없는 ID 만 `[미확인 인용]` 으로. 실존 ID 는 건드리지 않는다."""
    brief = _generate(_llm_json(evidence=[
        f"실존 알람 {ALERT} [SPC]",
        "없는 사례 CASE-9999 를 인용 [CASE]",
        f"실존 qual {QUAL} [CASE]",
    ]))
    assert ALERT in brief.evidence[0] and "미확인" not in brief.evidence[0]
    assert "[미확인 인용: CASE-9999]" in brief.evidence[1]
    assert QUAL in brief.evidence[2] and "미확인" not in brief.evidence[2]
    assert "CASE-9999" in brief.counter_evidence, "반증에 유령 ID 사유가 병기돼야 한다"


def test_guard_order_evidence_floor_before_precheck():
    """②가 readiness 를 바꾸므로 ③(conditional 검사)은 그 **뒤**여야 한다.

    순서가 뒤집히면 강등된 건의 확인 항목이 영영 안 채워진다 — 여기서는 ②가 넣은 사유가
    남아 있으므로 미기재 표지가 아니라 그 문장이 보인다.
    """
    brief = _generate(_llm_json(precheck_items=[]),
                      materials=_materials(past=False), kb=_kb(hits=False))
    assert brief.readiness == "conditional"
    assert brief.precheck_items and brief.precheck_items[0] != rb._NO_PRECHECK


# =============================================================================
# 4. 실패 경로
# =============================================================================
def test_fallback_is_not_ready_and_keeps_retrospect():
    """LLM 파싱 2회 실패 → `not_ready` + **회고 재료는 그대로**.

    실패 산출물이 "재가동해도 됨"으로 보이면 안 되고(안전측), 서술이 없어도 사람은 표를 보고
    판단할 수 있다 — `make_fallback_brief`(escalate)·처분 폴백(HOLD)과 같은 방향이다.
    """
    brief = _generate("깨진 JSON")
    assert brief.readiness == "not_ready"
    assert brief.confidence == 0.0
    assert brief.retrospect.persistent_sensors[0].sensor_id == "C17"
    assert brief.retrospect.past_releases[0].release_qual_id == QUAL
    assert brief.report_id == "RLS-20260808-SIMCH1-0042"


def test_fallback_helper_shape():
    """fallback 헬퍼 단독 — evidence 3개·counter 1개 계약을 스스로 지킨다."""
    retro = retrospect_from_materials(_materials())
    b = make_fallback_release_brief(
        report_id="RLS-20260808-SIMCH1-0042", incident_id=INCIDENT,
        chamber_id="SIM_CH_1", trigger_alert_id=ALERT, inhibited_at=INHIBITED_AT,
        retrospect=retro,
    )
    assert len(b.evidence) == 3 and b.counter_evidence
    assert b.readiness == "not_ready"


def test_kb_query_is_empty_when_no_trigger_violation():
    """ⓐ 가 비면 검색어가 없다 — 빈 질의로 KB 를 훑지 않는다(엉뚱한 문서가 걸린다)."""
    assert rb.build_kb_query(_materials(violations=False)) != ""   # ⓑ 센서로는 만들어진다
    assert rb.build_kb_query({"trigger_violations": [], "persistent_sensors": []}) == ""


# =============================================================================
# 5. 적재 — 왕복 형태
# =============================================================================
def test_row_mapping_roundtrip():
    """Brief → release_briefs 행. jsonb 3종이 문자열로, 나머지는 그대로 간다."""
    row = release_brief_to_row(_generate(_llm_json()))
    assert row["report_id"] == "RLS-20260808-SIMCH1-0042"
    assert row["incident_id"] == INCIDENT
    assert row["chamber_id"] == "SIM_CH_1"
    assert row["readiness"] == "ready"
    assert row["inhibited_at"] == INHIBITED_AT
    evidence = json.loads(row["evidence"])
    assert isinstance(evidence, list) and len(evidence) == 3
    retro = json.loads(row["retrospect"])
    assert retro["window_total_alerts"] == 93
    assert retro["past_releases"][0]["release_qual_id"] == QUAL
    # 컬럼에 없는 값을 흘리지 않는다 — release_authority 는 타입 상수라 적재 대상이 아니다
    assert "release_authority" not in row


# =============================================================================
# 6. ⓐ 상한 — 프롬프트 예산 보호 (2026-08-10 실측 사고)
# =============================================================================
#
# 스톰 알람 1건이 위반 56행이라 ReleaseJudgement 프롬프트가 ~11,417 토큰 → 예산 10,288
# 초과 → vLLM 400 → 폴백 Brief(자동 판단 없음). ⓐ 만 무제한이었던 것이 원인이다.
# `max_model_len` 상향으로는 못 막는다 — 무제한 배열은 어떤 천장이든 결국 넘는다.
def _viol(sensor: str, rule: str, severity: str) -> dict:
    """`spc_violations` 행 모양의 위반 1건."""
    return {"sensor_id": sensor, "rule_id": rule, "severity": severity,
            "current_value": 1.0, "description": f"{rule}: 6점 창"}


def test_cap_keeps_all_when_under_limit():
    """상한 이하면 손대지 않는다 — 정렬도 바꾸지 않는다(DB 순서 보존)."""
    vs = [_viol("C17", "N1", "CRITICAL"), _viol("C11", "N2", "WARNING")]
    assert rb._cap_trigger_violations(vs, 20, INCIDENT) == vs


def test_cap_drops_least_severe_first():
    """초과 시 CRITICAL 이 먼저 남는다 — 잘리는 쪽이 항상 덜 심각해야 한다."""
    vs = [_viol(f"C{i}", "N2", "WARNING") for i in range(10)]
    vs += [_viol("C17", "N1", "CRITICAL"), _viol("C62", "N1", "CRITICAL")]
    kept = rb._cap_trigger_violations(vs, 2, INCIDENT)
    assert len(kept) == 2
    assert {v["sensor_id"] for v in kept} == {"C17", "C62"}
    assert all(v["severity"] == "CRITICAL" for v in kept)


def test_cap_is_not_silent_total_survives_into_retrospect():
    """자른 사실이 payload 에 남는다 (7장 no silent caps).

    `trigger_violations_total` 이 배열 길이보다 크면 "잘렸다"를 사람이 읽을 수 있다.
    이 필드가 없으면 근거가 얕아진 Brief 와 원래 얕은 Brief 가 **구별되지 않는다**.
    """
    materials = {
        "incident_id": INCIDENT,
        "trigger_violations": [_viol("C17", "N1", "CRITICAL")],
        "trigger_violations_total": 56,
    }
    retro = retrospect_from_materials(materials)
    assert retro.trigger_violations_total == 56
    assert len(retro.trigger_violations) == 1
    assert retro.trigger_violations_total > len(retro.trigger_violations)


def test_retrospect_total_defaults_to_len_when_absent():
    """총량 미주입(구 호출부)이면 배열 길이 = 안 잘림. 하위 호환."""
    retro = retrospect_from_materials(
        {"incident_id": INCIDENT, "trigger_violations": [_viol("C17", "N1", "CRITICAL")]}
    )
    assert retro.trigger_violations_total == 1
