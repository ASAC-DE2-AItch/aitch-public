# B6-3-d — M3 통합 E2E 검증 · B 슬라이스 (검증 플랜)

| 항목 | 내용 |
|---|---|
| **작성** | 팀원 B (팀원 B) · 2026-07-24 · 상태: 초안 |
| **성격** | **검증 플랜** — 새 설계 없음. a·b·c 산출물을 **요란 재인증 시나리오**로 관통시켜 "되나?"를 확인. **B 슬라이스만** |
| **경계** | d(M3 통합 E2E)=**전원**(c-스펙 §8). 본 문서 = **B가 주는 것·받는 것·단계별 기대동작·합격기준**. 전체 관통 조율·타 파트 검증은 **PM/전원** |
| **전제** | a·b·c 구현 완료(c=`feat/agent-b-live-consumer` 스택 — C-1) · 이벤트 2종(QualVerdictConfirmed·ChamberRequalified) **mock** |
| **참조** | `B6-3-a/b/c_...` · `B5-1a_승인적용` · `B4-2_TTTM` · 계약 §8 |

---

## 1. 시나리오 체크리스트 (요란 재인증 — B 기대동작)

| # | 단계 | 입력 (→B) | B 기대동작 | 체크포인트 (관측) |
|---|---|---|---|---|
| **1** | 요란 PM | `fdc.raw` pm_count↑ (real·시뮬) | `on_pm` → **PHASE_0 억제**, 카운터·중심버퍼 초기화 | `get_phase=PHASE_0` (정기 리캘리는 이 E2E 밖 — B4-3 흐름) |
| **2** | Qual 요란 판정 | QualVerdictConfirmed `loud` (**mock**) | `on_qual_verdict` → **PHASE_1** · provisional 광폭 write(**σ 인코딩**) · `set_excluded` | control_limits provisional 활성 · TTTM 제외 · σ-존 룰 침묵 |
| **3** | 정착 진행 | `fdc.raw` wafer (real) | `on_wafer` → post_pm_count↑ · (P0)중심 축적/(P1)유지 | 광폭이 위반 억제(알람 급감) · **backward 버퍼 아직 비어 있음**(축적은 post_pm>1,000=4단계 이후) |
| **4** | 정착 완료·제안 | post_pm_count=**1,000** (A7) | **PHASE2_SIGNAL** → AWAITING_PHASE2 · **backward 버퍼 축적 시작**(post_pm>1,000) · (+500 표본 후) `establish_group`→`record_proposed` (**관리선만**) | 신호 1회 · 버퍼 채워짐(1,001~1,500) · limit_corrections **PROPOSED** 생성 |
| **5** | 승인·firm 적용 | `fdc.correction`(firm, real·오케) | `apply_approved` — **관리선만** control_limits 적용 (스냅샷·Y는 6단계 후속서) | control_limits 신 버전 (그룹별 correction 여러 건) |
| **6** | 재수립 완료·복귀 | ChamberRequalified (**mock**) | **후속 블록**(`_run_firm_followup` — `phase==AWAITING_PHASE2` 게이트·챔버당 1회): `rebase_snapshot`(센서버퍼)·`compute_y_threshold`(**wafer_predictions read**)·write · `on_firm_established`→NORMAL·TTTM 복귀 · `verify.start` | **qual_snapshots·y_thresholds write**(버퍼·`wafer_predictions` 존재 시 — 없으면 skip+로그, firm 관리선은 유지) · provisional 해제·fleet 재참여·NORMAL · **(fallback) 연이은 correction에도 후속 1회만·verify 리셋 안 됨** |
| **7** | verify | forward **30장** (real) | `verify_forward` 오탐율 채점 | PASS/FAIL (오탐율 ≤ `verify_forward_false_alarm_max_pct`) |

