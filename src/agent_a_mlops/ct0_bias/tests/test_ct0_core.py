# -*- coding: utf-8 -*-
"""CT⓪ 코어 결정론 테스트 — 설계 §11 "테스트 카탈로그" 전 항목.

    clamp 경계(±상한·부호) / 최소 표본 미충족→0 / pm_count 자격 / 리셋 멱등(중복 이벤트)
    / 양자화 δ 경계 / NaN·비유한 라벨 skip / 재계산 determinism / ct_id 채번 / config 폴백

`core` 는 I/O 가 없으므로 브로커·DB·파일 없이 전부 돈다. 그것이 이 분리의 목적이다.

실행: python -m pytest src/agent_a_mlops/ct0_bias/tests/test_ct0_core.py -q
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ct0_bias import core  # noqa: E402


# ── 헬퍼 ──────────────────────────────────────────────────────────────────
def _cfg(**kw):
    base = dict(mode=core.MODE_ACTIVE, window_n=500, min_labels=50,
                max_rmse_ratio=1.0, min_update_delta=1.0)
    base.update(kw)
    return core.BiasConfig(**base)


def _state(residuals, *, rmse=100.0, boundary=core.NO_BOUNDARY, cfg=None, pm_count=None):
    st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=rmse, valid_from_pm_count=boundary)
    cfg = cfg or _cfg()
    for i, r in enumerate(residuals):
        core.push_label(st, core.Label(f"C64_{i}", r, pm_count=pm_count,
                                       model_version="lean85_v1"), cfg)
    return st


# ── 산식·clamp ────────────────────────────────────────────────────────────
def test_rolling_mean_and_apply():
    """표본 충족 시 rolling mean 이 그대로 서빙값이 된다 (반올림 2자리)."""
    st = _state([80.0] * 60)
    dec = core.decide(st, _cfg())
    assert dec.status == core.STATUS_APPLIED
    assert dec.bias == 80.0 and dec.n == 60
    assert dec.changed is True and dec.should_record() is True


def test_clamp_upper_and_lower():
    """|bias| > 1.0×RMSE 는 clamp 값으로 서빙 + CAP_EXCEEDED (헌법 1-1 ⓒ).

    부호 양쪽을 모두 본다 — 음의 bias 에서 부호를 잃으면 보정이 정반대로 간다.
    """
    up = core.decide(_state([300.0] * 60, rmse=100.0), _cfg())
    assert up.status == core.STATUS_CAP_EXCEEDED and up.bias == 100.0
    assert up.raw_bias == 300.0 and up.cap_exceeded is True

    down = core.decide(_state([-300.0] * 60, rmse=100.0), _cfg())
    assert down.status == core.STATUS_CAP_EXCEEDED and down.bias == -100.0


def test_clamp_boundary_is_inclusive():
    """정확히 상한이면 초과가 아니다 (`>` 판정 — 경계값에서 CAP 이 오발화하면 안 된다)."""
    dec = core.decide(_state([100.0] * 60, rmse=100.0), _cfg())
    assert dec.cap_exceeded is False and dec.status == core.STATUS_APPLIED


def test_ratio_below_one():
    """`bias_max_rmse_ratio` 를 낮추면 상한도 같이 내려간다 (config 가 산식의 정본)."""
    dec = core.decide(_state([80.0] * 60, rmse=100.0), _cfg(max_rmse_ratio=0.5))
    assert dec.bias == 50.0 and dec.cap_exceeded is True


# ── 최소 표본·분모 가드 ───────────────────────────────────────────────────
def test_insufficient_samples_serves_zero():
    """표본 < 50 이면 미적용. 이미 0 이면 변경이 없으므로 행도 남기지 않는다 (D4)."""
    dec = core.decide(_state([80.0] * 49), _cfg())
    assert dec.status == core.STATUS_INSUFFICIENT and dec.bias == 0.0
    assert dec.should_record() is False
    assert dec.record_status == core.STATUS_DISABLED     # 감사에는 "보정 꺼짐" 한 값으로


def test_falling_below_min_forces_zero_regardless_of_delta():
    """이미 보정 중이었다면 0 으로 되돌리는 변경은 δ 양자화를 타지 않는다 (안전 방향)."""
    st = _state([80.0] * 10)
    st.bias_applied = 80.0                       # 직전까지 적용 중이었다고 가정
    dec = core.decide(st, _cfg(min_update_delta=1000.0))
    assert dec.bias == 0.0 and dec.changed is True


def test_train_rmse_missing_disables():
    """분모 결손·0·음수 → 보정 비활성 (0 분모 = 조용한 기능 정지 방지, 헌법 7장)."""
    for bad in (None, 0.0, -5.0, float("nan")):
        dec = core.decide(_state([80.0] * 60, rmse=bad), _cfg())
        assert dec.status == core.STATUS_DISABLED and dec.bias == 0.0


def test_mode_off_disables():
    """mode=off 는 계산 결과와 무관하게 미적용."""
    dec = core.decide(_state([80.0] * 60), _cfg(mode=core.MODE_OFF))
    assert dec.status == core.STATUS_DISABLED and dec.bias == 0.0


def test_shadow_records_but_marks_shadow():
    """shadow 는 값을 계산하고 SHADOW 로 기록한다 — 적용 여부는 updater 가 가른다."""
    dec = core.decide(_state([80.0] * 60), _cfg(mode=core.MODE_SHADOW))
    assert dec.status == core.STATUS_SHADOW and dec.bias == 80.0 and dec.should_record()


# ── 양자화 δ (D4) ─────────────────────────────────────────────────────────
def test_quantization_delta_boundary():
    """δ 미만 변동은 갱신·기록하지 않는다. 정확히 δ 면 갱신한다 (`>=` 경계)."""
    st = _state([80.0] * 60)
    st.bias_applied = 79.5
    assert core.decide(st, _cfg(min_update_delta=1.0)).changed is False   # 0.5 < 1.0
    st.bias_applied = 79.0
    assert core.decide(st, _cfg(min_update_delta=1.0)).changed is True    # 1.0 >= 1.0


# ── 창 관리·자격·중복 ─────────────────────────────────────────────────────
def test_window_trims_and_forgets_seen():
    """창이 N 을 넘으면 오래된 것부터 빠지고 `seen` 도 같이 줄어든다 (메모리 누수 차단)."""
    cfg = _cfg(window_n=10)
    st = _state([1.0] * 25, cfg=cfg)
    assert st.n == 10 and len(st.seen) == 10


def test_duplicate_wafer_is_rejected():
    """같은 wafer_id 재전달은 창에 두 번 들어가지 않는다 (rolling mean 편향 방지)."""
    st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=100.0)
    cfg = _cfg()
    assert core.push_label(st, core.Label("C64_1", 10.0), cfg) is True
    assert core.push_label(st, core.Label("C64_1", 999.0), cfg) is False
    assert st.n == 1


def test_nonfinite_residual_is_skipped():
    """NaN/Inf 라벨은 창에 못 들어간다 (헌법 6-2 skip + log)."""
    st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=100.0)
    cfg = _cfg()
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert core.push_label(st, core.Label(f"w{bad}", bad), cfg) is False
    assert st.n == 0


def test_dummy_fallback_labels_are_excluded():
    """더미 폴백(`model_version="dummy"`) 예측의 라벨은 창에 못 들어간다.

    계약 §2 가 그 값을 남긴 이유가 "채점·Model R2R 에서 오염되지 않게"다. 더미는 난수
    (100±10)라 60일 뒤 라벨과 짝지으면 잔차가 수백 단위로 나와 rolling mean 을 끌어간다.
    """
    st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=100.0)
    cfg = _cfg()
    assert core.push_label(st, core.Label("w1", 10.0, model_version="dummy"), cfg) is False
    assert core.push_label(st, core.Label("w2", 10.0, model_version="DUMMY"), cfg) is False
    assert core.push_label(st, core.Label("w3", 10.0, model_version="lean85_v1"), cfg) is True
    assert st.n == 1
    # DB 재계산 경로도 같은 규칙이어야 determinism 이 성립한다
    rows = [("d1", 100.0, 0.0, 900.0, 25, "dummy"), ("d2", 900.0, 0.0, 1000.0, 25, "lean85_v1")]
    assert [x.wafer_id for x in core.labels_from_rows(rows)] == ["d2"]


def test_status_transition_forces_a_record():
    """서빙값이 그대로여도 **상태가 바뀌면** 기록한다 (CAP 진입의 dangling ct_id 방지)."""
    st = _state([100.0] * 60, rmse=100.0)          # 정확히 상한 → APPLIED(100)
    st.bias_applied = 100.0
    dec = core.decide(st, _cfg())
    assert dec.changed is False and dec.should_record(core.STATUS_APPLIED) is False
    # 같은 서빙값(clamp 100)인데 raw 만 커져 CAP 으로 전이한 경우
    st2 = _state([300.0] * 60, rmse=100.0)
    st2.bias_applied = 100.0
    dec2 = core.decide(st2, _cfg())
    assert dec2.bias == 100.0 and dec2.changed is False
    assert dec2.should_record(core.STATUS_APPLIED) is True      # 전이 → 기록
    assert dec2.should_record(core.STATUS_CAP_EXCEEDED) is False  # 지속 → 무기록


def test_residual_uses_raw_prediction():
    """잔차는 **보정 전 예측** 대비다 (D1) — 되먹임 루프 차단의 핵심."""
    # actual 1000, 발행 예측 900 인데 그중 50 이 bias → raw=850 → 잔차 150
    assert core.residual_of(1000, 900, 50) == 150.0
    assert core.residual_of(1000, 900, 0) == 100.0
    assert core.residual_of(1000, 900, None) == 100.0        # bias 결손 = 0 취급
    assert core.residual_of(float("nan"), 900, 0) is None


def test_pm_count_eligibility():
    """경계 이후 라벨만 편입. 경계가 없으면 전량 자격 (설계 §5)."""
    assert core.is_eligible(25, core.NO_BOUNDARY) is True
    assert core.is_eligible(None, core.NO_BOUNDARY) is True
    assert core.is_eligible(24, 25) is False
    assert core.is_eligible(25, 25) is True
    assert core.is_eligible(None, 25) is False               # 경계가 있으면 미상은 배제


def test_reset_filters_old_labels_and_zeroes_bias():
    """요란 리셋: 구레짐 라벨 차단 + bias=0 (신규 결정 5)."""
    cfg = _cfg()
    st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=100.0)
    for i in range(60):
        core.push_label(st, core.Label(f"old_{i}", 80.0, pm_count=24), cfg)
    st.bias_applied = 80.0
    assert core.apply_reset(st, 25) is True
    assert st.n == 0 and st.bias_applied == 0.0 and st.valid_from_pm_count == 25
    assert core.push_label(st, core.Label("old_x", 80.0, pm_count=24), cfg) is False
    assert core.push_label(st, core.Label("new_1", 10.0, pm_count=25), cfg) is True


def test_reset_is_idempotent_on_redelivery():
    """리셋 이벤트 재전달은 no-op — 그동안 쌓인 신레짐 창을 날리지 않는다."""
    cfg = _cfg()
    st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=100.0)
    assert core.apply_reset(st, 25) is True
    for i in range(60):
        core.push_label(st, core.Label(f"new_{i}", 10.0, pm_count=25), cfg)
    assert core.apply_reset(st, 25) is False                 # 같은 경계 = 무시
    assert core.apply_reset(st, 24) is False                 # 더 낮은 경계 = 무시
    assert st.n == 60


def test_reset_requires_int_pm_count():
    """pm_count 없는 리셋 이벤트는 경계를 못 세우므로 상태를 바꾸지 않는다."""
    st = core.BiasState(chamber_id="SIM_CH_3")
    assert core.apply_reset(st, None) is False
    assert core.apply_reset(st, "loud") is False


# ── 재계산 determinism (설계 §10 리허설 ①) ────────────────────────────────
def test_recompute_is_deterministic():
    """같은 입력 → 같은 bias. 두 번 계산해 같은 값이 나와야 부트스트랩을 믿을 수 있다."""
    rows = [(f"C64_{i}", 900.0 + i, 10.0, 1000.0 + (i % 7), 25, "lean85_v1") for i in range(120)]
    cfg = _cfg()

    def build():
        st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=100.0, valid_from_pm_count=25)
        for lab in core.labels_from_rows(rows):
            core.push_label(st, lab, cfg)
        return core.decide(st, cfg)

    a, b = build(), build()
    assert a.bias == b.bias and a.raw_bias == b.raw_bias and a.n == b.n == 120


def test_labels_from_rows_drops_unlabelled():
    """라벨 없는 행·비유한 행은 조용히 버린다 (원천이 DB 든 Kafka 든 같은 규칙)."""
    rows = [("w1", 900.0, 0.0, 1000.0, 25, "v1"),
            ("w2", 900.0, 0.0, None, 25, "v1"),          # 라벨 미도착
            ("w3", None, 0.0, 1000.0, 25, "v1"),         # 예측 결손
            ("w4", 900.0, 0.0, float("nan"), 25, "v1")]  # NaN 라벨
    out = core.labels_from_rows(rows)
    assert [x.wafer_id for x in out] == ["w1"] and out[0].residual == 100.0


# ── ID 채번·사유 문자열 ───────────────────────────────────────────────────
def test_ct_id_is_deterministic_and_scoped():
    """같은 (day, chamber, anchor) → 같은 ct_id. 하나라도 다르면 달라진다 (§7 멱등)."""
    a = core.ct_id_for("20260907", "SIM_CH_3", "C64_1")
    assert a == core.ct_id_for("20260907", "SIM_CH_3", "C64_1")
    assert a != core.ct_id_for("20260907", "SIM_CH_3", "C64_2")
    assert a != core.ct_id_for("20260907", "SIM_CH_4", "C64_1")
    assert a != core.ct_id_for("20260908", "SIM_CH_3", "C64_1")
    assert a.startswith("CT-20260907-SIMCH3-") and a.endswith("-R2R")
    assert len(a) <= core.CT_ID_MAX


def test_reason_roundtrip_for_boundary_recovery():
    """RESET 사유 문자열에서 경계를 되읽을 수 있어야 한다 (§7 폴백의 유일한 소스)."""
    r = core.reset_reason(25, "QUAL-20260713-SIMCH3-001")
    assert core.parse_reset_reason(r) == 25
    assert len(r) <= core.TRIGGER_REASON_MAX
    assert core.parse_reset_reason("아무 말") is None
    assert core.parse_reset_reason(None) is None


def test_update_reason_fits_column():
    """갱신 사유도 VARCHAR(64) 안에 들어가야 한다 (잘림은 감사 조회를 깨뜨린다)."""
    dec = core.decide(_state([123.456] * 60), _cfg())
    r = core.update_reason(dec, core.MODE_ACTIVE)
    assert r.startswith("residual_bias") and len(r) <= core.TRIGGER_REASON_MAX


# ── config 폴백 ───────────────────────────────────────────────────────────
def test_config_defaults_lock_to_off():
    """params 로드 실패(`{}`)·미지의 mode → off 로 잠금 (무인 적용 금지 규율)."""
    assert core.BiasConfig.from_params({}).mode == core.MODE_OFF
    assert core.BiasConfig.from_params(None).mode == core.MODE_OFF
    assert core.BiasConfig.from_params({"model_r2r": {"mode": "ACTIVE"}}).mode == core.MODE_ACTIVE
    assert core.BiasConfig.from_params({"model_r2r": {"mode": "야호"}}).mode == core.MODE_OFF
    # YAML 1.1 이 따옴표 없는 off 를 False 로 읽어도 결과는 off 여야 한다
    assert core.BiasConfig.from_params({"model_r2r": {"mode": False}}).mode == core.MODE_OFF


def test_config_reads_real_params_yaml():
    """실 `params.yaml` 의 `ct.model_r2r` 가 코어가 기대하는 형태인지 (계약-코드 정합).

    ⚠️ 값을 못박지 않는다 — 컷오버로 `mode` 가 바뀔 때 이 테스트가 red 가 되면 안 된다
    (헌법 7장 `publish_enabled` 선례: config 플립이 CI 만 더럽히던 사고).
    """
    import yaml
    repo = Path(__file__).resolve().parents[4]
    with open(repo / "config" / "params.yaml", encoding="utf-8") as f:
        ct = (yaml.safe_load(f) or {}).get("ct") or {}
    cfg = core.BiasConfig.from_params(ct)
    assert cfg.mode in core.MODES
    assert cfg.window_n == 500 and cfg.min_labels == 50        # 멘토 확정값 (산식 정본)
    assert cfg.max_rmse_ratio == 1.0
    assert cfg.min_update_delta >= 0
    assert "min_update_delta" in (ct.get("model_r2r") or {}), "§12-6 신설 키가 없다"


def test_window_observability_helpers():
    """관측 지표 — 창 혼합·잔차 σ (D2 감시용)."""
    st = _state([10.0, 20.0, 30.0])
    mean, sd = core.residual_stats(st)
    assert mean == 20.0 and math.isclose(sd, 10.0)
    assert core.window_mix(st) == {"lean85_v1": 3}
    assert core.dominant_model_version(st) == "lean85_v1"
    empty = core.BiasState(chamber_id="X")
    assert core.residual_stats(empty) == (None, None)
    assert core.dominant_model_version(empty) is None
