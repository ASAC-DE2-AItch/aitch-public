"""
run_chain.py — 실데이터로 ae_raw → ae_score → ae_drift_score → 축 기여점수 전 체인 재현.

1) 원 학습셋(seg1·C6_0) 시간순 VAL로 scorer 재구성 (infer build-scorer와 수학 동일)
2) VAL ae_raw 분위수 vs calib.json anchors_x 대조 → 재현 검증
3) 전체 wafer 스코어링 + 챔버별 EWMA drift
4) 설계서 §3 산식으로 Anomaly축·Drift축·score_ae 산출
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/sessions/sleepy-zealous-cerf/mnt/AITCH-common-contextscore")
AE = ROOT / "src/agent_a_mlops/Autoencoder"
ART = AE / "artifacts"
sys.path.insert(0, str(AE))
sys.path.insert(0, "/sessions/sleepy-zealous-cerf/mnt/outputs")

from ae_pipeline.features import prepare_frame, find_regime_boundary, extract_features  # noqa: E402
from ae_pipeline.constants import STR_COLS, CORRUPT_COL, feat_weight_vector, sha1_8  # noqa: E402
from ae_numpy_shim import (load_state_dict, load_quantile_transformer,  # noqa: E402
                           quantile_transform_normal, NpScorer)

DDIR = ROOT / "Data/문제1(하)"
DATA = [DDIR / "train_data.csv", DDIR / "valid_X.csv", DDIR / "test_X.csv"]


def load_data(paths):
    """원 merged_data_v3 재구성 — train+valid+test 전 구간 병합(시간축 연속)."""
    dt = {c: "string" for c in STR_COLS}
    dt[CORRUPT_COL] = "string"
    fr = [pd.read_csv(p, dtype=dt, low_memory=False) for p in paths]
    return pd.concat(fr, ignore_index=True)


def main():
    spec = json.loads((ART / "feature_spec.json").read_text())
    feat_cols = spec["columns"]
    calib = json.loads((ART / "calib.json").read_text())
    sd = load_state_dict(ART / "model_seed42.pt")
    q, refs = load_quantile_transformer(ART / "scaler.pkl")

    print("== 데이터 로드 ==")
    df_raw = load_data(DATA)
    df, info_prep = prepare_frame(df_raw)
    print("행", len(df), "wafer", df["C64"].nunique(), "| prepare:", info_prep)

    reg = find_regime_boundary(df)
    print("레짐:", {k: (len(v) if isinstance(v, list) else v) for k, v in reg.items()})

    # 정상셋 = seg1 ∩ C6_0, 시간순
    keep = set(reg["seg1_wafers"]) if "seg1_wafers" in reg else set(df["C64"])
    sub = df[df["C64"].isin(keep) & (df["C6"] == "C6_0")]
    order = sub.groupby("C64")["C10"].min().sort_values(kind="mergesort").index.tolist()
    cut = int(len(order) * 0.8)
    train_w, val_w = order[:cut], order[cut:]
    print(f"정상 wafer {len(order)} → TRAIN {len(train_w)} / VAL {len(val_w)}")
    print("VAL 해시:", sha1_8(val_w), "(원 지문 fa500f3d)")
    print("TRAIN 해시:", sha1_8(train_w), "(원 지문 6ac80376)")

    def feats(wafers):
        F = extract_features(df[df["C64"].isin(set(wafers))]).reindex(wafers).fillna(0.0)
        return F.reindex(columns=feat_cols, fill_value=0.0)

    X_val = quantile_transform_normal(feats(val_w).values, q, refs)
    scorer = NpScorer.build(sd, X_val)
    print(f"scorer 재구성: eff_rank {scorer.eff_rank:.2f} · cond {scorer.cond_used:.1f}")

    # ── 검증: VAL ae_raw 분위수 vs calib anchors_x ──
    raw_val = scorer.ae_raw(X_val)
    got = [float(np.percentile(raw_val, p)) for p in calib["anchors_percentile"]]
    exp = calib["anchors_x"]
    print("\n== 재현 검증 (VAL ae_raw 분위수 vs calib anchors_x) ==")
    for p, g, e in zip(calib["anchors_percentile"], got, exp):
        print(f"  P{p:<5} 재현 {g:10.3f} | 원본 {e:10.3f} | 오차 {abs(g-e)/e*100:6.3f}%")

    np.save("/sessions/sleepy-zealous-cerf/mnt/outputs/raw_val.npy", raw_val)
    Path("/sessions/sleepy-zealous-cerf/mnt/outputs/val_w.json").write_text(json.dumps(list(map(str, val_w))))

    # ── 전체 wafer 스코어링 ──
    all_order = df.groupby("C64")["C10"].min().sort_values(kind="mergesort").index.tolist()
    X_all = quantile_transform_normal(feats(all_order).values, q, refs)
    raw_all = scorer.ae_raw(X_all)
    score_all = np.clip(np.interp(raw_all, calib["anchors_x"], calib["anchors_y"],
                                  left=0.0, right=1.0), 0, 1)

    meta = df.groupby("C64").agg(C24=("C24", "first"), C6=("C6", "first"),
                                 t=("C10", "min")).reindex(all_order)
    out = pd.DataFrame({
        "wafer": all_order, "chamber": meta["C24"].values, "C6": meta["C6"].values,
        "t": meta["t"].values, "seg": ["seg1" if w in keep else "seg2" for w in all_order],
        "in_val": [w in set(val_w) for w in all_order],
        "ae_raw": raw_all, "ae_score": score_all,
    })
    out.to_csv("/sessions/sleepy-zealous-cerf/mnt/outputs/wafer_scores.csv", index=False)
    print(f"\n전체 {len(out)} wafer 스코어링 → wafer_scores.csv")
    print(out.groupby("seg")["ae_score"].describe()[["count", "mean", "50%", "max"]])


if __name__ == "__main__":
    main()
