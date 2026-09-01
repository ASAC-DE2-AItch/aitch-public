"""B5-2b: Qual 판정·적재 라이브 배선의 주입식 조각 (DB writer + 순수 취합/제안).

`spc_consumer`가 Qual 5장을 모아 `evaluate_qual`로 σ-갭·Nelson을 계산한 뒤, 여기 조각들이
A 지표(AE·C65)를 취합하고(`_combine_a_indicators`) proposed_verdict를 제안하며
(`_propose_verdict`) `quals`에 적재/확정(`insert_qual`·`update_qual_confirmed`)한다.
조용 확정 시 스냅샷 center-only 리베이스(`rebase_snapshot_center`, 결정9)까지 담당한다.

writer 3종(insert/update/rebase)은 **commit을 안 한다** — 트랜잭션 경계는 호출자 소유
(`recalc_writer`·`seed_qual_snapshots` 패턴). 순수 조각 2종은 DB 무접촉이라 유닛테스트 용이.
신규 config·스키마 0 (기존 `qual` 절 + `quals` 컬럼 소비). 스펙: specs/B5-2b_*.md.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.agent_b_spc.qual_engine import FAIL, PASS, UNKNOWN
from src.agent_b_spc.qual_snapshot_loader import load_active_snapshot
from src.agent_b_spc.seed_qual_snapshots import gk_str, shift_snapshot_row, write_sensor_stats

logger = logging.getLogger(__name__)


# ── 순수 조각 (DB 무접촉) ────────────────────────────────────────────
def _combine_a_indicators(preds, pass_c65_max):
    """A read(wafer_predictions) 행들 → (ae_anomaly_mean, c65_within_normal). 순수.

    NULL 규칙(결정5·§5): 예측 0장이면 둘 다 None. `ae_anomaly_mean`은 anomaly_score가
    있는 장만 평균(전부 없으면 None). `c65_within_normal`은 전 장 predicted_c65 ≤
    pass_c65_max(값 있는 장 기준, 하나도 없으면 None). B는 A 컬럼을 **읽기만** 한다(소유 경계).
    """
    preds = list(preds)
    if not preds:
        return None, None
    ae_vals = [p.get("anomaly_score") for p in preds if p.get("anomaly_score") is not None]
    ae_mean = (sum(ae_vals) / len(ae_vals)) if ae_vals else None
    c65_vals = [p.get("predicted_c65") for p in preds if p.get("predicted_c65") is not None]
    c65_ok = all(v <= pass_c65_max for v in c65_vals) if c65_vals else None
    return ae_mean, c65_ok


def _propose_verdict(ind, ae_mean, pass_ae_max, use_ae=True):
    """proposed_verdict 제안 — σ-갭 (+ AE) 트리거(결정6·§11 #1). 순수.

    FAIL = σ-갭 FAIL **or** (AE 사용 시 ae_mean > pass_ae_max) / PASS = σ-갭 PASS **and**
    (AE 통과 or None) / 그 외(σ-갭 UNKNOWN 등)는 None(판정 유보). **Nelson은 무영향**
    (트리거에서 제외·quals에 기록만). 명시적 FAIL 조건이 유보(None)보다 우선한다 —
    AE가 명백히 이상하면(sigma UNKNOWN이라도) 안전측 FAIL 제안(미탐 회피). 반환은 제안
    어휘(PASS/FAIL/None)이며 확정 어휘(loud/quiet)와 다르다.

    `use_ae`(config `qual.use_ae_axis`, PM 회신 2026-08-06): False면 **AE 축 제외** —
    AE가 PM 직후 100% 포화(1.0>max)라 전건 FAIL로 수렴하는 문제. AE는 "레짐 바뀜"을
    말하고 Qual은 "새 레짐 건전성"을 묻는 절차라 질문이 다르다(같은 사유로 RTD도 AE arm 제외).
    False면 σ-갭만 트리거(AE는 호출자가 ae_anomaly_mean에 기록만). 재학습 후 True 복원.
    함수 기본값은 True(기존 진리표 보존) — 런타임 제어는 config가 한다.
    """
    sigma_verdict = ind.sigma_gap.get("verdict")
    ae_fail = use_ae and ae_mean is not None and ae_mean > pass_ae_max
    if sigma_verdict == FAIL or ae_fail:
        return FAIL
    ae_ok = (not use_ae) or ae_mean is None or ae_mean <= pass_ae_max
    if sigma_verdict == PASS and ae_ok:
        return PASS
    return None                                          # σ-갭 UNKNOWN(AE 무이상/제외) → 유보


# ── DB writer (commit=호출자) ────────────────────────────────────────
_INSERT_QUAL = text(
    """
    INSERT INTO quals
      (qual_id, chamber_id, wafer_ids, ae_anomaly_mean, tttm_gap_pct, nelson_violations,
       c65_within_normal, thresholds, proposed_verdict)
    VALUES
      (:qual_id, :chamber_id, CAST(:wafer_ids AS JSONB), :ae_anomaly_mean, :tttm_gap_pct,
       :nelson_violations, :c65_within_normal, CAST(:thresholds AS JSONB), :proposed_verdict)
    ON CONFLICT (qual_id) DO NOTHING
    """
)


def insert_qual(conn, row: dict) -> None:
    """`quals` 1행 적재 — `ON CONFLICT (qual_id) DO NOTHING`(결정7 멱등). commit=호출자.

    `approval_status`는 DDL DEFAULT 'PENDING'에 위임(적재 시 명시 안 함). `wafer_ids`·
    `thresholds`는 JSONB라 json.dumps 후 CAST. 재전달·재시작 재수집은 같은 qual_id로 수렴
    (pm_count=SEQ)해 DO NOTHING이 중복 재적재를 막는다.
    """
    conn.execute(_INSERT_QUAL, {
        "qual_id": row["qual_id"],
        "chamber_id": row["chamber_id"],
        "wafer_ids": json.dumps(row["wafer_ids"]),
        "ae_anomaly_mean": row.get("ae_anomaly_mean"),
        "tttm_gap_pct": row.get("tttm_gap_pct"),
        "nelson_violations": row.get("nelson_violations"),
        "c65_within_normal": row.get("c65_within_normal"),
        "thresholds": json.dumps(row.get("thresholds")),
        "proposed_verdict": row.get("proposed_verdict"),
    })


_UPDATE_CONFIRMED = text(
    """
    UPDATE quals
    SET confirmed_verdict = :verdict,
        approval_status   = 'APPROVED',
        approved_by       = :approved_by,
        decided_at        = :ts,
        confirmed_at      = :ts
    WHERE qual_id = :qual_id
    """
)


def update_qual_confirmed(conn, qual_id: str, verdict: str, approved_by, ts) -> int:
    """확정 이벤트(QualVerdictConfirmed) 소비 시 quals 확정 UPDATE (#4). commit=호출자.

    `confirmed_verdict`는 확정 어휘(loud/quiet, DDL 계약) — proposed의 PASS/FAIL과 다르다.
    같은 값 재소비는 무증분(멱등). qual_id 미매칭이면 0행 no-op + WARNING(감사 공백 표면화).
    반환 = 영향 행 수.
    """
    result = conn.execute(_UPDATE_CONFIRMED, {
        "qual_id": qual_id, "verdict": verdict, "approved_by": approved_by, "ts": ts})
    if result.rowcount == 0:
        logger.warning("update_qual_confirmed: qual_id 미매칭 — no-op: %s", qual_id)
    return result.rowcount


def rebase_snapshot_center(conn, chamber: str, rebase_centers: dict) -> None:
    """조용(quiet) 확정 시 스냅샷 **center-only** 리베이스 (결정9). commit=호출자.

    현 active 스냅샷을 로드해 각 그룹 center를 `rebase_centers`(gk_str→center)로 교체하되
    **σ·밴드폭 유지** — center 이동량만큼 ucl/lcl을 함께 평행이동한다(`shift_snapshot_row`,
    밴드 왜곡 방지·σ 불변). 기존 active 비활성 + 신규 active(`write_sensor_stats` 재사용,
    챔버별 is_active 1개). `rebase_centers` 빈dict/None이거나 활성 스냅샷 부재면 no-op + 로그.
    """
    if not rebase_centers:
        logger.info("rebase_snapshot_center: centers 부재 — no-op: %s", chamber)
        return
    snap = load_active_snapshot(conn, chamber)
    if not snap:
        logger.warning("rebase_snapshot_center: 활성 스냅샷 부재 — no-op: %s", chamber)
        return
    new_stats: dict[str, dict] = {}
    changed = 0
    for gk, s in snap.items():
        key = gk_str(*gk)
        nc = rebase_centers.get(key)
        if nc is None:
            new_stats[key] = dict(s)                     # 미대상 그룹은 그대로 승계
        else:
            new_stats[key] = shift_snapshot_row(s, float(nc) - float(s["center"]))
            changed += 1
    sid = f"QUAL-{chamber}-rebase-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    write_sensor_stats(conn, chamber, new_stats, qual_id="qual-rebase", snapshot_id=sid)
    logger.info("qual 스냅샷 center 리베이스(조용): chamber=%s %d/%d 그룹 갱신 (snapshot=%s)",
                chamber, changed, len(snap), sid)
