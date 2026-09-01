"""pm_log_writer 테스트 (B6-3-f — 요란 확정 시 pm_log.json 런타임 기입).

파일 I/O seam이라 tmp_path 격리로 돌린다. 검증 축 3종(스펙 §5):
① 원자적 append·부재 시 생성 ② 멱등 키 `(chamber_id, pm_count)` ③ 손상 파일 비파괴.
+ **A 파서 호환**(핵심 회귀 방지) — 산출물을 lean85 `parse_pm_log`로 라운드트립.
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from src.agent_b_spc.pm_log_writer import (
    DATE_FMT, PmLogWriter, resolve_pm_log_path, resolve_pm_log_pending_cap,
    shadow_copy_dirs, to_naive_utc, warn_if_shadow_copy)

_TS = "2026-07-28T05:12:33.000+00:00"


def _payload(chamber="SIM_CH_4", pm_count=2, ts=_TS):
    return {"chamber_id": chamber, "pm_count": pm_count, "pm_detected_at": ts, "verdict": "loud"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── 시간축 (D3 — naive UTC 고정) ────────────────────────────────────

def test_to_naive_utc_strips_tz():
    """★tz-aware → UTC 변환 후 tz 제거. 접미사가 남으면 A 파서 정렬이 TypeError."""
    assert to_naive_utc("2026-07-28T05:12:33.000+00:00") == "2026-07-28 05:12:33"
    assert to_naive_utc("2026-07-28T14:12:33+09:00") == "2026-07-28 05:12:33"   # KST → UTC
    assert to_naive_utc("2026-07-28T05:12:33Z") == "2026-07-28 05:12:33"        # py3.10 Z 치환


def test_to_naive_utc_accepts_datetime_and_naive_string():
    """datetime·naive 문자열도 수용 (naive는 이미 UTC wall-clock 간주 — A 트레이스 축)."""
    aware = datetime(2026, 7, 28, 5, 12, 33, tzinfo=timezone(timedelta(hours=9)))
    assert to_naive_utc(aware) == "2026-07-27 20:12:33"
    assert to_naive_utc("2026-07-28 05:12:33") == "2026-07-28 05:12:33"
    assert to_naive_utc("2026-07-28") == "2026-07-28 00:00:00"


def test_to_naive_utc_unparsable_returns_none():
    """파싱 불가 → None(호출자 fallback). 예외로 전이를 깨지 않는다."""
    assert to_naive_utc("언제인지 모름") is None
    assert to_naive_utc("") is None and to_naive_utc(None) is None


# ── 기입 (§3-3) ─────────────────────────────────────────────────────

def test_append_creates_file_when_absent(tmp_path):
    """파일 부재 → `[엔트리]`로 생성."""
    p = tmp_path / "pm_log.json"
    assert PmLogWriter(p).append(_payload()) is True
    data = _read(p)
    assert len(data) == 1 and data[0]["date"] == "2026-07-28 05:12:33"
    assert data[0]["type"] == "major" and data[0]["verdict"] == "loud"     # D4
    assert data[0]["chamber_id"] == "SIM_CH_4" and data[0]["pm_count"] == 2


def test_append_preserves_existing_entries_and_order(tmp_path):
    """기존 배열 보존 + 말미 append (legacy 문자열 엔트리 혼재 포함)."""
    p = tmp_path / "pm_log.json"
    p.write_text(json.dumps(["2018-12-24",
                             {"date": "2026-01-02", "type": "major", "verdict": "loud"}]),
                 encoding="utf-8")
    PmLogWriter(p).append(_payload())
    data = _read(p)
    assert len(data) == 3
    assert data[0] == "2018-12-24" and data[1]["date"] == "2026-01-02"     # 원본 무손상·순서
    assert data[2]["date"] == "2026-07-28 05:12:33"


def test_idempotent_on_redelivery(tmp_path):
    """★멱등 키 (chamber_id, pm_count, date) — 재전달(같은 개방 시각) 이중 방어."""
    p = tmp_path / "pm_log.json"
    w = PmLogWriter(p)
    assert w.append(_payload(pm_count=2)) is True
    assert w.append(_payload(pm_count=2)) is False            # 재전달 = 같은 개방 시각
    assert len(_read(p)) == 1
    assert w.append(_payload(pm_count=3)) is True             # 다음 PM 사이클은 기입
    assert w.append(_payload(chamber="SIM_CH_1", pm_count=2)) is True   # 타 챔버 동 pm_count
    assert len(_read(p)) == 3


def test_pm_count_reset_across_runs_still_writes(tmp_path):
    """★리뷰 H1 — 시뮬 재기동으로 pm_count가 0부터 다시 세도 **새 요란 PM은 기입**된다.

    `(chamber, pm_count)`만 키로 쓰면 2회차 리허설이 통째로 무기입(조용한 기능 정지)이라
    개방 시각(`date`)을 키에 포함한다. 재전달은 같은 시각을 실어오므로 방어는 유지된다.
    """
    p = tmp_path / "pm_log.json"
    w = PmLogWriter(p)
    assert w.append(_payload(pm_count=1, ts="2026-07-28T05:00:00+00:00")) is True   # 1회차
    assert w.append(_payload(pm_count=1, ts="2026-07-29T05:00:00+00:00")) is True   # 2회차
    assert [e["date"] for e in _read(p)] == ["2026-07-28 05:00:00", "2026-07-29 05:00:00"]


def test_pm_count_type_normalized_for_dedup(tmp_path):
    """리뷰 L1 — `"2"`(str)와 `2`(int)가 갈라져 이중 기입되지 않는다."""
    p = tmp_path / "pm_log.json"
    w = PmLogWriter(p)
    assert w.append(_payload(pm_count=2)) is True
    assert w.append(_payload(pm_count="2")) is False
    assert _read(p)[0]["pm_count"] == 2                       # 저장은 int 정규화


def test_quiet_verdict_is_rejected(tmp_path):
    """리뷰 L2 — "요란만 기입" 규약을 writer에서도 막는다(코어 가드 이중화)."""
    p = tmp_path / "pm_log.json"
    assert PmLogWriter(p).append(_payload() | {"verdict": "quiet"}) is False
    assert not p.exists()


def test_corrupt_file_is_not_overwritten(tmp_path):
    """★손상(비-JSON) → 원본 무손상(덮어쓰면 이력 소실) + 엔트리는 pending 보관(H1)."""
    p = tmp_path / "pm_log.json"
    p.write_text("{깨진 json", encoding="utf-8")
    w = PmLogWriter(p)
    assert w.append(_payload()) is False
    assert p.read_text(encoding="utf-8") == "{깨진 json"
    assert len(w.pending) == 1                      # 버리지 않는다(리뷰 H1)


def test_corrupt_path_does_not_swallow_pending(tmp_path, monkeypatch):
    """★★리뷰 H1 재현 회귀 — 교체 실패로 보관된 엔트리가 '손상 조우'에 함께 소멸하면 안 된다.

    ① replace 실패 → pending 1건 보관(H2 설계)
    ② 손상 상태에서 다음 요란 PM 도착 → 구현 이전엔 신규분도 pending도 **둘 다 소멸**
       (flush_pending이 pop한 뒤 손상 경로에서 버렸음)
    ③ 파일 복구 후 flush → 2건 모두 살아나야 한다 (구현 이전엔 0건·영구 유실)
    """
    import src.agent_b_spc.pm_log_writer as mod

    p = tmp_path / "pm_log.json"
    p.write_text("[]", encoding="utf-8")
    w = PmLogWriter(p, retries=1, backoff_sec=0.0)

    real = mod.os.replace                                       # ① 교체 실패
    monkeypatch.setattr(mod.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("잠김")))
    assert w.append(_payload(pm_count=1, ts="2026-07-28T05:00:00+00:00")) is False
    assert len(w.pending) == 1
    monkeypatch.setattr(mod.os, "replace", real)

    p.write_text("{깨진 json", encoding="utf-8")                # ② 손상 상태에서 다음 PM
    assert w.append(_payload(pm_count=2, ts="2026-07-29T05:00:00+00:00")) is False
    assert len(w.pending) == 2                                  # 보관분 + 신규분 모두 생존
    assert p.read_text(encoding="utf-8") == "{깨진 json"        # 원본 무손상

    p.write_text("[]", encoding="utf-8")                        # ③ 수동 복구 후 회수
    assert w.flush_pending(force=True) == 2                     # 쿨다운 무시(진짜 회수 시도)
    assert [e["pm_count"] for e in _read(p)] == [1, 2] and w.pending == []


def test_non_list_json_is_rejected(tmp_path):
    """`json.loads` 성공 ≠ list — dict/스칼라도 유효 JSON (CLAUDE.md 7장 isinstance 가드)."""
    p = tmp_path / "pm_log.json"
    for body in ('{"date": "2026-01-02"}', '"2026-01-02"', "null", "123"):
        p.write_text(body, encoding="utf-8")
        assert PmLogWriter(p).append(_payload()) is False
        assert p.read_text(encoding="utf-8") == body


def test_vanished_file_between_exists_and_open(tmp_path, monkeypatch):
    """TOCTOU — `exists()`~`open` 사이 소멸은 '손상'이 아니라 '부재'로 다뤄 정상 기입."""
    import builtins

    p = tmp_path / "pm_log.json"
    p.write_text("[]", encoding="utf-8")
    real_open, seen = builtins.open, []

    def _vanishing(file, *a, **kw):
        if str(file) == str(p) and not seen:     # 첫 읽기만 소멸 재현
            seen.append(1)
            raise FileNotFoundError(str(p))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", _vanishing)
    assert PmLogWriter(p).append(_payload()) is True
    assert len(_read(p)) == 1


def test_atomic_write_leaves_no_tmp(tmp_path):
    """tmp+os.replace 원자성 — 쓰기 후 tmp 잔존 없음 (A 부분 읽기 방지)."""
    p = tmp_path / "pm_log.json"
    PmLogWriter(p).append(_payload())
    assert [f.name for f in tmp_path.iterdir()] == ["pm_log.json"]


def test_write_failure_returns_false_and_queues(tmp_path, monkeypatch):
    """os.replace 영구 실패(Windows 잠금 등) → 재시도 소진 후 False·예외 전파 없음·pending 보관."""
    import src.agent_b_spc.pm_log_writer as mod

    p = tmp_path / "pm_log.json"
    calls = []

    def _boom(src, dst):
        calls.append((src, dst))
        raise PermissionError("잠김")

    monkeypatch.setattr(mod.os, "replace", _boom)
    w = PmLogWriter(p, retries=3, backoff_sec=0.0)
    assert w.append(_payload()) is False
    assert len(calls) == 3                          # backoff 재시도 소진
    assert not p.exists() and list(tmp_path.iterdir()) == []   # tmp 정리됨
    assert len(w.pending) == 1                      # 유실 방지 큐(리뷰 H2)


def test_pending_cap_errors_once_and_does_not_drop(tmp_path, caplog):
    """B7-1 D4 — pending이 cap 초과 시 ERROR 1회(래치), **drop 하지 않는다**(유실 유발 §4-1)."""
    import logging

    p = tmp_path / "pm_log.json"
    w = PmLogWriter(p, pending_cap=2)
    with caplog.at_level(logging.ERROR, logger="src.agent_b_spc.pm_log_writer"):
        for i in range(4):                          # cap=2 넘게 누적 (background=True → 재시도 로그는 DEBUG)
            w._queue({"chamber_id": "SIM_CH_1", "pm_count": i, "date": "x"}, background=True, why="t")
    assert len(w.pending) == 4                       # drop 없음 — 전부 보관
    cap_err = [r for r in caplog.records
               if r.levelno == logging.ERROR and "> cap" in r.getMessage()]
    assert len(cap_err) == 1                         # cap 초과 ERROR 정확히 1회(래치)


def test_pending_cap_latch_independent_of_last_error(tmp_path, caplog):
    """리뷰3a — cap 래치가 `_last_error`(write/load 실패 `_log_once`)와 **독립**.

    구현이 cap을 `_log_once("pending_cap")`로 했으면, write 실패 로그와 `_last_error` 슬롯을
    핑퐁해 cap ERROR가 매번 반복(래치 무력화)된다. 전용 bool 플래그면 write 로그가 슬롯을
    덮어써도 cap ERROR는 1회. (이 테스트는 구 구현에선 2회로 실패한다.)
    """
    import logging

    p = tmp_path / "pm_log.json"
    w = PmLogWriter(p, pending_cap=2)
    with caplog.at_level(logging.ERROR, logger="src.agent_b_spc.pm_log_writer"):
        for i in range(4):
            w._queue({"chamber_id": "SIM_CH_1", "pm_count": i, "date": "x"}, background=True, why="t")
            w._log_once("write:OSError", "pm_log 쓰기 실패")   # _last_error를 덮어씀(핑퐁 모사)
    cap_err = [r for r in caplog.records
               if r.levelno == logging.ERROR and "> cap" in r.getMessage()]
    assert len(cap_err) == 1


def test_resolve_pm_log_pending_cap_reads_key(tmp_path):
    """정상 파싱 — params `spc.pm_log_pending_cap` → int (config 경로 직접 검증, 리뷰 minor)."""
    p = tmp_path / "params.yaml"
    p.write_text("spc:\n  pm_log_pending_cap: 7\n", encoding="utf-8")
    assert resolve_pm_log_pending_cap(p) == 7


def test_resolve_pm_log_pending_cap_missing_key_raises(tmp_path):
    """키 부재 → raise(조용한 기본값 금지). 호출자가 잡아 기본값 폴백(3b)하되 함수 자체는 명시 실패."""
    p = tmp_path / "params.yaml"
    p.write_text("spc:\n  other: 1\n", encoding="utf-8")
    with pytest.raises(KeyError):
        resolve_pm_log_pending_cap(p)


def test_pending_is_retried_on_next_append(tmp_path, monkeypatch):
    """★리뷰 H2 — 실패분이 다음 기입 때 재시도돼 살아난다(코어는 phase 가드로 재호출 없음)."""
    import src.agent_b_spc.pm_log_writer as mod

    p = tmp_path / "pm_log.json"
    real = mod.os.replace
    monkeypatch.setattr(mod.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("잠김")))
    w = PmLogWriter(p, retries=1, backoff_sec=0.0)
    assert w.append(_payload(pm_count=2)) is False and len(w.pending) == 1
    monkeypatch.setattr(mod.os, "replace", real)    # 잠금 해제
    assert w.append(_payload(pm_count=3, ts="2026-08-01T05:00:00+00:00")) is True  # append=force
    dates = [e["pm_count"] for e in _read(p)]
    assert dates == [2, 3] and w.pending == []      # 보류분이 먼저·순서 보존


def test_flush_pending_is_noop_when_empty(tmp_path):
    """pending 없으면 파일을 건드리지 않는다."""
    assert PmLogWriter(tmp_path / "pm_log.json").flush_pending() == 0


def test_retries_floor_is_one(tmp_path):
    """리뷰 L3 — retries=0 주입 시 조용한 무동작이 되지 않게 하한 1."""
    assert PmLogWriter(tmp_path / "pm_log.json", retries=0).retries == 1


def test_fallback_to_confirmed_at_then_now_with_source(tmp_path):
    """개방 시각 미보유 → confirmed_at → 기입 시각 순 fallback + `date_source` 표기(리뷰 M2).

    ※ 현 배선상 코어는 `confirmed_at`을 넘기지 않는다(1차 미포함 — 스펙 §3-3). writer는
    후속 meta 배선을 위해 지원만 하며, 실효 fallback은 "기입 시각"이다(계약 §8-B-1 각주).
    """
    p = tmp_path / "pm_log.json"
    w = PmLogWriter(p)
    w.append({"chamber_id": "SIM_CH_4", "pm_count": 2, "pm_detected_at": None,
              "confirmed_at": "2026-07-28T06:00:00+00:00", "verdict": "loud"})
    assert _read(p)[0] | {"date": "2026-07-28 06:00:00"} == _read(p)[0]
    assert _read(p)[0]["date_source"] == "confirmed_at"
    w.append({"chamber_id": "SIM_CH_4", "pm_count": 3, "pm_detected_at": None, "verdict": "loud"})
    entry = _read(p)[1]
    datetime.strptime(entry["date"], DATE_FMT)      # 포맷 유효(기입 시각) — 실패 시 raise
    assert entry["date_source"] == "written_at_fallback"


def test_unparsable_ts_is_flagged_not_silent(tmp_path):
    """리뷰 M2 — 파싱 불가 시각이 정상 엔트리로 위장하지 않는다(date_source로 식별)."""
    p = tmp_path / "pm_log.json"
    PmLogWriter(p).append(_payload(ts="언제인지 모름"))
    assert _read(p)[0]["date_source"] == "written_at_fallback"


def test_callable_seam_alias(tmp_path):
    """`pm_logger=PmLogWriter(p)` 직접 주입(callable)도 append와 동일 동작."""
    p = tmp_path / "pm_log.json"
    assert PmLogWriter(p)(_payload()) is True
    assert len(_read(p)) == 1


def test_shadow_copy_dirs_cover_all_paths_before_repo_root():
    """리뷰 M4 — A `find_file`이 루트보다 먼저 뒤지는 4곳을 전부 검사 대상에 포함."""
    names = [d.name for d in shadow_copy_dirs()]
    assert names == ["lean85", "frozen", "agent_a_mlops", "문제1(하)"]


def test_warn_if_shadow_copy_detects_copy(tmp_path, caplog):
    """★사본이 있으면 경고+경로 반환 (split-brain 기동 가드). 없으면 빈 목록."""
    d1, d2 = tmp_path / "lean85", tmp_path / "frozen"
    d1.mkdir()
    d2.mkdir()
    assert warn_if_shadow_copy("pm_log.json", dirs=[d1, d2]) == []
    (d1 / "pm_log.json").write_text("[]", encoding="utf-8")
    with caplog.at_level("WARNING"):
        found = warn_if_shadow_copy("pm_log.json", dirs=[d1, d2])
    assert found == [d1 / "pm_log.json"] and "split-brain" in caplog.text


def test_resolve_pm_log_path_points_to_repo_root():
    """정본 경로 = **리포 루트**(params `pm_log.path`) — 매직값 아닌 config 경유(6-1)."""
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    p = resolve_pm_log_path()
    assert p.is_absolute()
    assert p.name == "pm_log.json"
    assert p.parent == repo_root                    # 루트 상대 해석 확인(사본 경로 아님)


def test_resolve_pm_log_path_raises_on_missing_key(tmp_path):
    """키 부재는 대체값 없이 실패 — 폴백은 호출자 책임(_build_provisional이 흡수, 리뷰 M3)."""
    cfg = tmp_path / "params.yaml"
    cfg.write_text("limit_engine:\n  provisional_k: 5\n", encoding="utf-8")
    with pytest.raises((RuntimeError, KeyError)):
        resolve_pm_log_path(cfg)


# ── A 파서 호환 (핵심 회귀 방지 — 스펙 §5) ──────────────────────────

@pytest.mark.parametrize("legacy", ["2018-12-24"])
def test_roundtrip_with_lean85_parser(tmp_path, legacy):
    """★기입 산출물 → A `parse_pm_log` 라운드트립: 혼재 정렬 성공·여분 키 무시.

    legacy 날짜문자열(naive)과 신규 dict가 섞여도 aware/naive 비교 TypeError가 없어야 한다.
    """
    lean = pytest.importorskip("src.agent_a_mlops.lean85.lean85_pipeline",
                               reason="A 파이프라인(pandas/xgboost) 미설치 환경 skip")
    p = tmp_path / "pm_log.json"
    p.write_text(json.dumps([legacy]), encoding="utf-8")
    PmLogWriter(p).append(_payload())
    events = lean.parse_pm_log(_read(p))            # list 직접 파싱(캐시 우회)
    assert [t for _, t in events] == ["major", "major"]
    assert str(events[0][0].date()) == legacy       # 시간순 정렬(legacy 먼저)
    assert str(events[1][0]) == "2026-07-28 05:12:33"


def test_should_retrain_sees_new_pm(tmp_path):
    """기입 즉시 A `should_retrain`이 신규 요란 PM을 이벤트 트리거로 인식 (README §4)."""
    lean = pytest.importorskip("src.agent_a_mlops.lean85.lean85_pipeline",
                               reason="A 파이프라인(pandas/xgboost) 미설치 환경 skip")
    p = tmp_path / "pm_log.json"
    p.write_text(json.dumps([{"date": "2018-12-24", "type": "major"}]), encoding="utf-8")
    manifest = {"created_at_local": datetime.now().isoformat(), "pm_log_dates": ["2018-12-24"]}
    assert lean.should_retrain(manifest, _read(p))[0] is False      # 기입 전 — 트리거 없음
    PmLogWriter(p).append(_payload())
    need, reasons = lean.should_retrain(manifest, _read(p))
    assert need is True and "2026-07-28" in reasons[0]


def test_repeated_failure_is_logged_once(tmp_path, caplog):
    """poll 루프가 상시 재시도해도 동일 실패는 최초 1회만 ERROR (로그 도배 억제)."""
    p = tmp_path / "pm_log.json"
    p.write_text("{깨진 json", encoding="utf-8")
    w = PmLogWriter(p)
    with caplog.at_level("ERROR"):
        w.append(_payload())
        for _ in range(20):
            w.flush_pending(force=True)          # 쿨다운 무시해도 상세는 1회여야
    loads = [r for r in caplog.records if "원본 보존·기입 보류" in r.message]
    assert len(loads) == 1                          # 손상 상세는 최초 1회만
    assert len(w.pending) == 1                      # 유실 없이 계속 대기


def test_error_detail_returns_after_recovery(tmp_path, caplog):
    """한 번 성공(복구)하면 다음 실패는 다시 상세 로그 — 억제가 영구 침묵이 되지 않는다."""
    p = tmp_path / "pm_log.json"
    p.write_text("{깨진 json", encoding="utf-8")
    w = PmLogWriter(p)
    with caplog.at_level("ERROR"):
        w.append(_payload(pm_count=1))
        p.write_text("[]", encoding="utf-8")
        w.flush_pending(force=True)                 # 복구 → 성공
        p.write_text("{또 깨짐", encoding="utf-8")
        w.append(_payload(pm_count=2, ts="2026-08-01T05:00:00+00:00"))
    loads = [r for r in caplog.records if "원본 보존·기입 보류" in r.message]
    assert len(loads) == 2                          # 복구 후 재발은 다시 상세


# ── 2차 리뷰 M: 지속 쓰기 잠금 (읽기는 성공) ────────────────────────

class _FakeClock:
    """주입 시계 — 쿨다운 경과를 결정론적으로 재현 (컨슈머 clock 패턴)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def _lock_replace(monkeypatch):
    """os.replace만 지속 실패시키고 시도 횟수를 센다 (파일 읽기는 정상)."""
    import src.agent_b_spc.pm_log_writer as mod

    calls = []

    def _locked(src, dst):
        calls.append(1)
        raise PermissionError("다른 프로세스가 사용 중")

    monkeypatch.setattr(mod.os, "replace", _locked)
    return calls


