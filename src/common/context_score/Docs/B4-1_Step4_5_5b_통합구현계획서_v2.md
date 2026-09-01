# B4-1 · Step 4 공통 + Step 5 + Step 5b 통합 구현 계획서 (v2)

| 항목 | 내용 |
| --- | --- |
| **태스크** | Step 4 공통(orchestrator·combine·instrumentation) + Step 5(억제·조립·발행·적재·배선) + Step 5b(B9 crazy 발행 경로) **일괄 구현** — 담당 구분 없이 단일 트랙 |
| **대체** | `B4-1_Step4공통_Step5_구현계획서_v1.md` 폐기·대체 (v1은 A/B 분담 전제 + 5b 제외 + alert_id 포맷 오기) |
| **정본 스펙** | `spec/B4-1_Step4_공통_orchestrator_combine_스펙플랜.md`(D1~D9) · `spec/B4-1_Step5_publisher_억제_배선_스펙플랜.md`(D1~D6·§5) · `spec/B4-1_Step5b_B9_crazy_wafer_스펙플랜.md`(2-A·§4) · `spec/_carryover_Step5_반영대상.md` |
| **검증** | 본 문서의 모든 사실 진술은 **2026-07-28 현재 코드 기준 재검증**했다. 스펙과 코드가 어긋나는 항목은 §0 정정표에 명시 — **§0이 스펙 문서보다 최신** |
| **브랜치** | `feat/common-contextscore` (HEAD 2c5d1fb) → PR → `dev` (5-1·5-3) |
| **작성** | 2026-07-28 · 상태: v2 초안 |

---

## 0. 스펙 문서 검증 정정표 (코드 대조 결과 — 착수 전 필독)

스펙 문서들은 작성 시점이 달라 일부 서술이 코드보다 뒤처져 있다. 아래는 실제 코드로 확인한 정정이다.

