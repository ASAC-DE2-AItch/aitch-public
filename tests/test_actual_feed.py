# -*- coding: utf-8 -*-
"""P6-2 actual_feed 단위 테스트 — Kafka·fastapi 무의존 (헌법 6-2 스킵·graceful 검증).

fdc.actual(계약 §1-B)은 wafer 단위 확정 라벨 — 집계·σ 없는 순수 릴레이임을 검증.
실행: python -m pytest tests/test_actual_feed.py -q  (또는 python tests/test_actual_feed.py)
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gateway.actual_feed import ActualFeed  # noqa: E402


def _msg(wafer="C64_1_CH_3", chamber="SIM_CH_3", c65=1234, **over):
    d = {"wafer_id": wafer, "lot_id": "LOT_A", "chamber_id": chamber,
         "actual_c65": c65, "pm_count": 3,
         "processed_at": "2026-07-22T10:00:00.000Z",
         "measured_at": "2026-09-11T09:00:00.000Z", "label_delay_wafers": 13900}
    d.update(over)
    return json.dumps(d).encode("utf-8")


def test_ingest_and_recent():
    f = ActualFeed(consumer_factory=lambda: None)
    assert f.ingest(_msg(wafer="W1")) and f.ingest(_msg(wafer="W2", chamber="SIM_CH_1"))
    r = f.recent(10)
    assert [x["wafer_id"] for x in r] == ["W2", "W1"]          # 최신 우선
    assert all("_seq" in x for x in r)
    ch = f.recent(10, chamber="SIM_CH_1")
    assert [x["wafer_id"] for x in ch] == ["W2"]               # 챔버 버킷 필터


def test_relay_fields_only():
    """RELAY_FIELDS 화이트리스트 — 계약 외 키는 통과 안 함, 필수 키는 보존 (표시 계약 고정)."""
    f = ActualFeed(consumer_factory=lambda: None)
    assert f.ingest(_msg(wafer="W1", sneaky="DROP-ME", actual_c65=999))
    item = f.recent(1)[0]
    assert item["actual_c65"] == 999 and item["measured_at"].startswith("2026-09-11")
    assert "sneaky" not in item                                 # 화이트리스트 밖 = 차단
    assert item["label_delay_wafers"] == 13900


def test_skip_bad_payloads():
    """역직렬화 실패·필수 누락 = 스킵 + 카운트 (헌법 6-2 — 피드 생존)."""
    f = ActualFeed(consumer_factory=lambda: None)
    assert f.ingest(b"not-json{{{") is False
    assert f.ingest(json.dumps(["list"]).encode()) is False
    assert f.ingest(json.dumps({"wafer_id": "W1"}).encode()) is False   # actual_c65 누락
    assert f.status()["skipped"] == 3 and f.status()["count"] == 0


def test_since_cursor():
    f = ActualFeed(consumer_factory=lambda: None)
    for i in range(5):
        f.ingest(_msg(wafer=f"W{i}"))
    head = f.head_seq()
    assert f.since(head) == []                                  # 접속 시점 이후 신규 없음
    f.ingest(_msg(wafer="W-new"))
    got = f.since(head)
    assert [x["wafer_id"] for x in got] == ["W-new"]            # 신규만, 오래된 것부터
    assert len(f.since(0, limit=3)) == 3                        # limit 상한


def test_ring_bounds():
    from gateway import actual_feed as af
    f = ActualFeed(consumer_factory=lambda: None)
    for i in range(af.GLOBAL_MAX + 25):
        f.ingest(_msg(wafer=f"W{i}", chamber="SIM_CH_2"))
    st = f.status()
    assert st["count"] == af.GLOBAL_MAX                         # 전역 링버퍼 상한
    assert len(f.recent(9999, chamber="SIM_CH_2")) == af.PER_CHAMBER_MAX


def test_thread_survives_factory_failure_and_stops():
    """소비자 생성 실패 반복에도 스레드 생존 + graceful stop (헌법 6-2)."""
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("no broker")

    f = ActualFeed(consumer_factory=boom)
    f.start()
    time.sleep(0.05)
    assert f.status()["running"] is True
    f.stop()
    assert f.status()["running"] is False and calls["n"] >= 1


def test_thread_consumes_fake_messages():
    """가짜 consumer 주입 — poll 스크립트 소진 후에도 루프 유지, stop으로 종료."""
    class FakeMsg:
        def __init__(self, v): self._v = v
        def error(self): return None
        def value(self): return self._v

    class FakeConsumer:
        def __init__(self, script): self.script = list(script)
        def poll(self, _t):
            time.sleep(0.005)
            return FakeMsg(self.script.pop(0)) if self.script else None
        def close(self): pass

    f = ActualFeed(consumer_factory=lambda: FakeConsumer(
        [_msg(wafer="A"), b"broken", _msg(wafer="B")]))
    f.start()
    deadline = time.time() + 2
    while time.time() < deadline and f.status()["count"] < 2:
        time.sleep(0.01)
    f.stop()
    st = f.status()
    assert st["count"] == 2 and st["skipped"] == 1
    assert [x["wafer_id"] for x in f.recent(5)] == ["B", "A"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
