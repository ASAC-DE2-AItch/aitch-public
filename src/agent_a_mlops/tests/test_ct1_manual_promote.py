# -*- coding: utf-8 -*-
"""CT① 수동 promote·롤백 CLI + RUNNING 고아 리퍼 회귀 (리뷰 F4·F7 — PR-2).

두 장치의 공통 주제는 **"사람이 잊는 후속 작업"의 코드화**다.

  F4 — 설계 §4-5 의 수동 경로는 "사람이 신호를 드롭한다"인데, champion 포인터 갱신
       주체가 코드에 없었다. 신호만 드롭하고 포인터를 방치하면 다음 게이트가 **서빙
       중이 아닌 모델**과 비교한다 (헌법 3-3 ①). 자동 롤백을 폐지한 v2(개정 ③)에서
       수동 롤백은 **유일한 복구 수단**이라, 그 경로의 SOP 공백은 그대로 사고 경로다.
  F7 — claim 불변식("선점 이후 어떤 실패든 종결 상태를 남긴다")은 프로세스가 살아
       있을 때만 성립한다. 락 파일에는 유령 회수가 있는데 DB 행에는 없어서, finalize
       전 크래시가 영구 RUNNING 을 남기고 감사 테이블에서 '진행 중'과 '죽음'이
       구분 불가해졌다.

DB 는 fake conn 으로 태운다 — 기록 경로를 monkeypatch 로 잘라내면 이 테스트들이
검증하려는 것(**신호·포인터·기록이 한 묶음으로 간다**)이 통째로 빠진다.

실행:  python -m pytest src/agent_a_mlops/tests/test_ct1_manual_promote.py -q
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_LEAN85 = Path(__file__).resolve().parents[1] / "lean85"
sys.path.insert(0, str(_LEAN85))
import ct1_orchestrator as orch  # noqa: E402


# ── fake DB ───────────────────────────────────────────────────────────────
class _FakeCursor:
    """psycopg2 커서 최소 흉내 — 실행된 SQL·파라미터를 기록한다 (계약 검증용)."""

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.conn.execute_raises:
            raise RuntimeError("DB 오류(주입)")
        self.conn.sql.append((" ".join(sql.split()), params))
        head = sql.strip().upper()
        if head.startswith("SELECT CT_ID"):              # next_ct_id — 당일 미사용
            self._rows, self.rowcount = [], 0
        elif "RETURNING" in head:                        # claim_running — 선점 승패
            self._rows = [("claimed",)] if self.conn.claim_wins else []
            self.rowcount = len(self._rows)
        else:                                            # UPDATE (finalize·reap)
            self._rows, self.rowcount = [], self.conn.update_rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    def __init__(self, *, claim_wins=True, update_rowcount=1, execute_raises=False):
        self.sql: list = []
        self.claim_wins = claim_wins
        self.update_rowcount = update_rowcount
        self.execute_raises = execute_raises
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def find(self, needle: str):
        """실행된 SQL 중 `needle` 을 포함하는 첫 (sql, params)."""
        return next((row for row in self.sql if needle in row[0]), None)


@pytest.fixture()
def published(monkeypatch):
    """`fdc.mlops` 발행 캡처 — 패치하지 않으면 실 Producer 가 브로커를 5초 기다린다."""
    out: list = []
    monkeypatch.setattr(orch, "publish_mlops", out.append)
    return out


@pytest.fixture()
def world(tmp_path, monkeypatch, published):
    """신호·포인터·CHANGELOG·발행을 전부 tmp 로 격리한 최소 세계."""
    (tmp_path / "models" / "c65_predictor" / "v2_lean85").mkdir(parents=True)
    monkeypatch.setattr(orch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(orch, "CONTROL_DIR", tmp_path / "control" / "ct1")
    monkeypatch.setattr(orch, "CHAMPION_PTR", tmp_path / "control" / "ct1" / "champion.json")
    monkeypatch.setattr(orch, "LOCK_PATH", tmp_path / "control" / "ct1" / "ct1.lock")
    monkeypatch.setattr(orch, "model_store_glob", lambda: "models/c65_predictor/*/lean85_*")
    monkeypatch.delenv("LEAN85_SIM_NOW", raising=False)
    return tmp_path


def _model(root: Path, name: str, *, verdict: str | None = "PASS",
           bundle_name: str | None = None) -> Path:
    """manifest + 게이트 리포트를 갖춘 stamp 폴더 (수동 경로가 요구하는 최소 실체).

    기본이 PASS 인 이유: 수동 승격의 **정상** 대상은 게이트를 통과한 번들이다. 여기서
    리포트를 빼면 모든 승격 테스트가 "게이트 미검증 폴더를 밀어넣는" 비정상 시나리오가
    되어, 정작 검증하려는 묶음(신호·포인터·기록)을 못 본다.

    Args:
        verdict: `gate_verdict` 값. `None` 이면 리포트 파일 자체를 만들지 않는다.
        bundle_name: 리포트의 `bundle_name`. 기본은 폴더명(정상). 다른 값을 주면
            "다른 번들의 PASS 리포트를 복사해 넣은" 우회 시나리오가 된다.
    """
    d = root / "models" / "c65_predictor" / "v2_lean85" / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"features_n": 85}), encoding="utf-8")
    if verdict is not None:
        (d / orch.VALIDATION_REPORT_NAME).write_text(json.dumps({
            "gate_verdict": verdict,
            "gate_pass": verdict == "PASS",
            "bundle_name": bundle_name or name,
            "failed_checks": [] if verdict == "PASS" else ["rmse_regression"],
        }), encoding="utf-8")
    return d


def _signals(root: Path) -> list[Path]:
    return sorted((root / "control" / "ct1").glob("promote_*.json"))


def _set_ptr(root: Path, current: Path, previous: Path | None = None) -> None:
    p = root / "control" / "ct1" / "champion.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "model_dir": str(current.resolve()),
        "model_version": current.name,
        "previous_model_dir": str(previous.resolve()) if previous else None,
    }), encoding="utf-8")


# ── F4: 수동 승격 ─────────────────────────────────────────────────────────
def test_manual_promote_drops_signal_and_updates_pointer(world, published, monkeypatch):
    """★핵심 — 신호·포인터·감사기록·CHANGELOG·발행이 **한 묶음**으로 나간다.

    이 묶음이 코드에 없어서(F4) 사람이 신호만 드롭하고 포인터를 두고 가면, 다음 게이트가
    서빙 중이 아닌 모델과 비교한다 (헌법 3-3 ①).
    """
    incumbent = _model(world, "lean85_20260720_163040_initial")
    target = _model(world, "lean85_20260806_220000_daily")
    _set_ptr(world, incumbent)
    conn = _FakeConn()
    monkeypatch.setattr(orch, "_db", lambda: conn)

    assert orch.manual_promote(str(target), rollback=False) == 0

    sigs = _signals(world)
    assert len(sigs) == 1, "신호 1건이 드롭돼야 한다"
    payload = json.loads(sigs[0].read_text(encoding="utf-8"))
    assert payload["model_dir"] == str(target.resolve())
    assert payload["rollback"] is False
    assert payload["promoted_at"], "정렬 키(F1·F9)가 읽는 필드 — 누락 금지"

    ptr = json.loads((world / "control" / "ct1" / "champion.json").read_text(encoding="utf-8"))
    assert ptr["model_dir"] == str(target.resolve()), "포인터가 대상으로 갱신"
    assert ptr["previous_model_dir"] == str(incumbent.resolve()), "롤백 대상 보존 (D8)"

    upd = conn.find("SET retrain_status=%s")
    assert upd and upd[1][0] == "PROMOTED", "감사 상태는 PROMOTED (D8 GC 보존 쿼리 인지값)"
    assert upd[1][2].startswith("manual/promoted/"), f"사유에 수동 경로 표기: {upd[1][2]}"
    assert len(upd[1][2]) <= orch.TRIGGER_REASON_MAX, "감사 컬럼 절단 금지"
    assert upd[1][1] == target.name, "model_version_after = 지금 서빙될 모델 (S12)"

    changelog = (world / "models" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert target.name in changelog and "수동 신호" in changelog
    assert published and published[0]["retrain_status"] == "PROMOTED"
    assert published[0]["model_version_before"] == incumbent.name
    assert conn.closed, "커넥션은 반드시 닫힌다"


def test_rollback_targets_previous_champion(world, published, monkeypatch):
    """★핵심 — `--rollback` 은 포인터의 `previous_model_dir`(D8 보존분)로 되돌린다.

    payload `rollback=true` 는 장식이 아니다 — consumer 의 동률 타이브레이크(F9)가
    이 플래그로 롤백을 이기게 한다.
    """
    good = _model(world, "lean85_20260720_163040_initial")
    bad = _model(world, "lean85_20260806_220000_daily")
    _set_ptr(world, bad, previous=good)
    conn = _FakeConn()
    monkeypatch.setattr(orch, "_db", lambda: conn)

    assert orch.manual_promote(None, rollback=True) == 0

    payload = json.loads(_signals(world)[0].read_text(encoding="utf-8"))
    assert payload["model_dir"] == str(good.resolve()), "직전 champion 으로 되돌아간다"
    assert payload["rollback"] is True

    ptr = json.loads((world / "control" / "ct1" / "champion.json").read_text(encoding="utf-8"))
    assert ptr["model_dir"] == str(good.resolve())
    assert ptr["previous_model_dir"] == str(bad.resolve()), "물러난 모델이 previous 가 된다"

    upd = conn.find("SET retrain_status=%s")
    assert upd[1][0] == "ROLLED_BACK", "설계 §4-5 가 요구하는 감사 상태"
    assert published[0]["retrain_status"] == "ROLLED_BACK"


def test_rollback_without_previous_refuses(world, monkeypatch):
    """롤백 대상이 없으면 **아무것도 하지 않는다** — 부트스트랩 상태에서의 오작동 차단."""
    only = _model(world, "lean85_20260720_163040_initial")
    _set_ptr(world, only)                                  # previous_model_dir = None
    monkeypatch.setattr(orch, "_db", lambda: _FakeConn())

    assert orch.manual_promote(None, rollback=True) == 1
    assert not _signals(world), "신호 미생성"


def test_manual_promote_refuses_without_db(world, monkeypatch, caplog):
    """★회귀 — DB 접속 불가면 신호를 드롭하지 않는다 (헌법 7장 — 무기록 유실 금지).

    기록 없이 교체하면 그 교체는 감사 테이블에서 영영 보이지 않는다. "일단 배포하고
    기록은 나중에"가 정확히 헌법 7장이 등재한 실패 형태다.
    """
    target = _model(world, "lean85_20260806_220000_daily")

    def _boom():
        raise RuntimeError("DATABASE_URL 미설정")

    monkeypatch.setattr(orch, "_db", _boom)
    with caplog.at_level(logging.ERROR):
        rc = orch.manual_promote(str(target), rollback=False)

    assert rc == 1
    assert not _signals(world), "★신호 파일 미생성 — 기록 없는 교체 금지"
    assert not (world / "control" / "ct1" / "champion.json").exists()
    assert any("기록 없는 교체 금지" in r.getMessage() for r in caplog.records)


def test_manual_promote_rejects_folder_without_manifest(world, monkeypatch):
    """대상 실체 확인 — consumer 는 신호를 재검하지 않는다 (설계 §4-5 개정 ④).

    오타 하나가 서빙 모델 로드 실패로 직결되므로 발행 **전에** 막는다.
    """
    empty = world / "models" / "c65_predictor" / "v2_lean85" / "lean85_20260806_220000_daily"
    empty.mkdir(parents=True)                              # manifest.json 없음
    conn = _FakeConn()
    monkeypatch.setattr(orch, "_db", lambda: conn)

    assert orch.manual_promote(str(empty), rollback=False) == 1
    assert not _signals(world)
    assert not conn.sql, "선점조차 하지 않는다 (감사 축 오염 방지)"


def test_manual_promote_rejects_gate_failed_bundle(world, monkeypatch):
    """★회귀 (PM 리뷰 #123) — **게이트 FAIL 번들은 수동 경로로도 배포되지 않는다.**

    헌법 3-3 ① 은 "평가 통과 전 교체 금지"인데, 수동 경로는 `manifest.json` 존재만 보고
    신호를 드롭했다 — `gate_verdict=FAIL`(BLOCKED) stamp 를 지정하면 그대로 배포됐다.
    하필 이 경로가 `ct1_auto_promote: false`(차단기 강하) 구간의 유일한 승격 수단이라
    **사람이 stamp 를 손으로 타이핑하는 자리**이고, 그래서 실수가 나는 자리다.

    종료 코드가 아니라 예외인 것도 계약이다 — 스크립트의 `|| true` 한 줄에 삼켜지면
    가드가 없는 것과 같다 (헌법 7장).
    """
    target = _model(world, "lean85_20260806_220000_daily", verdict="FAIL")
    conn = _FakeConn()
    monkeypatch.setattr(orch, "_db", lambda: conn)

    with pytest.raises(ValueError, match="게이트 판정"):
        orch.manual_promote(str(target), rollback=False)

    assert not _signals(world), "★신호 미생성 — 배포되지 않았다"
    assert not (world / "control" / "ct1" / "champion.json").exists(), "포인터 무변화"
    assert not conn.sql, "선점조차 하지 않는다 (감사 축 오염 방지 — manifest 가드와 같은 극성)"
    assert conn.closed, "커넥션은 반드시 닫힌다 (예외 경로에서도)"


@pytest.mark.parametrize("kwargs, why", [
    ({"verdict": "SKIP"}, "SKIP=미검증 항목 존재 — PASS 아님"),
    ({"verdict": None}, "리포트 부재 — PASS 를 주장할 근거 없음"),
    ({"bundle_name": "lean85_20260101_000000_other"}, "다른 번들의 PASS 리포트 복사"),
])
def test_manual_promote_rejects_non_pass_variants(world, monkeypatch, kwargs, why):
    """FAIL 외 나머지 거부 갈래 — 가드의 세 분기를 전부 덮는다.

    FAIL 만 막고 SKIP·리포트 부재·`bundle_name` 불일치가 뚫리면 우회로가 남는다.
    """
    target = _model(world, "lean85_20260806_220000_daily", **kwargs)
    conn = _FakeConn()
    monkeypatch.setattr(orch, "_db", lambda: conn)

    with pytest.raises(ValueError):
        orch.manual_promote(str(target), rollback=False)

    assert not _signals(world), f"신호 미생성 — {why}"
    assert not conn.sql, "선점 없음"


def test_rollback_is_exempt_from_gate_check(world, published, monkeypatch):
    """★설계 — **롤백은 게이트 가드를 타지 않는다.**

    롤백 대상은 *이미 champion 이었던* 모델이고, 자동 롤백을 폐지한 v2(개정 ③)에서 수동
    롤백은 **유일한 복구 수단**이다. 여기에 PASS 를 요구하면 게이트 도입 이전에 승격된
    champion 으로는 되돌아갈 수 없어, 사고 대응 수단이 사고를 막는 가드에 막힌다.
    """
    good = _model(world, "lean85_20260720_163040_initial", verdict=None)   # 리포트 없는 구 champion
    bad = _model(world, "lean85_20260806_220000_daily")
    _set_ptr(world, bad, previous=good)
    conn = _FakeConn()
    monkeypatch.setattr(orch, "_db", lambda: conn)

    assert orch.manual_promote(None, rollback=True) == 0, "게이트 리포트가 없어도 롤백은 된다"
    assert json.loads(_signals(world)[0].read_text(encoding="utf-8"))["rollback"] is True


def test_pointer_failure_is_reported_but_deployment_stands(world, monkeypatch, caplog):
    """포인터 갱신 실패는 **배포를 번복하지 않는다** (F3 와 같은 극성).

    신호가 드롭된 순간부터 '배포됨'은 사실이다. 그래서 0(완전 성공)도 1(미실행)도
    아닌 제3의 종료 코드로 후속 복구를 지시한다.
    """
    target = _model(world, "lean85_20260806_220000_daily")
    conn = _FakeConn()
    monkeypatch.setattr(orch, "_db", lambda: conn)

    def _boom(*a, **kw):
        raise OSError("포인터 쓰기 실패")

    monkeypatch.setattr(orch, "set_champion", _boom)
    with caplog.at_level(logging.ERROR):
        rc = orch.manual_promote(str(target), rollback=False)

    assert rc == orch.EXIT_MANUAL_PARTIAL
    assert _signals(world), "신호는 실존한다 — 배포는 사실이다"
    assert conn.find("SET retrain_status=%s")[1][2].endswith("/ptr_fail"), \
        "감사 축에서 `trigger_reason LIKE '%ptr_fail%'` 로 뽑을 수 있어야 한다"
    assert any("수동 복구" in r.getMessage() for r in caplog.records)


def test_cli_rejects_conflicting_flags(world):
    """조용히 한쪽을 무시하면 "롤백한 줄 알았는데 재학습이 돌았다"가 된다."""
    for argv in (["--manual-promote", "x", "--rollback"], ["--once", "--rollback"]):
        with pytest.raises(SystemExit):
            orch.main(argv)


# ── F7: RUNNING 고아 리퍼 ─────────────────────────────────────────────────
def test_reap_scopes_ct1_and_running_only():
    """★계약 — 리퍼는 **CT① 의 RUNNING 행 중 임계 경과분**만 건드린다.

    범위가 새면 ⓐ CT²(retrain+validate 로 최대 2.5h 블록 — WP-B1)의 정상 실행을
    죽이거나 ⓑ 이미 종결된 행의 사유를 덮어쓴다. 둘 다 감사 파괴다.
    """
    conn = _FakeConn(update_rowcount=2)
    assert orch.reap_stale_running(conn) == 2

    sql, params = conn.find("UPDATE ct_decisions")
    assert "retrain_status='FAILED'" in sql
    assert "ct_type=%s" in sql and "retrain_status='RUNNING'" in sql
    assert "created_at < NOW() - make_interval" in sql, "시간 임계 없이 전량 종결 금지"
    assert "COALESCE(trigger_reason, '')" in sql, \
        "NULL || 'x' = NULL — COALESCE 없으면 사유가 통째로 날아간다"
    assert params == (orch.TRIGGER_REASON_MAX, orch.CT_TYPE, orch.STALE_RUNNING_SEC)
    assert orch.CT_TYPE == "ct1_xgb" and orch.STALE_RUNNING_SEC == orch.LOCK_STALE_SEC
    assert conn.commits == 1


def test_reap_failure_does_not_break_the_run():
    """위생 작업이지 경로가 아니다 — 실패해도 0 을 돌려주고 트랜잭션을 되감는다."""
    conn = _FakeConn(execute_raises=True)
    assert orch.reap_stale_running(conn) == 0
    assert conn.rollbacks == 1, "오염된 트랜잭션을 안 되감으면 이후 기록이 전부 실패한다"
    assert orch.reap_stale_running(None) == 0               # DB 부재 경로


def test_handle_trigger_reaps_before_claiming(world, monkeypatch):
    """트리거당 1회 호출 — 선점 **전**에 지난 실행의 잔해를 종결시킨다."""
    from datetime import datetime

    conn = _FakeConn()
    calls: list = []
    monkeypatch.setattr(orch, "_db", lambda: conn)
    monkeypatch.setattr(orch, "reap_stale_running", lambda c: calls.append(c) or 0)
    monkeypatch.setattr(orch, "record_decision", lambda *a, **kw: True)

    # g1(dry-run) 에서 즉시 반환하는 최단 경로로도 리퍼는 이미 돌았어야 한다.
    assert orch.handle_trigger({}, now=datetime(2026, 8, 6, 22, 0),
                               trigger="daily_sliding", tag="daily") == "DRYRUN"
    assert calls == [conn], "리퍼가 트리거당 1회 불린다"
