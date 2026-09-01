# -*- coding: utf-8 -*-
"""CT² 자동 검증 게이트 + 입력 어댑터 테스트 (WP-1·WP-2 DoD / 헌법 3-3 ③).

DoD 대응:
  · WP-1  입력 어댑터   : `_ts`→C10 매핑 · C46 합성 · 파싱 실패 행 스킵 · 시간축 부재 시 중단
                          · manifest 기록용 적용 내역 · `--input-schema merged` 오지정 방어
  · WP-2  게이트 PASS   : 정상(조용 + 민감) 스코어러 → 전 항목 통과
  · WP-2  게이트 FAIL   : ★S2 개통 조건 ①의 "막을 수 있음" 실증 — 3종 재현 케이스
                          ⓐ 깡통 모델(전부 0점 = 조용하지만 아무것도 못 잡음) → C1 차단
                          ⓑ 소표본 VAL → D1 차단
                          ⓒ 앵커 뭉침/붕괴 → D2 차단
  · 임계 로드           : `ct.ct2_gate_*` override 적용

설계 의도: 게이트 로직을 **torch 없이** 검증한다. `check_quiet_and_thresholds` ·
`probe_detection` 은 스코어러에서 `ae_raw`/`recon_rmse` 만 요구하는 덕타이핑 경계이므로
스텁으로 정상·깡통 모델을 재현할 수 있다. 결정적(난수 없음) 이라 재현성 100%.

실행: python -m pytest tests/test_ct2_gate.py -q
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
pytest.importorskip("numpy")
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
AE_DIR = REPO_ROOT / "src" / "agent_a_mlops" / "Autoencoder"
sys.path.insert(0, str(AE_DIR))

from ae_pipeline import retrain as R                    # noqa: E402  (torch 무의존 — 지연 import)
from ae_pipeline import validate_bundle as V            # noqa: E402
from ae_pipeline.constants import CH_SETTLE_CONT        # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# WP-1 — 입력 스키마 어댑터
# ══════════════════════════════════════════════════════════════════════════

def _ct2_csv_frame(n_wafer=3, rows_per_step=2, bad_ts=0) -> pd.DataFrame:
    """조립 스크립트(Kafka 경로) 산출 형태 모사 — `_ts` 있고 C10·C46 없음."""
    recs = []
    t = pd.Timestamp("2026-07-30T00:00:00Z")
    for w in range(n_wafer):
        for step in (3, 4):
            for i in range(rows_per_step):
                t = t + pd.Timedelta(seconds=1)
                recs.append({"C64": f"W{w}", "C24": "SIM_CH_1", "C7": step,
                             "C42": 0 if step == 4 else 1, "C41": i, "C20": "LOT_1",
                             "_ts": t.isoformat(), "C11": 1.0 + i, "C33": 100.0})
    for _ in range(bad_ts):
        recs.append({"C64": "WX", "C24": "SIM_CH_1", "C7": 4, "C42": 0, "C41": 0,
                     "C20": "LOT_1", "_ts": "not-a-timestamp", "C11": 1.0, "C33": 100.0})
    return pd.DataFrame(recs)


def test_detect_input_schema():
    """C10·C46 동시 존재 = merged, 하나라도 없으면 ct2."""
    assert R.detect_input_schema(_ct2_csv_frame()) == "ct2"
    merged = _ct2_csv_frame().rename(columns={"_ts": "C10"}).assign(C46=0)
    assert R.detect_input_schema(merged) == "merged"


def test_adapter_maps_ts_and_synthesizes_c46():
    """`_ts`→C10(datetime, tz-naive) 매핑 + C46 합성 + 적용 내역 기록."""
    df, applied = R.adapt_ct2_trainset(_ct2_csv_frame(n_wafer=2, rows_per_step=3))

    assert "C10" in df.columns and "C46" in df.columns
    assert pd.api.types.is_datetime64_any_dtype(df["C10"])
    assert df["C10"].dt.tz is None, "merged 경로와 dtype 정합 — tz-naive 여야 함"

    # C46 = (C64, C7) 그룹 내 0-기반 순번
    for (w, s), g in df.groupby(["C64", "C7"]):
        assert sorted(g["C46"].tolist()) == list(range(len(g))), f"{w}/{s} 순번 불연속"

    assert "_ts->C10" in applied["mapped"]
    assert "C46" in applied["synthesized"]
    assert applied["schema"] == "ct2"
    # dedup 무력화 사실이 기록에 남아야 한다 (조용한 변환 금지)
    assert any("dedup" in n for n in applied["notes"])


def test_adapter_skips_unparsable_ts_and_records_count():
    """`_ts` 파싱 실패 행은 스킵 + 건수 기록 (헌법 6-2 준용)."""
    df, applied = R.adapt_ct2_trainset(_ct2_csv_frame(n_wafer=2, bad_ts=3))
    assert "WX" not in set(df["C64"]), "파싱 실패 행이 남아 있음"
    assert any("3행 스킵" in n for n in applied["notes"])


def test_adapter_raises_without_time_axis():
    """C10·_ts 둘 다 없으면 정렬 불가 → 조용히 진행하지 않고 중단."""
    df = _ct2_csv_frame().drop(columns=["_ts"])
    with pytest.raises(ValueError, match="시간축"):
        R.adapt_ct2_trainset(df)


def test_adapter_raises_when_all_rows_unparsable():
    """전 행 파싱 실패 → 빈 프레임으로 학습에 들어가지 않는다."""
    df = _ct2_csv_frame(n_wafer=0, bad_ts=4)
    with pytest.raises(ValueError, match="행 0"):
        R.adapt_ct2_trainset(df)


def test_prepare_input_merged_force_guard():
    """--input-schema merged 오지정 시 필수 컬럼 부재를 알려준다 (조용한 진행 금지)."""
    with pytest.raises(ValueError, match="merged"):
        R.prepare_input(_ct2_csv_frame(), input_schema="merged")


def test_holdout_carve_keeps_calib_fit_floor():
    """홀드아웃을 떼도 남는 적합 표본이 게이트 하한(D4) 이상이어야 한다.

    학습만 통과하고 게이트 D4가 매번 FAIL 하는 조합(적합 표본 20~99장)을 만들지 않는다.
    """
    g = V.load_gate_params()
    assert R.MIN_CALIB_FIT_WAFERS >= g["min_val_wafers"], \
        "retrain 의 적합 표본 하한이 게이트 D4 하한보다 낮다 (항상 FAIL 조합)"
    for n_val in (200, 260, 400):
        fit, hold = R._carve_gate_holdout([f"W{i}" for i in range(n_val)], R.GATE_HOLDOUT_FRAC)
        if hold:
            assert len(fit) >= R.MIN_CALIB_FIT_WAFERS, (n_val, len(fit))
            assert len(hold) >= R.MIN_GATE_HOLDOUT_WAFERS, (n_val, len(hold))


def test_holdout_carve_skips_when_too_small():
    """적합 표본 하한을 못 지키면 홀드아웃을 떼지 않는다 (calib 을 깎는 쪽이 더 위험)."""
    fit, hold = R._carve_gate_holdout([f"W{i}" for i in range(60)], R.GATE_HOLDOUT_FRAC)
    assert hold == [] and len(fit) == 60


def test_holdout_carve_disabled_by_zero_frac():
    fit, hold = R._carve_gate_holdout([f"W{i}" for i in range(400)], 0.0)
    assert hold == [] and len(fit) == 400


def test_prepare_input_merged_passthrough():
    """merged 입력은 손대지 않는다 (기존 재현 경로 무영향)."""
    merged = _ct2_csv_frame().rename(columns={"_ts": "C10"}).assign(C46=0)
    out, applied = R.prepare_input(merged, input_schema="auto")
    assert applied["schema"] == "merged" and not applied["mapped"]
    pd.testing.assert_frame_equal(out, merged)


# ══════════════════════════════════════════════════════════════════════════
# WP-2 — 게이트: 스텁 스코어러
# ══════════════════════════════════════════════════════════════════════════

class SubspaceScorer:
    """정상 스코어러 — AE의 동작 원리를 최소로 재현한 참조 구현 대역.

    실 AE가 "정상엔 조용, 이탈엔 민감"할 수 있는 이유는 정상 데이터가 **저차원 다양체**에
    놓여 있어 재구성이 되기 때문이다. 이 스텁은 그 구조만 남긴다: 정상 VAL의 주부분공간
    (rank k)을 기준으로 잡고, 잔차 r = 부분공간 직교성분으로 스코어를 만든다.
      · 정상 행 → r ≈ 관측노이즈(작음) → ae_raw 낮음
      · 단일채널 kσ 주입 → 주입분 대부분이 부분공간 밖 → r 급증 → ae_raw 높음
    단순 "평균으로부터의 거리"로 만들면 정상 산포와 주입분이 같은 축에 섞여 4σ 주입조차
    P98.5 문턱을 겨우 넘는다 — 그건 AE가 아니라 단순 이상치 탐지의 한계다.
    """

    def __init__(self, X_normal, w_vec, rank=3):
        X = np.asarray(X_normal, float)
        self.mu = X.mean(axis=0)
        _u, _s, vt = np.linalg.svd(X - self.mu, full_matrices=False)
        self.basis = vt[:rank]                        # 정상 다양체 (행 = 주방향)
        self.w_vec = np.asarray(w_vec, float)
        self.resid_sd = float(np.sqrt((self._resid(X) ** 2).mean())) + 1e-9

    def _resid(self, X):
        d = np.asarray(X, float) - self.mu
        return d - (d @ self.basis.T) @ self.basis    # 부분공간 직교성분

    def ae_raw(self, X):
        z = self._resid(X) / self.resid_sd
        return (self.w_vec * z ** 2).sum(axis=1)

    def recon_rmse(self, X):
        r = self._resid(X)
        return float(np.sqrt((self.w_vec * r ** 2).mean()))


class DeadScorer:
    """★깡통 모델 — 모든 입력에 동일 최저점. 조용하지만 아무것도 못 잡는다.

    B·D군(조용함)만 보는 게이트는 이 모델을 만점으로 통과시킨다. C군(프로브)의
    존재 이유를 실증하는 반례 케이스.
    """

    def ae_raw(self, X):
        return np.zeros(len(np.asarray(X, float)))

    def recon_rmse(self, X):
        return 0.01


class LoudScorer:
    """정상에서도 시끄러운 모델 — 기존 캘리 기준으로 전부 임계 초과."""

    def __init__(self, inner, factor=1e3):
        self.inner, self.factor = inner, float(factor)

    def ae_raw(self, X):
        return self.inner.ae_raw(X) * self.factor

    def recon_rmse(self, X):
        return self.inner.recon_rmse(X) * self.factor


class IdentityScaler:
    """번들 scaler 대역 — 컬럼 정렬만 하고 값은 그대로 (게이트 로직 격리 검증용)."""

    def __init__(self, cols):
        self.cols = list(cols)

    def transform(self, F):
        return np.asarray(pd.DataFrame(F).reindex(columns=self.cols, fill_value=0.0), float)


def _val_features(n=200, seed=7, latent=3, noise=0.05):
    """정상 VAL 피처 모사 — 부록 A 정착 연속 채널의 `_s_mean` 컬럼.

    저차원 잠재구조(latent) + 소량 관측노이즈 = 실 FDC 정상 데이터의 상관 구조.
    """
    rng = np.random.default_rng(seed)
    cols = [f"{ch}_s_mean" for ch in CH_SETTLE_CONT]
    A = rng.normal(size=(len(cols), latent))
    Z = rng.normal(size=(n, latent))
    X = Z @ A.T + rng.normal(scale=noise, size=(n, len(cols)))
    X = X + np.arange(10.0, 10.0 + len(cols))          # 채널별 오프셋 (물리량 대역)
    return pd.DataFrame(X, columns=cols)


def _gate(**over):
    g = dict(V.DEFAULTS)
    g.update(over)
    return g


def _calib_from(scorer, X, **over):
    """실 파이프라인과 동일하게 정상 VAL raw 분포에서 캘리를 산출 (ECDF 앵커).

    임계를 손으로 박으면 스텁 스케일에 맞춘 자기충족 테스트가 된다 — `fit_calibration`
    을 그대로 써서 "0.2 = VAL P98.5" 계약 위에서 게이트를 검증한다.
    """
    from ae_pipeline.calibration import fit_calibration
    c = fit_calibration(scorer.ae_raw(X))
    c.update(over)
    return c


def _healthy_setup(n=200):
    """(feats, cols, scaler, X, scorer, calib) — 정상 번들 대역 일습."""
    from ae_pipeline.constants import feat_weight_vector
    feats = _val_features(n)
    cols = list(feats.columns)
    scaler = IdentityScaler(cols)
    X = scaler.transform(feats)
    scorer = SubspaceScorer(X, feat_weight_vector(cols))
    return feats, cols, scaler, X, scorer, _calib_from(scorer, X)


# ── PASS 경로 ──────────────────────────────────────────────────────────────

def test_gate_passes_for_healthy_bundle():
    """정상 스코어러 + 충분한 VAL + 건전한 앵커 → B·C·D 전 항목 PASS."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    gate = _gate()

    qd, metrics = V.check_quiet_and_thresholds(scorer, X, calib, gate, len(feats))
    assert all(c["pass"] for c in qd), [c for c in qd if not c["pass"]]
    assert metrics["l5"] <= gate["l5_budget"]
    # "0.2 = VAL P98.5" 계약 → L5 는 1.5% 근방에 앉는다 (REPORT_05 실측 1.53%와 동일 구조)
    assert metrics["l5"] == pytest.approx(0.015, abs=0.01)
    # 3밴드(2026-08-09 개정 v2 §1-3) — 건수·CI·라벨이 metrics·check 양쪽에 실린다
    assert metrics["l5_k"] == round(metrics["l5"] * metrics["n_val"])
    b1 = next(c for c in qd if c["check"] == "B1_L5_오경보율")
    assert b1["verdict_band"] in (V.BAND_PASS_CERTAIN, V.BAND_PASS_BOUNDARY)
    assert b1["band"]["ci_lo"] <= metrics["l5"] <= b1["band"]["ci_hi"]

    pr, detail = V.probe_detection(scorer, scaler, feats, cols, calib, gate)
    assert all(c["pass"] for c in pr), [c for c in pr if not c["pass"]]
    # 판정은 가중값 — 산술값은 병기된다 (§3-2)
    assert detail["scoring"] == "weighted"
    assert detail["at_gate_weighted"] >= gate["probe_min_detect"]
    assert detail["by_sigma"]["4sigma"] == detail["at_gate_arithmetic"]


