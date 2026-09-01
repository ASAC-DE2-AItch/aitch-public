# control_limits v1 시딩 + 로더 (B3-2) — 설계 스펙

- **작성**: 팀원 B (팀원 B), 2026-07-13
- **task**: B3-2 (control_limits v1 시딩 + DB→limits 로더) — 선행 B2-1(baseline 재생성)·B3-1(Nelson 엔진 조합②) 완료
- **DoD**: 감시 45그룹 × `len(offsets)`챔버 = **현 270행** 멱등 시딩 + 로더 왕복 통합테스트 통과 + transform 단위테스트 그린
- **상태**: 설계 (브레인스토밍 3결정 확정 → 구현 계획)
- **개정 v2 (2026-07-13, 스펙 리뷰)**: 하드코딩 제거(`k_sigma`·`q_low`·`q_high`는 CSV carry, `limit_version`·`trigger_type`은 명명 상수 — 헌법 6-1) · `qa_flags` NaN→NULL + numpy 타입 coercion · **노이즈–σ 근사 명문화** · 헌법 3-1(DML-only)·4-1 정당화 보강 · hermetic 통합테스트(`time_gap_hours` 주입) · 행수=`45×len(offsets)` · dry-run `logging`.
- **개정 v3 (2026-07-13, 3차 리뷰)**: **group_key 타입 계약 명시**(§4 — 런타임 Point와 정합해야 엔진 매칭, 최대 구멍) · **가드 ⓒ 강한 술어로 강화**(§5) · Header DoD·§2 표기 정합 · Task 4 pg 볼륨 운영 주의 · 노이즈 근사 정밀도.
- **개정 v4 (2026-07-13)**: 가드 함수명 `assert_no_active_v2`→`assert_no_conflicting_active`(강화된 동작과 정합, 플랜 v4 sync).
- **결정 이력 (2026-07-13 브레인스토밍)**:
  - **① 시딩 범위 = 감시 그룹만** (KEEP 39 + KEEP\* 6 = 45 × 6챔버 = 270행). EXCLUDE·EXCLUDE?는 미적재. 근거: control_limits는 "엔진이 조회할 현행 관리선" 스토어라 EXCLUDE는 정체성 밖 / DDL에 `decision` 컬럼 없어 is_active 오버로드 회피 / 깨진 관리선(zero_sigma·wide) NOT NULL 상주 방지 / 감사 이력은 whitelist CSV·qa_report에 이미 존재.
  - **② 스코프 = 시드 + DB→limits 로더 + 왕복 통합테스트 한 세트.** ①로 `is_active` 행 = 감시집합이 성립해 로더가 limits dict + whitelist set을 단일소스로 도출 → B3-1 엔진 계약(1번)을 실데이터로 첫 왕복 검증.
  - **③ 재시딩 = delete-then-insert (멱등, 트랜잭션).** 초기셋 정정이 잦은 국면(C54/56·오프셋 재생성)에 단순·partial-unique 충돌 없음. FK 참조 없어 id 변경 무해.

---

## 1. 목적 & 범위

B2-1이 산출한 **정적 초기 실력치 v1**(단일 챔버 69그룹)을 시뮬레이터의 **6가상 챔버(SIM_CH_1~6)로 복제·오프셋 shift**하여 `control_limits` 테이블에 `trigger_type='initial'`·`limit_version='v1'`·`is_active=true`로 적재하고, 엔진(B3-1)이 읽을 **DB→limits 로더**를 제공한다.

### 범위 경계 (In / Out)

| In (이 설계) | Out (별도 task) |
|---|---|
| initial_limits_v1.csv ⋈ whitelist 조인 | 동적 실력치 재산정 (B4-3) |
| 감시(KEEP/KEEP\*) 필터 → 45그룹 | fdc.correction 구독 → 한계 갱신 (B5-1) |
| 6챔버 복제 + 센서별 오프셋 shift | 라이브 fdc.raw·alert 배선 (B4-1) |
| method별 컬럼 매핑 (sigma/quantile) | 가한계(provisional) 시딩 (B6-3) |
| 멱등 적재 (delete-then-insert) | spc_violations·tttm 적재 (B3-2 별건) |
| DB → limits dict + whitelist set 로더 | |
| transform 단위테스트 + 왕복 통합테스트 | |

