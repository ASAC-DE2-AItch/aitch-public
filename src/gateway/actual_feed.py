# -*- coding: utf-8 -*-
"""P6-2 — 실측 C65 지연 피드백 릴레이 코어: fdc.actual 구독 → 링버퍼 (REST + SSE).

역할: 시뮬레이터가 label_delay 경과 후 발행하는 fdc.actual(계약 §1-B, wafer 1건 =
실측 C65 1개)을 백그라운드 스레드로 구독해 그대로 링버퍼에 적재한다. raw_feed와 달리
**집계·σ 환산이 없다** — fdc.actual은 이미 wafer 단위 확정 라벨이므로 순수 릴레이.

⚠️ label_delay 기본 13,900 wafer(≈2개월). 시나리오 1회 실행 중에는 실측이 거의
도착하지 않는다 — 본 피드는 A가 발행자(actual_publisher)를 활성화하면 자동으로
데이터를 흘리는 **아키텍처 완결용 순배선**이다. 미발행 시 빈 스트림으로 무해 degrade.

원칙 (raw_feed·prediction_feed와 동일):
  · 읽기 전용 소비 — 판정 없음 (Dashboard 소비자, 헌법 1-2 우회 아님: fdc.alert 미관여)
  · 역직렬화 실패 = 스킵 + 로그 (헌법 6-2) · graceful stop · Kafka 미가용에도 생존
  · offset: earliest — 실측은 저물량·고가치(놓치면 안 됨)라 접속 후만이 아닌 전량 소비
    (raw의 latest와 다른 이유 = prediction_feed와 동일 판단)
  · Consumer Group = consumer-group-dashboard-actual (계약 §0 등재 — 본 커밋 문서 동기)
  · 누수 가드(헌법 1-3)는 발행측·A-sink 책임 — 릴레이는 표시 전용, actual_c65를 Feature화 안 함
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger("actual-feed")

TOPIC = "fdc.actual"
CONSUMER_GROUP = "consumer-group-dashboard-actual"
GLOBAL_MAX = 400          # 최근 실측 wafer (저물량이라 raw보다 작게)
PER_CHAMBER_MAX = 120
RETRY_BASE_SEC = 2.0
RETRY_MAX_SEC = 30.0

#: 계약 §1-B 최소 요건 — 누락 시 스킵
REQUIRED_FIELDS = ("wafer_id", "actual_c65")
#: 릴레이 시 보존 필드 (계약 §1-B 페이로드) — 그 외 키는 통과시키지 않음(표시 계약 고정)
RELAY_FIELDS = ("wafer_id", "lot_id", "chamber_id", "actual_c65", "pm_count",
                "processed_at", "measured_at", "label_delay_wafers")


class ActualFeed:
    """fdc.actual → 실측 라벨 링버퍼 (REST recent + SSE since 커서). 순수 릴레이."""

    def __init__(self, bootstrap: Optional[str] = None,
                 consumer_factory: Optional[Callable] = None):
        """의존성 주입 지점 — 미지정 시 env 폴백. consumer_factory는 테스트 대역용."""
        self.bootstrap = bootstrap or os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
        self._factory = consumer_factory or self._default_factory
        self._items: deque = deque(maxlen=GLOBAL_MAX)
        self._by_chamber: dict[str, deque] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._last_measured_at: Optional[str] = None
        self._skipped = 0
        self._seen = 0

    # ---- Kafka -----------------------------------------------------------------
    def _default_factory(self):
        """실 consumer 생성 — 테스트에서는 factory 주입 (raw_feed와 동일 패턴)."""
        from confluent_kafka import Consumer
        c = Consumer({
            "bootstrap.servers": self.bootstrap,
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "earliest",   # 실측 저물량·고가치 — 전량 소비
            "enable.auto.commit": False,
        })
        c.subscribe([TOPIC])
        return c

    # ---- 수명주기 ----------------------------------------------------------------
    def start(self) -> None:
        """소비 스레드 기동 (daemon) — 이미 살아 있으면 no-op (중복 기동 방지)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="actual-feed")
        self._thread.start()

    def stop(self) -> None:
        """graceful 종료 (헌법 6-2)."""
        self._stop_ev.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        """소비 루프 — 생성 실패·루프 예외 모두 백오프 재시도 (게이트웨이 생존, 헌법 6-2)."""
        delay = RETRY_BASE_SEC
        while not self._stop_ev.is_set():
            try:
                consumer = self._factory()
                self._connected = True
            except Exception as e:                      # noqa: BLE001
                self._connected = False
                log.warning(f"fdc.actual 소비자 생성 실패 — {delay:.0f}s 후 재시도: {e}")
                if self._stop_ev.wait(delay):
                    return
                delay = min(delay * 2, RETRY_MAX_SEC)
                continue
            delay = RETRY_BASE_SEC
            try:
                while not self._stop_ev.is_set():
                    msg = consumer.poll(1.0)
                    if msg is None:
                        continue
                    if msg.error():
                        log.warning(f"Kafka 에러: {msg.error()}")
                        continue
                    self.ingest(msg.value())
            except Exception as e:                      # noqa: BLE001
                self._connected = False
                log.warning(f"소비 루프 예외 — 재접속: {e}")
            finally:
                try:
                    consumer.close()
                except Exception:                       # noqa: BLE001
                    pass

    # ---- 적재 --------------------------------------------------------------------
    def ingest(self, raw) -> bool:
        """실측 1건 반영. 역직렬화·필수필드 실패 = 스킵+로그 (헌법 6-2)."""
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise ValueError("dict 아님")
        except Exception as e:                          # noqa: BLE001
            self._skipped += 1
            log.warning(f"역직렬화 실패 — 스킵(누적 {self._skipped}): {e}")
            return False
        missing = [k for k in REQUIRED_FIELDS if k not in d]
        if missing:
            self._skipped += 1
            log.warning(f"필수 필드 누락 {missing} — 스킵(누적 {self._skipped})")
            return False

        item = {k: d.get(k) for k in RELAY_FIELDS}
        ch = str(d.get("chamber_id") or "")
        with self._lock:
            self._seq += 1
            item["_seq"] = self._seq
            self._items.append(item)
            if ch:
                self._by_chamber.setdefault(ch, deque(maxlen=PER_CHAMBER_MAX)).append(item)
            self._last_measured_at = d.get("measured_at") or self._last_measured_at
            self._seen += 1
        return True

    # ---- 조회 (raw_feed와 동일 시그니처) --------------------------------------------
    def recent(self, n: int = 40, chamber: Optional[str] = None) -> list[dict]:
        """최근 n장 — 최신 우선. chamber 지정 시 해당 챔버 버킷."""
        n = max(1, min(int(n), GLOBAL_MAX))
        with self._lock:
            src = self._by_chamber.get(chamber, deque()) if chamber else self._items
            return list(src)[-n:][::-1]

    def since(self, seq: int, limit: int = 200) -> list[dict]:
        """seq 이후 신규 — 오래된 것부터 (SSE append 순서)."""
        with self._lock:
            return [d for d in self._items if d["_seq"] > seq][:limit]

    def head_seq(self) -> int:
        """현재 시퀀스 커서 — SSE 접속 시점 기준값 (since()와 짝)."""
        with self._lock:
            return self._seq

    def status(self) -> dict:
        """상태 요약 — /actual/recent 응답 동봉 (실측 도착 배지 판단 소스)."""
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "connected": self._connected,
                "count": len(self._items),
                "seen": self._seen,
                "chambers": sorted(self._by_chamber.keys()),
                "last_measured_at": self._last_measured_at,
                "skipped": self._skipped,
                "bootstrap": self.bootstrap,
            }
