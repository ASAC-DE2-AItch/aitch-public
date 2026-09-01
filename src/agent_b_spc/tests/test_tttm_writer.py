"""③ tttm_comparisons writer 테스트 (spec: specs/B4-2_TTTM_writer_스펙플랜.md).

Task1: transform(rollup→DDL row)·None 드롭·엔진 계약검사 (무DB, 순수).
Task2: writer(INSERT·no-commit·[]→0) — spy conn(무DB).
Task3: 왕복·롤백 통합 — 실 DB(`skipif` 무DB skip).
"""

from __future__ import annotations

import json
import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from src.agent_b_spc.seed_control_limits import make_engine
from src.agent_b_spc.tttm_engine import ALERT_FIELDS
from src.agent_b_spc.tttm_writer import (
    DDL_COLUMNS,
    rollup_to_row,
    rollups_to_rows,
    write_comparisons,
)

_PROBE_TIMEOUT_SEC = 3


def _db_available() -> bool:
    """`DATABASE_URL`로 짧은 타임아웃 접속을 시도해 통합테스트용 DB 가용 여부를 판정한다.

    무DB(미설정·미기동) 환경에서도 CI를 그린으로 유지하기 위한 skip 판정 — 원인 불문
    실패는 즉시 False(`test_control_limits_loader`와 동일 패턴).
    """
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
    except Exception:  # noqa: BLE001 — 접속 가능 여부만 판정, 원인 불문 skip
        return False

# 엔진 chamber_rollup() 산출 형태와 동일한 정적 표본 (tttm_engine.py §4-3).
SAMPLE_ROLLUP = {
    "reference": "fleet_median",
    "score": 3.2,
    "top_gap_sensor": "C62",              # worst(경로②) — suspect 센서와 다를 수 있음(decouple)
    "gap_pct": 12.5,
    "reference_suspect": True,
    "suspect_sensors": ["C17"],           # 실제 공통이동 센서(경로①) — worst와 별개
    "reference_id": "v1",
    "chamber_id": "SIM_CH_1",
}

# 엔진이 rollup에 싣는 키 = alert 필드 ∪ DB 전용 2키 (계약검사 기준).
EXPECTED_ROLLUP_KEYS = ALERT_FIELDS | {"reference_id", "chamber_id"}


# ── Task 1: transform ──────────────────────────────────────────────

def test_sample_rollup_matches_engine_contract():
    """계약검사: 표본 rollup 키 == 엔진 ALERT_FIELDS ∪ {reference_id, chamber_id}.

    엔진이 필드를 rename하면(예: score→tttm_score) 여기서 조용히 깨지므로 즉시 fail.
    """
    assert set(SAMPLE_ROLLUP.keys()) == EXPECTED_ROLLUP_KEYS


def test_rollup_to_row_maps_eight_keys():
    """8개 rollup 키 → 8개 DDL 컬럼으로 값·이름 매핑. suspect_sensors는 JSONB 문자열로 직렬화."""
    row = rollup_to_row(SAMPLE_ROLLUP)
    assert row == {
        "chamber_id": "SIM_CH_1",
        "reference_type": "fleet_median",
        "reference_id": "v1",
        "tttm_score": 3.2,
        "top_gap_sensor": "C62",
        "gap_pct": 12.5,
        "is_reference_suspect": True,
        "suspect_sensors": json.dumps(["C17"]),      # list → JSONB 바인드 문자열
    }


def test_rollup_to_row_keys_are_ddl_columns():
    """산출 row의 키 == DDL_COLUMNS (id·created_at 등 DB 디폴트는 미생성)."""
    row = rollup_to_row(SAMPLE_ROLLUP)
    assert set(row.keys()) == set(DDL_COLUMNS)
    assert "id" not in row
    assert "created_at" not in row


def test_rollup_to_row_reads_reference_type_from_rollup():
    """reference_type은 하드코딩이 아니라 rollup['reference']에서 취득(헌법 6-1)."""
    other = {**SAMPLE_ROLLUP, "reference": "initial"}
    assert rollup_to_row(other)["reference_type"] == "initial"


def test_rollups_to_rows_drops_none():
    """rollups_to_rows는 None(평가불가 챔버)을 드롭하고 나머지만 변환."""
    second = {**SAMPLE_ROLLUP, "chamber_id": "SIM_CH_2"}
    rows = rollups_to_rows([SAMPLE_ROLLUP, None, second])
    assert len(rows) == 2
    assert [r["chamber_id"] for r in rows] == ["SIM_CH_1", "SIM_CH_2"]


def test_rollups_to_rows_empty():
    """빈·전부-None 입력 → 빈 리스트."""
    assert rollups_to_rows([]) == []
    assert rollups_to_rows([None, None]) == []


# ── Task 2: DB writer (INSERT · no-commit) ─────────────────────────

class _SpyConn:
    """SQLAlchemy Connection 대역 — execute/commit 호출을 기록만 한다(무DB 단위 검증용).

    writer의 계약(벌크 1회 execute·commit 미호출·[]→execute 스킵)을 실 DB 없이 검증한다.
    """

    def __init__(self) -> None:
        self.execute_calls: list = []
        self.commit_calls = 0

    def execute(self, statement, params=None):
        self.execute_calls.append((statement, params))
        return None

    def commit(self) -> None:
        self.commit_calls += 1


def test_write_comparisons_returns_row_count_via_single_bulk_execute():
    """다중 행 → execute 1회(벌크/executemany)로 넘기고 반환값 = 행수."""
    conn = _SpyConn()
    rows = rollups_to_rows([SAMPLE_ROLLUP, {**SAMPLE_ROLLUP, "chamber_id": "SIM_CH_2"}])
    n = write_comparisons(conn, rows)
    assert n == 2
    assert len(conn.execute_calls) == 1          # 행별 루프 금지 — 벌크 1회
    _, params = conn.execute_calls[0]
    assert params == rows                          # rows 전체를 executemany 파라미터로


