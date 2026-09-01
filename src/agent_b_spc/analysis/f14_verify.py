"""F14 검증: step5/6/7 D_VDC_RES↔C65 corr 0.78이 진짜 신호인가 교란인가.

의심: C65는 LOT 단위 타겟(헌법 1-3 누수 주의). D_VDC_RES와 C65가 둘 다
캠페인 시간에 따라 드리프트하면 가짜 상관이 생김. 아래로 판별:
 (1) post-reset 단일 레짐 내 corr (레짐 교차 드리프트 제거)
 (2) wafer 순서 추세 제거 후(1차 차분) corr — 남으면 진짜 동적 신호
 (3) LOT 내부(같은 C65) 분산 vs LOT 간 분산 — LOT 배치효과 여부
 (4) 값 범위/물리 sanity
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
df["D"] = df["C11"] - df["C12"]

w = df.groupby("C64").agg(t0=("C10", "min"), c33=("C33", "max")).sort_values("t0").reset_index()
reset_idx = w.index[w["c33"] < w["c33"].shift(1)].tolist()[-1]
post = set(w.iloc[reset_idx:]["C64"])

def corr(a, b):
    return np.corrcoef(a, b)[0, 1]

for st in [5, 6, 7]:
    g = df[(df["C7"] == st) & (df["C42"] == 0) & (df["C6"] == "C6_0")]
    gw = g.groupby("C64").agg(D=("D", "mean"), C12=("C12", "mean"), C11=("C11", "mean"),
                              C65=("C65", "first"), t0=("C10", "min")).dropna().sort_values("t0")
    gw["is_post"] = gw.index.isin(post) if False else gw["C64"].isin(post) if "C64" in gw else False
    gw = gw.reset_index()
    gw["is_post"] = gw["C64"].isin(post)

    full = corr(gw["D"], gw["C65"])
    po = gw[gw["is_post"]]
    within = corr(po["D"], po["C65"]) if po["C65"].nunique() > 1 else np.nan
    # 추세 제거: wafer 순서로 정렬 후 1차 차분
    d_diff = gw["D"].diff().dropna()
    c_diff = gw["C65"].diff().dropna()
    detr = corr(d_diff, c_diff) if c_diff.nunique() > 1 else np.nan
    # C65가 실제로 변하나 / 몇 개 값인가
    n_c65 = gw["C65"].nunique()
    # D가 C12에 좌우되나(step4처럼 무관하면 잔차 무의미)
    d_vs_c12 = corr(gw["C11"], gw["C12"])

    log.info(f"--- step{st}  (n={len(gw)}, post={gw['is_post'].sum()}) ---")
    log.info(f"  corr(D,C65)  전체={full:+.3f}  post단일레짐={within:+.3f}  추세제거(차분)={detr:+.3f}")
    log.info(f"  C65 고유값수={n_c65}  |  corr(C11,C12)={d_vs_c12:+.3f}  |  "
          f"D 평균={gw['D'].mean():.2f} std={gw['D'].std():.2f}")
    # C65와 t0(시간) 각각의 상관 — 둘 다 시간 드리프트면 가짜
    log.info(f"  corr(D,순서)={corr(np.arange(len(gw)),gw['D']):+.3f}  "
          f"corr(C65,순서)={corr(np.arange(len(gw)),gw['C65']):+.3f}")
    log.info("")
