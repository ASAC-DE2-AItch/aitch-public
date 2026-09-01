# -*- coding: utf-8 -*-
"""P3 AE 실연동 단위·패리티 테스트 — consumer.py의 AE 경로 검증 (A3-3).

계획 §P3 대응:
  · rows_to_trace_ae   : fdc.raw 모사 rows → 컬럼(C64·C7·C42·C46·C10·C24+센서)·타입
  · C42 결측 스킵      : stabilization_flag=None row 스킵+로그 (헌법 6-2 / 계획 §5-6)
  · dedup 가드         : (C64,C7,C46) 중복 → prepare_frame 후 1행 (불가침 11)
  · build_message      : 실값 매핑 / 더미 폴백 / 확장필드 §2-2 게이트(승인 전 미발행)
  · AE 실패 독립 폴백  : AE 스코어링 예외 시 flush 생존 + 더미 발행 (헌법 6-2)
  · 패리티(핵심)       : 오프라인 infer score ↔ consumer 재구성 경로 ae_score 대조
                          (perturb off, N=20, 0.2 임계 판정 100% 동일 — 계획 §P3-2)

실행: python -m pytest tests/test_ae_consumer.py -q   (또는 python tests/test_ae_consumer.py)
패리티는 torch·번들·parquet 없으면 자동 SKIP (단위 테스트는 항상 수행).
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
import pandas as pd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
AE_DIR = REPO_ROOT / "src" / "agent_a_mlops" / "Autoencoder"
BUNDLE_DIR = REPO_ROOT / "models" / "anomaly_ae" / "ae_v1"
PARQUET = AE_DIR / "merged_data_v3.parquet"

# consumer는 confluent_kafka·lean85_pipeline에 의존 — 없으면 모듈 통째 스킵
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops"))
try:
    import consumer  # noqa: E402
except Exception as e:                       # pragma: no cover - 환경 의존
    pytest.skip(f"consumer import 실패(의존성 미설치): {e}", allow_module_level=True)

# ae_pipeline (dedup·패리티용) — numpy/pandas만 필요, torch 무관
sys.path.insert(0, str(AE_DIR))


# ── 테스트 헬퍼 ────────────────────────────────────────────────────────────
def _raw_row(step=4, stab=0, seq=0, ts="2026-07-22T10:00:00", **sensors):
    """fdc.raw 1건(consumer 버퍼 저장형) 모사. sensors는 물리 C코드 kv."""
    base = {"C11": 1.0, "C17": 2.0, "C15": 0.5, "C16": 0.7, "C27": 0.3,
            "C32": 0.4, "C61": 0.2, "C62": 0.1, "C59": 1200.0, "C60": 5.0,
            "C33": 100.0}
    base.update(sensors)
    return {"step": step, "sensors": base, "lot_id": "LOT_1", "recipe_id": "C6_0",
            "timestamp": ts, "src_ts": None, "seq_in_step": seq,
            "stabilization_flag": stab}


class FakeProducer:
    """produce/poll/flush만 필요 — 발행 메시지 캡처.

    `on_delivery` 콜백 kwarg 를 받는다 (2026-07-28 리뷰 §2-1 — 전달 실패 가시화).
    """

    def __init__(self):
        self.produced = []

    def produce(self, topic, key=None, value=None, **kwargs):
        self.produced.append({"topic": topic, "key": key, "value": value, **kwargs})

    def poll(self, _timeout):
        return 0

    def flush(self, _timeout=None):
        return 0


# ── 1) rows_to_trace_ae 스키마·타입 ────────────────────────────────────────
def test_rows_to_trace_ae_schema_and_types():
    rows = [_raw_row(step=4, stab=1, seq=0), _raw_row(step=4, stab=0, seq=1)]
    meta = {"chamber_id": "SIM_CH_3", "is_qual": False}
    df = consumer.rows_to_trace_ae("C64_1_CH_3", meta, rows)

    for col in ("C64", "C7", "C42", "C46", "C10", "C24"):
        assert col in df.columns, f"필수 컬럼 누락: {col}"
    # 물리센서 보존 (C11·C17 등)
    assert "C11" in df.columns and "C17" in df.columns
    assert len(df) == 2
    assert (df["C64"] == "C64_1_CH_3").all()
    assert (df["C24"] == "SIM_CH_3").all()            # meta chamber → C24 (DriftTracker 키)
    assert df["C42"].dtype.kind in "iu"               # 정수형 마스크
    assert pd.api.types.is_datetime64_any_dtype(df["C10"])   # 정렬 전용 시각
    assert list(df["C7"]) == [4, 4] and list(df["C46"]) == [0, 1]


# ── 2) C42 결측 row 스킵 (헌법 6-2 / 계획 §5-6) ────────────────────────────
def test_rows_to_trace_ae_skips_missing_c42():
    rows = [_raw_row(stab=0, seq=0), _raw_row(stab=None, seq=1), _raw_row(stab=1, seq=2)]
    df = consumer.rows_to_trace_ae("C64_9_CH_1", {"chamber_id": "SIM_CH_1"}, rows)
    assert len(df) == 2                               # 결측 1건 스킵
    assert set(df["C46"]) == {0, 2}                   # seq=1(결측) 제외


# ── 3) dedup 가드 (C64,C7,C46) — prepare_frame 후 1행 (불가침 11) ──────────
def test_dedup_guard_prepare_frame():
    from ae_pipeline.features import prepare_frame
    rows = [_raw_row(step=4, stab=0, seq=0),
            _raw_row(step=4, stab=0, seq=0, C17=9.9),   # (C7,C46) 중복 → 첫행만
            _raw_row(step=4, stab=0, seq=1)]
    trace = consumer.rows_to_trace_ae("C64_2_CH_2", {"chamber_id": "SIM_CH_2"}, rows)
    assert len(trace) == 3
    deduped, n_removed = prepare_frame(trace)
    assert n_removed == 1 and len(deduped) == 2        # (C64,C7,C46) 유일화


# ── 4) build_message 실값 매핑 / 더미 폴백 ─────────────────────────────────
def _pred(c65=712.3):
    return {"pred_c65": c65, "low_confidence": 0, "model_version": "lean85_x",
            "shap_top3": [{"sensor": "C17", "name": "C17", "contribution": 0.4}]}


def test_build_message_real_ae_values():
    ae_rec = {"ae_drift_score": 0.0345, "ae_score": 0.1876, "ae_raw": 88.8,
              "ae_top_channels": ["C27_t_min"], "ae_model_version": "z16_rev3_seg1",
              "ae_calib_version": "calib_v1", "transient_context": True}
    msg = consumer.build_message("C64_1_CH_3", {"chamber_id": "SIM_CH_3"}, _pred(), ae_rec)
    assert msg["drift_score"] == 0.0345               # ae_drift_score → drift_score
    assert msg["anomaly_score"] == 0.1876             # ae_score → anomaly_score
    assert msg["predicted_c65"] == 712.3


def test_build_message_dummy_fallback_ranges():
    msg = consumer.build_message("C64_1_CH_3", {"chamber_id": "SIM_CH_3"}, _pred(), None)
    assert 0.1 <= msg["drift_score"] <= 0.4           # 더미 폴백 범위 (안전망)
    assert 0.01 <= msg["anomaly_score"] <= 0.15


# ── 5) 확장필드는 §2-2 승인 게이트 전까지 미발행 (계약 준수) ────────────────
def test_build_message_ext_fields_gated_off_by_default():
    ae_rec = {"ae_drift_score": 0.03, "ae_score": 0.18, "ae_raw": 88.8,
              "ae_top_channels": ["C27_t_min"], "ae_model_version": "z16_rev3_seg1",
              "ae_calib_version": "calib_v1", "transient_context": True}
    old = consumer.AE_PUBLISH_EXT
    consumer.AE_PUBLISH_EXT = False               # 기본(미승인) 상태 고정 — 환경 무관
    try:
        msg = consumer.build_message("C64_1_CH_3", {"chamber_id": "SIM_CH_3"}, _pred(), ae_rec)
    finally:
        consumer.AE_PUBLISH_EXT = old
    # AE_PUBLISH_EXT=0(기본) → 확장필드 미포함 (2단계 승인 전 발행 금지)
    for k in ("ae_raw", "ae_top_channels", "ae_model_version",
              "ae_calib_version", "transient_context"):
        assert k not in msg, f"승인 전 확장필드 누출: {k}"


def test_build_message_ext_fields_published_when_approved():
    """AE_PUBLISH_EXT=1(승인 후)일 때만 확장필드 발행 (게이트 반대편 검증)."""
    ae_rec = {"ae_drift_score": 0.03, "ae_score": 0.18, "ae_raw": 88.8,
              "ae_top_channels": ["C27_t_min"], "ae_model_version": "z16_rev3_seg1",
              "ae_calib_version": "calib_v1", "transient_context": True}
    old = consumer.AE_PUBLISH_EXT
    consumer.AE_PUBLISH_EXT = True
    try:
        msg = consumer.build_message("C64_1_CH_3", {"chamber_id": "SIM_CH_3"}, _pred(), ae_rec)
    finally:
        consumer.AE_PUBLISH_EXT = old
    assert msg["ae_raw"] == 88.8 and msg["ae_model_version"] == "z16_rev3_seg1"
    assert msg["transient_context"] is True


# ── 5-b) NaN/Inf 발행 방어 (2026-07-28 리뷰 §1-5) ──────────────────────────
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_build_message_rejects_non_finite_prediction(bad):
    """★ NaN/Inf 는 표준 JSON 이 아니다 — 발행 길목에서 유한값으로 대체된다.

    `json.dumps` 는 NaN 을 비표준 리터럴로 직렬화해 대시보드(JS `JSON.parse`)와
    sink 의 NOT NULL 수치 컬럼을 동시에 깨뜨린다.
    """
    pred = dict(_pred(), pred_c65=bad)
    msg = consumer.build_message("C64_1_CH_3", {"chamber_id": "SIM_CH_3"}, pred, None)
    assert msg["predicted_c65"] == consumer.DUMMY_C65_BASE
    json.dumps(msg, allow_nan=False)          # 표준 JSON 직렬화 가능(예외 없음)


def test_build_message_falls_back_when_ae_values_non_finite():
    """AE 실값이 NaN 이면 더미 폴백 범위로 되돌린다 (파이프라인 생존)."""
    ae_rec = {"ae_drift_score": float("nan"), "ae_score": 0.18}
    msg = consumer.build_message("C64_1_CH_3", {"chamber_id": "SIM_CH_3"}, _pred(), ae_rec)
    assert 0.1 <= msg["drift_score"] <= 0.4
    assert 0.01 <= msg["anomaly_score"] <= 0.15
    json.dumps(msg, allow_nan=False)


def test_finite_accepts_numpy_scalars():
    """numpy 스칼라를 '비수치'로 오판해 실측값을 더미로 바꿔버리면 안 된다."""
    np = pytest.importorskip("numpy")
    assert consumer._finite(np.float32(0.25)) == pytest.approx(0.25, abs=1e-6)
    assert consumer._finite(np.float64(1.5)) == 1.5
    assert consumer._finite(np.int32(3)) == 3.0
    assert consumer._finite(np.float32("nan")) is None
    assert consumer._finite("0.5") is None            # 문자열은 수치 계약 위반
    assert consumer._finite(True) is None             # bool 은 int 서브클래스라 별도 차단


def test_late_row_after_flush_does_not_republish():
    """★ 유휴 flush 뒤 지각 row 가 와도 같은 wafer 를 재발행하지 않는다.

    재발행되면 sink 의 COALESCE UPSERT 가 **부분 트레이스로 계산된 예측**으로 정상값을
    덮어쓴다 (시뮬레이터 일시정지·Kafka 랙이 유휴 임계를 넘길 때 발생).
    """
    wid, chamber = "C64_dup_CH_1", "SIM_CH_1"
    consumer.wafer_buffer[wid] = [_raw_row(step=4, stab=0, seq=0)]
    consumer.chamber_meta[wid] = {"chamber_id": chamber, "is_qual": False}
    consumer.chamber_current[chamber] = wid
    fake = FakeProducer()
    try:
        consumer.flush_wafer(wid, None, None, fake, mark_flushed=True)   # 유휴 flush 모사
        assert len(fake.produced) == 1
        consumer.handle_message({"wafer_id": wid, "chamber_id": chamber, "step": 4,
                                 "sensors": _raw_row()["sensors"],
                                 "timestamp": "2026-07-22T10:00:05"}, None, None, fake)
        assert wid not in consumer.wafer_buffer                     # 지각 row 는 버퍼에도 안 쌓임
        assert len(fake.produced) == 1                              # 재발행 없음
    finally:
        consumer.flushed_wafers.pop(wid, None)
        consumer.chamber_current.pop(chamber, None)


def test_normal_flush_does_not_block_wafer_id_reuse():
    """★ 정상 교체 flush 는 기억하지 않는다 — 시뮬레이터가 결정적 wafer_id 를 쓰므로,
    정상 경로까지 기억하면 재기동·리플레이 후 트래픽이 통째로 차단된다."""
    wid, chamber = "C64_reuse_CH_1", "SIM_CH_2"
    consumer.wafer_buffer[wid] = [_raw_row(step=4, stab=0, seq=0)]
    consumer.chamber_meta[wid] = {"chamber_id": chamber, "is_qual": False}
    fake = FakeProducer()
    try:
        consumer.flush_wafer(wid, None, None, fake)         # 정상 교체 flush (mark_flushed=False)
        assert wid not in consumer.flushed_wafers
        consumer.handle_message({"wafer_id": wid, "chamber_id": chamber, "step": 4,
                                 "sensors": _raw_row()["sensors"],
                                 "timestamp": "2026-07-22T10:00:05"}, None, None, fake)
        assert wid in consumer.wafer_buffer                 # 재기동 트래픽은 정상 수용
    finally:
        consumer.wafer_buffer.pop(wid, None)
        consumer.chamber_meta.pop(wid, None)
        consumer.wafer_last_seen.pop(wid, None)
        consumer.chamber_current.pop(chamber, None)


def test_dummy_prediction_handles_all_nan_c17():
    """step4 는 있는데 C17 이 전부 NaN → base 가 NaN 이 되던 경로 (리뷰 §1-5)."""
    rows = [_raw_row(step=4, C17=float("nan")), _raw_row(step=4, C17=float("nan"))]
    pred = consumer.dummy_prediction(rows)
    assert pred["pred_c65"] == pred["pred_c65"]          # NaN 이 아니다
    json.dumps(pred, allow_nan=False)


# ── 6) AE 실패 독립 폴백 — flush 생존 + 더미 발행 (헌법 6-2) ────────────────
def test_flush_survives_ae_failure_and_publishes_dummy():
    class BoomAE:
        """score_wafer가 항상 예외 — 번들 오염·추론 실패 모사."""
        def score_wafer(self, _trace):
            raise RuntimeError("scorer 손상")

    wid = "C64_777_CH_1"
    consumer.wafer_buffer[wid] = [_raw_row(step=4, stab=0, seq=0),
                                  _raw_row(step=4, stab=1, seq=1)]
    consumer.chamber_meta[wid] = {"chamber_id": "SIM_CH_1", "is_qual": False}
    fake = FakeProducer()

    # predictor=None → lean85도 더미. AE는 예외 → 독립 폴백. 예외 전파 없어야 함.
    consumer.flush_wafer(wid, None, BoomAE(), fake)

    assert len(fake.produced) == 1                    # C65 발행은 계속 (파이프라인 생존)
    msg = json.loads(fake.produced[0]["value"].decode("utf-8"))
    assert "predicted_c65" in msg
    assert 0.1 <= msg["drift_score"] <= 0.4           # AE 더미 폴백값
    assert 0.01 <= msg["anomaly_score"] <= 0.15
    assert wid not in consumer.wafer_buffer            # 버퍼 정리됨


# ── 7) 패리티(핵심): 오프라인 infer score ↔ consumer 재구성 경로 ───────────
def _reconstruct_rows(wdf):
    """parquet wafer 행 → consumer 버퍼형 rows (perturb 없음 — 순수 재구성)."""
    keys = {"C64", "C7", "C42", "C46", "C10", "C24"}
    rows = []
    for _, pr in wdf.iterrows():
        sensors = {c: pr[c] for c in wdf.columns if c not in keys}
        rows.append({"sensors": sensors, "step": pr["C7"],
                     "stabilization_flag": int(pr["C42"]),
                     "seq_in_step": pr["C46"], "timestamp": pr["C10"]})
    return rows


def test_parity_offline_vs_consumer_path():
    pytest.importorskip("torch")
    if not BUNDLE_DIR.exists():
        pytest.skip(f"AE 번들 없음: {BUNDLE_DIR}")
    if not PARQUET.exists():
        pytest.skip(f"원 학습셋 없음: {PARQUET} (repo 미커밋 — A 로컬 자산)")
    from ae_pipeline.infer import AEModel

    df = pd.read_parquet(PARQUET)
    df = df[df["C42"].notna()]                         # 결측 C42는 양 경로 동일 처리 위해 사전 제거
    wafers = list(dict.fromkeys(df["C64"].astype(str)))[:20]   # N=20
    sub = df[df["C64"].astype(str).isin(set(wafers))].copy()
    sub["C64"] = sub["C64"].astype(str)

    # 오프라인 경로 (infer score CLI와 동일) — drift 상태 배제(순수 anomaly 대조)
    ae_off = AEModel.from_bundle(str(BUNDLE_DIR))
    off = {r["wafer"]: r["ae_score"]
           for r in ae_off.score_wafers(sub, chamber_col="C24", with_drift=False)}

    # consumer 경로 — wafer별 fdc.raw 재구성 → rows_to_trace_ae → score
    ae_con = AEModel.from_bundle(str(BUNDLE_DIR))
    con = {}
    for w in wafers:
        wdf = sub[sub["C64"] == w]
        chamber = str(wdf["C24"].iloc[0])
        trace = consumer.rows_to_trace_ae(w, {"chamber_id": chamber}, _reconstruct_rows(wdf))
        rec = ae_con.score_wafers(trace, chamber_col="C24", with_drift=False)
        con[w] = rec[0]["ae_score"] if rec else None

    # 완료 기준: 0.2 임계 판정 100% 동일 + 값 근접(재구성/반올림 수준)
    THR = 0.2
    same_verdict, checked, max_diff = 0, 0, 0.0
    for w in wafers:
        a, b = off.get(w), con.get(w)
        assert a is not None and b is not None, f"스코어 누락: {w}"
        checked += 1
        max_diff = max(max_diff, abs(a - b))
        if (a >= THR) == (b >= THR):
            same_verdict += 1
    assert checked == len(wafers)
    assert same_verdict == checked, f"판정 불일치 {checked - same_verdict}/{checked}"
    assert max_diff < 0.02, f"ae_score 최대 편차 과다: {max_diff:.4f}"


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    _p, _s = 0, 0
    for _fn in _fns:
        try:
            _fn()
            print(f"  ✓ {_fn.__name__}")
            _p += 1
        except pytest.skip.Exception as _e:            # 스킵(패리티 자산 부재 등)
            print(f"  ⊘ {_fn.__name__} SKIP: {_e}")
            _s += 1
    print(f"{_p} PASS / {_s} SKIP / {len(_fns)} total")