> 동적 재산정(B4-3)이 개시되기 **전**의 1회성 정적 셋업이다. 재산정 이후엔 이 시드가 아니라 B4-3 경로가 새 버전을 발행한다(§6 가드).

---

## 2. 컴포넌트 & 모듈 구성

```
src/agent_b_spc/
├── seed_control_limits.py     ← 시드: build_seed_rows(순수 transform) + DB 멱등 적재 + CLI
├── control_limits_loader.py   ← DB(is_active) → limits dict + whitelist set (엔진 진입점)
├── (입력) limits/initial_limits_v1.csv       ← 관리선 값 (B2-1)
├── (입력) limits/monitoring_whitelist_v1.csv ← decision·method (B2-1)
├── (입력) config/chamber_offsets.json        ← 6챔버 오프셋 (PM, dump_offsets.py)
└── tests/
    ├── test_seed_control_limits.py   ← 순수 transform (DB 불필요)
    └── test_control_limits_loader.py ← 왕복 통합 (Postgres, skipif)
```

| 모듈 | 역할 | 의존 | DB |
|---|---|---|---|
| `seed_control_limits.build_seed_rows()` | (limits_df, whitelist_df, offsets) → 270 row dict 리스트. **순수 함수** | pandas | 없음 |
| `seed_control_limits.seed()` | 트랜잭션 멱등 적재 (DELETE initial/v1 → INSERT) | SQLAlchemy(psycopg2 드라이버) | 쓰기 |
| `control_limits_loader.load()` | is_active 행 → `limits: dict[gk, dict]` + `whitelist: set[gk]` | SQLAlchemy(psycopg2 드라이버) | 읽기 |

---

## 3. 확정 설계 결정

### 3-1. 조인 & 필터 (①)

- **조인 키** = `(chamber_id, recipe_id, step, sensor_window, sensor_id)` — 두 CSV 공통.
- `initial_limits_v1.csv`(값) ⋈ `monitoring_whitelist_v1.csv`(decision·method) **inner join**.
- **정합 가드**: 조인 후 행 수 ≠ 양쪽 원본 77 → 하드 실패(그룹키 mismatch = 산출 불일치 신호).
- 필터: `decision ∈ {KEEP, KEEP*}` → 45그룹. (EXCLUDE 28 + EXCLUDE? 4 = 32 제외)

### 3-2. 챔버 복제 + 오프셋 shift (밴드 전체 평행이동)

- 각 `SIM_CH_i`(i=1..6) × 45그룹마다: `off = offsets[SIM_CH_i][sensor_id]`.
- **오프셋 누락 시 하드 실패** (`KeyError` → 명시적 raise) — 조용한 0-shift는 5/6 챔버 오탐 유발.
- shift 대상(절대 오프셋 **더함**): `center, ucl, lcl, q_lcl, q_ucl`.
- **불변**: `sigma, cv, k_sigma, q_low/q_high(레벨)`. (오프셋은 위치 이동 — 산포 무변. 행 노이즈는 관리선 미반영 — chamber_offsets.json note.)
- `chamber_id` ← `SIM_CH_i` (원본 `C24_0` 폐기).
- **노이즈–σ 근사 (설계 투명성)**: SIM_CH_i 실데이터 = real + offset + **noise**(noise_sigma=0.05)라 챔버 실제 σ는 baseline σ보다 **대략 0.1%대 큼**(노이즈 기여; baseline σ가 트림값이라 실제 격차는 조금 더 클 수 있으나 여전히 작음). 시드는 **offset(위치)만 정합**하고 산포 노이즈는 의도적으로 미반영 → 시드 ±3σ가 미세하게 타이트(오탐 소폭↑, 무시 가능). 정합의 단일 소스는 offset뿐임을 명시.

