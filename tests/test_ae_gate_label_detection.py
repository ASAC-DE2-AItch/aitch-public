# -*- coding: utf-8 -*-
"""게이트 보강 3건 계약 테스트 (torch 무의존).

대상 (2026-08-03 신설):
  E군  `check_label_detection`  — 라벨된 실제 주입 구간 검출률 (C군 프로브의 사각 보완)
  B군  `chamber_breakdown`      — 홀드아웃 오경보 챔버별 분해 (판정 무관·리포트 병기)
  재학습 `exclude_injected`      — 정상셋에서 주입 wafer 제외 (학습 오염 재발 방지)

배경: 2026-08-02 라운드에서 주입 653장이 `C6_0` 필터를 통과해 학습에 섞였고, C군 프로브는
97.8%@4σ 로 통과했는데 실제 주입 검출은 0.6% 였다. 세 항목 모두 그 사고의 재발 방지물이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
AE_DIR = ROOT / "src" / "agent_a_mlops" / "Autoencoder"
sys.path.insert(0, str(AE_DIR))

from ae_pipeline import validate_bundle as V  # noqa: E402
from ae_pipeline import retrain as R          # noqa: E402

GATE = dict(V.DEFAULTS)


# ── B군: 챔버별 분해 ────────────────────────────────────────────────────────
def test_chamber_breakdown_exposes_what_the_mean_hides():
    """총계는 조용해 보여도 한 챔버만 시끄러울 수 있다 (2026-08-03 실측 재현)."""
    score = np.concatenate([np.full(90, 0.05), np.full(10, 0.9),      # CH2 10% 초과
                            np.full(99, 0.05), np.full(1, 0.9)])      # CH4 1% 초과
    ch = ["CH2"] * 100 + ["CH4"] * 100
    out = V.chamber_breakdown(score, ch, qual=0.2)
    assert out["CH2"]["l5"] == pytest.approx(0.10)
    assert out["CH4"]["l5"] == pytest.approx(0.01)
    assert float(np.mean(score > 0.2)) == pytest.approx(0.055)   # 총계는 중간값으로 희석


def test_chamber_breakdown_is_skipped_without_labels():
    assert V.chamber_breakdown(np.zeros(5), None, 0.2) == {}


def test_chamber_breakdown_skips_on_length_mismatch():
    """길이 불일치를 조용히 잘라 쓰면 엉뚱한 챔버에 수치가 붙는다."""
    assert V.chamber_breakdown(np.zeros(5), ["CH1", "CH2"], 0.2) == {}


# ── E군: 정답지 대조 검출률 ─────────────────────────────────────────────────
class _Scorer:
    """wafer id 로 raw 를 되돌려주는 더미 — E군 조립 로직만 검사한다."""

    def __init__(self, raw_by_row):
        self._raw = raw_by_row

    def ae_raw(self, X):
        return np.asarray(self._raw[: len(X)], float)


class _Scaler:
    def transform(self, F):
        return np.asarray(F, float)


CALIB = {"anchors_x": [0.0, 1.0], "anchors_y": [0.0, 1.0], "qual_threshold": 0.2}


def _frame(wafers):
    """extract_features 가 wafer 당 1행을 내도록 최소 프레임 구성."""
    rows = []
    for i, w in enumerate(wafers):
        rows.append({"C64": w, "C24": "SIM_CH_2", "C10": i, "C7": 4.0, "C46": 0})
    return pd.DataFrame(rows)


def _labels(wafers, injected, k=None, scenario="SC4"):
    return pd.DataFrame({
        "C64": wafers,
        "injected": injected,
        "k": k if k is not None else [999] * len(wafers),
        "scenario_id": [scenario] * len(wafers),
        "C24": ["SIM_CH_2"] * len(wafers),
    })


def test_load_labels_rejects_missing_columns(tmp_path):
    p = tmp_path / "bad.csv"
    pd.DataFrame({"C64": ["W1"]}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="필수 컬럼"):
        V.load_injection_labels(p)


def test_load_labels_rejects_all_normal(tmp_path):
    """주입 0장 라벨은 E군을 조용히 무력화한다 — 명시적 오류로 잡는다."""
    p = tmp_path / "none.csv"
    pd.DataFrame({"C64": ["W1", "W2"], "injected": [False, False]}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="주입 wafer 0장"):
        V.load_injection_labels(p)


def test_small_label_sample_is_reported_not_judged():
    """소표본에서 비율 판정은 이진에 가까워 의미가 없다 (D1 하한과 같은 취지)."""
    wafers = [f"W{i}" for i in range(5)]
    checks, detail = V.check_label_detection(
        _Scorer([0.0] * 5), _Scaler(), _frame(wafers), ["x"], CALIB, GATE,
        _labels(wafers, [True] * 5), train_wafers=set())
    assert checks[0]["pass"] is True and detail["judged"] is False


def test_warmup_window_is_excluded_from_judgement():
    """drift 램프 초기(k 작음)는 설계상 크기 ≈0 — 판정에 넣으면 정상 모델도 FAIL 한다."""
    wafers = [f"W{i}" for i in range(60)]
    k = list(range(60))                       # k=0..59, warmup 20 → 40장만 판정
    lab = _labels(wafers, [True] * 60, k=k)
    kept = lab[lab.k >= GATE["label_warmup_k"]]
    assert len(kept) == 40


def test_training_wafers_are_excluded_from_label_evaluation():
    """학습에 쓰인 주입 wafer 의 미검출은 이미 오염의 결과 — 평가 표본에서 뺀다."""
    wafers = [f"W{i}" for i in range(80)]
    lab = _labels(wafers, [True] * 80)
    train = {f"W{i}" for i in range(50)}
    remaining = lab[~lab.C64.isin(train)]
    assert len(remaining) == 30


# ── 재학습: 주입 제외 ───────────────────────────────────────────────────────
def _df_for(wafers):
    return pd.DataFrame({"C64": wafers, "C10": range(len(wafers))})


def test_exclude_injected_drops_labelled_wafers(tmp_path):
    wafers = [f"W{i}" for i in range(10)]
    p = tmp_path / "labels.csv"
    pd.DataFrame({"C64": wafers, "injected": [i >= 6 for i in range(10)]}).to_csv(p, index=False)
    kept = R.exclude_injected(_df_for(wafers), wafers, p)
    assert kept == wafers[:6]


def test_exclude_injected_preserves_time_order(tmp_path):
    wafers = [f"W{i}" for i in range(10)]
    p = tmp_path / "labels.csv"
    pd.DataFrame({"C64": wafers, "injected": [i % 2 == 0 for i in range(10)]}).to_csv(p, index=False)
    kept = R.exclude_injected(_df_for(wafers), wafers, p)
    assert kept == [w for w in wafers if wafers.index(w) % 2 == 1]


def test_exclude_injected_raises_when_nothing_left(tmp_path):
    """전량 제외는 조용히 넘기면 안 된다 — 라벨/데이터 대응 오류 신호."""
    wafers = [f"W{i}" for i in range(5)]
    p = tmp_path / "labels.csv"
    pd.DataFrame({"C64": wafers, "injected": [True] * 5}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="정상 wafer 0장"):
        R.exclude_injected(_df_for(wafers), wafers, p)


def test_exclude_injected_rejects_missing_columns(tmp_path):
    p = tmp_path / "bad.csv"
    pd.DataFrame({"C64": ["W1"]}).to_csv(p, index=False)
    with pytest.raises(ValueError, match="필수 컬럼"):
        R.exclude_injected(_df_for(["W1"]), ["W1"], p)


def test_select_normal_wafers_applies_label_filter(tmp_path):
    """명시 목록(--wafers-file) 경로에도 주입 제외가 걸린다."""
    wafers = [f"W{i}" for i in range(6)]
    df = pd.DataFrame({"C64": wafers, "C10": range(6), "C6": ["C6_0"] * 6})
    p = tmp_path / "labels.csv"
    pd.DataFrame({"C64": wafers, "injected": [False] * 4 + [True] * 2}).to_csv(p, index=False)
    got = R.select_normal_wafers(df, wafers=wafers, labels_path=p)
    assert got == wafers[:4]


# ── 게이트 임계 로딩 ────────────────────────────────────────────────────────
def test_new_gate_keys_have_defaults():
    p = V.load_gate_params()
    for k in ("label_min_detect", "label_warmup_k", "label_min_wafers"):
        assert k in p and isinstance(p[k], float)
