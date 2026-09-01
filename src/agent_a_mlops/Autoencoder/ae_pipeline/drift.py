"""
drift.py — ae_drift_score (EWMA 레벨, 데이터주도 baseline).

원 노트북 `ae_v3.ipynb` P6-3 셀과 동일. drift = 같은 ae_score 스트림의 누적(EWMA)
읽기 — **단일 ae_raw 후처리, 별도 모델 없음**(불가침 12 정합).

역할: per-wafer ae_score가 놓치는 느린 sub-σ 지속 드리프트 + 레짐 전환 감지.
anomaly와 상보 — 스파이크=ae_score만, 느린 drift=ae_drift_score만, 레짐=둘 다.

상태성: per-chamber. 챔버별 EWMA 상태를 유지해야 하므로 운영 추론은 DriftTracker로
챔버당 하나씩 상태를 들고 간다.

⚠ 분모 보호 (2026-07-28 코드리뷰 §1-4): drift = (E − B0)/(alarm − B0) 이므로
  **B0 < alarm 이 성립해야만** 정의된다. B0 는 정상 EWMA 의 P99 라 alarm(0.2) 보다
  작다는 보장이 없다 — CT② 재학습 후 새 정상셋 분포가 나쁘면 B0 ≥ alarm 이 될 수 있고,
  그러면 ZeroDivisionError(전량 더미 폴백 = 조용한 기능 정지) 또는 부호 반전(정상일수록
  drift 1.0)이 난다. 그래서 ⓐ 산출 시점(fit_drift_baseline)과 ⓑ 사용 시점(DriftTracker,
  drift_stream — 외부 drift.json 로드 경로 대비)에 **이중으로 캡**을 건다. 캡이 걸리면
  경고 로그를 남긴다 — 값이 조정됐다는 사실 자체가 정상셋 품질 신호이기 때문이다.
"""
from __future__ import annotations

import json
import logging

import numpy as np

log = logging.getLogger("ae.drift")

ALPHA = 0.05   # EWMA 계수 (긴 기억)
ALARM = 0.20   # 알람선 (Qual 임계와 동일)
B0_MARGIN = 1e-6   # baseline_B0 상한 여유 — B0 ≤ alarm − 이 값 (분모 0·음수 차단)


def cap_baseline(b0: float, alarm: float = ALARM, where: str = "") -> float:
    """baseline_B0 를 `alarm − B0_MARGIN` 이하로 캡 (분모 보호 — 리뷰 §1-4).

    Args:
        b0: 산출·적재된 baseline_B0.
        alarm: 알람선(분모의 상단).
        where: 경고 로그에 남길 호출 지점 표기.

    Returns:
        캡이 적용된 baseline_B0 (원래 유효하면 그대로).
    """
    b0, alarm = float(b0), float(alarm)
    limit = alarm - B0_MARGIN
    if b0 >= limit:
        log.warning("%sbaseline_B0=%.4f 가 alarm=%.2f 이상 → %.6f 로 캡 적용. "
                    "정상셋 품질(캘리 앵커·VAL 분포)을 확인하세요.",
                    f"[{where}] " if where else "", b0, alarm, limit)
        return limit
    return b0


def ewma(scores, e0, alpha=ALPHA) -> np.ndarray:
    """EWMA 스트림. E_t = alpha*s_t + (1-alpha)*E_{t-1}, 초기값 e0."""
    E = float(e0)
    out = []
    for s in scores:
        E = alpha * float(s) + (1 - alpha) * E
        out.append(E)
    return np.array(out)


def fit_drift_baseline(normal_scores, alpha=ALPHA, alarm=ALARM) -> dict:
    """정상 ae_score 스트림 → drift 계약 딕셔너리(drift.json 스키마).

    baseline_B0 = 정상 EWMA(초기값=정상 중앙값)의 P99 → 정상 drift 대부분 0.
    B0 ≥ alarm 이면 분모가 0·음수가 되므로 캡한다(리뷰 §1-4 — 경고 로그 동반).
    """
    normal_scores = np.asarray(normal_scores, float)
    e_norm = ewma(normal_scores, e0=float(np.median(normal_scores)), alpha=alpha)
    b0 = cap_baseline(float(np.percentile(e_norm, 99)), alarm, where="fit_drift_baseline")
    return {
        "method": "ewma_level",
        "alpha": float(alpha),
        "alarm_level": float(alarm),
        "baseline_B0": b0,
        "formula": "clip((EWMA_alpha(ae_score) - B0) / (alarm - B0), 0, 1)",
        "state": "per-chamber (stateful)",
    }


def drift_stream(scores, drift_cfg: dict) -> np.ndarray:
    """ae_score 스트림 → ae_drift_score 스트림 (배치·오프라인 검증용).

    스트림 시작 EWMA 초기값 = B0 (정상 기준선에서 출발).
    외부 drift.json 이 캡 이전 값을 담고 있을 수 있어 여기서도 캡한다(리뷰 §1-4).
    """
    alarm = float(drift_cfg["alarm_level"])
    b0 = cap_baseline(drift_cfg["baseline_B0"], alarm, where="drift_stream")
    alpha = float(drift_cfg["alpha"])
    e = ewma(scores, e0=b0, alpha=alpha)
    return np.clip((e - b0) / (alarm - b0), 0.0, 1.0)


class DriftTracker:
    """per-chamber 상태 유지 드리프트 추적기 (운영 스트리밍 추론용).

    챔버마다 하나씩 생성해 wafer 도착 순서대로 update()를 호출한다.
    번들의 drift.json 이 캡 이전 값일 수 있으므로 로드 시점에도 캡한다(리뷰 §1-4 — 이중 안전망).
    """

    def __init__(self, drift_cfg: dict, e0=None):
        self.alarm = float(drift_cfg["alarm_level"])
        self.b0 = cap_baseline(drift_cfg["baseline_B0"], self.alarm, where="DriftTracker")
        self.alpha = float(drift_cfg["alpha"])
        self.E = self.b0 if e0 is None else float(e0)   # 정상 기준선에서 출발

    def update(self, ae_score: float) -> float:
        """새 wafer의 ae_score 반영 → 현재 ae_drift_score [0,1]."""
        self.E = self.alpha * float(ae_score) + (1 - self.alpha) * self.E
        return float(np.clip((self.E - self.b0) / (self.alarm - self.b0), 0.0, 1.0))

    @property
    def state(self) -> float:
        return self.E


def evaluate_drift(d_norm, d_sub_sigma, d_seg2) -> bool:
    """drift 계약 성립 판정 (REPORT_05 §2): 정상≈0 · sub-σ↑ · 레짐↑."""
    return (
        float(np.percentile(d_norm, 95)) < 0.4
        and float(np.asarray(d_sub_sigma)[-1]) > 0.5
        and float(np.asarray(d_seg2)[-1]) > 0.5
    )


def save_drift(drift_cfg: dict, path) -> None:
    from pathlib import Path
    Path(path).write_text(json.dumps(drift_cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def load_drift(path) -> dict:
    from pathlib import Path
    return json.loads(Path(path).read_text(encoding="utf-8"))
