# -*- coding: utf-8 -*-
"""P4-1 승인 API 코어 단위테스트 — validate_decision(순수) + fetch_pending(fake conn).

fastapi/psycopg2/langgraph 불요. 라우트·실 DB·resume 실행은 사용자 env(#23 수동 / #24 Level 2).
실행: python tests/test_approvals_api.py
"""

import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo 루트 — gateway 상대 임포트(..orchestrator) 정합 (21c74fe)
from src.gateway.approvals import validate_decision, fetch_pending  # noqa: E402


def _expect_error(body):
    try:
        validate_decision(body)
    except ValueError:
        return True
    return False


def test_validate_approve_ok():
    p = validate_decision({"action": "approve", "approver": "eng1", "approver_role": "process"})
    assert p["action"] == "approve" and p["approver"] == "eng1" and p["approver_role"] == "process"
    assert "modified_value" not in p


def test_validate_modify_ok():
    p = validate_decision({"action": "modify", "approver": "eng2", "modified_value": -297.3})
    assert p["action"] == "modify" and p["modified_value"] == -297.3


def test_validate_bad_action():
    assert _expect_error({"action": "bogus", "approver": "e"})
    assert _expect_error({"approver": "e"})               # action 누락


def test_validate_missing_approver():
    assert _expect_error({"action": "approve"})           # approver 필수


def test_validate_modify_requires_value():
    assert _expect_error({"action": "modify", "approver": "e"})   # modified_value 필수


def test_validate_escalate_ok():
    assert validate_decision({"action": "escalate", "approver": "e"})["action"] == "escalate"


def test_validate_reject_requires_reason_and_reanalyze():
    assert _expect_error({"action": "reject", "approver": "e"})              # reason 필수
    p = validate_decision({"action": "reject", "approver": "e", "reason": "x"})
    assert p["reason"] == "x" and p["reanalyze"] is True                     # 기본 재분석
    p2 = validate_decision({"action": "reject", "approver": "e", "reason": "x", "reanalyze": False})
    assert p2["reanalyze"] is False                                          # 종료 선택


class _FakeCur:
    def __init__(self, rows, cols):
        self._rows = rows
        self.description = [(c,) for c in cols]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows, cols):
        self._cur = _FakeCur(rows, cols)

    def cursor(self):
        return self._cur


def test_fetch_pending_shapes_rows():
    """P6-1 갱신: SELECT에 incident_type·age_sec 동봉 — 에이징은 표시만 (is_aged)."""
    cols = ["incident_id", "chamber_id", "incident_type", "request_type", "selected_option",
            "requested_at", "priority_score", "severity_max", "age_sec"]
    rows = [("INC-1", "SIM_CH_3", None, "correction", "limit_option",
             "2026-07-13T10:00:00Z", 2072.0, "CRITICAL", 120.0)]
    out = fetch_pending(_FakeConn(rows, cols))                 # timeout 미주입 → is_aged 없음
    assert out[0]["incident_id"] == "INC-1" and out[0]["age_sec"] == 120.0
    assert "is_aged" not in out[0]

    aged = fetch_pending(_FakeConn(rows, cols), pending_timeout_sec=60)
    assert aged[0]["is_aged"] is True                          # 120s ≥ 60s
    fresh = fetch_pending(_FakeConn(rows, cols), pending_timeout_sec=600)
    assert fresh[0]["is_aged"] is False


def test_retry_failed_correction_validates_input():
    """재처리 입력 검증 — DB 접근 전 ValueError (P6-1, B5-1 §5-3)."""
    from src.gateway.approvals import retry_failed_correction
    for bad_mv in (None, {}, {"center": "abc"}, {"foo": 1}, {"center": True}):
        try:
            retry_failed_correction(None, "LIM-X", bad_mv, approver="eng")
            raise AssertionError(f"ValueError 기대: {bad_mv}")
        except ValueError:
            pass
    try:
        retry_failed_correction(None, "LIM-X", {"center": 1.0}, approver="")
        raise AssertionError("approver 누락 ValueError 기대")
    except ValueError:
        pass


