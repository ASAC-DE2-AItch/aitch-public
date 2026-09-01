"""LLM 구조화 호출 엔진 — JSON 강제 + 재시도 2회 + fallback (헌법 6-3).

핵심: 스키마를 두 번 적지 않는다. guided_json 은 Pydantic 모델에서 자동 생성
(model_json_schema())해 vLLM 에 넘기고, 같은 모델로 응답을 검증한다(report.py 단일 소스).

C4-2 단계: 실제 백엔드(vLLM) 미연결. LlmBackend Protocol + MockBackend(대본 주입)로
골격·재시도·fallback 경로만 완성한다. C5-1 에서 VllmBackend 를 구현해 갈아끼운다.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .prompts import build_messages

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

# --- 프롬프트 예산 (400 Bad Request 조기 경고) ----------------------------------
# vLLM `--max-model-len` 은 **프롬프트 + 생성** 합계다. 프롬프트가 그 안에 안 들어가면
# 서버가 400 을 돌려주는데, 응답 본문에 원인이 없어 "LLM 실패"로만 보인다.
#   실측 사고 3건: 2026-07-21 recipe 18/25 · 07-23 fewshot2 41/41 · 07-28 round9 42/42
# 여기서는 **토크나이저 없이** 문자수로 어림잡아 경고만 한다(런타임에 토크나이저를 올릴 수 없다).
# 계수는 params (헌법 6-4) — 재측정하면 바뀌는 값이라 코드 상수로 두지 않는다.
#   D26 llm.chars_per_token       : 문자/토큰 비 (2026-07-28 실측 역산 하한)
#   D11 llm.max_output_tokens     : 생성 여유 기본 — 호출측이 안 넘길 때만 쓴다.
#                                   구 `_DEFAULT_OUTPUT_RESERVE = 2048` 을 대체(소스 이중화 제거).
# import 시점 `require` 라 키가 없으면 즉시 명시적 에러 — 조용한 기본값 금지(6-1).
from ..config import load_settings  # noqa: E402 — 순환 없음(config 는 llm 을 임포트하지 않는다)

_PARAMS = load_settings()
_CHARS_PER_TOKEN = float(_PARAMS.require("llm.chars_per_token"))
_OUTPUT_RESERVE_DEFAULT = int(_PARAMS.require("llm.max_output_tokens"))
del _PARAMS

# 서버 `--max-model-len` 은 **인프라 값**이라 .env 가 정본이다(params 아님 — config.py 주석 규약).
_MAX_MODEL_LEN_ENV = "AGENT_LLM_MAX_MODEL_LEN"
_warned_max_model_len_unset = False


def _max_model_len() -> Optional[int]:
    """서버 max_model_len — .env 에서 읽는다. **폴백을 두지 않는다.**

    🔴 구 코드는 미설정 시 `8192` 를 가정했다. 2026-07-29 실측에서 compose 가 이 변수를
    안 넘긴 것을 아무도 모른 채 예산을 6144(=8192−2048)로 오인해, recipe 프롬프트(중위
    6,439 토큰)가 **전건 초과**로 판정되고 재시도가 붙어 max 43.8초까지 부풀었다.
    조용한 기본값이 그 사고를 만든 자리라 없앴다.

    미설정이면 검사를 **건너뛰고 1회 경고**한다 — 어림 경고가 본 호출을 막을 이유는 없으므로
    예외는 던지지 않는다(6-2). 대신 조용히 지나가지도 않게 한다.
    확인: `curl -s $AGENT_LLM_BASE_URL/v1/models | jq '.data[0].max_model_len'`
    """
    global _warned_max_model_len_unset
    raw = (os.environ.get(_MAX_MODEL_LEN_ENV) or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    if not _warned_max_model_len_unset:
        _warned_max_model_len_unset = True
        logger.warning(
            "%s 미설정(값=%r) — 프롬프트 예산 사전검사를 **건너뛴다**. vLLM 400 이 나도 원인이 "
            "로그에 안 남으니 서버 실제값으로 설정할 것 (infra variables.tf vllm_max_model_len 과 동일값).",
            _MAX_MODEL_LEN_ENV, raw,
        )
    return None


def _warn_if_over_budget(
    messages: list[dict[str, str]], model_name: str, max_output_tokens: Optional[int]
) -> bool:
    """프롬프트가 max_model_len 예산을 넘을 것으로 보이면 경고하고 True 를 돌려준다.

    어림값이라 판정에 쓰지 않는다 — **원인 특정용 신호**다. 정확한 게이트는
    `scripts/measure_prompt_budget.py`(토크나이저 실측, exit 1)가 담당한다.
    """
    max_model_len = _max_model_len()
    if max_model_len is None:
        return False                      # .env 미설정 — 검사 불가(경고는 _max_model_len 이 1회)
    chars = sum(len(m.get("content", "")) for m in messages)
    est_tokens = int(chars / _CHARS_PER_TOKEN + 0.5)   # +0.5 = 반올림 관용구(매직 넘버 아님)
    reserve = max_output_tokens or _OUTPUT_RESERVE_DEFAULT
    budget = max_model_len - reserve
    if est_tokens <= budget:
        return False
    logger.warning(
        "프롬프트 예산 초과 추정 (%s): ~%d 토큰 > 예산 %d "
        "(max_model_len %d − 출력 %d). 400 Bad Request 가 예상된다 — "
        "max_model_len 상향 또는 재료 축소 필요.",
        model_name, est_tokens, budget, max_model_len, reserve,
    )
    return True


# --- 백엔드 계약 --------------------------------------------------------------
class LlmBackend(Protocol):
    """LLM 백엔드 계약 — messages 를 받아 문자열 응답 1건을 비동기 반환.

    생성 파라미터(§0 호출 규약)는 호출 단위 kwargs 로 전달한다. None 이면 백엔드 기본값:
      guided_json: vLLM guided decoding 스키마(dict) · timeout: 호출 타임아웃(초)
      temperature(D10 0.15) · seed(D12 42) · max_output_tokens(D11 2000)
    구현체는 C5-1 의 VllmBackend / 벤치용 OllamaBackend / 테스트용 MockBackend.
    """

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        guided_json: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        temperature: Optional[float] = None,
        seed: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
    ) -> str: ...


class MockBackend:
    """대본(responses)을 순서대로 돌려주는 테스트/골격용 백엔드.

    responses 원소가 str 이면 그 문자열을, Exception 이면 raise 한다 —
    "첫 응답은 깨진 JSON, 두 번째는 정상" 같은 재시도 시나리오를 재현하기 위함.
    """

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []  # 호출별 messages 기록(검증용)
        self.guided: list[Optional[dict[str, Any]]] = []  # 호출별 guided_json 기록
        self.gen_params: list[dict[str, Any]] = []  # 호출별 생성 파라미터 기록

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
        self.calls.append(messages)
        self.guided.append(guided_json)
        self.gen_params.append(
            {"temperature": temperature, "seed": seed, "max_output_tokens": max_output_tokens}
        )
        if not self._responses:
            raise RuntimeError("MockBackend 대본 소진 — 예상보다 많이 호출됨")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --- 예외 서술 ----------------------------------------------------------------
# 2026-07-21 실측: 스팟 인스턴스 회수로 엔드포인트가 사라졌을 때 로그가 `last_error=` 로만
#   찍혔다. httpx 의 연결/타임아웃 예외는 str(exc) 가 빈 문자열이라 %s 가 아무것도 못 남긴 것.
#   원인 판별에 30분이 든 원인이므로 **예외 종류를 항상 남긴다**.
def _describe(exc: BaseException) -> str:
    """예외를 '종류: 메시지' 로 서술한다. 메시지가 비어도 종류는 반드시 남는다."""
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


# --- JSON 추출 ----------------------------------------------------------------
def _extract_json(raw: str) -> Any:
    """응답 문자열에서 JSON 파싱. 방어적으로 마크다운 코드펜스(```json)를 제거한다.

    guided_json 이 켜지면 펜스는 안 나오지만, ollama/폴백 경로 대비 이중 방어.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):  # ``` 또는 ```json
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


# --- 구조화 호출(재시도 + fallback) -------------------------------------------
async def generate_structured(
    backend: LlmBackend,
    system_prompt: str,
    user_payload: str,
    model_cls: type[M],
    *,
    fallback: Callable[[], M],
    schema_example: str = "",
    parse_retries: int = 2,
    call_retries: int = 1,
    timeout: Optional[float] = None,
    temperature: Optional[float] = None,
    seed: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
) -> M:
    """LLM 을 호출해 model_cls 인스턴스를 반환. 실패 시 재시도, 소진 시 fallback.

    재시도 예산은 둘로 분리한다 (params.yaml — call_retry 는 D3 파싱 재시도와 '별개'):
      · 호출 실패(타임아웃·연결) → call_retries 회 재시도 (llm.call_retry, 기본 1)
      · 파싱/검증 실패(깨진 JSON·스키마 위반) → parse_retries 회 재시도 (agent.json_parse_retry, 기본 2)
    두 예산 중 하나라도 소진되면 fallback() 리포트를 반환한다 — 파이프라인은 죽지 않는다(헌법 6-3).
    파싱 재시도에만 RETRY_SUFFIX("직전 응답 파싱 실패")를 붙인다 — 호출 실패는 직전 응답이 없으므로.

    Args:
        backend: LlmBackend 구현.
        system_prompt: 툴별 역할 프롬프트 (§0 공통블록은 build_messages 가 앞에 붙임).
        user_payload: 입력 직렬화 문자열.
        model_cls: 응답을 검증할 Pydantic 리포트 클래스. guided_json 스키마도 여기서 생성.
        fallback: 예산 소진 시 반환할 리포트를 만드는 무인자 콜러블 (§6 — report.make_fallback_report).
        schema_example: 프롬프트에 중복 탑재할 JSON 예시(이중 방어).
        parse_retries: 파싱/검증 실패 재시도 횟수 (params.yaml agent.json_parse_retry, 기본 2).
        call_retries: 호출 실패 재시도 횟수 (params.yaml llm.call_retry, 기본 1).
        timeout: 백엔드 호출 타임아웃(초). None 이면 백엔드 기본.
        temperature/seed/max_output_tokens: 생성 파라미터 (§0 호출 규약 — D10/D12/D11).
            None 이면 백엔드 기본. C5-1 에서 tool 이 settings 값을 읽어 전달한다.
    """
    guided = model_cls.model_json_schema()
    call_fails = 0
    parse_fails = 0
    last_error: Optional[str] = None
    over_budget = False          # 프롬프트 예산 초과 추정 — 400 의 원인 특정용

    while True:
        # 직전 '응답'이 있었을 때(=파싱 실패)만 재시도 지시를 붙인다. 호출 실패엔 붙이지 않는다.
        messages = build_messages(
            system_prompt, user_payload, schema_example=schema_example, retry=(parse_fails > 0)
        )
        # 프롬프트 예산 사전 검사 — 초과하면 vLLM 이 **400** 을 돌려주는데 그 응답에는
        # 원인이 안 실려 "LLM 실패"로만 보인다. 세 번 당했다(2026-07-21 recipe 18/25 ·
        # 07-23 fewshot2 41/41 · 07-28 round9 42/42) — 전부 EC2 를 켜고 전건 돌린 뒤에 발견.
        # 여기서 미리 경고해 400 의 원인을 즉시 특정한다. 정밀 측정은
        # `scripts/measure_prompt_budget.py`(토크나이저 실측·CI 게이트)가 담당한다.
        if call_fails == 0 and parse_fails == 0:
            over_budget = _warn_if_over_budget(messages, model_cls.__name__, max_output_tokens)
        try:
            raw = await backend.complete(
                messages,
                guided_json=guided,
                timeout=timeout,
                temperature=temperature,
                seed=seed,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:  # 호출 자체 실패(타임아웃·연결 등) — call 예산
            last_error = _describe(exc)
            call_fails += 1
            logger.warning(
                "LLM 호출 실패 (call %d/%d, %s): %s",
                call_fails, call_retries, model_cls.__name__, last_error,
            )
            if call_fails > call_retries:
                break
            continue
        try:
            return model_cls.model_validate(_extract_json(raw))
        except (json.JSONDecodeError, ValidationError) as exc:  # 파싱/검증 실패 — parse 예산
            last_error = _describe(exc)
            parse_fails += 1
            logger.warning(
                "JSON 파싱/검증 실패 (parse %d/%d, %s): %s",
                parse_fails, parse_retries, model_cls.__name__, last_error,
            )
            if parse_fails > parse_retries:
                break
            continue

    hint = ""
    if over_budget and call_fails and not parse_fails:
        # 호출만 실패(파싱 시도조차 못 함) + 예산 초과 추정 = 400 의 전형적 모양.
        hint = (" ⚠️ 프롬프트 예산 초과 추정 — vLLM max_model_len 상향 또는 재료 축소 필요."
                " `python -m src.agent_service.scripts.measure_prompt_budget` 로 확인할 것.")
    logger.error(
        "LLM 구조화 응답 실패 → fallback (%s). call_fails=%d parse_fails=%d last_error=%s%s",
        model_cls.__name__, call_fails, parse_fails, last_error, hint,
    )
    return fallback()
