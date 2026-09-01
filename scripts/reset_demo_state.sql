-- 데모/게이트 시작 전 상태 초기화 — 운영 데이터만 비우고 **시딩 자산은 보존**한다.
--
-- 왜 필요한가: 리허설을 반복하면 옛 Incident 가 화면에 남는다. 2026-07-30 에는 노이즈
-- 버그 시절의 Incident(알람 925건·score 100)가 수정 후 결과와 섞여 보여, 화면 숫자와 DB
-- 숫자가 어긋나 읽히는 혼선이 실제로 났다. 게이트 시연 중에 같은 질문이 나오면 곤란하다.
--
-- ⚠️ `docker compose down -v` 를 쓰지 말 것 — 볼륨을 통째로 날려 **Qdrant KB(C 스냅샷)**
--    까지 사라진다. 이 스크립트는 Postgres 의 운영 테이블만 건드린다.
--
-- 실행 (PowerShell — **stdin 파이프 금지**):
--   docker cp scripts\reset_demo_state.sql fdc-postgres:/tmp/reset.sql
--   docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/reset.sql
--
-- Get-Content 파이프는 인코딩·개행이 변형돼 구문 오류를 만들 수 있고(2026-07-30 실측),
-- ON_ERROR_STOP=1 이 없으면 BEGIN 안의 에러가 조용히 **전체 롤백**으로 끝나 초기화가
-- 안 된 채 "완료"처럼 보인다.

BEGIN;

-- ── 보존 (건드리지 않음) ─────────────────────────────────────────────────
--   control_limits    B 시딩 관리선 — 지우면 재시딩 필요 (45센서 × 4챔버)
--   y_thresholds      B5-3 Y 임계
--   rag_documents     KB 본문
--   historical_cases  RAG 사례 자산

-- ── 1. 판정·사건 계열 ────────────────────────────────────────────────────
TRUNCATE TABLE incident_alerts, spc_violations, tttm_comparisons RESTART IDENTITY;
-- incident_alerts 는 위에서 이미 비움 (FK)
TRUNCATE TABLE incidents RESTART IDENTITY CASCADE;

-- ── 2. 승인·조치 계열 ────────────────────────────────────────────────────
-- release_briefs 추가 (2026-08-12 실측 — 0014 가 본 스크립트보다 늦게 신설돼 목록에 없었다):
--   리셋이 RESTART IDENTITY 라 새 회차 사건이 **같은 incident_id 를 재사용**하는데,
--   낡은 해제 Brief 가 남아 있으면 새 사건의 R9 화면(/requalify package)에 죽은 세계의
--   근거가 조인된다. 실측: 리셋 후 inc=0 인데 rls=1 잔존.
TRUNCATE TABLE approval_records, agent_reports, wafer_dispositions,
               recipe_corrections, ct_decisions, release_briefs RESTART IDENTITY;

-- limit_corrections 는 **재산정 이력**이다. 시딩이 남긴 initial 행은 남기고
-- 데모가 만든 것(periodic·incident·qual·provisional)만 지운다.
DELETE FROM limit_corrections WHERE trigger_type IS DISTINCT FROM 'initial';

