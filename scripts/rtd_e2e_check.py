# -*- coding: utf-8 -*-
"""RTD E2E 왕복 검증 — 발동 → 기록 → 신호 → 해제까지 한 번에 확인한다.

드라이런(`rtd_dryrun.py`)이 **판정만** 보는 반면, 이쪽은 실제로 `chamber_inhibits` 행을
만들고 신호 파일을 떨군 뒤 되돌린다. 차단기를 켜기 전/후 왕복이 온전한지 보는 용도다.

**왜 합성 알람인가**: 그루퍼는 `_already_grouped`에서 중복 alert_id를 즉시 스킵하는데,
시뮬 replay가 루프라 두 바퀴째부터 alert_id가 전부 재사용된다. 그래서 Kafka 경로로는
RTD 판정까지 도달하지 못한다(2026-08-06 실측: 10분에 신규 alert_id 9건). 여기서는
**고유 alert_id를 단 합성 알람**으로 `record_trigger` 이후를 그대로 태운다 — 판정
재료(`persistent_breach`)는 합성이 아니라 **라이브 DB 실측치**를 쓴다.

실행:
    python -m scripts.rtd_e2e_check --chamber SIM_CH_2
    python -m scripts.rtd_e2e_check --chamber SIM_CH_2 --keep   # 해제하지 않고 남김(화면 확인용)
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import yaml
from dotenv import load_dotenv

from src.orchestrator import chamber_inhibit as ci

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DSN = "postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform"


def _params() -> dict:
    """config/params.yaml 로드 (rtd 절 포함 — 차단기 상태도 여기서 읽힌다)."""
    p = ROOT / "config" / "params.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def _synthetic_alert(chamber: str, sensors: list, stamp: str) -> dict:
    """사전 필터를 통과할 최소 형태의 CRITICAL 알람 (계약 키 `sensor` 사용).

    본 판정은 이 알람이 아니라 창 집계(persistent_breach)로 내려가므로, 여기서는
    필터 통과에 필요한 것만 담는다 — severity·violations(다중 센서)·chamber_id.
    """
    return {
        "alert_id": f"ALERT-E2E-{stamp}",
        "chamber_id": chamber,
        "severity": "CRITICAL",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "violations": [{"rule_id": "N1", "sensor": s} for s in sensors],
    }


def main() -> None:
    """발동 → DB·파일 확인 → (옵션) 해제 → 확인. 각 단계 결과를 표로 출력한다."""
    ap = argparse.ArgumentParser(description="RTD E2E 왕복 검증")
    ap.add_argument("--chamber", default="SIM_CH_2")
    ap.add_argument("--sensors", default="C17,C62,C11", help="합성 알람 violations 센서")
    ap.add_argument("--keep", action="store_true", help="해제하지 않고 남김 (화면 확인용)")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    params = _params()
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    incident_id = f"INC-E2E-{stamp}"
    alert = _synthetic_alert(args.chamber, args.sensors.split(","), stamp)

    conn = psycopg2.connect(args.dsn or os.environ.get("DATABASE_URL") or _DEFAULT_DSN)
    try:
        print(f"차단기 rtd.auto_inhibit = {(params.get('rtd') or {}).get('auto_inhibit')}")
        sensors, total = ci.persistent_breach(conn, args.chamber, params)
        print(f"① 본 판정 재료(라이브) — 창 알람 {total}건 · 지속이탈 {sensors or '없음'}")

        hit = ci.record_trigger(conn, incident_id, alert, params)
        conn.commit()                                   # 순서 규약: 기록 → 커밋 → 정지
        if hit is None:
            print("② 발동 안 함 — 차단기 off 이거나 판정 미충족. (여기서 중단)")
            return
        chambers, alert_id, scope = hit
        ci.drop_signal_after_commit(chambers, incident_id, alert_id, scope)
        print(f"② 발동 — scope={scope} · 대상 {chambers}")

        with conn.cursor() as cur:
            cur.execute("""SELECT chamber_id, scope, trigger_alert_id, released_at
                             FROM chamber_inhibits WHERE incident_id = %s ORDER BY chamber_id""",
                        (incident_id,))
            rows = cur.fetchall()
        print(f"③ DB 기록 {len(rows)}행: {[(r[0], r[1], r[3]) for r in rows]}")
        files = sorted(p.name for p in ci.INHIBIT_DIR.glob("*.json"))
        print(f"④ 신호 파일: {files}")

        if args.keep:
            print("⑤ --keep — 해제 생략. 화면(Header 핑크·RtdBanner) 확인 후 아래로 해제:")
            print(f"   python -m scripts.rtd_e2e_check --chamber {args.chamber} --release-only")
            return

        n = ci.release(conn, args.chamber, incident_id,
                       qual_id=f"QUAL-E2E-{stamp}", released_by="e2e_check")
        conn.commit()
        left = sorted(p.name for p in ci.INHIBIT_DIR.glob("*.json"))
        print(f"⑤ 해제 {n}행 · 남은 신호 파일: {left or '없음'}")
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM chamber_inhibits
                            WHERE incident_id = %s AND released_at IS NULL""", (incident_id,))
            row = cur.fetchone()
        print(f"⑥ 미해제 잔여: {row[0]}행  →  {'OK' if row and row[0] == 0 else '⚠ 확인 필요'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
