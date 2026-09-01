# -*- coding: utf-8 -*-
"""B-2/B5-1 — modify 수정값 영속 단위 테스트 (DB·langgraph 무의존).

순서 규약 검증: [*_modified 기입] → [판정 커밋] → [발행]. 기입 실패 = fail-closed(PENDING 유지).
실행: python tests/test_modify_persist.py  (또는 python -m pytest -q)
"""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from orchestrator.approval_graph import (apply_correction,  # noqa: E402
                                         resolve_modified_fields)
from src.gateway.approvals import validate_decision  # noqa: E402


def _state(action="modify", mv=-64.0, cid="LIM-20260723-SIMCH3-002"):
    return {"incident_id": "INC-1", "decision": action, "modified_value": mv, "approver": "eng",
            "report": {"selected_option": "limit_option",
                       "limit_analysis": {"correction_id": cid, "sensor": "C11",
                                          "limit_version_current": "v3", "center_proposed": -64.2}}}


class Store:
    def __init__(self, write_ok=True):
        self.seq, self.write_ok = [], write_ok

    def write_modified_fields(self, cid, fields):
        self.seq.append(("write", cid, dict(fields)))
        return self.write_ok

    def commit_decision(self, *a, **k):
        self.seq.append(("commit",))
        return True

    def set_lifecycle(self, *a, **k):
        pass    # P6-1 actioned→verifying — 이 테스트 관심사 아님 (seq 순서 보존 위해 미기록)


class LegacyStore:
    """write_modified_fields 부재 — 구형 stub 하위호환 경로."""
    def __init__(self):
        self.seq = []

    def commit_decision(self, *a, **k):
        self.seq.append(("commit",))
        return True

    def set_lifecycle(self, *a, **k):
        pass    # P6-1 — Store와 동일 (미기록)


def _emit_into(seq):
    return lambda ev, payload: seq.append(("emit", ev))


def test_resolve_scalar_dict_and_rejects():
    assert resolve_modified_fields("limit_option", -64.0) == {"center_modified": -64.0}
    assert resolve_modified_fields("limit_option", {"ucl": 55}) == {"ucl_modified": 55.0}
    assert resolve_modified_fields("limit_option", {"ucl": 55, "wat": 1, "lcl": True}) == {"ucl_modified": 55.0}
    assert resolve_modified_fields("recipe_option", -1.0) == {}        # recipe는 B5-4 후속
    assert resolve_modified_fields("limit_option", True) == {}         # bool 차단
    assert resolve_modified_fields("limit_option", "3.3") == {}        # 문자열 차단


def test_modify_order_write_then_commit_then_emit():
    st = Store()
    out = apply_correction(_state(), st, _emit_into(st.seq))
    assert [x[0] for x in st.seq] == ["write", "commit", "emit", "emit"], st.seq
    assert st.seq[0][1] == "LIM-20260723-SIMCH3-002"
    assert st.seq[0][2] == {"center_modified": -64.0}
    assert out["result"].get("correction_type") == "limit"


def test_write_fail_is_fail_closed():
    st = Store(write_ok=False)
    out = apply_correction(_state(), st, _emit_into(st.seq))
    assert [x[0] for x in st.seq] == ["write"]           # 커밋·발행 없음 — PENDING 유지
    assert out["result"]["blocked"] == "modified_fields write failed"


def test_modify_without_correction_id_blocked():
    st = Store()
    out = apply_correction(_state(cid=None), st, _emit_into(st.seq))
    assert st.seq == [] and out["result"]["blocked"] == "modify without correction_id"


def test_approve_path_untouched():
    st = Store()
    apply_correction(_state(action="approve"), st, _emit_into(st.seq))
    assert [x[0] for x in st.seq] == ["commit", "emit", "emit"]


def test_legacy_store_warns_and_proceeds():
    st = LegacyStore()
    apply_correction(_state(), st, _emit_into(st.seq))
    assert [x[0] for x in st.seq] == ["commit", "emit", "emit"]


def test_recipe_modify_payload_only():
    st = Store()
    s = {"incident_id": "INC-2", "decision": "modify", "modified_value": -1.3,
         "report": {"selected_option": "recipe_option", "recipe_tuning": {"parameter_id": "C1"}}}
    apply_correction(s, st, _emit_into(st.seq))
    assert [x[0] for x in st.seq] == ["commit", "emit", "emit"]   # write 없음 (정보성 전달만)


def test_validate_decision_shapes():
    ok = validate_decision({"action": "modify", "approver": "e", "modified_value": -64.0})
    assert ok["modified_value"] == -64.0
    ok2 = validate_decision({"action": "modify", "approver": "e", "modified_value": {"ucl": 55, "lcl": 10}})
    assert ok2["modified_value"] == {"ucl": 55, "lcl": 10}
    for bad in ["3.3", True, {"center": "x"}, {"foo": 1}]:
        try:
            validate_decision({"action": "modify", "approver": "e", "modified_value": bad})
            raise AssertionError(f"ValueError 미발생: {bad!r}")
        except ValueError:
            pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
