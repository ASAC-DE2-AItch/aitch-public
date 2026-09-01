"""Step 4 공통(combine·orchestrator·instrumentation) 단위 테스트.

스펙: Docs/B4-1_Step4_5_5b_통합구현계획서_v2.md §8-① · D-1~D-7.
순수 로직이라 Kafka/DB 불필요 — joined 부분 dict + cfg 주입(매직넘버 하드코딩 금지).

핵심 계약: combine = dict naive_sum(후처리 없음, D-1·D-2) · 과도 할인은 combine **前**
전 키 적용(D-3) · clip 경계는 params(cfg) 경유 · 반환은 float(int 변환은 seam, D-5) ·
계측 실패가 점수 반환을 막지 않는다(D-6) · 오케스트레이터는 enrichment 안 함(D-7).
"""
from __future__ import annotations

import copy
import json

import pytest

from src.common.context_score import context_score as orch
from src.common.context_score import instrumentation as instr
from src.common.context_score.combine import AXIS_KEYS, combine
from src.common.context_score.config import ContextScoreConfig
from src.common.context_score.context_score import context_score
from src.common.context_score.instrumentation import LOG_FIELDS, build_record, log_contribution


def _cfg(**over) -> ContextScoreConfig:
    """테스트용 cfg — 계산 검증 쉬운 정수 배점(실 params와 무관·test_axes와 동일 골격)."""
    base = dict(
        rule_base={"N1": 10.0, "N2": 12.0, "N3": 15.0, "N4": 12.0,
                   "N5": 12.0, "N6": 12.0, "N7": 12.0, "N8": 12.0},
        size_per_sigma=5.0,
        persistence_bonus=10.0,
        tttm_warning=2.0,
        tttm_critical=3.0,
        tttm_warning_pts=15.0,
        tttm_critical_pts=30.0,
        reference_suspect_penalty=20.0,
        transient_discount=0.5,
        k_sigma=3.0,
        clip_min=0.0,
        clip_max=100.0,
        ae_band_edges=(0.2, 0.5, 1.0),
        ae_band_pts=(10.0, 20.0, 30.0),
        ae_drift_lo=0.3,
        ae_drift_hi=0.7,
        ae_pts_drift_lo=10.0,
        ae_pts_drift_hi=20.0,
        ae_aggregate="max",
    )
    base.update(over)
    return ContextScoreConfig(**base)


def _v(rule_id="N1", current_value=100.0, ucl=110.0, lcl=90.0, sensor_window="settled"):
    """Nelson 위반 dict 최소본 (nelson_engine._make_violation 정합)."""
    return {"rule_id": rule_id, "current_value": current_value,
            "control_limit_upper": ucl, "control_limit_lower": lcl,
            "chamber_id": "SIM_CH_1", "recipe_id": "C6_0", "step": 4,
            "sensor_window": sensor_window, "sensor_id": "C11"}


def _joined(violations=None, tttm=None, prediction=None, is_transient=False, **over):
    """joined 이벤트 최소본 (collector.build_joined 스키마 정합)."""
    j = {"wafer_id": "C64_1", "chamber_id": "SIM_CH_1", "recipe_id": "C6_0",
         "ts": "2026-07-28T01:00:00+00:00",
         "violations": violations if violations is not None else [],
         "tttm": tttm, "prediction": prediction,
         "is_transient": is_transient, "is_qual": False}
    j.update(over)
    return j


@pytest.fixture(autouse=True)
def _no_disk_log(monkeypatch, tmp_path):
    """계측 덤프를 tmp로 격리 — 테스트가 리포 logs/ 를 오염시키지 않게."""
    monkeypatch.setattr(orch, "log_contribution",
                        lambda *a, **k: log_contribution(*a, path=tmp_path / "c.jsonl", **k))
    return tmp_path / "c.jsonl"


# ── combine (D-1·D-2·D-3) ────────────────────────────────────────────────────

