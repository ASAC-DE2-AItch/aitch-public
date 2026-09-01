"""축별 순수 서브함수 단위 테스트 (B 담당: score_spc · score_tttm).

스펙: Docs/spec/B4-1_Step3-4_collector_score_스펙플랜.md §5·§6·§9.
순수 함수라 Kafka/DB 불필요 — joined 부분 dict + cfg 주입(매직넘버 하드코딩 금지, §4-②).
배점은 설계 §4 가안을 B가 확정한 값(params `spc.context_score_*` flat)이며 테스트는 값 독립.

핵심 계약(스펙 결정): e2 지속성 = v0 보류(0) · 축은 raw 기여(clip·할인은 combine) ·
score_spc = 스트림별 (e1 max + e3) → 스트림 간 max · score_tttm = e4 − e5(음수 허용).
"""
from __future__ import annotations

import copy

import pytest

from src.common.context_score.config import ContextScoreConfig
from src.common.context_score.axes.score_spc import score_spc
from src.common.context_score.axes.score_tttm import score_tttm
from src.common.context_score.axes.score_pred import score_pred
from src.common.context_score.axes.score_ae import score_ae


def _cfg(**over) -> ContextScoreConfig:
    """테스트용 cfg — 계산 검증 쉬운 정수 배점(실 params와 무관)."""
    base = dict(
        rule_base={"N1": 10.0, "N2": 12.0, "N3": 15.0, "N4": 12.0,
                   "N5": 12.0, "N6": 12.0, "N7": 12.0, "N8": 12.0},
        size_per_sigma=5.0,
        persistence_bonus=10.0,          # v0 미사용(선등재 키) — score_spc는 안 씀
        tttm_warning=2.0,
        tttm_critical=3.0,
        tttm_warning_pts=15.0,
        tttm_critical_pts=30.0,
        reference_suspect_penalty=20.0,
        transient_discount=0.5,          # combine 소비(축은 안 씀)
        k_sigma=3.0,
        clip_min=0.0,                    # B0 — orchestrator clip 경계(축 테스트엔 무영향)
        clip_max=100.0,
        stream_damping=0.0,              # T14 — 0.0 = 현행 max (기본값)
    )
    base.update(over)
    return ContextScoreConfig(**base)


def _v(rule_id, current_value, ucl=110.0, lcl=90.0,
       chamber_id="SIM_CH_1", recipe_id="C6_0", step=4, sensor_window="settled", sensor_id="C11"):
    """Nelson 위반 dict 최소본 (nelson_engine _make_violation 정합 — score_spc가 읽는 키)."""
    return {"rule_id": rule_id, "current_value": current_value,
            "control_limit_upper": ucl, "control_limit_lower": lcl,
            "chamber_id": chamber_id, "recipe_id": recipe_id, "step": step,
            "sensor_window": sensor_window, "sensor_id": sensor_id}


# ── score_tttm (§6) ─────────────────────────────────────────────────────────

def test_score_tttm_none_zero():
    assert score_tttm(None, _cfg()) == 0.0


def test_score_tttm_empty_dict_zero():
    assert score_tttm({}, _cfg()) == 0.0


def test_score_tttm_warning_band():
    """warning(2.0) ≤ score < critical → +15."""
    assert score_tttm({"score": 2.3, "reference_suspect": False}, _cfg()) == 15.0


def test_score_tttm_critical_band():
    """score ≥ critical(3.0) → +30."""
    assert score_tttm({"score": 3.1, "reference_suspect": False}, _cfg()) == 30.0


def test_score_tttm_below_warning_zero():
    assert score_tttm({"score": 1.5, "reference_suspect": False}, _cfg()) == 0.0


def test_score_tttm_reference_suspect_net():
    """critical + suspect → e4(30) − e5(20) = 10 (net, A2)."""
    assert score_tttm({"score": 3.5, "reference_suspect": True}, _cfg()) == 10.0


def test_score_tttm_net_can_be_negative():
    """warning + suspect → 15 − 20 = −5 (음수 허용 — clip은 combine, 스펙 §6·§9)."""
    assert score_tttm({"score": 2.3, "reference_suspect": True}, _cfg()) == -5.0


# ── score_spc (§5) ──────────────────────────────────────────────────────────

def test_score_spc_empty_zero():
    assert score_spc([], _cfg()) == 0.0


