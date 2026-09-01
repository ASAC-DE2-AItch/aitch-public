# -*- coding: utf-8 -*-
"""평가 계측 · ablation 스위치 (W40 · 2026-08-06).

**운영 경로에는 아무 영향이 없어야 한다.** 평가 하네스가 켤 때만 값을 모으고, 안 켜면
`record_*` 는 전부 즉시 return 한다. 그래서 파이프라인 코드에 `if 평가중:` 분기를 심지 않는다.

두 가지를 담는다 — 성격이 다르니 아래 두 절로 나눠 둔다.

  ① **관측(observation)** — alert 1건마다 다른 값. `ContextVar` 로 **asyncio 태스크별 격리**.
     `asyncio.gather` 로 동시 실행하는 라운드에서 전역 dict 를 쓰면 건들이 서로 섞인다
     (`--concurrency 2` 가 기본이라 실제로 섞인다). Task 는 생성 시점 컨텍스트를 복사하므로,
     코루틴 **안에서** set 하면 그 태스크에만 보인다.

  ② **ablation** — 라운드 통째로 같은 값. 프로세스 전역 플래그로 충분하고, 그게 더 읽기 쉽다.
     "재료를 뺐을 때 판정이 얼마나 떨어지나"를 재는 용도 (KB / few-shot / SHAP).

⚠️ **왜 필요했나.** 8/5 까지 라운드 결과에 남는 건 판정과 근거 문장뿐이었다. 그래서
  ⓐ D4(30초) 예산을 넘겼는지 **결과 파일로는 판정할 수 없었고**(로그로만 나갔다),
  ⓑ 판정이 틀렸을 때 *검색이 못 찾아서인지 판단이 틀려서인지* 가를 수가 없었고,
  ⓒ 우리가 만든 재료(KB·few-shot)가 실제로 기여하는지 **한 번도 재본 적이 없었다.**
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# =============================================================================
# ① 관측 — alert 1건 단위 (ContextVar)
# =============================================================================

_BAG: ContextVar[Optional[dict[str, Any]]] = ContextVar("agent_observation", default=None)


@contextmanager
def observing() -> Iterator[dict[str, Any]]:
    """이 블록 안에서 일어난 검색·LLM 호출·지연을 모은다 (평가 하네스 전용).

    반환된 dict 는 블록을 나온 뒤에도 읽을 수 있다 — 호출자가 결과 레코드에 합친다.
    """
    bag: dict[str, Any] = {"retrieval": [], "llm": [], "latency": {}}
    token = _BAG.set(bag)
    try:
        yield bag
    finally:
        _BAG.reset(token)


def _bag() -> Optional[dict[str, Any]]:
    return _BAG.get()


def is_observing() -> bool:
    """지금 이 알람이 **계측 라운드 안**인가 (= `observing()` 안).

    계측 때문에 운영 비용을 늘리는 코드는 이 값을 보고 갈라야 한다. 예: 검색 폭을
    hit@10 계측용으로 넓히는 것(`pipeline._search_top_k`) — 프롬프트에 실리는 건
    여전히 top_k 뿐이라, 운영에서 넓히면 **Qdrant 부하만 3배가 되고 얻는 게 없다**.
    (2026-08-06 PR #122 3차 리뷰 지적 — 계측 배선이 운영에 상시 적용돼 있었다)

    `observing()` 은 평가 진입점에서만 열리고 ContextVar 라 알람 단위로 격리된다 —
    운영 consumer 에서는 항상 False.
    """
    return _BAG.get() is not None


def record_retrieval(collection: str, query: str, points: Any) -> None:
    """검색 1회 — **무엇이 몇 위로 나왔는지**를 남긴다.

    ⚠️ `hit` 플래그가 아니라 **doc_id 목록**을 남기는 이유:
      ⓐ hit 는 정답 라벨이 있어야 계산되는데, 기존 fixture 47건엔 라벨이 없다.
         목록으로 두면 라벨이 나중에 생겨도 **소급 계산**이 된다.
      ⓑ 플래그만 있으면 틀렸을 때 *무엇이 대신 들어왔는지* 를 못 본다 — 그게 정보다.
      ⓒ `hit@3` 와 `hit@10` 을 한 번의 라운드로 같이 잴 수 있다(순위를 들고 있으므로).

    🔴 **`doc_id` 만으로는 부족하다** (2026-08-06 스모크에서 잡음). `error_manual` 은 doc_id 가
    **매뉴얼 1권 단위**라 상위 10건이 전부 `oxford_100_manual` 로 같게 나온다 — 그 축은 무엇을
    맞혀도 "hit" 이 되어 **지표가 항상 100%** 가 된다. 청크 단위 식별자(`chunk_id`)를 함께 남긴다.
    (`benchmarks/eval_golden_set.py` 가 error_manual 만 phrase 기반으로 채점하는 이유가 이거였다.)

    `score` 도 남긴다 — RRF 융합 점수라 **동점이 흔하다**(실측: 0.5 가 여러 건). 동점 구간의
    순서는 사실상 임의라, hit@3 를 읽을 때 그걸 모르면 없는 정밀도를 믿게 된다.
    """
    bag = _bag()
    if bag is None:
        return
    try:
        bag["retrieval"].append({
            "collection": collection,
            "query": query,
            "hits": [
                {"rank": i,
                 "doc_id": (getattr(p, "payload", None) or {}).get("doc_id"),
                 "chunk_id": (getattr(p, "payload", None) or {}).get("chunk_id"),
                 "score": getattr(p, "score", None)}
                for i, p in enumerate(points or [], 1)
            ],
        })
    except Exception as exc:  # noqa: BLE001 — 계측이 파이프라인을 죽이면 안 된다
        logger.debug("record_retrieval 실패(무시): %s", exc)


def record_llm(usage: Any, *, model: str | None = None) -> None:
    """LLM 호출 1회의 토큰 사용량 (OpenAI 호환 `usage` 블록 그대로).

    프롬프트 예산(`measure_prompt_budget`)은 **보낼 때 추정치**이고, 이건 **서버가 센 실측치**다.
    둘이 갈라지면 예산 계산이 틀린 것이라 알아야 한다.
    """
    bag = _bag()
    if bag is None or not isinstance(usage, dict):
        return
    bag["llm"].append({
        "model": model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    })


def record_latency(**parts: float) -> None:
    """관통 1건의 단계별 소요 (초). `_log_latency` 가 이미 계산한 값을 그대로 받는다."""
    bag = _bag()
    if bag is None:
        return
    bag["latency"].update({k: round(float(v), 3) for k, v in parts.items()})


def summarize(bag: dict[str, Any]) -> dict[str, Any]:
    """결과 jsonl 에 실을 납작한 형으로 요약한다 (원본 목록도 함께 남긴다).

    `obs_` 접두 — 기존 `sup_`·`tool_` 접두와 같은 규약.
    """
    llm = bag.get("llm") or []
    lat = bag.get("latency") or {}
    return {
        "obs_latency_total_sec": lat.get("total"),
        "obs_latency_prep_sec": lat.get("prep"),
        "obs_latency_fanout_sec": lat.get("fanout"),
        "obs_latency_supervisor_sec": lat.get("supervisor"),
        "obs_llm_calls": len(llm),
        "obs_prompt_tokens": sum(c["prompt_tokens"] or 0 for c in llm) or None,
        "obs_completion_tokens": sum(c["completion_tokens"] or 0 for c in llm) or None,
        # 검색은 컬렉션별 doc_id 목록을 그대로 — hit 계산은 analyze 단계에서 한다(위 docstring)
        "obs_retrieval": bag.get("retrieval") or [],
        "obs_retrieved_n": sum(len(r["hits"]) for r in (bag.get("retrieval") or [])),
    }


# =============================================================================
# ② ablation — 라운드 단위 (프로세스 전역)
# =============================================================================

#: 끌 수 있는 재료. **이름 = CLI 플래그**(`--no-kb` → "kb").
_ABLATABLE = ("kb", "fewshot", "shap")

#: 이번 **프로세스**에서 뺀 재료. 위 `_BAG`(ContextVar, 알람 단위)과 달리 일부러 전역이다 —
#: ablation 은 "이 라운드 전체를 재료 없이 돈다"는 **라운드 속성**이라 알람마다 달라지면 안 된다.
#:
#: ⚠️ 전역이므로 **운영 프로세스와 절대 섞이면 안 된다.** 유일한 설정 진입점은
#: `eval_supervisor.py`(평가 CLI)이고, 운영 consumer(`app/run.py`)는 이 함수를 호출하지
#: 않아 항상 빈 set 이다. 그 경계는 테스트로 고정한다 —
#: `tests/test_observe.py::test_operational_entrypoints_never_set_ablation`.
#: (2026-08-06 PR #122 2차 리뷰 지적 — "동일 프로세스 재사용 시 동시성 소지" 확인 결과)
_ABLATED: set[str] = set()


def set_ablation(names) -> None:
    """이번 라운드에 **뺄** 재료를 지정한다. 오타는 조용히 넘기지 않는다."""
    unknown = set(names or ()) - set(_ABLATABLE)
    if unknown:
        raise ValueError(f"모르는 ablation 이름: {sorted(unknown)} (가능: {list(_ABLATABLE)})")
    _ABLATED.clear()
    _ABLATED.update(names or ())
    if _ABLATED:
        logger.warning("⚠️ ablation — 이번 라운드는 %s 를 **빼고** 돈다. 판정 품질 비교 전용이다",
                       sorted(_ABLATED))


def ablated(name: str) -> bool:
    """`name` 재료가 이번 라운드에서 빠졌는가."""
    return name in _ABLATED


def ablation_tag() -> str:
    """provenance 표기 — 뺀 게 없으면 'none'."""
    return "+".join(sorted(_ABLATED)) if _ABLATED else "none"


def strip_ablated_alert(dumped: dict[str, Any]) -> dict[str, Any]:
    """tool 에 넘길 alert dict 에서 ablation 대상 필드를 뺀다.

    ⚠️ **키를 지우지 않고 빈 값으로 둔다.** 키가 통째로 사라지면 LLM 이 "그런 축이 원래 없다"로
    읽어 다른 방향으로 답하게 되고, 그러면 *"SHAP 이 없을 때"* 가 아니라 *"입력 형태가 다를 때"* 를
    재게 된다. 재는 대상은 **재료의 유무**지 스키마가 아니다.
    """
    if not ablated("shap"):
        return dumped
    pc = dumped.get("prediction_context")
    if isinstance(pc, dict):
        pc = {**pc, "shap_top3": []}
        return {**dumped, "prediction_context": pc}
    return dumped
