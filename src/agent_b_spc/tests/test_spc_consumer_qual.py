"""spc_consumer Qual 라이브 배선 테스트 (B5-2b — T1·T3·T5·T6).

T1 순수 이동(무회귀): `_summarize_rows` 추출이 배치 산식·`_aggregate_to_points`와 값 동일.
T3 수집 버퍼(5장 트리거·dedup·pm_count 교체 partial). T5 분기(is_qual가 nelson/tttm/collector/
provisional 우회 + 일반 wafer 무회귀). T6 통합(실 DB skipif: seed→5장→quals 1행·빈 스냅샷·partial).
스펙: specs/B5-2b_Qual판정적재_라이브배선_스펙플랜.md §9.
"""
from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc import spc_consumer as sc
from src.agent_b_spc import seed_qual_snapshots as sq
from src.agent_b_spc.seed_control_limits import make_engine
from src.agent_b_spc.tests.test_spc_consumer import (
    FakeClock, FakeConsumer, FakeNelson, FakeTTTM, _GK_C11, _GK_DVDC, _limits, _wafer_rows,
)

_PROBE_TIMEOUT_SEC = 3


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


def _qwafer(wid, pm=3, ch="SIM_CH_1", c11=100.0):
    """is_qual 웨이퍼 1건의 raw 샘플 3건 (C11=c11+i / C12=1.0)."""
    return [{"wafer_id": wid, "chamber_id": ch, "recipe_id": "C6_0", "step": 4,
             "stabilization_flag": 0, "pm_count": pm, "is_qual": True,
             "timestamp": f"2026-08-05T01:00:0{i}.000Z",
             "sensors": {"C11": c11 + i, "C12": 1.0}} for i in range(3)]


def _consumer(engine=None, provisional=None, nelson=None, tttm=None, collector=None, limits=None):
    fake = FakeConsumer()
    return sc.SpcConsumer(
        consumer=fake, engine=engine, provisional=provisional,
        idle_flush_sec=30.0, clock=FakeClock(), offset_factory=lambda t, p, o, m=None: [(t, p, o + 1)],
        limits=limits if limits is not None else {},
        nelson=nelson or FakeNelson(), tttm=tttm or FakeTTTM(), collector=collector)


# ── T1: _summarize_rows 순수 이동(무회귀) ─────────────────────────────

def test_summarize_rows_matches_batch():
    """추출 헬퍼가 배치(add_derived_features+build_wafer_summaries)와 값 동일."""
    import pandas as pd
    from src.agent_b_spc.initial_limits import add_derived_features, build_wafer_summaries

    rows = _wafer_rows(3)
    got = {r["sensor_id"]: r["value"] for r in sc._summarize_rows(rows).to_dict("records")}

    df = pd.DataFrame([{"C24": "SIM_CH_1", "C6": "C6_0", "C7": 4, "C42": 0,
                        "C64": "W1", "C10": r["timestamp"],
                        "C11": r["sensors"]["C11"], "C12": r["sensors"]["C12"],
                        **{s: float("nan") for s in
                           ["C9", "C15", "C16", "C17", "C18", "C27", "C31", "C32",
                            "C54", "C56", "C57", "C58", "C61", "C62", "C63"]}}
                       for r in rows])
    batch = build_wafer_summaries(add_derived_features(df))
    expect = {r["sensor_id"]: r["value"] for _, r in batch.iterrows()}
    assert got == pytest.approx(expect)
    assert got["C11"] == pytest.approx(101.0)


def test_aggregate_and_group_values_share_formula():
    """무회귀 — `_aggregate_to_points`(Point)와 `_group_values`가 같은 요약값을 낸다(단일 산식)."""
    rows = _wafer_rows(3)
    pts = {p.group_key: p.value for p in sc._aggregate_to_points(rows, _limits(_GK_C11, _GK_DVDC))}
    gv = dict(sc._group_values(rows))
    for gk, val in pts.items():
        assert gv[gk] == pytest.approx(val)             # 동일 그룹 값 일치


