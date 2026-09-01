# ae_v3 — 피처 · 스코어 정리

> **대상**: 모델 최종 버전 `ae_v3` (채택 모델 = **z16 rev3**, G1 PASS → P5a 확정 → **P6 G3 PASS**)
> **근거 파일**: `ae_v3.ipynb`(P2/P4/P6 셀) · `ae_config.yaml` · `artifacts/{feature_spec.json, calib.json}` · `REPORT/ae_v3_REPORT_05_P6_G3.md`
> **성격**: 살아있는 참조 문서 — 피처·스코어 정의가 바뀌면 이 파일을 갱신한다.

---

## 1. 개요

`ae_v3`는 **wafer 1장**을 관측 단위로 하는 집계형 MLP-AutoEncoder 이상탐지 모델이다. 시간축 시퀀스가 아니라 **레벨축**(정착 T=2)에서 신호를 요약한 **83개 피처**를 입력받아, 재구성 잔차를 단일 계약 스코어 `ae_raw`로 낸다. `ae_raw`는 캘리브레이션을 거쳐 `ae_score`[0,1]가 되고, 그 스트림의 누적 읽기가 `ae_drift_score`[0,1]다.

| 항목 | 값 |
|---|---|
| 아키텍처 | MLP-AE  D→64→32→**z16**→32→64→D (GELU·LayerNorm·dropout 0.05) |
| 입력 피처 수 (D) | **83** (`feature_spec.json`) |
| 관측 단위 | wafer (`C64`) |
| 스케일러 | QuantileTransformer(normal), fit=TRAIN(3,922)만 |
| 출력 계약 | **단일 `ae_raw`** (불가침 12 — 계약 불변) → `ae_score` → `ae_drift_score` |
| 알람 권한 | 없음 — `fdc.prediction` 발행, B(Context Score)가 합산 |

---

## 2. 피처 구성 — wafer × 센서 × 통합방식 × step (4축)

피처는 아래 **4개 축의 조합**으로 만들어진다. "어떤 wafer의 / 어떤 센서를 / 어떤 step 구간에서 / 어떤 방식으로 요약했나"가 곧 피처 하나다.

### 2.1 4개 축

| 축 | 내용 |
|---|---|
| **① wafer** (`C64`) | 피처 행렬의 **행 = wafer 1장**. dedup 가드 `(C64,C7,C46)` + split 드랍 후 wafer별 groupby. |
| **② 센서** (물리 C코드만) | **정착연속 10** (C11·C17·C9·C52·C15·C16·C31·C63·C58·C57) + **과도 5** (C18·C27·C32·C62·C61) + **이산 4** (C49·C54·C56·C50) + **MUX 2** (C59·C60 → `C5960_active` 재구성). 식별자·카운터(C33)·시간(C10/C39/C41)·setpoint(C12)·타깃(C65)은 **입력금지**(불가침 1·7 · 금지입력 27종). |
| **③ 통합방식** (aggregation) | 연속 요약 **mean·std·min·max** + 과도 모양 **파생 스칼라**(slope·mode·sum·delta·peak·nunique·count). |
| **④ step** (윈도우) | **Step4**(`C7==4`)를 **정착**(`C42==0`)과 **과도**(`C42==1`)로 분리(불가침 5, 신성불가침). 정착은 정상 레벨, 과도는 점화 순간의 서명. |

> ⚠️ **정직한 한계**: 정착 구간이 **T=2행**이라 정착 std는 2행 기준(집계와 정보량 준동치 — 판단 v1 §3). `C11_wf_min`(F14 레짐 프록시) 병합본은 +0.21σ로 약함.

### 2.2 6개 카테고리 (총 83 피처)

83개 피처는 4축 조합을 따라 아래 **6개 카테고리**로 묶인다. (`extract_features()` 기준, 개수 프로그램 검증 완료)