def test_score_spc_n1_base_plus_size_no_persistence():
    """N1(ucl110·current115, σ=(110−90)/6=3.333·초과5→1.5σ): e1 10 + e3(1.5×5)=7.5 = 17.5.
    지속성(e2)은 v0 보류라 가산 없음 (스펙 결정1)."""
    assert score_spc([_v("N1", 115.0)], _cfg()) == pytest.approx(17.5)


def test_score_spc_run_rule_no_persistence_bonus():
    """N2(run 룰)·밴드 내(100): e1 12 + e3 0 = 12. persistence_bonus 미사용(스펙 결정1)."""
    assert score_spc([_v("N2", 100.0)], _cfg()) == pytest.approx(12.0)


def test_score_spc_stream_internal_max_not_sum():
    """동일 스트림 N1+N3 → e1 = max(10,15) = 15 (합산 25 아님, 그룹핑 A1). 밴드 내→e3 0."""
    assert score_spc([_v("N1", 100.0), _v("N3", 100.0)], _cfg()) == pytest.approx(15.0)


def test_score_spc_cross_stream_max_no_modifier_mixing():
    """다른 센서 스트림 — 스트림별 점수의 max (modifier가 스트림 넘나들면 안 됨).
    C11: N1 초과10(σ3.333→3σ→e3 15)+e1 10 = 25. C15: N3 밴드내 = e1 15. → max(25,15)=25."""
    vs = [_v("N1", 120.0, sensor_id="C11"), _v("N3", 100.0, sensor_id="C15")]
    assert score_spc(vs, _cfg()) == pytest.approx(25.0)


# ── 스트림 간 결합 감쇠 (T14 · 2026-08-09) ──────────────────────────────────

def test_score_spc_stream_damping_zero_is_identical_to_max():
    """T14 — `stream_damping=0.0` 은 **현행 max 와 완전히 동일**하다.

    이 키를 도입해도 **거동이 안 바뀐다**는 것을 고정한다(0.0 회귀 가드).
    """
    vs = [_v("N1", 120.0, sensor_id="C11"), _v("N3", 100.0, sensor_id="C15")]
    assert score_spc(vs, _cfg(stream_damping=0.0)) == pytest.approx(25.0)   # 위 max 테스트와 같은 값


def test_score_spc_stream_damping_adds_convergence_evidence():
    """T14 — 감쇠를 켜면 **나머지 스트림이 감쇠 합으로** 실린다.

    C11: e1 10 + e3 15 = 25 (최대) · C15: e1 15 (밴드 내) → 25 + 15×0.2 = 28.0
    """
    vs = [_v("N1", 120.0, sensor_id="C11"), _v("N3", 100.0, sensor_id="C15")]
    assert score_spc(vs, _cfg(stream_damping=0.2)) == pytest.approx(28.0)


def test_score_spc_stream_damping_single_stream_unchanged():
    """T14 — 스트림이 1개면 감쇠와 무관하게 같다 (더할 '나머지'가 없다).

    단일 센서 사건에서 점수가 안 오르는 것은 **의도**다 — 수렴 증거가 없는 곳을
    올리면 그건 그냥 오탐이다(T14 §2-a).
    """
    vs = [_v("N1", 120.0, sensor_id="C11")]
    assert score_spc(vs, _cfg(stream_damping=0.0)) == score_spc(vs, _cfg(stream_damping=0.5))


def test_score_spc_sigma_guard_degenerate_band():
    """퇴화 밴드(ucl==lcl→σ=0): e3=0(ZeroDivision 없음)·e1 유지(σ-무관 룰 발행). N1 → 10."""
    assert score_spc([_v("N1", 115.0, ucl=100.0, lcl=100.0)], _cfg()) == pytest.approx(10.0)


def test_score_spc_quantile_band_monotone():
    """분위수(비대칭 q-컷) 그룹도 밴드폭 대비라 단조 — 초과 클수록 점수↑."""
    small = score_spc([_v("N3", 130.0, ucl=125.0, lcl=80.0)], _cfg())
    large = score_spc([_v("N3", 140.0, ucl=125.0, lcl=80.0)], _cfg())
    assert large > small


def test_score_spc_lower_exceedance_symmetric():
    """하한 이탈 대칭 (lcl90·current85 초과5 = 상한 115와 동일)."""
    assert score_spc([_v("N1", 85.0)], _cfg()) == pytest.approx(score_spc([_v("N1", 115.0)], _cfg()))


