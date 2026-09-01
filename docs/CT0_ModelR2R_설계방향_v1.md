# CT⓪ Model R2R 아키텍처 설계 방향 v1 — lean-85 예측 bias 자동 보정

> 작성: 에이치 · 2026-08-07 · **상태: 초안 (팀 리뷰·PM 승인 전 — docs/ 소유권 3-1)**
> 정본 관계: 산식·파라미터는 멘토 확정(7/11)과 `config/params.yaml` `ct.model_r2r`이 정본이며 **본 문서는 산식을 바꾸지 않는다**. 배선·상태·장애 설계의 단일 소스를 목표로 한다 (CT²의 `docs/CT2_배선_설계_v2.md` 선례).
> 근거 등급 표기: **①실측** / **②헌법·계약·멘토 확정** / **③설계 판단(리뷰 대상)**.

---

## 1. 목적·범위

**목적**: 재학습(CT①) 사이의 Cycle 내 구간에서, 실측 라벨(`fdc.actual`)의 잔차 rolling mean을 lean-85 예측값에 가산해 정확도를 회복한다. 실증 앵커: 레짐 전환 후 무보정 잔차 ~513 → 당레짐 rolling bias 적용 85 **[①F8, 멘토 7/11 확정]**.

| 범위 | 비범위 |
|---|---|
| bias 추정·적용·기록·복구 배선 | 산식 변경 (rolling mean N=500·min 50·clamp ±1×RMSE — 멘토 확정 **[②]**) |
| `fdc.actual`·`QualVerdictConfirmed` 소비 설계 | Recipe R2R(승인 경로)·실력치 리캘리브레이션 |
| CT① promote와의 접점 규칙 | R9 "신규 기준선 수립" 대형 오프셋 (승인 경로) |
| 멀티챔버 확장·장애 내성·관측성 | CT①·CT² 재학습 자체 |

## 2. 이미 정해진 것 (설계 입력)

