# -*- coding: utf-8 -*-
"""P4-1 A2 — 승인 API 라이브 테스트 (FastAPI TestClient, 실 env).

내 샌드박스엔 fastapi/PG 불가라 미실행. **사용자 env에서 (docker PG + deps + DATABASE_URL):**

    pip install "fastapi[standard]"      # TestClient(httpx) 포함
    $env:DATABASE_URL = "postgresql://fdc_admin:change-me@localhost:5432/fdc_platform"
    python tests/test_api_live.py

→ 출력 붙여주면 진단. 검증: /health · seed→트리거→/approvals/pending 노출 → POST approve → DB 반영 ·
  reject 사유 누락 400.
"""

import os
import sys
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _bail(msg: str):
    """전제 미충족 시 중단 — 스크립트면 종료, **pytest 수집 중이면 모듈 스킵**.

    ⚠️ 모듈 레벨 `sys.exit()` 는 pytest 수집 단계에서 SystemExit 로 새어나가 자기 파일만
    실패시키는 게 아니라 **세션 전체를 INTERNALERROR 로 죽인다**. 그러면 팀 누구도
    `pytest tests/` 를 못 돌린다 (CI 가 파일 단위라 한동안 안 드러났다 — 2026-08-07).
    """
    print(msg)
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(msg, allow_module_level=True)
    sys.exit(1)


if not os.environ.get("DATABASE_URL"):
    _bail("[중단] DATABASE_URL 미설정 — 위 주석 참조.")

try:
    from fastapi.testclient import TestClient
    from gateway.main import _gate, app
    from orchestrator.approval_graph import start_incident
except ImportError as exc:
    print('       pip install "fastapi[standard]" langgraph langgraph-checkpoint-postgres "psycopg[binary]"')
    _bail(f"[중단] deps 미설치 — {exc}")


def main():
    with TestClient(app) as client:                          # startup(lifespan) → _gate 배선
        assert client.get("/health").status_code == 200
        if _gate.get("app") is None:
            print("[중단] 승인 게이트 미배선 — DATABASE_URL/langgraph/PG 확인.")
            sys.exit(2)
        conn = _gate["conn"]
        inc = f"INC-API-{uuid.uuid4().hex[:6]}"
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO incidents (incident_id, chamber_id, lifecycle) "
                        "VALUES (%s, 'SIM_CH_3', 'open') ON CONFLICT (incident_id) DO NOTHING", (inc,))

        start_incident(_gate["app"], inc, {"selected_option": "limit_option", "request_type": "correction"})

        pend = client.get("/approvals/pending").json()["pending"]
        assert any(p["incident_id"] == inc for p in pend), f"/approvals/pending에 {inc} 없음"
        print(f"  GET /approvals/pending: {inc} 노출 ✓")

        r = client.post(f"/approvals/{inc}", json={"action": "approve", "approver": "api-eng"})
        assert r.status_code == 200, (r.status_code, r.text)
        print(f"  POST /approvals/{inc} approve → {r.json()} ✓")

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM approval_records WHERE incident_id = %s "
                        "ORDER BY id DESC LIMIT 1", (inc,))
            assert cur.fetchone()[0] == "APPROVED"
            cur.execute("SELECT lifecycle FROM incidents WHERE incident_id = %s", (inc,))
            assert cur.fetchone()[0] == "actioned"
        print("  DB: approval_records=APPROVED, incidents.lifecycle=actioned ✓")

        r2 = client.post(f"/approvals/{inc}", json={"action": "reject", "approver": "e"})   # reason 누락
        assert r2.status_code == 400, (r2.status_code, r2.text)
        print("  POST reject(사유 누락) → 400 ✓")

        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM approval_records WHERE incident_id = %s", (inc,))
            cur.execute("DELETE FROM incidents WHERE incident_id = %s", (inc,))

    print("\nALL API-LIVE CHECKS PASS ✅  (FastAPI 라우트 + resume + fetch_pending 실동작)")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        print("\nFAIL ❌ — 아래 트레이스백 붙여주세요:\n")
        traceback.print_exc()
        sys.exit(1)
