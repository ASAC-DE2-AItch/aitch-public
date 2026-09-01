# handoff_lean85 README — lean-85 최종 운영 패키지 (살아있는 문서)

> 패키지 v1.0 · 제작 2026-07-19 · **확정 모델 = lean-85 + XGBoost(동결 파라미터) + 정기 재학습**
> 스냅샷 근거: `REPORT/handoff_lean85_REPORT_01_lean85_확정.md` (고정)
> 전달 규약: 미러 미검증본(v14 방식) — 정적 검토만 거침. 첫 실행 오류 시 메시지 → 즉시 수정.

---

## 1. 모델 카드

| 항목 | 내용 |
|---|---|
| 피처 | **lean-85** = Conservative-GA(99) − 시간불안정 14 (v13 REPORT_12) · 필수 5센서 floor 충족 {C17:2, C11:5, C31:3, C15:3, C16:1} |
| 학습기 | XGBoost `reg:squarederror` · v13 M5 튜닝 파라미터 동결 · 246 rounds · seed 42 · hist/cpu |
| 운영 전제 | **정기 재학습 필수** — 무재학습은 배포 불가(pooled 254.9, 단조 발산) |
| 현업 인용 수치 | **시간축(미래 1주, 재학습) pooled RMSE 99.84 · honest R² 0.8545** |
| same-era 수치 | lot-CV(GKF C20) stable 66.83 / seed_mean 66.956 — 현업 인용 금지 (§6) |
| 신뢰 플래그 | 요란 PM 후 7일(레짐-온셋) = `low_confidence=1` — 참고용 예측 |

## 2. 패키지 구성

```
handoff_lean85/
├─ handoff_lean85.ipynb            메인 노트북: 재현검증(A1/A2)→최종학습→예측→SOP→수용검사
├─ lean85_pipeline.py              단일 소스 모듈 (피처빌드·학습·예측·재학습·트리거·PSI·수용검사)
├─ retrain_lean85.py               재학습 CLI  (운영 진입점)
├─ predict_lean85.py               예측 CLI    (low_confidence 포함)
├─ frozen/                         동결 스펙 (수정 금지)
│   ├─ lean85_features.json        85피처 + 제거된 시간불안정 14 목록
│   ├─ tuned_params_xgb.json       XGB 튜닝 파라미터 + 246r
│   └─ pm_log_snapshot.json        pm_log 스냅샷(2026-07-19) — 운영은 루트 최신본 사용
├─ models/                         재학습 버전 폴더 누적 (lean85_<stamp>_<tag>/)
│   └─ .../lean85_model.json + manifest.json (+ preds, acceptance)
└─ REPORT/                         스냅샷 리포트 (고정)
```

외부 의존(프로젝트 내): `../문제1(하)/train_data.csv` · 루트 `pm_log.json` · (동결 대조용) `modeling_v13/colab_GA/core10_meta_wf.csv`, `modeling_v13/data/v13_fdc_pool_wf_oof.csv.gz`

## 3. 빠른 시작

로컬 venv 활성화 후 `handoff_lean85/` 에서:

1. **최초 인수**: `handoff_lean85.ipynb` 를 셀 순서대로 실행 — 셀2에서 피처 재현이 동결본과 일치(A1/A2)하는지 확인 후 셀3이 최초 버전을 `models/`에 저장. 셀6 `RUN_ACCEPT=True`로 B2′ 수용검사(99.840±0.5) 1회 권장.
2. **이후 운영**은 CLI만으로 충분:

```bash
python retrain_lean85.py --tag weekly                     # 재학습 (기본 경로 자동 탐색)
python predict_lean85.py --model models/lean85_<stamp>_weekly --data <신규_X.csv>
```

## 4. 재학습 SOP

트리거(하나라도 해당 시 실행):

| 트리거 | 기준 | 근거 |
|---|---|---|
| 정기 | 마지막 재학습 후 **7일** | B2′ 주간 재학습 프로토콜 그대로 |
| 이벤트 | **요란 PM 기입 즉시** | 레짐 시프트 원인 = 요란 PM (v13 R11) |
| 수시 | PSI ≥ 0.25 경보 · 주간 RMSE 급등 | 입력/성능 드리프트 감지 |

