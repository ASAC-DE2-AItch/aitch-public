# AITCH (AI + ETCH)

> FDC가 감지하고 SPC/TTTM이 분석한다. AI Agent가 Recipe·실력치·정비 조치안을 근거와 함께 올리면 엔지니어의 승인으로 루프가 닫힌다.

반도체 Etch(식각) 공정의 FDC Trace 데이터를 다루는 Human-in-the-Loop 능동형 제조 AI 플랫폼입니다. SK hynix 연계 ASAC 데이터엔지니어 과정(2기)에서 4인 팀으로 개발해 2026년 8월에 발표했습니다. 이상이 감지되면 AI가 그 성격을 판별해 근거를 모으고 조치안을 만듭니다. 실제 적용은 엔지니어 승인을 거쳐야 일어납니다. 물리 세계를 건드리지 않는 보정(모델 bias, 정기 리캘리브레이션)만 변동폭 상한과 이력 기록을 조건으로 자동으로 돌립니다. 판단의 근거를 SHAP과 RAG로 설명하고 승인 게이트로 감사 추적을 남기는 것이 설계의 중심입니다.

이 저장소는 원본 레포의 공개용 스냅샷입니다. 실데이터와 내부 기록을 뺀 1커밋으로 구성했습니다. 자세한 범위는 아래 데이터·민감정보 고지에 적었습니다.

## 무엇을 하는가

예측(A), 감지(B), 분석(C), 판단(E), 보정·조치(D)의 5개 파이프라인이 Kafka로 이어집니다.

| 파이프라인 | 하는 일 |
|---|---|
| A · 예측 | wafer 단위 C65(fail bit count) 실시간 예측 (XGBoost + SHAP Top-3), Autoencoder anomaly/drift score |
| B · 감지 | 챔버 건전성 추적, PM 전후 분포 변화 조기 탐지 |
| C · Smart SPC | Nelson Rules N1~N8 판정 + TTTM(동일 공정 챔버의 fleet median 비교)이 단일 경보 채널 `fdc.alert`로 수렴 |
| D · R2R·보정 | Recipe 튜닝안 생성과 승인 후 적용, 실력치(관리 기준선) 재설정, wafer HOLD와 Disposition |
| E · RAG Agent | 지식베이스 검색으로 리포트 3종(Recipe·실력치·정비)을 병렬 생성. Supervisor가 통합 판정 |

Supervisor의 판단 프레임은 4지선다입니다. 진짜 장비 이상(정비 + 스크랩)인가, 공정 조건 이탈(Recipe R2R 튜닝안)인가, 기준선 노후(실력치 재설정)인가, 모두 아닌가(근거 패키지와 함께 에스컬레이션). 같은 챔버에서 겹치는 시간대의 알람은 하나의 Incident로 묶여 승인 요청은 Incident당 동시 1건만 열립니다.

모델도 스스로 갱신됩니다. Model R2R은 사이클 내 잔차 bias를 상한 안에서 자동 보정하고 매 갱신을 기록합니다. 예측 모델(XGBoost)은 일간 슬라이딩 재학습 후 champion-challenger 게이트를 통과해야 교체됩니다. Autoencoder는 요란 PM 승인 하류에서 재학습이 생성되고 자동 검증 게이트 전 항목 PASS가 배포 조건입니다.

## 아키텍처

```
[시뮬레이터]  실데이터 리플레이 x 가상 챔버 4대 + 시나리오 주입
    |
    | fdc.raw (Kafka)
    +-------------------------+
    v                         v
[A. 추론 파이프라인]        [B. SPC 엔진]
 XGBoost + AE  ---------->  Nelson + TTTM + Context Score
        fdc.prediction        |
                              | fdc.alert (유일한 경보 채널)
                              v
                    [Incident 병합 백엔드]
                              v
        [C. 통합 Agent: 리포트 3종 병렬 + Supervisor 판정]
                              v
[LangGraph 승인 게이트]  승인 / 수정 / 반려 / 에스컬레이션
    |
    | fdc.correction (limit | recipe)
    v
 관리 한계 갱신 · Recipe 반영 · 정비/Disposition · Qual 검증 후 재학습
```

Kafka(토픽 계약 관리) · PostgreSQL(17개 테이블) · Qdrant(RAG, payload 필터) · LangGraph(승인 상태머신, Postgres 체크포인터) · React + FastAPI 대시보드.

## 팀 (4인)

