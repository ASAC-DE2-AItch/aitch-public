# score_ae 입력 확정 — B안(ae_score 발행) 채택 · 변경 목록 · 점검 (v2)

> **채택 결정 (2026-07-28)**: `fdc.prediction.anomaly_score` = **`ae_score`** (캘리 [0,1], 0.2 = VAL P98.5 = Qual 임계).
> B측 `score_ae`는 캐시 원본값을 재계산 없이 **밴드 lookup**만 한다. `ae_z`는 폐기가 아니라 **2단계 보류**
> — 연속 점수(Supervisor 순위 등) 수요 확정 시 신규 필드로 병행 발행.
> **개정 이력**: v1(z 발행 원안) → v1.1(실측 점검 §8 — z=2/3 오탐 15배 발견) → **v2(B안 채택·현업 근거 §9)**.
> 원안 폐기 사유는 §8에 기록 보존.
> **작성**: 에이치 · 현행 코드 전수 확인 + 실데이터 15,919 wafer 실측 기반
> **선행 문서**: `score_ae_설계_기획서_v1.md`(D1 → **본 문서로 종결**) · `AE_실연동_구현계획_v1.md`(§2-2 원안과 정합) ·
> `AE_스코어체인_실데이터_v1.pdf`(실측 근거)

---

## 0. 확정 파이프라인 (B안)

```
[A · ae_pipeline]   ae_raw ─calib(ECDF 앵커)→ ae_score [0,1]          ← 기존 그대로 (번들 무변경)
[A · consumer.py]   flush 시 AE 추론 → fdc.prediction 발행
                      anomaly_score = ae_score       ← AE_실연동_계획 §2-2 원안 그대로
                      drift_score   = ae_drift_score (불변 — 계약 변경금지 필드)
[B · spc_consumer]  fdc.prediction 구독 → PredictionCache.upsert      ← 무변경 (passthrough)
[B · collector]     build_joined → joined["prediction"]["anomaly_score"] ← 무변경 (passthrough)
[공용 · score_ae]   anomaly_score 밴드 + drift_score 밴드 → max       ← 본 구현 (설계서 §3.1(b) 경로)
```

**설계서 D1 종결**: anomaly 입력 = `ae_score` — 설계서 §3.1의 **(b) 퍼센타일 밴드가 본선**이 된다
(§3.1(a) z 경로·config 스위치 `ae_anomaly_mode`는 채택하지 않음). 유지보수 목표(score_ae = 캐시 원본을
그대로 쓰는 얇은 소비)는 동일하게 달성 — μ/σ 전달·z 재계산이 애초에 존재하지 않는 구조.

---

## 1. 변경 목록 (B안)

### 1-A. A측 — ae_pipeline 번들: **무변경 (0건)** ← B안의 최대 이득

| 원안(z)에서 필요했던 것 | B안 |
|---|---|
| Scorer에 μ_raw·σ_raw·ae_z() 추가 (A1·A2) | **불요** — ae_score는 이미 산출·검증(G3 PASS)된 기존 출력 |
| scorer.npz 스키마 확장 + 구버전 호환 처리 (A3) | **불요** — 불가침 12(세트 동기)를 건드리지 않음 |
| frozen 번들 build-scorer 재실행 (A6) | **불요** |
| CHANGELOG 기록 (C4) | **불요** (번들 무변경) |

### 1-B. A측 — consumer.py 발행 (소유: A · AE_실연동_계획 P2 그대로)

| # | 위치 | 변경 | 비고 |
|---|---|---|---|
| B1 | `build_message()` | `anomaly_score = rec["ae_score"]` · `drift_score = rec["ae_drift_score"]` (TODO(A3-3) 해소) | 계획 §2-2 표 **그대로** — 계획 개정 불요 |
| B2 | `dummy_prediction()` | **무변경** — 현행 더미 `uniform(0.01, 0.15)`는 ae_score 눈금과 이미 정합(정상 범위 값) | 원안의 눈금 교체(B2) 소멸 |
| B3 | AE 폴백 | AE 실패 시 lean-85와 독립 try-except → 더미 + 로그 (계획 P2-5 그대로) | 6-2 파이프라인 생존 |
| B4 | (기존 이슈 명시) | consumer 재시작 시 DriftTracker EWMA 상태 소실 → B0 재출발 | 본 전환 무관 기존 이슈 — P2 runbook에 기록 |