def test_score_spc_unknown_rule_base_zero():
    """미등록 rule_id → 기본점 0(경고)·크래시 없음. 밴드 내면 0."""
    assert score_spc([_v("N9_TYPO", 100.0)], _cfg()) == pytest.approx(0.0)


def test_score_spc_pure():
    """순수성 — 결정성 + 입력 dict 무변이(deepcopy 비교, 부작용 0)."""
    vs = [_v("N1", 115.0), _v("N3", 100.0)]
    before = copy.deepcopy(vs)
    r1 = score_spc(vs, _cfg())
    r2 = score_spc(vs, _cfg())
    assert r1 == r2                      # 결정성
    assert vs == before                  # 입력 불변(in-place 변이 없음)


def test_design_example2_b_axes_sum():
    """설계 §5 예시2 — N3 + TTTM 2.3: score_spc(N3 밴드내=15) + score_tttm(15) = B축 30 (스펙 §9)."""
    cfg = _cfg()
    assert score_spc([_v("N3", 100.0)], cfg) + score_tttm({"score": 2.3}, cfg) == pytest.approx(30.0)


# ── ContextScoreConfig.load (실 params·doc-code sync) ────────────────────────

def _params() -> dict:
    """params.yaml 원본 — 로더 매핑 검증의 **비교 대상**(리터럴 재현 금지)."""
    import yaml
    from src.common.context_score.config import CONFIG_PATH
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def test_config_load_reads_flat_params_and_reuses_keys():
    """실 params flat 키 로드 + k_sigma·tttm 임계 **기존 키 재사용** 확인(스펙 §8).

    ⚠️ 기대값을 리터럴로 못박지 않는다 — 여기서 검증할 계약은 "값이 얼마인가"가 아니라
    **"어느 키에서 왔는가"** 다. 배점·감점은 튜닝 손잡이라 컷오버될 수 있고(실측: e4·e5 를
    8/5 에 0 으로 격리), 리터럴로 두면 런타임은 멀쩡한데 CI 만 red 로 남아 다른 PR 신호까지
    더럽힌다(헌법 7장 "config 기본값 플립 시 그 값을 못박은 테스트").
    """
    p = _params()
    spc, le = p["spc"], p["limit_engine"]
    cfg = ContextScoreConfig.load()

    assert cfg.rule_base == {k: float(v) for k, v in spc["context_score_rule_base"].items()}
    assert cfg.reference_suspect_penalty == float(spc["context_score_reference_suspect_penalty"])
    assert cfg.size_per_sigma == float(spc["context_score_size_per_sigma"])
    # 기존 키 재사용 — 새 키를 만들지 않았다는 것이 이 테스트의 본체다.
    assert cfg.k_sigma == float(le["control_limit_k_sigma"])
    assert cfg.tttm_warning == float(spc["tttm_warning"])
    assert cfg.tttm_critical == float(spc["tttm_critical"])


