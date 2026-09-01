# B5-2 경량 스펙 · 구현 플랜 — Qual 2지표 (σ-갭 + Nelson)

| 항목 | 내용 |
|---|---|
| **태스크** | B5-2 — 정비 후 Qual 판정용 2지표(σ-갭 · Nelson) 산출 → A5-3 4지표 취합에 공급 |
| **분류** | M3-critical · 마일스톤 7/28 |
| **작성** | 팀원 B(B) · 2026-07-20 (초안 → rev.1 자체리뷰 → **rev.2 서브에이전트 검토 반영**) |
| **rev.2 변경** | M2 median-center(분위수 skew 편향 실측 0.32σ 제거) · H1 `rule_n1` list인자 · H2 Nelson 퇴화밴드 가드 · H3 챔버 config주도+fixture · M1 QualConfig 합성접근 · M3 분위수 ucl/lcl=q밴드 · M4 Nelson UNKNOWN · (폴리싱) Nelson 임계 `cfg.nelson_max`(6-1) · gk_str 5필드 근거 · sref 그룹당 1회 공유 |
| **전제** | B2-1 초기 실력치(`initial_limits.py`) · B3-1 Nelson(`nelson_rules.py`) · B4-3 재산정(`recalc_engine.sigma_ref_of`) — 전부 완료 |
| **선행 무관** | TTTM 방법론(5-1 멘토 확정)과 독립 · D0(Kafka·alert) 불필요 (순수 코어) |

---

## 1. 목적

챔버가 정비(PM/BM)로 열렸다 닫히면 바로 생산에 못 쓰고 **재인증(Qual)** 을 거친다. 개방 후 **Qual 웨이퍼 5장(고정 표준 조건)** 을 찍어 **4지표(AE·σ-갭·Nelson·C65 proxy)** 로 "조용(그냥 복귀) / 요란(새 레짐 → 신규 기준선 + AE 재학습)"을 판정한다.

이 4지표 중 **B가 소유하는 2지표(σ-갭 · Nelson)** 를 계산해 A5-3에 넘기는 것이 B5-2다. σ-갭은 AE와 함께 **운영 주력 2축**(둘 다 라벨 불요)이다.

> **σ-갭 = "센서가 정비 전 지문(스냅샷)에서 몇 σ 이사갔나"**(계통 이동) · **N1 = "개별 점이 스냅샷 관리선 밖으로 나갔나"**(개별 이상치) — 상호보완.
> B5-2 = "2지표를 계산해서 넘기는 데까지". 최종 조용/요란 종합은 **A5-3**, 스냅샷 리베이스는 **B6-3**.

---

## 2. 범위 결정 (5개 확정)

| # | 결정 | 확정 | 근거 |
|---|------|------|------|
| **1** 스코프·입출력 | 배선 포함? | **순수 판정 코어 함수.** 스냅샷 **주입식**. 발행·DB 영속·리베이스·라이브·A5-3취합 이월 | D0 독립·테스트 용이 (nelson_engine이 limits 주입받는 패턴) |
| **2** 스냅샷 | 무엇을·어디서 | 건강센서 8종(C11·C15·C16·C17·C31·C61·C62·C63, **C12 제외**) 그룹별 `{center, sigma, ucl, lcl, method}`. **그룹 단위(A1)** — max 갭. **전체창 σ 재계산**(seg1 전체 — F12). 분위수 그룹 **robust-σ + median-center**(2-track, M2). seg1 시딩 + **config 챔버 목록** offset | dict 형태 control_limits와 동일 · F12 함정 회피 · **M2 편향 0.32σ 제거**(실측) |
| **3** σ-갭 산식 | 5장 집계·임계·결측 | **A1′ 2-track**: σ그룹=**평균 집계·mean-center** / 분위수그룹=**중앙값 집계·median-center**(M2). 임계 **≥3.0σ=FAIL**. **C-슬림**: 유효 0장 skip, 전부면 **UNKNOWN**(거짓 PASS 금지) | 평균=정밀(X̄)·중앙값=치우침 정합 — **집계·center 짝맞춤** · 미탐>오탐 |
| **4** Nelson | 룰 범위 | **N1만**(5장이라 N2·N3·N4·N7·N8 발동불가, N5·N6은 얄팍→제외). 스냅샷 기준. 위반 0=PASS·유효0건=UNKNOWN(M4) | 5점 표본에 통계적으로 정직 · σ-갭과 역할 분담 |
| **5** 출력 계약 | 형태 | 2지표 dict. **최종판정 B 미포함**(A5-3 종합). **`per_group[]` 포함**(헤드라인 + 전 그룹 상세). snake_case JSON-직렬화 | "정량은 B, 근거는 C"(7/19) · 역할 경계(결정 1) |

