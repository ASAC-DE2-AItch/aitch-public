# -*- coding: utf-8 -*-
"""WP-C 배포 후 감시·자동 롤백 테스트 (헌법 3-3 ③ ⓔ·개통 조건 ②).

검증:
  감시① 교차 침묵 — 정상(무발화) / 미탐(SPC↑·AE 침묵) 발화 / 표본 부족 유예
  감시② 분포 붕괴 — 정상 / P95 급락 / 전 0 근접(floor)
  evaluate — 창 미충전 유예 / 종합 판정
  롤백 — 대상 해석 · 신호 형식(promote_*.json glob 매칭·절대경로) · 3중 기록
  ★Scorecard 프로브 — 정상 배포(SPC↑인데 AE도 잡음) = 미탐 0건 무발화 / 실명 배포 = 발화
  임계 로드 + params 등재

실행: python -m pytest tests/test_ct2_deploy_monitor.py -q
"""

import fnmatch
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
import numpy as np  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops"))

import ct2_deploy_monitor as M  # noqa: E402

QUAL = 0.2


def _params(**over):
    p = dict(M.WATCH_DEFAULTS)
    for k, v in over.items():
        p[k] = float(v) if isinstance(v, (int, float)) and k != "enabled" else v
    return p


# ── 감시① 교차 침묵 ─────────────────────────────────────────────────────────

def test_cross_silence_healthy_no_fire():
    """SPC가 잡은 wafer를 AE도 잡으면(ae_score ≥ qual) 미탐 아님 → 무발화."""
    n = 200
    spc = [False] * n
    ae = [0.05] * n
    for i in range(20):                       # SPC 위반 20건 — AE도 전부 잡음(0.5)
        spc[i] = True
        ae[i] = 0.5
    fired, d = M.cross_silence(ae, spc, QUAL, _params())
    assert not fired and d["silent_rate"] == 0.0 and d["n_spc"] == 20


def test_cross_silence_blind_fires():
    """SPC가 잡은 wafer에서 AE가 침묵(ae<qual)하면 미탐 발화."""
    n = 200
    spc = [False] * n
    ae = [0.05] * n
    for i in range(20):                       # SPC 위반 20건 — AE는 전부 침묵(0.05)
        spc[i] = True                          # ae는 0.05 그대로 (< qual)
    fired, d = M.cross_silence(ae, spc, QUAL, _params())
    assert fired and d["silent_rate"] == 1.0


def test_cross_silence_defers_on_small_sample():
    """SPC 위반이 min_events 미만이면 판정 유예(무발화) — 표본 부족 오발화 방지."""
    n = 200
    spc = [False] * n
    ae = [0.05] * n
    for i in range(3):                        # 3건 < min_events(10)
        spc[i] = True
    fired, d = M.cross_silence(ae, spc, QUAL, _params())
    assert not fired and d["silent_rate"] is None and "유예" in d["note"]


def test_cross_silence_length_mismatch_raises():
    with pytest.raises(ValueError, match="길이 불일치"):
        M.cross_silence([0.1, 0.2], [True], QUAL, _params())


# ── 감시② 분포 붕괴 ─────────────────────────────────────────────────────────

def test_distribution_healthy_no_fire():
    """배포 후 분포가 배포 전과 비슷하면 무발화."""
    rng = np.random.default_rng(0)
    cur = np.clip(rng.normal(0.06, 0.03, 300), 0, 1)
    fired, d = M.distribution_collapse(cur, baseline_p95=0.18, params=_params())
    assert not fired, d


def test_distribution_collapse_on_p95_drop():
    """배포 후 P95가 배포 전 대비 급락하면 발화 (AE 침묵화)."""
    cur = np.full(300, 0.03)                  # 전부 0.03 → P95 ≈ 0.03
    fired, d = M.distribution_collapse(cur, baseline_p95=0.18, params=_params())
    assert fired and d["drop_fired"]          # 0.03 < 0.18 × 0.3 = 0.054


def test_distribution_collapse_on_floor():
    """baseline 없어도 전 0 근접이면 floor로 발화."""
    cur = np.full(300, 0.005)
    fired, d = M.distribution_collapse(cur, baseline_p95=None, params=_params())
    assert fired and d["floor_fired"] and not d["drop_fired"]


