"""RTD 해제 근거 Brief — 자동 정지된 챔버의 "다시 돌려도 되나" 근거 (인프라 스텝 3 · #140).

**쉬운 말 요약** — 챔버가 자동으로 멈추면(헌법 1-1 예외 4) 엔지니어는 재가동 여부를 판단해야
한다. 그 판단의 재료(왜 섰나 / 얼마나 심했나 / 과거엔 뭘로 풀렸나)를 Agent 가 모아 한 장으로
만들어 두는 모듈이다. **해제는 하지 않는다** — 해제 경로는 requal 승인(R9) 하류 하나뿐이다.

흐름 (계약 §9 `/chambers/status` — #118 C 리뷰 ② ⓑ 채택):

    GET /chambers/status → items[].inhibited == true
      → incident 단위로 묶고 (장비 스코프면 형제 챔버가 같은 incident 를 공유한다)
      → 이미 Brief 가 있으면 skip (정지 1회 = Brief 1건)
      → 회고 3층 조회(db.py) + KB 검색(error_manual · historical_case)
      → LLM 1콜 → 가드 3종 → release_briefs 적재
      → S7(R9 승인 화면)이 그 행을 읽고 사람이 결정 → `/requalify/{incident_id}` → 해제

왜 DB 를 직접 안 보고 게이트웨이를 부르나: 열린 inhibit 의 단일 소스가 `/chambers/status`
라고 계약에 명시돼 있다(`:540`). 같은 판정을 우리가 다시 구현하면 정본이 둘이 된다 — 스코프
승격·uptime 창 같은 규칙이 갈라지는 자리다. **재료 조회만** DB 를 본다(읽기 전용).

⚠️ **「정지 이후 좋아졌나」는 담지 않는다.** 정지 중에는 그 챔버 wafer 가 0장이라
(`src/simulator/kafka_producer.py` 의 inhibit 스킵) 잴 재료가 없다. 현업 판단도
"왜 섰나 / 뭘 했나 / 전에 뭐가 통했나"이고 회고 + 사례가 그것과 맞는다.

무중단 (헌법 6-2): 게이트웨이·Postgres·Qdrant·LLM 어느 하나가 죽어도 폴러는 죽지 않는다.
근거가 얕아지거나 그 주기를 건너뛸 뿐이고, 다음 주기에 다시 시도한다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from .schemas.release import (
    ReleaseBrief,
    ReleaseJudgement,
    make_fallback_judgement,
    make_release_report_id,
    retrospect_from_materials,
)

if TYPE_CHECKING:
    from .config import Settings
    from .llm.client import LlmBackend

logger = logging.getLogger(__name__)

# 게이트웨이 주소는 배포마다 바뀌는 **인프라 값**이라 .env 다 (params.yaml 은 알고리즘 값 —
# config.py 주석 규약). 기본값은 docker-compose 의 gateway 서비스와 맞춘다.
GATEWAY_ENV = "AGENT_GATEWAY_BASE_URL"
_DEFAULT_GATEWAY = "http://localhost:8000"

#: 한 주기에 만들 최대 Brief 수 — 생성이 LLM 호출이라 건당 수십 초다. 장비 스코프 정지는
#  형제 챔버가 한꺼번에 서지만 incident 로 묶이므로 실제로는 1건이다. 상한이 없으면 다중
#  정지 상황에서 한 주기가 수 분간 잡혀 종료 신호에 늦게 반응한다 (reanalysis 선례).
_MAX_PER_TICK = 3


def gateway_base_url() -> str:
    """게이트웨이 베이스 URL — .env 우선, 없으면 로컬 compose 기본값."""
    return (os.environ.get(GATEWAY_ENV) or _DEFAULT_GATEWAY).rstrip("/")


# =============================================================================
# 1. 트리거 — GET /chambers/status
# =============================================================================
def fetch_inhibited(settings: "Settings") -> list[dict[str, Any]]:
    """열린 inhibit 목록 — `/chambers/status` 의 `items[]` 중 `inhibited == true`.

    ⚠️ **"떴으니 붙었다"로 보지 않는다** (헌법 7장). 응답이 200 이어도 `items` 가 리스트가
    아니거나 dict 가 아닌 원소가 섞이면 조용히 잘못 도는 자리라, 형까지 확인하고 거른다.

    실패(게이트웨이 미기동·타임아웃·형상 불량)는 빈 리스트 + 경고 — 다음 주기에 재시도한다.
    """
    import httpx

    win = settings.get("rtd.uptime_window_hours")
    params = {"window_hours": float(win)} if isinstance(win, (int, float)) else None
    timeout = float(settings.require("agent.release_http_timeout_sec"))
    url = f"{gateway_base_url()}/chambers/status"
    try:
        resp = httpx.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — 폴러 생존 우선 (6-2)
        logger.warning("챔버 상태 조회 실패 — 이번 주기 skip (%s: %s: %s)",
                       url, type(exc).__name__, exc)
        return []
    # json 파싱 성공이 dict 를 뜻하지 않는다 ("123"·[1,2]·null 전부 유효 JSON — 헌법 7장)
    if not isinstance(data, dict):
        logger.warning("챔버 상태 응답이 object 가 아니다 — skip (type=%s)", type(data).__name__)
        return []
    items = data.get("items")
    if not isinstance(items, list):
        logger.warning("챔버 상태 응답에 items 배열이 없다 — skip")
        return []
    return [it for it in items if isinstance(it, dict) and it.get("inhibited")]


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO 문자열 → datetime. 게이트웨이는 `inhibited_at` 을 isoformat 문자열로 준다."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.warning("inhibited_at 파싱 실패 — %r", value)
        return None


def group_by_incident(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """열린 inhibit 을 **Incident 단위로 접는다** (헌법 1-4 사건 단위 처리).

    장비 스코프 정지는 형제 챔버 전부에 행이 생기고 **전부 같은 incident_id** 를 갖는다
    (`chamber_inhibit.record_trigger`). 해제도 그 단위로 일괄이므로(`release()`), Brief 를
    챔버마다 만들면 같은 판단을 4번 묻는 화면이 된다.

    대표 챔버는 우선 `incidents.chamber_id`(= 실제로 이상이 관측된 곳)를 쓰고, 조회가 안 되면
    정렬 첫 챔버로 폴백한다 — 어느 쪽이든 정지 사실은 같고 `scope` 가 범위를 말해 준다.
    """
    from .db import fetch_incident_chamber

    groups: dict[str, dict[str, Any]] = {}
    for it in items:
        inc = it.get("incident_id")
        if not isinstance(inc, str) or not inc:
            logger.warning("incident_id 없는 열린 inhibit — skip (chamber=%s). "
                           "chamber_inhibits.incident_id 는 NOT NULL 이므로 응답 형상을 확인할 것",
                           it.get("chamber_id"))
            continue
        g = groups.setdefault(inc, {"incident_id": inc, "chambers": [], "scope": "chamber",
                                    "inhibited_at": None, "equipment_id": None})
        ch = it.get("chamber_id")
        if isinstance(ch, str) and ch:
            g["chambers"].append(ch)
        if it.get("scope") == "equipment":
            g["scope"] = "equipment"
        if g["equipment_id"] is None and it.get("equipment_id"):
            g["equipment_id"] = it.get("equipment_id")
        ts = _parse_ts(it.get("inhibited_at"))
        # 형제 행은 같은 트랜잭션에서 INSERT 되지만 DEFAULT NOW() 라 μs 가 다를 수 있다.
        # **가장 이른 것**을 그 정지의 시각으로 삼는다 — 정지 직전 창(ⓑ)의 기준점이라
        # 늦은 쪽을 쓰면 정지 이후(빈 구간)가 창에 섞인다.
        if ts is not None and (g["inhibited_at"] is None or ts < g["inhibited_at"]):
            g["inhibited_at"] = ts

    out = []
    for g in groups.values():
        g["chambers"].sort()
        g["chamber_id"] = fetch_incident_chamber(g["incident_id"]) or (
            g["chambers"][0] if g["chambers"] else ""
        )
        out.append(g)
    out.sort(key=lambda d: d["incident_id"])
    return out


# =============================================================================
# 2. 재료 — 회고 3층 + KB
# =============================================================================
_SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1}


def _cap_trigger_violations(
    violations: list[dict[str, Any]], limit: int, incident_id: str
) -> list[dict[str, Any]]:
    """ⓐ 발동 위반을 `limit` 건으로 자른다 — 심각한 것부터 남긴다 (2026-08-10 신설).

    **왜 필요한가**: ⓐ 만 유일하게 무제한이었다(ⓑ 는 센서 수, ⓒ 는 `release_past_limit`).
    스톰 알람 1건이 위반 56행이라 ReleaseJudgement 프롬프트가 ~11,417 토큰이 되어 예산
    10,288(max_model_len 12288 − 출력 2000)을 넘었고, vLLM 이 **400 Bad Request** 를
    돌려줘 폴백 Brief(자동 판단 없음)로 떨어졌다 — 2026-08-10 로컬 실측.

    `max_model_len` 상향으로는 못 막는다. 무제한 배열은 **어떤 천장이든 결국 넘는다** —
    더 큰 스톰이 오면 같은 자리가 다시 터진다. 그래서 상한이 근본이고 상향은 그 위의 여유다.

    정렬은 severity(CRITICAL 먼저) → rule_id → sensor_id. 잘리는 쪽이 **항상 덜 심각한
    위반**이 되도록 고정한다 (DB 정렬은 rule_id·sensor_id 뿐이라 여기서 severity 를 얹는다).

    ⚠️ 자른 사실은 **조용히 넘기지 않는다** (7장 "no silent caps"):
    ⓐ WARNING 로그 ⓑ `retrospect.trigger_violations_total` 로 payload 에 총량 동봉.
    근거가 얕아진 것을 사람이 보고 판단해야 한다.
    """
    if limit <= 0 or len(violations) <= limit:
        return violations
    ordered = sorted(
        violations,
        key=lambda v: (
            _SEVERITY_ORDER.get(str(v.get("severity") or "").upper(), 9),
            str(v.get("rule_id") or ""),
            str(v.get("sensor_id") or ""),
        ),
    )
    logger.warning(
        "ⓐ 발동 위반 %d건 → 상한 %d건으로 자름 (incident=%s). "
        "프롬프트 예산 보호 — 총량은 retrospect.trigger_violations_total 에 동봉된다",
        len(violations), limit, incident_id,
    )
    return ordered[:limit]


def collect_materials(group: dict[str, Any], settings: "Settings") -> dict[str, Any]:
    """회고 3층(ⓐⓑⓒ)을 DB 에서 조달한다. 각 층은 실패해도 빈 값으로 넘어간다(6-2).

    `trigger_alert_id` 는 상태 응답에 없으므로 `chamber_inhibits` 열린 행에서 가져온다 —
    ⓐ 의 유일한 열쇠다.
    """
    from .db import (
        fetch_breach_trend,
        fetch_open_inhibit,
        fetch_past_releases,
        fetch_trigger_violations,
    )

    chamber_id = group.get("chamber_id") or ""
    inhibited_at = group.get("inhibited_at")

    open_row = fetch_open_inhibit(group["incident_id"], chamber_id) or {}
    trigger_alert_id = open_row.get("trigger_alert_id")
    # 행 쪽 시각이 정본이다 — 게이트웨이 응답은 isoformat 문자열을 되파싱한 값이라
    # 마이크로초·tz 표기에서 어긋날 수 있고, 그 값이 UNIQUE 키(incident_id, inhibited_at)다.
    if open_row.get("inhibited_at") is not None:
        inhibited_at = open_row["inhibited_at"]

    window_min = int(settings.require("agent.release_trend_window_min"))
    past_limit = int(settings.require("agent.release_past_limit"))

    violations = fetch_trigger_violations(trigger_alert_id)
    violations_total = len(violations)
    violations = _cap_trigger_violations(
        violations, int(settings.require("agent.release_trigger_violation_limit")),
        group["incident_id"],
    )
    breaches, total = fetch_breach_trend(chamber_id, inhibited_at, window_min)
    past = fetch_past_releases(chamber_id, past_limit)

    if not violations:
        logger.warning("ⓐ 발동 알람 위반 0건 — incident=%s alert=%s. Brief 는 그 층 없이 나간다",
                       group["incident_id"], trigger_alert_id)
    if not past:
        logger.info("ⓒ 과거 해제 이력 0건 — chamber=%s (첫 정지면 정상). 'ready' 는 가드가 막는다",
                    chamber_id)

    return {
        "incident_id": group["incident_id"],
        "chamber_id": chamber_id,
        "chambers": group.get("chambers") or ([chamber_id] if chamber_id else []),
        "scope": group.get("scope") or "chamber",
        "equipment_id": group.get("equipment_id"),
        "trigger_alert_id": trigger_alert_id,
        "inhibited_at": inhibited_at,
        "trigger_violations": violations,
        "trigger_violations_total": violations_total,
        "persistent_sensors": breaches,
        "window_minutes": window_min,
        "window_total_alerts": total,
        "past_releases": past,
    }


def build_kb_query(materials: dict[str, Any]) -> str:
    """KB 검색어 — 발동 위반의 센서·룰·서술.

    `build_manual_query`(§3)와 같은 성질이다: error_manual 이 증상→원인→조치 구조라 서술이
    매칭 키다. 다른 점은 입력이 alert 이 아니라 **발동 알람의 위반 행**이라는 것뿐 —
    그래서 순수 함수로 따로 둔다(검색어 구성의 단일 소스).
    """
    parts: list[str] = []
    for v in (materials.get("trigger_violations") or [])[:3]:
        if not isinstance(v, dict):
            continue
        parts += [str(v.get(k)) for k in ("sensor_id", "rule_id", "description") if v.get(k)]
    for b in (materials.get("persistent_sensors") or [])[:2]:
        if isinstance(b, dict) and b.get("sensor_id"):
            parts.append(str(b["sensor_id"]))
    return " ".join(dict.fromkeys(p for p in parts if p)).strip()


def search_kb_evidence(materials: dict[str, Any]) -> dict[str, list[dict]]:
    """정비 매뉴얼 + 과거 사례 검색. 실패·0건이면 빈 리스트 + 계측(무중단).

    재료 0장 카운터는 `pipeline._note_kb_empty` 를 **공유한다** — "이 라운드에 근거가 몇 번
    비었나"가 한 수로 잡혀야 종료 요약이 의미를 갖는다(W16).
    """
    from .observe import ablated, record_retrieval
    from .pipeline import _evidence_top_k, _note_kb_empty, _search_top_k

    if ablated("kb"):                                   # ablation 라운드 (W40)
        return {"manual_hits": [], "cases": []}

    query = build_kb_query(materials)
    if not query:
        _note_kb_empty("error_manual", "검색어 없음(발동 위반 0건)")
        _note_kb_empty("historical_case", "검색어 없음(발동 위반 0건)")
        return {"manual_hits": [], "cases": []}

    top_k = _evidence_top_k()
    try:
        from ..vectordb import get_qdrant_client, search_kb

        client = get_qdrant_client()
        manual_pts = search_kb(client, "error_manual", query, limit=_search_top_k(top_k))
        case_pts = search_kb(client, "historical_case", query, limit=_search_top_k(top_k))
        record_retrieval("error_manual", query, manual_pts)
        record_retrieval("historical_case", query, case_pts)
        manual = [p.payload for p in manual_pts[:top_k]]
        cases = [p.payload for p in case_pts[:top_k]]
        if not manual:
            _note_kb_empty("error_manual", "검색 성공·히트 0")
        if not cases:
            _note_kb_empty("historical_case", "검색 성공·히트 0")
        return {"manual_hits": manual, "cases": cases}
    except Exception as exc:  # noqa: BLE001 — Qdrant 연결/검색 실패
        logger.warning("해제 근거 KB 검색 실패 — 근거 없이 진행 (%s: %s)", type(exc).__name__, exc)
        _note_kb_empty("error_manual", f"검색 실패: {type(exc).__name__}")
        _note_kb_empty("historical_case", f"검색 실패: {type(exc).__name__}")
        return {"manual_hits": [], "cases": []}


# =============================================================================
# 3. 프롬프트 (정본 = docs/Agent_프롬프트_라이브러리_v1.md §7)
# =============================================================================
SYSTEM_PROMPT = """[역할] 당신은 안전 정지(inhibit)된 챔버의 재가동 판단을 돕는 수석 분석가다.
엔지니어는 이 Brief를 읽고 재인증(Qual) 절차를 진행할지, 정지를 유지할지 정한다.

