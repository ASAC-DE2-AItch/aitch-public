"""
validate_bundle.py — CT² challenger 번들 자동 검증 게이트 (WP-2 / 헌법 3-3 ③ ⓑ).

**이 모듈이 게이트 항목·임계의 단일 소스다** (헌법은 수치를 규정하지 않는다 — 위임 서술).
임계 기본값은 아래 상수이며 `config/params.yaml` `ct.ct2_gate_*` 로 덮어쓴다 (6-1).
항목 추가·임계 강화는 여기/config 변경만으로 가능하고, **항목 삭제·완화는 PM 승인 필수**.

검증 5군 (설계 v2 §2 + E군 2026-08-03 신설):
  A. 무결성   7파일 완결·manifest sha256(항목 수 포함)·세트 해시·채널가중·세트 행 실존
  B. 정상조용 recon RMSE 상한 · L5 오경보율 · calib ①(평균) · ②(P95) · **B5 챔버별 L5**
              ★**calib 미적합 홀드아웃**에서 측정한다 — 적합 표본에서 재면 앵커 산식상
              결과가 고정돼(L5≈1.5% 등) 모델 품질과 무관한 항진명제가 된다
  C. 이상검출 합성 프로브 주입 채점 — 정상 VAL 피처에 ±kσ 교란 → ae_score > qual 검출률
  D. 임계건전 표본 하한 · anchors_x 인접 간격 비율(뭉침 = 소표본 붕괴) · 홀드아웃 실존
  E. 오염검사 **E0 정답지 제공** + E1 라벨된 실제 주입 구간 검출률

프로브 격리 (불가침 2 — 평가 전용): 주입은 **TRAIN에서 분리된 VAL 행에만** 하고 결과를
어디에도 영속화하지 않는다. 학습셋에 프로브가 섞이면 미탐의 자기실현이 된다.

왜 C군이 필요한가: B·D만 보면 "전부 0점을 내는 깡통 모델"이 만점으로 통과한다. 조용함과
민감함을 **동시에** 요구해야 게이트가 문지기 역할을 한다.

**판정 3값 (2026-08-05 G0-4 — CT①`validate_lean85` 와 표기 통일)**:
  `gate_verdict` ∈ {PASS, FAIL, SKIP} 이 최종 판정이고 `gate_pass` 는 `verdict == "PASS"` 다.
  **SKIP 은 PASS 가 아니다** — "검사를 못 했다"를 "통과했다"로 환원하면, 오염이 심할수록
  평가 표본이 줄어 E1 이 SKIP 으로 도망가는 구조(리스크 K-2)가 그대로 배포로 이어진다.
  SKIP 도 배포 불가이며, `ct2_deploy_approval.validate_gate_report` 가 `gate_pass` 로 차단한다.

exit code: 0 = PASS · 2 = FAIL(게이트 판정) · 3 = SKIP(판정 불가) · 1 = 실행 오류
  (조립 스크립트 0/2 관례 + CT① `validate_lean85` 의 3=SKIP 과 정합).

CLI:
  python -m ae_pipeline.validate_bundle \
      --bundle ../../../../models/anomaly_ae/ae_ct2_SIM_CH_1_20260730_120000 \
      --data ../../../../Data/ct2/ct2_trainset_SIM_CH_1_20260730_120000.csv \
      --labels ../../../../Data/ct2/injection_labels.csv \
      --out <bundle>/validation.json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .constants import CH_SETTLE_CONT, feat_weight, feat_weight_vector, sha1_8
from .calibration import apply_calibration, false_alarm_rate
from .features import prepare_frame, extract_features
from . import io_bundle

# `.model` 은 torch module-level import → 여기서는 지연 import 한다 (validate() 안).
# 덕분에 임계 로드·검사 조립·프로브 채점 로직을 torch 없이 단위 테스트할 수 있다
# (스코어러는 `ae_raw`/`recon_rmse`/`w_vec` 만 요구하는 덕타이핑 경계).

log = logging.getLogger("ae.validate")

REPO_ROOT = Path(__file__).resolve().parents[4]      # AITCH/ (…/src/agent_a_mlops/Autoencoder/ae_pipeline)
VALIDATION_REPORT = "validation.json"               # 리포트 파일명 (7파일 세트 아님)

# ── 게이트 판정 어휘·스키마 버전 (계약 §8-E 필수 키) ──────────────────────────
VERDICT_PASS, VERDICT_FAIL, VERDICT_SKIP = "PASS", "FAIL", "SKIP"
EXIT_PASS, EXIT_ERROR, EXIT_FAIL, EXIT_SKIP = 0, 1, 2, 3    # CT① validate_lean85 와 동일 규약
# 항목 집합이 바뀔 때마다 올린다. **리포트만으로 항목 집합을 판별할 수 있게** 하는 값이다 —
# `ae_cand_clean_time/validation.json` 에는 E1 이 아예 없고(E군 신설 이전) `ae_cand_clean_chamber`
# 에는 있는데, `gate_source` 가 상수 문자열이라 둘을 구분할 방법이 없었다 (WP-D 부수 발견).
GATE_SCHEMA_VERSION = 3                              # v3 = 3밴드(verdict_band)·가중 프로브 채점
#                                                      (2026-08-09 — `CT2_게이트_기준_개정_v2`)
#   v2 = E0·B5·gate_verdict 도입 (2026-08-05)

# ── 비율 판정의 3밴드 (2026-08-09, `docs/CT2_게이트_기준_개정_v2_2026-08-09.md` §1-3) ────
# 왜 필요한가: B1(총계 오경보율)·B5(챔버별)는 **비율**인데, 홀드아웃 600장에서 오경보 1건이
# 0.17%p 라 임계 근처의 점추정 하나로는 "예산을 넘었다"와 "표본이 작아 모른다"를 구분할 수
# 없었다. e5(6.67%)처럼 확실히 나쁜 것과 r3(4.33%)처럼 경계인 것이 같은 `FAIL` 로 보였다.
#
# ★불변식: **밴드는 PASS/FAIL 판정을 바꾸지 않는다.** lo ≤ p̂ ≤ hi 이므로
#   `lo > budget` 이면 반드시 `p̂ > budget`(FAIL), `hi ≤ budget` 이면 반드시 `p̂ ≤ budget`(PASS).
#   경계 밴드는 점추정으로 판정한다. 즉 v1 대비 판정은 전 건 동일하고(문서 §1-3 표), 밴드는
#   **불확실성을 승인 브리프에 노출**하는 라벨일 뿐이다 — 완화가 아니므로 PM 승인 불요(3-3③ⓑ).
Z_ONE_SIDED_95 = 1.6449                              # 단측 95% 정규 분위수 (Φ⁻¹(0.95))
BAND_FAIL_CERTAIN = "FAIL-확정"                       # CI 하한 > 예산 — 초과가 통계적으로 확정
BAND_FAIL_BOUNDARY = "FAIL-경계"                      # 구간이 예산을 걸침 + 점추정 초과
BAND_PASS_BOUNDARY = "PASS-경계"                      # 구간이 예산을 걸침 + 점추정 이내
BAND_PASS_CERTAIN = "PASS-확정"                       # CI 상한 ≤ 예산 — 예산 내가 확정
BOUNDARY_BANDS = (BAND_FAIL_BOUNDARY, BAND_PASS_BOUNDARY)
# 나쁜 순 정렬 — B5 처럼 여러 밴드를 하나로 접을 때 **가장 나쁜 것**을 대표로 쓴다.
BAND_SEVERITY = {BAND_FAIL_CERTAIN: 3, BAND_FAIL_BOUNDARY: 2,
                 BAND_PASS_BOUNDARY: 1, BAND_PASS_CERTAIN: 0}

# ── 게이트 임계 기본값 (params.yaml `ct.ct2_gate_*` 로 override — 6-1) ──────────
# 출발값 근거: 원 학습 REPORT_05 실측 (L5 1.53% · VAL 평균 0.05 · P95 < 0.2 · VAL ~980장).
# ⚠️ 스윕 확정값이 아니다 — WP-4(N 스윕)에서 재산정 대상 (합의안 16차 등재 대기).
DEFAULTS = {
    # ── B1 오경보 **예산** (2026-08-09 — 구 `l5_max` 개명. 레거시 키는 아래 승계 처리) ──
    # 기본값을 **원 목표 0.02 로 둔다**. params 의 0.04 는 "가드 걸린 완화"(문서 §1-4)라,
    # 기본값보다 느슨하다는 사실이 `_weakened` 에 남아 승인 브리프에 계속 노출돼야 한다.
    # 미탐 1건 검출 시 복귀 목표가 바로 이 값이다 — 기본값을 0.04 로 올리면 그 복귀선이
    # 코드에서 사라지고 완화가 조용히 상시화된다.
    "l5_budget": 0.02,              # B1: 홀드아웃 ae_score > qual 비율 예산 (DoD5 원 목표)
    "recon_rmse_max": 1.0,          # B: 채널가중 재구성 RMSE 상한 (정상화 실패 = 1.046 → 0.332 실측)
    "calib_mean": 0.05,             # B: calib ① 목표 VAL 평균
    "calib_mean_tol": 0.02,         # B: calib ① 허용 편차 (±)
    "val_p95_max": 0.2,             # B: calib ② VAL P95 상한 (= qual 임계)
    "min_val_wafers": 100,          # D: calib·scorer 적합 표본 하한 (앵커 분위수 안정)
    "min_gate_wafers": 100,         # D: B군 홀드아웃 하한. l5_budget 0.02 를 **측정 가능**하게 하는
                                    #    값 — 50장이면 오경보 1건이 이미 2% 라 해상도가 임계보다 거칠다.
                                    #    ⚠️ 3밴드 도입 후의 실질 하한은 이 값이 아니라 **CI 반폭**이다:
                                    #    p≈3.5% 에서 n=600 → ±1.25%p 라 예산 4% 와 원 목표 2% 를 구분
                                    #    못 한다. 문서 §1-5 의 "≥800장"은 반올림 표기 — 실계산은
                                    #    800→±1.08%p(미달) / **940장→±1.00%p**. 스윕 목표 1000장 (WP-4).
    "anchor_gap_min_ratio": 0.01,   # D: 인접 앵커 간격 / 전체 폭 하한 (뭉침 차단)
    "probe_sigma": 4.0,             # C: 채점 σ 배수 (게이트 판정 기준점)
    "probe_min_detect": 0.90,       # C: probe_sigma 에서 요구 검출률 (2026-08-09 원값 유지 —
                                    #    v1 의 0.875 완화는 가중 채점 도입으로 철회됨. 문서 §3)
    # ⚠️ 완화 감시(WEAKEN_WATCH)는 up/down 축이라 bool 을 표현할 수 없다. 대신 `probe_detection`
    #    이 **가중-산술 격차**를 재서 완화 창(PROBE_LENIENCY_MARGIN)에 들어오면 브리프에 올린다.
    "probe_weighted": True,         # C: 판정값을 부록 A 손실가중 w_c 로 가중평균할 것인가 (§3-2).
                                    #    false 면 산술평균으로 판정한다(구 거동). 두 값 모두 리포트에
                                    #    병기되므로 어느 쪽으로 쟀는지는 감사에서 항상 확인 가능.
    "probe_sigma_grid": [1.0, 2.0, 3.0, 4.0, 6.0],   # C: 리포트용 그리드 (판정은 probe_sigma)
    # ── E: 정답지 대조 검출률 (2026-08-03 신설) ────────────────────────────
    # 왜 C군으로 부족한가: 프로브는 **학습이 끝난 뒤** 만드는 합성 교란이라 모델이 원리적으로
    # 학습할 수 없고, 그래서 항상 높게 나온다. 학습셋에 실제 이상이 섞이면(2026-08-02 오염)
    # 모델은 그것을 "정상"으로 배우는데 C군은 그 사실을 검사하지 못한다 — 실증: 프로브
    # 97.8%@4σ PASS / 실제 주입 검출 0.6%. E군은 **정답지에 라벨된 실제 주입 구간**을
    # 그대로 채점한다. 라벨 미제공 시 검사는 생략되며 그 사실이 리포트에 남는다.
    "label_min_detect": 0.80,       # E: 라벨 주입 구간 검출률 하한 (성숙 구간 기준 — 아래 warmup 참조)
    "label_warmup_k": 20,           # E: 주입 후 경과 k < 이 값은 판정에서 제외 (drift 램프 초기는
                                    #    설계상 크기가 0에 가까워 미검출이 정상 — 오판정 방지)
    "label_min_wafers": 30,         # E: 판정 가능 최소 라벨 표본 (미달 시 리포트만·판정 생략)
    "require_labels": True,         # E0: 정답지 미제공을 FAIL 로 볼 것인가 (2026-08-05 G0-1).
                                    #     실운영(정답지 부재) 전환 시에만 false 로 **명시 면제**하며,
                                    #     그 사실이 리포트 `gate_params` 에 남아 승인자가 본다.
    # ── B5: 챔버별 L5 상한 (WP-E 중간 완화안, 2026-08-05) ───────────────────
    # 왜: B1 은 전체 평균 하나라 국소 붕괴를 가린다. 실측 총계 8.00% 가 CH4 1.05% ~ CH2 35.0%
    # (33배)였다. 챔버 스코프 전면 전환(학습·서빙 동시 전환 = 별건 결정) 전까지, 총계가
    # 임계 아래여도 한 챔버가 무너져 있으면 배포를 막는다. **항목 추가라 PM 승인 불요**(3-3③ⓑ).
    "chamber_l5_max": 0.05,         # B5: 챔버별 오경보율 상한 (전체 l5_max 보다 느슨 — 소표본 변동 감안)
    "chamber_min_wafers": 20,       # B5: 이 표본 미만 챔버는 판정에서 제외 (비율이 이진에 가까움)
}

# 수치 강제 변환에서 제외할 키 (bool·list). `float(True)` 는 1.0 이 되어 조용히 뜻이 바뀐다.
NON_NUMERIC_KEYS = ("probe_sigma_grid", "require_labels", "probe_weighted")

# 레거시 키 승계 (구 params.yaml 호환). `ct2_gate_l5_max` 만 있는 파일이면 그 값을 예산으로
# 읽는다 — 조용히 DEFAULTS(0.02)로 되돌아가면 **판정이 바뀐 줄 모르고** e4 가 FAIL 이 된다.
LEGACY_KEY_ALIASES = {"l5_budget": "l5_max"}

# C1 조합 완화 창 = (v2 임계 0.90) − (v1 임계 0.875). 가중값이 산술값보다 이만큼 넘게 높으면
# v1 이 막던 번들을 v2 가 통과시킨다 — `probe_detection` 이 그 사실을 브리프에 올린다.
PROBE_LENIENCY_MARGIN = 0.025

# ── 완화 감시 (G0-3, 2026-08-05) ────────────────────────────────────────────
# (키, 방향, 기준) — 'up' = 값이 **커지면** 완화(상한류), 'down' = 작아지면 완화(하한류).
# 구 구현의 결함 2가지를 고친다:
#   ⓐ 감시 대상이 `probe_min_detect`·`l5_max` 2개뿐이라, E군 3키는 감시 밖이었다 —
#      `ct2_gate_label_min_wafers: 100000` 한 줄이면 오염 검사가 **경고 한 줄 없이** 영구 SKIP.
#   ⓑ **방향이 반대**였다. `l5_max` 는 상한이라 완화 = 값을 키우는 쪽인데 `≤ 0.0`(강화)만 잡았다.
#   ⓒ 2026-08-09 — `l5_max` → `l5_budget` 개명. **감시 항목은 하나로 유지**한다(구 키를 함께
#      두면 같은 완화가 `_weakened` 에 2건으로 세어져 승인 브리프의 "약화 N건"이 틀린다).
WEAKEN_WATCH = (
    ("l5_budget", "up"),
    ("recon_rmse_max", "up"),
    ("val_p95_max", "up"),
    ("calib_mean_tol", "up"),
    ("chamber_l5_max", "up"),
    ("label_min_wafers", "up"),
    ("label_warmup_k", "up"),
    ("probe_min_detect", "down"),
    ("label_min_detect", "down"),
    ("min_val_wafers", "down"),
    ("min_gate_wafers", "down"),
    ("chamber_min_wafers", "down"),
    ("anchor_gap_min_ratio", "down"),
)


def _as_bool(value, key: str, default: bool) -> bool:
    """YAML 값 → bool. 문자열 `"false"`/`"no"`/`"0"` 도 거짓으로 읽는다 (§6-1 경계 방어).

    `bool()` 캐스팅은 비어 있지 않은 **모든 문자열을 True** 로 만든다. 게이트를 끄려는
    조작(`ct2_gate_require_labels: "false"`)이 게이트를 켠 채로 통과하는 형태의 실패라,
    "예외가 안 났으니 성공"(TSR-0002)과 같은 계열이다. 해석 불가는 기본값 + 경고.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0"):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    log.warning("ct2_gate_%s 값 %r 를 불리언으로 못 읽음 → 기본값 %s 사용", key, value, default)
    return bool(default)


