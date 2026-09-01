"""qual_engine 유닛테스트 (B5-2 — Qual 2지표 σ-갭 + Nelson N1).

Task 1: QualConfig 로드 + evaluate_qual σ-갭 판정 (순수, 스냅샷 주입).
스펙: specs/B5-2_qual지표_스펙플랜.md §10 테스트 계획.
"""
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from src.agent_b_spc import qual_engine as qe

K = 3.0


def _write_cfg(tmp_path: Path, qual: dict | None = None) -> Path:
    """hermetic params.yaml (limit_engine + spc + qual 절)."""
    cfg = {
        "limit_engine": {"control_limit_k_sigma": 3.0, "rolling_window_n": 500,
                         "seasoning_exclude_wafers": 10, "resolution_deadband_k": 0.0},
        "spc": {"trim_quantile": 0.01, "false_alarm_warn_pct": 2.0, "cv_warn": 0.5,
                "q_low": 0.00135, "q_high": 0.99865, "min_trim_n": 100},
        "qual": {"pass_sigma_gap_max": 3.0, "pass_nelson_violations_max": 0,
                 **(qual or {})},
    }
    p = tmp_path / "params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _cfg(tmp_path):
    return qe.QualConfig.load(_write_cfg(tmp_path))


def _sigma_snap(center=0.0, sigma=1.0):
    return {"center": center, "sigma": sigma, "ucl": center + K * sigma,
            "lcl": center - K * sigma, "method": "sigma"}


def _quantile_snap(center=0.0, q_lcl=-3.0, q_ucl=3.0):
    """분위수 스냅샷: center=median 기준, ucl/lcl=q밴드 (sigma_ref_of가 robust-σ 역산)."""
    return {"center": center, "sigma": 1.0, "ucl": q_ucl, "lcl": q_lcl, "method": "quantile"}


GK1 = ("SIM_CH_1", "C6_0", 4, "settled", "C11")
GK2 = ("SIM_CH_1", "C6_0", 4, "settled", "C17")


# ── QualConfig ──────────────────────────────────────────────────────

