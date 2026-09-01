"""spc_consumer 테스트 (D0-2 — fdc.raw 컨슈머 배선).

전부 순수(DB·Kafka 없이): FakeConsumer/주입 clock으로 poll 루프·집계 브리지·팬아웃을 검증한다.
confluent_kafka 미설치 환경에서도 돌아야 하므로 프로덕션 코드의 Kafka import는 지연(lazy)이다.
"""
from __future__ import annotations

import json

import pytest

from src.agent_b_spc import spc_consumer as sc


# ── 테스트 더블 (confluent_kafka Message/Consumer API 미러) ──────────

class FakeMsg:
    """confluent_kafka Message 미러 — value()/topic()/offset()/error()."""

    def __init__(self, value, topic="fdc.raw", offset=0, err=None):
        self._value = value if isinstance(value, (bytes, type(None))) else \
            json.dumps(value).encode("utf-8")
        self._topic, self._offset, self._err = topic, offset, err

    def value(self):
        return self._value

    def topic(self):
        return self._topic

    def offset(self):
        return self._offset

    def partition(self):
        return 0

    def error(self):
        return self._err


class FakeConsumer:
    """poll()로 준비된 메시지를 순서대로 뱉고, 소진되면 on_exhausted 콜백."""

    def __init__(self, msgs=(), on_exhausted=None):
        self.msgs = list(msgs)
        self.on_exhausted = on_exhausted
        self.subscribed: list = []
        self.commits: list = []
        self.closed = False
        self._committed_result: list = []        # committed() 반환(D14 restart 테스트용)

    def committed(self, partitions, timeout=None):   # D14 — 재시작 metadata 조회
        return self._committed_result

    def subscribe(self, topics):
        self.subscribed = list(topics)

    def poll(self, timeout=None):
        if self.msgs:
            return self.msgs.pop(0)
        if self.on_exhausted:
            self.on_exhausted()
        return None

    def commit(self, offsets=None, asynchronous=False):
        self.commits.append(offsets)

    def close(self):
        self.closed = True


def _row(**over):
    """유효한 fdc.raw row 1건 (필수 필드 전부)."""
    row = {
        "wafer_id": "W1_CH_SIM_CH_1", "chamber_id": "SIM_CH_1", "recipe_id": "C6_0",
        "step": 4, "stabilization_flag": 0, "pm_count": 3,
        "timestamp": "2026-07-20T01:00:00.000Z",
        "sensors": {"C11": 100.0, "C12": 1.0},
    }
    row.update(over)
    return row


def _make(msgs=(), **kw):
    """SpcConsumer + FakeConsumer 조립 — 메시지 소진 시 루프 정지.

    `join_grace_ms=0` 기본 — 이 파일의 테스트는 **예측 조인 유예 도입 이전**에 쓰였고
    "collector 가 flush 안에서 즉시 호출된다"를 전제한다. 유예가 켜지면 캐시 miss 인
    wafer 는 큐로 가서 다음 드레인에 발행되므로 그 전제가 깨진다(설계상 정상).
    유예 경로 자체는 `test_prediction_join_grace.py` 가 덮는다.
    """
    holder = {}
    fake = FakeConsumer(msgs, on_exhausted=lambda: holder["c"].request_stop())
    kw.setdefault("join_grace_ms", 0)
    holder["c"] = sc.SpcConsumer(consumer=fake, **kw)
    return holder["c"], fake


# ── D0-2-1: _valid (H2) ─────────────────────────────────────────────

def test_valid_accepts_complete_row():
    """필수 필드가 전부 있는 정상 row는 통과."""
    assert sc._valid(_row()) is True


@pytest.mark.parametrize("field", [
    "wafer_id", "chamber_id", "recipe_id", "step",
    "stabilization_flag", "pm_count", "sensors", "timestamp",
])
def test_valid_rejects_missing_required_field(field):
    """H2: 집계·Point가 하드 요구하는 필드 결측 → skip."""
    row = _row()
    del row[field]
    assert sc._valid(row) is False


@pytest.mark.parametrize("field", ["step", "chamber_id", "pm_count", "stabilization_flag"])
def test_valid_rejects_none_valued_field(field):
    """None은 '있음'이 아니다 — 시뮬레이터가 NaN을 None으로 실어 보낼 수 있음."""
    assert sc._valid(_row(**{field: None})) is False


def test_valid_rejects_empty_sensors():
    """sensors가 비면 집계할 값이 없음 → skip."""
    assert sc._valid(_row(sensors={})) is False


def test_valid_rejects_unparseable_timestamp():
    """ISO-8601 파싱 불가 timestamp → skip."""
    assert sc._valid(_row(timestamp="어제")) is False


def test_valid_accepts_z_suffix_timestamp():
    """'Z' 접미 timestamp는 정규화 후 파싱되므로 유효 (C3 — <3.11 대비)."""
    assert sc._valid(_row(timestamp="2026-07-20T01:00:00.000Z")) is True


# ── D0-2-1: 역직렬화 방어 · 디스패치 · shutdown (헌법 6-2) ───────────

def test_broken_json_is_skipped_and_loop_survives():
    """깨진 JSON → skip+로그, 루프는 죽지 않고 다음 메시지 처리."""
    c, fake = _make([FakeMsg(b"{not json"), FakeMsg(_row())])
    c.run()
    assert c.stats["skipped_deserialize"] == 1
    assert c.stats["handled"] == 1          # 뒤 메시지는 정상 처리
    assert fake.closed is True


@pytest.mark.parametrize("payload", [[1, 2, 3], "문자열", 123, True])
def test_deserialize_rejects_non_dict_json(payload):
    """dict 아닌 유효 JSON(list·str·숫자·bool)은 skip 처리(None 반환) — 7장 안티패턴 가드.

    `json.loads` 성공을 dict로 가정하면 하류 `.get()`이 AttributeError로 컨슈머를 죽인다.
    """
    c, _ = _make([])
    assert c._deserialize(FakeMsg(payload)) is None
    assert c.stats["skipped_deserialize"] == 1


def test_non_dict_json_payload_is_skipped_and_loop_survives():
    """dict 아닌 유효 JSON(list) → skip+로그, 루프는 죽지 않고 다음 메시지 처리 (헌법 6-2·7장).

    가드 없으면 `_valid([...])`의 `.get()`이 AttributeError로 새어 루프 전체가 죽는다.
    """
    c, fake = _make([FakeMsg([1, 2, 3]), FakeMsg(_row())])
    c.run()
    assert c.stats["skipped_deserialize"] == 1
    assert c.stats["handled"] == 1          # 뒤 메시지는 정상 처리
    assert fake.closed is True


def test_invalid_row_is_skipped_not_handled():
    """필수 필드 결측 row는 핸들러까지 못 감."""
    bad = _row()
    del bad["recipe_id"]
    c, _ = _make([FakeMsg(bad)])
    c.run()
    assert c.stats["skipped_invalid"] == 1 and c.stats["handled"] == 0


def test_subscribes_raw_topic():
    """구독 목록에 fdc.raw 포함 (B5-1a에서 fdc.correction 추가 — 아래 별도 테스트)."""
    c, fake = _make([])
    c.run()
    assert "fdc.raw" in fake.subscribed


def test_unknown_topic_is_ignored():
    """디스패치 뼈대 — 미등록 토픽은 조용히 무시(핸들러 미호출)."""
    c, _ = _make([FakeMsg(_row(), topic="fdc.other")])   # 실제 미등록 토픽(fdc.prediction은 이제 handled)
    c.run()
    assert c.stats["handled"] == 0


def test_kafka_error_message_is_skipped():
    """msg.error() 있는 메시지는 skip (파티션 EOF 등)."""
    c, _ = _make([FakeMsg(_row(), err="EOF")])
    c.run()
    assert c.stats["handled"] == 0


def test_request_stop_closes_consumer():
    """graceful shutdown — 루프 종료 시 consumer.close() (헌법 6-2)."""
    c, fake = _make([])
    c.run()
    assert fake.closed is True


# ── D0-2-2: 버퍼링 · 완성 트리거 ─────────────────────────────────────

class FakeClock:
    """주입 clock — 테스트가 시간을 직접 전진시킨다(sleep 없는 결정론적 tail 검증)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class RecordingConsumer(sc.SpcConsumer):
    """flush된 웨이퍼의 rows를 기록 — 집계·팬아웃(D0-2-3/4) 자리의 관측 seam."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.flushed: list = []

    def _process_wafer(self, rows):
        self.flushed.append(list(rows))


def _rec(msgs=(), clock=None, idle=30.0):
    holder = {}
    fake = FakeConsumer(msgs, on_exhausted=lambda: holder["c"].request_stop())
    holder["c"] = RecordingConsumer(
        consumer=fake, idle_flush_sec=idle, clock=clock or FakeClock(),
        offset_factory=lambda t, p, o, m=None: [(t, p, o + 1)])
    return holder["c"], fake


def test_config_key_wafer_idle_flush_sec_is_loaded():
    """헌법 6-1 — 매직넘버 금지, params.yaml `spc.wafer_idle_flush_sec`에서 로드."""
    assert sc.load_idle_flush_sec() > 0


def test_new_wafer_id_boundary_flushes_previous_wafer():
    """결정 2 — 같은 챔버에서 wafer_id가 바뀌면 직전 웨이퍼 완성 → flush."""
    c, _ = _rec([FakeMsg(_row(wafer_id="W1"), offset=0),
                 FakeMsg(_row(wafer_id="W1"), offset=1),
                 FakeMsg(_row(wafer_id="W2"), offset=2)])
    c.run()
    # W1(2건) flush, W2는 shutdown flush
    assert [len(f) for f in c.flushed] == [2, 1]
    assert {r["wafer_id"] for r in c.flushed[0]} == {"W1"}


def test_same_wafer_does_not_flush():
    """웨이퍼 진행 중에는 flush 없음 — 버퍼에 누적만."""
    c, _ = _rec([FakeMsg(_row(wafer_id="W1"), offset=i) for i in range(3)])
    c._install_signals()
    c._running = True
    for i in range(3):
        c._handle_raw(_row(wafer_id="W1"), FakeMsg(_row(), offset=i))
    assert c.flushed == []
    assert len(c.buffers["SIM_CH_1"]["rows"]) == 3


def test_idle_timeout_flushes_tail_wafer():
    """결정 2 — 마지막 웨이퍼는 경계 신호가 안 오므로 idle tail로 flush."""
    clock = FakeClock()
    c, _ = _rec(clock=clock, idle=30.0)
    c._handle_raw(_row(wafer_id="W1"), FakeMsg(_row(), offset=7))
    clock.t += 10
    c._sweep_idle()
    assert c.flushed == []                      # 아직 idle 아님
    clock.t += 25                               # 총 35s > 30s
    c._sweep_idle()
    assert len(c.flushed) == 1


def test_shutdown_flushes_remaining_buffers():
    """헌법 6-2 — 종료 시 남은 버퍼 전부 flush (in-flight 웨이퍼 유실 방지)."""
    c, fake = _rec([FakeMsg(_row(wafer_id="W1"), offset=3)])
    c.run()
    assert len(c.flushed) == 1 and fake.closed is True


def test_multichamber_buffers_are_independent():
    """멀티챔버 인터리브 — 챔버별 버퍼 분리로 서로 섞이지 않는다."""
    c, _ = _rec()
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=0))
    c._handle_raw(_row(chamber_id="CH2", wafer_id="B1"), FakeMsg(_row(), offset=1))
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=2))
    assert len(c.buffers["CH1"]["rows"]) == 2 and len(c.buffers["CH2"]["rows"]) == 1
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A2"), FakeMsg(_row(), offset=3))
    assert [len(f) for f in c.flushed] == [2]   # CH1만 경계 flush, CH2 무영향


def test_commit_only_after_flush_with_next_offset():
    """M1 — flush된 웨이퍼의 마지막 offset+1(다음 읽을 위치)까지만 커밋."""
    c, fake = _rec()
    c._handle_raw(_row(wafer_id="W1"), FakeMsg(_row(), offset=41))
    assert fake.commits == []                 # flush 전에는 커밋 없음
    c._flush("SIM_CH_1")
    assert fake.commits == [[("fdc.raw", 0, 42)]]


# ── B7-1 §6-2: offset 정밀(D1) + D16 + qual 커플링(§3-1-3) ──────────────────────

def test_commit_holds_at_min_unflushed_across_chambers():
    """D1 — 타 챔버 미flush 웨이퍼가 있으면 커밋이 그 first offset을 못 넘는다(1파티션 loss 방지)."""
    c, fake = _rec()
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=10))  # 진행 중
    c._handle_raw(_row(chamber_id="CH2", wafer_id="B1"), FakeMsg(_row(), offset=11))
    c._flush("CH2")
    assert fake.commits == [[("fdc.raw", 0, 10)]]     # min(CH1 first=10)−1=9 → 다음읽기 10(홀드)
    c._flush("CH1")
    assert fake.commits[-1] == [("fdc.raw", 0, 11)]   # 홀드 해제 → CH1 last(10) → 다음읽기 11


def test_flush_exception_holds_offset_via_pending_failed(monkeypatch):
    """D16 — flush 예외 웨이퍼 offset을 `_pending_failed`에 보관, 커밋 상한 홀드(재시작 회수)."""
    c, fake = _rec()
    orig = c._process_wafer
    def boom(rows):                              # CH2만 처리 실패
        if rows[-1]["chamber_id"] == "CH2":
            raise RuntimeError("process fail")
        return orig(rows)
    monkeypatch.setattr(c, "_process_wafer", boom)
    c._handle_raw(_row(chamber_id="CH2", wafer_id="B1"), FakeMsg(_row(), offset=3))
    c._flush("CH2")
    assert "CH2" in c._pending_failed and c.stats["flush_fail"] == 1
    assert fake.commits == []                          # 실패분은 커밋 안 함
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=5))
    c._flush("CH1")
    assert fake.commits == [[("fdc.raw", 0, 3)]]       # min(pending 3)−1=2 → 다음읽기 3(재전달 회수)


def test_restore_retries_once_before_giving_up():
    """D14 복원은 1회 재시도한다 (PM 리뷰 지적 2) — 복원 실패는 다음 재시작 보호까지 깎는다."""
    from types import SimpleNamespace
    c, _ = _rec()
    calls = []

    def flaky(partitions, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise RuntimeError("브로커 일시 지연")
        return [SimpleNamespace(metadata='{"CH1": 7}')]

    c.consumer.committed = flaky
    c._restore_dedup_state()
    assert len(calls) == 2 and calls[1] > calls[0]      # 2회 시도·timeout 증가
    assert c._restored_highwater == {"CH1": 7}          # 재시도로 복원 성공
    assert c.stats["dedup_restore_fail"] == 0          # 최종 성공이면 실패로 안 센다


def test_stall_warning_covers_all_failed_chambers_and_rewarns(caplog, monkeypatch):
    """정체 WARNING은 실패한 **전 챔버**를 덮고 주기적으로 재경고한다 (PM 리뷰 지적 4).

    구 로직은 ⓐ `fm[2]==min_hold` 라 동시 실패 시 하나만 경고 ⓑ 챔버당 1회 throttle 이라
    자동 해소 없는 영구 정체가 시간이 지나면 조용해졌다.
    """
    import logging
    clk = FakeClock()
    c, _ = _rec(clock=clk)
    c._pending_failed = {"CH1": ("fdc.raw", 0, 3), "CH2": ("fdc.raw", 0, 9)}   # 동시 실패 2챔버

    def stall_lines():
        return [r for r in caplog.records if "커밋 정체" in r.getMessage()]

    with caplog.at_level(logging.WARNING):
        c._commit(("fdc.raw", 0, 20))
        assert len(stall_lines()) == 2                  # 최솟값(3) 챔버만이 아니라 둘 다
        caplog.clear()
        c._commit(("fdc.raw", 0, 21))                   # 곧바로 재호출 → throttle 로 조용
        assert stall_lines() == []
        caplog.clear()
        clk.t += sc._STALL_REWARN_SEC                   # 재경고 간격 경과
        c._commit(("fdc.raw", 0, 22))
        assert len(stall_lines()) == 2                  # 영구 정체가 조용해지지 않는다


def test_single_partition_guard_logs_error(caplog):
    """파티션 >1 이면 ERROR 로 남긴다 — 기동은 막지 않는다 (PM 리뷰 지적 3)."""
    import logging
    from types import SimpleNamespace
    c, _ = _rec()
    c.consumer.list_topics = lambda topic=None, timeout=None: SimpleNamespace(
        topics={c.topic_raw: SimpleNamespace(partitions={0: object(), 1: object()})})
    with caplog.at_level(logging.ERROR):
        c._assert_single_partition()                    # 예외 없이 통과(기동 미차단)
    assert any("파티션 전제 위반" in r.getMessage() for r in caplog.records)


def test_single_partition_guard_never_blocks_startup(caplog):
    """브로커 조회가 실패해도 기동을 막지 않는다 — 가드가 서비스를 죽이면 역전이다."""
    c, _ = _rec()

    def boom(topic=None, timeout=None):
        raise RuntimeError("브로커 불가")

    c.consumer.list_topics = boom
    c._assert_single_partition()                        # 예외가 새면 여기서 터진다


def test_commit_exception_does_not_escape_flush(monkeypatch):
    """커밋 예외가 `_flush` 밖으로 새면 안 된다 — `run()`에 except가 없어 프로세스가 죽는다.

    `_commit`은 min-hold 계산 때문에 버퍼 리셋(finally) **뒤**에 있어야 해서 try 밖에 있다.
    그래서 `KafkaException`(코디네이터 페일오버·타임아웃)이 루프 밖으로 전파되고, 그 경로가
    `run()`의 `finally: _flush_all()`을 지나며 **거기서 또 던져 원래 예외를 대체**한다
    (죽은 이유가 트레이스백에 안 남음 — PR #128 PM 리뷰 지적 1). 커밋 offset은 누적이라
    삼켜도 안전하다(다음 성공 커밋이 덮는다).
    """
    c, fake = _rec()

    def boom(offsets=None, asynchronous=False):
        raise RuntimeError("코디네이터 페일오버")

    fake.commit = boom
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=5))
    c._flush("CH1")                                   # 예외가 새면 여기서 터진다
    assert c.buffers["CH1"]["rows"] == []             # 버퍼는 정상 리셋
    assert c.stats["flushed"] == 1                    # 판정 자체는 성공으로 남는다


def test_pending_failed_keeps_earliest_offset_on_consecutive_failures(monkeypatch):
    """D16 회귀 — 같은 챔버가 연속 2회 flush 실패해도 **최초(최소) offset**을 홀드한다.

    덮어쓰면 커밋 상한이 나중 실패 offset으로 올라가, 먼저 실패한 웨이퍼가 '처리됨'으로 커밋돼
    재시작 회수가 불가능해진다(영구 유실 — PR #128 3차 리뷰 Critical).
    """
    c, fake = _rec()
    orig = c._process_wafer

    def boom(rows):                              # CH2만 처리 실패
        if rows[-1]["chamber_id"] == "CH2":
            raise RuntimeError("process fail")
        return orig(rows)

    monkeypatch.setattr(c, "_process_wafer", boom)
    c._handle_raw(_row(chamber_id="CH2", wafer_id="B1"), FakeMsg(_row(), offset=3))
    c._flush("CH2")                              # 1차 실패 → 3 보관
    c._handle_raw(_row(chamber_id="CH2", wafer_id="B2"), FakeMsg(_row(), offset=7))
    c._flush("CH2")                              # 2차 실패 → 3 유지(7로 덮어쓰기 금지)
    assert c._pending_failed["CH2"][2] == 3
    assert c.stats["flush_fail"] == 2

    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=9))
    c._flush("CH1")
    assert fake.commits == [[("fdc.raw", 0, 3)]]   # min(3)−1=2 → 다음읽기 3 (B1 회수 가능)


def test_commit_holds_at_inprogress_qual_session():
    """§3-1-3 — 진행 중 qual 세션의 최초 offset을 커밋이 못 넘는다(#112 `_qual_buf` 편입)."""
    c, fake = _rec()
    c._qual_buf["CH1"] = {"pm_count": 3, "date": "20260720", "wafer_ids": ["q1"],
                          "values": {}, "session_first_msg": ("fdc.raw", 0, 7)}
    c._handle_raw(_row(chamber_id="CH2", wafer_id="B1"), FakeMsg(_row(), offset=20))
    c._flush("CH2")
    assert fake.commits == [[("fdc.raw", 0, 7)]]       # min(qual 7)−1=6 → 다음읽기 7(세션 홀드)


