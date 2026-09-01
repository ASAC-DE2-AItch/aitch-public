# 제안 — Phase 1 정착 판정의 데이터 기반화 (고정 1,000 → σ 기반 수렴 판정)

| 항목 | 내용 |
|---|---|
| **대상** | PM/PO · 팀원 B(팀원 B) |
| **작성** | 2026-07-25 |
| **관련** | `src/agent_b_spc/provisional_mode.py` (B6-3-a) · `specs/B6-3-b_Phase2재수립_스펙플랜.md` · 합의안 A7 v2 / A10 |
| **성격** | **논의용 제안** — 확정 아님. 실측 전 값·창 길이 미정 |
| **요청 사항** | ① 방향 승인 여부 ② A7 v2(1,000)의 **의미 재해석**(고정 → 상한) 가부 ③ 실측 라운드 착수 승인 |

> **요약 한 줄**: Phase 1 종료(정착) 판정을 wafer 1,000장 고정에서 **"센서 그룹별 center가 자기 표본오차 이내로 멈췄는가"** 로 바꾸자. 새 통계 개념 도입 없이 **이미 확정된 A10 dead band 산식을 시간축으로 재사용**한다. 1,000은 버리지 않고 **상한/폴백**으로 남긴다.

---

## 1. 현행 동작과 문제 제기

### 현행

`provisional_mode.py:219`

```python
elif st["phase"] == Phase.PHASE_1 and st["post_pm_count"] >= st["boundary"]:
    st["phase"] = Phase.AWAITING_PHASE2
    return Phase2Signal(chamber)
```

`boundary = limit_engine.seasoning_exclude_wafers_loud = 1000` (A7 v2, PM 확정).

이 값은 **두 가지 역할을 겸한다**:

| 역할 | 내용 | 틀렸을 때 비용 |
|---|---|---|
| ⓐ 광폭 보호 기간 | Phase 1 provisional(k=5) 밴드 유지 기간 | 길면 **미탐**(회복 가능 — 밴드 해제로 원복) |
| ⓑ A7 seasoning 경계 | Phase 2 재수립 **표본 제외 구간** | 짧으면 과도 데이터가 firm 관리선에 혼입 → **영구 오염** |

### 문제

**1,000은 챔버·PM 규모·요란 강도와 무관하게 상수다.** 회복 시상수는 챔버마다·정비 내용마다 다르다.

- 빨리 회복하는 챔버 → 광폭 밴드를 불필요하게 오래 유지 (미탐 노출 연장)
- 느리게 회복하는 챔버 → **아직 과도 중인 데이터로 firm 관리선을 세움**

⚠️ **두 오류의 비용이 비대칭이다.** ⓐ는 되돌릴 수 있지만, ⓑ로 오염된 관리선은 다음 재산정까지 살아남는다. 따라서 판정은 **보수적(늦게 종료)** 쪽으로 설계해야 한다.

> **참고 — 원래 검토했던 안**: "예측 C65를 나열해 기울기가 작아지는 구간" 으로 판정. §4에서 기각 사유를 정리한다.

---

## 2. 제안

### 2-1. 판정 신호 — 센서 그룹 값 (예측 C65 아님)

정착은 **챔버 상태**의 현상이고, 관리선은 **`group_key`(챔버×센서 C코드×step) 단위**로 세운다. 판정도 같은 단위여야 한다.

### 2-2. 판정 기준 — A10 dead band의 시간축 재사용 ★

**새 임계값을 만들지 않는다.** `recalc_engine.py:155`가 이미 쓰고 있는 산식을 그대로 쓴다.

```python
# 현행 A10 (재산정) — "새 center가 기존 center에서 의미 있게 움직였나"
dead_band = max(cfg.deadband_k_se / math.sqrt(n_used), res_sig)   # SE = 1/√n
if delta1 < dead_band: return no_change(reason="dead_band")
```

**제안 (정착 판정) — "최근 center가 직전 center에서 의미 있게 움직였나"**

```
그룹 g에 대해, 인접한 두 창(길이 W)의 평균 차:
    Δ_g = |mean(최근 W장) − mean(직전 W장)| / σ_ref(g)      # σ 정규화

    두 표본 평균차의 SE(σ 단위) = √(2/W)
    settle_band = max(deadband_k_se · √(2/W), res_σ)        # A10과 동일 형태·동일 키

    그룹 정착  := Δ_g < settle_band  가 M회 연속
    챔버 정착  := 모든 대상 그룹이 정착   ← 가장 느린 그룹이 결정
```

