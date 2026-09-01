# TTTM 이탈 판정 · 로직 정리 (v1)

> **생성**: 2026-07-23 · 담당 B(팀원 B) · B4-1 워킹 레퍼런스
> **소스**: `src/agent_b_spc/tttm_engine.py` + `config/params.yaml` + `docs/API_Contract_명세서.md` §3
> **한 줄**: Nelson이 *자기 시간축*을 본다면 TTTM은 *형제 챔버 공간축*을 본다. 감시 그룹·관리선·값은 Nelson과 100% 공유.

---

## 0. 감시 대상 = Nelson과 동일 ✅

- TTTM은 Nelson과 **같은 `(limits, whitelist)`** 를 `control_limits_loader`에서 주입받는다 — 감시 그룹 집합(**45개**: 평균 39 + 분위수 6), 관리선 값, 입력 `Point`(wafer 대표값) 전부 공유.

---

## 1. 두 개의 대표값 (2단계 median)

장비 1대 = 4챔버가 같은 레시피를 도니까 "챔버끼리 비교". 값에서 두 층의 median을 뽑는다.

### ① `center_live` — 내 챔버의 현재 위치 (시간축 median)

각 (챔버 × 레시피 × step × window × 센서) 그룹마다 최근 20 wafer의 median.

```
center_live = median(최근 window 20장)      # tttm_window_wafers = 20
```

- 최소 10장(`tttm_window_min_fill`) 차야 판정 (미충전 유예).
- 평균이 아니라 **median** → 튄 wafer 1장에 안 흔들림.
- 레짐 flush: PM count 증가·limit_version 변경 시 window 비움 (새 운전점 오염 차단).

### ② `fleet_median` — 형제 무리의 중앙 (챔버축 median)

같은 (레시피·step·window·센서, **챔버 뺀** 키)에서 형제 챔버들의 center_live를 모아 median.

```
fleet_median = median(형제 챔버들의 center_live)
```

- 형제 ≥ 2챔버(`MIN_FLEET_CHAMBERS`) 있어야 비교. "정상은 이 근처"라는 **라이브 기준선**.
- **center_live가 fleet_median의 재료** — 챔버별 center_live들을 다시 median.

```
CH1 최근20장 → median → center_live₁ = -221 ┐
CH2 최근20장 → median → center_live₂ = -223 ├→ median = fleet_median = -222
CH3 최근20장 → median → center_live₃ = -220 │
CH4 최근20장 → median → center_live₄ = -298 ┘
```

---

## 2. 이탈 스코어 — fleet 대비 몇 σ

```
score  = |(center_live − fleet_median) / σ_ref|
σ_ref  = (UCL − LCL) / (2·k)      # 관리선 폭에서 역산, k=3 → σ 복원
```

- 이 챔버가 **형제 중앙값에서 몇 σ** 벗어났나. σ_ref 정규화로 센서 스케일 무관.
- 분위수 그룹도 밴드폭에서 σ 등가 역산 (q_high의 Φ⁻¹ = k 정합 검증, `_check_q_k`).
- **챔버 롤업**: 한 챔버의 전 센서 중 `|score|` 최악 **1건만** 대표로 → `fdc.alert`의 tttm 객체.

---

## 3. 이탈 판정 기준 (severity)

| score (gap_σ 절대값) | 판정 |
|---|---|
| ≥ `tttm_warning` **2.0** | WARNING |
| ≥ `tttm_critical` **3.0** | CRITICAL |

---

## 4. 역방향 룰 (`reference_suspect`) — 기준 오염 판정

**다른 질문을 다른 앵커로** 본다:

- `score` = 라이브 **fleet_median** 대비 (이 챔버가 형제와 다른가)
- 역방향 = 각 챔버 자기 고정 **baseline(center)** 대비 (다수가 같이 움직였나)

```
각 챔버 z = (center_live − 자기 baseline center) / σ_ref
z > 2.0  → up 카운트 / z < -2.0 → down 카운트
max(up, down) ≥ reverse_min(=3, 4챔버 중 3/4 과반) → reference_suspect = true
```

- **왜 필요**: 참조/유틸리티 공통원인으로 4챔버가 같이 움직이면 fleet_median도 따라가 개별 score가 안 벌어짐(놓침). 근데 각자 자기 baseline 대비론 다 이탈 → "다수 동시 이탈 = 기준 오염".
- `reference_suspect=true` → Context Score **감점 방향** (이 챔버 개별 고장 아님, 참조를 의심).
- **래칭 방지**: 조건 풀리면 즉시 `discard` (드리프트 해소 후 영구 suspect 방지).

---

## 5. 최종 산출물 (tttm 객체 → `fdc.alert` §3)

```json
"tttm": {
  "reference": "fleet_median",   // 비교 기준 = 형제 챔버 center_live들의 median (라이브 잣대)
  "score": 2.13,                 // 최악 센서의 |gap_σ| — fleet_median에서 몇 σ 벗어났나 (2.0 WARN / 3.0 CRIT)
  "top_gap_sensor": "C11",       // |gap_σ| 최악 센서 1건 (C코드) — 챔버 롤업 대표
  "gap_pct": 34.2,               // 표시용 signed % 갭 = (center_live − fleet_median)/|fleet_median|×100 (점수엔 미소비)
  "reference_suspect": false     // 역방향 룰 결과 — 다수(3/4) 동시 이탈 시 true = 기준 오염 의심 → 감점
}
```

- 계약 §3의 tttm 필드(`reference`·`score`·`top_gap_sensor`·`gap_pct`·`reference_suspect`)만 alert에 실림 (chamber_id·reference_id는 DB 전용).
- B4-1 Context Score에서 **Nelson·AE와 독립된 수렴 축**: score ≥ 2.0 가점 / `reference_suspect=true` 감점.

---

## 6. 세 값 비교 (헷갈림 방지)

| 값 | 시점 | 산출 | 움직임 | 역할 |
|---|---|---|---|---|
| `center` (baseline) | 과거 정착 표본 | 최근 500장 트림 평균 (rolling mean) | 고정(버전당) | 역방향 룰 앵커 |
| `center_live` | 지금 | 최근 20장 median | 라이브 | 비교 대상(피검자) |
| `fleet_median` | 지금 | 형제 center_live들의 median | 라이브 | score 잣대 |

- **score**의 잣대 = `fleet_median` (라이브)
- **역방향 룰**의 잣대 = `center` (고정 baseline)
