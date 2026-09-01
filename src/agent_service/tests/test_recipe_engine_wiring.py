# -*- coding: utf-8 -*-
"""S3 배선 — recipe 튜닝 정량을 B5-4 실 엔진에서 조달 (2026-08-04 · PR #83 하류).

**무엇을 고정하나.** 스텁(`_stub_tuning_api`)을 실 엔진(`agent_b_spc.recipe_engine`)으로
바꾸면서 생긴 세 갈래를 테스트로 묶는다:

  ① 스텁 모드(API_MODE != real)   — 실 DB 를 보지 않는다
  ② 조달 실패                      — 스텁 폴백 + `_provisional` 도장 유지(무중단, 헌법 6-2)
  ③ 주입 우선순위                  — pipeline 이 넘긴 tuning 이 tool 의 스텁보다 우선

⚠️ **실 엔진 경로(API_MODE=real + 정상 DB)는 여기서 테스트하지 않는다.** B 모듈 import 와
   Postgres 왕복이 필요해 단위테스트 격리가 깨진다 — 그 경로는 실행으로 확인했고(2026-08-04,
   fixture 46건: 제안 11 / 에스컬 35) 결과를 `notes/04_참조/대기_항목.md` 에 남겼다.
   여기서 보는 것은 **그 경로가 실패했을 때 파이프라인이 사는가**다.
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
TUNABLE = "13_process_shift_low.json"          # 위반 C15(가스 측정) — 튜닝 대상이 있는 알람


def _alert(name: str = TUNABLE) -> AlertModel:
    return AlertModel.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


# --- ① 스텁 모드 — 실 DB 를 보지 않는다 -----------------------------------------
def test_stub_mode_does_not_touch_db(monkeypatch) -> None:
    """API_MODE 가 real 이 아니면 DB 커넥션을 아예 열지 않는다.

    스텁 모드에서 Postgres 를 찾으러 가면 미기동 환경(CI·동료 로컬)에서 느려지거나 죽는다.

    ⚠️ 모드를 **명시적으로 주입**한다 (2026-08-05 수정). 예전에는 주변 `.env` 가 mock 이라는
    가정에 기대고 있어서, 라운드를 실 엔진으로 돌리려고 `AGENT_API_MODE=real` 로 바꾸자
    이 테스트가 깨졌다 — 테스트가 개발자 로컬 설정에 의존하면 안 된다(②와 같은 패턴).
    """
    from agent_service.app.config import ApiMode

    class _MockSettings:
        api_mode = ApiMode.MOCK

    monkeypatch.setattr("agent_service.app.config.load_settings", lambda: _MockSettings())

    called = {"db": False}

    def _boom():
        called["db"] = True
        raise AssertionError("스텁 모드인데 DB 를 열었다")

    monkeypatch.setattr("agent_service.app.db.sa_connect", _boom, raising=False)
    out = P._compute_tuning(_alert(), "INC-TEST")
    assert called["db"] is False
    assert out is not None and out.get("_provisional") is True     # 스텁 도장


# --- ② 조달 실패 — 스텁 폴백(무중단) --------------------------------------------
def test_engine_failure_falls_back_to_stub(monkeypatch, caplog) -> None:
    """실 모드에서 엔진 조달이 실패하면 **스텁으로 진행하고 경고를 남긴다**.

    죽지 않는 것이 우선이고(6-2), 대신 조용히 진짜인 척하지 않는다 —
    `_provisional` 도장이 리포트 재료에 남아 "실물 아님"이 드러난다.
    """
    from agent_service.app.config import ApiMode

    class _RealSettings:
        api_mode = ApiMode.REAL

    # ⚠️ 패치 대상은 **`config` 모듈 쪽**이다. `_compute_tuning` 이 함수 안에서
    #    `from .config import ... load_settings` 로 매번 다시 가져오기 때문에,
    #    `pipeline` 모듈 속성(`P.load_settings`)을 갈아끼워도 아무 일도 일어나지 않는다.
    #    (자동 리뷰 지적 — PR #109. 실제로 죽은 줄이 하나 있었다)
    monkeypatch.setattr("agent_service.app.config.load_settings", lambda: _RealSettings())
    # B 리졸버 조회를 실패시킨다 (Postgres 미기동과 같은 모양)
    monkeypatch.setattr(
        "agent_service.app.db.sa_connect",
        lambda: (_ for _ in ()).throw(ConnectionError("postgres down")),
        raising=False,
    )
    with caplog.at_level("WARNING"):
        out = P._compute_tuning(_alert(), "INC-TEST")
    assert out is not None and out.get("_provisional") is True
    # ⚠️ **의도한 이유로 통과하는지**까지 본다 — B 모듈 import 실패(ModuleNotFoundError)도
    #    같은 경고를 내므로, 사유 문자열을 안 보면 "sqlalchemy 미설치" 를 "DB 장애 대응 검증"
    #    으로 착각한다. 실제로 2026-08-04 배선 중 그 상태였다.
    hits = [r.getMessage() for r in caplog.records]      # %-포맷을 적용한 최종 문자열
    assert any("스텁으로 진행" in m and "ConnectionError" in m for m in hits), hits


# --- ③ 주입 우선순위 — pipeline 이 넘긴 값이 tool 스텁보다 우선 ------------------
def test_injected_tuning_wins_over_tool_stub() -> None:
    """`recipe.run(tuning=...)` 이 주어지면 tool 은 스텁을 만들지 않는다.

    배선의 핵심이다 — 이게 깨지면 실 엔진 값이 조용히 스텁으로 덮인다.
    """
    import asyncio

    from agent_service.app.config import load_settings
    from agent_service.app.llm.client import MockBackend
    from agent_service.app.tools import recipe
    from test_pipeline_fanout import MOCK_BY_TYPE

    injected = {"parameter_id": "temp_target", "knob_axis": "temp", "basis_sensor": "C17",
                "value_current": 221.6, "value_proposed": 219.384, "delta_pct": -1.0,
                "direction": "down", "sensitivity": {"basis": "direction_only"},
                "escalate_reason": None, "applied_value": None}
    backend = MockBackend([MOCK_BY_TYPE["recipe_option"]] * 3)
    asyncio.run(recipe.run(_alert(), backend, load_settings(), tuning=injected))
    sent = json.dumps(backend.calls[-1], ensure_ascii=False)
    assert "temp_target" in sent
    assert "_provisional" not in sent            # 스텁 도장이 섞이지 않았다


def test_tool_still_self_serves_stub_without_injection() -> None:
    """주입이 없으면 tool 이 스텁으로 자체 조달한다 — 단독 호출 경로 보존.

    tool 을 직접 부르는 테스트·스모크가 배선 때문에 깨지면 안 된다.
    """
    import asyncio

    from agent_service.app.config import load_settings
    from agent_service.app.llm.client import MockBackend
    from agent_service.app.tools import recipe
    from test_pipeline_fanout import MOCK_BY_TYPE

    backend = MockBackend([MOCK_BY_TYPE["recipe_option"]] * 3)
    asyncio.run(recipe.run(_alert(), backend, load_settings()))
    assert "_provisional" in json.dumps(backend.calls[-1], ensure_ascii=False)


# --- ④ 인용 가드 정답지에 실리는가 ----------------------------------------------
def test_tuning_is_included_in_citation_sources() -> None:
    """튜닝안도 근거 재료다 — Supervisor 인용 가드의 정답지에 실려야 한다.

    실 엔진이 채운 `recipe_correction_id` 를 Brief 가 인용했을 때 유령으로 오탐되면 안 된다.
    2026-07-20 에 knob_map 을 정답지에서 빠뜨려 실존 문서 ID 11건을 유령 처리한 이력이 있다.
    """
    from agent_service.app.supervisor import known_refs

    tuning = {"recipe_correction_id": "RCP-20260804-SIMCH3-0001", "parameter_id": "C4"}
    refs = known_refs([], _alert(), [[tuning]])
    assert "RCP-20260804-SIMCH3-0001" in refs
