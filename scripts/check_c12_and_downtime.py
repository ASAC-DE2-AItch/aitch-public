# -*- coding: utf-8 -*-
"""N2 검증 (C12 = PM 도장인가) + PM 다운타임 실측.

① C12의 계단 구조: 세그먼트 안에서는 상수(또는 소수 값)이고 리셋에서만 점프하는가
   → 그렇다면 "기준값(레퍼런스)" 해석 확정 → Qual 판정 지표에서 제외 타당
② Qual 백테스트 재실행 — C12 제외 + σ 정규화 갭 기준(3σ)으로:
   기대 = PM 직후 '요란' / seg1 초반 '조용' 으로 갈림 (변별력 확보)
③ 대PM(12/23) 실측 다운타임: 리셋 전 마지막 wafer ~ 후 첫 wafer 시간 간격
   + 달력 결측일 위치 확인

사용법: python scripts/check_c12_and_downtime.py
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
SENSORS_JUDGE = ["C11", "C16", "C17", "C61", "C62", "C63", "C31", "C15"]  # C12 제외 판정용
SIGMA_LIMIT = 3.0
QUAL_N = 5


def main():
    """실행."""
    use = {"C64", "C10", "C33", "C65", "C42", "C12"} | set(SENSORS_JUDGE)
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    settled = df[df["C42"] == 0]
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt_first=("dt", "first"), dt_last=("dt", "max"),
                  c33=("C33", "first"), c65=("C65", "first")))
    sens = settled.groupby("C64")[SENSORS_JUDGE + ["C12"]].mean()
    w = ids.join(sens).reset_index().sort_values("dt_first").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:].reset_index(drop=True)

    print("=" * 80)
    print("① C12 계단 구조 검사")
    for name, seg in [("seg1", seg1), ("seg2", seg2)]:
        v = seg["C12"].round(1)
        print(f"  {name}: 고유값 {v.nunique()}개 │ 최빈값 {v.mode().iloc[0]} "
              f"({(v == v.mode().iloc[0]).mean() * 100:.1f}%) │ std {seg['C12'].std():.2f}")
    print(f"  리셋 전후 점프: {seg1['C12'].iloc[-50:].mean():.1f} → {seg2['C12'].iloc[:50].mean():.1f}")
    print("  → 세그 내 사실상 상수 + 리셋에서만 점프면 '기준값(도장)' 확정 → 판정 지표 제외")

    # ② σ 정규화 + C12 제외 백테스트
    # 기준 산포 = 직전 사이클 "전체" — C17류의 사이클 내 완만한 유동을 포함해야
    # 정상 유동이 과민 판정되지 않음 (좁은 창 σ는 8σ대 오판 유발 — 실측 확인)
    base = seg1
    bm, bs = base[SENSORS_JUDGE].mean(), base[SENSORS_JUDGE].std()

    def judge(sample, label):
        gap_sigma = ((sample[SENSORS_JUDGE].mean() - bm) / bs).abs()
        worst = gap_sigma.idxmax()
        verdict = "★요란" if gap_sigma.max() >= SIGMA_LIMIT else "조용"
        print(f"  {label:<24} 최대 |갭| {gap_sigma.max():.2f}σ @{worst} → {verdict}")

    print("-" * 80)
    print(f"② Qual 판정 재실행 (C12 제외, σ 정규화, 임계 {SIGMA_LIMIT}σ, 표본 {QUAL_N}장)")
    judge(seg2.iloc[:QUAL_N], "PM 직후 (요란 기대)")
    judge(seg1.iloc[:QUAL_N], "seg1 초반 (조용 기대)")
    judge(seg1.iloc[-QUAL_N:], "seg1 말기 (조용 기대)")

    # ③ 다운타임
    print("-" * 80)
    print("③ 대PM(12/23) 실측 다운타임")
    last_before = seg1["dt_last"].iloc[-1]
    first_after = seg2["dt_first"].iloc[0]
    gap_h = (first_after - last_before).total_seconds() / 3600
    print(f"  리셋 전 마지막 wafer 종료: {last_before} │ 후 첫 wafer 시작: {first_after}")
    print(f"  → 정지 시간 ≈ {gap_h:.1f} 시간")
    days = pd.Series(pd.date_range(w['dt_first'].min().date(), w['dt_first'].max().date()))
    observed = set(w["dt_first"].dt.date)
    missing = [d.date() for d in days if d.date() not in observed]
    print(f"  달력 결측일: {missing if missing else '없음'}")
    print("=" * 80)


if __name__ == "__main__":
    main()
