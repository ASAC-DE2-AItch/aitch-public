"""establish_engine 유닛테스트 (B6-3-b Phase 2 정식 재수립).

묶음1 — establish_group(무상한·항상 승인·trigger_type='incident') ·
        select_establishment_sample(A7 1,000 제외 + 최근 N=500 · 하한 가드).

순수 코어(주입 seam) — DB·Kafka 없이 테스트. `_write_cfg`는 recalc 테스트 패턴 재사용.
"""
import os
from pathlib import Path

import numpy as np
import pytest
import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc import initial_limits as il
from src.agent_b_spc import establish_engine as ee
from src.agent_b_spc import seed_qual_snapshots as sq
from src.agent_b_spc.seed_control_limits import make_engine


def _write_cfg(tmp_path: Path, spc: dict | None = None, limit_engine: dict | None = None) -> Path:
    """테스트용 최소 params.yaml (hermetic) — limit_engine + spc 절."""
    cfg = {
        "limit_engine": {
            "control_limit_k_sigma": 3.0,
            "rolling_window_n": 500,
            "seasoning_exclude_wafers": 10,
            "seasoning_exclude_wafers_loud": 1000,
            "resolution_deadband_k": 0.0,
            "min_establishment_wafers": 500,
            "snapshot_min_n": 30,
            "reset_delta_max_sigma": 0.5,
            "reset_cum_max_sigma_per_cycle": 1.0,
            "recalc_interval_wafers": 500,
            "recalc_deadband_k_se": 2,
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


def _cfg(tmp_path):
    return ee.EstablishConfig.load(_write_cfg(tmp_path))


K = 3.0
GK = ("SIM_CH_1", "C6_0", 4, "settled", "C11")


def _sigma_current(center, sigma, version="v1"):
    return {"center": center, "sigma": sigma, "ucl": center + K * sigma,
            "lcl": center - K * sigma, "method": "sigma", "limit_version": version}


def _quantile_current(center, ucl, lcl, version="v1"):
    return {"center": center, "sigma": 1.0, "ucl": ucl, "lcl": lcl,
            "method": "quantile", "limit_version": version}


def _vals(target, n=500):
    return [float(target)] * n


# ── establish_group ────────────────────────────────────────────────────────


def test_establish_group_always_needs_approval(tmp_path):
    """신규수립은 상한 없이 항상 승인대기 · trigger_type='incident' · 제안값 채움."""
    cfg = _cfg(tmp_path)
    p = ee.establish_group(GK, _vals(150.0), _sigma_current(100.0, 10.0), cfg)
    assert p.decision == "needs_approval"
    assert p.status == "PROPOSED"
    assert p.trigger_type == "incident"
    assert p.new_center == 150.0
    assert p.new_sigma == 0.0                    # 상수 표본
    assert p.new_ucl == pytest.approx(150.0)     # sigma 그룹: center±kσ, σ=0
    assert p.new_lcl == pytest.approx(150.0)
    assert p.n_used == 500
    assert p.group_key == GK


def test_establish_group_no_caps_even_tiny_move(tmp_path):
    """정기(recompute)면 dead_band/auto일 미세 이동도 신규수립은 승인대기 (상한 로직 없음)."""
    cfg = _cfg(tmp_path)
    # 0.05σ 이동 — recompute_group이면 no_change(dead_band)일 값
    p = ee.establish_group(GK, _vals(100.5), _sigma_current(100.0, 10.0), cfg)
    assert p.decision == "needs_approval"
    assert p.status == "PROPOSED"


def test_establish_group_small_sample_no_change(tmp_path):
    """n_used < min_group_n(30) → 변화없음(소표본 가드)."""
    cfg = _cfg(tmp_path)
    p = ee.establish_group(GK, _vals(150.0, n=20), _sigma_current(100.0, 10.0), cfg)
    assert p.decision == "no_change"
    assert p.reason == "small_sample"
    assert p.new_center is None


def test_establish_group_quantile_uses_q_bounds(tmp_path):
    """분위수 그룹: new_ucl/lcl = 새 q_ucl/q_lcl (mean±kσ 아님)."""
    cfg = _cfg(tmp_path)
    cur = _quantile_current(center=100.0, ucl=130.0, lcl=70.0)
    vals = [103.0] * 498 + [203.0, 3.0]
    p = ee.establish_group(GK, vals, cur, cfg)
    ref = il.compute_group_limits(np.array(vals), cfg.limit)
    assert p.decision == "needs_approval"
    assert p.method == "quantile"
    assert p.new_center == pytest.approx(ref["center"])
    assert p.new_ucl == pytest.approx(ref["q_ucl"])
    assert p.new_lcl == pytest.approx(ref["q_lcl"])
    assert ref["q_ucl"] != pytest.approx(ref["ucl"])   # 경로 분리 실증


def test_establish_group_nan_filtered(tmp_path):
    """NaN/None은 판정 전 필터 → n_used 재계산."""
    cfg = _cfg(tmp_path)
    vals = _vals(150.0) + [float("nan")] * 10 + [None] * 5
    p = ee.establish_group(GK, vals, _sigma_current(100.0, 10.0), cfg)
    assert p.decision == "needs_approval"
    assert p.n_used == 500


# ── select_establishment_sample ─────────────────────────────────────────────

GK2 = ("SIM_CH_1", "C6_0", 6, "settled", "C61")


def _cfg2(tmp_path, **le):
    """작은 임계용 cfg — 500장 안 만들고 하한/창 로직 검증."""
    return ee.EstablishConfig.load(_write_cfg(tmp_path, limit_engine=le))


def _wafer(ppc, values):
    return ee.WaferSample(post_pm_count=ppc, values=values)


def test_select_excludes_transient(tmp_path):
    """post_pm_count ≤ seasoning_loud(1,000) 과도는 제외, > 1,000만 표본에 들어간다."""
    cfg = _cfg2(tmp_path, min_establishment_wafers=2, rolling_window_n=10)
    buf = [_wafer(500, {GK: 9.0}),      # 과도 — 제외
           _wafer(1000, {GK: 9.0}),     # 경계(==1000) — 제외(strict >)
           _wafer(1001, {GK: 100.0}),   # 정착 — 포함
           _wafer(1002, {GK: 200.0})]   # 정착 — 포함
    out = ee.select_establishment_sample(buf, cfg)
    assert out == {GK: [100.0, 200.0]}


def test_select_returns_none_below_min(tmp_path):
    """정착 표본 < min_establishment_wafers → None (하한 가드 — 불완전 패키지 차단)."""
    cfg = _cfg2(tmp_path, min_establishment_wafers=3, rolling_window_n=10)
    buf = [_wafer(1001, {GK: 100.0}), _wafer(1002, {GK: 101.0})]   # 정착 2 < 3
    assert ee.select_establishment_sample(buf, cfg) is None


def test_select_takes_recent_n(tmp_path):
    """정착 표본이 rolling_window_n 초과면 최근 N장만 (오래된 것 버림)."""
    cfg = _cfg2(tmp_path, min_establishment_wafers=2, rolling_window_n=3)
    buf = [_wafer(1000 + i, {GK: float(i)}) for i in range(1, 7)]   # ppc 1001..1006, 값 1..6
    out = ee.select_establishment_sample(buf, cfg)
    assert out == {GK: [4.0, 5.0, 6.0]}          # 최근 3장


def test_select_groups_multiple_group_keys(tmp_path):
    """wafer당 여러 그룹 → group_key별 값 리스트로 묶는다."""
    cfg = _cfg2(tmp_path, min_establishment_wafers=2, rolling_window_n=10)
    buf = [_wafer(1001, {GK: 100.0, GK2: 5.0}),
           _wafer(1002, {GK: 101.0, GK2: 6.0})]
    out = ee.select_establishment_sample(buf, cfg)
    assert out == {GK: [100.0, 101.0], GK2: [5.0, 6.0]}


def test_select_recall_after_accumulation(tmp_path):
    """하한 가드 = 대기 메커니즘: 부족하면 None, wafer 쌓여 충족되면 표본 반환 (c 재호출)."""
    cfg = _cfg2(tmp_path, min_establishment_wafers=2, rolling_window_n=10)
    buf = [_wafer(1001, {GK: 100.0})]
    assert ee.select_establishment_sample(buf, cfg) is None      # 1장 — 대기
    buf.append(_wafer(1002, {GK: 200.0}))                        # 유입
    assert ee.select_establishment_sample(buf, cfg) == {GK: [100.0, 200.0]}


# ── rebase_snapshot (R5) ────────────────────────────────────────────────────

GK_NH = ("SIM_CH_1", "C6_0", 4, "settled", "C12")     # 비건강센서(C12) — 제외 대상
GK_Q = ("SIM_CH_1", "C6_0", 5, "settled", "C31")      # 건강+분위수 그룹


def _spread(center, n=60, half=5.0):
    """center 중심 대칭 표본(전체창 σ 검증용) — 트림 무영향 소폭 산포."""
    return [center - half] * (n // 2) + [center + half] * (n // 2)


def test_rebase_only_health_sensors(tmp_path):
    """건강센서 8종만 리베이스 — 비건강(C12) 그룹은 스냅샷에서 제외."""
    cfg = _cfg(tmp_path)
    settled = {GK: _spread(100.0), GK_NH: _spread(50.0)}
    cur = {GK: _sigma_current(100.0, 5.0)}
    out = ee.rebase_snapshot("SIM_CH_1", settled, cur, cfg)
    assert sq.gk_str(*GK) in out
    assert sq.gk_str(*GK_NH) not in out                # C12 제외


def test_rebase_skips_small_sample(tmp_path):
    """유효 표본 < snapshot_min_n(30) 그룹은 skip (붕괴 그룹 ZeroDiv/KeyError 방지)."""
    cfg = _cfg(tmp_path)
    settled = {GK: _spread(100.0, n=20)}               # 20 < 30
    out = ee.rebase_snapshot("SIM_CH_1", settled, {GK: _sigma_current(100.0, 5.0)}, cfg)
    assert out == {}


def test_rebase_delegates_full_window_stats(tmp_path):
    """σ그룹: snapshot_stats_for_group(전체창 σ) 위임 — 결과가 참조 산출과 동일."""
    cfg = _cfg(tmp_path)
    vals = _spread(100.0)
    settled = {GK: vals}
    out = ee.rebase_snapshot("SIM_CH_1", settled, {GK: _sigma_current(100.0, 5.0)}, cfg)
    ref = sq.snapshot_stats_for_group(vals, "sigma", cfg.limit)
    assert out[sq.gk_str(*GK)] == ref
    assert out[sq.gk_str(*GK)]["method"] == "sigma"


def test_rebase_quantile_center_is_median(tmp_path):
    """분위수 그룹(current method): center=median·band=q (snapshot_stats_for_group 위임)."""
    cfg = _cfg(tmp_path)
    vals = _spread(100.0)
    settled = {GK_Q: vals}
    cur = {GK_Q: _quantile_current(100.0, 130.0, 70.0)}
    out = ee.rebase_snapshot("SIM_CH_1", settled, cur, cfg)
    ref = sq.snapshot_stats_for_group(vals, "quantile", cfg.limit)
    assert out[sq.gk_str(*GK_Q)] == ref
    assert out[sq.gk_str(*GK_Q)]["method"] == "quantile"


def test_rebase_skips_group_without_current(tmp_path):
    """current 없는 건강 그룹은 method 불명 → skip(sigma 오폴백 금지, 리뷰 🟡).

    분위수 KEEP* 건강센서에 sigma를 씌우면 구조적 오탐 밴드가 돼 σ-갭 잣대가 오염된다.
    """
    cfg = _cfg(tmp_path)
    out = ee.rebase_snapshot("SIM_CH_1", {GK: _spread(100.0)}, {}, cfg)   # current 비어있음
    assert out == {}                                   # 추정 대신 skip


def test_rebase_nan_filtered_before_min_check(tmp_path):
    """NaN/None 정제 후 표본수 판정 — 유효분만 카운트."""
    cfg = _cfg(tmp_path)
    vals = _spread(100.0, n=30) + [float("nan")] * 50 + [None] * 50
    out = ee.rebase_snapshot("SIM_CH_1", {GK: vals}, {GK: _sigma_current(100.0, 5.0)}, cfg)
    assert sq.gk_str(*GK) in out                        # 유효 30 ≥ 30 → 포함(NaN 무관)


# ── compute_y_threshold (Y-1·Y-2, 순수) ─────────────────────────────────────


def test_compute_y_p95_p99(tmp_path):
    """정착 예측분포 → P95/P99 · trigger_type='incident' · calc_window_n=표본수."""
    cfg = _cfg(tmp_path)
    preds = [float(i) for i in range(1, 101)]           # 1..100
    y = ee.compute_y_threshold("SIM_CH_1", "C6_0", preds, cfg)
    assert y.chamber_id == "SIM_CH_1"
    assert y.recipe_id == "C6_0"
    assert y.p95 == pytest.approx(np.quantile(preds, 0.95))
    assert y.p99 == pytest.approx(np.quantile(preds, 0.99))
    assert y.p99 > y.p95
    assert y.trigger_type == "incident"
    assert y.calc_window_n == 100


def test_compute_y_nan_filtered(tmp_path):
    """NaN/None은 분위수 전 제거 → calc_window_n은 유효분."""
    cfg = _cfg(tmp_path)
    preds = [float(i) for i in range(1, 101)] + [float("nan")] * 7 + [None] * 3
    y = ee.compute_y_threshold("SIM_CH_1", "C6_0", preds, cfg)
    assert y.calc_window_n == 100
    assert y.p95 == pytest.approx(np.quantile([float(i) for i in range(1, 101)], 0.95))


def test_compute_y_empty_returns_none(tmp_path):
    """유효 예측 0장 → None (빈 분위수 크래시 가드)."""
    cfg = _cfg(tmp_path)
    assert ee.compute_y_threshold("SIM_CH_1", "C6_0", [float("nan")], cfg) is None


# ── write_y_threshold (전용 y_thresholds 테이블, 실 DB skipif) ────────────────

_YCH = "TEST_CH_YWRITER"
_PROBE = 3


def _ydb_available() -> bool:
    """DB 접속 + y_thresholds 테이블 존재까지 판정 (TEMP DDL 미반영 환경 skip)."""
    try:
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = create_engine(url, connect_args={"connect_timeout": _PROBE})
        try:
            with probe.connect() as c:
                return c.execute(text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name='y_thresholds'")).scalar() == 1
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


def _y(chamber=_YCH, recipe="C6_0", p95=1404.0, p99=1572.0, n=500):
    return ee.YThreshold(chamber_id=chamber, recipe_id=recipe, p95=p95, p99=p99, calc_window_n=n)


def _yrows(conn):
    return conn.execute(text(
        "SELECT threshold_version, p95, p99, calc_window_n, trigger_type, label_corrected, is_active "
        "FROM y_thresholds WHERE chamber_id=:ch ORDER BY id"), {"ch": _YCH}).mappings().all()


@pytest.mark.skipif(not _ydb_available(), reason="y_thresholds 미반영/DB 미접속 — skip")
def test_write_y_net_new_inserts_active():
    """net-new(선행 행 없음)도 그냥 INSERT — control_limits 승계 경로와 달리 skip 안 함."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            ver = ee.write_y_threshold(conn, _y())
            rows = _yrows(conn)
            assert ver == "v1"
            assert len(rows) == 1
            assert rows[0]["is_active"] is True
            assert rows[0]["p95"] == 1404.0 and rows[0]["p99"] == 1572.0
            assert rows[0]["calc_window_n"] == 500
            assert rows[0]["trigger_type"] == "incident"
            assert rows[0]["label_corrected"] is False        # label-free 1차
        finally:
            trans.rollback()


@pytest.mark.skipif(not _ydb_available(), reason="y_thresholds 미반영/DB 미접속 — skip")
def test_write_y_deactivates_previous_and_bumps_version():
    """두 번째 write → 기존 active=false·신규 v2 active (버전 증가·active 교체)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            ee.write_y_threshold(conn, _y(p95=1400.0, p99=1560.0))
            ee.write_y_threshold(conn, _y(p95=1123.0, p99=1250.0))   # 새 레짐
            rows = _yrows(conn)
            assert [r["threshold_version"] for r in rows] == ["v1", "v2"]
            v1 = next(r for r in rows if r["threshold_version"] == "v1")
            v2 = next(r for r in rows if r["threshold_version"] == "v2")
            assert v1["is_active"] is False and v2["is_active"] is True
            assert v2["p95"] == 1123.0 and v2["p99"] == 1250.0
        finally:
            trans.rollback()


@pytest.mark.skipif(not _ydb_available(), reason="y_thresholds 미반영/DB 미접속 — skip")
def test_write_y_active_unique_one():
    """챔버×레시피당 active 정확히 1개."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            ee.write_y_threshold(conn, _y())
            ee.write_y_threshold(conn, _y(p95=1123.0))
            active = conn.execute(text(
                "SELECT count(*) FROM y_thresholds WHERE chamber_id=:ch AND is_active"),
                {"ch": _YCH}).scalar()
            assert active == 1
        finally:
            trans.rollback()


@pytest.mark.skipif(not _ydb_available(), reason="y_thresholds 미반영/DB 미접속 — skip")
def test_write_y_does_not_commit():
    """writer는 commit 안 함 — 호출자 rollback이 삽입을 되돌린다."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            ee.write_y_threshold(conn, _y())
            assert len(_yrows(conn)) == 1
        finally:
            trans.rollback()
    with engine.connect() as conn:
        left = conn.execute(text(
            "SELECT count(*) FROM y_thresholds WHERE chamber_id=:ch"), {"ch": _YCH}).scalar()
    assert left == 0


# ── read_y_threshold (B4-1 Step5b read seam) ─────────────────────────────────
@pytest.mark.skipif(not _ydb_available(), reason="y_thresholds 미반영/DB 미접속 — skip")
def test_read_y_returns_active_p99():
    """활성 행의 P99를 반환 — writer 왕복(v2 active 값)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            ee.write_y_threshold(conn, _y(p95=1400.0, p99=1560.0))
            ee.write_y_threshold(conn, _y(p95=1123.0, p99=1250.0))   # v2 active
            assert ee.read_y_threshold(conn, _YCH, "C6_0") == 1250.0  # 활성만
        finally:
            trans.rollback()


@pytest.mark.skipif(not _ydb_available(), reason="y_thresholds 미반영/DB 미접속 — skip")
def test_read_y_none_when_absent_or_recipe_missing():
    """행 없거나 recipe None → None(호출부 1572 fallback)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            assert ee.read_y_threshold(conn, _YCH, "C6_9") is None      # 미시딩 recipe
            assert ee.read_y_threshold(conn, _YCH, None) is None        # recipe 결측
        finally:
            trans.rollback()


@pytest.mark.skipif(not _ydb_available(), reason="y_thresholds 미반영/DB 미접속 — skip")
def test_read_y_thresholds_returns_pair():
    """활성 행의 {p95, p99, threshold_version}을 1회 조회로 반환.

    crazy=p99 · Qual/pred enrichment=p95 · threshold_version=그 컷의 출처 버전(Q2 — crazy 마커
    `limit_version`). 재수립하면 활성 행이 v2로 교체되므로 버전도 v2가 따라와야 한다
    (고정 센티넬이면 재수립 후 crazy 가 실제 컷과 다른 버전으로 기록됨).
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            ee.write_y_threshold(conn, _y(p95=1400.0, p99=1560.0))
            ee.write_y_threshold(conn, _y(p95=1123.0, p99=1250.0))   # v2 active
            assert ee.read_y_thresholds(conn, _YCH, "C6_0") == {
                "p95": 1123.0, "p99": 1250.0, "threshold_version": "v2"}
            assert ee.read_y_thresholds(conn, _YCH, "C6_9") is None   # 미시딩 recipe
            assert ee.read_y_thresholds(conn, _YCH, None) is None     # recipe 결측
        finally:
            trans.rollback()


# ── B6-3-e: settled_at 컷오프 (조기 정착) ──────────────────────────────────
def test_select_settled_at_early_cutoff(tmp_path):
    """settled_at=600 전달 → 601부터 표본(조기 정착). seasoning_loud(1,000) 무관."""
    cfg = _cfg2(tmp_path, min_establishment_wafers=2, rolling_window_n=10)
    buf = [_wafer(600, {GK: 9.0}),       # ==600 컷오프 — 제외(strict >)
           _wafer(601, {GK: 100.0}),     # 정착 — 포함
           _wafer(700, {GK: 200.0})]     # 정착 — 포함
    out = ee.select_establishment_sample(buf, cfg, settled_at=600)
    assert out == {GK: [100.0, 200.0]}


def test_select_settled_at_none_is_legacy(tmp_path):
    """settled_at 미전달 = 현행(seasoning_loud=1,000 컷오프) 바이트 동일."""
    cfg = _cfg2(tmp_path, min_establishment_wafers=2, rolling_window_n=10)
    buf = [_wafer(700, {GK: 9.0}),       # 조기 정착이면 포함될 값이나 미전달이면 제외
           _wafer(1001, {GK: 100.0}), _wafer(1002, {GK: 200.0})]
    assert ee.select_establishment_sample(buf, cfg) == {GK: [100.0, 200.0]}
