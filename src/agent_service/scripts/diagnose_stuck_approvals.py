# -*- coding: utf-8 -*-
"""승인 표류 진단 — **「승인은 났는데 적용은 안 된」** 건을 사람 눈에 보이게 한다 (LLM 미사용).

**왜 만들었나 (2026-08-06 · PR #111 리뷰 3번 공약).**
승인 버튼을 누르면 오케스트레이터는 이렇게 하고 **끝난다**:

    approval_graph.apply_and_publish()
      ① commit_decision()          → approval_records.status = 'APPROVED'
      ② emit("CorrectionApplied")  → fdc.correction 발행
      ③ set_lifecycle("verifying") → 화면은 "조치 나감 · 관측 중"

실제 적용은 ②를 구독한 **B consumer 가 나중에** 한다. 그런데 B 의 `apply_approved` 는
`SUPERSEDED`(신규 제안에 밀려 승인 무효)·`APPLY_FAILED`(영구 불량 입력)에서
**로그 한 줄만 남기고 `False` 를 돌려준다** — 그 False 를 받는 쪽이 없다.
①③ 은 이미 커밋됐고 화면은 `verifying` 이라, **엔지니어는 자기가 승인한 것이
적용됐다고 믿는다.** 헌법 1-1 이 `approval_records` 를 "감사 추적"이라 한 자리에서
**감사 기록과 실제 상태가 갈라진 채 아무도 모르는** 상태다.

**기록은 이미 양쪽에 다 있다. 없는 것은 그 둘을 맞대보는 자리다.**

    approval_records.original_value -> 'limit_analysis' ->> 'correction_id'
        =  limit_corrections.correction_id      (B 채번 승계 — approval_graph.py:128)

이 도구는 그 조인을 돌려 **불일치 건수를 이름으로** 보여준다. 화면·컬럼 신설이 아니다.

⚠️ **이건 알림이 아니라 알림의 재료다.** 어디에 붙일지(화면 배너 / 게이트웨이 폴링 /
   lifecycle 전이)는 `gateway`·`frontend`·`orchestrator` 소유자인 **PM 판단**이다(헌법 3-1).
   우리는 조인과 판정식까지 들고 간다.

사용:
    python -m src.agent_service.scripts.diagnose_stuck_approvals
    python -m src.agent_service.scripts.diagnose_stuck_approvals --grace-min 30
    python -m src.agent_service.scripts.diagnose_stuck_approvals --sql   # 쿼리만 출력(붙여 쓰기용)

종료 코드: 불일치 0건이면 0, 1건 이상이면 **1** — 스케줄러·CI 가 그대로 걸 수 있게.
  (연결 실패는 2 — "문제 없음"과 "못 봤음"을 절대 같은 값으로 내지 않는다)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT.parents[1]))

logger = logging.getLogger("diagnose-approvals")

#: 승인 직후 몇 분은 PROPOSED 가 정상이다 — B consumer 가 아직 안 집었을 뿐.
#: 이 유예를 안 두면 **일시적 지연이 영구 미적용으로 오보**된다(W51 과 같은 축:
#: 결정적 실패와 일시적 실패를 가르지 않으면 신호가 늑대소년이 된다).
DEFAULT_GRACE_MIN = 10

#: 적용이 끝난 것으로 보는 상태 — 이 밖은 전부 "아직/영영 안 됨"이다.
_APPLIED_OK = ("APPLIED", "AUTO_APPLIED", "VERIFIED")

#: 사람이 읽을 진단명. status 값 자체보다 **무슨 일이 일어난 건지**를 적는다.
_VERDICT = {
    "SUPERSEDED": "밀려남 — 승인 뒤 같은 그룹에 신규 제안이 들어와 이 승인은 무효 (B가 되돌림 차단)",
    "APPLY_FAILED": "적용 실패 — 원안 NULL·구조 가드 위반 등 영구 불량 입력 (재시도해도 안 됨)",
    "PROPOSED": "미적용 — 승인이 났는데 아직 PROPOSED (B가 못 집었거나 발행이 유실)",
    "__missing__": "행 없음 — 승인 payload 의 correction_id 로 찾을 행이 아예 없다 (제일 나쁨)",
}

# ── 조인 ─────────────────────────────────────────────────────────────────────
# LEFT JOIN 인 이유: **행이 없는 경우가 가장 나쁜 케이스**라 INNER 로 지우면 안 된다.
# `%(grace)s` 는 psycopg 파라미터 — 문자열 포매팅으로 만들지 않는다.
STUCK_LIMIT_SQL = """
SELECT ar.incident_id,
       ar.status                                                  AS approval_status,
       ar.selected_option,
       ar.approver,
       ar.decided_at,
       ar.original_value->'limit_analysis'->>'correction_id'      AS correction_id,
       lc.status                                                  AS apply_status,
       lc.chamber_id,
       lc.sensor_id
  FROM approval_records ar
  LEFT JOIN limit_corrections lc
         ON lc.correction_id = ar.original_value->'limit_analysis'->>'correction_id'
 WHERE ar.status IN ('APPROVED', 'MODIFIED')
   AND ar.original_value->'limit_analysis'->>'correction_id' IS NOT NULL
   AND ar.decided_at < NOW() - (%(grace)s * INTERVAL '1 minute')
   AND (lc.correction_id IS NULL OR lc.status <> ALL(%(ok)s))
 ORDER BY ar.decided_at DESC