# ── evaluate 종합 ───────────────────────────────────────────────────────────

def test_evaluate_defers_when_window_unfilled():
    ae = [0.05] * 50                          # 50 < min_wafers(100)
    v = M.evaluate(ae, [False] * 50, QUAL, 0.18, _params())
    assert v["rollback"] is False and v["deferred"] is True


def test_evaluate_healthy_no_rollback():
    rng = np.random.default_rng(1)
    ae = list(np.clip(rng.normal(0.06, 0.03, 200), 0, 1))
    spc = [False] * 200
    v = M.evaluate(ae, spc, QUAL, 0.18, _params())
    assert v["rollback"] is False and not v["deferred"]


def test_evaluate_rollback_on_missed_detection():
    ae = [0.05] * 200
    spc = [False] * 200
    for i in range(20):
        spc[i] = True                          # SPC 20건, AE 전부 침묵
    v = M.evaluate(ae, spc, QUAL, 0.18, _params())
    assert v["rollback"] is True and "AE침묵" in v["reason"]


# ── ★Scorecard 프로브 (미탐 0건 구조) ───────────────────────────────────────

def test_scorecard_probe_healthy_deploy_no_false_alarm():
    """정상 배포(SPC↑ wafer를 AE도 함께 잡음) → 미탐 0건 = 무발화.

    개통 조건 ②의 '프로브 미탐 0건'을 단위 수준으로 고정한다: 감시가 정상 배포를 롤백
    시키지 않아야 한다(오발화 0). 실이상(주입)에서 AE가 함께 반응하는 시나리오.
    """
    rng = np.random.default_rng(7)
    ae = list(np.clip(rng.normal(0.06, 0.02, 200), 0, 1))
    spc = [False] * 200
    for i in range(15):                        # 실이상 15건 — AE도 크게 반응(0.6)
        spc[i] = True
        ae[i] = 0.6
    v = M.evaluate(ae, spc, QUAL, 0.18, _params())
    assert v["rollback"] is False, f"정상 배포를 롤백시킴(오발화): {v['reason']}"


def test_scorecard_probe_blind_deploy_fires():
    """실명 배포(SPC↑인데 AE 침묵) → 발화 = 롤백. 감시가 '막을 수 있음'의 증명."""
    ae = [0.04] * 200
    spc = [False] * 200
    for i in range(15):
        spc[i] = True                          # 실이상 15건 — AE 침묵(0.04)
    v = M.evaluate(ae, spc, QUAL, 0.18, _params())
    assert v["rollback"] is True


# ── 롤백 실행 (DB·파일 스텁) ────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._fetch = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT model_version_after FROM ct_decisions"):
            if "WHERE ct_id=%s" in s:                        # _current_bundle_for (현 배포분)
                cur = self.store.get("current_bundle")
                self._fetch = (cur,) if cur else None
            else:                                            # resolve_rollback_target (직전 정상)
                tgt = self.store.get("rollback_target")
                self._fetch = (tgt,) if tgt else None
        elif s.startswith("UPDATE ct_decisions"):
            self.store.setdefault("ct_updates", []).append(params)
        elif s.startswith("INSERT INTO approval_records"):
            self.store.setdefault("appr_inserts", []).append(params)
            self.store.setdefault("appr_status", []).append("ESCALATED" if "ESCALATED" in s
                                                             else "APPROVED")
        else:
            raise AssertionError(f"예상치 못한 SQL: {s[:60]}")

    def fetchone(self):
        return self._fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rollback_target=None, current_bundle=None):
        self.store = {"rollback_target": rollback_target, "current_bundle": current_bundle}
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.commits += 1


def test_resolve_rollback_target():
    conn = FakeConn(rollback_target="ae_ct2_SIM_CH_1_20260101_000000")
    assert M.resolve_rollback_target(conn, exclude_bundle="ae_ct2_new") == "ae_ct2_SIM_CH_1_20260101_000000"
    assert M.resolve_rollback_target(FakeConn(rollback_target=None), exclude_bundle="x") is None