> **신규 config 키 0 · 스키마 변경 0** → `params.yaml`·합의안 doc·`init.sql` 무변경 (헌법 4-3 doc-sync 불요, 6-1 매직넘버 0). 전부 기존 config 소비: `qual.pass_sigma_gap_max=3.0` · `qual.pass_nelson_violations_max=0` · `limit_engine.control_limit_k_sigma=3.0`(→`LimitConfig.k_sigma`). *(`qual.wafer_count=5`는 호출자가 소비 — 코어는 미사용, 정보성.)*

---

## 3. 트리거 · 경계

- **상류 (범위 밖)** — 시뮬레이터가 개방 후 `is_qual` 웨이퍼 5장을 발행(P5-3). 실시간 라우팅·구독은 라이브 배선(D0/B6-3). B5-2는 그 **5장의 그룹별 요약지표**를 입력으로 받는다(주입).
- **B5-2 책임** — 주입된 Qual 지표 + 스냅샷 → σ-갭·N1 2지표 계산 → 반환. **그게 전부.**
- **하류 (범위 밖)** — A5-3이 반환값을 AE·C65와 합쳐 4지표 종합 판정(S7). B5-2는 최종 조용/요란을 내지 않는다.
- **트랜잭션·발행 없음** — 순수 함수. 스냅샷 조회(로더)만 읽기 전용 DB 접근.

---

## 4. 처리 흐름 — `evaluate_qual(...) -> QualBIndicators`

```
evaluate_qual(
    qual_indicators: dict[gk, list[float]],   # 그룹별 Qual 5장 요약지표 (호출자가 준비 — 5-2 키 계약)
    snapshot:        dict[gk, dict],          # 스냅샷 로더 산출 {center,sigma,ucl,lcl,method}
    cfg:             QualConfig,              # .threshold · .limit(LimitConfig 합성) · .nelson_max
    *, chamber_id, qual_id, snapshot_id,
) -> QualBIndicators
```

```
[σ-갭]
1. for gk in snapshot:                       # 스냅샷에 든 건강센서 그룹만 순회
     pts = qual_indicators.get(gk, [])
     if len(pts) == 0:  per_group[gk]=skip;  continue          # C-슬림: 유효 0장 skip
     agg  = median(pts) if snapshot[gk].method=='quantile' else mean(pts)     # A1′
     sref = sigma_ref_of(snapshot[gk], cfg.limit.k_sigma)      # σ / robust-σ (재사용) — M1: 합성 접근
     if sref <= EPS:  per_group[gk]=skip('degenerate'); continue   # 밴드 붕괴(이산센서 ucl≈lcl)→분모 폭발 차단
     gap  = abs(agg - snapshot[gk].center) / sref              # center: σ=mean / 분위수=median (M2 — 편향 제거)
     per_group[gk] = {gap_sigma:gap, method, n_used:len(pts)}
2. valid = [g for g in per_group if not skip]
   if valid == ∅:  sigma_gap.verdict = UNKNOWN                 # 전부 skip → 거짓 PASS 금지
   else:
     top_gk = max(valid, key=lambda g: per_group[g]['gap_sigma'])   # 최대 갭 그룹 튜플
     max_gap = per_group[top_gk]['gap_sigma']
     top_gap_sensor = top_gk[-1]      # 5-tuple 마지막 = sensor_id ("C17") — 튜플 전체 오전달 방지
     top_gap_group  = list(top_gk)    # JSON 직렬화 위해 list 변환
     verdict = FAIL if max_gap >= cfg.threshold else PASS      # ≥3.0

[Nelson N1]
3. n_eval = 0
   for gk in snapshot:
     pts = qual_indicators.get(gk, [])
     if len(pts) == 0:  continue
     if sigma_ref_of(snapshot[gk], cfg.limit.k_sigma) <= EPS:  continue   # H2: σ-갭과 동일 퇴화밴드 가드 (붕괴 존→N1 폭풍 차단)
     n_eval += 1
     zones = Zones.from_quantile(c,lcl,ucl) if method=='quantile' else Zones.from_limit(c,sigma)   # 2-track
     for p in pts:
       if rule_n1([p], zones):  violations.append({group:gk, rule:'N1', value:p})   # H1: rule_n1은 list 인자(values[-1] 인덱싱)
   nelson.verdict = UNKNOWN if n_eval==0 else (PASS if len(violations) <= cfg.nelson_max else FAIL)  # M4: 유효 0건→UNKNOWN · 임계=config(nelson_max=0), 하드코딩 금지(6-1)

4. return QualBIndicators(chamber_id, qual_id, snapshot_id, evaluated_at, sigma_gap, nelson)
```

