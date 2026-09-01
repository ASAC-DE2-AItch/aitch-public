"""
calibration.py — ae_raw → ae_score [0,1] 캘리브레이션 (ECDF 앵커 단조매핑).

원 노트북 `ae_v3.ipynb` P6-1 셀과 동일. VAL 정상 raw 분위수를 앵커에 고정하고
그 사이를 선형보간(단조·포화)한다. **0.2 = VAL P98.5**에 고정 → L5 오경보 여유
(REPORT_05 실측 L5 = 1.53% ≤ 2%). Qual 임계 = 0.2 (C2 합의 연동).

calib은 model과 한 몸 — 재학습(CT②)마다 새 VAL 분포로 재산출한다.
"""
from __future__ import annotations

import json

import numpy as np

# 채택 앵커 (REPORT_05 확정)
ANCH_PERCENTILE = [50, 90, 98.5, 99.5, 99.9]
ANCH_SCORE = [0.05, 0.10, 0.20, 0.50, 1.00]   # 0.2 = VAL P98.5
QUAL_THRESHOLD = 0.2


def fit_calibration(raw_val, anch_p=None, anch_y=None, qual_threshold=QUAL_THRESHOLD) -> dict:
    """VAL 정상 ae_raw → 캘리 딕셔너리(calib.json 스키마).

    anchors_x = VAL raw의 anch_p 분위수. anchors_y = 목표 score.
    """
    anch_p = list(ANCH_PERCENTILE if anch_p is None else anch_p)
    anch_y = list(ANCH_SCORE if anch_y is None else anch_y)
    raw_val = np.asarray(raw_val, float)
    anch_x = [float(np.percentile(raw_val, p)) for p in anch_p]
    return {
        "method": "ecdf_anchor",
        "raw_score": "conditioned_residual_maha(rev3_z16)",
        "anchors_percentile": anch_p,
        "anchors_x": anch_x,
        "anchors_y": anch_y,
        "qual_threshold": float(qual_threshold),
    }


def apply_calibration(raw, calib: dict) -> np.ndarray:
    """ae_raw → ae_score [0,1] (단조·포화 선형보간)."""
    r = np.asarray(raw, float)
    x = calib["anchors_x"]
    y = calib["anchors_y"]
    return np.clip(np.interp(r, x, y, left=0.0, right=1.0), 0.0, 1.0)


def evaluate_calibration(sc_val, sc_seg2_early, sc_c6_1, sc_inject_mid) -> dict:
    """캘리 채택 5기준 판정 (REPORT_05 §1). 각 항목 (통과여부, 값).

    ①VAL평균≈0.05±0.02 ②VAL P95<0.2 ③seg2조기 평균>0.2 ④C6_1 평균>0.2
    ⑤중강도주입 median>0.2. + L5 오경보(VAL ae_score>0.2)는 L5 헬퍼 참조.
    """
    sc_val = np.asarray(sc_val, float)
    crit = {
        "①VAL평균≈0.05±0.02": (abs(float(sc_val.mean()) - 0.05) <= 0.02, round(float(sc_val.mean()), 3)),
        "②VAL P95<0.2":       (float(np.percentile(sc_val, 95)) < 0.2, round(float(np.percentile(sc_val, 95)), 3)),
        "③seg2조기 평균>0.2":   (float(np.mean(sc_seg2_early)) > 0.2, round(float(np.mean(sc_seg2_early)), 3)),
        "④C6_1 평균>0.2":      (float(np.mean(sc_c6_1)) > 0.2, round(float(np.mean(sc_c6_1)), 3)),
        "⑤중강도주입 median>0.2": (float(np.median(sc_inject_mid)) > 0.2, round(float(np.median(sc_inject_mid)), 3)),
    }
    return crit


def false_alarm_rate(sc_val, threshold=QUAL_THRESHOLD) -> float:
    """L5 오경보율 = VAL ae_score > threshold 비율 (DoD5 · ≤2% 목표)."""
    return float(np.mean(np.asarray(sc_val, float) > threshold))


def save_calibration(calib: dict, path) -> None:
    from pathlib import Path
    Path(path).write_text(json.dumps(calib, ensure_ascii=False, indent=1), encoding="utf-8")


def load_calibration(path) -> dict:
    from pathlib import Path
    return json.loads(Path(path).read_text(encoding="utf-8"))
