# DB 테이블 설계 근거 v1 — "왜 17개를 따로 두는가"

> 2026-07-07 작성 (PM). 계기: 팀원 C(팀원 C)의 합리적 의문 — "각 테이블이 다 독립적으로 존재해야 하나? 합칠 수 있는 건 없나?"
> 결론 요약: **개수를 줄이는 것보다 각 테이블의 단일 책임을 지키는 게 이 프로젝트에선 이득이 크다.** 다만 이 의문 덕에 실제 문제 3건(불필요한 신설 계획 1, 마스터 부재 1, 문서 불일치 1)을 발견해 v4.5에서 수정했다.

---

## 1. 분리 판단의 5가지 원칙

테이블을 합칠지 나눌지는 개수가 아니라 아래 기준으로 판단했다. **하나라도 다르면 분리**가 원칙이다.

| # | 원칙 | 의미 | 어기고 합치면 생기는 일 |
|---|------|------|----------------------|
| P1 | **Grain (행의 단위)** | 1행이 무엇을 의미하는가 | 성격이 다른 행이 섞여 NULL 밭 + 인덱스 비효율 + 쿼리마다 type 필터 |
| P2 | **단일 Writer (소유권)** | 쓰는 모듈이 하나인가 (헌법 3-1의 디렉토리 소유권과 동형) | A·B·C가 같은 테이블에 INSERT → 스키마 변경 시 전원 조율, PR 리뷰 경계 붕괴 |
| P3 | **Lifecycle (상태머신)** | 행이 상태 전이를 갖는가, 불변 기록인가 | 감사 로그(불변)와 작업 상태(가변)가 섞여 "누가 언제 뭘 바꿨나" 추적 불가 |
| P4 | **소비자·보안 경계** | 읽으면 안 되는 모듈이 있는가 | 접근 통제를 코드 리뷰로 강제할 수 없게 됨 |
| P5 | **볼륨·수명** | 초당 수십 건 스트림인가, 사이클당 몇 건 마스터인가 | TTL·파티셔닝·백업 정책을 테이블 단위로 못 가져감 |

---

## 2. 테이블별 존재 이유 (17개 전수 — v4.6에서 control_limits, 2026-07-13에 incident_alerts(P4-3) 추가)

### 스트림 적재 3종 — P1(grain)·P5(볼륨)로 분리

| 테이블 | 1행의 의미 | Writer | 왜 못 합치나 |
|---|---|---|---|
| `wafer_predictions` | wafer 1장의 예측 결과 (+ 지연 도착하는 actual_c65) | A | 세 테이블의 grain이 전부 다름: wafer당 1행 vs 위반 이벤트당 1행 vs 주기 스냅샷당 1행. 합치면 공통 컬럼이 3~4개뿐이라 사실상 EAV 테이블이 됨 |
| `spc_violations` | Nelson 위반 이벤트 1건 | B | 〃 (위반이 없으면 행이 없음 — wafer와 1:N도 0:N도 됨) |
| `tttm_comparisons` | 챔버 1대의 주기적 fleet 비교 1회 | B | 〃 (위반이 아니라 측정 — 정상값도 계속 쌓여 추세 차트의 소스가 됨) |

### 워크플로 상태머신 3종 — P3(lifecycle)로 분리

| 테이블 | 상태 전이 | Writer | 왜 못 합치나 |
|---|---|---|---|
| `incidents` | open → analyzing → pending → actioned → verifying → closed/reopened | PM 백엔드 | 세 개는 서로 다른 상태머신이고 전이 규칙·타이밍이 독립적. 예: Incident 1건에 승인 기록 여러 번(수정 승인·reopen), disposition 여러 장(LOT 파생 HOLD). 합치면 1:N 관계가 깨져 "Incident당 승인 1건" 같은 헌법 1-4 검증을 SQL로 못 함 |
| `approval_records` | PENDING → APPROVED/MODIFIED/REJECTED/ESCALATED (이후 불변 — 감사 추적) | 승인 워크플로 | 〃 + **헌법 1-1이 명시 요구** ("체크포인터로 대체 불가") — 불변 감사 로그는 가변 작업 테이블과 물리적으로 분리하는 게 감사의 기본 |
| `wafer_dispositions` | HOLD → SCRAP/RELEASE | 승인 워크플로 | 〃 (wafer 단위 — incident 단위와 grain도 다름) |

> **`incident_alerts` (신규 P4-3, 2026-07-13)**: `incidents`의 자식 — 알람↔Incident 멤버십 **정본**(그룹핑=PM 소관). `spc_violations.incident_id`(B 테이블)는 mock 단계엔 비어 있고 소유 경계상 옵션 스탬프. 1:N(incident 1 : alert N), `alert_id` UNIQUE로 재전달 idempotent. 그루퍼 `src/orchestrator/incident_grouper.py`.

### 조치 이력 2종 + Agent 리포트 — P2(writer)·소비자로 분리