**순수성** — 상태·IO 없음. `qual_indicators`·`snapshot`은 주입, 반환은 값 객체. → 합성 데이터로 전량 유닛테스트 (결정 1).
**H2 대칭** — σ-갭·Nelson **둘 다** `sref<=EPS` 그룹을 건너뛴다. σ-갭만 가드하고 Nelson을 방치하면, 붕괴 밴드에서 `from_quantile` 존이 뭉개져 N1이 폭발(허위 위반)하는 비대칭이 생긴다.
**sref 공유 (구현 노트)** — 위 의사코드는 명료성 위해 σ-갭·Nelson 루프에서 `sigma_ref_of`를 각각 표기했으나, **구현은 그룹당 1회 계산해 `per_group`에 캐시**하고 두 지표가 공유한다(이중 계산 회피). 두 루프의 skip 집합(빈 pts + `sref<=EPS`)이 동일하므로 단일 순회로 합쳐도 무방.

---

## 5. 스냅샷 — 구조 · 로더 · 시딩

### 5-1. 저장 형태 (`qual_snapshots` — 기존 DDL, 스키마 무변경)

| 컬럼 | 값 |
|---|---|
| `snapshot_id` | `QUAL-<YYYYMMDD>-<CHAMBER>-<SEQ>` (헌법 6-4) |
| `chamber_id` | `chamber_offsets.json`의 챔버 목록 (SIM_CH_*) — 하드코딩 금지 |
| `qual_id` | 기준 Qual 이벤트 / 초기 시딩은 `seg1-bootstrap` |
| `sensor_stats` (JSONB) | `{ "<gk_str>": {center, sigma, ucl, lcl, method}, ... }` — 건강센서 8종 그룹. **gk_str = `"chamber\|recipe\|step\|window\|sensor"`**(5필드·파이프 구분). **5필드 의도적** — in-memory 5-tuple gk와 **1:1 직렬화**라 storage↔memory 대칭 유지(chamber prepend/strip 브릿지·키 버그 회피). `load_resolutions`의 4필드(chamber 제외)와 다른 건 감수 — chamber 중복 바이트는 무시가능, 여기선 대칭·안전이 우선 |
| `is_active` | 활성 스냅샷 1개 (리베이스 시 교체 — B6-3) |

- **2-track 값 (M2·M3)**: `method='sigma'` → center=**mean**, ucl/lcl=**±kσ 밴드** · `method='quantile'` → center=**median**, ucl/lcl=**q_lcl/q_ucl**(분위수 밴드 — `sigma_ref_of`가 robust-σ `(ucl−lcl)/2k`를 여기서 역산하므로 **반드시 q밴드**, σ밴드 넣으면 robust-σ=σ로 붕괴). `sigma`는 σ그룹만 판정 사용, 분위수는 참고 통계.
- **핵심**: 그룹별 dict가 `control_limits_loader.load`의 `limits[gk]`와 **핵심 5키(center/sigma/ucl/lcl/method) 동일**(+n, limit_version 불요) → σ-갭·Nelson이 control_limits와 똑같이 소비. 차이는 **σ가 rolling-500이 아니라 seg1 전체창**(F12) + **분위수 center=median**(M2).
- **⚠️ 다중 활성 방어**: `qual_snapshots`엔 `control_limits`의 `idx_control_limits_active_one` 같은 부분 유니크 인덱스가 **없다**(`init.sql`). "chamber별 is_active 1개"는 **앱 레벨 불변식** — 로더는 다수 활성 시 최신(`created_at DESC`) 1개 선택. 인덱스 추가는 PM 스키마 권고(§8 이월).

