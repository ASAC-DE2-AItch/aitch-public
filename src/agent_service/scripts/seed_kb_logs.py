# -*- coding: utf-8 -*-
"""KB 조치로그 2종 → Postgres 시딩 (C5-2, 2026-07-20)

배경 (2026-07-10 개정 — `config/kb_schema.yaml:9`):
    KB = 근거 서사 + 의미검색(Qdrant) · Postgres = 정밀 수치 + 정확 필터 + 통계 + 감사

`limit_change_log`·`recipe_r2r_log`는 "비슷한 걸 찾는" 대상이 아니라 "그 사건의 수치를 정확히
꺼내는" 대상이라 벡터 KB에서 제외됐다. 대신 `historical_case`(벡터) 히트의 `incident_id`로
아래 두 테이블을 조회해 페어링한다(형제 관계 — HC가 부모가 아님).

    historical_case 벡터 검색 → incident_id → limit_corrections / recipe_corrections
                                              (center/delta·섀도 실적 등 정밀 수치)

입력 : rag_kb/limit_change_log/limit_corrections_from_synthetic_v2.jsonl   (1,775건)
       rag_kb/recipe_r2r_log/recipe_corrections_from_synthetic_v2.jsonl    (648건)
       ※ `derive_postgres_logs.py` 산출 — db 컬럼 형태(직접 INSERT용), 임베딩 없음

성격 : 과거 종결 사례(`status='VERIFIED'`)다. 런타임 권고(`PROPOSED`)·승인 적용(`APPROVED`)과
       같은 테이블을 쓰되 status 로 구분된다. 로컬 개발 DB 기준(각자 자기 Postgres).

멱등 : `*_correction_id` UNIQUE 에 ON CONFLICT DO NOTHING — 여러 번 돌려도 중복 안 쌓인다.
       B 가 데이터를 재생성하면(챔버 6→4 등) JSONL 만 갈아끼우고 이 스크립트를 다시 돌린다.

사용 : python -m agent_service.scripts.seed_kb_logs          (적재)
       python -m agent_service.scripts.seed_kb_logs --check  (건수만 확인)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_service.app.db import connect  # noqa: E402

logger = logging.getLogger("seed_kb_logs")

RAG_KB = Path(__file__).resolve().parents[1] / "rag_kb"

# (JSONL 경로, 테이블, 충돌 판정 컬럼, 적재 컬럼)
SOURCES = [
    (
        RAG_KB / "limit_change_log" / "limit_corrections_from_synthetic_v2.jsonl",
        "limit_corrections",
        "correction_id",
        [
            "correction_id", "incident_id", "chamber_id", "recipe_id", "step",
            "sensor_window", "sensor_id", "limit_version_before", "limit_version_after",
            "center_before", "center_after", "ucl_after", "lcl_after", "delta_pct",
            "trigger_type", "calc_window_n", "status",
            "shadow_false_alarm_reduction_pct", "shadow_missed_detection",
        ],
    ),
    (
        RAG_KB / "recipe_r2r_log" / "recipe_corrections_from_synthetic_v2.jsonl",
        "recipe_corrections",
        "recipe_correction_id",
        [
            "recipe_correction_id", "incident_id", "chamber_id", "recipe_id", "step",
            "parameter_id", "value_before", "value_after", "delta_pct",
            "shap_basis", "rag_evidence", "status", "verify_result", "applied_at",
        ],
    ),
]

# JSONB 컬럼 — dict/list 를 json 문자열로 넘겨야 한다.
_JSONB = {"shap_basis", "rag_evidence", "verify_result"}


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    """JSONL 을 한 줄씩 읽는다. 깨진 줄은 스킵 + 경고 (헌법 6-2 무중단)."""
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("%s:%d 파싱 실패 — 스킵 (%s)", path.name, i, exc)


def _value(row: dict[str, Any], col: str) -> Any:
    """컬럼 값 추출 — JSONB 는 직렬화, 없으면 None."""
    v = row.get(col)
    if col in _JSONB and v is not None and not isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return v


def seed_table(conn, path: Path, table: str, conflict_col: str, cols: list[str]) -> tuple[int, int]:
    """JSONL → 테이블 적재. Returns (읽은 건수, 실제 INSERT 건수)."""
    if not path.exists():
        logger.error("입력 없음: %s", path)
        return 0, 0

    placeholders = ", ".join(["%s"] * len(cols))
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_col}) DO NOTHING"
    )

    read = inserted = 0
    with conn.cursor() as cur:
        for row in _rows(path):
            read += 1
            cur.execute(sql, [_value(row, c) for c in cols])
            inserted += cur.rowcount  # 충돌로 스킵되면 0
    conn.commit()
    logger.info("%-20s 읽음 %5d → INSERT %5d (중복 스킵 %d)", table, read, inserted, read - inserted)
    return read, inserted


def check(conn) -> None:
    """현재 적재 상태만 조회 (적재 안 함)."""
    with conn.cursor() as cur:
        for _, table, _, _ in SOURCES:
            cur.execute(f"SELECT count(*), count(DISTINCT incident_id) FROM {table}")
            total, incidents = cur.fetchone()
            logger.info("%-20s %5d행 / incident %4d종", table, total, incidents)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser(description="KB 조치로그 2종 → Postgres 시딩 (멱등)")
    ap.add_argument("--check", action="store_true", help="적재하지 않고 현재 건수만 확인")
    args = ap.parse_args()

    try:
        with connect() as conn:
            if args.check:
                check(conn)
                return 0
            total_in = 0
            for path, table, conflict, cols in SOURCES:
                total_in += seed_table(conn, path, table, conflict, cols)[1]
            logger.info("완료 — 신규 %d행", total_in)
            check(conn)
    except Exception as exc:
        logger.error("실패: %s", exc)
        logger.error("Postgres 가 떠 있는지 확인: docker compose up -d postgres")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
