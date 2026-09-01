"""initial_limits 유닛테스트 (config 로드·주입·2-track·게이트)."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.agent_b_spc import initial_limits as il


def _write_cfg(tmp_path: Path, spc: dict | None = None, limit_engine: dict | None = None) -> Path:
    """테스트용 최소 params.yaml 작성 (hermetic)."""
    cfg = {
        "limit_engine": {
            "control_limit_k_sigma": 3.0,
            "rolling_window_n": 500,
            "seasoning_exclude_wafers": 10,
            "resolution_deadband_k": 0.0,      # 기본 테스트는 dead-band 비활성(기존 산식 회귀 보존)
            **(limit_engine or {}),
        },
        "spc": {
            "trim_quantile": 0.01,
            "false_alarm_warn_pct": 2.0,
            "cv_warn": 0.5,
            "q_low": 0.00135,
            "q_high": 0.99865,
            "min_trim_n": 100,
            **(spc or {}),
        },
    }
    p = tmp_path / "params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_limitconfig_load_reads_both_sections(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    assert cfg.k_sigma == 3.0
    assert cfg.rolling_n == 500
    assert cfg.trim_quantile == 0.01
    assert cfg.false_alarm_warn_pct == 2.0
    assert cfg.cv_warn == 0.5


def test_limitconfig_loads_quantile_levels(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    assert cfg.q_low == 0.00135
    assert cfg.q_high == 0.99865


def test_limitconfig_missing_key_names_the_key(tmp_path):
    p = _write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    del data["spc"]["cv_warn"]
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(KeyError, match="cv_warn"):
        il.LimitConfig.load(p)


def _synthetic_reset_df():
    """PM 리셋 후 C6_0(전)→C6_1(실험)→C6_0(복귀) 3블록 합성."""
    rows = []
    wid = 0
    def block(recipe, n, t0, c33):
        nonlocal wid
        for i in range(n):
            wid += 1
            # 각 wafer 1 step(4)·settled 1점 — 값은 recipe로 구분
            rows.append(dict(C64=f"W{wid:04d}", C6=recipe, C7=4, C42=0, C24="C24_0",
                             C10=t0 + wid, C33=c33, C11=-220.0, C12=-5.0, C59=50.0, C60=60.0,
                             C15=1.0, C16=1.0, C17=50.0, C9=50.0, C31=100.0, C32=1.0,
                             C57=1.0, C58=1.0, C61=-10.0, C62=100.0, C63=5.0,
                             C18=1.0, C27=1.0, C54=1.0, C56=1.0, C20=0, C6_dummy=0))
    block("C6_0", 20, 1_000, c33=5)     # 실험 前 (리셋 직후 → c33 상승 지점)
    block("C6_1", 5, 2_000, c33=5)      # 실험
    block("C6_0", 600, 3_000, c33=5)    # 복귀
    df = pd.DataFrame(rows)
    # 리셋 지점 생성: 첫 블록 앞에 c33 큰 wafer 하나
    head = df.iloc[:1].copy(); head["C64"] = "W0000"; head["C33"] = 99; head["C10"] = 0
    return pd.concat([head, df], ignore_index=True)


def test_sample_is_return_block_only(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    df = _synthetic_reset_df()
    sample, meta = il.determine_sample_wafers(df, cfg)
    sample_recipes = df[df.C64.isin(sample)].groupby("C64")["C6"].first()
    assert (sample_recipes == "C6_0").all()                 # C6_1 없음
    # 복귀 블록만 (실험 前 20장 제외)
    assert meta["excluded_experimental"]["C6_1"]["n"] == 5
    assert meta["excluded_experimental"]["C6_0_pre_experiment"]["n"] == 20


def test_meta_returnblock_arithmetic_consistent(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    _sample, meta = il.determine_sample_wafers(_synthetic_reset_df(), cfg)
    assert meta["return_block_wafers"] - meta["seasoning_excluded"] == meta["sample_wafers"]


def test_compute_limits_always_trims(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    # n=150(게이트 통과) — 극단치 2개가 트림돼 σ 작아지고 low_n 플래그 없음
    vals = [10.0] * 148 + [1000.0, -1000.0]
    summ = pd.DataFrame({
        "C24": "C24_0", "C6": "C6_0", "C7": 4, "sensor_window": "settled",
        "sensor_id": "C11", "C64": [f"W{i}" for i in range(150)],
        "t0": range(150), "value": vals,
    })
    out = il.compute_limits(summ, cfg)
    row = out.iloc[0]
    assert row["sigma"] < 50            # 극단치 트림됨 → σ 작음
    assert "low_n" not in row["qa_flags"]   # low_n 플래그 제거됨


def test_small_group_raises(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    summ = pd.DataFrame({"C24":"C24_0","C6":"C6_0","C7":4,"sensor_window":"settled",
        "sensor_id":"C11","C64":[f"W{i}" for i in range(50)],"t0":range(50),"value":[10.0]*50})
    with pytest.raises(RuntimeError, match="n<100"):
        il.compute_limits(summ, cfg)


def test_noncontiguous_c6_1_raises(tmp_path):
    """C6_1이 비연속(다중 에피소드)이면 에피소드 사이 C6_0가 손실 → 하드 실패 (silent 손실 방지, 설계 §5 하드닝)."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    recipes = ["C6_0"]*10 + ["C6_1"]*5 + ["C6_0"]*10 + ["C6_1"]*5 + ["C6_0"]*10  # 인터리브 → 중간 C6_0 손실
    n = len(recipes)
    df = pd.DataFrame({
        "C64": [f"W{i:04d}" for i in range(1, n + 1)],
        "C6": recipes, "C10": range(1, n + 1), "C33": [5] * n,
    })
    head = pd.DataFrame({"C64": ["W0000"], "C6": ["C6_0"], "C10": [0], "C33": [99]})  # 리셋 지점
    df = pd.concat([head, df], ignore_index=True)
    with pytest.raises(RuntimeError, match="정산 불일치"):
        il.determine_sample_wafers(df, cfg)


