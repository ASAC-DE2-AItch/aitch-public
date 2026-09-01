# -*- coding: utf-8 -*-
"""
Qdrant 적재 — rag_kb/<컬렉션>/*.jsonl 문서를 검증·임베딩하여 컬렉션에 적재한다.

각 컬렉션 폴더의 *.jsonl 을 소스로 삼아(예: historical_case/historical_cases_seed.jsonl),
kb_schema.yaml 계약(공통 payload 14필드 존재 + enum 값)으로 레코드를 검증한 뒤,
text 필드를 bge-m3 hybrid(dense+sparse)로 임베딩해 upsert 한다.

실행     : python -m src.agent_service.qdrant_ingest  [--collection historical_case] [--strict]
사전조건 : docker compose up -d (qdrant) + qdrant_init.py 로 컬렉션 생성
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.agent_service.vectordb import (
    RAG_KB_DIR, get_qdrant_client, load_schema, upsert_records, validate_record,
)

log = logging.getLogger("qdrant-ingest")


def _iter_jsonl(path: Path):
    """JSONL 을 한 줄씩 파싱한다. 역직렬화 실패 줄은 스킵+로그 (파이프라인 보호 · 헌법 6-2)."""
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning(f"역직렬화 실패, 스킵: {path.name}:{ln} — {e}")


def load_and_validate(logical: str, strict: bool) -> list:
    """rag_kb/<logical>/*.jsonl 을 로드·검증한다. strict 면 위반 레코드 제외."""
    coll_dir = RAG_KB_DIR / logical
    files = sorted(coll_dir.glob("*.jsonl")) if coll_dir.exists() else []
    if not files:
        log.warning(f"소스 JSONL 없음, 스킵: {coll_dir}")
        return []
    records, bad = [], 0
    for fp in files:
        for rec in _iter_jsonl(fp):
            errs = validate_record(logical, rec)
            if errs:
                bad += 1
                log.warning(f"검증 위반 {logical} {rec.get('doc_id') or rec.get('chunk_id')}: {errs[:3]}")
                if strict:
                    continue
            records.append(rec)
    log.info(f"{logical}: {len(records)}건 로드 (위반 {bad}건, strict={strict}) ← {[f.name for f in files]}")
    return records


def main():
    """지정 컬렉션(기본: 전체)의 JSONL 을 검증·임베딩·적재한다."""
    schema = load_schema()
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", choices=list(schema["collections"]), help="특정 컬렉션만 (기본 전체)")
    ap.add_argument("--strict", action="store_true", help="검증 위반 레코드 적재 제외")
    args = ap.parse_args()

    targets = [args.collection] if args.collection else list(schema["collections"])
    client = get_qdrant_client()
    grand = 0
    for logical in targets:
        records = load_and_validate(logical, args.strict)
        if records:
            grand += upsert_records(client, logical, records)
    log.info(f"전체 적재 완료: {grand}건")


if __name__ == "__main__":
    main()
