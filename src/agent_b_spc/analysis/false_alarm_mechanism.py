"""KEEP* 오탐의 정체: (A) 밴드가 넓어 미탐  vs  (B) 정상인데 밖으로 나가 오탐.
   그리고 그 원인이 '평균선 위치 오류'인가 '분포 치우침(skew)'인가.

방법: C6_0 KEEP* 그룹 각각에서 ±3σ 밖으로 나간 '오탐' 점이
  - 위쪽(UCL 초과)/아래쪽(LCL 미만) 어디에 쏠리나
  - skew 부호와 같은 쪽인가 (같으면 = 긴 꼬리의 정상점이 밖으로)
  - 중앙값 vs 평균 위치 (평균이 꼬리로 끌려갔나)
  - 분위수 관리선으로 바꾸면 그 오탐이 사라지나

표본 = 복귀 블록(설계 R1 정합, 2026-07-12)
σ-FA는 **배포 관리선과 동일**하게 산출한다(최근 500장·1% 트림 σ) — untrimmed 전-wafer σ로 재면
밴드가 넓어 오탐이 과소평가돼 "일부 그룹은 σ가 낫다"는 오해를 만든다(정정 2026-07-13). 트림 기준으로는
6그룹 전부 σ-FA(2.6~4.2%) > 분위수-FA(0.2~0.4%).
"""
import numpy as np
import pandas as pd

import logging
log = logging.getLogger(__name__)
if __name__ == "__main__":                      # import 시 루트 로거 전역 오염 방지 (PR#49 리뷰)
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # EDA 결과 stdout (헌법 6-1: print→logging)

df = pd.read_csv("Data/문제1(하)/train_data.csv",
                 usecols=["C64","C6","C7","C42","C10","C33"] +
                         [f"C{n}" for n in (57,58,61,31)])
df = df.sort_values("C10")
# post-reset C6_0만 (baseline 표본)
w = df.groupby("C64").agg(t0=("C10","min"), c33=("C33","max")).sort_values("t0").reset_index()
ri = w.index[w["c33"] < w["c33"].shift(1)].tolist()[-1]
seg = w.iloc[ri:].merge(df.groupby("C64")["C6"].first().rename("c6"), on="C64")
last_c61 = seg[seg.c6 == "C6_1"]["t0"].max()
post = set(seg[(seg.c6 == "C6_0") & (seg.t0 > last_c61)]["C64"])   # 복귀 블록(최근 레짐) — 스펙 R1 정합

# C6_0 KEEP* 6그룹: (sensor, step)
groups = [("C61",1),("C57",4),("C58",4),("C31",5),("C61",6),("C61",7)]

def skew(x):
    x = np.asarray(x,float); x = x-x.mean(); m2=(x**2).mean(); m3=(x**3).mean()
    return m3/m2**1.5 if m2>0 else 0.0

log.info(f"{'그룹':10s} {'skew':>6s} {'평균-중앙값(σ)':>12s} "
      f"{'오탐%':>6s} {'└위쪽':>6s} {'└아래':>6s} {'꼬리쪽?':>7s} {'분위수후%':>8s}")
# 배포 관리선과 동일 산출(params.yaml 값): 최근 ROLLING_N·σ는 1% 트림 표본·FA는 트림 전 v에 적용
ROLLING_N, TRIM_Q, Q_LOW, Q_HIGH = 500, 0.01, 0.00135, 0.99865
for sen, st in groups:
    g = df[(df.C7==st)&(df.C42==0)&(df.C6=="C6_0")&(df.C64.isin(post))]
    # wafer 요약(정착 평균) — baseline 단위, 최근 ROLLING_N장(배포 롤링 창)
    v = g.groupby("C64")[sen].mean().dropna().to_numpy()[-ROLLING_N:]
    if len(v) < 30:
        continue
    lo_t, hi_t = np.quantile(v, [TRIM_Q, 1-TRIM_Q])
    vt = v[(v>=lo_t)&(v<=hi_t)]                       # 1% 트림(σ-track 전용)
    mu, sd = vt.mean(), vt.std(ddof=1)                # 배포 center/σ = 트림 표본에서
    med = np.median(v)
    ucl, lcl = mu+3*sd, mu-3*sd
    hi = (v > ucl).mean()*100
    lo = (v < lcl).mean()*100
    fa = hi + lo
    sk = skew(v)
    # 오탐이 skew 긴 꼬리쪽에 쏠렸나
    tail = "위(우꼬리)" if (sk>0 and hi>=lo) else ("아래(좌꼬리)" if (sk<0 and lo>=hi) else "혼합")
    # 분위수 관리선(0.135%~99.865%, 트림 전 v)으로 바꾸면 자기표본 오탐
    qlo, qhi = np.quantile(v, [Q_LOW, Q_HIGH])
    fa_q = ((v<qlo)|(v>qhi)).mean()*100
    log.info(f"{sen}-s{st:<6d} {sk:>6.2f} {(mu-med)/sd:>12.2f} "
          f"{fa:>6.2f} {hi:>6.2f} {lo:>6.2f} {tail:>7s} {fa_q:>8.2f}")

log.info("")
log.info("해석: 평균-중앙값(σ)>0 = 평균이 우꼬리로 끌려감(중앙값보다 오른쪽).")
log.info("      오탐이 '꼬리쪽'에 쏠리고 분위수후 급감 = 긴 꼬리의 '정상점'이 대칭밴드 밖으로 나간 것.")
