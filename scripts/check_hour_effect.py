# -*- coding: utf-8 -*-
"""F15 검증 + 시뮬 재료 — hour(3교대) 효과가 레짐·일중 추세·배치 통제 후에도 남는가.

방법: wafer 단위 C65에서 ① 날짜별 평균 제거(레짐·감쇠·일간 표류 통제)
      ② 배치(C23) 평균 제거 후, hour(0~23)별 잔차 프로파일 산출.
      특수 레시피(C6_1)는 제외. 판정: 프로파일 진폭 vs 표본 오차.
산출: config/hour_profile_v1.csv (hour, offset, n) — 시뮬레이터 Y 오프셋 재료.

사용법: python scripts/check_hour_effect.py
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
OUT = ROOT / "config" / "hour_profile_v1.csv"
MIN_N_HOUR = 50  # 이보다 표본 적은 시간대는 신뢰 낮음 표시


def main():
    """실행."""
    use = {"C64", "C10", "C65", "C23", "C6"}
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    g = df.sort_values("dt").groupby("C64", sort=False)
    w = pd.DataFrame({
        "dt": g["dt"].first(),
        "c65": g["C65"].first(),
        "batch": g["C23"].first(),
        "recipe": g["C6"].first(),
    }).reset_index()

    main_recipe = w["recipe"].mode().iloc[0]
    w = (w.loc[w["recipe"] == main_recipe]
          .assign(date=lambda d: d["dt"].dt.date, hour=lambda d: d["dt"].dt.hour))
    # ① 날짜별 평균 제거 → ② 배치 평균 제거
    w = w.assign(r=lambda d: d["c65"] - d.groupby("date")["c65"].transform("mean"))
    w = w.assign(r=lambda d: d["r"] - d.groupby("batch")["r"].transform("mean"))

    prof = (w.groupby("hour")["r"]
              .agg(offset="mean", n="size", sd="std")
              .reindex(range(24))
              .assign(se=lambda p: p["sd"] / p["n"].pow(0.5)))

    print("=" * 70)
    print(f"hour별 C65 잔차 프로파일 (주력 레시피 {main_recipe}, 날짜·배치 통제 후)")
    print(f"{'hour':>4} {'offset':>9} {'±se':>7} {'n':>6}  비고")
    for h, row in prof.iterrows():
        note = "⚠ 표본 부족" if row["n"] < MIN_N_HOUR else ""
        bar = "#" * int(abs(row["offset"]) / 5)
        sign = "+" if row["offset"] >= 0 else "-"
        print(f"{h:>4} {row['offset']:>9.1f} {row['se']:>7.1f} {int(row['n']):>6}  {sign}{bar} {note}")

    amp = prof["offset"].max() - prof["offset"].min()
    spread = prof["offset"].std()
    se_mean = prof["se"].mean()
    ratio = spread / se_mean if se_mean > 0 else float("nan")

    shift_bin = pd.cut(w["hour"], bins=[-1, 5, 13, 21, 23],
                       labels=["야간(22-06)", "주간(06-14)", "스윙(14-22)", "야간(22-06)"],
                       ordered=False)
    shifts = w.groupby(shift_bin, observed=True)["r"].agg(["mean", "size"])

    print("-" * 70)
    print("3교대 집계 (06-14 / 14-22 / 22-06 가정):")
    for name, row in shifts.iterrows():
        print(f"  {name:<12} 평균 잔차 {row['mean']:>7.1f}  (n={int(row['size'])})")
    print("-" * 70)
    print(f"진폭(최대-최소) {amp:.1f} │ 시간대 평균 산포 {spread:.1f} vs 표본오차 {se_mean:.1f} → 비율 {ratio:.1f}")
    verdict = ("실효 패턴 — 시뮬 반영 타당" if ratio >= 2
               else "노이즈 범위 — 시뮬 반영 재고 (모델 이득은 스케줄 그림자였을 가능성)")
    print(f"판정: {verdict}")

    OUT.parent.mkdir(exist_ok=True)
    prof.reset_index()[["hour", "offset", "n"]].round(2).to_csv(OUT, index=False)
    print(f"프로파일 저장: {OUT.relative_to(ROOT)} (시뮬레이터 Y 오프셋 재료)")
    print("=" * 70)


if __name__ == "__main__":
    main()
