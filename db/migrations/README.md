# DB 마이그레이션 규약 (2026-07-31 신설 — PM)

## 왜 필요한가

`db/init.sql` 은 **신규 설치 전용**이다. 전 테이블이 `CREATE TABLE IF NOT EXISTS` 이고,
compose 가 마운트한 `/docker-entrypoint-initdb.d/` 는 **데이터 디렉토리가 빌 때만** 실행된다.
즉 컬럼을 추가하고 init.sql 을 다시 돌려도 **이미 떠 있는 DB 에는 아무 일도 일어나지 않는다.**

7/30~31 사이 이 벽에 세 사람이 각자 부딪혔다 — C 는 `brief_version` 등 3종을 못 붙여 대기,
A 는 `db/migrations/0001` 을 자체 규약으로 만들었고, PM 은 `scripts/` 에 따로 만들었다.
같은 문제에 세 가지 답이 생기던 것을 여기서 하나로 합친다.

> `docker compose down -v` 로 볼륨을 지우면 반영되지만 **쓰지 않는다** — `control_limits`·
> `y_thresholds` 재시딩이 필요하고 Qdrant KB 스냅샷까지 날아가, 로컬마다 상태가 갈린다.

## 규약

1. 파일명 `db/migrations/NNNN_<snake_case>.sql` — 4자리 일련번호, 충돌 시 뒤 번호를 쓴다.
2. **멱등**하게 쓴다 — `ADD COLUMN IF NOT EXISTS`, 인덱스는 `IF NOT EXISTS` 또는 존재 확인
   `DO` 블록. 두 번 돌려도 안전해야 한다 (누가 이미 돌렸는지 추적하지 않는다).
3. 파일 끝에 **확인 쿼리**를 넣는다 — 적용됐는지 눈으로 보고 넘어갈 수 있게.
4. `db/init.sql` 도 **같은 PR 에서 함께** 갱신한다 (신규 설치 경로 정합 — 헌법 4-3).
   init.sql 은 PM 소유(3-1)이므로 타 파트는 PM 리뷰를 받는다.
5. 컬럼 **추가**만 자유롭다. 삭제·개명은 헌법 3-2 위반이다.

## 실행 (PowerShell — stdin 파이프 금지)

```powershell
docker cp db\migrations\0002_agent_reports_supervisor_cols.sql fdc-postgres:/tmp/m.sql
docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -v ON_ERROR_STOP=1 -f /tmp/m.sql
```

`Get-Content | psql` 파이프는 인코딩·개행이 변형돼 구문 오류를 낸다(2026-07-30 실측).
`-v ON_ERROR_STOP=1` 이 없으면 트랜잭션 안의 에러가 **조용히 전체 롤백**으로 끝나 "완료"처럼
보인다. 두 가지 모두 실제로 당한 사고다.

## 적용 순서

새 컬럼을 **읽는 코드보다 마이그레이션이 먼저** 나가야 한다. 순서가 뒤집히면 그 컬럼을
SELECT 하는 서비스가 전부 죽는다 — 팀 전체 로컬에서 동시에.

권장: 마이그레이션 PR 을 먼저 머지 → 각자 `git pull` 후 위 명령 1회 → 소비 코드 머지.

## 목록

