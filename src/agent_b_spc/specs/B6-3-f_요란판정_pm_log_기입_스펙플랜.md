# B6-3-f — 요란(loud) 판정 확정 시 pm_log.json 런타임 기입 스펙플랜

> 작성: 2026-07-28 · 상태: **구현 완료 (브랜치 대기 — D1 A 회신 전 머지 금지, §7)**
> 갱신 2026-07-28: §4 전 파일 구현·§5 테스트 통과(코어 10 + writer 13, A 파서 호환 2는 xgboost 미설치 환경 skip·라운드트립 수동 실증 완료)·계약 §8-B-1 신설. **잔여 = D1 회신 + E2E 수동 검증**.
> 트리거 정의: `provisional_mode.py`의 **PHASE_0 상태에서 `on_qual_verdict(chamber, "loud")`가 수리되어 Phase 1로 전이하는 순간** (= "phase 0이 요란 loud를 출력"), 그와 동시에 `pm_log.json`에 요란 PM 1건을 기입한다.
> 선행 문서: `B6-3-a/c` 스펙플랜 · `회의_브리핑_2026-07-27.md` §6-2/§7/§8 · 계약 §8-B · `lean85/README.md` §4·§5

---

## 0. 배경 — 왜 지금 필요한가

`pm_log.json`은 **A 예측(lean-85) 메타 피처의 유일 입력**이다: `days_since_last_pm`·`is_high_regime`·`high_regime_days`·`dslp_x_hour`(`lean85_pipeline._meta_features` :245) + **이벤트 재학습 트리거**(`should_retrain` :437, README §4 "요란 PM 기입 즉시"). 7/27 §6-2에서 파일 부재로 인한 dummy fallback은 해소됐지만 **런타임 요란 PM 기입 주체가 미결**(§7 A 회신 대기, §8 "구현 미착수")이라, 라이브 중 레짐 전환이 일어나도 A 피처·재학습 루프에 영원히 반영되지 않는 상태다. 본 플랜은 이를 **B 가한계 상태머신의 loud 확정 지점**에서 닫는 구현안이다.

**B측 기입의 논거**: `QualVerdictConfirmed`(계약 §8-B)에는 `confirmed_at`·`pm_count`만 있고 **PM 개방 시각이 없다**. PM 개방 시각(pm_count↑ 감지 wafer의 timestamp)은 B 컨슈머만 관측한다 → 가장 정확한 `date`를 쓸 수 있는 주체가 B다.

## 1. 전수 조사 — phase 0 · loud · pm_log.json 사용처 지도

### 1-1. 판정·유입 경로 (B측)

| 파일 | 관련 지점 | 본 변경과의 관계 |
|---|---|---|
| `provisional_mode.py` | `on_qual_verdict` :151(loud 분기 :165) · `on_pm` :134 · `__init__` :96 | **기입 트리거 본체.** 순수 코어 — 부작용은 전부 주입 seam (모듈 docstring·1-1 원칙) |
| `spc_consumer.py` | `fdc.agent` 구독 :54 · `_handle_agent_event` :536-541(verdict 정제→`on_qual_verdict`) · `_drive_provisional` :424-426(`on_pm` 호출) · `_build_provisional` :714 | verdict 유입 경로 + seam 배선 지점. **PM 개방 시각(`rows[-1]["timestamp"]`) 보유 지점** |
| `provisional_seams.py` | `ProvisionalWriter`·`derive_restore_state`(:89 `verdict='loud'` 함의 복구) | seam 실구현 모음 — 신규 writer의 자매 모듈. 복구 경로는 **기입 재실행 안 함**(이미 기입됨 전제, §3-2) |
| `establish_engine.py` | `seasoning_loud` 소비 | 영향 없음 (참조만) |
| `tests/test_provisional_mode.py` 외 2종 | loud 경로 다수 | 테스트 추가 대상 (§5) |
| `analysis/b63e_settle_validation.py` | ProvisionalMode 재사용 | 시그니처 하위호환이면 무변경 |

