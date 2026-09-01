"""seed_control_limits 유닛테스트 (조인·감시 필터·shift·method 매핑·가드).

소형 합성 fixture: 감시 그룹 2개(KEEP=C11/sigma, KEEP*=C61/quantile) + 비감시 1개(EXCLUDE) ×
챔버 2개(SIM_CH_1/2) — B3-2 Task1 브리프 §Step1 그대로.
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.agent_b_spc import seed_control_limits as sc

# fixture 값은 일부러 통상 디폴트(k_sigma=3.0, q_low=0.00135/q_high=0.99865)와 다르게 잡아
# "CSV carry, 하드코딩 아님"을 증명한다.
K_SIGMA_FIXTURE = 2.7
Q_LOW_FIXTURE = 0.01
Q_HIGH_FIXTURE = 0.99


def _limits_row(sensor_id, step=1, center=10.0, sigma=2.0, ucl=16.0, lcl=4.0,
                 q_lcl=3.0, q_ucl=17.0, qa_flags=np.nan, n_wafers=500, calc_window_n=498):
    """initial_limits_v1.csv 1행 (조인·DROP 대상 여분 컬럼 포함)."""
    return dict(
        chamber_id="C24_0", recipe_id="C6_0", step=step, sensor_window="settled",
        sensor_id=sensor_id, n_wafers=n_wafers, n_used=498, center=center, sigma=sigma,
        ucl=ucl, lcl=lcl, cv=0.2, sample_min=0.0, sample_max=20.0, n_distinct=50,
        false_alarm_pct=1.0, qa_flags=qa_flags, limit_version="v1", trigger_type="initial",
        calc_window_n=calc_window_n, k_sigma=K_SIGMA_FIXTURE, q_low=Q_LOW_FIXTURE,
        q_high=Q_HIGH_FIXTURE, q_lcl=q_lcl, q_ucl=q_ucl,
    )


def _wl_row(sensor_id, decision, method, step=1):
    """monitoring_whitelist_v1.csv 1행."""
    return dict(
        chamber_id="C24_0", recipe_id="C6_0", step=step, sensor_window="settled",
        sensor_id=sensor_id, decision=decision, method=method, decision_reason="x",
        activity_ratio=0.9, mag_ratio=0.9, n_distinct=50, range=20.0, sigma=2.0,
        cv=0.2, false_alarm_pct=1.0, qa_flags=np.nan,
    )


@pytest.fixture
def limits_df():
    return pd.DataFrame([
        _limits_row("C11", qa_flags=np.nan),                 # KEEP → sigma, 빈 qa_flags
        _limits_row("C61", qa_flags="wide_limits"),           # KEEP* → quantile
        _limits_row("C63"),                                   # EXCLUDE → 감시 제외
    ])


@pytest.fixture
def whitelist_df():
    return pd.DataFrame([
        _wl_row("C11", "KEEP", "sigma"),
        _wl_row("C61", "KEEP*", "quantile"),
        _wl_row("C63", "EXCLUDE", "sigma"),
    ])


@pytest.fixture
def offsets():
    return {
        "SIM_CH_1": {"C11": 5.0, "C61": -2.0, "C63": 1.0},
        "SIM_CH_2": {"C11": -3.0, "C61": 1.5, "C63": -1.0},
    }


def _n_monitored(whitelist_df):
    return int(whitelist_df["decision"].isin({"KEEP", "KEEP*"}).sum())


def test_row_count_is_monitored_groups_times_chambers(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    assert len(rows) == _n_monitored(whitelist_df) * len(offsets)


def test_exclude_group_not_emitted(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    assert all(r["sensor_id"] != "C63" for r in rows)


def test_shift_sigma_method(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    r = next(r for r in rows if r["chamber_id"] == "SIM_CH_1" and r["sensor_id"] == "C11")
    off = offsets["SIM_CH_1"]["C11"]
    assert r["method"] == "sigma"
    assert math.isclose(r["center"] - 10.0, off, abs_tol=1e-9)
    assert r["sigma"] == 2.0
    assert math.isclose(r["ucl"], 16.0 + off, abs_tol=1e-9)
    assert math.isclose(r["lcl"], 4.0 + off, abs_tol=1e-9)
    assert r["q_low"] is None
    assert r["q_high"] is None
    assert r["k_sigma"] == K_SIGMA_FIXTURE
    assert r["k_sigma"] != 3.0  # 하드코딩 3.0이 아님을 증명


def test_shift_quantile_method(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    r = next(r for r in rows if r["chamber_id"] == "SIM_CH_2" and r["sensor_id"] == "C61")
    off = offsets["SIM_CH_2"]["C61"]
    assert r["method"] == "quantile"
    assert math.isclose(r["ucl"], 17.0 + off, abs_tol=1e-9)  # q_ucl + off
    assert math.isclose(r["lcl"], 3.0 + off, abs_tol=1e-9)   # q_lcl + off
    assert r["k_sigma"] is None
    assert r["q_low"] == Q_LOW_FIXTURE
    assert r["q_high"] == Q_HIGH_FIXTURE
    assert r["q_low"] != 0.00135  # 하드코딩 디폴트가 아님을 증명


def test_ddl_columns_only_extra_csv_columns_dropped(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    dropped = {"cv", "sample_min", "sample_max", "n_distinct", "false_alarm_pct", "n_used", "q_lcl", "q_ucl"}
    for r in rows:
        assert set(r.keys()) == set(sc.DDL_COLUMNS)
        assert not (set(r.keys()) & dropped)


def test_empty_qa_flags_becomes_none_and_native_types(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    r = next(r for r in rows if r["chamber_id"] == "SIM_CH_1" and r["sensor_id"] == "C11")
    assert r["qa_flags"] is None
    for v in r.values():
        assert not isinstance(v, (np.generic,))


def test_carried_qa_flags_non_empty(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    r = next(r for r in rows if r["chamber_id"] == "SIM_CH_1" and r["sensor_id"] == "C61")
    assert r["qa_flags"] == "wide_limits"


def test_state_constants_not_inline_literals(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    assert sc.LIMIT_VERSION == "v1"
    assert sc.TRIGGER_INITIAL == "initial"
    for r in rows:
        assert r["limit_version"] == sc.LIMIT_VERSION
        assert r["trigger_type"] == sc.TRIGGER_INITIAL


def test_chamber_id_replaced_with_sim_chambers(limits_df, whitelist_df, offsets):
    rows = sc.build_seed_rows(limits_df, whitelist_df, offsets)
    assert all(r["chamber_id"] != "C24_0" for r in rows)
    assert {r["chamber_id"] for r in rows} == set(offsets.keys())


def test_guard_a_missing_offset_raises(limits_df, whitelist_df):
    offsets_missing = {
        "SIM_CH_1": {"C11": 5.0, "C61": -2.0, "C63": 1.0},
        "SIM_CH_2": {"C11": -3.0, "C63": -1.0},  # C61 누락 — C61은 감시 대상(KEEP*)
    }
    with pytest.raises(ValueError):
        sc.build_seed_rows(limits_df, whitelist_df, offsets_missing)


def test_guard_b_join_mismatch_raises(limits_df, whitelist_df, offsets):
    mismatched_wl = pd.concat(
        [whitelist_df, pd.DataFrame([_wl_row("C99", "KEEP", "sigma")])], ignore_index=True
    )  # limits_df에 없는 그룹 추가 → 조인 후 행수(3) != whitelist 원본(4)
    with pytest.raises(ValueError):
        sc.build_seed_rows(limits_df, mismatched_wl, offsets)


def test_seed_empty_rows_raises_before_db_touch():
    """rows가 비면 DB를 건드리기 전에 RuntimeError.

    빈 rows로 진행하면 DELETE(initial/v1)만 커밋되고 INSERT는 0건이라 기존 initial 행이
    조용히 소실된다. 가드는 engine.begin() 이전에 fail-fast 해야 하므로, begin()이 호출되면
    터지는 가짜 엔진으로 'DB 미접촉 + RuntimeError'를 동시에 검증한다.
    """
    class _ExplodingEngine:
        def begin(self):
            raise AssertionError("가드 실패 — 빈 rows인데 DB 트랜잭션이 열렸다")

    with pytest.raises(RuntimeError, match="0건"):
        sc.seed(_ExplodingEngine(), [])


def test_load_offsets_reads_offsets_key(tmp_path):
    import json
    p = tmp_path / "chamber_offsets.json"
    p.write_text(json.dumps({"seed": 42, "offsets": {"SIM_CH_1": {"C11": 1.0}}}), encoding="utf-8")
    result = sc.load_offsets(p)
    assert result == {"SIM_CH_1": {"C11": 1.0}}


# ── B6-2 G6: DB 복원력 (pool_pre_ping + connect_timeout, params 경유) ──────
def test_pg_connect_timeout_from_params():
    """connect_timeout 인라인 금지 — params(agent.pg_connect_timeout_sec=3) 경유(6-1)."""
    assert sc._pg_connect_timeout() == 3


def test_make_engine_pool_pre_ping():
    """죽은 커넥션 자동 재확립 — pool_pre_ping."""
    eng = sc.make_engine("postgresql://u:p@localhost:5432/db")
    assert eng.pool._pre_ping is True


def test_make_engine_wires_pool_and_timeout(monkeypatch):
    """create_engine에 pool_pre_ping·connect_timeout(params 3)이 실제 전달되는지 직접 검증."""
    captured = {}

    def _fake_create_engine(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return "ENGINE"

    monkeypatch.setattr(sc, "create_engine", _fake_create_engine)
    assert sc.make_engine("postgresql://u:p@localhost:5432/db") == "ENGINE"
    assert captured["pool_pre_ping"] is True
    assert captured["connect_args"] == {"connect_timeout": 3}


# ── B6-2 G4: --skip-if-seeded (재기동 멱등, §1-⑤) ─────────────────────────
def test_seeded_count_reads_scalar():
    class _R:
        def scalar(self):
            return 7

    class _C:
        def execute(self, *a, **k):
            return _R()

    assert sc._seeded_count(_C()) == 7


def test_seeded_count_filters_active_only():
    """활성 행만 센다 (PM #88 ③) — 비활성 이력만 남은 DB에서 skip+G5 사망 방지.

    쿼리에 `is_active` 필터가 실려야 한다. 전체 count면 비활성 이력이 skip을 유발한다.
    """
    seen_sql = {}

    class _R:
        def scalar(self):
            return 0

    class _C:
        def execute(self, stmt, *a, **k):
            seen_sql["sql"] = str(stmt)
            return _R()

    sc._seeded_count(_C())
    assert "is_active" in seen_sql["sql"]
