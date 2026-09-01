# -*- coding: utf-8 -*-
"""S9 Agent Copilot — 컨텍스트 인식 상시 사이드 패널의 서버측 (화면 정의서 S9).

**쉬운 말 요약** — 대시보드가 알아서 띄워주는 것(push)에 더해, 엔지니어가 **직접 물으면
답이 오는** 채널이다. 지금 보고 있는 챔버·Incident 를 질문에 자동으로 붙여서, **이미 만들어져
있는** 리포트·근거를 찾아 자연어로 되돌려준다. 새로 판단하지 않고, 승인·조치도 못 한다.

흐름:

    질문 + 컨텍스트(chamber·incident_id·sensor)
      → 의도 판정 (키워드 — LLM 아님. 이유는 `detect_intent` 참조)
      → 재료 조달 (게이트웨이 기존 라우트 + KB) — **읽기 전용**
      → LLM 1콜 (generate_structured — 헌법 6-3 JSON 강제·재시도·fallback)
      → 가드 2종 (유령 인용 · 액션 문구) → CopilotAnswer

**설계 원칙 3개** (S9 스펙 그대로):
  ① **신규 추론 엔진이 아니다.** 기존 API 를 대화형으로 오케스트레이션만 한다.
     새 판정을 만들면 Supervisor 와 정본이 둘이 되고, 화면마다 다른 답이 나온다.
  ② **출처 없는 문장 금지.** 답에 실린 ID 는 조달한 재료에 실존해야 한다(인용 가드).
  ③ **읽기 전용.** 승인·판정·조치 경로가 이 모듈에 없다 — 딥링크로 정규 UI 를 가리킬 뿐이다.

⚠️ **헌법 1-2 와의 관계** — Agent 를 새로 트리거하지 않는다. 이 모듈은 `fdc.alert` 에서
출발해 **이미 생성된** 리포트를 읽을 뿐이라, 1-2 가 금지하는 *"fdc.alert 를 우회한 직접 호출"*
이 아니라 그 **하류**다 (#154 에서 RTD 해제 Brief 를 같은 논거로 정리했다).

무중단 (헌법 6-2): 게이트웨이·Qdrant·LLM 어느 하나가 죽어도 예외를 밖으로 던지지 않는다.
근거가 얕아지거나 `unsupported=True` 로 정직하게 빈손을 알릴 뿐이다.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any, Optional

from .schemas.copilot import (
    CopilotAnswer,
    CopilotContext,
    EvidenceRef,
    make_fallback_answer,
)

if TYPE_CHECKING:
    from .config import Settings
    from .llm.client import LlmBackend

logger = logging.getLogger(__name__)

# 게이트웨이 주소는 배포마다 바뀌는 **인프라 값**이라 .env 다 (release_brief 와 같은 규약).
GATEWAY_ENV = "AGENT_GATEWAY_BASE_URL"
_DEFAULT_GATEWAY = "http://localhost:8000"


def gateway_base_url() -> str:
    """게이트웨이 베이스 URL — .env 우선, 없으면 로컬 compose 기본값."""
    return (os.environ.get(GATEWAY_ENV) or _DEFAULT_GATEWAY).rstrip("/")


# =============================================================================
# 1. 의도 판정 — 키워드다. LLM 이 아니다.
# =============================================================================
#: 의도별 트리거 어휘. **LLM 라우팅을 쓰지 않는 이유**: 라우팅에 1콜을 더 쓰면 왕복이
#  두 배가 된다. Supervisor 1건이 22초인 박스에서 Copilot 이 그만큼 걸리면 "물어보면
#  답이 오는" 채널로 성립하지 않는다(D4 = 시연 체감 지연). 의도를 틀려도 재료가 조금
#  넓어질 뿐이고, 답은 **재료 안에서만** 만들어지므로 안전 쪽으로 실패한다.
_INTENT_WORDS: dict[str, tuple[str, ...]] = {
    # "이 추천 왜 나왔어?" · "근거가 뭐야"
    "why": ("왜", "이유", "근거", "어째서", "why"),
    # "지난주 비슷한 일 있었나?" · "전에 이런 적"
    "similar": ("비슷", "유사", "전에", "지난", "과거", "사례", "similar"),
    # "레시피 손잡이 뭐 있어?" · "조정 가능한 파라미터"
    #   ⚠️ `why` 보다 뒤다 — "왜 이 손잡이야?" 는 판정 설명이지 목록 조회가 아니다.
    "knob": ("손잡이", "knob", "레시피 파라미터", "조정 가능", "튜닝 가능", "setpoint"),
    # "실력치가 뭐야?" · "TTTM 무슨 뜻이야"
    #   ⚠️ `status` 보다 **앞**이다 — "실력치가 뭐야"에 '상태' 가 안 걸리게 하려는 게 아니라,
    #      "지금 Qual 상태 어때" 같은 혼합 질문에서 상태 조회가 이기게 하려면 뒤여야 하는데
    #      실측상 용어 질문이 더 흔해 앞에 둔다. 틀려도 재료가 넓어질 뿐이다.
    "glossary": ("뭐야", "무슨 뜻", "무슨뜻", "뜻이", "정의", "용어", "뭔가요", "뭐지", "설명해"),
    # "지금 상태 어때?" · "괜찮아?"
    "status": ("상태", "지금", "괜찮", "현재", "status"),
}
_DEFAULT_INTENT = "general"


def detect_intent(question: str) -> str:
    """질문에서 의도 1개를 고른다 — `why` / `similar` / `status` / `general`.

    첫 매치가 아니라 **`_INTENT_WORDS` 선언 순서**로 고른다. "왜 지난주랑 다르지" 처럼
    두 어휘가 겹칠 때 `why`(근거 설명)가 `similar`(사례 검색)보다 사용자 의도에 가깝다.
    """
    q = (question or "").lower()
    for intent, words in _INTENT_WORDS.items():
        if any(w in q for w in words):
            return intent
    return _DEFAULT_INTENT


# =============================================================================
# 2. 재료 조달 — 전부 읽기 전용
# =============================================================================
async def _get_json(path: str, settings: "Settings", params: Optional[dict] = None) -> Any:
    """게이트웨이 GET 1회. 실패·형상 불량은 None (헌법 6-2 — 부르는 쪽이 죽지 않는다).

    🔴 **반드시 async 여야 한다** (2026-08-11 실측). 이 모듈은 게이트웨이 **프로세스 안에서**
    돌고, 여기서 부르는 라우트도 같은 프로세스다. 동기 `httpx.get` 을 쓰면 async 핸들러가
    이벤트 루프를 붙잡은 채 자기 자신에게 요청을 보내 **루프가 그 요청을 못 받는다** —
    타임아웃까지 교착하고 재료가 통째로 빈다. 그런데 응답은 **200 에 그럴듯한 답**이라
    (모델이 "재료가 없습니다"라고 정직하게 답한다) 로그를 안 보면 안 들킨다.
    release_brief 가 같은 패턴을 동기로 쓰는 것은 **별 프로세스(폴러)** 라 문제가 없는 것이다.

    ⚠️ 200 을 성공으로 보지 않는다 — `json.loads` 성공이 dict 를 뜻하지 않는다(헌법 7장).
    형 확인은 호출부가 자기 기대에 맞게 한 번 더 한다.
    """
    import httpx

    timeout = float(settings.require("agent.copilot_http_timeout_sec"))
    url = f"{gateway_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — 근거가 얕아질 뿐 답변은 계속한다
        logger.warning("copilot 재료 조회 실패 (%s: %s: %s)", url, type(exc).__name__, exc)
        return None


#: 리포트 행에서 프롬프트에 실을 필드 — 행 전체를 실으면 `priority_score`·`age_sec` 같은
#  내부 계산값까지 들어가 모델이 그걸 근거처럼 인용한다. `title` 이 Supervisor 사유이고
#  `verdict` 가 선택된 조치다(라우트가 그 이름으로 내려준다 — 2026-08-11 실측).
_REPORT_FIELDS = ("incident_id", "chamber_id", "title", "verdict", "severity_max",
                  "context_score", "alarms", "lifecycle")


async def _incident_report(incident_id: str, settings: "Settings") -> dict[str, Any]:
    """이미 생성된 Supervisor 판정 + 위반 근거 — **새로 만들지 않는다**(원칙 ①).

    두 라우트를 합친다: `/incidents/recent`(판정·사유)와 `/incidents/{id}/evidence`
    (위반 상세). 판정만으로는 *"왜"* 에 답할 재료가 없고, 위반만으로는 결론이 없다.

    없으면 빈 dict — "아직 리포트가 없다"는 것도 사실이라 지어내지 않고 그대로 올린다.
    """
    out: dict[str, Any] = {}

    data = await _get_json("/incidents/recent", settings)
    items = data.get("items") if isinstance(data, dict) else None
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("incident_id") == incident_id:
            out["judgement"] = {k: it[k] for k in _REPORT_FIELDS if k in it}
            break

    ev = await _get_json(f"/incidents/{incident_id}/evidence", settings)
    if isinstance(ev, dict):
        vio = ev.get("violations")
        if isinstance(vio, list) and vio:
            # 🔴 상한 필수 — 스톰 알람 1건이 위반 56행이라 무제한이면 프롬프트 예산을 넘긴다
            #    (#154 실측: ReleaseJudgement 가 ~11,417 토큰 → 400 폴백). 잘랐다는 사실도 싣는다
            #    — 조용히 자르면 모델이 "그게 전부"로 읽는다.
            cap = int(settings.require("agent.copilot_violation_limit"))
            out["violations"] = vio[:cap]
            if len(vio) > cap:
                out["violations_truncated"] = f"{len(vio)}건 중 {cap}건만 실음"
        if isinstance(ev.get("sensors"), list) and ev["sensors"]:
            out["sensors"] = ev["sensors"]

    return out


#: "이 챔버"가 아니라 **여러 챔버**를 묻는 표현. 하나라도 걸리면 컨텍스트 챔버로 좁히지 않는다.
#  느슨하게 잡는 편이 안전하다 — 넓게 답하면 물은 것이 포함되지만, 좁게 답하면 빠진다.
_FLEET_WORDS = ("다른 챔버", "모든", "전체", "전부", "챔버들", "각 챔버", "챔버별",
                "나머지", "브리핑", "다른 곳", "all chamber")


def _asks_fleet_wide(question: str) -> bool:
    """질문이 여러 챔버를 묻고 있나 — 그러면 컨텍스트 챔버 필터를 걷는다."""
    q = (question or "").lower()
    return any(w in q for w in _FLEET_WORDS)


def _glossary_hits(question: str) -> list[dict[str, str]]:
    """질문에 등장한 용어의 **한 줄 힌트**를 `config/glossary.yaml` 에서 찾는다.

    ⚠️ 그 파일은 **정본이 아니다**(파일 헤더 참조) — 대화창 길이로 줄인 표시용 사본이다.
    그래서 답에 `ref`(정본 위치)를 함께 실어, 사람이 확인하러 갈 곳을 알려준다.

    전문 검색이 아니라 **부분 문자열 매칭**이다. 용어가 20여 개뿐이고 임베딩을 쓰면
    게이트웨이 이미지에 torch 가 딸려온다(유사 사례 검색이 막힌 것과 같은 이유).
    """
    import yaml

    try:
        from .config import REPO_ROOT  # noqa: PLC0415

        data = yaml.safe_load((REPO_ROOT / "config" / "glossary.yaml").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 용어를 못 붙일 뿐 답변은 계속한다
        logger.warning("copilot 용어집 로드 실패 (%s: %s)", type(exc).__name__, exc)
        return []
    terms = (data or {}).get("terms")
    if not isinstance(terms, list):
        return []

    q = (question or "").lower()

    def _matched(t: dict) -> bool:
        """표제어 또는 **별칭** 하나라도 걸리면 히트.

        별칭이 없으면 표기 하나만 잡힌다 — 실측(2026-08-11)에서 "Nelson Rule" 만 등재돼
        있어 *"넬슨 룰이 뭐야"* · *"nelson 룰"* 둘 다 미스였다. 사람은 음차·약칭·띄어쓰기를
        섞어 쓴다.
        """
        names = [t.get("term", "")] + list(t.get("aliases") or [])
        return any(str(n).lower() in q for n in names if n)

    hits = [t for t in terms if isinstance(t, dict) and _matched(t)]
    # 긴 용어부터 — "관리 기준선" 이 걸렸으면 "기준선" 만으로 또 넣지 않는다.
    hits.sort(key=lambda t: -len(str(t.get("term", ""))))
    return hits


def sensor_hits(question: str) -> list[dict[str, str]]:
    """질문에 나온 **센서**를 C코드·표시명 양쪽으로 잡는다 (`config/sensor_map.yaml` = 6-4 단일 소스).

    ⚠️ 센서 25종을 용어집에 **손으로 베끼지 않는다.** 베끼는 순간 표시명 정본이 둘이 되고,
    `sensor_map.yaml` 이 바뀌어도 용어집은 안 따라온다 — 오늘 종일 잡은 그 유형이다.
    여기서 읽어 그때그때 붙인다.

    사람은 "RF Vpp" 처럼 밑줄 없이 쓰므로 `_` 를 공백으로 바꾼 형태도 함께 본다.
    """
    import yaml

    try:
        from .config import REPO_ROOT  # noqa: PLC0415

        full = yaml.safe_load((REPO_ROOT / "config" / "sensor_map.yaml").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("copilot sensor_map 로드 실패 (%s: %s)", type(exc).__name__, exc)
        return []
    if not isinstance(full, dict):
        return []

    q = (question or "").lower()
    out: list[dict[str, str]] = []
    for code, name in full.items():
        label = str(name)
        forms = {code.lower(), label.lower(), label.replace("_", " ").lower()}
        if any(f in q for f in forms):
            out.append({"sensor": code, "display_name": label})
    return out


# KB 검색 서버 주소 — 배포마다 바뀌는 인프라 값이라 .env/compose 다 (게이트웨이와 같은 규약).
KB_SERVER_ENV = "AGENT_KB_SERVER_URL"
_DEFAULT_KB_SERVER = "http://kb-server:8100"


def kb_server_url() -> str:
    """KB 검색 서버 베이스 URL."""
    return (os.environ.get(KB_SERVER_ENV) or _DEFAULT_KB_SERVER).rstrip("/")


#: 질문 어휘 → `historical_case` payload 조건. **의미 검색이 못 잡는 축**만 여기서 잡는다.
#  🔴 2026-08-11 실측: *"관리선 재설정으로 **해결된** 사례 있어?"* 에 "없습니다" 라고 답했다 —
#     실제로는 **1,053건**이 있었다. 본문에 "개선"·"미개선" 이 한 글자 차이로 섞여 있어
#     임베딩이 오히려 **가깝게** 보고, 1,775건 중 상위 3개가 하필 실패분이었다.
#     성패·조치유형은 텍스트가 아니라 **payload 필드**다 — 거기로 걸러야 맞는다.
#     (헌법 4-2 가 *"payload 필터 필요로 FAISS→Qdrant 전환"* 이라고 적어둔 이유가 이것이다.)
_CASE_FILTERS: tuple[tuple[str, Any, tuple[str, ...]], ...] = (
    # (payload 키, 값, 트리거 어휘) — 앞에서 걸리면 그 값으로 확정한다(선언 순서 = 우선순위).
    # ⚠️ 부정형이 **먼저** 와야 한다 — "해결 **안 된**" 은 '해결' 을 품고 있어서, 성공 어휘가
    #    먼저 걸리면 정반대로 읽힌다(2026-08-11 테스트가 잡았다). 같은 축은 선착순이므로
    #    순서가 곧 규칙이다.
    ("is_success", False, ("실패", "미개선", "안 됐", "안됐", "안 된", "안된", "안 되",
                           "않은", "않았", "못 한", "못한", "효과 없", "소용없", "재발")),
    ("is_success", True,  ("해결", "개선", "성공", "효과 있", "잘 된", "잘된")),
    ("action_type", "limit_correction", ("관리선", "실력치", "기준선", "재설정", "재산정")),
    ("action_type", "maintenance",      ("정비", "교체", "수리", "부품")),
    ("action_type", "recipe_r2r",       ("레시피", "튜닝", "손잡이", "setpoint")),
    ("action_type", "escalation",       ("에스컬", "escalat")),
    ("cycle_phase", "early", ("pm 직후", "정비 직후", "초기")),
    ("cycle_phase", "late",  ("말기", "후반")),
)


def build_case_filter(question: str, ctx: CopilotContext) -> dict[str, Any]:
    """질문에서 사례 검색 조건을 뽑는다 — 없으면 빈 dict(=필터 없음).

    ⚠️ **좁히다 0건이 되는 것**이 이 기능의 유일한 위험이다. 서버가 0건이면 조건을 풀고
    다시 찾아 `relaxed=true` 로 알려준다 — "조건에 맞는 게 없다"와 "아예 없다"를 구별한다.

    챔버는 **넣지 않는다.** 사례는 다른 챔버 것이 오히려 참고가 되고(같은 장비 유형),
    좁히면 표본이 급감한다. 센서는 컨텍스트에 있을 때만 — 그건 물어본 대상 자체다.
    """
    q = (question or "").lower()
    where: dict[str, Any] = {}
    for key, value, words in _CASE_FILTERS:
        if key in where:                      # 같은 축은 먼저 걸린 것이 이긴다
            continue
        if any(w in q for w in words):
            where[key] = value
    if ctx.sensor:
        where["sensor_id"] = ctx.sensor
    return where


async def _kb_search(collection: str, query: str, settings: "Settings",
                     where: Optional[dict[str, Any]] = None
                     ) -> tuple[list[dict[str, Any]], bool, Optional[int]]:
    """벡터 검색을 **KB 검색 서버에 위임**한다 (2026-08-11 — 구 인프로세스 호출 대체).

    🔴 **왜 직접 안 하나.** 이 모듈은 게이트웨이 프로세스에서 도는데 거기엔 임베딩 스택이
    없다(`FlagEmbedding`·`torch` 미설치 — 넣으면 이미지가 GB 단위로 커진다). 예전 코드는
    `search_kb` 를 직접 불렀고, `ModuleNotFoundError` 를 삼켜 **사례 10,000점을 두고 "0건"**
    이라고 답했다 — 실패가 아니라 **빈손**이라 화면에서 구분이 안 됐다.
    임베딩을 가진 쪽(`kb_server`, agent-service 이미지)에 물어본다.

    ⚠️ 타임아웃이 다른 재료보다 길다 — bge-m3 **첫 호출**은 모델 로딩까지 붙는다.
    (`/warmup` 을 시연 전에 때려두면 이 대기는 사라진다.)
    """
    if not query.strip():
        return [], False, None
    import httpx

    timeout = float(settings.require("agent.copilot_kb_timeout_sec"))
    limit = int(settings.require("agent.copilot_kb_limit"))
    url = f"{kb_server_url()}/kb/search"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json={"query": query, "collection": collection,
                                                "limit": limit, "where": where or {}})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — 근거가 얕아질 뿐 답변은 계속한다
        logger.warning("copilot KB 검색 실패 (%s · %s: %s)", collection, type(exc).__name__, exc)
        return [], False, None
    if not isinstance(data, dict):
        return [], False, None
    if data.get("error"):
        logger.warning("copilot KB 검색 서버 오류 (%s): %s", collection, data["error"])
    hits = data.get("hits")
    out = [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []
    total = data.get("total")
    return out, bool(data.get("relaxed")), (int(total) if isinstance(total, int) else None)


async def collect_materials(
    question: str, ctx: CopilotContext, intent: str, settings: "Settings"
) -> tuple[dict[str, Any], set[str]]:
    """의도·컨텍스트에 따라 조달할 것만 조달한다 — 넓게 긁으면 프롬프트 예산이 샌다.

    Returns:
        `(materials, used)` — `used` 는 **실제로 조회에 쓴** 컨텍스트 필드 이름들이다.

    ⚠️ `used` 를 따로 돌려주는 이유 (2026-08-11): `scope_note` 가 *"준 컨텍스트"* 를 적으면
    거짓말은 아니어도 **오해를 만든다.** 손잡이 목록 질의는 Incident 를 전혀 안 쓰는데
    화면에는 *"… Incident INC-… 로 좁혀 답했습니다"* 가 떴다 — 읽는 사람은 그 Incident
    기준의 답으로 읽는다. 의도마다 쓰는 컨텍스트가 다르므로 **쓴 것만** 적는다.
    """
    mats: dict[str, Any] = {}
    used: set[str] = set()
    incident_missing = False        # Incident 를 못 찾았나 (아래 챔버 보강 조건)

    # 용어·센서는 의도와 무관하게 **항상** 붙인다 — 질문에 "실력치" 가 들어 있으면 판정을 묻든
    # 상태를 묻든 그 뜻을 알아야 답이 맞는다. 히트가 없으면 아무것도 안 실린다(비용 0).
    #   ⚠️ 센서도 `glossary` 키에 함께 넣는다 — 새 키를 만들면 `EvidenceKind` 에 kind 를
    #      추가해야 하고, 그걸 빠뜨리면 **답 전체가 폴백으로 죽는다**(오늘 두 번 밟은 자리).
    defs = _glossary_hits(question) + sensor_hits(question)
    if defs:
        mats["glossary"] = defs

    if intent == "knob":
        # 손잡이 목록은 Qdrant(process_knowledge)의 튜닝축 카드다 — recipe tool 과 **같은 소스**를
        # 쓴다(`pipeline._load_knob_map`). 여기서 따로 읽으면 정본이 둘이 된다(원칙 ①).
        try:
            from .pipeline import _load_knob_map  # noqa: PLC0415 — 순환 회피

            cards = _load_knob_map()
        except Exception as exc:  # noqa: BLE001
            logger.warning("copilot knob_map 로드 실패 (%s: %s)", type(exc).__name__, exc)
            cards = None
        if cards:
            mats["knob_map"] = cards

    if intent in ("why", "general") and ctx.incident_id:
        report = await _incident_report(ctx.incident_id, settings)
        if report:
            mats["report"] = report
            used.add("incident_id")
        else:
            # 🔴 Incident 를 못 찾아도 빈손으로 끝내지 않는다 (2026-08-11 실측). 화면이 목업
            #    incident_id 를 넘기거나(프론트 뷰가 목업 키 기반) 이미 종결된 건을 보고 있으면
            #    여기서 재료가 0 이 되어 **모든 질문이 "근거 없음"** 이 된다. 챔버라도 있으면
            #    그걸로 답할 거리는 있다 — 좁히기에 실패한 것이지 답할 수 없는 것이 아니다.
            # 🔴 **의도를 바꾸지 않는다** (2026-08-11 2차 실측). 처음엔 `intent = "status"` 로
            #    갈아치웠는데, 그러면 Incident 와 무관한 질문까지 챔버 조회로 끌려간다 —
            #    화면이 목업 incident_id 를 붙이는 동안 *"레시피 재산정 기록 몇 건?"* 이
            #    KB 검색을 건너뛰고 "총 건수를 알 수 없다" 로 답했다(직접 호출하면 1,775건).
            #    폴백은 **재료를 보태는 것**이지 질문을 바꾸는 것이 아니다.
            logger.info("copilot incident 재료 없음 → 챔버 상태 보강 (%s)", ctx.incident_id)
            incident_missing = True

    # 🔴 KB 를 봐야 하는 갈래를 넓혔다 (2026-08-11 실측).
    #   · `why` + Incident 없음 — *"석영 부품이 왜 상해?"* 가 재료 0 이었다. 판정을 묻는 게
    #     아니라 **원리를 묻는 것**이고, 그 답은 매뉴얼·사례에 있다.
    #   · `glossary` — 용어집에 없는 장비 용어(*"인터락 걸리는 원인"*)는 매뉴얼이 유일한 소스다.
    #     용어집 히트가 있어도 함께 본다: 한 줄 정의는 "무엇인가"만 답하고 "왜/어떻게" 는 못 한다.
    _kb_intents = (intent == "similar"
                   or (intent in ("general", "why") and (not ctx.incident_id or incident_missing))
                   or intent == "glossary")
    if _kb_intents:
        # 검색어는 질문 + 컨텍스트 센서. 챔버 ID 는 사례 본문에 안 실려 노이즈가 된다.
        terms = " ".join(t for t in (question, ctx.sensor or "") if t).strip()
        # 사례와 매뉴얼을 **함께** 본다 — "비슷한 일" 은 과거 사례에, "어떻게 고쳤나" 는
        # 매뉴얼에 있다. 둘 다 임베딩 검색이라 같은 서버 한 번씩이다.
        where = build_case_filter(question, ctx)
        cases, relaxed, cases_total = await _kb_search("historical_case", terms, settings, where)
        manuals, _, manuals_total = await _kb_search("error_manual", terms, settings)
        if cases:
            mats["cases"] = cases
            # 🔴 **표본이라는 사실을 항상 싣는다** (2026-08-11 실측). 검색은 상위 3건만 주는데
            #    모델은 그것을 전부로 읽어 *"몇 건이야?"* 에 **"1건입니다"** 라고 단정했다
            #    (실제 1,775건). 지어낸 것보다 나쁘다 — 숫자는 검증 없이 믿기기 때문이다.
            mats["cases_shown_of_total"] = {"shown": len(cases), "total": cases_total}
            if where:
                # 조건을 걸었다는 사실과, 0건이라 풀었다는 사실을 **모델에게 알린다** —
                # 안 알리면 넓힌 결과를 "조건에 맞는 것"으로 단정해 말한다.
                mats["cases_filter"] = {"applied": where, "relaxed": relaxed}
        if manuals:
            mats["manuals"] = manuals
            mats["manuals_shown_of_total"] = {"shown": len(manuals), "total": manuals_total}
        if (cases or manuals) and ctx.sensor:
            used.add("sensor")   # 챔버는 검색어에 안 넣는다(위 주석) → 쓴 것이 아니다

    if intent == "status" or incident_missing:
        # 🔴 **`/chambers/status` 만으로 "문제 있어?" 에 답하면 안 된다** (2026-08-11 실측).
        #    그 라우트가 주는 것은 `inhibited`·`uptime_pct` 둘뿐 — **RTD 자동정지 여부**지
        #    이상 유무가 아니다. 그것만 보고 *"정상입니다"* 라고 답했는데, 그 챔버에는
        #    미종결 Incident 431건(pending 359·open 72)과 알람 3,081건이 있었다.
        #    "안 멈췄다" 와 "문제 없다" 는 다른 말이다 — 열린 Incident 를 함께 싣는다.
        open_rows = await _get_json("/incidents/recent", settings, {"n": 60})
        items = open_rows.get("incidents") if isinstance(open_rows, dict) else None
        if isinstance(items, list):
            live = [it for it in items if isinstance(it, dict)
                    and str(it.get("lifecycle")) not in ("closed",)
                    and (not ctx.chamber or _asks_fleet_wide(question)
                         or it.get("chamber_id") == ctx.chamber)]
            if live:
                cap = int(settings.require("agent.copilot_violation_limit"))
                mats["open_incidents"] = [
                    {k: it.get(k) for k in ("incident_id", "chamber_id", "lifecycle",
                                            "severity_max", "title", "verdict", "alarms")}
                    for it in live[:cap]
                ]
                mats["open_incidents_shown_of_total"] = {"shown": min(len(live), cap),
                                                         "total": len(live)}

        data = await _get_json("/chambers/status", settings)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            rows = [it for it in items if isinstance(it, dict)]
            # 🔴 **질문이 컨텍스트를 이긴다** (2026-08-11 실측). 자동 주입은 *"말 안 해도
            #    알아듣게"* 하는 장치지 **말한 것을 덮는** 장치가 아니다. *"챔버 상태들 브리핑"*·
            #    *"다른 챔버는?"* 에 컨텍스트 챔버로 좁혀 답하면 사용자가 물은 것과 다른 답이
            #    나오고, 그게 틀렸다는 표시도 없다(scope_note 만 작게 적힌다).
            narrow = bool(ctx.chamber) and not _asks_fleet_wide(question)
            if narrow:
                rows = [it for it in rows if it.get("chamber_id") == ctx.chamber]
            if rows:
                mats["chambers"] = rows
                if narrow:
                    used.add("chamber")

    return mats, used


# =============================================================================
# 3. 프롬프트 — 정본은 `docs/Agent_프롬프트_라이브러리_v1.md` §8 (프롬프트 동기화 게이트 대상)
# =============================================================================
SYSTEM_PROMPT = """[역할] 당신은 FDC 대시보드 옆에 상주하는 분석 보조원이다. 엔지니어가 화면을 보다가 던진 질문에, 이미 만들어져 있는 판정·근거를 찾아 짧게 answer 한다.

