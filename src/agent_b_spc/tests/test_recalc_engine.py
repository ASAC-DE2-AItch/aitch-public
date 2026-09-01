"""recalc_engine 유닛테스트 (B4-3 실력치 재산정 엔진).

Task 0: RecalcConfig 로드 (LimitConfig 합성 + 신규 2키 + q↔k 가드).
Task 2: recompute_group 코어 (3구간 판정·telescoping·가드 순서).
"""
import math
from pathlib import Path

import numpy as np
import pytest
import yaml

from src.agent_b_spc import initial_limits as il
from src.agent_b_spc import recalc_engine as re


def _write_cfg(tmp_path: Path, spc: dict | None = None, limit_engine: dict | None = None) -> Path:
    """테스트용 최소 params.yaml 작성 (hermetic) — limit_engine + spc 절."""
    cfg = {
        "limit_engine": {
            "control_limit_k_sigma": 3.0,
            "rolling_window_n": 500,
            "seasoning_exclude_wafers": 10,
            "reset_delta_max_sigma": 0.5,
            "reset_cum_max_sigma_per_cycle": 1.0,
            "recalc_interval_wafers": 500,
            "recalc_deadband_k_se": 2,
            "resolution_deadband_k": 0.0,
            **(limit_engine or {}),
        },
        "spc": {
            "trim_quantile": 0.01,
            "false_alarm_warn_pct": 2.0,
            "cv_warn": 0.5,
            "q_low": 0.00135,
            "q_high": 0.99865,
            "min_trim_n": 100,
            "min_group_n": 30,
            **(spc or {}),
        },
    }
    p = tmp_path / "params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_recalcconfig_composes_limitconfig(tmp_path):
    """RecalcConfig는 LimitConfig를 합성으로 품는다 (M1 — 산식 파라미터 단일 소스)."""
    cfg = re.RecalcConfig.load(_write_cfg(tmp_path))
    # 합성된 LimitConfig 접근 — 속성명(k_sigma)이지 config 키명(control_limit_k_sigma)이 아님
    assert cfg.limit.k_sigma == 3.0
    assert cfg.limit.q_high == 0.99865


def test_recalcconfig_reads_reset_caps(tmp_path):
    """A2/A3 σ 상한을 limit_engine 절에서 로드."""
    cfg = re.RecalcConfig.load(_write_cfg(tmp_path))
    assert cfg.reset_delta_max_sigma == 0.5
    assert cfg.reset_cum_max_sigma_per_cycle == 1.0


def test_recalcconfig_reads_deadband_k_se(tmp_path):
    """신규 키 recalc_deadband_k_se → deadband_k_se (dead_band = k×SE)."""
    cfg = re.RecalcConfig.load(_write_cfg(tmp_path))
    assert cfg.deadband_k_se == 2


def test_recalcconfig_reads_min_group_n(tmp_path):
    """소표본 가드 임계 (spc 절, 재산정 소표본 가드로 용도 재활용)."""
    cfg = re.RecalcConfig.load(_write_cfg(tmp_path))
    assert cfg.min_group_n == 30


def test_recalcconfig_missing_deadband_key_names_it(tmp_path):
    """신규 키 부재 시 조용한 기본값 없이 명시적 실패 (헌법 6-1, H-C)."""
    p = _write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    del data["limit_engine"]["recalc_deadband_k_se"]
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(KeyError, match="recalc_deadband_k_se"):
        re.RecalcConfig.load(p)


def test_recalcconfig_q_k_mismatch_raises(tmp_path):
    """q_high의 Φ⁻¹ ≠ k_sigma면 로드 시 즉시 실패 (σ_ref 역산 정합 #12)."""
    # q_high를 0.975(≈1.96σ)로 변조 — k_sigma=3.0과 어긋남
    p = _write_cfg(tmp_path, spc={"q_high": 0.975})
    with pytest.raises(ValueError, match="q_high"):
        re.RecalcConfig.load(p)


# ── Task 2: recompute_group 코어 (3구간 판정) ──────────────────────────────────
#
# 자(尺): dead_band = deadband_k_se/√n = 2/√500 ≈ 0.0894σ, cap_1 = 0.5σ, cap_cum = 1.0σ.
# 경계 시맨틱: dead_band는 `<`(== → 변화로 봄), cap은 `>`(== → 이내=자동) — 결정문서 4-a·헌법 1-1 "초과".

K = 3.0


def _cfg(tmp_path):
    return re.RecalcConfig.load(_write_cfg(tmp_path))


