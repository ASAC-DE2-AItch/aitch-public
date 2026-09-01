"""NelsonEngine — 그룹별 버퍼·리셋·trigger로 위반을 생성하는 상태 엔진 (B3-1).

인스턴스당 단일 스레드. limits·whitelist는 주입받고, Kafka·DB·alert와 분리.
설계: src/agent_b_spc/specs/B3-1_Nelson엔진_설계.md
"""

from __future__ import annotations

import logging
import math
from collections import deque, namedtuple
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from src.common.config_util import _section, _key
from src.agent_b_spc.nelson_rules import RULES, MAX_WINDOW, RuleSpec, Zones

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_time_gap_hours(path: Path = CONFIG_PATH) -> float:
    """config `spc.time_gap_threshold_hours`(Nelson 버퍼 리셋 시간공백 임계, h)를 로드한다.

    키 부재 시 매직넘버 대체 없이 명시적 실패(헌법 6-1). 값 72 = 관측 최대 양성 공백 >53h 근거.
    """
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    spc = _section(cfg, "spc", path)
    return float(_key(spc, "time_gap_threshold_hours", "spc", path))

BufPoint = namedtuple("BufPoint", "wafer_id value timestamp")


@dataclass
class Point:
    """엔진 입력 — wafer 요약점 1개 (상류가 fdc.raw에서 시간순 공급)."""

    group_key: tuple
    wafer_id: str
    timestamp: str        # ISO 8601 UTC ("Z")
    value: float
    pm_count: int
    limit_version: str


class _GroupState:
    """그룹별 상태 — 점 버퍼(최대 MAX_WINDOW) + 리셋 추적 + 규칙별 에피소드."""

    def __init__(self) -> None:
        self.buffer: deque = deque(maxlen=MAX_WINDOW)
        self.last_pm_count: int | None = None
        self.last_limit_version: str | None = None
        self.last_dt: datetime | None = None
        self.episodes: dict[str, bool] = {}