def test_config_values_are_the_agreed_set():
    """**정책 고정(tripwire)** — 현재 합의된 배점 값. 값이 바뀌면 여기가 red 가 된다.

    ⚠️ 이 테스트가 실패하면 "테스트가 낡았다"가 아니라 **"배점이 바뀌었다"** 는 신호다.
    바꾼 PR 안에서 이 리터럴을 같이 갱신하고, 아래 이력에 한 줄 남긴다 (헌법 7장 ·
    선례 `test_publisher.py::test_config_load_from_real_params`).

    이력:
      · 2026-08-05 — e4·e5 격리: tttm_warning_pts 15→0 · tttm_critical_pts 30→0 ·
        reference_suspect_penalty 20→0 (C62 자 왜곡 — B 합의 8/4, 재분류 후 복원 예정)
      · **2026-08-11 — e6·e7 격리**: ae_band_pts (10,20,30)→(0,0,0) ·
        ae_pts_drift_lo 10→0 · ae_pts_drift_hi 20→0.
        라이브 anomaly_score 가 **98.2% 가 정확히 1.0**(서빙 번들 학습 구간 ≠ 시뮬 발행 구간)이라
        모든 알람에 최상단 밴드 30점이 상시 가산돼 게이트가 사실상 사라졌다(주입 74.6% vs
        비주입 72.6% — 구별 0). e7 은 e6 포화의 종속(EWMA)이라 같이 끈다.
        **AE 포화 해소 확인 시 되돌린다** — e4·e5 격리와 같은 회귀 조항.
      · **2026-08-11 — G8 선택 A 확정**: rule_base {N1 10→20 · N2~N8(N3 15 포함) → 8} ·
        size_per_sigma 5→2 · stream_damping 0.0→0.2. 게이트(31)는 불변.
        근거 = `analysis/step6/results/G8_배점임계_후보_v1.md` (5차원 전수 탐색 · AE=0 전제).
        시나리오 실측(주입 wafer 통과율 / 첫 탐지): SC3 6%→61% +8장→+2장 ·
        SC4 16%→93% +4장→+2장 · SC5 0%→12% +511장→+2장. 헛호출 1.4→3.0건/일.
    """
    cfg = ContextScoreConfig.load()
    assert cfg.rule_base["N1"] == 20.0 and cfg.rule_base["N3"] == 8.0
    assert cfg.size_per_sigma == 2.0
    assert cfg.stream_damping == 0.2
    assert cfg.k_sigma == 3.0
    assert cfg.tttm_warning == 2.0 and cfg.tttm_critical == 3.0
    # e4·e5 — 8/5 격리분(위 이력). 복원 시 15·30·20 으로 되돌리고 이력에 추가한다.
    assert cfg.tttm_warning_pts == 0.0
    assert cfg.tttm_critical_pts == 0.0
    assert cfg.reference_suspect_penalty == 0.0
    # e6·e7 — AE 축. **8/11 포화 격리분**(위 이력). 복원 시 (10,20,30)·10·20 으로 되돌린다.
    assert cfg.ae_band_pts == (0.0, 0.0, 0.0)
    assert cfg.ae_pts_drift_lo == 0.0 and cfg.ae_pts_drift_hi == 0.0
    # e8 — 예측 축 비활성(T21 판정 2026-08-10). 켜면 red.
    assert cfg.pred_weight is None


def test_axes_run_with_loaded_config():
    """실 params 로드 cfg 로 각 축이 **계약대로 동작**하는지 (값 고정 아님).

    정확한 산식·경계는 `_cfg()` 기반 테스트들이 덮는다. 여기서는 로드된 cfg 가 축에
    그대로 먹히는지(관통)와 **config 에서 유도되는 항등식**만 본다.
    """
    cfg = ContextScoreConfig.load()

    # 밴드 **내부** 위반 → e3=0 이므로 스트림 점수 = e1 = rule_base[rule_id] (산식 재현 아님)
    assert score_spc([_v("N1", 100.0)], cfg) == pytest.approx(cfg.rule_base["N1"])
    # 밴드 초과 → e1 위로 e3 가 더해진다(양수 단조). 크기는 배점 손잡이라 고정하지 않는다.
    assert score_spc([_v("N1", 115.0)], cfg) >= cfg.rule_base["N1"]

    tttm = score_tttm({"score": 3.5, "reference_suspect": True}, cfg)
    assert isinstance(tttm, float) and cfg.clip_min <= tttm <= cfg.clip_max


def test_config_loads_clip_bounds():
    """B0 — combine/orchestrator clip 경계를 params에서 로드(6-1, 0·100 하드코딩 금지)."""
    cfg = ContextScoreConfig.load()
    assert cfg.clip_min == 0.0 and cfg.clip_max == 100.0


def test_score_spc_non_dict_violation_skipped_no_crash():
    """B3(§4-4) — 비-dict 위반 원소(None 등)는 crash 대신 skip(total 계약)."""
    cfg = _cfg()
    assert score_spc([None], cfg) == 0.0                     # 전부 오염 → 0
    clean = score_spc([_v("N1", 115.0)], cfg)
    assert score_spc([None, _v("N1", 115.0)], cfg) == clean  # 오염 원소만 skip·정상 집계


def test_score_tttm_non_numeric_score_no_crash():
    """B3(§4-3) — score 비수치(str 등)는 0 취급(>= TypeError 방지, total 계약)."""
    cfg = _cfg()
    assert score_tttm({"score": "3.5"}, cfg) == 0.0
    assert score_tttm({"score": None}, cfg) == 0.0