[⛔ 권한 — 가장 먼저 읽는다]
당신은 정지를 해제하지 않는다. 해제는 재인증 승인(R9)을 거친 사람만 한다.
readiness는 "해제하라"가 아니라 "지금 재가동 절차를 밟을 근거가 있나"에 대한 의견이다.

[입력]
- inhibit: 정지 정보 (chamber_id·scope·inhibited_at·trigger_alert_id·정지된 챔버 목록)
- retrospect: 회고 3층 — 코드가 DB에서 조회한 사실이다. 이 표의 수치만 인용한다.
  ⓐ trigger_violations  : 정지를 발동시킨 알람의 위반 (센서·룰·심각도·현재값·관리선)
     ↳ trigger_violations_total 이 이 배열보다 크면 **상위 일부만 실렸다**(예산 상한).
       그때는 “전체 N건 중 M건”임을 서술에 밝히고, 보이지 않는 위반을 추측하지 않는다.
  ⓑ persistent_sensors  : 정지 직전 창의 센서별 이탈률 (n1_alerts / window_total_alerts)
  ⓒ past_releases       : 같은 챔버의 지난 정지가 **무엇으로 해제됐나** (release_qual_id·released_by)
- manual_hits: 정비 매뉴얼 검색 결과 / cases: 유사 사례 검색 결과

