# =============================================================================
# agent_a_mlops — 예측 컨슈머(a-pred) 컨테이너 이미지
#
# ⚠ `src/agent_a_mlops/` 는 팀원 A 소유(헌법 3-1). PR #121 에서 PM 이 컨테이너화 전체를
#   은비 주도로 위임했고(작성 은비·리뷰 소유자), 그 답에서 a-pred 포함을 밝혔다. **리뷰는 A.**
#
# 빌드는 **repo 루트에서** 한다:
#     docker build -f src/agent_a_mlops/Dockerfile -t aitch-a-pred:p1 .
#
# ── 무엇을 굽고 무엇을 밖에 두나 ─────────────────────────────────────────────
#   🔥 이미지에 굽는 것 : 파이썬 · 의존성 · **CPU 전용 torch**
#   📦 볼륨으로 붙이는 것: **소스 전체(리포 루트 `./:/app`)** — 굽지 않는다. 근거는 아래 ①
#   🌐 이미지 밖(env)  : Kafka 주소 · 모델/신호 경로 스위치
#
# ── ⚠️ 세 가지 의도적 결정 (PR 리뷰 포인트) ─────────────────────────────────
#   ① **소스를 굽지 않는다 (b-spc 와 동일 · grouper 와 상이).**
#      grouper 는 디스크에 안 쓰므로 구웠지만, a-pred 는 **다른 프로세스가 원자 교체하는
#      파일을 런타임에 읽는다**:
#        · `pm_log.json` (리포 루트) — B `pm_log_writer` 가 `os.replace` 로 갈아끼우고,
#          A `parse_pm_log` 는 **mtime+size 캐시**라 교체를 감지해 다시 읽는다.
#      소스를 구우면 `/app/pm_log.json` 이 **빌드 시점 스냅샷으로 영원히 얼어붙는다.**
#      에러는 안 난다 — 낡은 PM 이력으로 wafer 테이블을 계속 조립할 뿐이다(조용한 실패).
#      ⚠️ **파일 단위 bind mount 로 때우면 안 된다** — `os.replace` 는 inode 를 바꾸므로
#      파일 마운트는 **낡은 inode 를 계속 가리킨다**. 부모 디렉토리째 붙여야 한다.
#      (b-spc Dockerfile §1-③ 이 같은 제약을 쓰는 쪽에서 서술한다. 여기는 읽는 쪽이다.)
#
#   ② **CMD 는 exec form** (b-spc §1-② 실측 답습): `sh -c "…"` 는 dash 가 fork 해
#      SIGTERM 이 python 에 안 간다. a-pred 는 종료 시 producer flush + wafer 버퍼 정리가
#      있어 SIGKILL 되면 발행 대기분이 사라진다.
#
#   ③ **USER 미지정 = root** (b-spc D5 와 동일 · grouper 와 상이).
#      a-pred 는 `control/ct2` 승격 신호를 처리한 뒤 `processed/`·`failed/` 로 **옮긴다**
#      (consumer.py:658·666 `mkdir`). 호스트 bind mount 에 쓰는 서비스라 비루트로 두면
#      uid 불일치로 신호가 쌓이기만 하고 사라지지 않는다 — 그러면 같은 신호를 반복 처리한다.
#
# ── ⑩ 공유 볼륨 (인프라 설계 v2 §4-⑩ — 이 서비스가 그 항목의 본체다) ───────────
#   쓰는 프로세스와 읽는 프로세스가 다른 파일 채널이 셋이고 **전부 a-pred 가 소비측**이다:
#     · `control/ct2`  ← ct2_orchestrator 가 드롭 · a-pred 가 읽고 processed/ 로 이동 (rw)
#     · `models/`      ← CT²/학습이 적재 · a-pred 가 번들 로드 (ro 로 충분)
#     · `pm_log.json`  ← b-spc 가 os.replace · a-pred 가 mtime 캐시로 재독 (ro 로 충분)
#   리포 루트를 통째로 붙이면 셋이 한 번에 해결된다 — compose 5번 블록 참조.
#
# ── 넣지 않는 것 ─────────────────────────────────────────────────────────────
#   CT² 학습 스택(pyarrow 등). 재학습·배포감시는 `ct2_orchestrator.py`·`ct2_deploy_monitor.py`
#   가 **별도 프로세스**로 돌고, consumer 는 승격 신호를 보고 번들만 갈아끼운다.
#   → 그 둘을 컨테이너화할 때는 **requirements 를 분리**할 것. 한 파일에 합치면 상시 떠 있는
#     추론 컨슈머가 학습 의존을 지고 다닌다.
# =============================================================================

FROM python:3.11-slim
# 3.11 고정 — b-spc·agent-service·grouper 와 동일. 런타임 파이썬을 서비스마다 흔들지 않는다.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# ── 1) CPU 전용 torch 를 **먼저** 설치 (agent_service Dockerfile 52~56행과 같은 수법) ──
# ae_pipeline 이 torch 를 전이 의존으로 끌어오는데 기본 PyPI 는 CUDA 빌드라 2GB+ 가 붙는다.
# 앱 EC2 에 GPU 가 없고 AE 는 추론뿐이므로 CPU 휠로 선점해 뒤 설치가 넘어가게 한다.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

# ── 2) 서비스 의존성 — 코드보다 먼저 복사해 레이어 캐시를 살린다 ──────────────
COPY src/agent_a_mlops/requirements.txt /app/src/agent_a_mlops/requirements.txt
RUN pip install -r /app/src/agent_a_mlops/requirements.txt

# 소스는 COPY 하지 않는다 — 위 ① 참조(리포 루트를 볼륨 마운트).

# exec form (JSON 배열) — sh 를 거치지 않아 SIGTERM 이 python(PID 1)에 직접 전달된다(②).
# ⚠️ Dockerfile 은 인스트럭션 줄에 인라인 주석(#)을 지원하지 않는다 → 이 줄엔 JSON 만 둔다.
CMD ["python", "-m", "src.agent_a_mlops.consumer"]
