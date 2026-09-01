# -*- coding: utf-8 -*-
"""C11 사실관계 판별 — "정착 평균은 침묵 vs 레짐 프록시(corr 0.81)" 충돌 해소.

가설: C11의 '정착(C42=0) 평균'은 레짐 불변이지만,
      '과도(C42=1) 값' 또는 '극값(wf_min/max)'이 레짐에 반응한다.
방법: wafer 단위 4개 표현(정착평균/과도평균/최솟값/최댓값)을 seg1↔seg2로 비교(σ 정규화).

사용법: python scripts/check_c11_extremes.py
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"


def main():
    """실행."""
    df = pd.read_csv(TRAIN, usecols=lambda c: c in {"C64", "C10", "C33", "C42", "C11"})
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    g = df.sort_values("dt").groupby("C64", sort=False)
    w = pd.DataFrame({
        "dt": g["dt"].first(),
        "c33": g["C33"].first(),
        "settled_mean": df[df["C42"] == 0].groupby("C64")["C11"].mean(),
        "transient_mean": df[df["C42"] == 1].groupby("C64")["C11"].mean(),
        "wf_min": g["C11"].min(),
        "wf_max": g["C11"].max(),
    }).reset_index().sort_values("dt").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:]

    print("=" * 74)
    print("C11 표현별 레짐 반응 (seg1 → seg2, seg1σ 정규화)")
    print(f"{'표현':<16} {'seg1 평균':>12} {'seg1 σ':>10} {'seg2 평균':>12} {'이동(σ)':>9} │ 판정")
    for col, label in [("settled_mean", "정착 평균(C42=0)"), ("transient_mean", "과도 평균(C42=1)"),
                       ("wf_min", "wafer 최솟값"), ("wf_max", "wafer 최댓값")]:
        m1, sd = seg1[col].mean(), seg1[col].std()
        m2 = seg2[col].mean()
        sh = (m2 - m1) / sd if sd > 0 else float("nan")
        verdict = "★레짐 반응" if abs(sh) >= 1 else "침묵"
        print(f"{label:<16} {m1:>12.2f} {sd:>10.2f} {m2:>12.2f} {sh:>9.2f} │ {verdict}")

    print("-" * 74)
    print("해석: 정착=침묵 + 극값/과도=반응 이면 → 두 분석 모두 옳음 (표현이 달랐던 것).")
    print("      판정식(정착 기준)은 유지, 모델의 C11_wf_min은 '레짐 반응 극값'으로 딱지.")
    print("=" * 74)


if __name__ == "__main__":
    main()
