# -*- coding: utf-8 -*-
"""ct2_reconciler 판정표 5행 + 재드롭 상한·안전 불변식 (A14).

핵심 불변식: 쓰기 조치는 「무흔적 → 재드롭」 하나뿐이고, 그 외 어떤 경로도
신호 파일을 만들거나 옮기거나 지우지 않는다.
"""

import json
import os
import time

import pytest

from src.orchestrator import ct2_deploy_approval as cda
from src.orchestrator import ct2_reconciler as rec


# ── 재료 ─────────────────────────────────────────────────────────────────────

class _FakeCursor:
    """SQL 라우팅 페이크 — approval_records 목록과 ct_decisions 상태맵을 스크립트."""

    def __init__(self, approved_rows, ct_status):
        self._approved = approved_rows            # [(incident_id, bundle), ...]
        self._ct = ct_status                      # {ct_id: retrain_status}
        self._last = None

    def execute(self, sql, params=None):
        if "FROM approval_records" in sql:
            self._last = list(self._approved)
        elif "FROM ct_decisions" in sql:
            st = self._ct.get(params[0])
            self._last = None if st is None else (st,)
        else:                                     # pragma: no cover
            raise AssertionError(f"예상 밖 SQL: {sql}")

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, approved_rows, ct_status):
        self._rows, self._ct = approved_rows, ct_status

    def cursor(self):
        return _FakeCursor(self._rows, self._ct)


@pytest.fixture()
def promote_dir(tmp_path, monkeypatch):
    """PROMOTE_DIR 를 임시 디렉터리로 — 신호 3곳(원본/processed/failed) 스캐폴드."""
    d = tmp_path / "control" / "ct2"
    (d / "processed").mkdir(parents=True)
    (d / "failed").mkdir()
    monkeypatch.setattr(cda, "PROMOTE_DIR", d)
    rec._ALREADY_LOGGED.clear()                   # 프로세스 수명 dedup 초기화
    return d


@pytest.fixture()
def bundle(tmp_path):
    b = tmp_path / "bundles" / "ae_v9"
    b.mkdir(parents=True)
    return str(b)


def _sig_name(inc):
    return f"promote_{inc}.json"


# ── decide() — 판정표 5행 그대로 ─────────────────────────────────────────────

def test_decide_redrop_when_no_trace_and_promoted():
    assert rec.decide(retrain_status="PROMOTED", where="none", stale_sec=None,
                      bundle_exists=True) == "redrop"


def test_decide_redrop_allowed_when_ct_row_missing():
    """② 전에 죽은 경우(행 부재)도 재드롭 — ② 재수행은 수동(WARNING)."""
    assert rec.decide(retrain_status=None, where="none", stale_sec=None,
                      bundle_exists=True) == "redrop"


def test_decide_rollback_like_status_blocks_everything():
    """⑤ PROMOTED 계열이 아닌 명시 상태는 신호가 어디 있든 무동작."""
    for where in ("none", "original", "processed", "failed"):
        assert rec.decide(retrain_status="ROLLED_BACK", where=where, stale_sec=0.0,
                          bundle_exists=True) == "skip_status"


def test_decide_failed_escalates_never_redrops():
    assert rec.decide(retrain_status="AUTO_PROMOTED", where="failed", stale_sec=None,
                      bundle_exists=True) == "escalate_failed"


def test_decide_original_fresh_waits_stale_errors():
    fresh = rec.decide(retrain_status="PROMOTED", where="original", stale_sec=60.0,
                       bundle_exists=True, stale_min=10.0)
    stale = rec.decide(retrain_status="PROMOTED", where="original", stale_sec=601.0,
                       bundle_exists=True, stale_min=10.0)
    assert (fresh, stale) == ("waiting", "stale_error")


