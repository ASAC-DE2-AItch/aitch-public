# -*- coding: utf-8 -*-
"""AE 수준 1/2 재보정기의 비파괴 가드와 분할 계약 테스트."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
AE_DIR = ROOT / "src" / "agent_a_mlops" / "Autoencoder"
sys.path.insert(0, str(AE_DIR))

from ae_pipeline import recalibrate as R  # noqa: E402


GATE = {"min_val_wafers": 100, "min_gate_wafers": 100}


@pytest.mark.skipif(
    not (ROOT / "models" / "anomaly_ae" / "ae_v1" / "manifest.json").exists(),
    reason="공개 스냅샷에는 AE 학습 아티팩트 번들(models/anomaly_ae/ae_v1)을 넣지 않는다 — 번들이 있는 환경에서만 검사한다.",
)
def test_base_bundle_is_complete_and_hashed():
    """번들 7파일 완결 + 바이너리 아티팩트 해시 일치 (PM 핫픽스 08/04 00:42).

    원판은 verify_complete_bundle(전 해시 비교)이었는데, manifest 의 텍스트(.json) 해시가
    CRLF 작업트리(Windows)에서 계산돼 LF 체크아웃(CI ubuntu)과 어긋난다 — `* text=auto`
    EOL 정규화 팬텀. 실측: dev run 30827755193 에서 calib/drift/feature_spec .json 3종만
    mismatch, .pt/.pkl/.npz 바이너리 3종은 일치. 텍스트 해시의 EOL 정준화(또는 manifest
    재계산)는 A 후속 — 그전까지 이 테스트는 완결성과 바이너리 해시만 고정한다.
    """
    bundle = ROOT / "models" / "anomaly_ae" / "ae_v1"
    from ae_pipeline import io_bundle
    A = io_bundle.ARTIFACT_NAMES
    missing = [n for n in A.values() if not (bundle / n).exists()]
    assert missing == []
    verified = io_bundle.verify_bundle(bundle)
    bad_bin = [n for n, r in verified.items() if not r["ok"] and not n.endswith(".json")]
    assert bad_bin == []
    manifest = io_bundle.read_manifest(bundle)
    assert manifest["ae_model_version"] == "z16_rev3_seg1"


def test_split_fit_holdout_is_time_ordered():
    order = [f"W{i:04d}" for i in range(1000)]
    fit, hold = R.split_fit_holdout(order, 0.2, GATE)
    assert fit == order[:800]
    assert hold == order[800:]


@pytest.mark.parametrize("frac", [0.0, 1.0, -0.1, 1.1])
def test_split_rejects_invalid_fraction(frac):
    with pytest.raises(ValueError, match="0과 1"):
        R.split_fit_holdout(list(range(1000)), frac, GATE)


def test_split_rejects_unmeasurable_holdout():
    with pytest.raises(ValueError, match="홀드아웃 표본 부족"):
        R.split_fit_holdout(list(range(400)), 0.2, GATE)


def test_select_normal_order_filters_c6_and_keeps_time_order():
    df = pd.DataFrame(
        {
            "C64": ["W2", "W1", "W3"],
            "C10": pd.to_datetime(["2026-01-02", "2026-01-01", "2026-01-03"]),
            "C6": ["C6_0", "C6_0", "C6_1"],
        }
    )
    assert R.select_normal_order(df) == ["W1", "W2"]


def test_select_normal_order_uses_all_when_label_absent():
    df = pd.DataFrame(
        {
            "C64": ["W2", "W1"],
            "C10": pd.to_datetime(["2026-01-02", "2026-01-01"]),
        }
    )
    assert R.select_normal_order(df) == ["W1", "W2"]


def test_raw_drift_baseline_exposes_bad_normal_stream():
    assert R.raw_drift_baseline(np.full(300, 0.5)) >= R.ALARM

