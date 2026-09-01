"""B4-3: 실력치 재산정 엔진 코어 (순수).

정기 리캘리 시점에 `(group_key, 최근 N장 값, 현행 관리선, 리셋 기준점, 분해능)`을 받아
새 관리선을 재산정하고, 이동량(σ 단위)에 따라 **변화없음/자동적용/승인대기**를 판정한다.
DB·Kafka를 모르는 순수 코어 — 라이브 수급·게이팅은 B5-1 이월(설계 §9).

산식은 `initial_limits.compute_group_limits`를 공유해 initial과 recalc가 동일하게 한다.
자(尺)는 robust σ: 1회 이동은 활성 σ 기준(A2), 누적은 사이클 시작 σ 기준(A3, telescoping).

확정 파라미터는 config/params.yaml에서 로드한다 (헌법 6-1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from src.common.config_util import _section, _key
from src.agent_b_spc.initial_limits import LimitConfig, compute_group_limits
# q↔k 정합 가드·EPS는 tttm과 동일 정의 재사용 — 재정의 금지(리뷰 L7, σ_ref 역산 정합 #12)
from src.agent_b_spc.tttm_engine import _check_q_k, EPS

# 저장소 루트의 공유 파라미터 파일 (헌법 6-1)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

# DB 저장값(영문 리터럴) — 모듈 상수화(리뷰 L5). 한글(대기/자동적용…)은 문서 설명용.
STATUS_AUTO_APPLIED = "AUTO_APPLIED"
STATUS_PROPOSED = "PROPOSED"
STATUS_SUPERSEDED = "SUPERSEDED"   # 같은 그룹의 옛 PROPOSED가 신규 제안에 밀려난 종결 상태(cross-path dedup)
TRIGGER_PERIODIC = "periodic"


@dataclass(frozen=True)
class RecalcConfig:
    """재산정 엔진 파라미터. 산식 파라미터는 LimitConfig를 합성으로 품는다 (리뷰 M1).

    필드명을 config 키명(control_limit_k_sigma)으로 복제하면 LimitConfig 속성(k_sigma)과
    어긋나 산식 호출에서 AttributeError → 기존 LimitConfig 재사용으로 단일 소스 유지.
    산식 호출은 `compute_group_limits(values, cfg.limit)`.
    """
    limit: LimitConfig
    deadband_k_se: float                    # dead_band = k×SE (A10)
    reset_delta_max_sigma: float            # cap_1 — 1회 재설정 폭 상한 (A2)
    reset_cum_max_sigma_per_cycle: float    # cap_cum — 사이클 누적 상한 (A3)
    min_group_n: int                        # 소표본 가드 (재산정 소표본 — spc절 min_group_n 재활용)

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "RecalcConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        le = _section(cfg, "limit_engine", path)
        spc = _section(cfg, "spc", path)
        limit = LimitConfig.load(path)
        _check_q_k(limit.q_high, limit.k_sigma)      # #12 정합 가드 (q_high Φ⁻¹ ≈ k_sigma)
        return cls(
            limit=limit,
            deadband_k_se=float(_key(le, "recalc_deadband_k_se", "limit_engine", path)),
            reset_delta_max_sigma=float(_key(le, "reset_delta_max_sigma", "limit_engine", path)),
            reset_cum_max_sigma_per_cycle=float(
                _key(le, "reset_cum_max_sigma_per_cycle", "limit_engine", path)),
            min_group_n=int(_key(spc, "min_group_n", "spc", path)),
        )


@dataclass(frozen=True)
class RecalcProposal:
    """재산정 판정 결과 (writer가 소비). no_change면 new_*·status는 None.

    자(尺)는 σ 단위: delta_sigma는 1회 이동량(활성 σ 기준). new_ucl/new_lcl은 method에 따라
    sigma 그룹=mean±kσ / quantile 그룹=새 q_lcl/q_ucl. new_sigma는 항상 채운다(control_limits
    sigma가 NOT NULL — 리뷰 H2).
    """
    group_key: tuple
    decision: str                       # no_change / auto_applied / needs_approval
    method: str                         # sigma / quantile (current 승계)
    trigger_type: str                   # periodic 고정
    n_used: int
    new_center: float | None = None
    new_ucl: float | None = None
    new_lcl: float | None = None
    new_sigma: float | None = None
    delta_sigma: float | None = None    # 1회 이동량(σ) — writer가 기록(누적엔 비사용, telescoping)
    status: str | None = None           # AUTO_APPLIED / PROPOSED
    reason: str | None = None           # no_change 사유


def sigma_ref_of(limit_row: dict, k_sigma: float) -> float:
    """관리선 행의 σ_ref 자(尺) — 공유 헬퍼 (H4). recompute_group·correction_history 공용.

    quantile(KEEP*) 그룹은 σ 컬럼이 참고 통계라 못 쓰고, 밴드에서 robust σ를 역산한다:
    (ucl − lcl) / (2·k_sigma) — ±k·σ 등가 분위수 밴드 가정(#12 가드가 로드 시 보증). 6 하드코딩
    금지(리뷰 L8) — k 변경에 자동 추종. tttm_engine.sigma_ref(인스턴스 메서드)는 범위 밖(미변경).
    """
    if limit_row.get("method") == "quantile":
        return (limit_row["ucl"] - limit_row["lcl"]) / (2.0 * k_sigma)
    return limit_row["sigma"]


def _filter_values(values) -> list[float]:
    """NaN/None 제거 후 float 리스트 (L11 — 판정 최상단 필터)."""
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        fv = float(v)
        if math.isnan(fv):
            continue
        out.append(fv)
    return out


def recompute_group(group_key, values, current, reset_ref, resolution, cfg: RecalcConfig) -> RecalcProposal:
    """정기 리캘리 재산정 코어 (순수). 값·기준점 주입 → 변화없음/자동/승인 판정.

    가드는 모든 나눗셈·compute_group_limits 前(리뷰 H-A — ZeroDivision·빈배열 크래시 방지).
    1회 이동은 활성 σ, 누적은 리셋 σ 기준(telescoping — H2/H3). 경계는 dead_band `<`(==는 변화),
    cap `>`(==는 이내=자동 — 결정문서 4-a·헌법 1-1 "초과"). 무이상 전제는 상류(B5-1) 책임(§3-6).
    """
    k = cfg.limit.k_sigma
    method = current["method"]
    filtered = _filter_values(values)
    n_used = len(filtered)
    sigma_ref = sigma_ref_of(current, k)

    # ── 가드 (나눗셈·compute_group_limits 前 — H-A) ──
    if n_used < cfg.min_group_n:
        return RecalcProposal(group_key, "no_change", method, TRIGGER_PERIODIC, n_used,
                              reason="small_sample")
    if sigma_ref <= EPS:
        return RecalcProposal(group_key, "no_change", method, TRIGGER_PERIODIC, n_used,
                              reason="degenerate")

    # 재산정 (n_used ≥ min_group_n 보장 → np.quantile 안전)
    new = compute_group_limits(np.array(filtered), cfg.limit)
    new_center = new["center"]
    new_ucl, new_lcl = (new["q_ucl"], new["q_lcl"]) if method == "quantile" else (new["ucl"], new["lcl"])
    new_sigma = new["sigma"]
    delta1 = abs(new_center - current["center"]) / sigma_ref

    def _proposal(decision: str, status: str) -> RecalcProposal:
        return RecalcProposal(group_key, decision, method, TRIGGER_PERIODIC, n_used,
                              new_center=new_center, new_ucl=new_ucl, new_lcl=new_lcl,
                              new_sigma=new_sigma, delta_sigma=delta1, status=status)

    # ── 리셋 기준점 가드 (M7) — 제안값 채워 보수적 승인 (None.sigma_ref 방지) ──
    if reset_ref is None or reset_ref["sigma_ref"] <= EPS:
        return _proposal("needs_approval", STATUS_PROPOSED)

    # ── σ 단위 환산 (가드 통과 후) ──
    cum = abs(new_center - reset_ref["center"]) / reset_ref["sigma_ref"]        # 누적 순이동(telescoping)
    res_sig = resolution / sigma_ref if (resolution is not None and resolution > 0) else 0.0
    dead_band = max(cfg.deadband_k_se / math.sqrt(n_used), res_sig)             # SE=1/√n
    cap_1 = max(cfg.reset_delta_max_sigma, res_sig)
    cap_cum = cfg.reset_cum_max_sigma_per_cycle

    # ── 자동 불가 그룹 (H5): 분해능 ≥ 1회 상한 → 자동 구간 소멸 ──
    if res_sig >= cfg.reset_delta_max_sigma:
        return RecalcProposal(group_key, "no_change", method, TRIGGER_PERIODIC, n_used,
                              reason="resolution_bound")

    # ── 3구간 판정 (dead_band `<`, cap `>` — 이내=자동) ──
    if delta1 < dead_band:
        return RecalcProposal(group_key, "no_change", method, TRIGGER_PERIODIC, n_used,
                              reason="dead_band")
    if delta1 > cap_1 or cum > cap_cum:
        return _proposal("needs_approval", STATUS_PROPOSED)
    return _proposal("auto_applied", STATUS_AUTO_APPLIED)
