# -*- coding: utf-8 -*-
"""CT2 확인용 프로브 — 이미 END까지 간(resolve된) thread에 Command(resume)이 먹는가?"""
from typing import Any, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

import langgraph
print("langgraph", langgraph.__version__ if hasattr(langgraph, "__version__") else "?")


class S(TypedDict, total=False):
    phase: str
    decision: Any
    gate_hits: int


def gate(s: S) -> dict:
    n = s.get("gate_hits", 0) + 1
    print(f"   [gate 실행] phase={s.get('phase')!r} 누적진입={n}")
    rv = interrupt({"ask": s.get("phase")})
    print(f"   [gate 재개] resume_value={rv!r}")
    return {"decision": rv, "gate_hits": n}


def done(s: S) -> dict:
    print(f"   [done] decision={s.get('decision')!r}")
    return {}


g = StateGraph(S)
g.add_node("gate", gate)
g.add_node("done", done)
g.add_edge(START, "gate")
g.add_edge("gate", "done")
g.add_edge("done", END)
app = g.compile(checkpointer=MemorySaver())

cfg = {"configurable": {"thread_id": "INC-TEST-001"}}

print("\n=== 1) 최초 invoke → interrupt(PENDING) ===")
out = app.invoke({"phase": "R9"}, cfg)
print("   __interrupt__:", out.get("__interrupt__"))
print("   next:", app.get_state(cfg).next)

print("\n=== 2) resume → END 까지 (resolve) ===")
out = app.invoke(Command(resume={"action": "approve"}), cfg)
print("   result:", {k: v for k, v in out.items() if k != "__interrupt__"})
st = app.get_state(cfg)
print("   next:", st.next, "| interrupts:", st.tasks)

print("\n=== 3) resolve된 thread에 Command(resume) 재시도 (=CT2 '연장' 가설) ===")
try:
    out = app.invoke(Command(resume={"action": "approve", "who": "CT2-stale"}), cfg)
    print("   반환:", {k: v for k, v in out.items() if k != "__interrupt__"})
    print("   __interrupt__:", out.get("__interrupt__"))
    print("   next:", app.get_state(cfg).next)
except Exception as e:
    print(f"   예외: {type(e).__name__}: {e}")

print("\n=== 4) 같은 thread에 새 input 으로 재-invoke (순차 승인 가설) ===")
try:
    out = app.invoke({"phase": "CT2"}, cfg)
    print("   __interrupt__:", out.get("__interrupt__"))
    st = app.get_state(cfg)
    print("   next:", st.next, "| state:", {k: v for k, v in st.values.items()})
except Exception as e:
    print(f"   예외: {type(e).__name__}: {e}")

print("\n=== 5) 4)의 interrupt를 resume ===")
try:
    out = app.invoke(Command(resume={"action": "approve", "who": "CT2-real"}), cfg)
    print("   최종 state:", {k: v for k, v in out.items() if k != "__interrupt__"})
except Exception as e:
    print(f"   예외: {type(e).__name__}: {e}")
