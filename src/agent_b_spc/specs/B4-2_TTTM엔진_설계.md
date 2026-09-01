# TTTM 엔진 코어 (B4-2) — 설계 스펙

- **작성**: 팀원 B (팀원 B), 2026-07-14
- **task**: B4-2 (TTTM 엔진 코어) — 선행 B2-1(baseline)·B3-2(control_limits 시딩·로더) 완료, B4-4 ⓐ(σ-단위 상한) 결정 완료
- **DoD**: `scenarios/multi_chamber_common_c17.yaml`(≥4챔버 C17 공통 드리프트) → **reference_suspect=true + 에스컬 경로** 검증 + 단위테스트 그린
- **상태**: 설계 (3 결정 + 6 pin + 2·3 심화 + 자체리뷰 + 독립 크로스체크 확정 → 구현 계획)
- **개정 v2 (2026-07-14)**: §3-5/§4-2 정밀화 — control_limits(현행값) vs limit_corrections(이력) 역할, reference는 `trigger_type` 기준(is_active 비의존), qual_snapshots 상태.
- **개정 v3 (2026-07-14, 1차 리뷰)**: σ_ref=0 가드(score)·역방향 min-active floor·reference_suspect 디바운스 요건·loader 조용한 오참조.
- **개정 v4 (2026-07-14, 독립 크로스체크 반영)**: 🔴 **#1 suspect 래칭 해소**(add-only→set/discard, §3-3·§5) · 🔴 **#2 σ_ref=0 가드 역방향까지 확장**(§3-3·§8 — v3는 score만 커버해 여전히 크래시) · **#5 Point 6필드 정정 + GroupState `last_pm_count`**(§4-1·§5 — PM flush 구현 가능화) · **#6 gap_pct 부호**(음수센서 대응 `/|fleet_median|`, §3-2) · **#3 억제=플래그만·core는 전센서 max**(§3-3) · **#4 §5 의사코드 런타임 가드**(setdefault·roster·존재검사) · **#7 reference_id=DB 전용**(§4-3, alert 실으면 2-2) · **#8 분위수 σ_ref 비대칭 주의**(§3-4) · **#9 flush 후 reference stale**(§3-C·§9) · **#10 spike→지속드리프트 테스트**(§7) · 6-1 ε/분해능 상수화·config 권한 정정(§6).
- **개정 v5 (2026-07-14, 독립 플랜 리뷰 11건)**: 🔴 **#1 `reference_suspect`=fleet_key 튜플 비교**(§3-3·자료구조 — 문자열 비교라 항상 False였음) · 🔴 **#2 flush `None`-가드**(§5 — 첫 wafer 크래시) · 🔴 **#3 생성자 `k_sigma`**(§2) · **#4 rollup 가드**(자료구조) · **#5 `last_dt`/과거드롭/ts파싱**(§5) · **#10 fleet<MIN `discard`**(§5).

- **스코프 원칙**: **코어만**. 가한계 유예·median 제외(B6-3), alert 발행·Context Score 소비(B4-1), tttm_comparisons DB writer(③ 별건), Qual 스냅샷 절대비교(B2 보조축)는 훅/의미만 두고 이월.
- **결정 이력 (2026-07-14 브레인스토밍)**:
  - **결정 1 = (a) 그룹 σ + ref(레짐 스냅샷 고정)**. score 분모 = control_limits **레짐 수립 행**의 σ (정기 recalc·provisional 추종 안 함). 근거: fleet 산포(6점)는 불안정 + 집단 드리프트 시 자기 마스킹 / 라이브 σ 추종은 σ 인플레로 둔감(R6). 레짐 스냅샷 고정이 감도 일정 + B5-2 σ-갭과 철학 일관.
  - **결정 2 = (a) 챔버 자기 baseline center 앵커** — 역방향(집단 이동)은 라이브 median 아니라 **고정 baseline center** 기준. median 기준이면 집단 드리프트 시 median이 같이 끌려가 감지 불가. center_ref/σ_ref는 **같은 레짐 수립 행에서 쌍으로**.
  - **결정 3 = (a) 챔버별 rolling window + 매 wafer median 재계산**. 단일 wafer(~σ 노이즈) 헛발동 방지, 드리프트엔 반응. 신규 config `tttm_window_wafers`(제안 20~30).

---

## 1. 목적 & 범위

