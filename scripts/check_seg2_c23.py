# -*- coding: utf-8 -*-
"""seg2 초반 실측 급등의 원인 귀속 — PM 직후 효과 vs C23 캠페인 효과.

방법:
  ① seg2 초반(리셋 후 첫 1,000 wafer)의 C23 구성과 C65를 교차
  ② 같은 C23 배치가 seg1에도 있었다면 그때의 C65와 비교
     → 같은 배치인데 seg2에서만 높다  = PM 직후 효과
     → 원래 높은 배치가 흘렀을 뿐이다 = 캠페인 효과 (C23 귀속)
  ③ seg2 초반 vs seg2 중반의 배치 구성 변화도 확인

사용법: python scripts/check_seg2_c23.py
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
HEAD_N = 1000   # seg2 초반 = 리셋 후 첫 1,000 wafer


def main():
    """실행."""
    df = pd.read_csv(TRAIN, usecols=lambda c: c in {"C64", "C10", "C33", "C65", "C23"})
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    w = (df.sort_values("dt").groupby("C64", sort=False)
           .agg(dt=("dt", "first"), c33=("C33", "first"),
                c65=("C65", "first"), c23=("C23", "first"))
           .reset_index().sort_values("dt").reset_index(drop=True))

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:]
    head = seg2.iloc[:HEAD_N]
    mid2 = seg2.iloc[HEAD_N: HEAD_N * 4]

    print("=" * 78)
    print(f"seg1 {len(seg1):,} │ seg2 {len(seg2):,} │ seg2 초반 = 첫 {HEAD_N:,} wafer")
    print(f"C65 평균: seg1 {seg1['c65'].mean():.0f} │ seg2 초반 {head['c65'].mean():.0f} "
          f"│ seg2 중반 {mid2['c65'].mean():.0f} │ seg2 전체 {seg2['c65'].mean():.0f}")

    print("-" * 78)
    print("① seg2 초반의 C23 구성 (상위 8)")
    print(f"{'C23':<10} {'초반 wafer':>9} {'초반 C65':>9} │ {'seg1 동일배치 C65':>15} {'seg1 wafer':>9} │ 판정")
    top = head["c23"].value_counts().head(8)
    for code, n in top.items():
        h65 = head.loc[head["c23"] == code, "c65"].mean()
        s1 = seg1.loc[seg1["c23"] == code, "c65"]
        if len(s1) >= 30:
            base, nb = s1.mean(), len(s1)
            verdict = "≈ 캠페인(원래 수준)" if abs(h65 - base) < 100 else "→ PM 직후 상승 의심"
            print(f"{str(code):<10} {n:>9,} {h65:>9.0f} │ {base:>15.0f} {nb:>9,} │ {verdict}")
        else:
            print(f"{str(code):<10} {n:>9,} {h65:>9.0f} │ {'(seg1에 없음/희소)':>15} {len(s1):>9,} │ 신규 배치 — 캠페인 교체 증거")

    print("-" * 78)
    print("② 배치 구성 변화: seg2 초반 상위 5 vs seg2 중반 상위 5")
    print(f"  초반: {dict(head['c23'].value_counts().head(5))}")
    print(f"  중반: {dict(mid2['c23'].value_counts().head(5))}")
    print("-" * 78)
    print("해석: '신규 배치' 다수 or '≈캠페인' 다수 → 급등은 C23 귀속 /")
    print("      같은 배치가 seg1보다 일제히 +100↑ → PM 직후 효과 실재")
    print("=" * 78)


if __name__ == "__main__":
    main()
