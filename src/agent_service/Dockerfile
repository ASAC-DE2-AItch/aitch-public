# =============================================================================
# Agent Service (팀원 C) — 컨테이너 이미지 · C6-2
#
# 빌드는 **repo 루트에서** 한다 (코드가 `python -m src.agent_service.app.run` 로 돌고
# config/params.yaml 을 repo 루트 기준으로 찾기 때문 — app/config.py REPO_ROOT=parents[3]):
#
#     docker build -f src/agent_service/Dockerfile -t aitch-agent-service .
#
# ── 무엇을 굽고 무엇을 밖에 두나 ────────────────────────────────────────────────
#   🔥 이미지에 굽는 것 : 코드 · 파이썬 라이브러리 · **CPU 전용 torch** · config/fixtures
#   📦 볼륨으로 붙이는 것: 임베딩 모델 가중치(BAAI/bge-m3 약 2GB) — HF_HOME
#   🌐 이미지 밖(HTTP)  : LLM. vLLM/Ollama 는 별도 서버라 엔드포인트 URL 만 주면 된다
#
# 모델 가중치를 굽지 않는 이유: 코드는 하루에도 여러 번 리빌드하지만 모델은 고정이다.
# 같이 봉인하면 매번 2GB 를 다시 굽고 전송하게 된다. EC2 부트스트랩도 같은 전략을 쓴다
# (user_data.sh.tftpl → `aws s3 sync ${hf_cache_s3_uri} /root/.cache/huggingface/hub/`).
#
# 볼륨 채우는 법 (최초 1회 · HF 재다운로드 회피):
#     docker volume create aitch_hf_cache
#     docker run --rm -v aitch_hf_cache:/models -v ~/.aws:/root/.aws:ro amazon/aws-cli \
#       s3 sync s3://aitch-agent-service/hf_cache/ /models/hub/ --exclude "*model.onnx*"
#   · `--exclude "*model.onnx*"` — ONNX 런타임용 2.1GB 로 FlagEmbedding 은 쓰지 않는다.
#     우리가 쓰는 건 같은 스냅샷의 `pytorch_model.bin`(2.1GB). 제외하면 6.9GB → 4.7GB.
#   · ⚠️ **Git Bash(MSYS)에서는 `MSYS_NO_PATHCONV=1` 을 앞에 붙일 것.** 안 붙이면 `/models`
#     가 `C:/Program Files/Git/models` 로 치환되어 **볼륨이 아니라 컨테이너 내부에 받고
#     `--rm` 으로 사라진다**(2026-07-29 실측 — 4.7GB 를 받고도 볼륨이 비어 있었다).
#     PowerShell·cmd 는 그대로 써도 된다.
#   비워둬도 동작은 한다 — FlagEmbedding 이 HuggingFace 에서 자동으로 받는다(느릴 뿐).
# =============================================================================

FROM python:3.11-slim
# 3.11 고정 — 개발/검증 환경과 동일(로컬 3.11.9). pandas 3.0.4 DLL 이슈 등 버전 사고를
# 피하려면 런타임 파이썬을 흔들지 않는 편이 안전하다.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # FlagEmbedding/huggingface_hub 가 모델을 찾는 곳 = 볼륨 마운트 지점
    HF_HOME=/models \
    # 오프라인 강제는 하지 않는다(볼륨이 비었을 때 자동 다운로드로 살아남게).
    # 폐쇄망 배포 시 HF_HUB_OFFLINE=1 을 런타임에 주면 된다.
    PYTHONPATH=/app

WORKDIR /app

# psycopg[binary]·psycopg2-binary 는 휠로 오므로 libpq-dev 빌드툴이 필요 없다.
# curl 만 헬스체크용으로 남긴다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# ── 1) CPU 전용 torch 를 **먼저** 설치 ─────────────────────────────────────────
# FlagEmbedding 이 torch 를 전이 의존으로 끌어오는데, 기본 PyPI 는 CUDA 빌드라
# 2GB+ 가 붙는다. 우리는 질의 임베딩만 하므로 GPU 가 필요 없다 → CPU 휠로 선점 설치해
# 뒤따르는 FlagEmbedding 설치가 이미 충족된 것으로 보고 넘어가게 한다.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

# ── 2) 서비스 의존성 ──────────────────────────────────────────────────────────
# 코드보다 먼저 복사한다 — 레이어 캐시가 살아 코드만 고칠 때 재설치를 건너뛴다.
COPY src/agent_service/requirements.txt /app/src/agent_service/requirements.txt
RUN pip install -r /app/src/agent_service/requirements.txt

# ── 3) 코드·설정 ──────────────────────────────────────────────────────────────
# config/params.yaml 은 repo 루트 기준으로 해석된다(app/config.py REPO_ROOT).
COPY config/ /app/config/
# run.py 가 IncidentGrouper 를 import 한다(kafka 모드) — orchestrator 도 함께 넣는다.
# ※ orchestrator 의 confluent_kafka·langgraph 는 **우리 경로에 안 걸린다**(2026-07-29 확인):
#   incident_grouper 는 무거운 의존을 전부 지연 import 하고, confluent_kafka 는 events.py,
#   langgraph 는 approval_graph.py 쪽이라 `IncidentGrouper`+`load_params` 만 쓰는 우리는
#   requirements.txt 만으로 충분하다. → 이미지에 그 둘을 넣지 않는다.
# ※ 구 주석: "`src/common/` 은 넣지 않는다 — 어느 쪽도 참조하지 않는다(전수 grep 확인)".
#   **2026-08-08 그 전제가 깨졌다** — `recipe_engine.py:22` 가 `src.common.config_util` 을 쓰고,
#   그 모듈을 `app/pipeline.py:164` 가 `AGENT_API_MODE=real` 에서 지연 import 한다(#109).
#   *"생기면 그때 추가"* 라고 적어둔 그때가 왔다.
COPY src/__init__.py /app/src/__init__.py
COPY src/orchestrator/ /app/src/orchestrator/
COPY src/agent_service/ /app/src/agent_service/
COPY src/common/ /app/src/common/
# recipe 정량 엔진 조달 (#142) — `.dockerignore` 가 이 3개만 통과시킨다. 디렉토리째 COPY 해도
#   컨텍스트에 그 셋뿐이라 나머지 B 코드는 들어오지 않는다.
COPY src/agent_b_spc/ /app/src/agent_b_spc/

# 볼륨 마운트 지점 — 비어 있어도 기동은 된다(HF 자동 다운로드로 폴백).
VOLUME ["/models"]

# 루트로 돌리지 않는다. /models 는 볼륨이라 소유권을 런타임에 맞춰야 하므로
# 미리 만들어 두고 넘긴다.
RUN useradd --create-home --uid 10001 agent \
 && mkdir -p /models \
 && chown -R agent:agent /models /app
USER agent

# 스위치 3종 기본값 — 운영 값은 compose/.env 로 덮는다 (RUN_MODES.md).
# ⚠️ LlmMode 는 ollama|api 뿐이다(mock 없음 — MockBackend 는 테스트가 주입한다).
#    잘못된 값이면 load_settings 가 기동 시 즉시 죽는다(조용한 기본값 금지, 헌법 6-1).
ENV AGENT_SOURCE_MODE=replay \
    AGENT_API_MODE=mock \
    AGENT_LLM_MODE=api

CMD ["python", "-m", "src.agent_service.app.run"]