### 5-2. 로더 — `load_active_snapshot(engine, chamber_id) -> dict[gk, dict]`

`control_limits_loader.load` 패턴 계승 — `is_active` 스냅샷 행의 `sensor_stats` JSONB를 `gk` 튜플 키 dict로 파싱. 읽기 전용.

> **⚠️ group_key 타입 계약 (공유 `normalize_group_key`)** — `gk=(chamber_id, recipe_id, step, sensor_window, sensor_id)`에서 **`step`은 `int`, 나머지는 `str`**. JSONB는 키를 문자열로 역직렬화하므로, **로더 파싱과 호출자의 `qual_indicators` 키 생성이 반드시 같은 `normalize_group_key()` 헬퍼를 거친다.** 한쪽 `step='1'`(str), 다른 쪽 `1`(int)이면 튜플 매칭이 **조용히 0건 → 전 그룹 skip → 무조건 `UNKNOWN`**(control_limits_loader가 `int(step)`으로 못박은 것과 동일 계약). 공유 헬퍼로 divergence를 **구조적으로 차단**.

### 5-3. 시딩 — `seed_qual_snapshots.py`

- **표본** = seg1 복귀블록(6,824장, initial_limits와 동일 소스 — R3). 건강센서 8종(C11·C15·C16·C17·C31·C61·C62·C63) × 감시(KEEP/KEEP\*) 그룹, **C12 제외 · 과도 제외**. *(C57·C58은 KEEP\*지만 건강8종 밖 → Qual σ-갭 제외.)*
- **산식** = `compute_group_limits(values, cfg.limit)` **재사용**(M1 — 합성 필드), `values` = 그룹 **전체 seg1**(rolling-500 클립 안 함) → **전체창 σ**(F12) + 분위수 밴드(q_lcl/q_ucl).
  - **method별 center·밴드 매핑 (M2·M3)**: σ그룹 → `center=res.center`(mean)·`ucl/lcl=res.ucl/res.lcl` / 분위수그룹 → `center=median(values)`(M2)·`ucl/lcl=res.q_ucl/res.q_lcl`(M3). `compute_group_limits`는 center=mean만 주므로 분위수는 median으로 **덮어쓴다**(control_limits·recalc 공용 함수는 무수정 — 스냅샷 계층에서만 치환).
- **챔버 전개** = `seed_control_limits.build_seed_rows`의 offset shift 패턴 재사용. **챔버 수·목록은 `config/chamber_offsets.json`에서 읽음**(하드코딩 금지 — 헌법 6-1).
  - **불변식 — `shift_snapshot_row(row, offset)`**: `center`·`ucl`·`lcl`을 **같은 offset으로 평행이동**(밴드폭 `ucl−lcl`=robust-σ, σ 보존). center만 옮기고 ucl/lcl 방치 시 밴드폭·σ 왜곡 → 금지. σ그룹 `sigma`는 순수 center 이동엔 불변.
  - **⚠️ H3 — 챔버 4 vs 6**: 결정은 4챔버(SIM_CH_1~4)지만 **현재 `chamber_offsets.json`은 6챔버**(PM/simulator 소유, 6→4 재생성 미완). **라이브 시딩은 PM의 config 6→4 선행**이 전제. **테스트는 공유 config를 건드리지 않고 4챔버 offsets fixture 주입**(순수 함수라 가능 — §10). 코어가 `len(offsets)`를 따르므로 config가 6이면 6, 4면 4로 자동.
- 멱등 시딩(chamber별 is_active 1개), `sensor_stats` JSONB 직렬화.

---

## 6. 반환 계약 — `QualBIndicators`

```
QualBIndicators (dataclass, asdict()로 snake_case JSON 직렬화)
  chamber_id · qual_id · snapshot_id · evaluated_at(ISO8601 UTC)
  sigma_gap:
    verdict          # "PASS" | "FAIL" | "UNKNOWN"
    max_gap_sigma    # float | null(UNKNOWN)     ← 정량
    top_gap_sensor   # "C17" 등 | null           ← S7 "이사갔다" 표시 (tttm_comparisons 필드명 정합)
    top_gap_group    # [chamber,recipe,step,window,sensor]  ← 드릴다운
    threshold        # 3.0 (config 실어 재현성)
    per_group[]      # {group, gap_sigma, method, n_used, skipped}  ← 전 그룹 상세 (드릴다운·Scorecard)
  nelson:
    verdict          # "PASS" | "FAIL" | "UNKNOWN"   ← M4: 유효 평가 그룹 0건이면 UNKNOWN(σ-갭과 대칭)
    violation_count  # int                        ← 정량
    violations[]     # {group, rule:"N1", value}
```

