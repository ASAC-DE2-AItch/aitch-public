"""
make_drift.py — 동봉 frozen 번들의 drift.json · manifest.json 1회 생성 유틸.

배경 (AE_실연동_구현계획_v1 §P0-3·P0-4 · §5-1):
  `AEModel.from_bundle()`은 scorer.npz·drift.json을 무조건 로드하고 manifest.json은
  있으면 버전 소스로 읽는다. 그런데 동봉 frozen 번들은 scorer.npz만 `infer build-scorer`
  로 재구성되고 **drift.json·manifest.json은 생성 경로가 없다** — build-scorer는 scorer만
  만들고(manifest는 "있을 때만" 갱신), drift.json은 retrain(CT②) 경로에서만 나온다.
  이 스크립트가 그 공백을 메운다 (재학습 전까지 재사용).

절차 (retrain.run_ct2 ⑦단계와 수학 동일 — 단, calib은 재적합하지 않고 동봉 calib_v1 사용):
  ① 원 정상셋(seg1·C6_0) 시간순 VAL을 build-scorer와 동일 경계로 재현 (VAL 해시 대조)
  ② 동봉 scorer.npz로  raw = scorer.ae_raw(X_val)
  ③ 동봉 calib.json으로  sc_val = apply_calibration(raw)   ← 프리즈된 캘리 그대로
  ④ drift.fit_drift_baseline(sc_val) → drift.json 저장  (baseline_B0 ≈ 0.130 확인)
  ⑤ scorer·drift 확정 후 **마지막에** manifest.json 작성 (해시가 최종 세트를 지문화)

이 유틸은 재학습이 아니다 — model/scaler/scorer/calib은 동봉 frozen을 그대로 두고
drift만 새로 산출한다(불가침 12: 세트 버전 동기 유지). 재학습은 `ae_pipeline.retrain`.

CLI:
  python -m ae_pipeline.make_drift --bundle ./artifacts --data ./merged_data_v3.parquet
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import numpy as np

from . import MODEL_VERSION, CALIB_VERSION
from .constants import EXPECT_HASH, SEED, seed_all, sha1_8
from .features import prepare_frame, extract_features
from .model import load_model, Scorer
from .calibration import apply_calibration, load_calibration
from .drift import fit_drift_baseline, save_drift
from . import io_bundle
from .retrain import load_data, select_normal_wafers, time_split

log = logging.getLogger("ae.make_drift")

# 완료 기준 대조값 (ae_config.yaml `drift.baseline_B0` 기록값) — 계획 P0-3.
REF_BASELINE_B0 = 0.130
B0_MATCH_TOL = 0.001   # "소수 셋째 자리 이내" 일치 판정
B0_WARN_TOL = 0.010    # 이 범위 밖이면 데이터 버전/필터부터 확인


def make_drift(bundle_dir, data_path, regime="seg1", normal_col="C6",
               normal_value="C6_0", val_frac=0.2, seed=SEED, device=None,
               model_version=None, calib_version=None, force=False,
               verbose=True) -> dict:
    """동봉 번들에서 drift.json·manifest.json을 1회 생성한다.

    build-scorer와 동일한 VAL 경계를 재현해 스코어링 → 동봉 calib로 sc_val →
    fit_drift_baseline → drift.json. 이어서 manifest.json을 마지막에 작성한다
    (scorer·drift 포함 최종 세트를 sha256으로 지문화 — from_bundle/verify 호환).

    Returns:
        drift_cfg 딕셔너리 (drift.json에 저장된 내용).
    """
    bundle_dir = Path(bundle_dir)
    A = io_bundle.ARTIFACT_NAMES
    seed_all(seed)

    # 선행 조건: scorer.npz 존재 (build-scorer 완료 — 계획 P0-2).
    scorer_path = bundle_dir / A["scorer"]
    if not scorer_path.exists():
        raise FileNotFoundError(
            f"scorer.npz 없음: {scorer_path}\n"
            "먼저 스코어러를 재구성하세요 (계획 P0-2):\n"
            f"  python -m ae_pipeline.infer build-scorer --bundle {bundle_dir} "
            f"--data {data_path}")

    # 번들 조각 직접 로드 (from_bundle은 drift.json을 요구하므로 우회).
    spec = io_bundle.load_json(bundle_dir / A["feature_spec"])
    feat_cols = spec["columns"]
    d = int(spec.get("n_features", len(feat_cols)))
    model = load_model(bundle_dir / A["model"], d=d, device=device)
    scaler = io_bundle.load_scaler(bundle_dir / A["scaler"])
    scorer = Scorer.load_stats(scorer_path, model, device=device)
    calib = load_calibration(bundle_dir / A["calib"])

    # ① 원 정상셋 시간순 VAL 재현 (build-scorer와 동일 경계) + 해시 대조.
    df_raw = load_data(data_path)
    df, n_dedup = prepare_frame(df_raw)
    normal_ordered = select_normal_wafers(df, regime, normal_col, normal_value)
    _, val_w = time_split(normal_ordered, val_frac)
    if len(val_w) < 20:
        raise ValueError(
            f"VAL 정상셋이 너무 작음(n={len(val_w)}). 데이터/필터를 확인하세요.")

    val_hash = sha1_8(val_w)
    hash_ok = (val_hash == EXPECT_HASH.get("val"))
    if verbose:
        log.info("VAL 세트 경계 해시: %s (%s)", val_hash,
                 "OK 원 지문 일치" if hash_ok
                 else "불일치 exp=" + str(EXPECT_HASH.get("val")))
    if not hash_ok and not force:
        raise RuntimeError(
            "VAL 해시 불일치 — 원 학습셋(seg1·C6_0) 재현 실패. "
            "데이터 버전/필터를 확인하거나 --force 로 진행하세요. "
            "(drift baseline은 원 VAL 분포에서 산출돼야 의미가 맞습니다.)")

    # ② 시간순 VAL 스코어링 → ③ 동봉 calib 적용 (재적합 금지 — frozen calib_v1).
    feats_val = (extract_features(df[df["C64"].isin(set(val_w))])
                 .reindex(val_w).fillna(0.0))
    X_val = scaler.transform(feats_val.reindex(columns=feat_cols, fill_value=0.0))
    raw_val = scorer.ae_raw(X_val)
    sc_val = apply_calibration(raw_val, calib)

    # ④ drift baseline 적합 → drift.json 저장.
    drift_cfg = fit_drift_baseline(sc_val)
    save_drift(drift_cfg, bundle_dir / A["drift"])
    b0 = float(drift_cfg["baseline_B0"])
    diff = abs(b0 - REF_BASELINE_B0)
    verdict = ("일치" if diff <= B0_MATCH_TOL
               else "근사(확인 요망)" if diff <= B0_WARN_TOL
               else "불일치(데이터 확인)")
    if verbose:
        log.info("drift.json 저장 · baseline_B0=%.4f (ref %.3f · Δ=%.4f · %s) → %s",
                 b0, REF_BASELINE_B0, diff, verdict, bundle_dir / A["drift"])
        log.info("  VAL n=%d · sc_val mean=%.4f p95=%.4f (정상 대부분 0 근처면 정상)",
                 len(val_w), float(sc_val.mean()), float(np.percentile(sc_val, 95)))
    if diff > B0_WARN_TOL and not force:
        raise RuntimeError(
            f"baseline_B0={b0:.4f} 이(가) 기록값 {REF_BASELINE_B0} 에서 크게 벗어남. "
            "scorer.npz/데이터 버전을 먼저 확인하세요 (--force 로 무시 가능).")

    # ⑤ manifest.json — 마지막에 작성 (해시가 scorer·drift 포함 최종 세트를 지문화).
    #    ae_model_version·ae_calib_version 은 from_bundle()이 읽어 fdc.prediction
    #    2단계 필드(ae_model_version/ae_calib_version)의 소스가 된다.
    meta = {
        "ae_model_version": model_version or MODEL_VERSION,
        "ae_calib_version": calib_version or CALIB_VERSION,
        "score_contract": "conditioned residual Mahalanobis (단일 ae_raw, 불가침 12)",
        "n_features": len(feat_cols),
        "drift_baseline_B0": round(b0, 4),
        "val_set_hash": val_hash,
        "val_size": len(val_w),
        "dedup_removed": int(n_dedup),
        "source": "make_drift.py (frozen 번들 drift/manifest 보완 — 계획 §P0-3·P0-4)",
        "note": ("재학습(CT②) 아님 · model/scaler/scorer/calib 은 동봉 frozen 유지, "
                 "drift 만 신규 산출 (불가침 12 세트 버전 동기)."),
    }
    manifest = io_bundle.write_manifest(bundle_dir, meta)
    if verbose:
        log.info("manifest.json 작성 · 아티팩트 %d개 해시 지문화 → %s",
                 len(manifest.get("sha256", {})), bundle_dir / A["manifest"])
        log.info("다음: python -m ae_pipeline.infer verify --bundle %s", bundle_dir)
    return drift_cfg


# ── CLI ───────────────────────────────────────────────────────────────────
def build_argparser() -> argparse.ArgumentParser:
    """make_drift CLI 파서 (infer/retrain 스타일 통일)."""
    ap = argparse.ArgumentParser(
        prog="ae_pipeline.make_drift",
        description="동봉 frozen 번들의 drift.json·manifest.json 1회 생성 "
                    "(계획 §P0-3·P0-4).")
    ap.add_argument("--bundle", required=True, help="번들 폴더 (예: ./artifacts)")
    ap.add_argument("--data", required=True,
                    help="원 학습셋 merged_data_v3.parquet/.csv (VAL 재현용)")
    ap.add_argument("--regime", default="seg1", choices=["seg1", "seg2"])
    ap.add_argument("--normal-col", default="C6")
    ap.add_argument("--normal-value", default="C6_0")
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--model-version", default=None,
                    help=f"manifest 기록 모델 버전 (기본 {MODEL_VERSION})")
    ap.add_argument("--calib-version", default=None,
                    help=f"manifest 기록 캘리 버전 (기본 {CALIB_VERSION})")
    ap.add_argument("--force", action="store_true",
                    help="VAL 해시 불일치·B0 이탈에도 진행")
    ap.add_argument("--device", default=None, help="cuda/cpu (기본 자동)")
    return ap


def main(argv=None) -> None:
    """CLI 진입점 — logging 구성 후 make_drift 실행."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ [make_drift] %(message)s")
    args = build_argparser().parse_args(argv)
    make_drift(
        bundle_dir=args.bundle, data_path=args.data,
        regime=args.regime, normal_col=args.normal_col,
        normal_value=args.normal_value, val_frac=args.val_frac,
        seed=args.seed, device=args.device,
        model_version=args.model_version, calib_version=args.calib_version,
        force=args.force,
    )


if __name__ == "__main__":
    main()
