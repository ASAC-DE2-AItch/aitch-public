"""Frozen AE의 scorer/calibration/drift를 시뮬 정상 세계에 재적합한다.

수준 1은 scorer를 유지하고 calibration/drift만, 수준 2는 scorer까지 재산출한다.
모델·스케일러·feature spec은 항상 base 번들에서 frozen 복사한다.
"""
from __future__ import annotations

import argparse
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import io_bundle
from .calibration import apply_calibration, false_alarm_rate, fit_calibration
from .constants import feat_weight_vector, sha1_8
from .drift import ALARM, ALPHA, ewma, fit_drift_baseline
from .features import extract_features, prepare_frame
from .retrain import VAL_WAFERS_SIDECAR, load_data, prepare_input
from .validate_bundle import load_gate_params

log = logging.getLogger("ae.recalibrate")


def verify_complete_bundle(bundle_dir: Path) -> dict:
    """7파일 완결과 manifest의 6개 sha256을 확인한다."""
    A = io_bundle.ARTIFACT_NAMES
    missing = [name for name in A.values() if not (bundle_dir / name).exists()]
    if missing:
        raise ValueError(f"base 번들 파일 누락: {missing}")
    verified = io_bundle.verify_bundle(bundle_dir)
    expected = len(A) - 1
    bad = [name for name, result in verified.items() if not result["ok"]]
    if bad or len(verified) != expected:
        raise ValueError(
            f"base 번들 무결성 실패: mismatch={bad}, hashes={len(verified)}/{expected}"
        )
    return io_bundle.read_manifest(bundle_dir)


def split_fit_holdout(order: list, holdout_frac: float, gate: dict) -> tuple[list, list]:
    """시간순 뒤쪽을 홀드아웃으로 떼고 게이트 표본 하한을 강제한다."""
    if not 0.0 < float(holdout_frac) < 1.0:
        raise ValueError("holdout_frac은 0과 1 사이여야 합니다")
    n_hold = max(int(len(order) * float(holdout_frac)), 1)
    fit_wafers, hold_wafers = list(order[:-n_hold]), list(order[-n_hold:])
    if len(fit_wafers) < int(gate["min_val_wafers"]):
        raise ValueError(
            f"적합 표본 부족: {len(fit_wafers)} < {gate['min_val_wafers']}"
        )
    if len(hold_wafers) < int(gate["min_gate_wafers"]):
        raise ValueError(
            f"홀드아웃 표본 부족: {len(hold_wafers)} < {gate['min_gate_wafers']}"
        )
    return fit_wafers, hold_wafers


def select_normal_order(df, normal_col="C6", normal_value="C6_0") -> list:
    """시간순 정상 wafer. 정상 컬럼이 없으면 입력 전량을 정상으로 본다."""
    order = df.groupby("C64")["C10"].min().sort_values(kind="mergesort")
    wafers = order.index.tolist()
    if normal_col and normal_col in df.columns and normal_value is not None:
        labels = df.groupby("C64")[normal_col].first().reindex(wafers).astype(str)
        wafers = [wafer for wafer in wafers if labels.loc[wafer] == str(normal_value)]
    if not wafers:
        raise ValueError("정상 wafer가 0장입니다 - 정상 필터/입력을 확인하세요")
    return wafers


def raw_drift_baseline(scores, alpha=ALPHA) -> float:
    """캡 적용 전 정상 EWMA P99. 분모 가드 판정에 사용한다."""
    values = np.asarray(scores, float)
    stream = ewma(values, e0=float(np.median(values)), alpha=alpha)
    return float(np.percentile(stream, 99))


