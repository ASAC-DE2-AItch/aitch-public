# -*- coding: utf-8 -*-
"""P4-1 Level 2 — 실 Postgres + PostgresSaver 통합 검증 (그래프 실행 + 실 SQL).

내 샌드박스에선 langgraph/PG 불가라 미실행. **사용자 env에서:**

    docker-compose up -d postgres        # init.sql로 테이블 자동 생성
    # ⚠ PG를 전에 띄운 적 있으면 볼륨에 구버전 스키마가 남아 init.sql이 재실행 안 됨 →
    #   docker-compose down -v ; docker-compose up -d postgres  (볼륨 초기화; dev DB라 안전)
    pip install langgraph langgraph-checkpoint-postgres "psycopg[binary]"
    $env:DATABASE_URL = "postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform"
    python tests/test_approval_graph_level2_pg.py

→ 출력 전체 붙여주면 진단·수정.
"""

import os
import sys
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


DSN = os.environ.get("DATABASE_URL")
if not DSN:
    _bail("[중단] DATABASE_URL 미설정 — 위 주석의 env 설정 후 재실행.")

try:
    import psycopg2
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.types import Command
    from orchestrator.approval_graph import ApprovalStore, build_graph
except ImportError as exc:
    print('       pip install langgraph langgraph-checkpoint-postgres "psycopg[binary]"')
    _bail(f"[중단] deps 미설치 — {exc}")


def _preflight(conn) -> bool:
    """스키마 최신 여부 — 구버전 볼륨(init.sql 미재실행) 감지."""
    need = {("incidents", "updated_at"), ("incidents", "reopen_count"),
            ("approval_records", "status"), ("incident_alerts", "alert_id")}
    with conn.cursor() as cur:
        cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'")
        have = {(t, c) for t, c in cur.fetchall()}
    missing = sorted(f"{t}.{c}" for (t, c) in need if (t, c) not in have)
    if missing:
        print("[중단] 스키마 구버전 — 없는 컬럼/테이블:", missing)
        print("  → 볼륨이 예전에 생성돼 init.sql이 재실행 안 됨.")
        print("  → docker-compose down -v ; docker-compose up -d postgres  후 재실행 (dev DB라 안전).")
        return False
    return True


def _seed_incident(conn, inc, chamber="SIM_CH_3"):
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO incidents (incident_id, chamber_id, lifecycle) "
                    "VALUES (%s, %s, 'open') ON CONFLICT (incident_id) DO NOTHING", (inc, chamber))


def _lifecycle(conn, inc):
    with conn.cursor() as cur:
        cur.execute("SELECT lifecycle FROM incidents WHERE incident_id = %s", (inc,))
        row = cur.fetchone()
        return row[0] if row else None


def _latest_status(conn, inc):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM approval_records WHERE incident_id = %s "
                    "ORDER BY id DESC LIMIT 1", (inc,))
        row = cur.fetchone()
        return row[0] if row else None


def _cleanup(conn, inc):
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM approval_records WHERE incident_id = %s", (inc,))
        cur.execute("DELETE FROM incidents WHERE incident_id = %s", (inc,))


def main():
    conn = psycopg2.connect(DSN)
    if not _preflight(conn):
        conn.close()
        sys.exit(2)

    inc = f"INC-L2-{uuid.uuid4().hex[:6]}"
    _seed_incident(conn, inc)
    store = ApprovalStore(conn)

    with PostgresSaver.from_conn_string(DSN) as cp:      # ★ 올바른 lifecycle (build_graph 리뷰 교정)
        cp.setup()                                       # 체크포인터 테이블 최초 1회
        app = build_graph(store, cp)
        cfg = {"configurable": {"thread_id": inc}}

        try:
            app.invoke({"incident_id": inc,
                        "report": {"selected_option": "limit_option", "request_type": "correction"}}, cfg)
        except Exception as e:                           # interrupt면 정상, 아니면 이게 원인
            print(f"  [first invoke] {type(e).__name__}: {e}")

        st = _latest_status(conn, inc)
        assert st == "PENDING", f"approval_records PENDING 미기록 (status={st}) — 위 [first invoke] 로그 확인"
        assert _lifecycle(conn, inc) == "pending", f"incidents.lifecycle != pending ({_lifecycle(conn, inc)})"
        print(f"  interrupt 후: approval_records=PENDING, incidents.lifecycle=pending ✓ (inc={inc})")

        app.invoke(Command(resume={"action": "approve", "approver": "eng-L2", "approver_role": "process"}), cfg)

        assert _latest_status(conn, inc) == "APPROVED", f"status != APPROVED ({_latest_status(conn, inc)})"
        assert _lifecycle(conn, inc) == "actioned", f"lifecycle != actioned ({_lifecycle(conn, inc)})"
        print("  resume(approve) 후: approval_records=APPROVED, incidents.lifecycle=actioned ✓")

    _cleanup(conn, inc)
    conn.close()
    print("\nALL LEVEL-2 CHECKS PASS ✅  (실 Postgres SQL + PostgresSaver 체크포인터 확인)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nFAIL ❌ — 아래 트레이스백 전체를 붙여주세요 (실 SQL/체크포인터 이슈 즉시 수정):\n")
        traceback.print_exc()
        sys.exit(1)
