# agent_b_spc — Smart SPC Engine (팀원 B 팀원 B)

`fdc.raw`를 구독해 Nelson Rules(N1~N8)·TTTM을 판정하고 `fdc.alert`를 발행하며,
실력치(Control Limit)를 관리하는 모듈. (RnR v3 / 기획서 v4.5 Pipeline C·D)

## 구성

| 파일 | 내용 | 상태 |
|---|---|---|
| `initial_limits.py` | **B2-1**: 초기 실력치 테이블 산출 (train PM 리셋 이후 세그먼트 기반) | ✅ |
| `limits/initial_limits_v1.csv` | 산출된 초기 관리선 69그룹 (limit_version=v1, 2026-07-14 재생성 — C54/C56 과도 재정정) | ✅ |
| `limits/initial_limits_v1_meta.json` | 표본 구간·파라미터 메타 | ✅ |
| `limits/qa_report_v1.md` | 그룹별 QA (표본 수·σ·CV·가성알람·한계 폭 sanity) | ✅ |
| `monitoring_whitelist.py` | **B2-1 보완①**: 센서×step×window 감시 대상 판정 (초안) | ✅ |
| `limits/monitoring_whitelist_v1.csv` | 그룹별 감시 판정 (**B가 검토·수정할 원본**) | 🔶 초안 |
| `limits/monitoring_whitelist_v1.md` | 판정 카테고리별 요약 | ✅ |
| `limits/control_limits_ddl_proposal.sql` | `control_limits` 테이블 DDL 제안 — **PM 승인·반영 완료** (`db/init.sql` v4.6, `method`/`q_low`/`q_high` 포함, 2026-07-13 확인) | ✅ |

## SPC 판정 엔진 (B3-1 Nelson / B4-2 TTTM)

| 파일 | 내용 | 상태 |
|---|---|---|
| `nelson_engine.py` | **B3-1**: Nelson N1~N8 + 조합②(KEEP\* 분위수 2-track) 판정 엔진 | ✅ |
| `tttm_engine.py` | **B4-2 코어**: fleet median 대비 개별 outlier(`score`) + baseline 대비 집단 이동(`reference_suspect`) 판정. 입력 `fdc.raw`만(D0 독립). 발행·DB·가한계 제외는 이월(설계 §9) | ✅ 단위 34 |
| `control_limits_loader.py` | **B3-2**: `control_limits`(is_active) → `(limits, whitelist)` 로더 (Nelson·TTTM 공용 reference 소스) | ✅ |
| `tttm_writer.py` | **③ writer**: 엔진 `chamber_rollup` dict → `tttm_comparisons` **append-only** INSERT(`rollups_to_rows`·`write_comparisons`). **commit은 호출자 소유**(테스트=rollback / 라이브=commit), 스키마 변경 0(DML only). | ✅ 단위 9 + 왕복 1 |

> 설계·계획: `specs/B4-2_TTTM엔진_설계.md` · `plans/B4-2_TTTM엔진_플랜.md` · ③ writer `specs/B4-2_TTTM_writer_스펙플랜.md`.
> config 2키(`tttm_window_wafers`/`tttm_window_min_fill`) = `spc:`절 B 권한, **합의안 공식 등재는 PM sync 대기**.

## Recipe Tuning 엔진 (B5-4)

| 파일 | 내용 | 상태 |
|---|---|---|
| `recipe_engine.py` | **B5-4 코어**: SHAP+위반 → 튜닝안(`compute_tuning`, 순수·total). 스코프 축소(D11 — 게인 식별 불가): **C4 축 "방향만" + 고정 정책 스텝(±`recipe_step_pct`)**, 나머지 축 `escalate_only`. `direction_sign` 상쇄·D6 누적 예산·`applied_setpoints` 역주행 방지(D10)·`applied_value` 필드(C ⓐ)·**온도 반복 정책**(명목 앵커 부재 → Incident당 1스텝, 반복이면 에스컬·새 Incident면 리셋 — `temp_tuned_this_incident`, C ⓑ) | ✅ 단위 48 |
| `recipe_writer.py` | **B5-4 적재**: `resolve_alert_context`(spc_violations 조인 → recipe_id·step, D13) + `resolve_applied_setpoints`(D10 역주행) + `resolve_temp_tuned_this_incident`(온도 A — C가 `compute_tuning`에 주입, C ⓑ) + `record_recipe_proposed`(RCP 무패딩 채번·SEQ 재시도·NOT NULL 선검사·Incident당 PROPOSED 1행·`shap_basis`=원본). commit은 호출자 | ✅ 단위 14 + 실 DB 1 |
| `config/recipe_knob_map.yaml` | 손잡이 맵(B 소유·데이터 사전 §0 파생) — `status:active/escalate_only`·`direction_sign`·`nominal_setpoints` 이관 | ✅ |

> 설계: `specs/B5-4_Recipe정량엔진_스펙플랜.md`(v2.2). config 2키(`recipe_step_pct`·`recipe_knob_map_path`) = `spc:`절 B 권한, **합의안 등재·`docs/`·계약 doc-sync는 PM/C 크로스파트 대기**(스펙 X1·X4·X6). C 스텁(`app/tools/recipe.py`) 교체·`sensitivity` 재정의 사인오프는 **C 수행·B 리뷰**(S4).

## 초기 실력치 산출 방법 (B2-1)