> **주(注) — E2E 테스트 정확도**:
> - **카운터 제로포인트**: `post_pm_count`는 **PM(`on_pm`) 시점 0**에서 **매 wafer +1**, **Phase 0 wafer(PM~Qual 구간) 포함**, **Qual verdict서 리셋 안 됨**(`provisional_mode.py:120`·`:212`, `on_qual_verdict`는 카운터 미변경). 1,000 도달 시(**PHASE_1 상태에서만**) PHASE2_SIGNAL. → 테스트는 **PM부터 1,000장** 주입(Qual은 그 사이 아무 시점).
> - **산출 시점·소스(C-2·단일)**: **관리선** = 4단계 제안(센서 버퍼)→5단계 apply(`apply_approved`). **스냅샷·Y** = **6단계 후속**(`_run_firm_followup` — primary=ChamberRequalified / fallback=첫 firm correction서 실행): 스냅샷=센서 버퍼 재계산·Y=`wafer_predictions` DB read. **제안(4)·apply(5)에서는 스냅샷·Y 계산 안 함**(② 해소). → primary 시나리오에서 qual_snapshots·y_thresholds write는 **6단계에서 관측**.

---

## 2. mock / real 매트릭스

| 신호 | 지금 | 전환 시점 |
|---|---|---|
| `fdc.raw` (pm_count·wafer) | ✅ **real** (시뮬·D0-2) | — |
| `fdc.correction` (firm) | ✅ **real** (오케스트레이터) | — |
| `wafer_predictions` read (Y 소스) | ✅ **real** (A 적재) | — (6단계 후속서 최근 N행 read) |
| `QualVerdictConfirmed` | ⚠️ **mock** (c 하네스) | PM 발행 시 실전환 (배선 그대로, 발행자만 교체) |
| `ChamberRequalified` | ⚠️ **mock** (c 하네스·계약 미정의) | PM 발행 시 실전환 (**필드·토픽 PM 확정 필요**) |
| 알람 억제 **집행** | ⏳ 대기 | B4-1 랜딩 |
| `y_thresholds` write | ⚠️ **로컬 TEMP 관측 가능** | 공식 PM 승인 대기 (`init.sql:377` TEMP → B-슬라이스 write 관측 O) |

---

## 3. 크로스파트 핸드오프 (B 경계)

| 방향 | 상대 | 내용 |
|---|---|---|
| B ← **받음** | 시뮬·오케·PM·**A** | `fdc.raw` · `fdc.correction` · `fdc.agent`(mock) · **`wafer_predictions`**(A 적재·Y read) |
| B → **줌** | B4-1 | `get_phase`(억제 게이트 입력) · 위반 기록 |
| B → **줌** | 대시보드 | control_limits·qual_snapshots·y_thresholds (적용 결과) |
| **대기** | B4-1·PM | 억제 집행(collector) · 이벤트 실발행 |

> `fdc.alert` 발행은 **B4-1** (c는 발행 안 함 — 헌법 1-2). c는 `get_phase`·위반 기록만 제공.

---

## 4. 합격 기준

- **관통** — 1~7 단계가 순서대로 관측 (요란→가한계→정착→재수립→firm→복귀).
- **억제** — Phase 0·1에서 알람 급감 (광폭이 σ-존 룰 침묵 + B4-1 게이트 랜딩 후 완전).
- **멱등** — at-least-once 재전달 시 `on_pm`(pm_count 가드)·`on_qual_verdict`(phase 가드)·`apply_approved`(멱등)·firm 후속(블록 게이트) 중복 무해.
- **재시작** — Phase 1 중 재시작 → control_limits 파생으로 provisional 복원 (post_pm_count 리셋·정착 지연 수용 — 결정2 ⓓ).
- **무회귀** — 기존 `spc_consumer`(fdc.raw·correction 경로) 정상.

**네거티브·엣지 대조 (선택 — B 불변식)**
- **Qual quiet** → NORMAL·provisional 미진입 ("loud만 개입" 대조).
- **R9 반려** → firm correction 미유입 → AWAITING holding·광폭 유지(졸업 안 함).
- **AWAITING 중 재시작** → backward 버퍼 유실 → 후속서 스냅샷·Y skip, firm 관리선만 적재(알려진 한계, c-스펙 §8).
- **표본 부족** → `select_establishment_sample` None → 다음 wafer 재시도(무한 대기 아님).

