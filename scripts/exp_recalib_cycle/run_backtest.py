# -*- coding: utf-8 -*-
"""run_backtest.py — 2단계: rolling-origin 백테스트 (설계서 §4).

프로토콜 (설계서 §4 그대로):
    초기 학습:  train(wf_day <= 2019-02-08)               → M0 (3군 공통)
    평가 루프:  for d in 2019-02-09 .. 2019-11-30:
        ARM-D1:     M_d = retrain(wf_day <= d-1)          # 매일
        ARM-D7:     if (d - 시작일) % 7 == 0: M = retrain(wf_day <= d-1)
        ARM-STATIC: M = M0

★ 계산 최적화 (결과 등가 — 근거):
    세 군 모두 "평가일 d 의 모델 = 컷오프(d-1) 학습본" 이라는 **같은 규칙**을 쓰고,
    다른 것은 *어느 d 에서 교체하느냐* 뿐이다. 그러므로
      · ARM-D7 이 쓰는 컷오프 43개 ⊂ ARM-D1 컷오프 295개
      · ARM-STATIC 의 M0 = 컷오프 2019-02-08 = D1 첫 컷오프
    → **컷오프 295개만 학습하면 3군을 모두 재현**한다 (서로 다른 fit = 295개, 명목 338회).
    군 배정(arm_assignment.csv)은 학습과 분리된 사후 선택이므로 등가성이 보존된다.

누수 차단 (헌법 1-3 / 설계서 §5):
    - 학습셋 = (컷오프-365일, 컷오프-buffer] 구간의 **완결 웨이퍼**만
    - 평가 웨이퍼는 학습셋과 wafer(C64) 단위 배타 — 매 컷오프 교집합 검사
    - 학습셋 최대 시각 > 컷오프 종료 시 즉시 실패
    - 모든 가드는 명시적 raise (헌법 7장 — `python -O` 로 사라지지 않게)

산출: run_<tag>/ predictions.parquet · probe_preds.parquet · fits.csv ·
      arm_assignment.csv · run_manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import CONFIG as C

sys.path.insert(0, str(C.LEAN85_DIR))
import lean85_pipeline as lp  # noqa: E402
import xgboost as xgb  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("backtest")

N_PROBE = 500          # §6 M4 churn 고정 프로브 웨이퍼 수


def load_table() -> pd.DataFrame:
    """prep_data.py 산출 웨이퍼 테이블 로드 (3군 공유 단일 소스)."""
    if not C.TABLE_PARQUET.exists():
        raise FileNotFoundError(f"{C.TABLE_PARQUET} 없음 — prep_data.py 를 먼저 실행하세요")
    tbl = pd.read_parquet(C.TABLE_PARQUET)
    tbl["wf_day"] = pd.to_datetime(tbl["wf_day"])
    tbl["wf_ts"] = pd.to_datetime(tbl["wf_ts"])
    if tbl[lp.TARGET_COL].isna().any():
        raise ValueError("타깃(C65) 결측 웨이퍼 존재 — 백테스트 전에 정리 필요")
    return tbl.sort_values("wf_ts").reset_index(drop=True)


def eval_days(tbl: pd.DataFrame) -> pd.DatetimeIndex:
    """평가 대상 = 평가창 내 **웨이퍼가 존재하는 날**만 (빈 날은 예측 대상 자체가 없다)."""
    m = (tbl["wf_day"] >= C.EVAL_START) & (tbl["wf_day"] <= C.EVAL_END)
    return pd.DatetimeIndex(sorted(tbl.loc[m, "wf_day"].unique()))


def d7_retrain_days(days: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """ARM-D7 재학습일 = 평가 시작일 기준 7일 그리드 (설계서 §3·§4).

    그리드 날짜에 생산이 없으면 **다음 생산일로 이월**한다. 재학습은 달력 사건이지만
    예측·평가는 웨이퍼가 있는 날에만 일어나므로, 이월해야 '7일마다 교체'의 의미가 보존된다.
    """
    if len(days) == 0:
        raise ValueError("평가 대상일 0개 — 기간 설정 확인")
    start = days[0]
    out: list[pd.Timestamp] = []
    nxt = start
    for d in days:
        if d >= nxt:
            out.append(pd.Timestamp(d))
            k = int(np.floor((d - start).days / C.D7_PERIOD_DAYS)) + 1
            nxt = start + pd.Timedelta(days=k * C.D7_PERIOD_DAYS)
    return out


def gate_decision(rmse_champ: float, rmse_chal: float, min_gain_pct: float) -> tuple[bool, float]:
    """승격 게이트 판정 (설계서 §8-A 부속 — 운영 `ct1_promote_min_rmse_gain_pct` 소급).

    두 RMSE 는 **같은 웨이퍼 집합**에서 산출된 값이어야 한다 (호출자 책임).
    반환: (승격 여부, 상대 개선 %). 챔피언 RMSE 가 0/비유한이면 판정 불가로 raise.
    """
    if not np.isfinite(rmse_champ) or rmse_champ <= 0:
        raise ValueError(f"게이트 판정 불가 — 챔피언 RMSE={rmse_champ}")
    gain = (rmse_champ - rmse_chal) / rmse_champ * 100.0
    return bool(gain >= min_gain_pct), float(gain)


def build_assignment(days: pd.DatetimeIndex) -> pd.DataFrame:
    """평가일 → 각 군이 사용할 학습 컷오프 (설계서 §3 3군)."""
    d7_set = set(d7_retrain_days(days))
    d7_cut, cur = [], None
    for d in days:
        if d in d7_set:
            cur = d - pd.Timedelta(days=1)
        if cur is None:
            raise ValueError("ARM-D7 첫 재학습일이 평가 시작일이 아님 — 그리드 로직 오류")
        d7_cut.append(cur)
    return pd.DataFrame({
        "wf_day": days,
        "ARM-D1": [d - pd.Timedelta(days=1) for d in days],
        "ARM-D7": d7_cut,
        "ARM-STATIC": [C.INIT_TRAIN_END] * len(days),
    })


def main() -> int:
    """컷오프별 lean-85 재학습 → 담당 평가일 예측·프로브·게이트 판정을 기록한다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--buffer-days", type=int, default=C.BOUNDARY_BUFFER_DAYS,
                    help="§5 경계 완충 — 컷오프 직전 N일 웨이퍼를 학습에서 제외 (민감도 런)")
    ap.add_argument("--seed", type=int, default=C.SEED)
    ap.add_argument("--limit-days", type=int, default=0, help="스모크용: 앞 N 평가일만")
    ap.add_argument("--tag", default="main")
    ap.add_argument("--n-jobs", type=int, default=0, help="0 = xgboost 기본")
    args = ap.parse_args()

    out_dir = C.OUT_DIR / f"run_{args.tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    lean, params, rounds = lp.load_frozen()
    lp.check_feature_contract(lean)                       # 헌법 1-3 (raise, not assert)
    tbl = load_table()
    days = eval_days(tbl)
    if args.limit_days:
        days = days[:args.limit_days]

    assign = build_assignment(days)
    assign.to_csv(out_dir / "arm_assignment.csv", index=False)

    # ── 학습할 컷오프 = 3군이 실제로 참조하는 컷오프의 합집합 ──
    cutoffs = sorted(set(assign["ARM-D1"]) | set(assign["ARM-D7"]) | set(assign["ARM-STATIC"]))
    # 컷오프 → 그 컷오프 모델이 예측해야 할 평가일 (어느 군에서든 참조되면 포함)
    served: dict[pd.Timestamp, set[pd.Timestamp]] = {c: set() for c in cutoffs}
    for _, r in assign.iterrows():
        for a in C.ARMS:
            served[r[a]].add(r["wf_day"])

    # ── §6 M4 churn 고정 프로브 (모든 모델 버전이 동일 셋을 예측) ──
    rng = np.random.default_rng(args.seed)
    em = (tbl["wf_day"] >= C.EVAL_START) & (tbl["wf_day"] <= C.EVAL_END)
    pool = tbl.index[em].to_numpy()
    probe_idx = np.sort(rng.choice(pool, size=min(N_PROBE, len(pool)), replace=False))
    probe_X = tbl.loc[probe_idx, lean]
    pd.DataFrame({lp.ID_COL: tbl.loc[probe_idx, lp.ID_COL].to_numpy()}).to_csv(
        out_dir / "probe_wafers.csv", index=False)

    logger.info("평가일 %d · 고유 컷오프 %d · 프로브 %d wafer", len(days), len(cutoffs),
                len(probe_idx))
    logger.info("군별 명목 재학습: D1=%d D7=%d STATIC=0", len(days),
                len(set(assign["ARM-D7"])))

    xp = dict(params)
    xp.update(objective="reg:squarederror", tree_method="hist", device=lp.XGB_DEVICE,
              random_state=args.seed, n_estimators=int(rounds))
    if args.n_jobs:
        xp["n_jobs"] = args.n_jobs

    y_all = tbl[lp.TARGET_COL].to_numpy(float)
    wf_day = tbl["wf_day"].to_numpy()

    # ── §8-A 게이트 시뮬레이션 상태 (군별 챔피언 모델을 메모리에 유지) ──
    # 챔피언·챌린저를 **챌린저가 담당할 날(양쪽 모두 out-of-sample)** 에서 같은 웨이퍼로
    # 비교한다. 사후 소급 판정이므로 라벨을 쓰는 것은 설계서 §8-A 취지("소급 적용")에 맞고,
    # 주 분석(무조건 교체)에는 아무 영향이 없다 — 별도 파일로만 기록한다.
    gate_arms = {"ARM-D1": {}, "ARM-D7": {}}     # arm → {champ_model, champ_cutoff}
    gate_rows: list[dict] = []
    arm_cutoff_seq = {a: list(dict.fromkeys(assign[a])) for a in ("ARM-D1", "ARM-D7")}

    pred_rows, fit_rows, probe_cols = [], [], {}
    t_start = time.perf_counter()

    for i, c in enumerate(cutoffs):
        hi = c - pd.Timedelta(days=args.buffer_days)
        lo = c - pd.Timedelta(days=C.TRAIN_WINDOW_DAYS)
        tr = (wf_day <= np.datetime64(hi)) & (wf_day > np.datetime64(lo))
        n_tr = int(tr.sum())
        if n_tr < 100:
            raise ValueError(f"컷오프 {c.date()} 학습 표본 {n_tr} < 100 — 창 설정 확인")

        t0 = time.perf_counter()
        model = xgb.XGBRegressor(**xp)
        model.fit(tbl.loc[tr, lean], y_all[tr])
        fit_sec = time.perf_counter() - t0

        tr_max = tbl.loc[tr, "wf_ts"].max()
        if tr_max.normalize() > hi:               # 누수 가드 ① 시각 (헌법 1-3)
            raise ValueError(f"누수: 컷오프 {c.date()} 학습셋 최대 시각 {tr_max} > {hi.date()}")

        tgt = sorted(served[c])
        te = np.isin(wf_day, np.array([np.datetime64(d) for d in tgt]))
        if bool((tr & te).any()):                 # 누수 가드 ② wafer(C64) 단위 배타
            raise ValueError(f"누수: 컷오프 {c.date()} 학습·평가 wafer 교집합 "
                             f"{int((tr & te).sum())}건")
        sub = tbl.loc[te]
        p = model.predict(sub[lean])
        pred_rows.append(pd.DataFrame({
            "cutoff": c,
            "wf_day": sub["wf_day"].to_numpy(),
            "model_age": (sub["wf_day"] - c).dt.days.to_numpy() - 1,
            lp.ID_COL: sub[lp.ID_COL].to_numpy(),
            "y": sub[lp.TARGET_COL].to_numpy(float),
            "pred": p.astype(float),
            "low_conf": lp.low_confidence_flags(sub).to_numpy(),
        }))
        probe_cols[str(c.date())] = model.predict(probe_X).astype(float)

        # ── §8-A: 군별 챔피언과 이번 챌린저를 '챌린저 담당일' 동일 셋에서 비교 ──
        for arm, seq in arm_cutoff_seq.items():
            if c not in seq:
                continue
            arm_days = set(assign.loc[assign[arm] == c, "wf_day"])
            g = gate_arms[arm]
            if not g:                                  # 첫 모델 = 무조건 챔피언
                g.update(model=model, cutoff=c)
                gate_rows.append({"arm": arm, "cutoff": c, "decision": "initial",
                                  "gain_pct": None, "n_eval": 0})
                continue
            ev = np.isin(wf_day, np.array([np.datetime64(d) for d in sorted(arm_days)]))
            n_ev = int(ev.sum())
            if n_ev < 30:                              # 표본 부족 → 판정 보류(챔피언 유지)
                gate_rows.append({"arm": arm, "cutoff": c, "decision": "skip(n<30)",
                                  "gain_pct": None, "n_eval": n_ev})
                continue
            Xe, ye = tbl.loc[ev, lean], y_all[ev]
            r_champ = float(np.sqrt(np.mean((ye - g["model"].predict(Xe)) ** 2)))
            r_chal = float(np.sqrt(np.mean((ye - model.predict(Xe)) ** 2)))
            promote, gain = gate_decision(r_champ, r_chal, C.PROMOTE_MIN_RMSE_GAIN_PCT)
            gate_rows.append({"arm": arm, "cutoff": c,
                              "decision": "promote" if promote else "keep",
                              "gain_pct": round(gain, 4), "n_eval": n_ev,
                              "rmse_champ": round(r_champ, 4), "rmse_chal": round(r_chal, 4),
                              "champ_cutoff": g["cutoff"],
                              "champ_age_days": int((c - g["cutoff"]).days)})
            if promote:
                g.update(model=model, cutoff=c)

        fit_rows.append({"cutoff": c, "n_train": n_tr, "fit_sec": round(fit_sec, 3),
                         "train_min": str(tbl.loc[tr, "wf_ts"].min()), "train_max": str(tr_max),
                         "n_served_days": len(tgt), "n_served_wafers": int(te.sum())})
        if i % 20 == 0 or i == len(cutoffs) - 1:
            el = time.perf_counter() - t_start
            eta = el / (i + 1) * (len(cutoffs) - i - 1)
            logger.info("[%3d/%d] cutoff=%s n_train=%6d fit=%5.1fs  경과 %.1f분 · ETA %.1f분",
                        i + 1, len(cutoffs), c.date(), n_tr, fit_sec, el / 60, eta / 60)

    preds = pd.concat(pred_rows, ignore_index=True)
    preds.to_parquet(out_dir / "predictions.parquet", index=False)
    pd.DataFrame(probe_cols).to_parquet(out_dir / "probe_preds.parquet", index=False)
    fits = pd.DataFrame(fit_rows)
    fits.to_csv(out_dir / "fits.csv", index=False)
    pd.DataFrame(gate_rows).to_csv(out_dir / "gate_sim.csv", index=False)

    prep = json.loads((C.OUT_DIR / "prep_report.json").read_text(encoding="utf-8")) \
        if (C.OUT_DIR / "prep_report.json").exists() else {}
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "tag": args.tag, "seed": args.seed, "buffer_days": args.buffer_days,
        "protocol": "rolling-origin (설계서 §4)",
        "eval_window": [str(days[0].date()), str(days[-1].date())],
        "n_eval_days": int(len(days)),
        "n_retrain_nominal": {"ARM-D1": int(len(set(assign["ARM-D1"]))),
                              "ARM-D7": int(len(set(assign["ARM-D7"]))), "ARM-STATIC": 0},
        "n_distinct_fits": int(len(cutoffs)),
        "train_window_days": C.TRAIN_WINDOW_DAYS,
        "time_axis": "C40 실측 타임스탬프 (사전 등록 균등 근사를 대체 — 부록 A-6 v1.1)",
        "features_n": len(lean),
        "features_sha1": hashlib.sha1(",".join(lean).encode()).hexdigest()[:12],
        "n_estimators": int(rounds), "params": params,
        "total_fit_sec": round(float(fits["fit_sec"].sum()), 1),
        "mean_fit_sec": round(float(fits["fit_sec"].mean()), 2),
        "wall_clock_sec": round(time.perf_counter() - t_start, 1),
        "gate_applied": False,
        "gate_note": "설계서 §3 — 주 분석은 무조건 교체. 게이트 시나리오는 §8-A 부속 분석",
        "n_probe_wafers": int(len(probe_idx)),
        "wafer_day_map_sha1": prep.get("wafer_day_map_sha1"),
        "pm_log_exp": prep.get("pm_log_exp"),
        "env": {"python": platform.python_version(), "xgboost": xgb.__version__,
                "pandas": pd.__version__, "numpy": np.__version__,
                "platform": platform.platform()},
        "note": "오프라인 백테스트 — models/ 등재·CHANGELOG 기록 대상 아님 (설계서 §4)",
    }
    (out_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("완료: %s  (fit 총 %.1f분, 평균 %.2f초)", out_dir,
                fits["fit_sec"].sum() / 60, fits["fit_sec"].mean())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
