# -*- coding: utf-8 -*-
"""프라이어 다채널 앙상블 검증 — D+0 측정 노이즈를 다채널이 얼마나 줄이나 (빗나감 원인 ⓑ).

배경: C17 전이 프라이어(신규 결정 9)는 Qual 5장으로 델타를 읽음 — 5장 표본의
      읽기 노이즈가 프라이어 오차에 직결. 레짐 경계 반응 채널은 C17 외에도
      C11 wf_min(+6.56σ, F14)·C63·C61·C12(도장)가 있음 (F10·센서 차트).
방법: seg2 초반 정착 wafer 20장 풀에서 "Qual 5장"을 부트스트랩 재추출(500회),
      채널별 프라이어 추정치(기울기×델타)의 산포를 측정.
      → 다채널 중앙값 앙상블이 C17 단독보다 안정적이면 앙상블 채택 근거.
주의: 기울기 자체는 전이 1회 앵커(인샘플) — 여기서 검증하는 건 기울기가 아니라
      "D+0 읽기의 안정성"뿐. C12는 setpoint 도장이라 인과 아님(레짐 인덱스로만).

사용법: python scripts/check_prior_ensemble.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
CH_SETTLED = ["C17", "C63", "C61", "C12"]   # 정착 평균 채널
QUAL_N, POOL_N, BOOT = 5, 20, 500
RNG = np.random.default_rng(42)


def main():
    """실행."""
    use = {"C64", "C10", "C33", "C42", "C65", "C11"} | set(CH_SETTLED)
    df = pd.read_csv(TRAIN, usecols=lambda c: c in use)
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))

    settled = df[df["C42"] == 0]
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt=("dt", "first"), c33=("C33", "first"), c65=("C65", "first")))
    sens = settled.groupby("C64")[CH_SETTLED].mean()
    c11min = df.groupby("C64")["C11"].min().rename("C11_min")
    w = ids.join(sens).join(c11min).dropna().reset_index().sort_values("dt").reset_index(drop=True)

    c33 = w["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg1, seg2 = w.iloc[:reset], w.iloc[reset:].reset_index(drop=True)
    chans = CH_SETTLED + ["C11_min"]

    s1_y = seg1["c65"].mean()
    y_late = seg2["c65"].iloc[-1000:].mean()
    jump = y_late - s1_y

    # 채널별 앵커 기울기 (전이 1회 — 인샘플) + SNR
    m1, sd1 = seg1[chans].mean(), seg1[chans].std()
    m2 = seg2[chans].mean()
    slope = jump / (m2 - m1)                      # ΔY/Δch
    snr = ((m2 - m1) / sd1).abs()                 # 경계 반응 / 레짐 내 잡음

    print("=" * 78)
    print(f"기준: seg1 Y {s1_y:.0f} → seg2 점근 {y_late:.0f} (점프 {jump:+.0f})")
    print(f"{'채널':<9} {'seg1 평균':>10} {'seg1 σ':>8} {'seg2 평균':>10} {'SNR(σ)':>8} {'앵커 기울기':>10}")
    for ch in chans:
        print(f"{ch:<9} {m1[ch]:>10.2f} {sd1[ch]:>8.2f} {m2[ch]:>10.2f} "
              f"{snr[ch]:>8.2f} {slope[ch]:>10.2f}")

    # 부트스트랩 — Qual 5장 읽기 노이즈 → 프라이어 추정치 산포
    pool = seg2.iloc[:POOL_N]
    est = {ch: [] for ch in chans}
    est["ensemble"] = []
    for _ in range(BOOT):
        pick = pool.iloc[RNG.choice(POOL_N, QUAL_N, replace=False)]
        per_ch = [float(slope[ch] * (pick[ch].mean() - m1[ch])) for ch in chans]
        for ch, v in zip(chans, per_ch):
            est[ch].append(v)
        est["ensemble"].append(float(np.median(per_ch)))

    print("-" * 78)
    print(f"Qual {QUAL_N}장 부트스트랩({BOOT}회) — 프라이어 점프 추정치 산포 (실제 {jump:+.0f}):")
    print(f"{'추정기':<10} {'중앙값':>8} {'표준편차':>8} {'P5~P95':>16}")
    rows = chans + ["ensemble"]
    for k in rows:
        v = np.array(est[k])
        label = "다채널 중앙값" if k == "ensemble" else k
        print(f"{label:<10} {np.median(v):>+8.0f} {v.std():>8.1f} "
              f"{np.percentile(v, 5):>+7.0f}~{np.percentile(v, 95):+.0f}")

    sd_c17, sd_ens = np.std(est["C17"]), np.std(est["ensemble"])
    print("-" * 78)
    verdict = ("다채널 앙상블 채택 근거 확보" if sd_ens < sd_c17
               else "C17 단독 유지 (앙상블 이득 없음)")
    print(f"판정: C17 단독 σ={sd_c17:.1f} vs 앙상블 σ={sd_ens:.1f} → {verdict}")
    print("주의: 기울기는 전이 1회 인샘플 — 이 시험은 'D+0 읽기 안정성'만 검증 (원인 ⓑ).")
    print("=" * 78)


if __name__ == "__main__":
    main()