def test_flush_registers_qual_session_first_offset(monkeypatch):
    """§3-1-3 — qual 웨이퍼 flush 시 진행 세션 최초 offset을 홀드로 등록."""
    c, _ = _rec()
    def sim_qual(rows):                          # #112 _handle_qual_wafer 시뮬(세션버퍼 생성)
        ch = rows[-1]["chamber_id"]
        c._qual_buf.setdefault(ch, {"pm_count": 3, "date": "d", "wafer_ids": [], "values": {}})[
            "wafer_ids"].append(rows[-1]["wafer_id"])
    monkeypatch.setattr(c, "_process_wafer", sim_qual)
    c._handle_raw(_row(chamber_id="CH1", wafer_id="q1", is_qual=True), FakeMsg(_row(), offset=7))
    c._flush("CH1")
    assert c._qual_buf["CH1"]["session_first_msg"] == ("fdc.raw", 0, 7)


def test_commit_monotonic_guard_no_rewind():
    """단조 가드 — 이미 커밋한 offset 이하로는 되감지 않는다(재소비 방지)."""
    c, fake = _rec()
    c._handle_raw(_row(wafer_id="W1"), FakeMsg(_row(), offset=50))
    c._flush("SIM_CH_1")                          # commit 50 → 다음읽기 51
    assert c._last_committed_off == 50
    c._handle_raw(_row(wafer_id="W2"), FakeMsg(_row(), offset=20))  # 낮은 offset(비현실이나 가드)
    c._flush("SIM_CH_1")
    assert fake.commits == [[("fdc.raw", 0, 51)]]  # 두 번째는 20<50이라 되감기 안 함(추가 커밋 없음)


def test_commit_carries_dedup_metadata():
    """D14 (§6-2-9 가) — 커밋에 챔버별 마지막 처리 offset을 metadata로 동봉(커밋 offset과 별개)."""
    captured = {}
    c, _ = _rec()
    c.offset_factory = lambda t, p, o, m=None: captured.update(meta=m) or [(t, p, o + 1)]
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=8))
    c._flush("CH1")
    assert json.loads(captured["meta"]) == {"CH1": 8}   # CH1 처리 high-water


def test_restore_dedup_skips_reprocessed_on_restart():
    """D14 (§6-2-9 나) — 재시작 시 metadata high-water 이하 재전달은 skip(크래시 dup 방지)."""
    from types import SimpleNamespace
    c, _ = _rec()
    c.consumer._committed_result = [SimpleNamespace(metadata='{"CH1": 10}')]
    c._restore_dedup_state()
    assert c._restored_highwater == {"CH1": 10}
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=5))   # ≤10 재전달
    assert not c.buffers.get("CH1", {"rows": []})["rows"]        # skip(버퍼에 안 쌓임)
    assert c.stats["durable_dedup_skips"] == 1                   # §5 계측 — dedup이 일한 증거
    c._handle_raw(_row(chamber_id="CH1", wafer_id="A2"), FakeMsg(_row(), offset=11))  # >10 신규
    assert c.buffers["CH1"]["rows"]                              # 정상 처리
    assert c.stats["durable_dedup_skips"] == 1                   # 신규분은 skip 아님


def test_restore_failure_counted_and_run_continues():
    """D14 계측 — 복원 실패는 진행을 막지 않되 `dedup_restore_fail`로 계측한다(§5 GREEN 0 조건).

    실패해도 at-least-once로 진행하므로 로그만으로는 "크래시 dup 방어가 조용히 꺼진" 상태를 놓친다.
    """
    c, _ = _rec()

    def boom(*_a, **_kw):
        raise RuntimeError("broker timeout")

    c.consumer.committed = boom
    c._restore_dedup_state()                       # 예외를 삼키고 진행(치명 아님)
    assert c.stats["dedup_restore_fail"] == 1
    assert c._restored_highwater == {}             # 방어 없이 진행(복원분 없음)


def test_restore_seeds_last_processed_so_metadata_survives_partial_flush():
    """D14 회귀 — 재시작 후 **한 챔버만** flush해도 커밋 metadata가 나머지 챔버 high-water를 승계한다.

    복원분을 `_last_processed`에 시딩하지 않으면 첫 커밋이 metadata를 자기 챔버 것만으로 덮어써,
    연쇄 크래시(재시작→일부 flush→재크래시) 시 나머지 챔버의 dedup 보호가 사라진다(PR #128 2차 리뷰 Critical).
    """
    from types import SimpleNamespace
    captured = {}
    c, _ = _rec()
    c.offset_factory = lambda t, p, o, m=None: captured.update(meta=m) or [(t, p, o + 1)]
    c.consumer._committed_result = [
        SimpleNamespace(metadata='{"CH1": 10, "CH2": 20, "CH3": 30, "CH4": 40}')]
    c._restore_dedup_state()
    assert c._last_processed == {"CH1": 10, "CH2": 20, "CH3": 30, "CH4": 40}   # 복원분 시딩

    c._handle_raw(_row(chamber_id="CH1", wafer_id="A1"), FakeMsg(_row(), offset=50))  # CH1만 신규 flush
    c._flush("CH1")
    meta = json.loads(captured["meta"])
    assert meta["CH1"] == 50                                   # 새 값으로 갱신
    assert meta["CH2"] == 20 and meta["CH3"] == 30 and meta["CH4"] == 40   # 나머지 승계(유실 없음)


def test_flush_exception_still_resets_buffer():
    """P3 — 집계 예외에도 finally reset → 다음 웨이퍼에 이전 rows가 안 섞인다."""
    c, fake = _rec()

    def boom(rows):
        raise RuntimeError("집계 실패")

    c._process_wafer = boom
    c._handle_raw(_row(wafer_id="W1"), FakeMsg(_row(), offset=5))
    c._flush("SIM_CH_1")                        # 예외를 삼키고 루프 생존
    assert c.buffers["SIM_CH_1"]["rows"] == []
    assert fake.commits == []                 # 처리 실패 → 미커밋(재전달 창)


def test_flush_empty_buffer_is_noop():
    """빈 버퍼 flush는 무해 — 집계·커밋 모두 없음."""
    c, fake = _rec()
    c._flush("없는챔버")
    assert c.flushed == [] and fake.commits == []


# ── D0-2-3: 집계 브리지 (§5) ────────────────────────────────────────

_GK_C11 = ("SIM_CH_1", "C6_0", 4, "settled", "C11")
_GK_DVDC = ("SIM_CH_1", "C6_0", 4, "settled", "D_VDC_RES")


def _limits(*gks):
    """감시 대상 그룹의 관리선 — control_limits_loader가 싣는 6키와 동일 형태."""
    return {gk: {"center": 100.0, "sigma": 10.0 / 3, "ucl": 110.0, "lcl": 90.0,
                 "method": "sigma", "limit_version": "v1"} for gk in gks}


def _wafer_rows(n=3, **over):
    """같은 웨이퍼의 raw 샘플 n건 — C11은 100,101,102 / C12는 1.0 고정."""
    return [_row(wafer_id="W1", timestamp=f"2026-07-20T01:00:0{i}.000Z",
                 sensors={"C11": 100.0 + i, "C12": 1.0}, **over) for i in range(n)]


def test_bridge_matches_batch_aggregation():
    """결정 3 — 브리지 value가 배치(add_derived_features+build_wafer_summaries)와 동일.

    산식이 어긋나면 관리선과 사과-오렌지 비교가 되어 판정이 붕괴한다.
    """
    import pandas as pd
    from src.agent_b_spc.initial_limits import add_derived_features, build_wafer_summaries

    rows = _wafer_rows(3)
    points = sc._aggregate_to_points(rows, _limits(_GK_C11, _GK_DVDC))

    # 배치 경로: 같은 데이터를 C컬럼 df로 손수 구성
    df = pd.DataFrame([{"C24": "SIM_CH_1", "C6": "C6_0", "C7": 4, "C42": 0,
                        "C64": "W1", "C10": r["timestamp"],
                        "C11": r["sensors"]["C11"], "C12": r["sensors"]["C12"],
                        **{s: float("nan") for s in
                           ["C9", "C15", "C16", "C17", "C18", "C27", "C31", "C32",
                            "C54", "C56", "C57", "C58", "C61", "C62", "C63"]}}
                       for r in rows])
    batch = build_wafer_summaries(add_derived_features(df))
    expect = {r["sensor_id"]: r["value"] for _, r in batch.iterrows()}

    got = {p.group_key[-1]: p.value for p in points}
    assert got == pytest.approx(expect)
    assert got["C11"] == pytest.approx(101.0)          # (100+101+102)/3


def test_bridge_produces_derived_feature_point():
    """C1 — add_derived_features 선행 필수. D_VDC_RES(=C11−C12) Point가 나와야 한다."""
    points = sc._aggregate_to_points(_wafer_rows(3), _limits(_GK_C11, _GK_DVDC))
    dv = [p for p in points if p.group_key[-1] == "D_VDC_RES"]
    assert len(dv) == 1
    assert dv[0].value == pytest.approx(100.0)         # 101 − 1


def test_bridge_group_key_is_normalized_with_int_step():
    """결정 4 — group_key는 normalize(step=int). 타입 어긋나면 전 그룹 조용히 skip."""
    points = sc._aggregate_to_points(_wafer_rows(2, step="4"), _limits(_GK_C11, _GK_DVDC))
    assert points, "step이 str로 와도 int 정규화되어 매칭돼야 한다"
    assert all(isinstance(p.group_key[2], int) for p in points)
    assert points[0].group_key[:3] == ("SIM_CH_1", "C6_0", 4)


def test_bridge_timestamp_is_normalized_str_t0():
    """C3 — timestamp는 웨이퍼 t0의 'Z'→'+00:00' 정규화 **str**(datetime 아님)."""
    from datetime import datetime as _dt

    p = sc._aggregate_to_points(_wafer_rows(3), _limits(_GK_C11, _GK_DVDC))[0]
    assert isinstance(p.timestamp, str) and not p.timestamp.endswith("Z")
    assert p.timestamp == "2026-07-20T01:00:00.000+00:00"   # t0 = min
    _dt.fromisoformat(p.timestamp)                          # 엔진이 파싱 가능


def test_bridge_carries_pm_count_and_limit_version():
    """M2 — pm_count는 버퍼에서 별도 추출(요약이 안 실어나름) · limit_version은 limits에서."""
    p = sc._aggregate_to_points(_wafer_rows(2, pm_count=7), _limits(_GK_C11, _GK_DVDC))[0]
    assert p.pm_count == 7 and p.limit_version == "v1"
    assert p.wafer_id == "W1"


def test_bridge_omits_points_without_limits():
    """M3 — 비화이트리스트 그룹은 Point 생략(KeyError로 브리지가 먼저 죽지 않는다)."""
    points = sc._aggregate_to_points(_wafer_rows(3), _limits(_GK_C11))   # D_VDC_RES 미등록
    assert {p.group_key[-1] for p in points} == {"C11"}


def test_bridge_tolerates_missing_sensor_columns():
    """방어 — fdc.raw에 일부 센서가 없어도 KeyError 전면 실패 없이 있는 그룹만 산출.

    build_wafer_summaries가 센서 컬럼 전체를 하드 요구(.agg)하므로 결측 컬럼을 NaN으로
    채워야 한다. C1(D_VDC_RES 누락)과 같은 '0 point 무음 실패' 부류를 원천 차단.
    """
    rows = [_row(wafer_id="W1", sensors={"C11": 100.0}) for _ in range(2)]   # C12 없음
    points = sc._aggregate_to_points(rows, _limits(_GK_C11, _GK_DVDC))
    assert {p.group_key[-1] for p in points} == {"C11"}   # D_VDC_RES는 NaN → 자연 탈락


def test_bridge_transient_window_routing():
    """window=stabilization_flag — flag=1은 transient, 과도전용 센서만 살아남는다."""
    gk_t = ("SIM_CH_1", "C6_0", 4, "transient", "C18")
    rows = [_row(wafer_id="W1", stabilization_flag=1,
                 sensors={"C18": 5.0, "C11": 100.0}) for _ in range(2)]
    points = sc._aggregate_to_points(rows, _limits(gk_t, _GK_C11))
    assert {p.group_key[-1] for p in points} == {"C18"}   # C11(정착센서)은 transient서 제외


def test_bridge_empty_rows_returns_empty():
    """빈 입력 방어 — 예외 없이 빈 리스트."""
    assert sc._aggregate_to_points([], _limits(_GK_C11)) == []


# ── D0-2-4: 팬아웃 · seam (H1) ──────────────────────────────────────

class FakeNelson:
    """feed 반환 = 위반 목록 (Nelson 계약)."""

    def __init__(self, violations=None, boom=False):
        self.violations, self.boom, self.fed = violations or [], boom, []

    def feed(self, point):
        if self.boom:
            raise RuntimeError("nelson 폭발")
        self.fed.append(point)
        return list(self.violations)

    def update_whitelist(self, new_set):
        self.whitelist = new_set


class FakeTTTM:
    """feed 반환 = 텔레메트리(위반 아님) · 판정은 chamber_rollup (H1 비대칭)."""

    def __init__(self, rollup=None, boom=False):
        self.rollup, self.boom, self.fed = rollup, boom, []

    def feed(self, point):
        if self.boom:
            raise RuntimeError("tttm 폭발")
        self.fed.append(point)
        return [{"sensor_id": "C99", "rule_id": "TTTM_TELEMETRY"}]   # 섞이면 안 되는 반환

    def chamber_rollup(self, chamber_id):
        return self.rollup


def _fan(nelson=None, tttm=None, limits=None, **kw):
    """팬아웃 검증용 조립 — 수집 seam(collector)에 결과를 모은다.

    `join_grace_ms=0` — 이 헬퍼를 쓰는 테스트는 "collector 가 `_process_wafer` 안에서
    즉시 호출된다"를 전제한다(팬아웃·순서·예외격리). 예측 조인 유예가 켜지면 캐시 miss
    wafer 는 큐로 가서 다음 드레인에 발행되므로 그 전제가 깨진다(설계상 정상 — 스펙 §3-2).
    유예 경로는 `test_prediction_join_grace.py` 가 덮는다.
    """
    collected: list = []
    fake = FakeConsumer()
    kw.setdefault("join_grace_ms", 0)
    c = sc.SpcConsumer(consumer=fake, idle_flush_sec=30.0, clock=FakeClock(),
                       offset_factory=lambda t, p, o, m=None: [(t, p, o + 1)],
                       limits=limits if limits is not None else _limits(_GK_C11, _GK_DVDC),
                       nelson=nelson or FakeNelson(), tttm=tttm or FakeTTTM(),
                       collector=collected.append, **kw)
    return c, collected


_VIOL = {"sensor_id": "C11", "rule_id": "N1", "severity": "critical"}
_TTTM_OBJ = {"reference": "fleet_median", "score": 3.4, "top_gap_sensor": "C11",
             "reference_suspect": False}


def test_seam_emits_raw_violations_with_chamber_ts():
    """계약(B4-1 Step3a) — seam은 원형 violations[] + chamber_id·ts(wafer t0)를 6-튜플로 emit."""
    c, collected = _fan(nelson=FakeNelson([_VIOL]))
    c._process_wafer(_wafer_rows(3))
    wafer_id, chamber_id, ts, violations, *_ = collected[0]
    assert wafer_id == "W1" and chamber_id == "SIM_CH_1"
    assert ts.endswith("+00:00")                    # wafer t0 정규화(now() 아님)
    assert _VIOL in violations                       # 축약(_to_spc_flags) 아님 — Nelson 원형 dict


def test_tttm_feed_return_is_not_mixed_into_violations():
    """H1 — TTTM feed 반환은 텔레메트리라 violations에 섞이면 안 된다."""
    c, collected = _fan(nelson=FakeNelson([]), tttm=FakeTTTM(rollup=_TTTM_OBJ))
    c._process_wafer(_wafer_rows(3))
    _, _, _, violations, tttm_obj, _ = collected[0]
    assert violations == []                          # Nelson 위반 0 → 비어야 함
    assert tttm_obj == _TTTM_OBJ                    # 판정은 chamber_rollup에서만


def test_both_engines_receive_every_point():
    """팬아웃 — 웨이퍼의 모든 그룹 Point가 두 엔진에 동일하게 들어간다."""
    nelson, tttm = FakeNelson(), FakeTTTM()
    c, _ = _fan(nelson=nelson, tttm=tttm)
    c._process_wafer(_wafer_rows(3))
    assert len(nelson.fed) == len(tttm.fed) == 2    # C11 · D_VDC_RES
    assert {p.group_key[-1] for p in nelson.fed} == {"C11", "D_VDC_RES"}


def test_nelson_failure_does_not_stop_tttm():
    """엔진 격리 — 한쪽 예외가 다른 엔진·루프를 죽이지 않는다 (헌법 6-2)."""
    tttm = FakeTTTM(rollup=_TTTM_OBJ)
    c, collected = _fan(nelson=FakeNelson(boom=True), tttm=tttm)
    c._process_wafer(_wafer_rows(3))
    assert len(tttm.fed) == 2                       # TTTM은 정상 수급
    assert collected[0][4] == _TTTM_OBJ             # tttm_obj = 6-튜플 index 4


def test_tttm_failure_does_not_stop_nelson():
    """엔진 격리 — 역방향."""
    nelson = FakeNelson([_VIOL])
    c, collected = _fan(nelson=nelson, tttm=FakeTTTM(boom=True))
    c._process_wafer(_wafer_rows(3))
    assert len(nelson.fed) == 2
    assert collected[0][3]                          # violations 정상 산출(6-튜플 index 3)


def test_no_findings_does_not_emit_to_collector():
    """위반도 rollup도 없으면 수집 seam 미호출 (소음 방지)."""
    c, collected = _fan(nelson=FakeNelson([]), tttm=FakeTTTM(rollup=None))
    c._process_wafer(_wafer_rows(3))
    assert collected == []


class _FakePredCache:
    """예측 캐시 스텁 — get(wafer_id) 인터페이스만(비파괴 peek 검증용)."""

    def __init__(self, hits=None):
        self._h = hits or {}

    def get(self, wafer_id):
        return self._h.get(wafer_id)


def test_prediction_only_wafer_emits_for_crazy_reach():
    """위반·tttm 0이어도 예측 있으면 collector로 태운다 (B4-1 Step5b 리뷰2 — crazy 판정 도달 보장).

    진성 crazy(위반·tttm 0)가 seam early-return에 막혀 드롭되면 안 된다. 예측 존재 시 우회.
    """
    fake_pred = _FakePredCache({"W1": {"predicted_c65": 9999.0}})
    c, collected = _fan(nelson=FakeNelson([]), tttm=FakeTTTM(rollup=None),
                        prediction_cache=fake_pred)
    c._process_wafer(_wafer_rows(3))
    assert len(collected) == 1
    _, _, _, violations, tttm_obj, recipe_id = collected[0]
    assert violations == [] and tttm_obj is None
    assert recipe_id == "C6_0"                       # seam이 raw row recipe_id 승계(리뷰②)


def test_seam_carries_recipe_id_from_raw_row():
    """seam이 wafer raw row recipe_id를 6-튜플 마지막에 실어 collector로 전달(리뷰②)."""
    c, collected = _fan(nelson=FakeNelson([_VIOL]))
    c._process_wafer(_wafer_rows(3))
    assert collected[0][5] == "C6_0"                 # index 5 = recipe_id


def test_flush_drives_fanout_end_to_end():
    """_flush → 브리지 → 팬아웃 → 수집까지 관통."""
    c, collected = _fan(nelson=FakeNelson([_VIOL]), tttm=FakeTTTM(rollup=_TTTM_OBJ))
    for r in _wafer_rows(3):
        c._handle_raw(r, FakeMsg(r, offset=1))
    c._flush("SIM_CH_1")
    assert len(collected) == 1 and collected[0][0] == "W1"


