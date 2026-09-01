-- =============================================================================
-- 능동형 제조 AI 플랫폼 v4.5 — PostgreSQL 초기 테이블 스키마
-- Docker Compose 기동 시 자동 실행됨 (docker-entrypoint-initdb.d)
--
-- [v4.7 개정 — 2026-08-06, PM (헌법 3-2: 신규 테이블 사유 명시)]
--   · 신규: chamber_inhibits (RTD 자동 정지 승격 — 헌법 1-1 예외 4. 발동·해제 이력 +
--     S1 챔버 배지·uptime 위젯 단일 소스. 마이그레이션 0008 동반 — 3-2 규약)
--   · 테이블 수(개정): 17 → 18
--
-- [v4.6 개정 — 2026-07-13, PM (헌법 3-2: 신규 테이블 사유 명시)]
--   · 신규: incident_alerts (P4-3 그루퍼 멤버십 — fdc.alert→incident 편입 기록.
--     멤버십 정본=이 테이블(그룹핑=PM 소관). spc_violations.incident_id는 B 엔진 후 옵션 스탬프(정본 아님))
--   · 테이블 수(개정): 16 → 17
--
-- [v4.5 → v4.6 개정 — 2026-07-10, PM 승인 (헌법 3-1/3-2)]
--   · 신규: control_limits (B 팀원 B 제안 — 현행 관리선 저장소. limit_corrections는
--     이력만 기록, Nelson 엔진이 조회할 현행 선 부재 해소. PM 수정 3건 반영:
--     ① method/q_low/q_high — 결정 A 조합②(KEEP* 분위수, 헌법 1-1 예외 2) 지원
--     ② is_active 부분 유니크 인덱스 — 그룹당 현행 1행 보장
--     ③ trigger_type에 provisional 예약 — 가한계 Phase 0~1(B6-3))
--
-- [v4.4 → v4.5 개정 — 2026-07-07, PM 승인 (헌법 3-1/3-2)]
--   · 신규: quals (Qual 판정 마스터 — 3개 테이블이 참조하던 qual_id의 부모 테이블 부재 해소)
--   · wafer_predictions: measured_at 컬럼 추가 — fdc.actual(v4.6 초안) 도착 시
--     actual_c65와 함께 UPDATE (별도 wafer_actuals 테이블 신설 취소)
--   · NOTICE 테이블 수 정정 (실제 14개였음 → 15개로)
--
-- [v4.2 → v4.4 개정 — 2026-07-03, PM 승인 (헌법 3-1/3-2)]
--   · r2r_recommendations 폐기 → limit_corrections (Recipe 보정 → 실력치 재설정)
--   · 신규: incidents, approval_records, wafer_dispositions, tttm_comparisons,
--           sim_events, qual_snapshots, rag_documents
--   · 확장: wafer_predictions(+anomaly/spc_flags/is_qual),
--           spc_violations(+window/limit_version/alert_id/incident_id),
--           agent_reports(옵션명 limit_option), ct_decisions(+ct_type/qual)
--   · 네이밍: 헌법 6-4 준수 (snake_case 복수형, *_at, is_*, idx_<table>_<cols>)
--   · chamber_id: 시뮬레이터가 부여하는 가상 챔버 (SIM_CH_1 ~ SIM_CH_4 — 장비 1대=4PM, 시뮬 스펙 v1.3)
--   · LangGraph 체크포인터 테이블은 라이브러리가 자체 생성 (별도 스키마 langgraph)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS langgraph;

