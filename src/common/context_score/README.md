# context_score — B4-1 알람 종합 파이프라인

`fdc.raw`에서 나온 웨이퍼 단위 이상 신호(관리도 위반·플릿 이탈·이상탐지·예측)를 **하나의
`context_score`(0~100)로 종합**하고, 억제·조립을 거쳐 표준 경보 `fdc.alert`로 발행하는 공용 모듈.

- **소유**: 팀원 B (팀원 B) · **경로**: `src/common/context_score/`
- **입력**: consumer(`agent_b_spc`)가 넘기는 `joined` 이벤트 · **출력**: `fdc.alert` + `spc_violations` 적재
- **의존 방향**: `agent_b_spc → common` 단방향 (common은 consumer를 모른다)

---

## 파이프라인

```
fdc.raw ─▶ spc_consumer (Nelson·TTTM 판정, agent_b_spc)
             │  6-튜플 (wafer_id, chamber, ts, violations, tttm, recipe_id)
             ▼
        ┌─ alert_seam.make_alert_collector ─────────────────────────┐
        │  1) collector.build_joined      3채널 wafer_id 1:1 조인    │
        │  2) enrichment                  pred에 phase·p95 주입      │
        │  3) context_score               4축 → 소독 → 할인 → clip   │
        │  4) int(round(...))             정수화 단일 지점 (D-5)     │
        │  5) is_crazy                    B9 crazy 판정              │
        │  6) publisher.process           억제→채번→적재→조립→발행   │
        └───────────────────────────────────────────────────────────┘
             │
             ├─▶ fdc.alert        (C·PM 구독 — 승인 워크플로)
             └─▶ spc_violations   (DB 적재)
```

> **점수 계산과 발행은 분리**된다. `context_score`가 점수를 **한 번** 산출하면, `publisher`는
> 그 값을 재계산하지 않고 운반만 한다(passenger 원칙).

---

## 구성

| 파일 | 역할 |
|---|---|
| `collector.py` | 세 채널(SPC 위반·TTTM rollup·예측)을 `wafer_id`로 조인해 `joined` 이벤트 생성. 예측 결측이면 대기 없이 진행. `JoinCounter`로 조인 hit/miss 계측. **순수 함수**. |
| `context_score.py` | 얇은 오케스트레이터. 4축 dispatch → `_safe` 소독 → 과도(C42) 할인 → `combine` → `[0,100]` clip. **float 반환**(int 변환은 seam 몫). |
| `combine.py` | 4축 점수를 이름으로 합산(naive_sum). 키 `spc·tttm·ae·pred`. |
| `axes/` | 축별 점수 함수 4종 — 어떤 입력에도 raise 안 하는 total function. |
| `instrumentation.py` | 축 기여 1행을 `contribution_log`(JSONL)에 append. Step 6 튜닝 입력 자산. 실패해도 점수 반환을 막지 않는다. |
| `publisher.py` | 발행 계층 전부 — 억제·채번·조립·적재·발행 + B9 crazy 마커 + `YThresholdCache`. 진입점 `process()`. |
| `config.py` | `ContextScoreConfig`(배점·할인·clip 경계) — `params.yaml` 로드. |
| `prediction_cache.py` | A 예측(`fdc.prediction`) 캐시. consumer가 주입, collector는 `get`만 호출. |

### 4축 (`axes/`)

| 축 | 파일 | 신호 |
|---|---|---|
| `spc` | `score_spc.py` | Nelson 위반 (관리도) |
| `tttm` | `score_tttm.py` | fleet median 대비 이탈 |
| `ae` | `score_ae.py` | AE 이상 점수 (`anomaly_score`) |
| `pred` | `score_pred.py` | 예측 상단 초과 (`predicted_c65` vs P95) — **v0 비활성**(`pred_weight=null`) |

---

## 계약

### 입력 — `joined` (collector 출력)
```
wafer_id · chamber_id · recipe_id · ts · violations[] · tttm{} · prediction{} · is_transient · is_qual
```

### 출력 — `fdc.alert` (계약 §3)
```json
{
  "alert_id": "ALERT-20260728-SIMCH1-0001",
  "timestamp": "...", "chamber_id": "SIM_CH_1",
  "violations": [{ "rule_id": "N3", "sensor": "C11", "window": "settled", "severity": "...",
                   "current_value": ..., "control_limit_upper": ..., "control_limit_lower": ... }],
  "tttm": { "reference": "fleet_median", "score": ..., "top_gap_sensor": "...",
            "gap_pct": ..., "reference_suspect": false },
  "context_score": 72,
  "suspect_window": { "start_wafer": ..., "end_wafer": ..., "member_wafers": [...], "basis": "..." },
  "prediction_context": { "wafer_id": ..., "predicted_c65": ..., "anomaly_score": ...,
                          "shap_top3": ["C11","C62","C17"], "spc_flags": [{"sensor":"C11","rule":"N3"}] }
}
```
- **변경 금지 필드**: `wafer_id · chamber_id · rule_id · severity · context_score` (계약 §2-1)
- **센서는 C코드**(`C11`) — 표시명(`DC_Bias`)은 표시 계층만 (헌법 6-4)
- 조립 산출은 소비자(C) `AlertModel`로 **실제 파싱** 검증 (E2E)

