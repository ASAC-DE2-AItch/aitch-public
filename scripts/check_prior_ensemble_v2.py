# -*- coding: utf-8 -*-
"""프라이어 수정 설계 검증 — C17 주채널 + C12/C11_min 교차확인 + Qual-기준 캘리브레이션.

v1 결과 반영한 설계 변경 2건을 시험:
  ① 기울기 캘리브레이션 수정: 분모 = seg2 전체 평균 → D+0 Qual 읽기 기준
     (운영에서 쓰는 값과 동일 기준 — v1의 계통 편향 +302/364 해소 여부 확인)
  ② 교차 확인 룰: gap = C17 추정 − 백업(C12·C11_min 평균) 추정
     ⓐ 정상 부트스트랩에서 gap 분포 → 임계(2σ/3σ) 오경보율
     ⓑ C17 고장 주입(PM 중 재교정 오류 가정, 크기별) → 탐지율 + 백업 생존 오차

사용법: python scripts/check_prior_ensemble_v2.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
QUAL_N, POOL_N, BOOT = 5, 20, 500
FAULTS_C17 = [3, 5, 10, 20]     # C17 읽기 오염 크기 (센서 단위, ± 각각)
RNG = np.random.default_rng(42)


def main():
    """실행."""
    use = {"C64", "C10", "C33", "C42", "C65", "C11", "C17", "C12"}
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    settled = df[df["C42"] == 0]
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt=("dt", "first"), c33=("C33", "first"), c65=("C65", "first")))
    sens = settled.groupby("C64")[["C17", "C12"]].mean()
    c11min = df.groupby("C64")["C11"].min().rename("C11_min")
    w = ids.join(sens).join(c11min).dropna().reset_index().sort_values("dt").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:].reset_index(drop=True)
    chans = ["C17", "C12", "C11_min"]

    s1_y = seg1["c65"].mean()
    jump = seg2["c65"].iloc[-1000:].mean() - s1_y
    m1 = seg1[chans].mean()
    pool = seg2.iloc[:POOL_N]

    # ① Qual-기준 캘리브레이션: 분모 = 정본 D+0 읽기 (첫 5장)
    qual0 = pool.iloc[:QUAL_N][chans].mean()
    slope = jump / (qual0 - m1)
    print("=" * 78)
    print(f"① Qual-기준 캘리브레이션 (점프 {jump:+.0f} 기준):")
    for ch in chans:
        print(f"   {ch:<8} 델타(D+0) {qual0[ch] - m1[ch]:>+8.2f} → 기울기 {slope[ch]:>+8.2f}")

    def estimate(sample: pd.DataFrame, c17_fault: float = 0.0):
        e = {ch: float(slope[ch] * (sample[ch].mean() - m1[ch])) for ch in chans}
        e["C17"] = float(slope["C17"] * (sample["C17"].mean() + c17_fault - m1["C17"]))
        backup = (e["C12"] + e["C11_min"]) / 2
        return e["C17"], backup

    # ② ⓐ 정상 부트스트랩 — 편향·산포·gap 분포
    prim, back, gaps = [], [], []
    for _ in range(BOOT):
        pick = pool.iloc[RNG.choice(POOL_N, QUAL_N, replace=False)]
        p, b = estimate(pick)
        prim.append(p)
        back.append(b)
        gaps.append(p - b)
    prim, back, gaps = map(np.array, (prim, back, gaps))
    sd_gap = gaps.std()

    print("-" * 78)
    print(f"② 정상 상태 부트스트랩({BOOT}회, 실제 점프 {jump:+.0f}):")
    print(f"   C17 주채널   중앙값 {np.median(prim):>+7.0f} │ σ {prim.std():>5.1f} │ "
          f"편향 {np.median(prim) - jump:>+5.0f}")
    print(f"   백업(2채널)  중앙값 {np.median(back):>+7.0f} │ σ {back.std():>5.1f} │ "
          f"편향 {np.median(back) - jump:>+5.0f}")
    for k in (2, 3):
        fa = float((np.abs(gaps - gaps.mean()) > k * sd_gap).mean() * 100)
        print(f"   교차확인 임계 {k}σ_gap(={k * sd_gap:.0f}): 오경보 {fa:.1f}%")

    # ② ⓑ C17 고장 주입 — 탐지율 + 백업 생존
    thr = 3 * sd_gap
    print("-" * 78)
    print(f"③ C17 고장 주입 (임계 3σ_gap = {thr:.0f}, {BOOT}회씩):")
    print(f"   {'오염(C17단위)':>12} {'C17추정 오차':>11} {'탐지율':>7} {'백업 오차(중앙값)':>15}")
    for f in [x * s for x in FAULTS_C17 for s in (+1, -1)]:
        det, perr, berr = [], [], []
        for _ in range(BOOT):
            pick = pool.iloc[RNG.choice(POOL_N, QUAL_N, replace=False)]
            p, b = estimate(pick, c17_fault=f)
            det.append(abs((p - b) - gaps.mean()) > thr)
            perr.append(abs(p - jump))
            berr.append(abs(b - jump))
        print(f"   {f:>+12.0f} {np.median(perr):>11.0f} {np.mean(det) * 100:>6.0f}% "
              f"{np.median(berr):>15.0f}")

    print("-" * 78)
    print("판정 가이드: ①편향≈0 확인 → 캘리브레이션 수정 채택 / ②오경보 낮고 ③탐지율 높으면")
    print("            교차확인 룰 채택. 백업 오차가 read-noise 수준이면 'C17 고장 시 생존' 입증.")
    print("주의: 기울기·임계 모두 전이 1회 기반 — 전이 원장 누적 시마다 재캘리브레이션.")
    print("=" * 78)


if __name__ == "__main__":
    main()