### 1-2. verdict 발행측 (참조만 — 변경 없음)

| 파일 | 역할 |
|---|---|
| `orchestrator/regime_events.py` | `QualVerdictConfirmed` 빌더 (loud면 `incident_id` 필수, `pm_count`·`confirmed_at` 포함) |
| `gateway/main.py` :285 | 판정 확정 API → 발행 |
| `agent_b_spc/agent_mock.py` | dev mock 발행기 — **E2E 테스트 주입 도구** (§5) |
| `simulator/scenarios/qual_loud_ch4.yaml` | 요란 시나리오 (SC2, `verdict: loud` 기대) — E2E 검증 시나리오 |

### 1-3. pm_log.json 소비측 (A — 계약상 이해관계자)

| 파일 | 관련 지점 | 함의 |
|---|---|---|
| `lean85/lean85_pipeline.py` | `parse_pm_log` :181(**mtime+size 캐시** — 갱신 시 자동 무효화) · `_parse_pm_entries` :206(legacy 날짜문자열/dict 겸용, **dict는 `date`·`type`만 읽고 여분 키 무시**) · `_meta_features` :245(**date만 사용, 챔버 무관 전역**) · `should_retrain` :437(신규 date → 재학습 권고) · `retrain` :404(manifest `pm_log_dates`) | 여분 키 추가는 하위호환. **기입 즉시 다음 wafer부터 피처·재학습 권고에 반영** |
| `consumer.py` (A) | `LEAN85_PM_LOG` env :15("§6-2 확정 전 임시") · 매 wafer `build_wafer_table` :174 · `rows_to_trace` :278 — **C40 ← 수신 timestamp를 naive(tz 제거)로 변환** :275·:301 | 라이브 시간축 = **naive UTC wall-clock** → 기입 `date`도 동일 축·naive 필수 (§3-4) |
| `predict_lean85.py`·`retrain_lean85.py` | `find_file("pm_log.json")` 탐색 (lean85/ → frozen/ → … → **리포 루트**) | 경로 단일화 필요 (§3-5) |

### 1-4. 파일 실체·규약 문서

- **운영본 = 리포 루트 `pm_log.json`** (현재 1건: `{"date":"2018-12-24","type":"major","verdict":"loud"}`) — README §2 "운영은 루트 최신본 사용".
- `lean85/frozen/pm_log_snapshot.json`(동결·수정 금지) · 루트 `pm_log_meta.json`(EDA 산출 메타 — 검출 규칙·major/minor) · `.gitignore` :132에 `lean85/pm_log.json` 등재(**사본 출현 시 find_file이 루트보다 먼저 집는 위험** — §3-5 가드).
- 규약: "요란(loud) PM만 기입"(`parse_pm_log` docstring) · `is_high_regime` 갱신 규칙 = **pm_log `verdict` 필드**(사이클_정의 §5, A 전달 완료) · README §4 이벤트 재학습 = "요란 PM 기입 즉시".

## 2. 선행 결정 (구현 PR 전 확정 — 미확정 시 **머지** 금지)

> **2026-07-28 진행 상태**: D2~D5는 **권고안대로 구현 확정**(B 내부 결정 — A 소비 계약에 영향 없는 범위이거나 계약 §8-B-1에 명문화해 A 리뷰로 회수). **D1만 A 회신 대기** — 회신이 "A 기입"으로 오면 §7대로 §3-3~3-6을 A측으로 이식한다. 구현은 브랜치에 올려두되 **dev 머지는 회신 후**.

