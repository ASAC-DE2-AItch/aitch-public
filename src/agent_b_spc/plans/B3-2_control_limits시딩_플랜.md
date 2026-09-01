# control_limits v1 시딩 + 로더 (B3-2) Implementation Plan

> **개정 v2 (2026-07-13, 플랜 리뷰 반영)**: ① `k_sigma`·`q_low`·`q_high`는 **CSV 컬럼 carry**(매직넘버 하드코딩 제거 — 헌법 6-1) ② transform 방출 = **control_limits DDL 컬럼만**(CSV 잉여 컬럼 DROP) ③ 시드는 **`params.yaml` 미의존**(q 레벨=CSV) ④ delete-then-insert의 "사라진 그룹" 처리 명문화 ⑤ **시드 전 `--dry-run` 통과를 게이트로** DoD 추가 ⑥ 통합테스트 DB 정리.
> **개정 v3 (2026-07-13, 2차 리뷰 반영)**: ① `limit_version`·`trigger_type`을 **명명 상수**(`LIMIT_VERSION`·`TRIGGER_INITIAL`)로 정의해 매핑·DELETE·가드 공용(인라인 'v1'/'initial' 제거 — 헌법 6-1) ② **빈 `qa_flags`(NaN)→NULL + numpy 타입 coercion**(INSERT 정확성) ③ 헌법 4-1 hard-delete 정당화 airtight ④ **stale 그룹 제거 테스트** 추가 ⑤ dry-run 출력 `logging` ⑥ 행 수 = `45×len(offsets)` 파생 ⑦ **DML-only** 명시(헌법 3-1/3-2) ⑧ 로더 time_gap 계약 명확화.
> **개정 v4 (2026-07-13, 스펙 v3 sync)**: ① **가드 강한 술어**(`active AND NOT(trigger='initial' AND version=LIMIT_VERSION)`) — 예전 약한 `version!=v1` 교체(스펙 §5) ② **group_key 타입 계약**을 Task 3에 명시(스펙 §4) ③ 가드 함수명 `assert_no_active_v2`→`assert_no_conflicting_active`(동작 정합, 스펙 동일) ④ 행 수 `45×len(offsets)` 표기 통일.
>
> ⚠️ 후속(2026-07-14): C54/C56 정착→과도 재정정으로 baseline 재생성 → 총 69그룹·EXCLUDE 20(감시 45 불변). 이 문서의 77/28은 PR #21 당시 기록. 감시 45(및 이 문서의 `45×len(offsets)`=270행 시딩)는 C54/C56이 정착이든 과도든 step4 KEEP으로 계속 감시되므로 **영향 없음**.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (권장) 또는 superpowers:executing-plans로 task 단위 구현. Step은 체크박스(`- [ ]`)로 추적.

**Goal:** B2-1 정적 초기 실력치 v1(단일 챔버 77그룹)을 감시 그룹(KEEP+KEEP\*=45)만 6가상 챔버로 복제·오프셋 shift해 `control_limits`에 멱등 적재하고, 엔진(B3-1)이 읽을 DB→limits 로더를 제공한다.

**Architecture:** 순수 transform(`build_seed_rows`) → 멱등 DB 적재(`seed`, delete-then-insert 트랜잭션) → 로더(`load`, is_active→limits+whitelist) → 왕복 통합테스트. transform은 DB 없이 단위테스트, DB는 SQLAlchemy 2.0 엔진. 브레인스토밍 3결정(① 감시 그룹만 ② 시드+로더+왕복 ③ delete-then-insert)을 그대로 구현.

**Tech Stack:** Python, pandas, PyYAML, SQLAlchemy≥2.0, psycopg2-binary, pytest (전부 requirements.txt 기존). 설계 근거: `src/agent_b_spc/specs/B3-2_control_limits시딩_설계.md`.

## Global Constraints

