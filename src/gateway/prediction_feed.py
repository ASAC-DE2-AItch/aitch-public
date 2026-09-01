# -*- coding: utf-8 -*-
"""P5-4 — S1 실배선 코어: fdc.prediction 구독 → 링버퍼 (fastapi 무의존, 단위 테스트 대상).

역할: A 파이프라인(A4-1)이 발행하는 fdc.prediction을 백그라운드 스레드로 구독해
전역·챔버별 링버퍼에 적재한다. S1(useLiveFdc)의 초기 이력은 REST(recent),
append는 SSE(since 커서)가 이 버퍼를 읽어 공급한다.

원칙:
  · 읽기 전용 소비 — 판정·점수 재계산 없음 (Dashboard 소비자, 계약 §2. SHAP 순위도
    그대로 전달 — 헌법 3-3 독립 채널)
  · 역직렬화 실패 = 스킵 + 로그 (헌법 6-2 — 파이프라인 생존 우선)
  · graceful stop (헌법 6-2) — stop 이벤트 + join
  · Kafka 미가용이어도 gateway를 죽이지 않는다 — 백오프 재시도, status()로 노출
  · offset 정책: enable.auto.commit=false + earliest — 재기동 시 토픽을 처음부터
    재생해 링버퍼(최근 N)만 남긴다. 커밋 오프셋 관리 없이 "부팅 직후에도 이력이
    보이는" 대시보드 특성에 맞춤 (소비 부하 = wafer당 1건이라 재생 비용 무시 가능)
  · Consumer Group = consumer-group-dashboard (계약 §0 등재 — 2026-07-22, 신규 소비자)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from typing import Callable, Optional

log = logging.getLogger("prediction-feed")

TOPIC = "fdc.prediction"
CONSUMER_GROUP = "consumer-group-dashboard"
GLOBAL_MAX = 400          # 전역 최근 예측 (Live Wafer 스트림 + 초기 이력 소스)
PER_CHAMBER_MAX = 80      # 챔버별 (S1 차트 40점 + 여유)
RETRY_BASE_SEC = 2.0      # Kafka 미가용 재시도 백오프 (시작값)
RETRY_MAX_SEC = 30.0

#: 계약 §2 필수 필드 중 표시 최소 요건 — 누락 시 스킵 (chamber_id는 버킷 키로 별도 취급)
REQUIRED_FIELDS = ("wafer_id", "predicted_c65")


class PredictionFeed:
    """fdc.prediction 링버퍼 — 백그라운드 스레드 소비, seq 단조 증가(SSE 커서)."""

    def __init__(self, bootstrap: Optional[str] = None,
                 consumer_factory: Optional[Callable] = None):
        self.bootstrap = bootstrap or os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
        self._factory = consumer_factory or self._default_factory
        self._items: deque = deque(maxlen=GLOBAL_MAX)          # dict (+_seq)
        self._by_chamber: dict[str, deque] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False        # 소비자 생성 성공 여부 (브로커 접속 근사)
        self._last_ts: Optional[str] = None
        self._skipped = 0

    # ---- Kafka ---------------------------------------------------------------
    def _default_factory(self):
        """실 consumer 생성 — 테스트에서는 factory 주입으로 대체하는 시임."""
        from confluent_kafka import Consumer
        c = Consumer({
            "bootstrap.servers": self.bootstrap,
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,   # 재기동 = 전체 재생 → 링버퍼가 최근만 유지
        })
        c.subscribe([TOPIC])
        return c

    # ---- 수명주기 -------------------------------------------------------------
    def start(self) -> None:
        """소비 스레드 기동 (이미 돌고 있으면 no-op)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="prediction-feed")
        self._thread.start()

    def stop(self) -> None:
        """graceful 종료 (헌법 6-2) — 루프 탈출 신호 후 join."""
        self._stop_ev.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        delay = RETRY_BASE_SEC
        while not self._stop_ev.is_set():
            try:
                consumer = self._factory()
                self._connected = True
            except Exception as e:                      # noqa: BLE001 — 게이트웨이 생존 우선
                self._connected = False
                log.warning(f"fdc.prediction 소비자 생성 실패 — {delay:.0f}s 후 재시도: {e}")
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
            except Exception as e:                      # noqa: BLE001 — 루프 붕괴 시 재접속
                self._connected = False
                log.warning(f"소비 루프 예외 — 재접속: {e}")
            finally:
                try:
                    consumer.close()
                except Exception:                       # noqa: BLE001
                    pass

    # ---- 적재 ------------------------------------------------------------------
    def ingest(self, raw) -> bool:
        """메시지 1건 적재. 실패는 스킵+로그 (헌법 6-2). 테스트에서 직접 호출 가능."""
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
        with self._lock:
            self._seq += 1
            d["_seq"] = self._seq
            self._items.append(d)
            ch = str(d.get("chamber_id") or "?")
            self._by_chamber.setdefault(
                ch, deque(maxlen=PER_CHAMBER_MAX)).append(d)
            self._last_ts = d.get("timestamp")
        return True

    # ---- 조회 ------------------------------------------------------------------
    def recent(self, n: int = 40, chamber: Optional[str] = None) -> list[dict]:
        """최근 n건 — 최신 우선. chamber 지정 시 해당 챔버 버킷."""
        n = max(1, min(int(n), GLOBAL_MAX))
        with self._lock:
            src = self._by_chamber.get(chamber, deque()) if chamber else self._items
            return list(src)[-n:][::-1]

    def since(self, seq: int, limit: int = 100) -> list[dict]:
        """seq 이후 신규 항목 — 오래된 것부터 (SSE append 순서)."""
        with self._lock:
            return [d for d in self._items if d["_seq"] > seq][:limit]

    def head_seq(self) -> int:
        """현재 최신 seq — SSE 접속 시 커서 초기값 (이력은 REST 담당)."""
        with self._lock:
            return self._seq

    def status(self) -> dict:
        """상태 요약 — /predictions/recent 응답에 동봉 (S1 GATE 배지 판단 소스)."""
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "connected": self._connected,
                "count": len(self._items),
                "chambers": sorted(self._by_chamber.keys()),
                "last_ts": self._last_ts,
                "skipped": self._skipped,
                "bootstrap": self.bootstrap,
            }
