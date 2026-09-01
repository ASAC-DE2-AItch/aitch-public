-- 0006_tttm_suspect_sensors.sql
-- tttm_comparisons.suspect_sensors 추가 (경로① reference_suspect decouple — 2026-08-05, B 팀원 B)
--
-- [왜]
--   경로①(reference_suspect)이 worst 센서(경로②·C62 독점) 게이팅에 묶여 실제 공통이동
--   (C17 등)을 masking했다. decouple로 reference_suspect를 worst와 분리하고, 실제 역방향
--   발동 센서 C코드 목록을 별도 필드 suspect_sensors로 내보낸다(S7 화면·RTD 진단·DB+alert).
--   설계 근거 = src/agent_b_spc/notes/제안_경로1_reference_suspect_decouple_20260805.md.
--
--   init.sql 은 CREATE TABLE IF NOT EXISTS + compose 데이터 디렉토리 최초 1회만 실행이라
--   이미 떠 있는 DB 에는 컬럼이 붙지 않는다 → 이 마이그레이션으로 기존 DB 경로를 이행한다.
--   tttm_writer._INSERT_SQL 이 이 컬럼에 CAST(:suspect_sensors AS JSONB)로 쓰므로, 컬럼이
--   없으면 라이브 tttm 적재가 UndefinedColumn 으로 끊긴다. 헌법 3-2 (스키마 변경 = PM + 마이그레이션).
--
-- [값의 의미]
--   JSON 배열(예: ["C17"] · ["C17","C31"]) = 역방향 룰이 발동한 실제 공통이동 센서 C코드 목록.
--   []   = 평가는 됐으나 공통이동 없음 (reference_suspect=false 와 짝).
--   NULL = 미평가 (rollup 미생성 경로 — writer 는 항상 값을 싣지만 수기·구행 대비 nullable).
--
-- [주의]
--   멱등. 재실행 안전. 컬럼 물리 순서는 신규 설치(init.sql: is_reference_suspect 뒤)와 달리
--   맨 뒤에 붙는다 — 코드는 이름으로만 접근하므로 무해(SELECT * 눈비교 금지).
--
-- [실행]
--   docker cp db/migrations/0006_tttm_suspect_sensors.sql fdc-postgres:/tmp/0006.sql
--   docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/0006.sql

ALTER TABLE tttm_comparisons
    ADD COLUMN IF NOT EXISTS suspect_sensors JSONB;

-- 확인 쿼리 — 1행이 나와야 한다
SELECT column_name,
       data_type,
       is_nullable
  FROM information_schema.columns
 WHERE table_name  = 'tttm_comparisons'
   AND column_name = 'suspect_sensors';
-- 기대: suspect_sensors | jsonb | YES