| # | 스펙 문서 서술 | 실제 (코드 근거) | 계획 영향 |
| --- | --- | --- | --- |
| V1 | Step4 스펙 헤더 "score_ae(A·**stub**)" | **구현 완료** — `axes/score_ae.py` 전체 구현(e6 밴드·e7 drift·max 집계·[0,1] 가드), 커밋 d8cc26b | 축 4종 전부 완성 — Step4 잔여는 공통 3파일뿐 |
| V2 | Step5 스펙 §4 1-1 "collector 호출(:452)엔 개별 try가 **없다**" (P1 사전수정) | **이미 격리 구현됨** — `spc_consumer.py:451~457` wafer 단위 try + `collector_failed` 카운터 + `_persist_tttm` 무조건 호출 | carryover **1-1(P1) 완료** — 재작업 불요 |
| V3 | Step5 스펙 §5 "C 계약 `SuspectWindow.member_wafers` **C 선반영**" | 현 repo `alert.py:79~86`의 SuspectWindow엔 `member_wafers` **없음** (start·end·basis만) | 발행은 안전(`extra="ignore"`로 무시됨·에러 아님), **실효는 C 스키마 반영 후** — doc-sync·C 통보 유지 |
| V4 | Step5b 스펙 §5 "`read_y_threshold` **신설 필요**(현재 write만 존재)" | **구현 완료** — `establish_engine.py:216`(is_active 1행·recipe None 가드) + `publisher.py:147` `YThresholdCache`(TTL 600s·invalidate) | 5b §7 step 1~3 **전부 완료** — 잔여는 step 4~6(조립·적재·발행) |
| V5 | `publisher.py:185` 주석 "PM/C 회신 확정 **대기** 스텁. 절대 지금 구현 금지" | **stale** — Step5b 스펙 §2-B(2026-07-28 갱신): PM-4 답장 도착 → **마커 확정·step4~6 언블록**. PM-1·2도 해소(incident_type은 grouper가 `rule_id="B9"`에서 파생 — B측 계약·마커 무변경) | **5b emit 3함수 지금 구현 가능** — 구현 시 stale 주석 삭제 |
| V6 | (v1 계획서 오기) alert_id 챔버 세그먼트 `[A-Z0-9_]+` | `mock_alert_publisher.py:52` `ALERT_ID_RE = ^ALERT-\d{8}-[A-Z0-9]+-\d{4}$` — **언더스코어 불허**. mock은 `chamber_id.replace("_","")`로 `SIM_CH_1→SIMCH1` 변환(:103) | 채번 함수도 **동일 변환 필수** (아니면 하류 정규식 검증 탈락) |
| V7 | Step5 스펙에 shap 변환 언급 없음 | `fdc.prediction`의 shap_top3는 **객체형** `[{sensor,name,contribution}]`(`agent_a_mlops/consumer.py:155`), alert §3는 **C코드 문자열 리스트**(`alert.py:109` — 문서에 "§2 객체형과 다름" 명시) | build_alert에서 `[s["sensor"] for s in shap]` **변환 필수** |
| V8 | — | TTTM rollup(`tttm_engine.py:236~244`)엔 `reference_id`·`chamber_id`가 있는데 **DB 전용·alert 유입 금지**(#7 주석) | build_alert tttm 매핑 시 계약 5필드만 추출 |
| V9 | Step5 스펙 D4 "incident_grouper의 SEQ 파싱" | grouper는 alert_id의 SEQ를 **파싱하지 않음** — 자체 `INC-` SEQ 채번(:99·:110), dedup은 `incident_alerts.alert_id UNIQUE` + `ON CONFLICT DO NOTHING`(:205) | 포맷 준수 이유는 6-4 규약+mock 정규식. **중복 채번 = 조용한 알람 유실**(DO NOTHING) 경고는 유효 |
| V10 | — | seam early-return 우회(`:447 not violations and not tttm_obj and not _has_prediction`)·recipe_id 승계(`:444`) **구현 완료** — crazy-only wafer 도달 보장 이미 확보 | 5b §7 step1 완료 |
| V11 | Step4 스펙 D1 "combine dict — A 사인오프 대기 🔴 블로커" | 통합 트랙 전환으로 **사인오프 절차 소멸** — 본 계획 §4 D-1로 확정 | 착수 블로커 없음 |

**결론: 외부 블로커 0. 아래 M1부터 바로 착수 가능.** (잔여 외부 항목은 전부 비블로킹 — §10)

---

## 1. 현재 상태 (코드 근거 요약)

### 1-1. 완료 — 이 위에 얹는다 (재구현 금지)

| 구성요소 | 근거 |
| --- | --- |
| 컨슈머 4토픽 구독·수동커밋·graceful shutdown | `spc_consumer.py:272` (raw·correction·agent·prediction) |
| 예측 캐시 + upsert·TTL·purge 배선 | `prediction_cache.py` + `:295~297·:278` |
| collector 조인(순수)·recipe_id 승계·계약명 정규화·`JoinCounter` | `collector.py` — `build_joined`·`_PREDICTION_KEY_MAP`·`JoinCounter`(미배선) |
| 축 4종 + 배점 config | `axes/` 4파일 + `config.py` `ContextScoreConfig` + `params.yaml:69~99` |
| seam: 6-tuple 콜백 + wafer 단위 예외 격리 + 카운터 | `:453` `(wafer_id, chamber_id, ts, violations, tttm_obj, recipe_id)` + `:451~457` |
| `get_phase` 노출 (억제 게이트 입력) | `:715` — Phase enum: `NORMAL·PHASE_0·PHASE_1·AWAITING_PHASE2` |
| crazy 판정 코어 + y_thresholds read seam | `publisher.py` `CrazyConfig·is_crazy·YThresholdCache` + `establish_engine.py:216` + `tests/test_crazy.py` |
| 위반 dict `member_wafers` | `nelson_engine.py:240` — 룰 창 wafer_id 시간순 |
| DB 스키마 | `init.sql:59` spc_violations(alert_id **비유니크**) · `:123` incident_alerts.alert_id **UNIQUE** · `:380` y_thresholds(TEMP·p99·threshold_version·active 부분유니크) |

### 1-2. 미구현 = 이번 범위

| # | 대상 | 파일 | 현 상태 |
| --- | --- | --- | --- |
| G1 | `combine()` | `combine.py` | 주석 스텁 |
| G2 | orchestrator `context_score()` | `context_score.py` | 주석 스텁 |
| G3 | 계측 `contribution_log` | `instrumentation.py` | 주석 스텁 |
| G4 | publisher 공통(억제·채번·조립·적재·발행) | `publisher.py` | crazy 코어 아래 부재 |
| G5 | 5b emit 3함수 | `publisher.py:197~221` | `NotImplementedError` 스텁 (V5 — 언블록됨) |
| G6 | seam 어댑터 + `main()` 배선 + producer | `agent_b_spc/` | `main():795`이 collector 미주입 — **체인이 아직 안 돎** |
| G7 | carryover P2(JoinCounter 배선)·P3(결측 로그 강등) | `collector.py:102` 외 | 미배선 |

---

## 2. 목표·완료 기준 (DoD)

**목표**: `fdc.raw` → Nelson/TTTM → 조인 → context_score → 억제 → `fdc.alert` 조립(Nelson 경로 + crazy 경로) → `spc_violations` 적재까지 E2E 관통. 발행 스위치 off(dry-run)가 기본.

1. 단위 테스트 green — Step4 §4 + Step5 §6 + Step5b §6 전 항목 (§8).
2. `test_e2e.py` 실구현 — joined 주입 → 점수 → `AlertModel.model_validate()` **실제 파싱 통과** → fake DB 적재 확인.
3. crazy-only wafer(위반·tttm 0) → B9 마커 alert 조립·적재 확인.
4. mock 페이로드와 구조 동일(`ALERT_ID_RE` 매치 포함) — 구독자(C·PM grouper) 무중단.
5. 라이브 스위치 off 기본 — 컷오버는 PM 합의 후(§10).

---

## 3. 전체 배선도 (구현 후)

```
spc_consumer._process_wafer                                     [완료]
  └─ collector callback (wafer_id, chamber, ts, violations, tttm, recipe_id)
        ▼
  alert_seam.make_alert_collector(...)의 closure                 [G6]
    1) joined = build_joined(..., prediction_cache)                    [완료]
    2) join_counter.record(joined) + N건당 요약 로그                    [G7]
    3) enrichment(D8): joined["prediction"] is not None 일 때만
         pred["phase"] = consumer.get_phase(chamber)                   ← Phase enum
         pred["p95_threshold"] = y_cache.p99(chamber, recipe)          ← 캐시 재사용
    4) score = int(round(context_score(joined, cs_cfg)))               [G1~G3]
    5) verdict = is_crazy(pred, chamber, recipe, crazy_cfg, y_cache)   [완료]
    6) publisher.process(joined, score, verdict, deps)                 [G4·G5]
         ① 억제: get_phase == PHASE_0 → 적재·발행 둘 다 skip (Step5 D1)
         ② Nelson 경로: violations 있으면 → 채번 → INSERT(먼저·독립 try)
            → AlertModel 조립 → 스위치 on 시 발행
         ③ crazy 경로: verdict 있으면 → 별도 채번 → 마커 INSERT(먼저)
            → build_crazy_violation 봉투 → 스위치 on 시 발행
         ④ 위반 0 ∧ verdict 없음(TTTM 단독 포함) → skip (Step5 D2)
        ▼
  _persist_tttm — collector 실패와 무관하게 항상 호출               [완료]
```

의존 방향 불변: `agent_b_spc → common` (`__init__.py` 경계 원칙 — common은 consumer를 모른다). seam 어댑터는 consumer 컨텍스트(get_phase·engine·producer)가 필요하므로 `agent_b_spc/`에 신설.

---

## 4. 확정 결정 (구 크로스파트 합의 → 본 계획으로 흡수)

통합 트랙이므로 아래를 본 계획의 결정으로 확정한다. 스펙의 결정 번호와 매핑해 둔다.

| # | 결정 | 근거·출처 |
| --- | --- | --- |
| D-1 | `combine(scores: dict[str,float], cfg) -> float` — 키 `spc·tttm·ae·pred`, **naive_sum** | Step4 D1·D3 (인덱스 침묵오류 제거·grouped_2stage 시 이름 접근) |
| D-2 | 후처리(할인·clip) = orchestrator / combine = 결합 전담 | Step4 D2 |
| D-3 | m1 과도 할인 = combine 前 scores dict에 ×`transient_discount`, 대상 키 = **(a) 전 키** — 모듈 상수 `_DISCOUNT_KEYS`로 국소화(naive_sum에선 (b)와 결과 차이는 spc·ae 외 축 존재 시만) | Step4 D4·A-1 흡수 |
| D-4 | pred 축 v0 = **비활성 유지**(`pred_weight: null`) — 배선(phase·p95 주입)은 완비, 활성화는 Step 6 가중치 확정 때 config만 | Step4 D5 (기획서 §3.2 "확정 전 0") |
| D-5 | orchestrator 반환 = float·순수 / **int 변환은 seam 1곳**(`int(round(…))`) — alert.py `int 0~100` 정합 | Step4 D6 + alert.py:127 |
| D-6 | contribution_log = **축 단위 JSONL append** (필드 §5 M1-3) — 실패는 orchestrator try/except로 격리, element(e1~e5) 확장은 Step 6 | Step4 D6·D7 |
| D-7 | enrichment는 seam이 `build_joined` **이후** `joined["prediction"]`에 주입, pred None이면 skip — collector `_PREDICTION_KEY_MAP` 화이트리스트 무변경 | Step4 D8 |
| D-8 | 억제 = `Phase.PHASE_0`일 때 적재·발행 둘 다 skip + per-chamber 카운터 + 전환 시 요약 로그 1건(wafer별은 DEBUG) | Step5 D1 |
| D-9 | severity 재계산 금지(엔진 `spec.severity` 운반) · context_score 재계산 금지(passenger) · TTTM 단독 발행 skip | Step5 D2·D3 |
| D-10 | alert_id = `ALERT-<YYYYMMDD>-<CHAMBER무언더스코어>-<SEQ:04d>` · SEQ = spc_violations (날짜,챔버) 내 max+1 (DB 조회·재시작 안전) · **챔버 세그먼트는 `replace("_","")`** (V6) | Step5 D4 + mock :103 |
| D-11 | producer 소유 = consumer(main 생성·finally flush), publisher는 주입받아 send만 · 발행 스위치 `spc.publish_enabled=false` 기본 — off는 **send만 skip**(조립·적재는 수행) | Step5 D5 |
| D-12 | `spc_violations` INSERT는 발행보다 **먼저·독립 try** — 기록=must / 발행=best-effort + 실패 카운터 | Step5 D6·carryover |
| D-13 | crazy alert는 **별도 alert 1건**(마커 Violation 단독 봉투) — 같은 wafer에 Nelson 위반이 있어도 합치지 않는다. 근거: PM 해소책이 "grouper가 `rule_id=B9`로 crazy incident 분리"라 혼합 alert면 분리 모호 + `build_crazy_violation` 격리(스왑 지점) 설계 유지 | Step5b §2-B PM 답장·§4 |
| D-14 | crazy 마커 = 선등재 상수 그대로: `rule_id="B9"`·`sensor="C65"`·`window="settled"`·`control_limit_lower=0.0`·`severity="CRITICAL"`·anomaly-only `limit_version="v1"` 센티넬. 대표 arm 우선: 둘 다 hit면 predicted arm 값·컷·threshold_version 사용, anomaly는 description에만 | Step5b D4·리뷰③ (`publisher.py:189~194` 상수 기등재) |
| D-15 | `prediction_context.anomaly_score`·`suspect_window.member_wafers`는 **additive 발행** — C 파서 `extra="ignore"`라 무해, 실효는 C 스키마 반영 후 (2-2 절차: C 리뷰 + API_Contract doc-sync) | V3 + Step5b §2-B |

---

## 5. 구현 단계 (M1 → M5 순차)

### M1. Step 4 공통 3파일 (G1~G3) — 순수·라이브 무영향

구현 순서: combine → orchestrator → instrumentation → 테스트.

**M1-1. `combine.py`**
```python
def combine(scores: dict[str, float], cfg) -> float:
    """naive_sum — 키 spc·tttm·ae·pred. 후처리(할인·clip)는 orchestrator(D-2)."""
    return float(sum(scores.values()))
```
- combine-level 가중치 없음(가중은 축 내부). Step 6 grouped_2stage는 이 함수 내부만 교체(키·축 불변).
- 파일 헤더의 "list 시그니처" 주석을 dict로 동시 개정(4-3).

**M1-2. `context_score.py`** — Step4 스펙 §3-1 의사코드 그대로:
```python
def context_score(joined, cfg) -> float:
    pred = joined.get("prediction")            # seam enrichment 포함 or None
    scores = {
        "spc":  _safe(score_spc(joined.get("violations") or [], cfg)),
        "tttm": _safe(score_tttm(joined.get("tttm"), cfg)),
        "ae":   _safe(score_ae(pred, cfg)),
        "pred": _safe(score_pred(pred, cfg)),   # pred_weight null → 0 (D-4)
    }
    scores = _apply_transient_discount(scores, joined.get("is_transient", False), cfg)
    raw = combine(scores, cfg)
    score = _clip(raw, cfg.clip_min, cfg.clip_max)   # params 키 기존재(:76~77)
    _log_contribution(joined, scores, raw, score)     # try/except 격리 (D-6)
    return score
```
- `_safe(x)`: NaN·Inf·None → 0.0 (값 소독 전용 — 축 내부 예외의 1차 방어는 축 total 계약, 이미 4축 모두 이행됨).
- `_apply_transient_discount`: `_DISCOUNT_KEYS`(모듈 상수, (a)=4키 전부)에 ×`cfg.transient_discount`.
- enrichment는 여기서 안 함(D-7) — 순수 유지.

**M1-3. `instrumentation.py`**
- `log_contribution(joined, scores, raw, final)` — JSONL 1행 append: `wafer_id·chamber_id·ts·s_spc·s_tttm·s_ae·s_pred·raw_combined·final·is_transient·prediction_present`.
- 경로 = 신규 키 `spc.context_score_contribution_log_path`(§7). 디렉토리 생성 포함, 자체 예외는 던져도 orchestrator 격리가 흡수.

**M1-4. 테스트** `tests/test_orchestrator.py` (신규): §8-①.

### M2. publisher 공통 골격 (G4) — 조립·적재까지 라이브 무영향

**M2-1. `PublisherConfig`** — `spc.publish_enabled`(신규 키) 로드, `CrazyConfig.load()`의 `_key/_section` 패턴(부재 시 명시 실패). 토픽명 `"fdc.alert"`는 모듈 상수(계약 고정 2-1).

**M2-2. 채번 `next_alert_id(conn, chamber_id, date) -> str`**
- `recalc_writer._NEXT_SEQ`(:70) SQL 이식: `SELECT COALESCE(MAX(CAST(SUBSTRING(alert_id FROM '[0-9]+$') AS INTEGER)),0)+1 FROM spc_violations WHERE chamber_id=:ch AND alert_id LIKE :date_like` (`date_like = 'ALERT-<date>-%'`).
- 조립: `f"ALERT-{date}-{chamber_id.replace('_','')}-{seq:04d}"` (D-10 — mock `ALERT_ID_RE` 정합, V6).
- recalc_writer의 IntegrityError 재시도는 **이식 불가**(spc_violations.alert_id 비유니크 — 안전망 없음) → 단일 컨슈머 전제 TODO 주석 부착(Step5 D4 확장 주의: 중복 채번 시 grouper `ON CONFLICT DO NOTHING`으로 조용한 유실, V9).

**M2-3. 조립 `build_alert(joined, context_score, alert_id, sensor_map) -> dict`** — AlertModel(alert.py) 1:1, 함정 전부 코드화:
- 최상위: `alert_id`·`timestamp`(**`joined["ts"]` = wafer t0, `now()` 금지**)·`chamber_id`·`violations`·`tttm`·`context_score`(int)·`prediction_context`·`suspect_window`(있을 때만). **최상위 `wafer_id` 금지**(C가 조용히 드롭).
- `violations[]` 매핑 — 엔진 위반 dict(`nelson_engine.py:227~241`) → 계약 키 변환:
  `sensor_id→sensor` · `sensor_window→window` · rule_id·severity·description·current_value·limit_version·control_limit_upper/lower 그대로 · `sensor_name` = `sensor_map.yaml` 조회 시만 병기(mock 선례·표시층 전용) · 엔진 내부 키(`limit_basis`·`offending`·`member_wafers`·`wafer_id`·`timestamp` 등)는 **싣지 않는다**.
- `tttm`: rollup 있으면 계약 5필드만 추출(`reference·score·top_gap_sensor·gap_pct·reference_suspect` — **`reference_id`·`chamber_id`는 DB 전용, 유입 금지** V8). None이면 중립 스텁 5필드 전부: `{"reference":"fleet_median","score":0.0,"top_gap_sensor":"","gap_pct":0.0,"reference_suspect":False}` (일부 누락 = C ValidationError → alert 통째 유실).
- `suspect_window`: violations 전원 `member_wafers` 합집합·dedup → **numeric 정렬**(`key=int(w.rsplit("_",1)[-1] ...)` — wafer_id 포맷 `C64_<n>_CH_x`라 순수 끝자리 아닐 수 있음 → **숫자 세그먼트 파싱 유틸 + 실패 시 원문 fallback**, 문자열 정렬 금지: `"C64_1013.." < "C64_995.."` 역전) → `start=첫·end=끝·member_wafers·basis`(대표 룰 서술). 비면 필드 생략. `member_wafers`는 additive(D-15).
- `prediction_context`: `wafer_id`(**여기 위치**)·`predicted_c65`·`shap_top3`(**객체형→C코드 리스트 변환** `[s["sensor"] for s in shap]`, 비-dict 원소 방어 — V7)·`spc_flags`(위반→`{sensor, rule}` 병렬 채널, 헌법 3-3)·`anomaly_score`(additive).
- 예측 결측 시: `{wafer_id, "predicted_c65": 0.0, "shap_top3": [], "spc_flags":[...]}` 중립 스텁 (Q1 — C 통보 후 확정, 계약상 필수 필드라 생략 불가).

**M2-4. 적재 `insert_violations(conn, joined, alert_id, context_score) -> int`**
- 위반 1건=1행, 같은 alert_id 공유. 컬럼 = `init.sql:59~77`(chamber_id·recipe_id·step·sensor_window·rule_id·sensor_id·severity·current_value·limit_version·ucl/lcl·context_score(FLOAT — int 무손실)·description). commit=호출자(`engine.begin()` — `_persist_tttm`·recalc_writer 선례).

**M2-5. 진입점 `process(joined, context_score, verdict, deps) -> list[str]`**
```
phase == PHASE_0 → suppressed[chamber]+=1, DEBUG, return []          (D-8)
결과 = []
if joined["violations"]:                     # Nelson 경로
    aid = 채번 → [INSERT 먼저·독립 try(D-12)] → build_alert → [스위치 on시 발행] → 결과.append
if verdict is not None:                      # crazy 경로 (M3에서 합류)
    aid2 = 채번 → [마커 INSERT 먼저] → build_crazy_violation 봉투 → [발행] → 결과.append
return 결과                                   # 둘 다 없으면 [] (TTTM 단독 skip, D-9)
```
- 발행: `producer.produce(topic, key=chamber_id, value=json.dumps(alert))` — 직렬화는 mock과 동일 구조. 실패 = ERROR 1건 + `publish_failed` 카운터(best-effort).
- Phase 0→1 전환 요약 로그는 seam에서 직전 phase 비교로 1회.

**M2-6. 테스트** `tests/test_publisher.py`: §8-②.

### M3. 5b emit 3함수 (G5) — V5로 언블록, M2 골격 재사용

**M3-1. `build_crazy_violation(verdict, joined, context_score, alert_id, sensor_map) -> dict`** (스텁 교체)
- AlertModel **봉투 전체** 반환(Step5b §4): `violations=[합성 마커 1건]` + `tttm`(rollup 또는 M2 중립 스텁 재사용) + `prediction_context`(predicted_c65·shap 변환·**anomaly_score**) + `context_score`(int — 게이트 우회지만 추적성 첨부) + timestamp=wafer t0 + `suspect_window` 생략(마커엔 member_wafers 없음).
- 합성 Violation: 선등재 상수(D-14) + 대표 arm 값:
  - 둘 다/predicted hit: `current_value=verdict.predicted_c65`·`control_limit_upper=verdict.threshold`·`limit_version=y_thresholds.threshold_version`(read seam 확장 필요 시 'v1' 우선 — Q2)
  - anomaly-only: `current_value=verdict.anomaly_score`·`upper=verdict.anomaly_cut`(0.6)·`limit_version="v1"` 센티넬
  - 공통: `window="settled"`·`lower=0.0`(비Optional float — null 금지)·`description`=걸린 arm 서술(예: `"B9 crazy: predicted_c65 1650.2 > P99 1590.1"`).
- PM-4 1급필드 전환 시 이 함수만 스왑(격리 목적 유지).
- **주의**: 현 라이브 anomaly=dummy(0.01~0.15 < 컷 0.6)라 anomaly arm은 구조적 미발동 — predicted arm만 실동작. 실 AE 값(A3-3) 전환 시 자동 활성(코드 무변경).

**M3-2. `insert_spc_violation`** (스텁 교체) — M2-4 `insert_violations`에 합성 위반 dict를 태우는 얇은 어댑터로 구현(마커 컬럼값: rule_id=B9·sensor_id=C65·window=settled·lower=0.0). 발행 前 독립 try(D-12) 동일.

**M3-3. `publish_crazy_alert`** (스텁 교체) — M2-5 발행 경로 재사용(별도 함수 유지 = 스펙 격리 의도, 내부는 공통 `_send` 호출).

**M3-4. process 합류** — M2-5의 crazy 분기 활성 + `crazy_detected` per-chamber 카운터 + WARNING 로그 1건.

**M3-5. 테스트** `tests/test_crazy.py` 확장: §8-③.

### M4. 라이브 배선 (G6·G7) — 스위치 off라 라이브 무영향

**M4-1. producer** — `make_producer(bootstrap)` (lazy import confluent `Producer`, `make_consumer:748` 선례). 소유=main, `finally`에서 `producer.flush()` (6-2).

**M4-2. seam 어댑터** — 신규 `src/agent_b_spc/alert_seam.py`:
- `make_alert_collector(consumer, engine, producer, cs_cfg, pub_cfg, crazy_cfg, y_cache, join_counter, sensor_map) -> Callable` — §3 배선도 1)~6)의 closure.
- enrichment(D-7): pred not None일 때만 `phase`·`p95_threshold` 주입. phase는 **값이 아니라 enum 그대로** 넣어도 됨(`score_pred._phase_value`가 `.value` 정규화 — `pred_gated_phases`의 `phase_0·phase_1` 문자열과 매치 확인됨).
- int 변환 단일 지점(D-5).
- `joined["is_qual"]=True` → 발행 skip·기록 정책은 Q3 확정 전 **발행 skip + 카운터**(보수 기본값).
- deps 묶음은 dataclass 1개로(테스트 주입 편의).

