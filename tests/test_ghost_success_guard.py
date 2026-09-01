# -*- coding: utf-8 -*-
"""승인 게이트 유령 성공 회귀 — 닫힌 thread 재승인이 409로 거절되는가 (TSR-0002).

배경: `POST /approvals/{incident_id}` 의 가드가 **예외에만** 의존했다. LangGraph 는 재개
지점이 없는 thread 에 `Command(resume)` 을 보내도 예외를 내지 않고 조용히 통과하며 과거
상태를 그대로 돌려준다 — 그래서 이미 결정된 Incident 에 두 번째 결정을 보내면 200 이
나가고 `approval_records` 는 그대로였다. 엔지니어는 반려했다고 믿는데 감사 추적엔 없다
(헌법 1-1 훼손). C 진단 프로브가 재현 — PR #71, 2026-07-30.

⚠️ 이 버그는 **"예외가 안 나는 것"이 증상**이라 CI 가 지키지 않으면 조용히 재발한다.
   그래서 아래 A-2 는 "두 번째 resume 이 예외 없이 통과한다"는 **버그의 전제 자체**를
   단언한다 — LangGraph 가 이 동작을 바꾸면 이 테스트가 먼저 깨져 재검토 신호가 된다.

실행: pytest tests/test_ghost_success_guard.py
   CI(.github/workflows/tests.yml)는 langgraph·fastapi·psycopg2 를 설치해 8건 전부 돌린다 —
   의존성이 없으면 이 파일은 조용히 skip 되고, 그러면 위 문단의 재검토 신호가 울리지 않는다
   (2026-08-02, #76 C 리뷰). 로컬에 그 의존이 없으면 해당 절만 skip 된다.
"""

import sys
from pathlib import Path
from typing import TypedDict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo 루트 (gateway 상대 임포트 정합)
from src.gateway.approvals import has_resume_point              # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── A. has_resume_point 의미 — 실 LangGraph + MemorySaver (실 DB 불요) ──────────

class _ProbeState(TypedDict, total=False):
    """프로브 그래프 상태 — **채널을 명시해야** 노드 반환이 서로를 덮지 않는다.

    `StateGraph(dict)` 로 두면 마지막 노드 반환(`{"closed": True}`)이 상태 전체를 대체해
    `action`/`approver` 키가 사라지고, A-2 의 "2차 결정이 반영되지 않았다" 단언이 무엇을
    보내든 통과하는 죽은 단언이 된다 (2026-08-02 C 리뷰 지적).
    """
    action: str
    approver: str
    closed: bool


def _build_probe_graph():
    """interrupt 1회로 멈추는 최소 그래프 — 승인 게이트와 같은 형태(gate→done)."""
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt
    from langgraph.checkpoint.memory import MemorySaver

    def gate(s: _ProbeState) -> dict:
        rv = interrupt({"ask": "decide"}) or {}
        return {"action": rv.get("action"), "approver": rv.get("approver")}

    def done(s: _ProbeState) -> dict:
        return {"closed": True}

    g = StateGraph(_ProbeState)
    g.add_node("gate", gate)
    g.add_node("done", done)
    g.add_edge(START, "gate")
    g.add_edge("gate", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=MemorySaver())


@pytest.fixture()
def probe_graph():
    pytest.importorskip("langgraph")
    return _build_probe_graph()


def _cfg(tid):
    return {"configurable": {"thread_id": tid}}


def test_untouched_thread_has_no_resume_point(probe_graph):
    """미접촉 thread — 재개 지점 없음 (구 가드는 우연히 KeyError 로 걸렸던 케이스 A)."""
    assert has_resume_point(probe_graph, "INC-NEVER-STARTED") is False


def test_interrupted_thread_has_resume_point(probe_graph):
    """interrupt 대기 중 — 재개가 정당하므로 통과시켜야 한다."""
    probe_graph.invoke({}, _cfg("INC-OPEN"))                    # gate 에서 멈춤 = PENDING
    assert has_resume_point(probe_graph, "INC-OPEN") is True


def test_closed_thread_has_no_resume_point(probe_graph):
    """🔴 케이스 B — 승인 완료로 닫힌 thread. 상태는 온전하지만 재개 지점이 없다."""
    from langgraph.types import Command
    tid = "INC-CLOSED"
    probe_graph.invoke({}, _cfg(tid))
    probe_graph.invoke(Command(resume={"action": "approve", "approver": "FIRST"}), _cfg(tid))
    assert has_resume_point(probe_graph, tid) is False          # 가드가 잡아야 하는 자리


