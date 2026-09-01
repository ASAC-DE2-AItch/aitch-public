# 능동형 제조 AI 플랫폼 v4 — 시스템 간 데이터 통신 규약 (API & Event Contract)

**버전**: v4.5 (Recipe R2R 부활 + Model R2R + Cycle 기반 CT — 2026-07-06 개정) / **v4.6 초안 (2026-07-10 확장)**: ① `fdc.actual`(실측 C65 지연 피드백) 신설 ② `QualVerdictConfirmed` 이벤트(§8-B — 레짐 스위치 단일 소스) ③ CorrectionApplied recipe형 예시(§6) ④ §0 C54·C56 Window 정정 — **소비자 A·B 리뷰 대기 (7/15, 헌법 2-2)** + §0은 C 확인 1건 / **v4.6-b (2026-07-22)**: ⑤ **§8-C `ChamberRequalified` 스키마 신설** + §0 B 구독 필터 갱신 + §8-B quals 복구 갭 각주 (B6-3-c 요청 — 소비자 = 요청자 B라 리뷰 충족) / **v4.6-c (2026-07-28)**: ⑥ **§8-B-1 `pm_log.json` 기입 계약 신설** (B6-3-f — 작성자 = B 1곳·A 읽기 전용, 스키마·naive UTC 시간축·멱등 키). **소비자 A 리뷰 승인 필요**(헌법 2-2) + PM 승인(docs/ 소유권) / **v4.6-d (2026-07-30)**: ⑦ **§8-E 배포 자동화 개정** — 자동 검증 게이트를 문지기로, `ct2_auto_promote` 2모드 · `retrain_status` 값 목록 · 게이트 리포트/exit code 규약 명문화 (헌법 3-3 ③·1-1 예외 3 연동). 소비자 = A consumer·게이트웨이 — promote 신호 채널·resume 규약 **무변경**이라 하위 호환 / ⑧ **§8-E 배포 후 감시·자동 롤백 (WP-C)** — 롤백 promote 신호(`promote_*-rollback.json`) + 신규 `request_type='ct2_rollback'` 감사 행. promote 채널 재사용이라 consumer 무변경 / **v4.6-e (2026-08-07 — CT① 일간 재학습 루프 PR, PM 조건부 승인 후속 4건)**: ⑨ **§7 `fdc.mlops` payload 전 필드 등재** (구 1줄 요약 → `event_type`·`challenger`·`window_meta`·`model_version_before/after`·`retrain_status` 값 목록·발행/미발행 경계. 발행 코드 `ct1_orchestrator.publish_mlops` 기준, 소비자 0곳이라 파급 없음) / ⑩ **`ct_type` 현행화 `ct1_lgbm` → `ct1_xgb`** (XGBoost 확정 2026-07-22 — 코드는 처음부터 `ct1_xgb`, 문서 3곳만 뒤처져 있었다: 본 문서 §7 · `db/init.sql` 주석 · `DB_네이밍_컨벤션_v1.md`) / **v4.6-f (2026-08-07 — CT⓪ Model R2R 구현 PR)**: ⑫ **§2 `bias_applied` 필드 신설** (float·round 2 — `ct.model_r2r.mode ≠ off` 일 때만 발행. `predicted_c65` 의미 = "보정 후 최선 추정", 보정 전 = `predicted_c65 − bias_applied`. **소비자 B·Dashboard 리뷰 필요** — 헌법 2-2) / ⑬ **§0 Consumer Group `consumer-group-bias` 신설** (A CT⓪ updater — `fdc.actual` + `fdc.agent`) / ⑭ **§1-B 소비 배선 등재**(A·sink·GW 3곳 + `pm_count` 영속화)·파티션 키 `wafer_id`→`chamber_id` 제안 **유예** / ⑮ **§7-5 `ModelBiasCapExceeded` 이벤트 신설** (소비자 0곳, 알림 합류는 B 협의 대상) / ⑯ **§8-B quals 복구 갭 해소 확인 + CT⓪ 소비 등재** (P6-1 은 컬럼·쓰기·테스트까지 이미 구현돼 있었고 마이그레이션 `0010` 으로 보강 — CT⓪ 이 `qual_id` SEQ 를 요란 PM 경계로 읽는다는 **실질 인터페이스**를 명문화) — 이하 v4.6-e / ⑪ **§0 구독자 정본 목록에 A(sink) → `fdc.alert` 등재** (2026-08-05 M1 배선의 산문 각주 2곳만 갱신돼 있던 것을 토픽 목록·Consumer Group·§3 헤더까지 반영 — 헌법 2-2. 구독자 추가만이라 하위 호환)
**목적**: 팀원 A, B, C가 서로의 코드 완성을 기다리지 않고, 이 문서의 JSON 포맷만 믿고 즉시 병렬 개발을 시작할 수 있도록 한다.
**v4.2 → v4.4 주요 변경**: ① `fdc.r2r` → **`fdc.correction`** (Recipe 보정 폐기 → 실력치 재설정), ② `fdc.raw`에 **`is_qual`** 필드 추가, ③ prediction에 **`spc_flags`** 독립 채널 추가, ④ **`incident_id`** 도입 (승인은 Incident당 1건), ⑤ 컬럼 의미를 데이터 사전 v3 기준으로 정정 (C11=Vdc 등), ⑥ chamber_id는 **시뮬레이터가 부여하는 가상 챔버**(SIM_CH_1~4 — *2026-07-20 6→4 정정, 장비 1대=4PM 멘토 확정·시뮬 스펙 v1.3*).
**v4.4 → v4.5 변경**: ⑦ `fdc.correction`에 **`correction_type`**(limit/recipe) 추가 — recipe면 **시뮬레이터가 구독해 setpoint 반영**, ⑧ Agent 리포트 옵션 3종(Recipe/실력치/정비) + Supervisor 4지선다, ⑨ CT① 트리거 = ~~PM Cycle 종료~~ **일간 슬라이딩·창 365일 (2026-07-22 재확정, 멘토 7/21 — CT① 캐던스는 계약 필드가 아닌 A 내부 동작)**, Model R2R(bias)은 ct_type='ct0_bias'로 기록, ⑩ C33 = **RF time**(리셋=PM).

---

## 0. 컬럼 매핑 테이블 (C코드 → 물리적 의미)

> 전체 근거·신뢰도: `docs/데이터_사전_v1.md` (v3) 참조. **v4.2 계약의 컬럼 의미 다수가 정정되었음** — 아래가 최신.

### 핵심 식별자

| C코드 | 물리적 의미 | 비고 |
|-------|-----------|------|
| **C64** | Wafer ID | 기준키 (C38 중복) |
| **C20** | Lot ID | 기준키 (+C34 FOUP 슬롯 1~25) → LOT HOLD 파생 |
| **C6** | **Recipe ID** | 2종. ⚠️ Chamber 아님 (v4.2 오류 정정) |
| **C7** | Step 번호 | 1,4,5,6,7 — **Step 4가 메인 플라즈마** |
| **C24** | Chamber | 실데이터는 1종 — **가상 챔버는 시뮬레이터가 부여** |
| **C33** | **RF time** (RF 누적 시간 — 멘토 확인 v4.5) | 리셋 = PM 이벤트, PM Cycle 식별 기준 |
| **C42** | Step 4 안정화 플래그 | 1=점화 과도(첫 9초), 0=정착 — **Window 축의 기준** |
| **C41** | Step 내 경과 시간(초) | = C10 − C39 |
| **C65** | Target (Y) | Defect Test. **실시간 스트림에 포함 안 됨** |

### 핵심 센서 (판정·발표용 명칭)

| C코드 | 물리적 의미 (데이터 사전 v3) | 발표용 이름 | SPC 대상 |
|-------|--------------------------|-----------|---------|
| **C1** | RF Power 설정값 ✅확정 | RF_Power_Set | 설정값 — 참고용 |
| **C31** | RF 출력 실측 (Vdc와 -0.978 동행, 단위 미상) | RF_Forward | ✅ |
| **C32** | RF Reflected (디지털) ✅확정 | RF_Reflect | ✅ |
| **C61** | RF 음(-)측 피크 전압 후보 (~~Reflected 아날로그~~ 기각) | RF_Vneg | ✅ |
| **C62** | RF 전극 전압 (Vpp 계열) ✅확정 | RF_Vpp | ✅ |
| **C11** | **DC Self-Bias 전압 (Vdc)** ✅확정 (~~압력~~ 기각) | DC_Bias | ✅ ★핵심 |
| **C12** | Step 단위 갱신 Vdc 기준값 | DC_Bias_Ref | 참고용 |
| **C17 / C9** | 히터·척 온도 / 동일 열 거동 쌍 | Chamber_Temp / Temp_Aux | ✅ |
| **C15 / C16** | Gas Flow A 실측 / Gas B 변동신호(실측) | Gas_A / Gas_B_Actual | ✅ |
| **C48** | Main Gas Flow **설정값** (~~실측~~ 반전 정정) | Gas_B_Set | 설정값 |
| **C4 / C5** | 가스 Setpoint A/B ✅확정 (recipe·step 함수) | Gas_Set_A/B | 설정값 |
| **C57 / C58** | He Backside 제어/압력 후보 (~~챔버 압력~~ 기각) | He_Valve / He_Pressure | ✅ |
| **C18 / C27 / C54 / C56** | 매칭 과도편차 ✅ / 잔류편차 / Match 축 위치 | Match_* | C18·C27 과도 Window / **C54·C56 과도 Window (2026-07-12 재정정 — B raw-std 오류 발견: wafer-mean 기준 정착 480 vs 과도 148, 정착 ±3σ 광폭·타 step dead. 7/10 정착 이동 번복·PM 승인)** |
| **C25** | 베이스라인 드리프트 지표 (노후도 보조) | Baseline_Drift | 참고용 |
| **C59/C60** | 2채널 교대 판독 (상호배타) — `active=where(C59>1000,C59,C60)` + 채널 플래그로 재구성 | DualCh_Value | 재구성 후 |
| **C63** | 미상 (step별 운전점 계단 + 상승 드리프트) | Aux_63 | ✅ |
| **C50** | Step4 종료 이벤트 카운트 (Endpoint 후보) | EndEvent | 참고용 |

### Drop (모델 Input 제외 — 20개 + 중복 4개)

- 전체 NULL(8): C2, C13, C26, C37, C43, C47, C53, C55
- 상수(12): C3, C8, C14, C19, C21, C24, C28, C29, C30, C44, C45, C51
- 중복 → 기준키만 사용: C36(=C7), C35(=C34), C38(=C64), C40(=C10)

---

## Kafka 토픽 구성 (6개 + 초안 1개)

```
fdc.raw           ← 시뮬레이터(PM/PO) 발행 · A, B, GW(대시보드 표시 전용) 구독  [is_qual 필드 추가 — v4.4]
fdc.prediction    ← 팀원 A 발행 · B, Dashboard, A-sink(DB 적재 — #37) 구독
fdc.alert         ← 팀원 B 발행 (단일 경보 채널) · C(통합 Agent), PM(Incident 그룹핑), A-sink(spc_flags 적재 전용 — M1) 구독
fdc.correction    ← C(권고) / 승인워크플로(적용) 발행 · B(limit 한계 갱신), 시뮬레이터(recipe 적용), Dashboard 구독   [v4.5: correction_type=limit|recipe]
fdc.agent         ← 승인 워크플로(PM 인프라 + C의 Supervisor) 발행 · Dashboard 구독
fdc.mlops         ← 팀원 A 발행 (CT 트리거·결과)
fdc.actual        ← 시뮬레이터 발행 (label_delay 경과 후) · A(CT⓪ updater), A-sink(actual_c65·pm_count UPDATE), GW 구독 (Model R2R bias·CT① 채점)   [v4.6 초안 — A 소비 구현(#37 sink + CT⓪ updater 2026-08-07), 발행측(시뮬) 활성화 시 최종화]
```

### Consumer Group 규칙 (4인 체제)

```
팀원 A: consumer-group-prediction   → fdc.raw + fdc.actual(v4.6 초안 — Model R2R·CT 채점용) + fdc.agent(QualVerdictConfirmed만 — is_post_loud_pm·bias 리셋, v4.6. ※pm_log.json 기입은 B 단독 — A는 읽기 전용, §8-B-1)
팀원 A(sink): consumer-group-db-sink → fdc.prediction + fdc.actual + fdc.alert — wafer_predictions UPSERT 적재 (모델 consumer 와 분리 프로세스, DB 장애 격리 — #37 등재, 2026-07-23. fdc.alert 는 spc_flags 한 컬럼 UPDATE 전용 — 2026-08-05 M1 등재. 🆕 2026-08-07 CT⓪: prediction 의 `bias_applied`·actual 의 `pm_count` 도 적재 — 이 테이블의 쓰기 주체는 여전히 sink 1곳)
팀원 A(CT⓪): consumer-group-bias    → fdc.actual + fdc.agent(QualVerdictConfirmed·loud 만) — Model R2R bias 추정 전용 분리 프로세스 (`ct0_bias.updater`, 2026-08-07 신설). 쓰기는 `ct_decisions`(감사) + `control/ct0/bias_*.json`(서빙 전달) 두 곳뿐이고 `wafer_predictions` 는 읽기 전용. 예측 consumer 와 분리한 이유는 sink(#37)와 같다 — 보정 경로의 DB 장애가 예측 지연을 만들면 안 된다 (설계 D3)
팀원 B: consumer-group-spc          → fdc.raw + fdc.prediction + fdc.correction(limit만 처리) + fdc.agent(QualVerdictConfirmed + ChamberRequalified — 가한계 Phase 전환 / verify 30장 경계·Y-임계 1회 write. §8-C, 2026-07-22 추가)
시뮬레이터: consumer-group-simulator → fdc.correction(recipe만 처리 — 승인된 튜닝을 챔버 setpoint에 반영, v4.5)
팀원 C: consumer-group-agent        → fdc.alert  (통합 Agent Service 1개 — 내부에서 tool 3종 병렬 실행, v4.5)
PM/PO:  consumer-group-incident     → fdc.alert  (Incident 그룹핑 백엔드)
Dashboard(GW): consumer-group-dashboard → fdc.prediction  (읽기 전용 링버퍼 — S1 실배선 P5-4. 무커밋·earliest 재생: 재기동 시 토픽 재생 후 최근 N만 유지. 스키마 소비만 — 필드 변경 없음, 2026-07-22 등재)
Dashboard(GW): consumer-group-dashboard-raw → fdc.raw  (읽기 전용 링버퍼 — S1 센서 실곡선 W6-①. wafer 단위 settled 집계 → 현행 관리선 σ 환산(대표 스텝 **고정** 규칙: PREFER 4 → 없으면 최소 스텝. wafer마다 재선택 안 함 — 스텝 혼합 방지. 대표 스텝 표본 없으면 sig 생략·eng only). 무커밋·latest: raw는 row 물량이 커서 재생 없이 접속 이후만 소비 — 재기동 시 이력 리셋(대시보드 최근 창 특성). 스키마 소비만 — 필드 변경 없음, 2026-07-27 등재)
Dashboard(GW): consumer-group-dashboard-actual → fdc.actual  (읽기 전용 릴레이 — 실측 C65 지연 라벨 순배선 P6-2. 집계·σ 없음 = wafer 단위 확정 라벨 그대로. 무커밋·earliest: 실측 저물량·고가치 전량 소비. label_delay(≈13,900 wafer)로 시나리오 중 거의 미도착 — 발행자 활성 시 자동 작동. 스키마 소비만, 2026-07-28 등재)
```

