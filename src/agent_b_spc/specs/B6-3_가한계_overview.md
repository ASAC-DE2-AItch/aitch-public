# B6-3 가한계(Provisional) 3단계 — Overview (공유 모델)

> **역할**: a/b/c 스펙이 공유하는 **모델·계약·경계**의 단일 소스. 각 조각 스펙은 이 문서를 참조하고 자기 결정·구현만 담는다.
> **작성**: 팀원 B(B) · 2026-07-22 · **의존**: B5-1a·B5-2 완료 · **마감**: 7/29 핵심(a) / 7/31 완성(b)
> **갱신 2026-07-22 (a 스펙 확정·서브에이전트+계약 대조 반영)**: verdict enum `loud`/`quiet` 명문화 · 광폭 **sigma 인코딩**(§2) · `set_excluded` union·write version bump(§4) · new_center **B 자체산출**(§5) · 영속 스토어 **c 이월**(§6·§7).

---

## 1. 무엇을·왜

**가한계 3단계 상태머신** — 요란 PM(레짐 전환)으로 챔버 정상이 이사가면, 관리선을 **"억제 → 가한계 광폭 → 정식 재산정"** 으로 단계적으로 갈아타게 한다.

**왜**: 옛 관리선 그대로면 이후 전 wafer 위반 → **알람 폭주(~231건/일)**. 그렇다고 즉시 재산정도 못 함(정착·정당성 확인 전). → **수명주기**로 푼다. (사이클_정의 원천 ④)

---

## 2. 3단계 모델 + 전이

```
PM(pm_count↑) → [Phase 0 억제] → Qual 판정(B5-2)
                                   ├ 조용 → seasoning 10장 제외 → 정상 복귀 (provisional 미진입)
                                   └ 요란 → [Phase 1 광폭] ──정착 1,000장──→ [Phase 2 재산정] → 정상(firm)
```

| 단계 | 언제 | 동작 |
|---|---|---|
| **Phase 0** | 개방~Qual | 알람 억제 (새 정상 모름) |
| **Phase 1** | 요란 후~정착 | 중심만 이동 · 광폭 4~5σ · `provisional` 플래그 · 추세 룰(광폭이라 σ-존 침묵) · **정기 리캘리 OFF**(R2) · TTTM 제외 · pred 가중 0 |
| **Phase 2** | 정착 1,000장 후 | 정식 재산정(신규수립 모드) · 스냅샷 리베이스(R5) · Y-임계 갱신 · `provisional→firm` |

**핵심 원칙**: 후속은 **작업 유형이 아니라 판정(조용/요란)에 건다** (사이클_정의). "대PM인데 조용"이면 소정비처럼 취급 = 올바름.

> **광폭 인코딩 주의 (a 확정)**: "광폭"을 ucl/lcl에만 넣으면 **안 됨** — Nelson은 sigma 그룹서 `from_limit(center,sigma)`로 판정(ucl/lcl 무시, `nelson_rules.py:37`). 따라서 **sigma 그룹 → `sigma` 컬럼 확대(`provisional_k/3·σ_ref`)**, **분위수 그룹 → `ucl·lcl` 확대**로 "Nelson이 실제 읽는 필드"에 인코딩한다. 두 경로 판정 밴드 = `center ± provisional_k·σ_ref`. `method`는 원본 유지(Phase 2 재산정이 진짜 method 필요). 상세 = a 스펙 §3.

---

## 3. A7 — Seasoning(과도) 제외

PM 직후 **과도(transient) 구간**(실측 요란 ~1,000장 지수감쇠)은 대표성 없어 **통계·재산정에서 제외**한다.
- **조용 10장 / 요란 1,000장** (판정 차등 — config 합의안 A7). 조용은 정상 990장을 안 버리려 10장만.
- **요란 1,000 = Phase 1 기간 = 과도 제외 창** (같은 카운터·같은 숫자).

---

## 4. 이미 준비된 seam (B6-3이 호출/채움)

| seam | 위치 | 용도 |
|---|---|---|
| `set_excluded(chamber_ids)` | `tttm_engine.py:124` (*"B6-3 seam"*) | 가한계 챔버 fleet median·score 제외 · **⚠️집합 통째 교체 → provisional 챔버 union 전달**(add 아님, 다중 챔버 clobber 방지 — a §3) |
| `limit_basis` 필드 | `nelson_engine.py:234` (현 `'firm'` 예약) | provisional/firm 표시 |
| `trigger_type='provisional'` | `db/init.sql:187` (예약) | provisional 관리선 적재 |
| Qual 스냅샷 리베이스 | `qual_engine.py:8` (*"리베이스는 B6-3"*) | Phase 2 R5 |
| `reload_limits` | B5-1a seam | 관리선 변경 엔진 반영 · **provisional write 시 `limit_version` bump 필수**(안 올리면 Nelson/TTTM **_rebaseline(zones 재계산)** 미발동 → reload 무효·새 밴드 미반영. 버퍼는 유지 — clear는 pm_count↑·time-gap에서만, a §3) |
| `recalc_engine.recompute_group` | B4-3 | 재산정 산식(단, 신규수립 모드는 B6-3 추가) |
| `sigma_ref_of(row, k)` | `recalc_engine.py:90` | robust-σ 헬퍼 — 광폭 σ_ref(sigma/quantile 분기). q_ucl/q_lcl 인라인 금지(loader에 없음, a §3) |
| provisional write payload | `recalc_writer.apply_auto` 패턴 | version bump→활성행 deactivate→INSERT(필수컬럼 전체) — a §3 |

