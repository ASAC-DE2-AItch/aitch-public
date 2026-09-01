# -*- coding: utf-8 -*-
"""
골든셋 검색 평가 — bge-m3 hybrid 검색 품질(hit@K·MRR)을 컬렉션별로 측정한다.

정답(gold) 판정 2방식:
  · error_manual : known-item — 질의의 정답을 담은 청크를 distinctive 구절(phrase)로 판정
                   (retrieved 청크 text에 phrase 포함 시 relevant)
  · 구조화 3종   : set-based — payload predicate(sensor/action_type 등) 매칭 시 relevant
                   (정답이 여러 건이라 hit@K·MRR로 평가; Recall 대신 success@K)

지표 : hit@1 / hit@3 / hit@5 (top-K 안에 relevant 1건 이상) · MRR@10
실행 : QDRANT_PATH=<로컬DB> python -m src.agent_service.eval_golden_set   (docker면 QDRANT_URL)
사전 : qdrant_ingest 로 컬렉션 적재돼 있어야 함
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.agent_service.vectordb import get_qdrant_client, hybrid_search

log = logging.getLogger("eval-golden")

# ── 골든셋 (큐레이션 저작) ────────────────────────────────────────────────────
# error_manual: {"phrase": 정답 청크에 있는 distinctive 구절(소문자 비교)}
# 구조화 3종  : {"pred": {payload필드: 값}}  (모두 일치해야 relevant)
GOLDEN = [
    # ── error_manual (장비 매뉴얼) ──
    ("error_manual", "실리콘에 깊고 수직인 트렌치를 만드는 이방성 식각 공정은?", {"phrase": "bosch process"}),
    ("error_manual", "공정 챔버 진공을 배기하는 터보분자펌프", {"phrase": "atp900"}),
    ("error_manual", "RF 누설 전자파를 측정하는 계측기 종류", {"phrase": "narda"}),
    ("error_manual", "기존 레시피 스텝을 수정하는 방법", {"phrase": "edit an existing recipe"}),
    ("error_manual", "Oxford Plasmalab 80에서 식각 가능한 재료 목록", {"phrase": "thermo oxide"}),
    ("error_manual", "로드락을 벤트하는 절차", {"phrase": "vent the loadlock"}),
    ("error_manual", "이 에처의 모델명과 Bay 위치", {"phrase": "180-icp"}),
    ("error_manual", "8인치 실리콘 RIE 장비 플라즈마썸", {"phrase": "plasma-therm 790"}),
    ("error_manual", "뷰포트로 볼 때 자외선 노출 위험 안전", {"phrase": "uv exposure"}),
    ("error_manual", "비표준 샘플을 캐리어 웨이퍼에 고정하는 방법", {"phrase": "kapton tape"}),
    ("error_manual", "오일 실 펌프의 윤활유 선택 기준", {"phrase": "lubricating oils"}),
    ("error_manual", "ICP 웨이퍼 이온 전류 밀도 성능 지표", {"phrase": "ion current density"}),
    ("error_manual", "비상 정지 EMO 인터락 기능", {"phrase": "emergency off"}),
    ("error_manual", "승인된 식각 재료 파릴렌 폴리이미드", {"phrase": "parylene"}),
    # ── historical_case (사건) ──
    ("historical_case", "PM 이후 DC self-bias 드리프트로 관리한계 재설정한 사례", {"pred": {"sensor_id": "C11", "action_type": "limit_correction"}}),
    ("historical_case", "RF 반사파 이상으로 Match Network 정비한 사례", {"pred": {"sensor_id": "C32", "action_type": "maintenance"}}),
    ("historical_case", "공정 조건 이탈로 레시피 튜닝한 사례", {"pred": {"action_type": "recipe_r2r"}}),
    ("historical_case", "반복 재발로 공정팀에 에스컬레이션한 사례", {"pred": {"action_type": "escalation"}}),
    ("historical_case", "Throttle Valve 이상으로 정비한 사례", {"pred": {"sensor_id": "C57", "action_type": "maintenance"}}),
    ("historical_case", "가스 유량 실력치 재산정 사례", {"pred": {"sensor_id": "C48", "action_type": "limit_correction"}}),
    ("historical_case", "진짜 장비 이상으로 웨이퍼 스크랩 동반한 사례", {"pred": {"action_type": "maintenance", "wafer_disposition": "scrap"}}),
    ("historical_case", "조치 없이 모니터링만 한 Info 등급 사례", {"pred": {"action_type": "monitor"}}),
    ("historical_case", "척 온도 냉각 이상으로 정비한 사례", {"pred": {"sensor_id": "C17", "action_type": "maintenance"}}),
    # ── limit_change_log·recipe_r2r_log 골든 제거 (2026-07-10): 로그는 Postgres 전용 → 벡터 검색 대상 아님 ──
    # ── process_knowledge (센서 정의 카드 + DRAM 개념노트) — phrase 매칭 ──
    ("process_knowledge", "DC Self-Bias 전압 Vdc 센서, 플라즈마 점화 시퀀스", {"phrase": "-1→-335"}),
    ("process_knowledge", "Main Gas Flow 설정값 센서 (실측 반전, 이산 레벨)", {"phrase": "235/1576/258"}),
    ("process_knowledge", "He Backside 압력·냉각 제어 센서", {"phrase": "he backside"}),
    ("process_knowledge", "DRAM 셀 전하 누설 전 재기록하는 Refresh 주기", {"phrase": "7.8μs"}),
    ("process_knowledge", "DRAM 저장노드 커패시터 High-K 유전체 EOT scaling 구조", {"phrase": "metal-insulator-metal"}),
    ("process_knowledge", "소자 간 분리 STI Shallow Trench Isolation 공정", {"phrase": "shallow trench isolation"}),
    # ── 코드/고유명사 포함 질의 (엔지니어 실제 검색 방식 — sparse/hybrid 이점 검증용) ──
    ("error_manual", "ATP900 터보분자펌프 진공 배기", {"phrase": "atp900"}),
    ("error_manual", "Narda 계측기로 RF 누설 측정", {"phrase": "narda"}),
    ("error_manual", "Oxford 180-ICP 에처 SOP", {"phrase": "180-icp"}),
    ("error_manual", "Hexid A40 냉각수 보충", {"phrase": "hexid"}),
    ("historical_case", "C62 RF 전극전압 Vpp 이상 사례", {"pred": {"sensor_id": "C62"}}),
    ("historical_case", "C18 RF 매칭 편차 이상 사례", {"pred": {"sensor_id": "C18"}}),
]

K = 10


def _is_relevant(gold: dict, payload: dict) -> bool:
    """retrieved 문서가 gold 기준에 relevant 한지."""
    if "phrase" in gold:
        return gold["phrase"] in (payload.get("text") or "").lower()
    return all(payload.get(k) == v for k, v in gold["pred"].items())


def evaluate(client):
    """골든셋 전체를 검색·채점해 컬렉션별/전체 지표를 반환한다."""
    per = {}
    rows = []
    for kb_type, query, gold in GOLDEN:
        points = hybrid_search(client, kb_type, query, limit=K)
        rank = 0
        for i, p in enumerate(points, 1):
            if _is_relevant(gold, p.payload or {}):
                rank = i
                break
        rec = {
            "kb_type": kb_type, "query": query,
            "hit@1": int(0 < rank <= 1), "hit@3": int(0 < rank <= 3),
            "hit@5": int(0 < rank <= 5), "rr": (1.0 / rank if rank else 0.0),
            "first_rank": rank or None,
        }
        rows.append(rec)
        per.setdefault(kb_type, []).append(rec)
    return rows, per


def _agg(recs):
    n = len(recs)
    return {m: sum(r[m] for r in recs) / n for m in ("hit@1", "hit@3", "hit@5", "rr")}, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=None, help="골든셋+결과 jsonl 저장 경로")
    args = ap.parse_args()
    client = get_qdrant_client()
    rows, per = evaluate(client)

    print(f"\n{'='*66}\n bge-m3 hybrid 검색 평가 · 골든셋 {len(rows)}문항 (top-{K})\n{'='*66}")
    print(f"{'컬렉션':<20}{'문항':>4}{'hit@1':>8}{'hit@3':>8}{'hit@5':>8}{'MRR':>8}")
    print("-" * 66)
    for kb_type, recs in per.items():
        a, n = _agg(recs)
        print(f"{kb_type:<20}{n:>4}{a['hit@1']:>8.2f}{a['hit@3']:>8.2f}{a['hit@5']:>8.2f}{a['rr']:>8.3f}")
    a, n = _agg(rows)
    print("-" * 66)
    print(f"{'전체':<20}{n:>4}{a['hit@1']:>8.2f}{a['hit@3']:>8.2f}{a['hit@5']:>8.2f}{a['rr']:>8.3f}")

    miss = [r for r in rows if not r["hit@5"]]
    if miss:
        print(f"\n▼ top-{K} 안에 정답 없음 ({len(miss)}건):")
        for r in miss:
            print(f"  [{r['kb_type']}] {r['query']}")

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n결과 저장: {args.dump}")


if __name__ == "__main__":
    main()
