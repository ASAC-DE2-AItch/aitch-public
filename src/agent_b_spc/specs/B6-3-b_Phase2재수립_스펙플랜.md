# B6-3-b 스펙·플랜 — Phase 2 정식 재수립 (재산정·리베이스·Y·firm·verifying)

| 항목 | 내용 |
|---|---|
| **태스크** | B6-3-b — 요란 정착 후 **정식 실력치 재수립** + 스냅샷 리베이스(R5) + Y-임계 + firm 전환 + verifying |
| **참조** | **`specs/B6-3_가한계_overview.md`**(3단계 모델·seam·계약) · **`B6-3-a_가한계진입_스펙플랜.md`**(진입 상태머신·`on_firm_established` 훅) |
| **전제** | B6-3-a 완료(PHASE2_SIGNAL·provisional 행·exit 훅) · B5-1a(승인적용) · B5-1b(shadow_eval) · recalc·qual_snapshots 존재 |
| **작성** | 팀원 B(B) · 2026-07-22 · **마감** 7/31 |
| **성격** | 순수 코어(산출·writer 소유) + 승인·적용은 기존 배관 재사용. 라이브 트리거·orchestrator 판정은 이월 |

> **문서 구성**: 이 문서는 **[스펙]**(§1~§7 — 무엇을·왜·계약·불변식)과 **[플랜]**(§8~§10 — 파일·TDD·이월)을 한 파일에 **섹션으로 분리**한다. 공유 모델은 overview가 담당하고, 이 문서는 b의 결정·설계·구현 계획만 담는다.

---
---

# ══════════ [스펙] ══════════

## 1. 목적 · 경계

요란 PM으로 챔버 정상이 이사간 뒤(B6-3-a가 provisional 광폭으로 억제하며 **정착 1,000장** 대기), 정착이 끝나면(`PHASE2_SIGNAL`) **새 레짐의 정식 실력치를 세우고 챔버를 firm으로 졸업**시킨다.

핵심은 **관리선은 제안·승인**(승인값 그대로), **스냅샷·Y는 파생 참조라 apply 시 재계산**(스냅샷=c 정착버퍼 · Y=`wafer_predictions` read, C-2 ⓑ)하는 것이다 — 셋 다 같은 정착 레짐을 가리킨다(§2, 저장 불필요=② 해소).

- **경계(IN)** = ① 신규수립 재산정(상한 없음·항상 승인) · ② A7 과도 제외 표본 선택 · ③ 스냅샷 리베이스(R5) · ④ Y-임계 1차 갱신 · ⑤ firm 전환(provisional→firm) · ⑥ verifying(forward 오탐 품질 채점) — **산출·writer는 b 소유**
- **이월** = 승인 게이트(R9)·Incident 상태머신·반려 재트리거 = **orchestrator(PM)** · 라이브 트리거·버퍼 공급·firm apply = **c 배선** · CT②(AE 재학습) = **A** · Y read-side(B9 챔버별 read) = **B4-1**

---

## 2. 전체 흐름 — Phase 2 "패키지"

```
[a] 정착 1,000장 → PHASE2_SIGNAL(chamber)
      │
[b] 정착 표본 1벌 선택 (A7 1,000 제외 + 그 뒤 N=500 — 결정3·①)
      │   └ 표본 부족(1,001~1,500 미축적)이면 대기·None → c 재호출 (Review1 가드)
      └─ 관리선 = establish_group(표본) → **PROPOSED 기록**(limit_corrections)  # 상한 없음·항상 승인 (결정1)
      │      ※ 스냅샷·Y는 제안 때 계산·저장 안 함 — apply 시점에 재계산(스냅샷=c버퍼 · Y=wafer_predictions read, 파생 참조라 무방, ②해소)
      │
[R9] 엔지니어 승인 (orchestrator 게이트 — 이월)
      │
[적용] c 배선 트리거:
      ├─ firm 관리선 적재 (+ provisional 자동 deactivate) — **승인값 그대로**, B5-1a apply 재사용 (결정4·5)
      ├─ 스냅샷 리베이스 = rebase_snapshot(c 버퍼 정착표본) → 기존 active 비활성→신규 active  # apply 시 재계산 (R5-1)
      ├─ Y-임계 write = compute_y_threshold(**`wafer_predictions` 최근 N행 read**) → 전용 `y_thresholds` 행  # apply 시 재계산 (Y-1 ⓑ · 소스=C-2 ⓑ)
      ├─ a.on_firm_established(chamber) → TTTM 복귀·NORMAL (a 훅)
      └─ verifying 예약 (firm 후 forward 30장 축적되면 c가 verify_forward 호출 — 비동기, V·Review4)
      │
[종결] regime_transition Incident close (orchestrator)  · 반려 시 holding + 재트리거 대기(⑥)
```

