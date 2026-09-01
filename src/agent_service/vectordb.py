# -*- coding: utf-8 -*-
"""
[공용] Qdrant 벡터 DB 코어 유틸리티 — kb_schema.yaml 소비형 (agent_service RAG)
================================================================================
단일 소스 : config/kb_schema.yaml (KB 5컬렉션 계약)
임베딩    : BAAI/bge-m3 hybrid(dense + sparse lexical weights) — FlagEmbedding
컬렉션명  : <logical>__<model_slug>__<version>  (모델/청킹 교체 = 새 컬렉션 · CLAUDE.md 4-2)

무거운 의존성(FlagEmbedding·qdrant_client)은 지연 import 한다 —
스키마 로딩/레코드 검증 계층은 모델·DB 없이도 단독으로 쓸 수 있어야 하므로.
"""
import functools
import logging
import os
import sys
import uuid
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if getattr(_stream, "encoding", None) and _stream.encoding.lower() != "utf-8":
        try:
            _stream.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949) 한글 로그 깨짐 방지
        except Exception:
            pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ [vectordb] %(message)s")
log = logging.getLogger("vectordb")

SCHEMA_PATH = Path(__file__).resolve().parent / "config" / "kb_schema.yaml"
RAG_KB_DIR = Path(__file__).resolve().parent / "rag_kb"
DEFAULT_QDRANT_URL = "http://localhost:6333"


# ── 스키마 로딩 ───────────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def load_schema() -> dict:
    """config/kb_schema.yaml 을 로드한다 (프로세스당 1회 캐시)."""
    import yaml
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def embedding_cfg() -> dict:
    """embedding 섹션 (mode/model/text_field/registry/qdrant 벡터명)."""
    return load_schema()["embedding"]


def is_hybrid() -> bool:
    return embedding_cfg().get("mode", "dense") == "hybrid"


def model_slug(model: str = None) -> str:
    """모델명 → 컬렉션 물리명 슬러그 (예: 'BAAI/bge-m3' → 'bge_m3')."""
    model = model or embedding_cfg()["model"]
    return model.split("/")[-1].replace("-", "_").replace(".", "_").lower()


def physical_name(logical: str, model: str = None, version: str = None) -> str:
    """논리 컬렉션명 → 물리 컬렉션명 (<logical>__<slug>__<version>)."""
    version = version or load_schema().get("version", "v1")
    return f"{logical}__{model_slug(model)}__{version}"


def model_spec(model: str = None) -> dict:
    """registry에서 모델별 벡터 스펙(dense_dims/distance/sparse)."""
    model = model or embedding_cfg()["model"]
    reg = embedding_cfg()["registry"]
    if model not in reg:
        raise KeyError(f"kb_schema.yaml embedding.registry 에 모델 없음: {model}")
    return reg[model]


# ── 레코드 검증 (모델·DB 불필요) ──────────────────────────────────────────────
def _enum_field_map(logical: str) -> dict:
    """컬렉션의 (공통+전용) payload 중 type=='enum' 인 필드 → enum 이름 매핑."""
    schema = load_schema()
    out = {}
    for fld, spec in schema.get("common_payload", {}).items():
        if isinstance(spec, dict) and spec.get("type") == "enum":
            out[fld] = spec["enum"]
    coll = schema["collections"][logical]
    for fld, spec in (coll.get("payload") or {}).items():
        if isinstance(spec, dict) and spec.get("type") == "enum":
            out[fld] = spec["enum"]
    return out


