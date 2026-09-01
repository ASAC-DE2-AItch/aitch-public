# ae_v3_handoff — 최종 AE 모델 확정 + 재학습 로직 패키지

> ⚠️ **초기 상태(init) — 외부 저장소 as-is 이관본 (2026-07-23)**
> 별도 개발 저장소에서 확정한 ae_v3 최종 모델·코드를 **구조 변경 없이 그대로** 옮겨온
> 시점의 스냅샷이다. AITCH 헌법 정합화는 **미적용** 상태이며, 후속 PR에서 진행한다.
> - 모델 경로: 현재 `artifacts/` (헌법 4-1 `models/anomaly_ae/` 배치 미이행)
> - `models/CHANGELOG.md` 등재 미이행 (헌법 3-3)
> - 이하 본문의 경로·실행 예시는 **원 개발 저장소 기준** — 이관 후 재검증 필요
>
> A트랙(세그·AE) · **ae_v3 최종 확정** · 2026-07-23
> 상태: **G3 운영 투입 게이트 PASS(REPORT_05) → ae_v3 최종 모델 확정(REPORT_06_FINAL)**
> 이 패키지 = Pipeline B의 Autoencoder 트랙 배포 세트 + CT② 재학습 로직(운영 인계용).

---

## 무엇 / 왜

`ae_v3` = **집계 MLP-AE(z16 rev3) + 조건화 잔차 Mahalanobis 스코어**. Etch FDC 신호는
시간축이 아니라 **레벨축**(Step4 정착 T=2)이라 시퀀스 AE 대신 집계 MLP-AE + 과도 모양
지표가 주력(아키텍처 판단 v1). wafer 1장 → `ae_score`(0~1) · `ae_drift_score` ·
`ae_top_channels`. 최종 출력은 **단일 `ae_raw`** 계약(불가침 12, 변경 금지).

원 노트북(`../ae_v3/ae_v3.ipynb`)의 재현/재학습 로직을 운영용 Python 모듈로 정리하고,
아티팩트 세트를 self-contained 번들로 묶은 것이 이 패키지다.

## 패키지 구성

```
ae_v3_handoff/
├── README.md                        ← (이 문서) 사용법·재학습·계약·불가침
├── ae_v3_REPORT_06_P7_FINAL.md      ← 최종 확정 스냅샷(immutable)
├── ae_config.yaml                   ← 배포 config (FINAL)
├── requirements.txt                 ← 재학습·추론 의존성
├── artifacts/                       ← 배포 세트(한 몸·버전 동기)
│   ├── model_seed42.pt              ·  MLP-AE state_dict (~71KB)
│   ├── scaler.pkl                   ·  QuantileTransformer(normal), fit=TRAIN
│   ├── calib.json                   ·  ae_raw→ae_score 앵커 (0.2@P98.5)
│   └── feature_spec.json            ·  입력 83피처 명세
│       (scorer.npz = build-scorer / drift.json·manifest.json = make_drift 로 최초 1회 생성 · 재학습 시 전체 재산출)
└── ae_pipeline/                     ← 재학습·추론 로직
    ├── constants.py                 ·  단일소스 상수·시드·해시 (부록 A)
    ├── features.py                  ·  전처리 유틸 + extract_features (D=83)
    ├── model.py                     ·  MLPAE·train_ae + Scorer(조건화 잔차 Maha)
    ├── calibration.py               ·  ae_raw → ae_score (ECDF 앵커)
    ├── drift.py                     ·  ae_drift_score (EWMA, per-chamber)
    ├── io_bundle.py                 ·  번들 저장/로드·무결성 해시·manifest
    ├── retrain.py                   ·  CT② 전체 재학습 + 입력 스키마 어댑터 (CLI)
    ├── validate_bundle.py           ·  CT② 자동 검증 게이트 — 항목·임계 단일 소스 (CLI)
    ├── infer.py                     ·  추론·스코어링 + build-scorer·verify (CLI)
    └── make_drift.py                ·  frozen 번들 drift.json·manifest.json 생성 (CLI)
```

## 설치

```bash
cd ae_v3_handoff
python -m venv venv && source venv/bin/activate    # (Windows: venv\Scripts\activate)
pip install -r requirements.txt
# torch는 CUDA 충돌 방지로 별도 설치 (계획 §4)
pip install "torch>=2.2"                            # CPU
# 또는 CUDA: pip install "torch>=2.2" --index-url https://download.pytorch.org/whl/cu121
```

Python ≥ 3.10. 실행 기준 런타임(REPORT_05) = torch 2.13.0+cpu.

## 빠른 시작 (동봉 frozen 모델로 스코어링)