**핵심 원칙**: **관리선**은 엔지니어가 승인하는 값이라 **제안 시점에 계산·기록 → 승인값 그대로 적용**(제안≠적용 불일치 방지, limit_corrections에 저장). **스냅샷·Y-임계**는 승인 대상이 아니라 **파생 참조**(엔지니어는 관리선=레짐전환을 승인)라, 저장 없이 **apply 시점에 재계산**한다(스냅샷=c 정착버퍼 · Y=`wafer_predictions` 최근 N행 read; 레짐이 정착 상태라 값 사실상 동일 — ② 저장 문제 해소). 모두 **같은 정착 레짐**을 가리킨다.

---

## 3. 결정 (13개 확정 2026-07-22)

### 묶음1 — 실력치 재수립 (재산정·과도제외·firm)

| # | 결정 | 확정 | 근거 |
|---|------|------|------|
| **1** 신규수립 | 어디에 | **별도 `establish_group` 함수** — `compute_group_limits`(공유 산식)만 재사용 · **상한(A2·A3) 없음** · **항상 needs_approval(R9)** | 초기수립=오프라인 배치·정기=상한 有 → **온라인 무상한 재수립은 세 번째 맥락**(신규). 정기 `recompute_group`(헌법 1-1 예외 무이상 자동)에 섞으면 회귀 위험. 신규결정 2 "신규 기준선 수립=상한 미적용" |
| **2** trigger_type | 무슨 값 | **기존 `incident` 재사용** (신규 값 X) | `incident`은 이미 `reset_ref` baseline 리셋 마커(`correction_history.py:43`)이자 regime_transition Incident(신규결정 10)의 종결 조치. firm/provisional 구분은 trigger_type(control_limits에 limit_basis 컬럼 없음) |
| **3** 과도 제외 표본 | 누가·어느 창 | **b 소유 `select_establishment_sample`** — A7 요란 **1,000장 제외** + 그 뒤 **N=500**(rolling_window_n) · 버퍼는 주입(c) · **정착 창=1,000 후 +500(①·잠정, 시뮬 튜닝)** | 신규결정 3 "N=500·C42=0·과도 제외". 과도 예측·값은 불안정(입력 미정착 + A 모델 적응 중)이라 정착분만. C42=0(sensor_window)은 wafer 내부 점화과도로 별개 |
| **4** firm 전환 | 어떻게 | **B5-1a apply 재사용**(firm 쓰며 provisional 자동 deactivate) · **TTTM 복귀=firm 시점**(신규결정 8) · **a.`on_firm_established` 훅**(구현됨) · 트리거=**c 배선** | 승인적용 배관 이미 존재. TTTM 제외 union은 a 소유라 해제는 반드시 a 경유(직접 set_excluded=union clobber). verifying은 별개 층이라 복귀는 firm 시점 |
| **5** 승인·적용 경로 | 재사용 범위 | **writer 파라미터화** — `recalc_writer._insert_correction`가 `TRIGGER_PERIODIC` 하드코딩(:123) → **`proposal.trigger_type`** 읽도록. `record_proposed` 경로 재사용 | proposal에 trigger_type 필드 이미 존재(단지 무시됨). **apply 쪽은 이미 승계 확인**(`approval_apply.py:110` `proposed["trigger_type"]`→control_limits) → 이 2줄만 고치면 'incident' 끝까지 흐름 |

### 묶음2 — 리베이스(R5) · Y-임계

