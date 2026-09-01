# TTTM 엔진 코어 (B4-2) Implementation Plan

> 설계 근거: `src/agent_b_spc/specs/B4-2_TTTM엔진_설계.md` (v5).
> **개정 v2 (2026-07-14, 플랜 리뷰)**: Φ⁻¹=stdlib NormalDist · EPS 정당화 · Task5 합성길이 가드.
> **개정 v3 (2026-07-14, 독립 리뷰 11건 반영)**: 🔴 **#1 `reference_suspect` = worst_gk의 fleet_key ∈ suspect_sensors**(센서ID 문자열 아님 — 타입 불일치로 항상 False였음) · 🔴 **#2 flush None-가드**(`last_pm_count is not None` — 첫 wafer 크래시) · 🔴 **#3 생성자에 `k_sigma`**(주입점 부재) · **#4 rollup 가드**(warm∧σ_ref>EPS∧fk∈fleet_median) · **#5 `last_dt`+과거드롭+ts파싱**(§4-1 "Nelson 규약" 실구현) · **#6 DoD=6챔버 합성·flag는 *안정* 챔버**(4챔버만이면 median 마스킹) · #7 "순서무관"→내부 발동→해제 테스트 · **#8 params.yaml+합의안 *같은 PR*(4-3)** · #9 flush 재개 assert · #10 fleet<MIN시 discard · #11 `ALERT_FIELDS` 정의.
> **For agentic workers:** task 단위 TDD. Step 체크박스. 코어는 DB·Kafka 불요(Point 주입) — 라이브는 B4-1.

**Goal:** 6가상 챔버 fdc.raw(Point) → **fleet median 대비 개별 outlier(score)** + **baseline 대비 집단 이동(reference_suspect)** 판정 엔진(`tttm_engine.py`). 출력 = `fdc.alert.tttm` + `tttm_comparisons` 공통 형태(발행·영속은 B4-1·③).

**Architecture:** `feed(point)` per-wafer — 가드/ts드롭/warmup/flush → window median(center_live) → fleet median(fk별) → 역방향(set/discard) + score. 고정(center_ref/σ_ref=레짐 수립 행) vs 라이브(center_live·fleet median) 분리. reference·whitelist·roster는 `control_limits_loader.load()`(B3-2) 재사용, thresholds·window·k_sigma는 생성자 주입(hermetic).

**Tech Stack:** Python, `statistics`(median·NormalDist)/numpy, pytest. Point/GroupState는 `nelson_engine` 패턴.

---

## Global Constraints (헌법 + 불변식)

- **헌법 1-2**: 판정만 — alert 발행 없음(B4-1). `reference_suspect`도 플래그만.
- **헌법 1-3**: fdc.raw 센서값만. C65 미사용.
- **헌법 6-1**: `tttm_window_wafers`·`tttm_window_min_fill`·`tttm_warning`·`tttm_critical`·`reverse_rule_min_chambers`·`k_sigma`(=`control_limit_k_sigma`)는 **config/주입**. `EPS(1e-9)`·`MIN_FLEET_CHAMBERS(2)`·`ALERT_FIELDS`는 **모듈 상수**. *(분해능은 skip 채택 → 미사용.)*
  - **`EPS=1e-9` 정당화(리뷰②)**: 감시 45 센서 center·σ 전부 O(0.1)+ 실측 → (1e-15,1e-9)에 유효값 0 = 수치적 0 가드. gap_pct는 표시용이라 오탐 무영향.