| # | 카테고리 | 센서 | step | 통합방식 | 개수 | 대표 예시 |
|---|---|---|---|---|---|---|
| **A** | 정착 연속 | 정착연속 10 + `C5960_active` = **11** | 정착 | mean·std·min·max (**×4**) | **44** | `C11_s_mean`, `C17_s_std`, `C5960_active_s_max` |
| **B** | 과도 서명(점화) | C18·C27·C32·C62·C61 = **5** | 과도 | max·mean·min (**×3**) | **15** | `C18_t_max`, `C62_t_min`, `C27_t_mean` |
| **C** | 정착 잔류 | C32·C62·C61 = **3** | 정착 | mean·std (**×2**) | **6** | `C32_sr_mean`, `C61_sr_std` |
| **D** | C54/C56 매칭 위치 | C54·C56 = **2** | 과도+정착 | 과도 mean(주) + 정착 mean·std(보조) | **6** | `C54_t_mean`, `C56_s_mean`, `C54_s_std` |
| **E** | 과도 모양 파생 스칼라 | 혼합 | wafer/과도/정착 | slope·mode·sum·delta·peak·nunique·count | **10** | `C11_wf_min`, `C18_t_peak`, `C62_ignite_delta`, `C50_s4_sum`, `C63_s_slope`, `C49_mode`, `C49_switch`, `transient_len`, `settle_len`, `n_steps` |
| **F** | 완결성 플래그 | 구조 | — | 존재 여부(0/1) | **2** | `has_settle`, `has_transient` |
| | | | | | **83** | |

**카테고리별 역할 요약**

- **A. 정착 연속 (44)** — 안정 구간의 정상 레벨. 모델의 주력 신호. 접미사 `_s_`.
- **B. 과도 서명 (15)** — 점화(ignition) 순간만 잘라낸 과도 신호. heavy-tail·bimodal(C27/C61/C62) 특성. 접미사 `_t_`.
- **C. 정착 잔류 (6)** — 과도 센서(C32/C62/C61)가 정착 구간에 남긴 잔류. 접미사 `_sr_`.
- **D. C54/C56 매칭 (6)** — 매칭 위치 신호. 과도 wafer-mean이 주(7/13 재정정), 정착 mean/std가 보조.
- **E. 과도 모양 (10)** — 파생 스칼라: 점화 스파이크 최심점(`C11_wf_min`), 과도 피크(`C18_t_peak`), 점화 반전(`C62_ignite_delta`=과도 첫샘플−정착), Endpoint 이벤트(`C50_s4_sum`), 정착 드리프트(`C63_s_slope`), 이산상태(`C49_mode`/`C49_switch`), 정착 도달속도(`transient_len`/`settle_len`), 스텝 수(`n_steps`).
- **F. 완결성 (2)** — 정착/과도 구간 존재 여부. 구조결측을 0으로 채운 wafer를 구분하는 플래그.

### 2.3 스케일링 (rev3 핵심 변경)

- **QuantileTransformer(output_distribution=normal)**, **fit=TRAIN(3,922)만**(불가침 3).
- rev2의 RobustScaler(IQR)가 heavy-tail·bimodal 과도피처(C27/C61/C62)의 희소극단을 증폭 → PCA 1성분 97.8%·유효차원 6의 조건수 아티팩트를 유발. rev3는 분위수→정규(랭크기반)로 지배를 제거하고 유효차원을 회복.
- 신규 챔버는 시딩 N장 → QT fit → registry 등록(공용 모델 1 + 챔버별 스케일러).

---

## 3. 스코어 3종

세 스코어는 **모두 단일 `ae_raw`에서 파생**된다(불가침 12). 별도 모델은 없다.

```
피처(83) ──모델──▶ 잔차 ──▶ [ae_raw] ──캘리(calib.json)──▶ [ae_score] ──EWMA──▶ [ae_drift_score]
                          조건화 Maha²         [0,1]                         [0,1]
```