- **최종 조용/요란 없음** — 4지표 종합은 A5-3. B는 2지표 사실만.
- **센서명 = C코드**(C17 등). 발표용 이름(DC_Bias) 변환은 표시 계층만 (헌법 6-4).

---

## 7. 재사용 / 신규

**재사용 (무수정)**
- `recalc_engine.sigma_ref_of(row, k)` — σ그룹=σ컬럼 / 분위수=`(ucl−lcl)/(2k)` robust-σ. **σ-갭 분모 전용**(Nelson 존은 `Zones`가 별도 산출 — 혼동 주의). + `recalc_engine.EPS`(tttm 공유) — 밴드 붕괴 `sref<=EPS` skip 가드(σ-갭·Nelson 공용).
- `initial_limits.compute_group_limits(values, cfg)` — 시딩 산식(전체창 σ + 분위수 밴드). 분위수 center만 median으로 치환(§5-3).
- `nelson_rules.rule_n1` + `Zones.from_limit` / `Zones.from_quantile` — N1 판정(2-track). **`rule_n1`은 list 인자**(`values[-1]` 인덱싱 — 단일 점은 `[p]`). **stateful `NelsonEngine` 대신 순수 함수 직접 호출**(결정 1 순수성·5장 배치엔 스트리밍 엔진 불필요).
- `seed_control_limits.build_seed_rows` offset-shift 패턴 · `control_limits_loader` 로더 패턴.

**신규**
- `qual_engine.py` — `evaluate_qual` + `QualBIndicators` + **`QualConfig`** *(⚠️ M1: RecalcConfig처럼 `limit: LimitConfig` **합성** 필드로 두고 `cfg.limit.k_sigma`로 접근 — `k_sigma`를 평탄 복제하면 AttributeError/단일소스 붕괴, RecalcConfig 도크스트링 경고)*
- `qual_snapshot_loader.py` — `load_active_snapshot` + **`normalize_group_key(chamber, recipe, step, window, sensor)`**(공유 헬퍼 — 로더·호출자 공용, step=int 강제, §5-2)
- `seed_qual_snapshots.py` — 시딩 CLI(`--dry-run`)

---

## 8. 범위 밖 · 이월

- **`qual_min_fill` config** — 그룹 최소 유효 장수 가드(예: 3/5). `qual:` = 합의안 C1·C2 블록이라 추가 시 **PM 합의안 doc-sync 필요**(헌법 4-3) → **강건화(데모 후)**. 현재 C-슬림(0장만 skip).
- **스냅샷 리베이스 (R5)** — 조용→그 Qual로 갱신 / 요란→Phase 2 확정본으로 갱신. **B6-3** 소관(가한계 3단계). B5-2는 `is_active` 스냅샷을 **읽기만**.
- **라이브 배선** — `is_qual` 웨이퍼 라우팅·구독·`fdc.agent` 발행·DB 영속 = D0/B6-3.
- **`chamber_offsets.json` 6→4 재생성** — PM/simulator 소유(E1·E3 파라미터). B5-2 라이브 시딩의 선행(H3).
- **`qual_snapshots` is_active 부분 유니크 인덱스** — PM 스키마 권고(현재 앱 레벨 불변식으로 방어, §5-1).
- **A5-3 4지표 취합 · S7 화면** = A/PM.
- **조용 경로 최종 검증** — 부트스트랩(seg1) 조용 판정은 F12 백테스트서 근사표본 2건뿐(사이클_정의 한계⑤) → **시뮬 리허설**서 최종 확인.

---

## 9. Task 단위