def validate_record(logical: str, rec: dict) -> list:
    """레코드 1건을 kb_schema 계약에 검증한다. 위반 사유 리스트(빈 리스트=통과) 반환.

    검사: ① 공통 payload 필수 키 존재(nullable:true 필드는 선택 — 컬렉션별 미보유 허용),
          ② kb_type == 컬렉션명, ③ enum 필드 값이 해당 enum 목록 내(null 은 통과).
    """
    schema = load_schema()
    errors = []
    for fld, spec in schema.get("common_payload", {}).items():
        # nullable:true 는 특정 컬렉션 전용(예: historical_case는 equipment/sensor 미보유) → 부재 허용
        if fld not in rec and not (isinstance(spec, dict) and spec.get("nullable")):
            errors.append(f"공통 필드 누락: {fld}")
    if rec.get("kb_type") != logical:
        errors.append(f"kb_type 불일치: {rec.get('kb_type')} != {logical}")
    enums = schema.get("enums", {})
    for fld, enum_name in _enum_field_map(logical).items():
        val = rec.get(fld)
        if val is not None and val not in enums.get(enum_name, []):
            errors.append(f"enum 위반 {fld}={val!r} (허용: {enum_name})")
    return errors


# ── Qdrant 클라이언트 ─────────────────────────────────────────────────────────
def get_qdrant_client():
    """Qdrant 클라이언트. 환경변수 QDRANT_PATH 있으면 인프로세스 로컬 모드(docker 불필요),
    없으면 QDRANT_URL(기본 localhost:6333) 서버 모드."""
    from qdrant_client import QdrantClient
    path = os.environ.get("QDRANT_PATH")
    if path:
        return QdrantClient(path=path)
    return QdrantClient(url=os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL))


# ── bge-m3 hybrid 임베딩 ──────────────────────────────────────────────────────
@functools.lru_cache(maxsize=2)
def _bgem3(model: str):
    """BGEM3FlagModel 지연 로드 (모델당 1회)."""
    from FlagEmbedding import BGEM3FlagModel
    log.info(f"임베딩 모델 로드: {model}")
    return BGEM3FlagModel(model, use_fp16=True)


def embed_hybrid(texts, model: str = None) -> list:
    """텍스트 리스트 → [{'dense': [float×1024], 'sparse': {int: float}}, ...].

    mode=dense 면 sparse 는 빈 dict. mode=hybrid 면 bge-m3 lexical weights 를 sparse 로.
    """
    if isinstance(texts, str):
        texts = [texts]
    model = model or embedding_cfg()["model"]
    hybrid = is_hybrid()
    out = _bgem3(model).encode(
        list(texts), return_dense=True, return_sparse=hybrid, return_colbert_vecs=False,
    )
    dense = [v.tolist() for v in out["dense_vecs"]]
    if not hybrid:
        return [{"dense": d, "sparse": {}} for d in dense]
    sparse = [{int(k): float(w) for k, w in lw.items()} for lw in out["lexical_weights"]]
    return [{"dense": d, "sparse": s} for d, s in zip(dense, sparse)]


def _to_sparse_vector(sparse: dict):
    """{token_id: weight} → qdrant SparseVector."""
    from qdrant_client.models import SparseVector
    return SparseVector(indices=list(sparse.keys()), values=list(sparse.values()))


# ── 컬렉션 생성 (hybrid + payload 인덱스) ─────────────────────────────────────
def ensure_collection(client, logical: str, recreate: bool = False) -> str:
    """논리 컬렉션을 kb_schema 대로 생성한다 (dense + (hybrid면)sparse + payload 인덱스).

    이미 있으면 스킵(recreate=True 면 삭제 후 재생성). 물리 컬렉션명을 반환한다.
    """
    from qdrant_client.models import (
        Distance, VectorParams, SparseVectorParams, PayloadSchemaType,
    )
    name = physical_name(logical)
    if client.collection_exists(name):
        if not recreate:
            log.info(f"컬렉션 존재 — 스킵: {name}")
            return name
        client.delete_collection(name)
        log.info(f"컬렉션 삭제 후 재생성: {name}")

    spec = model_spec()
    ecfg = embedding_cfg()
    dense_name = ecfg["qdrant"]["dense_vector_name"]
    distance = getattr(Distance, spec["distance"].upper())
    vectors_config = {dense_name: VectorParams(size=spec["dense_dims"], distance=distance)}
    sparse_config = None
    if is_hybrid():
        sparse_config = {ecfg["qdrant"]["sparse_vector_name"]: SparseVectorParams()}

    client.create_collection(name, vectors_config=vectors_config, sparse_vectors_config=sparse_config)
    for fld in load_schema().get("payload_indexes", []):
        client.create_payload_index(name, field_name=fld, field_schema=PayloadSchemaType.KEYWORD)
    log.info(f"컬렉션 생성: {name} (hybrid={is_hybrid()}, 인덱스 {len(load_schema().get('payload_indexes', []))}개)")
    return name