def load_gate_params(params_path=None) -> dict:
    """`ct.ct2_gate_*` 로 DEFAULTS override. 로드 실패 시 DEFAULTS 유지(+경고).

    키 규약: params 의 `ct2_gate_l5_budget` → DEFAULTS 의 `l5_budget` (접두 제거).
    구 키(`ct2_gate_l5_max`)만 있는 params.yaml 은 `LEGACY_KEY_ALIASES` 로 승계한다.

    **완화 방향 override 는 경고 + 리포트 기록한다** (§7 "분모가 되는 baseline·임계를 검증
    없이 사용" 취지): 오타 하나로 게이트가 조용히 무력화되는 것을 막기 위해, 기본값보다
    느슨한 값은 로그로 드러내고 `_weakened` 키에 담아 리포트 `gate_params` 로 흘린다 —
    승인 브리프가 "임계가 완화된 채 PASS"를 볼 수 있게. 값 자체는 존중한다(조정 권한은
    A). 완화의 승인 책임은 헌법 3-3 ③ ⓑ가 PM에게 둔다.
    """
    p = dict(DEFAULTS)
    path = Path(params_path) if params_path else REPO_ROOT / "config" / "params.yaml"
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            ct = (yaml.safe_load(f) or {}).get("ct") or {}
    except Exception as e:                                    # noqa: BLE001
        log.warning("params.yaml 로드 실패 → 게이트 기본값 사용: %s", e)
        p["_weakened"] = []
        p["l5_max"] = p["l5_budget"]                          # 구 키 미러 (아래 성공 경로와 동일)
        return p
    for key in DEFAULTS:
        cfg_key = f"ct2_gate_{key}"
        if ct.get(cfg_key) is not None:
            p[key] = ct[cfg_key]
        elif key in LEGACY_KEY_ALIASES and ct.get(f"ct2_gate_{LEGACY_KEY_ALIASES[key]}") is not None:
            # 구 키만 있는 params.yaml — 값을 승계하고 그 사실을 남긴다. 조용히 기본값으로
            # 돌아가면 판정이 바뀐 줄 모른다 (헌법 7장 "폐기하면서 읽는 코드를 안 찾음"의 역).
            old = LEGACY_KEY_ALIASES[key]
            p[key] = ct[f"ct2_gate_{old}"]
            log.warning("ct2_gate_%s 는 구 키다 → ct2_gate_%s 로 승계(%s). "
                        "params.yaml 을 갱신하라", old, key, p[key])

    grid = p["probe_sigma_grid"]                            # 스칼라 오기재 → TypeError 방어
    if not isinstance(grid, (list, tuple)) or not grid:
        log.warning("ct2_gate_probe_sigma_grid 형식 오류(%r) → 기본 그리드 사용", grid)
        grid = list(DEFAULTS["probe_sigma_grid"])
    # ★불리언은 `bool()` 로 캐스팅하지 않는다 — YAML 에 따옴표가 붙어 `"false"` 문자열이 오면
    #   `bool("false") is True` 라 **끄려던 시도가 조용히 무시**된다. 특히 `probe_weighted` 는
    #   false 가 더 엄격한 쪽(산술 판정)이라, 운영자가 보수적으로 되돌리려는 조작이 무증상으로
    #   사라지는 자리다. 문자열은 명시적으로 해석하고, 못 읽으면 기본값 + 경고.
    for key in ("require_labels", "probe_weighted"):
        p[key] = _as_bool(p[key], key, DEFAULTS[key])
    # 수치 키는 여기서 한 번에 강제 변환한다 — YAML 에 `"4.0"` 처럼 인용부호가 붙으면
    # 문자열이 되고, 나중에 `f"{x:g}"` 포매팅이 ValueError 로 터져 판정이 실행 오류로
    # 강등된다 (리포트 미기록). 변환 불가면 기본값으로 되돌린다.
    for key in DEFAULTS:
        if key in NON_NUMERIC_KEYS:
            continue
        try:
            p[key] = float(p[key])
        except (TypeError, ValueError):
            log.warning("ct2_gate_%s 값 %r 를 수치로 못 읽음 → 기본값 %s 사용",
                        key, p[key], DEFAULTS[key])
            p[key] = float(DEFAULTS[key])
    try:
        p["probe_sigma_grid"] = [float(k) for k in grid]
    except (TypeError, ValueError):
        log.warning("ct2_gate_probe_sigma_grid 원소가 수치 아님(%r) → 기본 그리드 사용", grid)
        p["probe_sigma_grid"] = list(DEFAULTS["probe_sigma_grid"])

    weakened = []
    for key, direction in WEAKEN_WATCH:
        base, got = float(DEFAULTS[key]), float(p[key])
        if (direction == "up" and got > base) or (direction == "down" and got < base):
            weakened.append(key)
            log.warning("★게이트 완화 감지 — %s=%s (기본 %s, 방향 %s). 항목이 느슨해졌다. "
                        "완화는 PM 승인 사항 (헌법 3-3 ③ ⓑ).", key, got, base, direction)
    if not p["require_labels"]:
        weakened.append("require_labels")
        log.warning("★E0 면제 — require_labels=false. 정답지 없이 통과 가능해지므로 "
                    "**학습셋 오염 방어가 0**이다 (리스크 K-1). 면제 사실을 리포트에 기록한다.")
    p["_weakened"] = weakened
    # 구 키 읽기 호환 미러 — 옛 리포트/스크립트가 `gate_params["l5_max"]` 를 본다.
    # WEAKEN_WATCH·DEFAULTS 에는 없으므로 완화 감시가 이중으로 세지 않는다.
    p["l5_max"] = p["l5_budget"]
    return p


