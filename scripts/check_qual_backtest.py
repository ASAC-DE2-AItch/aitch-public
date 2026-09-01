# -*- coding: utf-8 -*-
"""Qual 판정 백테스트 — 12/23 PM 직후에 Qual 기준(C2 대용)이 '요란'을 잡아냈을까.

방법: PM 직후 첫 N장(5/20/100)을 Qual wafer로 가정하고,
      seg1 기준선 대비 4개 대용 지표를 산출해 판정 시뮬레이션.
  ① C65 기준: Qual C2의 "C65 ≤ train P95(1404)" — 실측 C65로 대용
  ② 센서 갭: TTTM 절대 비교 대용 — seg1 평균 대비 % 갭 (기준 1%)
  ③ 위치 위반: seg1 mean±3σ 밖 센서 수 (N1 대용, 기준 0건)
  ④ (AE는 모델이 없어 생략 — 센서 갭이 대용)
비교군: seg1 초반 동일 N장 (조용한 PM 직후의 대용 표본)

사용법: python scripts/check_qual_backtest.py
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
SENSORS = ["C11", "C12", "C16", "C17", "C61", "C62", "C63", "C31", "C15"]
P95 = 1404.0          # C2 기준 (train P95)
GAP_LIMIT_PCT = 1.0   # C2의 TTTM 갭 기준
QUAL_NS = [5, 20, 100]


def main():
    """실행."""
    use = {"C64", "C10", "C33", "C65", "C42"} | set(SENSORS)
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    settled = df[df["C42"] == 0]
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt=("dt", "first"), c33=("C33", "first"), c65=("C65", "first")))
    sens = settled.groupby("C64")[SENSORS].mean()
    w = ids.join(sens).reset_index().sort_values("dt").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:].reset_index(drop=True)

    # 기준선: seg1 중반 (안정 구간) — Qual 스냅샷 대용
    base = seg1.iloc[len(seg1) // 3: 2 * len(seg1) // 3]
    base_mean = base[SENSORS].mean()
    base_std = base[SENSORS].std()

    def judge(sample: pd.DataFrame, label: str):
        c65m = sample["c65"].mean()
        pass_c65 = c65m <= P95
        gaps = ((sample[SENSORS].mean() - base_mean) / base_mean.abs() * 100)
        worst = gaps.abs().idxmax()
        n1 = int(((sample[SENSORS].mean() - base_mean).abs() > 3 * base_std).sum())
        pass_gap = gaps.abs().max() <= GAP_LIMIT_PCT
        pass_n1 = n1 == 0
        verdict = "통과(조용)" if (pass_c65 and pass_gap and pass_n1) else "★탈락(요란)"
        print(f"  {label:<22} C65 {c65m:>6.0f} ({'PASS' if pass_c65 else 'FAIL'}) │ "
              f"최대갭 {gaps.abs().max():>6.1f}% @{worst} ({'PASS' if pass_gap else 'FAIL'}) │ "
              f"3σ밖 센서 {n1}개 ({'PASS' if pass_n1 else 'FAIL'}) → {verdict}")

    print("=" * 88)
    print(f"기준선 = seg1 중반 │ 판정 기준: C65≤{P95:.0f} · 센서갭≤{GAP_LIMIT_PCT}% · 3σ밖 0개")
    for n in QUAL_NS:
        print(f"— Qual 표본 {n}장 —")
        judge(seg2.iloc[:n], f"PM 직후 (12/23, 요란 예상)")
        judge(seg1.iloc[:n], f"seg1 초반 (조용 대용)")
    print("-" * 88)
    print("기대: PM 직후 = 탈락(요란) / seg1 초반 = 통과(조용) — 둘이 갈리면 판정기 변별력 입증")
    print("=" * 88)


if __name__ == "__main__":
    main()
