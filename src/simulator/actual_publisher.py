# -*- coding: utf-8 -*-
"""fdc.actual 발행 스텁 — 실측 C65 지연 피드백 (API Contract v4.6 초안).

⚠️ 기본 비활성 (enabled=False):
  - 계약이 초안 상태 (소비자 A 리뷰 대기, 헌법 2-2)
  - 멘토 논의(2026-07-07)로 Model R2R 방식 확정 후 스키마 최종화 → 활성화

동작 (활성 시): wafer 완료마다 (wafer_id, actual_c65, ...) 버퍼에 push,
label_delay_wafers 경과분을 pop하여 fdc.actual로 발행 —
"성적표가 2개월 늦게 도착"의 wafer 수 기준 재현 (E6 비율 정의).
"""

import json
import logging
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("actual-publisher")


class ActualPublisher:
    """label_delay 큐 기반 실측 C65 지연 발행기."""

    def __init__(self, producer, topic: str = "fdc.actual",
                 label_delay_wafers: int = 13900, enabled: bool = False):
        self.producer = producer
        self.topic = topic
        self.delay = label_delay_wafers
        self.enabled = enabled
        self._queue: deque = deque()
        if enabled:
            log.info(f"fdc.actual 활성 — label_delay={label_delay_wafers} wafer")
        else:
            log.info("fdc.actual 비활성 (계약 초안 — A 리뷰 후 활성화)")

    def on_wafer_complete(self, wafer_id: str, lot_id: str, chamber_id: str,
                          actual_c65: float | None, pm_count: int,
                          is_qual: bool = False):
        """wafer 처리 완료 훅 — 큐에 넣고, 지연 경과분을 발행.

        is_qual=True는 무조건 스킵 — Qual wafer는 WT 미경유라 실측 없음 (계약 1-B).
        """
        if not self.enabled or actual_c65 is None or is_qual:
            return
        self._queue.append({
            "wafer_id": wafer_id,
            "lot_id": lot_id,
            "chamber_id": chamber_id,
            "actual_c65": actual_c65,
            "pm_count": pm_count,
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        })
        while len(self._queue) > self.delay:
            msg = self._queue.popleft()
            msg["measured_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            msg["label_delay_wafers"] = self.delay
            self.producer.produce(
                topic=self.topic,
                key=msg["wafer_id"].encode("utf-8"),
                value=json.dumps(msg, ensure_ascii=False).encode("utf-8"),
            )
