# =============================================================================
# orchestrator — Incident 그루퍼 컨테이너 이미지
#
# ⚠ `src/orchestrator/` 는 PM/PO 소유(헌법 3-1). 이 파일은 PM 이 PR #121 본문에서
#   "PM 소유 디렉토리 Dockerfile — 승인. 컨테이너화 전체를 은비 주도로(작성 은비, 리뷰 PM)"
#   라고 위임한 범위에서 작성했다. **리뷰는 PM.**
#
# 빌드는 **repo 루트에서** 한다 (코드가 `python -m src.orchestrator.incident_grouper` 로
# 돌고 config/params.yaml 을 루트 기준으로 찾기 때문 — B6-2·C6-2 Dockerfile 과 동일 규약):
#
#     docker build -f src/orchestrator/Dockerfile -t aitch-orchestrator:p4-3 .
#
# ── 무엇을 굽고 무엇을 밖에 두나 ─────────────────────────────────────────────
#   🔥 이미지에 굽는 것 : 파이썬 · 의존성 · **소스 · config**
#   📦 볼륨            : **`control/`** — RTD inhibit 신호 (아래 ①-b)
#   🌐 이미지 밖(env)  : DATABASE_URL · KAFKA_BOOTSTRAP
#
# ── ⚠️ 세 가지 의도적 결정 (PR 리뷰 포인트) ─────────────────────────────────
#   ① **소스는 굽고, `control/` 만 볼륨으로 붙인다** (B6-2 와 부분 상이 · C6-2 와 동일).
#
#      ①-a 소스를 굽는 이유: b-spc 가 리포 루트를 `./:/app` 로 붙이는 건 편의가 아니라
#      `pm_log` 의 `os.replace` 제약이다(같은 디렉토리 tmp → 원자 교체라 부모째 마운트 필요).
#      그루퍼에는 그 제약이 없으므로 굽는 쪽이 맞다 — 실행 평면이 이미지 하나로 닫혀
#      "코드가 밖에 있다"가 사라진다(인프라 설계 v2 §4-②).
#
#      ①-b ⚠️ **`control/` 은 볼륨이어야 한다 (2026-08-07 정정 — #130 리뷰 차단).**
#      최초 작성 시 *"그루퍼는 디스크에 아무것도 쓰지 않는다"* 를 근거로 볼륨을 두지 않았다.
#      확인 시점에는 사실이었으나 **#118 이 쓰기 경로를 추가**했다:
#          incident_grouper → chamber_inhibit.drop_signal_after_commit
#          → _drop_signal → INHIBIT_DIR.mkdir + tmp.write_text   (control/inhibit/<chamber>.json)
#      **읽는 쪽은 시뮬레이터**다(`src/simulator/kafka_producer.py:148` —
#      `(inhibit_dir / f"{chamber_id}.json").exists()`). 즉 쓰는 프로세스와 읽는 프로세스가
#      다른 **파일 신호 채널**이라 ⑩ 대상이 됐다.
#
#      볼륨이 없으면 **쓰기가 실패하는 게 아니라 컨테이너 안에서 성공한다** — 예외도 로그도
#      없고 DB 에는 정지 기록이 남아 화면은 "정지됨" 인데 **신호를 아무도 못 봐 라인은 계속 돈다.**
#      `rtd.auto_inhibit` 이 꺼져 있어 스텝 4~5 에서야 드러날 자리였다.
#
#   ② **CMD 는 반드시 exec form** (B6-2 §1-② 실측 답습): `sh -c "…"` 는 dash 가 마지막
#      명령을 fork 해 SIGTERM 이 python 에 전달되지 않는다. 그루퍼는 `enable.auto.commit=False`
#      로 **DB 적재 성공 후 수동 커밋**하므로(헌법 7장 "영속화 consumer 자동 커밋 금지"),
#      시그널을 못 받고 SIGKILL 되면 처리 중이던 구간이 커밋도 롤백도 아닌 채로 끊긴다.
#      exec form 이면 python 이 PID 1 을 차지해 시그널을 직접 받는다(_handle_signal → 루프 탈출).
#
#   ③ **USER 미지정 = root** (B6-2·a-pred·gateway 와 동일 — 2026-08-07 정정).
#      최초에는 `USER agent`(uid 10001)로 두었다. 볼륨도 쓰기도 없으니 권한을 줄 이유가
#      없다는 판단이었고 당시엔 맞았다. ①-b 로 `control/` 을 호스트 bind mount 로 붙이면서
#      **비루트로는 EC2(Linux)에서 호스트 디렉토리 소유자와 uid 가 안 맞아 같은 자리에서 다시
#      막힌다.** 호스트 디렉토리에 쓰는 서비스는 넷 다 root 로 통일한다(b-spc D5 와 같은 사유).
#      ⚠️ 로컬 Docker Desktop 은 권한이 관대해 비루트로도 통과한다 — **로컬에서 되는 것이
#      근거가 되지 않는 자리**다.
#
# ── 넣지 않는 것 ─────────────────────────────────────────────────────────────
#   langgraph · psycopg(3) — `approval_graph.py`·`ct2_deploy_approval.py` 쪽 의존이고
#   그루퍼 진입점은 그 모듈을 import 하지 않는다. 같은 판단의 선례가 C6-2 Dockerfile
#   66~72행이다("우리 경로에 안 걸린다"고 확인한 의존은 이미지에서 뺀다).
#   ⚠️ 나중에 approval 계열을 같은 이미지로 돌리려면 requirements 를 **분리**할 것 —
#      한 파일에 합치면 그루퍼가 langgraph 를 지고 다니게 된다.
# =============================================================================

FROM python:3.11-slim
# 3.11 고정 — B6-2·C6-2·개발/검증 환경과 동일. 런타임 파이썬을 서비스마다 흔들지 않는다
# (pandas DLL 사고 계열의 재발 방지 — 같은 이유로 b-spc 도 3.11 을 못박았다).

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# ── 1) 의존성 — 코드보다 먼저 복사해 레이어 캐시를 살린다 ────────────────────
# psycopg2-binary·confluent-kafka 모두 휠로 오므로 libpq-dev·librdkafka 빌드툴이 필요 없다(apt 0).
COPY src/orchestrator/requirements.txt /app/src/orchestrator/requirements.txt
RUN pip install -r /app/src/orchestrator/requirements.txt

# ── 2) 코드·설정 ──────────────────────────────────────────────────────────────
# params.yaml 은 repo 루트 기준으로 해석된다 (incident_grouper.py:37 ROOT = parents[2]).
COPY config/ /app/config/
COPY src/__init__.py /app/src/__init__.py
COPY src/orchestrator/ /app/src/orchestrator/

# USER 는 지정하지 않는다 = root (위 ③). `control/` 호스트 bind mount 에 쓰기 때문이다.
# 볼륨 마운트 지점 — compose 가 `./control` 을 여기 붙인다(①-b). 비어 있어도 기동은 된다.
VOLUME ["/app/control"]

# exec form (JSON 배열) — sh 를 거치지 않아 SIGTERM 이 python(PID 1)에 직접 전달된다(②).
# ⚠️ Dockerfile 은 인스트럭션 줄에 인라인 주석(#)을 지원하지 않는다 → 이 줄엔 JSON 만 둔다.
#    (주석을 붙이면 JSON 파싱 실패 → shell form 폴백 → `/bin/sh: [python,: not found`)
CMD ["python", "-m", "src.orchestrator.incident_grouper"]
