# -*- coding: utf-8 -*-
"""CT⓪ 경계 복구 테스트 — `quals` 권위 소스 + 최댓값 규칙 (설계 §7 ①).

P6-1(`quals.confirmed_verdict`·`confirmed_at`)이 배포돼 있으므로 폴백 없이 권위 경로가
성립한다. 경계 정수는 `qual_id` 의 SEQ(= pm_count)에서 나온다 — B `_judge_qual` 채번 규칙.

검증 대상:
  · quals 요란 확정 → 경계
  · 세 소스(quals·ct_decisions RESET·pm_log) 불일치 시 **최댓값** 채택 + 경고
  · `confirmed_verdict` 컬럼 미배포(마이그레이션 0010 미적용) → 다른 소스로 완주
  · 접속 장애 등 진짜 오류는 **올린다** (fail-closed, D6)
  · qual_id 채번 규칙이 깨지면 경계를 만들지 않고 경고

실행: python -m pytest src/agent_a_mlops/ct0_bias/tests/test_ct0_bootstrap.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ct0_bias import bootstrap, core  # noqa: E402


# ── 테스트 더블 ────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.conn.queries.append(text)
        if "FROM quals" in text:
            if self.conn.quals_error is not None:
                raise self.conn.quals_error
            self._result = self.conn.quals_row
        elif "FROM ct_decisions" in text:
            self._result = self.conn.reset_row
        else:
            self._result = None

    def fetchone(self):
        return self._result


class FakeConn:
    def __init__(self, quals_row=None, reset_row=None, quals_error=None):
        self.quals_row = quals_row          # (qual_id, confirmed_at)
        self.reset_row = reset_row          # (trigger_reason, created_at)
        self.quals_error = quals_error
        self.queries = []

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        pass


class PgError(Exception):
    """psycopg2 예외 모사 — 판정은 클래스가 아니라 SQLSTATE(`pgcode`) 로 한다."""

    def __init__(self, msg, pgcode=None):
        super().__init__(msg)
        self.pgcode = pgcode


def _no_pm_log(monkeypatch, value=None):
    monkeypatch.setattr(bootstrap, "pm_log_boundary", lambda _ch: value)


# ── qual_id → pm_count (순수) ─────────────────────────────────────────────
def test_pm_count_from_qual_id():
    """B 채번 `QUAL-<날짜>-<챔버>-<pm_count>` 의 마지막 조각이 경계다."""
    assert core.pm_count_from_qual_id("QUAL-20260713-SIM_CH_3-25") == 25
    assert core.pm_count_from_qual_id("QUAL-20260713-SIM-CH-3-7") == 7   # 챔버에 '-' 가 있어도
    assert core.pm_count_from_qual_id("QUAL-20260713-SIM_CH_3-0") == 0   # 0 도 유효한 경계
    for bad in (None, "", "QUAL-20260713-SIM_CH_3", "QUAL-x-y-abc", "qual-rebase"):
        assert core.pm_count_from_qual_id(bad) is None, bad


# ── 경계 해석 ─────────────────────────────────────────────────────────────
def test_quals_is_authoritative(monkeypatch):
    """P6-1 배포 상태에서는 quals 만으로 경계가 선다 (폴백 불필요)."""
    _no_pm_log(monkeypatch)
    conn = FakeConn(quals_row=("QUAL-20260713-SIM_CH_3-25", "2026-07-13T11:00:00Z"))
    assert bootstrap.resolve_boundary(conn, "SIM_CH_3") == (25, "quals.confirmed_verdict")


def test_max_wins_when_sources_disagree(monkeypatch):
    """뒤처진 소스는 '그쪽이 이벤트를 놓쳤다'는 신호 — 경계는 뒤로 가지 않는다."""
    _no_pm_log(monkeypatch, 24)
    conn = FakeConn(quals_row=("QUAL-20260713-SIM_CH_3-25", None),
                    reset_row=(core.reset_reason(26), None))          # 우리 기록이 더 앞섬
    assert bootstrap.resolve_boundary(conn, "SIM_CH_3") == (26, "ct_decisions RESET")

    _no_pm_log(monkeypatch, 27)                                       # pm_log 가 가장 앞섬
    conn2 = FakeConn(quals_row=("QUAL-20260713-SIM_CH_3-25", None),
                     reset_row=(core.reset_reason(26), None))
    assert bootstrap.resolve_boundary(conn2, "SIM_CH_3") == (27, "pm_log.json")


def test_no_source_means_no_boundary(monkeypatch):
    """요란 이력이 없으면 경계 없음 = 전량 자격 (첫 사이클 정상 상태)."""
    _no_pm_log(monkeypatch)
    assert bootstrap.resolve_boundary(FakeConn(), "SIM_CH_3")[0] == core.NO_BOUNDARY


def test_missing_column_falls_through(monkeypatch):
    """마이그레이션 0010 미적용 DB(42703) → 죽지 않고 다른 소스로 완주."""
    _no_pm_log(monkeypatch)
    conn = FakeConn(quals_error=PgError('column "confirmed_verdict" does not exist', "42703"),
                    reset_row=(core.reset_reason(25), None))
    assert bootstrap.resolve_boundary(conn, "SIM_CH_3") == (25, "ct_decisions RESET")


def test_real_db_error_is_raised(monkeypatch):
    """접속 끊김 등은 **올린다** — 삼키면 구레짐 라벨이 전량 창에 들어간다 (D6 fail-closed)."""
    _no_pm_log(monkeypatch)
    conn = FakeConn(quals_error=PgError("server closed the connection", "57P01"))
    try:
        bootstrap.resolve_boundary(conn, "SIM_CH_3")
    except PgError:
        return
    raise AssertionError("DB 장애가 삼켜졌다 — fail-open 은 조용한 오적용이 된다")


def test_unparseable_qual_id_does_not_set_boundary(monkeypatch):
    """채번 규칙이 바뀌면 경계를 만들지 않고 경고 — 잘못된 정수를 쓰는 것보다 낫다."""
    _no_pm_log(monkeypatch)
    conn = FakeConn(quals_row=("QUAL-20260713-SIM_CH_3", None))       # SEQ 없음
    assert bootstrap.resolve_boundary(conn, "SIM_CH_3")[0] == core.NO_BOUNDARY


def test_quiet_verdict_is_not_a_boundary():
    """조용 PM 은 레짐 전환이 아니다 — SQL 이 `confirmed_verdict='loud'` 로 거른다."""
    conn = FakeConn(quals_row=("QUAL-20260713-SIM_CH_3-25", None))
    bootstrap.quals_boundary(conn, "SIM_CH_3")
    assert any("confirmed_verdict='loud'" in q for q in conn.queries)
