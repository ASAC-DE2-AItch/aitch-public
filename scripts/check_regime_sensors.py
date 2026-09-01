# -*- coding: utf-8 -*-
"""레짐 전환(12/23 PM)이 센서 공간에 흔적을 남겼는가 — 침묵 전환 판정.

통제 조건 (교란 제거):
  · 같은 배치(C23 최다 배치)  · 같은 레시피(C6 주력)  · 정착 구간만(C42=0)
추가 확인:
  · seg2 내 레시피(C6) 타임라인 — "PM 후 FBC 안정 구간 = 레시피 실험" (PM 증언) 위치 특정

판정:
  · 핵심 센서 |이동| ≥ 1σ(seg1 기준) 존재 → 레짐이 센서로 식별 가능 (AE·모델이 볼 수 있음)
  · 전부 < 0.5σ                     → 침묵 전환 — 실측 피드백(fdc.actual)·Qual만이 감지 수단

사용법: python scripts/check_regime_sensors.py
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
SENSORS = ["C11", "C12", "C16", "C17", "C61", "C62", "C63", "C31", "C15"]  # EDA 핵심 + SPC 대상
HEAD_N = 1000


def main():
    """실행."""
    use = {"C64", "C10", "C33", "C65", "C23", "C6", "C42"} | set(SENSORS)
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    # wafer 단위: 식별자 + 정착(C42=0) 구간 센서 평균
    settled = df[df["C42"] == 0]
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt=("dt", "first"), c33=("C33", "first"),
                  c65=("C65", "first"), c23=("C23", "first"), c6=("C6", "first")))
    sens = settled.groupby("C64")[SENSORS].mean()
    w = ids.join(sens).reset_index().sort_values("dt").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:].reset_index(drop=True)

    print("=" * 80)
    print("① seg2 내 레시피(C6) 타임라인 — 1,000 wafer 블록별 구성과 C65")
    print(f"{'블록':<12} {'기간':<13} {'C6 구성':<28} {'C65 평균':>8}")
    for b in range(0, len(seg2), 1000):
        blk = seg2.iloc[b:b + 1000]
        mix = dict(blk["c6"].value_counts().head(2))
        mix_s = ", ".join(f"{k}:{v}" for k, v in mix.items())
        print(f"{b:>5}~{b + len(blk) - 1:<6} {blk['dt'].iloc[0]:%m/%d}~{blk['dt'].iloc[-1]:%m/%d}"
              f"   {mix_s:<28} {blk['c65'].mean():>8.0f}")

    # 레시피별 C65 (seg2)
    print("-" * 80)
    print("② seg2 레시피별 실측 C65 (레시피 실험 구간의 효과 확인)")
    for r, grp in seg2.groupby("c6"):
        print(f"   {r}: wafer {len(grp):,} │ C65 {grp['c65'].mean():.0f} "
              f"│ 기간 {grp['dt'].min():%m/%d}~{grp['dt'].max():%m/%d}")

    # ③ 통제 후 센서 비교: 최다 배치 + 주력 레시피
    batch = w["c23"].value_counts().idxmax()
    recipe = w["c6"].value_counts().idxmax()
    a = seg1[(seg1["c23"] == batch) & (seg1["c6"] == recipe)]
    b2 = seg2[(seg2["c23"] == batch) & (seg2["c6"] == recipe)]
    b_head = b2.iloc[:HEAD_N]
    print("-" * 80)
    print(f"③ 센서 흔적 검사 — 통제: 배치={batch}, 레시피={recipe}, C42=0 정착 샘플")
    print(f"   seg1 {len(a):,} wafer vs seg2(동조건) {len(b2):,} wafer (초반 {len(b_head):,})")
    print(f"{'센서':<6} {'seg1 평균':>12} {'seg1 σ':>10} {'seg2 평균':>12} {'이동(σ)':>9} │ {'seg2초반 이동(σ)':>14}")
    silent = True
    for s in SENSORS:
        m1, sd = a[s].mean(), a[s].std()
        m2 = b2[s].mean()
        mh = b_head[s].mean()
        sh, shh = (m2 - m1) / sd, (mh - m1) / sd
        flag = " ★" if abs(sh) >= 1 else ""
        if abs(sh) >= 0.5:
            silent = False
        print(f"{s:<6} {m1:>12.2f} {sd:>10.2f} {m2:>12.2f} {sh:>9.2f}{flag} │ {shh:>14.2f}")
    print(f"   (동조건 C65: seg1 {a['c65'].mean():.0f} → seg2 {b2['c65'].mean():.0f} — 레짐 확인용)")
    print("-" * 80)
    print("판정:", "센서 이동 ≥0.5σ 존재 → 레짐이 센서에 흔적 있음 (AE·모델 식별 가능)"
          if not silent else "전 센서 <0.5σ → ★침묵 전환★ — 실측(fdc.actual)·Qual만 감지 가능")
    print("=" * 80)


if __name__ == "__main__":
    main()