```
표본:  마지막 C33 리셋(C64_9664, 3692번째 wafer) 이후 복귀 블록 6,824장
       (실험구간 C6_1 93장 + 실험前 C6_0 1,330장 제외 — 2026-07-11 회의 결정)
       − seasoning 첫 10장 (A7) = 6,814장 실사용            [A8: PM 리셋 이후 안정 구간]
지표:  wafer × Step × Window(C42 과도/정착) 단위 센서 평균 (summary indicator)
산식:  그룹별 시간순 최근 min(500, n)장 → 양쪽 1% 트림 → mean ± 3σ
       (KEEP\* 6그룹은 분위수 — 2-track, 아래 회의 결정 참조)
       [A1 k=3.0, A4 rolling N=500 — 멘토 확정 산식을 t=0에 적용]
그룹:  Chamber(C24) × Recipe(C6_0만, C6_1 실험구간 제외) × Step(C7) × Window × 센서
       = 69그룹 (2026-07-14 재생성 — 종전 148그룹에서 recipe 축 절반 + D_CH5960 제외 +
       C54/C56 과도 재정정)
센서:  API Contract 0절 "SPC 대상 ✅" 12종 + 파생 1종(D_VDC_RES=C11−C12) +
       매칭 4종(C18/C27/C54/C56 — 과도 window만). D_CH5960(C59/C60 재구성)은 SPC 미적용 제외.
       (C54/C56은 2026-07-11 정착 재분류 결정을 2026-07-12 번복 — 아래 회의 결정 참조)
```

실행: `python -m src.agent_b_spc.initial_limits` (파라미터는 `config/params.yaml`에서
로드 — `LimitConfig`)

## QA 플래그 (4종)

`compute_limits`가 그룹별로 자동 진단. 자동 탈락이 아니라 **엔지니어(B) 검토용 표식**.
(구 `low_n`(표본<30)은 C6_1 제외 후 전 그룹 n=500이라 미발동 → 삭제. 소표본 가드는 B4-3.)

| 플래그 | 조건 | 의미 |
|---|---|---|
| `zero_sigma` | σ = 0 | 해당 step에서 센서가 상수(물리적 OFF) |
| `high_false_alarm` | 자기 표본 이탈율 > 2% | ±3σ 정규 가정이 약함(알람 과다 예상) |
| `wide_limits` | 한쪽 부호 센서인데 ±3σ 밴드가 0을 넘어감 | 정규 가정 붕괴 — 한계가 물리적으로 무의미 |
| `high_cv` | σ/\|center\| > 0.5 (0을 안 걸치는 센서) | 변동계수 과대 — 한계가 무의미하게 넓음 |

> `wide_limits`·`high_cv`는 **D_CH5960 발견을 계기로 추가**(2026-07-07). 기존 QA는
> "알람 과다"만 봤을 뿐 "한계가 쓸모없이 넓어 아무 알람도 안 나는" 경우를 못 잡았음
> (`false_alarm_pct=0`이라 통과처럼 보임). 이제 자동으로 표식된다.

## QA 결과 요약 (v1, 2026-07-14 재생성 — 69그룹 중 31그룹 플래그)

- **zero_sigma 15그룹** — RF(C31/C32)·가스(C15/C16)·C57이 비활성 step에서 상수인
  물리적 OFF 조합. 모니터링 대상 제외 권고. (C54/C56 정착 zero_sigma 8그룹은 2026-07-12
  과도 재정정으로 소멸 — 아래 회의 결정 참조)
- **high_false_alarm 8 / wide_limits 9 / high_cv 7그룹** — 대부분 해당 step에서
  꺼져있거나 0 근처인 센서(C32 정착 시 반사파≈0 등). 진짜 플라즈마 활성 센서
  (C11 step4 등)는 무플래그.
- **C6_1 실험구간 — 표본 얇음 문제 자체가 소멸**: "표본 부족(더 모으면 해결)"이 아니라
  "실험 구간이라 baseline 부적절"로 재정의(2026-07-11 회의) → v1 baseline은 복귀 블록
  6,824장(C6_0)만 사용, C6_1(93장)은 애초에 표본에서 제외.

> **F14 감시 피처 재검토 — ✅ 완료(2026-07-10)**: raw C11 정착 평균 vs `D_VDC_RES`(C11−C12) 비교 →
> **raw C11 유지**(교체 기각). step4에서 D_VDC_RES가 열등하고, step5·6·7의 높은 corr(−0.78)는 챔버 노후
> 시간추세가 만든 교란(spurious)이었음(차분·레짐내 검증). C6_0 D_VDC_RES는 알람에서 제외(계산은
> 드리프트 트리거 후보로 보존). 상세: `analysis/F14_feature_review.md`.

## 보완① — 모니터링 화이트리스트 (센서×step×window 감시 대상)

**목적**: 69그룹을 그대로 Nelson 엔진(B3-1)에 먹이면 꺼진 센서에서 가성알람이 폭증하므로,
감시 대상을 명시적으로 확정한다. `monitoring_whitelist.py`가 **초안**을 자동 판정하고,
**B가 `monitoring_whitelist_v1.csv`의 `decision`을 검토·수정해 확정**한다.

**판정 규칙** — "활성도(range 비율)"와 "크기(|center| 비율)"를 **둘 다** 봄:
```
EXCLUDE   : σ=0 (상수 — 물리적 OFF). 감시 불가. [자동 확정]
EXCLUDE?  : activity < 5% 그리고 mag < 10% (준-꺼짐). [제외 제안 — B 검토]
KEEP*     : 활성이나 false_alarm > 2% (비정규 → 보완②). [감시]
KEEP      : 활성·정상. [감시]
```
> range만 보면 C62/C63처럼 **높은 값에 안정적으로 활성**인 센서(range 작음)가 "꺼짐"으로
> 오판됨 → 크기 조건을 함께 걸어 해결 (C62 step4·C63 step4 정상 KEEP 확인).