# ── score_pred (e8 예측 P95 방향 가중항 · A) ─────────────────────────────────
# p95_threshold·phase 는 오케스트레이터 주입 런타임 키(계약 필드 아님, 기획서 §3).

def _cfg_pred(**over) -> ContextScoreConfig:
    """pred 축 활성 cfg (B 확정 가정치). 검증 쉬운 정수."""
    return _cfg(pred_weight=10.0, pred_p95_scale=100.0, pred_w_cap=3.0, **over)


def test_score_pred_none_and_empty_zero():
    assert score_pred(None, _cfg_pred()) == 0.0
    assert score_pred({}, _cfg_pred()) == 0.0


def test_score_pred_missing_predicted_zero():
    assert score_pred({"p95_threshold": 1500.0}, _cfg_pred()) == 0.0


def test_score_pred_weight_null_axis_disabled():
    """pred_weight=null(기본) → 축 비활성 → 0 (B 확정 전)."""
    assert score_pred({"predicted_c65": 9999.0, "p95_threshold": 1000.0}, _cfg()) == 0.0


def test_score_pred_phase_gate_0_and_1_zero():
    for ph in ("phase_0", "phase_1"):
        assert score_pred({"predicted_c65": 1600.0, "p95_threshold": 1500.0, "phase": ph},
                          _cfg_pred()) == 0.0


def test_score_pred_phase_enum_value_gated():
    class _P:  # provisional Phase enum 흉내 (.value)
        value = "phase_1"
    assert score_pred({"predicted_c65": 1600.0, "p95_threshold": 1500.0, "phase": _P()},
                      _cfg_pred()) == 0.0


def test_score_pred_normal_phase_passes_gate():
    # gap=100 → 100/100=1.0 → ×10 = 10.0
    assert score_pred({"predicted_c65": 1600.0, "p95_threshold": 1500.0, "phase": "normal"},
                      _cfg_pred()) == pytest.approx(10.0)


def test_score_pred_missing_p95_threshold_zero():
    assert score_pred({"predicted_c65": 1600.0, "phase": "normal"}, _cfg_pred()) == 0.0


def test_score_pred_below_p95_zero():
    assert score_pred({"predicted_c65": 1450.0, "p95_threshold": 1500.0}, _cfg_pred()) == 0.0


def test_score_pred_proportional_below_cap():
    # gap=60 → 0.6 → ×10 = 6.0
    assert score_pred({"predicted_c65": 1560.0, "p95_threshold": 1500.0},
                      _cfg_pred()) == pytest.approx(6.0)


def test_score_pred_saturates_at_w_cap():
    # gap=400 → 4.0 clip w_cap 3 → ×10 = 30.0
    assert score_pred({"predicted_c65": 1900.0, "p95_threshold": 1500.0},
                      _cfg_pred()) == pytest.approx(30.0)


# --- 계약 위반 입력 (0 + WARNING) — §4-③ total function (score_ae와 동일 계약) ---
@pytest.mark.parametrize("bad", ["oops", True, False, float("nan"), float("inf"),
                                 float("-inf"), [1.0], {"v": 1}, None])
def test_score_pred_contract_violation_predicted_zero(bad):
    """predicted_c65 가 비수치·bool·비유한값 → 0.0 반환(raise 금지, §4-③).

    특히 bool: `float(True)=1.0` 이 조용한 오계산을 만든다(계약 §4-③ "bool 제외 필수").
    None 은 정상 결측 경로와 결과가 같지만(0.0) 로그 수준이 다르다(아래 테스트).
    """
    assert score_pred({"predicted_c65": bad, "p95_threshold": 1500.0},
                      _cfg_pred()) == pytest.approx(0.0)


@pytest.mark.parametrize("bad", ["oops", True, False, float("nan"), float("inf"),
                                 float("-inf"), [1.0], {"v": 1}])
def test_score_pred_contract_violation_p95_zero(bad):
    """p95_threshold 가 비수치·bool·비유한값 → 0.0 (주입 seam 오류가 alert을 못 죽인다)."""
    assert score_pred({"predicted_c65": 1600.0, "p95_threshold": bad},
                      _cfg_pred()) == pytest.approx(0.0)