def test_probe_detection_is_monotone_in_sigma():
    """교란이 커지면 검출률이 떨어지지 않는다 (프로브 채점기 자체의 건전성)."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    _, detail = V.probe_detection(scorer, scaler, feats, cols, calib, _gate())
    rates = [detail["by_sigma"][f"{k:g}sigma"] for k in V.DEFAULTS["probe_sigma_grid"]]
    assert rates == sorted(rates), f"단조 아님: {rates}"


def test_probe_is_deterministic():
    """프로브 채점에 난수가 없다 — 같은 입력이면 같은 결과 (S2 조건 ③의 하한 보장)."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    a = V.probe_detection(scorer, scaler, feats, cols, calib, _gate())[1]["by_sigma"]
    b = V.probe_detection(scorer, scaler, feats, cols, calib, _gate())[1]["by_sigma"]
    assert a == b


def test_probe_does_not_mutate_input_features():
    """프로브 주입이 원본 VAL 피처를 오염시키지 않는다 (불가침 2 — 격리)."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    before = feats.copy(deep=True)
    V.probe_detection(scorer, scaler, feats, cols, calib, _gate())
    pd.testing.assert_frame_equal(feats, before)


# ── ★FAIL 재현 (S2 개통 조건 ①) ─────────────────────────────────────────────

def test_gate_blocks_dead_model():
    """ⓐ 깡통 모델 — 조용함(B)은 통과하지만 프로브 검출률 0 → C1 차단.

    ★게이트의 존재 이유를 실증하는 케이스: B·D군만으로는 막히지 않는다.
    """
    feats, cols, scaler, X, healthy, calib = _healthy_setup()
    dead = DeadScorer()
    gate = _gate()

    qd, _ = V.check_quiet_and_thresholds(dead, X, calib, gate, len(feats))
    quiet = {c["check"]: c["pass"] for c in qd}
    assert quiet["B1_L5_오경보율"] and quiet["B2_recon_RMSE"] and quiet["B4_calib②_VAL_P95"], \
        "깡통 모델은 조용함 항목을 통과한다 — 그래서 C군(프로브)이 필요하다"

    pr, detail = V.probe_detection(dead, scaler, feats, cols, calib, gate)
    failed = [c["check"] for c in pr if not c["pass"]]
    assert "C1_프로브_검출률" in failed, "깡통 모델이 게이트를 통과했다 (문지기 실패)"
    assert detail["by_sigma"]["6sigma"] == 0.0


def test_gate_blocks_small_val_sample():
    """ⓑ 소표본 VAL → D1 차단 (min_val_wafers 미달)."""
    feats, _cols, _scaler, X, scorer, calib = _healthy_setup(n=30)
    qd, _ = V.check_quiet_and_thresholds(scorer, X, calib, _gate(), len(feats))
    failed = [c["check"] for c in qd if not c["pass"]]
    assert "D1_B군_표본_하한" in failed


def test_gate_blocks_collapsed_anchors():
    """ⓒ 앵커 뭉침 → D2 차단 (소표본 붕괴 신호)."""
    feats, _cols, _scaler, X, scorer, calib = _healthy_setup()
    clumped = {**calib, "anchors_x": [1.0, 1.0001, 1.0002, 1.0003, 60.0]}
    qd, _ = V.check_quiet_and_thresholds(scorer, X, clumped, _gate(), len(feats))
    failed = [c["check"] for c in qd if not c["pass"]]
    assert "D2_앵커_간격_비율" in failed


def test_gate_blocks_zero_span_anchors():
    """앵커 전 구간 동일(span=0) → 분모 0을 예외 대신 FAIL로 처리."""
    feats, _cols, _scaler, X, scorer, calib = _healthy_setup()
    flat = {**calib, "anchors_x": [2.0] * 5}
    qd, _ = V.check_quiet_and_thresholds(scorer, X, flat, _gate(), len(feats))
    assert "D2_앵커_간격_비율" in [c["check"] for c in qd if not c["pass"]]


def test_gate_blocks_malformed_calib_without_crashing():
    """앵커 구조 위반(길이 불일치·1개) → 예외가 아니라 critical FAIL + 스코어링 미시도.

    `apply_calibration` 이 던지는 예외를 그대로 흘리면 게이트가 판정(exit 2) 대신
    실행 오류(exit 1)로 끝나 리포트에 사유가 남지 않는다.
    """
    feats, _cols, _scaler, X, scorer, calib = _healthy_setup()
    for bad in ([1.0], [1.0, 2.0, 3.0]):                  # anchors_y 는 5개 유지 → 길이 불일치
        qd, metrics = V.check_quiet_and_thresholds(scorer, X, {**calib, "anchors_x": bad},
                                                   _gate(), len(feats))
        d2 = next(c for c in qd if c["check"] == "D2_앵커_간격_비율")
        assert not d2["pass"] and d2["critical"], bad
        assert metrics["scored"] is False, "깨진 캘리로 스코어링을 시도했다"
        assert not any(c["check"].startswith("B") for c in qd), "B군은 산출되지 않아야 한다"


def test_calib_contract_accepts_healthy_anchors():
    """정상 캘리는 구조 검사를 통과하고 스코어링을 허용한다."""
    _f, _c2, _s, X, scorer, calib = _healthy_setup()
    checks, scorable = V.check_calib_contract(calib, _gate())
    assert scorable and all(c["pass"] for c in checks)


def test_gate_blocks_noisy_model():
    """ⓓ 정상에서 시끄러운 모델 → B1(L5)·B4(P95) 차단 (오경보 폭주 방어)."""
    feats, _cols, _scaler, X, healthy, calib = _healthy_setup()
    qd, _ = V.check_quiet_and_thresholds(LoudScorer(healthy), X, calib, _gate(), len(feats))
    failed = [c["check"] for c in qd if not c["pass"]]
    assert "B1_L5_오경보율" in failed and "B4_calib②_VAL_P95" in failed


def test_probe_fails_when_no_target_channels():
    """feature_spec 이 부록 A와 어긋나 주입 대상이 0이면 critical FAIL."""
    feats = pd.DataFrame({"junk_a": [1.0, 2.0], "junk_b": [3.0, 4.0]})
    cols = list(feats.columns)
    pr, detail = V.probe_detection(DeadScorer(), IdentityScaler(cols), feats, cols,
                                   _calib_from(DeadScorer(), np.zeros((2, 2))), _gate())
    assert not pr[0]["pass"] and pr[0]["critical"] and detail["targets"] == []


# ══════════════════════════════════════════════════════════════════════════
# 임계 로드 (6-1)
# ══════════════════════════════════════════════════════════════════════════

def test_load_gate_params_reads_repo_params():
    """리포 params.yaml 의 `ct.ct2_gate_*` 가 DEFAULTS 를 덮어쓴다.

    값은 **2026-08-09 기준 개정 v2** (`docs/CT2_게이트_기준_개정_v2_2026-08-09.md`):
      · l5_budget 0.02 → **0.04** (구 `l5_max` 개명) — v1 의 완화를 유지하되 성격을 정정했다.
        "측정 오차 반영"이 아니라 **오경보 예산 상향**이다: e4 23/600=3.83% 의 Wilson 단측95%
        하한이 2.74% > 2% 라 참 오경보율이 원 목표 2% 를 유의하게 초과함이 확정(z=3.06).
        ★DEFAULTS 는 0.02 로 남는다 — 이 완화가 매 판정 `_weakened` 에 잡혀 승인 브리프에
        노출되도록 (문서 §1-4 가드: 미탐 1건 시 즉시 0.02 복귀).
      · probe_min_detect 0.875 → **0.90 복원** — v1 완화 철회. 가중 채점(w_c) 도입으로
        판정값이 0.8983 → 0.9019 가 되어 원값으로도 PASS 한다 (문서 §3).
    ⚠️ 값 자체를 못박는 테스트다 — params 를 다시 컷오버하면 **여기도 같은 PR 에서** 갱신할 것
    (7장 규칙: config→테스트 방향 표류는 런타임이 안 깨지고 CI 만 red 로 남는다).
    """
    p = V.load_gate_params()
    assert set(V.DEFAULTS) <= set(p), "DEFAULTS 키가 유실되면 게이트 항목이 조용히 빠진다"
    assert p["l5_budget"] == pytest.approx(0.04)
    assert p["l5_max"] == p["l5_budget"], "구 키 미러가 끊기면 옛 소비자가 KeyError 로 죽는다"
    assert p["probe_min_detect"] == pytest.approx(0.90)
    assert p["probe_weighted"] is True
    # 예산 완화는 **가드 걸린 완화**라 승인 브리프에 계속 노출돼야 한다 (문서 §1-4)
    assert "l5_budget" in p["_weakened"]
    assert "probe_min_detect" not in p["_weakened"], "0.90 복원 후에도 약화로 잡히면 감시가 틀렸다"


def test_load_gate_params_override(tmp_path):
    """override 적용 + 미기재 키는 DEFAULTS 유지."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_l5_budget: 0.05\n  ct2_gate_probe_sigma: 2.0\n",
                 encoding="utf-8")
    p = V.load_gate_params(y)
    assert p["l5_budget"] == 0.05 and p["probe_sigma"] == 2.0
    assert p["min_val_wafers"] == V.DEFAULTS["min_val_wafers"]


