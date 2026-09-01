# CT①(lean85) 일간 재학습 루프 — 배선 설계 v1 (제안)

> ⚠️ **본 문서는 v2로 대체되었다 (2026-08-05)** — 정본: `CT1_배선_설계_v2.md`.
> v2 핵심: **WP-7(배포 후 감시·자동 롤백)·S3 폐지 → D13(절대 열화 가드)·D14(리로드 스모크) 확정 대체**,
> §3-1 실행시각(KST 07:00) · 개정 ④(consumer 게이트 한정) 반영. 본 §5 후속의무 1(S3 선행 권고)·§8 WP-7·§9 S3 은 폐기됨.
> 본 파일은 이력 보존용(양안 비교표·리뷰 상세·갭 실사 원문).

> 2026-08-04 작성 · 상태: **D3 확정(① 완전 자동) · WP-1~5 배선 구현 완료** (§5 결정 기록란 · §5-1 구현 현황).
> 상류 정본: 헌법 3-3 ①(일간 슬라이딩·창 365일+완결 요란 레짐 조항·champion–challenger 통과 전 배포 금지) · 1-1 · 1-3 · 계약 §7(fdc.mlops) · §8-B-1(pm_log) · §8-E(CT² 게이트 규약 — 대칭 템플릿) · `config/params.yaml` `ct:`(:151-154) · `docs/config_파라미터_합의안_v1.md` C3·C4.
> 실증 근거: `예측모델_재보정주기_실험설계_v1.md` · `예측모델_재보정주기_실험결과_v1.md` (2026-08-03, R1 일간 확정).
> 설계 선례: `../Autoencoder/CT2_배선_설계_v2.md` — 오케스트레이터·게이트·promote 신호·개통 단계 패턴을 대칭 재사용한다.

---

## 0. 요지

CT①은 헌법·config 수준에서는 확정돼 있으나(**일간 슬라이딩 · 창 365일 · 완결 요란 레짐 조항 · 승격 기준 3.0%**), **운영 경로 구현은 0**이다 — 창 절단·champion–challenger·스케줄러·consumer 리로드 전부 부재(§1-2). 본 설계는 CT² 배선(v2)의 확정 패턴(오케스트레이터 → 자동 검증 게이트 → promote 신호 → 관리형 리로드 → 3중 기록)을 일간 캐던스에 맞게 이식한다.

- **생성(재학습)은 자동** — 헌법 3-3① 확정 사항. 상주 오케스트레이터가 매일 실행 + 요란 PM 기입 시 즉시 실행.
- **배포(모델 교체)는 자동 검증 게이트(champion–challenger 포함) 전 항목 PASS가 선행 조건** — 헌법 3-3① "평가 통과 전 배포 금지".
- **게이트 PASS 후의 배포 방식만 미결** — ① 완전 자동 vs ② 스위치식(차단기 기본 false → 실증 후 개통). 양안 비교는 §5. **PM 결정 요청.**

## 1. 전제와 현재 갭

### 1-1. 확정된 전제 (재확인 — 본 설계가 바꾸지 않는 것)

| 항목 | 확정 내용 | 정본 |
|---|---|---|
| 트리거 | **일간 슬라이딩** — "매일 재학습(생성)하고 가장 오래된 하루를 버림" (멘토 7/21 "실시간 스트리밍이니 1일") | `ct.ct1_trigger: daily` (params.yaml:151) · 합의안 C3 |
| 학습 창 | **365일** (현업 대PM 3~4개월 × 최악 3사이클 = 완결 2 + 진행 1) | `ct.ct1_window_days: 365` (:152) · 합의안 C4 |
| 완결 요란 레짐 조항 | 창 내 **완결 요란 레짐(loud PM~다음 PM) < 1이면 직전 완결 요란 레짐까지 임시 확장, 충족 시 365 복귀** (요란은 대PM의 30% — 1년 창도 34% 확률로 미포함) | `ct.ct1_require_complete_loud_regime: true` (:153) · 합의안 C4 |
| 배포 게이트 | **champion–challenger 통과 전 배포(모델 교체) 금지**. 승격 기준 = RMSE 개선 ≥ 3.0% | 헌법 3-3① · `ct.ct1_promote_min_rmse_gain_pct: 3.0` (:154) |
| PSI·AE 드리프트 | **재학습 트리거 아님** — 서빙 보호(low_confidence)·에스컬레이션 채널 | 헌법 3-3 말미 · `lean85.psi_alert` 주석 |
| Cycle 내 격차 | Model R2R(bias)가 담당 — CT①과 별개 (ct0) | 헌법 1-1 · 3-3 ⓪ |
| 학습기·포맷 | XGBoost · 네이티브 `lean85_model.json` + `manifest.json` · stamp 폴더 신규 저장 | 헌법 3-3 · 4-1 · `lean85.model_store_dir` |
| 실증 | D1이 D7 대비 pooled RMSE **+22.66%** 개선(p=2.67e-08) — 단, **이음새 7일 제외 시 +3.87%. 두 수치 병기 의무.** 무재학습 STATIC 334.2(발산) · challenger 나이 6일 = 누적 열화 +6.41 RMSE | 실험결과 v1 :11-15·:110 |

### 1-2. 현재 갭 (2026-08-04 실사 — 전부 신규 구현 대상)

