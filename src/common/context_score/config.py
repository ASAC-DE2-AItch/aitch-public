"""Context Score 배점 설정 (B 확정 — 설계 §4 가안 v0 → params flat 키).

스펙: Docs/spec/B4-1_Step3-4_collector_score_스펙플랜.md §2 결정2·§8.
설계 §4는 배점을 "이해용 가안"으로 두고 확정을 B4-1에 위임했다. 이 모듈은 그 확정값을
담는 주입식 설정(dataclass)이며, 축 함수(score_spc·score_tttm)는 cfg만 읽는다(매직넘버 금지).

키 등재 규약(스펙 §8 · `params.yaml:66` 합의): 별도 `context_score:` 섹션 금지 →
`spc.context_score_*` **flat 키**. **k_sigma·tttm 임계는 신규 키 없이 기존 키 재사용** —
`limit_engine.control_limit_k_sigma`(σ 유도) · `spc.tttm_warning`/`spc.tttm_critical`(밴드 경계).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from src.common.config_util import _key, _section   # 부재 시 명시 실패(헌법 6-1)

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "params.yaml"

# A3 그룹 내 집계 — v0 는 max 만 지원한다(`axes/score_ae.py` 구현이 max 하드코딩).
# 키를 남겨두는 이유: grouped_2stage 전환 시 이 키가 집계 방식 확장 지점이 된다(계획서 §2).
# 다만 지원하지 않는 값이 들어와도 조용히 max 로 도는 것은 금지(6-1) → __post_init__ 검증.
_AE_AGGREGATE_SUPPORTED = ("max",)


@dataclass(frozen=True)
class ContextScoreConfig:
    """축 배점(순수 함수 주입용). 필드 = params 명시 매핑(키명 복제 금지 — RecalcConfig 패턴)."""

    rule_base: dict                         # rule_id → 기본점 (축 내 max 대표점, 그룹핑 A1)
    size_per_sigma: float                   # 위반 크기 — 초과 1σ당 가산
    persistence_bonus: float                # e2 — v0 보류(선등재·미사용, 스펙 결정1)
    tttm_warning: float                     # TTTM score warning 경계 (기존 spc.tttm_warning 재사용)
    tttm_critical: float                    # TTTM score critical 경계 (기존 spc.tttm_critical 재사용)
    tttm_warning_pts: float                 # warning 밴드 가점
    tttm_critical_pts: float                # critical 밴드 가점
    reference_suspect_penalty: float        # reference_suspect=true 감점(양수 — e4에서 뺀다)
    transient_discount: float               # m1 — combine(공동)이 소비(과도 ×0.5). 축은 안 씀
    k_sigma: float                          # σ 유도 상수 (기존 limit_engine.control_limit_k_sigma 재사용)
    clip_min: float                         # 최종 점수 하한 (orchestrator clip — 0 하드코딩 금지, B0)
    clip_max: float                         # 최종 점수 상한 (alert.py int 0~100 정합)
    # --- A1 스트림 간 결합 (score_spc) — T14 2026-08-09 ---
    #   기본 0.0 = **현행 max 와 수학적 동치**(거동 무변경). 값을 올리면 수렴 증거가 실린다.
    #   기본값을 둔 이유: 기존 생성자(테스트·하네스)를 안 깨기 위함 + 켜는 것은 G8 결정 사항.
    stream_damping: float = 0.0             # `s₁ + Σ(나머지)×d` — 권장 0.2(T14), 기본은 현행 유지
    # --- e6·e7 AE 축 (score_ae) — D1 확정 2026-07-28: anomaly 입력 = ae_score([0,1] 캘리) ---
    #   z 경로 미채택 근거·실측은 Docs/score_ae_z발행_전환_변경점검_v1.md §8·§9.
    ae_band_edges: tuple = ()               # e6 anomaly 밴드 경계(오름차순, `>=` 포함). calib 앵커 눈금
    ae_band_pts: tuple = ()                 # e6 밴드별 가점. edges와 길이 동일(불일치 = 오설정)
    ae_drift_lo: float = 0.0                # e7 drift 하단 경계(`>` 초과 시 발화)
    ae_drift_hi: float = 0.0                # e7 drift 상단 경계
    ae_pts_drift_lo: float = 0.0            # e7 lo 밴드 가점
    ae_pts_drift_hi: float = 0.0            # e7 hi 밴드 가점
    ae_aggregate: str = "max"               # A3 그룹 내 집계 — max 고정(v0). 합산 금지(이중계상 +72% 실측)
    # --- e8 예측 축 (score_pred) — 기본값 = 축 비활성(null). B 확정 전 기여 0 (기획서 §3.2) ---
    pred_weight: Optional[float] = None     # context_score_pred_weight (null=축 비활성)
    pred_p95_scale: Optional[float] = None  # gap 정규화 스케일. pred_weight set 시 필수
    pred_w_cap: Optional[float] = None      # 방향 가중 상한(포화). pred_weight set 시 필수
    pred_gated_phases: tuple = ("phase_0", "phase_1")  # 가중 0 처리 Phase value

    def __post_init__(self) -> None:
        """AE 밴드 정합 검증 — 길이 일치·오름차순. 오설정은 기동 시점에 명시적 실패(헌법 6-1)."""
        if len(self.ae_band_edges) != len(self.ae_band_pts):
            raise ValueError(
                "context_score_ae_band_edges 와 _band_pts 의 길이가 다릅니다: "
                f"{len(self.ae_band_edges)} vs {len(self.ae_band_pts)}"
            )
        if list(self.ae_band_edges) != sorted(self.ae_band_edges):
            raise ValueError(
                f"context_score_ae_band_edges 는 오름차순이어야 합니다: {self.ae_band_edges}"
            )
        if self.ae_drift_lo > self.ae_drift_hi:
            raise ValueError(
                f"ae_drift_lo({self.ae_drift_lo}) 가 ae_drift_hi({self.ae_drift_hi}) 보다 큽니다"
            )
        if self.ae_aggregate not in _AE_AGGREGATE_SUPPORTED:
            # 코드리뷰 §3-5 — 로드만 하고 미소비였던 키. `complementary` 등 오설정 시 조용히
            # max 로 돌아 "설정한 대로 도는 줄 알았는데 아니었다"가 된다. 기동 시점에 실패시킨다.
            raise ValueError(
                f"context_score_ae_aggregate 는 {_AE_AGGREGATE_SUPPORTED} 만 지원합니다: "
                f"{self.ae_aggregate!r} (score_ae 는 max 집계 — 합산은 이중계상 +72% 실측)"
            )

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "ContextScoreConfig":
        """params.yaml → ContextScoreConfig. flat 키 + 재사용 키(k_sigma·tttm 임계) 명시 매핑."""
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        spc = _section(cfg, "spc", path)
        le = _section(cfg, "limit_engine", path)
        rb = _key(spc, "context_score_rule_base", "spc", path)
        pred_w = _key(spc, "context_score_pred_weight", "spc", path)          # null 허용(축 비활성)
        pred_scale = _key(spc, "context_score_pred_p95_scale", "spc", path)   # null 허용
        pred_cap = _key(spc, "context_score_pred_w_cap", "spc", path)         # null 허용
        gated = _key(spc, "context_score_pred_gated_phases", "spc", path)
        return cls(
            rule_base={str(k): float(v) for k, v in rb.items()},
            size_per_sigma=float(_key(spc, "context_score_size_per_sigma", "spc", path)),
            stream_damping=float(_key(spc, "context_score_stream_damping", "spc", path)),
            persistence_bonus=float(_key(spc, "context_score_persistence_bonus", "spc", path)),
            tttm_warning=float(_key(spc, "tttm_warning", "spc", path)),           # 재사용
            tttm_critical=float(_key(spc, "tttm_critical", "spc", path)),         # 재사용
            tttm_warning_pts=float(_key(spc, "context_score_tttm_warning_pts", "spc", path)),
            tttm_critical_pts=float(_key(spc, "context_score_tttm_critical_pts", "spc", path)),
            reference_suspect_penalty=float(
                _key(spc, "context_score_reference_suspect_penalty", "spc", path)),
            transient_discount=float(_key(spc, "context_score_transient_discount", "spc", path)),
            k_sigma=float(_key(le, "control_limit_k_sigma", "limit_engine", path)),   # 재사용
            clip_min=float(_key(spc, "context_score_clip_min", "spc", path)),         # B0 — combine 선행
            clip_max=float(_key(spc, "context_score_clip_max", "spc", path)),
            ae_band_edges=tuple(float(e) for e in _key(spc, "context_score_ae_band_edges", "spc", path)),
            ae_band_pts=tuple(float(p) for p in _key(spc, "context_score_ae_band_pts", "spc", path)),
            ae_drift_lo=float(_key(spc, "context_score_ae_drift_lo", "spc", path)),
            ae_drift_hi=float(_key(spc, "context_score_ae_drift_hi", "spc", path)),
            ae_pts_drift_lo=float(_key(spc, "context_score_ae_pts_drift_lo", "spc", path)),
            ae_pts_drift_hi=float(_key(spc, "context_score_ae_pts_drift_hi", "spc", path)),
            ae_aggregate=str(_key(spc, "context_score_ae_aggregate", "spc", path)),
            pred_weight=None if pred_w is None else float(pred_w),
            pred_p95_scale=None if pred_scale is None else float(pred_scale),
            pred_w_cap=None if pred_cap is None else float(pred_cap),
            pred_gated_phases=tuple(str(p) for p in gated),
        )
