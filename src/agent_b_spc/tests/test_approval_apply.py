"""approval_apply 테스트 (B5-1a — 승인된 재설정 즉시 적용).

apply_approved(conn, correction_id): PROPOSED 재설정 → control_limits 새 버전 적용 +
status PROPOSED→APPROVED_APPLIED 전이. 실 DB(skipif) + TEST_CH_* INSERT→rollback (M4).
스펙: specs/B5-1a_승인적용_스펙플랜.md §10 테스트 계획.
"""
from __future__ import annotations

import logging
import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc.seed_control_limits import make_engine
from src.agent_b_spc import approval_apply as aa

_PROBE_TIMEOUT_SEC = 3
K = 3.0
_CH = "TEST_CH_APPROVE"
_GK = (_CH, "C6_0", 4, "settled", "C11")


def _db_available() -> bool:
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
                    "WHERE table_name='limit_corrections' "
                    "AND column_name IN ('delta_sigma','center_modified')")).scalar()
                return has == 2   # delta_sigma(B4-3) + center_modified(수정승인) 둘 다 필요
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")

_INS_CL = text(
    "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
    "limit_version, method, center, sigma, ucl, lcl, k_sigma, q_low, q_high, trigger_type, is_active) "
    "VALUES (:ch,'C6_0',4,'settled','C11',:ver,:m,:c,:sg,:u,:l,:k,:ql,:qh,'initial',:a)"
)
_INS_LC = text(
    "INSERT INTO limit_corrections (correction_id, chamber_id, recipe_id, step, sensor_window, "
    "sensor_id, limit_version_before, center_before, center_after, ucl_after, lcl_after, "
    "center_modified, ucl_modified, lcl_modified, "
    "delta_sigma, trigger_type, calc_window_n, status) VALUES "
    "(:cid,:ch,'C6_0',4,'settled','C11','v1',100.0,:ca,:ua,:la,:cm,:um,:lm,0.6,:tt,:cw,:st)"
)


def _seed_initial(conn, center=100.0, sigma=10.0, method="sigma", ucl=None, lcl=None,
                  q_low=None, q_high=None, active=True, ver="v1", k="auto"):
    kv = (None if method == "quantile" else K) if k == "auto" else k  # k=None → sigma 행 k_sigma NULL 재현
    conn.execute(_INS_CL, {
        "ch": _CH, "ver": ver, "m": method, "c": center, "sg": sigma,
        "u": ucl if ucl is not None else center + K * sigma,
        "l": lcl if lcl is not None else center - K * sigma,
        "k": kv, "ql": q_low, "qh": q_high, "a": active})


def _seed_proposed(conn, cid, center_after=106.0, ucl_after=136.0, lcl_after=76.0,
                   center_modified=None, ucl_modified=None, lcl_modified=None,
                   calc_window_n=500, trigger_type="periodic", status="PROPOSED"):
    conn.execute(_INS_LC, {
        "cid": cid, "ch": _CH, "ca": center_after, "ua": ucl_after, "la": lcl_after,
        "cm": center_modified, "um": ucl_modified, "lm": lcl_modified,
        "tt": trigger_type, "cw": calc_window_n, "st": status})


def _corr_status(conn, cid):
    return conn.execute(text(
        "SELECT status FROM limit_corrections WHERE correction_id=:cid"), {"cid": cid}).scalar()


def _cl(conn):
    return conn.execute(text(
        "SELECT limit_version, center, sigma, ucl, lcl, method, q_low, q_high, is_active, trigger_type "
        "FROM control_limits WHERE chamber_id=:ch ORDER BY id"), {"ch": _CH}).mappings().all()


def _corr(conn, cid):
    return conn.execute(text(
        "SELECT status, limit_version_after FROM limit_corrections WHERE correction_id=:cid"),
        {"cid": cid}).mappings().first()