| 테이블 | Writer → Reader | 왜 못 합치나 |
|---|---|---|
| `limit_corrections` | B 엔진 산출 → 승인 → **B가 SPC 한계 갱신** | `fdc.correction` 토픽은 correction_type으로 통합돼 있지만, 테이블은 컬럼 구조가 완전히 다름 (limit: center/ucl/lcl/섀도 평가 vs recipe: parameter/delta/SHAP/적용 후 검증). 통합 시 행마다 절반이 NULL + 소비자(B vs 시뮬레이터)가 매번 type 필터. **"토픽은 라우팅이라 통합이 이득, 테이블은 스키마라 분리가 이득"** — 같은 데이터라도 계층마다 최적이 다름. *(2026-07-16 `delta_sigma` FLOAT 추가 — B4-3 요청·PM 승인: A2 0.5σ/A3 1.0σ 상한의 사이클 누적을 σ로 합산하기 위해 이동량을 σ 단위로 병행 저장. delta_pct는 그룹별 σ 환산율이 달라 역산 부정확. 헌법 3-2 추가만)* |
| `recipe_corrections` | C 권고 → 승인 → **시뮬레이터가 setpoint 반영** | 〃 |
| `control_limits` *(v4.6 신규 — B 제안·PM 승인 2026-07-10)* | B 산출·시딩 → **Nelson 엔진이 현행 선(is_active) 조회** | limit_corrections는 "변경 이력"(무슨 일이 있었나), 이 테이블은 "현행 상태"(지금 선이 뭔가) — grain·용도·조회 패턴이 다름. 이력에서 현행을 매번 재구성하면 판정 핫패스에 최신 버전 집계 쿼리가 낌. method(sigma/quantile — 헌법 1-1 예외 2)·q_low/q_high 포함, is_active 부분 유니크로 그룹당 현행 1행 보장 |
| `agent_reports` | C (Supervisor 통합 결과) | Brief 렌더링용 스냅샷. 옵션 JSONB가 corrections와 일부 중복인 건 사실이나, 이건 "승인 화면에 보여준 그대로"를 동결하는 감사 목적의 의도적 중복 (corrections 행은 이후 상태가 변함) |

### RAG 2종 — 용도로 분리

| 테이블 | 용도 | 왜 못 합치나 |
|---|---|---|
| `historical_cases` | 구조화된 사례 레코드 — **성공률 통계 쿼리**의 소스 (`search_similar_cases` tool의 "과거 성공률", KPI 대시보드) | Qdrant는 벡터 검색만, 통계('실력치 +4% 재설정의 성공률')는 관계형 집계가 필요. rag_documents와 합치면 문서 메타에 사례 전용 컬럼 10개가 낌 |
| `rag_documents` | 문서형 KB(Error Manual·Process Knowledge 등)의 메타 + Qdrant 컬렉션 페어링 | 〃 (이쪽은 통계 대상이 아니라 출처 추적용) |

### 마스터·기록 4종

| 테이블 | 존재 이유 |
|---|---|
| `quals` *(v4.5 신설)* | Qual 판정 결과의 마스터. **이번 검토에서 발견된 공백** — qual_id를 3개 테이블이 참조하는데 부모가 없었음 (팀원 C님 의문의 직접 성과) |
| `qual_snapshots` | TTTM **절대 비교**의 참조 스냅샷 (B2 멘토 확정 — "자기 Qual 스냅샷 대비"). quals와 분리인 이유: 판정은 1회 기록, 스냅샷은 이후 계속 조회되는 활성 참조 (is_active 교체 주기가 다름) |
| `ct_decisions` | 헌법 1-1·3-3이 명시 요구 — Model R2R(ct0_bias) 매 갱신·CT 재학습 기록 |
| `sim_events` | 채점 답안지. **P4(보안 경계)의 대표 사례** — 파이프라인 코드가 이 테이블을 SELECT하면 커닝이므로 물리적으로 격리돼 있어야 리뷰에서 차단 가능. 다른 무엇과도 합치면 안 되는 테이블 |

---

## 3. "합치자"를 실제로 검토한 3건과 결론

1. **limit_corrections + recipe_corrections → corrections 통합?** → 기각. 위 표 참조 — 공유 컬럼보다 전용 컬럼이 많고, 소비자·소유자·검증 로직이 다름. 토픽 계층에서 이미 통합돼 있어 통합의 이득(라우팅 단순화)은 이미 확보됨.
2. **wafer_actuals 신설 (fdc.actual 적재용)?** → **기각 (계획 취소).** `wafer_predictions.actual_c65`가 이미 있어 UPDATE + `measured_at` 컬럼 추가로 충분. 잔차(predicted vs actual) 계산이 한 행 안에서 끝나 Model R2R 쿼리도 단순해짐. — 팀원 C님 의문이 없었으면 테이블이 하나 더(당시 15→16) 늘 뻔했다. *(현 17개는 별개 사유 — v4.6 control_limits + 2026-07-13 incident_alerts(P4-3) 추가분)*
3. **quals + qual_snapshots 통합?** → 기각. 판정 기록(불변)과 활성 참조(교체됨)는 lifecycle이 다름 (P3).

## 4. 이번 검토로 고친 것 (v4.5, 2026-07-07 PM 승인)

- `quals` 신설 (마스터 부재 해소) / `wafer_predictions.measured_at` 추가 / wafer_actuals 신설 취소 (API Contract 1-B 갱신)
- 문서 정합: init.sql NOTICE 13→15, CLAUDE.md 3-2 "6개"(v4.2 잔재) 정정, README 14→15

> **한 줄 답변**: 테이블 수는 복잡도의 원인이 아니라 결과다. 이 시스템의 복잡도는 "승인·감사·채점이 전부 SQL로 검증 가능해야 한다"는 요구에서 오고, 그걸 17개로 나눠 담은 게 지금 구조다. 합쳐서 줄어드는 건 CREATE TABLE 줄 수지 복잡도가 아니다.