# ── D15 (B7-1 §3-2-1) — 엔진 feed 앞 dedup 가드 (재전달 판정버퍼 오염 방지) ──────
def _d15_wafer(wid, pm=3):
    """distinct wafer_id·pm_count 지정 웨이퍼 rows(raw 3건). _drive_provisional이 _last_wafer를
    설정하려면 provisional 주입 + pm_count가 필요하다."""
    return [_row(wafer_id=wid, timestamp=f"2026-07-20T01:00:0{i}.000Z",
                 sensors={"C11": 100.0 + i, "C12": 1.0}, pm_count=pm) for i in range(3)]


def test_d15_duplicate_wafer_skips_feed_and_keeps_counter():
    """같은 wafer 재전달 → 엔진 feed 스킵(판정 버퍼 불변) + _post_pm 카운터도 불변(:604 유지).

    엔진 시간 가드가 동일 timestamp를 수용하므로, D15가 없으면 재전달 Point가 Nelson·TTTM
    버퍼에 이중 적재된다(⑥⑦). 여기서 fed 길이 불변으로 스킵을 검증한다.
    """
    nelson, tttm = FakeNelson([_VIOL]), FakeTTTM(rollup=_TTTM_OBJ)
    c, collected = _fan(nelson=nelson, tttm=tttm, provisional=_prov())
    rows = _d15_wafer("W1", pm=3)
    c._process_wafer(rows)                               # 1차 — feed + _last_wafer 설정 + collector
    n1, t1 = len(nelson.fed), len(tttm.fed)
    assert n1 > 0 and c._post_pm["SIM_CH_1"] == 1        # 1차엔 실제로 fed·카운트됨
    assert len(collected) == 1                           # 1차엔 하류(collector) 1회
    c._process_wafer(rows)                               # 2차 — 같은 wafer(재전달)
    assert len(nelson.fed) == n1                         # Nelson 판정 버퍼 불변 (⑥ 차단)
    assert len(tttm.fed) == t1                           # TTTM 롤링 윈도우 불변 (⑦ 차단)
    assert c._post_pm["SIM_CH_1"] == 1                   # _post_pm 이중 증가 없음 (읽기만 전진)
    assert len(collected) == 1                           # 🆕 하류 collector 재호출 없음 (dup no-op, 리뷰 지적1)


def test_d15_distinct_wafers_both_fed():
    """다른 wafer는 안 막는다 — 정상 처리 무영향(가드 과다 스킵 방지)."""
    nelson, tttm = FakeNelson(), FakeTTTM()
    c, _ = _fan(nelson=nelson, tttm=tttm, provisional=_prov())
    c._process_wafer(_d15_wafer("W1", pm=3))
    n1 = len(nelson.fed)
    assert n1 > 0
    c._process_wafer(_d15_wafer("W2", pm=3))             # 다른 wafer
    assert len(nelson.fed) == 2 * n1                     # 둘 다 fed
    assert c._post_pm["SIM_CH_1"] == 2                   # 카운터 2회 증가


# ── B4-1 Step3b — 예측 캐시 배선 (구독→upsert · purge · 정규화 collector) ──────
class FakePredictionCache:
    """예측 캐시 fake — upsert/purge/get 호출 관측 (3b 배선 검증용)."""

    def __init__(self, boom_upsert=False):
        self.upserts: list = []
        self.purge_calls = 0
        self.boom_upsert = boom_upsert

    def upsert(self, msg):
        if self.boom_upsert:
            raise RuntimeError("boom")
        self.upserts.append(msg)
        return True

    def purge_expired(self):
        self.purge_calls += 1
        return 0

    def get(self, wafer_id):
        return None


def _pred_consumer(cache, msgs=(), clock=None):
    """예측 캐시 주입 컨슈머 (from_config 기본값 우회)."""
    holder = {}
    fake = FakeConsumer(list(msgs), on_exhausted=lambda: holder["c"].request_stop())
    c = sc.SpcConsumer(consumer=fake, prediction_cache=cache, clock=clock or FakeClock(),
                       limits=_limits(_GK_C11, _GK_DVDC), nelson=FakeNelson(), tttm=FakeTTTM(),
                       idle_flush_sec=30.0, offset_factory=lambda t, p, o, m=None: [(t, p, o + 1)])
    holder["c"] = c
    return c, fake


def test_handle_prediction_upserts_to_cache():
    """fdc.prediction row → 예측 캐시 upsert."""
    cache = FakePredictionCache()
    c, _ = _pred_consumer(cache)
    c._handle_prediction({"wafer_id": "W1", "predicted_c65": 1200.0})
    assert cache.upserts == [{"wafer_id": "W1", "predicted_c65": 1200.0}]


def test_handle_prediction_upsert_exception_isolated():
    """캐시 upsert 예외가 루프를 못 죽인다 (헌법 6-2) — raise 없이 삼킴."""
    cache = FakePredictionCache(boom_upsert=True)
    c, _ = _pred_consumer(cache)
    c._handle_prediction({"wafer_id": "W1"})     # 예외 전파 안 됨(정상 반환)


def test_maybe_purge_on_monotonic_interval():
    """purge 캐던스 — 인터벌 경과 시만 purge_expired(monotonic clock 기준)."""
    clock = FakeClock()
    cache = FakePredictionCache()
    c, _ = _pred_consumer(cache, clock=clock)
    c._purge_interval_sec = 60.0
    c._last_purge_at = clock()
    c._maybe_purge()                             # 경과 0 < 60 → skip
    assert cache.purge_calls == 0
    clock.t += 61
    c._maybe_purge()                             # 경과 61 ≥ 60 → purge
    assert cache.purge_calls == 1


def test_run_subscribes_prediction_and_dispatches_before_valid():
    """run() — fdc.prediction 4번째 구독 + _valid 우회 upsert(raw 필수필드 없어도)."""
    cache = FakePredictionCache()
    pred = FakeMsg({"wafer_id": "W1", "predicted_c65": 1200.0}, topic="fdc.prediction")
    c, fake = _pred_consumer(cache, msgs=[pred])
    c.run()
    assert "fdc.prediction" in fake.subscribed          # 4토픽 구독
    assert cache.upserts == [{"wafer_id": "W1", "predicted_c65": 1200.0}]  # _valid 우회 upsert


def test_reload_limits_seam_updates_engines():
    """결정 5 — 리로드 seam. 실제 fdc.correction 구독 트리거는 B5-1a 이월."""
    nelson, tttm = FakeNelson(), FakeTTTM()
    c, _ = _fan(nelson=nelson, tttm=tttm)
    new_limits = _limits(_GK_C11)
    c.reload_limits(new_limits)
    assert c.limits == new_limits
    assert nelson.limits == new_limits and tttm.limits == new_limits


def test_reload_limits_keeps_whitelist_startup_only():
    """whitelist는 startup-only(운영 중 불변) — 리로드는 관리선 값만 바꾼다."""
    nelson = FakeNelson()
    nelson.whitelist = {"기존"}
    c, _ = _fan(nelson=nelson)
    c.reload_limits(_limits(_GK_C11))
    assert nelson.whitelist == {"기존"}


# ── D0-2-5: 실엔진 관통 · 진입점 ────────────────────────────────────

def test_bootstrap_env_missing_fails_loudly(monkeypatch):
    """헌법 6-1 — bootstrap 미설정 시 조용한 기본값 없이 명시적 실패."""
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    with pytest.raises(RuntimeError, match="KAFKA_BOOTSTRAP_SERVERS"):
        sc._bootstrap()


def test_bootstrap_env_present(monkeypatch):
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    assert sc._bootstrap() == "localhost:9092"


def test_real_engines_end_to_end_nelson_fires():
    """관통(실엔진) — raw 스트림 → 집계 → 실 NelsonEngine → N1 위반 수집.

    관리선 밖(200 vs UCL 110) 웨이퍼가 들어오면 N1이 떠야 한다. 엔진을 stub이 아닌
    실물로 돌려 Point 계약(group_key 타입·timestamp 파싱·limit_version)을 함께 검증.
    """
    collected: list = []
    limits = _limits(_GK_C11)                       # C11만 감시 (D_VDC_RES 비대상)
    msgs = []
    for w, val in [("W1", 100.0), ("W2", 200.0)]:   # W2가 UCL 초과
        for i in range(2):
            msgs.append(FakeMsg(_row(wafer_id=w, sensors={"C11": val, "C12": 1.0},
                                     timestamp=f"2026-07-20T01:0{len(msgs)}:00.000Z")))
    holder = {}
    fake = FakeConsumer(msgs, on_exhausted=lambda: holder["c"].request_stop())
    holder["c"] = sc.SpcConsumer(consumer=fake, limits=limits, whitelist=set(limits),
                                 collector=collected.append, idle_flush_sec=30.0,
                                 clock=FakeClock(),
                                 offset_factory=lambda t, p, o, m=None: [(t, p, o + 1)])
    holder["c"].run()

    viols = [v for _, _, _, vs, *_ in collected for v in vs]
    assert any(v["sensor_id"] == "C11" and v["rule_id"] == "N1" for v in viols)
    assert fake.closed is True


# ── 통합(skipif): 실제 Kafka 브로커 관통 ─────────────────────────────

def _kafka_available() -> bool:
    """로컬 브로커 접속 가능 여부 — 미가동/미설치면 통합 테스트 skip."""
    try:
        from confluent_kafka.admin import AdminClient

        import os
        from dotenv import load_dotenv
        load_dotenv()
        bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
        if not bootstrap:
            return False
        AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _kafka_available(), reason="Kafka 브로커 미접속 — 통합테스트 skip")
def test_live_kafka_end_to_end_nelson_fires():
    """통합 — 실제 브로커로 fdc.raw 발행 → 실 Consumer 구독 → 집계 → Nelson N1.

    가짜 컨슈머가 못 잡는 것을 잡는다: 실제 Consumer/Message API 정합, JSON 왕복,
    수동 커밋(TopicPartition offset+1) 호출 성공, idle tail flush의 실시간 동작.
    """
    import threading
    import time as _time

    from confluent_kafka import Producer

    bootstrap = sc._bootstrap()
    topic = "test.fdc.raw.d02"
    group = f"test-spc-{_time.time_ns()}"        # 매 실행 새 그룹 → earliest 재현성

    # ① 발행: W1(정상) → W2(UCL 초과). 실제 파티셔닝과 동일하게 wafer_id를 키로.
    producer = Producer({"bootstrap.servers": bootstrap})
    for i, (w, val) in enumerate([("LW1", 100.0), ("LW1", 101.0),
                                  ("LW2", 200.0), ("LW2", 201.0)]):
        row = _row(wafer_id=w, chamber_id="LIVE_CH", sensors={"C11": val, "C12": 1.0},
                   timestamp=f"2026-07-20T02:0{i}:00.000Z")
        producer.produce(topic, key=w.encode(), value=json.dumps(row).encode())
    producer.flush(10)

    # ② 구독: 실 Consumer + 실 엔진
    gk = ("LIVE_CH", "C6_0", 4, "settled", "C11")
    collected: list = []
    consumer = sc.make_consumer(bootstrap, group=group, auto_offset_reset="earliest")
    c = sc.SpcConsumer(consumer=consumer, limits=_limits(gk), whitelist={gk},
                       collector=collected.append, idle_flush_sec=3.0,
                       topic_raw=topic, topic_correction="test.fdc.correction.d02",
                       topic_agent="test.fdc.agent.d02", topic_prediction="test.fdc.prediction.d02")

    t = threading.Thread(target=c.run, daemon=True)
    t.start()
    # W1은 관리선 내 정상이라 수집 대상이 아니다 — 위반은 W2에서만 나온다.
    deadline = _time.time() + 45                 # 미도달 시 무한대기 방지
    while not collected and _time.time() < deadline:
        _time.sleep(0.3)
    c.request_stop()
    t.join(timeout=15)

    viols = [v for _, _, _, vs, *_ in collected for v in vs]
    assert any(v["sensor_id"] == "C11" and v["rule_id"] == "N1" for v in viols), f"수집 결과: {collected}"
    assert c.stats["flushed"] >= 2               # W1 경계 flush + W2 tail/shutdown flush


# ── B5-1a-L1: 토픽 인지형 라우팅(C1) · fdc.correction 구독 · engine 주입(M2) ──

def _corr(correction_type="limit", **over):
    """fdc.correction 메시지 payload — wafer_id 없음(raw _valid에 걸리면 안 됨)."""
    row = {"incident_id": "INC-20260721-SIM_CH_3-01", "correction_type": correction_type,
           "sensor": "C11", "limit_version": "v5", "correction_id": "LIM-20260721-SIM_CH_3-01"}
    row.update(over)
    return row


class FakeEngine:
    """sqlalchemy Engine 미러 — begin() 컨텍스트매니저만 (apply_approved는 mock)."""

    def __init__(self):
        self.begins = 0

    def begin(self):
        self.begins += 1
        return _FakeTxn()


class _FakeTxn:
    def __enter__(self):
        return "CONN"

    def __exit__(self, *a):
        return False


def test_subscribes_both_raw_and_correction():
    """§12-3 + B6-3-c + B4-1 Step3b — 구독: raw + correction + agent + prediction 4토픽."""
    c, fake = _make([])
    c.run()
    assert fake.subscribed == [sc.TOPIC_RAW, sc.TOPIC_CORRECTION, sc.TOPIC_AGENT, sc.TOPIC_PREDICTION]


def test_engine_is_stored_for_apply():
    """M2 — apply_approved·loader가 쓸 engine을 __init__에서 보유."""
    eng = FakeEngine()
    c, _ = _make([], engine=eng)
    assert c.engine is eng


def test_engine_defaults_none_when_not_injected():
    """engine 미주입(순수 raw 경로) 시 None — 기존 D0-2 테스트 무영향."""
    c, _ = _make([])
    assert c.engine is None


def test_correction_bypasses_valid_and_reaches_handler():
    """C1 — correction은 raw _valid(wafer_id 요구)를 우회하고 _handle_correction 도달.

    과거엔 run()이 전건 _valid를 강제해 correction(=wafer_id 없음)을 전부 폐기했다.
    """
    called = []
    c, _ = _make([FakeMsg(_corr(), topic=sc.TOPIC_CORRECTION)])
    c._handle_correction = lambda row: called.append(row) or True
    c.run()
    assert len(called) == 1                       # 핸들러 도달
    assert c.stats["skipped_invalid"] == 0        # _valid에 폐기 안 됨


def test_correction_commits_when_handler_returns_true():
    """①(b) — 핸들러 True(성공·skip) → 오프셋 커밋."""
    c, fake = _make([FakeMsg(_corr(), topic=sc.TOPIC_CORRECTION, offset=7)],
                    offset_factory=lambda t, p, o, m=None: [(t, p, o + 1)])
    c._handle_correction = lambda row: True
    c.run()
    assert fake.commits == [[(sc.TOPIC_CORRECTION, 0, 8)]]


def test_correction_not_committed_when_handler_returns_false():
    """①(b) — 핸들러 False(apply 예외) → 커밋 보류(재전달 재시도)."""
    c, fake = _make([FakeMsg(_corr(), topic=sc.TOPIC_CORRECTION)])
    c._handle_correction = lambda row: False
    c.run()
    assert fake.commits == []


def test_raw_path_still_validated_after_split():
    """C1 — raw 경로는 불변: _valid 통과 후 _dispatch (correction 분기가 raw를 안 건드림)."""
    bad = _row()
    del bad["recipe_id"]                           # raw _valid 실패 유도
    c, _ = _make([FakeMsg(bad, topic=sc.TOPIC_RAW)])
    c.run()
    assert c.stats["skipped_invalid"] == 1 and c.stats["handled"] == 0


def test_raw_message_does_not_reach_correction_handler():
    """라우팅 격리 — raw 메시지는 _handle_correction으로 새지 않는다."""
    called = []
    c, _ = _make([FakeMsg(_row(), topic=sc.TOPIC_RAW)])
    c._handle_correction = lambda row: called.append(row) or True
    c.run()
    assert called == []


# ── B5-1a-L2: _handle_correction (correction_id gate·apply·reload·(b)커밋) ──

def _corr_consumer(monkeypatch, *, apply_side_effect=None, apply_result=True, new_limits=None):
    """_handle_correction 검증용 — apply_approved·loader를 mock한 SpcConsumer.

    `apply_result` = apply_approved 반환(bool) mock — True=적용됨 / False=APPLY_FAILED·no-op.
    """
    apply_calls, load_calls = [], []

    def fake_apply(conn, cid):
        apply_calls.append((conn, cid))
        if apply_side_effect:
            apply_side_effect()
        return apply_result

    def fake_load(engine):
        load_calls.append(engine)
        return (new_limits if new_limits is not None else {"GK": 1}, {"WL"})

    monkeypatch.setattr(sc, "apply_approved", fake_apply)
    monkeypatch.setattr(sc.control_limits_loader, "load", fake_load)
    c, _ = _make([], engine=FakeEngine())
    return c, apply_calls, load_calls


def test_correction_limit_applies_and_reloads(monkeypatch):
    """정상 경로 — limit correction → apply_approved(cid) → 전체 리로드 → True."""
    new_limits = {("SIM_CH_3", "C6_0", 4, "settled", "C11"): {"limit_version": "v7"}}
    c, apply_calls, load_calls = _corr_consumer(monkeypatch, new_limits=new_limits)
    ok = c._handle_correction(_corr(correction_id="LIM-X-1"))
    assert ok is True
    assert apply_calls == [("CONN", "LIM-X-1")]   # engine.begin() conn으로 apply
    assert load_calls == [c.engine]               # apply 커밋 후 전체 리로드
    assert c.limits == new_limits                 # reload_limits 반영


def test_correction_recipe_is_skipped(monkeypatch):
    """correction_type='recipe' → 시뮬레이터 몫, skip + 커밋(True), apply 미호출."""
    c, apply_calls, _ = _corr_consumer(monkeypatch)
    ok = c._handle_correction(_corr(correction_type="recipe"))
    assert ok is True and apply_calls == []


def test_correction_missing_id_skips_with_commit(monkeypatch):
    """② correction_id 부재(과도기) → skip+로그, apply 미호출, 커밋(True — 재시도 X)."""
    c, apply_calls, _ = _corr_consumer(monkeypatch)
    row = _corr()
    del row["correction_id"]
    ok = c._handle_correction(row)
    assert ok is True and apply_calls == []


def test_correction_apply_failure_holds_commit(monkeypatch):
    """①(b) — apply 예외 → False(커밋 보류=재전달), 루프 생존(예외 삼킴)."""
    def boom():
        raise RuntimeError("DB 충돌")

    c, apply_calls, _ = _corr_consumer(monkeypatch, apply_side_effect=boom)
    ok = c._handle_correction(_corr())
    assert ok is False and len(apply_calls) == 1   # 시도는 함, 실패로 미커밋


def test_correction_reload_only_after_apply(monkeypatch):
    """③ — apply가 예외면 reload 안 함(옛 관리선 유지)."""
    def boom():
        raise RuntimeError("apply 실패")

    c, _, load_calls = _corr_consumer(monkeypatch, apply_side_effect=boom)
    c._handle_correction(_corr())
    assert load_calls == []                         # apply 실패 → 리로드 미실행


def test_correction_apply_failed_skips_reload_and_commits(monkeypatch):
    """수정승인 §7 — apply_approved=False(APPLY_FAILED) → reload·firm 후속 skip, 커밋(True·재시도X)."""
    c, apply_calls, load_calls = _corr_consumer(monkeypatch, apply_result=False)
    ok = c._handle_correction(_corr(correction_id="LIM-X-1"))
    assert ok is True                               # 커밋(APPLY_FAILED status는 txn서 기록됨·재시도 X)
    assert len(apply_calls) == 1                    # 시도는 함
    assert load_calls == []                         # ★ 미적용이라 reload 안 함(stale 관리선 방지)


def test_correction_uses_transaction(monkeypatch):
    """원자성 — apply_approved는 engine.begin() 트랜잭션 안에서 호출(commit=호출자)."""
    c, apply_calls, _ = _corr_consumer(monkeypatch)
    c._handle_correction(_corr())
    assert c.engine.begins == 1                     # begin() 한 번 (한 트랜잭션)