| # | 결정 | 확정 | 근거 |
|---|------|------|------|
| **R5-1** 스냅샷 σ | 기준 | **전체 정착창 산포** (관리선 rolling-500 아님) | B5-2(F12)가 초기 스냅샷을 전체창 σ로 만듦. 스냅샷은 시점 고정 참조라 Qual σ-갭 잣대가 레짐 전후 일관되려면 관행 유지 |
| **R5-2** 리베이스 흐름 | 자동/게이트 | **firm 승인에 묶어 자동**(패키지) — 별도 게이트 X | 스냅샷은 헌법 1-1 승인 카테고리 아님(관리선·Recipe·정비·스크랩만) + 승인된 정착 데이터의 파생. 같은 레짐 전환 이중 승인 회피(신규결정 3 "일괄 수행") |
| **Y-1** Y 계산·저장 | 방식 | **전용 P95/P99 함수** — 정착 예측분포에서 `np.quantile([.95,.99])` · **저장 = 전용 `y_thresholds` 테이블(ⓑ 확정 2026-07-22)** — P95·P99 두 컬럼·챔버×레시피별·control_limits 안 씀 · DDL 제안=`limits/y_thresholds_ddl_proposal.sql`(**PM init.sql 승인 대기**) | control_limits는 밴드 1개·물리센서·기존행 갱신 형식이라 **2임계·가상센서·net-new인 Y와 부적합**(Review H1·H2). 억지 편입 시 가짜 메타·센서명·loader 오편입 → 전용 테이블이 문제 원천 제거·의미 정합 |
| **Y-2** Y 스코프 | 어디까지 | **label-free 1차만 b** — Phase 2서 정착 예측값(과도 제외·관리선과 같은 창)으로 산출 · **라벨 보정(D+60)=이월** · **read-side(B9 챔버별 read)=B4-1 하류(③열림)** | 신규결정 1 "최근 예측분포로 1차 갱신→라벨 후 보정, 챔버별". 데이터=wafer_predictions(A 발행). B5-3(예측치 Nelson 모니터링)은 별개·v5 강등 |

### 묶음3 — verifying

| # | 결정 | 확정 | 근거 |
|---|------|------|------|
| **V** verifying | b 역할 | **shadow_eval을 forward·단일세트로 재사용** — 새 firm 관리선 **forward N=30장**(`reopen_window_wafers`) 오탐율 채점. **b는 채점만** | 재발감지·reopen/close·Incident 'verifying' 상태=orchestrator(PM), forward 공급=c 라이브. overview "verifying 섀도 감시=b 스코프"의 실체=새 관리선 품질(오탐) forward 감시. shadow_eval의 오탐 카운팅 재사용 |

### 반려 경로 (⑥ 확정)

- **R9 반려 시 b는 reactive** — 자동 재제안 **안 함**. 반려=사람 판단이라 **재분석·재트리거·에스컬레이션은 orchestrator(Incident)**. b는 재트리거될 때만 다시 제안. a는 AWAITING_PHASE2 holding 유지(훅 가드가 안전 처리) — provisional 밴드가 계속 보호.

### 후속 보강 (2026-08-05) — cross-path PROPOSED supersede

> C(팀원 C) PR #110 §8 리뷰로 드러난 갭. 구현 후 발견이라 위 결정을 덮지 않고 보강으로 남긴다(헌법 4-1).

- **갭**: 재수립 제안은 `_try_phase2_proposal`이 **G2(`_proposed_groups`)·G3 없이** `record_proposed`를 호출한다(정기 리캘리 `_maybe_periodic_recalc`는 둘 다 있음). writer(`record_proposed`)도 supersede를 안 하고, status 전이는 `APPROVED_APPLIED`/`APPLY_FAILED`뿐이며 `limit_corrections` UNIQUE는 `correction_id`만 → **같은 그룹에 옛 정기 PROPOSED와 재수립 PROPOSED가 공존 가능.** 옛 정기 제안은 **PM 이전 상태** 기반이라, 나중에 독립 승인되면 `apply_approved`가 그 값을 적용해 관리선이 PM 이전으로 되돌아간다(미탐 위험).
- **결정**: **`record_proposed`에서 신규 PROPOSED INSERT 직전, 같은 그룹의 기존 live PROPOSED를 `SUPERSEDED`로 종결**한다(`_SUPERSEDE_LIVE_PROPOSED`). 재수립(post-PM firm)이 옛 정기(pre-PM)를 **이긴다**. G2-skip이 아니라 supersede여야 하는 이유 = skip이면 옛(무효) 제안이 유일하게 남는다. 옛 행은 삭제하지 않고 상태만 바꿔 감사 이력 보존(1-1 ⓑ).
- **배치 불변식**: supersede는 **두 가드(`decision != needs_approval` / `cur is None`) 뒤, `_insert_correction` 앞**. 실제 INSERT가 뒤따를 때만 실행 — 아니면 그룹 PROPOSED가 0이 되어 `derive_restore_state`의 AWAITING_PHASE2 파생(`_HAS_PROPOSED_ESTABLISH`)이 깨진다. 같은 트랜잭션이라 원자적.
- **정합(전수 확인)**: ⓐ `_G2_PROPOSED`는 `status='PROPOSED'`만 세니 SUPERSEDED 자동 제외 → 정기 재제안 가능 ⓑ `approval_apply._SELECT_PROPOSED`는 superseded면 `None` → **멱등 no-op**(되돌림 원천 차단, 이득) ⓒ `_HAS_PROPOSED_ESTABLISH`는 incident PROPOSED로 판정하는데 supersede는 Phase2에서만 실효+즉시 새 incident INSERT라 마커 보존 ⓓ 정기 경로는 G2로 걸러져 no-op.
- **스키마 무변경**: `limit_corrections.status`는 CHECK 없는 `VARCHAR(16)` → `'SUPERSEDED'` 그대로 저장. **마이그레이션·PM 불요, B 단독.**
- **코드**: `recalc_engine.STATUS_SUPERSEDED` 상수 + `recalc_writer._SUPERSEDE_LIVE_PROPOSED`·`record_proposed` supersede. 테스트 `test_record_proposed_supersedes_prior_live_proposed`.

