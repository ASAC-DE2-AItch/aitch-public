# Nelson Rule 판정 엔진 (B3-1) — 설계 스펙

- **작성**: 팀원 B (팀원 B), 2026-07-09
- **task**: B3-1 (Nelson N1~N8 + Window 단위 집계) — 선행 B2-1 완료
- **DoD**: 유닛테스트 8/8 룰 통과
- **상태**: ✅ **구현 완료** — 원엔진 N1~N8(47 tests) + **조합②(결정 B) 반영**(PR #23, 2026-07-13): method='quantile'→N1 분위수경계·N5~N8 제외 + skip 로그·time_gap config. 72 tests.
- **개정(2026-07-09, 스펙 리뷰 반영)**: N1 level-trigger(기획서 4-3), N3/N4 동점·경계 strict,
  zone-less 룰 관리선 필드, 리셋 ② = 버퍼유지+silent re-baseline(헌법 1-1), time-gap 느슨한 안전망,
  동시성 모델(파티션당 인스턴스·lock-free) 추가. F14 C11 주의·time-gap 근거(231/일·48h).
- **개정 2 (2026-07-09, 2차 리뷰)**: `limit_basis` 필드 예약(가한계 대비), 경계 정정("초과/밖 strict·
  이내 inclusive" → N7/N8 상보), N3 strict tie 지연 인지 노트.
- **개정 3 (2026-07-09, 3차 리뷰)**: time-gap이 PM 구조적 미검출(∴ pm_count-primary 데이터 필연) +
  존치 판단, **F14 논리 정정**(정착 평균은 arc도 PM도 둔감 — 화이트리스트 결정으로 재프레임),
  `update_whitelist` 컨슈머 스레드 적용(lock-free 성립), `pm_count` 유도=상류 계약.
- **개정 4 (2026-07-09, 4차 리뷰)**: N2 center 경계 등가(==center → run 단절, 일관성 잠금), N4 양자화
  발화 0 노트, offending 범위 명확화(N5/N6), time-gap 앵커 >53h. B2-1 피처 재검토(raw C11 vs
  D_VDC_RES)는 README로 이관(스코프 밖).

---

## 1. 목적 & 범위

`fdc.raw` 센서값과 B2-1 초기 실력치(관리선)를 입력으로, Nelson Rule N1~N8을 실시간 판정해
**위반(pre-alert) 목록**을 생성하는 **순수·상태 엔진**. 스트리밍(라이브)에서 wafer 요약점이
하나씩 도착하며 그룹별 버퍼에 쌓이는 구조를 그대로 구현하되, Kafka·DB·alert와는 분리한다.

### 범위 경계 (In / Out)

| In (이 설계) | Out (별도 task) |
|---|---|
| 룰 8개 순수 판정 | fdc.raw 구독·Kafka 배선 (D0-2) |
| 그룹별 점 버퍼·리셋·edge-trigger | `fdc.alert` 발행·Context Score (B4-1) |
| 위반 객체 생성 (spc_violations 매핑) | spc_violations DB 적재 (B3-2) |
| 화이트리스트 필터 (런타임 교체) | 실력치 재산정 (B4-3) |

> 이 분리 덕에 결정 A·B(→ 조합② 확정)·미구현(D0-1·D0-2) 의존성과 무관하게 착수·완료했으며,
> DoD(유닛테스트 8/8)도 Kafka·DB 없이 합성 데이터로 검증된다.

---

## 2. 컴포넌트 & 모듈 구성

```
src/agent_b_spc/
├── nelson_rules.py     ← 순수 룰 8개 + 레지스트리 (상태 없음)
├── nelson_engine.py    ← 상태 엔진: 버퍼·에피소드·리셋·zone·화이트리스트
└── (기존) initial_limits.py · monitoring_whitelist.py
```

| 모듈 | 역할 | 의존 | 상태 |
|---|---|---|---|
| `nelson_rules.py` | `rule_nX(values, zones) -> bool` — 점 시퀀스에 패턴 존재 여부만 판정 | 없음 (순수) | 없음 |
| `nelson_engine.py` | 점 수신 → 그룹별 버퍼·리셋·룰 실행·edge-trigger → 위반 반환 | rules + 관리선 + 화이트리스트 | 그룹별 버퍼·에피소드 |

### 주입되는 3가지 (미정 대비 파라미터화)

- **zones (관리선)**: B2-1 산출 테이블 → 그룹별 `{center, ±1σ, ±2σ, ±3σ}` 경계.
  **결정 A(분위수)** 시 이 경계 계산만 교체 (룰은 경계값만 봄).
- **whitelist**: 감시 대상 111그룹 집합. **런타임 교체 가능** (§4 생명주기).
- **active_rules**: 기본 N1~N8 전체. **결정 B(치우친 센서 룰 범위)** 시 센서별 필터로 주입.

---

## 3. 룰 정의 & 레지스트리

### 룰 함수 시그니처 (전부 순수)

```python
def rule_nX(values: list[float], zones: Zones) -> bool:
    # values: 그룹 최근 점들 (오래된→최신, 최신이 마지막)
    # zones:  {center, s1p, s1m, s2p, s2m, s3p, s3m}
    # 반환:   최신 점에서 끝나는 패턴 존재 시 True. 점 부족하면 False.
```

### 8룰 (헌법 3-1 기준)

| rule_id | 조건 | 윈도우 | 심각도 | zone | 판정 요지 |
|---|---|---|---|---|---|
| N1 | 1점 ±3σ 초과 | 1 | CRITICAL | 3σ | 최신 점이 s3p 위 또는 s3m 아래 |
| N2 | 9점 연속 한쪽 | 9 | WARNING | center | 최근 9점 전부 center 위 OR 전부 아래 |
| N3 | 6점 연속 증/감 | 6 | WARNING | (없음) | 최근 6점 단조 증가 OR 단조 감소 |
| N4 | 14점 증감 반복 | 14 | WARNING | (없음) | 최근 14점 방향 매번 교대(지그재그) |
| N5 | 3점 중 2점 2σ초과(한쪽) | 3 | WARNING | 2σ | 최근 3점 중 ≥2점이 s2p 위(또는 s2m 아래) |
| N6 | 5점 중 4점 1σ초과(한쪽) | 5 | INFO | 1σ | 최근 5점 중 ≥4점이 s1p 위(또는 s1m 아래) |
| N7 | 15점 ±1σ 이내 | 15 | INFO | 1σ | 최근 15점 전부 ±1σ 밴드 안 (층화) |
| N8 | 8점 ±1σ 밖(양쪽) | 8 | WARNING | 1σ | 최근 8점 **전부** 1σ 밖 **+ 위·아래 양쪽 분포** (혼합) |

### 확정된 해석 결정 (테스트로 잠금 — §6)

- **N1 발행 = level-trigger (점별), N2~N8 = edge-trigger (에피소드당 1회)**: **기획서 4-3** 의심 윈도우
  표에서 N1 윈도우는 "해당 wafer 1장"(비소급)이고 disposition은 "wafer별 개별"이다. 3σ 밖 wafer가 연속
  발생 시 edge-trigger는 첫 장만 발행해 나머지가 HOLD·스크랩에서 누락된다. 따라서 **N1은 위반 점마다
  발행**한다. N2~N8은 윈도우가 "추세 시작~현재 소급"이라 1회 alert가 구간 전체를 덮으므로 edge-trigger 유지.
- **N5·N6 무가드**: "최근 3점 중 2점" 조건만 보고, **`values[-1]`이 위험구역이어야 한다는 가드는 넣지 않는다.**
  이유: 가드는 `[2σ밖, 2σ밖, 정상]` 같은 완결-후-회복 excursion을 **미탐**시키며, 이는 우리 도메인의
  "미탐 > 오탐" 원칙과 충돌. "정상 점 귀속" 우려는 위반 객체의 `offending`(§5)으로 해결.
- **N8 = "전부 밖 + 양쪽"**: 카운트 룰 아님(8점 전부 1σ 밖). 한쪽으로만 8점 밖이면 N2 성격이라 False,
  위·아래 양쪽에 분포해야 True (혼합/두 population).
- **동점(equal) 처리**: **N3 = strict 단조** (직전과 동일값이면 run 깨짐), **N4 = delta 0이면 교대 깨짐**.
  우리 데이터의 양자화 센서(-1/-2 등)는 동일값이 흔하므로 strict가 추세 오탐을 억제(의도된 결정).
  ⚠️ 트레이드오프: 진짜 완만한 상승 중 동점 1개가 끼면 run이 리셋돼 **감지가 약간 지연**(non-strict는
  양자화 헛발동으로 더 나쁨). 지연은 완전 미탐이 아니라 **N2(중심선 한쪽)·N6(1σ 밖) 겹침으로 보완**.
- **경계 등가 (σ 경계)**: **"초과/밖" = strict**, **"이내" = inclusive** — N7("이내")과 N8("밖")이 경계에서
  상보(partition)가 되게 한다.
    - N1·N5·N6·N8 (초과/밖): `value > s_p` 또는 `value < s_m` (경계값 자체는 위반 아님)
    - N7 (이내): `s1m ≤ value ≤ s1p` (경계값 포함)
    - → 정확히 1σ에 앉는 값 = **N7 포함 / N6·N8 단절** (공백 없음). 경계 케이스를 테스트로 명시.
- **N2 center 경계 등가**: 위=`value > center`, 아래=`value < center` (strict). **`value == center`는 위도
  아래도 아니므로 9-run 단절**. σ 경계와 동일 원칙. ※ center는 계산된 float라 `==center`는 드물지만
  (N3/N4 tie보다 낮은 빈도), **다른 경계를 다 잠갔으므로 일관성 위해 잠근다**. 테스트로 명시.
- **N4는 양자화 정착 평균에선 사실상 발화 0** (14점 strict 교대가 거의 불가) — 버그 아님, 비양자화 센서의
  hunting/진동 감지용으로 존치. "왜 N4 안 뜨나" 혼란 방지 노트.

### 결정 A·B 대응

- **결정 A(분위수)**: 룰은 `zones` 경계값만 봄 → ±3σ든 분위수든 룰 코드 무변경. `zone` 필드는 표식.
- **결정 B(치우친 센서 룰 범위)**: `RULES` 레지스트리를 센서별 필터 → 룰 함수 그대로.

---

## 4. 엔진 상태 & feed() 흐름

### 점 모델

```python
Point = { group_key, wafer_id, timestamp, value, pm_count, limit_version }
# group_key = (chamber_id, recipe_id, step, sensor_window, sensor_id)
```

### 그룹별 상태

```python
GroupState = {
  buffer: deque(maxlen=MAX_WINDOW),   # MAX_WINDOW = 최장 룰 윈도우 = 15 (N7)
  last_pm_count, last_limit_version, last_timestamp,   # 리셋 판정용
  episodes: {rule_id -> bool},        # 규칙별 "현재 위반 중" (edge-trigger)
}
```

### `feed(point) -> list[Violation]`

```
1. 화이트리스트 체크: group_key ∉ whitelist → 스킵 (빈 목록)
2. 입력 방어:
     - value 결측/NaN·필드 누락 → 점 스킵 + 로그
     - timestamp < last_timestamp (역전/late arrival) → 드롭 + 로그
3. 리셋 판정 → 해당 시 처리:
     ① pm_count 증가 (PM 이벤트)         → buffer·episodes **비움** (주 리셋)
     ③ timestamp − last_timestamp > TIME_GAP_THRESHOLD (연속성 단절) → buffer·episodes **비움** (느슨한 안전망)
     ② limit_version 변경                → **버퍼 유지**, zones 재계산 + 에피소드 **silent re-baseline**
        (경계에서 발행 안 함 — §아래 "리셋 ② 상세")
4. buffer에 value 추가, last_* 갱신
5. zones 조회: 그룹 관리선(center·σ) → {center, ±1/2/3σ}  (limit_version별 캐시)
6. 활성 룰마다 rule_fn(buffer, zones) → bool
7. 발행 판정 (룰 타입별):
     - **N1 (level-trigger)**: 지금 True면 **매번 발행** (위반 점마다 — 기획서 4-3 wafer별 disposition)
     - **N2~N8 (edge-trigger)**: 이전 False & 지금 True → 발행 (offending 포함) /
       이전 True & 지금 False → 해소·재무장 (CLEAR 방출 안 함) / 그 외 무발행
8. 새로 발행된 위반 목록 반환
```

### 공개 인터페이스

```python
class NelsonEngine:
    def __init__(self, limits, whitelist, active_rules=ALL_RULES): ...
    def feed(self, point) -> list[Violation]: ...
    def update_whitelist(self, new_set): ...   # 런타임 교체 — 제외 그룹 GroupState 즉시 삭제
    def reset(self, group_key): ...            # 수동 리셋
```

### 화이트리스트 생명주기

- 엔진이 `self.whitelist` 집합 보유, `feed()`에서 멤버십 체크. **불변 아님.**
- `update_whitelist(new_set)`로 **프로세스 재시작 없이 교체**. **축소 시 제외된 그룹의 `GroupState`를
  즉시 삭제** — stale 버퍼(재추가 시 낡은 점으로 재개) 방지 + 잔류 상태 정리.
- "누가 언제 갱신을 트리거하나"(파일 감시·config 리로드·API)는 **엔진 범위 밖** = 스트리밍·config
  리로드 계층(D0-2·운영)의 컨트롤러 관심사. feed마다의 파일 hot-reload는 성능·부분읽기·시스템 config
  정합성 이유로 현 시점 보류.
- ⚠️ 재시작의 실제 비용은 wafer 데이터 유실이 아니라 **인메모리 버퍼 상태 손실(~15 wafer 감지 공백)**
  — Kafka는 offset에서 재개하므로 데이터 자체는 유실 안 됨. 이는 별도 사안.

### 리셋 ② (limit_version 변경) 상세

정기 리캘리브레이션(헌법 1-1 — 무이상·상한 내 자동)은 **wafer 값 자체는 그대로**고 zones만 이동시킨다.
버퍼를 비우면 매 리캘리브레이션마다 ~15 wafer 감지 공백이 반복되므로 **버퍼를 유지**한다. 단순히 zones만
재계산하면 값 기반 룰(N2 — center 어느 쪽)이 recenter로 **인위적 발동**할 수 있어(옛 center 위 9점이 새 center
아래로 뒤집힘), 다음을 함께 한다:

1. zones 재계산 (새 center·σ)
2. 활성 룰을 현재 버퍼에 재평가 → 에피소드 상태를 결과로 **덮어씀(발행 없이 = silent re-baseline)**

이로써 감지 공백도 recenter 헛발동도 없앤다. 코스트: 버전 경계에서 시작하는 신규 패턴은 흡수될 수 있으나
드문 코너. (pm_count·time-gap은 그대로 버퍼 클리어 — 물리 상태 변화·연속성 단절이므로.)

### 동시성 모델

- 엔진 인스턴스는 **단일 스레드**, 상태는 그룹별 `GroupState`. **파티션당 엔진 인스턴스 1개** — 멀티챔버
  병렬화(일정 A4-1)는 Kafka를 `chamber_id`로 파티셔닝해 컨슈머마다 자기 엔진으로 자기 그룹만 처리한다.
  **공유 가변 상태·락 없음(lock-free).** "점을 순서대로 받는다" 불변식은 인스턴스 단위로 성립.
- **`update_whitelist`도 컨슈머 스레드에서만 적용** — 외부 트리거(config 리로드·API)는 별도 스레드에서
  `self.whitelist`·`GroupState`를 **직접 변경 금지**(feed와 레이스). 대신 **커맨드 큐에 넣어 feed 루프가
  드레인하며 적용**한다. 이래야 lock-free가 실제로 성립.

### 불변식 & 상류 계약

- 엔진은 **점을 시간순으로 받는다** 가정. **상류(Consumer 계층)는 시간순 보장 OR late-arrival 드롭
  정책을 반드시 둔다** (D0-2 설계 시 수반 의무). 엔진은 방어선으로 timestamp 역전만 드롭(§4-2), 재정렬은 안 함.
- **time-gap 리셋은 느슨한 안전망**: 주 리셋은 pm_count·limit_version. `TIME_GAP_THRESHOLD`는 **양성 공백
  보다 충분히 크게** 잡아 정상 pause에 리셋 안 되게 한다. 근거(`사이클_정의_v1.md`): 처리율 **231 wafer/일**,
  **달력 결측 ~2일(48h)은 리셋 무관**(F13). 따라서 **임계 ≫ 48h**. 보정 앵커: **관측 최대 양성 공백 ~53h**
  (실데이터 분석) → **임계 > 53h**. 임계는 실데이터 wafer 간 간격으로 확정.
    - **데이터가 강제하는 결론**: 정비 다운타임(대PM 12~24h·소정비 6~10h) ≤ 24h < 양성 공백 48h < 임계 →
      **time-gap은 구조적으로 PM에 절대 안 걸린다.** ∴ **PM은 오직 pm_count(C33 리셋)로만** 잡힌다
      (pm_count-primary는 취향이 아니라 데이터 필연).
    - **time-gap 존치 판단**: 임계 ≫ 48h라 거의 발동 안 하나, **PM 아닌 다중일 중단(연휴·장기 유휴)**은
      pm_count가 못 잡으므로 — 스트리밍 연속성 보호를 위한 **순수 안전망으로 존치**(발동은 드묾).
- **`pm_count` 유도 책임 = 상류 계약**: 리셋①은 `point.pm_count` 정확 증가에 의존하나 엔진은 이 값을
  **신뢰만** 한다. C33 리셋에서 pm_count를 유도하는 책임은 **상류(시뮬레이터/producer, D0-2 계약)**에 있으며,
  pm_count가 PM에 증가하지 않으면 **리셋① 무음 실패**. producer 계약에 못박는다.
- zones는 **관리선에서만** 나옴 — 룰은 경계값만 보고 σ 계산을 내부에 두지 않음 (결정 A 분리).
- **감시 대상 특성 주의 (F14 — C11 이중 성격)** *(화이트리스트/요약추출 결정, 엔진 아님)*: 우리 pipeline은
  C11을 **정착 window 평균(mean)으로만** 감시한다(`build_wafer_summaries`가 `.mean()` — wafer 최솟값 미추출).
  F14의 강한 신호 **+6.56σ는 wafer 최솟값(점화 스파이크)의 PM 반응**이지 정착 평균이 아니다.
    - **C11 정착-mean Nelson이 잡는 것**: 정착 Vdc **운전 레벨**의 지속/완만한 이상 (N1=한 wafer 정착 평균이
      3σ 밖, N2/N3=레벨 드리프트).
    - **못 잡는 것**: PM 레짐(정착 평균 침묵 0.13σ → C17·AE, F10) **그리고 급성 arc·스파이크**(순간 스파이크라
      정착 평균에 묻힘 — min/과도에 실림, 우리 미추출 → AE 담당).
    - **의도**: C11-min/과도를 안 뽑는 건 일부러 — 뽑으면 **PM 점화 스파이크마다 N1 폭발**. 정착 평균만 두고
      급성/PM은 AE·C17·pm_count로 위임. (버그 아님, Qual 판정식(정착 평균 기준)에도 무영향.)

---

## 5. 위반 객체 & 에러 처리

### Violation (엔진 출력) — `spc_violations` 매핑

```python
Violation = {
  "chamber_id", "recipe_id", "step", "sensor_window", "sensor_id",
  "rule_id", "severity",
  "current_value",                    # 최신 점 값
  "control_limit_upper", "control_limit_lower",
  "limit_version",
  "limit_basis",                      # "firm"(기본) — 가한계 대비 예약 (아래 주석)
  "description",                      # 예: "N3: 6점 연속 증가 (C64_1..C64_6)"
  "wafer_id", "timestamp",            # 발행 유발 점
  "offending": [ {"wafer_id", "value"} ... ],   # 기여 점 — 귀속 애매한 N5/N6에 채움
                                                #  (N1은 트리비얼=current_value, N2/N3/N4는 description이 run 서술)
}
```

- **timestamp 포맷**: **ISO 8601 UTC ("Z")**, `fdc.raw`에서 passthrough (계약 `fdc.raw`/`fdc.alert`와 정렬).
  엔진은 timestamp를 생성하지 않고 유발 점의 값을 보존.
- **zone-less 룰(N3/N4)의 `control_limit_upper/lower`**: **그룹의 UCL/LCL(center±3σ)을 채운다**(null 아님).
  관리선은 룰이 아니라 **그룹의 속성**이므로 어느 룰이 걸리든 그 그룹 관리선을 담아 위반 레코드를 자기완결적으로.
- **표시 계층 주의**: N5/N6 무가드로 `current_value`가 정상점일 수 있으므로, 대시보드(PM S2)는 `current_value`
  만이 아니라 **`offending` 점을 함께 노출**해야 귀속 오해가 없다.
- **`limit_basis` 필드 예약 (가한계 대비)**: 현재는 **항상 `"firm"`**. `사이클_정의`의 가한계(provisional
  limit — 요란 PM 후 Phase 1 광폭/보류, 신규결정1·R2·M1)가 **W5 요란 PM task**에 붙으면, 관리선 레코드의
  provisional 플래그를 passthrough해 `"provisional"`을 채운다. 지금 필드 자리를 예약해 위반 객체 모양을
  안정화(그때 값만 바뀜). ※ spc_violations DB 컬럼 추가는 그 시점 별도 PM 변경(엔진 내부 필드와 분리).

### 하류가 채우는 필드 (엔진이 안 채움)

| spc_violations 컬럼 | 담당 |
|---|---|
| `alert_id` (NOT NULL) | B4-1 (alert 발행 시) |
| `incident_id` | PM (Incident 그룹핑) |
| `context_score` | B4-1 (Context Score) |
| `created_at` | B3-2 (적재 시) |

> `offending`은 spc_violations 전용 컬럼이 없어 **B3-2가 `description`에 렌더링** (전용 컬럼 추가는 PM 협의).

### 에러 처리 (헌법 6-2 정신 — 스킵 + 로그, 무크래시)

| 상황 | 처리 |
|---|---|
| 점 결측/NaN·필드 누락 | 점 스킵 + 로그 |
| timestamp 역전(late arrival) | 드롭 + 로그 |
| 화이트리스트 그룹인데 관리선 없음 | 그룹 스킵 + 에러 로그 (반쪽 감시 안 함) |
| 버퍼 < 룰 윈도우 | 룰 False (정상, 무로그) |
| σ=0 (화이트리스트가 이미 제외하나 방어) | zone 룰 스킵 + 로그 |
| 생성 시(`__init__`) | 화이트리스트 각 그룹 관리선 존재 점검 → 누락분 경고 로그 |

---

## 6. 테스트 전략

DoD "유닛테스트 8/8"이자 설계 결정의 잠금장치. 전부 **합성 리스트로 구동** (Kafka·DB 불필요).

### 계층 1 — 룰 유닛테스트 (순수)

| 룰 | 통과 | 비통과 | 결정 잠금 |
|---|---|---|---|
| N1 | 3σ 밖 1점 | 3σ 안 / **정확히 경계값 → False**(strict) | 경계 strict 잠금 |
| N2 | 9점 한쪽 | 8점 / 중간 반대쪽 1점 / **중간 1점 ==center → False** | center 경계 잠금 |
| N3 | 6점 단조↑ | 평평/반전 / **중간 동일값 → False** | strict 단조 잠금 |
| N4 | 14점 교대 | 한 번 안 교대 / **delta=0 → False** | delta≠0 교대 잠금 |
| N5 | 3점 중 2점 2σ밖 | 1점만 | **`[2σ밖,2σ밖,정상]`→True** (무가드 잠금) |
| N6 | 5점 중 4점 1σ밖 | 3점만 | `[1σ밖×4,정상]`→True |
| N7 | 15점 1σ 안 | 1점 밖 | **정확히 1σ 값 → 포함(inclusive)** |
| N8 | 8점 밖 **양쪽** | 8점 밖 **한쪽만**→False / **정확히 1σ 값 → 미포함(strict)** | 양쪽 + N7 상보 잠금 |

### 계층 2 — 엔진 상태 테스트

| 테스트 | 검증 |
|---|---|
| edge-trigger (N2~N8) | 지속 위반 → 1회만 발행 / 해소 시 clear / 재무장 후 재발행 |
| **N1 level-trigger** | **3σ 밖 연속 3장 → 3건 모두 발행** (기획서 4-3 wafer별 disposition) |
| 리셋 ① PM | pm_count 증가 → 버퍼 비움 |
| **리셋 ② limit_version** | 버전 변경 → **버퍼 유지 + zones 재계산 + silent re-baseline** (감지 공백 없음, recenter 헛발동 없음) |
| 리셋 ③ time-gap | Δt > 임계 → 리셋 (양성 공백 20~53h엔 리셋 안 됨) |
| **zone-less 관리선 필드** | N3/N4 위반 객체에 그룹 UCL/LCL 채워짐 |
| 화이트리스트 교체 | 비대상 무처리 / `update_whitelist` 런타임 반영 |
| **화이트리스트 축소** | 제외 그룹 `GroupState` **삭제** → 재추가 시 fresh + 잔류 방지 |
| **버퍼 경계** | 16점째 유입 시 1점 소멸, **N7이 최근 15점으로 정확 판정** (maxlen ≥ 최장 룰 불변식) |
| **N5/N6 해소 핀포인트** | 위반 개수 2→1 떨어지는 찰나 episodes=False 재무장 |
| timestamp 역전 | 드롭 + 로그, 버퍼 불변 |
| 결측·NaN 점 | 스킵 + 로그 |
| 관리선 없는 그룹 | 스킵 + 로그 |
| 버퍼 부족 | 무발행·무크래시 |
| 위반 객체 | 필드 매핑 정확 / N5·N6 offending 채워짐 / **`limit_basis` 기본 "firm"** |
| **1σ 경계 상보** | 정확히 1σ 값 → **N7 포함 & N8 미포함** (경계 partition) |

- `pytest` 기반, `src/agent_b_spc/tests/`. 시퀀스 빌더 헬퍼 하나.
- 테스트 = 실행 가능한 설계 문서 (무가드·N8양쪽·edge-trigger·3리셋·버퍼경계 고정).

---

## 7. 미결·후속 (이 설계 밖)

- **결정 A·B → ✅ 조합② 확정(2026-07-11)**: KEEP\* 그룹은 zones를 **분위수**로 주입(`q_low`/`q_high`),
  `active_rules`에서 **N5~N8 제외**(N1은 분위수 경계). 일반 그룹은 ±3σ+전체룰 유지 → **2-track**.
  엔진은 zones 주입·룰 필터로 대비돼 있어 **국소 반영**(재작성 불필요). 상세: `회의안건_요약_B.md`.
- **신규 config 파라미터**: `TIME_GAP_THRESHOLD`(리셋 ③) — 시뮬 시간 도메인 주의. B2-1의 QA·화이트리스트
  파라미터와 함께 `config/params.yaml` 등재 PM 제안 (헌법 6-1).
- **CLEAR 이벤트**: 방출 안 함 (Incident 종료는 승인·verifying 기반이지 SPC-clear 기반 아님). 향후 복구
  신호 소비자가 생기면 위반 목록과 분리된 별도 이벤트로 D0-1·PM lifecycle과 함께 설계.
- **하류 연동**: D0-2(Kafka 배선)·B3-2(spc_violations 적재)·B4-1(fdc.alert 발행·Context Score).
