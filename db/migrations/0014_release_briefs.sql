-- 0014_release_briefs.sql — RTD 해제 근거 Brief (인프라 스텝 3 · #140, 2026-08-08)
--   ※ 개번: 초안 `0012` → **`0014`** (2026-08-10). `0012` 는 CT⓪ 분, `0013` 은 프라이어 원장으로
--     확정돼 머지가 늦은 이쪽이 비켰다 (README 「머지가 늦은 쪽이 개번」 — 파일명·목록·적용 상태 표 3곳).
-- 규약: db/migrations/README.md (init.sql 과 같은 PR — 헌법 3-2). 기존 DB 적용은 README 실행법 참조
--   (docker cp + psql -f — stdin 파이프 금지).
-- 소유: db/ 는 PM (3-1) — 본 파일은 C 작성·PM 리뷰 대상. 신규 테이블 사유는 아래.
--
-- [사유 — 헌법 3-2 "새 테이블 추가는 가능하되, PR 에 사유를 명시한다"]
--   챔버가 RTD 로 자동 정지되면(1-1 예외 4) 엔지니어는 "다시 돌려도 되나"를 판단해야 하는데,
--   그 근거가 지금은 어디에도 모이지 않는다. Agent 가 회고 3층(왜 섰나 / 얼마나 심했나 /
--   과거엔 뭘로 풀렸나)을 만들어 여기에 남기고, R9(requalify) 승인 화면 S7 이 읽는다.
--
--   ⚠️ **이 테이블은 해제를 실행하지 않는다.** 해제 경로는 requal 승인 하류 하나뿐이며
--      (1-1 예외 4 ⓒ · gateway `/requalify/{incident_id}`), readiness 는 권고 값이다.
--
--   agent_reports 를 재사용하지 않은 이유: `/incidents/recent` 의 LATERAL 이 그 테이블의
--   **최신 1행**을 `SUP-%` 필터 없이 읽어 S3 결정 대기 큐의 title/verdict 로 렌더한다
--   (src/gateway/main.py:1115). 다른 종류의 행을 섞으면 그 화면이 조용히 오염된다.

CREATE TABLE IF NOT EXISTS release_briefs (
    id               BIGSERIAL PRIMARY KEY,
    report_id        VARCHAR(64)  NOT NULL UNIQUE,   -- RLS-<YYYYMMDD>-<CHAMBER>-<SEQ> (6-4 신설)
    incident_id      VARCHAR(64)  NOT NULL,
    chamber_id       VARCHAR(32)  NOT NULL,          -- 대표 챔버(발동 incident 의 챔버)
    scope            VARCHAR(16)  NOT NULL DEFAULT 'chamber',   -- chamber | equipment
    trigger_alert_id VARCHAR(64),
    inhibited_at     TIMESTAMPTZ,
    readiness        VARCHAR(16)  NOT NULL,          -- ready | conditional | not_ready (권고)
    summary          TEXT,
    evidence         JSONB,                          -- 근거 3층 list[str] (계약 v4.6 정합)
    counter_evidence TEXT,
    confidence       FLOAT,
    precheck_items   JSONB,                          -- requal 전 확인 항목 list[str]
    retrospect       JSONB,                          -- 회고 3층 정량 (코드가 채움 — LLM 창작 아님)
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 정지 1회 = Brief 1건 (LLM 중복 호출 방지의 DB 보장). 해제 후 같은 incident 로 재정지되면
-- inhibited_at 이 달라 새 행이 된다 — "다시 섰다"는 새 판단이 필요한 사건이기 때문.
CREATE UNIQUE INDEX IF NOT EXISTS idx_release_briefs_inhibit
    ON release_briefs (incident_id, inhibited_at);
CREATE INDEX IF NOT EXISTS idx_release_briefs_chamber
    ON release_briefs (chamber_id, created_at);

-- 확인 쿼리 (규약 3 — 적용 여부 육안 확인)
SELECT 'release_briefs' AS applied,
       count(*) AS existing_rows,
       (SELECT count(*) FROM pg_indexes
         WHERE tablename = 'release_briefs') AS index_count   -- 기대: 4 (PK·UNIQUE 포함)
  FROM release_briefs;