**결과 (v1 재생성, 2026-07-14)**: KEEP 39 / KEEP\* 6 / EXCLUDE 20 / EXCLUDE? 4 (총 69그룹)
→ **감시 대상(KEEP+KEEP\*) 45그룹**
- **소비 규칙**: B3-1 Nelson 엔진은 `KEEP`·`KEEP*`만 감시. EXCLUDE·EXCLUDE?는 제외.
- **EXCLUDE? 4그룹 = C11(Vdc) 비플라즈마 step(1·5·6·7) → 제외 확정** (B, 2026-07-07).
  Vdc는 플라즈마 ON에서만 유효 — 오탐 4~6%가 alert·Incident 오염. **사각지대(예상치 못한
  점화 등)는 별도 고정임계 규칙으로 대비** → `monitoring_whitelist_v1.md`의 "향후 감시 조건" 참조.
- KEEP\* 6그룹 = C61(step1·6·7)·C57(step4)·C58(step4)·C31(step5) → **보완②(관리선 방식,
  분위수)** 대상. D_VDC_RES는 F14 판단(2026-07-10)으로 EXCLUDE(감시 가치 없음 — 계산은
  드리프트 트리거 후보로 보존).

**파라미터**: `activity_min=0.05`, `mag_min=0.10` — `config/params.yaml`(spc)에 등재되어
`WhitelistConfig`로 로드.

실행: `python -m src.agent_b_spc.monitoring_whitelist`

## ✅ 회의 결정 (2026-07-11) — 비정규(KEEP\*) 센서의 관리선·Nelson 방식 (결정 A·B → 조합②)

> **결정: 조합②** — KEEP\* 6그룹은 **분위수 관리선** + **σ-무관 룰만**(N1 분위수 + N2·N3·N4, N5~N8 제외).
> ⚠️ **주의(최우선)**: N5~N8 조기경고 상실 → 미탐>오탐 원칙과 충돌 → **W7 Scorecard 미탐 0건 확인 필수**(아니면 롤백). 전체 주의 7건: `회의안건_요약_B.md` §1.
> 아래는 결정 배경·선택지 기록.

**배경(보완②)**: KEEP\* 17그룹(C61·C31·C57·C58·D_VDC_RES 등)은 활성이나 분포가 비정규라
±3σ가 안 맞아 자기 표본 가성알람 평균 4.56%(일부 9%). 원인 진단(500장 기준):

- **드리프트(시간 추세): 0그룹** → 정기 재산정(B4-3)으로 해결되는 문제 아님
- **치우침(skew): 13/17그룹**(\|skew\|>1) → ★주범. 비대칭 분포
- 이산/양자화 7/17 (일부 skew와 중복)

**방식별 실측 가성알람**: μ±3σ **4.56%** / 분위수(0.135~99.865%) **1.28%** / MAD 8.30%(악화).
→ 분위수가 최선이나, 아래 두 결정은 **멘토 확정(±3σ) 이탈 + C/PM 소비 + Scorecard 공동채점**이라
**혼자 결정 불가 — 팀·멘토 논의 필요**.

> **결정 A·B는 커플링**: A에서 분위수 채택 시 B의 σ-존도 분위수화하거나 σ-존 룰을 빼야 함.

### 결정 A — 관리선 방식 (±3σ vs 분위수)

| | 장점 | 단점 |
|---|---|---|
| **±3σ (현행·멘토확정)** | 멘토 확정·승인 불요 / Nelson σ-존과 일체 / 재산정 산식 통일 / 작은 표본 안정 / 단순 | 비대칭서 오탐 4~9% / LCL 물리적 이탈 / 정규 보장 상실 |
| **분위수 (KEEP\*)** | 비대칭 강함(4.56→1.28%) / 무가정 / 물리적 타당 | 멘토확정 이탈 / **작은 표본 불안정(C6_1 n=93)** / Nelson σ-존 단절 / 엔진 이원화 / 극단 skew는 잔존 |

**고려 상황**: C6_1 표본 얇음(분위수 신뢰↓, 시뮬레이터 증량 시 완화) · 데모 스코프(설명성 vs 오탐) ·
±3σ/분위수 혼합의 유지보수 비용 · KEEP\* 센서가 핵심 불량신호(SHAP 상위)인지 확인 필요.

### 결정 B — 치우친 센서의 Nelson 룰 범위

σ-존 의존 룰 = **N1·N5·N6·N7·N8** (skew 취약) / σ-무관 = **N2·N3·N4**(방향·추세, 분포 무관 안전).

| 옵션 | 내용 | 장점 | 단점 |
|---|---|---|---|
| B-1 | 전체 룰 적용(현행) | 단순·최대 커버리지 | σ-존 룰이 skew서 오탐 |
| B-2 | σ-무관 룰만(N1분위수+N2/3/4) | 오탐↓·skew 안전 | **미탐 위험**(N5/N6 조기경고 상실)·룰셋 이원화 |
| B-3 | σ-존을 분위수로 재정의 | 정교·통계적 최선 | 구현·검증 부담 최대·데모 스코프 초과 |

