# AE 실연동 P3 검증 리포트 v1 — 단위·패리티 테스트

> 작성: 2026-07-24 · 팀원 A 트랙 (A3-3) · 대상 `tests/test_ae_consumer.py`
> 선행: P4 실연동 리허설(2026-07-24) 마감 후 · P3 단위·패리티 검증
> 실행 환경: 로컬 venv (torch·AE 번들 `models/anomaly_ae/ae_v1`·`merged_data_v3.parquet` 존재)

---

## 0. 한 줄 결론

consumer의 AE 실연동 경로에 대한 **9개 테스트 전부 통과** — `rows_to_trace_ae` 스키마·방어, `build_message` 실값/폴백/계약 게이트, AE 실패 독립 폴백이 무결하고, **오프라인 `infer score` ↔ consumer 재구성 경로의 `ae_score`가 20장에서 판정 100% 일치**함을 자동화로 확정. P3(단위·패리티) 완료.

---

## 1. 실행 결과

```
$ python -m pytest tests/test_ae_consumer.py -q
.........                                                          [100%]
9 passed in 5.03s
```

`.` 9개 = 아래 9개 테스트 전부 PASS. **패리티 테스트가 SKIP이 아닌 PASS**라는 점이 중요하다 — torch·번들·parquet가 모두 로드돼 실제 스코어링으로 대조됐다는 뜻(자산 부재 시 SKIP 설계).

---

## 2. 테스트별 검증 내용과 보증

| # | 테스트 | 검증 내용 | 보증하는 것 |
|---|---|---|---|
| 1 | `rows_to_trace_ae_schema_and_types` | fdc.raw 모사 rows → 트레이스 | 필수 컬럼(C64·C7·C42·C46·C10·C24)·센서 보존, C42=정수·C10=datetime·C24=챔버키 |
| 2 | `rows_to_trace_ae_skips_missing_c42` | `stabilization_flag=None` row 주입 | 마스크 불능 row 스킵+로그 (헌법 6-2 — C42 결측 방어) |
| 3 | `dedup_guard_prepare_frame` | `(C64,C7,C46)` 중복 주입 | `prepare_frame` 후 유일화(중복 1건 제거) — 불가침 11 |
| 4 | `build_message_real_ae_values` | ae_rec 실값 주입 | `ae_drift_score→drift_score`·`ae_score→anomaly_score` 정매핑 |
| 5 | `build_message_dummy_fallback_ranges` | ae_rec=None | AE 미가동 시 더미 폴백 범위(drift 0.1~0.4·anomaly 0.01~0.15) |
| 6a | `build_message_ext_fields_gated_off_by_default` | `AE_PUBLISH_EXT=0` | 확장필드(ae_raw 등) **미발행** — 계약 §2-2 승인 전 금지 |
| 6b | `build_message_ext_fields_published_when_approved` | `AE_PUBLISH_EXT=1` | 승인 후에만 확장필드 발행 (게이트 반대편) |
| 7 | `flush_survives_ae_failure_and_publishes_dummy` | AE 스코어러 예외 | flush 생존 + C65 발행 지속 + AE 더미 폴백 (헌법 6-2 독립 폴백) |
| 8 | `parity_offline_vs_consumer_path` | 오프라인 vs consumer 20장 | **ae_score 0.2 판정 100% 동일 · 최대 편차 < 0.02** (패리티 완료 기준 — 0.2 판정 100%) |

---

## 3. 핵심 — 패리티 결과의 의미

패리티 테스트는 **같은 wafer 20장**을 두 경로로 스코어링해 대조한다:

1. **오프라인 경로**: `merged_data_v3.parquet` → `AEModel.score_wafers` (= `infer score` CLI와 동일 수학)
2. **consumer 경로**: 같은 wafer를 fdc.raw 형태로 재구성(perturb 없음) → `rows_to_trace_ae` → `score_wafers`

PASS 판정(20/20 동일, 편차 <0.02)이 보증하는 것:

- **`rows_to_trace_ae` 재구성이 정확하다** — 센서 누락·컬럼 오정렬·타입 왜곡이 있었다면 두 경로 `ae_score`가 갈렸을 것. 100% 일치 = fdc.raw → ae_pipeline 스키마 매핑(fdc.raw → ae_pipeline)이 무결하다는 **코드 레벨 증거**.
- **consumer가 AE 계약을 흔들지 않는다** — 발행되는 `anomaly_score`가 오프라인 배치 채점과 동일한 판정을 낸다.

이로써 P2(consumer 실값 전환)의 정합성이 정적 코드 리뷰를 넘어 **실측으로 검증**됐다.

---

## 4. P4 리허설과의 연결

패리티 통과는 P4에서 관측한 사실을 자동화로 못 박은 것이다:

| | anomaly 관측 | 해석 |
|---|---|---|
| P4 라이브 (offset 0.5) | 40장 전부 `1.000` 포화 | 시뮬레이터 챔버 오프셋이 AE 캘리 앵커 밖으로 입력을 밀어냄 |
| P4 라이브 (offset 0.0) | `0.0 ~ 0.09` 정상 복귀 | 오프셋 제거 시 정상 |
| P3 패리티 (offset 없음) | 오프라인과 20장 판정 100% 일치 | **경로는 무결** — 포화는 입력 분포 문제이지 코드 버그가 아님 |

즉 **AE·번들·consumer 무결**이라는 P4 진단이 P3 패리티로 독립 재확인됐다. 남은 챔버 오프셋 이슈는 코드가 아니라 시뮬레이터 설정(`simulator.chamber_offset_sigma`)의 팀 결정 사항(상세: `models/anomaly_ae/README.md` 알려진 한계).

---

## 5. 헌법·계약 준수 (테스트가 강제)

- **헌법 6-2** — AE 실패 시 독립 폴백·스킵+로그·발행 지속 (테스트 2·7)
- **계약 §2-2** — 확장필드 승인 게이트, 미승인 시 발행 금지 (테스트 6a/6b)
- **불가침 11** — `(C64,C7,C46)` dedup 상시 적용 (테스트 3)
- **스키마 불변(1단계)** — 기존 필드(drift/anomaly)에만 주입, 신규 필드 미발행 (테스트 4·6a)

---

## 6. 결론 및 다음 단계

- **P2·P3 검증 완료.** consumer AE 실연동 경로는 단위·패리티·리허설 3중으로 확인됐다.
- **남은 것**: P5(문서 동기 · 소비자 B 공지 · PR 분할). `models/anomaly_ae/README.md`의 챔버 오프셋 한계는 반영 완료.
- **주의**: 패리티 테스트는 로컬 자산(`merged_data_v3.parquet`, repo 미커밋)에 의존 → CI에서는 자동 SKIP. 단위 8개는 자산 무관하게 항상 수행.

---

## 참고

- 테스트: `tests/test_ae_consumer.py`
- 대상 코드: `src/agent_a_mlops/consumer.py` (`rows_to_trace_ae`·`build_message`·`flush_wafer`)
- 관련: `models/anomaly_ae/README.md` 알려진 한계 · `models/CHANGELOG.md` ae_v1
- 한계 문서: `models/anomaly_ae/README.md` (챔버 오프셋 민감도)