def test_trigger_rollback_escalates_when_current_unknown(tmp_path, monkeypatch):
    """현 배포 번들을 특정 못하면(--current-bundle 없고 ct_decisions에도 없음) 자기 롤백을
    피해 에스컬레이션 — 나쁜 번들 자신으로 리로드하는 사고를 막는다 (★리뷰 지적)."""
    monkeypatch.setattr(M, "CONTROL_DIR", tmp_path / "c")
    monkeypatch.setattr(M, "REPO_ROOT", tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "CHANGELOG.md").write_text("x", encoding="utf-8")
    conn = FakeConn(rollback_target="ae_ct2_bad", current_bundle=None)   # 현 배포분 미상
    result = M.trigger_rollback(conn, "INC-1", {"rollback": True, "reason": "붕괴"})
    assert result["action"] == "escalated" and "미상" in result["reason"]
    assert not (tmp_path / "c").exists(), "현 배포분 미상인데 신호를 드롭했다(자기 롤백 위험)"


def test_trigger_rollback_escalates_when_target_is_self(tmp_path, monkeypatch):
    """롤백 대상이 현 배포분과 동일하면(직전 승격분 부재) 에스컬레이션 — 자기 롤백 방지."""
    monkeypatch.setattr(M, "CONTROL_DIR", tmp_path / "c")
    monkeypatch.setattr(M, "REPO_ROOT", tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "CHANGELOG.md").write_text("x", encoding="utf-8")
    # 현 배포 = rollback_target 과 같은 이름 → resolve가 자기 자신을 고름
    conn = FakeConn(rollback_target="ae_ct2_same", current_bundle="ae_ct2_same")
    result = M.trigger_rollback(conn, "INC-1", {"rollback": True, "reason": "붕괴"})
    assert result["action"] == "escalated"
    assert not (tmp_path / "c").exists()


def test_drop_rollback_signal_matches_glob_and_is_absolute(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "CONTROL_DIR", tmp_path / "control" / "ct2")
    sig = M.drop_rollback_signal("INC-20260730-SIMCH1-001", str(tmp_path / "models" / "b"))
    assert fnmatch.fnmatch(sig.name, "promote_*.json"), "consumer glob과 불일치"
    payload = json.loads(sig.read_text(encoding="utf-8"))
    assert payload["rollback"] is True
    assert Path(payload["bundle"]).is_absolute(), "consumer cwd 무관 절대경로여야 한다"
    assert not (sig.parent / (sig.name + ".tmp")).exists(), "tmp 잔류 (원자 교체 실패)"


def test_trigger_rollback_full_path(tmp_path, monkeypatch):
    """대상 존재 시: 신호 드롭 + ct_decisions ROLLED_BACK + approval_records 감사 행."""
    monkeypatch.setattr(M, "CONTROL_DIR", tmp_path / "control" / "ct2")
    monkeypatch.setattr(M, "BUNDLE_ROOT", tmp_path / "models" / "anomaly_ae")
    monkeypatch.setattr(M, "REPO_ROOT", tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "CHANGELOG.md").write_text("# CHANGELOG\n", encoding="utf-8")
    target = "ae_ct2_SIM_CH_1_20260101_000000"
    (tmp_path / "models" / "anomaly_ae" / target).mkdir(parents=True)
    conn = FakeConn(rollback_target=target)

    verdict = {"rollback": True, "reason": "SPC↑/AE침묵 1.0 ≥ 0.5"}
    result = M.trigger_rollback(conn, "INC-20260730-SIMCH1-001", verdict, current_bundle="ae_ct2_new")
    assert result["action"] == "rolled_back" and result["to_bundle"] == target
    assert conn.store["appr_inserts"], "감사 행 미기록 (approval_records)"
    assert any(M.ROLLBACK_APPROVER in p for p in conn.store["appr_inserts"]), "approver 누락"
    upd = conn.store["ct_updates"][0]
    assert target in upd                              # model_version_before = 롤백 대상
    assert "ROLLED_BACK" in (tmp_path / "models" / "CHANGELOG.md").read_text(encoding="utf-8")


