# DB 네이밍 컨벤션 v1.2 *(2026-07-06: v4.5 — recipe_corrections·RCP 접두사. 테이블 수 = init.sql 단일 소스(현 17개))*

> 근거: 헌법 6-4 · 실제 적용: `db/init.sql` v4.4 · 위반 시 리뷰 차단
> **이 문서 사용법: 아래 예제 테이블 하나만 이해하면 규칙의 90%를 아는 것.** 나머지는 필요할 때 참조.

---

## 예제 하나로 보는 전체 규칙

```sql
CREATE TABLE IF NOT EXISTS limit_corrections (   -- ① 테이블: snake_case + 복수형
    id              BIGSERIAL PRIMARY KEY,       -- ② PK는 항상 id (기술키)
    correction_id   VARCHAR(64) UNIQUE,          -- ③ 업무키: <entity>_id — 조인·이벤트 참조는 이걸로
    incident_id     VARCHAR(64),                 -- ④ 다른 테이블 참조: 그쪽 업무키 이름 그대로
    sensor_id       VARCHAR(16),                 -- ⑤ 센서는 C코드 그대로 ('C11') — 'DC_Bias' 저장 금지
    delta_pct       FLOAT,                       -- ⑥ 비율은 *_pct, 점수는 *_score
    limit_version   VARCHAR(16),                 -- ⑦ 버전은 *_version, 값은 'v1','v2'...
    center_before   FLOAT,
    center_after    FLOAT,                       -- ⑧ 전/후 쌍은 *_before / *_after
    is_escalated    BOOLEAN,                     -- ⑨ 불리언은 is_*
    status          VARCHAR(16),                 -- ⑩ 상태값 표기는 아래 '상태값' 절 참조
    effective_from  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ                  -- ⑪ 시각은 *_at (TIMESTAMPTZ)
);
CREATE INDEX idx_limit_corrections_key           -- ⑫ 인덱스: idx_<테이블>_<의미>
    ON limit_corrections (chamber_id, recipe_id, step, sensor_window, sensor_id);
```

## 상태값 — 딱 2가지만 기억

| 언제 | 표기 | 예 |
|------|------|-----|
| 시스템이 굴리는 **흐름 상태** | 소문자 | `open → analyzing → pending → actioned → verifying → closed` |
| 사람/판정의 **결정 결과** | 대문자 | `PENDING / APPROVED / REJECTED` · `HOLD / SCRAP / RELEASE` · `CRITICAL / WARNING / INFO` |

유형 구분자(enum 아닌 분류값)는 소문자 snake: `ct1_xgb`, `fleet_median`, `settled`

> `ct1_xgb` 는 구 `ct1_lgbm` 이다 — 최종 예측 모델 **XGBoost 확정**(2026-07-22, 헌법 3-3)에 따른 현행화이며, 코드 정본은 `ct1_orchestrator.CT_TYPE` 다. 값 자체를 바꾼 것이므로 **표기 규칙(소문자 snake)에는 변경이 없다**.

## 업무 ID 포맷

```
<PREFIX>-<YYYYMMDD>-<CHAMBER>-<SEQ>        예: INC-20260713-SIMCH3-001
```

`ALERT`(B) · `INC`(PM) · `LIM`(B) · `RCP`(C — v4.5) · `MNT`/`SUP`(C) · `QUAL`/`CT`(A) · `CASE`/`MAN`(C) — 챔버 무관 ID는 CHAMBER 생략 가능 (`CT-20260713-001`)

## 하지 마세요 (리뷰 차단 5선)

| ❌ | ✅ |
|-----|-----|
| `createdAt`, `waferId` (camelCase) | `created_at`, `wafer_id` |
| `incident` (단수형 테이블) | `incidents` |
| `sensor_name = 'DC_Bias'`로 판정 로직 작성 | `sensor_id = 'C11'` (발표명은 화면에서만) |
| 기존 컬럼 삭제·이름 변경 | 컬럼 추가만 (헌법 3-2) |
| 파이프라인 코드에서 `sim_events` 조회 | 금지 — 답안지 커닝 (Scorecard 전용) |

## 참고

- JSONB 내부 키도 snake_case, 구조는 API Contract의 JSON 예시와 동일하게
- `langgraph` 스키마(체크포인터)는 직접 조회·수정 금지 — 승인 조회는 `approval_records`로

---

## 부록: 테이블 17개 한눈에 (init.sql 단일 소스)

| 테이블 | 소유 | 업무키 | 한 줄 설명 |
|--------|------|--------|-----------|
| wafer_predictions | A | wafer_id | C65 예측 + SHAP/spc_flags |
| spc_violations | B | alert_id | Nelson 위반 기록 (window 단위) |
| tttm_comparisons | B | — | 챔버 간 편차 + 참조 의심 플래그 |
| incidents | PM | incident_id | 알람 그룹 = 승인 단위 = LangGraph thread |
| limit_corrections | B | correction_id | 실력치 재설정 이력 (5축 관리) |
| recipe_corrections | C/PM | recipe_correction_id | Recipe R2R 이력 — 제안→승인→적용→검증 (v4.5) |
| wafer_dispositions | PM/C | — | HOLD → SCRAP/RELEASE |
| approval_records | PM | — | 승인 감사 추적 (원안+수정값) |
| agent_reports | C | report_id | 옵션 3종 + Supervisor 4지선다 판정 (v4.5) |
| historical_cases | C | case_id | RAG 과거 사례 (is_seed 구분) |
| rag_documents | C | doc_id | KB 문서 메타 (Qdrant 페어) |
| ct_decisions | A | ct_id | 재학습 이력 (ct1/ct2) |
| qual_snapshots | B | snapshot_id | Qual 시점 정상 지문 (TTTM 보조) |
| sim_events | PM | event_id | 주입 정답 — **파이프라인 읽기 금지** |
| quals | PM | qual_id | Qual 판정 마스터 (조용/요란 — 3테이블 참조 부모, v4.5) |
| control_limits | B | (chamber+recipe+step+sensor+window) | 현행 실력치(관리선) — Nelson 조회 (is_active 1행, v4.6) |
| incident_alerts | PM | alert_id | Incident 멤버십 정본 (알람↔incident 1:N, P4-3 2026-07-13) |