def test_correction_handles_full_orchestrator_payload(monkeypatch):
    """실 orchestrator 페이로드(build_correction_payload limit형) 관통 — PM correction_id 반영(2026-07-22).

    correction_type·correction_id만 쓰고 나머지(sensor·limit_version·effective_from·delta_pct)는
    .get()으로 무시 → 실제 페이로드 형태에 견고. gate 점등 실증.
    """
    c, apply_calls, _ = _corr_consumer(monkeypatch)
    payload = {"incident_id": "INC-20260722-SIM_CH_3-01", "correction_type": "limit",
               "effective_from": None, "sensor": "C11", "limit_version": "v5",
               "correction_id": "LIM-20260722-SIM_CH_3-01",
               "parameter_id": None, "delta_pct": None}   # 계약 §6 limit형 전체 형태
    ok = c._handle_correction(payload)
    assert ok is True
    assert apply_calls == [("CONN", "LIM-20260722-SIM_CH_3-01")]   # correction_id 정확 추출


# ── B5-1a-L3: 실 DB commit→reload 왕복 (skipif) ─────────────────────
# ⚠️ 이 테스트만 commit 함 — reload_limits가 loader.load(engine)로 새 커넥션 재로드라
#    apply가 commit돼야 새 버전이 가시(READ COMMITTED). teardown으로 시드 행 삭제.

def _db_available() -> bool:
    import os

    from dotenv import load_dotenv
    from sqlalchemy import create_engine, text
    try:
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = create_engine(url, connect_args={"connect_timeout": 3})
        try:
            with probe.connect() as c:
                return c.execute(text(
                    "SELECT count(*) FROM information_schema.columns WHERE "
                    "table_name='limit_corrections' AND column_name='delta_sigma'")).scalar() == 1
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


_L3_CH = "TEST_CH_CORR_L3"
_L3_GK = (_L3_CH, "C6_0", 4, "settled", "C11")
_L3_CID = "LIM-TESTL3-0001"


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")
def test_correction_e2e_applies_and_reloads():
    """관통: fdc.correction → apply_approved(실 DB) → control_limits 새 버전 → reload 반영.

    스펙 §12-7 — commit→reload 왕복. 롤백하면 reload가 옛 값을 읽어 검증이 헛돎.
    """
    from sqlalchemy import text

    from src.agent_b_spc.seed_control_limits import make_engine

    engine = make_engine()
    ins_cl = text(
        "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
        "limit_version, method, center, sigma, ucl, lcl, k_sigma, q_low, q_high, trigger_type, "
        "is_active) VALUES (:ch,'C6_0',4,'settled','C11','v1','sigma',100.0,10.0,130.0,70.0,"
        "3.0,NULL,NULL,'initial',true)")
    ins_lc = text(
        "INSERT INTO limit_corrections (correction_id, chamber_id, recipe_id, step, sensor_window, "
        "sensor_id, limit_version_before, center_before, center_after, ucl_after, lcl_after, "
        "delta_sigma, trigger_type, calc_window_n, status) VALUES (:cid,:ch,'C6_0',4,'settled',"
        "'C11','v1',100.0,106.0,136.0,76.0,0.6,'periodic',500,'PROPOSED')")
    try:
        with engine.begin() as conn:                # 시드 커밋 (가시성 필요)
            conn.execute(ins_cl, {"ch": _L3_CH})
            conn.execute(ins_lc, {"cid": _L3_CID, "ch": _L3_CH})

        c = sc.SpcConsumer(consumer=FakeConsumer([]), engine=engine,
                           limits={_L3_GK: {"center": 100.0, "sigma": 10.0, "ucl": 130.0,
                                            "lcl": 70.0, "method": "sigma", "limit_version": "v1"}},
                           whitelist={_L3_GK})
        ok = c._handle_correction({"correction_type": "limit", "correction_id": _L3_CID})
        assert ok is True

        with engine.connect() as conn:
            active = conn.execute(text(
                "SELECT limit_version FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": _L3_CH}).scalar_one()
            assert active == "v2"                    # 새 버전 적용 (v1 → v2)
            status = conn.execute(text(
                "SELECT status FROM limit_corrections WHERE correction_id=:cid"),
                {"cid": _L3_CID}).scalar_one()
            assert status == "APPROVED_APPLIED"      # 상태 전이

        assert c.limits[_L3_GK]["limit_version"] == "v2"   # reload가 새 버전 반영
        assert c.limits[_L3_GK]["center"] == 106.0
    finally:
        with engine.begin() as conn:                # teardown — 시드·적용 행 제거
            conn.execute(text("DELETE FROM control_limits WHERE chamber_id=:ch"), {"ch": _L3_CH})
            conn.execute(text("DELETE FROM limit_corrections WHERE chamber_id=:ch"), {"ch": _L3_CH})
        engine.dispose()


@pytest.mark.skipif(not (_kafka_available() and _db_available()),
                    reason="Kafka 브로커/DB 미접속 — 관통 통합테스트 skip")
def test_live_kafka_correction_e2e():
    """관통(실 브로커+실 DB): Producer가 fdc.correction 발행 → 실 Consumer가 받아
    apply_approved → reload까지 정말 도는지 확인. correction_id는 이 테스트가 생성.

    FakeConsumer가 못 잡는 것을 잡는다: 실 Consumer 2토픽 구독·correction 라우팅·
    수동 커밋·실제 메시지 왕복. PM 페이로드 미배선 상태를 우회해 라이브 경로만 검증.
    """
    import threading
    import time as _time

    from confluent_kafka import Producer
    from confluent_kafka.admin import AdminClient, NewTopic
    from sqlalchemy import text

    from src.agent_b_spc.seed_control_limits import make_engine

    bootstrap = sc._bootstrap()
    ns = _time.time_ns()
    corr_topic = f"test.fdc.correction.{ns}"
    raw_topic = f"test.fdc.raw.{ns}"                 # 빈 토픽 (구독만, 메시지 없음)
    ch, cid = f"TEST_CH_KFK_{ns}", f"LIM-KFK-{ns}"
    gk = (ch, "C6_0", 4, "settled", "C11")

    admin = AdminClient({"bootstrap.servers": bootstrap})
    for _t, f in admin.create_topics([NewTopic(corr_topic, 1, 1),
                                      NewTopic(raw_topic, 1, 1)]).items():
        try:
            f.result()
        except Exception:  # noqa: BLE001 — 이미 존재 등
            pass
    _time.sleep(2)

    engine = make_engine()
    ins_cl = text(
        "INSERT INTO control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, "
        "limit_version, method, center, sigma, ucl, lcl, k_sigma, q_low, q_high, trigger_type, "
        "is_active) VALUES (:ch,'C6_0',4,'settled','C11','v1','sigma',100.0,10.0,130.0,70.0,"
        "3.0,NULL,NULL,'initial',true)")
    ins_lc = text(
        "INSERT INTO limit_corrections (correction_id, chamber_id, recipe_id, step, sensor_window, "
        "sensor_id, limit_version_before, center_before, center_after, ucl_after, lcl_after, "
        "delta_sigma, trigger_type, calc_window_n, status) VALUES (:cid,:ch,'C6_0',4,'settled',"
        "'C11','v1',100.0,106.0,136.0,76.0,0.6,'periodic',500,'PROPOSED')")
    c = None
    t = None
    try:
        with engine.begin() as conn:
            conn.execute(ins_cl, {"ch": ch})
            conn.execute(ins_lc, {"cid": cid, "ch": ch})

        # 실 Consumer (2토픽 구독) 기동
        c = sc.SpcConsumer(
            consumer=sc.make_consumer(bootstrap, group=f"corr-e2e-{ns}",
                                      auto_offset_reset="earliest"),
            engine=engine, topic_raw=raw_topic, topic_correction=corr_topic,
            topic_agent=f"test.agent.{ns}", topic_prediction=f"test.pred.{ns}",
            limits={gk: {"center": 100.0, "sigma": 10.0, "ucl": 130.0, "lcl": 70.0,
                         "method": "sigma", "limit_version": "v1"}}, whitelist={gk})
        t = threading.Thread(target=c.run, daemon=True)
        t.start()
        _time.sleep(5)                              # 구독·파티션 배정 대기

        # 실 Producer로 fdc.correction 발행 (correction_id 포함)
        p = Producer({"bootstrap.servers": bootstrap})
        p.produce(corr_topic, value=json.dumps(
            {"correction_type": "limit", "correction_id": cid}).encode())
        p.flush(10)

        # reload가 v2를 반영할 때까지 대기
        deadline = _time.time() + 45
        while _time.time() < deadline and c.limits.get(gk, {}).get("limit_version") != "v2":
            _time.sleep(0.3)
        c.request_stop()
        t.join(timeout=15)

        # 검증: 실제 관통
        assert c.limits[gk]["limit_version"] == "v2", "reload가 새 버전 반영 실패"
        assert c.limits[gk]["center"] == 106.0
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT limit_version FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar_one() == "v2"
            assert conn.execute(text(
                "SELECT status FROM limit_corrections WHERE correction_id=:cid"),
                {"cid": cid}).scalar_one() == "APPROVED_APPLIED"
    finally:
        if c is not None:
            c.request_stop()
        if t is not None:
            t.join(timeout=10)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM control_limits WHERE chamber_id=:ch"), {"ch": ch})
            conn.execute(text("DELETE FROM limit_corrections WHERE chamber_id=:ch"), {"ch": ch})
        engine.dispose()
        try:
            admin.delete_topics([corr_topic, raw_topic])
        except Exception:  # noqa: BLE001
            pass


# ── B4-2 라이브 배선: _persist_tttm (tttm_comparisons 적재) ──────────

def _rollup(**over):
    """엔진 chamber_rollup 산출 dict (8키 — rollups_to_rows가 요구, suspect_sensors 포함)."""
    r = {"reference": "fleet_median", "chamber_id": "SIM_CH_1", "reference_id": "v1",
         "score": 3.4, "top_gap_sensor": "C11", "gap_pct": 12.5, "reference_suspect": False,
         "suspect_sensors": []}
    r.update(over)
    return r


def test_persist_calls_writer_in_transaction(monkeypatch):
    """정상 rollup → rollups_to_rows → write_comparisons를 engine.begin() 트랜잭션에서."""
    calls = []
    monkeypatch.setattr(sc, "write_comparisons", lambda conn, rows: calls.append((conn, rows)))
    c, _ = _make([], engine=FakeEngine())
    c._persist_tttm(_rollup())
    assert c.engine.begins == 1                       # 호출자=commit 트랜잭션
    assert len(calls) == 1
    conn, rows = calls[0]
    assert conn == "CONN" and len(rows) == 1
    assert rows[0]["chamber_id"] == "SIM_CH_1" and rows[0]["tttm_score"] == 3.4


def test_persist_skips_when_engine_none(monkeypatch):
    """L2 — engine 미주입(순수 테스트) → skip, write 미호출(DB 없이 기존 테스트 통과)."""
    calls = []
    monkeypatch.setattr(sc, "write_comparisons", lambda conn, rows: calls.append(1))
    c, _ = _make([])                                  # engine=None 기본
    c._persist_tttm(_rollup())
    assert calls == []


def test_persist_skips_when_tttm_obj_none():
    """평가불가(None) → 적재할 것 없음, 트랜잭션 안 엶."""
    c, _ = _make([], engine=FakeEngine())
    c._persist_tttm(None)
    assert c.engine.begins == 0


def test_persist_swallows_write_exception(monkeypatch):
    """L1 best-effort — write 실패 삼킴(예외 전파 X). 적재 실패 ≠ 판정 실패."""
    def boom(conn, rows):
        raise RuntimeError("DB down")

    monkeypatch.setattr(sc, "write_comparisons", boom)
    c, _ = _make([], engine=FakeEngine())
    c._persist_tttm(_rollup())                        # raise 안 하면 통과


def test_collector_runs_before_persist(monkeypatch):
    """L4 — 실시간 alert(collector) 먼저, 이력 적재(persist) 마지막."""
    order = []
    c, _ = _fan(nelson=FakeNelson([]), tttm=FakeTTTM(rollup=_rollup()))
    c.collector = lambda x: order.append("collector")
    c._persist_tttm = lambda obj: order.append("persist")
    c._process_wafer(_wafer_rows(3))
    assert order == ["collector", "persist"]


def test_collector_exception_isolated_persist_and_counter():
    """collector(오케스트레이터) 예외 격리 — 전파 X · 실패 카운터 · persist 계속 (§4-1 P1·개정안1-1).

    체인 예외가 _persist_tttm 스킵·_flush except행(오프셋 미커밋→대량 리플레이)로 번지면 안 된다.
    """
    order = []
    c, _ = _fan(nelson=FakeNelson([]), tttm=FakeTTTM(rollup=_TTTM_OBJ))

    def _boom(_):
        raise RuntimeError("orchestrator down")

    c.collector = _boom
    c._persist_tttm = lambda obj: order.append("persist")
    c._process_wafer(_wafer_rows(3))                 # 전파되면 여기서 에러(격리 실패)
    assert order == ["persist"]                      # collector 실패해도 persist 도달
    assert c.stats["collector_failed"] == 1


def test_persist_failure_still_commits_offset(monkeypatch):
    """L1 헤드라인 — persist 실패해도 _flush가 오프셋 커밋에 도달(로그가 SPC 소비 안 막음)."""
    def boom(conn, rows):
        raise RuntimeError("DB down")

    monkeypatch.setattr(sc, "write_comparisons", boom)
    fake = FakeConsumer()
    c = sc.SpcConsumer(consumer=fake, engine=FakeEngine(), nelson=FakeNelson([]),
                       tttm=FakeTTTM(rollup=_rollup()), idle_flush_sec=30.0,
                       clock=FakeClock(), offset_factory=lambda t, p, o, m=None: [(t, p, o + 1)])
    c._handle_raw(_row(wafer_id="W1"), FakeMsg(_row(), offset=5))
    c._flush("SIM_CH_1")
    assert fake.commits == [[("fdc.raw", 0, 6)]]    # persist 실패해도 커밋


# ── 실 DB (skipif) — ⚠️ commit + teardown (Task 3 롤백 트릭 불가) ────

_TTTM_TEST_CH = "TEST_CH_TTTM_PERSIST"


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")
def test_persist_tttm_e2e_writes_row():
    """관통: _persist_tttm → tttm_comparisons 실제 1행 적재(값·bool 왕복). commit → DELETE 청소."""
    from sqlalchemy import text

    from src.agent_b_spc.seed_control_limits import make_engine

    engine = make_engine()
    ch = _TTTM_TEST_CH
    rollup = {"reference": "fleet_median", "chamber_id": ch, "reference_id": "v3",
              "score": 4.1, "top_gap_sensor": "C62", "gap_pct": None, "reference_suspect": True,
              "suspect_sensors": ["C17"]}
    try:
        with engine.begin() as conn:                  # DELETE-before (직전 크래시 잔여 청소)
            conn.execute(text("DELETE FROM tttm_comparisons WHERE chamber_id=:ch"), {"ch": ch})
            cl_before = conn.execute(text("SELECT count(*) FROM control_limits")).scalar()

        c = sc.SpcConsumer(consumer=FakeConsumer([]), engine=engine)
        c._persist_tttm(rollup)

        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT chamber_id, reference_type, reference_id, tttm_score, top_gap_sensor, "
                "gap_pct, is_reference_suspect, suspect_sensors "
                "FROM tttm_comparisons WHERE chamber_id=:ch"),
                {"ch": ch}).mappings().all()
            assert len(rows) == 1
            r = rows[0]
            assert r["reference_type"] == "fleet_median" and r["reference_id"] == "v3"
            assert r["tttm_score"] == 4.1 and r["top_gap_sensor"] == "C62"
            assert r["gap_pct"] is None                # None → NULL → None 왕복
            assert r["is_reference_suspect"] is True    # bool 왕복
            assert r["suspect_sensors"] == ["C17"]      # JSONB list 왕복(decouple)
            cl_after = conn.execute(text("SELECT count(*) FROM control_limits")).scalar()
            assert cl_after == cl_before                # control_limits 무변경
    finally:
        with engine.begin() as conn:                  # DELETE-after (성공·실패 무관)
            conn.execute(text("DELETE FROM tttm_comparisons WHERE chamber_id=:ch"), {"ch": ch})
        engine.dispose()


@pytest.mark.skipif(not _db_available(), reason="DATABASE_URL 미접속/미sync — 통합테스트 skip")
def test_persist_tttm_appends_across_calls():
    """L5 append-only — 웨이퍼마다 rollup 1개(chamber_rollup=최악 1건)라 호출당 1행이지만,
    여러 웨이퍼(여러 호출)에 걸쳐 누적된다(덮어쓰기 아님).
    """
    from sqlalchemy import text

    from src.agent_b_spc.seed_control_limits import make_engine

    engine = make_engine()
    ch = _TTTM_TEST_CH
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tttm_comparisons WHERE chamber_id=:ch"), {"ch": ch})

        c = sc.SpcConsumer(consumer=FakeConsumer([]), engine=engine)
        for score in (3.4, 4.1, 2.9):                 # 웨이퍼 3장 상당 (호출 3회)
            c._persist_tttm({"reference": "fleet_median", "chamber_id": ch,
                             "reference_id": "v1", "score": score, "top_gap_sensor": "C11",
                             "gap_pct": 10.0, "reference_suspect": False,
                             "suspect_sensors": []})

        with engine.connect() as conn:
            scores = [r[0] for r in conn.execute(text(
                "SELECT tttm_score FROM tttm_comparisons WHERE chamber_id=:ch ORDER BY id"),
                {"ch": ch}).all()]
        assert scores == [3.4, 4.1, 2.9]              # 3행 누적 (append-only, 순서 보존)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tttm_comparisons WHERE chamber_id=:ch"), {"ch": ch})
        engine.dispose()


# ═══════════════ B6-3-c: 가한계 라이브 배선 ═══════════════
#
# a(ProvisionalMode)·b(establish) 순수 코어를 컨슈머에 배선. seam(writer·tttm·reload)은
# fake 주입, ProvisionalMode는 실물(순수 코어). 이벤트는 mock(QualVerdictConfirmed 등).

from src.agent_b_spc.provisional_mode import ProvisionalMode, ProvisionalConfig, Phase


class _FakeTTTM:
    """set_excluded seam만 — c는 union 전달, TTTM 제외 반영."""

    def __init__(self):
        self.excluded: set = set()

    def set_excluded(self, s):
        self.excluded = set(s)


class _FakeProvWriter:
    """write_provisional seam — provisional 관리선 write 캡처(무DB)."""

    def __init__(self):
        self.writes: list = []

    def write_provisional(self, rows):
        self.writes.append(rows)


def _prov(tttm=None, writer=None, reloads=None):
    """실 ProvisionalMode + fake seam. reloads=호출 카운터 list."""
    cfg = ProvisionalConfig.load()
    return ProvisionalMode(cfg, writer=writer or _FakeProvWriter(),
                           reload_fn=(lambda: reloads.append(1)) if reloads is not None else (lambda: None),
                           tttm=tttm or _FakeTTTM(), persist=None)


def test_c_subscribes_four_topics():
    """구독 4토픽 — fdc.raw·fdc.correction·fdc.agent·fdc.prediction(B4-1 Step3b)."""
    c, fake = _make([], provisional=_prov())
    c.run()
    assert set(fake.subscribed) == {"fdc.raw", "fdc.correction", "fdc.agent", "fdc.prediction"}


def test_c_get_phase_delegates_to_provisional():
    """get_phase는 ProvisionalMode에 위임 (B4-1 억제 게이트 입력)."""
    prov = _prov()
    c, _ = _make([], provisional=prov)
    assert c.get_phase("SIM_CH_1") == Phase.NORMAL      # 미지 챔버
    prov.on_pm("SIM_CH_1", 5)                            # → PHASE_0
    assert c.get_phase("SIM_CH_1") == Phase.PHASE_0


def test_c_get_phase_normal_without_provisional():
    """provisional 미주입(순수 D0-2)이면 항상 NORMAL — 억제 없음."""
    c, _ = _make([])
    assert c.get_phase("SIM_CH_1") == Phase.NORMAL


# ── c-2: fdc.raw 배선 (on_pm↑·on_wafer·backward 버퍼) ─────────────────

import dataclasses as _dc

from src.agent_b_spc.establish_engine import EstablishConfig, WaferSample
from src.agent_b_spc.nelson_engine import Point as _Point

