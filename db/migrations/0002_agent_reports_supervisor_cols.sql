-- 0002 — agent_reports 에 Supervisor 판정 3필드 추가 (2026-07-31, PM)
--
-- 왜: LLM 이 verdict(4지선다)·evidence(근거)·counter_evidence(반증)를 생성하는데
-- report_writer 가 selected·reason·confidence 만 적재해 S4 화면의 근거·반증이 "—" 로
-- 비어 있었다. 구 주석 "verdict 는 옵션 JSONB 안에 실린다"는 오기였다 — 옵션 스키마
-- (OptionSummary)에 그 필드가 없다.
--
-- 헌법 3-2: 컬럼 추가(ALTER)는 허용 — 기존 컬럼 삭제·개명 없음.
-- 실행·규약: db/migrations/README.md 참조 (docker cp + psql -v ON_ERROR_STOP=1)
--
-- 구 행은 NULL 로 남는다 — 소비처(supervisor_adapter.fetch_bundle · gateway
-- fetch_pending · S4 briefFromRow)가 전부 폴백을 갖고 있어 무해하다. 재적재는 하지 않는다.

ALTER TABLE agent_reports
    ADD COLUMN IF NOT EXISTS supervisor_verdict  VARCHAR(32),
    ADD COLUMN IF NOT EXISTS supervisor_evidence JSONB,
    ADD COLUMN IF NOT EXISTS supervisor_counter  TEXT;

-- 확인 — 3행이 나와야 정상
SELECT column_name, data_type FROM information_schema.columns
 WHERE table_name = 'agent_reports'
   AND column_name IN ('supervisor_verdict', 'supervisor_evidence', 'supervisor_counter')
 ORDER BY 1;
