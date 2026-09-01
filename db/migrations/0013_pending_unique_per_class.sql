-- 0013 · 승인 PENDING 유니크를 **클래스별**로 (2026-08-11 · 헌법 1-4 예외의 DB 층)
--
-- 배경: 0001 이 「Incident 당 PENDING 1건」을 부분 유니크 인덱스로 강제했다. 8/11 PM 결정으로
--   처분(disposition)이 본선(정비/보정) 카드와 **공존**하게 되면서 이 인덱스가 처분 행 INSERT 를
--   23505 로 막았고, `open_pending` 은 그 예외를 "다른 요청이 선점" 으로 삼켜(정상 동작) 카드가
--   조용히 안 열렸다 — 라우트는 200 을 돌려주는데 `approval_records` 는 4행 그대로였다(실측).
--
-- 조치: 유니크 키에 **클래스 축**을 넣는다. `(request_type = 'disposition')` 불리언 식이 축이다.
--   · 본선 PENDING 1건 + 처분 PENDING 1건  → 허용 (공존)
--   · 같은 클래스 2건                        → 여전히 차단 (멱등 방어 유지 · interrupt 재실행 대비)
--
-- 안전: 기존 인덱스 이름을 모르므로 「UNIQUE + PENDING 조건」인 인덱스를 찾아 지운다. PK 와
--   비유니크 조회 인덱스(idx_approval_records_status·_incident)는 조건에 안 걸려 그대로 남는다.
--   멱등 — 재실행해도 무해하다.

DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT indexname
          FROM pg_indexes
         WHERE schemaname = current_schema()
           AND tablename  = 'approval_records'
           AND indexdef ILIKE '%UNIQUE%'
           AND indexdef ILIKE '%PENDING%'
    LOOP
        RAISE NOTICE '구 PENDING 유니크 인덱스 제거: %', r.indexname;
        EXECUTE format('DROP INDEX IF EXISTS %I', r.indexname);
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_pending_per_class
    ON approval_records (incident_id, (request_type = 'disposition'))
 WHERE status = 'PENDING';

-- 검증 쿼리 (수동):
--   SELECT indexname, indexdef FROM pg_indexes WHERE tablename='approval_records';
--   → uq_approval_pending_per_class 가 보이고, 구 유니크는 없어야 한다.