---

## 4. 처리 흐름 · 핵심 로직 (의사코드)

> 부작용(writer·apply·persist)·버퍼·예측분포는 **주입**. 코어는 순수(Kafka·실 DB 없이 테스트).

**① 신규수립 재산정 — `establish_group`**
```
establish_group(group_key, settled_values, current, cfg) -> RecalcProposal:
    method = current["method"]                                # ★method 분기·필수 필드 (Review M2)
    filtered = _filter_values(settled_values)                 # NaN 필터(재사용, recalc_engine:102)
    if len(filtered) < cfg.min_group_n: return no_change("small_sample")
    new = compute_group_limits(filtered, cfg.limit)           # ★공유 산식 재사용 — 반환은 dict
    ucl, lcl = (new["q_ucl"], new["q_lcl"]) if method=="quantile" else (new["ucl"], new["lcl"])
    # ★정기와 차이: 상한(A2·A3)·dead_band·auto 경로 없음 → 무조건 승인
    return RecalcProposal(group_key, decision="needs_approval", method=method,   # method·n_used 필수(recalc_engine:76)
                          trigger_type="incident", n_used=len(filtered),         # 결정2
                          new_center=new["center"], new_ucl=ucl, new_lcl=lcl, new_sigma=new["sigma"],
                          status=PROPOSED)
# 주의: record_proposed(conn, proposal)는 **그룹당 1건**(패키지 일괄 아님) — 그룹별 호출
```

**② 과도 제외 표본 — `select_establishment_sample`**
```
select_establishment_sample(buffered_wafers, cfg) -> {group_key: [values]} | None:
    # A7 요란 1,000장 제외 → 그 뒤 정착분에서 최근 N=500 (결정3·①(b))
    settled = [w for w in buffered_wafers if w.post_pm_count > cfg.seasoning_loud]  # >1,000
    if len(settled) < cfg.min_establishment_wafers:          # ★하한 가드 (Review1)
        return None      # 부족 → 대기(불완전 패키지 원천 차단). c가 wafer 축적 후 재호출 = ①(b) "+500 대기" 구현
    recent = settled[-cfg.rolling_window_n:]                  # 최근 500
    return group_by(recent)          # 버퍼=주입(c) · 정확 창 경계는 시뮬 튜닝(①)
```
> **★ 하한 가드 = ①(b) 메커니즘**(Review1): a가 정착 1,000에 `PHASE2_SIGNAL`을 쏜 **직후엔 post-1,000 wafer가 0장**이다. b는 1,001~1,500이 쌓일 때까지 **기다려야** 하므로, `select_...`은 `settled < min_establishment_wafers`면 **None 반환(패키지 미생성)** 하고, c가 wafer 유입에 따라 **재호출**해 충분해지면 그때 패키지가 만들어진다. (a는 그동안 AWAITING_PHASE2 holding — provisional 밴드 보호.) Kafka 누락·재시작으로 표본이 모자라도 동일하게 안전 대기.

**③ 스냅샷 리베이스 — `rebase_snapshot` (R5)**
```
rebase_snapshot(chamber, settled_by_group, cfg) -> {snapshot_id, sensor_stats}:
    stats = {}
    for gk, vals in settled_by_group.items():
        if gk not in 건강센서8종: continue
        clean = _filter_values(vals)                           # ★NaN 정제 (Review3)
        if len(clean) < cfg.snapshot_min_n: continue           # ★소표본·붕괴 skip (B5-2 C-slim 패턴 — ZeroDiv/KeyError 방지)
        stats[gk] = snapshot_stats_for_group(clean, method, cfg)   # ★재사용, σ=전체창(R5-1)
    return stats               # 쓰기=seed_snapshot 패턴(기존 active 비활성→신규 active INSERT)
```