# ── 비율 판정 3밴드 (Wilson) ────────────────────────────────────────────────
def wilson_interval(k: int, n: int, z: float = Z_ONE_SIDED_95) -> tuple[float, float]:
    """이항 비율 k/n 의 Wilson 점수 구간 (기본 = 단측 95% 분위수 양쪽).

    Wald(정규 근사)를 쓰지 않는 이유: 오경보율은 0 근처의 소표본 비율이라 Wald 구간이
    음수까지 내려가고 실제 포함확률이 명목치에 한참 못 미친다. Wilson 은 그 영역에서
    포함확률이 안정적이고, 경계에서 구간이 0 밖으로 새지 않는다.

    Args:
        k: 사건 수(오경보 건수). n: 표본 수. z: 정규 분위수.

    Returns:
        (lo, hi) — n ≤ 0 이면 (0.0, 1.0) (판정 불가 = 아무것도 배제 못 함).
    """
    if n <= 0:
        return 0.0, 1.0
    k = max(0, min(int(k), int(n)))
    p_hat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denom
    half = z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def band_for_rate(k: int, n: int, budget: float) -> dict:
    """비율 k/n 을 예산 대비 3밴드로 라벨링 (문서 §1-3).

    ★판정은 **점추정**으로 한다 — 밴드는 라벨이지 기준이 아니다. lo ≤ p̂ ≤ hi 이므로
    확정 밴드는 점추정 판정과 항상 일치하고, 경계 밴드에서만 점추정이 결정을 짓는다.
    따라서 이 함수 도입으로 통과/차단이 뒤집히는 후보는 원리적으로 없다(개정 v1 대비
    판정 무변경 = 완화 아님 → PM 승인 불요, 헌법 3-3 ③ ⓑ).

    Returns:
        {k, n, rate, ci_lo, ci_hi, budget, band, boundary, pass} — 리포트·브리프 공용.
    """
    lo, hi = wilson_interval(k, n)
    rate = (k / n) if n > 0 else 0.0
    passed = rate <= budget
    if lo > budget:
        band = BAND_FAIL_CERTAIN
    elif hi <= budget:
        band = BAND_PASS_CERTAIN
    else:
        band = BAND_PASS_BOUNDARY if passed else BAND_FAIL_BOUNDARY
    return {"k": int(k), "n": int(n), "rate": round(rate, 4),
            "ci_lo": round(lo, 4), "ci_hi": round(hi, 4), "budget": float(budget),
            "band": band, "boundary": band in BOUNDARY_BANDS, "pass": bool(passed)}


def fmt_band(b: dict) -> str:
    """밴드 dict → 사람이 읽는 한 줄 (검사 detail·브리프 공용 문구).

    ★부등호는 판정에 따라 뒤집는다. 구 형식(`f"{l5} ≤ {l5_max}"`)을 그대로 물려받으면
    FAIL 항목이 `"6.67% (40/600) ≤ 4% · FAIL-확정"` 이라는 **거짓 부등식**이 되는데,
    v2 에서 이 문자열은 `approval_brief.boundaries[].detail` 로 승인 화면까지 흐른다.
    """
    op = "≤" if b["pass"] else ">"
    return (f"{b['rate'] * 100:.2f}% ({b['k']}/{b['n']}) {op} {b['budget'] * 100:g}% · "
            f"CI[{b['ci_lo'] * 100:.2f}, {b['ci_hi'] * 100:.2f}]% · {b['band']}")


# ── VAL 세트 복원 ──────────────────────────────────────────────────────────
def resolve_val_wafers(bundle_dir: Path, df, val_frac=None) -> tuple[list, list, str]:
    """VAL(적합) + 게이트 홀드아웃 목록 복원. (val, gate, source) 반환.

    ① 사이드카 `val_wafers.json`(run_ct2 산출) — 정확·우선. `gate` 는 홀드아웃(미적합).
    ② 폴백: manifest `selection` 으로 재유도 (사이드카 유실 시). 홀드아웃은 복원 불가라
       빈 목록 — 그 사실은 D3 검사로 드러난다. explicit_wafers 였다면 재현 자체가 불가해
       예외 (조용한 근사 금지).
    """
    from .retrain import VAL_WAFERS_SIDECAR, select_normal_wafers, time_split

    side = bundle_dir / VAL_WAFERS_SIDECAR
    if side.exists():
        s = io_bundle.load_json(side)
        return ([str(w) for w in s.get("val", [])],
                [str(w) for w in (s.get("gate") or [])], "sidecar")

    man = io_bundle.read_manifest(bundle_dir)
    sel = man.get("selection", {})
    if sel.get("explicit_wafers"):
        raise RuntimeError(
            f"{VAL_WAFERS_SIDECAR} 없음 + 학습이 --wafers-file 경로였음 → VAL 재현 불가. "
            "사이드카를 복구하거나 재학습하세요 (근사 검증 금지).")
    if sel.get("injection_excluded"):
        # K-6 — 학습은 `--labels` 로 주입을 제외했는데 폴백은 `select_normal_wafers` 를
        # **labels_path 없이** 부른다. 그러면 오염 포함 목록이 VAL 로 재유도되어, 검증이
        # 학습과 다른 세트에서 돌아간다. manifest 에 set_hashes 가 없으면 A3 도 생략돼
        # 조용히 새어나간다 — explicit_wafers·chamber 와 같은 급의 재현 불가로 환원한다.
        raise RuntimeError(
            f"{VAL_WAFERS_SIDECAR} 없음 + 학습이 라벨 제외 경로(injection_excluded=true) → "
            "VAL 재현 불가 (폴백은 주입 포함 목록을 재유도한다). 사이드카를 복구하거나 재학습하세요.")
    if sel.get("split_mode") == "chamber":
        # 아래 폴백은 전역 `time_split` 이다 — chamber 분할 번들에 쓰면 **다른 세트**를
        # VAL 이라 부르며 통과/실패시킨다. A3 세트 해시가 잡아주지만, manifest 에 해시가
        # 없는 번들에서는 조용히 새어나간다. 재현 불가는 명시적 오류로 환원한다.
        raise RuntimeError(
            f"{VAL_WAFERS_SIDECAR} 없음 + split_mode='chamber' → VAL 재현 불가 "
            "(폴백은 전역 시간순 분할이라 다른 세트가 된다). 사이드카를 복구하거나 재학습하세요.")
    ordered = select_normal_wafers(df, sel.get("regime"), sel.get("normal_col"),
                                   sel.get("normal_value"), None)
    _, val_w = time_split(ordered, val_frac if val_frac is not None
                          else float(sel.get("val_frac", 0.2)))
    return [str(w) for w in val_w], [], "rederived"


# ── 검사 레코드 ────────────────────────────────────────────────────────────
def _c(name: str, passed: bool, detail: str, critical: bool = False,
       skip: bool = False, band: dict | None = None) -> dict:
    """단일 검사 결과 레코드. critical=True 는 계약·무결성 위반(설명이 곧 차단 사유).

    Args:
        skip: **판정 불가**(검사를 돌리지 못함). `pass` 는 True 로 두되 최종 verdict 를
            SKIP 으로 끌어내려 배포를 막는다 — "검사를 못 했다"를 "통과했다"로 환원하지
            않기 위함이다 (K-2: 오염이 심할수록 평가 표본이 줄어 E1 이 SKIP 으로 도망간다).
        band: 비율 판정 항목(B1·B5)의 3밴드 dict (`band_for_rate` 산출). 판정에는 쓰이지
            않고 `verdict_band` 로 리포트·승인 브리프에 흐른다 (문서 §1-3).
    """
    rec = {"check": name, "pass": bool(passed), "detail": detail,
           "critical": critical, "skip": bool(skip)}
    if band is not None:
        rec["verdict_band"] = band["band"]
        rec["band"] = band
    return rec


# ── A. 무결성 (스코어링·세트 복원보다 **먼저** — 아래 주석 참조) ───────────────
def check_bundle_files(bundle_dir: Path) -> list[dict]:
    """7파일 완결성 + manifest sha256 — 데이터/manifest 를 읽기 **전에** 도는 검사.

    순서가 중요한 이유: manifest 누락·파손은 A1·A2가 잡아야 할 critical FAIL 인데,
    세트 복원(`resolve_val_wafers`)이 먼저 `read_manifest` 를 호출하면 예외가 터져
    게이트가 **판정(exit 2) 대신 실행 오류(exit 1)** 로 끝난다 — 리포트 파일조차 남지
    않는다. 그래서 파일 존재·해시를 선행시킨다.
    """
    A = io_bundle.ARTIFACT_NAMES
    missing = [n for k, n in A.items() if not (bundle_dir / n).exists()]
    checks = [_c("A1_번들_7파일_완결", not missing, f"누락 {missing}" if missing else "7/7",
                 critical=True)]
    if missing:
        return checks                                   # 이후 검사는 의미 없음

    n_expected = len(A) - 1                             # manifest 자신은 해시 대상 아님
    try:
        ver = io_bundle.verify_bundle(bundle_dir)
    except Exception as e:                              # noqa: BLE001 — manifest 파손
        return checks + [_c("A2_manifest_sha256", False, f"manifest 판독 실패: {e}",
                            critical=True)]
    bad = [n for n, v in ver.items() if not v["ok"]]
    # 해시 항목 수도 함께 본다 — manifest 에 sha256 키가 없으면 verify_bundle 이 빈 dict 를
    # 돌려주고 "불일치 0건"으로 통과한다 (무결성 검사가 조용히 사라지는 형태의 실패).
    ok = (not bad) and len(ver) == n_expected
    checks.append(_c("A2_manifest_sha256", ok,
                     f"불일치 {bad}" if bad else f"{len(ver)}/{n_expected}건 일치"
                     + (" — sha256 항목 부족(make_drift 재생성 필요)"
                        if len(ver) != n_expected else ""),
                     critical=True))
    return checks


def check_set_hashes(bundle_dir: Path, val_wafers, gate_wafers, gate: dict) -> list[dict]:
    """VAL·게이트 홀드아웃 세트 해시 대조 + 홀드아웃 실존 (A3·D3)."""
    man = io_bundle.read_manifest(bundle_dir)
    hashes = man.get("set_hashes") or {}
    exp, got = hashes.get("val"), sha1_8(val_wafers)
    checks = [_c("A3_VAL_세트_해시", exp is None or exp == got,
                 f"got={got} exp={exp}"
                 + (" (manifest 미기재 — 검사 생략)" if exp is None else ""),
                 critical=True)]

    exp_g = hashes.get("gate")
    if gate_wafers:
        checks.append(_c("A5_게이트_세트_해시", exp_g is None or exp_g == sha1_8(gate_wafers),
                         f"got={sha1_8(gate_wafers)} exp={exp_g}", critical=True))
    # D3 — 홀드아웃 실존. 없으면 B군이 calib 적합 표본에서 측정돼 항진명제가 된다
    # (retrain._carve_gate_holdout docstring). **하드 FAIL 이다** — B군을 신뢰할 수 없는
    # 번들은 배포하지 않는다. 자동 경로는 `ct2_min_train_wafers`(1000) 게이트를 이미
    # 통과했으므로 홀드아웃이 없을 수 없고, 없다면 표본 설계가 어긋난 것이다.
    # 퇴화 경로(on_holdout=False)는 **PASS 용이 아니라** B군 수치를 진단용으로 리포트에
    # 남기기 위한 것이다 — 그 상태로 gate_pass 가 참이 되는 일은 없다.
    checks.append(_c("D3_게이트_홀드아웃", bool(gate_wafers),
                     f"{len(gate_wafers)}장 (calib 미적합)" if gate_wafers
                     else "없음 — B군이 calib 적합 표본에서 측정됨(항진명제). "
                          "재학습 시 --gate-holdout-frac 로 확보 필요 (배포 불가)"))
    # D4 — calib·scorer 적합 표본 하한. 앵커(P98.5·P99.5)의 분위수 안정성이 걸린다.
    checks.append(_c("D4_적합_표본_하한", len(val_wafers) >= gate["min_val_wafers"],
                     f"{len(val_wafers)} ≥ {gate['min_val_wafers']} (calib·scorer 적합)"))
    return checks


