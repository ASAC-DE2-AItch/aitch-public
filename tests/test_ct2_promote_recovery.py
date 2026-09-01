# -*- coding: utf-8 -*-
"""promote() 부분 실패 복구 판정기 테스트 (#100 리뷰 ② ⓐ — 2026-08-04).

시나리오: promote 가 ①승인 닫기(커밋)·②ct_decisions(커밋)까지 가고 ③신호 드롭에서 죽으면,
재시도의 close_ct2_pending 은 0행(False)이다. 이때 「APPROVED 인데 신호 파일이 없다」를
복구 신호로 쓴다 — 그 판정기(_promote_recovery_needed)를 여기서 못박는다.
그래프 배선(promote 노드가 이 판정기로 ②③④를 재수행)은 C 리뷰가 커버.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import ct2_deploy_approval as M  # noqa: E402


class _Cur:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, args):
        self.sql = sql

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cur(self._row)


def test_approved_exists_true_false():
    assert M.approved_ct2_exists(_Conn((1,)), "INC-X") is True
    assert M.approved_ct2_exists(_Conn(None), "INC-X") is False


def test_signal_path_single_definition(tmp_path, monkeypatch):
    """드롭과 복구 판정이 같은 경로 정의를 쓴다 — 이름이 갈라지면 복구가 영원히 참."""
    monkeypatch.setattr(M, "PROMOTE_DIR", tmp_path)
    assert M.promote_signal_path("INC-9").name == "promote_INC-9.json"
    assert M.promote_signal_path("INC-9").parent == tmp_path


def test_recovery_needed_only_when_approved_and_signal_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "PROMOTE_DIR", tmp_path)
    # APPROVED + 드롭 이력 없음 → 복구 필요 (③에서 죽은 이전 시도)
    assert M._promote_recovery_needed(_Conn((1,)), "INC-1") is True
    # APPROVED + 신호 있음 → 불필요 (정상 완료의 중복 resume — 종전과 동일 무시)
    M.promote_signal_path("INC-2").write_text("{}", encoding="utf-8")
    assert M._promote_recovery_needed(_Conn((1,)), "INC-2") is False
    # 승인 자체가 없음 → 불필요
    assert M._promote_recovery_needed(_Conn(None), "INC-3") is False


def test_drop_lands_where_check_looks(tmp_path, monkeypatch):
    """드롭과 판정이 같은 정의 — 실제 드롭을 불러 못박는다 (#100 리뷰 ③ 제안 테스트)."""
    monkeypatch.setattr(M, "PROMOTE_DIR", tmp_path)
    sig = M.drop_promote_signal("INC-7", "models/anomaly_ae/ae_x")
    assert sig == M.promote_signal_path("INC-7")
    assert M.promote_signal_dropped("INC-7")


def test_consumed_or_failed_signal_still_counts_as_dropped(tmp_path, monkeypatch):
    """consumer 가 processed/·failed/ 로 옮긴 뒤에도 '떨궜음' — 정상 완료·로드 실패를
    복구 대상으로 오판해 재드롭(자동 롤백 되밀기 포함)하지 않는다 (#100 리뷰 ③ 표 3~5행)."""
    monkeypatch.setattr(M, "PROMOTE_DIR", tmp_path)
    (tmp_path / "processed").mkdir()
    (tmp_path / "processed" / "promote_INC-4.json").write_text("{}", encoding="utf-8")
    assert M._promote_recovery_needed(_Conn((1,)), "INC-4") is False
    (tmp_path / "failed").mkdir()
    (tmp_path / "failed" / "promote_INC-5.json").write_text("{}", encoding="utf-8")
    assert M._promote_recovery_needed(_Conn((1,)), "INC-5") is False


# ── ①② 단일 트랜잭션 (#100 리뷰 ② 후속) ────────────────────────────────────
#   구현은 ①승인 닫기·②ct_decisions 가 각각 커밋해서, ①만 커밋되고 ②가 죽으면
#   "APPROVED 인데 ct_decisions 미갱신"이 고착됐다(재시도의 ①은 0행). 한 트랜잭션으로
#   묶으면 ② 실패 시 ①도 함께 풀려 PENDING 이 유지되고 재승인이 정상 동작한다.
#   그래프 노드(promote/reject)가 실제로 commit=False 로 부르는지는 langgraph 의존이라
#   level2(pg)가 담당하고, 여기서는 **스위치가 스위치답게 동작하는지**를 못박는다.
class _WriteCur:
    def __init__(self, rowcount):
        self.rowcount = rowcount

    def execute(self, sql, args=None):
        self.sql = sql

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _WriteConn:
    def __init__(self, rowcount=1):
        self.commits = 0
        self._rowcount = rowcount

    def cursor(self):
        return _WriteCur(self._rowcount)

    def commit(self):
        self.commits += 1


def test_close_pending_commit_switch():
    """기본은 자체 커밋(기존 호출자 무변경) · commit=False 면 커밋하지 않는다."""
    c = _WriteConn(rowcount=1)
    assert M.close_ct2_pending(c, "INC-1", "APPROVED", None, "eng", None) is True
    assert c.commits == 1                                    # 기본값 — 거동 보존

    c2 = _WriteConn(rowcount=1)
    assert M.close_ct2_pending(c2, "INC-1", "APPROVED", None, "eng", None, commit=False) is True
    assert c2.commits == 0                                   # 호출자가 묶어서 커밋


def test_mark_ct_decision_commit_switch():
    """ct_decisions 도 같은 스위치 — 두 쓰기를 한 트랜잭션으로 묶을 수 있어야 한다."""
    c = _WriteConn(rowcount=1)
    assert M.mark_ct_decision(c, "INC-1", "PROMOTED", "ae_x") is True
    assert c.commits == 1

    c2 = _WriteConn(rowcount=1)
    assert M.mark_ct_decision(c2, "INC-1", "PROMOTED", "ae_x", commit=False) is True
    assert c2.commits == 0


def test_commit_switch_does_not_change_rowcount_verdict():
    """멱등 가드(0행=이미 닫힘)는 커밋 여부와 무관하게 유지된다."""
    for commit in (True, False):
        assert M.close_ct2_pending(_WriteConn(rowcount=0), "INC-1", "APPROVED",
                                   None, "eng", None, commit=commit) is False
