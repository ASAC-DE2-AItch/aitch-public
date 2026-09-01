# -*- coding: utf-8 -*-
"""ae_baseline_ladder.py — AE 베이스라인 레짐 대조 + 오프셋·노이즈 적정 (A 실험 도구).

**왜 만들었나 (2026-08-05)**: PM `_ae_offline_ab.py` 의 A/B 대조가 기준선을
`order[3692:3992]` 하드코딩 인덱스로 뽑았는데, 그 3692가 **정확히 C33 리셋 지점**이라
기준선 300장이 전부 **리셋 후** 구간이었다 (모델 훈련셋과 교집합 0). 그래서 "A 원본"의
raw 215 가 정상값이 아니라 **레짐 밖 값**이었고, 그 위에서 잰 오프셋·노이즈 증분만으로는
포화 원인을 배분할 수 없다.

이 스크립트는 ⓐ 경계를 **데이터에서 산출**하고(하드코딩 금지) ⓑ 리셋 **직전/직후**를
같은 조건으로 대조해 "레짐 성분"을 분리한 뒤 ⓒ 그 위에 오프셋·노이즈를 적정한다.

⚠️ **용어 충돌 주의 (이 스크립트가 매번 경고로 출력한다)**
    AE  (`ae_pipeline.features.find_regime_boundary`) : 리셋 **전** = seg1 / 리셋 **후** = seg2
    시뮬(`simulator.message_builder.filter_post_reset`): 리셋 **전** = seg0 / 리셋 **후** = seg1
  같은 "seg1"이 반대 구간을 가리킨다. 배포 번들 `z16_rev3_seg1` 은 **AE 기준 seg1(리셋 전)**
  학습이고, `config/params.yaml` `replay_post_reset_only: true` 는 리셋 **후**만 발행한다.
  → 혼동을 막기 위해 본 스크립트는 seg 번호를 쓰지 않고 **PRE / POST** 로만 표기한다.

헌법: 6-1(로깅·매직넘버 상수화) · 1-3(타겟 C65 미사용 — 추론 전용) · 4-1(모델 경로 인자화).

사용:
  # ① 기본 — PRE/POST 베이스라인 대조 (15분 작업의 본체)
  python scripts/ae_baseline_ladder.py \
      --data "Data/문제1(하)/train_data.csv" --bundle models/anomaly_ae/ae_v1

  # ② 캐터펄트 피처 대조 추가 (클립 셀·피처별 Δz 랭킹)
  python scripts/ae_baseline_ladder.py ... --catapult

  # ③ 오프셋·노이즈 적정 사다리 (closure_report 재현·확장)
  python scripts/ae_baseline_ladder.py ... --ladder --ladder-window post

  # ④ 리포트 저장
  python scripts/ae_baseline_ladder.py ... --catapult --ladder --out outputs/ae_baseline.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
AE_PKG_ROOT = REPO_ROOT / "src" / "agent_a_mlops" / "Autoencoder"
sys.path.insert(0, str(AE_PKG_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

log = logging.getLogger("ae.baseline")

# ── 상수 (헌법 6-1: 매직 넘버 금지) ─────────────────────────────────────────
DEFAULT_COUNT = 300              # 창당 wafer 수 (PM 실험 300장과 정합)
QT_CLIP_ABS = 5.19               # QuantileTransformer(normal) 클립 경계 근사 (norm.ppf(1e-7)≈5.199)
CATAPULT_TOP_N = 15              # 캐터펄트 명단 출력 개수 (closure_report §4와 동일)
SAT_THRESHOLD = 0.999            # 포화 판정 (ae_score)
DEFAULT_OFFSET_SIGMA = 0.1       # 적정 기본 오프셋 (PM closure_report params와 동일)
DEFAULT_NOISE_GRID = [0.0, 0.001, 0.01, 0.05]   # 적정 노이즈 격자
REPLICATOR_SEED = 42             # ChamberReplicator 기본 시드 (재현성)
LADDER_CHAMBER = "SIM_CH_4"      # 적정 대상 가상 챔버 (B_platform_trace 와 동일)
# 시뮬이 섭동하지 않는 컬럼 — import 실패 시 폴백 (정본은 simulator.chamber_replicator)
NON_PERTURB_FALLBACK = {
    "C6", "C7", "C10", "C20", "C22", "C23", "C33", "C34", "C39",
    "C41", "C42", "C46", "C50", "C59", "C60", "C64",
}
# 이산(격자) 채널 — 연속 노이즈가 값 도메인을 깨는 대상. 진단 출력용.
DISCRETE_CHANNELS = ["C49", "C54", "C56", "C50"]
DISCRETE_VALUES_SHOWN = 6        # 값 목록 출력 개수 (개수만 보고 오독하는 것 방지)
DISCRETE_CONTINUOUS_HINT = 100   # 이 이상이면 격자 붕괴(연속화) 의심 표기


# ── 데이터 로딩 ────────────────────────────────────────────────────────────
def load_frame(data_path: Path) -> pd.DataFrame:
    """parquet/csv 로딩 + C46 부재 시 합성 (rows_to_trace_ae 동형).

    Raises:
        ValueError: 정렬·그룹핑에 필수인 컬럼이 없으면 중단 (조용한 진행 금지).
    """
    if data_path.suffix == ".parquet":
        df = pd.read_parquet(data_path)
    else:
        df = pd.read_csv(data_path, low_memory=False)

    missing = [c for c in ("C64", "C7", "C42", "C10", "C33") if c not in df.columns]
    if missing:
        raise ValueError(f"필수 컬럼 부재 {missing}: {data_path}")

    if "C46" not in df.columns:
        log.warning("C46 부재 → (C64,C7) 내 순번으로 합성 (rows_to_trace_ae 동형). "
                    "dedup 키가 행마다 유일해져 머지 아티팩트 가드(불가침 11)는 적용 대상 0이 된다.")
        df = df.sort_values(["C64", "C7", "C10"], kind="mergesort")
        df["C46"] = df.groupby(["C64", "C7"], sort=False).cumcount() + 1
    if "C24" not in df.columns:
        log.warning("C24 부재 → 'default' 로 채움 (드리프트 트래커 키 전용, 피처 아님)")
        df["C24"] = "default"
    return df


# ── 레짐 경계 (하드코딩 금지 — 데이터에서 산출) ──────────────────────────────
def wafer_time_order(df: pd.DataFrame) -> list:
    """wafer 를 최초 관측 시각(C10 min) 순으로 정렬한 목록 (`features.standard_sort` 정합)."""
    return df.groupby("C64", sort=False)["C10"].min().sort_values(kind="mergesort").index.tolist()


def wafer_file_order(df: pd.DataFrame) -> list:
    """파일 등장 순서 그대로의 wafer 목록 (`df["C64"].drop_duplicates()`).

    **왜 따로 두나**: PM `_ae_offline_ab.py` 가 이 순서로 `order[3692:3992]` 를 잘랐다.
    실측(2026-08-05) — 두 순서는 11,939장 중 **11,900장이 서로 다른 위치**에 있다
    (단조 상관 0.9991 = 국소 뒤섞임). 스코어링은 `standard_sort` 가 시간순으로 되돌리므로
    무영향이지만, **창 선택은 달라진다**. PM 창을 그대로 재현하려면 `--order file`.
    """
    return df["C64"].drop_duplicates().tolist()


def reset_indices(df: pd.DataFrame, drop_ratio: float = 0.5) -> tuple[list, list]:
    """C33 리셋 지점 산출 — (AE 방식 인덱스, 시뮬 방식 인덱스).

    두 코드베이스가 서로 다른 규칙을 쓴다. 어긋나면 같은 데이터에서 다른 경계가 나오므로
    **둘 다 계산해 비교**한다 (조용한 불일치 금지).
      · AE   `find_regime_boundary`  : diff <= -2  → **첫** 리셋을 경계로 사용
      · 시뮬 `last_reset_wafer_index`: cur < prev*0.5 → **마지막** 리셋을 경계로 사용

    Returns:
        (ae_hits, sim_hits) — 각각 시간순 wafer 인덱스(0-based) 리스트.
    """
    order = wafer_time_order(df)
    c33 = df.groupby("C64", sort=False)["C33"].first().reindex(order).to_numpy(dtype=float)
    ae_hits = list(np.where(np.diff(c33, prepend=c33[0]) <= -2)[0])
    sim_hits = [i for i in range(1, len(c33))
                if np.isfinite(c33[i - 1]) and np.isfinite(c33[i])
                and c33[i - 1] > 0 and c33[i] < c33[i - 1] * drop_ratio]
    return ae_hits, sim_hits


def resolve_boundary(df: pd.DataFrame, order_mode: str = "time") -> dict:
    """레짐 경계 확정 + 두 용어계 병기 리포트.

    Args:
        order_mode: 'time'(기본 — 시간순, 옳은 창) 또는 'file'(PM 창 재현용 파일 순서).
            경계 판정은 **항상 시간순**으로 하고(리셋은 시간 사건), 창 절단만 order_mode 를 따른다.
    """
    order = wafer_time_order(df) if order_mode == "time" else wafer_file_order(df)
    if order_mode not in ("time", "file"):
        raise ValueError(f"order_mode 는 'time' 또는 'file' — 받은 값: {order_mode!r}")
    ae_hits, sim_hits = reset_indices(df)
    if not ae_hits and not sim_hits:
        raise ValueError("C33 리셋을 찾지 못했습니다 — 레짐 경계 없이는 PRE/POST 대조가 불가합니다.")

    ae_b = int(ae_hits[0]) if ae_hits else None
    sim_b = int(sim_hits[-1]) if sim_hits else None
    if ae_b is not None and sim_b is not None and ae_b != sim_b:
        log.warning("★경계 불일치 — AE(첫 리셋)=%d · 시뮬(마지막 리셋)=%d. 리셋이 %d회 있습니다. "
                    "두 코드가 서로 다른 구간을 '정상'이라 부르게 됩니다.",
                    ae_b, sim_b, max(len(ae_hits), len(sim_hits)))
    boundary = ae_b if ae_b is not None else sim_b

    log.info("─" * 72)
    log.info("레짐 경계: 시간순 wafer 인덱스 %d / 전체 %d장 (리셋 %d회)",
             boundary, len(order), max(len(ae_hits), len(sim_hits)))
    log.info("  PRE  (리셋 전) = AE 용어 'seg1'  · 시뮬 용어 'seg0'  → %d장", boundary)
    log.info("  POST (리셋 후) = AE 용어 'seg2'  · 시뮬 용어 'seg1'  → %d장", len(order) - boundary)
    log.info("  ⚠ 배포 번들 z16_rev3_seg1 = AE 기준 seg1 = **PRE** 학습")
    log.info("  ⚠ params.yaml replay_post_reset_only: true = **POST** 만 발행")
    if order_mode == "file":
        log.warning("  ⚠ order_mode='file' — PM 창 재현 모드입니다. 파일 순서는 시간순과 다르므로 "
                    "(실측 11,900/11,939장 위치 상이) 창 경계가 시간축과 어긋납니다. "
                    "정식 측정은 기본값 'time' 으로 하세요.")
    log.info("─" * 72)
    return {"boundary": boundary, "n_wafers": len(order), "order_mode": order_mode,
            "ae_reset_hits": [int(x) for x in ae_hits], "sim_reset_hits": [int(x) for x in sim_hits],
            "order": order}


def pick_window(order: list, boundary: int, side: str, count: int) -> list:
    """경계 기준 창 선택. side='pre' = 리셋 **직전** count장 / 'post' = 리셋 **직후** count장."""
    if side == "pre":
        lo = max(0, boundary - count)
        return order[lo:boundary]
    if side == "post":
        return order[boundary:boundary + count]
    raise ValueError(f"side 는 'pre' 또는 'post' — 받은 값: {side!r}")


# ── 스코어링 ──────────────────────────────────────────────────────────────
def score_window(model, df: pd.DataFrame, wafers: list, label: str) -> dict:
    """창 하나 스코어링 → 요약 통계. 드리프트 상태는 창마다 리셋(창 간 오염 방지)."""
    model.reset_drift()
    sub = df[df["C64"].isin(set(wafers))]
    recs = model.score_wafers(sub, chamber_col="C24", with_drift=False)
    if not recs:
        raise ValueError(f"[{label}] 스코어링 결과 0건 — wafer id 대응을 확인하세요.")
    raw = np.array([r["ae_raw"] for r in recs], float)
    sc = np.array([r["ae_score"] for r in recs], float)
    qual = float(model.calib.get("qual_threshold", 0.2))
    return {
        "label": label, "n": len(recs),
        "raw_p50": float(np.percentile(raw, 50)), "raw_p90": float(np.percentile(raw, 90)),
        "raw_max": float(raw.max()),
        "score_p50": float(np.percentile(sc, 50)), "score_mean": float(sc.mean()),
        "score_p95": float(np.percentile(sc, 95)),
        "sat": int((sc >= SAT_THRESHOLD).sum()),
        "over_qual": int((sc > qual).sum()), "qual": qual,
    }


def describe_vs_anchors(raw_value: float, calib: dict) -> str:
    """raw 값이 캘리 앵커 기준 어느 분위인지 사람이 읽을 문장으로."""
    xs, ps = calib.get("anchors_x", []), calib.get("anchors_percentile", [])
    if not xs:
        return "앵커 정보 없음"
    if raw_value <= xs[0]:
        return f"P{ps[0]:g} 이하 (훈련 분포 중앙 이하)"
    for i in range(len(xs) - 1):
        if xs[i] <= raw_value <= xs[i + 1]:
            return f"P{ps[i]:g}({xs[i]:.1f}) ~ P{ps[i+1]:g}({xs[i+1]:.1f}) 구간"
    return f"P{ps[-1]:g}({xs[-1]:.1f}) 초과 — 캘리 상한 밖(포화)"


def report_windows(model, results: list) -> None:
    """PRE/POST 대조표 출력 + 레짐 성분 판정."""
    log.info("")
    log.info("== 베이스라인 대조 (섭동 없음 — 원본 값 그대로) ==")
    log.info("%-28s %6s %9s %9s %9s %7s %9s", "창", "n", "raw p50", "raw p90",
             "score p50", "sat", ">qual")
    for r in results:
        log.info("%-28s %6d %9.1f %9.1f %9.3f %4d/%-4d %5d",
                 r["label"], r["n"], r["raw_p50"], r["raw_p90"], r["score_p50"],
                 r["sat"], r["n"], r["over_qual"])
    log.info("")
    for r in results:
        log.info("  %-26s raw p50 %.1f → %s", r["label"], r["raw_p50"],
                 describe_vs_anchors(r["raw_p50"], model.calib))

    pre = next((r for r in results if r["label"].startswith("PRE")), None)
    post = next((r for r in results if r["label"].startswith("POST")), None)
    if pre and post:
        log.info("")
        log.info("  ▶ 레짐 성분 = POST raw %.1f − PRE raw %.1f = **%+.1f**",
                 post["raw_p50"], pre["raw_p50"], post["raw_p50"] - pre["raw_p50"])
        if pre["raw_p50"] <= float(model.calib["anchors_x"][1]):      # P90 이하
            log.info("     PRE 가 훈련 분포 안(P90 이하)에 있습니다 → **레짐 불일치 가설 확정**. "
                     "오프셋·노이즈를 다 고쳐도 위 차이는 남습니다.")
        else:
            log.warning("     ★PRE 도 이미 높습니다(P90 초과) → 레짐 말고 다른 성분이 더 있습니다. "
                        "번들 학습셋과 이 데이터 파일이 같은 원천인지 확인하세요.")


# ── 캐터펄트 진단 (스케일 후 z 공간) ────────────────────────────────────────
def transformed_z(model, df: pd.DataFrame, wafers: list) -> tuple[np.ndarray, list]:
    """창 → 스케일 변환된 피처 행렬 X (QT 출력 z). (X, wafer_order) 반환."""
    from ae_pipeline.features import prepare_frame, extract_features

    sub = df[df["C64"].isin(set(wafers))]
    d, _ = prepare_frame(sub)
    order = d.groupby("C64")["C10"].min().sort_values(kind="mergesort").index.tolist()
    F = extract_features(d).reindex(order).fillna(0.0)
    return model._align_transform(F), order


def catapult_report(model, df_a: pd.DataFrame, wa: list, df_b: pd.DataFrame, wb: list,
                    label_a: str, label_b: str) -> dict:
    """두 조건의 피처별 평균 z 차이 랭킹 + 클립 셀 비율 (closure_report §4 확장).

    (df_a, wa) 와 (df_b, wb) 는 **다른 창**(PRE vs POST)일 수도, **같은 창의 다른 조건**
    (원본 vs 섭동)일 수도 있다. 후자면 wafer 가 짝지어져 비교가 더 정확하다.

    ★가중 미적용 랭킹을 정본으로 쓴다: `ae_raw = dᵀPd` 는 채널가중 `w_vec` 를 **쓰지 않는데**
    `Scorer.top_channels` 는 쓴다. C49 가중이 0.3 이라 점수를 밀어올린 채널이 top_channels
    상위에 안 뜰 수 있다. 참고용으로 가중 적용 랭킹도 함께 낸다.
    """
    from ae_pipeline.constants import feat_weight_vector

    Xa, _ = transformed_z(model, df_a, wa)
    Xb, _ = transformed_z(model, df_b, wb)
    cols = model.feat_cols
    za, zb = Xa.mean(0), Xb.mean(0)
    dz = np.abs(zb - za)
    w = feat_weight_vector(cols)

    rank = np.argsort(dz)[::-1][:CATAPULT_TOP_N]
    rank_w = np.argsort(dz * w)[::-1][:CATAPULT_TOP_N]
    clip_a = float(np.mean(np.abs(Xa) >= QT_CLIP_ABS))
    clip_b = float(np.mean(np.abs(Xb) >= QT_CLIP_ABS))

    log.info("")
    log.info("== 캐터펄트 피처 (%s → %s · 가중 미적용 = ae_raw 정합) ==", label_a, label_b)
    for j in rank:
        log.info("  %-24s |Δz|=%6.2f   z(%s)=%+6.2f -> z(%s)=%+6.2f   w=%.1f",
                 cols[j], dz[j], label_a, za[j], label_b, zb[j], w[j])
    log.info("  참고 — 가중 적용(top_channels 관점) 상위: %s",
             ", ".join(cols[j] for j in rank_w[:8]))
    log.info("  클립 셀(|z|≥%.2f): %s %.1f%% → %s %.1f%%   (총 %d셀)",
             QT_CLIP_ABS, label_a, clip_a * 100, label_b, clip_b * 100, Xa.size)

    share = float(dz[rank[0]] ** 2 / max((dz ** 2).sum(), 1e-12))
    log.info("  1위 피처 단독 기여(Δz² 비중): %.1f%% — 나머지 %.1f%% 는 다른 피처군입니다.",
             share * 100, (1 - share) * 100)
    return {"top": [{"feature": cols[j], "dz": round(float(dz[j]), 3),
                     "z_a": round(float(za[j]), 3), "z_b": round(float(zb[j]), 3),
                     "w": float(w[j])} for j in rank],
            "clip_rate_a": round(clip_a, 4), "clip_rate_b": round(clip_b, 4),
            "top1_dz2_share": round(share, 4)}


def discrete_channel_report(df: pd.DataFrame, wafers: list, label: str) -> dict:
    """이산 채널의 값 도메인 진단 — 연속화 여부를 한눈에 (C49 스모킹건).

    ⚠️ **고유값 개수만 보고 레짐 차이로 읽지 말 것** (2026-08-05 실측 오독 사례): C49 는
    희귀 준위가 있어(`−23` 0.5% · `−35` 0.02%) 300장 창에는 안 들어올 수 있다. PRE 창
    2개 / POST 창 3개는 **표본 크기 효과**였고, 전체로 보면 PRE 3개 / POST 4개다.
    그래서 개수와 함께 **실제 값 목록**을 찍는다 — 개수만으로는 판정하지 않는다.

    판정에 쓸 신호는 `within_wafer_switch_rate` 다. 원본은 격자값이라 0% 이고, 시뮬 노이즈가
    연속화하면 100% 가 된다 (실측: `ae_input_sim_v2.csv` C49 고유값 20,787 = 전 행).
    """
    sub = df[df["C64"].isin(set(wafers))]
    s4 = sub[sub["C7"] == 4]
    out = {}
    log.info("")
    log.info("== 이산 채널 도메인 진단 [%s] ==", label)
    for c in DISCRETE_CHANNELS:
        if c not in sub.columns:
            continue
        vals = sub[c].dropna()
        uniq = np.sort(vals.unique())
        nuniq = int(len(uniq))
        switch = float((s4.groupby("C64")[c].nunique() > 1).mean()) if len(s4) else float("nan")
        shown = ("[" + ", ".join(f"{v:g}" for v in uniq[:DISCRETE_VALUES_SHOWN]) +
                 (", …]" if nuniq > DISCRETE_VALUES_SHOWN else "]"))
        out[c] = {"n_unique": nuniq, "within_wafer_switch_rate": round(switch, 4),
                  "values": [float(v) for v in uniq[:DISCRETE_VALUES_SHOWN]]}
        flag = ("  ← ★연속화 (격자 붕괴 — 시뮬 노이즈 의심)"
                if nuniq > DISCRETE_CONTINUOUS_HINT else "")
        log.info("  %-5s 고유값 %7d개 %-34s wafer 내 Step4 변동 %5.1f%%%s",
                 c, nuniq, shown, switch * 100, flag)
    log.info("  ※ 고유값 '개수' 차이는 희귀 준위의 표본 효과일 수 있다 — 판정은 변동 비율로.")
    return out


# ── 오프셋·노이즈 적정 (ChamberReplicator 충실) ─────────────────────────────
def _non_perturb_columns() -> set:
    """시뮬 정본에서 섭동 제외 컬럼을 가져온다 (실패 시 폴백 + 경고)."""
    try:
        from simulator.chamber_replicator import NON_PERTURB_COLUMNS
        return set(NON_PERTURB_COLUMNS)
    except Exception as e:                                   # noqa: BLE001
        log.warning("simulator.chamber_replicator import 실패 → 폴백 목록 사용 "
                    "(정본과 어긋날 수 있음): %s", e)
        return set(NON_PERTURB_FALLBACK)


def perturb(df_win: pd.DataFrame, stats_df: pd.DataFrame, offset_sigma: float,
            noise_sigma: float, seed: int = REPLICATOR_SEED) -> pd.DataFrame:
    """ChamberReplicator 와 같은 규칙으로 오프셋·노이즈 적용 (재현 가능).

    offset = std × U(−offset_sigma, +offset_sigma) [챔버 고정] · noise = std × N(0, noise_sigma) [행마다]
    """
    non_perturb = _non_perturb_columns()
    rng = np.random.default_rng(seed)
    numeric = stats_df.select_dtypes(include=[np.number]).columns
    std = {c: float(stats_df[c].std()) for c in numeric
           if c not in non_perturb and float(stats_df[c].std()) > 0}
    offsets = {c: float(rng.uniform(-offset_sigma, offset_sigma) * s) for c, s in std.items()}

    out = df_win.copy()
    for c, s in std.items():
        if c not in out.columns:
            continue
        noise = rng.normal(0.0, noise_sigma * s, size=len(out)) if noise_sigma > 0 else 0.0
        out[c] = out[c] + offsets[c] + noise
    out["C24"] = LADDER_CHAMBER
    return out


def ladder_report(model, df: pd.DataFrame, wafers: list, stats_df: pd.DataFrame,
                  offset_sigma: float, noise_grid: list, label: str) -> list:
    """적정 사다리 — 오프셋/노이즈를 하나씩 넣어 raw 증분을 잰다 (closure_report 재현)."""
    win = df[df["C64"].isin(set(wafers))]
    rows = []
    log.info("")
    log.info("== 오프셋·노이즈 적정 [%s · offset_sigma=%.3f] ==", label, offset_sigma)
    log.info("%-34s %9s %9s %7s", "조건", "raw p50", "score p50", "sat")

    combos = [("원본 (섭동 없음)", 0.0, 0.0)]
    combos += [(f"노이즈만 {n:g}", 0.0, n) for n in noise_grid if n > 0]
    combos += [(f"오프셋만 {offset_sigma:g}σ", offset_sigma, 0.0)]
    combos += [(f"오프셋 {offset_sigma:g}σ + 노이즈 {n:g}", offset_sigma, n)
               for n in noise_grid if n > 0]

    for name, off, noi in combos:
        d = win if (off == 0.0 and noi == 0.0) else perturb(win, stats_df, off, noi)
        try:
            r = score_window(model, d, wafers, name)
        except Exception as e:                               # noqa: BLE001
            log.warning("  [%s] 스코어링 실패 → 스킵: %s", name, e)
            continue
        log.info("%-34s %9.1f %9.3f %4d/%-4d", name, r["raw_p50"], r["score_p50"],
                 r["sat"], r["n"])
        rows.append({"condition": name, "offset_sigma": off, "noise_sigma": noi,
                     "raw_p50": round(r["raw_p50"], 1), "score_p50": round(r["score_p50"], 4),
                     "sat": r["sat"], "n": r["n"]})
    base = rows[0]["raw_p50"] if rows else None
    if base is not None:
        log.info("")
        log.info("  ▶ 증분 배분 (원본 raw %.1f 기준)", base)
        for r in rows[1:]:
            log.info("     %-32s %+8.1f", r["condition"], r["raw_p50"] - base)
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────
# ── 엔진 (torch / numpy 폴백) ──────────────────────────────────────────────
WEIGHTS_NPZ = "model_weights.npz"      # scripts/ae_export_weights.py 산출 (번들 옆 사이드카)
FORWARD_TOL = 1e-4                     # numpy 순전파 ↔ torch 기준 출력 허용 오차


def _gelu(x: np.ndarray) -> np.ndarray:
    """torch `nn.GELU()` 기본(approximate='none') = 정확 erf 기반."""
    try:
        from scipy.special import erf                     # sklearn 의존이라 사실상 상주
    except ImportError:                                   # 최후 폴백 (느리지만 동일)
        import math
        erf = np.vectorize(math.erf)
    return 0.5 * x * (1.0 + erf(x / np.sqrt(2.0)))


def _layer_norm(x: np.ndarray, w: np.ndarray, b: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    """torch `nn.LayerNorm` (마지막 축, 편향 분산, eps 기본 1e-5)."""
    mu = x.mean(-1, keepdims=True)
    var = x.var(-1, keepdims=True)
    return (x - mu) / np.sqrt(var + eps) * w + b


class NumpyMLPAE:
    """`ae_pipeline.model.MLPAE` 순전파의 numpy 복제 (torch 무의존 · eval 모드).

    **왜 필요한가 (2026-08-05)**: Windows WDAC 가 `torch/lib/shm.dll` 로드를 차단해
    번들 스코어링이 불가능해졌다(TSR-0001 변종 — detached 가 아니라 foreground 에서 발생).
    보안 정책 변경은 범위 밖(TSR-0001 기각 대안 ②)이므로, **모델을 numpy 로 옮겨** 진단을
    계속할 수 있게 한다. Dropout 은 eval 모드에서 항등이라 재현에 영향 없다.

    가중치는 `scripts/ae_export_weights.py` 가 torch 가 살아있는 환경에서 1회 추출한
    `model_weights.npz` 에서 읽는다. 같은 파일에 **torch 기준 순전파 출력**이 들어 있어
    로드 시 대조 검증한다 — 수치가 어긋나면 조용히 쓰지 않고 예외를 던진다
    (§7 "예외가 안 났으니 성공으로 간주 금지").
    """

    # (종류, state_dict 키) 순서 — MLPAE 정의와 1:1. Dropout(enc.3)은 eval 항등이라 생략.
    ENC_OPS = [("lin", "enc.0"), ("gelu", None), ("ln", "enc.2"),
               ("lin", "enc.4"), ("gelu", None), ("ln", "enc.6"), ("lin", "enc.7")]
    DEC_OPS = [("lin", "dec.0"), ("gelu", None), ("ln", "dec.2"),
               ("lin", "dec.3"), ("gelu", None), ("ln", "dec.5"), ("lin", "dec.6")]

    def __init__(self, params: dict):
        self.p = params

    def _run(self, x: np.ndarray, ops) -> np.ndarray:
        for kind, key in ops:
            if kind == "gelu":
                x = _gelu(x)
            elif kind == "lin":
                x = x @ self.p[f"{key}.weight"].T + self.p[f"{key}.bias"]
            else:
                x = _layer_norm(x, self.p[f"{key}.weight"], self.p[f"{key}.bias"])
        return x

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        return self._run(self._run(x, self.ENC_OPS), self.DEC_OPS)

    @classmethod
    def from_npz(cls, path: Path) -> "NumpyMLPAE":
        """가중치 npz 로드 + torch 기준 출력 대조 (불일치 시 예외)."""
        z = np.load(path, allow_pickle=False)
        params = {k: np.asarray(z[k], dtype=np.float32) for k in z.files
                  if k.startswith(("enc.", "dec."))}
        m = cls(params)
        if "x_ref" in z.files and "y_ref" in z.files:
            err = float(np.abs(m(z["x_ref"]) - z["y_ref"]).max())
            if err > FORWARD_TOL:
                raise ValueError(
                    f"numpy 순전파가 torch 기준과 어긋납니다 (max|Δ|={err:.3e} > {FORWARD_TOL}). "
                    f"가중치·아키텍처 불일치 — 재추출하세요: {path}")
            log.info("numpy 엔진 순전파 검증 OK (torch 기준 대비 max|Δ|=%.2e)", err)
        else:
            log.warning("가중치 npz 에 기준 출력(x_ref/y_ref)이 없어 순전파를 검증하지 못했습니다. "
                        "scripts/ae_export_weights.py 로 재추출을 권합니다.")
        return m


class NumpyScorer:
    """`ae_pipeline.model.Scorer` 의 torch 무의존 복제 — `scorer.npz` 통계 그대로 사용."""

    def __init__(self, model: NumpyMLPAE, stats_path: Path):
        z = np.load(stats_path, allow_pickle=True)
        self.model = model
        self.feat_cols = [str(c) for c in z["feat_cols"]]
        self.w_vec = np.asarray(z["w_vec"], float)
        self.mu = np.asarray(z["mu"], float)
        self.sd = np.asarray(z["sd"], float)
        self.P = np.asarray(z["P"], float)

    def _resid(self, X):
        X = np.asarray(X, float)
        return X - np.asarray(self.model(X), float)

    def ae_raw(self, X) -> np.ndarray:
        """조건화 잔차 Mahalanobis² — `Scorer.ae_raw` 와 동일 식 (w_vec 미사용)."""
        d = self._resid(X) - self.mu
        return np.einsum("ij,jk,ik->i", d, self.P, d)

    def recon_rmse(self, X) -> float:
        """채널가중 재구성 RMSE."""
        r = self._resid(X)
        return float(np.sqrt((self.w_vec * r ** 2).mean()))

    def top_channels(self, X, k: int = 5):
        """잔차 상위 k채널 (가중 적용 — 원본 `Scorer` 와 동일 관례)."""
        z = self.w_vec * ((self._resid(X) - self.mu) / self.sd) ** 2
        return [[self.feat_cols[j] for j in row[::-1]] for row in np.argsort(z, 1)[:, -k:]]


class NumpyAEModel:
    """`ae_pipeline.infer.AEModel` 과 같은 인터페이스의 torch 무의존 구현."""

    engine = "numpy"

    def __init__(self, bundle_dir: Path):
        from ae_pipeline import io_bundle
        from ae_pipeline.calibration import apply_calibration, load_calibration

        A = io_bundle.ARTIFACT_NAMES
        w_path = bundle_dir / WEIGHTS_NPZ
        if not w_path.exists():
            raise FileNotFoundError(
                f"{WEIGHTS_NPZ} 없음: {w_path}\n"
                "torch 가 동작하는 환경에서 1회 추출하세요:\n"
                f"  python scripts/ae_export_weights.py --bundle {bundle_dir}")
        self._apply_calibration = apply_calibration
        self.model = NumpyMLPAE.from_npz(w_path)
        self.scorer = NumpyScorer(self.model, bundle_dir / A["scorer"])
        self.scaler = io_bundle.load_scaler(bundle_dir / A["scaler"])
        self.calib = load_calibration(bundle_dir / A["calib"])
        self.feat_cols = list(io_bundle.load_json(bundle_dir / A["feature_spec"])["columns"])
        man = io_bundle.load_json(bundle_dir / A["manifest"])
        self.model_version = man.get("ae_model_version", "unknown")
        self.calib_version = man.get("ae_calib_version", "unknown")

    def reset_drift(self) -> None:
        """드리프트 상태 없음 — 인터페이스 호환용 no-op (본 스크립트는 with_drift=False)."""

    def _align_transform(self, F):
        """피처표 → 번들 컬럼 순서로 정렬 후 스케일 변환 (AEModel 과 동일)."""
        return self.scaler.transform(F.reindex(columns=self.feat_cols, fill_value=0.0))

    def score_wafers(self, df_rows, chamber_col: str = "C24", with_drift: bool = False, k: int = 5):
        """원본 행 → per-wafer 레코드 (ae_score·ae_raw·ae_top_channels). 시간순."""
        from ae_pipeline.features import prepare_frame, extract_features

        if with_drift:
            raise ValueError("numpy 엔진은 드리프트 상태를 지원하지 않습니다 (with_drift=False 전용).")
        df, _ = prepare_frame(df_rows)
        order = df.groupby("C64")["C10"].min().sort_values(kind="mergesort").index.tolist()
        F = extract_features(df).reindex(order).fillna(0.0)
        X = self._align_transform(F)
        raw = self.scorer.ae_raw(X)
        score = self._apply_calibration(raw, self.calib)
        tops = self.scorer.top_channels(X, k=k)
        return [{"wafer": str(w), "ae_score": round(float(score[i]), 4),
                 "ae_raw": round(float(raw[i]), 4), "ae_top_channels": list(tops[i]),
                 "ae_model_version": self.model_version, "ae_calib_version": self.calib_version}
                for i, w in enumerate(order)]


def torch_preflight() -> tuple[bool, str]:
    """torch import 생존 확인 — **본 작업 전에 2초 만에** 판정 (TSR-0001 미해결 사항 대응).

    왜 맨 앞인가: 스코어링용 torch 로드는 데이터 적재·피처 추출을 다 한 **뒤**에 일어난다
    (torch 무의존 진단을 먼저 살리려는 설계). 그대로 두면 torch 가 죽어 있을 때 수십 초를
    태우고 나서야 알게 된다. 그래서 시작 직후 한 번 찔러보고 상태를 알린다.

    실패해도 **중단하지 않는다** — 무의존 진단은 여전히 유효하므로 진행하고, 스코어링
    단계에서 폴백/스킵한다.

    Returns:
        (생존 여부, 메시지) — 메시지는 버전 또는 실패 요약.
    """
    import time
    t0 = time.time()
    try:
        import torch
        return True, f"torch {torch.__version__} ({time.time() - t0:.1f}s)"
    except OSError as e:
        return False, f"DLL 로드 차단 — {e}"
    except Exception as e:                                     # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def load_engine(bundle: Path, engine: str, device=None):
    """엔진 선택 — 'auto'(torch 시도 → numpy 폴백) · 'torch' · 'numpy'. 실패 시 None.

    torch 로드 실패를 **삼키지 않고** 원인과 다음 수를 로그로 남긴다. WDAC 차단은
    패키지 문제가 아니라 실행 정책 문제라, 재설치를 권하면 오진으로 시간을 버린다.
    """
    if engine in ("auto", "torch"):
        try:
            from ae_pipeline.infer import AEModel
            m = AEModel.from_bundle(bundle, device=device)
            m.engine = "torch"
            return m
        except OSError as e:                                   # DLL 차단·로드 실패
            log.error("★torch 로드 실패 (DLL): %s", e)
            log.error("  앱 제어 정책(WDAC) 차단으로 보입니다 — TSR-0001 변종(foreground 발생). "
                      "패키지 재설치는 오진입니다. 확인 순서:")
            log.error("   1) python -c \"import torch\"  → 같은 오류면 스크립트 무관")
            log.error("   2) 이벤트 뷰어 > 응용 프로그램 및 서비스 로그 > Microsoft > Windows > "
                      "CodeIntegrity > Operational → 차단된 파일·해시 확인 (IT 전달용)")
            log.error("   3) 임시 우회: --engine numpy (가중치 npz 필요) 또는 --no-score")
            if engine == "torch":
                return None
            log.warning("→ numpy 엔진으로 폴백을 시도합니다.")
        except Exception as e:                                 # noqa: BLE001
            log.error("torch 엔진 초기화 실패: %s", e)
            if engine == "torch":
                return None
            log.warning("→ numpy 엔진으로 폴백을 시도합니다.")
    try:
        return NumpyAEModel(bundle)
    except Exception as e:                                     # noqa: BLE001
        log.error("numpy 엔진도 사용 불가: %s", e)
        return None


def _save_report(report: dict, out_path) -> None:
    """리포트 JSON 저장 (경로 미지정이면 건너뜀)."""
    if not out_path:
        return
    out = Path(out_path)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("리포트 저장: %s", out)


def build_argparser() -> argparse.ArgumentParser:
    """CLI 인자 정의 (헌법 6-1: 값은 상수/인자로, 코드에 매직넘버 금지)."""
    ap = argparse.ArgumentParser(
        prog="ae_baseline_ladder",
        description="AE 베이스라인 레짐 대조(PRE/POST) + 오프셋·노이즈 적정")
    ap.add_argument("--data", required=True, help="원본 CSV/parquet (train_data.csv 등)")
    ap.add_argument("--bundle", default="models/anomaly_ae/ae_v1", help="AE 번들 폴더 (헌법 4-1)")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help="창당 wafer 수")
    ap.add_argument("--order", choices=["time", "file"], default="time",
                    help="창 절단 순서 — time=시간순(기본·정식) / file=파일 순서(PM 창 재현)")
    ap.add_argument("--normal-col", default=None, help="정상 필터 컬럼 (예: C6) — 지정 시 창 선택 전 적용")
    ap.add_argument("--normal-value", default=None, help="정상 필터 값 (예: C6_0)")
    ap.add_argument("--catapult", action="store_true", help="피처별 Δz 랭킹·클립 셀 진단")
    ap.add_argument("--ladder", action="store_true", help="오프셋·노이즈 적정 사다리")
    ap.add_argument("--ladder-window", choices=["pre", "post"], default="pre",
                    help="적정 대상 창 (기본 pre — 훈련 레짐 위에서 재는 것이 옳다)")
    ap.add_argument("--offset-sigma", type=float, default=DEFAULT_OFFSET_SIGMA)
    ap.add_argument("--noise-grid", type=float, nargs="*", default=DEFAULT_NOISE_GRID)
    ap.add_argument("--engine", choices=["auto", "torch", "numpy"], default="auto",
                    help="스코어링 엔진 — auto=torch 시도 후 numpy 폴백(기본) / "
                         "numpy=torch 무의존(model_weights.npz 필요, WDAC 차단 우회용)")
    ap.add_argument("--no-score", action="store_true",
                    help="모델 로드 없이 torch 무의존 진단(경계·이산채널)만 실행")
    ap.add_argument("--device", default=None, help="cuda/cpu (기본 자동)")
    ap.add_argument("--out", default=None, help="리포트 JSON 저장 경로")
    return ap


def main(argv=None) -> int:
    """진입점 — 경계 산출 → PRE/POST 대조 → (선택) 캐터펄트·적정 → 리포트."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s │ %(levelname)-7s │ [ae.baseline] %(message)s")
    args = build_argparser().parse_args(argv)

    # 데이터 적재(수십 초) 전에 torch 생존부터 알린다 — 실패해도 진행(무의존 진단은 유효)
    if not args.no_score and args.engine in ("auto", "torch"):
        ok, msg = torch_preflight()
        (log.info if ok else log.warning)("preflight: torch %s — %s",
                                          "OK" if ok else "사용 불가", msg)
        if not ok:
            log.warning("  → 스코어링은 numpy 폴백 또는 스킵으로 진행합니다. "
                        "무의존 진단(경계·이산채널)은 그대로 나옵니다.")

    data_path = Path(args.data)
    if not data_path.is_absolute():
        data_path = REPO_ROOT / data_path
    df = load_frame(data_path)
    stats_df = df.copy()                                      # 섭동 std 모집단 = 전체 (replicator 규약)

    if args.normal_col and args.normal_value is not None:
        keep = df.groupby("C64", sort=False)[args.normal_col].first().astype(str)
        sel = set(keep[keep == str(args.normal_value)].index)
        log.info("정상 필터 %s==%s → wafer %d → %d장", args.normal_col, args.normal_value,
                 df["C64"].nunique(), len(sel))
        df = df[df["C64"].isin(sel)]

    info = resolve_boundary(df, args.order)
    order, boundary = info["order"], info["boundary"]
    w_pre = pick_window(order, boundary, "pre", args.count)
    w_post = pick_window(order, boundary, "post", args.count)
    if not w_pre or not w_post:
        raise ValueError(f"창 확보 실패 — PRE {len(w_pre)}장 / POST {len(w_post)}장. "
                         "--count 를 줄이거나 필터를 확인하세요.")
    log.info("창 확보: PRE %d장 (인덱스 %d~%d) · POST %d장 (인덱스 %d~%d)",
             len(w_pre), boundary - len(w_pre), boundary - 1,
             len(w_post), boundary, boundary + len(w_post) - 1)

    bundle = Path(args.bundle)
    if not bundle.is_absolute():
        bundle = REPO_ROOT / bundle

    report = {"data": str(data_path), "bundle": str(bundle),
              "boundary": boundary, "n_wafers": info["n_wafers"], "order_mode": args.order,
              "ae_reset_hits": info["ae_reset_hits"], "sim_reset_hits": info["sim_reset_hits"]}

    # ── torch 무의존 진단 먼저 (모델 로드 실패해도 이만큼은 손에 남는다) ──
    # WDAC 등으로 torch DLL 이 막히면 여기서 끝나도 C49 연속화·경계 증거는 확보된다.
    report["discrete_pre"] = discrete_channel_report(df, w_pre, "PRE")
    report["discrete_post"] = discrete_channel_report(df, w_post, "POST")

    model = None
    if not args.no_score:
        model = load_engine(bundle, args.engine, device=args.device)
    if model is None:
        log.warning("스코어링을 건너뜁니다 — 위 이산채널 진단만 유효합니다. "
                    "PRE/POST raw 대조는 엔진 복구 후 재실행하세요.")
        _save_report(report, args.out)
        return 0

    log.info("번들 로드: %s (model=%s · calib=%s · engine=%s)",
             bundle, model.model_version, model.calib_version, getattr(model, "engine", "torch"))
    report.update(model_version=model.model_version, calib_version=model.calib_version,
                  engine=getattr(model, "engine", "torch"))

    results = [score_window(model, df, w_pre, f"PRE  (리셋 전 {len(w_pre)}장)"),
               score_window(model, df, w_post, f"POST (리셋 후 {len(w_post)}장)")]
    report_windows(model, results)
    report["windows"] = results

    if args.catapult:
        # 레짐 성분이 어느 피처에서 오는지 (창이 달라 짝짓기는 안 되고 평균 대조)
        report["catapult_pre_to_post"] = catapult_report(
            model, df, w_pre, df, w_post, "PRE", "POST")

    if args.ladder:
        w_l = w_pre if args.ladder_window == "pre" else w_post
        report["ladder"] = ladder_report(model, df, w_l, stats_df, args.offset_sigma,
                                         list(args.noise_grid), args.ladder_window.upper())
        if args.catapult:
            # 같은 창 · 원본 vs 최소 노이즈 — wafer 가 짝지어지므로 이 대조가 가장 정확하다
            n_min = min([n for n in args.noise_grid if n > 0], default=0.001)
            win = df[df["C64"].isin(set(w_l))]
            report["catapult_noise_only"] = catapult_report(
                model, win, w_l, perturb(win, stats_df, 0.0, n_min), w_l,
                "원본", f"노이즈{n_min:g}")
            report["catapult_offset_only"] = catapult_report(
                model, win, w_l, perturb(win, stats_df, args.offset_sigma, 0.0), w_l,
                "원본", f"오프셋{args.offset_sigma:g}σ")

    _save_report(report, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
