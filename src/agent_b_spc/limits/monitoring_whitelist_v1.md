# 모니터링 화이트리스트 v1

- 생성: 2026-07-14T02:18:18.875896+00:00
- 총 69그룹 → **감시(KEEP/KEEP\*) 45** / 제외(EXCLUDE?/EXCLUDE) 24

> **소비 규칙**: Nelson 엔진(B3-1)은 `KEEP`·`KEEP*`만 감시한다. `EXCLUDE`·`EXCLUDE?`는 감시하지 않는다.

## 결정 기록

- **EXCLUDE?(C11 비플라즈마 step 1·5·6·7, 8그룹) → 제외 확정** (B, 2026-07-07). 사유: Vdc는 플라즈마 ON에서만 유효 — 비플라즈마 step은 −1~−2 양자화라 ±3σ 무효, 자기 표본 오탐 4~6%(step1·7)가 `fdc.alert`·Incident를 오염. 사각지대(예상치 못한 점화)는 아래 '향후 감시 조건'으로 별도 대비.
- EXCLUDE(상수) → 자동 제외 확정.
- **D_VDC_RES(C6_0 정착 5그룹) → 감시 제외** (B, F14, 2026-07-10). 자동판정은 KEEP(active_normal)이나, 알람 가치 없음이 확인됨: step4에서 C11보다 열등, step5·6·7 corr(C65) 0.78은 챔버 노후 시간추세 교란(단일레짐 +0.13·차분 −0.03). **계산(initial_limits)은 보존** — 향후 PM 타이밍/실력치 재캘리브 드리프트 트리거(B4-3/B5-1) 후보로 백로그. 상세: `analysis/F14_feature_review.md`.

## 판정별 그룹 수

| 판정 | 뜻 | 그룹 수 |
|---|---|---|
| KEEP | 활성·정상 → 감시 | 39 |
| KEEP* | 활성·비정규 → 감시하되 보완② | 6 |
| EXCLUDE? | 준-꺼짐 → 제외 제안(검토) | 4 |
| EXCLUDE | 상수 → 제외 확정 | 20 |

## KEEP (39그룹)