def test_score_pred_bool_not_silently_scored():
    """회귀 가드 — bool 이 뚫리면 P95 음수 주입 시 조용히 양수 점수가 난다(리뷰 §3-2 실증)."""
    assert score_pred({"predicted_c65": True, "p95_threshold": -0.5},
                      _cfg_pred()) == pytest.approx(0.0)


def test_score_pred_contract_violation_logs_warning(caplog):
    """계약 위반은 WARNING 으로 표면화 (조용한 통과 금지, 6-1)."""
    with caplog.at_level("WARNING"):
        score_pred({"predicted_c65": "oops", "p95_threshold": 1500.0}, _cfg_pred())
    assert any("predicted_c65" in r.getMessage() for r in caplog.records)


def test_score_pred_normal_missing_logs_nothing(caplog):
    """예측 미도달(None·키 없음)은 흔한 상황 — WARNING 로그 소음 금지 (score_ae 계약 정합)."""
    with caplog.at_level("WARNING"):
        score_pred({"predicted_c65": None, "p95_threshold": 1500.0}, _cfg_pred())
        score_pred({"p95_threshold": 1500.0}, _cfg_pred())
    assert not caplog.records


def test_score_pred_int_input_accepted():
    """int 는 정상 수치 — bool 제외가 int 까지 막으면 안 된다 (bool 은 int 의 서브클래스)."""
    assert score_pred({"predicted_c65": 1600, "p95_threshold": 1500},
                      _cfg_pred()) == pytest.approx(10.0)


# --- cfg 오설정 (기동 시점 명시적 실패) — 입력 가드와 경계가 다르다 ---
def test_score_pred_misconfig_raises():
    """pred_weight set 인데 scale/w_cap null → 명시적 에러 (6-1).

    §4-③ total function 계약은 **입력**(prediction dict)에 대한 것이고, 이 raise 는
    **설정**(cfg) 오류다. 입력 가드를 넣을 때 이 경계를 무너뜨려 설정 오류까지 0.0 으로
    삼키면 "조용한 기본값 금지"(6-1)가 깨진다 — 두 계약은 함께 성립한다.
    """
    bad = _cfg(pred_weight=10.0)   # scale/w_cap 여전히 None
    with pytest.raises(ValueError):
        score_pred({"predicted_c65": 1600.0, "p95_threshold": 1500.0}, bad)


def test_score_pred_misconfig_not_reached_when_input_invalid():
    """입력 가드가 설정 검증보다 먼저 — 비수치 입력은 오설정 cfg 여도 0.0(raise 없음).

    가드 순서 계약: 입력 정규화 → (통과 시에만) 산식·설정 검증. 순서가 뒤집히면
    비수치 입력 하나가 오설정 ValueError 로 둔갑해 wafer alert 을 죽인다.
    """
    bad = _cfg(pred_weight=10.0)   # scale/w_cap None (오설정)
    assert score_pred({"predicted_c65": "oops", "p95_threshold": 1500.0},
                      bad) == pytest.approx(0.0)


def test_score_pred_pure_no_side_effects():
    p = {"predicted_c65": 1700.0, "p95_threshold": 1500.0}
    snap = copy.deepcopy(p)
    r1 = score_pred(p, _cfg_pred())
    r2 = score_pred(p, _cfg_pred())
    assert r1 == r2 and p == snap


def test_score_pred_ignores_actual_c65_leakage():
    """누수 가드 — 실측 C65 유사 키가 있어도 무시, predicted_c65만 사용 (1-3)."""
    p = {"predicted_c65": 1450.0, "actual_c65": 9999.0, "c65": 9999.0, "p95_threshold": 1500.0}
    assert score_pred(p, _cfg_pred()) == 0.0   # predicted 1450 ≤ P95 → 0 (actual 무시)


# ── 부록B 1-2② — 위반 dict 키 결측 total-function 가드 (라이브 전 사전수정) ──
def test_score_spc_missing_limit_keys_no_crash():
    """관리선/현재값 키 결측 위반 → KeyError 없이 e3=0(기본점만). wafer 전체 alert 안 죽음."""
    v = {"rule_id": "N1", "sensor_id": "C11"}       # control_limit_*·current_value 없음
    assert score_spc([v], _cfg()) == pytest.approx(10.0)   # e1(N1)=10 + e3=0


def test_score_spc_missing_rule_id_no_crash():
    """rule_id 결측도 무크래시 — 기본점 0(미등록 취급)."""
    v = {"current_value": 100.0, "control_limit_upper": 110.0, "control_limit_lower": 90.0}
    assert score_spc([v], _cfg()) == pytest.approx(0.0)