| # | 결정 | 권고안 | 근거 |
|---|---|---|---|
| D1 | **기입 주체 확정** | **B(spc_consumer 프로세스) 단일 작성자, A는 읽기 전용** | 7/27 §7 발송 건(A 회신 대기)과 **정합 필수** — 본 플랜이 사실상 그 답안. A도 `fdc.agent`(QualVerdictConfirmed) 구독 예정(계약 §0, 현재 미구현 확인)이라 방치 시 **이중 기입** 위험 → 계약에 "작성자 = B 1곳" 명문화. **A(팀원 A님) 리뷰 승인 필수**(A 소비 파일 — 계약 2-2 정신) |
| D2 | `date` 값 | **PM 개방 시각**(pm_count↑ 감지 wafer timestamp) | `confirmed_at`(승인 시각)은 Qual 5장+승인 지연만큼 늦다. `days_since_last_pm` 기준점은 물리 PM 시점이 정본. fallback: 개방 시각 미보유(재시작 등) 시 confirmed_at |
| D3 | 시간 포맷 | **naive UTC `"%Y-%m-%d %H:%M:%S"`** (tz 접미사 금지) | A 트레이스 축이 naive UTC(:275). tz-aware를 섞으면 `_parse_pm_entries`의 `sorted`에서 aware/naive 비교 **TypeError → A 추론 전체 다운** |
| D4 | `type` 값 | `"major"` 유지 | 파서 기본값·기존 규약(요란↔대PM). 판정 의미는 `verdict:"loud"`가 운반(사이클_정의 §5) |
| D5 | 다챔버 시맨틱 | `chamber_id` **기입은 하되**, 챔버별 필터는 A 후속 | `_meta_features`는 현재 챔버 무관 전역 PM으로 계산 → CH4 요란이 타 챔버 피처에도 반영되는 **기존 모델링 단순화**. 본 PR 범위 밖, 리스크로 명시·A 공유 |

## 3. 설계

### 3-1. 순수 코어 seam (provisional_mode.py)

기존 주입 계약(writer·reload_fn·tttm·persist)에 **`pm_logger` 추가** — 코어는 파일 I/O를 모른다.

```python
# __init__(..., pm_logger=None)  # 키워드 전용·기본 None → 기존 호출부(테스트·b63e) 하위호환
# on_qual_verdict loud 분기 (:165):
elif verdict == "loud":
    self._enter_phase_1(chamber, st)
    self._log_pm(chamber, st)          # 신규 — 실패해도 Phase 1 진입에 영향 없음
```

`_log_pm`은 `pm_logger({chamber, pm_count=st["last_pm_count"], pm_detected_at=st["pm_detected_at"], verdict})`를 **try-except로 격리 호출**(에러 로그만, 6-2 정신 — pm_log는 A 피처 최신성 문제지 억제/광폭 안전 문제가 아니므로 절대 전이를 막지 않는다). 호출 순서는 `_enter_phase_1` **뒤**(광폭 적용이 우선순위).

멱등: `on_qual_verdict`의 phase 가드(PHASE_0만 수리)가 재전달을 이미 차단 → seam은 **챔버당 PM 사이클당 최대 1회** 호출. 파일 측 중복 방어는 §3-3.

### 3-2. PM 개방 시각 확보 (on_pm 확장)

- `on_pm(chamber, pm_count, ts=None)` — 선택 인자(하위호환). state에 `pm_detected_at` 저장, `_new_state`에 필드 추가.
- 호출부 `spc_consumer._drive_provisional` :426에서 `rows[-1]["timestamp"]` 전달 (`_handle_wafer`가 이미 보유한 계약 필수 필드).
- **재시작 복구 경로**(`derive_restore_state`)는 `pm_detected_at`을 파생 못 함(persist=None) → 복구 챔버는 이미 PHASE_1/AWAITING(=기입 완료 후)이라 **재기입 없음, 문제 없음**. PHASE_0 중 재시작은 기존에도 상태 유실(알려진 한계) — 본 건이 새 갭을 만들지 않는다.

### 3-3. 신규 `src/agent_b_spc/pm_log_writer.py`

`PmLogWriter(path)` — seam 실구현 (provisional_seams 패턴의 자매 모듈, 파일 I/O라 별도 파일):

