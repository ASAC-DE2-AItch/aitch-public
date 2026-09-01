# -*- coding: utf-8 -*-
"""P6-3 — S11 RUN 실배선 코어 (fastapi 무의존, 단위 테스트 대상).

역할: 시나리오 YAML 목록 제공 + kafka_producer를 시나리오 장전 서브프로세스로 기동/중지,
stdout을 링버퍼로 수집해 S11 주입 로그 패널에 공급한다.

원칙:
  · 상시 세계 1개 (M3 ② — 2026-07-28 상주화): 최초 RUN이 baseline 프로세스를 기동하고,
    이후 RUN은 프로세스를 새로 띄우지 않고 **실행 중 세계에 주입**한다 (control/inject 파일드롭
    → InjectControl 폴링 → inject_now — 잔류 승계 §0-2). STOP만이 세계를 종료한다.
  · 시나리오 id = 파일명 stem, [a-z0-9_] 화이트리스트 (경로 주입 차단)
  · 중지 = terminate (Windows에선 하드 종료 — producer flush 일부 유실 가능, 데모 허용 범위.
    graceful은 콘솔 Ctrl+C 경로가 담당 — 헌법 6-2)
  · 경계 유지: 이 러너는 fdc.raw 위쪽(주입)만 만진다 — 판정·Incident 조작 경로 없음 (스펙 §핵심 원칙)
"""

from __future__ import annotations

import logging
import re
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import shutil
import yaml

log = logging.getLogger("sim-runner")

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = ROOT / "src" / "simulator" / "scenarios"
_ID_RE = re.compile(r"^[a-z0-9_]+$")
LOG_MAX = 400
PARAMS_PATH = ROOT / "config" / "params.yaml"


def _bootstrap() -> str:
    """시뮬 자식에게 넘길 Kafka 브로커 주소 — env 우선, 호스트 기본값 폴백 (2026-08-07).

    compose 가 주입하는 `KAFKA_BOOTSTRAP_SERVERS`(내부 리스너 `kafka:9093`)를 1순위로 쓴다.
    ⚠️ 다만 **gateway 컨테이너에 실제로 들어오는 키는 `KAFKA_BOOTSTRAP`** 이다 (#130 의 gateway
    블록 · C 리뷰 #136) — 2순위가 gateway 의 현행 정본이고, `run_stack.ps1` 이 호스트 창에 넣는
    키도 같다. 둘 다 보므로 어느 쪽이 지워져도 살아남는다 — **한 줄로 줄이지 말 것.**
    """
    return (os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
            or os.environ.get("KAFKA_BOOTSTRAP")
            or "localhost:9092")


def _sim_cfg() -> dict:
    """params.yaml simulator 섹션 (inject_control·persistent_run) — 실패 시 안전 기본값."""
    try:
        d = yaml.safe_load(PARAMS_PATH.read_text(encoding="utf-8")) or {}
        return d.get("simulator") or {}
    except Exception as e:                              # noqa: BLE001 — 게이트웨이 생존
        log.warning(f"params.yaml 로드 실패 — 기본값 사용: {e}")
        return {}


class RunnerBusy(RuntimeError):
    """(구) 동시 실행 1개 가드 — 상주화(M3 ②) 이후 start()는 미발생. import 호환 보존."""


class UnknownScenario(KeyError):
    """화이트리스트 밖 또는 존재하지 않는 시나리오."""