def test_load_gate_params_inherits_legacy_l5_max(tmp_path, caplog):
    """★구 키 `ct2_gate_l5_max` 만 있는 params.yaml 은 값을 승계한다 (2026-08-09 개명).

    승계가 없으면 구 파일(`--params` 로 넘기는 라운드별 override 포함)이 조용히
    DEFAULTS 0.02 로 되돌아가 **판정이 바뀐 줄 모른 채** e4 가 FAIL 이 된다 —
    헌법 7장 "폐기하면서 그걸 읽는 코드를 안 찾음"의 반대 방향(값이 사라져도 예외가 없다).
    """
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_l5_max: 0.04\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = V.load_gate_params(y)
    assert p["l5_budget"] == pytest.approx(0.04) and p["l5_max"] == pytest.approx(0.04)
    assert any("구 키" in r.message for r in caplog.records)
    # 신 키가 함께 있으면 신 키가 이긴다 (승계는 부재 시에만)
    y.write_text("ct:\n  ct2_gate_l5_max: 0.09\n  ct2_gate_l5_budget: 0.03\n", encoding="utf-8")
    assert V.load_gate_params(y)["l5_budget"] == pytest.approx(0.03)


def test_load_gate_params_survives_missing_file(tmp_path):
    """params 로드 실패 시 게이트가 죽지 않고 기본값으로 계속 (경고만)."""
    p = V.load_gate_params(tmp_path / "nope.yaml")
    # `_weakened`(완화 감지 목록, 2026-08-05 G0-3)는 DEFAULTS 에 없는 **산출 필드**이고,
    # `l5_max` 는 구 키 읽기 호환 미러(2026-08-09)라 둘 다 DEFAULTS 밖이다.
    assert {k: v for k, v in p.items()
            if not k.startswith("_") and k != "l5_max"} == V.DEFAULTS
    assert p["l5_max"] == p["l5_budget"]
    assert p["_weakened"] == [], "기본값에서 완화 감지가 뜨면 감시 로직이 잘못된 것"


def test_gate_params_present_in_repo_config():
    """params.yaml 에 ct2 게이트 키·스위치가 실제로 등재돼 있는지 (문서-코드 동기)."""
    import yaml
    ct = yaml.safe_load((REPO_ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))["ct"]
    for k in ("ct2_auto_promote", "ct2_retrain_cmd", "ct2_validate_cmd", "ct2_cmd_cwd"):
        assert k in ct, f"params.yaml ct.{k} 미등재"
    assert ct["ct2_auto_promote"] is False, "자동 배포 스위치는 개통 조건 충족 전 false 여야 한다"
    assert ct["ct2_retrain_cmd"], "retrain 인터페이스 미등재면 오케스트레이터가 IFACE_WAIT"
    for k in V.DEFAULTS:
        assert f"ct2_gate_{k}" in ct, f"params.yaml ct.ct2_gate_{k} 미등재"


# ══════════════════════════════════════════════════════════════════════════
# 리포트 계약 (오케스트레이터·Brief 소비)
# ══════════════════════════════════════════════════════════════════════════

def test_check_record_shape():
    """검사 레코드 스키마 — 리포트 JSON 소비자(Brief·ct_decisions) 계약."""
    c = V._c("X1_테스트", False, "detail", critical=True)
    assert set(c) == {"check", "pass", "detail", "critical", "skip"}
    assert c["skip"] is False, "기본은 판정함 — SKIP 은 명시적으로만"
    assert c["pass"] is False and c["critical"] is True
    json.dumps(c, ensure_ascii=False)          # 직렬화 가능해야 한다


def test_integrity_reports_missing_bundle_files(tmp_path):
    """번들 7파일 누락 → A1 critical FAIL 후 조기 반환 (해시 검사 무의미)."""
    checks = V.check_bundle_files(tmp_path)
    assert len(checks) == 1 and checks[0]["check"] == "A1_번들_7파일_완결"
    assert not checks[0]["pass"] and checks[0]["critical"]


def test_integrity_runs_before_manifest_read(tmp_path):
    """파일 검사가 manifest 판독보다 먼저 — 그래야 FAIL(2)이 실행오류(1)로 강등되지 않는다.

    manifest 없는 폴더에 `check_bundle_files` 를 걸어도 예외가 아니라 FAIL 레코드가 나온다.
    """
    for name in ("model_seed42.pt", "scaler.pkl", "scorer.npz", "calib.json",
                 "drift.json", "feature_spec.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")   # manifest.json 만 누락
    checks = V.check_bundle_files(tmp_path)
    assert checks[0]["check"] == "A1_번들_7파일_완결" and not checks[0]["pass"]
    assert "manifest.json" in checks[0]["detail"]


def test_sha256_item_count_is_checked(tmp_path):
    """manifest 에 sha256 키가 없으면 '불일치 0건'으로 통과하던 구멍을 막는다."""
    import json as _json
    for name in ("model_seed42.pt", "scaler.pkl", "scorer.npz", "calib.json",
                 "drift.json", "feature_spec.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(_json.dumps({"ae_model_version": "v"}),
                                            encoding="utf-8")
    checks = V.check_bundle_files(tmp_path)
    a2 = next(c for c in checks if c["check"] == "A2_manifest_sha256")
    assert not a2["pass"] and a2["critical"] and "부족" in a2["detail"]


def test_holdout_absence_is_surfaced(tmp_path):
    """홀드아웃이 없으면 D3 가 FAIL — B군의 항진명제화를 조용히 넘기지 않는다."""
    import json as _json
    (tmp_path / "manifest.json").write_text(
        _json.dumps({"set_hashes": {"val": None, "gate": None}}), encoding="utf-8")
    checks = V.check_set_hashes(tmp_path, ["W0", "W1"], [], _gate())
    d3 = next(c for c in checks if c["check"] == "D3_게이트_홀드아웃")
    assert not d3["pass"] and "항진명제" in d3["detail"] and "배포 불가" in d3["detail"]


def test_holdout_presence_passes(tmp_path):
    """홀드아웃이 있으면 D3 PASS + 게이트 세트 해시도 대조된다."""
    import json as _json
    from ae_pipeline.constants import sha1_8
    val = [f"W{i}" for i in range(120)]
    hold = [f"H{i}" for i in range(110)]
    (tmp_path / "manifest.json").write_text(
        _json.dumps({"set_hashes": {"val": sha1_8(val), "gate": sha1_8(hold)}}),
        encoding="utf-8")
    checks = V.check_set_hashes(tmp_path, val, hold, _gate())
    assert all(c["pass"] for c in checks), [c for c in checks if not c["pass"]]
    assert {"A3_VAL_세트_해시", "A5_게이트_세트_해시",
            "D3_게이트_홀드아웃", "D4_적합_표본_하한"} == {c["check"] for c in checks}