### 1-C. 계약·문서 (2-2 · 4-3 — 같은 PR에서)

| # | 문서 | 변경 | 비고 |
|---|---|---|---|
| C1 | `docs/API_Contract_명세서.md` §2 | `anomaly_score` 의미 **확정 기재**: "= `ae_score` (ECDF 캘리 [0,1] · 0.2=VAL P98.5=Qual 임계 · 0.5=P99.5 · 1.0=P99.9 클립)" | 의미 변경 아님(더미 난수 → 실분포 실값화). 스키마 승인 불요·소비자 영향 **공지**는 필수(계획 §P5-1) |
| C2 | `score_ae_설계_기획서_v1.md` → v2 | **D1 종결**(ae_score 채택 — §0), §3.1 (a) z 경로·`ae_anomaly_mode` 스위치 삭제, §4 결측표에서 μ/σ·σ=0 행 삭제, §5 config 축소(밴드 키만), §7 미결 1 종결 | 문서-코드 동기(4-3) |
| C3 | `config/params.yaml` 주석 | `qual.pass_ae_max`·`ct.ae_anomaly_threshold`에 "= anomaly_score(ae_score) 눈금과 동일" 명기 | 원안의 눈금 충돌 함정이 **정합 관계로 반전** — 명시해서 굳힘 |

### 1-D. B측 — context_score 소비

| # | 파일 | 변경 | 비고 |
|---|---|---|---|
| D1 | `config/params.yaml` | flat 키 등재(B7 관례 — `spc.` 내): `context_score_ae_band_edges: [0.2, 0.5, 1.0]` · `context_score_ae_band_pts: [10, 20, 30]` · `context_score_ae_drift_lo: 0.3` · `context_score_ae_drift_hi: 0.7` · `context_score_ae_pts_drift_lo: 10` · `context_score_ae_pts_drift_hi: 20` · `context_score_ae_aggregate: max` | anomaly 밴드 경계 0.2/0.5/1.0은 **calib 앵커와 동일 눈금**(오탐률 1.53/0.51/0.10% 실증). 배점(pts)은 가안 — Scorecard 확정(미탐 0 하 호출 최소화). drift 경계 0.3/0.7은 가안 — 유효 밴드폭 0.07 포화 특성상 실측 튜닝 |
| D2 | `config.py` `ContextScoreConfig` | 위 7키 필드 추가 + `load()` 명시 매핑 (`_key` 패턴 — 부재 시 명시 실패) | edges·pts 길이 불일치 시 raise (`len(edges)==len(pts)`) |
| D3 | `axes/score_ae.py` | **본 구현**: 결측 가드 → anomaly 밴드 lookup → drift 밴드 → `max` 집계 (§4-3 의사코드) | z·μ/σ 로직 전무 — 순수 lookup |
| D4 | `context_score.py` | s3 dispatch placeholder → `score_ae` 실연결 | |
| D5 | `tests/test_axes.py` | score_ae 케이스 추가 (§4-4) | |
| D6 | `prediction_cache.py` · `collector.py` | **코드 무변경.** 주석의 anomaly_score 의미만 갱신 | passthrough 확인 완료 |

---

## 2. 점검 ① — 파이프라인 연결 무결성