# ── A-warn 누적 표류 배지 (2026-08-07) ──────────────────────────────────────
#   B(correction_history.cum_from_initial)가 계산을 제공하고 표시층이 소비하는 계약의
#   **호출자 쪽** 테스트. B 계산 자체는 B 테스트가 담당하므로 여기선 stub 으로 고정하고,
#   ⓐ 최댓값 선택 ⓑ 임계 비교 ⓒ 계약 필드 상시 존재 ⓓ **실패 시 fail-open** 을 본다.
#   ⓓ가 핵심이다 — 이 배선은 실패해도 예외가 안 나고 "배지가 안 뜸"으로만 드러난다.
class _FakeSAConn:
    def __init__(self, proposals, boom=False):
        self._p, self._boom = proposals, boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        if self._boom:
            raise RuntimeError("DB down")
        return _FakeResult(self._p)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self._rows


class _FakeEngine:
    def __init__(self, proposals, boom=False):
        self._p, self._boom = proposals, boom

    def connect(self):
        return _FakeSAConn(self._p, self._boom)


def _prop(inc, sensor, center):
    return {"incident_id": inc, "chamber_id": "SIM_CH_3", "recipe_id": "C6_0", "step": 4,
            "sensor_window": "settled", "sensor_id": sensor, "center": center}


def _with_stub_cum(fn, sigma_map):
    """`cum_from_initial` 을 sensor_id → σ 맵으로 대체하고 fn 실행 (원복 보장)."""
    import src.agent_b_spc.correction_history as chmod
    orig = chmod.cum_from_initial
    chmod.cum_from_initial = lambda conn, gk, center, k: sigma_map.get(gk[4])
    try:
        return fn()
    finally:
        chmod.cum_from_initial = orig


def test_attach_cum_drift_takes_worst_sensor_and_warns():
    """한 Incident 에 제안이 여러 건이면 **최댓값**을 싣고, 임계 이상이면 경고."""
    from src.gateway.approvals import attach_cum_drift
    rows = [{"incident_id": "INC-1"}]
    eng = _FakeEngine([_prop("INC-1", "C11", 10.0), _prop("INC-1", "C17", 20.0)])
    out = _with_stub_cum(
        lambda: attach_cum_drift(rows, warn_sigma=2.0, k_sigma=3.0, engine=eng),
        {"C11": 3.7, "C17": 1.2})
    assert out[0]["cum_from_initial_sigma"] == 3.7      # max(3.7, 1.2)
    assert out[0]["cum_worst_sensor"] == "C11"          # 그 값을 만든 센서
    assert out[0]["is_cum_warned"] is True and out[0]["cum_warn_sigma"] == 2.0


def test_attach_cum_drift_below_threshold_not_warned():
    """임계 미만이면 값은 싣되 경고 배지는 끈다 (비차단·과경보 방지)."""
    from src.gateway.approvals import attach_cum_drift
    rows = [{"incident_id": "INC-2"}]
    out = _with_stub_cum(
        lambda: attach_cum_drift(rows, warn_sigma=2.0, k_sigma=3.0,
                                 engine=_FakeEngine([_prop("INC-2", "C11", 1.0)])),
        {"C11": 0.9})
    assert out[0]["cum_from_initial_sigma"] == 0.9 and out[0]["is_cum_warned"] is False


def test_attach_cum_drift_none_sigma_means_no_badge():
    """기저 부재·σ 퇴화(B 가 None 반환)면 배지 없음 — 근거 없는 경고를 만들지 않는다."""
    from src.gateway.approvals import attach_cum_drift
    rows = [{"incident_id": "INC-3"}]
    out = _with_stub_cum(
        lambda: attach_cum_drift(rows, warn_sigma=2.0, k_sigma=3.0,
                                 engine=_FakeEngine([_prop("INC-3", "C11", 1.0)])),
        {})                                             # stub 이 None 반환
    assert out[0]["cum_from_initial_sigma"] is None and out[0]["is_cum_warned"] is False


def test_attach_cum_drift_is_fail_open_on_db_error():
    """★ DB 실패해도 승인 목록은 살아야 한다 — 예외 전파 금지 + 계약 필드 유지."""
    from src.gateway.approvals import attach_cum_drift
    rows = [{"incident_id": "INC-4"}]
    out = attach_cum_drift(rows, warn_sigma=2.0, k_sigma=3.0,
                           engine=_FakeEngine([], boom=True))
    assert out[0]["incident_id"] == "INC-4"
    for k in ("cum_from_initial_sigma", "cum_worst_sensor", "cum_warn_sigma", "is_cum_warned"):
        assert k in out[0]                              # 프론트 분기 단순화 — 항상 존재


