# -*- coding: utf-8 -*-
"""fetch_bundle evidence 폴백 계약 — evidence=[] 는 되살리지 않는다 (C 리뷰 #80-⑦).

`or` 폴백이면 KB 인용 가드를 통과한 근거가 **0건**인 Brief 에서 구 limit_option 의 지난
근거가 되살아난다 — "신설 컬럼(supervisor_evidence)이 정본" 원칙과 반대로 동작.
[] 는 "가드 통과 근거 없음"이라는 유효한 판정 결과이고 C 가 그 상태를 의도적으로 만든다.
폴백은 None(신설 컬럼 이전 구 행)에만 허용된다.

실행: pytest tests/test_supervisor_bundle_fallback.py  (DB 무의존 — fake conn)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.orchestrator.supervisor_adapter import fetch_bundle  # noqa: E402


def _row(evid, counter, lim):
    """SELECT 컬럼 순서와 1:1 — supervisor_adapter.fetch_bundle 의 쿼리 참조.

    (report_id, incident_id, chamber_id, severity_score, suspected_root_causes,
     recipe_option, limit_option, manual_option, supervisor_recommendation,
     supervisor_reason, supervisor_confidence, supervisor_verdict,
     supervisor_evidence, supervisor_counter)
    """
    return ("SUP-20260802-TEST-1", "INC-1", "SIM_CH_1", 55, [],
            {}, lim, {}, "limit_option", "이유", 0.8,
            "baseline_aging", evid, counter)


class _Cur:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cur(self._row)


def test_empty_evidence_is_preserved_not_resurrected():
    """🔴 회귀 본체 — evid=[] 인데 구 limit_option 근거가 되살아나면 안 된다."""
    lim = {"evidence": ["옛 근거 1", "옛 근거 2"], "counter_evidence": "옛 반증"}
    brief, _, _ = fetch_bundle(_Conn(_row([], "반증 탐색 실패", lim)), "INC-1")
    assert brief["supervisor_recommendation"]["evidence"] == []


def test_none_evidence_falls_back_to_option_jsonb():
    """구 행(신설 컬럼 NULL) 하위호환 — None 일 때만 limit_option 으로 폴백한다."""
    lim = {"evidence": ["옛 근거"], "counter_evidence": "옛 반증"}
    brief, _, _ = fetch_bundle(_Conn(_row(None, None, lim)), "INC-1")
    rec = brief["supervisor_recommendation"]
    assert rec["evidence"] == ["옛 근거"]
    assert rec["counter_evidence"] == "옛 반증"      # counter 도 같은 규칙(is not None)


def test_fallback_bottom_is_empty_list():
    """둘 다 없으면 [] — None 이 Brief 에 새지 않는다 (스키마 list[str])."""
    brief, _, _ = fetch_bundle(_Conn(_row(None, None, {})), "INC-1")
    assert brief["supervisor_recommendation"]["evidence"] == []