동일 공정 챔버 6대(SIM_CH_1~6)의 fdc.raw를 받아 **각 챔버가 fleet(형제 챔버 전체)와 얼마나 어긋났는지**를 실시간 판정한다. Nelson("자기 관리선 초과")과 **병렬 감지 채널**이며 입력은 **fdc.raw뿐**(C65·alert 불요)이라 D0 독립.

두 질문을 **다른 기준**으로 분리 판정한다 (의도된 설계):

- **개별 outlier (score)**: 이 챔버가 지금 형제들과 다른가 → **라이브 fleet median** 대비
- **집단 이동 (reference_suspect)**: 다수가 자기 정상에서 같이 움직였나(=기준 오염) → **고정 baseline center** 대비

### 범위 경계 (In / Out)

| In (이 설계) | Out (별도 task) |
|---|---|
| fleet median 산출(챔버별 rolling window) | 가한계 provisional 유예·median 제외 **구현** (B6-3 — 훅만) |
| TTTM score(σ-등가) + top_gap_sensor·gap_pct | alert 발행·Context Score 소비·**suspect 억제 라우팅** (B4-1) |
| 역방향 룰 → reference_suspect **플래그**(센서별) | tttm_comparisons DB writer (③ 별건, alert_id 없음) |
| 가한계 제외 훅 **인터페이스** | Qual 스냅샷 **절대 비교**(B2 보조축) |
| warmup·레짐 flush·엣지 가드 | reference 갱신 자동 배선(B4-3 — 코어는 v1만) |

> B4-3(동적 재산정)·B4-1(라이브 배선) **전**의 판정 코어. 산출물의 DB 영속·alert 실림은 각각 ③·B4-1이 붙인다.

---

## 2. 컴포넌트 & 모듈 구성

```
src/agent_b_spc/
├── tttm_engine.py            ← TTTM 엔진: feed(point) + fleet median + score + 역방향 룰
├── (재사용) control_limits_loader.py  ← reference(center_ref/σ_ref) + whitelist 소스
├── (재사용) config_util.py            ← config 로드 (B1/B2/B3 + tttm_window_*)
├── (입력) scenarios/multi_chamber_common_c17.yaml  ← DoD 검증 시나리오 (PM, read-only)
└── tests/
    └── test_tttm_engine.py            ← 단위 + multi_chamber 왕복
```