def test_write_comparisons_empty_skips_execute():
    """rows=[] → execute 안 함·0 반환."""
    conn = _SpyConn()
    assert write_comparisons(conn, []) == 0
    assert conn.execute_calls == []


def test_write_comparisons_does_not_commit():
    """writer는 commit 하지 않는다(트랜잭션 경계는 호출자 소유 — 결정 #5)."""
    conn = _SpyConn()
    write_comparisons(conn, rollups_to_rows([SAMPLE_ROLLUP]))
    assert conn.commit_calls == 0


# ── Task 3: 왕복·롤백 통합 (실 DB — 무DB 환경은 skip) ────────────────

# is_reference_suspect True·False + gap_pct=None 을 모두 포함하는 정적 rollup(왕복 타입 검증).
# 챔버는 TEST_CH_*(실 SIM_CH_* 와 격리) — 라이브 데이터가 tttm_comparisons 에 있어도
# WHERE 로 이 테스트 행만 세도록(아래 롤백 테스트). SIM_CH_* 로 두면 실 행과 섞여 count 가 터진다.
# suspect_sensors: True→비어있지 않은 목록(단일·복수 왕복), False→[] (empty list JSONB 왕복).
_ROUNDTRIP_ROLLUPS = [
    {"reference": "fleet_median", "score": 3.2, "top_gap_sensor": "C62",
     "gap_pct": 12.5, "reference_suspect": True, "suspect_sensors": ["C17"],
     "reference_id": "v1", "chamber_id": "TEST_CH_1"},
    {"reference": "fleet_median", "score": 1.1, "top_gap_sensor": "C11",
     "gap_pct": None, "reference_suspect": False, "suspect_sensors": [],
     "reference_id": "v1", "chamber_id": "TEST_CH_2"},
    {"reference": "fleet_median", "score": 4.7, "top_gap_sensor": "C62",
     "gap_pct": -8.0, "reference_suspect": True, "suspect_sensors": ["C22", "C31"],
     "reference_id": "v1", "chamber_id": "TEST_CH_3"},
]


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip")
def test_roundtrip_and_rollback_leaves_db_untouched():
    """정적 rollup 적재 → SELECT 왕복(값·타입 일치) → 롤백 후 tttm 원복·control_limits 무변경.

    fixture 대신 함수 내 명시적 트랜잭션 — `with connect()`(close 보장) → `begin()` →
    try/finally rollback(성공·실패 무관 항상 rollback, §3-1). writer는 commit 안 하므로
    이 rollback이 삽입을 통째로 되돌린다.
    """
    engine = make_engine()
    rows = rollups_to_rows(_ROUNDTRIP_ROLLUPS)
    expected = {r["chamber_id"]: r for r in rows}

    # 사전 상태(롤백 무결성 대조군) — 별도 커넥션.
    with engine.connect() as conn:
        tttm_before = conn.execute(
            text("SELECT count(*) FROM tttm_comparisons WHERE chamber_id = ANY(:c)"),
            {"c": list(expected.keys())}).scalar()
        cl_before = conn.execute(text("SELECT count(*) FROM control_limits")).scalar()

    with engine.connect() as conn:
        trans = conn.begin()
        try:
            n = write_comparisons(conn, rows)
            assert n == 3

            fetched = conn.execute(
                text(
                    "SELECT chamber_id, reference_type, reference_id, tttm_score, "
                    "top_gap_sensor, gap_pct, is_reference_suspect, suspect_sensors "
                    "FROM tttm_comparisons WHERE chamber_id = ANY(:chambers) "
                    "ORDER BY chamber_id"
                ),
                {"chambers": list(expected.keys())},   # 이 테스트가 넣은 TEST_CH_* 3행만 (실 데이터 격리)
            ).mappings().all()

            assert len(fetched) == 3
            for got in fetched:
                exp = expected[got["chamber_id"]]
                assert got["reference_type"] == exp["reference_type"] == "fleet_median"
                assert got["reference_id"] == exp["reference_id"]
                assert got["tttm_score"] == exp["tttm_score"]
                assert got["top_gap_sensor"] == exp["top_gap_sensor"]
                assert got["gap_pct"] == exp["gap_pct"]  # None(SIM_CH_2)→NULL→None
                # BOOLEAN 왕복: Python bool로 되읽히고 값 일치(True/False 둘 다 포함).
                assert isinstance(got["is_reference_suspect"], bool)
                assert got["is_reference_suspect"] == exp["is_reference_suspect"]
                # JSONB 왕복: list 저장(json.dumps) → JSONB → Python list 되읽기(빈 목록 포함).
                assert isinstance(got["suspect_sensors"], list)
                assert got["suspect_sensors"] == json.loads(exp["suspect_sensors"])
        finally:
            trans.rollback()

    # 롤백 후: writer가 넣은 행은 사라지고(tttm 원복) control_limits는 무변경.
    with engine.connect() as conn:
        tttm_after = conn.execute(
            text("SELECT count(*) FROM tttm_comparisons WHERE chamber_id = ANY(:c)"),
            {"c": list(expected.keys())}).scalar()
        cl_after = conn.execute(text("SELECT count(*) FROM control_limits")).scalar()
    assert tttm_after == tttm_before  # 롤백으로 삽입 3행 원복 (§3-1 — 라이브 직전 비우기 방식)
    assert cl_after == cl_before      # control_limits 불변
