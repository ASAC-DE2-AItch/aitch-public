# -*- coding: utf-8 -*-
"""P4-3 검증 — incident_grouper 그룹핑 로직 단위테스트 (Fake DB).

실 Postgres 없이 handle_alert의 결정 로직(병합/신규/idempotency/집계)을 검증한다.
전체 SQL 정합성은 docker-compose 통합테스트(CI)에서 별도 확인.
실행: python tests/test_incident_grouper.py   (또는 pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orchestrator.incident_grouper import IncidentGrouper  # noqa: E402


class FakeCursor:
    """grouper가 실제로 실행하는 SQL만 패턴 매칭해 in-memory dict로 응답하는 테스트 스텁."""

    def __init__(self, db):
        self.db = db
        self._result = []
        self.rowcount = 0                                  # P6-1 — reopen 전이 가드 반환용

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        db = self.db
        if "FROM incident_alerts WHERE alert_id" in s:
            aid = params[0]
            self._result = [(1,)] if any(a["alert_id"] == aid for a in db["alerts"]) else []
        elif "FROM incidents i JOIN incident_alerts" in s:
            chamber = params[0]
            best = None
            for inc in db["incidents"]:
                if inc["chamber_id"] != chamber or inc["lifecycle"] == "closed":
                    continue
                ts = [a["alert_ts"] for a in db["alerts"] if a["incident_id"] == inc["incident_id"]]
                if not ts:
                    continue
                last = max(ts)
                if best is None or last > best[1]:
                    best = (inc["incident_id"], last)
            self._result = [best] if best else []
        elif "SELECT incident_id FROM incidents WHERE incident_id LIKE" in s:
            prefix = params[0][:-1]  # 끝의 % 제거
            self._result = [(inc["incident_id"],) for inc in db["incidents"]
                            if inc["incident_id"].startswith(prefix)]
        elif s.startswith("INSERT INTO incidents"):
            db["incidents"].append({                       # P6-1 — incident_type 8-param
                "incident_id": params[0], "chamber_id": params[1], "lifecycle": params[2],
                "incident_type": params[3], "severity_max": params[4],
                "priority_score": params[5], "suspect_window_start": params[6],
                "suspect_window_end": params[7], "reopen_count": 0})
            self._result = []
        elif s.startswith("INSERT INTO incident_alerts"):
            aid = params[1]
            if not any(a["alert_id"] == aid for a in db["alerts"]):
                db["alerts"].append({
                    "incident_id": params[0], "alert_id": aid, "chamber_id": params[2],
                    "severity": params[3], "context_score": params[4], "alert_ts": params[5],
                    "suspect_start": params[6], "suspect_end": params[7]})
            self._result = []
        elif "SELECT severity_max, priority_score FROM incidents" in s:
            inc = next(i for i in db["incidents"] if i["incident_id"] == params[0])
            self._result = [(inc["severity_max"], inc["priority_score"])]
        elif "SET lifecycle = 'reopened'" in s:            # P6-1 — verifying 재발 전이 (가드 포함)
            inc = next(i for i in db["incidents"] if i["incident_id"] == params[0])
            if inc["lifecycle"] == "verifying":
                inc["lifecycle"] = "reopened"
                inc["reopen_count"] = inc.get("reopen_count", 0) + 1
                self.rowcount = 1
            else:
                self.rowcount = 0
            self._result = []
        elif "SELECT 1 FROM wafer_dispositions" in s:      # P6-1 — HOLD 멱등 체크
            wid, iid = params
            self._result = [(1,)] if any(
                d["wafer_id"] == wid and d["incident_id"] == iid
                for d in db["dispositions"]) else []
        elif s.startswith("INSERT INTO wafer_dispositions"):   # P6-1 커밋2 — B9 잠정 HOLD 행
            db["dispositions"].append({                        # 잠정: hold_reason만·basis/decided 비움
                "wafer_id": params[0], "lot_id": params[1], "chamber_id": params[2],
                "incident_id": params[3], "status": "HOLD", "hold_reason": params[4],
                "system_recommendation": "SCRAP",
                "recommendation_basis": None, "decided_by": None})
            self._result = []
        elif s.startswith("UPDATE incidents SET"):
            sev, pri, end_wafer, iid = params
            inc = next(i for i in db["incidents"] if i["incident_id"] == iid)
            inc["severity_max"], inc["priority_score"] = sev, pri
            if end_wafer is not None:
                inc["suspect_window_end"] = end_wafer
            self._result = []
        else:
            raise AssertionError(f"미지원 SQL: {s[:70]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    """psycopg2 connection 인터페이스 최소 구현 (with/cursor/rollback)."""

    def __init__(self):
        self.db = {"incidents": [], "alerts": [], "dispositions": []}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self.db)

    def rollback(self):
        pass

    def close(self):
        pass


def _alert(aid, chamber, ts, sev, ctx, sw_start="C64_1", sw_end="C64_9"):
    """계약 §3 최소 alert payload."""
    return {"alert_id": aid, "chamber_id": chamber, "timestamp": ts,
            "violations": [{"severity": sev}], "context_score": ctx,
            "suspect_window": {"start_wafer": sw_start, "end_wafer": sw_end}}


def _grouper():
    conn = FakeConn()
    return conn, IncidentGrouper(conn, merge_gap_sec=300, initial_lifecycle="open", agent_min=31)


def test_new_then_merge_then_split():
    """같은 챔버: 근접(2분)→병합·severity 상향, 초과(4h)→신규 채번."""
    conn, g = _grouper()
    id1 = g.handle_alert(_alert("ALERT-1", "SIM_CH_3", "2026-07-13T10:05:00.000Z", "WARNING", 60))
    assert id1 == "INC-20260713-SIMCH3-001"
    id2 = g.handle_alert(_alert("ALERT-2", "SIM_CH_3", "2026-07-13T10:07:00.000Z", "CRITICAL", 80))
    assert id2 == id1                                    # 병합
    assert len(conn.db["incidents"]) == 1
    assert len(conn.db["alerts"]) == 2
    inc = conn.db["incidents"][0]
    assert inc["severity_max"] == "CRITICAL"             # 상향
    assert inc["priority_score"] == 2080.0               # CRITICAL(2)*1000 + 80
    id3 = g.handle_alert(_alert("ALERT-3", "SIM_CH_3", "2026-07-13T14:00:00.000Z", "WARNING", 55))
    assert id3 == "INC-20260713-SIMCH3-002"              # gap 초과 → 신규
    assert len(conn.db["incidents"]) == 2


def test_chamber_separation():
    """다른 챔버는 시간 근접해도 분리."""
    conn, g = _grouper()
    g.handle_alert(_alert("A-CH3", "SIM_CH_3", "2026-07-13T10:05:00.000Z", "WARNING", 60))
    id_ch2 = g.handle_alert(_alert("A-CH2", "SIM_CH_2", "2026-07-13T10:06:00.000Z", "WARNING", 60))
    assert id_ch2 == "INC-20260713-SIMCH2-001"
    assert len(conn.db["incidents"]) == 2


def test_idempotent_duplicate():
    """같은 alert_id 재전달 → 스킵(None), 고아 incident 미생성."""
    conn, g = _grouper()
    g.handle_alert(_alert("DUP", "SIM_CH_3", "2026-07-13T10:05:00.000Z", "WARNING", 60))
    again = g.handle_alert(_alert("DUP", "SIM_CH_3", "2026-07-13T10:05:30.000Z", "WARNING", 60))
    assert again is None
    assert len(conn.db["alerts"]) == 1
    assert len(conn.db["incidents"]) == 1


def test_low_context_still_grouped():
    """context_score<31도 그룹핑·기록은 됨 (agent 트리거만 downstream gating)."""
    conn, g = _grouper()
    iid = g.handle_alert(_alert("LOW", "SIM_CH_4", "2026-07-13T10:05:00.000Z", "WARNING", 20))
    assert iid == "INC-20260713-SIMCH4-001"
    assert len(conn.db["alerts"]) == 1


# ===========================================================================
# P6-1 — B9 crazy 라우팅 · verifying 재발 전이 (2026-07-28)
# ===========================================================================
def _b9_alert(aid, chamber, ts, wafer_id, ctx=20):
    """B9 crazy 마커 alert — 계약 §3 합성 Violation (sensor='C65'는 종류 마커).

    wafer_id 는 **계약 위치인 `prediction_context` 안**에만 싣는다 — 실 B 페이로드와 동일
    (`src/common/context_score/publisher.py:312`). 구 픽스처는 최상위에 실어서, 조회 위치를
    계약대로 교정한 #89 이후 "탐지는 되는데 HOLD 가 안 생기는" 실사고를 테스트가 재현하지
    못했다 (되레 버그 형태를 정답으로 강제). 최상위 형태는 아래 계약 가드 테스트가 지킨다.
    """
    return {"alert_id": aid, "chamber_id": chamber, "timestamp": ts,
            "prediction_context": {"wafer_id": wafer_id},
            "context_score": ctx,
            "violations": [{"rule_id": "B9", "severity": "CRITICAL", "sensor": "C65",
                            "window": "settled", "current_value": 1650.0,
                            "control_limit_upper": 1572.0, "control_limit_lower": 0.0,
                            "description": "predicted_c65 arm 초과"}],
            "suspect_window": {"start_wafer": wafer_id, "end_wafer": wafer_id}}


def test_b9_spot_creates_crazy_incident_and_hold():
    """B9 스팟 1건 → 크기 1 incident(incident_type='crazy_spot') + HOLD 행 자동 생성.

    nullable 반려 설계(PM 2026-07-28): 스팟도 incident_id를 자연 확보 — 스키마 무변경.
    """
    conn, g = _grouper()
    iid = g.handle_alert(_b9_alert("B9-1", "SIM_CH_1", "2026-07-13T10:05:00.000Z", "C64_777"))
    inc = conn.db["incidents"][0]
    assert iid == inc["incident_id"] and inc["incident_type"] == "crazy_spot"
    assert len(conn.db["dispositions"]) == 1
    d = conn.db["dispositions"][0]
    assert (d["wafer_id"], d["incident_id"], d["status"]) == ("C64_777", iid, "HOLD")
    # 잠정 행: SCRAP 후보 + B9 근거는 hold_reason에 / basis·decided는 확정 몫이라 비어있음 (커밋2 전이 모델)
    assert d["system_recommendation"] == "SCRAP" and "B9" in d["hold_reason"]
    assert d["recommendation_basis"] is None and d["decided_by"] is None


def test_b9_hold_idempotent_same_wafer():
    """같은 wafer의 B9가 같은 incident에 재편입돼도 HOLD 행은 1개 (멱등)."""
    conn, g = _grouper()
    g.handle_alert(_b9_alert("B9-A", "SIM_CH_1", "2026-07-13T10:05:00.000Z", "C64_777"))
    g.handle_alert(_b9_alert("B9-B", "SIM_CH_1", "2026-07-13T10:06:00.000Z", "C64_777"))
    assert len(conn.db["incidents"]) == 1              # 병합 (연속 경로 자연 승격)
    assert len(conn.db["dispositions"]) == 1           # HOLD 중복 없음


def test_b9_merge_keeps_existing_type_but_holds():
    """일반 incident에 B9가 병합되면 type은 유지(선행 사건 성격 우선), HOLD 행은 생성."""
    conn, g = _grouper()
    g.handle_alert(_alert("N1", "SIM_CH_3", "2026-07-13T10:05:00.000Z", "WARNING", 60))
    g.handle_alert(_b9_alert("B9-2", "SIM_CH_3", "2026-07-13T10:06:00.000Z", "C64_888"))
    inc = conn.db["incidents"][0]
    assert inc["incident_type"] is None                # 기존 type 유지
    assert len(conn.db["dispositions"]) == 1           # 격리는 수행


def test_b9_top_level_wafer_id_ignored_no_hold():
    """계약 가드 (#89) — 최상위 wafer_id(계약 위반 형태)만 있으면 HOLD 를 만들지 않는다.

    실 B 페이로드는 wafer_id 를 prediction_context 에만 싣는다. 최상위 폴백을 허용하면
    잘못된 페이로드가 조용히 통과해 계약 위반이 은폐된다 — 무시가 정답이고, 이 테스트가
    그 결정을 고정한다 (§7-1 ⑤ 재발 방지의 반대편 절반).
    """
    conn, g = _grouper()
    bad = _b9_alert("B9-X", "SIM_CH_2", "2026-07-13T10:05:00.000Z", "C64_999")
    bad.pop("prediction_context")
    bad["wafer_id"] = "C64_999"                        # 옛(버그 시절) 최상위 형태
    g.handle_alert(bad)
    assert len(conn.db["incidents"]) == 1              # 사건화 자체는 정상
    assert len(conn.db["dispositions"]) == 0           # HOLD 없음 — 계약 위치만 읽는다


def test_verifying_realert_reopens():
    """verifying(관측 창) 중 같은 챔버 알람 재병합 → reopened + reopen_count++ (B6 재발)."""
    conn, g = _grouper()
    g.handle_alert(_alert("V-1", "SIM_CH_3", "2026-07-13T10:05:00.000Z", "WARNING", 60))
    conn.db["incidents"][0]["lifecycle"] = "verifying"   # 조치 발행 후 상태 재현
    iid = g.handle_alert(_alert("V-2", "SIM_CH_3", "2026-07-13T10:07:00.000Z", "WARNING", 55))
    inc = conn.db["incidents"][0]
    assert iid == inc["incident_id"]
    assert inc["lifecycle"] == "reopened" and inc["reopen_count"] == 1


def test_reopen_only_from_verifying():
    """verifying이 아닌 병합(analyzing 등)은 reopened로 바뀌지 않음 (가드)."""
    conn, g = _grouper()
    g.handle_alert(_alert("G-1", "SIM_CH_3", "2026-07-13T10:05:00.000Z", "WARNING", 60))
    conn.db["incidents"][0]["lifecycle"] = "analyzing"
    g.handle_alert(_alert("G-2", "SIM_CH_3", "2026-07-13T10:07:00.000Z", "WARNING", 55))
    assert conn.db["incidents"][0]["lifecycle"] == "analyzing"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"ALL {len(tests)} TESTS PASS")