[대전제 — 어기면 답을 버린다]
1. 새로 판정하지 마라. 원인·조치를 스스로 결론짓지 말고, 재료에 있는 판정을 전달하라.
2. 재료에 없는 것은 말하지 마라. 모르면 모른다고 하고 unsupported 를 true 로 둔다.
3. 승인·반려·적용·정지를 실행하지 마라. 필요하면 어느 화면으로 가라고만 말한다.

[입력] question(질문) · context(chamber·incident_id·sensor) · materials(report·cases·chambers·knob_map·glossary) · recent_turns(직전 대화)

[작성 규칙]
- answer 는 3문장 이내. 결론 먼저, 그다음 근거 한 줄.
- 숫자는 재료에 있는 값만 쓴다. 반올림·환산도 하지 마라.
- evidence 에는 재료에 실제로 있는 식별자만 담는다. 지어낸 ID 는 즉시 걸러진다.
- 재료가 비었으면 answer 에 그 사실을 쓴다. 추측으로 채우지 마라.
- recent_turns 는 "다른 챔버는?" 같은 후속 질문의 **맥락 파악에만** 쓴다. 거기 있는 값을 근거로 인용하지 마라 — 근거는 materials 뿐이다.
- 근거 카드의 label 은 **그 건 자체**의 내용만 적는다. 다른 건에서 본 센서·원인을 끌어와 섞지 마라 — 서버가 사실로 덮어쓴다.
- chambers 의 inhibited·uptime 은 **자동정지 여부**일 뿐 이상 유무가 아니다. "문제 있나" 는 open_incidents 로 답하고, 둘을 섞어 "정상"이라고 단정하지 마라.
- cases·manuals 는 **상위 몇 건만 보여주는 표본**이다. 전체 수는 shown_of_total.total 에 있다. **개수를 물으면 total 을 답하고**, 보여준 건수를 전체인 것처럼 말하지 마라. total 이 null 이면 개수를 모른다고 하라.
- cases_filter 가 있으면 어떤 조건으로 찾았는지 한 마디로 밝힌다. relaxed 가 true 면 **조건에 맞는 것이 없어 넓혀 찾은 결과**이므로, 조건에 맞는 사례라고 말하지 마라.
- glossary 가 있으면 그 한 줄 정의를 그대로 쓴다. industry 가 있으면 **현업과 뜻이 다르다는 것을 반드시 함께** 말한다. 정의를 늘려 쓰지 말고, 상세가 필요하면 ref 위치를 알려준다.
- scope_note 와 unsupported 는 비워 둔다(false). 서버가 실제 상태로 덮어쓴다.
"""

SCHEMA_EXAMPLE = """{
  "answer": "기준선 노후(baseline_aging)로 판정됐고 관리선 재설정안이 준비돼 있습니다. TTTM 갭이 이웃 수준이라 공정 이동이 아니라고 봤습니다.",
  "evidence": [
    {"kind": "report", "ref": "SUP-20260810-SIMCH3-0042", "label": "Supervisor 판정 — baseline_aging (확신 0.82)"},
    {"kind": "case", "ref": "CASE-0912", "label": "유사 사례 — 관리선 재설정으로 종결"}
  ],
  "deeplink": "/incidents/INC-20260810-SIMCH3-0007",
  "scope_note": "",
  "unsupported": false
}"""


#: 컨텍스트 라벨 — `scope_note` 는 **코드가 쓴다**(팀 원칙 "판정=코드 / 서술=LLM").
#  🔴 2026-08-11 실측: LLM 에 맡겼더니 `SCHEMA_EXAMPLE` 의 incident_id 를 그대로 베껴,
#     chamber 만 준 질의에 *"SIM_CH_3 · INC-…-0007 로 좁혀 답했습니다"* 가 나왔다.
#     `base.py` 가 이미 적어둔 함정이다 — *"예시가 값을 가르치면 그 값이 그대로 복사된다"*.
#     하필 이 필드는 **주입이 맞는지 사람이 눈으로 확인하는 자리**라, 지어내면 없느니만 못하다.
_SCOPE_LABELS = (("chamber", "챔버"), ("incident_id", "Incident"),
                 ("sensor", "센서"), ("range", "기간"))


def build_scope_note(ctx: CopilotContext, used: set[str] | None = None) -> str:
    """**실제로 조회에 쓴** 컨텍스트만 적는다 — 안 쓴 것은 "좁혔다"고 말하지 않는다.

    `used=None` 이면 준 것 전부를 적는다(가드 단독 테스트용). 운영 경로는 항상
    `collect_materials` 가 돌려준 집합을 넘긴다.
    """
    parts = [f"{label} {v}" for key, label in _SCOPE_LABELS
             if (used is None or key in used) and (v := getattr(ctx, key, None))]
    return " · ".join(parts) + " 로 좁혀 답했습니다." if parts else "좁히지 않고 전역으로 답했습니다."


_C_CODE_RE = re.compile(r"\bC\d{1,3}\b")


def sensor_map_for(mats: dict[str, Any], ctx: CopilotContext) -> dict[str, str]:
    """재료·컨텍스트에 **등장한 C코드만** 표시명과 함께 싣는다 (6-4 단일 소스).

    §0 공통블록이 *"표시명은 입력의 sensor_map 에 있는 것만 쓴다"* 로 이미 규약을 정해뒀다.
    그런데 이 경로는 게이트웨이가 `sensor_name` 없이 C코드만 내려주므로(2026-08-11 실측)
    실어주지 않으면 모델이 **자기 기억에서 표시명을 꺼낸다** — 맞을 때도 있지만(C11=DC_Bias
    실측 적중) 근거 없는 값이라 모르는 센서에서 지어낸다. 전체 맵을 싣지 않는 이유는 예산이다.
    """
    import json

    import yaml

    blob = json.dumps(mats, ensure_ascii=False, default=str) + " " + (ctx.sensor or "")
    codes = set(_C_CODE_RE.findall(blob))
    if not codes:
        return {}
    try:
        from .config import REPO_ROOT  # noqa: PLC0415

        path = REPO_ROOT / "config" / "sensor_map.yaml"
        full = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 표시명이 없을 뿐 답변은 계속한다
        logger.warning("sensor_map 로드 실패 — 표시명 없이 진행 (%s: %s)", type(exc).__name__, exc)
        return {}
    if not isinstance(full, dict):
        return {}
    return {c: str(full[c]) for c in sorted(codes) if c in full}


def build_payload(question: str, ctx: CopilotContext, mats: dict[str, Any],
                  history: list[dict[str, str]] | None = None) -> str:
    """LLM 입력 직렬화. 재료가 없으면 **없다고 명시**한다 — 빈 키를 지우면 모델이 지어낸다."""
    import json

    body = {
        "question": question,
        "context": ctx.model_dump(exclude_none=True),
        "materials": mats or "없음",
        "sensor_map": sensor_map_for(mats, ctx),
        # 맥락 파악용 — **재료가 아니다**(build_history docstring 참조)
        "recent_turns": history or [],
    }
    return json.dumps(body, ensure_ascii=False, allow_nan=False, default=str)


# =============================================================================
# 4. 가드
# =============================================================================
#: 인용 가드의 ID 패턴 — **supervisor 의 것을 그대로 쓴다.** 복제하면 접두를 신설할 때
#  한쪽만 고쳐져 그 접두가 무검사로 통과한다(헌법 6-4 *"접두 추가 시 함께 고칠 것"*,
#  round18 `MAN-` 실측). 단일 소스를 유지하려고 private 이지만 import 한다.
from .supervisor import _ID_RE as _CITED_ID_RE  # noqa: E402  (순환 아님 — supervisor 는 copilot 을 모른다)


def known_refs(mats: dict[str, Any]) -> set[str]:
    """조달한 재료에 실존하는 ID 집합 — 유령 인용 가드의 정답지.

    재료 dict 를 통째로 훑어 ID 형태 문자열을 전부 걷는다. 키 이름으로 좁히면 컬렉션마다
    다른 키(`case_id`·`manual_id`·`doc_id`…)를 하나 빠뜨리는 순간 실존 ID 가 유령으로
    오탐된다 — supervisor 가 2026-07-20 에 11건 겪은 자리다.
    """
    import json

    blob = json.dumps(mats, ensure_ascii=False, default=str)
    return set(_CITED_ID_RE.findall(blob))


def enforce_citation_guard(answer: CopilotAnswer, mats: dict[str, Any]) -> tuple[CopilotAnswer, bool]:
    """재료에 없는 ID 를 인용한 근거 카드를 걷어낸다 (원칙 ②).

    카드를 지우기만 하고 `answer` 본문은 건드리지 않는다 — 본문을 기계가 편집하면 문장이
    깨져 더 못 읽는다. 대신 **전부 걷혔으면 `unsupported=True`** 로 올려, 근거 없이 남은
    단정이 근거 있는 답처럼 보이지 않게 한다.
    """
    if not answer.evidence:
        # 🔴 **애초에 근거를 안 단 것**과 **달았는데 전부 유령인 것**은 다르다 (2026-08-11 실측).
        #    전자는 인용 누락일 뿐 답 자체는 재료 기반일 수 있는데, 둘을 같이 취급했더니
        #    멀쩡한 답(손잡이 목록·챔버 브리핑)에 "근거 없음" 이 붙었다. 여기서 갈라 준다.
        return answer, False
    allowed = known_refs(mats)
    kept = [e for e in answer.evidence if e.ref in allowed]
    removed = [e.ref for e in answer.evidence if e.ref not in allowed]

    # 🔴 **"지어낸 ID" 와 "ID 가 아닌 출처 표기" 를 가른다** (2026-08-11 실측).
    #    걷어낸 것이 `sensor_map`·`knob_map`·`SIM_CH_3`·`CLAUDE.md 6-4`·`σ` 였다 — 전부
    #    재료의 **출처를 가리키는 말**이지 존재하지 않는 사례 ID 가 아니다. 이걸 유령으로
    #    세면 용어·챔버 답변이 통째로 "근거 없음" 이 된다(실측: 멀쩡한 답 4건이 NG 로 뒤집힘).
    #    카드에서 빼는 것은 맞다(ID 가 아니니 클릭·대조가 안 된다). 다만 **환각으로는 세지 않는다.**
    ghosts = [r for r in removed if _CITED_ID_RE.search(r)]
    if removed:
        logger.warning(
            "copilot 근거 %d건 제거 (남은 %d건) — 유령 %s · 비-ID %s",
            len(removed), len(kept), ghosts or "없음",
            [r for r in removed if r not in ghosts] or "없음",
        )
    return answer.model_copy(update={
        "evidence": kept,
        # 유령이 **실제로** 있었고 그 결과 근거가 하나도 안 남았을 때만 단정을 거둔다.
        "unsupported": answer.unsupported or (bool(ghosts) and not kept),
    }), bool(ghosts)


#: 라벨을 **서버가 아는** 재료 — ref 를 이 표에서 찾으면 모델 문장 대신 사실을 쓴다.
_LABEL_SOURCES = ("open_incidents",)
_LABEL_CHARS = 70          # 카드 한 줄 폭. 넘기면 화면에서 잘려 읽히지 않는다.


def true_labels(mats: dict[str, Any]) -> dict[str, str]:
    """재료에서 **ID → 사실 라벨** 표를 만든다 (severity + 판정 사유 앞부분)."""
    out: dict[str, str] = {}
    for key in _LABEL_SOURCES:
        for row in mats.get(key) or []:
            if not isinstance(row, dict):
                continue
            rid = row.get("incident_id")
            if not rid:
                continue
            sev = row.get("severity_max") or "?"
            title = str(row.get("title") or "").strip() or "판정 사유 미기재"
            out[str(rid)] = f"{sev} · {title[:_LABEL_CHARS]}"
    return out


def enforce_label_guard(answer: CopilotAnswer, mats: dict[str, Any]) -> CopilotAnswer:
    """근거 카드의 **설명을 사실로 덮어쓴다** (2026-08-11 실측).

    🔴 ID 는 실존이라 인용 가드를 통과하는데 **설명이 다른 건 이야기**였다:
       카드가 *"미처리 CRITICAL 이상 — C61 급변 및 장비 고장 의심"* 이라고 했지만
       그 Incident 는 **WARNING** 이고 사유는 *"SHAP 상위 센서 부재로 원인 축을 특정할 수
       없다"* 였다. C61·인클로저는 **다른 챔버** 건의 내용이다.
       유령 ID 보다 위험하다 — **검증된 것처럼 보이기** 때문이다(ID 가 맞으니까).

    라벨은 서술이 아니라 **사실**이다(그 건의 심각도와 판정 사유). 서버가 아는 값이 있으면
    모델 문장을 쓰지 않는다 — `scope_note`·`unsupported` 와 같은 원칙이다.
    ⚠️ 사례·매뉴얼 카드는 덮지 않는다. 그쪽 라벨은 **요약**이라 모델이 하는 일이 맞다.
    """
    if not answer.evidence:
        return answer
    truth = true_labels(mats)
    if not truth:
        return answer
    fixed, changed = [], 0
    for e in answer.evidence:
        real = truth.get(e.ref)
        if real and real != e.label:
            changed += 1
            fixed.append(e.model_copy(update={"label": real}))
        else:
            fixed.append(e)
    if changed:
        logger.warning("copilot 근거 라벨 %d건 사실로 교체", changed)
    return answer.model_copy(update={"evidence": fixed})


def enforce_deeplink_guard(answer: CopilotAnswer, mats: dict[str, Any]) -> CopilotAnswer:
    """재료에 없는 ID 를 가리키는 딥링크를 지운다 — **없는 화면으로 보내지 않는다.**

    🔴 2026-08-11 실측: 컨텍스트에 목업 `incident_id` 가 붙어 있으면(프론트 뷰가 목업 키
    기반) 조회에 쓰지도 않은 그 값으로 *"조치는 이 화면에서 → /incidents/INC-…"* 를 만들었다.
    인용 가드가 **근거 카드**만 보고 있어서 딥링크는 무검사로 새던 자리다 — 근거보다
    오히려 위험하다. 사람이 **클릭해서 이동**하고, 빈 화면을 보면 시스템을 의심하게 된다.
    """
    link = answer.deeplink
    if not link:
        return answer
    ids = _CITED_ID_RE.findall(link)
    if not ids:
        return answer                    # 화면 경로만 가리키는 링크(/incidents 등)는 통과
    allowed = known_refs(mats)
    if all(i in allowed for i in ids):
        return answer
    logger.warning("copilot 미확인 딥링크 제거: %s", link)
    return answer.model_copy(update={"deeplink": None})


#: 액션을 **했다고** 주장하는 어미. S9 는 읽기 전용이라 이런 문장은 사실과 다르다.
#  "승인하세요"(권유)는 막지 않는다 — 딥링크와 같은 뜻이다. 막는 건 완료형뿐이다.
_ACTION_CLAIM_RE = re.compile(
    r"(승인|반려|적용|정지|해제|스크랩|배포)\s*(했|하였|완료|처리했|됐습니다|되었습니다)"
)
_ACTION_NOTICE = " (이 채널에서는 조치를 실행하지 않습니다 — 해당 화면에서 진행해 주세요.)"


def enforce_readonly_guard(answer: CopilotAnswer) -> CopilotAnswer:
    """"승인했습니다" 류의 **완료 주장**에 정정 문구를 붙인다 (원칙 ③).

    문장을 지우지 않는 이유는 위 인용 가드와 같다 — 다만 이건 사람이 **오해하면 위험한**
    종류라(승인된 줄 알고 넘어간다) 침묵하지 않고 한 줄을 덧붙인다.
    """
    if not _ACTION_CLAIM_RE.search(answer.answer or ""):
        return answer
    logger.warning("copilot 액션 완료 주장 감지 — 정정 문구 부착")
    return answer.model_copy(update={"answer": answer.answer + _ACTION_NOTICE})


# =============================================================================
# 5. 진입점
# =============================================================================
#: 프롬프트에 실을 직전 대화 턴 수. 대화창은 짧게 주고받는 자리라 길게 실으면 예산만 먹는다
#  (한 턴 = 질문 + 답 요약). 후속 질문("다른 챔버는?")이 가리키는 것은 대개 **직전 한두 턴**이다.
_HISTORY_TURNS = 3
#: 이력 1턴의 답변 인용 길이 — 전문을 실으면 근거 카드·수치까지 다시 들어와 예산이 배가된다.
_HISTORY_ANSWER_CHARS = 200


def build_history(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """직전 대화를 프롬프트용으로 줄인다 — 최근 `_HISTORY_TURNS` 턴, 답변은 앞부분만.

    ⚠️ **이력은 재료가 아니다.** 여기 실린 과거 답변을 근거로 다시 인용하면 유령이 재생산되므로
    (모델은 자기 말을 사실로 취급하기 쉽다) 프롬프트에서 *"맥락 파악에만 쓰라"* 고 못박는다.
    인용 가드도 이력을 정답지에 넣지 않는다 — `known_refs` 는 `mats` 만 본다.
    """
    out: list[dict[str, str]] = []
    for turn in (history or [])[-_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        q = str(turn.get("question") or "").strip()
        a = str(turn.get("answer") or "").strip()[:_HISTORY_ANSWER_CHARS]
        if q:
            out.append({"question": q, "answer": a})
    return out


async def answer_question(
    question: str,
    ctx: CopilotContext,
    backend: "LlmBackend",
    settings: "Settings",
    history: list[dict[str, Any]] | None = None,
) -> CopilotAnswer:
    """질문 1건에 답한다. **예외를 밖으로 던지지 않는다** — 실패는 fallback 응답이다.

    Args:
        question: 엔지니어가 입력한 자연어 질문.
        ctx: 화면이 자동 첨부한 컨텍스트 (전부 optional).
        backend: LlmBackend (Mock 도 가능 — 테스트·시연 폴백).
        settings: `agent.copilot_*` 키를 읽는다.

    Returns:
        CopilotAnswer — 실패해도 `unsupported=True` 인 유효 객체.
    """
    from .llm.client import generate_structured  # noqa: PLC0415 — 순환 회피

    if not (question or "").strip():
        return make_fallback_answer("질문이 비어 있습니다")

    intent = detect_intent(question)
    mats, used = await collect_materials(question, ctx, intent, settings)
    logger.info(
        "copilot 질의 — intent=%s chamber=%s incident=%s 재료=%s",
        intent, ctx.chamber, ctx.incident_id, sorted(mats.keys()) or "없음",
    )

    # LLM 이 폴백으로 떨어졌는지를 **플래그로 잡는다.** 아래 grounded 계산이 이걸 봐야 한다 —
    # 안 보면 *"답을 받지 못했습니다"* 라는 폴백 문장에 `unsupported=false` 가 붙어
    # 화면이 그걸 **근거 있는 답으로** 표시한다 (2026-08-11 실측 회귀).
    llm_failed = {"v": False}

    def _fallback() -> CopilotAnswer:
        llm_failed["v"] = True
        return make_fallback_answer("모델 응답을 받지 못했습니다")

    result = await generate_structured(
        backend,
        SYSTEM_PROMPT,
        build_payload(question, ctx, mats, build_history(history)),
        CopilotAnswer,
        fallback=_fallback,
        schema_example=SCHEMA_EXAMPLE,
        max_output_tokens=int(settings.require("agent.copilot_max_output_tokens")),
    )

    result, ghosted = enforce_citation_guard(result, mats)
    result = enforce_label_guard(result, mats)
    result = enforce_deeplink_guard(result, mats)
    result = enforce_readonly_guard(result)
    # scope_note·unsupported 는 LLM 값을 쓰지 않고 **서버가 덮어쓴다** — 둘 다 사실 판정이지
    # 서술이 아니다(팀 원칙 "판정=코드 / 서술=LLM").
    #   🔴 2026-08-11 실측: unsupported 를 모델에 맡겼더니 챔버 상태로 **제대로 답하고도**
    #      *"이상 판정이 없다"* 를 근거 부재로 읽어 true 를 달았다. 그러면 화면이 멀쩡한
    #      답에 "근거 없음 · LIMITED" 를 붙인다. 프롬프트로 두 번 조여도 안 바뀌었다 —
    #      base.py 가 confidence 에서 겪은 것과 같은 자리다(모델은 불리언에서 움츠린다).
    #      서버는 **재료를 실제로 조달했는지**를 알고, 그게 화면이 물어보는 바로 그 질문이다.
    #   재료에 인용 가능한 ID 가 **있었는데도** 근거가 하나도 안 남았다면 = 전부 유령이었다는
    #   뜻이라 근거 없음으로 본다. 애초에 인용할 ID 가 없는 재료(챔버 상태 등)는 그 이유로
    #   벌하지 않는다 — 그건 모델 잘못이 아니라 재료의 성질이다.
    #   유령 인용으로 근거가 **걷힌** 경우만 근거 없음이다. 모델이 처음부터 인용을 안 한 것은
    #   인용 누락이지 근거 부재가 아니다 — 재료로 답했으면 grounded 다.
    grounded = bool(mats) and not llm_failed["v"] and not ghosted
    return result.model_copy(update={
        "scope_note": build_scope_note(ctx, used),
        "unsupported": not grounded,
    })
