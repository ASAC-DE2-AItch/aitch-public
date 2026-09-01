# -*- coding: utf-8 -*-
"""S1 배선 — limit 재설정 수치를 **B 가 써둔 행**에서 조달 (2026-08-04).

**무엇을 고정하나.** 스텁(`_stub_recalc_api`)이 `center` 를 +0.3σ 지어내던 것을,
`limit_corrections` 의 PROPOSED 행을 읽는 것으로 바꿨다. 그 배선의 갈래를 묶는다:

  ① 비-real 모드   — 실 DB 를 보지 않고 None(→ tool 이 스텁 자체 조달)
  ② 조달 실패      — None (수치 없이 진행. 지어내지 않는다)
  ③ 주입 우선순위  — pipeline 이 넘긴 recalc 가 tool 스텁보다 우선
  ④ 행 → dict 매핑 — 컬럼 이름이 프롬프트 §2 명세로 옮겨지는가
  ⑤ 인용 정답지    — B 채번 `correction_id` 가 실린다
  ⑥ kwargs 격리    — `recalc` 가 recipe tool 로 새지 않는다 (TypeError 방지)

⚠️ **실 DB 경로(AGENT_API_MODE=real + PROPOSED 행 존재)는 여기서 테스트하지 않는다.**
   B 모듈 import 와 Postgres 왕복이 필요하다. 그 경로는 실행으로 확인했다 —
   2026-08-04 실험에서 PROPOSED 9건 / AUTO_APPLIED 15건을 실제로 만들어 행 모양을 봤고,
   결과는 `notes/04_참조/대기_항목.md` 에 있다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # test_pipeline_fanout 재사용

from agent_service.app import pipeline as P  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"
TUNABLE = "13_process_shift_low.json"


def _alert(name: str = TUNABLE) -> AlertModel:
    return AlertModel.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


# B 가 실제로 써둔 행 (2026-08-04 실험 산출 — LIM-20260804-SIM_CH_1-3 을 그대로 옮김)
REAL_ROW = {
    "correction_id": "LIM-20260804-SIM_CH_1-3", "chamber_id": "SIM_CH_1",
    "recipe_id": "C6_0", "step": 4, "sensor_window": "settled", "sensor_id": "C61",
    "limit_version_before": "v1",
    "center_before": -786.3115914693878, "center_after": -809.146867820248,
    "ucl_after": -729.9219996271685, "lcl_after": -888.3717360133274,
    "delta_pct": 2.9041001809712124, "delta_sigma": 0.6875976611288724,
    "trigger_type": "periodic", "calc_window_n": 490,
    "shadow_false_alarm_reduction_pct": -50, "shadow_missed_detection": None,
    "method": "sigma",
}


# --- ① 비-real 모드 — 실 DB 를 보지 않는다 ---------------------------------------
def test_non_real_mode_does_not_touch_db(monkeypatch) -> None:
    """AGENT_API_MODE 가 real 이 아니면 DB 커넥션을 아예 열지 않는다.

    미기동 환경(CI·동료 로컬)에서 Postgres 를 찾으러 가면 느려지거나 죽는다.
    """
    def _boom():
        raise AssertionError("비-real 모드인데 DB 를 열었다")

    monkeypatch.setattr("agent_service.app.db.sa_connect", _boom, raising=False)
    assert P._fetch_recalc(_alert()) is None      # None → tool 이 스텁으로 자체 조달


# --- ② 조달 실패 — 수치 없이 진행(창작 금지) -------------------------------------
def test_fetch_failure_returns_none_not_stub(monkeypatch, caplog) -> None:
    """실 모드에서 조달이 실패하면 **None** 이다 — 스텁으로 메우지 않는다.

    recipe(S3)와 다른 선택이다. recipe 는 폴백 스텁에 `_provisional` 도장이 남아 "실물 아님"이
    드러나지만, limit 은 **수치 자체가 승인 화면에 뜨는 값**이라 대역값을 채우면 사람이
    그걸 보고 관리선을 옮긴다(헌법 1-1). 없으면 없다고 하는 편이 안전하다 — 규칙6.
    """
    from agent_service.app.config import ApiMode

    class _RealSettings:
        api_mode = ApiMode.REAL

    # ⚠️ 패치 대상은 `config` 모듈 쪽이다 — `_fetch_recalc` 가 함수 안에서 다시 import 한다.
    monkeypatch.setattr("agent_service.app.config.load_settings", lambda: _RealSettings())
    monkeypatch.setattr(
        "agent_service.app.db.sa_connect",
        lambda: (_ for _ in ()).throw(ConnectionError("postgres down")),
        raising=False,
    )
    with caplog.at_level("WARNING"):
        assert P._fetch_recalc(_alert()) is None
    hits = [r.getMessage() for r in caplog.records]
    # 의도한 이유로 통과하는지까지 본다 — B 모듈 import 실패도 같은 경고를 낸다(#109 선례).
    assert any("수치 없이 진행" in m and "ConnectionError" in m for m in hits), hits


# --- ③ 주입 우선순위 -------------------------------------------------------------
def test_injected_recalc_wins_over_tool_stub() -> None:
    """`limit.run(recalc=...)` 이 주어지면 tool 은 스텁을 만들지 않는다.

    배선의 핵심 — 깨지면 B 실물 값이 조용히 스텁으로 덮인다.
    """
    import asyncio

    from agent_service.app.config import load_settings
    from agent_service.app.llm.client import MockBackend
    from agent_service.app.tools import limit
    from test_pipeline_fanout import MOCK_BY_TYPE

    injected = P._to_recalc_dict(REAL_ROW)
    backend = MockBackend([MOCK_BY_TYPE["limit_option"]] * 3)
    asyncio.run(limit.run(_alert(), backend, load_settings(), recalc=injected))
    sent = json.dumps(backend.calls[-1], ensure_ascii=False)
    assert "LIM-20260804-SIM_CH_1-3" in sent
    assert "_provisional" not in sent          # 스텁 도장이 섞이지 않았다
    assert '"stub"' not in sent                # 섀도 basis 도 실물 표기


def test_tool_still_self_serves_stub_without_injection() -> None:
    """주입이 없으면 tool 이 스텁으로 자체 조달한다 — 단독 호출 경로 보존."""
    import asyncio

    from agent_service.app.config import load_settings
    from agent_service.app.llm.client import MockBackend
    from agent_service.app.tools import limit
    from test_pipeline_fanout import MOCK_BY_TYPE

    backend = MockBackend([MOCK_BY_TYPE["limit_option"]] * 3)
    asyncio.run(limit.run(_alert(), backend, load_settings()))
    assert "_provisional" in json.dumps(backend.calls[-1], ensure_ascii=False)


# --- ④ 행 → dict 매핑 ------------------------------------------------------------
def test_row_maps_to_prompt_spec() -> None:
    """컬럼이 프롬프트 §2 [입력] recalc 명세 이름으로 옮겨진다 — 값은 손대지 않는다."""
    d = P._to_recalc_dict(REAL_ROW)
    assert d is not None
    assert d["correction_id"] == "LIM-20260804-SIM_CH_1-3"   # B 채번 승계(6-4)
    assert d["delta_sigma"] == REAL_ROW["delta_sigma"]        # 상한 가드 정답지 — 반올림 금지
    assert d["delta_pct"] == REAL_ROW["delta_pct"]            # 스텁은 None 이었다
    assert d["trigger_type"] == "periodic"                    # 가한계 강등 가드가 읽는다
    assert d["method"] == "sigma"                             # control_limits join
    assert d["shadow_eval"]["false_alarm_reduction_pct"] == -50
    # B 가 채점하지 않는 것이 확정(2026-07-22) — "미채점"이지 "0건"이 아니다
    assert d["shadow_eval"]["missed_detection"] is None
    assert "_provisional" not in d                            # 실물엔 도장이 없다


def test_none_row_maps_to_none() -> None:
    """제안이 없으면 None — 규칙6(창작 금지)이 발동하도록."""
    assert P._to_recalc_dict(None) is None
    assert P._to_recalc_dict({}) is None


def test_method_defaults_to_sigma_when_join_missed() -> None:
    """활성 `control_limits` join 이 비면 sigma 로 둔다 — 분위수라고 단정하지 않는다."""
    row = {**REAL_ROW, "method": None}
    assert P._to_recalc_dict(row)["method"] == "sigma"


# --- ⑤ 인용 정답지 ---------------------------------------------------------------
def test_correction_id_is_included_in_citation_sources() -> None:
    """B 채번 `correction_id` 가 정답지에 실린다 — Brief 인용이 유령으로 오탐되면 안 된다.

    2026-07-20 에 knob_map 을 정답지에서 빠뜨려 실존 문서 ID 11건을 유령 처리한 이력이 있다.
    """
    from agent_service.app.supervisor import known_refs

    refs = known_refs([], _alert(), [[P._to_recalc_dict(REAL_ROW)]])
    assert "LIM-20260804-SIM_CH_1-3" in refs


# --- ⑥ kwargs 격리 ---------------------------------------------------------------
def test_recalc_does_not_leak_into_recipe_tool() -> None:
    """`recalc` 는 limit 에만 간다 — recipe.run 에 새면 모르는 kwarg 로 TypeError.

    `_tool_kwargs` 의 recipe 분기가 `recipe_inputs` 를 **통째로** 넘기는 구조라,
    limit 전용 키를 넣을 때마다 밟는 함정이다. `_LIMIT_ONLY` 등재로 막는다.
    """
    class _T:
        def __init__(self, name): self.name = name

    inputs = {"knob_map": {}, "cases": [], "limit_logs": [], "recipe_logs": [],
              "tuning": {"parameter_id": "C4"}, "recalc": P._to_recalc_dict(REAL_ROW)}

    to_recipe = P._tool_kwargs(_T("recipe"), inputs, None)
    assert "recalc" not in to_recipe and "limit_logs" not in to_recipe
    assert "tuning" in to_recipe                       # recipe 몫은 그대로 간다

    to_limit = P._tool_kwargs(_T("limit"), inputs, None)
    assert to_limit["recalc"]["correction_id"] == "LIM-20260804-SIM_CH_1-3"
    assert "tuning" not in to_limit                    # limit 은 튜닝안을 안 받는다