| # | 갭 | 근거 |
|---|---|---|
| G1 | `lp.retrain()`에 **슬라이딩 창 절단 없음** — 넘긴 raw 전체를 학습. 창 절단의 유일한 실동작 구현은 실험 하네스(`scripts/exp_recalib_cycle/run_backtest.py:188-209`)뿐이며 운영 경로와 무관 | 파이프라인 실사 |
| G2 | **완결 요란 레짐 확장 판정 코드 없음** (config 키·문서만) | 〃 |
| G3 | **champion–challenger 비교·승격 코드 없음** (실험 §8-A는 사후 소급 시뮬) | 〃 |
| G4 | consumer에 **lean85 런타임 리로드 없음** — `_init_predictor()` 기동 1회. AE만 `_check_ct2_promote`(consumer.py:639) 보유 | consumer.py 실사 |
| G5 | **일간 배치 메커니즘 전무** — cron/스케줄러/GH Actions schedule 흔적 0건. 앱 프로세스는 `scripts/run_stack.ps1` 창 기동 방식 | 레포 전수 검색 |
| G6 | 모델 최신 선택이 `sorted(glob)[-1]` **이름 사전순** — `_tag` 접미에 취약, stamp 폴더 1개뿐이라 미검증 | consumer.py:567-585 |
| G7 | stamp 폴더 **보존(GC) 정책 부재** — 일간이면 연 365폴더 | models/ 실사 |
| G8 | 운영 재학습 **데이터 원천 미정** — 현 CLI는 `train_data.csv`(개발 데이터), raw 적재기 없음. 라벨(실측 C65) 조인 경로 미정 | retrain_lean85.py:34-37 |
| G9 | `ct_decisions.ct_type='ct1_lgbm'`(스키마 주석·계약 §7) — **XGBoost 확정(7/22) 이전 명칭** 잔재. ct1 기록 행 0건 | db/init.sql §11 |
| G10 | params.yaml **창 키 이중화·모순** — `ct.ct1_*`(정본)와 `lean85.retrain_*` 병존, 확장 복귀값이 ct 절 "365" vs lean85 절 "**240**"(:221)으로 불일치 | params.yaml 실사 |

## 2. 결정표 (D1~D12)

> D3만 미결(★). 나머지는 본 설계의 제안 확정값이며, 리뷰에서 뒤집히면 본 표를 개정한다.

| # | 항목 | 결정 | 근거 |
|---|---|---|---|
| D1 | 실행체·트리거 | **상주 오케스트레이터 `ct1_orchestrator.py`** (A 소유). 매일 `ct.ct1_run_at_utc` 정기 실행 + `lp.should_retrain()` 폴링으로 **요란 PM 기입 즉시 실행**(이벤트). 기동 시 캐치업(마지막 stamp가 1일 초과 경과면 즉시 1회) | 레포에 스케줄러 전무(G5) — 기존 운용 방식(run_stack.ps1 상주 창)과 정합. `should_retrain`(lean85_pipeline.py:437)이 정기+이벤트 판정을 이미 구현 |
| D2 | 학습창 절단 | `lean85_pipeline.py`에 **`slice_train_window()` 신설** — (d−H−365, d−H] 절단 + 완결 요란 레짐 <1 시 직전 완결 레짐 시작까지 임시 확장. **레짐 완결 판정 소스 = pm_log.json의 물리 PM 이벤트만** (모델 출력 무관 — 순환 참조 차단, `제안_Phase1_정착판정_데이터기반화_v1.md:157` 경고 수용) | 참조 구현 = run_backtest.py:188-209. H = 게이트 평가 버퍼(D4) |
| D3 | 배포 정책 | ✅**확정 — ① 완전 자동** (2026-08-04 PM). 게이트 전 항목 PASS 시 즉시 자동 promote. 차단기 `ct.ct1_auto_promote` 는 비상 정지용 존치(기본 true) — §5 ⓓ 절충 | 헌법 3-3①은 "통과 전 금지"만 규정 — 통과 후 방식은 열려 있음. 일간 캐던스에서 ②안은 매일 수동 promote 가 즉시 병목 |
| D4 | C–C 프로토콜 | **공통 버퍼 홀드아웃**: 모든 모델의 학습 상한 = 실행일 d − H (`ct1_gate_eval_days`, 제안 1). 평가셋 = (d−H, d] **라벨 완결 + 온셋 제외(low_confidence=0)** wafer. 판정 = pooled RMSE 개선 ≥ `ct1_promote_min_rmse_gain_pct`(3.0) → 승격, 미달 = SKIP | 같은 규율이면 champion(과거 승격 시점에 d′−H까지 학습)도 평가창을 학습한 적 없음 → 공정 비교. 온셋 제외 = 온셋 오차 급증 실증(README §5, fold 147/113/109) |
| D5 | promote·리로드 | **CT² 신호 채널 대칭**: 게이트 PASS 후 `control/ct1/promote_<ct_id>.json` 원자 드롭 → consumer `_check_ct1_promote()`(신설)가 스캔 주기마다 감지, **로드 성공 시에만 원자 스왑**, 실패 시 `failed/` 이동·기존 유지, 완료 시 `processed/` 이동. `model_search_glob` 최신 선택(G6)은 **부트스트랩 폴백으로 강등** — 운영 교체는 promote 신호가 정본 | 계약 §8-E promote 효과 규약·consumer.py:639 패턴 복제. 자동·수동이 같은 신호 채널(D3 어느 안이든 동일) |
| D6 | 기록 | **매 실행 `ct_decisions` 1행**(RUNNING 선점 → PROMOTED/SKIP/BLOCKED/FAILED finalize, rmse_before/after·champion_challenger_result 기입) + **승격 시 `models/CHANGELOG.md`** + `fdc.mlops` 발행. ct_id = **`CT-<YYYYMMDD>-ALL-<SEQ>-XGB`** (CT² `-AE` 접미와 대칭 — `ct2_orchestrator.ct_id_for` docstring의 충돌 방지 예약 이행) | 헌법 3-3 ⓒ·7장 "수동 CT를 기록 없이 종결 금지". CHANGELOG 밀도 해석은 §7-3 (PM 확인) |
| D7 | 네이밍 | stamp = `lean85_<YYYYmmdd_HHMMSS>_<tag>`, **tag 고정 어휘 {daily, evtpm, manual}** → 최장 28자. `retrain()`에 **32자 초과 시 raise 가드 추가** | `ct_decisions.model_version_after`·`approval_records.selected_option` VARCHAR(32) — CT² 절단 사고 재발 방지(7장) |
| D8 | 보존(GC) | **승격 계보(champion 이력) 전부 보존** + 미승격 challenger는 `ct1_gc_keep_unpromoted_days`(제안 14) 경과 후 삭제. 단 **직전 champion 1개는 롤백 대비 무조건 보존**. 삭제 전 ct_decisions 대조 | 연 365폴더 방지(G7). 4-1 "덮어쓰기 금지"는 준수(삭제≠덮어쓰기) — 해석 확인은 §7-3 |
| D9 | 데이터 원천 | **시뮬 단계**: 정본 raw CSV(시뮬레이터 입력과 동일 소스)를 조립기가 날짜 절단 — 라벨(C65) 동봉. **실운영 전환**: raw 적재기(fdc.raw → 일자 파티션) 신설 + 라벨은 `wafer_predictions.actual_c65`(measured_at 존재분) 조인 — 후속(§10). **라벨 정책**: 라벨 완결 wafer만 학습, **차단(clamp)된 옛 라벨도 CT① 학습에는 사용**(사이클_정의 신규 결정 5) | 실험 하네스 prep_data와 동일 계보. Kafka 되감기(CT² D9)는 365일 창에 부적합(retention) |
| D10 | 실패 처리 | 재학습 예외 = FAILED 기록 + 익일 정기 실행이 자연 재시도. 게이트 FAIL(누수·완결성 등 critical) = **BLOCKED + challenger 보존·미배포 + 에스컬레이션**(현 수준 = 로그+ct_decisions — CT²와 동일). SKIP(C-C 미달) = 정상 종료. **연속 미승격 `ct1_max_stale_champion_days`(제안 7) 초과 시 에스컬레이션**(경보만 — 게이트 우회 강제 교체 금지) | challenger 나이 6일 = +6.41 RMSE 실증. FAIL을 사람에게 떠넘겨 통과시키는 우회로 금지(1-4 각주 정신) |
| D11 | 개통 순서 | **S0~S3 4스테이지** (§9). 현 위치 = S0(설계·배선) | CT² D11 대칭 |
| D12 | 게이트 임계 소재 | **헌법·본 설계서에 수치 미기재** — 단일 소스 = `lean85/validate_lean85.py`(신설), 임계 = `ct.ct1_gate_*` | CT² D12 선례("테이블 개수는 init.sql이 단일 소스"·"매직 넘버는 전부 params.yaml") — 재개정 반복 회피 |