def test_persistent_write_lock_logs_detail_once(tmp_path, monkeypatch, caplog):
    """★★2차 리뷰 M — 파일은 읽히고 **교체만** 계속 실패해도 상세 로그는 최초 1회.

    구현 이전엔 `_last_error` 리셋이 *읽기 성공* 시점이라, 매 flush마다 리셋 → 억제 무력화
    → poll마다 ERROR+traceback. 리셋을 **기입 성공** 시점으로 옮겨 해결.
    """
    p = tmp_path / "pm_log.json"
    p.write_text("[]", encoding="utf-8")         # ★읽기는 정상(손상 경로와 다름)
    clock = _FakeClock()
    w = PmLogWriter(p, retries=2, backoff_sec=0.0, cooldown_sec=30.0, clock=clock)
    _lock_replace(monkeypatch)
    with caplog.at_level("ERROR"):
        w.append(_payload())
        for _ in range(20):                      # poll 20회 상당
            clock.advance(31)                    # 쿨다운은 매번 만료시켜 시도는 실제로 발생
            w.flush_pending()
    details = [r for r in caplog.records if "쓰기·교체 실패" in r.message]
    assert len(details) == 1                     # 21회 시도 → 상세 1회
    assert len(w.pending) == 1                   # 유실 없음


def test_background_flush_is_single_attempt_without_backoff(tmp_path, monkeypatch):
    """★핫패스 보호 — 백그라운드 재시도는 **단발·무대기**(append 예산 5회·≈1초를 안 쓴다).

    지속 잠금에서 poll마다 1초를 물면 `fdc.raw` 소비가 초당 ~1건으로 주저앉는다(2차 리뷰 M).
    실제로 자면 테스트가 멈추므로 **주입 sleeper**로 대기를 계측만 한다(전역 `time.sleep`
    patch는 pytest 내부와 충돌).
    """
    p = tmp_path / "pm_log.json"
    p.write_text("[]", encoding="utf-8")
    clock, slept = _FakeClock(), []
    w = PmLogWriter(p, retries=5, backoff_sec=0.1, cooldown_sec=0.0,
                    clock=clock, sleep=slept.append)
    calls = _lock_replace(monkeypatch)

    w.append(_payload())                                  # append = 전력 예산
    assert len(calls) == 5 and len(slept) == 4            # 5회 시도 · 대기 4회
    assert sum(slept) == pytest.approx(1.0)               # poll 루프가 물면 안 되는 그 1초

    calls.clear()
    slept.clear()
    w.flush_pending()                                     # 백그라운드 = 단발·무대기
    assert len(calls) == 1 and slept == []


