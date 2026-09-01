# -*- coding: utf-8 -*-
"""CT² 스모크 — ChamberRequalified 수동 발행 (계약 §8-C '콘솔 프로듀서 수동 발행' 대역).

용법: python scripts/ct2_smoke_publish.py INC-20260729-SIMCH3-001 [--chamber SIM_CH_3]
오케스트레이터(dry-run) 창이 이 이벤트를 받아 ct_decisions에 DRYRUN 행을 남기면 성공.
같은 ID 재발행 = 멱등 확인용 (두 번째는 '기록=False' 로그).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone


def main() -> None:
    """이벤트 1건 발행 — 스키마는 계약 §8-C 예시와 동일 필드."""
    ap = argparse.ArgumentParser(description="ChamberRequalified 수동 발행 (스모크)")
    ap.add_argument("incident_id")
    ap.add_argument("--chamber", default="SIM_CH_3")
    ap.add_argument("--bootstrap", default="localhost:9092")
    ap.add_argument("--topic", default="fdc.agent")
    a = ap.parse_args()

    from confluent_kafka import Producer
    evt = {
        "event_type": "ChamberRequalified",
        "incident_id": a.incident_id,
        "chamber_id": a.chamber,
        "qual_id": f"QUAL-{a.incident_id[4:]}" if a.incident_id.startswith("INC-") else "QUAL-SMOKE",
        "pm_count": 0,
        "corrections_applied": [],
        "limit_version": "smoke",
        "requalified_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "approved_by": "smoke_pm",
    }
    p = Producer({"bootstrap.servers": a.bootstrap})
    p.produce(a.topic, json.dumps(evt, ensure_ascii=False).encode("utf-8"))
    p.flush(10)
    print(f"발행 완료 → {a.topic}: {evt['incident_id']} ({evt['chamber_id']})")


if __name__ == "__main__":
    main()
