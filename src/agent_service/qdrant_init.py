# -*- coding: utf-8 -*-
"""
Qdrant 컬렉션 초기화 — kb_schema.yaml 의 5컬렉션을 hybrid(dense+sparse)로 생성한다.

db/init.sql 이 Postgres 테이블을 만들듯, 이 스크립트는 RAG 검색용 Qdrant 컬렉션을 만든다.
컬렉션 목록·벡터 스펙·payload 인덱스는 전부 config/kb_schema.yaml 단일 소스에서 읽는다.

실행     : python -m src.agent_service.qdrant_init  [--recreate]
사전조건 : docker compose up -d 로 qdrant 서비스 기동
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.agent_service.vectordb import (
    ensure_collection, get_qdrant_client, is_hybrid, load_schema, physical_name,
)

log = logging.getLogger("qdrant-init")


def main():
    """kb_schema.yaml collections 전체를 Qdrant 에 생성한다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--recreate", action="store_true", help="기존 컬렉션 삭제 후 재생성")
    args = ap.parse_args()

    schema = load_schema()
    client = get_qdrant_client()
    log.info(f"임베딩 mode={schema['embedding']['mode']} · model={schema['embedding']['model']}")
    for logical, coll in schema["collections"].items():
        name = ensure_collection(client, logical, recreate=args.recreate)
        log.info(f"  └ {logical} ({coll.get('kind')}) → {name}")
    log.info(f"완료: {len(schema['collections'])}개 컬렉션 (hybrid={is_hybrid()})")


if __name__ == "__main__":
    main()