### 3.1 `ae_raw` — 조건화 잔차 Mahalanobis²

원 스코어(single contract). 값이 클수록 이상(higher_is_worse).

- **정의**: 재구성 잔차 `r = x − AE(x)`를 VAL 정상 잔차 분포로 조건화한 Mahalanobis 제곱거리.
- **절차** (`make_scorer`):
  1. VAL 잔차 `rv`의 평균 `μ`·공분산을 **LedoitWolf**로 추정.
  2. 고유분해 후 **고유값 플로어**(`max(wv, wv.max/100)`) → **조건수 ≤ 100** 캡 (rev2 아티팩트 차단).
  3. 조건화 정밀도 `P`로 `ae_raw(X) = (r−μ)ᵀ P (r−μ)`.
- **부산물**: `ae_top_channels`(가중 z² 상위 5개 C코드 — Evidence Card용), 진단용 `ae_raw_diag`(대각)·`ae_raw_full`(무조건 full-Maha) 대조, `eff_rank`(유효차원)·`cond_used`(실사용 조건수).
- **계약**: 직접 알람 없음 → `fdc.prediction`으로 발행, B가 Context Score에 합산.

### 3.2 `ae_score` — [0,1] 캘리브레이션

`ae_raw`를 운영 해석 가능한 [0,1]로 만든 **단조·포화 매핑**.

- **정의**: `ae_score = clip( interp(ae_raw; ANCH_X, ANCH_Y), 0, 1 )` (ECDF 앵커).
- **앵커**: VAL 정상 raw 분위수에 고정.

  | 백분위(VAL) | 50 | 90 | **98.5** | 99.5 | 99.9 |
  |---|---|---|---|---|---|
  | raw 앵커 x (실측) | 36.9 | 88.5 | **179.4** | 254.9 | 399.0 |
  | score y | 0.05 | 0.10 | **0.20** | 0.50 | 1.00 |

- **0.2 = VAL P98.5**에 고정 → **L5 오경보 = 1.53% ≤ 2%** (REPORT_05 실측 확정).
- **Qual 임계값 0.2** (C2 합의 연동). 채택 5기준 전부 PASS.
- `calib.json`은 **모델과 한 몸** — 재학습(CT②) 시 model+scaler+calib 세트로 재산출·버전 동기(`calib_v1`).

### 3.3 `ae_drift_score` — EWMA 레벨

같은 `ae_score` 스트림의 **누적(EWMA) 읽기**. per-wafer가 놓치는 **느린 지속 드리프트·레짐 전환**을 잡는다. 단일 `ae_raw` 후처리(별도 모델 없음).

- **정의**: `ae_drift_score = clip( (EWMA_α(ae_score) − B0) / (0.2 − B0), 0, 1 )`
- **파라미터**: `α = 0.05`(긴 기억) · `B0 = 0.130`(정상 EWMA **P99** — 데이터주도 baseline, 실측 확정) · 알람선 0.2.
- **상태**: **per-chamber stateful** (챔버별 EWMA 상태 유지).
- **역할**: 느린 sub-σ 지속 드리프트 + 레짐 전환 감지.

검증 (REPORT_05):

| 스트림 | per-wafer `ae_score` | `ae_drift_score` |
|---|---|---|
| ① 정상(VAL) | 평균 0.045 | P95 **0.000** · 최대 0.32 (조용) |
| ② 지속 sub-σ(0.05σ) | 중앙 0.114 · >0.2 20.7% (**대체로 침묵**) | 최종 **1.000** |
| ③ seg2 레짐 | 평균 0.356 | 최종 **1.000** (계단·지속) |

> **핵심**: ②에서 per-wafer는 대부분 0.2 아래로 침묵인데 drift는 1.0 — "느린 드리프트를 drift가 잡는다".

### 3.4 세 스코어 상보 관계

