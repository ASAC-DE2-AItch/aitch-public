-- 0010: quals.confirmed_verdict · confirmed_at — P6-1(2026-07-28) 소급 마이그레이션
--
-- 경위: P6-1 이 init.sql 에 두 컬럼을 넣고 쓰기(qual_recorder)·테스트까지 구현했으나,
--       헌법 3-2(마이그레이션 동반 의무)가 7/31 신설이라 **사흘 앞선 작업이라 파일이 빠졌다.**
--       0007(incidents.incident_type — C 발굴 2026-08-07)과 같은 날·같은 계열 구멍.
--       구 볼륨(7/28 이전 생성) DB 는 컬럼이 없어 Qual 확정 UPDATE 가 실패한다
--       (테스트는 skip 가드로 피하지만 라이브는 안 피한다).
--
-- 용도: 확정 verdict('loud'/'quiet') 영속 — QualVerdictConfirmed 이벤트 유실 대비
--       재시작 복구 소스(계약 §8-B 갭 해소) + CT⓪ 부트스트랩 경계 조회(설계 §7).
--
-- 멱등: IF NOT EXISTS — 이미 적용된 DB(신규 볼륨)에 재실행해도 무해.

ALTER TABLE quals
    ADD COLUMN IF NOT EXISTS confirmed_verdict VARCHAR(8),
    ADD COLUMN IF NOT EXISTS confirmed_at      TIMESTAMPTZ;

COMMENT ON COLUMN quals.confirmed_verdict IS
    'P6-1: 확정 verdict — loud/quiet. NULL=미확정. 쓰기 = qual_recorder(B) 단일';
COMMENT ON COLUMN quals.confirmed_at IS
    'P6-1: verdict 확정 시각 (QualVerdictConfirmed.confirmed_at 동일값)';