## 3. 확정 루프 (도표)

```
==============================================================================
  CT1 LOOP -- lean85 일간 슬라이딩 재학습 [v1 제안]
==============================================================================

 [매일 ct1_run_at_utc]          [요란 PM 기입 -- pm_log.json (B 작성, A 읽기전용)]
        |                                   |
        v                                   v
+================ ct1_orchestrator (상주, A 소유) =========================+
| (0) 가드: ct1_auto_generate? / 멱등(ct_id 선점 RUNNING) / 중복 실행 락   |
| (1) 트리거 판정: lp.should_retrain(manifest, pm_log) -- 정기 OR 신규 PM |
| (2) 학습창 절단: (d-H-365, d-H]                                         |
|     + 완결 요란 레짐(loud PM~다음 PM) < 1 -> 직전 완결 레짐까지 확장    |
| (3) 조립: raw 원천 날짜 절단 + 라벨 완결분 (옛 라벨 포함 -- 결정 5)     |
| (4) 재학습: lp.retrain() -> challenger stamp 폴더 (미배포)              |
|     subprocess 격리 · 타임아웃 · 32자 네이밍 가드                       |
| (5) 자동 검증 게이트: validate_lean85 (exit 0=PASS / 2=FAIL / 1=오류)   |
|     누수 가드 2종(raise) · 표본 하한 · 창 검증 · C-C(gain >= 3.0%)      |
+==========================================================================+
        |                    |                     |
      FAIL                 SKIP                  PASS
        |                (C-C 미달)                |
        v                    |                     v
  ct_decisions(BLOCKED)      v          [D3 배포 정책 -- PM 확정, §5]
  challenger 보존·미배포  ct_decisions     |
  에스컬레이션            (SKIP)           +-- 배포 승인됨(자동/수동 공통 경로):
  (재요청 = 사람 발의)    champion 유지    |     control/ct1/promote_<ct_id>.json 원자 드롭
                                           |       -> [A consumer] _check_ct1_promote 스캔
                                           |       -> 로드 성공 시에만 원자 스왑
                                           |          (실패 -> failed/ 이동·기존 유지)
                                           |       -> ct_decisions(PROMOTED | AUTO_PROMOTED)
                                           |          + models/CHANGELOG.md + fdc.mlops 발행
                                           |       -> (S3) 배포 후 감시 -> 발화 시
                                           |          직전 champion 재드롭 = 롤백(ROLLED_BACK)
                                           +-- 차단기 내림(스위치식 실증 구간):
                                                 ct_decisions(SHADOW) 기록만 -- 교체는 사람 실행

  학습창 (시간축):
     [d-H-365]                                  [d-H]      [d]
        +--- 학습 365일 (완결 요란 레짐 <1 시 좌측 확장) ---+--- 평가 H일 ---+
        |    라벨 완결 wafer만 · 온셋 포함(피처가 흡수)     | 라벨 완결 +    |
        |                                                   | 온셋 제외      |
        +---------------------------------------------------+----------------+
        ^ champion·challenger 모두 학습 상한 = d-H  =>  평가창은 양쪽 다 미학습 (공정)
```

## 4. 상세 설계

### 4-1. 오케스트레이터 (D1)

