# ae_v3 · REPORT_06 — P7 최종 모델 확정 + 재학습 로직 핸드오프 — **FINAL: CONFIRMED**

> **고정 스냅샷 (immutable).** 발행: 세그 (A트랙·AE) · 2026-07-23
> 실행: `ae_v3_handoff/ae_pipeline` (재학습·추론 모듈) · 아티팩트 = `ae_v3` 배포 세트
> 선행: REPORT_03(rev3 G1 PASS) · REPORT_04(P5a z16 확정) · REPORT_05(P6 G3 PASS) · 계획 §10·§11
> 대상: **B 팀원 B**(출력 스키마) · **PM 윤준호**(CT² 절차·NFR) · **C 팀원 C**(Evidence Card) · **A 팀원 A**(참조)

---

## 0. 요약 (TL;DR)

- **`ae_v3` 최종 모델 확정 ✅** — G1 PASS(REPORT_03) + G3 운영 투입 게이트 PASS(REPORT_05)로
  검증된 **집계 MLP-AE(z16 rev3) + 조건화 잔차 Mahalanobis 스코어**를 v1 AE의 최종 배포 모델로 확정.
- **재학습(CT②) 로직 포함 핸드오프 패키지** 산출 — 원 노트북 재현/재학습 로직을 운영용 Python
  모듈(`ae_pipeline`)로 정리, 아티팩트 self-contained 번들 구성. 재학습은 **model+scaler+scorer
  +calib+drift 세트 통째 재산출·버전 동기**(불가침 12)를 CLI 1회로 수행.
- **핸드오프 확장**: 스코어러 통계(μ·sd·조건화 정밀도 P)를 `scorer.npz`로, drift baseline을
  `drift.json`으로 영속화 → VAL 없이 번들만으로 운영 추론 가능(노트북 대비 수학 동일).
- **모델링 아크 완결**: rev1 미달 → rev2 조건수 아티팩트 검출·기각 → rev3 G1 PASS → P5a z16 확정
  (G2a 기각) → P6 G3 PASS → **P7 최종 확정·핸드오프**.

---

## 1. 확정 근거 (게이트 이력)

| 게이트 | 결과 | 근거 |
|---|---|---|
| G1 (정직 검증) | **PASS** | L2①A 0.981 · L2③ 1.000 · L4 0.999 · L6 OK · 재현성 5해시 (REPORT_03) |
| G2a (축소 챌린저) | **기각** | flatten-MLP 미달 — 계획 사전등록 예측 적중 → z16 유지 (REPORT_04) |
| G3 (운영 투입) | **PASS** | 캘리 5기준 · L5 1.53% · drift · CT² 정상화 · NFR (REPORT_05) |
| **FINAL** | **CONFIRMED** | 위 전부 충족 + 재학습 로직 핸드오프 (본 REPORT_06) |

rev2(잔차 Maha)는 표면 PASS였으나 **조건수 아티팩트**(C27 스케일 지배 97.8%)를 진위검증으로
규명·기각했고, rev3는 QT 스케일 + **조건화 스코어(고유값 플로어 → 조건수 캡 100)**로 이를 원천
차단했다. 이 정직한 아크가 최종 확정의 신뢰 근거다.

## 2. 최종 모델 사양

- **아키텍처**: MLP-AE `D→64→32→z16→32→64→D` (GELU·LayerNorm·dropout 0.05). params ~수천, .pt ~71KB.
- **학습**: 채널가중 MSE · denoise=0.1 · AdamW lr=1e-3 · cosine · max_ep=400 · patience=30 · seed=42.
  학습셋 = seg1·C6_0 TRAIN(3,922, 시간순 앞 80%), fit=TRAIN만(불가침 3).
- **스케일**: QuantileTransformer(output_distribution=normal) — heavy-tail/bimodal 과도피처
  (C27/C61/C62) 강건.
- **스코어 계약 (불가침 12, 단일 `ae_raw`)**: 조건화 잔차 Mahalanobis²
  `(r−μ)ᵀ P (r−μ)`, `r = x − AE(x)`(채널가중), `P` = VAL 잔차 LedoitWolf 공분산에
  고유값 플로어(조건수 ≤ 100)를 적용한 정밀도. higher_is_worse, 직접 알람권 없음.
- **입력**: 83피처(부록 A 물리신호만). 식별자·카운터(C33)·시간·setpoint(C12)·타깃(C65)·
  과도플래그(C42) 등 27종 입력금지(불가침 1·7).

## 3. `ae_score` 캘리 · `ae_drift_score` (확정값)

- **캘리**: `ae_score = clip(interp(ae_raw; ANCH_X, ANCH_Y), 0, 1)`. 앵커 백분위
  **[50,90,98.5,99.5,99.9] → [0.05,0.10,0.20,0.50,1.00]**, **0.2 = VAL P98.5**.
  채택 5기준 전부 ✅, **L5 오경보(VAL>0.2) = 1.53% ≤ 2%**(REPORT_05).
