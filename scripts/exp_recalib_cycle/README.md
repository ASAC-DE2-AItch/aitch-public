# exp_recalib_cycle — 예측모델 재보정 주기 실험 하네스

> 설계 정본: `src/agent_a_mlops/lean85/예측모델_재보정주기_실험설계_v1.md` (일간 D1 vs 주간 D7 vs STATIC)
> 성격: **오프라인 백테스트**. 운영 CT 경로와 무관하며 산출 모델은 이 디렉토리에만 남는다
> — `models/` 등재·`CHANGELOG.md` 기록 대상이 아니다 (설계서 §4, 헌법 3-3 은 운영 재학습 한정).

## 실행

```bat
run_smoke.bat         :: 20 평가일 스모크 (더블클릭 가능 · 수 분)
run_all.bat           :: 전체 (295 fit)
run_sensitivity.bat   :: §5 경계완충 + §6 seed 43·44 민감도 (전체 런의 약 3배)
```

venv(`AITCH\venv`)를 직접 쓴다. 진행 로그는 `out\run_all.log`.
xgboost 없이도 순수 로직만 따로 검증할 수 있다: `python test_harness.py`.

## 파이프라인

| 단계 | 스크립트 | 하는 일 | 산출 |
|---|---|---|---|
| 1 | `prep_data.py` | 데이터 계약 검증(§2) + 웨이퍼 테이블 1회 고정 생성 | `out/wafer_table_1y.parquet` · `wafer_day_map.csv` · `pm_log_exp.json` · `prep_report.json` |
| 2 | `verify_leakage.py` | 누수 차단 체크리스트 5항목(§5, 헌법 1-3) — **실패 시 백테스트 진입 차단** | `out/leakage_report.json` |
| 3 | `run_backtest.py` | rolling-origin 백테스트(§4) + §8-A 게이트 판정 인라인 | `out/run_<tag>/predictions.parquet` · `probe_preds.parquet` · `fits.csv` · `gate_sim.csv` · `arm_assignment.csv` · `run_manifest.json` |
| 4 | `analyze.py` | 지표 M1~M6(§6) · 통계 · 사전등록 판정(§7) · 부속 §8-A 요약 · 그래프 4종 | `out/run_<tag>/metrics.json` · `daily_rmse.csv` · `fig_*.png` |
| 5 | `make_report.py` | metrics.json → 결과 보고서 초안 (수치 전사 없음) | `src/agent_a_mlops/lean85/예측모델_재보정주기_실험결과_v1.md` |

실험 상수는 전부 `CONFIG.py` (헌법 6-1 매직 넘버 금지). `✱` 표시 임계는 설계서 §7 제안값 —
PM·멘토 확정 대상이라 확정되면 `CONFIG.py` 한 곳만 고치면 된다.

## 설계서 대비 정정·구현 결정 (리뷰 대상)

### ① 시간축 — 균등 근사 폐기, C40 실측 시각 사용 ★
설계서 §2 는 "데이터에 타임스탬프 컬럼이 없다 → 월내 균등 배치로 근사"를 전제했으나,
**`C40` 이 실제 웨이퍼 타임스탬프**다 (`lean85_pipeline.TIME_COL = "C40"` — 운영 피처 빌드가
이미 이 컬럼으로 `wf_ts`·`hour`·`days_since_last_pm` 을 만든다). 전 구간 834,036행에서
결측 0 · 포맷 이탈 0 을 확인했다. 따라서 근사를 쓰지 않고 실측 시각으로 매핑한다.
→ 설계서 §9 "매핑 근사" 위협은 **해소**되며, "3군이 동일 매핑을 공유" 성질은 그대로다.

### ② §2 확인 항목 답
| 확인 항목 | 결과 |
|---|---|
| README 68일/297일 vs 달력 70일/295일 | **생산일 기준이 정본**. 실측 = 생산일 68 / 달력 70 (README 68 = 생산일). 합성 = 2019-02-09~2019-12-01, 생산일 296 / 달력 296. 평가창(~11-30) = 295일 전부 생산일, 빈 날 0 |
| `events_ledger` 인덱스가 합성 S-인덱스인가 | **그렇다**. ledger `wafer_end` 최대 68,106 ≤ S-인덱스 최대 68,354 |
| 생성기 재실행으로 매핑 복원 | **불필요** — ①에 따라 실측 시각을 그대로 쓴다 (설계서 "복원 > 근사" 우선순위의 상위 해답) |

부수 사실: 합성 웨이퍼 6장이 2019-12-01 로 넘어간다. 평가창 상한(11-30)에서 자연 제외되며
`EVAL_END` 로 통제된다. 평가 대상 = **68,349 wafer / 295일**.

### ③ 재학습 338회 → 고유 fit 295회 (계산 등가)
세 군 모두 "평가일 d 의 모델 = 컷오프(d-1) 학습본"이라는 같은 규칙을 쓰고, 다른 것은
*어느 d 에서 교체하느냐*뿐이다. 그래서 D7 의 컷오프 43개와 STATIC 의 M0 는 **D1 컷오프 295개의
부분집합**이고, 295회만 학습하면 3군을 모두 재현한다. 군 배정(`arm_assignment.csv`)은 학습과
분리된 사후 선택이라 등가성이 보존된다 — `test_harness.py::t_assignment` 가 이 포함관계를 검증한다.
명목 횟수(D1 295 / D7 43)는 §6 M6 비용 산정에 그대로 쓴다.

