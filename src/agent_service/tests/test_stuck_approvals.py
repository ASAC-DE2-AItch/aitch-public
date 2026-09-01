# -*- coding: utf-8 -*-
"""승인 표류 진단 테스트 (W50 · PR #111 리뷰 3번 공약).

두 층으로 나눈다:
  · 판정명 매핑 — Postgres 없이 항상 돈다
  · **조인 SQL 자체** — Postgres 가 있을 때만. 여기가 진짜 축이다

⚠️ **SQL 을 테스트에서 빼면 이 도구는 검증된 적이 없는 것이다.** 파이썬 쪽은 표를 그릴 뿐이고,
   "승인 났는데 적용 안 됨"을 실제로 가려내는 것은 전부 SQL 안에 있다. 그리고 이 도구가 막으려는
   사고가 정확히 *"0건이 나왔는데 그게 없어서인지 못 봐서인지 모르는 것"* 이라, 조인이 진짜로
   무는지를 **데이터를 넣어보고** 확인하지 않으면 도구가 저 자신의 실패 사례가 된다.
   (2026-08-06 실측: 로컬 `approval_records` 가 0행이라 실행만으로는 "0건"밖에 안 나왔다.)

PG 테스트는 **트랜잭션에 넣고 롤백**한다 — 개발 DB 에 잔재를 남기지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.agent_service.scripts.diagnose_stuck_approvals import (  # noqa: E402
    DEFAULT_GRACE_MIN,
    STUCK_LIMIT_SQL,
    _APPLIED_OK,
    _verdict,
)


# --- 판정명 (PG 불필요) --------------------------------------------------------
def test_verdict_names_every_apply_status() -> None:
    """B 가 내는 미적용 상태 3종에 사람이 읽을 진단명이 붙어야 한다."""
    assert "밀려남" in _verdict({"apply_status": "SUPERSEDED"})
    assert "적용 실패" in _verdict({"apply_status": "APPLY_FAILED"})
    assert "미적용" in _verdict({"apply_status": "PROPOSED"})


def test_verdict_missing_row_is_worst_case() -> None:
    """행 자체가 없는 경우 — `None` 을 조용히 통과시키지 않는다 (제일 나쁜 케이스)."""
    assert "행 없음" in _verdict({"apply_status": None})
    assert "행 없음" in _verdict({})


def test_verdict_unknown_status_is_labeled_not_dropped() -> None:
    """B 가 새 status 를 추가해도 **이름 없이 사라지지 않는다** — 모르면 모른다고 쓴다."""
    out = _verdict({"apply_status": "SOMETHING_NEW"})
    assert "미확인" in out and "SOMETHING_NEW" in out


def test_applied_ok_excludes_superseded_and_failed() -> None:
    """'적용 완료'로 보는 집합에 미적용 상태가 섞이면 진단이 통째로 침묵한다."""
    assert "SUPERSEDED" not in _APPLIED_OK
    assert "APPLY_FAILED" not in _APPLIED_OK
    assert "PROPOSED" not in _APPLIED_OK


# --- 조인 SQL (PG 필요) --------------------------------------------------------
def _conn_or_skip():
    """Postgres 가 없으면 스킵. **조용히 통과시키지 않고 이유를 남긴다.**"""
    try:
        import psycopg
        from psycopg.rows import dict_row

        from src.agent_service.app.db import dsn

        return psycopg.connect(dsn(), row_factory=dict_row, connect_timeout=2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres 미기동 — 조인 SQL 미검증 (이 축은 안 돌았다): {exc}")


#: (correction_id, limit_corrections.status | None, approval.status, 승인 시각 오프셋 분, 잡혀야 하나)
_CASES = [
    ("LIM-T-SUP",   "SUPERSEDED",   "APPROVED", 60, True),   # 밀려남
    ("LIM-T-FAIL",  "APPLY_FAILED", "MODIFIED", 60, True),   # 영구 불량 입력
    ("LIM-T-PROP",  "PROPOSED",     "APPROVED", 60, True),   # 오래 미적용
    ("LIM-T-GHOST", None,           "APPROVED", 60, True),   # 행 자체 없음
    ("LIM-T-OK",    "VERIFIED",     "APPROVED", 60, False),  # 정상 적용
    ("LIM-T-AUTO",  "AUTO_APPLIED", "APPROVED", 60, False),  # 자동 적용도 정상
    ("LIM-T-YOUNG", "PROPOSED",     "APPROVED",  2, False),  # 유예 안 — 정상 지연
    ("LIM-T-PEND",  "SUPERSEDED",   "PENDING",  60, False),  # 아직 승인 전
]


def test_stuck_join_catches_exactly_the_unapplied() -> None:
    """조인이 **미적용만** 골라내는가 — 정상 적용·유예 안·미승인은 잡지 않아야 한다.

    잘못 잡으면(오탐) 늑대소년이 되고, 못 잡으면(미탐) 이 도구가 존재할 이유가 없다.
    특히 `LIM-T-YOUNG` 은 유예(grace)가 빠지면 즉시 오탐으로 뒤집히는 자리다.
    """
    conn = _conn_or_skip()
    try:
        with conn.cursor() as cur:
            for cid, lc_status, _ar_status, _off, _hit in _CASES:
                if lc_status is None:
                    continue  # 유령 케이스 — limit_corrections 에 넣지 않는다
                cur.execute(
                    "INSERT INTO limit_corrections (correction_id, chamber_id, recipe_id,"
                    " step, sensor_id, limit_version_before, status)"
                    " VALUES (%s, 'SIM_CH_1', 'C6_0', 4, 'C11', 'v1', %s)",
                    (cid, lc_status),
                )
            for cid, _lc, ar_status, off, _hit in _CASES:
                cur.execute(
                    "INSERT INTO approval_records (incident_id, request_type, status,"
                    " original_value, decided_at)"
                    " VALUES (%s, 'limit_correction', %s, %s::jsonb,"
                    "         NOW() - (%s * INTERVAL '1 minute'))",
                    (f"INC-T-{cid}", ar_status,
                     '{"limit_analysis":{"correction_id":"%s"}}' % cid, off),
                )
            cur.execute(STUCK_LIMIT_SQL, {"grace": DEFAULT_GRACE_MIN, "ok": list(_APPLIED_OK)})
            found = {r["correction_id"] for r in cur.fetchall()}

        expected = {cid for cid, _lc, _ar, _off, hit in _CASES if hit}
        unexpected = {cid for cid, _lc, _ar, _off, hit in _CASES if not hit}
        assert expected <= found, f"미탐 — 잡혔어야 할 건: {expected - found}"
        assert not (found & unexpected), f"오탐 — 잡히면 안 되는 건: {found & unexpected}"
    finally:
        conn.rollback()   # ★ 개발 DB 에 잔재를 남기지 않는다
        conn.close()
