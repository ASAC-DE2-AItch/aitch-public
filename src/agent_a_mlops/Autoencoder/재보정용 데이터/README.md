# 재보정용 데이터 (2026-08-02) — AE post-reset 재보정·재학습 입력

> ⚠️ **원자료 CSV 2종은 git 미추적**입니다 (`.gitignore` — 21MB). seed 42 고정이라
> 재생성 가능하며, 절차는 §재생성 참조. **파생 산출물(정답지·라벨·목록·리포트)은 추적**합니다.

## 파일

| 파일 | 추적 | 내용 |
|---|:--:|---|
| `ae_input_sim_v2.csv` | ✗ | 시뮬 발행값 재현 (원본 replay + 챔버 오프셋/노이즈 + **주입 2건**) · 2,000장 |
| `ae_input_control_v2.csv` | ✗ | 같은 wafer의 원본 replay값 (오프셋·주입 없음) — 대조군 |
| `dump_events.jsonl` | ✓ | **주입 정답지** (아래) |
| `injection_labels.csv` | ✓ | 전 wafer 라벨 — wafer·chamber·pos·injected·scenario_id·k·expected_sigma |
| `wafers_clean.txt` | ✓ | 주입 제외 정상 1,254장 — `retrain --wafers-file` 입력 |
| `diag_ae_recalib_report.md` | ✓ | 재보정 수준 진단 (레짐 불일치 판정) |
| `fa_decomposition_report.md` · `fa_holdout_scores_*.csv` | ✓ | 오경보 분해 |
| `fa_feature_trend_prelim_2026-08-02.md` | ✓ | 피처 추세 예비 분석 (결론 일부 폐기 — 문서 상단 참조) |
| `FA_판정_PM브리핑_2026-08-02.md` | ✓ | PM 보고 정본 |

## ★ 주입 2건 — `C6_0` 필터로는 안 걸러진다

| 시나리오 | 챔버 | 센서 | 패턴 | 크기 | 시작 | 대상 |
|---|---|---|---|---|---|---|
| `SC4-STORM-CH2` | SIM_CH_2 | C11·C17·C62 | shift | 4.0σ | 101장~ | 400장 |
| `SC1-DRIFT-C11` | SIM_CH_3 | C11 | drift | 2.5σ | 201장~ | 300장 |

**주입은 센서값만 흔들고 레시피 라벨(`C6`)은 바꾸지 않습니다.** 2026-08-02 재학습에서
정상 필터가 `C6_0` 하나뿐이라 주입 653장이 학습에 섞여 후보 3종이 전량 무효 처리됐습니다
(`models/CHANGELOG.md` 정정 ②). **학습·보정에 쓸 때는 반드시 `wafers_clean.txt`를
경유**하세요.

σ 자 주의: 크기는 **wafer 간 σ**(C11 16.9 · C17 3.0 · C62 209.4) 기준입니다. 전역 row σ
(C11 140.5)로 재면 4σ 주입이 0.4σ로 보여 "주입 실패"로 오판합니다.

## 재생성

원자료 CSV는 시뮬레이터에서 다시 뽑습니다 (seed 42 — 값 동일 복원). `scripts/dump_ae_input.py`
현행본은 **주입을 적용하지 않으므로**, v2와 동일한 세트를 만들려면 위 표의 시나리오 2건을
장전한 상태로 덤프해야 합니다 (`src/simulator/scenario_injector.py` · `control/inject/`
파일드롭). 재생성 후에는 반드시 정답지 대조를 다시 통과시키세요:

```
python scripts/build_clean_wafer_list.py          # 정답지 검증 + 목록·라벨 재생성
```

이 스크립트는 정답지를 그대로 믿지 않고 **control 쌍 대조로 실측 검증**합니다 — 대상 챔버의
증분(≥0.5σ)과 비대상 챔버 무흔적을 확인하며, 어긋나면 중단합니다(다른 실행의 정답지 차단).

## 파생 산출 명령

```
python scripts/diagnose_ae_recalib.py       # 재보정 수준 진단 (torch — foreground, TSR-0001)
python scripts/analyze_ae_false_alarms.py   # 오경보 분해 (torch)
python scripts/build_clean_wafer_list.py    # 정상 목록·라벨 (torch 불필요)
```
