"""B6-3-b: Phase 2 정식 재수립 코어 (순수).

요란 PM으로 챔버 정상이 이사간 뒤 정착이 끝나면(a의 PHASE2_SIGNAL), 새 레짐의
**정식 실력치를 세우고** 챔버를 firm으로 졸업시킨다. 한 정착 표본에서 세 산출물
(관리선·스냅샷·Y-임계)을 동시 산출해 "패키지"로 R9 승인 → 일괄 적용한다(스펙 §2).

정기 재산정(`recalc_engine.recompute_group`)과의 차이 — **온라인 무상한 재수립**:
상한(A2·A3)·dead_band·auto 경로가 없고 **항상 needs_approval**(신규 기준선 수립,
신규결정 2). 산식은 `initial_limits.compute_group_limits`를 공유(initial=recalc=establish
동일). DB·Kafka를 모르는 순수 코어 — 라이브 트리거·버퍼 공급·firm apply는 c 이월.

확정 파라미터는 config/params.yaml에서 로드한다 (헌법 6-1).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from sqlalchemy import text

from src.common.config_util import _key, _section
from src.agent_b_spc.initial_limits import LimitConfig, compute_group_limits
from src.agent_b_spc.recalc_engine import (
    STATUS_PROPOSED,
    RecalcProposal,
    _filter_values,
)
from src.agent_b_spc.seed_qual_snapshots import (
    HEALTH_SENSORS,
    gk_str,
    snapshot_stats_for_group,
)

# 저장소 루트의 공유 파라미터 파일 (헌법 6-1)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

# 재수립 correction의 trigger_type — 정기('periodic')와 구분. 이미 reset_ref baseline
# 마커이자 regime_transition Incident 종결 조치(결정2). firm/provisional 구분은 trigger_type.
TRIGGER_INCIDENT = "incident"


@dataclass(frozen=True)
class EstablishConfig:
    """Phase 2 재수립 파라미터. 산식은 LimitConfig 합성(RecalcConfig 패턴 — 단일 소스)."""

    limit: LimitConfig
    min_group_n: int                        # 소표본 가드 (spc 절 재활용)
    seasoning_loud: int                     # A7 요란 과도 제외 창(1,000) — 초과분만 정착
    seasoning_quiet: int                    # A7 조용 과도 제외 창(10) — Step7 정기 리캘리 backward head skip (D3)
    min_establishment_wafers: int           # 재수립 최소 정착 표본(하한 가드, Review1)
    rolling_window_n: int                   # 재수립 표본 최근 창(N=500)
    snapshot_min_n: int                     # 리베이스 그룹별 최소 유효 표본(붕괴 skip, Review3)

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "EstablishConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        spc = _section(cfg, "spc", path)
        le = _section(cfg, "limit_engine", path)
        return cls(
            limit=LimitConfig.load(path),
            min_group_n=int(_key(spc, "min_group_n", "spc", path)),
            seasoning_loud=int(_key(le, "seasoning_exclude_wafers_loud", "limit_engine", path)),
            seasoning_quiet=int(_key(le, "seasoning_exclude_wafers", "limit_engine", path)),
            min_establishment_wafers=int(
                _key(le, "min_establishment_wafers", "limit_engine", path)),
            rolling_window_n=int(_key(le, "rolling_window_n", "limit_engine", path)),
            snapshot_min_n=int(_key(le, "snapshot_min_n", "limit_engine", path)),
        )


@dataclass(frozen=True)
class YThreshold:
    """챔버×레시피 예측 수율(C65) 임계 1행 — P95(C2 Qual)·P99(B9 crazy). writer가 소비.

    predicted_c65는 net-new 가상센서라 control_limits에 안 담고 전용 `y_thresholds` 테이블에
    쓴다(Y-1 ⓑ). label-free 1차(라벨 보정은 이월). trigger_type='incident'(레짐 재수립).
    """
    chamber_id: str
    recipe_id: str
    p95: float
    p99: float
    calc_window_n: int
    trigger_type: str = TRIGGER_INCIDENT


@dataclass(frozen=True)
class WaferSample:
    """정착 버퍼 1장 — c가 라이브에서 채워 주입하는 계약(순수 코어는 shape만 안다).

    `post_pm_count` = PM 이후 누적 wafer 수(a의 on_wafer 카운터). `values` = 그 wafer의
    group_key별 대표값(그룹당 1점 — B5-2/shadow 관행). 재수립 표본 선택의 최소 정보.
    """
    post_pm_count: int
    values: dict = field(default_factory=dict)


def establish_group(group_key, settled_values, current, cfg: EstablishConfig) -> RecalcProposal:
    """정착 표본으로 새 관리선을 신규수립 — **상한 없음·항상 승인대기**(신규결정 2).

    정기 `recompute_group`과 달리 dead_band·cap·auto 판정이 없다(무상한). method는
    current 승계(sigma/quantile 분기 — new_ucl/lcl 소스가 달라짐). new_sigma는 항상
    채운다(control_limits sigma NOT NULL). trigger_type='incident'(결정2).
    """
    method = current["method"]
    filtered = _filter_values(settled_values)
    n_used = len(filtered)
    if n_used < cfg.min_group_n:
        return RecalcProposal(group_key, "no_change", method, TRIGGER_INCIDENT, n_used,
                              reason="small_sample")
    new = compute_group_limits(np.array(filtered), cfg.limit)
    new_ucl, new_lcl = (new["q_ucl"], new["q_lcl"]) if method == "quantile" else (new["ucl"], new["lcl"])
    return RecalcProposal(group_key, "needs_approval", method, TRIGGER_INCIDENT, n_used,
                          new_center=new["center"], new_ucl=new_ucl, new_lcl=new_lcl,
                          new_sigma=new["sigma"], status=STATUS_PROPOSED)


def select_establishment_sample(buffered_wafers, cfg: EstablishConfig, settled_at=None):
    """정착 표본 1벌 선택 — 과도 컷오프 제외 + 최근 N=500, 부족하면 None(하한 가드).

    **컷오프 = `settled_at` (B6-3-e)**: a의 σ-수렴 정착이 정한 표본 컷오프 기준점. 미전달이면
    `cfg.seasoning_loud`(고정 1,000 — 현행 바이트 동일). 조기 정착(예: 600)이면 601부터 표본.

    **하한 가드 = 대기 메커니즘(Review1)**: a가 정착 1,000에 PHASE2_SIGNAL을 쏜 직후엔
    post-1,000 wafer가 0장 → 1,001~1,500이 쌓일 때까지 **None을 반환(패키지 미생성)**,
    c가 wafer 유입에 따라 재호출해 충족되면 그때 표본이 만들어진다. Kafka 누락·재시작으로
    표본이 모자라도 동일하게 안전 대기(불완전 패키지 원천 차단).

    `buffered_wafers`는 c가 **도착 순서**로 주입 → `[-rolling_window_n:]`이 최근 N장.
    반환은 `{group_key: [values]}`(establish_group·rebase가 소비). 정착 없으면 None.
    """
    cutoff = cfg.seasoning_loud if settled_at is None else settled_at
    settled = [w for w in buffered_wafers if w.post_pm_count > cutoff]
    if len(settled) < cfg.min_establishment_wafers:
        return None                                       # 부족 → 대기(c 재호출)
    recent = settled[-cfg.rolling_window_n:]               # 최근 N장(도착순 가정)
    grouped: dict = defaultdict(list)
    for w in recent:
        for gk, v in w.values.items():
            grouped[gk].append(v)
    return dict(grouped)


def rebase_snapshot(chamber, settled_by_group, current_by_group, cfg: EstablishConfig) -> dict:
    """정착 표본으로 Qual 스냅샷 리베이스(R5) — 건강센서 8종·σ=전체창. gk_str-keyed sensor_stats.

    B5-2 초기 스냅샷과 동일 산식(`snapshot_stats_for_group` — 전체창 σ, R5-1)이라 σ-갭 잣대가
    레짐 전후 일관된다. method는 establish와 같은 소스(current 승계 — sigma/quantile 분기).
    비건강 센서·붕괴 그룹(유효 < snapshot_min_n)은 skip(ZeroDiv/KeyError 방지 — Review3).
    반환은 qual_snapshots INSERT 직전 형식(gk_str→stats, offset shift 없음 — 실 챔버 데이터).
    """
    stats: dict = {}
    for gk, vals in settled_by_group.items():
        sensor = gk[4]
        if sensor not in HEALTH_SENSORS:
            continue
        clean = _filter_values(vals)
        if len(clean) < cfg.snapshot_min_n:
            continue
        cur = current_by_group.get(gk)
        if cur is None:                                   # method 불명 → skip(sigma 추정 금지)
            continue                                      # 분위수 KEEP*에 sigma는 구조적 오탐 밴드
        stats[gk_str(*gk)] = snapshot_stats_for_group(clean, cur["method"], cfg.limit)
    return stats


def compute_y_threshold(chamber, recipe, recent_predictions, cfg: EstablishConfig):
    """정착 예측분포 → Y-임계(P95·P99) — label-free 1차(Y-2). 유효 0장이면 None.

    요란 PM(레짐 전환) 후 고정 상수(1,572)는 새 레짐서 오발동(seg2 평균 1,123)하므로,
    챔버별 최근 예측분포로 임계를 갱신한다(신규결정 1). P95=C2 Qual·P99=B9 crazy 기준.
    데이터=wafer_predictions(A 발행). 라벨 보정(D+60)은 이월. `cfg`는 시그니처 정합용(향후
    최소표본 가드 등 — 현재 미사용, 빈배열만 가드).
    """
    clean = _filter_values(recent_predictions)
    if not clean:
        return None                                       # 빈 분위수 크래시 가드
    p95, p99 = np.quantile(clean, [0.95, 0.99])
    return YThreshold(chamber_id=chamber, recipe_id=recipe,
                      p95=float(p95), p99=float(p99), calc_window_n=len(clean))


# 전용 y_thresholds writer — control_limits 버전 관리 패턴(active 교체·버전 bump). net-new
# INSERT라 선행 행 불요(predicted_c65는 시딩된 적 없는 가상센서 — Y-1 ⓑ). commit=호출자.
_Y_NEXT_VERSION = text(
    "SELECT COALESCE(MAX(CAST(SUBSTRING(threshold_version FROM 2) AS INTEGER)), 0) + 1 "
    "FROM y_thresholds WHERE chamber_id = :ch AND recipe_id = :rc")
_Y_DEACTIVATE = text(
    "UPDATE y_thresholds SET is_active = false WHERE chamber_id = :ch AND recipe_id = :rc AND is_active")
_Y_INSERT = text(
    "INSERT INTO y_thresholds "
    "  (chamber_id, recipe_id, p95, p99, threshold_version, calc_window_n, trigger_type, is_active) "
    "VALUES (:ch, :rc, :p95, :p99, :ver, :n, :tt, true)")


def write_y_threshold(conn, y: YThreshold) -> str:
    """Y-임계 1행 적재 — 기존 active 비활성 → 신규 active INSERT (버전 bump). commit=호출자.

    control_limits 승계 경로(apply_approved)와 달리 **선행 행 없이도 그냥 INSERT**한다
    (predicted_c65는 net-new 가상센서). 쓰기 순서: DEACTIVATE 먼저 → INSERT
    (idx_y_thresholds_active_one 부분 유니크). label_corrected는 DB 기본값 FALSE(label-free 1차).
    """
    n = conn.execute(_Y_NEXT_VERSION, {"ch": y.chamber_id, "rc": y.recipe_id}).scalar()
    version = f"v{n}"
    conn.execute(_Y_DEACTIVATE, {"ch": y.chamber_id, "rc": y.recipe_id})
    conn.execute(_Y_INSERT, {"ch": y.chamber_id, "rc": y.recipe_id, "p95": y.p95, "p99": y.p99,
                             "ver": version, "n": y.calc_window_n, "tt": y.trigger_type})
    return version


# B4-1 Step5b — read seam(신설). crazy predicted arm=P99 / Qual·pred enrichment=P95.
#   p95·p99는 같은 활성 행이라 1회 조회로 둘 다 반환한다(상위 YThresholdCache가 쌍으로 캐시 →
#   DB 왕복 절약). 캐싱·무효화는 상위 — 여기는 순수 DB 1행 조회.
_Y_SELECT_ACTIVE_PAIR = text(
    "SELECT p95, p99, threshold_version FROM y_thresholds "
    "WHERE chamber_id = :ch AND recipe_id = :rc AND is_active LIMIT 1")


def read_y_thresholds(conn, chamber_id: str, recipe_id) -> dict | None:
    """활성(is_active) y_thresholds 행의 {p95, p99, threshold_version}을 반환.

    행 없거나 recipe 결측이면 None. crazy predicted arm 컷=p99 / Qual·pred enrichment 임계=p95 /
    `threshold_version`=그 컷의 출처 버전(Q2 — crazy 마커 `limit_version` 에 실린다).
    셋 다 같은 행이라 1회 조회로 끝난다. is_active 부분 유니크라 최대 1행. commit 불요(읽기).

    `threshold_version` 이 필요한 이유: 재수립(`write_y_threshold`)마다 버전이 bump 되는데
    (v1→v2…), 마커에 고정 센티넬을 실으면 **실제 판정에 쓴 컷과 다른 버전**이 기록된다.
    """
    if recipe_id is None:
        return None
    row = conn.execute(_Y_SELECT_ACTIVE_PAIR, {"ch": chamber_id, "rc": recipe_id}).first()
    if row is None:
        return None
    return {"p95": float(row[0]), "p99": float(row[1]), "threshold_version": row[2]}


def read_y_threshold(conn, chamber_id: str, recipe_id) -> float | None:
    """활성 y_thresholds 행의 P99만 반환 — 하위호환(crazy predicted arm 단건 조회). 부재=None.

    `read_y_thresholds`(쌍) 위임. is_crazy fallback=config 1572. recipe None이면 None.
    """
    rec = read_y_thresholds(conn, chamber_id, recipe_id)
    return rec["p99"] if rec is not None else None

# 참고 (B6-3-b C-2 결정, 2026-07-22): 세 산출물을 한 패키지로 묶던 `assemble_package`는 제거됨.
# c 오케스트레이션이 **관리선=제안 시점(record_proposed·승인값 그대로)** / **스냅샷·Y=apply 시점 재계산**
# (스냅샷=c 정착버퍼·rebase_snapshot · Y=wafer_predictions read·compute_y_threshold)으로 시점을
# 분리하므로, 셋을 동시 산출·묶는 헬퍼는 시점 충돌(제안≠적용)이라 미사용 확정. 개별 함수는 그대로.
