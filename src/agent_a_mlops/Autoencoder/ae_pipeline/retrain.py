"""
retrain.py — CT② 전체 재학습 파이프라인 (계획 §10-4) + CLI.

트리거: 새 레짐 승인(대PM 등) 시 Qual 통과 후 재학습.

절차 (원 노트북 P6-5의 정상화 데모를 운영 CT②로 일반화):
  ① 새 정상셋 선택(레짐/필터/wafer 목록)
  ② dedup 가드 + split 드랍 → 피처 추출(D=83)
  ③ 시간순 TRAIN/VAL 80/20 (C64 그룹, 불가침 2·3)
  ④ QuantileTransformer(normal) **fit=TRAIN만** 재fit
  ⑤ MLP-AE 재학습 (동일 config)
  ⑥ 스코어러(μ·sd·조건화 정밀도 P) 재산출
  ⑦ calib 앵커 재산출(새 VAL 분포) · drift baseline B0 재산출
  ⑧ 버전 태깅 + 번들 통째 저장 (model·scaler·scorer·calib·drift·feature_spec·manifest)

주의: 재학습 = 세트 교체(버전 불일치 금지). seg1↔seg2 역방향 score 상승은
레짐 전환의 대칭성(정상 동작).

입력 스키마 2종 (WP-1 / 설계 v2 §3 — 2026-07-30):
  merged  원 오프라인 데이터셋(`merged_data_v3.parquet`) — C10·C46·C33·C6 전부 존재.
  ct2     운영 CT² 학습셋(`scripts/ct2_assemble_trainset.py` 산출 CSV). Kafka 되감기
          경로는 `_ts`만 있고 **C10·C46이 없다** → `adapt_ct2_trainset`이 보정한다.
  둘의 판별은 `--input-schema auto`(기본)가 컬럼 존재로 수행하고, 적용한 보정 내역은
  **manifest `input_adaptation`에 기록**한다(조용한 변환 금지 — 감사 추적).

CLI 예:
  # 원 데이터셋 (레짐·정상 필터 사용)
  python -m ae_pipeline.retrain \
    --data ../Data/merged_data_v3.parquet --out ./bundle_seg2 \
    --regime seg2 --normal-col C6 --normal-value C6_0 \
    --model-version z16_rev3_seg2 --calib-version calib_v2

  # 운영 CT² (조립 CSV = 이미 소급 필터 통과분 전체 → 필터 인자 없음)
  python -m ae_pipeline.retrain \
    --data ../../../../Data/ct2/ct2_trainset_SIM_CH_1_20260730_120000.csv \
    --out ../../../../models/anomaly_ae/ae_ct2_SIM_CH_1_20260730_120000 \
    --model-version z16_rev3_ct2 --calib-version calib_v2
"""
from __future__ import annotations

import argparse
import logging
import platform
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .constants import (STR_COLS, CORRUPT_COL, SEED, seed_all,
                        feat_weight_vector, sha1_8)
from .features import prepare_frame, find_regime_boundary, extract_features, build_feature_spec
# .model 은 torch 를 module-level 로 import 한다 → 여기서 끌어오면 입력 어댑터·세트 선택 같은
# 순수 pandas 로직조차 torch 없이는 import 불가해진다 (테스트·오케스트레이터 부담). run_ct2
# 안에서 지연 import 한다 (infer.py 가 load_data·select_normal_wafers 만 쓰는 경로도 가벼워짐).
from .calibration import fit_calibration, apply_calibration, false_alarm_rate
from .drift import fit_drift_baseline
from . import io_bundle

log = logging.getLogger("ae.retrain")   # 헌법 6-1: print() 금지 — logging 사용