def _sigma_current(center, sigma, version="v1"):
    """sigma-method 현행 관리선: ucl/lcl = center ± kσ."""
    return {"center": center, "sigma": sigma, "ucl": center + K * sigma,
            "lcl": center - K * sigma, "method": "sigma", "limit_version": version}


def _quantile_current(center, ucl, lcl, version="v1"):
    """quantile-method(KEEP*) 현행: sigma_ref = (ucl−lcl)/2k. sigma 컬럼은 참고 통계(무시)."""
    return {"center": center, "sigma": 1.0, "ucl": ucl, "lcl": lcl,
            "method": "quantile", "limit_version": version}


def _reset(center, sigma_ref):
    return {"center": center, "sigma_ref": sigma_ref}


def _vals(target, n=500):
    """상수 표본 → 트림 평균 = target(new.center 정밀 제어). new.sigma=0은 판정에 무영향."""
    return [float(target)] * n


GK = ("SIM_CH_1", "C6_0", 4, "settled", "C11")


def test_no_change_dead_band(tmp_path):
    """delta1 < dead_band → 변화없음(관리선 미변경)."""
    cfg = _cfg(tmp_path)
    # delta1 = 0.05σ < 0.0894
    p = re.recompute_group(GK, _vals(100.5), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "no_change"
    assert p.reason == "dead_band"
    assert p.new_center is None


def test_auto_applied(tmp_path):
    """dead_band < delta1 ≤ 0.5σ, 누적 여유 → 자동적용."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(103.0), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)      # delta1=0.3σ, cum=0.3σ
    assert p.decision == "auto_applied"
    assert p.status == "AUTO_APPLIED"
    assert p.trigger_type == "periodic"
    assert p.new_center == 103.0
    assert p.new_sigma == 0.0                                  # 상수 표본
    assert p.delta_sigma == pytest.approx(0.3)
    assert p.n_used == 500
    assert p.group_key == GK


def test_needs_approval_single_exceed(tmp_path):
    """delta1 > 0.5σ → 승인대기(1회 상한 초과)."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(106.0), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)      # delta1=0.6σ
    assert p.decision == "needs_approval"
    assert p.status == "PROPOSED"
    assert p.new_center == 106.0                               # 제안값 채움(record_proposed용)


def test_needs_approval_cumulative_exceed(tmp_path):
    """delta1 ≤ 0.5σ이나 누적 telescoping > 1.0σ → 승인대기."""
    cfg = _cfg(tmp_path)
    # current는 이미 리셋 대비 드리프트(113), 1회 이동은 작지만(0.1σ) 누적은 1.4σ
    p = re.recompute_group(GK, _vals(114.0), _sigma_current(113.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "needs_approval"
    assert p.status == "PROPOSED"


def test_cumulative_signed_roundtrip_stays_auto(tmp_path):
    """H2 signed 순이동: 리셋 대비 왕복 → cum≈0 → 자동 (오승인 방지)."""
    cfg = _cfg(tmp_path)
    # current가 +0.4σ 드리프트(104)한 상태에서 리셋값(100)으로 복귀
    p = re.recompute_group(GK, _vals(100.0), _sigma_current(104.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)      # delta1=0.4σ, cum=0σ
    assert p.decision == "auto_applied"


def test_quantile_group_uses_q_bounds(tmp_path):
    """분위수 그룹: sigma_ref=(ucl−lcl)/2k, center=mean, UCL/LCL=새 q_lcl/q_ucl."""
    cfg = _cfg(tmp_path)
    cur = _quantile_current(center=100.0, ucl=130.0, lcl=70.0)   # sigma_ref=(130−70)/6=10
    # 트림되는 극단치 → q 경계는 넓고 mean±kσ는 좁음(분리 확인). center=103 이동(delta1=0.3→auto)
    vals = [103.0] * 498 + [203.0, 3.0]
    p = re.recompute_group(GK, vals, cur, _reset(100.0, 10.0), 0.0, cfg)
    ref = il.compute_group_limits(np.array(vals), cfg.limit)
    assert p.decision == "auto_applied"
    assert p.method == "quantile"
    assert p.new_center == pytest.approx(ref["center"])
    assert p.new_ucl == pytest.approx(ref["q_ucl"])            # q 경계 (mean+kσ 아님)
    assert p.new_lcl == pytest.approx(ref["q_lcl"])
    assert ref["q_ucl"] != pytest.approx(ref["ucl"])          # q 경계 ≠ mean+kσ (경로 분리 실증)


def test_guard_degenerate_current_sigma(tmp_path):
    """H-A: 현행 sigma_ref ≤ EPS → 나눗셈 前 degenerate 스킵(크래시 없음)."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(103.0), _sigma_current(100.0, 0.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "no_change"
    assert p.reason == "degenerate"


def test_guard_reset_sigma_degenerate(tmp_path):
    """H-A: 리셋 σ ≤ EPS → 누적 나눗셈 前 보수적 승인(크래시 없음)."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(103.0), _sigma_current(100.0, 10.0),
                           _reset(100.0, 0.0), 0.0, cfg)
    assert p.decision == "needs_approval"
    assert p.status == "PROPOSED"


def test_reset_ref_none_needs_approval(tmp_path):
    """M7: 리셋 기준점 부재(None) → 보수적 승인 (None.sigma_ref 방지)."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(103.0), _sigma_current(100.0, 10.0),
                           None, 0.0, cfg)
    assert p.decision == "needs_approval"
    assert p.new_center == 103.0                               # 제안값은 채움


def test_small_sample_no_change(tmp_path):
    """n_used < min_group_n(30) → 변화없음(소표본 가드)."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(103.0, n=20), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "no_change"
    assert p.reason == "small_sample"


def test_nan_filtered_before_judgment(tmp_path):
    """L11: NaN/None은 판정 최상단에서 필터 → n_used 재계산."""
    cfg = _cfg(tmp_path)
    vals = _vals(103.0) + [float("nan")] * 10 + [None] * 5
    p = re.recompute_group(GK, vals, _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "auto_applied"
    assert p.n_used == 500                                     # NaN/None 제외


def test_resolution_none_no_crash(tmp_path):
    """resolution None → res_σ=0 항 스킵, 크래시 없음(타입 방어)."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(103.0), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), None, cfg)
    assert p.decision == "auto_applied"


def test_resolution_bound_excludes_auto(tmp_path):
    """H5: res_σ ≥ 0.5σ → 자동 구간 소멸 → no_change(resolution_bound)."""
    cfg = _cfg(tmp_path)
    # resolution=5, sigma_ref=10 → res_σ=0.5 ≥ reset_delta_max_sigma(0.5)
    p = re.recompute_group(GK, _vals(103.0), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 5.0, cfg)
    assert p.decision == "no_change"
    assert p.reason == "resolution_bound"


def test_boundary_cap1_equal_is_auto(tmp_path):
    """L-E 정정: delta1 == cap_1(0.5σ) → 자동 (결정문서 '이내'=자동, `>` 시맨틱)."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(105.0), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)      # delta1 = 5/10 = 0.5 정확
    assert p.decision == "auto_applied"


def test_boundary_cap1_just_over_is_approval(tmp_path):
    """delta1 = 0.51σ(> 0.5) → 승인."""
    cfg = _cfg(tmp_path)
    p = re.recompute_group(GK, _vals(105.1), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "needs_approval"


def test_boundary_cap_cum_equal_is_auto(tmp_path):
    """cum == cap_cum(1.0σ)이고 1회는 자동 구간 → 자동 ('이내')."""
    cfg = _cfg(tmp_path)
    # reset 100, new 110 → cum=1.0. current 107 → delta1=0.3(자동 구간)
    p = re.recompute_group(GK, _vals(110.0), _sigma_current(107.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "auto_applied"


def test_m8_sigma_widening_rides_along_auto(tmp_path):
    """M8 의도 고정: center가 자동 구간 이동 + σ 2배 확대 → σ 게이트 없이 자동(사각지대).

    나중에 조용히 '승인'으로 바뀌지 않게 현 설계 의도를 테스트로 못박는다.
    """
    cfg = _cfg(tmp_path)
    # center 103(delta1=0.3, 자동) + 산포 2배(std≈20 vs current σ=10)
    vals = [83.0] * 250 + [123.0] * 250      # mean=103, std≈20
    p = re.recompute_group(GK, vals, _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 0.0, cfg)
    assert p.decision == "auto_applied"
    assert p.new_sigma > 15                  # 확대된 σ가 게이트 없이 그대로 반영됨


def test_dead_band_uses_resolution_floor(tmp_path):
    """dead_band = max(2·SE, res_σ) — res_σ가 크면 dead_band 하한이 됨."""
    cfg = _cfg(tmp_path)
    # resolution=3, sigma_ref=10 → res_σ=0.3. delta1=0.2σ < 0.3(res_σ) → no_change
    #   (res_σ 0.3 < 0.5라 resolution_bound 아님, dead_band 하한만 상승)
    p = re.recompute_group(GK, _vals(102.0), _sigma_current(100.0, 10.0),
                           _reset(100.0, 10.0), 3.0, cfg)
    assert p.decision == "no_change"
    assert p.reason == "dead_band"
