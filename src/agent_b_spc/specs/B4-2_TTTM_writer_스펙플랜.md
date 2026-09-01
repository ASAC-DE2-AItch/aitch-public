# TTTM DB Writer (③ `tttm_comparisons` 적재) — 경량 Spec+Plan

> 상위 근거: `specs/B4-2_TTTM엔진_설계.md` §9(③ writer) · 인수인계 §3-1.
> **경량 1종 근거**: 알고리즘 0 · 스키마 변경 0 · 기존 패턴(B3-2 `seed_control_limits`) 재사용 → 코드 ~100 / 테스트 ~100줄. 정식 spec+plan 2종은 이 규모에 과다(오버엔지니어링) → spec+plan 통합 1장.
> **개정 (2026-07-15, 외부 리뷰 반영)**: Task 2 벌크 insert(`executemany`) 명시 · Task 3 `is_reference_suspect` bool 왕복 assert 명시 · fixture `with connect()`+`try/finally`(항상 rollback·close 보장). *리뷰의 "부분적재 누수"는 트랜잭션(결정 #5)이 이미 원자성 보장 → 벌크는 성능 사유로만 채택. "실패 시 teardown 미실행" 전제도 부정확(pytest는 테스트 실패해도 teardown 실행) → 실위험인 close 누수만 가드.*
> **개정 (2026-07-15, 정밀·독립 교차리뷰)**: 🔴 무DB **skip 가드**(Task 3, 무DB서 error 방지) · `rollups_to_rows`로 **None-skip 소유**(글루 명시) · `chamber_id`=**alert 최상위(2-1) 정정**("유입 금지" 오기) · 6-1 **docstring·logging** 명시 · `rows=[]`→0 · 엣지 테스트데이터(bool·null) · 엔진 팩토리 출처 명시.
> **For agentic workers:** TDD, Step 체크박스. 입력은 **정적 rollup dict 주입**(엔진 불요) · DB는 **롤백 트랜잭션**.

**Goal:** 엔진 `chamber_rollup(chamber_id)` 산출 dict → `tttm_comparisons` 행 적재 writer(`tttm_writer.py`) + 왕복·롤백 검증 테스트. **alert 발행·라이브 영속은 범위 밖**(B4-1·라이브 배선).

**Architecture:** `rollups_to_rows(rollups)`(None 드롭 + `rollup_to_row` 변환) → `write_comparisons(conn, rows)`(INSERT, **commit 안 함**). 테스트: 정적 rollup dict 주입 → writer → INSERT → SELECT 왕복 → **트랜잭션 rollback**(테이블 원복). B3-2 `seed_control_limits` 패턴(SQLAlchemy `text`·dotenv 엔진·`DDL_COLUMNS` 화이트리스트) 재사용.

**Tech Stack:** Python, SQLAlchemy(`text`)·python-dotenv, pytest. DDL은 `db/init.sql`의 기존 `tttm_comparisons`(**변경 없음**).

---

## 결정 (2026-07-15 확정)

| # | 결정 | 확정 | 근거 |
|---|---|---|---|
| 1 | 발화 단위 | **A — 평가되는 모든 비교 기록** | 테이블명 "comparisons"(위반 아님) · §3-1 "누적" · 이상 게이팅은 alert(B4-1) 몫. `None`(평가불가)만 skip, **임계 필터 없음** |
| 2 | 테스트 입력 | **정적 dict 주입 + 계약검사 1** | hermetic(엔진은 자체 38/38). 단 `transform 입력 키 == 엔진 rollup 키` assert로 엔진 필드 rename의 조용한 파손 방지 |
| 3 | 왕복 | **테스트 내 SELECT** (전용 reader ✕) | tttm 읽는 소비자 現 부재(YAGNI). 소비자 등장 시 그쪽이 읽기 형태 정의 |
| 4 | `is_qual` 필터 | **이월(훅만 명시)** | 정적 입력엔 qual 개념 없음 + 상류 라우팅 소관(#⑧). 라이브 배선(B4-1)에서 적용 |
| 5 | 트랜잭션 경계 | **호출자 소유 · writer는 commit 안 함** | §3-1이 롤백 방식 이미 확정. writer가 commit 안 해야 (테스트)롤백 깔끔 · (라이브)호출자 commit로 재사용 |

## 필드 매핑 (엔진 `chamber_rollup` → `tttm_comparisons`)

| 엔진 rollup 키 | DDL 컬럼 | 비고 |
|---|---|---|
| `reference` | `reference_type` | ="fleet_median" (rollup에서 취득, 하드코딩 금지) |
| `chamber_id` | `chamber_id` | alert 최상위 필드(2-1) — tttm 서브객체엔 미중복 |
| `reference_id` | `reference_id` | **DB 전용**(#7, =`limit_version`) |
| `score` | `tttm_score` | |
| `top_gap_sensor` | `top_gap_sensor` | C코드 |
| `gap_pct` | `gap_pct` | nullable |
| `reference_suspect` | `is_reference_suspect` | |
| — | `id` · `created_at` | DB 디폴트(생성 안 함) |

## Global Constraints (헌법 + 불변식)

- **헌법 3-1/3-2**: `tttm_comparisons` DDL 기존 존재 → **DML only, 스키마 변경 0** → PM 승인 불요.
- **헌법 6-1**: (a) 값은 **rollup에서 취득**(`'fleet_median'`/`'initial'` 하드코딩 금지), `DDL_COLUMNS`는 모듈 상수. (b) 두 함수 **docstring 필수**. (c) `print` 금지 — `logging.getLogger(__name__)`로 적재 행수 info 로그(seed·loader 동일).
- **헌법 2-1/2-2**: `reference_id`만 **진짜 DB 전용**(alert에 실으면 신규 필드 → 2-2 발동, B4-1 소관). `chamber_id`는 **이미 `fdc.alert` 최상위 필드(2-1)** — alert에서 빼면 안 됨. 단 tttm 서브객체엔 중복 안 함(엔진 `ALERT_FIELDS`가 이미 제외).
- **헌법 1-2**: `alert_id` 없음 → alert 채널 무관. writer는 발행 안 함.
- **헌법 4-3**: writer 코드 + 이 문서 + README 갱신 = **같은 PR**.
- **불변식**:
  - **writer는 commit 안 함** — 트랜잭션 경계는 호출자(테스트=rollback / 라이브=commit).
  - **`None` rollup·제외 챔버 = 행 생성 안 함** (평가불가 skip. 임계 필터는 없음 — 결정 #1 A).
  - **`control_limits` 불변** — writer는 `tttm_comparisons`만 씀.

## File Structure

| 파일 | 역할 |
|---|---|
| `src/agent_b_spc/tttm_writer.py` | **Create** — `rollup_to_row` · `rollups_to_rows` · `write_comparisons` |
| `src/agent_b_spc/tests/test_tttm_writer.py` | **Create** — 단위 + 왕복·롤백 통합(무DB skip) |
| `src/agent_b_spc/seed_control_limits.py` | 읽기전용 — 엔진 생성(dotenv)·`DDL_COLUMNS`·INSERT 패턴 재사용(B3-2) |
| `db/init.sql` (`tttm_comparisons`) | 읽기전용 — DDL 참조 |

---

# Task 1: transform (`rollup → DDL row`) ✅

**Files:** Create `tttm_writer.py`, `tests/test_tttm_writer.py`
**Interfaces:** `rollup_to_row(rollup: dict) -> dict`(7키→DDL 컬럼 매핑) · `rollups_to_rows(rollups: list[dict|None]) -> list[dict]`(**None 드롭 + 변환** — None-skip을 여기서 소유, 테스트·라이브 공용 진입점). 둘 다 docstring 필수.

- [x] **Step 1 실패 테스트**: 매핑 정확(7→7) / **`rollups_to_rows`가 `None` 드롭**(행 없음) / **계약검사: rollup 키 == 엔진 `ALERT_FIELDS ∪ {reference_id, chamber_id}`**(엔진 rename 시 fail) / `id`·`created_at` 미생성
- [x] **Step 2 구현**: 순수 dict 변환(값은 rollup에서, 하드코딩 0). `_ROLLUP_TO_DDL` 매핑 + dict-comprehension.
- [x] **Step 3 검증**: 그린(6건). *실제 결과: 엔진 `ALERT_FIELDS`를 실 import해 계약검사 → 6 passed.*

# Task 2: DB writer (INSERT · no-commit) ✅

**Files:** Modify `tttm_writer.py`, `tests`
**Interfaces:** `write_comparisons(conn, rows: list[dict]) -> int` — `DDL_COLUMNS` 화이트리스트 INSERT, 반환=행수(docstring 필수). **commit 안 함**(호출자 소유). `conn`=트랜잭션 열린 SQLAlchemy Connection. **불변식**: 모든 row는 정확히 7개 non-default 컬럼 키(executemany 균일 키 요구 — `rollups_to_rows` 산출이라 보장). **`rows=[]` → execute 스킵·`return 0`**.
**벌크 삽입**: `conn.execute(text("INSERT …"), rows)` **1회(executemany)** — 행별 루프 금지. *(원자성은 트랜잭션(결정 #5)에서 옴 — 벌크는 왕복·성능 사유, 라이브 관련.)*

- [x] **Step 1 실패 테스트**: 다중 행 INSERT 행수 / **`rows=[]` → 0 반환·execute 안 함** / **writer가 commit 호출 안 함**(호출자가 롤백하면 0행 잔존)
- [x] **Step 2 구현**: `seed_control_limits`의 `DDL_COLUMNS`·`text` 패턴 재사용하되 **delete-then-insert 복사 안 함**(append-only). `_INSERT_SQL` 모듈 상수 + `conn.execute(_INSERT_SQL, rows)` 벌크 1회 · `DELETE` 없음 · commit 없음 · 적재 행수 logging.
- [x] **Step 3 검증**: 그린(3건). *무DB에서도 계약 검증되도록 `_SpyConn`(execute/commit 기록 대역) 도입 — 벌크 1회·no-commit·`[]`→스킵을 실 DB 없이 확인.*

# Task 3: 왕복·롤백 통합테스트 ✅

**Files:** Modify `tests`
**Interfaces:** engine = `seed_control_limits`의 **dotenv 기반 엔진 생성 재사용**(`make_engine`). **`_db_available()` + `@pytest.mark.skipif`**(DB 미접속 시 skip, `test_control_limits_loader` 동일 패턴). ⚠️ **per-test 데코레이터**(모듈 `pytestmark` 금지 — Task1/2 순수 테스트까지 skip돼선 안 됨). 함수 내 명시적 트랜잭션 — `with engine.connect() as conn`(close 보장) → `trans = conn.begin()` → `try` / `finally: trans.rollback()`(성공·실패 무관 **항상 rollback**, §3-1).

- [x] **Step 1 통합테스트**: 정적 rollup 3건(**`is_reference_suspect` True·False + `gap_pct=None` 1건 포함**) 주입 → writer INSERT → **SELECT 되읽어 값·타입 일치**(왕복 — `is_reference_suspect`→Python `bool`, `gap_pct=None`→NULL→None) / 롤백 후 `tttm_comparisons` 원복(delta 0) / `control_limits` 무변경(`cl_after==cl_before`)
- [x] **Step 2 검증**: 그린 — *이 환경은 DB 접속돼 **실행됨(skip 아님)**, 10/10 통과.* **2단계 sabotage 검증**으로 "즉시 통과가 무의미"를 방어: ① transform 오염은 unit이 잡음(roundtrip은 `expected`를 같은 transform서 파생해 장님) → **매핑=unit / DB층=roundtrip** 책임 경계 실증. ② INSERT 컬럼 누락(DB DEFAULT FALSE) → 스파이 unit 장님·**roundtrip이 잡음** → bool 영속 고유 가치 입증.

---

# 완료 판정 (DoD) — ✅ 전 항목 충족 (2026-07-15)

- [x] 단위 그린: transform(매핑·**계약검사**) / `rollups_to_rows`(None 드롭) / writer(행수·`[]`→0·**no-commit**) — 9건
- [x] **왕복 통과**: 넣은 정적 데이터 == 되읽은 데이터(값·타입 — bool·null gap_pct 포함) — 실 DB 실행
- [x] **롤백 후 빈 상태**: `tttm_comparisons` 원복(delta 0) + `control_limits` 무변경 (§3-1)
- [x] **무DB 환경 = skip(에러 아님)** · 함수 docstring · 적재 행수 logging (6-1) — per-test skipif
- [x] 코드 + 이 문서 + README 같은 PR(4-3)

**최종 결과**: `test_tttm_writer.py` **10 passed**(단위 9 + 왕복 1) · 회귀 nelson/limits/tttm engine 포함 **120 passed** 무회귀. sabotage 2건으로 테스트 유효성 자체 검증.

---

# 이월 (이 문서 밖)

- **`is_qual` 필터**(#⑧): 라이브 배선(B4-1)에서 **앞단 적용** — writer는 받은 rollup 적재만.
- **라이브 영속·alert 발행**: B4-1 / 라이브 배선. 이 문서는 **테스트 적재+롤백**까지.
- **전용 reader**: tttm 소비자 등장 시 별도(현재 YAGNI).
- **라이브 직전 DB 비우기**(§3-1): 실가동 전 `tttm_comparisons` 정리(테스트 데이터 일회성 청소) — **운영 체크리스트**(코드 아님).
- **라이브 보존정책**(오래된 행 정리): 결정 #1(A=전부 기록)이라 라이브에서 무한 누적 → 주기적 purge/아카이브 여부는 **소비자 요구·라이브 배선 확정 후 별도 결정**(운영·PM 영역, 현재 writer 설계 무영향).
- **측정시각 vs 적재시각**: DDL은 `created_at`(적재 시각)만 있고 rollup에도 ts 없음 → 비교의 시간 정체성 = 적재 시각. 라이브에서 측정시각이 필요해지면 DDL 컬럼(PM)·rollup 확장 별도(현재 무영향).
