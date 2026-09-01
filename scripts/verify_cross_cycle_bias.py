# -*- coding: utf-8 -*-
"""멘토 R2R 제안 검증 — 사이클 간 위상 정렬 잔차 전이 분석.

질문: "이전 사이클에서 모델이 틀린 패턴(위상별 잔차 곡선)이
       다음 사이클에서도 반복되는가?"
  - 반복된다  -> 이전 사이클 잔차로 현 사이클 bias 보정 가능 (멘토안 성립)
  - 안 된다   -> 이전 사이클 보정은 무의미 (PM 반박 성립)

입력: OOF 예측 CSV (컬럼: wafer_id, C33, predicted_c65, actual_c65)
출력: 콘솔 요약 + analysis_cross_cycle_bias.png + analysis_cross_cycle_bias.md

사용법:
    python scripts/verify_cross_cycle_bias.py [oof_predictions.csv 경로]
"""

import sys
import os

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---- 상수 (헌법 6-1: 매직 넘버는 상단 정의) ---------------------------------
N_PHASE_BINS = 40          # 위상(겹침 구간) 분할 수
MIN_WAFERS_PER_BIN = 20    # 신뢰 가능한 bin 최소 표본
CORR_SUPPORT_THRESHOLD = 0.5   # 이 이상이면 "곡선이 닮았다"로 해석 (참고 기준)
OUT_PNG = "analysis_cross_cycle_bias.png"
OUT_MD = "analysis_cross_cycle_bias.md"


def load(path):
    """OOF 예측 CSV를 로드하고 wafer 순서로 정렬한다."""
    df = pd.read_csv(path)
    need = {"wafer_id", "C33", "predicted_c65", "actual_c65"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"[에러] 필요한 컬럼 누락: {missing}")
    df = df.dropna(subset=["C33", "predicted_c65", "actual_c65"]).copy()
    # wafer_id 'C64_1234' -> 1234 (처리 순서 근사)
    df["seq"] = (
        df["wafer_id"].astype(str).str.extract(r"(\d+)$").astype(float)
    )
    df = df.sort_values("seq").reset_index(drop=True)
    df["residual"] = df["actual_c65"] - df["predicted_c65"]
    return df


def split_cycles(df):
    """C33 하락(리셋) 지점으로 PM 사이클을 분리한다 (train 내 리셋 1회 전제)."""
    c33 = df["C33"].to_numpy()
    drops = np.where(np.diff(c33) < 0)[0]
    if len(drops) == 0:
        raise SystemExit("[에러] C33 리셋을 찾지 못함 — 정렬 기준(seq)을 확인하세요.")
    if len(drops) > 1:
        print(f"[주의] 리셋이 {len(drops)}회 감지됨 — 가장 큰 하락 1곳만 사용")
        drops = [drops[np.argmax(-np.diff(c33)[drops])]]
    cut = drops[0] + 1
    seg1 = df.iloc[:cut].copy()   # PM 전 (이전 사이클의 꼬리)
    seg2 = df.iloc[cut:].copy()   # PM 후 (새 사이클의 머리)
    return seg1, seg2


def binned_curve(seg, edges):
    """위상(C33) bin별 평균 잔차 곡선."""
    idx = np.digitize(seg["C33"], edges) - 1
    rows = []
    for b in range(len(edges) - 1):
        r = seg["residual"][idx == b]
        if len(r) >= MIN_WAFERS_PER_BIN:
            rows.append((0.5 * (edges[b] + edges[b + 1]), r.mean(), len(r)))
        else:
            rows.append((0.5 * (edges[b] + edges[b + 1]), np.nan, len(r)))
    return pd.DataFrame(rows, columns=["phase", "mean_residual", "n"])


def rmse(x):
    """RMSE."""
    return float(np.sqrt(np.mean(np.square(x))))