**M4-3. `main()` 배선** (`spc_consumer.py:784~`):
```python
producer = make_producer(_bootstrap())
y_cache  = YThresholdCache(reader=lambda ch, rc: read_y_threshold_with(engine, ch, rc))
consumer.collector = make_alert_collector(consumer, engine, producer, ...)
```
- `read_y_threshold`는 conn 시그니처라 engine 래핑 헬퍼 1개 필요.
- firm 재수립 경로(`write_y_threshold` 호출부, `:640` 부근)에 `y_cache.invalidate(chamber)` 훅.

**M4-4. carryover 마무리**
- P2: `join_counter.record(joined)` 배선 + N건당 1회 요약 INFO(hit/miss/miss_rate) — 레이스(2-1) measure-first 실측 입력.
- P3: `collector.py:102` 예측 결측 INFO → **DEBUG 강등** (요약은 P2가 담당).

### M5. E2E 검증

- `tests/test_e2e.py` 실구현(현재 주석만): fake consumer·DB(sqlite or fake conn)·fake producer로 —
  ① 위반+예측 wafer → alert dict를 **실제 `AlertModel.model_validate()`로 파싱**(C 관점 검증) + DB rows + contribution_log 1행
  ② 예측 결측 wafer → SPC 알람 무지연·ae/pred 0·prediction_context 중립 스텁
  ③ is_transient → ×0.5 반영 ④ Phase 0 → 억제(적재·발행 0) ⑤ crazy-only wafer → B9 봉투·적재 ⑥ 게이트 31 경계.
