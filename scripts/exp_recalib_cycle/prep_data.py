# -*- coding: utf-8 -*-
"""prep_data.py — 1단계: 데이터 검증 + wafer 테이블 1회 고정 생성 (설계서 §2).

산출:
  out/wafer_table_1y.parquet   웨이퍼 1행 테이블 [C64, C20, wf_ts, lean-85 85피처, C65]
  out/wafer_day_map.csv        wafer → 날짜 매핑 (해시 기록) — 3군 공유
  out/pm_log_exp.json          실험용 pm_log (요란 PM = major only, naive UTC)
  out/prep_report.json         §2 확인 항목 검증 결과

★ 설계서 §2 정정 (본 스크립트가 근거):
  설계서는 "데이터에 타임스탬프 컬럼이 없다 → 월내 균등 배치로 근사"를 전제했으나,
  **C40 이 실제 웨이퍼 타임스탬프**다 (lean85_pipeline.TIME_COL = "C40", 전 구간 존재).
  따라서 균등 근사를 쓰지 않고 **C40 실측 시각**으로 매핑한다 — §9 "매핑 근사" 위협은
  해소되며, 3군이 같은 매핑을 공유한다는 성질도 그대로다.

메모리 전략: 필요한 컬럼(lean-85 원천 24센서 + 메타 7)만 chunk 로 읽어 wafer 단위로
집계 후 concat. 전량 로드(834k×65)는 3GB 환경에서 위험하다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import CONFIG as C

sys.path.insert(0, str(C.LEAN85_DIR))
import lean85_pipeline as lp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("prep")

CHUNK_ROWS = 120_000


def _needed_cols(lean) -> tuple[list[str], list[str]]:
    """lean-85 이 실제로 요구하는 raw 컬럼만 추린다 (메모리 절감)."""
    _, sensors = lp._lean_sensor_cols(lean)
    meta = [lp.ID_COL, lp.STEP_COL, lp.LOT_COL, lp.C33_COL, lp.RECIPE_COL,
            lp.TIME_COL, lp.TARGET_COL]
    return sensors, meta


def _load_raw_needed(path, lean) -> pd.DataFrame:
    """full_year csv.gz 를 필요한 컬럼만 chunk 로 읽는다."""
    sensors, meta = _needed_cols(lean)
    usecols = meta + sensors
    dtypes = {s: "float32" for s in sensors}
    dtypes.update({lp.ID_COL: "string", lp.LOT_COL: "string", lp.RECIPE_COL: "string",
                   lp.TIME_COL: "string"})
    parts = []
    for i, ch in enumerate(pd.read_csv(path, usecols=usecols, dtype=dtypes,
                                       chunksize=CHUNK_ROWS, low_memory=False)):
        parts.append(ch)
        logger.info("  chunk %d rows=%d (누적 %d)", i, len(ch), sum(len(p) for p in parts))
    return pd.concat(parts, ignore_index=True)


def _build_pm_log_exp() -> list[dict]:
    """실험용 pm_log = 요란(loud) PM 만. 운영 규약(pm_log.json)과 동일 스킴.

    - 실측 구간: 루트 pm_log.json (2018-12-24 major/loud)
    - 합성 구간: pm_ledger.csv 의 kind == 'major'
      minor 는 규약상 pm_log 미기입(README §재학습 SOP: "요란 PM만 기입") — 피처에서 제외되고
      §6 M2-ⓑ 구간 분해에서만 쓴다.
    시각 포맷은 **naive UTC "%Y-%m-%d %H:%M:%S" 고정** (헌법 7장 실수 목록 — aware/naive 혼입 시
    `_parse_pm_entries` 의 sorted 가 TypeError 로 죽는다).
    """
    out = []
    for e in json.loads((C.REPO_ROOT / "pm_log.json").read_text(encoding="utf-8")):
        d = e["date"] if isinstance(e, dict) else e
        ts = pd.Timestamp(d)
        if ts.tzinfo is not None:
            raise ValueError(f"pm_log.json 에 tz-aware 시각: {d} (naive UTC 고정 규약 위반)")
        out.append({"date": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "type": (e.get("type", "major") if isinstance(e, dict) else "major"),
                    "verdict": "loud", "source": "pm_log.json (실측)"})

    led = pd.read_csv(C.PM_LEDGER)
    for _, r in led[led["kind"] == "major"].iterrows():
        ts = pd.Timestamp(r["time"])
        if ts.tzinfo is not None:
            raise ValueError(f"pm_ledger 에 tz-aware 시각: {r['time']}")
        out.append({"date": ts.strftime("%Y-%m-%d %H:%M:%S"), "type": "major",
                    "verdict": "loud", "source": "pm_ledger.csv (합성)"})
    out.sort(key=lambda e: e["date"])
    return out


def _sha1_file(path) -> str:
    """파일 SHA1 (wafer_day_map 고정 해시 기록용 — 3군 동일 매핑 증빙)."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _outputs_complete() -> bool:
    """산출물 4종이 **전부** 있고 계약 수치가 맞을 때만 재사용한다.

    parquet 존재만 보면, 중단된 런이 남긴 스테일 parquet 위에서 verify/backtest 가
    옛 pm_log 기준으로 돌 수 있다 (조용한 오염). 매니페스트까지 대조한다.
    """
    need = (C.TABLE_PARQUET, C.WAFER_DAY_MAP, C.PM_LOG_EXP, C.OUT_DIR / "prep_report.json")
    if not all(p.exists() for p in need):
        return False
    try:
        rep = json.loads((C.OUT_DIR / "prep_report.json").read_text(encoding="utf-8"))
        return rep.get("rows") == C.EXPECT_ROWS and rep.get("wafers") == C.EXPECT_WAFERS
    except (ValueError, OSError):
        return False