def test_closed_thread_resume_passes_silently(probe_graph):
    """🔴 버그의 전제 — 닫힌 thread 재개는 **예외 없이** 통과하고 결정이 반영되지 않는다.

    실측(C 프로브)에서는 reject/SECOND 를 보냈는데 approve/FIRST 가 돌아왔다. 응답 모양·
    소요시간이 정상 승인과 구별되지 않아 DB 를 직접 보지 않으면 실패를 알 수 없다 —
    그래서 예외 기반 가드로는 못 잡고 상태 기반 판정이 필요하다.

    단언은 실측 그대로 건다: 예외가 안 난다 · **2차(reject/SECOND)가 아니라 1차
    (approve/FIRST)가 되돌아온다** · thread 는 여전히 닫혀 있다. 이 단언이 서려면 상태가
    채널별로 남아야 하므로 스키마는 `_ProbeState` 다 — `StateGraph(dict)` 로 두면 `action`
    키 자체가 사라져 `!= "reject"` 가 **무엇을 보내든 통과**한다(위 `_ProbeState` 주석).
    """
    from langgraph.types import Command
    tid = "INC-GHOST"
    probe_graph.invoke({}, _cfg(tid))
    probe_graph.invoke(Command(resume={"action": "approve", "approver": "FIRST"}), _cfg(tid))
    out = probe_graph.invoke(                                   # 2차 — 예외가 나지 않는다
        Command(resume={"action": "reject", "approver": "SECOND"}), _cfg(tid))
    assert out.get("action") == "approve", f"닫힌 thread 에 2차 결정이 반영됐다 — 전제 재검토: {out!r}"
    assert out.get("approver") == "FIRST", f"닫힌 thread 에 2차 결정이 반영됐다 — 전제 재검토: {out!r}"
    assert has_resume_point(probe_graph, tid) is False           # 여전히 닫힘


def test_state_query_failure_is_fail_open():
    """조회 자체가 실패하면 None — 가드가 정상 승인을 막지 않도록 판정을 보류한다."""
    class _Broken:
        def get_state(self, cfg):
            raise RuntimeError("checkpointer down")
    assert has_resume_point(_Broken(), "INC-1") is None


# ── B. 라우트 가드 ① — 열린 PENDING 이 없으면 409 (langgraph 불요) ─────────────

class _FakeCur:
    """approval_records PENDING 조회만 흉내 — rows 가 비면 '열린 요청 없음'."""

    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCur(self._row)

    def commit(self):
        pass                                                    # WP-B5: 라우트가 SELECT 뒤 commit 한다

    def rollback(self):
        pass


@pytest.fixture()
def client(monkeypatch):
    """(TestClient, inject) — `inject(app=..., conn=...)` 로 `main._gate` 를 갈아끼운다.

    `_gate.update(...)` 를 직접 부르면 모듈 전역이 오염된 채 남아 다음 테스트·다음 파일로
    샌다(teardown 없음). `monkeypatch.setitem` 은 원래 값을 테스트 끝에 되돌린다.
    psycopg2 는 fastapi 와 함께 필요하다 — main.py 가 predictions_router 를 모듈 최상단에서
    import 하므로 없으면 skip 이 아니라 수집 에러가 난다.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("psycopg2")
    from fastapi.testclient import TestClient
    from src.gateway import main as gw

    def inject(**kw):
        for k, v in kw.items():
            monkeypatch.setitem(gw._gate, k, v)

    return TestClient(gw.app), inject


def test_route_rejects_when_no_pending_row(client):
    """🔴 회귀 본체 — PENDING 행이 없으면 그래프를 건드리지 않고 409."""
    c, inject = client
    called = {"n": 0}

    class _NeverResume:
        def invoke(self, *a, **k):
            called["n"] += 1                                    # 여기 오면 가드 실패
            return {}

        def get_state(self, cfg):
            raise AssertionError("DB 가드에서 이미 걸러졌어야 한다")

    inject(app=_NeverResume(), conn=_FakeConn(None))
    r = c.post("/approvals/INC-CLOSED",
               json={"action": "reject", "approver": "SECOND", "reason": "중복"})
    assert r.status_code == 409, r.text
    assert called["n"] == 0                                     # resume 시도 자체가 없어야 한다


def test_route_allows_when_pending_exists(client):
    """열린 PENDING 이 있고 재개 지점도 있으면 통과 — 가드가 정상 경로를 막지 않는다."""
    c, inject = client
    seen = {}

    class _Resumable:
        def invoke(self, cmd, cfg):
            seen["cfg"] = cfg
            return {}

        def get_state(self, cfg):
            return type("S", (), {"next": ("merge_reports",)})()

    inject(app=_Resumable(), conn=_FakeConn(("correction",)))
    r = c.post("/approvals/INC-OPEN",
               json={"action": "approve", "approver": "eng1"})
    assert r.status_code == 200, r.text
    assert seen["cfg"]["configurable"]["thread_id"] == "INC-OPEN"


def test_route_rejects_closed_thread_despite_pending_row(client):
    """가드 ② — DB 는 PENDING 인데 thread 가 닫힌 불일치도 409 (이중 방어)."""
    c, inject = client
    called = {"n": 0}

    class _ClosedThread:
        def invoke(self, *a, **k):
            called["n"] += 1
            return {}

        def get_state(self, cfg):
            return type("S", (), {"next": ()})()                # 닫힘

    inject(app=_ClosedThread(), conn=_FakeConn(("correction",)))
    r = c.post("/approvals/INC-STALE",
               json={"action": "reject", "approver": "SECOND", "reason": "중복"})
    assert r.status_code == 409, r.text
    assert called["n"] == 0


if __name__ == "__main__":                                      # 수동 실행
    raise SystemExit(pytest.main([__file__, "-q"]))