[⚠️ 없는 축 — 물어보지도, 지어내지도 말 것]
"정지 이후 좋아졌나"는 이 Brief에 없다. 정지 중에는 그 챔버 wafer가 0장이라 측정 자체가
불가능하다. 재가동 근거는 **회고(ⓐⓑ)와 선례(ⓒ)**뿐이며, 그것이 이 판단의 정상 형태다.

[판정 절차 — 위에서부터 순서대로. 처음 확정되는 곳에서 멈춘다.]

STEP 1. 선례가 있나? (ⓒ past_releases)
  · 같은 챔버가 과거에 정지됐다가 **release_qual_id를 달고 해제된 이력**이 있으면,
    그 절차가 이번에도 유효한 경로다 → 그 qual_id를 근거로 인용한다.
  · 선례가 0건이면 그 사실을 명시한다. **"보통 이렇게 한다"를 지어내지 않는다.**

STEP 2. 발동 근거가 무엇이었나? (ⓐ + ⓑ)
  · 특정 센서가 관리선을 지속적으로 넘었다 → 그 센서의 물리적 원인이 확인·조치돼야 한다.
    매뉴얼(manual_hits)에 해당 증상의 조치가 있으면 precheck_items로 옮긴다.
  · 이탈이 여러 센서에 걸쳐 있거나 scope가 equipment면 공용 설비(가스·전원·냉각) 축이다 —
    챔버 하나가 아니라 장비 차원의 확인이 선행돼야 한다.