- 검증 체인: pytest 전체 → 자체 리뷰 → **서브에이전트 코드 대조**(본 계획 D-1~D-15 항목별) → mock 페이로드 diff(`validate_alert` 기준).

---

## 6. 파일별 변경 명세

| 파일 | 변경 | 단계 |
| --- | --- | --- |
| `common/context_score/combine.py` | 스텁 → dict naive_sum + 헤더 개정 | M1 |
| `common/context_score/context_score.py` | 스텁 → orchestrator + 헬퍼 4종 | M1 |
| `common/context_score/instrumentation.py` | 스텁 → JSONL 계측 | M1 |
| `common/context_score/publisher.py` | Step5 공통 5함수 추가 + 5b 스텁 3종 교체 + stale 주석(:185) 삭제 | M2·M3 |
| `common/context_score/collector.py` | `:102` INFO→DEBUG만 | M4 |
| `agent_b_spc/alert_seam.py` | **신규** — seam closure + deps dataclass | M4 |
| `agent_b_spc/spc_consumer.py` | `make_producer` + `main()` 배선 + flush + y_cache invalidate 훅 | M4 |
| `agent_b_spc/establish_engine.py` | (Q2 채택 시) read seam에 threshold_version 동시 반환 확장 | M3 |
| `config/params.yaml` | §7 신규 키 | M2 |
| `tests/` | test_orchestrator·test_publisher 신규 · test_crazy 확장 · test_e2e 실구현 | M1~M5 |
| `docs/API_Contract_명세서.md` | anomaly_score·member_wafers additive doc-sync | §10 절차 |

