# -*- coding: utf-8 -*-
"""
lean85_pipeline.py — lean-85 최종 운영 파이프라인 (handoff 단일 소스)
════════════════════════════════════════════════════════════════════════
확정 모델 (2026-07-19 사용자 확정):
    lean-85 = Conservative-GA(99) − 시간불안정 14  (v13 REPORT_12)
    학습기  = XGBoost (v13 M5 튜닝 파라미터 동결, 246 rounds, seed 42)
    운영    = **정기 재학습 전제** (주간 또는 요란 PM 기입 시 즉시)

정직 수치 (현업 인용 규칙 — REPORT_01 §5):
    시간축(미래 1주, 재학습): pooled RMSE 99.84 · honest R² 0.8545   ← 현업 인용
    lot-CV(GKF C20, same-era): stable 66.83 / seed_mean 66.956      ← same-era 전용
    무재학습: 254.9 (배포 불가 → 재학습은 선택이 아니라 필수)

규율 승계:
    R2  누수 금지: C64/C20/fold 등 식별자는 피처에 절대 미포함 (**명시적 raise** — `assert`
        는 `python -O` 하나로 사라져 금지. 헌법 7장·리뷰 §2-2)
    R6  공식 판정 = 사용자 로컬 venv. 미러/타 환경 수치는 상대비교 전용
    R10 floor: 필수 5센서(C17·C11·C31·C15·C16) 각 ≥1 (동일하게 raise)
    인과: 모든 피처는 웨이퍼 시점 기지값(집계·시각·pm_log 과거 이벤트)만 사용

사용 (요약 — 자세한 건 handoff_lean85_README.md):
    import lean85_pipeline as lp
    lean, params, rounds = lp.load_frozen()
    raw   = pd.read_csv("train_data.csv")
    vdir, model, mani = lp.retrain(raw, lp.find_file("pm_log.json"), out_dir="models")
    new   = pd.read_csv("valid_X.csv")
    preds = lp.predict_lean85(model, lp.build_wafer_table(new, pm_log, lean), lean)
════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import mean_squared_error

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# 상수 (동결)
# ──────────────────────────────────────────────────────────────────────
PKG_DIR = Path(__file__).resolve().parent
FROZEN_DIR = PKG_DIR / "frozen"
REPO_ROOT = PKG_DIR.parents[2]                 # AITCH repo 루트 (lean85 → agent_a_mlops → src → AITCH)
DATA_DIR = REPO_ROOT / "Data" / "문제1(하)"     # AITCH 표준 데이터 위치 (train/valid/test)

ID_COL, STEP_COL, TARGET_COL, LOT_COL = "C64", "C7", "C65", "C20"
TIME_COL, FMT = "C40", "%Y-%m-%d %H:%M:%S.%f"
RECIPE_COL, C33_COL = "C6", "C33"

REF_DATE = pd.Timestamp("2018-12-01")      # 캠페인 시작일 — pre-PM 경과일 기준점 (v12 동결)
SIGMA_C65 = 261.7                          # honest R² 분모
AGG_FUNCS = ["mean", "std", "max", "min", "last"]

CORE10 = ["is_high_regime", "high_regime_days", "days_since_last_pm", "C33",
          "dslp_x_hour", "hour", "hour_x_c33", "C60_mean_step4", "C59_mean_step4",
          "is_special_recipe"]
PROTECTED = ["C17", "C11", "C31", "C15", "C16"]      # R10 필수 5센서
META_COLS = ["is_high_regime", "high_regime_days", "days_since_last_pm", "C33",
             "dslp_x_hour", "hour", "hour_x_c33", "is_special_recipe"]

# 운영 파라미터 — config/params.yaml[lean85] 단일 소스 (헌법 6-4).
# 파일·키·PyYAML 부재 시 아래 동결 기본값으로 폴백 → 동작 불변(Phase 0 재현성 보존).
def _load_lean85_config() -> dict:
    """config/params.yaml 의 lean85 섹션을 읽어 운영 파라미터 반환 (실패 시 동결 기본값)."""
    cfg = {"onset_days": 7.0, "retrain_days": 7, "bench_pooled_rmse": 99.840,
           "bench_tol": 0.5, "psi_warn": 0.10, "psi_alert": 0.25}
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            section = (yaml.safe_load(f) or {}).get("lean85") or {}
        cfg.update({k: section[k] for k in cfg if k in section})
    except Exception as e:                       # 폴백: 동결 기본값
        logger.warning("params.yaml[lean85] 로드 실패 → 동결 기본값 사용: %s", e)
    return cfg


_LEAN_CFG = _load_lean85_config()
ONSET_DAYS = float(_LEAN_CFG["onset_days"])           # 요란 PM 후 레짐-온셋 → low_confidence (B2′)
RETRAIN_DAYS = int(_LEAN_CFG["retrain_days"])         # 정기 재학습 주기(일) — 보조 트리거(계획 §4·PM 결정)
BENCH_POOLED = float(_LEAN_CFG["bench_pooled_rmse"])  # B2′ 시간축 수용검사 기준(정본 99.84)
BENCH_TOL = float(_LEAN_CFG["bench_tol"])             # 환경차 허용 오차 (R6 — 공식 판정=로컬 venv)
PSI_WARN = float(_LEAN_CFG["psi_warn"])               # 입력 PSI 주의 임계
PSI_ALERT = float(_LEAN_CFG["psi_alert"])             # 입력 PSI 경보 임계 (보조 트리거 후보)

XGB_DEVICE = os.environ.get("XGB_DEVICE", "cpu")


# ──────────────────────────────────────────────────────────────────────
# 공통 헬퍼
# ──────────────────────────────────────────────────────────────────────
def _rmse(a, b) -> float:
    return float(np.sqrt(mean_squared_error(a, b)))


def r2_honest(rmse: float) -> float:
    return round(1 - (rmse / SIGMA_C65) ** 2, 4)


def sensor_of(col: str) -> str:
    m = re.match(r"(C\d+)_", col)
    return m.group(1) if m else col


def floor_ok(feat_cols):
    have = {s: sum(1 for c in feat_cols if sensor_of(c) == s) for s in PROTECTED}
    return all(v >= 1 for v in have.values()), have


def find_file(name: str, extra_dirs=()) -> Path:
    """패키지·프로젝트 표준 위치에서 파일 탐색 (노트북/CLI 공용)."""
    root = PKG_DIR.parent
    dirs = [PKG_DIR, FROZEN_DIR, root, root / "문제1(하)",
            REPO_ROOT, REPO_ROOT / "Data", DATA_DIR,
            root / "modeling_v13" / "data", root / "modeling_v13" / "colab_GA",
            root / "modeling_v14" / "data", *map(Path, extra_dirs)]
    for d in dirs:
        p = d / name
        if p.exists():
            return p
    raise FileNotFoundError(f"'{name}' 을 찾지 못함. 탐색 위치: {[str(d) for d in dirs]}")


def _read_json(path):
    """JSON 읽기 — 파일 핸들을 반드시 닫는다 (`with`, 리뷰 §2-4)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(obj, path) -> None:
    """JSON 쓰기 — 버퍼 flush·핸들 종료 보장 (잘린 manifest 방지, 리뷰 §2-4)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def check_feature_contract(lean) -> None:
    """lean-85 피처 계약 검증 — R2(누수)·R10(floor).

    ⚠️ `assert` 가 아니라 **명시적 예외**다 (리뷰 §2-2). 누수 차단은 헌법 1-3 "PR 즉시 차단"
    등급 원칙인데, `assert` 는 `python -O` 실행 하나로 통째로 사라진다 — 방어선이 실행
    플래그에 좌우되면 안 된다.

    Raises:
        ValueError: 피처 수 불일치 · R10 floor 위반 · R2 식별자 유입.
    """
    if len(lean) != 85:
        raise ValueError(f"lean-85 아님: {len(lean)}")
    ok, have = floor_ok(lean)
    if not ok:
        raise ValueError(f"R10 floor 위반 (필수 5센서 각 ≥1): {have}")
    banned = {ID_COL, LOT_COL, TARGET_COL, "fold_kf5", "wf_ts", "lot_ts"}
    leaked = set(lean) & banned
    if leaked:
        raise ValueError(f"R2 위반 (헌법 1-3 데이터 누수): 식별자 유입 {leaked}")


def load_frozen():
    """동결 스펙 로드: (lean_features 85, xgb_params, n_estimators)."""
    lean = _read_json(FROZEN_DIR / "lean85_features.json")["lean_features"]
    xgj = _read_json(FROZEN_DIR / "tuned_params_xgb.json")
    params, rounds = xgj["params"], int(xgj["n_estimators"])

    check_feature_contract(lean)          # R2·R10 (명시적 예외 — 리뷰 §2-2)
    return lean, params, rounds


# pm_log 파싱 캐시: 경로 → (mtime, size, events). 추론은 wafer 1장마다 build_wafer_table 을
# 호출하므로, 캐시가 없으면 wafer 마다 디스크를 다시 읽는다 (리뷰 §3-3).
_PM_CACHE: dict = {}


def parse_pm_log(pm_log):
    """pm_log(list | 경로) → 시간순 [(Timestamp, type)]. 요란(loud) PM만 기입하는 규약.
    레거시 ["2018-12-24"] / 신규 [{"date": ..., "type": ...}] 모두 지원.

    경로로 주면 **mtime+size 기준 캐시**를 쓴다 — 파일이 갱신되면(요란 PM 기입) 자동으로
    무효화되므로 "최신 pm_log 사용" 규약(README §예측 SOP)은 그대로 유지된다.
    """
    if isinstance(pm_log, (str, Path)):
        path = Path(pm_log)
        try:
            st = path.stat()
            key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
        except OSError:                    # stat 실패 → 캐시 없이 정공법
            key = None
        if key is not None and key in _PM_CACHE:
            return list(_PM_CACHE[key])    # 방어적 복사 — 호출자의 in-place 정렬이 캐시를 오염시키지 않게
        events = _parse_pm_entries(_read_json(path))
        if key is not None:
            _PM_CACHE.clear()              # 경로당 최신 1개만 유지 (무한 성장 방지)
            _PM_CACHE[key] = events
            return list(events)
        return events
    return _parse_pm_entries(pm_log)


def _parse_pm_entries(entries):
    """pm_log 엔트리 리스트 → 시간순 [(Timestamp, type)].

    시각은 **naive UTC 로 정규화**한다 (계약 §8-B-1 · 코드리뷰 3차 M-2, 2026-08-08):
    aware 엔트리가 1건이라도 섞이면 아래 `sorted` 가 naive/aware 비교 TypeError 로
    죽는다 — 헌법 7장 등재 사고("pm_log tz-aware 기입 → A 추론 전체 다운")의 재발
    경로이고, 이 함수는 `due_event`(오케스트레이터 상주 60초 폴링)·`_meta_features`
    (조립·재학습·게이트·**consumer 서빙**)가 공유하는 핫패스다. 같은 파일의
    `parse_pm_entries_full` 은 이미 같은 방어를 갖고 있었다 — 비대칭 해소.
    규약 준수 데이터(전량 naive)에는 동작 불변이다 (`naive_utc` 는 naive 를 그대로 둔다).
    """
    out = []
    for e in entries:
        if isinstance(e, dict):
            out.append((naive_utc(e["date"]), e.get("type", "major")))
        else:
            out.append((naive_utc(e), "major"))
    return sorted(out, key=lambda x: x[0])


def naive_utc(ts) -> pd.Timestamp:
    """tz-aware 시각 → **naive UTC** 로 정규화 (계약 §8-B-1 시각 규율).

    왜 필요한가: wafer 시각(`C40`)·pm_log 는 전부 naive 인데 오케스트레이터는
    `datetime.now(timezone.utc)`(aware)로 돈다. 둘을 그대로 비교하면 pandas 가
    `TypeError: Cannot compare tz-naive and tz-aware timestamps` 를 낸다 — 헌법 7장에
    이미 등재된 사고(`pm_log` aware 기입 → `sorted` TypeError → A 추론 전체 다운)와
    같은 계열이다. 경계에서 한 번 정규화해 시간축을 단일화한다.
    """
    t = pd.Timestamp(ts)
    return t.tz_convert(None) if t.tz is not None else t


def parse_pm_entries_full(pm_log) -> list[dict]:
    """pm_log → 시간순 `[{"date": Timestamp, "type": str, "verdict": str}]`.

    `parse_pm_log` 이 (date, type) 2-튜플만 돌려주는 것과 달리 **verdict(요란/조용)까지**
    보존한다 — CT① 학습창의 '완결 요란 레짐' 판정(설계 D2)이 verdict 를 필요로 한다.
    기존 호출부를 건드리지 않으려고 별도 함수로 뽑았다 (parse_pm_log 는 wafer 마다
    호출되는 뜨거운 경로라 반환 형태를 바꾸면 파급이 크다).

    verdict 기본값 = `"loud"` — 계약 §8-B-1 상 `pm_log.json` 은 **요란 PM만 기입**하는
    규약이므로, 필드가 없는 레거시 엔트리는 요란으로 본다 (누락 시 창 확장을 과소
    발동시켜 희소 전환 표본을 놓치는 쪽보다, 있는 그대로 요란으로 세는 쪽이 안전).

    Args:
        pm_log: 경로(str|Path) 또는 엔트리 리스트.

    Returns:
        date 오름차순 dict 리스트. CT 실행당 1회만 부르므로 캐시하지 않는다.
    """
    entries = _read_json(pm_log) if isinstance(pm_log, (str, Path)) else pm_log
    out = []
    for e in entries:
        if isinstance(e, dict):
            out.append({"date": naive_utc(e["date"]),           # tz 혼재 방어 (헌법 7장)
                        "type": e.get("type", "major"),
                        "verdict": str(e.get("verdict", "loud")).lower()})
        else:
            out.append({"date": naive_utc(e), "type": "major", "verdict": "loud"})
    return sorted(out, key=lambda x: x["date"])


def complete_loud_regimes(entries, *, until=None) -> list[tuple]:
    """완결된 요란 레짐 목록 → `[(레짐 시작, 레짐 종료)]` (시간순).

    레짐 = **요란 PM ~ 다음 PM**. '완결'은 다음 PM 이 실재하고(진행 중이 아님) 그 시각이
    `until` 이하라는 뜻이다. 판정 소스는 **pm_log 의 물리 PM 이벤트뿐** — 모델 출력·정착
    판정을 창 결정에 쓰지 않는다 (설계 D2: 순환 참조 차단).

    ⚠️ 한계(명시): pm_log 규약상 조용 PM 은 기입되지 않으므로 '다음 PM' 은 실질적으로
    **다음 요란 PM**이다. 조용 PM 이 기입되기 시작하면 이 함수는 자동으로 그 값을 쓴다
    (엔트리를 종류 구분 없이 경계로 쓰기 때문).

    Args:
        entries: `parse_pm_entries_full` 산출.
        until: 이 시각 이후에 끝나는 레짐은 미완결로 본다 (기본: 제한 없음).

    Returns:
        (t0, t1) 튜플 리스트. 요란 PM 이 없거나 전부 진행 중이면 빈 리스트.
    """
    out = []
    lim = naive_utc(until) if until is not None else None     # tz 혼재 방어 (헌법 7장)
    for i, e in enumerate(entries):
        if e["verdict"] != "loud" or i + 1 >= len(entries):
            continue                                   # 조용 PM 또는 진행 중(다음 PM 없음)
        t0, t1 = naive_utc(e["date"]), naive_utc(entries[i + 1]["date"])
        if lim is not None and t1 > lim:
            continue
        out.append((t0, t1))
    return out


# ──────────────────────────────────────────────────────────────────────
# 피처 빌드 (raw 트레이스 → 웨이퍼 1행)  — v13 build_fdc_pool / v12 메타와 동일 스킴
# ──────────────────────────────────────────────────────────────────────
def _lean_sensor_cols(lean):
    """lean-85 중 센서 집계 컬럼(Cxx_stat_stepN)과 그 원천 센서 목록."""
    sens_cols = [c for c in lean if re.match(r"C\d+_(mean|std|max|min|last)_step\d+$", c)]
    sensors = sorted({sensor_of(c) for c in sens_cols}, key=lambda s: int(s[1:]))
    return sens_cols, sensors


def _fdc_aggregate(raw: pd.DataFrame, sensors, expected_cols):
    """전역 C40(datetime 파싱) 정렬 → groupby(C64,C7) 5통계 → pivot → 기대 컬럼 reindex.
    (정렬·집계 스킴은 v13 build_fdc_pool.py 와 동일 — 'last'=시간순 마지막 유효값.
     ⚠️ 문자열 정렬 금지: %f 자릿수 차이로 순서가 틀어질 수 있어 반드시 파싱 후 정렬)"""
    df = raw.copy()
    df["_ts_sort"] = pd.to_datetime(df[TIME_COL], format=FMT)   # 동결본과 동일: 엄격 파싱
    df = df.sort_values("_ts_sort").reset_index(drop=True)
    for s in sensors:
        df[s] = pd.to_numeric(df[s], errors="coerce")
    agg = df.groupby([ID_COL, STEP_COL])[sensors].agg(AGG_FUNCS)
    agg.columns = ["_".join(c) for c in agg.columns]
    wide = agg.reset_index().pivot(index=ID_COL, columns=STEP_COL)
    wide.columns = [f"{c0}_step{int(c1)}" for c0, c1 in wide.columns]
    # 신규 배치에 특정 Step 이 없으면 해당 컬럼 자체가 사라짐 → NaN 으로 복원(XGB 자체 처리)
    wide = wide.reindex(columns=expected_cols)
    return wide.reset_index()


def _meta_features(raw: pd.DataFrame, pm_log):
    """core 메타 8종 — v12 feature_engineering(2026-07-09 A안) 재현.
    is_high_regime = 요란 PM 이후 one-way 플래그 (구명 유지 — lean-85 컬럼명과 일치)."""
    df = raw.copy()
    df["_ts"] = pd.to_datetime(df[TIME_COL], format=FMT)   # 엄격 파싱(포맷 이탈 시 즉시 실패)
    wf = df.groupby(ID_COL)

    meta = wf[C33_COL].first().reset_index()
    meta["wf_ts"] = wf["_ts"].min().to_numpy()
    meta[LOT_COL] = wf[LOT_COL].first().to_numpy()          # CV/정렬 메타 — 피처 아님
    meta["hour"] = pd.Series(meta["wf_ts"]).dt.hour

    # 직전 요란 PM 탐색 — searchsorted (구: wafer마다 events 전체 선형 스캔 = O(n_wf × n_ev),
    # 리뷰 §3-3). events 는 시간 정렬돼 있고 판정식(e <= d)은 side="right" 와 동일하다.
    events = parse_pm_log(pm_log)
    wf_ts = pd.DatetimeIndex(meta["wf_ts"]).values
    if events:
        ev = pd.DatetimeIndex([e[0] for e in events]).values
        idx = np.searchsorted(ev, wf_ts, side="right") - 1
        has_pm = idx >= 0
        last_pm = np.where(has_pm, ev[np.clip(idx, 0, None)], REF_DATE.to_datetime64())
    else:
        has_pm = np.zeros(len(wf_ts), dtype=bool)
        last_pm = np.full(len(wf_ts), REF_DATE.to_datetime64())
    meta["days_since_last_pm"] = (wf_ts - last_pm) / np.timedelta64(1, "D")
    meta["is_high_regime"] = has_pm.astype(int)
    meta["high_regime_days"] = meta["days_since_last_pm"] * meta["is_high_regime"]
    meta["dslp_x_hour"] = meta["days_since_last_pm"] * meta["hour"]
    meta["hour_x_c33"] = meta["hour"] * meta[C33_COL]

    rec = wf[RECIPE_COL].first().reset_index()
    meta = meta.merge(rec, on=ID_COL)
    meta["is_special_recipe"] = (meta[RECIPE_COL] == "C6_1").astype(int)
    return meta.drop(columns=[RECIPE_COL])


def build_wafer_table(raw: pd.DataFrame, pm_log, lean) -> pd.DataFrame:
    """raw 트레이스 → 웨이퍼 1행 테이블 [C64, C20, wf_ts, lean-85 피처, (C65)].
    train(타깃 有)·신규 X(타깃 無) 공용. 인과: 웨이퍼 시점 기지값만 사용."""
    sens_cols, sensors = _lean_sensor_cols(lean)
    missing = [s for s in sensors if s not in raw.columns]
    if missing:                                   # 명시적 예외 (리뷰 §2-2 — `-O` 무력화 방지)
        raise ValueError(f"raw 에 센서 누락: {missing}")

    fdc = _fdc_aggregate(raw, sensors, sens_cols)
    meta = _meta_features(raw, pm_log)
    tbl = fdc.merge(meta, on=ID_COL, how="inner")

    if TARGET_COL in raw.columns:
        tgt = raw.groupby(ID_COL)[TARGET_COL].first().reset_index()
        tbl = tbl.merge(tgt, on=ID_COL, how="left")

    absent = [c for c in lean if c not in tbl.columns]
    if absent:
        raise ValueError(f"lean-85 컬럼 누락: {absent[:5]} (총 {len(absent)}개)")
    check_feature_contract(lean)                  # R2 누수·R10 floor 재확인 (헌법 1-3)

    order = [ID_COL, LOT_COL, "wf_ts"] + lean + ([TARGET_COL] if TARGET_COL in tbl.columns else [])
    return tbl[order].reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────
# 모델 (동결 파라미터)
# ──────────────────────────────────────────────────────────────────────
def make_model(params, rounds) -> xgb.XGBRegressor:
    p = dict(params)
    p.update(objective="reg:squarederror", tree_method="hist", device=XGB_DEVICE,
             random_state=42, n_estimators=int(rounds))
    return xgb.XGBRegressor(**p)


def fit_lean85(table: pd.DataFrame, lean, params, rounds) -> xgb.XGBRegressor:
    """lean-85 학습 (동결 파라미터). 타깃(C65) 없는 테이블은 명시적 예외."""
    if TARGET_COL not in table.columns:
        raise ValueError(f"학습에는 타깃 {TARGET_COL} 컬럼이 필요합니다")
    m = table[TARGET_COL].notna()
    model = make_model(params, rounds)
    model.fit(table.loc[m, lean], table.loc[m, TARGET_COL].to_numpy(float))
    return model


def low_confidence_flags(table: pd.DataFrame) -> pd.Series:
    """레짐-온셋 `low_confidence` 판정 (단일 소스).

    1 = 요란 PM 후 ONSET_DAYS 이내. B2′ 실증상 이 구간은 어떤 모델도 오차가 급증한다
    (온셋 주 RMSE 147/113/109) — 예측은 참고용으로만 쓴다.

    `predict_lean85` 와 실시간 consumer(`Lean85Predictor.predict_wafer`)가 이 함수를
    공유한다 — consumer 는 SHAP 기여도로 예측값을 얻어 `predict()` 를 호출하지 않으므로
    (리뷰 §3-2), 판정식이 두 벌로 갈라지지 않게 여기로 뽑았다.
    """
    return ((table["is_high_regime"] == 1) &
            (table["days_since_last_pm"] <= ONSET_DAYS)).astype(int)


def predict_lean85(model, table: pd.DataFrame, lean) -> pd.DataFrame:
    """예측 + 레짐-온셋 low_confidence 플래그 (판정식은 `low_confidence_flags`)."""
    pred = model.predict(table[lean])
    return pd.DataFrame({ID_COL: table[ID_COL], "pred_C65": pred,
                         "low_confidence": low_confidence_flags(table).to_numpy()})


def evaluate_rmse(model, table: pd.DataFrame, lean):
    """타깃 보유 테이블의 RMSE (주간 모니터링용)."""
    m = table[TARGET_COL].notna()
    rmse = _rmse(table.loc[m, TARGET_COL].to_numpy(float),
                 model.predict(table.loc[m, lean]))
    return rmse, r2_honest(rmse)


# ──────────────────────────────────────────────────────────────────────
# CT① 일간 슬라이딩 학습창 (설계 CT1_배선_설계_v2 D2 / WP-1 · 헌법 3-3 ①)
# ──────────────────────────────────────────────────────────────────────
# 산출물 폴더명은 `ct_decisions.model_version_after`·`approval_records.selected_option`
# (둘 다 VARCHAR(32), db/init.sql)에 그대로 들어간다. 초과분을 절단하면 서로 다른 후보가
# 같은 값으로 뭉개져 **어느 모델이 배포됐는지 감사에서 구분 불가**가 된다 (헌법 7장 —
# CT² 에서 실제로 발생한 사고). 이름을 짓는 시점에 센다.
STAMP_NAME_MAX = 32
# tag 고정 어휘 (설계 D7) — stamp 최장 = "lean85_"(7) + "YYYYmmdd_HHMMSS"(15) + "_"(1)
# + "manual"(6) = 29자 ≤ 32. 어휘를 열어두면 길이 상한이 무너진다.
CT1_TAGS = ("daily", "evtpm", "manual")

_CT1_DEFAULTS = {
    "ct1_window_days": 365,
    "ct1_require_complete_loud_regime": True,
    "ct1_gate_eval_days": 1,
    "ct1_promote_min_rmse_gain_pct": 3.0,
    "ct1_gate_min_train_wafers": 1000,
    "ct1_gate_min_eval_wafers": 100,
    "ct1_gc_keep_unpromoted_days": 14,     # 조립 산출물·미승격 challenger 공통 보존 기간
}


def ct1_config() -> dict:
    """`config/params.yaml` `ct:` 절의 CT① 키 로드 (정본 — 헌법 6-1·설계 §6 WP-6).

    창·주기의 **정본은 `ct.ct1_*`** 이다. `lean85.retrain_window_*` 는 참조용 잔재이며
    (설계 G10), 값이 어긋나면 `ct1_window_mismatch()` 가 경고한다.

    Returns:
        `_CT1_DEFAULTS` 를 params.yaml 값으로 덮어쓴 dict. 로드 실패 시 기본값(+경고).
    """
    cfg = dict(_CT1_DEFAULTS)
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            section = (yaml.safe_load(f) or {}).get("ct") or {}
        cfg.update({k: section[k] for k in cfg if k in section})
    except Exception as e:
        logger.warning("params.yaml[ct] 로드 실패 → CT① 기본값 사용: %s", e)
    return cfg


def ct1_window_mismatch() -> list[str]:
    """`ct.ct1_*`(정본)와 `lean85.retrain_*`(잔재) 값 불일치 목록 (설계 G10).

    오케스트레이터가 기동 시 호출해 경고한다. 두 절이 어긋난 채로 운영되면 "설정은
    365인데 코드는 240" 류의 조용한 오작동이 난다.

    Returns:
        불일치 설명 문자열 리스트 (없으면 빈 리스트).
    """
    out = []
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        ct, ln = doc.get("ct") or {}, doc.get("lean85") or {}
        pairs = [("ct1_window_days", "retrain_window_days"),
                 ("ct1_require_complete_loud_regime", "retrain_window_extend_until_high_regime")]
        for a, b in pairs:
            if a in ct and b in ln and ct[a] != ln[b]:
                out.append(f"ct.{a}={ct[a]!r} ≠ lean85.{b}={ln[b]!r}")
        if ct.get("ct1_trigger") == "daily" and ln.get("retrain_days") not in (None, 1):
            out.append(f"ct.ct1_trigger=daily ≠ lean85.retrain_days={ln.get('retrain_days')!r}")
    except Exception as e:
        out.append(f"params.yaml 교차 검사 실패: {e}")
    return out


def wafer_times(raw: pd.DataFrame) -> pd.DataFrame:
    """raw 트레이스 → `[C64, wf_ts]` (웨이퍼 최초 row 시각) — 창 절단 전용 경량 테이블.

    `build_wafer_table` 과 **같은 정의**(`_meta_features` 의 `wf_ts = groupby(C64)._ts.min()`)
    를 쓰되 센서 집계를 하지 않는다. 창 절단은 1년치 raw 를 훑으므로, 잘라내기 전에
    85피처 집계를 도는 것은 낭비다.
    """
    ts = pd.to_datetime(raw[TIME_COL], format=FMT)      # 엄격 파싱 (동결본과 동일)
    out = (pd.DataFrame({ID_COL: raw[ID_COL].to_numpy(), "wf_ts": ts.to_numpy()})
           .groupby(ID_COL, as_index=False)["wf_ts"].min())
    return out


def slice_train_window(table: pd.DataFrame, now, *, window_days, pm_events,
                       buffer_days=0, require_complete_loud_regime=True):
    """CT① 학습창 절단 → `(mask, window_meta)` (설계 D2 — 헌법 3-3 ①).

    창 = `(now − buffer − window_days, now − buffer]`. 좌측 개구간·우측 폐구간이며,
    상한 `cutoff = now − buffer` 는 champion·challenger **공통 학습 상한**이다 (D4 —
    평가창 `(cutoff, now]` 은 양쪽 모두 학습한 적이 없어야 비교가 공정하다).

    창 안의 **완결 요란 레짐**(요란 PM ~ 다음 PM)이 1개 미만이면 직전 완결 레짐 시작까지
    좌측으로 **임시 확장**한다 (요란은 대PM 의 30% — 1년 창도 34% 확률로 미포함).
    확장은 상태가 아니라 매 실행의 계산 결과다: 다음 실행에서 창이 충족되면 자동으로
    `window_days` 로 복귀한다.

    Args:
        table: `wf_ts` 컬럼을 가진 DataFrame (`wafer_times` 또는 `build_wafer_table` 산출).
        now: 실행 기준 시각 (d).
        window_days: 슬라이딩 창 길이(일) — 정본 `ct.ct1_window_days`.
        pm_events: `parse_pm_entries_full` 산출 (물리 PM 이벤트만 — 모델 출력 무관).
        buffer_days: 게이트 평가 버퍼 H (`ct.ct1_gate_eval_days`).
        require_complete_loud_regime: 확장 조항 적용 여부 (`ct.ct1_require_complete_loud_regime`).

    Returns:
        (mask, window_meta) — mask 는 `table` 인덱스에 정렬된 bool Series.
        window_meta 는 manifest·`ct_decisions.trigger_reason`·게이트 대조용 dict.

    Raises:
        ValueError: `wf_ts` 컬럼 부재 · window_days ≤ 0 · buffer_days < 0.
    """
    if "wf_ts" not in table.columns:
        raise ValueError("slice_train_window: 'wf_ts' 컬럼이 필요합니다 (wafer_times 산출 사용)")
    window_days, buffer_days = float(window_days), float(buffer_days)
    if window_days <= 0:
        raise ValueError(f"window_days 는 양수여야 합니다: {window_days}")
    if buffer_days < 0:
        raise ValueError(f"buffer_days 는 음수일 수 없습니다: {buffer_days}")

    # 시간축 단일화: wafer 시각·pm_log 는 naive 인데 호출자(오케스트레이터)는 aware UTC 로
    # 돈다 — 경계에서 정규화하지 않으면 아래 비교가 통째로 TypeError 다 (헌법 7장).
    now = naive_utc(now)
    cutoff = now - pd.Timedelta(days=buffer_days)
    start_base = cutoff - pd.Timedelta(days=window_days)

    regimes_all = complete_loud_regimes(pm_events, until=cutoff)
    in_window = [r for r in regimes_all if r[0] >= start_base]
    start, extended, regime_used, extend_failed = start_base, False, None, False
    if require_complete_loud_regime and len(in_window) < 1:
        if regimes_all:                                  # 가장 최근 완결 레짐까지 좌측 확장
            regime_used = regimes_all[-1]
            start = min(start_base, regime_used[0])
            extended = start < start_base
        else:
            extend_failed = True                         # 완결 레짐 자체가 없음 = 워밍업 구간

    ts = pd.to_datetime(table["wf_ts"])
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)                          # 원천이 aware 여도 정규화
    mask = (ts > start) & (ts <= cutoff)
    meta = {
        "now": now.isoformat(),
        "cutoff": cutoff.isoformat(),
        "start": pd.Timestamp(start).isoformat(),
        "start_base": start_base.isoformat(),
        "window_days": window_days,
        "buffer_days": buffer_days,
        "effective_window_days": round((cutoff - pd.Timestamp(start)) / pd.Timedelta(days=1), 3),
        "require_complete_loud_regime": bool(require_complete_loud_regime),
        "extended": bool(extended),
        "extended_days": round((start_base - pd.Timestamp(start)) / pd.Timedelta(days=1), 3),
        "extend_failed": bool(extend_failed),
        "complete_loud_regimes_in_window": len(in_window),
        "regime_used": [str(regime_used[0]), str(regime_used[1])] if regime_used else None,
        "pm_events_n": len(pm_events),
        "n_wafers": int(mask.sum()),
    }
    return mask, meta


def assert_window_no_leakage(train_ts, cutoff) -> None:
    """학습셋 최대 시각 ≤ 컷오프 검증 (헌법 1-3 — 미래 정보 차단).

    ⚠️ `assert` 가 아니라 명시적 `raise` 다 — 누수 방어선이 `python -O` 플래그 하나로
    사라지면 안 된다 (헌법 7장).

    Raises:
        ValueError: 컷오프 이후 시각의 학습 wafer 가 1장이라도 존재할 때.
    """
    ts = pd.to_datetime(pd.Series(list(train_ts)))
    if ts.empty:
        return
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(None)
    cutoff = naive_utc(cutoff)
    over = ts[ts > cutoff]
    if len(over):
        raise ValueError(
            f"헌법 1-3 위반(창 누수): 학습셋에 컷오프({cutoff}) 이후 wafer {len(over)}장 "
            f"— 최대 {over.max()}")


def assert_disjoint_wafers(train_ids, eval_ids) -> None:
    """학습·평가 wafer 교집합 0 검증 (헌법 1-3 — C64 그룹 분할 강제).

    시간 경계로 가르면 한 wafer 가 양쪽에 걸칠 수 없지만, 조립 경로가 바뀌어도 그
    불변식이 유지되는지를 **코드로** 확인한다.

    Raises:
        ValueError: 교집합이 1장이라도 있을 때.
    """
    dup = set(map(str, train_ids)) & set(map(str, eval_ids))
    if dup:
        sample = sorted(dup)[:5]
        raise ValueError(
            f"헌법 1-3 위반(그룹 분할): 학습·평가 wafer 교집합 {len(dup)}장 — 예: {sample}")


# ──────────────────────────────────────────────────────────────────────
# 재학습 (핵심 SOP) — 전체 누적 데이터로 재적합 → 버전 폴더에 저장
# ──────────────────────────────────────────────────────────────────────
def _default_store_dir() -> Path:
    """재학습 산출물 기본 저장 위치 (헌법 4-1·6-4): config lean85.model_store_dir → REPO_ROOT 상대."""
    rel = "models/c65_predictor/v2_lean85"
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            v = ((yaml.safe_load(f) or {}).get("lean85") or {}).get("model_store_dir")
        if v:
            rel = v
    except Exception as e:
        logger.warning("params.yaml model_store_dir 로드 실패 → 기본값: %s", e)
    return REPO_ROOT / rel


def default_store_dir() -> Path:
    """재학습 산출물 기본 저장 위치 (공개 접근자 — 타 모듈은 `_default_store_dir` 대신 이것)."""
    return _default_store_dir()


def retrain(raw_all: pd.DataFrame, pm_log, out_dir=None, tag: str | None = None,
            lean=None, params=None, rounds=None, return_table: bool = False,
            window_meta: dict | None = None):
    """누적 raw 전체 + 최신 pm_log 로 lean-85 재적합 후 버전 저장.

    저장 위치: out_dir=None → config canonical(`models/c65_predictor/v2_lean85`, 4-1).
      명시적 상대경로(예: 노트북 "models")는 PKG_DIR 스크래치 유지(재현 실험용).

    Args:
        return_table: True 면 빌드한 웨이퍼 테이블을 함께 반환한다. 수용검사(`--acceptance`)
            처럼 같은 raw 로 다시 `build_wafer_table` 을 부르는 호출자가 **누적 raw 전체
            집계를 두 번** 하지 않게 하려는 것 (리뷰 §3-4). 기본 False = 기존 3-튜플 유지
            (노트북 등 기존 호출부 호환).
        window_meta: CT① 조립기가 낸 `slice_train_window` 메타. manifest 에 그대로 실어
            게이트(B군 창 검증)와 `ct_decisions` 가 **같은 창 근거**를 보게 한다.
            None(수동 재학습)이면 manifest 에 기록하지 않는다.

    반환: (버전 폴더 Path, 학습된 모델, manifest dict) — `return_table=True` 면 + table
    산출: lean85_model.json (XGB 네이티브 포맷) + manifest.json (재현 메타)
    """
    if lean is None:
        lean, params, rounds = load_frozen()
    table = build_wafer_table(raw_all, pm_log, lean)
    model = fit_lean85(table, lean, params, rounds)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if out_dir is None:                       # 기본: canonical (models/c65_predictor/v2_lean85)
        base = _default_store_dir()
    elif os.path.isabs(str(out_dir)):
        base = Path(out_dir)
    else:                                     # 명시적 상대경로(노트북 "models") = PKG_DIR 스크래치 보존
        base = PKG_DIR / out_dir
    name = f"lean85_{stamp}" + (f"_{tag}" if tag else "")
    if len(name) > STAMP_NAME_MAX:                 # 감사 컬럼 폭 선제 검사 (설계 D7·헌법 7장)
        raise ValueError(
            f"stamp 폴더명 {len(name)}자 > {STAMP_NAME_MAX}: '{name}'. "
            f"`ct_decisions.model_version_after`·`approval_records.selected_option` 이 "
            f"VARCHAR(32) 라 절단되면 후보 구분이 감사에서 불가능해진다. "
            f"tag 를 줄이세요 (CT① 고정 어휘: {CT1_TAGS})")
    vdir = base / name
    # 헌법 4-1 "덮어쓰지 않는다": stamp 는 초 단위라 같은 초의 두 실행이 한 폴더를
    # 공유할 수 있다. exist_ok=False 로 충돌을 드러내고 접미로 회피한다.
    base.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "b", "c", "d"):
        try:
            cand = base / (name + suffix)
            if len(cand.name) > STAMP_NAME_MAX:
                raise ValueError(f"충돌 회피 접미로 {STAMP_NAME_MAX}자 초과: '{cand.name}'")
            cand.mkdir(parents=False, exist_ok=False)
            vdir = cand
            break
        except FileExistsError:
            logger.warning("stamp 폴더 충돌 — 접미 회피 시도: %s", cand.name)
    else:
        raise FileExistsError(f"stamp 폴더 충돌 회피 실패: {vdir} (같은 초에 4회 재학습?)")
    model.save_model(str(vdir / "lean85_model.json"))

    events = parse_pm_log(pm_log)
    m = table[TARGET_COL].notna()
    manifest = {
        "package": "handoff_lean85 v1.0",
        "model": "lean-85 XGBoost (동결 파라미터, v13 M5)",
        "created_at_local": datetime.now().isoformat(timespec="seconds"),
        "n_wafers_train": int(m.sum()),
        "train_period": [str(table.loc[m, "wf_ts"].min()), str(table.loc[m, "wf_ts"].max())],
        "pm_log_dates": [str(d.date()) for d, _ in events],
        "features_n": len(lean),
        "features_sha1": hashlib.sha1(",".join(lean).encode()).hexdigest()[:12],
        "n_estimators": int(rounds),
        "params": params,
        "onset_days_flag": ONSET_DAYS,
        "env": {"python": platform.python_version(), "xgboost": xgb.__version__,
                "pandas": pd.__version__, "numpy": np.__version__},
        "benchmark": {"time_axis_pooled_rmse": BENCH_POOLED, "honest_R2": 0.8545,
                      "lot_cv_seed_mean": 66.956,
                      "note": "시간축 수치만 현업 인용 (REPORT_01 §5)"},
    }
    if window_meta:                                    # CT① 창 근거 (게이트 B군 대조 소스)
        manifest["ct1_window_meta"] = window_meta
    _write_json(manifest, vdir / "manifest.json")      # with-open (리뷰 §2-4)
    return (vdir, model, manifest, table) if return_table else (vdir, model, manifest)


def load_model(version_dir) -> tuple[xgb.XGBRegressor, dict]:
    """버전 폴더 → (XGB 모델, manifest). 네이티브 json 로더 (헌법 3-3)."""
    vdir = Path(version_dir)
    model = xgb.XGBRegressor()
    model.load_model(str(vdir / "lean85_model.json"))
    manifest = _read_json(vdir / "manifest.json")
    return model, manifest


def should_retrain(manifest: dict, pm_log, now=None):
    """재학습 트리거 판정 → (bool, 사유 리스트).
    ① 정기: 마지막 학습 후 RETRAIN_DAYS 경과   ② 이벤트: 신규 요란 PM 기입."""
    now = pd.Timestamp.now() if now is None else pd.Timestamp(now)
    reasons = []
    last = pd.Timestamp(manifest["created_at_local"])
    if (now - last).total_seconds() >= RETRAIN_DAYS * 86400:
        reasons.append(f"정기: 마지막 재학습 후 {RETRAIN_DAYS}일 경과")
    known = set(manifest.get("pm_log_dates", []))
    new_pm = [str(d.date()) for d, _ in parse_pm_log(pm_log) if str(d.date()) not in known]
    if new_pm:
        reasons.append(f"이벤트: 신규 요란 PM 기입 {new_pm} → 즉시 재학습")
    return bool(reasons), reasons


# ──────────────────────────────────────────────────────────────────────
# 모니터링 — 입력 PSI 드리프트
# ──────────────────────────────────────────────────────────────────────
def psi(expected, actual, bins=10):
    """Population Stability Index. 관례: <0.1 안정 / 0.1~0.25 주의 / ≥0.25 경보."""
    e = np.asarray(expected, float); a = np.asarray(actual, float)
    e, a = e[np.isfinite(e)], a[np.isfinite(a)]
    if len(e) < 10 or len(a) < 10:
        return np.nan
    qs = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    if len(qs) < 3:
        return np.nan
    qs[0], qs[-1] = -np.inf, np.inf
    pe = np.histogram(e, qs)[0] / len(e)
    pa = np.histogram(a, qs)[0] / len(a)
    pe, pa = np.clip(pe, 1e-6, None), np.clip(pa, 1e-6, None)
    return float(np.sum((pa - pe) * np.log(pa / pe)))


def drift_report(ref_table: pd.DataFrame, new_table: pd.DataFrame, lean, top=15):
    """피처별 PSI 상위 top — 재학습 보조 트리거·원인 추적용."""
    rows = [{"feature": c, "psi": psi(ref_table[c], new_table[c])} for c in lean]
    rep = pd.DataFrame(rows).sort_values("psi", ascending=False).reset_index(drop=True)
    rep["level"] = np.select([rep["psi"] >= PSI_ALERT, rep["psi"] >= PSI_WARN],
                             ["경보", "주의"], default="안정")
    return rep.head(top)


# ──────────────────────────────────────────────────────────────────────
# 수용 검사 — B2′ 롤링-재학습 재현 (환경/코드 변경 후 1회 실행 권장)
# ──────────────────────────────────────────────────────────────────────
def walkforward_acceptance(table: pd.DataFrame, lean, params, rounds,
                           init_weeks=4, h_days=7, min_test=30, verbose=True):
    """B2′ 프로토콜(동결) lean-85 arm 재현: 확장창 · Lot 시간정렬 · 주간 재학습.
    기대 pooled = 99.840 ± 0.5 (R6: 공식 판정은 사용자 로컬 venv)."""
    df = table.copy()
    lot_ts = df.groupby(LOT_COL)["wf_ts"].transform("median")
    df["lot_ts"] = lot_ts
    y = df[TARGET_COL].to_numpy(float)

    t0 = df["wf_ts"].min()
    T = t0 + pd.Timedelta(weeks=init_weeks)
    end = df["wf_ts"].max()
    H = pd.Timedelta(days=h_days)

    folds, ys, ps = [], [], []
    while T < end:
        te = ((df["lot_ts"] > T) & (df["lot_ts"] <= T + H)).to_numpy()
        if te.sum() < min_test:
            T += H
            continue
        tr = (df["lot_ts"] <= T).to_numpy()
        m = make_model(params, rounds)
        m.fit(df.loc[tr, lean], y[tr])
        p = m.predict(df.loc[te, lean])
        r = _rmse(y[te], p)
        folds.append({"cut": str(T)[:10], "n": int(te.sum()),
                      "train_n": int(tr.sum()), "rmse": round(r, 3)})
        ys.append(y[te]); ps.append(p)
        if verbose:
            logger.info("  %s  test %4d | train %5d | RMSE %7.2f",
                        str(T)[:10], int(te.sum()), int(tr.sum()), r)
        T += H

    if not ys:                                   # 진단 가능한 예외 (리뷰 §2-3)
        raise ValueError(
            f"유효 fold 0개 — 데이터 기간이 짧거나 min_test({min_test})가 큽니다. "
            f"(기간 {t0.date()}~{end.date()}, init_weeks={init_weeks}, h_days={h_days}, "
            f"n_wafers={len(df)})")
    pooled = _rmse(np.concatenate(ys), np.concatenate(ps))
    out = {"pooled_rmse": round(pooled, 3), "R2_honest": r2_honest(pooled),
           "n_folds": len(folds), "folds": folds,
           "benchmark": BENCH_POOLED, "delta": round(pooled - BENCH_POOLED, 3),
           "pass": bool(abs(pooled - BENCH_POOLED) <= BENCH_TOL)}
    if verbose:
        logger.info("pooled %.3f (기준 %s±%s) → %s", pooled, BENCH_POOLED, BENCH_TOL,
                    "✅ 수용" if out["pass"] else "❌ 기준 이탈 — 환경/코드 점검")
    return out
