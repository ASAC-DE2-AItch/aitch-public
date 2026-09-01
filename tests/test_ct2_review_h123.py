# -*- coding: utf-8 -*-
"""리뷰 H1·H2·H3 수정 검증 (2026-07-30 코드리뷰 반영).

H1 실패 이벤트 유실 — handle_event 예외 시 **seek 되감기**. 커밋 보류만으로는 부족하다:
     poll 후 인메모리 position이 전진하고, 타 event_type이 뒤에서 정상 커밋되며 실패 offset을
     지나쳐 영구 유실된다 (헌법 7장 "실패 경로는 seek 되감기").
H2 게이트 fail-open — VAL 0장이면 A1·A2만 통과한 채 `gate_pass=True` 가 됐다. 채점 불가는
     **명시적 배포 불가**로 환원 (3-3 ③ 배포 결정 단일 소스 보호).
H3 감시 스키마 — `ae_score`(없음) → `anomaly_score`, `spc_violations` 조인(wafer_id 없음) →
     같은 행 `spc_flags`(JSONB). + 더미 폴백 배제·의심 지표.

실행: python -m pytest tests/test_ct2_review_h123.py -q
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops"))
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops" / "Autoencoder"))

import ct2_orchestrator as O          # noqa: E402
import ct2_deploy_monitor as M        # noqa: E402
from ae_pipeline import validate_bundle as V   # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# H1 — 실패 이벤트 seek 되감기
# ══════════════════════════════════════════════════════════════════════════

class FakeMsg:
    def __init__(self, payload, offset=5, topic="fdc.agent", partition=0):
        self._p, self._o, self._t, self._part = payload, offset, topic, partition

    def value(self):
        return json.dumps(self._p).encode()

    def error(self):
        return None

    def offset(self):
        return self._o

    def topic(self):
        return self._t

    def partition(self):
        return self._part


class FakeConsumer:
    """poll 시퀀스를 재생하고 commit/seek 호출을 기록하는 스텁."""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.commits, self.seeks, self.closed = [], [], False

    def subscribe(self, topics):
        self.topics = topics

    def poll(self, _t):
        if self._msgs:
            return self._msgs.pop(0)
        O.running = False               # 시퀀스 소진 → 루프 종료
        return None

    def commit(self, msg, asynchronous=False):
        self.commits.append(msg.offset())

    def seek(self, tp):
        self.seeks.append(tp.offset)

    def close(self):
        self.closed = True


class FakeTP:
    def __init__(self, topic, partition, offset):
        self.topic, self.partition, self.offset = topic, partition, offset


@pytest.fixture()
def kafka_stub(monkeypatch):
    """confluent_kafka 모듈 스텁 주입 (Consumer·TopicPartition)."""
    import types
    mod = types.ModuleType("confluent_kafka")
    holder = {}

    def _consumer(_conf):
        return holder["consumer"]

    mod.Consumer = _consumer
    mod.TopicPartition = FakeTP
    monkeypatch.setitem(sys.modules, "confluent_kafka", mod)
    monkeypatch.setattr(O, "_params", lambda: {"ct2_auto_generate": False,
                                               "ct2_retry_backoff_sec": 0})
    monkeypatch.setattr(O.time, "sleep", lambda _s: None)
    monkeypatch.setattr(O.signal, "signal", lambda *a: None)
    O.running = True
    return holder


EVT = {"event_type": "ChamberRequalified", "incident_id": "INC-1", "chamber_id": "SIM_CH_1"}


def test_h1_failure_seeks_back_and_does_not_commit(kafka_stub, monkeypatch):
    """★실패 시 실패 offset으로 seek + 커밋 안 함 (유실 차단)."""
    msg = FakeMsg(EVT, offset=5)
    kafka_stub["consumer"] = FakeConsumer([msg])
    monkeypatch.setattr(O, "handle_event",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("DB 다운")))
    O.main([])
    c = kafka_stub["consumer"]
    assert c.seeks == [5], f"실패 offset으로 되감지 않았다: seeks={c.seeks}"
    assert c.commits == [], f"실패했는데 커밋했다: {c.commits}"


def test_h1_other_event_type_cannot_skip_failed_offset(kafka_stub, monkeypatch):
    """★핵심: 실패(offset 5) 직후 타 event_type(offset 6)이 와도 5를 지나치지 않는다.

    수정 전에는 6이 정상 커밋되며 committed offset이 7이 되어 5가 영구 유실됐다.
    수정 후에는 5에서 seek 되감기가 일어나므로, 6의 커밋보다 되감기가 선행한다.
    """
    fail_msg = FakeMsg(EVT, offset=5)
    other = FakeMsg({"event_type": "QualVerdictConfirmed"}, offset=6)
    kafka_stub["consumer"] = FakeConsumer([fail_msg, other])
    monkeypatch.setattr(O, "handle_event",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("DB 다운")))
    O.main([])
    c = kafka_stub["consumer"]
    assert 5 in c.seeks, "실패 offset 되감기 없음"
    assert 5 not in c.commits, "실패 offset을 커밋했다"


def test_h1_success_commits(kafka_stub, monkeypatch):
    """정상 처리는 커밋하고 되감지 않는다 (진행 보장)."""
    kafka_stub["consumer"] = FakeConsumer([FakeMsg(EVT, offset=9)])
    monkeypatch.setattr(O, "handle_event", lambda *a: None)
    O.main([])
    c = kafka_stub["consumer"]
    assert c.commits == [9] and c.seeks == []


def test_h1_poison_and_other_type_commit(kafka_stub, monkeypatch):
    """poison(역직렬화 불능)·타 이벤트는 커밋해 offset을 전진 (파티션 정지 방지)."""
    class Bad(FakeMsg):
        def value(self):
            return b"{not json"
    kafka_stub["consumer"] = FakeConsumer([Bad({}, offset=1),
                                           FakeMsg({"event_type": "Other"}, offset=2)])
    monkeypatch.setattr(O, "handle_event", lambda *a: None)
    O.main([])
    assert kafka_stub["consumer"].commits == [1, 2]


def test_h1_backoff_param_loaded(kafka_stub, monkeypatch):
    """`ct2_retry_backoff_sec` 가 params에서 로드된다 (6-1 — 매직넘버 금지)."""
    monkeypatch.setattr(O, "_params", lambda: {"ct2_auto_generate": False,
                                               "ct2_retry_backoff_sec": 7})
    kafka_stub["consumer"] = FakeConsumer([])
    O.main([])
    assert O.RETRY_BACKOFF_SEC == 7.0


def test_h1_backoff_key_in_params_yaml():
    import yaml
    ct = yaml.safe_load((REPO_ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))["ct"]
    assert "ct2_retry_backoff_sec" in ct


# ══════════════════════════════════════════════════════════════════════════
# H2 — 게이트 fail-open 차단
# ══════════════════════════════════════════════════════════════════════════

def _stub_bundle(tmp_path, val_list, gate_list=None):
    """7파일 + manifest(sha256) + 사이드카를 갖춘 최소 번들 (해시 정합)."""
    from ae_pipeline import io_bundle
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in ("model_seed42.pt", "scaler.pkl", "scorer.npz", "calib.json",
                 "drift.json", "feature_spec.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    io_bundle.write_manifest(tmp_path, {"ae_model_version": "v"})
    io_bundle.save_json({"val": val_list, "gate": gate_list or []},
                        tmp_path / "val_wafers.json")
    return tmp_path


def test_h2_empty_val_is_not_pass(tmp_path, monkeypatch):
    """★VAL 0장이면 gate_pass=False (fail-open 봉인)."""
    bundle = _stub_bundle(tmp_path / "b", [])
    (tmp_path / "d.csv").write_text("C64,C10,C46\nW0,1,1\n", encoding="utf-8")
    # validate()가 `from .retrain import load_data` 로 지연 import 하므로 원본에 패치한다
    from ae_pipeline import retrain as R
    monkeypatch.setattr(R, "load_data", lambda p: __import__("pandas").DataFrame(
        {"C64": ["W0"], "C10": [1], "C46": [1], "C7": [4], "C42": [0]}))
    report = V.validate(bundle, tmp_path / "d.csv")
    assert report["gate_pass"] is False, "VAL 0장인데 게이트가 통과했다 (fail-open)"
    a3 = next(c for c in report["checks"] if c["check"] == "A3_VAL_세트_해시")
    assert not a3["pass"] and a3["critical"] and "0장" in a3["detail"]


def test_h2_val_restore_failure_still_fails(tmp_path, monkeypatch):
    """VAL 복원 예외 경로도 FAIL (중복 A3 추가 없이 1건)."""
    bundle = _stub_bundle(tmp_path / "b2", ["W0"])
    # validate()가 `from .retrain import load_data` 로 지연 import 하므로 원본에 패치한다
    from ae_pipeline import retrain as R
    monkeypatch.setattr(R, "load_data", lambda p: __import__("pandas").DataFrame(
        {"C64": ["W0"], "C10": [1], "C46": [1], "C7": [4], "C42": [0]}))
    monkeypatch.setattr(V, "resolve_val_wafers",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("사이드카 파손")))
    report = V.validate(bundle, tmp_path / "d.csv")
    assert report["gate_pass"] is False
    a3s = [c for c in report["checks"] if c["check"] == "A3_VAL_세트_해시"]
    assert len(a3s) == 1, f"A3 중복 추가: {len(a3s)}건"
    assert "복원 실패" in a3s[0]["detail"]


# ══════════════════════════════════════════════════════════════════════════
# H3 — 감시 스키마 정합
# ══════════════════════════════════════════════════════════════════════════

class SchemaCursor:
    """실 스키마를 모사 — `ae_score`·`spc_violations.wafer_id` 참조는 예외."""

    def __init__(self, rows, sink):
        self.rows, self.sink = rows, sink

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sink.append((s, params))
        if "ae_score" in s:
            raise RuntimeError('UndefinedColumn: "ae_score" (실제는 anomaly_score)')
        if "spc_violations" in s and "wafer_id" in s:
            raise RuntimeError('UndefinedColumn: spc_violations.wafer_id (alert 단위 테이블)')
        self._fetch = list(self.rows)

    def fetchall(self):
        return self._fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SchemaConn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def cursor(self):
        return SchemaCursor(self.rows, self.sql)


def test_h3_query_uses_real_columns():
    """★anomaly_score + spc_flags 사용, spc_violations 조인 없음 (스키마 정합)."""
    rows = [(0.0537, ["C11"], "lean85_x"), (0.0421, [], "lean85_x")]
    conn = SchemaConn(rows)
    scores, flags, meta = M._fetch_window(conn, "SIM_CH_1", 200)
    sql = conn.sql[0][0]
    assert "anomaly_score" in sql and "ae_score" not in sql
    assert "spc_violations" not in sql, "존재하지 않는 조인이 남아 있다"
    assert "spc_flags" in sql
    assert scores == [0.0537, 0.0421] and flags == [True, False]
    assert meta["spc_true"] == 1 and meta["n"] == 2


def test_h3_excludes_dummy_model_rows():
    """기본적으로 model_version='dummy' 행을 제외한다 (더미 위 판정 무의미)."""
    conn = SchemaConn([(0.05, [], "dummy")])
    M._fetch_window(conn, "SIM_CH_1", 10)
    assert "model_version" in conn.sql[0][0] and "dummy" in conn.sql[0][0]
    conn2 = SchemaConn([(0.05, [], "dummy")])
    M._fetch_window(conn2, "SIM_CH_1", 10, exclude_dummy=False)
    assert "dummy" not in conn2.sql[0][0]


def test_h3_since_filter_applied():
    conn = SchemaConn([])
    M._fetch_window(conn, "SIM_CH_1", 10, since="2026-07-30T00:00:00Z")
    sql, args = conn.sql[0]
    assert "created_at >= %s" in sql and "2026-07-30T00:00:00Z" in args


def test_h3_dummy_suspect_rate_flags_dummy_window():
    """더미 대역·2자리 반올림이 많으면 의심률이 높다 (판정 아님, 경고 지표)."""
    dummy_like = [0.05, 0.12, 0.03, 0.14]
    real_like = [0.0537, 0.0421, 0.1132, 0.0918]
    assert M._dummy_suspect_rate(dummy_like) == 1.0
    assert M._dummy_suspect_rate(real_like) == 0.0
    assert M._dummy_suspect_rate([]) == 0.0


@pytest.mark.parametrize("raw,expected", [
    (None, False), ([], False), (["C11"], True), ('["C11"]', True), ("[]", False),
    ({"violations": ["C11"]}, True), ({"violations": []}, False), ("not-json", False),
])
def test_h3_spc_flag_parsing(raw, expected):
    """JSONB가 list/str/dict로 와도 일관 판정 (드라이버 설정 차이 방어)."""
    assert M._spc_flag_truthy(raw) is expected


def test_h3_spc_inactive_until_a3_2_is_surfaced(caplog):
    """spc_flags가 전부 빈 배열이면(A3-2 미구현) 경고로 노출 — 감시 ①이 비활성임을 알린다."""
    conn = SchemaConn([(0.05, [], "lean85_x") for _ in range(5)])
    with caplog.at_level("WARNING"):
        _s, flags, meta = M._fetch_window(conn, "SIM_CH_1", 10)
    assert meta["spc_true"] == 0 and not any(flags)
    assert any("교차침묵" in r.message or "A3-2" in r.message for r in caplog.records)


if __name__ == "__main__":                     # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# ══════════════════════════════════════════════════════════════════════════
# O1 — `--once` 1회 실행 + 종료 코드 (2026-08-05)
# ══════════════════════════════════════════════════════════════════════════

def test_once_mode_returns_exit_status(kafka_stub, monkeypatch):
    """★게이트 FAIL(BLOCKED)을 0으로 돌려주면 크론·CI 래퍼가 성공으로 오독한다.

    CT① `EXIT_STATUS` 와 같은 규약: FAILED=1 · BLOCKED=2 · IFACE_WAIT=3. SKIP·DRYRUN 은
    정상 종료(0)다 — "아무 일도 없었음"과 "막혔음"을 종료 코드에서 섞지 않는다.
    """
    kafka_stub["consumer"] = FakeConsumer([FakeMsg(EVT, offset=1)])
    monkeypatch.setattr(O, "handle_event", lambda *a: "BLOCKED")
    assert O.main(["--once"]) == 2

    kafka_stub["consumer"] = FakeConsumer([FakeMsg(EVT, offset=2)])
    O.running = True
    monkeypatch.setattr(O, "handle_event", lambda *a: "DRYRUN")
    assert O.main(["--once"]) == 0


def test_once_stops_after_one_event(kafka_stub, monkeypatch):
    """`--once` 는 우리 이벤트 1건만 처리하고 종료한다 (뒤 메시지는 건드리지 않는다)."""
    seen = []
    kafka_stub["consumer"] = FakeConsumer([FakeMsg(EVT, offset=1), FakeMsg(EVT, offset=2)])
    monkeypatch.setattr(O, "handle_event", lambda e, p: seen.append(e) or "SKIP")
    O.main(["--once"])
    assert len(seen) == 1 and kafka_stub["consumer"].commits == [1]


def test_once_failure_does_not_loop(kafka_stub, monkeypatch):
    """`--once` 에서 처리 실패는 **재시도하지 않고** FAILED(1)로 끝난다 (무한 대기 방지)."""
    kafka_stub["consumer"] = FakeConsumer([FakeMsg(EVT, offset=3)])
    monkeypatch.setattr(O, "handle_event",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("DB 다운")))
    assert O.main(["--once"]) == 1
    assert kafka_stub["consumer"].commits == [], "실패했는데 커밋했다"
