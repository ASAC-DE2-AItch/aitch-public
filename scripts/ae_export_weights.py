# -*- coding: utf-8 -*-
"""ae_export_weights.py — AE 번들의 `.pt` 가중치를 numpy `.npz` 로 1회 추출 (A 실험 도구).

**왜 (2026-08-05)**: Windows WDAC 가 `torch/lib/shm.dll` 로드를 차단해 로컬에서 AE 스코어링이
불가능해졌다 (TSR-0001 변종 — detached 가 아니라 **foreground 에서** 발생). 보안 정책 변경은
범위 밖(TSR-0001 기각 대안 ②)이므로, torch 가 살아있는 환경에서 **가중치만 한 번 꺼내**
`scripts/ae_baseline_ladder.py --engine numpy` 가 torch 없이 돌 수 있게 한다.

MLP-AE 는 Linear·GELU·LayerNorm 뿐이라 numpy 순전파로 정확히 재현된다(Dropout 은 eval 항등).
헌법 3-3 의 "pickle 버전 결박 회피 · 프레임워크 네이티브/이식 포맷 우선" 취지와도 정합이다.

**조용한 불일치 방지**: 고정 시드 입력의 **torch 기준 순전파 출력**(`x_ref`/`y_ref`)을 같이
저장한다. numpy 엔진은 로드 시 이를 대조하고 어긋나면 예외를 던진다 — 값이 다른 채로
"성공"하는 경로를 만들지 않는다 (§7 "예외가 안 났으니 성공으로 간주 금지").

⚠️ 산출물은 **7파일 세트가 아니다**(사이드카). `deploy_bundle.py` 복사 대상이 아니고
manifest sha256·게이트 A1/A2 판정에 영향을 주지 않는다 (불가침 12 무영향).

사용 (torch 가 동작하는 환경에서):
  python scripts/ae_export_weights.py --bundle models/anomaly_ae/ae_v1
  # → models/anomaly_ae/ae_v1/model_weights.npz

그 뒤 torch 가 막힌 환경에서:
  python scripts/ae_baseline_ladder.py --data ... --bundle models/anomaly_ae/ae_v1 --engine numpy
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
AE_PKG_ROOT = REPO_ROOT / "src" / "agent_a_mlops" / "Autoencoder"
sys.path.insert(0, str(AE_PKG_ROOT))

log = logging.getLogger("ae.export")

WEIGHTS_NPZ = "model_weights.npz"   # = ae_baseline_ladder.WEIGHTS_NPZ (값 규약만 공유)
REF_ROWS = 8                        # 기준 순전파 표본 수
REF_SEED = 0                        # 기준 입력 시드 (고정 — 재현성)
MODEL_FILE = "model_seed42.pt"
SPEC_FILE = "feature_spec.json"


def export(bundle_dir: Path, out_path: Path | None = None) -> Path:
    """번들 `.pt` → `model_weights.npz` (state_dict + 아키텍처 메타 + torch 기준 출력).

    Raises:
        FileNotFoundError: 번들에 모델·feature_spec 이 없을 때.
        ValueError: state_dict 키가 MLPAE 정의와 어긋날 때 (조용한 부분 추출 금지).
    """
    import torch                                  # 이 스크립트의 존재 이유 = torch 가 살아있는 곳
    from ae_pipeline.model import MLPAE, ADOPTED_CFG

    model_path = bundle_dir / MODEL_FILE
    spec_path = bundle_dir / SPEC_FILE
    for p in (model_path, spec_path):
        if not p.exists():
            raise FileNotFoundError(f"번들 파일 없음: {p}")

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    d = int(spec.get("n_features", len(spec["columns"])))
    z_dim = int(ADOPTED_CFG["z"])
    p_drop = float(ADOPTED_CFG["p"])

    state = torch.load(model_path, map_location="cpu")
    model = MLPAE(d, z_dim, p_drop)
    model.load_state_dict(state)                  # 키 불일치는 여기서 예외 (부분 로드 금지)
    model.eval()

    expected = set(model.state_dict().keys())
    got = set(state.keys())
    if expected != got:
        raise ValueError(f"state_dict 키 불일치 — 누락 {sorted(expected - got)} / "
                         f"여분 {sorted(got - expected)}")

    arrays = {k: v.detach().cpu().numpy().astype(np.float32) for k, v in state.items()}

    # 기준 순전파 — numpy 복제가 자기 자신을 검증할 근거
    rng = np.random.default_rng(REF_SEED)
    x_ref = rng.standard_normal((REF_ROWS, d)).astype(np.float32)
    with torch.no_grad():
        y_ref = model(torch.tensor(x_ref)).cpu().numpy().astype(np.float32)

    out = out_path or (bundle_dir / WEIGHTS_NPZ)
    np.savez(out, x_ref=x_ref, y_ref=y_ref,
             _meta=np.array(json.dumps(
                 {"n_features": d, "z": z_dim, "dropout": p_drop,
                  "arch": "MLP-AE D→64→32→z16→32→64→D (GELU·LayerNorm·dropout)",
                  "source_model": str(model_path), "torch": torch.__version__,
                  "note": "사이드카 — 7파일 세트 아님(배포·manifest 무영향, 불가침 12 무영향)"},
                 ensure_ascii=False)),
             **arrays)
    log.info("추출 완료: %s", out)
    log.info("  파라미터 %d개 · D=%d · z=%d · torch %s", len(arrays), d, z_dim, torch.__version__)
    log.info("  기준 출력 %d×%d 동봉 (numpy 엔진이 로드 시 대조 — 불일치 시 예외)", *y_ref.shape)
    return Path(out)


def build_argparser() -> argparse.ArgumentParser:
    """CLI 인자 정의."""
    ap = argparse.ArgumentParser(
        prog="ae_export_weights",
        description="AE 번들 .pt → numpy .npz 가중치 추출 (torch 무의존 스코어링용)")
    ap.add_argument("--bundle", required=True, help="AE 번들 폴더 (예: models/anomaly_ae/ae_v1)")
    ap.add_argument("--out", default=None, help="출력 npz 경로 (기본: <bundle>/model_weights.npz)")
    return ap


def main(argv=None) -> int:
    """진입점 — 번들에서 가중치 추출."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s │ %(levelname)-7s │ [ae.export] %(message)s")
    args = build_argparser().parse_args(argv)
    bundle = Path(args.bundle)
    if not bundle.is_absolute():
        bundle = REPO_ROOT / bundle
    out = Path(args.out) if args.out else None
    if out is not None and not out.is_absolute():
        out = REPO_ROOT / out
    export(bundle, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
