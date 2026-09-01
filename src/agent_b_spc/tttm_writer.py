"""③ TTTM DB writer — 엔진 `chamber_rollup` dict → `tttm_comparisons` 적재.

spec: `specs/B4-2_TTTM_writer_스펙플랜.md`. 엔진 산출 rollup(8키)을 DDL 컬럼으로 변환해
`tttm_comparisons`에 append-only INSERT한다. 스키마 변경 0(DML only, 헌법 3-1/3-2) →
PM 승인 불요. `alert_id` 없음 → alert 채널 무관(헌법 1-2, writer는 발행 안 함).

경계(불변식):
  · writer는 **commit 안 함** — 트랜잭션 경계는 호출자(테스트=rollback / 라이브=commit).
  · `None` rollup(평가불가 챔버)은 **행 생성 안 함**(임계 필터 없음 — 결정 #1 A, 전부 기록).
  · **append-only** — seed의 delete-then-insert(멱등) 패턴은 복사 금지.
  · `control_limits` 불변 — writer는 `tttm_comparisons`만 씀.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# 방출 화이트리스트 = tttm_comparisons DDL의 비-디폴트 컬럼(db/init.sql).
# id/created_at은 DB 디폴트(BIGSERIAL/NOW())라 여기서 만들지 않는다. reference_type은
# DDL에 DEFAULT가 있으나 rollup['reference']에서 명시 취득해 싣는다(헌법 6-1 — 하드코딩 금지).
DDL_COLUMNS = [
    "chamber_id",
    "reference_type",
    "reference_id",
    "tttm_score",
    "top_gap_sensor",
    "gap_pct",
    "is_reference_suspect",
    "suspect_sensors",
]

# 엔진 rollup 키 → DDL 컬럼 매핑(spec 필드 매핑표). 값은 전부 rollup에서 취득(하드코딩 0).
_ROLLUP_TO_DDL = {
    "chamber_id": "chamber_id",
    "reference": "reference_type",
    "reference_id": "reference_id",
    "score": "tttm_score",
    "top_gap_sensor": "top_gap_sensor",
    "gap_pct": "gap_pct",
    "reference_suspect": "is_reference_suspect",
    "suspect_sensors": "suspect_sensors",
}

# JSONB 컬럼(리스트 값) — INSERT 바인드에 CAST(:col AS JSONB), 값은 json.dumps로 직렬화.
#   psycopg2가 Python list를 그대로 바인딩하지 못하므로 문자열로 넘긴다(quals.wafer_ids 선례).
_JSONB_COLUMNS = frozenset({"suspect_sensors"})


def rollup_to_row(rollup: dict) -> dict:
    """엔진 `chamber_rollup` dict(8키) → `tttm_comparisons` INSERT용 row(DDL 컬럼 8개).

    이름 매핑만 수행(reference→reference_type, score→tttm_score,
    reference_suspect→is_reference_suspect 등). id·created_at은 DB 디폴트라 생성하지 않는다.
    값은 전부 rollup에서 취득한다(헌법 6-1 — 'fleet_median' 등 하드코딩 금지).
    `suspect_sensors`(list)는 JSONB 컬럼이라 `json.dumps`로 직렬화한다(_JSONB_COLUMNS·CAST 정합).
    """
    row = {ddl_col: rollup[rollup_key] for rollup_key, ddl_col in _ROLLUP_TO_DDL.items()}
    row["suspect_sensors"] = json.dumps(rollup["suspect_sensors"])   # list → JSONB 바인드 문자열
    return row


def rollups_to_rows(rollups: list[dict | None]) -> list[dict]:
    """rollup 리스트 → INSERT row 리스트. `None`(평가불가 챔버)은 드롭한다.

    None-skip을 여기서 소유하는 단일 진입점(테스트·라이브 공용). 반환 row는 모두 정확히
    8개 non-default 컬럼 키를 가져 executemany 균일 키 요구를 만족한다.
    """
    return [rollup_to_row(r) for r in rollups if r is not None]


def _bind(col: str) -> str:
    """INSERT VALUES 바인드 표현식 — JSONB 컬럼만 CAST, 나머지는 균일 `:col` 바인드."""
    return f"CAST(:{col} AS JSONB)" if col in _JSONB_COLUMNS else f":{col}"


# INSERT 문(DDL_COLUMNS 화이트리스트) — 컬럼·바인드 파라미터를 모듈 로드 시 1회 구성.
#   suspect_sensors만 CAST(:suspect_sensors AS JSONB)(_bind), 나머지는 균일 바인드 유지.
_INSERT_SQL = text(
    "INSERT INTO tttm_comparisons ({cols}) VALUES ({binds})".format(
        cols=", ".join(DDL_COLUMNS),
        binds=", ".join(_bind(col) for col in DDL_COLUMNS),
    )
)


def write_comparisons(conn, rows: list[dict]) -> int:
    """`rows`(rollups_to_rows 결과)를 `tttm_comparisons`에 벌크 INSERT하고 적재 행수를 반환한다.

    **append-only**(결정 #1 A — 전부 기록, seed의 delete-then-insert 멱등 패턴 복사 금지).
    **commit 하지 않는다** — 트랜잭션 경계는 호출자 소유(테스트=rollback / 라이브=commit,
    결정 #5). `conn`은 이미 트랜잭션이 열린 SQLAlchemy Connection. 모든 row는 정확히 8개
    non-default 컬럼 키를 가져(rollups_to_rows 보장) executemany 균일 키 요구를 만족한다.
    `rows=[]`이면 execute를 건너뛰고 0을 반환한다.
    """
    if not rows:
        return 0
    conn.execute(_INSERT_SQL, rows)  # 벌크(executemany) 1회 — 행별 루프 금지
    logger.info("tttm_comparisons 적재 %d행(commit은 호출자 몫)", len(rows))
    return len(rows)