STEP 3. readiness를 고른다.
  · ready       : 선례가 있고(ⓒ), 발동 센서의 조치 경로가 매뉴얼·사례로 확인되며,
                  남은 확인 항목이 없다.
  · conditional : 진행해도 되나 **선행 확인 항목이 있다**. precheck_items를 반드시 채운다.
  · not_ready   : 근거가 부족하다(선례 0건 + 매뉴얼 0건), 또는 발동 원인이 특정되지 않았다.
                  확신이 서지 않으면 여기를 고른다 — 틀린 재가동은 되돌리기 어렵다.

[규칙]
1. 모든 수치는 retrospect·manual_hits·cases에서 인용한다. Brief에서 새 수치를 만들지 않는다.
2. precheck_items는 **매뉴얼·과거 사례에 실제로 있는 조치만** 쓴다. 없으면 빈 배열로 둔다.
   지어낸 점검 항목은 엔지니어를 엉뚱한 곳으로 보낸다.
3. 근거는 정확히 3개(각각 출처 태그 [SPC]/[CASE]/[KB]), 반증은 정확히 1개.
   근거가 3개보다 적으면 남는 자리에 "(해당 근거 없음 — 판정이 N개 신호에만 의존)"을 명시하고
   confidence를 낮춘다.
4. 반증 1개는 **"지금 재가동하면 안 되는 이유"** 를 쓴다. 없으면 없다고 쓴다.
5. ID는 입력에 실제로 있는 것만 인용한다 (QUAL-·CASE-·ALERT-·EM-·MAN- 등). 형식이 그럴듯한
   가짜 ID는 근거가 아니라 거짓 권위다.
   ※ 이 Brief 자신의 번호(RLS-)는 코드가 붙인다 — 본문에 쓰지 않는다.

