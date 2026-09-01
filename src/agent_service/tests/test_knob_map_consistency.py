# -*- coding: utf-8 -*-
"""손잡이 정본 3자 정합 — Qdrant 카드 · B yaml · 우리 프롬프트 (W17·W18, 2026-08-04).

**왜 이 테스트가 있나.** 조정 가능한 손잡이가 세 곳에 흩어져 있다:

  ① `config/recipe_knob_map.yaml`  — B 소유 **정본**. `status: active` 인 축의 knobs.
  ② Qdrant `tuning_axis` 카드      — 우리가 LLM 에게 보내는 재료(`knob_param`).
                                      런타임 화이트리스트(`recipe.allowed_knobs`)의 소스이기도 하다.
  ③ 프롬프트 문장                   — LLM 에게 주는 규칙.

②가 ①과 어긋나면 **B 가 낸 정당한 손잡이를 우리 가드가 유령으로 강등**하거나, 반대로
**B 가 escalate 할 축을 LLM 이 조정하라고 제안**한다. 실제로 2026-08-04 W24 대조에서
그 상태였다 — yaml 은 D11(2026-07-30 PM 승인)로 C1·C5·C48·C41 을 escalate_only 로
내렸는데 카드는 여전히 "손잡이"로 가르치고 있었다(카드 3장).

③은 값을 박지 않는 것이 원칙이다(W17) — 목록을 프롬프트에 쓰면 정본이 바뀌어도 안 따라온다.
그래서 여기서는 **"프롬프트에 손잡이 C코드 목록이 되살아나지 않았는지"** 를 본다.

⚠️ 이 테스트는 **Qdrant 를 띄우지 않는다.** 카드의 소스인 seed JSONL 을 직접 읽는다 —
적재는 그 파일을 그대로 upsert 하므로 같은 값이고, CI 에서 벡터 DB 의존을 만들지 않기 위해서다.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from src.agent_service.app.tools.recipe import allowed_knobs

REPO = Path(__file__).resolve().parents[3]
YAML_PATH = REPO / "config" / "recipe_knob_map.yaml"
SEED_PATH = (REPO / "src" / "agent_service" / "rag_kb" / "process_knowledge"
             / "process_knowledge_tuning_seed.jsonl")


def _yaml_active_knobs() -> set[str]:
    """B 정본에서 `status: active` 축의 손잡이만."""
    km = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    out: set[str] = set()
    for ax in (km.get("axes") or {}).values():
        if ax.get("status") == "active":
            out.update(ax.get("knobs") or [])
    return out


def _card_records() -> list[dict]:
    return [json.loads(line) for line in SEED_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_card_whitelist_matches_yaml_active_knobs() -> None:
    """카드에서 뽑은 런타임 화이트리스트 == yaml 의 active 손잡이.

    어긋나면 가드와 B 엔진의 판정이 갈린다 — 이 테스트가 그 순간 실패한다.
    """
    cards = [{"knob_param": r["knob_param"]} for r in _card_records()]
    assert allowed_knobs(cards) == _yaml_active_knobs()


def test_escalate_only_axes_are_not_in_whitelist() -> None:
    """yaml 이 escalate_only 로 내린 손잡이는 화이트리스트에 없어야 한다.

    `knob_param='에스컬레이션'` 마커가 C코드를 품으면 `allowed_knobs` 의 정규식에 걸려
    되살아난다 — 그 실수를 여기서 잡는다.
    """
    km = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    escalate: set[str] = set()
    for ax in (km.get("axes") or {}).values():
        if ax.get("status") == "escalate_only":
            escalate.update(ax.get("knobs") or [])
    assert escalate, "yaml 에 escalate_only 축이 없다 — 테스트 전제가 바뀌었으니 확인 필요"
    cards = [{"knob_param": r["knob_param"]} for r in _card_records()]
    assert not (allowed_knobs(cards) & escalate)


def test_forbidden_sensors_are_not_knobs() -> None:
    """yaml `forbidden`(레짐 도장·모니터·타겟)은 어떤 경로로도 손잡이가 되면 안 된다."""
    km = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    forbidden = set(km.get("forbidden") or [])
    cards = [{"knob_param": r["knob_param"]} for r in _card_records()]
    assert not (allowed_knobs(cards) & forbidden)


def test_prompt_does_not_hardcode_knob_list() -> None:
    """프롬프트에 손잡이 C코드 목록이 되살아나지 않았는지 (W17 회귀 방지).

    값을 프롬프트에 박으면 정본이 바뀌어도 따라오지 않는다 — 2026-08-04 에 실제로
    `C4/C5·C1·C41` 이 박혀 있었고 그중 셋은 이미 폐기된 손잡이였다.
    `knob_map` 카드가 런타임에 통째로 실려 가므로 프롬프트에 목록을 쓸 이유가 없다.
    """
    from src.agent_service.app import supervisor
    from src.agent_service.app.llm import prompts
    from src.agent_service.app.tools import recipe

    escalated = {"C1", "C5", "C48", "C41"}          # D11 로 내려간 손잡이들
    # 규칙·지시 문장만 본다(few-shot 대조쌍 `FEWSHOT_EXAMPLES` 는 배선 OFF — 켤 때 별도 검토).
    # ⚠️ **Supervisor 를 빠뜨리지 말 것** — 2026-08-04 초안이 recipe·공통만 검사해서
    #    `supervisor.SYSTEM_PROMPT` 판정 트리 B 가지의 손잡이 목록(가스 C4/C5·파워 C1·시간 C41)을
    #    놓쳤다. 판정 트리는 ②(Recipe 튜닝)로 보낼지를 그 목록으로 갈랐다.
    texts = {
        "prompts.SYSTEM_BLOCK_V1": prompts.SYSTEM_BLOCK_V1,
        "recipe.SYSTEM_PROMPT": recipe.SYSTEM_PROMPT,
        "supervisor.SYSTEM_PROMPT": supervisor.SYSTEM_PROMPT,
    }
    # ⚠️ getattr 기본값으로 받으면 이름이 바뀐 순간 **빈 문자열이 되어 조용히 통과**한다
    #    (실제로 초안이 그랬다 — 존재하지 않는 SYSTEM_RULES/ROLE_PROMPT 를 봤다).
    #    직접 참조해 AttributeError 로 터지게 두고, 비어 있지 않은 것도 확인한다.
    # ⚠️ 부분 문자열로 찾으면 `C1` 이 `C12`(레짐 도장)에 걸린다 — 실제로 초안이 그 오탐을 냈다.
    #    단어 경계로 본다: `C1` 뒤에 숫자가 오면 다른 센서다.
    import re as _re

    for name, text in texts.items():
        assert text.strip(), f"{name} 이 비어 있다 — 검사 대상이 사라졌다"
        hit = {c for c in escalated if _re.search(rf"\b{c}\b", text)}
        assert not hit, f"{name} 에 폐기된 손잡이가 박혀 있다: {sorted(hit)}"