| 홉 | z 원안 | **B안** |
|---|---|---|
| consumer 발행 | 값 교체 + 계약 의미 변경 | 값 실값화만 — **계약 의미 그대로** |
| 캐시·collector | passthrough (무해) | 동일 |
| score_ae | z 눈금 전제 | [0,1] 눈금 — calib 앵커·Qual 임계와 **한 눈금** |
| DB (`wafer_predictions`) | 과거 더미 [0,1] + z 혼재 오염 | **혼재 없음** — 더미도 실값도 [0,1] (실값화 시점만 model_version으로 식별) |
| Dashboard | [0,1] 게이지 가정 파손 위험 | 위험 소멸 |
| Qual·CT 임계 (0.2) | 눈금 충돌 함정 | **정합** — 같은 눈금이라 재사용 가능 (C3에서 명문화) |
| AE 버전 식별자 | 1단계 승격 필수 (P3) | **2단계 유지 가능** — 눈금이 [0,1] 불변이라 긴급성 소멸. 단 calib 재산출 시 앵커 이동은 있으므로 2단계 `ae_calib_version` 추가는 여전히 권장 |

**남는 무결성 유의점 1건**: calib 앵커는 CT②마다 재산출된다(불가침 12 세트). 눈금 틀([0,1]·0.2=P98.5 의미)은
불변이지만 **같은 ae_raw가 다른 score로 매핑**될 수 있다 — 밴드 경계(0.2/0.5/1.0)는 "꼬리확률" 의미로
고정이므로 B측 재튜닝은 불요하나, 사후 분석에서 score 시계열을 모델 버전 경계로 끊어 봐야 한다
(→ 2단계 `ae_calib_version` 권장 근거).

---

## 3. 점검 ② — 버그

| # | 위험 | 상세 · 대응 |
|---|---|---|
| b1 | ~~구버전 scorer.npz KeyError~~ | **소멸** — 번들 무변경 |
| b2 | ~~σ_raw = 0~~ | **소멸** — z 산출 없음 |
| b3 | **NaN 침묵 통과** | `json.dumps(NaN)` → 발행 → 파싱 → 밴드 비교 전부 False → 0점 침묵. **유지** — 발행측 `math.isfinite` 검증 + score_ae측 NaN 가드(WARNING) 이중 방어 |
| b4 | **1.0 클립 동점** | ae_score ≥ 1.0(P99.9 초과분)은 전부 최상위 밴드 30점 — 초고심각도 구분 소실. 실측: 포화 607장의 raw 3.42배 차이가 같은 점수. **버킷 산식에서는 z도 동일했음(§8-P2)** — 게이트 용도로는 무해, 연속 점수 전환 시 ae_z 2단계로 해소 |
| b5 | **밴드 경계 off-by-one** | 경계 포함 방향 통일: `>=` (0.2는 발화). calib "0.2 = P98.5 = 상위 1.5% **이상**" 정의와 일치시킴 — 테스트로 고정 (§4-4) |
| b6 | wafer_id 접미사 (`…_CH_3`) | 캐시 조인 동일 문자열 — 무해. 패리티 테스트에 접미사 케이스 포함 (유지) |
| b7 | 순서 역전 | 캐시 ts 역전 방어 기존 구현 확인 — 무해 (유지) |

---

## 4. 점검 ③ — 예외처리

### 4-1. 발행측 (consumer.py — 계획 P2 그대로)

- AE 추론 try-except, lean-85와 **독립** 폴백 + WARNING.
- `ae_score`·`ae_drift_score` `math.isfinite` 검증 → 비유한이면 더미 폴백 + WARNING (b3 1차 방어선).
- 번들 로드 실패 → AE 더미 모드 기동 + ERROR (기존 `_init_predictor` 패턴).

### 4-2. 소비측 (score_ae) — 결측 정책 확정판

| 상황 | 처리 | 로그 |
|---|---|---|
| `prediction` None / 빈 dict | `return 0.0` (SPC·TTTM 무지연 — ★③) | collector가 이미 INFO |
| `anomaly_score` 없음/None | Anomaly 채널 0 | — (정상 결측) |
| `anomaly_score` NaN/비수치/음수/1 초과 | Anomaly 채널 0 | **WARNING** (계약 위반 신호 — [0,1] 계약이므로 범위 밖도 위반) |
| `drift_score` 없음/None/NaN/비수치/범위 밖 | Drift 채널 0 | NaN·비수치·범위 밖만 WARNING |
| cfg 키 부재·edges/pts 길이 불일치 | 명시적 raise | 기동 실패로 표면화 |
| 두 채널 모두 0 | `0.0` | — |

