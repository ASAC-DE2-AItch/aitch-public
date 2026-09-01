-- =============================================================================
-- [제안] control_limits — 센서별 현행 실력치(관리선) 테이블
--
-- 작성: 팀원 B (팀원 B), 2026-07-07 — B2-1 초기 실력치 산출 작업의 후속
-- 상태: 제안 (db/init.sql은 PM/PO 소유 — 헌법 3-1/3-2, PM 승인 후 init.sql에 반영)
--
-- 사유:
--   limit_corrections는 "변경 이력"만 기록하고, Nelson 판정 엔진이 조회할
--   "현행 관리선" 저장소가 스키마에 없음. 초기값(v1)은 B2-1 산출물
--   (initial_limits_v1.csv)을 시딩하고, 이후 실력치 엔진이 재산정 시
--   새 limit_version row를 추가(기존 row는 is_active=false 처리)한다.
--
-- 네이밍: 헌법 6-4 (snake_case 복수형 예외 없음 — limits 복수형, *_at, is_*)
--   limit_corrections와 컬럼명 정합 (sensor_window, limit_version, trigger_type)
-- =============================================================================

CREATE TABLE IF NOT EXISTS control_limits (
    id              BIGSERIAL PRIMARY KEY,
    chamber_id      VARCHAR(32)   NOT NULL,
    recipe_id       VARCHAR(16)   NOT NULL,
    step            SMALLINT      NOT NULL,
    sensor_window   VARCHAR(16)   NOT NULL DEFAULT 'settled',   -- settled / transient
    sensor_id       VARCHAR(16)   NOT NULL,                     -- C코드 (파생은 D_ 접두)
    limit_version   VARCHAR(16)   NOT NULL DEFAULT 'v1',
    center          FLOAT         NOT NULL,
    sigma           FLOAT         NOT NULL,
    ucl             FLOAT         NOT NULL,
    lcl             FLOAT         NOT NULL,
    k_sigma         FLOAT         NOT NULL DEFAULT 3.0,          -- config A1
    n_wafers        SMALLINT,                                    -- 산출 표본 수 (트림 전)
    calc_window_n   SMALLINT,                                    -- 트림 후 실사용 표본 수
    trigger_type    VARCHAR(16)   NOT NULL DEFAULT 'initial',    -- initial / periodic / incident / qual
    qa_flags        VARCHAR(64),                                 -- low_n / zero_sigma / high_false_alarm
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,         -- 현행 버전 여부
    effective_from  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (chamber_id, recipe_id, step, sensor_window, sensor_id, limit_version)
);
CREATE INDEX IF NOT EXISTS idx_control_limits_key
    ON control_limits (chamber_id, recipe_id, step, sensor_window, sensor_id, is_active);
