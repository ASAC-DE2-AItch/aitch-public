# -*- coding: utf-8 -*-
"""CONFIG.py — 재보정 주기 실험 상수 (설계서 §4 "실험 상수는 스크립트 상단 CONFIG 정의", 헌법 6-1).

이 실험은 **오프라인 백테스트**다. 운영 CT 경로(`ct.*` params.yaml)와 무관하며,
산출 모델은 실험 디렉토리에만 저장한다 — `models/` 등재·CHANGELOG 기록 금지
(설계서 §4 말미, 헌법 3-3 은 운영 재학습에만 해당).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# ── 경로 ──────────────────────────────────────────────────────────────
EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[1]
LEAN85_DIR = REPO_ROOT / "src" / "agent_a_mlops" / "lean85"
SYNTH_DIR = REPO_ROOT / "Data" / "synthetic_1y"
FULL_YEAR_GZ = SYNTH_DIR / "full_year_2018-12-01_2019-11-30.csv.gz"
PM_LEDGER = SYNTH_DIR / "pm_ledger.csv"
EVENTS_LEDGER = SYNTH_DIR / "events_ledger.csv"

OUT_DIR = EXP_DIR / "out"                 # 산출물 (대용량 — .git/info/exclude 대상)
TABLE_PARQUET = OUT_DIR / "wafer_table_1y.parquet"
WAFER_DAY_MAP = OUT_DIR / "wafer_day_map.csv"
PM_LOG_EXP = OUT_DIR / "pm_log_exp.json"  # 실험용 pm_log (요란 PM = major only)
# 런별 산출물(predictions/manifest/fits/gate_sim)은 OUT_DIR/run_<tag>/ 아래 — 스크립트가 tag 로 조립한다

# ── 기간 (설계서 §2·§4) ───────────────────────────────────────────────
INIT_TRAIN_END = pd.Timestamp("2019-02-08")     # 초기 모델 M0 학습 상한 (실측 구간 끝)
EVAL_START = pd.Timestamp("2019-02-09")         # 평가 시작일
EVAL_END = pd.Timestamp("2019-11-30")           # 평가 종료일 (설계서 명시 1년 경계)

# ── 실험군 (설계서 §3) ────────────────────────────────────────────────
ARMS = ("ARM-D1", "ARM-D7", "ARM-STATIC")
D7_PERIOD_DAYS = 7                              # ARM-D7 재학습 그리드 (평가 시작일 기준)

# ── 공통 통제 (3군 동일 — "주기 하나만 조작 변인") ────────────────────
SEED = 42
SIGMA_C65 = 261.7                               # honest R² 분모 (lean85_pipeline.SIGMA_C65 동일값 — 보고서 표기용)
TRAIN_WINDOW_DAYS = 365                        # min(가용 이력, 365일). 데이터가 1년이라 expanding ≡ 365 sliding
BOUNDARY_BUFFER_DAYS = 0                        # §5 민감도 런에서 1 로 올림 (경계 ±1일 wafer 학습 제외)

# ── 게이트 (설계서 §3 말미: 주 분석에서 제외 = 무조건 교체) ───────────
PROMOTE_MIN_RMSE_GAIN_PCT = 3.0                 # §8-A 부속 분석에서만 소급 적용

# ── 지표 구간 정의 (설계서 §6 M2) ─────────────────────────────────────
ONSET_MAJOR_DAYS = 14                           # major PM 후 14일 = 레짐 온셋
ONSET_MINOR_DAYS = 3                            # minor PM 후 3일
WARMUP_DAYS = 7                                 # §9 이음새 단차 — 평가 첫 7일 별도 표기

# ── 통계 (설계서 §6) ──────────────────────────────────────────────────
ALPHA = 0.05
BOOTSTRAP_N = 5000
BOOTSTRAP_BLOCK_DAYS = 7                        # moving-block bootstrap 블록 길이 (자기상관 반영)
SEED_SENSITIVITY = (42, 43, 44)                 # 부속 런 (동결 seed 42 가 정본)

# ── 판정 임계 (설계서 §7 사전 등록) ───────────────────────────────────
# 2026-08-04 멘토 보고에서 R1 임계 2.0% **확정**. 판정도 R1(일간 유지)로 확정됐고
# 하이브리드(R3) 검토는 종결됐다. 값을 바꾸면 판정이 바뀌므로 임의 수정 금지 —
# 변경은 PM·멘토 재확정 사항이다.
R1_MIN_REL_GAIN_PCT = 2.0                       # ✅확정 (2026-08-04 멘토) D1 vs D7 pooled RMSE 상대 개선
R3_ONSET_MIN_REL_GAIN_PCT = 5.0                 # 온셋 구간 한정 D1 우위 — R3 미발동으로 종결, 참고 보존

# ── 확정 사항 (멘토 보고 결과 — 보고서 상태 전환에 쓰인다) ────────────
# None 이면 보고서는 "결과 초안 (PM 리뷰 대기)" 상태로 나온다.
DECISION = {
    "date": "2026-08-04",
    "by": "멘토 보고",
    "verdict": "R1 — 재학습 주기 일간(daily) 확정",
    "threshold_confirmed": True,          # R1 임계 2.0% 확정 (✱ 해제)
    "followups_closed": ["하이브리드(R3) 검토 — 일간으로 닫음",
                         "minor PM 트리거 확장 — 종결"],
    "applied": ["config/params.yaml `ct.ct1_trigger: daily` (값 불변, 실증 근거 주석 추가)",
                "CONFIG.R1_MIN_REL_GAIN_PCT 2.0 — ✱ 해제"],
}

# ── 데이터 계약 (설계서 §2 실측 집계 — 불일치 시 즉시 실패) ───────────
EXPECT_WAFERS = 80294
EXPECT_ROWS = 834036