def test_attach_cum_drift_contract_fields_without_engine():
    """엔진 미생성(DATABASE_URL 부재)에서도 계약 필드는 채워진다 — 환경 무관 고정."""
    from src.gateway import approvals as ap
    saved = ap._cum_engine[0]
    ap._cum_engine[0] = False                           # 생성 실패 상태로 고정
    try:
        out = ap.attach_cum_drift([{"incident_id": "INC-5"}], warn_sigma=2.0)
    finally:
        ap._cum_engine[0] = saved
    assert out[0]["is_cum_warned"] is False and out[0]["cum_warn_sigma"] == 2.0


# ── CT²(AE) 배포 브리프 평탄화 (2026-08-09 — 기준 개정 v2 §7) ────────────────

def _ct2_row(report):
    return {"incident_id": "INC-1", "request_type": "ct2_deploy",
            "original_value": {"bundle": "/m/ae_cand_x", "report": report}}


def test_attach_ct2_brief_flattens_report():
    """게이트 리포트의 `approval_brief` 가 평탄한 `ct2_*` 키로 올라온다."""
    from src.gateway.approvals import attach_ct2_brief
    rows = [_ct2_row({"bundle_name": "ae_cand_x", "gate_verdict": "PASS",
                      "gate_schema_version": 3, "failed_checks": [],
                      "approval_brief": {"verdict": "PASS", "weakened_count": 2,
                                         "boundary_count": 2, "weakened": [],
                                         "boundaries": [], "residual_risks": []}})]
    attach_ct2_brief(rows)
    r = rows[0]
    assert r["ct2_verdict"] == "PASS" and r["ct2_bundle"] == "ae_cand_x"
    assert r["ct2_brief"]["weakened_count"] == 2 and r["ct2_brief"]["boundary_count"] == 2
    assert "ct2_brief_missing" not in r


def test_attach_ct2_brief_marks_legacy_report_missing():
    """★구 리포트(브리프 없음)를 조용히 빈 화면으로 넘기지 않는다.

    빈 브리프를 그대로 렌더하면 승인자가 '약화 0건'으로 읽는다 — 검사를 안 한 것과
    통과한 것을 같은 값으로 표현하는 실패 유형(게이트 E1 SKIP 사고와 같은 형태).
    """
    from src.gateway.approvals import attach_ct2_brief
    rows = [_ct2_row({"bundle_name": "ae_old", "gate_verdict": "PASS",
                      "gate_schema_version": 2})]
    attach_ct2_brief(rows)
    assert "ct2_brief" not in rows[0]
    assert "재채점" in rows[0]["ct2_brief_missing"]

    rows2 = [{"incident_id": "INC-2", "request_type": "ct2_deploy", "original_value": None}]
    attach_ct2_brief(rows2)
    assert rows2[0]["ct2_brief_missing"]


def test_attach_ct2_brief_ignores_other_request_types():
    """SPC 승인 행은 건드리지 않는다 (기존 화면 무영향)."""
    from src.gateway.approvals import attach_ct2_brief
    rows = [{"incident_id": "INC-3", "request_type": "limit_change",
             "original_value": {"brief": {}}}]
    attach_ct2_brief(rows)
    assert not any(k.startswith("ct2_") for k in rows[0])


def test_attach_ct2_brief_does_not_translate_keys_itself():
    """★번역은 gateway 가 하지 않는다 — 라벨은 validate_bundle 산출을 그대로 통과시킨다.

    여기서 키 이름을 한국어로 옮기기 시작하면 임계를 바꿀 때 두 곳을 고쳐야 하고,
    안 고쳐도 아무 일도 일어나지 않는다 (헌법 7장 "신설하고 하류를 안 따라감").
    """
    from src.gateway import approvals as ap
    src = pathlib.Path(ap.__file__).read_text(encoding="utf-8")
    body = src.split("def attach_ct2_brief")[1].split("\ndef ")[0]
    assert "WEAKENED_LABELS" not in body and "오경보 예산" not in body


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"ALL {len(tests)} TESTS PASS")
