-- 0013_prior_transitions.sql — 프라이어 전이 원장 + 마커 버전 (A26 · BL2)
--   적용: docker exec -i fdc-postgres psql -U fdc_admin -d fdc_platform < 이 파일
--   재적용 무해 (CREATE TABLE IF NOT EXISTS · INSERT ON CONFLICT DO NOTHING).

-- 18. 프라이어 전이 원장 (BL2 · 신규 결정 9) ------------------------------------
--   요란 PM 전이마다 1행. "라벨-프리 레벨 프라이어"의 재캘리브레이션 재료다
--   (사이클_정의_v1 §5 결정 9 · F17). 추정은 전이 시점에 INSERT, 실측(ΔY 점근)은
--   D+60 라벨 도착 후 UPDATE — 한 행이 추정→실측으로 닫히며 오차가 확정된다.
--   n=1 앵커가 회귀로 성장하는 구조라 **원장 없이는 마커 개정이 불가능**하다.
CREATE TABLE IF NOT EXISTS prior_transitions (
    transition_id   VARCHAR(48)  PRIMARY KEY,          -- PRI-YYYYMMDD-<CHAMBER>-<SEQ> (6-4)
    chamber_id      VARCHAR(32)  NOT NULL,
    incident_id     VARCHAR(48),                       -- regime_transition Incident (BL1)
    qual_id         VARCHAR(48),                       -- D+0 Qual (C17 읽기 출처)
    verdict_at      TIMESTAMPTZ  NOT NULL,             -- 요란 판정 시각 = 전이 기준점
    c17_delta       DOUBLE PRECISION NOT NULL,         -- 주채널 ⓐ: Qual 5장 평균 − 직전 레짐 평균 (자 = delta_basis 컬럼과 세트)
    c11_wfmin_delta DOUBLE PRECISION,                  -- 교차확인 ⓒ (마커 v2 후보축)
    c12_delta       DOUBLE PRECISION,                  -- 교차확인 ⓒ (step6 평균 — ⓟ2)
    delta_basis     VARCHAR(24)  NOT NULL DEFAULT 'qual5-seg1',
                                                       -- Δ채널의 자: 'qual5-seg1'(정본 — 앵커 −6.96 이 이 자로 캘리브레이션됨,
                                                       --   전체평균 기준은 계통 편향 −17%로 기각·직전300 기준은 시나리오 전용).
                                                       --   ⚠ 자가 다른 행은 refit 에서 섞지 않는다 (헌법 7장 "파생 수치는 자와 세트로")
    marker_version  VARCHAR(16)  NOT NULL,             -- 추정에 쓴 마커 (prior_markers.version)
    y_est           DOUBLE PRECISION NOT NULL,         -- 추정 ΔY = slope × delta
    y_actual        DOUBLE PRECISION,                  -- 실측 ΔY **점근 자 고정** — 자는 y_basis 컬럼. 초기 과도 몫은 템플릿(τ·A) 소관이라 여기 섞지 않는다 (8/8 실측: 창에 따라 +364~+968 — 자 혼합이 "v1 오차 72%" 오독의 뿌리)
    y_basis         VARCHAR(24)  NOT NULL DEFAULT 'last1000_mean',
                                                       -- 점근의 조작적 정의(2026-08-09 PM 확정 ②a): 'last1000_mean' = 마지막
                                                       --   1,000장 창 평균·hour 보정(ⓟ3) — **정본**. 감쇠 적합 상수항은 fit_c 에 병기(참고).
    fit_c           DOUBLE PRECISION,                  -- 감쇠 적합 C+A·e^(−t/τ) 의 C (참고 — 8/9 실측: last1000 +364 vs fit_c +383, 19 차이가 자 명기 의무의 실증)
    y_actual_at     TIMESTAMPTZ,
    err_pct         DOUBLE PRECISION,                  -- |est−actual|/|actual|×100 (실측 UPDATE 시 산출)
    tau_days        DOUBLE PRECISION,                  -- 과도 몫 ⓑ 감쇠 템플릿 τ
    amplitude       DOUBLE PRECISION,                  -- 과도 초기 진폭 A
    hour_adjusted   BOOLEAN      NOT NULL DEFAULT FALSE,  -- ⓟ3: Y 는 hour 보정 후 기록
    source          VARCHAR(12)  NOT NULL DEFAULT 'live', -- 'live' | 'injected'(실주입) | 'synthetic'(시나리오 정의만) — 주입 공개 원칙
    -- ▼ 재생 불변성 (2026-08-08) — 같은 시나리오를 리허설마다 다시 주입해도 **증거가 늘지 않는다**.
    --   원장의 불변식은 "사건 1건 = 1행"이다. 재생 시각으로 키를 잡으면 같은 전이가 회차마다
    --   새 행이 되어 회귀에 가중이 붙고 카드의 원장 건수가 부풀려진다 (실측 8/8: 같은 점 3행).
    --   자연키는 **전이의 정체**다 — 시뮬 = scenario_id, 실운전 = incident_id.
    --   ※ 값이 같으면 새 정보가 0 이므로 계수가 안 바뀌는 것이 옳다. 데모에서 원장이 움직이는
    --     것을 보이려면 **다른 전이**(다른 강도 시나리오)를 주입한다 — 연출이 아니라 실제로
    --     다른 사건이기 때문이다. 실운전은 incident_id 가 매번 달라 축적이 막히지 않는다.
    source_ref      VARCHAR(64)  NOT NULL,               -- 자연키 (UNIQUE)
    observed_count  INTEGER      NOT NULL DEFAULT 1,     -- 재생 횟수 (증거 아님 · 위생 기록)
    last_seen_at    TIMESTAMPTZ,                         -- 마지막 재생 시각
    note            TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prior_transitions_chamber ON prior_transitions (chamber_id, verdict_at DESC);
CREATE INDEX IF NOT EXISTS idx_prior_transitions_source ON prior_transitions (source);
-- 자연키 유일성 — 재주입이 새 행을 만들지 못하게 DB 가 막는다
CREATE UNIQUE INDEX IF NOT EXISTS uq_prior_transitions_ref ON prior_transitions (source_ref);
-- 기존 설치 DB 보강 (0013 을 8/8 에 이미 적용한 로컬 — CREATE IF NOT EXISTS 는 컬럼을 못 늘린다)
ALTER TABLE prior_transitions ADD COLUMN IF NOT EXISTS delta_basis VARCHAR(24) NOT NULL DEFAULT 'qual5-seg1';
ALTER TABLE prior_transitions ADD COLUMN IF NOT EXISTS y_basis     VARCHAR(24) NOT NULL DEFAULT 'last1000_mean';
ALTER TABLE prior_transitions ADD COLUMN IF NOT EXISTS fit_c       DOUBLE PRECISION;
-- 8/8 에 자 컬럼 없이 적재된 로컬 행 정합: 당시 행은 전부 시뮬 세계(정의값이 그 세계의 자)다.
--   위 ALTER 의 DEFAULT('qual5-seg1')가 시뮬 행에 실자 딱지를 붙이는 것을 되돌린다 —
--   이대로 두면 refit 의 "자 일치" 그룹에 시뮬 행이 섞인다(자 규율 위반, 8/8 "72%→1.23%" 사고 재발).
--   신규 행은 적재 코드가 자를 명시하므로 created_at 가드로 재적용 시 미접촉 (멱등).
UPDATE prior_transitions SET delta_basis = 'scenario_def', y_basis = 'scenario_def'
 WHERE source IN ('synthetic', 'injected')
   AND delta_basis = 'qual5-seg1'
   AND created_at < TIMESTAMPTZ '2026-08-09 00:00:00+09';

-- 19. 프라이어 마커 버전 (개정 이력) ---------------------------------------------
--   마커 = "무슨 채널로 레벨을 추정하나". v1 = C17 단독(앵커 −6.96), 개정 후보 = C17+C11.
--   개정은 **자동 금지 · 승인 경유**(제안 카드 → approval_records) — 물리 미개입이라
--   correction 은 발행하지 않고, 롤백은 직전 버전 재활성화다.
CREATE TABLE IF NOT EXISTS prior_markers (
    version         VARCHAR(16)  PRIMARY KEY,          -- v1, v2 ...
    channels        JSONB        NOT NULL,             -- ["C17"] | ["C17","C11_wf_min"]
    coefs           JSONB        NOT NULL,             -- {"C17": -6.96, "intercept": 0}
    basis           VARCHAR(48)  NOT NULL DEFAULT 'qual5-seg1 -> last1000_mean',
                                                       -- 계수가 유효한 자 조합 (Δ자 → Y자). **다른 자의 원장 행으로 refit 금지** —
                                                       --   "v1 72.1% → 재캘리 1.23%" 가 자 불일치 비교였던 사고(8/8)의 재발 방지 컬럼
    fitted_n        INTEGER      NOT NULL DEFAULT 0,   -- 적합에 쓴 원장 건수
    err_pct         DOUBLE PRECISION,                  -- 적합 시점 원장 평균 오차
    is_active       BOOLEAN      NOT NULL DEFAULT FALSE,
    approval_id     VARCHAR(48),                       -- 개정 승인 기록 (헌법 1-1)
    changelog_ref   TEXT,                              -- models/CHANGELOG.md 항목
    note            TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    activated_at    TIMESTAMPTZ
);
-- 활성 마커는 항상 1개 (부분 유니크 — 두 마커가 동시에 활성이면 추정이 갈린다)
CREATE UNIQUE INDEX IF NOT EXISTS uq_prior_markers_active ON prior_markers (is_active) WHERE is_active;

-- 기존 설치 DB 보강 (위 ALTER 와 동일 사유)
ALTER TABLE prior_markers ADD COLUMN IF NOT EXISTS basis VARCHAR(48) NOT NULL DEFAULT 'qual5-seg1 -> last1000_mean';

-- v1 시드 — F17 검증분 (Qual 기준 캘리브레이션 앵커). 전체평균 기준은 계통 편향 −17%로 기각됨.
-- 검산: −6.96 × Δqual5(−52.30) = +364 ≈ 점근 last1000(+364) — 자 정합 (2026-08-09 확정).
INSERT INTO prior_markers (version, channels, coefs, basis, fitted_n, is_active, note, activated_at)
VALUES ('v1', '["C17"]'::jsonb, '{"C17": -6.96, "intercept": 0}'::jsonb,
        'qual5-seg1 -> last1000_mean', 1, TRUE,
        'F17 전이 앵커 기울기 (관측 전이 1건 · 인샘플) — 사이클_정의_v1 결정 9 ⓐ', NOW())
ON CONFLICT (version) DO NOTHING;
-- 8/8 에 basis 없이 시드된 로컬 행 정합 (신규 설치는 no-op)
UPDATE prior_markers SET basis = 'qual5-seg1 -> last1000_mean'
 WHERE version = 'v1' AND basis IS DISTINCT FROM 'qual5-seg1 -> last1000_mean';