| 이상 유형 | `ae_score`(per-wafer) | `ae_drift_score`(누적) |
|---|---|---|
| 급성 스파이크 | ✅ 잡음 | — |
| 느린 지속 드리프트(sub-σ) | ✗ 놓침 | ✅ 잡음 |
| 레짐 전환 | ✅ | ✅ (둘 다) |

---

## 4. 출력 스키마 (`fdc.prediction`)

`ae_score`(0~1) · `ae_raw`(조건화 Maha, 계약불변) · `ae_top_channels[]`(C코드) · `ae_drift_score`(0~1, EWMA 스트림) · `ae_model_version`(z16_rev3_seg1) · `ae_calib_version`(calib_v1) · `transient_context`(bool).

---

## 5. Context Score 연동 — AE 두 채널 사용법

> **출처**: `ae_v3_handoff/ae_pipeline`(model.py·calibration.py·drift.py) + `API_Contract §2` + `Context_Score 문서 §3~4`
> **대상**: B4-1 A·B 인터페이스 합의 / Context Score의 AE 축 스코어링
> **상태**: **A 제안 초안** — 배점·확정은 B4-1(A+B 공동).
> ⚠️ 이 절의 `§n` 참조는 **API_Contract·Context_Score 문서** 기준(본 문서 절 번호 아님).

### 5.1 두 채널 매핑

단일 `ae_raw` 하나에서 나온 3개 산출물(§3)을 Context Score에서는 **2개 채널**로 쓴다.

```
wafer 83피처
 → scaler(QuantileTransformer, fit=TRAIN)
 → MLP-AE(D→64→32→z16→32→64→D, GELU·LayerNorm) → 잔차 r = x − AE(x)
 → [조건화 잔차 Mahalanobis²]            → ① ae_raw        (원점수, unbounded)
      → [ECDF 앵커 캘리]                 → ② ae_score      ([0,1], per-wafer 이상)
           → [EWMA α=0.05, per-chamber]  → ③ ae_drift_score([0,1], 누적 드리프트)
```

| 채널 | 산출물 | 성격 | 감지 대상 |
|---|---|---|---|
| **채널 1 · Anomaly** | ① `ae_raw` / ② `ae_score` | 순간·다변량 | 스파이크, 다변량 관계 붕괴 |
| **채널 2 · Drift** | ③ `ae_drift_score` | 완만·누적 | 느린 sub-σ 드리프트, 레짐 이동 |

**상보 원칙**(README): 스파이크 = 채널1만, 느린 드리프트 = 채널2만, 레짐 전환 = 둘 다.

### 5.2 채널별 계산 요약 (연동 해석 포인트)

계산식 전체는 **§3** 참조. 여기서는 Context Score 연동에 필요한 해석만 짚는다.

- **① `ae_raw`** = `(r−μ)ᵀ P (r−μ)`. **0 이상 unbounded**(χ²형, 높을수록 이상), 자유도 ≈ 유효차원(`eff_rank`). 의미: "정상 잔차 분포에서 다변량으로 몇 σ² 벗어났나". 단일·불변(불가침 12, 재채번·재정의 금지).
- **② `ae_score`** = raw→[0,1] 단조·포화 선형보간. **꼬리확률 눈금** — **0.2 = 상위 1.5%**(VAL P98.5 = Qual 임계), **0.5 = 상위 0.5%**, **1.0 = 상위 0.1%**. P99.9 이상은 1.0 클립. 정상 평균 ≈ 0.05, 정상 P95 < 0.2, L5 오경보 1.53%.
- **③ `ae_drift_score`** = **`ae_score` 스트림의 EWMA**(‼️ `ae_raw` 아님). `E_t = 0.05·score_t + 0.95·E_{t−1}`, `clip((E_t−B0)/(ALARM−B0),0,1)`. **B0 ≈ 0.13**(정상 EWMA P99)에서 0, **ALARM = 0.20**(=Qual)에서 1로 포화 → 유효 밴드폭 겨우 **0.07**. per-chamber stateful — A가 `DriftTracker`로 챔버별 상태를 이어 발행.

