# -*- coding: utf-8 -*-
"""AE 재보정 수준 진단: frozen 채점과 수준 1/2 게이트 프리뷰."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "agent_a_mlops" / "Autoencoder"))

from ae_pipeline import io_bundle  # noqa: E402
from ae_pipeline.calibration import apply_calibration, fit_calibration  # noqa: E402
from ae_pipeline.constants import feat_weight_vector  # noqa: E402
from ae_pipeline.features import extract_features, prepare_frame  # noqa: E402
from ae_pipeline.retrain import load_data, prepare_input  # noqa: E402
from ae_pipeline.validate_bundle import (  # noqa: E402
    check_quiet_and_thresholds,
    load_gate_params,
    probe_detection,
)

log = logging.getLogger("ae.diag")

DATA_DIR = ROOT / "Data" / "ae_repro"
DEFAULT_BUNDLE = ROOT / "models" / "anomaly_ae" / "ae_v1"
DEFAULT_REPORT = DATA_DIR / "diag_ae_recalib_report.md"
PCTS = [50, 90, 98.5, 99.5, 99.9]
CTL_NOISY = 0.05
CTL_SAT_MAX = 0.02


def _rank(a: np.ndarray) -> np.ndarray:
    """동점에 평균 순위를 주는 Spearman 순위."""
    return pd.Series(np.asarray(a, float)).rank(method="average").to_numpy(float, copy=True)


def spearman(a, b) -> float:
    """쌍 wafer raw 순위 상관. 수준 판정은 게이트 프리뷰가 담당한다."""
    ra, rb = _rank(np.asarray(a, float)), _rank(np.asarray(b, float))
    ra -= ra.mean()
    rb -= rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    if denom == 0:
        raise ValueError("순위 상관 분모 0 - 표본/분산 확인")
    return float((ra * rb).sum() / denom)


def load_bundle(bundle_dir: Path, device=None) -> dict:
    """완결·해시를 확인하고 frozen 번들을 로드한다."""
    try:
        from ae_pipeline.model import Scorer, load_model
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "torch/모델 로드 실패 - foreground 터미널 실행 여부 확인 (TSR-0001)"
        ) from exc

    expected = len(io_bundle.ARTIFACT_NAMES) - 1
    verified = io_bundle.verify_bundle(bundle_dir)
    bad = [name for name, result in verified.items() if not result["ok"]]
    if bad or len(verified) != expected:
        raise ValueError(
            f"번들 무결성 실패: mismatch={bad}, hashes={len(verified)}/{expected}"
        )

    A = io_bundle.ARTIFACT_NAMES
    spec = io_bundle.load_json(bundle_dir / A["feature_spec"])
    feat_cols = list(spec["columns"])
    model = load_model(
        bundle_dir / A["model"],
        d=int(spec.get("n_features", len(feat_cols))),
        device=device,
    )
    return {
        "model": model,
        "scorer": Scorer.load_stats(bundle_dir / A["scorer"], model, device=device),
        "scaler": io_bundle.load_scaler(bundle_dir / A["scaler"]),
        "calib": io_bundle.load_json(bundle_dir / A["calib"]),
        "feat_cols": feat_cols,
        "w_vec": feat_weight_vector(feat_cols),
    }


def featurize(csv_path: Path, feat_cols: list) -> tuple[list, pd.DataFrame, pd.Series, list]:
    """CSV를 현행 추론 경로로 피처화하고 정상 wafer 목록을 반환한다."""
    df = load_data(csv_path)
    if "C65" in df.columns:
        raise ValueError(f"금지 컬럼 C65가 입력에 존재합니다: {csv_path}")
    missing_schema = [col for col in ("C24", "C64") if col not in df.columns]
    if missing_schema:
        raise ValueError(f"필수 컬럼 누락 {missing_schema}: {csv_path}")

    df, _ = prepare_input(df, "auto")
    df, _ = prepare_frame(df)
    order = df.groupby("C64")["C10"].min().sort_values(kind="mergesort").index.tolist()
    F = extract_features(df).reindex(order).fillna(0.0)
    missing = [col for col in feat_cols if col not in F.columns]
    if missing:
        raise ValueError(f"피처 {len(missing)}개 누락 (예: {missing[:5]})")

    cham = df.groupby("C64")["C24"].first().reindex(order).astype(str)
    normal_order = list(order)
    if "C6" in df.columns:
        labels = df.groupby("C64")["C6"].first().reindex(order).astype(str)
        normal_order = [wafer for wafer in order if labels.loc[wafer] == "C6_0"]
        if not normal_order:
            raise ValueError(f"C6_0 정상 wafer가 없습니다: {csv_path}")
    return order, F, cham, normal_order


def _X(scaler, F: pd.DataFrame, feat_cols: list) -> np.ndarray:
    return scaler.transform(F.reindex(columns=feat_cols, fill_value=0.0))


def dataset_stats(name, raw, score, sat_x, qual, cham) -> dict:
    """frozen 번들 채점 요약."""
    per_chamber = {}
    for chamber in sorted(cham.unique()):
        mask = (cham == chamber).to_numpy()
        per_chamber[chamber] = {
            "n": int(mask.sum()),
            "sat": round(float(np.mean(raw[mask] > sat_x)), 4),
            "over_qual": round(float(np.mean(score[mask] > qual)), 4),
            "raw_p50": round(float(np.median(raw[mask])), 1),
        }
    return {
        "name": name,
        "n": len(raw),
        "raw_pcts": {f"P{p:g}": round(float(np.percentile(raw, p)), 1) for p in PCTS},
        "raw_max": round(float(np.max(raw)), 1),
        "saturation": round(float(np.mean(raw > sat_x)), 4),
        "over_qual": round(float(np.mean(score > qual)), 4),
        "score_med": round(float(np.median(score)), 4),
        "per_chamber": per_chamber,
    }


def preview(tag, scorer, scaler, F_fit, F_hold, feat_cols, gate) -> dict:
    """적합/홀드아웃으로 B·C·D 게이트를 미리 평가한다."""
    X_fit = _X(scaler, F_fit, feat_cols)
    X_hold = _X(scaler, F_hold, feat_cols)
    calib = fit_calibration(scorer.ae_raw(X_fit))

    checks = [{
        "check": "D4_적합_표본_하한",
        "pass": len(F_fit) >= gate["min_val_wafers"],
        "detail": f"{len(F_fit)} >= {gate['min_val_wafers']}",
    }]
    quiet_checks, metrics = check_quiet_and_thresholds(
        scorer, X_hold, calib, gate, len(F_hold), on_holdout=True
    )
    checks.extend(quiet_checks)

    probe = {}
    if metrics.get("scored"):
        probe_checks, probe = probe_detection(
            scorer, scaler, F_fit, feat_cols, calib, gate
        )
        checks.extend(probe_checks)
    else:
        checks.append({
            "check": "C1_프로브_검출률",
            "pass": False,
            "detail": "캘리 구조 위반으로 채점 불가",
        })

    passed = all(item["pass"] for item in checks)
    failed = [item["check"] for item in checks if not item["pass"]]
    log.info("[%s] %s", tag, "전 항목 PASS" if passed else "FAIL: " + ", ".join(failed))
    return {
        "tag": tag,
        "pass": passed,
        "checks": checks,
        "metrics": metrics,
        "calib": calib,
        "probe": probe,
    }


def write_report(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("리포트 저장: %s", path)


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | [ae.diag] %(message)s",
    )
    ap = argparse.ArgumentParser(description="AE 재보정 수준 판정 (게이트 프리뷰)")
    ap.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    ap.add_argument("--control", type=Path, default=DATA_DIR / "ae_input_control.csv")
    ap.add_argument("--sim", type=Path, default=DATA_DIR / "ae_input_sim.csv")
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--holdout-frac", type=float, default=0.2)
    ap.add_argument("--device", default=None)
    args = ap.parse_args(argv)
    if not 0.0 < args.holdout_frac < 1.0:
        raise ValueError("--holdout-frac은 0과 1 사이여야 합니다")

    bundle = load_bundle(args.bundle, device=args.device)
    gate = load_gate_params()
    sat_x = float(bundle["calib"]["anchors_x"][-1])
    qual = float(bundle["calib"]["qual_threshold"])

    stats, raws, packs = {}, {}, {}
    for name, path in (("control", args.control), ("sim", args.sim)):
        order, feats, chambers, normal_order = featurize(path, bundle["feat_cols"])
        raw = bundle["scorer"].ae_raw(_X(bundle["scaler"], feats, bundle["feat_cols"]))
        score = apply_calibration(raw, bundle["calib"])
        stats[name] = dataset_stats(name, raw, score, sat_x, qual, chambers)
        raws[name] = pd.Series(raw, index=order)
        packs[name] = (order, feats, chambers, normal_order)
        log.info(
            "[%s] %d장 | 포화율 %.2f%% | score>%.1f %.2f%%",
            name,
            len(order),
            stats[name]["saturation"] * 100,
            qual,
            stats[name]["over_qual"] * 100,
        )

    control_set, sim_set = set(packs["control"][0]), set(packs["sim"][0])
    if control_set != sim_set:
        raise ValueError(
            f"paired wafer 불일치: control={len(control_set)}, sim={len(sim_set)}, "
            f"common={len(control_set & sim_set)}"
        )
    common = [wafer for wafer in packs["control"][0] if wafer in sim_set]
    rho = spearman(raws["control"].loc[common], raws["sim"].loc[common])
    recon_sim = bundle["scorer"].recon_rmse(
        _X(bundle["scaler"], packs["sim"][1], bundle["feat_cols"])
    )
    log.info(
        "쌍 Spearman %.3f | sim recon_rmse %.3f (B2 상한 %.1f)",
        rho,
        recon_sim,
        gate["recon_rmse_max"],
    )

    control = stats["control"]
    previews = []
    if control["saturation"] >= CTL_SAT_MAX or control["over_qual"] >= CTL_NOISY:
        verdict = (
            "오프셋 무관 - control부터 시끄러움. base 학습 레짐과 입력 레짐의 "
            "불일치 여부를 먼저 규명한 뒤 재판단"
        )
    else:
        _, sim_feats, _, normal_order = packs["sim"]
        n_hold = max(int(len(normal_order) * args.holdout_frac), 1)
        fit_wafers, hold_wafers = normal_order[:-n_hold], normal_order[-n_hold:]
        F_fit, F_hold = sim_feats.loc[fit_wafers], sim_feats.loc[hold_wafers]
        log.info("sim 정상 분할: fit %d / holdout %d", len(F_fit), len(F_hold))

        p1 = preview(
            "수준1 calib만",
            bundle["scorer"],
            bundle["scaler"],
            F_fit,
            F_hold,
            bundle["feat_cols"],
            gate,
        )
        previews.append(p1)
        if p1["pass"]:
            verdict = "수준 1 - calib(앵커)+drift 재산출"
        else:
            from ae_pipeline.model import build_scorer

            scorer2 = build_scorer(
                bundle["model"],
                _X(bundle["scaler"], F_fit, bundle["feat_cols"]),
                bundle["feat_cols"],
                bundle["w_vec"],
                device=args.device,
            )
            p2 = preview(
                "수준2 scorer+calib",
                scorer2,
                bundle["scaler"],
                F_fit,
                F_hold,
                bundle["feat_cols"],
                gate,
            )
            previews.append(p2)
            verdict = (
                "수준 2 - scorer+calib+drift 재산출"
                if p2["pass"]
                else "수준 3 또는 시뮬 재시딩 - 수준 1/2 모두 FAIL, PM 승인 필요"
            )

    lines = [
        "# AE 재보정 진단 리포트",
        "",
        f"- 번들: `{args.bundle}`",
        f"- 입력: `{args.control}` / `{args.sim}`",
        f"- 판정: **{verdict}**",
        f"- 쌍 Spearman: {rho:.3f}",
        f"- sim recon_rmse: {recon_sim:.4f} (상한 {gate['recon_rmse_max']})",
        "",
        "## Frozen 채점",
        "",
        "| 세트 | n | P50 | P90 | P98.5 | P99.5 | P99.9 | max | 포화율 | score>qual | 중앙값 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in stats.values():
        p = item["raw_pcts"]
        lines.append(
            f"| {item['name']} | {item['n']} | {p['P50']} | {p['P90']} | "
            f"{p['P98.5']} | {p['P99.5']} | {p['P99.9']} | {item['raw_max']} | "
            f"{item['saturation']:.2%} | {item['over_qual']:.2%} | {item['score_med']} |"
        )
    lines.extend(["", "### 챔버별", ""])
    for item in stats.values():
        detail = " / ".join(
            f"{ch}: n={v['n']}, sat={v['sat']:.1%}, >qual={v['over_qual']:.1%}, P50={v['raw_p50']}"
            for ch, v in item["per_chamber"].items()
        )
        lines.append(f"- **{item['name']}**: {detail}")

    for result in previews:
        lines.extend([
            "",
            f"## 게이트 프리뷰 - {result['tag']} ({'PASS' if result['pass'] else 'FAIL'})",
            "",
            "| 검사 | 판정 | 상세 |",
            "|---|---|---|",
        ])
        for check in result["checks"]:
            lines.append(
                f"| {check['check']} | {'PASS' if check['pass'] else 'FAIL'} | {check['detail']} |"
            )
        by_sigma = result["probe"].get("by_sigma", {})
        if by_sigma:
            lines.append(
                "| 프로브 sigma별 | - | "
                + " / ".join(f"{key}={value:.1%}" for key, value in by_sigma.items())
                + " |"
            )
        lines.append(
            f"| 후보 anchors_x | - | {[round(x, 3) for x in result['calib']['anchors_x']]} |"
        )

    write_report(args.report, lines)
    log.info("판정: %s", verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
