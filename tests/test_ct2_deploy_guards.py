# -*- coding: utf-8 -*-
"""CT² 자동 promote 안전장치 테스트 (헌법 1-1 예외 3 · 3-3 ③ ⓑⓒ · 1-4).

★핵심은 **게이트 우회 차단**이다. `scripts/ct2_assemble_trainset.py` 가 남기는 조립
`meta.json` 에도 `gate_pass` 키가 있다(의미 = wafer 표본 수 게이트) — `gate_pass` 만 보고
배포하면 `validate_bundle` 이 한 번도 실행되지 않은 번들이 통과한다. 그래서 발급자
(`gate_source`)·번들명·`failed_checks` 를 함께 확인한다.

검증 항목:
  ① 조립 meta.json 으로는 자동 promote 불가 (발급자 확인)
  ② 다른 번들의 PASS 리포트 재사용 불가 (bundle_name 대조)
  ③ gate_pass=true 인데 failed_checks 가 남은 모순 리포트 거부
  ④ 빈/비-dict 리포트 거부 (§7 — json.loads 성공 ≠ dict)
  ⑤ 거부(REJECTED)·대기(PENDING) 이력 위의 자동 배포 차단 (1-4)
  ⑥ 같은 번들 재수신은 멱등 (감사 행 중복 없음)
  ⑦ 상태값 대문자 통일 (혼합 케이싱 = 감사 조회 누락)

DB 는 최소 스텁이다 — SQL 문법이 아니라 **분기와 기록 내용**을 검증한다.

실행: python -m pytest tests/test_ct2_deploy_guards.py -q
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.orchestrator import ct2_deploy_approval as D  # noqa: E402


BUNDLE = "models/anomaly_ae/ae_ct2_SIM_CH_1_20260730_120000"
NAME = "ae_ct2_SIM_CH_1_20260730_120000"
INCIDENT = "INC-20260730-SIMCH1-001"


def _gate_report(bundle_name=NAME, gate_pass=True, failed=None):
    """validate_bundle 리포트 형태 (필요한 키만)."""
    return {
        "gate_pass": gate_pass,
        "failed_checks": failed or [],
        "bundle_name": bundle_name,
        "metrics": {"recon_rmse": 0.33, "l5": 0.014, "n_val": 150,
                    "b_group_sample": "holdout"},
        "gate_source": "ae_pipeline/validate_bundle.py (헌법 3-3 ③ ⓑ 위임 — 항목·임계 단일 소스)",
    }


def _assemble_meta():
    """조립 스크립트 meta.json — `gate_pass` 키가 **있지만** 게이트 근거가 아니다."""
    return {"source": "kafka:fdc.raw", "chamber": "SIM_CH_1", "wafers_seen": 1200,
            "wafers_kept": 1100, "min_wafers_gate": 1000, "gate_pass": True}


# ── ①~④ 리포트 검증 ────────────────────────────────────────────────────────

def test_assemble_meta_is_rejected():
    """★조립 meta.json 으로는 자동 promote 불가 — 게이트 미실행 배포 차단."""
    reason = D.validate_gate_report(_assemble_meta(), BUNDLE, "trainset.meta.json")
    assert reason and "검증 리포트가 아님" in reason


def test_gate_report_is_accepted():
    assert D.validate_gate_report(_gate_report(), BUNDLE, "validation.json") is None


def test_report_reuse_across_bundles_is_rejected():
    """② 이전 번들의 PASS 리포트를 새 번들에 재사용하면 거부."""
    reason = D.validate_gate_report(_gate_report(bundle_name="ae_ct2_SIM_CH_1_20260101_000000"),
                                    BUNDLE, "validation.json")
    assert reason and "재사용 방어" in reason


def test_contradictory_report_is_rejected():
    """③ gate_pass=true 인데 failed_checks 가 남은 모순 리포트 거부 (이중 확인)."""
    reason = D.validate_gate_report(_gate_report(failed=["C1_프로브_검출률"]),
                                    BUNDLE, "validation.json")
    assert reason and "게이트 미통과" in reason


def test_failed_report_is_rejected():
    reason = D.validate_gate_report(_gate_report(gate_pass=False, failed=["D1_VAL_표본_하한"]),
                                    BUNDLE, "validation.json")
    assert reason and "게이트 미통과" in reason


@pytest.mark.parametrize("bad", [{}, None, "123", [1, 2], 5])
def test_non_dict_or_empty_report_is_rejected(bad):
    """④ `json.loads` 성공 ≠ dict — 스칼라·배열·빈 dict 전부 거부 (§7)."""
    reason = D.validate_gate_report(bad, BUNDLE, "x.json")
    assert reason and "읽을 수 없음" in reason


# ── ⑤⑥ 감사 기록 + 이력 가드 ────────────────────────────────────────────────

class FakeCursor:
    """approval_records SELECT 결과만 흉내내고 INSERT 를 캡처하는 스텁.

    `requalify` 조회(R9 실존 — 1-1 예외 3 ⓒ)는 `r9` 목록으로 따로 응답한다.
    """

    def __init__(self, rows, sink, r9=((1,),)):
        self._rows, self._sink, self._r9 = rows, sink, list(r9)
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._last = (" ".join(sql.split()), params)
        if sql.lstrip().upper().startswith("SELECT"):
            self._fetch = list(self._r9) if "requalify" in sql else list(self._rows)
        else:
            self._sink.append(self._last)
            self.rowcount = 1
            self._fetch = []

    def fetchall(self):
        return self._fetch

    def fetchone(self):
        return self._fetch[0] if self._fetch else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows=(), r9=((1,),)):
        self.rows, self.writes, self.commits = list(rows), [], 0
        self.r9 = list(r9)

    def cursor(self):
        return FakeCursor(self.rows, self.writes, self.r9)

    def commit(self):
        self.commits += 1


def test_auto_approval_inserts_uppercase_status_and_report_path():
    """⑦ status 는 대문자 APPROVED, 사유에 리포트 경로 (3-3 ③ ⓒ)."""
    conn = FakeConn()
    made = D.record_auto_approval(conn, INCIDENT, BUNDLE, "val.json", _gate_report())
    assert made is True and conn.writes
    sql, params = conn.writes[0]
    assert "INSERT INTO approval_records" in sql
    assert D.STATUS_APPROVED in params and D.STATUS_APPROVED == "APPROVED"
    assert params[3] == NAME, "selected_option = 번들명 (VARCHAR(32))"
    reason = next(p for p in params if isinstance(p, str) and "게이트 PASS" in p)
    assert "val.json" in reason, "무엇을 근거로 배포됐는지 추적 불가"
    assert D.AUTO_APPROVER in params


def test_auto_approval_is_idempotent_for_same_bundle():
    """⑥ 같은 번들의 자동 승인 행이 이미 있으면 새로 만들지 않는다 (재수신)."""
    conn = FakeConn([(D.STATUS_APPROVED, D.AUTO_APPROVER, NAME)])
    assert D.record_auto_approval(conn, INCIDENT, BUNDLE, "val.json", _gate_report()) is False
    assert conn.writes == []


def test_auto_approval_blocked_by_rejection_history():
    """⑤ 엔지니어 거부 이력 위에 자동 배포하지 않는다 (1-4 역할 조정)."""
    conn = FakeConn([(D.STATUS_REJECTED, "engineer_01", NAME)])
    with pytest.raises(RuntimeError, match="거부 이력"):
        D.record_auto_approval(conn, INCIDENT, BUNDLE, "val.json", _gate_report())
    assert conn.writes == []


def test_auto_approval_blocked_by_pending():
    """⑤ 사람 승인 대기(PENDING) 건을 자동 배포로 덮지 않는다."""
    conn = FakeConn([("PENDING", None, NAME)])
    with pytest.raises(RuntimeError, match="PENDING"):
        D.record_auto_approval(conn, INCIDENT, BUNDLE, "val.json", _gate_report())
    assert conn.writes == []


def test_auto_approval_allows_new_bundle_after_prior_auto_approval():
    """다른(신규) 번들은 이전 자동 승인 이력과 무관하게 기록된다 — 롤백 후 재배포 경로."""
    conn = FakeConn([(D.STATUS_APPROVED, D.AUTO_APPROVER, "ae_ct2_SIM_CH_1_20260101_000000")])
    assert D.record_auto_approval(conn, INCIDENT, BUNDLE, "val.json", _gate_report()) is True


def test_auto_promote_refuses_bad_report(monkeypatch):
    """auto_promote 자체도 리포트를 재검증한다 (호출자 신뢰만으로 배포 금지)."""
    monkeypatch.setattr(D, "drop_promote_signal", lambda *a: pytest.fail("배포가 실행됐다"))
    with pytest.raises(RuntimeError, match="자동 promote 거부"):
        D.auto_promote(FakeConn(), INCIDENT, BUNDLE, "meta.json", _assemble_meta())


def test_auto_promote_requires_r9_approval(monkeypatch):
    """★상류 R9 승인이 없으면 자동 배포 금지 (헌법 1-1 예외 3 ⓒ).

    오케스트레이터 가드 g3 가 같은 검사를 하지만, 이 모듈은 CLI 로 직접 호출될 수 있어
    여기서도 확인한다 — 게이트 리포트를 재검증하는 것과 같은 이유.
    """
    monkeypatch.setattr(D, "drop_promote_signal", lambda *a: pytest.fail("배포가 실행됐다"))
    conn = FakeConn(r9=())                                  # requalify 승인 없음
    with pytest.raises(RuntimeError, match="R9"):
        D.auto_promote(conn, INCIDENT, BUNDLE, "val.json", _gate_report())
    assert conn.writes == []


def test_r9_check_is_case_insensitive():
    """status 케이싱이 섞여 있어도 R9 조회가 놓치지 않는다 (UPPER 비교)."""
    conn = FakeConn()
    assert D.r9_approval_exists(conn, INCIDENT) is True
    assert D.r9_approval_exists(FakeConn(r9=()), INCIDENT) is False


def test_auto_promote_refuses_overlong_bundle_name(monkeypatch):
    """CLI 직접 호출 시에도 `selected_option` 폭(32)을 초과하면 거부한다.

    오케스트레이터의 길이 검사를 우회하는 경로이므로 여기서도 막는다.
    """
    monkeypatch.setattr(D, "drop_promote_signal", lambda *a: pytest.fail("배포가 실행됐다"))
    long_bundle = "models/anomaly_ae/ae_ct2_SIM_CHAMBER_LONGNAME_1_20260730_120000"
    rpt = _gate_report(bundle_name=Path(long_bundle).name)
    with pytest.raises(RuntimeError, match="selected_option"):
        D.auto_promote(FakeConn(), INCIDENT, long_bundle, "val.json", rpt)


def test_auto_promote_happy_path_drops_signal(monkeypatch, tmp_path):
    """전 조건 충족 시 감사 행 + ct_decisions + 신호 + CHANGELOG 가 모두 남는다."""
    monkeypatch.setattr(D, "REPO_ROOT", tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "CHANGELOG.md").write_text("# CHANGELOG\n", encoding="utf-8")
    monkeypatch.setattr(D, "PROMOTE_DIR", tmp_path / "control" / "ct2")
    conn = FakeConn()
    sig = D.auto_promote(conn, INCIDENT, BUNDLE, "val.json", _gate_report())
    assert sig.exists() and sig.name == f"promote_{INCIDENT}.json"
    payload = json.loads(sig.read_text(encoding="utf-8"))
    assert payload["ct_id"] == D.ct_id_for(INCIDENT)
    assert Path(payload["bundle"]).is_absolute(), "consumer cwd 무관 절대경로여야 한다"
    assert any("INSERT INTO approval_records" in s for s, _p in conn.writes)
    assert NAME in (tmp_path / "models" / "CHANGELOG.md").read_text(encoding="utf-8")


# ── mark_ct_decision 0행 가시화 ─────────────────────────────────────────────

class ZeroRowCursor(FakeCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.rowcount = 0                      # ct_decisions 행이 아직 없는 상황


def test_mark_ct_decision_reports_zero_rows():
    """행이 없으면 조용한 no-op 이 아니라 False + 경고 (model_version_after 유실 방지)."""
    conn = FakeConn()
    conn.cursor = lambda: ZeroRowCursor(conn.rows, conn.writes)
    assert D.mark_ct_decision(conn, INCIDENT, "AUTO_PROMOTED", BUNDLE) is False


# ── CHANGELOG 문안 (3-3·4-1) ────────────────────────────────────────────────

def test_changelog_line_carries_metrics(tmp_path, monkeypatch):
    """헌법 3-3(버전·RMSE·사유)·4-1 요구 수치가 기록에 들어간다."""
    monkeypatch.setattr(D, "REPO_ROOT", tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "CHANGELOG.md").write_text("# CHANGELOG\n", encoding="utf-8")
    D.append_changelog(INCIDENT, BUNDLE, D.AUTO_APPROVER, _gate_report(), auto=True)
    text = (tmp_path / "models" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert NAME in text and "0.33" in text and "자동 게이트 PASS" in text
    assert "사유:" in text and INCIDENT in text


def test_changelog_failure_does_not_raise(monkeypatch, tmp_path):
    """기록 실패가 배포 흐름을 깨지 않는다 (로그만)."""
    monkeypatch.setattr(D, "REPO_ROOT", tmp_path / "nonexistent")
    D.append_changelog(INCIDENT, BUNDLE, D.AUTO_APPROVER, _gate_report(), auto=True)


def test_thread_and_ct_id_conventions():
    """파생 thread·ct_id 규약 (1-4 · 계약 §8-E)."""
    assert D.thread_id_for(INCIDENT) == f"{INCIDENT}-CT2"
    assert D.ct_id_for(INCIDENT) == "CT-20260730-SIMCH1-001-AE"


def test_request_type_fits_db_column():
    """`approval_records.request_type` VARCHAR(32) · `status` VARCHAR(16)."""
    assert len(D.REQUEST_TYPE) <= 32
    assert len(D.STATUS_APPROVED) <= 16 and len(D.STATUS_REJECTED) <= 16
    assert len(D.AUTO_APPROVER) <= 64 and len(D.AUTO_APPROVER_ROLE) <= 32


if __name__ == "__main__":                     # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