| 파일 | 내용 | 도입 |
|---|---|---|
| `0001_approval_pending_unique.sql` | `approval_records` PENDING 부분 유니크 인덱스 (헌법 1-4 DB 강제) | A · PR #73 |
| `0002_agent_reports_supervisor_cols.sql` | `supervisor_verdict`·`supervisor_evidence`·`supervisor_counter` (S4 근거·반증 유실 갭) | PM · 2026-07-31 |
| `0003_brief_versioning_and_snapshot.sql` | `brief_version`·`covered_signatures`·`evidence_snapshot` (서사층/증거층 분리 — PR #70 결정) | PM · 2026-07-31 |
| `0004_add_settle_converged.sql` | `limit_corrections.settle_converged` (B6-3-e ② 미수렴 폴백 플래그 영속 — `a906bb5` 가 init.sql 에만 넣어 기동 중 DB 에는 안 붙던 컬럼) | B · 2026-07-26 |
| `0005_agent_reports_supervisor_history.sql` | `agent_reports.supervisor_history` JSONB — D10 Supervisor 판단 변경 이력 append-only (#97 리뷰 공약 · 새 테이블 대신 컬럼, 3-2) **(#100 머지 — 2026-08-08)** | PM · 2026-08-04 |
| `0006_tttm_suspect_sensors.sql` | `tttm_comparisons.suspect_sensors` JSONB (경로① reference_suspect decouple — 실제 공통이동 센서 목록) | B · 2026-08-05 (#113) |
| `0007_incidents_incident_type.sql` | `incidents.incident_type` — 7/28 P6-1 이 init.sql·그루퍼 INSERT 를 같은 커밋에서 고치면서 빠뜨린 소급분 (아래 번호 재조정 이력의 "철회" 참조) | C · 2026-08-07 (#130) |
| `0008_chamber_inhibits.sql` | `chamber_inhibits` 신규 — RTD 자동 정지 이력 (헌법 1-1 예외 4 · S1 배지·uptime 단일 소스 · 부분 유니크 = 챔버당 열린 inhibit 1건) | PM · 2026-08-06 |
| `0009_chamber_inhibits_scope.sql` | `chamber_inhibits.scope`·`equipment_id` — 정지 스코프 2단화(챔버/장비. 멘토 8/6 "장비 단위로도 정지한다"). 장비 정지도 행은 챔버별 + `scope='equipment'` 로 묶어 신호·해제 로직 무변경 재사용 | PM · 2026-08-06 |
| `0010_quals_confirmed_verdict.sql` | `quals.confirmed_verdict`·`confirmed_at` — **P6-1(7/28) 소급분**. 컬럼·쓰기(`qual_recorder`)·테스트는 그때 전부 들어갔는데 **3-2 규약(7/31)보다 사흘 앞선 작업이라 마이그레이션만 빠졌다**. 구 볼륨 DB 는 Qual 확정 UPDATE 가 실패한다(테스트는 skip 가드로 피하지만 **라이브는 못 피한다**). 소비처 = 재시작 복구(계약 §8-B 갭)·CT⓪ 부트스트랩 경계 조회 | PM · 2026-08-07 |
| `0011_limit_corrections_modified_cols.sql` | `limit_corrections.center_modified`·`ucl_modified`·`lcl_modified` — B5-1(07-23) 소급. **수정 승인(MODIFIED) 경로 전체**가 이 셋에 걸린다(gateway 쓰기·orchestrator 매핑·B `approval_apply` 읽기). 🔴 셋 중 가장 무겁다 — 읽는 쪽(`_SELECT_PROPOSED`)이 컬럼을 명시 나열하고 try/except 가 없어 **MODIFIED 만이 아니라 평범한 APPROVE 까지 적용이 막힌다** | C · 2026-08-07 |
| `0012_wafer_predictions_ct0_bias.sql` | `wafer_predictions.bias_applied`·`pm_count` + 라벨 부분 인덱스 (CT⓪ Model R2R — raw 예측 복원·레짐 자격 축) | A · 2026-08-07 |
| `0013_prior_transitions.sql` | `prior_transitions`·`prior_markers` 신설 — 프라이어 전이 원장(BL2) + 마커 버전 이력. 추정→실측이 한 행에서 닫히며 오차 확정, 마커 개정은 승인 경유. **자(basis) 컬럼 3종**(`delta_basis`·`y_basis`·`fit_c`)으로 "계수는 (Δ자 × Y자) 조합과 세트로만 유효"를 스키마가 강제한다 | PM · 2026-08-08 |
| `0014_release_briefs.sql` | `release_briefs` 신규 — RTD 해제 근거 Brief (인프라 스텝 3 · #140). 정지 1건당 회고 3층(왜 섰나/얼마나 심했나/과거엔 뭘로 풀렸나)을 남겨 R9 승인 화면 S7 이 읽는다. **해제를 실행하지 않는다**(1-1 예외 4 ⓒ — 해제는 requal 하류 하나뿐). `agent_reports` 재사용 불가 사유는 파일 헤더. *(초안 `0012` → 개번 2026-08-10 — 네 번째 번호 충돌)* | C · 2026-08-08 |

> **컬럼 표류 3형제 (`0007`·`0010`·`0011`)** — 2026-08-07 C 가 컨테이너화 중 `init.sql` 전수
> 대조로 발굴한 6건의 나머지 전부다. **셋 다 3-2 규약(7/31) 이전 변경**이라 아무도 규칙을
> 어기지 않은 채 구멍이 남았다(P6-1 7/28 · B5-1 7/23 · incident_type 7/28).
> **증상이 공통으로 두 겹 조용하다** — ⓐ 화면·로그는 정상이고 DB 만 안 되며 ⓑ 테스트가
> 컬럼 존재를 probe 해 skip 하므로 **CI 도 초록불**이다. 발견 조건도 공통이었다: *"컴포넌트
> 소유자는 안 걸리고(볼륨을 새로 만들어서), 걸리는 사람은 그 컴포넌트를 안 돌린다"* — 인프라를
> 맡아 **남의 컴포넌트를 띄워본 것**이 발견 경로였다.
> **재발 방지**: `init.sql` 변경 PR 은 `db/migrations/NNNN_*.sql` 동반 필수(3-2, 이미 규약).
> 과거분은 이 셋으로 종결됐다 — 전수 대조 근거는 #130·#134 PR 본문.

> **번호 재조정 이력 (2026-08-06)**: chamber_inhibits 계열은 0004 → 0006 → **0008/0009** 로 두 번
> 밀렸다. 워크트리 6개가 병행 개발되며 같은 번호를 각자 집은 결과다(0004는 guard 브랜치의
> settle_converged, 0006은 #113 의 tttm_suspect_sensors 와 충돌). **전부 멱등이라 이미 적용한
> DB 는 재적용해도 무해**하다. 재발 방지: 새 마이그레이션 번호는 `git branch -r` 의 미머지
> 브랜치까지 확인하고 딴다. (~~`0007` 은 결번~~ → **철회 (2026-08-07, #118 C 리뷰 조율)**:
> C 컨테이너화 브랜치의 `0007_incidents_incident_type.sql` 이 그 번호를 실사용한다 — 7/28
> P6-1 이 init.sql 에만 넣고 마이그레이션을 빠뜨린 `incidents.incident_type` 의 소급분.
> 3-2 규약(7/31 신설)보다 사흘 앞선 변경이라 생긴 구멍으로, 구 볼륨 환경에서 **Incident
> 적재가 7/29에 조용히 멈춰 있던** 원인. 다음 번호는 **0010부터**.)
>
> **후속 (2026-08-08, dev 머지)**: CT⓪ 브랜치(`feat/ct0-model-r2r`)가 08-07 에 `0007` 로
> 딴 `wafer_predictions` 컬럼 추가분을 **`0012` 로 재번호**했다 — 위 철회로 `0007` 이
> C 컨테이너화 브랜치(incidents.incident_type) 실사용분으로 확정된 뒤 dev 를 받아
> 충돌이 드러난 건이다. 파일은 멱등이라 `0007` 로 이미 적용한 로컬 DB 는 그대로 두면 되고,
> 재적용도 무해하다.
>
> **후속 (2026-08-09, dev 머지)**: `0012` 가 CT⓪ 분으로 확정되면서 프라이어 원장이 **`0013` 으로
> 재번호**됐다(같은 파일을 8/8 에 `0012` 로 딴 것 — 세 번째 번호 충돌이다).
>
> **후속 (2026-08-10, #154 머지 전)**: 해제 Brief 가 **`0012` → `0014` 로 개번**됐다 — 8/8 에 `0012` 를
> 딴 세 번째 파일이고 **네 번째 번호 충돌**이다. `0012`(CT⓪)·`0013`(프라이어 원장)이 먼저 dev 에
> 들어가 규칙대로 늦은 쪽이 비켰다. **다음 번호는 0015부터.**
> 이 파일이 상습 충돌 지점인 이유는 하나다 — 새 마이그레이션은 **항상 목록 끝에 한 행을 붙인다.**
> 그래서 두 브랜치가 같은 날 파일을 만들면 번호와 README 행이 동시에 겹친다. 브랜치를 딸 때
> `git log --oneline -1 origin/dev -- db/migrations/` 로 마지막 번호를 확인하고, 겹치면
> **머지가 늦은 쪽이 개번**한다(파일명 + 이 목록 + 아래 적용 상태 표 3곳).

`0001` 은 적용 전 **중복 PENDING 0건**을 확인하는 가드를 포함한다. 적용 선행 조건인
23505 흡수는 **두 함수 모두** 반영됐다 — `approval_graph.open_pending` (선행) ·
`ct2_deploy_approval.open_ct2_pending` (**#100 머지(2026-08-08)** — dev 반영 완료).
후자는 흡수와 함께 **커넥션 반납 위생**도 같이 넣었다: 예외가 그대로 나가면 aborted
트랜잭션 상태의 커넥션이 pool 로 돌아가 **다음 대여자**가 InFailedSqlTransaction 을 맞는다.

## 적용 상태 (2026-08-07 기준)

| 파일 | 상태 |
|---|---|
| `0001` | **적용 완료 (2026-08-03)** — ① 위반 조회 0건 → ② `uq_approval_pending_per_incident` 생성 → ③ **앱 가드 정합 완료(08-03, #100)** |
| `0002`·`0003` | 로컬 DB 적용됨 (07-31 — `agent_reports` supervisor/brief 컬럼 실재 확인 08-02) |
| `0004` | 로컬 DB 적용됨 (07-26 — `limit_corrections.settle_converged`) |
| `0005` | **미적용** — #100 머지 후 각자 1회 적용. 쓰기 배선이 C 후속이라 선적용 무해(컬럼 NULL 존재만) |
| `0006` | **머지 직후 각자 1회 적용 필수** (#113 — `tttm_writer` 라이브 적재가 이 컬럼에 쓴다. 미적용 시 UndefinedColumn 으로 tttm 적재 중단) |
| `0007` | **머지 직후 각자 1회 적용 필수** (#130 — 미적용이면 그루퍼가 Incident 를 하나도 못 만든다. 프로세스는 살아 있고 로그만 남아 **밖에서는 정상으로 보인다**) |
| `0008`·`0009` | PM 로컬 적용됨 (08-06) — RTD 자동 정지 계열 |
| `0011` | **머지 직후 각자 1회 적용 필수** — 미적용이면 수정 승인(MODIFIED)이 저장되지 않고 원안으로 폴백한다. **예외도 에러 로그도 없다** — 화면은 이미 "조치 나감" 이라 엔지니어는 자기가 고친 값이 걸렸다고 믿는다 |
| `0012` | A 로컬 적용됨 (08-07 — 당시 파일명 `0007`). **머지 직후 각자 1회 적용 필수** — `prediction_sink` 가 두 컬럼에 쓰므로 미적용이면 라이브 적재가 UndefinedColumn 으로 끊긴다 |
| `0013` | PM 로컬 적용됨 (08-08, 자 컬럼 보강 08-09). **머지 직후 각자 1회 적용 필수** — 미적용이면 프라이어 원장 CLI·시드 라우트가 UndefinedTable 로 멈춘다(라이브 적재 경로는 무영향이라 조용히 실패한다). 재적용 무해 — `CREATE TABLE IF NOT EXISTS` + `ALTER ... ADD COLUMN IF NOT EXISTS` + `ON CONFLICT DO NOTHING` |
| `0014` | C 로컬 적용됨 (08-09 — 당시 파일명 `0012`). **머지 직후 각자 1회 적용 필수** — 미적용이면 해제 Brief 폴러가 적재 단계에서 UndefinedTable 로 멈춘다. ⚠️ **`0012` 로 이미 적용한 로컬 DB 는 그대로 두면 된다** — 테이블 정의가 같고 파일이 멱등이라(`CREATE TABLE IF NOT EXISTS`) 재적용해도 무해하다 |

> ⚠️ **적용 상태는 각자 로컬 DB 기준이다.** 위 표는 PM 로컬에서 확인한 값이고,
> 팀원 DB 에 자동으로 반영되지 않는다 — `git pull` 후 각자 1회 적용해야 한다.

### 내 DB 가 뒤처졌는지 한 줄로 보기

```powershell
docker exec fdc-postgres psql -U fdc_admin -d fdc_platform -t -c "SELECT CASE WHEN EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='incidents' AND column_name='incident_type') THEN 'OK' ELSE 'MISSING' END"
```

`MISSING` 이면 `0007` 미적용이다 (`pg_data` 볼륨을 2026-07-28 이전에 만든 환경 전부 해당).

