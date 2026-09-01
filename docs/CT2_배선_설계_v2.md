# CT²(AE) 재학습 배선 설계 v2 — 배포 자동화 확정본 (단독 정본)

> 2026-07-30 PM 작성 · **2026-07-30 갱신 (v1 흡수 — 단독 정본화)**.
> v1(2026-07-29) 대체 — **D2·D3 개정**(사람 승인 = R9 1곳 한정, 배포 = 자동 검증 게이트).
> 상태: **설계 확정 + S0~S3 배선 구현 완료 · 현 위치 S0(dry-run)** — 스위치 2종 모두 false.
> **본 문서 하나로 전체 파악이 되도록** v1의 결정표(D1~D11)·전체 도표·구현 현황·스모크 실측을 흡수했다.
> 선행 문서: `CT2_배선_설계_v1.md`(이력 보존 — 참조 불요) · `제안_CT2_자동화_평가_및_절충설계_v1.md`(원안 평가).
> *(2026-07-30: `AE_CT2_구현플래너_v1.md` 삭제 — WP 분해·갭 매트릭스(G1~G8)는 본 문서 §5·§7로 흡수 완료.
> 이제 WP 번호는 쓰지 않고 §7 잔여 번호로 지칭한다.)*
> 상류 정본: 헌법 3-3 ②·③ · 1-1 예외 3 · 1-4 각주 · 계약 §8-C/§8-D/§8-E.

---

## 0. v1 → v2 변경 요지

| # | v1 (2026-07-29) | v2 (2026-07-30) | 사유 |
|---|---|---|---|
| D2 | R9 Brief에 "CT² **생성** 동의" 병합 — 범위 = 생성까지 | R9 Brief에 **"생성 + 자동 배포" 동의** + **게이트 항목 열거** | 백지 승인 반론의 해소 장치 = 항목 열거. 엔지니어가 "무엇이 PASS인지" 알고 서명 |
| D3 | 배포 승인 **존치** (파생 thread로 사람 승인) | **자동 검증 게이트가 유일한 문지기** — PASS 시 자동 promote. 파생 thread는 **FAIL 후 수동 재요청 전용** + `auto_promote=false` 구간의 PASS 회귀 경로 | 요란 PM 빈도(수개월)에 승인 대기를 얹으면 과도기 노출(8.7일)이 길어짐. 물리 세계 무접촉(모델 출력) + 롤백 가능 = Model R2R 자동 허용과 동일 논리 |
| D12 | *(구 의미 = 배포 자동화 2단계 전환 조항)* | **재정의** — 게이트 항목·임계는 헌법에 미기재. 단일 소스 = `ae_pipeline/validate_bundle.py`, 임계 = `params.yaml` `ct.ct2_gate_*` | 구현 진화 시 헌법 재개정 반복 회피. 헌법 선례: "테이블 개수는 init.sql이 단일 소스"·"매직 넘버는 전부 params.yaml" |
| D13 | *(구 의미 = 표본 수 min 1,000 / target 2,000 · A 스윕)* | **재정의** — 게이트는 항상 **차단기로 먼저 동작**. `ct2_auto_promote: false`면 PASS는 파생 thread 승인으로 회귀, FAIL은 두 모드 공통 배포 봉인 | 개통 조건 미충족 구간에서도 게이트의 방어 가치를 즉시 확보 |
| D14 | — | **신설** — 게이트 홀드아웃 (calib 미적합 표본에서 B군 측정) | 적합 표본에서 재면 **항진명제**가 된다 (§2 D14) |

**⚠️ 결정번호 재사용 매핑 (2026-07-30 추가 — 리뷰 지적)**: v2는 D12·D13 번호를 **다른 의미로 재사용**했다.
v1을 이력으로 열었을 때 혼동하지 않도록 구 결정의 승계처를 명시한다.

| 구 번호 (v1) | 구 내용 | v2 승계처 |
|---|---|---|
| 구 D12 | 배포 자동화 2단계 전환 조항 (`ct2_auto_deploy` 승격 조건) | **§4 Stage S3 개통 조건** + 헌법 3-3 ③ (스위치명은 `ct2_auto_promote`로 교체) |
| 구 D13 | 표본 수 min 1,000 / target 2,000 · A 스윕 | **§7-3 #5(표본 수 스윕)** + `ct.ct2_min_train_wafers`·`ct2_target_train_wafers` |

**불변(v1 유지)**: D1(트리거 = `ChamberRequalified` 하류) · D4(동시 pending 1건 = Incident 기준) · D5(B 재산정 창 완전 포함) · D6(소급 필터 v1 = firm UCL/LCL 컷) · D7(연장 적재 만료 = 다음 요란 PM) · D8(converged = `limit_corrections.settle_converged` DB 조회) · D9(Kafka 되감기) · D10(창 경계 = `requalified_at` backward) · D11(개통 순서 — §4로 승계).

---

## 1. 확정 결정 (D1~D14 전체 — v1 흡수)

