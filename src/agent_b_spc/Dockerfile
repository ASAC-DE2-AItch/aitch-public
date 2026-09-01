# =============================================================================
# agent_b_spc (팀원 B — Smart SPC) 컨테이너 이미지 · B6-2
#
# 빌드는 **repo 루트에서** 한다 (코드가 `python -m src.agent_b_spc.spc_consumer` 로 돌고
# config/params.yaml 을 repo 루트 기준으로 찾기 때문 — C6-2 Dockerfile 과 동일 규약):
#
#     docker build -f src/agent_b_spc/Dockerfile -t aitch-spc:b6-2 .
#
# ── 무엇을 굽고 무엇을 밖에 두나 ─────────────────────────────────────────────
#   🔥 이미지에 굽는 것 : 파이썬 · 의존성 · entrypoint (환경 고정 역할만)
#   📦 볼륨으로 붙이는 것: **소스 전체(리포 루트 `./:/app`)** — 굽지 않는다.
#      근거는 편의가 아니라 §1-③ `pm_log` 의 `os.replace` 제약이다: tmp 파일을 같은
#      디렉토리에 쓰고 원자 교체하므로, 파일 단위 bind mount 는 EXDEV(다른 FS)로 실패한다.
#      → pm_log 정본(리포 루트)을 부모 디렉토리째 마운트해야 하고, 그러면 마운트가 이미지
#      레이어를 덮으므로 소스를 구울 이유가 없다.
#
# ── ⚠️ 두 가지 의도적 결정 (PR 리뷰 포인트) ─────────────────────────────────
#   ① CMD 는 반드시 **exec form** (§1-②): `sh -c "… && python …"` 은 dash 가 마지막
#      명령을 fork 해 SIGTERM 이 python 에 전달되지 않는다(실측). 그러면 graceful
#      shutdown(_flush_all·consumer.close·producer.flush)이 전부 무실행 → SIGKILL.
#      exec form 이면 python 이 PID 1 을 차지해 시그널을 직접 받는다.
#   ② **USER 미지정 = root** (D5): C 의 `USER agent`(uid 10001)를 그대로 쓰면 호스트
#      bind mount(리포 루트)에 쓰기 권한이 없을 수 있다. B 는 리포 루트에 쓰는 유일한
#      서비스(pm_log·contribution_log)라 root 를 유지한다 — C 와 의도적 상이.
# =============================================================================

FROM python:3.11-slim
# 3.11 고정 — C6-2·개발/검증 환경과 동일. pandas 버전 사고를 피하려면 런타임 파이썬을
# 흔들지 않는 편이 안전하다.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# 의존성만 먼저 복사 — 레이어 캐시가 살아 코드만 고칠 때 재설치를 건너뛴다.
# psycopg2-binary 는 휠로 오므로 libpq-dev 빌드툴이 필요 없다(apt 설치 0).
COPY src/agent_b_spc/requirements.txt /app/src/agent_b_spc/requirements.txt
RUN pip install -r /app/src/agent_b_spc/requirements.txt

# 소스는 COPY 하지 않는다 — 위 §1-③ 참조(리포 루트를 볼륨 마운트).

# exec form (JSON 배열) — sh 를 거치지 않아 SIGTERM 이 python(PID 1)에 직접 전달된다(§1-②).
# ⚠️ Dockerfile 은 인스트럭션 줄에 인라인 주석(#)을 지원하지 않는다 → 이 줄엔 JSON 만 둔다.
#    (주석을 붙이면 JSON 파싱 실패 → shell form 폴백 → `/bin/sh: [python,: not found`)
CMD ["python", "-m", "src.agent_b_spc.spc_consumer"]
