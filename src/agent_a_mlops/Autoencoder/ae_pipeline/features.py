"""
features.py — 표준 전처리 유틸 + wafer×피처 추출 (부록 A 단일소스).

원 노트북 `ae_v3.ipynb` P0-4 · P2 셀과 동일 로직. 시간축이 아니라 레벨축(Step4
정착 T=2) 신호이므로 시퀀스 AE 대신 **집계 MLP-AE + 과도 모양 지표**를 쓴다
(아키텍처 판단 v1). 산출 = wafer 1장당 D=83 피처 행.

불가침:
  · (5) 과도(C42==1)/정착(C42==0) 분리 — 과도 서명은 과도 구간에서만 집계.
  · (11) dedup 가드 `(C64,C7,C46)` + split 드랍은 학습·추론 전 상시 적용.
  · (1·7) 물리신호만. 식별자·타깃·시간·setpoint·과도플래그는 입력 금지(constants).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ── 표준 전처리 유틸 (P0-4) ────────────────────────────────────────────────
def standard_sort(df: pd.DataFrame) -> pd.DataFrame:
    """표준 정렬 (C10 시간 → C64 wafer → C46 스텝 인덱스). 안정정렬."""
    return df.sort_values(["C10", "C64", "C46"], kind="mergesort").reset_index(drop=True)


def reconstruct_c59_c60(df: pd.DataFrame, thr: float = 1000.0) -> pd.DataFrame:
    """MUX 채널 C59/C60 → 활성 신호 C5960_active + 채널 플래그 복원."""
    out = df.copy()
    big = out["C59"] > thr
    out["C5960_active"] = np.where(big, out["C59"], out["C60"]).astype(float)
    out["C5960_ch"] = np.where(big, 0, 1).astype("int8")
    return out


def is_step4_transient(df: pd.DataFrame) -> pd.Series:
    """Step4 과도 구간 마스크 (C7==4 & C42==1)."""
    return (df["C7"] == 4) & (df["C42"] == 1)


def is_step4_settle(df: pd.DataFrame) -> pd.Series:
    """Step4 정착 구간 마스크 (C7==4 & C42==0)."""
    return (df["C7"] == 4) & (df["C42"] == 0)


def dedup_merge_artifact(df: pd.DataFrame):
    """머지 아티팩트 중복 제거 (불가침 11). (C64,C7,C46) 첫 행 유지.

    Returns: (deduped_df, n_removed)
    """
    before = len(df)
    out = (
        df.sort_values(["C10", "C64", "C46"], kind="mergesort")
        .drop_duplicates(["C64", "C7", "C46"], keep="first")
        .reset_index(drop=True)
    )
    return out, before - len(out)


def find_regime_boundary(df: pd.DataFrame) -> dict:
    """C33 카운터 리셋(diff<=-2)으로 레짐 경계(seg1/seg2) 탐지.

    Returns dict: n_reset, (있으면) seg2_first_wafer, seg1_wafers, seg2_wafers.
    """
    w = (
        df.groupby("C64", sort=False)
        .agg(c33_first=("C33", "first"), t_min=("C10", "min"))
        .reset_index()
        .sort_values("t_min", kind="mergesort")
        .reset_index(drop=True)
    )
    reset_idx = np.where((w["c33_first"].diff() <= -2).values)[0]
    info = {"n_reset": int(len(reset_idx))}
    if len(reset_idx):
        b = int(reset_idx[0])
        info.update(
            seg2_first_wafer=str(w.loc[b, "C64"]),
            seg1_wafers=w.iloc[:b]["C64"].tolist(),
            seg2_wafers=w.iloc[b:]["C64"].tolist(),
        )
    return info


def prepare_frame(df_raw: pd.DataFrame):
    """원본 행 → 표준정렬 + dedup 가드 + split 드랍 (학습·추론 공통 전처리).

    Returns: (df, n_dedup)
    """
    df = standard_sort(df_raw)
    df, n_dedup = dedup_merge_artifact(df)
    df = df.drop(columns=["split"], errors="ignore")
    return df, n_dedup


# ── 피처 추출 (P2 · 부록 A) ────────────────────────────────────────────────
def extract_features(df_all: pd.DataFrame) -> pd.DataFrame:
    """dedup·정렬된 원본 물리단위 행 → wafer × 집계 피처 (D=83).

    정착 연속 mean/std/min/max · 점화 과도 서명 max/mean/min(과도 구간만) ·
    정착 잔류 · C54/C56 매칭 · 과도 모양 파생 스칼라 · 완결성 플래그.
    정착/과도 없는 wafer는 0으로 채운다.
    """
    from .constants import CH_SETTLE_CONT  # 지연 import (단일소스)

    d = reconstruct_c59_c60(df_all)
    s4 = d[d["C7"] == 4]
    settle = s4[s4["C42"] == 0]
    trans = s4[s4["C42"] == 1]
    gs, gt, g4, gd = (
        settle.groupby("C64"),
        trans.groupby("C64"),
        s4.groupby("C64"),
        d.groupby("C64"),
    )
    parts = []

    # 정착 연속 mean/std/min/max
    a = gs[CH_SETTLE_CONT + ["C5960_active"]].agg(["mean", "std", "min", "max"])
    a.columns = [f"{c}_s_{s}" for c, s in a.columns]
    parts.append(a)

    # 점화 과도 서명 max/mean/min (과도 구간만 — 불가침 5)
    b = gt[["C18", "C27", "C32", "C62", "C61"]].agg(["max", "mean", "min"])
    b.columns = [f"{c}_t_{s}" for c, s in b.columns]
    parts.append(b)

    # 정착 잔류 (C32/C62/C61 정착 mean/std)
    c = gs[["C32", "C62", "C61"]].agg(["mean", "std"])
    c.columns = [f"{c2}_sr_{s}" for c2, s in c.columns]
    parts.append(c)

    # C54/C56 매칭 위치 — 과도 wafer-mean(주) + 정착 mean/std(보조)
    e1 = gt[["C54", "C56"]].mean()
    e1.columns = [f"{x}_t_mean" for x in e1.columns]
    e2 = gs[["C54", "C56"]].agg(["mean", "std"])
    e2.columns = [f"{c2}_s_{s}" for c2, s in e2.columns]
    parts += [e1, e2]

    # 과도 모양 지표(파생 스칼라, 판단 v1 §3-④) + 완결성
    def _slope(v):
        v = np.asarray(v, float)
        return float((v[-1] - v[0]) / (len(v) - 1)) if len(v) > 1 else 0.0

    def _mode(x):
        m = x.mode()
        return float(m.iloc[0]) if len(m) else 0.0

    shape = pd.DataFrame({
        "C11_wf_min":       g4["C11"].min(),                     # 점화 스파이크 최심점(F14 레짐 프록시)
        "C18_t_peak":       gt["C18"].max(),                     # 매칭 과도 피크
        "C62_ignite_delta": gt["C62"].min() - gs["C62"].mean(),  # 점화 반전(첫샘플−정착)
        "C50_s4_sum":       g4["C50"].sum(),                     # Endpoint 이벤트
        "C63_s_slope":      gs["C63"].agg(_slope),               # 정착 드리프트
        "C49_mode":         g4["C49"].agg(_mode),
        "C49_switch":       (g4["C49"].nunique() > 1).astype(float),
        "transient_len":    gt.size(),                           # 정착 도달 속도(과도 길이)
        "settle_len":       gs.size(),                           # 완결성
        "n_steps":          gd["C7"].nunique(),
    })
    parts.append(shape)

    F = pd.concat(parts, axis=1).reindex(gd.size().index)        # 전 wafer 인덱스
    F["has_settle"] = F["settle_len"].notna().astype(float)      # 구조결측 플래그(완결성 피처)
    F["has_transient"] = F["transient_len"].notna().astype(float)
    return F.fillna(0.0)                                         # 정착/과도 없는 wafer → 0


def build_feature_spec(feat_cols) -> dict:
    """extract_features 컬럼 → feature_spec.json 딕셔너리."""
    return {
        "columns": list(feat_cols),
        "n_features": len(feat_cols),
        "windows": {"settle": "C7==4 & C42==0", "transient": "C7==4 & C42==1"},
        "note": "T=2 → 정착 std 2행 기준(판단 v1 §3). seg2/C6_1 피처는 평가 전용.",
    }