| 항목 | 내용 | 근거 |
|---|---|---|
| 자동 허용 3조건 | ⓐ 상한 clamp 내 ⓑ 매 갱신 `ct_decisions` 기록 ⓒ 상한 초과 격차는 미적용·Incident 신호 전환 | ② 헌법 1-1 |
| 산식 | "마지막 요란 판정 PM 이후" 라벨 잔차 rolling mean, N=500, 자동 적용 최소 표본 50(SE≈10.7), `|bias| ≤ 1.0 × train RMSE` | ② params `ct.model_r2r` C7·C8 |
| 리셋 경계 | 요란 판정 PM에서만 bias=0 + 이전 라벨 차단. 소정비·조용 PM은 연속. 승인 Recipe 튜닝(±3% 이내)은 리셋 불요 — 차단된 옛 라벨도 CT① 학습에는 사용(창 자격 ≠ 학습 자격) | ② 사이클_정의 결정 5 |
| 라벨 채널 | `fdc.actual` (label_delay ≈13,900장≈2개월, `pm_count` 포함, Qual wafer 미발행). 계약 §1-B **v4.6 초안** — 발행측 스텁 `enabled=False` | ② 계약 §1-B |
| 리셋 이벤트 | `fdc.agent`의 `QualVerdictConfirmed{verdict, chamber_id, pm_count}` — 복구는 `quals` 조회(P6-1 컬럼 선결) | ② 계약 §8-B |
| 누수 가드 | `actual_c65`는 Feature 금지 — bias·채점·재학습 라벨 전용 | ② 헌법 1-3 |
| 현행 코드 | `consumer.py`는 `fdc.raw`만 구독 — actual·agent 소비와 bias 적용은 **미구현(그린필드)**. sink(#37)는 DB 격리용 분리 프로세스, CT①은 `control/ct1` 파일 신호로 promote | ① 코드 확인 8/7 |

## 3. 설계 원칙 8개

1. **물리 불가침**: 출력 가산항만. 장비·wafer·Recipe·실력치에 어떤 쓰기도 없다 (헌법 1-1).
2. **감사 우선 (기록 없으면 갱신 없음)**: `ct_decisions` 기록 성공 후에만 serving bias 변경·오프셋 커밋 (7장 "감사 행 기록 후 커밋" 선례).
3. **상태는 파생물**: 진실 원천 = DB(`wafer_predictions`+`ct_decisions`). 메모리·파일 상태는 언제든 재계산 가능한 캐시 — 재기동·리밸런스 = 재계산 (quals 부트스트랩·TSR-0002 "상태로 판정" 선례).
4. **결정론·멱등**: 같은 DB 상태 → 같은 bias. `ct_id` 결정론 채번 + UNIQUE로 중복 전달 무해화.
5. **raw 기준 잔차**: bias 추정은 항상 **무보정 예측 대비** (`잔차 = actual − raw_pred`). 보정 후 예측 대비 잔차로 갱신하면 60일 지연 하의 되먹임 루프가 된다 — 개루프 추정 + 가산 적용만 허용 **[③핵심]**.
6. **우아한 축퇴**: 어떤 실패에서도 최종 상태 = bias 미적용(현행 raw 예측). 보정 실패가 예측 발행을 죽이는 코드는 금지 (헌법 6-2 정신).
7. **SHAP 불간섭**: `shap_top3`는 raw 예측 설명 그대로. bias는 별도 필드로 병렬 제시 — `contribs 합 = predicted − bias_applied` 항등식 유지 (헌법 3-3 SHAP 독립).
8. **config 단일 소스**: 임계·창·모드는 전부 `params.yaml` `ct.model_r2r.*` (6-1). 신설 키는 최소화 (D14 "키 남발 방지" 선례).

## 4. 아키텍처 개요

```
【serving 경로 — 지연 무추가】
fdc.raw ─▶ [A consumer] build → booster(raw) ─▶ (+ bias[chamber])* ─▶ fdc.prediction
                 ▲                                                      {predicted_c65(보정 후),
                 │ bias 파일 스캔 (mtime 캐시, 주기적)                     bias_applied, model_version…}
        control/ct0/bias_<chamber>.json  (원자 교체 — os.replace)          │
                 ▲                                              [A-sink] wafer_predictions
                 │ 갱신 시에만 쓰기                                        (predicted·bias_applied 적재,
【bias 추정 경로 — 별도 프로세스 ct0_bias_updater】                          60일 뒤 actual_c65 UPDATE)
fdc.actual ──▶ [updater] wafer_predictions에서 raw_pred 조인 ─▶ 잔차 ─▶ 창(pm_count 자격 필터)
fdc.agent  ──▶ (QualVerdictConfirmed·loud) valid_from_pm_count 갱신 + 창 비움
                     │ rolling mean → clamp(±1×RMSE) → 양자화(Δ≥δ만 갱신)
                     ▼
              ct_decisions(ct0_bias) INSERT ─성공 후─▶ bias 파일 교체 ─▶ 오프셋 커밋
                     └ 상한 초과: clamp 유지 + fdc.mlops `ModelBiasCapExceeded` (알림 합류는 WP-C 협의)
【CT① 접점】 control/ct1 promote → consumer 리로드(D14) — bias 창은 리셋하지 않음 (§6 D8)
```
*(\*) `is_qual=true` wafer는 bias 미적용 (§6 D7).*

**컴포넌트 3개 + 접점 2개**:

| 컴포넌트 | 역할 | 프로세스 |
|---|---|---|
| `ct0_bias_updater` (신규) | actual·verdict 소비 → 잔차 창 관리 → bias 결정 → 기록·신호 | **분리 프로세스, 신규 그룹 `consumer-group-bias`** — sink(#37)의 "DB 장애 격리" 선례와 동일 이유 **[③]** |
| control 파일 채널 (신규) | updater → serving 단방향 bias 전달 | `control/ct0/` — `control/ct1` promote 신호·원자 교체(7장) 선례 재사용 |
| serving 적용부 (consumer.py 소폭) | 파일 스캔 → 가산 → `bias_applied` 발행 | 기존 consumer — DB 미접촉 유지 |
| 접점: A-sink | `bias_applied` 컬럼 적재 (쓰기 소유 유지 — updater는 `wafer_predictions` 읽기 전용, `ct_decisions`만 쓰기) | 테이블당 쓰기 주체 1개 원칙 **[③]** |
| 접점: CT① | promote는 bias와 독립. 평가(`validate_lean85`)는 **raw 기준 유지** — bias가 champion 선정을 가리면 안 됨 | ③, §6 D8 |

## 5. 상태 모델

상태 키 = **`chamber_id` 단독** (모델 버전 아님 — 근거는 §6 D2).

```
BiasState(chamber_id):
  valid_from_pm_count : int      ← 마지막 요란 판정 PM의 pm_count (자격 경계)
  window              : deque[(wafer_id, residual, model_version)]  최대 N=500
  bias_applied        : float    ← clamp·양자화 후 현재 서빙값 (미충족 시 0.0)
  train_rmse          : float    ← champion 메타에서 로드 (clamp 분모 — >0 검증 필수)
  source_ct_id        : str      ← 마지막 갱신의 감사 행 (추적성)
```

- **자격 필터 = `pm_count`**: 라벨의 `pm_count ≥ valid_from_pm_count`일 때만 창에 편입. 타임스탬프 비교가 아니라 정수 비교라 **이벤트 순서 역전·tz 함정과 무관**하고, 리셋 이벤트 재전달도 `max()` 갱신이라 멱등 **[③핵심]**.
- **눈금**: bias 단위 = C65 개수(예측값과 동일). 발행 시 `round(2)` (predicted_c65 관례).

## 6. 핵심 설계 결정 D1~D8

| # | 결정 | 근거 |
|---|---|---|
| **D1** | 잔차는 **raw 예측 대비**. `wafer_predictions`에 보정 전 값 복원이 항상 가능하도록 `bias_applied`를 wafer별 저장 (`raw = predicted_c65 − bias_applied`) | 원칙 5. 60일 지연 하 되먹임 불안정 차단 + 감사 시 분해 가능 **[③]** |
| **D2** | 창은 **챔버 키 단독 — 모델 버전별 분리·스왑 리셋 안 함**. 창 엔트리에 model_version만 병기(관찰용) | CT①이 **일간** promote인데 라벨은 60일 지연 — 버전별 창은 최소 표본 50에 영원히 미달(기아). 연속 champion은 게이트(D1 상대 + D13 절대)가 유사 성능을 보증하고, bias가 잡는 것은 **어떤 champion도 아직 못 배운 최근 60일의 공정 드리프트**라 버전 공통 **[③핵심]** |
| **D3** | updater는 **분리 프로세스 + control 파일 채널**. serving은 DB 미접촉 유지 | sink #37(DB 장애 격리)·`control/ct1` promote 신호·os.replace 원자 교체(7장) — 기존 선례 3개 재사용. 보정 경로 장애가 예측 지연을 못 만든다 **[③]** |
| **D4** | **양자화 갱신**: `|신규 − 현재| ≥ δ(`min_update_delta`)`일 때만 갱신·기록. 헌법 "매 갱신 기록"의 실행 정의 = **서빙값 변경 = 갱신 = 행 1개** | 라벨마다 ε 변동을 전부 기록하면 231행/일/챔버 노이즈. 양자화로 기록 = 변경 1:1 보존, fleet 확장 시 기록량 O(변경)로 유계 **[③]** |
| **D5** | **상한 초과**: clamp 값으로 서빙 유지 + `ct_decisions(retrain_status='CAP_EXCEEDED')` + `fdc.mlops`에 `ModelBiasCapExceeded` 발행 (상태 진입 시 1회 + 지속 시 일 1회) | 헌법 1-1 ⓒ·params C7 주석("초과분 = Incident 신호"). **`fdc.alert` 직접 발행 금지(1-2)** — 알림 합류는 B가 `fdc.mlops`를 구독해 자기 룰로 alert를 여는 안을 협의(발행자 = 여전히 B 단일, WP-C 정합) **[③협의 필요]** |
| **D6** | 기록 실패 시 **미적용·미커밋** (state 변경 fail-closed) / 서빙은 마지막 정상 bias 유지 (serving fail-open) | 원칙 2·6. `enable.auto.commit=False` + 기록 성공 후 커밋 + 실패 시 seek 되감기 (7장 두 선례) |
| **D7** | **Qual wafer(is_qual=true)는 bias 미적용** | Qual C65-proxy 판정 시점엔 verdict 확정 전이라 구레짐 bias가 살아 있음 — 판정 입력을 오염시키고 사이클 간 비교 가능성을 깨뜨림. Qual은 fdc.actual도 미발행이라 창 오염은 원천 없음 **[③]** |
| **D8** | **모델 스왑 시 무리셋·무시딩** — promote 직후 잔차 추이를 관찰 지표로만 감시. 재채점 방식 시딩은 P3 사다리(§11)로 예약 | D2와 동일 근거. 시딩(challenger 홀드아웃 잔차)은 평가창=라벨창이라 정보 이득이 작고 상태 기계만 복잡화 **[③]** |

## 7. 복구·멱등 (fault tolerance 1/2)

**부트스트랩(기동·리밸런스 파티션 할당 시) — 전량 DB 재계산**:

```sql
-- ① 경계: quals(confirmed_verdict='loud' 최신, P6-1 컬럼) → valid_from_pm_count
-- ② 창: wafer_predictions에서 actual_c65 IS NOT NULL AND pm_count ≥ 경계
--        ORDER BY measured_at DESC LIMIT 500 → 잔차 = actual − (predicted − bias_applied)
-- ③ 교차 검증: 재계산 bias vs ct_decisions 최신 bias_applied — 불일치 시 경고 + 재계산 우선
```

- 마지막 `ct_decisions` 행을 믿지 않고 **재계산이 정본** — "예외가 안 났으니 성공" 금지, 상태로 판정 (TSR-0002).
- P6-1(quals verdict 컬럼) 선결 전 폴백: `ct_decisions(retrain_status='RESET')` 최신 행에서 경계 복원 — 리셋도 감사 행을 남기므로 자체 체크포인트가 된다 **[③]**.
- **멱등**: `ct_id`를 트리거 라벨 wafer_id 기반 결정론 채번(6-4 `CT` PREFIX) + UNIQUE `ON CONFLICT DO NOTHING` — Kafka 중복 전달·재기동 재처리 무해.

## 8. 장애 매트릭스 (fault tolerance 2/2)

| 장애 | 감지 | 대응 | 축퇴 상태 |
|---|---|---|---|
| updater crash | 프로세스 감시 | 재기동 → §7 부트스트랩 (RTO 수 초) | serving은 마지막 bias 파일로 계속 |
| DB 다운 | INSERT 실패 | 미커밋 + backoff 재시도 (ct2 `retry_backoff` 선례) | bias 동결 — 갱신만 멈춤 |
| bias 파일 손상·부재 | `json.loads` + `isinstance(dict)` 가드 (7장) | 경고 + **bias=0** | raw 예측 = 현행과 동일 |
| 역직렬화 실패·NaN 라벨 | 유한값 검증 | skip + log + 카운터 (6-2) | 해당 라벨만 제외 |
| 예측 row 미존재 (join miss) | SELECT 미스 | 지연 재시도 1회 → skip + log. miss율 지표 감시 | sink 장기 장애 시에만 발생 — 창 표본만 감소 |
| 중복·재전달 | UNIQUE 충돌 | DO NOTHING — 창은 wafer_id 중복 편입 금지 | 무해 |
| 리밸런스 | 파티션 할당 훅 | 해당 챔버만 재부트스트랩 | — |
| reset↔actual 순서 역전 | — | `pm_count` 자격 필터가 순서 무관 흡수 (§5) | — |
| QualVerdict 이벤트 유실 | 부트스트랩 대조 | `quals` 조회 폴백 (계약 §8-B 복구 원칙) | — |
| train_rmse 결손·≤0 | 로드 시 검증 | **보정 비활성** + 경고 (분모 가드 — 7장 "0 분모 = 조용한 기능 정지" 선례) | bias=0 |
| bias 급변·진동 | 갱신율 지표 | δ 양자화 + clamp가 1차 방어. 지속 진동 = CAP과 동급 경고 **[③후속]** | — |
| serving의 파일 스캔 실패 (잠금 등) | 읽기 예외 | 직전 값 유지 + 짧은 backoff (Windows 잠금 — 7장) | — |

**공통 불변식: 어떤 행이든 예측 발행은 멈추지 않는다** — 보정은 꺼질 수 있어도(bias=0) 파이프라인은 산다.

## 9. 확장성

| 축 | 현재 | 확장 설계 |
|---|---|---|
| 챔버 수 | SIM_CH_1~4, 231 wafer/일/챔버 | 상태 O(챔버)·챔버 간 공유 없음 → **파티션 키 = chamber_id**면 수평 샤딩이 공짜 (인스턴스별 담당 파티션 = 담당 챔버). §12: `fdc.actual` 키를 초안의 wafer_id에서 **chamber_id로 최종화 제안** — wafer_id 키는 한 챔버 라벨을 파티션에 흩어 단일 소유를 깬다 **[③]** |
| 처리량 | actual ≈231건/일 | fleet 100챔버여도 ≈0.3 msg/s — 병목 없음. 설계 목적은 처리량이 아니라 **소유·순서 규율의 선확보** |
| 기록량 | ct_decisions ct0 행 | D4 양자화로 O(변경) 유계. 연 단위 아카이브 정책은 후속 |
| 모델 진화 | 일간 CT① promote | D2 혼합 창(현재) → **P3: 예측 시점 피처 스냅샷 저장 + promote 시 신규 champion으로 재채점(re-score)** — 창을 현행 모델 잔차로 즉시 재구성해 혼합 가정 자체를 제거하는 상위 단계 **[③사다리]** |
| 다중 fab (EAL) | — | 상태 키 (site, chamber) 합성 — 구조 변경 없이 키 확장만 |

## 10. 관측성·검증

- **지표**: 챔버별 bias·창 표본 수·잔차 mean/σ·갱신율·CAP 히트·join miss율·파일 스캔 나이. 노출은 로그 + `ct_decisions` 조회(S 화면 후속)로 시작.
- **감사 쿼리**: `SELECT * FROM ct_decisions WHERE ct_type='ct0_bias' AND ...` — retrain_status 의미: `APPLIED`(갱신)·`SHADOW`(모의)·`RESET`(요란 리셋)·`CAP_EXCEEDED`. **스키마 변경 없음** — 기존 컬럼(residual·bias_applied·model_version_before/after) 재사용.
- **Scorecard 프로브**: 시뮬 라벨 구간에서 무보정 vs 보정 잔차 비교 — 기대 앵커 = F8(513→85) 재현. `scripts/verify_deployment_bias.py` 로직 재사용. 채점기만 `sim_events` 접근(파이프라인 코드의 SELECT는 커닝 — P4 보안 경계).
- **리허설 필수 항목**: ① 재기동 재계산 determinism(두 번 계산 동일값) ② 요란 리셋 후 창 자격 ③ CAP 진입·해제 ④ DB 다운 중 동결→복구 재처리.

## 11. 구현 배치·테스트 (maintainability)

```
src/agent_a_mlops/ct0_bias/            ← 소유 A (3-1)
├── core.py        순수 함수만 (I/O 0): update_window / decide_bias(창, rmse, cfg) → BiasDecision
├── updater.py     consumer 프로세스 (fdc.actual + fdc.agent, group=consumer-group-bias)
├── control_io.py  bias 파일 원자 교체·가드 로드 (ct1 신호 채널 유틸과 공유 검토)
├── bootstrap.py   §7 DB 재계산 (updater 기동·--rebuild CLI 겸용)
└── tests/         core 100% 결정론 테스트
consumer.py        소폭 diff: 파일 스캔 → is_qual 제외 가산 → bias_applied 발행
```

- **core는 순수**: Kafka·DB·파일을 모르는 함수로 산식·clamp·자격·양자화를 전부 구현 — 테스트가 브로커 없이 돈다.
- **테스트 카탈로그**: clamp 경계(±상한·부호)/최소 표본 미충족→0/pm_count 자격/리셋 멱등(중복 이벤트)/양자화 δ 경계/NaN·비유한 라벨 skip/재계산 determinism/ct_id 채번 충돌/파일 가드(비 dict·부분 쓰기)/**config 플립 시 그 값을 못박은 테스트 grep 동기화** (7장 `publish_enabled` 선례).
- 누수 리뷰 가드 문구: "`actual_c65`·`bias`가 피처 테이블에 들어가면 1-3 위반" — 가드는 `assert` 아닌 `raise ValueError` (7장).

## 12. 계약·스키마·config 변경 목록 (승인 절차 매핑)

| # | 변경 | 절차 |
|---|---|---|
| 1 | `fdc.actual` §1-B **최종화**: 파티션 키 wafer_id → **chamber_id** 제안, 필드 불변, 발행측 `enabled=true` 컷오버 | 계약 초안 리뷰 (소비자 A·sink·GW relay) + 컷오버 시 테스트 동기화 (7장) |
| 2 | `fdc.prediction` 신규 필드 **`bias_applied`** (float, 항상, round 2 — shadow 중엔 0). `predicted_c65` 의미 = "보정 후 최선 추정"으로 주석 명시 (이름·삭제 없음) | 헌법 2-2 — 소비자 B·Dashboard 리뷰 승인 + 계약 동시 갱신 |
| 3 | `wafer_predictions` **ALTER ADD `bias_applied` FLOAT** (sink가 적재) | 헌법 3-2 — `db/migrations/NNNN_*.sql` + init.sql 동일 PR. ⚠ sink upsert의 `COALESCE` 덮어쓰기 함정 점검 (spc_flags M1 실측 선례) |
| 4 | `ct_decisions` — **변경 없음** (기존 컬럼 재사용, retrain_status 의미는 본 문서 §10) | — |
| 5 | Consumer Group 신설 `consumer-group-bias` | 계약 그룹표 추가 (6-4 케밥 표기) |
| 6 | `params.yaml` `ct.model_r2r` 신설 키 **2개만**: `mode` (off / shadow / active — 기본 `off`), `min_update_delta` (D4 δ) — 기존 4키(C7·C8·reset_on·min_labels)는 그대로 | config 근거 문서 동시 갱신 (4-3). 코드 폴백 기본값 = `off` (ct1_auto_promote "로드 실패 시 무인화 금지" 규율과 동일 방향) |
| 7 | `fdc.mlops` 이벤트 `ModelBiasCapExceeded` (PascalCase — 6-4) | A 소유 토픽 — 알림 합류 경로는 B·WP-C 협의 (D5) |

## 13. 단계 로드맵

| 단계 | 내용 | 게이트 |
|---|---|---|
| **P0 계약 최종화** | §12-1·5 확정, 발행측 활성, sink 소급 적재 확인 | 소비자 리뷰 승인 |
| **P1 shadow** | updater 가동 — 계산·`ct_decisions(SHADOW)` 기록만, 적용·발행 0. 소비자 전원 무영향 | §10 리허설 4종 PASS |
| **P2 active 컷오버** | `mode: active` + §12-2·3 (bias_applied 발행·적재) | **Scorecard 프로브에서 무보정 대비 개선 실증** (F8 재현) — 실증 전 active 금지 (CT² 개통 조항·1-1 예외 2 "가드 걸고 허용" 선례) |
| **P3 확장** | 멀티챔버 파티션 정렬, CAP→alert 합류 배선, 피처 스냅샷 재채점(§9 사다리) | — |

**회귀 조항**: Scorecard 미탐 유발·잔차 악화 실측 1건 → 즉시 `shadow` 회귀, 원인 규명 전 재전환 금지. **롤백 = mode 플립 1개** — 모델·물리 자산 무접촉이라 초 단위.

## 14. 정직한 한계·결정 요청

**한계**: ① 60일 지속성 가정의 실측 근거는 1사이클(F8) — 표본 부족은 세계관 공통 한계. ② D2 혼합 창은 "연속 champion 유사 성능" 가정에 기댐 — promote 직후 잔차 추이 감시로 보강, 구조적 해소는 P3. ③ outlier 라벨(스크랩 누락분 등)이 rolling mean을 끌 수 있음 — 산식 변경(트림 평균 등)은 멘토 확정 이탈이라 **협의 없이 도입 금지**.

**결정 요청 (PM·팀)**: ⓐ 본 문서 승인 (docs/ 3-1) ⓑ §12-1 `fdc.actual` 키 변경 ⓒ §12-2 `bias_applied` 필드 (B·Dashboard) ⓓ D5 CAP 알림의 B 경유안 (1-2 정합) ~~ⓔ P6-1 `quals` verdict 컬럼 일정 (§7 폴백 해소)~~ → **ⓔ 종결 (2026-08-07)**: P6-1 은 컬럼(init.sql 7/28)·쓰기(B `qual_recorder`, loud/quiet)·테스트까지 **이미 구현돼 있었고**, 빠진 것은 마이그레이션 파일뿐이라 PM 이 `0010` 으로 보강했다(3-2 신설 전 작업분 — 같은 계열의 `0007` 과 동일 사유). **§7 폴백 없이 `quals` 조회 경로가 곧바로 성립**한다. 남은 후속은 결정이 아니라 개선 항목 하나뿐 — `quals.pm_count` 컬럼(현재는 `qual_id` SEQ 파싱, §15-3 ⑦).

---

## 15. 구현 정합 — 설계와 코드가 갈린 지점 *(2026-08-07 추가, P1 구현 PR)*

> 코드가 설계를 벗어난 곳을 **여기 한 곳에** 모은다. "문서와 코드가 다른데 에러가 안 나는" 표류는 헌법 7장이 이미 등재한 사고 유형이라(계약 §5 중첩 형 삭제 → 승인 payload 전량 null), 구현하면서 바꾼 것은 같은 PR에서 문서로 되돌린다 (4-3).

### 15-1. 배치

```
src/agent_a_mlops/ct0_bias/
├── core.py         순수 함수 (I/O 0) — 산식·clamp·자격·양자화·ID 채번
├── control_io.py   bias 파일 원자 교체·가드 로드 + 서빙용 BiasCache
├── bootstrap.py    DB 전량 재계산 · config·train_rmse 해석 · `--rebuild` CLI
├── updater.py      consumer 프로세스 (fdc.actual + fdc.agent, consumer-group-bias)
└── tests/          73건 — core 28 · control_io 11 · updater 19 · bootstrap 8 은 **브로커·DB
                    없이** 실행, serving 7 은 lean85 의존(pandas·xgboost) 환경에서만
consumer.py         서빙 적용부 (파일 스캔 → is_qual 제외 가산 → bias_applied 발행)
prediction_sink.py  bias_applied·pm_count 적재 (쓰기 주체 1개 유지)
```

실행: `python -m ct0_bias.updater` (또는 `docker compose --profile ct0 up -d ct0-bias-updater`) · 재계산 `python -m ct0_bias.bootstrap --all [--write]`

### 15-2. 설계와 달라진 것 3건

| # | 설계 | 구현 | 왜 |
|---|---|---|---|
| **①** | §7 경계 복구 = ① `quals` → ② RESET 행 **우선순위 사슬** | **세 소스(`quals`·RESET 행·`pm_log.json`)를 모두 읽고 최댓값** | **`quals` 가 권위 소스라는 설계는 그대로 성립한다** — P6-1 은 이미 구현돼 있었다(컬럼 = init.sql 7/28 · 쓰기 = B `qual_recorder.update_qual_confirmed`, loud/quiet 둘 다 · 테스트 有 · 마이그레이션 `0010` 은 3-2 신설 전 누락분을 PM 이 발굴·추가). 경계 정수는 **`qual_id` 의 SEQ** 에서 뽑는다 (`quals` 에 `pm_count` 컬럼은 없지만 B 가 `QUAL-<날짜>-<챔버>-<pm_count>` 로 채번 — `_judge_qual` 결정7). 사슬 대신 **최댓값**인 이유: 경계는 레짐이 넘어간 지점이라 뒤로 가면 안 되고, `update_qual_confirmed` 가 qual_id 미매칭으로 0행 no-op 이 되는 경로가 실재해(그쪽도 WARNING) 어느 한 소스를 유일 진실로 두면 그 사고에서 경계를 잃는다. 불일치는 경고로 표면화한다 |
| **②** | §12-3 = `wafer_predictions` **1컬럼**(`bias_applied`) | **2컬럼** (`+ pm_count INTEGER`) | §7 부트스트랩 SQL 이 `pm_count ≥ 경계` 로 필터하는데 그 컬럼이 테이블에 없었다. 시각 근사로 대신하면 §5 가 정수 비교를 택한 이유(순서 역전·tz 함정 회피)가 무너진다. `fdc.actual` 이 이미 싣고 오는 값이라 sink 가 한 줄 더 적으면 끝난다 (마이그레이션 `0012` — 최초 `0007` 로 땄으나 08-08 dev 머지에서 재번호) |
| **③** | §4 "리셋 = `valid_from_pm_count` 갱신 + **창 비움**" | **자격 필터 재적용** (bias=0 은 무조건) | 무조건 비우면, 리셋이 늦게 도착하는 경로(부트스트랩 복구·유실 재전달)에서 **이미 유효한 신레짐 표본까지** 날아가 보정이 불필요하게 오래 꺼진다. 라벨이 60일 지연이라 실전에서는 리셋 시점의 창이 전부 구레짐이므로 **결과는 "창 비움"과 같다** — 다른 것은 늦은 리셋에서의 안전성뿐이다 |
| **④** | §10 `retrain_status` **4종**(APPLIED·SHADOW·RESET·CAP_EXCEEDED) | **+ `DISABLED`** (표본 미달·분모 결손으로 bias 를 0 으로 되돌릴 때) | 4종만으로는 "보정이 꺼짐"이 **행 없는 서빙 변경**이 된다 — 헌법 1-1 ⓑ("매 갱신 `ct_decisions` 기록")를 문자 그대로 지키려면 해제도 기록해야 하고, 안 그러면 "왜 보정이 꺼졌나"를 감사로 답할 수 없다. 내부 구분(`INSUFFICIENT`)은 사유 문자열에만 남긴다 |
| **⑤** | (설계 무언급) | **더미 폴백 라벨 배제** (`model_version='dummy'`) | 계약 §2 가 그 값을 남긴 이유가 "채점·Model R2R 에서 오염되지 않게"다. 더미는 난수(100±10)라 라벨과 짝지으면 잔차가 수백 단위로 나와 rolling mean 을 통째로 끌어간다 — 50건이면 CAP 에 고정된다. 창(`push_label`)·재계산 SQL 양쪽에서 막는다 |
| **⑥** | (설계 무언급) | **기록 조건 = 값 변경 `or` 상태 전이** | D4 의 "서빙값 변경 = 행 1개"만 지키면, `APPLIED(clamp 100)` → `CAP_EXCEEDED(clamp 100)` 처럼 **값이 같은 상태 전이**가 무기록이 된다. 그러면 CAP 에스컬레이션 이벤트가 DB 에 없는 `ct_id` 를 가리키고("언제부터 상한을 넘었나"를 못 답함), 감사가 끊긴다 |

### 15-3. 새로 드러난 한계 (§14 보강)

- **④ `pm_count` 단조성** — 시뮬레이터의 `pm_count` 는 **프로세스마다 0에서 재시작**한다 (계약 §8-B-1 이 멱등 키에 `date` 를 넣은 바로 그 이유). 재기동 후 라벨의 `pm_count` 가 경계보다 작아지면 **전량 자격 미달**이 되어 보정이 조용히 영구 정지한다. 지금은 `자격 미달 누적 500건마다 경고 + rebuild 안내`로 **감지만** 한다 — 자동 리셋은 넣지 않았다(경계를 스스로 낮추는 것은 구레짐 라벨을 다시 들이는 일이라 안전 방향이 아니다). 실운영 전환 시에는 `pm_count` 를 fab 축의 단조 증가값으로 받아야 근본 해소된다.
- **⑤ clamp 분모의 출처** — champion 폴더에 `validation.json`·`manifest.acceptance` 가 없으면 `lean85.bench_pooled_rmse`(99.84, 동결 벤치)로 폴백하고 **경고를 남긴다**. champion 별 실측이 아니므로 P1 리허설에서 실제 어느 소스가 쓰이는지 로그로 확인할 것. 셋 다 없으면 보정 **비활성**(0 분모 방지).
- **⑦ 경계가 `qual_id` 채번 규칙에 기댄다** — `quals` 에 `pm_count` 컬럼이 없어 `QUAL-<날짜>-<챔버>-<pm_count>` 의 마지막 조각을 파싱한다. **ID 포맷 변경이 조용히 경계를 잃게 만드는** 구조라, 판독 실패 시 경계를 만들지 않고 경고 + 다른 소스로 넘긴다(잘못된 정수를 쓰는 것보다 낫다). 항구 해소는 `quals.pm_count` 컬럼 신설이며, 그때 파싱을 지우면 된다. 마이그레이션 `0010` 미적용 DB 는 `42703` 으로 식별해 완주한다(접속 장애는 fail-closed 로 올린다).
- **⑧ 챔버 토큰 앨리어싱** — `chamber_token` 은 영숫자만 남기므로 `SIM_CH_3`·`SIMCH3`·`SIM-CH-3` 이 전부 `SIMCH3` 이 된다. `ct_id` 채번과 경계 복구의 `LIKE` 패턴이 함께 앨리어싱되므로 현 `SIM_CH_1~4` 에서는 무해하지만, 챔버 명명이 늘면 확인할 것.
- **⑨ `mode` 의 YAML 함정** — 따옴표 없는 `off` 는 YAML 1.1 에서 **불리언 False** 다. 코드가 미지의 값을 `off` 로 잠그기 때문에 사고는 안 나지만, `params.yaml` 은 `"off"` 로 적는다 (헌법 7장에 한 줄 등재).

### 15-4. 아직 안 한 것 (P1 리허설·P2 전제)

- §10 리허설 4종(재계산 determinism · 요란 리셋 자격 · CAP 진입·해제 · DB 다운 중 동결→복구)은 **실브로커·실 DB 가 필요**하다. 단위 테스트는 같은 항목을 가짜 커넥션으로 덮었을 뿐이다.
- Scorecard 프로브(무보정 vs 보정 잔차, F8 재현)는 미실행 — **P2 `active` 의 게이트**라 이것 없이는 컷오버하지 않는다.
- D5 CAP 알림의 B 합류(결정 요청 ⓓ)는 이벤트 발행까지만 구현. 현재 `fdc.mlops` 소비자는 0곳이다.
- §12-1 `fdc.actual` 파티션 키(`wafer_id` → `chamber_id`)는 **유예** — 파티션 1개인 현 구성에서는 무영향이고, 소비자 3곳(A·sink·GW) 합의가 선행이다.
