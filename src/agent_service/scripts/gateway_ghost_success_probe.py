# -*- coding: utf-8 -*-
"""gateway 유령 성공 프로브 — 승인이 끝난 incident 에 승인을 다시 보내면 200 이 온다.

## 이 버그의 증상은 "에러가 나는 것"이 아니라 **"에러가 안 나는 것"** 이다

`POST /approvals/{incident_id}` 라우트(`src/gateway/main.py:177`)에는 **409 가드가 이미 있다**
(M2 배선 `4d31a75`):

    try:
        resume_decision(_gate["app"], incident_id, payload)
    except Exception as e:
        raise HTTPException(409, detail=f"재개할 PENDING 없음 — ...: {incident_id}")
    return {"incident_id": ..., "action": ..., "status": "resumed"}      # ← 200

가드의 전제는 *"재개할 PENDING 이 없으면 `resume_decision` 이 예외를 던진다"* 다.
그런데 LangGraph 는 **재개 지점이 없으면 예외 없이 조용히 통과**한다. 그러면 except 를 지나쳐
아래 `return` 으로 떨어져 200 이 나가고, DB 에는 아무것도 기록되지 않는다.

## 두 케이스를 대조한다 — 경계가 어디인가

    A) 그래프 미시작        → 상태가 비어 KeyError → 가드가 잡는다 (409)   ← 대조군
    B) 승인 완료로 닫힌 thread → 상태가 온전해 예외 없음 → 200 유령 성공    ← 문제

B 가 실사용 경로다: S6 화면이 5초 폴링으로 목록을 갱신하는데 선택(`sel`)이 남아, 이미 확정된
incident 에 승인을 다시 보내면 그 thread 는 닫힌 상태다 (PR #67 소비자 리뷰 ⓐ).

## 증거는 "대조"로 만든다 (에러 메시지가 아니라)

B 에서 **`reject` / `approver=SECOND` 를 보내고 `approve` / `approver=FIRST` 가 돌아온다.**
보낸 것과 다른 것이 오면 요청이 처리되지 않은 것이다. 기록 수도 늘지 않는다.

## 수정 방향의 근거

A·B 모두 `get_state(cfg).next == ()` 다 — *"다음에 실행할 노드가 없다"*. 예외가 아니라 이
상태를 보면 **한 규칙으로 두 경우가 다 잡힌다.**

## 범위 (정직하게)

`fastapi` 미설치 환경이라 HTTP 계층은 태우지 않는다. 대신 라우트가 실제로 호출하는
`approvals.validate_decision` → `approvals.resume_decision` 을 그대로 태운다. 그 사이·이후에
다른 분기가 없으므로(위 코드 전문) 예외가 없으면 200 은 결정적이다.

업무 테이블은 건드리지 않는다 — 케이스 B 는 `tests/test_approval_graph.py` 의 `FakeStore` 를
주입하므로 `approval_records` 는 변하지 않는다(interrupt/resume 의미는 실물). 체크포인터
테이블에 프로브 thread 가 남는다.

## 실행

    $env:DATABASE_URL="postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform"
    python gateway_ghost_success_probe.py

관련: `ct2_resume_probe.py` — LangGraph 레벨 5단계(닫힌 thread resume 이 no-op·재-invoke 만 가능)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_cands = []
if os.environ.get("AITCH_REPO"):
    _cands.append(Path(os.environ["AITCH_REPO"]))
_cands += [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
for _p in _cands:
    if (_p / "src" / "orchestrator" / "approval_graph.py").is_file():
        REPO_ROOT = _p
        break
else:
    raise SystemExit("repo 루트를 못 찾았다 — repo 안에서 실행하거나 AITCH_REPO 를 지정할 것")

sys.path.insert(0, str(REPO_ROOT))                 # src.* 상대 임포트(..orchestrator)용
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))       # FakeStore 재사용

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform"
)
NEVER = "INC-PROBE-NEVER-STARTED"                  # 케이스 A — 한 번도 invoke 하지 않음
CLOSED = "INC-PROBE-CLOSED"                        # 케이스 B — 승인 완료로 닫힘

FIRST = {"action": "approve", "approver": "FIRST", "approver_role": None, "reason": None}
SECOND = {"action": "reject", "approver": "SECOND", "approver_role": None,
          "reason": "구별용", "reanalyze": True}   # 1차와 **다른 값** — 무시 여부를 보려고


def count_records(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM approval_records")
        return cur.fetchone()[0]


def main() -> int:
    import psycopg2
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.types import Command

    # 라우트(main.py)가 쓰는 것과 **같은 모듈**
    from src.gateway.approvals import resume_decision, validate_decision
    from src.orchestrator.approval_graph import build_graph
    from test_approval_graph import FakeStore

    print(f"DSN = {DSN}")
    conn = psycopg2.connect(DSN)
    before = count_records(conn)
    print(f"[전] approval_records {before}행\n")

    print("=== 0) validate_decision — 라우트 1단계 ===")
    print(f"   통과: {validate_decision(dict(SECOND))}")
    print("   ※ body(action·approver) 만 검사한다 — PENDING 존재 여부는 보지 않는다\n")

    raised: dict = {}

    # ── A) 대조군: 그래프 미시작 ────────────────────────────────────────────────
    print("=" * 74)
    print("A) 그래프 미시작 incident — 가드가 잡는가?")
    print("=" * 74)
    with PostgresSaver.from_conn_string(DSN) as cp:
        cp.setup()
        app = build_graph(FakeStore(), cp)
        try:
            resume_decision(app, NEVER, validate_decision(dict(SECOND)))
            raised["A"] = None
            print("   예외 없음 → 라우트 200")
        except Exception as e:                                   # noqa: BLE001
            raised["A"] = e
            print(f"   {type(e).__name__}: {e}")
            print("   → 라우트 except 가 잡아 409 (main.py:189). **가드 작동**")

    # ── B) 문제: 승인 완료로 닫힌 thread ────────────────────────────────────────
    print("\n" + "=" * 74)
    print("B) 승인이 끝난 incident 에 승인을 다시 보낸다 (S6 stale 선택 경로)")
    print("=" * 74)
    fake = FakeStore()
    with PostgresSaver.from_conn_string(DSN) as cp:
        app = build_graph(fake, cp)
        cfg = {"configurable": {"thread_id": CLOSED}}

        app.invoke({"incident_id": CLOSED, "chamber_id": "SIM_CH_3",
                    "selected_option": "limit_option"}, cfg)
        app.invoke(Command(resume=validate_decision(dict(FIRST))), cfg)
        print(f"   1차 승인 완료 — 기록 {len(fake.records)}건 · next={app.get_state(cfg).next!r} (빈 tuple = 닫힘)")

        print(f"\n   ▶ 2차 전송 (라우트와 같은 경로): action={SECOND['action']!r} "
              f"approver={SECOND['approver']!r}")
        t0 = time.perf_counter()
        try:
            out = resume_decision(app, CLOSED, validate_decision(dict(SECOND)))
            raised["B"] = None
            dt = (time.perf_counter() - t0) * 1000
            print(f"   예외 없음 → 라우트 **200**  (소요 {dt:.1f}ms — 타임아웃이 아니라 즉시 반환)")
            print(f"   받은 값: action={out.get('decision')!r} approver={out.get('approver')!r}")
            ignored = (out.get("approver") != SECOND["approver"]
                       or out.get("decision") != SECOND["action"])
            print(f"   → 보낸 것과 {'다르다 — 요청이 무시됐다 (과거 상태를 그대로 반환)' if ignored else '같다'}")
        except Exception as e:                                   # noqa: BLE001
            raised["B"] = e
            print(f"   {type(e).__name__}: {e} → 409")
        print(f"   기록 {len(fake.records)}건 (안 늘면 무기입)")

    # ── 수정 방향의 근거 ────────────────────────────────────────────────────────
    # ⚠️ thread 상태가 **세 가지**다. A 는 위에서 resume 을 시도해 그래프가 중간에 크래시했으므로
    #    "재시도 대기"(next 에 노드가 남음) 상태다 — 손대지 않은 thread 와 구분해야 한다.
    print("\n" + "=" * 74)
    print("수정 방향 — 예외가 아니라 get_state().next 로 판정")
    print("=" * 74)
    with PostgresSaver.from_conn_string(DSN) as cp:
        app = build_graph(FakeStore(), cp)
        rows = [
            (NEVER + "-UNTOUCHED", "완전 미접촉", "요청 대상이 오타·미시작"),
            (NEVER, "A 크래시 후", "resume 시도가 중간에 죽어 재시도 지점이 남음"),
            (CLOSED, "B 닫힘", "승인이 끝나 재개 지점이 없음 ← 문제"),
        ]
        for tid, label, note in rows:
            st = app.get_state({"configurable": {"thread_id": tid}})
            n = len(st.values) if st.values else 0
            verdict = "409 로 거름" if not st.next else "재개 시도 (정당)"
            print(f"   {label:12} next={str(st.next):24} values={n}개  → {verdict}")
            print(f"   {'':12} {note}")
    print("\n   ⇒ `if not st.next: 409` 한 줄로 **미접촉과 닫힘(B)** 이 잡힌다.")
    print("      크래시 후 상태는 재개 지점이 실제로 남아 있어 통과시키는 것이 맞고,")
    print("      그 경로의 실패는 기존 except → 409 가 계속 담당한다(이중 방어).")

    after = count_records(conn)
    print(f"\n[후] approval_records {after}행 (업무 테이블 무변화 기대)")
    conn.close()

    print("\n" + "=" * 74)
    print("결론")
    print("=" * 74)
    for k, label in (("A", "그래프 미시작"), ("B", "승인 완료로 닫힘")):
        e = raised.get(k)
        mark = "✅ 409 정상" if e is not None else "🔴 200 유령 성공"
        detail = f"({type(e).__name__})" if e is not None else "(DB·기록 무변화)"
        print(f"  {k}) {label:16} → {mark} {detail}")
    if raised.get("B") is None:
        print("\n  가드의 전제 *'PENDING 없으면 예외가 난다'* 가 닫힌 thread 에서 거짓이다.")
        print("  UI 에는 성공으로 보이는데 승인은 기록되지 않는다 — 감사 추적(헌법 1-1)이 끊긴다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
