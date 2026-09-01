-- 0012_wafer_predictions_ct0_bias.sql
-- wafer_predictions.bias_applied · pm_count 추가 (CT⓪ Model R2R — 2026-08-07, A)
--   ※ 최초 `0007` 로 땄다가 08-08 dev 머지에서 재번호 (0007 = C 컨테이너화 브랜치의
--     incidents.incident_type 실사용분. README "번호 재조정 이력" 참조). 멱등이라
--     0007 로 이미 적용한 DB 는 재적용해도 무해.
--
-- [왜]
--   설계 정본 = docs/CT0_ModelR2R_설계방향_v1.md §12-3.
--   ① bias_applied FLOAT — 발행된 예측에서 **보정 전 값을 복원**하는 유일한 수단이다
--      (`raw = predicted_c65 − bias_applied`, 설계 D1). bias 추정은 항상 raw 예측 대비
--      잔차로 해야 하는데(원칙 5), 이 컬럼이 없으면 보정된 예측 대비 잔차로 갱신돼
--      **60일 지연 하의 되먹임 루프**가 된다. 감사 시 분해 가능성도 여기서 나온다.
--   ② pm_count INTEGER — 창 편입 **자격 필터의 축**이다 (설계 §5). 요란 판정 PM 경계
--      이후의 라벨만 창에 들어가야 하는데, 재기동·리밸런스 때의 창 재계산(§7)은 DB 만
--      보고 판단하므로 라벨의 pm_count 가 행에 남아 있어야 한다. 시각 비교로 대신하면
--      이벤트 순서 역전·tz 함정이 되살아난다(정수 비교를 택한 이유가 그것이다).
--      ⚠ 설계 §7 의 부트스트랩 SQL 이 전제한 컬럼이며, 현 스키마에 없어서 이번에 붙인다.
--
--   init.sql 은 CREATE TABLE IF NOT EXISTS + compose 데이터 디렉토리 최초 1회만 실행이라
--   이미 떠 있는 DB 에는 컬럼이 붙지 않는다 → 이 마이그레이션으로 기존 DB 경로를 이행한다
--   (헌법 3-2 · db/migrations/README.md).
--
-- [값의 의미]
--   bias_applied = 그 wafer 의 발행 예측에 실제로 가산된 CT⓪ bias (C65 개수, round 2).
--                  0    = 보정 경로는 살아 있으나 가산 없음 (shadow·표본 미달·Qual wafer)
--                  NULL = 보정 경로 자체가 없던 시기의 행 (mode=off — 읽는 쪽은 0 으로 해석)
--   pm_count     = fdc.actual 이 실어온 그 wafer 의 PM 카운트 (계약 §1-B). NULL = 미도착·구행.
--
-- [적용 순서 — 중요]
--   이 마이그레이션이 **코드보다 먼저** 나가야 한다. prediction_sink 가 두 컬럼에 INSERT/
--   UPDATE 하므로, 컬럼이 없으면 라이브 적재가 UndefinedColumn 으로 끊긴다
--   (README "새 컬럼을 읽는 코드보다 마이그레이션이 먼저").
--
-- [주의]
--   멱등. 재실행 안전. 컬럼 물리 순서는 신규 설치(init.sql)와 달리 맨 뒤에 붙는다 —
--   코드는 이름으로만 접근하므로 무해(SELECT * 눈비교 금지).
--
-- [실행]
--   docker cp db/migrations/0012_wafer_predictions_ct0_bias.sql fdc-postgres:/tmp/0012.sql
--   docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/0012.sql

ALTER TABLE wafer_predictions
    ADD COLUMN IF NOT EXISTS bias_applied FLOAT;

ALTER TABLE wafer_predictions
    ADD COLUMN IF NOT EXISTS pm_count INTEGER;

-- CT⓪ 부트스트랩 재계산(§7)의 조회 축 — 챔버별 최신 라벨 N건.
-- 부분 인덱스인 이유: 라벨은 전체 행의 일부(60일 지연)라 IS NOT NULL 조건이 선택도를 준다.
CREATE INDEX IF NOT EXISTS idx_wafer_predictions_label
    ON wafer_predictions (chamber_id, measured_at DESC)
 WHERE actual_c65 IS NOT NULL;

-- 확인 쿼리 — 2행이 나와야 한다
SELECT column_name,
       data_type,
       is_nullable
  FROM information_schema.columns
 WHERE table_name = 'wafer_predictions'
   AND column_name IN ('bias_applied', 'pm_count')
 ORDER BY column_name;
-- 기대: bias_applied | double precision | YES
--       pm_count     | integer          | YES
