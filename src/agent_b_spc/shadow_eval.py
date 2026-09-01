r"""실력치 재설정 섀도 평가 (B5-1b) — 승인 필요 재설정을 **적용 전** replay로 채점.

승인이 필요한 관리선 재설정(이상 대응·Qual 후 재산정·상한 초과)을 실제 적용하기 전에,
과거 wafer를 구/신 관리선으로 각각 replay해서 **① 오탐(가성알람) 감소** 와 **② 미탐** 을
채점하는 순수 함수다. 상한 내 정기 자동 재설정은 섀도 없이 즉시 적용이라 대상 밖(트리거는
호출자 — 합의안 A5).

경계: **순수 점수 계산만.** 트리거 게이팅·DB 영속·발행·실제 적용·사후 검증(지연 라벨)은
이월(호출자·승인 워크플로). side-effect 0 — 과거 데이터만 읽는다(헌법 1-3).

**미탐(진짜 이상 놓침)은 채점하지 않는다**(2026-07-22 확정) — 판정 기준(정답 소스)이
애매·불확실해 확실한 오탐 감소만 판정한다. 미탐 안전성은 재설정 캡(B4-3 ≤1σ) + 사후
검증(Qual·지연 라벨·섀도 감시)이 담당한다.

⚠️ **"미탐 가드" 용어 구분**(이름 유사·메커니즘 상이 — PR#33 리뷰): 여기 "미탐 미채점"(② 재설정
승인 전 replay 채점, B5-1b)은 헌법 1-1 예외2의 **"Scorecard 미탐 0건" 가드**(① W7 — KEEP\*
분위수 그룹의 N5~N8 제외 안전판, 위반 시 ±3σ 롤백)와 **별개**다. ①=Nelson 룰 축소 안전판 / ②=재설정 안전성 채점.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.common.config_util import _key, _section
from src.agent_b_spc.nelson_engine import NelsonEngine, Point

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


@dataclass(frozen=True)
class ShadowConfig:
    """섀도 평가 임계 (A5/A6). 신규 config 0 — 기존 키를 속성명으로 명시 매핑한다.

    키는 **2개 섹션**에 흩어져 있다(네임스페이스 주의): shadow 2키는 `limit_engine:`,
    `time_gap_threshold_hours`는 `spc:`. config 키명을 속성명으로 복제하지 않고 명시
    매핑해 AttributeError를 막는다(RecalcConfig 패턴). 미탐 미채점(§4)이라
    `shadow_pass_missed_detection_max`는 로드하지 않는다(params 키 자체는 존치).
    """

    reduction_min_pct: float                # A6 — 오탐 감소 최소 (통과 기준)
    n_wafers: int                           # A5 — replay 표본 수 (호출자가 집계에 사용)
    time_gap_hours: float                   # Nelson 버퍼 리셋 시간공백 임계 (replay hermetic)

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "ShadowConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        le = _section(cfg, "limit_engine", path)
        spc = _section(cfg, "spc", path)
        return cls(
            reduction_min_pct=float(
                _key(le, "shadow_pass_false_alarm_reduction_min_pct", "limit_engine", path)),
            n_wafers=int(_key(le, "shadow_eval_wafers", "limit_engine", path)),
            time_gap_hours=float(_key(spc, "time_gap_threshold_hours", "spc", path)),
        )


@dataclass(frozen=True)
class VerifyConfig:
    """verifying(B6-3-b V) 임계 — 새 firm 관리선 forward 오탐 품질.

    합격선은 **절대 오탐율 상한**이다(shadow의 감소율이 아님 — 새 firm 관리선은 비교 대상이
    없다). 키는 2섹션: 상한은 `limit_engine:`(신규 B 키), time_gap은 `spc:`. forward N은
    수집 시점을 c가 정하므로(shadow_eval_wafers) 코어 채점엔 불요.
    """

    false_alarm_max_pct: float              # 절대 오탐율 상한 (PASS 기준)
    time_gap_hours: float                   # _replay_flagged 재사용(Nelson 버퍼 리셋)

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "VerifyConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        le = _section(cfg, "limit_engine", path)
        spc = _section(cfg, "spc", path)
        return cls(
            false_alarm_max_pct=float(
                _key(le, "verify_forward_false_alarm_max_pct", "limit_engine", path)),
            time_gap_hours=float(_key(spc, "time_gap_threshold_hours", "spc", path)),
        )


def _replay_flagged(limits: dict, points: list[Point], group_key: tuple,
                    cfg: ShadowConfig) -> int:
    """관리선 `limits`로 순차 replay → 위반 낸 distinct wafer 수.

    Nelson은 stateful(순차 룰: N2 9연속 등)이라 한 엔진에 시간순 feed한다. 구/신은 각각
    **새 NelsonEngine**을 써 상태를 격리한다. 단일 그룹(whitelist={group_key})이라 wafer당
    1 point → set(위반 wafer_id) 크기 = 위반 wafer 수. `feed`는 항상 list 반환([]=위반없음).
    """
    eng = NelsonEngine(limits, whitelist={group_key}, time_gap_hours=cfg.time_gap_hours)
    return len({p.wafer_id for p in points if eng.feed(p)})


def _verdict(reduction: float, cfg: ShadowConfig, measurable: bool) -> str:
    """지표 → 파생 verdict (오탐만 — A6: 오탐 감소 ≥ 임계 → PASS). 미탐 미채점(§4)."""
    if not measurable:                      # 구=0 → 감소율 정의 불가 → 판정 유보
        return "UNKNOWN"
    return "PASS" if reduction >= cfg.reduction_min_pct else "FAIL"


def evaluate_shadow(old_limits: dict, new_limits: dict, points: list[Point],
                    cfg: ShadowConfig, *, group_key: tuple) -> dict:
    """구/신 관리선으로 replay → 오탐 감소율·verdict (단일 그룹, 순수).

    `new_limits`는 호출자가 current row에 proposal delta를 오버레이한 것(limit_version·
    method는 current 승계 — Nelson `_make_violation`이 limit_version을 무조건 읽으므로 누락
    시 KeyError). `points`는 시간순 replay 표본(방어적으로 재정렬). **미탐은 채점하지
    않는다**(§4 — 판정 기준 애매, 안전성은 재설정 캡+사후 검증이 담당).
    """
    points = sorted(points, key=lambda p: p.timestamp)   # 방어 정렬 (ISO str=시간순, stable)
    old_flagged = _replay_flagged(old_limits, points, group_key, cfg)
    new_flagged = _replay_flagged(new_limits, points, group_key, cfg)
    measurable = old_flagged > 0
    reduction = 0.0 if not measurable else (old_flagged - new_flagged) / old_flagged * 100
    verdict = _verdict(reduction, cfg, measurable)
    logger.info("섀도 평가 %s: 구 %d→신 %d 위반 wafer (감소 %.1f%%) → %s",
                group_key, old_flagged, new_flagged, reduction, verdict)
    return {
        "group_key": group_key,
        "n_wafers": len(points),
        "false_alarm_reduction_pct": round(reduction, 1),
        "old_violating_wafers": old_flagged,
        "new_violating_wafers": new_flagged,
        "verdict": verdict,                  # PASS / FAIL / UNKNOWN
    }


def verify_forward(new_firm_limits: dict, forward_points: list[Point],
                   cfg: VerifyConfig, *, group_key: tuple) -> dict:
    """새 firm 관리선을 forward N장으로 채점 — 절대 오탐율 ≤ 상한이면 PASS (B6-3-b V).

    shadow_eval의 과거 replay와 반대 방향(사후 forward). 승인·적용된 firm 관리선이 실가동
    데이터에서 과하게 알람 내지 않는지 검증한다. **비교 대상(구 관리선)이 없어 감소율이 아니라
    절대 오탐율**로 판정한다. `forward_points`는 c가 firm 후 실시간으로 모은 집합이고, b는
    **주어진 집합을 채점만** 한다(수집·호출 시점은 c 비동기). 0장이면 UNKNOWN(판정 유보).
    """
    if group_key not in new_firm_limits:             # 관리선 부재 → 채점 불가(거짓 PASS 방지)
        return {"group_key": group_key, "n_wafers": 0, "flagged_wafers": 0,
                "false_alarm_pct": 0.0, "verdict": "UNKNOWN"}
    # 분자/분모 universe 정합 — 채점 가능한 point만(그룹 일치 + 유효값). NaN/None은
    # _replay_flagged가 분자서 스킵하므로 분모(n_wafers)에서도 제외해야 오탐율이 안 희석됨.
    valid = [p for p in forward_points
             if p.group_key == group_key and p.value is not None and not math.isnan(p.value)]
    valid.sort(key=lambda p: p.timestamp)            # 방어 정렬(stateful 안전)
    flagged = _replay_flagged(new_firm_limits, valid, group_key, cfg)
    total = len({p.wafer_id for p in valid})
    if total == 0:
        pct, verdict = 0.0, "UNKNOWN"                # 채점 불가(유효 forward 0장)
    else:
        pct = flagged / total * 100
        verdict = "PASS" if pct <= cfg.false_alarm_max_pct else "FAIL"
    logger.info("verify_forward %s: forward %d장 중 %d 위반 (오탐 %.1f%%, 상한 %.1f) → %s",
                group_key, total, flagged, pct, cfg.false_alarm_max_pct, verdict)
    return {
        "group_key": group_key,
        "n_wafers": total,
        "flagged_wafers": flagged,
        "false_alarm_pct": round(pct, 1),
        "verdict": verdict,                          # PASS / FAIL / UNKNOWN
    }
