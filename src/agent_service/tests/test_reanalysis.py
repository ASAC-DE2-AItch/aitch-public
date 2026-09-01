"""재분석 폴링 (PM-4) — 플래그 소비 · 재료 복원 · 같은 행 UPDATE.

실 DB·LLM 없이 검증 가능한 계약만 본다:
  · `[재분석]` 플래그가 선 행만 집어온다
  · 재생성 재료(incident_alerts.raw)를 dict 로 복원한다 — 손상 payload 는 스킵
  · **같은 행 UPDATE** 이고 새 행을 만들지 않는다 (PR #70 결정 4)
  · `brief_version` 은 SQL 이 올린다 — 코드가 계산하지 않는다
  · 주기 키가 없으면 폴러를 **띄우지 않는다** (조용한 기본값 금지 — 헌법 6-1)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_service.app import reanalysis as R  # noqa: E402
from agent_service.app import report_writer as W  # noqa: E402

ALERT = {
    "alert_id": "ALERT-20260803-SIMCH2-0001",
    "timestamp": "2026-08-03T10:05:00.000Z",
    "chamber_id": "SIM_CH_2",
    "violations": [{"rule_id": "N3", "sensor": "C11", "window": "settled",
                    "severity": "WARNING", "current_value": -302.0,
                    "limit_version": "v3", "control_limit_upper": -280.0,
                    "control_limit_lower": -340.0}],
    "tttm": {"reference": "fleet_median", "score": 2.3, "top_gap_sensor": "C11",
             "gap_pct": 4.1, "reference_suspect": False},
    "context_score": 72,
    "prediction_context": {"wafer_id": "C64_1", "predicted_c65": 715.2,
                           "shap_top3": ["C11", "C62", "C17"]},
}


class _Cur:
    def __init__(self, rows): self._rows, self.sql, self.params = rows, None, None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.sql, self.params = sql, params
    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)


def _row(raw, ctx: int = 72, alert_id: str = ALERT["alert_id"]) -> tuple:
    """`_SELECT_ALERT` 가 돌려주는 행 모양 — **(raw, context_score, alert_id)**.

    #180 에서 1컬럼 → 3컬럼으로 늘었다(표본을 최신순이 아니라 context_score 최고로
    고르면서 고른 표본을 로그에 남기게 됨). 행 모양이 또 바뀌면 여기 한 곳만 고친다 —
    각 테스트가 튜플 리터럴을 직접 들고 있으면 같은 수정이 파일 전체에 흩어진다.
    """
    return (raw, ctx, alert_id)


# --- 1. 플래그가 선 것만 집어온다 -------------------------------------------------
def test_fetch_pending_returns_triples():
    conn = _Conn([("SUP-A", "INC-1", 2), ("SUP-B", "INC-2", 1)])
    got = R.fetch_pending(conn)
    assert got == [("SUP-A", "INC-1", 2), ("SUP-B", "INC-2", 1)]


def test_pending_query_filters_flag_and_sup_prefix():
    """SUP 접두 + needs_reanalysis 조건이 쿼리에 있어야 한다.

    빠지면 LIM/RCP 상세 행이나 플래그 없는 행까지 재생성 대상이 된다.
    """
    assert "needs_reanalysis IS TRUE" in R._SELECT_PENDING
    assert "SUP-" in R._SELECT_PENDING


# --- 2. 재생성 재료 복원 ----------------------------------------------------------
def test_fetch_alert_payload_dict():
    conn = _Conn([_row(ALERT)])
    assert R.fetch_alert_payload(conn, "INC-1")["alert_id"] == ALERT["alert_id"]


def test_fetch_alert_payload_accepts_json_string():
    """구 드라이버가 jsonb 를 문자열로 주는 구성도 흡수한다."""
    conn = _Conn([_row(json.dumps(ALERT))])
    assert R.fetch_alert_payload(conn, "INC-1")["chamber_id"] == "SIM_CH_2"


@pytest.mark.parametrize("raw", ["{not json", '"123"', "[1,2]", None])
def test_fetch_alert_payload_rejects_non_dict(raw):
    """파싱 성공이 dict 를 뜻하지 않는다 — `"123"`·`[1,2]` 도 유효 JSON (헌법 §7)."""
    conn = _Conn([_row(raw)])
    assert R.fetch_alert_payload(conn, "INC-1") is None


def test_fetch_alert_payload_picks_representative():
    """표본은 '최신'이 아니라 **context_score 최고** — 최신순은 동점 처리로 남는다.

    구 계약은 `ORDER BY alert_ts DESC`(최신)였고 #180 에서 바뀌었다. context_score 는
    위반 폭·강도의 집계축(계약 §3)이라 그 Incident 를 **대표하는** 알람을 고른다 —
    최신순만 쓰면 꼬리 알람이 뽑혀 산문과 근거 카드가 어긋난다(2026-08-10 실측).
    """
    conn = _Conn([_row(ALERT)])
    cur = conn.cursor()
    cur.execute(R._SELECT_ALERT, ("INC-1",))
    order = cur.sql.split("ORDER BY", 1)[1]
    assert "context_score" in order and "alert_ts DESC" in order
    assert order.index("context_score") < order.index("alert_ts"), \
        "동점 처리(alert_ts)가 1순위로 올라오면 대표 표본이 아니라 최신 표본이 된다"


# --- 3. 같은 행 UPDATE (새 행 금지) -----------------------------------------------
def test_update_sql_is_update_not_insert():
    """PR #70 결정 4 — 재생성은 새 행이 아니라 같은 행 갱신."""
    sql = W._UPDATE
    assert sql.strip().startswith("UPDATE agent_reports")
    assert "INSERT" not in sql.upper()
    assert "WHERE report_id = %(report_id)s" in sql


