"""B2-1 보완①: 센서 × Step × Window 모니터링 화이트리스트 초안 생성.

initial_limits_v1.csv(69그룹)를 읽어, 각 그룹을 Nelson 엔진(B3-1)의 감시 대상으로
쓸지 판정한다. "꺼진/준-꺼짐 센서"를 걸러내지 않으면 가성알람이 폭증하므로, 감시 대상을
명시적으로 확정하는 게 목적이다.

⚠️ 이 파일은 **초안 자동 판정**이다. 최종 감시 여부는 엔지니어(B)가 도메인 지식으로
확정한다 — `decision` 컬럼을 직접 검토·수정한 뒤 확정본으로 사용할 것.

판정 규칙 (활성도 + 크기 이중 조건):
  activity_ratio = 그룹 range / (그 센서·window의 step별 최대 range)
  mag_ratio      = |center| / (그 센서·window의 step별 최대 |center|)
  ※ 두 조건을 함께 봐야 "꺼짐(0 근처)"과 "안정적 활성(높은 값에 고정 — range 작음)"이 갈린다.
    range만 보면 C62/C63처럼 step4에서 안정적으로 활성인 센서가 오판된다.
  - EXCLUDE      : σ=0 (상수 — 물리적으로 꺼짐). 감시 불가. [자동 확정]
  - EXCLUDE?     : activity_ratio < activity_min 그리고 mag_ratio < mag_min (준-꺼짐). [제외 제안]
  - KEEP*        : 활성이나 false_alarm > 임계 (비정규 분포 — 보완②에서 관리선 방식 재검토). [유지]
  - KEEP         : 활성·정상. [유지]
  * 자동 판정 뒤 MANUAL_OVERRIDES(엔지니어 도메인 확정)를 적용한다. 예: F14로 D_VDC_RES
    C6_0 → EXCLUDE(알람 무가치·드리프트지배, 계산은 보존). 근거는 decision_reason·docs.

산출물:
  - monitoring_whitelist_v1.csv : 그룹별 판정 + 진단 수치 (B가 검토·수정할 원본)
  - monitoring_whitelist_v1.md  : 카테고리별 요약

사용법:
    python -m src.agent_b_spc.monitoring_whitelist [--limits PATH] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
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
class WhitelistConfig:
    """whitelist 판정 임계 (params.yaml `spc` 단일 소스)."""
    activity_min: float
    mag_min: float
    false_alarm_warn_pct: float

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "WhitelistConfig":
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        spc = _section(cfg, "spc", path)
        return cls(
            activity_min=float(_key(spc, "activity_min", "spc", path)),
            mag_min=float(_key(spc, "mag_min", "spc", path)),
            false_alarm_warn_pct=float(_key(spc, "false_alarm_warn_pct", "spc", path)),
        )

# 엔지니어 도메인 확정 오버라이드 — 자동 판정(decide) 이후 적용해 재현성 확보.
# 자동 규칙만으로 안 걸러지는 도메인 판단을 코드에 명시(헌법 4-3). 근거는 각 사유·docs.
#   (recipe_id, sensor_id, sensor_window, decision, reason)
# F14(2026-07-10, B): D_VDC_RES(C11−C12)는 알람 무가치 — step4에서 C11보다 열등하고
#   step5·6·7의 corr(C65) 0.78은 챔버 노후 시간추세가 만든 교란(단일레짐 +0.13/차분 −0.03).
#   자동판정은 active_normal→KEEP이나, 감시(KEEP)로 두면 드리프트 잔소리 알람이 남으므로
#   v1 감시 대상에서 제외. 계산(initial_limits)은 보존 → 향후 PM 타이밍/실력치 재캘리브
#   드리프트 트리거(B4-3/B5-1) 후보로 백로그. 상세: analysis/F14_feature_review.md.
MANUAL_OVERRIDES: list[tuple[str, str, str, str, str]] = [
    ("C6_0", "D_VDC_RES", "settled", "EXCLUDE",
     "F14_drift_dominated_no_alarm_value(→PM/recalib_trigger_backlog)"),
]


# 제외 그룹의 사각지대 대비 — ±3σ가 아닌 별도 규칙으로, 필요 시 B3-1에 추가한다.
# (현재 EXCLUDE?는 전부 C11 비플라즈마 step이므로 C11/플라즈마 관점으로 정리)
FUTURE_CONDITIONS = """## 향후 감시 조건 (제외 그룹 사각지대 대비)