- `deadband_k_se = 2` — **기존 확정 키 재사용**(A10). 신규 키 아님
- `σ_ref` — `recalc_engine.sigma_ref_of()` 그대로 (sigma/quantile 분기 처리됨)
- `res_σ` 하한 — A10과 동일하게 분해능 이하 진동을 흡수
- σ로 정규화하므로 센서 단위(℃·mTorr·V)와 무관하게 **임계 1개**로 통일

### 2-3. 안전장치 — 하한·상한·미수렴 플래그

| 가드 | 값 | 근거 |
|---|---|---|
| **하한** | `settle_min_wafers` ⚠️신규·실측 대기 (후보 300~500) | 조기 오판 방지. 요란 직후 우연한 평탄 구간에서 종료하는 것을 막는다 |
| **상한** | `seasoning_exclude_wafers_loud = 1000` | **현행 값 유지.** 판정이 실패·미수렴해도 자동으로 현행 동작으로 폴백 |
| **미수렴 플래그** | 1,000 도달 시점에 미수렴이면 신호에 `converged=False` | 승인자(R9)와 Supervisor에게 "이 재수립은 과도 혼입 의심"을 알림 |

즉 **정착 시점 ∈ [settle_min_wafers, 1000]**, 미수렴 시 현행과 완전히 동일하게 동작한다.

> ⚠️ **`min_establishment_wafers`(500)는 하한이 아니다** — 초안에서 오독했던 부분. `establish_engine.py:131-133`은 **정착 경계를 넘긴 뒤의 표본 수** 하한이다:
> ```python
> settled = [w for w in buffered_wafers if w.post_pm_count > cfg.seasoning_loud]
> if len(settled) < cfg.min_establishment_wafers: return None
> ```
> 즉 현행 firm 수립 시점은 1,000이 아니라 **≈1,500장**(1,000 제외 + 최근 500 표본)이다. 정착 판정과는 **직교하는 가드**이므로 별도 하한 키가 필요하다.

### 2-4. ★ 필수 연동 변경 — `select_establishment_sample`

`establish_engine.py:131`이 **config 상수 `cfg.seasoning_loud`를 직접** 표본 컷오프로 쓴다.

```python
settled = [w for w in buffered_wafers if w.post_pm_count > cfg.seasoning_loud]
```

정착 시점이 챔버별 동적 값이 되면, 이 컷오프도 **상수가 아니라 "그 챔버가 실제로 정착한 시점"** 이어야 한다. 안 바꾸면 조기 정착해도 표본은 여전히 1,000 이후만 쓰므로 **이득이 0이 되고**, Phase 2 신호와 표본 선택이 불일치한다.

**변경안**: `Phase2Signal`에 `settled_at: int`를 실어 보내고, `select_establishment_sample(buffered, cfg, settled_at)`이 그 값을 컷오프로 쓴다. 기본값을 `cfg.seasoning_loud`로 두면 기존 호출부는 무변경.

> 변경 지점은 **B 소유 파일 2개**(`provisional_mode.py`, `establish_engine.py`) + c 배선 1곳. B 담당자 확인이 필요한 부분.

### 2-5. 상태머신 변경 범위

`ProvisionalMode.on_wafer` 한 곳. `_phase0_center`가 이미 그룹별 러닝 누적을 하고 있으므로, Phase 1용 **그룹별 링 버퍼(2W장)** 를 추가하고 종료 조건만 교체한다.

```python
elif st["phase"] == Phase.PHASE_1:
    self._settle_buf_update(chamber, points)
    if st["post_pm_count"] >= self.cfg.settle_min_wafers and self._is_settled(chamber):
        ...  # 조기 정착 → Phase2Signal(chamber, settled_at=post_pm_count, converged=True)
    elif st["post_pm_count"] >= st["boundary"]:
        ...  # 현행 경로 → Phase2Signal(chamber, settled_at=boundary, converged=False)
```

순수 코어 성질(주입 seam·DB 무의존)은 유지된다. 메모리는 `그룹수 × 2W × float`.

---

## 3. 기대 효과

**1. 느린 챔버의 관리선 오염 방지 — 이게 본 제안의 주된 가치**
현행은 1,000장에서 무조건 종료한다. 실제 회복이 더 느린 케이스를 잡을 수단이 지금은 **없다**. 제안은 최소한 `converged=False`로 **감지·경고**할 수 있게 한다.