def main() -> int:
    """데이터 계약 검증 → wafer 테이블·매핑·실험용 pm_log 생성 → prep_report 기록."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="기존 산출물 덮어쓰기")
    args = ap.parse_args()

    C.OUT_DIR.mkdir(parents=True, exist_ok=True)
    if _outputs_complete() and not args.force:
        logger.info("산출물 4종 완비 + 계약 일치 → 재사용 (--force 로 재생성)")
        return 0

    lean, params, rounds = lp.load_frozen()      # R2 누수·R10 floor 계약 검증 포함
    logger.info("동결 스펙 로드: lean-%d, rounds=%d", len(lean), rounds)

    logger.info("raw 로드: %s", C.FULL_YEAR_GZ)
    raw = _load_raw_needed(C.FULL_YEAR_GZ, lean)
    n_rows = len(raw)
    n_wafers = raw[lp.ID_COL].nunique()
    logger.info("raw rows=%d wafers=%d", n_rows, n_wafers)

    # ── 설계서 §2 데이터 계약 (불일치 = 즉시 실패, assert 금지 — 헌법 7장) ──
    if n_rows != C.EXPECT_ROWS or n_wafers != C.EXPECT_WAFERS:
        raise ValueError(
            f"설계서 §2 실측 집계와 불일치: rows {n_rows} (기대 {C.EXPECT_ROWS}) / "
            f"wafers {n_wafers} (기대 {C.EXPECT_WAFERS})")

    pm_log = _build_pm_log_exp()
    C.PM_LOG_EXP.write_text(json.dumps(pm_log, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("실험용 pm_log(요란만) %d건: %s", len(pm_log), [e["date"][:10] for e in pm_log])

    # ── wafer 테이블 1회 생성 ────────────────────────────────────────
    # 인과성 근거: `_meta_features` 의 PM 탐색은 searchsorted(side="right")-1 로
    # **웨이퍼 자기 시각 이전의 PM 만** 본다. 따라서 전체 pm_log 로 한 번 빌드해도
    # 각 행의 피처는 그 웨이퍼 시점 기지값뿐이다 (미래 PM 은 결과에 영향 없음).
    # 이 등가성은 verify_leakage.py 에서 절단본 대조로 실증한다 (설계서 §5).
    logger.info("wafer 테이블 빌드 (집계 %d rows → %d wafers)...", n_rows, n_wafers)
    tbl = lp.build_wafer_table(raw, pm_log, lean)
    del raw

    tbl["wf_ts"] = pd.to_datetime(tbl["wf_ts"])
    tbl["wf_day"] = tbl["wf_ts"].dt.normalize()
    # nullable boolean(pandas string dtype) 가 섞이지 않게 확정 bool 로 고정
    tbl["is_synth"] = tbl[lp.ID_COL].str.startswith("C64_S").fillna(False).astype(bool)
    tbl = tbl.sort_values("wf_ts").reset_index(drop=True)

    if tbl["wf_ts"].isna().any():
        raise ValueError("wf_ts NaT 발생 — C40 파싱 실패 웨이퍼 존재")
    if tbl[lp.ID_COL].duplicated().any():
        raise ValueError("wafer ID 중복 — 집계 경계 오류")

    tbl.to_parquet(C.TABLE_PARQUET, index=False)
    logger.info("저장: %s (%d wafers)", C.TABLE_PARQUET, len(tbl))

    # ── wafer_day_map.csv (3군 공유 · 해시 기록) ─────────────────────
    dmap = tbl[[lp.ID_COL, "wf_day", "is_synth"]].copy()
    dmap["wf_day"] = dmap["wf_day"].dt.strftime("%Y-%m-%d")
    dmap.to_csv(C.WAFER_DAY_MAP, index=False)
    map_sha1 = _sha1_file(C.WAFER_DAY_MAP)

    # ── §2 확인 항목 검증 ────────────────────────────────────────────
    real = tbl[~tbl["is_synth"]]
    synth = tbl[tbl["is_synth"]]
    per_day = tbl.groupby("wf_day").size()
    eval_days = pd.date_range(C.EVAL_START, C.EVAL_END, freq="D")
    covered = per_day.reindex(eval_days).fillna(0)

    ev = pd.read_csv(C.EVENTS_LEDGER)
    synth_ids = synth[lp.ID_COL].str.replace("C64_S", "", regex=False).astype(int)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(n_rows), "wafers": int(n_wafers),
        "contract_ok": True,
        "time_axis": {
            "source": "C40 (실측 타임스탬프)",
            "design_doc_said": "타임스탬프 컬럼 없음 → 월내 균등 배치 근사",
            "correction": "C40 이 전 구간 존재 → 균등 근사 불필요. §9 '매핑 근사' 위협 해소",
            "span": [str(tbl['wf_ts'].min()), str(tbl['wf_ts'].max())],
        },
        "real_segment": {
            "wafers": int(len(real)),
            "span": [str(real['wf_ts'].min()), str(real['wf_ts'].max())],
            "calendar_days": int(real['wf_day'].nunique()),
            "note": "README 표기 68일 vs 달력일수 — 아래 production_days 로 정본 확정",
        },
        "synth_segment": {
            "wafers": int(len(synth)),
            "span": [str(synth['wf_ts'].min()), str(synth['wf_ts'].max())],
            "calendar_days": int(synth['wf_day'].nunique()),
            "s_index_range": [int(synth_ids.min()), int(synth_ids.max())],
        },
        "day_check_q1": {
            "설계서_확인항목": "README 실측 68일/합성 297일 vs 달력 70일/295일 — 2일 불일치",
            "판정": ("생산일(wafer 존재 날짜) 기준이 정본. "
                     f"실측 생산일={int(real['wf_day'].nunique())}, "
                     f"합성 생산일={int(synth['wf_day'].nunique())}"),
        },
        "events_ledger_q2": {
            "설계서_확인항목": "wafer_start/end 인덱스(최대 68,106)가 합성 S-인덱스인지",
            "s_index_max": int(synth_ids.max()),
            "ledger_wafer_end_max": int(ev["wafer_end"].max()),
            "판정": ("S-인덱스로 확인 (ledger max ≤ S-index max)"
                     if int(ev["wafer_end"].max()) <= int(synth_ids.max())
                     else "불일치 — 생성기 대조 필요"),
        },
        "eval_window": {
            "start": str(C.EVAL_START.date()), "end": str(C.EVAL_END.date()),
            "n_days_calendar": int(len(eval_days)),
            "n_days_with_wafers": int((covered > 0).sum()),
            "n_wafers": int(((tbl["wf_day"] >= C.EVAL_START) & (tbl["wf_day"] <= C.EVAL_END)).sum()),
            "empty_days": [str(d.date()) for d in eval_days[covered.to_numpy() == 0]][:20],
        },
        "init_train": {
            "cutoff": str(C.INIT_TRAIN_END.date()),
            "n_wafers": int((tbl["wf_day"] <= C.INIT_TRAIN_END).sum()),
        },
        "pm_log_exp": pm_log,
        "wafer_day_map_sha1": map_sha1,
        "target_stats": {"mean": float(tbl[lp.TARGET_COL].mean()),
                         "std": float(tbl[lp.TARGET_COL].std()),
                         "n_missing": int(tbl[lp.TARGET_COL].isna().sum())},
        "env": {"python": sys.version.split()[0], "pandas": pd.__version__,
                "numpy": np.__version__},
    }
    (C.OUT_DIR / "prep_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("검증 리포트: %s", C.OUT_DIR / "prep_report.json")
    logger.info("wafer_day_map sha1=%s", map_sha1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