- `src/agent_a_mlops/lean85/ct1_orchestrator.py` 신설. **상주 프로세스** — `run_stack.ps1`에 창 1개 추가. Kafka consumer가 아니므로 offset 규율은 없으나 **graceful shutdown(SIGINT/SIGTERM)·`logging` 사용은 6-2·6-1 그대로 준수**한다.
- 루프: `ct1_poll_sec`(제안 60초)마다 ① 정기 시각 도래 ② `lp.should_retrain()` 참(신규 요란 PM 기입) 중 하나면 실행. pm_log 감지는 기존 mtime 캐시(`parse_pm_log`)가 흡수 — 파일 감시 코드 불요.
- **멱등·중복 방지**: 실행 전 당일 `ct_id` 채번 + `ct_decisions` RUNNING 행 원자 선점(`ct2_orchestrator.claim_running` 패턴). 같은 날 이벤트+정기가 겹치면 SEQ 증가로 별건 처리하되, **동시 실행은 락 파일로 배제**(재학습 subprocess 진행 중 신규 트리거는 스킵+로그).
- 재학습·게이트는 **subprocess 격리**(`ct2_orchestrator._render_cmd` 패턴 — `sys.executable` 치환, cwd = `ct.ct1_cmd_cwd`), 타임아웃 `ct1_retrain_timeout_sec`. 명령 템플릿은 config 등재(`ct1_retrain_cmd`·`ct1_validate_cmd`) — 미등재 시 실행 금지 가드.
- 알려진 한계 승계(명시): `should_retrain`은 날짜 단위 비교라 **같은 날 2번째 요란 PM 미감지**(계약 §8-B-1 단서 ②) — 일간 정기 트리거가 익일 흡수하므로 수용. **다챔버 전역 PM 시맨틱**(ADR-0001) — 한 챔버 PM이 전 챔버 모델 재학습 유발. 일간 캐던스에서는 정기 트리거와 겹쳐 실효 피해 작음(계약 §8-B-1 :426) — 챔버 스코프 전환은 학습·서빙 동시 전환+동결 벤치 재검증이 전제인 별도 안건.

### 4-2. 학습창 절단 + 완결 요란 레짐 확장 (D2)

```python
def slice_train_window(table, now, *, window_days, require_complete_loud_regime,
                       pm_events, buffer_days):  # lean85_pipeline.py 신설
    """(now-buffer-window, now-buffer] 절단. 창 내 완결 요란 레짐(loud PM~다음 PM)
    < 1 이면 직전 완결 레짐 시작까지 좌측 확장(임시), 충족 시 window_days 복귀.
    반환: (mask, window_meta)  -- window_meta는 manifest·ct_decisions 기록용."""
```

- **완결 판정 = pm_log의 물리 PM 이벤트 쌍(loud PM_k ~ PM_{k+1})만 사용.** 모델 예측·정착 판정을 창 결정에 쓰지 않는다(순환 참조 차단 — D2).
- 확장은 **임시**다: 다음 실행에서 창 내 완결 레짐이 충족되면 자동으로 365일로 복귀. 확장 발생 여부·확장 폭은 `window_meta`로 manifest와 `ct_decisions.trigger_reason`에 남긴다.
- 워밍업(가동 초기 창 미충족): 합의안 C4 그대로 **창이 찰 때까지 누적 사용**.

### 4-3. 학습셋 조립 (D9)

- `ct1_assemble_trainset.py` 신설(시뮬 단계): 정본 raw 원천 → `build_wafer_table()` → `slice_train_window()` → 라벨 완결 필터. **라벨 없는 wafer는 학습 제외, clamp로 차단됐던 옛 라벨은 포함**(사이클_정의 결정 5 — 소정비는 레짐 연속).
- 온셋 구간(요란 PM 후 7일) wafer는 **학습에는 포함**한다 — 레짐 피처(`days_since_last_pm` 등 gain 1·2위)가 흡수하는 구조이고, B2′·실험 모두 포함 학습이 전제. 제외는 **평가에서만**(D4).
- 실운영 전환 시: raw 적재기(fdc.raw → 일자 파티션 parquet) + `wafer_predictions.actual_c65` 조인으로 교체 — 조립기 인터페이스(`--data`, `--labels`)는 그때도 불변.

### 4-4. 자동 검증 게이트 (D4·D12)

`lean85/validate_lean85.py` 신설 — **항목·임계의 단일 소스는 이 모듈 + `ct.ct1_gate_*`** (본 문서는 항목 후보만 열거, 수치 미기재 — D12).

| 군 | 항목 후보 | 성격 |
|---|---|---|
| A 완결성 | 산출물 존재(`lean85_model.json`·`manifest.json`) · 네이티브 로드 성공 · 폴더명 ≤32자 · 피처 계약(`check_feature_contract`) | critical |
| B 누수·창 | **학습셋 최대 시각 ≤ 컷오프(raise)** · **학습·평가 wafer 교집합 0(raise)** — run_backtest.py:188-209 가드 이식, `assert` 금지(7장) · 완결 요란 레짐 조항 이행 여부(window_meta 대조) | critical |
| C 표본 | 학습 wafer ≥ `ct1_gate_min_train_wafers` · 평가 wafer ≥ `ct1_gate_min_eval_wafers` (미달 = 판정 불가 → SKIP, PASS 아님) | 차단 |
| D champion–challenger | 공통 평가셋(온셋 제외·라벨 완결)에서 pooled RMSE 비교 — 개선 ≥ `ct1_promote_min_rmse_gain_pct` → PASS. 참고 지표로 전체 pooled(온셋 포함) 병기 | 승격 판정 |

- 리포트 = `<stamp>/validation.json` — `gate_source`·`gate_pass`·`failed_checks`·`checks[]`·`metrics{}` 등 **계약 §8-E 리포트 규약과 동일 키**. exit code `0=PASS / 2=FAIL / 1=실행 오류`(판정 불가를 PASS로 오해 금지 — §8-E).
- **1-3 정합**: wafer 단위 테이블에서 시간 경계로 가르므로 한 wafer가 학습·평가에 걸치지 않는다(= C64 그룹 분할 충족). 교집합 0 가드가 이를 코드로 강제한다.
- 항목 **추가·임계 강화**는 config·모듈 변경만으로 가능, **항목 삭제·완화는 PM 승인 + PR 사유 명시**(3-3③ⓑ 준용).

