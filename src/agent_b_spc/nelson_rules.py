"""Nelson Rule N1~N8 — 순수 판정 함수 + 레지스트리 (상태 없음).

각 rule_nX(values, zones)는 values(오래된→최신, 최신이 마지막)의 최신 점에서
끝나는 패턴 존재 시 True를 반환한다. 점이 부족하면 False. 설계:
src/agent_b_spc/specs/B3-1_Nelson엔진_설계.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Zones:
    """관리선 경계값 묶음 (center + ±1/2/3σ). 룰은 이 경계만 본다 (결정 A 분리)."""

    center: float
    s1p: float
    s1m: float
    s2p: float
    s2m: float
    s3p: float
    s3m: float

    @classmethod
    def from_limit(cls, center: float, sigma: float) -> "Zones":
        """center·sigma에서 ±1/2/3σ 경계를 계산한다. Nelson zone은 1/2/3σ 고정(K 무관).

        관리한계 배수 K_SIGMA는 B2-1의 ucl/lcl에만 반영되며 여기 zone과는 무관하다.
        (결정 A 분위수 채택 시 이 메서드만 분위수 경계로 교체.)
        """
        return cls(
            center=center,
            s1p=center + sigma, s1m=center - sigma,
            s2p=center + 2 * sigma, s2m=center - 2 * sigma,
            s3p=center + 3 * sigma, s3m=center - 3 * sigma,
        )

    @classmethod
    def from_quantile(cls, center: float, lcl: float, ucl: float) -> "Zones":
        """분위수 관리선 → N1 판정용 경계 (s3m/s3p = lcl/ucl). 결정 A 조합②(헌법 1-1 예외 2).

        KEEP* 비대칭 그룹은 ±3σ 대신 분위수 상/하한(q_low/q_high로 산출된 lcl/ucl)을 N1
        경계로 쓴다. σ-존(±1/2σ)은 이 그룹에서 미사용(N5~N8 제외)이라 NaN으로 둔다 — 혹시
        σ-존 룰이 평가돼도 NaN 비교는 항상 False라 헛발동하지 않는다(방어).
        """
        nan = float("nan")
        return cls(
            center=center,
            s1p=nan, s1m=nan,
            s2p=nan, s2m=nan,
            s3p=ucl, s3m=lcl,
        )


def rule_n1(values: list[float], zones: Zones) -> bool:
    """N1: 최신 1점이 ±3σ 밖(strict) 이면 True."""
    if not values:
        return False
    v = values[-1]
    return v > zones.s3p or v < zones.s3m


def rule_n2(values: list[float], zones: Zones) -> bool:
    """N2: 최근 9점이 전부 center 위 또는 전부 아래(strict). ==center는 단절."""
    if len(values) < 9:
        return False
    w = values[-9:]
    return all(v > zones.center for v in w) or all(v < zones.center for v in w)


def rule_n3(values: list[float], zones: Zones) -> bool:
    """N3: 최근 6점이 strict 단조 증가 또는 감소. 동점(==)이면 단절."""
    if len(values) < 6:
        return False
    w = values[-6:]
    up = all(w[i] > w[i - 1] for i in range(1, 6))
    down = all(w[i] < w[i - 1] for i in range(1, 6))
    return up or down


def rule_n4(values: list[float], zones: Zones) -> bool:
    """N4: 최근 14점의 방향이 매번 교대(지그재그). delta 0이면 교대 깨짐."""
    if len(values) < 14:
        return False
    w = values[-14:]
    diffs = [w[i] - w[i - 1] for i in range(1, 14)]   # 13개
    if any(d == 0 for d in diffs):
        return False
    return all((diffs[i] > 0) != (diffs[i - 1] > 0) for i in range(1, 13))


def rule_n5(values: list[float], zones: Zones) -> bool:
    """N5: 최근 3점 중 2점 이상이 +2σ 밖(또는 2점 이상이 −2σ 밖). 무가드."""
    if len(values) < 3:
        return False
    w = values[-3:]
    above = sum(1 for v in w if v > zones.s2p)
    below = sum(1 for v in w if v < zones.s2m)
    return above >= 2 or below >= 2


def rule_n6(values: list[float], zones: Zones) -> bool:
    """N6: 최근 5점 중 4점 이상이 +1σ 밖(또는 4점 이상이 −1σ 밖)."""
    if len(values) < 5:
        return False
    w = values[-5:]
    above = sum(1 for v in w if v > zones.s1p)
    below = sum(1 for v in w if v < zones.s1m)
    return above >= 4 or below >= 4


def rule_n7(values: list[float], zones: Zones) -> bool:
    """N7: 최근 15점이 전부 ±1σ 이내(inclusive — 경계값 포함). 층화 감지."""
    if len(values) < 15:
        return False
    w = values[-15:]
    return all(zones.s1m <= v <= zones.s1p for v in w)


def rule_n8(values: list[float], zones: Zones) -> bool:
    """N8: 최근 8점이 전부 ±1σ 밖(strict) 이고 위·아래 양쪽에 분포(혼합)."""
    if len(values) < 8:
        return False
    w = values[-8:]
    if not all(v > zones.s1p or v < zones.s1m for v in w):   # 전부 밖(strict)
        return False
    has_above = any(v > zones.s1p for v in w)
    has_below = any(v < zones.s1m for v in w)
    return has_above and has_below


@dataclass(frozen=True)
class RuleSpec:
    """룰 메타 — 엔진이 룰을 순회·필터하는 단위 (결정 B는 이 리스트 필터로 처리)."""

    id: str
    fn: Callable[[list, Zones], bool]
    window: int
    severity: str          # CRITICAL / WARNING / INFO
    zone: str              # 3sigma / center / none / 2sigma / 1sigma
    trigger: str           # level(N1) / edge(N2~N8)


RULES: list[RuleSpec] = [
    RuleSpec("N1", rule_n1, 1, "CRITICAL", "3sigma", "level"),
    RuleSpec("N2", rule_n2, 9, "WARNING", "center", "edge"),
    RuleSpec("N3", rule_n3, 6, "WARNING", "none", "edge"),
    RuleSpec("N4", rule_n4, 14, "WARNING", "none", "edge"),
    RuleSpec("N5", rule_n5, 3, "WARNING", "2sigma", "edge"),
    RuleSpec("N6", rule_n6, 5, "INFO", "1sigma", "edge"),
    RuleSpec("N7", rule_n7, 15, "INFO", "1sigma", "edge"),
    RuleSpec("N8", rule_n8, 8, "WARNING", "1sigma", "edge"),
]

MAX_WINDOW: int = max(r.window for r in RULES)   # 15 (N7)