def test_combine_naive_sum_dict():
    """dict naive_sum — 계획서 §8-① 예시 그대로 30."""
    assert combine({"spc": 15.0, "tttm": 15.0, "ae": 0.0, "pred": 0.0}, _cfg()) == 30.0


def test_combine_empty_dict_zero():
    """빈 dict → 0.0 (sum(빈)=0, 크래시 없음)."""
    assert combine({}, _cfg()) == 0.0


def test_combine_order_independent():
    """dict라 키 순서에 의존하지 않는다 — list 시그니처의 순서 침묵오류 제거(D-1)."""
    a = {"spc": 10.0, "tttm": 20.0, "ae": 30.0, "pred": 5.0}
    b = {"pred": 5.0, "ae": 30.0, "tttm": 20.0, "spc": 10.0}
    assert combine(a, _cfg()) == combine(b, _cfg())


def test_combine_accepts_negative_axis():
    """축은 음수 가능(score_tttm net) — combine은 clip하지 않는다(후처리=orchestrator, D-2)."""
    assert combine({"spc": 0.0, "tttm": -5.0, "ae": 0.0, "pred": 0.0}, _cfg()) == -5.0


def test_combine_does_not_clip_or_discount():
    """combine은 결합 전담 — 100 초과도 그대로 반환(clip은 orchestrator)."""
    assert combine({"spc": 200.0, "tttm": 0.0, "ae": 0.0, "pred": 0.0}, _cfg()) == 200.0


def test_axis_keys_contract():
    """축 키 계약 고정 — 오케스트레이터 scores dict와 일치해야 한다."""
    assert AXIS_KEYS == ("spc", "tttm", "ae", "pred")


# ── 4축 dispatch ─────────────────────────────────────────────────────────────

def test_dispatch_all_four_axes():
    """네 축이 각자 자기 필드를 읽어 합산된다.

    spc: N1 밴드 내 = 10 / tttm: 2.3 → warning 15 / ae: anomaly 0.5 → 20 / pred: 비활성 0.
    합 = 45.
    """
    j = _joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3},
                prediction={"anomaly_score": 0.5, "drift_score": 0.0,
                            "predicted_c65": 1500.0})
    assert context_score(j, _cfg()) == pytest.approx(45.0)


def test_pred_axis_inactive_when_weight_null(_no_disk_log):
    """pred_weight=null(기본) → pred 축 0 (D-4). p95 초과 예측이 있어도 기여 없음."""
    j = _joined(prediction={"predicted_c65": 9999.0, "p95_threshold": 1000.0,
                            "phase": "normal"})
    assert context_score(j, _cfg()) == 0.0


def test_pred_axis_active_contributes():
    """pred_weight 설정 시 축이 살아난다 — gap 100/scale 100 ×10 = 10."""
    cfg = _cfg(pred_weight=10.0, pred_p95_scale=100.0, pred_w_cap=3.0)
    j = _joined(prediction={"predicted_c65": 1600.0, "p95_threshold": 1500.0,
                            "phase": "normal"})
    assert context_score(j, cfg) == pytest.approx(10.0)


def test_prediction_none_ae_pred_zero_no_crash():
    """★③ 예측 결측 → AE·pred 0, SPC/TTTM은 무지연 채점(10 + 15 = 25)."""
    j = _joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3}, prediction=None)
    assert context_score(j, _cfg()) == pytest.approx(25.0)


def test_missing_joined_keys_use_get_defaults():
    """joined 키 결측(violations·tttm·prediction 없음) → .get 방어로 0 (D-9)."""
    assert context_score({"wafer_id": "W1"}, _cfg()) == 0.0


def test_violations_none_treated_as_empty():
    """violations=None → `or []` 로 흡수(축에 None 안 넘김)."""
    assert context_score(_joined(violations=None), _cfg()) == 0.0


# ── 과도 할인 (D-3) ──────────────────────────────────────────────────────────

