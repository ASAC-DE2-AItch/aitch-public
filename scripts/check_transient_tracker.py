# -*- coding: utf-8 -*-
"""과도(초반 공백 441) 공략 — 감쇠 템플릿 + 라벨-프리 과도 추적 센서 발굴 (원인 ⓒ).

배경: 프라이어는 점근 레벨만 겨냥 → 초반 과도 몫(+781 중 ~417)이 공백으로 남음.
      운영 D+0~60엔 Y가 안 보이므로, 과도는 ① 시계 기반 템플릿(과거 τ·A 이월)
      또는 ② 과도를 실시간 추적하는 센서(있다면)로만 커버 가능.
방법: ⓐ seg2 과도를 지수 fit(L + A·exp(−t/τ)) — 템플릿 파라미터 산출(인샘플 명시)
      ⓑ 3층 커버리지 비교: 무보정 / 프라이어 상수 / 프라이어+감쇠 템플릿
      ⓒ seg2 초반 21일 일별 Y 감쇠와 동행하는 센서 스캔 (C25 유력 — EDA within +0.459)
         → 있으면 다음 PM에서 템플릿을 센서로 스케일링(라벨-프리) 가능.

사용법: python scripts/check_transient_tracker.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
CH_SCAN = ["C25", "C17", "C61", "C63", "C12"]   # 정착 평균 스캔 채널 (+C11_min 별도)
TRACK_DAYS = 21


def main():
    """실행."""
    use = {"C64", "C10", "C33", "C42", "C65", "C11"} | set(CH_SCAN)
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    settled = df[df["C42"] == 0]
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt=("dt", "first"), c33=("C33", "first"), c65=("C65", "first")))
    sens = settled.groupby("C64")[CH_SCAN].mean()
    c11min = df.groupby("C64")["C11"].min().rename("C11_min")
    w = ids.join(sens).join(c11min).dropna().reset_index().sort_values("dt").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:].reset_index(drop=True)

    s1_y = seg1["c65"].mean()
    seg2 = seg2.assign(day=(seg2["dt"] - seg2["dt"].iloc[0]).dt.total_seconds() / 86400)
    daily = seg2.assign(d=seg2["day"].astype(int)).groupby("d").agg(
        y=("c65", "mean"), **{ch: (ch, "mean") for ch in CH_SCAN + ["C11_min"]})

    # ⓐ 지수 fit — τ 그리드 + (L, A) 폐형 해
    days = daily.index.to_numpy(float)
    y = daily["y"].to_numpy(float)
    best = None
    for tau in np.arange(2, 40, 0.5):
        z = np.exp(-days / tau)
        X = np.column_stack([np.ones_like(z), z])
        coef, res, *_ = np.linalg.lstsq(X, y, rcond=None)
        sse = float(res[0]) if len(res) else float(((X @ coef - y) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, tau, coef[0], coef[1])
    _, tau, L, A = best
    print("=" * 78)
    print(f"ⓐ 과도 지수 fit (인샘플): Y(t) = {L:.0f} + {A:.0f}·exp(−t/{tau:.1f}일)")
    print(f"   점근 L={L:.0f} (실측 후반 {seg2['c65'].iloc[-1000:].mean():.0f}) │ "
          f"초기 진폭 A={A:+.0f} │ 반감 ≈ {tau * 0.693:.1f}일")

    # ⓑ 3층 커버리지 — D+0~60 wafer 단위 MAE
    jump = seg2["c65"].iloc[-1000:].mean() - s1_y
    t = seg2["day"].to_numpy(float)
    actual = seg2["c65"].to_numpy(float)
    preds = {
        "무보정 (옛 레짐 그대로)": np.full_like(actual, s1_y),
        "프라이어 상수 (레벨만)": np.full_like(actual, s1_y + jump),
        "프라이어+감쇠 템플릿": L + A * np.exp(-t / tau),
    }
    print("-" * 78)
    print(f"ⓑ 커버리지 (seg2 전체 wafer MAE │ 초반 14일 MAE):")
    early_mask = t <= 14
    for name, p in preds.items():
        mae = np.abs(actual - p).mean()
        mae_e = np.abs(actual[early_mask] - p[early_mask]).mean()
        print(f"   {name:<24} {mae:>7.0f} │ {mae_e:>7.0f}")

    # ⓒ 과도 추적 센서 스캔 — 초반 TRACK_DAYS일, 일별 센서 vs 일별 Y
    early = daily[daily.index <= TRACK_DAYS]
    print("-" * 78)
    print(f"ⓒ 라벨-프리 과도 추적 센서 스캔 (초반 {TRACK_DAYS}일, 일별 상관):")
    found = []
    for ch in CH_SCAN + ["C11_min"]:
        r = float(early[ch].corr(early["y"]))
        mark = " ★후보" if abs(r) >= 0.6 else ""
        found += [ch] if abs(r) >= 0.6 else []
        print(f"   {ch:<8} corr(일별 {ch}, 일별 Y) = {r:+.2f}{mark}")
    print("-" * 78)
    if found:
        print(f"판정: {', '.join(found)} — 과도를 라벨 없이 추적 가능성. "
              f"다음 PM에서 템플릿(τ·A)을 이 센서로 스케일링하는 설계 검토")
    else:
        print("판정: 추적 센서 없음 — 과도는 시계 기반 템플릿(과거 τ·A 이월)만 가능, "
              "불확실성 폭에 반영")
    print("주의: 템플릿 τ·A는 과도 1회 인샘플 — 이월값이며 일반화 주장 불가 (시뮬 리허설로).")
    print("=" * 78)


if __name__ == "__main__":
    main()
