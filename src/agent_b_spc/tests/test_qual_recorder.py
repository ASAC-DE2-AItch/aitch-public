"""qual_recorder 테스트 (B5-2b — Qual 판정·적재 조각).

순수(무DB): _combine_a_indicators(NULL 규칙)·_propose_verdict(진리표 — σ-갭+AE, Nelson 무영향).
실 DB(skipif): insert_qual 멱등·thresholds JSONB 왕복(T4) · update_qual_confirmed(T7) ·
              rebase_snapshot_center center-only(T8). TEST_CH INSERT→검증→rollback.
스펙: specs/B5-2b_Qual판정적재_라이브배선_스펙플랜.md §9 (T2·T4·T7·T8).
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc import qual_recorder as qr
from src.agent_b_spc import seed_qual_snapshots as sq
from src.agent_b_spc import qual_snapshot_loader as ql
from src.agent_b_spc.seed_control_limits import make_engine

_PROBE_TIMEOUT_SEC = 3
_CH = "TEST_CH_QREC"
_C65_MAX = 1404.0
_AE_MAX = 0.2


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


def _confirmed_col() -> bool:
    """quals.confirmed_verdict 존재까지 판정 (P6-1 컬럼 — init.sql 정본엔 있으나 그 전 생성된
    라이브 DB엔 없을 수 있다. test_recalc_writer.delta_sigma 가드와 동일 관례). sync:
    `ALTER TABLE quals ADD COLUMN IF NOT EXISTS confirmed_verdict VARCHAR(8), ADD ... confirmed_at TIMESTAMPTZ`.
    """
    if not _db_available():
        return False
    try:
        engine = make_engine()
        with engine.connect() as c:
            return c.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='quals' AND column_name='confirmed_verdict'")).scalar() == 1
    except Exception:  # noqa: BLE001
        return False


def _ind(sigma_verdict, *, nelson_verdict="PASS", max_gap=None):
    """QualBIndicators 스텁 — _propose_verdict은 sigma_gap.verdict만 읽는다(Nelson 무영향 검증용)."""
    return SimpleNamespace(
        sigma_gap={"verdict": sigma_verdict, "max_gap_sigma": max_gap},
        nelson={"verdict": nelson_verdict, "violation_count": 9})


# ── T2: _combine_a_indicators (NULL 규칙 3케이스) ─────────────────────

def test_combine_no_predictions_both_none():
    """예측 0장 → (None, None)."""
    assert qr._combine_a_indicators([], _C65_MAX) == (None, None)


def test_combine_all_present():
    """전 장 anomaly·c65 존재 → 평균·전건 판정."""
    preds = [{"anomaly_score": 0.1, "predicted_c65": 1000.0},
             {"anomaly_score": 0.3, "predicted_c65": 1200.0}]
    ae, c65_ok = qr._combine_a_indicators(preds, _C65_MAX)
    assert ae == pytest.approx(0.2)          # (0.1+0.3)/2
    assert c65_ok is True                     # 둘 다 ≤ 1404


def test_combine_partial_anomaly_missing():
    """anomaly 있는 장만 평균(부분 결측) — c65는 전 장."""
    preds = [{"anomaly_score": None, "predicted_c65": 1000.0},
             {"anomaly_score": 0.4, "predicted_c65": 1500.0}]
    ae, c65_ok = qr._combine_a_indicators(preds, _C65_MAX)
    assert ae == pytest.approx(0.4)          # None 제외, 있는 1장만
    assert c65_ok is False                    # 1500 > 1404


def test_combine_zero_anomaly_present_ae_none():
    """예측은 있으나 anomaly 전무 → ae None, c65는 정상 판정."""
    preds = [{"anomaly_score": None, "predicted_c65": 900.0}]
    ae, c65_ok = qr._combine_a_indicators(preds, _C65_MAX)
    assert ae is None and c65_ok is True


# ── T2: _propose_verdict (진리표 — σ-갭+AE, Nelson 무영향) ─────────────

def test_propose_sigma_fail_is_fail():
    assert qr._propose_verdict(_ind("FAIL"), None, _AE_MAX) == "FAIL"


def test_propose_ae_over_max_is_fail_even_if_sigma_pass():
    """AE > max면 σ-갭 PASS라도 FAIL(2원인 중 AE)."""
    assert qr._propose_verdict(_ind("PASS"), 0.5, _AE_MAX) == "FAIL"


def test_propose_sigma_pass_ae_ok_is_pass():
    assert qr._propose_verdict(_ind("PASS"), 0.1, _AE_MAX) == "PASS"


def test_propose_sigma_pass_ae_none_follows_sigma():
    """AE NULL이면 σ-갭만으로 판정."""
    assert qr._propose_verdict(_ind("PASS"), None, _AE_MAX) == "PASS"


def test_propose_sigma_unknown_ae_none_is_none():
    """σ-갭 UNKNOWN + AE 무이상 → 유보(None)."""
    assert qr._propose_verdict(_ind("UNKNOWN"), None, _AE_MAX) is None


def test_propose_sigma_unknown_ae_ok_is_none():
    assert qr._propose_verdict(_ind("UNKNOWN"), 0.1, _AE_MAX) is None


def test_propose_sigma_unknown_ae_fail_is_fail():
    """σ-갭 UNKNOWN이라도 AE 명백 이상이면 안전측 FAIL(미탐 회피)."""
    assert qr._propose_verdict(_ind("UNKNOWN"), 0.9, _AE_MAX) == "FAIL"


# ── T2b: use_ae=False (AE 축 제외 — PM 회신 2026-08-06, AE 포화 전건 FAIL 방지) ──

def test_propose_ae_excluded_ignores_ae_over_max():
    """use_ae=False면 AE>max여도 무시 — σ-갭 PASS면 PASS(전건 FAIL 방지가 핵심)."""
    assert qr._propose_verdict(_ind("PASS"), 0.9, _AE_MAX, use_ae=False) == "PASS"


def test_propose_ae_excluded_unknown_is_none_not_fail():
    """use_ae=False면 σ-갭 UNKNOWN + AE 이상이어도 안전측 FAIL 안 함 → 유보(None)."""
    assert qr._propose_verdict(_ind("UNKNOWN"), 0.9, _AE_MAX, use_ae=False) is None


def test_propose_ae_excluded_sigma_fail_still_fail():
    """use_ae=False여도 σ-갭 FAIL은 FAIL (AE 제외는 σ-갭 판정에 무영향)."""
    assert qr._propose_verdict(_ind("FAIL"), 0.9, _AE_MAX, use_ae=False) == "FAIL"


def test_propose_default_use_ae_true_preserves_truth_table():
    """함수 기본값(use_ae 미지정=True)은 기존 진리표 보존 — AE>max면 FAIL (런타임 제어는 config)."""
    assert qr._propose_verdict(_ind("PASS"), 0.9, _AE_MAX) == "FAIL"


def test_propose_nelson_fail_does_not_affect_verdict():
    """Nelson FAIL이어도 verdict 무영향(트리거 제외·기록만 — 결정6)."""
    assert qr._propose_verdict(_ind("PASS", nelson_verdict="FAIL"), 0.1, _AE_MAX) == "PASS"
    assert qr._propose_verdict(_ind("UNKNOWN", nelson_verdict="FAIL"), None, _AE_MAX) is None


# ── T4·T7·T8: 실 DB writer ───────────────────────────────────────────

def _qual_row(qual_id, **over):
    row = {
        "qual_id": qual_id, "chamber_id": _CH, "wafer_ids": ["W1", "W2", "W3", "W4", "W5"],
        "ae_anomaly_mean": 0.15, "tttm_gap_pct": 1.2, "nelson_violations": 0,
        "c65_within_normal": True,
        "thresholds": {"sigma_gap_unit": "sigma", "partial": False,
                       "rebase_centers": {sq.gk_str(_CH, "C6_0", 4, "settled", "C11"): 105.0}},
        "proposed_verdict": "PASS",
    }
    row.update(over)
    return row


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_insert_qual_idempotent_and_jsonb_roundtrip():
    """T4 — ON CONFLICT(qual_id) DO NOTHING 재실행 무증가 + thresholds JSONB 왕복."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            qid = "QUAL-20260805-TESTQREC-1"
            qr.insert_qual(conn, _qual_row(qid))
            qr.insert_qual(conn, _qual_row(qid, proposed_verdict="FAIL"))   # 재실행 → 무시
            rows = conn.execute(text(
                "SELECT proposed_verdict, thresholds, wafer_ids, approval_status "
                "FROM quals WHERE qual_id = :q"), {"q": qid}).mappings().all()
            assert len(rows) == 1                        # 멱등 — 1행
            assert rows[0]["proposed_verdict"] == "PASS"  # 최초값 유지(DO NOTHING)
            assert rows[0]["approval_status"] == "PENDING"  # DDL DEFAULT
            assert rows[0]["thresholds"]["sigma_gap_unit"] == "sigma"   # JSONB 왕복
            assert rows[0]["wafer_ids"] == ["W1", "W2", "W3", "W4", "W5"]
        finally:
            trans.rollback()


