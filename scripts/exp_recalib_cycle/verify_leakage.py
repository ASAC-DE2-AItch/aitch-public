# -*- coding: utf-8 -*-
"""verify_leakage.py — 설계서 §5 누수 차단 체크리스트 실행 검증 (헌법 1-3).

체크리스트 5항목을 **실행으로** 확인하고 out/leakage_report.json 에 기록한다.
하나라도 실패하면 exit code 1 — run_all 이 여기서 멈춘다 (백테스트 진입 차단).

  [1] C65 및 C65 파생(LOT 평균 포함)의 피처 사용 금지
  [2] 학습/평가 경계는 wafer(C64) 단위 — 한 wafer 가 두 쪽에 걸치지 않음
  [3] d일 예측 모델의 학습셋 = d-1일까지 완결 wafer만
  [4] pm_log 절단 등가성 + naive UTC 포맷
  [5] 가드는 assert 가 아닌 명시적 raise (헌법 7장)
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import CONFIG as C

sys.path.insert(0, str(C.LEAN85_DIR))
import lean85_pipeline as lp  # noqa: E402
import run_backtest as rb  # noqa: E402   (check 3 이 실제 배정 로직을 직접 호출)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("leakage")


def check_1_no_target_features(lean) -> dict:
    """[1] C65 및 파생·식별자가 피처에 없는지 — 동결 피처셋 대조."""
    banned_exact = {lp.TARGET_COL, lp.ID_COL, lp.LOT_COL, "fold_kf5", "wf_ts", "lot_ts"}
    hits = sorted(set(lean) & banned_exact)
    pat = re.compile(rf"(^|_){lp.TARGET_COL}($|_)")
    derived = sorted(c for c in lean if pat.search(c))
    lot_derived = sorted(c for c in lean if re.search(rf"(^|_){lp.LOT_COL}($|_)", c))
    ok = not (hits or derived or lot_derived)
    return {"ok": ok, "n_features": len(lean), "banned_exact_hits": hits,
            "target_derived": derived, "lot_derived": lot_derived,
            "note": "lean-85 동결 피처셋 구조적 보장 + 실행 전 1회 대조 (설계서 §5)"}


def check_2_wafer_boundary(tbl: pd.DataFrame) -> dict:
    """[2] wafer(C64)가 학습·평가에 걸치지 않음 — wafer 1행 테이블이므로 구조적."""
    dup = int(tbl[lp.ID_COL].duplicated().sum())
    multi_day = int(tbl.groupby(lp.ID_COL)["wf_day"].nunique().gt(1).sum())
    return {"ok": dup == 0 and multi_day == 0, "duplicate_wafer_rows": dup,
            "wafers_spanning_multiple_days": multi_day,
            "note": "테이블이 wafer 1행이므로 날짜 경계 = wafer 경계 (스텝 행 분할 불가)"}


def check_3_train_cutoff(tbl: pd.DataFrame, buffer_days: int) -> dict:
    """[3] **백테스트가 실제로 쓰는 배정**(`run_backtest.build_assignment`)을 검사한다.

    자체 규칙을 재구현하면 "컷오프 = d-1" 이라는 항등식을 확인하는 무의미한 검사가 된다.
    그래서 실제 코드 경로인 `build_assignment` 를 호출해 그 산출물을 검증한다 —
    배정 로직이 미래를 보도록 회귀하면 여기서 잡힌다.

    검사: ⓐ 모든 군의 컷오프 < 평가일 ⓑ 학습창 상한(컷오프-buffer)에 속한 wafer 집합과
    그 평가일 wafer 집합의 교집합 0 ⓒ D7 모델 나이 0~6 ⓓ STATIC 컷오프 = M0 고정.
    """
    days = pd.DatetimeIndex(sorted(
        tbl.loc[(tbl["wf_day"] >= C.EVAL_START) & (tbl["wf_day"] <= C.EVAL_END), "wf_day"].unique()))
    assign = rb.build_assignment(days)

    bad: list[dict] = []
    for arm in C.ARMS:
        future = assign[assign[arm] >= assign["wf_day"]]
        for _, r in future.iterrows():
            bad.append({"arm": arm, "day": str(r["wf_day"].date()),
                        "cutoff": str(r[arm].date()), "why": "컷오프가 평가일 이후"})
    age = (assign["wf_day"] - assign["ARM-D7"]).dt.days - 1
    if not bool(age.between(0, 6).all()):
        bad.append({"arm": "ARM-D7", "why": f"모델 나이 범위 이탈 max={int(age.max())}"})
    if not bool((assign["ARM-STATIC"] == C.INIT_TRAIN_END).all()):
        bad.append({"arm": "ARM-STATIC", "why": "M0 컷오프 고정 위반"})

    # 실제 학습셋/평가셋 wafer 교집합 (배정 표본에서 직접 계산)
    rng = np.random.default_rng(C.SEED)
    picks = rng.choice(len(assign), size=min(40, len(assign)), replace=False)
    by_day = {d: set(g[lp.ID_COL]) for d, g in tbl.groupby("wf_day")}
    for i in sorted(picks):
        r = assign.iloc[int(i)]
        for arm in C.ARMS:
            hi = r[arm] - pd.Timedelta(days=buffer_days)
            lo = r[arm] - pd.Timedelta(days=C.TRAIN_WINDOW_DAYS)
            tr = tbl[(tbl["wf_day"] <= hi) & (tbl["wf_day"] > lo)]
            ov = len(set(tr[lp.ID_COL]) & by_day.get(r["wf_day"], set()))
            if ov:
                bad.append({"arm": arm, "day": str(r["wf_day"].date()),
                            "why": f"학습·평가 wafer 교집합 {ov}건"})
    return {"ok": not bad, "n_assignment_rows": int(len(assign)),
            "n_sampled_days": int(len(picks)), "violations": bad[:20],
            "buffer_days": buffer_days,
            "note": "run_backtest.build_assignment 산출물을 직접 검증 (자체 재구현 아님)"}


def check_4_pm_log(n_rows: int) -> dict:
    """[4] pm_log 절단 등가성 + naive UTC 포맷.

    검증 대상 주장(README ④·prep_data): "`_meta_features` 의 PM 탐색은 후향이므로
    전체 pm_log 로 1회 빌드해도 컷오프 이전 웨이퍼의 메타 피처는 절단본과 동일하다."

    이 주장을 재구현으로 확인하면 의미가 없다(`searchsorted` 를 여기서 다시 짜면
    파이프라인이 바뀌어도 통과한다). 그래서 **`lp._meta_features` 를 실제로 두 번 호출**해
    — 전체 pm_log vs 컷오프 절단 pm_log — 결과 프레임을 직접 비교한다.
    파이프라인이 전방 탐색으로 회귀하면 여기서 불일치가 잡힌다.
    """
    pm = json.loads(C.PM_LOG_EXP.read_text(encoding="utf-8"))
    fmt_bad = [e["date"] for e in pm
               if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", e["date"])]
    tz_bad = [e["date"] for e in pm if pd.Timestamp(e["date"]).tzinfo is not None]

    # raw 표본: 메타 피처에 필요한 컬럼만 (C40 시각 · C33 · C6 recipe · C20 lot · C64)
    cols = [lp.ID_COL, lp.LOT_COL, lp.C33_COL, lp.RECIPE_COL, lp.TIME_COL]
    raw = pd.read_csv(C.FULL_YEAR_GZ, usecols=cols, nrows=n_rows)
    ts = pd.to_datetime(raw[lp.TIME_COL], format=lp.FMT)

    results, mism = [], 0
    for cut in (pd.Timestamp("2019-05-01"), pd.Timestamp("2019-09-01"), C.EVAL_END):
        sub = raw[ts <= cut]                      # 컷오프 이전 웨이퍼만
        if len(sub) < 100:
            continue
        trunc = [e for e in pm if pd.Timestamp(e["date"]) <= cut]
        full_meta = lp._meta_features(sub, pm).sort_values(lp.ID_COL).reset_index(drop=True)
        tr_meta = lp._meta_features(sub, trunc).sort_values(lp.ID_COL).reset_index(drop=True)
        cmp_cols = [c for c in ("days_since_last_pm", "is_high_regime", "high_regime_days",
                                "dslp_x_hour") if c in full_meta.columns]
        diff = int((~np.isclose(full_meta[cmp_cols].to_numpy(float),
                                tr_meta[cmp_cols].to_numpy(float),
                                rtol=0, atol=1e-9, equal_nan=True)).sum())
        mism += diff
        results.append({"cutoff": str(cut.date()), "n_wafers": int(len(full_meta)),
                        "n_pm_full": len(pm), "n_pm_truncated": len(trunc),
                        "cell_mismatches": diff})

    if not results:
        raise ValueError("절단 등가 검증 표본 0건 — raw 표본 크기(n_rows)를 늘리세요")

    return {"ok": not fmt_bad and not tz_bad and mism == 0,
            "n_pm_events": len(pm), "format_violations": fmt_bad, "tz_aware": tz_bad,
            "truncation_mismatches": mism, "per_cutoff": results,
            "method": "lp._meta_features 를 전체/절단 pm_log 로 각각 호출해 프레임 직접 비교",
            "simplification": "요란/조용 판정 = 즉시 인지 가정 (설계서 §5·§9 한계)"}


def check_5_no_assert() -> dict:
    """[5] 실험 스크립트에 assert 기반 가드가 없는지 (헌법 7장 — `python -O` 무력화 방지)."""
    hits = []
    for f in sorted(C.EXP_DIR.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                hits.append({"file": f.name, "line": node.lineno})
    return {"ok": not hits, "assert_usages": hits}


def main() -> int:
    """§5 체크리스트 5항목을 실행하고 leakage_report.json 을 남긴다 (실패 시 exit 1)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta-sample-rows", type=int, default=300_000,
                    help="check 4 의 _meta_features 대조 표본 raw 행 수")
    ap.add_argument("--buffer-days", type=int, default=C.BOUNDARY_BUFFER_DAYS)
    args = ap.parse_args()

    lean, _, _ = lp.load_frozen()
    tbl = pd.read_parquet(C.TABLE_PARQUET, columns=[lp.ID_COL, "wf_ts", "wf_day"])
    tbl["wf_day"] = pd.to_datetime(tbl["wf_day"]); tbl["wf_ts"] = pd.to_datetime(tbl["wf_ts"])

    checks = {
        "1_no_target_or_id_features": check_1_no_target_features(lean),
        "2_wafer_level_boundary": check_2_wafer_boundary(tbl),
        "3_train_cutoff_causal": check_3_train_cutoff(tbl, args.buffer_days),
        "4_pm_log_truncation_and_format": check_4_pm_log(args.meta_sample_rows),
        "5_explicit_raise_not_assert": check_5_no_assert(),
    }
    all_ok = all(v["ok"] for v in checks.values())
    report = {"generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
              "constitution": "1-3 데이터 누수 완전 차단 / 설계서 §5",
              "all_ok": all_ok, "checks": checks}
    (C.OUT_DIR / "leakage_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for k, v in checks.items():
        logger.info("[%s] %s", "PASS" if v["ok"] else "FAIL", k)
    if not all_ok:
        logger.error("누수 체크리스트 실패 — 백테스트 진입 차단 (헌법 1-3)")
        return 1
    logger.info("누수 체크리스트 전 항목 PASS → %s", C.OUT_DIR / "leakage_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
