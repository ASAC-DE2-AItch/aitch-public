# -*- coding: utf-8 -*-
"""WP-B 코드 결함 수정 테스트 (R1~R12 — CT2_구현플래너_v2 §3 WP-B).

검증:
  B1  claim_running 원자 선점 (재수신 skip) + finalize_decision 전이·0행 경고
      → 멱등 창이 '이벤트 수신 시점'으로 좁혀져 학습 중 재전달이 이중 생성되지 않는다
  B4  load_limits: assert 제거 → 리스트/dict/필수키 위반에 raise ValueError (헌법 7장)
  B6  rows_from_kafka._utc: naive→UTC 정규화 (naive/aware/Z 동일 결과, TypeError 오진 차단)

DB는 in-memory 스텁 — SQL 문법이 아니라 **분기·rowcount·기록 내용**을 검증한다.

실행: python -m pytest tests/test_ct2_wp_b.py -q
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ct2_orchestrator as O            # noqa: E402
import ct2_assemble_trainset as A       # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# B1 — RUNNING 선점 + finalize (in-memory ct_decisions 스텁)
# ══════════════════════════════════════════════════════════════════════════

class FakeCtCursor:
    """ct_decisions를 dict(row_by_ct_id)로 흉내내는 커서. claim/finalize 분기 검증용."""

    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._fetch = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT 1 FROM ct_decisions"):
            ct_id = params[0]
            self._fetch = (1,) if ct_id in self.store else None
        elif s.startswith("INSERT INTO ct_decisions"):
            ct_id = params[0]
            if ct_id in self.store:                      # ON CONFLICT DO NOTHING
                self.rowcount = 0
            elif len(params) == 2:                       # claim_running: (ct_id, qual_id), 나머지 리터럴
                self.store[ct_id] = {"retrain_status": "RUNNING",
                                     "trigger_reason": "chamber_requalified",
                                     "model_version_after": None}
                self.rowcount = 1
            else:                                        # record_decision: (ct_id, reason, qual, status, after)
                self.store[ct_id] = {"retrain_status": params[3], "trigger_reason": params[1],
                                     "model_version_after": params[4]}
                self.rowcount = 1
        elif s.startswith("UPDATE ct_decisions"):
            # SET retrain_status=%s, model_version_after=COALESCE(%s,..), trigger_reason=COALESCE(%s,..) WHERE ct_id=%s
            status, after, reason, ct_id = params
            if ct_id in self.store:
                row = self.store[ct_id]
                row["retrain_status"] = status
                if after is not None:
                    row["model_version_after"] = after
                if reason is not None:
                    row["trigger_reason"] = reason
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:
            raise AssertionError(f"예상치 못한 SQL: {s[:60]}")

    def fetchone(self):
        return self._fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCtConn:
    def __init__(self):
        self.store = {}
        self.commits = 0

    def cursor(self):
        return FakeCtCursor(self.store)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def test_claim_running_wins_once_then_skips():
    """첫 선점만 True, 이후(RUNNING 또는 종결 존재) 재수신은 False = skip."""
    conn = FakeCtConn()
    cid = "CT-20260730-SIMCH1-001-AE"
    assert O.claim_running(conn, cid, "QUAL-1") is True
    assert conn.store[cid]["retrain_status"] == "RUNNING"
    # 학습 중 재전달 모사 — 이미 RUNNING이므로 skip
    assert O.claim_running(conn, cid, "QUAL-1") is False
    # 종결된 뒤 재전달도 skip
    conn.store[cid]["retrain_status"] = "AUTO_PROMOTED"
    assert O.claim_running(conn, cid, "QUAL-1") is False


def test_finalize_transitions_running_and_sets_bundle():
    """RUNNING → 종결 상태 + 번들명 + 분류자 trigger_reason."""
    conn = FakeCtConn()
    cid = "CT-X-AE"
    O.claim_running(conn, cid, None)
    ok = O.finalize_decision(conn, cid, "AUTO_PROMOTED", after="ae_ct2_SIM_CH_1_20260730_120000",
                             reason="chamber_requalified")
    assert ok is True
    row = conn.store[cid]
    assert row["retrain_status"] == "AUTO_PROMOTED"
    assert row["model_version_after"] == "ae_ct2_SIM_CH_1_20260730_120000"


def test_finalize_without_claim_warns_zero_rows():
    """선점 없이 finalize하면 0행 → False (경고). model_version_after 유실 방지 신호."""
    conn = FakeCtConn()
    assert O.finalize_decision(conn, "CT-missing-AE", "FAILED") is False


def test_finalize_coalesce_keeps_existing_bundle():
    """after=None이면 기존 model_version_after를 덮어쓰지 않는다 (COALESCE)."""
    conn = FakeCtConn()
    cid = "CT-Y-AE"
    O.claim_running(conn, cid, None)
    O.finalize_decision(conn, cid, "AUTO_PROMOTED", after="bundle_v1")
    O.finalize_decision(conn, cid, "ROLLED_BACK", after=None, reason="chamber_requalified/rolled_back")
    assert conn.store[cid]["model_version_after"] == "bundle_v1"      # 유지
    assert conn.store[cid]["retrain_status"] == "ROLLED_BACK"


def test_running_status_fits_db_column():
    """신규 RUNNING 상태값이 VARCHAR(16) 이내."""
    assert len("RUNNING") <= 16


def test_post_claim_failure_transitions_to_failed_and_reraises(monkeypatch):
    """★선점 후 g3/finalize 자체가 실패해도 RUNNING이 남지 않는다 (claim 불변식 = 항상 terminal).

    g3(guard_r9_approved)에서 일시 DB 오류를 모사한다. handle_event는 except에서 FAILED로
    종결한 뒤 재-raise해야 한다(main이 커밋 보류 → 재처리, 그때 g2가 FAILED 보고 skip).
    이 방어가 없으면 행이 RUNNING에 멈춘 채 offset만 전진해 CT²가 조용히 유실된다.
    """
    conn = FakeCtConn()
    monkeypatch.setattr(O, "_db", lambda: conn)
    monkeypatch.setattr(O, "guard_r9_approved",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("일시 DB 오류")))
    evt = {"event_type": "ChamberRequalified", "incident_id": "INC-20260730-SIMCH1-009",
           "chamber_id": "SIM_CH_1", "qual_id": "QUAL-9"}
    with pytest.raises(RuntimeError, match="일시 DB 오류"):
        O.handle_event(evt, {"ct2_auto_generate": True})
    cid = O.ct_id_for("INC-20260730-SIMCH1-009")
    assert conn.store[cid]["retrain_status"] == "FAILED", "RUNNING이 종결되지 않았다"
    assert conn.store[cid]["trigger_reason"] == "chamber_requalified/handler_error"


def test_dry_run_records_without_claim(monkeypatch):
    """dry-run(auto_generate=false)은 record_decision(DRYRUN) — claim_running 미경유."""
    conn = FakeCtConn()
    monkeypatch.setattr(O, "_db", lambda: conn)
    evt = {"event_type": "ChamberRequalified", "incident_id": "INC-20260730-SIMCH1-010",
           "chamber_id": "SIM_CH_1", "qual_id": "QUAL-10"}
    O.handle_event(evt, {"ct2_auto_generate": False})
    cid = O.ct_id_for("INC-20260730-SIMCH1-010")
    assert conn.store[cid]["retrain_status"] == "DRYRUN"


def test_trigger_reason_classifier_fits_64():
    """B9 R11 — 분류자 병기 trigger_reason이 VARCHAR(64) 이내."""
    for s in ("chamber_requalified/converged_unknown", "chamber_requalified/r9_consent_missing",
              "chamber_requalified/blocked", "chamber_requalified/auto_promoted"):
        assert len(s) <= 64, s


# ══════════════════════════════════════════════════════════════════════════
# B4 — load_limits: assert → raise ValueError
# ══════════════════════════════════════════════════════════════════════════

def _write(tmp_path, obj):
    p = tmp_path / "limits.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_load_limits_accepts_valid(tmp_path):
    rows = A.load_limits(_write(tmp_path, [{"sensor": "C11", "step": 4, "lcl": 1.0, "ucl": 9.0},
                                           {"sensor": "C17", "step": None, "ucl": 5.0}]))
    assert len(rows) == 2


def test_load_limits_rejects_non_list(tmp_path):
    """json.loads 성공 ≠ 리스트 — dict/스칼라 최상위는 거부 (헌법 7장)."""
    with pytest.raises(ValueError, match="리스트 아님"):
        A.load_limits(_write(tmp_path, {"sensor": "C11", "ucl": 9.0}))


@pytest.mark.parametrize("bad", [
    [{"step": 4, "lcl": 1.0}],                    # sensor 없음
    [{"sensor": "C11", "step": 4}],               # lcl/ucl 둘 다 없음
    [{"sensor": "C11", "ucl": 9.0}, "not-a-dict"],  # 원소가 dict 아님
])
def test_load_limits_raises_on_malformed(tmp_path, bad):
    with pytest.raises(ValueError, match="형식 오류"):
        A.load_limits(_write(tmp_path, bad))


def test_load_limits_is_not_assert():
    """소스에 assert가 남아 있지 않음 — python -O 방어선 유실 차단 (헌법 7장)."""
    src = (REPO_ROOT / "scripts" / "ct2_assemble_trainset.py").read_text(encoding="utf-8")
    # load_limits 함수 본문에 assert 문이 없어야 한다
    body = src.split("def load_limits")[1].split("\ndef ")[0]
    assert "assert " not in body, "load_limits에 assert 잔존 (raise ValueError로 교체해야 함)"


# ══════════════════════════════════════════════════════════════════════════
# B6 — _utc tz 정규화
# ══════════════════════════════════════════════════════════════════════════

def test_utc_normalizes_naive_and_aware_equal():
    """naive(=UTC 간주)·Z 접미·+00:00 세 형태가 동일 순간으로."""
    a = A._utc("2026-07-30T12:00:00")            # naive → UTC
    b = A._utc("2026-07-30T12:00:00Z")           # Z
    c = A._utc("2026-07-30T12:00:00+00:00")      # 명시 offset
    assert a == b == c
    assert a.tzinfo is not None                  # 전부 aware


def test_utc_comparison_no_typeerror():
    """정규화된 두 시각 비교가 TypeError 없이 성립 (오진 차단의 핵심)."""
    until = A._utc("2026-07-30T12:00:00")        # naive 입력
    msg_ts = A._utc("2026-07-30T13:00:00Z")      # aware 입력
    assert (msg_ts > until) is True              # 구버전이면 여기서 TypeError였다


def test_utc_offset_preserved():
    """비-UTC offset은 UTC로 환산 — 같은 순간이면 동일."""
    kst = A._utc("2026-07-30T21:00:00+09:00")
    utc = A._utc("2026-07-30T12:00:00Z")
    assert kst == utc


if __name__ == "__main__":                       # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