# ── D. 캘리 구조 (스코어링 선행 — 깨진 계약은 예외가 아니라 FAIL) ─────────────
def check_calib_contract(calib: dict, gate: dict) -> tuple[list[dict], bool]:
    """앵커 구조·간격 검사. (checks, 스코어링_가능) 반환.

    왜 스코어링보다 먼저인가: `apply_calibration` 은 `anchors_x`/`anchors_y` 길이가
    어긋나면 예외를 던진다. 그대로 두면 게이트가 **판정(FAIL) 대신 실행 오류(exit 1)** 로
    끝나 "무엇이 문제였는지"가 리포트에 남지 않는다. 깨진 계약은 명시적 FAIL로 잡는다.

    D2 = 인접 앵커 간격 / 전체 폭. 소표본이면 상위 분위수가 뭉쳐 캘리 곡선이 계단이 되고,
    그 상태의 임계는 신뢰할 수 없다 (앵커 뭉침 = 소표본 붕괴 신호).
    """
    ax = [float(x) for x in calib.get("anchors_x", [])]
    ay = list(calib.get("anchors_y", []))

    if len(ax) < 2 or len(ax) != len(ay):
        return ([_c("D2_앵커_간격_비율", False,
                    f"앵커 구조 위반 — anchors_x {len(ax)}개 / anchors_y {len(ay)}개 "
                    "(2개 이상·동일 길이 필요)", critical=True)], False)

    span = ax[-1] - ax[0]
    gaps = [ax[i + 1] - ax[i] for i in range(len(ax) - 1)]
    ratio = (min(gaps) / span) if span > 0 else 0.0
    return ([_c("D2_앵커_간격_비율", ratio >= gate["anchor_gap_min_ratio"],
                f"min_gap/span={ratio:.4f} ≥ {gate['anchor_gap_min_ratio']}"
                + (" (span≤0 = 앵커 붕괴)" if span <= 0 else ""))], True)


# ── B·D. 정상 조용 + 임계 건전 ──────────────────────────────────────────────
def chamber_breakdown(score, chambers, qual: float) -> dict:
    """홀드아웃 오경보를 **챔버별로 분해** (판정 아님 — 리포트 상시 병기, 2026-08-03 신설).

    왜 필요한가: B1 은 전체 평균 하나다. 2026-08-03 실측에서 총계 L5 8.00% 가 챔버별로는
    CH4 1.1% ~ CH2 35.0% 로 **30배** 차이였다. 평균만 보면 "전반적으로 조금 높다"로 읽혀
    전역 임계 조정이라는 처방으로 가는데, 그 방향으로는 해결되지 않는다(한쪽에 맞추면
    다른 쪽이 깨진다). 원인 진단에 필요한 정보라 **판정 기준은 그대로 두고 기록만** 남긴다.

    Args:
        score: 홀드아웃 ae_score 배열. chambers: 같은 순서의 챔버 라벨(없으면 None).
        qual: 오경보 판정 임계.

    Returns:
        {챔버: {n, k, l5, mean, p95}} — 챔버 정보가 없으면 빈 dict.
        `k`(오경보 **건수**)를 함께 남긴다 — 비율만 있으면 밴드(CI)를 되짚어야 하고,
        `round(l5*n)` 되짚기는 반올림 오차로 1건씩 어긋난다 (헌법 7장 "순번은 추정하지
        말고 기록한다"와 같은 취지).
    """
    if chambers is None:
        return {}
    s = np.asarray(score, float)
    ch = np.asarray([str(c) for c in chambers])
    if len(ch) != len(s):
        log.warning("챔버 라벨 %d개 ≠ 점수 %d개 → 챔버별 분해 생략", len(ch), len(s))
        return {}
    out = {}
    for c in sorted(set(ch)):
        m = ch == c
        out[c] = {"n": int(m.sum()),
                  "k": int(np.sum(s[m] > qual)),
                  "l5": round(float(np.mean(s[m] > qual)), 4),
                  "mean": round(float(s[m].mean()), 4),
                  "p95": round(float(np.percentile(s[m], 95)), 4)}
    return out


def check_quiet_and_thresholds(scorer, X_val, calib: dict, gate: dict,
                               n_val: int, on_holdout: bool = True,
                               chambers=None) -> tuple[list[dict], dict]:
    """정상 표본에서 조용한지 + 임계(앵커·표본)가 건전한지. (checks, metrics) 반환.

    Args:
        n_val: 채점에 쓴 표본 수.
        on_holdout: True면 calib 미적합 홀드아웃(정상 경로), False면 적합 표본으로 퇴화한
            상태. 하한 기준이 달라진다 — 홀드아웃은 `min_gate_wafers`(L5 측정 가능성),
            적합 표본은 `min_val_wafers`(앵커 분위수 안정).

    캘리 구조가 깨져 있으면 스코어링을 시도하지 않고 그 사실만 FAIL로 돌려준다.
    """
    calib_checks, scorable = check_calib_contract(calib, gate)
    floor_key = "min_gate_wafers" if on_holdout else "min_val_wafers"
    n_check = _c("D1_B군_표본_하한", n_val >= gate[floor_key],
                 f"{n_val} ≥ {gate[floor_key]} ({floor_key}"
                 + (", 홀드아웃)" if on_holdout else ", 적합 표본으로 퇴화)"))
    if not scorable:
        return calib_checks + [n_check], {"n_val": n_val, "scored": False}

    raw = scorer.ae_raw(X_val)
    sc = apply_calibration(raw, calib)
    qual = float(calib.get("qual_threshold", gate["val_p95_max"]))

    l5 = false_alarm_rate(sc, qual)
    # 밴드 계산에는 비율이 아니라 **건수**가 필요하다 — `round(l5*n)` 되짚기는 반올림으로
    # 1건씩 어긋나 CI 가 틀어진다. 세는 곳에서 세어 둔다.
    l5_k = int(np.sum(np.asarray(sc, float) > qual))
    l5_band = band_for_rate(l5_k, len(sc), float(gate["l5_budget"]))
    recon = scorer.recon_rmse(X_val)
    mean_, p95 = float(sc.mean()), float(np.percentile(sc, 95))
    by_chamber = chamber_breakdown(sc, chambers, qual)

    checks = [
        # 판정은 점추정(`l5_band["pass"]` = l5 ≤ 예산) — 밴드는 라벨이다 (band_for_rate 참조).
        _c("B1_L5_오경보율", l5_band["pass"], fmt_band(l5_band), band=l5_band),
        _c("B2_recon_RMSE", recon <= gate["recon_rmse_max"],
           f"{recon:.4f} ≤ {gate['recon_rmse_max']}"),
        _c("B3_calib①_VAL평균", abs(mean_ - gate["calib_mean"]) <= gate["calib_mean_tol"],
           f"{mean_:.4f} = {gate['calib_mean']}±{gate['calib_mean_tol']}"),
        _c("B4_calib②_VAL_P95", p95 < gate["val_p95_max"], f"{p95:.4f} < {gate['val_p95_max']}"),
        n_check,
        check_chamber_l5(by_chamber, gate),
    ] + calib_checks

    return checks, {"l5": round(l5, 4), "l5_k": l5_k, "l5_band": l5_band,
                    "recon_rmse": round(recon, 4),
                    "val_mean": round(mean_, 4), "val_p95": round(p95, 4),
                    "qual_threshold": qual, "n_val": n_val, "scored": True,
                    "by_chamber": by_chamber}


def check_chamber_l5(by_chamber: dict, gate: dict) -> dict:
    """B5 — **챔버별** 오경보율 상한 (WP-E 중간 완화안, 2026-08-05 신설).

    `chamber_breakdown` 을 기록 전용에서 **판정으로 승격**한 것이다. B1(총계 평균) 하나로는
    국소 붕괴를 못 잡는다 — 실측에서 총계 8.00% 는 "전반적으로 조금 높다"로 읽히는데
    실제로는 CH2 35.0% / CH4 1.05% 의 33배 편차였고, 그 상태의 처방(전역 임계 조정)은
    한쪽을 맞추면 다른 쪽이 깨진다.

    표본이 `chamber_min_wafers` 미만인 챔버는 **제외**한다 — n=20 에서 오경보 1건이 5% 라
    비율이 이진 판정에 가까워진다(D1 표본 하한과 같은 취지).

    챔버 정보가 없으면(입력 CSV 에 C24 부재) 판정하지 않고 그 사실을 남긴다 — 조용히
    통과시키지 않기 위해 detail 에 명시한다.

    **3밴드 준용 (2026-08-09, 문서 §2)**: 챔버당 n=150 이면 CI 반폭이 ±3%p 라 임계 5% 와
    비교해 "5% 이내임을 확인"하는 게 아니라 "**5% 초과가 확정은 아님**"까지만 말할 수 있다
    (실측 4챔버 전부 CI 상한이 5% 초과). 판정은 종전대로 점추정으로 하되, 대표 밴드
    (가장 나쁜 챔버)를 라벨로 실어 승인자가 그 한계를 보게 한다. `chamber_min_wafers` 20 은
    **밴드 표기 전제로만** 유지한다 — 20장에서 5% 는 1건이 곧 5% 라 사실상 이진 판정이다.
    """
    if not by_chamber:
        # 챔버 라벨 부재는 "판정 가능 챔버 0"보다 **넓은 사각**이다 — 어느 챔버도 검증되지
        # 않은 상태이므로 더 관대하게 다룰 이유가 없다. 같은 SKIP 으로 묶는다.
        return _c("B5_챔버별_L5", True, "챔버 라벨 없음 — 분해 불가(판정 불가). "
                                        "입력에 C24 가 있으면 활성화된다", skip=True)
    floor = float(gate["chamber_min_wafers"])
    cap = float(gate["chamber_l5_max"])
    judged = {c: v for c, v in by_chamber.items() if v["n"] >= floor}
    if not judged:
        return _c("B5_챔버별_L5", True,
                  f"판정 가능 챔버 0 (전부 n < {floor:g}) — 생략", skip=True)
    # 챔버별 밴드 — `k` 가 없는 구 리포트 재사용 대비로 비율에서 되짚되, 그 사실을 남긴다.
    bands = {c: band_for_rate(int(v.get("k", round(v["l5"] * v["n"]))), v["n"], cap)
             for c, v in judged.items()}
    worst_c = max(bands, key=lambda c: (BAND_SEVERITY[bands[c]["band"]], bands[c]["rate"]))
    worst = bands[worst_c]
    bad = {c: v["l5"] for c, v in judged.items() if v["l5"] > cap}
    n_boundary = sum(1 for b in bands.values() if b["boundary"])
    detail = (f"초과 {bad} > {cap} (총계가 국소 붕괴를 가린다) · 최악 {worst_c} {fmt_band(worst)}"
              if bad else
              f"{len(judged)}개 챔버 전부 ≤ {cap} · 최악 {worst_c} {fmt_band(worst)}"
              + (f" · 경계 {n_boundary}/{len(judged)}개 챔버 (소표본 — CI 상한이 임계를 넘어 "
                 f"'초과 아님'까지만 판정 가능)" if n_boundary else ""))
    rec = _c("B5_챔버별_L5", not bad, detail, band=worst)
    rec["bands_by_chamber"] = bands
    return rec


