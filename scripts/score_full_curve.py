# -*- coding: utf-8 -*-
"""전 구간 AE 스코어 곡선 산출 — e4 번들로 덤프 전체(68일·11,936장) 스코어링.

목적: "학습 구간은 조용, PM 경계 너머는 경보"를 **곡선 전체**로 보이기 위한 원자료.
산출: Data/ae_repro/ae_score_curve_e4.csv
      [wafer, chamber, idx(챔버 내 시간순), ae_raw, ae_score, ae_drift]

실행 (_ae_lab에서 — 실험실 사본의 ae_pipeline 사용):
  cd <레포 루트>\\_ae_lab
  python -W "ignore::FutureWarning" ..\\scripts\\score_full_curve.py

소요 ~2-4분(CPU). 읽기 전용 — 번들·DB 어떤 상태도 바꾸지 않는다.
⚠️ PM 대행 (A 부재) — src/agent_a_mlops 소유권상 A 리뷰 필수 (헌법 3-1).
"""
import sys
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.WARNING)

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "models" / "anomaly_ae" / "ae_cand_seg1pre_e4_20260806"
DUMP = ROOT / "Data" / "ae_repro" / "ae_input_sim_postguard_full.csv"
OUT = ROOT / "Data" / "ae_repro" / "ae_score_curve_e4.csv"

sys.path.insert(0, str(Path.cwd()))
from ae_pipeline.infer import AEModel  # noqa: E402


def main() -> None:
    """전체 wafer 스코어링 → 챔버 내 시간순 인덱스 부여 → CSV."""
    m = AEModel.from_bundle(str(BUNDLE))
    print(f"번들 {BUNDLE.name} (model={m.model_version}) 로드")

    df = pd.read_csv(DUMP, low_memory=False)
    print(f"덤프 {len(df):,}행 / wafer {df['C64'].nunique():,}장 — 스코어링 시작…")

    recs = m.score_wafers(df, chamber_col="C24", with_drift=True)
    out = pd.DataFrame([{"wafer": r["wafer"], "ae_raw": r["ae_raw"],
                         "ae_score": r["ae_score"], "ae_drift": r.get("ae_drift_score")}
                        for r in recs])

    meta = (df.groupby("C64", sort=False)
              .agg(chamber=("C24", "first"), t=("C10", "min")).reset_index()
              .rename(columns={"C64": "wafer"}))
    out = out.merge(meta, on="wafer", how="left")
    out = out.sort_values(["chamber", "t"], kind="mergesort")
    out["idx"] = out.groupby("chamber").cumcount()          # 챔버 내 시간순 0-based
    out = out[["wafer", "chamber", "idx", "ae_raw", "ae_score", "ae_drift"]]
    out.to_csv(OUT, index=False, encoding="utf-8")

    print(f"\n→ {OUT.name}: {len(out):,}행")
    for ch, g in out.groupby("chamber"):
        pre, post = g[g["idx"] < 871], g[g["idx"] >= 923]
        print(f"  {ch}: 학습구간(0~870) 평균 {pre['ae_score'].mean():.3f} "
              f"· >0.2 비율 {(pre['ae_score'] > 0.2).mean() * 100:.1f}%  |  "
              f"PM후(923~) 평균 {post['ae_score'].mean():.3f} "
              f"· >0.2 비율 {(post['ae_score'] > 0.2).mean() * 100:.1f}%")


if __name__ == "__main__":
    main()