L5_FALSE_ALARM_MAX = 0.02      # DoD5 오경보 상한 (REPORT_05 실측 1.53%) — 헌법 6-1 매직넘버 금지
MIN_NORMAL_WAFERS = 20         # 재학습 가능 최소 정상셋 크기
# VAL 을 절반으로 갈라 앞 = calib·scorer 적합, 뒤 = 게이트 홀드아웃(미적합)으로 쓴다.
# 0.5 인 이유: 게이트의 L5 상한이 2% 라 홀드아웃이 작으면 임계가 **측정 불가**해진다
# (50장이면 오경보 1건 = 2%, 2건 = 4% — 해상도가 임계보다 거칠다). `ct2_min_train_wafers`
# 1000 기준 800/100/100 이 되어 양쪽 다 게이트 하한(100)을 만족한다. 세 값
# (min_train_wafers · val_frac · gate_holdout_frac)은 **함께 조정**해야 한다 — WP-4 스윕 항목.
GATE_HOLDOUT_FRAC = 0.5
# 홀드아웃 최소 크기. 미달이면 홀드아웃을 떼지 않는다. 게이트의 `ct2_gate_min_gate_wafers`
# 와 **같은 값이어야 한다** (아래 MIN_CALIB_FIT_WAFERS ↔ ct2_gate_min_val_wafers 와 같은 짝).
# 구 30 은 두 가지로 나빴다: ⓐ frac<0.5 스윕에서 홀드아웃 30~99 장짜리 번들이 만들어져,
# 조립·학습(최대 2h)을 다 태운 뒤 게이트 D1 에서 떨어진다 — 게다가 리포트가 "표본 하한
# 미달"을 가리켜 원인(carve 설정)이 아니라 정상셋 부족으로 오진된다. ⓑ 그 크기에서는
# 오경보 1건이 3.3% 라 L5(2%)가 비율이 아니라 **이진 판정**이 된다(측정 불가능한 표본).
# 하한 미달 경로는 진단이 훨씬 낫다(아래 경고) — 애초에 그쪽으로 보낸다.
MIN_GATE_HOLDOUT_WAFERS = 100
# 홀드아웃을 뗀 뒤 남는 calib·scorer 적합 표본의 하한. 게이트의 `ct2_gate_min_val_wafers`
# 와 같은 값이어야 한다 — 여기서 20(MIN_NORMAL_WAFERS)만 요구하면 학습은 통과하지만
# 게이트 D4가 매번 FAIL 하는 조합이 만들어진다 (앵커 분위수 불안정).
MIN_CALIB_FIT_WAFERS = 100
# 챔버 **하나당** 홀드아웃 하한 (WP-E 중간 완화안, 2026-08-05). 게이트의
# `ct2_gate_chamber_min_wafers` 와 같은 값이어야 한다 — 이 미만인 챔버는 B5(챔버별 L5)
# 판정에서 제외되므로, "챔버별로 조용한가"가 그 챔버에 대해서는 검증되지 않는다.
# 총계 하한과 달리 **경고**다: 챔버 자체가 작으면(정상 30장) 구조적으로 못 채우는데
# 그걸 raise 로 막으면 재학습이 통째로 불가능해진다. 대신 어느 챔버가 미검증인지 이름을 남긴다.
MIN_CHAMBER_GATE_WAFERS = 20

# ── CT² 입력 어댑터 상수 (WP-1) ────────────────────────────────────────────
CT2_TS_COL = "_ts"             # 조립 스크립트가 남기는 수신 시각 컬럼 (fdc.raw timestamp)
SORT_TIME_COL = "C10"          # features.standard_sort 가 요구하는 시간축
SEQ_IN_STEP_COL = "C46"        # dedup 키 (C64,C7,C46) 구성원 — 스텝 내 순번
VAL_WAFERS_SIDECAR = "val_wafers.json"   # 번들 옆 사이드카 (7파일 세트 아님 — 검증 입력)


# ── 데이터 로딩 ────────────────────────────────────────────────────────────
def load_data(path) -> pd.DataFrame:
    """parquet/csv 로딩 (문자열 컬럼 dtype 고정)."""
    path = Path(path)
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    dtypes = {c: "string" for c in STR_COLS}
    dtypes[CORRUPT_COL] = "string"
    return pd.read_csv(path, dtype=dtypes, low_memory=False)


def detect_input_schema(df: pd.DataFrame) -> str:
    """입력 스키마 판별 — 'merged' | 'ct2'.

    판별 기준: 시간축(C10)과 스텝 순번(C46)이 **둘 다** 있으면 merged(보정 불요).
    하나라도 없으면 ct2 경로로 보고 어댑터를 태운다 (조립 CSV·Kafka 되감기 산출).
    """
    has_time = SORT_TIME_COL in df.columns
    has_seq = SEQ_IN_STEP_COL in df.columns
    return "merged" if (has_time and has_seq) else "ct2"


