# -*- coding: utf-8 -*-
"""P6-1 선행 — 레짐 이벤트 빌더 단위 테스트 (kafka·fastapi 무의존).

실행: python -m pytest tests/test_regime_events.py -q  (또는 python tests/test_regime_events.py)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.events import route_topic  # noqa: E402
from orchestrator.regime_events import (RequalOnceGuard,  # noqa: E402
                                        build_chamber_requalified,
                                        build_qual_verdict)


def test_qual_verdict_loud_ok():
    p = build_qual_verdict("QUAL-20260722-SIMCH4-001", "SIM_CH_4", "loud",
                           26, "이수민", incident_id="INC-20260722-SIMCH4-001")
    assert p["verdict"] == "loud" and p["incident_id"] == "INC-20260722-SIMCH4-001"
    assert p["confirmed_at"].endswith("Z") or "+" in p["confirmed_at"]


def test_qual_verdict_quiet_incident_null():
    """조용 확정은 incident_id null (계약 §8-B) — 넘겨도 null로 강제."""
    p = build_qual_verdict("QUAL-20260722-SIMCH1-001", "SIM_CH_1", "quiet",
                           12, "이수민", incident_id="INC-무시될값")
    assert p["incident_id"] is None


def test_qual_verdict_violations():
    for kwargs, frag in [
        (dict(qual_id="QUAL-1", chamber_id="CH", verdict="noisy", pm_count=1, approved_by="a"), "loud/quiet"),
        (dict(qual_id="QUAL-1", chamber_id="CH", verdict="loud", pm_count=1, approved_by="a"), "incident_id"),
        (dict(qual_id="BAD-1", chamber_id="CH", verdict="quiet", pm_count=1, approved_by="a"), "QUAL-"),
        (dict(qual_id="QUAL-1", chamber_id="CH", verdict="quiet", pm_count=1, approved_by=""), "approved_by"),
    ]:
        try:
            build_qual_verdict(**kwargs)
            raise AssertionError(f"ValueError 미발생: {kwargs}")
        except ValueError as e:
            assert frag in str(e)


def test_requalified_ok_and_idempotent_guard():
    p = build_chamber_requalified(
        "INC-20260722-SIMCH4-001", "SIM_CH_4", "QUAL-20260722-SIMCH4-001",
        26, ["LIM-20260722-SIMCH4-002", "LIM-20260722-SIMCH4-003"], "v4", "이수민")
    assert p["limit_version"] == "v4" and len(p["corrections_applied"]) == 2

    g = RequalOnceGuard()
    assert g.check_and_mark("INC-X") is True
    assert g.check_and_mark("INC-X") is False    # 재시도 = 스킵 (409 몫)
    assert g.check_and_mark("INC-Y") is True


def test_requalified_violations():
    for kwargs, frag in [
        (dict(incident_id="", chamber_id="CH", qual_id="QUAL-1", pm_count=1,
              corrections_applied=[], limit_version="v4", approved_by="a"), "incident_id"),
        (dict(incident_id="INC-1", chamber_id="CH", qual_id="QUAL-1", pm_count=1,
              corrections_applied=["CORR-001"], limit_version="v4", approved_by="a"), "LIM-/RCP-"),
        (dict(incident_id="INC-1", chamber_id="CH", qual_id="QUAL-1", pm_count=1,
              corrections_applied="LIM-1", limit_version="v4", approved_by="a"), "리스트"),
    ]:
        try:
            build_chamber_requalified(**kwargs)
            raise AssertionError(f"ValueError 미발생: {kwargs}")
        except ValueError as e:
            assert frag in str(e)


def test_topic_routing_both_agent():
    """두 이벤트 모두 승인 워크플로 채널 fdc.agent (계약 §8-B·§8-C)."""
    assert route_topic("QualVerdictConfirmed") == "fdc.agent"
    assert route_topic("ChamberRequalified") == "fdc.agent"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
