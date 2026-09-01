# TSR-0003: 통합테스트 teardown 이 라이브 관리선 180행을 삭제 — 가드 경고를 끄는 방향으로 고쳐서 터졌다

## Status

Resolved

## Context

- **발생일**: 2026-08-09 (원인 조치 2026-08-08 심야 → 발각 2026-08-09 오전)
- **담당자**: 팀원 B (팀원 B)
- **관련 컴포넌트**: `src/agent_b_spc/tests/test_control_limits_loader.py::seeded_engine`,
  `src/agent_b_spc/seed_control_limits.py::seed`/`assert_no_conflicting_active`,
  `control_limits` 테이블
- **증상**: Step6 검증(T3) 준비 중 KEEP\* 분위수 그룹을 조회하다 발견 —

  ```
  SELECT method, is_active, count(*) FROM control_limits GROUP BY 1,2;
    quantile | f |  69
    sigma    | f | 412
                          ← 활성 행 0.  v1(시딩 기준선) 은 아예 없음
  ```

  그런데 **컨슈머는 멀쩡히 판정을 내고 있었다**:

  ```
  02:39:58  control_limits 로드 완료: 그룹 180개      ← 마지막 로드(메모리에 상주)
  02:40:39  판정 C64_17974_CH_4: nelson=[{'sensor':'C58','rule':'N5'}]
  ```

  프로세스가 기동 시 로드한 180그룹을 메모리에 들고 있어 **DB 가 비어도 겉으로는 정상**이다.
  **재시작하는 순간 0그룹을 읽고 Nelson 위반이 전량 사라진다** — 예외도 ERROR 도 없이
  "정상 동작 중 탐지 0". 시연 중 컨테이너를 한 번만 재시작하면 그대로 무증상 실패한다.

- **왜 늦게 발견됐나**: 두 겹이다. ⓐ 삭제된 것이 **읽기 전용으로 소비되는 기준 데이터**라
  쓰기 실패가 없다. ⓑ 소비자가 **메모리 캐시**라 삭제와 증상 발현 사이에 재시작이라는
  지연이 낀다. 사고와 증상이 시간·공간적으로 분리돼 있어 인과를 잇기 어렵다.

## Decision

### 근본 원인 — teardown 의 DELETE 가 "이번 시딩분"이 아니라 전역이었다

```python
# tests/test_control_limits_loader.py (구)
@pytest.fixture
def seeded_engine(engine, rows):
    seed(engine, rows)
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM control_limits WHERE trigger_type = :t"),
                     {"t": TRIGGER_INITIAL})        # ← 조건이 이것뿐
```

`rows` fixture 는 **실 CSV · 실 offsets** 에서 만든다 — 즉 라이브와 **같은 180행**이다.

```python
limits_df = pd.read_csv(_DEFAULT_CSV_DIR / "initial_limits_v1.csv")
offsets = load_offsets(_DEFAULT_OFFSETS_PATH)      # SIM_CH_1~4
return build_seed_rows(...)                        # 45 그룹 × 4 챔버 = 180
```

따라서 **이 테스트가 성공할 때마다 라이브 기준선이 삭제된다.** 복원한 `v1` 이 정확히
180행인 것이 그 지문이다.

`seed()` 의 DELETE 범위가 전역인 것은 **의도된 설계**다(`seed_control_limits.py:213-215`
— *"재생성으로 사라진 그룹의 옛 행도 통째로 제거·재구성"*). 챔버 네임스페이스로 테스트를
분리해도 소용없다. 문제는 **운영 DB 를 공유한 채 그 함수를 진짜로 실행한 것**이다.

### 사고 경로 — 가드가 울렸는데, 가드를 끄는 쪽으로 고쳤다

```
① 라이브 정상 (정기 재산정이 만든 periodic 활성 62행)
     → seed() 의 assert_no_conflicting_active 가 setup 에서 실패, 테스트 6건 ERROR
     → ★ 이것이 정상 신호다. 가드가 라이브를 지키고 있었다.
② "테스트가 깨졌다"고 판단 → periodic 행을 비활성화
     → 가드 통과 → 테스트 PASS
③ teardown 이 initial 180행 DELETE → 라이브 기준선 소실
④ 활성 0.  컨슈머는 캐시로 계속 동작 → 무증상
```

