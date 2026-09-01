# -*- coding: utf-8 -*-
"""M3 ② 실행 중 주입 — 파일드롭 제어 채널 (Kafka 무의존).

상시프로세스(kafka_producer.run)가 wafer마다 이 폴러를 호출한다. S11 RUN(또는 사람이
직접)이 `control/inject/`에 시나리오 YAML을 떨어뜨리면, 다음 스캔에서 집어
`ScenarioInjector.inject_now()`로 장전한다 — start는 대상 챔버의 **현재 wafer 순번**으로
스탬프되므로 효과는 "지금부터" 시작하고, 잔류 패턴은 컷이 바뀌어도 원복되지 않는다 (§0-2).

설계 근거 (docs/M3_월드스테이트_개조_v1.md §4 — 파일 드롭 권장):
  · 크로스플랫폼(Windows 데모 머신)·Kafka 무의존·단위 테스트 용이
  · utf-8-sig 읽기 — PowerShell이 BOM 붙여 저장해도 파싱 (헌법 §7 .ps1 교훈)
  · 실패 파일은 failed/ 로 이동 + 로그, 루프는 절대 죽지 않는다 (헌법 6-2 정신)
  · 처리 파일은 processed/ 로 이동 (동일 파일 재주입 방지 + 감사 흔적)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import yaml

from .scenario_injector import ScenarioInjector, normalize_scenario

log = logging.getLogger("inject-control")

SUB_PROCESSED = "processed"
SUB_FAILED = "failed"


class InjectControl:
    """control 디렉토리 폴러 — 드롭된 시나리오 YAML을 런타임 주입으로 변환."""

    def __init__(self, control_dir: Path, injector: ScenarioInjector,
                 min_interval_sec: float = 1.0):
        """control_dir은 없으면 생성. min_interval_sec 간격으로만 실제 스캔한다."""
        self.control_dir = Path(control_dir)
        self.injector = injector
        self.min_interval = float(min_interval_sec)
        self._last_scan: float | None = None      # None = 미스캔 (최초 poll은 항상 스캔)
        self.n_injected = 0
        self.n_failed = 0
        self.control_dir.mkdir(parents=True, exist_ok=True)
        (self.control_dir / SUB_PROCESSED).mkdir(exist_ok=True)
        (self.control_dir / SUB_FAILED).mkdir(exist_ok=True)

    def _move(self, p: Path, sub: str) -> None:
        """처리/실패 파일 이동 — 타임스탬프 접두로 충돌 방지 (이동 실패는 로그만)."""
        try:
            dest = self.control_dir / sub / f"{time.strftime('%Y%m%dT%H%M%S')}_{p.name}"
            p.replace(dest)
        except OSError as e:                               # noqa: PERF203
            log.warning(f"파일 이동 실패({sub}): {p.name} — {e}")

    def poll(self, idx_by_chamber: dict[str, int]) -> list[dict]:
        """드롭 파일 스캔 → 장전. 반환 = 이번 호출에 장전된 정규화 시나리오 목록.

        start 스탬프 = 대상 챔버들의 현재 순번 **최대값** (소급 발동 방지 — 뒤처진 챔버는
        따라잡는 wafer부터 k=0). 아직 스트림에 안 나온 챔버는 0으로 간주.
        """
        now = time.monotonic()
        # ⚠ monotonic 원점은 플랫폼별(리눅스=부팅 후 초) — 0.0 초기값과 비교하면 갓 부팅한
        # CI 머신에서 첫 스캔이 스로틀에 걸린다(실측: PR #61 CI 실패). None 센티넬로 분리.
        if self._last_scan is not None and now - self._last_scan < self.min_interval:
            return []
        self._last_scan = now

        loaded: list[dict] = []
        for p in sorted(self.control_dir.glob("*.y*ml")):   # *.yaml + *.yml
            try:
                sc = yaml.safe_load(p.read_text(encoding="utf-8-sig"))
                if not isinstance(sc, dict):
                    raise ValueError("YAML 최상위가 dict가 아님")
                sc = normalize_scenario(sc, origin=p.name)
                at = max((idx_by_chamber.get(ch, 0) for ch in sc["chambers"]), default=0)
                stamped = self.injector.inject_now(sc, at)
                self._move(p, SUB_PROCESSED)
                self.n_injected += 1
                loaded.append(stamped)
                log.info(f"💉 라이브 주입: {sc['scenario_id']} ({sc['pattern']} "
                         f"{sc['sensors']} @{sc['chambers']}, start#{at}) ← {p.name}")
            except Exception as e:                          # noqa: BLE001 — 루프 생존 (6-2)
                self.n_failed += 1
                log.warning(f"주입 파일 실패 — failed/ 이동: {p.name} — {e}")
                self._move(p, SUB_FAILED)
        return loaded

    def status(self) -> dict:
        """상태 요약 — 대기/누적 카운트 (S11 로그 패널·디버깅용)."""
        return {
            "dir": str(self.control_dir),
            "pending": len(list(self.control_dir.glob("*.y*ml"))),
            "injected": self.n_injected,
            "failed": self.n_failed,
        }
