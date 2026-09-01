"""seed_qual_snapshots 테스트 (B5-2 Task4 — 스냅샷 시딩 + 통합 왕복).

순수: shift_snapshot_row(밴드폭 보존)·gk_str·snapshot_stats_for_group(전체창 σ·method별 center).
실 DB(skipif): seed→load 왕복 + seed→load→evaluate_qual 통합. 4챔버 fixture(공유 config 미변경).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc.initial_limits import LimitConfig
from src.agent_b_spc.seed_control_limits import make_engine
from src.agent_b_spc import seed_qual_snapshots as sq
from src.agent_b_spc import qual_snapshot_loader as ql
from src.agent_b_spc import qual_engine as qe

_PROBE_TIMEOUT_SEC = 3
_CH = "TEST_CH_QSEED"


def _db_available() -> bool:
    try:
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = create_engine(url, connect_args={"connect_timeout": _PROBE_TIMEOUT_SEC})
        try:
            probe.connect().close()
        finally:
            probe.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


def _limit_cfg(tmp_path):
    cfg = {"limit_engine": {"control_limit_k_sigma": 3.0, "rolling_window_n": 500,
                            "seasoning_exclude_wafers": 10, "resolution_deadband_k": 0.0},
           "spc": {"trim_quantile": 0.01, "false_alarm_warn_pct": 2.0, "cv_warn": 0.5,
                   "q_low": 0.00135, "q_high": 0.99865, "min_trim_n": 100}}
    p = tmp_path / "params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return LimitConfig.load(p)


# ── 순수 변환 ────────────────────────────────────────────────────────

def test_shift_snapshot_row_preserves_band_width():
    """center·ucl·lcl 동일 offset 평행이동 → 밴드폭(ucl−lcl) 보존."""
    stats = {"center": 100.0, "sigma": 5.0, "ucl": 115.0, "lcl": 85.0, "method": "sigma"}
    shifted = sq.shift_snapshot_row(stats, 10.0)
    assert shifted["center"] == 110.0 and shifted["ucl"] == 125.0 and shifted["lcl"] == 95.0
    assert shifted["ucl"] - shifted["lcl"] == stats["ucl"] - stats["lcl"]   # 밴드폭 보존
    assert shifted["sigma"] == 5.0                                          # σ 불변(center 이동)


def test_gk_str_format_symmetric_with_normalize():
    """gk_str 5필드 파이프 → normalize_group_key로 되파싱하면 원 gk."""
    s = sq.gk_str("SIM_CH_1", "C6_0", 4, "settled", "C11")
    assert s == "SIM_CH_1|C6_0|4|settled|C11"
    assert ql.normalize_group_key(*s.split("|")) == ("SIM_CH_1", "C6_0", 4, "settled", "C11")


def test_snapshot_stats_sigma_group_full_window(tmp_path):
    """σ그룹: center=mean·band=±kσ, 전체창(클립 없이 전 값) σ."""
    cfg = _limit_cfg(tmp_path)
    vals = [100.0 + i * 0.1 for i in range(600)]              # 600장 (rolling-500 초과)
    stats = sq.snapshot_stats_for_group(vals, "sigma", cfg)
    assert stats["method"] == "sigma"
    assert stats["center"] == pytest.approx(sum(vals) / len(vals), abs=0.5)
    assert stats["ucl"] == pytest.approx(stats["center"] + 3 * stats["sigma"])


def test_snapshot_stats_quantile_median_center(tmp_path):
    """M2: 분위수그룹 center=median·band=q밴드."""
    cfg = _limit_cfg(tmp_path)
    vals = [0.0] * 300 + [0.0] * 200 + [10.0] * 100          # median=0, mean=1
    stats = sq.snapshot_stats_for_group(vals, "quantile", cfg)
    assert stats["method"] == "quantile"
    assert stats["center"] == pytest.approx(0.0)             # median (mean 1.67 아님)
    assert stats["ucl"] > stats["lcl"]


def test_build_sensor_stats_4chamber_fixture():
    """4챔버 fixture offset shift → gk_str 키·offset 적용 (공유 config 미변경)."""
    base = {("C6_0", 4, "settled", "C11"): {"center": 0.0, "sigma": 1.0,
                                            "ucl": 3.0, "lcl": -3.0, "method": "sigma"}}
    offsets = {f"SIM_CH_{i}": {"C11": float(i)} for i in range(1, 5)}   # 4챔버
    stats = sq.build_sensor_stats(base, "SIM_CH_2", offsets["SIM_CH_2"])
    key = "SIM_CH_2|C6_0|4|settled|C11"
    assert key in stats
    assert stats[key]["center"] == 2.0                       # offset +2


def test_health_sensors_excludes_c12():
    """건강센서 8종 — C12 제외 (제어 기준값이지 건강 신호 아님)."""
    assert "C12" not in sq.HEALTH_SENSORS
    assert set(sq.HEALTH_SENSORS) == {"C11", "C15", "C16", "C17", "C31", "C61", "C62", "C63"}


# ── 실 DB: seed → load 왕복 + 통합 ───────────────────────────────────

def _base_stats():
    return {
        ("C6_0", 4, "settled", "C11"): {"center": 100.0, "sigma": 2.0,
                                        "ucl": 106.0, "lcl": 94.0, "method": "sigma"},
        ("C6_0", 4, "settled", "C17"): {"center": 50.0, "sigma": 1.0,
                                        "ucl": 53.0, "lcl": 47.0, "method": "sigma"},
    }


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_seed_load_roundtrip():
    """seed(1챔버) → load_active_snapshot 왕복 = 입력 stats."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            sq.seed_snapshot(conn, _CH, _base_stats(), {"C11": 5.0, "C17": 0.0},
                             qual_id="seg1-bootstrap", snapshot_id="QUAL-T-SEED-1")
            snap = ql.load_active_snapshot(conn, _CH)
            gk = ("SIM_CH_X", "C6_0", 4, "settled", "C11")   # chamber는 _CH로 치환됨
            gk = (_CH, "C6_0", 4, "settled", "C11")
            assert gk in snap
            assert snap[gk]["center"] == 105.0               # 100 + offset 5
            assert snap[gk]["ucl"] == 111.0 and snap[gk]["lcl"] == 99.0   # 밴드 평행이동
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_e2e_seed_load_evaluate(tmp_path):
    """통합: seed → load → evaluate_qual. 스냅샷 대비 정상 Qual → σ-갭 PASS."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            sq.seed_snapshot(conn, _CH, _base_stats(), {"C11": 0.0, "C17": 0.0},
                             qual_id="seg1-bootstrap", snapshot_id="QUAL-T-E2E-1")
            snapshot = ql.load_active_snapshot(conn, _CH)
            # 스냅샷 center 근처 Qual 5장 → 갭 작음
            qi = {(_CH, "C6_0", 4, "settled", "C11"): [100.0, 100.5, 99.5, 100.0, 100.2],
                  (_CH, "C6_0", 4, "settled", "C17"): [50.0, 50.1, 49.9, 50.0, 50.0]}
            cfg = qe.QualConfig.load()
            r = qe.evaluate_qual(qi, snapshot, cfg, chamber_id=_CH,
                                 qual_id="Q1", snapshot_id="QUAL-T-E2E-1")
            assert r.sigma_gap["verdict"] == "PASS"          # 정상 복귀
            assert r.nelson["verdict"] == "PASS"
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_seed_idempotent_one_active():
    """멱등: 재시딩 시 챔버별 is_active 1개 (직전 활성 비활성화)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            sq.seed_snapshot(conn, _CH, _base_stats(), {"C11": 0.0, "C17": 0.0},
                             qual_id="s1", snapshot_id="QUAL-T-IDEM-1")
            sq.seed_snapshot(conn, _CH, _base_stats(), {"C11": 5.0, "C17": 0.0},
                             qual_id="s2", snapshot_id="QUAL-T-IDEM-2")
            active = conn.execute(text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": _CH}).scalar()
            assert active == 1
            snap = ql.load_active_snapshot(conn, _CH)
            assert snap[(_CH, "C6_0", 4, "settled", "C11")]["center"] == 105.0   # 최신(s2)
        finally:
            trans.rollback()


# ── write_sensor_stats (B6-3-b 리베이스 write — 최종 gk_str stats 직접 적재) ──

def _final_stats(center=123.0):
    """이미 gk_str-keyed·offset shift 없는 최종 sensor_stats (rebase_snapshot 산출 형식)."""
    return {sq.gk_str(_CH, "C6_0", 4, "settled", "C11"): {
        "center": center, "sigma": 2.0, "ucl": center + 6.0, "lcl": center - 6.0, "method": "sigma"}}


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_write_sensor_stats_direct_no_offset():
    """리베이스된 최종 stats를 그대로 적재 — build_sensor_stats(offset shift) 안 거침."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            sq.write_sensor_stats(conn, _CH, _final_stats(123.0),
                                  snapshot_id="QUAL-T-WSS-1", qual_id="rebase")
            snap = ql.load_active_snapshot(conn, _CH)
            gk = (_CH, "C6_0", 4, "settled", "C11")
            assert snap[gk]["center"] == 123.0               # offset shift 없이 그대로
            assert snap[gk]["ucl"] == 129.0
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_write_sensor_stats_idempotent_one_active():
    """재적재 시 기존 active 비활성 → active 1개 (seed_snapshot과 동일 불변식)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            sq.write_sensor_stats(conn, _CH, _final_stats(100.0),
                                  snapshot_id="QUAL-T-WSS-A", qual_id="r1")
            sq.write_sensor_stats(conn, _CH, _final_stats(120.0),
                                  snapshot_id="QUAL-T-WSS-B", qual_id="r2")
            active = conn.execute(text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": _CH}).scalar()
            assert active == 1
            snap = ql.load_active_snapshot(conn, _CH)
            assert snap[(_CH, "C6_0", 4, "settled", "C11")]["center"] == 120.0   # 최신
        finally:
            trans.rollback()