def test_small_calib_fit_sample_is_flagged(tmp_path):
    """적합 표본이 작으면 D4 FAIL — 앵커 분위수가 불안정해진다."""
    import json as _json
    (tmp_path / "manifest.json").write_text(_json.dumps({"set_hashes": {}}), encoding="utf-8")
    checks = V.check_set_hashes(tmp_path, ["W0", "W1"], [f"H{i}" for i in range(110)], _gate())
    d4 = next(c for c in checks if c["check"] == "D4_적합_표본_하한")
    assert not d4["pass"]


def test_set_hash_mismatch_is_critical(tmp_path):
    """VAL 세트 해시 불일치 = 다른 표본으로 검증 중 → critical FAIL."""
    import json as _json
    (tmp_path / "manifest.json").write_text(
        _json.dumps({"set_hashes": {"val": "deadbeef"}}), encoding="utf-8")
    a3 = V.check_set_hashes(tmp_path, ["W0"], [], _gate())[0]
    assert not a3["pass"] and a3["critical"]


def test_b_group_sample_floor_depends_on_source():
    """D1 하한은 표본 출처에 따라 다르다 — 홀드아웃 min_gate_wafers / 적합 min_val_wafers."""
    feats, _c2, _s, X, scorer, calib = _healthy_setup(n=200)
    g = _gate(min_gate_wafers=150, min_val_wafers=50)

    hold_fail = V.check_quiet_and_thresholds(scorer, X, calib, g, 100, on_holdout=True)[0]
    fit_pass = V.check_quiet_and_thresholds(scorer, X, calib, g, 100, on_holdout=False)[0]
    d1_hold = next(c for c in hold_fail if c["check"] == "D1_B군_표본_하한")
    d1_fit = next(c for c in fit_pass if c["check"] == "D1_B군_표본_하한")
    assert not d1_hold["pass"] and "홀드아웃" in d1_hold["detail"]
    assert d1_fit["pass"] and "퇴화" in d1_fit["detail"]


def test_l5_threshold_is_measurable_at_default_floor():
    """★L5 상한(2%)이 홀드아웃 하한에서 **측정 가능**해야 한다.

    표본이 작으면 오경보 1건의 기여가 임계보다 커져 항목이 사실상 이진 판정이 된다
    (50장 → 1건 = 2%). 기본값 조합이 이 함정을 피하는지 산술로 고정한다 —
    min_train_wafers(1000) × val_frac(0.2) × gate_holdout_frac(0.5) = 100장,
    1건 기여 = 1% ≤ l5_budget. 세 값 중 하나만 바꾸면 이 테스트가 깨진다.
    """
    import yaml
    from ae_pipeline.retrain import GATE_HOLDOUT_FRAC, MIN_GATE_HOLDOUT_WAFERS
    ct = yaml.safe_load((REPO_ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))["ct"]
    g = V.load_gate_params()
    n_hold = int(ct["ct2_min_train_wafers"] * 0.2 * GATE_HOLDOUT_FRAC)
    assert n_hold >= g["min_gate_wafers"], (
        f"표본 설계 불일치 — 예상 홀드아웃 {n_hold}장 < 하한 {g['min_gate_wafers']}장. "
        "min_train_wafers·val_frac·gate_holdout_frac 를 함께 조정해야 한다")
    # ★carve 결정 상수도 같은 그물에 넣는다 — 위 3값은 **기본 경로**만 덮는다.
    #   `--gate-holdout-frac` 을 내리면(WP-D 스윕 항목) 3값 검사는 통과하는데 carve 가
    #   게이트 하한 미만의 홀드아웃을 만들어, 학습을 다 태운 뒤 D1 에서 떨어진다.
    assert MIN_GATE_HOLDOUT_WAFERS >= g["min_gate_wafers"], (
        f"carve 하한({MIN_GATE_HOLDOUT_WAFERS})이 게이트 하한({g['min_gate_wafers']})보다 "
        "느슨하다 — 게이트가 항상 FAIL 하는 홀드아웃이 만들어진다")
    assert 1.0 / g["min_gate_wafers"] <= g["l5_budget"], (
        f"L5 예산 {g['l5_budget']} 이 표본 해상도 {1.0 / g['min_gate_wafers']:.4f} 보다 촘촘해 "
        "오경보 1건이 곧 FAIL 이 된다 (임계 측정 불가)")


def test_probe_is_skipped_when_calib_broken():
    """★깨진 캘리로 probe_detection 을 호출하지 않는다.

    probe 도 `apply_calibration` 을 쓰므로, D2 가 잡은 계약 위반을 그대로 흘리면
    np.interp 예외가 새어 판정(exit 2)이 실행 오류(exit 1)로 강등된다 — 리포트 미기록.
    `validate()` 가 `metrics["scored"]` 로 막는지 계약 수준에서 고정한다.
    """
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    broken = {**calib, "anchors_x": [1.0]}                 # anchors_y 5개 → 길이 불일치
    _qd, metrics = V.check_quiet_and_thresholds(scorer, X, broken, _gate(), len(X))
    assert metrics["scored"] is False
    with pytest.raises(ValueError):                        # 막지 않으면 이렇게 터진다
        V.probe_detection(scorer, scaler, feats, cols, broken, _gate())


def test_gate_params_coerce_quoted_numbers(tmp_path):
    """YAML 에 인용부호가 붙어 문자열이 된 임계도 수치로 강제 변환한다.

    미변환 시 `f"{x:g}"` 포매팅이 ValueError 로 터져 판정이 실행 오류가 된다.
    """
    y = tmp_path / "params.yaml"
    y.write_text('ct:\n  ct2_gate_probe_sigma: "4.0"\n'
                 '  ct2_gate_probe_sigma_grid: ["1.0", "2.0"]\n', encoding="utf-8")
    p = V.load_gate_params(y)
    assert isinstance(p["probe_sigma"], float) and p["probe_sigma"] == 4.0
    assert p["probe_sigma_grid"] == [1.0, 2.0]
    assert f"{p['probe_sigma']:g}" == "4"                  # 포매팅이 터지지 않는다


def test_gate_params_bad_value_falls_back(tmp_path):
    """수치로 못 읽는 값은 기본값으로 되돌린다 (게이트가 죽지 않는다)."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_l5_budget: abc\n", encoding="utf-8")
    p = V.load_gate_params(y)
    assert p["l5_budget"] == V.DEFAULTS["l5_budget"]


def test_gate_params_load_rejects_scalar_grid(tmp_path):
    """probe_sigma_grid 가 스칼라로 오기재되면 기본 그리드로 복구 (TypeError 방어)."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_probe_sigma_grid: 4.0\n", encoding="utf-8")
    p = V.load_gate_params(y)
    assert p["probe_sigma_grid"] == list(V.DEFAULTS["probe_sigma_grid"])


