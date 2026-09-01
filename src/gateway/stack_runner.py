# -*- coding: utf-8 -*-
"""P6-4 원클릭 리허설 — 앱 서비스(컨슈머·그루퍼) 수명주기 관리 (SimulatorRunner 패턴 확장).

배경: 리허설이 PowerShell 5창(run_stack.ps1) 수동 기동 + 콘솔 육안 관제라 가시성이
나빴다. 이 러너로 gateway가 서비스들을 자식 프로세스로 관리하고, S11 스택 패널이
상태 LED·마지막 로그를 보여준다 → 운영자는 브라우저의 [서비스 모두 시작] 버튼 하나.

  창 5개(run_stack) → 2개: GATEWAY(uvicorn) + FRONTEND(vite). docker는 별도(compose).

설계 원칙:
  · 서비스 정의는 **params.yaml `stack.services`가 유일 소스** (6-1) — REST는 name만
    받는다. 임의 커맨드 실행 API가 되지 않게 화이트리스트 밖 name은 거부.
  · gateway env를 상속(DATABASE_URL·KAFKA_BOOTSTRAP은 gateway 기동 셸에서 이미 주입)
    + 서비스별 env(params)를 덮어쓴다. PYTHONIOENCODING=utf-8 고정(Windows cp949).
  · stop = terminate → 5s → kill (SimulatorRunner 규약). gateway 종료 시 전체 정리.
  · ⚠️ TSR-0001: torch 서비스(a-pred)가 파이프 자식에서 WDAC에 막히면 params에서
    해당 서비스 `new_console: true` — 별도 콘솔 창을 자동 스폰(로그는 그 창에서).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger("stack-runner")

ROOT = Path(__file__).resolve().parents[2]
LOG_MAX = 400


def _stack_cfg() -> list[dict]:
    """params.yaml `stack.services` — 없으면 빈 목록 (패널이 '미구성' 표시)."""
    try:
        import yaml
        with open(ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            return ((yaml.safe_load(f) or {}).get("stack") or {}).get("services") or []
    except Exception as e:                                   # noqa: BLE001
        log.warning("stack.services 로드 실패 → 빈 목록: %s", e)
        return []


class _Service:
    """서비스 1개 — Popen 수명주기 + 로그 링버퍼 (SimulatorRunner._pump 규약)."""

    def __init__(self, spec: dict):
        self.name: str = spec["name"]
        self.cmd: str = spec["cmd"]
        self.env: dict = dict(spec.get("env") or {})
        self.cwd: Path = ROOT / spec.get("cwd", ".")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.new_console: bool = bool(spec.get("new_console", False))
        self._proc: Optional[subprocess.Popen] = None
        self._logs: deque[str] = deque(maxlen=LOG_MAX)
        self._started_at: Optional[float] = None

    # -- 상태 ---------------------------------------------------------------
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        p = self._proc
        return {
            "name": self.name,
            "enabled": self.enabled,
            "running": self.is_running(),
            "pid": (p.pid if p is not None and self.is_running() else None),
            "returncode": (p.poll() if p is not None else None),
            "uptime_sec": (int(time.monotonic() - self._started_at)
                           if self.is_running() and self._started_at else None),
            "last_line": (self._logs[-1] if self._logs else None),
            "new_console": self.new_console,
        }

    def logs(self, n: int = 120) -> list[str]:
        return list(self._logs)[-max(1, min(n, LOG_MAX)):]

    # -- 수명주기 -------------------------------------------------------------
    def start(self) -> dict:
        if self.is_running():
            return self.status()                             # 멱등 — 이미 가동
        env = {**os.environ, **{k: str(v) for k, v in self.env.items()},
               "PYTHONIOENCODING": "utf-8"}
        argv = shlex.split(self.cmd)
        kwargs: dict = {"cwd": str(self.cwd), "env": env}
        if self.new_console and os.name == "nt":             # TSR-0001 폴백 — 창 자동 스폰
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        else:
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._logs.clear()
        self._proc = subprocess.Popen(argv, **kwargs)
        self._started_at = time.monotonic()
        if not self.new_console:
            threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
        log.info("▶ 서비스 기동: %s (pid=%s)", self.name, self._proc.pid)
        return self.status()

    def _pump(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout or []:
                self._logs.append(line.rstrip("\n"))
        except Exception as e:                               # noqa: BLE001 — 수집만 멈춘다
            self._logs.append(f"[pump 종료: {e}]")

    def stop(self) -> dict:
        if not self.is_running():
            return self.status()
        assert self._proc is not None
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._logs.append("[강제 종료 — terminate 5s 초과]")
        log.info("⏹ 서비스 종료: %s", self.name)
        return self.status()


class StackRunner:
    """params `stack.services` 화이트리스트 기반 서비스 집합 관리."""

    def __init__(self):
        self._svcs: dict[str, _Service] = {
            s["name"]: _Service(s) for s in _stack_cfg() if s.get("name") and s.get("cmd")
        }
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        return list(self._svcs.keys())

    def status(self) -> list[dict]:
        return [s.status() for s in self._svcs.values()]

    def logs(self, name: str, n: int = 120) -> list[str]:
        if name not in self._svcs:
            raise KeyError(name)
        return self._svcs[name].logs(n)

    def start(self, name: Optional[str] = None) -> list[dict]:
        """name 지정 시 1개, 아니면 enabled 전체 (registered 순서대로, 400ms 간격)."""
        with self._lock:
            targets = ([self._svcs[name]] if name else
                       [s for s in self._svcs.values() if s.enabled])
            if name and name not in self._svcs:
                raise KeyError(name)
            out = []
            for s in targets:
                out.append(s.start())
                time.sleep(0.4)                              # run_stack.ps1 과 동일 간격
            return out

    def stop(self, name: Optional[str] = None) -> list[dict]:
        with self._lock:
            if name:
                if name not in self._svcs:
                    raise KeyError(name)
                return [self._svcs[name].stop()]
            return [s.stop() for s in self._svcs.values()]

    def shutdown(self) -> None:
        """gateway 종료 훅 — 자식 프로세스 고아화 방지."""
        for s in self._svcs.values():
            try:
                s.stop()
            except Exception:                                # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# compose 컨테이너 상태 (2026-08-10) — 이 패널의 새 용도.
#   원래 이 러너는 호스트 자식 프로세스를 관리했지만 b-spc(#88)·a-pred·grouper(#130)·
#   c-agent(#142) 가 차례로 compose 로 이관돼 `stack.services` 가 비었다. 그래서 패널은
#   0줄이 됐는데 `[].every()` 가 true 라 "전체 가동 중" 초록불이 켜지는 허위 신호였다.
#   지우는 대신 **볼 대상을 바꾼다** — 이제 확인이 필요한 것은 compose 컨테이너다.
#   (agent-service 가 죽어 있으면 Brief 가 0건, spc-consumer 가 Restarting 이면 알람이
#    0건인데 화면만 보면 "조용한 정상"과 구분이 안 된다 — 8/10 새벽에 실제로 겪었다.)
#   읽기 전용이다: 여기서 컨테이너를 죽이거나 살리지 않는다.
# ---------------------------------------------------------------------------

# 시연 판정에 직접 걸리는 것만 — kafka-ui 등 곁가지는 뺀다(패널이 길어지면 안 읽힌다).
COMPOSE_WATCH = ["postgres", "kafka", "qdrant", "prediction-sink",
                 "spc-consumer", "a-pred", "grouper", "agent-service"]


def _compose_project(timeout: int = 5) -> Optional[str]:
    """이 게이트웨이가 **실제로 속한** compose 프로젝트명. 못 알아내면 None.

    🔴 왜 env 를 안 믿나 (2026-08-12 AWS 실측).
    `docker-compose.yml` 이 gateway 에 `COMPOSE_PROJECT_NAME: sk` 를 **하드코딩**한다
    (2026-08-11 회차 초기화 버튼용 — 로컬 폴더명에서 온 값). 그런데 프로젝트명은
    **compose 를 띄운 디렉토리**에서 정해지므로 배포지가 다르면 어긋난다:

        AWS 노드  /opt/aitch  →  실제 프로젝트 = "aitch"
        주입된 env               COMPOSE_PROJECT_NAME = "sk"

    그러면 `docker compose ps` 가 **존재하지 않는 프로젝트 "sk"** 를 조회해 **빈 목록**을
    돌려주고, S11 인프라 패널이 컨테이너 8종을 전부 `absent`(빨강)로 그린다 —
    **실제로는 전부 Up 인데도.** 에러가 아니라 빈 결과라 조용하고, 하필
    *"인프라가 죽었다"* 로 읽힌다(이 패널이 막으려던 바로 그 오해의 반대 방향).

    그래서 env 대신 **자기 자신에게 물어본다.** compose 가 붙여 준 라벨
    `com.docker.compose.project` 는 정의상 실제 값이라 어긋날 수 없다.

    순서: ① 자기 컨테이너 라벨 → ② `COMPOSE_PROJECT_NAME` → ③ None(compose 가 cwd 로 추론)
    """
    # ① 컨테이너 안이면 hostname 이 곧 자기 컨테이너 ID(docker 기본).
    hostname = (os.environ.get("HOSTNAME") or "").strip()
    if hostname:
        try:
            r = subprocess.run(
                ["docker", "inspect", hostname,
                 "--format", '{{index .Config.Labels "com.docker.compose.project"}}'],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout)
            name = (r.stdout or "").strip()
            if r.returncode == 0 and name and name != "<no value>":
                return name
        except Exception as e:                           # noqa: BLE001 — 게이트웨이 생존
            log.debug("compose 프로젝트 라벨 조회 실패(무시): %s", e)

    # ② 호스트 실행(개발 PC)이면 라벨이 없다 — env 를 쓴다.
    return (os.environ.get("COMPOSE_PROJECT_NAME") or "").strip() or None


def compose_status(timeout: int = 8) -> list[dict]:
    """`docker compose ps` → [{service, name, state, health, ok}] (COMPOSE_WATCH 순서).

    compose 버전에 따라 `--format json` 이 **JSON 배열**이거나 **줄당 1객체(JSONL)** 다 —
    둘 다 받는다. docker 미설치·미기동이면 빈 목록(패널이 '확인 불가'를 표시).

    프로젝트는 `_compose_project()` 가 정한다 — env 가 실제와 어긋나도 안 깨지게
    (2026-08-12 AWS 실측: env `sk` vs 실제 `aitch` → 전 컨테이너 `absent` 오표시).
    """
    project = _compose_project()
    argv = ["docker", "compose"]
    if project:
        argv += ["-p", project]
    argv += ["ps", "-a", "--format", "json"]
    try:
        r = subprocess.run(argv,
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if r.returncode != 0:
            log.warning("docker compose ps 실패: %s", (r.stderr or "")[:160])
            return []
        import json
        raw = (r.stdout or "").strip()
        items: list[dict] = []
        if raw.startswith("["):
            items = json.loads(raw)
        else:
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except Exception:                    # noqa: BLE001 — 한 줄 손상은 건너뛴다
                        pass
    except Exception as e:                               # noqa: BLE001 — 게이트웨이 생존
        log.warning("compose_status 예외: %s", e)
        return []

    by_svc = {str(it.get("Service") or ""): it for it in items}
    out: list[dict] = []
    for svc in COMPOSE_WATCH:
        it = by_svc.get(svc)
        state = str((it or {}).get("State") or "absent")
        health = str((it or {}).get("Health") or "")
        # running + (health 없음 또는 healthy) 만 초록. restarting·exited 는 빨강.
        ok = state == "running" and health in ("", "healthy")
        out.append({"service": svc, "name": str((it or {}).get("Name") or "—"),
                    "state": state, "health": health, "ok": ok,
                    "status": str((it or {}).get("Status") or "")})
    return out


# ---------------------------------------------------------------------------
# LLM(vLLM) 상태 — 이 패널의 마지막 사각지대 (2026-08-12 신설)
#
# 🔴 왜 필요한가. 이 패널이 만들어진 이유는 *"agent-service 가 죽으면 Brief 0건인데
#   화면만 보면 조용한 정상과 구분이 안 된다"* 였다. **LLM 이 정확히 그 케이스인데
#   빠져 있었다** — GPU 는 별도 EC2(또는 원격 호스트)라 compose 컨테이너가 아니어서
#   `docker compose ps` 에 안 잡힌다. 그래서:
#
#       GPU 안 닿음 → agent-service 는 살아 있음(초록) → Brief 는 나온다
#                   → 다만 "LLM 분석 실패" 폴백(헌법 6-3 무중단)
#                   → 패널은 여전히 전부 초록
#
#   실제로 이 형태를 여러 번 겪었다: 방화벽 IP 명단에서 빠지면(장소 이동·와이파이 변경)
#   호출이 **거부가 아니라 무응답**으로 버려지고, 폴백이 받아 정상처럼 보인다.
#   `.env` 주소가 낡았을 때(재생성으로 IP 변경)도 같은 모양이다.
#
# 읽기 전용이다 — 켜거나 끄지 않는다(패널 원칙). 컨테이너가 아니지만 **판정에 직접
# 걸리는 의존성**이라 같은 목록에 같은 형식으로 싣는다(프론트 수정 불요).
# ---------------------------------------------------------------------------

LLM_PROBE_TTL_SEC = 15.0          # 패널은 3초마다 폴링한다 — 매번 원격을 치면 안 된다
LLM_PROBE_TIMEOUT_SEC = 3.0       # 죽어 있을 때 패널 전체가 이 시간만큼 늦어지므로 짧게
_llm_probe_cache: dict = {"at": 0.0, "row": None}


def llm_status(ttl: float = LLM_PROBE_TTL_SEC) -> Optional[dict]:
    """`AGENT_LLM_BASE_URL` 로 `/v1/models` 를 쳐서 상태 1행. 미설정이면 None.

    반환 형식은 `compose_status()` 행과 같다 — 프론트가 그대로 그린다.
    TTL 캐시를 둔다: 3초 폴링 × 3초 타임아웃이면 죽어 있을 때 패널이 계속 멈춘다.
    """
    base = (os.environ.get("AGENT_LLM_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return None                                   # LLM 미설정 배포 — 항목 자체를 숨긴다

    now = time.monotonic()
    cached = _llm_probe_cache.get("row")
    if cached is not None and (now - float(_llm_probe_cache.get("at") or 0.0)) < ttl:
        return cached

    # 주소는 호스트까지만 온다(`/v1` 없음 — vllm.py 규약). 경로는 여기서 붙인다.
    host = base.split("//", 1)[-1]
    row = {"service": "llm (vLLM)", "name": host, "state": "absent",
           "health": "", "ok": False, "status": "응답 없음"}

    # 🔴 주소가 로컬이면 **닿더라도** 사고다 (2026-08-13). AWS 배포에서 `localhost:11434`
    #    는 컨테이너 자기 자신이고, 설령 뭔가 응답해도 그건 우리 GPU 가 아니다.
    #    `target_status()` 의 DB 판정과 같은 규칙 — 다만 LLM 은 이 함수가 이미 실제로
    #    찔러보므로 행을 따로 만들지 않고 여기에 얹는다.
    if _host_of(base).lower() in _LOCAL_HOSTS and remote_only():
        row["status"] = f"⚠ 로컬 주소 ({host}) — 원격 GPU 여야 한다"
        log.error("LLM 주소가 로컬입니다 — %s (%s=1)", host, _REMOTE_ONLY_ENV)
        _llm_probe_cache.update(at=now, row=row)
        return row
    try:
        with urllib.request.urlopen(base + "/v1/models",
                                    timeout=LLM_PROBE_TIMEOUT_SEC) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        data = (body.get("data") or [{}])[0]
        model = str(data.get("id") or "?")
        mlen = data.get("max_model_len")
        row.update(state="running", ok=True,
                   status=f"READY · {model}" + (f" · len={mlen}" if mlen else ""))
    except urllib.error.HTTPError as e:               # 떴는데 응답이 이상함 — 구분해서 보여준다
        row["status"] = f"HTTP {e.code}"
    except Exception as e:                            # noqa: BLE001 — 게이트웨이 생존
        # 타임아웃이 대부분이다. 그 자체가 신호다(방화벽 차단·주소 낡음·GPU 정지).
        row["status"] = f"{type(e).__name__}"
    _llm_probe_cache.update(at=now, row=row)
    return row


# =============================================================================
# 연결 대상 감시 — "로컬은 없는 셈 친다"
# =============================================================================
#: 이 호스트로 해석되면 **로컬**이다. 컨테이너 안에서 `localhost` 는 자기 자신이라
#  바깥 어디에도 못 닿고, `postgres`/`fdc-postgres` 는 스택 안의 빈 껍데기 DB 다.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1",
                "postgres", "fdc-postgres", "db"}

#: 이 값이 참이면 대상이 로컬로 해석될 때 **경고**한다. AWS 오버레이
#  (`compose.prod.yml`)가 켜준다 — 로컬 개발자 화면에는 경고가 안 뜬다.
_REMOTE_ONLY_ENV = "AITCH_REMOTE_ONLY"

DB_PROBE_TTL_SEC = 15.0
DB_PROBE_TIMEOUT_SEC = 3.0
_db_probe_cache: dict = {}


def remote_only() -> bool:
    """*"로컬은 없는 셈 친다"* 스위치 (AWS 오버레이에서만 켠다)."""
    return str(os.environ.get(_REMOTE_ONLY_ENV, "")).strip().lower() in ("1", "true", "yes")


def _host_of(value: str) -> str:
    """DSN·URL·`host:port` 어느 형태든 호스트만 뽑는다 (실패하면 빈 문자열)."""
    v = (value or "").strip()
    if not v:
        return ""
    try:
        if "://" in v:
            from urllib.parse import urlparse
            return (urlparse(v).hostname or "").strip()
        return v.split(",")[0].rsplit("@", 1)[-1].split(":")[0].strip()
    except Exception:                                   # noqa: BLE001
        return ""


def target_status(ttl: float = DB_PROBE_TTL_SEC) -> list[dict]:
    """DB 연결 대상 1행 — **주소가 맞나 + 실제로 닿나** 를 함께 본다.

    🔴 왜 만들었나 (2026-08-13 실측). 회차 초기화가 RDS 가 아니라 **이름이 같은 빈 로컬
    DB** 에 붙어, 빈 테이블을 성공적으로 비우고 `exit 0` 을 냈다. 붙는 데 진짜로 성공하니
    예외도 에러 로그도 없다 — 컨테이너는 전부 초록이었고, 지워지지 않은 데이터를 사람이
    눈으로 발견할 때까지 아무 일도 일어나지 않았다. 패널에 없던 것은 "무엇이 떠 있나"가
    아니라 **"그것들이 어디에 붙어 있나"** 였다.

    ⚠️ **주소 검사만으로는 부족하다** (초판의 한계 — 같은 날 보강). 문자열만 보면 RDS 가
    죽거나 보안그룹이 막혀도 초록으로 남는다. **틀린 초록불은 불이 없는 것보다 나쁘다** —
    그래서 `SELECT 1` 을 실제로 태운다. 게이트웨이 풀은 빌리지 않는다(psycopg2 커넥션은
    스레드 안전하지 않고, 여기서 보고 싶은 것은 *지금 새로 붙을 수 있는가* 다).

    ⚠️ Kafka·Qdrant 는 대상에 넣지 않는다 — `kafka:9093`·`qdrant:6333` 은 컨테이너
    이름이지 로컬 노트북이 아니고 스택 안에서 도는 것이 정상이다. 넣으면 매번 경고가 뜨고
    그러면 진짜 경고까지 함께 무시된다.
    LLM 은 `llm_status()` 가 이미 실제로 찔러보므로 여기서 중복하지 않는다.

    반환 형식은 `compose_status()` 행과 같아 프론트가 그대로 그린다 (프론트 무수정).
    """
    raw = (os.environ.get("DATABASE_URL") or os.environ.get("AGENT_PG_DSN") or "").strip()
    if not raw:
        return []                                       # DB 미설정 배포 — 항목 자체를 숨긴다

    now = time.monotonic()
    cached = _db_probe_cache.get("row")
    if cached is not None and (now - float(_db_probe_cache.get("at") or 0.0)) < ttl:
        return [cached]

    host = _host_of(raw)
    is_local = host.lower() in _LOCAL_HOSTS
    row = {"service": "db", "name": host or "?", "state": "running",
           "health": "", "ok": True, "status": ""}

    # ① 주소 — 로컬이면 그 자체가 사고다 (닿든 안 닿든).
    if is_local and remote_only():
        row.update(state="exited", ok=False,
                   status=f"⚠ 로컬로 붙어 있음 ({host}) — 원격이어야 한다")
        log.error("연결 대상이 로컬입니다 — db → %s (%s=1). AWS 배포에서 이 값은 원격이어야 "
                  "하며, 로컬에 이름이 같은 DB 가 있으면 '성공적으로 빈 테이블을 지우는' "
                  "사고가 난다.", host, _REMOTE_ONLY_ENV)
        _db_probe_cache.update(at=now, row=row)
        return [row]

    # ② 실제 접속 — 여기까지 와야 초록불이 "닿는다"는 뜻이 된다.
    try:
        import psycopg2
        conn = psycopg2.connect(raw, connect_timeout=int(DB_PROBE_TIMEOUT_SEC))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        row["status"] = ("로컬" if is_local else "원격") + f" · {host} · 응답 OK"
    except Exception as e:                              # noqa: BLE001 — 게이트웨이 생존
        # 타임아웃·인증 실패·보안그룹 차단이 여기로 온다. 그 자체가 신호다.
        row.update(state="exited", ok=False,
                   status=f"⚠ 접속 실패 · {host} · {type(e).__name__}")
        log.error("DB 접속 실패 — %s (%s)", host, type(e).__name__)
    _db_probe_cache.update(at=now, row=row)
    return [row]


if __name__ == "__main__":                                   # 수동 점검용
    logging.basicConfig(level=logging.INFO)
    r = StackRunner()
    print("services:", r.names())
    print(r.status())
    for c in compose_status():
        print(c)
    sys.exit(0)