---

## 5. B 미결 · 의존

- **PM 이벤트 발행** — **mock으로 관통 가능**(블로커 아님), 실배선은 발행 후. **QualVerdictConfirmed**=계약 §8-B **확정 스키마** / **ChamberRequalified**=계약 미정의(**c 초안·PM 확정 대기**, 소비는 chamber_id만).
- **B4-1 collector** (억제 집행) — `get_phase` 제공 완료, 집행 랜딩 대기.
- **브랜치 스택** (C-1) — c dev 통합은 B4-1에 전이 의존(`feat/agent-b-live-consumer` 미푸시).
- **y_thresholds 테이블** — **공식** PM init.sql 승인 대기 (로컬 TEMP `init.sql:377`로 B-슬라이스 E2E write는 관측 가능).

---

> **d 전체(전원) 관통**은 PM 조율 — 본 B 슬라이스는 "B가 각 단계에서 정확히 동작하고, 받을 것·줄 것이 맞는지"를 탄탄히 하는 데 한정.

---

## 6. ✅ 검증 결과 (2026-07-24 — B 슬라이스 전 항목 통과, 432 passed)

**7단계 관통** — `test_b63d_m3_full_scenario_b_slice`(CP1~CP7 단계별 assert):

| 단계 | 체크포인트 | 결과 |
|---|---|---|
| 1 요란 PM | `get_phase=PHASE_0` | ✅ |
| 2 Qual 요란 | PHASE_1 · provisional 활성 · **TTTM 제외**(`tttm.excluded`) | ✅ |
| 3 정착 진행 | post_pm↑ · **과도 버퍼 미축적** | ✅ |
| 4 정착 완료 | PHASE2_SIGNAL 1회 · 버퍼 축적 · **관리선만 PROPOSED** | ✅ |
| 5 firm 적용 | control_limits 신 버전(incident) · 후속 침묵(requalified_live) | ✅ |
| 6 재수립 완료 | ChamberRequalified→후속: **qual_snapshots·y_thresholds write** · NORMAL · TTTM 재참여 · verify 시작 | ✅ |
| 7 verify | forward 오탐율 채점·정리 | ✅ |

**네거티브·엣지**:
- Qual quiet → NORMAL 미진입 — `test_c_agent_quiet_verdict_returns_normal` ✅
- **R9 반려 → AWAITING holding·광폭 유지**(졸업 안 함) — `test_b63d_negative_r9_rejection_holds_provisional` ✅
- **AWAITING 재시작 → 버퍼 유실 → 스냅샷·Y skip·firm 관리선 유지**(알려진 한계) — `test_b63d_edge_restart_empty_buffer_skips_snapshot_y` ✅
- 표본 부족 → 재시도 종료(무한 대기 아님) — `test_c_proposal_discards_pending_after_sufficient_sample` ✅

**합격 기준**:
- **관통** ✅ (7단계) · **억제** = 광폭 σ 인코딩(a 테스트 보장) + `get_phase`(B4-1 게이트 랜딩 후 완전 — 대기) · **멱등** ✅ (`on_pm`/`on_qual_verdict` phase 가드·`apply_approved`·firm 후속 게이트·버퍼 dedup `test_c_buffer_dedups_redelivered_wafer`) · **재시작** ✅ (`derive_restore_state` + AWAITING 재시작 엣지) · **무회귀** ✅ (432 passed, 기존 D0-2·B5-1a·B4-2 정상)

> **미결(문서대로)**: 알람 억제 **집행**=B4-1 랜딩 대기(c는 `get_phase` 제공) · 이벤트 실발행=PM(현 mock 관통) · `ChamberRequalified` 스키마 PM 확정. **d 전원 관통**(시뮬→A→B→agent→오케→대시보드)은 PM 조율.
