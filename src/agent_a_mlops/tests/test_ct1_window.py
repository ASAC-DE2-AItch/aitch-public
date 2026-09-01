# -*- coding: utf-8 -*-
"""CT① 학습창 절단·누수 가드 단위 테스트 (WP-1 / 헌법 1-3).

실행:  python -m pytest src/agent_a_mlops/tests/test_ct1_window.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lean85"))
import lean85_pipeline as lp  # noqa: E402


def _wt(dates) -> pd.DataFrame:
    """[C64, wf_ts] 경량 테이블 생성 헬퍼."""
    return pd.DataFrame({lp.ID_COL: [f"W{i}" for i in range(len(dates))],
                         "wf_ts": pd.to_datetime(list(dates), format="ISO8601")})


def _pm(pairs):
    """[(날짜, verdict)] → parse_pm_entries_full 형식."""
    return [{"date": pd.Timestamp(d), "type": "major", "verdict": v} for d, v in pairs]


# ── 기본 절단 ──────────────────────────────────────────────────────────────
def test_window_bounds_are_left_open_right_closed():
    """창 = (cutoff−window, cutoff] — 좌측 개구간·우측 폐구간."""
    now = pd.Timestamp("2026-08-04")
    tbl = _wt(["2025-08-04", "2025-08-05", "2026-08-03", "2026-08-04"])
    mask, meta = lp.slice_train_window(tbl, now, window_days=365, pm_events=[],
                                       buffer_days=0, require_complete_loud_regime=False)
    assert meta["cutoff"].startswith("2026-08-04")
    assert meta["start"].startswith("2025-08-04")
    # 2025-08-04 = start 와 동일 → 개구간이라 제외. 2026-08-04 = cutoff → 폐구간이라 포함
    assert list(mask) == [False, True, True, True]


def test_buffer_days_shifts_cutoff_back():
    """H 버퍼만큼 학습 상한이 당겨진다 — 평가창 (d−H, d] 는 학습되지 않는다 (D4)."""
    now = pd.Timestamp("2026-08-04")
    tbl = _wt(["2026-08-02", "2026-08-03T12:00", "2026-08-04"])
    mask, meta = lp.slice_train_window(tbl, now, window_days=365, pm_events=[],
                                       buffer_days=1, require_complete_loud_regime=False)
    assert meta["cutoff"].startswith("2026-08-03T00:00")
    assert list(mask) == [True, False, False]


def test_rejects_bad_args():
    """방어적 인자 검증 — 조용한 오작동보다 즉시 실패."""
    tbl = _wt(["2026-01-01"])
    with pytest.raises(ValueError):
        lp.slice_train_window(tbl.rename(columns={"wf_ts": "x"}), "2026-08-04",
                              window_days=365, pm_events=[])
    with pytest.raises(ValueError):
        lp.slice_train_window(tbl, "2026-08-04", window_days=0, pm_events=[])
    with pytest.raises(ValueError):
        lp.slice_train_window(tbl, "2026-08-04", window_days=365, pm_events=[],
                              buffer_days=-1)


# ── 완결 요란 레짐 확장 (D2) ───────────────────────────────────────────────
def test_no_extension_when_window_has_complete_loud_regime():
    """창 안에 완결 요란 레짐이 있으면 확장하지 않는다."""
    now = pd.Timestamp("2026-08-04")
    pm = _pm([("2026-01-10", "loud"), ("2026-04-10", "loud")])   # 레짐 1-10 ~ 4-10 완결
    mask, meta = lp.slice_train_window(_wt(["2026-05-01"]), now, window_days=365,
                                       pm_events=pm, buffer_days=0)
    assert meta["complete_loud_regimes_in_window"] == 1
    assert meta["extended"] is False
    assert meta["effective_window_days"] == 365.0


def test_extends_left_to_last_complete_loud_regime():
    """창 안에 완결 요란 레짐이 0개면 직전 완결 레짐 시작까지 좌측 확장."""
    now = pd.Timestamp("2026-08-04")
    # 레짐 2024-01-05 ~ 2024-03-05 = 완결이지만 365일 창(2025-08-04~) 밖
    pm = _pm([("2024-01-05", "loud"), ("2024-03-05", "loud")])
    tbl = _wt(["2024-02-01", "2025-01-01", "2026-01-01"])
    mask, meta = lp.slice_train_window(tbl, now, window_days=365, pm_events=pm,
                                       buffer_days=0)
    assert meta["complete_loud_regimes_in_window"] == 0
    assert meta["extended"] is True
    assert meta["start"].startswith("2024-01-05")
    assert meta["regime_used"][0].startswith("2024-01-05")
    assert list(mask) == [True, True, True]           # 확장분까지 전부 편입


def test_extension_disabled_by_flag():
    """조항을 끄면 확장하지 않는다 (config 스위치 이행)."""
    now = pd.Timestamp("2026-08-04")
    pm = _pm([("2024-01-05", "loud"), ("2024-03-05", "loud")])
    _, meta = lp.slice_train_window(_wt(["2026-01-01"]), now, window_days=365,
                                    pm_events=pm, buffer_days=0,
                                    require_complete_loud_regime=False)
    assert meta["extended"] is False


def test_warmup_when_no_complete_regime_exists_anywhere():
    """완결 레짐이 아예 없으면 확장 불가 — 워밍업으로 표시하고 창은 그대로."""
    now = pd.Timestamp("2026-08-04")
    pm = _pm([("2026-07-01", "loud")])                # 다음 PM 없음 = 진행 중
    _, meta = lp.slice_train_window(_wt(["2026-07-15"]), now, window_days=365,
                                    pm_events=pm, buffer_days=0)
    assert meta["extend_failed"] is True
    assert meta["extended"] is False


def test_ongoing_regime_is_not_complete():
    """마지막 요란 PM 은 다음 PM 이 없으므로 '완결'로 세지 않는다."""
    pm = _pm([("2026-01-10", "loud"), ("2026-04-10", "loud")])
    regimes = lp.complete_loud_regimes(pm)
    assert len(regimes) == 1                          # (1-10 ~ 4-10) 하나뿐
    assert regimes[0][0] == pd.Timestamp("2026-01-10")


def test_quiet_pm_does_not_open_a_loud_regime():
    """조용 PM 은 레짐 시작이 아니다 — 다만 다음 PM(경계)으로는 쓰인다."""
    pm = _pm([("2026-01-10", "quiet"), ("2026-02-10", "loud"), ("2026-03-10", "quiet")])
    regimes = lp.complete_loud_regimes(pm)
    assert regimes == [(pd.Timestamp("2026-02-10"), pd.Timestamp("2026-03-10"))]


def test_regime_ending_after_cutoff_is_not_complete():
    """컷오프 이후에 끝나는 레짐은 미완결 — 미래 정보를 창 결정에 쓰지 않는다."""
    pm = _pm([("2026-01-10", "loud"), ("2026-09-10", "loud")])
    assert lp.complete_loud_regimes(pm, until=pd.Timestamp("2026-08-04")) == []


def test_legacy_pm_entries_default_to_loud():
    """레거시 날짜 문자열 엔트리는 요란으로 본다 (계약 §8-B-1: pm_log = 요란만 기입)."""
    entries = lp.parse_pm_entries_full(["2026-01-10", {"date": "2026-04-10"}])
    assert [e["verdict"] for e in entries] == ["loud", "loud"]
    assert entries[0]["date"] < entries[1]["date"]


# ── 누수 가드 (헌법 1-3) ───────────────────────────────────────────────────
def test_window_leakage_guard_raises_not_asserts():
    """컷오프 이후 학습 wafer 는 ValueError — `assert` 가 아니라 `raise` (헌법 7장)."""
    with pytest.raises(ValueError, match="1-3"):
        lp.assert_window_no_leakage(["2026-08-05"], "2026-08-04")
    lp.assert_window_no_leakage(["2026-08-04", "2026-01-01"], "2026-08-04")   # 경계 포함 OK
    lp.assert_window_no_leakage([], "2026-08-04")                              # 빈 셋 OK


def test_disjoint_wafer_guard():
    """학습·평가 wafer 교집합은 ValueError (C64 그룹 분할 강제)."""
    with pytest.raises(ValueError, match="1-3"):
        lp.assert_disjoint_wafers({"W1", "W2"}, {"W2", "W3"})
    lp.assert_disjoint_wafers({"W1"}, {"W2"})


def test_slice_and_eval_are_disjoint_by_construction():
    """시간 경계로 가르면 학습·평가가 겹치지 않는다 — 게이트 B2 의 전제."""
    now = pd.Timestamp("2026-08-04")
    tbl = _wt(["2026-08-01", "2026-08-03T12:00", "2026-08-04"])
    mask, meta = lp.slice_train_window(tbl, now, window_days=365, pm_events=[],
                                       buffer_days=1, require_complete_loud_regime=False)
    ts = pd.to_datetime(tbl["wf_ts"])
    cutoff = pd.Timestamp(meta["cutoff"])
    train, ev = set(tbl.loc[mask, lp.ID_COL]), set(tbl.loc[ts > cutoff, lp.ID_COL])
    lp.assert_disjoint_wafers(train, ev)
    assert train and ev


# ── tz 정규화 (리뷰 B1 · 헌법 7장 "pm_log tz-aware 기입" 계열) ────────────
def test_aware_now_does_not_raise():
    """오케스트레이터가 넘기는 aware UTC 로도 창 절단이 동작해야 한다.

    회귀 방지: 정규화가 없으면 naive wafer 시각과 비교하는 순간
    `TypeError: Cannot compare tz-naive and tz-aware timestamps` 로 **자동 경로 전체가
    죽는다** (수동 `--once --now <naive>` 만 통과해 리허설을 그대로 넘어간다).
    """
    tbl = _wt(["2026-08-01", "2026-08-03"])
    mask, meta = lp.slice_train_window(tbl, "2026-08-04T00:00:00+00:00", window_days=365,
                                       pm_events=[], buffer_days=0,
                                       require_complete_loud_regime=False)
    assert list(mask) == [True, True]
    assert "+" not in meta["cutoff"], meta["cutoff"]          # 메타도 naive 로 남는다


def test_aware_pm_events_and_offset_conversion():
    """aware pm_log·offset 있는 컷오프도 naive UTC 로 변환돼 비교된다."""
    pm = [{"date": "2024-01-05T00:00:00+09:00", "verdict": "loud"},
          {"date": "2024-03-05T00:00:00+09:00", "verdict": "loud"}]
    entries = lp.parse_pm_entries_full(pm)
    assert all(e["date"].tz is None for e in entries)
    # KST 자정 = 전날 15:00 UTC
    assert entries[0]["date"] == pd.Timestamp("2024-01-04 15:00:00")
    regimes = lp.complete_loud_regimes(entries, until="2026-08-04T00:00:00+00:00")
    assert len(regimes) == 1


def test_parse_pm_log_mixed_tz_does_not_raise():
    """naive·aware 혼재 pm_log 도 naive UTC 로 정규화된다 (코드리뷰 3차 M-2, 2026-08-08).

    회귀 방지: 정규화 전에는 `_parse_pm_entries` 의 `sorted` 가 naive/aware 비교
    TypeError 를 던져 — aware 엔트리 **1건**에 due_event(상주 60초 폴링)·
    `_meta_features`(조립·재학습·게이트·consumer 서빙)가 전부 죽었다
    (헌법 7장 "pm_log tz-aware 기입" 계열. full 판만 방어가 있던 비대칭의 해소).
    """
    mixed = [{"date": "2026-01-01 00:00:00"},
             {"date": "2026-02-01T00:00:00+09:00", "type": "minor"}]
    events = lp.parse_pm_log(mixed)
    assert [t.tz for t, _ in events] == [None, None]
    # KST 자정 = 전날 15:00 UTC — full 판(parse_pm_entries_full)과 같은 축
    assert events[1][0] == pd.Timestamp("2026-01-31 15:00:00")
    assert events[0][0] < events[1][0]
    assert [k for _, k in events] == ["major", "minor"]


def test_naive_utc_helper():
    """naive_utc 는 aware 를 UTC 로 변환해 벗기고, naive 는 그대로 둔다."""
    assert lp.naive_utc("2026-08-04T09:00:00+09:00") == pd.Timestamp("2026-08-04 00:00:00")
    assert lp.naive_utc("2026-08-04 00:00:00") == pd.Timestamp("2026-08-04 00:00:00")


# ── 네이밍 가드 (D7 · 헌법 7장) ────────────────────────────────────────────
def test_stamp_name_max_covers_fixed_tag_vocabulary():
    """고정 어휘 tag 전부가 32자 상한 안에 들어간다 (감사 컬럼 VARCHAR(32))."""
    for tag in lp.CT1_TAGS:
        assert len(f"lean85_20260804_020000_{tag}") <= lp.STAMP_NAME_MAX
    assert len("lean85_20260804_020000_" + "x" * 12) > lp.STAMP_NAME_MAX   # 반증 케이스


# ── raw 스키마 검사 (F8) ───────────────────────────────────────────────────
import ct1_assemble_trainset as asm  # noqa: E402


def test_load_raw_reorders_equal_columns(tmp_path):
    """컬럼 **집합**이 같고 순서만 다르면 첫 파일 순서로 정렬해 수용한다.

    구 구현은 순서 포함 비교라 raise 했고, 메시지가 "누락 [] / 초과 []" 로 나와
    원인을 오도했다 — 실제 스키마 사고와 구분되지 않는 실패다.
    """
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    pd.DataFrame({"C64": ["W1"], "C11": [1.0], "C65": [800.0]}).to_csv(a, index=False)
    pd.DataFrame({"C65": [810.0], "C64": ["W2"], "C11": [2.0]}).to_csv(b, index=False)

    raw = asm.load_raw([a, b])
    assert list(raw.columns) == ["C64", "C11", "C65"]
    assert list(raw["C64"]) == ["W1", "W2"]          # 값이 컬럼을 따라간다 (재정렬 정합)
    assert list(raw["C65"]) == [800.0, 810.0]


def test_load_raw_rejects_true_schema_diff(tmp_path):
    """집합이 다르면 여전히 즉시 실패한다 — 누락 컬럼명이 메시지에 실린다."""
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    pd.DataFrame({"C64": ["W1"], "C11": [1.0], "C65": [800.0]}).to_csv(a, index=False)
    pd.DataFrame({"C64": ["W2"], "C11": [2.0]}).to_csv(b, index=False)

    with pytest.raises(ValueError, match="C65"):
        asm.load_raw([a, b])