- **헌법 2-2**: `reference_id`는 **DB 전용**(`ALERT_FIELDS`에서 제외) — alert 유입 금지(B4-1).
- **헌법 4-3**: `params.yaml`(config) 변경과 `합의안`(근거 doc) sync는 **같은 PR**(#8). 분리 금지.
- **헌법 6-2**: consumer graceful shutdown = B4-1. **6-4**: C코드·snake_case. **절대규칙 5**: sim_events 조회 금지.
- **불변식**:
  - **ref vs live**: center_ref/σ_ref=레짐 수립 행(고정), center_live·fleet median=라이브. 코어 v1=active.
  - **σ_ref skip 양쪽(#2/#4)**: `σ_ref≤EPS` gk는 score·역방향·rollup 전부 skip.
  - **suspect set/discard(#1/#10)**: fk별 현재 상태. 조건 거짓·fleet<MIN이면 `discard`.
  - **reference_suspect(#1)**: `worst_gk[1:] ∈ suspect_sensors`(**fleet_key 튜플 비교** — 센서ID 문자열 아님). `top_gap_sensor=worst_gk[-1]`은 표시용.
  - **억제=플래그만(#3배경)**: score/top은 전 센서 max(suspect 포함). 6챔버 억제는 B4-1.
  - **Point 6필드/시간(#5)**: pm_count·limit_version + ts 파싱·엄격 과거 드롭·`last_dt`(Nelson 규약).

---

## File Structure

| 파일 | 변경 | 소유·PR |
|---|---|---|
| `src/agent_b_spc/tttm_engine.py` | **생성** | B |
| `src/agent_b_spc/tests/test_tttm_engine.py` | **생성** | B |
| `src/agent_b_spc/config_util.py`(또는 엔진 로더) | TTTM 2키 로드 | B |
| `config/params.yaml` | 2키 신규 | B(spc절) — **합의안과 같은 PR(#8)** |
| `docs/config_파라미터_합의안_v1.md` | B절 2키 sync | **같은 PR**, docs/라 PM 리뷰(값=B권한) |
| `src/agent_b_spc/README.md` | 상태 갱신(4-3) | B |
| `src/simulator/scenarios/multi_chamber_common_c17.yaml` | 읽기전용 | 외부 의존(PM — #13) |

---

# Task 1: 엔진 골격 — Point/GroupState + feed() 가드·ts드롭·warmup·flush

**Files:** Create `tttm_engine.py`, `tests/test_tttm_engine.py`

**Interfaces:**
- 모듈 상수: `EPS=1e-9`, `MIN_FLEET_CHAMBERS=2`, `ALERT_FIELDS=frozenset({"reference","score","top_gap_sensor","gap_pct","reference_suspect"})`
- **`TTTMEngine(limits, whitelist, *, window=None, min_fill=None, warning=None, critical=None, reverse_min=None, k_sigma=None, excluded=frozenset())`** — 값 None이면 config 로드, 지정 시 주입(hermetic). **`k_sigma` 필수 추가(#3)** — 분위수 σ_ref용, Nelson `time_gap_hours` 패턴.
- `GroupState{window: deque(maxlen), center_live, last_pm_count, last_limit_version, last_dt}` (#5 `last_dt` 추가)
- `Point` = nelson_engine 정의 그대로(6필드)
- `set_excluded(chamber_ids: set)`
- `sigma_ref(gk)`: 'sigma'→`limits[gk].sigma` / 'quantile'→`(ucl−lcl)/(2·self.k_sigma)`

**feed() 순서 (스펙 §5 — 가드 수정 반영):**
```
gk=point.group_key; ch=gk[0]
if gk not in whitelist: log1회; return []
if gk not in limits:    warn; return []
if ch in excluded:      return []
if value None/NaN:      log; return []
σref=sigma_ref(gk); if σref<=EPS: log; return []
try: dt=datetime.fromisoformat(point.timestamp)
except ValueError: log; return []                                 # #5 파싱 실패 skip
st=state.setdefault(gk, GroupState())
if st.last_dt is not None and dt < st.last_dt: log; return []     # #5 엄격 과거 드롭
if st.last_pm_count is not None and (point.pm_count>st.last_pm_count
                                     or point.limit_version!=st.last_limit_version):
    st.window.clear()                                             # #2 None-가드 + 3-C flush
st.last_pm_count=point.pm_count; st.last_limit_version=point.limit_version; st.last_dt=dt
st.window.append(value)
if len(st.window)<min_fill: return []                             # 3-B warmup
st.center_live=median(st.window)                                  # 3-A window median
... (Task 2·3)
```
> ⚠️ **#9 flush 후 reference stale**: window는 비우나 center_ref/σ_ref는 v1 고정(리로드는 B4-3) → v1 코어에서 flush는 window 리셋만. post-PM 챔버 spurious 가능(DoD엔 PM 없어 무관).

- [x] **Step 1: 실패 테스트**: whitelist 밖·limits 없음·excluded·NaN skip / `σ_ref≤EPS`(quantile ucl==lcl) skip / **첫 wafer 크래시 없음(#2 — last_pm_count None)** / **ts 역전 드롭·파싱실패 skip(#5)** / warmup / window median(crazy 흡수) / **flush: pm_count↑ → window clear + 재충전 후 판정 재개(#9)**
- [x] **Step 2: 구현** — 위 순서. `k_sigma`는 주입 or `load_k_sigma()`.
- [x] **Step 3: 검증** — `pytest -v -k "guard or warmup or flush or median or drop"` 그린. *(전체 파일 15 passed — 상위집합 검증)*

---

# Task 2: fleet median + score (개별 outlier)

**Files:** Modify `tttm_engine.py`, `tests`

**Interfaces:**
- `fleet_key(gk)=gk[1:]`. roster=`{g[0] for g in whitelist}`. `warm(st) ≡ len(st.window)≥min_fill`.
- `feed` 후반:
  ```
  fk=fleet_key(gk)
  members={g[0]: state[g].center_live for g in state
           if g[1:]==fk and warm(state[g]) and g[0] not in excluded and sigma_ref(g)>EPS}
  if len(members)<MIN_FLEET_CHAMBERS: suspect_sensors.discard(fk); return []   # #10 fleet<MIN → discard
  fleet_median[fk]=median(members.values())
  gap_σ=(center_live−fleet_median[fk])/σref
  gap_pct=(center_live−fleet_median[fk])/abs(fleet_median[fk])*100 if abs(fleet_median[fk])>EPS else None  # #6
  ```
- **`chamber_rollup(chamber) -> dict|None` (#4 가드)**:
  ```
  cand=[gk for gk in state if gk[0]==chamber and warm(state[gk])
        and sigma_ref(gk)>EPS and fleet_key(gk) in fleet_median]      # #4 존재·degenerate 가드
  if not cand: return None
  g={gk:(state[gk].center_live−fleet_median[gk[1:]])/sigma_ref(gk) for gk in cand}
  worst=argmax_gk |g[gk]|
  → score=|g[worst]| · top_gap_sensor=worst[-1] · gap_pct(worst)
    reference_suspect = worst[1:] in suspect_sensors                 # #1 fleet_key 비교!
    reference="fleet_median" · reference_id=limits[worst].limit_version(DB전용) · chamber_id=chamber
  ```

- [x] **Step 1: 실패 테스트**: score 산식 / **gap_pct 부호(#6 음수median→양수)** / 표본부족 → None / 분위수 σ_ref `(ucl−lcl)/2k` / **rollup 가드(#4): fleet_median 없는 fk·σ_ref=0 챔버 rollup → None(크래시 없음)** / **rollup 필드 = `ALERT_FIELDS ∪ {reference_id,chamber_id}` 정확(#11 — reference_id∉ALERT_FIELDS assert)** / gap_pct 가드
- [x] **Step 2: 구현** — members·rollup 가드·`abs()` 부호.
- [x] **Step 3: 검증** — `pytest -v -k "score or gap_pct or rollup or quantile"` 그린. *(전체 23 passed)*

---

# Task 3: 역방향 룰 (집단 이동)

**Files:** Modify `tttm_engine.py`, `tests`

**Interfaces (스펙 §3-3):**
```
def reverse_rule(fk, members):        # members = Task2와 동일 집합
    up  = #{ch: (members[ch]−center_ref(ch,fk))/σ_ref(ch,fk) >  warning}
    down= #{ch: (members[ch]−center_ref(ch,fk))/σ_ref(ch,fk) < -warning}   # center_ref=limits[(ch,)+fk].center
    if max(up,down) >= reverse_min:  suspect_sensors.add(fk)
    else:                            suspect_sensors.discard(fk)      # #1 래칭 금지
```
- 앵커=각 챔버 center_ref(고정), 자=σ_ref. σ_ref≤EPS·비warm·excluded 챔버는 members에서 이미 제외.
- 분모=active members. min-active floor는 B6-3(코어 threshold=3 무해, 챔버 6→4 멘토 2026-07-19).
- **챔버 reference_suspect = `worst_gk[1:] ∈ suspect_sensors`**(rollup, #1). score는 전 센서 max 유지(#3배경).

- [x] **Step 1: 실패 테스트**:
  - **발동**: 3/4 챔버 C17 동일방향 >warning → fk suspect=true *(챔버 6→4, reverse_min 4→3, 2026-07-19)*
  - **해제(#1 래칭)**: 발동 후 해소 point → suspect=false (동일 인스턴스 순차)
  - **reference_suspect 실발동(#1)**: suspect fk일 때 그 fk가 top인 챔버 → `reference_suspect==True`(fleet_key 비교가 실제로 매칭됨을 검증 — 문자열이면 항상 False라 여기서 잡힘)
  - **방향 분리**: 2up+2down → false
  - **σ_ref=0 역방향(#2)**: 한 챔버 σ_ref=0 → 크래시 없이 제외
  - **fleet<MIN discard(#10)**: 발동 후 fleet 챔버가 2 미만으로 떨어지면 suspect=false
- [x] **Step 2: 구현** — set/discard·worst_gk fleet_key 비교.
- [x] **Step 3: 검증** — `pytest -v -k "reverse or suspect or direction"` 그린. **#1 가드 = 각 테스트 fresh 엔진 인스턴스 + 위 "발동→해제" 명시 테스트**(순서-무관은 pytest-randomly 도입 시에만 의미, DoD 참조). *(전체 29 passed)*

---

# Task 4: config 통합 (2키 등재·로딩·Φ⁻¹ assert)

**Files:** Modify `config_util.py`, `config/params.yaml`, `README.md`, `docs/합의안` (같은 PR)

**Interfaces:**
- `params.yaml` `spc:`절: `tttm_window_wafers: 20` / `tttm_window_min_fill: 10`
- 생성자 None 인자 → config 로드(`window/min_fill/warning/critical/reverse_min/k_sigma`), 지정 시 주입.
- **#12 Φ⁻¹**: `abs(statistics.NormalDist().inv_cdf(q_high) − control_limit_k_sigma) < tol` (stdlib — scipy 불요, `statistics`는 median으로 이미 임포트).

- [x] **Step 1: 실패 테스트** — 2키 로드 / 주입값 우선 / Φ⁻¹≈k assert(q_high 변조 시 raise)
- [x] **Step 2: 구현 + 등재** — `params.yaml` 2키 + config 로더. **⚠️ #8**: 사용자 결정(2026-07-15) — 합의안은 **임시 로컬(미커밋)**, PM 공식 등재로 대체 예정. params.yaml(spc절=B권한)만 working-tree 등재. *(feat 브랜치 단계라 4-3은 PR 시점에 PM 합의안과 재조정)*
- [x] **Step 3: 검증** — `pytest -v -k "config"` 그린 (전체 34 passed) + README 갱신.

---

# Task 5: DoD 시나리오 통합테스트 (multi_chamber)

`multi_chamber_common_c17.yaml`(target 4챔버 C17 +3σ, start_after_wafers=150). **6챔버 전부 합성** 필수.

**Files:** Modify `tests`

**Interfaces:** YAML 로드 → **6챔버 전부 Point 스트림 합성**(답안지 미조회):
- 4 target(SIM_CH_1·2·4·6): 150 이후 C17 = `center_ref + 3σ`
- **2 stable(SIM_CH_3·5): 전 구간 C17 ≈ `center_ref`**  ← **필수(#6)**
- ⚠️ **길이(리뷰③)**: `150 + window + margin ≥ 180` 합성(이하면 미충전 미발동). YAML엔 총 길이 없어 테스트가 제어.

- [x] **Step 1: 통합테스트**:
  1. 초반(warmup 중) 판정 없음
  2. post-shift window 완충 뒤 **C17 fk suspect=true**(#13 full-fill 후만)
  3. **reference_suspect=true는 *안정* 2챔버(CH_3·5)에 뜸(#6)** — target 4챔버는 오염 median과 gap≈0이라 안 뜸(스펙 §3-3 의도). 에스컬 재료(소비 B4-1)
  4. **대조(#10 스펙)**: 단일 챔버만 지속 드리프트(나머지 **5 stable**) → suspect=false + 그 챔버 score>critical. 단일 스파이크는 window median 흡수(별도)
- [x] **Step 2: 검증** — 전체 그린(36 passed). **#13/#6 PM 확인(실 replay 경로만)**: post-150 ≥window + **target 외 ≥2 stable 챔버 존재** + +3σ 진폭. *(YAML 실측: target=CH1·2·4·6 → stable=CH3·5, start=150, mag=3.0 → 조건 충족)*

---

# 완료 판정 (DoD)

- [x] 단위 그린: T1(가드·ts드롭·flush재개·첫wafer·warmup) / T2(score·gap_pct·rollup가드·필드셋·분위수) / T3(발동·**해제·reference_suspect실발동**·방향·σ_ref=0·fleet<MIN) / T4(config·Φ⁻¹)
- [x] **DoD 시나리오**: 6챔버 합성 → **안정 2챔버 reference_suspect=true** + 단일 지속드리프트 대조 + 스파이크 흡수(T1)
- [x] **크래시 0**: 첫 wafer·σ_ref=0·rollup·미충전·표본부족·median≈0 전부 예외 없음
- [x] **#1 가드**: fresh 엔진/테스트 + 발동→해제 테스트 그린(top_gap_sensor 문자열 비교였으면 실발동 테스트가 잡음)
- [x] params.yaml 2키 + 합의안 ~~같은 PR~~ **임시 로컬(미커밋, 사용자 결정 2026-07-15)** + Φ⁻¹ assert + config 로드
- [x] 기존 nelson/seed 회귀 무영향(108 passed) · README 갱신(4-3)

---

# 외부 의존 (추적)

| 항목 | 대상 | 방향 |
|---|---|---|
| `합의안` B절 2키 | **PM** | params.yaml과 **같은 PR**, docs/ 부분 PR 리뷰(#8) |
| `multi_chamber_common_c17.yaml` | **PM**(시뮬) | post-150 ≥window·**target 외 ≥2 stable**·+3σ (실 replay 경로, #6·#13) |
| P4-5 `LIMIT_DELTA_MAX_PCT` | **PM+C** | %유지·L73 주석 정정 (별도 안건) |
| Point pm_count·is_qual 운반 | **A**(B4-1) | 6필드 유지·is_qual 상류 필터 |

# 이월 액션 (이 플랜 밖)

- **B4-3**: loader trigger_type 리팩터 선등록(reference is_active 비의존 쿼리+리로드, #①·#9) · 분위수 재설정 상한 robust σ(인수 노트 ④).
- **B4-1**: alert 발행·Context Score(σ-score·억제 라우팅·디바운스 #②·#⑦)·Point 구성·is_qual 필터·staleness TTL.
- **B6-3**: provisional 제외(set_excluded)·active 과반+min-active floor.
- **③ writer**: tttm_comparisons 적재·테스트 롤백.
- **#8 분위수 비대칭**: 코어는 대칭 근사 σ_ref — W7 관측 후 방향별 확장.