**②가 사고의 전부다.** `assert_no_conflicting_active` 는 *"해당 행을 먼저 정리하세요"* 라고
정확히 말하고 있었는데, 그 메시지를 **테스트 실패의 원인**으로 읽고 조건을 제거했다.
가드는 자기 일을 했고, 사람이 가드를 무력화했다.

TSR-0002 와 정확히 대칭이다 — 그쪽은 *"예외가 안 났으니 성공"*, 이쪽은 **"에러가 났으니 고장"**.
둘 다 신호의 의미를 확인하지 않고 신호의 유무만 봤다.

### 채택 해결 — 전용 스키마 격리 + 격리 자체를 왕복 확인

프로덕션 코드(`seed_control_limits.py`)는 **한 줄도 고치지 않는다.** DELETE 전역 범위는
정당한 설계이므로, 바꿀 것은 **테스트가 보는 테이블**이다.

① **전용 스키마 사본** — 모듈 fixture 가 `b_spc_test` 스키마에
   `CREATE TABLE ... (LIKE public.control_limits INCLUDING ALL)` 로 사본을 만든다.
   DDL 단일 소스는 `db/init.sql` 그대로이며(복제라 컬럼·인덱스 자동 추종), 특히 부분 UNIQUE
   `idx_control_limits_active_one`(활성 1개)까지 따라와 **가드 계약이 실제로 검증된다.**

② **`search_path` 주입** — 테스트 엔진만 `options=-csearch_path=b_spc_test,public` 로 만든다.
   `seed()`·`load()` 는 테이블 이름만 쓰므로 **스키마를 모르는 채 격리된다.** `public` 을
   뒤에 두는 것은 사본의 id DEFAULT 가 public 시퀀스를 참조하기 때문.

③ **격리를 가정하지 않고 확인** — fixture 가 `control_limits` 의 실제 해석 스키마를 조회해
   전용 스키마가 아니면 `RuntimeError` 로 즉시 중단한다. 여기서 조용히 `public` 이면 라이브가
   지워지므로, **이 확인이 fixture 의 존재 이유**다. `assert` 가 아니라 `raise` 인 것은
   `python -O` 로 방어선이 사라지지 않게 하기 위함(헌법 7장).

④ 모듈 종료 시 `DROP SCHEMA CASCADE`.

### 검토했으나 채택하지 않은 안

| 안 | 기각 사유 |
|---|---|
| teardown DELETE → `seed()` 재실행(원상복구) | 파괴는 막지만 **가드 충돌(①)은 그대로** — 라이브 운영 중엔 여전히 setup 실패. 테스트 중간 크래시 시 라이브가 지워진 채 남는다 |
| 테스트 전용 챔버(`TEST_CH_*`) 네임스페이스 | `seed()` 의 DELETE 가 챔버가 아니라 `trigger_type+limit_version` 전역이라 **효과 없음** |
| `TEST_DATABASE_URL` 별도 DB | 동작하지만 팀원마다 DB 를 하나 더 띄워야 한다. 스키마 격리로 같은 효과를 인프라 추가 없이 얻는다 |
| `seed()` 에 스키마 인자 추가 | 프로덕션 코드를 테스트 사정으로 바꾸는 것. 소비자(운영 시딩 CLI)가 모르는 축이 생긴다 |

## Consequences

**복구 (2026-08-09) — ⚠️ 초판 서술 정정**

> **초판은 "백업 테이블에서 복원했다"로 적었고 그것을 해결로 기록했다. 그 복원은 틀렸다.**
> 아래는 정정본이다(2026-08-09 후속). 코드 수정(스키마 격리)은 그대로 유효하며, 바뀌는 것은
> **이미 지워진 데이터를 되살리는 방법**뿐이다.

**틀린 복구 — 백업 테이블 복원**