- **drift**: 같은 `ae_score` 스트림의 EWMA(α=0.05), baseline **B0 = 정상 EWMA P99 = 0.130**.
  `clip((E_t−B0)/(0.2−B0),0,1)`. **단일 `ae_raw` 후처리 — 별도 모델 없음**. per-chamber 상태.
  검증: 정상 P95=0 / 지속 sub-σ(per-wafer 침묵) → drift 1.0 / seg2 레짐 → drift 1.0(상보).
- calib·drift baseline은 **재학습마다 재산출**(model과 한 몸).

## 4. 재학습(CT②) 로직 — 핸드오프 (계획 §10-4)

**패키지**: `ae_pipeline` (Python 모듈). **엔트리**: `python -m ae_pipeline.retrain`.

**파이프라인 8단계**: ① 새 정상셋 선택(레짐/필터/wafer 목록) → ② dedup 가드+split 드랍·
피처추출(D=83) → ③ 시간순 TRAIN/VAL 80/20 → ④ QuantileTransformer **fit=TRAIN만** 재fit →
⑤ MLP-AE 재학습(동일 config) → ⑥ 스코어러(μ·sd·조건화 정밀도 P) 재산출 → ⑦ calib 앵커·
drift baseline 재산출 → ⑧ **버전 태깅 + 번들 통째 저장 + manifest(해시·세트크기·메트릭)**.

**세트 동기(불가침 12)**: 재학습 = model/scaler/scorer/calib/drift **세트 교체** — 버전 불일치
금지. 예: `z16_rev3_seg1→_seg2`, `calib_v1→v2`. seg1↔seg2 역방향 score 상승은 레짐 전환의
대칭성(정상 동작).

**리허설 실측(REPORT_05)**: seg2 정착 재학습 → recon RMSE **1.046 → 0.332 정상화 ✅**,
141s(CPU-fallback; N-2 ≤2분 = prod-GPU 기준). 재학습 후 **새 정상 recon 정상화 + Qual 5기준
재확인**으로 운영 승격.

**추론 인계**: `python -m ae_pipeline.infer {score|build-scorer|verify}`. 동봉 frozen 모델은
`build-scorer`로 원 VAL(seg1·C6_0, 해시 `fa500f3d` 대조)에서 스코어러 통계를 1회 재구성.

## 5. 아티팩트 번들 (배포 세트 · 한 몸)

| 파일 | 내용 |
|---|---|
| `artifacts/model_seed42.pt` | MLP-AE z16 state_dict (~71KB) |
| `artifacts/scaler.pkl` | QuantileTransformer(normal), fit=TRAIN |
| `artifacts/scorer.npz` | μ·sd·조건화 정밀도 P·W_VEC *(build-scorer/재학습 생성)* |
| `artifacts/calib.json` | ae_raw→ae_score 앵커 (0.2@P98.5) |
| `artifacts/drift.json` | EWMA α·alarm·baseline_B0 *(재학습 생성)* |
| `artifacts/feature_spec.json` | 입력 83피처 명세 |
| `manifest.json` | 버전·시드·config·sha256·세트크기·메트릭 *(재학습 생성)* |
| `ae_config.yaml` | 배포 config (FINAL) |

무결성: `manifest.json`에 아티팩트별 sha256 기록, `infer verify`로 대조.

## 6. 출력 스키마 (§11-2 — B 통보용)

`ae_score`(0~1) · `ae_raw`(조건화 Maha, 계약불변) · `ae_top_channels[]`(C코드, Evidence Card)
· `ae_drift_score`(0~1, EWMA 스트림) · `ae_model_version` · `ae_calib_version` ·
`transient_context`(bool). registry = 공용 모델 1 + 챔버 스케일러.

## 7. NFR

**N-1** 추론 3.18ms/장(피처추출 제외, <500ms ✅) · **N-2** 재학습 141s CPU(prod-GPU ≤2분 목표)
· **N-3** 모델 71KB(<수 MB ✅, torch 외 특수 의존성 없음).

## 8. 확정 판정 & 잔여(팀)

- **FINAL: `ae_v3` 최종 모델 확정 ✅** — G1·G3 검증 + 재학습 로직 포함 핸드오프 완료. 배포 준비 완료.
- **잔여(팀, 코드 밖 결정)**: ① L6 문구 오너 결정(`팀통보_L6기준_결정요청`) ② C2 "Qual 0.2"
  config 합의(A 제안 근거 = L5 1.53%·5기준) ③ `API_Contract` 필드명 대조 ④ is_qual 스키마 리뷰.

## 정직한 한계

① Qual·CT²는 시뮬 Qual 발행 전 **오프라인 근사**(seg2 표본). ② `ae_drift_score` v1 = EWMA
레벨 단독(속도항 백로그). ③ registry는 인터페이스·절차 문서(멀티챔버 fit은 통합 단계).
④ CT² 시간 CPU-fallback(prod-GPU 시 N-2 충족). ⑤ 정착 T=2(시퀀스 정보 원천 제한).
⑥ 캘리 앵커·drift baseline은 실측 기반 — 재학습마다 재산출. ⑦ 본 패키지는 **로컬 실행 기준**
(클라우드 미러 자체검증 미실행) — 문법(py_compile) 검증 완료, 학습/추론 수치는 로컬 재현으로 확인.

---
*본 스냅샷은 수정하지 않는다. 이후 분석 변경 시 새 REPORT.*
