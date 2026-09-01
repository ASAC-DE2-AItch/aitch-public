# -*- coding: utf-8 -*-
"""
[초안 · 팀원A 제안 — 2026-07-23] 예측 결과 조회 API (wafer_predictions read-only)

오너 편입 완료 (2026-07-23, PM 리뷰 — #37): main.py 말미에서 include_router 로 편입.
   편입 시 조정 1건 — GET /{wafer_id} → GET /wafer/{wafer_id}. gateway 에는 실시간 라우트
   /predictions/recent·/predictions/stream(P5-4 링버퍼·SSE)이 이미 있어, 파라미터 경로가
   "recent"·"stream"을 wafer_id 로 삼키지 않도록 /wafer/ 아래로 격리 (등록 순서 무관).

설계:
  - 읽기 전용. DB sink(prediction_sink.py)가 적재한 wafer_predictions 를 조회만 한다.
  - gateway 관례 준수: psycopg2 conn 주입(요청당 연결·해제 — 초안. 운영은 풀 권장),
    행→dict 변환은 RealDictCursor(= approvals.py 의 dict(zip(cols,row))와 동치).
  - 엔드포인트:
      GET /predictions                      최근 예측 목록 (chamber_id·wafer_id 필터, 최신순)
      GET /predictions/wafer/{wafer_id}     특정 wafer 최신 예측 1건
      GET /predictions/chambers/summary     챔버별 건수·평균·최신 (멀티챔버 대시보드용)
"""
import os

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, Depends, HTTPException, Query

router = APIRouter(prefix="/predictions", tags=["predictions"])

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform")

_COLS = ("wafer_id, chamber_id, lot_id, recipe_id, predicted_c65, actual_c65, measured_at, "
         "drift_score, anomaly_score, shap_top3, spc_flags, is_qual, model_version, created_at")


def get_conn():
    """요청당 psycopg2 연결(초안). 운영 전환 시 커넥션 풀 권장."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


@router.get("")
def list_predictions(
    chamber_id: str | None = Query(None, description="챔버 필터 (예: SIM_CH_1)"),
    wafer_id: str | None = Query(None, description="wafer 필터"),
    limit: int = Query(50, ge=1, le=500, description="최대 건수"),
    conn=Depends(get_conn),
):
    """최근 예측 목록 (최신순). chamber_id·wafer_id 로 필터."""
    clauses, params = [], []
    if chamber_id:
        clauses.append("chamber_id = %s")
        params.append(chamber_id)
    if wafer_id:
        clauses.append("wafer_id = %s")
        params.append(wafer_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {_COLS} FROM wafer_predictions {where} ORDER BY id DESC LIMIT %s", params)
        return cur.fetchall()


@router.get("/chambers/summary")
def chambers_summary(conn=Depends(get_conn)):
    """챔버별 예측 건수·평균 C65·최신 시각 — 4챔버 동시 처리 확인/대시보드용."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT chamber_id,
                      count(*)                       AS n_predictions,
                      round(avg(predicted_c65)::numeric, 2) AS avg_predicted_c65,
                      max(created_at)                AS latest_at
                 FROM wafer_predictions
                GROUP BY chamber_id
                ORDER BY chamber_id""")
        return cur.fetchall()


@router.get("/wafer/{wafer_id}")
def get_prediction(wafer_id: str, conn=Depends(get_conn)):
    """특정 wafer 의 최신 예측 1건."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {_COLS} FROM wafer_predictions WHERE wafer_id = %s ORDER BY id DESC LIMIT 1",
                    (wafer_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"예측 없음: {wafer_id}")
    return row
