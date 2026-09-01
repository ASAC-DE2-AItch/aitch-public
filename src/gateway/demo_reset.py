# -*- coding: utf-8 -*-
"""회차 초기화 — S11 [회차 초기화] 버튼의 백엔드 (2026-08-10).

왜 필요한가: 「리허설 준비에 명령창을 쓰지 않는다」가 원칙인데 A-2(운영 데이터 초기화)만
회차마다 터미널을 요구해 '다음-다음-다음'이 거기서 끊겼다. 2026-08-10 리허설에서 손으로
돈 절차를 **그 순서 그대로** 옮긴다 (`_round1_retry.ps1` R1b 실측 = 약 60초).

⚠️ 순서가 규약이다 — 바꾸면 조용히 실패한다. 각 단계의 존재 이유:
  ① 세계 정지 — producer 가 살아 있으면 초기화 직후 다시 채워진다.
  ② 컨슈머 컨테이너 정지 — Kafka 는 **활성 그룹의 오프셋 리셋을 거부**한다. 이걸 건너뛰면
     ③이 전부 실패하는데 종료코드만 보면 "0건 리셋"으로 조용히 지나간다.
  ③ 오프셋 latest 리셋 — 오프셋은 **브로커에 있어 SQL 초기화로 안 지워진다.** 2026-08-10
     실측 `consumer-group-prediction × fdc.raw` LAG **4,321,562** — 안 지우면 초기화
     직후 `wafer_predictions` 가 되찬다(97행 재출현이 그 증거). `--all-topics` 금지:
     그룹이 구독하지 않는 토픽까지 건드린다 → **그룹 × 토픽 개별 지정.**
  ④ 초기화 SQL — `docker cp` 후 `-f` 로 실행한다. stdin 파이프는 인코딩·개행이 변형돼
     구문 오류를 만들고, `ON_ERROR_STOP=1` 이 없으면 BEGIN 안의 에러가 조용히 **전체
     롤백**으로 끝나 초기화가 안 된 채 "완료"처럼 보인다(reset_demo_state.sql 머리말).
  ⑤ Qual 스냅샷 재시딩 — ④가 `qual_snapshots` 를 비우므로 4챔버 × 28그룹을 다시 심는다.
     빼먹으면 σ-갭이 비교 기준을 잃어 **Qual 판정 자체가 성립하지 않는다.**
  ⑥ 컨슈머 재기동 — `spc-consumer` 는 start 가 아니라 **restart**: 가한계 복구 상태를 새
     DB 기준으로 다시 파생해야 한다(`provisional_seams.derive_restore_state`).

🚫 절대 쓰지 않는 것: `docker compose down -v` · `docker volume prune` · `docker system
   prune --volumes` · `docker container prune` — Qdrant KB 와 `aitch_hf_cache`(6.4GB)가
   날아간다. 이 모듈은 컨테이너를 **stop/start** 만 하고 볼륨·이미지를 건드리지 않는다.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

log = logging.getLogger("demo-reset")

ROOT = Path(__file__).resolve().parents[2]

# 컨슈머 컨테이너 — 정지 순서는 상류→하류 무관(전부 멈춰야 오프셋 리셋이 허용된다).
CONSUMERS = ["fdc-spc-consumer", "fdc-a-pred", "fdc-grouper",
             "fdc-agent-service", "fdc-prediction-sink"]
# 재기동 순서는 의미가 있다 — 싱크가 먼저 떠 있어야 첫 예측을 흘리지 않는다.
RESTART_ORDER = ["fdc-prediction-sink", "fdc-spc-consumer", "fdc-a-pred", "fdc-grouper"]

TOPICS = ["fdc.raw", "fdc.alert", "fdc.prediction", "fdc.actual"]
KAFKA_CONTAINER = "fdc-kafka"
KAFKA_BIN = "/opt/kafka/bin/kafka-consumer-groups.sh"
KAFKA_INTERNAL = "kafka:9093"          # 컨테이너 안에서 보는 리스너 (호스트 9092 아님)
PG_CONTAINER = "fdc-postgres"
RESET_SQL = "scripts/reset_demo_state.sql"

SETTLE_SEC = 20                        # 컨슈머 재기동 후 그룹 조인 대기 (실측 30s → 20s)


def _run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """docker CLI 호출 — 게이트웨이는 호스트 프로세스라 docker 에 닿는다.
    실패해도 예외를 올리지 않는다(호출부가 returncode 로 판단 · 단계별 로그 유지)."""
    return subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _self_mount_and_network() -> tuple[Optional[str], Optional[str]]:
    """게이트웨이 컨테이너 자신을 inspect 해 (repo 바인드의 데몬측 소스, 네트워크명)을 얻는다.

    왜 (2026-08-11 리허설 E2E 실측): 컨테이너 안에서 `docker compose run spc-seed` 를 돌리면
    compose 가 상대 바인드 `.:/app` 을 **자기 cwd(/app — 컨테이너 내부 경로)** 로 풀어 호스트
    데몬에 넘긴다. 데몬 VM 엔 /app 이 없으니 **빈 디렉토리를 만들어 마운트** → 모듈 없음 →
    seed exit 1. "성공한 명령, 빈 결과" 계열 (헌법 7장). 해법 = 자기 /app 마운트의 Source
    (데몬이 이미 아는 경로 문자열)를 그대로 `docker run -v` 에 재사용한다.
    호스트 실행(구 방식)이면 inspect 실패 → (None, None) → compose run 폴백."""
    import os
    import socket
    cid = os.environ.get("HOSTNAME") or socket.gethostname()   # 컨테이너 short ID
    r = _run(["docker", "inspect", "-f",
              '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}',
              cid], timeout=30)
    src = (r.stdout or "").strip()
    if r.returncode != 0 or not src:
        return None, None
    r = _run(["docker", "inspect", "-f",
              "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}", cid], timeout=30)
    net = (r.stdout or "").strip() or None
    return src, net


#: `DATABASE_URL` 의 호스트가 이 목록이면 **로컬 컨테이너**로 본다 (trust 인증 경로).
#  그 밖의 값은 원격(RDS 등)이므로 psql 에 -h/-p 와 PGPASSWORD 를 넘겨야 한다.
_LOCAL_PG_HOSTS = {"", "postgres", "fdc-postgres", "localhost", "127.0.0.1", "db"}


def _pg_target() -> tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """psql 접속 대상 — (user, dbname, host, port, password).

    🔴 **호스트를 봐야 한다** (2026-08-13 AWS 실측). 구 `_pg_identity()` 는 user·db 만
    뽑고 `docker exec fdc-postgres psql -U … -d …` 로 붙었다. RDS 로 옮긴 뒤에도
    **이름이 같은 로컬 컨테이너 DB** 가 그대로 있어서, 초기화가 *빈 로컬 DB 를 성공적으로
    비우고* `exit 0` 을 냈다. 로그는 `(0 rows)` 로 정상처럼 보이는데 **RDS 의 실데이터는
    한 줄도 안 지워졌다** — 실측: 초기화 완료 후에도 incidents 8 · alerts 4,155 ·
    violations 10,100 이 그대로였고, 전부 초기화 시각보다 **앞선** 데이터였다.

    에러가 안 나는 종류라(붙을 DB 가 실제로 있으니 인증도 쿼리도 성공) 화면·로그 어디에도
    표시가 없다. host 를 비교해 원격이면 원격으로 붙는다.
    """
    import os
    dsn = os.environ.get("DATABASE_URL", "")
    try:
        u = urlparse(dsn)
        user = u.username or "fdc_admin"
        db = u.path.lstrip("/") or "fdc_platform"
        host = (u.hostname or "").strip()
        if host.lower() in _LOCAL_PG_HOSTS:
            return user, db, None, None, None            # 로컬 = 구 동작(trust)
        # ⚠️ `urlparse().password` 는 **퍼센트 인코딩된 그대로**다. psycopg·SQLAlchemy 는
        #    내부에서 디코드하므로 앱은 멀쩡히 붙지만, psql 에 그대로 넘기면 인코딩 문자열을
        #    비밀번호로 써서 `password authentication failed` 가 난다 (2026-08-13 실측:
        #    raw 36자 · unquote 후 24자). 앱이 되니까 비밀번호는 맞다고 읽기 쉬운 자리다.
        pw = unquote(u.password) if u.password else None
        return user, db, host, str(u.port or 5432), pw
    except Exception:                                   # noqa: BLE001
        return "fdc_admin", "fdc_platform", None, None, None


class DemoResetRunner:
    """회차 초기화 1회 실행 — 백그라운드 스레드 + 단계 로그.

    HTTP 는 즉시 202 로 돌려주고(60초 이상 걸려 어떤 프론트 타임아웃도 못 버틴다)
    화면은 `/demo/reset/status` 를 폴링해 진행을 본다.
    """

    # ⚠️ 표시 문구에 내부 은어("상시 세계")를 쓰지 않는다 — 화면을 보는 사람은 PM·멘토다.
    #    "세계"는 우리끼리의 말이고, 읽는 사람에겐 무엇이 멈췄는지 안 알려준다 (2026-08-13).
    STEPS = ["시뮬레이터 정지", "컨슈머 정지", "오프셋 리셋", "초기화 SQL",
             "Qual 재시딩", "컨슈머 재기동"]

    def __init__(self, stop_world: Optional[Callable[[], object]] = None):
        self._stop_world = stop_world
        self._lock = threading.Lock()
        self._th: Optional[threading.Thread] = None
        self._state: dict = {"running": False, "step": None, "step_i": 0,
                             "ok": None, "lines": [], "started_at": None,
                             "elapsed_sec": 0, "detail": ""}

    # ---- 상태 ---------------------------------------------------------------
    def status(self) -> dict:
        s = dict(self._state)
        s["steps"] = self.STEPS
        if s["running"] and s.get("_t0"):
            s["elapsed_sec"] = int(time.monotonic() - s["_t0"])
        s.pop("_t0", None)
        return s

    def _log(self, msg: str) -> None:
        self._state["lines"] = (self._state["lines"] + [msg])[-60:]
        log.info("[회차 초기화] %s", msg)

    def _phase(self, i: int) -> None:
        self._state["step_i"] = i
        self._state["step"] = self.STEPS[i]
        self._log(f"── {i + 1}/{len(self.STEPS)} {self.STEPS[i]}")

    # ---- 실행 ---------------------------------------------------------------
    def start(self) -> dict:
        # docker CLI 가용성 게이트 (2026-08-11 리허설 E2E 실측) — 이 절차는 6단계 전부
        # docker subprocess 다. 컨테이너 게이트웨이에서는 이미지에 CLI 를 굽고 호스트
        # 소켓(/var/run/docker.sock)을 마운트해 동작한다(Dockerfile·compose 참조).
        # CLI 가 없으면 2/6 에서 [Errno 2] 로 죽어 "세계 정지 + 컨슈머 일부 정지" 반쪽
        # 상태가 남으므로 **시작 전에 막고** 호스트 경로를 안내한다.
        import shutil
        if shutil.which("docker") is None:
            return {"accepted": False,
                    "detail": "docker CLI 없음 — 게이트웨이 이미지 재빌드 필요 "
                              "(docker compose build gateway) 또는 호스트에서 "
                              "scripts\\demo_reset_host.bat 실행", **self.status()}
        with self._lock:
            if self._state["running"]:
                return {"accepted": False, "detail": "이미 실행 중", **self.status()}
            self._state = {"running": True, "step": self.STEPS[0], "step_i": 0,
                           "ok": None, "lines": [], "_t0": time.monotonic(),
                           "started_at": time.strftime("%H:%M:%S"),
                           "elapsed_sec": 0, "detail": ""}
            self._th = threading.Thread(target=self._work, daemon=True)
            self._th.start()
            return {"accepted": True, "detail": "회차 초기화 시작 — 약 60초", **self.status()}

    def _work(self) -> None:
        t0 = time.monotonic()
        try:
            self._phase(0)
            if self._stop_world is not None:
                try:
                    self._stop_world()
                    self._log("시뮬레이터 종료 요청 완료 — 데이터 생성이 멈춥니다")
                except Exception as e:                  # noqa: BLE001 — 이미 정지 상태 등
                    self._log(f"시뮬레이터 정지 skip: {e}")
            else:
                self._log("시뮬레이터 정지 콜백 없음 — skip")

            self._phase(1)
            for c in CONSUMERS:
                r = _run(["docker", "stop", c], timeout=60)
                self._log(f"stop {c} → {'ok' if r.returncode == 0 else r.stderr.strip()[:80]}")

            self._phase(2)
            r = _run(["docker", "exec", KAFKA_CONTAINER, KAFKA_BIN,
                      "--bootstrap-server", KAFKA_INTERNAL, "--list"], timeout=90)
            groups = [g.strip() for g in (r.stdout or "").splitlines() if g.strip()]
            if not groups:
                raise RuntimeError(f"컨슈머 그룹 목록을 못 읽었다 — {(r.stderr or '')[:160]}")
            n_ok = 0
            for g in groups:
                for t in TOPICS:                        # --all-topics 금지 (모듈 머리말 ③)
                    rr = _run(["docker", "exec", KAFKA_CONTAINER, KAFKA_BIN,
                               "--bootstrap-server", KAFKA_INTERNAL, "--group", g,
                               "--topic", t, "--reset-offsets", "--to-latest",
                               "--execute"], timeout=90)
                    if rr.returncode == 0:
                        n_ok += 1                       # 미구독 토픽은 실패가 정상 — 세지 않는다
            self._log(f"오프셋 latest 리셋: 그룹 {len(groups)}개 · {n_ok} group×topic")
            if n_ok == 0:
                raise RuntimeError("리셋 0건 — 컨슈머가 아직 살아 있을 수 있다 (활성 그룹은 거부됨)")

            self._phase(3)
            r = _run(["docker", "cp", RESET_SQL, f"{PG_CONTAINER}:/tmp/reset.sql"], timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f"docker cp 실패 — {(r.stderr or '')[:160]}")
            user, db, host, port, pw = _pg_target()
            # psql 실행 자체는 로컬 컨테이너를 빌린다(그 안에 psql 바이너리가 있다).
            # 다만 **붙는 대상**은 DATABASE_URL 이 가리키는 곳이다 — 원격이면 -h/-p 를 준다.
            # 비밀번호는 argv 가 아니라 `-e PGPASSWORD` 로 넘긴다 (argv 는 `ps` 에 노출된다).
            exec_argv = ["docker", "exec"]
            if pw:
                exec_argv += ["-e", f"PGPASSWORD={pw}"]
            exec_argv += [PG_CONTAINER, "psql", "-U", user, "-d", db]
            if host:
                exec_argv += ["-h", host, "-p", port or "5432"]
                self._log(f"초기화 SQL 대상: {host}:{port or 5432}/{db} (원격)")
            else:
                self._log(f"초기화 SQL 대상: {PG_CONTAINER}/{db} (로컬)")
            exec_argv += ["-v", "ON_ERROR_STOP=1", "-f", "/tmp/reset.sql"]
            r = _run(exec_argv, timeout=180)
            for ln in (r.stdout or "").splitlines()[-8:]:
                if ln.strip():
                    self._log(f"psql| {ln.strip()[:110]}")
            if r.returncode != 0:                       # ON_ERROR_STOP 이 여기서 걸린다
                raise RuntimeError(f"초기화 SQL 실패 (exit {r.returncode}) — "
                                   f"{(r.stderr or '')[:160]}")
            self._log("초기화 SQL exit 0 — 운영 테이블 비움 · 관리선 initial 원복")

            # 3-B 실행분 (2026-08-12 · PM 지시 — 버튼 통합): SQL 은 chamber_inhibits(DB)만
            #   해제한다. 시뮬레이터가 읽는 **신호 파일**(control/inhibit/*.json)을 같이 지워야
            #   생산이 재개된다 — reset.sql 3-B 주석의 "런북 A-2 에 같은 줄 추가할 것" 실행분.
            #   파일이 남으면 ⓐ DB 는 해제인데 챔버는 계속 멈춰 있고(8/11 실측 — 프리롤 챔버가
            #   죽은 채 시작, 규명 40분) ⓑ 다음 회차 스톰이 '즉발 RTD' 로 오독된다(8/12 실측).
            #   게이트웨이 /app 은 rw 바인드 마운트라 호스트 파일이 그대로 지워진다.
            n_rm = 0
            for f in sorted((ROOT / "control" / "inhibit").glob("*.json")):
                try:
                    f.unlink()
                    n_rm += 1
                except OSError as e:
                    self._log(f"inhibit 신호 삭제 실패 — 수동 확인 필요: {f.name} ({e})")
            self._log(f"inhibit 신호 파일 삭제: {n_rm}건 (RTD 해제 파일 채널)")

            self._phase(4)
            # 컨테이너 게이트웨이: compose run 금지(상대 바인드가 깨진다 — _self_mount_and_network
            # docstring). 자기 마운트 소스를 재사용한 docker run 으로 spc-seed 서비스(compose 7-1,
            # image aitch-spc:b6-2)를 복제한다. 호스트 실행이면 구 방식(compose run) 폴백.
            import os
            src, net = _self_mount_and_network()
            if src:
                dsn = os.environ.get(
                    "DATABASE_URL",
                    "postgresql://fdc_admin:change-me@postgres:5432/fdc_platform")
                argv = ["docker", "run", "--rm", "-v", f"{src}:/app", "-w", "/app",
                        "-e", f"DATABASE_URL={dsn}",
                        "aitch-spc:b6-2",
                        "python", "-m", "src.agent_b_spc.seed_qual_snapshots"]
                if net:
                    argv[3:3] = ["--network", net]
                self._log(f"seed 경로: docker run (net={net or '-'})")
            else:
                argv = ["docker", "compose", "run", "--rm", "spc-seed",
                        "python", "-m", "src.agent_b_spc.seed_qual_snapshots"]
                self._log("seed 경로: compose run (호스트 게이트웨이)")
            r = _run(argv, timeout=240)
            for ln in (r.stdout or "").splitlines()[-3:]:
                if ln.strip():
                    self._log(f"seed| {ln.strip()[:110]}")
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                raise RuntimeError(f"Qual 재시딩 실패 (exit {r.returncode}) — {tail[0][:120]} "
                                   "· σ-갭 기준이 없으면 Qual 판정이 성립하지 않는다")

            self._phase(5)
            for c in RESTART_ORDER:
                _run(["docker", "start", c], timeout=60)
            # spc-consumer 만 restart — 가한계 복구 상태를 새 DB 로 다시 파생해야 한다.
            _run(["docker", "restart", "fdc-spc-consumer"], timeout=90)
            _run(["docker", "start", "fdc-agent-service"], timeout=60)
            self._log(f"컨슈머 재기동 — 그룹 조인 대기 {SETTLE_SEC}초")
            time.sleep(SETTLE_SEC)

            self._state["ok"] = True
            self._state["detail"] = ("초기화 완료 — 왼쪽 메뉴 [Simulator] 에서 시나리오 1 RUN "
                                    "→ 90초 → 시나리오 2 주입 순서로 시작하세요")
            self._log(f"✅ 완료 ({int(time.monotonic() - t0)}초)")
            # 🔴 **로그 줄에도 적는다** (2026-08-13). 1단계에서 시뮬레이터를 멈췄다는 사실이
            #    `detail` 에만 있어서, 초기화 후 화면이 조용한 것을 **고장으로 읽었다** —
            #    실측: RTD 해제는 정상 동작했는데 시뮬이 멈춰 있어 *"CH2 가 안 돌아온다"* 로
            #    보고됐다(원인 규명 40분). 사람 눈이 가는 곳은 진행 로그지 detail 이 아니다.
            # 화면 ID("S11")가 아니라 **메뉴에 실제로 뜨는 이름**으로 적는다 — 읽는 사람이
            # 화면 정의서를 외우고 있지 않다 (2026-08-13, "세계" 은어 제거와 같은 취지).
            self._log("⚠ 시뮬레이터가 정지된 상태입니다 — 왼쪽 메뉴 [Simulator] 에서 RUN 을 "
                      "눌러야 데이터가 다시 흐릅니다 (챔버가 안 돌아오는 것처럼 보이는 원인)")
        except Exception as e:                          # noqa: BLE001 — 실패도 상태로 보고
            self._state["ok"] = False
            self._state["detail"] = str(e)
            self._log(f"❌ 중단: {e}")
        finally:
            self._state["running"] = False
            self._state["elapsed_sec"] = int(time.monotonic() - t0)
            self._state.pop("_t0", None)
