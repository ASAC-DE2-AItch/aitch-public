# -*- coding: utf-8 -*-
"""single_instance — producer 중복 기동 가드 (2026-08-07).

배경: producer 2개가 같은 파티션에 동시 발행하면 도착 순서가 뒤집혀 SPC 단조성
가드가 전량 드롭하고 적재가 조용히 멎는다 (2026-08-06 실측 42분). 이 테스트는
"두 번째 기동이 실제로 막히는가"를 **같은 프로세스 안**과 **별도 OS 프로세스**
양쪽에서 확인한다 — 뮤텍스가 OS 레벨이어야 의미가 있으므로 후자가 핵심이다.
"""

import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.simulator import single_instance as si  # noqa: E402


def _free_port() -> int:
    """지금 비어 있는 포트 하나 — 테스트 격리용(운영 포트와 무관)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def port() -> int:
    return _free_port()


def test_second_acquire_blocked(port):
    """★핵심 — 같은 포트 두 번째 획득은 AlreadyRunning."""
    lock = si.acquire(port, "테스트")
    try:
        with pytest.raises(si.AlreadyRunning):
            si.acquire(port, "테스트")
    finally:
        si.release(lock)


def test_release_allows_reacquire(port):
    """해제 후에는 다시 획득된다 (락이 영구 고착되지 않는다)."""
    si.release(si.acquire(port, "테스트"))
    lock2 = si.acquire(port, "테스트")
    si.release(lock2)


def test_so_reuseaddr_must_stay_off(port):
    """★Windows 회귀 가드 — SO_REUSEADDR가 켜지면 중복 bind가 허용돼 가드가 죽는다.

    Linux와 의미가 반대라 눈으로는 안 잡힌다. 옵션 값 자체를 못박는다.
    """
    lock = si.acquire(port, "테스트")
    try:
        assert lock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
    finally:
        si.release(lock)


def test_pid_hint_written_and_surfaced(port, tmp_path):
    """진단용 PID 힌트가 기록되고, 차단 메시지에 그 PID가 실린다."""
    hint = tmp_path / "sub" / "producer.pid"
    lock = si.acquire(port, "테스트", pid_hint_path=hint)
    try:
        import os
        assert hint.read_text(encoding="utf-8").strip() == str(os.getpid())
        with pytest.raises(si.AlreadyRunning) as e:
            si.acquire(port, "테스트", pid_hint_path=hint)
        assert str(os.getpid()) in str(e.value)
    finally:
        si.release(lock)


def test_pid_hint_missing_is_not_fatal(port, tmp_path):
    """힌트 파일이 없어도 차단은 정상 동작한다 (락은 소켓이지 파일이 아니다)."""
    lock = si.acquire(port, "테스트")
    try:
        with pytest.raises(si.AlreadyRunning):
            si.acquire(port, "테스트", pid_hint_path=tmp_path / "없는파일.pid")
    finally:
        si.release(lock)


_CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, r"{root}")
    from src.simulator import single_instance as si
    try:
        si.acquire({port}, "child")
    except si.AlreadyRunning:
        sys.exit(2)
    print("ACQUIRED", flush=True)
    time.sleep({hold})
    """
)


def test_second_os_process_is_blocked(port):
    """★★ 진짜 검증 — 별도 OS 프로세스 두 번째가 종료코드 2로 거부된다.

    프로세스 내부 가드(구 RunnerBusy)가 못 막던 바로 그 경로다.
    """
    first = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(root=ROOT, port=port, hold=15)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # 첫 프로세스가 락을 잡을 때까지 대기 (고정 sleep 금지 — 느린 머신에서 흔들린다)
        deadline = time.time() + 30
        while time.time() < deadline:
            line = first.stdout.readline()
            if "ACQUIRED" in line:
                break
            if first.poll() is not None:
                pytest.fail(f"첫 프로세스가 조기 종료: rc={first.returncode}")
        else:
            pytest.fail("첫 프로세스가 30초 내 락을 잡지 못함")

        second = subprocess.run(
            [sys.executable, "-c", _CHILD.format(root=ROOT, port=port, hold=0)],
            capture_output=True, text=True, timeout=60)
        assert second.returncode == 2, f"두 번째가 안 막혔다: {second.stdout}{second.stderr}"
    finally:
        first.kill()
        first.wait(timeout=10)


def test_lock_released_when_process_killed(port):
    """★ stale lock 없음 — 강제 종료(kill)해도 다음 기동이 정상 획득한다.

    PID 파일 방식을 쓰지 않은 이유. 발표 직전 강제 종료 후 재기동이 막히면
    원래 문제보다 나쁘다.
    """
    first = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(root=ROOT, port=port, hold=15)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if "ACQUIRED" in first.stdout.readline():
            break
        if first.poll() is not None:
            pytest.fail(f"첫 프로세스가 조기 종료: rc={first.returncode}")
    else:
        pytest.fail("첫 프로세스가 30초 내 락을 잡지 못함")

    first.kill()
    first.wait(timeout=10)

    # OS가 해제할 때까지 잠깐 (TIME_WAIT 없음 — bind만 하고 listen/connect 안 했다)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            si.release(si.acquire(port, "재획득"))
            return
        except si.AlreadyRunning:
            time.sleep(0.2)
    pytest.fail("강제 종료 후 10초가 지나도 락이 안 풀렸다 (stale lock)")
