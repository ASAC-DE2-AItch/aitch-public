# -*- coding: utf-8 -*-
"""W6-① — S1 센서 곡선 실배선 코어: fdc.raw 구독 → wafer 단위 집계 → 링버퍼.

역할: 시뮬레이터가 발행하는 fdc.raw(3초 샘플 row, 계약 §1)를 백그라운드 스레드로
구독해 **wafer 1장 = 점 1개**로 집계한다 (표시 센서 C4·C11·C17 — useLiveFdc
SENSOR_KEYS와 일치). σ 환산은 control_limits(현행 관리선)를 참조해 게이트웨이가
수행 — 프론트는 σ값을 그대로 그린다 (관리선 재설정 시 새 버전 기준으로 자동 정규화
= F3의 σ-공간 문법).

집계 규칙:
  · settled 행만 (stabilization_flag == 0) — 관리선 산출 창과 동일 (sensor_window='settled')
  · wafer 경계 = 같은 챔버에서 새 wafer_id 도착 (계약 §1: 키=chamber_id → 챔버 내 순서 보장)
  · 마지막 wafer는 STALE_FLUSH_SEC 무입력 시 강제 마감 (다음 wafer가 안 와도 점이 뜬다)
  · σ = (settled 평균 − center) / sigma — 그룹 매칭: (chamber, recipe, sensor, 'settled')의
    is_active 행. 같은 키에 스텝이 여러 개면 해당 스텝 bucket 우선, 없으면 전 스텝 평균.
  · 관리선 캐시 TTL LIMITS_TTL_SEC — 승인→갱신(B apply) 후 다음 wafer부터 새 버전 반영

원칙 (prediction_feed.py와 동일):
  · 읽기 전용 소비 — 판정 없음 (Dashboard 소비자)
  · 역직렬화 실패 = 스킵 + 로그 (헌법 6-2) · graceful stop · Kafka/DB 미가용에도 생존
  · offset: enable.auto.commit=false + latest — raw는 물량이 커서(row 단위) 전체 재생 대신
    접속 시점 이후만 소비 (이력은 어차피 최근 N wafer만 표시. prediction_feed의 earliest와
    다른 이유를 여기 명시)
  · Consumer Group = consumer-group-dashboard-raw (계약 §0 등재 필요 — 본 PR에서 문서 동기)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger("raw-feed")

TOPIC = "fdc.raw"
CONSUMER_GROUP = "consumer-group-dashboard-raw"
DISPLAY_SENSORS = tuple(
    s.strip() for s in os.getenv("RAW_FEED_SENSORS", "C4,C11,C17").split(",") if s.strip())
GLOBAL_MAX = 600          # 전역 최근 wafer 점 (4챔버 × 40점 + 여유)
PER_CHAMBER_MAX = 80      # 챔버별 (S1 차트 40점 + 여유)
RETRY_BASE_SEC = 2.0
RETRY_MAX_SEC = 30.0
STALE_FLUSH_SEC = 5.0     # 마지막 wafer 강제 마감 대기
LIMITS_TTL_SEC = 5.0      # 관리선 캐시 갱신 주기 (승인 직후 다음 wafer부터 새 버전)
# σ 환산 대표 스텝 — 같은 센서에 스텝별 그룹이 여럿일 때(예: C17 스텝 1·4·5·6·7)의 선택 규칙:
# ① PREFER_STEP(기본 4 — S1 주시 문법 "STEP 4 · SETTLED"과 일치) 그룹이 있으면 그 스텝으로 고정
# ② 없으면 그룹 보유 스텝 중 최소 스텝으로 고정 (wafer마다 재선택하지 않음 — 스텝 혼합 방지)
PREFER_STEP = int(os.getenv("RAW_FEED_PREFER_STEP", "4"))

#: 계약 §1 필수 중 집계 최소 요건 — 누락 시 스킵
REQUIRED_FIELDS = ("wafer_id", "chamber_id", "sensors")


class _LimitsCache:
    """control_limits 현행(is_active) 캐시 — (chamber, recipe, sensor) → {step: 그룹}.

    ⚠ 같은 센서에 스텝별 그룹이 여럿 존재한다 (예: CH3 C17 = 스텝 1·4·5·6·7).
    스텝을 무시하고 한 행으로 덮어쓰면 임의 스텝 기준 σ가 나온다 — 실측 사고
    (2026-07-27: C17이 스텝 7 그룹으로 환산돼 +10.7σ 표기). 스텝 선택은 finalize가 한다.
    """

    def __init__(self, dsn: Optional[str]):
        """dsn 보관만 — 접속은 첫 refresh에서 지연 수립. None이면 σ 미환산(eng-only) 모드."""
        self._dsn = dsn
        self._conn = None
        self._rows: dict[tuple, dict] = {}
        self._loaded_at = 0.0
        self._warned = False

    def _ensure_conn(self):
        """지연 접속 — 실패로 버린 커넥션은 다음 호출에서 재수립 (autocommit, 조회 전용)."""
        if self._dsn is None:
            # 지연 재해석 (2026-07-31): 생성 시점(모듈 import)에 DATABASE_URL 이 아직 없던
            # 경우(.env 가 나중에 로드) None 으로 고착되지 않게 매 호출 재확인한다.
            # main._load_env 를 import 시점으로 당겨 근본 해소했지만, 임베드·리로드 등
            # 다른 진입로에서도 σ 환산이 조용히 죽지 않도록 이중 방어로 남긴다.
            self._dsn = os.getenv("DATABASE_URL")
        if self._dsn is None:
            return None
        if self._conn is not None:
            return self._conn
        import psycopg2
        self._conn = psycopg2.connect(self._dsn)
        self._conn.autocommit = True
        return self._conn

    def refresh_if_stale(self) -> None:
        """TTL 경과 시 재로드. DB 미가용은 경고 1회 + 기존 캐시 유지 (게이트웨이 생존)."""
        if time.monotonic() - self._loaded_at < LIMITS_TTL_SEC:
            return
        try:
            conn = self._ensure_conn()
            if conn is None:
                return
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT chamber_id, recipe_id, step, sensor_id, center, sigma,
                              ucl, lcl, limit_version, method
                       FROM control_limits
                       WHERE is_active AND sensor_window = 'settled'
                         AND sensor_id = ANY(%s)""",
                    (list(DISPLAY_SENSORS),))
                rows: dict[tuple, dict] = {}
                for (ch, rcp, step, sid, center, sigma, ucl, lcl, ver, method) in cur.fetchall():
                    rows.setdefault((ch, rcp, sid), {})[int(step)] = {
                        "step": int(step), "center": float(center),
                        "sigma": float(sigma), "ucl": float(ucl), "lcl": float(lcl),
                        "limit_version": str(ver), "method": str(method),
                    }
            self._rows = rows
            self._loaded_at = time.monotonic()
            self._warned = False
        except Exception as e:                          # noqa: BLE001 — 캐시 유지
            self._conn = None                           # 다음 주기 재접속
            self._loaded_at = time.monotonic()          # 미가용 시 폭풍 재시도 방지
            if not self._warned:
                log.warning(f"control_limits 캐시 로드 실패 — σ 미환산으로 진행: {e}")
                self._warned = True

    def groups_for(self, chamber: str, recipe: str, sensor: str) -> Optional[dict]:
        """{step: 그룹} 반환 — 스텝 선택은 호출자(finalize)의 규칙으로."""
        return self._rows.get((chamber, recipe, sensor))