def test_update_sql_bumps_version_and_clears_flag():
    """brief_version 은 **SQL 이** 올린다(경합 방지) · 플래그는 같은 문장에서 내린다.

    플래그를 먼저 내리고 재생성이 실패하면 요청이 조용히 사라진다 — 사람이 버튼을
    눌렀는데 아무 일도 안 일어나는 상태가 된다.
    """
    sql = W._UPDATE
    assert "brief_version = COALESCE(brief_version, 1) + 1" in sql
    assert "needs_reanalysis = FALSE" in sql
    assert "RETURNING brief_version" in sql


def test_update_brief_returns_none_when_row_missing(monkeypatch):
    """덮어쓸 행이 없으면 **INSERT 하지 않고** None — 원본과 별개 행이 생기면 안 된다."""
    class _NoRowConn:
        def cursor(self): return _Cur([])
        def commit(self): pass
    import contextlib

    @contextlib.contextmanager
    def _fake_connect():
        yield _NoRowConn()

    monkeypatch.setattr(W, "connect", _fake_connect)

    class _Brief:
        report_id = "SUP-없음"
        incident_id = "INC-X"
        parallel_options = {}
        wafer_disposition = None
        context_score = 10
        suspected_root_causes = []
        chamber_id = "SIM_CH_2"

        class supervisor_recommendation:  # noqa: N801
            selected = "limit_option"
            reason = "r"
            confidence = 0.5
            verdict = "baseline_aging"
            evidence = ["① [SPC]", "② [MODEL]", "③ [KB]"]
            counter_evidence = "반증"

    assert W.update_brief(_Brief()) is None


def test_update_covers_every_column_save_writes():
    """UPDATE 가 INSERT 와 **같은 컬럼 집합**을 덮는지 — 빠지면 그 필드만 옛 값이 남는다.

    실제로 처음 구현에서 `supervisor_verdict`·`evidence`·`counter`(0002 신설분)가 빠져
    "새 판단 + 옛 근거"가 섞이는 상태였다. 컬럼이 늘 때 UPDATE 를 잊는 것을 여기서 잡는다.
    """
    import re
    insert_cols = set(re.findall(r"%\((\w+)\)s", W._INSERT))
    update_cols = set(re.findall(r"%\((\w+)\)s", W._UPDATE))
    # 의도적 제외 — 재생성해도 바뀌지 않는 신원값이다.
    #   incident_id: 그 행이 속한 Incident 는 재분석으로 바뀌지 않는다(바뀌면 다른 사건이다).
    #   report_id  : UPDATE 에서는 WHERE 키로만 쓴다(같은 행을 찾는 근거).
    identity = {"incident_id"}
    missing = insert_cols - update_cols - identity
    assert not missing, f"UPDATE 에 빠진 컬럼: {sorted(missing)}"


# --- 4. 주기 키가 없으면 폴러를 안 띄운다 -----------------------------------------
def test_poller_disabled_without_param():
    """조용한 기본값 금지(6-1) — 키가 없으면 기능을 끄고 경고한다."""
    class _S:
        def require(self, _key):
            raise KeyError(_key)

    assert R.start_poller(_S()) is None


def test_poller_starts_with_param():
    class _S:
        def require(self, _key):
            return 0.01

    stop = R.start_poller(_S())
    assert stop is not None
    stop.set()          # 데몬 스레드 종료 신호
