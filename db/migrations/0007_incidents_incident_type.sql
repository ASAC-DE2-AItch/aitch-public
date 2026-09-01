-- 0007_incidents_incident_type.sql
-- incidents.incident_type 추가 (P6-1 소급분 — 2026-08-07, C 팀원 C)
--
-- [왜]
--   `01afdfd`(2026-07-28, "P6-1 Incident lifecycle 완성 + B9 crazy 라우팅")가 **같은 커밋에서**
--   ⓐ init.sql 에 컬럼을 넣고 ⓑ incident_grouper 의 INSERT 에 그 컬럼을 쓰기 시작했다.
--   레포는 정합인데 **이미 떠 있는 DB 에는 컬럼이 붙지 않아**(init.sql = CREATE TABLE IF NOT
--   EXISTS + 데이터 디렉토리 최초 1회) 대응 마이그레이션이 필요했는데, 그 파일이 만들어지지
--   않았다. 헌법 3-2 「스키마 변경 = 마이그레이션 동반」이 2026-07-31 신설이라 **3일 빨랐던
--   변경**이고, 아무도 규칙을 어기지 않은 채로 구멍이 남았다. 이 파일이 그 소급분이다.
--
-- [증상] — 조용하지 않은데 안 보인다
--   컬럼이 없으면 `_create_incident` 의 INSERT 가 통째로 실패한다(컬럼 목록에 무조건 포함).
--   그런데 그루퍼는 헌법 6-2 무중단이라 `log.exception` 후 **스킵하고 계속 돌고**, 오프셋도
--   전진시킨다 → 프로세스는 살아 있고 SPC 는 계속 쌓이는데 **Incident 만 0건**이다.
--     psycopg2.errors.UndefinedColumn: column "incident_type" of relation "incidents" does not exist
--   실측(2026-08-07): 이 DB 는 spc_violations 가 08-05 까지 8,546건 쌓이는 동안
--   incidents·incident_alerts·agent_reports 가 **07-29 에서 멈춰 있었다**.
--
-- [누가 걸리나]
--   `pg_data` 볼륨을 **2026-07-28 이전에 만든 환경 전부**. 그 뒤 새로 만든 환경은 init.sql 이
--   최신으로 돌아 정상이다. ⚠️ 컴포넌트 소유자(PM)는 P6-1 작업 중 볼륨을 새로 만들었을 가능성이
--   높아 **본인 로컬은 정상일 수 있다** — "우리는 되는데"로 덮이기 쉬운 자리다.
--   확인 한 줄:
--     docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -t -c "SELECT CASE WHEN EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='incidents' AND column_name='incident_type') THEN 'OK' ELSE 'MISSING' END"
--
-- [값의 의미] — init.sql:102 정본
--   NULL                = 일반 SPC 사건 (기존 행은 전부 여기 해당)
--   'crazy_spot'        = B9 crazy wafer 1장 격리 건 (그루퍼가 B9 마커 인식 시 기입 — 조사 사건 아님·HOLD 처분 전용)
--   'regime_transition' = 요란 PM 전환 건 (BL1 — Qual 경로 작업 시 기입)
--   S3/S6 화면 분기·Scorecard 채점 소스이며, C Supervisor 프롬프트도 이 값으로 분기한다
--   (`src/agent_service/app/supervisor.py:106` — regime_transition 이면 Brief 첫 문장에 맥락 명시).
--
-- [주의]
--   멱등. 재실행 안전. nullable + DEFAULT 없음이라 PG 11+ 에서는 **카탈로그만 바뀐다**
--   (테이블 재작성 없음 · 락 순간적) — 실측 환경 PG 16.14, 기존 47행은 NULL 로 남고
--   그 값이 곧 "일반 SPC 사건"이라 의미상으로도 정확하다.
--   컬럼 물리 순서는 신규 설치(init.sql: lifecycle 뒤)와 달리 맨 뒤에 붙는다 — 코드는 이름으로만
--   접근하므로 무해(SELECT * 눈비교 금지). 0006 과 같은 성질.
--
-- [롤백]
--   ALTER TABLE incidents DROP COLUMN IF EXISTS incident_type;
--
-- [실행]
--   docker cp db/migrations/0007_incidents_incident_type.sql fdc-postgres:/tmp/0007.sql
--   docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/0007.sql

ALTER TABLE incidents
    ADD COLUMN IF NOT EXISTS incident_type VARCHAR(24);

-- 확인 쿼리 — 1행이 나와야 한다
SELECT column_name,
       data_type,
       character_maximum_length,
       is_nullable
  FROM information_schema.columns
 WHERE table_name  = 'incidents'
   AND column_name = 'incident_type';
-- 기대: incident_type | character varying | 24 | YES