**2. 빠른 챔버의 firm 복귀 단축**
현행 firm 수립 시점은 **≈1,500장 ≈ 6.5일** (1,000 제외 + 최근 500 표본, `throughput_wafers_per_day = 231`). 정착이 600장에서 검출되면 400장 ≈ **1.7일** 단축된다. 하한을 300으로 잡으면 이론상 최대 700장 ≈ 3일.
그만큼 광폭(k=5) 밴드 유지 구간 = **미탐 노출 구간**이 줄어든다.

**3. 재시작 복구 지연 완화 (부수 효과)**
`provisional_seams.derive_restore_state`가 `post_pm_count: 0`으로 리셋해 "최대 ~1,000 wafer 재수립 지연"을 감수하고 있다(결정2 ⓓ). 수렴 판정은 **최근 2W창만** 보므로 카운터 리셋에 둔감하다 — 이 지연이 상당 부분 사라진다.

**4. `provisional_k` 실측과 같은 프레임**
"값을 방어하지 않고 검증·조정 체계를 방어" (B6-3-a §2). 1,000이라는 값 대신 판정 체계를 둔다.

---

## 4. 검토했으나 채택하지 않은 안

### 4-1. 예측 C65 기울기 (당초 아이디어)

| 문제 | 내용 |
|---|---|
| **상쇄로 인한 미검출** | 모델은 다수 센서 → 스칼라 1개의 축약 사상이다. 센서 A↑·B↓가 상쇄되면 **센서는 흔들리는데 예측값은 평평** → 오탐지("정착") |
| **최악 구간의 최악 신호** | 요란 PM 직후는 정의상 학습 분포 밖(OOD)이다. 모델 오차가 최대인 구간의 값을 **미분**하면 오차가 증폭된다 |
| **해상도 불일치** | 관리선은 그룹별인데 C65는 스칼라 1개 → 빠른 그룹·느린 그룹을 일괄 판정. 느린 그룹이 오염된다 |
| **순환 참조** | CT①이 `ct1_require_complete_loud_regime: true` — 완결 요란 레짐이 학습창에 관여한다. 그 레짐 완결을 그 모델의 예측으로 판정하면 루프가 닫힌다 |
| **정보 손실** | 센서는 어차피 모델의 입력이다. 센서 → 모델 → 스칼라 → 기울기 는 센서 → 판정 대비 정보가 줄기만 한다 |

### 4-2. 기울기 임계 (신호와 무관하게 기준 자체)

- 평활 창 길이에 따라 정착 시점이 달라진다 (MA-50 vs MA-200)
- 순수 잡음의 기울기도 0이 아니다 → 임계 근처에서 판정이 진동한다
- **느린 드리프트를 통과시킨다.** 첨부 그래프의 꼬리 구간은 실제로 완만히 상승 중인데, 기울기 임계만으로는 "정착"으로 판정된다
- 임계값이 `C65/wafer` 라는 낯선 단위 → 근거 있는 값을 정하기 어렵다 (매직넘버, 헌법 6-1)

§2-2의 σ 대비 방식은 위 4개를 모두 회피한다. (단위 무관 · 잡음 대비 정의 · 창 길이가 SE에 반영 · 기존 확정 키 재사용)

### 4-3. 지수 감쇠 피팅 `y = a + b·exp(−t/τ)`

기각이 아니라 **보류**. 정착 시점 = 3τ~5τ 로 **조기 예측**이 가능한 게 장점이나 —

- 그룹별 피팅 실패·수렴 불량 처리가 필요 (레벨 계단형 이사에는 부적합 — 1차 검증에서 C17은 `level_shifts_eng`로 감쇠가 아니었음)
- 상태머신이 무거워지고 순수 코어 성질을 해친다

→ **2차 개선안**으로 남긴다. §2 채택 시에도 검증 라운드에서 τ를 **참고 지표로만** 산출해 비교한다.

---

## 5. 리스크

| 리스크 | 완화 |
|---|---|
| 조기 종료로 과도 데이터가 firm에 혼입 | `settle_min_wafers` 하한 + M회 연속 조건 + 보수적 `deadband_k_se`. 그리고 Phase 2 재수립은 **항상 승인 필요**(`establish_engine.py:8` — 상한·dead_band·auto 경로 없이 항상 `needs_approval`)라 사람이 최종 게이트 |
| `settled_at` 연동 누락 시 이득 0 + 신호/표본 불일치 | §2-4 변경이 **필수 세트**. 단독 머지 금지 |
| 일부 그룹이 영구 미수렴(고장·준-꺼짐) → 종료 불가 | 상한 1,000이 무조건 종료시킴. 추가로 `activity_min`·`mag_min` 준-꺼짐 그룹은 판정 대상에서 제외 검토 |
| Kafka 누락으로 창이 덜 참 | 창 충전(`W` 미달) 시 판정 유예 — TTTM `tttm_window_min_fill` 패턴 승계 |
| 상태 영속 부담 증가 | 링 버퍼는 **미영속**. 재시작 시 재충전(최대 2W장 지연) 수용 — 결정2 ⓓ와 동일 성격 |
| A7 v2(PM 확정값) 의미 변경 | **값은 바꾸지 않는다.** "고정 경계 → 상한" 재해석만. 미수렴 시 동작은 현행과 바이트 단위로 동일 |