**고려 상황**: 시연 시나리오가 어떤 룰로 잡히나(N1/N2/N3 위주면 B-2 부담↓) · MVP는 라우팅
커버리지 100%(M12)가 목표지 룰 정교화 아님 · 유닛테스트 8/8(B3-1 DoD) 복잡도 · **Etch는
미탐>오탐 비용**이라 조기경고 룰 제거 신중.

### 조합 3안

| 조합 | A | B | 성격 |
|---|---|---|---|
| ① 보수 | ±3σ | 전체(B-1) | 현행 유지·skew 오탐 감수. 최단순·멘토확정 그대로 |
| ② 실용 | KEEP\*만 분위수 | B-2 | skew 센서는 극단+추세만. 오탐↓·스코프 적정 |
| ③ 정교 | 전부 분위수 | B-3 | 통계적 최선·구현/검증 부담 최대 |

**✅ 회의 결정: 조합②** (KEEP\* 6그룹 분위수 + B-2). 근거: 오탐이 skew 긴 꼬리의 정상점이라 분위수로
소멸(4.56→1.28%, C6_1 제외 후 6그룹은 4.4→0.3%), 데모 스코프에 B-3는 과함. 구현 시 `q_low`/`q_high`
config 등재(PM). **후속·주의사항 전체는 `회의안건_요약_B.md` §1.**
근거 스크립트: `monitoring_whitelist.py`(KEEP\* 분류) · `analysis/false_alarm_mechanism.py`(오탐 정체 실측).

## ✅ 회의 결정 (2026-07-11) — D_CH5960 SPC 미적용 (제외)

**발견(2026-07-07)**: C59/C60 재구성값 `D_CH5960`은 wafer 레벨에서 **bimodal**(작은 무리
~40 / 큰 무리 ~80만)이라 mean±3σ 틀에 맞지 않음. 산출된 관리선이 CV 0.79~1.15,
LCL ≈ −70만(원본은 전부 양수)으로 **물리적으로 무의미**. 전 그룹 `wide_limits;high_cv` 표식됨.

- **제안**: v1 실력치에서 D_CH5960 **제외**. 필요 시 채널별 개별 관리 또는
  "작은/큰 무리" 상태 범주 룰(SPC 아닌 방식)로 별도 검토.
- **✅ 결정(2026-07-11)**: v1에서 **제외 확정**(SPC 미적용) → `DERIVED_SETTLED`에서 D_CH5960 제거·재생성(**B 파일만, 타팀 무관**). **알람 파생지표 0개 → API Contract 파생지표 표기 불필요(안건 소멸).** C59/C60 재구성 개념은 데이터 사전에 유지(A는 raw C59/C60 사용, 별개).
- 근거 데이터: `데이터_사전_v1.md`(C59/C60 상호배타 멀티플렉스, 물리량 미상).

## ✅ 회의 결정 (2026-07-11 → 2026-07-12 번복) — C54·C56 과도(transient) 유지

**발견(2026-07-07)**: 현재 `SENSORS_TRANSIENT_ONLY = [C18, C27, C54, C56]`은 API Contract
센서표가 매칭 계열 4개를 한 묶음으로 "과도 Window만"으로 표기한 것을 따른 것. 그러나1
Step 4 실측 검증 결과 **C54·C56은 정착 구간에서도 신호가 살아있음**(과도/정착 std 거의 동일):

| 센서 | 과도 std | 정착 std | 판정 |
|---|---|---|---|
| C18 | 4.26 | 1.11 | 과도 전용 ✓ (정착에서 잠잠) |
| C27 | 85.90 | 2.81 | 과도 전용 ✓ (정착에서 죽음) |
| C54 | 818 | **832** | ⚠️ 정착에서도 살아있음 |
| C56 | 885 | **866** | ⚠️ 정착에서도 살아있음 |

- **원인**: C54·C56은 "매칭 축 **절대 위치**"값(`데이터_사전_v1.md`: "정착 구간 준고정")이라
  정착에도 유효. 과도 전용 분류 시 **정착 값 모니터링 누락**(틀린 값 아님 — 커버리지 갭).
- **✅ 결정(2026-07-11)**: C54·C56을 **정착으로 이동**(`SENSORS_TRANSIENT_ONLY`에서 제거). C18·C27은 현행 유지. **양쪽 관리는 안 함** — 과도 구간 오탐 리스크. → **W7 Scorecard에서 "정착-only" 미탐이 보이면 그때 양쪽으로 확장.**
- **후속(당시)**: ⚙️ 재생성(B) · 👑 **API Contract line 44** "과도 Window만"→"정착" 갱신(PM 승인) · 🔌 C(소비자) 통보.
- **정착 실측 결과 (v1 재생성, 2026-07-13)** — 정직하게: step1/5/6/7은 상수0(`zero_sigma`) →
  `EXCLUDE` 자동 확정. step4는 `KEEP`이나 ±3σ 밴드가 광폭(자기 표본 가성알람 0%) — **감시
  자체는 살아있지만 사실상 무력**(한계가 느슨해 이상이 나도 안 걸릴 가능성). 광폭이 "진짜 넓은
  정상 변동"인지 "두 위치가 섞인 것"인지는 미확인.