class RawFeed:
    """fdc.raw → wafer 단위 센서 점 링버퍼 (REST recent + SSE since 커서)."""

    def __init__(self, bootstrap: Optional[str] = None,
                 consumer_factory: Optional[Callable] = None,
                 dsn: Optional[str] = None):
        """의존성 주입 지점 — 미지정 시 env 폴백. consumer_factory는 테스트 대역용."""
        self.bootstrap = bootstrap or os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
        self._factory = consumer_factory or self._default_factory
        self._limits = _LimitsCache(dsn if dsn is not None else os.getenv("DATABASE_URL"))
        self._items: deque = deque(maxlen=GLOBAL_MAX)
        self._by_chamber: dict[str, deque] = {}
        self._acc: dict[str, dict] = {}                 # chamber → 집계 중 wafer 상태
        self._seq = 0
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._last_ts: Optional[str] = None
        self._skipped = 0
        self._rows_seen = 0

    # ---- Kafka -----------------------------------------------------------------
    def _default_factory(self):
        """실 consumer 생성 — 테스트에서는 factory 주입 시임 (prediction_feed와 동일)."""
        from confluent_kafka import Consumer
        c = Consumer({
            "bootstrap.servers": self.bootstrap,
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "latest",   # raw는 row 물량이 커서 재생 대신 이후만
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
        self._thread = threading.Thread(target=self._run, daemon=True, name="raw-feed")
        self._thread.start()

    def stop(self) -> None:
        """graceful 종료 (헌법 6-2)."""
        self._stop_ev.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        """소비 루프 본체 — 생성 실패·루프 예외 모두 백오프 재시도 (게이트웨이 생존, 헌법 6-2)."""
        delay = RETRY_BASE_SEC
        while not self._stop_ev.is_set():
            try:
                consumer = self._factory()
                self._connected = True
            except Exception as e:                      # noqa: BLE001
                self._connected = False
                log.warning(f"fdc.raw 소비자 생성 실패 — {delay:.0f}s 후 재시도: {e}")
                if self._stop_ev.wait(delay):
                    return
                delay = min(delay * 2, RETRY_MAX_SEC)
                continue
            delay = RETRY_BASE_SEC
            try:
                while not self._stop_ev.is_set():
                    msg = consumer.poll(1.0)
                    self._flush_stale()
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

    # ---- 집계 --------------------------------------------------------------------
    def ingest(self, raw) -> bool:
        """row 1건 반영. 새 wafer_id 도착 시 직전 wafer 마감(점 방출). 실패 = 스킵+로그."""
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

        ch = str(d["chamber_id"])
        wid = str(d["wafer_id"])
        acc = self._acc.get(ch)
        if acc is not None and acc["wafer_id"] != wid:
            self._finalize(ch)                          # wafer 경계 — 직전 장 마감
            acc = None
        if acc is None:
            acc = {"wafer_id": wid, "recipe_id": str(d.get("recipe_id", "")),
                   "pm_count": d.get("pm_count"), "is_qual": bool(d.get("is_qual", False)),
                   "ts": d.get("timestamp"), "buckets": {}, "touched": time.monotonic()}
            self._acc[ch] = acc
        acc["touched"] = time.monotonic()
        acc["ts"] = d.get("timestamp") or acc["ts"]
        self._rows_seen += 1

        if int(d.get("stabilization_flag", 0) or 0) == 0:      # settled 행만 (관리선 창과 동일)
            sensors = d.get("sensors") or {}
            step = int(d.get("step", 0) or 0)
            for sid in DISPLAY_SENSORS:
                v = sensors.get(sid)
                if v is None:
                    continue
                try:
                    acc["buckets"].setdefault((sid, step), []).append(float(v))
                except (TypeError, ValueError):
                    continue
        return True

    def _flush_stale(self) -> None:
        """마지막 wafer 강제 마감 — 다음 wafer가 안 와도 STALE_FLUSH_SEC 후 점 방출."""
        now = time.monotonic()
        for ch in list(self._acc.keys()):
            if now - self._acc[ch]["touched"] >= STALE_FLUSH_SEC:
                self._finalize(ch)

    def _finalize(self, ch: str) -> None:
        """챔버의 집계 중 wafer를 점으로 확정해 링버퍼에 적재."""
        acc = self._acc.pop(ch, None)
        if acc is None or not acc["buckets"]:
            return
        self._limits.refresh_if_stale()
        out_sensors: dict[str, dict] = {}
        for sid in DISPLAY_SENSORS:
            groups = self._limits.groups_for(ch, acc["recipe_id"], sid)  # {step: grp} | None
            grp = None
            vals = None
            if groups:
                # 대표 스텝 "고정" — PREFER_STEP 그룹이 있으면 그 스텝만, 없으면 최저 스텝 그룹으로
                # 불변 고정. wafer마다 표본 많은 스텝으로 갈아타면 σ·eng 시리즈가 스텝 혼합으로
                # 오염된다 (2026-07-27 실측: C17 도면 eng 172→309 점프 — 스텝별 모집단이 다름).
                # 이 wafer에 대표 스텝 표본이 없으면 sig를 생략한다 (다른 스텝으로 대체하지 않음).
                step_pick = PREFER_STEP if PREFER_STEP in groups else min(groups)
                vals = acc["buckets"].get((sid, step_pick))
                if vals is not None:
                    grp = groups[step_pick]
            if vals is None:                             # 매칭 그룹 없음 → 전 스텝 평균 (eng-only)
                pooled = [v for (s, _st), vs in acc["buckets"].items() if s == sid for v in vs]
                vals = pooled or None
            if vals is None:
                continue
            eng = sum(vals) / len(vals)
            entry: dict = {"eng": round(eng, 4), "n_rows": len(vals),
                           "sig": None, "limit_version": None}
            if grp is not None and grp["sigma"] > 1e-12:
                entry["sig"] = round((eng - grp["center"]) / grp["sigma"], 4)
                entry["limit_version"] = grp["limit_version"]
                entry["method"] = grp["method"]
                entry["step"] = grp["step"]              # 어느 스텝 기준 σ인지 투명화
            out_sensors[sid] = entry
        if not out_sensors:
            return
        item = {"wafer_id": acc["wafer_id"], "chamber_id": ch,
                "recipe_id": acc["recipe_id"], "pm_count": acc["pm_count"],
                "is_qual": acc["is_qual"], "timestamp": acc["ts"], "sensors": out_sensors}
        with self._lock:
            self._seq += 1
            item["_seq"] = self._seq
            self._items.append(item)
            self._by_chamber.setdefault(ch, deque(maxlen=PER_CHAMBER_MAX)).append(item)
            self._last_ts = acc["ts"]

    # ---- 조회 (prediction_feed와 동일 시그니처) -------------------------------------
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
        """상태 요약 — /raw/recent 응답 동봉 (S1 실곡선 배지 판단 소스)."""
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "connected": self._connected,
                "wafers": len(self._items),
                "rows_seen": self._rows_seen,
                "chambers": sorted(self._by_chamber.keys()),
                "last_ts": self._last_ts,
                "skipped": self._skipped,
                "sensors": list(DISPLAY_SENSORS),
                "bootstrap": self.bootstrap,
            }
