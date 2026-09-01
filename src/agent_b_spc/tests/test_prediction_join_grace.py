"""예측 조인 유예 (prediction join grace) 단위 테스트.

스펙: `specs/예측조인유예_스펙플랜.md`.

계약:
  · 캐시 hit → 유예 큐 미경유(지연 0 회귀 방지)
  · miss → 큐 적재 → 다음 드레인에서 예측 도착 시 발행
  · 상한 초과 → 스텁 발행(현행 동작으로 낙하) + expired 카운터
  · 커밋 상한이 유예 wafer 를 앞지르지 않는다(발행 전 커밋 = 조용한 유실)
  · 챔버 내 발행 순서 = wafer 순서
  · grace_ms=0 → 완전 현행 동작 (킬 스위치)
"""
from __future__ import annotations

import pytest

from src.agent_b_spc import spc_consumer as sc


class _Clock:
    """주입형 monotonic — 유예 만료를 시간 조작으로 검증한다."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


class _Cache:
    """예측 캐시 스텁 — `has` 집합으로 hit/miss 를 조작한다."""

    def __init__(self, has=()):
        self.has = set(has)

    def get(self, wafer_id):
        return {"predicted_c65": 1.0} if wafer_id in self.has else None

    def upsert(self, row):
        self.has.add(row["wafer_id"])

    def purge_expired(self):
        return 0


def _consumer(grace_ms=800, pending_max=500, has=(), clock=None):
    """유예 설정을 주입한 컨슈머 — Kafka 없이 순수 경로만 쓴다."""
    clock = clock or _Clock()
    c = sc.SpcConsumer.__new__(sc.SpcConsumer)          # __init__ 우회(Kafka·config 의존 회피)
    c.clock = clock
    c.collector = lambda item: c._emitted.append(item)
    c._emitted = []
    c.prediction_cache = _Cache(has)
    c._join_grace_sec = grace_ms / 1000.0
    c._join_pending_max = pending_max
    from collections import defaultdict, deque
    c._pending_join = defaultdict(deque)
    c._join_expired_warned = 0
    c.buffers = {}
    c.stats = {"collector_failed": 0, "join_grace_enqueued": 0, "join_grace_resolved": 0,
               "join_grace_expired": 0, "join_grace_overflow": 0}
    return c, clock


def _item(wafer_id, chamber="SIM_CH_1"):
    """collector 6-튜플 (wafer_id, chamber_id, ts, violations, tttm, recipe_id)."""
    return (wafer_id, chamber, "2026-08-11T00:00:00+00:00", [], None, "C6_0")


# ── 즉시 경로 (회귀 방지) ────────────────────────────────────────────────────

def test_hit_publishes_immediately_without_queue():
    """예측이 이미 있으면 유예 큐를 **거치지 않는다** — 지연 0 유지."""
    c, _ = _consumer(has={"W1"})
    assert c._defer_for_prediction("SIM_CH_1", "W1", _item("W1")) is False
    assert c._pending_join_len() == 0
    assert c.stats["join_grace_enqueued"] == 0


def test_grace_zero_is_kill_switch():
    """grace_ms=0 → 결측이어도 유예하지 않는다(완전 현행 동작)."""
    c, _ = _consumer(grace_ms=0)
    assert c._defer_for_prediction("SIM_CH_1", "W1", _item("W1")) is False
    assert c._pending_join_len() == 0


# ── 유예 경로 ────────────────────────────────────────────────────────────────

def test_miss_enqueues_and_resolves_on_arrival():
    """miss → 큐 적재 → 예측 도착 → 드레인이 발행하고 resolved 증가."""
    c, _ = _consumer()
    assert c._defer_for_prediction("SIM_CH_1", "W1", _item("W1")) is True
    assert c._pending_join_len() == 1

    c._drain_pending_join()                     # 아직 미도착 → 그대로 대기
    assert c._emitted == [] and c._pending_join_len() == 1

    c.prediction_cache.upsert({"wafer_id": "W1"})   # 예측 도착
    c._drain_pending_join()
    assert [e[0] for e in c._emitted] == ["W1"]
    assert c.stats["join_grace_resolved"] == 1 and c.stats["join_grace_expired"] == 0
    assert c._pending_join_len() == 0


def test_expired_falls_back_to_stub_publish():
    """상한 초과 → 예측 없이 발행(현행 동작 낙하) + expired 카운터."""
    c, clock = _consumer(grace_ms=800)
    c._defer_for_prediction("SIM_CH_1", "W1", _item("W1"))
    clock.advance(0.9)
    c._drain_pending_join()
    assert [e[0] for e in c._emitted] == ["W1"]     # 알람을 버리지 않는다
    assert c.stats["join_grace_expired"] == 1 and c.stats["join_grace_resolved"] == 0


def test_overflow_publishes_without_grace():
    """큐 상한 초과 → 유예 없이 즉시 발행(메모리 가드)."""
    c, _ = _consumer(pending_max=1)
    assert c._defer_for_prediction("SIM_CH_1", "W1", _item("W1")) is True
    assert c._defer_for_prediction("SIM_CH_1", "W2", _item("W2")) is False   # 상한 도달
    assert c.stats["join_grace_overflow"] == 1


# ── 순서·종료 ────────────────────────────────────────────────────────────────

def test_chamber_order_preserved():
    """챔버 내에서 앞이 안 풀리면 뒤도 대기 — 발행 순서 = wafer 순서."""
    c, _ = _consumer()
    c._defer_for_prediction("SIM_CH_1", "W1", _item("W1"))
    c._defer_for_prediction("SIM_CH_1", "W2", _item("W2"))
    c.prediction_cache.upsert({"wafer_id": "W2"})   # 뒤엣것만 도착
    c._drain_pending_join()
    assert c._emitted == []                         # W2 먼저 나가면 안 된다
    c.prediction_cache.upsert({"wafer_id": "W1"})
    c._drain_pending_join()
    assert [e[0] for e in c._emitted] == ["W1", "W2"]


def test_force_drain_flushes_everything():
    """종료 경로(force) — 상한 전이어도 전부 발행. 안 하면 유예분이 사라진다."""
    c, _ = _consumer()
    c._defer_for_prediction("SIM_CH_1", "W1", _item("W1"))
    c._defer_for_prediction("SIM_CH_2", "W2", _item("W2", "SIM_CH_2"))
    c._drain_pending_join(force=True)
    assert sorted(e[0] for e in c._emitted) == ["W1", "W2"]
    assert c._pending_join_len() == 0


def test_collector_exception_is_isolated():
    """발행 실패가 드레인 루프를 죽이지 않는다(§4-1 P1 예외 격리)."""
    c, _ = _consumer()

    def boom(item):
        raise RuntimeError("collector down")

    c.collector = boom
    c._defer_for_prediction("SIM_CH_1", "W1", _item("W1"))
    c._drain_pending_join(force=True)               # 예외가 새면 여기서 실패
    assert c.stats["collector_failed"] == 1
    assert c._pending_join_len() == 0                # 큐에서는 빠진다(무한 재시도 방지)


# ── 커밋 홀드 ────────────────────────────────────────────────────────────────

def test_commit_holds_for_pending_join():
    """커밋 상한이 유예 wafer 의 first offset 을 앞지르지 않는다.

    발행 전에 커밋하면 그 사이 크래시 시 알람이 **조용히 유실**된다(ⓑ D16 과 같은 취지).
    """
    c, _ = _consumer()
    c._pending_failed = {}
    c._qual_buf = {}
    c._commit_stall_warned = {}
    c._last_committed_off = -1
    c._last_processed = {}
    c.stats["flush_fail"] = 0
    committed = []
    c.consumer = type("F", (), {"commit": lambda self, offsets=None, **kw: committed.append(offsets)})()
    c.offset_factory = lambda t, p, o, m=None: [(t, p, o)]

    c.buffers["SIM_CH_1"] = {"rows": [], "first_msg": ("fdc.raw", 0, 100)}
    c._pending_join["SIM_CH_1"].append(
        {"item": _item("W1"), "wafer_id": "W1", "deadline": 9e9,
         "first_msg": ("fdc.raw", 0, 100)})

    c._commit(("fdc.raw", 0, 500))
    assert committed, "커밋이 호출되지 않았다"
    off = committed[-1][0][2]
    assert off <= 99, f"유예 wafer(offset 100)를 앞질러 커밋됨: {off}"
