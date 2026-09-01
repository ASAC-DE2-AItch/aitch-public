"""alert 입력 소스 — replay(fixtures 재생) / kafka(fdc.alert 실구독) 스위치.

헌법 6-2 (Consumer 무중단): 역직렬화·검증 실패 메시지는 **스킵 + 로그**하고 다음으로 넘어간다.
전체 파이프라인이 손상 메시지 1건에 죽지 않아야 한다. (파손 fixture 30_edge_BROKEN 이 이 경로를 탄다.)

replay 모드: fixtures/alerts/*.json 을 파일명 정렬 순으로 재생.
kafka  모드: fdc.alert 토픽을 **kafka-python(순수 파이썬)** 으로 구독. 그루퍼(orchestrator)는
             confluent_kafka 를 쓰지만 우리 흐름에선 그루퍼의 kafka 를 안 탄다(handle_alert(dict)=
             DB 쓰기만) — 우리 consumer 라이브러리는 독립이다. confluent_kafka 의 native DLL
             (librdkafka)이 이 개발환경 WDAC 에 하드 차단돼(TSR: 애플리케이션 제어 정책) 순수
             파이썬 클라이언트로 간다. 낱개 alert 은 소비 루프(run.py)가 그루퍼로 묶는다(1-4·§4).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import FIXTURES_DIR
from .schemas.alert import AlertModel

logger = logging.getLogger(__name__)

# fdc.alert 단일 경보 채널(헌법 1-2). consumer group 은 그루퍼(consumer-group-*)와 **달라야**
# 한다 — 같은 토픽을 독립 소비(각자 오프셋)하기 위함. 6-4 네이밍(consumer-group-<역할>).
KAFKA_TOPIC_ALERT = os.environ.get("KAFKA_TOPIC_ALERT", "fdc.alert")
KAFKA_CONSUMER_GROUP = os.environ.get("KAFKA_CONSUMER_GROUP", "consumer-group-agent-service")
# .env 정본 키는 KAFKA_BOOTSTRAP_SERVERS. 그루퍼(orchestrator)는 KAFKA_BOOTSTRAP 를 읽으므로
# 둘 다 수용한다 — _SERVERS 우선(.env 정합), 없으면 KAFKA_BOOTSTRAP(그루퍼 관례), 최후 default.
KAFKA_BOOTSTRAP = (
    os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
    or os.environ.get("KAFKA_BOOTSTRAP")
    or "localhost:9092"
)


def _parse_or_skip(raw_text: str, source_label: str) -> AlertModel | None:
    """원문 1건을 AlertModel 로 파싱. 실패(JSON/스키마)면 스킵+로그 후 None (헌법 6-2)."""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("skip malformed alert (JSON decode) %s: %s", source_label, exc)
        return None
    try:
        return AlertModel.model_validate(payload)
    except ValidationError as exc:
        # 손상/계약위반 메시지 — 스킵하고 다음으로 (파이프라인 무중단)
        logger.warning(
            "skip invalid alert (schema) %s: %d error(s)",
            source_label,
            exc.error_count(),
        )
        return None


def iter_replay_alerts(fixtures_dir: Path = FIXTURES_DIR) -> Iterator[AlertModel]:
    """fixtures/alerts/*.json 을 파일명 순으로 재생하며 파싱된 AlertModel 만 방출.

    손상 메시지는 걸러진다(6-2) — 30건 중 정상 29건만 yield.
    """
    for path in sorted(fixtures_dir.glob("*.json")):
        alert = _parse_or_skip(path.read_text(encoding="utf-8"), path.name)
        if alert is not None:
            yield alert


# 한 alert 처리 = 그루퍼 + LLM 리포트 3종 + Supervisor + 적재 → 초 단위(ollama 는 더). poll 이
# 한 번에 여러 건을 가져와 그걸 다 처리한 뒤 다음 poll 을 부르면, 그 간격이 max_poll_interval 을
# 넘겨 consumer 가 그룹에서 쫓겨난다(CommitFailedError, 2026-07-24 실측). 방어 2겹:
#   ① max_poll_records=1 — poll 당 1건만 → 처리 간격을 한 alert 로 제한
#   ② max_poll_interval_ms 를 넉넉히 — 느린 LLM(특히 slow-path)도 쫓겨나지 않게
_MAX_POLL_INTERVAL_MS = int(os.environ.get("KAFKA_MAX_POLL_INTERVAL_MS", "600000"))  # 10분


def make_kafka_consumer():
    """fdc.alert 구독용 kafka-python KafkaConsumer. 지연 import — kafka 모드에서만 필요.

    수동 커밋(enable_auto_commit=False) + earliest — at-least-once. 재전달돼도 그루퍼가
    alert_id UNIQUE 로 idempotent 스킵하므로 안전(1-2).
    """
    from kafka import KafkaConsumer  # 지연 import (replay 모드는 미설치여도 됨)

    # 오프셋 시작 정책 (2026-07-30 리허설 실측 — PM 대행, C 리뷰 필수):
    #   기본 earliest = at-least-once 유지(기존 시맨틱). 단 **새 그룹**이 earliest 로 붙으면
    #   토픽 맨 앞(7/13 픽스처 발행분 + 폭주기 수만 건)부터 알람당 ~16s 로 갈아
    #   실시간 도달에 수십 시간이 걸린다. 데모/신규 기동은 latest 로 현재부터 시작한다.
    #   유효값 밖이면 earliest 로 폴백 (조용한 오타가 백로그 폭주로 이어지지 않게 로그).
    _reset = os.environ.get("AGENT_KAFKA_OFFSET_RESET", "earliest")
    if _reset not in ("earliest", "latest"):
        logger.warning("AGENT_KAFKA_OFFSET_RESET=%r 미지원 — earliest 폴백", _reset)
        _reset = "earliest"
    return KafkaConsumer(
        KAFKA_TOPIC_ALERT,
        bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
        group_id=KAFKA_CONSUMER_GROUP,
        auto_offset_reset=_reset,
        enable_auto_commit=False,
        max_poll_records=1,                          # ① 처리 간격 = 한 alert (위 주석)
        max_poll_interval_ms=_MAX_POLL_INTERVAL_MS,  # ② 느린 LLM 여유
    )


def iter_kafka_alerts(consumer, *, poll_timeout_ms: int, should_stop=lambda: False
                      ) -> Iterator[tuple[AlertModel, Any]]:
    """fdc.alert 을 구독하며 (파싱된 AlertModel, 원본 record) 를 방출한다.

    ⚠️ `poll_timeout_ms` 는 **기본값을 두지 않는다** — 호출자가 params
    `agent.kafka_poll_timeout_ms`(D25)에서 읽어 넘긴다 (헌법 6-1 조용한 기본값 금지).
    구 기본값 1000 은 코드에 박힌 매직 넘버였다 (2026-07-30 이관).

    replay 와 달리 **원본 record 도 함께** 준다 — 호출자(run.py)가 처리 완료 후 그 오프셋을
    수동 커밋해야 하기 때문(at-least-once). 손상 메시지(JSON/스키마 실패)는 스킵+로그하되
    **record 는 방출**한다 — poison 이 오프셋을 막지 않도록(호출자가 커밋해 전진).

    graceful shutdown: should_stop() 이 True 면 루프를 끝낸다(6-2). kafka-python 의
    poll(timeout_ms) 은 {TopicPartition: [records]} 를 돌려주며, 타임아웃 시 빈 dict 라
    should_stop 을 자주 확인할 수 있다.
    """
    logger.info("구독 시작: %s (group=%s, bootstrap=%s)",
                KAFKA_TOPIC_ALERT, KAFKA_CONSUMER_GROUP, KAFKA_BOOTSTRAP)
    while not should_stop():
        batches = consumer.poll(timeout_ms=poll_timeout_ms)
        for _tp, records in batches.items():
            for record in records:
                try:
                    raw = record.value.decode("utf-8")
                except (UnicodeDecodeError, AttributeError) as exc:
                    logger.warning("skip malformed alert (decode): %s", exc)
                    yield None, record  # 커밋 대상만 넘김 (poison 파티션 방지)
                    continue
                alert = _parse_or_skip(raw, f"offset={record.offset}")
                yield alert, record  # alert=None(손상)이어도 record 는 넘겨 커밋되게 한다
            if should_stop():
                return
