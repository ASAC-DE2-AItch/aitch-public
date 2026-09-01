# -*- coding: utf-8 -*-
"""KB 검색 서버 — 임베딩이 필요한 검색만 떼어 놓은 얇은 HTTP 층 (2026-08-11 신설).

**쉬운 말 요약** — 챗봇이 "지난주 비슷한 일 있었나?" 에 답하려면 질문을 **벡터로 바꿔서**
사례 10,000점 중 가까운 것을 찾아야 한다. 그 변환기(bge-m3)가 무거워서 게이트웨이에는
못 넣는다(torch 포함 GB 단위). 그래서 **이미 그 변환기를 가진 이 이미지**에 검색 라우트
하나만 얹고, 게이트웨이가 HTTP 로 물어보게 한다.

왜 게이트웨이에 임베딩을 안 넣나 (2026-08-11 판단):
  · 게이트웨이 이미지는 REST/SSE 용 경량 층이다. torch·FlagEmbedding 을 넣으면 GB 단위로
    커지고, `hf_cache` 볼륨(4.7GB)까지 마운트해야 한다 — 역할 경계가 무너진다.
  · 반면 이 이미지는 **이미 전부 갖고 있다**(FlagEmbedding·torch·`hf_cache:/models`).
    가진 쪽에 라우트를 얹는 것이 싸고, 모델 로딩 지점도 한 곳으로 유지된다.

⚠️ **이 프로세스는 컨슈머(`app/run.py`)와 따로 뜬다.** 한 컨테이너에서 둘을 돌리면
   무엇이 죽었는지 구분이 안 되고, 컨슈머 재시작이 검색을 끊는다. compose 에서 **같은
   이미지·다른 커맨드**로 별 서비스를 둔다.

⚠️ **읽기 전용이다.** 검색만 하고 아무것도 쓰지 않는다 — 이 포트로 조치가 나갈 길이 없다.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import Body, FastAPI
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ %(message)s")
logger = logging.getLogger("kb-server")

app = FastAPI(title="AITCH KB Search", docs_url=None, redoc_url=None)

#: 검색 가능한 논리 컬렉션 — **화이트리스트다.** 임의 문자열을 그대로 넘기면 오타가
#  "컬렉션 없음" 예외로 가고, 더 나쁘게는 의도치 않은 컬렉션을 열어준다.
_ALLOWED = ("historical_case", "error_manual", "process_knowledge")

#: 한 번에 돌려줄 최대 건수. 부르는 쪽(대화창)이 카드 3장 넘게 못 쓴다 — 상한을 서버가 쥔다.
_MAX_LIMIT = 10


class SearchRequest(BaseModel):
    """검색 1건. `collection` 은 논리명이고 물리명 해석은 `vectordb.physical_name` 이 한다."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(..., min_length=1)
    collection: str = "historical_case"
    limit: int = Field(default=3, ge=1, le=_MAX_LIMIT)
    #: payload 동등 조건 — `{"action_type": "limit_correction", "is_success": true}`.
    #  **Qdrant 문법이 아니라 평범한 dict 다** — 와이어 계약을 단순하게 두고 변환은
    #  `_to_filter` 한 곳에서만 한다(부르는 쪽이 Qdrant 모델에 결박되지 않게).
    where: dict[str, Any] = Field(default_factory=dict)


def _to_filter(where: dict[str, Any]):
    """`{k: v}` → Qdrant Filter(must=[...]). 빈 dict 면 None(=필터 없음)."""
    if not where:
        return None
    from qdrant_client import models  # noqa: PLC0415

    return models.Filter(must=[
        models.FieldCondition(key=k, match=models.MatchValue(value=v))
        for k, v in where.items()
    ])


@app.get("/health")
def health() -> dict:
    """기동 확인. **모델이 실제로 로드됐는지까지는 말하지 않는다** — 그건 `/warmup` 이다.

    "떴다"와 "붙었다"를 구별하려고 나눠 둔다(헌법 7장). bge-m3 는 첫 호출에서 수십 초가
    걸릴 수 있어, health 가 200 이라고 검색이 즉시 되는 것은 아니다.
    """
    return {"ok": True, "collections": list(_ALLOWED)}