- **헌법 6-1**: 경로·파라미터·**상태값** 하드코딩 금지 — 경로 상수 + CLI 오버라이드, DB = `DATABASE_URL`(.env). **`limit_version`·`trigger_type`은 module 상수**(`LIMIT_VERSION`=`initial_limits.INITIAL_LIMIT_VERSION` 재사용·`TRIGGER_INITIAL="initial"`)로 정의해 매핑·DELETE·가드 공용(인라인 'v1'/'initial' 3중복 금지). 로그는 `logging`(print 금지).
- **헌법 6-4**: `snake_case`, 센서는 C코드 그대로.
- **헌법 3-1/3-2**: `control_limits` DDL은 이미 `db/init.sql`(v4.6) 승인·존재. seed는 **DML(delete/insert)만** — `ALTER`·`db/init.sql`(PM 소유) 미변경. 스키마 무변경.
- **헌법 4-1 (hard-delete 정당화)**: "덮어쓰기 금지"는 **재산정 이력**(v2+·is_active 토글)에 적용, 초기셋 정정엔 미적용. 가드 ⓒ가 **active v2 존재 시 delete 차단** → recalc 개시 후엔 절대 삭제 안 함(이력 무손실). `spc_violations`는 `limit_version` **문자열** 참조(FK 아님)라 재삽입(새 id)에도 orphan 없음.
- **감시 그룹만(①)**: decision∈{KEEP,KEEP\*}만 적재 → is_active 집합 = 감시집합(로더 단일소스).
- **밴드 전체 shift**: `center·ucl·lcl(+분위수면 q_ucl/q_lcl)`에 오프셋 더함, `sigma` 불변. 행 노이즈 미반영.
- **하드코딩 금지(헌법 6-1)**: `k_sigma`·`q_low`·`q_high`는 **CSV 컬럼값을 carry** — 매직넘버(3.0/0.00135) 금지. sigma 행은 CSV `k_sigma`, quantile 행은 `k_sigma=NULL`·CSV `q_low`/`q_high`.
- **방출 컬럼 = control_limits DDL만**: CSV의 `cv·sample_min·sample_max·n_distinct·false_alarm_pct`는 DDL에 없음 → transform이 **DROP**(그대로 넘기면 INSERT 깨짐).
- **NULL·타입 coercion (INSERT 정확성, 실측)**: 빈 `qa_flags`(pandas NaN — CSV에 실존) → **None(NULL)**. `k_sigma`(quantile)·`q_low/q_high`(sigma) → NULL. numpy 타입(int64/float64/bool_) → 파이썬 네이티브(SMALLINT/FLOAT/BOOLEAN 정합).
- **시드는 `params.yaml` 미의존**: 입력 = `initial_limits_v1.csv` + `monitoring_whitelist_v1.csv` + `chamber_offsets.json` + `DATABASE_URL`(.env). q 레벨은 CSV, 경로는 상수.
- **qa_flags carry (참고)**: 미shift 단일챔버 기준 flag를 그대로 실음 — 대부분 분포 형태라 챔버 무관이나 `wide_limits`(0 대비 위치)는 shift 후 엄밀히 달라질 수 있음. 진단용이라 v1은 carry, 필요 시 W7 백로그.
- **하드 실패 가드**: ⓐ 감시 센서가 `chamber_offsets.json`에 없으면 `KeyError`→명시 raise(조용한 0-shift 금지) ⓑ CSV⋈whitelist 조인 mismatch → raise ⓒ **active v2 존재 시 시딩 중단**(동적 재산정 개시 후 재시딩 금지 — 그룹당 active 2행 방지).
- **오프셋 커플링**: PM이 seed·E1(챔버수)·E3(offset_sigma) 변경 시 `chamber_offsets.json` 재생성 → 재시딩(json note "재생성 필수").
- **테스트**: transform은 순수→DB 불필요 단위테스트. 왕복 통합은 Postgres 필요→`skipif(DB 미접속)`로 CI 안전. 기존 nelson 테스트 회귀 무영향(다른 모듈).
- **적재 행 수 = `45 × len(offsets)`** (현 6챔버 = 270). 코드·테스트 어서션 모두 리터럴 270이 아니라 `len(offsets)` 기반(E1 변경 대비). 문서의 "270"은 현 상태 표기.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/agent_b_spc/seed_control_limits.py` | 시드: 순수 transform + 멱등 적재 + CLI | **생성** |
| `src/agent_b_spc/control_limits_loader.py` | DB(is_active) → limits dict + whitelist set | **생성** |
| `src/agent_b_spc/tests/test_seed_control_limits.py` | 순수 transform 단위테스트 (DB 불필요) | **생성** |
| `src/agent_b_spc/tests/test_control_limits_loader.py` | 왕복 통합테스트 (Postgres, skipif) | **생성** |
| `src/agent_b_spc/limits/initial_limits_v1.csv`·`monitoring_whitelist_v1.csv` | 입력 (B2-1) | 읽기 전용 |
| `config/chamber_offsets.json` | 6챔버 오프셋 (PM) | 읽기 전용 |
| `src/agent_b_spc/README.md` | 문서 | 완료 시 4단계 체크 갱신(헌법 4-3) |

---

# Task 1: 순수 transform `build_seed_rows` (조인·필터·shift·method 매핑)

DB 없이 (limits_df, whitelist_df, offsets) → 270 row dict 리스트를 만드는 **순수 함수**. B3-2의 핵심 로직 전부가 여기 모이고 DB 없이 완결 검증된다.

**Files:**
- Create: `src/agent_b_spc/seed_control_limits.py`
- Test: `src/agent_b_spc/tests/test_seed_control_limits.py`

**Interfaces:**
- `GROUP_KEYS = ["chamber_id","recipe_id","step","sensor_window","sensor_id"]`
- **상태값 상수**: `LIMIT_VERSION = initial_limits.INITIAL_LIMIT_VERSION`(="v1"), `TRIGGER_INITIAL = "initial"` — 매핑·DELETE·가드 공용(헌법 6-1). `DDL_COLUMNS = [...]`(방출 화이트리스트).
- `load_offsets(path) -> dict[str, dict[str, float]]` — chamber_offsets.json의 `offsets` 반환.
- `build_seed_rows(limits_df: pd.DataFrame, whitelist_df: pd.DataFrame, offsets: dict) -> list[dict]` — 순수. 반환 dict 키 = control_limits 컬럼.

**shift/매핑 규칙 (설계 §3-3, 리뷰 v2 반영):**
```
감시 필터: decision ∈ {KEEP, KEEP*}
챔버 루프: for ch in offsets:  for row in monitored:
  off = offsets[ch][sensor_id]        # 없으면 raise (가드 ⓐ)
  method = row.method                 # whitelist join — 실데이터 1:1(KEEP*→'quantile' / KEEP→'sigma')
  center' = center + off
  if method == 'sigma':   ucl'=ucl+off;   lcl'=lcl+off;   k_sigma=row.k_sigma; q_low=q_high=None
  if method == 'quantile':ucl'=q_ucl+off; lcl'=q_lcl+off; k_sigma=None;        q_low=row.q_low; q_high=row.q_high
  sigma'=sigma(불변); chamber_id=ch; limit_version=LIMIT_VERSION; trigger_type=TRIGGER_INITIAL; is_active=True
  qa_flags=원본 carry(빈값 NaN→None); n_wafers·calc_window_n 원본(numpy→네이티브)