**④ Y-임계 — `compute_y_threshold` (Y-1·Y-2)**
```
compute_y_threshold(chamber, recipe, recent_predictions, cfg) -> y_row:
    p95, p99 = np.quantile(recent_predictions, [0.95, 0.99])   # label-free 1차 (Y-2)
    return y_row(chamber, recipe, p95=p95, p99=p99,            # 전용 y_thresholds 행 (ⓑ)
                 trigger_type="incident", calc_window_n=len(recent_predictions))

write_y_threshold(row):   # 전용 writer — 기존 active 비활성 → 신규 active INSERT (control_limits 패턴)
    UPDATE y_thresholds SET is_active=false WHERE chamber_id=:ch AND recipe_id=:rc AND is_active
    INSERT INTO y_thresholds (...)          # ★net-new도 그냥 INSERT (선행 행 불요)
```
> **✅ Y 저장 = 전용 `y_thresholds` 테이블 확정 (ⓑ, 2026-07-22)** — control_limits 부적합(net-new·2임계·가상센서 → Review H1·H2)을 **원천 제거**. `chamber×recipe`별 **P95·P99 두 컬럼**, `is_active` 버전 관리(control_limits 패턴), **단순 INSERT라 net-new 자연스러움**. DDL 제안 = `src/agent_b_spc/limits/y_thresholds_ddl_proposal.sql` → **PM init.sql 승인 대기**. loader/Nelson 무관(control_limits 아님). read-side(B9/C2가 여기서 조회)는 여전히 **③(B4-1 하류)**.

**⑤ verifying — `verify_forward` (V)**
```
verify_forward(new_firm_limits, forward_points, cfg) -> {오탐율, verdict}:
    flagged = _replay_flagged(new_firm_limits, forward_points, group_key)  # ★shadow_eval 재사용
    # forward N=30, 단일세트(신 관리선). 재발/reopen/상태=orchestrator, 공급=c
    return {"false_alarm_pct": ..., "verdict": PASS/FAIL}
```
> **★ 실행 시점 = 지연/비동기**(Review4): firm 적용 t=0엔 미래 forward 30장이 **아직 없다**. `verify_forward`는 승인 즉시 실행이 아니라, **firm 적용 후 30장이 실시간으로 흘러들어오는 동안 c(컨슈머)가 `forward_points`를 모아** 30장 채워지면 호출한다. b의 `verify_forward`는 **주어진 forward 집합을 채점만** 하는 순수 함수고, forward 수집·호출 시점은 c 라이브(shadow_eval의 과거 replay와 정확히 반대 방향).

**c 오케스트레이션 (b는 산출·writer 제공만) — proposal=관리선 / apply=스냅샷·Y 재계산**
```
# [제안] a의 PHASE2_SIGNAL 수신 (c)
on_phase2_signal(chamber):
    sample = select_establishment_sample(buffer, cfg)         # 정착 표본(부족 시 None→대기)
    if sample is None: return
    for gk, vals in sample: record_proposed(establish_group(gk, vals, current[gk], cfg))  # 관리선만 PROPOSED

# [적용] 승인된 firm correction 유입 시 (c) — 스냅샷=c 정착버퍼·Y=wafer_predictions read로 재계산(② 해소·C-2 ⓑ)
on_firm_correction_applied(chamber, recipe):
    # (firm 관리선은 B5-1a apply가 이미 적재·provisional deactivate)
    write_snapshot(rebase_snapshot(chamber, buffer.settled(chamber), current, cfg))   # apply 시 재계산
    write_y_threshold(compute_y_threshold(chamber, recipe, read_recent_predictions(chamber, recipe, cfg), cfg)) # apply 시 wafer_predictions read (C-2 ⓑ)
    a.on_firm_established(chamber)                 # TTTM 복귀·NORMAL
    verify.start(chamber)                          # forward 30장 tally 예약
# 확정(c): assemble_package(세 산출 묶음) 미사용 = 단일(개별 호출) — 관리선=제안시 승인값·스냅샷/Y=apply 재계산이라 시점 상이(②·제안≠적용 충돌). 사함수→삭제 권장(저우선)
```

---

## 5. 재사용 / 신규

