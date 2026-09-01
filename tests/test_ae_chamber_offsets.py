# -*- coding: utf-8 -*-
"""AE 챔버 기준선 오프셋 로더 가드 — 리뷰 M5 회귀 (2026-08-08).

배경: `_ae_chamber_offsets()` 가 무가드였다. 파일 부재·`offsets` 키 부재·비 dict
JSON·비수치 오프셋이 전부 예외로 상위 AE try 에 새고, 캐시가 `None` 으로 남아
**wafer 마다** 더미 폴백 + 경고 + 파일 IO 재시도를 반복했다 — AE 실값 전면 축퇴가
wafer 단위 로그로만 보이는 상태다.

계약(이 파일이 지키는 것):
  · 어떤 입력에도 **예외를 올리지 않는다** — 설정 문제가 AE 실추론을 막지 않는다
  · 실패는 **빈 dict 부정 캐시**로 확정 — 재호출에서 파일 IO 를 다시 하지 않는다
  · 값은 `_finite` 통과분만 — 비유한 오프셋 1개가 컬럼을 NaN 으로 만들지 못한다
  · `ae_mode` 의 `live-decal` 은 **실제 차감됐을 때만** — 스위치만 보고 찍지 않는다

실행: python -m pytest tests/test_ae_chamber_offsets.py -q
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
import pandas as pd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# consumer 는 confluent_kafka·lean85_pipeline(xgboost) 의존 — 없으면 모듈 통째 스킵.
# ⚠️ sys.exit() 금지: 수집 단계 SystemExit 는 pytest 세션 전체를 죽인다 (헌법 7장)
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops"))
try:
    import consumer  # noqa: E402
except Exception as e:                       # pragma: no cover - 환경 의존
    pytest.skip(f"consumer import 실패(의존성 미설치): {e}", allow_module_level=True)


@pytest.fixture(autouse=True)
def _reset_cache():
    """모듈 전역 캐시·경고 기억은 테스트마다 초기화 (순서 의존 제거)."""
    consumer._AE_OFFSETS_CACHE = None
    consumer._ae_offset_miss_warned.clear()
    yield
    consumer._AE_OFFSETS_CACHE = None
    consumer._ae_offset_miss_warned.clear()


def _point(tmp_path, monkeypatch, payload, *, write=True) -> Path:
    """오프셋 파일을 tmp 로 갈아끼운다. payload 가 str 이면 원문 그대로 쓴다."""
    p = tmp_path / "chamber_offsets.json"
    if write:
        p.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                     encoding="utf-8")
    monkeypatch.setattr(consumer, "AE_OFFSETS_PATH", p)
    return p


# ── 정상 경로 ──────────────────────────────────────────────────────────────
def test_valid_file_loads_and_coerces_to_float(tmp_path, monkeypatch):
    _point(tmp_path, monkeypatch,
           {"note": "메타 키는 무시", "offsets": {"SIM_CH_1": {"C11": -11.6, "C1": 2}}})
    offs = consumer._ae_chamber_offsets()
    assert offs == {"SIM_CH_1": {"C11": -11.6, "C1": 2.0}}
    assert all(isinstance(v, float) for v in offs["SIM_CH_1"].values())


def test_repo_config_is_loadable():
    """실물 `config/chamber_offsets.json`(B 7/10 전달분)이 계약을 만족하는지 — 회귀 감시."""
    if not consumer.AE_OFFSETS_PATH.exists():        # pragma: no cover - 워크트리 의존
        pytest.skip("config/chamber_offsets.json 부재")
    offs = consumer._ae_chamber_offsets()
    assert offs, "실물 설정이 빈 dict 로 축퇴했다 — 부정 캐시가 걸렸다는 뜻"
    assert all(isinstance(per, dict) and per for per in offs.values())


def test_result_is_cached_without_rereading(tmp_path, monkeypatch):
    p = _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 1.0}}})
    first = consumer._ae_chamber_offsets()
    p.unlink()                                       # 캐시 뒤 파일이 사라져도
    assert consumer._ae_chamber_offsets() is first   # 같은 객체 — 재로드 없음


# ── 실패 경로: 전부 예외 없이 빈 dict 부정 캐시 ────────────────────────────
@pytest.mark.parametrize("payload, write", [
    (None, False),                       # 파일 부재
    ("{ not json", True),                # 파싱 실패
    ('"123"', True),                     # json.loads 성공 ≠ dict
    ("[1, 2]", True),                    # 리스트
    ("null", True),                      # null
    ({"seed": 7}, True),                 # offsets 키 부재
    ({"offsets": []}, True),             # offsets 가 비 dict
    ({"offsets": "SIM_CH_1"}, True),     # offsets 가 문자열
])
def test_bad_input_degrades_to_empty_dict(tmp_path, monkeypatch, payload, write):
    _point(tmp_path, monkeypatch, payload, write=write)
    assert consumer._ae_chamber_offsets() == {}      # 예외가 새지 않는다


def test_failure_is_negatively_cached(tmp_path, monkeypatch):
    """실패 후 파일이 정상화돼도 그 프로세스 수명 동안 재시도하지 않는다.

    wafer 마다 파일 IO 를 다시 하던 것이 M5 의 핵심 증상이라, '고쳐도 안 읽는다'가
    **의도한 계약**이다. 설정 수정 후엔 consumer 를 재기동한다.
    """
    p = _point(tmp_path, monkeypatch, None, write=False)
    assert consumer._ae_chamber_offsets() == {}
    p.write_text(json.dumps({"offsets": {"CH1": {"C11": 1.0}}}), encoding="utf-8")
    assert consumer._ae_chamber_offsets() == {}


def test_failure_logs_error_once(tmp_path, monkeypatch, caplog):
    _point(tmp_path, monkeypatch, None, write=False)
    with caplog.at_level(logging.ERROR, logger=consumer.log.name):
        consumer._ae_chamber_offsets()
        consumer._ae_chamber_offsets()
        consumer._ae_chamber_offsets()
    hits = [r for r in caplog.records if "오프셋 로드 실패" in r.getMessage()]
    assert len(hits) == 1


# ── 부분 손상: 나쁜 항목만 떨어뜨리고 나머지는 살린다 ──────────────────────
def test_zero_offset_survives(tmp_path, monkeypatch):
    """`0.0` 은 **유효 오프셋**이다 — 가드가 `if not f` 로 회귀하면 조용히 사라진다.

    다른 케이스가 전부 truthy 값이라 이 1건이 `is None` 판정의 유일한 파수꾼이다.
    """
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 0.0, "C12": 0, "C13": -0.0}}})
    assert consumer._ae_chamber_offsets() == {"CH1": {"C11": 0.0, "C12": 0.0, "C13": 0.0}}


def test_non_finite_and_non_numeric_offsets_are_dropped(tmp_path, monkeypatch):
    _point(tmp_path, monkeypatch, '{"offsets": {"CH1": {'
           '"C11": 1.5, "C12": null, "C13": "0.5", "C14": NaN, "C15": Infinity,'
           '"C16": true, "C17": [1]}}}')
    assert consumer._ae_chamber_offsets() == {"CH1": {"C11": 1.5}}


def test_non_dict_chamber_entry_is_dropped_others_survive(tmp_path, monkeypatch):
    _point(tmp_path, monkeypatch,
           {"offsets": {"CH1": {"C11": 1.0}, "CH2": "bad", "CH3": None, "CH4": [1, 2]}})
    assert consumer._ae_chamber_offsets() == {"CH1": {"C11": 1.0}}


def test_dropped_items_are_warned(tmp_path, monkeypatch, caplog):
    _point(tmp_path, monkeypatch, '{"offsets": {"CH1": {"C11": 1.0, "C12": NaN}}}')
    with caplog.at_level(logging.WARNING, logger=consumer.log.name):
        consumer._ae_chamber_offsets()
    assert any("비수치·비유한" in r.getMessage() for r in caplog.records)


# ── _ae_decal: 차감 여부가 ae_mode 를 가른다 ───────────────────────────────
def _trace(**cols) -> pd.DataFrame:
    return pd.DataFrame({k: [v, v] for k, v in cols.items()})


def test_decal_subtracts_and_reports_true(tmp_path, monkeypatch):
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 2.0, "C99": 5.0}}})
    tr = _trace(C11=10.0, C17=3.0)
    assert consumer._ae_decal(tr, "CH1") is True
    assert list(tr["C11"]) == [8.0, 8.0]              # 차감됨
    assert list(tr["C17"]) == [3.0, 3.0]              # 오프셋 없는 컬럼은 불변
    assert "C99" not in tr.columns                    # trace 에 없는 센서는 무시


def test_decal_false_for_unregistered_chamber(tmp_path, monkeypatch):
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 2.0}}})
    tr = _trace(C11=10.0)
    assert consumer._ae_decal(tr, "CH_UNKNOWN") is False
    assert list(tr["C11"]) == [10.0, 10.0]            # 손대지 않았다


def test_decal_false_when_offsets_failed_to_load(tmp_path, monkeypatch):
    """로드 실패 = 보정 없음. 스위치가 켜져 있어도 'live-decal' 로 찍히면 안 된다."""
    _point(tmp_path, monkeypatch, None, write=False)
    tr = _trace(C11=10.0)
    assert consumer._ae_decal(tr, "CH1") is False
    assert list(tr["C11"]) == [10.0, 10.0]


def test_decal_false_when_no_sensor_overlaps(tmp_path, monkeypatch):
    """등재된 챔버라도 겹치는 컬럼이 하나도 없으면 차감은 일어나지 않았다."""
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C99": 2.0}}})
    assert consumer._ae_decal(_trace(C11=10.0), "CH1") is False


def test_missing_chamber_warns_once_per_chamber(tmp_path, monkeypatch, caplog):
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 2.0}}})
    with caplog.at_level(logging.WARNING, logger=consumer.log.name):
        for _ in range(3):
            consumer._ae_decal(_trace(C11=1.0), "CH_A")
            consumer._ae_decal(_trace(C11=1.0), "CH_B")
    hits = [r for r in caplog.records if "미등재" in r.getMessage()]
    assert len(hits) == 2                             # 챔버당 1회 (wafer 마다 아님)


def test_registered_but_all_values_dropped_is_distinguishable(tmp_path, monkeypatch, caplog):
    """'챔버 자체 없음' 과 '등재됐지만 유효값 0개' 가 같은 문구면 개통 점검이 오독된다."""
    _point(tmp_path, monkeypatch, '{"offsets": {"CH1": {"C11": NaN}}}')
    with caplog.at_level(logging.WARNING, logger=consumer.log.name):
        assert consumer._ae_decal(_trace(C11=1.0), "CH1") is False
        assert consumer._ae_decal(_trace(C11=1.0), "CH_GONE") is False
    msgs = [r.getMessage() for r in caplog.records if "보정 없이 간다" in r.getMessage()]
    assert any("유효값 0개" in m and "CH1" in m for m in msgs)
    assert any("미등재" in m and "CH_GONE" in m for m in msgs)


# ── _ae_decal 도 예외를 올리지 않는다 (가드가 형제 함수로 이동하지 않게) ────
def test_decal_survives_non_numeric_column_dtype(tmp_path, monkeypatch, caplog):
    """`Series - float` 는 object·문자열 dtype 에서 TypeError 다.

    그게 상위 AE try 로 새면 **M5 와 똑같은 증상**(wafer 마다 더미 폴백)이 된다.
    실패 컬럼만 건너뛰고 나머지 차감은 그대로 간다.
    """
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 2.0, "C17": 1.0}}})
    tr = pd.DataFrame({"C11": ["a", "b"], "C17": [10.0, 10.0]})
    with caplog.at_level(logging.WARNING, logger=consumer.log.name):
        assert consumer._ae_decal(tr, "CH1") is True   # C17 은 차감됐다
    assert list(tr["C11"]) == ["a", "b"]               # 실패 컬럼은 원본 유지
    assert list(tr["C17"]) == [9.0, 9.0]
    assert any("차감 실패" in r.getMessage() for r in caplog.records)


def test_decal_false_when_every_column_fails(tmp_path, monkeypatch):
    """전부 실패면 차감 0건 = `live` — 예외 없이."""
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 2.0}}})
    assert consumer._ae_decal(pd.DataFrame({"C11": ["a", "b"]}), "CH1") is False


def test_decal_subtraction_failure_warns_once_per_sensor(tmp_path, monkeypatch, caplog):
    _point(tmp_path, monkeypatch, {"offsets": {"CH1": {"C11": 2.0, "C17": 1.0}}})
    with caplog.at_level(logging.WARNING, logger=consumer.log.name):
        for _ in range(3):
            consumer._ae_decal(pd.DataFrame({"C11": ["a"], "C17": ["b"]}), "CH1")
    hits = [r for r in caplog.records if "차감 실패" in r.getMessage()]
    assert len(hits) == 2                              # 센서당 1회


# ── 호출부: ae_mode 조립 (이번 수정의 핵심 성과가 나오는 줄) ───────────────
def _ae_mode_for(decal_switch, decal_result, ae_rec="rec"):
    """`flush_wafer` 의 ae_mode 3분기와 **같은 식**을 재현 — 회귀 감시용.

    호출부 전체(flush_wafer)는 Kafka·모델 픽스처가 필요해 여기선 조립식만 고정한다.
    식이 바뀌면 이 테스트가 아니라 `flush_wafer` 를 먼저 보라는 뜻이다.
    """
    decal = decal_switch and decal_result
    return ("live-decal" if decal else "live") if ae_rec else "dummy(빈trace)"


@pytest.mark.parametrize("switch, decal, rec, expected", [
    (False, False, "rec", "live"),            # 스위치 off
    (True,  True,  "rec", "live-decal"),      # 실제 차감됨
    (True,  False, "rec", "live"),            # 스위치 on 인데 차감 0건 → decal 아님(핵심)
    (True,  True,  None,  "dummy(빈trace)"),  # AE 산출 없음이 최우선
])
def test_ae_mode_reflects_actual_decal_not_the_switch(switch, decal, rec, expected):
    assert _ae_mode_for(switch, decal, rec) == expected


def test_flush_wafer_uses_the_same_ae_mode_expression():
    """위 재현식이 실제 `flush_wafer` 소스와 어긋나면 즉시 실패한다 (문서 표류 방지)."""
    src = Path(consumer.__file__).read_text(encoding="utf-8")
    assert 'ae_mode = (("live-decal" if decal else "live") if ae_rec else "dummy(빈trace)")' in src
    assert 'decal = _ae_decal(trace_ae, meta.get("chamber_id") or "default")' in src