| recipe_id   |   step | sensor_window   | sensor_id   |   n_distinct |      range |   activity_ratio |   false_alarm_pct | decision_reason   |
|:------------|-------:|:----------------|:------------|-------------:|-----------:|-----------------:|------------------:|:------------------|
| C6_0        |      4 | settled         | C11         |          181 |  135       |           1      |               0   | active_normal     |
| C6_0        |      1 | settled         | C15         |            5 |   11.5     |           0.1394 |               0   | active_normal     |
| C6_0        |      4 | settled         | C15         |            8 |   82.5     |           1      |               0   | active_normal     |
| C6_0        |      1 | settled         | C16         |            7 |  117.5     |           0.1722 |               0   | active_normal     |
| C6_0        |      4 | settled         | C16         |           24 |  682.5     |           1      |               0   | active_normal     |
| C6_0        |      5 | settled         | C16         |            3 |   23       |           0.0337 |               0   | active_normal     |
| C6_0        |      1 | settled         | C17         |          150 |   81.3333  |           1      |               1.4 | active_normal     |
| C6_0        |      4 | settled         | C17         |           15 |    7.5     |           0.0922 |               0   | active_normal     |
| C6_0        |      5 | settled         | C17         |           12 |    9       |           0.1107 |               0   | active_normal     |
| C6_0        |      6 | settled         | C17         |           71 |   45       |           0.5533 |               0   | active_normal     |
| C6_0        |      7 | settled         | C17         |           50 |   23       |           0.2828 |               0   | active_normal     |
| C6_0        |      4 | transient       | C18         |          379 |    6.84333 |           1      |               0.2 | active_normal     |
| C6_0        |      4 | transient       | C27         |           82 |  119.333   |           1      |               0   | active_normal     |
| C6_0        |      4 | settled         | C31         |          160 |  109       |           1      |               0   | active_normal     |
| C6_0        |      4 | settled         | C32         |            7 |    1.5     |           0.25   |               0.6 | active_normal     |
| C6_0        |      5 | settled         | C32         |            5 |    6       |           1      |               1.2 | active_normal     |
| C6_0        |      4 | transient       | C54         |           41 |  411.667   |           1      |               0   | active_normal     |
| C6_0        |      4 | transient       | C56         |           46 |  469.333   |           1      |               0   | active_normal     |
| C6_0        |      1 | settled         | C58         |            8 |    0.02    |           0.0494 |               0   | active_normal     |
| C6_0        |      5 | settled         | C58         |           14 |    0.12    |           0.2963 |               0   | active_normal     |
| C6_0        |      6 | settled         | C58         |            8 |    0.025   |           0.0617 |               0.2 | active_normal     |
| C6_0        |      7 | settled         | C58         |            5 |    0.02    |           0.0494 |               0   | active_normal     |
| C6_0        |      4 | settled         | C61         |          193 |  127       |           0.1698 |               0.2 | active_normal     |
| C6_0        |      5 | settled         | C61         |          307 |  748       |           1      |               0   | active_normal     |
| C6_0        |      1 | settled         | C62         |           71 |   20.6667  |           0.0038 |               2   | active_normal     |
| C6_0        |      4 | settled         | C62         |          104 |   56.5     |           0.0105 |               0.6 | active_normal     |
| C6_0        |      5 | settled         | C62         |          342 | 5371       |           1      |               0   | active_normal     |
| C6_0        |      6 | settled         | C62         |           47 |   24.5     |           0.0046 |               1.6 | active_normal     |
| C6_0        |      7 | settled         | C62         |           43 |   20       |           0.0037 |               1.2 | active_normal     |
| C6_0        |      1 | settled         | C63         |          137 |  290.5     |           1      |               0   | active_normal     |
| C6_0        |      4 | settled         | C63         |           23 |    9.5     |           0.0327 |               0   | active_normal     |
| C6_0        |      5 | settled         | C63         |           10 |   10       |           0.0344 |               0   | active_normal     |
| C6_0        |      6 | settled         | C63         |            2 |  100       |           0.3442 |               0   | active_normal     |
| C6_0        |      7 | settled         | C63         |            2 |  200       |           0.6885 |               0   | active_normal     |
| C6_0        |      1 | settled         | C9          |          411 |   26.5855  |           1      |               0   | active_normal     |
| C6_0        |      4 | settled         | C9          |          134 |    3.4055  |           0.1281 |               0   | active_normal     |
| C6_0        |      5 | settled         | C9          |          106 |    7.311   |           0.275  |               0   | active_normal     |
| C6_0        |      6 | settled         | C9          |          228 |   13.84    |           0.5206 |               0   | active_normal     |
| C6_0        |      7 | settled         | C9          |          274 |   10.184   |           0.3831 |               0   | active_normal     |

## KEEP* (6그룹)

| recipe_id   |   step | sensor_window   | sensor_id   |   n_distinct |   range |   activity_ratio |   false_alarm_pct | decision_reason                             |
|:------------|-------:|:----------------|:------------|-------------:|--------:|-----------------:|------------------:|:--------------------------------------------|
| C6_0        |      5 | settled         | C31         |            8 |   8     |           0.0734 |               5.2 | active_nonnormal(→보완② 관리선 방식 재검토) |
| C6_0        |      4 | settled         | C57         |            8 |   3     |           1      |               5.2 | active_nonnormal(→보완② 관리선 방식 재검토) |
| C6_0        |      4 | settled         | C58         |           69 |   0.405 |           1      |               5.4 | active_nonnormal(→보완② 관리선 방식 재검토) |
| C6_0        |      1 | settled         | C61         |           57 |  19.5   |           0.0261 |               3.6 | active_nonnormal(→보완② 관리선 방식 재검토) |
| C6_0        |      6 | settled         | C61         |          103 | 105.5   |           0.141  |               4.2 | active_nonnormal(→보완② 관리선 방식 재검토) |
| C6_0        |      7 | settled         | C61         |           40 |  19.5   |           0.0261 |               3   | active_nonnormal(→보완② 관리선 방식 재검토) |

## EXCLUDE? (4그룹)