_GKC = ("SIM_CH_1", "C6_0", 4, "settled", "C11")


def _pcfg(seasoning_loud=2, W=200, M=2, settle_min=600):
    # B6-3-e settle 필드 — 기본 W=200으로 c 테스트는 폴백 종료(floor 미도달=현행). 조기정착 테스트만 작은 W.
    return ProvisionalConfig(provisional_k=5.0, seasoning_quiet=1,
                             seasoning_loud=seasoning_loud, k_sigma=3.0,
                             settle_window_wafers=W, settle_consecutive_m=M,
                             settle_min_wafers=settle_min, settle_block_min_fill=0.5, deadband_k_se=2.0)


def _ecfg(seasoning_loud=2, rolling_window_n=3, min_establishment_wafers=2,
          min_group_n=None, snapshot_min_n=None, seasoning_quiet=None):
    kw = dict(seasoning_loud=seasoning_loud, rolling_window_n=rolling_window_n,
              min_establishment_wafers=min_establishment_wafers)
    if min_group_n is not None:
        kw["min_group_n"] = min_group_n
    if snapshot_min_n is not None:
        kw["snapshot_min_n"] = snapshot_min_n
    if seasoning_quiet is not None:
        kw["seasoning_quiet"] = seasoning_quiet
    return _dc.replace(EstablishConfig.load(), **kw)


def _pts(val, wafer, gk=_GKC):
    return [_Point(gk, wafer, "2026-07-20T01:00:00+00:00", float(val), 0, "v1")]


def _prov2(sl=2):
    return ProvisionalMode(_pcfg(sl), writer=_FakeProvWriter(),
                           reload_fn=lambda: None, tttm=_FakeTTTM(), persist=None)


def _cons_prov(sl=2, rw=3, mew=2):
    prov = _prov2(sl)
    c, _ = _make([], provisional=prov, establish_cfg=_ecfg(sl, rw, mew))
    return c, prov


def test_c_first_wafer_is_baseline_not_pm():
    """첫 wafer는 베이스라인(on_pm 미호출) — 정상 챔버가 PHASE_0 오진입 안 함."""
    c, prov = _cons_prov()
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))
    assert c.get_phase("SIM_CH_1") == Phase.NORMAL       # 베이스라인만


def test_c_pm_increase_enters_phase0():
    """pm_count↑ 감지 → on_pm → PHASE_0."""
    c, prov = _cons_prov()
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))   # 베이스라인
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))   # PM↑
    assert c.get_phase("SIM_CH_1") == Phase.PHASE_0


def test_c_backward_buffers_only_settled():
    """정착 버퍼는 post_pm_count > seasoning_loud(과도 제외)만."""
    c, prov = _cons_prov(sl=2, rw=10)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))   # 베이스라인
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))   # PM↑ → PHASE_0
    prov.on_qual_verdict("SIM_CH_1", "loud")                     # → PHASE_1
    # post_pm: W1=1, 이후 W2..W6 = 2..6. seasoning_loud=2 → post_pm>2(=3,4,5,6)만 버퍼
    for i in range(2, 7):
        c._drive_provisional("SIM_CH_1", f"W{i}", 4, _pts(100 + i, f"W{i}"))
    buf = c._backward["SIM_CH_1"]
    ppcs = [w.post_pm_count for w in buf]
    assert min(ppcs) > 2                                         # 과도(≤2) 제외
    assert all(isinstance(w, WaferSample) for w in buf)


def test_c_backward_buffer_bounded_to_window():
    """버퍼는 rolling_window_n으로 바운드 (인메모리 결정3)."""
    c, prov = _cons_prov(sl=1, rw=3)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))
    prov.on_qual_verdict("SIM_CH_1", "loud")
    for i in range(2, 12):                                       # 많이 유입
        c._drive_provisional("SIM_CH_1", f"W{i}", 4, _pts(100, f"W{i}"))
    assert len(c._backward["SIM_CH_1"]) == 3                     # maxlen=rw


def test_c_new_pm_resets_buffer():
    """새 PM(pm_count↑) → 버퍼 리셋 (직전 레짐 값 오염 방지)."""
    c, prov = _cons_prov(sl=1, rw=10)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))
    prov.on_qual_verdict("SIM_CH_1", "loud")
    for i in range(2, 6):
        c._drive_provisional("SIM_CH_1", f"W{i}", 4, _pts(100, f"W{i}"))
    assert len(c._backward["SIM_CH_1"]) > 0
    c._drive_provisional("SIM_CH_1", "WN", 5, _pts(200, "WN"))   # 새 PM
    assert len(c._backward["SIM_CH_1"]) == 0                     # 리셋


def test_c_phase2_signal_marks_pending():
    """정착 경계 도달 → PHASE2_SIGNAL → 제안 pending 표시(Task5 재시도 대상)."""
    c, prov = _cons_prov(sl=2, rw=10)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))
    prov.on_qual_verdict("SIM_CH_1", "loud")
    for i in range(2, 5):                                        # post_pm 2,3 → boundary 2 도달
        c._drive_provisional("SIM_CH_1", f"W{i}", 4, _pts(100, f"W{i}"))
    assert "SIM_CH_1" in c._phase2_pending
    assert c.get_phase("SIM_CH_1") == Phase.AWAITING_PHASE2


# ── Step7 S5: _post_pm 단일화 + _recalc_buf 적재 (조용 정기 리캘리 backward) ──────

def _cons_sq(sq=1, rw=10, sl=2):
    """seasoning_quiet 오버라이드 소비자 (Step7 정기 리캘리 backward 테스트용)."""
    prov = _prov2(sl)
    c, _ = _make([], provisional=prov, establish_cfg=_ecfg(sl, rw, seasoning_quiet=sq))
    return c, prov


def test_c_post_pm_counts_in_normal():
    """S5 단일화 — NORMAL(무PM)에서도 _post_pm 증가(구: 요란 전용). 첫 wafer=베이스라인도 카운트."""
    c, _ = _cons_sq(sq=10, rw=10)                    # sq 커서 버퍼 미적재, 카운터만 관찰
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))
    c._drive_provisional("SIM_CH_1", "W1", 3, _pts(100, "W1"))
    assert c._post_pm["SIM_CH_1"] == 2               # NORMAL 카운트
    assert c.get_phase("SIM_CH_1") == Phase.NORMAL   # PM 없어 NORMAL 유지


def test_c_recalc_buf_fills_in_normal_after_seasoning():
    """조용 구간에서 _post_pm > seasoning_quiet 부터 _recalc_buf 적재(head skip·D3)."""
    c, _ = _cons_sq(sq=2, rw=10)
    for i in range(5):                               # _post_pm 1..5, sq=2 → 3·4·5만 적재
        c._drive_provisional("SIM_CH_1", f"W{i}", 3, _pts(100 + i, f"W{i}"))
    buf = c._recalc_buf["SIM_CH_1"]
    assert len(buf) == 3
    assert all(_GKC in row for row in buf)           # {group_key: value} dict


def test_c_recalc_buf_empty_points_increments_but_no_append():
    """🔴 빈 points → _post_pm는 증가하되 버퍼는 skip, return 금지(무크래시·signal 경로 유지)."""
    c, _ = _cons_sq(sq=0, rw=10)                     # sq=0 → 전 wafer 적재 자격
    c._drive_provisional("SIM_CH_1", "W0", 3, [])    # 감시 밖 wafer(빈 points)
    assert c._post_pm["SIM_CH_1"] == 1               # 카운터는 증가
    assert len(c._recalc_buf.get("SIM_CH_1", [])) == 0   # 버퍼는 skip
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))   # 이후 PM 정상 동작(경로 안 죽음)
    assert c.get_phase("SIM_CH_1") == Phase.PHASE_0


def test_c_recalc_buf_setdefault_no_keyerror():
    """🔴 fresh _recalc_buf(빈 dict) → setdefault로 KeyError 0 (재시작 안전)."""
    c, _ = _cons_sq(sq=0, rw=10)
    assert c._recalc_buf == {}                       # 시작은 빈 dict
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))   # KeyError 없이 생성
    assert len(c._recalc_buf["SIM_CH_1"]) == 1


def test_c_loud_fills_backward_not_recalc_buf():
    """무회귀 — 요란(non-NORMAL)은 여전히 _backward, _recalc_buf엔 안 들어감."""
    c, _ = _cons_sq(sq=1, rw=10, sl=1)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))   # 베이스라인 NORMAL
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))   # PM↑ → PHASE_0
    for i in range(2, 6):
        c._drive_provisional("SIM_CH_1", f"W{i}", 4, _pts(100, f"W{i}"))
    assert len(c._backward["SIM_CH_1"]) > 0          # 요란 → backward
    assert "SIM_CH_1" not in c._recalc_buf           # recalc_buf 미적재


def test_c_recalc_buf_popped_on_pm():
    """PM 감지 시 _recalc_buf.pop — 옛 레짐 조용 표본 폐기(D3)."""
    c, _ = _cons_sq(sq=1, rw=10)
    for i in range(4):
        c._drive_provisional("SIM_CH_1", f"W{i}", 3, _pts(100, f"W{i}"))
    assert len(c._recalc_buf["SIM_CH_1"]) > 0
    c._drive_provisional("SIM_CH_1", "WPM", 4, _pts(100, "WPM"))   # PM↑
    assert "SIM_CH_1" not in c._recalc_buf            # 폐기


def test_c_recalc_buf_dedup_redelivery():
    """재전달(같은 wafer) → 중복 카운트·적재 없음(dedup)."""
    c, _ = _cons_sq(sq=0, rw=10)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))
    n1 = len(c._recalc_buf["SIM_CH_1"])
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))     # 재전달
    assert len(c._recalc_buf["SIM_CH_1"]) == n1


def test_c_recalc_buf_bounded_to_window():
    """maxlen = rolling_window_n (인메모리 바운드)."""
    c, _ = _cons_sq(sq=0, rw=3)
    for i in range(6):
        c._drive_provisional("SIM_CH_1", f"W{i}", 3, _pts(100, f"W{i}"))
    assert len(c._recalc_buf["SIM_CH_1"]) == 3


# ── Step7 S6c: _reload_after_commit + 재시도 최소 간격 (D5) ──────

class _Clk:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_c_reload_after_commit_success(monkeypatch):
    """load 성공 → reload_limits 호출·_reload_pending False."""
    c, _ = _make([], clock=_Clk())
    got = {}
    monkeypatch.setattr(sc.control_limits_loader, "load", lambda e: ({"x": 1}, set()))
    monkeypatch.setattr(c, "reload_limits", lambda lim: got.update(lim=lim))
    c._reload_pending = True
    c._reload_after_commit()
    assert got["lim"] == {"x": 1} and c._reload_pending is False


def test_c_reload_after_commit_failure_sets_pending(monkeypatch):
    """load 실패(DB down) → _reload_pending True·무크래시(루프 생존)."""
    c, _ = _make([], clock=_Clk(5.0))

    def _boom(_e):
        raise RuntimeError("db down")
    monkeypatch.setattr(sc.control_limits_loader, "load", _boom)
    c._reload_after_commit()
    assert c._reload_pending is True and c._last_reload_try_at == 5.0


def test_c_retry_reload_respects_min_interval(monkeypatch):
    """🔴 매 wafer 재시도 금지 — 최소 간격 전엔 load 미호출, 경과 후에만 재시도."""
    clk = _Clk()
    c, _ = _make([], clock=clk)
    calls = []
    monkeypatch.setattr(sc.control_limits_loader, "load",
                        lambda e: (calls.append(1), ({}, set()))[1])
    monkeypatch.setattr(c, "reload_limits", lambda lim: None)
    c._reload_pending = True
    c._last_reload_try_at = 0.0
    clk.t = 10.0                                  # < 30s
    c._retry_reload_if_pending()
    assert calls == []                            # 간격 미달 → 재시도 안 함
    clk.t = 40.0                                  # ≥ 30s
    c._retry_reload_if_pending()
    assert calls == [1] and c._reload_pending is False


def test_c_retry_reload_noop_when_not_pending(monkeypatch):
    """보류 없으면 no-op (load 미호출)."""
    c, _ = _make([], clock=_Clk(1000.0))
    calls = []
    monkeypatch.setattr(sc.control_limits_loader, "load", lambda e: calls.append(1))
    c._reload_pending = False
    c._retry_reload_if_pending()
    assert calls == []


# ── Step7 S6b/S6e: _maybe_periodic_recalc 3분기·게이팅 + 트리거·kill switch ──────

from collections import deque as _deque

from sqlalchemy import create_engine as _ce

_GKR = ("SIM_CH_1", "C6_0", 4, "settled", "C11")


class _TTk:
    k_sigma = 3.0


class _Prop:
    def __init__(self, decision):
        self.decision = decision


def _sqlite():
    return _ce("sqlite://")


def _setup_recalc(monkeypatch, decision, blocked=False, proposed=None):
    """오케스트레이션 격리 — recompute/apply/record/reset_ref/게이팅을 spy·주입."""
    c, _ = _make([], engine=_sqlite(), clock=_Clk())
    c.tttm = _TTk()
    c._recalc_cfg = object()
    c._resolutions = {}
    c.limits = {_GKR: {"center": 100.0, "method": "sigma"}}
    c._recalc_buf["SIM_CH_1"] = _deque([{_GKR: 100.0}, {_GKR: 101.0}])
    calls = {"auto": [], "prop": [], "reload": 0}
    monkeypatch.setattr(sc, "reset_ref",
                        lambda conn, g, k: {"center": 100.0, "sigma_ref": 10.0, "limit_version": "v1"})
    monkeypatch.setattr(sc, "recompute_group", lambda *a: _Prop(decision))
    monkeypatch.setattr(sc, "_incident_blocked", lambda conn, ch: blocked)
    monkeypatch.setattr(sc, "_proposed_groups", lambda conn, ch: proposed or set())
    monkeypatch.setattr(sc, "apply_auto", lambda conn, p: calls["auto"].append(p))
    monkeypatch.setattr(sc, "record_proposed",
                        lambda conn, p, **kw: calls["prop"].append((p, kw.get("shadow_far_pct"))))
    monkeypatch.setattr(c, "_reload_after_commit",
                        lambda: calls.__setitem__("reload", calls["reload"] + 1))
    return c, calls


def test_c_recalc_auto_applied_applies_and_reloads(monkeypatch):
    """auto_applied → apply_auto + 커밋 후 리로드."""
    c, calls = _setup_recalc(monkeypatch, "auto_applied")
    c._maybe_periodic_recalc("SIM_CH_1")
    assert len(calls["auto"]) == 1 and calls["reload"] == 1 and calls["prop"] == []


def test_c_recalc_needs_approval_records_no_reload(monkeypatch):
    """needs_approval → record_proposed(PROPOSED 기록)·리로드 없음."""
    c, calls = _setup_recalc(monkeypatch, "needs_approval")
    c._maybe_periodic_recalc("SIM_CH_1")
    assert len(calls["prop"]) == 1 and calls["reload"] == 0 and calls["auto"] == []


def test_c_recalc_no_change_no_writes(monkeypatch):
    """no_change → 쓰기·리로드 없음."""
    c, calls = _setup_recalc(monkeypatch, "no_change")
    c._maybe_periodic_recalc("SIM_CH_1")
    assert calls["auto"] == [] and calls["prop"] == [] and calls["reload"] == 0


def test_c_recalc_g3_blocked_skips_all(monkeypatch):
    """G3(미종결 incident) → recompute 진입 전 skip."""
    c, calls = _setup_recalc(monkeypatch, "auto_applied", blocked=True)
    c._maybe_periodic_recalc("SIM_CH_1")
    assert calls["auto"] == [] and calls["prop"] == []


def test_c_recalc_g2_group_skipped(monkeypatch):
    """G2 — PROPOSED 미결 그룹은 자기 경로 중복 방지로 skip."""
    c, calls = _setup_recalc(monkeypatch, "auto_applied", proposed={_GKR[1:]})
    c._maybe_periodic_recalc("SIM_CH_1")
    assert calls["auto"] == []


def test_c_recalc_g1_non_normal_skips(monkeypatch):
    """G1 — phase가 NORMAL 아니면 skip (provisional 주입 후 PM으로 PHASE_0)."""
    c, calls = _setup_recalc(monkeypatch, "auto_applied")
    prov = _prov2(2)
    c.provisional = prov
    prov.on_pm("SIM_CH_1", 4)                         # → PHASE_0
    c._maybe_periodic_recalc("SIM_CH_1")
    assert calls["auto"] == []


def test_c_trigger_fires_at_stagger_interval(monkeypatch):
    """트리거 — 카운터가 스태거 임계(첫 발동=interval+0) 도달 시 발동·0 리셋."""
    c, _ = _make([], engine=_sqlite(), clock=_Clk())
    c._periodic_enabled = True
    c._recalc_interval = 3
    c.limits = {_GKR: {}}
    fired = []
    monkeypatch.setattr(c, "_maybe_periodic_recalc", lambda ch: fired.append(ch))
    for _ in range(3):
        c._trigger_periodic_recalc("SIM_CH_1")
    assert fired == ["SIM_CH_1"] and c._recalc_count["SIM_CH_1"] == 0
    assert c._first_fire_done["SIM_CH_1"] is True


def test_c_trigger_kill_switch_off_never_fires(monkeypatch):
    """kill switch off → 트리거 발동 0 (매 wafer no-op)."""
    c, _ = _make([], engine=_sqlite(), clock=_Clk())
    c._periodic_enabled = False
    c._recalc_interval = 3
    c.limits = {_GKR: {}}
    fired = []
    monkeypatch.setattr(c, "_maybe_periodic_recalc", lambda ch: fired.append(ch))
    for _ in range(10):
        c._trigger_periodic_recalc("SIM_CH_1")
    assert fired == []


def test_c_trigger_noop_when_engine_none(monkeypatch):
    """engine 미주입 → 트리거 skip (순수 구성)."""
    c, _ = _make([], clock=_Clk())                   # engine=None
    c._periodic_enabled = True
    c._recalc_interval = 1
    fired = []
    monkeypatch.setattr(c, "_maybe_periodic_recalc", lambda ch: fired.append(ch))
    c._trigger_periodic_recalc("SIM_CH_1")
    assert fired == []


# ── Step7 D2: 장기 봉쇄 방치 경고 (_note_gate_block·관측 전용) ──────

from src.agent_b_spc.spc_consumer import _GATE_REWARN_SEC as _REWARN
from src.agent_b_spc.spc_consumer import _GATE_STALE_WARN_SEC as _STALE


def test_c_gate_stale_no_warn_before_threshold(caplog):
    """봉쇄 임계 미달 → 추적만·경고 없음."""
    clk = _Clk(100.0)
    c, _ = _make([], clock=clk)
    c._note_gate_block("SIM_CH_1", True)             # 봉쇄 시작
    clk.t = 100.0 + _STALE - 1
    with caplog.at_level("WARNING"):
        c._note_gate_block("SIM_CH_1", True)
    assert "SIM_CH_1" in c._gate_blocked_since
    assert not any("장기 봉쇄" in r.getMessage() for r in caplog.records)


def test_c_gate_stale_warns_after_threshold(caplog):
    """봉쇄 임계 초과 → WARNING 1건 (자동 해소 없음)."""
    clk = _Clk(0.0)
    c, _ = _make([], clock=clk)
    c._note_gate_block("SIM_CH_1", True)
    clk.t = _STALE + 10
    with caplog.at_level("WARNING"):
        c._note_gate_block("SIM_CH_1", True)
    assert any("장기 봉쇄" in r.getMessage() for r in caplog.records)


def test_c_gate_stale_throttled_within_rewarn_window(caplog):
    """재알림 간격 안에서는 경고 1건 — 매 wafer 도배 방지."""
    clk = _Clk(0.0)
    c, _ = _make([], clock=clk)
    c._note_gate_block("SIM_CH_1", True)
    clk.t = _STALE + 10
    with caplog.at_level("WARNING"):
        c._note_gate_block("SIM_CH_1", True)
        c._note_gate_block("SIM_CH_1", True)     # 같은 시각 — 재알림 간격 미달
    assert sum("장기 봉쇄" in r.getMessage() for r in caplog.records) == 1