class SimulatorRunner:
    """kafka_producer 서브프로세스 수명주기 + 로그 링버퍼."""

    def __init__(self, scenarios_dir: Path = SCENARIOS_DIR, root: Path = ROOT):
        self.scenarios_dir = scenarios_dir
        self.root = root
        self._proc: Optional[subprocess.Popen] = None
        self._meta: dict = {}
        self._logs: deque[str] = deque(maxlen=LOG_MAX)
        self._lock = threading.Lock()
        cfg = _sim_cfg()
        self.control_dir = self.root / (cfg.get("inject_control") or {}).get("dir", "control/inject")
        pr = cfg.get("persistent_run") or {}
        self.default_delay = float(pr.get("delay_sec", 0.01))
        self.default_limit = int(pr.get("limit_wafers", 200000))

    # ---- 조회 ---------------------------------------------------------------
    def list_scenarios(self) -> list[dict]:
        """scenarios/*.yaml → 표시 메타 (id=파일 stem, 스키마 필드 요약)."""
        out = []
        for p in sorted(self.scenarios_dir.glob("*.yaml")):
            try:
                sc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception as e:  # 손상 yaml — 목록에서 제외하지 않고 사유 표기
                out.append({"id": p.stem, "error": f"yaml 파싱 실패: {e}"})
                continue
            chambers = sc.get("target_chambers") or ([sc.get("target_chamber")]
                                                     if sc.get("target_chamber") else [])
            out.append({
                "id": p.stem,
                "scenario_id": sc.get("scenario_id"),
                "pattern": sc.get("pattern"),
                "chambers": chambers,
                "description": sc.get("description"),
                "ground_truth_type": (sc.get("ground_truth") or {}).get("type"),
            })
        return out

    def _resolve(self, sid: str) -> Path:
        if not _ID_RE.match(sid or ""):
            raise UnknownScenario(f"시나리오 id 형식 위반: {sid!r}")
        p = self.scenarios_dir / f"{sid}.yaml"
        if not p.exists():
            raise UnknownScenario(f"시나리오 없음: {sid}")
        return p

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        p = self._proc
        return {
            "running": self.is_running(),
            "scenario": self._meta.get("scenario"),
            "started_at": self._meta.get("started_at"),
            "pid": (p.pid if p is not None else None),
            "returncode": (p.poll() if p is not None else None),
            "last_line": (self._logs[-1] if self._logs else None),
            "mode": "persistent",                       # M3 ② — RUN=주입·STOP=세계 종료
            "injected": list(self._meta.get("injected", [])),
        }

    def logs(self, n: int = 120) -> list[str]:
        return list(self._logs)[-max(1, min(n, LOG_MAX)):]

    # ---- 수명주기 (M3 ② 상주화: 최초 RUN=baseline 기동+주입, 이후 RUN=주입만) ----
    def _cmd_baseline(self, delay: float, limit: int) -> list[str]:
        """상시 baseline 커맨드 (--scenario 없음·주입 대기) — 테스트 오버라이드 시임.

        --loop 필수 (2026-08-03): replay 소스가 8,247장뿐이라 loop 없이는 --limit 과
        무관하게 소진 후 정상종료한다. --delay 는 wafer 가 아니라 row 마다 걸리므로
        실측 6.6 wafer/s · 68 row/s, 8,247장(약 85,000행) = 약 25분이면 끝난다.
        "상주 세계" 라는 이름과 달리 발표 도중 조용히 죽는다 — 8/1~8/3 데이터 정지의 실제 원인.

        `--bootstrap` 명시 (2026-08-07, #126 C 리뷰 🔴): 자식이 env 를 상속하긴 하지만
        **명시적으로 넘겨** 게이트웨이가 보는 브로커와 시뮬이 붙는 브로커를 한 값으로 묶는다.
        컨테이너에서 이게 어긋나면 시뮬 자식은 정상으로 뜨고 주입 로그도 찍히는데 **데이터가
        한 톨도 안 흐른다** — confluent producer 가 연결 실패를 예외 없이 로컬 큐에 쌓기 때문.
        """
        return [sys.executable, "-m", "src.simulator.kafka_producer",
                "--bootstrap", _bootstrap(),
                "--delay", str(delay), "--limit", str(limit),
                "--loop", "--inject-control"]

    def _inject(self, path: Path, sid: str) -> str:
        """시나리오 yaml을 control 디렉토리에 드롭 — 실행 중 세계 주입 (파일드롭 채널)."""
        self.control_dir.mkdir(parents=True, exist_ok=True)
        dest = self.control_dir / f"{time.strftime('%Y%m%dT%H%M%S')}_{sid}.yaml"
        shutil.copyfile(path, dest)
        self._meta.setdefault("injected", []).append(
            {"scenario": sid, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        self._meta["scenario"] = sid                    # 마지막 주입 표시 (status 호환)
        log.info(f"💉 주입 드롭: {sid} → {dest.name}")
        # 시연 선심기 훅 (2026-08-11 · 리허설 대본 시나리오 2) — 등록된 시나리오면 처분(HOLD)
        # 자동 적재를 백그라운드로 예약한다. 실패해도 주입 본선에는 영향 없음(모듈 내 격리).
        try:
            from .dispo_seed import maybe_seed
            maybe_seed(sid)
        except Exception:                               # noqa: BLE001 — 연출 훅이 주입을 못 죽인다
            log.exception("dispo_seed 훅 실패 — 주입은 계속")
        return dest.name

    def start(self, sid: str, delay: float | None = None, limit: int | None = None) -> dict:
        """RUN = 상시 세계에 주입. 세계가 없으면 baseline 자동 기동 후 주입.

        delay/limit은 baseline 최초 기동에만 적용 (이후 RUN에서는 무시 — 세계는 하나).
        반환 status에 mode 필드: "injected"(주입만) / "started+injected"(기동+주입).
        """
        path = self._resolve(sid)
        with self._lock:
            started = False
            if not self.is_running():
                self._logs.clear()
                d = self.default_delay if delay is None else float(delay)
                n = self.default_limit if limit is None else int(limit)
                env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # Windows cp949 깨짐 방지
                self._proc = subprocess.Popen(
                    self._cmd_baseline(d, n), cwd=str(self.root), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1)
                self._meta = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                              "injected": []}
                threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
                started = True
                log.info(f"▶ 상시 세계 기동 (delay={d}, limit={n}, pid={self._proc.pid})")
            self._inject(path, sid)
        out = self.status()
        out["mode"] = "started+injected" if started else "injected"
        return out

    def _pump(self, proc: subprocess.Popen) -> None:
        """stdout → 링버퍼 (프로세스 종료까지)."""
        try:
            for line in proc.stdout or []:
                self._logs.append(line.rstrip("\n"))
        except Exception as e:  # 파이프 종료 등 — 수집만 멈춘다
            self._logs.append(f"[pump 종료: {e}]")

    def stop(self) -> dict:
        with self._lock:
            if not self.is_running():
                return self.status()
            self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._logs.append("[강제 종료 — terminate 5s 초과]")
        log.info(f"⏹ 상시 세계 종료 (마지막 주입: {self._meta.get('scenario')})")
        return self.status()
