# -*- coding: utf-8 -*-
"""ae_raw 판별력 분석 — 챔버별 앵커(A안)로 풀리는 문제인지 판정한다.

배경 (2026-08-03): 게이트 E군 실측에서 **검출률 ≈ 오경보율**이 나왔다 (CH2 40.5% vs 35.0%,
CH3 11.8% vs 10.0%). 즉 현행 점수에는 판별력이 사실상 없다. 원인 후보가 둘이고 처방이 갈린다:

  ⓐ **천장 눌림** — 정상이 이미 score 1.0 에 붙어(CH2 정상의 15%) 그 위 이상이 더 못 올라감.
     `ae_raw` 에서는 갈려 있을 수 있다 → **챔버별 앵커(A안)로 해결**
  ⓑ **원천 겹침** — raw 단계에서 이미 정상과 이상이 겹침 → 눈금을 어떻게 옮겨도 안 됨
     → **오프셋 하향(C안)** 이 유일

점수는 [0,1] 로 클립되지만 raw 는 클립되지 않으므로, raw 분포를 보면 둘이 갈린다.

측정 (챔버별, 정상 vs 주입):
  · AUC        — 순위 기반 판별력. 0.5=무판별 / 0.8↑=쓸 만함 / 1.0=완전분리 (임계 무관)
  · 분리도 d   — (주입 중앙값 − 정상 중앙값) / 정상 MAD*1.4826 (강건 표준화)
  · 최적 임계 — 그 챔버 raw 에서 오경보 2% 를 만족하는 지점의 검출률 (= A안 상한 추정)

출력: 재보정용 데이터/raw_separability_report.md

⚠️ 실행: 사용자 터미널 foreground (torch — docs/adr/TSR-0001).
    python scripts/analyze_raw_separability.py --bundle models/anomaly_ae/ae_cand_clean_chamber
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "agent_a_mlops" / "Autoencoder"))

from ae_pipeline import io_bundle                                        # noqa: E402
from ae_pipeline.calibration import apply_calibration, fit_calibration   # noqa: E402
from ae_pipeline.features import extract_features, prepare_frame         # noqa: E402
from ae_pipeline.retrain import VAL_WAFERS_SIDECAR, load_data, prepare_input  # noqa: E402

log = logging.getLogger("ae.sep")

DATA_DIR = ROOT / "src" / "agent_a_mlops" / "Autoencoder" / "재보정용 데이터"
DEFAULT_BUNDLE = ROOT / "models" / "anomaly_ae" / "ae_cand_clean_chamber"
REPORT = DATA_DIR / "raw_separability_report.md"
L5_TARGET = 0.02          # 챔버별 앵커가 목표로 할 오경보율 (게이트 B1 상한)
WARMUP_K = 20             # drift 램프 초기 제외 (게이트 E군과 동일 규약)
AUC_USABLE = 0.80         # 이 이상이면 "raw 에 판별력 있음" — A안 유효
AUC_NONE = 0.60           # 이 미만이면 원천 겹침 — C안 필요


def auc(pos, neg) -> float:
    """순위 기반 AUC (Mann-Whitney U / mn). 임계와 무관한 판별력 지표."""
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    r = pd.Series(allv).rank(method="average").to_numpy(float, copy=True)
    return float((r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def robust_d(pos, neg) -> float:
    """(주입 중앙값 − 정상 중앙값) / 정상 강건표준편차. 분모 0 은 예외 (헌법 7장)."""
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    mad = float(np.median(np.abs(neg - np.median(neg)))) * 1.4826
    if not np.isfinite(mad) or mad <= 0:
        raise ValueError("정상 raw 의 강건 표준편차가 0 — 표본/분포 확인")
    return float((np.median(pos) - np.median(neg)) / mad)


def detect_at_fpr(pos, neg, fpr=L5_TARGET) -> tuple[float, float]:
    """정상에서 오경보 `fpr` 이 되는 raw 임계와, 그 임계에서의 주입 검출률.

    **챔버별 앵커(A안)를 썼을 때 기대되는 성능의 추정**이다 — 앵커는 그 챔버 자기 분포의
    분위수이므로, "오경보 2% 지점"이 곧 그 챔버의 임계가 된다.
    """
    pos, neg = np.asarray(pos, float), np.asarray(neg, float)
    thr = float(np.percentile(neg, 100 * (1 - fpr)))
    return thr, float(np.mean(pos > thr))


def score_all(bundle_dir: Path, data_path: Path, device=None) -> pd.DataFrame:
    """전 wafer 를 번들로 채점 → wafer·chamber·raw·score 프레임."""
    from ae_pipeline.model import Scorer, load_model       # torch 지연 import

    bad = [n for n, v in io_bundle.verify_bundle(bundle_dir).items() if not v["ok"]]
    if bad:
        raise ValueError(f"번들 sha256 불일치 {bad} (불가침 12)")
    A = io_bundle.ARTIFACT_NAMES
    spec = io_bundle.load_json(bundle_dir / A["feature_spec"])
    feat_cols = list(spec["columns"])
    model = load_model(bundle_dir / A["model"],
                       d=int(spec.get("n_features", len(feat_cols))), device=device)
    scorer = Scorer.load_stats(bundle_dir / A["scorer"], model, device=device)
    scaler = io_bundle.load_scaler(bundle_dir / A["scaler"])
    calib = io_bundle.load_json(bundle_dir / A["calib"])

    df = load_data(data_path)
    df, _ = prepare_input(df, "auto")
    df, _ = prepare_frame(df)
    order = df.groupby("C64")["C10"].min().sort_values(kind="mergesort").index.tolist()
    F = extract_features(df).reindex(order).fillna(0.0)
    missing = [c for c in feat_cols if c not in F.columns]
    if missing:
        raise ValueError(f"피처 {len(missing)}개 누락 (예: {missing[:5]})")
    X = scaler.transform(F.reindex(columns=feat_cols, fill_value=0.0))
    raw = scorer.ae_raw(X)
    cham = df.groupby("C64")["C24"].first().reindex(order).astype(str)
    return pd.DataFrame({"C64": [str(w) for w in order], "chamber": cham.values,
                         "raw": raw, "score": apply_calibration(raw, calib)}), calib


def analyze(sc: pd.DataFrame, labels: pd.DataFrame, calib: dict) -> tuple[list, dict]:
    """챔버별 정상/주입 raw 판별력 산출. (행 목록, 요약) 반환."""
    lab = labels.copy()
    lab["C64"] = lab["C64"].astype(str)
    m = sc.merge(lab[["C64", "injected", "k", "recipe_normal"]], on="C64", how="left")
    m["injected"] = m["injected"].fillna(False).astype(bool)
    top = float(calib["anchors_x"][-1])
    qual_x = float(np.interp(float(calib["qual_threshold"]),
                             calib["anchors_y"], calib["anchors_x"]))   # score 0.2 의 raw 좌표

    rows = []
    for ch, g in m.groupby("chamber"):
        neg = g[(~g.injected) & (g.recipe_normal.fillna(True))]["raw"].values
        pos = g[g.injected & (pd.to_numeric(g.k, errors="coerce").fillna(999) >= WARMUP_K)]["raw"].values
        if len(pos) == 0:
            rows.append({"챔버": ch, "정상n": len(neg), "주입n": 0, "AUC": None})
            continue
        thr, det = detect_at_fpr(pos, neg)
        rows.append({
            "챔버": ch, "정상n": len(neg), "주입n": len(pos),
            "정상 raw P50": round(float(np.median(neg)), 1),
            "주입 raw P50": round(float(np.median(pos)), 1),
            "AUC": round(auc(pos, neg), 3),
            "분리도 d": round(robust_d(pos, neg), 2),
            "현행 검출": round(float(np.mean(pos > qual_x)), 3),
            "현행 오경보": round(float(np.mean(neg > qual_x)), 3),
            "챔버앵커 임계": round(thr, 1),
            "챔버앵커 검출@FPR2%": round(det, 3),
            "정상 천장초과": round(float(np.mean(neg > top)), 3),
        })
    aucs = [r["AUC"] for r in rows if r.get("AUC") is not None]
    summary = {"auc_min": min(aucs) if aucs else float("nan"),
               "auc_mean": float(np.mean(aucs)) if aucs else float("nan"),
               "global_qual_raw": round(qual_x, 1), "anchor_top_raw": round(top, 1)}
    return rows, summary


def verdict(rows, summary) -> str:
    """A안(챔버별 앵커) 유효성 판정."""
    tgt = [r for r in rows if r.get("주입n")]
    if not tgt:
        return "판정 불가 — 주입 챔버 표본 없음"
    worst = min(r["AUC"] for r in tgt)
    gain = [r for r in tgt if r["챔버앵커 검출@FPR2%"] > r["현행 검출"]]
    if worst >= AUC_USABLE:
        return (f"**A안 유효** — raw 판별력 살아있음(최저 AUC {worst:.3f} ≥ {AUC_USABLE}). "
                "현행 무판별은 점수 천장 눌림의 결과이며, 챔버별 앵커로 해소된다.")
    if worst < AUC_NONE:
        return (f"**A안 불충분 — C안 필요** (최저 AUC {worst:.3f} < {AUC_NONE}). "
                "raw 단계에서 이미 정상과 이상이 겹쳐 있어 눈금 조정으로는 분리되지 않는다. "
                "챔버 오프셋을 이상 크기 아래로 낮춰야 한다.")
    return (f"**혼재 — 챔버별 판단 필요** (최저 AUC {worst:.3f}). "
            f"개선 챔버 {len(gain)}/{len(tgt)}. 챔버앵커 검출률 열을 보고 결정.")


def write_report(path: Path, rows, summary, verd, bundle) -> None:
    """판정 리포트 저장."""
    L = ["# ae_raw 판별력 분석 — A안(챔버별 앵커) 유효성 판정", "",
         f"- 번들: `{bundle}` · warmup k<{WARMUP_K} 제외 · 목표 오경보 {L5_TARGET:.0%}",
         f"- score 0.2 의 raw 좌표 = {summary['global_qual_raw']} · 앵커 천장 = {summary['anchor_top_raw']}",
         f"- 판정: {verd}", "",
         "| " + " | ".join(rows[0].keys()) + " |",
         "|" + "---|" * len(rows[0])]
    for r in rows:
        L.append("| " + " | ".join("—" if v is None else str(v) for v in r.values()) + " |")
    L += ["", "## 읽는 법", "",
          "- **AUC**: 임계와 무관한 순위 판별력. 0.5=무판별, 0.8↑=쓸 만함, 1.0=완전분리.",
          "  점수(score)가 천장에 눌려도 raw 순위는 보존되므로 여기서 진짜 능력이 드러난다.",
          "- **현행 검출/오경보**: 전역 앵커 기준(지금 상태). 둘이 비슷하면 판별력 없음.",
          "- **챔버앵커 검출@FPR2%**: 그 챔버 자기 분포에서 오경보 2% 지점의 검출률 —",
          "  **A안을 적용했을 때 기대되는 성능**이다. 현행 검출보다 크게 높으면 A안이 답이다.",
          "- **정상 천장초과**: 정상인데 앵커 최상단을 넘은 비율. 높을수록 점수가 뭉개진다."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("리포트 저장: %s", path)


def main(argv=None) -> int:
    """raw 판별력 분석 실행."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | [ae.sep] %(message)s")
    ap = argparse.ArgumentParser(description="ae_raw 판별력 — A안 유효성 판정")
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    ap.add_argument("--data", type=Path, default=DATA_DIR / "ae_input_sim_v2.csv")
    ap.add_argument("--labels", type=Path, default=DATA_DIR / "injection_labels.csv")
    ap.add_argument("--report", type=Path, default=REPORT)
    ap.add_argument("--device", default=None)
    a = ap.parse_args(argv)

    sc, calib = score_all(a.bundle, a.data, device=a.device)
    labels = pd.read_csv(a.labels)
    for c in ("C64", "injected"):
        if c not in labels.columns:
            raise ValueError(f"라벨 필수 컬럼 누락 {c}: {a.labels}")
    rows, summary = analyze(sc, labels, calib)
    verd = verdict(rows, summary)
    for r in rows:
        log.info("  %s: AUC %s · 현행 검출 %s / 오경보 %s → 챔버앵커 검출 %s",
                 r["챔버"], r.get("AUC"), r.get("현행 검출"), r.get("현행 오경보"),
                 r.get("챔버앵커 검출@FPR2%"))
    write_report(a.report, rows, summary, verd, a.bundle)
    sc.to_csv(a.report.parent / "raw_scores_all.csv", index=False)
    log.info("★ %s", verd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