---

## 두 경보 경로

`publisher.process()`는 억제 판정 후 아래로 갈린다:

| 경로 | 조건 | 동작 |
|---|---|---|
| **Nelson** | 위반 있음 | 채번 → `spc_violations` INSERT → `AlertModel` 조립 → 발행 |
| **crazy (B9)** | 예측/이상 극단 (`is_crazy`) | **별도** 채번 → B9 마커 INSERT → `build_crazy_violation` 봉투 → 발행 |
| skip | 위반 0 ∧ crazy 없음 (TTTM 단독 포함) | 아무것도 안 함 |

- **crazy 마커**: `rule_id=B9` · `sensor=C65`(가상) · `window=settled` · `control_limit_lower=0.0` · `severity=CRITICAL`
- 같은 웨이퍼에 Nelson 위반 + crazy면 **경보 2건**(합치지 않음) — grouper가 `rule_id=B9`로 분리 (D-13)

---

## 핵심 설계 원칙

| 원칙 | 내용 |
|---|---|
| **passenger 점수** | 점수는 오케스트레이터가 한 번만 계산 · publisher는 운반만 → DB와 alert 값 일치 보장 |
| **Phase 0 억제** | 신규 챔버 초기 구간(`Phase.PHASE_0`)은 적재·발행 둘 다 skip |
| **발행 스위치 off = dry-run** | `spc.publish_enabled=false` 기본 · off는 send만 skip(조립·적재는 수행) → 라이브 무영향 |
| **int 변환 단일 지점** | 오케스트레이터는 float · 정수화는 seam 1곳(`int(round())`) — `AlertModel.context_score`가 int 0~100 |
| **SHAP 독립 채널** | 룰이 SHAP 순위를 건드리지 않는다 · Nelson 위반은 `spc_flags`로 병렬 제공 (헌법 3-3) |
| **채번 = B 소유** | `alert_id` = `ALERT-<YYYYMMDD>-<CHAMBER>-<SEQ>` · DB max+1(재시작 안전) |

---

## 설정 (`config/params.yaml`)

| 키 | 기본 | 용도 |
|---|---|---|
| `spc.publish_enabled` | `false` | 발행 스위치(off=dry-run) |
| `spc.context_score_*` | — | 축 배점·clip·할인계수 |
| `spc.crazy_wafer.*` | — | B9 판정 임계 |
| `spc.y_threshold_cache_ttl_sec` | `600` | `YThresholdCache` TTL |
| `spc.context_score_contribution_log_path` | `logs/...jsonl` | 계측 덤프 |

매직넘버는 전부 `params.yaml` 경유 (헌법 6-1).

---

## 테스트

```bash
pytest src/common/context_score/tests/ -q          # 순수·sqlite (브로커·Postgres 불요)
DATABASE_URL=... pytest ... -q                      # pg 통합(채번 SQL·실 스키마 INSERT)
```

| 파일 | 대상 |
|---|---|
| `tests/test_collector.py` | 조인·정규화·JoinCounter |
| `tests/test_orchestrator.py` | 4축 dispatch·할인·clip·계측 격리 |
| `tests/test_publisher.py` | 억제·채번·조립·적재·발행 + `AlertModel` 파싱 + pg 통합 |
| `tests/test_crazy.py` | B9 판정·마커·`YThresholdCache` |
| `tests/test_e2e.py` | collector→context_score→publisher 관통 6종 |

---

## 관련 — 정기 리캘리 (`agent_b_spc`)

context_score와 같은 SPC 시스템의 실력치(관리 기준선) 자동 재산정 파이프라인:

- **Step 7** (`spc_consumer._maybe_periodic_recalc` 등): 챔버별 텀블링 트리거 → 무이상 게이팅(NORMAL·PROPOSED·incident) → `recompute_group` → 상한 내 자동적용 / 초과 `PROPOSED` 기록 → 내부 리로드 · TTTM baseline 레짐 행 추종
- **Step 7b** (`_eval_shadow`): `needs_approval` 건에 소급 오탐 감소율(shadow) 채점을 붙여 승인 근거 제공

---

## 상태

| 범위 | 상태 |
|---|---|
| B4-1 M1~M5 (collector·orchestrator·publisher·seam·E2E) | ✅ 완료 |
| Step 7 (정기 리캘리 트리거·게이팅·리로드·baseline) | ✅ 완료 |
| Step 7b (shadow 채점) | ✅ 완료 |
| **Step 6 (배점 튜닝·검증·고도화)** | ⏳ 이월 — 별도 단계 |

> `pred` 축은 Step 6 가중치 확정 전까지 비활성(`pred_weight=null`). 배선(phase·p95 주입)은 완비.
