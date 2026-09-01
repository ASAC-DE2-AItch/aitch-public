# -*- coding: utf-8 -*-
"""CT⓪ updater 배선 테스트 — **기록 → 파일 → 커밋** 순서 규율 (설계 원칙 2·D6).

가짜 DB 커넥션만으로 돈다 (`confluent_kafka`·`psycopg2` 는 지연 import 라 불필요).
검증 대상:
  · 라벨 1건 처리: join → 잔차 → 창 → 결정 → 감사 행 → bias 파일
  · **기록 실패 시 파일 미교체·상태 미변경** (fail-closed — 무기록 갱신 금지)
  · 중복 전달(ct_id 충돌)은 서빙 갱신을 하지 않는다 (멱등)
  · join miss·Qual·비유한 라벨·자격 미달의 skip 경로
  · 리셋 이벤트: 1회 반영 + 재전달 무시 + RESET 감사 행
  · `ct_id` 날짜는 **메시지 시각**에서 뽑는다 (재처리 시 같은 ID)

실행: python -m pytest src/agent_a_mlops/ct0_bias/tests/test_ct0_updater.py -q
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ct0_bias import bootstrap, control_io, core, updater  # noqa: E402


# ── 테스트 더블 ────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        head = " ".join(sql.split())
        self.conn.statements.append((head[:60], params))
        if head.startswith("INSERT INTO ct_decisions"):
            if self.conn.insert_raises:
                raise RuntimeError("DB 다운")
            ct_id = params[0]
            self._result = None if ct_id in self.conn.rows else (ct_id,)
            self.conn.rows[ct_id] = params
        elif "FROM wafer_predictions" in head:
            self._result = self.conn.pred_row
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, pred_row=None, insert_raises=False):
        self.pred_row = pred_row
        self.insert_raises = insert_raises
        self.rows = {}                 # ct_id → params (감사 행)
        self.statements = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _up(tmp_path, monkeypatch, *, mode=core.MODE_ACTIVE, pred_row=None, insert_raises=False,
        window=0, boundary=core.NO_BOUNDARY, seed_pm=25):
    """updater 1개 + 미리 채운 창. bias 파일은 tmp 디렉토리로 격리.

    `seed_pm` = 씨앗 라벨의 pm_count. 리셋 테스트는 **구레짐(24)** 을 심어야 실제 상황
    (라벨이 60일 늦게 오므로 리셋 시점의 창은 전부 이전 레짐)을 재현한다.
    """
    monkeypatch.setenv("CT0_BIAS_DIR", str(tmp_path))
    monkeypatch.setattr(updater, "JOIN_RETRY_SEC", 0.0)          # 테스트 지연 제거
    cfg = core.BiasConfig(mode=mode, window_n=500, min_labels=50, max_rmse_ratio=1.0,
                          min_update_delta=1.0)
    conn = FakeConn(pred_row=pred_row, insert_raises=insert_raises)
    up = updater.Updater(conn, cfg, train_rmse=100.0)

    st = core.BiasState(chamber_id="SIM_CH_3", train_rmse=100.0, valid_from_pm_count=boundary)
    for i in range(window):
        core.push_label(st, core.Label(f"seed_{i}", 100.0, pm_count=seed_pm,
                                       model_version="lean85_v1"), cfg)
    up.states["SIM_CH_3"] = st
    monkeypatch.setattr(bootstrap, "rebuild_state",
                        lambda *a, **k: core.BiasState(chamber_id=a[1], train_rmse=100.0))
    return up, conn, st


def _actual(wafer="C64_9", actual_c65=1000.0, pm_count=25):
    return {"wafer_id": wafer, "chamber_id": "SIM_CH_3", "actual_c65": actual_c65,
            "pm_count": pm_count, "measured_at": "2026-09-07T09:00:00.000Z",
            "processed_at": "2026-07-13T10:00:00.000Z"}


def _bias_file(tmp_path):
    p = control_io.bias_path("SIM_CH_3", tmp_path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ── 정상 경로 ─────────────────────────────────────────────────────────────
def test_label_updates_window_and_records(tmp_path, monkeypatch):
    """창 충족 상태에서 라벨 1건 → 감사 행 1개 + bias 파일 교체.

    발행 예측 900 = raw 850 + bias 50 이므로 실측 950 의 잔차는 **150 이 아니라 100** 이다
    (D1 — 보정 전 예측 대비). 씨앗 100 과 같은 값이라 rolling mean 은 100 을 유지한다.
    """
    up, conn, st = _up(tmp_path, monkeypatch, pred_row=(900.0, 50.0, False, "lean85_v1",
                                                        "SIM_CH_3"), window=60)
    act = up.handle_actual(_actual(actual_c65=950.0))
    assert act.startswith("update")
    assert st.n == 61 and st.bias_applied != 0.0
    assert len(conn.rows) == 1
    ct_id, params = next(iter(conn.rows.items()))
    assert ct_id.endswith("-R2R") and params[1] == core.CT_TYPE
    assert params[3] == core.STATUS_APPLIED
    saved = _bias_file(tmp_path)
    assert saved["bias"] == st.bias_applied and saved["mode"] == core.MODE_ACTIVE


def test_shadow_mode_records_but_serves_zero(tmp_path, monkeypatch):
    """shadow: 감사 행은 SHADOW, 파일의 서빙값은 0 (적용·발행 0 — 설계 §13 P1)."""
    up, conn, st = _up(tmp_path, monkeypatch, mode=core.MODE_SHADOW,
                       pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=60)
    up.handle_actual(_actual())
    params = next(iter(conn.rows.values()))
    assert params[3] == core.STATUS_SHADOW
    assert _bias_file(tmp_path)["bias"] == 0.0
    assert st.bias_applied != 0.0                     # 상태(추정)는 살아 있다


def test_no_record_when_below_delta(tmp_path, monkeypatch):
    """δ 미만 변동은 행도 파일도 만들지 않는다 (D4 — 기록량 O(변경))."""
    up, conn, st = _up(tmp_path, monkeypatch,
                       pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=60)
    st.bias_applied = 100.0                           # 이미 같은 값을 서빙 중
    act = up.handle_actual(_actual(actual_c65=1000.0))
    assert act.startswith("window") and not conn.rows
    assert _bias_file(tmp_path) is None


# ── fail-closed·멱등 ──────────────────────────────────────────────────────
def test_record_failure_blocks_file_and_state(tmp_path, monkeypatch):
    """감사 기록이 실패하면 서빙값도 파일도 바뀌지 않는다 (원칙 2 — 기록 없으면 갱신 없음)."""
    up, conn, st = _up(tmp_path, monkeypatch, insert_raises=True,
                       pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=60)
    before = st.bias_applied
    try:
        up.handle_actual(_actual())
    except RuntimeError:
        pass                                          # main 루프가 되감기·재시도로 다룬다
    else:
        raise AssertionError("기록 실패가 삼켜졌다 — 무기록 갱신 위험")
    assert st.bias_applied == before and _bias_file(tmp_path) is None


def test_duplicate_ct_id_adds_no_row_but_converges(tmp_path, monkeypatch):
    """재전달 → ct_id 충돌 → **행은 안 늘고** 서빙값·파일은 그 행의 값으로 수렴한다.

    "행이 이미 있으면 손 떼기"로 만들면, 직전 시도가 INSERT 성공 후 파일 교체에서 실패한
    경우 **감사 행은 있는데 서빙은 옛 값**인 상태가 영구히 남는다 (재처리해도 계속 중복).
    """
    up, conn, st = _up(tmp_path, monkeypatch,
                       pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=60)
    assert up.handle_actual(_actual()).startswith("update")
    expected = st.bias_applied
    st.seen.discard("C64_9")                          # 창 중복 가드를 우회해 ID 충돌만 본다
    st.bias_applied = 0.0                             # 파일 교체 실패로 어긋난 상태를 모사
    act = up.handle_actual(_actual())
    assert act == "duplicate_decision"
    assert len(conn.rows) == 1                        # 새 행 없음 (DO NOTHING)
    assert st.bias_applied == expected and _bias_file(tmp_path)["bias"] == expected


# ── skip 경로 ─────────────────────────────────────────────────────────────
def test_join_miss_is_skipped(tmp_path, monkeypatch):
    """예측 행이 아직 없으면 (sink 지연) 창 표본만 줄고 넘어간다."""
    up, conn, _ = _up(tmp_path, monkeypatch, pred_row=None, window=60)
    assert up.handle_actual(_actual()) == "skip_join_miss"
    assert up.n_join_miss == 1 and not conn.rows


def test_qual_wafer_is_skipped(tmp_path, monkeypatch):
    """Qual wafer 라벨은 창에 넣지 않는다 (D7 2중 가드 — 애초에 미발행)."""
    up, _, st = _up(tmp_path, monkeypatch,
                    pred_row=(900.0, 0.0, True, "lean85_v1", "SIM_CH_3"), window=60)
    assert up.handle_actual(_actual()) == "skip_qual" and st.n == 60


def test_nonfinite_label_is_skipped(tmp_path, monkeypatch):
    up, _, st = _up(tmp_path, monkeypatch,
                    pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=60)
    assert up.handle_actual(_actual(actual_c65=float("nan"))) == "skip_nonfinite"
    assert up.handle_actual(_actual(actual_c65=None)) == "skip_nonfinite"
    assert st.n == 60


def test_ineligible_label_is_skipped_and_counted(tmp_path, monkeypatch):
    """구레짐 라벨(pm_count < 경계)은 배제하고 센다 — 되감김 감지의 신호."""
    up, _, st = _up(tmp_path, monkeypatch,
                    pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"),
                    window=0, boundary=25)
    assert up.handle_actual(_actual(pm_count=24)) == "skip_ineligible"
    assert up.n_ineligible == 1 and st.n == 0


# ── 리셋 ─────────────────────────────────────────────────────────────────
def _verdict(pm_count=25, verdict="loud"):
    return {"event_type": "QualVerdictConfirmed", "qual_id": "QUAL-20260713-SIMCH3-001",
            "chamber_id": "SIM_CH_3", "verdict": verdict, "pm_count": pm_count,
            "confirmed_at": "2026-07-13T11:00:00.000Z"}


def test_loud_verdict_resets_and_records(tmp_path, monkeypatch):
    """구레짐 라벨(pm_count=24)로 찬 창이 리셋으로 비워지고 bias 가 0 이 된다."""
    up, conn, st = _up(tmp_path, monkeypatch, window=60, seed_pm=24)
    st.bias_applied = 80.0
    assert up.handle_agent(_verdict()) == "reset"
    assert st.n == 0 and st.bias_applied == 0.0 and st.valid_from_pm_count == 25
    params = next(iter(conn.rows.values()))
    assert params[3] == core.STATUS_RESET and core.parse_reset_reason(params[2]) == 25
    assert params[7] == "QUAL-20260713-SIMCH3-001"     # qual_id 로 추적 가능
    assert _bias_file(tmp_path)["bias"] == 0.0


def test_reset_redelivery_is_noop(tmp_path, monkeypatch):
    up, conn, st = _up(tmp_path, monkeypatch)
    assert up.handle_agent(_verdict()) == "reset"
    for i in range(60):                                # 신레짐 라벨이 쌓인 뒤
        core.push_label(st, core.Label(f"n{i}", 10.0, pm_count=25), up.cfg)
    assert up.handle_agent(_verdict()) == "skip_reset_idempotent"
    assert st.n == 60 and len(conn.rows) == 1          # 창 보존 · 행 1개


def test_reset_keeps_labels_of_the_new_regime(tmp_path, monkeypatch):
    """리셋은 **일괄 삭제가 아니라 자격 필터**다 — 이미 신레짐(pm_count≥경계)인 라벨은 남는다.

    이 구분이 중요한 이유: 리셋 이벤트가 늦게 도착하는 경우(부트스트랩 복구·유실 재전달)
    무조건 비우면 이미 유효한 신레짐 표본까지 날아가 보정이 불필요하게 오래 꺼진다.
    """
    up, _, st = _up(tmp_path, monkeypatch, window=60, seed_pm=25)
    st.bias_applied = 80.0
    assert up.handle_agent(_verdict(pm_count=25)) == "reset"
    assert st.n == 60 and st.bias_applied == 0.0        # 창은 유지, 서빙값은 0 으로


def test_quiet_verdict_and_other_events_are_ignored(tmp_path, monkeypatch):
    """조용 PM 은 레짐 전환이 아니다 (bias 연속) · Brief 등 타 이벤트는 event_type 으로 skip."""
    up, conn, st = _up(tmp_path, monkeypatch, window=60)
    assert up.handle_agent(_verdict(verdict="quiet")) == "skip_quiet"
    assert up.handle_agent({"event_type": "SupervisorBrief"}) == "skip_other_event"
    assert st.n == 60 and not conn.rows


def test_reset_without_pm_count_is_refused(tmp_path, monkeypatch):
    """경계는 정수 축이다 — pm_count 없는 이벤트로는 리셋하지 않는다 (조용한 오작동 방지)."""
    up, conn, st = _up(tmp_path, monkeypatch, window=60)
    ev = _verdict()
    ev.pop("pm_count")
    assert up.handle_agent(ev) == "skip_no_pm_count"
    assert st.n == 60 and not conn.rows


# ── 더미 폴백·부트스트랩 대조 (2026-08-07 리뷰 대응) ──────────────────────
def test_dummy_prediction_label_is_skipped(tmp_path, monkeypatch):
    """더미 폴백 예측(난수)의 라벨은 창에 넣지 않는다 (계약 §2 오염 금지)."""
    up, conn, st = _up(tmp_path, monkeypatch,
                       pred_row=(100.0, 0.0, False, "dummy", "SIM_CH_3"), window=60)
    assert up.handle_actual(_actual()) == "skip_dummy"
    assert st.n == 60 and up.n_dummy == 1 and not conn.rows


def test_rmse_refresh_fires_even_on_state_cache_hit(tmp_path, monkeypatch):
    """★회귀 (리뷰 H1) — clamp 분모 갱신이 **상태 캐시 미스일 때만** 돌면 안 된다.

    챔버가 워밍업된 뒤에는 리밸런스·처리 실패로 상태가 폐기되기 전까지 `state_for` 의
    재계산 분기가 안 불린다. 그 사이 CT① 이 champion 을 교체하면 CAP 상한(±1.0×RMSE)만
    옛 모델 기준으로 남아, 헌법 1-1 ⓐ 의 '상한'이 실제 서빙 모델과 어긋난다.
    """
    up, _, st = _up(tmp_path, monkeypatch,
                    pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=60)
    calls = []

    def fake_resolve():
        calls.append(1)
        return 120.0, "test"

    monkeypatch.setattr(bootstrap, "resolve_train_rmse", fake_resolve)
    up._rmse_at = time.monotonic() - updater.RMSE_REFRESH_SEC - 1   # 스로틀 만료 상태
    up.handle_actual(_actual())                                     # 상태는 이미 캐시에 있다

    assert calls, "캐시 히트 경로에서 분모 재해석이 아예 안 돌았다"
    assert up.train_rmse == 120.0 and st.train_rmse == 120.0        # 기존 상태까지 전파


def test_rmse_refresh_is_throttled(tmp_path, monkeypatch):
    """매 라벨 호출이지만 1h 스로틀이라 실제 해석은 드물게 — 비용 0 이 전제다."""
    up, _, _ = _up(tmp_path, monkeypatch,
                   pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=60)
    calls = []
    monkeypatch.setattr(bootstrap, "resolve_train_rmse",
                        lambda: (calls.append(1) or (100.0, "test")))
    for i in range(5):
        up.handle_actual(_actual(wafer=f"C64_{i}"))
    assert not calls                                                # 방금 기동 = 스로틀 내


def test_reconcile_fixes_stale_file(tmp_path, monkeypatch):
    """재기동 재계산 결과와 서빙 파일이 다르면 **감사 행과 함께** 맞춘다.

    D6 이 상정한 창(INSERT 성공 → 파일 교체 실패 → 크래시)에서 재기동하면 파일만 옛 값인데,
    δ 비교는 메모리 기준이라 손대지 않으면 다음 δ 이상 변동까지 영영 어긋난 채로 남는다.
    """
    up, conn, st = _up(tmp_path, monkeypatch, window=60)   # 창 = 잔차 100 × 60 → bias 100
    control_io.write_bias("SIM_CH_3", control_io.bias_payload(
        "SIM_CH_3", bias=7.0, status=core.STATUS_APPLIED, mode=core.MODE_ACTIVE, n=60,
        raw_bias=7.0, cap=100.0, ct_id="CT-old-R2R", valid_from_pm_count=25,
        train_rmse=100.0), tmp_path)                       # 파일만 옛 값(7.0)

    assert up.reconcile_file(st, "20260907") is True
    assert _bias_file(tmp_path)["bias"] == 100.0
    assert len(conn.rows) == 1                             # 기록 없는 서빙 변경은 만들지 않는다


def test_reconcile_noop_when_file_matches(tmp_path, monkeypatch):
    """일치하면 아무 것도 하지 않는다 — 재기동마다 행이 늘면 감사가 노이즈가 된다."""
    up, conn, st = _up(tmp_path, monkeypatch, window=0)     # 표본 0 → bias 0, 파일 없음
    assert up.reconcile_file(st, "20260907") is False
    assert not conn.rows and _bias_file(tmp_path) is None


def test_cap_event_carries_existing_ct_id(tmp_path, monkeypatch):
    """CAP 이벤트의 `ct_id` 는 **실재하는 감사 행**이어야 한다 (계약 §7-5 감사 정본)."""
    sent = []

    class FakeProducer:
        def produce(self, topic, key=None, value=None):
            sent.append((topic, json.loads(value.decode())))

        def poll(self, _t):
            pass

    up, conn, st = _up(tmp_path, monkeypatch,
                       pred_row=(900.0, 0.0, False, "lean85_v1", "SIM_CH_3"), window=0)
    up.producer = FakeProducer()
    for i in range(60):                                    # 상한(100) 크게 초과하는 창
        core.push_label(st, core.Label(f"big_{i}", 500.0, pm_count=25,
                                       model_version="lean85_v1"), up.cfg)
    up.handle_actual(_actual(actual_c65=1400.0))           # 잔차 500

    assert sent and sent[0][0] == "fdc.mlops"
    ev = sent[0][1]
    assert ev["event_type"] == "ModelBiasCapExceeded"
    assert ev["bias_applied"] == 100.0 and ev["raw_bias"] > 100.0
    assert ev["ct_id"] in conn.rows                         # dangling 참조 금지


# ── ct_id 날짜 축 ─────────────────────────────────────────────────────────
def test_day_comes_from_message_not_wall_clock():
    """재처리가 같은 ct_id 를 내려면 날짜가 메시지에서 나와야 한다 (멱등의 전제)."""
    assert updater._day_of({"measured_at": "2026-09-11T09:00:00.000Z"}) == "20260911"
    assert updater._day_of({"confirmed_at": "2026-07-13T11:00:00.000Z"}) == "20260713"
    assert updater._day_of({"processed_at": "2026-07-13T10:00:00.000Z"}) == "20260713"
    assert len(updater._day_of({})) == 8               # 최후 폴백 = 벽시계