def test_happy_path_applies_and_transitions():
    """승인 → 신규 버전 active(신값)·기존 false + correction APPROVED_APPLIED."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, ucl_after=136.0, lcl_after=76.0)
            aa.apply_approved(conn, "LIM-T-0001")
            rows = _cl(conn)
            assert len(rows) == 2
            v1 = next(r for r in rows if r["limit_version"] == "v1")
            v2 = next(r for r in rows if r["limit_version"] == "v2")
            assert v1["is_active"] is False and v2["is_active"] is True
            assert v2["center"] == 106.0 and v2["ucl"] == 136.0 and v2["lcl"] == 76.0
            assert v2["sigma"] == pytest.approx((136.0 - 106.0) / K)      # 역산 =10
            assert v2["trigger_type"] == "periodic"
            corr = _corr(conn, "LIM-T-0001")
            assert corr["status"] == "APPROVED_APPLIED"
            assert corr["limit_version_after"] == "v2"                    # 동일 f"v{n}"
        finally:
            trans.rollback()


def test_version_string_has_v_prefix_and_channel_works():
    """적용 버전이 'v2'(접두 有), 재적용 시 _NEXT_VERSION 정상 채번(맨정수 회귀 없음)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0)
            aa.apply_approved(conn, "LIM-T-0001")
            _seed_proposed(conn, "LIM-T-0002", center_after=109.0, ucl_after=139.0, lcl_after=79.0)
            aa.apply_approved(conn, "LIM-T-0002")                         # v2 위에서 채번 → v3
            versions = [r["limit_version"] for r in _cl(conn)]
            assert versions == ["v1", "v2", "v3"]
            assert [r["limit_version"] for r in _cl(conn) if r["is_active"]] == ["v3"]
        finally:
            trans.rollback()


def test_idempotent_second_call_noop():
    """같은 correction_id 2회 → 2번째는 status 가드로 no-op (관리선·버전 불변)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0)
            aa.apply_approved(conn, "LIM-T-0001")
            aa.apply_approved(conn, "LIM-T-0001")                         # 재호출
            versions = [r["limit_version"] for r in _cl(conn)]
            assert versions == ["v1", "v2"]                              # v3 안 생김
            assert [r["limit_version"] for r in _cl(conn) if r["is_active"]] == ["v2"]
        finally:
            trans.rollback()


def test_missing_correction_id_noop():
    """없는 correction_id → no-op, 예외 없음."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            aa.apply_approved(conn, "LIM-NOPE-9999")
            assert len(_cl(conn)) == 1                                    # v1만
        finally:
            trans.rollback()


@pytest.mark.parametrize("status", ["APPROVED_APPLIED", "REJECTED"])
def test_non_proposed_status_noop(status):
    """이미 APPROVED_APPLIED / REJECTED 행 → status 가드로 no-op."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, status=status)
            aa.apply_approved(conn, "LIM-T-0001")
            assert len(_cl(conn)) == 1                                    # 관리선 불변
        finally:
            trans.rollback()


def test_superseded_approval_logs_distinctly(caplog):
    """SUPERSEDED 행 승인 → no-op(관리선 불변)이되 '멱등 재전달'과 다른 문장으로 로그 (C 리뷰 #111 §2).

    신규 재수립 제안에 밀려 정당하게 미적용된 사건("승인 났는데 왜 미적용?")을, 무해한 중복
    재전달과 감사에서 구분하기 위함. 동작(False·관리선 불변)은 기존 no-op과 동일 — 로그 문장만 분기.
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, status="SUPERSEDED")
            with caplog.at_level(logging.INFO, logger="src.agent_b_spc.approval_apply"):
                assert aa.apply_approved(conn, "LIM-T-0001") is False     # 미적용
            assert len(_cl(conn)) == 1                                    # 관리선 불변
            assert "SUPERSEDED" in caplog.text and "되돌림 차단" in caplog.text   # 구분 문장
            assert "멱등 재전달" not in caplog.text                        # 멱등 케이스로 안 샘
        finally:
            trans.rollback()


