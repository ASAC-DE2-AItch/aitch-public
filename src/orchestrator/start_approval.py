# -*- coding: utf-8 -*-
"""승인 그래프 수동 시작 CLI — M2 리허설·데모 운영 도구 (2026-07-24, fix/rehearsal-wiring).

용법:
    python -m src.orchestrator.start_approval INC-20260724-SIMCH3-001 [brief.json]

동작: build_graph 후 start_incident 1회 호출 → approval_gate의 interrupt에서 멈춤(=PENDING).
  · brief.json 지정 시 그 내용을 Brief로 사용 (경로① 게이트형 stub / 경로② Brief 원문 모두 가능
    — merge_reports가 형태로 분기). C 부재 리허설에서 limit_option+correction_id를 물릴 때 사용.
  · C(agent_service)가 agent_reports에 리포트 3종+SUP를 적재해뒀으면 merge_reports 경로③이
    DB에서 수합해 실 Brief로 PENDING을 연다 (store.conn 주입 — 경로③ 활성화의 필요조건).
  · 적재 전이면 stub 통과(빈 Brief) — 게이트 배선 검증용.
  · INC 실존 가드 (2026-07-26): incidents에 없는 ID면 시작 전에 중단 — 오타 ID로 유령
    PENDING이 생긴 실측 사고(7/24) 재발 방지.
resume(승인/수정/반려)은 gateway(POST /approvals/{id})가 담당 — PostgresSaver 체크포인터를
DB로 공유하므로 시작 프로세스와 재개 프로세스가 달라도 이어진다 (thread_id=incident_id).

정식 자동화(P5-1 완성): C의 '리포트 3종 완료' 이벤트(fdc.agent) 구독 → start_incident 호출.
그 배선 전까지 이 CLI가 그 신호의 수동 대역이다.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv


def main() -> None:
    """CLI 본체 — .env 로드 후 그래프 1회 시작 (헌법 6-1: 접속값은 env)."""
    load_dotenv()
    if len(sys.argv) < 2 or not sys.argv[1].startswith("INC-"):
        raise SystemExit("용법: python -m src.orchestrator.start_approval <INC-...> [brief.json]")
    inc = sys.argv[1]
    report: dict = {}
    if len(sys.argv) >= 3:                # 선택: Brief 파일 주입 (C 부재 리허설·데모 운영)
        import json
        with open(sys.argv[2], encoding="utf-8") as f:
            report = json.load(f)
        print(f"Brief 파일 주입: {sys.argv[2]} (selected_option={report.get('selected_option')!r})")
    dsn = os.environ["DATABASE_URL"]

    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
    from langgraph.checkpoint.postgres import PostgresSaver

    # ── INC 실존 가드 (2026-07-26) ─────────────────────────────────────────
    # 오타 ID로도 그래프가 시작돼 유령 PENDING이 생긴 실측 사고(7/24, INC-…-XXX) 방지.
    # 체크포인터·커넥션 풀을 만들기 전에 incidents 실존을 확인하고, 없으면 명시적 중단.
    _chk = psycopg2.connect(dsn)
    try:
        with _chk.cursor() as _cur:
            _cur.execute("SELECT 1 FROM incidents WHERE incident_id = %s", (inc,))
            if _cur.fetchone() is None:
                raise SystemExit(
                    f"Incident 미존재: {inc} — incidents 테이블에 없음 (오타 확인). "
                    "그래프를 시작하지 않았다 (유령 PENDING 방지 가드)."
                )
    finally:
        _chk.close()

    from .approval_graph import ApprovalStore, build_graph, emit_event, start_incident
    from .events import build_producer, make_kafka_emit

    emit = emit_event
    producer = None
    try:
        producer = build_producer(os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"))
        emit = make_kafka_emit(producer)
    except Exception as e:  # noqa: BLE001 — Kafka 미가용이어도 배선 검증은 진행
        print(f"Kafka 미가용 — 이벤트 로그 stub 발행: {e}")

    pool = ThreadedConnectionPool(1, 2, dsn)
    conn = psycopg2.connect(dsn)          # merge_reports 경로③(agent_reports 수합)용 읽기 커넥션
    try:
        store = ApprovalStore(pool=pool)
        store.conn = conn                 # 경로③ 활성화 — getattr(store, "conn") 시임 충족
        with PostgresSaver.from_conn_string(dsn) as cp:
            cp.setup()
            app = build_graph(store, cp, emit=emit)
            start_incident(app, inc, report)
            print(f"✅ 그래프 시작 — {inc} → PENDING 대기 (승인: S5 화면 또는 POST /approvals/{inc})")
    finally:
        if producer is not None:
            try:
                producer.flush(5)         # ApprovalRequested 유실 방지 (헌법 6-2 — 종료 전 전달)
            except Exception:  # noqa: BLE001
                pass
        conn.close()
        pool.closeall()


if __name__ == "__main__":
    main()
