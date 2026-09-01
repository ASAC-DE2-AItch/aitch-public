# -*- coding: utf-8 -*-
"""오경보(FA) 분해 분석 — 게이트 홀드아웃 재채점으로 "시간 드리프트 vs 표본 노이즈" 판별.

배경 (AE_재보정_설계_v1.1 §6-4): 8/2 재학습 후보들이 B1(L5)만으로 FAIL했고, 홀드아웃을
키우자 L5가 악화(2.11→3.50%)됐다 — 시간 후반 정상분포 이동 가설. 본 스크립트는 각 후보의
홀드아웃을 재채점해 오경보 wafer의 **시각·챔버·시간 위치**를 분해하고, 홀드아웃 전체의
score~time 추세를 계량해 8/3 PM 결정(학습창 정책 vs 재시딩)의 근거를 만든다.

판정 신호:
  · FA 후미 집중도(tail_share) — FA 중 시간순 뒤 30% 구간 비율
  · score~time Spearman(홀드아웃 전체) — FA 4~11장보다 강건한 연속 신호
  · 10분위 시간 구간별 L5 — "후반 군집"을 눈으로 확인하는 표

출력: 재보정용 데이터/fa_decomposition_report.md (+ 홀드아웃 per-wafer CSV)

실행(로컬 Windows): venv에서 **foreground** (torch — docs/adr/TSR-0001).
    (venv) python scripts/analyze_ae_false_alarms.py
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

from ae_pipeline import io_bundle                                   # noqa: E402
from ae_pipeline.calibration import apply_calibration               # noqa: E402
from ae_pipeline.features import extract_features, prepare_frame    # noqa: E402
from ae_pipeline.retrain import VAL_WAFERS_SIDECAR, load_data, prepare_input  # noqa: E402

log = logging.getLogger("ae.fa")

DATA_DIR = ROOT / "src" / "agent_a_mlops" / "Autoencoder" / "재보정용 데이터"
DEFAULT_DATA = DATA_DIR / "ae_input_sim_v2.csv"
DEFAULT_BUNDLES = [
    ROOT / "models" / "anomaly_ae" / "ae_cand_retrain_20260802_postreset_wideval",
    ROOT / "models" / "anomaly_ae" / "ae_cand_retrain_20260802_postreset_robust",
]
DEFAULT_REPORT = DATA_DIR / "fa_decomposition_report.md"

N_BINS = 10          # 시간 10분위 구간
TAIL_FRAC = 0.3      # "후미" 정의 — 시간순 뒤 30%
TAIL_SHARE_DRIFT = 0.75   # FA 후미 집중도 드리프트 판정 하한
RHO_DRIFT = 0.3      # score~time Spearman 드리프트 판정 하한
RHO_NOISE = 0.15     # 이하이면 노이즈 쪽


def _rank(a) -> np.ndarray:
    """평균 순위 배열. `copy=True` 필수 — pandas가 읽기 전용 뷰를 돌려주면
    이후 제자리 연산(`-=`)이 ValueError로 터진다 (diagnose_ae_recalib 동일 처방·회귀 테스트)."""
    return pd.Series(np.asarray(a, float)).rank(method="average").to_numpy(float, copy=True)


def spearman(a, b) -> float:
    """평균 순위 Spearman (scipy 무의존 — diagnose_ae_recalib와 동일 방식)."""
    ra, rb = _rank(a), _rank(b)
    ra -= ra.mean()
    rb -= rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    if denom == 0:
        raise ValueError("Spearman 분모 0 — 표본/분산 확인 (헌법 7장)")
    return float((ra * rb).sum() / denom)


def load_frame(csv_path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """CSV → (row frame, 전 wafer 피처, 챔버, wafer 시작시각 t0). 현행 추론 경로 재사용."""
    df = load_data(csv_path)
    if "C65" in df.columns:
        raise ValueError(f"금지 컬럼 C65가 입력에 존재합니다: {csv_path}")
    df, _ = prepare_input(df, "auto")
    df, _ = prepare_frame(df)
    t0 = df.groupby("C64")["C10"].min()
    order = t0.sort_values(kind="mergesort").index.tolist()
    feats = extract_features(df).reindex(order).fillna(0.0)
    cham = df.groupby("C64")["C24"].first().reindex(order).astype(str)
    return df, feats, cham, t0.reindex(order)


def load_candidate(bundle_dir: Path, device=None) -> dict:
    """후보 번들 로드 (무결성 검사 + 사이드카 홀드아웃 목록)."""
    from ae_pipeline.model import Scorer, load_model    # torch 지연 import

    bad = [n for n, v in io_bundle.verify_bundle(bundle_dir).items() if not v["ok"]]
    if bad:
        raise ValueError(f"{bundle_dir.name}: 번들 sha256 불일치 {bad} (불가침 12)")
    A = io_bundle.ARTIFACT_NAMES
    side = bundle_dir / VAL_WAFERS_SIDECAR
    if not side.exists():
        raise ValueError(f"{bundle_dir.name}: {VAL_WAFERS_SIDECAR} 없음 — 홀드아웃 복원 불가")
    sidecar = io_bundle.load_json(side)
    gate_wafers = [str(w) for w in (sidecar.get("gate") or [])]
    if not gate_wafers:
        raise ValueError(f"{bundle_dir.name}: 게이트 홀드아웃 0장")
    spec = io_bundle.load_json(bundle_dir / A["feature_spec"])
    feat_cols = list(spec["columns"])
    model = load_model(bundle_dir / A["model"],
                      d=int(spec.get("n_features", len(feat_cols))), device=device)
    return {
        "name": bundle_dir.name,
        "scorer": Scorer.load_stats(bundle_dir / A["scorer"], model, device=device),
        "scaler": io_bundle.load_scaler(bundle_dir / A["scaler"]),
        "calib": io_bundle.load_json(bundle_dir / A["calib"]),
        "feat_cols": feat_cols,
        "gate": gate_wafers,
    }


def analyze_bundle(cand: dict, feats: pd.DataFrame, cham: pd.Series,
                   t0: pd.Series) -> dict:
    """홀드아웃 재채점 → FA 분해·시간 추세 지표. (per-wafer frame 포함) 반환."""
    missing = [w for w in cand["gate"] if w not in feats.index]
    if missing:
        raise ValueError(f"{cand['name']}: 홀드아웃 {len(missing)}장이 데이터에 없음 "
                         f"(예: {missing[:3]}) — 데이터/사이드카 불일치")
    hold = sorted(cand["gate"], key=lambda w: t0.loc[w])          # 시간순 확정
    F = feats.loc[hold]
    X = cand["scaler"].transform(F.reindex(columns=cand["feat_cols"], fill_value=0.0))
    score = apply_calibration(cand["scorer"].ae_raw(X), cand["calib"])
    qual = float(cand["calib"]["qual_threshold"])

    n = len(hold)
    pos = np.arange(n, dtype=float)
    tbl = pd.DataFrame({
        "wafer": hold,
        "chamber": cham.reindex(hold).values,
        "t0": [str(t0.loc[w]) for w in hold],
        "score": np.round(score, 4),
        "time_pos_pct": np.round(100.0 * pos / max(n - 1, 1), 1),
    })
    fa = tbl[tbl["score"] > qual].copy()

    rho = spearman(pos, score)
    halves = {
        "front_l5": float((score[: n // 2] > qual).mean()),
        "back_l5": float((score[n // 2:] > qual).mean()),
    }
    bins = []
    for b in range(N_BINS):
        lo, hi = int(n * b / N_BINS), int(n * (b + 1) / N_BINS)
        seg = score[lo:hi]
        bins.append({"bin": b + 1, "n": hi - lo,
                     "l5": float((seg > qual).mean()) if hi > lo else 0.0,
                     "mean": float(seg.mean()) if hi > lo else 0.0})
    tail_cut = 100.0 * (1.0 - TAIL_FRAC)
    tail_share = float((fa["time_pos_pct"] >= tail_cut).mean()) if len(fa) else 0.0

    if len(fa) and tail_share >= TAIL_SHARE_DRIFT and rho >= RHO_DRIFT:
        verdict = "시간 드리프트 지지 — FA 후미 군집 + score~time 양의 추세"
    elif abs(rho) < RHO_NOISE and (len(fa) == 0 or tail_share <= 0.5):
        verdict = "표본 노이즈 지지 — 추세·군집 없음"
    else:
        verdict = "혼재 — 단독 판정 유보 (양 후보 교차·챔버 분해 참조)"

    per_ch = fa.groupby("chamber")["wafer"].count().to_dict() if len(fa) else {}
    log.info("[%s] 홀드아웃 %d장 · FA %d장 · L5 %.2f%% · rho %.3f · 후미집중 %.0f%% → %s",
             cand["name"], n, len(fa), 100 * len(fa) / n, rho, 100 * tail_share, verdict)
    return {"name": cand["name"], "n": n, "qual": qual, "table": tbl, "fa": fa,
            "rho": rho, "halves": halves, "bins": bins, "tail_share": tail_share,
            "per_chamber_fa": per_ch, "verdict": verdict}


def write_report(results: list, data_path: Path, out_md: Path) -> None:
    """분석 리포트(md) + 홀드아웃 per-wafer CSV 저장."""
    L = ["# AE 오경보(FA) 분해 분석 — 시간 드리프트 vs 표본 노이즈", "",
         f"- 입력: `{data_path}` · 판정 임계 qual=0.2 · 후미 정의 = 시간순 뒤 {int(TAIL_FRAC*100)}%",
         f"- 신호 기준: 드리프트 = FA 후미집중 ≥ {int(TAIL_SHARE_DRIFT*100)}% AND "
         f"Spearman ≥ {RHO_DRIFT} / 노이즈 = |ρ| < {RHO_NOISE} + 군집 없음", ""]
    for r in results:
        L += [f"## {r['name']} — **{r['verdict']}**", "",
              f"- 홀드아웃 {r['n']}장 · FA {len(r['fa'])}장 (L5 {100*len(r['fa'])/r['n']:.2f}%) · "
              f"score~time Spearman **{r['rho']:.3f}** · FA 후미집중 **{100*r['tail_share']:.0f}%**",
              f"- 전/후반 L5: {100*r['halves']['front_l5']:.2f}% → {100*r['halves']['back_l5']:.2f}%"
              f" · 챔버별 FA: {r['per_chamber_fa'] or '없음'}", "",
              "| 시간 10분위 | n | L5 | 평균 score |", "|---|---:|---:|---:|"]
        L += [f"| {b['bin']} | {b['n']} | {100*b['l5']:.1f}% | {b['mean']:.4f} |"
              for b in r["bins"]]
        L += ["", "### FA wafer 목록", "",
              "| wafer | chamber | t0 | score | 시간 위치(%) |", "|---|---|---|---:|---:|"]
        L += [f"| {row.wafer} | {row.chamber} | {row.t0} | {row.score} | {row.time_pos_pct} |"
              for row in r["fa"].itertuples()] or ["| (없음) | | | | |"]
        L.append("")
    if len(results) == 2:
        fa_a = set(results[0]["fa"]["wafer"])
        fa_b = set(results[1]["fa"]["wafer"])
        common = sorted(fa_a & fa_b)
        L += ["## 후보 간 교차", "",
              f"- 공통 FA {len(common)}장: {common if common else '없음'} — 공통이 많고 "
              "후미에 몰리면 특정 wafer·구간의 실제 이동(모델 무관) 방증", ""]
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(L) + "\n", encoding="utf-8")
    for r in results:
        csv = out_md.parent / f"fa_holdout_scores_{r['name']}.csv"
        r["table"].to_csv(csv, index=False)
    log.info("리포트 저장: %s (+ per-wafer CSV %d개)", out_md, len(results))


def main(argv=None) -> int:
    """양 후보 홀드아웃 FA 분해 실행."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | [ae.fa] %(message)s")
    ap = argparse.ArgumentParser(description="AE 오경보 분해 (시간 드리프트 판별)")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--bundles", type=Path, nargs="+", default=DEFAULT_BUNDLES)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--device", default=None)
    args = ap.parse_args(argv)

    _, feats, cham, t0 = load_frame(args.data)
    log.info("피처 준비: wafer %d장 · 데이터 %s", len(feats), args.data)
    results = [analyze_bundle(load_candidate(b, device=args.device), feats, cham, t0)
               for b in args.bundles]
    write_report(results, args.data, args.report)
    for r in results:
        log.info("★ %s → %s", r["name"], r["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
