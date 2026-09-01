# -*- coding: utf-8 -*-
"""P6-3 러너 단위 테스트 — Kafka·fastapi 무의존 (가짜 커맨드로 수명주기 검증).

실행: python tests/test_simulator_runner.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gateway.simulator_runner import (  # noqa: E402
    SimulatorRunner, UnknownScenario)


class FakeCmdRunner(SimulatorRunner):
    """kafka_producer 대신 짧은 에코 프로세스 — 수명주기·로그 펌프 검증용.

    M3 ② 상주화 이후 러너가 호출하는 것은 `_cmd_baseline(delay, limit)`이다 (구 `_cmd`는
    소멸 — 오버라이드가 안 먹어 실제 producer가 떴고 즉시 죽어 테스트가 오판했다, PR #61 CI).
    주입 드롭도 레포 `control/inject`를 오염시키므로 임시 디렉토리로 돌린다.
    """
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.control_dir = Path(tempfile.mkdtemp(prefix="runner_ctl_"))

    def _cmd_baseline(self, delay, limit):
        code = ("import time,sys\n"
                f"print('FAKE baseline delay={delay} limit={limit}')\n"
                "sys.stdout.flush()\n"
                "time.sleep(5)\n"          # 상주 세계 — STOP 전까지 살아 있어야 한다
                "print('FAKE done')\n")
        return [sys.executable, "-u", "-c", code]


def _wait_log(r, needle: str, timeout: float = 5.0) -> bool:
    """로그 펌프는 별도 스레드라 start() 직후엔 아직 비어 있을 수 있다 — 폴링 대기.

    CI에서 `FAKE baseline` 단언이 간헐 실패한 원인(PR #61 2차): 자식 프로세스 기동·출력
    시점과 단언 시점의 경쟁. 고정 sleep 대신 조건 폴링으로 결정론화한다.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if any(needle in l for l in r.logs()):
            return True
        time.sleep(0.05)
    return False


def test_list_scenarios_fields():
    r = SimulatorRunner()
    lst = r.list_scenarios()
    ids = {s["id"] for s in lst}
    assert "drift_c11" in ids and "qual_loud_ch4" in ids
    q = next(s for s in lst if s["id"] == "qual_loud_ch4")
    assert q["pattern"] == "pm_reset" and q["chambers"] == ["SIM_CH_4"]
    assert q["ground_truth_type"] == "regime_transition"


def test_resolve_rejects_traversal():
    r = SimulatorRunner()
    for bad in ("../etc/passwd", "a/b", "DRIFT_C11", "", "drift c11"):
        try:
            r._resolve(bad)
            raise AssertionError(f"거부 실패: {bad!r}")
        except UnknownScenario:
            pass
    try:
        r._resolve("no_such_scenario_xyz")
        raise AssertionError("존재하지 않는 시나리오 통과")
    except UnknownScenario:
        pass


def test_lifecycle_and_persistent_inject():
    """M3 ② 상주화: 1회차=기동+주입 / 2회차=주입만(같은 pid — 세계 유지). STOP만 종료.

    구 `test_lifecycle_and_busy_guard`(RunnerBusy 기대)를 대체한다 — RUN이 '새 실행'에서
    '상시 세계에 주입'으로 바뀌면서 동시 실행 가드는 설계상 폐지됐다 (docs/M3_월드스테이트_개조_v1 §4).
    """
    r = FakeCmdRunner()
    st = r.start("drift_c11", delay=0.5, limit=7)
    assert st["running"] is True and st["scenario"] == "drift_c11" and st["pid"]
    assert st["mode"] == "started+injected" and st["injected"] and len(st["injected"]) == 1
    pid1 = st["pid"]

    st2 = r.start("qual_loud_ch4")                    # 실행 중 재-RUN = 주입만
    assert st2["mode"] == "injected" and st2["pid"] == pid1    # 세계 유지 (원복 금지 전제)
    assert st2["scenario"] == "qual_loud_ch4" and len(st2["injected"]) == 2
    assert len(list(r.control_dir.glob("*.yaml"))) == 2        # 파일드롭 2건

    assert _wait_log(r, "FAKE baseline")              # 펌프 스레드 경쟁 — 폴링 대기
    r.stop()                                          # STOP만 세계 종료
    assert r.status()["running"] is False


def test_stop_terminates():
    r = FakeCmdRunner()
    r.start("drift_c11")
    st = r.stop()
    assert st["running"] is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f(); print(f"  ✓ {f.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
