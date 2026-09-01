# Agent Service 운용 모드 (스위치 3종 + LLM 엔드포인트)

> 이 문서는 통합 Agent Service를 **어떤 조합으로 켜면 무엇이 도는가**를 정의한다.
> 값 자체(모델·임계값)는 `config/params.yaml`, 인프라 주소는 `.env`가 소스다 (헌법 6-1 / `app/config.py` 헤더).
> 소유: 팀원 C. 관련: `EVALUATION.md`(품질 측정), `infra/llm-ec2-experiment/README.md`(EC2 기동).
> **최종 갱신: 2026-07-24** — kafka 라이브 E2E 실증(§4)·WDAC DLL 회피(§5) 반영.

---

## 1. 스위치 3종 + 엔드포인트

에이전트는 각 계층을 mock↔real로 갈아끼우는 **경계 스위치**로 운용 모드를 정한다 (`app/config.py`).

| 스위치 | 허용값 | dev 기본 | 라이브 목표 | 읽는 위치 |
|---|---|---|---|---|
| `AGENT_SOURCE_MODE` | `replay` / `kafka` | `replay` | `kafka` | `config.py` `load_settings()` |
| `AGENT_API_MODE` | `mock` / `real` | `mock` | `real` | 〃 |
| `AGENT_LLM_MODE` | `ollama` / `api` | `ollama` | `api` (vLLM) | 〃 |
| `AGENT_LLM_BASE_URL` | 주소 | `http://localhost:11434` | `http://<ec2-ip>:8000` | 〃 |

- **모델명은 `.env`에 없다** — 엔진마다 식별자 체계가 달라 `params.yaml`이 소스다
  (`llm.gen_model`=Ollama 태그 / `llm.vllm_model`=vLLM HF repo id). `LLM_MODE=api`면
  `app/llm/factory.py`가 `vllm_model`을 자동 선택한다. `.env`에 모델명을 넣는 자리는 없다.
- **vLLM은 API 키가 없다** — 인증 대신 보안그룹(IP 제한)으로 보호한다. `.env`에 채울 키 없음.
- 엔드포인트는 배포마다 바뀌는 인프라 값이라 `.env`에 둔다 —
  `infra/llm-ec2-experiment`의 `terraform output -raw vllm_endpoint`가 실주소의 소스.

---

## 2. 조합별 실제 동작

| SOURCE | API | LLM | 상태 | 설명 |
|---|---|---|---|---|
| `replay` | `mock` | `ollama` | ✅ 돈다 | 순수 로컬 개발 |
| `replay` | `mock` | `api` | ✅ 라이브 선(오프라인) | fixtures 재생 + EC2 vLLM |
| `replay` | `real` | `api` | ⚠️ B 엔진 기동 전제 | 실력치 엔진 실호출 |
| `kafka`  | `mock` | `api` | ✅ **kafka 라이브 E2E 실증(2026-07-24)** | fdc.alert 실구독 → 그루퍼 → 파이프라인 → agent_reports |
| `kafka`  | `real` | `api` | ⚠️ B 엔진 기동 전제 | 위 + B 실력치 엔진 실호출 |

> **핵심**: `SOURCE_MODE=kafka` 가 실동작한다(2026-07-24 실측). fdc.alert 를 kafka-python 으로
> 구독 → PM 그루퍼로 Incident 묶기 → 파이프라인 → Brief 를 `agent_reports` 에 적재까지 관통.
> 이후는 PM 승인그래프(merge_reports 경로③)가 DB 에서 자동 수합하므로 그래프를 직접 호출하지 않는다.
> 실증 로그: `agent_reports 적재: report=SUP-... selected=recipe_option conf=0.75` (EC2 vLLM 실물).

---

## 3. `.env` 블록 — 모드별

### 3-A. 지금 되는 라이브 (replay + EC2 vLLM)

```bash
AGENT_SOURCE_MODE=replay          # fixtures 재생 (kafka·DB 없이 파이프라인만 확인)
AGENT_API_MODE=mock               # B 실력치 엔진 실호출하려면 real (엔진 기동 전제)
AGENT_LLM_MODE=api                # vLLM (guided_json + thinking 억제 ON)
AGENT_LLM_BASE_URL=http://<ec2-ip>:8000
# 모델명 → params.yaml llm.vllm_model (이미 EC2 모델과 일치) / API 키 불필요
```

실행:
```powershell
python -m src.agent_service.app.run     # fixtures 가 EC2 vLLM 타고 리포트 3종 생성
```