def test_group_values_not_limits_filtered():
    """qual 경로는 limits로 안 거른다 — 스냅샷 없는 그룹도 요약값 산출(교집합은 판정에서)."""
    gv = dict(sc._group_values(_wafer_rows(3)))
    sensors = {gk[-1] for gk in gv}
    assert "C11" in sensors and "D_VDC_RES" in sensors   # 화이트리스트 무관 전 그룹


# ── T3: 수집 버퍼(5장 트리거·dedup·pm_count 교체) ─────────────────────

def _stub_judge(c):
    """_judge_qual를 캡처 스텁으로 교체 — DB 없이 버퍼 거동만 검증."""
    calls = []
    c._judge_qual = lambda ch, buf, partial=False: calls.append((ch, dict(buf), partial))
    return calls


def test_qual_buffer_triggers_at_five():
    """5장(config wafer_count) 도달 시 판정 트리거·버퍼 pop·values 5원소."""
    c = _consumer()
    calls = _stub_judge(c)
    for n in range(1, 6):
        c._process_wafer(_qwafer(f"Q{n}"))
        if n < 5:
            assert calls == []                          # 5장 전엔 미판정
    assert len(calls) == 1
    ch, buf, partial = calls[0]
    assert partial is False and len(buf["wafer_ids"]) == 5
    assert all(len(v) == 5 for v in buf["values"].values())
    assert "SIM_CH_1" not in c._qual_buf                # 판정 후 pop


def test_qual_buffer_dedup_same_wafer():
    """동일 프로세스 재전달(같은 wafer_id) → 이중 카운트 안 함."""
    c = _consumer()
    _stub_judge(c)
    c._process_wafer(_qwafer("Q1"))
    c._process_wafer(_qwafer("Q1"))                     # 재전달
    assert c._qual_buf["SIM_CH_1"]["wafer_ids"] == ["Q1"]


def test_qual_pm_count_change_partial_then_new():
    """pm_count 바뀐 qual 도착 → 직전 미완 버퍼 partial 판정 후 새 Qual 시작."""
    c = _consumer()
    calls = _stub_judge(c)
    c._process_wafer(_qwafer("Q1", pm=3))
    c._process_wafer(_qwafer("Q2", pm=3))
    c._process_wafer(_qwafer("Q3", pm=4))              # pm↑ → pm=3 버퍼 partial 판정
    assert len(calls) == 1
    ch, buf, partial = calls[0]
    assert partial is True and buf["pm_count"] == 3 and len(buf["wafer_ids"]) == 2
    assert c._qual_buf["SIM_CH_1"]["pm_count"] == 4    # 새 버퍼로 교체


# ── T5: 분기 배선(우회 + 일반 wafer 무회귀) ───────────────────────────

class _SpyProv:
    def __init__(self):
        self.on_wafer_calls = 0
        self.pm_calls = []                               # (chamber, pm_count) — PM 경계 관측 기록
        self.verdicts = []

    def on_wafer(self, *a, **k):
        self.on_wafer_calls += 1
        return None

    def on_pm(self, chamber, pm_count, ts=None):
        self.pm_calls.append((chamber, pm_count))

    def on_qual_verdict(self, chamber, verdict):
        self.verdicts.append((chamber, verdict))

    def get_phase(self, chamber):
        from src.agent_b_spc.provisional_mode import Phase
        return Phase.NORMAL


def test_qual_wafer_bypasses_engines_and_collector():
    """is_qual → nelson/tttm feed·collector 우회. **중심 버퍼 적재는 예외**(2026-08-13 개정).

    구 계약은 provisional 까지 "전부 우회"였다(결정1). 그러나 `ProvisionalMode` 의 기본값
    주석이 *"min_center_n=Phase 0 중심 버퍼 최소 표본 — Qual 5장 전제로 기본 3"* 이다: 설계상
    **이 5장이 새-레짐 중심의 재료**다. 전부 우회하면 `_center_for` 가 항상 old_center 로
    폴백해, 이동량이 provisional_k(5)·σ_ref 를 넘는 그룹은 요란 확정 뒤에도 계속 위반한다
    (2026-08-12 CH2 실측 — C17 step1 +6.5σ · step4 −19σ 이동).

    결정1 의 **목적**(표준조건 wafer 가 판정을 오염시키지 않는 것)은 nelson/tttm/collector
    우회로 그대로 지킨다 — 아래 두 assert 가 그 가드다.
    """
    nelson, tttm, prov = FakeNelson([{"sensor_id": "C11", "rule_id": "N1"}]), FakeTTTM(rollup={"x": 1}), _SpyProv()
    collected = []
    c = _consumer(nelson=nelson, tttm=tttm, provisional=prov, collector=collected.append,
                  limits=_limits(_GK_C11, _GK_DVDC))
    _stub_judge(c)                                       # 판정은 여기서 관심 밖
    c._process_wafer(_qwafer("Q1"))
    assert nelson.fed == [] and tttm.fed == []           # feed 우회 (판정 오염 금지 — 결정1 목적)
    assert collected == []                               # collector 우회 (알람 발행 금지)
    assert prov.on_wafer_calls == 1                      # 중심 버퍼는 **먹인다**(개정분)


