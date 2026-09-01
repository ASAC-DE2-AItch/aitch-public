-- 0011: limit_corrections.center_modified · ucl_modified · lcl_modified
--        B5-1(2026-07-23) 소급 마이그레이션
--
-- 경위: B5-1 필드규칙 안ⓐ 가 init.sql 에 세 컬럼을 넣고 쓰기(gateway `approvals.py`)·
--       매핑(orchestrator `approval_graph.MODIFIED_FIELD_MAP`)·읽기(B `approval_apply`)까지
--       구현했으나, **마이그레이션 파일이 없었다.** 헌법 3-2(마이그레이션 동반 의무)가
--       7/31 신설이라 여드레 앞선 작업이고, 아무도 규칙을 어기지 않은 채 구멍이 남았다.
--       `0007`(incidents.incident_type)·`0010`(quals.confirmed_*)과 같은 계열이다 —
--       C 가 2026-08-07 컨테이너화 중 init.sql 전수 대조로 발굴한 6건의 마지막 3건.
--
-- 🔴 증상: **수정 승인만이 아니라 승인 적용 경로 전체가 막힌다.** 세 파트가 이 컬럼을 쓴다:
--       · gateway/approvals.py:132-134      UPDATE ... SET center_modified = COALESCE(...)   ← 쓰기
--       · orchestrator/approval_graph.py:79 MODIFIED_FIELD_MAP = {"center": "center_modified", …}
--       · agent_b_spc/approval_apply.py:41  SELECT … center_modified, ucl_modified, lcl_modified ← 읽기
--
--       읽는 쪽이 결정적이다. `_SELECT_PROPOSED`(approval_apply.py:37-46)가 세 컬럼을 **명시적으로
--       나열**하고 실행부(:93 conn.execute)에 try/except 가 없어, 컬럼이 없으면
--       psycopg2.errors.UndefinedColumn 으로 **SELECT 가 그대로 터진다** — :136 의 _coalesce 까지
--       가지도 못한다. 따라서 MODIFIED 만이 아니라 **평범한 APPROVE 도 적용되지 않고**,
--       승인 대기 건이 전부 못 나간다. (범위 확인: 팀원 B 실측 — #130 리뷰)
--
--       ⚠️ 두 겹으로 조용하다 — 승인 화면은 이미 "조치 나감" 이고 **CI 도 초록불**이다
--       (tests/test_approval_apply.py:33-39 가 컬럼 존재를 probe 해서 없으면 테스트를 skip).
--       화면과 실제가 갈리는 종류의 사고다.
--
-- 대상: `pg_data` 볼륨을 2026-07-23 이전에 만든 환경. 그 뒤 새로 만든 DB 는 init.sql 이
--       최신으로 돌아 정상이다.
--
-- 값의 의미 (init.sql:160-162 정본 승계):
--   center_modified  수정 승인 시 엔지니어 보정값. **원안(`center_after`)은 불변**이고
--                    B apply 가 `COALESCE(수정, 원안)` 으로 고른다. sigma 그룹은 center 만
--                    수정하고 밴드를 재산출한다.
--   ucl_modified     분위수(KEEP*) 그룹 전용 — 경계 직접·부분 수정. NULL = 해당 없음.
--   lcl_modified     위와 같음(하한).
--   셋 다 NULL = 수정 없음(원안 그대로 승인). 기존 행은 전부 NULL 이 되며 그 값이 곧
--   "수정 없이 승인된 건" 이라 의미상 정확하다.
--
-- 멱등: ADD COLUMN IF NOT EXISTS — 이미 적용된 DB(신규 볼륨)에 재실행해도 무해.
--       nullable + DEFAULT 없음이라 PG 11+ 에서는 카탈로그만 바뀐다(테이블 재작성 없음).
--
-- 롤백: ALTER TABLE limit_corrections
--         DROP COLUMN IF EXISTS center_modified, DROP COLUMN IF EXISTS ucl_modified,
--         DROP COLUMN IF EXISTS lcl_modified;
--
-- 실행:
--   docker cp db/migrations/0011_limit_corrections_modified_cols.sql fdc-postgres:/tmp/0011.sql
--   docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/0011.sql

ALTER TABLE limit_corrections
    ADD COLUMN IF NOT EXISTS center_modified FLOAT,
    ADD COLUMN IF NOT EXISTS ucl_modified    FLOAT,
    ADD COLUMN IF NOT EXISTS lcl_modified    FLOAT;

COMMENT ON COLUMN limit_corrections.center_modified IS
    'B5-1: 수정 승인(MODIFIED) 엔지니어 보정값 — 중심. 원안(center_after) 불변, B apply 가 COALESCE(수정, 원안). NULL=수정 없음';
COMMENT ON COLUMN limit_corrections.ucl_modified IS
    'B5-1: 수정 승인 보정값 — 상한. 분위수(KEEP*) 그룹 전용(경계 직접·부분 수정). NULL=해당 없음';
COMMENT ON COLUMN limit_corrections.lcl_modified IS
    'B5-1: 수정 승인 보정값 — 하한. 분위수(KEEP*) 그룹 전용. NULL=해당 없음';

-- 확인 쿼리 — 3행이 나와야 한다
SELECT column_name,
       data_type,
       is_nullable
  FROM information_schema.columns
 WHERE table_name  = 'limit_corrections'
   AND column_name IN ('center_modified', 'ucl_modified', 'lcl_modified')
 ORDER BY column_name;
-- 기대: center_modified | double precision | YES
--       lcl_modified    | double precision | YES
--       ucl_modified    | double precision | YES