# ── C. 합성 프로브 주입 채점 ────────────────────────────────────────────────
def probe_detection(scorer, scaler, feats_val, feat_cols, calib: dict,
                    gate: dict) -> tuple[list[dict], dict]:
    """정상 VAL 피처에 ±kσ 단일채널 교란 주입 → 검출률 (불가침 2 — 평가 전용).

    설계 의도:
      · **원 피처 공간에 주입**한 뒤 번들 scaler 로 변환한다 — 실제 물리 이탈이 집계
        피처를 밀어내는 경로와 같다(스케일 후 주입은 스케일러 특성을 우회해 과대평가).
      · σ = VAL 피처 표준편차. 채널당 ±양방향 주입 후 평균 → **난수 없음(결정적)** 이라
        시드 간 변동이 원리적으로 0이다.
      · 대상 = 부록 A 정착 연속 채널의 `<ch>_s_mean` 컬럼 (물리 이탈의 1차 반영면).

    **가중 채점 (2026-08-09, 문서 §3-2)**: 판정값 = Σ w_c·rate_c / Σ w_c (w_c =
    `constants.feat_weight` — 부록 A 단일 소스, AE 손실·`scorer.w_vec` 과 같은 값).
    산술평균은 저중요 채널(C57 w=0.3)의 우연 성적과 고중요 채널을 같은 무게로 섞는다 —
    채점이 이미 채널 중요도를 반영하는 학습 손실과 어긋나 있었다. 실측 e4: 산술 0.8983 /
    가중 0.9019. **산술평균도 함께 싣는다**(비교·감사용) — 어느 쪽으로 쟀는지 리포트만
    보고 답할 수 있어야 한다.

    ⚠️ C2(단조성)는 **산술평균 그리드**로 판정한다 — 단조성은 σ 증가에 따른 검출 증가라는
    구조 점검이라 가중 여부와 무관하고, 가중으로 바꾸면 과거 리포트와 비교가 끊긴다.

    Returns:
        (checks, detail) — detail 은 σ 그리드별(산술·가중)·채널별 검출률 (리포트/Brief 근거).
    """
    qual = float(calib.get("qual_threshold", gate["val_p95_max"]))
    targets = [f"{ch}_s_mean" for ch in CH_SETTLE_CONT if f"{ch}_s_mean" in feats_val.columns]
    if not targets:
        return ([_c("C1_프로브_검출률", False,
                    "주입 대상 채널 0 — feature_spec 이 부록 A와 어긋남", critical=True)],
                {"targets": []})

    sd = feats_val[targets].std(ddof=0).replace(0.0, np.nan)
    grid = [float(k) for k in gate["probe_sigma_grid"]]
    if gate["probe_sigma"] not in grid:
        grid = sorted(grid + [float(gate["probe_sigma"])])

    by_sigma: dict[str, float] = {}
    by_sigma_weighted: dict[str, float] = {}
    by_channel: dict[str, float] = {}
    weights_at_gate: dict[str, float] = {}
    for k in grid:
        rates, weights = [], []
        for col in targets:
            s = sd.get(col)
            if s is None or not np.isfinite(s):
                continue                                  # 상수 채널 = 교란 정의 불가 → 제외
            hits = []
            for sign in (+1.0, -1.0):
                F = feats_val.copy()
                F[col] = F[col] + sign * k * float(s)
                X = scaler.transform(F.reindex(columns=feat_cols, fill_value=0.0))
                sc = apply_calibration(scorer.ae_raw(X), calib)
                hits.append(float(np.mean(sc > qual)))
            rate = float(np.mean(hits))
            rates.append(rate)
            # 가중은 **채점된 채널에 대해서만** 정규화한다 — 상수 채널로 빠진 몫을 분모에
            # 남기면 판정값이 그만큼 조용히 낮아진다(없는 채널을 0점으로 세는 셈).
            weights.append(feat_weight(col))
            if k == float(gate["probe_sigma"]):
                by_channel[col] = round(rate, 4)
                weights_at_gate[col] = feat_weight(col)
        by_sigma[f"{k:g}sigma"] = round(float(np.mean(rates)), 4) if rates else 0.0
        wsum = float(np.sum(weights))
        by_sigma_weighted[f"{k:g}sigma"] = (
            round(float(np.dot(rates, weights) / wsum), 4) if rates and wsum > 0 else 0.0)

    key_at_gate = f"{float(gate['probe_sigma']):g}sigma"
    arith = by_sigma[key_at_gate]
    weighted = by_sigma_weighted[key_at_gate]
    use_weighted = bool(gate.get("probe_weighted", True))
    at_gate = weighted if use_weighted else arith
    mode = "가중(w_c·부록A)" if use_weighted else "산술"
    other = f"산술 {arith:.4f}" if use_weighted else f"가중 {weighted:.4f}"
    # ★조합 완화 창 (2026-08-09 리뷰 지적 — 문서 §3-2 보강).
    #   v1 = 산술 ≥ 0.875 / v2 = 가중 ≥ 0.90 이므로, **가중이 산술보다 0.025 넘게 높으면**
    #   v1 이 막던 번들을 v2 가 통과시킨다. 실증 반례: C57(w=0.3)·C58(w=0.5)만 검출 0,
    #   나머지 8채널 1.0 → 산술 0.8000(v1 FAIL) / 가중 0.9024(v2 PASS).
    #   = **저가중 채널에 검출 사각이 몰린 번들**이 이 창에 들어온다. 판정은 문서 확정대로
    #   가중을 쓰되(임의 변경 금지), 그 사실을 detail·브리프에 드러낸다 — 두 값을 리포트에
    #   병기해 놓고 승인자가 못 보면 병기의 의미가 없다 (헌법 7장 "생략과 통과를 같은 값으로").
    lenient_gap = float(weighted - arith)
    in_lenient_window = use_weighted and lenient_gap > PROBE_LENIENCY_MARGIN
    hi, lo = max(grid), min(grid)
    monotone = by_sigma[f"{hi:g}sigma"] >= by_sigma[f"{lo:g}sigma"]

    checks = [
        _c("C1_프로브_검출률", at_gate >= gate["probe_min_detect"],
           f"{at_gate:.4f} @ {gate['probe_sigma']:g}σ ≥ {gate['probe_min_detect']} "
           f"[{mode} 판정 · {other} 병기]"
           + (f" ★가중-산술 격차 {lenient_gap:+.4f} > {PROBE_LENIENCY_MARGIN} — "
              "저가중 채널에 검출 사각이 몰려 있다 (v1 산술 기준이면 막혔을 조합)"
              if in_lenient_window else "")),
        _c("C2_프로브_단조성", monotone,
           f"{hi:g}σ {by_sigma[f'{hi:g}sigma']:.4f} ≥ {lo:g}σ {by_sigma[f'{lo:g}sigma']:.4f} "
           f"(산술 그리드 — 가중 무관 구조 점검)"),
    ]
    detail = {"targets": targets, "by_sigma": by_sigma,
              "by_sigma_weighted": by_sigma_weighted,
              "by_channel_at_gate": by_channel,
              "weights_at_gate": weights_at_gate,
              "scoring": "weighted" if use_weighted else "arithmetic",
              "at_gate": at_gate,                       # ★판정에 실제로 쓴 값 (하류는 이것을 읽는다)
              "at_gate_weighted": weighted, "at_gate_arithmetic": arith,
              "leniency_gap": round(lenient_gap, 4),
              "in_leniency_window": in_lenient_window,
              "note": "평가 전용 주입(불가침 2) — 학습셋 미유입·주입본 미영속. "
                      "판정값은 부록 A 손실가중 w_c 가중평균 (문서 §3-2)"}
    return checks, detail


# ── E. 정답지 대조 검출률 (2026-08-03 신설) ────────────────────────────────
LABEL_COLS = ("C64", "injected")     # 최소 요구 컬럼 (chamber·k·scenario_id 는 선택)


def load_injection_labels(path) -> pd.DataFrame:
    """주입 정답지 라벨 CSV 로드 (`scripts/build_clean_wafer_list.py` 산출).

    필수 컬럼 `C64`·`injected`. 선택 `C24`(챔버)·`k`(주입 후 경과)·`scenario_id`.
    형식 위반은 예외로 올린다 — 라벨을 조용히 무시하면 E군이 "생략"으로 통과해버려
    검사가 사라진 줄 모른다 (C군이 학습 오염을 조용히 통과시킨 것과 같은 실패 유형).
    """
    df = pd.read_csv(path)
    missing = [c for c in LABEL_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"라벨 파일 필수 컬럼 누락 {missing}: {path}")
    df["C64"] = df["C64"].astype(str)
    df["injected"] = df["injected"].astype(bool)
    if not df["injected"].any():
        raise ValueError(f"라벨에 주입 wafer 0장 — 정답지/데이터 대응을 확인하세요: {path}")
    return df


def check_label_detection(scorer, scaler, df, feat_cols, calib: dict, gate: dict,
                          labels: pd.DataFrame, train_wafers: set) -> tuple[list[dict], dict]:
    """라벨된 **실제 주입 구간**의 검출률 (E1). 게이트 C군의 사각을 메운다.

    C군(합성 프로브)과의 차이: 프로브 자극은 학습 이후에 만들어져 모델이 배울 수 없지만,
    실제 주입은 학습셋에 섞이면 모델이 "정상"으로 배운다. 그 오염을 검사하는 유일한 항목이다.

    설계 주의:
      · **학습에 쓰인 wafer 는 제외**한다. 학습셋에 주입이 섞였다면 그 wafer 의 미검출은
        이미 오염의 결과이고, 여기서 재차 세면 "학습 데이터 성능"을 재는 셈이 된다.
        평가는 학습 밖 주입분으로만 한다 (학습셋 오염 자체는 표본 수 경고로 드러난다).
      · **warmup 제외**: drift 패턴은 설계상 시작 직후 크기가 0에 가깝다(선형 램프).
        `k < label_warmup_k` 구간을 판정에서 빼지 않으면 정상 모델도 FAIL 한다.
        `k` 컬럼이 없으면 warmup 을 적용할 수 없으므로 그 사실을 detail 에 남긴다.
      · 표본이 `label_min_wafers` 미만이면 **판정하지 않고 리포트만** 남긴다 — 소표본에서
        비율 판정은 이진에 가까워 의미가 없다(D1 하한과 같은 취지).
    """
    lab_all = labels[labels["injected"]].copy()
    n_injected = len(lab_all)
    lab = lab_all[~lab_all["C64"].isin(train_wafers)]         # 학습분 제외
    n_excluded_train = n_injected - len(lab)
    n_all = len(lab)
    warm_note = ""
    if "k" in lab.columns:
        k = pd.to_numeric(lab["k"], errors="coerce")
        lab = lab[(k.isna()) | (k >= float(gate["label_warmup_k"]))]
        warm_note = f" · warmup k<{gate['label_warmup_k']:g} 제외 {n_all - len(lab)}장"
    else:
        warm_note = " · k 컬럼 없음(warmup 미적용)"
    n_excluded_warmup = n_all - len(lab)

    wafers = [w for w in lab["C64"].tolist() if w in set(df["C64"].astype(str))]
    if len(wafers) < int(gate["label_min_wafers"]):
        # ★SKIP 이지 PASS 가 아니다 (K-2). 학습분 제외 후 표본이 마르는 상황은 **오염이
        # 심할수록 잘 일어난다** — 주입분 대부분이 학습에 들어갔다는 뜻이기 때문이다.
        # 검사가 가장 필요한 순간에 가장 먼저 꺼지는 구조라, 통과로 환원하면 안 된다.
        return ([_c("E1_정답지_검출률", True,
                    f"표본 {len(wafers)} < {gate['label_min_wafers']:g} — 판정 불가(SKIP). "
                    f"주입 {n_injected}장 중 학습분 {n_excluded_train}장 제외"
                    f"{warm_note} → 배포 불가 (오염이 심할수록 표본이 마른다 — K-2)",
                    skip=True)],
                {"n": len(wafers), "judged": False, "note": "표본 하한 미달(SKIP)",
                 "n_injected": n_injected, "excluded_train": n_excluded_train,
                 "excluded_warmup": n_excluded_warmup})

    sub = df[df["C64"].astype(str).isin(set(wafers))]
    F = extract_features(sub)
    present = [w for w in wafers if w in F.index]
    F = F.reindex(present).fillna(0.0)
    X = scaler.transform(F.reindex(columns=feat_cols, fill_value=0.0))
    sc = apply_calibration(scorer.ae_raw(X), calib)
    qual = float(calib.get("qual_threshold", gate["val_p95_max"]))
    rate = float(np.mean(sc > qual))

    by_scn, by_ch = {}, {}
    idx = pd.Series(sc, index=present)
    meta = lab.set_index("C64")
    for key, col in (("scenario_id", by_scn), ("C24", by_ch)):
        if key not in meta.columns:
            continue
        g = meta.reindex(present)[key].astype(str)
        for v in sorted(set(g.dropna())):
            m = (g == v).values
            col[v] = {"n": int(m.sum()), "detect": round(float(np.mean(idx.values[m] > qual)), 4)}

    ok = rate >= float(gate["label_min_detect"])
    checks = [_c("E1_정답지_검출률", ok,
                 f"{rate:.4f} ≥ {gate['label_min_detect']} ({len(present)}장{warm_note})")]
    detail = {"n": len(present), "judged": True, "detect_rate": round(rate, 4),
              "qual_threshold": qual, "by_scenario": by_scn, "by_chamber": by_ch,
              "n_injected": n_injected,
              # 구 구현은 `excluded_train` 에 **warmup 제외 건수**를 넣었다 (`n_all` 이 이미
              # train 제외 후 값이라). 두 값을 분리한다 — 리포트만 보고 "학습에 주입이
              # 얼마나 들어갔나"를 답할 수 있어야 한다 (오염 진단의 1차 지표).
              "excluded_train": n_excluded_train,
              "excluded_warmup": n_excluded_warmup,
              "note": "라벨된 실제 주입 구간 채점 — 학습분·warmup 제외 (C군 프로브의 사각 보완)"}
    return checks, detail