def test_transient_discount_applies_to_all_keys():
    """is_transient=True → 전 키 ×0.5. 무할인 45 → 22.5 (a안 = 합산 후 ×0.5와 등가)."""
    j = _joined(violations=[_v("N1", 100.0, sensor_window="transient")],
                tttm={"score": 2.3},
                prediction={"anomaly_score": 0.5, "drift_score": 0.0},
                is_transient=True)
    assert context_score(j, _cfg()) == pytest.approx(22.5)


def test_no_discount_when_not_transient():
    """is_transient=False → 그대로."""
    j = _joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3})
    assert context_score(j, _cfg()) == pytest.approx(25.0)


def test_discount_helper_targets_all_axis_keys():
    """_apply_transient_discount 격리 — (a)안이라 네 키 전부 ×discount."""
    scores = {"spc": 10.0, "tttm": 20.0, "ae": 30.0, "pred": 40.0}
    out = orch._apply_transient_discount(scores, True, _cfg())
    assert out == {"spc": 5.0, "tttm": 10.0, "ae": 15.0, "pred": 20.0}


def test_discount_helper_is_pure():
    """할인은 새 dict 반환 — 입력 변이 금지(계측이 원본을 다시 볼 수 있게)."""
    scores = {"spc": 10.0, "tttm": 20.0, "ae": 30.0, "pred": 40.0}
    snap = copy.deepcopy(scores)
    orch._apply_transient_discount(scores, True, _cfg())
    assert scores == snap


def test_discount_uses_config_not_hardcoded_half():
    """할인계수는 cfg 경유 — 0.5 하드코딩 아님(6-1). 0.1로 바꾸면 결과도 바뀐다."""
    j = _joined(violations=[_v("N1", 100.0)], is_transient=True)
    assert context_score(j, _cfg(transient_discount=0.1)) == pytest.approx(1.0)


# ── clip (경계는 params) ─────────────────────────────────────────────────────

def test_clip_negative_to_min():
    """음수 raw(TTTM net) → clip_min. warning 15 − suspect 20 = −5 → 0."""
    j = _joined(tttm={"score": 2.3, "reference_suspect": True})
    assert context_score(j, _cfg()) == 0.0


def test_clip_over_max():
    """합이 상한 초과 → clip_max. 큰 초과 위반 여러 스트림 대신 배점을 키워 검증."""
    cfg = _cfg(rule_base={"N1": 500.0})
    assert context_score(_joined(violations=[_v("N1", 100.0)]), cfg) == 100.0


def test_clip_bounds_come_from_config():
    """clip 경계는 cfg 값 — 0·100 하드코딩이면 이 테스트가 깨진다(6-1)."""
    cfg = _cfg(rule_base={"N1": 500.0}, clip_min=-10.0, clip_max=50.0)
    assert context_score(_joined(violations=[_v("N1", 100.0)]), cfg) == 50.0
    assert context_score(_joined(tttm={"score": 2.3, "reference_suspect": True},
                                 ), _cfg(clip_min=-10.0, clip_max=50.0)) == -5.0


def test_returns_float_not_int():
    """반환은 float — int 변환은 seam 1곳(D-5)."""
    assert isinstance(context_score(_joined(violations=[_v("N1", 100.0)]), _cfg()), float)


# ── _safe 값 소독 (D-9) ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None,
                                 "oops", True])
def test_safe_sanitizes_bad_axis_output(bad):
    """축이 NaN·Inf·None·비수치·bool을 반환해도 0.0으로 소독. bool은 float(True)=1.0 차단."""
    assert orch._safe(bad) == 0.0


def test_safe_passes_through_numbers():
    """정상 수치는 그대로(음수 포함 — TTTM net 보존)."""
    assert orch._safe(17.5) == 17.5
    assert orch._safe(-5.0) == -5.0
    assert orch._safe(0) == 0.0