class NelsonEngine:
    """Nelson 판정 엔진. feed(point) → 새로 발행된 위반 목록."""

    def __init__(self, limits: dict, whitelist: set,
                 active_rules=RULES, time_gap_hours: float | None = None) -> None:
        """limits·whitelist 주입. 화이트리스트 각 그룹의 관리선 존재를 __init__에서 점검.

        `time_gap_hours` 미지정 시 config(`spc.time_gap_threshold_hours`)에서 로드(헌법 6-1,
        호출 시점 로드 — import 부작용 없음). 테스트는 값을 주입해 hermetic하게 둘 수 있다.
        """
        self.limits = limits
        self.whitelist = set(whitelist)
        self.active_rules = active_rules
        gap = time_gap_hours if time_gap_hours is not None else load_time_gap_hours()
        self.time_gap_sec = gap * 3600
        self._state: dict[tuple, _GroupState] = {}
        self._skip_logged: set = set()          # 감시 대상 아닌 그룹 skip 로그 (그룹당 1회)
        # 관측 계측 — 드롭은 판정에서 빠지는 데이터라 "조용한 정지"로 보인다. 로그 하나가 유일한
        #   신호면 grep·인코딩에 관측이 좌우되므로(헌법 7장 실측: 원인 규명 40분) 숫자로 노출한다.
        self.stats: dict[str, int] = {"ts_reversal_drops": 0}
        self._warn_missing_limits(self.whitelist)

    def _warn_missing_limits(self, groups: set) -> None:
        """화이트리스트 그룹 중 관리선 없는 것을 경고 (config 갭 조기 발견 — __init__·축소 공용)."""
        for gk in groups:
            if gk not in self.limits:
                logger.warning("화이트리스트 그룹에 관리선 없음(config 갭): %s", gk)

    def _zones(self, gk: tuple) -> Zones:
        """그룹 관리선에서 Zones 계산 — method별 분기 (결정 A 조합②).

        method='quantile'(KEEP* 6그룹, 헌법 1-1 예외 2): N1 경계 = 분위수 상/하한(ucl/lcl,
        DB control_limits의 활성 관리선). 그 외(sigma): center±1/2/3σ. method 키 부재 시
        'sigma'로 폴백(하위 호환 — 기존 주입 dict·테스트 무영향).
        """
        lim = self.limits[gk]
        if lim.get("method") == "quantile":
            return Zones.from_quantile(lim["center"], lim["lcl"], lim["ucl"])
        return Zones.from_limit(lim["center"], lim["sigma"])

    def _active_specs(self, gk: tuple) -> list[RuleSpec]:
        """그룹 method·σ에 따라 평가할 룰 목록 (결정 B-2 필터 / σ=0 가드 통합).

        - method='quantile'(조합②): σ-존 룰 N5~N8 제외(zone∈{1σ,2σ}), N1(분위수 경계)·
          N2·N3·N4 유지. ⚠️ N5~N8 조기경고 상실 — W7 Scorecard 미탐 0건 확인 필수
          (회의안건_요약_B §1).
        - method='sigma'이고 σ≤0(붕괴): σ-존 룰(1/2/3σ) 전부 스킵(헛발동 방지 — 기존 동작).
        - 그 외: 전체 active_rules.
        """
        lim = self.limits[gk]
        if lim.get("method") == "quantile":
            return [s for s in self.active_rules if s.zone not in ("1sigma", "2sigma")]
        if lim["sigma"] <= 0:
            logger.warning("σ=0 그룹 — σ-존 룰 스킵(헛발동 방지): %s", gk)
            return [s for s in self.active_rules if s.zone not in ("1sigma", "2sigma", "3sigma")]
        return list(self.active_rules)

    def feed(self, point: Point) -> list[dict]:
        """점 1개 처리 → 새로 발행된 위반 목록.

        시간 규약: **엄격히 과거**(dt < last_dt)인 점만 드롭한다. **동일 timestamp**
        (dt == last_dt)는 같은 순간의 다중 wafer로 보고 **도착 순서대로 버퍼에 수용**한다
        (드롭하지 않음) — 상류가 wafer를 시간순 공급하는 계약(Point) 전제.
        """
        gk = point.group_key
        if gk not in self.whitelist:
            if gk not in self._skip_logged:      # 회의 결정: baseline 없는 그룹 감시 skip+로그
                logger.info("감시 대상 아님 — skip (그룹당 최초 1회, 오설정 그룹키 감지 겸용): %s", gk)
                self._skip_logged.add(gk)
            return []
        # 입력 방어
        if point.value is None or (isinstance(point.value, float) and math.isnan(point.value)):
            logger.warning("값 결측/NaN — 점 스킵: wafer=%s", point.wafer_id)
            return []
        if gk not in self.limits:
            logger.error("관리선 없는 화이트리스트 그룹 — 스킵: %s", gk)
            return []
        try:
            dt = datetime.fromisoformat(point.timestamp)
        except (ValueError, TypeError):
            logger.warning("timestamp 파싱 실패 — 점 스킵: wafer=%s ts=%s", point.wafer_id, point.timestamp)
            return []
        st = self._state.setdefault(gk, _GroupState())
        if st.last_dt is not None and dt < st.last_dt:
            self.stats["ts_reversal_drops"] += 1
            # 메시지 앞의 ASCII 토큰(`ts_reversal`)은 의도적이다 — 한글만 있으면 콘솔 인코딩이
            #   깨진 파이프에서 grep 이 조용히 0건을 돌려준다(헌법 7장). 원인은 단정하지 않는다
            #   (producer 중복·재전달 등 여러 갈래라 코드가 단정하면 오진을 유도한다).
            logger.warning("ts_reversal drop — timestamp 역전 드롭: wafer=%s", point.wafer_id)
            return []
        # 리셋 판정 (설계 §4)
        if st.last_pm_count is not None and point.pm_count > st.last_pm_count:
            st.buffer.clear()                                  # ① PM
            st.episodes.clear()
        elif st.last_dt is not None and (dt - st.last_dt).total_seconds() > self.time_gap_sec:
            st.buffer.clear()                                  # ③ time-gap
            st.episodes.clear()
        elif st.last_limit_version is not None and point.limit_version != st.last_limit_version:
            self._rebaseline(gk, st)                          # ② limit_version — 버퍼 유지
        # 버퍼 적재
        st.buffer.append(BufPoint(point.wafer_id, point.value, point.timestamp))
        st.last_dt = dt
        st.last_pm_count = point.pm_count
        st.last_limit_version = point.limit_version
        return self._evaluate(gk, st)

    def update_whitelist(self, new_set: set) -> None:
        """감시 대상 런타임 교체. 축소 시 제외 그룹 상태 삭제(stale 버퍼·잔류 방지).

        ⚠️ 컨슈머(feed) 스레드에서만 호출 — 외부 트리거는 커맨드 큐로 전달해 feed 루프가
        적용해야 lock-free 성립 (별도 스레드 직접 호출 금지).
        """
        new_set = set(new_set)
        for gk in list(self._state):
            if gk not in new_set:
                del self._state[gk]           # 제외 그룹 GroupState 삭제
        self.whitelist = new_set
        self._warn_missing_limits(new_set)

    def reset(self, group_key: tuple) -> None:
        """그룹 상태 수동 리셋 (버퍼·에피소드 비움)."""
        self._state.pop(group_key, None)

    def _rebaseline(self, gk: tuple, st: _GroupState) -> None:
        """limit_version 변경 시 zones 재계산 + 에피소드 silent 재기준(발행 없음).

        평가와 동일한 _active_specs 필터를 써 두 경로의 룰셋을 일관되게 유지한다
        (method='quantile'이면 N5~N8 제외, method='sigma'·σ=0이면 σ-존 제외).
        """
        zones = self._zones(gk)
        values = [bp.value for bp in st.buffer]
        for spec in self._active_specs(gk):
            if spec.trigger != "edge":
                continue
            st.episodes[spec.id] = spec.fn(values, zones)

    def _evaluate(self, gk: tuple, st: _GroupState) -> list[dict]:
        """활성 룰(_active_specs)을 버퍼에 평가하고 trigger(N1 level / 나머지 edge)로 위반 생성.

        method·σ에 따른 룰 필터는 _active_specs가 담당한다: method='quantile'이면 σ-존
        룰(N5~N8) 제외(N1은 분위수 경계), method='sigma'·σ≤0(붕괴)이면 σ-존 룰(1/2/3σ)을
        스킵해 zones 붕괴에 의한 헛발동(N1 폭풍)을 막는다 (스펙 §5 defense-in-depth).
        """
        zones = self._zones(gk)
        values = [bp.value for bp in st.buffer]
        emitted: list[dict] = []
        for spec in self._active_specs(gk):
            fired = spec.fn(values, zones)
            if spec.trigger == "level":
                if fired:
                    emitted.append(self._make_violation(gk, st, spec, zones))
            else:  # edge
                prev = st.episodes.get(spec.id, False)
                if fired and not prev:
                    emitted.append(self._make_violation(gk, st, spec, zones))
                st.episodes[spec.id] = fired
        return emitted

    def _make_violation(self, gk: tuple, st: _GroupState, spec: RuleSpec, zones: Zones) -> dict:
        """위반 dict 생성 — spc_violations 매핑 + offending(N5/N6) + limit_basis + run 서술."""
        chamber_id, recipe_id, step, sensor_window, sensor_id = gk
        lim = self.limits[gk]
        latest = st.buffer[-1]
        win = list(st.buffer)[-spec.window:]
        w_range = f"{win[0].wafer_id}..{win[-1].wafer_id}" if len(win) > 1 else latest.wafer_id
        # offending: 귀속 애매한 N5/N6에 채움 (해당 zone 밖 점들)
        offending = []
        if spec.id in ("N5", "N6"):
            edge_p = zones.s2p if spec.id == "N5" else zones.s1p
            edge_m = zones.s2m if spec.id == "N5" else zones.s1m
            offending = [{"wafer_id": bp.wafer_id, "value": bp.value}
                         for bp in win if bp.value > edge_p or bp.value < edge_m]
        return {
            "chamber_id": chamber_id, "recipe_id": recipe_id, "step": step,
            "sensor_window": sensor_window, "sensor_id": sensor_id,
            "rule_id": spec.id, "severity": spec.severity,
            "current_value": latest.value,
            "control_limit_upper": lim["ucl"], "control_limit_lower": lim["lcl"],
            "limit_version": lim["limit_version"],
            "limit_basis": "firm",   # 가한계 대비 예약 (W5 활성)
            "description": f"{spec.id}: {spec.window}점 창 ({w_range})",   # run 범위 = 자기완결
            "wafer_id": latest.wafer_id, "timestamp": latest.timestamp,
            "offending": offending,
            # B4-1 suspect_window 소스 — 이 룰 창의 wafer_id(시간순·창 크기만큼). alert 레벨
            #   합집합·정렬은 publisher(Step5) 몫(Q1 전 위반 합집합 / Q2 룰창 기준).
            "member_wafers": [bp.wafer_id for bp in win],
        }