def test_qualconfig_composes_limitconfig(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.limit.k_sigma == 3.0        # 합성 LimitConfig (M1)
    assert cfg.threshold == 3.0            # qual.pass_sigma_gap_max
    assert cfg.nelson_max == 0             # qual.pass_nelson_violations_max


def test_qualconfig_missing_key_raises(tmp_path):
    p = _write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    del data["qual"]["pass_sigma_gap_max"]
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(KeyError, match="pass_sigma_gap_max"):
        qe.QualConfig.load(p)


# ── σ-갭 판정 ────────────────────────────────────────────────────────

def _eval(tmp_path, qual_indicators, snapshot):
    return qe.evaluate_qual(qual_indicators, snapshot, _cfg(tmp_path),
                            chamber_id="SIM_CH_1", qual_id="Q1", snapshot_id="S1")


def test_sigma_gap_fail(tmp_path):
    """한 그룹 3.2σ 이동 → FAIL, top_gap_sensor 정확, max_gap≈3.2."""
    snap = {GK1: _sigma_snap(), GK2: _sigma_snap()}
    qi = {GK1: [0.1] * 5, GK2: [3.2] * 5}          # GK2(C17) 3.2σ
    r = _eval(tmp_path, qi, snap)
    assert r.sigma_gap["verdict"] == "FAIL"
    assert r.sigma_gap["top_gap_sensor"] == "C17"
    assert r.sigma_gap["max_gap_sigma"] == pytest.approx(3.2)
    assert r.sigma_gap["top_gap_group"] == list(GK2)


def test_sigma_gap_pass(tmp_path):
    """전 그룹 <3σ → PASS."""
    snap = {GK1: _sigma_snap(), GK2: _sigma_snap()}
    qi = {GK1: [1.0] * 5, GK2: [2.0] * 5}
    r = _eval(tmp_path, qi, snap)
    assert r.sigma_gap["verdict"] == "PASS"
    assert r.sigma_gap["max_gap_sigma"] == pytest.approx(2.0)


def test_sigma_gap_boundary_exactly_3_is_fail(tmp_path):
    """경계값 정확히 3.0σ → FAIL (≥ 포함)."""
    snap = {GK1: _sigma_snap()}
    r = _eval(tmp_path, {GK1: [3.0] * 5}, snap)
    assert r.sigma_gap["verdict"] == "FAIL"


def test_aggregation_mean_for_sigma_median_for_quantile(tmp_path):
    """A1′: 같은 치우친 5장에 sigma 그룹=mean·quantile 그룹=median (값 차)."""
    skewed = [0.0, 0.0, 0.0, 0.0, 10.0]            # mean=2.0, median=0.0
    snap = {GK1: _sigma_snap(sigma=1.0), GK2: _quantile_snap(q_lcl=-6.0, q_ucl=6.0)}
    r = _eval(tmp_path, {GK1: skewed, GK2: skewed}, snap)
    pg = {tuple(g["group"]): g for g in r.sigma_gap["per_group"]}
    # sigma 그룹: mean=2.0, sref=1.0 → gap 2.0
    assert pg[GK1]["gap_sigma"] == pytest.approx(2.0)
    # quantile 그룹: median=0.0, center=0.0 → gap 0.0 (mean이었으면 2.0/robustσ)
    assert pg[GK2]["gap_sigma"] == pytest.approx(0.0)


def test_m2_median_center_no_floor_bias(tmp_path):
    """M2: 분위수 정상 챔버(median≈center) → gap≈0 (mean-center의 바닥편향 소멸)."""
    snap = {GK1: _quantile_snap(center=100.0, q_lcl=94.0, q_ucl=106.0)}
    # 정상 5장(중앙값 100) — skew 있어도 median=center
    r = _eval(tmp_path, {GK1: [99.0, 100.0, 100.0, 100.0, 105.0]}, snap)
    assert r.sigma_gap["per_group"][0]["gap_sigma"] == pytest.approx(0.0, abs=1e-9)


def test_robust_sigma_quantile_formula(tmp_path):
    """M3: quantile gap = |median−center| / ((q_ucl−q_lcl)/2k)."""
    snap = {GK1: _quantile_snap(center=0.0, q_lcl=-6.0, q_ucl=6.0)}   # robust-σ=(12)/(6)=2
    r = _eval(tmp_path, {GK1: [4.0] * 5}, snap)                        # median=4, gap=4/2=2
    assert r.sigma_gap["per_group"][0]["gap_sigma"] == pytest.approx(2.0)


def test_c_slim_skip_empty_group(tmp_path):
    """C-슬림: 한 그룹 0장 → per_group skipped, max 산정서 제외."""
    snap = {GK1: _sigma_snap(), GK2: _sigma_snap()}
    r = _eval(tmp_path, {GK1: [], GK2: [2.0] * 5}, snap)
    pg = {tuple(g["group"]): g for g in r.sigma_gap["per_group"]}
    assert pg[GK1]["skipped"] == "no_data"
    assert pg[GK1]["gap_sigma"] is None
    assert r.sigma_gap["max_gap_sigma"] == pytest.approx(2.0)          # GK2만


def test_all_skipped_is_unknown(tmp_path):
    """전 그룹 0장 → σ-갭 UNKNOWN (거짓 PASS 금지)."""
    snap = {GK1: _sigma_snap(), GK2: _sigma_snap()}
    r = _eval(tmp_path, {GK1: [], GK2: []}, snap)
    assert r.sigma_gap["verdict"] == "UNKNOWN"
    assert r.sigma_gap["max_gap_sigma"] is None
    assert r.sigma_gap["top_gap_sensor"] is None


def test_degenerate_band_skipped(tmp_path):
    """붕괴 밴드(ucl≈lcl → sref≤EPS) → skip('degenerate'), 분모 폭발 차단."""
    snap = {GK1: _quantile_snap(center=1.0, q_lcl=1.0, q_ucl=1.0)}     # robust-σ=0
    r = _eval(tmp_path, {GK1: [5.0] * 5}, snap)
    assert r.sigma_gap["per_group"][0]["skipped"] == "degenerate"
    assert r.sigma_gap["verdict"] == "UNKNOWN"                         # 유효 0


def test_per_group_all_present_and_top_matches(tmp_path):
    """per_group[]에 전 그룹 담김 + top_gap_sensor가 per_group max와 일치."""
    snap = {GK1: _sigma_snap(), GK2: _sigma_snap()}
    r = _eval(tmp_path, {GK1: [1.0] * 5, GK2: [2.5] * 5}, snap)
    assert len(r.sigma_gap["per_group"]) == 2
    top = max((g for g in r.sigma_gap["per_group"] if g["gap_sigma"] is not None),
              key=lambda g: g["gap_sigma"])
    assert r.sigma_gap["top_gap_sensor"] == tuple(top["group"])[-1]


def test_return_contract_serializable_no_final_verdict(tmp_path):
    """반환 계약: asdict 직렬화 · 최종 verdict 필드 없음 (2지표만)."""
    snap = {GK1: _sigma_snap()}
    r = _eval(tmp_path, {GK1: [1.0] * 5}, snap)
    d = asdict(r)
    assert set(d) == {"chamber_id", "qual_id", "snapshot_id", "evaluated_at",
                      "sigma_gap", "nelson"}
    assert "verdict" not in d                          # 최종 종합은 A5-3
    assert d["chamber_id"] == "SIM_CH_1"


# ── Nelson N1 (Task 2) ───────────────────────────────────────────────

def test_nelson_n1_violation_fails(tmp_path):
    """H1: 한 점이 스냅샷 관리선 밖 → violation_count≥1, FAIL (rule_n1 list 계약)."""
    snap = {GK1: _sigma_snap(center=0.0, sigma=1.0)}       # s3=±3
    r = _eval(tmp_path, {GK1: [0.0, 0.0, 0.0, 0.0, 4.0]}, snap)   # 4.0 > 3
    assert r.nelson["violation_count"] >= 1
    assert r.nelson["verdict"] == "FAIL"
    assert r.nelson["violations"][0]["rule"] == "N1"
    assert r.nelson["violations"][0]["value"] == 4.0
    assert r.nelson["violations"][0]["group"] == list(GK1)


def test_nelson_n1_all_inside_passes(tmp_path):
    """전 점 관리선 안 → 0 위반, PASS."""
    snap = {GK1: _sigma_snap(center=0.0, sigma=1.0)}
    r = _eval(tmp_path, {GK1: [0.5, -0.5, 1.0, -1.0, 2.0]}, snap)
    assert r.nelson["violation_count"] == 0
    assert r.nelson["verdict"] == "PASS"


def test_nelson_complements_sigma_gap(tmp_path):
    """상호보완: σ-갭은 평균 안쪽이라 PASS이나 N1이 단일 이상치를 잡는다."""
    snap = {GK1: _sigma_snap(center=0.0, sigma=1.0)}
    r = _eval(tmp_path, {GK1: [0.0, 0.0, 0.0, 0.0, 4.0]}, snap)   # mean=0.8(<3σ) / 점 4.0(>3σ)
    assert r.sigma_gap["verdict"] == "PASS"
    assert r.nelson["verdict"] == "FAIL"


def test_nelson_n1_quantile_boundary(tmp_path):
    """분위수 그룹은 from_quantile 경계(q밴드)로 N1 판정."""
    snap = {GK1: _quantile_snap(center=0.0, q_lcl=-6.0, q_ucl=6.0)}
    r = _eval(tmp_path, {GK1: [0.0, 0.0, 0.0, 0.0, 7.0]}, snap)   # 7 > q_ucl 6
    assert r.nelson["violation_count"] >= 1
    assert r.nelson["verdict"] == "FAIL"


def test_nelson_degenerate_band_skipped_no_storm(tmp_path):
    """H2: 붕괴 밴드 그룹은 σ-갭·Nelson 둘 다 skip (N1 폭풍 없음)."""
    snap = {GK1: _quantile_snap(center=1.0, q_lcl=1.0, q_ucl=1.0)}   # robust-σ=0 붕괴
    r = _eval(tmp_path, {GK1: [5.0] * 5}, snap)          # 밖으로 튀지만 skip
    assert r.nelson["violation_count"] == 0
    assert r.nelson["verdict"] == "UNKNOWN"
    assert r.sigma_gap["verdict"] == "UNKNOWN"           # 대칭


def test_nelson_unknown_when_no_evaluable(tmp_path):
    """M4: 유효 평가 그룹 0건 → nelson UNKNOWN (거짓 PASS 아님, σ-갭과 대칭)."""
    snap = {GK1: _sigma_snap(), GK2: _sigma_snap()}
    r = _eval(tmp_path, {GK1: [], GK2: []}, snap)
    assert r.nelson["verdict"] == "UNKNOWN"
    assert r.nelson["violation_count"] == 0