---

## 6. 신규/변경 config

`limit_engine:` 절 (B 소유).

| 키 | 값 | 비고 |
|---|---|---|
| `settle_window_wafers` | ⚠️ 신규·실측 대기 | 판정 창 W. 후보 100~250 |
| `settle_consecutive_m` | ⚠️ 신규·실측 대기 | 연속 만족 횟수 M. 후보 2~3 |
| `settle_min_wafers` | ⚠️ 신규·실측 대기 | 조기 종료 하한. 후보 300~500 |
| `recalc_deadband_k_se` | **2 (기존)** | 재사용 — 신규 아님 |
| `seasoning_exclude_wafers_loud` | **1000 (기존)** | 의미만 "고정 경계 → 상한" |
| `min_establishment_wafers` | **500 (기존, 무변경)** | 정착 이후 표본 하한 — 본 제안과 직교 |

신규 3키는 `provisional_k` 선례(C8 임시 등록)와 동일하게 **기본값 기입 + 실측 대기 주석** 으로 착수한다.

---

## 7. 검증 계획 (승인 시)

**데이터**: 시뮬레이터 요란 시나리오 (`qual_loud_ch4` 등 — B6-3-a 1차 검증 자산 재사용)

**비교군 3종**

1. 고정 1,000 (현행)
2. 제안 (σ 대비 수렴 판정, [500, 1000])
3. 예측 C65 기울기 임계 (기각 근거 실측 확인용)

**측정 지표**

| 지표 | 기준 | 출처 |
|---|---|---|
| 재수립 관리선의 forward 오탐율 | ≤ 5% | `verify_forward_false_alarm_max_pct` |
| 미탐 | **0건** | `shadow_pass_missed_detection_max` (헌법 1-1 예외2 가드와 동일 원칙) |
| 그룹별 정착 시점 분포 | 관측 | 가장 느린 그룹 vs 1,000의 위치 |
| 참고: 지수 피팅 τ | 관측 | §4-3 2차안 판단 재료 |

**판정**: 지표 ①② 를 만족하면서 그룹별 정착 시점이 1,000보다 **유의하게 흩어져 있으면** 채택. 전부 1,000 근처에 몰려 있으면 **현행 유지**가 정답이므로 제안을 철회한다.

**일정**: W7 Scorecard(`provisional_k` 미탐 상한 확정)와 **같은 라운드에 태운다** — 동일 시나리오·동일 지표라 추가 비용이 작다.

---

## 8. 결정 요청

1. **방향 승인** — 정착 판정을 고정 상수에서 데이터 기반으로 전환하는 것에 동의하는가
2. **A7 v2 재해석** — `seasoning_exclude_wafers_loud: 1000` 을 **상한/폴백**으로 재해석 (값 변경 없음, PM 확정 사항이므로 승인 필요)
3. **검증 착수** — §7 라운드를 W7과 함께 진행

미결 시에도 현행 코드는 그대로 동작하므로 **블로킹 사안이 아니다.**

---

### 부록 — 근거 위치

| 주장 | 파일:라인 |
|---|---|
| 현행 종료 조건 | `src/agent_b_spc/provisional_mode.py:219` |
| A10 dead band 산식 | `src/agent_b_spc/recalc_engine.py:155` |
| σ_ref 분기 헬퍼 | `src/agent_b_spc/recalc_engine.py:90` (`sigma_ref_of`) |
| 표본 컷오프가 config 상수 직참조 (§2-4 대상) | `src/agent_b_spc/establish_engine.py:131` |
| 표본 하한 500 (정착 하한 아님) | `src/agent_b_spc/establish_engine.py:132` |
| Phase 2는 항상 승인 필요 | `src/agent_b_spc/establish_engine.py:8` |
| 재시작 시 카운터 리셋 | `src/agent_b_spc/provisional_seams.py:69-85` |
| A7 v2 = PM 확정 | `src/agent_b_spc/specs/B6-3-a_가한계진입_스펙플랜.md:37` |
| config 값 | `config/params.yaml:25-30` |