동봉된 `artifacts/`에는 `scorer.npz`·`drift.json`·`manifest.json`이 없다(노트북은 매 실행
VAL에서 재계산했음). **최초 1회** 원 정상 VAL에서 ① 스코어러 통계(μ·sd·조건화 정밀도 P)를
`build-scorer`로, ② drift baseline·manifest를 `make_drift`로 재구성한다 — 노트북과 수학적으로
동일하며, VAL 세트 경계 해시(`fa500f3d`)를 원 지문과 대조해 재현성을 확인한다.
⚠ `build-scorer`는 **scorer.npz만** 생성한다 — `drift.json`·`manifest.json`은 `make_drift`가 만든다.

```bash
# ① 스코어러 통계 재구성 (원 학습셋 seg1·C6_0 VAL 필요) — 1회
python -m ae_pipeline.infer build-scorer \
    --bundle ./artifacts --data ./merged_data_v3.parquet
#   → VAL 해시 fa500f3d 일치 확인 후 artifacts/scorer.npz 생성

# ② drift.json·manifest.json 생성 (scorer.npz 이후) — 1회
python -m ae_pipeline.make_drift \
    --bundle ./artifacts --data ./merged_data_v3.parquet
#   → baseline_B0 ≈ 0.130 확인 후 artifacts/drift.json·manifest.json 생성

# ③ wafer 배치 스코어링 → 출력 스키마(JSON)
python -m ae_pipeline.infer score \
    --bundle ./artifacts --data ./merged_data_v3.parquet \
    --wafers <C64_1>,<C64_2> --out scores.json

# ④ 번들 무결성 검증 (manifest 해시 대조)
python -m ae_pipeline.infer verify --bundle ./artifacts
```

프로그램적 사용:

```python
from ae_pipeline.infer import AEModel
ae = AEModel.from_bundle("artifacts")
recs = ae.score_wafers(df_rows, chamber_col="C24")   # per-wafer 출력 스키마 리스트
```

## 재학습 (CT②) — 계획 §10-4

**트리거**: 새 레짐 승인(대PM 등) 시 Qual 통과 후 재학습.

**절차 (파이프라인이 자동 수행)**: ① 새 정상셋 선택 → ② dedup 가드+split 드랍·피처추출
→ ③ 시간순 TRAIN/VAL 80/20 → ④ QuantileTransformer **fit=TRAIN만** 재fit →
⑤ MLP-AE 재학습(동일 config) → ⑥ 스코어러(μ·sd·P) 재산출 → ⑦ calib 앵커·drift
baseline 재산출 → ⑧ **버전 태깅 + 번들 통째 저장**.

```bash
# 예: seg2 정착 레짐(C6_0)으로 재학습
python -m ae_pipeline.retrain \
    --data ../Data/merged_data_v3.parquet --out ./bundle_seg2 \
    --regime seg2 --normal-col C6 --normal-value C6_0 \
    --model-version z16_rev3_seg2 --calib-version calib_v2

# 또는 승인된 정상 wafer 목록으로 직접 지정
python -m ae_pipeline.retrain \
    --data ../Data/merged_data_v3.parquet --out ./bundle_new \
    --wafers-file approved_normal_wafers.txt \
    --model-version z16_rev3_seg2 --calib-version calib_v2
```

출력 번들 = `model_seed42.pt · scaler.pkl · scorer.npz · calib.json · drift.json ·
feature_spec.json · manifest.json` (+ 사이드카 `val_wafers.json` — 검증 입력, 7파일 세트
아님·배포 미포함). **주의**: 재학습 = 세트 교체 — model/scaler/scorer/calib/drift는 항상
같은 학습에서 나온 **버전 동기** 세트여야 한다(불가침 12, 버전 불일치 금지).

리허설 실측(REPORT_05): seg2 정착 재학습 → recon RMSE **1.046 → 0.332 정상화**,
141s(CPU-fallback; **N-2 ≤2분은 prod-GPU 기준** — 계획 P0-1 "CPU는 수 분"과 정합).

### 운영 CT² 입력 (조립 CSV) — 입력 스키마 어댑터 *(WP-1, 2026-07-30)*

운영 경로의 학습셋은 `scripts/ct2_assemble_trainset.py` 산출 CSV다. Kafka 되감기
산출물은 `_ts` 만 있고 **`C10`·`C46` 이 없어** 그대로는 `standard_sort`·dedup 키가 성립하지
않는다. `--input-schema`(기본 `auto`)가 이를 흡수한다.

| 스키마 | 판별 | 보정 |
|---|---|---|
| `merged` | `C10`·`C46` 둘 다 존재 | 없음 (기존 재현 경로 무영향) |
| `ct2` | 하나라도 부재 | `_ts→C10`(ISO8601, tz-naive) 매핑 · `C46` = `(C64,C7)` 내 순번 합성 |

