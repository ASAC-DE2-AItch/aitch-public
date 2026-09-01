"""LLM JSON 강제 + 재시도 2회 + fallback 골격 테스트 (C4-2 · 헌법 6-3).

LLM 미연결 단계이므로 MockBackend(대본 주입)로 경로만 잠근다:
  · build_messages — §0 공통블록·툴 프롬프트·JSON 예시·재시도 지시 조립
  · guided_json = 리포트 모델의 json_schema 가 백엔드로 전달
  · 성공(1회) / 재시도 후 성공(2회) / 3회 실패 → fallback(§6)
  · 백엔드 호출 예외(타임아웃 등)도 재시도 대상
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app.llm import (  # noqa: E402
    MockBackend,
    build_messages,
    generate_structured,
)
from agent_service.app.llm.client import _extract_json  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402
from agent_service.app.schemas.report import (  # noqa: E402
    LimitOption,
    make_fallback_report,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"


def _alert() -> AlertModel:
    data = json.loads((FIXTURES / "01_baseline_aging_below_gate.json").read_text("utf-8"))
    return AlertModel.model_validate(data)


def _valid_limit_json() -> str:
    return json.dumps({
        "report_id": "LIM-20260713-SIMCH3-0412",
        "option_type": "limit_option",
        "confidence": 0.88,
        "rationale": "추세 룰 위주, 급변 없음 [SPC]",
        "uncertainty": "재산정 창 치우침",
    })


def _fallback():
    return make_fallback_report(LimitOption, _alert())


# --- build_messages -----------------------------------------------------------
def test_build_messages_includes_system_block_and_example() -> None:
    msgs = build_messages("[역할] 실력치 재설정", "alert payload", schema_example='{"a":1}')
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    sys_text = msgs[0]["content"]
    assert "FDC 분석 엔지니어 보조 AI" in sys_text  # §0 공통블록
    assert "[역할] 실력치 재설정" in sys_text       # 툴 프롬프트
    assert '{"a":1}' in sys_text                     # JSON 예시(이중 방어)
    assert msgs[1]["content"] == "alert payload"


def test_build_messages_retry_adds_suffix() -> None:
    base = build_messages("role", "p")[0]["content"]
    retry = build_messages("role", "p", retry=True)[0]["content"]
    assert "직전 응답은 JSON 파싱에 실패" in retry
    assert "직전 응답은 JSON 파싱에 실패" not in base


# --- generate_structured ------------------------------------------------------
def test_success_first_try() -> None:
    backend = MockBackend([_valid_limit_json()])
    rep = asyncio.run(generate_structured(
        backend, "role", "payload", LimitOption, fallback=_fallback
    ))
    assert isinstance(rep, LimitOption)
    assert rep.confidence == 0.88
    assert len(backend.calls) == 1  # 재시도 없음


def test_guided_json_is_model_schema() -> None:
    """guided_json 으로 리포트 모델의 json_schema 가 백엔드에 전달돼야 한다."""
    backend = MockBackend([_valid_limit_json()])
    asyncio.run(generate_structured(backend, "role", "p", LimitOption, fallback=_fallback))
    assert backend.guided[0] == LimitOption.model_json_schema()


def test_generation_params_forwarded() -> None:
    """temperature·seed·max_output_tokens 가 백엔드 호출까지 전달돼야 한다 (§0 호출 규약)."""
    backend = MockBackend([_valid_limit_json()])
    asyncio.run(generate_structured(
        backend, "role", "p", LimitOption, fallback=_fallback,
        temperature=0.15, seed=42, max_output_tokens=2000,
    ))
    assert backend.gen_params[0] == {"temperature": 0.15, "seed": 42, "max_output_tokens": 2000}


def test_retry_then_success() -> None:
    """1회 깨진 JSON → 2회차 정상. 2번째 호출엔 재시도 지시가 붙는다."""
    backend = MockBackend(["not json{", _valid_limit_json()])
    rep = asyncio.run(generate_structured(
        backend, "role", "payload", LimitOption, fallback=_fallback
    ))
    assert isinstance(rep, LimitOption)
    assert len(backend.calls) == 2
    assert "직전 응답은 JSON 파싱에 실패" in backend.calls[1][0]["content"]


def test_validation_error_triggers_retry() -> None:
    """JSON 은 되지만 스키마 위반(confidence>1)이면 재시도 사유."""
    bad = json.dumps({"report_id": "LIM-x", "confidence": 5.0,
                      "rationale": "r", "uncertainty": "u"})
    backend = MockBackend([bad, _valid_limit_json()])
    rep = asyncio.run(generate_structured(
        backend, "role", "p", LimitOption, fallback=_fallback
    ))
    assert isinstance(rep, LimitOption) and rep.confidence == 0.88
    assert len(backend.calls) == 2


def test_parse_failures_return_fallback() -> None:
    """파싱 최초 1 + 재시도 2 = 3회 모두 실패 → fallback 리포트(§6)."""
    backend = MockBackend(["bad", "still bad", "nope"])
    rep = asyncio.run(generate_structured(
        backend, "role", "payload", LimitOption, fallback=_fallback, parse_retries=2
    ))
    assert isinstance(rep, LimitOption)
    assert rep.confidence == 0.0
    assert rep.escalate_reason is not None and "LLM 분석 실패" in rep.escalate_reason
    assert len(backend.calls) == 3  # 무한 재시도 아님


def test_backend_exception_is_retried() -> None:
    """호출 자체 예외(타임아웃 등)도 재시도 대상 (call_retries 기본 1 → 1회 재호출)."""
    backend = MockBackend([TimeoutError("call timeout"), _valid_limit_json()])
    rep = asyncio.run(generate_structured(
        backend, "role", "p", LimitOption, fallback=_fallback
    ))
    assert isinstance(rep, LimitOption) and rep.confidence == 0.88
    assert len(backend.calls) == 2


def test_call_retries_are_separate_from_parse() -> None:
    """호출 예산(call_retries=1)은 파싱 예산과 별개 — 타임아웃 2연발이면 fallback.

    params.yaml: llm.call_retry=1 (D3 파싱 재시도와 별개). 타임아웃이 파싱 예산을 까먹지 않고,
    call_retries=1 이므로 최초+재시도 1 = 2회 호출 후 fallback.
    """
    backend = MockBackend([
        TimeoutError("t1"), TimeoutError("t2"), _valid_limit_json(),
    ])
    rep = asyncio.run(generate_structured(
        backend, "role", "p", LimitOption, fallback=_fallback, call_retries=1
    ))
    assert rep.confidence == 0.0  # fallback (세 번째 정상 응답까지 못 감)
    assert len(backend.calls) == 2


# --- 프롬프트 예산 가드 (400 조기 경고) — 2026-07-28 -----------------------------
# 사고 3건: 07-21 recipe 18/25 · 07-23 fewshot2 41/41 · 07-28 round9 42/42.
# 전부 vLLM max_model_len 초과 → 400 인데, 응답에 원인이 없어 "LLM 실패"로만 보였다.
#
# ⚠️ 2026-07-30: 예산 검사는 `AGENT_LLM_MAX_MODEL_LEN`(.env)이 있을 때만 돈다 — 구 코드의
#    `8192` 폴백을 제거했기 때문(그 폴백이 7/29 사고 원인). 아래 테스트들은 그동안 **그 폴백에
#    조용히 의존**하고 있었고, 값이 없는 환경(CI)에서 검사가 skip 되며 실패했다. 이제 검사할
#    예산을 테스트가 **명시적으로 선언**한다 — 주변 환경에 따라 결과가 달라지지 않게.
_TEST_MAX_MODEL_LEN = "8192"    # 구 폴백과 같은 값 — "27k 토큰은 어떤 예산도 초과" 전제 유지


def test_over_budget_warns_before_call(caplog, monkeypatch) -> None:
    """예산을 넘는 프롬프트면 호출 **전에** 경고한다 — 400 의 원인을 즉시 특정."""
    import logging

    from agent_service.app.llm import client as C

    monkeypatch.setenv("AGENT_LLM_MAX_MODEL_LEN", _TEST_MAX_MODEL_LEN)
    long_payload = "가" * 60_000        # ≈27k 토큰(2.2자/토큰) — 어떤 예산도 초과
    backend = MockBackend([_valid_limit_json()])
    with caplog.at_level(logging.WARNING, logger=C.__name__):
        asyncio.run(generate_structured(
            backend, "role", long_payload, LimitOption, fallback=_fallback
        ))
    assert any("프롬프트 예산 초과 추정" in r.message for r in caplog.records)


def test_within_budget_does_not_warn(caplog, monkeypatch) -> None:
    """정상 길이엔 경고하지 않는다 — 오탐이 나면 경고가 무시된다.

    env 를 명시하는 이유: 미설정이면 검사 자체가 skip 되어 **이유가 다른데 통과**한다(위장 통과).
    """
    import logging

    from agent_service.app.llm import client as C

    monkeypatch.setenv("AGENT_LLM_MAX_MODEL_LEN", _TEST_MAX_MODEL_LEN)
    backend = MockBackend([_valid_limit_json()])
    with caplog.at_level(logging.WARNING, logger=C.__name__):
        asyncio.run(generate_structured(backend, "role", "짧은 입력", LimitOption,
                                        fallback=_fallback))
    assert not any("예산 초과" in r.message for r in caplog.records)


def test_over_budget_call_failure_adds_hint(caplog, monkeypatch) -> None:
    """예산 초과 + 호출만 실패(파싱 시도 0) = 400 의 전형 → fallback 로그에 원인 힌트."""
    import logging

    from agent_service.app.llm import client as C

    monkeypatch.setenv("AGENT_LLM_MAX_MODEL_LEN", _TEST_MAX_MODEL_LEN)
    long_payload = "가" * 60_000
    backend = MockBackend([RuntimeError("400 Bad Request"), RuntimeError("400 Bad Request")])
    with caplog.at_level(logging.ERROR, logger=C.__name__):
        rep = asyncio.run(generate_structured(
            backend, "role", long_payload, LimitOption, fallback=_fallback, call_retries=1
        ))
    assert rep.confidence == 0.0                                    # fallback
    assert any("프롬프트 예산 초과 추정" in r.message for r in caplog.records)


def test_max_model_len_unset_skips_check_but_warns(caplog, monkeypatch) -> None:
    """env 미설정이면 검사를 건너뛰되 **조용히 넘어가지 않는다** (2026-07-30).

    구 코드는 미설정 시 `8192` 를 가정했다. compose 가 변수를 안 넘긴 것을 아무도 모른 채
    예산을 6144 로 오인해 recipe 가 전건 초과 판정되고 재시도로 max 43.8초까지 부풀었다.
    폴백을 없앤 대신 **1회 경고**로 미설정 자체를 드러낸다 — 호출은 막지 않는다(6-2).
    """
    import logging

    from agent_service.app.llm import client as C

    monkeypatch.delenv("AGENT_LLM_MAX_MODEL_LEN", raising=False)
    monkeypatch.setattr(C, "_warned_max_model_len_unset", False)  # 모듈 1회 플래그 초기화
    long_payload = "가" * 60_000        # 예산이 있었다면 확실히 초과할 길이
    backend = MockBackend([_valid_limit_json()])
    with caplog.at_level(logging.WARNING, logger=C.__name__):
        asyncio.run(generate_structured(
            backend, "role", long_payload, LimitOption, fallback=_fallback
        ))
    msgs = [r.message for r in caplog.records]
    assert not any("예산 초과 추정" in m for m in msgs)          # 검사는 skip
    assert any("AGENT_LLM_MAX_MODEL_LEN" in m for m in msgs)      # 미설정은 드러난다


def test_fallback_report_shape() -> None:
    """§6 fallback: action 계열 None·confidence 0·escalate_reason 존재."""
    rep = make_fallback_report(LimitOption, _alert())
    assert rep.confidence == 0.0
    assert rep.escalate_reason is not None
    assert rep.evidence_cards == []


# --- _extract_json 방어 -------------------------------------------------------
def test_extract_json_strips_markdown_fence() -> None:
    fenced = '```json\n{"a": 1}\n```'
    assert _extract_json(fenced) == {"a": 1}


def test_extract_json_plain() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}
