-- 0001_approval_pending_unique.sql
-- WP-B7 (R8) — approval_records: Incident당 PENDING 1행 DB 강제 (헌법 1-4 "동시 pending 1건")
--
-- 상태: **적용 완료 (2026-08-03, PM 로컬).** ① 위반 조회 0건 확인 → ② 인덱스 생성 →
--   ③ 앱 가드 정합 완료(같은 날). init.sql(PM 소유)은 건드리지 않는다.
--   **팀원 DB 에는 자동 반영되지 않는다** — 각자 `git pull` 후 psql -f 로 1회 적용.
--   재적용은 무해하다(가드 + IF NOT EXISTS).
--
-- 배경: src/orchestrator/ct2_deploy_approval.open_ct2_pending 등은 check-then-insert 패턴이라
--   동시 호출 시 PENDING 2행 레이스가 가능하다. 애플리케이션 가드는 유지하되, DB가 최종
--   방어선이 되도록 부분 유니크 인덱스를 건다. 삽입 실패는 IntegrityError로 잡아 False 반환.
--
-- ⚠️ 파급 (반드시 검토): 이 인덱스는 CT²만이 아니라 **본편 Incident 승인 전체**
--   (request_type='requalify'·'ct2_deploy' 등 모든 타입)에 적용된다. 헌법 1-4의 DB 강제이므로
--   원칙적으로 정합이지만, 기존 데이터에 위반 행이 있으면 인덱스 생성 자체가 실패한다.
--   그래서 ② 인덱스 생성 전에 ①로 위반이 0건임을 보장한다.
--
-- 롤백: DROP INDEX IF EXISTS uq_approval_pending_per_incident;

-- ── ① 위반 조회 (적용 전 필수 — 결과가 0행이어야 ②로 진행) ─────────────────────
-- 수동 실행:
--   SELECT incident_id, count(*) AS pending_rows
--     FROM approval_records WHERE status = 'PENDING'
--    GROUP BY incident_id HAVING count(*) > 1;
-- → 0행이면 위반 없음. 행이 나오면 그 Incident들을 먼저 정리(중복 PENDING resolve)한 뒤 재시도.

-- ── ② 위반 가드 + 부분 유니크 인덱스 (원자적) ─────────────────────────────────
DO $$
DECLARE
    v_count integer;
BEGIN
    SELECT count(*) INTO v_count
      FROM (
          SELECT incident_id
            FROM approval_records
           WHERE status = 'PENDING'
           GROUP BY incident_id
          HAVING count(*) > 1
      ) dup;

    IF v_count > 0 THEN
        RAISE EXCEPTION
          '중단: Incident % 건에 PENDING 중복 존재 — 인덱스 생성 불가. ① 위반 조회로 정리 후 재시도.',
          v_count;
    END IF;

    -- 부분 유니크: status='PENDING' 행만 incident_id 유일. 종결(APPROVED/REJECTED 등)은 다건 허용.
    CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_pending_per_incident
        ON approval_records (incident_id)
        WHERE status = 'PENDING';

    RAISE NOTICE 'uq_approval_pending_per_incident 생성 완료 (위반 0건 확인).';
END $$;

-- ── ③ 애플리케이션 정합 — **완료 (2026-08-03)** ───────────────────────────────
-- 인덱스 적용 후, open_ct2_pending / 본편 open_pending 은 INSERT의 psycopg2 IntegrityError를
-- 잡아 False(=이미 PENDING 존재)로 환원해야 한다. check-then-insert의 SELECT 가드는 유지
-- (빠른 경로) — 인덱스는 레이스 창에서만 발동하는 최종 방어선이다.
--
-- 반영 위치:
--   · approval_graph.open_pending          — 07-31 (인덱스 도입 선행 조건으로 먼저 반영)
--   · ct2_deploy_approval.open_ct2_pending — **08-03** (이게 남아 있었다)
-- 둘 다 pgcode == '23505' 로 판별한다(psycopg2 를 import 하지 않아 테스트 격리 유지).
--
-- ⚠️ **환원 없이 인덱스만 걸면 도입 전보다 나쁘다.** 경합에서 진 호출이 False 가 아니라
-- 예외로 터져 승인 노드가 통째로 죽는다. 인덱스를 먼저 건 DB 가 있다면 이 커밋을 반드시 받을 것.