def test_gate_uses_actual_trim_size(tmp_path):
    """게이트는 그룹 크기가 아니라 실제 트림 표본 min(size, rolling_n)로 검사 (config 결합 방지, A②)."""
    cfg = il.LimitConfig.load(_write_cfg(
        tmp_path, limit_engine={"rolling_window_n": 100}, spc={"min_trim_n": 150}))
    # 그룹 크기 500이나 실제 트림 표본은 rolling_n=100 < min_trim_n=150 → 하드 실패 (미클립 게이트면 통과했을 것)
    summ = pd.DataFrame({"C24":"C24_0","C6":"C6_0","C7":4,"sensor_window":"settled",
        "sensor_id":"C11","C64":[f"W{i}" for i in range(500)],"t0":range(500),"value":[10.0]*500})
    with pytest.raises(RuntimeError, match="n<150"):
        il.compute_limits(summ, cfg)


def test_quantile_bounds_from_pretrim_values(tmp_path):
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    # 최솟값이 극단 — 트림 후엔 사라지지만 분위수(트림 전)엔 반영돼야
    vals = [-9999.0] + [10.0] * 498 + [9999.0]     # n=500
    summ = pd.DataFrame({
        "C24": "C24_0", "C6": "C6_0", "C7": 4, "sensor_window": "settled",
        "sensor_id": "C11", "C64": [f"W{i}" for i in range(500)],
        "t0": range(500), "value": vals,
    })
    row = il.compute_limits(summ, cfg).iloc[0]
    assert row["q_low"] == 0.00135 and row["q_high"] == 0.99865
    # 트림 전 분위수라 경계가 극단쪽으로 넓음 (트림된 표본이면 ±10 근처로 붕괴)
    assert row["q_lcl"] < -100 and row["q_ucl"] > 100
    # σ는 트림 후라 좁음 (분리 확인)
    assert abs(row["sigma"]) < 50


def test_sensor_lists_reflect_decisions():
    # C54/C56 = 과도(transient) 재정정 (2026-07-12, 7/10 정착 이동 번복·PM 승인)
    assert "C54" not in il.SENSORS_SETTLED and "C56" not in il.SENSORS_SETTLED
    assert "C54" in il.SENSORS_TRANSIENT_ONLY and "C56" in il.SENSORS_TRANSIENT_ONLY
    assert il.SENSORS_TRANSIENT_ONLY == ["C18", "C27", "C54", "C56"]
    assert "D_CH5960" not in il.DERIVED_SETTLED
    assert il.DERIVED_SETTLED == ["D_VDC_RES"]


