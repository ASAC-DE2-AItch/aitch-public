# -*- coding: utf-8 -*-
"""seg1 말기(12/23 리셋 직전) 상태 점검 — 리셋의 성격 판정용.

질문: 리셋 직전에 악화 흔적이 있었나?
  · 있음  → 상태 반응성 개방 (조기 PM 해석 가능)
  · 없음  → 상태 무관 개방 = 부품 수명 기반 '정기' 교체로 해석

비교: seg1을 [초반 / 중반 / 말기(마지막 1,000 wafer)]로 나눠
  ① 실측 C65 (train에만 존재)  ② 핵심 센서 평균 (C11, C31, C62, C17)
말기가 중반 대비 유의하게 이동했는지 본다.

사용법: python scripts/check_seg1_tail.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
SENSORS = ["C11", "C31", "C62", "C17", "C15"]
TAIL_N = 1000  # 말기 = 마지막 1,000 wafer


def main():
    """실행."""
    use = ["C64", "C10", "C33", "C65"] + SENSORS
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df["dt"] = pd.to_datetime(df["C10"], unit="s", errors="coerce")

    # wafer 단위 집계 (시간순)
    w = (df.sort_values("dt").groupby("C64", sort=False)
           .agg({"dt": "first", "C33": "first", "C65": "first",
                 **{s: "mean" for s in SENSORS}})
           .reset_index().sort_values("dt").reset_index(drop=True))

    # 리셋 지점 (50% 급락)
    c33 = w["C33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1 = w.iloc[:reset]
    n = len(seg1)
    early = seg1.iloc[: n // 3]
    mid = seg1.iloc[n // 3: 2 * n // 3]
    tail = seg1.iloc[-TAIL_N:]

    print("=" * 76)
    print(f"seg1 = {n:,} wafer (train 기준) │ 리셋 직전 말기 = 마지막 {TAIL_N:,} wafer")
    print(f"{'지표':<8} {'초반':>12} {'중반':>12} {'말기':>12} {'말기-중반':>12} {'중반σ':>10} {'이동(σ)':>8}")
    for col in ["C65"] + SENSORS:
        e, m, t = early[col].mean(), mid[col].mean(), tail[col].mean()
        sd = mid[col].std()
        shift_sigma = (t - m) / sd if sd > 0 else float("nan")
        print(f"{col:<8} {e:>12.2f} {m:>12.2f} {t:>12.2f} {t - m:>12.2f} {sd:>10.2f} {shift_sigma:>8.2f}")

    print("-" * 76)
    print("해석 가이드: |이동(σ)| ≥ 1 인 지표가 있으면 '말기 악화 흔적 있음',")
    print("            전부 < 0.5 수준이면 '상태 무관 개방(수명 기반 정기)' 지지")
    print("=" * 76)


if __name__ == "__main__":
    main()