# ── 승인 브리프 블록 (2026-08-09, 문서 §7 — PM 코멘트 ②) ────────────────────
# 왜 리포트 안에서 만드는가: `_weakened` 는 **키 이름 목록**이라 (`['l5_budget',
# 'require_labels']`) 승인자가 그것만 보고 "무엇이 얼마나 느슨한지"를 알 수 없었다.
# 게이트 항목·임계의 단일 소스가 이 모듈(헌법 3-3 ③ ⓑ)이므로, 그 뜻을 아는 유일한 자리도
# 여기다 — 하류(gateway·프론트)가 키 이름을 한국어로 번역하기 시작하면 임계를 바꿀 때마다
# 두 곳을 고쳐야 하고, 안 고쳐도 아무 일도 안 일어난다 (헌법 7장 "신설하고 하류를 안 따라감").
WEAKENED_LABELS = {
    "l5_budget": ("B1 오경보 예산", "홀드아웃 오경보율 상한을 원 목표보다 올린 상태"),
    "chamber_l5_max": ("B5 챔버별 오경보 상한", "챔버 단위 오경보 상한을 올린 상태"),
    "recon_rmse_max": ("B2 재구성 RMSE 상한", "재구성 오차 허용을 키운 상태"),
    "val_p95_max": ("B4 VAL P95 상한", "정상 표본 상위 분위 허용을 키운 상태"),
    "calib_mean_tol": ("B3 캘리 평균 허용편차", "캘리브레이션 중심 허용폭을 키운 상태"),
    "label_min_wafers": ("E1 판정 표본 하한", "이 값을 올리면 오염 검사가 SKIP 으로 꺼진다"),
    "label_warmup_k": ("E1 warmup 제외 구간", "판정에서 빼는 초기 구간을 늘린 상태"),
    "probe_min_detect": ("C1 프로브 검출률 하한", "요구 검출률을 낮춘 상태"),
    "label_min_detect": ("E1 정답지 검출률 하한", "요구 검출률을 낮춘 상태"),
    "min_val_wafers": ("D4 적합 표본 하한", "캘리 적합 표본 하한을 낮춘 상태"),
    "min_gate_wafers": ("D1 홀드아웃 하한", "오경보율 측정 표본 하한을 낮춘 상태"),
    "chamber_min_wafers": ("B5 챔버 표본 하한", "챔버 판정 표본 하한을 낮춘 상태"),
    "anchor_gap_min_ratio": ("D2 앵커 간격 하한", "앵커 뭉침 허용을 늘린 상태"),
    "require_labels": ("E0 정답지 필수", "정답지 없이 통과 가능 — 학습셋 오염 방어 0 (K-1)"),
}
# §1-4 가드 — v1 개정 원문 승계. 예산 완화에 걸린 회귀 조건이라 브리프에 **항상** 병기한다.
L5_BUDGET_GUARD = ("시뮬 Scorecard 또는 운영에서 미탐 1건 검출 시 즉시 구기준 복귀 "
                   "(복귀 목표 = 원 목표 l5 0.02) + 원인 규명 전 재완화 금지")


def build_approval_brief(gate: dict, checks: list[dict], verdict: str,
                         labels_path=None, probe: dict | None = None) -> dict:
    """승인 브리프용 요약 블록 — 약화 조건 · 경계 라벨 · 잔여 리스크 (문서 §7).

    승인자가 브리프에서 반드시 봐야 하는 3가지를 리포트에 못 박는다:
      ⓐ **약화 조건** — 어떤 항목이 기본값보다 느슨한 채로 판정됐는가 (`_weakened` 를
        사람 말로 옮기고 기본값·현재값을 함께 준다).
      ⓑ **경계 라벨** — 표본이 작아 "확정"이 아닌 항목 (B1·B5 의 `verdict_band`).
      ⓒ **잔여 리스크** — 검사가 아예 돌지 않은 항목 (E1 미실행 등).

    브리프 문구를 여기서 만들어야 `gate_params` 를 바꿀 때 문구가 자동으로 따라온다.
    """
    weakened = list(gate.get("_weakened") or [])
    items = []
    for key in weakened:
        label, why = WEAKENED_LABELS.get(key, (key, "기본값보다 느슨한 설정"))
        item = {"key": key, "label": label, "why": why,
                "baseline": DEFAULTS.get(key), "value": gate.get(key)}
        if key == "l5_budget":
            item["guard"] = L5_BUDGET_GUARD            # §1-4 — 가드 걸린 완화
        items.append(item)

    boundaries = [{"check": c["check"], "band": c["verdict_band"],
                   "ci": [c["band"]["ci_lo"], c["band"]["ci_hi"]],
                   "budget": c["band"]["budget"], "rate": c["band"]["rate"],
                   "detail": c["detail"]}
                  for c in checks if c.get("band", {}).get("boundary")]

    risks = []
    # C1 조합 완화 창 — bool 스위치라 `_weakened` 축(up/down)으로 못 잡는 완화다.
    # 판정을 바꾸진 않지만 "v1 이면 막혔을 조합"이므로 승인자에게 올린다 (§3-2 보강).
    if (probe or {}).get("in_leniency_window"):
        risks.append(
            f"C1 가중 채점이 산술 대비 {probe['leniency_gap']:+.4f} — 저가중 채널(C57 w=0.3 등)에 "
            f"검출 사각이 몰려 있다. 판정값 {probe.get('at_gate')} 는 통과지만 산술 "
            f"{probe.get('at_gate_arithmetic')} 는 v1 기준(0.875)에도 미달한다 — "
            "채널별 검출률(`probe.by_channel_at_gate`)을 확인할 것")
    if not labels_path:
        risks.append("E1(정답지 대조 검출률) 미실행 — 학습셋 오염을 직접 검사한 항목이 없다. "
                     "차기 challenger 부터 상시 라운드로 개통 (문서 §4)")
    for c in checks:
        if c["pass"] and c.get("skip"):
            risks.append(f"{c['check']} SKIP — {c['detail']}")
    if boundaries:
        risks.append(f"경계 라벨 {len(boundaries)}종 — 표본이 작아 예산 이내임이 "
                     f"'확정'이 아니다 (CI 반폭 ±1%p 를 넘으려면 홀드아웃 ~1000장 — "
                     f"문서 §1-5, WP-4)")

    return {"verdict": verdict,
            "weakened_count": len(items), "weakened": items,
            "boundary_count": len(boundaries), "boundaries": boundaries,
            "residual_risks": risks,
            "note": "게이트 항목·임계의 단일 소스는 validate_bundle.py + params.yaml "
                    "`ct.ct2_gate_*` 다 (헌법 3-3 ③ ⓑ). 항목 삭제·완화는 PM 승인 필수."}


# ── 판정 조립 ──────────────────────────────────────────────────────────────
def decide_verdict(checks: list[dict]) -> tuple[str, list[str], list[str]]:
    """검사 목록 → `(verdict, failed, skipped)` (G0-4).

    우선순위는 **FAIL > SKIP > PASS** 다. 하나라도 떨어지면 FAIL 이고, 떨어진 건 없지만
    판정을 못 한 항목이 있으면 SKIP 이다. SKIP 을 PASS 로 접으면 "검사를 못 했다"가
    "통과했다"로 둔갑한다 — 오염이 심할수록 E1 평가 표본이 줄어 SKIP 이 나기 쉬우므로
    (K-2), 그 방향의 관용은 정확히 위험한 쪽으로 작동한다.
    """
    failed = [c["check"] for c in checks if not c["pass"]]
    skipped = [c["check"] for c in checks if c["pass"] and c.get("skip")]
    verdict = VERDICT_FAIL if failed else (VERDICT_SKIP if skipped else VERDICT_PASS)
    return verdict, failed, skipped


