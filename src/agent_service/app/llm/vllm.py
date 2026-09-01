"""VllmBackend — LlmBackend 계약의 실제 구현 (vLLM OpenAI 호환 서버 호출, C5-1).

client.py 의 LlmBackend Protocol 을 만족하는 프로덕션 백엔드. vLLM 의 OpenAI 호환
엔드포인트(`/v1/chat/completions`)에 httpx 로 비동기 POST 하고, 응답 문자열 1건을 돌려준다.
guided_json 은 vLLM 확장 필드로 그대로 실어 토큰 레벨 스키마 강제(D13)를 건다.

경계:
- 이 클래스는 "요청 조립 + 응답 추출"만 안다. 재시도·fallback·스키마 검증은 generate_structured 소관.
- base_url(엔드포인트)은 인프라 값 — 생성자 인자로 주입받는다(팩토리가 env/deploy 에서 해석).
- httpx 클라이언트는 주입 가능(테스트는 httpx.MockTransport 로 라이브 서버 없이 검증).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# 호출 타임아웃 기본값 — 실운영 값은 params.yaml llm.call_timeout_sec(D14). 팩토리가 주입.
# 여기 상수는 단독 사용(테스트·직접 생성) 시의 폴백일 뿐, 판정 로직 매직넘버가 아니다.
_DEFAULT_TIMEOUT_SEC = 20.0

# vLLM OpenAI 호환 채팅 완성 경로.
_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


class VllmBackend:
    """vLLM(OpenAI 호환) 백엔드 — messages → 응답 문자열 1건 (LlmBackend 구현).

    Args:
        base_url: vLLM 서버 베이스 URL (예: "http://<host>:8000"). client 를 주입하면 무시.
        model: 서빙 모델명 (params.yaml llm.gen_model — 예: qwen3.6). 요청 body 의 "model".
        timeout: 호출 타임아웃(초). 호출별 override 가 없을 때의 기본.
        temperature/seed/max_output_tokens: 생성 파라미터 기본값(§0 D10/D12/D11).
            호출별 인자가 None 이면 이 기본을 쓰고, 그것도 None 이면 요청에서 생략(서버 기본).
        guided_json_supported: False 면 guided_json 을 요청에서 **제외**한다.
            Ollama 의 OpenAI 호환 경로는 vLLM 확장 필드를 모르므로 그대로 실으면 거부될 수 있다.
            이때 JSON 강제는 프롬프트 지시 + `client._extract_json` + 재시도가 담당한다(헌법 6-3 이중 방어).
        extra_payload: 요청 본문에 그대로 병합할 엔진별 추가 필드.
            **thinking 억제용** — qwen3.6 은 thinking 모델이라 기본값이면 사고과정에 1000~1500 토큰을
            쓰고 content 를 비운 채 토큰이 소진된다(2026-07-16 실측: max_tokens=900 → content='').
            vLLM: {"chat_template_kwargs": {"enable_thinking": false}}
            Ollama(/v1): 억제 수단이 먹지 않아 max_tokens 를 넉넉히 주는 것 외엔 방법이 없다(실측).
        client: 주입용 httpx.AsyncClient (테스트: MockTransport). None 이면 첫 호출 시 생성.
    """

    def __init__(
        self,
        base_url: Optional[str],
        model: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SEC,
        temperature: Optional[float] = None,
        seed: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        guided_json_supported: bool = True,
        extra_payload: Optional[dict[str, Any]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._model = model
        self._default_timeout = timeout
        self._default_temperature = temperature
        self._default_seed = seed
        self._default_max_output_tokens = max_output_tokens
        self._guided_json_supported = guided_json_supported
        self._extra_payload = dict(extra_payload or {})
        self._client = client

    def _get_client(self) -> httpx.AsyncClient:
        """httpx 클라이언트 반환 — 주입본이 없으면 base_url 로 지연 생성."""
        if self._client is None:
            if not self._base_url:
                raise ValueError(
                    "VllmBackend base_url 미설정(null). vLLM 엔드포인트 확정 후 주입 필요 "
                    "(env AGENT_LLM_BASE_URL / deploy.yaml). — 조용한 기본값 금지."
                )
            self._client = httpx.AsyncClient(base_url=self._base_url)
        return self._client

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        guided_json: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        temperature: Optional[float] = None,
        seed: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
    ) -> str:
        """vLLM 에 채팅 완성 1건 요청 → 응답 content 문자열 반환.

        호출별 생성 파라미터가 None 이면 생성자 기본값으로 대체하고, 둘 다 None 이면
        요청 body 에서 생략한다(서버 기본 사용). guided_json 은 있으면 그대로 실어 스키마 강제.
        비 2xx 응답은 raise_for_status 로 예외 → generate_structured 가 호출 실패로 처리.
        """
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        payload.update(self._extra_payload)  # 엔진별 추가 필드(예: thinking 억제)

        temp = temperature if temperature is not None else self._default_temperature
        if temp is not None:
            payload["temperature"] = temp
        sd = seed if seed is not None else self._default_seed
        if sd is not None:
            payload["seed"] = sd
        mx = max_output_tokens if max_output_tokens is not None else self._default_max_output_tokens
        if mx is not None:
            payload["max_tokens"] = mx  # OpenAI 호환 필드명
        if guided_json is not None and self._guided_json_supported:
            payload["guided_json"] = guided_json  # vLLM 확장 — 토큰 레벨 스키마 강제(D13)
        elif guided_json is not None:
            # Ollama 등 미지원 백엔드 — 조용히 빼되, 강제 수단이 프롬프트뿐임을 로그로 남긴다.
            logger.debug("guided_json 미지원 백엔드 — 프롬프트 강제로 대체 (model=%s)", self._model)

        to = timeout if timeout is not None else self._default_timeout
        client = self._get_client()
        resp = await client.post(_CHAT_COMPLETIONS_PATH, json=payload, timeout=to)
        resp.raise_for_status()
        data = resp.json()
        # 서버가 센 실측 토큰 (OpenAI 호환 `usage`) — 그동안 통째로 버리고 있었다.
        # `measure_prompt_budget` 은 **보내기 전 추정치**라, 둘이 갈라지면 예산 계산이 틀린 것이다.
        # 평가 하네스가 켰을 때만 담긴다(운영은 no-op).
        from ..observe import record_llm

        record_llm(data.get("usage"), model=self._model)
        return data["choices"][0]["message"]["content"]

    async def aclose(self) -> None:
        """지연 생성한 httpx 클라이언트 정리 (graceful shutdown 시 호출)."""
        if self._client is not None:
            await self._client.aclose()
