# -*- coding: utf-8 -*-
"""ct2_threshold_sweep.py — CT² 게이트·감시 임계 스윕 하네스 (WP-D / 설계 v2 §7-3 #5).

**왜 필요한가**: 게이트 임계 11종 + watch 임계 6종이 전부 원 학습(REPORT_05) 실측에서
역산한 **출발값**이지 스윕 확정값이 아니다 (설계 v2 "정직한 한계 ②"). 값의 근거가 없으면
게이트가 FAIL 했을 때 "모델이 나쁜 것"인지 "임계가 틀린 것"인지 구분할 수 없다.

스윕 축 (설계 §7-3 #5):
  · n = 학습 표본 수 (기본 500 / 1000 / 2000 / 전량) × 시드 3
  · 지표 = ⓐ **앵커 부트스트랩 CI**(calib 안정성) ⓑ **프로브 분리도**(민감도)
           ⓒ **홀드아웃 L5**(오경보 — 적합 표본이 아니라 홀드아웃에서)

⚠️ **표본 3값은 묶여 있다** (설계 D14): `min_train_wafers × val_frac × gate_holdout_frac`
   이 홀드아웃 장수를 정하고, 그 장수가 `l5_budget` 의 **측정 가능성**을 정한다. 100장이면
   오경보 1건 = 1% 라 2% 임계를 잴 수 있지만, 50장이면 1건이 곧 2% 라 임계가 비율이 아니라
   **이진 판정**이 된다. `check_sample_triplet()` 이 이 정합을 스윕 전에 검사한다.
   ★2026-08-09 (기준 개정 v2 §1-5 · PM 결정 #4): 해상도만으로는 부족하다는 것이 실측에서
   드러나 **CI 반폭** 축이 추가됐다. n=600 은 해상도 0.17%p 로 통과하지만 Wilson 반폭이
   ±1.25%p 라 예산 4% 와 원 목표 2% 를 구분하지 못한다(그래서 e4 가 `PASS-경계`).
   ⚠️ 문서 §1-5 의 "≥800장"은 반올림 표기다 — 이 함수 산식으로 실제 계산하면 **800장은
   ±1.08%p 로 목표 미달**이고, ±1%p 를 넘어서려면 **940장**(여유 잡아 1000장)이 필요하다.
   스윕 목표는 홀드아웃 총 **1000장** · 챔버당 300장(±1.82%p — 챔버 축은 여전히 경계).

정직한 한계:
  · 이 스크립트는 **드라이버**다. 실제 학습·검증은 `ae_pipeline.retrain` /
    `ae_pipeline.validate_bundle` subprocess 가 하고, 여기서는 조합을 돌리고 리포트를 모은다.
  · 조합당 CT² 1회(CPU 폴백 기준 수 분)이므로 4n × 3seed = 12회는 **수십 분~수 시간**이다.
    CI 에 넣을 것이 아니라 사람이 한 번 돌려 표를 만드는 용도다.
  · torch 가 필요한 것은 subprocess 쪽이고, 이 파일의 순수 함수(`check_sample_triplet`·
    `anchor_bootstrap_ci`·`summarize`)는 torch 없이 단위 테스트할 수 있다.

실행:
  python scripts/ct2_threshold_sweep.py --data Data/ae_repro/ae_input_*.csv \
      --labels Data/ct2/injection_labels.csv --out outputs/ct2_sweep \
      --n 500 1000 2000 --seeds 42 43 44 [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AE_CWD = REPO_ROOT / "src" / "agent_a_mlops" / "Autoencoder"
sys.path.insert(0, str(AE_CWD))          # `ae_pipeline` 순수 모듈 (torch 무의존 경로만 쓴다)
VALIDATION_REPORT = "validation.json"
BUNDLE_NAME_MAX = 32                      # 감사 컬럼 폭 (헌법 7장 — 이름 짓는 시점에 센다)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ [ct2.sweep] %(message)s")
log = logging.getLogger("ct2_sweep")


# ── 순수 함수 (torch 무의존 — 단위 테스트 대상) ────────────────────────────
def check_sample_triplet(n_train: int, val_frac: float, gate_frac: float,
                         l5_budget: float, min_gate_wafers: int,
                         p_assumed: float = 0.035, target_halfwidth: float = 0.01) -> dict:
    """표본 3값 ↔ `l5_budget` 측정 가능성 정합 검사 (설계 D14). 판정 dict 반환.

    세 가지를 본다:
      ⓐ 홀드아웃 장수가 `min_gate_wafers` 하한을 넘는가 (게이트 D1 이 FAIL 할 조합인가).
      ⓑ **해상도** — 오경보 1건의 비율(1/holdout)이 `l5_budget` 보다 작아야 임계를 '비율로'
         잴 수 있다. 같거나 크면 임계 판정이 사실상 "오경보 0건이냐 아니냐"가 된다.
      ⓒ **CI 반폭** *(2026-08-09 신설 — 기준 개정 v2 §1-5, PM 결정 #4)*. ⓑ 만으로는 부족한
         것이 실측에서 드러났다: n=600 은 해상도 0.17%p 로 ⓑ 를 여유 있게 통과하는데,
         p≈3.5% 의 Wilson 반폭이 **±1.25%p** 라 예산 4% 와 원 목표 2% 를 구분하지 못한다
         (e4 판정이 `PASS-경계` 로 남는 이유). 반폭 <1%p 를 요구하면 **940장**이 실제 하한이다
         (문서 §1-5 의 "≥800장"은 반올림 표기 — 800장은 ±1.08%p 로 이 검사를 통과하지 못한다).

    Args:
        p_assumed: 반폭 계산에 쓸 가정 오경보율 (실측 중심 ~3.5%).
        target_halfwidth: 목표 CI 반폭 (기본 1%p — 예산 4% 와 원 목표 2% 를 가르는 값).

    Returns:
        {n_holdout, n_fit, resolution, measurable, meets_floor, ci_halfwidth,
         band_resolvable, note}
    """
    from ae_pipeline.validate_bundle import wilson_interval   # 밴드 산식 단일 소스

    n_val = int(n_train * float(val_frac))
    n_hold = int(n_val * float(gate_frac))
    n_fit = n_val - n_hold
    resolution = (1.0 / n_hold) if n_hold else float("inf")
    measurable = bool(n_hold) and resolution < float(l5_budget)
    meets_floor = n_hold >= int(min_gate_wafers)
    if n_hold:
        lo, hi = wilson_interval(round(p_assumed * n_hold), n_hold)
        half = (hi - lo) / 2.0
    else:
        half = float("inf")
    band_resolvable = half < float(target_halfwidth)
    notes = []
    if not meets_floor:
        notes.append(f"홀드아웃 {n_hold} < 하한 {min_gate_wafers} (게이트 D1 FAIL)")
    if not measurable:
        notes.append("홀드아웃이 작아 l5_budget 을 비율로 잴 수 없다 (이진 판정으로 퇴화)")
    if not band_resolvable:
        notes.append(f"CI 반폭 ±{half * 100:.2f}%p ≥ 목표 ±{target_halfwidth * 100:g}%p — "
                     "경계 밴드를 못 벗어난다 (예산과 원 목표를 구분 불가)")
    return {"n_train": n_train, "n_val": n_val, "n_holdout": n_hold, "n_fit": n_fit,
            "resolution": round(resolution, 6) if n_hold else None,
            "measurable": measurable, "meets_floor": meets_floor,
            "ci_halfwidth": round(half, 6) if n_hold else None,
            "band_resolvable": band_resolvable,
            "note": "OK" if not notes else " / ".join(notes)}


def anchor_bootstrap_ci(raw_values, q=0.985, n_boot=500, seed=42, ci=0.90) -> dict:
    """calib 앵커(분위수)의 **부트스트랩 신뢰구간** — 소표본 불안정성의 정량 지표.

    `qual_threshold` 0.2 는 VAL raw 의 P98.5 로 정의된다. 표본이 작으면 그 분위수가 재추출
    마다 크게 흔들리고, 그러면 같은 모델이 표본 운에 따라 통과/탈락한다. CI 폭이 곧
    "이 n 에서 임계가 얼마나 믿을 만한가"다.

    numpy 만 쓴다(학습 무관) — 스윕 결과 CSV 를 사후 분석할 때도 재사용한다.
    """
    import numpy as np
    x = np.asarray(list(raw_values), dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 2:
        return {"n": int(x.size), "point": None, "lo": None, "hi": None, "width": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, x.size, size=(int(n_boot), x.size))
    qs = np.quantile(x[idx], q, axis=1)
    lo, hi = np.quantile(qs, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    point = float(np.quantile(x, q))
    return {"n": int(x.size), "q": q, "point": round(point, 6),
            "lo": round(float(lo), 6), "hi": round(float(hi), 6),
            "width": round(float(hi - lo), 6),
            "width_rel": round(float(hi - lo) / point, 6) if point else None}


def probe_separation(report: dict) -> dict:
    """게이트 리포트에서 프로브 분리도 요약 — 낮은 σ 와 높은 σ 의 검출률 차.

    분리도가 낮다(=낮은 σ에서도 다 잡거나, 높은 σ에서도 못 잡는다)는 것은 임계가 표본에
    비해 너무 느슨하거나 빡빡하다는 신호다. 판정이 아니라 스윕 관측치다.
    """
    by = ((report.get("probe") or {}).get("by_sigma") or {})
    if not by:
        return {"lo": None, "hi": None, "spread": None}
    keys = sorted(by, key=lambda k: float(k.replace("sigma", "")))
    lo, hi = by[keys[0]], by[keys[-1]]
    return {"lo_sigma": keys[0], "hi_sigma": keys[-1], "lo": lo, "hi": hi,
            "spread": round(float(hi) - float(lo), 4)}


def summarize(rows: list[dict]) -> str:
    """스윕 결과 → 마크다운 표 (사람이 임계를 고를 때 보는 것)."""
    if not rows:
        return "_(결과 없음)_\n"
    cols = ["n", "seed", "verdict", "holdout_l5", "l5_band", "l5_ci_lo", "l5_ci_hi",
            "recon_rmse", "val_p95", "probe_at_gate", "probe_scoring", "probe_arithmetic",
            "probe_spread", "label_detect", "n_holdout", "resolution"]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "-")) for c in cols) + " |")
    return "\n".join(out) + "\n"


# ── 드라이버 ───────────────────────────────────────────────────────────────
def _run(argv, cwd, label) -> subprocess.CompletedProcess:
    """subprocess 실행 — utf-8 고정 (Windows cp949 로 실패 사유가 뭉개지는 것 방지, 헌법 7장)."""
    log.info("  $ %s", " ".join(str(x) for x in argv))
    import os
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, env=env)
    if r.returncode not in (0,):
        log.warning("  %s rc=%s\n%s", label, r.returncode, (r.stderr or "")[-800:])
    return r


def write_wafers_file(data: Path, out: Path, n: int, labels: Path | None) -> int:
    """**앞 n장**의 정상 wafer 목록 파일을 쓴다 (시간순). 실제 기록 장수 반환.

    ★이게 없으면 `--n` 축이 **무의미**하다: `retrain` 은 `--data` 전체를 쓰므로 n 을 바꿔도
    같은 데이터로 학습하고 시드만 달라진다(그러면 스윕이 아니라 시드 반복이다). `--wafers-file`
    로 학습 표본 자체를 자른다.

    라벨을 주면 **주입 wafer 를 먼저 제외한 뒤** 앞에서 n장을 센다 — 오염분을 세어 넣으면
    "n=1000" 이 실제로는 정상 700장을 뜻하게 되어 축이 또 거짓말을 한다.
    """
    from ae_pipeline.features import prepare_frame
    from ae_pipeline.retrain import load_data, prepare_input, select_normal_wafers

    df_raw, _ = prepare_input(load_data(data), "auto")
    df, _ = prepare_frame(df_raw)
    ordered = select_normal_wafers(df, labels_path=str(labels) if labels else None)
    picked = [str(w) for w in ordered[:int(n)]]
    out.write_text("\n".join(picked) + "\n", encoding="utf-8")
    if len(picked) < n:
        log.warning("n=%d 요청했으나 정상셋이 %d장뿐 — 이 조합은 '전량' 과 같다", n, len(picked))
    return len(picked)


def sweep_one(data: Path, out_dir: Path, n: int, seed: int, *, labels: Path | None,
              val_frac: float, gate_frac: float, dry_run: bool) -> dict:
    """조합 1개 — 표본 절단 → 재학습 → 게이트 → 리포트 수집. 결과 행 dict.

    번들명은 `sweep_n<N>_s<SEED>_<stamp>` 이며 **32자 이내**로 유지한다 (헌법 7장 — 감사
    컬럼 폭. 스윕 산출물은 DB 에 들어가지 않지만 같은 규율을 쓰는 편이 실수를 줄인다).
    """
    stamp = datetime.now(timezone.utc).strftime("%m%d%H%M")
    name = f"sweep_n{n}_s{seed}_{stamp}"
    if len(name) > BUNDLE_NAME_MAX:
        name = name[:BUNDLE_NAME_MAX]
    bundle = out_dir / name
    row = {"n": n, "seed": seed, "bundle": name}

    wafers_file = out_dir / f"wafers_n{n}.txt"
    retrain = [sys.executable, "-m", "ae_pipeline.retrain",
               "--data", str(data), "--out", str(bundle), "--input-schema", "ct2",
               "--wafers-file", str(wafers_file),
               "--val-frac", str(val_frac), "--gate-holdout-frac", str(gate_frac),
               "--seed", str(seed), "--model-version", f"sweep_n{n}",
               "--calib-version", "calib_sweep", "--allow-thin-split"]
    if labels:
        retrain += ["--labels", str(labels)]
    validate = [sys.executable, "-m", "ae_pipeline.validate_bundle",
                "--bundle", str(bundle), "--data", str(data), "--input-schema", "ct2",
                "--out", str(bundle / VALIDATION_REPORT)]
    if labels:
        validate += ["--labels", str(labels)]

    if dry_run:
        row["verdict"] = "DRYRUN"
        row["cmd_retrain"] = " ".join(retrain)
        row["cmd_validate"] = " ".join(validate)
        return row

    try:
        row["n_actual"] = write_wafers_file(data, wafers_file, n, labels)
    except Exception as e:                                   # noqa: BLE001
        row["verdict"] = "SETUP_FAIL"
        row["detail"] = f"표본 목록 생성 실패: {e}"
        return row

    r = _run(retrain, AE_CWD, "재학습")
    if r.returncode != 0:
        row["verdict"] = "RETRAIN_FAIL"
        row["detail"] = (r.stderr or "")[-300:]
        return row
    _run(validate, AE_CWD, "게이트")                 # exit 2/3 은 정상 판정이라 rc 로 죽이지 않는다

    rep_p = bundle / VALIDATION_REPORT
    if not rep_p.exists():
        row["verdict"] = "NO_REPORT"
        return row
    rep = json.loads(rep_p.read_text(encoding="utf-8"))
    m = rep.get("metrics") or {}
    ld = rep.get("label_detection") or {}
    ps = probe_separation(rep)
    row.update({
        "verdict": rep.get("gate_verdict"),
        "failed": ",".join(rep.get("failed_checks") or []),
        "skipped": ",".join(rep.get("skipped_checks") or []),
        "holdout_l5": m.get("l5"), "recon_rmse": m.get("recon_rmse"),
        "val_mean": m.get("val_mean"), "val_p95": m.get("val_p95"),
        "b_group_sample": m.get("b_group_sample"),
        "n_holdout": (rep.get("set_sizes") or {}).get("gate_holdout"),
        "n_val": (rep.get("set_sizes") or {}).get("val"),
        # ★`probe_at_gate` 는 **판정에 실제로 쓴 값**이어야 한다 (2026-08-09 개정 v2 §3-2).
        #   `by_sigma` 는 이제 병기용 **산술**값이다 — 그것으로 표를 만들면 산술 분포를 보고
        #   `probe_min_detect` 를 재산정해 **가중 판정에 먹이게** 된다. 예외도 경고도 없고
        #   값이 0.8983 처럼 그럴듯해 눈으로도 안 걸린다 (헌법 7장 "신설하고 하류를 안 따라감").
        #   구 리포트(schema ≤2)에는 `at_gate` 가 없으므로 산술로 폴백하고 `scoring` 으로 구분한다.
        "probe_at_gate": probe_at_gate(rep),
        "probe_scoring": (rep.get("probe") or {}).get("scoring", "arithmetic(구 리포트)"),
        "probe_arithmetic": (rep.get("probe") or {}).get("at_gate_arithmetic"),
        "probe_weighted": (rep.get("probe") or {}).get("at_gate_weighted"),
        "probe_leniency_gap": (rep.get("probe") or {}).get("leniency_gap"),
        "probe_spread": ps.get("spread"),
        "label_detect": ld.get("detect_rate"),
        # 3밴드 (개정 v2 §1-3) — 목표 CI 반폭과 **실측 반폭**을 같은 표에서 대조하려면 필요하다.
        "l5_band": _b1_field(rep, "band"), "l5_ci_lo": _b1_field(rep, "ci_lo"),
        "l5_ci_hi": _b1_field(rep, "ci_hi"),
        "by_chamber": json.dumps(m.get("by_chamber") or {}, ensure_ascii=False),
    })
    return row


def probe_at_gate(report: dict):
    """게이트 리포트 → C1 **판정값**. schema v3 는 `probe.at_gate`, 구 리포트는 산술 폴백."""
    pr = report.get("probe") or {}
    if pr.get("at_gate") is not None:
        return pr["at_gate"]
    sigma = float((report.get("gate_params") or {}).get("probe_sigma", 4.0))
    return (pr.get("by_sigma") or {}).get(f"{sigma:g}sigma")


def _b1_field(report: dict, key: str):
    """B1 검사 레코드의 밴드 필드 (구 리포트면 None)."""
    for c in report.get("checks") or []:
        if c.get("check", "").startswith("B1_") and isinstance(c.get("band"), dict):
            return c["band"].get(key)
    return None


def build_argparser():
    """CLI 파서."""
    ap = argparse.ArgumentParser(prog="ct2_threshold_sweep",
                                 description="CT² 게이트 임계 스윕 하네스 (WP-D)")
    ap.add_argument("--data", required=True, help="학습 CSV (CT² 조립 산출물 또는 재현 덤프)")
    ap.add_argument("--labels", default=None, help="주입 정답지 라벨 CSV (E군 활성화)")
    ap.add_argument("--out", default="outputs/ct2_sweep", help="산출 폴더")
    ap.add_argument("--n", type=int, nargs="+", default=[500, 1000, 2000],
                    help="학습 표본 수 축 — 조합마다 `--wafers-file` 로 앞 n장(정상·시간순)만 쓴다")
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44])
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--gate-frac", type=float, default=0.5)
    ap.add_argument("--l5-budget", "--l5-max", dest="l5_budget", type=float, default=0.02,
                    help="정합 검사용 현행 예산 (구 --l5-max 는 별칭 — 2026-08-09 개명)")
    ap.add_argument("--min-gate-wafers", type=int, default=100)
    # 기준 개정 v2 §1-5 (PM 결정 #4) — 홀드아웃 하한 800(총)·챔버 300 목표의 근거가 되는 축.
    ap.add_argument("--assumed-p", type=float, default=0.035,
                    help="CI 반폭 계산용 가정 오경보율 (실측 중심 ~3.5%%)")
    ap.add_argument("--target-halfwidth", type=float, default=0.01,
                    help="목표 CI 반폭 — 예산 4%% 와 원 목표 2%% 를 가르려면 1%%p (v2 §1-5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="명령만 출력 (조합 수·표본 정합만 확인. 학습 미실행)")
    return ap


def main(argv=None) -> int:
    """스윕 실행 — 표본 정합 검사 → 조합 반복 → CSV·마크다운 요약."""
    a = build_argparser().parse_args(argv)
    data = Path(a.data)
    if not data.exists():
        log.error("데이터 없음: %s", data)
        return 1
    out_dir = REPO_ROOT / a.out if not Path(a.out).is_absolute() else Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = Path(a.labels) if a.labels else None
    if labels and not labels.exists():
        log.error("라벨 없음: %s — E군 없이 도는 스윕은 오염을 못 본다 (K-1)", labels)
        return 1

    # ★스윕 **전에** 표본 3값 정합을 본다 — 측정 불가능한 조합을 수 시간 돌린 뒤에
    #   "임계가 이상하다"고 결론 내는 것을 막는다 (설계 D14).
    log.info("표본 정합 검사 (l5_budget=%s · min_gate_wafers=%s · 목표 CI 반폭 ±%.2f%%p @ p=%.3f)",
             a.l5_budget, a.min_gate_wafers, a.target_halfwidth * 100, a.assumed_p)
    triplets = []
    for n in a.n:
        t = check_sample_triplet(n, a.val_frac, a.gate_frac, a.l5_budget, a.min_gate_wafers,
                                 p_assumed=a.assumed_p, target_halfwidth=a.target_halfwidth)
        triplets.append(t)
        ok = t["measurable"] and t["meets_floor"] and t["band_resolvable"]
        log.info("  n=%-5d → fit %-4d / holdout %-4d · 해상도 %s · CI 반폭 ±%s · %s",
                 n, t["n_fit"], t["n_holdout"], t["resolution"],
                 f"{t['ci_halfwidth'] * 100:.2f}%p" if t["ci_halfwidth"] is not None else "—",
                 "OK" if ok else "★" + t["note"])

    rows = []
    for n in a.n:
        for seed in a.seeds:
            log.info("── 조합 n=%s seed=%s ──", n, seed)
            rows.append(sweep_one(data, out_dir, n, seed, labels=labels,
                                  val_frac=a.val_frac, gate_frac=a.gate_frac,
                                  dry_run=a.dry_run))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_p = out_dir / f"sweep_{stamp}.csv"
    keys = sorted({k for r in rows for k in r})
    with open(csv_p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    md_p = out_dir / f"sweep_{stamp}.md"
    md_p.write_text(
        f"# CT² 임계 스윕 결과 ({stamp} UTC)\n\n"
        f"- 데이터: `{data}`\n- 라벨: `{labels or '없음 (E군 비활성 — K-1)'}`\n"
        f"- 축: n={a.n} × seed={a.seeds} · val_frac={a.val_frac} · gate_frac={a.gate_frac}\n\n"
        "## 표본 3값 정합 (설계 D14)\n\n"
        + "\n".join(f"- n={t['n_train']}: fit {t['n_fit']} / holdout {t['n_holdout']} · "
                    f"해상도 {t['resolution']} · {t['note']}" for t in triplets)
        + "\n\n## 조합별 결과\n\n" + summarize(rows)
        + "\n> 임계 확정은 이 표 + 앵커 부트스트랩 CI 를 근거로 PM 합의(합의안 16차)에 올린다.\n",
        encoding="utf-8")
    log.info("완료 — %s · %s", csv_p, md_p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