def recalibrate(
    base_dir,
    data_path,
    out_dir,
    *,
    refit_scorer=False,
    holdout_frac=0.2,
    calib_version="calib_v2_sim",
    normal_col="C6",
    normal_value="C6_0",
    input_schema="auto",
    params_path=None,
    device=None,
    verbose=True,
) -> dict:
    """수준 1/2 후보 번들을 새 디렉터리에 완결 세트로 산출한다."""
    base_dir = Path(base_dir).resolve()
    data_path = Path(data_path).resolve()
    out_dir = Path(out_dir).resolve()
    if out_dir.exists():
        raise FileExistsError(f"출력 경로가 이미 존재합니다(덮어쓰기 금지): {out_dir}")

    from .model import Scorer, build_scorer, load_model

    base_manifest = verify_complete_bundle(base_dir)
    gate = load_gate_params(params_path)
    A = io_bundle.ARTIFACT_NAMES
    spec = io_bundle.load_json(base_dir / A["feature_spec"])
    feat_cols = list(spec["columns"])
    model = load_model(
        base_dir / A["model"],
        d=int(spec.get("n_features", len(feat_cols))),
        device=device,
    )
    scaler = io_bundle.load_scaler(base_dir / A["scaler"])

    df_raw = load_data(data_path)
    if "C65" in df_raw.columns:
        raise ValueError("금지 컬럼 C65가 재보정 입력에 존재합니다")
    df_raw, adaptation = prepare_input(df_raw, input_schema)
    df, n_dedup = prepare_frame(df_raw)
    normal_order = select_normal_order(df, normal_col, normal_value)
    fit_wafers, hold_wafers = split_fit_holdout(normal_order, holdout_frac, gate)

    normal_rows = df[df["C64"].isin(set(normal_order))]
    feats = extract_features(normal_rows).reindex(normal_order).fillna(0.0)
    missing = [col for col in feat_cols if col not in feats.columns]
    if missing:
        raise ValueError(f"feature_spec 대비 피처 {len(missing)}개 누락: {missing[:5]}")
    X_all = scaler.transform(feats.reindex(columns=feat_cols, fill_value=0.0))
    fit_idx = [normal_order.index(wafer) for wafer in fit_wafers]
    hold_idx = [normal_order.index(wafer) for wafer in hold_wafers]
    X_fit, X_hold = X_all[fit_idx], X_all[hold_idx]

    if refit_scorer:
        w_vec = feat_weight_vector(feat_cols)
        scorer = build_scorer(model, X_fit, feat_cols, w_vec, device=device)
        level = 2
    else:
        scorer = Scorer.load_stats(base_dir / A["scorer"], model, device=device)
        level = 1

    calib = fit_calibration(scorer.ae_raw(X_fit))
    scores_all = apply_calibration(scorer.ae_raw(X_all), calib)
    uncapped_b0 = raw_drift_baseline(scores_all)
    if uncapped_b0 >= ALARM:
        raise ValueError(
            f"drift baseline 분모 붕괴: uncapped B0={uncapped_b0:.6f} >= alarm={ALARM}"
        )
    drift = fit_drift_baseline(scores_all)

    raw_hold = scorer.ae_raw(X_hold)
    score_hold = apply_calibration(raw_hold, calib)
    hold_metrics = {
        "l5": round(false_alarm_rate(score_hold, calib["qual_threshold"]), 4),
        "recon_rmse": round(scorer.recon_rmse(X_hold), 4),
        "score_mean": round(float(np.mean(score_hold)), 4),
        "score_p50": round(float(np.median(score_hold)), 4),
        "score_p95": round(float(np.percentile(score_hold, 95)), 4),
    }

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir()
    shutil.copy2(base_dir / A["model"], out_dir / A["model"])
    shutil.copy2(base_dir / A["scaler"], out_dir / A["scaler"])
    shutil.copy2(base_dir / A["feature_spec"], out_dir / A["feature_spec"])
    if refit_scorer:
        scorer.save_stats(out_dir / A["scorer"])
    else:
        shutil.copy2(base_dir / A["scorer"], out_dir / A["scorer"])
    io_bundle.save_json(calib, out_dir / A["calib"])
    io_bundle.save_json(drift, out_dir / A["drift"])
    io_bundle.save_json(
        {
            "val": [str(wafer) for wafer in fit_wafers],
            "gate": [str(wafer) for wafer in hold_wafers],
            "data_path": str(data_path),
            "holdout_frac": float(holdout_frac),
            "note": "val=scorer/calib fit, gate=calib 미적합 시간순 홀드아웃",
        },
        out_dir / VAL_WAFERS_SIDECAR,
    )

    meta = {
        "ae_model_version": base_manifest.get("ae_model_version", "z16_rev3_seg1"),
        "ae_calib_version": calib_version,
        "score_contract": base_manifest.get(
            "score_contract", "conditioned residual Mahalanobis (단일 ae_raw, 불가침 12)"
        ),
        "n_features": len(feat_cols),
        "recalibration_level": level,
        "base_bundle": str(base_dir),
        "base_calib_version": base_manifest.get("ae_calib_version"),
        "data_path": str(data_path),
        "input_adaptation": adaptation,
        "selection": {
            "normal_col": normal_col if normal_col and normal_col in df.columns else None,
            "normal_value": normal_value if normal_col and normal_col in df.columns else None,
            "holdout_frac": float(holdout_frac),
            "time_order": "C10 minimum per C64",
            "chamber_scope": "integrated",
        },
        "set_sizes": {
            "normal": len(normal_order),
            "val": len(fit_wafers),
            "gate": len(hold_wafers),
            "dedup_removed": int(n_dedup),
        },
        "set_hashes": {
            "val": sha1_8(fit_wafers),
            "gate": sha1_8(hold_wafers),
        },
        "metrics_preview": hold_metrics,
        "anchors_x": [float(value) for value in calib["anchors_x"]],
        "drift_baseline_B0": round(float(drift["baseline_B0"]), 6),
        "scorer_refit": bool(refit_scorer),
        "scorer_eff_rank": scorer.eff_rank,
        "scorer_cond_used": scorer.cond_used,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ae_pipeline.recalibrate",
        "note": (
            f"재학습 아님 - 시뮬 정상 세계 수준 {level} 재보정. "
            "model/scaler/feature_spec frozen, 7파일 세트 동기."
        ),
    }
    manifest = io_bundle.write_manifest(out_dir, meta)
    verified = io_bundle.verify_bundle(out_dir)
    bad = [name for name, result in verified.items() if not result["ok"]]
    if bad or len(verified) != len(A) - 1:
        raise RuntimeError(f"산출 번들 자체 검증 실패: mismatch={bad}")

    if verbose:
        old_calib = io_bundle.load_json(base_dir / A["calib"])
        log.info("수준 %d 재보정 후보 산출: %s", level, out_dir)
        log.info("정상 fit %d / holdout %d", len(fit_wafers), len(hold_wafers))
        log.info("anchors_x 구=%s", [round(x, 3) for x in old_calib["anchors_x"]])
        log.info("anchors_x 신=%s", [round(x, 3) for x in calib["anchors_x"]])
        log.info(
            "홀드아웃 L5 %.4f / recon %.4f / mean %.4f / P95 %.4f / drift B0 %.4f",
            hold_metrics["l5"],
            hold_metrics["recon_rmse"],
            hold_metrics["score_mean"],
            hold_metrics["score_p95"],
            drift["baseline_B0"],
        )
    return {"out_dir": str(out_dir), "manifest": manifest, "metrics": hold_metrics}


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ae_pipeline.recalibrate",
        description="Frozen AE 수준 1/2 재보정 후보 번들 산출",
    )
    ap.add_argument("--base", required=True, help="기준 번들(예: ae_v1)")
    ap.add_argument("--data", required=True, help="시뮬 정상 CSV")
    ap.add_argument("--out", required=True, help="신규 후보 번들 디렉터리")
    ap.add_argument("--refit-scorer", action="store_true", help="수준 2 scorer 재적합")
    ap.add_argument("--holdout-frac", type=float, default=0.2)
    ap.add_argument("--calib-version", default="calib_v2_sim")
    ap.add_argument("--normal-col", default="C6")
    ap.add_argument("--normal-value", default="C6_0")
    ap.add_argument("--input-schema", choices=["auto", "merged", "ct2"], default="auto")
    ap.add_argument("--params", default=None, help="게이트 임계 params.yaml 경로")
    ap.add_argument("--device", default=None)
    return ap


def main(argv=None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | [ae.recalibrate] %(message)s",
    )
    args = build_argparser().parse_args(argv)
    recalibrate(
        args.base,
        args.data,
        args.out,
        refit_scorer=args.refit_scorer,
        holdout_frac=args.holdout_frac,
        calib_version=args.calib_version,
        normal_col=args.normal_col,
        normal_value=args.normal_value,
        input_schema=args.input_schema,
        params_path=args.params,
        device=args.device,
    )


if __name__ == "__main__":
    main()