def test_qual_wafer_observes_pm_boundary():
    """정지 중 챔버의 PM 경계는 **Qual wafer 로만** 온다 — 놓치면 PHASE_0 미진입(2026-08-13).

    회귀 대상 실측(2026-08-12 CH2): STORM → 자동 정지 → producer 가 생산 wafer 발행을 멈춘 뒤
    대PM 주입 → pm_count↑ 가 Qual wafer 에만 실림 → 컨슈머가 우회 → PHASE_0 미진입 →
    요란 확정(`on_qual_verdict`)·R9(`ChamberRequalified`)가 phase 가드에서 **조용히** 무시 →
    가한계·firm 재수립 0건 → 해제 후에도 옛 관리선으로 판정.
    """
    prov = _SpyProv()
    c = _consumer(provisional=prov, limits=_limits(_GK_C11, _GK_DVDC))
    _stub_judge(c)
    c._process_wafer(_qwafer("Q0", pm=3))                # 첫 관측 = 베이스라인(호출 X)
    assert prov.pm_calls == []
    c._process_wafer(_qwafer("Q1", pm=4))                # pm_count↑ = 진짜 PM
    assert prov.pm_calls == [("SIM_CH_1", 4)]
    c._process_wafer(_qwafer("Q2", pm=4))                # 같은 PM 재관측 — 멱등
    assert prov.pm_calls == [("SIM_CH_1", 4)]


def test_normal_wafer_still_feeds_after_split():
    """무회귀 — is_qual 아닌 일반 wafer는 기존대로 두 엔진에 feed."""
    nelson, tttm = FakeNelson(), FakeTTTM()
    c = _consumer(nelson=nelson, tttm=tttm, limits=_limits(_GK_C11, _GK_DVDC))
    c._process_wafer(_wafer_rows(3))                     # is_qual 없음
    assert len(nelson.fed) == 2 and len(tttm.fed) == 2   # C11·D_VDC_RES


# ── 확정 루프 배선(#4·결정9) — _handle_agent → _confirm_qual ─────────

def test_handle_agent_routes_qual_verdict_to_confirm():
    """QualVerdictConfirmed → on_qual_verdict(가한계) + _confirm_qual(quals 확정) 둘 다 호출."""
    prov = _SpyProv()
    c = _consumer(provisional=prov)
    seen = []
    c._confirm_qual = lambda row, ch, v: seen.append((row.get("qual_id"), ch, v))
    c._handle_agent({"event_type": "QualVerdictConfirmed", "chamber_id": "SIM_CH_1",
                     "verdict": "quiet", "qual_id": "QUAL-X-1"})
    assert prov.verdicts == [("SIM_CH_1", "quiet")]      # 기존 가한계 소비 무변경
    assert seen == [("QUAL-X-1", "SIM_CH_1", "quiet")]   # #4 확정 훅 호출


def test_confirm_qual_missing_qual_id_is_noop():
    """qual_id 부재 이벤트 → 확정 skip(예외 없음)."""
    c = _consumer(engine=object())                       # engine 존재하나 도달 전 반환
    c._confirm_qual({"verdict": "quiet"}, "SIM_CH_1", "quiet")   # qual_id 없음 → no-op


# ── T6: 통합(실 DB skipif) ────────────────────────────────────────────

