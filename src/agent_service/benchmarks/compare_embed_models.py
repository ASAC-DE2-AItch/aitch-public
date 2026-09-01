# -*- coding: utf-8 -*-
"""
임베딩 모델 비교 하네스 — 골든셋으로 정확도(hit@K·MRR) + 속도(docs/sec·query latency) 측정.

집(RTX3070 등)에서 3모델을 공정 비교하기 위한 세팅. 로컬모드(docker 불필요)로 돌아가며,
QDRANT_URL 주면 서버모드(도커, payload 인덱스 유효)로도 동작.

모델별 백엔드:
  · bge_m3_hybrid : BAAI/bge-m3, dense+sparse(RRF) — 운영 구성(hybrid)
  · bge_m3_dense  : BAAI/bge-m3, dense only — dense 순수 비교 기준
  · korpatent     : sttempler/KORPatent-BGE, dense only (bge-m3 fine-tune, sparse 없음)
  · qwen3         : Qwen/Qwen3-Embedding-4B(기본), dense only — 큰 모델(집 GPU 필요, 차원 큼)

실행 예:
  # 3모델 전부 (bge-m3는 hybrid+dense 둘 다)
  python -m src.agent_service.compare_embed_models --models bge_m3_hybrid,bge_m3_dense,korpatent,qwen3
  # Qwen3 변형 지정
  python -m src.agent_service.compare_embed_models --models qwen3 --qwen-id Qwen/Qwen3-Embedding-8B

출력: 콘솔 표 + <report> 마크다운(정확도·속도·오답 예시) + results.json
사전: qwen3는 large 모델이라 VRAM 필요(4B≈2560dim, 8B≈4096dim). bge-m3/KORPatent는 1024dim.
"""
import argparse
import json
import logging
import time
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ [compare] %(message)s")
for _n in ("httpx", "huggingface_hub", "urllib3", "sentence_transformers", "datasets", "qdrant_client", "FlagEmbedding"):
    logging.getLogger(_n).setLevel(logging.ERROR)
log = logging.getLogger("compare")

BASE = Path(__file__).resolve().parent
RK = BASE / "rag_kb"
from src.agent_service.eval_golden_set import GOLDEN, _is_relevant  # noqa: E402

# kb_schema 벡터 3컬렉션 (2026-07-10) — 각 rag_kb/<coll>/*.jsonl 전부 대상.
#   로그(limit_change_log·recipe_r2r_log)는 Postgres 전용 → 벡터 벤치 대상 아님.
COLLECTIONS = ["error_manual", "process_knowledge", "historical_case"]

REGISTRY = {
    "bge_m3_hybrid": {"hf": "BAAI/bge-m3", "backend": "hybrid"},
    "bge_m3_dense": {"hf": "BAAI/bge-m3", "backend": "dense"},
    "korpatent": {"hf": "sttempler/KORPatent-BGE", "backend": "dense"},
    # Qwen3-Embedding: 검색 시 query에 instruction 프리픽스 권장
    "qwen3": {"hf": "Qwen/Qwen3-Embedding-4B", "backend": "dense",
              "query_prompt": "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: "},
}
K = 10


def _load(paths):
    """컬렉션의 jsonl 파일들(리스트)을 모두 로드한다."""
    recs = []
    for p in paths:
        recs += [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    return recs


def _pid(r):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, r.get("chunk_id") or r.get("doc_id")))


def _active_sources():
    """컬렉션 → rag_kb/<coll>/*.jsonl 리스트. jsonl 없는 컬렉션은 자동 제외."""
    out = {}
    for kb in COLLECTIONS:
        files = sorted((RK / kb).glob("*.jsonl"))
        if files:
            out[kb] = files
    return out


def _device():
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


