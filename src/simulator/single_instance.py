# -*- coding: utf-8 -*-
"""프로세스 단일 기동 가드 — OS 레벨 뮤텍스 (loopback 포트 bind).

배경 (2026-08-06 실측):
    `SimulatorRunner`의 "상시 세계 1개" 가드는 게이트웨이 프로세스 메모리
    (`self._proc`)에만 존재해 두 경로를 막지 못한다.
      ⓐ CLI 직접 기동 — `python -m src.simulator.kafka_producer`는 러너를 안 거친다.
      ⓑ 게이트웨이 재시작 — `_runner`가 새로 생성돼 `_proc=None`이 되는데
         이전에 띄운 producer는 고아로 살아남는다. 다음 RUN이 하나 더 띄운다.
    구 `RunnerBusy` 가드는 상주화(M3 ②, 2026-07-28) 때 무력화됐고 import 호환
    껍데기만 남아 있다.

    실제 피해: producer 2개가 같은 파티션(key=chamber_id)에 동시 발행하면 각자의
    배치·linger 타이밍이 엇갈려 **도착 순서 ≠ 발행 순서**가 된다. SPC 엔진의
    단조성 가드(`nelson_engine.py:143`·`tttm_engine.py:213`)가 이를 전량 드롭해
    `spc_violations` 적재가 42분간 정지했다. 예외도 ERROR도 없이 멈춘다.

왜 포트 bind인가:
    · 프로세스가 어떻게 죽든(kill -9·전원 차단 포함) OS가 즉시 해제한다 →
      **stale lock이 원천적으로 없다.** PID 파일 방식은 강제 종료 시 남은 파일이
      정상 기동을 막아, 발표 직전에 더 나쁜 실패를 만든다.
    · Windows·Linux 동일하게 동작한다.

주의 (Windows):
    · **`SO_REUSEADDR`를 절대 켜지 않는다.** Linux와 의미가 반대라 Windows에서는
      중복 bind를 허용해버려 가드가 조용히 무력화된다. Windows에서는 대신
      `SO_EXCLUSIVEADDRUSE`를 켜 하이재킹까지 막는다.
    · `127.0.0.1`에만 bind한다. `0.0.0.0`이면 Windows 방화벽 팝업이 뜬다.

한계:
    같은 OS 안에서만 유효하다. producer를 컨테이너로 옮기면 네트워크 네임스페이스가
    분리돼 이 가드는 무력해진다 (현재 `docker-compose.yml`에 simulator 서비스가 없어
    호스트 프로세스 전용임을 확인했다 — 컨테이너화 시 Kafka `transactional.id`
    펜싱 등으로 교체할 것).
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Optional

log = logging.getLogger("single-instance")

# 획득한 소켓을 프로세스 수명 동안 살려 둔다 (GC로 닫히면 락이 풀린다).
_HELD: list[socket.socket] = []


class AlreadyRunning(RuntimeError):
    """같은 이름의 프로세스가 이미 실행 중 — 중복 기동 차단."""


def _read_pid_hint(path: Optional[Path]) -> Optional[str]:
    """진단용 PID 힌트 읽기 — 실패해도 조용히 None (락 판정과 무관)."""
    if path is None:
        return None
    try:
        pid = path.read_text(encoding="utf-8").strip()
        return pid or None
    except OSError:
        return None


def _write_pid_hint(path: Optional[Path]) -> None:
    """진단용 PID 힌트 기록 — best-effort. 이 파일은 **락이 아니다**."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(str(os.getpid()), encoding="utf-8")
        os.replace(tmp, path)          # 원자 교체 (헌법 7장 — 부분 쓰기 노출 방지)
    except OSError as e:
        log.warning(f"PID 힌트 기록 실패(무시): {e}")


def acquire(port: int, name: str = "프로세스",
            pid_hint_path: Optional[Path] = None) -> socket.socket:
    """단일 기동 락 획득. 이미 실행 중이면 `AlreadyRunning`.

    Args:
        port: 뮤텍스로 쓸 loopback 포트. 서비스 포트가 아니라 **락 전용**이다.
        name: 오류 메시지에 쓸 사람이 읽는 이름.
        pid_hint_path: 진단용 PID 파일 경로 (선택). 락 판정에는 쓰지 않는다.

    Returns:
        점유 중인 소켓. 호출자가 참조를 버려도 모듈이 붙잡아 둔다.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR는 켜지 않는다 (Windows에서 중복 bind 허용 — 가드 무력화).
    excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if excl is not None:                                   # Windows 전용
        try:
            s.setsockopt(socket.SOL_SOCKET, excl, 1)
        except OSError as e:                               # 미지원 환경 — bind만으로도 배타적
            log.debug(f"SO_EXCLUSIVEADDRUSE 미적용(무시): {e}")
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        s.close()
        holder = _read_pid_hint(pid_hint_path)
        who = f" — 기존 PID {holder} 추정" if holder else ""
        raise AlreadyRunning(
            f"{name}이(가) 이미 실행 중입니다 (락 포트 {port} 점유{who}). "
            f"중복 기동은 Kafka 파티션 도착 순서를 뒤집어 SPC 적재를 조용히 멈춥니다 "
            f"— 기존 프로세스를 먼저 종료하세요. "
            f"(포트가 무관한 프로그램에 점유된 경우 params.yaml "
            f"simulator.single_instance.port를 바꾸세요)"
        ) from e

    _HELD.append(s)
    _write_pid_hint(pid_hint_path)
    log.info(f"🔒 단일 기동 락 획득 — {name} (포트 {port}, PID {os.getpid()})")
    return s


def release(sock: socket.socket) -> None:
    """락 해제 — 테스트용. 실제 운영에서는 프로세스 종료 시 OS가 해제한다."""
    try:
        _HELD.remove(sock)
    except ValueError:
        pass
    try:
        sock.close()
    except OSError:
        pass
