# -*- coding: utf-8 -*-
"""P4-1 Kafka 이벤트 발행 — 계약 토픽 라우팅 (confluent_kafka 지연 import).

계약: 승인 워크플로 이벤트 → `fdc.agent` (line 65) / CorrectionApplied(적용) → `fdc.correction` (line 280).
소비자는 `event_type`으로 필터 (계약 line 351). route_topic·make_kafka_emit은 confluent 무의존 → 단위 테스트.
"""

import json
import logging

log = logging.getLogger("gate-events")

# event_type → 토픽. 미지정은 승인 워크플로 채널(fdc.agent).
_TOPIC = {
    "CorrectionApplied": "fdc.correction",     # 계약 §6 line 280 (limit/recipe 적용)
    "WaferDispositioned": "fdc.correction",    # Event Contract(기획서 §7): wafer 스크랩/해제 판정 완료
    # MaintenanceApproved(정비 승인, 2026-08-12) 는 **의도적으로 미등재** — DEFAULT(fdc.agent).
    #   정비 승인은 물리 조치 적용이 아니라 승인 워크플로 이벤트다. 여기 넣어 fdc.correction
    #   으로 보내면 8/11 분리 이전(정비가 처분 채널을 겸함)으로 돌아간다.
}
DEFAULT_TOPIC = "fdc.agent"


def route_topic(event_type: str) -> str:
    """이벤트 → Kafka 토픽 (승인 워크플로 이벤트는 fdc.agent, 적용은 fdc.correction)."""
    return _TOPIC.get(event_type, DEFAULT_TOPIC)


def make_kafka_emit(producer):
    """emit(event_type, payload) 훅 생성 — payload에 event_type 병기 후 라우팅 토픽에 발행.

    producer: confluent_kafka Producer (또는 .produce(topic,key,value)+.poll 호환). 실발행은 env.
    """
    def _emit(event_type: str, payload: dict) -> None:
        topic = route_topic(event_type)
        msg = {"event_type": event_type, **payload}            # 소비자 필터용 (계약 line 351)
        producer.produce(topic=topic,
                         key=str(payload.get("incident_id", "")).encode("utf-8"),
                         value=json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        producer.poll(0)
        log.debug(f"발행 {topic} ← {event_type} ({payload.get('incident_id')})")
    return _emit


def build_producer(bootstrap: str):
    """confluent_kafka Producer 생성 (지연 import — 실 env에서만)."""
    from confluent_kafka import Producer
    return Producer({"bootstrap.servers": bootstrap, "client.id": "approval-gate",
                     "linger.ms": 10, "compression.type": "lz4"})