> v4.2의 "C와 D가 다른 Consumer Group" 규칙은 폐기 — 통합 Agent Service가 한 번 수신해 내부에서 **논리적 Agent(tool) 3종(Recipe·실력치·정비 — v4.5)**을 병렬 실행한다 (헌법 1-2의 병렬성은 서비스 내부에서 보장. ~~2종~~은 v4.4 잔재).

---

## 1. 🏭 [시뮬레이터 → A, B (+GW 표시 전용)] 원천 센서 데이터 — `fdc.raw`

- 전송 단위: Row(3초 샘플) 1건 / 키: `chamber_id` *(2026-07-21 변경 — 구 wafer_id. 챔버=파티션=순서 보장: backlog 소비 시 파티션 인터리빙으로 순서 역전 → Nelson 정상 wafer 65% 드롭 실측(B). Incident 그룹핑 축과 일치, 소비자 A·B 리뷰)*

```json
{
  "wafer_id": "C64_1",
  "lot_id": "C20_1",
  "slot_no": 24,
  "chamber_id": "SIM_CH_3",
  "recipe_id": "C6_0",
  "step": 4,
  "seq_in_step": 2,
  "elapsed_in_step": 3.0,
  "stabilization_flag": 1,
  "pm_count": 24,
  "is_qual": false,
  "timestamp": "2026-07-13T10:00:00.000Z",
  "sensors": { "C1": 118.0, "C11": -335.0, "C62": 1809.0, "...": "..." }
}
```

> ⚠️ `chamber_id`는 시뮬레이터가 부여하는 **가상 챔버**(SIM_CH_1~4, v1.3). `is_qual=true`는 QUAL 모드 wafer (시뮬레이터 스펙 3절). C65는 이 스트림에 없음 — 실측은 `fdc.actual`(1-B, label_delay 지연)로만 도착.
> **wafer_id 접미사 규칙 *(P3-1, 2026-07-07)***: 멀티챔버 복제 시 중복 방지를 위해 원본 wafer_id에 챔버 접미사가 붙는다 — 예: `C64_516_CH_3`. 원본 번호가 필요하면 `_CH_` 앞부분을 취할 것 (소비자 A·B 파싱 주의).
> **변경 금지 필드**: wafer_id, chamber_id, step, timestamp (헌법 2-1)

## 1-B. 🧾 [시뮬레이터 → A] 실측 C65 지연 피드백 — `fdc.actual` *(v4.6 초안 — 오늘(7/7) 회의의 Model R2R 확정 + 소비자 A 리뷰 후 최종화)*

- 전송 단위: wafer 1건 / 키: `wafer_id` / DB: **`wafer_predictions` UPDATE** (`actual_c65`·`measured_at` — 별도 테이블 신설 취소, 2026-07-07) / 발행 시점: 해당 wafer 처리 후 **label_delay** 경과 시 — E6 비율 정의: `PM_cycle_wafers × (2개월/PM주기)` ≈ 13,900 wafer 상당 (231/일×60일 — `config/params.yaml`, 2026-07-08 처리율 정정)

```json
{
  "wafer_id": "C64_1",
  "lot_id": "C20_1",
  "chamber_id": "SIM_CH_3",
  "actual_c65": 1234,
  "processed_at": "2026-07-13T10:00:00.000Z",
  "measured_at": "2026-09-11T09:00:00.000Z",
  "pm_count": 24,
  "label_delay_wafers": 13900
}
```

> **소비 배선 (2026-08-07 — CT⓪ 구현 시 등재)**: 소비자는 3곳이다 — **A(CT⓪ updater, `consumer-group-bias`)** = 잔차 창 갱신 / **A-sink** = `actual_c65`·`measured_at`·`pm_count` UPDATE / **GW** = 대시보드 릴레이. `pm_count` 는 **자격 필터 축**이라 소비 측에서 버리면 안 된다(설계 §5) — sink 가 `wafer_predictions.pm_count` 로 영속화하고, CT⓪ 재기동 시 창 재계산이 그 값으로 구레짐 라벨을 걸러낸다.
> **파티션 키 제안 (설계 §12-1, 미확정)**: 현재 키는 `wafer_id` 인데, CT⓪ 의 상태 키는 `chamber_id` 단독이다. 파티션을 1개 이상으로 늘리는 시점에는 **키를 `chamber_id` 로 바꿔야** 한 챔버의 라벨이 여러 파티션에 흩어져 단일 소유가 깨지는 일을 막는다. 파티션 1개인 현 구성에서는 무영향이라 **컷오버 전까지 결정을 유예**한다 (소비자 A·sink·GW 합의 필요).
> **설계 근거**: fail bit(C65)는 fab out(WT) 후에만 확보 — 실시간 스트림(`fdc.raw`)에 C65 미포함 원칙은 유지하고, 지연 도착만 이 채널로 모사한다. 용도는 ① A의 **Model R2R** rolling residual 갱신 (`ct_type='ct0_bias'`, `ct_decisions` 기록, 상한 = config C7), ② **CT① 재학습** 채점·학습 라벨.
> ⚠️ **누수 가드 (헌법 1-3)**: `actual_c65`를 예측 Feature로 사용 금지 — bias 보정·채점·재학습 라벨로만 사용. 도착 시점에 이미 처리 완료된 과거 wafer의 라벨이므로 시간 순서상 누수 없음.
> **변경 금지 필드**: wafer_id, actual_c65, measured_at

## 2. 🤖 [A → B, Dashboard] 예측 결과 — `fdc.prediction`

- 전송 단위: Wafer 완료 시 / 키: `wafer_id` / DB: `wafer_predictions`

```json
{
  "wafer_id": "C64_1",
  "chamber_id": "SIM_CH_3",
  "predicted_c65": 715.2,
  "drift_score": 0.85,
  "anomaly_score": 0.12,
  "shap_top3": [
    {"sensor": "C11", "name": "DC_Bias", "contribution": 0.58},
    {"sensor": "C62", "name": "RF_Vpp", "contribution": 0.27},
    {"sensor": "C17", "name": "Chamber_Temp", "contribution": 0.15}
  ],
  "spc_flags": [ {"sensor": "C11", "rule": "N3"} ],
  "is_qual": false,
  "model_version": "lean85_20260720_163040_initial",
  "low_confidence": 0,
  "timestamp": "2026-07-13T10:00:10.000Z"
}
```

> **`spc_flags`는 SHAP과 독립 채널** — SHAP 순위에 룰 개입 금지 (헌법 3-3). 변경 금지 필드: wafer_id, predicted_c65, drift_score, shap_top3
>
> ⚠️ **`spc_flags` 는 `fdc.prediction` 에서 발행되지 않는다 (2026-08-05 M1)**. 필드 삭제가 아니라 **발행 중단**이다 — Nelson 판정은 B 소유이고 이 토픽의 발행자(A)는 그 값을 계산하지 않는다. A 는 계약 준수를 위해 `[]` 를 실어 왔는데, 그 빈 배열이 `prediction_sink` 의 `COALESCE(%s, spc_flags)` 를 **NULL 이 아닌 값**으로 통과해 B 가 채워 넣은 플래그를 매번 덮어썼다(감시① 구조적 사망). 정본 경로는 **`fdc.alert.prediction_context.spc_flags`**(아래 §2 alert 예시)이며 `prediction_sink` 가 그것을 `wafer_predictions.spc_flags` 로 적재한다. 위 예시의 `spc_flags` 줄은 **스키마 참고용**으로 남긴다 — 소비자는 이 토픽에서 해당 키의 부재를 정상으로 다뤄야 한다(B·대시보드 모두 이 필드를 읽지 않음을 확인).

> **`drift_score`·`anomaly_score` 산출 (2026-07-24 실값 전환 — A3-3)**: `LEAN85_AE_MODE=live` 시 AE 번들(`models/anomaly_ae/ae_v1` · z16_rev3_seg1/calib_v1)의 실추론값으로 발행한다. `anomaly_score` = `ae_score` [0,1] (**0.2 = Qual/알람 참조 임계** — `qual.pass_ae_max`·`ct.ae_anomaly_threshold`와 동일값). `drift_score` = `ae_drift_score` [0,1] (per-chamber EWMA · 0.2 알람선 정규화). ⚠ **1단계는 스키마 불변·의미 변경**(더미 난수 → 실분포) — 소비자 B·Dashboard 공지 필수. AE 미가동/실패 시 더미 난수 폴백(안전망 — 파이프라인 생존, 헌법 6-2). 확장필드(`ae_raw`·`ae_top_channels`·`ae_model_version`·`ae_calib_version`·`transient_context`)는 2단계 — 계약 §2-2 승인 후 `AE_PUBLISH_EXT=1`로만 발행.

> **`AE_OFFSET_CORRECT` — AE 입력 챔버 기준선 보정 (2026-08-08 등재, L8)**: `AE_OFFSET_CORRECT=1` 이면 A 는 AE 스코어링 **직전** 입력 trace 에서 `config/chamber_offsets.json` 의 챔버별 기지 오프셋을 차감한다(8/4 밤 리허설의 챔버 캘리 최소형 — B 7/10 전달분). **기본값 0 이라 현행 발행은 무영향**이다. ⚠ **스키마는 불변이고 `drift_score`·`anomaly_score` 의 의미가 바뀐다** — 위 "1단계 의미 변경"과 같은 종류의 변경이다. 켠 뒤의 두 값은 *절대 센서값 기준 이상도*가 아니라 **챔버 간 기준선 차를 제거한 뒤의 이상도**이며, 챔버별 상수 오프셋이 걷히므로 챔버 간 비교 가능성이 올라가는 대신 **오프셋 자체가 실이상인 경우(기준선 이동)를 흡수**할 수 있다. 보정은 **입력 전처리일 뿐 발행 필드·판정 경로를 추가하지 않는다**. 설정 로드 실패·미등재 챔버·차감 실패 컬럼은 **보정 없이 AE 실추론만** 진행하며(부정 캐시 + ★경고 1회 — 리뷰 M5·H3), 실제 차감 여부는 consumer 로그 `ae_mode` 로 구분한다: `live-decal`(차감됨) vs `live`(미차감).
>
> ⚠️ **켜기 전 통지 대상 = `anomaly_score`·`drift_score` 를 절대 임계와 비교하는 하류 전부**(헌법 7장 *"필드를 신설하고 그 값을 읽는 하류를 안 따라감"*). "B·Dashboard"만으로는 부족하다 — 실측 확인된 소비처는 아래 4곳이며, 이 점수들은 **눈금이 config 임계와 직접 맞물려 있어**(0.2 = Qual/알람 참조) 분포가 이동하면 임계가 같이 움직여야 한다:
>
> | 소비처 | 임계 | 켰을 때 위험 |
> |---|---|---|
> | `src/common/context_score/publisher.py` (B9 crazy anomaly arm) | `spc.crazy_wafer.anomaly_score_multiplier` × `ct.ae_anomaly_threshold` | `fdc.alert` → wafer 처분 경로 (헌법 1-1 물리 판정) |
> | `src/agent_b_spc/qual_recorder.py` | `qual.pass_ae_max` | Qual FAIL 제안 (현재 `qual.use_ae_axis: false` 로 비활성이나 `ae_anomaly_mean` 은 계속 적재) |
> | `src/agent_a_mlops/ct2_deploy_monitor.py` | 배포 전 P95 대비 `p95_drop_ratio` | **CT② 자동 롤백** — 스위치 플립과 배포 감시 창이 겹치면 분포가 계단식으로 바뀌어 **정상 번들이 롤백**된다 (헌법 3-3 ③ⓔ) |
> | Dashboard / `wafer_predictions` 적재 | 표시·이력 | 플립 전후 구간이 같은 축으로 그려진다 |
>
> 영향 없음 확인: **RTD 자동 챔버 정지**는 `rtd.exclude_ae_arm` 기본 true 로 AE 단독 근거를 배제한다(헌법 1-1 예외 4 경로 — 무영향). `db/init.sql` 은 FLOAT 컬럼이라 스키마 무영향. **플립은 CT② 배포 감시 창 밖에서** 하고, 플립 시각을 `models/CHANGELOG.md` 에 남겨 전후 구간을 가를 것.

#### 신규 필드 (2026-07-23 추가 — 헌법 2-2, 소비자 B·Dashboard 리뷰 대상)

| 필드 | 타입 | 발행 조건 | 사유 |
|---|---|---|---|
| `model_version` | string | **항상** | 예측 출처 추적. 값 = lean-85 재학습 stamp 폴더명(`lean85_<stamp>_<tag>`) = 모델 버전 단위(헌법 4-1). 더미 폴백 시 `"dummy"` — 실모델 예측과 구분해야 채점·Model R2R에서 오염되지 않음. `wafer_predictions` 적재 시 함께 저장 |
| `low_confidence` | int (0/1) | **`LEAN85_PUBLISH_LOW_CONFIDENCE=1`일 때만** (기본 0 = 미발행) | 요란 PM 직후 레짐-온셋 구간(`params.yaml` `lean85.onset_days`) 예측의 신뢰도 저하 표시. **참고용 신호이며 판정 근거 아님** — B의 SPC 판정·알람 로직 변경 불필요 |
| `bias_applied` | float (round 2) | **`ct.model_r2r.mode ≠ off` 일 때 항상** (off = 미발행 = 현행 페이로드와 동일) | 🆕 **CT⓪ Model R2R** (2026-08-07 — 설계 §12-2). 이 wafer 의 예측에 실제로 가산된 bias. `shadow` 구간에는 항상 `0`. **`predicted_c65` 의 의미 = "보정 후 최선 추정"** 이며, 보정 전 값은 `predicted_c65 − bias_applied` 로 복원한다. Qual wafer(`is_qual=true`)는 보정 대상이 아니라 항상 `0` (설계 D7) |

