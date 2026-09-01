# -*- coding: utf-8 -*-
"""CT① 연속 FAILED 상한 회귀 테스트 (2026-08-07 코드리뷰 H2).

문제였던 것: `FAILED` 는 "그날을 소비하지 않고 backoff 후 재시도"였다. 일시 장애(DB 다운·
락 경합)에는 맞지만 **결정론적 실패**(데이터 glob 오경로·스키마 불일치·재학습 코드 결함)를
구분하지 못해, fast-fail 이면 하루 수백 회 재시도한다 — 매 시도가 새 SEQ 의 `ct_decisions`
행(RUNNING→FAILED)과 subprocess 를 남기므로 감사 테이블 오염 + CPU 낭비 + 진짜 원인
로그가 홍수에 묻힌다.

여기서 지키는 계약:
  · 상한 미만 FAILED → `retry` (그날 미소비 — 일시 장애 가정 유지)
  · 상한 도달        → `give_up` (오늘 소비 + ★에스컬레이션 1건. 익일 정기가 자연 재시도)
  · `LOCKED`         → 세지 않고 항상 `retry` (다른 프로세스가 도는 중 = 실패 아님)
  · 완주(SKIP 등)    → 카운터 리셋 → 다음 FAILED 는 다시 1부터
  · 날짜가 바뀌면    → 카운터 리셋 (하루 단위)

실행: python -m pytest src/agent_a_mlops/tests/test_ct1_failure_cap.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LEAN85 = Path(__file__).resolve().parents[1] / "lean85"
sys.path.insert(0, str(_LEAN85))
import ct1_orchestrator as orch  # noqa: E402

CAP3 = {"ct1_max_daily_failures": 3}
DAY = "20260807"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """상태 파일 + `_mark_state` 메모리 미러(F2b)를 테스트마다 격리."""
    monkeypatch.setattr(orch, "STATE_PATH", tmp_path / "ct1_state.json")
    orch._STATE_MEM.clear()
    yield
    orch._STATE_MEM.clear()


def test_failed_retries_until_cap():
    """상한 직전까지는 오늘을 소비하지 않는다 — 일시 장애의 회복 여지를 남긴다."""
    assert orch.after_scheduled(CAP3, "FAILED", DAY) == "retry"
    assert orch.after_scheduled(CAP3, "FAILED", DAY) == "retry"
    assert orch._state()["failed_streak"] == 2


def test_cap_reached_consumes_the_day():
    """상한 도달 = 결정론적 실패로 판정 → 오늘 소비(호출자가 last_scheduled_day 마킹)."""
    for _ in range(2):
        orch.after_scheduled(CAP3, "FAILED", DAY)
    assert orch.after_scheduled(CAP3, "FAILED", DAY) == "give_up"


def test_success_resets_the_counter():
    """완주하면 카운터가 풀린다 — 어제의 실패가 오늘의 상한을 미리 깎지 않게."""
    orch.after_scheduled(CAP3, "FAILED", DAY)
    orch.after_scheduled(CAP3, "FAILED", DAY)
    assert orch.after_scheduled(CAP3, "SKIP", DAY) == "consume"
    assert not orch._state().get("failed_streak")
    assert orch.after_scheduled(CAP3, "FAILED", DAY) == "retry"      # 다시 1부터


def test_new_day_resets_the_counter():
    """카운터는 **당일** 연속이다 — 날짜가 바뀌면 1부터 (익일 자연 재시도의 전제)."""
    for _ in range(3):
        orch.after_scheduled(CAP3, "FAILED", DAY)
    assert orch.after_scheduled(CAP3, "FAILED", "20260808") == "retry"
    assert orch._state()["failed_streak"] == 1


def test_locked_is_not_counted():
    """LOCKED 는 다른 프로세스가 도는 중이라 실패가 아니다 — 세지도, 소비하지도 않는다."""
    for _ in range(10):
        assert orch.after_scheduled(CAP3, "LOCKED", DAY) == "retry"
    assert not orch._state().get("failed_streak")


def test_cap_zero_disables_the_guard():
    """0·음수·판독 불가 = 기능 해제(무한 재시도 = 구 동작). 잘못된 값이 배포를 막지 않게."""
    for cfg in ({"ct1_max_daily_failures": 0}, {"ct1_max_daily_failures": -1}):
        orch._mark_state(failed_streak=0, failed_day="")
        for _ in range(5):
            assert orch.after_scheduled(cfg, "FAILED", DAY) == "retry"


def test_bad_cap_value_falls_back_to_default():
    """`"세번"` 같은 오설정은 코드 폴백(3)으로 흡수한다 — 예외로 루프를 죽이지 않는다."""
    cfg = {"ct1_max_daily_failures": "세번"}
    assert orch.after_scheduled(cfg, "FAILED", DAY) == "retry"
    assert orch.after_scheduled(cfg, "FAILED", DAY) == "retry"
    assert orch.after_scheduled(cfg, "FAILED", DAY) == "give_up"     # 기본값 3 적용


def test_corrupt_state_does_not_raise():
    """상태 파일이 수기 편집·손상으로 비수치를 담아도 던지지 않는다.

    던지면 main 루프의 `except` 가 받아 `last_scheduled_day` 마킹에 도달하지 못하고,
    H2 가 막으려던 **재시도 루프로 그대로 되돌아간다**.
    """
    orch._mark_state(failed_day=DAY, failed_streak="abc")
    assert orch.after_scheduled(CAP3, "FAILED", DAY) == "retry"
    assert orch._state()["failed_streak"] == 1                      # 0부터 다시


def test_default_cap_when_key_missing():
    """params 로드 실패(`{}`)여도 상한은 살아 있다 — 폭주 차단은 기본 동작이다."""
    assert orch.after_scheduled({}, "FAILED", DAY) == "retry"
    assert orch.after_scheduled({}, "FAILED", DAY) == "retry"
    assert orch.after_scheduled({}, "FAILED", DAY) == "give_up"


def test_escalation_is_logged_once_at_cap(caplog):
    """★에스컬레이션은 알림 채널이 없으므로 **로그가 실체**다 (D10·0표본 경보와 동일)."""
    import logging
    with caplog.at_level(logging.ERROR, logger=orch.log.name):
        for _ in range(2):
            orch.after_scheduled(CAP3, "FAILED", DAY)
        assert not [r for r in caplog.records if "★에스컬레이션" in r.getMessage()]
        orch.after_scheduled(CAP3, "FAILED", DAY)
    hits = [r for r in caplog.records if "★에스컬레이션" in r.getMessage()]
    assert len(hits) == 1 and "FAILED" in hits[0].getMessage()