def _seed_snap(conn, ch, center=101.0):
    base = {("C6_0", 4, "settled", "C11"): {"center": center, "sigma": 2.0,
                                            "ucl": center + 6.0, "lcl": center - 6.0,
                                            "method": "sigma"}}
    sq.seed_snapshot(conn, ch, base, {"C11": 0.0},
                     qual_id="seg1-bootstrap", snapshot_id=f"QUAL-T-INT-{ch}")


def _cleanup(engine, chambers):
    with engine.begin() as conn:
        for ch in chambers:
            conn.execute(text("DELETE FROM quals WHERE chamber_id = :ch"), {"ch": ch})
            conn.execute(text("DELETE FROM qual_snapshots WHERE chamber_id = :ch"), {"ch": ch})


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_integration_five_qual_wafers_insert_row():
    """seed 스냅샷 → 5장 → quals 1행(σ-갭 PASS·매핑 검증). 예측 0장이라 AE/C65 NULL."""
    engine = make_engine()
    ch = "TEST_CH_QINT"
    try:
        with engine.begin() as conn:
            _seed_snap(conn, ch, center=101.0)
        c = _consumer(engine=engine)
        for n in range(1, 6):
            c._process_wafer(_qwafer(f"QI{n}", pm=3, ch=ch))
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT qual_id, proposed_verdict, approval_status, tttm_gap_pct, "
                "ae_anomaly_mean, thresholds, wafer_ids FROM quals WHERE chamber_id = :ch"),
                {"ch": ch}).mappings().first()
        assert row is not None
        assert row["qual_id"] == f"QUAL-20260805-{ch}-3"     # full chamber·pm=SEQ
        assert row["proposed_verdict"] == "PASS"             # 갭 0 + AE None → PASS
        assert row["approval_status"] == "PENDING"
        assert row["tttm_gap_pct"] == pytest.approx(0.0, abs=0.5)   # σ 단위
        assert row["ae_anomaly_mean"] is None                # 예측 0장
        assert row["thresholds"]["sigma_gap_unit"] == "sigma"
        assert row["thresholds"]["partial"] is False
        assert len(row["wafer_ids"]) == 5
    finally:
        _cleanup(engine, [ch])


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_integration_no_snapshot_unknown_null_row():
    """스냅샷 없는 챔버 → UNKNOWN → proposed NULL·gap/nelson NULL 적재(graceful)."""
    engine = make_engine()
    ch = "TEST_CH_QINT_NOSNAP"
    try:
        c = _consumer(engine=engine)
        for n in range(1, 6):
            c._process_wafer(_qwafer(f"QN{n}", pm=2, ch=ch))
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT proposed_verdict, tttm_gap_pct, nelson_violations "
                "FROM quals WHERE chamber_id = :ch"), {"ch": ch}).mappings().first()
        assert row is not None
        assert row["proposed_verdict"] is None
        assert row["tttm_gap_pct"] is None and row["nelson_violations"] is None
    finally:
        _cleanup(engine, [ch])


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_integration_partial_proposed_null_and_tagged():
    """partial(3장 후 pm↑) → proposed_verdict NULL·thresholds.partial=True (#2)."""
    engine = make_engine()
    ch = "TEST_CH_QINT_PART"
    try:
        with engine.begin() as conn:
            _seed_snap(conn, ch, center=101.0)
        c = _consumer(engine=engine)
        for n in range(1, 4):                            # 3장만 pm=3
            c._process_wafer(_qwafer(f"QP{n}", pm=3, ch=ch))
        c._process_wafer(_qwafer("QP_NEW", pm=4, ch=ch))  # pm↑ → pm=3 partial 판정
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT proposed_verdict, thresholds, wafer_ids FROM quals "
                "WHERE qual_id = :q"), {"q": f"QUAL-20260805-{ch}-3"}).mappings().first()
        assert row is not None
        assert row["proposed_verdict"] is None           # partial 자동 제안 안 함
        assert row["thresholds"]["partial"] is True
        assert len(row["wafer_ids"]) == 3                # 실장수
        assert row["thresholds"]["rebase_centers"] == {}  # partial은 미저장
    finally:
        _cleanup(engine, [ch])


