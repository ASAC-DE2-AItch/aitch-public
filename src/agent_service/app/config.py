"""Agent Service 설정 — 스위치 3종 + params.yaml 로더.

헌법 6-1: 하드코딩·매직 넘버 금지 → 모든 파라미터는 config/params.yaml 단일 소스.
이 모듈은 ⓐ 3개 운용 스위치(.env)와 ⓑ params.yaml 로딩만 담당한다. 값 자체를 정의하지 않는다.

스위치 3종 (뼈대 단계 — 각 계층을 mock↔real 로 갈아끼우기 위한 경계):
  1. SOURCE_MODE  : replay | kafka   — alert 입력 소스 (fixtures 재생 / 실제 Kafka 구독)
  2. API_MODE     : mock   | real    — B 실력치 엔진·A 예측 등 외부 API (스텁 / 실호출)
  3. LLM_MODE     : ollama | api     — 생성 LLM 백엔드 (D21: 벤치 Ollama / 운영 vLLM guided_json)

엔드포인트(AGENT_LLM_BASE_URL)는 배포마다 바뀌는 인프라 값이라 params.yaml(알고리즘 값)이 아니라
.env 로 둔다 — deploy.yaml/Terraform 이 실주소의 소스(infra/llm-ec2-experiment outputs).

미확정(null) 값 정책(params.yaml 헤더): 코드는 null 을 만나면 조용한 기본값 대신 명시적 에러.
→ require_param() 헬퍼 제공.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

try:  # .env 가 있으면 로드 (없어도 동작)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv 미설치 환경도 허용
    pass


# --- 경로 (repo 루트 기준) -----------------------------------------------------
# 이 파일: src/agent_service/app/config.py → parents[3] = repo 루트
REPO_ROOT = Path(__file__).resolve().parents[3]   # 3 = 디렉토리 깊이(app→agent_service→src→repo). 튜닝값이 아니라 파일 위치라 params 대상 아님
SERVICE_ROOT = Path(__file__).resolve().parents[1]  # src/agent_service

PARAMS_PATH = REPO_ROOT / "config" / "params.yaml"
ANCHOR_CONSTANTS_PATH = SERVICE_ROOT / "config" / "anchor_constants.json"
FIXTURES_DIR = SERVICE_ROOT / "fixtures" / "alerts"


# --- 스위치 enum ---------------------------------------------------------------
class SourceMode(str, Enum):
    REPLAY = "replay"
    KAFKA = "kafka"


class ApiMode(str, Enum):
    MOCK = "mock"
    REAL = "real"


class LlmMode(str, Enum):
    OLLAMA = "ollama"
    API = "api"  # vLLM guided_json (D13)


def _env_enum(name: str, enum_cls: type[Enum], default: Enum) -> Enum:
    """환경변수를 enum 으로 파싱. 미설정이면 default, 잘못된 값이면 명시적 에러."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return enum_cls(raw.strip().lower())
    except ValueError as exc:
        allowed = [e.value for e in enum_cls]
        raise ValueError(f"{name}={raw!r} 는 허용값 {allowed} 아님") from exc


@dataclass(frozen=True)
class Settings:
    """런타임 스위치 + 로드된 파라미터 스냅샷 (frozen)."""

    source_mode: SourceMode
    api_mode: ApiMode
    llm_mode: LlmMode
    llm_base_url: str | None = None  # AGENT_LLM_BASE_URL — 미설정이면 백엔드 생성 시 명시적 에러
    params: dict[str, Any] = field(default_factory=dict)

    # --- params.yaml 접근 헬퍼 ---
    def get(self, dotted: str, default: Any = None) -> Any:
        """점 경로로 파라미터 조회 (예: 'agent.rag_top_k')."""
        node: Any = self.params
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def require(self, dotted: str) -> Any:
        """필수 파라미터 조회 — 없거나 null 이면 명시적 에러 (조용한 기본값 금지)."""
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel or value is None:
            raise ValueError(
                f"params.yaml 필수값 '{dotted}' 미설정(null). "
                f"조용한 기본값 대신 에러 — 담당자 확정 필요."
            )
        return value


def load_params(path: Path = PARAMS_PATH) -> dict[str, Any]:
    """config/params.yaml 로드 (단일 소스)."""
    if not path.exists():
        raise FileNotFoundError(f"params.yaml 없음: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings() -> Settings:
    """환경변수 스위치 3종 + params.yaml 을 읽어 Settings 구성."""
    return Settings(
        source_mode=_env_enum("AGENT_SOURCE_MODE", SourceMode, SourceMode.REPLAY),
        api_mode=_env_enum("AGENT_API_MODE", ApiMode, ApiMode.MOCK),
        llm_mode=_env_enum("AGENT_LLM_MODE", LlmMode, LlmMode.OLLAMA),
        llm_base_url=os.environ.get("AGENT_LLM_BASE_URL") or None,
        params=load_params(),
    )


# 편의: 모듈 임포트 시 지연 로딩 대신 명시적 호출 권장 (테스트 격리).
#
# ⚠️ 아래 print 는 헌법 6-1 이 금지하는 "디버깅용 print" 가 아니라 **CLI 진단 출력**이다
#    (`python -m src.agent_service.app.config` 로 지금 어떤 스위치가 걸려 있는지 확인하는 용도).
#    logging 으로 바꾸면 레벨·핸들러 설정에 따라 안 보일 수 있어 진단 목적에 반한다.
#    파이프라인 로직에는 print 가 한 줄도 없다 (2026-07-22 리뷰 확인).
if __name__ == "__main__":
    s = load_settings()
    print("SOURCE_MODE :", s.source_mode.value)
    print("API_MODE    :", s.api_mode.value)
    print("LLM_MODE    :", s.llm_mode.value)
    print("gate(B7)    :", s.require("spc.context_score_agent_min"))
    print("gen_model   :", s.require("llm.gen_model"))