절차: ① `pm_log.json` 최신화(요란 PM만 기입 — 조용 PM은 미기입, C33이 담당) → ② `retrain_lean85.py --data <누적 raw 전체> --pm-log <최신>` → ③ 새 버전 폴더 확인, predict `--model` 교체 → ④ **`models/CHANGELOG.md` 기록**(버전·RMSE·사유 — 헌법 3-3, CLI가 종료 시 리마인드를 띄운다) → ⑤ 구버전 보존(롤백 대비). 환경/코드 변경 시엔 `--acceptance`로 B2′ 재현(99.840±0.5, R6: 공식=로컬 venv) 확인. 트리거 판정은 `lp.should_retrain(manifest, pm_log)` 로 자동화 가능.

> `lp.retrain(..., return_table=True)` 를 쓰면 빌드한 웨이퍼 테이블을 함께 돌려받는다 — 수용검사가 같은 raw 를 다시 집계하지 않게 하려는 것(2026-07-28 리뷰 §3-4). 기본값(False)은 기존 3-튜플이라 노트북 등 기존 호출부는 그대로다.

## 5. 예측 SOP

`predict_lean85.py` 출력 = `[C64, pred_C65, low_confidence]`. `low_confidence=1`(요란 PM 후 7일)은 온셋 구간 — B2′ 실증상 이 구간 오차 급증(fold 147/113/109)이므로 참고용으로만 쓰고, 재학습 후 회복을 기다린다. 예측 시 pm_log는 반드시 최신 운영본을 쓸 것(구본 사용 시 days/레짐 피처 왜곡). 모델이 재학습 권고 상태면 CLI가 경고를 띄운다.

## 6. 수치 인용 규칙 (필수)

현업/보고 인용은 **시간축 99.84 / R² 0.8545** (주간 재학습 전제)만. lot-CV 66.8~67.0은 "같은 시기 새 Lot"(후향·대회축) 전용 — 미래를 엿본 낙관 수치라 현업 근거로 인용 금지. 무재학습 254.9는 "재학습이 전제"의 실증 근거로만 인용.

## 7. 모니터링

주간 RMSE(라벨 확보분 `lp.evaluate_rmse`) · 입력 PSI(`lp.drift_report`, 월 1회+재학습 시) · 잔차 lot-ICC(선택) · pm_log 이벤트. 기록은 아래 운영 로그에 누적.

## 8. 운영·실험 로그 (누적)

| 날짜 | 작업 | 결과/비고 |
|---|---|---|
| 2026-07-19 | 패키지 v1.0 제작 · lean-85 최종 확정(사용자) | 동결 스펙 복사, 노트북+CLI+문서. 미러 미검증(정적 검토만) — 로컬 최초 실행 대기 |
| 2026-07-28 | 코드리뷰 대응 (§2-2·2-3·2-4·3-3·3-4) | 계약 가드 `assert`→`raise`(`check_feature_contract`, 헌법 1-3 방어선이 `python -O` 로 사라지지 않게) · 수용검사 유효 fold 0 진단 예외 · 파일 핸들 `with` 전환 · `pm_log` mtime 캐시 + PM 탐색 `searchsorted`(구 O(n_wf×n_ev) 선형 스캔) · `retrain(return_table=True)` 로 피처 빌드 중복 제거. **수치 영향 없음** — 메타 8종 산출 등가성 실측 확인(Δ=0) |

## 9. 스냅샷 인덱스

| REPORT | 내용 |
|---|---|
| `REPORT/handoff_lean85_REPORT_01_lean85_확정.md` | lean-85 확정 경위·모델 카드·정직 수치·재학습 프로토콜·한계 (2026-07-19) |

상위 근거 문서: `modeling_v14/v13_v14_통합_FINAL_REPORT.md` · `modeling_v13/REPORT/modeling_v13_REPORT_12_lean_eval.md`(lean-85 정의) · `.../REPORT_13_B2prime_final.md`(재학습 판정)
