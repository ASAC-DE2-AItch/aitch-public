-- =============================================================================
-- [제안] y_thresholds — 챔버별 예측 수율(C65) 임계 테이블
--
-- 작성: 팀원 B (팀원 B), 2026-07-22 — B6-3-b Phase 2 재수립(Y-임계 갱신) 후속
-- 상태: 제안 (db/init.sql은 PM/PO 소유 — 헌법 3-1/3-2, PM 승인 후 init.sql에 반영)
--
-- 사유:
--   요란 PM(레짐 전환) 후 예측 수율 임계를 챔버별로 갱신해야 한다
--   (신규결정 1 — 고정 상수는 새 레짐서 오발동: seg2 평균 1,123 vs 고정 1,572).
--   임계는 두 개: B9 crazy = P99 · C2 Qual = P95 (다른 값·다른 소비자).
--
--   control_limits에 담지 않는 이유:
--     · control_limits 한 행 = 밴드(ucl/lcl) 1개인데, Y는 컷오프 2개(P95·P99)
--     · predicted_c65는 시딩된 적 없는 가상센서(net-new) → 기존 승인·적용 경로
--       (현행 행 갱신 방식)가 "현행 행 없음"으로 skip돼 최초 write가 안 됨
--     · 억지 편입 시 가짜 메타(center/sigma/step)·가짜 센서명·loader 오편입 발생
--   → SPC 밴드가 아니라 "스팟 임계"라 전용 소형 테이블이 정합.
--
--   쓰기 = B6-3-b (Phase 2 재수립 시, 최근 예측분포 분위수 — label-free 1차)
--   읽기 = C2 (Qual C65 판정, P95) · B9 (crazy wafer 판정, P99)
--
-- 네이밍: 헌법 6-4 (snake_case 복수형 · *_at · is_* · idx_<table>_<cols>).
--   control_limits와 컬럼명 정합 (threshold_version, trigger_type, is_active, effective_from).
-- =============================================================================

CREATE TABLE IF NOT EXISTS y_thresholds (
    id                 BIGSERIAL     PRIMARY KEY,
    chamber_id         VARCHAR(32)   NOT NULL,
    recipe_id          VARCHAR(16)   NOT NULL,
    p95                FLOAT         NOT NULL,               -- C2 Qual 기준 (train P95=1404 초기값)
    p99                FLOAT         NOT NULL,               -- B9 crazy 기준 (train P99=1572 초기값)
    threshold_version  VARCHAR(16)   NOT NULL DEFAULT 'v1',
    calc_window_n      SMALLINT,                             -- 산출 예측 표본 수 (정착 창)
    label_corrected    BOOLEAN       NOT NULL DEFAULT FALSE, -- label-free 1차(FALSE) / 라벨 보정 후(TRUE) — Y-2
    trigger_type       VARCHAR(16)   NOT NULL DEFAULT 'initial',  -- initial / incident(레짐 재수립)
    is_active          BOOLEAN       NOT NULL DEFAULT TRUE,  -- 현행 버전 여부
    effective_from     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (chamber_id, recipe_id, threshold_version)
);

-- 조회 핫패스 (챔버·레시피별 현행)
CREATE INDEX IF NOT EXISTS idx_y_thresholds_key
    ON y_thresholds (chamber_id, recipe_id, is_active);

-- 챔버·레시피당 현행(is_active) 1건 보장 (control_limits 부분유니크와 동일 패턴)
CREATE UNIQUE INDEX IF NOT EXISTS idx_y_thresholds_active_one
    ON y_thresholds (chamber_id, recipe_id) WHERE is_active;