> **`bias_applied` 를 소비자가 알아야 하는 이유 (B·Dashboard)**: ⓐ **SHAP 항등식이 바뀐다** — 기존 `contribs 합 = predicted_c65` 가 `contribs 합 = predicted_c65 − bias_applied` 가 된다(가산은 SHAP 산출 **뒤**에 일어나며, 룰·SHAP 순위에는 개입하지 않는다 — 헌법 3-3). ⓑ 무보정 값과 비교하는 화면·채점 로직은 이 필드로 되돌려 계산해야 한다. ⓒ 발행 스위치는 env 가 아니라 `params.yaml` `ct.model_r2r.mode` 단일 소스이며, **`active` 전환은 Scorecard 실증 후**다 (설계 §13 P2).
> **하위 호환**: 세 필드 모두 **추가**이며 기존 필드의 이름 변경·삭제 없음(헌법 2-2). `low_confidence`는 소비자 승인 전까지 env 플래그로 **발행 차단**되어 있어 기존 소비자에 영향 없음 — 승인 후 플래그를 1로 전환한다. `bias_applied` 도 같은 규율로 `mode: off` 동안 발행되지 않는다.
> **변경 금지 필드에는 미포함** — 신규 필드이므로 소비자 승인 후 재평가.
> `lot_id`·`recipe_id`는 `wafer_id` 종속이라 이벤트에 싣지 않고 wafer 마스터 조인으로 해석한다(정규화 — 2026-07-23 A 결정).

## 3. 🚨 [B → C, PM, A-sink] SPC 경보 — `fdc.alert` (단일 경보 채널)

- 전송 단위: 위반 즉시 / 키: `chamber_id` / DB: `spc_violations`

> **구독자 A(sink) 등재 (2026-08-05 M1 — 구독자 정본 목록 반영 2026-08-07, 헌법 2-2)**: `prediction_sink`(`consumer-group-db-sink`)가 본 토픽을 추가 구독해 `prediction_context.spc_flags` 를 `wafer_predictions.spc_flags` **한 컬럼만** UPDATE 한다 (§8-E 감시① 교차 침묵의 데이터 소스). wafer 키 = **`prediction_context.wafer_id`** — alert 최상위에는 `wafer_id` 가 없다. 대상 행이 아직 없으면(토픽 간 순서 무보장) 무해 no-op 후 재도달에 의존한다.
> ⚠️ **헌법 1-2 무저촉**: A-sink 는 **적재 전용 소비자**로 Nelson 판정·Agent 트리거를 하지 않는다. 1-2 가 금지하는 것은 "`fdc.alert` 를 **우회한** Agent 직접 호출"이며, 본 구독은 우회가 아니라 경유다. A 가 Nelson 을 자체 계산하는 것은 금지(판정 소유 = B, 헌법 3-3 SHAP 독립 채널).
> **발행자 B 무영향**: 신규 필드·형식 변경 없이 **구독자만 추가**된 건이라 하위 호환이다.

```json
{
  "alert_id": "ALERT-20260713-001",
  "timestamp": "2026-07-13T10:05:00.000Z",
  "chamber_id": "SIM_CH_3",
  "violations": [
    {
      "rule_id": "N3",
      "sensor": "C11",
      "sensor_name": "DC_Bias",
      "window": "settled",
      "severity": "WARNING",
      "description": "N3: 6점 창 (C64_1234..C64_1239)",
      "current_value": -302.0,
      "limit_version": "v1",
      "control_limit_upper": -280.0,
      "control_limit_lower": -340.0
    }
  ],
  "tttm": {
    "reference": "fleet_median",
    "score": 2.3,
    "top_gap_sensor": "C11",
    "gap_pct": 4.1,
    "reference_suspect": false,
    "suspect_sensors": []
  },
  "context_score": 72,
  "suspect_window": { "start_wafer": "C64_9981", "end_wafer": "C64_1", "basis": "N3 추세 시작점 소급", "member_wafers": ["C64_9981","C64_9983","C64_9990","C64_1"] },
  "prediction_context": { "wafer_id": "C64_1", "predicted_c65": 715.2, "anomaly_score": 0.31, "shap_top3": ["C11","C62","C17"], "spc_flags": [{"sensor":"C11","rule":"N3"}] }
}
```

