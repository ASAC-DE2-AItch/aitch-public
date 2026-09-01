# -*- coding: utf-8 -*-
"""prediction_sink 방어 로직 테스트 (2026-07-28 코드리뷰 §1-1·1-3 대응).

검증 대상:
  · 비-dict 페이로드   : `json.loads` 성공 ≠ dict. 어떤 입력에도 예외를 던지지 않고
                         스킵한다 (헌법 6-2 — 한 건이 sink 프로세스를 죽이지 않는다).
  · actual 선행 도착   : 대상 행이 없으면 라벨을 버리지 않고 pending 보관 → 이후
                         prediction 적재 시 즉시 반영 (A안, 리뷰 §1-3).
  · pending 상한       : 건수 상한 초과분은 오래된 것부터 evict (메모리 무한 성장 차단).

실행: python -m pytest tests/test_prediction_sink.py -q
psycopg2·confluent_kafka 미설치 환경에서는 모듈 통째 SKIP.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops"))

pytest.importorskip("psycopg2")
pytest.importorskip("confluent_kafka")
import prediction_sink as ps  # noqa: E402


# ── 테스트 더블 ────────────────────────────────────────────────────────────
class FakeCursor:
    """psycopg2 커서 최소 모사 — SELECT 결과와 rowcount 를 시나리오로 주입."""

    def __init__(self, select_row=None, update_rowcount=1):
        self._select_row = select_row          # SELECT id ... 결과 (None = 행 없음)
        self._update_rowcount = update_rowcount
        self.rowcount = 0
        self.statements = []                   # 실행된 SQL 앞부분 기록

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split())[:40], params))
        head = sql.strip().split()[0].upper()
        if head == "SELECT":
            self._last_select = self._select_row
            self.rowcount = 0 if self._select_row is None else 1
        else:
            self.rowcount = self._update_rowcount
            if head == "INSERT":               # 삽입 이후엔 행이 존재한다
                self._select_row = (1,)

    def fetchone(self):
        return self._select_row


@pytest.fixture(autouse=True)
def _clear_pending():
    """테스트 간 전역 pending 버퍼 격리."""
    ps.PENDING_ACTUALS.clear()
    ps.PENDING_SPC.clear()
    yield
    ps.PENDING_ACTUALS.clear()
    ps.PENDING_SPC.clear()


# ── ① 비-dict 페이로드 방어 (§1-1) ─────────────────────────────────────────
@pytest.mark.parametrize("bad", ["문자열", [1, 2], None, 123, 4.5, True])
def test_wafer_id_of_never_raises_on_non_dict(bad):
    """★ 로그 경로에서도 쓰이므로 어떤 입력에도 예외를 던지면 안 된다.

    구 코드는 `data.get("wafer_id")` 를 except 핸들러에서 다시 호출해 AttributeError 가
    루프 밖으로 탈출했다 — 즉 비정상 메시지 1건에 sink 프로세스가 죽었다.
    """
    assert ps.wafer_id_of(bad) is None


def test_wafer_id_of_extracts_and_stringifies():
    assert ps.wafer_id_of({"wafer_id": "C64_1_CH_1"}) == "C64_1_CH_1"
    assert ps.wafer_id_of({"wafer_id": 42}) == "42"
    assert ps.wafer_id_of({}) is None          # 필드 누락도 계약 위반 → None


@pytest.mark.parametrize("bad", [{"no_wafer_id": 1}, {"wafer_id": None}])
def test_upsert_skips_payload_without_wafer_id(bad):
    cur = FakeCursor()
    assert ps.upsert_prediction(cur, bad) == "skip_bad_payload"
    assert cur.statements == []                # DB 를 건드리지 않고 스킵


def test_update_actual_skips_payload_without_wafer_id():
    cur = FakeCursor()
    assert ps.update_actual(cur, {"actual_c65": 700.0}) == "skip_bad_payload"
    assert not ps.PENDING_ACTUALS              # 잘못된 페이로드는 보관도 하지 않는다


# ── ② actual 선행 도착 → pending 보관·반영 (§1-3, A안) ────────────────────
def test_actual_before_prediction_is_stashed_not_lost():
    """대상 행이 없으면 라벨을 버리지 않는다 (구: actual_no_row 로그 후 폐기)."""
    cur = FakeCursor(select_row=None, update_rowcount=0)     # UPDATE 매칭 0건
    assert ps.update_actual(cur, {"wafer_id": "W1", "actual_c65": 712.3,
                                  "measured_at": "2026-07-28T00:00:00Z"}) == "actual_pending"
    assert "W1" in ps.PENDING_ACTUALS


def test_pending_actual_applied_on_prediction_insert():
    """★ prediction 이 뒤늦게 도착하면 보관 라벨이 같은 트랜잭션에서 반영된다."""
    ps.stash_pending_actual("W1", {"actual_c65": 712.3, "measured_at": "2026-07-28T00:00:00Z"})
    cur = FakeCursor(select_row=None, update_rowcount=1)      # 행 없음 → INSERT 경로
    act = ps.upsert_prediction(cur, {"wafer_id": "W1", "predicted_c65": 700.0})

    assert act == "insert+pending_actual"
    assert "W1" not in ps.PENDING_ACTUALS                     # 반영 후 버퍼에서 제거
    assert any("actual_c65" in sql for sql, _ in cur.statements)


def test_pending_actual_applied_on_merge_path():
    ps.stash_pending_actual("W2", {"actual_c65": 88.0, "measured_at": None})
    cur = FakeCursor(select_row=(7,), update_rowcount=1)       # 기존 행 → merge 경로
    assert ps.upsert_prediction(cur, {"wafer_id": "W2", "drift_score": 0.3}) == "merge+pending_actual"
    assert "W2" not in ps.PENDING_ACTUALS


def test_pending_kept_when_update_matches_nothing():
    """★ 반영 UPDATE 가 0행이면 보관분을 지우지 않는다.

    `autocommit=True` 라 INSERT 는 이미 커밋된 상태 — 여기서 먼저 pop 해버리면 offset 을
    되감아 재소비해도 merge 경로로 흘러 라벨이 영구 유실된다.
    """
    ps.stash_pending_actual("W5", {"actual_c65": 1.0, "measured_at": None})
    cur = FakeCursor(select_row=(9,), update_rowcount=0)        # UPDATE 매칭 0건
    assert ps.upsert_prediction(cur, {"wafer_id": "W5", "predicted_c65": 1.0}) == "merge"
    assert "W5" in ps.PENDING_ACTUALS                            # 라벨 보존


def test_normal_order_does_not_touch_pending():
    """평시(prediction 선행)에는 pending 경로가 개입하지 않는다."""
    cur = FakeCursor(select_row=(3,), update_rowcount=1)
    assert ps.upsert_prediction(cur, {"wafer_id": "W3", "predicted_c65": 1.0}) == "merge"
    cur2 = FakeCursor(select_row=(3,), update_rowcount=1)
    assert ps.update_actual(cur2, {"wafer_id": "W3", "actual_c65": 1.0}) == "actual"
    assert not ps.PENDING_ACTUALS


# ── ③ pending 버퍼 상한 (메모리 가드) ─────────────────────────────────────
def test_pending_buffer_evicts_oldest_beyond_max(monkeypatch):
    monkeypatch.setattr(ps, "PENDING_ACTUAL_MAX", 3)
    for i in range(5):
        ps.stash_pending_actual(f"W{i}", {"actual_c65": float(i), "measured_at": None})
    assert len(ps.PENDING_ACTUALS) == 3
    assert list(ps.PENDING_ACTUALS) == ["W2", "W3", "W4"]     # 오래된 것부터 폐기


def test_pending_buffer_evicts_expired(monkeypatch):
    ps.stash_pending_actual("W9", {"actual_c65": 1.0, "measured_at": None})
    monkeypatch.setattr(ps, "PENDING_ACTUAL_TTL_SEC", -1.0)   # 즉시 만료 판정
    ps._evict_pending()
    assert "W9" not in ps.PENDING_ACTUALS


# ══════════════════════════════════════════════════════════════════════════
# M1 — `fdc.alert` → spc_flags 적재 (감시① 개통, 2026-08-05)
# ══════════════════════════════════════════════════════════════════════════

def _alert(wafer="C64_1", flags=(("C11", "N1"),)):
    """`fdc.alert` 봉투 — wafer 키는 계약상 `prediction_context.wafer_id` 다.

    최상위 `wafer_id` 는 **없다**(C 파서가 조용히 드롭하므로 B 가 싣지 않는다). 그래서
    sink 도 거기서 찾으면 안 된다.
    """
    return {"alert_id": "ALERT-20260805-SIMCH1-0001", "chamber_id": "SIM_CH_1",
            "violations": [{"sensor": s, "rule_id": r} for s, r in flags],
            "prediction_context": {"wafer_id": wafer, "predicted_c65": 1200.0,
                                   "shap_top3": ["C11"],
                                   "spc_flags": [{"sensor": s, "rule": r} for s, r in flags]}}


def test_alert_updates_spc_flags_only():
    """★alert 은 `spc_flags` **한 컬럼만** 만진다 — 예측값을 B 값으로 덮지 않는다."""
    cur = FakeCursor(select_row=(7,))
    assert ps.update_spc_flags(cur, _alert()) == "spc_flags"
    sql, params = cur.statements[-1]
    assert sql.startswith("UPDATE wafer_predictions SET spc_flags=")
    assert "predicted_c65" not in sql and "anomaly_score" not in sql
    assert params[1] == "C64_1"


def test_alert_before_prediction_is_buffered_not_lost():
    """★alert 선행 도착 — `fdc.alert`·`fdc.prediction` 은 다른 토픽이라 순서 보장이 없다.

    대상 행이 없다고 버리면 그 wafer 의 Nelson 플래그가 영구 유실되고, 감시①이 다시
    `n_spc=0` 을 본다. actual 라벨과 같은 A안 버퍼로 보관했다가 prediction 도착 시 반영한다.
    (`predicted_c65 NOT NULL` 이라 placeholder 행은 못 만든다 — B안은 스키마 변경 = PM 승인.)
    """
    cur = FakeCursor(select_row=None, update_rowcount=0)
    assert ps.update_spc_flags(cur, _alert(wafer="C64_9")) == "spc_flags_pending"
    assert "C64_9" in ps.PENDING_SPC

    # 이후 예측이 도착하면 즉시 반영된다
    cur2 = FakeCursor(select_row=None, update_rowcount=1)
    act = ps.upsert_prediction(cur2, {"wafer_id": "C64_9", "predicted_c65": 1000.0})
    assert "pending_spc" in act and "C64_9" not in ps.PENDING_SPC


def test_pending_spc_survives_failed_update():
    """UPDATE 가 0행이면 **보관분을 지우지 않는다** (먼저 지우면 영구 유실 — actual 과 같은 규율)."""
    ps.stash_pending_spc("C64_9", [{"sensor": "C11", "rule": "N1"}])
    cur = FakeCursor(select_row=(1,), update_rowcount=0)
    assert ps.flush_pending_spc(cur, "C64_9") is False
    assert "C64_9" in ps.PENDING_SPC


def test_pending_spc_evicted_by_ttl():
    """TTL 만료분은 폐기 — 무한 성장 차단 (actual 버퍼와 공용 정리)."""
    ps.PENDING_SPC["old"] = ([{"sensor": "C11"}], 0.0)      # epoch = 아주 오래됨
    assert ps._evict_pending() >= 1 and "old" not in ps.PENDING_SPC


def test_alert_bad_payload_is_skipped():
    """계약 위반 봉투는 예외 없이 스킵 (헌법 6-2 — 한 건이 sink 를 죽이지 않는다)."""
    cur = FakeCursor(select_row=(1,))
    assert ps.update_spc_flags(cur, {}) == "skip_no_context"
    assert ps.update_spc_flags(cur, {"prediction_context": "nope"}) == "skip_no_context"
    assert ps.update_spc_flags(cur, {"prediction_context": {}}) == "skip_bad_payload"
    assert ps.update_spc_flags(cur, {"prediction_context": {"wafer_id": "W", "spc_flags": 1}}) \
        == "skip_no_flags"
    assert cur.statements == [], "스킵인데 UPDATE 를 실행했다"


def test_prediction_does_not_erase_spc_flags():
    """★핵심 회귀 — 예측 메시지가 기존 `spc_flags` 를 지우면 안 된다.

    `COALESCE(%s, spc_flags)` 에서 `[]` 는 **NULL 이 아닌 값**이라 그대로 통과했다. A consumer
    가 빈 배열을 싣던 동안, B 가 채운 Nelson 플래그는 **다음 예측이 도착하는 순간** 사라졌고
    감시①(SPC↑/AE 침묵 교차)이 표본 0으로 영구 유예됐다. 발행(consumer)과 적재(여기) 양쪽에서 막는다.
    """
    cur = FakeCursor(select_row=(3,))
    ps.upsert_prediction(cur, {"wafer_id": "C64_1", "predicted_c65": 1000.0, "spc_flags": []})
    _sql, params = cur.statements[-1]
    assert params[4] is None, f"빈 spc_flags 가 그대로 실렸다: {params[4]}"


def test_prediction_carries_nonempty_spc_flags():
    """비지 않은 값은 정상 반영 (가드가 유효값까지 막지 않는다)."""
    cur = FakeCursor(select_row=(3,))
    ps.upsert_prediction(cur, {"wafer_id": "C64_1", "predicted_c65": 1000.0,
                               "spc_flags": [{"sensor": "C11", "rule": "N1"}]})
    assert cur.statements[-1][1][4] is not None


def test_nonempty_spc_helper():
    assert ps._nonempty_spc([]) is None
    assert ps._nonempty_spc(None) is None
    assert ps._nonempty_spc("nope") is None
    assert ps._nonempty_spc([{"sensor": "C11"}]) == [{"sensor": "C11"}]


def test_sink_subscribes_alert_topic():
    """토픽 상수 등재 — 구독 목록에서 빠지면 적재 경로가 통째로 죽는다."""
    import inspect
    assert ps.TOPIC_ALERT == "fdc.alert"
    src = inspect.getsource(ps.main)
    assert "TOPIC_ALERT" in src and "update_spc_flags" in src


def test_consumer_no_longer_publishes_empty_spc_flags():
    """★발행 측 — `build_message` 가 `spc_flags` 키를 더는 싣지 않는다 (M1).

    consumer 모듈 import 는 환경 의존이라, 소스 텍스트로 회귀를 고정한다(값싼 가드).
    """
    src = (REPO_ROOT / "src" / "agent_a_mlops" / "consumer.py").read_text(encoding="utf-8")
    # 주석은 뺀다 — 그 줄들은 **왜 뺐는지**를 설명하려고 옛 코드를 인용하고 있다.
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    hits = [ln.strip() for ln in code if '"spc_flags"' in ln]
    assert not hits, f"빈 배열 발행이 되살아났다 (감시① 재사망): {hits}"