| Task | 내용 | 산출 |
|---|---|---|
| **B5-2-1** | 스냅샷 시딩+로더 — seg1 전체창 σ(compute_group_limits 재사용·분위수 center=median) + `normalize_group_key` 공유 헬퍼 + config 챔버 offset(테스트=4챔버 fixture) + JSONB 왕복 | `seed_qual_snapshots.py` · `qual_snapshot_loader.py` |
| **B5-2-2** | σ-갭 판정 코어 — method별 집계(A1′) + sigma_ref_of + EPS 가드 + max·verdict + C-슬림 + per_group[] + `QualBIndicators`·`QualConfig`(합성) | `qual_engine.py` |
| **B5-2-3** | Nelson N1 지표 — `rule_n1([p])` + Zones 2-track + EPS 가드(H2) + UNKNOWN(M4) → violation_count 통합 | 〃 |
| **B5-2-4** | 단위·통합 테스트 | `tests/test_qual_engine.py` · `test_qual_snapshot_loader.py` |

---

## 10. 테스트 계획

- **σ-갭 FAIL** — 한 그룹 3.2σ 이동 → `verdict=FAIL`, `top_gap_sensor` 정확, `max_gap_sigma≈3.2`
- **σ-갭 PASS** — 전 그룹 <3σ → PASS · **경계값** 정확히 3.0σ → **FAIL**(≥ 포함)
- **A1′ 집계 분기** — 같은 5장에 sigma 그룹=mean·quantile 그룹=median 사용(치우친 표본으로 값 차 확인)
- **M2 median-center** — 분위수 그룹 스냅샷 center=median(values) · **정상 챔버(median≈center)면 gap≈0**(mean-center였을 때의 ~0.32σ 바닥편향 소멸) assert
- **robust-σ (M3)** — quantile gap = `|median−center| / ((q_ucl−q_lcl)/2k)` 수치 assert · 스냅샷 ucl/lcl=q밴드 확인
- **C-슬림** — 한 그룹 0장 → per_group `skipped`, max 제외 / **전 그룹 0장 → σ-갭 `UNKNOWN`**
- **per_group[]** — 전 그룹 담김 + `top_gap_sensor`가 per_group max와 일치
- **Nelson N1 (H1)** — `rule_n1([p])` list 계약 · 한 점 밖→`violation_count≥1`·FAIL / 전부 안쪽→0·PASS · quantile은 `from_quantile` 경계로 판정
- **Nelson 퇴화밴드 (H2)** — 붕괴 밴드 그룹은 σ-갭·**Nelson 둘 다 skip**(N1 폭풍 없음)
- **Nelson UNKNOWN (M4)** — 유효 평가 그룹 0건 → `nelson.verdict=UNKNOWN`(거짓 PASS 아님)
- **반환 계약** — snake_case · `asdict()` 직렬화 · **최종 verdict 필드 없음**(2지표만)
- **시딩** — seg1 전체창 σ ≠ control_limits rolling-500 σ · offset shift(center·ucl·lcl 동일 이동·밴드폭 보존) · JSONB 왕복 = 입력 · **fixture 4챔버**(공유 config 미변경)
- **로더** — `is_active` 스냅샷 → `dict[gk]` = {center/sigma/ucl/lcl/method}(+n), `step`=int · 다중 활성 시 최신 1개
- **group_key 타입 계약** — 호출자 키 ↔ 로더 키가 `normalize_group_key` 경유 매칭(step int/str 불일치→0건→UNKNOWN 회귀 방지) caller↔snapshot 통합 테스트

---

## 11. 불변식 (헌법 정합)

- **1-3 누수 차단** — 센서 관측만 사용, C65(타겟) 미참조, wafer 단위(LOT 평균 혼입 없음). σ-갭은 스냅샷(과거 seg1) 대비라 미래정보 없음.
- **3-2 스키마 무변경** — `qual_snapshots` 기존 컬럼만(ALTER 0), 신규 테이블 0.
- **6-1 설정주도** — 임계·k·nelson_max 전부 기존 config, **신규 키 0** · 챔버 수는 `chamber_offsets.json`에서(하드코딩 0).
- **6-4 네이밍** — 반환 snake_case · 센서는 C코드(표시 계층만 변환) · `snapshot_id` 6-4 포맷.
- **4-3 문서동기** — 신규 config·스키마 0 → doc-sync 불요. 스펙 문서는 `specs/`. 신규 모듈 3종은 README B 모듈 표에 추가(계약 무변경).
- **결정 1 경계** — 발행·DB영속·리베이스·A5-3취합 미포함(순수 코어). 최종 조용/요란 판정 B 미생성.
- **대칭 가드 (H2·M4)** — σ-갭·Nelson이 퇴화밴드 skip·no-data UNKNOWN을 **동일하게** 처리(비대칭 미탐/오탐 방지).