---

## 5. 크로스파트 계약 (근거)

| 계약 | 근거 |
|---|---|
| `QualVerdictConfirmed`(`fdc.agent`) → Phase 전환 · **verdict enum `"loud"`/`"quiet"`**(조용/요란 라벨 아님) · **엔지니어 승인 하류**(1-1 정합) | **API_Contract §8-B** (P4-6 확정) · `consumer-group-spc` 소비 |
| **new_center = B 자체산출** (이벤트에 center 필드 **없음** → B가 Phase 0 새-레짐 wafer로 러닝 중심 산출) | **API_Contract §8-B** payload 확인 (a §3, Review G) |
| A7(10/1,000) · pred 가중 0 · 리캘리 비활성(R2) | **config 합의안 A7** · **Context_Score 설계**(Phase 0~1 pred 0) |
| pm_count | **fdc.raw 스키마** (Point.pm_count) |
| 신규 기준선 수립(A2·A3 상한 미적용·R9) | **합의안 신규결정 2** · 헌법 1-1 정합 |
| Y-임계 레짐 갱신(**B9·C2, 챔버별·B 소유**) — Phase 2서 예측분포로 label-free 1차 갱신→라벨 보정 | **신규결정 1**(사이클_정의) · **B9=B 확정**(합의안). *A/C 충돌 아님 — A=C65 예측, B=임계 갱신. b가 대상·산식 상세* |
| 재시작 복구 = control_limits 파생(phase=provisional 활성) · verdict enum은 provisional-활성이 loud 함의 · **post_pm_count만 영속 필요** | **API_Contract §8-B**(단 'quals 복구'는 스키마상 부정확 — quals=PASS/FAIL, loud/quiet 없음) · a §2·§7 (스토어 c 결정) |

*(클래스 내부 구조·메서드명은 B 내부 설계 자유 — src/agent_b_spc/ B 소유)*

---

## 6. 스코프 분할

| 조각 | 내용 | 마감 |
|---|---|---|
| **a** | Phase 상태머신(0/1/2·인메모리+복원 seam) + Phase 0/1(억제·광폭) + A7 카운터 + seam 호출 (순수 코어) | 7/29 핵심 |
| **b** | Phase 2 재산정(신규수립 모드) + 리베이스(R5) + Y-임계 + firm 전환 + verifying 섀도 감시 + A7 과도 제외 적용 | 7/31 완성 |
| **c** | fdc.agent 구독(`QualVerdictConfirmed`→전환) + 알람 억제 실제 게이팅(collector) + **영속 스토어 확정**(post_pm_count — 신규 테이블/컬럼/재계산 택1, 스키마 변경 시 PM) | (구독 배선) |
| **d** | M3 통합 검증 (전원 E2E: 요란→가한계→Phase2→R9→CT②) | 검증 |

**성격**: B6-3은 대체로 **"지휘자"** — 신규 코드는 **① Phase 상태머신 ② Phase 1 광폭 ③ recalc 신규수립 모드**뿐, 나머지는 기존 조각(B4-3·B5-2·TTTM seam) 호출·조율.

---

## 7. 열린 항목

- **섀도 평가 전체 사용 여부**: 멘토 "현업 무용" → PM 판단. **미탐은 제거(오탐만) 확정**, drift 경로에만 영향 → **가한계(B6-3) 무관**(Qual 체인이 검증).
- **`QualVerdictConfirmed` publisher**(A5-3) 라이브 여부 — 아니어도 코어는 주입식.
- **영속 스토어 방식(c 확정)**: `post_pm_count` 저장 = ⓐ신규 소형 테이블(`provisional_states`) / ⓑ기존 테이블 컬럼 / ⓒ재시작 재계산 택1. ⓐ·ⓑ는 스키마 변경이라 PM(헌법 3-2). *a 순수코어는 무관(주입 seam).*
- **`provisional_k` 실측(B)**: 광폭 배수 값은 config 등재(기본 5·임시)하되 Scorecard 실측 확정 — "미탐 0 하 과도 폭주 최소" 기준(a §2 박스).
- **Y-임계**: 소유·레짐 **해소**(챔버별·B 소유 — 신규결정 1·B9). A/C 충돌 아님. **b 잔여 = 설계 디테일**: 갱신 대상(B9·C2)·산식(A 예측분포 기반 label-free 1차→라벨 보정)·label-free 레벨 프라이어(신규결정 9)와의 관계.
- **verifying 섀도 감시** = b 스코프(사후 forward N장 — 사전 replay B5-1b와 다른 층).

---

## 8. Phase 2 검증 구조 (섀도 아님 — 참고)

Phase 2 재설정 correctness는 **자동 사전 점수 없음**(fit-by-construction) → **R9 사람 승인(사전) + verifying 섀도 감시(사후 N장)**. 새 레짐은 사전 정답이 없어(섀도 미탐과 같은 벽) 사람+사후로 — 멘토 입장과 일치.
