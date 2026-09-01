"""qual_snapshot_loader 테스트 (B5-2 Task3 — 스냅샷 로더 + group_key 정규화).

순수: normalize_group_key (step=int 강제·나머지 str — 로더·호출자 공용 계약).
실 DB(skipif): load_active_snapshot — sensor_stats JSONB → dict[gk], 다중 활성 시 최신 1개.
TEST_CH_* INSERT→rollback (M4).
"""
from __future__ import annotations

import json
import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc.seed_control_limits import make_engine
from src.agent_b_spc import qual_snapshot_loader as ql

_PROBE_TIMEOUT_SEC = 3
_CH = "TEST_CH_QUAL"


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


# ── 순수: normalize_group_key ────────────────────────────────────────

def test_normalize_group_key_coerces_step_to_int():
    """step은 int, 나머지는 str — JSONB 역직렬화 str step도 int로 강제."""
    gk = ql.normalize_group_key("SIM_CH_1", "C6_0", "4", "settled", "C11")
    assert gk == ("SIM_CH_1", "C6_0", 4, "settled", "C11")
    assert isinstance(gk[2], int)


def test_normalize_group_key_int_step_stays_int():
    gk = ql.normalize_group_key("SIM_CH_1", "C6_0", 4, "settled", "C11")
    assert gk == ("SIM_CH_1", "C6_0", 4, "settled", "C11")


def test_normalize_matches_across_str_and_int_step():
    """로더(JSONB str step) ↔ 호출자(int step)가 같은 gk로 매칭 (0건 skip 회귀 방지)."""
    from_loader = ql.normalize_group_key("SIM_CH_1", "C6_0", "4", "settled", "C11")
    from_caller = ql.normalize_group_key("SIM_CH_1", "C6_0", 4, "settled", "C11")
    assert from_loader == from_caller


# ── 실 DB: load_active_snapshot ──────────────────────────────────────

_INS = text(
    "INSERT INTO qual_snapshots (snapshot_id, chamber_id, qual_id, sensor_stats, is_active) "
    "VALUES (:sid, :ch, :qid, CAST(:stats AS JSONB), :active)"
)


def _stats(center):
    """gk_str(5필드 파이프) → 스냅샷 통계 dict."""
    return {
        "SIM_CH_1|C6_0|4|settled|C11": {"center": center, "sigma": 1.0,
                                        "ucl": center + 3, "lcl": center - 3, "method": "sigma"},
        "SIM_CH_1|C6_0|4|settled|C17": {"center": 50.0, "sigma": 2.0,
                                        "ucl": 56.0, "lcl": 44.0, "method": "sigma"},
    }


def _seed(conn, sid, qid, center, active):
    conn.execute(_INS, {"sid": sid, "ch": _CH, "qid": qid,
                        "stats": json.dumps(_stats(center)), "active": active})


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_load_active_snapshot_parses_jsonb_to_gk_dict():
    """is_active 스냅샷 sensor_stats → dict[gk]{center,sigma,ucl,lcl,method}, step=int."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, "QUAL-T-0001", "Q1", 100.0, True)
            snap = ql.load_active_snapshot(conn, _CH)
            gk = ("SIM_CH_1", "C6_0", 4, "settled", "C11")
            assert gk in snap                                     # step=int 파싱
            assert snap[gk] == {"center": 100.0, "sigma": 1.0,
                                "ucl": 103.0, "lcl": 97.0, "method": "sigma"}
            assert isinstance(list(snap)[0][2], int)             # step 타입 계약
            assert len(snap) == 2                                 # 2 그룹
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_load_active_snapshot_picks_latest_when_multiple_active():
    """다중 활성 방어: created_at DESC 최신 1개 (부분 유니크 인덱스 부재 앱레벨 방어)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, "QUAL-T-0001", "Q1", 100.0, True)
            # 같은 트랜잭션 NOW 동일 → id tie-break은 미보장이나, created_at으로 최신 우선.
            # 명시적 시각차: 두 번째를 나중 created_at으로
            conn.execute(text(
                "INSERT INTO qual_snapshots (snapshot_id, chamber_id, qual_id, sensor_stats, is_active, created_at) "
                "VALUES (:sid, :ch, :qid, CAST(:stats AS JSONB), true, NOW() + INTERVAL '1 second')"),
                {"sid": "QUAL-T-0002", "ch": _CH, "qid": "Q2", "stats": json.dumps(_stats(200.0))})
            snap = ql.load_active_snapshot(conn, _CH)
            gk = ("SIM_CH_1", "C6_0", 4, "settled", "C11")
            assert snap[gk]["center"] == 200.0                    # 최신(Q2)
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_load_active_snapshot_none_when_absent():
    """활성 스냅샷 없는 챔버 → 빈 dict."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            assert ql.load_active_snapshot(conn, "TEST_CH_NONE") == {}
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_load_active_snapshot_ignores_inactive():
    """is_active=false 스냅샷은 무시."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed(conn, "QUAL-T-0001", "Q1", 100.0, False)        # 비활성만
            assert ql.load_active_snapshot(conn, _CH) == {}
        finally:
            trans.rollback()