| # | 항목 | 결정 | 근거·상태 |
|---|---|---|---|
| D1 | 트리거 | **`ChamberRequalified`(§8-C) 하류에서만 학습 개시** | 기존 계약 재사용·순차 승인 자동 충족·R9 = 데이터 창 간접 검증 |
| D2 | 생성·배포 동의 | **R9 Brief에 "생성 + 자동 배포" 동의 병합 + 게이트 항목 열거** — CT² 전용 승인 없음 | v2 개정. 백지 승인 반론의 해소 장치 = 항목 열거 (문안은 §5 잔여 #2) |
| D3 | 배포 문지기 | **자동 검증 게이트** (`validate_bundle`) — PASS 시 자동 promote. 파생 thread `<incident_id>-CT2`는 **FAIL 후 수동 재요청** 및 `auto_promote=false` 회귀 전용 | v2 개정. 파생 thread 구조는 v1 그대로 재사용 (C 확인 7/29 — 종결 thread 연장 불가) |
| D4 | 동시 pending 1건 | 물리 thread가 아니라 **Incident 기준** — 요청 오픈 전 체크 | 헌법 1-4 각주. 자동 promote 경로는 pending을 만들지 않으므로 무관 |
| D5 | 표본 창 | **B 재산정 창 완전 포함** + 부족 시 firm 이후 전방 연장 | "관리선과 AE가 같은 정상을 봄" |
| D6 | 오염 필터 | **소급 필터** — 앞 구간(광폭 아래 수집분)을 firm 관리선으로 재평가해 위반 wafer 제외. v1 판정 깊이 = firm UCL/LCL 초과 컷 (Nelson 전체 재판정은 B 로직 — v2 검토) | 광폭 구간 "알람 꺼진 창" 오염 대응 |
| D7 | 연장 적재 만료 | **다음 요란 PM 도래 시 적재분 폐기** | 무기한 대기 방지 |
| D8 | converged 전달 | **DB 조회** — `limit_corrections.settle_converged`(§8-D, 패키지 AND). False/조회불가면 생성 금지·에스컬레이션 | B 추가 작업 0 |
| D9 | 적재 구현 | 상주 적재기 대신 **Kafka `fdc.raw` 되감기**(offsets_for_times) | 조건: retention > 정착 소요 (실운영 정책 키 — §5 잔여 #4). **단일 파티션 전제**(시뮬 환경): `rows_from_kafka`가 첫 out-of-window 메시지에서 `break`하므로 멀티 파티션 실운영은 파티션별 소진 추적 필요 — R10, 실운영 전환 시 코드 보강 |
| D10 | 창 경계 소스 | v1 = `requalified_at`(§8-C)에서 **backward `ct2_backfill_hours`** 근사 + 연장분 forward | `settled_at`·`window_start`는 계약에 없음 — 정밀화는 후속 |
| D11 | 개통 순서 | **S0~S3 4스테이지** (§4) | 현 위치 = **S0** |
| D12 | 게이트 임계 소재 | **헌법 미기재 — 단일 소스 = `validate_bundle.py`, 임계 = `ct.ct2_gate_*`** | 재개정 반복 회피 (위임 서술). ⚠️ **params 등재 미완 — §5 잔여 #1** |
| D13 | 게이트 우선 동작 | **항상 차단기로 먼저** — FAIL은 두 모드 공통 배포 봉인, PASS만 스위치 분기 | 개통 전에도 방어 가치 확보 |
| D14 | 게이트 홀드아웃 | **VAL을 반으로 갈라 뒤쪽을 calib 미적합 홀드아웃으로 남기고 거기서 B군 측정** (`gate_holdout_frac` 기본 0.5) | 적합 표본 측정 = 항진명제 (§2) |

---

## 2. 확정 루프

### 2-1. 요지 (텍스트 압축본)

```
Phase2Signal(정착) ──┬─ [B] 재산정(500장) → R9 승인 요청
                     │        Brief = firm 관리선 패키지 + CT² "생성 + 자동 배포" 동의 + 게이트 항목 열거
                     └─ [AE] 표본 창 확보 예약 (되감기 예약 — 상주 적재기 없음, D9)
R9 반려 → provisional 유지 · CT² 미개시 (창 정보만 보존)
R9 승인 → firm 발행 → ChamberRequalified(§8-C) ──▶ [CT² 오케스트레이터 · fdc.agent 구독]
  ① 가드 g1~g5: auto_generate · 멱등(ct_id) · R9 승인 실존(동의 승계) · converged(D8) · cmd 등재
  ② 표본: 되감기(D9·D10) → 소급 필터(D6) → n ≥ ct2_min_train_wafers?  미달 → 전방 연장 적재(만료 D7)
  ③ run_ct2 (8단계, subprocess 격리 · TSR-0001) → challenger 번들 (미배포)
      + val_wafers 사이드카 (val = calib·scorer 적합 / gate = 미적합 홀드아웃 — D14)
  ④ 자동 검증 게이트 (validate_bundle) → 리포트 JSON + ct_decisions
       ├─ FAIL → challenger 보존·미배포 · ct_decisions(BLOCKED) · 에스컬레이션
       │          재요청 = 엔지니어 발의(파생 thread) — 자동 오픈 금지
       └─ PASS → ct2_auto_promote ?
                   ├─ true  → promote 신호 드롭 → consumer 관리형 리로드 + DriftTracker 리셋
                   │           → approval_records(APPROVED/ct2_auto_gate) · ct_decisions(AUTO_PROMOTED) · CHANGELOG
                   │           → 배포 후 감시 → 발화 시 자동 롤백(직전 번들) + ct_decisions(ROLLED_BACK)
                   └─ false → 파생 thread 배포 승인 오픈 (SHADOW — v1 경로 회귀)
```

표본 창(시간축): `[정착 신호] ─ B 공유 500장(광폭 아래·firm 소급 필터) ─ [firm] ─ 연장분(실시간 필터) ─ [학습]`
= 관리선과 AE가 **같은 정상**을 본다.

### 2-2. 전체 도표 (v2 반영판 — v1 도표 갱신)

```
================================================================================
  AE CT LOOP (CT2) -- 요란 PM 후 AE 재학습 파이프라인 [v2 확정]
================================================================================

                      [ 요란 PM 확정 -> Phase 1 광폭 구간 ]
                                     |
                              sigma 수렴 정착
                                     |
                                     v
                      +==============================+
                      |   Phase2Signal   (B core)    |  << 트리거
                      |   chamber / settled_at /     |
                      |   converged                  |
                      +==============================+
                                     |
             +-----------------------+------------------------+
             |                                                |
             v  [B 트랙]                                      v  [AE 트랙]
     +------------------+                          +----------------------+
     | 관리선 재산정    |                          | 표본 창 확보 예약    |
     | (500장)          |                          | (B와 동일 창)        |
     +------------------+                          | * 되감기 예약 / 학습X|
             |                                     +----------------------+
             v                                                 |
     +--------------------------+                              |
     | R9 승인 요청             |                              |
     | Brief = firm 패키지      |  << 사람 개입 (유일)          |
     |  + CT2 "생성+자동배포"   |     (D2 -- 게이트 항목        |
     |    동의 + 게이트 항목    |      열거로 백지승인 해소)    |
     +--------------------------+                              |
             |                                                 |
        +----+----+                                            |
        |         |                                            |
      반려      승인                                           |
        |         |                                            |
        |         v                                            |
        |  firm corrections 발행                               |
        |         |                                            |
        |         v                                            |
        |  ChamberRequalified =======[ 개시 신호 ]===========> |
        |  (firm 관리선 가동)                                  |
        v                                                      v
  provisional holding 유지                      +-------------------------+
  CT2 미개시 (창 정보 보존)                     | 가드 g1~g5              |
                                                | auto_generate/멱등/R9/  |
                                                | converged/cmd 등재      |
                                                +-------------------------+
                                                             |
                                                             v
                                                +-------------------------+
                                                | 소급 필터 (D6)          |
                                                | 앞 500장을 firm 관리선  |
                                                | 으로 재평가             |
                                                | -> 위반 wafer 제외      |
                                                +-------------------------+
                                                             |
                                                             v
                                                     표본 N장 충족?  (?)N
                                                   +---------+---------+
                                              미달 |                   | 충족
                                                   v                   |
                                        +----------------------+       |
                                        | firm 이후 연장 적재  |       |
                                        | (만료 = 다음 요란 PM)|       |
                                        +----------------------+       |
                                                   |                   |
                                                   +---------+---------+
                                                             |
                                                             v

  - - - - - - - - - - - - -  표본 창 (시간축)  - - - - - - - - - - - - - - -

       [정착 신호]                  [firm]                  [학습 개시]
            |                          |                         |
            v                          v                         v
            +--------------------------+-------------------------+
            |    B 공유 구간 500장     |   AE 연장분 (부족 시)   |
            +--------------------------+-------------------------+
      수집  |<---- 광폭 아래 --------->|<---- firm 아래 -------->|
      필터  |    firm 관리선 소급 적용 |    실시간 Nelson 판정   |
            +--------------------------+-------------------------+
            |<=========== AE 학습셋 (N장) =========================>|
                        ^
                        +-- B 창을 완전히 포함
                            => 관리선과 AE 가 같은 정상을 봄

  - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
                                                             |
                                                             v
                                 +==========================================+
                                 |  run_ct2   (ae_pipeline/retrain.py)      |
                                 +==========================================+
                                 |  (0) 입력 어댑터 (_ts->C10 / C46 합성)   |
                                 |  (1) 정상셋 확정                         |
                                 |  (2) dedup (C64,C7,C46) + 피처 D=83      |
                                 |  (3) 시간순 TRAIN / VAL  80 / 20         |
                                 |      + VAL 절반 = 게이트 홀드아웃 (D14)  |
                                 |  (4) QuantileTransformer fit=TRAIN only  |
                                 |  (5) MLP-AE 재학습 (동일 config)         |
                                 |  (6) scorer 재산출  mu / sd / P          |
                                 |  (7) calib 앵커 + drift B0 재산출        |
                                 |  (8) 번들 저장 + manifest (sha256)       |
                                 |      + val_wafers.json 사이드카          |
                                 +==========================================+
                                                             |
                                                             v
                                     models/anomaly_ae/ae_ct2_<ch>_<stamp>/
                                     *** challenger -- 운영은 기존 번들 유지 ***
                                                             |
                                                             v
                                 +==========================================+
                                 |  자동 검증 게이트  (D3 -- 유일한 문지기) |
                                 |  validate_bundle.py  4군 11항목          |
                                 +==========================================+
                                 | [정상 조용]  홀드아웃에서 측정 (D14)     |
                                 |    recon RMSE 정상화 / L5 오경보율       |
                                 |    calib (1) 평균 / (2) P95              |
                                 | [이상 검출]                              |
                                 |    합성 프로브 주입 채점 (평가 전용)     |
                                 | [임계 건전]                              |
                                 |    표본 하한 / 앵커 간격.구조 / 홀드아웃 |
                                 | [무결성]                                 |
                                 |    7파일 / manifest sha256 / 세트 해시   |
                                 +==========================================+
                                                             |
                                                     ct_decisions 기록
                                                             |
                                               +-------------+-------------+
                                               |                           |
                                             FAIL                        PASS
                                          (exit 2)                    (exit 0)
                                               |                           |
                                               v                           v
                                    +--------------------+     ct2_auto_promote ?
                                    | 번들 보존 / 미배포 |     +---------+---------+
                                    | ct_decisions       |     |                   |
                                    |   (BLOCKED)        | false                 true
                                    | 에스컬레이션       |     |                   |
                                    | 재요청 = 사람 발의 |     v                   v
                                    +--------------------+  +-----------+   +---------------+
                                          (파생 thread)     | 파생      |   | 자동 promote  |
                                                            | thread    |   | (승인화면 X)  |
                                                            | 승인 오픈 |   +---------------+
                                                            | (SHADOW)  |           |
                                                            +-----------+           |
                                                                  |                 |
                                                                  +--------+--------+
                                                                           |
                                                                           v
                                                             +---------------------------+
                                                             | promote 신호 드롭         |
                                                             | control/ct2/promote_*.json|
                                                             +---------------------------+
                                                                           |
                                                                           v
                                                             +---------------------------+
                                                             | consumer 관리형 리로드    |
                                                             | + DriftTracker EWMA 리셋  |
                                                             |   (새 B0 기준)            |
                                                             +---------------------------+
                                                                           |
                                                                           v
                                                             +---------------------------+
                                                             | approval_records          |
                                                             |  (APPROVED/ct2_auto_gate) |
                                                             | ct_decisions(AUTO_PROMOTED|
                                                             |             /PROMOTED)    |
                                                             | CHANGELOG.md              |
                                                             +---------------------------+
                                                                           |
                                                                           v
                                                             +---------------------------+
                                                             | 배포 후 감시      <미구현>|
                                                             |  - SPC 잡는데 AE 침묵?    |
                                                             |  - ae_score 분포 붕괴?    |
                                                             +---------------------------+
                                                                           |
                                                                     이상 시 롤백
                                                                           |
                                                                           v
                                                                 직전 번들 재배포
                                                                 ct_decisions(ROLLED_BACK)

================================================================================
  범례
================================================================================
 <미구현> 배포 후 감시.자동 롤백 -- S3(자동 배포) 개통 조건 (2)의 전제 (§7-3 #9).
          ct2_auto_promote 는 이것 없이 켤 수 없다.
  (?)N    표본 수 = min 1,000(게이트) / target 2,000(권장) -- 확정은 스윕 (§7-3 #5)
  사람    개입 지점 = R9 **1곳** (D2 동의 병합). 예외 = 게이트 FAIL 후 수동 재요청,
          그리고 auto_promote=false 구간의 파생 thread 승인 (D13 차단기 모드)
================================================================================
```

---

## 3. 자동 검증 게이트 (D12 — 정본은 코드)

**단일 소스**: `src/agent_a_mlops/Autoencoder/ae_pipeline/validate_bundle.py`
**임계**: `config/params.yaml` `ct.ct2_gate_*` (근거·합의 = `docs/config_파라미터_합의안_v1.md`)
**종료 코드 규약**: `0` = PASS · `2` = FAIL(배포 봉인) · `1` = 실행 오류(판정 불가 — PASS로 오해 금지)

| 군 | 항목 | 취지 |
|---|---|---|
| 정상 조용 | recon RMSE 정상화 · L5 오경보율 상한 · calib ①(평균) · ②(P95) — **calib 미적합 홀드아웃에서 측정** | 새 정상셋에서 조용한가 |
| 이상 검출 | **합성 프로브 주입 채점** — 정상 VAL 피처에 기지 σ 배수 교란 주입 → 검출률 | 조용하기만 한 "깡통 모델" 차단 |
| 임계 건전 | B군 표본 하한 · `anchors_x` 인접 간격 비율·구조 · **홀드아웃 실존** · 적합 표본 하한 | 소표본 붕괴(앵커 뭉침) · B군 항진명제화 차단 |
| 무결성 | 7파일 완결성 · manifest sha256(항목 수 포함) · VAL·홀드아웃 세트 해시 · 채널가중 정합 · 세트 행 실존 | 세트 동기(불가침 12) · 다른 데이터로 검증 차단 |

**D14 — 게이트 홀드아웃 (2026-07-30 리뷰 반영)**: B군을 **calib 적합 표본에서 재면 항진명제**가 된다 — calib 앵커가 그 표본의 P50·P90·P98.5로 정의되므로 L5 ≈ 1.5% · 평균 ≈ 0.05 · P95 < 0.2가 **모델 품질과 무관하게** 성립한다. 그래서 `run_ct2`가 VAL을 절반으로 갈라 뒤쪽을 **미적합 홀드아웃**으로 남기고(`gate_holdout_frac` 기본 0.5, 사이드카 `gate` 키), 게이트는 그 표본에서 B군을 잰다. 확보 실패 시 퇴화는 허용하되 **D3 검사로 FAIL 노출**한다. 표본 3값(`ct2_min_train_wafers` × `val_frac` × `gate_holdout_frac`)은 `l5_budget`(구 `l5_max` — 2026-08-09 개명)의 측정 가능성과 묶여 있어 **함께 조정**해야 한다 (1000×0.2×0.5 = 100장 → 오경보 1건 = 1% ≤ 2%). ★2026-08-09 (기준 개정 v2 §1-5): 여기에 **CI 반폭** 축이 추가됐다 — 해상도가 충분해도(600장 = 0.17%p) Wilson 반폭이 ±1.25%p 면 예산 4%와 원 목표 2%를 구분하지 못한다. 반폭 ±1%p 목표 → 홀드아웃 **940장**(여유 1000장). `check_sample_triplet` 이 세 축을 함께 본다.

**프로브 격리 원칙 (불가침 2)**: 프로브는 **평가 전용**이며 학습셋에 유입되면 미탐의 자기실현이 된다. 구현은 TRAIN에서 분리된 VAL 행에만 주입하고, 주입본을 어디에도 영속화하지 않는다.

**게이트 항목 변경 규약**: 항목 **추가·임계 강화**는 config·모듈 변경만으로 가능(헌법 재개정 불요). **항목 삭제·완화는 PM 승인 필수**이며 PR에 사유 명시 (헌법 3-3 ③ ⓑ).

---

## 4. 소유권·산출물 분담

| 주체 | 산출물 | 상태 |
|---|---|---|
| **PM** | 헌법 3-3 ③·1-1 예외 3·1-4 개정 문안 · 본 설계 v2 · `params.yaml` ct2 키 · `scripts/ct2_assemble_trainset.py` · 승인/promote 오픈(`orchestrator/ct2_deploy_approval`) · gateway 라우팅 · 계약 §8-C/§8-E · **오케스트레이터 코어 대행 구현** | 구현 완료 / ⬜ params 등재·합의안 16차 |
| **A** | retrain 입력 어댑터·인터페이스(`ct2_retrain_cmd`) · **자동 검증 게이트**(`validate_bundle.py`) · 검증 리포트 · 번들 규약(4-1) · **A 소유 파일 2건 리뷰**(`ct2_orchestrator.py`·`consumer.py` 패치) · 표본 수 스윕 · 챔버별 번들 스코프 · 배포 후 감시 신호 정의 | 게이트·어댑터 완료 / ⬜ 리뷰·감시·스윕·스코프 |
| **B** | 추가 작업 **0** (converged = DB 조회 · 소급 필터 v1 = 단순 컷) | — |
| **C** | 추가 작업 **0** — 자동 promote 경로는 승인 화면 미경유. FAIL 후 수동 재요청·차단기 모드에서만 기존 파생 thread 사용 | — |

**소유권 경계 유지**: 오케스트레이터(A 파일)는 `src/orchestrator/*`를 **직접 import 하지 않고 subprocess로만 호출**한다. `approval_records`·promote 신호 write는 전부 PM 소유 모듈에 남는다.

---

## 5. 구현 현황 (2026-07-30 실사)

| 산출물 | 상태 | 비고 |
|---|---|---|
| `scripts/ct2_assemble_trainset.py` | ✅ | 되감기/CSV 겸용 · 소급 필터 · 표본 게이트. **CSV 경로 실측**(관대 한계 248/250 PASS · 타이트 86장 제외 exit 2) / Kafka 경로 미실측 |
| `src/agent_a_mlops/ct2_orchestrator.py` | ✅ 구현 / ⬜ A 리뷰 | 가드 g1~g5 · dry-run 기본 · retrain·validate·승인은 subprocess 경계(소유권) |
| `ae_pipeline/retrain.py` 입력 어댑터 | ✅ | `adapt_ct2_trainset`(`_ts→C10`·`C46` 합성) + `--input-schema` + manifest `input_adaptation` 기록 |
| `ae_pipeline/validate_bundle.py` | ✅ | 4군 11항목 · exit 0/2/1 · `val_wafers.json` 사이드카 소비 · 임계 override(`ct2_gate_*`) |
| `src/orchestrator/ct2_deploy_approval.py` | ✅ | 파생 thread 미니 그래프 + **자동 promote 경로**(리포트 발급자·번들명 재검증·R9 실존·거부/대기 이력 차단) · `incidents.lifecycle` 무접촉 |
| `src/gateway/main.py` | ✅ | ct2 그래프 병행 배선 + `request_type` 라우팅(approve/reject 2지선다 강제) |
| `src/agent_a_mlops/consumer.py` | ✅ 구현 / ⬜ A 리뷰 | promote 신호 → 관리형 리로드(= DriftTracker 리셋), 실패 시 기존 번들 유지 |
| 계약 §8-C 소비자 A 추가 · **§8-E** | ✅ | 모델 게이트 규약(파생 thread·resume 라우팅·promote 효과·생성 동의 v1). ⬜ **자동 promote 경로 반영 갱신 필요** |
| 헌법 3-3 ③ · 1-1 예외 3 · 1-4 각주 | ⬜ **문안 미반영** | 코드가 이미 인용하는 조항이 CLAUDE.md에 없음 — §6 잔여 #2 (헌법 4-3 위반 상태) |
| `params.yaml` ct2 키 | ⬜ **등재 미완** | 코드가 읽는 `ct2_auto_promote`·`ct2_validate_cmd`·`ct2_cmd_cwd`·`ct2_gate_*` 부재 + 구키 `ct2_auto_deploy` 잔존 — §6 잔여 #1 |
| WP-3 배포 후 감시·자동 롤백 | ⬜ | S3 개통 조건 ②의 전제 |

**실브로커 스모크 (7/29, 워크트리 격리 — 시연 체크아웃 무접촉)**: 이벤트 수신 → `ct_decisions` DRYRUN 1행(2회 발행에도 1행 = 멱등) → 파생 thread PENDING(타 Incident pending 3건과 무간섭) → gateway 라우팅 200 → PROMOTED + 신호 드롭 → **consumer 번들 리로드·drift 리셋 무재기동**. 전 구간 PASS. 발견 결함 2건 수정 반영(신규 기록 판정 명시 조회 · promote 신호 번들 절대경로).

**단위 테스트 (2026-07-30 실측)**: `tests/test_ct2_gate.py`(41) · `test_ct2_deploy_guards.py`(21) · `test_ct2_orchestrator_gate.py`(16) — **82건 중 81 통과 / 1 실패**. 실패 = `test_gate_params_present_in_repo_config` (params.yaml 미등재 감지 — 잔여 #1의 자동 검출. 등재하면 해소).

---

## 6. 개통 단계 (D11)

| Stage | 내용 | 게이트 조건 |
|---|---|---|
| **S0** | dry-run — 후보 감지·`ct_decisions(DRYRUN)`만 | `ct2_auto_generate=false` (**현재**) |
| **S1** | 수동 완주 — 조립→학습→게이트를 사람 개시로 1회 관통, 게이트 판정 ↔ 엔지니어 소견 shadow 대조 | 잔여 #1·#2 해소 + 실브로커 되감기 스모크 |
| **S2** | 자동 생성 개통 — `ct2_auto_generate=true`, 게이트 PASS는 **파생 thread 승인**으로 회귀(`auto_promote=false`, D13 차단기) | 챔버 스코프 + N 확정 |
| **S3** | **자동 배포 개통** — `ct2_auto_promote=true` | 헌법 3-3 ③ 개통 조건 5종 전부 + **WP-3 구현** |

**현 위치 = S0.** 코드는 S3까지 배선되어 있으나 스위치 2개(`ct2_auto_generate`·`ct2_auto_promote`)가 모두 false다.

---

## 7. 잔여 확인·후속

### 7-1. 개통 차단 (S1 진입 전 필수)

1. ✅ **`params.yaml` ct2 키 정합 (2026-07-30 완료 — WP-A1)** — `ct2_auto_promote`·`ct2_validate_cmd`·`ct2_cmd_cwd`·`ct2_gate_*` 11종 등재 + 구키 `ct2_auto_deploy` 제거 + `ct2_retrain_cmd` 실템플릿 등재. `test_gate_params_present_in_repo_config` 통과.
2. ✅ **헌법 개정 문안 반영 (2026-07-30 완료 — WP-A2)** — **3-3 ③**·**1-1 예외 3**·**1-4 각주** + 7장 실수 1줄 + 개정 이력 반영. `grep "3-3 ③" CLAUDE.md` 3건.
3. ✅ **계약 §8-E 갱신 (2026-07-30 완료 — WP-A3)** — v4.6-d, 자동 promote 2모드·`retrain_status` 값 목록(RUNNING 포함)·게이트 리포트/exit code 규약 반영.

### 7-2. 코드 리뷰 지적 (2026-07-30 — **WP-B 반영 완료** · R10 문서화 완료 · R8 마이그레이션 초안(적용 PM 판단))

| # | 항목 | 파일 | 심각도 | 처리 |
|---|---|---|---|---|
| R1 | 오케스트레이터 consumer **기본 auto-commit** — retrain 중 크래시 시 무기록 유실 | `ct2_orchestrator.py` | 高 | ✅ `enable.auto.commit=False` + 처리 성공 후 수동 commit + `max.poll.interval.ms` 확대 |
| R2 | **g2 멱등 창이 수 시간** — 학습 중 재전달 이중 생성 가능 | `ct2_orchestrator.py` | 高 | ✅ `claim_running` RUNNING 선점 — 멱등 창을 수신 시점으로 축소 + `finalize_decision` UPDATE 전이 |
| R3 | promote 스캔 `ae_predictor is not None` — 더미 폴백 시 핫 복구 불가 | `consumer.py` | 中 | ✅ `AE_MODE == "live"`로 교체 — 폴백에서도 자력 복구 |
| R4 | `load_limits`의 `assert` — `python -O`로 방어선 소멸 | `ct2_assemble_trainset.py` | 中 | ✅ 리스트 가드 + 항목별 `raise ValueError` |
| R5 | `ct2_validate_cmd` 검사가 retrain 후 위치 | `ct2_orchestrator.py` | 中 | ✅ 함수 첫머리로 호이스트 (retrain·validate 둘 다 선제 확인) |
| R6 | 라우팅 SELECT 성공 경로 commit 없음 → idle-in-transaction | `gateway/main.py` | 中 | ✅ 성공 경로 `commit()` 추가 (타 엔드포인트와 일관) |
| R7 | 되감기 tz 혼합 — naive 로컬 해석·비교 TypeError 오진 | `ct2_assemble_trainset.py` | 中 | ✅ `_utc()` 정규화 — since/until/메시지 ts 전부 aware UTC |
| R8 | `open_ct2_pending` check-then-insert **레이스** — DB 유니크 가드 부재 | `ct2_deploy_approval.py` | 低 | 🟡 마이그레이션 초안 작성(`db/migrations/0001_*.sql`) — **적용은 PM 판단**(본편 승인 전체 파급). 애플리케이션 가드 유지 |
| R9 | `run_generation` 블로킹 + max.poll.interval 초과 소음 | `ct2_orchestrator.py` | 低 | ✅ R1과 함께 `max.poll.interval.ms` 확대로 해소 (정공법 = 별 워커, 이벤트 희소로 미채택) |
| R10 | 되감기 `break` 멀티 파티션 조기 절단 | `ct2_assemble_trainset.py` | 低 | ✅ 문서화 완료 — D9 행에 "단일 파티션 전제·멀티 파티션은 실운영 전환 시 보강" 명시 |
| R11 | 실패 detail이 로그에만 — `trigger_reason` 병기 | `ct2_orchestrator.py` | 低 | ✅ `finalize_decision(reason=...)`가 `chamber_requalified/<status>` 분류자 기록 |
| R12 | GATED 산출물 누적 — 정리 규칙 | `ct2_assemble_trainset.py` | 低 | ✅ 정리 규칙 확정 (7-2 하단 — 다음 요란 PM 만료 D7) |

**R11·R12 처리 (WP-B9, 2026-07-30)**:
- **R11** — `finalize_decision(reason=...)`가 `trigger_reason`에 분류자를 병기한다: 성공(AUTO_PROMOTED·SHADOW)은 `chamber_requalified`, 그 외는 `chamber_requalified/<status>`(예: `/blocked`·`/failed`·`/gated`), SKIP은 `/r9_consent_missing`·`/converged_unknown` 등. 전부 VARCHAR(64) 이내. 상세 detail은 로그·리포트에 유지.
- **R12 정리 규칙 (확정)**: `Data/ct2/` 산출물 중 **meta의 `gate_pass=false`**(표본 미달 = 연장 적재 대기분)는 **다음 요란 PM 도래 시 폐기**한다 (D7과 동일 만료 — 무기한 대기 방지). 배치 인프라가 없으므로 v1은 **수동/스케줄 절차**: 대상 챔버의 새 `ChamberRequalified` 수신 시(또는 주간 정리) `ct2_trainset_<chamber>_*.csv` + `.meta.json` 쌍에서 `gate_pass=false`인 것을 삭제. `gate_pass=true`로 학습에 쓰인 산출물은 재현·감사용으로 보존(manifest `set_hashes`가 정본). 자동화는 WP-C 배치와 함께.

### 7-3. 후속 (개통과 병행)

4. **R9 Brief 템플릿 문안** — 동의 범위(생성 + 자동 배포) + 게이트 항목 열거를 S7 카드에 렌더. `ct2_consent` 필드 추가는 계약 2-2 절차.
5. **표본 수 스윕(WP-4, 구 D13)** — n=500/1k/2k/3922 × 시드 3, 지표 = 앵커 부트스트랩 CI·프로브 분리도·홀드아웃 오경보율. `ct2_gate_*` 임계도 함께 재산정.
6. 창 경계 정밀화(D10) — `window_start` 필드 추가 여부 (15~16차 결정).
7. **챔버 스코프(선결 과제)** — 현재 공용 모델 1개라 한 챔버 재학습이 전 챔버에 반영된다. AE 재보정(~8/3) 결과가 설계 입력. 해결 전 자동 생성 발동 금지(dry-run만 — 헌법 3-3② ⓔ).
8. 합의안 16차 등재 — ct2 키 전체 + 표본 min/target + Kafka retention 정책 키(D9 전제).
9-a. **리뷰 H1~H3 반영 (2026-07-30 — `AE재학습_파이프라인` 코드리뷰)**
- **H1 실패 이벤트 유실**: `ct2_orchestrator.main`이 `handle_event` 예외 시 커밋만 보류했는데, poll 후 인메모리 position이 전진하고 **뒤따르는 타 event_type이 정상 커밋되며 실패 offset을 지나쳐** 영구 유실됐다(pre-claim 실패면 무기록 유실 = 헌법 7장 자기 위반). → **실패 offset으로 `seek` 되감기 + backoff**(`ct.ct2_retry_backoff_sec`). 무한 재시도는 g2 선점 행이 skip-commit으로 자연 종결, pre-claim 실패는 DB 복구까지 되감기 유지(fail-stop).
- **H2 게이트 fail-open**: VAL 0장(사이드카 빈 목록·재유도 0장)이면 A1·A2만 통과한 채 **`gate_pass=True`** 가 됐다 — 배포 결정 단일 소스(3-3 ③)의 유일한 fail-open. → 채점 불가를 **A3 critical FAIL**로 환원. 게이트는 "정상 생산자 가정"이 아니라 자체로 닫힌다.
- **H3 감시 스키마 불일치**: `_fetch_window`가 존재하지 않는 `wafer_predictions.ae_score`와 `spc_violations.wafer_id`(alert 단위 테이블)를 참조해 **실행 즉시 UndefinedColumn**이었다. → **`anomaly_score` + 같은 행 `spc_flags`(JSONB)**, 조인 제거. 더미 폴백 배제(`model_version<>'dummy'` + `--since`) + `dummy_suspect_rate` 경고 추가.
  - ⚠️ **파생 발견**: `consumer.build_message`가 `spc_flags`를 **빈 배열로 발행**(TODO A3-2 미구현)하므로, 그동안 감시 **①(교차 침묵)은 판정 유예로 비활성**이고 **②(분포 붕괴)만 유효**하다. S3 개통 조건의 "감시 2종 실증"은 **A3-2 착지 후 완결** — 모듈이 이 상태를 경고 로그로 노출한다.
- 검증: `tests/test_ct2_review_h123.py` 21건(타 event_type이 실패 offset을 지나치지 않음·VAL 0장 미통과·실컬럼 쿼리·JSONB 파싱·더미 배제 등). 전체 회귀 216 passed / 2 skipped.

9. **WP-C(배포 후 감시·자동 롤백) 구현 완료 (2026-07-30)** — `src/agent_a_mlops/ct2_deploy_monitor.py`: 감시 2종(SPC↑/AE 침묵 교차·ae_score 분포 붕괴) 순수 함수 + evaluate 판정 + trigger_rollback(직전 번들 promote 신호 재드롭 · ROLLED_BACK·approval_records·CHANGELOG 3중 기록) + CLI. `ct.ct2_watch_*` 8종 등재. 테스트 20건(★Scorecard 프로브 미탐 0건 무발화 / 실명 배포 발화 포함). **잔여 = 실증**: 시뮬 Scorecard 프로브 미탐 0건 실측 + 롤백 1회 실측 + `_fetch_window` 스키마 결합(wafer_predictions × spc_violations 컬럼 정본) 확인 — 실브로커·DB 환경(S3 개통 조건 ②). 스위치 `ct2_watch_enabled`·`ct2_watch_auto_rollback` 둘 다 실증 전 false.

---

## 정직한 한계

① 본 v2는 **정적 설계 + 구현**이며 실브로커 E2E·요란 PM 실사례 관통은 미수행(S1에서 수행). 7/29 스모크는 **dry-run·승인·리로드 배선**을 실측했고 학습·게이트 실데이터 완주는 포함하지 않는다.
② 게이트 임계 초기값은 원 학습(REPORT_05) 실측에서 역산한 **출발값**이지 스윕 확정값이 아니다 — `ct2_min_train_wafers`(1,000)와 함께 WP-4에서 재산정.
③ 자동 배포의 정당성 논거(모델 출력 보정 = 물리 세계 무접촉 + 롤백 가능)는 Model R2R 선례에 기댄 것이며, **AE는 B의 Context Score에 합산되어 간접적으로 알람 판정에 영향**을 준다는 점에서 Model R2R보다 파급이 크다 — 그래서 개통 조건에 Scorecard 미탐 0건을 넣었다.
④ 프로브 검출률은 합성 교란 기준이라 실제 장비 이상과 모양이 다를 수 있다(대리 지표).
⑤ 소급 필터 v1은 firm UCL/LCL 단순 컷이라 **Nelson 패턴 위반(연속 추세 등)은 걸러지지 않는다** — B 로직 재사용은 v2 검토 항목.
⑥ **현 코드는 헌법·params 정본과 어긋난 상태다** (§7-1 #1·#2). 이 둘이 해소되기 전의 구현은 "설계대로 배선됐다"고만 말할 수 있고 "규약을 통과했다"고는 말할 수 없다.