C11 비플라즈마 step 제외로 놓치는 상황은 **±3σ가 아닌 별도 규칙**으로 대비한다. 아래는
양자화 노이즈에 안 걸리는 **고정 임계·교차검증** 방식이라 오탐 스팸 없이 실질 위험만 잡는다.
**추가 시점**: 시연 시나리오에 비플라즈마 step 이상이 포함되거나, 운영 중 실제 미탐이 확인될 때.
**추가 위치**: Nelson 엔진(B3-1)의 별도 규칙 계층 (실력치 ±3σ 관리선과 분리).

| 우선 | 감시 조건 | 규칙(±3σ 아님) | 근거 | 심각도 |
|---|---|---|---|---|
| 1 | 예상치 못한 플라즈마 점화 | 비플라즈마 step에서 `C11 < THRESHOLD`(예: −50V) | idle ~−1 vs plasma ~−221 — 중간값이면 이상 점화 | High |
| 2 | RF 교차검증 (모순 감지) | `C1`(RF power)≈0 인데 `\\|C11\\|` 큼 → 모순 | 플라즈마는 RF와 동행해야 함 (데이터 사전: C11=RF와 −0.978) | High |
| 3 | idle 바닥값 오프셋 이동 | idle `C11`이 고정밴드(예: −3~0) 밖 | 센서/오프셋 결함 | Low |

- **미정 파라미터**(추가 시 데이터 기반 튜닝 + config 확정): 점화 임계값(−50V 등),
  RF-off 판정 기준, idle 고정밴드. 전부 `config/params.yaml` 대상.
- **주의**: 위 조건은 현재 EXCLUDE? = C11 전제. 향후 규칙 변경으로 EXCLUDE? 센서 구성이
  바뀌면 이 표도 재작성할 것.