def test_gate_params_warn_on_weakening(tmp_path, caplog):
    """게이트를 사실상 끄는 override 는 경고로 드러난다 (§7 임계 무검증 사용 방지)."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_probe_min_detect: 0.0\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = V.load_gate_params(y)
    assert p["probe_min_detect"] == 0.0
    assert any("게이트 완화" in r.message for r in caplog.records)


# ── PM 리뷰 대응 — torch 지연 import · 리포트 원자 쓰기 ────────────────────────
def test_validate_defers_torch_import_to_scoring():
    """★`.model`(torch) import 는 **스코어링 블록 안**에 있어야 한다.

    함수 최상단에 두면 채점에 도달하지 않는 경로(A군 무결성 FAIL·VAL 0장 봉인)까지
    torch 를 요구해 **판정(exit 2)이 실행 오류(exit 1)로 강등**되고 리포트조차 남지
    않는다 (CI torch 부재에서 H2 2건이 실패했던 회귀). torch 설치 여부에 결과가
    좌우되지 않도록 **소스 구조**로 고정한다.
    """
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(V.validate)).splitlines()
    hits = [ln for ln in src if "from .model import" in ln]
    assert len(hits) == 1, f"`.model` import 가 {len(hits)}곳 — 단일 지점이어야 한다"
    assert hits[0].startswith("        "), (
        "validate() 의 `.model` import 가 함수 최상단에 있다 — 스코어링 블록 안으로 "
        "내려야 torch 없이도 무결성 판정이 리포트로 남는다")


def test_write_report_normalizes_nonfinite(tmp_path):
    """NaN/Inf → null · 표준 JSON (헌법 7장 — 비표준 리터럴 발행 금지)."""
    out = tmp_path / "sub" / V.VALIDATION_REPORT
    got = V.write_report({"gate_pass": True,
                          "metrics": {"recon_rmse": float("nan"), "val_p95": float("inf"),
                                      "nested": [float("-inf"), 1.0],
                                      "ok": True, "none": None}}, out)
    assert got == out and out.exists()
    assert not out.with_suffix(out.suffix + ".tmp").exists(), "tmp 파일 잔류"
    raw = out.read_text(encoding="utf-8")
    assert "NaN" not in raw and "Infinity" not in raw
    back = json.loads(raw)
    assert back["metrics"]["recon_rmse"] is None and back["metrics"]["val_p95"] is None
    assert back["metrics"]["nested"] == [None, 1.0] and back["metrics"]["ok"] is True


def test_write_report_failure_leaves_previous_intact(tmp_path, monkeypatch):
    """★교체 실패해도 기존 리포트가 파손되지 않는다 = 제자리 덮어쓰기가 아님의 증명.

    이 파일은 프로세스 간 공유물(`ct2_deploy_approval` 이 읽어 배포를 판정)이라
    부분 쓰기 노출이 PASS 리포트를 파손 판정으로 둔갑시킨다 (헌법 7장).
    """
    out = tmp_path / V.VALIDATION_REPORT
    V.write_report({"gate_pass": False, "failed_checks": ["A1_번들_7파일_완결"]}, out)
    monkeypatch.setattr(V.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("교체 실패")))
    with pytest.raises(OSError):
        V.write_report({"gate_pass": True, "failed_checks": []}, out)
    back = json.loads(out.read_text(encoding="utf-8"))
    assert back["gate_pass"] is False, "제자리 덮어쓰기였다면 기존 리포트가 파손된다"
    assert not out.with_suffix(out.suffix + ".tmp").exists(), "실패 후 tmp 잔류"


def test_write_report_retries_windows_lock(tmp_path, monkeypatch):
    """★Windows 잠금(PermissionError)은 짧은 backoff 로 재시도한다 (헌법 7장).

    읽는 쪽이 리포트를 여는 순간과 교체가 겹치면 Windows 는 WinError 5 를 낸다 —
    한 번에 포기하면 게이트가 판정을 내고도 근거 파일을 갱신하지 못한다.
    """
    out = tmp_path / V.VALIDATION_REPORT
    calls = {"n": 0}
    real = V.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:                      # 두 번 잠기고 세 번째 성공
            raise PermissionError(5, "다른 프로세스가 사용 중")
        return real(src, dst)

    monkeypatch.setattr(V.os, "replace", flaky)
    monkeypatch.setattr(V.time, "sleep", lambda s: None)     # 테스트 지연 제거
    V.write_report({"gate_pass": True}, out)
    assert calls["n"] == 3 and json.loads(out.read_text(encoding="utf-8"))["gate_pass"] is True


def test_write_report_gives_up_after_attempts(tmp_path, monkeypatch):
    """무한 재시도는 하지 않는다 — 상한 도달 시 예외 + tmp 청소."""
    out = tmp_path / V.VALIDATION_REPORT
    monkeypatch.setattr(V.os, "replace",
                        lambda *a: (_ for _ in ()).throw(PermissionError(5, "잠김")))
    monkeypatch.setattr(V.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        V.write_report({"gate_pass": True}, out)
    assert not out.with_suffix(out.suffix + ".tmp").exists(), "실패 후 tmp 잔류"


if __name__ == "__main__":                     # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# ══════════════════════════════════════════════════════════════════════════
# G0 — 게이트 봉인 (E0 라벨 제공 · E2 대칭 · verdict 3값 · 완화 감시) 2026-08-05
# ══════════════════════════════════════════════════════════════════════════

def test_gate_fails_without_labels():
    """★E0 — 정답지 미제공은 **FAIL** 이다 (구 동작: `pass=True` 로 조용히 통과).

    라벨 없이 오염을 잡는 신호는 현 게이트에 0개다: B군은 오염 시 앵커가 부풀려져 오경보를
    오히려 **낮게** 보고하고(은폐), C군 프로브는 학습 이후 합성 교란이라 원리적으로 못 잡는다
    (실증: 프로브 97.8% PASS / 실제 주입 검출 0.6%). 그래서 미제공 = 배포 불가.
    """
    checks = V.check_labels_provided(None, _gate())
    assert checks[0]["check"] == "E0_라벨_제공"
    assert checks[0]["pass"] is False and checks[0]["critical"] is True


def test_gate_label_exemption_is_recorded(tmp_path, caplog):
    """실운영 면제(`ct2_gate_require_labels: false`)는 통과시키되 **흔적을 남긴다**.

    면제 자체가 "오염 방어 0" 상태이므로(K-1), 승인 브리프가 그것을 볼 수 있어야 한다 —
    `gate_params._weakened` 에 실려 리포트로 흘러간다.
    """
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_require_labels: false\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = V.load_gate_params(y)
    assert p["require_labels"] is False
    assert "require_labels" in p["_weakened"]
    assert any("E0 면제" in r.message for r in caplog.records)
    assert V.check_labels_provided(None, p)[0]["pass"] is True


def test_labels_provided_passes_when_given():
    c = V.check_labels_provided("/x/injection_labels.csv", _gate())[0]
    assert c["pass"] is True and "injection_labels" in c["detail"]


def _write_manifest(d: Path, **selection):
    """최소 manifest — `selection` 절만 필요한 검사용."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"selection": selection}, ensure_ascii=False), encoding="utf-8")
    return d


def test_label_symmetry_flags_trained_clean_but_unlabeled_validation(tmp_path):
    """★K-5 — 학습은 라벨 제외인데 검증은 라벨 없음 = 오염 검사가 빠진 조합 → FAIL."""
    b = _write_manifest(tmp_path / "b", injection_excluded=True)
    c = V.check_label_symmetry(b, None)[0]
    assert c["check"] == "E2_라벨_대칭" and c["pass"] is False and c["critical"] is True


def test_label_symmetry_notes_untrained_labels(tmp_path):
    """학습이 라벨 없이 돌았으면 FAIL 은 아니되(E1 이 판정) 사실을 기록한다."""
    b = _write_manifest(tmp_path / "b", injection_excluded=False)
    c = V.check_label_symmetry(b, "/x/labels.csv")[0]
    assert c["pass"] is True and "injection_excluded=false" in c["detail"]


def test_label_symmetry_skips_when_manifest_unreadable(tmp_path):
    """manifest 를 못 읽으면 A1·A2 가 이미 FAIL 이다 — 여기서 중복 차단하지 않는다."""
    c = V.check_label_symmetry(tmp_path / "none", "/x/labels.csv")[0]
    assert c["pass"] is True and "검사 생략" in c["detail"]


def test_fallback_rejects_label_trained_bundle(tmp_path):
    """★K-6 — 사이드카 유실 + 라벨 학습 번들이면 VAL 재유도를 **거부**한다.

    폴백(`select_normal_wafers`)은 `labels_path` 없이 도는 탓에 **오염 포함 목록**을 VAL 로
    되살린다. manifest 에 `set_hashes` 가 없으면 A3 도 생략돼 조용히 다른 세트로 검증된다 —
    explicit_wafers·chamber 분할과 같은 급의 '재현 불가'로 환원한다.
    """
    b = _write_manifest(tmp_path / "b", injection_excluded=True, val_frac=0.2)
    with pytest.raises(RuntimeError, match="injection_excluded"):
        V.resolve_val_wafers(b, pd.DataFrame({"C64": ["W1"], "C10": [0]}), None)


def test_decide_verdict_priority():
    """FAIL > SKIP > PASS. **SKIP 은 PASS 가 아니다** (K-2)."""
    ok = V._c("A", True, "")
    skip = V._c("B", True, "", skip=True)
    bad = V._c("C", False, "")
    assert V.decide_verdict([ok])[0] == V.VERDICT_PASS
    assert V.decide_verdict([ok, skip])[0] == V.VERDICT_SKIP
    assert V.decide_verdict([ok, skip, bad])[0] == V.VERDICT_FAIL
    v, failed, skipped = V.decide_verdict([ok, skip])
    assert failed == [] and skipped == ["B"]


def test_report_carries_verdict_and_schema_version(tmp_path):
    """리포트가 `gate_verdict`·`gate_schema_version`·`checks_expected` 를 싣는다.

    ★WP-D 부수 발견: `gate_source` 가 상수 문자열이라 **항목 집합이 다른 두 리포트**
    (`ae_cand_clean_time` 은 E1 부재 / `ae_cand_clean_chamber` 는 존재)를 리포트만으로
    구분할 수 없었다. 스키마 버전과 기대 항목 목록이 그 구분을 만든다.
    """
    rep = V.validate(tmp_path / "nobundle", tmp_path / "nodata.csv")
    assert rep["gate_verdict"] == V.VERDICT_FAIL          # 번들 부재 = A1 critical
    assert rep["gate_pass"] is False
    assert rep["gate_schema_version"] == V.GATE_SCHEMA_VERSION
    assert "E0_라벨_제공" in rep["failed_checks"], "라벨 미제공이 항목으로 드러나야 한다"
    assert "A1_번들_7파일_완결" in rep["checks_expected"]
    assert rep["labels_path"] is None and "val_device" in rep


# ── G0-3 완화 감시 (방향 + E군 3키) ────────────────────────────────────────

def test_weaken_watch_direction_upper(tmp_path, caplog):
    """★상한류(`l5_budget`)는 **키우는 쪽**이 완화다 — 구 구현은 `≤0`(강화)만 잡았다."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_l5_budget: 0.5\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = V.load_gate_params(y)
    assert "l5_budget" in p["_weakened"]
    assert "l5_max" not in p["_weakened"], (
        "구 키가 감시에 남아 있으면 같은 완화가 2건으로 세어져 브리프의 '약화 N건'이 틀린다")
    assert any("게이트 완화 감지" in r.message for r in caplog.records)


def test_weaken_watch_covers_label_keys(tmp_path, caplog):
    """E군 3키도 감시한다 — `label_min_wafers: 100000` 한 줄로 오염 검사가 영구 SKIP 된다."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_label_min_wafers: 100000\n"
                 "  ct2_gate_label_min_detect: 0.1\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = V.load_gate_params(y)
    assert {"label_min_wafers", "label_min_detect"} <= set(p["_weakened"])