def test_decide_processed_ok_and_missing_bundle_skips():
    assert rec.decide(retrain_status="PROMOTED", where="processed", stale_sec=None,
                      bundle_exists=True) == "ok"
    assert rec.decide(retrain_status="PROMOTED", where="none", stale_sec=None,
                      bundle_exists=False) == "skip_no_bundle"


# ── reconcile_once() — 부작용 불변식 ─────────────────────────────────────────

def test_redrop_creates_signal_with_bundle_abspath(promote_dir, bundle):
    conn = _FakeConn([("INC-1", bundle)], {cda.ct_id_for("INC-1"): "PROMOTED"})
    s = rec.reconcile_once(conn)
    assert s["redropped"] == ["INC-1"] and s["scanned"] == 1
    sig = promote_dir / _sig_name("INC-1")
    assert sig.exists()
    payload = json.loads(sig.read_text(encoding="utf-8"))
    assert payload["incident_id"] == "INC-1"
    assert os.path.isabs(payload["bundle"])       # consumer cwd 무관 (스모크 교훈)


def test_failed_signal_never_redropped(promote_dir, bundle):
    (promote_dir / "failed" / _sig_name("INC-2")).write_text("{}", encoding="utf-8")
    conn = _FakeConn([("INC-2", bundle)], {cda.ct_id_for("INC-2"): "PROMOTED"})
    s = rec.reconcile_once(conn)
    assert s["escalate_failed"] == ["INC-2"] and s["redropped"] == []
    assert not (promote_dir / _sig_name("INC-2")).exists()      # 원본 자리 생성 금지


def test_processed_signal_is_ok_and_untouched(promote_dir, bundle):
    p = promote_dir / "processed" / _sig_name("INC-3")
    p.write_text("{}", encoding="utf-8")
    conn = _FakeConn([("INC-3", bundle)], {cda.ct_id_for("INC-3"): "AUTO_PROMOTED"})
    s = rec.reconcile_once(conn)
    assert s["ok"] == 1 and s["redropped"] == []
    assert p.exists() and not (promote_dir / _sig_name("INC-3")).exists()


def test_stale_original_logged_but_file_untouched(promote_dir, bundle):
    sig = promote_dir / _sig_name("INC-4")
    sig.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    os.utime(sig, (old, old))
    conn = _FakeConn([("INC-4", bundle)], {cda.ct_id_for("INC-4"): "PROMOTED"})
    s = rec.reconcile_once(conn, stale_min=10.0)
    assert s["stale"] == ["INC-4"] and s["redropped"] == []
    assert sig.exists() and sig.stat().st_mtime == pytest.approx(old)   # 무접촉


def test_rolled_back_never_redropped_even_without_trace(promote_dir, bundle):
    conn = _FakeConn([("INC-5", bundle)], {cda.ct_id_for("INC-5"): "ROLLED_BACK"})
    s = rec.reconcile_once(conn)
    assert s["skip_status"] == ["INC-5"] and s["redropped"] == []
    assert not (promote_dir / _sig_name("INC-5")).exists()


def test_redrop_capped_at_one_per_scan(promote_dir, bundle):
    ct = {cda.ct_id_for(i): "PROMOTED" for i in ("INC-6", "INC-7")}
    conn = _FakeConn([("INC-6", bundle), ("INC-7", bundle)], ct)
    s = rec.reconcile_once(conn)
    assert len(s["redropped"]) == 1               # 실행당 1건 — 나머지는 다음 주기
    assert s["waiting"] == ["INC-7"]
    assert not (promote_dir / _sig_name("INC-7")).exists()


def test_missing_bundle_path_skips_and_logs(promote_dir, tmp_path):
    gone = str(tmp_path / "bundles" / "deleted_v0")
    conn = _FakeConn([("INC-8", gone)], {cda.ct_id_for("INC-8"): "PROMOTED"})
    s = rec.reconcile_once(conn)
    assert s["skip_no_bundle"] == ["INC-8"] and s["redropped"] == []
    assert not (promote_dir / _sig_name("INC-8")).exists()
