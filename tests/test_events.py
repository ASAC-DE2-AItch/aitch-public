# -*- coding: utf-8 -*-
"""P4-1 이벤트 라우팅 단위테스트 — route_topic + make_kafka_emit (confluent 불요, fake producer)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orchestrator.events import make_kafka_emit, route_topic  # noqa: E402


def test_route_topic():
    assert route_topic("CorrectionApplied") == "fdc.correction"    # 적용 (계약 §6)
    assert route_topic("WaferDispositioned") == "fdc.correction"   # Event Contract §7 (스크랩/해제)
    assert route_topic("ApprovalRequested") == "fdc.agent"         # 승인 워크플로
    assert route_topic("ApprovalCompleted") == "fdc.agent"
    assert route_topic("Escalated") == "fdc.agent"
    assert route_topic("RejectionLearned") == "fdc.agent"
    assert route_topic("MaintenanceApproved") == "fdc.agent"       # 정비 승인 (2026-08-12 개명 — 의도적 DEFAULT. fdc.correction 이면 회귀)


class _FakeProducer:
    def __init__(self):
        self.sent = []

    def produce(self, topic, key, value):
        self.sent.append((topic, key, value))

    def poll(self, _t):
        pass


def test_kafka_emit_routes_and_serializes():
    p = _FakeProducer()
    emit = make_kafka_emit(p)
    emit("CorrectionApplied", {"incident_id": "INC-1", "correction_type": "limit"})
    emit("ApprovalRequested", {"incident_id": "INC-1"})
    assert [t for (t, k, v) in p.sent] == ["fdc.correction", "fdc.agent"]
    assert p.sent[0][1] == b"INC-1"                               # key = incident_id
    msg = json.loads(p.sent[0][2].decode("utf-8"))
    assert msg["event_type"] == "CorrectionApplied" and msg["correction_type"] == "limit"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"ALL {len(tests)} TESTS PASS")
