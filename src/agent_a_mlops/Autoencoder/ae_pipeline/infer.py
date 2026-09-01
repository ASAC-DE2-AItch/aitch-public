"""
infer.py — 운영 추론·스코어링 (번들 로드 → 출력 스키마) + CLI.

추론 계약 (wafer 1장):
  feats = extract_features(wafer_rows)     # Step4 정착/과도 분리, 물리신호만
  X     = scaler.transform(feats)          # 챔버 registry에서 scaler 선택
  ae_raw       = 조건화 잔차 Maha(model,X) # 단일 계약 (불가침 12)
  ae_score     = calib(ae_raw)             # [0,1]
  ae_drift_score = EWMA 스트림 (per-chamber 상태)
  ae_top_channels = 잔차 상위 k채널 (C코드)
  → fdc.prediction 발행 (AE는 직접 알람권 없음 — 헌법 1-2)

출력 스키마(§11-2 → B): ae_score · ae_raw · ae_top_channels · ae_drift_score ·
ae_model_version · ae_calib_version · transient_context.

번들에 scorer.npz가 없으면(동봉 frozen 모델 최초 사용) `build-scorer`로 원 정상
VAL에서 스코어러 통계를 1회 재구성한다(노트북과 수학 동일).
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np

from . import MODEL_VERSION, CALIB_VERSION
from .constants import EXPECT_HASH, sha1_8
from .features import prepare_frame, extract_features
from .model import load_model, build_scorer, Scorer
from .calibration import apply_calibration, load_calibration
from .drift import DriftTracker, load_drift
from . import io_bundle

log = logging.getLogger("ae.infer")   # 헌법 6-1: print() 금지 — logging 사용


class AEModel:
    """번들 로드 + wafer 배치 스코어링 + per-chamber 드리프트 상태."""

    def __init__(self, model, scaler, scorer, calib, drift_cfg,
                 feat_cols, model_version, calib_version):
        self.model = model
        self.scaler = scaler
        self.scorer = scorer
        self.calib = calib
        self.drift_cfg = drift_cfg
        self.feat_cols = list(feat_cols)
        self.model_version = model_version
        self.calib_version = calib_version
        self._trackers = {}   # chamber → DriftTracker (stateful)

    # ── 로딩 ──
    @classmethod
    def from_bundle(cls, bundle_dir, device=None) -> "AEModel":
        bundle_dir = Path(bundle_dir)
        A = io_bundle.ARTIFACT_NAMES
        spec = io_bundle.load_json(bundle_dir / A["feature_spec"])
        feat_cols = spec["columns"]
        d = int(spec.get("n_features", len(feat_cols)))
        model = load_model(bundle_dir / A["model"], d=d, device=device)

        scorer_path = bundle_dir / A["scorer"]
        if not scorer_path.exists():
            raise FileNotFoundError(
                f"scorer.npz 없음: {scorer_path}\n"
                "동봉 frozen 모델 최초 사용입니다. 먼저 스코어러 통계를 1회 재구성하세요:\n"
                "  python -m ae_pipeline.infer build-scorer "
                "--bundle <bundle> --data <merged_data_v3.parquet>")
        scorer = Scorer.load_stats(scorer_path, model, device=device)

        calib = load_calibration(bundle_dir / A["calib"])
        drift_cfg = load_drift(bundle_dir / A["drift"])
        scaler = io_bundle.load_scaler(bundle_dir / A["scaler"])

        mv, cv = MODEL_VERSION, CALIB_VERSION
        man_path = bundle_dir / A["manifest"]
        if man_path.exists():
            man = io_bundle.load_json(man_path)
            mv = man.get("ae_model_version", mv)
            cv = man.get("ae_calib_version", cv)
        return cls(model, scaler, scorer, calib, drift_cfg, feat_cols, mv, cv)

    # ── 드리프트 상태 ──
    def _tracker(self, chamber) -> DriftTracker:
        if chamber not in self._trackers:
            self._trackers[chamber] = DriftTracker(self.drift_cfg)
        return self._trackers[chamber]

    def reset_drift(self):
        self._trackers = {}

    # ── 스코어링 ──
    def _align_transform(self, F):
        return self.scaler.transform(F.reindex(columns=self.feat_cols, fill_value=0.0))

    def score_wafers(self, df_rows, chamber_col="C24", with_drift=True, k=5):
        """원본 FDC 행 → per-wafer 출력 스키마 리스트 (시간순).

        with_drift=True면 챔버별 DriftTracker 상태를 순차 갱신(스트리밍).
        """
        df, _ = prepare_frame(df_rows)
        order = df.groupby("C64")["C10"].min().sort_values(kind="mergesort").index.tolist()
        F = extract_features(df).reindex(order).fillna(0.0)
        X = self._align_transform(F)
        raw = self.scorer.ae_raw(X)
        score = apply_calibration(raw, self.calib)
        tops = self.scorer.top_channels(X, k=k)
        cham = (df.groupby("C64")[chamber_col].first().reindex(order).astype(str)
                if chamber_col in df.columns else None)
        has_tr = F["has_transient"] if "has_transient" in F.columns else None

        out = []
        for i, w in enumerate(order):
            ch = str(cham.iloc[i]) if cham is not None else "default"
            rec = {
                "wafer": str(w),
                "ae_score": round(float(score[i]), 4),
                "ae_raw": round(float(raw[i]), 4),
                "ae_top_channels": list(tops[i]),
                "ae_model_version": self.model_version,
                "ae_calib_version": self.calib_version,
                "transient_context": (bool(has_tr.iloc[i] > 0) if has_tr is not None else None),
            }
            if with_drift:
                rec["ae_drift_score"] = round(self._tracker(ch).update(score[i]), 4)
            out.append(rec)
        return out


# ── build-scorer: frozen 모델용 스코어러 재구성 ────────────────────────────
def build_scorer_cmd(bundle_dir, data_path, regime="seg1", normal_col="C6",
                     normal_value="C6_0", val_frac=0.2, seed=42, device=None,
                     force=False, verbose=True):
    """동봉 frozen 모델의 scorer.npz를 원 정상 VAL에서 재구성(수학 = 노트북).

    원 학습셋(seg1·C6_0)의 시간순 VAL 잔차로 μ·sd·조건화 정밀도 P를 산출한다.
    VAL 세트 경계 해시를 원 지문(EXPECT_HASH)과 대조하여 재현성을 확인한다.
    """
    from .retrain import load_data, select_normal_wafers, time_split
    from .constants import feat_weight_vector

    bundle_dir = Path(bundle_dir)
    A = io_bundle.ARTIFACT_NAMES
    spec = io_bundle.load_json(bundle_dir / A["feature_spec"])
    feat_cols = spec["columns"]
    d = int(spec.get("n_features", len(feat_cols)))
    model = load_model(bundle_dir / A["model"], d=d, device=device)
    scaler = io_bundle.load_scaler(bundle_dir / A["scaler"])

    df_raw = load_data(data_path)
    df, _ = prepare_frame(df_raw)
    normal_ordered = select_normal_wafers(df, regime, normal_col, normal_value)
    train_w, val_w = time_split(normal_ordered, val_frac)

    val_hash = sha1_8(val_w)
    hash_ok = (val_hash == EXPECT_HASH.get("val"))
    if verbose:
        log.info("VAL 세트 경계 해시: %s (%s)", val_hash,
                 "원 지문 일치" if hash_ok else "불일치 exp=" + str(EXPECT_HASH.get("val")))
    if not hash_ok and not force:
        raise RuntimeError(
            "VAL 해시 불일치 — 원 학습셋 재현 실패. 데이터/필터를 확인하거나 --force로 진행.")

    feats_val = extract_features(df[df["C64"].isin(set(val_w))]).reindex(val_w).fillna(0.0)
    X_val = scaler.transform(feats_val.reindex(columns=feat_cols, fill_value=0.0))
    w_vec = feat_weight_vector(feat_cols)
    scorer = build_scorer(model, X_val, feat_cols, w_vec, device=device)
    scorer.save_stats(bundle_dir / A["scorer"])
    # manifest 해시 갱신(있으면)
    if (bundle_dir / A["manifest"]).exists():
        man = io_bundle.read_manifest(bundle_dir)
        io_bundle.write_manifest(bundle_dir, {k: v for k, v in man.items()
                                              if k not in ("artifacts", "sha256")})
    if verbose:
        log.info("scorer.npz 재구성 완료 · eff_rank %.2f · cond %.1f · VAL %d장 → %s",
                 scorer.eff_rank, scorer.cond_used, len(val_w), bundle_dir / A["scorer"])
    return scorer


# ── CLI ───────────────────────────────────────────────────────────────────
def _cmd_score(args):
    from .retrain import load_data
    ae = AEModel.from_bundle(args.bundle, device=args.device)
    df = load_data(args.data)
    if args.wafers:
        df = df[df["C64"].astype(str).isin(set(args.wafers.split(",")))]
    res = ae.score_wafers(df, chamber_col=args.chamber_col, with_drift=not args.no_drift)
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("%d건 → %s", len(res), args.out)
    else:
        log.info("스코어링 결과(미리보기 %d/%d건):\n%s", min(args.head, len(res)), len(res),
                 json.dumps(res[: args.head], ensure_ascii=False, indent=2))
        if len(res) > args.head:
            log.info("... (총 %d건, --out 로 전체 저장)", len(res))


def _cmd_build_scorer(args):
    build_scorer_cmd(args.bundle, args.data, regime=args.regime,
                     normal_col=args.normal_col, normal_value=args.normal_value,
                     val_frac=args.val_frac, seed=args.seed, device=args.device,
                     force=args.force)


def _cmd_verify(args):
    result = io_bundle.verify_bundle(args.bundle)
    allok = all(v["ok"] for v in result.values())
    for name, v in result.items():
        log.info("  %s %s: %s", "OK" if v["ok"] else "★", name,
                 v["got"][:12] if v["got"] else None)
    if allok:
        log.info("번들 무결성: 일치")
    else:
        log.error("번들 무결성: 불일치 — manifest 해시와 실제 파일이 다릅니다")


def build_argparser():
    ap = argparse.ArgumentParser(prog="ae_pipeline.infer",
                                 description="ae_v3 AE — 추론·스코어러 재구성·번들 검증")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("score", help="wafer 배치 스코어링 → 출력 스키마")
    s.add_argument("--bundle", required=True)
    s.add_argument("--data", required=True, help="스코어링할 FDC 행 (parquet/csv)")
    s.add_argument("--wafers", default=None, help="쉼표구분 C64 필터 (선택)")
    s.add_argument("--chamber-col", default="C24")
    s.add_argument("--no-drift", action="store_true", help="드리프트 상태 갱신 생략")
    s.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    s.add_argument("--head", type=int, default=5, help="stdout 미리보기 건수")
    s.add_argument("--device", default=None)
    s.set_defaults(func=_cmd_score)

    b = sub.add_parser("build-scorer", help="frozen 모델 scorer.npz 재구성 (원 VAL)")
    b.add_argument("--bundle", required=True)
    b.add_argument("--data", required=True, help="merged_data_v3.parquet/.csv")
    b.add_argument("--regime", default="seg1", choices=["seg1", "seg2"])
    b.add_argument("--normal-col", default="C6")
    b.add_argument("--normal-value", default="C6_0")
    b.add_argument("--val-frac", type=float, default=0.2)
    b.add_argument("--seed", type=int, default=42)
    b.add_argument("--force", action="store_true", help="VAL 해시 불일치여도 진행")
    b.add_argument("--device", default=None)
    b.set_defaults(func=_cmd_build_scorer)

    v = sub.add_parser("verify", help="manifest 해시로 번들 무결성 검증")
    v.add_argument("--bundle", required=True)
    v.set_defaults(func=_cmd_verify)
    return ap


def main(argv=None):
    """CLI 진입점 — logging 구성 후 서브커맨드 실행 (make_drift 스타일 통일)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ [ae.infer] %(message)s")
    args = build_argparser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