def test_derived_features_no_ch5960():
    df = pd.DataFrame({"C11": [-220.0], "C12": [-5.0]})
    out = il.add_derived_features(df)
    assert "D_VDC_RES" in out.columns
    assert "D_CH5960" not in out.columns


# ── Task 1 (B4-3): compute_group_limits 추출 + resolution 산출 ──────────────────

def test_compute_group_limits_matches_compute_limits_row(tmp_path):
    """추출된 compute_group_limits가 기존 compute_limits 그룹 산식과 동일 (회귀 0 — 산식 공유)."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    rng = np.random.default_rng(42)
    vals = list(rng.normal(100.0, 5.0, size=300))
    summ = pd.DataFrame({
        "C24": "C24_0", "C6": "C6_0", "C7": 4, "sensor_window": "settled",
        "sensor_id": "C11", "C64": [f"W{i}" for i in range(300)],
        "t0": range(300), "value": vals,
    })
    row = il.compute_limits(summ, cfg).iloc[0]
    g = il.compute_group_limits(np.array(vals), cfg)
    for f in ["n_wafers", "n_used", "center", "sigma", "ucl", "lcl", "cv",
              "sample_min", "sample_max", "n_distinct", "false_alarm_pct",
              "qa_flags", "q_lcl", "q_ucl"]:
        assert g[f] == row[f], f"{f}: {g[f]!r} != {row[f]!r}"


def test_compute_group_limits_constant_sample(tmp_path):
    """상수 표본 → sigma=0·zero_sigma 플래그·resolution=0 (uniq≤1 무예외)."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    g = il.compute_group_limits(np.array([10.0] * 100), cfg)
    assert g["center"] == 10.0
    assert g["sigma"] == 0.0
    assert "zero_sigma" in g["qa_flags"]
    assert g["resolution"] == 0.0          # uniq 1개 → 0.0, np.diff 빈 배열 예외 없음


def test_compute_group_limits_resolution_quantized(tmp_path):
    """양자화(이산) 표본 → 최소 인접 간격이 분해능."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    # 값이 0.5 스텝: 1.0, 1.5, 2.0, ... → min diff = 0.5
    vals = np.array([1.0, 1.5, 2.0, 2.5, 3.0] * 30)
    g = il.compute_group_limits(vals, cfg)
    assert g["resolution"] == pytest.approx(0.5)


# ── σ 재보정 방식 B (2026-08-06): center=최근 / σ=전체 표본 분리 ──────────────────

def test_compute_group_limits_spread_separates_center_and_sigma(tmp_path):
    """방식 B: μ는 values(center 표본)·σ는 spread 표본에서. 서로 다르면 분리 확인."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    recent = np.full(500, 100.0)                                    # center 표본 = 상수(σ=0)
    full = np.concatenate([recent, np.tile([80.0, 120.0], 250)])   # spread = 넓음(σ>0)
    g = il.compute_group_limits(recent, cfg, spread_values=full)
    assert g["center"] == pytest.approx(100.0)      # μ = recent(상수) 중심
    assert g["sigma"] > 5.0                          # σ = full(넓음) — recent만이면 0
    assert g["n_wafers"] == len(full)                # 표본 수 = spread(전체)
    # 대조: spread=None이면 recent만 → σ=0 (기존 거동)
    assert il.compute_group_limits(recent, cfg)["sigma"] == 0.0