# ── dense 백엔드 (SentenceTransformer) ────────────────────────────────────────
def run_dense(slug, spec, device, local_root, batch_size=32):
    import torch
    from sentence_transformers import SentenceTransformer
    from qdrant_client import QdrantClient
    from qdrant_client.models import VectorParams, Distance, PointStruct

    # 큰 모델(Qwen3-4B/8B)은 fp16으로 로드해야 GPU VRAM에 들어감
    mkw = {"torch_dtype": torch.float16} if device == "cuda" else {}
    st = SentenceTransformer(spec["hf"], device=device, trust_remote_code=True, model_kwargs=mkw)
    dim = st.get_embedding_dimension() if hasattr(st, "get_embedding_dimension") else st.get_sentence_embedding_dimension()
    qprompt = spec.get("query_prompt", "")
    client = QdrantClient(path=str(local_root / f"cmp_{slug}"))

    docs, embed_t = 0, 0.0
    for kb, path in _active_sources().items():
        recs = _load(path)
        if client.collection_exists(kb):
            client.delete_collection(kb)
        client.create_collection(kb, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        t = time.time()
        vecs = st.encode([r["text"] for r in recs], batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False)
        embed_t += time.time() - t
        docs += len(recs)
        client.upsert(kb, [PointStruct(id=_pid(r), vector=v.tolist(), payload=r) for r, v in zip(recs, vecs)])

    rows, qlat = _score(lambda kb, q: _dense_search(client, st, kb, qprompt + q))
    del st
    return {"dim": dim, "docs": docs, "docs_per_sec": docs / embed_t, "query_ms": qlat, "rows": rows}


def _dense_search(client, st, kb, query):
    qv = st.encode([query], normalize_embeddings=True)[0].tolist()
    return client.query_points(kb, query=qv, limit=K, with_payload=True).points


# ── hybrid 백엔드 (bge-m3 dense+sparse, RRF) ─────────────────────────────────
def run_hybrid(slug, spec, device, local_root, batch_size=32):
    import os
    os.environ["QDRANT_PATH"] = str(local_root / f"cmp_{slug}")
    from src.agent_service import vectordb as V
    client = V.get_qdrant_client()

    docs, embed_t = 0, 0.0
    for kb, path in _active_sources().items():
        recs = _load(path)
        V.ensure_collection(client, kb, recreate=True)
        t = time.time()
        V.upsert_records(client, kb, recs, batch_size=batch_size)
        embed_t += time.time() - t
        docs += len(recs)

    rows, qlat = _score(lambda kb, q: V.hybrid_search(client, kb, q, limit=K))
    return {"dim": V.model_spec()["dense_dims"], "docs": docs, "docs_per_sec": docs / embed_t, "query_ms": qlat, "rows": rows}


# ── 공통 채점 ─────────────────────────────────────────────────────────────────
def _score(search_fn):
    rows, lat = [], []
    active = set(_active_sources())
    for kb, query, gold in GOLDEN:
        if kb not in active:
            continue
        t = time.time()
        pts = search_fn(kb, query)
        lat.append((time.time() - t) * 1000)
        rank = next((i for i, p in enumerate(pts, 1) if _is_relevant(gold, p.payload or {})), 0)
        rows.append({"kb": kb, "query": query, "gold": gold, "rank": rank,
                     "hit@1": int(0 < rank <= 1), "hit@3": int(0 < rank <= 3), "hit@5": int(0 < rank <= 5),
                     "rr": (1 / rank if rank else 0.0),
                     "top3": [{"id": (p.payload.get("chunk_id") or p.payload.get("case_id") or p.payload.get("correction_id") or p.payload.get("recipe_correction_id")),
                               "rel": _is_relevant(gold, p.payload or {}),
                               "snip": (p.payload.get("text") or "")[:90]} for p in pts[:3]]})
    return rows, (sum(lat) / len(lat) if lat else 0.0)


def _agg(rows, kb=None):
    r = [x for x in rows if kb is None or x["kb"] == kb]
    n = len(r) or 1
    return {m: sum(x[m] for x in r) / n for m in ("hit@1", "hit@3", "hit@5", "rr")}


def _report(results, device, path):
    kbs = list(_active_sources())
    lines = [
        f"# 임베딩 모델 비교 리포트\n",
        f"- device: **{device}** · 골든셋 {sum(1 for g in GOLDEN if g[0] in set(kbs))}문항 · top-{K}\n",
        "## 지표 설명\n",
        "- **dim**: 벡터 차원 수 (낮을수록 저장·메모리·검색 부담 ↓)",
        "- **docs/sec**: 초당 임베딩 문서 수 = 적재 속도 (↑ 좋음)",
        "- **query ms**: 질의 1건당 검색 지연(ms) (↓ 좋음)",
        "- **hit@1/3/5**: 상위 1/3/5위 안에 정답이 있는 질의 비율 (0~1, ↑ 좋음)",
        "- **MRR**: 평균 역순위 — 정답 순위 종합(1위=1.0·2위=0.5·3위=0.33…). 대표 정확도 지표",
        "- 선택 원칙: 정확도(hit@k·MRR) 비슷하면 비용지표(dim↓·docs/sec↑·query ms↓) 우수한 모델\n",
        "## 종합\n",
        "| 모델 | dim | docs/sec | query ms | hit@1 | hit@3 | hit@5 | MRR |", "|---|---|---|---|---|---|---|---|"]
    for slug, r in results.items():
        a = _agg(r["rows"])
        lines.append(f"| {slug} | {r['dim']} | {r['docs_per_sec']:.1f} | {r['query_ms']:.1f} | {a['hit@1']:.2f} | {a['hit@3']:.2f} | {a['hit@5']:.2f} | {a['rr']:.3f} |")
    lines.append("\n## 컬렉션별 hit@3\n")
    lines.append("| 컬렉션 | " + " | ".join(results) + " |")
    lines.append("|---|" + "---|" * len(results))
    for kb in kbs:
        lines.append(f"| {kb} | " + " | ".join(f"{_agg(r['rows'], kb)['hit@3']:.2f}" for r in results.values()) + " |")
    # 오답 예시: 어느 모델이든 top-5 밖인 질의
    lines.append("\n## 오답 예시 (어느 모델이든 top-5 안에 정답 없음)\n")
    any_slug = next(iter(results))
    for i, row in enumerate(results[any_slug]["rows"]):
        q = row["query"]
        if all(0 < results[s]["rows"][i]["rank"] <= 5 for s in results):
            continue
        lines.append(f"### [{row['kb']}] {q}")
        lines.append(f"- 기대(gold): `{row['gold']}`")
        for s in results:
            rr = results[s]["rows"][i]
            verdict = f"rank {rr['rank']}" if rr["rank"] else "정답 없음"
            lines.append(f"- **{s}** ({verdict}):")
            for t in rr["top3"]:
                mark = "✅" if t["rel"] else "❌"
                lines.append(f"    - {mark} `{t['id']}` — {t['snip']}")
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    log.info(f"리포트 저장: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="bge_m3_hybrid,bge_m3_dense,korpatent,qwen3")
    ap.add_argument("--qwen-id", default=None, help="Qwen3 모델 id 오버라이드 (예: Qwen/Qwen3-Embedding-8B)")
    ap.add_argument("--batch-size", type=int, default=32, help="임베딩 배치 (VRAM 부족 시 8/16으로 낮춤)")
    ap.add_argument("--local-root", default=str(BASE.parent.parent / ".cmp_qdrant"), help="로컬 qdrant 루트")
    ap.add_argument("--report", default=str(BASE.parent.parent / "model_compare_report.md"))
    ap.add_argument("--dump", default=str(BASE.parent.parent / "model_compare_results.json"))
    args = ap.parse_args()

    device = _device()
    local_root = Path(args.local_root)
    local_root.mkdir(parents=True, exist_ok=True)
    log.info(f"device={device} · 대상 컬렉션={list(_active_sources())}")

    results = {}
    for slug in [s.strip() for s in args.models.split(",") if s.strip()]:
        spec = dict(REGISTRY[slug])
        if slug == "qwen3" and args.qwen_id:
            spec["hf"] = args.qwen_id
        log.info(f">>> {slug} ({spec['hf']}, {spec['backend']})")
        runner = run_hybrid if spec["backend"] == "hybrid" else run_dense
        results[slug] = runner(slug, spec, device, local_root, args.batch_size)
        r = results[slug]
        log.info(f"    {r['docs']}건 · {r['docs_per_sec']:.1f} docs/sec · query {r['query_ms']:.1f}ms · dim {r['dim']}")

    print("\n" + "=" * 74)
    print(f" 모델 비교 · {device} · 골든셋 (top-{K})")
    print("=" * 74)
    print(f"{'모델':<16}{'dim':>6}{'docs/s':>9}{'q_ms':>7}{'hit@1':>8}{'hit@3':>8}{'hit@5':>8}{'MRR':>7}")
    print("-" * 74)
    for slug, r in results.items():
        a = _agg(r["rows"])
        print(f"{slug:<16}{r['dim']:>6}{r['docs_per_sec']:>9.1f}{r['query_ms']:>7.1f}{a['hit@1']:>8.2f}{a['hit@3']:>8.2f}{a['hit@5']:>8.2f}{a['rr']:>7.3f}")

    with open(args.dump, "w", encoding="utf-8") as f:
        json.dump({s: {"dim": r["dim"], "docs_per_sec": r["docs_per_sec"], "query_ms": r["query_ms"], "rows": r["rows"]}
                   for s, r in results.items()}, f, ensure_ascii=False, indent=2)
    _report(results, device, args.report)
    print(f"\n리포트: {args.report}\n덤프: {args.dump}")


if __name__ == "__main__":
    main()