[Brief 문체]
- summary 첫 문장 = 결론: "무엇을, 왜" 한 문장. 수식어 없이.
- 근거 3개는 서로 다른 출처 계열에서 하나씩 — 한 계열만으로 결론짓지 않는다.

[JSON 스키마로만 응답]"""

# 형태 참고용 — **판정 내용을 담지 않는다** (W56 중립화와 같은 이유: 예시에 완결된 판정을
# 두면 LLM 이 판단 대신 그것을 베낀다. 실측 round18 — 오답 7건이 7/7 전부 예시를 베꼈다).
SCHEMA_EXAMPLE = """{
  "readiness": "<ready | conditional | not_ready — 판정 절차로 확정한 것>",
  "summary": "<결론 한 문장 — 무엇을, 왜>",
  "evidence": [
    "<근거 1 — retrospect의 실제 값을 인용하고 출처를 표기 [SPC]>",
    "<근거 2 — 위와 다른 축 [CASE]/[KB]>",
    "<근거 3 — 반드시 3개. 없으면 '해당 근거 없음'이라고 쓴다>"
  ],
  "counter_evidence": "<지금 재가동하면 안 되는 이유. 없으면 그렇게 쓴다>",
  "confidence": "<근거 강도에서 산출>",
  "precheck_items": ["<매뉴얼·사례에 실제로 있는 확인 항목. 없으면 빈 배열>"]
}

