# -*- coding: utf-8 -*-
"""S9 Agent Copilot — 오케스트레이터 단위 테스트.

LLM 은 MockBackend(대본 주입)로 대체하고 **경로와 가드만** 잠근다. 판단 품질은 여기서
재지 않는다 — 그건 평가 하네스의 몫이고, pytest 는 통과/실패라 LLM 을 못 잰다.

특히 잠그는 것 셋 (전부 "조용히 틀리는" 종류다):
  ① 유령 인용 — 재료에 없는 ID 를 답이 인용하면 걷어내고 unsupported 로 올린다
  ② 액션 완료 주장 — "승인했습니다"는 사실과 다르다(읽기 전용). 정정 문구가 붙는가
  ③ 재료 0 — 빈손이 빈손으로 보이는가 (근거 0장이 정상처럼 나오는 것이 우리 사고 유형)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.agent_service.app import copilot  # noqa: E402
from src.agent_service.app.config import load_settings  # noqa: E402
from src.agent_service.app.llm.client import MockBackend  # noqa: E402
from src.agent_service.app.schemas.copilot import CopilotAnswer, CopilotContext  # noqa: E402


def _reply(**over) -> str:
    """LLM 대본 1건 — 기본은 '근거 있는 정상 응답'이고 필요한 필드만 덮어쓴다."""
    body = {
        "answer": "기준선 노후로 판정됐습니다.",
        "evidence": [{"kind": "report", "ref": "SUP-20260810-SIMCH3-0042", "label": "판정"}],
        "deeplink": "/incidents/INC-20260810-SIMCH3-0007",
        "scope_note": "SIM_CH_3 로 좁혀 답했습니다.",
        "unsupported": False,
    }
    body.update(over)
    return json.dumps(body, ensure_ascii=False)


_MATS = {"report": {"incident_id": "INC-20260810-SIMCH3-0007",
                    "report_id": "SUP-20260810-SIMCH3-0042"}}


def _amats(value: dict, used=None):
    """`collect_materials` 대체물 — **코루틴이어야 한다.**

    이 함수가 async 인 이유는 성능이 아니라 **교착 회피**다(2026-08-11 실측): 오케스트레이터가
    게이트웨이 프로세스 **안에서** 돌아 자기 자신을 부르므로, 동기 호출이면 이벤트 루프가
    자기 요청을 못 받아 타임아웃까지 멈춘다. 그때 응답은 200 이라 로그 없이는 안 보인다.
    """
    async def _f(*_a, **_k) -> tuple[dict, set]:
        return dict(value), set(used or ())
    return _f


# --- 의도 판정 -----------------------------------------------------------------
@pytest.mark.parametrize("question,expected", [
    ("이 추천 왜 나왔어?", "why"),
    ("근거가 뭐야", "why"),
    ("지난주 비슷한 일 있었나?", "similar"),
    ("지금 상태 어때?", "status"),
    ("안녕하세요", "general"),
    ("", "general"),
])
def test_detect_intent(question, expected):
    assert copilot.detect_intent(question) == expected


def test_intent_why_wins_over_similar():
    """두 어휘가 겹치면 why 가 이긴다 — 선언 순서가 곧 우선순위라는 계약."""
    assert copilot.detect_intent("왜 지난주랑 다르지") == "why"


# --- ① 유령 인용 가드 ------------------------------------------------------------
def test_citation_guard_drops_unknown_ref():
    ans = CopilotAnswer.model_validate_json(
        _reply(evidence=[{"kind": "case", "ref": "CASE-9999", "label": "지어낸 사례"}])
    )
    out, ghosted = copilot.enforce_citation_guard(ans, _MATS)
    assert out.evidence == []
    assert out.unsupported is True, "근거가 전부 걷혔으면 단정으로 남겨선 안 된다"
    assert ghosted is True, "실제로 걷어냈다는 사실을 알려야 한다"


def test_citation_guard_keeps_real_ref():
    ans = CopilotAnswer.model_validate_json(_reply())
    out, ghosted = copilot.enforce_citation_guard(ans, _MATS)
    assert [e.ref for e in out.evidence] == ["SUP-20260810-SIMCH3-0042"]
    assert out.unsupported is False and ghosted is False


def test_known_refs_scans_nested_material():
    """키 이름으로 좁히지 않는다 — 중첩 어디에 있든 ID 형태면 정답지에 든다."""
    mats = {"cases": [{"payload": {"deep": {"case_id": "CASE-0912"}}}]}
    assert "CASE-0912" in copilot.known_refs(mats)


# --- ② 읽기 전용 가드 ------------------------------------------------------------
@pytest.mark.parametrize("claim", [
    "승인했습니다.", "반려 완료", "관리선을 적용했습니다", "챔버를 정지했습니다",
])
def test_readonly_guard_flags_action_claims(claim):
    ans = CopilotAnswer.model_validate_json(_reply(answer=claim))
    out = copilot.enforce_readonly_guard(ans)
    assert "조치를 실행하지 않습니다" in out.answer


def test_readonly_guard_allows_suggestion():
    """권유는 막지 않는다 — 딥링크와 같은 뜻이다. 막는 건 완료형뿐."""
    ans = CopilotAnswer.model_validate_json(_reply(answer="승인하려면 S4 로 가세요."))
    assert copilot.enforce_readonly_guard(ans).answer == "승인하려면 S4 로 가세요."


# --- ③ 빈손·실패 경로 -------------------------------------------------------------
def test_empty_question_returns_fallback():
    out = asyncio.run(copilot.answer_question(
        "   ", CopilotContext(), MockBackend([]), load_settings()))
    assert out.unsupported is True
    assert out.answer, "빈 문자열을 돌려주면 화면이 '오다 말았다'와 구별 못 한다"


def test_llm_failure_falls_back(monkeypatch):
    """LLM 이 계속 깨진 JSON 을 뱉어도 예외가 밖으로 안 나간다 (헌법 6-3)."""
    monkeypatch.setattr(copilot, "collect_materials", _amats({}))
    out = asyncio.run(copilot.answer_question(
        "왜?", CopilotContext(), MockBackend(["{{{깨진", "{{{깨진", "{{{깨진", "{{{깨진"]),
        load_settings()))
    assert isinstance(out, CopilotAnswer) and out.unsupported is True


def test_gateway_down_does_not_raise(monkeypatch):
    """게이트웨이가 죽어도 답변은 계속된다 — 재료만 얕아진다 (헌법 6-2)."""
    monkeypatch.setenv(copilot.GATEWAY_ENV, "http://127.0.0.1:1")   # 즉시 연결 거부
    mats, used = asyncio.run(copilot.collect_materials(
        "지금 상태 어때", CopilotContext(chamber="SIM_CH_3"), "status", load_settings()))
    assert mats == {} and used == set()


# --- scope_note 는 코드가 쓴다 -----------------------------------------------------
# 🔴 2026-08-11 실측 회귀: LLM 에 맡겼더니 SCHEMA_EXAMPLE 의 incident_id 를 그대로 베껴,
#    chamber 만 준 질의에 없는 Incident 가 찍혔다. 하필 "주입이 맞는지 사람이 눈으로
#    확인하는 자리"라 지어내면 없느니만 못하다.
def test_scope_note_lists_only_given_context():
    note = copilot.build_scope_note(CopilotContext(chamber="SIM_CH_3"))
    assert "SIM_CH_3" in note
    assert "INC-" not in note, "안 준 컨텍스트가 실리면 안 된다"


def test_scope_note_when_global():
    assert "전역" in copilot.build_scope_note(CopilotContext())


# 🔴 2026-08-11 두 번째 실측: "준 것"이 아니라 **"쓴 것"** 을 적어야 한다. 손잡이 목록 질의는
#    Incident 를 전혀 안 쓰는데 *"… Incident INC-… 로 좁혀 답했습니다"* 가 떴고, 읽는 사람은
#    그 Incident 기준 답으로 오해한다. 거짓말은 아닌데 오해를 만드는 종류다.
_BOTH = CopilotContext(chamber="SIM_CH_3", incident_id="INC-20260713-CH3-001")


def test_scope_note_omits_unused_context():
    """knob 질의처럼 컨텍스트를 안 쓴 경우 — 좁혔다고 말하면 안 된다."""
    assert copilot.build_scope_note(_BOTH, set()) == "좁히지 않고 전역으로 답했습니다."


def test_scope_note_lists_only_used_field():
    note = copilot.build_scope_note(_BOTH, {"chamber"})
    assert "SIM_CH_3" in note and "INC-" not in note


def test_scope_note_reflects_used_from_pipeline(monkeypatch):
    """오케스트레이터가 돌려준 used 집합이 그대로 반영된다."""
    monkeypatch.setattr(copilot, "collect_materials",
                        _amats({"knob_map": [{"axis": "가스"}]}, used=set()))
    out = asyncio.run(copilot.answer_question(
        "손잡이 목록 알려줘", _BOTH, MockBackend([_reply(evidence=[])]), load_settings()))
    assert "INC-" not in out.scope_note and "전역" in out.scope_note


def test_scope_note_overrides_llm_value(monkeypatch):
    """LLM 이 무엇을 적어 보내든 서버 값으로 덮인다."""
    ctx = CopilotContext(chamber="SIM_CH_9")
    monkeypatch.setattr(copilot, "collect_materials", _amats(_MATS, used={"chamber"}))
    out = asyncio.run(copilot.answer_question(
        "왜?", ctx, MockBackend([_reply(scope_note="완전히 다른 소리")]), load_settings()))
    assert out.scope_note == copilot.build_scope_note(ctx, {"chamber"})


# --- unsupported 도 코드가 정한다 ---------------------------------------------------
# 🔴 2026-08-11 실측 회귀: 모델에 맡겼더니 챔버 상태로 **제대로 답하고도** true 를 달았고
#    (프롬프트로 두 번 조여도 안 바뀜), 화면이 멀쩡한 답에 "근거 없음 · LIMITED" 를 붙였다.
def test_unsupported_false_when_material_has_no_citable_id(monkeypatch):
    """인용할 ID 가 없는 재료(챔버 상태)로 답한 것은 **근거 있는 답**이다."""
    monkeypatch.setattr(copilot, "collect_materials",
                        _amats({"chambers": [{"chamber_id": "SIM_CH_3", "inhibited": False}]}))
    out = asyncio.run(copilot.answer_question(
        "지금 상태 어때?", CopilotContext(chamber="SIM_CH_3"),
        MockBackend([_reply(evidence=[], unsupported=True)]), load_settings()))
    assert out.unsupported is False, "재료로 답했으면 근거 없음이 아니다"


def test_unsupported_true_when_no_material(monkeypatch):
    monkeypatch.setattr(copilot, "collect_materials", _amats({}))
    out = asyncio.run(copilot.answer_question(
        "왜?", CopilotContext(), MockBackend([_reply(unsupported=False)]), load_settings()))
    assert out.unsupported is True, "재료가 0 이면 모델이 뭐라 하든 근거 없음이다"


def test_unsupported_true_when_all_citations_were_ghosts(monkeypatch):
    """인용 가능한 ID 가 있었는데 하나도 안 남았다 = 전부 지어낸 것."""
    monkeypatch.setattr(copilot, "collect_materials", _amats(_MATS))
    out = asyncio.run(copilot.answer_question(
        "왜?", CopilotContext(),
        MockBackend([_reply(evidence=[{"kind": "case", "ref": "CASE-9999", "label": "유령"}])]),
        load_settings()))
    assert out.unsupported is True and out.evidence == []


# --- 질문이 컨텍스트를 이긴다 --------------------------------------------------------
# 🔴 2026-08-11 실측: "챔버 상태들 브리핑해줘" 에 컨텍스트 챔버 하나로 좁혀 답했다. 자동 주입은
#    *"말 안 해도 알아듣게"* 하는 장치지 **말한 것을 덮는** 장치가 아니다.
@pytest.mark.parametrize("q,expected", [
    ("현재 챔버 상태들에 대해서 브리핑해줘", True),
    ("다른 챔버는?", True),
    ("모든 챔버 알려줘", True),
    ("지금 상태 어때?", False),
    ("이 챔버 괜찮아?", False),
])
def test_asks_fleet_wide(q, expected):
    assert copilot._asks_fleet_wide(q) is expected


# --- 딥링크 가드 — 없는 화면으로 보내지 않는다 -----------------------------------------
def test_deeplink_guard_drops_unknown_incident():
    """조회에 쓰지도 않은 목업 incident_id 로 링크를 만들면 사람이 빈 화면을 본다."""
    ans = CopilotAnswer.model_validate_json(
        _reply(deeplink="/incidents/INC-20260713-CH3-001", evidence=[]))
    assert copilot.enforce_deeplink_guard(ans, _MATS).deeplink is None


def test_deeplink_guard_keeps_known_incident():
    ans = CopilotAnswer.model_validate_json(_reply(evidence=[]))
    out = copilot.enforce_deeplink_guard(ans, _MATS)
    assert out.deeplink == "/incidents/INC-20260810-SIMCH3-0007"


def test_deeplink_guard_keeps_plain_route():
    """ID 가 없는 화면 경로(/incidents 등)는 검사 대상이 아니다."""
    ans = CopilotAnswer.model_validate_json(_reply(deeplink="/incidents", evidence=[]))
    assert copilot.enforce_deeplink_guard(ans, {}).deeplink == "/incidents"


# --- 대화 이력 ---------------------------------------------------------------------
def test_history_trimmed_to_recent_turns():
    """길게 실으면 예산만 먹는다 — 최근 N턴, 답변은 앞부분만."""
    hist = [{"question": f"q{i}", "answer": "가" * 500} for i in range(10)]
    out = copilot.build_history(hist)
    assert len(out) == copilot._HISTORY_TURNS
    assert out[-1]["question"] == "q9"
    assert len(out[0]["answer"]) == copilot._HISTORY_ANSWER_CHARS


def test_history_skips_malformed():
    assert copilot.build_history([None, "문자열", {"answer": "질문 없음"}]) == []


def test_history_is_not_evidence():
    """이력에 있는 ID 는 인용 정답지에 들어가면 안 된다 — 유령이 재생산된다."""
    hist = [{"question": "왜?", "answer": "CASE-9999 사례를 참고했습니다"}]
    p = copilot.build_payload("다른 챔버는?", CopilotContext(), {}, copilot.build_history(hist))
    assert "CASE-9999" in p                       # 맥락으로는 실린다
    assert "CASE-9999" not in copilot.known_refs({})   # 근거로는 안 쳐준다


# 🔴 2026-08-11 3차 실측: 걷어낸 것이 `sensor_map`·`knob_map`·`SIM_CH_3`·`CLAUDE.md 6-4`·`σ`
#    였다 — 재료의 **출처를 가리키는 말**이지 지어낸 사례 ID 가 아니다. 유령으로 셌더니
#    멀쩡한 답 4건(용어·챔버·손잡이)이 통째로 "근거 없음" 으로 뒤집혔다.
@pytest.mark.parametrize("ref", ["sensor_map", "knob_map", "SIM_CH_3", "CLAUDE.md 6-4", "σ", "glossary"])
def test_non_id_ref_is_dropped_but_not_a_ghost(ref):
    ans = CopilotAnswer.model_validate_json(
        _reply(evidence=[{"kind": "report", "ref": ref, "label": "출처 표기"}]))
    out, ghosted = copilot.enforce_citation_guard(ans, _MATS)
    assert out.evidence == [], "ID 가 아니면 카드에서는 뺀다(클릭·대조가 안 되므로)"
    assert ghosted is False, f"{ref!r} 는 환각이 아니라 출처 표기다"
    assert out.unsupported is False, "출처 표기 때문에 답을 근거 없음으로 만들면 안 된다"


def test_fabricated_id_is_still_a_ghost():
    """ID 형태를 갖춘 가짜는 그대로 유령이다 — 관용은 비-ID 에만."""
    ans = CopilotAnswer.model_validate_json(
        _reply(evidence=[{"kind": "case", "ref": "CASE-9999", "label": "지어냄"}]))
    out, ghosted = copilot.enforce_citation_guard(ans, _MATS)
    assert ghosted is True and out.unsupported is True


def test_no_citation_is_not_a_ghost():
    """🔴 모델이 근거를 **안 단 것**은 유령이 아니다 (2026-08-11 실측).

    둘을 같이 취급했더니 손잡이 목록·챔버 브리핑처럼 재료로 제대로 답한 것에도
    "근거 없음" 이 붙었다 — 그 재료들엔 인용할 ID 가 있어서 citable 이었기 때문이다.
    """
    ans = CopilotAnswer.model_validate_json(_reply(evidence=[]))
    out, ghosted = copilot.enforce_citation_guard(ans, _MATS)
    assert ghosted is False and out.unsupported is False


def test_answer_with_material_but_no_citation_is_grounded(monkeypatch):
    monkeypatch.setattr(copilot, "collect_materials", _amats(_MATS))
    out = asyncio.run(copilot.answer_question(
        "손잡이 목록", CopilotContext(), MockBackend([_reply(evidence=[])]), load_settings()))
    assert out.unsupported is False, "재료로 답했는데 인용만 안 한 것은 근거 있음이다"


# --- 사례 검색 payload 필터 ------------------------------------------------------------
# 🔴 2026-08-11 실측: "관리선 재설정으로 **해결된** 사례 있어?" 에 "없습니다" 라고 답했다 —
#    실제로는 1,053건이 있었다. 본문에 "개선"·"미개선" 이 한 글자 차이로 섞여 있어 임베딩이
#    구분을 못 하고, 상위 3개가 하필 실패분이었다. 성패는 텍스트가 아니라 payload 필드다.
@pytest.mark.parametrize("question,expected", [
    ("관리선 재설정으로 해결된 사례 있어?", {"is_success": True, "action_type": "limit_correction"}),
    ("정비했는데 실패한 사례", {"is_success": False, "action_type": "maintenance"}),
    ("레시피 튜닝 사례 보여줘", {"action_type": "recipe_r2r"}),
    ("PM 직후 사례는?", {"cycle_phase": "early"}),
    ("비슷한 사례 있어?", {}),                       # 조건 없으면 안 좁힌다
])
def test_build_case_filter(question, expected):
    assert copilot.build_case_filter(question, CopilotContext()) == expected


def test_case_filter_uses_sensor_but_not_chamber():
    """센서는 물어본 대상이라 좁히고, 챔버는 안 좁힌다 — 다른 챔버 사례가 오히려 참고된다."""
    f = copilot.build_case_filter("비슷한 사례", CopilotContext(chamber="SIM_CH_3", sensor="C11"))
    assert f == {"sensor_id": "C11"}


def test_success_and_failure_are_mutually_exclusive():
    """'실패' 가 '해결' 보다 먼저다 — "해결 안 된" 같은 문장이 성공으로 읽히면 안 된다."""
    assert copilot.build_case_filter("해결 안 된 사례", CopilotContext())["is_success"] is False


# 🔴 2026-08-11 실측: "관리선 재설정 사례가 몇 건이야?" 에 **"1건입니다"** 라고 단정했다
#    (실제 1,775건). 검색은 상위 3건만 주는데 모델이 그걸 전부로 읽었다. **없는 것을 지어내는
#    것보다 나쁘다** — 숫자는 그럴듯해서 검증 없이 믿긴다. 표본이라는 사실을 항상 싣는다.
def test_case_search_carries_total(monkeypatch):
    async def _fake(coll, q, settings, where=None):
        return ([{"case_id": "CASE-1"}], False, 1775) if coll == "historical_case" else ([], False, None)
    monkeypatch.setattr(copilot, "_kb_search", _fake)
    mats, _ = asyncio.run(copilot.collect_materials(
        "관리선 재설정 사례 몇 건이야?", CopilotContext(), "similar", load_settings()))
    assert mats["cases_shown_of_total"] == {"shown": 1, "total": 1775}


def test_total_none_when_count_unavailable(monkeypatch):
    """개수를 못 세면 None 이어야 한다 — 0 으로 두면 '없다' 로 읽힌다."""
    async def _fake(coll, q, settings, where=None):
        return ([{"case_id": "CASE-1"}], False, None) if coll == "historical_case" else ([], False, None)
    monkeypatch.setattr(copilot, "_kb_search", _fake)
    mats, _ = asyncio.run(copilot.collect_materials(
        "비슷한 사례", CopilotContext(), "similar", load_settings()))
    assert mats["cases_shown_of_total"]["total"] is None


# 🔴 2026-08-11 3차 실측: 화면이 붙이는 **목업 incident_id** 때문에 KB 검색이 통째로
#    건너뛰어졌다. Incident 를 못 찾으면 폴백이 `intent = "status"` 로 **갈아치웠고**,
#    그 결과 *"레시피 재산정 기록 몇 건?"* 이 챔버 조회로 끌려가 "총 건수를 알 수 없다"
#    가 됐다(같은 질문을 incident 없이 부르면 1,775건). **폴백은 재료를 보태는 것이지
#    질문을 바꾸는 것이 아니다.**
def test_missing_incident_still_searches_kb(monkeypatch):
    seen = {}

    async def _fake_report(*_a, **_k):
        return {}                                   # Incident 못 찾음

    async def _fake_kb(coll, q, settings, where=None):
        seen[coll] = True
        return ([{"case_id": "CASE-1"}], False, 1775) if coll == "historical_case" else ([], False, None)

    monkeypatch.setattr(copilot, "_incident_report", _fake_report)
    monkeypatch.setattr(copilot, "_kb_search", _fake_kb)
    monkeypatch.setattr(copilot, "_get_json", lambda *a, **k: _none())

    async def _none():
        return None

    mats, _ = asyncio.run(copilot.collect_materials(
        "레시피 재산정 기록 몇 건이야?", _BOTH, "general", load_settings()))
    assert seen.get("historical_case"), "Incident 를 못 찾아도 KB 는 봐야 한다"
    assert mats["cases_shown_of_total"]["total"] == 1775


# --- 근거 라벨은 사실이다 ------------------------------------------------------------
# 🔴 2026-08-11 실측: ID 는 실존이라 인용 가드를 통과했는데 **설명이 다른 건 이야기**였다.
#    카드가 "미처리 CRITICAL — C61 급변 및 장비 고장 의심" 이라고 했지만 그 Incident 는
#    WARNING 이고 사유는 "SHAP 상위 센서 부재로 원인 축 특정 불가" 였다(C61·인클로저는
#    다른 챔버 건). **유령 ID 보다 위험하다 — ID 가 맞으니 검증된 것처럼 보인다.**
_OPEN = {"open_incidents": [{"incident_id": "INC-1", "severity_max": "WARNING",
                             "title": "SHAP 상위 센서 부재로 원인 축 불명"}]}


def test_label_guard_overwrites_with_fact():
    ans = CopilotAnswer(answer="x", evidence=[
        {"kind": "report", "ref": "INC-1", "label": "미처리 CRITICAL — C61 급변 장비 고장"}])
    out = copilot.enforce_label_guard(ans, _OPEN)
    assert out.evidence[0].label.startswith("WARNING")
    assert "C61" not in out.evidence[0].label


def test_label_guard_leaves_case_labels_alone():
    """사례·매뉴얼 라벨은 **요약**이라 모델이 하는 일이 맞다 — 덮지 않는다."""
    ans = CopilotAnswer(answer="x", evidence=[
        {"kind": "case", "ref": "CASE-1", "label": "관리선 재설정으로 개선"}])
    out = copilot.enforce_label_guard(ans, {"cases": [{"case_id": "CASE-1"}]})
    assert out.evidence[0].label == "관리선 재설정으로 개선"


def test_label_guard_noop_without_open_incidents():
    ans = CopilotAnswer(answer="x", evidence=[
        {"kind": "report", "ref": "INC-1", "label": "그대로"}])
    assert copilot.enforce_label_guard(ans, {}).evidence[0].label == "그대로"


# --- 용어집 -------------------------------------------------------------------------
def test_glossary_hits_finds_term():
    hits = copilot._glossary_hits("실력치가 뭐야?")
    assert hits and hits[0]["term"] == "실력치"
    assert "ref" in hits[0], "정본 위치를 같이 줘야 사람이 확인하러 갈 수 있다"


def test_glossary_carries_industry_gap():
    """현업과 뜻이 반대인 용어는 그 사실이 재료에 실려야 한다 — 발표에서 걸리는 자리."""
    hits = copilot._glossary_hits("실력치")
    assert "industry" in hits[0] and "반대" in hits[0]["industry"]


def test_glossary_empty_when_no_term():
    assert copilot._glossary_hits("오늘 날씨 어때") == []


# 🔴 **프론트에 뜨는 말은 다 답할 수 있어야 한다** (사용자 요구 2026-08-11).
#    화면에서 직접 물어보다 미스가 났던 것들을 그대로 회귀 케이스로 박는다 —
#    "Nelson Rule" 만 등재돼 있어 음차("넬슨 룰")·약칭("nelson 룰")이 둘 다 빈손이었다.
@pytest.mark.parametrize("question", [
    "넬슨 룰이 뭔지 알아?", "nelson 룰이 뭔지알아?",      # 음차·약칭 (실측 미스)
    "shap 분석이 뭐야", "rf power가 뭐지",                 # 소문자·영문 (실측 미스)
    "focus ring 교체가 뭐야?", "436.2 sccm이 뭐야?",       # 부품·단위 (실측 미스)
    "3.1 σ은 무엇을 뜻해?", "레시피가 뭐야", "C65 예측이 말하는 값이 뭐야?",
    "tttm이 뭐야?", "실력치가 뭐야?", "가한계가 뭐지",
])
def test_frontend_vocabulary_is_answerable(question):
    """정의 재료가 하나라도 붙어야 한다 — 붙어야 LLM 이 지어내지 않고 답한다."""
    defs = copilot._glossary_hits(question) + copilot.sensor_hits(question)
    assert defs, f"용어 미스: {question!r} — glossary.yaml 에 term/aliases 를 추가하라"


# 센서 표시명은 **베끼지 않고** sensor_map.yaml 에서 읽는다 (6-4 단일 소스)
@pytest.mark.parametrize("question,code", [
    ("DC_Bias 가 뭐야", "C11"), ("RF Vpp 는?", "C62"),
    ("C17 설명해줘", "C17"), ("chamber_temp 이 뭐지", "C17"),
])
def test_sensor_hits_matches_code_and_display_name(question, code):
    hits = copilot.sensor_hits(question)
    assert code in [h["sensor"] for h in hits], f"{question!r} → {hits}"


def test_sensor_names_not_duplicated_into_glossary():
    """표시명을 용어집에 베끼면 정본이 둘이 된다 — 안 베꼈는지 확인."""
    import yaml

    from src.agent_service.app.config import REPO_ROOT
    smap = yaml.safe_load((REPO_ROOT / "config" / "sensor_map.yaml").read_text(encoding="utf-8"))
    gloss = yaml.safe_load((REPO_ROOT / "config" / "glossary.yaml").read_text(encoding="utf-8"))
    names = {str(v).lower() for v in smap.values()}
    terms = {str(t.get("term", "")).lower() for t in gloss["terms"]}
    assert not (names & terms), f"sensor_map 표시명이 용어집에 중복: {names & terms}"


def test_glossary_prefers_longer_term():
    """'관리 기준선' 이 걸렸으면 그게 먼저다 — 짧은 조각이 앞서면 엉뚱한 정의가 실린다."""
    hits = copilot._glossary_hits("관리 기준선이 뭐야?")
    assert hits[0]["term"] == "관리 기준선"


# 🔴 **2026-08-11 하루에 두 번 밟은 자리.** 재료 종류를 늘리면서 `EvidenceKind` 를 안 늘리면
#    모델이 그 재료에 맞는 kind 를 보내고 검증이 3회 실패해 **답 전체가 폴백으로 죽는다.**
#    증상이 "그 재료 무시"가 아니라 "답이 아예 없음"이라 원인이 안 보인다 — 그래서 잠근다.
# 🔴 **세 번째로 같은 자리를 밟고 나서** 구조로 막았다 (2026-08-11). kind 는 아이콘용
#    표시 힌트인데 Literal 검증에 걸리면 재시도 3회가 다 실패해 **답이 통째로** 사라졌다.
#    아이콘 한 값으로 문장을 버리는 건 우선순위가 거꾸로다 — 이제 흡수한다.
@pytest.mark.parametrize("raw,expected", [
    ("chambers", "chamber"),      # 모델이 재료 키를 그대로 쓴 것 (실측)
    ("cases", "case"), ("manuals", "manual"), ("knob_map", "knob"),
    ("KNOB_MAP", "knob"), (" manual ", "manual"),
    ("정체불명", "report"), (123, "report"), (None, "report"),
])
def test_evidence_kind_is_forgiving(raw, expected):
    from src.agent_service.app.schemas.copilot import EvidenceRef
    assert EvidenceRef(kind=raw, ref="X", label="y").kind == expected


def test_bad_kind_does_not_kill_the_answer():
    """kind 가 이상해도 answer·evidence 는 살아남아야 한다 — 폴백으로 죽지 않는다."""
    ans = CopilotAnswer.model_validate_json(
        _reply(evidence=[{"kind": "chambers", "ref": "SUP-20260810-SIMCH3-0042", "label": "x"}]))
    assert ans.answer and ans.evidence[0].kind == "chamber"


def test_evidence_kind_covers_every_material_key():
    from typing import get_args

    from src.agent_service.app.schemas.copilot import EvidenceKind

    # collect_materials 가 mats 에 넣는 키 전부 (늘리면 여기도 늘린다)
    material_keys = {"report", "cases", "manuals", "chambers", "open_incidents",
                     "knob_map", "glossary"}
    #: 부속 메타(필터·표본 수)는 근거 카드로 인용되지 않으므로 kind 가 없다 — 제외한다.
    meta_keys = {"cases_filter", "cases_shown_of_total", "manuals_shown_of_total",
                 "open_incidents_shown_of_total", "violations_truncated"}
    assert not (material_keys & meta_keys)
    #: 재료 키 → 그 재료를 인용할 때 쓰는 kind
    expected = {"report": "report", "cases": "case", "manuals": "manual",
                "chambers": "chamber", "open_incidents": "report",
                "knob_map": "knob", "glossary": "glossary"}
    assert material_keys == set(expected), "재료 키를 늘렸으면 이 표도 늘려라"
    kinds = set(get_args(EvidenceKind))
    missing = {v for v in expected.values() if v not in kinds}
    assert not missing, f"EvidenceKind 에 없는 kind: {missing} — 답이 통째로 폴백으로 죽는다"


# --- sensor_map 은 단일 소스에서 온다 (6-4) -----------------------------------------
def test_sensor_map_only_codes_present():
    """재료에 등장한 C코드만 싣는다 — 전체 맵을 실으면 예산이 샌다."""
    m = copilot.sensor_map_for({"violations": [{"sensor": "C11"}]}, CopilotContext())
    assert m.get("C11") == "DC_Bias"
    assert "C62" not in m


def test_sensor_map_empty_without_codes():
    assert copilot.sensor_map_for({}, CopilotContext()) == {}


def test_payload_carries_sensor_map():
    """§0 공통블록이 '표시명은 입력의 sensor_map 에 있는 것만' 이라 실려야 뜻이 산다."""
    p = copilot.build_payload("왜?", CopilotContext(), {"violations": [{"sensor": "C11"}]})
    assert "DC_Bias" in p and "sensor_map" in p


# --- 배선 전체 -------------------------------------------------------------------
def test_answer_question_injects_context(monkeypatch):
    """컨텍스트가 LLM 페이로드에 실려야 한다 — S9 의 존재 이유(자동 주입)."""
    seen: dict = {}
    monkeypatch.setattr(copilot, "collect_materials", _amats(_MATS))
    real = copilot.build_payload
    monkeypatch.setattr(copilot, "build_payload",
                        lambda *a, **k: seen.setdefault("p", real(*a, **k)))
    ctx = CopilotContext(chamber="SIM_CH_3", incident_id="INC-20260810-SIMCH3-0007")
    out = asyncio.run(copilot.answer_question("왜?", ctx, MockBackend([_reply()]), load_settings()))
    assert "SIM_CH_3" in seen["p"] and "INC-20260810-SIMCH3-0007" in seen["p"]
    assert out.unsupported is False and out.evidence