"""


def classify(limits: pd.DataFrame, cfg: WhitelistConfig) -> pd.DataFrame:
    """실력치 테이블에 모니터링 판정(decision·reason·activity_ratio)을 붙인다."""
    df = limits.copy()
    df["range"] = df["sample_max"] - df["sample_min"]

    # 센서 × window 별 step 간 최대 range·최대 |center| 대비 활성도·크기
    grp = df.groupby(["sensor_id", "sensor_window"])
    df["sensor_max_range"] = grp["range"].transform("max")
    df["abs_center"] = df["center"].abs()
    df["sensor_max_abs_center"] = grp["abs_center"].transform("max")
    df["activity_ratio"] = (df["range"] / df["sensor_max_range"].where(df["sensor_max_range"] > 0)).fillna(0.0).round(4)
    df["mag_ratio"] = (df["abs_center"] / df["sensor_max_abs_center"].where(df["sensor_max_abs_center"] > 0)).fillna(0.0).round(4)

    def decide(r: pd.Series) -> tuple[str, str]:
        if r["sigma"] == 0 or r["range"] == 0:
            return "EXCLUDE", "dead(상수 — 물리적 OFF)"
        if r["activity_ratio"] < cfg.activity_min and r["mag_ratio"] < cfg.mag_min:
            return "EXCLUDE?", (f"quasi_off(활성도 {r['activity_ratio']:.1%}·크기 "
                               f"{r['mag_ratio']:.1%} — 해당 step 비활성)")
        if r["false_alarm_pct"] > cfg.false_alarm_warn_pct:
            return "KEEP*", "active_nonnormal(→보완② 관리선 방식 재검토)"
        return "KEEP", "active_normal"

    decided = df.apply(decide, axis=1, result_type="expand")
    df["decision"], df["decision_reason"] = decided[0], decided[1]
    df = apply_overrides(df)
    df["method"] = np.where(df["decision"] == "KEEP*", "quantile", "sigma")
    return df


def apply_overrides(df: pd.DataFrame) -> pd.DataFrame:
    """자동 판정 위에 엔지니어 도메인 확정(MANUAL_OVERRIDES)을 덮어쓴다."""
    for recipe_id, sensor_id, window, decision, reason in MANUAL_OVERRIDES:
        mask = ((df["recipe_id"] == recipe_id) & (df["sensor_id"] == sensor_id)
                & (df["sensor_window"] == window))
        n = int(mask.sum())
        if n == 0:
            logger.warning("오버라이드 미적용(대상 없음): %s/%s/%s", recipe_id, sensor_id, window)
            continue
        df.loc[mask, "decision"] = decision
        df.loc[mask, "decision_reason"] = reason
        logger.info("오버라이드 %d행 → %s: %s/%s/%s", n, decision, recipe_id, sensor_id, window)
    return df


def write_summary(wl: pd.DataFrame, path: Path) -> None:
    """카테고리별 요약을 markdown으로 저장한다."""
    order = ["KEEP", "KEEP*", "EXCLUDE?", "EXCLUDE"]
    counts = wl["decision"].value_counts()
    monitored = counts.get("KEEP", 0) + counts.get("KEEP*", 0)
    lines = [
        "# 모니터링 화이트리스트 v1",
        "",
        f"- 생성: {datetime.now(timezone.utc).isoformat()}",
        f"- 총 {len(wl)}그룹 → **감시(KEEP/KEEP\\*) {monitored}** / "
        f"제외(EXCLUDE?/EXCLUDE) {counts.get('EXCLUDE?',0)+counts.get('EXCLUDE',0)}",
        "",
        "> **소비 규칙**: Nelson 엔진(B3-1)은 `KEEP`·`KEEP*`만 감시한다. "
        "`EXCLUDE`·`EXCLUDE?`는 감시하지 않는다.",
        "",
        "## 결정 기록",
        "",
        "- **EXCLUDE?(C11 비플라즈마 step 1·5·6·7, 8그룹) → 제외 확정** (B, 2026-07-07). "
        "사유: Vdc는 플라즈마 ON에서만 유효 — 비플라즈마 step은 −1~−2 양자화라 ±3σ 무효, "
        "자기 표본 오탐 4~6%(step1·7)가 `fdc.alert`·Incident를 오염. "
        "사각지대(예상치 못한 점화)는 아래 '향후 감시 조건'으로 별도 대비.",
        "- EXCLUDE(상수) → 자동 제외 확정.",
        "- **D_VDC_RES(C6_0 정착 5그룹) → 감시 제외** (B, F14, 2026-07-10). "
        "자동판정은 KEEP(active_normal)이나, 알람 가치 없음이 확인됨: step4에서 C11보다 열등, "
        "step5·6·7 corr(C65) 0.78은 챔버 노후 시간추세 교란(단일레짐 +0.13·차분 −0.03). "
        "**계산(initial_limits)은 보존** — 향후 PM 타이밍/실력치 재캘리브 드리프트 트리거"
        "(B4-3/B5-1) 후보로 백로그. 상세: `analysis/F14_feature_review.md`.",
        "",
        "## 판정별 그룹 수",
        "",
        "| 판정 | 뜻 | 그룹 수 |",
        "|---|---|---|",
        f"| KEEP | 활성·정상 → 감시 | {counts.get('KEEP',0)} |",
        f"| KEEP* | 활성·비정규 → 감시하되 보완② | {counts.get('KEEP*',0)} |",
        f"| EXCLUDE? | 준-꺼짐 → 제외 제안(검토) | {counts.get('EXCLUDE?',0)} |",
        f"| EXCLUDE | 상수 → 제외 확정 | {counts.get('EXCLUDE',0)} |",
        "",
    ]
    for dec in order:
        sub = wl[wl["decision"] == dec]
        if sub.empty:
            continue
        lines += [f"## {dec} ({len(sub)}그룹)", ""]
        lines.append(
            sub.sort_values(["sensor_id", "step"])[
                ["recipe_id", "step", "sensor_window", "sensor_id",
                 "n_distinct", "range", "activity_ratio", "false_alarm_pct", "decision_reason"]
            ].to_markdown(index=False)
        )
        lines.append("")
    lines.append(FUTURE_CONDITIONS)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("요약 저장: %s", path)


def main() -> None:
    """화이트리스트 초안 생성 파이프라인 실행."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limits", default="src/agent_b_spc/limits/initial_limits_v1.csv")
    parser.add_argument("--out-dir", default="src/agent_b_spc/limits")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = WhitelistConfig.load()
    limits = pd.read_csv(args.limits)
    wl = classify(limits, cfg)

    cols = ["chamber_id", "recipe_id", "step", "sensor_window", "sensor_id",
            "decision", "method", "decision_reason", "activity_ratio", "mag_ratio", "n_distinct",
            "range", "sigma", "cv", "false_alarm_pct", "qa_flags"]
    out_csv = Path(args.out_dir) / "monitoring_whitelist_v1.csv"
    wl[cols].to_csv(out_csv, index=False, encoding="utf-8")
    logger.info("화이트리스트 저장: %s (%d행)", out_csv, len(wl))

    write_summary(wl, Path(args.out_dir) / "monitoring_whitelist_v1.md")

    counts = wl["decision"].value_counts()
    logger.info("판정 분포: %s", counts.to_dict())


if __name__ == "__main__":
    main()