@app.post("/warmup")
def warmup() -> dict:
    """임베딩 모델을 미리 올린다 — 첫 질문이 수십 초 걸리는 것을 피한다.

    시연 전에 한 번 때려 두는 용도다. 실패해도 500 을 내지 않는다 — 준비가 안 됐다는
    사실을 그대로 돌려주고, 검색은 그때 다시 시도한다.
    """
    try:
        from .vectordb import embed_hybrid  # noqa: PLC0415

        embed_hybrid(["warmup"])
        logger.info("✅ 임베딩 모델 로드 완료")
        return {"ready": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("임베딩 예열 실패 (%s: %s)", type(exc).__name__, exc)
        return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/kb/search")
def kb_search(body: SearchRequest = Body(...)) -> dict:
    """벡터 검색 1회 → payload 목록.

    ⚠️ **500 을 내지 않는다.** 부르는 쪽은 대화창이고, 근거가 얕아질지언정 답변은 계속돼야
    한다(헌법 6-2). 실패는 `hits: []` + `error` 로 알린다 — 빈손을 빈손으로 보이게.
    """
    if body.collection not in _ALLOWED:
        logger.warning("허용되지 않은 컬렉션 요청: %r", body.collection)
        return {"hits": [], "error": f"unknown collection: {body.collection}"}

    def _run(where: dict[str, Any]) -> list[dict[str, Any]]:
        from .vectordb import get_qdrant_client, search_kb  # noqa: PLC0415

        pts = search_kb(get_qdrant_client(), body.collection, body.query,
                        limit=body.limit, query_filter=_to_filter(where))
        return [p.payload for p in (pts or []) if isinstance(getattr(p, "payload", None), dict)]

    def _count(where: dict[str, Any]) -> Optional[int]:
        """조건에 맞는 **전체** 건수. 실패하면 None — 모르면 모른다고 해야 한다.

        🔴 **왜 세어 주나** (2026-08-11 실측). 검색은 상위 N건만 돌려주는데 모델은 그것을
        **전부로 읽는다** — *"관리선 재설정 사례가 몇 건이야?"* 에 **"1건입니다"** 라고
        단정했다(실제 1,775건). 없는 것을 지어내는 것보다 나쁘다: 숫자는 그럴듯해서
        검증 없이 믿긴다. 그래서 개수를 묻든 안 묻든 **표본이라는 사실을 항상** 알린다.
        """
        from .vectordb import get_qdrant_client, physical_name  # noqa: PLC0415

        try:
            r = get_qdrant_client().count(collection_name=physical_name(body.collection),
                                          count_filter=_to_filter(where), exact=True)
            return int(r.count)
        except Exception as exc:  # noqa: BLE001 — 개수를 못 세도 검색 결과는 유효하다
            logger.warning("KB count 실패 (%s: %s)", type(exc).__name__, exc)
            return None

    try:
        hits = _run(body.where)
        total = _count(body.where)
        relaxed = False
        # 🔴 **필터가 0건이면 풀고 다시 찾는다.** 좁힌 결과가 비었다는 것과 아예 없다는 것은
        #    다른 사실인데, 화면에서는 똑같이 "사례 없음" 으로 보인다. 넓혀서 찾아 주고
        #    **넓혔다는 사실을 함께** 돌려준다 — 조용히 넓히면 "조건에 맞는 것"으로 오해한다.
        if body.where and not hits:
            hits = _run({})
            total = _count({})          # 조건을 풀었으면 전체 수도 그 기준으로
            relaxed = True
            logger.info("필터 0건 → 조건 해제 재검색 (%s) → %d건", body.where, len(hits))
    except Exception as exc:  # noqa: BLE001
        logger.warning("KB 검색 실패 (%s · %s: %s)", body.collection, type(exc).__name__, exc)
        return {"hits": [], "total": None, "error": f"{type(exc).__name__}: {exc}", "relaxed": False}

    logger.info("KB 검색 — %s · %r · where=%s → %d건 / 전체 %s%s",
                body.collection, body.query[:40], body.where or "없음", len(hits),
                total if total is not None else "미상", " (조건 해제)" if relaxed else "")
    return {"hits": hits, "total": total, "error": None, "relaxed": relaxed}


def main() -> None:
    """`python -m src.agent_service.kb_server` 로 기동 (compose command)."""
    import uvicorn

    port = int(os.environ.get("KB_SERVER_PORT", "8100"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
