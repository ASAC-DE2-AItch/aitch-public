"""recalc_writer 테스트 (B4-3 Task4 — 자동 케이스 DB 반영 + 승인대기 이력).

진입 가드: spy conn(무DB) — decision 불일치 시 쓰기 0.
실 DB(skipif): apply_auto(control_limits 새 버전+기존 false, limit_corrections AUTO_APPLIED)·
              채번 SEQ·버전 증가·active 유일·commit 경계·quantile q 승계·record_proposed(불변).
TEST_CH_* INSERT→검증→rollback (M4 — 기존 시딩 무의존).
"""
from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from src.agent_b_spc.recalc_engine import RecalcProposal
from src.agent_b_spc.seed_control_limits import make_engine
from src.agent_b_spc import recalc_writer as rw

_PROBE_TIMEOUT_SEC = 3
K = 3.0
_CH = "TEST_CH_WRITER"
_GK = (_CH, "C6_0", 4, "settled", "C11")


def _db_available() -> bool:
    """DB 접속 + limit_corrections.delta_sigma 컬럼 존재까지 판정.

    delta_sigma는 init.sql 정본에 있으나 그 전 생성된 라이브 DB엔 없을 수 있다(CREATE IF NOT
    EXISTS는 ALTER 안 함) → 미sync 환경에선 skip으로 우회(spec Task4 §3). sync 방법:
    `ALTER TABLE limit_corrections ADD COLUMN IF NOT EXISTS delta_sigma FLOAT`.
    """
    try:
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = create_engine(url, connect_args={"connect_timeout": _PROBE_TIMEOUT_SEC})
        try:
            with probe.connect() as c:
                has = c.execute(text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name='limit_corrections' AND column_name='delta_sigma'")).scalar()
                return has == 1
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


def _auto(new_center=103.0, method="sigma", new_ucl=None, new_lcl=None, new_sigma=10.0,
          trigger_type="periodic"):
    return RecalcProposal(
        group_key=_GK, decision="auto_applied", method=method, trigger_type=trigger_type,
        n_used=500, new_center=new_center,
        new_ucl=new_ucl if new_ucl is not None else new_center + K * new_sigma,
        new_lcl=new_lcl if new_lcl is not None else new_center - K * new_sigma,
        new_sigma=new_sigma, delta_sigma=0.3, status="AUTO_APPLIED")


def _proposed(new_center=106.0, trigger_type="periodic"):
    return RecalcProposal(
        group_key=_GK, decision="needs_approval", method="sigma", trigger_type=trigger_type,
        n_used=500, new_center=new_center, new_ucl=new_center + 30, new_lcl=new_center - 30,
        new_sigma=10.0, delta_sigma=0.6, status="PROPOSED")


# ── 진입 가드 (spy conn, 무DB) ──────────────────────────────────────

class _SpyConn:
    def __init__(self):
        self.execute_calls = []
        self.commit_calls = 0

    def execute(self, statement, params=None):
        self.execute_calls.append((statement, params))
        return None

    def commit(self):
        self.commit_calls += 1


def test_apply_auto_rejects_no_change():
    """apply_auto는 auto_applied가 아니면 쓰기 0(호출자만 믿지 않는 이중 방어)."""
    conn = _SpyConn()
    nc = RecalcProposal(_GK, "no_change", "sigma", "periodic", 500, reason="dead_band")
    rw.apply_auto(conn, nc)
    assert conn.execute_calls == []
    assert conn.commit_calls == 0


def test_apply_auto_rejects_needs_approval():
    """apply_auto에 승인대기 주입 → 쓰기 0 (record_proposed 소관)."""
    conn = _SpyConn()
    rw.apply_auto(conn, _proposed())
    assert conn.execute_calls == []


def test_record_proposed_rejects_auto():
    """record_proposed는 needs_approval 전용 — auto 주입 시 쓰기 0."""
    conn = _SpyConn()
    rw.record_proposed(conn, _auto())
    assert conn.execute_calls == []


# ── 실 DB (skipif — TEST_CH_* INSERT→rollback) ─────────────────────

_INS_CL = text(
    "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
    "limit_version, method, center, sigma, ucl, lcl, k_sigma, q_low, q_high, trigger_type, is_active) "
    "VALUES (:ch,:rc,:st,:win,:sn,:ver,:m,:c,:sg,:u,:l,:k,:ql,:qh,:tt,:a)"
)


def _seed_initial(conn, center=100.0, sigma=10.0, method="sigma", ucl=None, lcl=None,
                  q_low=None, q_high=None):
    conn.execute(_INS_CL, {
        "ch": _CH, "rc": "C6_0", "st": 4, "win": "settled", "sn": "C11", "ver": "v1",
        "m": method, "c": center, "sg": sigma,
        "u": ucl if ucl is not None else center + K * sigma,
        "l": lcl if lcl is not None else center - K * sigma,
        "k": None if method == "quantile" else K, "ql": q_low, "qh": q_high,
        "tt": "initial", "a": True})


def _rows(conn):
    return conn.execute(text(
        "SELECT limit_version, center, sigma, ucl, lcl, method, q_low, q_high, is_active, trigger_type "
        "FROM control_limits WHERE chamber_id=:ch ORDER BY id"), {"ch": _CH}).mappings().all()


def _corr(conn):
    return conn.execute(text(
        "SELECT correction_id, status, limit_version_before, limit_version_after, center_before, "
        "center_after, delta_sigma, calc_window_n, trigger_type FROM limit_corrections "
        "WHERE chamber_id=:ch ORDER BY id"), {"ch": _CH}).mappings().all()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_apply_auto_writes_new_version_and_history():
    """자동: control_limits v1→false·v2 active(신값) + limit_corrections AUTO_APPLIED 1행."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            rw.apply_auto(conn, _auto(new_center=103.0, new_sigma=12.0))
            rows = _rows(conn)
            assert len(rows) == 2
            v1 = next(r for r in rows if r["limit_version"] == "v1")
            v2 = next(r for r in rows if r["limit_version"] == "v2")
            assert v1["is_active"] is False                 # 기존 비활성
            assert v2["is_active"] is True                  # 신규 활성
            assert v2["center"] == 103.0 and v2["sigma"] == 12.0
            assert v2["trigger_type"] == "periodic"
            corr = _corr(conn)
            assert len(corr) == 1
            assert corr[0]["status"] == "AUTO_APPLIED"
            assert corr[0]["correction_id"] == f"LIM-{_today()}-{_CH}-1"   # SEQ=1(MAX NULL 가드)
            assert corr[0]["limit_version_before"] == "v1"
            assert corr[0]["limit_version_after"] == "v2"
            assert corr[0]["center_before"] == 100.0 and corr[0]["center_after"] == 103.0
            assert corr[0]["delta_sigma"] == pytest.approx(0.3)
            assert corr[0]["calc_window_n"] == 500
            assert corr[0]["trigger_type"] == "periodic"
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_apply_auto_active_unique_one_row():
    """그룹당 is_active 행은 정확히 1개."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            rw.apply_auto(conn, _auto())
            active = conn.execute(text(
                "SELECT count(*) FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": _CH}).scalar()
            assert active == 1
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_apply_auto_version_and_seq_increment():
    """두 번 자동 적용 → 버전 v2·v3 (그룹별 증가), correction SEQ 1·2."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            rw.apply_auto(conn, _auto(new_center=103.0))
            rw.apply_auto(conn, _auto(new_center=106.0))
            versions = [r["limit_version"] for r in _rows(conn)]
            assert versions == ["v1", "v2", "v3"]
            active = [r["limit_version"] for r in _rows(conn) if r["is_active"]]
            assert active == ["v3"]
            seqs = [c["correction_id"].rsplit("-", 1)[1] for c in _corr(conn)]
            assert seqs == ["1", "2"]
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_apply_auto_does_not_commit_rollback_restores():
    """writer는 commit 안 함 — 호출자 rollback이 삽입을 통째로 되돌린다."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            rw.apply_auto(conn, _auto())
            assert len(_rows(conn)) == 2
        finally:
            trans.rollback()
    with engine.connect() as conn:
        left = conn.execute(text(
            "SELECT count(*) FROM control_limits WHERE chamber_id=:ch"), {"ch": _CH}).scalar()
    assert left == 0                                         # 커밋 안 됨 → 전부 원복


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_apply_auto_quantile_carries_q_levels():
    """분위수 그룹 왕복: 신규 행이 q_low/q_high를 승계 보존."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=1.0, method="quantile",
                          ucl=130.0, lcl=70.0, q_low=0.00135, q_high=0.99865)
            p = _auto(new_center=103.0, method="quantile", new_ucl=133.0, new_lcl=73.0, new_sigma=1.0)
            rw.apply_auto(conn, p)
            v2 = next(r for r in _rows(conn) if r["limit_version"] == "v2")
            assert v2["method"] == "quantile"
            assert v2["q_low"] == 0.00135 and v2["q_high"] == 0.99865
            assert v2["ucl"] == 133.0 and v2["lcl"] == 73.0   # q 경계(코어 산출)
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_record_proposed_writes_history_only():
    """M6: 승인대기 → limit_corrections PROPOSED 1행 + control_limits 완전 불변."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=106.0))
            rows = _rows(conn)
            assert len(rows) == 1 and rows[0]["limit_version"] == "v1"   # control_limits 불변
            assert rows[0]["is_active"] is True
            corr = _corr(conn)
            assert len(corr) == 1
            assert corr[0]["status"] == "PROPOSED"
            assert corr[0]["limit_version_after"] is None               # 아직 미적용
            assert corr[0]["center_after"] == 106.0                     # 제안값은 채움
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_record_proposed_supersedes_prior_live_proposed():
    """cross-path dedup (C 리뷰 #110 §8): 같은 그룹에 옛 PROPOSED가 있으면 신규 record_proposed가
    그것을 SUPERSEDED로 밀어내고 live PROPOSED는 새 것 1건만 남긴다.

    시나리오: 정기(periodic) PROPOSED 잔존 상태에서 Phase2 재수립(incident) 제안이 들어옴.
    옛 행은 삭제하지 않고 상태만 바꿔 감사 이력을 보존한다(헌법 1-1 ⓑ).
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=106.0))                          # 정기 선행
            rw.record_proposed(conn, _proposed(new_center=150.0, trigger_type="incident"))  # Phase2 재수립
            corr = _corr(conn)
            live = [c for c in corr if c["status"] == "PROPOSED"]
            gone = [c for c in corr if c["status"] == "SUPERSEDED"]
            assert len(corr) == 2                                       # 두 행 다 보존(감사)
            assert len(live) == 1 and len(gone) == 1                    # live는 정확히 1건
            assert live[0]["center_after"] == 150.0                     # 최신(재수립)이 살아남음
            assert live[0]["trigger_type"] == "incident"
            assert gone[0]["center_after"] == 106.0                     # 옛 정기가 밀려남
            assert gone[0]["trigger_type"] == "periodic"
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_active_unique_constraint_rejects_two_active():
    """쓰기 순서 불변식 근거: 그룹당 active 2행은 idx_control_limits_active_one이 거부.

    apply_auto가 INSERT(active) 前에 UPDATE(기존 false)를 먼저 하는 이유를 실증한다.
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)                              # v1 active
            sp = conn.begin_nested()
            with pytest.raises(IntegrityError):
                conn.execute(_INS_CL, {                      # v2를 active로 추가 → active 2행
                    "ch": _CH, "rc": "C6_0", "st": 4, "win": "settled", "sn": "C11", "ver": "v2",
                    "m": "sigma", "c": 105.0, "sg": 10.0, "u": 135.0, "l": 75.0, "k": K,
                    "ql": None, "qh": None, "tt": "periodic", "a": True})
            sp.rollback()
        finally:
            trans.rollback()


