# =============================================================================
# tests/test_idle_flush_config.py  —  a-pred ↔ B(spc) 유휴 flush 순서 계약
# =============================================================================
# 값 두 개를 **서로 다른 파트가 반씩** 들고 있고(A `lean85.idle_flush_sec` /
# B `spc.wafer_idle_flush_sec`), 어긋났을 때의 증상이 예외가 아니라 **조인율 0%**
# (양쪽 다 정상 로그, 산출물만 빔)라 사람이 못 본다. 그래서 테스트로 고정한다.
#
# ⚠️ 기대값을 리터럴로 재현하지 않는다 — params 를 못박으면 값이 바뀔 때 "테스트가 낡은
#   것"과 "계약이 깨진 것"을 구별할 수 없다. **양쪽 값을 실제로 읽어 부등식만** 본다.
# =============================================================================
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

PARAMS_PATH = Path(__file__).resolve().parents[3] / "config" / "params.yaml"


def _params() -> dict:
    with open(PARAMS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _consumer():
    """consumer 모듈 — torch·confluent 등 무거운 의존이 없으면 skip."""
    try:
        return importlib.import_module("src.agent_a_mlops.consumer")
    except Exception as e:                                   # noqa: BLE001
        pytest.skip(f"consumer import 불가(런타임 의존 부재): {e}")


def test_params_has_idle_flush_key():
    """정본 키 존재 — env 기본값 하드코딩으로 되돌아가면 red (헌법 6-1)."""
    lean = _params().get("lean85") or {}
    assert "idle_flush_sec" in lean, "lean85.idle_flush_sec 가 params 에 없다(정본 키)"
    assert float(lean["idle_flush_sec"]) >= 0


def test_flush_ordering_holds_in_params():
    """**계약**: a-pred 유효 flush < B flush.

    유효치 = `lean85.idle_flush_sec` + `IDLE_CHECK_INTERVAL_SEC`(유휴 점검 주기).
    설정값끼리 비교하면 26 < 30 처럼 통과처럼 보이지만 유효치 31 이라 조용히 역전된다.
    """
    p = _params()
    a_flush = float((p["lean85"])["idle_flush_sec"])
    b_flush = float((p["spc"])["wafer_idle_flush_sec"])
    if a_flush <= 0:
        pytest.skip("유휴 flush 비활성 — 순서 개념 없음")

    mod = _consumer()
    effective = a_flush + mod.IDLE_CHECK_INTERVAL_SEC
    assert effective < b_flush, (
        f"유휴 flush 순서 역전: a-pred 유효 {effective}s "
        f"(= {a_flush} + {mod.IDLE_CHECK_INTERVAL_SEC}) >= B {b_flush}s. "
        "예측 조인이 비게 된다(예외 없이 산출물만 빔)"
    )


def test_consumer_loads_idle_flush_from_params():
    """consumer 가 **params 를 읽는다** — env 기본값 300 하드코딩이면 red."""
    mod = _consumer()
    expected = float((_params()["lean85"])["idle_flush_sec"])
    assert mod.IDLE_FLUSH_SEC == expected


def test_env_override_warns(monkeypatch, caplog):
    """env override 는 허용하되 **WARNING 을 남긴다** — 창을 닫으면 정본으로 돌아가므로.

    (AE 번들이 세션 override 로만 지정돼 있다가 창을 닫자 에러 없이 구 번들로 되돌아간
    선례 #153 과 같은 함정을 로그로 노출한다.)
    """
    import logging
    mod = _consumer()
    monkeypatch.setenv("LEAN85_IDLE_FLUSH_SEC", "42")
    with caplog.at_level(logging.WARNING, logger="mlops-consumer"):
        got = mod._load_idle_flush_sec()
    assert got == 42.0
    assert [r for r in caplog.records if "override" in r.getMessage()]


def test_ordering_guard_logs_error_on_inversion(monkeypatch, caplog):
    """가드가 역전을 **ERROR 로 노출**하는지 — 기동은 막지 않는다(관측 전용).

    조용한 상태 게이트를 카운터만 두고 노출 없이 운용하지 말 것(헌법 7장) — 여기서는
    로그가 유일한 관측 채널이라, 그 로그가 실제로 나오는지까지 확인한다.
    """
    import logging
    mod = _consumer()
    b_flush = float((_params()["spc"])["wafer_idle_flush_sec"])
    monkeypatch.setattr(mod, "IDLE_FLUSH_SEC", b_flush)      # 유효치 = b_flush + 5 → 역전
    with caplog.at_level(logging.ERROR, logger="mlops-consumer"):
        mod._check_idle_flush_ordering()
    assert [r for r in caplog.records if r.levelno == logging.ERROR and "역전" in r.getMessage()]