### 4-5. promote · 리로드 · 롤백 (D5)

- 신호: `control/ct1/promote_<ct_id>.json` = `{ct_id, model_dir(절대경로), promoted_at}` — tmp + `os.replace` 원자 교체(7장). 자동·수동(스위치식 실증 구간의 사람 실행 포함)이 **같은 신호 채널**을 쓴다 → consumer는 D3 결정과 무관하게 단일 구현.
- consumer: `_check_ct1_promote(predictor)` 신설 — `_check_ct2_promote`(consumer.py:639) 복제. `CT1_RELOAD_CHECK_EVERY` poll마다 스캔, **새 모델 로드 성공 시에만 스왑**, 실패는 `failed/` 이동 + 기존 유지, 성공은 `processed/` 이동. `_resolve_model_dir()`의 `sorted(glob)[-1]`은 **기동 부트스트랩 전용**으로 강등(운영 중 교체는 신호만).
- 롤백: 직전 champion 폴더(D8이 무조건 보존)로 **promote 신호 재드롭** + `ct_decisions(ROLLED_BACK)` + CHANGELOG. S3 전까지는 수동 롤백만 존재.

### 4-6. 기록 (D6)

| 시점 | 기록 | 비고 |
|---|---|---|
| 실행 개시 | `ct_decisions` RUNNING 선점 (ct_id·model_version_before) | 멱등 창 축소(WP-B1 선례) |
| 종결 | finalize → PROMOTED / AUTO_PROMOTED / SHADOW / SKIP / BLOCKED / FAILED + rmse_before/after · champion_challenger_result · model_version_after | 상태 어휘 = 계약 §8-E 목록(≤16자) 재사용 |
| 승격 시 | `models/CHANGELOG.md` 1행(버전·RMSE·사유·피처 수 — 헌법 3-3) + `fdc.mlops` 발행(§7-2) | SKIP 일간 기록은 ct_decisions가 정본(§7-3 해석 확인) |

## 5. ★ 배포 정책 양안 — 완전 자동 vs 스위치식 (PM 결정 요청)

> 공통 전제: 어느 안이든 **자동 검증 게이트 전 항목 PASS 없이는 배포 불가**(헌법 3-3①)이며, promote 신호·리로드·기록 경로는 동일(§4-5·4-6)이다. 두 안의 차이는 **"게이트 PASS 후 교체를 누가 실행하는가"의 개통 시점**뿐이다.

| 축 | ① 완전 자동 | ② 스위치식 (CT² 방식) |
|---|---|---|
| 동작 | 개통 첫날부터 게이트 PASS = 즉시 자동 promote | 같은 자동 코드 + **차단기 `ct.ct1_auto_promote` 기본 false**. false 구간 = PASS를 **SHADOW로 기록만**(교체는 사람이 신호 드롭으로 실행), 실증 후 **config 한 줄로 true 전환** |
| 본질 | 개통 전 실증을 요구하지 않음 | 개통 전 실증(dry-run 기간)을 요구 — "가드 걸고 허용, 위반 시 회귀"(1-1 예외 2 선례) |
| 헌법 정합 | **위헌 아님** — 3-3①은 "통과 전 배포 금지"만 규정, 1-1 승인 목록(Recipe·실력치·정비·wafer 판정)에 모델 교체 없음. 모델 출력 경로 변경 = Model R2R 자동 허용과 동일 논리(CT² D3 근거) | 동일 + **CT² 선례와 대칭**(3-3③ⓓ: 감시·롤백 실증 전 auto_promote 금지 — 단 그 조항은 CT² 전용이라 CT①을 구속하지는 않음) |
| 핵심 리스크 | **게이트가 못 거르는 결함이 무인 배포**됨. 실증 사례: AE 라벨 오염 38장이 재학습·게이트·PM 보고를 전부 통과한 뒤에야 발견(7장 "주입 정답지" 사고) — 게이트 완전성은 가정이지 보장이 아님 | 실증 기간 중 **champion 노화**(무재학습 발산 STATIC 334 · 나이 6일 +6.41 RMSE 실증) + **매일 1회 수동 promote 부담**. CT²(수개월 1회)와 달리 일간 캐던스라 수동 구간이 즉시 병목 — **실증 기간 상한(제안 1~2주) 필수** |
| 사고 대응 | 정지하려면 코드 수정·프로세스 중단 (차단기 없음) | **config 한 줄로 수동 회귀** — 대응 반경 짧음 |
| 구현량 차이 | 분기 코드 불요 | 스위치 분기 + SHADOW 상태 + 개통 조건 문서화 — **차이 소** (수십 줄) |
| 감시·롤백(S3) 의존 | S3 완성 전 개통 시 안전망 부재 — **S3 선행이 사실상 전제** | S0~S2 실증이 S3 완성 전의 안전망 역할 |

**판단 기준(중립 제안)**: ⓐ 오배포 1건의 비용 — lean85 예측은 Brief·스크랩 판정·Model R2R의 입력이라 오염 예측이 물리 판정에 간접 파급된다. ⓑ 실증 기간의 노화 비용 — 1~2주 상한이면 열화는 D7 수준(+3.87~22.66%) 이내로 제한. ⓒ 감시·자동 롤백(WP-7) 완성 시점과의 선후. ⓓ **절충 가능**: ①을 채택하더라도 차단기 키 자체는 두는 것(비상 정지용)이 안전하며, 그 경우 양안의 차이는 "기본값 true/false + 개통 실증 요구 유무"로 좁혀진다.

**결정 기록란** *(PM 확정)*:

| 채택안 | 결정일 | 결정자 | 개통 조건(②일 때) |
|---|---|---|---|
| **① 완전 자동** (ⓓ 절충 적용 — 차단기 키 `ct.ct1_auto_promote` 는 **비상 정지용으로 존치**, 기본 `true`) | 2026-08-04 | PM/PO | 해당 없음 (②안 미채택) |