| recipe_id   |   step | sensor_window   | sensor_id   |   n_distinct |   range |   activity_ratio |   false_alarm_pct | decision_reason                                     |
|:------------|-------:|:----------------|:------------|-------------:|--------:|-----------------:|------------------:|:----------------------------------------------------|
| C6_0        |      1 | settled         | C11         |            5 |     0.5 |           0.0037 |               5.8 | quasi_off(활성도 0.4%·크기 0.5% — 해당 step 비활성) |
| C6_0        |      5 | settled         | C11         |            3 |     1   |           0.0074 |               0.8 | quasi_off(활성도 0.7%·크기 0.6% — 해당 step 비활성) |
| C6_0        |      6 | settled         | C11         |            3 |     1   |           0.0074 |               0   | quasi_off(활성도 0.7%·크기 0.5% — 해당 step 비활성) |
| C6_0        |      7 | settled         | C11         |            3 |     1   |           0.0074 |               4.2 | quasi_off(활성도 0.7%·크기 0.5% — 해당 step 비활성) |

## EXCLUDE (20그룹)

| recipe_id   |   step | sensor_window   | sensor_id   |   n_distinct |   range |   activity_ratio |   false_alarm_pct | decision_reason                                                 |
|:------------|-------:|:----------------|:------------|-------------:|--------:|-----------------:|------------------:|:----------------------------------------------------------------|
| C6_0        |      5 | settled         | C15         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      6 | settled         | C15         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      7 | settled         | C15         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      6 | settled         | C16         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      7 | settled         | C16         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      1 | settled         | C31         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      6 | settled         | C31         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      7 | settled         | C31         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      1 | settled         | C32         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      6 | settled         | C32         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      7 | settled         | C32         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      1 | settled         | C57         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      5 | settled         | C57         |            2 |     0   |           0      |               0.2 | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      6 | settled         | C57         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      7 | settled         | C57         |            1 |     0   |           0      |               0   | dead(상수 — 물리적 OFF)                                         |
| C6_0        |      1 | settled         | D_VDC_RES   |           24 |     6   |           0.0446 |               0.4 | F14_drift_dominated_no_alarm_value(→PM/recalib_trigger_backlog) |
| C6_0        |      4 | settled         | D_VDC_RES   |          201 |   134.5 |           1      |               0   | F14_drift_dominated_no_alarm_value(→PM/recalib_trigger_backlog) |
| C6_0        |      5 | settled         | D_VDC_RES   |           11 |     9   |           0.0669 |               0.4 | F14_drift_dominated_no_alarm_value(→PM/recalib_trigger_backlog) |
| C6_0        |      6 | settled         | D_VDC_RES   |           15 |     6   |           0.0446 |               0   | F14_drift_dominated_no_alarm_value(→PM/recalib_trigger_backlog) |
| C6_0        |      7 | settled         | D_VDC_RES   |           16 |     6   |           0.0446 |               0   | F14_drift_dominated_no_alarm_value(→PM/recalib_trigger_backlog) |

## 향후 감시 조건 (제외 그룹 사각지대 대비)

C11 비플라즈마 step 제외로 놓치는 상황은 **±3σ가 아닌 별도 규칙**으로 대비한다. 아래는
양자화 노이즈에 안 걸리는 **고정 임계·교차검증** 방식이라 오탐 스팸 없이 실질 위험만 잡는다.
**추가 시점**: 시연 시나리오에 비플라즈마 step 이상이 포함되거나, 운영 중 실제 미탐이 확인될 때.
**추가 위치**: Nelson 엔진(B3-1)의 별도 규칙 계층 (실력치 ±3σ 관리선과 분리).

| 우선 | 감시 조건 | 규칙(±3σ 아님) | 근거 | 심각도 |
|---|---|---|---|---|
| 1 | 예상치 못한 플라즈마 점화 | 비플라즈마 step에서 `C11 < THRESHOLD`(예: −50V) | idle ~−1 vs plasma ~−221 — 중간값이면 이상 점화 | High |
| 2 | RF 교차검증 (모순 감지) | `C1`(RF power)≈0 인데 `\|C11\|` 큼 → 모순 | 플라즈마는 RF와 동행해야 함 (데이터 사전: C11=RF와 −0.978) | High |
| 3 | idle 바닥값 오프셋 이동 | idle `C11`이 고정밴드(예: −3~0) 밖 | 센서/오프셋 결함 | Low |

- **미정 파라미터**(추가 시 데이터 기반 튜닝 + config 확정): 점화 임계값(−50V 등),
  RF-off 판정 기준, idle 고정밴드. 전부 `config/params.yaml` 대상.
- **주의**: 위 조건은 현재 EXCLUDE? = C11 전제. 향후 규칙 변경으로 EXCLUDE? 센서 구성이
  바뀌면 이 표도 재작성할 것.