def test_compute_group_limits_spread_none_identical(tmp_path):
    """spread_values=None이면 기존 산식과 완전 동일 (recalc 경로·회귀 0)."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path))
    vals = np.array(list(np.random.default_rng(7).normal(50.0, 3.0, 400)))
    g = il.compute_group_limits(vals, cfg)                  # None
    g2 = il.compute_group_limits(vals, cfg, spread_values=vals)  # 명시적 동일 표본
    assert g == g2


def test_compute_limits_center_recent_sigma_full(tmp_path):
    """n>rolling_n 배선: center=최근창·σ=전체블록 (방식 B). 최근은 좁고 앞은 넓게."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path, limit_engine={"rolling_window_n": 100}))
    early = list(np.random.default_rng(1).normal(100.0, 10.0, 400))  # 앞 400 넓음(σ↑)
    recent = [105.0] * 100                                            # 최근 100 = 상수 105
    summ = pd.DataFrame({"C24": "C24_0", "C6": "C6_0", "C7": 4, "sensor_window": "settled",
        "sensor_id": "C11", "C64": [f"W{i}" for i in range(500)],
        "t0": range(500), "value": early + recent})
    row = il.compute_limits(summ, cfg).iloc[0]
    assert row["center"] == pytest.approx(105.0)     # 최근창 중심(현재 운영점)
    assert row["sigma"] > 5.0                          # 전체블록 σ (최근 100만이면 0)
    assert row["n_wafers"] == 500                      # σ 표본 = 전체블록


# ── 분해능 dead-band (2026-08-06, PM 승인): 관리선 최소폭 ≥ k×계측분해능 ──────────────

def test_resolution_deadband_widens_tight_discrete_band(tmp_path):
    """이산 센서의 좁은 σ·분위수 밴드가 center 기준 최소 k×resolution 반폭으로 넓혀짐."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path, limit_engine={"resolution_deadband_k": 1.0}))
    vals = np.array([11.0] * 490 + [11.5] * 5 + [10.5] * 5)   # 분해능 0.5, σ 매우 좁음
    g = il.compute_group_limits(vals, cfg)
    assert g["resolution"] == pytest.approx(0.5)
    assert g["ucl"] >= g["center"] + 0.5 - 1e-9      # σ 밴드 floor
    assert g["lcl"] <= g["center"] - 0.5 + 1e-9
    assert g["q_ucl"] >= 11.0 + 0.5 - 1e-9           # 분위수 밴드도 median 기준 floor
    assert g["q_lcl"] <= 11.0 - 0.5 + 1e-9
    assert "deadband_floored" in g["qa_flags"]        # QA 가시성 플래그(리뷰 §4 ②)


def test_resolution_deadband_off_when_k_zero(tmp_path):
    """k_res=0이면 dead-band 미적용 — 좁은 밴드 그대로 (기존 산식 보존)."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path, limit_engine={"resolution_deadband_k": 0.0}))
    vals = np.array([11.0] * 490 + [11.5] * 5 + [10.5] * 5)
    g = il.compute_group_limits(vals, cfg)
    assert (g["ucl"] - g["center"]) < 0.5            # floor 없으면 분해능보다 좁음


def test_resolution_deadband_surgical_wide_band_untouched(tmp_path):
    """밴드가 이미 분해능보다 넓으면 floor 무영향 (외과적 — 넓은 σ 그룹 안 건드림)."""
    cfg = il.LimitConfig.load(_write_cfg(tmp_path, limit_engine={"resolution_deadband_k": 1.0}))
    vals = np.random.default_rng(3).normal(100.0, 10.0, 500)   # 3σ≈30 >> resolution
    g = il.compute_group_limits(vals, cfg)
    assert g["ucl"] == pytest.approx(g["center"] + 3.0 * g["sigma"])
    assert g["lcl"] == pytest.approx(g["center"] - 3.0 * g["sigma"])
    assert "deadband_floored" not in g["qa_flags"]    # 넓힘 없으면 플래그 미부착


def test_load_resolutions_key_excludes_chamber(tmp_path):
    """resolution 로드 키 = chamber 제외 4-tuple (recipe·step·window·sensor) — H1.

    CSV는 C24_0 하나, 라이브는 SIM_CH_*라 chamber 포함 키면 전 그룹 miss(무음 고장).
    """
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps({
        "resolutions": {"C6_0|4|settled|C11": 0.5, "C6_0|4|settled|C31": 0.1}
    }, ensure_ascii=False), encoding="utf-8")
    res = il.load_resolutions(meta_path)
    assert res[("C6_0", 4, "settled", "C11")] == 0.5
    assert res[("C6_0", 4, "settled", "C31")] == 0.1
    # 4-tuple 키 (chamber 없음) — 5-tuple(chamber 포함)로 조회하면 miss
    assert ("SIM_CH_1", "C6_0", 4, "settled", "C11") not in res
