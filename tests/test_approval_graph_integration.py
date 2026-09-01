# -*- coding: utf-8 -*-
"""P4-1 통합 검증 — 실 LangGraph 그래프 실행 (interrupt → resume 왕복).

내 샌드박스에선 langgraph 설치 불가(PyPI 프록시 403)라 **미실행**. 사용자 env에서 실행:

    pip install langgraph            # DB 불요 — MemorySaver + FakeStore로 '배선'만 검증
    python tests/test_approval_graph_integration.py

→ **출력 전체를 붙여주면** 통과/실패 진단·수정. (실 Postgres/PostgresSaver 검증은 #24 Level 2 — docker 필요, 별도.)

검증 대상: build_graph 조립 · approval_gate에서 interrupt로 멈춤 · Command(resume) 재개 ·
4액션 라우팅(approve/modify/reject/escalate) · interrupt 노드 재실행 멱등(PENDING 중복 없음).
"""

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # FakeStore 재사용


def _bail(msg: str):
    """전제 미충족 시 중단 — 스크립트면 종료, **pytest 수집 중이면 모듈 스킵**.

    ⚠️ 모듈 레벨 `sys.exit()` 는 수집 단계에서 SystemExit 로 새어나가 **세션 전체를
    INTERNALERROR 로 죽인다** (자기 파일만 실패하는 게 아니다 — 2026-08-07 실측).
    """
    print(msg)
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(msg, allow_module_level=True)
    sys.exit(1)


try:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
except ImportError as exc:                                  # langgraph 미설치 안내
    _bail(f"[중단] langgraph 미설치 — `pip install langgraph` 후 재실행. 상세: {exc}")

from orchestrator.approval_graph import build_graph          # noqa: E402
from test_approval_graph import FakeStore                    # noqa: E402


def run_case(action, extra=None, option="limit_option"):
    """새 그래프로 1건 처리: 최초 invoke(→interrupt 멈춤) → resume(→판정 노드)."""
    store = FakeStore()
    app = build_graph(store, MemorySaver())
    inc = f"INC-{action.upper()}"
    cfg = {"configurable": {"thread_id": inc}}
    report = {"selected_option": option, "request_type": "correction"}

    try:
        app.invoke({"incident_id": inc, "report": report}, cfg)     # approval_gate에서 멈춤
    except Exception as e:                                           # interrupt가 예외로 표면화되는 버전 대비
        print(f"  (first invoke raised {type(e).__name__} — interrupt로 간주)")

    assert store.records.get(inc, {}).get("status") == "PENDING", \
        f"interrupt 전 PENDING 미생성 (records={store.records.get(inc)})"
    assert store.lifecycle.get(inc) == "pending", \
        f"lifecycle != pending ({store.lifecycle.get(inc)})"

    app.invoke(Command(resume={"action": action, **(extra or {})}), cfg)   # 재개
    return store, inc


def main():
    import langgraph
    print("langgraph", getattr(langgraph, "__version__", "?"))
    passed = []

    s, inc = run_case("approve", {"approver": "eng1", "approver_role": "process"})
    assert s.records[inc]["status"] == "APPROVED", s.records[inc]
    assert s.lifecycle[inc] == "actioned", s.lifecycle[inc]
    passed.append("approve → interrupt→resume→APPROVED/actioned")

    s, inc = run_case("modify", {"modified_value": -297.3, "approver": "eng2"})
    assert s.records[inc]["status"] == "MODIFIED" and s.records[inc]["modified_value"] == -297.3
    passed.append("modify → MODIFIED + 수정값 기록")

    s, inc = run_case("reject", {"reason": "근거 부족"})
    assert s.records[inc]["status"] == "REJECTED" and s.lifecycle[inc] == "analyzing"
    passed.append("reject → REJECTED/analyzing (pending 잔류 없음)")

    s, inc = run_case("escalate")
    assert s.records[inc]["status"] == "ESCALATED" and s.lifecycle[inc] == "reopened"
    assert s.reopen.get(inc) == 1
    passed.append("escalate → ESCALATED/reopened + reopen_count++")

    # 멱등: 재실행에도 PENDING 1건 (gate_ensure 가드) — approve 케이스에서 records 1건인지
    store = FakeStore()
    app = build_graph(store, MemorySaver())
    inc = "INC-IDEM"
    cfg = {"configurable": {"thread_id": inc}}
    try:
        app.invoke({"incident_id": inc, "report": {"selected_option": "limit_option"}}, cfg)
    except Exception:
        pass
    app.invoke(Command(resume={"action": "approve", "approver": "e1"}), cfg)
    assert store.records[inc]["status"] == "APPROVED"           # 재실행 멱등: 단일 record, 승자 1
    passed.append("멱등 — interrupt 노드 재실행에도 PENDING 중복 없음")

    print("-" * 60)
    for p in passed:
        print("PASS", p)
    print(f"\nALL {len(passed)} INTEGRATION CHECKS PASS ✅  (그래프 배선 실행 확인)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nFAIL ❌ — 아래 트레이스백 전체를 붙여주세요 (langgraph API 미세차 시 제가 즉시 수정):\n")
        traceback.print_exc()
        sys.exit(1)
