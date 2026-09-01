# -*- coding: utf-8 -*-
"""M3 ② SimulatorRunner 상주화 단위 테스트 — Kafka 무의존 (스텁 baseline 프로세스).

검증: 최초 RUN=기동+드롭 · 2번째 RUN=드롭만(프로세스 재사용, RunnerBusy 미발생) ·
STOP=세계 종료 · status mode/injected. 실행: python tests/test_simulator_runner_persistent.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gateway.simulator_runner import SimulatorRunner  # noqa: E402

SC_YAML = """scenario_id: T-{sid}
pattern: shift
target_chamber: SIM_CH_1
sensor: C11
magnitude_sigma: 1.0
start_after_wafers: 0
ground_truth: test
"""


class StubRunner(SimulatorRunner):
    """baseline을 무해한 sleep 프로세스로 대체 — 수명주기만 검증."""

    def _cmd_baseline(self, delay: float, limit: int) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]


def _runner():
    scen = Path(tempfile.mkdtemp(prefix="scen_"))
    for sid in ("alpha", "beta"):
        (scen / f"{sid}.yaml").write_text(SC_YAML.format(sid=sid), encoding="utf-8")
    r = StubRunner(scenarios_dir=scen)
    r.control_dir = Path(tempfile.mkdtemp(prefix="ctl_"))  # params 무관 격리
    return r


def test_first_run_starts_world_and_injects():
    r = _runner()
    try:
        out = r.start("alpha")
        assert out["mode"] == "started+injected" and out["running"] is True
        assert len(list(r.control_dir.glob("*.yaml"))) == 1     # 드롭 1건
        assert [i["scenario"] for i in out["injected"]] == ["alpha"]
    finally:
        r.stop()


def test_second_run_injects_without_respawn():
    """★핵심 — 실행 중 RUN은 새 프로세스를 띄우지 않고 드롭만 (상시 세계 유지)."""
    r = _runner()
    try:
        pid1 = r.start("alpha")["pid"]
        out2 = r.start("beta")                                  # 구 RunnerBusy 지점
        assert out2["mode"] == "injected" and out2["pid"] == pid1   # 같은 세계
        assert len(list(r.control_dir.glob("*.yaml"))) == 2
        assert [i["scenario"] for i in out2["injected"]] == ["alpha", "beta"]
        assert out2["scenario"] == "beta"                       # 마지막 주입 표시
    finally:
        r.stop()


def test_stop_ends_world():
    r = _runner()
    r.start("alpha")
    st = r.stop()
    time.sleep(0.1)
    assert r.is_running() is False and st["mode"] == "persistent"


def test_status_shape():
    """status 계약 — S11이 읽는 필드 (mode·injected 추가, 기존 키 유지)."""
    r = _runner()
    st = r.status()
    for k in ("running", "scenario", "pid", "returncode", "last_line", "mode", "injected"):
        assert k in st
    assert st["running"] is False and st["mode"] == "persistent"


# ── 브로커 주소 전달 (2026-08-07, #126 C 리뷰 🔴) ───────────────────────────
#   컨테이너에서 시뮬 자식이 localhost:9092 를 보면 Kafka 에 못 붙는데, confluent producer 가
#   **연결 실패를 예외로 안 내고 로컬 큐에 쌓는다** — 프로세스는 정상, 주입 로그도 정상,
#   데이터만 0. 그래서 "안 넘겼다"를 코드로 못박는다. 오늘 하루의 실패들과 같은 모양이다.
_ENV_KEYS = ("KAFKA_BOOTSTRAP_SERVERS", "KAFKA_BOOTSTRAP")


def _with_env(**kv):
    """env 를 임시 설정하는 컨텍스트 — pytest fixture 없이(스크립트 실행 겸용)."""
    import contextlib
    import os

    @contextlib.contextmanager
    def _cm():
        saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        try:
            for k in _ENV_KEYS:
                os.environ.pop(k, None)
            for k, v in kv.items():
                os.environ[k] = v
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _cm()


def test_baseline_cmd_carries_bootstrap():
    """★ `--bootstrap` 이 커맨드에 실린다 — 빠지면 컨테이너에서 조용히 0건."""
    import gateway.simulator_runner as M
    with _with_env(KAFKA_BOOTSTRAP_SERVERS="kafka:9093"):
        cmd = SimulatorRunner()._cmd_baseline(0.01, 100)
        assert "--bootstrap" in cmd
        assert cmd[cmd.index("--bootstrap") + 1] == "kafka:9093"
        assert M._bootstrap() == "kafka:9093"


def test_bootstrap_fallback_order():
    """env 우선순위 — KAFKA_BOOTSTRAP_SERVERS > KAFKA_BOOTSTRAP(구 키) > 호스트 기본값."""
    import gateway.simulator_runner as M
    with _with_env():                                  # 둘 다 없음
        assert M._bootstrap() == "localhost:9092"      # 기존 거동 보존
    with _with_env(KAFKA_BOOTSTRAP="old:9092"):        # run_stack.ps1 구 키
        assert M._bootstrap() == "old:9092"
    with _with_env(KAFKA_BOOTSTRAP_SERVERS="kafka:9093", KAFKA_BOOTSTRAP="old:9092"):
        assert M._bootstrap() == "kafka:9093"          # compose 키가 이긴다


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
