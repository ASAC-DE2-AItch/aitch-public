# -*- coding: utf-8 -*-
"""P4-1 검증 — 승인 게이트 노드 로직 단위테스트 (Fake DB, langgraph 불요).

interrupt/resume의 **비즈니스 로직**(판정 기록·멱등·lifecycle 전이·reopen_count·이벤트·옵션별 적용 분기)을
langgraph 없이 검증한다. 그래프 실행(interrupt→resume 왕복 라우팅·PostgresSaver)은 실 langgraph+Postgres
(사용자 env / docker-compose)에서 별도 통합테스트(#24) — 이 파일 범위 밖.
실행: python tests/test_approval_graph.py   (또는 pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orchestrator.approval_graph import (  # noqa: E402
    ACTION_STATUS, merge_reports, gate_ensure, gate_parse,
    apply_correction, reject_and_learn, escalate, apply_target, request_type_for,
)


class FakeStore:
    """ApprovalStore 인터페이스 in-memory 구현 (원자 open_pending / commit_decision)."""

    def __init__(self):
        self.records = {}        # incident_id -> 본선(정비/보정) row
        self.dispo_records = {}  # incident_id -> 처분 row — 클래스별 PENDING 공존 (헌법 1-4 예외)
        self.lifecycle = {}    # incident_id -> lifecycle
        self.reopen = {}       # incident_id -> reopen_count
        self.dispo = {}        # (incident_id, wafer_id) -> 처분 행 dict (잠정→확정 전이 시뮬)

    def set_lifecycle(self, incident_id, lifecycle):
        self.lifecycle[incident_id] = lifecycle

    def seed_provisional(self, incident_id, wafer_id):
        """그루퍼 B9 잠정 HOLD 행 선행 시뮬 — recommendation_basis·decided_*는 비움."""
        self.dispo[(incident_id, wafer_id)] = {
            "status": "HOLD", "system_recommendation": "SCRAP",
            "hold_reason": "B9 crazy 자동 격리(잠정)", "recommendation_basis": None,
            "decided_by": None}

    def finalize_dispositions(self, incident_id, per_wafer, approver, approver_role):
        n = 0
        for it in per_wafer:
            wid, rec = it.get("wafer_id"), it.get("recommendation")
            if not wid or rec not in ("SCRAP", "RELEASE", "HOLD"):
                continue
            row = self.dispo.get((incident_id, wid), {"hold_reason": None})   # UPDATE or INSERT
            row.update(status=rec, system_recommendation=rec,
                       recommendation_basis=it.get("reason"), decided_by=approver)
            self.dispo[(incident_id, wid)] = row
            n += 1
        return n

    def _bucket(self, is_dispo):
        """PENDING 을 **클래스별로** 나눠 센다 (헌법 1-4 예외, 2026-08-11 분리).

        운영 store 는 `AND (request_type = 'disposition') = %s` 로 조준하는데, 대역이
        incident_id 하나로만 세면 처분 카드가 본선 카드를 덮어써 그 예외가 통째로
        검증에서 빠진다 — 같은 축으로 갈라 둔다.
        """
        return self.dispo_records if is_dispo else self.records

    def open_pending(self, incident_id, request_type, selected_option, original_value):
        bucket = self._bucket(request_type == "disposition")
        rec = bucket.get(incident_id)
        if rec and rec["status"] == "PENDING":
            return False
        bucket[incident_id] = {"status": "PENDING", "request_type": request_type,
                               "selected_option": selected_option,
                               "original_value": original_value}
        if request_type != "disposition":
            self.lifecycle[incident_id] = "pending"   # 처분은 본선 lifecycle 을 건드리지 않는다
        return True

    def commit_decision(self, incident_id, action, modified_value, reason, approver,
                        approver_role, new_lifecycle=None, bump_reopen=False,
                        request_class="main"):
        rec = self._bucket(request_class == "disposition").get(incident_id)
        if not rec or rec["status"] != "PENDING":
            return False
        rec.update(status=ACTION_STATUS.get(action, "REJECTED"), modified_value=modified_value,
                   reason=reason, approver=approver, approver_role=approver_role)
        if new_lifecycle:
            self.lifecycle[incident_id] = new_lifecycle
        if bump_reopen:
            self.reopen[incident_id] = self.reopen.get(incident_id, 0) + 1
        return True


def _emit_into(events):
    def _emit(event_type, payload):
        events.append((event_type, payload))
    return _emit


def _report(option="limit_option"):
    return {"selected_option": option, "request_type": request_type_for(option)}


def test_merge_sets_analyzing():
    s = FakeStore()
    out = merge_reports({"incident_id": "INC-1", "report": _report()}, s)
    assert s.lifecycle["INC-1"] == "analyzing"
    assert out["report"]["selected_option"] == "limit_option"


def test_gate_ensure_idempotent():
    """최초엔 [PENDING+pending] 원자 생성 + ApprovalRequested, 재실행엔 중복 없음."""
    s, ev = FakeStore(), []
    st = {"incident_id": "INC-1", "report": _report()}
    assert gate_ensure(st, s, _emit_into(ev)) is True
    assert s.records["INC-1"]["status"] == "PENDING"
    assert s.lifecycle["INC-1"] == "pending"
    assert gate_ensure(st, s, _emit_into(ev)) is False          # interrupt 노드 재실행 멱등
    assert sum(1 for e in ev if e[0] == "ApprovalRequested") == 1


def test_apply_approve_full_and_idempotent():
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-2"
    st = {"incident_id": inc, "report": _report("recipe_option")}
    gate_ensure(st, s, emit)
    st2 = {**st, **gate_parse(st, {"action": "approve", "approver": "eng1", "approver_role": "process"})}
    out = apply_correction(st2, s, emit)
    assert s.records[inc]["status"] == "APPROVED" and s.records[inc]["approver"] == "eng1"
    assert s.lifecycle[inc] == "verifying"   # P6-1 — 발행 직후 actioned→verifying (관측 창 시작)
    assert out["result"]["correction_type"] == "recipe" and out["result"]["consumer"] == "simulator"
    types = [e[0] for e in ev]
    assert "CorrectionApplied" in types and "ApprovalCompleted" in types
    out2 = apply_correction(st2, s, emit)                       # 중복 승인 클릭 → 멱등 스킵
    assert out2["result"].get("skipped")
    assert s.lifecycle[inc] == "verifying"   # 멱등 재호출이 lifecycle을 되돌리지 않음


def test_apply_manual_option_is_maintenance_not_disposition():
    """manual_option = **정비** 카드 — 승인해도 웨이퍼 처분은 확정되지 않는다 (2026-08-11 분리).

    구 매핑은 manual 을 'disposition' 으로 적어서 정비 승인 한 번이 웨이퍼 확정까지 끌고
    갔다(승인 1회 = 결정 2개, S4 가 정비 근거 옆에 RELEASE/SCRAP 토글을 띄우는 기형).
    이 테스트가 그 회귀를 막는다 — 원장은 `disposition_option` 전용 카드만 움직인다.
    """
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-2b"
    assert request_type_for("manual_option") == "maintenance"
    st = {"incident_id": inc, "report": _report("manual_option")}
    gate_ensure(st, s, emit)
    assert s.records[inc]["request_type"] == "maintenance"
    assert inc not in s.dispo_records                  # 정비 카드는 처분 클래스를 열지 않는다
    st2 = {**st, **gate_parse(st, {"action": "approve", "approver": "eng9"})}
    apply_correction(st2, s, emit)
    assert s.records[inc]["status"] == "APPROVED"
    assert s.dispo == {}                               # ← 분리의 핵심: 처분 원장 무변화
    assert s.lifecycle[inc] == "verifying"   # P6-1 — 정비 발행도 관측 창 진입 (sweep이 종결)
    # 2026-08-12 개명 완료 (TSR-0006 미해결 2 해소) — 정비 승인은 MaintenanceApproved(fdc.agent).
    #   WaferDispositioned 는 처분 확정(apply_disposition) 전용 — 정비 경로에서 다시 나가면 회귀다.
    ev_types = [e[0] for e in ev]
    assert "MaintenanceApproved" in ev_types
    assert "WaferDispositioned" not in ev_types


def test_pending_classes_coexist():
    """헌법 1-4 예외 — 본선(정비) PENDING 과 처분 PENDING 은 같은 Incident 에서 공존한다.

    #175 의 핵심인데 대역이 incident_id 하나로만 세는 바람에 검증에서 빠져 있었다.
    """
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-2e"
    main = {"incident_id": inc, "report": _report("manual_option")}
    dispo = {"incident_id": inc, "report": _dispo_report_per_wafer()}
    assert gate_ensure(main, s, emit) is True
    assert gate_ensure(dispo, s, emit) is True         # 처분이 본선에 막히지 않는다
    assert gate_ensure(dispo, s, emit) is False        # 단, 클래스 안에서는 여전히 1건 (멱등)
    assert s.records[inc]["status"] == "PENDING" and s.dispo_records[inc]["status"] == "PENDING"
    st2 = {**dispo, **gate_parse(dispo, {"action": "approve", "approver": "eng9"})}
    apply_correction(st2, s, emit)                     # is_disposition_state → apply_disposition
    assert s.dispo_records[inc]["status"] == "APPROVED"
    assert s.records[inc]["status"] == "PENDING"        # ← 본선은 그대로 (구 결함: 한 클릭이 둘을 닫았다)
    assert s.lifecycle.get(inc) == "pending"            # 처분은 본선 lifecycle 을 건드리지 않는다


def _dispo_report_per_wafer():
    """per_wafer 담은 **처분 전용 카드** (계약 §6) — 2장 중 1장 SCRAP 케이스.

    2026-08-11 분리 전에는 manual(정비) Brief 가 이 목록을 실어 날랐다. 지금은 S6 가
    `disposition_option` 으로 발원하고 `apply_disposition` 한 경로만 원장을 움직인다.
    """
    r = _report("disposition_option")
    r["wafer_disposition"] = {
        "held_wafers": ["C64_995", "C64_1001"], "recommendation": "SCRAP",
        "reason": "HOLD 2장 중 1장 초과",
        "per_wafer": [
            {"wafer_id": "C64_995", "recommendation": "RELEASE",
             "reason": "predicted_c65 701.3 ≤ B9 1572 — 근거 없음"},
            {"wafer_id": "C64_1001", "recommendation": "SCRAP",
             "reason": "predicted_c65 1650 > B9 1572 · anomaly 0.83 — 이중 확인(1장 spot)"}]}
    return r


def test_per_wafer_finalize_promotes_provisional():
    """P6-1 커밋2 — B9 잠정 HOLD 행이 있으면 승인 시 UPDATE(잠정→확정), basis·decided 채움."""
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-2c"
    s.seed_provisional(inc, "C64_1001")                        # 그루퍼가 미리 만든 잠정 HOLD
    st = {"incident_id": inc, "report": _dispo_report_per_wafer()}
    gate_ensure(st, s, emit)
    assert s.dispo_records[inc]["request_type"] == "disposition"
    st2 = {**st, **gate_parse(st, {"action": "approve", "approver": "eng9"})}
    apply_correction(st2, s, emit)                              # → apply_disposition 위임
    scrap = s.dispo[(inc, "C64_1001")]
    assert scrap["status"] == "SCRAP" and scrap["decided_by"] == "eng9"
    assert scrap["recommendation_basis"] and "1650" in scrap["recommendation_basis"]  # 확정 basis 채워짐
    assert scrap["hold_reason"] == "B9 crazy 자동 격리(잠정)"    # 잠정 사유 보존(경쟁 안 함)
    rel = s.dispo[(inc, "C64_995")]                            # 잠정 없던 wafer → INSERT
    assert rel["status"] == "RELEASE" and rel["decided_by"] == "eng9"


def test_per_wafer_finalize_insert_when_no_provisional():
    """비-B9 경로(잠정 행 없음) — per_wafer가 확정 상태로 INSERT (2장 전부 신규)."""
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-2d"
    st = {"incident_id": inc, "report": _dispo_report_per_wafer()}
    gate_ensure(st, s, emit)
    st2 = {**st, **gate_parse(st, {"action": "approve", "approver": "eng1"})}
    apply_correction(st2, s, emit)                              # → apply_disposition 위임
    assert len(s.dispo) == 2                                    # 2장 전부 생성
    assert {s.dispo[(inc, w)]["status"] for w in ("C64_995", "C64_1001")} == {"RELEASE", "SCRAP"}


def test_modify_records_value():
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-3"
    st = {"incident_id": inc, "report": _report("limit_option")}
    gate_ensure(st, s, emit)
    st2 = {**st, **gate_parse(st, {"action": "modify", "modified_value": -297.3, "approver": "eng2"})}
    apply_correction(st2, s, emit)
    assert s.records[inc]["status"] == "MODIFIED"
    assert s.records[inc]["modified_value"] == -297.3          # Modify-and-Approve 값 기록


def test_reject_reanalyze_or_close():
    """reject 거취 = 엔지니어 선택: reanalyze True→analyzing / False→closed (자동 루프가드 없음)."""
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-4"
    st = {"incident_id": inc, "report": _report()}
    gate_ensure(st, s, emit)
    st2 = {**st, **gate_parse(st, {"action": "reject", "reason": "오진단", "approver": "eng3", "reanalyze": True})}
    out = reject_and_learn(st2, s, emit)
    assert s.records[inc]["status"] == "REJECTED" and out["result"]["rejected"]
    assert s.lifecycle[inc] == "analyzing"                    # 재분석 요청
    inc2 = "INC-4b"
    st = {"incident_id": inc2, "report": _report()}
    gate_ensure(st, s, emit)
    st2 = {**st, **gate_parse(st, {"action": "reject", "reason": "오탐", "approver": "eng3", "reanalyze": False})}
    reject_and_learn(st2, s, emit)
    assert s.lifecycle[inc2] == "closed"                      # 종료 (오탐/조치 불필요)
    assert "RejectionLearned" in [e[0] for e in ev]


def test_escalate_reopens_and_bumps_count():
    s, ev = FakeStore(), []
    emit = _emit_into(ev)
    inc = "INC-5"
    st = {"incident_id": inc, "report": _report()}
    gate_ensure(st, s, emit)
    st2 = {**st, **gate_parse(st, {"action": "escalate", "approver": "eng4"})}
    out = escalate(st2, s, emit)
    assert s.records[inc]["status"] == "ESCALATED" and out["result"]["escalated"]
    assert s.lifecycle[inc] == "reopened"                     # 기획서 line 673: Reopen + HOLD
    assert s.reopen[inc] == 1                                 # reopen_count++
    assert "Escalated" in [e[0] for e in ev]


def test_unknown_action_fail_closed():
    """미지 action → gate_parse가 그대로 통과(경고)하고 route는 반려로 fail-closed."""
    from orchestrator.approval_graph import route_decision
    p = gate_parse({}, {"action": "bogus"})
    assert route_decision(p["decision"]) == "reject_and_learn"


def test_apply_target_polymorphism():
    assert apply_target("limit_option")["consumer"] == "B"
    assert apply_target("recipe_option")["consumer"] == "simulator"
    assert apply_target("manual_option")["event"] == "MaintenanceApproved"
    assert apply_target("manual_option")["table"] is None   # 정비는 원장 직접 쓰기 없음 (처분 분리)
    assert "적용 없음" in apply_target("unknown")["note"]


def test_start_incident_invokes_graph():
    """트리거: start_incident가 initial_state + thread_id로 그래프를 invoke."""
    from orchestrator.approval_graph import start_incident
    calls = []

    class _App:
        def invoke(self, state, config):
            calls.append((state, config))

    start_incident(_App(), "INC-9", {"selected_option": "limit_option"})
    (state, cfg), = calls
    assert state["incident_id"] == "INC-9"
    assert cfg["configurable"]["thread_id"] == "INC-9"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"ALL {len(tests)} TESTS PASS")