def test_null_center_after_guard():
    """center_after NULL인 PROPOSED → APPLY_FAILED(영구 불량 입력·재시도 방지, 수정승인 §5-3)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            _seed_proposed(conn, "LIM-T-0001", center_after=None, ucl_after=None, lcl_after=None)
            assert aa.apply_approved(conn, "LIM-T-0001") is False         # 미적용
            assert len(_cl(conn)) == 1                                    # 미변경
            assert _corr_status(conn, "LIM-T-0001") == "APPLY_FAILED"     # 영구 실패 마킹
        finally:
            trans.rollback()


# ── 수정 승인(Modify-and-Approve) — B5-1 필드규칙 ──────────────────────

def test_modify_sigma_recomputes_symmetric_band():
    """sigma 그룹 센터 수정 → 밴드 재산출(수정센터±kσ, σ=원안 복원). 대칭 유지 (§4-1)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)                 # k=3
            # 원안 center 106·ucl 136·lcl 76 (σ=10). 엔지니어가 센터만 103으로 수정.
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, ucl_after=136.0, lcl_after=76.0,
                           center_modified=103.0)
            assert aa.apply_approved(conn, "LIM-T-0001") is True
            v2 = next(r for r in _cl(conn) if r["limit_version"] == "v2")
            assert v2["center"] == 103.0                                  # 수정 센터
            assert v2["sigma"] == pytest.approx((136.0 - 106.0) / K)      # σ=10 (원안서 복원)
            assert v2["ucl"] == pytest.approx(103.0 + K * 10.0)           # 133 (재산출)
            assert v2["lcl"] == pytest.approx(103.0 - K * 10.0)           # 73  (대칭)
        finally:
            trans.rollback()


def test_modify_quantile_partial_bound():
    """분위수 그룹: ucl만 수정(부분) → ucl=수정값·lcl=원안·center/σ 승계 (§4-2)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=7.5, method="quantile",
                          ucl=130.0, lcl=70.0, q_low=0.00135, q_high=0.99865)
            _seed_proposed(conn, "LIM-T-0001", center_after=103.0, ucl_after=133.0, lcl_after=73.0,
                           ucl_modified=140.0)                            # lcl_modified=None(부분)
            assert aa.apply_approved(conn, "LIM-T-0001") is True
            v2 = next(r for r in _cl(conn) if r["limit_version"] == "v2")
            assert v2["ucl"] == 140.0                                     # 수정 상한
            assert v2["lcl"] == 73.0                                      # 원안(부분 수정)
            assert v2["center"] == 103.0 and v2["sigma"] == 7.5           # 승계·재산출 없음
        finally:
            trans.rollback()


def test_modify_all_null_equals_normal_approval():
    """수정값 전부 NULL = 일반 승인(원안 그대로) — modify/일반 한 경로 통일 (§4)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, ucl_after=136.0, lcl_after=76.0)
            assert aa.apply_approved(conn, "LIM-T-0001") is True
            v2 = next(r for r in _cl(conn) if r["limit_version"] == "v2")
            assert v2["center"] == 106.0 and v2["ucl"] == 136.0 and v2["lcl"] == 76.0  # 원안
        finally:
            trans.rollback()


def test_modify_guard_fail_bad_bound_apply_failed():
    """분위수 ucl 수정이 center 밑 → ucl≥center 위반 → APPLY_FAILED·관리선 불변 (§5-1)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=7.5, method="quantile",
                          ucl=130.0, lcl=70.0, q_low=0.00135, q_high=0.99865)
            _seed_proposed(conn, "LIM-T-0001", center_after=103.0, ucl_after=133.0, lcl_after=73.0,
                           ucl_modified=90.0)                             # 90 < center 103 → 뒤집힘
            assert aa.apply_approved(conn, "LIM-T-0001") is False
            assert len(_cl(conn)) == 1                                    # 관리선 불변(미적용)
            assert _corr_status(conn, "LIM-T-0001") == "APPLY_FAILED"
        finally:
            trans.rollback()


def test_guard_degenerate_sigma_apply_failed():
    """원안 밴드 붕괴(ucl_after=center_after→σ=0) → σ>0 위반 → APPLY_FAILED (§5-1)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, ucl_after=106.0, lcl_after=106.0)  # σ=0
            assert aa.apply_approved(conn, "LIM-T-0001") is False
            assert len(_cl(conn)) == 1
            assert _corr_status(conn, "LIM-T-0001") == "APPLY_FAILED"
        finally:
            trans.rollback()


def test_sigma_k_sigma_null_fails_closed():
    """sigma 행 k_sigma NULL(데이터 오염) → raise 아님·APPLY_FAILED (§5-3 fail-closed).

    NULL 프리체크가 *_after만 막고 k_sigma를 놓치면 `/None` TypeError→rollback→무한 재전달
    (§5-3이 금지한 바로 그 무한루프). 영구 불량이므로 raise 없이 APPLY_FAILED로 닫혀야 한다.
    """
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0, k=None)         # sigma 행인데 k_sigma NULL
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, ucl_after=136.0, lcl_after=76.0)
            assert aa.apply_approved(conn, "LIM-T-0001") is False          # raise 아님
            assert len(_cl(conn)) == 1                                    # 관리선 불변(미적용)
            assert _corr_status(conn, "LIM-T-0001") == "APPLY_FAILED"
        finally:
            trans.rollback()