def test_c_gate_stale_rewarns_after_interval(caplog):
    """#137 회신 — 재알림 간격이 지나면 **다시** 경고한다.

    구 동작(챔버당 1회 throttle)은 자동 해소가 없는 **영구 봉쇄를 조용하게** 만들었다.
    장기 상주 프로세스에서 한 번만 뜨고 마는데, 그때가 정확히 경고가 필요한 상황이다.
    """
    clk = _Clk(0.0)
    c, _ = _make([], clock=clk)
    c._note_gate_block("SIM_CH_1", True)
    clk.t = _STALE + 10
    with caplog.at_level("WARNING"):
        c._note_gate_block("SIM_CH_1", True)          # 1회차
        clk.t += _REWARN - 1
        c._note_gate_block("SIM_CH_1", True)          # 간격 미달 — 안 뜸
        clk.t += 2                                    # 간격 초과
        c._note_gate_block("SIM_CH_1", True)          # 2회차
    assert sum("장기 봉쇄" in r.getMessage() for r in caplog.records) == 2


def test_c_gate_stale_rewarn_does_not_add_db_queries(caplog):
    """재알림을 넣어도 **DB 재조회 빈도는 안 늘어난다** (#137 회신에서 확인한 전제).

    재알림 게이트가 재조회 게이트보다 **앞**이라, 최근 경고한 챔버는 조회 자체를 건너뛴다.
    """
    clk = _Clk(0.0)
    c, _ = _make([], clock=clk)
    calls = []
    c._gate_block_elapsed_sec = lambda ch: (calls.append(ch), _STALE + 10)[1]
    c._note_gate_block("SIM_CH_1", True)
    clk.t = _STALE + 10
    c._note_gate_block("SIM_CH_1", True)              # 조회 1회 + 경고
    n_after_first = len(calls)
    for _ in range(5):                                # 재알림 간격 안에서 반복 호출
        clk.t += 1
        c._note_gate_block("SIM_CH_1", True)
    assert len(calls) == n_after_first                # 조회 증가 없음


def test_c_gate_block_cleared_on_progress():
    """봉쇄 풀림(진행) → 추적·경고 리셋."""
    c, _ = _make([], clock=_Clk(5.0))
    c._note_gate_block("SIM_CH_1", True)
    c._gate_warned["SIM_CH_1"] = 5.0
    assert "SIM_CH_1" in c._gate_blocked_since
    c._note_gate_block("SIM_CH_1", False)
    assert "SIM_CH_1" not in c._gate_blocked_since and "SIM_CH_1" not in c._gate_warned


# ── D2 (2026-08-08): 봉쇄 경과를 **DB 기준**으로 잰다 ────────────────────────────
#   구현은 프로세스 메모리(`_gate_blocked_since`)로 쟀는데, 재시작마다 0 으로 리셋돼
#   3일 경고가 영영 안 떴다(실측: 4챔버 영구 봉쇄인데 경고 0건). 아래 3건이 그 회귀 가드다.

class _AgeEngine:
    """`_gate_block_elapsed_sec` 용 최소 엔진 stub — 쿼리 순서대로 값을 돌려준다."""

    def __init__(self, ages, boom=False):
        self._ages, self._boom = list(ages), boom      # [G3초, G2초] · None 허용
        self.connects = 0

    def connect(self):
        self.connects += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        if self._boom:
            raise RuntimeError("DB down")
        return _AgeResult(self._ages.pop(0) if self._ages else None)


class _AgeResult:
    def __init__(self, v):
        self._v = v

    def scalar(self):
        return self._v


def test_c_gate_stale_uses_db_age_not_process_memory(caplog):
    """★ 재시작 직후(메모리 0)여도 **DB 기준 경과**로 경고가 뜬다.

    구 동작이면 `self.clock()` 기준이라 방금 시작한 프로세스는 elapsed=0 → 영영 무경고.
    """
    c, _ = _make([], clock=_Clk(0.0))
    c.engine = _AgeEngine([_STALE + 86400.0, None])    # G3 4일째 · G2 해당 없음
    with caplog.at_level("WARNING"):
        c._note_gate_block("SIM_CH_1", True)           # 시계 진행 없이 첫 호출
    msgs = [r.getMessage() for r in caplog.records]
    assert any("gate_stale" in m and "장기 봉쇄" in m for m in msgs)
    assert "SIM_CH_1" in c._gate_warned
    assert "SIM_CH_1" not in c._gate_blocked_since     # DB 경로면 메모리 추적은 안 쓴다


def test_c_gate_stale_takes_older_of_g2_g3():
    """G2·G3 둘 다 막고 있으면 **더 오래된 쪽**이 방치 기간이다."""
    c, _ = _make([], clock=_Clk(0.0))
    c.engine = _AgeEngine([100.0, 900.0])              # G3 100s · G2 900s
    assert c._gate_block_elapsed_sec("SIM_CH_1") == 900.0


def test_c_gate_stale_db_failure_falls_back_to_memory(caplog):
    """조회 실패해도 리캘리는 계속된다 — 예외 전파 금지 + 메모리 폴백."""
    c, _ = _make([], clock=_Clk(0.0))
    c.engine = _AgeEngine([], boom=True)
    with caplog.at_level("WARNING"):
        c._note_gate_block("SIM_CH_1", True)
    assert "SIM_CH_1" in c._gate_blocked_since         # 폴백 경로로 추적
    assert "SIM_CH_1" not in c._gate_warned            # elapsed=0 이라 아직 경고 없음


def test_c_gate_stale_recheck_interval_throttles_db(caplog):
    """매 wafer DB 조회 방지 — 재조회 간격 미달이면 쿼리하지 않는다."""
    from src.agent_b_spc.spc_consumer import _GATE_STALE_RECHECK_SEC as _RECHECK
    clk = _Clk(0.0)
    c, _ = _make([], clock=clk)
    eng = _AgeEngine([1.0, None])
    c.engine = eng
    c._note_gate_block("SIM_CH_1", True)
    assert eng.connects == 1
    clk.t = _RECHECK - 1
    c._note_gate_block("SIM_CH_1", True)               # 간격 미달 — 조회 없음
    assert eng.connects == 1


def test_c_recalc_g3_block_tracked(monkeypatch):
    """G3 봉쇄가 _note_gate_block로 추적된다(통합)."""
    c, calls = _setup_recalc(monkeypatch, "auto_applied", blocked=True)
    c._maybe_periodic_recalc("SIM_CH_1")
    assert "SIM_CH_1" in c._gate_blocked_since and calls["auto"] == []


def test_c_recalc_progress_clears_block(monkeypatch):
    """정상 진행(auto) → 봉쇄 추적 없음/리셋."""
    c, calls = _setup_recalc(monkeypatch, "auto_applied")
    c._gate_blocked_since["SIM_CH_1"] = 0.0          # 이전 봉쇄 흔적
    c._maybe_periodic_recalc("SIM_CH_1")
    assert "SIM_CH_1" not in c._gate_blocked_since    # 진행으로 리셋


# ── Step7b: _eval_shadow (needs_approval 소급 채점·순수·DB 미접촉) ──────

class _SP:
    def __init__(self, center, ucl, lcl, sigma):
        self.new_center, self.new_ucl, self.new_lcl, self.new_sigma = center, ucl, lcl, sigma


_SHCUR = {"center": 100.0, "sigma": 10.0, "ucl": 130.0, "lcl": 70.0,
          "method": "sigma", "limit_version": "v1"}


def _shadow_buf(gk, vals, ver="v1"):
    from collections import deque as _dq
    return _dq([[_Point(gk, f"S{i}", f"2026-07-20T01:{i:02d}:00+00:00", float(v), 0, ver)]
                for i, v in enumerate(vals)])


def test_c_eval_shadow_returns_reduction():
    """구 관리선 한쪽 이탈(N2 발동)·신 관리선 감소 → far_pct 실수."""
    c, _ = _make([])
    c._shadow_buf[_GKC[0]] = _shadow_buf(_GKC, [108.0] * 15)   # 구 center 100 대비 전부 위 → N2
    far = c._eval_shadow(_GKC[0], _GKC, _SHCUR, _SP(108.0, 138.0, 78.0, 10.0))
    assert far is not None and far > 0


def test_c_eval_shadow_unknown_is_none(monkeypatch):
    """evaluate_shadow verdict=UNKNOWN(구 위반 0·측정 불가) → None(NULL·D4)."""
    c, _ = _make([])
    c._shadow_buf[_GKC[0]] = _shadow_buf(_GKC, [108.0] * 12)
    monkeypatch.setattr(sc, "evaluate_shadow",
                        lambda *a, **k: {"verdict": "UNKNOWN", "false_alarm_reduction_pct": 0.0})
    assert c._eval_shadow(_GKC[0], _GKC, _SHCUR, _SP(108.0, 138.0, 78.0, 10.0)) is None


def test_c_eval_shadow_small_sample_none():
    """표본 < _SHADOW_MIN_POINTS(10) → None."""
    c, _ = _make([])
    c._shadow_buf[_GKC[0]] = _shadow_buf(_GKC, [108.0] * 5)
    assert c._eval_shadow(_GKC[0], _GKC, _SHCUR, _SP(108.0, 138.0, 78.0, 10.0)) is None


def test_c_eval_shadow_version_mixed_none():
    """창 내 limit_version 혼재 → 잣대 불일치 skip → None."""
    c, _ = _make([])
    buf = _shadow_buf(_GKC, [108.0] * 10, ver="v1")
    buf.extend(_shadow_buf(_GKC, [108.0] * 5, ver="v2"))
    c._shadow_buf[_GKC[0]] = buf
    assert c._eval_shadow(_GKC[0], _GKC, _SHCUR, _SP(108.0, 138.0, 78.0, 10.0)) is None


def test_c_eval_shadow_empty_buf_none():
    """버퍼 없음(재시작) → None(무크래시)."""
    c, _ = _make([])
    assert c._eval_shadow("SIM_CH_9", _GKC, _SHCUR, _SP(108.0, 138.0, 78.0, 10.0)) is None


# ── c-3: fdc.agent 배선 (QualVerdictConfirmed → on_qual_verdict) ──────

_LIMC = {_GKC: {"center": 100.0, "sigma": 5.0, "ucl": 115.0, "lcl": 85.0,
                "method": "sigma", "limit_version": "v1"}}


def _prov3(sl=2):
    return ProvisionalMode(_pcfg(sl), limits=dict(_LIMC), writer=_FakeProvWriter(),
                           reload_fn=lambda: None, tttm=_FakeTTTM(), persist=None)


def _cons3(sl=2):
    prov = _prov3(sl)
    c, fake = _make([], provisional=prov, establish_cfg=_ecfg(sl))
    return c, prov, fake


def _qvc(chamber="SIM_CH_1", verdict="loud"):
    return {"event_type": "QualVerdictConfirmed", "chamber_id": chamber, "verdict": verdict}


def test_c_agent_loud_verdict_enters_phase1():
    """QualVerdictConfirmed loud → on_qual_verdict → PHASE_1 광폭."""
    c, prov, _ = _cons3()
    prov.on_pm("SIM_CH_1", 5)                          # PHASE_0
    c._handle_agent(_qvc(verdict="loud"))
    assert c.get_phase("SIM_CH_1") == Phase.PHASE_1


def test_c_agent_quiet_verdict_returns_normal():
    """QualVerdictConfirmed quiet → NORMAL 복귀(provisional 미진입)."""
    c, prov, _ = _cons3()
    prov.on_pm("SIM_CH_1", 5)
    c._handle_agent(_qvc(verdict="quiet"))
    assert c.get_phase("SIM_CH_1") == Phase.NORMAL


def test_c_agent_malformed_verdict_skipped():
    """verdict enum 밖(대문자·오타 등) → skip, 전이·크래시 없음(얇은 파서 경계)."""
    c, prov, _ = _cons3()
    prov.on_pm("SIM_CH_1", 5)
    c._handle_agent({"event_type": "QualVerdictConfirmed", "chamber_id": "SIM_CH_1",
                     "verdict": "LOUD"})               # 대문자 — 계약 위반
    assert c.get_phase("SIM_CH_1") == Phase.PHASE_0    # 무전이


def test_c_agent_missing_chamber_skipped():
    """chamber_id 부재 → skip(크래시 없음)."""
    c, prov, _ = _cons3()
    c._handle_agent({"event_type": "QualVerdictConfirmed", "verdict": "loud"})   # 크래시 안 나면 통과


def test_c_agent_unknown_event_ignored():
    """QualVerdictConfirmed·ChamberRequalified 외 이벤트(Brief 등) → 무시."""
    c, prov, _ = _cons3()
    prov.on_pm("SIM_CH_1", 5)
    c._handle_agent({"event_type": "SupervisorBrief", "chamber_id": "SIM_CH_1"})
    assert c.get_phase("SIM_CH_1") == Phase.PHASE_0    # 무전이


def test_c_agent_via_run_loop_commits():
    """run 루프: fdc.agent 메시지 처리 후 오프셋 커밋(at-least-once)."""
    prov = _prov3(2)
    prov.on_pm("SIM_CH_1", 5)                          # PHASE_0
    msg = FakeMsg(_qvc(verdict="loud"), topic="fdc.agent", offset=7)
    c, fake = _make([msg], provisional=prov, establish_cfg=_ecfg(2))
    c.run()
    assert c.get_phase("SIM_CH_1") == Phase.PHASE_1    # 배선 관통
    assert len(fake.commits) == 1                     # 커밋됨


# ── c-5: firm apply 확장 (제안·firm 후속 게이트·verify tally) ─────────

def test_c_verify_tally_accumulates_then_clears():
    """verify forward tally — N장 미만은 누적, N 도달 시 판정·정리."""
    c, prov = _cons_prov()
    c._verify_n = 4
    c._verify_max_pct = 30.0
    c._verify["SIM_CH_1"] = [0, 0]
    for f in (True, False, False):                     # 3장(<4) 누적
        c._tally_verify("SIM_CH_1", f)
    assert c._verify["SIM_CH_1"] == [3, 1]             # seen=3, flagged=1
    c._tally_verify("SIM_CH_1", False)                 # 4장째 → N 도달
    assert "SIM_CH_1" not in c._verify                 # 판정 후 정리


def test_c_verify_tally_counts_only_flagged():
    """flagged=False 위주면 오탐율 낮음 — tally는 위반 wafer만 센다."""
    c, prov = _cons_prov()
    c._verify_n = 2
    c._verify_max_pct = 5.0
    c._verify["SIM_CH_1"] = [0, 0]
    c._tally_verify("SIM_CH_1", False)
    assert c._verify["SIM_CH_1"][1] == 0               # 무위반


def test_c_firm_followup_skips_when_not_awaiting():
    """firm 후속은 AWAITING_PHASE2에서만 — PHASE_0/PHASE_1이면 no-op(잔여 group correction 차단)."""
    c, prov = _cons_prov()
    prov.on_pm("SIM_CH_1", 5)                          # PHASE_0
    c._maybe_firm_followup("SIM_CH_1", "incident")     # DB 진입 전 게이트 반환(PHASE_0)
    assert "SIM_CH_1" not in c._verify                 # 후속 미실행


def test_c_firm_followup_noop_without_provisional():
    """provisional 미주입(순수 D0-2)이면 firm 후속 no-op."""
    c, _ = _make([])
    c._maybe_firm_followup("SIM_CH_1", "incident")     # 크래시 없으면 통과


# ── c-6: E2E (요란→가한계→정착→제안→firm apply→후속→복귀) 실 DB skipif ──

import os as _os

from dotenv import load_dotenv as _load_dotenv
from sqlalchemy import create_engine as _create_engine, text as _text

from src.agent_b_spc import control_limits_loader
from src.agent_b_spc.seed_control_limits import make_engine as _make_engine
from src.agent_b_spc.provisional_seams import ProvisionalWriter
from src.agent_b_spc.tttm_engine import TTTMEngine