**불변(건드리지 않음)**: 축 4종 · config.py · prediction_cache · nelson/tttm 엔진(위반 dict 계약 포함) · crazy 판정 코어 · `mock_alert_publisher`(PM 소유 — 제거 금지, 이중 발행은 스위치 off로 회피) · `alert.py`(C 소유) · seam 6-tuple 시그니처.

## 7. config 신규 키 (params.yaml — 6-1, doc-sync 4-3 동반)

| 키 | 기본값 | 용도 |
| --- | --- | --- |
| `spc.publish_enabled` | `false` | 발행 스위치 — off는 send만 skip(조립·적재 dry-run) (D-11) |
| `spc.context_score_contribution_log_path` | `logs/contribution_log.jsonl` | M1-3 계측 덤프 |
| `spc.y_threshold_cache_ttl_sec` | `600` | `publisher._Y_CACHE_TTL_SEC` 하드코딩 승격([잠정] 주석 이행) |

기존 키 그대로 사용: `context_score_*` 배점·clip(:69~99) · `crazy_wafer.*`(:46~) · `ct.ae_anomaly_threshold`(:128) · `prediction_cache.*`(:101~).

## 8. 테스트 플랜 총괄

**① test_orchestrator (M1)**: combine naive_sum(`{"spc":15,"tttm":15,"ae":0,"pred":0}==30`)·빈 dict→0 / 4축 dispatch / 할인 (a) 전 키·False 무할인 / clip 음수→0·초과→100 / pred None→ae·pred 0 무크래시 / NaN·Inf→`_safe` 0 / `_log_contribution` 예외 주입→score 정상 반환 / 배점 예시 재현(현행 config 값으로 기대값 산출 — 설계 §5 가안 수치 그대로 아님·주석 명기).

