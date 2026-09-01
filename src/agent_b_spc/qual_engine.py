"""B5-2: Qual 2지표 (σ-갭 + Nelson N1) 순수 판정 코어.

정비(PM/BM) 후 재인증(Qual)에서 B가 소유하는 2지표를 계산한다:
- **σ-갭** = 센서가 정비 전 지문(스냅샷)에서 몇 σ 이사갔나 (계통 이동)
- **N1**  = 개별 점이 스냅샷 관리선 밖으로 나갔나 (개별 이상치)

스냅샷 주입식 순수 함수 — DB·Kafka 없음(테스트 용이). 최종 조용/요란 종합은 A5-3,
스냅샷 리베이스는 B6-3. 신규 config·스키마 0 (기존 qual 절 소비). 스펙: specs/B5-2_qual지표_스펙플랜.md.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.common.config_util import _section, _key
from src.agent_b_spc.initial_limits import LimitConfig
from src.agent_b_spc.recalc_engine import EPS, sigma_ref_of   # 재사용 (tttm 공유 EPS·robust-σ)
from src.agent_b_spc.nelson_rules import Zones, rule_n1

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

# 판정 리터럴 (모듈 상수)
PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class QualConfig:
    """Qual 판정 파라미터. 산식 파라미터는 LimitConfig 합성(M1 — cfg.limit.k_sigma).

    신규 config 키 0 — 기존 qual 절(pass_sigma_gap_max·pass_nelson_violations_max) + LimitConfig 소비.
    """
    limit: LimitConfig
    threshold: float        # qual.pass_sigma_gap_max (≥ 이면 FAIL)
    nelson_max: int         # qual.pass_nelson_violations_max (초과면 FAIL)

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "QualConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        q = _section(cfg, "qual", path)
        return cls(
            limit=LimitConfig.load(path),
            threshold=float(_key(q, "pass_sigma_gap_max", "qual", path)),
            nelson_max=int(_key(q, "pass_nelson_violations_max", "qual", path)),
        )


@dataclass
class QualBIndicators:
    """B5-2 반환 계약 (asdict → snake_case JSON). 최종 조용/요란 verdict 없음(A5-3 종합)."""
    chamber_id: str
    qual_id: str
    snapshot_id: str
    evaluated_at: str       # ISO8601 UTC
    sigma_gap: dict         # {verdict, max_gap_sigma, top_gap_sensor, top_gap_group, threshold, per_group}
    nelson: dict            # {verdict, violation_count, violations}


def _aggregate(pts: list[float], method: str) -> float:
    """A1′ 2-track 집계: σ그룹=mean(정밀 X̄) / 분위수그룹=median(치우침 정합)."""
    return statistics.median(pts) if method == "quantile" else statistics.mean(pts)


def evaluate_qual(qual_indicators, snapshot, cfg: QualConfig, *,
                  chamber_id: str, qual_id: str, snapshot_id: str) -> QualBIndicators:
    """주입된 Qual 5장 요약지표 + 스냅샷 → σ-갭·N1 2지표. 순수 함수.

    스냅샷에 든 건강센서 그룹만 순회. 그룹당 σ_ref 1회 계산, σ-갭·Nelson이 공유(H2 대칭 —
    유효 0장·붕괴밴드 skip 집합 동일). 유효 그룹 0이면 거짓 PASS 대신 UNKNOWN.
    """
    k = cfg.limit.k_sigma
    per_group: list[dict] = []
    evaluable: list[tuple] = []          # [(gk, pts, snap)] — σ-갭·Nelson 공유 (빈 pts·sref≤EPS 제외)

    for gk in snapshot:
        snap = snapshot[gk]
        method = snap["method"]
        pts = list(qual_indicators.get(gk, []))
        if not pts:                                          # C-슬림: 유효 0장 skip
            per_group.append(_pg(gk, method, 0, None, "no_data"))
            continue
        sref = sigma_ref_of(snap, k)
        if sref <= EPS:                                      # 붕괴 밴드(이산센서 ucl≈lcl) → 분모 폭발 차단
            per_group.append(_pg(gk, method, len(pts), None, "degenerate"))
            continue
        gap = abs(_aggregate(pts, method) - snap["center"]) / sref   # center: σ=mean / 분위수=median (M2)
        per_group.append(_pg(gk, method, len(pts), gap, None))
        evaluable.append((gk, pts, snap))

    return QualBIndicators(
        chamber_id=chamber_id, qual_id=qual_id, snapshot_id=snapshot_id,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        sigma_gap=_sigma_gap_verdict(per_group, cfg.threshold),
        nelson=_nelson_verdict(evaluable, cfg),
    )


def _pg(gk, method, n_used, gap, skipped) -> dict:
    """per_group 출력 항목 (드릴다운·Scorecard)."""
    return {"group": list(gk), "gap_sigma": gap, "method": method,
            "n_used": n_used, "skipped": skipped}


def _sigma_gap_verdict(per_group: list[dict], threshold: float) -> dict:
    """최대 갭 그룹으로 PASS/FAIL/UNKNOWN. 유효 그룹 0이면 UNKNOWN(거짓 PASS 금지)."""
    valid = [g for g in per_group if g["skipped"] is None]
    base = {"threshold": threshold, "per_group": per_group}
    if not valid:
        return {"verdict": UNKNOWN, "max_gap_sigma": None,
                "top_gap_sensor": None, "top_gap_group": None, **base}
    top = max(valid, key=lambda g: g["gap_sigma"])
    return {
        "verdict": FAIL if top["gap_sigma"] >= threshold else PASS,   # ≥ 포함
        "max_gap_sigma": top["gap_sigma"],
        "top_gap_sensor": tuple(top["group"])[-1],                    # 5-tuple 마지막 = sensor_id
        "top_gap_group": top["group"],
        **base,
    }


def _zones_of(snap: dict) -> Zones:
    """스냅샷 → N1 판정용 Zones (2-track). 분위수는 q밴드(lcl/ucl)로 경계."""
    if snap["method"] == "quantile":
        return Zones.from_quantile(snap["center"], snap["lcl"], snap["ucl"])
    return Zones.from_limit(snap["center"], snap["sigma"])


def _nelson_verdict(evaluable, cfg: QualConfig) -> dict:
    """Nelson N1 — 스냅샷 관리선 밖 개별 점 검출. 5장이라 N1만(N2~N8 발동불가/얄팍).

    H2: evaluable은 σ-갭과 동일 skip 집합(빈 pts·sref≤EPS 제외) → 붕괴 밴드 N1 폭풍 차단.
    M4: 유효 평가 그룹 0건이면 UNKNOWN(거짓 PASS 금지, σ-갭과 대칭). 임계=config(nelson_max).
    """
    violations: list[dict] = []
    for gk, pts, snap in evaluable:
        zones = _zones_of(snap)
        for p in pts:
            if rule_n1([p], zones):                       # H1: rule_n1은 list 인자(values[-1])
                violations.append({"group": list(gk), "rule": "N1", "value": p})
    if not evaluable:
        verdict = UNKNOWN
    else:
        verdict = PASS if len(violations) <= cfg.nelson_max else FAIL
    return {"verdict": verdict, "violation_count": len(violations), "violations": violations}
