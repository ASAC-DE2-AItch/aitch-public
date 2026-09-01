# -*- coding: utf-8 -*-
"""SCRAP arm 모델 감도 프로브 — "어떤 센서를 몇 σ 튀기면 predicted_c65가 1572를 넘나".

실서비스 정합 원칙(결정안 §5 후속): 주입은 원인(센서)까지만, predicted_c65는 모델이
산출한다. 이 프로브는 실 wafer 코호트의 센서를 +kσ 부스트한 변형본을 lean-85 모델에
넣어 예측 반응을 직접 측정한다 — crazy 시나리오의 (sensor, magnitude_sigma) 설계 근거.

사용 (사용자 머신, 저장소 루트에서 — venv에 xgboost 필요 = A 파이프라인 구동 환경):
    python scripts/probe_scrap_arm.py
    python scripts/probe_scrap_arm.py --sensors C31,C11 --sigmas 6,10
출력: probe_scrap_report.txt (루트) — 경로를 PM/Claude에게 전달하면 판정.

주의:
  · 부스트는 injector 산식과 동일 (delta = k × 전체 CSV 컬럼 std, wafer 전 행 적용 = spike).
  · 변형은 코호트 내 서로 다른 wafer에 1건씩 배정 — 파이프라인 2회 실행(기준/변형)으로 끝.
  · wafer 간 rolling류 피처가 있으면 이웃 오염 가능 — 프로브 목적(감도 순위·근사 임계)에는 무해.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_CSV = ROOT / "Data" / "문제1(하)" / "train_data.csv"
LEAN85_DIR = ROOT / "src" / "agent_a_mlops" / "lean85"
THRESHOLD = 1572.0          # B9 predicted_c65 임계 (train P99 — 합의안)


def latest_model() -> Path:
    """params 4-1 글롭으로 최신 lean85 모델 폴더 (mtime 최신)."""
    hits = glob.glob(str(ROOT / "models" / "c65_predictor" / "*" / "lean85_*"))
    if not hits:
        sys.exit("모델 폴더 없음: models/c65_predictor/*/lean85_*")
    return Path(max(hits, key=os.path.getmtime))


def run_predict(model: Path, data_csv: Path, out_csv: Path) -> pd.DataFrame:
    """predict_lean85 CLI 서브프로세스 (cwd=lean85 — 평면 import 대응)."""
    pm_log = ROOT / "pm_log.json"
    cmd = [sys.executable, "predict_lean85.py",
           "--model", str(model), "--data", str(data_csv), "--out", str(out_csv)]
    if pm_log.exists():
        cmd += ["--pm-log", str(pm_log)]
    r = subprocess.run(cmd, cwd=str(LEAN85_DIR), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"predict 실패 (rc={r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return pd.read_csv(out_csv)


def main() -> None:
    ap = argparse.ArgumentParser(description="SCRAP arm 모델 감도 프로브")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--model", type=Path, default=None)
    ap.add_argument("--wafers", type=int, default=40, help="코호트 크기 (말미 정착 구간)")
    ap.add_argument("--sensors", default="C31,C11,C17,C62,C4")
    ap.add_argument("--sigmas", default="4,6,8,10")
    ap.add_argument("--out", type=Path, default=ROOT / "probe_scrap_report.txt")
    a = ap.parse_args()

    sensors = [s.strip() for s in a.sensors.split(",") if s.strip()]
    sigmas = [float(k) for k in a.sigmas.split(",")]
    model = a.model or latest_model()

    print(f"[1/5] CSV 로드: {a.csv}")
    df_full = pd.read_csv(a.csv)
    try:                                     # C17 seg1 필터 (있으면 — 시뮬 replay와 동일 소스)
        from simulator.message_builder import filter_post_reset
        df = filter_post_reset(df_full)
        seg_note = f"seg1 필터 적용 ({df['C64'].nunique():,}/{df_full['C64'].nunique():,} wafer)"
    except Exception:                        # noqa: BLE001 — 미병합 환경 폴백
        df = df_full
        seg_note = "seg1 필터 없음 (전체 사용)"
    print(f"       {seg_note}")

    std = {s: float(df_full[s].std()) for s in sensors if s in df_full.columns}
    missing = [s for s in sensors if s not in std]
    if missing:
        print(f"       ⚠ 컬럼 없음 제외: {missing}")
        sensors = [s for s in sensors if s in std]

    # 말미 코호트 — wafer 등장 순서 보존
    order = df["C64"].drop_duplicates().tolist()
    cohort_ids = order[-a.wafers:]
    cohort = df[df["C64"].isin(cohort_ids)].copy()
    if "C65" in cohort.columns:
        cohort = cohort.drop(columns=["C65"])          # 서빙 입력 정합 (헌법 1-3)

    combos = [(s, k) for s in sensors for k in sigmas]
    slots = cohort_ids[2:-2]                           # 양끝 제외 후 배정
    if len(combos) > len(slots):
        sys.exit(f"조합 {len(combos)} > 배정 가능 wafer {len(slots)} — --wafers 증가 필요")
    assign = {combos[i]: slots[i] for i in range(len(combos))}

    tmp = Path(tempfile.mkdtemp(prefix="probe_"))
    base_csv, var_csv = tmp / "base.csv", tmp / "variant.csv"
    cohort.to_csv(base_csv, index=False)

    variant = cohort.copy()
    for (s, k), wid in assign.items():
        mask = variant["C64"] == wid
        variant.loc[mask, s] = variant.loc[mask, s] + k * std[s]
    variant.to_csv(var_csv, index=False)

    print(f"[2/5] 모델: {model.name}")
    print(f"[3/5] 기준 예측 ({len(cohort_ids)} wafer)...")
    base_pred = run_predict(model, base_csv, tmp / "base_out.csv")
    print(f"[4/5] 변형 예측 ({len(combos)} 조합)...")
    var_pred = run_predict(model, var_csv, tmp / "var_out.csv")

    b = dict(zip(base_pred["C64"].astype(str), base_pred["pred_C65"]))
    v = dict(zip(var_pred["C64"].astype(str), var_pred["pred_C65"]))

    rows = []
    for (s, k), wid in assign.items():
        w = str(wid)
        if w in b and w in v:
            rows.append({"sensor": s, "k_sigma": k, "wafer": w,
                         "pred_base": round(b[w], 1), "pred_boost": round(v[w], 1),
                         "delta": round(v[w] - b[w], 1),
                         "crossed_1572": bool(v[w] > THRESHOLD)})
    res = pd.DataFrame(rows).sort_values(["crossed_1572", "delta"], ascending=[False, False])

    lines = ["# SCRAP arm 모델 감도 프로브 결과", f"# 모델: {model.name} · 코호트 {len(cohort_ids)} wafer · {seg_note}",
             f"# 임계: predicted_c65 > {THRESHOLD:.0f} (B9)", ""]
    lines.append(res.to_string(index=False))
    lines.append("")
    crossed = res[res["crossed_1572"]]
    if len(crossed):
        best = crossed.sort_values("k_sigma").iloc[0]
        lines += ["## 판정: 돌파 조합 존재",
                  f"최소 σ 돌파: {best.sensor} +{best.k_sigma}σ → {best.pred_boost} (Δ{best.delta:+})",
                  "",
                  "## crazy 시나리오 제안 (이 값으로 yaml 작성)",
                  f"  sensor: {best.sensor}",
                  f"  magnitude_sigma: {best.k_sigma}",
                  "  pattern: spike  ·  target: 조용 챔버 1대  ·  ground_truth: crazy_spot/SCRAP"]
    else:
        mx = res.iloc[0] if len(res) else None
        lines += ["## 판정: 전 조합 미돌파 — 모델이 단발 spot에 둔감",
                  (f"최대 반응: {mx.sensor} +{mx.k_sigma}σ → Δ{mx.delta:+} (pred {mx.pred_boost})" if mx is not None else ""),
                  "→ M3는 B안(HOLD까지 시연 + 이중 확인 설계 설명)으로. A(팀원 A)에게 spot 감도 과제 전달."]
    a.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[5/5] 저장: {a.out}")
    print("\n".join(lines[-6:]))


if __name__ == "__main__":
    main()
