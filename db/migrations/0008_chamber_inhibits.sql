-- 0008_chamber_inhibits.sql — 챔버 자동 inhibit(안전 정지) 이력 (헌법 1-1 예외 4, 2026-08-06)
--   (번호 이력: 0004 → 0006 → 0008. 병행 워크트리에서 0004=settle_converged(guard) ·
--    0006=tttm_suspect_sensors(#113) 와 각각 충돌해 두 번 밀었다. 전부 멱등 — 재적용 무해)
-- 규약: db/migrations/README.md (init.sql 과 같은 PR — 3-2). 기존 DB 적용은 README 실행법 참조
--   (docker cp + psql -f — stdin 파이프 금지).
-- 소유: PM (RTD 자동 승격 — 설계 docs/RTD_자동정지_승격_설계_v1_2026-08-06.md).
-- 이 테이블이 S1 챔버 상태 배지 + uptime 위젯(멘토 제안)의 단일 소스다 (inhibit 구간 = downtime).

CREATE TABLE IF NOT EXISTS chamber_inhibits (
    id                  BIGSERIAL PRIMARY KEY,
    chamber_id          VARCHAR(32)  NOT NULL,
    incident_id         VARCHAR(64)  NOT NULL,           -- 발동 근거 Incident
    trigger_alert_id    VARCHAR(64),                     -- 발동 CRITICAL 마커 알람
    inhibited_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    released_at         TIMESTAMPTZ,                     -- NULL = 정지 중
    released_by         VARCHAR(64),                     -- requal 승인자 (예외 4 ⓒ — 사람)
    release_qual_id     VARCHAR(64),
    release_incident_id VARCHAR(64)
);

-- 디바운스의 DB 보장: 챔버당 열린 inhibit 1건 (record_trigger ON CONFLICT 대상)
CREATE UNIQUE INDEX IF NOT EXISTS idx_chamber_inhibits_open_one
    ON chamber_inhibits (chamber_id) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_chamber_inhibits_chamber
    ON chamber_inhibits (chamber_id, inhibited_at);

-- 확인 쿼리 (규약 3 — 적용 여부 육안 확인)
SELECT 'chamber_inhibits' AS applied,
       count(*) AS existing_rows,
       (SELECT count(*) FROM pg_indexes
         WHERE tablename = 'chamber_inhibits') AS index_count   -- 기대: 3 (PK 포함)
  FROM chamber_inhibits;
