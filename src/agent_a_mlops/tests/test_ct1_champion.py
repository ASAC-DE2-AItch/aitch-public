# -*- coding: utf-8 -*-
"""CT① champion 해석 회귀 테스트 (리뷰 B2 — 헌법 3-3 ① 우회로 차단).

핵심 회귀: stamp 는 `lean85_<YYYYmmdd_HHMMSS>_<tag>` 라 **사전순 = 시간순**이다.
challenger 를 만든 뒤에 champion 을 glob 으로 뽑으면 방금 만든 challenger 자신이
뽑히고, 게이트는 그걸 "champion 부재(최초 학습)"로 읽어 **champion–challenger 비교
없이 무조건 승격**한다. 실물 champion 이 디스크에 있는데도 그렇다.

실행:  python -m pytest src/agent_a_mlops/tests/test_ct1_champion.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LEAN85 = Path(__file__).resolve().parents[1] / "lean85"
sys.path.insert(0, str(_LEAN85))
import ct1_orchestrator as orch  # noqa: E402
import validate_lean85 as vl  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """`models/c65_predictor/v2_lean85/` 를 흉내낸 임시 스토어 + 포인터 격리."""
    root = tmp_path
    (root / "models" / "c65_predictor" / "v2_lean85").mkdir(parents=True)
    monkeypatch.setattr(orch, "REPO_ROOT", root)
    monkeypatch.setattr(vl, "REPO_ROOT", root)
    monkeypatch.setattr(orch, "CONTROL_DIR", root / "control" / "ct1")
    monkeypatch.setattr(orch, "CHAMPION_PTR", root / "control" / "ct1" / "champion.json")
    monkeypatch.setattr(orch, "model_store_glob",
                        lambda: "models/c65_predictor/*/lean85_*")
    monkeypatch.setattr(vl, "_model_glob", lambda: "models/c65_predictor/*/lean85_*")
    return root


def _mk(store: Path, name: str) -> Path:
    d = store / "models" / "c65_predictor" / "v2_lean85" / name
    d.mkdir(parents=True)
    return d


def test_champion_glob_would_pick_the_challenger(store):
    """전제 확인 — 제외 없이는 사전순 최신인 challenger 가 뽑힌다 (버그 재현)."""
    _mk(store, "lean85_20260720_163040_initial")
    chal = _mk(store, "lean85_20260804_020000_daily")
    assert orch.champion_dir() == chal          # ← 이것이 B2 의 정체


def test_champion_dir_excludes_challenger(store):
    """challenger 를 제외하면 실제 incumbent 가 뽑힌다."""
    inc = _mk(store, "lean85_20260720_163040_initial")
    chal = _mk(store, "lean85_20260804_020000_daily")
    assert orch.champion_dir(exclude=chal) == inc


def test_champion_pointer_pointing_at_challenger_is_ignored(store):
    """포인터가 challenger 자신을 가리키면 무시하고 다른 후보로 넘어간다."""
    inc = _mk(store, "lean85_20260720_163040_initial")
    chal = _mk(store, "lean85_20260804_020000_daily")
    ptr = store / "control" / "ct1" / "champion.json"
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text(json.dumps({"model_dir": str(chal)}), encoding="utf-8")
    assert orch.champion_dir(exclude=chal) == inc


def test_gate_resolve_champion_excludes_and_honours_none(store):
    """게이트 쪽 해석기도 challenger 를 배제하고, `none` 은 '비교 없음'을 의미한다."""
    inc = _mk(store, "lean85_20260720_163040_initial")
    chal = _mk(store, "lean85_20260804_020000_daily")
    assert vl.resolve_champion("auto", exclude=chal) == inc
    assert vl.resolve_champion("none", exclude=chal) is None
    assert vl.resolve_champion(str(chal), exclude=chal) is None   # 자기 자신 지정도 무효


def test_other_models_exist_distinguishes_bootstrap(store):
    """스토어가 비었는지로 '진짜 최초 학습'과 '해석 실패'를 가른다."""
    chal = _mk(store, "lean85_20260804_020000_daily")
    assert vl.other_models_exist(chal) is False          # 부트스트랩
    _mk(store, "lean85_20260720_163040_initial")
    assert vl.other_models_exist(chal) is True           # 사고 — SKIP 되어야 한다


# ── GC 보존 규칙 (리뷰 B3 — 되돌릴 수 없는 삭제) ──────────────────────────
def test_gc_never_deletes_pre_ct1_model(store):
    """게이트 이전에 만든 모델(= 현 운영 champion·벤치 근거)은 GC 대상이 아니다."""
    d = _mk(store, "lean85_20260720_163040_initial")
    (d / "manifest.json").write_text(json.dumps({"features_n": 85}), encoding="utf-8")
    m = orch._STAMP_RE.match(d.name)
    why = orch._gc_deletable(d, m)
    assert why and "고정 어휘 밖" in why


def test_gc_keeps_folder_without_gate_report(store):
    """게이트 리포트가 없는 폴더는 '미승격'이 아니라 **판정 이력 없음** — 보존."""
    d = _mk(store, "lean85_20260804_020000_daily")
    (d / "manifest.json").write_text(
        json.dumps({"ct1_window_meta": {"window_days": 365}}), encoding="utf-8")
    why = orch._gc_deletable(d, orch._STAMP_RE.match(d.name))
    assert why and "리포트 부재" in why


def test_gc_keeps_promoted_lineage(store):
    """게이트 PASS(승격 계보)는 영구 보존."""
    d = _mk(store, "lean85_20260804_020000_daily")
    (d / "manifest.json").write_text(
        json.dumps({"ct1_window_meta": {"window_days": 365}}), encoding="utf-8")
    (d / "validation.json").write_text(json.dumps({"gate_verdict": "PASS"}), encoding="utf-8")
    why = orch._gc_deletable(d, orch._STAMP_RE.match(d.name))
    assert why and "PASS" in why


def test_gc_targets_only_own_unpromoted_output(store):
    """CT① 이 만들었고 게이트에서 떨어진 challenger 만 삭제 대상이다."""
    d = _mk(store, "lean85_20260804_020000_daily")
    (d / "manifest.json").write_text(
        json.dumps({"ct1_window_meta": {"window_days": 365}}), encoding="utf-8")
    (d / "validation.json").write_text(json.dumps({"gate_verdict": "SKIP"}), encoding="utf-8")
    assert orch._gc_deletable(d, orch._STAMP_RE.match(d.name)) is None


# ── 배포 기본값 (리뷰 B4) ─────────────────────────────────────────────────
def test_auto_promote_defaults_to_locked():
    """params.yaml 로드 실패(`{}`)면 자동 배포는 **잠긴다** — `_params()` 계약과 정합."""
    assert bool({}.get("ct1_auto_promote", False)) is False


# ── 일간 스케줄 — 기준 07:00 KST = naive UTC 22:00 (설계 §3-1, 2026-08-05) ──
def _sched(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "STATE_PATH", tmp_path / "ct1_state.json")


def test_due_at_2200_utc_equals_kst_0700(tmp_path, monkeypatch):
    """22:00 UTC(= 익일 07:00 KST) 도래 전 미발동, 도래 시 발동."""
    from datetime import datetime
    _sched(tmp_path, monkeypatch)
    assert orch.due_scheduled({}, datetime(2026, 8, 5, 21, 59)) is False
    assert orch.due_scheduled({}, datetime(2026, 8, 5, 22, 0)) is True


def test_one_run_per_utc_day(tmp_path, monkeypatch):
    """같은 (UTC)날 두 번 발동하지 않는다 — 불변식 'UTC 날짜당 정기 1회'."""
    from datetime import datetime
    _sched(tmp_path, monkeypatch)
    orch._mark_state(last_scheduled_day="20260805")
    assert orch.due_scheduled({}, datetime(2026, 8, 5, 23, 30)) is False
    # 다음 날 22:00 전 — 정상 대기 (밀린 게 아니라 아직 안 온 것)
    assert orch.due_scheduled({}, datetime(2026, 8, 6, 7, 0)) is False
    assert orch.due_scheduled({}, datetime(2026, 8, 6, 22, 5)) is True


def test_catchup_when_overdue_more_than_a_day(tmp_path, monkeypatch):
    """24h 초과 밀림(≤ 전전일)이면 시각 무관 즉시 캐치업 (D1 — §3-1 ⓑ).

    이 분기가 없으면 아침에 죽었다 낮에 살아난 프로세스가 다음 22:00 까지
    ~19시간을 그냥 기다린다 — 밀린 하루가 이틀이 된다.
    """
    from datetime import datetime
    _sched(tmp_path, monkeypatch)
    orch._mark_state(last_scheduled_day="20260803")
    assert orch.due_scheduled({}, datetime(2026, 8, 5, 7, 0)) is True


# ── F2: 실행 후 하우스키핑이 '오늘'을 삼키지 않는다 ────────────────────────
@pytest.fixture(autouse=True)
def _isolate_state_mem():
    """`_mark_state` 의 메모리 미러(F2b)를 테스트마다 격리한다.

    단언이 중간에 실패하면 수동 `clear()` 에 도달하지 못해 전역이 오염된 채
    `_state()` 를 읽는 다른 테스트로 전파된다 — 원인 추적이 어려운 연쇄 실패다.
    """
    orch._STATE_MEM.clear()
    yield
    orch._STATE_MEM.clear()


@pytest.mark.parametrize("victim", ["_update_champion_cap_streak", "_gc_unpromoted",
                                    "_warn_stale_champion"])
def test_housekeeping_failure_does_not_consume_the_day(tmp_path, monkeypatch, victim):
    """★회귀 — 종결 기록이 끝난 **뒤**의 위생 작업 실패는 실행 밖으로 새면 안 된다.

    새면 main 루프의 `_mark_state(last_scheduled_day=…)` 에 도달하지 못해 60초 뒤
    같은 날 정기가 통째로 재실행된다 (조립+재학습 최대 1h + 게이트를 디스크 오류
    하나로 반복). 본 실행은 이미 `finalize_decision` 으로 종결 기록됐다.

    3종 **전부**를 파라미터로 도는 이유: 첫 호출(`_update_champion_cap_streak` — 상태
    파일을 쓰므로 OSError 원천이 바로 여기다)만 다시 try 밖으로 새는 회귀는 한 지점만
    찌르는 테스트가 못 잡는다.
    """
    from datetime import datetime
    _sched(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "CONTROL_DIR", tmp_path / "control" / "ct1")
    monkeypatch.setattr(orch, "CHAMPION_PTR", tmp_path / "control" / "ct1" / "champion.json")
    monkeypatch.setattr(orch, "_db", lambda: None)
    monkeypatch.setattr(orch, "run_cycle", lambda p, **kw: ("SKIP", "게이트 SKIP", {}))
    monkeypatch.setattr(orch, "publish_mlops", lambda payload: None)
    monkeypatch.setattr(orch, "acquire_lock", lambda: True)
    monkeypatch.setattr(orch, "release_lock", lambda: None)

    def _boom(*a, **kw):
        raise OSError(f"디스크 오류 — {victim} 실패")

    monkeypatch.setattr(orch, victim, _boom)

    p = {"ct1_auto_generate": True}
    assert orch.handle_trigger(p, now=datetime(2026, 8, 5, 22, 0),
                               trigger="daily_sliding", tag="daily") == "SKIP"


def test_mark_state_survives_write_failure(tmp_path, monkeypatch):
    """★회귀 (F2b) — 상태 파일 쓰기가 실패해도 **프로세스 안에서는 일관**해야 한다.

    실패를 삼키고 넘어가면 `last_scheduled_day` 가 기록되지 않아 같은 날 재학습이
    반복된다. 파일이 정본이되, 미persist 델타는 메모리 미러가 받는다.
    """
    from datetime import datetime
    _sched(tmp_path, monkeypatch)

    real_write = orch._atomic_write_json

    def _boom(obj, path):
        raise OSError("디스크 가득 참")

    monkeypatch.setattr(orch, "_atomic_write_json", _boom)
    orch._mark_state(last_scheduled_day="20260805")
    # 파일은 못 썼지만 같은 날 재실행은 막힌다
    assert orch.due_scheduled({}, datetime(2026, 8, 5, 23, 0)) is False

    monkeypatch.setattr(orch, "_atomic_write_json", real_write)   # 쓰기 복구
    orch._mark_state(last_scheduled_day="20260806")
    assert not orch._STATE_MEM, "쓰기 성공 시 메모리 미러는 비워진다 (테스트 간 누수 차단)"
    assert orch._state().get("last_scheduled_day") == "20260806", "파일에 반영돼야 한다"


# ── F3: 신호 드롭 이후의 실패는 배포를 번복하지 않는다 ─────────────────────
def _promote_world(tmp_path, monkeypatch):
    """`_do_promote` 를 태울 수 있는 최소 세계 — 신호·포인터·CHANGELOG 경로 격리."""
    monkeypatch.setattr(orch, "CONTROL_DIR", tmp_path / "control" / "ct1")
    monkeypatch.setattr(orch, "CHAMPION_PTR", tmp_path / "control" / "ct1" / "champion.json")
    monkeypatch.setattr(orch, "append_changelog", lambda *a, **kw: None)
    chal = tmp_path / "models" / "lean85_20260805_220000_daily"
    chal.mkdir(parents=True)
    return chal


def test_do_promote_survives_pointer_failure(tmp_path, monkeypatch, caplog):
    """★회귀 — 신호가 드롭된 순간부터 '배포됨'은 사실이다.

    그 뒤의 포인터 갱신 실패를 FAILED 로 기록하면 **서빙은 신모델, 감사는 실패**인
    역방향 불일치(설계 §4-8 의 반대 극성)가 된다. 상태는 AUTO_PROMOTED 로 유지하고
    실패는 detail·★로그로 드러낸다.
    """
    import logging
    chal = _promote_world(tmp_path, monkeypatch)

    def _boom(*a, **kw):
        raise OSError("포인터 쓰기 실패")

    monkeypatch.setattr(orch, "set_champion", _boom)
    report: dict = {}
    with caplog.at_level(logging.ERROR):
        status, detail, _ = orch._do_promote("CT-20260805-ALL-001-XGB", chal, None,
                                             report, {"rmse_after": 99.0})

    assert status == "AUTO_PROMOTED"
    assert "★포인터 갱신 실패" in detail
    assert list((tmp_path / "control" / "ct1").glob("promote_*.json")), "신호는 실존한다"
    assert any("수동 복구" in r.getMessage() for r in caplog.records)
    assert report.get(orch.PROMOTE_WARN_KEY) == ["ptr_fail"], "감사 축으로 올라가야 한다"


def test_pointer_failure_lands_in_audit_reason(tmp_path, monkeypatch):
    """★회귀 — 포인터 실패가 `ct_decisions.trigger_reason` 에 남는다.

    detail 은 DB 에도 `fdc.mlops` 에도 실리지 않아 로그가 유일한 증거였다. 그러면
    "서빙=신모델 / 포인터=구모델"인 건이 감사 테이블상 **완전 정상**으로 보이고,
    다음 게이트는 낡은 포인터로 서빙 중이 아닌 모델과 비교한다 (헌법 3-3 ①).
    """
    from datetime import datetime
    _sched(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "CONTROL_DIR", tmp_path / "control" / "ct1")
    monkeypatch.setattr(orch, "CHAMPION_PTR", tmp_path / "control" / "ct1" / "champion.json")
    monkeypatch.setattr(orch, "_db", lambda: None)
    monkeypatch.setattr(orch, "publish_mlops", lambda payload: None)
    monkeypatch.setattr(orch, "acquire_lock", lambda: True)
    monkeypatch.setattr(orch, "release_lock", lambda: None)
    monkeypatch.setattr(orch, "_update_champion_cap_streak", lambda *a, **kw: 0)
    monkeypatch.setattr(orch, "_gc_unpromoted", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_warn_stale_champion", lambda *a, **kw: None)

    monkeypatch.setattr(orch, "run_cycle", lambda p, **kw: (
        "AUTO_PROMOTED", "자동 배포 · ★포인터 갱신 실패(OSError) — 수동 복구 필요",
        {"bundle_name": "lean85_20260805_220000_daily",
         orch.PROMOTE_WARN_KEY: ["ptr_fail"]}))

    seen: dict = {}

    def _capture(conn, ct_id, status, **kw):
        seen.update(kw, status=status)
        return True

    monkeypatch.setattr(orch, "finalize_decision", _capture)

    p = {"ct1_auto_generate": True}
    assert orch.handle_trigger(p, now=datetime(2026, 8, 5, 22, 0),
                               trigger="daily_sliding", tag="daily") == "AUTO_PROMOTED"
    assert seen["reason"] == "daily_sliding/auto_promoted/ptr_fail"
    assert len(seen["reason"]) <= orch.TRIGGER_REASON_MAX, "감사 컬럼 절단 금지"
    assert seen["after"] == "lean85_20260805_220000_daily", "배포는 사실이다 (S12)"


def test_do_promote_signal_failure_propagates(tmp_path, monkeypatch):
    """신호 드롭 **자체**의 실패는 전파된다 — 아무것도 배포되지 않았으므로 FAILED 가 맞다."""
    chal = _promote_world(tmp_path, monkeypatch)

    def _boom(*a, **kw):
        raise OSError("신호 드롭 실패")

    monkeypatch.setattr(orch, "drop_promote_signal", _boom)
    with pytest.raises(OSError):
        orch._do_promote("CT-20260805-ALL-001-XGB", chal, None, {}, {})


# ── F6: 60초 폴링 경로에서 booster 를 로드하지 않는다 ──────────────────────
def test_due_event_does_not_load_booster(tmp_path, monkeypatch):
    """★회귀 (현행 코드에서 실패해야 한다) — 이벤트 판정에 필요한 것은 manifest 뿐이다.

    부수 효과: 모델 파일이 손상돼도 이벤트 판정은 살아 있다.
    """
    import lean85_pipeline as lp
    import pandas as pd
    _sched(tmp_path, monkeypatch)
    champ = tmp_path / "lean85_20260720_163040_initial"
    champ.mkdir()
    (champ / "manifest.json").write_text(
        json.dumps({"pm_log_dates": ["2026-07-20"]}), encoding="utf-8")

    def _boom(*a, **kw):
        raise RuntimeError("booster 로드 — 폴링 경로에서 불려서는 안 된다")

    monkeypatch.setattr(orch, "champion_dir", lambda exclude=None: champ)
    monkeypatch.setattr(lp, "load_model", _boom)
    monkeypatch.setattr(lp, "find_file", lambda name: tmp_path / "pm_log.json")
    monkeypatch.setattr(lp, "parse_pm_log",
                        lambda p: [(pd.Timestamp("2026-08-05"), "major")])
    monkeypatch.delenv("LEAN85_PM_LOG", raising=False)

    hit, new_pm = orch.due_event({})
    assert hit is True and new_pm == ["2026-08-05"]
