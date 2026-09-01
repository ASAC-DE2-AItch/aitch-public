"""F14: raw C11 정착 평균 vs D_VDC_RES(C11-C12) — 어느 게 더 나은 감시 대상인가.

기준: ① 신호(변동)가 있나 ② PM 레짐에 반응하나(드리프트 감지) ③ C65(불량)와
상관이 있나(불량 신호 담나 — 피처선택 EDA, 모델입력 아님) ④ 분포 품질(skew).
"""
import numpy as np
import pandas as pd

import logging
log = logging.getLogger(__name__)
if __name__ == "__main__":                      # import 시 루트 로거 전역 오염 방지 (PR#49 리뷰)
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # EDA 결과 stdout (헌법 6-1: print→logging)

df = pd.read_csv("Data/문제1(하)/train_data.csv",
                 usecols=["C64", "C6", "C7", "C42", "C10", "C33", "C11", "C12", "C65"])
df = df.sort_values("C10").reset_index(drop=True)
df["D_VDC_RES"] = df["C11"] - df["C12"]

# PM 리셋 위치
w = df.groupby("C64").agg(t0=("C10", "min"), c33=("C33", "max")).sort_values("t0").reset_index()
reset_idx = w.index[w["c33"] < w["c33"].shift(1)].tolist()[-1]
reset_wafers_post = set(w.iloc[reset_idx:]["C64"])

# step4 정착(C42=0) wafer 요약 (C6_0 생산만)
s4 = df[(df["C7"] == 4) & (df["C42"] == 0) & (df["C6"] == "C6_0")]
wf = s4.groupby("C64").agg(C11=("C11", "mean"), C12=("C12", "mean"),
                           D_VDC_RES=("D_VDC_RES", "mean"), C65=("C65", "first"),
                           t0=("C10", "min")).reset_index()
wf["is_post"] = wf["C64"].isin(reset_wafers_post)

def skew(x):
    x = x - x.mean(); m2 = (x**2).mean(); m3 = (x**3).mean()
    return m3 / m2**1.5 if m2 > 0 else 0.0

log.info("=== C6_0 step4 정착 (wafer 요약) — C11 vs D_VDC_RES ===")
log.info(f"{'지표':22s} {'C11(정착평균)':>16s} {'D_VDC_RES':>16s}")
for name, col in [("std", None), ("CV(std/|mean|)", None), ("skew", None),
                  ("|corr(C65)|", None), ("PM반응(σ단위)", None)]:
    vals = {}
    for c in ["C11", "D_VDC_RES"]:
        x = wf[c].to_numpy()
        if name == "std":
            vals[c] = np.std(x)
        elif name == "CV(std/|mean|)":
            vals[c] = np.std(x) / abs(np.mean(x)) if np.mean(x) != 0 else np.nan
        elif name == "skew":
            vals[c] = skew(x)
        elif name == "|corr(C65)|":
            vals[c] = abs(np.corrcoef(x, wf["C65"])[0, 1])
        elif name == "PM반응(σ단위)":
            pre = wf.loc[~wf["is_post"], c]; post = wf.loc[wf["is_post"], c]
            pooled = np.sqrt((pre.var() + post.var()) / 2)
            vals[c] = abs(post.mean() - pre.mean()) / pooled if pooled > 0 else 0
    log.info(f"{name:22s} {vals['C11']:>16.4f} {vals['D_VDC_RES']:>16.4f}")

log.info("")
log.info("=== C12(기준값)가 실제로 변하나 — 변해야 잔차가 의미 ===")
log.info(f"C12 std={wf['C12'].std():.2f}, C11 std={wf['C11'].std():.2f}, "
      f"corr(C11,C12)={np.corrcoef(wf['C11'],wf['C12'])[0,1]:.3f}")
log.info(f"→ C11이 C12를 따라가면 잔차(D_VDC_RES)가 '순수 이탈'을 분리. corr 높을수록 잔차의 가치↑")

log.info("")
log.info("=== 참고: 전 step C6_0 정착에서 두 피처 corr(C65) (감시 커버리지) ===")
for st in [1, 4, 5, 6, 7]:
    g = df[(df["C7"] == st) & (df["C42"] == 0) & (df["C6"] == "C6_0")]
    gw = g.groupby("C64").agg(C11=("C11", "mean"), D=("D_VDC_RES", "mean"),
                              C65=("C65", "first")).dropna()
    if len(gw) < 30:
        continue
    c11c = abs(np.corrcoef(gw["C11"], gw["C65"])[0, 1])
    dc = abs(np.corrcoef(gw["D"], gw["C65"])[0, 1])
    log.info(f"  step{st}: |corr(C65)| C11={c11c:.3f} / D_VDC_RES={dc:.3f}")