**재사용 (무수정)**
- `compute_group_limits`(산식·반환 dict) · `snapshot_stats_for_group`·`seed_snapshot`(스냅샷 쓰기) · `shadow_eval._replay_flagged`(오탐 카운팅·**int 반환**이라 오탐율=flagged/n은 verify가 계산) · `_filter_values`(recalc_engine:102)
- **`apply_approved`·`record_proposed` = 물리센서 firm 한정 재사용** — Phase 1 provisional **선행 is_active 행**이 있어 deactivate+승계 성립(trigger_type 승계 :110). **Y(predicted_c65)는 선행 행 없어 이 경로 불가**(Review H1 — §4④ 참조)

**재사용 (소수정)**
- `recalc_writer._insert_correction` — `TRIGGER_PERIODIC` 하드코딩(:123) → `proposal.trigger_type` 읽도록 **파라미터화**(결정5). 정기 경로는 값 그대로 'periodic' 나오는지만 회귀 확인
- **write payload (Review2 정정 + Review M3)** — control_limits엔 **`limit_basis` 컬럼 없음**(firm/provisional 구분 = `trigger_type`; `limit_basis='firm'`은 nelson_engine **출력 필드** 하드코딩이지 DB 컬럼 아님). 진짜 **NOT NULL = center·sigma·ucl·lcl·method·limit_version**(init.sql:176-181) — *k_sigma·calc_window_n은 nullable*. firm 물리센서 경로는 apply_approved가 center/ucl/lcl은 correction에서, method/sigma/q_low/q_high는 **현행 행 승계**로 전부 커버(:105-112) → 무해. (establish_group은 center·ucl·lcl·sigma·method·trigger_type만 산출)

**신규**
- `establish_group`(무상한·항상 승인 재산정) · `select_establishment_sample`(A7 제외+N500) · `compute_y_threshold`(P95/P99) + **`write_y_threshold`(전용 `y_thresholds` INSERT·is_active 교체)** · `verify_forward`(forward 오탐 품질) · 패키지 조립
- **신규 테이블 `y_thresholds`** (DDL 제안 `limits/y_thresholds_ddl_proposal.sql` — PM init.sql 승인 대기)

**신규 (config — B 소유 `limit_engine:`)**
- `min_establishment_wafers` — 재수립 최소 정착 표본(하한 가드, Review1). 잠정 = `rolling_window_n`(500)와 동일하게 두되, Kafka 누락 여유 위해 소폭 floor(예 400)도 검토 — 값 실측/튜닝
- `snapshot_min_n` — 리베이스 그룹별 최소 유효 표본(가드, Review3). 잠정 30(B5-2 스냅샷 관행)
- **verifying (Review M4 정정)**: forward N = **`shadow_eval_wafers`(A5, B 소유)** 우선 — `reopen_window_wafers`는 `incident:`절 **PM 소유**·의미도 "재발 창"이라 부적합. 합격선은 감소율(%) 아닌 **절대 오탐율 상한** = 신규 B 키 **`verify_forward_false_alarm_max_pct`(잠정 5.0 — 구현 시 확정)**(`shadow_pass_false_alarm_reduction_min_pct`는 감소율이라 못 씀). 오탐율=`_replay_flagged`(int)/n은 verify 자체 계산

---

## 6. 크로스파트 계약 · 열린 항목

**계약 (근거 있음 — 임의 아님)**
| 계약 | 근거 |
|---|---|
| PHASE2_SIGNAL·provisional 행·`on_firm_established` | B6-3-a 스펙 |
| 신규수립=상한 미적용·R9 · TTTM 복귀=Phase 2 완료 · regime_transition Incident | 신규결정 2·8·10 |
| Y-임계 챔버별·B 소유 · 데이터=wafer_predictions · 저장=전용 `y_thresholds` | 신규결정 1 · Y-1 ⓑ(control_limits 부적합) |
| apply가 correction.trigger_type 승계 | `approval_apply.py:110` (확인됨) |

