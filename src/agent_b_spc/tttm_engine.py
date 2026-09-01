"""TTTMEngine — fleet(형제 챔버) 대비 챔버 이탈을 판정하는 상태 엔진 (B4-2 코어).

Nelson("자기 관리선 초과")과 병렬 감지 채널. 입력은 fdc.raw뿐(C65·alert 불요)이라
D0 독립. 두 질문을 다른 기준으로 분리 판정한다:
  · 개별 outlier(score)      : 라이브 fleet median 대비  (이 챔버가 지금 형제와 다른가)
  · 집단 이동(reference_suspect): 고정 baseline center 대비 (다수가 같이 움직여 기준 오염됐나)

인스턴스당 단일 스레드. limits·whitelist·config는 주입받고 Kafka·DB·alert와 분리된다
(발행은 B4-1, DB writer는 ③, 가한계 제외는 B6-3 — §9 이월).
설계: src/agent_b_spc/specs/B4-2_TTTM엔진_설계.md (v5)
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml

from src.common.config_util import _section, _key
from src.agent_b_spc.nelson_engine import Point   # 6필드 Point 재사용 (라이브 fan-out 공유)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

# 명명 상수 (헌법 6-1 — 매직넘버 금지)
EPS = 1e-9                       # 0 나눗셈/퇴화 관리선 가드 epsilon (감시 45 센서 center·σ는 O(0.1)+)
MIN_FLEET_CHAMBERS = 2           # fleet median 최소 표본 (미만이면 판정 보류)
# fdc.alert.tttm에 실리는 필드 (계약 §3). reference_id/chamber_id는 DB 전용(#7 — 헌법 2-2).
#   suspect_sensors(2026-08-05 decouple): 역방향 실제 공통이동 센서 C코드 목록 — DB+alert 둘 다.
ALERT_FIELDS = frozenset(
    {"reference", "score", "top_gap_sensor", "gap_pct", "reference_suspect", "suspect_sensors"})
Q_K_TOL = 0.01                   # 분위수↔k 정합 허용오차 (#12 — σ_ref=(ucl−lcl)/2k 역산 가드)


def _check_q_k(q_high: float, k_sigma: float) -> None:
    """분위수 q_high의 Φ⁻¹이 k_sigma와 일치하는지 검증 (#12 — stdlib NormalDist).

    σ_ref=(ucl−lcl)/(2·k)는 밴드가 ±k·σ 등가일 때만 σ를 복원한다. q_low/q_high와
    control_limit_k_sigma는 별개 config라, 누가 분위수만 ±2.5σ로 바꾸면 σ_ref가 조용히
    틀어진다 → 로드 시 즉시 실패시킨다.
    """
    z = statistics.NormalDist().inv_cdf(q_high)
    if abs(z - k_sigma) > Q_K_TOL:
        raise ValueError(
            f"분위수 q_high={q_high}의 Φ⁻¹={z:.4f} ≠ k_sigma={k_sigma} — "
            f"σ_ref=(ucl−lcl)/2k 역산이 틀어짐(#12). q_low/q_high 또는 k 재확인.")


def load_tttm_config(path: Path = CONFIG_PATH) -> dict:
    """config에서 TTTM 파라미터를 로드한다 (헌법 6-1 — 키 부재 시 명시적 실패).

    window·min_fill는 `spc:`절 2키(B4-2 등재), warning·critical·reverse_min은 기존 B1/B3 키,
    k_sigma는 `limit_engine.control_limit_k_sigma`(분위수 σ_ref 역산용, §3-4)를 재사용한다.
    로드 시 분위수↔k 정합(#12)을 검증한다. 호출 시점 로드 — import 부작용 없음.
    """
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    spc = _section(cfg, "spc", path)
    le = _section(cfg, "limit_engine", path)
    k_sigma = float(_key(le, "control_limit_k_sigma", "limit_engine", path))
    _check_q_k(float(_key(spc, "q_high", "spc", path)), k_sigma)      # #12 정합 가드
    return {
        "window": int(_key(spc, "tttm_window_wafers", "spc", path)),
        "min_fill": int(_key(spc, "tttm_window_min_fill", "spc", path)),
        "warning": float(_key(spc, "tttm_warning", "spc", path)),
        "critical": float(_key(spc, "tttm_critical", "spc", path)),
        "reverse_min": int(_key(spc, "reverse_rule_min_chambers", "spc", path)),
        "k_sigma": k_sigma,
    }


class GroupState:
    """그룹별 상태 — rolling window(최근 wafer 중심점) + 레짐 flush 추적."""

    def __init__(self, maxlen: int) -> None:
        self.window: deque = deque(maxlen=maxlen)
        self.center_live: float | None = None      # window median (warmup 전 None)
        self.last_pm_count: int | None = None
        self.last_limit_version: str | None = None
        self.last_dt: datetime | None = None


class TTTMEngine:
    """TTTM 판정 엔진. feed(point) → 챔버 판정 (rollup은 chamber_rollup)."""

    def __init__(self, limits: dict, whitelist: set, *,
                 window: int | None = None, min_fill: int | None = None,
                 warning: float | None = None, critical: float | None = None,
                 reverse_min: int | None = None, k_sigma: float | None = None,
                 excluded: set = frozenset()) -> None:
        """limits·whitelist 주입. config 값은 None이면 params.yaml에서 로드, 지정 시 주입(hermetic).

        `k_sigma`는 분위수 σ_ref 역산(§3-4)에 필수라 명시 인자로 둔다(Nelson time_gap 패턴).
        """
        self.limits = limits
        self.whitelist = set(whitelist)
        if None in (window, min_fill, warning, critical, reverse_min, k_sigma):
            cfg = load_tttm_config()
        else:
            cfg = None
        self.window = window if window is not None else cfg["window"]
        self.min_fill = min_fill if min_fill is not None else cfg["min_fill"]
        self.warning = warning if warning is not None else cfg["warning"]
        self.critical = critical if critical is not None else cfg["critical"]
        self.reverse_min = reverse_min if reverse_min is not None else cfg["reverse_min"]
        self.k_sigma = k_sigma if k_sigma is not None else cfg["k_sigma"]
        self.excluded = set(excluded)
        self._state: dict[tuple, GroupState] = {}
        self._baseline: dict[tuple, dict] = {}          # Step7 D6-a: 레짐 수립 행 {center,sigma_ref,limit_version}
        self._fleet_median: dict[tuple, float] = {}     # fleet_key → median (Task 2)
        self._suspect_sensors: set[tuple] = set()        # 역방향 발동 fleet_key (Task 3)
        self._skip_logged: set = set()                   # 감시 대상 아닌 그룹 skip 로그 (그룹당 1회)
        # 관측 계측 — Nelson과 동일 계약. 드롭은 판정에서 빠지는 데이터라 "조용한 정지"로 보이고,
        #   로그 하나가 유일한 신호면 grep·인코딩에 관측이 좌우된다(헌법 7장 실측: 원인 규명 40분).
        self.stats: dict[str, int] = {"ts_reversal_drops": 0}
        self._warn_missing_limits(self.whitelist)

    def _warn_missing_limits(self, groups: set) -> None:
        """화이트리스트 그룹 중 관리선 없는 것을 경고 (config 갭 조기 발견)."""
        for gk in groups:
            if gk not in self.limits:
                logger.warning("화이트리스트 그룹에 관리선 없음(config 갭): %s", gk)

    def set_excluded(self, chamber_ids: set) -> None:
        """가한계(provisional) 챔버 제외 훅 (B6-3 seam). 제외 챔버는 fleet median·역방향·score에서 빠짐."""
        self.excluded = set(chamber_ids)

    def set_baseline(self, baseline: dict) -> None:
        """레짐 수립 행 기준점 주입 (Step7 D6-a). `{gk: {center, sigma_ref, limit_version}}`.

        맵에 있는 그룹은 `sigma_ref`·`center_ref`·`reference_id`가 이 값(레짐 수립 행)을 따라가
        정기 recalc(periodic auto)·provisional에 안 흔들린다(B4-2 결정 1 — score 분모 고정).
        맵을 비우면 전량 fallback(활성 행) = 현행 동작. Step7이 startup·리로드마다 전량 재주입한다.
        """
        self._baseline = baseline

    def center_ref(self, gk: tuple) -> float:
        """역방향 룰 앵커 center — baseline 맵(레짐 행) 우선, 미주입이면 활성 행(fallback·D6-a)."""
        ref = self._baseline.get(gk)
        return ref["center"] if ref is not None else self.limits[gk]["center"]

    def _reference_id(self, gk: tuple) -> str:
        """σ_ref 출처 limit_version(=reference_id·B4-2:178) — baseline 우선, 분모와 정합(D6-a)."""
        ref = self._baseline.get(gk)
        return ref["limit_version"] if ref is not None else self.limits[gk]["limit_version"]

    def sigma_ref(self, gk: tuple) -> float:
        """레짐 수립 행의 σ_ref. baseline 맵(Step7 D6-a) 우선 — 미주입 시 활성 행에서 계산(fallback).

        baseline은 `reset_ref`가 이미 robust σ까지 산출한 값이라 그대로 쓴다. fallback 경로에서
        method='quantile'(KEEP*)는 분위수 밴드에서 robust σ 역산: (ucl − lcl)/(2·k_sigma) —
        분위수 행의 `sigma` 컬럼은 참고 통계라 판정에 못 쓴다(#12는 Task 4 assert로 보증).
        """
        ref = self._baseline.get(gk)
        if ref is not None:
            return ref["sigma_ref"]
        lim = self.limits[gk]
        if lim.get("method") == "quantile":
            return (lim["ucl"] - lim["lcl"]) / (2.0 * self.k_sigma)
        return lim["sigma"]

    def _warm(self, st: GroupState) -> bool:
        """warmup 완료 여부 (window ≥ min_fill) — fleet median 표본·rollup 자격 기준."""
        return len(st.window) >= self.min_fill

    @staticmethod
    def _gap_pct(center: float, fleet_median: float) -> float | None:
        """표시용 signed % 갭. |fleet_median|≤EPS면 NULL (0 나눗셈 가드).

        #6: 음수값 센서(fleet_median<0)에서 부호가 뒤집히지 않도록 `/|fleet_median|`로 나눠
        부호가 항상 (center−median) 방향을 따르게 한다 (표시용, Context Score 미소비).
        """
        if abs(fleet_median) <= EPS:
            return None
        return (center - fleet_median) / abs(fleet_median) * 100.0

    def feed(self, point: Point) -> list[dict]:
        """점 1개 처리 → 판정(코어는 Task 2·3에서 완성). 가드 통과 시 window 갱신·center_live 산출.

        시간 규약(Nelson 정합): 엄격 과거(dt < last_dt) 드롭, 동일 timestamp 허용, NaN/None skip.
        레짐 flush(3-C): pm_count 증가 또는 limit_version 변경 시 window 비움(새 운전점 오염 차단).
        """
        gk = point.group_key
        ch = gk[0]
        if gk not in self.whitelist:
            if gk not in self._skip_logged:      # baseline 없는 그룹 감시 skip+로그 (그룹당 최초 1회)
                logger.info("감시 대상 아님 — skip (그룹당 최초 1회, 오설정 그룹키 감지 겸용): %s", gk)
                self._skip_logged.add(gk)
            return []
        if gk not in self.limits:
            logger.error("관리선 없는 화이트리스트 그룹 — 스킵: %s", gk)
            return []
        if ch in self.excluded:                  # pin ⑥ 훅 — 제외 챔버는 상태도 안 만듦
            return []
        value = point.value
        if value is None or (isinstance(value, float) and math.isnan(value)):
            logger.warning("값 결측/NaN — 점 스킵: wafer=%s", point.wafer_id)
            return []
        sigma_ref = self.sigma_ref(gk)
        if sigma_ref <= EPS:                     # #2 퇴화 관리선(zero_sigma·밴드 붕괴) skip
            logger.warning("σ_ref≤EPS(퇴화 관리선) — 점 스킵: %s", gk)
            return []
        try:
            dt = datetime.fromisoformat(point.timestamp)
        except (ValueError, TypeError):
            logger.warning("timestamp 파싱 실패 — 점 스킵: wafer=%s ts=%s", point.wafer_id, point.timestamp)
            return []
        st = self._state.setdefault(gk, GroupState(self.window))
        if st.last_dt is not None and dt < st.last_dt:
            self.stats["ts_reversal_drops"] += 1
            # ASCII 토큰(`ts_reversal`) 의도적 — 한글만이면 인코딩 깨진 파이프에서 grep 이
            #   조용히 0건을 낸다(헌법 7장). 원인 단정 금지(producer 중복·재전달 등 다갈래).
            logger.warning("ts_reversal drop — timestamp 역전 드롭: wafer=%s", point.wafer_id)
            return []
        # 3-C 레짐 flush (#2 None-가드 = 첫 wafer 크래시 방지). pm_count 증가는 항상 flush(새 PM=새 레짐).
        #   limit_version 변경은 **리셋 지점(is_reset_point)일 때만** flush — 정기 리캘리(periodic auto)는
        #   같은 운전점에서 자만 다시 잰 것이라 옛 점이 유효하다(D6-b·Nelson "같은 자리 버퍼 유지").
        #   is_reset_point 미탑재(구 limits) 시 default True = 보수적 flush(현행 동작).
        version_reset = (point.limit_version != st.last_limit_version
                         and self.limits[gk].get("is_reset_point", True))
        if st.last_pm_count is not None and (point.pm_count > st.last_pm_count or version_reset):
            st.window.clear()
        st.last_pm_count = point.pm_count
        st.last_limit_version = point.limit_version
        st.last_dt = dt
        st.window.append(value)

        if len(st.window) < self.min_fill:       # 3-B warmup
            return []
        st.center_live = statistics.median(st.window)   # 3-A window median

        # 개별 outlier (score) — 라이브 fleet median 대비 (§3-2)
        fk = gk[1:]
        members = {g[0]: self._state[g].center_live
                   for g in self._state
                   if g[1:] == fk and self._warm(self._state[g])
                   and g[0] not in self.excluded and self.sigma_ref(g) > EPS}
        if len(members) < MIN_FLEET_CHAMBERS:
            self._suspect_sensors.discard(fk)            # #10 fleet<MIN → discard (래칭 방지)
            self._fleet_median.pop(fk, None)             # 캐시 stale 방지 — fleet 붕괴 후 rollup이 None 내도록
            return []
        self._fleet_median[fk] = statistics.median(members.values())

        # 집단 이동 (reference_suspect) — 각 챔버 자기 baseline(center_ref) 대비 (§3-3)
        suspect = self._reverse_rule(fk, members)
        gap_sigma = (st.center_live - self._fleet_median[fk]) / sigma_ref
        gap_pct = self._gap_pct(st.center_live, self._fleet_median[fk])
        return [{"group_key": gk, "gap_sigma": gap_sigma, "gap_pct": gap_pct, "suspect": suspect}]

    def chamber_rollup(self, chamber_id: str) -> dict | None:
        """챔버 전(全) 센서 중 최악 |gap_σ| 1건 롤업 → tttm 객체(§4-3). 자격 gk 없으면 None.

        #4 가드: warm·σ_ref>EPS·fk∈fleet_median 인 gk만 후보. score/top은 suspect 센서 포함
        전 센서 max(#3 배경 — 억제는 B4-1 라우팅). `reference_id`/`chamber_id`는 DB 전용(#7).
        """
        if chamber_id in self.excluded:                  # §3-7 제외 챔버는 score에서 전부 빠짐
            return None
        cand = [gk for gk in self._state
                if gk[0] == chamber_id and self._warm(self._state[gk])
                and self.sigma_ref(gk) > EPS and gk[1:] in self._fleet_median]
        if not cand:
            return None
        gaps = {gk: (self._state[gk].center_live - self._fleet_median[gk[1:]]) / self.sigma_ref(gk)
                for gk in cand}
        worst = max(cand, key=lambda gk: abs(gaps[gk]))
        fm = self._fleet_median[worst[1:]]
        return {
            "reference": "fleet_median",                             # B2
            "score": abs(gaps[worst]),                               # §3-2 max σ-갭
            "top_gap_sensor": worst[-1],                             # 최악 센서 = 경로② 진단용 (C코드)
            "gap_pct": self._gap_pct(self._state[worst].center_live, fm),
            # decouple(2026-08-05): reference_suspect를 worst(경로②) 게이팅에서 분리 — 이 챔버의
            #   어느 센서 그룹이든 역방향 suspect면 True. reverse_rule(경로①)은 offset-불변이라
            #   worst(C62 독점)에 묶으면 실제 공통이동(C17 등)이 masking됐다. (제안_경로1_..._20260805)
            "reference_suspect": any(gk[1:] in self._suspect_sensors for gk in cand),
            # 실제 역방향 발동 센서 C코드 목록 (worst와 별개 — S7 화면·RTD 진단). 없으면 [].
            #   sorted+set: cand는 삽입순이라 비결정 → C코드 정렬. 같은 센서가 여러 step 그룹에서
            #   발동하면 gk[-1]이 중복되나, step을 안 싣는 챔버 요약이라 중복은 표현 불가·노이즈 →
            #   dedup(set)로 유니크 목록. 개수도 발동 그룹 수에 안 흔들려 diff 예측성 확보(PR #113 C 리뷰).
            "suspect_sensors": sorted({gk[-1] for gk in cand if gk[1:] in self._suspect_sensors}),
            "reference_id": self._reference_id(worst),               # #7 DB 전용·σ_ref 출처(D6-a) — alert 유입 금지
            "chamber_id": chamber_id,
        }

    def _reverse_rule(self, fk: tuple, members: dict) -> bool:
        """집단 이동 판정(§3-3) — 다수가 자기 baseline에서 같은 방향으로 이탈했나(=기준 오염).

        앵커 = 각 챔버 자기 `center_ref`(고정 baseline, 라이브 median 아님 — 결정 2), 자 = σ_ref.
        active(비제외·σ_ref>EPS·warm — members) 챔버 중 >warning 이탈이 reverse_min 이상이면 fk
        suspect. 조건 거짓이면 `discard`(#1 래칭 금지 — 드리프트 해소 후 영구 suspect 방지).
        ※ score(fleet median 대비)와 앵커가 다름: 역방향은 baseline 대비 — 같은 warning이어도
        재는 대상이 다르다(의도).
        """
        up = down = 0
        for ch, center_live in members.items():
            gk = (ch,) + fk
            z = (center_live - self.center_ref(gk)) / self.sigma_ref(gk)
            if z > self.warning:
                up += 1
            elif z < -self.warning:
                down += 1
        if max(up, down) >= self.reverse_min:
            self._suspect_sensors.add(fk)
        else:
            self._suspect_sensors.discard(fk)        # #1 조건 해제 시 반드시 내려감
        return fk in self._suspect_sensors