- **원자적 append**: load(json) → `isinstance(list)` 가드(7장: `json.loads` 성공≠기대 타입) → 중복 검사 → append → **같은 디렉토리 tmp 파일에 쓰고 `os.replace`** (A가 mtime+size 캐시로 읽는 중 부분 쓰기 노출 금지).
- **Windows 재시도**: A 프로세스가 읽는 순간의 `os.replace`는 `PermissionError` 가능 → backoff **5회(총 ≈1.0s)**, 소진 시 엔트리를 **`pending`에 보관**한다. *(구현 시 개정 — 구 "3회 후 에러 로그"는 코어가 phase 가드로 재호출되지 않아 그 PM이 **영구 유실**된다는 리뷰 지적 반영)*
- **유실 방지 큐 — 보관 경로·회수 계기 2축** *(2차 리뷰 H1·M1)*:
  - 보관은 **모든 실패 경로**(원자 교체 실패 + 손상·읽기 실패)에서 한다. 손상 경로에서 엔트리를 버리면, `flush_pending`이 pop해 둔 **직전 보관분까지 함께 소멸**해 영구 유실이 된다(재현: 교체 실패로 1건 보관 → 손상 상태에서 다음 PM → pending 0 → 복구 후 flush 0건).
  - 회수는 **`append` 시점 + 컨슈머 poll 루프 상시 호출**(`SpcConsumer._flush_pm_log`). append만이면 다음 계기가 "다음 요란 PM"=**3~4개월 뒤**라 실전에서 재시도가 없는 것과 같다. writer 인스턴스를 `consumer.pm_log_writer`에 보관해 seam(`.append`) 밖에서 flush할 수 있게 한다. 빈 큐 가드로 상시 호출 비용은 O(1), 큐 길이 = 요란 PM 수라 무한 성장 없음.
- **재시도 예산을 호출 성격별로 분리** *(2차 리뷰 M — "읽기는 되는데 교체만 지속 실패"(편집기·AV·동기화 도구 잠금) 상태에서 드러남)*:
  - `append`(진짜 새 요란 PM — 분기 1회): **전력**(5회·총 ≈1.0s). 드문 이벤트라 대기를 감수하고 성공률을 산다.
  - 백그라운드 flush(poll 상시): **단발·무대기 + 쿨다운 30s**. append 예산을 그대로 쓰면 poll마다 1초를 블로킹해 `fdc.raw` 핫패스가 초당 ~1건으로 주저앉는다 — 재시도 큐가 컨슈머 지연으로 전이되는 구조. 실측 **1,020ms → 15.6ms**(쿨다운 중 0ms).
  - 실패 상태(`_last_error`) 해제는 **기입 성공 시점**. 읽기 성공에서 풀면 위 잠금 상태에서 매 flush 리셋 → 로그 억제가 무력화된다(실측 21회 시도 중 상세 21회 → **1회**).
  - `clock`·`sleep`은 주입점(컨슈머 `clock` 패턴). 전역 `time.sleep` patch는 pytest 내부와 충돌한다.
- **멱등 키 `(chamber_id, pm_count, date)`**: 동일 키 엔트리 존재 시 skip+로그 (크래시-재전달 창 이중 방어). *(구현 시 개정 — 시뮬 `pm_count`는 프로세스마다 0부터라 `(chamber, pm_count)`만으로는 2회차 실행의 진짜 요란 PM이 조용히 무기입. 재전달은 같은 개방 시각을 실어오므로 date 포함이 방어력을 안 깎는다)*
- **loud 아닌 verdict 거부**: "요란만 기입" 규약을 writer에서도 막는다(코어 가드 이중화).
- **`date_source` 표기**: `pm_detected_at` / `confirmed_at` / `written_at_fallback` — fallback 엔트리가 정상 엔트리로 위장해 `days_since_last_pm` 기준점을 조용히 틀지 않게 한다.
- 파일 부재 → `[엔트리]`로 생성. **파싱 불가(손상) → 기입 포기+에러 로그** (덮어쓰면 이력 소실 — 복구는 수동).
- NaN/Inf 없음(문자열·int만)이나 관례상 `json.dumps(..., ensure_ascii=False, allow_nan=False)`.

