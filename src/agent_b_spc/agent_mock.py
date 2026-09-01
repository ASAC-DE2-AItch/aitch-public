"""B6-3-c: fdc.agent 이벤트 dev mock publisher.

오케스트레이터가 아직 발행하지 않는 `QualVerdictConfirmed`·`ChamberRequalified`를 c 배선
검증·데모용으로 만든다(§6·§7 — mock 이벤트 생성·주입은 c 소유). 스키마는 계약 §8-B 준수:
verdict enum 소문자 `loud`/`quiet`, `chamber_id` 필수. PM이 실제 발행하면 이 mock을 실이벤트로
교체(스키마 동일이라 c 배선 무변경).

`publish_*`는 실 Kafka로 흘리는 dev 헬퍼(confluent 지연 import) — 테스트는 dict를 FakeMsg로 감싼다.
"""

from __future__ import annotations

import json


def qual_verdict_confirmed(chamber: str, verdict: str, *, qual_id: str | None = None,
                           incident_id: str | None = None, pm_count: int | None = None,
                           confirmed_at: str | None = None,
                           approved_by: str = "mock_engineer") -> dict:
    """`QualVerdictConfirmed`(계약 §8-B) — S7 판정(조용/요란) 엔지니어 승인 확정 방송.

    verdict='loud'(요란→가한계 Phase 1) / 'quiet'(조용→정상 복귀). c는 `chamber_id`·`verdict`만
    소비(on_qual_verdict). 나머지는 계약 완결성(A·Dashboard 소비)용.
    """
    return {
        "event_type": "QualVerdictConfirmed",
        "chamber_id": chamber,
        "verdict": verdict,
        "qual_id": qual_id,
        "incident_id": incident_id,
        "pm_count": pm_count,
        "confirmed_at": confirmed_at,
        "approved_by": approved_by,
    }


def chamber_requalified(chamber: str, *, incident_id: str | None = None,
                        requalified_at: str | None = None,
                        new_limit_version: str | None = None) -> dict:
    """`ChamberRequalified` — 재수립 완료(전 그룹 firm 적용 후 1회) 방송.

    c는 이 신호로 firm 후속(스냅샷·Y·TTTM 복귀·verify) 1회 구동(`_requalified_live=True`). 전용
    스키마 블록이 계약에 없어 c 초안(chamber_id·incident_id·requalified_at·new_limit_version) —
    **PM 확정 필요**(발행자·토픽·필드). 소비는 `chamber_id`만.
    """
    return {
        "event_type": "ChamberRequalified",
        "chamber_id": chamber,
        "incident_id": incident_id,
        "requalified_at": requalified_at,
        "new_limit_version": new_limit_version,
    }


def publish(producer, event: dict, *, topic: str = "fdc.agent") -> None:
    """dev: mock 이벤트를 실 Kafka fdc.agent에 발행(키=chamber_id). confluent producer 주입."""
    producer.produce(topic=topic, key=event["chamber_id"].encode("utf-8"),
                     value=json.dumps(event).encode("utf-8"))
    producer.poll(0)
