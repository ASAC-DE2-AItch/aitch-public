"""LLM JSON 강제·재시도·fallback 골격 (C4-2 · 헌법 6-3).

정본: docs/Agent_프롬프트_라이브러리_v1.md §0(공통블록)·§6(fallback).
이 패키지는 "LLM을 어떻게 부르고, 응답을 어떻게 JSON으로 강제하고, 실패하면 어떻게
무중단으로 넘어가는가"의 골격만 담는다. 실제 LLM 백엔드(vLLM) 연결은 C5-1.

  · prompts.py — §0 공통 시스템블록 + JSON 지시/예시 + 재시도 지시 조립
  · client.py  — LlmBackend Protocol · MockBackend(골격/테스트) · generate_structured(재시도+fallback)
  · vllm.py    — VllmBackend: LlmBackend 의 실제 구현 (vLLM/Ollama 등 OpenAI 호환 엔드포인트 호출)
"""

from __future__ import annotations

from .client import (
    LlmBackend,
    MockBackend,
    generate_structured,
)
from .prompts import (
    SYSTEM_BLOCK_V1,
    build_messages,
)
from .vllm import VllmBackend

__all__ = [
    "LlmBackend",
    "MockBackend",
    "VllmBackend",
    "generate_structured",
    "SYSTEM_BLOCK_V1",
    "build_messages",
]