**엔트리 스키마** (기존 스냅샷 형식 + 추적 필드 — A 파서는 `date`·`type` 외 무시하므로 하위호환):

```json
{"date": "2026-07-28 05:12:33", "type": "major", "verdict": "loud",
 "chamber_id": "SIM_CH_4", "pm_count": 2, "date_source": "pm_detected_at",
 "written_by": "spc_consumer", "written_at": "2026-07-28 05:20:01"}
```

*(`written_at`도 naive UTC — 구 초안의 tz-aware ISO는 오기. 계약 §8-B-1이 정본)*

(`qual_id`·`incident_id` 동봉은 선택 — 코어에 이벤트 메타를 넘기려면 `on_qual_verdict(..., meta=None)` 선택 인자 추가. 1차는 미포함 권고 — 코어-이벤트 결합 최소화.)

### 3-4. 시간축 규칙 (D2·D3 구현)

`pm_detected_at`(ISO, tz 포함 가능) → **UTC 변환 후 tz 제거** → `"%Y-%m-%d %H:%M:%S"`. A `rows_to_trace`의 naive 변환(:275)과 동일 규칙. 미보유 시 fallback 순서: `confirmed_at` → 기입 시각(둘 다 동일 변환).

### 3-5. 경로 해석 — 단일화

- `config/params.yaml` **신규 키** `pm_log: {path: "pm_log.json"}`(리포 루트 상대 — 6-4 매직 값 금지). B writer는 이 키만 사용.
- A측(`LEAN85_PM_LOG` "임시"·`find_file` 탐색)의 동일 키 전환은 **A 소관 후속** — 본 PR에서 강제하지 않되 계약 문서에 "정본 = 루트" 명문화.
- **기동 가드**: `lean85/pm_log.json` 사본이 존재하면 경고 로그(find_file이 루트보다 먼저 집어 **B 기입·A 독해가 갈라지는** split-brain — .gitignore :132가 사본 출현을 예고).

### 3-6. 배선 (spc_consumer.py)

`_build_provisional` :714에서 `pm_logger=PmLogWriter(경로).append` 주입 + main 로그 1줄(기입 경로 표시).

## 4. 변경 파일·리뷰 라인 (PR 구성)

| 파일 | 변경 | 리뷰 |
|---|---|---|
| `src/agent_b_spc/provisional_mode.py` | `pm_logger` seam·`on_pm(ts)`·`pm_detected_at`·`_log_pm` | B 소유 (3-1) |
| `src/agent_b_spc/pm_log_writer.py` | **신규** | B |
| `src/agent_b_spc/spc_consumer.py` | ts 전달·writer 주입·기동 가드 | B |
| `config/params.yaml` | `pm_log.path` 키 | B (+`docs/config_파라미터_합의안` 근거 줄) |
| `docs/API_Contract_명세서.md` | §8-B에 "B 소비 효과: pm_log 기입(단일 작성자=B, 스키마·시간축 규칙)" 명문화 | **PM 승인** (docs/ 소유권) + **A 승인**(소비자 리뷰 — 계약 2-2) |
| `src/agent_b_spc/tests/*` | §5 테스트 | B |
| (A 후속, 별도 PR) | `lean85/README.md` 운영 규약 현행화·`LEAN85_PM_LOG` 정리·챔버 필터 검토 | A |

브랜치: `feat/spc-pm-log-writer` → dev PR (제목 `[팀원B] 요란 판정 확정 시 pm_log 런타임 기입`).

## 5. 테스트 계획

**순수 코어** (`test_provisional_mode.py` — 기존 mock 패턴):
loud 전이 시 pm_logger 1회 호출·페이로드 검증 / quiet·phase 가드·미지 챔버 시 0회 / 재전달(중복 verdict) 시 0회 / pm_logger 예외에도 Phase 1 진입·seam 순서 유지 / `on_pm(ts)` 저장·미전달(None) 하위호환.