- **⚠️ 번복(2026-07-12, PM 승인)**: 위 "정착 이동" 결정을 **번복**. 근거였던 "정착 std≈과도"(위 표)는
  **raw 3초 std**였고, 관리선이 쓰는 **wafer-mean** 단위론 정착 480 vs 과도 148(3~4배) → **과도가
  ±3σ 약 2배 더 예민**(오탐 둘 다 0%). 게다가 정착의 타 step(1·5·6·7)은 상수0(dead)이라 정착 감시는
  바로 위 "정착 실측 결과"가 보여주듯 step4 광폭 하나만 남아 **사실상 무력**이었음. → **C54·C56은
  과도(transient)로 원복**(`SENSORS_TRANSIENT_ONLY = [C18, C27, C54, C56]`, step4 1그룹씩 KEEP —
  정착 EXCLUDE 8그룹 소멸, 총 그룹 77→69). 멘토 사후 공유. 상세: `회의안건_요약_B.md` §4.
- **후속(번복 반영)**: ⚙️ 재생성 완료(B, 2026-07-14) · 👑 **API Contract line 44 재갱신 필요**
  ("정착"→"과도 Window만", PM 승인 대상) · 🔌 C(소비자)에 "C54/C56 settled 판정 철회, 과도만 유지"
  재통보. C54/C56 실질 감시 강화(step4 광폭 무력 이슈)는 여전히 팀·W7에 위임한다.

## ✅ 회의 결정 (2026-07-11) — C6_1 실험 구간 baseline 제외

**멘토 확인(2026-07-08)**: 데이터셋의 **C6_1 구간은 실험 구간**이다. 그래서 소량이고, 이후
다시 C6_0으로 복귀했다. 데이터로 확인:

```
C6_0  1330장  (2018-12-23 ~ 12-31)   생산
C6_1    93장  (2018-12-31 ~ 01-01)   ← 실험 블록 (약 하루, 연속 1개)
C6_0  6824장  (2019-01-01 ~ 02-08)   생산 복귀
```

- **프레임 전환**: 기존 "표본 33~93장이라 불안정(데이터 더 모으면 해결)" → **"실험 구간이라
  정상 생산 baseline이 아님"**. 실력치는 "정상 생산이 평소 내는 범위"이므로 실험 wafer로
  baseline을 만드는 것 자체가 부적절. 표본 부족은 원인이 아니라 실험의 증상.
- **제안**: C6_1을 v1 실력치에서 **제외**, C6_0만 감시 (전 step N=500·오염 없음 —
  C6_0 최근 500장은 실험 블록 이후 1~2월 구간).
- **✅ 표본 정의 확정(v1 재생성, 2026-07-13)**: 실제로는 C6_1(93장)뿐 아니라 **실험 前
  C6_0(1,330장, 2018-12-23~12-31)도 함께 제외** — 현재 레짐 창(복귀 블록)만 사용하는 게
  일관되므로. 최종 표본 = **복귀 블록 6,824장**(`return_block_wafers`, `excluded_experimental`:
  C6_1 93장 + 실험前 C6_0 1,330장). 산출 결과는 **무영향** — 그룹별 최근 500장은 어차피
  복귀 블록 안에서 채워지므로(위 표 참조), "8,144(전 C6_0)" 표기는 폐기하고 6,824(복귀 블록)로
  통일한다.
- **효과**: 보완 3(얇은 표본) 해소 + **결정 A·B 축소** — KEEP\* 17→**6그룹**(전부 C6_0·n=500).
  분위수 불안정의 주범이던 C6_1(n=93) 케이스 소멸.
- **제외 시 코드 정리**: C6_1이 유일한 소표본(n<100) 그룹이므로, 제외되면 전 그룹 n=500 →
  `MIN_TRIM_N`(트림 가드)이 **한 번도 발동 안 함 → 트림 가드에서 제거, 값은 사전 게이트 임계 `cfg.min_trim_n`로 재활용**.
  `MIN_GROUP_N`(low_n QA 플래그)도 미발동 → **제거 완료**(low_n 플래그와 함께 삭제).
  **소표본 가드는 여기(1회성 초기 baseline)가 아니라 라이브 재산정(B4-3)에서 필요 시 별도 처리** —
  "방어용으로 여기 남기기"는 정작 B4-3가 이 파일을 안 쓰므로 성립 안 함.
- **✅ 결정(2026-07-11)**: **baseline 제외 확정**(C6_0만 감시). 시뮬레이터 리플레이 여부는 미확인이나 **안전측으로 감시 skip**("실험 → baseline 없음 → skip", `is_experimental` 식). 재생 안 하면 자연히 무시됨.
- **후속**: ⚙️ C6_0만으로 재생성 + skip 로직(코드 재생성 시 삭제 말고 `excluded_experimental` 기록 보존) · 👑 `데이터_사전_v1.md` "레시피 전환"→"실험 구간" 정정(docs/ PM 소유).

## ✅ 회의 결정 결과 (2026-07-11 — 전 안건 확정)

> 전체 결정·주의·후속은 `회의안건_요약_B.md`가 단일 소스. 아래는 결과 요약.

