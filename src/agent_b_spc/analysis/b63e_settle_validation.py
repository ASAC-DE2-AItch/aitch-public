"""B6-3-e 정착 판정 B-단독 실측 검증 (W7 지표 중 B가 팀 없이 돌릴 수 있는 부분).

σ-수렴 정착 로직에 **시뮬 센서 시퀀스**를 먹여 파라미터(W·M·settle_min·k_se) 거동을 측정한다:
- **false-settle율**: 안 멈춘(드리프트·slow-τ) 시퀀스가 조기 정착(converged=True)한 비율 → 낮아야 안전
- **검출율/지연**: 진짜 수렴 시퀀스가 조기 정착하는 비율·시점
- **k 민감도**: deadband_k_se ∈ {1.5, 2, 2.5} 스윕 (밴드 폭 ↔ 오탐/미탐 트레이드오프)
- **자기상관 φ**(AR1)·**느린 τ**(지수 접근) 시나리오 (제안서 §7 ⓐⓑ)

⚠️ 알람 레벨 오탐≤5%·미탐 0(W7 Scorecard)은 A·C 관통 팀 통합 라운드 — 여기 범위 밖.
실행: `python -m src.agent_b_spc.analysis.b63e_settle_validation`
"""
from __future__ import annotations

import logging
from collections import namedtuple

import numpy as np

from src.agent_b_spc.provisional_mode import ProvisionalConfig, ProvisionalMode

log = logging.getLogger(__name__)

_GK = ("SIM_CH_1", "C6_0", 4, "settled", "C11")
_Pt = namedtuple("_Pt", "group_key value")
_SIGMA_REF = 10.0                      # firm σ (밴드 기준자)
_W, _M, _SETTLE_MIN, _BOUNDARY = 200, 2, 600, 1000


def _cfg(k_se: float) -> ProvisionalConfig:
    return ProvisionalConfig(
        provisional_k=5.0, seasoning_quiet=10, seasoning_loud=_BOUNDARY, k_sigma=3.0,
        settle_window_wafers=_W, settle_consecutive_m=_M, settle_min_wafers=_SETTLE_MIN,
        settle_block_min_fill=0.5, deadband_k_se=k_se)


def _run(values: np.ndarray, k_se: float) -> tuple[int | None, bool | None]:
    """센서 시퀀스 → 정착 로직 구동. (settled_at, converged) 반환 (신호 없으면 (None,None))."""
    firm = {_GK: {"center": 100.0, "sigma": _SIGMA_REF, "ucl": 100 + 3 * _SIGMA_REF,
                  "lcl": 100 - 3 * _SIGMA_REF, "method": "sigma", "limit_version": "v1"}}
    p = ProvisionalMode(_cfg(k_se), limits=firm, whitelist={_GK})
    p.on_pm("SIM_CH_1", 1)
    p.on_qual_verdict("SIM_CH_1", "loud")
    for i, v in enumerate(values):
        sig = p.on_wafer("SIM_CH_1", f"W{i}", [_Pt(_GK, float(v))])
        if sig is not None:
            return sig.settled_at, sig.converged
    return None, None


def _series(rng, kind: str, n: int = 1400) -> np.ndarray:
    """시뮬 센서 시퀀스 (100 중심, σ_ref=10 스케일)."""
    noise = rng.normal(0, 2.0, n)                        # 관측 노이즈
    if kind == "converge":                               # 빠른 정착 — 즉시 안정
        return 100.0 + noise
    if kind == "slow_tau":                               # 느린 지수 접근(τ=400) — 늦게 정착
        t = np.arange(n)
        return 100.0 + 15.0 * np.exp(-t / 400.0) + noise
    if kind == "drift":                                  # 끊임없는 선형 드리프트 — 안 멈춤
        return 100.0 + 0.02 * np.arange(n) + noise       # 1400장에 +28 (2.8σ)
    if kind == "ar1":                                     # 자기상관 φ=0.9 — 방황(정착 아님)
        x = np.zeros(n)
        for i in range(1, n):
            x[i] = 0.9 * x[i - 1] + rng.normal(0, 3.0)
        return 100.0 + x
    raise ValueError(kind)


def main(chambers: int = 40, seed: int = 20260726) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rng = np.random.default_rng(seed)
    log.info("B6-3-e 정착 검증 — W=%d M=%d settle_min=%d boundary=%d · 챔버 %d/시나리오",
             _W, _M, _SETTLE_MIN, _BOUNDARY, chambers)
    log.info("%-10s %-6s %8s %8s %10s", "시나리오", "k_se", "조기정착%", "중앙시점", "판정")
    # 정착해야: converge·slow_tau (조기정착률 높아야) / 안멈춤: drift·ar1 (false-settle 낮아야)
    for kind, expect in [("converge", "검출"), ("slow_tau", "검출"),
                         ("drift", "false-settle"), ("ar1", "false-settle")]:
        for k_se in (1.5, 2.0, 2.5):
            early = []
            for _ in range(chambers):
                at, conv = _run(_series(rng, kind), k_se)
                if conv:                                 # 조기 σ-수렴 정착
                    early.append(at)
            rate = 100.0 * len(early) / chambers
            med = int(np.median(early)) if early else None
            verdict = ("false-settle" if kind in ("drift", "ar1") else "검출")
            log.info("%-10s %-6.1f %7.1f%% %8s %10s", kind, k_se, rate,
                     med if med is not None else "-",
                     f"{'⚠' if kind in ('drift','ar1') and rate>5 else '·'} {verdict}")


if __name__ == "__main__":
    main()