**② test_publisher (M2)**: Phase 0→적재·발행 둘 다 skip+카운터 / TTTM 단독→skip·위반→발행 / passenger(재계산 없음·DB=alert 동일 값) / alert_id: max+1·`:04d`·**`SIM_CH_1→SIMCH1`**·N행 공유·재시작 이어채번·`ALERT_ID_RE` 매치 / 스위치 off→send 0회·조립·적재 수행 / INSERT 발행前 독립(발행 예외에도 rows 존재) / tttm None→중립 5필드 / tttm rollup→DB 전용 키 미유입 / shap 객체형→C코드 리스트 / suspect_window 합집합·dedup·numeric 정렬(`995 < 1013`)·빈 생략 / 최상위 wafer_id 부재 / `AlertModel.model_validate()` 통과.

**③ test_crazy 확장 (M3)**: (기존 판정 테스트 유지) + 마커 Violation 필드(D-14 상수·대표 arm 값·anomaly-only 센티넬) / 봉투 파싱 통과(window·lower float·tttm·int) / anomaly_score 첨부 / crazy INSERT 발행前 독립 / Nelson+crazy 동시 wafer→alert 2건·SEQ 2개(D-13).

**④ test_e2e (M5)**: §5 M5 시나리오 6종.

## 9. 헌법·계약 체크

