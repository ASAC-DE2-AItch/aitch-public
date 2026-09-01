"""B9 crazy 마커 배제 가드 — tool 3종이 합성 마커를 '대표 위반 센서'로 쓰지 않는지.

**왜 이 파일이 있나** (B 확인요청 2026-07-28 → ⓐ 소비 측 제외로 합의)
B 의 crazy wafer alert 은 센서 SPC 위반이 아니라 predicted_c65 가 P99 컷을 넘었다는 판정이다.
그런데 계약 §3 의 `violations` 가 `min_length=1` 이라 빈 배열이 불가능해서, B 가 그 한 칸에
**합성 마커 1건**을 끼워 넣는다 — `rule_id="B9"` · `sensor="C65"`(예측 타겟) ·
`control_limit_upper`=P99 컷 · `control_limit_lower`=0.0(센티널).

가드가 없으면 tool 3종이 이걸 진짜 위반으로 믿고:
  · limit  → "C65 센서 관리선을 center=(1572+0)/2=786, σ=1572/6=262 로 재산정"  ← 전부 허구
  · recipe → "C65 를 조정 손잡이로 튜닝"                                      ← 결과값은 손잡이 아님
  · manual → "C65 정비 매뉴얼 검색"                                          ← 그런 센서 없음
그래서 `base.primary_violation`/`real_violations` 가 마커를 걸러내고, 남는 게 없으면 기존
"위반 0건" 경로로 떨어져 recalc/tuning=None → 프롬프트 규칙6(창작 금지)이 발동해야 한다.

**게이트로는 못 막는다** — B 는 crazy 알람에도 Nelson 과 같은 context_score 를 준다(계약 D-9
이관 원칙). 한 wafer 가 Nelson 위반과 crazy 를 동시에 만족하면 crazy alert 도 게이트를 통과해
tool 로 들어온다. 그래서 소비 측 가드가 유일한 방어선이다.

⚠️ 판별식 정본은 `rule_id == "B9"` 이며 orchestrator 의 `incident_grouper.is_crazy_alert()` 와
   같은 규칙이다. 마커 규격이 바뀌면 두 곳이 함께 바뀌어야 한다.

Qdrant·LLM 없이 돈다 — 순수 함수(검색어 빌더·스텁 API·패턴 판정)만 검증한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402

# 스텁 재산정은 관리선 σ 배수를 params A1(control_limit_k_sigma)에서 읽는다 (2026-07-30 이관).
SETTINGS = load_settings()
from agent_service.app.tools import limit as L  # noqa: E402
from agent_service.app.tools import maintenance as M  # noqa: E402
from agent_service.app.tools import recipe as R  # noqa: E402
from agent_service.app.tools.base import (  # noqa: E402
    CRAZY_MARKER_RULE_ID,
    is_crazy_marker,
    primary_violation,
    real_violations,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"

# B 문서(2026-07-28) §1 의 마커 원문 그대로 — 값이 바뀌면 이 상수도 B 에 맞춰 갱신한다.
CRAZY_MARKER = {
    "rule_id": CRAZY_MARKER_RULE_ID,   # Nelson 룰이 아니다 — 종류 마커
    "sensor": "C65",                   # 센서가 아니다 — 예측 타겟
    "window": "settled",               # 계약 필수라 채운 고정값
    "severity": "CRITICAL",
    "current_value": 1650.2,           # predicted_c65
    "control_limit_upper": 1572.0,     # 관리선이 아니다 — y_thresholds P99 컷
    "control_limit_lower": 0.0,        # 비Optional float 이라 넣은 센티널 — 의미 없음
    "limit_version": "v2",
    "description": "B9 crazy: predicted_c65 1650.2 > P99 1572.0",
}


def _raw(name: str = "44_edge_crazy_wafer.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _crazy_only_alert() -> AlertModel:
    """B 가 발행하는 crazy alert — violations 에 합성 마커 1건뿐."""
    raw = _raw()
    raw["violations"] = [dict(CRAZY_MARKER)]
    return AlertModel.model_validate(raw)


def _mixed_alert() -> AlertModel:
    """마커가 진짜 위반보다 **앞에** 온 alert — 순서에 기대지 않는지 확인용.

    B 는 crazy 를 별도 alert 으로 발행하므로(D-13) 실제로는 안 나오는 모양이지만,
    가드가 '첫 원소' 가 아니라 '마커 여부' 로 판별하는지를 잠근다.
    """
    raw = _raw()
    raw["violations"] = [dict(CRAZY_MARKER), *raw["violations"]]
    return AlertModel.model_validate(raw)


# --- 판별 헬퍼 --------------------------------------------------------------------
def test_marker_is_detected_and_excluded():
    """마커는 마커로 인식되고, 진짜 위반 목록에서 빠진다."""
    alert = _mixed_alert()
    assert is_crazy_marker(alert.violations[0])          # 마커 인식
    assert not is_crazy_marker(alert.violations[1])      # 진짜 N1 위반은 그대로
    survivors = real_violations(alert)
    assert [v.rule_id for v in survivors] == ["N1"]
    assert primary_violation(alert).sensor == "C62"      # 마커가 앞에 있어도 진짜 위반이 대표


def test_crazy_only_has_no_primary_violation():
    """crazy-only alert 은 대표 위반이 없다 → 각 tool 의 '위반 0건' 경로로 떨어진다."""
    alert = _crazy_only_alert()
    assert alert.violations                              # 계약상 min_length=1 은 여전히 충족
    assert real_violations(alert) == []
    assert primary_violation(alert) is None


# --- limit: 허구 관리선 재산정을 막는다 ------------------------------------------
def test_limit_recalc_is_none_on_crazy_only():
    """마커의 P99 컷·센티널 0.0 으로 center·σ 를 역산하지 않는다 (786·262 금지)."""
    assert L._stub_recalc_api(_crazy_only_alert(), SETTINGS) is None


def test_limit_recalc_uses_real_violation_when_mixed():
    """마커가 섞여 있어도 재산정 대상은 진짜 위반 센서다."""
    recalc = L._stub_recalc_api(_mixed_alert(), SETTINGS)
    assert recalc is not None
    assert recalc["sensor_id"] == "C62"                  # C65 가 아니다
    # 진짜 위반의 관리선(1965/1635)에서 역산된 값인지 — 마커(1572/0) 밴드가 아님
    assert recalc["center_before"] == (1965.0 + 1635.0) / 2.0


def test_limit_version_ignores_marker():
    """마커의 limit_version('v2')이 재설정안에 실려 나가지 않는다."""
    alert = _mixed_alert()
    assert L.limit_version_of(alert, "C65") is None      # 마커 센서로는 조회 불가
    assert L.limit_version_of(alert, "C62") == "v1"      # 진짜 위반은 정상 조회


def test_limit_case_query_empty_on_crazy_only():
    """crazy-only 면 사례 검색어가 비어야 한다 ('C65 B9' 로 검색하면 노이즈)."""
    assert L.build_case_query(_crazy_only_alert()) == ""


# --- recipe: C65 를 손잡이로 삼지 않는다 -----------------------------------------
def test_recipe_tuning_is_none_on_crazy_only():
    """예측 결과값(C65)은 조정 손잡이가 될 수 없다 → tuning null → 규칙6 발동."""
    assert R._stub_tuning_api(_crazy_only_alert()) is None


def test_recipe_tuning_uses_real_violation_when_mixed():
    """마커가 섞여도 손잡이 후보는 진짜 위반 센서다."""
    tuning = R._stub_tuning_api(_mixed_alert())
    assert tuning is not None
    assert tuning["parameter_id"] == "C62"
    assert tuning["value_current"] == 2185.0            # 마커의 1650.2 이 아니다


def test_recipe_queries_empty_on_crazy_only():
    """KB·사례 검색어 둘 다 비어야 한다."""
    alert = _crazy_only_alert()
    assert R.build_kb_query(alert) == ""
    assert R.build_case_query(alert) == ""


def test_recipe_has_shift_ignores_marker():
    """마커의 severity='CRITICAL' 만으로 '급변 동반'이 성립하면 안 된다.

    성립하면 실제로 튄 센서가 없는데 온도 패턴 가드(enforce_temp_pattern_guard)가 발동해
    정상 튜닝안을 강등한다.
    """
    assert R.has_shift(_crazy_only_alert()) is False
    assert R.has_shift(_mixed_alert()) is True          # 진짜 N1/CRITICAL 은 그대로 잡는다


# --- maintenance: C65 정비 매뉴얼을 뒤지지 않는다 --------------------------------
def test_maintenance_queries_empty_on_crazy_only():
    """매뉴얼·사례 검색어 둘 다 비어야 한다."""
    alert = _crazy_only_alert()
    assert M.build_manual_query(alert) == ""
    assert M.build_case_query(alert) == ""


def test_maintenance_queries_use_real_violation_when_mixed():
    """마커가 섞여도 증상 검색어는 진짜 위반 센서 기준이다."""
    alert = _mixed_alert()
    assert M.build_manual_query(alert).startswith("C62")
    assert "C65" not in M.build_case_query(alert)
