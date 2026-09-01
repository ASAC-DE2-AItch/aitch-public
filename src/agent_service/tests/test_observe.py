# -*- coding: utf-8 -*-
"""평가 계측 · ablation 테스트 (W40 · 2026-08-06).

이 파일이 지키는 것은 하나다 — **운영 경로는 그대로여야 한다.**
계측을 안 켰을 때 `record_*` 가 조용히 아무 일도 안 하는지, ablation 이 꺼져 있을 때
프롬프트·payload 가 **글자 하나 안 바뀌는지**를 고정한다.

두 번째 축은 **격리**다. 라운드는 `--concurrency` 로 동시 실행하므로, 계측 가방이 전역이면
건들이 섞여 *"A 알람의 검색 결과가 B 알람 기록에 들어가는"* 일이 난다. 그건 눈에 안 띈다 —
숫자가 그럴듯하게 나오기 때문이다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.agent_service.app import observe  # noqa: E402


class _Pt:
    """Qdrant ScoredPoint 대역 — payload·score 만 본다."""

    def __init__(self, doc_id, score, chunk_id=None):
        self.payload = {"doc_id": doc_id, "chunk_id": chunk_id or f"{doc_id}#0"}
        self.score = score


@pytest.fixture(autouse=True)
def _clean_ablation():
    """테스트 간 ablation 이 새지 않게 (프로세스 전역 플래그다)."""
    observe.set_ablation([])
    yield
    observe.set_ablation([])


# --- 운영 무영향 --------------------------------------------------------------
def test_record_is_noop_without_observing() -> None:
    """계측을 안 켜면 아무 일도 안 한다 — 운영 경로에서 호출돼도 무해해야 한다."""
    observe.record_retrieval("process_knowledge", "q", [_Pt("PK-1", 0.9)])
    observe.record_llm({"prompt_tokens": 100})
    observe.record_latency(total=1.0)
    # 예외 없이 통과하는 것 자체가 단언이다 (담을 가방이 없으므로 담지 않는다)


def test_record_retrieval_survives_bad_points() -> None:
    """계측이 파이프라인을 죽이면 안 된다 — 이상한 입력에도 조용히 넘어간다."""
    with observe.observing() as bag:
        observe.record_retrieval("c", "q", [object()])   # payload·score 없는 객체
        observe.record_retrieval("c", "q", None)
    assert isinstance(bag["retrieval"], list)


# --- 기록 내용 ----------------------------------------------------------------
def test_retrieval_keeps_rank_and_doc_id() -> None:
    """**순위와 doc_id 를 남긴다** — hit 플래그가 아니라. 그래야 hit@3·hit@10 을 한 번에 잰다."""
    with observe.observing() as bag:
        observe.record_retrieval("historical_case", "질의",
                                 [_Pt("CASE-1", 0.9), _Pt("CASE-2", 0.8), _Pt("CASE-3", 0.7)])
    hits = bag["retrieval"][0]["hits"]
    assert [h["rank"] for h in hits] == [1, 2, 3]
    assert [h["doc_id"] for h in hits] == ["CASE-1", "CASE-2", "CASE-3"]


def test_retrieval_keeps_chunk_id_not_only_doc_id() -> None:
    """🔴 **doc_id 만 남기면 매뉴얼 축의 hit 지표가 항상 100% 가 된다** (2026-08-06 스모크 발견).

    `error_manual` 은 doc_id 가 **매뉴얼 1권 단위**라 상위 10건이 전부 `oxford_100_manual` 로
    같게 나왔다. 그 상태로 정답을 doc_id 로 라벨링하면 **무엇을 검색해도 맞은 것**이 된다 —
    지표가 존재하되 아무것도 재지 않는, 제일 위험한 형태다.
    """
    same_doc = [_Pt("oxford_100_manual", 0.5, chunk_id=f"oxford#{i}") for i in range(3)]
    with observe.observing() as bag:
        observe.record_retrieval("error_manual", "q", same_doc)
    hits = bag["retrieval"][0]["hits"]
    assert len({h["doc_id"] for h in hits}) == 1        # doc_id 로는 구분 불가
    assert len({h["chunk_id"] for h in hits}) == 3      # chunk_id 로는 구분된다


def test_retrieval_keeps_score_for_tie_detection() -> None:
    """RRF 융합 점수는 **동점이 흔하다**(실측 0.5 다수). 동점 구간의 순서는 임의라,
    점수를 안 남기면 hit@3 에 없는 정밀도를 믿게 된다."""
    with observe.observing() as bag:
        observe.record_retrieval("c", "q", [_Pt("A", 0.5), _Pt("B", 0.5), _Pt("C", 0.5)])
    assert [h["score"] for h in bag["retrieval"][0]["hits"]] == [0.5, 0.5, 0.5]


def test_summarize_flattens_for_jsonl() -> None:
    """결과 jsonl 에 실릴 납작한 형 — 지연·토큰이 **결과 파일에서** 판정 가능해야 한다(D4)."""
    with observe.observing() as bag:
        observe.record_latency(total=12.5, prep=1.0, fanout=9.0, supervisor=2.5)
        observe.record_llm({"prompt_tokens": 8000, "completion_tokens": 400})
        observe.record_llm({"prompt_tokens": 5000, "completion_tokens": 300})
        observe.record_retrieval("c", "q", [_Pt("D1", 0.9), _Pt("D2", 0.8)])
    out = observe.summarize(bag)
    assert out["obs_latency_total_sec"] == 12.5
    assert out["obs_llm_calls"] == 2
    assert out["obs_prompt_tokens"] == 13000
    assert out["obs_completion_tokens"] == 700
    assert out["obs_retrieved_n"] == 2


def test_observing_is_isolated_per_task() -> None:
    """🔴 **동시 실행되는 두 알람의 계측이 섞이면 안 된다** (`--concurrency` 기본 2~3).

    전역 dict 로 만들면 여기서 깨진다. 그리고 이 사고는 **에러 없이** 그럴듯한 숫자를 낸다.
    """
    async def one(name: str, n: int) -> dict:
        with observe.observing() as bag:
            observe.record_retrieval(name, "q", [_Pt(f"{name}-{i}", 0.5) for i in range(n)])
            await asyncio.sleep(0)          # 다른 태스크에 양보 — 섞일 기회를 준다
            observe.record_latency(total=float(n))
            return observe.summarize(bag)

    a, b = asyncio.run(_gather(one("A", 2), one("B", 5)))
    assert a["obs_retrieved_n"] == 2 and a["obs_latency_total_sec"] == 2.0
    assert b["obs_retrieved_n"] == 5 and b["obs_latency_total_sec"] == 5.0


async def _gather(*aws):
    return await asyncio.gather(*aws)


# --- ablation -----------------------------------------------------------------
def test_unknown_ablation_name_raises() -> None:
    """오타를 조용히 넘기면 **뺐다고 믿고 안 뺀 라운드**가 나온다 — 측정이 통째로 거짓이 된다."""
    with pytest.raises(ValueError):
        observe.set_ablation(["knowledgebase"])


def test_ablation_off_leaves_prompt_untouched() -> None:
    """끄면 프롬프트가 **글자 하나 안 바뀐다** (운영 경로 = 이 상태)."""
    from src.agent_service.app.tools.base import system_prompt

    prompt, few = "규칙들...\n[학습 예시]\nA", "\n[학습 예시]\nA"
    assert system_prompt(prompt, few) == prompt


def test_ablation_fewshot_removes_examples() -> None:
    observe.set_ablation(["fewshot"])
    from src.agent_service.app.tools.base import system_prompt

    prompt, few = "규칙들...\n[학습 예시]\nA", "\n[학습 예시]\nA"
    assert system_prompt(prompt, few) == "규칙들..."


def test_ablation_shap_empties_but_keeps_key() -> None:
    """⚠️ 키를 **지우지 않고 비운다** — 키가 없으면 '그런 축이 원래 없다'로 읽혀
    *재료의 유무*가 아니라 *스키마의 차이*를 재게 된다."""
    observe.set_ablation(["shap"])
    dumped = {"prediction_context": {"wafer_id": "C64_1", "shap_top3": ["C11", "C62", "C17"]}}
    out = observe.strip_ablated_alert(dumped)
    assert out["prediction_context"]["shap_top3"] == []
    assert "shap_top3" in out["prediction_context"]
    assert out["prediction_context"]["wafer_id"] == "C64_1"
    # 원본 불변 (얕은 복사로 다른 건이 오염되지 않게)
    assert dumped["prediction_context"]["shap_top3"] == ["C11", "C62", "C17"]


def test_ablation_off_returns_same_object() -> None:
    """끄면 payload 도 원본 그대로 — 복사조차 하지 않는다."""
    dumped = {"prediction_context": {"shap_top3": ["C11"]}}
    assert observe.strip_ablated_alert(dumped) is dumped


def test_ablation_tag_for_provenance() -> None:
    """runs_history 에 뭘 뺐는지 남는다 — 태그는 사람이 붙여 틀린다(8/5 교훈)."""
    assert observe.ablation_tag() == "none"
    observe.set_ablation(["fewshot", "kb"])
    assert observe.ablation_tag() == "fewshot+kb"   # 정렬 — 라운드 간 비교 가능하게


# --- 배선 확인: 플래그가 **실제 프롬프트/payload 까지 닿는가** ------------------------
# 🔴 여기가 이 파일에서 두 번째로 중요한 축이다. `set_ablation` 이 켜지고 provenance 에도
#    "kb+shap" 이 찍히는데 정작 tool 이 그걸 안 보면, **뺐다고 믿고 안 뺀 라운드**가 나온다.
#    그 라운드는 에러도 안 나고 숫자도 그럴듯하다 — 8/5 의 "정량 엔진이 스텁이던 5라운드"와
#    정확히 같은 형태의 사고다.
def _alert():
    import json

    from src.agent_service.app.schemas.alert import AlertModel
    p = (Path(__file__).resolve().parents[1] / "fixtures" / "alerts"
         / "02_baseline_aging_gate_edge.json")
    return AlertModel.model_validate(json.loads(p.read_text(encoding="utf-8")))


@pytest.mark.parametrize("mod", ["recipe", "limit", "maintenance"])
def test_shap_ablation_reaches_every_tool_payload(mod) -> None:
    """세 tool **전부** 뚫려 있어야 한다 — 하나만 빠져도 그 축은 대조가 안 된다."""
    import importlib
    import json as _json

    m = importlib.import_module(f"src.agent_service.app.tools.{mod}")
    alert = _alert()

    observe.set_ablation([])
    on = _json.loads(m.build_payload(alert))["alert"]["prediction_context"]["shap_top3"]
    observe.set_ablation(["shap"])
    off = _json.loads(m.build_payload(alert))["alert"]["prediction_context"]["shap_top3"]

    assert on, f"{mod}: 기준 라운드에 SHAP 이 실려 있어야 한다"
    assert off == [], f"{mod}: --ablate shap 인데 SHAP 이 그대로 실렸다"


@pytest.mark.parametrize("mod", ["recipe", "limit", "maintenance"])
def test_fewshot_ablation_reaches_every_tool_prompt(mod) -> None:
    """few-shot 도 세 tool 전부 — 예시가 프롬프트에서 실제로 빠지는가."""
    import importlib

    from src.agent_service.app.tools.base import system_prompt

    m = importlib.import_module(f"src.agent_service.app.tools.{mod}")

    observe.set_ablation([])
    assert m.FEWSHOT_EXAMPLES in system_prompt(m.SYSTEM_PROMPT, m.FEWSHOT_EXAMPLES)
    observe.set_ablation(["fewshot"])
    assert m.FEWSHOT_EXAMPLES not in system_prompt(m.SYSTEM_PROMPT, m.FEWSHOT_EXAMPLES)


def test_measurement_width_does_not_leak_into_operation() -> None:
    """계측용 검색 확장은 **계측 라운드 안에서만** 일어난다.

    hit@10 을 재려고 Qdrant 조회 폭을 넓히는데, 프롬프트에 실리는 건 여전히 top_k 뿐이다.
    운영에서 넓히면 3컬렉션 × 3배 조회를 하고 초과분은 그대로 버린다 — 얻는 것 없이
    부하만 는다. 계측 배선이 운영 경로에 스며드는 것을 막는 경계선이라 양방향으로 고정한다.

    2026-08-06 PR #122 3차 리뷰 지적: "별도 계측 on/off 플래그 없이 상시 켜져 있습니다."
    """
    from src.agent_service.app.pipeline import _search_top_k

    assert _search_top_k(3) == 3          # 운영 — 넓히지 않는다
    with observe.observing():
        assert _search_top_k(3) >= 10     # 계측 — rag_measure_top_k 까지 넓힌다
        assert _search_top_k(20) == 20    # 이미 계측 폭 이상이면 그대로


def test_operational_entrypoints_never_set_ablation() -> None:
    """`_ABLATED` 는 프로세스 전역이므로 **운영 경로가 켤 수 있으면 안 된다.**

    ablation 은 "이 라운드는 재료를 빼고 돈다"는 라운드 속성이라 알람 단위 격리(ContextVar)를
    하지 않는다. 대신 **설정 진입점을 평가 CLI 하나로 묶는 것**이 안전장치다 — 운영 consumer 가
    같은 프로세스에서 이걸 켜면 근거 없는 리포트가 조용히 나간다(에러 없음).

    2026-08-06 PR #122 2차 리뷰가 "동일 프로세스 재사용 시 동시성 소지"를 지적해 확인한 결과
    실제 호출은 평가 CLI + 이 테스트 파일뿐이었다. 그 사실을 **고정**한다.
    """
    root = Path(__file__).resolve().parents[1]
    offenders = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "set_ablation(" in p.read_text(encoding="utf-8")
        and p.name not in ("observe.py", "eval_supervisor.py")   # 정의 · 유일한 설정 진입점
        and "tests" not in p.parts
    ]
    assert not offenders, (
        f"운영 경로가 set_ablation 을 호출한다: {offenders} — "
        "평가 CLI(eval_supervisor) 밖에서 켜면 운영 리포트가 재료 없이 나간다"
    )


# --- 검색 결정성 (Qdrant 불필요 — 정렬 함수만 본다) ---------------------------------
# 🔴 2026-08-06 실측: RRF 융합 점수는 동점이 매우 흔하고(error_manual 상위 3건이 전부 0.5),
#    Qdrant 는 그 구간의 순서를 보장하지 않아 **같은 질의를 두 번 돌리면 순서가 달라졌다.**
#    그 순서가 곧 프롬프트에 실리는 근거 카드의 순서다 → `llm.seed=42` 를 고정해도 입력이
#    매번 달라진다. "같은 조건인데 5.4%p 흔들린다"(round16↔17)의 유력한 후보였다.
def test_tie_break_is_deterministic_and_score_first() -> None:
    """점수 우선, 동점이면 식별자로 — 입력 순서가 달라도 결과가 같아야 한다."""
    from src.agent_service.vectordb import _break_ties

    a = _Pt("D", 0.5, "c3"), _Pt("D", 0.9, "c1"), _Pt("D", 0.5, "c2")
    b = _Pt("D", 0.5, "c2"), _Pt("D", 0.5, "c3"), _Pt("D", 0.9, "c1")
    ids = lambda pts: [p.payload["chunk_id"] for p in _break_ties(list(pts))]  # noqa: E731
    assert ids(a) == ["c1", "c2", "c3"]      # 0.9 먼저, 동점은 chunk_id 순
    assert ids(a) == ids(b)                  # 받은 순서가 달라도 같은 결과


def test_tie_break_keeps_the_set_intact() -> None:
    """순서만 바꾼다 — 집합·점수는 건드리지 않는다(검색 품질을 손대는 게 아니다)."""
    from src.agent_service.vectordb import _break_ties

    pts = [_Pt("A", 0.5, "c1"), _Pt("B", 0.5, "c2"), _Pt("C", 0.7, "c3")]
    out = _break_ties(pts)
    assert len(out) == 3
    assert {p.payload["chunk_id"] for p in out} == {"c1", "c2", "c3"}
    assert sorted(p.score for p in out) == [0.5, 0.5, 0.7]


def test_tie_break_survives_missing_identifiers() -> None:
    """식별자가 없는 point 도 죽지 않는다 — 계측이 파이프라인을 멈추면 안 된다."""
    from src.agent_service.vectordb import _break_ties

    class _Bare:
        payload, score, id = None, 0.5, "pid-1"

    assert len(_break_ties([_Bare(), _Pt("A", 0.5, "c1")])) == 2


# --- Supervisor 스키마 예시가 판정을 가르치지 않는가 (W56) ---------------------------
# 🔴 round18 실측: 예시에 완결된 판정 1건(`selected=limit_option`·`verdict=baseline_aging`)이
#    있었고, 가동 42건 중 20건(48%)이 그 근거 문장을 **그대로 베꼈다.** 오답 7건은 7/7 전부였다.
#    "형태 참고"라는 이름이 붙어 있어도, 완결된 답이 들어 있으면 그건 few-shot 이다.
#    ⚠️ 이 테스트가 없으면 "예시가 부실하다"며 구체값을 되넣는 것을 막을 수 없다.
def test_schema_example_does_not_pick_a_winner() -> None:
    """예시가 특정 옵션·판정을 **정답으로** 보여주면 안 된다."""
    from src.agent_service.app.supervisor import SCHEMA_EXAMPLE

    for verdict in ("baseline_aging", "process_shift", "equipment_fault"):
        assert f'"verdict": "{verdict}"' not in SCHEMA_EXAMPLE, f"예시가 {verdict} 를 정답으로 가르친다"
    for opt in ("recipe_option", "limit_option", "manual_option"):
        assert f'"selected": "{opt}"' not in SCHEMA_EXAMPLE, f"예시가 {opt} 를 승자로 가르친다"


def test_schema_example_teaches_no_confidence_value() -> None:
    """확신도 **값**을 가르치면 LLM 이 그 숫자를 복사한다 (실측: 예시 값 ↔ 최빈값 일치).

    `limit 0.85 > maint 0.79 > recipe 0.78` 이라는 예시의 서열이 그대로 판정 서열이 됐다.
    """
    import re

    from src.agent_service.app.supervisor import SCHEMA_EXAMPLE

    vals = {float(v) for v in re.findall(r'"confidence":\s*([0-9.]+)', SCHEMA_EXAMPLE)}
    assert vals <= {0.0}, f"예시가 구체적인 confidence 를 가르친다: {sorted(vals)}"


def test_schema_example_has_no_fabricated_ids() -> None:
    """예시에 실재하지 않는 인용 ID 를 두면 LLM 이 **그대로 인용한다** (CASE-0077 4건 실측)."""
    from src.agent_service.app.supervisor import SCHEMA_EXAMPLE

    assert "CASE-0077" not in SCHEMA_EXAMPLE


def test_schema_example_still_shows_the_shape() -> None:
    """중립화해도 **형태는 남아야 한다** — 헌법 6-3 이중 방어가 목적이기 때문."""
    from src.agent_service.app.supervisor import SCHEMA_EXAMPLE

    for key in ("report_id", "parallel_options", "recipe_option", "limit_option",
                "manual_option", "supervisor_recommendation", "evidence",
                "counter_evidence", "wafer_disposition", "approval_status"):
        assert f'"{key}"' in SCHEMA_EXAMPLE, f"형태에서 {key} 가 사라졌다"
    # evidence 는 계약상 정확히 3개다(EvidenceBrief min/max_length=3) — 자리도 3개여야 한다.
    # ⚠️ `"<근거` 로 세면 confidence 자리표시("<근거 강도에서 산출>")까지 잡힌다(2026-08-06 실측).
    for i in (1, 2, 3):
        assert f'"<근거 {i}' in SCHEMA_EXAMPLE, f"evidence 자리 {i} 가 없다"
    assert '"<근거 4' not in SCHEMA_EXAMPLE


# --- tool 예시가 확신도 값을 가르치지 않는가 (W58) -----------------------------------
# 🔴 실측(r18~r21 4라운드 연속): 예시가 가르친 값 ↔ 실제 최빈값이 일치했다 —
#    limit 0.85(60~69%) · maintenance 0.3(71~76%) · recipe 0.4(38~43%).
#    `limit.py` 는 아예 문장으로 *"예시 A 수준(0.85 안팎)을 쓴다"* 라고 지시하고 있었다.
#    그래서 `limit 0.85 > maint 0.79 > recipe 0.78` 서열이 그대로 판정 서열이 됐고,
#    recipe 는 재료를 아무리 채워도 천장이 0.78 이라 구조적으로 못 이겼다.
@pytest.mark.parametrize("mod", ["recipe", "limit", "maintenance"])
def test_tool_fewshot_teaches_no_confidence_value(mod) -> None:
    """정답 사례에서 **값**을 뺀다 — 예시 자체는 남긴다(대조쌍의 교육 가치는 유효)."""
    import importlib
    import re

    m = importlib.import_module(f"src.agent_service.app.tools.{mod}")
    assert not re.search(r'"confidence":\s*[0-9]', m.FEWSHOT_EXAMPLES), \
        f"{mod}: few-shot 이 confidence 값을 가르친다"


@pytest.mark.parametrize("mod", ["recipe", "limit", "maintenance"])
def test_tool_schema_example_confidence_is_placeholder(mod) -> None:
    """형태 예시의 confidence 도 자리표시여야 한다 — 값이 두 군데서 가르쳐지고 있었다."""
    import importlib
    import re

    m = importlib.import_module(f"src.agent_service.app.tools.{mod}")
    vals = {float(v) for v in re.findall(r'"confidence":\s*([0-9.]+)', m.SCHEMA_EXAMPLE)}
    assert vals <= {0.0}, f"{mod}: 스키마 예시가 구체값을 가르친다: {sorted(vals)}"


@pytest.mark.parametrize("mod", ["recipe", "limit", "maintenance"])
def test_tool_teaches_how_to_derive_confidence(mod) -> None:
    """🔴 **값을 지우기만 하면 모델이 움츠린다** (round22 실측).

    W58 로 값을 뺐더니 manual 확신이 0.65~0.75 → 0.6 대로 주저앉아 equipment_fault 가
    7/9 → 4/9 로 떨어지고 escalate 가 18 → 22 로 늘었다. **값도 침묵도 아닌 산출 방법**을 준다.
    """
    import importlib

    m = importlib.import_module(f"src.agent_service.app.tools.{mod}")
    assert "확신도(confidence) 산출" in m.SYSTEM_PROMPT, f"{mod}: 산출 규칙이 프롬프트에 없다"
    assert "근거 3층" in m.SYSTEM_PROMPT
    # 구간이 살아 있어야 한다 — 구간을 지우면 다시 '값 없음'이 되어 모델이 움츠린다(round22)
    for band in ("0.80~0.90", "0.60~0.75", "0.40~0.55", "0.20~0.35"):
        assert band in m.SYSTEM_PROMPT, f"{mod}: 산출 구간 {band} 가 없다"


@pytest.mark.parametrize("mod", ["recipe", "limit", "maintenance"])
def test_schema_placeholder_is_not_a_number(mod) -> None:
    """자리표시를 **숫자로 두면 그 숫자가 복사된다** — `0.0` 이 recipe 응답의 38% 로 나왔다."""
    import importlib
    import re

    m = importlib.import_module(f"src.agent_service.app.tools.{mod}")
    assert not re.search(r'"confidence":\s*[0-9]', m.SCHEMA_EXAMPLE), \
        f"{mod}: 스키마 예시의 confidence 가 숫자다"
    assert '"confidence": "<' in m.SCHEMA_EXAMPLE


def test_limit_no_longer_prescribes_a_number() -> None:
    """🔴 `limit.py` 는 문장으로 값을 지시하고 있었다 — 방향은 남기고 숫자만 뺀다."""
    from src.agent_service.app.tools import limit

    assert "0.85 안팎" not in limit.FEWSHOT_EXAMPLES
    assert "예시 B = 0.3" not in limit.FEWSHOT_EXAMPLES
    assert "특정 숫자를 외우지 말 것" in limit.FEWSHOT_EXAMPLES   # 방향 지침은 유지


@pytest.mark.parametrize("mod", ["recipe", "limit", "maintenance"])
def test_tool_examples_have_no_fabricated_citation_ids(mod) -> None:
    """예시의 인용 ID 는 **그대로 베껴진다** — round18 에서 CASE-0077 이 4건 나왔다."""
    import importlib

    m = importlib.import_module(f"src.agent_service.app.tools.{mod}")
    blob = m.FEWSHOT_EXAMPLES + m.SCHEMA_EXAMPLE
    for ghost in ("CASE-0077", "CASE-0182", "CASE-0211", "PK-013", "EM-042"):
        assert ghost not in blob, f"{mod}: 지어낸 인용 ID {ghost} 가 예시에 남아 있다"


# --- 판정 트리: 원인 ≠ 조치 (round19 에서 13/13 이 걸린 오독) ------------------------
# 🔴 round19 실측: 잘못 이관한 13건이 **전부** 같은 사유였다 —
#    "C31 축은 자동 튜닝이 원리적으로 금지되며..." → verdict=escalate
#    그런데 프롬프트가 실제로 그렇게 가르치고 있었다("손잡이 히트가 없으면 ② 아님 → ④").
#    헌법 1-4 의 ④는 *"원인이 ①②③ 중 아무것도 아님"* 이지 *"우리가 조치를 못 함"* 이 아니다.
#    조치 불가용 자리는 `none_executable` 로 따로 있다.
def test_prompt_separates_cause_from_actionability() -> None:
    """판정 트리가 **원인과 조치를 명시적으로 분리**해야 한다."""
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    assert "원인 판정과 조치 가능성은 다른 질문이다" in SYSTEM_PROMPT
    assert "none_executable" in SYSTEM_PROMPT


def test_tttm_split_comes_before_the_knob_branch() -> None:
    """🔴 **트리는 위에서부터 읽고 처음 확정되는 곳에서 멈춘다** — 순서가 곧 우선순위다 (round24 실측).

    STEP 1-B 의 첫 줄이 *"SHAP 이 손잡이면 → ②"* 였다. 그래서 손잡이가 잡히면 거기서 ②로
    확정하고 **아래의 TTTM 갈림에 도달하지 못했다** — `baseline_aging` 6건 중 5건이 ②로 샜고,
    그중 2건은 *"TTTM 갭이 낮아"* 라고 써놓고 ②를 골랐다(자기모순).
    W62(대전제를 STEP 0 앞으로)와 **같은 교훈** — 원칙을 아래에 두면 위 분기가 먼저 먹는다.
    """
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    b = SYSTEM_PROMPT.index("B. 급변 없음")
    tttm = SYSTEM_PROMPT.index("B-1. 먼저 TTTM 갭으로", b)
    knob = SYSTEM_PROMPT.index("B-3.", b)
    assert b < tttm < knob, "TTTM 갈림이 손잡이 분기보다 뒤에 있다 — 그러면 도달하지 않는다"


def test_prompt_warns_against_the_observed_self_contradiction() -> None:
    """실측된 자기모순을 이름으로 막는다 — *"갭이 낮다"고 쓰고 ②로 가는 것*."""
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    assert '"갭이 낮다"고 써놓고 ②로 가지 마라' in SYSTEM_PROMPT
    assert "어느 축인지는 원인을 가르지 않는다" in SYSTEM_PROMPT


def test_cause_vs_action_is_a_top_level_premise() -> None:
    """🔴 **원칙은 특정 가지가 아니라 트리 전체에 걸려야 한다** (round23 실측).

    W57 을 STEP 1-B(급변 없음) **안에** 넣었더니, N1 급변 건은 STEP 1-A 로 가서 그 지침을
    보지 못했다 — `equipment_fault` 6건이 *"자동 튜닝 불가"* 를 이유로 ④로 샜다.
    manual 리포트가 conf 0.85 로 조치를 제시하고 있는데도 그랬다.
    (round20 에서 오독이 13→9 로 '절반만' 준 것도 1-A 건들이 안 덮였기 때문이다.)
    """
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    premise = SYSTEM_PROMPT.index("대전제")
    step0 = SYSTEM_PROMPT.index("STEP 0.")
    assert premise < step0, "대전제가 STEP 0 보다 뒤에 있다 — 가지 안에 갇히면 그 가지만 덮는다"
    # 급변 가지(1-A)에도 직접 걸려 있어야 한다
    assert "손잡이가 없다\"는 ①을 부정하지 않는다" in SYSTEM_PROMPT


def test_prompt_no_longer_routes_unactionable_to_escalate() -> None:
    """옛 지시("손잡이 없으면 ② 아님 → ④")가 남아 있으면 안 된다 — 그게 13/13 의 원인이었다."""
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    assert "② 아님 → ④ escalate" not in SYSTEM_PROMPT
    assert "손잡이 히트가 전혀 없으면(압력 등 knob_param 미확정 축) ② 아님" not in SYSTEM_PROMPT


def test_prompt_splits_shift_from_aging_by_tttm() -> None:
    """②와 ③을 가르는 축이 **TTTM 갭**이라고 명시돼야 한다 (round20 부작용 교정).

    W57 을 처음 넣을 때 *"손잡이가 없어도 완만 이동이면 ②"* 로 써서 baseline_aging 4건이
    ②로 샜다. 손잡이는 **조치**를 정하는 축이지 **원인**을 가르는 축이 아니다.
    """
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    # ⚠️ 문구는 round24 뒤 재구성됐다(B-1/B-2/B-3). 검사는 **뜻**을 붙든다 —
    #    ②·③ 은 TTTM 이 가르고, 손잡이는 원인 판정에서 배제된다.
    assert "TTTM 갭으로 ②와 ③을 가른다" in SYSTEM_PROMPT
    assert "손잡이 유무는 여기서 보지 않는다" in SYSTEM_PROMPT


def test_prompt_guards_escalate_reason_at_step2() -> None:
    """마지막 관문(STEP 2)에서도 한 번 더 거른다 — 트리를 잘못 타고 와도 여기서 잡히게."""
    from src.agent_service.app.supervisor import SYSTEM_PROMPT

    assert "④를 골랐는데 그 사유가" in SYSTEM_PROMPT
    # W62 에서 표현 나열 → 패턴 인식으로 바꿨다. 실측된 표현들이 예시로 남아 있어야 한다.
    assert "조치·수단에 걸려 있으면" in SYSTEM_PROMPT
    assert "자동 튜닝 불가" in SYSTEM_PROMPT


# --- 인용 가드 접두 커버리지 (2026-08-06 round18 에서 샌 것) -------------------------
def test_citation_regex_covers_every_contract_prefix() -> None:
    """🔴 **가드 정규식의 접두 목록이 헌법 6-4 와 같아야 한다.**

    빠진 접두로 인용하면 가드가 **아예 보지 않는다** — 유령 ID 가 조용히 승인 화면까지 간다.
    round18 에서 `MAN-20260726` 이 그렇게 샜다(`MAN-` 은 실재 형식인데 정규식에 없었다).
    이 테스트가 없으면 헌법에 접두가 추가될 때마다 같은 구멍이 다시 생긴다.
    """
    from src.agent_service.app.supervisor import _ID_RE

    # 헌법 6-4 「업무 ID 포맷」 PREFIX 고정 목록 (RLS 추가 2026-08-09 — #140 해제 근거 Brief)
    for pfx in ("ALERT", "INC", "LIM", "MNT", "SUP", "QUAL", "CT", "CASE", "MAN", "RCP", "RLS"):
        assert _ID_RE.search(f"근거 [{pfx}-20260806-X-0001]"), f"{pfx} 접두가 가드에 안 잡힌다"


def test_manual_id_is_in_the_answer_key() -> None:
    """`error_manual` 은 doc_id 가 매뉴얼 **1권 단위**라 조각을 가리키는 건 `manual_id` 뿐이다.

    정답지에 없으면 **정확히 인용해도 유령으로 몰린다**(오탐) — 2026-07-20 의 PK 오탐 11건과
    같은 형태다.
    """
    from src.agent_service.app.supervisor import _REF_KEYS

    assert "manual_id" in _REF_KEYS


# --- applied_value 적재 (W21) ---------------------------------------------------------
# 승인 화면까지 실제로 닿는지는 왕복 테스트가 본다 —
#   tests/test_option_quant_roundtrip.py::test_recipe_current_values_reach_the_approval_screen
# 여기서는 **우리 쪽 적재 키**만 고정한다 (writer 가 빠뜨리면 왕복 이전에 알아야 하므로).
def test_we_do_persist_applied_value() -> None:
    """리포트 적재 키에 현재값 2종이 들어 있는가."""
    from src.agent_service.app.report_writer import _QUANT_FIELDS

    assert "applied_value" in _QUANT_FIELDS["recipe_option"]
    assert "value_current" in _QUANT_FIELDS["recipe_option"]


# --- 라운드 요약이 4축을 다 담는가 (W66) --------------------------------------------
# 🔴 2026-08-06 까지 `runs_history` 에는 ①(정확도)만 남았다. 그래서 ②③④·지연 추이를 볼 때마다
#    **일회용 스크립트로 다시 계산**했고, 그렇게 만든 추이표는 재현되지 않는다.
#    소급 채운 뒤 드러난 것: ③A(정답옵션이 확신 1위 아님)가 42% → 6% 로 크게 좋아져 있었는데
#    **요약에 없어서 하루 종일 못 봤다.** 안 남기면 안 보인다.
def _round_records():
    import json

    p = (Path(__file__).resolve().parents[1] / "notes" / "eval"
         / "supervisor_results_round25.jsonl")
    if not p.exists():
        pytest.skip("round25 결과 파일 없음 — 라운드를 돌린 환경에서만 검증")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_round_metrics_covers_every_axis() -> None:
    """요약 한 줄에 ②③④·지연·토큰이 **전부** 들어가야 한다."""
    from src.agent_service.analyze_quality import round_metrics

    m = round_metrics(_round_records())
    for key in ("err_irreversible", "err_missed_escalation",      # ②
                "hit_conf_not_top", "hit_zero_cards",             # ③
                "conf_stick_limit_pct", "conf_mode_limit",        # ③-D
                "evi_diversity_pct", "evi_placeholder_leak",      # ④
                "lat_median_sec", "lat_over_d4_pct",              # 지연
                "prompt_tokens_median", "retrieved_per_alert"):
        assert key in m, f"요약에 {key} 가 없다 — 그러면 추이를 볼 때 또 손으로 계산하게 된다"


def test_round_metrics_matches_printed_report(capsys) -> None:
    """🔴 **같은 정의를 두 벌 쓰고 있다** — 계산본과 출력본이 갈라지면 둘 다 못 믿는다.

    출력 함수가 찍는 숫자와 `round_metrics` 가 담는 숫자가 같은지 대조한다.
    """
    import re

    from src.agent_service.analyze_quality import (_scored, error_direction,
                                                   evidence_quality, round_metrics)

    recs = _round_records()
    m = round_metrics(recs)
    evidence_quality(_scored(recs))
    error_direction(_scored(recs))
    out = capsys.readouterr().out

    printed_irr = re.search(r"불가역 오답\s*:\s*(\d+)/(\d+)", out)
    assert printed_irr, "출력 형식이 바뀌었다"
    assert int(printed_irr.group(1)) == m["err_irreversible"]
    assert int(printed_irr.group(2)) == m["err_n"]

    printed_top = re.search(r"confidence 1위 아님\s*:\s*(\d+)/(\d+)", out)
    assert printed_top and int(printed_top.group(1)) == m["hit_conf_not_top"]


# --- fixture 지문 --------------------------------------------------------------
def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body, encoding="utf-8")


def test_fixture_hash_changes_when_content_changes(tmp_path) -> None:
    """🔴 **이 테스트가 fixture_hash 의 존재 이유다.**

    경로명(`alerts_v2`)은 그대로인데 안의 내용만 고치는 일이 W9 에서 반복된다. 그때
    지문이 안 바뀌면 *"round18 과 round21 이 같은 자로 잰 것인가"* 를 되물을 수 없다 —
    8/5 에 태그(`round12-realengine`)를 믿고 다섯 라운드를 잘못 읽은 것과 같은 종류다.
    """
    from src.agent_service.eval_supervisor import _fixture_stamp

    _write(tmp_path, "01_a.json", '{"x": 1}')
    before = _fixture_stamp(tmp_path)
    _write(tmp_path, "01_a.json", '{"x": 2}')          # 이름은 그대로, 내용만
    after = _fixture_stamp(tmp_path)

    assert before["fixtures"] == after["fixtures"]      # 경로명으론 구분 불가
    assert before["fixture_n"] == after["fixture_n"]    # 건수로도 구분 불가
    assert before["fixture_hash"] != after["fixture_hash"]   # 지문만이 잡는다


def test_fixture_hash_changes_when_filename_changes(tmp_path) -> None:
    """정답지가 **파일명**이라(`01_baseline_aging_*.json`) 이름만 바뀌어도 채점이 달라진다."""
    from src.agent_service.eval_supervisor import _fixture_stamp

    _write(tmp_path, "01_baseline_aging_low.json", '{"x": 1}')
    before = _fixture_stamp(tmp_path)
    (tmp_path / "01_baseline_aging_low.json").rename(tmp_path / "01_process_shift_low.json")
    assert _fixture_stamp(tmp_path)["fixture_hash"] != before["fixture_hash"]


def test_fixture_hash_is_stable_and_order_independent(tmp_path) -> None:
    """같은 내용이면 몇 번을 재도 같은 값 — 아니면 라운드 비교가 전부 '다름'이 된다."""
    from src.agent_service.eval_supervisor import _fixture_stamp

    _write(tmp_path, "b.json", '{"x": 2}')
    _write(tmp_path, "a.json", '{"x": 1}')
    assert _fixture_stamp(tmp_path)["fixture_hash"] == _fixture_stamp(tmp_path)["fixture_hash"]
    assert _fixture_stamp(tmp_path)["fixture_n"] == 2
