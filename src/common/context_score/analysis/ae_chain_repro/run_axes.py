"""
run_axes.py — wafer_scores.csv → EWMA drift → Anomaly축/Drift축/score_ae 산출 + 검증.

설계서 score_ae_설계_기획서_v1 §3 산식:
  Anomaly (z버킷): z=(ae_raw−μ)/σ · z<2→0 / 2≤z<3→+15 / z≥3→+25
  Anomaly (밴드) : ae_score <0.2→0 / 0.2~0.5→+10 / 0.5~1.0→+20 / ≥1.0→+30
  Drift  (밴드)  : d≤0.3→0 / 0.3<d≤0.7→+10 / d>0.7→+20
  score_ae = max(anom, drift)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("/sessions/sleepy-zealous-cerf/mnt/outputs")
ART = Path("/sessions/sleepy-zealous-cerf/mnt/AITCH-common-contextscore"
           "/src/agent_a_mlops/Autoencoder/artifacts")

ALPHA, B0, ALARM = 0.05, 0.130, 0.20
Z_WARN, Z_CRIT, PTS_ZW, PTS_ZC = 2.0, 3.0, 15, 25
D_LO, D_HI, PTS_DL, PTS_DH = 0.3, 0.7, 10, 20


def main():
    df = pd.read_csv(OUT / "wafer_scores.csv")
    calib = json.loads((ART / "calib.json").read_text())
    val_w = set(json.loads((OUT / "val_w.json").read_text()))
    val = df[df["wafer"].isin(val_w)]

    # ── 정상(VAL) 통계 → z 경로의 μ_raw·σ_raw ──
    mu_raw, sd_raw = float(val["ae_raw"].mean()), float(val["ae_raw"].std())
    print("== 정상(VAL) 기준통계 ==")
    print(f"  n={len(val)}  μ_raw={mu_raw:.3f}  σ_raw={sd_raw:.3f}")
    print(f"  ae_score 평균 {val['ae_score'].mean():.4f} (문서 ≈0.045)"
          f" · P95 {np.percentile(val['ae_score'],95):.4f} (문서 <0.2)")
    l5 = float((val["ae_score"] > 0.2).mean())
    print(f"  L5 오경보율(ae_score>0.2) {l5*100:.2f}% (문서 1.53%)")
    print(f"  z 임계 대응 raw: z=2 → {mu_raw+2*sd_raw:.1f} · z=3 → {mu_raw+3*sd_raw:.1f}")

    # ── EWMA drift (per-chamber; 본 데이터는 C24 단일) ──
    df = df.sort_values("t", kind="mergesort").reset_index(drop=True)
    E, drift = B0, []
    for s in df["ae_score"].values:
        E = ALPHA * s + (1 - ALPHA) * E
        drift.append(np.clip((E - B0) / (ALARM - B0), 0, 1))
    df["ewma"] = np.nan
    ew, E = [], B0
    for s in df["ae_score"].values:
        E = ALPHA * s + (1 - ALPHA) * E
        ew.append(E)
    df["ewma"], df["ae_drift_score"] = ew, drift

    # ── 축 기여점수 ──
    z = (df["ae_raw"] - mu_raw) / sd_raw
    df["z"] = z
    df["anom_z"] = np.select([z >= Z_CRIT, z >= Z_WARN], [PTS_ZC, PTS_ZW], 0)
    s = df["ae_score"]
    df["anom_band"] = np.select([s >= 1.0, s >= 0.5, s >= 0.2], [30, 20, 10], 0)
    d = df["ae_drift_score"]
    df["drift_pts"] = np.select([d > D_HI, d > D_LO], [PTS_DH, PTS_DL], 0)
    df["score_ae_z"] = np.maximum(df["anom_z"], df["drift_pts"])
    df["score_ae_band"] = np.maximum(df["anom_band"], df["drift_pts"])
    df["score_ae_sum"] = df["anom_z"] + df["drift_pts"]      # 이중계상 비교용

    df.to_csv(OUT / "wafer_axes.csv", index=False)

    print("\n== 구간별 요약 ==")
    g = df.groupby("seg").agg(
        n=("wafer", "size"), ae_raw중앙=("ae_raw", "median"),
        ae_score평균=("ae_score", "mean"), drift평균=("ae_drift_score", "mean"),
        anom점수평균=("anom_z", "mean"), drift점수평균=("drift_pts", "mean"),
        score_ae평균=("score_ae_z", "mean"))
    print(g.round(3).to_string())

    print("\n== 포화 실태 (설계 쟁점) ==")
    print(f"  ae_score == 1.0 인 wafer: {(df['ae_score']>=1.0).sum()} / {len(df)}"
          f" ({(df['ae_score']>=1.0).mean()*100:.2f}%)")
    print(f"    └ 그중 정상 VAL 구간: {(val['ae_score']>=1.0).sum()}장"
          f" ({(val['ae_score']>=1.0).mean()*100:.2f}% — 정상 꼬리 오탐)")
    print(f"  ae_drift_score == 1.0 인 wafer: {(df['ae_drift_score']>=1.0).sum()}"
          f" ({(df['ae_drift_score']>=1.0).mean()*100:.1f}%)")
    # drift가 1.0에 처음 도달한 뒤 0으로 복귀하는지
    first1 = int(np.argmax(df["ae_drift_score"].values >= 1.0))
    after = df["ae_drift_score"].values[first1:]
    back0 = np.where(after <= 0.0)[0]
    print(f"  drift 최초 1.0 도달: {first1}번째 wafer")
    print(f"  그 이후 0으로 복귀: {'없음 (끝까지 1.0 고착)' if len(back0)==0 else str(back0[0])+'장 뒤'}")
    print(f"  max(anom,drift) vs 단순합산 평균: {df['score_ae_z'].mean():.2f}"
          f" vs {df['score_ae_sum'].mean():.2f}"
          f" (합산 시 +{(df['score_ae_sum'].mean()/max(df['score_ae_z'].mean(),1e-9)-1)*100:.0f}% 과대)")

    # ── 데모용 구간: 레짐 전환 앞뒤 ──
    idx = int(np.argmax(df["seg"].values == "seg2"))
    demo = df.iloc[max(0, idx - 12): idx + 28][
        ["wafer", "seg", "ae_raw", "z", "ae_score", "ewma", "ae_drift_score",
         "anom_z", "anom_band", "drift_pts", "score_ae_z"]]
    demo.to_csv(OUT / "demo_window.csv", index=False)
    print(f"\n== 레짐 전환 데모 구간 (seg2 시작 index={idx}) ==")
    print(demo.round(3).to_string(index=False))

    # 시각화용 다운샘플 스트림
    step = max(1, len(df) // 1200)
    df.iloc[::step][["wafer", "seg", "ae_raw", "ae_score", "ewma",
                     "ae_drift_score", "score_ae_z"]].to_csv(
        OUT / "stream_ds.csv", index=False)
    json.dump({"mu_raw": mu_raw, "sd_raw": sd_raw, "l5": l5,
               "val_mean_score": float(val["ae_score"].mean()),
               "val_p95": float(np.percentile(val["ae_score"], 95)),
               "seg2_start": idx, "n": len(df),
               "anchors_x": calib["anchors_x"], "anchors_y": calib["anchors_y"]},
              open(OUT / "stats.json", "w"))


if __name__ == "__main__":
    main()