**열린 항목 (b 단독 확정 금물 — 제안 + 의존)**
- **✅ 승인대기 패키지 저장 = 해소 (저장 불필요, 2026-07-22)** — 스냅샷·Y는 **승인받는 값이 아니라 파생 참조**(엔지니어는 관리선=레짐 전환을 승인). 따라서 저장 안 하고 **apply 시점에 c 버퍼(정착 표본)로 재계산**한다. 관리선만 승인값 그대로(limit_corrections). c 버퍼는 계속 갱신돼 apply 때도 정착 데이터 있음(재시작도 재충전). → **PM 협의 불필요.** *`assemble_package`(세 산출 묶음 헬퍼)는 c가 proposal=관리선·apply=스냅샷/Y 재계산으로 오케스트레이션하면 안 쓰일 수 있음 → c 구현 시 사용/삭제 결정(기능 영향 없음).*
- **✅ Y write 표현 = 전용 `y_thresholds` 테이블 확정 (ⓑ, 2026-07-22)** — control_limits 부적합(net-new·2임계·가상센서)을 원천 해결. ⓐ(2 가상센서: 가짜메타·센서명·loader 방어·의미부채)·ⓒ(config: 런타임 갱신 불가) 기각. **PM 요청**: DDL 제안(`limits/y_thresholds_ddl_proposal.sql`) → **init.sql 승인**(헌법 3-2). loader/Nelson 자동편입(M1) 문제도 control_limits 미사용으로 소멸.
- **③ Y read-side** — crazy 판정(B9)이 글로벌 상수(1572) 대신 챔버별 `y_thresholds`(P99) 읽어야 효과 · Qual(C2)은 P95. **B4-1/A 하류 배선 의존.** b는 계산·쓰기까지.
- **① 정착 창 정확 경계** — "1,000 후 +500" 잠정 확정, **최종 수는 시뮬 거동 튜닝**(provisional_k 선례처럼 값만 교체).

---

## 7. 불변식 (헌법 정합)

- **1-1 HITL** — 신규수립은 **항상 R9 승인**(무상한이라 더 엄격). firm 적용은 승인 하류. Y-임계·스냅샷은 승인 카테고리 아니나 승인된 데이터 파생(패키지). *무승인 실력치 변경 코드 0.*
- **1-3 누수** — 과거·현재 데이터만(정착 표본·과거 예측). verifying은 사후 forward(미래 정보로 과거 예측 아님).
- **1-4 Incident** — regime_transition Incident당 동시 pending 1건(순차 승인은 허용 — 복귀→신규수립→CT②). 반려 재트리거도 순차.
- **6-1** — docstring·logging·매직넘버 0(N=500·1,000·P95/P99·30 전부 config).
- **순수 코어** — 산출은 주입 seam으로 순수 테스트. 적용·트리거는 c/orchestrator.

---
---

# ══════════ [플랜] ══════════

## 8. File Structure

| 파일 | 역할 |
|---|---|
| `src/agent_b_spc/establish_engine.py` | **Create** — `establish_group`·`select_establishment_sample`·`compute_y_threshold`·`write_y_threshold`·`rebase_snapshot`(패키지 산출·Y writer, 순수) |
| `src/agent_b_spc/verify_forward.py` (또는 `shadow_eval.py`에 추가) | **Create/Extend** — `verify_forward`(shadow_eval 오탐 카운팅 forward 재사용) |
| `src/agent_b_spc/limits/y_thresholds_ddl_proposal.sql` | **Create(제안)** — `y_thresholds` DDL 제안 → **PM init.sql 반영 대기**(Y-1 ⓑ) |
| `src/agent_b_spc/recalc_writer.py` | **Edit** — `_insert_correction` trigger_type 파라미터화(결정5) · **`record_proposed` cross-path supersede**(후속 보강 2026-08-05) |
| `src/agent_b_spc/recalc_engine.py` | **Edit** — `STATUS_SUPERSEDED` 상수(후속 보강 2026-08-05) |
| `src/agent_b_spc/tests/test_establish_engine.py` 등 | **Create** — 순수 테스트(seam mock) |
| `initial_limits.py`·`qual_snapshot`·`shadow_eval.py`·`approval_apply.py`·`db/init.sql`·`params.yaml` | 읽기전용 — 재사용 산식·writer·스키마·config |

## 9. Task (TDD) — 묶음1 → 2 → 3 (의존 순서)

- [ ] **묶음1 (실력치 재수립)**
  - **establish_group** — 정착 표본 → 관리선 산출 · **상한 없음·항상 needs_approval·trigger_type='incident'** · 소표본 가드 · `compute_group_limits` 재사용 확인
  - **select_establishment_sample** — A7 1,000 제외 + 최근 N=500 · 창 경계(1,000 후) · 버퍼 주입 · **하한 가드 테스트**(settled < min → None, 재호출 시 충족되면 표본 반환 — Review1)
  - **writer 파라미터화** — `_insert_correction`가 proposal.trigger_type 사용 · **정기 경로 'periodic' 무회귀** · apply_approved 'incident' 승계 통합 확인