`control_limits_backup_20260806` 에서 전체 복원했다. 행 수·활성 그룹 수가 다 맞아
(376행 · 활성 180 · quantile 24) 정상으로 보였고, 컨슈머 재기동 왕복 확인도 통과했다
(`control_limits 로드 완료: 그룹 180개`).

**그러나 그 백업은 #117(2026-08-06 σ 재보정) 「직전」 스냅샷**이다 — `σ재보정_실행체크리스트`
가 롤백 대상으로 만든 것이고, 일반 백업이 아니다. 복원 결과 관리선이 시뮬레이터 데이터와
어긋났다:

| C62 step1 · SIM_CH_1 | center |
| --- | ---: |
| 시딩 CSV(-3657.9) + 챔버 오프셋(+144.0) | **-3513.9** |
| 실제 관측값 | **-3511.0** ✅ |
| **복원된 백업** | **-2937.8** ❌ 576 어긋남 |

**행 수가 맞아서 알아채지 못했다.** 값이 틀린 것은 다음 라운드(T4 1차)에서 점수가 전부
clip 상한 100 에 포화되고 나서야 드러났다(C62 가 **160σ** 이탈로 계산). 그 라운드는 폐기했다.

**맞는 복구 — CSV 재시딩**

```bash
docker exec ... psql -c "UPDATE control_limits SET is_active=false WHERE trigger_type <> 'initial';"
python -m src.agent_b_spc.seed_control_limits          # CSV 기반 · 멱등 · 항상 현재 정본
```