def test_weaken_watch_silent_on_strengthening(tmp_path, caplog):
    """강화(임계를 빡빡하게)는 경고 대상이 아니다 — 항목 추가·강화는 A 권한 (3-3③ⓑ)."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_gate_l5_budget: 0.001\n  ct2_gate_probe_min_detect: 0.99\n",
                 encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = V.load_gate_params(y)
    assert p["_weakened"] == []


# ── WP-E 중간 완화안 — B5 챔버별 L5 ────────────────────────────────────────

def test_chamber_l5_blocks_local_collapse():
    """★총계 PASS + 한 챔버 붕괴 → FAIL. 평균이 국소 붕괴를 가리는 것을 막는다.

    실측 근거: 총계 L5 8.00% 가 챔버별로는 CH4 1.05% ~ CH2 35.0% (33배). 평균만 보면
    "전반적으로 조금 높다"로 읽혀 전역 임계 조정이라는 처방으로 가는데, 그 방향으로는
    해결되지 않는다(한쪽에 맞추면 다른 쪽이 깨진다).
    """
    by = {"CH1": {"n": 120, "l5": 0.010, "mean": 0.05, "p95": 0.18},
          "CH2": {"n": 100, "l5": 0.350, "mean": 0.09, "p95": 0.59}}
    c = V.check_chamber_l5(by, _gate())
    assert c["check"] == "B5_챔버별_L5" and c["pass"] is False and "CH2" in c["detail"]


def test_chamber_l5_excludes_thin_chambers():
    """표본 하한 미만 챔버는 판정에서 제외 — n=5 에서 1건이면 20% 라 이진 판정이 된다."""
    by = {"CH1": {"n": 120, "l5": 0.010, "mean": 0.05, "p95": 0.18},
          "CH2": {"n": 5, "l5": 0.400, "mean": 0.3, "p95": 0.8}}
    assert V.check_chamber_l5(by, _gate())["pass"] is True


def test_chamber_l5_without_labels_is_not_silent():
    """챔버 라벨이 없으면 판정하지 않되 그 사실을 detail 에 남긴다 (조용한 통과 금지)."""
    c = V.check_chamber_l5({}, _gate())
    assert c["pass"] is True and "챔버 라벨 없음" in c["detail"]


# ── E1 상세 필드 (excluded_train ↔ excluded_warmup 분리) ───────────────────

def test_excluded_train_field_is_train_only():
    """★`excluded_train` 에 **warmup 제외 건수**가 들어가던 버그 (G0-1 동반 수정).

    구 구현은 `n_all - len(lab)` 을 넣었는데 `n_all` 이 이미 train 제외 후 값이라, 실제로는
    warmup 건수가 담겼다. 리포트만 보고 "학습에 주입이 얼마나 들어갔나"를 답할 수 있어야
    한다 — 오염 진단의 1차 지표다.
    """
    labels = pd.DataFrame({
        "C64": [f"W{i}" for i in range(10)],
        "injected": [True] * 10,
        # W0~W3 = 학습 유입 / W4 = warmup(k<20) — 두 제외 사유를 **겹치지 않게** 배치해야
        # 필드가 실제로 분리됐는지 드러난다 (겹치면 둘 다 통과하는 가짜 초록).
        "k": [1, 2, 3, 4, 5, 60, 70, 80, 90, 100],
    })
    df = pd.DataFrame({"C64": [f"W{i}" for i in range(10)]})
    train = {"W0", "W1", "W2", "W3"}                        # 학습에 4장 유입
    checks, detail = V.check_label_detection(None, None, df, [], {}, _gate(),
                                             labels, train)
    assert checks[0]["skip"] is True, "표본 하한 미달은 SKIP (PASS 아님)"
    assert detail["n_injected"] == 10
    assert detail["excluded_train"] == 4
    assert detail["excluded_warmup"] == 1                   # W4 (k=5)
    assert detail["judged"] is False


# ══════════════════════════════════════════════════════════════════════════
# 기준 개정 v2 (2026-08-09) — 3밴드 판정 · 가중 프로브 채점 · 승인 브리프
#   정본: docs/CT2_게이트_기준_개정_v2_2026-08-09.md
# ══════════════════════════════════════════════════════════════════════════

# 문서 §1-3 6후보 표 — (이름, k, n, 기대 밴드, 기대 pass). 예산 4% 기준.
# ★실측 재현 표다. 여기 숫자가 바뀌면 문서와 코드 중 하나가 표류한 것이다.
V2_B1_CASES = [
    ("r1", 49, 111, V.BAND_FAIL_CERTAIN, False),
    ("r2", 21, 600, V.BAND_PASS_BOUNDARY, True),
    ("r3", 26, 600, V.BAND_FAIL_BOUNDARY, False),
    ("e4", 23, 600, V.BAND_PASS_BOUNDARY, True),
    ("e5", 40, 600, V.BAND_FAIL_CERTAIN, False),
    ("e6", 19, 600, V.BAND_PASS_BOUNDARY, True),
]


@pytest.mark.parametrize("name,k,n,band,passed", V2_B1_CASES)
def test_v2_band_matches_documented_table(name, k, n, band, passed):
    """문서 §1-3 표의 6후보 밴드·판정을 그대로 재현한다."""
    b = V.band_for_rate(k, n, 0.04)
    assert b["band"] == band, f"{name}: {b}"
    assert b["pass"] is passed, f"{name}: {b}"


def test_v2_e4_ci_matches_documented_interval():
    """e4 23/600 의 Wilson 단측95% 구간 = [2.74%, 5.34%] (문서 §1-1 · z=1.6449)."""
    lo, hi = V.wilson_interval(23, 600)
    assert lo == pytest.approx(0.0274, abs=5e-5)
    assert hi == pytest.approx(0.0534, abs=5e-5)


def test_v2_chamber_ci_upper_matches_documented_table():
    """문서 §2 표 — 4챔버 전부 CI 상한이 임계 5% 를 초과한다(n=150 의 한계)."""
    for k, want_hi in ((6, 0.0755), (6, 0.0755), (7, 0.0839), (4, 0.0581)):
        b = V.band_for_rate(k, 150, 0.05)
        assert b["ci_hi"] == pytest.approx(want_hi, abs=5e-5)
        assert b["boundary"] is True, "소표본이라 '5% 이내'가 확정될 수 없다"


@pytest.mark.parametrize("k,n,budget", [(0, 100, 0.04), (100, 100, 0.04),
                                        (1, 1, 0.5), (23, 600, 0.0383333333)])
def test_v2_band_never_flips_the_point_verdict(k, n, budget):
    """★불변식 — 밴드는 판정을 바꾸지 않는다 (lo ≤ p̂ ≤ hi).

    이 성질이 "v1 대비 판정 무변경 = 완화 아님"의 근거다 (헌법 3-3 ③ ⓑ — 완화는 PM 승인
    사항인데, 밴드 도입이 통과/차단을 뒤집으면 그 자체가 승인 대상이 된다).
    """
    b = V.band_for_rate(k, n, budget)
    assert b["pass"] is (k / n <= budget)
    assert b["ci_lo"] <= k / n <= b["ci_hi"]
    # 확정 밴드는 점추정과 항상 일치한다
    if b["band"] == V.BAND_FAIL_CERTAIN:
        assert b["pass"] is False
    if b["band"] == V.BAND_PASS_CERTAIN:
        assert b["pass"] is True


def test_v2_wilson_survives_degenerate_sample():
    """n=0 은 예외가 아니라 '아무것도 배제 못 함'(0,1) — 게이트가 실행 오류로 강등되지 않는다."""
    assert V.wilson_interval(0, 0) == (0.0, 1.0)
    assert V.band_for_rate(0, 0, 0.04)["band"] == V.BAND_PASS_BOUNDARY


def test_v2_b1_check_carries_band_and_counts():
    """B1 검사 레코드에 `verdict_band` + 건수가 실린다 (브리프로 흐르는 필드)."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    qd, metrics = V.check_quiet_and_thresholds(scorer, X, calib, _gate(), len(feats))
    b1 = next(c for c in qd if c["check"] == "B1_L5_오경보율")
    assert "verdict_band" in b1 and b1["band"]["n"] == len(feats)
    assert b1["band"]["k"] == metrics["l5_k"]
    assert b1["band"]["budget"] == V.DEFAULTS["l5_budget"]


def test_v2_chamber_band_uses_recorded_k_not_rounded_rate():
    """★챔버 밴드는 기록된 `k` 를 쓴다 — 비율 되짚기는 반올림으로 1건씩 어긋난다.

    `chamber_breakdown` 이 l5 를 소수 4자리로 반올림하므로, `round(l5*n)` 로 되짚으면
    표본이 클수록 건수가 밀린다 (헌법 7장 "순번은 추정하지 말고 기록한다"와 같은 취지).
    """
    by = {"CH1": {"n": 600, "k": 23, "l5": 0.0383, "mean": 0.05, "p95": 0.18}}
    c = V.check_chamber_l5(by, _gate())
    assert c["bands_by_chamber"]["CH1"]["k"] == 23
    assert c["bands_by_chamber"]["CH1"]["ci_lo"] == pytest.approx(0.0274, abs=5e-5)
    # k 없는 구 리포트는 비율에서 되짚어 **동작은 한다**(하위호환) — 정확도만 떨어진다
    by_old = {"CH1": {"n": 600, "l5": 0.0383, "mean": 0.05, "p95": 0.18}}
    assert V.check_chamber_l5(by_old, _gate())["bands_by_chamber"]["CH1"]["k"] == 23


def test_v2_chamber_breakdown_records_k():
    """`chamber_breakdown` 이 오경보 **건수**를 함께 남긴다."""
    sc = np.array([0.1] * 90 + [0.5] * 10)
    ch = ["CH1"] * 100
    out = V.chamber_breakdown(sc, ch, qual=0.2)
    assert out["CH1"] == {"n": 100, "k": 10, "l5": 0.1, "mean": 0.14, "p95": 0.5}


def test_v2_b5_reports_worst_chamber_band():
    """B5 대표 밴드 = **가장 나쁜 챔버** (여러 밴드를 하나로 접을 때 관대한 쪽으로 가지 않는다)."""
    by = {"CH1": {"n": 600, "k": 6, "l5": 0.01, "mean": 0.05, "p95": 0.18},
          "CH2": {"n": 150, "k": 7, "l5": 0.0467, "mean": 0.06, "p95": 0.19}}
    c = V.check_chamber_l5(by, _gate())
    assert c["pass"] is True                                # 둘 다 점추정 ≤ 5%
    assert c["verdict_band"] == V.BAND_PASS_BOUNDARY        # CH2 가 경계 → 대표도 경계
    assert c["band"]["k"] == 7


# ── C1 가중 채점 (§3) ──────────────────────────────────────────────────────

def test_v2_probe_weighted_matches_manual_formula():
    """판정값 = Σ w_c·rate_c / Σ w_c — 부록 A 가중을 단일 소스로 쓴다 (§3-2)."""
    from ae_pipeline.constants import feat_weight
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    _, d = V.probe_detection(scorer, scaler, feats, cols, calib, _gate())
    rates = d["by_channel_at_gate"]
    w = {c: feat_weight(c) for c in rates}
    manual = sum(rates[c] * w[c] for c in rates) / sum(w.values())
    assert d["at_gate_weighted"] == pytest.approx(manual, abs=1e-4)
    assert d["weights_at_gate"] == w, "가중이 constants.feat_weight 밖에서 재정의되면 안 된다"