### 3-3. method별 컬럼 매핑 (B3-1 엔진 계약 — 1번)

| DB 컬럼 | `method='sigma'` (KEEP 39) | `method='quantile'` (KEEP\* 6) |
|---|---|---|
| `method` | `'sigma'` | `'quantile'` |
| `center` | center + off | center + off |
| `sigma` | σ (판정용) | σ (**참고 통계** — NOT NULL 충족, 판정 미사용) |
| `ucl` / `lcl` | ucl+off / lcl+off (±3σ) | **q_ucl+off / q_lcl+off** (분위수 관리선) |
| `k_sigma` | **CSV `k_sigma` carry** (하드코딩 3.0 금지) | **NULL** (DDL 허용) |
| `q_low` / `q_high` | **NULL** | **CSV `q_low`/`q_high` carry** (0.00135/0.99865 레벨, 하드코딩 금지) |
| `qa_flags` | 원본 carry (빈값 NaN→**NULL**) | 원본 carry (빈값 NaN→**NULL**) |

> 엔진(1번)은 `method`를 보고 quantile이면 N1 경계로 ucl/lcl(=분위수값)을 쓰고 N5~N8 제외. 즉 **로더는 method + ucl/lcl만 정확히 넘기면 됨** — q_low/q_high는 근거 보존용.

### 3-4. 고정 컬럼

`limit_version`·`trigger_type`은 **명명 상수** — `LIMIT_VERSION`(=`initial_limits.INITIAL_LIMIT_VERSION`="v1") / `TRIGGER_INITIAL`("initial")로 정의해 매핑·DELETE·가드 공용(인라인 'v1'/'initial' 금지, 헌법 6-1). `is_active=true`, `effective_from/created_at=NOW()`(DDL 기본).

---

## 4. 로더 계약 (control_limits → 엔진 입력)

```
SELECT chamber_id, recipe_id, step, sensor_window, sensor_id,
       method, center, sigma, ucl, lcl, limit_version
FROM control_limits WHERE is_active = true;
```

각 행 → `gk = (chamber_id, recipe_id, step, sensor_window, sensor_id)` →

- `limits[gk] = {center, sigma, ucl, lcl, method, limit_version}`
- `whitelist.add(gk)`

> **⚠️ group_key 타입 계약 (필수 — seed 실효의 전제)**: gk 원소 타입·포맷 = `(chamber_id:str, recipe_id:str, step:int, sensor_window:str, sensor_id:str)`. `step`은 DB `SMALLINT`→**int**. **런타임 `Point.group_key`(B4-1 컨슈머가 fdc.raw에서 구성)가 동일 타입·포맷이어야 엔진이 그룹 매칭** — 불일치(예: step을 str `'4'`)면 `gk not in whitelist`로 **조용히 매칭 0건**(판정 없음, whitelist 밖 스킵). seed/로더가 이 gk 형태의 **단일 소스**이므로 B4-1과 이 계약을 공유한다.

반환 `(limits, whitelist)` → 프로덕션은 `NelsonEngine(limits, whitelist)`(`time_gap_hours` 미지정 → 엔진이 config 로드), 테스트는 `time_gap_hours` 주입(hermetic). 로더는 time_gap 미관여. **①(감시 그룹만) 덕에 is_active 집합 = 감시집합**이라 whitelist를 CSV에서 따로 안 읽는다(단일 소스).

---

## 5. 멱등 적재 (③ delete-then-insert)

```
BEGIN;
DELETE FROM control_limits WHERE trigger_type='initial' AND limit_version='v1';
INSERT INTO control_limits (...) VALUES ...;   -- 270행 (execute_values / bulk)
COMMIT;
```

