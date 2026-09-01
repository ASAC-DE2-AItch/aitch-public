"""B2-1: 센서별 초기 실력치(Control Limit) 테이블 산출.

train_data.csv에서 PM 리셋(C33) 이후 정착 세그먼트를 표본으로,
Chamber(C24) x Recipe(C6) x Step(C7) x Window(C42) x 센서별
rolling mean ± 3σ 초기 관리선을 계산한다 (기획서 4-0/4-1, config A1·A7·A8).
확정 파라미터(k_sigma·rolling_n·seasoning)는 config/params.yaml에서 로드한다 (헌법 6-1).

산출물 (--out-dir, 기본 src/agent_b_spc/limits/):
  - initial_limits_v1.csv   : 초기 실력치 테이블 (limit_version=v1, trigger_type=initial)
  - initial_limits_v1_meta.json : 표본 구간 메타 (리셋 위치, seasoning 제외 등)
  - qa_report_v1.md         : 그룹별 QA 결과 (표본 수·σ·CV·가성알람율·한계 폭 sanity check)

사용법:
    python -m src.agent_b_spc.initial_limits [--data PATH] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from src.common.config_util import _section, _key

logger = logging.getLogger(__name__)

# 저장소 루트의 공유 파라미터 파일 (헌법 6-1: 하드코딩 금지)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


@dataclass(frozen=True)
class LimitConfig:
    """initial_limits 확정 파라미터 (params.yaml 단일 소스). Stage B에서 q_low/q_high 추가."""
    k_sigma: float
    rolling_n: int
    seasoning_wafers: int
    trim_quantile: float
    false_alarm_warn_pct: float
    cv_warn: float
    q_low: float
    q_high: float
    min_trim_n: int
    resolution_deadband_k: float

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "LimitConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        le = _section(cfg, "limit_engine", path)
        spc = _section(cfg, "spc", path)
        return cls(
            k_sigma=float(_key(le, "control_limit_k_sigma", "limit_engine", path)),
            rolling_n=int(_key(le, "rolling_window_n", "limit_engine", path)),
            seasoning_wafers=int(_key(le, "seasoning_exclude_wafers", "limit_engine", path)),
            trim_quantile=float(_key(spc, "trim_quantile", "spc", path)),
            false_alarm_warn_pct=float(_key(spc, "false_alarm_warn_pct", "spc", path)),
            cv_warn=float(_key(spc, "cv_warn", "spc", path)),
            q_low=float(_key(spc, "q_low", "spc", path)),
            q_high=float(_key(spc, "q_high", "spc", path)),
            min_trim_n=int(_key(spc, "min_trim_n", "spc", path)),
            resolution_deadband_k=float(_key(le, "resolution_deadband_k", "limit_engine", path)),
        )


INITIAL_LIMIT_VERSION = "v1"

# SPC 대상 센서 (API_Contract_명세서.md 0절 "SPC 대상 ✅" 기준)
SENSORS_SETTLED = [
    "C11",   # DC Self-Bias (Vdc) ★핵심
    "C31",   # RF 출력 실측
    "C32",   # RF Reflected (디지털)
    "C61",   # RF 음측 피크 전압
    "C62",   # RF 전극 전압 (Vpp)
    "C17",   # 히터/척 온도
    "C9",    # 온도 계열 (C17 동행)
    "C15",   # Gas Flow A 실측
    "C16",   # Gas B 변동 신호 (실측)
    "C57",   # He Backside 제어
    "C58",   # He Backside 압력
    "C63",   # 미상 아날로그 (step별 운전점 + 드리프트)
]
# 매칭 계열 — 점화 과도(C42=1) window에서만 의미 (계약: "과도 Window만")
#  - C54·C56: 2026-07-12 정착→과도 재정정(7/10 정착 이동 번복·PM 승인). raw 3초 std로 본
#    "정착 std≈과도"가 오류였고, 관리선이 쓰는 wafer-mean 단위론 정착 480 vs 과도 148(3~4배)
#    → 과도가 ±3σ ~2배 예민(오탐 둘 다 0%). 정착 타 step(1·5·6·7)은 상수 0 = 감시 불가(dead).
SENSORS_TRANSIENT_ONLY = ["C18", "C27", "C54", "C56"]

# 파생 지표 (데이터 사전 모델링 노트 3·본 작업 설계)
#  - D_VDC_RES : C11 − C12 (Vdc 실측 − step 기준값 잔차)
DERIVED_SETTLED = ["D_VDC_RES"]

ID_COLS = ["C64", "C20", "C6", "C7", "C10", "C24", "C33", "C42"]


def load_data(path: Path) -> pd.DataFrame:
    """train CSV를 읽어 시간순(C10)으로 정렬해 반환한다."""
    logger.info("데이터 로드: %s", path)
    usecols = sorted(set(ID_COLS + SENSORS_SETTLED + SENSORS_TRANSIENT_ONLY + ["C12"]))
    df = pd.read_csv(path, usecols=usecols)
    df = df.sort_values("C10").reset_index(drop=True)
    logger.info("rows=%d, wafers=%d", len(df), df["C64"].nunique())
    return df


def determine_sample_wafers(df: pd.DataFrame, cfg: LimitConfig) -> tuple[list[str], dict]:
    """표본 구간 확정 (A8+A7 + R1 복귀 블록).

    마지막 PM 리셋(C33 증가) 세그먼트에서, C6_1 실험 블록 **이후**의 연속 C6_0(복귀 블록)만
    표본으로 쓴다. 실험 前 C6_0는 '현재 레짐 창 밖'으로 제외(갭 가로지름 방지 — 설계 R1).
    seasoning(A7)은 복귀 블록엔 post-PM wafer가 없어 의미상 N/A이나 코드 일관성 위해 유지(무영향).
    """
    w = (df.groupby("C64")
         .agg(t0=("C10", "min"), c33=("C33", "max"), recipe=("C6", "first"))
         .sort_values("t0").reset_index())
    reset_idx = w.index[w["c33"] < w["c33"].shift(1)].tolist()
    if not reset_idx:
        raise ValueError("C33 리셋 지점을 찾지 못했습니다 — 표본 구간(A8)을 확정할 수 없습니다.")
    last_reset = reset_idx[-1]
    segment = w.iloc[last_reset:]

    exp = segment[segment["recipe"] == "C6_1"]
    if not exp.empty:
        last_exp_t0 = exp["t0"].max()
        return_block = segment[(segment["recipe"] == "C6_0") & (segment["t0"] > last_exp_t0)]
        pre_exp = segment[(segment["recipe"] == "C6_0") & (segment["t0"] < exp["t0"].min())]
    else:
        return_block = segment[segment["recipe"] == "C6_0"]
        pre_exp = segment.iloc[0:0]

    # 정산 무결성 (silent 데이터 손실 방지): C6_1이 비연속(다중 에피소드)이면 에피소드 사이 C6_0가
    #   pre/exp/return 어디에도 안 들어가 조용히 사라진다 → 하드 실패로 잡는다(현 데이터는 단일 블록이라 무발동).
    accounted = len(pre_exp) + len(exp) + len(return_block)
    if accounted != len(segment):
        raise RuntimeError(
            f"세그먼트 정산 불일치: pre {len(pre_exp)} + C6_1 {len(exp)} + 복귀 {len(return_block)} "
            f"= {accounted} ≠ segment {len(segment)} — C6_1 비연속(다중 에피소드) 또는 예상 밖 recipe. 재검토 필요.")

    sample = return_block.iloc[cfg.seasoning_wafers:]

    def _period(s):
        if s.empty:
            return [None, None]
        return [str(pd.to_datetime(s["t0"].min(), unit="s")),
                str(pd.to_datetime(s["t0"].max(), unit="s"))]

    meta = {
        "reset_wafer": str(w.loc[last_reset, "C64"]),
        "reset_position": int(last_reset),
        "segment_wafers": int(len(segment)),
        "return_block_wafers": int(len(return_block)),
        "seasoning_excluded": cfg.seasoning_wafers,
        "sample_wafers": int(len(sample)),
        "sample_period": _period(sample),
        "excluded_experimental": {
            "C6_1": {"n": int(len(exp)), "period": _period(exp)},
            "C6_0_pre_experiment": {"n": int(len(pre_exp)), "period": _period(pre_exp),
                                    "reason": "현재 레짐 창 밖(실험 前 생산)"},
        },
    }
    logger.info("표본(복귀 블록): C6_0 %d장 − seasoning %d = %d장 / 제외 C6_1 %d·실험前 C6_0 %d",
                len(return_block), cfg.seasoning_wafers, len(sample), len(exp), len(pre_exp))
    return sample["C64"].tolist(), meta


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """가공 피처를 각 row에 추가한다 (D_VDC_RES)."""
    df = df.copy()
    df["D_VDC_RES"] = df["C11"] - df["C12"]
    return df


def build_wafer_summaries(df: pd.DataFrame) -> pd.DataFrame:
    """wafer x step x window 단위 summary indicator(평균)를 만든다.

    관리선의 SPC 포인트는 raw 3초 샘플이 아니라 wafer 단위 summary다
    (기획서 4-0: 현업 FDC summary 구조와 정합). Window(C42)는 관리 축이
    아니라 지표 추출 계층 — step 4만 과도/정착 2종, 나머지 step은 정착만.
    """
    df = df.copy()
    df["sensor_window"] = np.where(df["C42"] == 1, "transient", "settled")

    sensors = SENSORS_SETTLED + DERIVED_SETTLED + SENSORS_TRANSIENT_ONLY
    grouped = (
        df.groupby(["C24", "C6", "C7", "sensor_window", "C64"])
        .agg({**{s: "mean" for s in sensors}, "C10": "min"})
        .rename(columns={"C10": "t0"})
        .reset_index()
    )
    long = grouped.melt(
        id_vars=["C24", "C6", "C7", "sensor_window", "C64", "t0"],
        var_name="sensor_id",
        value_name="value",
    ).dropna(subset=["value"])

    # window 유효성: 과도 전용 센서는 transient만, 나머지는 settled만
    is_transient_only = long["sensor_id"].isin(SENSORS_TRANSIENT_ONLY)
    keep = (is_transient_only & (long["sensor_window"] == "transient")) | (
        ~is_transient_only & (long["sensor_window"] == "settled")
    )
    # step 4 정착 센서의 과도 구간 통계도 향후 참고용으로 남길 수 있으나 v1은 제외
    long = long[keep]
    logger.info("wafer summary 포인트: %d건", len(long))
    return long


def compute_group_limits(values, cfg: LimitConfig, spread_values=None) -> dict:
    """단일 그룹 값 배열 → 관리선·QA·분해능 전 필드 (initial·recalc 공유 산식 — B4-3).

    현 `compute_limits` 그룹 루프 내부 산식을 순수 함수로 추출한다. initial_limits는
    groupby 루프에서 이를 호출하고, recalc_engine도 같은 함수를 호출해 **동일 산식**을
    보장한다(회귀 0). 반환은 CSV 한 행의 계산 필드 + resolution(meta 저장용).

    값 배열은 이미 (최근 N장 클립·NaN 필터가) 정리된 상태를 전제한다 — 클립·필터는 호출자
    책임(initial=루프에서 rolling_n 클립 / recalc=주입·NaN 필터). 트림은 여기서 항상 적용.

    **center/spread 표본 분리 (2026-08-06, σ 재보정 방식 B — PM 승인)**: `values`는
    center(μ = 트림 평균) 표본, `spread_values`는 σ·분위수·형태 진단 표본이다.
    `spread_values=None`이면 values를 재사용해 **기존 거동과 완전히 동일**(recalc 경로·
    회귀 0). 초기 시딩만 방식 B로 분리 호출한다 — center=최근 rolling_n창(현재 운영점,
    멘토 "rolling mean") / σ·분위수=전체 복귀블록(대표 산포). 조용한 최근 500창이 σ를
    과소추정해 라이브 오탐 밀도를 키우던 뿌리를 고친다(±3σ 배수는 불변, σ 추정 표본만 교체).
    """
    values = np.asarray(values, dtype=float)
    spread = np.asarray(spread_values, dtype=float) if spread_values is not None else values
    n_raw = len(spread)
    # center(μ) — center 표본(values)의 트림 평균 (방식 B: 최근 창 = 현재 운영점)
    lo_c, hi_c = np.quantile(values, [cfg.trim_quantile, 1 - cfg.trim_quantile])
    trimmed_c = values[(values >= lo_c) & (values <= hi_c)]
    mu = float(np.mean(trimmed_c))
    # σ·분위수·형태 진단 — spread 표본 (방식 B: 전체 복귀블록 = 대표 산포)
    lo, hi = np.quantile(spread, [cfg.trim_quantile, 1 - cfg.trim_quantile])
    trimmed = spread[(spread >= lo) & (spread <= hi)]      # 트림은 σ-track 전용
    # 분위수 관리선 (조합②) — 반드시 트림 전 spread에서 (트림된 표본이면 경계 붕괴)
    q_lcl = float(np.quantile(spread, cfg.q_low))
    q_ucl = float(np.quantile(spread, cfg.q_high))
    sigma = float(np.std(trimmed, ddof=1)) if len(trimmed) > 1 else 0.0
    ucl = mu + cfg.k_sigma * sigma
    lcl = mu - cfg.k_sigma * sigma

    # 분해능 (M-B) — 정렬 unique의 최소 인접 간격. uniq≤1(상수·극소표본)이면 0.0
    #   (np.diff가 빈 배열 → np.min 예외로 파이프라인 정지하는 것 방지).
    uniq = np.unique(spread)
    resolution = float(np.min(np.diff(uniq))) if len(uniq) > 1 else 0.0

    # 분해능 dead-band (2026-08-06, PM 승인 — gage R&R: 관리선 최소폭 ≥ 계측 분해능).
    #   이산 센서의 밴드가 분해능보다 좁으면 계측 노이즈만 잡는다(오탐). 각 밴드의 center 기준
    #   최소 반폭 = k_res×resolution 을 보장한다. σ·분위수 밴드 둘 다 floor한다 — 어느 method가
    #   쓰일지는 seed 단계에서 결정되고, quantile 밴드는 σ-floor로 못 막기 때문(median 인접 여유가
    #   분해능보다 좁은 게 뿌리). σ 통계량 자체는 불변 — ucl/lcl·q 밴드 폭만 넓힐 수 있다.
    min_margin = cfg.resolution_deadband_k * resolution
    deadband_floored = False
    if min_margin > 0:
        q_med = float(np.median(spread))
        new_ucl, new_lcl = max(ucl, mu + min_margin), min(lcl, mu - min_margin)
        new_q_ucl, new_q_lcl = max(q_ucl, q_med + min_margin), min(q_lcl, q_med - min_margin)
        # 실제 floor 발동 여부(밴드가 넓어졌나) — QA 가시성용(리뷰 §4 ②: σ=0인데 밴드 벌어지는
        #   케이스를 명시해 zero_sigma와의 겉보기 모순을 리포트에서 드러낸다).
        deadband_floored = (new_ucl != ucl or new_lcl != lcl
                            or new_q_ucl != q_ucl or new_q_lcl != q_lcl)
        ucl, lcl, q_ucl, q_lcl = new_ucl, new_lcl, new_q_ucl, new_q_lcl

    false_alarm_pct = float(((spread > ucl) | (spread < lcl)).mean() * 100)

    # 분포 형태 진단 — bimodal/비정규 센서는 ±3σ가 깨져 한계가 무의미하게 넓어진다.
    # (예: 상호배타 다채널 재구성값 — false_alarm이 0이라 high_false_alarm으로는 안 잡힘)
    s_min = float(np.min(trimmed))
    s_max = float(np.max(trimmed))
    crosses_zero = s_min < 0 < s_max
    cv = abs(sigma / mu) if mu != 0 else float("inf")

    flags = []
    # low_n 제거 (MIN_GROUP_N 죽은 코드) — 소표본 가드는 B4-3
    if sigma == 0.0 or not np.isfinite(sigma):
        flags.append("zero_sigma")
    if deadband_floored:                    # 분해능 dead-band이 밴드를 넓힘(zero_sigma여도 밴드有 사유)
        flags.append("deadband_floored")
    if false_alarm_pct > cfg.false_alarm_warn_pct:
        flags.append("high_false_alarm")
    # 한쪽 부호 센서인데 ±3σ 밴드가 0(물리적 바닥/천장)을 넘어감 = 정규 가정 붕괴
    if (s_min >= 0 and lcl < 0) or (s_max <= 0 and ucl > 0):
        flags.append("wide_limits")
    # 변동계수 과대 — 0을 걸치지 않는 센서에 한해 (잔차형 0중심 센서 오탐 방지)
    if not crosses_zero and np.isfinite(cv) and cv > cfg.cv_warn:
        flags.append("high_cv")

    return {
        "n_wafers": n_raw,
        "n_used": len(trimmed),
        "center": mu,
        "sigma": sigma,
        "ucl": ucl,
        "lcl": lcl,
        "cv": round(cv, 4) if np.isfinite(cv) else None,
        "sample_min": s_min,
        "sample_max": s_max,
        "n_distinct": int(pd.Series(spread).round(6).nunique()),
        "false_alarm_pct": round(false_alarm_pct, 3),
        "qa_flags": ";".join(flags),
        "q_lcl": q_lcl,
        "q_ucl": q_ucl,
        "resolution": resolution,
    }


def load_resolutions(meta_path) -> dict[tuple, float]:
    """meta.json의 resolutions를 `{(recipe, step, window, sensor): resolution}`로 로드.

    🔴 키는 **chamber를 뺀 4-tuple** (리뷰 H1) — CSV 산출물의 chamber는 원 챔버 `C24_0`
    하나뿐인데 라이브는 `SIM_CH_*`라, chamber 포함 키면 전 그룹 조회 miss → res_σ=0으로
    기능이 조용히 죽는다. 챔버 오프셋은 밴드 평행이동이라 값 간격(분해능)은 챔버 무관.
    재산정 호출자는 `group_key[1:]`(chamber 제외)로 조회한다. JSON 키 규약: "recipe|step|window|sensor".
    """
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    out: dict[tuple, float] = {}
    for key, res in meta.get("resolutions", {}).items():
        recipe, step, window, sensor = key.split("|")
        out[(recipe, int(step), window, sensor)] = float(res)
    return out


def compute_limits(summaries: pd.DataFrame, cfg: LimitConfig) -> pd.DataFrame:
    """그룹(Chamber x Recipe x Step x Window x 센서)별 μ, σ, UCL/LCL을 계산한다.

    **표본 분리 (σ 재보정 방식 B, 2026-08-06 — PM 승인)**: center(μ)는 그룹별 시간순
    **최근 min(cfg.rolling_n, n)장**(멘토 "rolling mean" = 현재 운영점), σ·분위수는
    **전체 복귀블록**(대표 산포)에서 계산한다. 최근 500창이 조용해 σ를 과소추정하던
    라이브 오탐 밀도의 뿌리를 고친다(±3σ 배수는 불변, σ 추정 표본만 최근→전체로 교체).
    그룹 크기 ≤ rolling_n이면 최근=전체라 기존 거동과 동일. 그룹별 산식은
    `compute_group_limits`로 공유(recalc와 동일). 반환 DataFrame은 CSV 컬럼 + resolution
    (main이 meta로 분리 저장하고 CSV에선 드롭 — DB 스키마 무변경, 결정 4-a).
    """
    group_cols = ["C24", "C6", "C7", "sensor_window", "sensor_id"]
    key_names = ["chamber_id", "recipe_id", "step", "sensor_window", "sensor_id"]

    # 선검증 게이트 ① (설계 §5): 실제 트림 표본 n≥cfg.min_trim_n — 미달 시 하드 실패 중단.
    #   소표본을 조용히 트림해 왜곡하지 않는다. 현 데이터는 전 그룹 ≥500이라 무발동(미래 안전망).
    #   실제 표본 = min(그룹크기, rolling_n)로 검사 → rolling_n<min_trim_n여도 정확(config 결합 방지).
    #   방식 B(2026-08-06): 게이트는 **center(최근 창) 트림 표본**을 지킨다. σ는 전체
    #   블록(≥ 최근 창)이라 게이트를 자동 충족 → 최근 창만 검사하면 둘 다 안전(보수적).
    #   임계는 config min_trim_n 재사용(헌법 6-1). 게이트 ② t0 연속성은 R1 필터로 구조 보장 → 미구현(§5 ② redundant).
    sizes = summaries.groupby(group_cols).size().clip(upper=cfg.rolling_n)
    small = sizes[sizes < cfg.min_trim_n]
    if len(small):
        raise RuntimeError(
            f"n<{cfg.min_trim_n} 그룹 {len(small)}건 — 트림 항상-적용 가정 위반, 재검토 필요: {small.to_dict()}")

    rows = []
    for keys, g in summaries.groupby(group_cols):
        ordered = g.sort_values("t0")["value"].to_numpy()
        recent = ordered[-cfg.rolling_n:]        # center 표본 — 최근 N창(현재 운영점)
        r = compute_group_limits(recent, cfg, spread_values=ordered)   # σ·분위수 = 전체 복귀블록
        rows.append(
            dict(zip(key_names, keys))
            | {
                "n_wafers": r["n_wafers"],
                "n_used": r["n_used"],
                "center": r["center"],
                "sigma": r["sigma"],
                "ucl": r["ucl"],
                "lcl": r["lcl"],
                "cv": r["cv"],
                "sample_min": r["sample_min"],
                "sample_max": r["sample_max"],
                "n_distinct": r["n_distinct"],
                "false_alarm_pct": r["false_alarm_pct"],
                "qa_flags": r["qa_flags"],
                "limit_version": INITIAL_LIMIT_VERSION,
                "trigger_type": "initial",
                "calc_window_n": r["n_used"],
                "k_sigma": cfg.k_sigma,
                "q_low": cfg.q_low,
                "q_high": cfg.q_high,
                "q_lcl": r["q_lcl"],
                "q_ucl": r["q_ucl"],
                "resolution": r["resolution"],
            }
        )
    limits = pd.DataFrame(rows)
    limits["step"] = limits["step"].astype(int)
    logger.info("관리선 그룹: %d건", len(limits))
    return limits


def write_qa_report(limits: pd.DataFrame, meta: dict, path: Path) -> None:
    """그룹별 QA 결과를 markdown으로 저장한다."""
    flagged = limits[limits["qa_flags"] != ""]
    lines = [
        "# 초기 실력치 v1 — QA 리포트",
        "",
        f"- 생성: {datetime.now(timezone.utc).isoformat()}",
        f"- 표본: PM 리셋({meta['reset_wafer']}) 이후 복귀 블록 {meta['return_block_wafers']}장 − "
        f"seasoning {meta['seasoning_excluded']}장 = {meta['sample_wafers']}장 "
        f"({meta['sample_period'][0]} ~ {meta['sample_period'][1]})",
        f"- 제외(실험구간): C6_1 {meta['excluded_experimental']['C6_1']['n']}장 · "
        f"실험前 C6_0 {meta['excluded_experimental']['C6_0_pre_experiment']['n']}장",
        f"- 관리선 그룹 수: {len(limits)} / QA 플래그 그룹: {len(flagged)}",
        "",
        "## 플래그 요약",
        "",
    ]
    for flag in ["zero_sigma", "high_false_alarm", "wide_limits", "high_cv"]:
        cnt = limits["qa_flags"].str.contains(flag).sum()
        lines.append(f"- `{flag}`: {cnt}건")
    lines += ["", "## 플래그 상세", ""]
    if flagged.empty:
        lines.append("(없음)")
    else:
        lines.append(
            flagged[
                ["recipe_id", "step", "sensor_window", "sensor_id",
                 "n_wafers", "center", "sigma", "cv", "false_alarm_pct", "qa_flags"]
            ].to_markdown(index=False)
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("QA 리포트 저장: %s", path)


def main() -> None:
    """초기 실력치 테이블 산출 파이프라인 실행."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="Data/문제1(하)/train_data.csv")
    parser.add_argument("--out-dir", default="src/agent_b_spc/limits")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = LimitConfig.load()
    df = load_data(Path(args.data))
    sample_wafers, meta = determine_sample_wafers(df, cfg)
    df = df[df["C64"].isin(set(sample_wafers))]
    df = add_derived_features(df)
    summaries = build_wafer_summaries(df)
    limits = compute_limits(summaries, cfg)

    # 분해능은 meta로 분리 저장(재산정 σ 단위 자 하한 — B4-3), CSV·control_limits 스키마는 무변경(결정 4-a).
    #   키 = chamber 제외 4-tuple(H1). CSV는 resolution 컬럼 드롭 후 저장(컬럼 diff 0).
    resolutions = {
        f"{t.recipe_id}|{t.step}|{t.sensor_window}|{t.sensor_id}": t.resolution
        for t in limits.itertuples(index=False)
    }
    csv_df = limits.drop(columns=["resolution"])
    csv_df["created_at"] = datetime.now(timezone.utc).isoformat()
    csv_path = out_dir / "initial_limits_v1.csv"
    csv_df.to_csv(csv_path, index=False, encoding="utf-8")
    logger.info("초기 실력치 저장: %s (%d행)", csv_path, len(csv_df))

    meta_path = out_dir / "initial_limits_v1_meta.json"
    meta |= {
        # 재현성(리뷰 §4 ①) — 이 산출물(CSV·meta·QA)을 만든 명령. 기본 인자 기준.
        "regenerate_cmd": f"python -m src.agent_b_spc.initial_limits --data '{args.data}' --out-dir '{args.out_dir}'",
        "resolution_deadband_k": cfg.resolution_deadband_k,
        "k_sigma": cfg.k_sigma,
        "rolling_n": cfg.rolling_n,
        "trim_quantile": cfg.trim_quantile,
        "sensors_settled": SENSORS_SETTLED + DERIVED_SETTLED,
        "sensors_transient_only": SENSORS_TRANSIENT_ONLY,
        "limit_version": INITIAL_LIMIT_VERSION,
        "decisions_applied": ["C6_1_baseline_excluded", "C54_C56_transient", "D_CH5960_excluded",
                              "combo2_quantile_2track", "config_migration"],
        "notes": ["C54/C56 = 과도(transient) window 감시 (2026-07-12 재정정, 7/10 정착 이동 번복·PM 승인). "
                  "관리선이 쓰는 wafer-mean 단위 std: 정착 480 vs 과도 148(3~4배) → 과도 ±3σ ~2배 예민, "
                  "오탐 둘 다 0%. 정착의 타 step(1·5·6·7)은 상수0(dead)이라 정착 감시는 step4 광폭만 남아 무력이었음. "
                  "※ C54/56은 C65 상관~0(움직임이 C62·C32 0.6~0.65에 반영) → 독립 감시 가치는 W7 Scorecard 관측 플래그."],
        # 재산정(B4-3) σ 단위 자 하한 — 키 = "recipe|step|window|sensor"(chamber 제외 H1). load_resolutions로 로드.
        "resolutions": resolutions,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    write_qa_report(limits, meta, out_dir / "qa_report_v1.md")


if __name__ == "__main__":
    main()
