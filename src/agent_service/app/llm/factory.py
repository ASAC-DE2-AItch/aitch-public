"""LLM 백엔드 팩토리 — LLM_MODE 스위치 → LlmBackend 구현 선택 (C5-1).

한 곳에서만 백엔드를 만든다. tool 은 "받은 backend 를 쓸 뿐" 어느 엔진인지 모른다
(의존성 주입 — 테스트는 MockBackend, 운영은 VllmBackend 를 같은 자리에 꽂는다).

엔진별 차이는 딱 두 가지뿐이라 구현체는 VllmBackend 하나로 충분하다:
  · 주소      — AGENT_LLM_BASE_URL (Ollama :11434 / vLLM :8000)
  · guided_json 지원 여부 — vLLM 만 토큰 레벨 강제(D13). Ollama 는 프롬프트 강제로 대체.
둘 다 OpenAI 호환(/v1/chat/completions)이라 호출 코드는 동일하다.
"""

from __future__ import annotations

import logging

from ..config import LlmMode, Settings
from .client import LlmBackend
from .vllm import VllmBackend

logger = logging.getLogger(__name__)

__all__ = ["make_backend"]

# thinking 억제 (2026-07-16 실측 — C4-3 서빙 비교):
# gen_model(qwen3.6)은 thinking 모델이라 기본값이면 사고에 1000~1500 토큰을 쓰고 content 를 비운다.
# 실측: 동시 24호출 storm 에서 Ollama(억제 불가) JSON 유효율 0.0 / p95 236s vs
#       vLLM(아래 필드로 억제) JSON 유효율 1.0 / p95 13.1s.
# 이 필드는 튜닝값이 아니라 **모델 채팅 템플릿 프로토콜**이라 params.yaml 이 아닌 여기 상수로 둔다.
_VLLM_DISABLE_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


def make_backend(settings: Settings) -> LlmBackend:
    """settings 의 LLM_MODE·params 로 LLM 백엔드를 만든다.

    생성 파라미터(D10 temperature / D12 seed / D11 max_tokens / D14 timeout)를 백엔드 기본값으로
    주입한다 — tool 은 호출 시 따로 넘기지 않아도 config 정본 값이 적용된다(헌법 6-1).

    Raises:
        ValueError: AGENT_LLM_BASE_URL 미설정 — 조용한 기본값 대신 명시적 에러(params.yaml 정책).
    """
    if not settings.llm_base_url:
        raise ValueError(
            "AGENT_LLM_BASE_URL 미설정 — LLM 엔드포인트 주소가 필요합니다. "
            "(예: Ollama http://localhost:11434 / vLLM http://<ip>:8000). "
            "실주소는 infra/llm-ec2-experiment 의 terraform output 참조."
        )

    # vLLM(api) 만 guided_json(D13)·thinking 억제를 해석한다. ollama 는 둘 다 안 먹어(실측)
    # → 프롬프트 강제 + 재시도에만 의존하게 되고, 사고 토큰을 매 호출 강제로 태운다.
    is_vllm = settings.llm_mode is LlmMode.API
    extra = dict(_VLLM_DISABLE_THINKING) if is_vllm else {}

    # 모델 식별자는 **엔진마다 체계가 다르다** (2026-07-20 실측):
    #   · Ollama : 태그        `qwen3.6:35b-a3b`
    #   · vLLM   : HF repo id  `QuantTrio/Qwen3.6-35B-A3B-AWQ`
    # vLLM 에 Ollama 태그를 보내면 **404(모델 미등록)** 이고, 클라이언트는 이를 URL 오류처럼
    # 보고해 원인이 가려진다. 그래서 config 키를 분리하고 여기서 갈라 준다.
    model_id = str(settings.require("llm.vllm_model" if is_vllm else "llm.gen_model"))

    backend = VllmBackend(
        base_url=settings.llm_base_url,
        model=model_id,
        timeout=float(settings.require("llm.call_timeout_sec")),
        temperature=float(settings.require("llm.temperature")),
        seed=int(settings.require("llm.seed")),
        max_output_tokens=int(settings.require("llm.max_output_tokens")),
        guided_json_supported=is_vllm,
        extra_payload=extra,
    )
    if not is_vllm:
        logger.warning(
            "LLM_MODE=ollama — guided_json·thinking 억제 미적용. qwen3.6 은 thinking 모델이라 "
            "사고에 토큰을 소진해 JSON 이 비는 것이 실측됨(2026-07-16). 운영은 LLM_MODE=api(vLLM) 권장."
        )
    logger.info(
        "LLM 백엔드 생성: mode=%s url=%s model=%s guided_json=%s thinking_off=%s",
        settings.llm_mode.value,
        settings.llm_base_url,
        model_id,
        is_vllm,
        is_vllm,
    )
    return backend