```bash
# 운영 CT² — 조립 CSV 는 이미 소급 필터 통과분 전체이므로 정상 필터 인자를 주지 않는다
python -m ae_pipeline.retrain \
    --data ../../../../Data/ct2/ct2_trainset_SIM_CH_1_20260730_120000.csv \
    --out ../../../../models/anomaly_ae/ae_ct2_SIM_CH_1_20260730_120000 \
    --input-schema ct2 --model-version z16_rev3_ct2 --calib-version calib_v2
```

⚠️ `C46` 합성은 dedup 키 `(C64,C7,C46)` 를 행마다 유일하게 만들어 **머지 아티팩트
가드(불가침 11)를 적용 대상 0으로 만든다**. Kafka 산출물에는 오프라인 머지가 없어 의미
손실은 없지만, 적용 내역은 manifest `input_adaptation` 에 남는다 (조용한 변환 금지).

## 자동 검증 게이트 (CT² 배포 문지기) — `validate_bundle` *(WP-2, 헌법 3-3 ③ ⓑ)*

**이 모듈이 게이트 항목·임계의 단일 소스다.** 헌법은 수치를 규정하지 않고 위임하며,
임계는 `config/params.yaml` `ct.ct2_gate_*` 로 조정한다. 항목 **추가·강화는 자유,
삭제·완화는 PM 승인 필수**.

| 군 | 검사 | 막는 것 |
|---|---|---|
| A 무결성 | 7파일 완결 · manifest sha256(항목 수 포함) · VAL·홀드아웃 세트 해시 · 채널가중 정합 · 세트 행 실존 | 세트 불일치(불가침 12) · 다른 데이터로 검증 |
| B 정상조용 | L5 오경보율 · recon RMSE · calib ①평균 · ②P95 — **calib 미적합 홀드아웃에서 측정** | 오경보 폭주·정상화 실패 |
| C 이상검출 | **합성 프로브 주입 채점**(±kσ 단일채널, 결정적) · 단조성 | ★조용하기만 한 **깡통 모델** |
| D 임계건전 | B군 표본 하한 · 앵커 간격 비율·구조 · 홀드아웃 실존 · 적합 표본 하한 | 소표본 붕괴(앵커 뭉침) · 항진명제화 |

**★B군을 홀드아웃에서 재는 이유**: calib 앵커는 정의상 "VAL raw의 P50·P90·**P98.5**…"다.
그래서 **calib을 적합한 그 표본으로** L5·평균·P95를 재면 값이 산식으로 고정된다
(L5 ≈ 1.5% · 평균 ≈ 0.05 · P95 < 0.2) — 모델 품질과 무관한 항진명제이고, B군이 문지기가
아니게 된다. `run_ct2`는 VAL을 절반으로 갈라 앞쪽만 calib·scorer 적합에 쓰고 뒤쪽을
게이트 홀드아웃으로 남긴다(`--gate-holdout-frac`, 기본 0.5). 홀드아웃 확보에 실패하면
**D3가 FAIL로 그 사실을 드러낸다** — 조용한 퇴화는 없다.

⚠️ **표본 3값은 함께 조정한다**: `ct2_min_train_wafers`(1000) × `val_frac`(0.2) ×
`gate_holdout_frac`(0.5) = 홀드아웃 100장. `l5_budget`(구 `l5_max` — 2026-08-09 개명)의
DEFAULTS 가 0.02이므로 홀드아웃이 50장이면 오경보 1건이 곧 2%가 되어 **임계를 측정할 수
없다**. 이 정합은 `tests/test_ct2_gate.py::test_l5_threshold_is_measurable_at_default_floor`
가 고정한다.

★2026-08-09 (기준 개정 v2 §1-5): 해상도만으로는 부족하다. 홀드아웃 600장은 해상도
0.17%p 로 여유롭게 통과하지만 **Wilson CI 반폭이 ±1.25%p** 라 예산 4%와 원 목표 2%를
구분하지 못한다 — 그래서 B1 판정이 `PASS-경계` 로 남는다. 반폭 ±1%p 를 넘으려면
홀드아웃 **940장**(여유 1000장)이 필요하다. `scripts/ct2_threshold_sweep.py` 의
`check_sample_triplet` 이 이 축을 함께 검사한다.

```bash
python -m ae_pipeline.validate_bundle \
    --bundle ../../../../models/anomaly_ae/ae_ct2_SIM_CH_1_20260730_120000 \
    --data ../../../../Data/ct2/ct2_trainset_SIM_CH_1_20260730_120000.csv
# exit 0 = PASS · 2 = FAIL(판정) · 1 = 실행 오류 → <bundle>/validation.json
```