**①안 채택에 따른 후속 의무** *(설계서 §5 리스크 열의 이행)*:

1. **S3(WP-7) 선행 권고** — ①안은 "게이트가 못 거르는 결함이 무인 배포"되는 리스크를 받는 선택이다(실증: AE 라벨 오염 38장이 재학습·게이트·PM 보고를 전부 통과). 배포 후 감시·자동 롤백(`ct1_deploy_monitor.py`)을 S2 전에 완성할 것을 권고한다. 그전까지의 안전망은 ⓐ 게이트 critical 항목 ⓑ `ct_decisions` 3중 기록 ⓒ 차단기 수동 강하다.
2. **차단기 존치의 의미** — `ct1_auto_promote: false` 는 개통 절차가 아니라 **사고 시 회귀 스위치**다. 내리면 게이트 PASS 가 `SHADOW` 로 기록만 되고 교체는 사람이 promote 신호를 드롭해 수행한다(자동·수동 동일 채널이라 consumer 무변경).
3. **미탐 1건 회귀 규율 승계** — 시뮬 Scorecard 프로브 미탐이 1건이라도 나오면 즉시 차단기를 내리고 원인 규명 전 재전환을 금지한다 (헌법 1-1 예외 2·3-3③ⓓ의 "가드 걸고 허용, 위반 시 회귀" 선례).

## 5-1. 배선 구현 현황 *(2026-08-04 — WP-1~5 구현 완료)*

| WP | 산출물 | 비고 |
|---|---|---|
| WP-1 | `lean85_pipeline.slice_train_window()` · `parse_pm_entries_full()` · `complete_loud_regimes()` · `wafer_times()` · `assert_window_no_leakage()` · `assert_disjoint_wafers()` · `ct1_config()` · `ct1_window_mismatch()` · `retrain()` 32자 가드 + `window_meta` manifest 적재 | 누수 가드는 전부 명시적 `raise` (헌법 7장 — `assert` 금지) |
| WP-2 | `ct1_assemble_trainset.py` (exit 0/2/1) | 온셋 제외는 **게이트가** 수행 — 판정식 단일 소스 유지(`low_confidence_flags`) |
| WP-3 | `validate_lean85.py` (exit 0=PASS/2=FAIL/**3=SKIP**/1=오류) | CT² 0/2/1 규약에 SKIP 추가 — CT² 엔 champion 비교가 없어 SKIP 자체가 없다. **critical 실패만 FAIL**, 표본 미달·승격 기준 미달은 SKIP |
| WP-4 | `ct1_orchestrator.py` (`--once` 지원) + `control/ct1/champion.json` 포인터 | g1 dry-run · g2 락 · g3 RUNNING 선점 · g4 명령 미등재. 이벤트 트리거는 상태 파일로 디듀프(승격 실패 시 60초마다 재발화 방지) |
| WP-5 | `consumer._check_ct1_promote()` + `_resolve_model_dir()` 포인터 우선 | `sorted(glob)[-1]`(G6)은 부트스트랩 폴백으로 강등 |
| WP-6(부분) | `params.yaml` `ct.ct1_*` 15키 신설 + `lean85.retrain_window_extend_*` "240일" 주석 정정(G10) | `ct_type` `ct1_lgbm`→`ct1_xgb`(§7-1)는 **DB·계약 PR 별건**(PM 승인 경유) — 코드는 이미 `ct1_xgb` 로 기록한다 |

**미구현(후속)**: WP-7(배포 후 감시·자동 롤백 — S3) · WP-8(리허설 H·임계 스윕) · §7-1 스키마/계약 PR · D9 실운영 raw 적재기.

### 5-2. 배선 리뷰 반영 *(2026-08-04, 구현 직후 코드 리뷰)*

설계에는 없던(구현 단계에서만 드러나는) 사고 4건을 잡아 고쳤다. 전부 회귀 테스트로 고정했다.

| # | 증상 | 원인 | 조치 |
|---|---|---|---|
| B1 | **자동 경로가 100% FAILED** — 수동 `--once --now <naive>` 만 통과해 리허설을 그대로 넘어간다 | 오케스트레이터는 aware UTC(`datetime.now(timezone.utc)`)로 도는데 wafer 시각(`C40`)·pm_log 는 naive → pandas 비교가 `TypeError`. 헌법 7장 등재 사고(`pm_log` aware 기입)와 같은 계열 | `lp.naive_utc()` 신설 + 경계 전부 정규화(`_now()`·조립기·`slice_train_window`·`complete_loud_regimes`). 테스트 `test_aware_now_does_not_raise` 외 2건 |
| B2 | **champion–challenger 비교 없이 자동 배포** (헌법 3-3 ① 우회로) | challenger 생성 **뒤에** `sorted(glob)[-1]` 로 champion 을 뽑으면 사전순=시간순이라 challenger 자신이 뽑히고, 게이트가 "champion 부재(최초 학습) → 무조건 승격"으로 읽는다. 실물 champion 이 디스크에 있어도 그렇다 | champion 을 **재학습 전에** 확정해 게이트에 명시 전달(`--champion none` = 호출자가 부트스트랩을 단언). `champion_dir(exclude=)`·`resolve_champion(exclude=)` 로 자기 자신 배제. `auto` 해석 실패 + 스토어 비어있지 않음 = **SKIP**(승격 아님). 테스트 5건 |
| B3 | **기존 champion(`lean85_20260720_163040_initial`, 99.84 벤치 근거)이 GC 로 소실** | "`validation.json` 에 PASS 가 없으면 미승격"이라는 **부정 판정** — 게이트 이전에 손으로 만든 모델이 전부 여기 걸린다. `ignore_errors=True` 라 실패도 안 보였다 | 삭제는 **긍정 증거**로만: tag ∈ `CT1_TAGS` + manifest 에 `ct1_window_meta` + 게이트 리포트 존재 + PASS 아님. 추가로 `ct_decisions` 대조(D8), `ignore_errors` 제거. 테스트 4건 |
| B4 | config 로드 실패 시 **자동 배포가 켜진다** | 코드 기본값이 `True` 라 `_params()` 의 "실패 시 전 가드 잠금" 계약과 모순 | 코드 기본값 `False`(params.yaml 값은 D3 ①안대로 `true` 유지) |