@pytest.mark.skipif(not _confirmed_col(), reason="quals.confirmed_verdict 미존재(P6-1 미sync) — skip")
def test_update_qual_confirmed_sets_fields_and_idempotent():
    """T7 — 확정 UPDATE(loud/quiet·APPROVED·approved_by·confirmed_at) + 멱등 + 미매칭 no-op."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            qid = "QUAL-20260805-TESTQREC-7"
            qr.insert_qual(conn, _qual_row(qid))
            n1 = qr.update_qual_confirmed(conn, qid, "quiet", "eng_kim", "2026-08-05T10:00:00+00:00")
            assert n1 == 1
            row = conn.execute(text(
                "SELECT confirmed_verdict, approval_status, approved_by, confirmed_at, decided_at "
                "FROM quals WHERE qual_id = :q"), {"q": qid}).mappings().first()
            assert row["confirmed_verdict"] == "quiet"
            assert row["approval_status"] == "APPROVED"
            assert row["approved_by"] == "eng_kim"
            assert row["confirmed_at"] is not None and row["decided_at"] is not None
            # 멱등 재소비 — 1행 갱신(무증분)
            assert qr.update_qual_confirmed(conn, qid, "quiet", "eng_kim",
                                            "2026-08-05T10:00:00+00:00") == 1
            # 미매칭 qual_id → 0행 no-op
            assert qr.update_qual_confirmed(conn, "QUAL-NOPE", "loud", "x", None) == 0
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_rebase_snapshot_center_center_only():
    """T8 — center만 갱신·σ 유지·밴드 평행이동·기존 active 비활성+신규 active(1개)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            base = {("C6_0", 4, "settled", "C11"): {"center": 100.0, "sigma": 2.0,
                                                    "ucl": 106.0, "lcl": 94.0, "method": "sigma"}}
            sq.seed_snapshot(conn, _CH, base, {"C11": 0.0},
                             qual_id="seg1-bootstrap", snapshot_id="QUAL-T-QREC-BASE")
            gk = (_CH, "C6_0", 4, "settled", "C11")
            centers = {sq.gk_str(*gk): 110.0}            # center 100 → 110
            qr.rebase_snapshot_center(conn, _CH, centers)
            active = conn.execute(text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": _CH}).scalar()
            assert active == 1                            # 신규 active 1개
            snap = ql.load_active_snapshot(conn, _CH)
            assert snap[gk]["center"] == 110.0            # center 갱신
            assert snap[gk]["sigma"] == 2.0               # σ 유지
            assert snap[gk]["ucl"] == 116.0 and snap[gk]["lcl"] == 104.0   # 밴드 평행이동(폭 12 보존)
        finally:
            trans.rollback()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_rebase_snapshot_center_noop_when_empty():
    """centers 빈dict → no-op(활성 스냅샷 무변경)."""
    engine = make_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            base = {("C6_0", 4, "settled", "C11"): {"center": 100.0, "sigma": 2.0,
                                                    "ucl": 106.0, "lcl": 94.0, "method": "sigma"}}
            sq.seed_snapshot(conn, _CH, base, {"C11": 0.0},
                             qual_id="s", snapshot_id="QUAL-T-QREC-NOOP")
            qr.rebase_snapshot_center(conn, _CH, {})     # no-op
            snap = ql.load_active_snapshot(conn, _CH)
            assert snap[(_CH, "C6_0", 4, "settled", "C11")]["center"] == 100.0   # 무변경
        finally:
            trans.rollback()


def test_rebase_snapshot_center_empty_no_db_touch():
    """centers 부재면 DB 조회 전 즉시 반환 — conn 미사용(순수 가드)."""
    class _Boom:
        def execute(self, *a, **k):
            raise AssertionError("centers 부재 시 DB를 건드리면 안 된다")
    qr.rebase_snapshot_center(_Boom(), _CH, {})          # 예외 없이 반환
    qr.rebase_snapshot_center(_Boom(), _CH, None)