**writer 단위** (`test_pm_log_writer.py` 신규):
부재→생성 / append 후 배열 보존·순서 / 멱등 키 중복 skip / 손상 파일(비-list·비-JSON)→기입 포기·원본 무손상 / tmp+replace 원자성(쓰기 후 tmp 잔존 없음).

**A 파서 호환** (핵심 — 회귀 방지):
기입 산출물을 `lean85_pipeline.parse_pm_log`로 라운드트립 — legacy(`"2018-12-24"`)+신규 혼재 정렬 성공(naive 일관), 여분 키 무시, `should_retrain`이 신규 date를 새 이벤트로 인식.

**E2E (수동)**: `qual_loud_ch4.yaml` 재생 → `agent_mock`으로 `QualVerdictConfirmed(loud)` 주입 → ① 루트 pm_log에 1건 추가 ② A 컨슈머 로그에서 다음 wafer부터 캐시 무효화·`low_confidence=1` 전환 ③ 중복 주입 시 무기입 확인. ※ torch 실측 라운드는 foreground 실행(TSR-0001).

## 6. 헌법·규약 준수 체크

- **1-1 비해당 확인**: 관리선·Recipe·물리 세계를 건드리지 않는 **파일 로그 기록** — 승인 게이트 불요. (Phase 전이 자체는 기존 승인 하류 `QualVerdictConfirmed` 소비로 변경 없음.)
- **1-2**: Agent 호출 아님 — fdc.agent 소비는 기존 계약 §0 그대로.
- **2-2 정신**: pm_log는 토픽은 아니나 **A 소비 인터페이스** → 여분 키 추가 사유 PR 명시 + A 리뷰 승인 + 계약 문서 동시 갱신(4-3).
- **6-1/6-2**: 경로 상수 params.yaml·logging 사용·기입 실패가 컨슈머를 죽이지 않음.
- **7장 반영**: `json.loads` 후 isinstance 가드 · `assert` 금지(명시 raise/로그) · 문서 동기화.
- 구현 후 7장에 추가 검토: "❌ pm_log에 tz-aware 시각 기입 → ✅ naive UTC 고정 (A 파서 정렬 TypeError)".

## 7. 리스크·열어둔 것