- **1-1** 관리선 변경 없음(발행·적재만) ✅ / **1-2** fdc.alert 단일 채널·Agent 직접 호출 없음 ✅ / **1-3** predicted_c65=모델 출력 운반(feature 아님) ✅ / **1-4** 승인요청 생성 없음(Incident·HOLD·disposition=PM downstream) ✅
- **2-1** 변경금지 5필드 보존(wafer_id·chamber_id·rule_id·severity·context_score — 하위 객체 위치 포함)·토픽명 고정 ✅ / **2-2** additive 2필드(anomaly_score·member_wafers) → C 리뷰+doc-sync 절차(발행은 extra=ignore로 무해) ◐절차
- **3-3** SHAP 순위 무개입(spc_flags 병렬 채널) ✅ / B9 마커의 violations[] 혼입은 PM-4 확정안 ✅
- **6-1** 매직넘버 params·마커 리터럴 상수화(기등재) ✅ / **6-2** producer graceful=main·직렬화 실패 skip ✅ / **6-4** C코드 유지·sensor_name 표시층·alert_id 채번 주체=이 모듈 ✅
- **3-1** `src/common/` 소유권 미등재 — PM 등재 요청 지속(비블로킹) ◐

## 10. 잔여 외부 항목 (전부 비블로킹) · 열린 결정

