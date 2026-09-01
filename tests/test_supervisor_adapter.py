# -*- coding: utf-8 -*-
"""P5-1 어댑터 시임 단위 테스트 — DB·langgraph 무의존 (헌법 검증: 1-4 병렬 3종·1-1 fail-closed 위임).

실행: python -m pytest tests/test_supervisor_adapter.py -q  (또는 python tests/test_supervisor_adapter.py)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.supervisor_adapter import (  # noqa: E402
    _verdict_from_selected, to_gate_report, validate_brief)
from orchestrator.approval_graph import (  # noqa: E402
    apply_target, build_correction_payload, merge_reports)


def _brief(**over):
    """라이브러리 §4 예시 축약형 — 정상 Brief."""
    b = {
        "report_id": "SUP-20260722-001",
        "incident_id": "INC-20260722-SIMCH3-001",
        "chamber_id": "SIM_CH_3",                     # C-② (2026-07-21) — agent_reports NOT NULL
        "context_score": 72,
        "suspected_root_causes": ["PM 후 정상 상태 이동"],
        "parallel_options": {
            "recipe_option": {"report_id": "RCP-20260722-SIMCH3-001", "action": "-",
                              "recipe_correction_id": "RCP-20260722-SIMCH3-002",
                              "confidence": 0.55, "rejected_because": "레짐 신호 우세"},
            "limit_option": {"report_id": "LIM-20260722-SIMCH3-001",
                             "correction_id": "LIM-20260722-SIMCH3-002",   # B 채번 승계 (B-② 2026-07-21)
                             "action": "C11 실력치 +2.5% 재설정", "confidence": 0.88,
                             "sensor": "C11", "limit_version_current": "v3",
                             "center_proposed": -64.2,
                             "evidence": ["N3 추세·급변 없음 [SPC]", "TTTM 0.8% [SPC]"],
                             "counter_evidence": "이동 속도 빠름 — 적용 후 30 wafer reopen 감시"},
            "manual_option": {"report_id": "MNT-20260722-SIMCH3-001", "action": "-",
                              "confidence": 0.42, "rejected_because": "급변·anomaly 부재"},
        },
        "supervisor_recommendation": {
            "decision_frame": "4지선다", "selected": "limit_option",
            "verdict": "baseline_aging", "reason": "기준선 노후",
            "evidence": ["e1", "e2", "e3"], "counter_evidence": "반증 1", "confidence": 0.88,
        },
        "approval_status": "PENDING",
    }
    b.update(over)
    return b


class FakeStore:
    """set_lifecycle만 기록 — conn 없음 (경로 ③ 비활성 확인용)."""
    def __init__(self):
        self.calls = []

    def set_lifecycle(self, inc, lc):
        self.calls.append((inc, lc))
        return True


def test_validate_pass():
    assert validate_brief(_brief()) == []


def test_validate_required_violations():
    assert any("report_id" in p for p in validate_brief(_brief(report_id="BAD-1")))
    assert any("incident_id" in p for p in validate_brief(_brief(incident_id="")))
    b = _brief(); del b["parallel_options"]["manual_option"]
    assert any("manual_option" in p for p in validate_brief(b))  # 병렬 3종 — 헌법 1-4
    b = _brief(); del b["chamber_id"]
    assert any("chamber_id" in p for p in validate_brief(b))     # C-② — agent_reports NOT NULL
    b = _brief(); b["supervisor_recommendation"]["selected"] = "yolo_option"
    assert any("selected" in p for p in validate_brief(b))
    b = _brief(); b["supervisor_recommendation"]["verdict"] = "aliens"
    assert any("verdict" in p for p in validate_brief(b))
    b = _brief(); b["supervisor_recommendation"]["confidence"] = 1.7
    assert any("confidence" in p for p in validate_brief(b))
    assert validate_brief("문자열") == ["brief가 dict가 아님"]


def test_to_gate_report_mapping():
    b = _brief()
    g = to_gate_report(b, b["parallel_options"]["limit_option"],
                       b["parallel_options"]["recipe_option"])
    assert g["selected_option"] == "limit_option"
    assert g["verdict"] == "baseline_aging"
    assert g["chamber_id"] == "SIM_CH_3"                   # C-② 승계
    assert g["limit_analysis"] == {"sensor": "C11", "limit_version_current": "v3",
                                   "center_proposed": -64.2,
                                   "correction_id": "LIM-20260722-SIMCH3-002"}
    assert g["brief"]["report_id"] == "SUP-20260722-001"   # 원문 보존 (감사·S4 렌더)


def test_gate_to_correction_payload():
    """어댑터 출력 → 기존 페이로드 빌더 접합 — limit형 수치가 그대로 흐른다 ('정량은 B' 보존)."""
    b = _brief()
    g = to_gate_report(b, b["parallel_options"]["limit_option"], None)
    p = build_correction_payload(b["incident_id"], g["selected_option"], g)
    assert p["correction_type"] == "limit" and p["sensor"] == "C11"
    assert p["value_proposed"] == -64.2 and p["limit_version"] == "v3"
    assert p["correction_id"] == "LIM-20260722-SIMCH3-002"  # B apply_approved 참조 키 (B-②)


def test_merge_reports_path2_brief():
    st = FakeStore()
    out = merge_reports({"incident_id": "INC-X", "report": _brief()}, st)
    r = out["report"]
    assert r["selected_option"] == "limit_option" and "validation_problems" not in r
    assert st.calls == [("INC-X", "analyzing")]


def test_merge_reports_path2_flags_problems():
    st = FakeStore()
    bad = _brief(); bad["supervisor_recommendation"]["verdict"] = "aliens"
    out = merge_reports({"incident_id": "INC-X", "report": bad}, st)
    assert any("verdict" in p for p in out["report"]["validation_problems"])  # 드랍 아님 — 표기 (ADR-31)


def test_merge_reports_path1_stub_preserved():
    st = FakeStore()
    legacy = {"selected_option": "limit_option", "limit_analysis": {"sensor": "C11"}}
    out = merge_reports({"incident_id": "INC-X", "report": legacy}, st)
    assert out["report"] is legacy or out["report"] == legacy   # 기존 동작 그대로


def test_verdict_fallback():
    assert _verdict_from_selected("manual_option") == "equipment_fault"
    assert _verdict_from_selected(None) is None or _verdict_from_selected(None) == ""


# --- C 정합 (2026-07-22 C 확인분) ------------------------------------------------
def test_none_executable_is_valid_and_applies_nothing():
    """'고를 옵션 없음'은 sentinel 로 온다 — 정상값이며 적용 대상이 없다.

    C 라운드7c 실측 Brief 41건 중 15건(37%)이 이 상태다(escalate 8 · 가드② 6 · fallback 1).
    escalate 는 verdict 이지 option 이 아니라서 selected 자리에 들어가지 않는다.
    """
    for verdict in ("escalate", "baseline_aging"):        # ①원인미상 / ②원인확정·실행불가
        b = _brief()
        b["supervisor_recommendation"]["selected"] = "none_executable"
        b["supervisor_recommendation"]["verdict"] = verdict
        assert validate_brief(b) == [], f"{verdict} + none_executable 이 위반 처리됨"
    # 게이트 하류 — 적용 대상이 없어야 한다 (None 과 동일 취급)
    assert apply_target("none_executable") == apply_target(None)
    p2 = build_correction_payload("INC-1", "none_executable", {})
    assert p2.get("correction_type") is None       # fdc.correction 로 안 나간다
    # 쓰레기값 차단은 그대로 유지된다
    b = _brief(); b["supervisor_recommendation"]["selected"] = "yolo_option"
    assert any("selected" in p for p in validate_brief(b))


def test_legacy_null_selected_still_passes():
    """구 계약(null) Brief 도 드랍하지 않는다 — 하위호환. 다만 경고 로그를 남긴다.

    정상 Brief 는 이제 null 이 아니므로(C 가 sentinel 로 채움), null 은 구 계약이거나
    LLM 이 칸을 건너뛴 신호다. 드랍은 엔지니어 몫이라 통과시키되 흔적을 남긴다.
    """
    b = _brief(); b["supervisor_recommendation"]["selected"] = None
    assert validate_brief(b) == []


def test_limit_analysis_accepts_c_field_names():
    """C 정본(라이브러리 §2) 이름으로도 수치가 채워진다 — sensor_id·center_after·limit_version.

    이 이름들이 안 맞아 limit_analysis 가 전부 null 이었고, 승인해도 fdc.correction 의
    변경 금지 필드(sensor·limit_version, 계약 §2-1)가 빈 채로 B 에게 갔다.
    """
    b = _brief()
    b["parallel_options"]["limit_option"] = {
        "report_id": "LIM-20260722-SIMCH3-001",
        "correction_id": "LIM-20260722-SIMCH3-002",
        "sensor_id": "C11", "center_after": -64.2, "limit_version": "v3",   # C 정본 이름
        "confidence": 0.88,
    }
    g = to_gate_report(b, b["parallel_options"]["limit_option"], None)
    assert g["limit_analysis"] == {"sensor": "C11", "limit_version_current": "v3",
                                   "center_proposed": -64.2,
                                   "correction_id": "LIM-20260722-SIMCH3-002"}
    p = build_correction_payload(b["incident_id"], g["selected_option"], g)
    assert p["sensor"] == "C11" and p["limit_version"] == "v3" and p["value_proposed"] == -64.2


def test_falsy_but_valid_values_survive():
    """falsy-but-valid 값이 폴백에 먹히지 않는다 — 0.0 과 "" 둘 다.

    `or` 로 병합하면 0.0·"" 이 falsy 라 뒤 필드(구 이름)로 넘어가 버린다.
    "값이 있으나 비었다"와 "키가 아예 없다"는 다르므로 None 여부로만 판단한다.
    """
    b = _brief()
    b["parallel_options"]["limit_option"] = {
        "sensor_id": "", "center_after": 0.0, "limit_version": "",   # C 정본 이름, 전부 falsy
        "sensor": "C99", "center_proposed": -1.0, "limit_version_current": "v9",  # 구 이름
    }
    g = to_gate_report(b, b["parallel_options"]["limit_option"], None)
    la = g["limit_analysis"]
    assert la["center_proposed"] == 0.0        # 구 이름(-1.0)으로 안 넘어감
    assert la["sensor"] == ""                  # 구 이름("C99")으로 안 넘어감
    assert la["limit_version_current"] == ""   # 구 이름("v9")으로 안 넘어감


def test_legacy_names_still_used_when_c_names_absent():
    """C 정본 이름이 **없을 때만** 구 이름으로 폴백한다 (경로 ① stub 보존)."""
    b = _brief()
    b["parallel_options"]["limit_option"] = {
        "sensor": "C11", "center_proposed": -64.2, "limit_version_current": "v3",
    }
    la = to_gate_report(b, b["parallel_options"]["limit_option"], None)["limit_analysis"]
    assert la == {"sensor": "C11", "limit_version_current": "v3",
                  "center_proposed": -64.2, "correction_id": None}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f(); print(f"  ✓ {f.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
