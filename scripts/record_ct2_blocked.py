# -*- coding: utf-8 -*-
"""수동 CT²(AE) 재보정에서 게이트에 막힌 challenger를 `ct_decisions`에 BLOCKED로 기입한다.

배경: 헌법 3-3 ②는 게이트 FAIL 처리를 "challenger 보존·미배포 + `ct_decisions`(BLOCKED)
+ 에스컬레이션"으로 규정하고, ⓒ가 **매 실행 기록**을 요구한다. 알림 채널(WP-C)이 아직
없으므로 **이 행 자체가 에스컬레이션의 실체**다 — 행이 없으면 "무엇을 왜 막았는가"가
시스템 어디에도 남지 않는다.

왜 수기 스크립트인가: 이 행은 평시 `ct2_orchestrator`가 자동으로 쓴다(`record_decision` /
`claim_running`→`finalize_decision`). 그런데 2026-08-02~03 AE 재보정은 설계 v1 §7-3대로
**CT² 자동 경로가 아닌 수동 1회 작업**이라 `retrain`·`validate_bundle`을 CLI로 직접 돌렸고,
기록 코드 경로가 통째로 우회됐다. 본 스크립트가 그 공백을 사후 보정한다.

계약 (docs/API_Contract_명세서.md §8-E · db/init.sql §11):
  · `retrain_status` ∈ DRYRUN/SKIP/GATED/IFACE_WAIT/FAILED/BLOCKED/RUNNING/SHADOW/
    AUTO_PROMOTED/PROMOTED/REJECTED/ROLLED_BACK  (VARCHAR(16))
  · `model_version_after` VARCHAR(32) · `trigger_reason` VARCHAR(64) · `ct_id` UNIQUE

ct_id 채번 규약 (신설 — 수동 경로용):
  `CT-<YYYYMMDD>-<CHAMBER>-<SEQ>-AE`  (헌법 6-4 업무 ID 포맷 + `ct2_orchestrator.ct_id_for`
  의 `-AE` 접미 승계). 자동 경로는 Incident에서 승계하지만 수동 재보정에는 Incident가
  없으므로 실행 날짜로 채번한다. 통합(다챔버) 재보정은 CHAMBER 슬롯에 `ALL`을 쓴다.

멱등: `ct_id` UNIQUE + 명시 SELECT 선행 (ON CONFLICT rowcount 불신 관측 — `record_decision`
주석과 동일 처방). 재실행해도 중복 행이 생기지 않는다.

실행:
    python scripts/record_ct2_blocked.py --dry-run     # SQL·제약 검증만 (DB 미접속)
    python scripts/record_ct2_blocked.py               # DATABASE_URL 로 실제 기입
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger("ct2.blocked")

# ── DB 제약 (db/init.sql §11 — 단일 소스는 스키마, 여기는 사전 검증용 사본) ──────
MAX_CT_ID = 64
MAX_MODEL_VERSION = 32          # model_version_after VARCHAR(32)
MAX_TRIGGER_REASON = 64         # trigger_reason  VARCHAR(64)
CT_TYPE = "ct2_ae"

# 계약 §8-E 값 목록 — 오타 하나로 감사 조회에서 누락되는 것을 막는다 (헌법 1-1 케이싱 선례).
VALID_STATUS = {
    "DRYRUN", "SKIP", "GATED", "IFACE_WAIT", "FAILED", "BLOCKED", "RUNNING",
    "SHADOW", "AUTO_PROMOTED", "PROMOTED", "REJECTED", "ROLLED_BACK",
}

# ── 기입 대상 (2026-08-02~03 AE 재보정 · 게이트 FAIL 5종) ──────────────────────
# `bundle` = `models/anomaly_ae/` 하위 폴더명 그대로 (감사 행 ↔ 디스크 경로 1:1).
#   ⚠️ 2026-08-03 개명: 구 `ae_cand_retrain_20260802_postreset*`(34~42자)는 VARCHAR(32)를
#      넘겨 절단 시 세 후보가 같은 값으로 뭉개졌다. `pr` = post-reset.
# `reason` 은 **FAIL 축 + 근거 위치**를 담는다 — ct_decisions에 자유 서술 컬럼이 없으므로
#   수치 근거(L5·recon·프로브)는 설계서에 두고 여기서는 포인터만 남긴다.
#   8/2 3종은 번들에 3파일(model·scaler·scorer)만 남아 manifest·validation.json이 유실됐다.
#   재실행 복원은 사실상 재학습이라 하지 않고, **유실 사실을 사유에 명시**한다(조용한 누락 금지).
RECORDS = [
    {
        "ct_id": "CT-20260802-ALL-01-AE",
        "bundle": "ae_cand_20260802_pr_base",
        "reason": "manual_recalib/FAIL:B1,B3,B4/ref:design_v1_10-2(report_lost)",
    },
    {
        "ct_id": "CT-20260802-ALL-02-AE",
        "bundle": "ae_cand_20260802_pr_wideval",
        "reason": "manual_recalib/FAIL:B1/ref:design_v1_10-2(report_lost)",
    },
    {
        "ct_id": "CT-20260802-ALL-03-AE",
        "bundle": "ae_cand_20260802_pr_robust",
        "reason": "manual_recalib/FAIL:B1,B3/ref:design_v1_10-2(report_lost)",
    },
    {
        "ct_id": "CT-20260803-ALL-01-AE",
        "bundle": "ae_cand_clean_time",
        "reason": "manual_recalib/FAIL:B1,B3,B4,C1/ref:validation.json",
    },
    {
        "ct_id": "CT-20260803-ALL-02-AE",
        "bundle": "ae_cand_clean_chamber",
        "reason": "manual_recalib/FAIL:B1,B3,B4,E1/ref:validation.json",
    },
    # 2026-08-05 — postguard_full(클린 전량 덤프) seg1 전반 700/223 분할 재학습.
    # 홀드아웃(챔버별 #812~922 = PM 직전)에 레짐 전환 선행 구간(#871~877 이후 포화)이
    # 포함되어 B군 FAIL — 모델 결함이 아니라 전이구간 검출. 상세: 결과 리포트 + validation.json.
    {
        "ct_id": "CT-20260805-ALL-01-AE",
        "bundle": "ae_cand_seg1pre_20260805",
        "reason": "manual_recalib/FAIL:B1,B3,B4,B5,E0/ref:validation.json",
    },
]

INSERT_SQL = """INSERT INTO ct_decisions
       (ct_id, ct_type, trigger_reason, qual_id, retrain_status, model_version_after)
   VALUES (%s, %s, %s, NULL, %s, %s)
   ON CONFLICT (ct_id) DO NOTHING"""


def validate(records, status: str = "BLOCKED") -> None:
    """DB 왕복 전에 스키마 제약을 강제한다 (초과 시 즉시 중단).

    `assert` 가 아니라 `raise` 인 이유: `python -O` 한 번이면 방어선이 통째로 사라진다
    (헌법 7장). 길이 초과는 psycopg2에서 잡히긴 하지만, **부분 커밋 뒤**에 터지면 어떤
    행이 들어갔는지 추적이 번거로워진다 — 전량 사전 검증한다.
    """
    if status not in VALID_STATUS:
        raise ValueError(f"retrain_status '{status}' 는 계약 §8-E 목록에 없음: {sorted(VALID_STATUS)}")
    seen = set()
    for r in records:
        for key, limit in (("ct_id", MAX_CT_ID), ("bundle", MAX_MODEL_VERSION),
                           ("reason", MAX_TRIGGER_REASON)):
            n = len(r[key])
            if n > limit:
                raise ValueError(
                    f"{r['ct_id']}: {key} 가 {n}자로 상한 {limit} 초과 — "
                    f"{'번들 폴더를 개명하세요' if key == 'bundle' else '값을 줄이세요'}: {r[key]!r}")
        if r["ct_id"] in seen:
            raise ValueError(f"ct_id 중복: {r['ct_id']} (UNIQUE 제약 위반)")
        seen.add(r["ct_id"])


def record(conn, records, status: str = "BLOCKED") -> tuple[int, int]:
    """행 기입. (신규, 기존) 건수 반환.

    멱등: 명시 SELECT 로 존재를 먼저 판정한다 — `ON CONFLICT` 의 rowcount 가 스모크에서
    불신 관측된 전례가 있어(`ct2_orchestrator.record_decision`) 같은 처방을 쓴다.
    """
    inserted = skipped = 0
    with conn.cursor() as cur:
        for r in records:
            cur.execute("SELECT 1 FROM ct_decisions WHERE ct_id=%s", (r["ct_id"],))
            if cur.fetchone() is not None:
                log.info("  skip  %-24s 이미 존재", r["ct_id"])
                skipped += 1
                continue
            cur.execute(INSERT_SQL, (r["ct_id"], CT_TYPE, r["reason"], status, r["bundle"]))
            log.info("  INSERT %-24s %-28s %s", r["ct_id"], r["bundle"], r["reason"])
            inserted += 1
    conn.commit()
    return inserted, skipped


def dry_run(records, status: str = "BLOCKED") -> None:
    """DB 없이 제약 검증 + 기입될 값을 그대로 출력한다."""
    log.info("── dry-run (DB 미접속) · status=%s · ct_type=%s ──", status, CT_TYPE)
    for r in records:
        log.info("  %-24s(%2d) │ %-28s(%2d) │ %s(%d)",
                 r["ct_id"], len(r["ct_id"]),
                 r["bundle"], len(r["bundle"]),
                 r["reason"], len(r["reason"]))
    log.info("제약 검증 통과 — %d행 (ct_id≤%d · model_version_after≤%d · trigger_reason≤%d)",
             len(records), MAX_CT_ID, MAX_MODEL_VERSION, MAX_TRIGGER_REASON)


def main(argv=None) -> int:
    """CLI 진입점 — 검증 → (dry-run | 기입). 0=정상 · 1=오류."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ [ct2.blocked] %(message)s")
    ap = argparse.ArgumentParser(
        prog="record_ct2_blocked",
        description="수동 CT² 재보정 게이트 FAIL 후보를 ct_decisions(BLOCKED)에 기입 (헌법 3-3②)")
    ap.add_argument("--dry-run", action="store_true", help="DB 미접속 · 제약 검증과 값 출력만")
    ap.add_argument("--status", default="BLOCKED", help="retrain_status (계약 §8-E 값)")
    args = ap.parse_args(argv)

    try:
        validate(RECORDS, args.status)
    except ValueError as e:
        log.error("사전 검증 실패: %s", e)
        return 1

    if args.dry_run:
        dry_run(RECORDS, args.status)
        return 0

    url = os.environ.get("DATABASE_URL")
    if not url:
        log.error("DATABASE_URL 미설정 — 스택 기동 후 실행하거나 --dry-run 을 쓰세요")
        return 1
    import psycopg2                                   # 지연 import (dry-run 은 무의존)
    conn = psycopg2.connect(url)
    try:
        ins, skip = record(conn, RECORDS, args.status)
    finally:
        conn.close()
    log.info("완료 — 신규 %d행 · 기존 %d행 (멱등). 다음: 설계서 §10-2 상태 갱신 확인", ins, skip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