z 원안 대비: μ/σ 부재·σ=0 행이 **구조적으로 소멸**했고, 대신 [0,1] **범위 검증**이 가능해졌다
(z는 unbounded라 범위 가드 불가) — 계약 위반 감지력이 오히려 상승.

### 4-3. 의사코드 (D3)

```python
def score_ae(prediction: dict, cfg) -> float:
    """AE 축 기여점수 — anomaly_score([0,1] 캘리)·drift_score를 밴드 매핑 후 max 집계 (D1 종결: ae_score 채택)."""
    if not prediction:
        return 0.0
    s = _as_unit_interval(prediction.get("anomaly_score"), warn_key="anomaly_score")  # None/NaN/범위밖 → None+WARN
    d = _as_unit_interval(prediction.get("drift_score"), warn_key="drift_score")
    s_anom = 0.0
    if s is not None:
        for edge, pts in zip(reversed(cfg.ae_band_edges), reversed(cfg.ae_band_pts)):
            if s >= edge:
                s_anom = pts
                break
    s_drift = 0.0 if d is None else (
        cfg.ae_pts_drift_hi if d > cfg.ae_drift_hi
        else cfg.ae_pts_drift_lo if d > cfg.ae_drift_lo else 0.0)
    return max(s_anom, s_drift)          # A3 그룹 내 합산 금지 (이중계상 실측 +72%)
```

### 4-4. 테스트 (D5)

밴드 경계 s=0.19→0 / s=0.2→10 / s=0.5→20 / s=1.0→30 (`>=` 방향 고정 단언) · drift 0.3·0.7 경계 ·
동시 발화 max(30,20)=30 (합산 50 아님 명시 단언) · `prediction=None`→0 · `anomaly_score=-0.1`·`1.5`·NaN·"oops"→0+WARNING ·
순수성(동일 입력 2회 동일 반환) · cfg edges/pts 길이 불일치 → raises.

---

## 5. 점검 ④ — 성능

변경 증분 사실상 0 (z 원안과 동일 평가). 발행 값은 이미 계산된 필드의 참조 교체, score_ae는 비교 수 회.
실측이 필요한 유일 항목은 AE 실연동 자체(추론 3.2ms/wafer — 계획 P4 리허설 몫)이며 본 결정의 추가분은 없다.

---

## 6. 실행 순서 (PR 분할 — 원안 3개 → **2개**)

```
PR-1 [A] consumer 실값 전환: B1~B4 + 계약 C1 + 계획 P3 패리티     → 소비자 공지(§P5-1) · 1인 리뷰
PR-2 [B] 소비측: D1~D6 + 설계서 C2 + params 주석 C3 + 테스트      → 1인 리뷰
의존: 없음 — 병렬 가능 (score_ae는 결측 0 처리라 실값 도착 전엔 0 기여로 무해)
```

원안의 PR-1(ae_pipeline 번들 개조)이 통째로 사라진 것이 B안의 실행 리스크 감소분이다.

**패리티 게이트 (머지 전 필수)**: 같은 wafer에 대해 ① `infer score` CLI ② consumer 경로의
`ae_score`·`ae_drift_score` 대조 — round(4) 일치 (계획 P3 그대로).

---

## 7. 하지 말 것

- **`ae_score` 산식(ECDF 앵커) 변경 금지** — Qual 0.2(C2)·G3 게이트·drift B0 눈금이 걸려 있다.
  변경은 CT② 재학습 절차(Qual 검증 + 승인, 3-3)로만.
- **`drift_score` 필드 불변** — 계약 변경금지 필드.
- **B측에서 ae_raw 역산·z 재계산 금지** — 얇은 소비 원칙. 연속값이 필요해지면 2단계 `ae_z` 필드로.
- **밴드 경계(0.2/0.5/1.0)를 calib 앵커와 다른 값으로 임의 변경 금지** — 이 경계의 의미는 "꼬리확률"이며
  calib와 한 몸이다. 바꾸려면 Scorecard 근거 + 설계서 동기(4-3).
