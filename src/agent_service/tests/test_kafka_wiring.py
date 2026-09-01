"""kafka 라이브 배선 — consumer 방출·스킵 + Brief→agent_reports 매핑 (C4-1 kafka 경로).

실 Kafka·Postgres 없이 검증 가능한 부분만: 소비 루프의 방출·스킵·graceful stop 로직과
brief_to_row 의 컬럼 매핑(supervisor_adapter.fetch_bundle 이 되읽는 형태와의 정합).
실 브로커·DB 통합(오프셋 커밋·실적재)은 docker E2E 몫.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app import source as S  # noqa: E402
from agent_service.app.config import load_settings  # noqa: E402
from agent_service.app.report_writer import brief_to_row  # noqa: E402
from agent_service.app.schemas.report import (  # noqa: E402
    SupervisorBrief,
    SupervisorRecommendation,
    WaferDisposition,
    WaferVerdict,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"


# --- fake kafka-python ConsumerRecord/Consumer ---------------------------------
class _Rec:
    """kafka-python ConsumerRecord 대역 — value(bytes)·topic·partition·offset."""

    def __init__(self, value: bytes | None, *, topic="fdc.alert", partition=0, offset=0):
        self.value, self.topic, self.partition, self.offset = value, topic, partition, offset


class _TP:
    """poll() 반환 dict 의 키 대역 (TopicPartition)."""

    def __init__(self, topic="fdc.alert", partition=0):
        self.topic, self.partition = topic, partition


class _Consumer:
    """poll(timeout_ms) 이 미리 넣은 레코드를 한 배치로 돌려주고, 소진되면 빈 dict."""

    def __init__(self, recs):
        self._batches = [{_TP(): [r]} for r in recs]  # 레코드당 한 배치(순서 보존)
        self.committed = []
        self.closed = False

    def poll(self, timeout_ms=0):
        return self._batches.pop(0) if self._batches else {}

    def commit(self, offsets):
        self.committed.append(offsets)

    def close(self):
        # run_kafka 의 finally 가 부른다 — graceful shutdown 에서 그룹을 정상 이탈하는 경로(6-2).
        self.closed = True

    @property
    def _drained(self):
        return not self._batches


def _alert_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_text(encoding="utf-8").encode("utf-8")


def _drain(consumer, max_iter=50):
    """iter_kafka_alerts 를 유한 횟수 돌려 (alert, record) 리스트로 모은다."""
    out = []
    gen = S.iter_kafka_alerts(consumer, poll_timeout_ms=0,
                              should_stop=lambda: len(out) >= max_iter or consumer._drained)
    for alert, record in gen:
        out.append((alert, record))
    return out


# --- consumer 방출/스킵 --------------------------------------------------------
def test_valid_alert_is_emitted_with_msg():
    """정상 alert → (AlertModel, msg) 방출. msg 는 오프셋 커밋용이라 항상 함께 온다."""
    c = _Consumer([_Rec(_alert_bytes("02_baseline_aging_gate_edge.json"))])
    out = _drain(c)
    assert len(out) == 1
    alert, msg = out[0]
    assert alert is not None and alert.chamber_id.startswith("SIM_CH_")
    assert msg is not None


def test_broken_alert_skips_but_still_yields_msg():
    """손상 메시지(스키마 위반)는 alert=None 이지만 msg 는 방출 — poison 이 오프셋을 막지 않게."""
    c = _Consumer([_Rec(_alert_bytes("46_edge_BROKEN.json"))])
    out = _drain(c)
    assert len(out) == 1
    alert, msg = out[0]
    assert alert is None      # 파싱 실패 → 스킵
    assert msg is not None    # 그래도 커밋 대상은 넘어온다


def test_malformed_json_skips_but_yields_msg():
    """JSON 자체가 깨져도 동일 — decode/파싱 실패는 alert=None + msg 방출."""
    c = _Consumer([_Rec(b"{not json")])
    out = _drain(c)
    assert out and out[0][0] is None and out[0][1] is not None


def test_undecodable_bytes_skip():
    """UTF-8 디코드 실패도 스킵 경로."""
    c = _Consumer([_Rec(b"\xff\xfe\x00")])
    out = _drain(c)
    assert out and out[0][0] is None


def test_should_stop_ends_loop():
    """graceful shutdown — should_stop()=True 면 즉시 종료(6-2)."""
    c = _Consumer([_Rec(_alert_bytes("02_baseline_aging_gate_edge.json"))])
    got = list(S.iter_kafka_alerts(c, poll_timeout_ms=0, should_stop=lambda: True))
    assert got == []


def test_topic_is_fdc_alert_single_channel():
    """구독 토픽은 fdc.alert 단일 채널(헌법 1-2) — kafka-python 은 생성 시 토픽 지정."""
    assert S.KAFKA_TOPIC_ALERT == "fdc.alert"


# --- run_kafka 소비 루프 (카운터·실패 격리·오프셋 전진) --------------------------
# 2026-07-28 추가: 그동안 run_kafka 자체는 테스트가 없었다(source.py 만 커버).
# 실 브로커·DB 없이, 그루퍼·파이프라인·적재를 대역으로 바꿔 루프 계약만 검증한다.
class _FakeTopicPartition:
    """kafka.TopicPartition 대역 — (topic, partition) 값 홀더."""

    def __init__(self, topic, partition):
        self.topic, self.partition = topic, partition

    def __hash__(self):
        return hash((self.topic, self.partition))

    def __eq__(self, other):
        return (self.topic, self.partition) == (other.topic, other.partition)


class _FakeOffsetAndMetadata:
    """kafka.structs.OffsetAndMetadata 대역 — offset 검증에 쓰인다."""

    def __init__(self, offset, metadata=None, leader_epoch=-1):
        self.offset, self.metadata, self.leader_epoch = offset, metadata, leader_epoch


def _install_fake_kafka(monkeypatch):
    """`kafka` 모듈이 없으면 대역을 sys.modules 에 심는다.

    CI 유닛 게이트는 의존성을 **최소분만** 설치한다(`pytest pyyaml pandas pydantic httpx`) —
    `kafka-python` 은 없다. 그런데 `run_kafka` 는 함수 첫 줄에서 `TopicPartition`·
    `OffsetAndMetadata` 를 import 하므로 그대로 두면 CI 에서 ImportError 로 죽는다.

    `importorskip` 으로 건너뛰지 않는 이유: 이 테스트들이 검증하는 건 **소비 루프의 계약**
    (카운터 분리·실패해도 오프셋 전진·실패가 루프를 죽이지 않음)이고 실 브로커가 필요 없다.
    CI 에서 스킵하면 남이 그 계약을 깨도 게이트가 안 잡는다 — "조용한 실패"가 된다.
    쓰는 것은 값 홀더 두 개뿐이라 대역으로 충분하다. monkeypatch 라 테스트 종료 시 복구된다.
    """
    import sys
    import types

    try:
        import kafka  # noqa: F401  — 실물이 있으면 그대로 쓴다(로컬)
        return
    except ImportError:
        pass
    fake = types.ModuleType("kafka")
    fake.TopicPartition = _FakeTopicPartition
    structs = types.ModuleType("kafka.structs")
    structs.OffsetAndMetadata = _FakeOffsetAndMetadata
    fake.structs = structs
    monkeypatch.setitem(sys.modules, "kafka", fake)
    monkeypatch.setitem(sys.modules, "kafka.structs", structs)


def _run_kafka_with(monkeypatch, recs, *, grouper_ret="INC-1", pipeline=None):
    """run_kafka 를 fake consumer/grouper/pipeline 으로 돌려 (summary, consumer) 를 돌려준다.

    pipeline: async (alert, settings, *, incident_id) -> (reports, brief). None 이면 항상 성공 대역.
    """
    import asyncio

    _install_fake_kafka(monkeypatch)
    from agent_service.app import run as R

    consumer = _Consumer(recs)

    class _FakeGrouper:
        def handle_alert(self, _payload):
            return grouper_ret          # None 이면 중복(idempotent 스킵)

    class _FakeConn:
        def close(self):
            self.closed = True

    async def _ok(_alert, _settings, *, incident_id):
        return None, _brief()

    monkeypatch.setattr(R, "_build_grouper", lambda: (_FakeGrouper(), _FakeConn()))
    monkeypatch.setattr(R, "handle_alert_with_brief", pipeline or _ok)
    # 시그니처가 (brief, reports=None) 로 늘었다 (2026-08-03 정량 필드 적재).
    # 시임이 인자 수를 고정하면 본체 변경이 여기서 TypeError 로 터진다 — 넉넉히 받는다.
    monkeypatch.setattr(R, "save_brief", lambda _b, _r=None: True)
    monkeypatch.setattr("agent_service.app.source.make_kafka_consumer", lambda: consumer)
    # fake consumer 는 소진되면 빈 dict 를 돌려주므로 should_stop 없이는 무한 루프가 된다
    # → 소진 시 멈추도록 감싼다(실 consumer 는 시그널로 멈춘다).
    real_iter = S.iter_kafka_alerts
    monkeypatch.setattr(
        "agent_service.app.source.iter_kafka_alerts",
        lambda c, **kw: real_iter(c, poll_timeout_ms=0, should_stop=lambda: c._drained),
    )
    # settings 를 실물로 넘긴다 — run_kafka 가 poll 대기(params D25)를 require 로 읽으므로
    # 빈 object() 로는 못 돈다. 이 테스트가 보는 축(카운터·오프셋)과는 무관한 주입이다.
    return asyncio.run(R.run_kafka(load_settings())), consumer


def test_run_kafka_counts_skipped_separately(monkeypatch):
    """손상 메시지는 consumed 가 아니라 skipped 로 센다 — 섞으면 지표로 못 쓴다."""
    recs = [_Rec(_alert_bytes("03_baseline_aging_low.json"), offset=0),
            _Rec(b"{not json", offset=1)]
    summary, _ = _run_kafka_with(monkeypatch, recs)
    assert summary["consumed"] == 1 and summary["skipped"] == 1
    assert summary["failed"] == 0


def test_run_kafka_advances_offset_even_on_failure(monkeypatch):
    """처리 예외에도 오프셋은 전진한다(무한 재시도 방지) — 대신 failed 로 센다.

    ⚠️ 이 alert 은 재시도되지 않는다. incident 행은 남으므로 수동 재분석으로 복구한다.
    """
    recs = [_Rec(_alert_bytes("03_baseline_aging_low.json"), offset=7)]

    async def _boom(_alert, _settings, *, incident_id):
        raise RuntimeError("LLM 500")

    summary, consumer = _run_kafka_with(monkeypatch, recs, pipeline=_boom)
    assert summary["failed"] == 1 and summary["briefs"] == 0
    assert len(consumer.committed) == 1                      # 실패해도 커밋됐다
    committed = list(consumer.committed[0].values())[0]
    assert committed.offset == 8                             # record.offset + 1


def test_run_kafka_failure_does_not_kill_loop(monkeypatch):
    """한 건이 실패해도 다음 alert 을 계속 처리한다 (헌법 6-2 무중단)."""
    recs = [_Rec(_alert_bytes("03_baseline_aging_low.json"), offset=0),
            _Rec(_alert_bytes("02_baseline_aging_gate_edge.json"), offset=1)]
    calls = {"n": 0}

    async def _flaky(_alert, _settings, *, incident_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("일시 장애")
        return None, _brief()

    summary, consumer = _run_kafka_with(monkeypatch, recs, pipeline=_flaky)
    assert summary["failed"] == 1 and summary["briefs"] == 1  # 1건 실패·1건 성공
    assert len(consumer.committed) == 2                       # 둘 다 오프셋 전진
    assert consumer.closed                                    # graceful 종료로 그룹 이탈


def test_run_kafka_duplicate_alert_is_not_grouped(monkeypatch):
    """그루퍼가 None(중복)이면 파이프라인을 돌리지 않는다 — Incident 단위 처리(1-4)."""
    recs = [_Rec(_alert_bytes("03_baseline_aging_low.json"), offset=0)]
    summary, consumer = _run_kafka_with(monkeypatch, recs, grouper_ret=None)
    assert summary["consumed"] == 1 and summary["grouped"] == 0
    assert summary["briefs"] == 0 and summary["failed"] == 0
    assert len(consumer.committed) == 1                       # 중복도 오프셋은 전진


# --- Brief → agent_reports 매핑 ------------------------------------------------
def _brief() -> SupervisorBrief:
    return SupervisorBrief(
        report_id="SUP-20260713-SIMCH2-0002", incident_id="INC-20260713-SIMCH2-001",
        chamber_id="SIM_CH_2", context_score=72,
        suspected_root_causes=["PM 후 장기 경과 [SPC]"],
        parallel_options={
            "recipe_option": {"report_id": "RCP-20260713-SIMCH2-0002", "confidence": 0.55},
            "limit_option": {"report_id": "LIM-20260713-SIMCH2-0002", "confidence": 0.65},
            "manual_option": {"report_id": "MNT-20260713-SIMCH2-0002", "confidence": 0.56},
        },
        supervisor_recommendation=SupervisorRecommendation(
            decision_frame="4지선다", selected="limit_option", verdict="baseline_aging",
            reason="선이 낡음", confidence=0.65,
            evidence=["N3 추세 [SPC]", "TTTM 갭 낮음 [SPC]", "재설정 사례 [CASE]"],
            counter_evidence="급변이었다면 정비"),
        wafer_disposition=WaferDisposition(
            held_wafers=["C64_1001"], recommendation="RELEASE", reason="예측 정상",
            per_wafer=[WaferVerdict(wafer_id="C64_1001", recommendation="RELEASE",
                                    reason="predicted_c65 715.2 ≤ B9 1572", predicted_c65=715.2)]),
    )


def test_brief_row_has_all_agent_reports_columns():
    """fetch_bundle(supervisor_adapter) 이 읽는 컬럼을 모두 채운다."""
    row = brief_to_row(_brief())
    expected = {
        "report_id", "incident_id", "chamber_id", "severity_score", "suspected_root_causes",
        "recipe_option", "limit_option", "manual_option",
        "supervisor_recommendation", "supervisor_reason", "supervisor_confidence",
        # 2026-07-31 신설 — verdict·근거·반증이 적재에서 유실돼 S4 가 "—" 로 비던 갭 (mig 0002)
        "supervisor_verdict", "supervisor_evidence", "supervisor_counter",
        "disposition_recommendation",
    }
    assert set(row) == expected


def test_brief_row_scalar_fields_match_fetch_bundle():
    """개별 컬럼 = fetch_bundle 이 supervisor_recommendation 조립에 쓰는 값과 정합."""
    row = brief_to_row(_brief())
    assert row["report_id"] == "SUP-20260713-SIMCH2-0002"      # SUP- 접두 → 그래프가 집어감
    assert row["incident_id"] == "INC-20260713-SIMCH2-001"
    assert row["chamber_id"] == "SIM_CH_2"
    assert row["severity_score"] == 72                          # context_score → severity_score
    assert row["supervisor_recommendation"] == "limit_option"   # selected
    assert row["supervisor_reason"] == "선이 낡음"
    assert row["supervisor_confidence"] == 0.65


def test_brief_row_options_are_jsonb_strings():
    """옵션 3종·처분·원인은 JSONB 문자열로 직렬화(psycopg 파라미터)."""
    row = brief_to_row(_brief())
    for col in ("recipe_option", "limit_option", "manual_option",
                "disposition_recommendation", "suspected_root_causes"):
        assert isinstance(row[col], str)                        # None 아님 — 값이 있으므로
        json.loads(row[col])                                    # 유효 JSON

    lim = json.loads(row["limit_option"])
    assert lim["confidence"] == 0.65
    disp = json.loads(row["disposition_recommendation"])
    assert disp["per_wafer"][0]["wafer_id"] == "C64_1001"


def test_brief_row_none_option_is_sql_null():
    """옵션이 없으면 빈 dict 가 아니라 SQL NULL(None) — fetch_bundle 이 `or {}` 로 흡수."""
    b = _brief()
    b = b.model_copy(update={"parallel_options": {
        k: v for k, v in b.parallel_options.items() if k != "recipe_option"}})
    row = brief_to_row(b)
    assert row["recipe_option"] is None
    assert row["limit_option"] is not None