| 모듈 | 역할 | 의존 |
|---|---|---|
| `tttm_engine.TTTMEngine` | `(limits, whitelist, *, window, min_fill, thresholds, k_sigma, excluded=∅)` — 값 None이면 config 로드(**#3 `k_sigma` 필수: 분위수 σ_ref 주입점**, Nelson `time_gap_hours` 패턴) → `feed(point)`로 판정 | control_limits_loader 산출물 |
| `feed(point: Point)` | 챔버 window 갱신 → warm이면 fleet median 재계산 → score·역방향 판정 반환 | Nelson `Point` 재사용 |
| `chamber_rollup(chamber_id)` | 챔버 sensor-group별 `gap_σ`(fleet_median 캐시에서 **재계산**) → **최악 센서** 1건 롤업 | fleet_median 캐시 |

> **Nelson과 입력 공유**: `Point`(nelson_engine 정의, §4-1)를 그대로 소비. B4-1 라이브 컨슈머가 fdc.raw → Point 구성 시 Nelson·TTTM 동일 스트림 fan-out. **챔버 로스터**(fleet median·역방향에 필요한 6챔버 목록) = `whitelist`의 distinct `chamber_id`(= `gk[0]`)에서 도출 — 별도 주입 불요.

---

## 3. 확정 설계 결정

### 3-1. 4개 양(量) — 고정 2 / 라이브 2

| 양 | 출처 | 성격 |
|---|---|---|
| `center_ref` | control_limits 레짐 수립 행의 `center` | **고정** (레짐 안 불변) |
| `σ_ref` | control_limits 레짐 수립 행의 `sigma` (분위수는 §3-4) | **고정** |
| 챔버 현재중심 | 챔버별 rolling window `median` | 라이브 |
| fleet median | (recipe×step×window×sensor) 내 챔버 현재중심들의 median | 라이브 |

### 3-2. score (개별 outlier — 결정 1)

```
gap_σ(chamber, gk)  = (챔버_현재중심 − fleet_median[fleet_key]) / σ_ref[gk]
score(chamber)      = max_gk |gap_σ|          # 그 챔버 전(全) 센서 중 최악 (suspect 센서 포함)
top_gap_sensor      = argmax_gk |gap_σ| 의 sensor_id
gap_pct             = (챔버_현재중심 − fleet_median) / |fleet_median| × 100   # top 센서, signed, 표시용
```

- `fleet_key = (recipe_id, step, sensor_window, sensor_id)` — 챔버 제외한 비교 단위.
- score는 **σ-등가**라 config B1(warning 2.0 / critical 3.0)과 직접 비교.
- **#6 gap_pct 부호**: 음수값 센서(C11 DC_Bias≈−335·C61 Vneg 등)에서 `fleet_median<0`이면 signed median으로 나누면 **부호가 뒤집힘** → `|fleet_median|`로 나눠 부호가 항상 (center−median) 방향을 따르게 함. (표시용, Context Score 미소비 — pin ④.) API 계약 예시(C11 gap_pct=+4.1)와 정합.
- `|fleet_median| ≤ EPS`이면 gap_pct=NULL(0 나눗셈 가드), **score는 무영향**.
- ⚠️ **σ_ref 0 가드 (분모, #2·#4)**: `σ_ref ≤ EPS`(분위수 밴드 붕괴 ucl=lcl·데이터 글리치·zero_sigma)면 **그 gk를 score·역방향 양쪽에서 skip + 로그**(§3-3·§8). floor(`max(σ_ref, 분해능)`)는 σ_ref≈0에서 작은 갭도 huge gap_σ로 튀어 spurious critical(오탐) 유발 → **skip 채택**(zero_sigma→EXCLUDE 철학 정합; 감시 45는 baseline σ>0 전제라 σ_ref=0은 감시 부적격 신호). `EPS`·`분해능`은 §6 명명 상수(6-1).

### 3-3. 역방향 룰 (집단 이동 — 결정 2 + 심화 2-A/2-B/2-C)

```
# 매 wafer, 해당 fleet_key만 재평가 (add-only 아님 — set/discard로 켜짐·꺼짐 둘 다)
def reverse_rule(fk, members):        # members = warm·비제외·σ_ref>EPS 챔버
    up   = #{ch in members : (center_live[ch] − center_ref[ch,fk]) / σ_ref[ch,fk] >  warning}
    down = #{ch in members : (center_live[ch] − center_ref[ch,fk]) / σ_ref[ch,fk] < -warning}
    if max(up, down) >= reverse_rule_min_chambers(active 기준):
        suspect_sensors.add(fk)
    else:
        suspect_sensors.discard(fk)       # 🔴 #1: 조건 해제 시 반드시 내려감 (래칭 방지)
    return fk in suspect_sensors
```

- **앵커 = 각 챔버 자기 `center_ref`**(라이브 median 아님 — 결정 2). 자(尺) = `σ_ref`. **σ_ref≤EPS 챔버는 up/down·active 분모에서 제외**(#2, score와 대칭).
- **이탈 임계 = warning(B1) 재사용**(2-C). ※ score는 fleet median 대비, 역방향은 baseline 대비 — 같은 2.0이어도 **재는 대상이 다름**(의도).
- **분모 = active(비제외·σ_ref>EPS) 챔버**(2-B). ⚠️ **챔버 6→4 축소(멘토 2026-07-19)**: `reverse_rule_min_chambers 4→3`(4/6 과반이 4챔버선 만장일치라 3/4 과반 재정의). 코어는 전원 4 → ≥3/4. B6-3가 provisional 제외로 active를 줄이면 "3 고정 vs active 과반" + **min-active floor**(예: active<3이면 역방향 skip — 소수 챔버 '과반' 헛발동 방지; 리뷰 ①)를 **함께** 확정. **코어는 threshold=3 고정이라 무해**(active 2에선 이미 3 미달로 자연 미발동).
- **🔴 #1 래칭 금지**: `suspect_sensors`는 fk별 **현재 상태**(매 wafer set/discard). add-only면 드리프트 해소 후에도 영구 suspect로 남아 B4-1이 영구 에스컬 → 2-D 디바운스·"on/off" 전제가 무의미해짐. 반드시 조건 거짓 시 `discard`.
- **#3 억제는 플래그만(코어)**: 코어의 `score`/`top_gap_sensor`는 **전(全) 센서 max로 그대로 산출**(suspect 센서 포함 — 안 그러면 suspect 센서가 top이 될 수 없어 `reference_suspect`가 영영 false, 도달 불가). "오염 센서를 6챔버 전부 억제"는 **B4-1의 라우팅 행위**(§9 ③)지 코어의 score 변형이 아님. 챔버 레벨 `reference_suspect`=true = **`worst_gk[1:](=fleet_key) ∈ suspect_sensors`**(🔴 #1 — **튜플 비교**. `top_gap_sensor`는 센서ID 문자열이라 `"C17" ∈ {(...,"C17")}`은 **항상 False**; rollup이 든 worst_gk의 `gk[1:]`로 판정, `top_gap_sensor=worst_gk[-1]`은 표시용). (구조상 드리프트 4챔버는 오염 median과 gap≈0이라 그 센서가 top이 아니고, **끌려간 무고한 2챔버**에서 top=그 센서 → suspect=true로 뜸 — 의도된 동작, 시나리오 target_chambers가 아니라 **나머지 2챔버가 플래그됨**.)
- **플래핑(2-D)**: 경계 진동 시 set/discard 반복 → 코어는 순간 판정, 잦은 전이 억제는 B4-1 디바운스(§9 ⑦). W7 Scorecard 떨림 관측 시 K-wafer 지속 조건 추가.

### 3-4. 분위수 그룹 σ_ref (pin ②)

`method='quantile'`(KEEP\* 6그룹) 행은 `sigma` 컬럼이 **참고 통계(판정 미사용)**이므로 σ로 못 씀. →

```
σ_ref = (ucl − lcl) / (2 · k_sigma)      # k_sigma=config 3.0 (행은 NULL)
```

- ±3σ 등가 분위수 밴드에서 robust σ 역산 — B4-4 ⓐ 트릭과 동일. loader의 `ucl/lcl`만으로 산출.
- ⚠️ **#8 비대칭 주의**: KEEP\*는 애초에 **치우친(비정규)** 분포라 분위수 관리선을 쓰는 것. 밴드를 단일 대칭 σ_ref=span/6으로 접으면 skew 방향 감도가 뒤틀림(긴 꼬리 쪽 과소·반대쪽 과대 flag). **코어는 대칭 근사 채택(명시적 한계)** — 데모 스코프. 필요 시 방향별 `σ_up=(ucl−center)/k` / `σ_down=(center−lcl)/k`로 확장은 후속. (C17이 KEEP\*면 DoD 감도에 영향 — W7 관측.)
- ⚠️ **#12 k↔q 결합**: (ucl−lcl)/(2k)는 밴드가 ±k·σ일 때만 σ 복원. `control_limit_k_sigma`(3.0)와 `q_low/q_high`(0.00135/0.99865)는 별개 config라, 누가 분위수만 ±2.5σ로 바꾸면 σ_ref가 **조용히 틀림**. → 구현 시 `abs(statistics.NormalDist().inv_cdf(q_high) − control_limit_k_sigma) < tol` **assert**(**stdlib** `statistics.NormalDist` — scipy 미도입·median으로 이미 임포트, 근사식 불요). 로더는 q_low/q_high 미노출이라 config에서 직접 검증.

### 3-5. reference 소스·갱신 규칙 (pin ① — 결정 1 배선)

**테이블 역할 (init.sql·DB설계근거 v1)**: `control_limits` = **현행 관리선 저장소**(엔진 조회 값 소스) / `limit_corrections` = **변경 이력 원장**(엔진 미조회). 실력치 변경 시 **양쪽에 다** 씀. **TTTM은 값을 control_limits에서 읽는다.**

- σ_ref/center_ref = control_limits **승인·수립 성격 행**(`trigger_type ∈ {initial, qual, incident}`) 중 그룹별 **최신**. `periodic`(자동)·`provisional` **추종 안 함**.
- **승인 변동은 갱신 / 자동은 불변**: qual(Phase-2 리베이스)·incident(승인 재설정)는 reference **갱신**. periodic 무시.
- ⚠️ **is_active는 selector가 아니다**: 재산정마다 새 버전 + 기존 `is_active=false`(옛 행 보존). 승인 행 뒤 periodic 한 번이면 그 승인 행이 `is_active=FALSE`로 밀림 → **`is_active` 필터 금지, `trigger_type∈{initial,qual,incident}` 최신 선택**. (Nelson은 반대로 is_active=라이브 — 소스 분기점.)
- **코어 현실**: B4-3 전이라 승인 행 = v1(initial)뿐·그게 is_active → **loader.load() 그대로 = reference**, 동작 차이 0.
- ⚠️ **배선 이월(B4-3)**: loader는 is_active·6필드만 조회(trigger_type 없음). periodic 발행 시 → **loader에 trigger_type 노출 + is_active 비의존 쿼리** 필요. **위험 = 조용한 오참조**(#③: is_active인 periodic을 멀쩡히 반환 → 틀린 기준 판정, 크래시 아님). B4-2·B4-3 동일 소유(B)라 plan에 **loader 리팩터 action item 선등록**.
- (참고) **qual_snapshots**(설계근거·계약 지정, Writer=B)는 TTTM **절대 비교**(B2 보조축)용, fleet-median 코어와 별개 축. **init.sql 미생성**(문서 17 vs DDL 14, Qual/채점 3종 대기). 코어 밖.

### 3-6. 감시 스코프 (pin ⑤)

TTTM = Nelson과 **동일 whitelist**(감시 45 = KEEP 39 + KEEP\* 6)·per-window. loader `(limits, whitelist)` 재사용, EXCLUDE류 skip(그룹당 1회 로그).

### 3-7. 가한계 제외 훅 (pin ⑥ — B6-3 seam)

```
engine.set_excluded(chamber_ids: set)   # B6-3가 provisional 챔버 주입
```

제외 챔버는 fleet median·역방향 카운트(active 분모)·score에서 전부 빠짐. 코어는 인터페이스 + 빈 set 기본. 구현(trigger_type='provisional' 감지·Phase 연동·min-active floor)은 B6-3.

---

## 4. 참조·입출력 계약

### 4-1. 입력 Point (Nelson 공유 — 6필드, #5)

```
Point{ group_key=(chamber_id:str, recipe_id:str, step:int, sensor_window:str, sensor_id:str),
       wafer_id:str, timestamp:str(ISO8601 UTC), value:float,
       pm_count:int, limit_version:str }        # ← Nelson 정의 그대로 (레짐 flush에 필요)
```

- `fleet_key = group_key[1:]`(chamber 제거). `chamber_id = group_key[0]`. `step`은 int(DB SMALLINT 정합).
- **시간 규약**: Nelson과 동일 — 엄격 과거 드롭, 동일 timestamp 허용, value NaN/None 스킵+로그.
- **레짐 flush 신호(#5)**: `pm_count`·`limit_version` 두 필드로 감지(§5). 이 둘을 Point에서 뺀 v3는 flush 구현 불가였음 → 6필드로 정정.

### 4-2. reference (control_limits_loader)

`limits[gk] = {center, sigma, ucl, lcl, method, limit_version}` → center_ref=center, σ_ref=§3-4. `whitelist:set`. **소스 = `control_limits`(현행 관리선 — 엔진 조회 대상). `limit_corrections`(이력)는 안 읽음.** 행 선택 = §3-5.

### 4-3. 출력 tttm 객체 (계약 정합 — #7 alert/DB 분리)

`chamber_rollup(chamber)` → dict. **alert 필드**와 **DB 전용 필드**를 구분:

| 필드 | 값 | alert `tttm`? | tttm_comparisons |
|---|---|---|---|
| `reference` | `"fleet_median"` (B2) | ✅ | `reference_type` |
| `score` | §3-2 max σ-갭 | ✅ | `tttm_score` |
| `top_gap_sensor` | 최악 센서 (C코드) | ✅ | `top_gap_sensor` |
| `gap_pct` | signed % (NULL 가드) | ✅ | `gap_pct` |
| `reference_suspect` | §3-3 (top ∈ suspect) | ✅ | `is_reference_suspect` |
| `reference_id` | σ_ref 출처 `limit_version` | ❌ **DB 전용** | `reference_id` |
| `chamber_id` | 롤업 키 | ❌ **DB 전용** | `chamber_id` |

> ⚠️ **#7**: `fdc.alert.tttm`(계약 §3)은 위 ✅ 5필드뿐 — `reference_id` 없음. B4-1이 이 dict를 **그대로 alert에 실으면** `reference_id`가 신규 필드로 유입 → **헌법 2-2**(소비자 리뷰 + API_Contract 동시 갱신) 발동. → alert엔 ✅만, `reference_id`/`chamber_id`는 ③ DB writer만 소비.

---

## 5. 알고리즘 (per-wafer)

```
feed(point):
  gk = point.group_key ;  ch = gk[0] ;  fk = gk[1:]
  if gk not in whitelist:  log 1회; return []              # pin ⑤
  if gk not in limits:     warn; return []                 # reference 없음(엣지)
  if ch in excluded:       return []                        # pin ⑥ 훅
  if value None/NaN:       log; return []                   # Nelson 규약
  σref = sigma_ref(gk)                                      # §3-4
  if σref <= EPS:          log; return []                   # #2 degenerate skip

  try: dt = datetime.fromisoformat(point.timestamp)
  except ValueError: log; return []                        # #5 파싱 실패 skip
  st = state.setdefault(gk, GroupState())                  # #4 KeyError 방지
  if st.last_dt is not None and dt < st.last_dt: log; return []          # #5 엄격 과거 드롭
  if st.last_pm_count is not None and (point.pm_count > st.last_pm_count  # 🔴 #2 None-가드(첫 wafer 크래시 방지)
                                       or point.limit_version != st.last_limit_version):
        st.window.clear()                                  # 3-C flush
  st.last_pm_count = point.pm_count ; st.last_limit_version = point.limit_version ; st.last_dt = dt
  st.window.append(value)   # deque(maxlen = tttm_window_wafers)

  if len(st.window) < min_fill:  return []                 # 3-B warmup (warm ≡ len≥min_fill)
  st.center_live = median(st.window)                        # 3-A window median

  # fleet 구성원 = 이 fk에서 warm·비제외·σ_ref>EPS 인 챔버 (존재검사 먼저 — #4)
  members = { g[0]: state[g].center_live
              for g in state
              if g[1:]==fk and warm(state[g]) and g[0] not in excluded and sigma_ref(g) > EPS }
  if len(members) < MIN_FLEET_CHAMBERS:  suspect_sensors.discard(fk); return []   # #10 fleet<MIN→discard(래칭 방지) + §8 보류
  fleet_median[fk] = median(members.values())

  suspect = reverse_rule(fk, members)                       # §3-3 (set/discard)
  gap_σ   = (st.center_live − fleet_median[fk]) / σref
  gap_pct = pct(st.center_live, fleet_median[fk])           # §3-2 (|median| 가드)
  return [{gk, gap_σ, gap_pct, suspect}]                    # 롤업은 chamber_rollup가 max
```

**자료구조**: `state: dict[gk → GroupState{window: deque(maxlen=W), center_live, last_pm_count, last_limit_version, last_dt}]` (Nelson `_GroupState` + `last_pm_count`·`last_dt` #5). `fleet_median`·`suspect_sensors`는 fleet_key 캐시(매 wafer 해당 fk만 갱신). **`chamber_rollup`(#4 가드)**: 그 챔버 gk 중 `warm ∧ σ_ref>EPS ∧ fk∈fleet_median`만 골라 `gap_σ` **재계산**(없으면 `None`) → `worst_gk`=argmax|gap_σ| → `score`=|gap_σ|·`top_gap_sensor=worst_gk[-1]`·**`reference_suspect = worst_gk[1:] ∈ suspect_sensors`(#1 fleet_key)**. 저장 안 함(#15).

> **#9 flush 후 reference stale**: `limits`는 생성 시 1회 주입(정적). 코어가 limit_version 변경에 **window flush로 반응**하지만 center_ref/σ_ref는 v1 고정 → 라이브 배선(B4-3) 전엔 flush가 **v1 코어에선 사실상 no-op**(reference 리로드 부재). 재요란 후 새 운전점을 옛 reference로 재는 stale은 provisional 제외(B6-3)가 마스킹 — 둘 다 이월. plan에 "reference 리로드=B4-3" TODO.

---

## 6. Config

| 키 | 값 | 근거 |
|---|---|---|
| `spc.tttm_warning` | 2.0 (기존 B1) | score warning + 역방향 이탈 임계(2-C) |
| `spc.tttm_critical` | 3.0 (기존 B1) | score critical |
| `spc.tttm_reference` | `fleet_median` (기존 B2) | 참조 방식 |
| `spc.reverse_rule_min_chambers` | 4 (기존 B3) | 역방향 발동(active 분모) |
| `spc.tttm_window_wafers` | **신규 20~30** | rolling window 길이. Incident 병합창 20과 정합 |
| `spc.tttm_window_min_fill` | **신규**(제안 = window 또는 ⌈W/2⌉) | warmup 최소 충전(3-B) |

- **명명 상수(6-1, #11)**: `EPS`(0 나눗셈 가드 epsilon, 파일 상단)·`MIN_FLEET_CHAMBERS=2`·`분해능`(센서 양자화 스텝, 근거 있는 값)는 매직넘버 금지 대상 → **파일 상단 상수** 또는 config. k_sigma는 §3-4에서 `limit_engine.control_limit_k_sigma` 재사용.
- **권한(정정)**: 신규 2키는 `spc:`절 = **B 권한**이라 **B가 config PR + 사유로 등재(PM 승인 불요)**. `합의안` 문서만 `docs/`(PM 소유)라 그 sync PR에 **PM 리뷰**가 붙음(값 승인이 아니라 문서 소유권). window 값 확정 후 등재하되 **단위테스트는 주입값으로 등재 전 착수 가능**.

---

## 7. 테스트 설계 (TDD)

### 계층 1 — 단위 (순수, DB 불필요, config 주입)

| 케이스 | 검증 |
|---|---|
| score 산식 | 챔버 1대만 오프셋 → gap_σ = (center−median)/σ_ref 수치 일치(tol) |
| gap_pct 부호(#6) | 음수 median 센서에서 center>median → gap_pct **양수**(`/|median|`) |
| warmup(3-B) | window < min_fill 챔버는 fleet median 표본·score 0건 |
| window median(3-A) | window에 crazy 1장 → center_live 불변 |
| **spike 흡수(#10)** | 단일-wafer 스파이크 → score 낮게 유지(median robust, 미발동) |
| 역방향 발동 | 3/4 챔버 C17 동일방향 >warning → fk suspect=true *(챔버 6→4, reverse_min 4→3, 2026-07-19)* |
| **역방향 해제(#1)** | 발동 후 드리프트 해소 → suspect=**false**(discard 확인, 래칭 없음) |
| 역방향 전챔버 억제(2-A) | suspect 센서에서 무고한 2챔버가 그 센서 top → reference_suspect=true |
| 방향 분리 | 2 up + 2 down(각 <4) → suspect=false |
| **σ_ref=0 역방향(#2)** | 한 챔버 σ_ref=0 → 크래시 없이 그 챔버 제외·나머지 정상 판정 |
| 분위수 σ_ref | KEEP\* gap_σ가 (ucl−lcl)/2k 분모 |
| 레짐 flush(3-C) | pm_count 증가 point → window clear + **재충전 후** 판정 재개(post-flush assert) |
| 제외 훅(pin ⑥) | set_excluded({SIM_CH_3}) → CH3이 median·역방향·score 전부 빠짐 |
| whitelist skip(pin ⑤) | EXCLUDE gk → 판정 없이 skip+1회 로그 |
| gap_pct 가드 | fleet_median≈0 → gap_pct=NULL, score 정상 |
| chamber_rollup | 여러 센서 중 max|gap_σ| = score, 그 센서 = top_gap_sensor |

### 계층 2 — DoD 시나리오 (multi_chamber)

`scenarios/multi_chamber_common_c17.yaml`(≥4챔버 C17 공통 드리프트) 재생. **답안지(`sim_events`) 조회 금지** — 주입 사양(YAML)으로만 assert(절대규칙 5):

1. 초반(warmup 중) 판정 없음
2. **post-shift 충분히 진행(≥ window 채워진 뒤)** window 충전 → C17 fk **suspect=true**(#13: 판정은 full-fill 이후에만, 4/4 knife-edge라 부분충전 시 up=3로 미발동 가능)
3. suspect로 개별 챔버 `reference_suspect` **플래그** 세팅(에스컬 재료 — 소비는 B4-1)
4. **대조(수정, #10)**: 단일 챔버 **지속 드리프트**(≥3·σ_ref, ≥window wafers) → suspect=false + 그 챔버 score>critical(개별 경로). ※ "spike"는 계층1에서 별도 검증(흡수)

---

## 8. 엣지케이스 & 가드

- **reference 없음**(gk not in limits) → warn + skip. seed(B3-2)가 감시집합=is_active라 정상 경로선 미발생.
- **σ_ref ≤ EPS**(zero_sigma·분위수 밴드 붕괴 ucl=lcl·글리치) → **score·역방향 양쪽 skip + 로그**(#2, ZeroDivisionError·spurious critical 방지, floor 아닌 skip).
- **fleet median 표본 부족**: warm·비제외·σ_ref>EPS 챔버 < `MIN_FLEET_CHAMBERS`(2) → 판정 보류(로그). 역방향은 active<min_chambers면 자연 미발동. ⚠️ **min-active floor(#①)**: 2-B 'active 과반' 채택 시 active 소수(2) 헛발동 하한 — B6-3 확정(코어 threshold=3라 무해, 챔버 6→4).
- **fleet_median ≤ EPS** → gap_pct NULL, score(σ-등가)는 정상.
- **crazy wafer(B9)** → window median 흡수(3-A) — 스팟에 안 흔들림(HOLD는 B9 별도).
- **레짐 전환(pm_count↑/limit_version)** → window flush(3-C). post-PM 챔버는 window 비어 warmup 미충전 → 자동 제외(3-B). ⚠️ **#9**: reference(center_ref/σ_ref)는 v1 고정 → flush는 v1 코어에서 no-op, 리로드는 B4-3.
- **is_qual wafer(#14)**: QUAL 5장(고정 표준 조건)은 median 오염 소지. 코어 `Point`엔 `is_qual` 없어 코어가 못 거름 → **상류(B4-1 필터·B6-3 Phase-0 제외)가 담당하는 전제**를 명시(코어 밖). 라이브 배선 시 확인 대상.
- **챔버 staleness**: wafer 공급 멈춘 챔버의 옛 center_live가 fleet median에 잔류(오염) — sim(라운드로빈 전원가동)엔 무해, **라이브 TTL은 B4-1 배선 이월**.
- **동일 timestamp·시간 역전** → Nelson 규약(과거 드롭·동일 허용).
- **부동소수** → gap·median 반올림 안 함(재현성).

---

## 9. 배선 대상 (forward-coupling — 의미 고정, 구현 이월)

| # | 의미 고정(코어) | 배선 대상 | 근거 |
|---|---|---|---|
| ① | reference = 레짐 수립 행만. **is_active 비의존 trigger_type 쿼리** + **reference 리로드**(flush 후). 코어는 v1=active·flush no-op | **B4-3** (loader trigger_type 노출·전용 쿼리·리로드) | 결정 1·#9 |
| ③ | reference_suspect = **개별 억제 + 에스컬 라우팅**(코어는 플래그만·score는 전센서 max) | **B4-1** | pin ③·#3 |
| ④ | Context Score는 σ-score 소비(gap_pct 아님) | **B4-1** | pin ④ |
| ⑥ | 제외 훅. **active 과반 + min-active floor** 확정 | **B6-3** | pin ⑥·2-B·#① |
| ⑦ | reference_suspect 순간 판정 — 플래핑 가능 | **B4-1** (플래그 전이 **디바운스**. ※ 헌법 1-4 Incident 병합 부분 커버) | 2-D·#② |
| ⑧ | `reference_id`/`chamber_id` = **DB 전용**(alert 실으면 2-2) · `is_qual` 필터 전제 | **③ writer / B4-1** | #7·#14 |
| — | tttm 객체 형태(§4-3) 확정 | **③ writer** / **B4-1** | 계약 |

---

## 10. 의존성 & 헌법 정합

- **입력**: fdc.raw Point(B4-1 컨슈머 구성 — 코어 테스트는 직접 주입) / control_limits_loader(B3-2) / config(B1~B3 + 신규 2키).
- **소비**: chamber_rollup → B4-1(alert `tttm`)·③(tttm_comparisons) — **엔진은 판정까지, 발행·영속 미포함**.
- **헌법**:
  - **1-2**(판정만 — 발행 B4-1 단일 채널) · **1-3**(fdc.raw만·C65 없음) · **1-1**(control_limits 읽기만·변경 없음) ✓
  - **2-2**(스키마): `reference_id` alert 유입 금지(§4-3 #7) — 코어는 DB 전용 표기까지.
  - **3-1**(agent_b_spc B소유·시나리오 read-only) · **3-2**(DDL 미변경) ✓
  - **6-1**(window·min_fill·임계·**EPS·MIN_FLEET_CHAMBERS·분해능** 전부 config/명명상수, k_sigma 재사용) · **6-4**(snake_case·C코드) ✓
  - **6-2**(컨슈머 graceful shutdown·역직렬화 skip)는 **B4-1 컨슈머 몫**(코어 엔진엔 Kafka 없음).
  - **절대규칙 5**(sim_events 답안지 조회 금지): §7 테스트는 시나리오 YAML로만 assert ✓
- **문서 동기화(4-3)**: 신규 config 2키 → `config_파라미터_합의안_v1.md` B절 sync(값=B 권한·문서 소유는 docs/ PM 리뷰). README B4-2 상태 갱신.
- **후속**: 이 스펙 → 구현 계획(plan) → TDD. 라이브 배선(B4-1)·DB writer(③)·가한계(B6-3)·reference 리로드(B4-3)는 §9 이월.