주요 SHOULD-FIX: 서빙 경계 게이트 재확인(consumer 가 `validation.json` PASS·`bundle_name` 대조 없이는 리로드 거부 — CT² 3-3③ⓐ 대칭) · promote 신호 다중 대기 시 **최신만** 적용(롤백이 큐잉된 구 신호에 즉시 뒤집히는 문제) · `claim_running` 을 `INSERT … RETURNING` 으로 원자화 · DB 장애 시 그날 정기분을 소비하지 않음 · `--once` 가 BLOCKED 에 비영(非零) 종료 코드 · 게이트가 학습셋 CSV 를 직접 읽어 **교집합 0 을 독립 검증**(B2b) + `features_sha1` 대조 · `B3_요란레짐_조항` 을 실질 판정으로 교체(구 판정식은 항진명제였다) · 조립 산출물 gzip + 보존 정리 · 0표본 SKIP 을 일반 SKIP 과 분리해 에스컬레이션(`LEAN85_SIM_NOW` 로 압축시간 정합).

## 6. config 신설·정리 (`config/params.yaml` `ct:` 절)

기존 4키(`ct1_trigger`·`ct1_window_days`·`ct1_require_complete_loud_regime`·`ct1_promote_min_rmse_gain_pct`) 유지 + 신설 제안(값은 제안치 — 근거는 `docs/config_파라미터_합의안_v1.md` 증보로 확정):

```yaml
ct:
  ct1_auto_generate: false        # S1 개통 전 dry-run (CT2 대칭). 개통 시 true
  ct1_auto_promote: false         # D3 확정 대상 — ①안 채택 시 true(또는 키 유지·기본 true)
  ct1_run_at_utc: "02:00"         # 일간 정기 실행 시각 (naive UTC — §8-B-1 시각 규율 정합)
  ct1_poll_sec: 60                # 트리거 폴링 주기
  ct1_gate_eval_days: 1           # D4 공통 버퍼 H — 리허설 스윕 대상 (1~7)
  ct1_gate_min_train_wafers: 1000 # 실험 하네스 하한(100)의 운영 상향 제안 — 스윕 대상
  ct1_gate_min_eval_wafers: 100   # 평가 표본 하한 — 미달 시 SKIP
  ct1_retrain_timeout_sec: 3600
  ct1_retry_backoff_sec: 30
  ct1_gc_keep_unpromoted_days: 14 # D8 미승격 challenger 보존 기간
  ct1_max_stale_champion_days: 7  # D10 연속 미승격 에스컬레이션 (나이6=+6.41 실증)
  ct1_cmd_cwd: "src/agent_a_mlops/lean85"
  ct1_retrain_cmd: "..."          # 명령 템플릿 — 미등재 시 실행 금지 가드
  ct1_validate_cmd: "..."
```

**정리(모순 해소 — WP-6)**: 창·주기의 **정본 = `ct.ct1_*`**. `lean85.retrain_days`·`retrain_window_days`·`retrain_window_extend_until_high_regime`(:216-221)은 ct 절 참조 주석으로 이관하고, **:221의 "240일 복귀" 주석을 365로 정정**(ct 절·합의안 C4와 모순 — G10). 오케스트레이터는 기동 시 두 절 잔존 값 불일치를 검사해 경고한다.

## 7. DB·계약 반영

### 7-1. 스키마·값 (PM 승인 필요 — 3-2·2-2)

| 항목 | 제안 | 근거 |
|---|---|---|
| `ct_decisions.ct_type` | `'ct1_lgbm'` → **`'ct1_xgb'` 개정** (init.sql §11 주석 + 계약 §7 동시 PR) | XGBoost 확정(2026-07-22). **기존 ct1 행 0건 실측** — 지금이 무비용 개정의 마지막 시점 |
| `approval_records` | **CT①은 미사용 제안** — 승인 요청 자체가 없는 경로(1-1 승인 목록에 모델 교체 없음). 감사 정본 = ct_decisions + CHANGELOG + fdc.mlops | CT²의 approval_records 기록은 '존치됐던 배포 승인'의 자동 대체 흔적(1-1 예외 3)이라 CT①에 대응물 없음. **유추 적용해 기록을 남길지는 PM 확인 1건**(§7-3) — 남긴다면 `incident_id NOT NULL` 제약과 충돌(정기 배치는 Incident 무관) |
| `ct_decisions` 인덱스 | `idx_ct_decisions_created (ct_type, created_at)` 추가 검토(선택) — 일간 누적 대비 | 현재 인덱스 0개 |

### 7-2. 계약 §7 `fdc.mlops` CT① 페이로드 (공백 채움 — 계약 PR에서 확정)

```json
{"event_type": "CtDecision", "ct_id": "CT-20260804-ALL-001-XGB", "ct_type": "ct1_xgb",
 "trigger_reason": "daily_sliding | loud_pm_event", "retrain_status": "PROMOTED",
 "model_version_before": "lean85_20260803_020000_daily",
 "model_version_after": "lean85_20260804_020000_daily",
 "rmse_before": 0.0, "rmse_after": 0.0, "champion_challenger_result": "PROMOTED | SKIP",
 "window_meta": {"window_days": 365, "extended": false}, "created_at": "..."}
```

필드는 `ct_decisions` 컬럼과 1:1(snake_case — 6-4). NaN/Inf 발행 금지 + `allow_nan=False`(7장).

### 7-3. 헌법·규약 해석 확인 (PM — 리뷰에서 확정)