### ④ pm_log — 전체본 1회 빌드 = 절단본 공급과 등가
`_meta_features` 의 PM 탐색은 `searchsorted(side="right")-1` 후향 탐색이라 **웨이퍼 자기 시각
이전 PM 만** 본다. 그러므로 컷오프에서 pm_log 를 절단하든 전체를 주든 그 시점 이전 웨이퍼의
메타 피처는 동일하다 → 테이블을 1회만 빌드해도 설계서 §5 "절단본 공급"과 등가.
이 등가성은 주장으로 두지 않고 `verify_leakage.py` 의 check 4 에서 **직접 계산해 실증**한다
(불일치 1건이라도 나오면 exit 1).

### ⑤ 실험용 pm_log = 요란(major) PM 만
운영 규약(`pm_log.json`, README §재학습 SOP)대로 요란 PM 만 기입한다:
`2018-12-24`(실측) + `2019-04-22`·`2019-08-20`(합성 major). minor 9회는 피처에서 빠지고
§6 M2-ⓑ 구간 분해에만 쓴다. 시각은 **naive UTC `%Y-%m-%d %H:%M:%S` 고정**
(헌법 7장 — aware/naive 혼입 시 `_parse_pm_entries` 의 `sorted` 가 TypeError 로 죽는다).

### ⑥ §8-A 게이트 시뮬은 백테스트 루프 **안에서** 판정한다
챔피언–챌린저 비교는 **같은 웨이퍼 집합**에서 두 모델을 돌려야 한다. 그런데 챔피언은 승격
시점 이후 임의로 오래될 수 있어(승격이 드물수록 더 오래) 저장된 예측 기록만으로는 재구성이
불가능하다 — 사후에 하려 하면 "챔피언과 챌린저가 서로 겹치는 날이 없어 판정 자체가 스킵되고
승격이 구조적으로 최대 1회"인 가짜 결과가 나온다. 그래서 `run_backtest.py` 가 군별 챔피언
모델을 메모리에 들고 매 트리거마다 판정해 `gate_sim.csv` 에 남기고, `analyze.py` 는 그것을
요약만 한다. 평가 셋 = **챌린저가 담당할 날의 웨이퍼**로, 두 컷오프 모두 그 날짜 이전이라
**챔피언·챌린저 양쪽 모두 out-of-sample** 이다. 사후 소급 판정(§8-A "소급 적용")이며 주
분석에는 영향이 없다.

### ⑦ ARM-D7 그리드 이월
7일 그리드 날짜에 생산이 없으면 다음 생산일로 이월한다. 예측·평가는 웨이퍼가 있는 날에만
일어나므로 이월해야 "7일마다 교체"의 의미가 보존된다. 본 데이터는 평가창에 빈 날이 0개라
실제로는 발동하지 않지만, 민감도·재사용을 위해 구현해 두고 테스트한다.

## 실행 이력

| 날짜 | 런 | 결과 |
|---|---|---|
| 2026-08-03 | `main` (seed 42, 완충 0) | **R1** — pooled RMSE D1 54.80 / D7 70.85 / STATIC 334.24. 워밍업 제외 시 개선폭 22.66% → 3.87% |
| 2026-08-03 | `buf1`·`seed43`·`seed44` | 4런 전부 R1, 워밍업 제외 개선폭 3.62~4.07% → **판정 안정** (`out/sensitivity_summary.json`) |

실행 중 발견해 설계서 §9(v1.2)에 추가한 한계 2건: **M3 요일 완전 교락**(→ M3-adj 병기로 대응),
**R4 전제 이탈**(STATIC 발산의 정체가 점진 열화가 아니라 레짐 고착).

재현: `run_all.bat` → `run_sensitivity.bat`. 지표만 다시 뽑으려면 `run_reanalyze.bat`
(재학습 없이 수 초). 원자료 `Data/synthetic_1y/` 는 exclude 대상이라 리포에 없다 — seed 42 로
재생성한다.

## 대용량 산출물

`out/` 은 재생성 가능한 중간·대용량 산출물이다. `CLAUDE.local.md` 규칙에 따라
`.gitignore` 가 아니라 **`.git/info/exclude`** 에 등록한다:

```powershell
Add-Content -Path .git\info\exclude -Value @"
scripts/exp_recalib_cycle/out/**
!scripts/exp_recalib_cycle/out/
!scripts/exp_recalib_cycle/out/*.json
!scripts/exp_recalib_cycle/out/run_main/
!scripts/exp_recalib_cycle/out/run_main/metrics.json
!scripts/exp_recalib_cycle/out/run_main/run_manifest.json
!scripts/exp_recalib_cycle/out/run_main/fig_*.png
Data/synthetic_1y/
"@
```

추적 대상은 코드·`README.md`·결과 보고서(`src/agent_a_mlops/lean85/예측모델_재보정주기_실험결과_v1.md`)와
**소량 감사 산출물**이다: `prep_report.json`(§2 검증)·`leakage_report.json`(헌법 1-3 준수 증빙)·
`sensitivity_summary.json`(부속 런 판정)·`run_main/metrics.json`(결과 정본)·`run_manifest.json`(재현 메타)·
`fig_*.png`(보고서 그림). 합계 약 210KB.

제외: parquet 3종(12MB)·`wafer_day_map.csv`(2.2MB)·`daily_rmse.csv`·`gate_sim.csv`·
`arm_assignment.csv`·부속 런 폴더·스모크 런. 전부 `run_reanalyze.bat` 로 재생성된다.

## 한계 (해석 시 필수 — 설계서 §9)

이벤트 16건과 aging 은 **센서 층만** 주입돼 있고 y 커플링이 없다. 따라서 여기서 측정되는
재학습 이득은 주로 "X 분포 이동 적응 + PM 주변 y 패턴"이며, 실제 팹의 y 드리프트 적응분은
**과소 반영**된다. 결과는 "합성 가정 하 하한 추정"으로 읽어야 하고, R4(ARM-STATIC 이 D7 과
유의차 없음)는 그 과소 반영이 판정을 무의미하게 만들었는지 잡아내는 안전장치다.