# ── 적재 ─────────────────────────────────────────────────────────────────────
def _point_id(rec: dict) -> str:
    """chunk_id(없으면 doc_id) 기반 결정적 UUID — 재적재 시 동일 point 덮어쓰기."""
    key = str(rec.get("chunk_id") or rec.get("doc_id") or rec)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def upsert_records(client, logical: str, records: list, batch_size: int = 64) -> int:
    """레코드 리스트를 임베딩(text 필드)하여 물리 컬렉션에 upsert. 적재 건수 반환."""
    from qdrant_client.models import PointStruct
    name = physical_name(logical)
    ecfg = embedding_cfg()
    dense_name, sparse_name = ecfg["qdrant"]["dense_vector_name"], ecfg["qdrant"]["sparse_vector_name"]
    text_field = ecfg.get("text_field", "text")
    total = 0
    for i in range(0, len(records), batch_size):
        chunk = records[i:i + batch_size]
        embs = embed_hybrid([r[text_field] for r in chunk])
        points = []
        for rec, emb in zip(chunk, embs):
            vec = {dense_name: emb["dense"]}
            if is_hybrid() and emb["sparse"]:
                vec[sparse_name] = _to_sparse_vector(emb["sparse"])
            points.append(PointStruct(id=_point_id(rec), vector=vec, payload=rec))
        client.upsert(collection_name=name, points=points)
        total += len(points)
    log.info(f"적재 완료: {name} +{total}건")
    return total


# ── 검색 (hybrid RRF fusion) ──────────────────────────────────────────────────
def dense_search(client, logical: str, query: str, limit: int = 5, query_filter=None):
    """dense 벡터만으로 검색 (sparse 제외). hybrid_search 와 동일 인덱스에서 dense-only 비교용."""
    name = physical_name(logical)
    dense_name = embedding_cfg()["qdrant"]["dense_vector_name"]
    emb = embed_hybrid([query])[0]
    res = client.query_points(name, query=emb["dense"], using=dense_name,
                              limit=limit, with_payload=True, query_filter=query_filter)
    return res.points


#: RRF 융합에 넣을 후보 수 — **`limit` 과 분리한 고정값** (2026-08-06).
#
# ⚠️ 전에는 `limit * 3` 이었다. 그래서 `limit` 을 바꾸면 **융합 풀이 같이 바뀌어 상위 순위 자체가
#    달라졌다** (실측: 같은 질의에 limit=3 → `PK-TUNE-PLASMA` 1위 / limit=10 → `PK-SENSOR-C11` 1위).
#    즉 *"top_k 를 3→5 로 올려보는 실험"*(D18)은 top_k 만 바꾸는 실험이 아니었다 — 검색 결과를
#    함께 바꿔놓고 그 차이를 top_k 탓으로 읽게 되어 있었다. **한 번에 한 변수만 바꾸려면 끊어야 한다.**
#
# 고정하면 얻는 것: ⓐ 프롬프트에 몇 장을 싣든 **상위 순위가 동일** ⓑ 그래서 `hit@3` 와 `hit@10` 을
#    *같은 검색 결과*를 자른 것으로 비교할 수 있다(rerank 판단 = 그 차이) ⓒ top_k 실험이 순수해진다.
#
# 값은 `config/params.yaml` `agent.rag_fusion_pool` (6-4 — 매직 넘버는 전부 params).
#   기본 30 = 계측 폭 10의 3배(옛 비율 유지). 늘려도 프롬프트 예산과 무관하다 — 자르는 건 호출자다.
#   ⚠️ config 를 못 읽어도 검색이 죽지 않아야 한다(KB 는 무중단이 원칙) — 실패 시 30 으로 진행한다.
_FUSION_POOL_DEFAULT = 30


