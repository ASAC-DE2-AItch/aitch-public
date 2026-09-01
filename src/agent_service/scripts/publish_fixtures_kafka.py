"""fixtures/alerts/*.json 을 fdc.alert 로 발행 — kafka-python(순수 파이썬) E2E 도구.

시뮬레이터 mock_alert_publisher 는 confluent_kafka.Producer 를 쓰는데, 그 native DLL 이
Windows 개발환경 WDAC 에 차단된다(consumer 와 같은 사유). 이 스크립트는 kafka-python 으로
발행해 그 벽을 우회한다 — **kafka 배선 E2E 검증 전용**(운영 발행기가 아니다).

사용:
    python -m src.agent_service.scripts.publish_fixtures_kafka           # 정상 29건 발행
    python -m src.agent_service.scripts.publish_fixtures_kafka --one     # 1건만 (빠른 확인)
    python -m src.agent_service.scripts.publish_fixtures_kafka --topic fdc.alert --bootstrap localhost:9092

consumer(python -m src.agent_service.app.run) 를 먼저 띄운 뒤 이 스크립트를 다른 터미널에서
돌리면, consumer 로그에 처리·적재가 찍히고 agent_reports 에 SUP- 행이 쌓인다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", default="localhost:9092")
    ap.add_argument("--topic", default="fdc.alert")
    ap.add_argument("--one", action="store_true", help="1건만 발행(빠른 확인)")
    args = ap.parse_args()

    from kafka import KafkaProducer  # 순수 파이썬 — WDAC 미차단

    producer = KafkaProducer(
        bootstrap_servers=args.bootstrap.split(","),
        value_serializer=lambda v: v.encode("utf-8"),
        # 키 = chamber_id (계약 §3 파티션 키). 같은 챔버가 같은 파티션으로 가 순서 보존.
        key_serializer=lambda k: (k or "").encode("utf-8"),
    )

    files = sorted(FIXTURES.glob("*.json"))
    if args.one:
        files = files[:1]

    sent = skipped = 0
    for path in files:
        raw = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # 의도적 파손 fixture(46_edge_BROKEN)는 파티션 키를 못 뽑으니 건너뛴다.
            print(f"skip (broken fixture): {path.name}")
            skipped += 1
            continue
        chamber = payload.get("chamber_id", "")
        producer.send(args.topic, key=chamber, value=raw)
        sent += 1
        print(f"→ {args.topic}  {path.name}  (chamber={chamber})")

    producer.flush()
    producer.close()
    print(f"\n발행 완료: {sent}건 (skip {skipped}) → {args.topic} @ {args.bootstrap}")


if __name__ == "__main__":
    main()
