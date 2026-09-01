# -*- coding: utf-8 -*-
"""PM 사이클 구조 실측 — 사이클 정의(2계층 안) 확정을 위한 데이터 팩트 수집.

확인 항목:
  ① 분할(train/valid/test)별 규모·기간 — "랜덤 분할" 구조 확인
  ② 전체 합산 기준 실제 처리율 (train 단독 170/일은 과소 의심)
  ③ C33(RF time)의 리셋 지점·세그먼트별 범위/증가율 — wet clean 사이클 실측
  ④ 달력 커버리지 (총 며칠, 빠진 날짜 있는지 — "60일" 확인)

사용법: python scripts/analyze_pm_cycle.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "Data" / "문제1(하)"

FILES = {
    "train": DATA / "train_data.csv",
    "valid": DATA / "valid_X.csv",
    "test":  DATA / "test_X.csv",
}


def load_split(name: str, path: Path) -> pd.DataFrame:
    """분할 로드 — 공통 컬럼(C64, C10, C33)만."""
    if not path.exists():
        print(f"[경고] {name}: 파일 없음 — {path}")
        return pd.DataFrame()
    df = pd.read_csv(path, usecols=lambda c: c in {"C64", "C10", "C33", "C7"})
    df["split"] = name
    return df


def main():
    """실측 실행."""
    parts = [load_split(n, p) for n, p in FILES.items()]
    df = pd.concat([p for p in parts if len(p)], ignore_index=True)

    df["dt"] = pd.to_datetime(df["C10"], unit="s", errors="coerce")
    df["date"] = df["dt"].dt.date

    print("=" * 72)
    print("① 분할별 규모·기간")
    for name in FILES:
        s = df[df["split"] == name]
        if not len(s):
            continue
        print(f"  {name:5s}: rows {len(s):>8,} │ wafer {s['C64'].nunique():>6,} "
              f"│ {s['dt'].min():%m/%d} ~ {s['dt'].max():%m/%d} "
              f"│ C33 [{s['C33'].min():.0f}, {s['C33'].max():.0f}]")

    total_wafers = df["C64"].nunique()
    days = df["date"].nunique()
    span = (df["dt"].max() - df["dt"].min()).days + 1
    print()
    print("② 전체 합산")
    print(f"  wafer 총 {total_wafers:,}장 │ 관측일 {days}일 (달력 span {span}일, "
          f"빠진 날 {span - days}일) │ 처리율 ≈ {total_wafers / days:.0f} wafer/일")

    # ③ C33 세그먼트 — wafer 단위 시간순으로 리셋 탐지 (50% 급락 기준)
    w = (df.sort_values("dt").groupby("C64", sort=False)
           .agg(dt=("dt", "first"), c33=("C33", "first")).reset_index()
           .sort_values("dt").reset_index(drop=True))
    c33 = w["c33"].to_numpy(float)
    resets = [i + 1 for i in range(len(c33) - 1)
              if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5]
    bounds = [0] + resets + [len(w)]
    print()
    print(f"③ C33 세그먼트 (리셋 {len(resets)}회 감지)")
    for k in range(len(bounds) - 1):
        seg = w.iloc[bounds[k]:bounds[k + 1]]
        d0, d1 = seg["dt"].iloc[0], seg["dt"].iloc[-1]
        dur = (d1 - d0).days + 1
        c_lo, c_hi = seg["c33"].min(), seg["c33"].max()
        rate = (c_hi - c_lo) / max(dur, 1)
        tail = "  ← 잘림(진행 중)" if k == len(bounds) - 2 else ""
        print(f"  seg{k + 1}: wafer {len(seg):>6,} │ {d0:%m/%d}~{d1:%m/%d} ({dur:>2}일) "
              f"│ C33 {c_lo:.0f}→{c_hi:.0f} (+{c_hi - c_lo:.0f}, {rate:.2f}/일){tail}")

    print()
    print("④ 참고 — 현업 wet clean 벤치마크: wafer 2,000~3,000장 / 2~4주 / 수백 RF-h")
    print("   → seg별 wafer 수·일수를 위와 비교해 minor PM(wet clean) 해석 타당성 판단")
    print("=" * 72)


if __name__ == "__main__":
    main()
