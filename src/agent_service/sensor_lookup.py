# -*- coding: utf-8 -*-
"""
[공용] 센서 lookup 보완 — kb_schema.yaml 의 lookup_complement 소비형
================================================================================
목적 : process_knowledge 등 컬렉션이 선언한 lookup_complement(예: sensor_mapping.json)를
       로드해, 검색 결과의 C코드(payload['sensor'])에 표준명·카테고리·비고를 붙인다.
원칙 : 룩업 표는 KB 에 임베딩하지 않는다(센서 오검색 방지 · kb_schema.yaml line 99).
       코드·DB·Kafka 는 C코드(C11 등) 그대로 사용, 표준명은 표시 계층 변환용 (CLAUDE.md 6-4).
파일 : rag_kb/_shared/<lookup_complement 파일명>
단일 소스: config/kb_schema.yaml  (collections.<x>.lookup_complement)
"""
import functools
import json
import logging
from pathlib import Path

# 상대 임포트 — 절대 경로(src.*)는 repo 루트가 sys.path 에 있어야만 해석된다.
# 런타임 경로(app/pipeline → vectordb → 여기)가 실행 위치에 따라 조용히 깨지는 걸 막는다.
from .vectordb import RAG_KB_DIR, load_schema

log = logging.getLogger("sensor-lookup")

SHARED_DIR = RAG_KB_DIR / "_shared"


@functools.lru_cache(maxsize=8)
def _load_lookup_file(filename: str) -> dict:
    """rag_kb/_shared/<filename> 을 로드한다 (파일당 1회 캐시)."""
    path = SHARED_DIR / filename
    if not path.exists():
        log.warning(f"lookup 파일 없음: {path}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def lookup_filename(logical: str) -> str | None:
    """컬렉션이 선언한 lookup_complement 파일명 (없으면 None)."""
    coll = load_schema()["collections"].get(logical, {})
    return coll.get("lookup_complement")


def sensor_map(logical: str = "process_knowledge") -> dict:
    """컬렉션의 lookup_complement 에서 code→info(dict) 매핑을 반환한다 (없으면 {})."""
    fname = lookup_filename(logical)
    if not fname:
        return {}
    data = _load_lookup_file(fname)
    # sensor_mapping.json 은 {"_meta":..., "sensors": {code: info}} 구조
    return data.get("sensors", data) if isinstance(data, dict) else {}


def lookup(code: str, logical: str = "process_knowledge") -> dict | None:
    """C코드 1개의 lookup 정보(dict). 없으면 None."""
    if not code:
        return None
    return sensor_map(logical).get(code)


def standard_name(code: str, logical: str = "process_knowledge") -> str:
    """C코드 → 표준명. 매핑 없으면 코드 자체를 폴백으로 반환 (하드코딩 금지 · 6-4)."""
    info = lookup(code, logical)
    return info.get("name", code) if info else code


def enrich_points(logical: str, points: list) -> list:
    """검색 결과(ScoredPoint 리스트)의 payload['sensor'] C코드에 lookup 정보를 붙인다.

    각 point.payload 에 '_sensor_lookup' 키(표준명·카테고리·단위·비고)를 추가한다.
    lookup_complement 미선언 컬렉션은 원본 그대로 반환 (부작용 없음).
    """
    if not lookup_filename(logical):
        return points
    smap = sensor_map(logical)
    for p in points:
        payload = getattr(p, "payload", None) or {}
        code = payload.get("sensor")
        info = smap.get(code) if code else None
        if info:
            payload["_sensor_lookup"] = {
                "code": code,
                "standard_name": info.get("name"),
                "category": info.get("category"),
                "unit": info.get("unit"),
                "confidence": info.get("confidence"),
                "note": info.get("note"),
            }
    return points


if __name__ == "__main__":
    # 자가 점검 — 모델·Qdrant 불필요 (룩업 파일만 로드)
    logging.basicConfig(level=logging.INFO)
    smap = sensor_map("process_knowledge")
    print(f"lookup 파일: {lookup_filename('process_knowledge')}  ·  센서 {len(smap)}개")
    for code in ["C64", "C11", "C65"]:
        print(f"  {code} → {standard_name(code)}")