- [ ] **묶음2 (리베이스·Y)**
  - **rebase_snapshot** — 같은 정착 표본·**σ=전체창** · seed_snapshot 패턴(active 교체) · 건강센서 8종(`HEALTH_SENSORS` 상수) · **NaN 정제·소표본 skip 가드**(붕괴 그룹 ZeroDiv/KeyError 방지 — Review3) · 반환 키=`build_sensor_stats` 요구 형식(gk 조정)
  - **compute_y_threshold** + **write_y_threshold** — 예측분포 P95/P99 계산 · **전용 `y_thresholds` 테이블** INSERT(is_active 교체·net-new INSERT) · label-free 1차 · *테이블은 PM init.sql 승인 후*
- [ ] **묶음3 (verifying)**
  - **verify_forward** — 새 firm 관리선 forward N=30 오탐율·verdict · shadow_eval 재사용 · 단일세트 · **순수 채점(주어진 forward 집합)** — 수집·호출 시점은 c 비동기(firm 후 30장 축적, Review4)
- [ ] **통합(c 오케스트레이션)** — 제안=관리선 record_proposed · **apply 시 재계산**(스냅샷=c버퍼·Y=`wafer_predictions` read, 저장 X) · (mock) apply 순서 firm·rebase·Y·`on_firm_established`·verify · ***`assemble_package`=미사용 확정(단일)·삭제 권장***
- [ ] **검증** — 그린 + 기존 recalc·shadow·qual 무회귀 · 서브에이전트 독립 검토(코드 대조)

## 10. 이월 · 확인

**이월**: 승인 게이트(R9)·Incident 상태머신·반려 재트리거·verifying reopen/close = **orchestrator(PM)** · 라이브 트리거·버퍼 공급·firm apply 배선 = **c** · CT②(AE) = **A** · Y read-side(B9) = **B4-1** · 라벨 보정(Y 2차) = 후속

**착수 전 확인**
- ✅ **13개 결정 확정**(묶음1 5 + 묶음2 4 + 묶음3 1 + 부가 3: ①정착창·⑤apply승계·⑥반려) · 그라운딩 완료(recalc·writer·approval_apply·qual_snapshots·shadow_eval·config·일정)
- ✅ **⑤ apply 승계 확인**(`approval_apply.py:110`) — 결정5(writer 2줄)만 필요
- ✅ **Y write 표현 확정(ⓑ, 2026-07-22)** — 전용 `y_thresholds` 테이블(control_limits 부적합 원천 해결). **PM 요청**: config 2키(a — provisional_k·seasoning_loud, 합의안 sync) + **`y_thresholds` DDL 승인**(제안 파일 첨부). 카톡 전달용 초안 완료
- ✅ **② 패키지 저장 = 해소** — apply 시 재계산(파생 참조, 저장·PM 불필요): **스냅샷=c 정착버퍼 · Y=`wafer_predictions` 최근 N행 read**(C-2 ⓑ — 예측은 인메모리 버퍼 아님). 관리선만 승인값 저장(limit_corrections)
- ⚠️ **열린 항목**: ③ Y read-side(B4-1/A) · ① 정착 창 정확 수(시뮬 튜닝)
- ⚠️ **B6-3-a 반영 대기 없음** — a 구현·훅·restore 완료(PR #42·#43). b는 a 인터페이스 위에서 착수 가능
- ✅ **로버스트니스 리뷰 반영(2026-07-22)** — 표본 하한 가드(Review1, ①(b) 대기 메커니즘 겸함)·스냅샷 NaN/소표본 가드(Review3)·verify_forward 비동기 시점(Review4)·write 필수컬럼 승계 명시(Review2). *단 Review2의 `limit_basis` 컬럼 우려는 스키마상 무효(해당 컬럼 없음, 구분=trigger_type) — 정정.*
- ✅ **서브에이전트 코드대조 검토 반영(2026-07-22)** — 🟠 **Y write 경로 재설계**(H1·H2: net-new라 apply_approved 재사용 불가·P95/P99 2임계 → 전용 설계·열린항목 승격) · 🟡 establish_group 시그니처(M2: method·n_used·dict·분기) · 🟡 loader 자동편입 정정(M1) · 🟡 NOT NULL列表 교정(M3) · 🟡 verify config 정정(M4). **✅ 검증 통과**: 결정5(:123)·apply승계(:110)·결정2(:43)·config 실재·R5/verify 재사용·헌법 — 묶음1 배관 전부 성립.
