# -*- coding: utf-8 -*-
"""CT² promote 이중 쓰기 정합 — DB(진실)와 리로드 신호(투영)의 주기 수렴 (A14).

배경: promote 는 ① `approval_records` APPROVED ② `ct_decisions` 전이 ③ 신호 드롭
④ CHANGELOG 의 4단이고, ①②는 DB·③은 파일시스템이라 원자적이지 않다. ①② 커밋 후
③ 전에 프로세스가 죽으면 「DB=배포됨 / 실제=옛 번들」이 되는데, 게이트웨이 가드 ①
(PENDING 0행 → 409)이 재시도를 선차단하므로 승인 경로 **안**에서는 복구할 수 없다.
그래서 승인 경로 **밖** 주기 점검이 필요하다 — `_promote_recovery_needed` docstring 의
"배선은 승인 경로 밖 주기 점검(#100 리뷰 ② ⓑ)으로 후속"이 가리키는 자리가 이 파일이다.

판정표 (APPROVED 인 ct2_deploy 행 × ct_decisions × 신호 3곳 — 우선순위 순):

  ⑤ retrain_status 가 PROMOTED 계열이 아님(롤백·거부 등 명시 상태) → **무동작**
     ("복구는 언제나 앞으로만" — 시스템이 승인·롤백 기록을 역전시키지 않는다)
  ③ 신호가 `failed/`               → **에스컬레이션 로그** (재드롭 금지 — 같은 번들
     재드롭은 또 실패할 뿐이다, #100 리뷰 ③)
  ② 신호가 원본 자리에 stale_min 이상 방치 → **ERROR 로그만** (consumer 부재/폴링 고장.
     파일은 consumer 소유 — 대신 옮기면 재기동 시 이중 처리)
  ④ 신호가 `processed/`            → **무동작** (정상 완료)
  ① 신호 흔적 **0곳**              → **재드롭** (`drop_promote_signal` 재수행 —
     `_promote_recovery_needed` 와 동일 판정. 유일한 쓰기 조치, 실행당 상한 1건)

원칙: 신호 파일을 옮기거나 지우지 않는다(소유자 = consumer). PENDING 을 되돌리지
않는다(감사 추적 1-1). ct_decisions 행이 아예 없으면(② 전에 죽음) 재드롭은 하되
② 재수행은 수동 — WARNING 으로 드러낸다.

기동: gateway startup 폴러(`ct.reconciler_poll_sec` — 0/미설정 = **비활성, 기본 off**)
또는 수동 1회 `python -m src.orchestrator.ct2_reconciler --once`.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from . import ct2_deploy_approval as cda

log = logging.getLogger("ct2_reconciler")

PROMOTED_STATES = {"PROMOTED", "AUTO_PROMOTED"}   # mark_ct_decision 이 promote 시 기록하는 값
MAX_REDROPS_PER_SCAN = 1                          # 폭주 방지 — 실행당 쓰기 조치 상한
DEFAULT_STALE_MIN = 10.0

# 같은 프로세스에서 같은 (incident, 종류) ERROR 를 폴링마다 반복하지 않기 위한 기억.
# 프로세스 재시작이면 다시 울린다 — 감시 로그는 "안 울리면 없는 것과 구별되지 않는다"(7장).
_ALREADY_LOGGED: set[tuple[str, str]] = set()


def _signal_state(incident_id: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """신호 3곳 관측 → `{"where": none|original|processed|failed, "stale_sec": float|None}`.

    우선순위: failed > original > processed — failed 는 어느 조합에서든 사람이 봐야 하고,
    original 이 남아 있으면 아직 소비 전이므로 processed 여부보다 먼저 본다.
    """
    name = cda.promote_signal_path(incident_id).name
    if (cda.PROMOTE_DIR / "failed" / name).exists():
        return {"where": "failed", "stale_sec": None}
    orig = cda.PROMOTE_DIR / name
    if orig.exists():
        ref = time.time() if now is None else now
        return {"where": "original", "stale_sec": max(0.0, ref - orig.stat().st_mtime)}
    if (cda.PROMOTE_DIR / "processed" / name).exists():
        return {"where": "processed", "stale_sec": None}
    return {"where": "none", "stale_sec": None}


def decide(*, retrain_status: Optional[str], where: str, stale_sec: Optional[float],
           bundle_exists: bool, stale_min: float = DEFAULT_STALE_MIN) -> str:
    """판정표의 순수 구현 — 부작용 없음 (테스트 고정용).

    반환: redrop | escalate_failed | stale_error | waiting | ok | skip_status | skip_no_bundle
    """
    st = (retrain_status or "").upper()
    if retrain_status is not None and st not in PROMOTED_STATES:
        return "skip_status"                      # ⑤ 롤백·거부 등 — 앞으로만 간다
    if where == "failed":
        return "escalate_failed"                  # ③ 재드롭 금지, 사람 몫
    if where == "original":
        if stale_sec is not None and stale_sec >= stale_min * 60.0:
            return "stale_error"                  # ② consumer 부재 의심 — 로그만
        return "waiting"                          # 소비 대기 중 (정상 과도 상태)
    if where == "processed":
        return "ok"                               # ④ 정상 완료
    if not bundle_exists:
        return "skip_no_bundle"                   # ①의 전제 미충족 — 재드롭해도 failed/ 행
    return "redrop"                               # ① 무흔적 — 유일한 쓰기 조치


def _log_once(incident_id: str, kind: str, message: str) -> None:
    """(incident, 종류)당 프로세스 수명 내 1회만 ERROR — 폴링 스팸 방지."""
    key = (incident_id, kind)
    if key in _ALREADY_LOGGED:
        return
    _ALREADY_LOGGED.add(key)
    log.error(message)


def reconcile_once(conn, *, stale_min: float = DEFAULT_STALE_MIN,
                   max_redrops: int = MAX_REDROPS_PER_SCAN,
                   now: Optional[float] = None) -> dict[str, Any]:
    """APPROVED 인 ct2_deploy 전수 1회 대조. 요약 dict 반환 (폴러·CLI 공용).

    커넥션 위생은 호출자 책임 (#100 리뷰 ① — 폴러는 자기 전용 커넥션 사용).
    """
    summary: dict[str, Any] = {"scanned": 0, "redropped": [], "escalate_failed": [],
                               "stale": [], "waiting": [], "ok": 0,
                               "skip_status": [], "skip_no_bundle": []}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT incident_id, original_value->>'bundle'
                 FROM approval_records
                WHERE request_type = %s AND UPPER(status) = %s
                ORDER BY decided_at NULLS LAST""",
            (cda.REQUEST_TYPE, cda.STATUS_APPROVED))
        rows = cur.fetchall()
    redrops = 0
    for incident_id, bundle in rows:
        summary["scanned"] += 1
        with conn.cursor() as cur:
            cur.execute("SELECT retrain_status FROM ct_decisions WHERE ct_id = %s",
                        (cda.ct_id_for(incident_id),))
            hit = cur.fetchone()
        retrain_status = hit[0] if hit else None
        sig = _signal_state(incident_id, now=now)
        bundle_exists = bool(bundle) and os.path.exists(bundle)
        action = decide(retrain_status=retrain_status, where=sig["where"],
                        stale_sec=sig["stale_sec"], bundle_exists=bundle_exists,
                        stale_min=stale_min)

        if action == "redrop":
            if redrops >= max_redrops:
                summary["waiting"].append(incident_id)     # 다음 주기에 처리 — 폭주 방지
                log.warning("재드롭 상한(%d) 도달 — %s 는 다음 주기", max_redrops, incident_id)
                continue
            cda.drop_promote_signal(incident_id, bundle)
            redrops += 1
            summary["redropped"].append(incident_id)
            note = "" if retrain_status else " (ct_decisions 행 부재 — ② 재수행은 수동 확인 필요)"
            log.warning("CT² 신호 재드롭: %s — APPROVED 인데 드롭 이력 0곳 (③에서 사망 추정)%s",
                        incident_id, note)
        elif action == "escalate_failed":
            summary["escalate_failed"].append(incident_id)
            _log_once(incident_id, "failed",
                      f"CT² 번들 로드 실패 상태: {incident_id} — failed/ 에 신호 잔존. "
                      f"재드롭은 같은 실패를 반복하므로 하지 않는다. 번들 검증 후 수동 재승인 필요.")
        elif action == "stale_error":
            summary["stale"].append(incident_id)
            _log_once(incident_id, "stale",
                      f"CT² 신호 방치 {sig['stale_sec']:.0f}s ≥ {stale_min}분: {incident_id} — "
                      f"consumer 가 신호를 집어가지 않는다 (프로세스 생존 확인 필요).")
        elif action == "skip_no_bundle":
            summary["skip_no_bundle"].append(incident_id)
            _log_once(incident_id, "no_bundle",
                      f"CT² 재드롭 불가: {incident_id} — 번들 경로 소실({bundle!r}). 수동 확인 필요.")
        elif action == "skip_status":
            summary["skip_status"].append(incident_id)
        elif action == "waiting":
            summary["waiting"].append(incident_id)
        else:
            summary["ok"] += 1
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    """CLI — `python -m src.orchestrator.ct2_reconciler --once [--stale-min N]`."""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="CT² promote 이중 쓰기 정합 1회 점검 (A14)")
    parser.add_argument("--once", action="store_true", required=True,
                        help="1회 실행 (주기 실행은 gateway 폴러 — ct.reconciler_poll_sec)")
    parser.add_argument("--stale-min", type=float, default=DEFAULT_STALE_MIN,
                        help=f"원본 자리 방치 판정(분, 기본 {DEFAULT_STALE_MIN})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s │ %(levelname)-7s │ %(message)s")
    import psycopg2                                   # 지연 import — 단위 테스트 격리
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL 미설정")
        return 2
    conn = psycopg2.connect(dsn)
    try:
        summary = reconcile_once(conn, stale_min=args.stale_min)
        conn.commit()                                 # SELECT 만이라도 트랜잭션 정리
        print(_json.dumps(summary, ensure_ascii=False, indent=2))
        # 사람 눈에 남는 종료코드: 조치·이상이 있으면 1 (cron/스크립트 후처리용)
        return 1 if (summary["redropped"] or summary["escalate_failed"]
                     or summary["stale"] or summary["skip_no_bundle"]) else 0
    finally:
        conn.close()


if __name__ == "__main__":                            # pragma: no cover
    raise SystemExit(main())