def _fusion_pool() -> int:
    """RRF 융합 후보 수 — params 우선, 실패 시 기본값."""
    try:
        from .app.config import load_settings

        return int(load_settings().require("agent.rag_fusion_pool"))
    except Exception:  # noqa: BLE001 — 근거 검색은 무중단(6-2)
        return _FUSION_POOL_DEFAULT


def hybrid_search(client, logical: str, query: str, limit: int = 5, query_filter=None):
    """dense+sparse 검색 후 RRF 융합 (mode=dense 면 dense 단독). ScoredPoint 리스트 반환.

    융합 후보 수는 `limit` 과 무관한 고정값(`_FUSION_POOL`)이다 — 위 주석 참조.
    """
    from qdrant_client.models import Prefetch, FusionQuery, Fusion
    name = physical_name(logical)
    ecfg = embedding_cfg()
    dense_name, sparse_name = ecfg["qdrant"]["dense_vector_name"], ecfg["qdrant"]["sparse_vector_name"]
    emb = embed_hybrid([query])[0]
    if not is_hybrid() or not emb["sparse"]:
        res = client.query_points(name, query=emb["dense"], using=dense_name,
                                  limit=limit, with_payload=True, query_filter=query_filter)
        return res.points
    pool = max(limit, _fusion_pool())   # limit 이 풀보다 크면 그만큼은 봐야 한다
    res = client.query_points(
        name,
        prefetch=[
            Prefetch(query=emb["dense"], using=dense_name, limit=pool),
            Prefetch(query=_to_sparse_vector(emb["sparse"]), using=sparse_name, limit=pool),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        # ⚠️ **자르는 것은 우리가 한다** — Qdrant 에 limit 을 주면 동점 구간에서 *어느 것을 남길지*를
        #    Qdrant 가 정하고, 그 선택이 limit 값에 따라 달라진다(실측: limit=3 과 10 의 3번째가 다름).
        #    풀 전체를 받아 아래에서 결정적으로 정렬한 뒤 자르면 **limit 과 무관하게 상위가 고정**된다.
        limit=pool, with_payload=True, query_filter=query_filter,
    )
    return _break_ties(res.points)[:limit]


def _tie_key(p):
    """동점 정렬 키 — 점수 내림차순, 같으면 **안정된 문자열**로 가른다.

    payload 식별자를 먼저 쓰고(적재 내용에 묶이므로 재적재해도 같은 순서), 없으면 point id.
    """
    pl = getattr(p, "payload", None) or {}
    ident = pl.get("chunk_id") or pl.get("doc_id") or getattr(p, "id", "")
    return (-(getattr(p, "score", 0.0) or 0.0), str(ident))


def _break_ties(points):
    """🔴 **검색을 재현 가능하게 만든다** (2026-08-06 실측 발견).

    RRF 융합 점수는 **동점이 매우 흔하다** — 한 리스트에만 1위로 잡히면 전부 같은 값이 되어,
    실측에서 error_manual 상위 3건이 전부 `0.5` 였다. 그 구간의 순서를 Qdrant 는 보장하지 않고,
    **같은 질의를 같은 `limit` 으로 두 번 돌리면 순서가 달라졌다**(집합·점수는 동일).

    왜 문제인가: 그 순서가 그대로 **프롬프트에 실리는 근거 카드의 순서**다. 입력이 매 실행
    달라지면 `llm.seed` 를 42로 고정해도 출력이 달라진다 — *"같은 조건 재실행인데 5.4%p 흔들린다"*
    (round16 83.8% ↔ round17 78.4%)의 유력한 후보다. 이걸 고정해야 남은 흔들림이 **정말로 LLM
    쪽인지** 가려진다. 재현되지 않는 자로는 무엇을 재도 소용이 없다.

    집합과 점수는 건드리지 않는다 — **순서만** 결정적으로 만든다.
    """
    return sorted(points or [], key=_tie_key)


def fetch_knob_map(client) -> list:
    """튜닝 손잡이 매핑표(SHAP 축 → 조정 가능 손잡이 C코드) 전체를 반환한다.

    process_knowledge 의 card_type='tuning_axis' 카드(현 5장: 온도·가스·압력·플라즈마·식각시간)를
    **검색이 아니라 전수 조회**한다 — 레시피 조수 규칙4("SHAP 상위가 knob_map에 '없으면' 레짐
    신호")는 목록 전체를 봐야 '없음'을 확정할 수 있기 때문(부분 검색이면 누락과 구분 불가).
    C6-3 "SHAP 축→손잡이 변환의 단일 소스"이므로 Qdrant(적재 KB)를 유일 소스로 읽는다.

    반환: 축별 요약 dict 리스트 — LLM 규칙4 판정에 필요한 필드만 추린다(전체 26필드 X).
      [{axis, knob_param, related_sensors, cause_effect, confidence, doc_id, note}, ...]

    ⚠️ **related_sensors·cause_effect 는 빼면 안 된다** (2026-07-21 실측):
       SHAP 은 **측정 센서**(C15/C16/C31…)를 지목하는데 knob_param 에는 **setpoint 만** 있다.
       둘을 잇는 정보를 빼고 보내면 LLM 이 "SHAP 상위가 knob_map 에 없음 → 레짐 신호"로
       오판한다 — Round4 에서 process_shift 0/5 의 직접 원인이었다(카드엔 있는데 안 실어 보냄).
    """
    from qdrant_client import models
    name = physical_name("process_knowledge")
    pts, _ = client.scroll(
        collection_name=name,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="card_type", match=models.MatchValue(value="tuning_axis"))]
        ),
        limit=64, with_payload=True, with_vectors=False,  # 64 = 축 카드 상한 여유(현 5)
    )
    cards = []
    for p in pts:
        pl = p.payload
        cards.append({
            "axis": pl.get("shap_axis"),          # 온도/가스/압력/플라즈마·이온에너지/식각 시간
            "knob_param": pl.get("knob_param"),   # 실제 손잡이 C코드(list/str) 또는 '미확정'
            # ↓ SHAP(측정) ↔ 손잡이(setpoint) 를 잇는 두 필드 — 빼면 규칙4 오판(위 docstring)
            "related_sensors": pl.get("related_sensors"),  # 그 축에 속한 센서(측정 포함: C15·C16·C31…)
            "cause_effect": pl.get("cause_effect"),        # "SHAP 측정 기여 → setpoint 조정" 한 줄
            "confidence": pl.get("confidence_grade"),  # ✅/🔶 — 미확정 축 판단 보조
            "doc_id": pl.get("doc_id"),           # 근거 인용용 [KB:PK-TUNE-…]
            # ⚠️ basis_note(≈750자)는 cause_effect 와 내용이 겹쳐 제외한다.
            #    recipe 는 재료가 가장 많은 tool 이라(knob_map+kb_hits+cases+recipe_logs)
            #    vLLM max_model_len(8192) 을 넘겨 400 이 났다(2026-07-21 실측: recipe 18/25 fallback).
        })
    cards.sort(key=lambda c: c.get("doc_id") or "")  # 결정적 순서(프롬프트 재현성)
    return cards


def search_kb(client, logical: str, query: str, limit: int = 5, query_filter=None, enrich: bool = True):
    """Agent 검색 진입점 — hybrid_search 후 컬렉션 lookup_complement 로 결과를 보강한다.

    process_knowledge 처럼 lookup_complement(sensor_mapping.json)를 선언한 컬렉션은
    각 결과 payload 에 '_sensor_lookup'(C코드→표준명·카테고리·비고)이 붙는다.
    lookup 미선언 컬렉션은 hybrid_search 와 동일하게 동작한다.
    """
    points = hybrid_search(client, logical, query, limit=limit, query_filter=query_filter)
    if enrich:
        # 상대 임포트 — 절대 경로(src.*)는 repo 루트가 sys.path 에 있어야만 해석되어
        # 실행 위치에 따라 조용히 깨진다(2026-07-20 실측: KB 근거가 통째로 빠짐).
        from .sensor_lookup import enrich_points
        points = enrich_points(logical, points)
    return points
