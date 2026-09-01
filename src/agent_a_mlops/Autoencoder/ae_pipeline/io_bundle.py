"""
io_bundle.py — 아티팩트 번들 저장/로드 + 무결성 해시 + manifest.

배포 세트(한 몸 · 버전 동기):
  model_seed42.pt   MLP-AE state_dict (~71KB)
  scaler.pkl        QuantileTransformer(normal), fit=TRAIN
  scorer.npz        스코어러 통계 μ·sd·P(조건화 정밀도)·W_VEC  ← 핸드오프 확장
  calib.json        ae_raw → ae_score 앵커 (0.2@P98.5)
  drift.json        EWMA α·alarm·baseline_B0
  feature_spec.json 입력 83피처 명세
  manifest.json     버전·시드·config·해시·세트크기·타임스탬프

재학습(CT②)은 이 세트를 통째로 재산출하고 버전을 동기 태깅한다(불가침 12:
버전 불일치 금지 — model/scaler/scorer/calib/drift는 항상 같은 학습에서 나온다).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ARTIFACT_NAMES = {
    "model": "model_seed42.pt",
    "scaler": "scaler.pkl",
    "scorer": "scorer.npz",
    "calib": "calib.json",
    "drift": "drift.json",
    "feature_spec": "feature_spec.json",
    "manifest": "manifest.json",
}


def save_scaler(scaler, path) -> None:
    """QuantileTransformer 저장 (joblib 우선, 없으면 pickle)."""
    path = str(path)
    try:
        import joblib
        joblib.dump(scaler, path)
    except Exception:
        import pickle
        with open(path, "wb") as f:
            pickle.dump(scaler, f)


def load_scaler(path):
    """scaler.pkl 로드 (joblib 우선, 없으면 pickle)."""
    path = str(path)
    try:
        import joblib
        return joblib.load(path)
    except Exception:
        import pickle
        with open(path, "rb") as f:
            return pickle.load(f)


def save_json(obj: dict, path) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def bundle_hashes(bundle_dir) -> dict:
    """번들 내 존재하는 아티팩트별 sha256 (무결성 지문)."""
    bundle_dir = Path(bundle_dir)
    out = {}
    for key, name in ARTIFACT_NAMES.items():
        if key == "manifest":
            continue
        p = bundle_dir / name
        if p.exists():
            out[name] = sha256_file(p)
    return out


def write_manifest(bundle_dir, meta: dict) -> dict:
    """manifest.json 작성 (meta + 아티팩트 해시). 작성한 manifest 딕셔너리 반환."""
    bundle_dir = Path(bundle_dir)
    manifest = dict(meta)
    manifest["artifacts"] = ARTIFACT_NAMES
    manifest["sha256"] = bundle_hashes(bundle_dir)
    save_json(manifest, bundle_dir / ARTIFACT_NAMES["manifest"])
    return manifest


def read_manifest(bundle_dir) -> dict:
    return load_json(Path(bundle_dir) / ARTIFACT_NAMES["manifest"])


def verify_bundle(bundle_dir) -> dict:
    """manifest 해시와 실제 파일 해시 대조. 각 파일 (ok, expected, got)."""
    bundle_dir = Path(bundle_dir)
    man = read_manifest(bundle_dir)
    expected = man.get("sha256", {})
    got = bundle_hashes(bundle_dir)
    result = {}
    for name, exp in expected.items():
        g = got.get(name)
        result[name] = {"ok": g == exp, "expected": exp, "got": g}
    return result