# ── trigger_type 파라미터화 (B6-3-b 결정5 — 'incident' 재수립 승계) ──────────

@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_record_proposed_carries_incident_trigger_type():
    """결정5: needs_approval proposal의 trigger_type='incident'가 이력에 그대로 기록된다.

    establish_group(재수립)은 trigger_type='incident'로 record_proposed를 탄다. writer가
    TRIGGER_PERIODIC 하드코딩이면 firm이 'incident'으로 안 남아 reset_ref 마커·감사가 어긋남.
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=150.0, trigger_type="incident"))
            corr = _corr(conn)
            assert len(corr) == 1
            assert corr[0]["trigger_type"] == "incident"       # 하드코딩이면 'periodic' → RED
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_record_proposed_periodic_still_periodic():
    """무회귀: 정기 경로(trigger_type='periodic')는 그대로 'periodic' 기록."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=106.0))   # 기본 periodic
            assert _corr(conn)[0]["trigger_type"] == "periodic"
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_apply_auto_carries_proposal_trigger_type():
    """결정5: apply_auto도 proposal.trigger_type 승계 — control_limits·이력 둘 다.

    apply_auto는 정기 자동 경로라 통상 'periodic'이나, :166 하드코딩 제거로 proposal 값을
    따르게 해 정합화(무회귀: 기본 periodic 유지는 기존 test가 커버).
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            rw.apply_auto(conn, _auto(new_center=103.0, trigger_type="incident"))
            v2 = next(r for r in _rows(conn) if r["limit_version"] == "v2")
            assert v2["trigger_type"] == "incident"            # :166 하드코딩이면 'periodic' → RED
            assert _corr(conn)[0]["trigger_type"] == "incident"
        finally:
            trans.rollback()


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def test_record_proposed_writes_settle_converged():
    """B6-3-e — settle_converged 영속(미수렴 폴백 경고). False=상한 폴백 / None(기본)=비-재수립 NULL."""
    from sqlalchemy import text as _text
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=106.0), settle_converged=False)
            sc = conn.execute(_text(
                "SELECT settle_converged FROM limit_corrections WHERE status='PROPOSED' "
                "ORDER BY id DESC LIMIT 1")).scalar()
            assert sc is False
        finally:
            trans.rollback()


def test_record_proposed_settle_converged_defaults_null():
    """settle_converged 미전달(비-재수립 경로) → NULL(기존 호출부 무변경)."""
    from sqlalchemy import text as _text
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=106.0))
            sc = conn.execute(_text(
                "SELECT settle_converged FROM limit_corrections WHERE status='PROPOSED' "
                "ORDER BY id DESC LIMIT 1")).scalar()
            assert sc is None
        finally:
            trans.rollback()


def test_record_proposed_writes_shadow_far_pct():
    """Step7b D6 — 오탐 감소율이 shadow_false_alarm_reduction_pct 컬럼에 기록된다."""
    from sqlalchemy import text as _text
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=106.0), shadow_far_pct=41.0)
            far = conn.execute(_text(
                "SELECT shadow_false_alarm_reduction_pct FROM limit_corrections "
                "WHERE status='PROPOSED' ORDER BY id DESC LIMIT 1")).scalar()
            assert far == 41.0
        finally:
            trans.rollback()


def test_record_proposed_shadow_defaults_null():
    """shadow 미전달(UNKNOWN/미채점) → NULL(0.0 아님·D4). 기존 호출부 무변경."""
    from sqlalchemy import text as _text
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0)
            rw.record_proposed(conn, _proposed(new_center=106.0))
            far, missed = conn.execute(_text(
                "SELECT shadow_false_alarm_reduction_pct, shadow_missed_detection "
                "FROM limit_corrections WHERE status='PROPOSED' ORDER BY id DESC LIMIT 1")).first()
            assert far is None and missed is None
        finally:
            trans.rollback()
