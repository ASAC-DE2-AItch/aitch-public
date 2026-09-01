-- 0009_chamber_inhibits_scope.sql — inhibit 스코프 2단화 (챔버 / 장비) 2026-08-06
--   (구 0007 — 0008 개명에 맞춰 뒤로 이동. 멱등이라 재적용 무해)
--   근거: 멘토 확정 — "챔버 단위 정지가 디폴트지만 **장비 단위로도 정지한다**".
--   장비 정지 = 공용 설비(가스·전원·냉각) 공통 원인 → 4챔버 동시. 우리 신호는 RTD 경로①
--   (`reference_suspect` 연속 K = fleet median 오염 = 공통 이동)이 그대로 대응한다.
-- 구현 원칙: 장비 정지도 **행은 챔버별로** 생성하고 scope='equipment' + 같은 incident_id 로 묶는다
--   → 신호 파일(챔버별)·시뮬 스킵·해제 로직을 무변경 재사용 (0006 배선 그대로).
-- 규약: db/migrations/README.md · 소유 PM · 헌법 1-1 예외 4.

ALTER TABLE chamber_inhibits
    ADD COLUMN IF NOT EXISTS scope        VARCHAR(16) NOT NULL DEFAULT 'chamber',  -- chamber | equipment
    ADD COLUMN IF NOT EXISTS equipment_id VARCHAR(32);                             -- 장비 정지 시 대상 장비

CREATE INDEX IF NOT EXISTS idx_chamber_inhibits_scope
    ON chamber_inhibits (scope, inhibited_at);

-- 확인 쿼리 (규약 3)
SELECT 'chamber_inhibits.scope' AS applied,
       (SELECT count(*) FROM information_schema.columns
         WHERE table_name = 'chamber_inhibits'
           AND column_name IN ('scope', 'equipment_id')) AS new_cols;   -- 기대: 2