def test_flush_cooldown_skips_until_expiry(tmp_path, monkeypatch):
    """실패 후 쿨다운 동안 백그라운드 flush는 no-op — 지속 잠금이 파일시스템을 두드리지 않는다."""
    p = tmp_path / "pm_log.json"
    p.write_text("[]", encoding="utf-8")
    clock = _FakeClock()
    w = PmLogWriter(p, retries=1, backoff_sec=0.0, cooldown_sec=30.0, clock=clock)
    calls = _lock_replace(monkeypatch)
    w.append(_payload())
    calls.clear()
    for _ in range(10):
        w.flush_pending()                        # 쿨다운 중 — 시도 자체가 없어야
    assert calls == []
    clock.advance(31)
    w.flush_pending()
    assert len(calls) == 1                       # 만료 후 1회(단발)


def test_append_bypasses_cooldown(tmp_path, monkeypatch):
    """진짜 새 요란 PM(append)은 쿨다운을 무시한다 — 분기 1회 이벤트를 놓치면 안 됨."""
    import src.agent_b_spc.pm_log_writer as mod

    p = tmp_path / "pm_log.json"
    p.write_text("[]", encoding="utf-8")
    clock = _FakeClock()
    w = PmLogWriter(p, retries=1, backoff_sec=0.0, cooldown_sec=300.0, clock=clock)
    real = mod.os.replace
    _lock_replace(monkeypatch)
    assert w.append(_payload(pm_count=1, ts="2026-07-28T05:00:00+00:00")) is False
    monkeypatch.setattr(mod.os, "replace", real)              # 잠금 해제
    # 쿨다운(300s) 한복판이지만 append는 통과 → 보류분까지 함께 회수
    assert w.append(_payload(pm_count=2, ts="2026-07-29T05:00:00+00:00")) is True
    assert [e["pm_count"] for e in _read(p)] == [1, 2] and w.pending == []


def test_cooldown_cleared_after_success(tmp_path, monkeypatch):
    """기입 성공 시 쿨다운·억제 상태가 함께 풀린다(다음 실패는 다시 상세·즉시 재시도)."""
    import src.agent_b_spc.pm_log_writer as mod

    p = tmp_path / "pm_log.json"
    p.write_text("[]", encoding="utf-8")
    clock = _FakeClock()
    w = PmLogWriter(p, retries=1, backoff_sec=0.0, cooldown_sec=300.0, clock=clock)
    real = mod.os.replace
    _lock_replace(monkeypatch)
    w.append(_payload(pm_count=1))
    assert w._cooldown_until > clock() and w._last_error is not None
    monkeypatch.setattr(mod.os, "replace", real)
    w.flush_pending(force=True)
    assert w._cooldown_until == 0.0 and w._last_error is None
