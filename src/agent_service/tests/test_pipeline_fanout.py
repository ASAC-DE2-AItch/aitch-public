"""관통 테스트 — alert 1건 → 3조수 병렬 팬아웃 → 리포트 3건.

완료선(DoD)을 잠그는 회귀 테스트:
  · 게이트 통과(score≥31) alert → 리포트 정확히 3건 (recipe/limit/manual)
  · 게이트 미통과(score<31) alert → 리포트 0건 (기록만, B7) — LLM 미가동
  · report_id 포맷 = <PREFIX>-<YYYYMMDD>-<CHAMBER>-<SEQ> (LLM 응답값이 아니라 우리가 스탬프)
  · replay 소스: 30건 중 파손 1건 스킵 → 29건 파싱 (헌법 6-2 무중단)

C5-2: **3조수가 모두** LLM 실호출 경로다 (C5-1 까지는 recipe 만). 테스트는 백엔드를 주입해
엔진 없이 검증한다(의존성 주입의 요점) — 병렬 호출이라 순서 기반 대본은 tool↔응답이 어긋나므로
기본 백엔드는 요청 스키마로 대본을 고르는 RoutingBackend 다.
가드는 tool 별로 다르다: recipe=숫자·손잡이·delta / limit=숫자·변동폭상한·가한계 / manual=출처·downtime.

pytest-asyncio 미의존 — asyncio.run 으로 코루틴을 직접 구동한다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.llm.client import MockBackend  # noqa: E402
from agent_service.app.pipeline import handle_alert  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402
from agent_service.app.source import iter_replay_alerts  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"
SETTINGS = load_settings()

EXPECTED_OPTION_TYPES = {"recipe_option", "limit_option", "manual_option"}

# LLM 이 낼 법한 정상 응답 1건 (B5-4 미연결 → 규칙 6대로 수치 창작 없이 insufficient_evidence).
# report_id 는 일부러 틀린 값 — tool 이 덮어쓰는지 검증하기 위함.
MOCK_RECIPE_JSON = json.dumps(
    {
        "report_id": "RCP-LLM이-지어낸-값",
        "option_type": "recipe_option",
        "parameter_id": None,
        "value_proposed": None,
        "delta_pct": None,
        "shap_basis": [],
        "expected_effect": None,
        "rationale": "① N3 추세 위반 [SPC] ② 손잡이 매핑 미연결 ③ 유사 사례 없음 [CASE]",
        "hypothesis": "insufficient_evidence",
        "escalate_reason": "tuning 미연결 — 수치 창작 금지(§1 규칙 6)",
        "evidence_cards": [],
        "confidence": 0.4,
        "uncertainty": "B5-4 튜닝 API 미연결 — 정량 근거 없음",
    },
    ensure_ascii=False,
)


# C5-2: limit·maintenance 도 LLM 경로가 되면서 3조수가 **동시에** LLM 을 부른다.
# MockBackend 는 대본을 호출 순서대로 pop 하므로, 병렬 팬아웃에서는 어느 tool 이 몇 번째로
# 부를지에 리포트 내용이 좌우된다(순서 의존 = 깨지기 쉬운 테스트).
# → 아래 대본은 tool 별로 나누고, RoutingBackend 가 **요청 스키마로 골라** 돌려준다.
MOCK_LIMIT_JSON = json.dumps(
    {
        "report_id": "LIM-LLM이-지어낸-값",
        "option_type": "limit_option",
        "sensor_id": "C11",
        "method": "sigma",
        "center_after": None,
        "ucl_after": None,
        "lcl_after": None,
        "delta_pct": None,
        "delta_sigma": None,
        "rationale": "① 추세 룰 위주 위반 [SPC] ② 섀도 평가 미실행 ③ 유사 사례 없음 [CASE]",
        "escalate_reason": "recalc 스텁 — 섀도 평가 미실행",
        "evidence_cards": [],
        "confidence": 0.45,
        "uncertainty": "B4-3 재산정 API 대조 전 — 정량 근거 잠정",
    },
    ensure_ascii=False,
)

MOCK_MANUAL_JSON = json.dumps(
    {
        "report_id": "MNT-LLM이-지어낸-값",
        "option_type": "manual_option",
        "action": None,
        "target_component": None,
        "estimated_downtime_h": None,
        "similar_cases": [],
        "rationale": "① N1 급변 [SPC] ② 매뉴얼 검색 0건 — 조치 창작 금지 [KB]",
        "escalate_reason": "KB 미커버 — manual_hits 0건(§6)",
        "evidence_cards": [],
        "confidence": 0.3,
        "uncertainty": "manual_hits 0건 — 증상 매칭 불가",
    },
    ensure_ascii=False,
)

# option_type → 기본 대본. RoutingBackend 가 guided_json 스키마로 tool 을 식별해 고른다.
MOCK_BY_TYPE = {
    "recipe_option": MOCK_RECIPE_JSON,
    "limit_option": MOCK_LIMIT_JSON,
    "manual_option": MOCK_MANUAL_JSON,
}


class RoutingBackend(MockBackend):
    """요청 스키마(guided_json)의 option_type 으로 대본을 고르는 백엔드.

    MockBackend 의 순서 기반 대본은 3조수 병렬 호출에서 tool↔응답 짝이 어긋난다
    (recipe 가 limit 대본을 받는 식). 여기서는 **어느 tool 이 물었는지**로 고르므로
    asyncio.gather 의 실행 순서와 무관하게 결정적이다.

    by_type 에 없는 option_type 을 물으면 MockBackend 의 순서 대본으로 폴백한다 —
    "깨진 JSON 3연발" 같은 재시도 시나리오를 그대로 쓰기 위함.
    """

    def __init__(
        self,
        by_type: dict[str, str | Exception] | None = None,
        responses: list[str | Exception] | None = None,
    ) -> None:
        super().__init__(responses or [])
        self._by_type = dict(by_type or {})

    @staticmethod
    def _option_type(guided_json: dict | None) -> str | None:
        """guided_json(스키마)에서 option_type 리터럴을 읽는다 — 어느 tool 의 요청인지 식별."""
        prop = ((guided_json or {}).get("properties") or {}).get("option_type") or {}
        if "const" in prop:
            return prop["const"]
        enum = prop.get("enum")
        return enum[0] if enum else None

    async def complete(self, messages, *, guided_json=None, **kwargs):  # type: ignore[override]
        opt = self._option_type(guided_json)
        if opt in self._by_type:
            self.calls.append(messages)
            self.guided.append(guided_json)
            self.gen_params.append(
                {
                    "temperature": kwargs.get("temperature"),
                    "seed": kwargs.get("seed"),
                    "max_output_tokens": kwargs.get("max_output_tokens"),
                }
            )
            item = self._by_type[opt]
            if isinstance(item, Exception):
                raise item
            return item
        return await super().complete(messages, guided_json=guided_json, **kwargs)


def _calls_for(backend: MockBackend, option_type: str) -> list:
    """특정 tool 의 LLM 호출만 골라낸다 (병렬이라 호출 순서로는 못 고른다)."""
    return [
        msgs
        for msgs, g in zip(backend.calls, backend.guided)
        if RoutingBackend._option_type(g) == option_type
    ]


def _alert(name: str) -> AlertModel:
    return AlertModel.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


# 테스트용 knob_map — 실제 Qdrant 를 타지 않도록 명시 주입한다(외부 의존 없이 결정적).
# 형은 vectordb.fetch_knob_map 반환과 동일(axis·knob_param·confidence·doc_id·note).
STUB_KNOB_MAP = [
    {"axis": "가스", "knob_param": ["C4", "C5"], "confidence": "✅",
     "doc_id": "PK-TUNE-GAS", "note": "가스 setpoint 손잡이"},
    {"axis": "플라즈마/이온에너지", "knob_param": ["C1"], "confidence": "🔶",
     "doc_id": "PK-TUNE-PLASMA", "note": "C12=레짐 도장 손잡이 금지, 조정은 C1"},
]


# 테스트용 recipe 근거 재료 묶음 — 실제 Qdrant 를 타지 않도록 명시 주입(결정적).
STUB_RECIPE_INPUTS = {"knob_map": STUB_KNOB_MAP, "kb_hits": [], "cases": []}

# 테스트용 정비 근거 재료 묶음 — 같은 이유로 명시 주입 (C5-2).
# ⚠️ 이걸 안 넘기면 handle_alert 가 error_manual 컬렉션을 **실제로** 검색하러 간다(테스트 hang).
STUB_MANUAL_INPUTS: dict = {"manual_hits": []}


def _run(
    alert: AlertModel,
    responses: list[str | Exception] | None = None,
    recipe_inputs: dict | None = None,
    manual_inputs: dict | None = None,
):
    """백엔드를 주입해 handle_alert 구동. 기본 대본 = 3조수 각각의 정상 응답 1건.

    C5-2 부터 3조수가 **모두** LLM 을 부르므로 기본 백엔드는 RoutingBackend(스키마로 대본 선택)다.
    responses 를 주면 순서 기반 MockBackend 로 돌아간다 — 재시도/fallback 시나리오용.

    재료(recipe_inputs·manual_inputs)는 STUB 를 주입해 Qdrant 를 타지 않는다(테스트 결정성).
    Qdrant 조회·검색 경로는 별도 테스트(_load_knob_map/_search_*_evidence)에서만 검증한다.
    """
    backend = (
        MockBackend(responses) if responses is not None else RoutingBackend(MOCK_BY_TYPE)
    )
    return asyncio.run(
        handle_alert(
            alert,
            SETTINGS,
            backend=backend,
            recipe_inputs=recipe_inputs if recipe_inputs is not None else STUB_RECIPE_INPUTS,
            manual_inputs=manual_inputs if manual_inputs is not None else STUB_MANUAL_INPUTS,
        )
    )


# --- 완료선: 1 alert → 3 리포트 -----------------------------------------------
def test_gate_open_yields_exactly_three_reports() -> None:
    """게이트 통과 alert(17: score 84)는 정확히 3종 리포트를 낸다."""
    reports = _run(_alert("28_equipment_fault_high.json"))
    assert len(reports) == 3
    assert {r.option_type for r in reports} == EXPECTED_OPTION_TYPES


def test_gate_edge_31_is_open() -> None:
    """경계값 31(포함)도 게이트 통과 → 3건."""
    reports = _run(_alert("02_baseline_aging_gate_edge.json"))
    assert len(reports) == 3


def test_gate_closed_yields_zero_reports() -> None:
    """게이트 미통과 alert(01: score 22)는 리포트 0건 (기록만)."""
    reports = _run(_alert("01_baseline_aging_below_gate.json"))
    assert reports == []


def test_gate_closed_does_not_call_llm() -> None:
    """B7 미통과면 LLM 을 아예 부르지 않는다 — 게이트의 존재 이유(비용·부하)."""
    backend = MockBackend([])  # 대본 0건: 호출되면 RuntimeError
    reports = asyncio.run(handle_alert(_alert("01_baseline_aging_below_gate.json"), SETTINGS, backend=backend))
    assert reports == []
    assert backend.calls == []


# --- report_id 포맷 -----------------------------------------------------------
def test_report_id_format_and_prefixes() -> None:
    """report_id = <PREFIX>-<YYYYMMDD>-<CHAMBER>-<SEQ>. alert 의 날짜·챔버·SEQ 재사용."""
    reports = _run(_alert("28_equipment_fault_high.json"))  # ALERT-20260713-SIMCH4-0028
    by_type = {r.option_type: r.report_id for r in reports}
    assert by_type["recipe_option"] == "RCP-20260713-SIMCH4-0028"
    assert by_type["limit_option"] == "LIM-20260713-SIMCH4-0028"
    assert by_type["manual_option"] == "MNT-20260713-SIMCH4-0028"


def test_llm_report_id_is_overridden() -> None:
    """LLM 이 지어낸 report_id 는 버리고 우리가 스탬프한다 (네이밍 6-4·결정성은 우리 소관)."""
    reports = _run(_alert("28_equipment_fault_high.json"))
    recipe = next(r for r in reports if r.option_type == "recipe_option")
    assert "지어낸" not in recipe.report_id
    assert recipe.report_id == "RCP-20260713-SIMCH4-0028"


# --- C5-1: recipe = LLM 경로 / limit·maintenance = 아직 스텁 --------------------
def test_recipe_uses_llm_response() -> None:
    """recipe 는 LLM 응답을 반영한다 — 대본의 confidence·hypothesis 가 리포트에 실린다."""
    reports = _run(_alert("28_equipment_fault_high.json"))
    recipe = next(r for r in reports if r.option_type == "recipe_option")
    assert recipe.confidence == 0.4
    assert recipe.hypothesis == "insufficient_evidence"
    assert "스텁" not in recipe.rationale


def test_recipe_prompt_carries_alert_tuning_and_knob_map() -> None:
    """LLM 입력에 alert 원문 + 스텁 tuning(1-1) + knob_map(1-2)이 실리고, 아직 미연결인
    재료(kb_hits/cases)는 '없음'으로 정직히 전달된다(§0)."""
    backend = RoutingBackend(MOCK_BY_TYPE)
    asyncio.run(
        handle_alert(_alert("28_equipment_fault_high.json"), SETTINGS, backend=backend,
                     recipe_inputs=STUB_RECIPE_INPUTS, manual_inputs=STUB_MANUAL_INPUTS)
    )
    # C5-2: 3조수가 모두 LLM 을 부르므로 recipe 호출만 골라 본다(순서 의존 제거).
    recipe_calls = _calls_for(backend, "recipe_option")
    assert len(recipe_calls) == 1
    user_msg = recipe_calls[0][-1]["content"]
    assert "ALERT-20260713-SIMCH4-0028" in user_msg
    # 1-1: tuning 은 채워진다 — 위반 센서의 소폭 튜닝안(스텁 도장 _provisional 포함)
    assert '"tuning": null' not in user_msg
    assert '"_provisional": true' in user_msg  # ⚠️B5-4 합의 전 잠정 표지
    # 1-2: knob_map 이 실린다 — C12 손잡이 금지 근거가 프롬프트에 도달
    assert '"knob_map": null' not in user_msg
    assert "PK-TUNE-PLASMA" in user_msg  # knob_map 카드가 payload 에 실림
    # 아직 미연결인 재료(1-3)는 숨기지 않는다
    assert '"cases": []' in user_msg


def test_stub_tuning_api_within_d6_and_none_when_no_violation() -> None:
    """[1-1] B5-4 대역 스텁 tuning — 위반 센서에서 delta 를 뽑되 D6(±3%) 이내,
    위반이 없으면 None(→ 프롬프트 규칙6 insufficient_evidence)."""
    from src.agent_service.app.tools import recipe as R

    alert = _alert("28_equipment_fault_high.json")
    t = R._stub_tuning_api(alert)
    assert t is not None
    assert t["parameter_id"] == alert.violations[0].sensor  # 위반 센서를 손잡이 후보로
    assert t["value_current"] == alert.violations[0].current_value  # 실측 인용(창작 금지)
    assert abs(t["delta_pct"]) <= 3.0  # D6 상한 이내
    assert t["_provisional"] is True   # 스텁 도장

    # 위반 0건이면 tuning 없음 → 수치 창작 금지 경로
    no_viol = alert.model_copy(update={"violations": []})
    assert R._stub_tuning_api(no_viol) is None


def test_load_knob_map_returns_none_on_qdrant_failure(monkeypatch) -> None:
    """[1-2] Qdrant 조회 실패 시 _load_knob_map 은 None 반환(예외 전파 X) — 무중단(6-3)."""
    from src.agent_service.app import pipeline as P
    import src.agent_service.vectordb as V

    def _boom():
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(V, "get_qdrant_client", _boom)
    assert P._load_knob_map() is None  # 예외 삼키고 None — 파이프라인 죽지 않음


def test_recipe_build_payload_carries_knob_map() -> None:
    """[1-2] recipe.build_payload 는 주입된 knob_map 을 그대로 payload 에 싣는다.
    (knob_map 은 recipe 만 소비 — limit/maintenance 는 LLM 을 안 부르므로 payload 자체가 없다.)"""
    from src.agent_service.app.tools import recipe as R

    alert = _alert("28_equipment_fault_high.json")
    payload = json.loads(R.build_payload(alert, tuning=None, knob_map=STUB_KNOB_MAP))
    assert payload["knob_map"] == STUB_KNOB_MAP


def test_build_queries_are_collection_specific() -> None:
    """[1-3] 검색어는 컬렉션별로 다르다 — 지식은 센서 중심 짧게, 사례는 룰+서술 포함(실측 근거)."""
    from src.agent_service.app.tools import recipe as R

    alert = _alert("40_escalate_critical.json")  # C17 Chamber_Temp N3
    kb_q = R.build_kb_query(alert)
    case_q = R.build_case_query(alert)
    assert "C17" in kb_q and "Chamber_Temp" in kb_q
    assert alert.violations[0].rule_id not in kb_q  # 지식 검색엔 룰 미포함(짧게)
    assert alert.violations[0].rule_id in case_q     # 사례 검색엔 룰 포함(정확 매칭)
    assert len(case_q) > len(kb_q)                    # 사례가 더 김(서술 포함)

    # 위반 0건이면 빈 검색어 (검색 안 함 → 근거 없음으로 정직)
    no_viol = alert.model_copy(update={"violations": []})
    assert R.build_kb_query(no_viol) == "" and R.build_case_query(no_viol) == ""


def test_search_recipe_evidence_empty_on_qdrant_failure(monkeypatch) -> None:
    """[1-3] Qdrant 검색 실패 시 kb_hits/cases 는 빈 리스트(예외 전파 X) — 무중단(6-3)."""
    from src.agent_service.app import pipeline as P
    import src.agent_service.vectordb as V

    monkeypatch.setattr(V, "get_qdrant_client", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    out = P._search_recipe_evidence(_alert("40_escalate_critical.json"))
    assert out == {"kb_hits": [], "cases": []}


def test_kb_empty_counter_counts_search_failure(monkeypatch) -> None:
    """[W16] 검색 실패로 근거가 비면 컬렉션별 카운터에 잡힌다 — 조용히 넘어가지 않는다."""
    from src.agent_service.app import pipeline as P
    import src.agent_service.vectordb as V

    P.reset_kb_empty_counts()
    monkeypatch.setattr(V, "get_qdrant_client", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    P._search_recipe_evidence(_alert("40_escalate_critical.json"))

    counts = P.kb_empty_counts()
    # 실패 1회로 두 컬렉션이 각각 1건 — 실패도 "근거 0장"이므로 성공·실패를 한 수로 센다.
    assert counts == {"process_knowledge": 1, "historical_case": 1}
    P.reset_kb_empty_counts()


def test_kb_empty_counter_counts_successful_zero_hit(monkeypatch) -> None:
    """[W16] **검색이 성공하고 0건**인 경우도 잡힌다 — 종전에는 아무 흔적이 없던 경로."""
    from src.agent_service.app import pipeline as P
    import src.agent_service.vectordb as V

    P.reset_kb_empty_counts()
    monkeypatch.setattr(V, "get_qdrant_client", lambda: object())
    monkeypatch.setattr(V, "search_kb", lambda *a, **kw: [])          # 성공·히트 0
    out = P._search_recipe_evidence(_alert("40_escalate_critical.json"))

    assert out == {"kb_hits": [], "cases": []}                        # 흐름은 그대로(계측 전용)
    assert P.kb_empty_counts() == {"process_knowledge": 1, "historical_case": 1}
    P.reset_kb_empty_counts()


def test_kb_empty_counter_counts_knob_map_loss(monkeypatch) -> None:
    """[W16] knob_map 조회 실패도 계측 — 비면 `enforce_knob_guard` 가 통째로 비활성된다."""
    from src.agent_service.app import pipeline as P
    import src.agent_service.vectordb as V

    P.reset_kb_empty_counts()
    monkeypatch.setattr(V, "get_qdrant_client", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    assert P._load_knob_map() is None                                  # 흐름은 그대로(무중단 6-3)
    assert P.kb_empty_counts() == {"knob_map": 1}
    P.reset_kb_empty_counts()

    # 조회 성공·카드 0 도 같은 결과 — 가드가 꺼지는 건 동일하다
    monkeypatch.setattr(V, "get_qdrant_client", lambda: object())
    monkeypatch.setattr(V, "fetch_knob_map", lambda *a, **kw: [])
    P._load_knob_map()
    assert P.kb_empty_counts() == {"knob_map": 1}
    P.reset_kb_empty_counts()


def test_kb_empty_counter_silent_when_hits_exist(monkeypatch) -> None:
    """[W16] 근거가 있으면 카운터는 비어 있다 — 정상 경로에 잡음을 만들지 않는다."""
    from types import SimpleNamespace
    from src.agent_service.app import pipeline as P
    import src.agent_service.vectordb as V

    P.reset_kb_empty_counts()
    monkeypatch.setattr(V, "get_qdrant_client", lambda: object())
    monkeypatch.setattr(V, "search_kb",
                        lambda *a, **kw: [SimpleNamespace(payload={"doc_id": "PK-TUNE-TEMP"})])
    P._search_recipe_evidence(_alert("40_escalate_critical.json"))

    assert P.kb_empty_counts() == {}
    P.reset_kb_empty_counts()


def test_recipe_payload_carries_kb_hits_and_cases() -> None:
    """[1-3] recipe.build_payload 는 주입된 kb_hits·cases 를 그대로 payload 에 싣는다."""
    from src.agent_service.app.tools import recipe as R

    alert = _alert("40_escalate_critical.json")
    kb = [{"doc_id": "PK-TUNE-TEMP", "text": "온도 축"}]
    cases = [{"doc_id": "CASE-SYN-02147", "text": "C17 드리프트 N3"}]
    payload = json.loads(R.build_payload(alert, kb_hits=kb, cases=cases))
    assert payload["kb_hits"] == kb
    assert payload["cases"] == cases


def test_allowed_knobs_extracts_c_codes() -> None:
    """[2단계] knob_map 의 knob_param(리스트/문자열 섞임)에서 C코드만 추출 —
    'temp_setpoint'·'미확정'(C코드 없음)은 자연 제외(화이트리스트)."""
    from src.agent_service.app.tools import recipe as R

    km = [
        {"knob_param": ["C4(가스유량_A)", "C5(가스유량_B)", "C48(Main)"]},
        {"knob_param": "step_time(C41)"},
        {"knob_param": "temp_setpoint"},  # C코드 없음 → 제외
        {"knob_param": "미확정"},          # 제외
    ]
    assert R.allowed_knobs(km) == {"C4", "C5", "C48", "C41"}
    assert R.allowed_knobs(None) == set()  # 재료 없음 → 빈 집합


def test_allowed_knobs_admits_named_knob_temp_target() -> None:
    """[2단계] 명명 손잡이 temp_target(BL8) 은 C코드가 없어도 인정 — 화이트리스트 등재.
    옛 이름 temp_setpoint·미확정·오타는 여전히 제외(정합성만 열고 안전장치 유지)."""
    from src.agent_service.app.tools import recipe as R

    km = [
        {"knob_param": "C1(RF_Power_Set)"},
        {"knob_param": "temp_target"},       # 명명 손잡이 → 인정
        {"knob_param": "temp_setpoint"},     # 옛 이름 → 여전히 제외
        {"knob_param": "미확정"},             # 제외
        {"knob_param": "temp_targett"},       # 오타 → 제외(정확 일치만)
    ]
    assert R.allowed_knobs(km) == {"C1", "temp_target"}


def test_knob_guard_admits_temp_target_report() -> None:
    """[2단계] parameter_id=temp_target 인 튜닝안은 강등되지 않고 통과 — 온도 튜닝 출구 개방.
    (언제 이 출구로 보낼지 = 2026-07-26 멘토 확정 후 아래 온도 패턴 가드가 강제한다.)"""
    from src.agent_service.app.tools import recipe as R

    km = [{"knob_param": "temp_target"}, {"knob_param": "C1(RF_Power_Set)"}]
    r = R.RecipeOption(report_id="RCP-T", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id="temp_target", value_proposed=250.0,
                       hypothesis="process_condition_shift")
    assert R.enforce_knob_guard(r, km).parameter_id == "temp_target"  # 강등 안 됨


# --- 온도 패턴 가드 (규칙4-1 · 2026-07-26 멘토 확정) ----------------------------------
# "온도는 레짐이 아니라 가변 축" — 기본 경로는 튜닝이고, 급변·레짐 도장(C12)만 물러선다.
# 가르는 건 센서 신원(C17)이 아니라 **패턴**이라는 §0 원칙을 온도에 적용한 것이다.
def _temp_report(report_id: str = "RCP-T") -> "object":
    """temp_target 튜닝안 1건 — 온도 패턴 가드 입력용."""
    from src.agent_service.app.tools import recipe as R

    return R.RecipeOption(report_id=report_id, confidence=0.7, rationale="r", uncertainty="u",
                          parameter_id="temp_target", value_proposed=250.0, delta_pct=-1.2,
                          expected_effect="1스텝 기대", hypothesis="process_condition_shift")


def test_temp_pattern_guard_admits_drift_without_regime_stamp() -> None:
    """[규칙4-1] 급변 없음 + C12 미동반 온도 추세 → temp_target 튜닝안 통과 (신규 개통 경로)."""
    from src.agent_service.app.tools import recipe as R

    alert = _alert("47_process_shift_temp_drift.json")
    assert not R.has_shift(alert) and not R.has_regime_stamp(alert)  # 전제 확인
    out = R.enforce_temp_pattern_guard(_temp_report(), alert)
    assert out.parameter_id == "temp_target" and out.delta_pct == -1.2  # 무손상 통과
    assert out.escalate_reason is None


def test_temp_pattern_guard_demotes_when_regime_stamp_present() -> None:
    """[규칙4-1ⓑ] C12(레짐 도장)가 SHAP 상위에 함께 있으면 온도여도 강등 → regime_signal.

    기존 escalate fixture 10건(31~40)이 전부 이 모양이다 — 온도를 열어도 ④가 유지되는 근거.
    """
    from src.agent_service.app.tools import recipe as R

    alert = _alert("40_escalate_critical.json")  # C17/N3 + shap_top3 에 C12
    assert R.has_regime_stamp(alert)
    out = R.enforce_temp_pattern_guard(_temp_report(), alert)
    assert out.parameter_id is None and out.value_proposed is None and out.delta_pct is None
    assert out.hypothesis == "regime_signal"          # 레짐 도장이 찍혔으니 원인 가설을 바꾼다
    assert out.escalate_reason and "레짐 도장" in out.escalate_reason


def test_temp_pattern_guard_demotes_on_shift_but_keeps_hypothesis() -> None:
    """[규칙4-1ⓐ] 급변(N1/CRITICAL) 동반이면 온도 튜닝안 강등 — 단 hypothesis 는 손대지 않는다.

    급변의 원인은 Supervisor 급변 관문이 TTTM 갭으로 가른다(헌법 1-4). 여기서 regime_signal 을
    찍으면 트리를 ④로 밀어 ①(정비)을 가린다 — 가드는 '적용 가능 여부'만 판정한다(delta 가드와 동일).
    """
    from src.agent_service.app.tools import recipe as R

    alert = _alert("43_edge_multi_violation.json")  # N1 CRITICAL + N3, C12 없음
    assert R.has_shift(alert) and not R.has_regime_stamp(alert)
    out = R.enforce_temp_pattern_guard(_temp_report(), alert)
    assert out.parameter_id is None and out.value_proposed is None
    assert out.hypothesis == "process_condition_shift"  # 원인 가설은 LLM 것 — 유지
    assert out.escalate_reason and "급변" in out.escalate_reason


def test_temp_pattern_guard_ignores_non_temp_knobs() -> None:
    """[규칙4-1] 이 가드는 온도 축(temp_target)만 본다 — 가스·파워 튜닝안은 급변이어도 건드리지 않는다.

    (급변한 가스/파워 튜닝안을 막는 건 이 가드가 아니라 Supervisor 급변 관문의 몫이다.)
    """
    from src.agent_service.app.tools import recipe as R

    alert = _alert("43_edge_multi_violation.json")  # 급변 있음
    gas = R.RecipeOption(report_id="RCP-G", confidence=0.7, rationale="r", uncertainty="u",
                         parameter_id="C4", value_proposed=39.2, delta_pct=-2.0,
                         hypothesis="process_condition_shift")
    assert R.enforce_temp_pattern_guard(gas, alert).parameter_id == "C4"  # 무개입


def test_has_regime_stamp_reads_violations_and_spc_flags() -> None:
    """[규칙4-1ⓑ] 레짐 도장은 SHAP 뿐 아니라 위반 센서·spc_flags 에서도 잡는다(어디 실려도 동일)."""
    from src.agent_service.app.tools import recipe as R

    alert = _alert("45_edge_regime_signal_only.json")  # C12 가 위반 센서
    assert R.has_regime_stamp(alert)
    assert not R.has_regime_stamp(_alert("47_process_shift_temp_drift.json"))


def test_knob_guard_demotes_non_knob_sensor() -> None:
    """[2단계] parameter_id 가 허용 손잡이(화이트리스트)에 없으면 강등 → regime_signal."""
    from src.agent_service.app.tools import recipe as R

    km = [{"knob_param": ["C4(가스_A)", "C5(가스_B)"]}]  # 허용 = {C4, C5}

    # 진짜 손잡이(C4)는 통과
    ok = R.RecipeOption(report_id="RCP-A", confidence=0.7, rationale="r", uncertainty="u",
                        parameter_id="C4", value_proposed=39.2, delta_pct=-2.0,
                        hypothesis="process_condition_shift")
    assert R.enforce_knob_guard(ok, km).parameter_id == "C4"  # 그대로

    # 손잡이 아님(C17 온도 측정)은 강등
    bad = R.RecipeOption(report_id="RCP-B", confidence=0.7, rationale="r", uncertainty="u",
                         parameter_id="C17", value_proposed=1.0, delta_pct=-2.0,
                         hypothesis="process_condition_shift")
    out = R.enforce_knob_guard(bad, km)
    assert out.parameter_id is None and out.value_proposed is None  # 튜닝 수치 무효화
    assert out.hypothesis == "regime_signal"                        # 규칙4
    assert out.escalate_reason and "손잡이가 아님" in out.escalate_reason


def test_knob_guard_no_op_when_knob_map_empty() -> None:
    """[2단계] knob_map 이 비면(조회 실패) 판정 근거 없음 → 강등하지 않는다(오탐 방지)."""
    from src.agent_service.app.tools import recipe as R

    r = R.RecipeOption(report_id="RCP-C", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id="C17", value_proposed=1.0, delta_pct=-2.0)
    assert R.enforce_knob_guard(r, None).parameter_id == "C17"  # knob_map 없으면 통과
    assert R.enforce_knob_guard(r, []).parameter_id == "C17"


def test_number_guard_corrects_tampered_value() -> None:
    """[2단계] LLM 이 tuning 값과 다른 수치를 내면(변조 ④) B 값으로 교정 — 근거는 유지."""
    from src.agent_service.app.tools import recipe as R

    tuning = {"value_proposed": 39.2, "delta_pct": -2.0}
    r = R.RecipeOption(report_id="RCP-A", confidence=0.7, rationale="근거 서술", uncertainty="u",
                       parameter_id="C4", value_proposed=39.0, delta_pct=-2.5)  # LLM이 바꿈
    out = R.enforce_number_guard(r, tuning)
    assert out.value_proposed == 39.2 and out.delta_pct == -2.0  # B 값으로 교정
    assert out.rationale == "근거 서술"  # 근거는 유지


def test_number_guard_demotes_fabrication() -> None:
    """[2단계] tuning 이 없는데 LLM 이 수치를 내면(창작 ③) 무효화 + 에스컬레이션."""
    from src.agent_service.app.tools import recipe as R

    r = R.RecipeOption(report_id="RCP-B", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id="C4", value_proposed=39.2, delta_pct=-2.0)
    out = R.enforce_number_guard(r, None)  # tuning 없음
    assert out.value_proposed is None and out.delta_pct is None
    assert out.escalate_reason and "창작 금지" in out.escalate_reason


def test_number_guard_passes_faithful_citation() -> None:
    """[2단계] LLM 이 tuning 값을 충실히 인용하면 그대로 통과."""
    from src.agent_service.app.tools import recipe as R

    tuning = {"value_proposed": 39.2, "delta_pct": -2.0}
    r = R.RecipeOption(report_id="RCP-C", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id="C4", value_proposed=39.2, delta_pct=-2.0)
    out = R.enforce_number_guard(r, tuning)
    assert out.value_proposed == 39.2 and out.escalate_reason is None  # 통과


def test_number_guard_fills_missing_value_current_for_temp_target() -> None:
    """[2단계] 목표 지정형(temp_target·BL8)에 value_current 가 없으면 tuning 값으로 채운다(⑤).

    시뮬(`recipe_applier.py:100`)은 target 모드에서 value_current·value_proposed 를 **둘 다**
    요구하고 없으면 조용히 스킵한다 — 승인까지 통과한 뒤 아무 일도 안 일어나는 것을 막는다.
    """
    from src.agent_service.app.tools import recipe as R

    tuning = {"value_current": 60.5, "value_proposed": 58.2, "delta_pct": -1.2}
    r = R.RecipeOption(report_id="RCP-T1", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id=R.TEMP_KNOB, value_proposed=58.2, delta_pct=-1.2)  # current 누락
    out = R.enforce_number_guard(r, tuning)
    assert out.value_current == 60.5           # tuning 에서 채움
    assert out.value_proposed == 58.2          # 나머지 수치는 그대로
    assert out.escalate_reason is None         # 채웠으므로 강등하지 않는다


def test_number_guard_demotes_temp_target_when_current_unavailable() -> None:
    """[2단계] tuning 에도 value_current 가 없으면 목표 지정형은 강등한다(⑤) — 스킵될 안을 막는다."""
    from src.agent_service.app.tools import recipe as R

    tuning = {"value_proposed": 58.2, "delta_pct": -1.2}  # value_current 없음
    r = R.RecipeOption(report_id="RCP-T2", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id=R.TEMP_KNOB, value_proposed=58.2, delta_pct=-1.2)
    out = R.enforce_number_guard(r, tuning)
    assert out.value_proposed is None and out.delta_pct is None
    assert out.escalate_reason and "value_current" in out.escalate_reason


def test_number_guard_value_current_check_skips_non_target_knobs() -> None:
    """[2단계] 증분형 손잡이(C4 등)는 value_current 가 없어도 강등하지 않는다 — delta 로 적용된다."""
    from src.agent_service.app.tools import recipe as R

    tuning = {"value_proposed": 39.2, "delta_pct": -2.0}  # value_current 없음
    r = R.RecipeOption(report_id="RCP-T3", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id="C4", value_proposed=39.2, delta_pct=-2.0)
    out = R.enforce_number_guard(r, tuning)
    assert out.value_proposed == 39.2 and out.escalate_reason is None  # 통과


def test_number_guard_value_current_checked_before_tamper_correction() -> None:
    """[2단계] ⑤가 ④보다 먼저 — 변조 교정이 즉시 반환하므로 뒤에 두면 value_current 검사를 건너뛴다."""
    from src.agent_service.app.tools import recipe as R

    tuning = {"value_current": 60.5, "value_proposed": 58.2, "delta_pct": -1.2}
    r = R.RecipeOption(report_id="RCP-T4", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id=R.TEMP_KNOB, value_proposed=57.0, delta_pct=-2.9)  # 변조 + 누락
    out = R.enforce_number_guard(r, tuning)
    assert out.value_current == 60.5           # ⑤가 채웠고
    assert out.value_proposed == 58.2 and out.delta_pct == -1.2  # ④도 교정됐다


def test_delta_guard_demotes_over_limit(monkeypatch) -> None:
    """[2단계] LLM 이 D6(±3%) 초과 튜닝안을 내면 코드가 강등한다 — 승인 큐로 못 감(헌법 1-1)."""
    from src.agent_service.app.tools import recipe as R

    # 상한 내(-2%)는 통과
    within = R.RecipeOption(report_id="RCP-X", confidence=0.7, rationale="r", uncertainty="u",
                            parameter_id="C4", value_proposed=39.2, delta_pct=-2.0)
    out = R.enforce_delta_guard(within, max_pct=3.0)
    assert out.delta_pct == -2.0 and out.value_proposed == 39.2  # 그대로
    assert out.escalate_reason is None

    # 상한 초과(-4%)는 강등: 수치 제거 + escalate
    over = R.RecipeOption(report_id="RCP-Y", confidence=0.7, rationale="r", uncertainty="u",
                          parameter_id="C4", value_proposed=38.4, delta_pct=-4.0)
    out = R.enforce_delta_guard(over, max_pct=3.0)
    assert out.delta_pct is None and out.value_proposed is None  # 승인 큐로 새지 않게 제거
    assert out.escalate_reason and "상한 초과" in out.escalate_reason


def test_delta_guard_preserves_existing_escalate() -> None:
    """[2단계] LLM 이 이미 쓴 escalate_reason 은 가드가 강등해도 보존·병기한다."""
    from src.agent_service.app.tools import recipe as R

    r = R.RecipeOption(report_id="RCP-Z", confidence=0.5, rationale="r", uncertainty="u",
                       parameter_id="C4", value_proposed=38.4, delta_pct=-4.0,
                       escalate_reason="LLM이 이미 쓴 사유")
    out = R.enforce_delta_guard(r, max_pct=3.0)
    assert "LLM이 이미 쓴 사유" in out.escalate_reason  # 보존
    assert "상한 초과" in out.escalate_reason           # 병기


def test_all_three_tools_call_llm() -> None:
    """C5-2: 3조수가 **전부** LLM 경로다 — 더 이상 limit/maintenance 스텁이 아니다.

    (C5-1 까지는 recipe 만 호출하고 나머지는 confidence 0.0 스텁이었다.)
    """
    backend = RoutingBackend(MOCK_BY_TYPE)
    asyncio.run(
        handle_alert(
            _alert("28_equipment_fault_high.json"),
            SETTINGS,
            backend=backend,
            recipe_inputs=STUB_RECIPE_INPUTS,
            manual_inputs=STUB_MANUAL_INPUTS,
        )
    )
    assert len(backend.calls) == 3
    for option_type in EXPECTED_OPTION_TYPES:
        assert len(_calls_for(backend, option_type)) == 1


def test_tools_reflect_their_own_llm_response() -> None:
    """각 tool 이 **자기 대본**을 받는다 — 병렬 호출에서 응답이 뒤섞이지 않는다."""
    reports = _run(_alert("28_equipment_fault_high.json"))
    by_type = {r.option_type: r for r in reports}
    assert by_type["recipe_option"].confidence == 0.4
    assert by_type["limit_option"].confidence == 0.45
    assert by_type["manual_option"].confidence == 0.3
    for r in reports:
        assert "스텁" not in r.rationale  # 미연결 표지가 남아 있으면 안 된다


def test_recipe_falls_back_when_llm_unparseable() -> None:
    """LLM 이 계속 깨진 JSON 을 뱉어도 파이프라인은 죽지 않는다 (§6 fallback·헌법 6-3).

    파싱 재시도(D3=2) 소진 → fallback 리포트. 그래도 리포트 3건은 유지되고 report_id 는 정상.
    """
    # 3조수 × (최초 1 + 파싱 재시도 D3=2) = 9회 — 전부 깨진 JSON 이어도 3건은 나온다.
    reports = _run(_alert("28_equipment_fault_high.json"), responses=["깨진 JSON"] * 9)
    assert len(reports) == 3
    recipe = next(r for r in reports if r.option_type == "recipe_option")
    assert recipe.confidence == 0.0
    assert recipe.report_id == "RCP-20260713-SIMCH4-0028"
    assert recipe.escalate_reason is not None


# --- replay 소스 무중단 (헌법 6-2) --------------------------------------------
def test_replay_skips_broken_and_parses_all() -> None:
    """파손 1건(BROKEN)은 스킵되고 나머지만 방출된다 (파이프라인 무중단)."""
    alerts = list(iter_replay_alerts(FIXTURES))
    assert len(alerts) == 46  # 47건 중 파손 1건 제외 (엣지 7 — temp_drift 추가 2026-07-26)


def test_replay_end_to_end_report_count() -> None:
    """관통: 전체 재생 시 게이트 통과 건마다 3건 — 총 리포트 = 통과건수 × 3."""
    alerts = list(iter_replay_alerts(FIXTURES))
    gate_open = [a for a in alerts if a.context_score >= 31]
    total = 0
    for a in alerts:
        total += len(_run(a))
    assert total == len(gate_open) * 3


# =============================================================================
# C5-2: limit tool 가드 3종 (숫자 → 변동폭 상한 → 가한계) — 헌법 1-1 코드 강제
# =============================================================================
from agent_service.app.tools import limit as limit_tool  # noqa: E402
from agent_service.app.tools import maintenance as mnt_tool  # noqa: E402
from agent_service.app.schemas.report import LimitOption, ManualOption  # noqa: E402


def _limit_report(**over) -> LimitOption:
    """가드 입력용 최소 LimitOption — 재설정 수치가 실린 상태가 기본."""
    base = dict(
        report_id="LIM-20260713-SIMCH4-0028",
        sensor_id="C11",
        method="sigma",
        center_after=-302.4,
        ucl_after=-280.1,
        lcl_after=-324.7,
        delta_pct=None,
        delta_sigma=0.3,
        rationale="근거",
        uncertainty="불확실성",
        confidence=0.8,
    )
    base.update(over)
    return LimitOption(**base)


RECALC_OK = {
    "center_after": -302.4, "ucl_after": -280.1, "lcl_after": -324.7,
    "delta_pct": None, "delta_sigma": 0.3,
}


def test_limit_stub_recalc_stays_within_reset_cap() -> None:
    """B4-3 대역 스텁은 A2 상한(0.5σ) 이내 값만 낸다 — 스텁이 헌법 상한을 넘으면 안 된다."""
    recalc = limit_tool._stub_recalc_api(_alert("28_equipment_fault_high.json"), SETTINGS)
    cap = float(SETTINGS.require("limit_engine.reset_delta_max_sigma"))
    assert recalc is not None
    assert abs(recalc["delta_sigma"]) <= cap
    assert recalc["_provisional"] is True  # 스텁 도장이 재료에 남는다


def test_limit_number_guard_demotes_fabrication() -> None:
    """recalc 가 없는데 관리선 수치를 내면 무효화 + 에스컬레이션 (수치 창작 금지)."""
    out = limit_tool.enforce_number_guard(_limit_report(), None)
    assert out.center_after is None and out.ucl_after is None and out.delta_pct is None
    assert "창작 금지" in out.escalate_reason


def test_limit_number_guard_corrects_tampered_value() -> None:
    """LLM 이 B 값을 바꿔 쓰면 recalc 값으로 교정한다 (근거 서술은 유지)."""
    out = limit_tool.enforce_number_guard(_limit_report(center_after=-999.9), RECALC_OK)
    assert out.center_after == -302.4  # B 값으로 정정
    assert out.rationale == "근거"  # 서술은 살린다


def test_limit_reset_cap_guard_keeps_values_and_flags_approval() -> None:
    """A2 초과는 **승인 전환**이지 제안 금지가 아니다 — 수치를 유지한다.

    합의안 A2(B4-4): "정기 리캘리브레이션의 자동 적용 한계도 겸함 (초과 시 승인 전환)".
    헌법 1-1 ⓐ도 상한 초과를 "승인 필수"라 하지 금지라 하지 않는다. B 엔진도 값을 채운 채
    decision='needs_approval' 로 반환한다. 수치를 지우면 승인 화면에 승인할 안이 사라진다.
    (recipe D6 는 반대 — 설비 setpoint 라 제안 자체를 버린다. 복제 금물.)
    """
    over = {**RECALC_OK, "delta_sigma": 0.9}  # A2 = 0.5σ
    out = limit_tool.enforce_reset_cap_guard(_limit_report(), over, 0.5)
    assert out.center_after == -302.4          # 수치 유지 — 승인 대상이 살아 있다
    assert out.ucl_after == -280.1
    assert "승인 전환" in out.escalate_reason   # 자동 적용만 막힌다


def test_limit_reset_cap_guard_no_op_without_delta_sigma() -> None:
    """delta_sigma 가 없으면(=B 미제공) 판정 근거가 없어 강등하지 않는다 (오탐 방지)."""
    no_sigma = {k: v for k, v in RECALC_OK.items() if k != "delta_sigma"}
    out = limit_tool.enforce_reset_cap_guard(_limit_report(), no_sigma, 0.5)
    assert out.center_after == -302.4  # 통과


def test_limit_provisional_guard_blocks_during_phase() -> None:
    """가한계 구간이면 정기 재설정안을 내지 않는다 (§2 규칙4·R2).

    판정 근거는 **B가 주는 trigger_type** — C가 챔버 상태를 따로 읽어 판정하지 않는다
    (가한계 Phase는 B6-3 소유. C가 같은 판단을 다시 하면 B와 어긋난다).
    """
    out = limit_tool.enforce_provisional_guard(
        _limit_report(), {**RECALC_OK, "trigger_type": "provisional"}
    )
    assert out.center_after is None
    assert "가한계" in out.escalate_reason


def test_limit_provisional_guard_no_op_for_periodic() -> None:
    """정기(periodic) 재산정은 통과 — 가한계일 때만 막는다."""
    out = limit_tool.enforce_provisional_guard(
        _limit_report(), {**RECALC_OK, "trigger_type": "periodic"}
    )
    assert out.center_after == -302.4


def test_limit_provisional_guard_no_op_when_unknown() -> None:
    """trigger_type 이 없으면(B 미연결) 판정 근거가 없어 강등하지 않는다 (오탐 방지)."""
    out = limit_tool.enforce_provisional_guard(_limit_report(), RECALC_OK)
    assert out.center_after == -302.4


# =============================================================================
# C5-2: maintenance tool 가드 2종 (출처 → downtime)
# =============================================================================
def _manual_report(**over) -> ManualOption:
    base = dict(
        report_id="MNT-20260713-SIMCH4-0028",
        action="Focus Ring 교체",
        target_component="focus_ring",
        estimated_downtime_h=8.0,
        rationale="근거",
        uncertainty="불확실성",
        confidence=0.7,
    )
    base.update(over)
    return ManualOption(**base)


def test_manual_source_guard_demotes_when_no_material() -> None:
    """manual_hits·cases 가 모두 0건인데 조치를 내면 무효화 — 조치 창작 금지(§3 규칙1)."""
    out = mnt_tool.enforce_source_guard(_manual_report(), [], [])
    assert out.action is None and out.target_component is None
    assert "창작 금지" in out.escalate_reason


def test_manual_source_guard_passes_with_material() -> None:
    """재료가 하나라도 있으면 통과 — 문구 수준 충실도는 프롬프트·근거카드 소관."""
    out = mnt_tool.enforce_source_guard(_manual_report(), [{"doc_id": "EM-042"}], [])
    assert out.action == "Focus Ring 교체"


def test_manual_downtime_guard_strips_unsourced_value() -> None:
    """재료에 다운타임 근거가 없으면 추정값을 지우고 사유를 uncertainty 에 남긴다(§3 규칙4)."""
    out = mnt_tool.enforce_downtime_guard(_manual_report(), [{"doc_id": "EM-042"}], [])
    assert out.estimated_downtime_h is None
    assert "다운타임 정보 없음" in out.uncertainty


def test_manual_downtime_guard_keeps_sourced_value() -> None:
    """매뉴얼·사례에 다운타임이 있으면 그대로 둔다."""
    out = mnt_tool.enforce_downtime_guard(
        _manual_report(), [{"doc_id": "EM-042", "estimated_downtime_h": 8.0}], []
    )
    assert out.estimated_downtime_h == 8.0


def test_manual_query_differs_from_case_query() -> None:
    """error_manual 은 증상 중심(severity 포함), historical_case 는 룰 중심 — 검색어가 다르다."""
    a = _alert("28_equipment_fault_high.json")
    assert mnt_tool.build_manual_query(a) != mnt_tool.build_case_query(a)


# --- C5-2: 이동량 단위 = σ 정본 (B4-4 %→σ 전환 정합) --------------------------
def test_recalc_stub_leaves_delta_pct_null() -> None:
    """B 는 % 를 산출하지 않는다 — 스텁도 delta_pct 를 창작하지 않고 σ 만 낸다.

    (%는 center 를 분모로 써서 center≈0 센서에서 왜곡됨: 실측 C32 는 0.3σ 가 20% 로 표시됐다.)
    """
    recalc = limit_tool._stub_recalc_api(_alert("28_equipment_fault_high.json"), SETTINGS)
    assert recalc["delta_pct"] is None
    assert recalc["delta_sigma"] == 0.3


def test_limit_number_guard_corrects_tampered_delta_sigma() -> None:
    """LLM 이 σ 를 바꿔 쓰면 recalc 값으로 교정 — 화면에 뜨는 상한 근거가 틀리면 안 된다."""
    out = limit_tool.enforce_number_guard(_limit_report(delta_sigma=0.05), RECALC_OK)
    assert out.delta_sigma == 0.3


def test_limit_report_carries_sigma_not_pct() -> None:
    """관통: LLM 이 재설정안을 내면 σ 가 실리고 % 는 null 로 남는다 (승인 화면 오해 방지).

    가드는 '검문'이지 '채우기'가 아니다 — 값을 싣는 건 LLM 의 몫(규칙1 인용)이고,
    가드는 그 값이 recalc 와 다를 때만 교정한다. 그래서 여기선 값을 실은 대본을 쓴다.
    """
    alert = _alert("28_equipment_fault_high.json")
    recalc = limit_tool._stub_recalc_api(alert, SETTINGS)
    cited = json.dumps(
        {
            **json.loads(MOCK_LIMIT_JSON),
            "center_after": recalc["center_after"],
            "ucl_after": recalc["ucl_after"],
            "lcl_after": recalc["lcl_after"],
            "delta_pct": None,  # B 미산출 — 규칙1대로 비운다
            "delta_sigma": recalc["delta_sigma"],
        },
        ensure_ascii=False,
    )
    reports = asyncio.run(
        handle_alert(
            alert,
            SETTINGS,
            backend=RoutingBackend({**MOCK_BY_TYPE, "limit_option": cited}),
            recipe_inputs=STUB_RECIPE_INPUTS,
            manual_inputs=STUB_MANUAL_INPUTS,
        )
    )
    lim = next(r for r in reports if r.option_type == "limit_option")
    assert lim.delta_sigma == 0.3
    assert lim.delta_pct is None  # % 는 끝까지 null — 20% 같은 오해 값이 안 뜬다


def test_limit_number_guard_fills_omitted_sigma_when_numbers_present() -> None:
    """수치를 내면서 σ 만 빠뜨리면 변조로 보고 recalc 값으로 채운다 (상한 근거 누락 방지)."""
    out = limit_tool.enforce_number_guard(_limit_report(delta_sigma=None), RECALC_OK)
    assert out.delta_sigma == 0.3


# =============================================================================
# C5-2: KB 로그 페어링 — 벡터(서사) + Postgres(정밀 수치) 2단 근거
# =============================================================================
from agent_service.app import db as db_mod  # noqa: E402


def test_incident_ids_extracts_and_dedupes() -> None:
    """cases 에서 페어링 키만 뽑는다 — 없는 사례는 조용히 제외, 중복은 1건으로."""
    cases = [
        {"case_id": "CASE-1", "incident_id": "INC-A"},
        {"case_id": "CASE-2"},                      # 신규 데이터엔 incident_id 없을 수 있다
        {"case_id": "CASE-3", "incident_id": "INC-A"},  # 중복
        {"case_id": "CASE-4", "incident_id": "INC-B"},
    ]
    assert db_mod.incident_ids_of(cases) == ["INC-A", "INC-B"]


def test_incident_ids_empty_inputs() -> None:
    """None/빈 리스트/키 없음 → 빈 리스트 (조회 자체를 건너뛰게)."""
    assert db_mod.incident_ids_of(None) == []
    assert db_mod.incident_ids_of([]) == []
    assert db_mod.incident_ids_of([{"case_id": "X"}]) == []


def test_pairing_logs_empty_without_incident_ids(monkeypatch) -> None:
    """페어링 키가 없으면 Postgres 를 아예 안 친다 (불필요한 연결 방지)."""
    from agent_service.app import pipeline

    def _boom(*a, **k):  # 호출되면 실패
        raise AssertionError("incident_id 없는데 DB 조회 시도")

    monkeypatch.setattr(db_mod, "fetch_limit_logs", _boom)
    monkeypatch.setattr(db_mod, "fetch_recipe_logs", _boom)
    assert pipeline._fetch_pairing_logs([{"case_id": "X"}]) == {"limit_logs": [], "recipe_logs": []}


def test_pairing_logs_survive_db_failure_fast(monkeypatch) -> None:
    """Postgres 미기동이어도 **빨리 포기하고** 빈 결과로 진행한다 (헌법 6-2 무중단).

    타임아웃이 없으면 OS 기본값(수십 초~분)까지 파이프라인이 멈춘다 — "빈 결과로 진행"이
    의미가 있으려면 빨리 실패해야 한다. D4 지연 예산(30s) 안에 들어와야 하므로 여유 있게 검증.
    """
    import time

    monkeypatch.setattr(db_mod, "dsn", lambda: "postgresql://x:y@127.0.0.1:59999/none")
    monkeypatch.setattr(db_mod, "CONNECT_TIMEOUT_SEC", 1)
    started = time.monotonic()
    assert db_mod.fetch_limit_logs(["INC-A"]) == []
    assert db_mod.fetch_recipe_logs(["INC-A"]) == []
    assert time.monotonic() - started < 10  # 타임아웃이 빠지면 여기서 걸린다


def test_limit_payload_carries_pairing_logs() -> None:
    """limit payload 에 limit_logs 가 실린다 — '얼마나 바꿨고 섀도가 어땠나'."""
    alert = _alert("28_equipment_fault_high.json")
    logs = [{"correction_id": "LIM-0001", "shadow_false_alarm_reduction_pct": 51.2,
             "shadow_missed_detection": 0}]
    payload = json.loads(limit_tool.build_payload(alert, limit_logs=logs))
    assert payload["limit_logs"][0]["shadow_false_alarm_reduction_pct"] == 51.2


def test_recipe_payload_carries_pairing_logs() -> None:
    """recipe payload 에 recipe_logs 가 실린다 — '무엇을 얼마나 바꿔서 통했나'."""
    from agent_service.app.tools import recipe as recipe_tool

    alert = _alert("28_equipment_fault_high.json")
    logs = [{"recipe_correction_id": "RCP-0001", "parameter_id": "C4", "delta_pct": -2.1}]
    payload = json.loads(recipe_tool.build_payload(alert, recipe_logs=logs))
    assert payload["recipe_logs"][0]["parameter_id"] == "C4"


def test_tool_kwargs_routes_logs_to_right_tool() -> None:
    """limit_logs 는 limit 에만, recipe_logs 는 recipe 에만 — 서로 남의 로그를 받지 않는다."""
    from agent_service.app import pipeline
    from agent_service.app.tools import TOOLS

    inputs = {"knob_map": [], "kb_hits": [], "cases": [],
              "limit_logs": [{"correction_id": "L"}], "recipe_logs": [{"recipe_correction_id": "R"}]}
    by_name = {t.name: pipeline._tool_kwargs(t, inputs, {"manual_hits": []}) for t in TOOLS}
    assert "recipe_logs" in by_name["recipe"] and "limit_logs" not in by_name["recipe"]
    assert "limit_logs" in by_name["limit"] and "recipe_logs" not in by_name["limit"]
    assert "limit_logs" not in by_name["maintenance"]


# --- correction_id 주입 (B 요청 2026-07-21) -------------------------------------
def test_correction_id_injected_from_recalc(monkeypatch) -> None:
    """correction_id 는 B(recalc) 채번 — LLM 이 지어내도 recalc 값으로 덮어쓴다.

    db `limit_corrections.correction_id` 가 NOT NULL·UNIQUE 라, LLM 이 만든 ID 가 실리면
    승인 결과가 엉뚱한 이력 행에 붙는다(chamber_id 와 같은 이유).
    """
    from src.agent_service.app.tools import limit as L

    monkeypatch.setattr(L, "fetch_recalc", lambda alert, settings: {
        "correction_id": "LIM-20260713-SIMCH1-0042",
        "center_after": -302.4, "delta_sigma": 0.31, "method": "sigma",
    })
    bogus = json.loads(MOCK_LIMIT_JSON)
    bogus["correction_id"] = "LIM-LLM이-지어낸-값"
    out = asyncio.run(L.run(_alert("28_equipment_fault_high.json"),
                            MockBackend([json.dumps(bogus, ensure_ascii=False)]), SETTINGS))
    assert out.correction_id == "LIM-20260713-SIMCH1-0042"  # B 값이 이김


def test_correction_id_null_when_recalc_missing(monkeypatch) -> None:
    """B 미연결(recalc 에 correction_id 없음)이면 창작하지 말고 null — §0 정직성."""
    from src.agent_service.app.tools import limit as L

    monkeypatch.setattr(L, "fetch_recalc", lambda alert, settings: {"center_after": -302.4})
    bogus = json.loads(MOCK_LIMIT_JSON)
    bogus["correction_id"] = "LIM-LLM이-지어낸-값"
    out = asyncio.run(L.run(_alert("28_equipment_fault_high.json"),
                            MockBackend([json.dumps(bogus, ensure_ascii=False)]), SETTINGS))
    assert out.correction_id is None


# --- limit_version 승계 (계약 §6 동명 · 2026-07-22) -----------------------------
def test_limit_version_inherited_from_alert(monkeypatch) -> None:
    """limit_version 은 alert 승계 — LLM 이 지어내도 그 센서의 위반 값으로 덮어쓴다.

    승인 대기 중 B 정기 리캘리브레이션(헌법 1-1 예외 — 무승인 자동)이 관리선을 갱신할 수 있다.
    이 값이 틀리면 B 가 **엉뚱한 버전을 대체**한다(낙관적 잠금이 무너진다).
    """
    from src.agent_service.app.tools import limit as L

    monkeypatch.setattr(L, "fetch_recalc", lambda alert, settings: {"center_after": -302.4})
    bogus = json.loads(MOCK_LIMIT_JSON)
    bogus["sensor_id"] = "C31"            # fixture 28 의 위반 센서
    bogus["limit_version"] = "v99"        # LLM 이 지어낸 값
    out = asyncio.run(L.run(_alert("28_equipment_fault_high.json"),
                            MockBackend([json.dumps(bogus, ensure_ascii=False)]), SETTINGS))
    assert out.limit_version == "v1"      # alert violations[].limit_version 이 이긴다


def test_limit_version_null_when_sensor_not_in_violations(monkeypatch) -> None:
    """지목 센서가 위반 목록에 없으면 창작하지 말고 null — 틀린 버전보다 없는 게 낫다(§0)."""
    from src.agent_service.app.tools import limit as L

    monkeypatch.setattr(L, "fetch_recalc", lambda alert, settings: {"center_after": -302.4})
    bogus = json.loads(MOCK_LIMIT_JSON)   # sensor_id=C11 — fixture 28 의 위반은 C31 뿐
    bogus["limit_version"] = "v99"
    out = asyncio.run(L.run(_alert("28_equipment_fault_high.json"),
                            MockBackend([json.dumps(bogus, ensure_ascii=False)]), SETTINGS))
    assert out.limit_version is None


# --- knob_map 브리징 필드 (2026-07-21 process_shift 0/5 원인) --------------------
def test_knob_map_carries_measurement_bridge_fields() -> None:
    """knob_map 은 related_sensors·cause_effect 를 실어야 한다.

    SHAP 은 측정 센서(C15/C16/C31)를 지목하는데 knob_param 에는 setpoint 만 있다.
    둘을 잇는 필드를 빼고 보내면 LLM 이 "knob_map 에 없음 → 레짐 신호"로 오판한다
    (Round4 실측: process_shift 0/5 의 직접 원인).
    """
    from src.agent_service.app.tools import recipe as R

    knob_map = [{"axis": "가스", "knob_param": ["C4", "C5", "C48"],
                 "related_sensors": ["C4", "C5", "C48", "C15", "C16"],
                 "cause_effect": "SHAP 가스 유량(C15/C16) 기여 → gas setpoint 조정",
                 "doc_id": "PK-TUNE-GAS"}]
    payload = R.build_payload(_alert("28_equipment_fault_high.json"), knob_map=knob_map)
    assert "related_sensors" in payload and "C16" in payload      # 측정 센서가 LLM 에 도달
    assert "cause_effect" in payload                              # 측정→setpoint 연결도


def test_recipe_prompt_explains_measurement_to_setpoint() -> None:
    """프롬프트 규칙4 가 '측정 센서는 knob_param 에 원래 없다'를 명시해야 한다."""
    from src.agent_service.app.tools import recipe as R

    assert "related_sensors" in R.SYSTEM_PROMPT
    assert "측정 센서는 원래 거기 없다" in R.SYSTEM_PROMPT


# --- W21: applied_value (승인 화면 '현재값') ------------------------------------
# value_current(명목값)만 실으면 승인 화면이 장비 실제와 다른 값을 보여준다. B 가 주는
# applied_value 는 **사실**이지 LLM 의 판단이 아니므로 report_id 처럼 무조건 승계한다.
# Optional 필드라 안 채워도 에러가 안 나므로 — 값 자체를 검증한다(리포트 Optional/null 함정).
def test_applied_value_is_carried_into_report_schema() -> None:
    """[W21] RecipeOption 이 applied_value 를 갖고, 미지정 시 None 이다."""
    from src.agent_service.app.tools import recipe as R

    r = R.RecipeOption(report_id="RCP-AV", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id="C4", value_current=40.0, applied_value=39.6,
                       value_proposed=39.2, delta_pct=-2.0)
    assert r.applied_value == 39.6
    assert r.value_current == 40.0                      # 명목값과 **별개 필드**여야 한다
    bare = R.RecipeOption(report_id="RCP-AV2", confidence=0.7, rationale="r", uncertainty="u")
    assert bare.applied_value is None                   # 첫 스텝·escalate·온도 경로


def test_applied_value_reaches_supervisor_and_approval_payload() -> None:
    """[W21] 요약(Supervisor)·승인 jsonb 둘 다 applied_value 를 통과시킨다.

    이 둘 중 하나라도 빠지면 **에러 없이** 승인 화면의 '현재값'이 명목값으로 남는다 —
    계약 §5 중첩형 삭제 때 정량 필드가 전량 null 로 샜던 것과 같은 유형(헌법 7장).
    """
    from src.agent_service.app import report_writer, supervisor
    from src.agent_service.app.tools import recipe as R

    r = R.RecipeOption(report_id="RCP-AV3", confidence=0.7, rationale="r", uncertainty="u",
                       parameter_id="C4", value_current=40.0, applied_value=39.6,
                       value_proposed=39.2, delta_pct=-2.0)
    assert supervisor.summarize_report(r).get("applied_value") == 39.6
    assert "applied_value" in report_writer._QUANT_FIELDS["recipe_option"]


def test_prompt_teaches_applied_value_and_null_case() -> None:
    """[W21] 프롬프트가 두 값의 차이와 **null 일 때 지어내지 말 것**을 가르친다.

    입력 설명만 늘리고 규칙을 안 주면 LLM 이 명목값을 그대로 '현재값'이라 쓴다.
    """
    from src.agent_service.app.tools import recipe as R

    sp = R.SYSTEM_PROMPT
    assert "applied_value" in sp
    assert "명목값" in sp and "실제로 걸린 값" in sp
    assert "지어내지 마라" in sp or "지어내지" in sp     # null 경로 가드


# --- W22: 온도 1스텝 소진 (Incident당 1회) --------------------------------------
def test_temp_guard_does_not_double_demote_b_escalation() -> None:
    """[W22-ⓐ] B 가 이미 에스컬로 보낸 건(parameter_id=None)은 온도 가드가 건드리지 않는다.

    B `_escalate` 는 parameter_id=None 을 준다. 우리 가드는 temp_target 한 축만 보므로
    통과해야 한다 — 여기서 또 강등하면 escalate_reason 이 두 번 겹쳐 붙는다(중복 강등).
    """
    from src.agent_service.app.tools import recipe as R

    r = R.RecipeOption(report_id="RCP-T2", confidence=0.4, rationale="r", uncertainty="u",
                       parameter_id=None, value_proposed=None, delta_pct=None,
                       escalate_reason="이번 Incident 에서 온도는 이미 1회 조정됨 — 사람 판단",
                       hypothesis="insufficient_evidence")
    alert = _alert("47_process_shift_temp_drift.json")   # 급변·C12 없음(기존 헬퍼 재사용)
    out = R.enforce_temp_pattern_guard(r, alert)
    assert out.escalate_reason == r.escalate_reason      # 사유가 덧붙지 않았다
    assert out.hypothesis == "insufficient_evidence"     # 가설도 안 바뀐다


def test_prompt_teaches_temp_one_step_and_reason_carryover() -> None:
    """[W22-ⓑ] 온도 1스텝 정책과 "B 사유를 옮겨라"를 프롬프트가 가르친다.

    규칙 6은 tuning==null 만 다뤘다. tuning 이 **있는데** escalate_reason 만 채워져 오는
    경로(B `_escalate`)가 비어 있어, LLM 이 "튜닝안 없음"으로만 쓰면 왜 없는지가 사라진다.
    """
    from src.agent_service.app.tools import recipe as R

    sp = R.SYSTEM_PROMPT
    assert "Incident당 1스텝" in sp                       # 규칙 4-2
    assert "escalate_reason 을 그대로 옮긴" in sp          # 규칙 6-1
    assert "왜 없는지가 사라진다" in sp                    # 이유까지 가르친다