# ── score_ae (A3 — 변경점검 v2 §4-4) ────────────────────────────────────────
# D1 확정: anomaly_score = ae_score([0,1] ECDF 캘리). z 경로 미채택(§8 실측 근거).
# 밴드 경계는 `>=` 포함(calib "0.2 = P98.5 = 상위 1.5% 이상" 정의 정합),
# drift 경계는 `>` 초과. 두 채널은 단일 ae_raw 파생이라 max 집계(합산 금지).

def _cfg_ae(**over) -> ContextScoreConfig:
    """AE 축 테스트용 cfg — params 가안값과 동일한 밴드/배점."""
    return _cfg(ae_band_edges=(0.2, 0.5, 1.0), ae_band_pts=(10.0, 20.0, 30.0),
                ae_drift_lo=0.3, ae_drift_hi=0.7,
                ae_pts_drift_lo=10.0, ae_pts_drift_hi=20.0, ae_aggregate="max", **over)


def _p(anomaly=None, drift=None, **extra):
    """prediction 최소본 (계약명 키 — collector _normalize_prediction 정합)."""
    return {"anomaly_score": anomaly, "drift_score": drift, **extra}


# --- 결측 (★③) ---
def test_score_ae_none_zero():
    assert score_ae(None, _cfg_ae()) == 0.0


def test_score_ae_empty_dict_zero():
    assert score_ae({}, _cfg_ae()) == 0.0


def test_score_ae_both_channels_missing_zero():
    """키는 있으나 None — 정상 결측이라 조용히 0 (WARNING 아님)."""
    assert score_ae(_p(), _cfg_ae()) == 0.0


def test_score_ae_missing_keys_no_crash():
    """계약 키 자체가 없어도 KeyError 없이 0 — wafer 전체 alert 안 죽음."""
    assert score_ae({"predicted_c65": 1500.0}, _cfg_ae()) == 0.0


# --- e6 anomaly 밴드 경계 (`>=`) ---
def test_score_ae_anomaly_below_first_band():
    assert score_ae(_p(anomaly=0.19), _cfg_ae()) == pytest.approx(0.0)


def test_score_ae_anomaly_band_edge_inclusive():
    """경계값 0.2는 발화 — calib 정의(P98.5 '이상')와 방향 일치."""
    assert score_ae(_p(anomaly=0.2), _cfg_ae()) == pytest.approx(10.0)


def test_score_ae_anomaly_mid_band():
    assert score_ae(_p(anomaly=0.5), _cfg_ae()) == pytest.approx(20.0)


def test_score_ae_anomaly_top_band_clip():
    """ae_score는 P99.9에서 1.0 클립 — 그 이상 심각도는 같은 밴드(설계상 수용, §3-b4)."""
    assert score_ae(_p(anomaly=1.0), _cfg_ae()) == pytest.approx(30.0)


def test_score_ae_anomaly_zero_no_fire():
    assert score_ae(_p(anomaly=0.0), _cfg_ae()) == pytest.approx(0.0)


# --- e7 drift 밴드 경계 (`>`) ---
def test_score_ae_drift_at_lo_edge_not_fire():
    """drift는 초과(`>`) 기준 — 경계값 0.3은 미발화."""
    assert score_ae(_p(drift=0.3), _cfg_ae()) == pytest.approx(0.0)


def test_score_ae_drift_lo_band():
    assert score_ae(_p(drift=0.31), _cfg_ae()) == pytest.approx(10.0)


def test_score_ae_drift_at_hi_edge_stays_lo():
    assert score_ae(_p(drift=0.7), _cfg_ae()) == pytest.approx(10.0)


def test_score_ae_drift_hi_band():
    assert score_ae(_p(drift=0.71), _cfg_ae()) == pytest.approx(20.0)


def test_score_ae_drift_saturated():
    """실측상 drift는 한 번 1.0에 닿으면 고착(11,006장) — 상단 밴드 유지."""
    assert score_ae(_p(drift=1.0), _cfg_ae()) == pytest.approx(20.0)