1. **CHANGELOG 기록 밀도**: 3-3 "재학습 시 반드시 기록"을 일간 캐던스에 그대로 적용하면 연 365행(대부분 SKIP)이 된다. **해석 제안: 일간 생성 이력의 정본 = ct_decisions, CHANGELOG = 승격(운영 모델 교체) 건만** — 실험설계 :84 "운영 이력 오염 방지" 논리의 연장.
2. **approval_records 유추 적용 여부** (§7-1).
3. **D8 GC와 4-1 "덮어쓰기 금지" 해석**: 승격 계보 영구 보존 + 미승격 한시 보존이 4-1의 취지(버전 이력 보존)를 충족하는지.

## 8. 구현 WP 분해 (전부 A 소유 — WP-6만 PM 승인 경유)

| WP | 내용 | 산출물 | 선행 |
|---|---|---|---|
| WP-1 | 학습창 절단 + 완결 요란 레짐 확장 + 단위 테스트(누수 가드 포함) | `lean85_pipeline.slice_train_window()` + tests | — |
| WP-2 | 학습셋 조립기(원천 절단·라벨 정책) | `ct1_assemble_trainset.py` | WP-1 |
| WP-3 | 자동 검증 게이트 | `validate_lean85.py` + `ct.ct1_gate_*` | WP-1 |
| WP-4 | 오케스트레이터(트리거·멱등·subprocess·기록·GC) | `ct1_orchestrator.py` + run_stack.ps1 1행 | WP-2·3 |
| WP-5 | consumer 관리형 리로드 + promote 신호 규약 | `consumer._check_ct1_promote()` + `control/ct1/` | — |
| WP-6 | config 정리(G10)·ct_type 개정·계약 §7 페이로드·§7-3 해석 확정 | params.yaml·init.sql 주석·계약 문서 PR (**PM 승인**) | — |
| WP-7 | 배포 후 감시·자동 롤백 (S3) | `ct1_deploy_monitor.py`(ct2 대칭) | S2 |
| WP-8 | 리허설 — dry-run 연속 가동 + H·임계·표본 하한 스윕(실험 하네스 재사용) | 리허설 리포트 | WP-4 |

## 9. 개통 단계 (D11)

| 단계 | 내용 | 스위치 상태 |
|---|---|---|
| S0 (현재) | 설계 확정·배선 구현·수동 실행 검증 | `ct1_auto_generate: false` |
| S1 | **생성 개통** — 일간 자동 생성+게이트+기록 가동. 배포는 D3 채택안 이전까지 수동 신호만 | `ct1_auto_generate: true` |
| S2 | **배포 개통** — ①안: S1과 동시 / ②안: 실증(제안 1~2주, 오배포·오판정 0건) 후 전환 | `ct1_auto_promote: true` |
| S3 | 배포 후 감시·자동 롤백 (WP-7) — ①안 채택 시 S2 전 완성 권고 | `ct1_watch_*` |

## 10. 잔여 확인·정직한 한계

1. **실증의 조건부성** — 일간 우위 +22.66%는 이음새 7일 포함 값, 제외 시 **+3.87%**(결과서가 병기 의무 명령). 승격 임계 3.0%는 이 3.87%와 근접 — **게이트 실효성(승격 빈도·flapping)은 WP-8 리허설에서 H·임계 스윕으로 확인**해야 한다. 승격 임계·게이트 판정은 실험에서 **사후 소급 시뮬**로만 검증됐고 운영 프로토콜(D4)로는 미실측.
2. **라벨 지연 미검증** — 시뮬은 즉시 라벨 전제. 실운영의 계측 지연(라벨 완결 시점)이 H·평가 표본에 미치는 영향 미측정.
3. **데이터 원천 이원화** — 시뮬(CSV 절단) → 실운영(raw 적재기+`wafer_predictions` 조인) 전환은 후속 과제로 남는다(D9). Kafka 되감기는 365일 창에 부적합.
4. **다챔버 전역 PM**(ADR-0001) — 단일 전역 모델 전제 유지. 챔버 스코프 전환은 학습·서빙 동시 전환+동결 벤치(99.84±0.5) 재검증이 전제인 별도 안건.
5. **CT²와의 자원 충돌** — 요란 Incident 직후 CT②(최대 2.5h)와 CT① 일간 실행이 겹칠 수 있음. 순차화(공유 락)는 미설계 — 저빈도라 후속.
6. **`should_retrain` 같은 날 2번째 요란 PM 미감지**(계약 §8-B-1 단서 ②) — 압축시간 데모 한정 이슈, 수용.
7. **D4 공통 버퍼의 전제** — "champion도 같은 규율로 학습했다"는 공정성은 **모든 승격 모델이 H 버퍼를 지켰을 때만** 성립. 초기 champion(`lean85_20260720_163040_initial`)은 이 규율 이전 모델이므로 첫 비교들은 champion 약간 유리/불리가 섞일 수 있음 — 리허설에서 확인.
8. **헌법 정합 자체 점검**: 1-1(승인 불요 경로 — 물리 조치 아님·§7-3 확인 1건) · 1-2(무관 — fdc.alert 우회 아님, 3-3이 규정하는 독립 캐던스) · 1-3(누수 가드 2종 raise + C64 그룹 분할 충족 — §4-4) · 3-3(캐던스·게이트 선행·기록 — 본 설계 전체) · 4-1(stamp 신규 저장·경로 config 경유·CHANGELOG) · 4-3(WP-6 동시 PR) · 6-1/6-2/6-4(logging·shutdown·snake_case·ID 포맷) · 7장(assert 금지·원자 교체·32자 가드·NaN 금지·상태 확인 가드).

---

*본 문서는 lean85 패키지 산출물이며, 승인·게이트 인프라 경계(1-4)를 건드리지 않는다 — CT①에는 승인 게이트가 없고(§7-1), Supervisor·Incident 경로와 독립이다.*