def test_quantile_nan_sigma_fails_closed():
    """분위수 승계 σ가 NaN(오염 행) → finite 가드 위반 → APPLY_FAILED (NaN write 차단, §5-1)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=float("nan"), method="quantile",
                          ucl=130.0, lcl=70.0, q_low=0.00135, q_high=0.99865)
            _seed_proposed(conn, "LIM-T-0001", center_after=103.0, ucl_after=133.0, lcl_after=73.0)
            assert aa.apply_approved(conn, "LIM-T-0001") is False
            assert len(_cl(conn)) == 1
            assert _corr_status(conn, "LIM-T-0001") == "APPLY_FAILED"
        finally:
            trans.rollback()


def test_modify_center_zero_is_real_value():
    """center_modified=0.0(falsy지만 not None) → 원안 아닌 0.0 적용 (COALESCE is-None, §4)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, ucl_after=136.0, lcl_after=76.0,
                           center_modified=0.0)                            # 0.0 = 실제 수정값
            assert aa.apply_approved(conn, "LIM-T-0001") is True
            v2 = next(r for r in _cl(conn) if r["limit_version"] == "v2")
            assert v2["center"] == 0.0                                    # 원안 106 아님 (falsy 함정 방지)
        finally:
            trans.rollback()


def test_sigma_ignores_ucl_lcl_modified():
    """sigma 그룹은 ucl/lcl_modified 무시·밴드는 센터±kσ로만 재산출 (§8 method 라우팅)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=10.0)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0, ucl_after=136.0, lcl_after=76.0,
                           ucl_modified=999.0, lcl_modified=-999.0)        # sigma 그룹엔 무의미 → 무시
            assert aa.apply_approved(conn, "LIM-T-0001") is True
            v2 = next(r for r in _cl(conn) if r["limit_version"] == "v2")
            assert v2["ucl"] == pytest.approx(106.0 + K * 10.0)           # 136 (센터±kσ, 999 아님)
            assert v2["lcl"] == pytest.approx(106.0 - K * 10.0)           # 76  (−999 아님)
        finally:
            trans.rollback()


def test_apply_returns_true_on_success():
    """happy path 적용여부 bool=True (호출자 후속 분기용, §7)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0)
            assert aa.apply_approved(conn, "LIM-T-0001") is True
        finally:
            trans.rollback()


def test_no_active_current_row_guard():
    """현행 활성 행 없음(비활성만) → 재조회 후 경고+return, 미변경."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, active=False)                            # v1 비활성
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0)
            aa.apply_approved(conn, "LIM-T-0001")
            assert [r["limit_version"] for r in _cl(conn) if r["is_active"]] == []  # 여전히 활성 0
            assert _corr(conn, "LIM-T-0001")["status"] == "PROPOSED"
        finally:
            trans.rollback()


def test_quantile_group_carries_and_succeeds():
    """quantile 그룹: q_low/q_high·sigma 승계 (역산 아님, k_sigma NULL)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn, center=100.0, sigma=7.5, method="quantile",
                          ucl=130.0, lcl=70.0, q_low=0.00135, q_high=0.99865)
            _seed_proposed(conn, "LIM-T-0001", center_after=103.0, ucl_after=133.0, lcl_after=73.0)
            aa.apply_approved(conn, "LIM-T-0001")
            v2 = next(r for r in _cl(conn) if r["limit_version"] == "v2")
            assert v2["method"] == "quantile"
            assert v2["q_low"] == 0.00135 and v2["q_high"] == 0.99865
            assert v2["sigma"] == 7.5                                     # 현행 승계(역산 아님)
            assert v2["ucl"] == 133.0 and v2["lcl"] == 73.0
        finally:
            trans.rollback()


def test_active_unique_one_row_after_apply():
    """적용 후 그룹당 is_active 정확히 1개 (쓰기 순서 불변식)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            _seed_initial(conn)
            _seed_proposed(conn, "LIM-T-0001", center_after=106.0)
            aa.apply_approved(conn, "LIM-T-0001")
            active = conn.execute(text(
                "SELECT count(*) FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": _CH}).scalar()
            assert active == 1
        finally:
            trans.rollback()