def validate(bundle_dir, data_path, params_path=None, val_frac=None,
             device=None, input_schema="auto", labels_path=None) -> dict:
    """번들 + 학습 데이터 → 게이트 리포트 딕셔너리 (`gate_verdict` 가 최종 판정).

    Args:
        labels_path: 주입 정답지 라벨 CSV (`build_clean_wafer_list.py` 산출).
            **E군(오염 검사)의 유일한 입력**이다. 미제공은 E0 이 FAIL 로 잡는다
            (`ct2_gate_require_labels: false` 로 명시 면제 가능 — 실운영 전환용).
    """
    from .retrain import VAL_WAFERS_SIDECAR, load_data, prepare_input  # torch 무의존

    bundle_dir = Path(bundle_dir)
    gate = load_gate_params(params_path)
    A = io_bundle.ARTIFACT_NAMES

    # ① 파일·해시 무결성 — manifest/데이터를 읽기 전에 (check_bundle_files docstring)
    checks = check_bundle_files(bundle_dir)
    metrics: dict = {}
    probe: dict = {}
    label_detail: dict = {}
    val_src, adaptation = "n/a", {}
    val_wafers: list = []
    gate_wafers: list = []
    train_wafers: set = set()
    val_device: str | None = None

    if all(c["pass"] for c in checks):
        df_raw = load_data(data_path)
        df_raw, adaptation = prepare_input(df_raw, input_schema)
        df, _ = prepare_frame(df_raw)
        side = bundle_dir / VAL_WAFERS_SIDECAR
        if side.exists():                                 # E군의 "학습분 제외"용 (없으면 빈 집합)
            train_wafers = {str(w) for w in (io_bundle.load_json(side).get("train") or [])}
        try:
            val_wafers, gate_wafers, val_src = resolve_val_wafers(bundle_dir, df, val_frac)
        except Exception as e:                            # noqa: BLE001 — 판정으로 환원
            checks.append(_c("A3_VAL_세트_해시", False, f"VAL 복원 실패: {e}", critical=True))
            val_wafers = []
        if val_wafers:
            checks += check_set_hashes(bundle_dir, val_wafers, gate_wafers, gate)
        else:
            # ★fail-open 봉인: VAL 0장(사이드카 빈 목록·재유도 0장)이면 아래 채점 블록이
            # 통째로 건너뛰어져 A1·A2만 통과한 상태로 `gate_pass=True` 가 됐다. 배포 결정의
            # 단일 소스(3-3 ③)가 조용히 열리는 경로라, 채점 불가를 **명시적 배포 불가**로
            # 환원한다. 게이트는 "정상 생산자 가정"이 아니라 자체로 닫혀 있어야 한다.
            if all(c["pass"] for c in checks):            # 복원 실패 FAIL이 이미 없을 때만 추가
                checks.append(_c("A3_VAL_세트_해시", False,
                                 "VAL 0장 — 사이드카/재유도 결과 빈 목록 (채점 불가 = 배포 불가)",
                                 critical=True))

    if val_wafers and all(c["pass"] or c["check"] == "D3_게이트_홀드아웃" for c in checks):
        # torch 의존은 **실제 스코어링 직전에만** 건다. 함수 최상단에 두면, 채점에 도달하지
        # 않는 경로(A군 무결성 FAIL · VAL 0장 봉인)까지 torch 를 요구해 **판정(exit 2) 대신
        # 실행 오류(exit 1)로 강등**되고 리포트조차 남지 않는다 — `check_bundle_files` 가
        # 파일·해시 검사를 앞으로 당긴 것과 같은 논리다. torch 로드 차단은 이 팀에서 실제로
        # 일어난 사고다(WDAC/detached — docs/adr/TSR-0001).
        from .model import load_model, Scorer
        spec = io_bundle.load_json(bundle_dir / A["feature_spec"])
        feat_cols = spec["columns"]
        model = load_model(bundle_dir / A["model"],
                          d=int(spec.get("n_features", len(feat_cols))), device=device)
        scorer = Scorer.load_stats(bundle_dir / A["scorer"], model, device=device)
        scaler = io_bundle.load_scaler(bundle_dir / A["scaler"])
        calib = io_bundle.load_json(bundle_dir / A["calib"])
        # K-7 — **검증 device 를 기록한다.** manifest 에는 학습 device 만 있어서, CPU/CUDA
        # 부동소수 차이로 임계 근처(예: C1 0.8868 vs 0.90)의 판정이 흔들려도 어느 장치에서
        # 잰 값인지 리포트만으로 알 수 없었다.
        val_device = getattr(scorer, "device", None)

        def _feats(wafers):
            """wafer 목록 → 피처 행. 데이터에 없는 wafer 를 0행으로 합성하지 않는다.

            `reindex(...).fillna(0)` 만 쓰면 `--data` 가 다른/절단된 CSV일 때 전부 0인
            가짜 정상 행이 만들어져 그대로 채점된다 (진단 불가능한 통과/실패).
            """
            sub = df[df["C64"].astype(str).isin(set(wafers))]
            F = extract_features(sub)
            present = [w for w in wafers if w in F.index]
            return F.reindex(present).fillna(0.0), [w for w in wafers if w not in F.index]

        feats_val, miss_val = _feats(val_wafers)
        feats_gate, miss_gate = _feats(gate_wafers) if gate_wafers else (None, [])
        missing = miss_val + miss_gate
        checks.append(_c("A6_세트_행_실존", not missing,
                         f"{len(missing)}장 데이터에 없음 (예: {missing[:3]}) — "
                         "--data 가 학습 입력과 다르거나 절단됨" if missing
                         else f"VAL {len(feats_val)}장" +
                              (f" + 홀드아웃 {len(feats_gate)}장" if gate_wafers else ""),
                         critical=True))

        if not missing:
            X_val = scaler.transform(feats_val.reindex(columns=feat_cols, fill_value=0.0))
            # B군은 **calib 미적합 홀드아웃**에서 잰다 (있으면). 없으면 VAL 로 퇴화하며
            # 그 사실은 D3가 FAIL 로 드러낸다 — 조용한 항진명제 금지.
            if gate_wafers:
                X_b = scaler.transform(feats_gate.reindex(columns=feat_cols, fill_value=0.0))
                n_b, b_src, on_hold = len(gate_wafers), "holdout", True
            else:
                X_b, n_b, b_src, on_hold = X_val, len(val_wafers), "val(적합 표본 — 퇴화)", False
            # 챔버 라벨 — B군 분해 병기용 (판정 무관, 없으면 분해 생략)
            b_wafers = gate_wafers if gate_wafers else val_wafers
            cham = (df.groupby("C64")["C24"].first().astype(str).reindex(b_wafers).tolist()
                    if "C24" in df.columns else None)
            qd_checks, metrics = check_quiet_and_thresholds(scorer, X_b, calib, gate, n_b,
                                                            on_holdout=on_hold, chambers=cham)
            metrics["b_group_sample"] = b_src
            checks += qd_checks
            # 프로브는 적합 표본(VAL)에 주입한다 — 홀드아웃보다 표본이 크고, C군은
            # "임계 대비 민감도"라 적합 여부에 영향받지 않는다.
            # ⚠️ 캘리 구조가 깨졌으면 **건너뛴다** — probe_detection 도 apply_calibration 을
            # 호출하므로, 여기서 막지 않으면 D2 가 잡아낸 계약 위반이 np.interp 예외로
            # 새어나가 판정(exit 2)이 실행 오류(exit 1)로 강등된다 (리포트 미기록).
            if metrics.get("scored"):
                pr_checks, probe = probe_detection(scorer, scaler, feats_val, feat_cols,
                                                   calib, gate)
                checks += pr_checks
            else:
                checks.append(_c("C1_프로브_검출률", False,
                                 "캘리 구조 위반으로 채점 불가 (D2 참조)", critical=True))
            # w_vec 정합 — scorer 통계와 부록 A 가중이 어긋나면 점수 계약이 흔들린다.
            w_ok = np.allclose(scorer.w_vec, feat_weight_vector(feat_cols))
            checks.append(_c("A4_채널가중_정합", bool(w_ok),
                             "scorer.w_vec == feat_weight_vector(부록 A)" if w_ok
                             else "불일치 — 세트 동기 확인(불가침 12)", critical=True))

            # ── E군: 정답지 대조 검출률 ────────────────────────────────────
            # 라벨 미제공 시 **E1 을 만들지 않는다** — 그 사실은 아래 E0 이 판정한다.
            # 구 구현은 여기서 `pass=True` 레코드를 남겼는데, "생략"과 "통과"가 같은 값으로
            # 표현돼 오염 검사가 없는 채로 `gate_pass: true` 가 났다 (G0-1 fail-open).
            if not labels_path:
                pass
            elif not metrics.get("scored"):
                checks.append(_c("E1_정답지_검출률", False,
                                 "캘리 구조 위반으로 채점 불가 (D2 참조)", critical=True))
            else:
                try:
                    labels = load_injection_labels(labels_path)
                    if not train_wafers:
                        log.warning("사이드카에 train 목록 없음 → E군 '학습분 제외' 미적용 "
                                    "(구 번들). 검출률이 낙관적으로 나올 수 있다.")
                    lb_checks, label_detail = check_label_detection(
                        scorer, scaler, df, feat_cols, calib, gate, labels, train_wafers)
                    label_detail["train_excluded_available"] = bool(train_wafers)
                    label_detail["labels_path"] = str(labels_path)
                    checks += lb_checks
                except Exception as e:                    # noqa: BLE001 — 판정으로 환원
                    checks.append(_c("E1_정답지_검출률", False,
                                     f"라벨 판독 실패: {e}", critical=True))

    # ── E0 · E2: 오염 검사의 **전제** 자체를 검사한다 (bundle 무관 — 항상 붙인다) ──
    checks += check_labels_provided(labels_path, gate)
    checks += check_label_symmetry(bundle_dir, labels_path)

    verdict, failed, skipped = decide_verdict(checks)
    return {
        # `gate_pass` 는 하위호환 유지 필드다 — 소비자(ct2_deploy_approval·orchestrator)가
        # 이 키를 본다. **SKIP 도 False** 라 판정 불가가 배포로 새지 않는다.
        "gate_pass": verdict == VERDICT_PASS,
        "gate_verdict": verdict,                  # PASS/FAIL/SKIP (계약 §8-E — CT①과 표기 통일)
        "gate_schema_version": GATE_SCHEMA_VERSION,
        "failed_checks": failed,
        "skipped_checks": skipped,
        "checks_expected": sorted({c["check"] for c in checks}),
        "checks": checks,
        # 승인 브리프가 그대로 렌더할 요약 (약화 조건·경계 라벨·잔여 리스크 — 문서 §7).
        # 하류(gateway·프론트)가 `gate_params._weakened` 키 이름을 번역하지 않게 하는 자리다.
        "approval_brief": build_approval_brief(gate, checks, verdict, labels_path, probe),
        "metrics": metrics,
        "probe": probe,
        "label_detection": label_detail,          # E군 상세 (시나리오·챔버별 검출률)
        "bundle": str(bundle_dir),
        "bundle_name": bundle_dir.name,
        "data_path": str(data_path),
        "labels_path": str(labels_path) if labels_path else None,
        "val_source": val_src,
        "val_device": val_device,                 # K-7 — 검증 device (manifest 는 학습 device)
        "set_sizes": {"val": len(val_wafers), "gate_holdout": len(gate_wafers)},
        "input_adaptation": adaptation,
        "gate_params": gate,
        "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate_source": "ae_pipeline/validate_bundle.py (헌법 3-3 ③ ⓑ 위임 — 항목·임계 단일 소스)",
    }


# ── E0 · E2 — 오염 검사의 전제 (G0-1 · K-5) ────────────────────────────────
def check_labels_provided(labels_path, gate: dict) -> list[dict]:
    """E0 — 정답지 제공 여부. **미제공은 FAIL 이다** (2026-08-05 G0-1).

    근거: 게이트가 오염 방어의 단일 방어선이라는 전제(PM 지시)를 코드에 못 박는 항목이다.
    B군은 오염 시 calib 앵커가 부풀려져 오경보를 **낮게** 보고하고(은폐 신호), C군 프로브는
    학습 이후 합성 교란이라 오염을 원리적으로 못 잡는다(실증: 프로브 97.8% PASS / 실제 주입
    검출 0.6%). 라벨 없이 오염을 잡는 신호는 현 게이트에 **0개**다 — 그래서 미제공은 통과가
    아니라 차단이다.

    실운영 전환(정답지 부재)은 `ct2_gate_require_labels: false` 로 **명시 면제**하며, 면제
    사실은 `gate_params._weakened` 에 남아 승인 브리프에 노출된다 (리스크 K-1 기록 의무).
    """
    if labels_path:
        return [_c("E0_라벨_제공", True, f"정답지 제공됨: {labels_path}")]
    if not gate.get("require_labels", True):
        return [_c("E0_라벨_제공", True,
                   "정답지 미제공 — `ct2_gate_require_labels: false` 로 **명시 면제**. "
                   "이 상태에서 학습셋 오염 방어는 0이다 (K-1). 승인 브리프에 적시할 것")]
    return [_c("E0_라벨_제공", False,
               "정답지 미제공 — 학습셋 오염 검사 불가. 게이트가 오염 방어의 단일 방어선이므로 "
               "미제공은 FAIL 이다 (헌법 3-3 ③ ⓑ). `--labels <injection_labels.csv>` 로 제공하거나 "
               "`ct2_gate_require_labels: false` 로 명시 면제하라", critical=True)]