### 5.3 두 채널 비교

| 기준 | (a) `ae_raw`에 z (z≥2/z≥3) | (b) `drift_score`[0,1] 밴드 임계 |
|---|---|---|
| **감지 대상** | per-wafer 이상·다변량 붕괴(스파이크) | 느린 레벨 이동(누적 드리프트) |
| **통계 적합성** | Maha²에 z/χ² tail = 정석, 해석 가능 | EWMA 밴드 = 드리프트엔 정석, 단 포화(~1.0) 위 해상도 상실 |
| **계약 영향** | `ae_raw` 필드 **추가** 필요(§2-2, B 리뷰+문서 동기) | **무변경** — `drift_score` 이미 계약에 있음 |
| **작업 위치** | A가 발행 추가(이미 계산 중), B는 z 버킷 구현 | B 스코어링 내부(밴드·튜닝), A는 그대로 |
| **재학습 견고성** | z 자기정규화 → baseline 이동 흡수, 버킷 안정 | B0(~0.13)·ALARM 재산출마다 임계 재튜닝·버전동기 리스크 |
| **튜닝 부담** | 2σ/3σ 보편값, 경량 | 밴드 마진 실측 튜닝(Scorecard) 필요 |
| **해석성** | "3.2σ 이상" — 엔지니어 직관적 | "drift 0.85" — baseline 알아야 해석 |
| **문서 정합** | §4의 z 표기 그대로 유지 | §4를 z→밴드로 재작성 |

### 5.4 Context Score 스코어링 제안 (B4-1)

**채널 1 — Anomaly (①/②)**

- 점수화 입력: **`ae_raw` 권장** (또는 `ae_score`).
- ae_raw 권장 이유: `ae_score`는 P99.9에서 1.0 포화 → 초고심각도 해상도 상실. `ae_raw`는 unbounded라 "얼마나 심한가"를 끝까지 구분.
- 스코어링: `z = (ae_raw − μ_raw)/σ_raw` 표준화 후 **z≥2 → +15 / z≥3 → +25**. (μ_raw·σ_raw = 정상 `ae_raw` 통계, A가 함께 전달.)
- 대안: `ae_score` 퍼센타일 밴드(≥0.2 / ≥0.5 / ≥1.0) — 이미 꼬리확률 눈금이라 계약 추가 없이 감.
- 발화 상황: 순간 스파이크, 다변량 관계 붕괴(단변량 Nelson 사각).

**채널 2 — Drift (③)**

- 점수화 입력: `ae_drift_score` 그대로.
- 스코어링: **[0,1] 밴드 임계**(예: >0.3 → +a, >0.7 → +b). **z 쓰지 말 것** — 이미 B0~ALARM로 정규화·포화된 값이라 z의 2 vs 3 구분이 붕괴함.
- 발화 상황: 완만한 sub-σ 지속 드리프트, PM 후 레짐 이동.
- 주의: per-chamber 누적값. 챔버 리셋(PM/재Qual) 시 **EWMA 상태 초기화 필수** — 안 하면 이전 레짐 드리프트가 새 레짐으로 샌다.

---

## 부록 · 참조

- **피처 정의**: `ae_v3.ipynb` P2 셀(`extract_features`) · `artifacts/feature_spec.json`
- **ae_raw**: P4 셀(`make_scorer`) · **ae_score**: P6-1 셀 · `artifacts/calib.json` · **ae_drift_score**: P6-3 셀
- **실측·판정**: `REPORT/ae_v3_REPORT_05_P6_G3.md` (G3 PASS) · 설정 요약: `ae_config.yaml`
- **§5 연동(Context Score)**: `ae_v3_handoff/ae_pipeline`(model.py·calibration.py·drift.py) · `API_Contract §2` · `Context_Score 문서 §3~4`