def test_nan_axis_output_does_not_poison_total(monkeypatch):
    """한 축이 NaN을 뱉어도 총점이 NaN으로 오염되지 않는다(나머지 축 정상 채점)."""
    monkeypatch.setattr(orch, "score_ae", lambda *a, **k: float("nan"))
    j = _joined(violations=[_v("N1", 100.0)], prediction={"anomaly_score": 0.5})
    assert context_score(j, _cfg()) == pytest.approx(10.0)


# ── 계측 격리 (D-6) ──────────────────────────────────────────────────────────

def test_log_failure_does_not_block_score(monkeypatch, caplog):
    """계측 예외를 주입해도 점수는 정상 반환 + warning 1건 (side-channel 격리)."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(orch, "log_contribution", _boom)
    with caplog.at_level("WARNING"):
        score = context_score(_joined(violations=[_v("N1", 100.0)]), _cfg())
    assert score == pytest.approx(10.0)
    assert any("contribution_log" in r.getMessage() for r in caplog.records)


def test_contribution_log_written_as_jsonl(_no_disk_log):
    """계측 1행이 JSONL로 append — 필드·값 확인(Step 6 입력 자산)."""
    j = _joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3},
                prediction={"anomaly_score": 0.5, "drift_score": 0.0})
    context_score(j, _cfg())
    rows = [json.loads(line) for line in _no_disk_log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["wafer_id"] == "C64_1" and r["chamber_id"] == "SIM_CH_1"
    assert (r["s_spc"], r["s_tttm"], r["s_ae"], r["s_pred"]) == (10.0, 15.0, 20.0, 0.0)
    assert r["raw_combined"] == 45.0 and r["final"] == 45.0
    assert r["is_transient"] is False and r["prediction_present"] is True


def test_contribution_log_appends_not_truncates(_no_disk_log):
    """두 번 채점하면 2행 — append 모드(기존 이력 덮어쓰기 금지)."""
    for _ in range(2):
        context_score(_joined(violations=[_v("N1", 100.0)]), _cfg())
    assert len(_no_disk_log.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_contribution_record_logs_discounted_scores(_no_disk_log):
    """기록되는 축 값은 **할인 적용 후**(= combine 입력)라 raw_combined와 정합."""
    j = _joined(violations=[_v("N1", 100.0)], is_transient=True)
    context_score(j, _cfg())
    r = json.loads(_no_disk_log.read_text(encoding="utf-8").splitlines()[0])
    assert r["s_spc"] == 5.0 and r["raw_combined"] == 5.0


def test_build_record_is_pure_and_missing_key_safe():
    """build_record는 순수·IO 없음 · joined 키 결측에도 무크래시(.get)."""
    rec = build_record({}, {}, 0.0, 0.0)
    assert rec["wafer_id"] is None and rec["prediction_present"] is False
    assert rec["is_transient"] is False


def test_record_schema_matches_log_fields():
    """스키마 드리프트 가드 — build_record 키 집합·순서 == LOG_FIELDS.

    두 곳(상수·조립부)이 이중 소스라 한쪽만 고치면 Step 6 분석 스크립트가 깨진다.
    """
    assert tuple(build_record(_joined(), {}, 0.0, 0.0)) == LOG_FIELDS


def test_log_creates_missing_directory(tmp_path):
    """덤프 경로의 디렉토리가 없으면 생성한다(첫 기동 시 logs/ 부재)."""
    target = tmp_path / "nested" / "deeper" / "c.jsonl"
    log_contribution(_joined(), {"spc": 1.0}, 1.0, 1.0, path=target)
    assert target.exists() and target.read_text(encoding="utf-8").strip()


# ── 계측 경로 해석 (params 키 계약) ──────────────────────────────────────────
# autouse fixture가 log_contribution을 우회하므로 _configured_log_path는 다른
# 테스트에서 한 번도 안 돈다 → 키 오타·삭제가 라이브에서만 터진다. 여기서 직접 건다.

def _write_params(tmp_path, spc_body: str):
    """최소 params.yaml 생성 — spc 섹션만."""
    p = tmp_path / "params.yaml"
    p.write_text(f"spc:\n{spc_body}\n", encoding="utf-8")
    return p


def test_configured_log_path_resolves_relative_to_repo_root(tmp_path):
    """상대경로는 리포 루트 기준으로 해석 — 실행 cwd에 따라 로그가 흩어지지 않게."""
    instr._configured_log_path.cache_clear()
    try:
        p = _write_params(tmp_path, "  context_score_contribution_log_path: logs/x.jsonl")
        assert instr._configured_log_path(p) == instr.REPO_ROOT / "logs/x.jsonl"
    finally:
        instr._configured_log_path.cache_clear()


def test_configured_log_path_keeps_absolute(tmp_path):
    """절대경로(POSIX /...)는 그대로 사용 — 플랫폼 무관(as_posix로 비교, Windows 백슬래시 회피)."""
    instr._configured_log_path.cache_clear()
    try:
        p = _write_params(tmp_path, "  context_score_contribution_log_path: /var/log/x.jsonl")
        assert instr._configured_log_path(p).as_posix() == "/var/log/x.jsonl"
    finally:
        instr._configured_log_path.cache_clear()


def test_configured_log_path_missing_key_raises(tmp_path):
    """키 부재 → 매직넘버 대체 없이 명시적 실패(6-1). 오케스트레이터가 warning으로 흡수."""
    instr._configured_log_path.cache_clear()
    try:
        p = _write_params(tmp_path, "  context_score_agent_min: 31")
        with pytest.raises(KeyError, match="context_score_contribution_log_path"):
            instr._configured_log_path(p)
    finally:
        instr._configured_log_path.cache_clear()


def test_live_params_has_contribution_log_key():
    """실 params.yaml에 키가 실재하는지 — 오타·삭제를 라이브 전에 잡는다(§7 등재 확인)."""
    instr._configured_log_path.cache_clear()
    try:
        assert instr._configured_log_path().name.endswith(".jsonl")
    finally:
        instr._configured_log_path.cache_clear()


def test_prediction_present_false_when_missing(_no_disk_log):
    """예측 결측 → prediction_present False (조인 miss 추적 — 2-1 레이스 계측 입력)."""
    context_score(_joined(prediction=None), _cfg())
    r = json.loads(_no_disk_log.read_text(encoding="utf-8").splitlines()[0])
    assert r["prediction_present"] is False


# ── 순수성 · 경계 (D-7) ──────────────────────────────────────────────────────

def test_orchestrator_does_not_enrich_prediction(_no_disk_log):
    """오케스트레이터는 phase·p95_threshold를 주입하지 않는다 (seam 몫, D-7).

    주입을 여기서 하면 collector 화이트리스트와 책임이 겹치고, seam이 안 채웠을 때
    조용히 통과해버린다. 입력 dict가 그대로여야 한다.
    """
    pred = {"predicted_c65": 1600.0, "anomaly_score": 0.1, "drift_score": 0.1}
    snap = copy.deepcopy(pred)
    context_score(_joined(prediction=pred), _cfg())
    assert pred == snap


def test_orchestrator_deterministic_and_input_unchanged(_no_disk_log):
    """동일 입력 2회 동일 반환 + joined 무변이(순수)."""
    j = _joined(violations=[_v("N1", 115.0)], tttm={"score": 3.5},
                prediction={"anomaly_score": 0.3, "drift_score": 0.9})
    snap = copy.deepcopy(j)
    assert context_score(j, _cfg()) == context_score(j, _cfg())
    assert j == snap


def test_no_leakage_actual_c65_ignored(_no_disk_log):
    """누수 가드(1-3) — 실측 C65 유사 키가 있어도 점수에 영향 없다."""
    cfg = _cfg(pred_weight=10.0, pred_p95_scale=100.0, pred_w_cap=3.0)
    clean = context_score(_joined(prediction={"predicted_c65": 1450.0,
                                              "p95_threshold": 1500.0}), cfg)
    dirty = context_score(_joined(prediction={"predicted_c65": 1450.0,
                                              "p95_threshold": 1500.0,
                                              "actual_c65": 9999.0, "c65": 9999.0}), cfg)
    assert clean == dirty == 0.0


# ── 실 params 정합 ───────────────────────────────────────────────────────────

def test_runs_with_loaded_config(_no_disk_log):
    """**정책 고정(tripwire)** — 실 params 로드 cfg 관통 점수. 배점이 바뀌면 red 다.

    분해: score_spc(N1 밴드 내)=**20** · score_tttm(2.3)=**0** · score_ae(0.5)=**0**
    · score_pred(pred_weight=null)=0 → **20**.
    (스트림이 1개라 stream_damping 은 기여하지 않는다 — 더할 '나머지'가 없다.)

    ⚠️ 실패하면 "테스트가 낡았다"가 아니라 **"배점이 바뀌었다"** 는 신호다 — 바꾼 PR 안에서
    같이 갱신하고 이력을 남긴다 (test_axes.py::test_config_values_are_the_agreed_set 와 세트).

    이력:
      · 2026-08-05 — e4 격리(tttm_*_pts 0)로 tttm 기여 15→0, 총점 45→30
      · **2026-08-11 — e6·e7 격리**(ae_band_pts 0)로 ae 기여 20→0, 총점 30→10.
        살아 있는 축이 SPC(e1+e3) 하나만 남은 상태를 이 숫자가 그대로 보여준다.
      · **2026-08-11 — G8 선택 A 확정**(rule_base N1 10→20)로 spc 기여 10→20, 총점 10→**20**.
    """
    cfg = ContextScoreConfig.load()
    j = _joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3},
                prediction={"anomaly_score": 0.5, "drift_score": 0.0})
    assert context_score(j, cfg) == pytest.approx(20.0)


def test_gate_threshold_boundary_scale(_no_disk_log):
    """게이트(≥31) 경계 감각 — **명시 cfg** 기준. 배점 손잡이와 분리한다.

    게이트 판정 자체는 하류 C 몫이고, 여기서는 "B축만으로는 못 넘고 AE 수렴이 얹히면
    넘는다"는 **눈금 설계**만 본다(계획서 §8-① "게이트 31 경계"). 실 params 로 재면
    배점 컷오버마다 이 의도가 깨지므로(8/5 e4 격리로 실제 깨졌다) 명시 cfg 를 쓴다.
    """
    cfg = _cfg()                                     # 설계 가정 배점(15/30/20 등)
    b_only = context_score(_joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3}), cfg)
    converged = context_score(
        _joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3},
                prediction={"anomaly_score": 0.5, "drift_score": 0.0}), cfg)
    assert b_only < 31 <= converged


def test_loaded_config_scale_vs_gate_is_recorded(_no_disk_log):
    """실 params 의 현재 눈금이 게이트(31)에 대해 **어디 서 있는지** 기록한다.

    단조성(B축 ≤ B+AE)은 언제나 성립해야 하는 불변식이고, 게이트 도달 여부는 지금
    **미달**이다 — e4·e5 를 0 으로 격리한 상태라 수렴 예시가 30 에서 멈춘다. G8 에서
    배점·게이트를 세트로 확정하면 이 테스트가 red 가 되어 재확인을 강제한다.
    """
    cfg = ContextScoreConfig.load()
    b_only = context_score(_joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3}), cfg)
    converged = context_score(
        _joined(violations=[_v("N1", 100.0)], tttm={"score": 2.3},
                prediction={"anomaly_score": 0.5, "drift_score": 0.0}), cfg)
    assert cfg.clip_min <= b_only <= converged <= cfg.clip_max   # 불변식
    assert converged < 31, "수렴 예시가 게이트를 넘었다 — G8 확정분이면 이 테스트를 갱신할 것"