def check_label_symmetry(bundle_dir: Path, labels_path) -> list[dict]:
    """E2 — 학습/검증 라벨 **대칭** (K-5). manifest `selection.injection_excluded` 와 대조.

    비대칭 2종을 잡는다:
      ⓐ 학습은 `--labels` 로 주입을 제외했는데 검증은 라벨 없이 — 오염 검사가 사라진다.
      ⓑ 학습이 라벨 없이 돌았는데(정상셋에 주입 포함) 검증만 라벨로 — E1 이 잡을 테니
         FAIL 은 아니지만, **학습셋이 오염됐을 수 있다**는 사실을 항목으로 남긴다.

    manifest 를 못 읽으면 검사를 생략한다(A1·A2 가 이미 그 사실을 FAIL 로 잡는다).
    """
    try:
        sel = (io_bundle.read_manifest(bundle_dir).get("selection") or {})
    except Exception as e:                                    # noqa: BLE001
        return [_c("E2_라벨_대칭", True, f"manifest 판독 불가 — 검사 생략 (A1·A2 참조): {e}")]
    trained_clean = bool(sel.get("injection_excluded"))
    if trained_clean and not labels_path:
        return [_c("E2_라벨_대칭", False,
                   "학습은 라벨 제외 경로(injection_excluded=true)인데 검증은 라벨 없음 — "
                   "오염 검사가 빠진 채 통과할 조합이다", critical=True)]
    if not trained_clean and labels_path:
        return [_c("E2_라벨_대칭", True,
                   "학습이 라벨 제외 없이 돌았다(injection_excluded=false) — 학습셋에 주입이 "
                   "섞였을 수 있다. E1 검출률로 판정한다")]
    return [_c("E2_라벨_대칭", True,
               f"학습·검증 라벨 사용 일치 (injection_excluded={trained_clean})")]


# ── 리포트 영속화 (프로세스 간 공유 JSON — 헌법 7장) ──────────────────────────
def _json_safe(obj):
    """비유한 수치(NaN/Inf)를 `None` 으로 정규화 — 표준 JSON 보장 (헌법 7장).

    `metrics` 의 `recon_rmse`·`val_mean`·`val_p95` 는 전부 `float(np.…)` 산출이라
    표본이 퇴화하면 NaN 이 될 수 있다. 그대로 두면 `json.dumps` 가 비표준 리터럴
    `NaN` 을 쓰고, 배포 판정 근거 파일이 소비자(JS `JSON.parse`)에서 깨진다.
    `allow_nan=False` 만 걸면 이번엔 리포트 쓰기 자체가 ValueError 로 죽으므로,
    **먼저 정규화하고 그 다음 엄격 직렬화**한다 (consumer.`_finite` 와 같은 처방).
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, (int, float)):
        return obj if math.isfinite(float(obj)) else None
    return obj


REPLACE_ATTEMPTS = 5          # Windows 잠금 대비 재시도 횟수 (헌법 7장)
REPLACE_BACKOFF_SEC = 0.05    # 초기 대기 — 시도마다 2배 (총 ≈0.75s 상한)


def _replace_with_retry(tmp: Path, out: Path,
                        attempts: int = REPLACE_ATTEMPTS,
                        backoff: float = REPLACE_BACKOFF_SEC) -> None:
    """`os.replace` + **짧은 backoff 재시도** (헌법 7장 — "Windows는 잠금 대비 필요").

    POSIX 에서 `os.replace` 는 원자적이지만, Windows 는 **대상 파일에 열린 핸들이
    있으면 `PermissionError`(WinError 5)** 를 낸다. 읽는 쪽
    (`ct2_deploy_approval.validate_gate_report`)이 리포트를 여는 순간과 겹치면
    교체가 실패하고, 그러면 게이트가 판정을 내고도 근거 파일을 갱신하지 못한다.
    실운영이 Windows 이므로 짧게 여러 번 시도한다.

    `PermissionError` 만 재시도한다 — 다른 `OSError`(경로 오류·디스크 등)는 기다려도
    풀리지 않으므로 즉시 올린다(무의미한 지연 방지).
    """
    for i in range(attempts):
        try:
            os.replace(tmp, out)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            log.warning("리포트 교체 잠금 — %.2fs 후 재시도 (%d/%d): %s",
                        backoff * (2 ** i), i + 1, attempts, out)
            time.sleep(backoff * (2 ** i))


def write_report(report: dict, out) -> Path:
    """게이트 리포트를 **원자 교체**로 저장 (같은 디렉토리 tmp → `os.replace`).

    이 파일은 프로세스 간 공유물이다 — `ct2_orchestrator` 가 존재를 확인하고
    `ct2_deploy_approval.validate_gate_report()` 가 읽어 배포를 판정한다. 제자리
    덮어쓰기는 읽는 쪽에 **부분 쓰기**를 노출시켜, PASS 리포트가 파손 판정으로
    둔갑할 수 있다 (헌법 7장 "공유 JSON 을 열어놓고 제자리 덮어쓰기 금지" — tmp +
    `os.replace` + Windows 잠금 backoff 재시도).
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")       # 같은 디렉토리 = 같은 파일시스템
    tmp.write_text(json.dumps(_json_safe(report), ensure_ascii=False, indent=2,
                              allow_nan=False), encoding="utf-8")
    try:
        _replace_with_retry(tmp, out)
    except OSError:
        tmp.unlink(missing_ok=True)                 # 잔여 tmp 청소 (다음 실행 혼선 방지)
        raise
    return out


# ── CLI ───────────────────────────────────────────────────────────────────
def build_argparser():
    """CLI 파서 구성 (게이트 실행 인자)."""
    ap = argparse.ArgumentParser(
        prog="ae_pipeline.validate_bundle",
        description="CT² challenger 번들 자동 검증 게이트 (exit 0=PASS / 2=FAIL / 3=SKIP / 1=오류)")
    ap.add_argument("--bundle", required=True, help="challenger 번들 폴더")
    ap.add_argument("--data", required=True, help="학습에 쓴 데이터 (VAL 복원용)")
    ap.add_argument("--out", default=None,
                    help=f"리포트 JSON 경로 (기본 <bundle>/{VALIDATION_REPORT})")
    ap.add_argument("--params", default=None, help="params.yaml 경로 override")
    ap.add_argument("--val-frac", type=float, default=None,
                    help="사이드카 없을 때 재유도용 VAL 비율 (기본 manifest 값)")
    ap.add_argument("--input-schema", choices=["auto", "merged", "ct2"], default="auto")
    ap.add_argument("--device", default=None)
    ap.add_argument("--labels", default=None,
                    help="주입 정답지 라벨 CSV (build_clean_wafer_list.py 산출) — "
                         "주면 E군(정답지 대조 검출률) 활성화")
    return ap


def main(argv=None) -> int:
    """CLI 진입점 — 리포트 저장 후 게이트 판정을 exit code 로 반환.

    exit: 0=PASS · 2=FAIL · **3=SKIP(판정 불가 — PASS 아님)** · 1=실행 오류.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ [ae.validate] %(message)s")
    args = build_argparser().parse_args(argv)

    try:
        report = validate(args.bundle, args.data, params_path=args.params,
                          val_frac=args.val_frac, device=args.device,
                          input_schema=args.input_schema, labels_path=args.labels)
    except Exception as e:                                 # noqa: BLE001 — 실행 오류는 1로 구분
        log.error("게이트 실행 오류(판정 불가): %s", e)
        return EXIT_ERROR

    out = write_report(report, args.out or Path(args.bundle) / VALIDATION_REPORT)

    for c in report["checks"]:
        mark = "OK " if c["pass"] and not c.get("skip") else ("SKIP" if c["pass"] else "★FAIL")
        log.info("  %-4s %-22s %s", mark, c["check"], c["detail"])

    # 챔버별 분해 병기 — 총계만 보면 원인을 못 짚는다 (2026-08-03 실측: 총계 8.00% 가
    # 챔버별로는 1.1%~35.0%). 판정에는 쓰지 않고 항상 보여준다.
    b5 = next((c for c in report["checks"] if c["check"] == "B5_챔버별_L5"), {})
    b5_bands = b5.get("bands_by_chamber") or {}
    for ch, v in (report["metrics"].get("by_chamber") or {}).items():
        bd = b5_bands.get(ch)
        log.info("     └ %-10s n=%-4d L5 %6.2f%%  평균 %.4f  P95 %.4f%s",
                 ch, v["n"], v["l5"] * 100, v["mean"], v["p95"],
                 f"  CI[{bd['ci_lo'] * 100:.2f}, {bd['ci_hi'] * 100:.2f}]% {bd['band']}"
                 if bd else "")
    ld = report.get("label_detection") or {}
    if ld.get("judged"):
        log.info("     └ 정답지 검출 %.2f%% (%d장)%s", ld["detect_rate"] * 100, ld["n"],
                 "" if ld.get("train_excluded_available") else " ※학습분 제외 미적용(구 번들)")
        for k, v in (ld.get("by_scenario") or {}).items():
            log.info("        · %-22s n=%-4d 검출 %6.2f%%", k, v["n"], v["detect"] * 100)
    # 승인 브리프 블록 — 약화·경계·잔여 리스크를 "키 이름"이 아니라 사람 말로 남긴다.
    ab = report.get("approval_brief") or {}
    if ab.get("weakened"):
        log.warning("★임계 완화 상태로 판정됨 — %d건 (승인 브리프에 노출 필요, 헌법 3-3 ③ ⓑ)",
                    ab["weakened_count"])
        for w in ab["weakened"]:
            log.warning("     └ %s (%s): 기본 %s → 현재 %s — %s",
                        w["label"], w["key"], w["baseline"], w["value"], w["why"])
            if w.get("guard"):
                log.warning("        가드: %s", w["guard"])
    for b in ab.get("boundaries") or []:
        log.warning("★경계 라벨 — %s %s (CI[%.2f, %.2f]%% vs 예산 %g%%). 표본이 작아 "
                    "'예산 이내'가 확정이 아니다", b["check"], b["band"],
                    b["ci"][0] * 100, b["ci"][1] * 100, b["budget"] * 100)
    for r in ab.get("residual_risks") or []:
        log.warning("★잔여 리스크 — %s", r)
    verdict = report["gate_verdict"]
    if verdict == VERDICT_PASS:
        log.info("게이트 PASS — %d개 항목 전부 통과 (schema v%s) → %s",
                 len(report["checks"]), report["gate_schema_version"], out)
        return EXIT_PASS
    if verdict == VERDICT_SKIP:
        log.error("게이트 SKIP — %s → %s (판정 불가 = 배포 불가. PASS 로 오해 금지)",
                  ", ".join(report["skipped_checks"]), out)
        return EXIT_SKIP
    log.error("게이트 FAIL — %s → %s (challenger 보존·미배포)",
              ", ".join(report["failed_checks"]), out)
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