1. **결정 A·B → 조합②** (KEEP\* 분위수 + σ-무관 룰). ⚠️ W7 미탐 0건 검증 필수.
2. **C6_1 실험구간 → baseline 제외** + 리플레이 감시 skip.
3. **D_CH5960 → SPC 미적용**(제외). → 파생지표 alert 0개 → **API Contract 표기 불필요(안건 소멸)**.
4. **C54·C56 → 정착 이동 → 2026-07-12 번복(과도 유지)**(양쪽 관리 X, W7 미탐 시 확장). → 👑 API Contract line 44 재갱신.
5. **B7 → predicted_c65 가중+HOLD만** (단독 알람 X, 센서 주 채널).
6. **chamber_id 매핑 → 복제 + 오프셋 shift** — PM이 `chamber_offsets.json` 제공(수령). B2-1 baseline center를 챔버 오프셋만큼 평행이동해 시딩.
7. **👑 PM 처리 → ✅ 완료 (2026-07-13 확인)**: `control_limits` DDL(`db/init.sql` v4.6 반영) + **신규 파라미터 8개 config 등재** (`TRIM_QUANTILE·MIN_TRIM_N·MIN_GROUP_N·FALSE_ALARM_WARN_PCT·CV_WARN·ACTIVITY_MIN·MAG_MIN·TIME_GAP_THRESHOLD_HOURS` — `config/params.yaml` 반영 확인) + **분위수 `q_low`/`q_high`**(등재 확인). 확정 3종(k·N·seasoning)은 이미 config 로드 완료.

## 회의 후 진행 절차 (B2-1 마무리 로드맵)

회의(2026-07-11)에서 **전 안건 결정 완료**. 실제 반영·연동은 아래 순서로 진행한다. (담당/의존 표기)

**1단계 — 결정 반영 (코드/산출물 갱신, 담당 B)** — *✅ 완료 (2026-07-13, baseline 재생성)*
- [x] **C6_1 제외** → `initial_limits.py` recipe 필터 + `excluded_experimental` 마킹(삭제 아님) 완료. **리플레이 skip 로직(런타임)은 미착수** — B3-1/라이브 컨슈머가 아직 없어 대상 코드가 없음(회의안건_요약_B.md 후속 3). 동반: `MIN_TRIM_N` 상수 제거(값은 게이트 임계 `cfg.min_trim_n`로 재활용)·`MIN_GROUP_N`+`low_n` 제거. 소표본 가드는 B4-3에서 별도.
- [x] **C54·C56 정착 이동** → `SENSORS_TRANSIENT_ONLY`에서 제거. → **[x] 2026-07-12 번복** — 과도 유지로
  `SENSORS_TRANSIENT_ONLY`에 재편입, 재생성 완료(69그룹).
- [x] **D_CH5960 제외** → `DERIVED_SETTLED`에서 제거.
- [x] **결정 A = 조합②** → KEEP\* 6그룹에 분위수 산식(`method` 컬럼 + `q_low`/`q_high`), 나머지는 ±3σ 유지(**2-track**).
- [x] **결정 B = B-2** → **B3-1 엔진**(`nelson_engine.py`)에 반영 **완료 (2026-07-13)**: `method='quantile'` 그룹은 N1을 분위수 경계(ucl/lcl)로 판정하고 σ-존 룰 N5~N8 제외, N2·N3·N4 유지 (`_zones` method 분기 + `_active_specs` 필터 + `Zones.from_quantile`). 엔진은 `method`+`ucl`/`lcl`(활성 분위수 관리선)만 소비하고 `q_low`/`q_high`(레벨)는 compute/시딩 계층 몫 — DB `control_limits` 정합. 테스트 6종 추가 → **53 passed**(기존 47 회귀 0). ⚠️ W7 Scorecard 미탐 0건 검증 조건 유효.
- [x] 재생성 → `initial_limits_v1.csv`/화이트리스트/QA 갱신 (초기 baseline = `v1`, 69그룹 —
  2026-07-14 C54/C56 과도 재정정 반영, 종전 77그룹).

**2단계 — config 이관 (담당 B + PM)** — *✅ 완료*
- [x] **확정 3종(k=3.0·N=500·seasoning=10) → `initial_limits.py`가 `config/params.yaml`(limit_engine) 로드**. 합의안 v1.7에서 B 확정, `load_limit_config()` 추가 (헌법 6-1).
- [x] **신규 파라미터를 `config/params.yaml`에 등재 완료.** `trim_quantile·false_alarm_warn_pct·cv_warn`(+`q_low`/`q_high`, 조합② 분위수)은 `LimitConfig`(`initial_limits.py`)가, `activity_min·mag_min`은 `WhitelistConfig`(`monitoring_whitelist.py`)가 `config/params.yaml`(spc)에서 로드한다. `min_trim_n`도 트림 게이트 임계로 `LimitConfig`가 로드해 사용 중. `min_group_n`은 config에는 등재되어 있으나 현재 코드에서 참조하지 않음(C6_1 제외 이후 low_n 미발동 — 위 1단계 참조, 재검토 대상).
- [ ] **A4 N=500 근거 재검토 (B 소유 — `사이클_정의_v1.md` F1)**: 처리율이 **170→231 wafer/일**로 정정되어 N=500이 "3일치"가 아니라 **약 2일치**로 짧아짐. 값(500)은 유지하되 "최근 구간" 의도 유지 위해 창 길이 재확인. config 주석("3일치")도 갱신 대상(PM 소유 파일).