def main():
    """분석 실행."""
    path = sys.argv[1] if len(sys.argv) > 1 else "oof_predictions.csv"
    if not os.path.exists(path):
        raise SystemExit(f"[에러] 파일 없음: {path}")
    df = load(path)
    seg1, seg2 = split_cycles(df)

    print(f"전체 {len(df)} wafer | PM 전 {len(seg1)} / PM 후 {len(seg2)}")
    print(f"OOF 전체 RMSE = {rmse(df['residual']):.2f}")
    ss_res = float(np.sum(np.square(df["residual"])))
    ss_tot = float(np.sum(np.square(df["actual_c65"] - df["actual_c65"].mean())))
    r2 = 1 - ss_res / ss_tot
    print(f"OOF 전체 R²  = {r2:.4f}  (A3-R 참고: 여전히 0.99+면 누수 의심 지속)")

    # --- 위상 겹침 구간 -------------------------------------------------------
    lo = max(seg1["C33"].min(), seg2["C33"].min())
    hi = min(seg1["C33"].max(), seg2["C33"].max())
    overlap_note = ""
    if lo >= hi:
        overlap_note = (
            "[중요] 두 세그먼트의 C33 위상이 겹치지 않음 — 이전 사이클의 '같은 위상' "
            "잔차가 데이터에 없어 멘토안을 이 데이터로는 직접 검증 불가. "
            "(seg1은 사이클 꼬리, seg2는 사이클 머리만 관측된 경우)"
        )
        print(overlap_note)
        n_ov1 = n_ov2 = 0
        corr = np.nan
        rmse_seg2 = rmse_const = rmse_phase = np.nan
    else:
        edges = np.linspace(lo, hi, N_PHASE_BINS + 1)
        c1 = binned_curve(seg1[(seg1["C33"] >= lo) & (seg1["C33"] <= hi)], edges)
        c2 = binned_curve(seg2[(seg2["C33"] >= lo) & (seg2["C33"] <= hi)], edges)
        both = c1.merge(c2, on="phase", suffixes=("_prev", "_curr")).dropna()
        n_ov1 = int(c1["n"].sum())
        n_ov2 = int(c2["n"].sum())
        corr = float(both["mean_residual_prev"].corr(both["mean_residual_curr"]))

        # --- 보정 시뮬레이션 (겹침 구간의 seg2만 대상) -----------------------
        sub2 = seg2[(seg2["C33"] >= lo) & (seg2["C33"] <= hi)].copy()
        rmse_seg2 = rmse(sub2["residual"])
        # (a) 상수 bias: seg1 겹침 구간 평균 잔차 하나로 보정
        const_bias = seg1[(seg1["C33"] >= lo) & (seg1["C33"] <= hi)]["residual"].mean()
        rmse_const = rmse(sub2["residual"] - const_bias)
        # (b) 위상별 bias: seg1 곡선을 같은 위상에 적용 (멘토안)
        bias_map = c1.set_index("phase")["mean_residual"]
        idx = np.digitize(sub2["C33"], edges) - 1
        centers = 0.5 * (edges[:-1] + edges[1:])
        phase_bias = np.array(
            [bias_map.get(centers[i], np.nan) if 0 <= i < len(centers) else np.nan
             for i in idx]
        )
        ok = ~np.isnan(phase_bias)
        rmse_phase = rmse(sub2["residual"][ok] - phase_bias[ok])

        print(f"\n위상 겹침 구간: C33 ∈ [{lo:.1f}, {hi:.1f}]"
              f"  (seg1 {n_ov1} / seg2 {n_ov2} wafer)")
        print(f"위상별 잔차 곡선 상관 (전↔후) = {corr:.3f}")
        print(f"겹침 구간 seg2 RMSE: 무보정 {rmse_seg2:.2f}"
              f" | 상수 bias {rmse_const:.2f} | 위상 bias(멘토안) {rmse_phase:.2f}")

        # --- 그림 -------------------------------------------------------------
        fig, ax = plt.subplots(1, 2, figsize=(13, 5))
        ax[0].plot(df["seq"], df["C33"], lw=0.6)
        ax[0].set_title("C33 (RF time) — PM reset")
        ax[0].set_xlabel("wafer seq")
        ax[1].plot(c1["phase"], c1["mean_residual"], "o-", label="prev cycle (seg1)")
        ax[1].plot(c2["phase"], c2["mean_residual"], "s-", label="curr cycle (seg2)")
        ax[1].axhline(0, color="gray", lw=0.8)
        ax[1].set_title(f"phase-aligned mean residual (corr={corr:.3f})")
        ax[1].set_xlabel("C33 phase")
        ax[1].set_ylabel("actual - predicted")
        ax[1].legend()
        fig.tight_layout()
        fig.savefig(OUT_PNG, dpi=150)
        print(f"\n그림 저장: {OUT_PNG}")

    # --- 판정 가이드 ----------------------------------------------------------
    lines = [
        "# Cross-Cycle Bias 검증 결과 (멘토 R2R 제안)",
        "",
        f"- 데이터: {os.path.basename(path)} — {len(df)} wafer "
        f"(PM 전 {len(seg1)} / 후 {len(seg2)})",
        f"- OOF RMSE = {rmse(df['residual']):.2f}, R² = {r2:.4f}",
        "",
    ]
    if overlap_note:
        lines += [f"**{overlap_note}**"]
    else:
        improve_const = 100 * (rmse_seg2 - rmse_const) / rmse_seg2
        improve_phase = 100 * (rmse_seg2 - rmse_phase) / rmse_seg2
        lines += [
            f"- 위상 겹침 구간: C33 [{lo:.1f}, {hi:.1f}] "
            f"(seg1 {n_ov1} / seg2 {n_ov2} wafer)",
            f"- **위상별 잔차 곡선 상관 = {corr:.3f}** "
            f"(참고 기준 {CORR_SUPPORT_THRESHOLD})",
            f"- 겹침 구간 RMSE: 무보정 {rmse_seg2:.2f} → 상수 bias "
            f"{rmse_const:.2f} ({improve_const:+.1f}%) → 위상 bias(멘토안) "
            f"{rmse_phase:.2f} ({improve_phase:+.1f}%)",
            "",
            "## 해석 가이드",
            f"- 상관 ≥ {CORR_SUPPORT_THRESHOLD} & 위상 bias 개선 > 0 → "
            "**멘토안 지지** (틀림 패턴이 사이클 간 반복됨)",
            "- 상관 낮음 & 상수 bias만 개선 → 위상 곡선까지는 과함 — "
            "상수 bias(당 사이클 라벨 도착 후 갱신)로 충분",
            "- 둘 다 개선 없음 → **PM 반박 지지** — 이전 사이클 정보로는 "
            "보정 불가, Model R2R은 당 사이클 후반(라벨 도착 후)로 한정",
        ]
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"리포트 저장: {OUT_MD}")


if __name__ == "__main__":
    main()
