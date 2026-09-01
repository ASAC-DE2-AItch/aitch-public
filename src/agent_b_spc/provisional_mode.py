"""가한계(Provisional) 3단계 진입 상태머신 — B6-3-a (순수 코어).

요란 PM(레짐 전환)으로 챔버 정상이 이사가면 관리선을 **"억제(Phase 0) → 가한계 광폭
(Phase 1) → 정식 재산정(Phase 2)"** 으로 단계 전환한다. 옛 관리선 그대로면 이후 전 wafer
위반 → 알람 폭주(~231건/일)라, 수명주기로 푼다 (사이클_정의 ④).

**순수 코어**: 트리거(pm_count·Qual verdict)와 부작용(DB write·reload·set_excluded·persist)은
전부 **주입**한다. 코어 로직은 Kafka·실 DB 없이 테스트된다. 상세·계약은 스펙 §·overview 참조.

경계(a): Phase 전이 · Phase 0/1(억제·광폭) · A7 카운터 · seam 호출 · get_phase.
이월: Phase 2 실제(재산정·리베이스·Y·firm)=b · fdc.agent 구독=c · 억제 게이팅=c/B4-1.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.common.config_util import _key, _section            # #1: config_util 이동(우리 구조) 유지
from src.agent_b_spc.recalc_engine import EPS, sigma_ref_of   # EPS: σ_ref 퇴화 가드(A10 :132) — dev B6-3-e

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


@dataclass(frozen=True)
class Phase2Signal:
    """정착 완료 → Phase 2 재산정 대기 신호 (실제 재산정은 b). 호출자가 소비.

    B6-3-e: `settled_at`=표본 컷오프 기준점(c 버퍼 게이트 — 정착 시점 아님·폴백 시 =boundary),
    `converged`=M연속 σ 수렴 정착(True) / 상한 폴백(False, 미수렴 경고). 필드 추가만(계약 2-2).
    """

    chamber: str
    settled_at: int = 0                  # 표본 컷오프 기준점 (c: 이 시점부터 backward 적재)
    converged: bool = False              # M연속 정착=True / 상한 폴백=False


class Phase(enum.Enum):
    """가한계 수명주기 단계 (overview §2)."""

    NORMAL = "normal"                    # provisional 밖 (PM 전 · 조용 복귀 · Phase 2 후 firm)
    PHASE_0 = "phase_0"                  # 개방~Qual: 알람 억제 (새 정상 모름)
    PHASE_1 = "phase_1"                  # 요란 후~정착: 광폭 provisional
    AWAITING_PHASE2 = "awaiting_phase_2"  # 정착 1,000장 후: Phase 2 재산정 대기 신호(실제는 b)


@dataclass(frozen=True)
class ProvisionalConfig:
    """B6-3 config (A7·광폭). `limit_engine:` 절 — B(팀원 B) 소유(params 조정 권한자).

    config 키명을 속성명으로 복제하지 않고 명시 매핑(RecalcConfig 패턴). `provisional_k`는
    실측 대기 값(기본 5), `seasoning_loud`는 합의안 A7 v2(요란 1,000) sync.
    """

    provisional_k: float                 # Phase 1 광폭 배수 (center ± provisional_k·σ_ref) ⚠️실측
    seasoning_quiet: int                 # A7 조용 seasoning 제외 (10)
    seasoning_loud: int                  # A7 v2 요란 seasoning = Phase 1 상한/폴백 (1,000)
    k_sigma: float                       # σ_ref 역산용 (control_limit_k_sigma)
    # B6-3-e — Phase 1 종료(정착) σ 수렴 판정 (고정 1,000 → 데이터기반, seasoning_loud=상한 폴백)
    settle_window_wafers: int            # W — 비중첩 블록 길이
    settle_consecutive_m: int            # M — Δ<band 연속 충족 횟수
    settle_min_wafers: int               # 정착 유효 하한(코드는 max(이 값,(M+1)W))
    settle_block_min_fill: float         # 블록 평가 최소 유효표본 비율 (n≥W×이 값)
    deadband_k_se: float                 # A10 계수 재사용 (YAML 키=recalc_deadband_k_se) — 정착 밴드

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "ProvisionalConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        le = _section(cfg, "limit_engine", path)
        return cls(
            provisional_k=float(_key(le, "provisional_k", "limit_engine", path)),
            seasoning_quiet=int(_key(le, "seasoning_exclude_wafers", "limit_engine", path)),
            seasoning_loud=int(_key(le, "seasoning_exclude_wafers_loud", "limit_engine", path)),
            k_sigma=float(_key(le, "control_limit_k_sigma", "limit_engine", path)),
            settle_window_wafers=int(_key(le, "settle_window_wafers", "limit_engine", path)),
            settle_consecutive_m=int(_key(le, "settle_consecutive_m", "limit_engine", path)),
            settle_min_wafers=int(_key(le, "settle_min_wafers", "limit_engine", path)),
            settle_block_min_fill=float(_key(le, "settle_block_min_fill", "limit_engine", path)),
            deadband_k_se=float(_key(le, "recalc_deadband_k_se", "limit_engine", path)),  # YAML 키명 주의
        )


class ProvisionalMode:
    """가한계 진입 상태머신 (순수). 부작용은 주입 seam으로 처리.

    상태(챔버별): phase·post_pm_count·verdict·boundary·last_pm_count·last_wafer_id·
    pm_detected_at(B6-3-f PM 개방 시각).
    `_phase0_center`(새-레짐 중심 버퍼)·`_provisional`(set_excluded union)은 별도 유지.
    """

    def __init__(self, cfg: ProvisionalConfig, *, limits: dict | None = None,
                 writer=None, reload_fn=None, tttm=None, persist=None,
                 whitelist=None, min_center_n: int = 3, pm_logger=None) -> None:
        """cfg 필수. 부작용은 주입(운영은 실물, 테스트는 mock). `limits`=현행 control_limits.

        `min_center_n`=Phase 0 중심 버퍼 최소 표본(이하면 old_center fallback — Qual 5장 전제로
        기본 3). `writer.write_provisional(rows)`·`reload_fn()`·`tttm.set_excluded(set)`·
        `persist(chamber, state)`는 요란 진입·전이 시 호출된다(주입 계약).

        `pm_logger(payload)` = B6-3-f 신규 seam. 요란 확정(Phase 0→1) 시 **1회** 호출되며
        A(lean-85)의 `pm_log.json`에 요란 PM 1건을 기입한다(실구현 `pm_log_writer.PmLogWriter`).
        기본 None(미주입 = no-op) — 기존 호출부·순수 테스트 하위호환.
        """
        self.cfg = cfg
        self.limits = limits or {}       # 현행 control_limits(gk → row) — _make_provisional용
        self.writer = writer
        self.reload_fn = reload_fn
        self.tttm = tttm
        self.persist = persist
        self.pm_logger = pm_logger       # B6-3-f — pm_log 기입 seam (파일 I/O는 코어 밖)
        # B6-3-e 정착 판정 대상 그룹 = whitelist(준-꺼짐 배제). 미주입이면 limits 전 그룹(리뷰3#1).
        self.whitelist: set = set(whitelist) if whitelist else set(self.limits)
        self.min_center_n = min_center_n
        self._state: dict[str, dict] = {}          # chamber → 상태 dict
        self._phase0_center: dict[str, dict] = {}  # chamber → {gk → 러닝 평균 누적}
        self._provisional: set[str] = set()        # set_excluded union (교체 시맨틱 대비)
        # B6-3-e σ 수렴 정착 상태 (미영속 링 버퍼 — 결정2 ⓓ. chamber→gk→…)
        self._settle_block: dict[str, dict] = {}   # gk → [sum, n] 현재 블록 누적
        self._settle_prev: dict[str, dict] = {}    # gk → (mean, n_prev) 직전 블록
        self._settle_streak: dict[str, dict] = {}  # gk → int Δ<band 연속 횟수
        self._settle_eval_ok: dict[str, dict] = {}  # gk → bool 이번 경계 평가 가능(동시충족 게이트)
        self._settle_sigma_ref: dict[str, dict] = {}  # gk → firm σ_ref(광폭 아님, 진입 스냅샷/재파생)

    def get_phase(self, chamber: str) -> Phase:
        """챔버 현재 단계 (알람 게이트가 매 wafer 조회 — Phase 0=억제). 미지 챔버=NORMAL."""
        st = self._state.get(chamber)
        return st["phase"] if st else Phase.NORMAL

    # ── 전이 ────────────────────────────────────────────────────────
    def _new_state(self) -> dict:
        return {"phase": Phase.NORMAL, "post_pm_count": 0, "verdict": None,
                "boundary": None, "last_pm_count": None, "last_wafer_id": None,
                "pm_detected_at": None}              # B6-3-f — PM 개방 시각(D2, 기입 date 정본)

    def on_pm(self, chamber: str, pm_count: int, ts=None) -> None:
        """pm_count↑ 감지 → Phase 0 억제 진입 + 카운터·중심버퍼 초기화 (멱등).

        같은/과거 pm_count 재전달(at-least-once)은 무시한다 — 안 그러면 Phase 1 카운터가
        리셋돼 정착 판정이 무한 지연된다(Review1).

        `ts`(선택, B6-3-f) = pm_count↑를 감지한 wafer의 `timestamp` = **PM 개방 시각**(D2).
        요란 확정 시 pm_log `date`의 정본이라 여기서만 잡힌다(`QualVerdictConfirmed`에는
        개방 시각 필드가 없음 — 계약 §8-B). 미전달(None)이면 writer가 fallback(§3-4).
        """
        st = self._state.setdefault(chamber, self._new_state())
        if st["last_pm_count"] is not None and pm_count <= st["last_pm_count"]:
            return                                   # 중복·과거 → 무시
        st["last_pm_count"] = pm_count
        st["pm_detected_at"] = ts                    # B6-3-f (None 허용 — writer fallback)
        st["phase"] = Phase.PHASE_0                  # 억제 (get_phase 노출)
        st["post_pm_count"] = 0
        st["verdict"] = None
        st["boundary"] = None
        self._phase0_center[chamber] = {}            # 새 레짐 중심 버퍼 리셋
        self._do_persist(chamber)

    def on_qual_verdict(self, chamber: str, verdict: str):
        """Qual verdict 주입 (계약 §8-B `QualVerdictConfirmed` — 엔지니어 승인 하류, 1-1 정합).

        `verdict`는 계약 enum **"quiet"/"loud"**(조용/요란 라벨 아님). PHASE_0에서만 처리한다
        (멱등·phase 가드 — 중복 verdict·오상태 무시, Review F). 조용→NORMAL 복귀(provisional
        미진입) / 요란→Phase 1 광폭(Pass 3).
        """
        st = self._state.get(chamber)
        if st is None or st["phase"] != Phase.PHASE_0:
            return                                   # phase 가드 (on_pm과 대칭)
        st["verdict"] = verdict
        if verdict == "quiet":
            st["boundary"] = self.cfg.seasoning_quiet   # A7 조용(정보 — 실제 제외는 b)
            st["phase"] = Phase.NORMAL                  # provisional 미진입, 복귀
        elif verdict == "loud":
            self._enter_phase_1(chamber, st)            # Pass 3
            self._log_pm(chamber, st)                   # B6-3-f — 광폭 적용 뒤(우선순위)
        self._do_persist(chamber)

    def _enter_phase_1(self, chamber: str, st: dict) -> None:
        """요란 → Phase 1 광폭 provisional 진입. 광폭 생성 → write → reload → set_excluded.

        순서(스펙 §3): 경계 설정 → 광폭 rows 생성 → writer(version bump·deactivate·INSERT)
        → reload(엔진 반영) → provisional set 추가 → set_excluded(union) → phase.
        """
        st["boundary"] = self.cfg.seasoning_loud     # A7 요란 = Phase 1 상한/폴백(1,000)
        # B6-3-e — 광폭 write 前에 **firm** σ_ref 그룹별 스냅샷(이 시점 self.limits=firm).
        #   재시작 시 self.limits=광폭이라 여기서 못 잡음 → restore()가 DB 재파생(리뷰2#3·3#2).
        self._settle_sigma_ref[chamber] = {
            gk: sigma_ref_of(self.limits[gk], self.cfg.k_sigma)
            for gk in self.whitelist if gk[0] == chamber and gk in self.limits}
        self._settle_block[chamber] = {}             # 정착 링 버퍼 초기화(새 Phase 1)
        self._settle_prev[chamber] = {}
        self._settle_streak[chamber] = {}
        self._settle_eval_ok[chamber] = {}
        rows = self._make_provisional(chamber)       # 광폭 인코딩 (순수)
        if self.writer is not None:
            self.writer.write_provisional(rows)      # version bump+deactivate+INSERT (주입)
        if self.reload_fn is not None:
            self.reload_fn()                         # version 변경 → 엔진 버퍼 flush (B5-1a seam)
        self._provisional.add(chamber)
        if self.tttm is not None:
            self.tttm.set_excluded(set(self._provisional))   # ★union 전달 (교체 시맨틱, Review B)
        st["phase"] = Phase.PHASE_1

    def _make_provisional(self, chamber: str) -> list[dict]:
        """챔버 각 그룹 → 광폭 provisional row(인코딩만, 순수). writer가 메타·버전 승계.

        **광폭은 Nelson이 실제 읽는 필드에 인코딩**한다(Review A·C):
        - sigma 그룹: Nelson `from_limit(center, sigma)` 판정(ucl/lcl 무시) → `sigma` 컬럼을
          `provisional_k/3·σ_ref`로 확대(3=N1 외곽존 고정 → 3·sigma_prov = half).
        - quantile 그룹: Nelson `from_quantile(center, lcl, ucl)` 판정 → `ucl·lcl`을 center±half로.
        두 경로 판정 밴드 = center ± provisional_k·σ_ref. `method`는 원본 유지(Phase 2 재산정용).
        중심은 Phase 0 새-레짐 버퍼 자체산출(계약에 center 없음, G) — 부족 시 old_center(Review2).
        """
        rows = []
        for gk, current in self.limits.items():
            if gk[0] != chamber:                     # 이 챔버 그룹만
                continue
            new_center = self._center_for(chamber, gk, current)
            sref = sigma_ref_of(current, self.cfg.k_sigma)   # sigma/quantile 분기 헬퍼
            half = self.cfg.provisional_k * sref
            row = {"group_key": gk, "center": new_center,
                   "ucl": new_center + half, "lcl": new_center - half}
            if current["method"] == "sigma":
                # 3·sigma_prov = half → Nelson 3σ 존이 center±half와 일치 (억제)
                row["sigma"] = self.cfg.provisional_k / 3.0 * sref
                row["method"] = "sigma"
            else:                                    # quantile — ucl/lcl 확대가 유효
                row["sigma"] = current["sigma"]      # 참고통계 승계(NOT NULL)
                row["method"] = "quantile"
            rows.append(row)
        return rows

    def _center_for(self, chamber: str, gk: tuple, current: dict) -> float:
        """새-레짐 중심 — Phase 0 버퍼 평균(n≥min_center_n) 아니면 old_center fallback."""
        buf = self._phase0_center.get(chamber, {}).get(gk)   # [sum, n]
        if buf is not None and buf[1] >= self.min_center_n:
            return buf[0] / buf[1]
        return current["center"]

    def on_wafer(self, chamber: str, wafer_id: str, points: list) -> Phase2Signal | None:
        """웨이퍼 1건 처리 — A7 카운터 + Phase 0 중심 축적 + Phase 2 신호 (wafer 단위).

        **A7 경계는 wafer 수** — 한 wafer에 그룹 point가 여럿 동반돼도 `post_pm_count`는 +1만
        올린다(point마다 세면 그룹수배 과다카운트). `wafer_id`로 중복 유입을 막는다.
        Phase 0: 새-레짐 중심 버퍼 그룹별 축적(new_center 소스). Phase 1: 경계(1,000) 도달 시
        AWAITING_PHASE2 전이 + **신호 1회만** 반환(재발행 방지, Review3). 실제 재산정은 b.
        NORMAL·신호後(AWAITING_PHASE2) 챔버는 no-op(카운트·신호 없음).
        """
        st = self._state.get(chamber)
        if st is None or st["phase"] in (Phase.NORMAL, Phase.AWAITING_PHASE2):
            return None                              # 대상 아님 (카운트·신호 없음)
        if wafer_id == st["last_wafer_id"]:
            return None                              # 같은 wafer 중복 유입
        st["last_wafer_id"] = wafer_id
        st["post_pm_count"] += 1
        if st["phase"] == Phase.PHASE_0:             # 새-레짐 중심 축적 (new_center 소스)
            buf = self._phase0_center.setdefault(chamber, {})
            for p in points:
                acc = buf.setdefault(p.group_key, [0.0, 0])
                acc[0] += p.value
                acc[1] += 1
        elif st["phase"] == Phase.PHASE_1:           # B6-3-e — σ 수렴 정착 판정
            self._settle_update(chamber, points)     # 블록 누적·경계(W장) 평가
            floor = max(self.cfg.settle_min_wafers,
                        (self.cfg.settle_consecutive_m + 1) * self.cfg.settle_window_wafers)
            if st["post_pm_count"] >= floor and self._is_settled(chamber):
                st["phase"] = Phase.AWAITING_PHASE2  # 조기 정착 (M연속 수렴)
                self._do_persist(chamber)
                return Phase2Signal(chamber, settled_at=st["post_pm_count"], converged=True)
            if st["post_pm_count"] >= st["boundary"]:  # 상한(1,000) 폴백 — 미수렴
                st["phase"] = Phase.AWAITING_PHASE2
                self._do_persist(chamber)
                return Phase2Signal(chamber, settled_at=st["boundary"], converged=False)
        return None

    # ── B6-3-e 정착(σ 수렴) 판정 ────────────────────────────────────
    def _settle_update(self, chamber: str, points: list) -> None:
        """PHASE_1 블록 누적 + W장 경계마다 그룹별 σ-수렴 평가 (§3, 순수·미영속).

        whitelist 대상 그룹만 현재 블록 `[sum,n]`에 누적하고, `post_pm_count`가 W 배수(블록
        완성)일 때 각 그룹을 평가한다. 평가 가능(유효표본 min-fill + firm σ_ref>EPS)이면 직전
        블록 평균과의 σ-거리 Δ가 표본오차 밴드(`k_se·√(1/n+1/n_prev)`) 미만인지로 streak를
        갱신하고, 유예(결측·퇴화)면 블록 폐기·streak 유지·prev 롤 안 함. eval_ok로 동시충족 게이트.
        """
        W = self.cfg.settle_window_wafers
        block = self._settle_block.setdefault(chamber, {})
        for p in points:                             # 이 wafer 값 누적 (whitelist 대상만)
            if p.group_key in self.whitelist and p.group_key[0] == chamber:
                acc = block.setdefault(p.group_key, [0.0, 0])
                acc[0] += p.value
                acc[1] += 1
        if self._state[chamber]["post_pm_count"] % W != 0:
            return                                   # 블록 미완성 — 누적만

        # 블록 완성 → 그룹별 평가·롤
        sref = self._settle_sigma_ref.get(chamber, {})
        prev = self._settle_prev.setdefault(chamber, {})
        streak = self._settle_streak.setdefault(chamber, {})
        eval_ok = self._settle_eval_ok.setdefault(chamber, {})
        min_n = W * self.cfg.settle_block_min_fill
        for gk in [g for g in self.whitelist if g[0] == chamber]:
            acc = block.get(gk, [0.0, 0])
            n, s = acc[1], sref.get(gk, 0.0)
            if n <= 0 or n < min_n or s <= EPS:      # 유예 — 블록 폐기·prev 롤 안 함·streak 유지
                # n<=0: min_fill=0 오설정 시에도 dead 센서(n=0) 0 나눗셈 방어(LOW-1)
                eval_ok[gk] = False
                block[gk] = [0.0, 0]
                continue
            mean = acc[0] / n
            pv = prev.get(gk)                        # (mean, n_prev)
            if pv is not None:                       # 첫 블록·유예 직후는 비교 skip(prev None 가드)
                band = self.cfg.deadband_k_se * (1.0 / n + 1.0 / pv[1]) ** 0.5
                streak[gk] = streak.get(gk, 0) + 1 if abs(mean - pv[0]) / s < band else 0
            eval_ok[gk] = True
            prev[gk] = (mean, n)                     # 롤
            block[gk] = [0.0, 0]                     # 리셋

    def _is_settled(self, chamber: str) -> bool:
        """챔버 정착 = 대상(whitelist) 전원 이번 경계 평가 가능 + streak ≥ M (§2, latch 아님).

        streak≥M 후 센서가 죽어 유예된 그룹의 조기 정착을 막기 위해 eval_ok를 함께 요구한다(#4).
        대상 그룹 0이면 정착 취급 안 함(공허참 방지).
        """
        targets = [g for g in self.whitelist if g[0] == chamber]
        if not targets:
            return False
        eval_ok = self._settle_eval_ok.get(chamber, {})
        streak = self._settle_streak.get(chamber, {})
        M = self.cfg.settle_consecutive_m
        return all(eval_ok.get(g) and streak.get(g, 0) >= M for g in targets)

    def on_firm_established(self, chamber: str) -> None:
        """firm 확립 → provisional 탈출 (c 배선이 firm 적용 시점 호출, b 결정4).

        **AWAITING_PHASE2(Phase 2 정착 후)에서만** 동작한다 — R9 반려 시 c가 안 부르면
        provisional holding이 유지된다(멱등·가드). provisional set에서 제거 → 남은 union으로
        `set_excluded` 재적용해 TTTM 복귀(교체 시맨틱, Review B) → 새-레짐 중심 버퍼 정리 → NORMAL.
        """
        st = self._state.get(chamber)
        if st is None or st["phase"] != Phase.AWAITING_PHASE2:
            return                               # 정착 후에만 (반려 시 holding 유지)
        self._provisional.discard(chamber)       # union에서 제거
        if self.tttm is not None:
            self.tttm.set_excluded(set(self._provisional))   # 남은 union → TTTM 복귀
        self._phase0_center.pop(chamber, None)   # 새-레짐 중심 버퍼 정리
        st["phase"] = Phase.NORMAL
        self._do_persist(chamber)

    def restore(self, state: dict, settle_sigma_ref: dict | None = None) -> None:
        """재시작 복구 (c) — 주입 state로 전 필드 복원 + PHASE_1 챔버 재-제외 + firm σ_ref 재적재.

        `state = {chamber: {phase, post_pm_count, verdict, boundary, last_pm_count}}`. phase는
        Phase enum 또는 그 value(str) 둘 다 허용(라운드트립 호환). `last_wafer_id`는 미영속
        (인메모리·±1 허용)이라 None으로. 복구 소스(c): phase←control_limits(provisional 활성)
        파생·verdict는 provisional-활성이 loud 함의(quals=PASS/FAIL이라 loud/quiet 직접 복구 X —
        §8-B 'quals 복구' 문구 정정). 여기선 주입 state를 신뢰한다. **PHASE_1·AWAITING_PHASE2** 챔버(둘 다 firm
        전까진 provisional-active·TTTM 제외 상태)는 `_provisional` 재구성 후 `set_excluded(union)`
        재적용 — excluded는 tttm 인메모리라 재시작 시 유실되므로(AWAITING 누락 시 조기 fleet 복귀→오염).
        """
        for chamber, s in state.items():
            st = self._new_state()
            st.update(s)
            if not isinstance(st["phase"], Phase):   # value(str)로 저장된 경우
                st["phase"] = Phase(st["phase"])
            st["last_wafer_id"] = None               # 미영속
            self._state[chamber] = st
            if st["phase"] in (Phase.PHASE_1, Phase.AWAITING_PHASE2):
                self._provisional.add(chamber)       # 둘 다 firm 전 provisional-active·TTTM 제외
        if self._provisional and self.tttm is not None:
            self.tttm.set_excluded(set(self._provisional))   # union 재적용 (excluded 유실 방어)
        # B6-3-e — firm σ_ref 재적재(리뷰3#2). c가 **비활성 firm** control_limits 행에서 재파생해
        #   주입(광폭-active 아님·별도 영속 없는 파생 모델). 미복원이면 σ_ref=0 → 유예→1,000 폴백
        #   (KeyError 없음·조기 정착만 손실, 안전). _settle_* 링 버퍼는 재충전(~2W 지연 수용, ⓓ).
        if settle_sigma_ref:
            for chamber, sref in settle_sigma_ref.items():
                self._settle_sigma_ref[chamber] = dict(sref)

    # ── 부작용 seam ─────────────────────────────────────────────────
    def _do_persist(self, chamber: str) -> None:
        """상태 영속 주입 호출 (미주입이면 no-op — 순수 테스트)."""
        if self.persist is not None:
            self.persist(chamber, self._state[chamber])

    def _log_pm(self, chamber: str, st: dict) -> None:
        """요란 확정 → pm_log 기입 seam 호출 (B6-3-f). **실패해도 전이를 막지 않는다.**

        pm_log는 A 예측 메타 피처(`days_since_last_pm`·`is_high_regime`)와 이벤트 재학습의
        입력이라 최신성 문제지, 억제·광폭 같은 안전 경로가 아니다 → try-except로 격리하고
        에러 로그만 남긴다(6-2 정신). 호출은 `_enter_phase_1` **뒤** — 광폭 적용이 우선순위.

        멱등: `on_qual_verdict`의 phase 가드(PHASE_0만 수리)가 재전달을 이미 차단하므로
        seam은 **챔버당 PM 사이클당 최대 1회** 호출된다. 파일 측 중복 방어는 writer의
        멱등 키 `(chamber_id, pm_count, date)`가 담당(크래시-재전달 창 이중 방어 — date를
        포함하는 이유는 `pm_log_writer.PmLogWriter` docstring·계약 §8-B-1 참조).
        """
        if self.pm_logger is None:
            return                                   # 미주입 = no-op (순수 테스트·b63e)
        try:
            self.pm_logger({"chamber_id": chamber,
                            "pm_count": st["last_pm_count"],
                            "pm_detected_at": st["pm_detected_at"],
                            "verdict": "loud"})
        except Exception:                            # noqa: BLE001 — seam 격리(전이 보호)
            logger.exception("pm_log 기입 실패 — Phase 1 진입은 유지: chamber=%s", chamber)