- **기입 주체 회신 충돌**: 7/27 발송 건 회신이 "A 기입"으로 오면 본 플랜 §3-3~3-6을 A측으로 이식(코어 seam·시각 확보는 그대로 유효 — B가 이벤트에 개방 시각을 실어주는 계약 확장 필요). **회신 전 머지 금지.**
- **크래시 창**: 광폭 DB 커밋 후·기입 전 크래시 시 엔트리 영구 유실(복구 경로 재기입 없음) — 빈도 낮음·영향은 A 피처 지연. **`pending` 큐도 인메모리라 프로세스 종료 시 함께 소멸**한다(디스크 저널 아님). 영구 보정(복구 시 provisional-활성 챔버의 pm_log 존재 대사)은 후속.
- **데모 압축시간**: 라이브 `days_since_last_pm`이 분 단위가 되는 왜곡은 **기존 §6-2 안건**(`LEAN85_TIME_SOURCE` 경고)과 동일 축 — 본 기입이 새로 만드는 문제 아님, A 안건에 병기.
- **전 챔버 공유**(D5): CH4 요란이 전 챔버 피처에 반영 — A 필터 후속 전까지 수용.
- **동시 프로세스 = lost update** *(리뷰 M5)*: `os.replace`는 독자 원자성만 보장하고 load→append→write에 락이 없다. `spc_consumer`를 2개 이상 띄우면 나중 쓰기가 앞 엔트리를 덮는다. 계약 §8-B-1에 "작성자 = B 1곳 **+ 프로세스 1개**"로 명문화 — 스케일아웃 시 기입 담당 인스턴스 고정 필요.
- **시간축 정합의 조건부성** *(리뷰 M6)*: A `_parse_ts`는 tz를 UTC 변환 **없이** 버리고 B는 변환 후 버린다 — 발행측이 UTC(+00:00)인 현재만 일치. 또 A가 `LEAN85_TIME_SOURCE=src_ts`(2018 축)면 B가 쓰는 wall-clock PM이 미래가 돼 `searchsorted`에서 탈락한다. 둘 다 **A 후속 정리 대상**(계약에 명시).
- **CI 게이트 밖** *(리뷰 M7)*: `.github/workflows/tests.yml`은 `tests/`·`agent_service/tests/`만 돌려 **B 스위트가 CI에 없다**. A 파서 호환 2건은 `importorskip`이라 CI 의존성(xgboost 없음)에서는 넣어도 skip된다 → 현재 이 회귀축은 **로컬 수동 검증**에 의존. B 스위트 CI 편입은 별건(의존성 정리 필요 — confluent_kafka·sqlalchemy 등).
- **E2E 기대치 정정** *(리뷰 M8)*: `is_high_regime`은 one-way 플래그이고 루트 pm_log에 이미 2018 엔트리가 있어 **현 wafer는 이미 1** — 기입으로 바뀌는 값은 `days_since_last_pm`·`high_regime_days`·`dslp_x_hour`다. §5 E2E ②의 "`low_confidence=1` 전환" 기대는 성립하지 않을 수 있으니 **위 3종 변화**로 확인할 것. 또 `should_retrain`은 날짜 단위 비교라 압축시간 데모에서 **같은 날 2번째 요란 PM은 재학습 트리거가 아니다**.
- **git 추적 파일** *(리뷰 L8)*: 런타임 append가 워킹트리를 더럽히고 브랜치 전환으로 운영 이력이 소실될 수 있다 — 리허설 전후 백업, 장기적으로 DB 이관 검토.

## 8. 순서 (실행 체크리스트)

1. ⬜ **D1 회신 확인(팀원 A님)** → D1~D5 확정 기록 — *잔여. 회신 전 머지 금지(§7)*
2. ✅ 계약 명문화 — `docs/API_Contract_명세서.md` **§8-B-1 신설**(작성자=B 1곳·스키마·naive UTC·멱등 키·A 소비 효과·D5 한계) + 헤더 v4.6-c + §0 A 구독 각주. **PM·A 승인 필요**
3. ✅ 구현 (§4) — `provisional_mode`(pm_logger seam·`on_pm(ts)`·`pm_detected_at`·`_log_pm`) / `pm_log_writer.py` 신규 / `spc_consumer` 배선·기동 가드 / `params.yaml` `pm_log.path` / 합의안 등재 / CLAUDE.md 7장 2줄
4. ✅ 테스트 (§5) — 신규 **41건** 통과(코어 10 / writer 26 / 컨슈머 배선 5). 회귀 없음: 변경 전 HEAD와 동일한 54건 env-fail(`confluent_kafka`·`sqlalchemy` 미설치 — 본 변경 무관). A 파서 호환 2건은 xgboost 미설치 환경 skip(`importorskip`) — **라운드트립 수동 실증 완료**: legacy `"2018-12-24"` + 신규 `"2026-07-28 05:12:33"` 혼재 정렬 성공(TypeError 없음). 2차 리뷰 H1 재현 시나리오(①교체실패 보관 → ②손상 조우 → ③복구 후 회수)는 **회귀 테스트로 고정**
5. ⬜ **E2E 수동 검증** — `qual_loud_ch4.yaml` 재생 → `agent_mock`으로 `QualVerdictConfirmed(loud)` 주입 → ① 루트 pm_log 1건 추가 ② A 컨슈머 다음 wafer부터 캐시 무효화 ③ 중복 주입 무기입. ※ foreground 실행(TSR-0001)
6. ⬜ 회의 브리핑 §8 결정 상태 갱신 (E2E 후)