```
> ⚠️ **헌법 6-1**: `k_sigma`·`q_low`·`q_high`는 **CSV 컬럼 carry**(매직넘버 3.0/0.00135 금지). `limit_version`·`trigger_type`은 **상수**(`LIMIT_VERSION`·`TRIGGER_INITIAL`) — 인라인 'v1'/'initial' 금지.
> ⚠️ **방출 = control_limits DDL 컬럼만** — CSV의 `cv·sample_min·sample_max·n_distinct·false_alarm_pct`는 DROP(DDL에 없어 INSERT 깨짐). row dict 키를 `DDL_COLUMNS` 화이트리스트로 고정.
> ⚠️ **NULL·타입**: 빈 `qa_flags`(NaN)→None, numpy 타입→파이썬 네이티브(SMALLINT/FLOAT/BOOLEAN 정합).

- [x] **Step 1: 실패 테스트 작성 — transform**

`test_seed_control_limits.py` 생성. 소형 합성 `limits_df`(KEEP 1행 + KEEP\* 1행)·`whitelist_df`(method 포함)·`offsets`(2챔버) 픽스처로:
  - `len(rows) == 감시그룹수 × 챔버수` (여기선 2×2=4)
  - EXCLUDE 행은 결과에 0건 (감시 필터)
  - shift: 특정 (ch,sensor) `center' - center == offsets[ch][sensor]` (tol 1e-9), `sigma' == sigma`
  - quantile 행: `method=='quantile'`·`ucl == q_ucl+off`·`lcl == q_lcl+off`·`k_sigma is None`·`q_low/q_high == CSV값(carry)`
  - sigma 행: `method=='sigma'`·`ucl == ucl+off`·`q_low/q_high is None`·`k_sigma == CSV k_sigma값(carry, 하드코딩 3.0 아님)`
  - **방출 컬럼 = control_limits DDL 집합**: 결과 dict 키에 `cv·sample_min·sample_max·n_distinct·false_alarm_pct` 0건(DROP 확인)
  - **빈 qa_flags 행 → `qa_flags is None`**(NaN 미유입), numpy 타입 아님(파이썬 네이티브)
  - **상태값 상수**: 결과 `limit_version == LIMIT_VERSION`·`trigger_type == TRIGGER_INITIAL`(인라인 리터럴 아님)
  - chamber_id 치환: 결과에 원본 챔버 0건, SIM_CH_* 만
  - 가드 ⓐ: offsets에 없는 감시 센서 → `pytest.raises`
  - 가드 ⓑ: 조인 후 행수 ≠ 원본(양쪽 77·조인 77) → `pytest.raises`

- [x] **Step 2: 구현 — `load_offsets` + `build_seed_rows`**

조인(inner, GROUP_KEYS) → 정합 가드 → 감시 필터 → 챔버×그룹 이중 루프 → 위 매핑. 순수(부작용·DB·파일 IO 없음, 입력은 이미 로드된 df·dict).

- [x] **Step 3: 검증** — `pytest test_seed_control_limits.py -v` 그린. 실데이터 스모크(선택): 실제 CSV·offsets로 `build_seed_rows` → `len==270`·챔버 6·quantile 6그룹×6 확인(`--dry-run`이 이걸 출력).

---

# Task 2: 멱등 DB 적재 `seed()` + CLI

`build_seed_rows` 결과를 트랜잭션으로 `control_limits`에 delete-then-insert. 충돌 active 가드(`assert_no_conflicting_active`) 포함.

**Files:**
- Modify: `src/agent_b_spc/seed_control_limits.py`
- (DB 통합 검증은 Task 4에서 — 여기선 CLI·트랜잭션 로직)

**Interfaces:**
- `make_engine(database_url: str | None = None)` — `DATABASE_URL`(.env) 또는 인자. SQLAlchemy `create_engine`.
- `seed(engine, rows: list[dict]) -> int` — 반환 = 적재 행 수. 트랜잭션:
  ```
  with engine.begin() as conn:
    assert_no_conflicting_active(conn)                          # 가드 ⓒ (강한 술어)
    conn.execute(delete where trigger_type=TRIGGER_INITIAL and limit_version=LIMIT_VERSION)
    conn.execute(insert, rows)                                  # bulk
  ```
  > 삭제가 그룹키가 아니라 `trigger_type+version` 기준이라, 재생성으로 **사라진 그룹**(C54/C56 settled→transient)의 옛 행도 통째로 제거·재구성됨 — UPSERT가 못 하던 stale 잔류가 없다(③ delete-then-insert의 실익, Task 4에서 검증).
- `main()` — argparse `--dry-run`(build만·DB 미접속·**요약은 `logging`**)·`--csv-dir`·`--offsets`·`--database-url`. (헌법 6-1 print 금지)

- [x] **Step 1: 구현 — `make_engine`·`assert_no_conflicting_active`·`seed`·`main`**

`assert_no_conflicting_active`: `SELECT 1 FROM control_limits WHERE is_active AND NOT (trigger_type=:t AND limit_version=:v) LIMIT 1`(:t=`TRIGGER_INITIAL`·:v=`LIMIT_VERSION`) 존재 시 `RuntimeError` — **DELETE가 안 지우는 active 행이 감시 그룹키와 겹칠 위험 차단**(단순 `version!=v1`이 아니라 강한 술어 — 설계 §5, 헌법 4-1 이력 보호). INSERT는 SQLAlchemy Core `text()` executemany 또는 table insert, 컬럼 = `DDL_COLUMNS`.

- [x] **Step 2: dry-run 스모크** — `python -m src.agent_b_spc.seed_control_limits --dry-run`: 270행·챔버6·method 분포(sigma 39×6/quantile 6×6) 출력, DB 접속 없음, exit 0.

- [x] **Step 3: 멱등 확인은 Task 4 통합테스트로** (seed 2회 → 270행 유지).

---

# Task 3: 로더 `control_limits_loader.load()`

`is_active` 행 → 엔진이 그대로 쓰는 `(limits, whitelist)`.

**Files:**
- Create: `src/agent_b_spc/control_limits_loader.py`

**Interfaces:**
- `load(engine) -> tuple[dict[tuple, dict], set[tuple]]`
  ```
  SELECT chamber_id,recipe_id,step,sensor_window,sensor_id,method,center,sigma,ucl,lcl,limit_version
  FROM control_limits WHERE is_active = true;
  → gk = (chamber_id,recipe_id,step,sensor_window,sensor_id)
    limits[gk] = {center,sigma,ucl,lcl,method,limit_version}
    whitelist.add(gk)
  ```
- 엔진 주입: **프로덕션은 `NelsonEngine(limits, whitelist)`**(time_gap_hours 미지정 → 엔진이 config 로드), 테스트는 값 주입(hermetic). 로더는 time_gap 미관여.
- **⚠️ group_key 타입 계약 (설계 §4)**: gk = `(chamber_id:str, recipe_id:str, step:int, sensor_window:str, sensor_id:str)`. `step`은 DB `SMALLINT`→**int**. **런타임 `Point.group_key`(B4-1)가 동일 타입·포맷이어야 엔진 매칭** — 불일치(예: step str `'4'`)면 조용히 매칭 0건. 로더가 이 gk 형태의 단일 소스.

- [x] **Step 1: 구현 — `load`** (①로 is_active=감시집합이라 whitelist를 CSV 아닌 DB에서 도출).
- [x] **Step 2: 검증은 Task 4 왕복테스트**(로더 단독은 DB 필요 → 통합에서 함께).

---

# Task 4: 왕복 통합테스트 (seed → load → 엔진)

Postgres에 실제 적재·조회·엔진 판정까지 관통. `@pytest.mark.skipif(DB 미접속)`.

**Files:**
- Create: `src/agent_b_spc/tests/test_control_limits_loader.py`

**Interfaces:**
- `_db_available() -> bool` — `DATABASE_URL` 접속 시도(`connect_timeout` 짧게). 실패 시 skip.
- 픽스처: `seed()`가 `engine.begin()`으로 **커밋**하므로, 테스트 종료 시 `trigger_type='initial'` 정리(또는 savepoint 롤백)를 **반드시** 넣어 공유 dev DB 오염 방지.

- [x] **Step 1: 통합테스트 작성·구현**
  1. `seed(engine, build_seed_rows(...))` → `control_limits` `45×len(offsets)`(현 270)행 적재
  2. `limits, whitelist = load(engine)` → 동수 그룹
  3. `NelsonEngine(limits, whitelist, time_gap_hours=72)` 주입 → feed 검증:
     - **챔버별 shift**: 동일 센서라도 SIM_CH_1·SIM_CH_2 offset 달라 N1 경계 다름 — 한 값이 CH1엔 위반, CH2엔 정상
     - **quantile 그룹**: KEEP\* 그룹에서 N1 분위수 경계 발동 + N5~N8 미발동 (1번 엔진 계약 실DB 검증)
  4. **멱등**: `seed` 2회 → `SELECT count(*)` 동일(중복 0)
  5. **stale 그룹 제거**: seed(A: 그룹 X 포함) → seed(B: X 제외) → `SELECT ... X` **0건** (delete-then-insert의 "사라진 그룹" 처리 검증 — Task 2 주장)

- [x] **Step 2: 검증** — DB 있는 환경서 `pytest test_control_limits_loader.py -v` 그린; DB 없으면 skip(수) 확인. *(현 환경 DB 미접속 → 6 skipped·exit 0 확인. 라이브 DB 실행 대기.)*

---

# 완료 판정 (DoD)

- [x] transform 단위테스트 그린 (DB 불필요) — shift·**k_sigma/q carry**·**DDL 컬럼만 방출**·**빈 qa_flags→None**·**상태값 상수(v1/initial)**·가드 3종
- [x] **시드 적재 전 `--dry-run` 무오류 통과**(오프셋 커버리지 게이트 — 가드 ⓐ 미발동) + `45×len(offsets)`(=270)·6챔버·method 분포(sigma 39×6/quantile 6×6)
- [x] `seed` 멱등(2회 동일 행수)·**stale 그룹 제거**·active v2 가드 동작 (통합) — **라이브 Postgres 6/6 PASS(2026-07-14)**
- [x] 로더 왕복: seed→load→엔진 feed에서 **챔버별 shift·quantile 판정** 검증 — **라이브 6/6 PASS**(Test4 sigma 대조군 강화). 실적재 270행 후 `load()`→270그룹, C11 챔버별 ucl 상이 확인.
- [x] 기존 nelson 테스트 회귀 무영향 · seed는 **DML만**(스키마 무변경 — 헌법 3-1/3-2) *(전체 스위트 84 passed·스키마 무변경 확인)*
- [x] README 4단계 "DB 시딩" 체크 갱신 (헌법 4-3) — 3단계(챔버별 복제 확정)·4단계(시딩/로더 도구 구현·리뷰 완료, 라이브 적재 대기) 반영

# 후속 (이 플랜 밖)

- **B4-1**: 로더 재사용 + Kafka 배선 → 라이브 fdc.alert.
- **B4-3**: 동적 재산정(새 limit_version·is_active 전환) — 이후엔 이 정적 시드 대신 재산정 경로.
- **C54/C56 회의**: 뒤집히면 whitelist/CSV 재생성 후 재시딩(스크립트 불변, ③ 멱등이라 안전).