- 알람 판정에 `anomaly_score` 단독 사용 금지 — AE는 알람 권한 없음(1-2), Context Score 합산 경유만.

---

## 8. (기록 보존) z 발행 원안 폐기 사유 — 실측 (v1.1, 2026-07-28)

> v1 원안은 `anomaly_score = ae_z`(z 표준화) 발행이었다. 실데이터 점검에서 아래가 확인되어 폐기.

- **P1 · z=2/3 경계 오탐 15배**: VAL 실측 z≥2 = 3.98%(정규 가정 2.28%) · z≥3 = **1.94%**(가정 0.13%).
  ae_raw 왜도 3.43 비대칭 → σ 경계 구조적 과발화. 최고 버킷(z≥3, +25)이 최저 밴드(score≥0.2, +10)보다
  자주 발화하는 모순. 교정하려면 분위수 정합(z≈3.31/5.20)이 필요 — "2σ/3σ 보편값" 장점 소멸.
- **P2 · 버킷 산식에서 포화 해상도 이득 0**: z버킷만 발화 2,020장 전부 과발화 방향(역방향 0장).
  분위수 정합 후 두 방식 판정 사실상 동일. z의 잔여 이득은 해석성·연속 점수 대비뿐.
- **P3 · 눈금 출처 식별자 부재**: z 눈금은 CT②마다 변동, 발행 메시지에 AE 버전 없음 + DB 눈금 혼재.
- **P4 · 재학습 견고성 재평가**: z는 μ·σ 이동만 흡수, 분포 모양 변화는 못 흡수. calib(분위수 재앵커)가
  꼬리확률 의미를 정확히 보존 — 버킷 의미 보존은 ae_score가 우월.

---

## 9. 채택 근거 — 현업 기준 (B안이 맞는 이유)

1. **팀 자체 전례 (헌법 1-1 예외 2, 멘토 확인)**: 비정규(치우침) 분포에 ±3σ가 구조적 오탐을 내는 경우
   **분위수 관리선으로 전환**한 것이 이 프로젝트의 확정 전례다 (KEEP\* 그룹: ±3σ 오탐 4.43% → 분위수 0.30%).
   ae_raw(왜도 3.43)에 σ 버킷을 새로 도입하는 것은 그 전례를 역행한다. ae_score = ECDF 분위수 캘리 =
   전례와 같은 계열의 해법.
2. **오탐 관리 문화**: fab 운영의 1순위 고질은 알람 피로다. 이 프로젝트도 L5 오경보 ≤2%를 G3 하드 게이트로
   운영했고 ae_score 0.2 앵커가 그 실증치(1.53%)를 보유한다. 검증 안 된 새 눈금(z)으로 갈아타는 것은
   G3 실증을 버리는 것.
3. **눈금 안정성 = 현업 "스펙" 사고방식 (헌법 6-4)**: 현업은 고정된 관리 한계("스펙") 기준으로 사고한다.
   [0,1]·0.2=Qual 임계는 이미 제도화된 눈금(C2 합의·대시보드·Qual·CT 키 공유)이고, 재학습마다 의미가
   흔들리는 z 눈금은 운영자 멘탈 모델·이력 분석 양쪽에 비용을 만든다. 상용 FDC 제품군이 다변량 지표를
   고정 눈금 health index로 제시하는 관행과도 일치.
4. **최소 변경 원칙**: B안은 AE 번들 무변경(불가침 12 무접촉)·계약 의미 무변경·기존 실연동 계획(§2-2)과
   완전 정합 — 승인·리뷰 표면적이 가장 작다. σ 언어가 필요한 엔지니어 표시("몇 σ")는 표시 계층에서
   변환하면 되고(6-4 센서명 원칙과 동일 구조), 연속 심각도가 필요해지는 시점에 `ae_z`를 2단계 필드로
   추가하면 A안의 이득도 회수된다.
