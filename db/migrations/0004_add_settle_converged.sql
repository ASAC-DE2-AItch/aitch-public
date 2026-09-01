-- 0004_add_settle_converged.sql
-- limit_corrections.settle_converged 추가 (B6-3-e ② 미수렴 폴백 플래그 영속)
--
-- [왜]
--   a906bb5 (2026-07-26, B 팀원 B) 가 db/init.sql 에 이 컬럼을 추가하고
--   recalc_writer._INSERT_CORRECTION 이 이 컬럼에 쓰기 시작했다. 그런데 init.sql 은
--   전 테이블이 CREATE TABLE IF NOT EXISTS 이고 compose 마운트도 데이터 디렉토리가
--   빌 때만 실행되므로, 이미 떠 있는 DB 에는 컬럼이 붙지 않는다.
--
--   그 결과 correction 쓰기 경로가 전부 끊긴다:
--     recalc_writer.apply_auto / record_proposed  → _insert_correction → UndefinedColumn
--     spc_consumer 정기 리캘리                     → "정기 리캘리 실패 — skip" 으로 삼켜짐
--     provisional_mode.on_qual_verdict → _enter_phase_1 → write_provisional (Phase 1 진입 불가)
--
--   해당 커밋 메시지가 "팀 dev PR(PM 컬럼)" 을 이월 항목으로 남겨둔 건이며,
--   DB 스키마는 PM 소관(팀 RnR)이므로 본 마이그레이션으로 이행한다.
--   헌법 3-2 (DB 스키마 변경 = PM 승인 + 마이그레이션 동반).
--
-- [값의 의미]  B6-3-e 스펙 기준
--   TRUE  = σ 수렴 판정으로 조기 정착 (Phase2Signal.converged=True)
--   FALSE = 1,000장 상한 폴백으로 종료 = 미수렴 → 승인자 R9 경고 대상
--   NULL  = 판정 이전 / 해당 없음 (기본값)
--
-- [주의]
--   멱등. 재실행 안전.
--   컬럼 물리 순서는 신규 설치(init.sql: delta_sigma 뒤)와 달리 맨 뒤에 붙는다.
--   코드는 이름으로만 접근하므로 무해하나, 두 DB 를 SELECT * 로 눈비교하지 말 것.
--
-- [실행]
--   docker cp db/migrations/0004_add_settle_converged.sql fdc-postgres:/tmp/0004.sql
--   docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/0004.sql

ALTER TABLE limit_corrections
    ADD COLUMN IF NOT EXISTS settle_converged BOOLEAN;

-- 확인 쿼리 — 1행이 나와야 한다
SELECT column_name,
       data_type,
       is_nullable
  FROM information_schema.columns
 WHERE table_name  = 'limit_corrections'
   AND column_name = 'settle_converged';
-- 기대: settle_converged | boolean | YES