| # | 항목 | 상대 | 성격 |
| --- | --- | --- | --- |
| E1 | anomaly_score·member_wafers C 스키마 반영 + API_Contract doc-sync | C·PM | 반영 전까지 no-op(무해) |
| E2 | mock 컷오버(스위치 on·mock off) — 이중 발행 방지 | PM | M5 완료 후 합의 |
| E3 | `y_thresholds` TEMP → 공식 DDL 승인(3-2) | PM | read seam은 현 TEMP로 동작 |
| E4 | 실 AE 값 전환(A3-3) → anomaly arm·e6/e7 실동작 | (구 A 트랙) | 코드 무변경 자동 활성 |
| E5 | crazy 하류(HOLD·wafer_dispositions·incident_type 분리) | PM | 발행까지가 본 범위 |
| Q1 | 예측 결측 시 prediction_context 중립 스텁(predicted_c65=0.0) | C 통보 | 기본값으로 구현·통보 후 조정 |
| Q2 | crazy limit_version에 threshold_version 실값 vs 'v1' 고정 | 내부 | v0='v1' 고정 → read seam 확장은 후속(스펙 D4 허용 "or 'v1'") |
| Q3 | is_qual wafer 발행 정책 | 내부·PM | 기본값 = 발행 skip·카운터(Qual은 qual_engine 트랙) |
| Q4 | Phase 0에서 crazy 발행 여부 | 내부 | **v0 = 억제에 포함(skip)** — Step5 D1 "둘 다 skip"을 crazy에도 적용(단순·보수). 미탐 우려 시 카운터 실측 후 예외 검토 |

## 11. 리스크·주의

- **AlertModel 함정 4종**(테스트로 강제): 최상위 wafer_id 금지 / tttm 5필드 전부 / timestamp=wafer t0 / shap 객체형 변환.
- **alert_id**: 챔버 언더스코어 제거 누락 시 mock 정규식·6-4 포맷 탈락(V6). 중복 채번은 grouper에서 **조용한 유실**(V9) — 단일 컨슈머 전제 유지, 확장 시 DB 시퀀스 리팩토링(TODO).
- **suspect_window 정렬**: wafer_id가 `C64_<n>_CH_x` 형태 — 끝 세그먼트가 숫자가 아닐 수 있어 숫자 세그먼트 파싱 유틸 필요(mock `_base` 포맷 확인 완료).
- **이중 발행**: mock 가동 중 스위치 on 금지(E2 전까지 off).
- **스펙 문서 역-갱신**: 구현 후 Step4·5·5b 스펙의 §0/개정 이력에 본 문서 V1~V11 정정 반영(4-3) — 특히 publisher.py stale 주석 삭제와 세트.
- 30분+ 디버깅 발생 시 TSR(`docs/adr/`) 작성 세트(헌법 8장).

## 12. 진행 순서·커밋 플랜

```
M1 Step4 공통+테스트 ─▶ M2 publisher 공통+테스트 ─▶ M3 5b emit+테스트
                                                        │
                                        M4 라이브 배선(스위치 off)
                                                        ▼
                                        M5 E2E → 스펙 역-갱신 → PR(dev)
```

커밋 분리(5-4): `feat: B4-1 Step4 공통 combine·orchestrator·계측 구현` → `feat: B4-1 Step5 publisher 억제·조립·적재 구현` → `feat: B4-1 Step5b crazy 마커 발행 경로 구현` → `feat: B4-1 Step5 seam·producer 라이브 배선(발행 off)` → `test: B4-1 E2E 관통` → `docs: B4-1 스펙 정정·doc-sync`. PR 1건(리뷰 1인+CI) — 평시 대상 `dev`.
