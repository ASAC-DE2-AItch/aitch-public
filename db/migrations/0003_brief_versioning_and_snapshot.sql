-- 0003 — 서사층/증거층 분리를 위한 컬럼 3종 (2026-07-31, PM)
--
-- 근거: PR #70 의 "알람 직렬 처리 결정" — Brief 를 서사층(LLM 1회, 얼린다)과
-- 증거층(화면이 조회 시점에 읽는다, 안 얼린다)으로 나눈다. C 파트 1·4번이 이 컬럼들을
-- 기다리느라 멈춰 있었다 (LLM 호출 21회 → 1회, 마지막 알람 체감 6분 45초 → 35초).
--
-- 헌법 3-2: 컬럼 추가(ALTER)만 — 삭제·개명 없음.
-- 실행·규약: db/migrations/README.md 참조.

-- ── 서사층: Brief 재생성은 새 행이 아니라 같은 행 UPDATE + 버전 증가 ──────────
--
-- brief_version      : 1 부터. 재생성마다 +1. 구 행은 NULL 이므로 소비 코드는
--                      COALESCE(brief_version, 1) 로 읽는다.
-- covered_signatures : 이 Brief 가 이미 설명한 위반 시그니처 집합.
--                      형태 = (sensor, rule_id) 쌍의 JSONB 배열 — 예:
--                        [["C11","N3"], ["C12","N1"]]
--                      새 알람의 시그니처가 이 집합 안이면 LLM 미가동(skip),
--                      밖이면 skip 하되 needs_reanalysis 를 세운다 — 그루퍼가 새 문제를
--                      기존 Incident 에 흡수했을 때 Brief 가 조용히 안 나오는 경로를 막는다.
--                      B9 crazy 마커(rule_id='B9' · sensor='C65')는 합성 마커라 집합에서
--                      제외한다 (tools/base.py 판별식과 같은 규칙 — C 제안 채택).
ALTER TABLE agent_reports
    ADD COLUMN IF NOT EXISTS brief_version      INT,
    ADD COLUMN IF NOT EXISTS covered_signatures JSONB,
    ADD COLUMN IF NOT EXISTS needs_reanalysis   BOOLEAN NOT NULL DEFAULT FALSE;

-- ── 증거층: 감사 기준을 '생성 시점' 에서 '결정 시점' 으로 ────────────────────
--
-- evidence_snapshot : 엔지니어가 승인/반려를 누른 **그 순간** 화면이 보여주던 증거층을
--                     그대로 굳힌다. 지금까지의 감사 근거는 Brief 생성 시점이라 그 뒤
--                     늘어난 알람을 보고 결정했어도 기록에는 없었다 — 이 컬럼이 그
--                     "무엇을 보고 결정했는가"를 결정 시점 기준으로 보증한다.
ALTER TABLE approval_records
    ADD COLUMN IF NOT EXISTS evidence_snapshot JSONB;

-- 확인 — 4행이 나와야 정상
SELECT table_name, column_name, data_type FROM information_schema.columns
 WHERE (table_name = 'agent_reports'
        AND column_name IN ('brief_version', 'covered_signatures', 'needs_reanalysis'))
    OR (table_name = 'approval_records' AND column_name = 'evidence_snapshot')
 ORDER BY 1, 2;