| 담당 | 역할 |
|---|---|
| PM/PO 윤준호 | 시뮬레이터, LangGraph 승인 인프라, Incident 백엔드, React 대시보드, 문서와 계약 관리 |
| 팀원 A | 추론 파이프라인(XGBoost + AE), Model R2R, 재학습 루프 2종, Qual 취합 |
| 팀원 B | Nelson/TTTM/Context Score, 실력치 엔진(재산정과 한계 갱신) |
| 팀원 C | RAG 지식베이스(Qdrant), 통합 Agent 리포트 3종, Supervisor 판단 로직 |

## 어떻게 협업했는가

이 프로젝트에서 코드만큼 공들인 것이 협업 체계입니다.

프로젝트 헌법. `CLAUDE.md` 한 파일에 불변 원칙을 못박고 모든 코드 리뷰의 기준으로 삼았습니다. 승인 없는 물리 조치 금지, `fdc.alert` 단일 경보 채널, 데이터 누수 차단(타겟 C65의 feature 사용 금지, Wafer ID 그룹 분할), Supervisor 통합 구조가 1장의 네 기둥입니다. 개정은 PM 승인을 거치며 두 달 동안 20회가 넘는 개정 이력이 문서 안에 남았습니다. 헌법 원문에는 팀 내부 기록이 많아 이 저장소에는 싣지 않고 private 원본 레포에 보존했습니다. 이 README의 서술이 그 요약입니다.

PR 전량 리뷰. 개발 기간 동안 PR 186건을 만들었고 전부 리뷰를 거쳐 머지했습니다. 사람 1인 리뷰에 CodeRabbit과 Claude의 자동 리뷰(헌법 기준 판정), pytest 유닛 게이트가 더해집니다. main과 dev 직접 push는 금지했습니다.

dev 통합 브랜치. `feat/* → PR(1인 리뷰 + CI) → dev → 릴리스 PR(전원 승인) → main`의 3계층입니다. main은 항상 시연 가능한 상태를 유지합니다.

API 계약 기반 병렬 개발. Kafka 토픽과 JSON 스키마를 계약 문서(`docs/API_Contract_명세서.md`)로 고정하고 4명이 각자 파트를 병렬로 개발했습니다. 기존 필드의 이름 변경과 삭제는 금지, 스키마를 바꾸면 소비자 측 팀원의 리뷰와 문서 동시 갱신이 의무입니다.

트러블슈팅 기록. 30분 넘게 걸린 디버깅은 `docs/adr/`에 TSR로 남기고 재발 방지 규칙 한 줄을 헌법에 추가하는 것까지가 한 세트입니다. TSR 6건과 ADR, 수십 항의 실수 목록이 그렇게 쌓였습니다. 이 저장소의 `docs/adr/`에서 볼 수 있습니다.

## 실행

```bash
# 인프라 기동 (Kafka + PostgreSQL + Qdrant)
docker compose up -d

# 환경 변수
cp .env.example .env   # 키·주소를 채워 넣기

# 파이썬 의존성
pip install -r requirements.txt
```

실데이터와 학습된 모델 아티팩트가 스냅샷에서 빠져 있어 전 구간 실행은 이 저장소만으로는 제한됩니다. 구조와 계약, 시뮬레이터·합성 지식베이스 코드는 그대로 볼 수 있습니다.

| 궁금한 것 | 문서 |
|---|---|
| 모듈 입출력 JSON 계약 | `docs/API_Contract_명세서.md` |
| PM 사이클 세계관 | `docs/사이클_정의_v1.md` |
| 컬럼 의미 | `docs/데이터_사전_v1.md` |
| DB 네이밍 규칙 | `db/init.sql` + `docs/DB_네이밍_컨벤션_v1.md` |
| 시뮬레이터 스펙 | `docs/시뮬레이터_스펙_v1.md` |
| 화면 정의 (S0~S11) | `docs/프론트_화면_정의서_v1.md` |
| 파라미터 합의 근거 | `docs/config_파라미터_합의안_v1.md` |
| 트러블슈팅·아키텍처 결정 | `docs/adr/` |

## 데이터·민감정보 고지

다음은 이 스냅샷에서 제외했습니다.

- 경진대회로 제공받은 실데이터와 정답지, 그로부터 학습한 모델 바이너리
- 내부 강의자료와 그 파생 문서
- 회의록, 일정, 팀 내부 운영 문서, 개인 정보
- API 키 등 비밀값 (`.env.example`의 플레이스홀더만 포함)

동작하는 전체 파이프라인은 요청 주시면 시연으로 보여드릴 수 있습니다.

---

*SK hynix M&T Data Science 연계 · ASAC 데이터엔지니어 2기 · 2026.06 ~ 2026.08*
