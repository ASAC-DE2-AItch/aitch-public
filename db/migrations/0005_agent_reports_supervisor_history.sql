-- =============================================================================
-- 0005: agent_reports.supervisor_history — Supervisor 판단 변경 이력 (D10)
-- 도입: PM · 2026-08-04 (#97 리뷰 공약 — 새 테이블 대신 컬럼 추가, 헌법 3-2)
--
-- 형식: JSONB 배열, append-only —
--   [{"version": 3, "selected": "limit_option", "ts": "2026-08-04T01:23:45+00:00"}, ...]
--   · version  = brief_version (재생성마다 +1, mig 0003)
--   · selected = 그 버전에서 Supervisor 가 고른 옵션
--   · ts       = 기록 시각 (UTC)
-- 근거: 조회가 항상 report_id 단위(조인 불요) · 감사 요구는 「무엇이 언제 바뀌었나」까지
--       (JSONB 배열로 충분) · 8/11 코드프리즈 일정. 새 테이블·라우트·프론트는 만들지 않는다.
-- 쓰기 주체: C 재생성 경로(save_brief/update_brief) — 배선은 C 후속.
--       그전까지 컬럼은 NULL 로 존재만 한다 (#97 L120-124 warning 로그 유지 합의).
--
-- 적용 (PowerShell — stdin 파이프 금지, README 규약):
--   docker cp db\migrations\0005_agent_reports_supervisor_history.sql fdc-postgres:/tmp/m.sql
--   docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/m.sql
-- =============================================================================

ALTER TABLE agent_reports
    ADD COLUMN IF NOT EXISTS supervisor_history JSONB;

COMMENT ON COLUMN agent_reports.supervisor_history IS
    'D10: Supervisor 판단 변경 이력 [{version,selected,ts}] append-only (mig 0005, 2026-08-04)';

-- 확인 쿼리 — 컬럼 실재 + 타입
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'agent_reports' AND column_name = 'supervisor_history';