`initial_limits_v1.csv` + `chamber_offsets.json` 이 관리선의 **단일 소스**이므로, 재시딩은
정의상 현재 정본과 일치한다. 백업 테이블은 "언제 찍힌 것인지"를 따로 알아야 쓸 수 있다.
시연 런북(#150)도 같은 경로를 규정한다 — `docker compose run --rm spc-seed`.

**교훈**: 스냅샷을 복구 소스로 쓰기 전에 **어느 시점 상태인지** 확인한다. 행 수·스키마가
맞는 것은 **값이 맞다는 뜻이 아니다.** 재생성 가능한 소스(CSV·시딩 스크립트)가 있으면
그쪽이 우선이다.

**백업 테이블 2개 삭제 (2026-08-09)** — `control_limits_backup_20260806`,
`control_limits_backup_before_restore_20260809`. 같은 오인을 다시 부르는 함정이고, 복구
경로는 재시딩으로 일원화했다. #117 은 8/6 머지 후 검증(오탐 45.9%→12.8%)·3일 안정이라
롤백 대상이 아니며, 필요하면 CSV 를 되돌려 재시딩하면 된다.

**측정 결과 영향 없음** — 마지막 정상 로드가 2026-08-09 02:39:58 이고 Step6 측정
(T1·T7·T11·T14·T18·T2·G2·G3·G6·G7·라벨 세트 5종)은 전부 그 이전이다. 당시 DB 는 180행 활성이었다.
복원 이후 라이브를 탄 것은 **T4 1차뿐**이고, 그것만 재실행했다(T4 2차 = 정본).

**검증**

| | 수정 전 | 수정 후 |
|---|---|---|
| `test_control_limits_loader.py` | 6 errors (setup) | **6 passed** |
| `test_publisher.py` | 64 passed **+ 라이브 61,503행 삭제** | **65 passed · 라이브 무변화** |
| 두 스위트 합계 | — | **1,005 passed / 2 skipped** *(별건 4 fail — 아래)* |
| 실행 후 `public.control_limits` 활성 | 180 → **0** | 180 → **180** |
| 실행 후 `public.spc_violations` | 61,503 → **0** | 567 → **567** |
| 임시 스키마 잔존 | — | 0 (DROP 확인) |

> 별건 4 fail = `test_axes.py`·`test_orchestrator.py` — `params.yaml` 의 e4/e5 가 8/5 에 0 으로
> 바뀌었는데(`context_score_reference_suspect_penalty` 등, "재분류 후 복원" 임시값) 테스트가
> 20.0 을 못박고 있다. **dev 기존 상태이며 본 수정과 무관**하다. 헌법 7장의 *"config 플립하면서
> 테스트 안 고침"* 사례로, 임시값을 어떻게 검증할지 결정이 필요해 별건으로 남긴다(CI 미대상).

**★ 동형 패턴 전수 스캔 (2026-08-09 · PM 제공 — #160 리뷰)**

초판의 *"다른 파트에도 있을 수 있다"* 를 실제로 훑었다. `test_*.py` 의 `TRUNCATE`/`DELETE FROM` 전수:

| 위치 | 내용 | 판정 |
| --- | --- | --- |
| **`common/context_score/tests/test_publisher.py:641-653`** | `DELETE FROM spc_violations` — **WHERE 절 없음** | 🔴 **발동형** — 아래 |
| `agent_b_spc/tests/test_spc_consumer.py` (1338·1436·1557 외) | 챔버 한정 DELETE | ✅ 무해 — `_L3_CH="TEST_CH_CORR_L3"`·`_E2E_CH="TEST_CH_E2E_C"` (전용 테스트 챔버) |
| `test_spc_consumer_qual.py:220-221` | `quals`·`qual_snapshots` 챔버 한정 | ✅ 무해 (동일 패턴) |
| `tests/test_api_live.py:84-85` | `incident_id` 한정 | ✅ 무해 (협소·CI ignore) |

**`test_publisher.py` 는 이번 건과 같은 계열이고 범위가 더 크다.** `pg_only` 가
`skipif(not _db_available())` 라 **`DATABASE_URL` 이 붙는 로컬에서 그대로 발동**한다.
2026-08-09 재현: **61,503행 → 0행**. 예외 없음, 64 passed.

**이것이 "spc_violations 가 왜 0행인가" 미스터리의 답**이기도 하다 — T4 준비 중 이 표가
비어 있어 원인을 못 찾았는데, 같은 세션에서 내가 이 테스트를 돌린 것이 원인이었다.
`control_limits`(#160)와 **완전히 같은 형태**가 다른 테이블에서 한 번 더 있었다.

조치: 같은 스키마 격리를 적용하고, 격리 기계를 공용 모듈
`src/common/context_score/tests/db_schema_isolation.py` 로 뺐다(두 벌로 두면 다음에 또 갈라진다).

**남은 리스크**

- 스키마 사본은 `LIKE ... INCLUDING ALL` 이라 **FK·트리거는 복제되지 않는다**. 현재 두 테이블
  모두 없어 무해하나, 추가되면 헬퍼를 함께 갱신해야 한다.
- 스캔은 `test_*.py` 의 `TRUNCATE`/`DELETE FROM` 문자열 기준이다. **`UPDATE`·`INSERT` 로
  라이브를 오염시키는 경로는 안 봤다** — 파괴적이지 않아 눈에 덜 띄지만 같은 계열이다.
- **관리선 백업 정책은 여전히 없다.** 이번엔 재시딩으로 풀었지만, 재생성 불가한 상태
  (라이브 재산정으로 누적된 periodic 행 등)는 그 방법으로 못 되살린다 — 별건으로 남는다.

**재발 방지**

- 헌법 7장에 한 줄 추가 (아래).
- `docs/adr/README.md` 인덱스에 본 문서 등재.

```
❌ 공유 DB 를 쓰는 통합테스트의 teardown 을 조건 없는 DELETE 로 작성
   → ✅ 전용 스키마·DB 로 격리하고, 격리됐는지를 fixture 가 조회로 확인한다
❌ 가드가 낸 실패를 "테스트가 깨졌다"로 읽고 가드 조건을 제거
   → ✅ 실패 메시지가 지목한 상태를 먼저 확인한다
```

## 참고

- 헌법 1-1(관리선 변경 승인) · 4-1(모델·버전 이력 훼손 금지) · 7장 · 8장
- `TSR-0002` — 대칭 사례(예외 없음을 성공으로 읽음)
- `db/migrations/README.md` — 스키마 변경 규약(본 건은 스키마 변경 아님, 데이터 사고)