def test_v2_probe_weighted_reproduces_e4_measurement():
    """★실측 재현 — e4 채널별 검출률에서 산술 0.8983 / 가중 0.9019 (문서 §3-1 표).

    가중 채점의 존재 이유가 이 3.6%p 다: 원값 0.90 을 복원해도 PASS 한다 →
    v1 의 `probe_min_detect` 0.875 완화를 철회할 수 있다.
    """
    from ae_pipeline.constants import feat_weight
    e4 = {"C11_s_mean": 0.9992, "C17_s_mean": 0.6342, "C9_s_mean": 0.9983,
          "C52_s_mean": 0.4608, "C15_s_mean": 0.9958, "C16_s_mean": 0.9883,
          "C31_s_mean": 0.9992, "C63_s_mean": 0.9792, "C58_s_mean": 0.9708,
          "C57_s_mean": 0.9575}
    w = {c: feat_weight(c) for c in e4}
    arith = sum(e4.values()) / len(e4)
    weighted = sum(e4[c] * w[c] for c in e4) / sum(w.values())
    assert arith == pytest.approx(0.8983, abs=5e-5)
    assert weighted == pytest.approx(0.9019, abs=5e-5)
    assert arith < V.DEFAULTS["probe_min_detect"] <= weighted, (
        "이 부등식이 깨지면 0.90 복원의 근거가 사라진다 — 완화 철회를 재검토할 것")


def test_v2_probe_weighted_switch_falls_back_to_arithmetic():
    """`probe_weighted: false` 는 구 산술 거동 — 두 값은 어느 쪽이든 함께 실린다(감사)."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    _, d = V.probe_detection(scorer, scaler, feats, cols, calib,
                             _gate(probe_weighted=False))
    assert d["scoring"] == "arithmetic"
    assert d["at_gate_arithmetic"] == d["by_sigma"]["4sigma"]
    assert "at_gate_weighted" in d and "by_sigma_weighted" in d


def test_v2_probe_weight_normalizes_over_scored_channels_only():
    """★상수 채널로 빠진 몫을 분모에 남기지 않는다 — 없는 채널을 0점으로 세면 안 된다."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    feats = feats.copy()
    feats["C57_s_mean"] = 1.0                               # 표준편차 0 → 교란 정의 불가
    _, d = V.probe_detection(scorer, scaler, feats, cols, calib, _gate())
    assert "C57_s_mean" not in d["weights_at_gate"], "제외된 채널이 분모에 남았다"
    assert 0.0 < d["at_gate_weighted"] <= 1.0


# ── 승인 브리프 블록 (§7 — PM 코멘트 ②) ─────────────────────────────────────

def test_v2_approval_brief_translates_weakened_keys():
    """`_weakened` 키 목록을 사람 말로 옮긴다 — 기본값·현재값·사유까지.

    키 이름만으로는 승인자가 "무엇이 얼마나 느슨한지"를 알 수 없다. 번역을 하류
    (gateway·프론트)에 두면 임계를 바꿀 때 두 곳을 고쳐야 하고, **안 고쳐도 아무 일도
    일어나지 않는다** (헌법 7장 "신설하고 하류를 안 따라감").
    """
    gate = _gate(l5_budget=0.04, require_labels=False)
    gate["_weakened"] = ["l5_budget", "require_labels"]
    ab = V.build_approval_brief(gate, [], V.VERDICT_PASS, labels_path=None)
    assert ab["weakened_count"] == 2
    keys = {w["key"]: w for w in ab["weakened"]}
    assert keys["l5_budget"]["baseline"] == 0.02 and keys["l5_budget"]["value"] == 0.04
    assert keys["l5_budget"]["label"] and keys["l5_budget"]["why"]
    # §1-4 가드는 예산 완화에 **항상** 붙는다 (미탐 1건 → 0.02 복귀)
    assert "0.02" in keys["l5_budget"]["guard"]
    assert "미탐" in keys["l5_budget"]["guard"]
    assert "guard" not in keys["require_labels"]


def test_v2_approval_brief_every_watched_key_has_a_label():
    """★감시 대상 전 키에 한국어 라벨이 있다 — 새 키를 감시에 넣고 라벨을 빠뜨리면
    브리프에 키 이름이 그대로 노출되는데, 그건 **에러가 아니라 그냥 안 읽히는 문구**다."""
    watched = {k for k, _ in V.WEAKEN_WATCH} | {"require_labels"}
    assert watched <= set(V.WEAKENED_LABELS), (
        f"라벨 누락: {sorted(watched - set(V.WEAKENED_LABELS))}")


def test_v2_approval_brief_surfaces_boundary_labels():
    """경계 라벨(B1·B5)이 브리프로 흐른다 — 리포트 `verdict_band` → 브리프."""
    band = V.band_for_rate(23, 600, 0.04)
    checks = [V._c("B1_L5_오경보율", True, V.fmt_band(band), band=band),
              V._c("B2_recon_RMSE", True, "무관")]
    ab = V.build_approval_brief(_gate(), checks, V.VERDICT_PASS, labels_path="x.csv")
    assert ab["boundary_count"] == 1
    assert ab["boundaries"][0]["check"] == "B1_L5_오경보율"
    assert ab["boundaries"][0]["band"] == V.BAND_PASS_BOUNDARY
    assert any("경계 라벨" in r for r in ab["residual_risks"])


def test_v2_approval_brief_flags_missing_e1_as_residual_risk():
    """E1 미실행은 **잔여 리스크로 남는다** — 라벨이 없으면 오염을 검사한 항목이 0이다."""
    ab = V.build_approval_brief(_gate(), [], V.VERDICT_PASS, labels_path=None)
    assert any("E1" in r for r in ab["residual_risks"])
    ab2 = V.build_approval_brief(_gate(), [], V.VERDICT_PASS, labels_path="labels.csv")
    assert not any("미실행" in r for r in ab2["residual_risks"])


def test_v2_approval_brief_carries_skip_as_risk():
    """SKIP 항목은 통과가 아니라 잔여 리스크로 브리프에 뜬다 (K-2)."""
    checks = [V._c("E1_정답지_검출률", True, "표본 12 < 30 — 판정 불가", skip=True)]
    ab = V.build_approval_brief(_gate(), checks, V.VERDICT_SKIP, labels_path="l.csv")
    assert any("E1_정답지_검출률 SKIP" in r for r in ab["residual_risks"])


def test_v2_report_includes_approval_brief_and_is_json_safe():
    """리포트에 `approval_brief` 가 실리고 표준 JSON 으로 직렬화된다 (브리프 소비 경로)."""
    gate = _gate(l5_budget=0.04)
    gate["_weakened"] = ["l5_budget"]
    band = V.band_for_rate(23, 600, 0.04)
    checks = [V._c("B1_L5_오경보율", True, V.fmt_band(band), band=band)]
    ab = V.build_approval_brief(gate, checks, V.VERDICT_PASS, labels_path=None)
    s = json.dumps(V._json_safe(ab), ensure_ascii=False, allow_nan=False)
    assert "l5_budget" in s and V.BAND_PASS_BOUNDARY in s


# ── §6 e4 v2 재판정 — **기록된 실측 리포트**로 재현 확인 ──────────────────────
#   토치 없는 재현이다: e4 채점(validation_v2_schema2.json)은 이미 났고, v2 가 바꾸는 것은
#   그 수치를 **어떻게 판정·라벨링하는가** 뿐이다. 전체 재채점(모델 로드)은 사용자 환경에서
#   `python -m ae_pipeline.validate_bundle --bundle <e4> --data <ct2 csv> --params <v2 yaml>`.

E4_REPORT = (REPO_ROOT / "models" / "anomaly_ae" / "ae_cand_seg1pre_e4_20260806"
             / "validation_v2_schema2.json")


def _e4():
    if not E4_REPORT.exists():
        pytest.skip(f"e4 실측 리포트 없음: {E4_REPORT}")
    return json.loads(E4_REPORT.read_text(encoding="utf-8"))


def test_v2_e4_rescore_reproduces_documented_verdict():
    """★문서 §6 재판정 재현 — B1 PASS-경계 · B5 4챔버 PASS-경계 · C1 가중 0.9019 PASS.

    기록된 e4 리포트의 **원 수치**에 v2 판정 규칙을 그대로 먹여 문서 결론을 재현한다.
    여기가 깨지면 코드와 문서 중 하나가 표류한 것이다 (헌법 4-3).
    """
    rep = _e4()
    m = rep["metrics"]
    budget = 0.04                                        # params `ct2_gate_l5_budget`

    # B1 — 23/600 = 3.83%, CI[2.74, 5.34], PASS-경계
    n = rep["set_sizes"]["gate_holdout"]
    k = round(m["l5"] * n)
    b1 = V.band_for_rate(k, n, budget)
    assert (k, n) == (23, 600)
    assert b1["band"] == V.BAND_PASS_BOUNDARY and b1["pass"] is True
    assert (b1["ci_lo"], b1["ci_hi"]) == (pytest.approx(0.0274, abs=5e-5),
                                          pytest.approx(0.0534, abs=5e-5))

    # B5 — 4챔버 전부 PASS-경계 (CI 상한이 임계 5% 를 넘는다)
    cap = V.DEFAULTS["chamber_l5_max"]
    bands = {c: V.band_for_rate(round(v["l5"] * v["n"]), v["n"], cap)
             for c, v in m["by_chamber"].items()}
    assert len(bands) == 4
    assert all(b["band"] == V.BAND_PASS_BOUNDARY for b in bands.values()), bands
    assert all(b["ci_hi"] > cap for b in bands.values()), "소표본 한계가 사라지면 문서 §2 재검토"

    # C1 — 산술 0.8983 (구 판정, 0.90 미달) → 가중 0.9019 (원값 복원해도 PASS)
    from ae_pipeline.constants import feat_weight
    rates = rep["probe"]["by_channel_at_gate"]
    w = {c: feat_weight(c) for c in rates}
    arith = sum(rates.values()) / len(rates)
    weighted = sum(rates[c] * w[c] for c in rates) / sum(w.values())
    assert arith == pytest.approx(rep["probe"]["by_sigma"]["4sigma"], abs=1e-4)
    assert arith == pytest.approx(0.8983, abs=5e-5)
    assert weighted == pytest.approx(0.9019, abs=5e-5)
    assert weighted >= V.DEFAULTS["probe_min_detect"] > arith, (
        "0.90 복원의 근거(가중 채점으로 임계를 넘는다)가 무너졌다 — 완화 철회를 재검토할 것")

    # 총평: PASS 유지 · 경계 라벨 2종이 승인 브리프에 노출된다
    checks = [V._c("B1_L5_오경보율", b1["pass"], V.fmt_band(b1), band=b1),
              V._c("B5_챔버별_L5", True, "4챔버", band=max(bands.values(),
                   key=lambda b: (V.BAND_SEVERITY[b["band"]], b["rate"])))]
    gate = _gate(l5_budget=budget, require_labels=False)
    gate["_weakened"] = ["l5_budget", "require_labels"]   # e4 라운드 실제 완화 2건
    ab = V.build_approval_brief(gate, checks, V.VERDICT_PASS, labels_path=None)
    assert ab["weakened_count"] == 2, "문서 §7 '약화 3건 → 2건' 과 어긋난다"
    assert ab["boundary_count"] == 2, "문서 §6 '경계 라벨 2종' 과 어긋난다"
    assert any("E1" in r for r in ab["residual_risks"])   # e4 소급 불가 잔여 리스크


