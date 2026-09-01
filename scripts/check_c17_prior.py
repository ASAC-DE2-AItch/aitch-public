# -*- coding: utf-8 -*-
"""C17 전이 프라이어 판별 — "C17 델타 × 기울기 = Y 레벨 추정"이 실제 도움이 되나.

배경: 요란 PM 직후 D+0~D+62(라벨 공백)에 모델 예측이 옛 레짐 천장에 붙음(F8 +489).
      C17 새 운전점은 D+0 Qual에서 즉시 관측되므로, (C17,Y) 전이 관계로
      Y 레벨 프라이어를 라벨 없이 세울 수 있다는 가설의 사전 검증.

시험 2종 (관측 전이가 1회뿐이라 설계가 중요):
  ① 정직한 시험(순환 없음): seg1 내부 일별 미세변동으로 기울기 추정(전이 미사용)
     → 12/23 점프 예측 → 실제와 비교
  ② 운영 앵커 시험: 전이 자체의 기울기(다음 PM 때 쓸 앵커)의 정합도 + 방향 부호
판정 기준: 프라이어 적용 시 레벨 공백(무보정 +485)이 얼마나 줄고, 방향이 맞는가.

사용법: python scripts/check_c17_prior.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
QUAL_N = 5          # D+0 Qual 표본 (정착 wafer)
EARLY_N = 1000      # "라벨 공백 창" 대용 — seg2 초반 wafer
LATE_N = 1000       # 점근 레벨 대용 — seg2 후반 wafer


def main():
    """실행."""
    use = {"C64", "C10", "C33", "C42", "C65", "C17"}
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    settled = df[df["C42"] == 0]
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt=("dt", "first"), c33=("C33", "first"), c65=("C65", "first")))
    sens = settled.groupby("C64")[["C17"]].mean()
    w = ids.join(sens).dropna().reset_index().sort_values("dt").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:].reset_index(drop=True)

    s1_c17, s1_y = seg1["C17"].mean(), seg1["c65"].mean()
    qual_c17 = seg2["C17"].iloc[:QUAL_N].mean()          # D+0 관측치
    delta = qual_c17 - s1_c17

    y_early = seg2["c65"].iloc[:EARLY_N].mean()          # 라벨 공백 창의 실제 레벨
    y_late = seg2["c65"].iloc[-LATE_N:].mean()           # 점근 레벨
    jump_early, jump_late = y_early - s1_y, y_late - s1_y

    # ① 정직한 기울기 — seg1 내부 일별 평균 OLS (전이 정보 완전 미사용)
    d1 = seg1.assign(day=seg1["dt"].dt.date).groupby("day")[["C17", "c65"]].mean()
    vx = d1["C17"] - d1["C17"].mean()
    slope_within = float((vx * (d1["c65"] - d1["c65"].mean())).sum() / (vx ** 2).sum())
    corr_within = float(d1["C17"].corr(d1["c65"]))

    # ② 운영 앵커 기울기 — 이번 전이 자체 (다음 요란 PM에서 쓸 값, 인샘플 주의)
    s2_c17 = seg2["C17"].mean()
    slope_anchor = (y_late - s1_y) / (s2_c17 - s1_c17)

    print("=" * 78)
    print(f"D+0 관측: C17 {s1_c17:.1f} → {qual_c17:.1f} (델타 {delta:+.1f}) │ "
          f"실제 Y 점프: 초반 {jump_early:+.0f} / 점근 {jump_late:+.0f}")
    print("-" * 78)
    print(f"{'프라이어':<26} {'기울기':>8} {'예측 점프':>9} {'레벨 공백(초반/점근)':>22}  방향")
    rows = [
        ("무보정 (현행 — 천장 붙음)", 0.0),
        (f"① seg1 내부 OLS (정직, corr {corr_within:.2f})", slope_within),
        ("② 전이 앵커 (다음 PM용, 인샘플)", slope_anchor),
    ]
    for label, slope in rows:
        pred_jump = slope * delta
        gap_e = abs(y_early - (s1_y + pred_jump))
        gap_l = abs(y_late - (s1_y + pred_jump))
        direction = "—" if slope == 0 else ("✓" if np.sign(pred_jump) == np.sign(jump_late) else "✗")
        print(f"{label:<28} {slope:>8.2f} {pred_jump:>+9.0f} {gap_e:>10.0f} / {gap_l:<8.0f}  {direction}")

    print("-" * 78)
    print("판정 가이드:")
    print("  ①의 공백 < 무보정 공백 → 순환 없는 입증 (레짐 내 기울기가 레짐 간에도 이전됨)")
    print("  ①이 실패해도 ② 방향 ✓면 → '방향+대략 크기' 프라이어로 격하 채택 (가한계 폭 설정용)")
    print("  둘 다 실패 → 프라이어 기각, 공백은 '추정 불가' 표시가 정직")
    print("주의: 관측 전이 1회 — ②는 인샘플이라 일반화 주장 불가. 최종 검증은 시뮬 리허설.")
    print("=" * 78)


if __name__ == "__main__":
    main()