def test_trigger_rollback_escalates_without_target(tmp_path, monkeypatch):
    """롤백 대상이 없으면 신호를 드롭하지 않고 에스컬레이션만 (서빙 보호)."""
    monkeypatch.setattr(M, "CONTROL_DIR", tmp_path / "c")
    monkeypatch.setattr(M, "REPO_ROOT", tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "CHANGELOG.md").write_text("x", encoding="utf-8")
    conn = FakeConn(rollback_target=None)
    result = M.trigger_rollback(conn, "INC-1", {"rollback": True, "reason": "붕괴"})
    assert result["action"] == "escalated"
    assert not (tmp_path / "c").exists(), "대상 없는데 신호를 드롭했다"
    # ★ct_decisions를 ROLLED_BACK으로 바꾸지 않는다 — 나쁜 번들이 여전히 서빙 중이라 부정확
    assert "ct_updates" not in conn.store, "롤백 못했는데 ROLLED_BACK으로 표기했다"
    assert conn.store["appr_status"] == ["ESCALATED"], "에스컬레이션은 ESCALATED로 기록"


def test_trigger_rollback_noop_when_not_fired():
    assert M.trigger_rollback(FakeConn(), "INC-1", {"rollback": False})["action"] == "none"


# ── 임계 로드 + params 등재 ─────────────────────────────────────────────────

def test_watch_params_present_in_repo_config():
    import yaml
    ct = yaml.safe_load((REPO_ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))["ct"]
    for key in M.WATCH_DEFAULTS:
        assert f"ct2_watch_{key}" in ct, f"params.yaml ct.ct2_watch_{key} 미등재"
    assert ct["ct2_watch_enabled"] is False, "감시 스위치는 실증 전 false"
    assert ct["ct2_watch_auto_rollback"] is False, "자동 롤백 스위치는 실증 전 false"


def test_watch_params_coerce_and_default(tmp_path):
    y = tmp_path / "params.yaml"
    y.write_text('ct:\n  ct2_watch_cross_silence_max: "0.7"\n', encoding="utf-8")
    p = M.load_watch_params(y)
    assert p["cross_silence_max"] == 0.7 and isinstance(p["cross_silence_max"], float)
    assert p["window_wafers"] == float(M.WATCH_DEFAULTS["window_wafers"])   # 미기재 키 기본값


def test_rollback_request_type_fits_column():
    """approval_records.request_type VARCHAR(32) — 'ct2_rollback' 이내."""
    assert len("ct2_rollback") <= 32
    assert len("ROLLED_BACK") <= 16                  # ct_decisions.retrain_status


# ── PM 리뷰 대응 — 0 분모 방어 (헌법 7장 "임계를 검증 없이 사용") ───────────────
def test_watch_params_caps_zero_sample_floors(tmp_path, caplog):
    """★표본 하한 0 은 감시를 **조용히 죽인다** — 로드 시점 캡 + 경고.

    `cross_silence_min_events: 0` 이면 SPC 발화 0건일 때 표본 부족 가드를 지나쳐
    `silent / n_spc` 가 ZeroDivisionError 를 내고, 판정이 아니라 실행 오류(exit 1)로
    끝난다 — 사람은 감시가 도는 줄 안다.
    """
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_watch_cross_silence_min_events: 0\n"
                 "  ct2_watch_min_wafers: 0\n  ct2_watch_window_wafers: 0\n",
                 encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = M.load_watch_params(y)
    assert p["cross_silence_min_events"] == 1.0
    assert p["min_wafers"] == 1.0 and p["window_wafers"] == 1.0
    assert sum("감시 무력화" in r.message for r in caplog.records) == 3


def test_watch_params_rejects_nonpositive_silence_max(tmp_path, caplog):
    """`cross_silence_max ≤ 0` 이면 SPC 발화가 있는 모든 창이 미탐 판정 — 기본값 복귀."""
    y = tmp_path / "params.yaml"
    y.write_text("ct:\n  ct2_watch_cross_silence_max: 0\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        p = M.load_watch_params(y)
    assert p["cross_silence_max"] == float(M.WATCH_DEFAULTS["cross_silence_max"])
    assert any("오발화 위험" in r.message for r in caplog.records)


def test_cross_silence_no_zero_division_when_min_events_zero():
    """캡을 우회해 params 를 직접 넘겨도 0 분모가 성립하지 않는다 (2층 방어)."""
    raw = dict(M.WATCH_DEFAULTS)
    raw["cross_silence_min_events"] = 0
    fired, detail = M.cross_silence([0.01] * 50, [False] * 50, QUAL, raw)
    assert fired is False and detail["n_spc"] == 0 and detail["silent_rate"] is None


if __name__ == "__main__":                           # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