- **트랜잭션 단일 커밋** — 부분 적재 방지.
- partial unique(`idx_control_limits_active_one`, 그룹당 is_active 1행) 충돌 없음: initial/v1만 지우고 다시 넣으므로.
- **⚠️ 가드 ⓒ (§1 재확인, 강화)**: 재삽입 충돌의 진짜 위험 집합 = "DELETE(`trigger_type='initial' AND version=LIMIT_VERSION`)가 **안 지우는** active 행이 감시 그룹키와 겹침" = **`active AND NOT(trigger_type='initial' AND limit_version=LIMIT_VERSION)`**. `assert_no_conflicting_active`는 이 **강한 술어**로 점검한다(단순 `version!='v1'`이 아니라) — active·v1·non-initial 이상 케이스까지 차단. 동적 재산정(B4-3)이 active 행을 남긴 이후엔 시드 중단(그룹당 active 2행·partial unique 위반 방지). "재산정=항상 새 버전 발행"이라는 정상 전제에 **의존하지 않고 술어로 방어**.
- **헌법 4-1 (hard-delete 정당화)**: "덮어쓰기 금지"는 **재산정 이력**(v2+·is_active 토글)에 적용, 초기셋 정정엔 미적용. 가드 ⓒ가 recalc 개시 후 delete를 **원천 차단**(이력 무손실). `spc_violations`는 `limit_version` **문자열** 참조(FK 아님)라 재삽입(새 id)에도 orphan 없음.
- **상수화**: 위 SQL의 `'initial'`/`'v1'`은 코드에서 `TRIGGER_INITIAL`/`LIMIT_VERSION` 상수(헌법 6-1).

---

## 6. DB 연결 & 실행

- 드라이버: `psycopg2-binary` + `sqlalchemy>=2.0` (둘 다 requirements.txt 기존). 연결 = `DATABASE_URL`(.env, `postgresql://fdc_admin:...@localhost:5432/fdc_platform`).
- **연결 방식(소형 결정)**: SQLAlchemy 2.0 `create_engine(DATABASE_URL)` + `text()` — 기존 선언 의존성·DATABASE_URL 네이티브. bulk INSERT는 `execute_many`/`execute_values`.
- 테이블: docker-compose `postgres:16-alpine`가 `db/init.sql` 자동 로드 → control_limits 존재(별도 DDL 실행 불요).
  - **운영 주의**: `init.sql`는 pg **최초 init(빈 볼륨)에만** 실행됨. 기존 dev DB 볼륨에 control_limits가 없으면(구 볼륨) 시드·통합테스트가 실패 → **`docker compose down -v`로 볼륨 재생성** 후 재기동.
- CLI: `python -m src.agent_b_spc.seed_control_limits [--dry-run] [--csv-dir ...] [--offsets ...]`
  - `--dry-run`: build_seed_rows까지만 실행·검증(행수=`45×len(offsets)`=현 270·챔버별 요약, **`logging`으로 출력**·print 금지), **DB 미접속**.
- 파라미터/경로는 헌법 6-1 정합(하드코딩 금지) — 기본 경로 상수 + CLI 오버라이드.

---

## 7. 테스트 설계

### 계층 1 — transform 단위테스트 (순수, DB 불필요) — `test_seed_control_limits.py`

| 케이스 | 검증 |
|---|---|
| 행 수 | 감시 45그룹 × 6챔버 = 270행 정확 |
| EXCLUDE 필터 | decision∈{EXCLUDE,EXCLUDE?} 그룹 0건 포함 |
| 오프셋 shift | 특정 (챔버,센서) center'−center == offsets[ch][sensor] (부동소수 tol) |
| σ 불변 | sigma == 원본 (shift 무영향) |
| quantile 매핑 | KEEP\* 행: method='quantile'·ucl==q_ucl+off·lcl==q_lcl+off·k_sigma NULL·q_low/high 세팅 |
| sigma 매핑 | KEEP 행: method='sigma'·ucl==ucl+off·q_low/high NULL·**k_sigma==CSV값(carry, 3.0 하드코딩 아님)** |
| 방출 컬럼 | 결과 dict 키에 cv·sample_min·sample_max·n_distinct·false_alarm_pct 0건(DDL 컬럼만) |
| qa_flags NaN | 빈 qa_flags 행 → 결과 `None`(NULL), numpy 타입 아님 |
| 상태값 상수 | `limit_version==LIMIT_VERSION`·`trigger_type==TRIGGER_INITIAL` |
| chamber_id 치환 | 결과에 C24_0 0건, SIM_CH_1~6만 |
| 오프셋 누락 하드실패 | 감시 센서가 offsets에 없으면 raise |
| 조인 정합 | 그룹키 mismatch(양쪽 77·조인 77 아님) 시 raise |