- `violations[].window`: `"transient"`(C42=1) / `"settled"` — **Window 축** (v4.4)
- `violations[].description`: `"<룰>: <N>점 창 (<창 범위>)"` — 생산 지점은 `nelson_engine.py` **한 곳**이라 형식이 임의로 바뀌지 않는다. ⚠️ **괄호 안은 자리표시자가 아니라 실제 `wafer_id`** 다 *(2026-08-10 정정 — B 리뷰 #147. 구 예시 `(W0..W5)` 는 실제로 나올 수 없는 형태였다)*. **창 크기가 1이면 `..` 자체가 없다** — `N1: 1점 창 (C64_665)` 처럼 wafer_id 하나만 들어간다(`len(win) > 1` 일 때만 범위 표기). 형식 정합 사항이고 판정에 쓰는 값은 아니다.
- `tttm.reference_suspect=true`: 다수 챔버 동시 이탈 → 참조/공통 원인 의심 (역방향 룰). **(2026-08-05 decouple)** 판정은 `top_gap_sensor`(=경로② worst)가 아니라 **역방향 룰(reverse_rule) 발동 여부**에 묶는다 — 이 챔버의 어느 센서 그룹이든 공통이동이면 true. `top_gap_sensor`(worst)와 실제 공통이동 센서가 다를 수 있어(예: worst=C62, 공통이동=C17) masking을 막는다.
  - **⚠️ 판정을 바꾸는 소비자 (스키마 무해 통과 아님)**: 이 값은 C Supervisor 판정 절차의 **STEP 0**에서 읽혀, `true`면 4지선다 ①②③을 건너뛰고 **곧바로 ④ escalate로 단락**한다(`agent_service/app/supervisor.py:56-58`). decouple로 `true` 빈도가 오르면 escalate 직행 빈도도 오른다 — **이 필드를 다시 손댈 땐 C 판정 로직 영향을 반드시 함께 볼 것**(단순 `extra="ignore"` 소비자로 분류 금지, PR #113 C 리뷰).
- `tttm.suspect_sensors`: `list[str]` — 역방향 룰이 실제로 발동한 **공통이동 센서 C코드 목록**(예: `["C17"]`). 공통이동 없으면 `[]`. `top_gap_sensor`(worst·경로② 진단용)와 별개 채널이다. `tttm_comparisons.suspect_sensors`(JSONB)와 동기. *(additive·optional — 미반영 소비자는 그대로 동작, §2-2 하위호환)*
- Context Score 구간·Agent 가동 조건(< 31 미가동)은 v4.2와 동일
- **변경 금지 필드**: wafer_id, chamber_id, rule_id, severity, context_score

> **additive 2건 등재 (2026-07-29 — B 요청 `B4-1_계약필드_반영요청_C_v1` · 소비자 C 수용)**
> 둘 다 **optional**이라 미발행 시에도 기존 소비자는 그대로 동작한다 (§2-2 하위호환).
>
> | 필드 | 타입 | 규격 |
> |---|---|---|
> | `suspect_window.member_wafers` | `list[str]` (optional) | 구간 내 **실재** wafer 목록 — **끝점 포함 · 여러 위반 창의 합집합 · dedup · wafer 번호(=시간) 오름차순**. 번호를 연번 전개한 값이 **아니다**(실데이터 결손률 62.4%). Nelson 엔진이 위반 판정에 실제로 쓴 룰 창의 레코드 목록이다. |
> | `prediction_context.anomaly_score` | `float` (optional) | **[0,1]** — A `ae_score` 를 ECDF 캘리브레이션한 값. **`0.2` = VAL P98.5 = Qual 임계**로 `qual.pass_ae_max`·`ct.ae_anomaly_threshold` 와 **동일 눈금**이라 config 임계와 직접 비교가 성립한다. B9 원문의 `3×기준`(`spc.crazy_wafer.anomaly_score_multiplier`)과는 **다른 축**이므로 3.0 과 비교하지 않는다. |
>
> **소비자(C) 동작**: `member_wafers` 는 wafer별 처분 대상 목록의 **1순위 출처**(미도착 시 A 예측 ∩ 구간 → 끝점 폴백). `anomaly_score` 는 **값의 유무가 아니라 임계 비교**로 "이중 확인 동반"을 판정한다(`spc.crazy_wafer.anomaly_score_min`). ⚠️ **그 하한은 현재 미확정이라 config 미등재** — 확정 전까지 SCRAP 을 내지 않는다(fail-safe, 합의안 B9-a).

## 4. 🧩 [PM 백엔드] Incident 그룹핑 — DB `incidents`

`fdc.alert`를 구독해 같은 `chamber_id` + 겹치는 시간창의 알람을 **Incident 1건**으로 병합. 승인 요청은 Incident당 1건 (헌법 1-4). LangGraph `thread_id` = `incident_id`.

```json
{ "incident_id": "INC-20260713-SIMCH3-001", "chamber_id": "SIM_CH_3", "alert_ids": ["ALERT-...-001","ALERT-...-002"], "lifecycle": "analyzing", "claimed_by": null, "reopen_count": 0 }
```

> **멤버십 저장 (P4-3, 2026-07-13)**: 알람↔Incident 편입은 `incident_alerts`(신규 테이블 — **정본**)에 기록. `spc_violations.incident_id`는 B 엔진 가동 후 옵션 스탬프(정본 아님). 병합축은 dev에서 **시간**(params `incident.merge_gap_sec`) — mock `timestamp`=wall-clock이라 wafer-gap(B5) literal 불가, 실물 정합은 params 주석. 채번 `INC-<YYYYMMDD>-<CHAMBER>-<SEQ>`, 구현 `src/orchestrator/incident_grouper.py`(PM 소유). 오프셋 at-least-once(수동 커밋) + `alert_id` UNIQUE로 idempotent.

### 4-A. Lifecycle 전이 정본 (P6-1, 2026-07-28 — 전이 주체 명문화)

| 전이 | 주체 | 시점 |
|---|---|---|
| (생성)→`open` | 그루퍼 | 신규 incident 생성 (모든 alert — 크기 1·저점수 포함) |
| `open`→`analyzing` | 승인 그래프 | merge_reports 진입 (리포트 수합 시작) |
| `analyzing`→`pending` | 승인 그래프 | 게이트 `interrupt()` = PENDING 오픈 (Incident당 동시 1건 — 헌법 1-4) |
| `pending`→`actioned` | 승인 그래프 | approve/modify 판정 커밋 (원자 — approval_records와 단일 트랜잭션) |
| `actioned`→`verifying` | 승인 그래프 | `fdc.correction` 발행 직후 = 효과 관측 창 시작 (actioned는 순간 상태·감사 기록은 유지) |
| `verifying`→`reopened` | 그루퍼 | 관측 창 내 같은 챔버 알람 재병합 = 재발 (B6 — reopen_count++) |
| `verifying`→`closed` | 그루퍼 sweep | `incident.verify_close_sec` 무재발 경과 시 자동 종결 (PM 결정 2026-07-28 — 시간 기반, B 30장 관측 이벤트 연동은 후속) |
| `pending`→`analyzing`/`closed` | 승인 그래프 | reject (재분석/종료 = 엔지니어 선택) |
| `pending`→`reopened` | 승인 그래프 | escalate (근거 패키지 + reopen_count++) |

**pending 타임아웃 = 표시만** (PM 결정 2026-07-28): `incident.pending_timeout_sec` 초과 시 `/approvals/pending` 응답에 `is_aged=true` 동봉 — 자동 전이·자동 escalate 없음 (헌법 1-1 HITL: 결정은 항상 사람).

### 4-B. incident_type + B9 crazy 라우팅 (P6-1, 2026-07-28)

- **`incidents.incident_type`** (신규 컬럼 — 3-2 ALTER ADD): `NULL`=일반 SPC 사건 / **`crazy_spot`**=B9 crazy 1장 격리 건 / `regime_transition`=요란 PM 전환 건(BL1 — Qual 경로 작업 시 기입). S3(조사 큐)·S6(처분 화면) 분기와 Scorecard 채점 소스.
- **B9 마커 라우팅**: `violations[]`에 `rule_id="B9"`(합성 마커 — §3, Nelson 룰 아님·`sensor="C65"`는 종류 마커라 **control_limits 조회 금지**)가 있으면 그루퍼가 ⓐ 신규 생성 시 `incident_type='crazy_spot'` (기존 incident 병합 시엔 기존 type 유지) ⓑ 처분 **잠정 행 자동 INSERT** (아래 상태 전이 모델). 멱등: 같은 (wafer_id, incident_id) 재전달 시 skip.
- **"1장=HOLD·연속=R2R" 성립 구조** (기획서 v4.5 라우팅 룰): 그루퍼는 모든 alert에 incident를 만들므로(크기 1로 태어나 병합으로 성장) 스팟도 incident_id를 자연 확보 — `wafer_dispositions.incident_id`·`approval_records.incident_id` NOT NULL과 승인 상태머신(thread_id=incident_id)을 그대로 재사용한다 (nullable 완화안 반려, PM 2026-07-28). 2장째 crazy가 병합 창 안에 오면 같은 incident로 병합 = 연속 경로 자연 승격.

**wafer 처분 행 상태 전이 모델 (P6-1 커밋2, 2026-07-28 — incident lifecycle과 대칭)**: `wafer_dispositions` 행은 **잠정 → 확정** 두 상태를 가지며, 작성 주체·컬럼 역할이 분리된다.

| 상태 | 작성 주체·시점 | 채우는 컬럼 | 비우는 컬럼 |
|---|---|---|---|
| **잠정** | 그루퍼 — B9 alert 수신 즉시 (자동 보호, 승인 전) | `status='HOLD'` · `hold_reason`(격리 사유+B9 arm 수치 코드 인용) · `system_recommendation='SCRAP'`(판정 트리 STEP4 코드 판정 후보) | `recommendation_basis`·`decided_*` — **확정 몫** |
| **확정** | 승인 게이트 — manual 승인 resume 시 Brief `per_wafer[]` 순회 (§6) | `status`=장별 판정 · `system_recommendation`·`recommendation_basis`(C 코드 조립 정형 문구 — 정본) · `decided_by`·`decided_at` | — |

- 전이 규칙 = **`finalize_dispositions`**: 행 있으면(잠정 선행) **UPDATE**, 없으면(비-B9 경로 — N3 추세 incident의 처분 등) 확정 상태로 **INSERT**. 멱등(같은 확정값 재기입 무해 — resume 재시도 안전). **두 작성자가 같은 컬럼을 경쟁하지 않는다** — grouper는 `hold_reason`만, 승인은 `recommendation_basis`만.
- SCRAP/RELEASE **확정은 항상 사람**(승인 게이트 경유 — 헌법 1-1). 잠정 HOLD는 물리 조치가 아닌 보호 기록. DB 스키마 무변경(테이블 이미 wafer당 1행 grain).
- **표시 규약 (C 소비자 리뷰 확인 2 — PM 채택 2026-07-29)**: 잠정/확정은 `decided_at IS NULL`로 구분한다. 잠정 행의 `system_recommendation`은 "STEP 4 **후보** 마커"지 판정이 아니다 — 최종 판정은 예측값 비교(STEP 2)를 거쳐 갈릴 수 있다(예: predicted_c65 ≤ 임계면 RELEASE). 표시 계층(S6)은 미확정 행을 **후보 표기**(예: 'SCRAP 후보' + 잠정 배지)로 렌더한다.

## 5. 🔧 [C 통합 Agent → Supervisor 노드] 병렬 리포트 3종 *(v4.5 / 2026-07-16 슬림화 — 스키마 정본 일원화, C 요청)*

- 전달: DB `recipe_corrections`(옵션①), `limit_corrections`(옵션②), `agent_reports`(옵션③ 및 통합) 적재 → 그래프 내 수합 / `fdc.correction`에 권고 로깅
- **스키마 정본 = `docs/Agent_프롬프트_라이브러리_v1.md`** — 옵션① Recipe Tuning은 라이브러리 §1, 옵션② 실력치 재설정은 §2, 옵션③ 정비는 §3의 "출력 스키마(guided_json)"가 유일한 필드 정의다 (소유 C). 본 절에 있던 구 JSON 예시 2건은 정본과 불일치(중첩 `recipe_tuning` 구조 · `report_id`에 CHAMBER 누락 · `hypothesis` enum 축소 등)로 **삭제** (2026-07-16, C 요청 — 이중 유지보수 방지). 이후 리포트 필드 변경은 라이브러리 PR로만 하며, §2-2 스키마 변경 규칙(소비자 리뷰 승인)을 동일 적용한다.
- 계약이 강제하는 **불변 요건** (라이브러리가 바뀌어도 유지):
  - 공통 필수 키: `report_id` · `incident_id` · `hypothesis` · `confidence` · `rag_evidence[]`
  - `report_id` = 6-4 업무 ID 포맷 **`<PREFIX>-<YYYYMMDD>-<CHAMBER>-<SEQ>`** (옵션① `RCP` / ② `LIM` / ③ `MNT`)
  - 수치의 출처: 옵션①은 **B5-4 Recipe 정량 API**, 옵션②는 **B 실력치 재산정 API** 산출값을 그대로 사용 — C가 수치를 재계산·수정하는 것 금지 ('정량은 B, 근거는 C' RnR 경계)
  - 옵션③ 계보: 구 v4.4 "옵션②"에서 번호 이동, v4.2의 5절 포맷 계승 (`alert_id` → `incident_id` 치환, `hypothesis: "true_fault"`)

## 6. 📑 [Supervisor(C 로직) → 승인 게이트 → Dashboard] 통합 Brief — `fdc.agent`

- DB: `agent_reports` / 트리거: **리포트 3종(recipe·limit·manual) 수합 완료 시** Supervisor 노드 실행 → `interrupt()` = PENDING *(v4.5 정정 — 구 "두 리포트"는 v4.4 잔재)*

```json
{
  "report_id": "SUP-20260713-001",
  "incident_id": "INC-20260713-SIMCH3-001",
  "context_score": 72,
  "suspected_root_causes": ["PM 직후 정상 상태 이동 (C33 리셋 12 wafer 전)"],
  "parallel_options": {
    "limit_option": { "report_id": "LIM-...", "action": "C11 실력치 +4.1% 재설정", "feasibility": "HIGH (다운타임 없음)", "confidence": 0.88 },
    "manual_option": { "report_id": "MNT-...", "action": "Focus Ring 검사", "feasibility": "LOW (8h 정지)", "confidence": 0.79 }
  },
  "supervisor_recommendation": {
    "decision_frame": "4지선다",
    "selected": "limit_option",
    "reason": "PM 직후 + 과거 정상 이동 사례 일치, 진성 열화 증거 부족",
    "confidence": 0.88
  },
  "wafer_disposition": {
    "held_wafers": ["C64_995", "C64_1001"],
    "recommendation": "SCRAP",
    "reason": "HOLD 2장 중 1장 예측 초과 — 스크랩 검토, 나머지 해제 권고",
    "per_wafer": [
      {"wafer_id": "C64_995",  "recommendation": "RELEASE", "predicted_c65": 701.3,
       "reason": "predicted_c65 701.3 ≤ B9 임계 1572 — 품질 근거 없음"},
      {"wafer_id": "C64_1001", "recommendation": "SCRAP", "predicted_c65": 1650.0, "anomaly_score": 0.83,
       "reason": "predicted_c65 1650 > B9 임계 1572 · anomaly 0.83 ≥ 임계 0.5 동반 — 이중 확인 충족(1장 spot)"}
    ]
  },
  "approval_status": "PENDING"
}
```

> **`per_wafer` (2026-07-28 등재 — C 요청·PM 승인, 헌법 2-2 추가만·기존 3필드 불변)**: wafer별 개별 판정 목록 — 판정 트리(기획서 §4-3 정본) STEP 3·4가 "초과 **장수**"로 갈리므로 장별 판정이 구조적으로 필요 (crazy 1장=스크랩 / 여러 장=R2R). **top-level `recommendation`은 `per_wafer` 중 최중 판정(SCRAP>HOLD>RELEASE)을 코드가 파생**한 요약(파생 주체 = C `apply_disposition` 한 곳 — 어긋남 방지)이고, `per_wafer[].reason`은 **코드가 조립하는 정형 문구**(수치 인용 — 감사 추적, LLM 무관). top-level `reason`만 LLM 서술. 승인 시 게이트가 `per_wafer`를 순회해 처분 행을 **잠정→확정 전이**한다 (§4-B 상태 전이 모델 — wafer당 1행, DB 스키마 무변경). `predicted_c65`·`anomaly_score`는 대응 컬럼 없이 정형 문구에 인용(집계 쿼리 필요 시 ALTER는 3-2 허용 범위, 현 범위 밖). 미지원 소비자는 무시 가능(default `[]`). **정형 문구 규약 (C 소비자 리뷰 2026-07-29 반영)**: SCRAP reason **형식**은 계약 고정 — `predicted_c65 {값} > B9 임계 {P99} · anomaly {값} ≥ 임계 {anomaly_score_min} 동반 — 이중 확인 충족(1장 spot)`. **임계 숫자는 config 현행값 인용**(계약이 숫자를 고정하지 않음 — 코드가 조립 시 반영). anomaly '동반'은 값 유무가 아니라 **임계 비교**다 — 유무 판정이면 anomaly 0.01(정상)도 동반이 되어 SCRAP 과잉 발동 (C 지적 승인).

- 승인 액션: `POST /incidents/{id}/decision` `{action: approve|modify|reject|escalate, option, modified_value?(modify), reason(reject 필수), reanalyze?(reject 전용: true→analyzing 재분석 / false→closed 종료 — 엔지니어 선택, 자동 루프가드 없음)}` → LangGraph `Command(resume)`
- 승인 시 → **`fdc.correction`에 CorrectionApplied 발행** (`correction_type` 포함): limit이면 **B가 한계 즉시 갱신**, recipe면 **시뮬레이터가 setpoint 반영 후 효과 검증 개시** (v4.5)
- **변경 금지 필드** (`fdc.correction`): incident_id, correction_type, sensor, limit_version, effective_from (헌법 2-1)
- **추가 필드 (2026-07-21 — B 요청·소비자 B 리뷰, 헌법 2-2 추가만)**: limit형 `correction_id` (= `limit_corrections` PK — B `apply_approved`가 적용 행을 집는 참조 키) · recipe형 `recipe_correction_id` (대칭, `recipe_corrections` PK). **채번 주체 = B 엔진(RecalcProposal)** — C 리포트·승인 워크플로는 값 승계만 (LLM 생성·재채번 금지, 헌법 6-4). 포맷 = `LIM-`/`RCP-<YYYYMMDD>-<CHAMBER>-<SEQ>`
- **정비(manual_option) 승인 이벤트 = `MaintenanceApproved` → `fdc.agent`** *(2026-08-12 개명 — 구 `WaferDispositioned` → `fdc.correction`)*: 8/11 정비/처분 분리 이후에도 정비 승인이 처분 이름의 이벤트를 correction 채널에 내보내던 잔재를 정리했다. `WaferDispositioned`는 기획서 §7 정의(*"wafer 스크랩/해제 판정 완료"*) 그대로 **처분 확정(`apply_disposition`) 전용으로 환원** — 발행 위치·페이로드(finalized·counts·held_back) 불변. 하위 호환: `fdc.correction` 소비자는 B(limit만)·시뮬레이터(recipe만)·Dashboard(§0)라 필터 무영향이고, `fdc.agent`는 `event_type` 필터 관례(§8-B)로 미지 이벤트 skip. 구현 = `src/orchestrator/approval_graph.py` `apply_target` · 라우팅 = `events.py`(의도적 DEFAULT)

**Brief·리포트 확장 필드 *(v4.6 추가 2026-07-11 — 프롬프트 라이브러리 v1과 동시 정의, P4-6 리뷰 일괄)***: 리포트 3종(§5)에 `report_id`(recipe는 `RCP-` — 6-4 정합)·`rationale`·`escalate_reason`·`evidence_cards`·`uncertainty`, Brief(§6)에 `supervisor_recommendation.verdict`(enum: equipment_fault/process_shift/baseline_aging/escalate)·`evidence[]`(정확히 3)·`counter_evidence`(정확히 1)·`parallel_options.*.rejected_because` — **전부 추가만(기존 필드 불변, 하위 호환)**. 스키마 단일 소스 = `docs/Agent_프롬프트_라이브러리_v1.md` §1~§5 (소유 C). 소비자 = Dashboard(S4 렌더).

**CorrectionApplied 예시 — `correction_type='recipe'`** *(2026-07-10 추가 — 시뮬레이터 구독부(P5-3 선행) 구현과 동시 명문화. limit형은 sensor·limit_version 사용, recipe형은 parameter_id·delta 사용 — 미사용 필드는 null 유지(이름 삭제 금지)):*

```json
{
  "incident_id": "INC-20260713-SIMCH3-001",
  "correction_type": "recipe",
  "recipe_correction_id": "RCP-20260713-SIMCH3-001",
  "chamber_id": "SIM_CH_3",
  "recipe_id": "C6_0",
  "step": 4,
  "parameter_id": "C1",
  "value_current": 118.0,
  "value_proposed": 116.5,
  "delta_pct": -1.3,
  "sensor": null,
  "limit_version": null,
  "effective_from": "2026-07-13T11:00:00.000Z",
  "approved_by": "engineer_01"
}
```

> **modify(수정 승인) 정본 규칙 (2026-07-23 — B5-1 수정승인 필드규칙 스펙 안ⓐ, PM 승인)**: 엔지니어 수정값의 **적용 정본은 `limit_corrections.center_modified / ucl_modified / lcl_modified`**다. 승인 워크플로가 이 컬럼들을 fdc.correction **발행 전에 커밋** 기입한다(순서 규약 — 뒤집히면 B가 원안을 적용하는 레이스). B `apply_approved`는 `COALESCE(수정값, 원안 *_after)`로 읽으며 원안은 불변(감사: AI 원안 vs 엔지니어 보정 병존). `value_proposed`는 **표시·감사용 정보성 필드**로, B는 적용에 사용하지 않는다. 구조 가드 실패 시 `limit_corrections.status='APPLY_FAILED'`(미적용·대시보드 노출·재입력 유도). recipe형 수정값의 DB 반영은 B5-4 후속.
> **APPLY_FAILED 재처리 (P6-1, 2026-07-28 — B5-1 스펙 §5-3 규약의 오케스트레이터 구현)**: 엔지니어 재입력 시 게이트웨이 `POST /corrections/{correction_id}/retry`가 [`*_modified` 갱신 + `status→PROPOSED` 되돌림 + `approval_records` MODIFIED 감사 행]을 단일 트랜잭션으로 처리한 뒤 **fdc.correction을 재발행**한다 — 재발행 페이로드는 본 절 형태 + `retry_of: "APPLY_FAILED"`(신규 정보성 필드 — 소비자 무시 가능). B는 재발행분을 받아 `apply_approved` 재실행(PROPOSED+`*_modified` 재검증 — B 코드 무변경, 통과=APPROVED_APPLIED / 재실패=다시 APPLY_FAILED 루프). 상태 전이 소유: `APPLY_FAILED→PROPOSED`·재발행=**오케스트레이터** / `PROPOSED→APPROVED_APPLIED`·`→APPLY_FAILED`=**B**.


> 시뮬레이터 적용 규칙 (v4.5): (chamber×recipe×step×parameter) 스코프로 setpoint 이동 — `value_current/proposed` 있으면 절대 delta, 없으면 `delta_pct` 배율. **|delta_pct| > D6(±3%)는 수신 측에서도 거부** (무승인 초과 방어선 이중화). 적용 이력은 sim_events 기록 → Scorecard "튜닝 후 분포 정상화" 채점.

## 7. 🔄 [A → 워크플로] CT 트리거·결과 — `fdc.mlops`

> **v4.6-e 개정 (2026-08-07 — CT① 일간 재학습 루프 PR)**: 그동안 본 절은 한 줄 요약뿐이라 **실제 발행 payload 를 문서에서 알 수 없었다.** 발행 코드(`src/agent_a_mlops/lean85/ct1_orchestrator.publish_mlops`) 기준으로 전 필드를 등재한다. 아울러 `ct_type` 값을 **`ct1_lgbm` → `ct1_xgb`** 로 현행화한다 (XGBoost 확정 2026-07-22, 헌법 3-3 — 코드 정본은 이미 `ct1_xgb`. 문서만 뒤처져 있었다).

- 전송 단위: **CT 사이클 1건 종결 시 1메시지** / 키: 없음(단일 파티션) / 발행자: **A** (`ct1_orchestrator`) / 구독자: **현재 없음**
- **감사 정본은 이 토픽이 아니라 `ct_decisions` 테이블이다.** 발행은 실패해도 흐름을 막지 않는다(warning 로그 후 계속) — 브로커 미가동 시에도 CT 사이클은 정상 종결되어야 하기 때문. 따라서 **소비자는 이 토픽을 유실 가능한 알림으로 다뤄야 하며, 정합이 필요한 조회는 `ct_decisions` 를 봐야 한다.**
- ⚠️ **CT②(AE)는 현재 이 토픽을 발행하지 않는다** — `ct2_orchestrator` 의 감사 경로는 `ct_decisions` + `approval_records` + `models/CHANGELOG.md` 3중 기록(헌법 3-3 ③)이다. 아래 `ct_type: "ct2_ae"` 는 **예약값**이며, CT② 발행을 추가할 때 본 절을 동시 갱신한다(헌법 4-3).

```json
{
  "event_type": "CtDecision",
  "ct_id": "CT-20260807-ALL-001-XGB",
  "ct_type": "ct1_xgb",
  "trigger_reason": "daily_sliding",
  "retrain_status": "AUTO_PROMOTED",
  "model_version_before": "lean85_20260806_220310_daily",
  "model_version_after": "lean85_20260807_220412_daily",
  "rmse_before": 54.80,
  "rmse_after": 51.93,
  "champion_challenger_result": "PROMOTED",
  "challenger": "lean85_20260807_220412_daily",
  "window_meta": {
    "now": "2026-08-07T22:00:00",
    "cutoff": "2026-08-06T22:00:00",
    "start": "2025-08-06T22:00:00",
    "start_base": "2025-08-06T22:00:00",
    "window_days": 365.0,
    "buffer_days": 1.0,
    "effective_window_days": 365.0,
    "require_complete_loud_regime": true,
    "extended": false,
    "extended_days": 0.0,
    "extend_failed": false,
    "complete_loud_regimes_in_window": 2,
    "regime_used": null,
    "pm_events_n": 7,
    "n_wafers": 84210,
    "n_train_wafers": 84210,
    "n_eval_wafers": 231,
    "gated": null,
    "trainset": "/…/ct1_trainset_20260807_220000.csv.gz"
  },
  "created_at": "2026-08-07T22:41:07+00:00"
}
```

### 7-1. 필드 명세

| 필드 | 타입 | 발행 조건 | 의미 |
|---|---|---|---|
| `event_type` | string | **항상** | 고정값 `"CtDecision"`. `fdc.agent` 의 필터 관례(§8-B)와 동일 패턴 — 향후 본 토픽에 타 이벤트가 추가돼도 소비자가 `event_type` 으로 분기할 수 있게 선등재 |
| `ct_id` | string | **항상** | `CT-<YYYYMMDD>-<CHAMBER>-<SEQ>-XGB` (헌법 6-4 · CT②의 `-AE` 접미와 대칭). `CHAMBER` 는 **`ALL` 고정** — lean-85 는 단일 전역 모델이다(ADR-0001). `ct_decisions.ct_id` 와 동일 값 = **조인 키** |
| `ct_type` | string | **항상** | `"ct1_xgb"` (구 `ct1_lgbm`) \| `"ct0_bias"`(Model R2R) \| `"ct2_ae"`(예약 — 위 각주) |
| `trigger_reason` | string | **항상** | `daily_sliding`(정기 일간) \| `loud_pm_event`(요란 PM 이벤트) \| `manual_once`(수동 1회) \| `manual`(수동 promote·롤백). ⚠️ **`ct_decisions.trigger_reason` 컬럼과 값이 다를 수 있다** — DB 컬럼에는 `<trigger>/<status>/<challenger>/<warn>` 형태의 접미가 붙지만(예: `daily_sliding/shadow/lean85_…`), 이 필드는 **접미 없는 트리거 원인만** 싣는다 |
| `retrain_status` | string | **항상** | 종결 상태. 값 목록은 7-2 |
| `model_version_before` | string \| null | **항상** | 교체 **전** champion 폴더명. 최초 학습이면 `null` |
| `model_version_after` | string \| null | **항상** | **지금 서빙 중인 모델**. 배포되지 않은 상태(`SHADOW`·`BLOCKED`·`SKIP`·`FAILED`)에서는 **`null`** — 여기에 challenger 명을 넣으면 "무엇이 배포됐나"를 쿼리로 답할 수 없다(리뷰 S12). 미배포 후보 이름은 아래 `challenger` 로 따로 운반한다 |
| `challenger` | string \| null | 자동 사이클만 | 이번 사이클이 만든 후보 번들명. **배포 여부와 무관하게** 채워지므로 SHADOW·BLOCKED 후보의 추적이 가능하다. 수동 promote·롤백 payload에는 **키 자체가 없다**(후보를 만들지 않는 경로) |
| `window_meta` | object \| null | 자동 사이클만 | 학습창 근거. 하위 스키마는 7-3. 게이트 리포트·`manifest.ct1_window_meta` 와 **같은 dict** — 누수 가드(헌법 1-3)의 창 경계를 외부에서 재검증할 수 있는 유일한 채널 |
| `rmse_before` / `rmse_after` | float \| null | 자동 사이클만 | champion / challenger 의 게이트 평가 RMSE. 게이트가 비교를 생략하면(`NO_CHAMPION`·표본 미달) `null` |
| `champion_challenger_result` | string \| null | 자동 사이클만 | `PROMOTED` \| `SKIP` \| `BLOCKED` \| `NO_CHAMPION`. 게이트 리포트 `metrics` 에서 그대로 승계 |
| `created_at` | string | **항상** | 발행 시각 — **tz-aware UTC ISO8601**(`+00:00`, 초 단위). ⚠️ `window_meta.now`·`cutoff` 는 **naive UTC** 다(§8-B-1 시간축 규율 — pm_log 축과 맞춘 것). 두 축을 같은 비교식에 넣지 말 것 |

> **NaN/Inf 금지**: 발행 길목에서 `json.dumps(..., allow_nan=False)` 로 막는다 — NaN 은 표준 JSON 이 아니라 대시보드 `JSON.parse` 와 DB 를 동시에 깨뜨린다(헌법 7장). `rmse_*` 가 산출 불가면 `NaN` 이 아니라 `null` 이다.

### 7-2. `retrain_status` 값 — 발행되는 것과 안 되는 것

| 값 | 발행 | 의미 |
|---|---|---|
| `AUTO_PROMOTED` | ✅ | 게이트 PASS + `ct1_auto_promote: true` → 자동 배포 완료 |
| `SHADOW` | ✅ | 게이트 PASS 이지만 차단기 강하(`ct1_auto_promote: false`) → 기록만, 미배포 |
| `BLOCKED` | ✅ | 게이트 **FAIL** → challenger 보존·배포 봉인. 재요청 발의 주체는 **사람**(헌법 1-4) |
| `SKIP` | ✅ | 게이트 SKIP(표본 미달 등 — **판정 불가이며 PASS 아님**) → champion 유지 |
| `IFACE_WAIT` | ✅ | 명령 템플릿 미등재 → 게이트 없이 배포 금지 |
| `FAILED` | ✅ | 조립·재학습·게이트 실행 오류 |
| `PROMOTED` / `ROLLED_BACK` | ✅ | **수동** 경로(`manual_promote`). 축소 payload — `challenger`·`window_meta`·`rmse_*`·`champion_challenger_result` **키 없음** |
| `DRYRUN` | ❌ | `ct1_auto_generate: false` 구간. `ct_decisions` 에만 기록 |
| `LOCKED` / `SKIPPED` | ❌ | 중복 실행 배제(g2) · 멱등 skip(g3). 사이클을 돌지 않았으므로 결과 이벤트도 없다 |
| `RUNNING` | ❌ | 선점 중간 상태(`ct_decisions` 전용) |

> 즉 **`ct_decisions` ⊃ `fdc.mlops`** — 토픽만 집계하면 dry-run·락 skip 이 통째로 빠진다. "CT 가 몇 번 돌았나"는 DB 로 답해야 한다.

### 7-3. `window_meta` 하위 스키마

단일 소스는 `lean85_pipeline.slice_train_window` 반환값 + 조립기(`ct1_assemble_trainset`) 보강분이다. 창 = `(now − buffer − window_days, now − buffer]` — 좌측 개구간·우측 폐구간.

| 키 | 타입 | 의미 |
|---|---|---|
| `now` / `cutoff` / `start` / `start_base` | string (**naive** ISO) | 실행 기준 시각 / 공통 학습 상한(`now − buffer`) / 실제 창 시작 / 확장 전 창 시작 |
| `window_days` / `buffer_days` / `effective_window_days` | float | 정본 `ct.ct1_window_days`(365) / `ct.ct1_gate_eval_days` / 확장 반영 실효 창 |
| `require_complete_loud_regime` / `extended` / `extended_days` / `extend_failed` | bool·float | **완결 요란 레짐 조항**(멘토 7/21) — 창 안에 완결 요란 레짐이 1개 미만이면 직전 완결 레짐까지 좌측 임시 확장. 확장은 상태가 아니라 매 실행의 계산 결과이며, 충족되면 자동으로 365 로 복귀 |
| `complete_loud_regimes_in_window` / `regime_used` / `pm_events_n` | int·array\|null | 창 내 완결 요란 레짐 수 / 확장에 쓴 레짐 `[시작, 끝]` / 참조한 물리 PM 이벤트 수 (`pm_log.json` — §8-B-1) |
| `n_wafers` / `n_train_wafers` / `n_eval_wafers` / `n_labelled_wafers` / `n_wafers_total` | int | 창 내 wafer / 학습 / 평가(온셋 제외·라벨 보유) / 라벨 보유 전체 / 원천 전체 |
| `gated` | string \| null | 표본 게이트 미달 사유. `null` = 통과 |
| `trainset` / `evalset` / `assembled_at` / `pm_log` / `sources` / `raw_period` | string·array | 산출물 절대경로 · 조립 시각 · 참조 pm_log · 원천 파일 목록 · 원천 기간 |

> ⚠️ **평가창 `(cutoff, now]` 는 champion·challenger 양쪽 모두 학습한 적이 없어야 한다**(D4) — `cutoff` 를 공통 학습 상한으로 두는 이유이며, 게이트가 `assert_window_no_leakage` 로 독립 재검증한다(헌법 1-3 2중 가드).

### 7-4. 하위 호환

- 본 절은 **문서화 공백의 해소**다. 기존 소비자가 **0곳**이고(위 각주) 발행 코드는 변경되지 않았으므로 파급이 없다 — 필드 이름 변경·삭제도 없다(헌법 2-2).
- `ct_type` 의 `ct1_lgbm` → `ct1_xgb` 는 **문서 현행화**다. 코드(`ct1_orchestrator.CT_TYPE`)·`ct_id` 접미(`-XGB`)는 처음부터 `xgb` 였고, 뒤처진 표기는 본 절·`db/init.sql` 주석·`DB_네이밍_컨벤션_v1.md` 3곳뿐이었다(같은 PR 에서 동시 정리).
- **향후 필드 추가 시**: 소비자가 생기기 전까지는 A 단독 판단으로 추가 가능하되 **본 절을 같은 PR 에서 갱신**한다(헌법 4-3). 소비자가 등재되는 순간부터 헌법 2-2(소비자 리뷰 승인)가 적용된다.

### 7-5. `ModelBiasCapExceeded` — CT⓪ 상한 초과 에스컬레이션 *(2026-08-07 신설 — 설계 D5)*

`fdc.mlops` 의 두 번째 이벤트 타입이다. 7-1 에서 `event_type` 분기를 선등재해 둔 이유가 이것이다.

```json
{
  "event_type": "ModelBiasCapExceeded",
  "ct_id": "CT-20260907-SIMCH3-482913-R2R",
  "chamber_id": "SIM_CH_3",
  "raw_bias": 148.32,
  "bias_applied": 99.84,
  "cap": 99.84,
  "n_samples": 231,
  "note": "상한 초과분은 적용하지 않는다 — Incident 신호 (헌법 1-1 ⓒ)",
  "timestamp": "2026-09-07T04:12:00.000Z"
}
```

- **발행 시점**: rolling bias 가 clamp 상한(`|bias| ≤ ct.model_r2r.bias_max_rmse_ratio × train RMSE`)을 넘은 **상태 진입 시 1회 + 지속되면 일 1회**. 해제되면 로그만 남기고 이벤트는 내지 않는다.
- **`bias_applied` ≠ `raw_bias`**: 서빙은 **clamp 값으로 계속**되고 초과분만 버려진다. 헌법 1-1 ⓒ("상한 초과 격차는 적용하지 말고 Incident 신호로 전환")의 구현이며, 감사 정본은 `ct_decisions(retrain_status='CAP_EXCEEDED')` 다.
- ⚠️ **`fdc.alert` 로 직접 발행하지 않는다 (헌법 1-2)**. 이상 경보 채널의 발행자는 B 단일이다. 알림 합류가 필요하면 **B 가 `fdc.mlops` 를 구독해 자기 룰로 alert 를 여는** 안을 협의한다 (설계 D5 — WP-C 정합, **미합의**). 그 전까지 본 이벤트의 실효 소비자는 **0곳**이며 발행 실패는 흐름을 막지 않는다(감사는 DB 에 이미 남았다).
- `ct_id` 접미는 `-R2R` (헌법 6-4 — CT①의 `-XGB`·CT②의 `-AE` 와 대칭). SEQ 는 순번이 아니라 **트리거 라벨 wafer_id 의 결정론 다이제스트**다 — 같은 메시지 재처리가 같은 ID 를 만들어야 `ct_decisions` 의 UNIQUE 가 중복 전달을 무해화한다(설계 §7).
- `ct_type` 은 payload 에 싣지 않는다 — 이 이벤트는 사이클 종결(`CtDecision`)이 아니라 **상태 경보**다. CT⓪ 의 사이클 기록은 전부 `ct_decisions(ct_type='ct0_bias')` 로만 남는다.
- **CT⓪ 의 `retrain_status` 어휘** (7-2 는 CT① 값 목록이다): `APPLIED`(서빙 bias 변경) · `SHADOW`(계산·미적용) · `RESET`(요란 판정 PM → bias=0 + 이전 라벨 차단) · `CAP_EXCEEDED`(상한 초과 — clamp 값으로 서빙 유지) · `DISABLED`(표본 미달·clamp 분모 결손으로 보정 해제). 기록 조건은 **서빙값 변경 또는 상태 전이** — 값이 같아도 `APPLIED → CAP_EXCEEDED` 같은 전이는 행을 남긴다(그래야 위 `ct_id` 가 실재한다).

## 8. 🧪 Qual 판정 데이터 (A 취합 → S7 화면)

```json
{
  "qual_id": "QUAL-20260713-SIMCH3-001",
  "chamber_id": "SIM_CH_3",
  "wafers": ["Q1","Q2","Q3","Q4","Q5"],
  "metrics": {
    "ae_anomaly_mean": 0.14,
    "tttm_gap_pct": 0.7,
    "nelson_violations": 0,
    "c65_within_normal": true
  },
  "thresholds": { "ae": 0.2, "tttm_pct": 1.0, "nelson": 0, "c65": "P95" },
  "proposed_verdict": "PASS",
  "approval_status": "PENDING"
}
```

승인 → CT②(AE 재학습) + 실력치 재산정 + Qual 스냅샷 저장(`qual_snapshots` — TTTM 절대 비교 참조) → `ChamberRequalified`.

### 8-B. QualVerdictConfirmed 이벤트 — 레짐 스위치의 단일 소스 *(v4.6 추가 2026-07-10 — 소비자 A·B 리뷰 대기, P4-6 일괄)*

S7 판정(조용/요란)이 엔지니어 승인으로 확정되는 순간, 승인 워크플로가 **`fdc.agent`에 발행**:

```json
{
  "event_type": "QualVerdictConfirmed",
  "qual_id": "QUAL-20260713-SIMCH3-001",
  "chamber_id": "SIM_CH_3",
  "verdict": "loud",
  "incident_id": "INC-20260713-SIMCH3-001",
  "pm_count": 25,
  "confirmed_at": "2026-07-13T11:00:00.000Z",
  "approved_by": "engineer_01"
}
```

- `verdict`: `"loud"`(요란) / `"quiet"`(조용). `incident_id`: 요란 시 레짐 전환 Incident(신규 결정 10), 조용이면 null
- **소비 3곳**: **A** = `is_post_loud_pm` 갱신(gain 1·2위 피처 합 77% — BL5)·Model R2R **bias=0 리셋·이전 라벨 차단**(신규 결정 5) / **B** = 가한계 Phase 0→1 진입·TTTM 유예·median 제외(신규 결정 3·8) / **Dashboard** = S7 상태
- 필터 관례: `fdc.agent`의 타 메시지(Brief)는 `event_type`으로 skip — `fdc.correction`의 type 필터와 동일 패턴
- **부트스트랩·재시작 복구 = `quals` 테이블 조회** (이벤트 유실 대비 멱등 — 헌법 6-2). 신규 토픽·기존 필드 변경 없음(추가만 — 하위 호환)
  - ~~⚠ **갭 인정 (2026-07-22, B 지적)**: 현 `quals`는 판정 후보(PASS/FAIL)만 보유 — **확정 verdict의 영속 소스가 없음**.~~ → **갭 해소 확인 (2026-08-07)**: P6-1은 **컬럼**(`confirmed_verdict`·`confirmed_at` — `db/init.sql` 12b, 7/28)·**쓰기**(B `qual_recorder.update_qual_confirmed` — loud/quiet 둘 다 확정, `approval_status='APPROVED'`·`approved_by`·`decided_at` 동반)·**테스트**까지 이미 구현돼 있었다. 빠져 있던 것은 마이그레이션 파일뿐이며(헌법 3-2 마이그레이션 동반 의무 **신설 이전** 작업분 — 같은 계열의 `0007`과 동일 사유) `db/migrations/0010`으로 보강했다. **재시작 복구 원칙이 이제 문자 그대로 성립한다.**
  - **CT⓪ 소비 (2026-08-07 등재)**: `ct0_bias.updater`가 재기동·리밸런스 때 이 테이블을 **요란 PM 경계(`valid_from_pm_count`)의 권위 소스**로 읽는다 — `confirmed_verdict='loud'` 최신 행. ⚠ 경계 **정수**는 `qual_id`의 SEQ에서 뽑는다 (`quals`에 `pm_count` 컬럼이 없고, B가 `QUAL-<YYYYMMDD>-<CHAMBER>-<pm_count>`로 채번하기 때문 — `spc_consumer._judge_qual` 결정7). **이 채번 규칙이 바뀌면 CT⓪이 경계를 잃으므로** 변경 시 A 리뷰 필요(헌법 2-2 정신 — Kafka 필드는 아니지만 실질 인터페이스다). 항구 해소는 `quals.pm_count` 컬럼 신설.

#### 8-B-1. B 소비 효과 — `pm_log.json` 기입 *(B6-3-f 신설 2026-07-28 — 소비자 A 리뷰 필요, 헌법 2-2)*

`verdict="loud"` 수리(Phase 0→1 전이)와 **동시에**, B는 `pm_log.json`에 요란 PM 1건을 append 한다. Kafka 토픽은 아니나 **A(lean-85)가 읽는 인터페이스**이므로 계약으로 고정한다.

- **작성자 = B(`spc_consumer`) 1곳. A는 읽기 전용.** A도 본 이벤트를 구독하지만(§0) **pm_log를 쓰지 않는다** — 두 주체가 쓰면 이중 기입이 된다.
  - 근거: 본 이벤트에는 `confirmed_at`(승인 시각)만 있고 **PM 개방 시각이 없다.** 개방 시각(= `pm_count`↑를 감지한 wafer의 `timestamp`)은 B 컨슈머만 관측하며, `days_since_last_pm`의 기준점은 승인 시각이 아니라 **물리 PM 시점**이 정본이다.
- **정본 경로 = 리포 루트 `pm_log.json`** (`config/params.yaml` → `pm_log.path`). `lean85/pm_log.json` 사본이 생기면 A `find_file`이 루트보다 먼저 집어 **split-brain** → B는 기동 시 경고한다.
- **엔트리 스키마** — A 파서(`_parse_pm_entries`)는 `date`·`type`만 읽고 여분 키는 무시하므로 추적 필드 추가는 하위호환:

```json
{"date": "2026-07-28 05:12:33", "type": "major", "verdict": "loud",
 "chamber_id": "SIM_CH_4", "pm_count": 2,
 "written_by": "spc_consumer", "written_at": "2026-07-28 05:20:01"}
```

- **`date` = naive UTC `"%Y-%m-%d %H:%M:%S"` — tz 접미사 금지.** A 트레이스 축(`rows_to_trace`가 수신 timestamp를 naive로 변환)과 같은 축이어야 하고, tz-aware가 섞이면 파서의 `sorted`에서 aware/naive 비교 **TypeError → A 추론 전체 다운**.
  - 값 우선순위 = PM 개방 시각 → `confirmed_at` → 기입 시각. **현 배선은 개방 시각만 전달**하므로(코어에 이벤트 메타 미주입 — 결합 최소화) 실효 fallback은 **기입 시각**이다. 어느 쪽을 썼는지는 엔트리 `date_source`(`pm_detected_at`/`confirmed_at`/`written_at_fallback`)에 남는다 — A가 fallback 엔트리를 식별할 수 있다.
  - ⚠ **축 정합의 조건**: A `_parse_ts`는 tz를 UTC 변환 없이 버리고 B는 UTC 변환 후 버린다. 발행측(`fdc.raw.timestamp`)이 **UTC(+00:00)인 현재만 일치**한다 — 로컬 offset 발행으로 바뀌면 두 축이 어긋난다. 또한 A가 `LEAN85_TIME_SOURCE=src_ts`(원본 2018 축)로 뜨면 B가 기록하는 wall-clock PM은 미래가 되어 `searchsorted`에서 탈락한다(피처 미갱신). 두 조건 모두 **A 후속 정리 대상**.
- **`type`은 `"major"` 고정**(요란↔대PM 기존 규약), 판정 의미는 `verdict`가 운반. **요란만 기입**한다 — 조용(quiet)은 레짐 전환이 아니므로 기입하지 않는다(`parse_pm_log` docstring 규약). writer도 loud가 아니면 거부한다.
- **멱등 키 = `(chamber_id, pm_count, date)`** — 재전달 시 skip. `date`를 포함하는 이유: 시뮬레이터 `pm_count`는 프로세스마다 0에서 재시작하는데 pm_log는 실행 간 누적되므로, `(chamber, pm_count)`만으로는 **2회차 실행의 진짜 새 요란 PM이 영구 미기입**된다. 방어 대상인 재전달은 같은 개방 시각을 실어오므로 방어력은 유지.
- 쓰기는 tmp+`os.replace` 원자 교체(A가 mtime+size 캐시로 읽는 중 부분 쓰기 노출 금지). 손상 파일은 **덮어쓰지 않는다**(이력 보존).
- **유실 방지**: 실패한 엔트리는 경로를 가리지 않고(원자 교체 실패·손상/읽기 실패 **모두**) 보관되며, B 컨슈머 poll 루프가 **상시 재시도**한다 — 손상 파일을 수동 복구하면 보관분이 그때 살아난다. 코어는 phase 가드로 재호출되지 않고 다음 요란 PM은 대PM 주기(3~4개월) 뒤라, 보관·상시 재시도가 없으면 그 PM은 A에 **영구 미반영**된다.
  - 백그라운드 재시도는 **단발·무대기 + 쿨다운(30s)**, 새 PM 기입은 **전력 재시도**(5회·≈1s)로 예산을 분리한다. 지속 잠금 시 재시도 비용이 `fdc.raw` 소비 지연으로 전이되지 않게 하기 위함.
- **작성자 = B 1곳 + 프로세스 1개.** `os.replace`는 독자 원자성만 보장하고 load→append→write에 락이 없어, `spc_consumer`가 동시에 2개 뜨면 **lost update**가 난다. 스케일아웃 시에는 기입 담당 인스턴스를 1개로 고정할 것.
- **파일 보존**: `pm_log.json`은 git 추적 파일이라 런타임 append가 워킹트리를 더럽히고 브랜치 전환·`git checkout`으로 운영 이력이 날아갈 수 있다. 데모·리허설 전후 백업 권장(장기 운영 시 DB 이관 검토 — 후속).
- **소비 효과(A)**: 기입 즉시 `parse_pm_log` 캐시가 무효화되어 **다음 wafer부터** `days_since_last_pm`·`high_regime_days`·`dslp_x_hour`에 반영되고, `should_retrain`이 신규 date를 **이벤트 재학습 트리거**로 인식한다(lean85 README §4).
  - 단서 ①: `is_high_regime`은 one-way 플래그인데 루트 pm_log에 이미 2018 엔트리가 있어 **현 wafer는 이미 1** — 신규 기입으로 바뀌는 값은 나머지 3종이다. ② `should_retrain`은 `str(date())` **날짜 단위** 비교라, 압축시간 데모에서 같은 날 2번째 이후 요란 PM은 트리거되지 않는다.
- **알려진 한계 — 다챔버 시맨틱 불일치** *(A 조건부 승인 조건 ①, 2026-07-28 명문화)*: `_meta_features`는 현재 **챔버 무관 전역 PM**으로 계산한다 → **한 챔버의 요란 PM이 전 챔버 wafer에 반영된다**(기존 모델링 단순화). `chamber_id`는 기입하되 **챔버별 필터는 A 후속 과제 — 상세·전제·재검증 조건은 `docs/adr/ADR-0001-chamber-scoped-pm-log.md`**. 구현 = `src/agent_b_spc/pm_log_writer.py`. 파급 2종:
  - **피처 오차**: `is_high_regime`·`days_since_last_pm`은 gain 1·2위(합 77%) 피처다. CH4 요란이 CH1~3 wafer의 레짐 피처까지 뒤집으면 **생산의 3/4에 계통 오차**가 들어간다.
  - **재학습 파급**: `should_retrain`은 신규 PM 날짜를 이벤트 트리거로 쓰므로(`lean85_pipeline` :445-448 — pm_log에 챔버 축이 없다) **한 챔버의 PM이 전 챔버 모델 재학습을 유발**한다. 현 캐던스가 일간 슬라이딩(`retrain_days: 1`)이라 정기 트리거와 겹쳐 실효 피해는 작지만, 캐던스가 길어지면 드러난다.
  - 단기 유지 근거(A): 현 동결 모델은 **PM이 전역이던 데이터로 학습**됐다 → 서빙만 챔버별로 바꾸면 train/serve skew. 학습·서빙 **동시 전환 + 동결 벤치(시간축 pooled RMSE 99.84 ± 0.5) 재검증**이 전제라 본 PR 범위 밖.

### 8-C. ChamberRequalified 이벤트 — 재인증 완료 경계 *(2026-07-22 신설 — B6-3-c 요청·PM 확정. §8-A 말미 "→ ChamberRequalified"의 스키마 구체화)*

**발행 시점 (권위 정의)**: R9(신규 기준선 수립) 승인 resume에서 **firm corrections(`fdc.correction`, 그룹별 여러 건)의 발행이 모두 완료된 직후, 마지막에 1회** — regime_transition Incident 종결(신규 결정 10: 종결 = Phase 2 + R9 완료)과 동시. 토픽 = **`fdc.agent`**.

```json
{
  "event_type": "ChamberRequalified",
  "incident_id": "INC-20260713-SIMCH3-001",
  "chamber_id": "SIM_CH_3",
  "qual_id": "QUAL-20260713-SIMCH3-001",
  "pm_count": 25,
  "corrections_applied": ["LIM-20260713-SIMCH3-002", "LIM-20260713-SIMCH3-003"],
  "limit_version": "v4",
  "requalified_at": "2026-07-13T18:30:00.000Z",
  "approved_by": "engineer_01"
}
```

- **멱등 키 = `incident_id`** — Incident당 정확히 1회 발행, 소비자는 재수신 시 skip
- `requalified_at` = **verify 창(firm 적용 후 30장 — B6)의 카운트 시작 경계이자 Y-임계 1회 write의 권위 신호**
- `corrections_applied` = 함께 적용된 firm correction ID 목록 (B 채번 승계값 — 수신 측 크로스체크용)
- 소비: **B**(verify 경계·`y_thresholds` write) · **Dashboard**(S7 복귀 표시·F8 진행바 종결) · **A — CT² 오케스트레이터**(자동 생성 트리거 — §8-E, 2026-07-29) · *(후속·선택)* 시뮬레이터(QUAL→RUN 게이팅 — 시뮬 스펙 §93. 현 시뮬은 대기 없이 재개하므로 데모 영향 없음)
- **발행 구현 = P6-1 (7/28, orchestrator — PM)**. 그 전 소비자 배선 테스트는 콘솔 프로듀서 수동 발행으로 (개발 가이드 1 참조)

### 8-D. R9(신규 기준선 수립) 승인 페이로드 — 정본 규약 *(P6-1 신설 2026-07-28 — 구 "잠정안" 확정. 이 절이 명문 정본)*

**역할 경계 (확정)**: **조립 = orchestrator(PM)** · **데이터 정본 = B `limit_corrections` PROPOSED 패키지** · **C 무관** (R9는 Qual→Phase 2 재수립 경로 — RAG 근거 불요, 수치는 전부 B 엔진 산출. 헌법 1-2의 alert 채널·6-4 채번 승계 원칙과 정합).

- **정본 데이터**: 대상 챔버의 `limit_corrections` `status='PROPOSED'` 행 전체(그룹별 여러 건 — B `record_proposed` 적재분). orchestrator는 **조회·수합·표시만** 하며 수치 재계산 금지 ("정량은 B"). `correction_id`는 B 채번 승계 (6-4 — 재채번 금지).
- **승인 단위**: 챔버 재수립 1회 = 승인 요청 1건 (그룹별 개별 승인 아님). `thread_id` = 해당 regime_transition `incident_id` — **순차 승인 규약**(헌법 1-4 해석 명문화 2026-07-08: 동시 pending 1건, 복귀 승인→R9→CT② 순차).
- **페이로드 형태** (S7 카드 렌더 소스):

```json
{
  "request_type": "requalify",
  "incident_id": "INC-20260713-SIMCH3-001",
  "chamber_id": "SIM_CH_3",
  "qual_id": "QUAL-20260713-SIMCH3-001",
  "corrections": [
    { "correction_id": "LIM-20260713-SIMCH3-002", "sensor_id": "C11", "step": 4,
      "center_before": 115.2, "center_after": 118.9, "ucl_after": 125.1, "lcl_after": 112.7,
      "calc_window_n": 812, "settle_converged": true }
  ],
  "settle_converged": true,
  "approval_status": "PENDING"
}
```

- **`settle_converged` 경고 규약** (#53 연동): 패키지 대표값 = 행들의 AND. **`false`(상한 폴백 — 미수렴)면 승인 화면(S7)에 경고 배지 필수** — "정착 미수렴 상태의 재수립 제안, 승인 검토 주의" (B 로그와 동일 문구 계열). `null`=비-재수립 경로(해당 없음).
- **승인 후**: resume이 firm corrections를 `fdc.correction`으로 그룹별 발행 → 전부 완료 후 **`ChamberRequalified` 1회**(§8-C) → B가 firm apply + verify 경계 + Y-임계 write. 거부 시 PROPOSED 유지(재검토) 또는 폐기 = 엔지니어 선택 (reject 규약 준용).

### 8-D-1. RTD 해제 근거 Brief — R9 승인 화면의 회고 근거 *(2026-08-08 신설 — 인프라 스텝 3 · #140. 생산자 C)*

**쉬운 말 요약** — 챔버가 자동으로 멈추면(헌법 1-1 예외 4) 엔지니어는 "다시 돌려도 되나"를 정해야 한다. 그 판단 재료(왜 섰나 / 얼마나 심했나 / 과거엔 뭘로 풀렸나)를 Agent 가 한 장으로 만들어 DB 에 남기는 규약이다. **해제는 하지 않는다** — 해제 경로는 §8-D 의 R9 승인 하류 하나뿐이다.

- **트리거**: `GET /chambers/status` → `items[].inhibited == true` (§9 — 열린 inhibit 의 단일 소스. #118 C 리뷰 ② ⓑ 채택분). C 는 그 판정을 재구현하지 않고 **호출만** 한다.
- **단위**: **Incident 1건 = Brief 1건**(헌법 1-4). 장비 스코프 정지는 형제 챔버에 행이 여러 개 생기지만 전부 같은 `incident_id` 이고 해제도 일괄이므로(`chamber_inhibit.release`), 챔버별로 만들지 않는다.
- **적재 정본**: DB `release_briefs` (마이그레이션 `0014_release_briefs.sql`). ⚠️ **`agent_reports` 가 아니다** — `/incidents/recent` 의 LATERAL 이 그 테이블 **최신 1행**을 `SUP-%` 필터 없이 읽어 S3 큐의 title/verdict 로 렌더하므로(`src/gateway/main.py:1115`), 다른 종류의 행을 섞으면 그 화면이 조용히 오염된다.
- **소비**: S7(R9 승인 화면). `incident_id` 로 §8-D 패키지와 짝지어 읽는다.
  - **PM 결정 (2026-08-09, PR #154 리뷰)**: **① API 얹기까지만 · S7 렌더는 시연 후.** 근거 — RTD 자동 정지가 `auto_inhibit=false`(임계 미확정)라 **시연 라이브에 정지 장면이 없고**, 리허설 골격의 라이브 필수 목록에도 없다. *"curl 로 보이는 상태면 리허설 확인에는 충분합니다."*
  - 그에 따라 `GET /requalify/{incident_id}/package` 에 `release_brief` 를 합류시킨다 (`incident_id` 1:1 · 없으면 `null`). 화면 렌더는 시연 후 별건.
- **행 형태** (스키마 정본 = `src/agent_service/app/schemas/release.py` · 프롬프트 정본 = `docs/Agent_프롬프트_라이브러리_v1.md` §7):

```json
{
  "report_id": "RLS-20260808-SIMCH1-0042",
  "incident_id": "INC-20260808-SIMCH1-003",
  "chamber_id": "SIM_CH_1",
  "scope": "chamber",
  "trigger_alert_id": "ALERT-20260808-SIMCH1-0042",
  "inhibited_at": "2026-08-08T04:12:07Z",
  "readiness": "conditional",
  "summary": "재인증 진행 가능 — 단 C17 히터 점검이 선행돼야 한다.",
  "evidence": ["...", "...", "..."],
  "counter_evidence": "...",
  "confidence": 0.72,
  "precheck_items": ["..."],
  "retrospect": {
    "trigger_violations": [{"sensor_id": "C17", "rule_id": "N1", "severity": "CRITICAL"}],
    "persistent_sensors": [{"sensor_id": "C17", "n1_alerts": 41, "breach_ratio": 0.441}],
    "window_minutes": 60, "window_total_alerts": 93,
    "past_releases": [{"incident_id": "INC-...", "release_qual_id": "QUAL-...", "released_by": "..."}]
  },
  "release_authority": "requal_approval_only"
}
```

- **`report_id` 접두 = `RLS`** — **헌법 6-4 접두 신설분**(2026-08-09, #140 PR 동반 · **PM 승인 완료** — PR #154 리뷰: *"승인합니다. 초안의 QUAL 재사용을 스스로 기각한 판단이 맞습니다"*). 초안은 `QUAL` 재사용이었으나 폐기했다: 6-4 의 접두 공유 규칙은 *"같은 대상을 두 각도에서"*(`LIM` = 리포트 ↔ correction, **같은 제안 1건**)일 때만 성립하는데, 이 Brief 와 `quals.qual_id` 는 **대상이 다르다**(Brief 는 Qual 이 생기기 전에 만들어지고 Qual 로 이어지지 않을 수도 있다). 실제로 **바로 이 절의 예시 안에서** `report_id`·`release_qual_id` 가, S7 화면에서는 거기에 §8-D 의 `qual_id` 까지 더해 **QUAL- 이 셋·두 뜻으로** 떴다. 접두 신설은 `supervisor._ID_RE`(인용 가드)·접두 커버리지 테스트 동반이 조건이다(6-4).
- **`release_authority` 는 `"requal_approval_only"` 고정**(타입 상수). §6 Brief 의 `approval_status: "PENDING"` 과 같은 장치다 — C 가 해제 권한을 표현할 수 있으면 무승인 통과 경로가 열린다(헌법 1-1 예외 4 ⓒ).
- **`readiness`**: `ready` / `conditional` / `not_ready` — **권고**다. 화면은 이 값으로 승인 버튼을 열거나 잠그지 않는다.
- ⚠️ **「정지 이후 좋아졌나」 축은 없다.** 정지 중에는 그 챔버 wafer 가 0장이라(`src/simulator/kafka_producer.py` inhibit 스킵) `spc_violations` 도 더 쌓이지 않는다. 사후 개선 지표를 요구하는 화면·채점을 만들지 말 것 — 구조적으로 채울 수 없다.

### 8-E. CT²(AE) 배포 — 모델 게이트 규약 *(2026-07-29 신설 / **2026-07-30 배포 자동화 개정** — 헌법 3-3 ③·1-1 예외 3. 설계 정본 = `docs/CT2_배선_설계_v2.md`)*

- **분류**: Incident 알람 승인이 아니라 **모델 게이트** — 종결(R9 완료)된 Incident의 후속. 경로가 자동이든 수동이든 `approval_records`에 `request_type='ct2_deploy'` 적재 (incident_id 참조 = 감사 연결, 헌법 1-1).
- **문지기 = 자동 검증 게이트** *(7/30 개정)*: `ae_pipeline/validate_bundle.py` 전 항목 PASS가 배포의 선행 조건이다. 항목·임계의 단일 소스 = 해당 모듈 + `params.yaml` `ct.ct2_gate_*` (계약 문서는 수치를 규정하지 않는다). FAIL = challenger 보존·미배포 + `ct_decisions(BLOCKED)` + 에스컬레이션, **재요청 발의는 사람**.
- **PASS 시 2모드** — `ct.ct2_auto_promote`:
  - `true` (**자동 promote**) — 승인 화면 미경유. `approval_records`에 `status='APPROVED'`·`approver='ct2_auto_gate'`·`approver_role='system'`·`decision_reason`에 **게이트 리포트 경로**를 담은 1행 + `ct_decisions('AUTO_PROMOTED')` + `models/CHANGELOG.md`. 인간 개입의 근거는 상류 R9(`request_type='requalify'`) 승인이다 (헌법 1-1 예외 3).
  - `false` (**승인 회귀** — 현재) — 파생 thread 배포 승인을 오픈한다 (아래 thread·resume 규약).
- **thread**: LangGraph `thread_id = <incident_id>-CT2` (파생 — 본편 thread 무접촉·재invoke 불요, C 확인 2026-07-29). 그래프 = `src/orchestrator/ct2_deploy_approval.build_ct2_graph` (gate→promote/reject **2지선다, modify 불허**). 자동 promote 경로는 그래프를 타지 않는다(interrupt 없음).
- **resume**: 기존 `POST /approvals/{incident_id}` 재사용 — gateway가 PENDING row의 `request_type`을 보고 ct2 그래프+파생 thread로 라우팅. `incidents.lifecycle`은 **무접촉** (종결 상태기계 보호 — P6-1).
- **promote 효과**: `control/ct2/promote_<incident_id>.json` 신호 드롭(`{incident_id, bundle(절대경로), ct_id, promoted_at}` · 원자 교체 `.tmp`→`os.replace`) → A consumer가 스캔 주기마다 감지해 **관리형 리로드** (AE 번들 재적재 = DriftTracker 리셋). 자동·수동 경로가 **같은 신호 채널**을 쓴다 (consumer 무변경). consumer는 처리 완료 신호를 `processed/`·`failed/`로 이동.
- **`retrain_status` 값 (`ct_decisions`, ≤16자)**: `DRYRUN`/`SKIP`/`GATED`/`IFACE_WAIT`/`FAILED`/`BLOCKED`/`RUNNING`/`SHADOW`/`AUTO_PROMOTED`/`PROMOTED`/`REJECTED`/`ROLLED_BACK` — 전부 VARCHAR(16) 이내 (최장 `AUTO_PROMOTED` 13자).
- **게이트 리포트 규약**: `<bundle>/validation.json` — 필수 키 `gate_source`(발급자 접두 `ae_pipeline/validate_bundle.py`) · `gate_pass` · `failed_checks` · `bundle_name` · `checks[]` · `metrics{}`. **조립 `meta.json`의 `gate_pass`(표본 수 게이트)와 혼동 금지** — 발급자·번들명 확인이 그 방어다.
  - **schema v3 추가분** *(2026-08-09 — 기준 개정 v2 §7. `gate_schema_version: 3`)*: 필수 키에 **`approval_brief{}`** 추가. 비율 판정 항목(B1·B5)의 `checks[]` 원소는 **`verdict_band`**(`PASS-확정`/`PASS-경계`/`FAIL-경계`/`FAIL-확정`)와 `band{k,n,rate,ci_lo,ci_hi,budget,boundary,pass}`를 함께 싣는다. **밴드는 판정을 바꾸지 않는다** — `pass` 는 종전대로 점추정이고, 밴드는 표본 부족으로 "확정이 아님"을 승인자에게 노출하는 라벨이다 (하위호환: 구 소비자는 `gate_pass`·`failed_checks`만 봐도 거동 무변경).
  - **`approval_brief` 소비 규약**: 승인 브리프에 뜨는 **약화 조건·경계 라벨·잔여 리스크의 한국어 문구는 이 블록이 정본**이다 (`weakened[]{key,label,why,baseline,value,guard}` · `boundaries[]{check,band,ci,budget,rate}` · `residual_risks[]`). gateway(`attach_ct2_brief`)는 `ct2_*` 키로 **평탄화만** 하고, 프론트(S4)는 그대로 렌더한다 — **하류가 키 이름을 번역하면 임계를 바꿀 때 두 곳을 고쳐야 하고, 안 고쳐도 아무 일도 일어나지 않는다**(헌법 7장). 브리프 부재(구 리포트)는 `ct2_brief_missing`으로 드러내며 **빈 화면 = 약화 0건으로 읽히지 않게** 한다.
  - **`gate_params` 키 개명** *(2026-08-09)*: `l5_max` → **`l5_budget`**(오경보 **예산**). 구 키는 읽기 호환 미러로 남고, 구 `params.yaml`의 `ct2_gate_l5_max`는 승계된다(경고 1줄). 신규 코드는 `l5_budget`을 쓴다.
- **exit code**: `0`=PASS · `2`=FAIL(봉인) · `1`=실행 오류(판정 불가 — PASS로 오해 금지).
- **동의 (v2)**: R9 승인 실존 = **생성 + 자동 배포** 동의 승계. R9 Brief는 게이트 항목을 열거해야 한다(백지 승인 금지 — 설계 v2 D2). R9 페이로드(§8-D)에 `ct2_consent` 명시 필드 추가는 후속 (2-2 절차 — S7 표시 포함).
- **배포 후 감시·자동 롤백 (WP-C, 2026-07-30)**: 배포된 번들은 `src/agent_a_mlops/ct2_deploy_monitor.py`가 감시한다 (헌법 3-3 ③ ⓔ). 발화(SPC↑/AE 침묵 교차 · ae_score 분포 붕괴) 시 **직전 정상 번들로 promote 신호 재드롭** — `control/ct2/promote_<incident_id>-rollback.json`(`promote_*.json` glob 매칭, `bundle`=복귀 대상 절대경로, `rollback:true`). consumer는 전방 배포와 동일하게 관리형 리로드 + DriftTracker 리셋. 감사: `ct_decisions('ROLLED_BACK', model_version_before=복귀 대상)` + `approval_records(request_type='ct2_rollback', status='APPROVED', approver='ct2_deploy_monitor')` + `CHANGELOG`. 롤백 대상이 없으면 신호를 드롭하지 않고 에스컬레이션만(서빙 보호). 감시 임계 = `ct.ct2_watch_*`(단일 소스 = `ct2_deploy_monitor.WATCH_DEFAULTS`). **개통 조건**: `ct2_watch_enabled`·`ct2_watch_auto_rollback` 둘 다 실증(시뮬 Scorecard 프로브 미탐 0건) 전 false.
  - **신규 `request_type='ct2_rollback'`** (VARCHAR(32)) — 자동 롤백 감사 전용 행. `ct2_deploy`의 pending-1 로직과 무간섭(다른 타입). 소비자 = 대시보드(이력 표시)·감사.
  - **감시 데이터 소스 규약** *(2026-07-30 H3 정합 — `db/init.sql` 정본 대조)*: 감시는 **`wafer_predictions` 단일 테이블**을 읽는다. 점수 = **`anomaly_score`**(계약 §2 `fdc.prediction` 필드명 — consumer가 AE의 `ae_score`를 이 이름으로 발행·적재. `ae_score` 컬럼은 **없다**), SPC 플래그 = **같은 행 `spc_flags`(JSONB)**. `spc_violations`에는 `wafer_id`가 없어(alert 단위) wafer 조인은 성립하지 않는다 — 조인 금지.
    - **더미 폴백 배제**: AE 미가동·번들 로드 실패 구간의 `anomaly_score`는 난수 더미다. 감시는 기본적으로 `model_version='dummy'` 행을 제외하고 `--since`(배포 시각)로 창을 제한하며, 잔여 혼입은 `dummy_suspect_rate`로 리포트한다. **행 단위 AE 더미 식별에는 스키마 확장이 필요**(AE 버전/실값 여부 컬럼 — 2-2 + 헌법 3-2 PM 승인, 후속).
    - ✅ **감시 ①(교차 침묵) 배선 완료 (2026-08-05 M1 — 구 "A3-2 미구현" 각주 해제)**: Nelson 판정은 **B 소유**이고 이미 `fdc.alert.prediction_context.spc_flags`(`[{sensor, rule}]`)로 발행 중이다. A 는 **운반·적재만** 한다 — `prediction_sink` 가 `fdc.alert` 를 추가 구독해 `wafer_predictions.spc_flags` **한 컬럼만** UPDATE 한다(wafer 키 = `prediction_context.wafer_id`. alert 최상위에는 `wafer_id` 가 없다). A 가 Nelson 을 자체 계산하는 것은 금지다(헌법 1-2 alert 채널 · 3-3 SHAP 독립 채널).
      - 구 상태의 원인은 **두 겹**이었다: ⓐ `consumer.build_message` 가 `spc_flags: []` 를 실었고 ⓑ sink 의 `COALESCE(%s, spc_flags)` 에서 `[]` 가 **NULL 이 아닌 값**이라 통과했다. 그래서 다른 writer 가 채워도 다음 예측이 지웠다. ⓐ는 **키 제거**로, ⓑ는 빈 배열을 `NULL` 로 낮추는 가드로 막는다. `fdc.prediction` 은 이제 `spc_flags` 를 **싣지 않는다** (필드 삭제 아님 — A 가 채운 적이 없던 필드의 발행 중단. 소비자 B·대시보드는 이 필드를 읽지 않는다).
      - ⚠️ 개통 조건의 "감시 2종 **실증**"은 배선이 아니라 실측이다 — 실브로커·DB 환경에서 창 내 `spc_flags` 발화 > 0 을 확인해야 완결된다.


---

## 9. 🌐 게이트웨이 REST API (P6-1 신설 2026-07-28 — 전 엔드포인트 등재. 코드 정본 `src/gateway/main.py`·`predictions_router.py`)

> #54 리뷰 후속 합의: REST가 계약 문서에 0건이던 것을 본 절로 해소. **이후 라우트 추가·변경 시 본 표 동시 갱신** (헌법 4-3). 모든 응답 JSON. 프론트는 `/api` 프록시 경유(vite — `/api` 접두 제거).
>
> **2026-08-02 갱신 (#80 리뷰 ③)**: 아래 신규 7종 등재 (S3·S4·S11 배선 + P6-4 원클릭 스택). API 외 동작 — startup **승인 자동시작 폴러**: SUP Brief 존재 + `approval_records` 0행인 Incident 주기 스캔 → `start_incident` invoke (`params incident.approval_autostart_poll_sec` 미설정 시 비활성 = CLI 수동 대역). ⚠️ **08-02 리허설 실측 계약 공백 2건**: ① ~~승인 발행 payload 정량 필드 null~~ → ✅ **해소 (2026-08-03, C)**: `agent_reports.limit_option/recipe_option` jsonb 에 요약뿐 아니라 **정량 필드를 병합 적재**한다. 원인은 상세 리포트가 `save_brief` 까지 **전달되지 않던 것**이었고(요약 `OptionSummary` 만 적재됨), PM 매퍼 키 정합은 **불필요**했다 — `supervisor_adapter._pick` 이 C 정본 이름(`sensor_id`·`center_after`·`limit_version`)을 이미 흡수하고 있다(2026-07-22 반영분). **잔여 1건**: `recipe_correction_id` 는 **B 채번 승계값**이라 B5-4 연동(#83) 전까지 계속 null 이다 — `recipe_corrections.recipe_correction_id` 가 NOT NULL UNIQUE 이므로 Recipe R2R 실적용 단계에서 B 쪽 PK 매칭이 필요하다. ② ~~reanalyze 는 표시 전용 — 소비자(C 재생성) 미구현~~ → ✅ **해소 (2026-08-03, C #97)**: 재분석 폴러가 소비한다(`reanalysis.py` · `agent.reanalysis_poll_sec` 15s · PM-4). ⚠️ **잔여 단서 (#97 리뷰 §1)**: 실패 경로(payload 부재·파싱 실패·brief None)가 `needs_reanalysis` 를 내리지 않아 **LLM 정지 상태에선 영구 재폴링** — 결정적/일시적 실패 분리는 C 후속. *(각주 현행화 2026-08-04 PM)*

| 메서드·경로 | 역할 | 비고 |
|---|---|---|
| GET `/health` | 게이트웨이 생존 확인 | 배선 실패에도 유지 |
| GET `/events` | SSE 푸시 (S0 상단바) | text/event-stream |
| GET `/approvals/pending` | 승인 대기 목록 (S3) | `age_sec`·`is_aged`(P6-1 에이징 — 표시만)·`incident_type` 동봉 |
| POST `/approvals/{incident_id}` | 결정 resume — approve/modify/reject/escalate | 계약 §6 4액션. 재개 불가 시 409 |
| POST `/corrections/{correction_id}/retry` | **APPLY_FAILED 재처리** (P6-1) | §6 재처리 각주 — `*_modified` 갱신+PROPOSED 되돌림+재발행 |
| GET `/predictions` · `/predictions/chambers/summary` · `/predictions/wafer/{wafer_id}` | 예측 이력·요약·단건 (DB 영속 — A4-1) | predictions_router (A) |
| GET `/predictions/recent` · `/predictions/stream` | 예측 실시간 링버퍼 + SSE (S1 — P5-4) | 인메모리 (재기동 리셋) |
| GET `/raw/recent` · `/raw/stream` | 센서 wafer 점 이력 + SSE (S1 실곡선 — W6-①) | σ 환산 동봉·재기동 리셋 |
| GET `/actual/recent` · `/actual/stream` | 실측 C65 라벨 이력 + SSE (P6-2) | 순수 릴레이·발행 off 시 빈 스트림·재기동 리셋 |
| GET `/limits/active` | 현행 관리선 (F3 버전 칩) | is_active·settled · **trigger_type·k_sigma 동봉** (S2 provisional='가한계·추정' 배지 — P6-2) |
| GET `/limits/corrections` | correction 이력 (F2 승인 마커) | 최신순 n≤50 |
| GET `/quals/recent` | 최신 Qual 판정 (S7) | quals 최신순 n≤10 — A5-3 적재 후 (P6-2) |
| GET `/dispositions/pending` | HOLD 처분 대기 (S6) | 잠정/확정 per_wafer — 계약 §4-B (P6-2) |
| POST `/qual/verdict` | Qual 판정 확정 (S7) | QualVerdictConfirmed 발행 경로 |
| POST `/requalify/{incident_id}` | R9 재수립 승인 (S7) | §8-D 페이로드·§8-C 발행 규약 |
| GET `/requalify/{incident_id}/package` | R9 승인 근거 패키지 (S7) | settle_converged AND·미수렴 warning (P6-2·#53) |
| GET `/simulator/scenarios` · `/status` · `/logs` | 시나리오 목록·상태·로그 (S11) | |
| POST `/simulator/scenarios/{sid}/run` · `/simulator/stop` | **RUN=상시 세계 주입 · STOP=세계 종료** (S11 — M3 ② 상주화) | 미기동 시 baseline 자동 기동 + 주입 · 응답 `mode`=injected/started+injected · delay/limit=최초 기동만 |
| GET `/incidents/recent` | Incident 목록 + 알람 집계·최신 Agent 판정 (S3 — P6-2 후속) | `n`≤200 · `chamber` 필터 · 점수 **max+avg 동봉**(max 만이면 오해 — 7/30 실사용 혼선) · Agent 미가동 구간 title/verdict NULL(실값 위장 금지) · 정렬 updated_at DESC |
| GET `/incidents/{incident_id}/evidence` | S4 02 증거층 — 위반 목록·센서 집합·`since_brief` 배지 (조회 시점 최신) | 집계는 Incident 전체·목록만 상한(집계·목록 분리 — 7/31 버그 수정) · `since_brief.new_sensors`=Brief 이후 **처음** 등장한 센서만 |
| POST `/incidents/{incident_id}/reanalyze` | [재분석] — 최신 SUP Brief 에 `needs_reanalysis` 플래그 (사람이 당기는 구조, PR #70) | ✅ **소비자 가동 (2026-08-03, C #97 — 15s 폴러)**. 시연 사용 가능 — 단 ⚠️ **LLM 정지 상태에서 누르면 영구 「재분석 대기」**(실패 경로가 플래그를 안 내림 — #97 리뷰 §1, C 후속) · Brief 없으면 404 |
| GET `/stack/status` | 서비스 상태 목록 (S11 스택 패널 폴링) | P6-4 — 서비스 정의 정본 = params `stack.services` (REST 는 name 만 받음 — 임의 커맨드 API 금지) |
| POST `/stack/start` | 서비스 기동 — `body.name` 1개 / 생략 시 enabled 전체 (등록 순서·400ms 간격) | 화이트리스트 밖 name 404 · 자식은 **게이트웨이 셸 env 상속**(DSN·KAFKA_BOOTSTRAP 를 게이트웨이 창에서 주입 필수) |
| POST `/stack/stop` | 서비스 정지 — name 1개 / 생략 시 전체 | terminate→5s→kill · 게이트웨이 종료 훅이 전체 정리(고아 방지) |
| GET `/stack/logs/{name}` | 서비스 로그 꼬리 (링버퍼 ≤400행) | ⚠️ `new_console: true` 서비스(a-pred·c-agent — TSR-0001 회피)는 **빈 목록** — 로그는 자동 스폰된 콘솔 창에 |
| GET `/rtd/status` | RTD 3단 장비 정지 「제안」 판정 (S1 배너·TopBar 칩 — 8/5 GO/NO-GO) | 경로①만 판정 — `reference_suspect` 연속 K(=`rtd_consecutive_k`) 챔버별 꼬리 열린 런 **순수 조회·쓰기 0건** · 경로②(lane2)는 `not_wired` 정직 표기 · 응답 `proposed`·`lane1{k,chambers,max_open_run,fired_chambers}`·`basis` · ⚠️ 판정 SQL은 tttm 중복 행에 취약 — B7-1 durable dedup(#106)으로 커버 예정 (B 참고1) |
| GET `/chambers/status` | RTD **자동 정지 실상태** (S1 챔버 칩 uptime%·정지 배너 — #118 신설) | 단일 소스 = `chamber_inhibits`(열린 inhibit = `released_at IS NULL`) · 응답 `window_hours`·`equipment_inhibited`·`items[]` · `/rtd/status` 와 **공존** — 그쪽은 경로① 「제안」 판정(무상태), 여기는 발동·해제 「실상태」 · **C(Agent) 해제 근거 Brief 트리거로도 사용** (#118 C 리뷰 ② ⓑ 채택 → **구현 2026-08-08 · #140**: C 폴러가 `items[].inhibited` 를 Incident 단위로 접어 `release_briefs` 를 만든다 — 규약 §8-D-1) · 해제 부수효과: requalify 가 `ChamberRequalified` emit 후 같은 incident 의 inhibit 일괄 release(실패 시 200 유지 + 로그 — 수동 복구 대상) |

---

## 💡 개발 가이드

1. 위 JSON을 더미 `.json`으로 저장해 병렬 개발 → 통합 시 Kafka 구독 한 줄 교체 (v4.2와 동일)
2. **스키마 변경 절차**: 필드 추가 = PR 사유 + 소비자 리뷰 / 이름 변경·삭제 금지 / 이 문서 동시 갱신 (헌법 2-2)
3. 매직 넘버 금지 — threshold류는 전부 `config/params.yaml` + `.env` (→ `docs/config_파라미터_합의안_v1.md`)