**C군(프로브)이 왜 필요한가**: B·D만 보면 "전부 최저점을 내는 모델"이 만점으로 통과한다.
조용함과 민감함을 동시에 요구해야 게이트가 문지기가 된다 (반례 실증 =
`tests/test_ct2_gate.py::test_gate_blocks_dead_model`). 프로브는 **평가 전용**이며 TRAIN에서
분리된 VAL 행에만 주입하고 주입본을 영속화하지 않는다 (불가침 2 — 학습셋 유입 시 미탐의
자기실현). 난수를 쓰지 않아 시드 간 변동이 원리적으로 0이다.

**배포 연결**: 게이트 PASS → `ct.ct2_auto_promote` 가 `true` 면 자동 promote(신호 드롭 →
consumer 관리형 리로드), `false`(현재) 면 파생 thread 배포 승인으로 회귀. FAIL →
challenger 보존·미배포 + `ct_decisions(BLOCKED)` + 에스컬레이션, 재요청 발의는 사람.
배선 정본 = `docs/CT2_배선_설계_v2.md`.

## I/O 계약 · 출력 스키마 (→ B 팀원 B · §11-2)

추론 계약(wafer 1장): `extract_features → scaler.transform → ae_raw(조건화 Maha) →
calib → ae_score`, 그리고 `ae_top_channels`(잔차 상위 k) + `ae_drift_score`(EWMA,
per-chamber). AE는 **직접 알람권 없음** — `fdc.prediction` 발행, B가 Context Score 합산.

| 필드 | 타입 | 의미 |
|---|---|---|
| `ae_score` | float[0,1] | per-wafer 이상도 (캘리, 0.2=Qual 임계) |
| `ae_raw` | float | 조건화 Maha 원점수 (계약불변) |
| `ae_top_channels` | list[str] | 잔차 상위 채널 (C코드, Evidence Card 재료) |
| `ae_drift_score` | float[0,1] | 스트림 누적 드리프트 (EWMA) |
| `ae_model_version` | str | 예: `z16_rev3_seg1` |
| `ae_calib_version` | str | 예: `calib_v1` |
| `transient_context` | bool | 과도 컨텍스트 표기 (C-3) |

anomaly vs drift 상보: 스파이크 고장=`ae_score`만, 느린 sub-σ 드리프트=`ae_drift_score`만,
레짐 전환=둘 다.

## 불가침 (코드로 강제)

`1` C65 입력금지·C64 그룹분할 · `2` seg2/C6_1/INJECT-EVAL은 평가 전용 · `3` 스케일
fit=TRAIN만 · `5` 과도(C42=1)/정착(C42=0) 분리 · `7` 물리신호만(부록 A) · `11` dedup
가드 `(C64,C7,C46)` + split 드랍 상시 · `12` **단일 `ae_raw` 계약불변**.

## 정직한 한계

① Qual·CT²는 시뮬 Qual 발행 전이라 **오프라인 근사**(seg2 표본). ② `ae_drift_score`
v1 = EWMA 레벨 단독(속도항 백로그). ③ registry는 인터페이스·절차 문서 범위(멀티챔버
fit은 통합 단계). ④ CT² 시간은 CPU-fallback(prod-GPU 시 N-2 충족). ⑤ 정착 T=2(시퀀스
정보 원천 제한). ⑥ 캘리 앵커·drift baseline은 실측 분포 기반 — **재학습마다 재산출**.
⑦ **로컬 실행 기준**: 본 패키지는 로컬 venv에서 실행(클라우드 미러 자체검증 미실행,
CLAUDE.md 작업방식) — 문법(py_compile)만 검증됨, 학습/추론 수치는 로컬 재현으로 확인.

## 변경 이력

- **2026-07-28** — 코드리뷰 대응 (§1-4·6-1). ⓐ `drift.cap_baseline()` 신설 — `baseline_B0`
  가 `alarm_level`(0.2) 이상이면 분모가 0·음수가 돼 ZeroDivisionError(전량 더미 폴백 =
  조용한 기능 정지) 또는 drift 부호 반전이 난다. 산출 시점(`fit_drift_baseline`)과 사용
  시점(`DriftTracker`·`drift_stream` — 외부 `drift.json` 로드 경로)에 **이중 캡 + 경고 로그**.
  ⓑ `infer.py`·`retrain.py` 의 `print()` → `logging` 이관(헌법 6-1, `make_drift.py` 와 일관).
  ⓒ CT② 종료 시 `models/CHANGELOG.md` 기록 리마인드 출력(헌법 3-3). 회귀 테스트:
  `tests/test_ae_drift_baseline.py`.
- **2026-07-23 v1.0** — ae_v3 최종 모델 확정. 재학습(CT②) 로직 + 추론 모듈을 운영용
  Python 패키지(`ae_pipeline`)로 정리, 아티팩트 self-contained 번들 구성.
  `ae_v3_REPORT_06_P7_FINAL.md` 발행. (선행: `../AE_핸드오프_P7.md` 팀 전달물)