def test_v2_e4_probe_weights_come_from_appendix_a():
    """e4 프로브 채널의 w_c 가 문서 §3-1 표와 일치한다 (부록 A 단일 소스)."""
    from ae_pipeline.constants import feat_weight
    doc = {"C52": 0.7, "C17": 1.0, "C57": 0.3, "C58": 0.5, "C63": 0.7,
           "C16": 1.0, "C15": 1.0, "C9": 1.0, "C11": 1.0, "C31": 1.0}
    for ch, w in doc.items():
        assert feat_weight(f"{ch}_s_mean") == w, f"{ch}: 부록 A 가중이 문서와 다르다"


# ── §1-5 표본 설계 연동 (WP-4 스윕 — PM 결정 #4) ────────────────────────────

def _triplet(*a, **k):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from ct2_threshold_sweep import check_sample_triplet
    return check_sample_triplet(*a, **k)


def test_v2_sweep_adds_ci_halfwidth_axis():
    """★해상도만으로는 부족하다 — n=600 은 ⓑ 해상도를 통과하지만 CI 반폭에서 걸린다.

    이 조합(홀드아웃 600)이 정확히 e4 다: 해상도 0.17%p 로 예산 4% 를 '비율로' 재는 데는
    문제가 없는데, Wilson 반폭이 ±1.25%p 라 예산 4% 와 원 목표 2% 를 **구분하지 못한다**.
    구 검사는 이 조합을 OK 로 통과시켰고, 그래서 표본 설계가 경계 밴드를 못 벗어났다.
    """
    t = _triplet(6000, 0.2, 0.5, 0.04, 100)              # → 홀드아웃 600
    assert t["n_holdout"] == 600
    assert t["measurable"] and t["meets_floor"], "구 두 축은 통과한다 — 그게 문제였다"
    assert t["ci_halfwidth"] == pytest.approx(0.0125, abs=2e-4)   # 문서 §1-5 ±1.25%p
    assert t["band_resolvable"] is False
    assert "CI 반폭" in t["note"]


@pytest.mark.parametrize("hold,want", [(600, 0.0125), (800, 0.0108), (1200, 0.0088)])
def test_v2_sweep_halfwidth_matches_documented_table(hold, want):
    """문서 §1-5 반폭 표 재현 (p≈3.5%): n=600 ±1.25 · 800 ±1.08 · 1200 ±0.88 %p."""
    t = _triplet(hold * 10, 0.2, 0.5, 0.04, 100)
    assert t["n_holdout"] == hold
    assert t["ci_halfwidth"] == pytest.approx(want, abs=2e-4)


def test_v2_sweep_note_lists_every_failed_axis():
    """세 축 중 여럿이 깨지면 **전부** note 에 적는다 — 하나만 고치고 통과했다고 오해하지 않게."""
    t = _triplet(200, 0.2, 0.5, 0.04, 100)               # 홀드아웃 20 — 하한·해상도·반폭 전부 미달
    assert not (t["meets_floor"] or t["measurable"] or t["band_resolvable"])
    assert t["note"].count("/") >= 2


# ── 리뷰 지적 반영분 (2026-08-09) ────────────────────────────────────────────

def test_v2_fmt_band_flips_operator_on_fail():
    """★FAIL 항목에 `≤` 를 박지 않는다 — 이 문자열은 승인 브리프까지 흐른다.

    구 형식(`f"{l5} ≤ {l5_max}"`)을 그대로 물려받으면 e5(40/600)의 detail 이
    `"6.67% (40/600) ≤ 4% · FAIL-확정"` 이라는 거짓 부등식이 된다.
    """
    assert "≤" in V.fmt_band(V.band_for_rate(23, 600, 0.04))     # PASS
    bad = V.fmt_band(V.band_for_rate(40, 600, 0.04))             # FAIL
    assert ">" in bad and "≤" not in bad, bad


def test_v2_probe_leniency_window_is_surfaced():
    """★가중 채점이 v1(산술 0.875)보다 관대해지는 구간을 브리프에 올린다.

    v1 = 산술 ≥ 0.875 / v2 = 가중 ≥ 0.90 이므로 **가중−산술 > 0.025** 면 v1 이 막던 번들을
    v2 가 통과시킨다. 반례: 저가중 2채널(C57 w=0.3 · C58 w=0.5)만 검출 0 →
    산술 0.8000(v1 FAIL) / 가중 0.9024(v2 PASS). bool 스위치라 `_weakened` 축으로는
    표현할 수 없어, 격차를 재서 잔여 리스크로 노출한다.
    """
    from ae_pipeline.constants import feat_weight
    rates = {f"{c}_s_mean": (0.0 if c in ("C57", "C58") else 1.0) for c in CH_SETTLE_CONT}
    w = {c: feat_weight(c) for c in rates}
    arith = sum(rates.values()) / len(rates)
    weighted = sum(rates[c] * w[c] for c in rates) / sum(w.values())
    assert arith == pytest.approx(0.80, abs=1e-9)                # v1 기준(0.875) 미달
    assert weighted == pytest.approx(0.9024, abs=5e-5)           # v2 기준(0.90) 통과
    assert weighted - arith > V.PROBE_LENIENCY_MARGIN

    probe = {"at_gate": weighted, "at_gate_weighted": weighted, "at_gate_arithmetic": arith,
             "leniency_gap": round(weighted - arith, 4), "in_leniency_window": True}
    ab = V.build_approval_brief(_gate(), [], V.VERDICT_PASS, labels_path="l.csv", probe=probe)
    assert any("저가중 채널" in r for r in ab["residual_risks"]), ab["residual_risks"]


def test_v2_probe_leniency_window_quiet_when_aligned():
    """정상 번들(전 채널 고르게 검출)에서는 격차가 작아 잔여 리스크가 뜨지 않는다."""
    feats, cols, scaler, X, scorer, calib = _healthy_setup()
    _, d = V.probe_detection(scorer, scaler, feats, cols, calib, _gate())
    assert d["in_leniency_window"] is False, d["leniency_gap"]
    assert d["at_gate"] == d["at_gate_weighted"]                 # 가중 모드 판정값


def test_v2_bool_params_reject_quoted_strings(tmp_path, caplog):
    """★`bool("false") is True` 함정 — 게이트를 끄려는(또는 보수적으로 되돌리는) 조작이
    조용히 무시되면 안 된다. `probe_weighted: "false"` 는 **더 엄격한 쪽**이라 특히 위험하다."""
    y = tmp_path / "params.yaml"
    y.write_text('ct:\n  ct2_gate_require_labels: "false"\n  ct2_gate_probe_weighted: "no"\n',
                 encoding="utf-8")
    p = V.load_gate_params(y)
    assert p["require_labels"] is False and p["probe_weighted"] is False
    assert "require_labels" in p["_weakened"]
    # 해석 불가는 기본값 + 경고 (조용한 통과 금지)
    y.write_text('ct:\n  ct2_gate_probe_weighted: maybe\n', encoding="utf-8")
    with caplog.at_level("WARNING"):
        p2 = V.load_gate_params(y)
    assert p2["probe_weighted"] is V.DEFAULTS["probe_weighted"]
    assert any("불리언으로 못 읽음" in r.message for r in caplog.records)


def test_v2_sweep_consumes_judgment_value_not_arithmetic():
    """★스윕 요약이 **판정값**(`probe.at_gate`)을 집는다 — `by_sigma`(산술)를 집으면
    산술 분포로 임계를 재산정해 가중 판정에 먹이게 된다 (헌법 7장 하류 미추적)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from ct2_threshold_sweep import probe_at_gate
    v3 = {"gate_params": {"probe_sigma": 4.0},
          "probe": {"at_gate": 0.9019, "by_sigma": {"4sigma": 0.8983}}}
    assert probe_at_gate(v3) == 0.9019
    # 구 리포트(schema ≤2)는 `at_gate` 가 없어 산술 폴백 — `scoring` 으로 구분 가능해야 한다
    v2 = {"gate_params": {"probe_sigma": 4.0}, "probe": {"by_sigma": {"4sigma": 0.8983}}}
    assert probe_at_gate(v2) == 0.8983


def test_v2_sweep_800_holdout_does_not_meet_ci_target():
    """★"홀드아웃 800장 = 반폭 <1%p" 는 이 산식으로 계산하면 거짓이다 (940장이 실제 하한).

    문서·주석이 800으로 적혀 있으면 스윕 담당자가 800을 목표로 조합을 짜고 전 조합이
    ★ 표시를 받는다 → "검사가 이상하다"로 오진하거나 `--target-halfwidth` 를 완화해
    통과시킨다. 숫자를 테스트로 못 박아 문구 표류를 막는다.
    """
    assert _triplet(8000, 0.2, 0.5, 0.04, 100)["band_resolvable"] is False   # 800장
    assert _triplet(9400, 0.2, 0.5, 0.04, 100)["band_resolvable"] is True    # 940장
    assert _triplet(10000, 0.2, 0.5, 0.04, 100)["band_resolvable"] is True   # 1000장(목표)
