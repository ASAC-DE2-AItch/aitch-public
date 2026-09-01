# -*- coding: utf-8 -*-
"""fixture ↔ spc_violations 시딩 (W9-0) — DB 없이 순수 부분만 검증.

이 시딩이 없으면 라운드에서 `resolve_alert_context` 가 0건을 돌려주고, 엔진이
"recipe_id=None · knob=C4 명목값 조회 불가" 로 에스컬해 **recipe 옵션이 전부 수치 없이**
나간다 (round11~15 실측 42/42). 에러도 `_provisional` 도장도 없어 조용히 무효가 된다.
"""

import json

from src.agent_service.fixtures import seed_violations as S


def test_seed_constants_match_knob_map_and_db_majority() -> None:
    """시딩 recipe 는 knob_map `nominal_setpoints` 에 실재해야 한다.

    없는 recipe 를 심으면 엔진이 명목값을 못 찾아 **시딩을 하고도 에스컬**한다 —
    고쳤다고 믿는 채로 같은 증상이 남는 게 제일 나쁘다.
    """
    import yaml

    km = yaml.safe_load(open("config/recipe_knob_map.yaml", encoding="utf-8"))
    assert S.SEED_RECIPE_ID in (km.get("nominal_setpoints") or {})
    assert isinstance(S.SEED_STEP, int) and S.SEED_STEP > 0


def test_rows_carry_every_not_null_column(tmp_path) -> None:
    """`spc_violations` NOT NULL 컬럼(chamber·rule·sensor·severity)이 fixture 에서 채워진다."""
    alert = {
        "alert_id": "ALERT-20260713-SIMCH1-9001", "chamber_id": "SIM_CH_1",
        "context_score": 38,
        "violations": [{"rule_id": "N3", "sensor": "C15", "severity": "WARNING",
                        "window": "settled", "current_value": 182.0,
                        "control_limit_upper": 179.0, "control_limit_lower": 149.0,
                        "description": "drift"}],
    }
    rows = S._rows_for(alert)
    assert len(rows) == 1
    r = rows[0]
    for col in ("alert_id", "ch", "rule", "sn", "sev", "win", "ver"):
        assert r[col] is not None, f"NOT NULL 컬럼 {col} 이 비었다"
    assert r["rc"] == S.SEED_RECIPE_ID and r["st"] == S.SEED_STEP


def test_multi_violation_alert_becomes_multiple_rows() -> None:
    """위반 N건 = N행 (운영 `insert_violations` 와 같은 규칙) — alert_id 는 공유한다."""
    alert = {"alert_id": "ALERT-20260713-SIMCH1-9002", "chamber_id": "SIM_CH_2",
             "violations": [{"rule_id": "N1", "sensor": "C11", "severity": "CRITICAL"},
                            {"rule_id": "N3", "sensor": "C15", "severity": "WARNING"}]}
    rows = S._rows_for(alert)
    assert len(rows) == 2
    assert {r["alert_id"] for r in rows} == {"ALERT-20260713-SIMCH1-9002"}


def test_broken_fixture_is_skipped_not_raised(tmp_path) -> None:
    """파손 fixture 는 스킵한다 — 시딩이 죽으면 라운드 자체를 못 돌린다(헌법 6-2)."""
    (tmp_path / "ok.json").write_text(
        json.dumps({"alert_id": "ALERT-20260713-SIMCH1-9003", "chamber_id": "SIM_CH_1",
                    "violations": []}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "no_id.json").write_text(json.dumps({"chamber_id": "SIM_CH_1"}), encoding="utf-8")
    got = [a["alert_id"] for a in S.iter_fixture_alerts(tmp_path)]
    assert got == ["ALERT-20260713-SIMCH1-9003"]


# --- 결과 기록에 정량이 실리는가 (2026-08-05) ------------------------------------
# 하루 종일 "recipe 제안 몇 건" 을 논하다 **지표 자체가 없다**는 것을 뒤늦게 알았다.
# 요약이 서술 7필드뿐이라 `parameter_id` 를 `.get()` 으로 읽으면 없는 키라 항상 None 이고,
# 그걸 "제안 0건" 으로 오독했다. 값이 안 실리는 것보다 **없는 값을 0으로 읽는 것**이 위험하다.
def test_recipe_quant_fields_are_recorded() -> None:
    """recipe 요약에 수치가 실린다 — 없으면 '제안이 나갔나'를 영영 못 잰다."""
    from src.agent_service.app.tools.recipe import RecipeOption
    from src.agent_service.eval_supervisor import _summarize_tool_report

    r = RecipeOption(report_id="RCP-Q", confidence=0.78, rationale="r", uncertainty="u",
                     parameter_id="C4", value_current=40.0, applied_value=39.6,
                     value_proposed=39.2, delta_pct=-2.0)
    out = _summarize_tool_report(r)
    assert out["parameter_id"] == "C4"
    assert out["value_current"] == 40.0 and out["applied_value"] == 39.6   # W21 두 값 모두
    assert out["value_proposed"] == 39.2 and out["delta_pct"] == -2.0


def test_escalated_recipe_records_none_not_missing_key() -> None:
    """물러선 리포트도 **키를 남긴다** — 키 부재와 값 None 은 다르다.

    키가 없으면 집계 코드가 `.get()` 으로 None 을 받아 "제안 0건" 과 구분되지 않는다.
    """
    from src.agent_service.app.tools.recipe import RecipeOption
    from src.agent_service.eval_supervisor import _summarize_tool_report

    r = RecipeOption(report_id="RCP-E", confidence=0.4, rationale="r", uncertainty="u",
                     escalate_reason="손잡이 축 아님")
    out = _summarize_tool_report(r)
    assert "parameter_id" in out and out["parameter_id"] is None


def test_limit_summary_does_not_leak_recipe_fields() -> None:
    """옵션마다 스키마가 다르다 — limit 요약에 recipe 전용 키가 섞이면 집계가 오염된다."""
    from src.agent_service.app.tools.limit import LimitOption
    from src.agent_service.eval_supervisor import _summarize_tool_report

    out = _summarize_tool_report(
        LimitOption(report_id="LIM-Q", confidence=0.85, rationale="r", uncertainty="u",
                    sensor_id="C11", method="sigma", delta_sigma=0.31))
    assert out["method"] == "sigma" and out["delta_sigma"] == 0.31
    assert "parameter_id" not in out and "delta_pct" not in out