**3단계 — 챔버 매핑 (PM 협의, 담당 B + PM)** — *방식 확인됨 (2026-07-08)*
- [x] 가상 챔버 생성 방식 확인 — 시뮬레이터 `chamber_replicator.py`: 각 SIM_CH = 실데이터 + 센서별 고정 오프셋(std × ±offset_sigma, 시드 고정) + 행 노이즈.
- [x] 현재 단일 챔버 `C24_0` 기준 baseline → 가상 챔버 `SIM_CH_1~6` 반영 방식 **확정: 챔버별 복제(offset shift)** — B3-2 `seed_control_limits.build_seed_rows`가 `chamber_offsets.json`으로 밴드 전체를 평행이동해 6챔버 전개(구현·리뷰 완료 2026-07-14).
  - ⚠️ **챔버 6→4 축소 (멘토 2026-07-19)**: 코드 무변경(offsets가 결정) — `chamber_offsets.json` 4챔버 → 재시딩 시 자동 **감시45×4=180행**. 값 변경 아닌 챔버 수만 축소. `reverse_rule_min_chambers 4→3`(TTTM 과반 재정의). **라이브 chamber_offsets 4챔버 재생성은 PM 시뮬(dump_offsets) 선행 → Phase 2에서 반영·재시딩** (개발용 임시 4챔버는 offsets CH5·6 제거로 로컬 구성 가능).
- [ ] 시뮬레이터 리플레이 범위(C6_1 실험 블록 포함 여부)와 정합

**4단계 — DB 시딩 (담당 B)** — *✅ 완료 (2026-07-14, 라이브 Postgres 적재·검증)*
- [x] PM이 `control_limits` 테이블 생성(`db/init.sql`) — ✅ **확인(2026-07-13)**: v4.6로 반영됨, `method`/`q_low`/`q_high`(조합② 분위수) 컬럼 포함. *(`limits/control_limits_ddl_proposal.sql`은 7/7자 구버전 — 분위수 컬럼 없음, init.sql이 최신·단일 소스)* ⚠️ **dev 컨테이너 주의**: 기동이 v4.6 이전이면 최초-init에 이 테이블이 없을 수 있음 → `docker exec -i fdc-postgres psql -U fdc_admin -d fdc_platform < db/init.sql`(전부 `IF NOT EXISTS`, idempotent)로 보강.
- [x] 확정 baseline을 DB 적재 (현재는 CSV) — **B3-2 시딩 도구 구현·리뷰 완료 + 라이브 적재 완료(2026-07-14)**: `seed_control_limits.py` = 순수 `build_seed_rows`(조인·감시필터·shift) + 멱등 `seed()`(delete-then-insert, active 충돌 가드) + `--dry-run` CLI. **라이브 Postgres 실적재 270행**(active 270 / 6챔버 / sigma 234 + quantile 36) 확인. 실행: `DATABASE_URL=... python -m src.agent_b_spc.seed_control_limits`.
- [x] Nelson 엔진(B3-1)이 DB에서 관리선 조회하도록 연동 — **로더 `control_limits_loader.load()` 구현·리뷰 완료 + 라이브 왕복 검증**: `is_active` 행 → 엔진 소비 `(limits, whitelist)`(gk step=int 계약). seed→load→엔진 feed **왕복 통합테스트 6케이스**(챔버별 shift·quantile 판정·멱등·stale 제거) **라이브 6/6 PASS**. 실적재 후 `load()`→270그룹, C11 챔버별 ucl 상이 확인. 라이브 배선(Kafka)은 B4-1.

**5단계 — 문서 동기화 (헌법 4-3, 담당 B + PM)**
- [ ] C54/C56·파생 sensor_id가 alert에 실리면 → `API_Contract_명세서.md` 갱신 (소비자 리뷰)
- [ ] C6_1 실험 구간 → `데이터_사전_v1.md` 반영 요청 (PM 소유)

**완료 판정**: 위 5단계 후 확정 baseline이 DB에서 가상 챔버별로 조회 가능 → B2-1 종료.
이후 B3-1(Nelson 판정)·B4-3(실력치 엔진 재산정)로 이어짐.

---

## B4-3 — 실력치 재산정 엔진 1차 *(✅ 완료 2026-07-18)*

정기 리캘리 시점에 `(group_key, 최근 N장 값, 현행 관리선, 리셋 기준점, 분해능)` → 새 관리선 재산정
+ **변화없음/자동적용/승인대기** 판정 → 자동만 DB 반영. 설계·결정: `specs/B4-3_실력치재산정_*.md`.

- **`recalc_engine.py`** — `RecalcConfig`(LimitConfig 합성) · `recompute_group`(순수 코어) · `sigma_ref_of`(H4 공유 헬퍼) · `RecalcProposal`. 3구간 판정 σ 단위 통일: dead_band `<`(2·SE / 분해능 하한) / cap `>`(1회 0.5σ·누적 1.0σ — 이내=자동·초과=승인). 누적은 telescoping(`|new−reset|/reset σ`, signed 순이동). 가드는 나눗셈·compute 前(H-A). 분위수 그룹 sigma_ref=(ucl−lcl)/2k·UCL/LCL=q 경계. `resolution_bound`(res_σ≥0.5σ 이산센서 9그룹 자동 제외).
- **`correction_history.py`** — `reset_ref`(누적 telescoping 기준점): ⓐ control_limits initial v1 기저(is_active 배제) ⓑ limit_corrections {qual,incident,APPROVED_APPLIED} 최신((created_at,id) DESC) 덮어쓰기. 읽기 전용.
- **`recalc_writer.py`** — `apply_auto`(자동: UPDATE(is_active=false)→INSERT(new active)→limit_corrections AUTO_APPLIED, 버전 숫자캐스팅·correction_id SAVEPOINT 재시도 채번) / `record_proposed`(승인대기: limit_corrections PROPOSED만·control_limits 불변). commit=호출자, 진입 가드 이중 방어.
- **`initial_limits.compute_group_limits`** 추출 — initial·recalc 산식 공유(회귀 0). `resolution`(정렬 unique 최소 인접 간격) meta 저장 + `load_resolutions`(chamber 제외 4-tuple 키 — H1).
- **config**: `limit_engine.recalc_interval_wafers:500`(A9, 코어 미사용·B5-1) · `recalc_deadband_k_se:2`(A10). `min_group_n`(spc) = 재산정 소표본 가드로 용도 재활용.
- **테스트**: recalc_engine 24(config6+core18) · correction_history 11(순수4+실 DB 7) · recalc_writer 10(가드3+실 DB 7) · 통합 E2E 4. 회귀 191 passed.
- **이월(B5-1)**: 라이브 값 수급 · 무이상 게이팅(호출 조건) · PROPOSED→승인/반려 전이 · fdc.correction 구독 · 섀도 평가.
- **DB 주의**: 라이브 `limit_corrections`에 `delta_sigma` 컬럼 필요(init.sql 정본) — 미sync 시 `ALTER TABLE limit_corrections ADD COLUMN IF NOT EXISTS delta_sigma FLOAT`.