def _confirmed_col() -> bool:
    if not _db_available():
        return False
    try:
        with make_engine().connect() as c:
            return c.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='quals' AND column_name='confirmed_verdict'")).scalar() == 1
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _confirmed_col(), reason="quals.confirmed_verdict 미존재(P6-1 미sync) — skip")
def test_integration_confirm_quiet_updates_quals_and_rebases():
    """조용 확정 이벤트 → quals APPROVED/quiet + 스냅샷 center-only 리베이스(결정9)."""
    from src.agent_b_spc import qual_recorder as qr
    from src.agent_b_spc import qual_snapshot_loader as ql
    engine = make_engine()
    ch = "TEST_CH_QCONF"
    gk = (ch, "C6_0", 4, "settled", "C11")
    qid = f"QUAL-20260805-{ch}-3"
    try:
        with engine.begin() as conn:
            _seed_snap(conn, ch, center=100.0)           # center 100
            qr.insert_qual(conn, {
                "qual_id": qid, "chamber_id": ch, "wafer_ids": ["W1", "W2", "W3", "W4", "W5"],
                "ae_anomaly_mean": None, "tttm_gap_pct": 0.1, "nelson_violations": 0,
                "c65_within_normal": True, "proposed_verdict": "PASS",
                "thresholds": {"partial": False, "rebase_centers": {sq.gk_str(*gk): 108.0}}})
        c = _consumer(engine=engine, provisional=_SpyProv())
        c._handle_agent({"event_type": "QualVerdictConfirmed", "chamber_id": ch,
                         "verdict": "quiet", "qual_id": qid, "approved_by": "eng_lee",
                         "confirmed_at": "2026-08-05T12:00:00+00:00"})
        with engine.connect() as conn:
            q = conn.execute(text(
                "SELECT confirmed_verdict, approval_status, approved_by FROM quals "
                "WHERE qual_id = :q"), {"q": qid}).mappings().first()
            snap = ql.load_active_snapshot(conn, ch)
        assert q["confirmed_verdict"] == "quiet" and q["approval_status"] == "APPROVED"
        assert q["approved_by"] == "eng_lee"
        assert snap[gk]["center"] == 108.0               # center-only 리베이스
        assert snap[gk]["sigma"] == 2.0                  # σ 유지
    finally:
        _cleanup(engine, [ch])


@pytest.mark.skipif(not _confirmed_col(), reason="quals.confirmed_verdict 미존재(P6-1 미sync) — skip")
def test_integration_confirm_loud_no_rebase():
    """요란 확정 → quals APPROVED/loud 이나 스냅샷 리베이스 안 함(center 불변)."""
    from src.agent_b_spc import qual_recorder as qr
    from src.agent_b_spc import qual_snapshot_loader as ql
    engine = make_engine()
    ch = "TEST_CH_QCONFL"
    gk = (ch, "C6_0", 4, "settled", "C11")
    qid = f"QUAL-20260805-{ch}-3"
    try:
        with engine.begin() as conn:
            _seed_snap(conn, ch, center=100.0)
            qr.insert_qual(conn, {
                "qual_id": qid, "chamber_id": ch, "wafer_ids": ["W1", "W2", "W3", "W4", "W5"],
                "ae_anomaly_mean": None, "tttm_gap_pct": 5.0, "nelson_violations": 1,
                "c65_within_normal": True, "proposed_verdict": "FAIL",
                "thresholds": {"partial": False, "rebase_centers": {sq.gk_str(*gk): 108.0}}})
        c = _consumer(engine=engine, provisional=_SpyProv())
        c._handle_agent({"event_type": "QualVerdictConfirmed", "chamber_id": ch,
                         "verdict": "loud", "qual_id": qid, "approved_by": "eng_lee",
                         "confirmed_at": "2026-08-05T12:00:00+00:00"})
        with engine.connect() as conn:
            q = conn.execute(text("SELECT confirmed_verdict FROM quals WHERE qual_id = :q"),
                             {"q": qid}).mappings().first()
            snap = ql.load_active_snapshot(conn, ch)
        assert q["confirmed_verdict"] == "loud"
        assert snap[gk]["center"] == 100.0               # 요란은 리베이스 안 함(불변)
    finally:
        _cleanup(engine, [ch])