### 계층 2 — 왕복 통합테스트 (Postgres) — `test_control_limits_loader.py`

`@pytest.mark.skipif(DB 미접속)` (CI/DB 없는 환경 안전).

1. seed() → control_limits 270행 적재
2. load() → (limits, whitelist) 도출
3. `NelsonEngine(limits, whitelist, time_gap_hours=72)`(hermetic 주입) → feed로 검증:
   - **챔버별 shift**: SIM_CH_1·SIM_CH_2가 같은 센서라도 offset 달라 N1 경계 다름 (한 값이 CH1엔 위반, CH2엔 정상)
   - **quantile 그룹**: KEEP\* 그룹에서 N1 분위수 경계 발동 + N5~N8 미발동 (1번 엔진 계약 실DB 검증)
4. **멱등**: seed() 2회 → 여전히 270행(중복 없음)
5. **stale 그룹 제거**: seed(A: 그룹 X 포함) → seed(B: X 제외) → X 행 0건 (delete-then-insert 사라진 그룹 처리 검증)

---

## 8. 엣지케이스 & 가드

- **오프셋 누락** → 하드 실패 (조용한 0-shift 금지).
- **조인 mismatch**(한쪽에만 있는 그룹키) → 하드 실패.
- **동적 재산정 공존** → 사전 점검으로 중단(§5 가드).
- **D_VDC_RES·EXCLUDE** → decision 필터에서 자연 제외(파생이라 offsets에도 없음 — 조회 안 함).
- **NOT NULL 충족**: quantile 행 `sigma`=원본σ(참고), `center/ucl/lcl` 전부 값 존재. `k_sigma`·`q_low/high`는 NULL 허용 컬럼.
- **빈 qa_flags(NaN)·타입 coercion (실측)**: pandas가 빈 `qa_flags`(CSV에 실존)를 NaN으로 읽음 → **None(NULL)** 코어싱 안 하면 INSERT 깨짐/`'nan'` 유입. numpy 타입(int64/float64/bool_) → 파이썬 네이티브(SMALLINT/FLOAT/BOOLEAN 정합).
- **C54/C56 회의 뒤집힘** → 입력(whitelist/CSV) 재생성 후 재시딩. **스크립트 불변**(입력만 갱신) — ③ 멱등이라 안전.
- **부동소수**: offset·shift는 float 그대로(반올림 안 함) — 근거 재현성.

---

## 9. 의존성 & 헌법 정합

- **입력**: `initial_limits_v1.csv`·`monitoring_whitelist_v1.csv`(B2-1), `config/chamber_offsets.json`(PM `dump_offsets.py`). **커플링**: PM이 seed·E1(챔버수)·E3(offset_sigma) 변경 시 chamber_offsets.json 재생성 → 재시딩 필요(json note "재생성 필수").
- **소비**: `control_limits_loader` → B3-1 엔진(1번) / 향후 B4-1 라이브 컨슈머가 로더 재사용.
- **헌법**: **3-1/3-2**(control_limits DDL 이미 v4.6 승인 — seed는 **DML만**, `db/init.sql`(PM 소유) 미변경·`ALTER` 없음) · **4-1**(초기셋 정정은 재산정 이력 대상 아님 + 가드 ⓒ가 recalc 후 delete 차단 — §5) · **6-1**(경로·파라미터·상태값 상수·CSV carry·`logging`) · **6-4**(snake_case·sensor_id는 C코드).
- **후속 문서 동기화**(헌법 4-3): 구현 완료 시 README 4단계 "DB 시딩" 체크 갱신.