> `.env`는 손으로 고치지 말고 헬퍼로 주입 (IP는 apply마다 바뀜):
> `infra/llm-ec2-experiment/sync_llm_endpoint.ps1` (되돌리기 `-Local`).
> ⚠️ 직접 편집 시 **BOM 없는 UTF-8**로 저장 — BOM 붙으면 python-dotenv가 첫 줄 키를 못 읽어
> `.env`를 통째로 무시한다(2026-07-20 실측). 헬퍼는 방어됨.

### 3-B. Kafka 라이브 (✅ 실동작 — 2026-07-24 E2E 실증)

```bash
AGENT_SOURCE_MODE=kafka           # ✅ fdc.alert 실구독 → 그루퍼 → 파이프라인 → agent_reports
AGENT_API_MODE=mock               # B 실력치 엔진 실호출하려면 real (엔진 기동 전제)
AGENT_LLM_MODE=api                # vLLM
AGENT_LLM_BASE_URL=http://<ec2-ip>:8000

# 구독 대상 = 팀원 B가 발행하는 fdc.alert (1-2 단일 토픽)
KAFKA_BOOTSTRAP_SERVERS=localhost:9092   # 코드가 이 키 우선, 없으면 KAFKA_BOOTSTRAP (그루퍼 관례)
KAFKA_TOPIC_ALERT=fdc.alert
# KAFKA_CONSUMER_GROUP / KAFKA_MAX_POLL_INTERVAL_MS 는 미설정 시 코드 기본값
#   (consumer-group-agent-service / 600000). 필요 시 .env 로 override.
```

DB 도 필요하다(그루퍼 적재 + Brief 적재):
```bash
DATABASE_URL=postgresql://fdc_admin:change-me@localhost:5432/fdc_platform
```

실행 (터미널 2개):
```powershell
docker compose up -d postgres kafka qdrant        # 인프라
python -m src.agent_service.app.run               # 창1 — consumer (계속 뜸, Ctrl+C 종료)
python -m src.agent_service.scripts.publish_fixtures_kafka   # 창2 — fixtures 발행(E2E 도구)
```

> **발행 도구 주의**: 시뮬레이터 `mock_alert_publisher` 는 confluent_kafka.Producer 라 이 Windows
> 개발환경 WDAC 에 차단된다(§5). `publish_fixtures_kafka.py`(kafka-python)로 우회한다 — E2E 전용.
> 같은 alert 재발행은 그루퍼가 idempotent 스킵하므로, 재처리하려면 `TRUNCATE incidents,
> incident_alerts, agent_reports CASCADE` 후 발행.

---

## 4. Kafka 라이브 — 구현 완료 (2026-07-24)

**소비 주체 = A안 확정(PM)**: agent_service 가 fdc.alert 을 직접 구독한다. 그루퍼(PM,
orchestrator)는 **라이브러리로 호출**하고 수정하지 않는다(헌법 3-1 — 소유 PM, 사용 자유).

### 연결 구조

```
fdc.alert ─(우리 consumer)→ 그루퍼.handle_alert ─(incident_id)→ 파이프라인 ─(Brief)→
  agent_reports 적재 ─→ [PM 승인그래프 merge_reports 경로③ 이 DB 에서 자동 수합]
```

우리가 짠 건 **①구독 ②적재** 둘뿐. 그루퍼·그래프는 기존 재사용(DB 경유 연결이라 orchestrator 무수정).

| 파일 | 역할 |
|---|---|
| `app/source.py` | `iter_kafka_alerts()`/`make_kafka_consumer()` — kafka-python 구독, 손상 스킵, graceful stop |
| `app/report_writer.py` | `save_brief()` — Brief → agent_reports 적재. `fetch_bundle` 이 되읽는 컬럼과 정합, 멱등(ON CONFLICT), 무중단 |
| `app/run.py` `run_kafka()` | 소비→그루퍼→파이프라인→적재→오프셋 수동커밋(at-least-once), SIGINT/SIGTERM graceful |

### 같이 도는 폴러 2종 (consumer 루프와 **별도 데몬 스레드**)

`run_kafka()` 가 기동한다. consumer 루프 안에 넣지 않는 이유가 둘 다 같다 — `poll` 이 최대
1초 블로킹이고, **알람이 없는 구간에는 루프 본문이 아예 안 돈다.**