def _e2e_db_available() -> bool:
    try:
        _load_dotenv()
        url = _os.environ.get("DATABASE_URL")
        if not url:
            return False
        probe = _create_engine(url, connect_args={"connect_timeout": 3})
        try:
            with probe.connect() as c:
                return c.execute(_text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name='y_thresholds'")).scalar() == 1
        finally:
            probe.dispose()
    except Exception:  # noqa: BLE001
        return False


_E2E_CH = "TEST_CH_E2E_C"
_E2E_GK = (_E2E_CH, "C6_0", 4, "settled", "C11")


def _e2e_clean(engine):
    with engine.begin() as conn:
        for tbl in ("control_limits", "limit_corrections", "qual_snapshots",
                    "y_thresholds", "wafer_predictions"):
            conn.execute(_text(f"DELETE FROM {tbl} WHERE chamber_id=:ch"), {"ch": _E2E_CH})


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_c_e2e_loud_to_firm_reestablish():
    """가한계 전 수명주기 관통 — provisional write→제안→firm apply→스냅샷·Y·복귀."""
    engine = _make_engine()
    ch, gk = _E2E_CH, _E2E_GK
    _e2e_clean(engine)
    try:
        with engine.begin() as conn:               # firm v1 + 예측분포 시드
            conn.execute(_text(
                "INSERT INTO control_limits (chamber_id,recipe_id,step,sensor_window,sensor_id,"
                "limit_version,method,center,sigma,ucl,lcl,k_sigma,q_low,q_high,trigger_type,is_active) "
                "VALUES (:ch,'C6_0',4,'settled','C11','v1','sigma',100,5,115,85,3,NULL,NULL,'initial',true)"),
                {"ch": ch})
            for i in range(40):
                conn.execute(_text(
                    "INSERT INTO wafer_predictions (wafer_id,chamber_id,recipe_id,predicted_c65) "
                    "VALUES (:w,:ch,'C6_0',:p)"), {"w": f"P{i}", "ch": ch, "p": 1100.0 + i})

        limits, _ = control_limits_loader.load(engine)
        c = sc.SpcConsumer(consumer=FakeConsumer([]), engine=engine, limits=limits,
                           whitelist=set(limits), tttm=TTTMEngine(limits, set(limits)),
                           establish_cfg=_ecfg(2, 6, 4, min_group_n=2, snapshot_min_n=3))
        prov = ProvisionalMode(_pcfg(2), limits=limits, writer=ProvisionalWriter(engine),
                               reload_fn=lambda: c.reload_limits(control_limits_loader.load(engine)[0]),
                               tttm=c.tttm, persist=None)
        c.provisional = prov

        def _pt(w, v=100.0):
            return [_Point(gk, w, "2026-07-20T01:00:00+00:00", float(v), 0, "v1")]

        # ① 요란 진입 → provisional 광폭
        c._drive_provisional(ch, "W0", 3, _pt("W0"))       # 베이스라인
        c._drive_provisional(ch, "W1", 4, _pt("W1"))       # PM↑ → PHASE_0
        prov.on_qual_verdict(ch, "loud")                    # → PHASE_1 (provisional write·commit)
        assert c.get_phase(ch) == Phase.PHASE_1
        with engine.connect() as conn:
            prov_active = conn.execute(_text(
                "SELECT trigger_type FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert prov_active == "provisional"

        # ② 정착 → PHASE2 신호 → 관리선 제안(PROPOSED)
        #    산포 있는 값(반복 distinct — 트림 견딤)이라야 establish σ>0 → apply 가드 통과.
        for i in range(2, 11):
            c._drive_provisional(ch, f"W{i}", 4, _pt(f"W{i}", 100.0 + 3.0 * (i % 5)))
        assert c.get_phase(ch) == Phase.AWAITING_PHASE2
        with engine.connect() as conn:
            cid = conn.execute(_text(
                "SELECT correction_id FROM limit_corrections WHERE chamber_id=:ch "
                "AND trigger_type='incident' AND status='PROPOSED' ORDER BY id DESC LIMIT 1"),
                {"ch": ch}).scalar()
        assert cid is not None                              # 재수립 제안 기록됨

        # ③ 승인 firm 유입 → apply + 후속(스냅샷·Y·복귀)
        assert c._handle_correction({"correction_type": "limit", "correction_id": cid}) is True
        assert c.get_phase(ch) == Phase.NORMAL              # on_firm_established 복귀
        assert ch in c._verify                              # verify forward tally 시작
        with engine.connect() as conn:
            firm = conn.execute(_text(
                "SELECT trigger_type FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
            snap = conn.execute(_text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
            yrow = conn.execute(_text(
                "SELECT count(*) FROM y_thresholds WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert firm == "incident"                           # firm 재수립 적재(provisional deactivate)
        assert snap == 1                                    # 스냅샷 리베이스
        assert yrow == 1                                    # Y-임계 write
    finally:
        _e2e_clean(engine)
        engine.dispose()


# ── c 검토 반영 (서브에이전트 🟠#3·#4·#6) ─────────────────────────────

def test_c_buffer_dedups_redelivered_wafer():
    """#3 — 재전달(같은 wafer_id) 시 _post_pm·버퍼 중복 카운트 안 함(a.on_wafer 정합)."""
    c, prov = _cons_prov(sl=1, rw=10)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))
    prov.on_qual_verdict("SIM_CH_1", "loud")
    c._drive_provisional("SIM_CH_1", "W2", 4, _pts(105, "W2"))   # 정착 1장
    n1 = len(c._backward["SIM_CH_1"])
    c._drive_provisional("SIM_CH_1", "W2", 4, _pts(105, "W2"))   # 같은 wafer 재전달
    assert len(c._backward["SIM_CH_1"]) == n1                    # 중복 미적재


def test_c_firm_followup_requires_incident_trigger():
    """#4 — AWAITING이라도 trigger_type≠incident correction엔 firm 후속 안 함(오트리거 방지)."""
    c, prov = _cons_prov()
    # 강제로 AWAITING 상태 구성
    prov.on_pm("SIM_CH_1", 5)
    prov.on_qual_verdict("SIM_CH_1", "loud")
    prov._state["SIM_CH_1"]["phase"] = Phase.AWAITING_PHASE2
    c._maybe_firm_followup("SIM_CH_1", "periodic")              # 정기 correction
    assert "SIM_CH_1" not in c._verify                          # 후속 미실행
    assert c.get_phase("SIM_CH_1") == Phase.AWAITING_PHASE2     # 무전이


def test_c_proposal_discards_pending_after_sufficient_sample():
    """#6 — 충분 표본으로 제안 시도하면(전 그룹 no_change라도) pending 종료(무한 재시도 방지)."""
    # min_group_n 크게 → establish_group 전부 no_change(소표본)
    prov = _prov2(2)
    ecfg = _dc.replace(EstablishConfig.load(), seasoning_loud=2, rolling_window_n=10,
                       min_establishment_wafers=2, min_group_n=9999)
    c, _ = _make([], provisional=prov, establish_cfg=ecfg, engine=FakeEngine())
    c._phase2_pending.add("SIM_CH_1")
    c._backward["SIM_CH_1"] = _collections_deque_of(3)          # 정착 3장(≥2)
    c._try_phase2_proposal("SIM_CH_1")
    assert "SIM_CH_1" not in c._phase2_pending                  # 재시도 종료


def _collections_deque_of(n):
    from collections import deque
    return deque(WaferSample(post_pm_count=1000 + i, values={_GKC: 100.0 + i})
                 for i in range(n))


# ── c mock 이벤트 관통 검증 (QualVerdictConfirmed·ChamberRequalified via Kafka) ──

from src.agent_b_spc import agent_mock


def test_c_requalified_live_disables_correction_fallback():
    """모드: requalified_live=True면 correction-apply fallback OFF(ChamberRequalified가 구동)."""
    prov = _prov3(2)
    prov.on_pm("SIM_CH_1", 5)
    prov.on_qual_verdict("SIM_CH_1", "loud")
    prov._state["SIM_CH_1"]["phase"] = Phase.AWAITING_PHASE2
    c, _ = _make([], provisional=prov, establish_cfg=_ecfg(2), requalified_live=True)
    c._maybe_firm_followup("SIM_CH_1", "incident")     # fallback 시도
    assert "SIM_CH_1" not in c._verify                 # 후속 미실행(이벤트 대기)
    assert c.get_phase("SIM_CH_1") == Phase.AWAITING_PHASE2


def _e2e_agent_consumer(engine, msgs, *, requalified_live=False, sl=2):
    """실 seam 컨슈머 + provisional, msgs를 poll 루프로 관통시키는 조립."""
    limits, _ = control_limits_loader.load(engine)
    holder = {}
    fake = FakeConsumer(msgs, on_exhausted=lambda: holder["c"].request_stop())
    c = sc.SpcConsumer(consumer=fake, engine=engine, limits=limits, whitelist=set(limits),
                       tttm=TTTMEngine(limits, set(limits)), requalified_live=requalified_live,
                       establish_cfg=_ecfg(sl, sl, 2, min_group_n=2, snapshot_min_n=2))
    prov = ProvisionalMode(_pcfg(sl), limits=limits, writer=ProvisionalWriter(engine),
                           reload_fn=lambda: c.reload_limits(control_limits_loader.load(engine)[0]),
                           tttm=c.tttm, persist=None)
    c.provisional = prov
    holder["c"] = c
    return c, prov


def _e2e_seed_firm(engine, ch, ver="v1", tt="initial"):
    with engine.begin() as conn:
        conn.execute(_text(
            "INSERT INTO control_limits (chamber_id,recipe_id,step,sensor_window,sensor_id,"
            "limit_version,method,center,sigma,ucl,lcl,k_sigma,q_low,q_high,trigger_type,is_active) "
            "VALUES (:ch,'C6_0',4,'settled','C11',:v,'sigma',100,5,115,85,3,NULL,NULL,:tt,true)"),
            {"ch": ch, "v": ver, "tt": tt})


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_c_e2e_qual_verdict_via_kafka_enters_provisional():
    """mock QualVerdictConfirmed을 fdc.agent로 흘려 → c가 가한계 진입 구동(poll 루프 관통)."""
    engine, ch = _make_engine(), _E2E_CH
    _e2e_clean(engine)
    try:
        _e2e_seed_firm(engine, ch)
        msg = FakeMsg(agent_mock.qual_verdict_confirmed(ch, "loud"), topic="fdc.agent", offset=1)
        c, prov = _e2e_agent_consumer(engine, [msg])
        prov.on_pm(ch, 4)                              # PHASE_0 (raw PM 상당 — raw 배선은 c-2)
        c.run()                                        # ← fdc.agent QVC 관통
        assert c.get_phase(ch) == Phase.PHASE_1        # 가한계 구동됨
        with engine.connect() as conn:
            tt = conn.execute(_text(
                "SELECT trigger_type FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert tt == "provisional"                     # 광폭 관리선 적재
    finally:
        _e2e_clean(engine)
        engine.dispose()


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_c_e2e_requalified_via_kafka_reestablishes():
    """mock ChamberRequalified을 fdc.agent로 흘려 → c가 재수립 후속 구동(스냅샷·Y·복귀)."""
    engine, ch, gk = _make_engine(), _E2E_CH, _E2E_GK
    _e2e_clean(engine)
    try:
        _e2e_seed_firm(engine, ch, ver="v2", tt="incident")   # firm 재수립 적용된 상태 가정
        with engine.begin() as conn:
            for i in range(40):
                conn.execute(_text(
                    "INSERT INTO wafer_predictions (wafer_id,chamber_id,recipe_id,predicted_c65) "
                    "VALUES (:w,:ch,'C6_0',:p)"), {"w": f"P{i}", "ch": ch, "p": 1100.0 + i})
        msg = FakeMsg(agent_mock.chamber_requalified(ch), topic="fdc.agent", offset=2)
        c, prov = _e2e_agent_consumer(engine, [msg], requalified_live=True)
        # 재수립 완료 직전 상태(AWAITING + 정착 버퍼) 구성
        prov._state[ch] = {"phase": Phase.AWAITING_PHASE2, "post_pm_count": 0, "verdict": "loud",
                           "boundary": 2, "last_pm_count": 4, "last_wafer_id": None}
        prov._provisional.add(ch)
        from collections import deque as _dq
        c._backward[ch] = _dq(WaferSample(post_pm_count=1000 + i, values={gk: 105.0})
                              for i in range(3))               # 상수(소표본 트림 엣지 회피)
        c.run()                                        # ← fdc.agent ChamberRequalified 관통
        assert c.get_phase(ch) == Phase.NORMAL         # 복귀 구동됨(on_firm_established)
        assert ch in c._verify                         # verify tally 시작
        with engine.connect() as conn:
            snap = conn.execute(_text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
            yrow = conn.execute(_text(
                "SELECT count(*) FROM y_thresholds WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert snap == 1 and yrow == 1                 # 스냅샷·Y 재계산 적재
    finally:
        _e2e_clean(engine)
        engine.dispose()


# ── Step7 라이브 관통 (실 DB): 조용 wafer → 트리거 발동 → recompute → control_limits/PROPOSED ──

from src.agent_b_spc.recalc_engine import RecalcConfig as _RecalcConfig


def _seed_recalc_initial(engine, ch, gk, center=100.0, sigma=10.0):
    """정기 리캘리 base — initial v1 활성 행(current + reset_ref 기저)."""
    with engine.begin() as conn:
        conn.execute(_text(
            "INSERT INTO control_limits (chamber_id,recipe_id,step,sensor_window,sensor_id,"
            "limit_version,method,center,sigma,ucl,lcl,k_sigma,q_low,q_high,trigger_type,is_active) "
            "VALUES (:ch,:rc,:st,:win,:sn,'v1','sigma',:c,:s,:ucl,:lcl,3,NULL,NULL,'initial',true)"),
            {"ch": ch, "rc": gk[1], "st": gk[2], "win": gk[3], "sn": gk[4],
             "c": center, "s": sigma, "ucl": center + 3 * sigma, "lcl": center - 3 * sigma})


def _wire_periodic(engine, ch):
    """Step7 정기 리캘리 배선한 consumer — 작은 interval·seasoning·min_group_n."""
    c, prov = _e2e_agent_consumer(engine, [])
    # 이 챔버로 격리 — loader가 DB 전체 active 행을 로드하므로, 다른 챔버 잔여(시딩·타 테스트)가
    # 있으면 chambers 수가 늘어 stagger_threshold 가 interval 을 넘겨(예: 5챔버·40 → 72) 40장에
    # 미발동한다. 단일 챔버 가정을 self.limits 로 보장(DB 잔여 무관 — 테스트 격리).
    c.limits = {gk: v for gk, v in c.limits.items() if gk[0] == ch}
    c.establish_cfg = _dc.replace(c.establish_cfg, seasoning_quiet=2, rolling_window_n=100)
    c._recalc_cfg = _dc.replace(_RecalcConfig.load(), min_group_n=5)
    c._resolutions = {}
    c._recalc_interval = 40                          # 챔버 1개 → 스태거 0 → 40장에 발동
    c._periodic_enabled = True
    return c


def _feed_quiet(c, ch, gk, val_fn, n=40):
    """조용(NORMAL) wafer n장 — PM 없음(pm_count 상수)·값은 val_fn(i)."""
    for i in range(n):
        c._drive_provisional(ch, f"W{i}", 3,
                             [_Point(gk, f"W{i}", "2026-07-20T01:00:00+00:00", val_fn(i), 0, "v1")])


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_c_e2e_periodic_recalc_auto_applies(monkeypatch):
    """라이브 관통 — 상한 내 shift(Δ0.4σ) → 트리거 → auto_applied → control_limits 새 버전·리로드."""
    monkeypatch.delenv("SPC_PERIODIC_RECALC_ENABLED", raising=False)
    engine, ch, gk = _make_engine(), _E2E_CH, _E2E_GK
    _clean_chamber(engine, ch)
    try:
        _seed_recalc_initial(engine, ch, gk, center=100.0, sigma=10.0)
        c = _wire_periodic(engine, ch)
        _feed_quiet(c, ch, gk, lambda i: 104.1 if i % 2 else 103.9)   # 평균 104 = 0.4σ 이동
        with engine.connect() as conn:
            row = conn.execute(_text(
                "SELECT limit_version, trigger_type, center FROM control_limits "
                "WHERE chamber_id=:ch AND is_active"), {"ch": ch}).mappings().first()
        assert row["trigger_type"] == "periodic"          # 정기 리캘리로 적용됨
        assert row["limit_version"] != "v1"               # 새 버전 INSERT(v1 deactivate)
        assert abs(row["center"] - 104.0) < 1.0           # 새 center ≈ 104
        assert abs(c.limits[gk]["center"] - 104.0) < 1.0  # 리로드로 메모리도 갱신(D5)
    finally:
        _clean_chamber(engine, ch)
        engine.dispose()


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_c_e2e_periodic_recalc_needs_approval_writes_proposed(monkeypatch):
    """라이브 관통 — 상한 초과 shift(Δ0.8σ) → needs_approval → limit_corrections PROPOSED(Step7b 지점)."""
    monkeypatch.delenv("SPC_PERIODIC_RECALC_ENABLED", raising=False)
    engine, ch, gk = _make_engine(), _E2E_CH, _E2E_GK
    _clean_chamber(engine, ch)
    try:
        _seed_recalc_initial(engine, ch, gk, center=100.0, sigma=10.0)
        c = _wire_periodic(engine, ch)
        _feed_quiet(c, ch, gk, lambda i: 108.1 if i % 2 else 107.9)   # 평균 108 = 0.8σ > cap 0.5
        with engine.connect() as conn:
            n_prop = conn.execute(_text(
                "SELECT count(*) FROM limit_corrections WHERE chamber_id=:ch "
                "AND status='PROPOSED' AND trigger_type='periodic'"), {"ch": ch}).scalar()
            active_ver = conn.execute(_text(
                "SELECT limit_version FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
            far = conn.execute(_text(
                "SELECT shadow_false_alarm_reduction_pct FROM limit_corrections "
                "WHERE chamber_id=:ch AND status='PROPOSED'"), {"ch": ch}).scalar()
        assert n_prop == 1                                # PROPOSED 기록(승인 하류)
        assert active_ver == "v1"                         # 승인 전 — 관리선 미변경(1-1)
        assert far is not None                            # Step7b — 소급 오탐 감소율 함께 기록됨
    finally:
        _clean_chamber(engine, ch)
        engine.dispose()


# ── c 실 Kafka 브로커 관통 (mock QualVerdictConfirmed 발행 → 실 Consumer → 가한계) ──

def _clean_chamber(engine, ch):
    with engine.begin() as conn:
        for tbl in ("control_limits", "limit_corrections", "qual_snapshots",
                    "y_thresholds", "wafer_predictions"):
            conn.execute(_text(f"DELETE FROM {tbl} WHERE chamber_id=:ch"), {"ch": ch})


@pytest.mark.skipif(not (_kafka_available() and _e2e_db_available()),
                    reason="Kafka 브로커/DB 미접속 — 통합테스트 skip")
def test_c_live_kafka_qual_verdict_drives_provisional():
    """실 브로커: mock QualVerdictConfirmed 발행 → 실 Consumer 구독 → c 가한계 진입(관통).

    FakeConsumer가 못 잡는 것: 실 Consumer/Message API·JSON 왕복·3토픽 구독에서 fdc.agent
    라우팅·수동 커밋이 실제로 동작하는지.
    """
    import threading
    import time as _time

    from confluent_kafka import Producer

    bootstrap = sc._bootstrap()
    agent_topic = f"test.fdc.agent.c.{_time.time_ns()}"
    group = f"test-spc-agent-{_time.time_ns()}"
    ch = "LIVE_CH_C"
    engine = _make_engine()
    _clean_chamber(engine, ch)
    try:
        _e2e_seed_firm(engine, ch)                     # firm 관리선(광폭 write가 deactivate할 대상)
        limits, _ = control_limits_loader.load(engine)
        consumer = sc.make_consumer(bootstrap, group=group, auto_offset_reset="earliest")
        c = sc.SpcConsumer(consumer=consumer, engine=engine, limits=limits, whitelist=set(limits),
                           tttm=TTTMEngine(limits, set(limits)), establish_cfg=_ecfg(2),
                           idle_flush_sec=3.0, topic_raw=f"test.raw.{_time.time_ns()}",
                           topic_correction=f"test.corr.{_time.time_ns()}", topic_agent=agent_topic,
                           topic_prediction=f"test.pred.{_time.time_ns()}")
        prov = ProvisionalMode(_pcfg(2), limits=limits, writer=ProvisionalWriter(engine),
                               reload_fn=lambda: c.reload_limits(control_limits_loader.load(engine)[0]),
                               tttm=c.tttm, persist=None)
        c.provisional = prov
        prov.on_pm(ch, 4)                              # PHASE_0 (raw PM 상당)

        producer = Producer({"bootstrap.servers": bootstrap})
        producer.produce(agent_topic, key=ch.encode(),
                         value=json.dumps(agent_mock.qual_verdict_confirmed(ch, "loud")).encode())
        producer.flush(10)

        t = threading.Thread(target=c.run, daemon=True)
        t.start()
        deadline = _time.time() + 45
        while c.get_phase(ch) != Phase.PHASE_1 and _time.time() < deadline:
            _time.sleep(0.3)
        c.request_stop()
        t.join(timeout=15)

        assert c.get_phase(ch) == Phase.PHASE_1        # 실 이벤트로 가한계 구동
        with engine.connect() as conn:
            tt = conn.execute(_text(
                "SELECT trigger_type FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert tt == "provisional"
    finally:
        _clean_chamber(engine, ch)
        engine.dispose()


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_c_e2e_requalified_live_full_flow_fallback_silent():
    """결합: requalified_live=True 전 흐름 — correction 적용 시 fallback 침묵, ChamberRequalified만 후속 구동.

    요란→가한계→정착→제안→firm correction 적용(후속 안 돎, AWAITING 유지)→ChamberRequalified→후속(복귀).
    fallback을 타는지 이벤트를 타는지 한 테스트로 판별한다.
    """
    engine, ch, gk = _make_engine(), _E2E_CH, _E2E_GK
    _e2e_clean(engine)
    try:
        _e2e_seed_firm(engine, ch)                     # firm v1
        with engine.begin() as conn:
            for i in range(40):
                conn.execute(_text(
                    "INSERT INTO wafer_predictions (wafer_id,chamber_id,recipe_id,predicted_c65) "
                    "VALUES (:w,:ch,'C6_0',:p)"), {"w": f"P{i}", "ch": ch, "p": 1100.0 + i})
        c, prov = _e2e_agent_consumer(engine, [], requalified_live=True)   # ★ fallback OFF 모드

        def _pt(w, v=100.0):
            return [_Point(gk, w, "2026-07-20T01:00:00+00:00", float(v), 0, "v1")]

        # ① 요란 진입
        c._drive_provisional(ch, "W0", 3, _pt("W0"))
        c._drive_provisional(ch, "W1", 4, _pt("W1"))
        prov.on_qual_verdict(ch, "loud")
        # ② 정착 → 제안(PROPOSED)
        for i in range(2, 6):
            c._drive_provisional(ch, f"W{i}", 4, _pt(f"W{i}", 105.0))
        assert c.get_phase(ch) == Phase.AWAITING_PHASE2
        with engine.connect() as conn:
            cid = conn.execute(_text(
                "SELECT correction_id FROM limit_corrections WHERE chamber_id=:ch "
                "AND trigger_type='incident' AND status='PROPOSED' ORDER BY id DESC LIMIT 1"),
                {"ch": ch}).scalar()
        assert cid is not None

        # ③ firm correction 적용 — requalified_live=True라 fallback 침묵(후속 X)
        assert c._handle_correction({"correction_type": "limit", "correction_id": cid}) is True
        assert c.get_phase(ch) == Phase.AWAITING_PHASE2   # ★ 아직 복귀 안 함(fallback 안 탐)
        assert ch not in c._verify                        # ★ 후속 미실행(verify 미시작)
        with engine.connect() as conn:
            snap0 = conn.execute(_text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert snap0 == 0                                 # ★ 스냅샷도 아직 없음

        # ④ ChamberRequalified → 이제서야 후속 구동(복귀)
        c._handle_agent(agent_mock.chamber_requalified(ch))
        assert c.get_phase(ch) == Phase.NORMAL            # ★ 이벤트가 복귀 구동
        assert ch in c._verify                            # verify tally 시작
        with engine.connect() as conn:
            snap = conn.execute(_text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
            yrow = conn.execute(_text(
                "SELECT count(*) FROM y_thresholds WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert snap == 1 and yrow == 1                    # 이벤트 후속에서 재계산 적재
    finally:
        _e2e_clean(engine)
        engine.dispose()


# ═══════════════ B6-3-d: M3 통합 검증 (B 슬라이스, 요란 재인증) ═══════════════
#
# 검증 플랜(B6-3-d) 7단계 체크포인트 관통 + 네거티브/엣지. 이벤트=mock(agent_mock),
# raw/correction=real 경로. Kafka 라우팅은 c 관통 테스트가 별도 증명, 여기선 단계별 동작·
# 크로스파트 핸드오프(받을 것·줄 것) 검증에 집중.

@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_b63d_m3_full_scenario_b_slice():
    """B6-3-d — 요란 재인증 7단계 관통(요란→가한계→정착→제안→firm→복귀→verify)."""
    engine, ch, gk = _make_engine(), _E2E_CH, _E2E_GK
    _e2e_clean(engine)
    try:
        _e2e_seed_firm(engine, ch)
        with engine.begin() as conn:
            for i in range(40):
                conn.execute(_text(
                    "INSERT INTO wafer_predictions (wafer_id,chamber_id,recipe_id,predicted_c65) "
                    "VALUES (:w,:ch,'C6_0',:p)"), {"w": f"P{i}", "ch": ch, "p": 1100.0 + i})
        c, prov = _e2e_agent_consumer(engine, [], requalified_live=True, sl=3)  # seasoning_loud=3
        # 정착 표본 창을 넓혀(rolling 6·min_est 4) 신호 시점에 산포 있는 ≥4장 확보 →
        # establish σ>0(피드 前 오버라이드라 버퍼 deque가 새 maxlen으로 생성).
        c.establish_cfg = _ecfg(3, 6, 4, min_group_n=2, snapshot_min_n=3)

        def _pt(w, v=100.0):
            return [_Point(gk, w, "2026-07-20T01:00:00+00:00", float(v), 0, "v1")]

        def _active_tt():
            with engine.connect() as conn:
                return conn.execute(_text(
                    "SELECT trigger_type FROM control_limits WHERE chamber_id=:ch AND is_active"),
                    {"ch": ch}).scalar()

        # ── 1단계: 요란 PM → PHASE_0 억제 ──
        c._drive_provisional(ch, "W0", 3, _pt("W0"))            # 베이스라인
        c._drive_provisional(ch, "W1", 4, _pt("W1"))           # PM↑
        assert c.get_phase(ch) == Phase.PHASE_0                # ✔ CP1: 억제 진입

        # ── 2단계: Qual 요란 판정 → PHASE_1 광폭 + TTTM 제외 ──
        c._handle_agent(agent_mock.qual_verdict_confirmed(ch, "loud"))
        assert c.get_phase(ch) == Phase.PHASE_1                # ✔ CP2: 광폭 전환
        assert _active_tt() == "provisional"                   # ✔ provisional 관리선 적재
        assert ch in c.tttm.excluded                           # ✔ TTTM fleet 제외

        # ── 3단계: 정착 진행 → post_pm↑, 과도라 버퍼 아직 비어 ──
        c._drive_provisional(ch, "W2", 4, _pt("W2", 105.0))    # post_pm=2 (≤seasoning_loud 3)
        assert c.get_phase(ch) == Phase.PHASE_1                # 아직 정착 전
        assert len(c._backward.get(ch, [])) == 0               # ✔ CP3: 과도 미축적

        # ── 4단계: 정착 완료 → PHASE2_SIGNAL + 버퍼 축적 + 관리선 제안(PROPOSED) ──
        #    산포 있는 값(반복 distinct — 트림 견딤)이라야 establish σ>0 → apply 가드 통과.
        for i in range(3, 10):                                 # 신호는 min_est=4번째 정착 wafer
            c._drive_provisional(ch, f"W{i}", 4, _pt(f"W{i}", 100.0 + 3.0 * (i % 5)))
        assert c.get_phase(ch) == Phase.AWAITING_PHASE2        # ✔ CP4a: 신호 1회
        assert len(c._backward[ch]) > 0                        # ✔ CP4b: 정착 버퍼 축적
        with engine.connect() as conn:
            cid = conn.execute(_text(
                "SELECT correction_id FROM limit_corrections WHERE chamber_id=:ch "
                "AND trigger_type='incident' AND status='PROPOSED' ORDER BY id DESC LIMIT 1"),
                {"ch": ch}).scalar()
        assert cid is not None                                 # ✔ CP4c: 관리선만 PROPOSED

        # ── 5단계: 승인 firm 적용 → 관리선만(스냅샷·Y는 6단계 후속) ──
        assert c._handle_correction({"correction_type": "limit", "correction_id": cid}) is True
        assert _active_tt() == "incident"                      # ✔ CP5: firm 신 버전 적용
        assert c.get_phase(ch) == Phase.AWAITING_PHASE2        #   후속은 아직(requalified_live 침묵)
        assert ch not in c._verify

        # ── 6단계: 재수립 완료(ChamberRequalified) → 후속·복귀 ──
        c._handle_agent(agent_mock.chamber_requalified(ch))
        assert c.get_phase(ch) == Phase.NORMAL                 # ✔ CP6a: 정상 복귀
        assert ch not in c.tttm.excluded                       # ✔ CP6b: TTTM fleet 재참여
        assert ch in c._verify                                 # ✔ CP6c: verify 시작
        with engine.connect() as conn:
            snap = conn.execute(_text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
            yrow = conn.execute(_text(
                "SELECT count(*) FROM y_thresholds WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert snap == 1 and yrow == 1                         # ✔ CP6d: 스냅샷·Y 재계산 write

        # ── 7단계: verify forward N장 오탐율 채점 ──
        c._verify_n, c._verify_max_pct = 3, 50.0
        for f in (False, False, False):                        # 0% ≤ 50 → PASS
            c._tally_verify(ch, f)
        assert ch not in c._verify                             # ✔ CP7: 판정 완료(정리)
    finally:
        _e2e_clean(engine)
        engine.dispose()


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_b63d_negative_r9_rejection_holds_provisional():
    """네거티브 — R9 반려(firm correction 미유입) → AWAITING holding·광폭 유지(졸업 안 함)."""
    engine, ch, gk = _make_engine(), _E2E_CH, _E2E_GK
    _e2e_clean(engine)
    try:
        _e2e_seed_firm(engine, ch)
        c, prov = _e2e_agent_consumer(engine, [], requalified_live=True, sl=3)

        def _pt(w, v=100.0):
            return [_Point(gk, w, "2026-07-20T01:00:00+00:00", float(v), 0, "v1")]

        c._drive_provisional(ch, "W0", 3, _pt("W0"))
        c._drive_provisional(ch, "W1", 4, _pt("W1"))
        c._handle_agent(agent_mock.qual_verdict_confirmed(ch, "loud"))
        for i in range(2, 8):
            c._drive_provisional(ch, f"W{i}", 4, _pt(f"W{i}", 105.0))
        assert c.get_phase(ch) == Phase.AWAITING_PHASE2        # 제안됨
        # R9 반려 = firm correction 안 옴 · ChamberRequalified 안 옴 → 추가 wafer 유입해도
        for i in range(8, 12):
            c._drive_provisional(ch, f"W{i}", 4, _pt(f"W{i}", 105.0))
        assert c.get_phase(ch) == Phase.AWAITING_PHASE2        # ✔ holding(재신호·졸업 없음)
        assert ch in c.tttm.excluded                           # ✔ 광폭·제외 유지(보호 지속)
        with engine.connect() as conn:
            tt = conn.execute(_text(
                "SELECT trigger_type FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert tt == "provisional"                             # ✔ firm 미졸업
    finally:
        _e2e_clean(engine)
        engine.dispose()


@pytest.mark.skipif(not _e2e_db_available(), reason="DB/스키마 미반영 — skip")
def test_b63d_edge_restart_empty_buffer_skips_snapshot_y():
    """엣지 — AWAITING 중 재시작(버퍼 유실) → 후속서 스냅샷·Y skip, firm 관리선은 유지·복귀(알려진 한계)."""
    engine, ch, gk = _make_engine(), _E2E_CH, _E2E_GK
    _e2e_clean(engine)
    try:
        _e2e_seed_firm(engine, ch, ver="v2", tt="incident")   # firm 적용된 상태(재시작 후)
        c, prov = _e2e_agent_consumer(engine, [], requalified_live=True, sl=3)
        # 재시작 복구: AWAITING인데 backward 버퍼는 비어 있음(인메모리 유실 — 결정2 ⓓ)
        prov._state[ch] = {"phase": Phase.AWAITING_PHASE2, "post_pm_count": 0, "verdict": "loud",
                           "boundary": 3, "last_pm_count": 4, "last_wafer_id": None}
        prov._provisional.add(ch)
        # 버퍼 없음(c._backward 비어 있음)
        c._handle_agent(agent_mock.chamber_requalified(ch))
        assert c.get_phase(ch) == Phase.NORMAL                 # ✔ 복귀는 함(on_firm_established)
        with engine.connect() as conn:
            snap = conn.execute(_text(
                "SELECT count(*) FROM qual_snapshots WHERE chamber_id=:ch"), {"ch": ch}).scalar()
            firm = conn.execute(_text(
                "SELECT trigger_type FROM control_limits WHERE chamber_id=:ch AND is_active"),
                {"ch": ch}).scalar()
        assert snap == 0                                       # ✔ 스냅샷 skip(버퍼 없음)
        assert firm == "incident"                              # ✔ firm 관리선은 유지
    finally:
        _e2e_clean(engine)
        engine.dispose()


# ── B6-3-e: c settled_at 배선 (조기 정착 컷오프) ─────────────────────────────
def test_c_settled_cutoff_helper():
    """_settled_cutoff — 신호 전=seasoning_loud 폴백 / 신호 후=settled_at."""
    c, _ = _cons_prov(sl=1000, rw=10)
    assert c._settled_cutoff("SIM_CH_1") == 1000            # 폴백(현행)
    c._settled_at["SIM_CH_1"] = 600
    assert c._settled_cutoff("SIM_CH_1") == 600             # 조기 정착 컷오프


def test_c_fallback_stores_settled_at_converged_false():
    """상한 폴백 정착 → settled_at=boundary 저장·converged=False(미수렴 경고 기록)."""
    c, prov = _cons_prov(sl=2, rw=10)
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))   # 베이스라인
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))   # PM↑ → PHASE_0
    prov.on_qual_verdict("SIM_CH_1", "loud")                     # → PHASE_1
    for i in range(2, 6):
        c._drive_provisional("SIM_CH_1", f"W{i}", 4, _pts(100, f"W{i}"))
    assert c._settled_at["SIM_CH_1"] == 2 and c._converged["SIM_CH_1"] is False
    assert c._settled_cutoff("SIM_CH_1") == 2


def test_c_early_settle_converged_integration():
    """③ 조기 σ-수렴 정착(c 통합) — 상수 수렴 → converged=True·settled_at<boundary·조기 컷오프.

    _prov2와 달리 limits+whitelist 주입(σ_ref 스냅샷 성립)·작은 W로 조기 정착을 라이브 구동으로 검증.
    """
    firm = {_GKC: {"center": 100.0, "sigma": 10.0, "ucl": 130.0, "lcl": 70.0,
                   "method": "sigma", "limit_version": "v1"}}
    prov = ProvisionalMode(_pcfg(seasoning_loud=1000, W=4, M=2, settle_min=0),
                           limits=firm, writer=_FakeProvWriter(), reload_fn=lambda: None,
                           tttm=_FakeTTTM(), persist=None, whitelist={_GKC})
    c, _ = _make([], provisional=prov, establish_cfg=_ecfg(1000, 10, 2))
    c._drive_provisional("SIM_CH_1", "W0", 3, _pts(100, "W0"))       # 베이스라인
    c._drive_provisional("SIM_CH_1", "W1", 4, _pts(100, "W1"))       # PM↑ → PHASE_0
    prov.on_qual_verdict("SIM_CH_1", "loud")                         # → PHASE_1 + firm σ_ref 스냅샷
    for i in range(2, 20):
        c._drive_provisional("SIM_CH_1", f"W{i}", 4, _pts(100, f"W{i}"))  # 상수 수렴
    assert c._converged.get("SIM_CH_1") is True                     # σ 수렴 조기 정착
    assert 0 < c._settled_at["SIM_CH_1"] < 1000                     # boundary(1000) 전 조기
    assert c._settled_cutoff("SIM_CH_1") == c._settled_at["SIM_CH_1"]  # 버퍼 컷오프=조기 시점


# ── B6-3-f: pm_log 배선 (ts 전달 · poll 루프 flush) ──────────────────

class _FakePmWriter:
    """PmLogWriter 미러 — flush_pending 호출 횟수·예외 주입."""

    def __init__(self, boom=False):
        self.flushes = 0
        self.boom = boom
        self.pending: list = []

    def flush_pending(self):
        self.flushes += 1
        if self.boom:
            raise OSError("디스크 실패 시뮬")
        return 0


def test_poll_loop_flushes_pm_log_pending():
    """★리뷰 M1 — poll마다 pm_log 보류분 회수. append(=다음 요란 PM, 3~4개월)만으론 재시도 없음."""
    w = _FakePmWriter()
    c, _ = _make([], pm_log_writer=w)
    c.run()
    assert w.flushes >= 1                        # 루프가 상시 호출


def test_pm_log_flush_failure_does_not_kill_loop():
    """flush 예외가 컨슈머 루프를 죽이지 않는다 (6-2 — pm_log는 안전 경로 아님)."""
    w = _FakePmWriter(boom=True)
    c, fake = _make([], pm_log_writer=w)
    c.run()                                      # 예외 전파 없이 정상 종료
    assert w.flushes >= 1 and fake.closed


def test_pm_log_writer_absent_is_noop():
    """미주입(기본 None) → _flush_pm_log no-op (기존 조립 경로 무회귀)."""
    c, _ = _make([])
    assert c.pm_log_writer is None
    c._flush_pm_log()                            # 예외 없음


def test_drive_provisional_passes_wafer_timestamp():
    """★PM 개방 시각(wafer timestamp)이 on_pm으로 전달된다 (B6-3-f D2)."""
    seen = []

    class _Prov:
        def on_pm(self, chamber, pm_count, ts=None):
            seen.append((chamber, pm_count, ts))

        def on_wafer(self, *a, **kw):
            return None

        def get_phase(self, chamber):
            from src.agent_b_spc.provisional_mode import Phase
            return Phase.PHASE_0

    c, _ = _make([], provisional=_Prov(), establish_cfg=_ecfg(1000, 10, 2))
    c._drive_provisional("SIM_CH_4", "W0", 1, [], ts="2026-07-28T05:12:33+00:00")  # 베이스라인
    c._drive_provisional("SIM_CH_4", "W1", 2, [], ts="2026-07-28T06:00:00+00:00")  # PM↑
    assert seen == [("SIM_CH_4", 2, "2026-07-28T06:00:00+00:00")]


def test_process_wafer_forwards_timestamp_to_drive():
    """_process_wafer가 rows[-1]['timestamp']를 넘긴다 (배선 회귀 방지)."""
    got = {}
    c, _ = _make([])
    c._drive_provisional = lambda ch, wid, pmc, pts, ts=None: got.update(ts=ts)
    c._process_wafer([_row(timestamp="2026-07-28T05:12:33.000Z")])
    assert got["ts"] == "2026-07-28T05:12:33.000Z"


# ── B6-2 G5: 진입 가드 — 감시 화이트리스트 공집합 → 무음 정지 차단 ──────────
def test_require_whitelist_raises_on_empty():
    """시딩 부재/실패로 whitelist 공집합이면 루프 진입 전 명시적 RuntimeError(무음 정지 방지)."""
    with pytest.raises(RuntimeError):
        sc._require_whitelist(set())


def test_require_whitelist_passes_nonempty():
    sc._require_whitelist({("SIM_CH_1", "C6_0", 4, "settled", "C11")})   # no raise


# ── TSR-0005 이관 ②: 미확정 Qual 방치 경고 (_note_qual_pending·관측 전용) ──
# 사고 요지: PM → PHASE_0(억제) → Qual PENDING 방치 → **탈출 경로 없음**.
#   전 챔버가 28시간 무음이었고 `quals` 208행 중 confirmed 0, 예외·ERROR 0건이었다.

from src.agent_b_spc.spc_consumer import _QUAL_PENDING_REWARN_SEC as _QREWARN
from src.agent_b_spc.spc_consumer import _QUAL_PENDING_WARN_SEC as _QWARN


class _QualEngine:
    """`_QUAL_OLDEST_PENDING_SEC` 만 답하는 최소 엔진 — (경과초, 건수) 또는 (None, 0)."""

    def __init__(self, elapsed, pending=1):
        self.row = (elapsed, pending)
        self.queries = 0

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        self.queries += 1
        return self

    def first(self):
        return self.row


def _qual_consumer(elapsed, pending=1, t=0.0):
    clk = _Clk(t)
    c, _ = _make([], clock=clk)
    eng = _QualEngine(elapsed, pending)
    c.engine = eng
    return c, eng, clk


def test_c_qual_pending_no_warn_before_threshold(caplog):
    """임계 미달 → 경고 없음(정상 대기 중)."""
    c, _, _ = _qual_consumer(_QWARN - 1)
    with caplog.at_level("WARNING"):
        c._note_qual_pending("SIM_CH_1")
    assert not any("qual_pending" in r.getMessage() for r in caplog.records)


def test_c_qual_pending_warns_after_threshold(caplog):
    """임계 초과 → WARNING. 자동 해제는 하지 않는다(헌법 1-1)."""
    c, _, _ = _qual_consumer(_QWARN + 60, pending=3)
    with caplog.at_level("WARNING"):
        c._note_qual_pending("SIM_CH_1")
    msg = [r.getMessage() for r in caplog.records if "qual_pending" in r.getMessage()]
    assert len(msg) == 1
    assert "3건" in msg[0] and "SIM_CH_1" in msg[0]
    assert "자동 해제 안 함" in msg[0]


def test_c_qual_pending_throttled_and_skips_requery(caplog):
    """재알림 간격 안에서는 경고도 **DB 재조회도** 생략 — throttle 순서(①→②) 확인."""
    c, eng, _ = _qual_consumer(_QWARN + 60)
    with caplog.at_level("WARNING"):
        c._note_qual_pending("SIM_CH_1")
        c._note_qual_pending("SIM_CH_1")
    assert sum("qual_pending" in r.getMessage() for r in caplog.records) == 1
    assert eng.queries == 1                      # 2회차는 조회조차 안 했다


def test_c_qual_pending_rewarns_after_interval(caplog):
    """재알림 간격이 지나면 다시 경고 — 영구 방치가 조용해지는 것 방지(#128 지적 4)."""
    c, _, clk = _qual_consumer(_QWARN + 60)
    with caplog.at_level("WARNING"):
        c._note_qual_pending("SIM_CH_1")
        clk.t += _QREWARN + 1
        c._note_qual_pending("SIM_CH_1")
    assert sum("qual_pending" in r.getMessage() for r in caplog.records) == 2


def test_c_qual_pending_clears_when_no_pending(caplog):
    """PENDING 이 없어지면(확정됨) 추적을 지운다 — 다음 방치를 처음부터 다시 센다."""
    c, eng, clk = _qual_consumer(_QWARN + 60)
    c._note_qual_pending("SIM_CH_1")
    assert "SIM_CH_1" in c._qual_pending_warned
    eng.row = (None, 0)                          # 확정 완료
    clk.t += _QREWARN + 1
    c._note_qual_pending("SIM_CH_1")
    assert "SIM_CH_1" not in c._qual_pending_warned


def test_c_qual_pending_db_failure_is_silent(caplog):
    """조회 실패는 skip — 관측 실패가 판정을 막지 않는다(메모리 폴백도 두지 않는다)."""
    c, eng, _ = _qual_consumer(_QWARN + 60)

    def boom(*a, **k):
        raise RuntimeError("db down")

    eng.execute = boom
    with caplog.at_level("WARNING"):
        c._note_qual_pending("SIM_CH_1")         # 예외가 새면 안 된다
    assert not any("qual_pending" in r.getMessage() for r in caplog.records)