---

## B5-2 — Qual 2지표 (σ-갭 + Nelson N1) *(✅ 완료 2026-07-20)*

정비 후 재인증(Qual)에서 B가 소유하는 2지표를 산출 → A5-3 4지표 취합에 공급. 순수 판정 코어
(스냅샷 주입식, DB·Kafka 없음). 최종 조용/요란 종합은 A5-3, 스냅샷 리베이스는 B6-3. 스펙: `specs/B5-2_qual지표_스펙플랜.md`.

- **`qual_engine.py`** — `evaluate_qual(qual_indicators, snapshot, cfg, *, chamber_id, qual_id, snapshot_id) -> QualBIndicators` + `QualConfig`(LimitConfig 합성). **σ-갭** = 정비 전 스냅샷 대비 계통 이동(2-track 집계: σ=mean/분위수=median A1′·M2, 분모 robust-σ, ≥threshold=FAIL). **N1** = 개별 점이 스냅샷 관리선 밖(Zones 2-track). σ-갭·Nelson이 `evaluable` 집합 공유(빈 pts·sref≤EPS 동일 skip — H2 대칭) · 유효 0건→UNKNOWN(거짓 PASS 금지 M4). **최종 verdict 없음**(A5-3 종합).
- **`qual_snapshot_loader.py`** — `load_active_snapshot(conn, chamber)`: is_active 스냅샷 `sensor_stats` JSONB → `dict[gk]{center,sigma,ucl,lcl,method}`, 다중 활성 시 최신 1개. `normalize_group_key`(step=int 강제) 공유 헬퍼로 로더↔호출자 타입 divergence 구조적 차단.
- **`seed_qual_snapshots.py`** — seg1 전체창 σ 부트스트랩(`compute_group_limits` 재사용·분위수 center=median). `shift_snapshot_row`(center·ucl·lcl 평행이동·밴드폭 보존)·`gk_str`·건강센서 8종(C11·C15·C16·C17·C31·C61·C62·C63, **C12 제외**). 챔버 전개 = chamber_offsets. 멱등(챔버별 is_active 1개).
- **`qual_recorder.py` (B5-2b 라이브 배선)** — 주입식 5조각: `_combine_a_indicators`(A read AE·C65 취합, NULL 규칙)·`_propose_verdict`(**σ-갭+AE 주력만 트리거**, Nelson/C65 기록만, σ-갭 UNKNOWN이라도 AE>max면 FAIL)·`insert_qual`(ON CONFLICT DO NOTHING 멱등)·`update_qual_confirmed`(#4 확정 UPDATE·멱등·미매칭 no-op)·`rebase_snapshot_center`(조용 확정 시 center **평행이동**·σ/밴드폭 보존). commit=호출자.
- **`spc_consumer.py` (B5-2b 편집)** — `_process_wafer` is_qual 분기 → `_handle_qual_wafer`(5장 수집·pm_count 키·partial→proposed NULL)·`_judge_qual`(evaluate_qual+A read → `quals` INSERT, **세션 단위 1행**)·`_confirm_qual`(QualVerdictConfirmed → 확정 UPDATE + 조용 center 리베이스, gateway가 quals 안 닫아 **B가 닫음**). `_summarize_rows` 공유 추출(정상 SPC와 산식 공유·핫패스 무회귀).
- **config·스키마 무변경**: 기존 `qual.pass_sigma_gap_max=3.0`·`pass_nelson_violations_max=0`·`limit_engine.control_limit_k_sigma` 소비. 신규 키 0.
- **테스트**: qual_engine 19(σ-갭13+Nelson6) · qual_snapshot_loader 7(순수3+실 DB4) · seed 9(순수6+실 DB3, seed→load→evaluate e2e 포함). 회귀 236 passed.
- **B5-2b로 해소**: 라이브 배선(is_qual 라우팅·수집·판정·적재) · 스냅샷 리베이스(R5, center 평행이동) · A5-3 취합(B가 `wafer_predictions` read로 대행) · 확정 루프(#4, B가 quals 닫음).
- **이월 잔여**: `qual_min_fill` 강건화 · `qual_snapshots` is_active 부분 유니크 인덱스(PM) · **quals P6-1 컬럼 마이그레이션 `0005_quals_confirmed_verdict.sql`(PM)** · σ-갭 전용 컬럼(발표 후) · 라이브 시딩 chamber_offsets 6→4(PM) 선행.