"""

# recipe 축 — 같은 모양의 구멍인지 함께 본다.
# ⚠️ `recipe_corrections` 의 status 어휘엔 SUPERSEDED·APPLY_FAILED 가 **없다**
#    (PROPOSED / APPLIED / VERIFIED). 그래서 이쪽 신호는 "오래 PROPOSED" 하나뿐이고,
#    limit 과 같은 정밀도로 읽으면 안 된다. 여기서는 세기만 한다.
STUCK_RECIPE_SQL = """
SELECT ar.incident_id,
       ar.status                                                       AS approval_status,
       ar.decided_at,
       ar.original_value->'recipe_tuning'->>'recipe_correction_id'     AS correction_id,
       rc.status                                                       AS apply_status
  FROM approval_records ar
  LEFT JOIN recipe_corrections rc
         ON rc.recipe_correction_id = ar.original_value->'recipe_tuning'->>'recipe_correction_id'
 WHERE ar.status IN ('APPROVED', 'MODIFIED')
   AND ar.original_value->'recipe_tuning'->>'recipe_correction_id' IS NOT NULL
   AND ar.decided_at < NOW() - (%(grace)s * INTERVAL '1 minute')
   AND (rc.recipe_correction_id IS NULL OR rc.status <> ALL(%(ok)s))
 ORDER BY ar.decided_at DESC
"""


def _query(sql: str, grace_min: int) -> list[dict[str, Any]]:
    """조회 1회. **연결 실패는 삼키지 않는다** — 호출자가 exit 2 로 구분해야 한다.

    ⚠️ 다른 조회 모듈(`db._fetch`)은 실패를 빈 리스트로 삼킨다. 그쪽은 근거가 얕아질
    뿐이지만 여기서 같은 짓을 하면 **"못 봤다"가 "문제 없다"로 보고**된다 — 이 도구가
    막으려는 사고를 이 도구가 저지르는 셈이다.
    """
    import psycopg
    from psycopg.rows import dict_row

    from src.agent_service.app.db import CONNECT_TIMEOUT_SEC, dsn

    with psycopg.connect(dsn(), row_factory=dict_row,
                         connect_timeout=CONNECT_TIMEOUT_SEC) as conn, conn.cursor() as cur:
        cur.execute(sql, {"grace": grace_min, "ok": list(_APPLIED_OK)})
        return [dict(r) for r in cur.fetchall()]


def _verdict(row: dict[str, Any]) -> str:
    """행 하나의 진단명 — status 가 없으면 '행 없음'이다."""
    return _VERDICT.get(row.get("apply_status") or "__missing__",
                        f"미확인 상태({row.get('apply_status')})")


def diagnose(grace_min: int = DEFAULT_GRACE_MIN) -> int:
    """승인 ↔ 적용 불일치를 표로 낸다. 반환 = exit code(0 없음 / 1 있음 / 2 못 봄)."""
    print(f"승인 표류 진단 — 승인 후 {grace_min}분이 지났는데 적용되지 않은 건")
    try:
        limit_rows = _query(STUCK_LIMIT_SQL, grace_min)
        recipe_rows = _query(STUCK_RECIPE_SQL, grace_min)
    except Exception as exc:  # noqa: BLE001 — 연결/조회 실패는 '문제 없음'이 아니다
        print(f"\n  🔴 조회 실패 — **판정 불가**(문제 없음이 아니다): {type(exc).__name__}: {exc}")
        return 2

    total = len(limit_rows) + len(recipe_rows)
    print("=" * 78)
    if not total:
        print("  ✅ 불일치 0건 — 승인된 건은 전부 적용 상태다")
        print("=" * 78)
        return 0

    print(f"  🔴 불일치 {total}건 (limit {len(limit_rows)} · recipe {len(recipe_rows)})")
    print("=" * 78)
    for row in limit_rows:
        print(f"\n  [limit] {row['correction_id']}  ({row.get('chamber_id')} · {row.get('sensor_id')})")
        print(f"     승인: {row['approval_status']} by {row.get('approver') or '?'} at {row['decided_at']}")
        print(f"     적용: {row.get('apply_status') or '(행 없음)'}")
        print(f"     → {_verdict(row)}")
        print(f"     incident: {row['incident_id']}")
    for row in recipe_rows:
        print(f"\n  [recipe] {row['correction_id']}")
        print(f"     승인: {row['approval_status']} at {row['decided_at']}")
        print(f"     적용: {row.get('apply_status') or '(행 없음)'}")
        print(f"     → {_verdict(row)}")
        print(f"     incident: {row['incident_id']}")

    print("\n" + "=" * 78)
    print("  ※ 이 건들은 **승인 화면상 이미 조치 완료**로 보인다 (lifecycle=verifying).")
    print("     엔지니어에게 되돌리는 알림 자리는 PM 소관 — 이 표가 그 재료다.")
    print("=" * 78)
    return 1


def main() -> None:
    """CLI 진입점."""
    ap = argparse.ArgumentParser(description="승인 났는데 적용 안 된 건 진단 (읽기 전용)")
    ap.add_argument("--grace-min", type=int, default=DEFAULT_GRACE_MIN,
                    help=f"승인 후 이 시간까지는 정상 지연으로 본다 (기본 {DEFAULT_GRACE_MIN}분)")
    ap.add_argument("--sql", action="store_true", help="쿼리만 출력하고 끝낸다 (psql 붙여쓰기용)")
    args = ap.parse_args()

    if args.sql:
        print("-- limit 축\n" + STUCK_LIMIT_SQL)
        print("-- recipe 축\n" + STUCK_RECIPE_SQL)
        sys.exit(0)
    sys.exit(diagnose(args.grace_min))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    main()
