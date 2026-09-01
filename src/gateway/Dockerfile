# =============================================================================
# gateway — REST/SSE + 승인 게이트 + 시뮬 러너 컨테이너 이미지 (인프라 스텝 1 마지막)
#
# ⚠ `src/gateway/` 는 PM/PO 소유(헌법 3-1). PR #121 위임 범위에서 작성했다. **리뷰는 PM.**
#
# 빌드는 **repo 루트에서**:
#     docker build -f src/gateway/Dockerfile -t aitch-gateway:p1 .
#
# ── 무엇을 굽고 무엇을 밖에 두나 ─────────────────────────────────────────────
#   🔥 이미지에 굽는 것 : 파이썬 · 의존성(gateway + **시뮬**)
#   📦 볼륨으로 붙이는 것: **소스 전체(리포 루트 `./:/app`)** — 굽지 않는다. 근거는 아래 ①
#   🌐 이미지 밖(env)  : DATABASE_URL · KAFKA_BOOTSTRAP (⚠️ a-pred 는 …_SERVERS 로 다르다)
#
# ── ⚠️ 네 가지 의도적 결정 (PR 리뷰 포인트) ─────────────────────────────────
#   ① **소스를 굽지 않는다 (a-pred·b-spc 와 동일 · grouper 와 상이).**
#      gateway 는 자식 프로세스를 띄우고 그 자식들이 **런타임에 리포를 읽는다**:
#        · `src/simulator/scenarios/*.yaml` — 시나리오 목록(파일명 stem 이 곧 id)
#        · 리플레이 CSV — `message_builder.DEFAULT_CSV_PATH`
#        · `config/params.yaml` — 스택·시뮬 설정
#      또 `control/inject` 에 **파일을 드롭**해 실행 중 세계에 주입한다(상시 세계 1개 원칙).
#      구우면 이미지 안의 사본에 드롭하게 되어 **다른 컨테이너가 그 신호를 못 본다** —
#      에러 없이 "주입했는데 아무 일도 안 일어남" 이 된다.
#
#   ② **B3 — 시뮬 의존성을 함께 굽는다** (인프라 설계 v2 §7 B3).
#      S11 RUN 은 `kafka_producer` 를 **자식 프로세스**로 띄운다(simulator_runner.py).
#      자식은 부모 이미지 안에서 돌므로 `pandas`·`confluent-kafka` 가 gateway 이미지 요구다.
#      리소스 리밋도 시뮬 가동분을 포함해 산정해야 한다(스텝 5).
#
#   ③ **CMD 는 exec form** — uvicorn 이 PID 1 로 SIGTERM 을 직접 받아야 자식(시뮬·스택)
#      정리가 돈다. shell form 이면 dash 가 fork 해 시그널이 안 가고, 그러면 시뮬 프로세스가
#      **컨테이너보다 오래 살아남는다**(고아 producer → 이중 발행).
#
#   ④ **USER 미지정 = root** (a-pred·b-spc 와 동일 · grouper 와 상이).
#      `control/inject` 에 쓰는 서비스라 호스트 bind mount 쓰기 권한이 필요하다.
#
# ── 🔴 남은 경계 하나 — `stack_runner` (PM 판단 필요) ────────────────────────
#   `stack_runner.py` 는 `params.yaml stack.services`(a-pred·grouper·c-agent)를 **자식
#   프로세스로** 띄운다. 그 셋은 이제 **각자 컨테이너**이므로, 컨테이너 안에서 S11 의
#   [서비스 모두 시작] 을 누르면 같은 서비스가 두 번 뜬다.
#     · a-pred  → 이 이미지에 torch 가 없어 **즉시 실패**(안전)
#     · c-agent → FlagEmbedding 없어 **즉시 실패**(안전)
#     · grouper → psycopg2·confluent 가 여기 있어 **뜬다** 🔴 같은 consumer group 에
#                 컨슈머 2개 = 파티션 갈림 (설계 v2 §4-④ 이중 소비의 재발)
#   이 Dockerfile 로는 막을 수 없다(코드·params 는 PM 소유). **컨테이너 배포에서는 스택
#   패널을 기동 수단이 아니라 상태 표시로 돌리거나, `stack.services` 를 비우는 게이트가
#   필요합니다** — 방식만 정해주시면 맞추겠습니다. 그때까지 compose 블록에 경고를 달아둔다.
# =============================================================================

FROM python:3.11-slim
# 3.11 고정 — 다른 서비스와 동일. 런타임 파이썬을 서비스마다 흔들지 않는다.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    # 시뮬 자식 프로세스가 Windows 호스트 로그를 그대로 물려받지 않게 고정
    # (stack_runner 가 같은 값을 주입한다 — 컨테이너에선 무해하나 명시로 남긴다)
    PYTHONIOENCODING=utf-8

WORKDIR /app

# ── 의존성 — 코드보다 먼저 복사해 레이어 캐시를 살린다 ────────────────────────
# psycopg2-binary·psycopg[binary]·confluent-kafka 모두 휠로 오므로 빌드툴이 필요 없다(apt 0).
COPY src/gateway/requirements.txt /app/src/gateway/requirements.txt
RUN pip install -r /app/src/gateway/requirements.txt

# ── docker CLI + compose 플러그인 (2026-08-11 · 회차 초기화 버튼 컨테이너 대응) ──────
#   demo_reset.py 의 6단계가 전부 `docker …` subprocess 다(호스트 게이트웨이 시절 설계).
#   컨테이너 이관 후 [Errno 2] 'docker' 로 죽던 것(리허설 E2E 실측)을, CLI 정적 바이너리
#   + compose 플러그인을 넣고 compose 가 **호스트 소켓**(/var/run/docker.sock 마운트)으로
#   호스트 데몬에 말하게 해 해결한다. 데몬은 안 굽는다 — CLI 만 (~70MB).
#   ⚠️ 소켓 마운트는 compose gateway 블록에 있다. 로컬 데모 한정 — AWS 전개 시 재검토.
#   slim 엔 curl 이 없어 ADD(URL) 로 받는다.
ADD https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz /tmp/docker.tgz
ADD https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64 \
    /usr/local/lib/docker/cli-plugins/docker-compose
RUN set -eux; \
    tar xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker; \
    rm /tmp/docker.tgz; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker --version && docker compose version

# 소스는 COPY 하지 않는다 — 위 ① 참조(리포 루트를 볼륨 마운트).

EXPOSE 8000

# exec form (JSON 배열) — uvicorn 이 PID 1 로 SIGTERM 을 직접 받는다(③).
#
# ⚠️ `--timeout-graceful-shutdown` 은 **실측으로 붙였다.** 없이 돌리면 SIGTERM 후
#    `Waiting for connections to close.` 에서 끝나지 않아 **30초 grace 를 다 쓰고 SIGKILL(137)**
#    로 죽는다(2026-08-07 실측). SSE(`/events`)와 Kafka 피드 스레드가 연결을 물고 있어서다.
#    10초면 시뮬 자식 정리(terminate→5s→kill 규약)를 덮고도 남는다. compose 의
#    `stop_grace_period: 30s` 는 그 위 여유로 남겨 둔다.
#    ※ 컨테이너가 SIGKILL 로 죽어도 **고아 프로세스가 밖으로 새지는 않는다** — PID 네임스페이스가
#      컨테이너와 함께 사라지기 때문. 호스트 실행 때와 달라진 점이고, 컨테이너화의 이득이다.
#
# ⚠️ Dockerfile 은 인스트럭션 줄에 인라인 주석(#)을 지원하지 않는다 → 이 줄엔 JSON 만 둔다.
CMD ["uvicorn", "src.gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