-- ── 3. 예측·시뮬 계열 ────────────────────────────────────────────────────
TRUNCATE TABLE wafer_predictions, sim_events RESTART IDENTITY;
-- ⚠️ qual_snapshots 는 **지우고 재시딩한다** (2026-08-09 재개정 — #150 리뷰 ③, B).
--   8/8 정정("보존")은 절반만 맞았다: 판정의 기준선인 건 맞지만, **런타임에도 써진다** —
--   요란 PM 후 Phase 2 재수립이 새 스냅샷을 활성화한다(spc_consumer `regime-reestablish`).
--   보존하면 리허설을 돌릴수록 활성 기준선이 이동해 **회차 간 결과 비교가 불가능**해진다
--   (출발점이 다르면 개입 효과인지 출발점 차이인지 못 가린다). control_limits 도 보존이
--   아니라 spc-seed **재시딩**이므로, 같은 성격 = 같은 취급이 맞다.
--   재시딩 = 런북 A-2 의 seed_qual_snapshots 1줄 (#117 부트스트랩 · 재실행 멱등).
--   기준선 공백에도 거짓 PASS 는 없다 — qual_engine 이 UNKNOWN 을 낸다(qual_engine.py:76).
TRUNCATE TABLE qual_snapshots RESTART IDENTITY;
TRUNCATE TABLE quals RESTART IDENTITY CASCADE;

-- ※ 보존 대상 (의도적으로 지우지 않는 것) — 리셋은 "이번 회차의 판정·사건"만 지운다:
--     control_limits = 판정의 기준선 — 재시딩 주체는 compose spc-seed (여기서 지우지 않는다)
--     prior_transitions · prior_markers = 누적 원장 (A26) — 회차마다 지우면 전이가 영원히 n=1 이라
--                                        마커 재캘리가 원리적으로 불가능해진다. 같은 시나리오
--                                        재주입은 원장이 자연키로 막으므로 중복도 안 쌓인다.

-- ── 3-B. RTD 챔버 정지 해제 (2026-08-11 신설) ────────────────────────────
-- 왜 필요한가: RTD 자동 정지(헌법 1-1 예외 4)는 **해제가 requal 승인 하류만**이라 설계상
-- 자동으로 안 풀린다. 그 규율은 운영에서는 옳지만, **리허설 회차 경계에서는 정지가 그대로
-- 살아남아 다음 회차가 그 챔버 없이 시작**한다. 정지된 챔버는 wafer 자체가 안 나오므로
-- 판정·알람·TTTM 이 통째로 0 이 되고, 로그에는 그 챔버만 조용히 빠진다 — 에러가 없어서
-- 발견이 늦다 (2026-08-11 실측: SIM_CH_2 가 19:38 정지 → 회차 초기화 후에도 죽은 채였고,
--  프리롤 스톰 대상 챔버라 대본 첫 컷이 성립하지 않았다. 원인 규명에 40분).
--
-- ⚠️ 이것은 **회차 초기화 전용 해제**다. 운영 경로(R9 승인)를 대체하지 않는다 —
--    released_by 를 'demo_reset' 으로 남겨 감사에서 사람 승인분과 구분된다.
UPDATE chamber_inhibits
   SET released_at = NOW(), released_by = 'demo_reset', release_qual_id = 'RESET'
 WHERE released_at IS NULL;
-- ⚠️ DB 만으로는 부족하다 — 시뮬레이터가 읽는 신호 파일도 지워야 생산이 재개된다:
--    PowerShell:  Remove-Item control\inhibit\*.json -ErrorAction SilentlyContinue
--    (런북 A-2 에 같은 줄 추가할 것. 파일을 안 지우면 DB 는 해제인데 챔버는 계속 멈춰 있다)

-- ── 4. LangGraph 체크포인터 ──────────────────────────────────────────────
-- 승인 thread 상태. 안 지우면 옛 thread 가 남아 같은 incident_id 재사용 시
-- "이미 닫힌 thread" 로 409 가 난다 (유령 성공 가드가 정상 동작한 결과지만
-- 데모에서는 새 사건이 안 열리는 것처럼 보인다).
DO $$
BEGIN
  -- TRUNCATE 에는 IF EXISTS 가 없어 to_regclass 로 존재를 확인한다
  IF to_regclass('public.checkpoints') IS NOT NULL THEN
    TRUNCATE TABLE checkpoint_writes, checkpoint_blobs, checkpoints;
  END IF;
END $$;

-- ── 5. 관리선을 **시딩 상태(initial)로 원복** — 매 리허설 같은 출발선 ──────────
-- 두 가지를 한 번에 되돌린다.
--   ⓐ 가한계(provisional) 잔류 — 남아 있으면 그 챔버가 광폭 기준선으로 시작해 알람이
--      안 뜨고 시나리오가 죽는다.
--   ⓑ **승인 누적 표류** — 2026-07-30 실측: SIM_CH_3/C11 이 v1(-273.48) → v12(-141.53)
--      으로 11회 재설정을 거치며 한 방향으로 3.7σ 이동해 있었다. 매 단계는 0.12σ 라
--      단건 상한에 안 걸렸지만 누적이 컸다. 그 상태로 시연하면 정상 데이터가 이미 LCL
--      아래에 있어 알람이 상시로 뜨고, 주입 효과와 구분되지 않는다.
--
-- 순서 주의: 부분 유니크 인덱스 idx_control_limits_active_one 이 그룹당 active 1행만
-- 허용하므로 **전부 내린 뒤 initial 만 올린다**.
--
-- ⚠️ 문 안에는 주석을 넣지 않는다 (2026-07-30: 인라인 주석이 다음 줄을 삼켜 구문 오류).

UPDATE control_limits SET is_active = FALSE WHERE is_active;

UPDATE control_limits c SET is_active = TRUE
 WHERE c.id IN (
        SELECT DISTINCT ON (chamber_id, recipe_id, step, sensor_window, sensor_id) id
          FROM control_limits
         WHERE trigger_type = 'initial'
         ORDER BY chamber_id, recipe_id, step, sensor_window, sensor_id,
                  effective_from ASC, id ASC
       );

UPDATE control_limits c SET is_active = TRUE
 WHERE c.id IN (
        SELECT DISTINCT ON (chamber_id, recipe_id, step, sensor_window, sensor_id) id
          FROM control_limits
         ORDER BY chamber_id, recipe_id, step, sensor_window, sensor_id,
                  effective_from ASC, id ASC
       )
   AND NOT EXISTS (
        SELECT 1 FROM control_limits o
         WHERE o.is_active
           AND o.chamber_id = c.chamber_id AND o.recipe_id = c.recipe_id
           AND o.step = c.step AND o.sensor_window = c.sensor_window
           AND o.sensor_id = c.sensor_id
       );

COMMIT;

-- ── 확인 ─────────────────────────────────────────────────────────────────
SELECT 'incidents' t, count(*) n FROM incidents
UNION ALL SELECT 'incident_alerts', count(*) FROM incident_alerts
UNION ALL SELECT 'spc_violations', count(*) FROM spc_violations
UNION ALL SELECT 'approval_records', count(*) FROM approval_records
UNION ALL SELECT 'agent_reports', count(*) FROM agent_reports
UNION ALL SELECT 'wafer_dispositions', count(*) FROM wafer_dispositions
UNION ALL SELECT '--- 보존 확인 ---', NULL
UNION ALL SELECT 'control_limits (보존)', count(*) FROM control_limits
UNION ALL SELECT 'control_limits active', count(*) FROM control_limits WHERE is_active
UNION ALL SELECT 'y_thresholds (보존)', count(*) FROM y_thresholds
ORDER BY 1;

-- ── 가한계 복구 결과 ─────────────────────────────────────────────────────
-- provisional_active 는 **0**, active 는 그룹 수(45센서 × 4챔버 = 180)여야 한다.
SELECT trigger_type, count(*) AS active_rows
  FROM control_limits WHERE is_active GROUP BY 1 ORDER BY 1;

-- 표류 점검 — active 가 그 그룹의 **가장 오래된 버전**이 아니면 원복이 안 된 것이다.
-- 0행이어야 정상.
SELECT a.chamber_id, a.sensor_id, a.step, a.limit_version, a.trigger_type
  FROM control_limits a
 WHERE a.is_active AND a.trigger_type <> 'initial'
 ORDER BY 1, 2 LIMIT 20;

-- active 가 비어버린 그룹 점검 — 0행이어야 정상 (있으면 그 센서는 판정에서 빠진다)
SELECT c.chamber_id, c.sensor_id, c.step, c.sensor_window
  FROM (SELECT DISTINCT chamber_id, recipe_id, step, sensor_window, sensor_id
          FROM control_limits) c
 WHERE NOT EXISTS (SELECT 1 FROM control_limits a
                    WHERE a.is_active
                      AND a.chamber_id = c.chamber_id AND a.recipe_id = c.recipe_id
                      AND a.step = c.step AND a.sensor_window = c.sensor_window
                      AND a.sensor_id = c.sensor_id)
 ORDER BY 1, 2 LIMIT 20;

-- ℹ️ B 상태머신의 **인메모리 phase** 는 이 스크립트가 못 되돌린다 —
--    reset 후에는 b-spc 를 반드시 재시작할 것 (S11 [모두 정지] → [서비스 모두 시작]).