-- 1. Pipeline A: Wafer 단위 C65 예측 결과 -----------------------------------
CREATE TABLE IF NOT EXISTS wafer_predictions (
    id              BIGSERIAL PRIMARY KEY,
    wafer_id        VARCHAR(32)   NOT NULL,
    lot_id          VARCHAR(32),
    chamber_id      VARCHAR(32)   NOT NULL,
    recipe_id       VARCHAR(16),
    predicted_c65   FLOAT         NOT NULL,
    actual_c65      FLOAT,                              -- fdc.actual 도착 시 UPDATE (label_delay 경과 후) — v4.5
    measured_at     TIMESTAMPTZ,                        -- 실측(WT) 확보 시점 — v4.5
    drift_score     FLOAT,
    anomaly_score   FLOAT,
    shap_top3       JSONB,
    spc_flags       JSONB,
    is_qual         BOOLEAN       NOT NULL DEFAULT FALSE,
    model_version   VARCHAR(32),
    bias_applied    FLOAT,                              -- CT⓪ Model R2R (2026-08-07, 마이그레이션 0012): 발행 예측에 가산된 bias. raw = predicted_c65 − bias_applied 로 보정 전 값 복원 (설계 D1 — 개루프 잔차 추정의 전제). NULL=보정 경로 없던 시기
    pm_count        INTEGER,                            -- CT⓪ (0012): fdc.actual 이 실어온 PM 카운트 — 창 편입 자격 필터 축 (설계 §5·§7 재계산). NULL=미도착·구행
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wafer_predictions_wafer   ON wafer_predictions (wafer_id);
CREATE INDEX IF NOT EXISTS idx_wafer_predictions_chamber ON wafer_predictions (chamber_id, created_at);
-- CT⓪ 부트스트랩(§7) 조회 축 — 라벨 도착분만 (전체의 일부라 부분 인덱스가 선택도를 준다)
CREATE INDEX IF NOT EXISTS idx_wafer_predictions_label   ON wafer_predictions (chamber_id, measured_at DESC) WHERE actual_c65 IS NOT NULL;

-- 2. Pipeline C(SPC): Nelson Rule 위반 (판정·기록) ---------------------------
CREATE TABLE IF NOT EXISTS spc_violations (
    id              BIGSERIAL PRIMARY KEY,
    alert_id        VARCHAR(64)   NOT NULL,
    incident_id     VARCHAR(64),
    chamber_id      VARCHAR(32)   NOT NULL,
    recipe_id       VARCHAR(16),
    step            SMALLINT,
    sensor_window   VARCHAR(16)   NOT NULL DEFAULT 'settled',
    rule_id         VARCHAR(8)    NOT NULL,
    sensor_id       VARCHAR(16)   NOT NULL,
    severity        VARCHAR(16)   NOT NULL,
    current_value   FLOAT,
    limit_version   VARCHAR(16)   NOT NULL DEFAULT 'v1',
    control_limit_upper FLOAT,
    control_limit_lower FLOAT,
    context_score   FLOAT,
    description     TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_spc_violations_chamber  ON spc_violations (chamber_id, created_at);
CREATE INDEX IF NOT EXISTS idx_spc_violations_incident ON spc_violations (incident_id);

-- 3. Pipeline C(SPC): TTTM 비교 ---------------------------------------------
CREATE TABLE IF NOT EXISTS tttm_comparisons (
    id              BIGSERIAL PRIMARY KEY,
    chamber_id      VARCHAR(32)   NOT NULL,
    reference_type  VARCHAR(32)   NOT NULL DEFAULT 'fleet_median',
    reference_id    VARCHAR(64),
    tttm_score      FLOAT         NOT NULL,
    top_gap_sensor  VARCHAR(16),
    gap_pct         FLOAT,
    is_reference_suspect BOOLEAN  NOT NULL DEFAULT FALSE,
    suspect_sensors JSONB,                              -- 경로① decouple(2026-08-05): 역방향 실제 공통이동 센서 C코드 목록 (worst≠suspect 분리). NULL=미평가/공통이동 없음
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tttm_comparisons_chamber ON tttm_comparisons (chamber_id, created_at);

-- 4. Incident — 알람 그룹핑 단위 (승인은 Incident당 1건, 헌법 1-4) -----------
CREATE TABLE IF NOT EXISTS incidents (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     VARCHAR(64)   NOT NULL UNIQUE,
    chamber_id      VARCHAR(32)   NOT NULL,
    lifecycle       VARCHAR(16)   NOT NULL DEFAULT 'open',
    incident_type   VARCHAR(24),                 -- P6-1 (2026-07-28, 헌법 3-2 ALTER ADD): 사건 종류 꼬리표. NULL=일반 SPC 사건 / 'crazy_spot'=B9 crazy 1장 격리 건(그루퍼가 B9 마커 인식 시 기입 — 조사 사건 아님·HOLD 처분 전용) / 'regime_transition'=요란 PM 전환 건(BL1 — Qual 경로 작업 시 기입). S3/S6 분기·Scorecard 채점 소스.
    severity_max    VARCHAR(16),
    priority_score  FLOAT,
    suspect_window_start VARCHAR(32),
    suspect_window_end   VARCHAR(32),
    claimed_by      VARCHAR(64),
    reopen_count    SMALLINT      NOT NULL DEFAULT 0,
    is_escalated    BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),   -- P4-3: 집계 갱신 시각 (감사)
    closed_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_incidents_lifecycle ON incidents (lifecycle, priority_score DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_chamber ON incidents (chamber_id, lifecycle);  -- P4-3 그루퍼 병합 조회 핫패스

-- 4a. Incident 멤버십 — 그루퍼(P4-3)가 fdc.alert를 incident에 편입한 기록 --------
--   [신규 v4.6 개정 2026-07-13, PM] 원래 멤버십은 spc_violations.incident_id 소급 예정이나
--   현 dev는 mock_alert_publisher가 fdc.alert 메시지만 위조하고 spc_violations 미기록 →
--   멤버십 정본=이 테이블(그룹핑은 PM 소관). mock은 임시 대역(B 엔진 뜨면 제거)이나 이 표는 영구.
--   spc_violations.incident_id는 B 엔진 후 조회 편의용 옵션 스탬프(정본 아님).
CREATE TABLE IF NOT EXISTS incident_alerts (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     VARCHAR(64)   NOT NULL REFERENCES incidents (incident_id),
    alert_id        VARCHAR(64)   NOT NULL UNIQUE,   -- 재편입 방지(idempotent) — 계약 §3 ALERT-...
    chamber_id      VARCHAR(32)   NOT NULL,
    severity        VARCHAR(16),                     -- violations[] 중 최고 severity
    context_score   SMALLINT,                        -- 계약 §3 (agent 가동 <31 게이팅 근거)
    alert_ts        TIMESTAMPTZ   NOT NULL,          -- fdc.alert.timestamp (시각근접 그룹핑 축)
    suspect_start   VARCHAR(32),                     -- alert.suspect_window.start_wafer
    suspect_end     VARCHAR(32),                     -- alert.suspect_window.end_wafer
    raw             JSONB,                           -- 원본 payload 보존 (감사·재처리)
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_incident_alerts_incident ON incident_alerts (incident_id);
CREATE INDEX IF NOT EXISTS idx_incident_alerts_chamber_ts ON incident_alerts (chamber_id, alert_ts DESC);

-- 5. Pipeline D(B 소유): 실력치 재설정 이력 (구 r2r_recommendations 대체) ----
CREATE TABLE IF NOT EXISTS limit_corrections (
    id              BIGSERIAL PRIMARY KEY,
    correction_id   VARCHAR(64)   NOT NULL UNIQUE,
    incident_id     VARCHAR(64),
    qual_id         VARCHAR(64),
    chamber_id      VARCHAR(32)   NOT NULL,
    recipe_id       VARCHAR(16)   NOT NULL,
    step            SMALLINT      NOT NULL,
    sensor_window   VARCHAR(16)   NOT NULL DEFAULT 'settled',
    sensor_id       VARCHAR(16)   NOT NULL,
    limit_version_before VARCHAR(16) NOT NULL,
    limit_version_after  VARCHAR(16),
    center_before   FLOAT,
    center_after    FLOAT,
    ucl_after       FLOAT,
    lcl_after       FLOAT,
    center_modified FLOAT,                          -- 수정 승인(modify) 엔지니어 보정값 (B5-1 필드규칙 안ⓐ 2026-07-23, 헌법 3-2 ALTER ADD): 원안(*_after) 불변, B apply는 COALESCE(수정,원안). sigma 그룹=center만 수정→밴드 재산출
    ucl_modified    FLOAT,                          -- 분위수(KEEP*) 그룹 전용 — 경계 직접·부분 수정 (NULL=해당 없음)
    lcl_modified    FLOAT,
    delta_pct       FLOAT,
    delta_sigma     FLOAT,                          -- 이동량 σ 단위 (2026-07-16 추가 — B4-3, 헌법 3-2 ALTER ADD): 재산정 시점 σ로 환산 저장(B 엔진 산출값), 사이클 누적 = Σ delta_sigma ≤ A3 1.0σ. delta_pct(%)는 그룹별 σ/center 환산율이 달라 사후 역산 불가(σ_ref 미보존·분위수 그룹은 σ 부재) — "트립미터에 km·마일 혼재" 문제
    trigger_type    VARCHAR(16)   NOT NULL DEFAULT 'incident',  -- incident / qual / periodic (정기 자동 리캘리브레이션 — 멘토 확인)
    settle_converged BOOLEAN,                        -- B6-3-e (2026-07-26, 헌법 3-2 ALTER ADD): incident 재수립 제안의 Phase 1 정착이 σ-수렴(TRUE)인지 상한 1,000 폴백(FALSE=미수렴)인지. 승인자(R9) 경고용. periodic/qual 등 비-재수립은 NULL. ⚠️우리 repo 선반영 — 팀 dev PR 대기(y_thresholds 방식)
    calc_window_n   SMALLINT,                       -- rolling window 표본 수 (mean ± 3σ 산식, EWMA 폐기)
    status          VARCHAR(16)   NOT NULL DEFAULT 'PROPOSED',
    shadow_false_alarm_reduction_pct FLOAT,        -- 섀도 = 승인 전 소급 채점 (직전 이력 30장 재적용, drift 재설정·상한 초과만 — Phase 2 신규 수립은 NULL. A5 11차 2026-07-22, 컬럼명 유지 = 헌법 3-2)
    shadow_missed_detection SMALLINT,
    effective_from  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_limit_corrections_key ON limit_corrections (chamber_id, recipe_id, step, sensor_window, sensor_id);

-- 5a-2. 현행 실력치(관리선) — B 팀원 B 제안 (2026-07-07), PM 승인 2026-07-10 (v4.6) --
--   limit_corrections(이력)와 역할 분리: Nelson 엔진은 이 테이블의 is_active 행만 조회.
--   초기값(v1) = B2-1 산출물(initial_limits_v1.csv) + 챔버별 오프셋 시딩(chamber_offsets.json).
--   재산정 시 새 limit_version row 추가 + 기존 row는 is_active=false (덮어쓰기 금지 — 헌법 4-1 정신)
CREATE TABLE IF NOT EXISTS control_limits (
    id              BIGSERIAL PRIMARY KEY,
    chamber_id      VARCHAR(32)   NOT NULL,
    recipe_id       VARCHAR(16)   NOT NULL,
    step            SMALLINT      NOT NULL,
    sensor_window   VARCHAR(16)   NOT NULL DEFAULT 'settled',   -- settled / transient
    sensor_id       VARCHAR(16)   NOT NULL,                     -- C코드 (파생은 D_ 접두, 가상 센서 predicted_c65 허용 — B5-3·Y-임계 챔버별 관리)
    limit_version   VARCHAR(16)   NOT NULL DEFAULT 'v1',
    method          VARCHAR(16)   NOT NULL DEFAULT 'sigma',     -- sigma / quantile (결정 A 조합② — KEEP* 6그룹, 헌법 1-1 예외 2)
    center          FLOAT         NOT NULL,
    sigma           FLOAT         NOT NULL,                     -- quantile 행에서는 참고 통계 (판정 미사용)
    ucl             FLOAT         NOT NULL,
    lcl             FLOAT         NOT NULL,
    k_sigma         FLOAT         DEFAULT 3.0,                  -- config A1 (quantile 행은 NULL)
    q_low           FLOAT,                                      -- method=quantile일 때만 (하한 분위수)
    q_high          FLOAT,                                      -- method=quantile일 때만 (상한 분위수)
    n_wafers        SMALLINT,                                   -- 산출 표본 수 (트림 전)
    calc_window_n   SMALLINT,                                   -- 트림 후 실사용 표본 수
    trigger_type    VARCHAR(16)   NOT NULL DEFAULT 'initial',   -- initial / periodic / incident / qual / provisional(가한계 Phase 0~1 — B6-3)
    qa_flags        VARCHAR(64),                                -- low_n / zero_sigma / high_false_alarm
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,        -- 현행 버전 여부
    effective_from  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (chamber_id, recipe_id, step, sensor_window, sensor_id, limit_version)
);
CREATE INDEX IF NOT EXISTS idx_control_limits_key
    ON control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, is_active);
-- 그룹당 현행(is_active) 행 1개 보장 — 판정 엔진 조회 무결성 (PM 추가)
CREATE UNIQUE INDEX IF NOT EXISTS idx_control_limits_active_one
    ON control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id) WHERE is_active;

-- 5b. Recipe R2R 이력 (v4.5 신설 — 멘토 정정: Recipe 수정도 서비스 포함, 승인 후 적용) --
CREATE TABLE IF NOT EXISTS recipe_corrections (
    id              BIGSERIAL PRIMARY KEY,
    recipe_correction_id VARCHAR(64) NOT NULL UNIQUE,     -- RCP-<YYYYMMDD>-<CHAMBER>-<SEQ>
    incident_id     VARCHAR(64)   NOT NULL,
    chamber_id      VARCHAR(32)   NOT NULL,
    recipe_id       VARCHAR(16)   NOT NULL,
    step            SMALLINT      NOT NULL,
    parameter_id    VARCHAR(16)   NOT NULL,               -- 튜닝 대상 setpoint (C코드, 예: 'C1','C4')
    value_before    FLOAT,
    value_after     FLOAT,
    delta_pct       FLOAT,
    shap_basis      JSONB,                                -- 제안 근거 SHAP 기여
    rag_evidence    JSONB,                                -- Process KB 근거 요약
    status          VARCHAR(16)   NOT NULL DEFAULT 'PROPOSED',  -- PROPOSED/APPROVED/REJECTED/APPLIED/VERIFIED
    verify_result   JSONB,                                -- 적용 후 N wafer 효과 검증
    applied_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_recipe_corrections_incident ON recipe_corrections (incident_id);

-- 6. Wafer Disposition — 인터락/HOLD/스크랩 ----------------------------------
CREATE TABLE IF NOT EXISTS wafer_dispositions (
    id              BIGSERIAL PRIMARY KEY,
    wafer_id        VARCHAR(32)   NOT NULL,
    lot_id          VARCHAR(32),
    chamber_id      VARCHAR(32)   NOT NULL,
    incident_id     VARCHAR(64)   NOT NULL,
    status          VARCHAR(16)   NOT NULL DEFAULT 'HOLD',
    hold_reason     TEXT,
    system_recommendation VARCHAR(16),
    recommendation_basis  TEXT,
    decided_by      VARCHAR(64),
    decided_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wafer_dispositions_status   ON wafer_dispositions (status, chamber_id);
CREATE INDEX IF NOT EXISTS idx_wafer_dispositions_incident ON wafer_dispositions (incident_id);

-- 7. Approval Records — 감사 추적 + RAG 학습 자산 (헌법 1-1) -----------------
CREATE TABLE IF NOT EXISTS approval_records (
    id              BIGSERIAL PRIMARY KEY,
    incident_id     VARCHAR(64)   NOT NULL,
    request_type    VARCHAR(32)   NOT NULL,
    status          VARCHAR(16)   NOT NULL DEFAULT 'PENDING',  -- PENDING/APPROVED/MODIFIED/REJECTED/ESCALATED (P4-1 A안)
    selected_option VARCHAR(32),
    original_value  JSONB,
    modified_value  JSONB,
    decision_reason TEXT,
    approver        VARCHAR(64),
    approver_role   VARCHAR(32),
    evidence_snapshot JSONB,                              -- 결정 시점 증거층 스냅샷 (2026-07-31, mig 0003)
    requested_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_approval_records_status   ON approval_records (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_approval_records_incident ON approval_records (incident_id);

-- 8. Agent 리포트 (C 소유 — 병렬 옵션 + Supervisor 판정) ---------------------
CREATE TABLE IF NOT EXISTS agent_reports (
    id              BIGSERIAL PRIMARY KEY,
    report_id       VARCHAR(64)   NOT NULL UNIQUE,
    incident_id     VARCHAR(64)   NOT NULL,
    chamber_id      VARCHAR(32)   NOT NULL,
    severity_score  INT,
    suspected_root_causes JSONB,
    recipe_option   JSONB,                                -- 옵션① Recipe Tuning 리포트 (v4.5)
    limit_option    JSONB,
    manual_option   JSONB,
    supervisor_recommendation VARCHAR(32),
    supervisor_reason TEXT,
    supervisor_confidence FLOAT,
    supervisor_verdict  VARCHAR(32),                      -- 4지선다 판정 (2026-07-31 신설 — 적재 유실 갭, mig 0002)
    supervisor_evidence JSONB,                            -- 근거 list[str] (인용 가드 통과분, mig 0002)
    supervisor_counter  TEXT,                             -- 반증 1건 (§5 필수 생성값, mig 0002)
    brief_version       INT,                              -- 재생성마다 +1 (서사층 — 새 행 아님, mig 0003)
    covered_signatures  JSONB,                            -- 설명 완료한 (sensor,rule_id) 배열 — B9 제외 (mig 0003)
    needs_reanalysis    BOOLEAN NOT NULL DEFAULT FALSE,   -- 미설명 시그니처 유입 표지 (mig 0003)
    supervisor_history  JSONB,                            -- D10 판단 변경 이력 [{version,selected,ts}] append-only (mig 0005)
    disposition_recommendation JSONB,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_reports_incident ON agent_reports (incident_id);

-- 9. RAG: 과거 사례 ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS historical_cases (
    id              BIGSERIAL PRIMARY KEY,
    case_id         VARCHAR(64)   NOT NULL UNIQUE,
    chamber_id      VARCHAR(32),
    nelson_rule     VARCHAR(8),
    sensor_id       VARCHAR(16),
    action_type     VARCHAR(32),
    action_taken    TEXT,
    delta_pct       FLOAT,
    outcome_false_alarm_reduction_pct FLOAT,
    outcome_missed_detection BOOLEAN,
    is_success      BOOLEAN,
    is_seed         BOOLEAN       NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- 10. RAG: 문서 메타데이터 (Qdrant 페이로드 페어링) --------------------------
CREATE TABLE IF NOT EXISTS rag_documents (
    id              BIGSERIAL PRIMARY KEY,
    doc_id          VARCHAR(64)   NOT NULL UNIQUE,
    kb_type         VARCHAR(32)   NOT NULL,
    title           TEXT,
    source          TEXT,
    qdrant_collection VARCHAR(64),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- 11. MLOps: CT 재학습 (2종 — 헌법 3-3) --------------------------------------
CREATE TABLE IF NOT EXISTS ct_decisions (
    id              BIGSERIAL PRIMARY KEY,
    ct_id           VARCHAR(64)   NOT NULL UNIQUE,
    ct_type         VARCHAR(16)   NOT NULL,               -- ct0_bias(Model R2R) / ct1_xgb / ct2_ae (구 ct1_lgbm — XGBoost 확정 2026-07-22, 헌법 3-3)
    trigger_reason  VARCHAR(64)   NOT NULL,               -- cycle_end / residual_bias / qual_approved 등
    residual        FLOAT,                                -- ct0: 실측-예측 격차 (v4.5)
    bias_applied    FLOAT,                                -- ct0: 적용 bias, 상한 clamp 후 (v4.5)
    qual_id         VARCHAR(64),
    drift_score     FLOAT,
    model_version_before VARCHAR(32),
    model_version_after  VARCHAR(32),
    champion_challenger_result VARCHAR(16),
    rmse_before     FLOAT,
    rmse_after      FLOAT,
    retrain_status  VARCHAR(16)   NOT NULL DEFAULT 'PENDING',
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- 12. Qual 스냅샷 — TTTM 보조 참조 (Qual 통과 시 저장) ---------------
CREATE TABLE IF NOT EXISTS qual_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_id     VARCHAR(64)   NOT NULL UNIQUE,
    chamber_id      VARCHAR(32)   NOT NULL,
    qual_id         VARCHAR(64)   NOT NULL,
    sensor_stats    JSONB         NOT NULL,
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- 12b. Qual 판정 마스터 — PM 후 Qual 5장 검증 결과 (API Contract 8절, S7 화면) — v4.5
CREATE TABLE IF NOT EXISTS quals (
    id              BIGSERIAL PRIMARY KEY,
    qual_id         VARCHAR(64)   NOT NULL UNIQUE,
    chamber_id      VARCHAR(32)   NOT NULL,
    wafer_ids       JSONB         NOT NULL,             -- QUAL 모드 5장
    ae_anomaly_mean FLOAT,
    tttm_gap_pct    FLOAT,
    nelson_violations SMALLINT,
    c65_within_normal BOOLEAN,
    thresholds      JSONB,                              -- 판정 당시 config C2 스냅샷
    proposed_verdict VARCHAR(8),                        -- PASS / FAIL
    approval_status VARCHAR(16)   NOT NULL DEFAULT 'PENDING',
    approved_by     VARCHAR(64),
    decided_at      TIMESTAMPTZ,
    confirmed_verdict VARCHAR(8),                       -- P6-1 (2026-07-28, 헌법 3-2 ALTER ADD): 확정 verdict 영속 — 'loud'/'quiet'. QualVerdictConfirmed 유실 대비 재시작 복구 소스 (계약 §8-B 갭 해소, 2026-07-22 B 지적). NULL=미확정
    confirmed_at    TIMESTAMPTZ,                        -- P6-1: verdict 확정 시각 (이벤트 confirmed_at 동일값)
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_quals_chamber ON quals (chamber_id, created_at);

-- 13. 시뮬레이터 Ground Truth (Scorecard 채점 전용) --------------------------
--     파이프라인(A/B/C 서비스)은 이 테이블을 읽지 않는다 — 답안지 커닝 금지
CREATE TABLE IF NOT EXISTS sim_events (
    id              BIGSERIAL PRIMARY KEY,
    event_id        VARCHAR(64)   NOT NULL UNIQUE,
    scenario_id     VARCHAR(64)   NOT NULL,
    chamber_id      VARCHAR(32)   NOT NULL,
    sensor_id       VARCHAR(16),
    pattern         VARCHAR(32),
    magnitude_pct   FLOAT,
    injected_at     TIMESTAMPTZ   NOT NULL,
    ground_truth_type VARCHAR(32) NOT NULL,
    correct_action  VARCHAR(32),
    correct_disposition VARCHAR(16),
    c65_effect      VARCHAR(16)   NOT NULL DEFAULT 'none',
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ═════════════════════════════════════════════════════════════════════════
-- ⚠️ TEMP (B6-3-b, 로컬 전용) — y_thresholds: 챔버별 예측수율(C65) 임계 (P95/P99)
--   상태: PM init.sql 공식 승인 대기(헌법 3-1/3-2). 제안 원본·사유 =
--         src/agent_b_spc/limits/y_thresholds_ddl_proposal.sql
--   공식 DDL 도착 시 이 블록을 교체(둘 다 제안 기반이라 diff ≈ 0). 팀 dev PR엔 미포함.
-- ═════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS y_thresholds (
    id                 BIGSERIAL     PRIMARY KEY,
    chamber_id         VARCHAR(32)   NOT NULL,
    recipe_id          VARCHAR(16)   NOT NULL,
    p95                FLOAT         NOT NULL,               -- C2 Qual 기준
    p99                FLOAT         NOT NULL,               -- B9 crazy 기준
    threshold_version  VARCHAR(16)   NOT NULL DEFAULT 'v1',
    calc_window_n      SMALLINT,
    label_corrected    BOOLEAN       NOT NULL DEFAULT FALSE, -- label-free 1차(FALSE)/라벨 보정(TRUE)
    trigger_type       VARCHAR(16)   NOT NULL DEFAULT 'initial',  -- initial / incident(레짐 재수립)
    is_active          BOOLEAN       NOT NULL DEFAULT TRUE,
    effective_from     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (chamber_id, recipe_id, threshold_version)
);
CREATE INDEX IF NOT EXISTS idx_y_thresholds_key
    ON y_thresholds (chamber_id, recipe_id, is_active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_y_thresholds_active_one
    ON y_thresholds (chamber_id, recipe_id) WHERE is_active;
-- ═════════════════════════════ TEMP 끝 ═══════════════════════════════════

-- 18. 챔버 자동 inhibit 이력 (RTD 자동 승격 — 헌법 1-1 예외 4, 2026-08-06 PM) --------
--     정지는 자동(CRITICAL+suspect), 해제는 requal 승인 하류만. 마이그레이션 0008 동일 정의.
--     S1 챔버 상태 배지 + uptime 위젯의 단일 소스 (inhibit 구간 = downtime).
CREATE TABLE IF NOT EXISTS chamber_inhibits (
    id                  BIGSERIAL PRIMARY KEY,
    chamber_id          VARCHAR(32)  NOT NULL,
    incident_id         VARCHAR(64)  NOT NULL,
    trigger_alert_id    VARCHAR(64),
    inhibited_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    released_at         TIMESTAMPTZ,
    released_by         VARCHAR(64),
    release_qual_id     VARCHAR(64),
    release_incident_id VARCHAR(64),
    -- 스코프 2단 (mig 0009, 2026-08-06 — 멘토: 챔버 디폴트 + 장비 단위도 정지):
    --   'equipment' 도 행은 챔버별로 만들고 같은 incident_id 로 묶는다(신호·해제 로직 재사용)
    scope               VARCHAR(16)  NOT NULL DEFAULT 'chamber',
    equipment_id        VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_chamber_inhibits_scope
    ON chamber_inhibits (scope, inhibited_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chamber_inhibits_open_one
    ON chamber_inhibits (chamber_id) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_chamber_inhibits_chamber
    ON chamber_inhibits (chamber_id, inhibited_at);

-- 19. RTD 해제 근거 Brief (인프라 스텝 3 · #140 — 2026-08-08 C) -------------------
--     "왜 섰나 / 얼마나 심했나 / 과거엔 뭘로 풀렸나" 회고 3층을 Agent 가 만들어 둔 것.
--     R9(requalify) 승인 화면(S7)이 읽는 근거이며 **해제를 실행하지 않는다** —
--     해제 경로는 requal 승인 하류 하나뿐이다(헌법 1-1 예외 4 ⓒ). 마이그레이션 0014 동일 정의.
--
--     agent_reports 를 재사용하지 않는 이유: `/incidents/recent` 의 LATERAL 이 그 테이블의
--     **최신 1행**을 SUP 필터 없이 읽어 title/verdict 로 렌더한다(gateway/main.py:1115).
--     여기에 다른 종류의 행을 섞으면 S3 결정 대기 큐가 조용히 오염된다.
CREATE TABLE IF NOT EXISTS release_briefs (
    id               BIGSERIAL PRIMARY KEY,
    report_id        VARCHAR(64)  NOT NULL UNIQUE,   -- RLS-<YYYYMMDD>-<CHAMBER>-<SEQ> (6-4 신설)
    incident_id      VARCHAR(64)  NOT NULL,
    chamber_id       VARCHAR(32)  NOT NULL,          -- 대표 챔버(발동 incident 의 챔버)
    scope            VARCHAR(16)  NOT NULL DEFAULT 'chamber',   -- chamber | equipment
    trigger_alert_id VARCHAR(64),
    inhibited_at     TIMESTAMPTZ,
    readiness        VARCHAR(16)  NOT NULL,          -- ready | conditional | not_ready (권고일 뿐)
    summary          TEXT,
    evidence         JSONB,                          -- 근거 3층 list[str] (계약 v4.6 정합)
    counter_evidence TEXT,
    confidence       FLOAT,
    precheck_items   JSONB,                          -- requal 전 확인 항목 list[str]
    retrospect       JSONB,                          -- 회고 3층 정량 (코드가 채움 — LLM 창작 아님)
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- 정지 1회 = Brief 1건. 해제 후 같은 incident 로 재정지되면 inhibited_at 이 달라 새 행이 된다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_release_briefs_inhibit
    ON release_briefs (incident_id, inhibited_at);
CREATE INDEX IF NOT EXISTS idx_release_briefs_chamber
    ON release_briefs (chamber_id, created_at);

DO $$
BEGIN
    RAISE NOTICE '✅ FDC Platform DB v4.7 초기화 완료: 19개 테이블(+y_thresholds TEMP) + langgraph 스키마';
END $$;

-- 18. 프라이어 전이 원장 (BL2 · 신규 결정 9) ------------------------------------
--   요란 PM 전이마다 1행. "라벨-프리 레벨 프라이어"의 재캘리브레이션 재료다
--   (사이클_정의_v1 §5 결정 9 · F17). 추정은 전이 시점에 INSERT, 실측(ΔY 점근)은
--   D+60 라벨 도착 후 UPDATE — 한 행이 추정→실측으로 닫히며 오차가 확정된다.
--   n=1 앵커가 회귀로 성장하는 구조라 **원장 없이는 마커 개정이 불가능**하다.
CREATE TABLE IF NOT EXISTS prior_transitions (
    transition_id   VARCHAR(48)  PRIMARY KEY,          -- PRI-YYYYMMDD-<CHAMBER>-<SEQ> (6-4)
    chamber_id      VARCHAR(32)  NOT NULL,
    incident_id     VARCHAR(48),                       -- regime_transition Incident (BL1)
    qual_id         VARCHAR(48),                       -- D+0 Qual (C17 읽기 출처)
    verdict_at      TIMESTAMPTZ  NOT NULL,             -- 요란 판정 시각 = 전이 기준점
    c17_delta       DOUBLE PRECISION NOT NULL,         -- 주채널 ⓐ: Qual 5장 정착 평균 − 직전 레짐 평균
    c11_wfmin_delta DOUBLE PRECISION,                  -- 교차확인 ⓒ (마커 v2 후보축)
    c12_delta       DOUBLE PRECISION,                  -- 교차확인 ⓒ (step6 평균 — ⓟ2)
    marker_version  VARCHAR(16)  NOT NULL,             -- 추정에 쓴 마커 (prior_markers.version)
    y_est           DOUBLE PRECISION NOT NULL,         -- 추정 ΔY = slope × delta
    y_actual        DOUBLE PRECISION,                  -- 실측 ΔY **점근 자 고정**(끝창 평균·hour 보정 — ⓟ3). 초기 과도 몫은 템플릿 소관이라 여기 섞지 않는다 (8/8 실측: 창에 따라 +364~+968 — 자 혼합이 v1 오차 72%의 뿌리)
    y_actual_at     TIMESTAMPTZ,
    err_pct         DOUBLE PRECISION,                  -- |est−actual|/|actual|×100 (실측 UPDATE 시 산출)
    tau_days        DOUBLE PRECISION,                  -- 과도 몫 ⓑ 감쇠 템플릿 τ
    amplitude       DOUBLE PRECISION,                  -- 과도 초기 진폭 A
    hour_adjusted   BOOLEAN      NOT NULL DEFAULT FALSE,  -- ⓟ3: Y 는 hour 보정 후 기록
    source          VARCHAR(12)  NOT NULL DEFAULT 'live', -- 'live' | 'injected'(실주입) | 'synthetic'(시나리오 정의만) — 주입 공개 원칙
    -- ▼ 재생 불변성 (2026-08-08) — 같은 시나리오를 리허설마다 다시 주입해도 **증거가 늘지 않는다**.
    --   원장의 불변식은 "사건 1건 = 1행"이다. 재생 시각으로 키를 잡으면 같은 전이가 회차마다
    --   새 행이 되어 회귀에 가중이 붙고 카드의 원장 건수가 부풀려진다 (실측 8/8: 같은 점 3행).
    --   자연키는 **전이의 정체**다 — 시뮬 = scenario_id, 실운전 = incident_id.
    --   ※ 값이 같으면 새 정보가 0 이므로 계수가 안 바뀌는 것이 옳다. 데모에서 원장이 움직이는
    --     것을 보이려면 **다른 전이**(다른 강도 시나리오)를 주입한다 — 연출이 아니라 실제로
    --     다른 사건이기 때문이다. 실운전은 incident_id 가 매번 달라 축적이 막히지 않는다.
    source_ref      VARCHAR(64)  NOT NULL,               -- 자연키 (UNIQUE)
    observed_count  INTEGER      NOT NULL DEFAULT 1,     -- 재생 횟수 (증거 아님 · 위생 기록)
    last_seen_at    TIMESTAMPTZ,                         -- 마지막 재생 시각
    note            TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prior_transitions_chamber ON prior_transitions (chamber_id, verdict_at DESC);
CREATE INDEX IF NOT EXISTS idx_prior_transitions_source ON prior_transitions (source);
-- 자연키 유일성 — 재주입이 새 행을 만들지 못하게 DB 가 막는다
CREATE UNIQUE INDEX IF NOT EXISTS uq_prior_transitions_ref ON prior_transitions (source_ref);

-- 19. 프라이어 마커 버전 (개정 이력) ---------------------------------------------
--   마커 = "무슨 채널로 레벨을 추정하나". v1 = C17 단독(앵커 −6.96), 개정 후보 = C17+C11.
--   개정은 **자동 금지 · 승인 경유**(제안 카드 → approval_records) — 물리 미개입이라
--   correction 은 발행하지 않고, 롤백은 직전 버전 재활성화다.
CREATE TABLE IF NOT EXISTS prior_markers (
    version         VARCHAR(16)  PRIMARY KEY,          -- v1, v2 ...
    channels        JSONB        NOT NULL,             -- ["C17"] | ["C17","C11_wf_min"]
    coefs           JSONB        NOT NULL,             -- {"C17": -6.96, "intercept": 0}
    fitted_n        INTEGER      NOT NULL DEFAULT 0,   -- 적합에 쓴 원장 건수
    err_pct         DOUBLE PRECISION,                  -- 적합 시점 원장 평균 오차
    is_active       BOOLEAN      NOT NULL DEFAULT FALSE,
    approval_id     VARCHAR(48),                       -- 개정 승인 기록 (헌법 1-1)
    changelog_ref   TEXT,                              -- models/CHANGELOG.md 항목
    note            TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    activated_at    TIMESTAMPTZ
);
-- 활성 마커는 항상 1개 (부분 유니크 — 두 마커가 동시에 활성이면 추정이 갈린다)
CREATE UNIQUE INDEX IF NOT EXISTS uq_prior_markers_active ON prior_markers (is_active) WHERE is_active;

-- v1 시드 — F17 검증분 (Qual 기준 캘리브레이션 앵커). 전체평균 기준은 계통 편향 −17%로 기각됨.
INSERT INTO prior_markers (version, channels, coefs, fitted_n, is_active, note, activated_at)
VALUES ('v1', '["C17"]'::jsonb, '{"C17": -6.96, "intercept": 0}'::jsonb, 1, TRUE,
        'F17 전이 앵커 기울기 (관측 전이 1건 · 인샘플) — 사이클_정의_v1 결정 9 ⓐ', NOW())
ON CONFLICT (version) DO NOTHING;