| 폴러 | 무엇을 본다 | 주기 키 | 없으면 |
|---|---|---|---|
| `app/reanalysis.py` | `agent_reports.needs_reanalysis` ([재분석] 버튼) | `agent.reanalysis_poll_sec` | 버튼이 조용히 무응답 |
| `app/release_brief.py` | 게이트웨이 `GET /chambers/status` 의 열린 inhibit (**#140**) | `agent.release_brief_poll_sec` | 챔버가 자동 정지돼도 해제 근거가 안 생긴다 |

- 주기 키가 **없으면 폴러를 안 띄운다**(조용한 기본값 금지 6-1) — 대신 기동 로그에 경고.
- 🔧 **해제 근거 Brief 가 늦게 뜨면** `config/params.yaml` 의 `agent.release_brief_poll_sec` **한 줄만**
  내린다 (코드 무변경 · 프로세스 재기동으로 반영). 체감 지연 ≈ 그 값 + LLM 작성 약 30초라
  기본 30 이면 정지 후 대략 1분이다. **하한은 10초** — 프론트 S1 이 같은 엔드포인트를 10초마다
  부르고 있어 그 눈금까지는 안전하고(`frontend/src/live/useChamberStatus.ts`), 그 밑은 게이트웨이
  쪽 `SELECT DISTINCT chamber_id FROM wafer_predictions` 부담을 프론트 이상으로 키운다.
  더 빨라야 하면 주기가 아니라 그 쿼리를 볼 자리다.
- 해제 근거 Brief 는 게이트웨이 주소가 필요하다: `.env` `AGENT_GATEWAY_BASE_URL`
  (컨테이너 `http://gateway:8000` / 호스트 `http://localhost:8000`). 미설정이면 localhost 로
  붙어 **컨테이너 안에서 조용히 연결 실패**한다 — DSN 2종과 같은 함정이다.
- 적재처는 `release_briefs` 테이블(마이그레이션 0014). `agent_reports` 가 **아니다** —
  `/incidents/recent` 가 그 테이블 최신 1행을 SUP 필터 없이 읽어 S3 큐를 오염시킨다.

### 실증된 것 (2026-07-24, docker + EC2 vLLM)
- consumer 구독·파티션 배정 → alert 소비 → 그루퍼 `신규/병합 INC-` → 게이트(B7) 동작
- `AlertModel.model_dump(mode="json")` ↔ 그루퍼 dict 키 정합 (KeyError 없음)
- idempotent 중복 스킵(재발행 29건 전건) · qdrant 3컬렉션(bge-m3) 검색 · vLLM 실물 판정
  (`selected=recipe_option conf=0.75`) · 가드 실전 작동(손잡이 강등·숫자 교정)
- `agent_reports 적재: report=SUP-...` — Brief 저장까지 관통

### 남은 것 (E2E 후속, 배선과 별개)
- 실 시뮬레이터(confluent publisher)로 발행 — Docker/Linux 에선 정상(WDAC 무관). 현 Windows 개발에선 `publish_fixtures_kafka` 우회
- 리포트 품질 측정(`analyze_quality`) — 배선이 아니라 내용 평가
- `AGENT_API_MODE=real`(B 실력치 엔진 실호출) 연결

---

## 5. kafka 라이브러리 두 종류 — 왜 (WDAC DLL 차단)

| 역할 | 라이브러리 | 이유 |
|---|---|---|
| 그루퍼(PM, orchestrator) | `confluent-kafka` | 운영(Linux/Docker)에서 정상·고성능 |
| agent_service consumer (우리) | `kafka-python` | 순수 파이썬 — WDAC 미차단 |
| E2E 발행 도구 | `kafka-python` | 〃 |

confluent-kafka 의 native `librdkafka`(cimpl) DLL 이 이 Windows 개발환경 **WDAC(애플리케이션 제어
정책)에 하드 차단**된다 — `import confluent_kafka` → *"애플리케이션 제어 정책에서 이 파일을
차단했습니다"*. TSR-0001(torch)과 달리 foreground/background 무관하게 막힌다. 순수 파이썬
클라이언트는 native DLL 이 없어 미차단이라 우리 consumer·발행 도구는 kafka-python 으로 간다.

**충돌 없음**: 두 라이브러리는 서로 독립이고, 같은 kafka 프로토콜로 같은 브로커와 통신한다.
우리 흐름은 그루퍼의 kafka(`main()`)를 안 타고 `handle_alert(dict)`(DB 쓰기)만 부르므로,
그루퍼의 confluent DLL 은 우리 프로세스에서 로딩되지 않는다. consumer group 도 달라 오프셋도 독립.
운영(Docker)에서 통일하고 싶으면 우리 consumer 도 confluent 로 바꿀 수 있다(Linux 에선 미차단).