⚠️ 꺾쇠(`<...>`)는 **채워 넣으라는 자리 표시**다. 그 문구를 그대로 쓰지 말고 이 사안의 실제
   값으로 채운다. report_id·incident_id·chamber_id 등 신원 필드는 **코드가 채우므로 쓰지 않는다.**
⚠️ confidence 는 **근거 층수에서 산출**한다. 3층(발동 근거 / 이탈 추이 / 선례·매뉴얼)이 다 서면
   0.80~0.90, 2층이면 0.60~0.75, 1층뿐이면 0.40~0.55, 재료가 없으면 0.20~0.35."""


def build_payload(materials: dict[str, Any], kb: dict[str, list[dict]]) -> str:
    """LLM 입력 직렬화 — §7 [입력] 항목 순서 그대로.

    `retrospect` 는 **코드가 조회한 사실**이라 그대로 싣는다. 프롬프트가 "이 표의 수치만
    인용한다"고 못 박는 근거가 이 블록이다.
    """
    retro = retrospect_from_materials(materials)
    return json.dumps(
        {
            "inhibit": {
                "chamber_id": materials.get("chamber_id"),
                "scope": materials.get("scope"),
                "equipment_id": materials.get("equipment_id"),
                "inhibited_chambers": materials.get("chambers"),
                "inhibited_at": _iso(materials.get("inhibited_at")),
                "trigger_alert_id": materials.get("trigger_alert_id"),
            },
            "retrospect": retro.model_dump(mode="json"),
            "manual_hits": kb.get("manual_hits") or [],
            "cases": kb.get("cases") or [],
        },
        ensure_ascii=False,
    )


def _iso(value: Any) -> Optional[str]:
    """datetime → isoformat 문자열 (None 은 그대로)."""
    return value.isoformat() if isinstance(value, datetime) else (value or None)


# =============================================================================
# 4. 가드 — 판정의 옳고 그름이 아니라 **근거 없는 재가동 권고**를 막는다
# =============================================================================
def known_refs(materials: dict[str, Any], kb: dict[str, list[dict]]) -> set[str]:
    """인용 가능한 실존 ID 집합 — 유령 인용 가드의 정답지.

    `supervisor.known_refs` 와 같은 원리이나 재료가 다르다: 여기는 발동 알람 ID·과거 해제의
    qual_id·incident_id 와 KB payload 의 문서 ID 가 정답지다. **재료 전체를 덮어야** 진짜
    유령만 잡힌다(2026-07-20 실측 11건 오탐이 그 반대 사례).
    """
    from .supervisor import _REF_KEYS

    refs: set[str] = set()
    if materials.get("trigger_alert_id"):
        refs.add(str(materials["trigger_alert_id"]))
    if materials.get("incident_id"):
        refs.add(str(materials["incident_id"]))
    for row in materials.get("past_releases") or []:
        if not isinstance(row, dict):
            continue
        for key in ("release_qual_id", "incident_id"):
            v = row.get(key)
            if isinstance(v, str) and v:
                refs.add(v)
    for bucket in ("manual_hits", "cases"):
        for item in kb.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            for key in _REF_KEYS:
                v = item.get(key)
                if isinstance(v, str) and v:
                    refs.add(v)
    return refs


def enforce_citation_guard(
    brief: ReleaseBrief, materials: dict[str, Any], kb: dict[str, list[dict]]
) -> ReleaseBrief:
    """가드 ①: 실존하지 않는 ID 인용을 `[미확인 인용: X]` 로 표시한다 (§0 창작 금지).

    문장을 지우지 않는다 — evidence 는 정확히 3개라 지우면 스키마가 깨지고, 서술 자체는
    유효할 수 있다. 지우는 것은 **거짓 권위**뿐이다. 정규식·접두 목록은 `supervisor` 와
    공유한다(`mask_unknown_refs`) — 갈라지면 빠진 접두가 무검사 통로가 된다.
    """
    from .supervisor import mask_unknown_refs

    refs = known_refs(materials, kb)
    masked, ghosts = mask_unknown_refs(list(brief.evidence), refs)
    if not ghosts:
        return brief
    logger.warning("인용 가드: brief=%s 미확인 ID %s", brief.report_id, sorted(set(ghosts)))
    note = f"[가드] 실존하지 않는 인용 {sorted(set(ghosts))} — 해당 근거의 신뢰도 낮음."
    return brief.model_copy(
        update={"evidence": masked, "counter_evidence": f"{brief.counter_evidence} / {note}"}
    )


def enforce_evidence_floor_guard(brief: ReleaseBrief, kb: dict[str, list[dict]]) -> ReleaseBrief:
    """가드 ②: **근거가 하나도 없는데 `ready`** 는 `conditional` 로 강등한다.

    재가동은 사람이 승인하지만, 화면에 "준비됨"이 떠 있으면 그 자체가 판단을 밀어낸다.
    선례(ⓒ)도 매뉴얼(KB)도 0인 상태는 "괜찮다"의 근거가 아니라 **모른다**이다.

    verdict 를 뒤집지 않고 한 단계만 내리는 이유는 `enforce_executable_guard` 와 같다 —
    진단을 지우는 것보다 "근거 부족"을 드러내는 편이 엔지니어에게 쓸모 있다. 강등된 건은
    `precheck_items` 에 그 사실이 남아 화면에서 이유를 볼 수 있다.
    """
    if brief.readiness != "ready":
        return brief
    has_past = bool(brief.retrospect.past_releases)
    has_kb = bool(kb.get("manual_hits")) or bool(kb.get("cases"))
    if has_past or has_kb:
        return brief
    note = "선례(과거 해제 이력) 0건 + 매뉴얼·사례 근거 0장 — 'ready' 근거 없음으로 강등."
    logger.warning("근거 하한 가드: brief=%s ready → conditional (선례·KB 모두 0)",
                   brief.report_id)
    return brief.model_copy(
        update={
            "readiness": "conditional",
            "summary": f"{brief.summary} / {note}",
            "precheck_items": [*brief.precheck_items, "재가동 근거 확보 — 선례·매뉴얼 부재"],
        }
    )


_NO_PRECHECK = "(확인 항목 미기재 — 엔지니어 확인 필요)"


def enforce_precheck_guard(brief: ReleaseBrief) -> ReleaseBrief:
    """가드 ③: `conditional` 인데 `precheck_items` 가 비었으면 표지를 채운다.

    "조건부"라고 해놓고 조건을 안 적으면 엔지니어는 무엇을 확인해야 할지 알 수 없다.
    사유를 **창작하지 않고** 미기재임을 드러낸다 — 없는 항목을 지어내는 것보다 낫다(§0).
    `enforce_rejection_reason_guard` 와 같은 계열의 가드다.
    """
    if brief.readiness != "conditional" or brief.precheck_items:
        return brief
    logger.info("확인항목 가드: brief=%s conditional 인데 precheck_items 비어 있음", brief.report_id)
    return brief.model_copy(update={"precheck_items": [_NO_PRECHECK]})


# =============================================================================
# 5. 생성
# =============================================================================
async def generate(
    group: dict[str, Any],
    settings: "Settings",
    backend: "LlmBackend | None" = None,
    *,
    materials: Optional[dict[str, Any]] = None,
    kb: Optional[dict[str, list[dict]]] = None,
) -> ReleaseBrief:
    """열린 inhibit 1건 → 해제 근거 Brief 1건.

    Args:
        group: `group_by_incident` 의 원소 (incident_id·chamber_id·scope·inhibited_at·chambers).
        materials/kb: 주입하면 조회·검색을 건너뛴다 (테스트가 DB·Qdrant 를 타지 않게).

    Returns: ReleaseBrief. LLM 실패 시 `not_ready` fallback (회고 재료는 그대로 실린다).
    """
    from .llm.client import generate_structured
    from .llm.factory import make_backend

    if materials is None:
        materials = collect_materials(group, settings)
    if kb is None:
        kb = search_kb_evidence(materials)

    report_id = make_release_report_id(
        materials.get("chamber_id") or "",
        materials.get("inhibited_at"),
        materials.get("trigger_alert_id"),
        materials["incident_id"],
    )
    retro = retrospect_from_materials(materials)
    identity = {
        "report_id": report_id,
        "incident_id": materials["incident_id"],
        "chamber_id": materials.get("chamber_id") or "",
        "scope": materials.get("scope") or "chamber",
        "trigger_alert_id": materials.get("trigger_alert_id"),
        "inhibited_at": materials.get("inhibited_at"),
    }

    if backend is None:
        backend = make_backend(settings)

    # ⚠️ LLM 에는 **판단 6필드만** 있는 모델(`ReleaseJudgement`)을 검증형으로 준다 —
    #    그게 곧 guided_json 스키마다. Brief 전체를 응답형으로 쓰면 신원·회고 칸이 "채울 수
    #    있는 자리"로 보이고, 매번 코드가 덮어쓰는 방어를 걸어야 한다(supervisor 가 그 형태).
    #    칸을 없애면 그 방어가 구조가 된다 — "덮어쓴다"보다 "쓸 수 없다"가 낫다.
    judgement = await generate_structured(
        backend,
        SYSTEM_PROMPT,
        build_payload(materials, kb),
        ReleaseJudgement,
        fallback=make_fallback_judgement,
        schema_example=SCHEMA_EXAMPLE,
        parse_retries=int(settings.require("agent.json_parse_retry")),
        call_retries=int(settings.require("llm.call_retry")),
    )
    brief = ReleaseBrief.compose(judgement, retrospect=retro, **identity)

    # 가드 순서 = 인용(①) → 근거 하한(②) → 확인 항목(③).
    #   ②가 readiness 를 바꾸므로 ③(conditional 검사)은 반드시 그 뒤다.
    brief = enforce_citation_guard(brief, materials, kb)
    brief = enforce_evidence_floor_guard(brief, kb)
    brief = enforce_precheck_guard(brief)

    logger.info(
        "release brief: %s (incident=%s, chamber=%s, scope=%s, readiness=%s, conf=%.2f, "
        "회고 ⓐ%d ⓑ%d ⓒ%d)",
        brief.report_id, brief.incident_id, brief.chamber_id, brief.scope,
        brief.readiness, brief.confidence,
        len(retro.trigger_violations), len(retro.persistent_sensors), len(retro.past_releases),
    )
    return brief


async def run_once(settings: "Settings", *, limit: int = _MAX_PER_TICK) -> int:
    """폴링 1주기 — 열린 inhibit 을 훑어 Brief 가 없는 건만 만들어 적재. 생성 건수를 반환."""
    from .db import release_brief_exists
    from .report_writer import save_release_brief

    groups = group_by_incident(fetch_inhibited(settings))
    made = 0
    for idx, g in enumerate(groups):
        if made >= limit:
            # 상한으로 자른 것을 조용히 넘기지 않는다 (7장 "no silent caps") — 남은 건은
            # 다음 주기에 만들어지므로 유실이 아니지만, 그 사실이 로그에 있어야 한다.
            # 남은 수는 **아직 안 본 건**이다(len - idx) — 이미 skip 한 건은 여기 안 든다.
            logger.info("해제 Brief 생성 상한 %d건 도달 — 미검토 %d건은 다음 주기에",
                        limit, len(groups) - idx)
            break
        if release_brief_exists(g["incident_id"], g.get("inhibited_at")):
            continue
        try:
            brief = await generate(g, settings)
        except Exception as exc:  # noqa: BLE001 — 한 건의 실패가 나머지를 막지 않게 (6-2)
            logger.exception("해제 Brief 생성 실패 — incident=%s: %s", g["incident_id"], exc)
            continue
        if save_release_brief(brief):
            made += 1
    return made


# =============================================================================
# 6. 폴러 — reanalysis 와 같은 형태(데몬 스레드 + 전용 커넥션)
# =============================================================================
def _loop(settings: "Settings", interval_sec: float, stop: threading.Event) -> None:
    """폴링 루프 — 예외가 나도 루프를 죽이지 않는다(6-2). 죽으면 정지가 조용히 방치된다."""
    import asyncio

    logger.info("해제 근거 Brief 폴러 시작 — %.0f초 주기, gateway=%s",
                interval_sec, gateway_base_url())
    while not stop.is_set():
        try:
            n = asyncio.run(run_once(settings))
            if n:
                logger.info("해제 근거 Brief %d건 생성", n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("해제 Brief 폴링 주기 실패(다음 주기에 재시도): %s", exc)
        stop.wait(interval_sec)
    logger.info("해제 근거 Brief 폴러 종료")


def start_poller(settings: "Settings") -> Optional[threading.Event]:
    """폴러를 데몬 스레드로 띄운다. 주기 미설정이면 **띄우지 않고** None (조용한 기본값 금지).

    Returns: 종료용 Event (호출측이 shutdown 때 set). 비활성이면 None.
    """
    try:
        interval = float(settings.require("agent.release_brief_poll_sec"))
    except Exception:  # noqa: BLE001 — 키 부재는 "기능 끔"이지 크래시가 아니다
        logger.warning("해제 근거 Brief 폴러 비활성 — params agent.release_brief_poll_sec 미설정. "
                       "챔버가 자동 정지돼도 해제 근거가 만들어지지 않는다(무응답 방지 안내)")
        return None
    stop = threading.Event()
    threading.Thread(target=_loop, args=(settings, interval, stop),
                     daemon=True, name="release-brief-poller").start()
    return stop