def adapt_ct2_trainset(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """CT² 학습셋 CSV → retrain 입력 스키마 보정. (df, 적용 내역) 반환.

    보정 2종 (설계 v2 §3 · 조립 스크립트는 PM 소유라 어댑터는 retrain 쪽에 둔다):
      ① `_ts` → `C10`  — `features.standard_sort` 의 시간축. datetime 파싱.
      ② `C46` 합성      — 스텝 내 순번. `(C64, C7)` 그룹을 C10 안정정렬한 뒤 cumcount.

    ⚠️ `C46` 합성의 부작용을 명시한다: dedup 키가 `(C64,C7,C46)` 이므로 순번을 새로
    부여하면 **머지 아티팩트 dedup(불가침 11)이 사실상 무력화**된다. Kafka 되감기
    산출물에는 오프라인 머지가 없어 중복 자체가 발생하지 않으므로 의미 손실은 없지만,
    "가드가 통과했다"가 아니라 "가드가 적용 대상이 없었다"는 사실을 manifest에 남긴다.

    Raises:
        ValueError: 시간축 후보(`C10`·`_ts`)가 둘 다 없으면 정렬 불가 — 조용한 진행 금지.
    """
    out = df.copy()
    applied: dict = {"schema": "ct2", "mapped": [], "synthesized": [], "notes": []}

    # ① 시간축
    if SORT_TIME_COL not in out.columns:
        if CT2_TS_COL not in out.columns:
            raise ValueError(
                f"시간축 컬럼이 없습니다: {SORT_TIME_COL}·{CT2_TS_COL} 둘 다 부재. "
                "조립 산출물이 아닌 CSV이거나 컬럼이 유실됐습니다 (정렬 불가 — 중단).")
        # format="ISO8601" — 조립 스크립트가 `datetime.isoformat()` 로 쓰므로 형식이 고정이다.
        # 미지정 시 pandas 가 행마다 dateutil 폴백을 시도해 경고 + 저속 (pandas>=2.0 요구).
        out[SORT_TIME_COL] = pd.to_datetime(out[CT2_TS_COL], errors="coerce", utc=True,
                                            format="ISO8601")
        n_bad = int(out[SORT_TIME_COL].isna().sum())
        if n_bad:
            out = out[out[SORT_TIME_COL].notna()].copy()      # 헌법 6-2 준용 — 행 스킵 + 기록
            applied["notes"].append(f"{CT2_TS_COL} 파싱 실패 {n_bad}행 스킵")
        # tz-aware → naive (merged 경로와 dtype 정합. 정렬 전용이라 절대시각 의미 없음)
        out[SORT_TIME_COL] = out[SORT_TIME_COL].dt.tz_localize(None)
        applied["mapped"].append(f"{CT2_TS_COL}->{SORT_TIME_COL}")

    # ② 스텝 내 순번
    if SEQ_IN_STEP_COL not in out.columns:
        out = out.sort_values([SORT_TIME_COL], kind="mergesort").reset_index(drop=True)
        out[SEQ_IN_STEP_COL] = out.groupby(["C64", "C7"], sort=False).cumcount()
        applied["synthesized"].append(SEQ_IN_STEP_COL)
        applied["notes"].append(
            "C46 합성 → dedup 키가 행마다 유일해져 머지 아티팩트 가드(불가침 11)는 "
            "적용 대상 0. Kafka 되감기 산출물은 오프라인 머지가 없어 의미 손실 없음")

    if out.empty:
        raise ValueError("보정 후 행 0 — 입력을 확인하세요 (전 행 시간축 파싱 실패).")
    log.info("CT² 입력 어댑터 적용: 매핑 %s · 합성 %s%s",
             applied["mapped"] or "-", applied["synthesized"] or "-",
             " · " + " / ".join(applied["notes"]) if applied["notes"] else "")
    return out, applied


def prepare_input(df_raw: pd.DataFrame, input_schema: str = "auto") -> tuple[pd.DataFrame, dict]:
    """스키마 판별 + (필요 시) CT² 어댑터 적용. (df, 적용 내역) 반환.

    Args:
        df_raw: load_data 산출 원본 프레임.
        input_schema: 'auto'(컬럼으로 판별) | 'merged'(보정 안 함) | 'ct2'(강제 보정).
    """
    schema = detect_input_schema(df_raw) if input_schema == "auto" else input_schema
    if schema == "merged":
        missing = [c for c in (SORT_TIME_COL, SEQ_IN_STEP_COL) if c not in df_raw.columns]
        if missing:                                  # --input-schema merged 강제 오지정 방어
            raise ValueError(
                f"input_schema='merged' 인데 필수 컬럼 부재: {missing}. "
                "조립 CSV라면 --input-schema ct2 (또는 auto) 를 쓰세요.")
        return df_raw, {"schema": "merged", "mapped": [], "synthesized": [], "notes": []}
    return adapt_ct2_trainset(df_raw)


# ── 정상셋 선택 ────────────────────────────────────────────────────────────
def exclude_injected(df, wafers_ordered, labels_path) -> list:
    """정답지 라벨로 **주입 wafer 를 정상셋에서 제외** (2026-08-03 신설).

    왜 필요한가 (2026-08-02 사고): 정상 필터가 레시피 라벨 `C6_0` 하나뿐이었다. 그 라벨은
    "어떤 레시피로 돌렸는가"를 기록하지 "결과가 정상이었는가"를 기록하지 않으므로,
    센서값만 흔드는 주입은 원리적으로 걸러지지 않는다. 실측 — 정상 표기 1,907장 중
    **34%(653장)가 주입**이었고, 모델이 그것을 정상으로 학습해 검출률이 0.6% 로 무너졌다.
    동시에 calib 앵커가 이상 점수까지 포함해 부풀려져 **오경보가 실제보다 낮게 보고**됐다.

    라벨 CSV 는 `scripts/build_clean_wafer_list.py` 산출(`injected` 컬럼). 그 스크립트가
    정답지를 control 쌍 대조로 실측 검증하므로, 여기서는 라벨을 신뢰하고 적용만 한다.

    Raises:
        ValueError: 라벨 파일에 `C64`·`injected` 가 없거나, 제외 후 정상셋이 비는 경우.
            **조용한 무시 금지** — 필터가 사라진 줄 모르는 것이 이번 사고의 본질이다.
    """
    lab = pd.read_csv(labels_path)
    missing = [c for c in ("C64", "injected") if c not in lab.columns]
    if missing:
        raise ValueError(f"라벨 파일 필수 컬럼 누락 {missing}: {labels_path}")
    inj = set(lab.loc[lab["injected"].astype(bool), "C64"].astype(str))
    kept = [w for w in wafers_ordered if str(w) not in inj]
    n_drop = len(wafers_ordered) - len(kept)
    if not kept:
        raise ValueError(f"주입 제외 후 정상 wafer 0장 — 라벨/데이터 대응 확인: {labels_path}")
    if n_drop == 0:
        log.warning("주입 제외 0장 — 라벨(%s)과 데이터의 wafer id 가 어긋났을 수 있다 "
                    "(라벨 주입 %d장). 정상셋이 실제로 깨끗한지 확인하세요.", labels_path, len(inj))
    else:
        log.info("주입 제외: %d장 → 정상 %d장 (라벨 %s)", n_drop, len(kept), labels_path)
    return kept


def select_normal_wafers(df, regime=None, normal_col=None, normal_value=None,
                         wafers=None, labels_path=None) -> list:
    """새 정상 wafer 집합을 시간순으로 반환.

    우선순위: wafers(명시 목록) > regime(seg1/seg2) [+ normal_col==normal_value 필터].
    `labels_path` 가 있으면 **마지막에 주입 wafer 를 제외**한다 (`exclude_injected`).
    명시 목록 경로에도 적용된다 — 목록이 이미 깨끗하면 0장 제외로 무해하다.
    """
    if wafers:
        sub = df[df["C64"].isin(set(map(str, wafers)))]
    else:
        sub = df
        if regime in ("seg1", "seg2"):
            reg = find_regime_boundary(df)
            if "seg1_wafers" not in reg:
                raise ValueError("레짐 경계를 찾지 못함 (C33 리셋 없음). --wafers-file 사용.")
            keep = set(reg[f"{regime}_wafers"])
            sub = df[df["C64"].isin(keep)]
        if normal_col and normal_value is not None:
            sub = sub[sub[normal_col].astype(str) == str(normal_value)]
    order = sub.groupby("C64")["C10"].min().sort_values(kind="mergesort")
    ordered = order.index.tolist()
    return exclude_injected(df, ordered, labels_path) if labels_path else ordered


def time_split(wafers_ordered, val_frac=0.2):
    """시간순 정렬된 wafer → TRAIN/VAL (앞 (1-val_frac), 뒤 val_frac)."""
    cut = int(len(wafers_ordered) * (1 - val_frac))
    return wafers_ordered[:cut], wafers_ordered[cut:]


def chamber_split(df, wafers_ordered, val_frac=0.2, gate_frac=GATE_HOLDOUT_FRAC,
                  chamber_col="C24", strict=True) -> tuple[list, list, list]:
    """**챔버별** 시간순 3분할 → (TRAIN, calib적합 VAL, 게이트 홀드아웃). `split_mode="chamber"`.

    왜 필요한가 (2026-08-02): 정상셋이 챔버별로 **시간축에서 잘려 있으면** 전역 시간순
    분할이 홀드아웃을 특정 챔버로 쏠리게 한다. 실측 — 주입 제외 정상 1,254장(CH1 477·
    CH2 100·CH3 200·CH4 477)을 전역 시간순으로 자르면 게이트 홀드아웃이 **CH1·CH4만**
    남아(CH2·CH3 0장) "모든 챔버에서 조용한가"를 검증할 수 없다.

    각 챔버 안에서는 시간 방향이 보존된다(챔버별로 뒤쪽을 VAL·홀드아웃으로 뗌). 대가는
    **전역 시간 순서가 어긋난다**는 것 — 어떤 챔버의 홀드아웃이 다른 챔버의 TRAIN보다
    벽시계상 앞일 수 있다. 누수(헌법 1-3)는 아니다(타겟 무관·wafer 단위 분리 유지)지만,
    "미래 예측" 성격은 약해지므로 **기본값은 `time`** 이고 이 모드는 명시 선택이다.

    반환 3버킷은 각각 **전역 시간순으로 재정렬**한다 — 하류(drift EWMA 스트림·`reindex`)가
    시간순 입력을 전제하기 때문이다.

    Args:
        df: `prepare_frame` 후 프레임 (chamber_col·C10 필요).
        wafers_ordered: 전역 시간순 정상 wafer 목록.
        val_frac: 챔버별 VAL 비율. gate_frac: VAL 중 홀드아웃 비율.
        strict: True(기본)면 **합계 하한 미달을 `ValueError` 로 중단**한다 (2026-08-05 WP-E).
            구현이 `log.warning` 뿐이던 시절에는 조립·학습(최대 2h)을 다 태운 뒤 게이트
            D1/D4 에서 떨어졌고, 리포트가 "표본 하한 미달"을 가리켜 원인(분할 설정)이 아니라
            정상셋 부족으로 **오진**됐다. 실패는 싼 지점에서 낸다. False 는 임계 스윕
            하네스(WP-D)처럼 의도적으로 작은 n 을 도는 실험 경로 전용이다.

    Raises:
        ValueError: chamber_col 부재 (조용한 폴백 금지 — 전역 분할로 몰래 되돌아가면
            manifest 는 chamber 라고 적힌 채 실제는 time 분할이 된다).
        ValueError: `strict` 이고 홀드아웃/적합 표본 **합계**가 하한 미달.
    """
    if chamber_col not in df.columns:
        raise ValueError(f"split_mode='chamber' 인데 {chamber_col} 컬럼이 없음 — "
                         "챔버 분할 불가 (time 모드를 쓰거나 입력을 확인하세요)")
    keep = set(map(str, wafers_ordered))
    cham = (df[df["C64"].astype(str).isin(keep)]
            .groupby("C64")[chamber_col].first().astype(str))
    rank = {w: i for i, w in enumerate(wafers_ordered)}          # 전역 시간순 위치
    train, val, gate = [], [], []
    thin_chambers = {}                                           # 챔버별 홀드아웃 하한 미달분
    for ch in sorted(set(cham.values)):
        seq = [w for w in wafers_ordered if cham.get(str(w)) == ch]   # 챔버 내 시간순
        tr, va = time_split(seq, val_frac)
        # ★`_carve_gate_holdout` 을 쓰지 않는다 — 그 하한(적합 100·홀드아웃 100)은 **합계**
        # 기준이라 챔버 단위로 적용하면 작은 챔버(CH2 100장 → VAL 50)가 매번 홀드아웃을
        # 못 받고, 정확히 이 모드가 없애려던 쏠림이 재현된다. 여기서는 비율만 쓰고
        # 하한은 합계에서 한 번 검사한다(아래).
        n_hold = int(len(va) * float(gate_frac)) if 0.0 < float(gate_frac) < 1.0 else 0
        va, ga = (va, []) if n_hold == 0 else (va[:-n_hold], va[-n_hold:])
        train += tr; val += va; gate += ga
        if len(ga) < MIN_CHAMBER_GATE_WAFERS:
            thin_chambers[ch] = len(ga)
        log.info("  챔버 %s: 정상 %d → TRAIN %d / fit %d / gate %d",
                 ch, len(seq), len(tr), len(va), len(ga))
    train, val, gate = (sorted(b, key=lambda w: rank[w]) for b in (train, val, gate))

    # 합계 하한 — **명시적 raise** (`assert` 금지, 헌법 7장). 여기서 죽는 편이 2h 학습 뒤
    # 게이트에서 원인 오진과 함께 죽는 것보다 싸다.
    short = [(what, n, floor) for n, floor, what in
             ((len(gate), MIN_GATE_HOLDOUT_WAFERS, "홀드아웃"),
              (len(val), MIN_CALIB_FIT_WAFERS, "calib 적합")) if n < floor]
    if short:
        msg = ("챔버 분할 표본 하한 미달 — "
               + " / ".join(f"{w} {n}장 < {f}" for w, n, f in short)
               + f". val_frac({val_frac})·gate_holdout_frac({gate_frac}) 을 키우거나 정상셋을 "
                 "더 모으세요 (이대로 학습하면 게이트 D1/D4 가 FAIL 합니다).")
        if strict:
            raise ValueError(msg)
        log.warning("%s [strict=False — 실험 경로라 진행]", msg)
    if thin_chambers:
        # B5(챔버별 L5)가 이 챔버들을 **판정에서 제외**한다 = 그 챔버는 "조용한가"가 검증되지
        # 않는다. 총계만 보고 "전 챔버 검증됐다"고 오해하지 않도록 이름을 남긴다.
        log.warning("챔버별 홀드아웃 하한(%d) 미달 — %s. 이 챔버들은 게이트 B5 판정에서 "
                    "제외되어 오경보율이 **검증되지 않는다**.",
                    MIN_CHAMBER_GATE_WAFERS,
                    ", ".join(f"{c}={n}장" for c, n in sorted(thin_chambers.items())))
    return train, val, gate


def _carve_gate_holdout(val_ordered, frac=GATE_HOLDOUT_FRAC, quiet=False):
    """VAL → (calib·scorer 적합용, 게이트 홀드아웃). 시간순 뒤쪽을 홀드아웃으로 뗀다.

    quiet=True 는 **챔버별 호출** 전용이다 (`chamber_split`). 하한(적합 100·홀드아웃 100)은
    전체 합계 기준이라 챔버 단위로는 당연히 미달하며, 챔버마다 경고를 띄우면 진짜 신호가
    묻힌다. 합계 미달 경고는 `chamber_split` 이 한 번만 낸다.

    **왜 필요한가 (리뷰 지적 반영, 2026-07-30)**: calib 앵커는 정의상 "VAL raw의 P50·P90·
    P98.5…"이므로, **calib을 적합한 그 표본으로** 오경보율(L5)·평균·P95를 재면 결과가
    모델 품질과 무관하게 산식으로 고정된다 (L5 ≈ 1.5% · 평균 ≈ 0.05 · P95 < 0.2). 즉
    게이트 B군이 항진명제가 되어 "문지기"가 아니게 된다. 홀드아웃을 떼면 B군이 **같은
    분포의 미적합 표본**에서의 실제 오경보 측정이 된다.

    표본이 모자라 홀드아웃이 게이트 하한 미달이면 **떼지 않는다** — calib 적합 표본을
    깎는 쪽이 더 위험하다. 이 경우 사이드카 `gate`가 비고, 게이트는 그 사실을 검사
    항목으로 드러낸다(조용한 퇴화 금지).
    """
    frac = float(frac)
    if not 0.0 < frac < 1.0:
        return list(val_ordered), []
    n_hold = int(len(val_ordered) * frac)
    n_fit = len(val_ordered) - n_hold
    if n_hold < MIN_GATE_HOLDOUT_WAFERS or n_fit < MIN_CALIB_FIT_WAFERS:
        log.warning("게이트 홀드아웃 생략 — VAL %d장을 %.0f%% 로 가르면 적합 %d/홀드아웃 %d "
                    "(하한 적합 %d·홀드아웃 %d 미달). B군(정상 조용)은 calib 적합 표본에서 "
                    "측정되어 항진명제가 되고, **게이트 D3가 FAIL 하여 배포가 막힌다** — "
                    "정상셋을 더 모으거나 val_frac 을 키워야 한다.",
                    len(val_ordered), frac * 100, n_fit, n_hold,
                    MIN_CALIB_FIT_WAFERS, MIN_GATE_HOLDOUT_WAFERS)
        return list(val_ordered), []
    return list(val_ordered[:-n_hold]), list(val_ordered[-n_hold:])


def _transform(scaler, F, feat_cols):
    return scaler.transform(F.reindex(columns=feat_cols, fill_value=0.0))


# ── CT② 실행 ──────────────────────────────────────────────────────────────
def run_ct2(data_path, out_dir, regime=None, normal_col=None, normal_value=None,
            wafers=None, val_frac=0.2, seed=SEED,
            model_version="z16_rev3_seg2", calib_version="calib_v2",
            cfg_overrides=None, device=None, verbose=True,
            input_schema="auto", gate_holdout_frac=GATE_HOLDOUT_FRAC,
            split_mode="time", labels_path=None, strict_split=True) -> dict:
    """전체 CT② 재학습 → 번들 저장. summary 딕셔너리 반환.

    Args:
        gate_holdout_frac: VAL 중 게이트 홀드아웃 비율 (calib·scorer 미적합 구간).
            0 이면 홀드아웃 없음 — `_carve_gate_holdout` docstring의 항진명제 주의 참조.
        split_mode: `"time"`(기본 — 전역 시간순) 또는 `"chamber"`(챔버별 시간순).
            정상셋이 챔버별로 시간축에서 잘려 있을 때만 chamber 를 쓴다 (`chamber_split`).
        labels_path: 주입 정답지 라벨 CSV. 주면 정상셋에서 **주입 wafer 를 제외**한다
            (`exclude_injected` — 2026-08-02 학습셋 오염 사고 재발 방지).
        strict_split: `split_mode='chamber'` 의 표본 하한 미달을 예외로 중단할지
            (기본 True — `chamber_split` docstring). 임계 스윕(WP-D) 전용 탈출구.
    """
    # 인자 검증은 **torch 로드보다 먼저** — 오타 하나로 학습 환경이 없는 곳에서
    # ValueError 대신 ModuleNotFoundError 가 나면 원인이 가려진다 (지연 import 취지 정합).
    if split_mode not in ("time", "chamber"):
        raise ValueError(f"split_mode 는 'time' 또는 'chamber' — 받은 값: {split_mode!r}")

    import torch  # 지연 import
    from .model import ADOPTED_CFG, train_ae, build_scorer   # torch 의존 — 지연 import

    t_start = time.time()
    seed_all(seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ① 로딩 + 입력 스키마 보정(WP-1) + 전처리
    df_raw = load_data(data_path)
    df_raw, adaptation = prepare_input(df_raw, input_schema)
    df, n_dedup = prepare_frame(df_raw)

    # ② 정상셋 선택 + 시간순 split (+ 게이트 홀드아웃 — 아래 주석 참조)
    normal_ordered = select_normal_wafers(df, regime, normal_col, normal_value, wafers,
                                          labels_path=labels_path)
    if len(normal_ordered) < MIN_NORMAL_WAFERS:
        raise ValueError(f"정상셋이 너무 작음(n={len(normal_ordered)} < {MIN_NORMAL_WAFERS}). 재학습 불가.")
    if split_mode == "chamber":
        train_w, val_w, gate_w = chamber_split(df, normal_ordered, val_frac, gate_holdout_frac,
                                               strict=strict_split)
    else:                                        # "time" — 상단에서 검증됨
        train_w, val_w = time_split(normal_ordered, val_frac)
        val_w, gate_w = _carve_gate_holdout(val_w, gate_holdout_frac)

    # ③ 피처 추출 (TRAIN 컬럼을 스펙 기준으로)
    feats_tr = extract_features(df[df["C64"].isin(set(train_w))]).reindex(train_w).fillna(0.0)
    feat_cols = list(feats_tr.columns)
    feats_val = extract_features(df[df["C64"].isin(set(val_w))]).reindex(val_w).fillna(0.0)

    # ④ QuantileTransformer(normal) fit=TRAIN만 (불가침 3)
    from sklearn.preprocessing import QuantileTransformer
    scaler = QuantileTransformer(
        output_distribution="normal",
        n_quantiles=min(1000, len(feats_tr)),
        subsample=10 ** 9, random_state=seed,
    ).fit(feats_tr)
    X_tr = _transform(scaler, feats_tr, feat_cols)
    X_val = _transform(scaler, feats_val, feat_cols)
    w_vec = feat_weight_vector(feat_cols)

    # ⑤ MLP-AE 재학습 (동일 config)
    cfg = dict(ADOPTED_CFG)
    if cfg_overrides:
        cfg.update({k: v for k, v in cfg_overrides.items() if v is not None})
    model, val_loss, n_ep = train_ae(X_tr, X_val, w_vec, seed=seed, device=device, **cfg)

    # ⑥ 스코어러 재산출 (조건화 정밀도)
    scorer = build_scorer(model, X_val, feat_cols, w_vec, device=device)
    recon_val = scorer.recon_rmse(X_val)

    # ⑦ calib · drift 재산출
    raw_val = scorer.ae_raw(X_val)
    calib = fit_calibration(raw_val)
    sc_val = apply_calibration(raw_val, calib)           # 시간순 VAL 스트림
    drift_cfg = fit_drift_baseline(sc_val)
    l5 = false_alarm_rate(sc_val, calib["qual_threshold"])

    # ⑧ 버전 태깅 + 번들 저장
    A = io_bundle.ARTIFACT_NAMES
    torch.save(model.state_dict(), out_dir / A["model"])
    io_bundle.save_scaler(scaler, out_dir / A["scaler"])
    scorer.save_stats(out_dir / A["scorer"])
    io_bundle.save_json(calib, out_dir / A["calib"])
    io_bundle.save_json(drift_cfg, out_dir / A["drift"])
    io_bundle.save_json(build_feature_spec(feat_cols), out_dir / A["feature_spec"])

    # 검증 입력 사이드카 — validate_bundle 이 VAL 세트를 재유도 없이 정확히 복원한다.
    # 7파일 세트(io_bundle.ARTIFACT_NAMES)가 아니므로 deploy_bundle 복사 대상에서 제외되며,
    # 세트 동기(불가침 12)에 영향을 주지 않는다. 감사 정본은 manifest 의 set_hashes.
    io_bundle.save_json(
        {"val": [str(w) for w in val_w], "gate": [str(w) for w in gate_w],
         "train": [str(w) for w in train_w],
         "train_n": len(train_w),
         "val_hash": sha1_8(val_w), "gate_hash": sha1_8(gate_w) if gate_w else None,
         "train_hash": sha1_8(train_w),
         "data_path": str(data_path), "val_frac": float(val_frac),
         "gate_holdout_frac": float(gate_holdout_frac),
         "note": "validate_bundle 입력. 7파일 세트 아님(배포 미포함). "
                 "val = calib·scorer 적합 표본 / gate = 미적합 홀드아웃(B군 채점용) / "
                 "train = 게이트 E군(정답지 대조)의 학습분 제외용 — 학습에 쓰인 wafer 의 "
                 "미검출은 이미 오염의 결과라 평가 표본에서 뺀다."},
        out_dir / VAL_WAFERS_SIDECAR)

    dt = time.time() - t_start
    meta = {
        "package_version": __version__,
        "ae_model_version": model_version,
        "ae_calib_version": calib_version,
        "arch": "MLP-AE D→64→32→z16→32→64→D (GELU·LayerNorm·dropout 0.05)",
        "score_contract": "conditioned residual Mahalanobis (단일 ae_raw, 불가침 12)",
        "train_config": cfg,
        "seed": seed,
        "device": scorer.device,
        "n_features": len(feat_cols),
        "set_sizes": {"train": len(train_w), "val": len(val_w), "gate": len(gate_w),
                      "dedup_removed": n_dedup},
        "set_hashes": {"train": sha1_8(train_w), "val": sha1_8(val_w),
                       "gate": sha1_8(gate_w) if gate_w else None},
        "selection": {"regime": regime, "normal_col": normal_col,
                      "normal_value": normal_value, "explicit_wafers": bool(wafers),
                      "val_frac": float(val_frac),
                      "gate_holdout_frac": float(gate_holdout_frac),
                      "split_mode": str(split_mode),   # 사이드카 유실 시 재유도 가능여부 판정 (validate_bundle)
                      "labels_path": str(labels_path) if labels_path else None,
                      "injection_excluded": bool(labels_path)},   # 학습셋 오염 여부 감사
        "input_adaptation": adaptation,          # WP-1 — 조용한 변환 금지(감사 추적)
        "data_path": str(data_path),
        "metrics": {"val_loss": round(float(val_loss), 6), "n_epoch": int(n_ep),
                    "recon_rmse_val": round(float(recon_val), 4),
                    "L5_false_alarm": round(float(l5), 4),
                    "val_score_mean": round(float(sc_val.mean()), 4),
                    "val_score_p95": round(float(np.percentile(sc_val, 95)), 4),
                    "scorer_eff_rank": None if scorer.eff_rank is None else round(scorer.eff_rank, 3),
                    "scorer_cond_used": None if scorer.cond_used is None else round(scorer.cond_used, 3),
                    "drift_baseline_B0": round(float(drift_cfg["baseline_B0"]), 4)},
        "retrain_seconds": round(dt, 1),
        "runtime": {"python": platform.python_version()},
        "note": "재학습 = 세트 교체. model/scaler/scorer/calib/drift 버전 동기(불가침 12).",
    }
    manifest = io_bundle.write_manifest(out_dir, meta)

    if verbose:
        log.info("── CT② 재학습 완료 (%.1fs · %dep · device=%s) ──", dt, n_ep, scorer.device)
        log.info("  정상셋: TRAIN %d / VAL(적합) %d / 게이트 홀드아웃 %d (dedup -%d)",
                 len(train_w), len(val_w), len(gate_w), n_dedup)
        log.info("  VAL loss %.5f · recon RMSE %.3f · eff_rank %.2f · cond %.1f",
                 val_loss, recon_val, scorer.eff_rank, scorer.cond_used)
        log.info("  calib 앵커 raw=%s → %s",
                 [round(x, 1) for x in calib["anchors_x"]], calib["anchors_y"])
        log.info("  L5 오경보(VAL>%.1f) %.3f %s · drift B0 %.3f",
                 calib["qual_threshold"], l5,
                 "≤2% OK" if l5 <= L5_FALSE_ALARM_MAX else "★>2% 앵커 조정 검토",
                 drift_cfg["baseline_B0"])
        log.info("  번들: %s  (버전 %s / %s)", out_dir, model_version, calib_version)
        log.info("  ※ 다음: 새 정상 recon 정상화 확인 + Qual 5기준 재확인(REPORT 절차) 후 운영 승격.")
    # 헌법 3-3: 재학습 시 models/CHANGELOG.md 기록 의무 (7장 "자주 하는 실수" 등재 항목).
    # verbose 와 무관하게 항상 띄운다 — 기록 누락은 verbose 설정으로 면제되지 않는다.
    log.warning("⚠️ 잊지 말 것 — models/CHANGELOG.md 에 즉시 기록 (헌법 3-3): "
                "버전 %s | 날짜 %s | 담당 A | recon RMSE %.3f · L5 %.3f | 피처 %d개 | 변경 사유",
                model_version, time.strftime("%Y-%m-%d"), recon_val, l5, len(feat_cols))
    return {"out_dir": str(out_dir), "manifest": manifest}


# ── CLI ───────────────────────────────────────────────────────────────────
def _read_wafers_file(path):
    if not path:
        return None
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip()]


def build_argparser():
    ap = argparse.ArgumentParser(
        prog="ae_pipeline.retrain",
        description="ae_v3 AE — CT② 전체 재학습 파이프라인 (계획 §10-4)")
    ap.add_argument("--data", required=True,
                    help="merged_data_v3.parquet/.csv 또는 CT² 조립 CSV 경로")
    ap.add_argument("--out", required=True, help="번들 출력 폴더")
    ap.add_argument("--input-schema", choices=["auto", "merged", "ct2"], default="auto",
                    help="입력 스키마 (기본 auto — C10·C46 존재로 판별. ct2 = _ts→C10 매핑 + C46 합성)")
    ap.add_argument("--regime", choices=["seg1", "seg2"], default=None,
                    help="C33 리셋 기준 레짐 선택")
    ap.add_argument("--normal-col", default=None, help="정상 필터 컬럼 (예: C6)")
    ap.add_argument("--normal-value", default=None, help="정상 필터 값 (예: C6_0)")
    ap.add_argument("--wafers-file", default=None,
                    help="정상 wafer(C64) 목록 파일 (한 줄 하나) — regime보다 우선")
    ap.add_argument("--val-frac", type=float, default=0.2, help="VAL 비율 (뒤쪽 시간)")
    ap.add_argument("--gate-holdout-frac", type=float, default=GATE_HOLDOUT_FRAC,
                    help="VAL 중 게이트 홀드아웃 비율 (calib 미적합. 0=홀드아웃 없음)")
    ap.add_argument("--labels", default=None,
                    help="주입 정답지 라벨 CSV (build_clean_wafer_list.py 산출) — "
                         "정상셋에서 주입 wafer 제외. 주입 포함 데이터로 학습할 때 필수")
    ap.add_argument("--split-mode", choices=["time", "chamber"], default="time",
                    help="time=전역 시간순(기본) · chamber=챔버별 시간순 "
                         "(정상셋이 챔버별로 시간축에서 잘렸을 때)")
    ap.add_argument("--allow-thin-split", action="store_true",
                    help="chamber 분할 표본 하한 미달을 경고로만 처리 (기본은 중단). "
                         "임계 스윕(WP-D) 등 의도적 소표본 실험 전용")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--model-version", default="z16_rev3_seg2")
    ap.add_argument("--calib-version", default="calib_v2")
    ap.add_argument("--max-ep", type=int, default=None, help="학습 에폭 상한 override")
    ap.add_argument("--patience", type=int, default=None, help="조기종료 patience override")
    ap.add_argument("--device", default=None, help="cuda/cpu (기본 자동)")
    return ap


def main(argv=None):
    """CLI 진입점 — logging 구성 후 CT② 실행 (헌법 6-1: print 금지)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ [ae.retrain] %(message)s")
    args = build_argparser().parse_args(argv)
    run_ct2(
        data_path=args.data, out_dir=args.out,
        regime=args.regime, normal_col=args.normal_col, normal_value=args.normal_value,
        wafers=_read_wafers_file(args.wafers_file),
        val_frac=args.val_frac, seed=args.seed,
        model_version=args.model_version, calib_version=args.calib_version,
        cfg_overrides={"max_ep": args.max_ep, "patience": args.patience},
        device=args.device, input_schema=args.input_schema,
        gate_holdout_frac=args.gate_holdout_frac,
        split_mode=args.split_mode, labels_path=args.labels,
        strict_split=not args.allow_thin_split,
    )


if __name__ == "__main__":
    main()