# --- A3 집계: max (합산 금지) ---
def test_score_ae_aggregate_is_max_not_sum():
    """동시 발화 → max(30, 20) = 30. 합산(50)이면 이중계상 — 단일 ae_raw 파생이라 금지."""
    assert score_ae(_p(anomaly=1.0, drift=0.9), _cfg_ae()) == pytest.approx(30.0)


def test_score_ae_aggregate_drift_dominant():
    """drift만 큰 경우(느린 드리프트 — 실측 1,149장) drift가 축을 대표."""
    assert score_ae(_p(anomaly=0.1, drift=0.9), _cfg_ae()) == pytest.approx(20.0)


def test_score_ae_aggregate_anomaly_dominant():
    """순간 스파이크(실측 81장: C64_5367 = score 1.0인데 drift 0.0)."""
    assert score_ae(_p(anomaly=1.0, drift=0.0), _cfg_ae()) == pytest.approx(30.0)


# --- 계약 위반 입력 (0 + WARNING) ---
@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan"), float("inf"), "oops", True])
def test_score_ae_contract_violation_zero(bad):
    """범위 밖·NaN·inf·비수치·bool → 채널 0. NaN이 비교를 전부 False로 만들어 침묵 통과하는 것 차단."""
    assert score_ae(_p(anomaly=bad), _cfg_ae()) == pytest.approx(0.0)
    assert score_ae(_p(drift=bad), _cfg_ae()) == pytest.approx(0.0)


def test_score_ae_contract_violation_logs_warning(caplog):
    """정상 결측과 달리 계약 위반은 WARNING으로 표면화 (조용한 통과 금지, 6-1)."""
    with caplog.at_level("WARNING"):
        score_ae(_p(anomaly=float("nan")), _cfg_ae())
    assert any("anomaly_score" in r.getMessage() for r in caplog.records)


def test_score_ae_normal_missing_logs_nothing(caplog):
    """예측 미도달(None)은 흔한 상황 — 로그 소음 금지."""
    with caplog.at_level("WARNING"):
        score_ae(_p(), _cfg_ae())
    assert not caplog.records


def test_score_ae_one_channel_violation_other_still_scores():
    """한 채널이 오염돼도 다른 채널은 정상 채점 — 전체 침묵 방지."""
    assert score_ae(_p(anomaly=float("nan"), drift=0.9), _cfg_ae()) == pytest.approx(20.0)


# --- 순수성·누수 ---
def test_score_ae_pure_no_side_effects():
    p = _p(anomaly=0.6, drift=0.5)
    snap = copy.deepcopy(p)
    r1 = score_ae(p, _cfg_ae())
    r2 = score_ae(p, _cfg_ae())
    assert r1 == r2 and p == snap


def test_score_ae_ignores_c65_leakage():
    """누수 가드(1-3) — 예측·실측 C65가 있어도 AE 축은 두 채널만 본다."""
    p = _p(anomaly=0.1, drift=0.1, predicted_c65=9999.0, actual_c65=9999.0)
    assert score_ae(p, _cfg_ae()) == pytest.approx(0.0)


# --- cfg 오설정 (기동 시점 명시적 실패) ---
def test_cfg_ae_band_length_mismatch_raises():
    with pytest.raises(ValueError, match="길이"):
        _cfg(ae_band_edges=(0.2, 0.5), ae_band_pts=(10.0,))


def test_cfg_ae_band_edges_not_sorted_raises():
    with pytest.raises(ValueError, match="오름차순"):
        _cfg(ae_band_edges=(0.5, 0.2), ae_band_pts=(10.0, 20.0))


def test_cfg_ae_drift_bounds_inverted_raises():
    with pytest.raises(ValueError, match="ae_drift_lo"):
        _cfg(ae_drift_lo=0.9, ae_drift_hi=0.3)


@pytest.mark.parametrize("bad", ["complementary", "sum", "mean", "MAX", ""])
def test_cfg_ae_aggregate_unsupported_raises(bad):
    """미지원 집계값 → 기동 시 명시적 실패 (코드리뷰 §3-5 — 조용히 max 로 도는 것 차단)."""
    with pytest.raises(ValueError, match="ae_aggregate"):
        _cfg(ae_aggregate=bad)


def test_cfg_ae_aggregate_max_accepted():
    """v0 지원값 max 는 통과 — 키는 grouped_2stage 확장 지점으로 남긴다."""
    assert _cfg(ae_aggregate="max").ae_aggregate == "max"
