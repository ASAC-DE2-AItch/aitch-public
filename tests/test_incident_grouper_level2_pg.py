# -*- coding: utf-8 -*-
"""P4-3 Level 2 — 실 Postgres 그루퍼 통합 검증 (incident_alerts/incidents 실 SQL).

내 샌드박스엔 PG 불가라 미실행. **사용자 env에서 (docker PG up):**

    docker-compose up -d postgres        # (전에 띄운 적 있으면 down -v 후 up — 최신 스키마)
    $env:DATABASE_URL = "postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform"
    python tests/test_incident_grouper_level2_pg.py

→ 출력 붙여주면 진단. 검증: upsert(INSERT incidents/incident_alerts)·JOIN 병합 조회·%s::jsonb·ON CONFLICT 멱등.
  Kafka 불요 — handle_alert 직접 호출.
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
    _bail("[중단] DATABASE_URL 미설정 — 위 주석 참조.")

try:
    import psycopg2
    from orchestrator.incident_grouper import IncidentGrouper
except ImportError as exc:
    _bail(f"[중단] deps 미설치 — {exc}")


def _preflight(conn) -> bool:
    need = {("incident_alerts", "alert_id"), ("incidents", "updated_at")}
    with conn.cursor() as cur:
        cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'")
        have = {(t, c) for t, c in cur.fetchall()}
    missing = sorted(f"{t}.{c}" for (t, c) in need if (t, c) not in have)
    if missing:
        print("[중단] 스키마 구버전 — 없는 컬럼/테이블:", missing)
        print("  → docker-compose down -v ; docker-compose up -d postgres  후 재실행.")
        return False
    return True


def _alert(aid, chamber, ts, sev, ctx):
    return {"alert_id": aid, "chamber_id": chamber, "timestamp": ts,
            "violations": [{"rule_id": "N3", "sensor": "C11", "severity": sev}],
            "context_score": ctx, "suspect_window": {"start_wafer": "C64_1", "end_wafer": "C64_9"}}


def _scalar(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def main():
    conn = psycopg2.connect(DSN)
    if not _preflight(conn):
        conn.close()
        sys.exit(2)

    ch = f"SIM_CH_L2{uuid.uuid4().hex[:3]}"                 # 유니크 챔버로 테스트 격리
    g = IncidentGrouper(conn, merge_gap_sec=300, initial_lifecycle="open", agent_min=31)

    id1 = g.handle_alert(_alert("AL2-1", ch, "2026-07-13T10:05:00.000Z", "WARNING", 60))
    assert id1, "신규 incident 미생성"
    id2 = g.handle_alert(_alert("AL2-2", ch, "2026-07-13T10:07:00.000Z", "CRITICAL", 80))
    assert id2 == id1, f"2분 근접인데 병합 실패 ({id2} != {id1})"
    id3 = g.handle_alert(_alert("AL2-3", ch, "2026-07-13T14:00:00.000Z", "WARNING", 55))
    assert id3 and id3 != id1, "gap(4h) 초과인데 신규 미생성/병합됨"
    dup = g.handle_alert(_alert("AL2-2", ch, "2026-07-13T10:07:30.000Z", "WARNING", 70))
    assert dup is None, "중복 alert_id인데 재처리됨(멱등 실패)"

    n_inc = _scalar(conn, "SELECT count(*) FROM incidents WHERE chamber_id = %s", (ch,))
    n_mem = _scalar(conn, "SELECT count(*) FROM incident_alerts WHERE chamber_id = %s", (ch,))
    sev = _scalar(conn, "SELECT severity_max FROM incidents WHERE incident_id = %s", (id1,))
    assert n_inc == 2, f"incidents {n_inc}건 (기대 2)"
    assert n_mem == 3, f"incident_alerts {n_mem}건 (기대 3: 001에 2 + 002에 1)"
    assert sev == "CRITICAL", f"severity_max {sev} (기대 CRITICAL — 병합 시 상향)"
    print(f"  실 SQL: incidents 2건 · incident_alerts 3건 · {id1} severity=CRITICAL · 중복 멱등 ✓")

    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM incident_alerts WHERE chamber_id = %s", (ch,))
        cur.execute("DELETE FROM incidents WHERE chamber_id = %s", (ch,))
    conn.close()
    print("\nALL P4-3 LEVEL-2 CHECKS PASS ✅  (그루퍼 실 SQL: upsert·JOIN 병합·::jsonb·ON CONFLICT 멱등)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nFAIL ❌ — 아래 트레이스백 전체를 붙여주세요:\n")
        traceback.print_exc()
        sys.exit(1)
