# -*- coding: utf-8 -*-
"""CT² 오케스트레이터 게이트 훅 테스트 — 배포 경로의 안전 속성 (헌법 3-3 ③ · 1-4).

검증하는 불변 조건:
  ① 게이트 FAIL(rc=2) → `BLOCKED` + **승인/promote 를 열지 않는다**
     (게이트 FAIL 상태 번들의 파생 thread 자동 오픈 금지 — 1-4 역할 조정)
  ② 게이트 실행 오류(rc=1) → `FAILED` — 판정 불가를 PASS 로 오해하지 않는다
  ③ PASS + `ct2_auto_promote: false` → `SHADOW` + `--auto-promote` **없이** 승인 오픈
  ④ PASS + `ct2_auto_promote: true`  → `AUTO_PROMOTED` + `--auto-promote` 포함 호출
  ⑤ `ct2_validate_cmd` 미등재 → `IFACE_WAIT` (게이트 없이 배포로 흐르지 않는다)
  ⑥ 명령 템플릿의 `python` 선두 토큰은 현재 인터프리터로 치환 (venv 오해석 방어)

subprocess 는 전부 스텁이다 — 여기서 검증하는 건 **분기와 인자 조립**이지 학습이 아니다.

실행: python -m pytest tests/test_ct2_orchestrator_gate.py -q
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "agent_a_mlops"))

import ct2_orchestrator as O  # noqa: E402


CHAMBER = "SIM_CH_1"
INCIDENT = "INC-20260730-SIMCH1-001"
EVT = {"event_type": "ChamberRequalified", "incident_id": INCIDENT,
       "chamber_id": CHAMBER, "qual_id": "QUAL-20260730-SIMCH1-001",
       "requalified_at": "2026-07-30T12:00:00.000Z"}

PARAMS = {
    "ct2_backfill_hours": 24,
    # O2-3 (2026-08-05) — 조립 argv 하드코딩을 config 템플릿으로 옮겼다. 미등재면 IFACE_WAIT.
    "ct2_assemble_cmd": ("python scripts/ct2_assemble_trainset.py --from-kafka "
                         "--bootstrap {bootstrap} --chamber {chamber} --since {since} "
                         "--until {until} --limits-json {limits} --out {out}"),
    "ct2_retrain_cmd": "python -m ae_pipeline.retrain --data {data} --out {out}",
    "ct2_validate_cmd": "python -m ae_pipeline.validate_bundle --bundle {bundle} --data {data}",
    "ct2_cmd_cwd": "src/agent_a_mlops/Autoencoder",
    "ct2_auto_promote": False,
}


class _Result:
    """subprocess.CompletedProcess 대역 (필요한 속성만)."""

    def __init__(self, rc=0, stderr=""):
        self.returncode, self.stdout, self.stderr = rc, "", stderr


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """REPO_ROOT 를 tmp 로 옮기고 조립 입력·산출물을 미리 깔아둔다."""
    monkeypatch.setattr(O, "REPO_ROOT", tmp_path)
    d = tmp_path / "Data" / "ct2"
    d.mkdir(parents=True)
    (d / f"firm_limits_{CHAMBER}.json").write_text(
        json.dumps([{"sensor": "C11", "step": 4, "lcl": 1.0, "ucl": 9.0}]), encoding="utf-8")
    (d / f"ct2_trainset_{CHAMBER}_20260730_120000.csv").write_text("C64\nW0\n", encoding="utf-8")
    return tmp_path


def _stub_runner(calls, rc_for):
    """subprocess.run 대역 — 호출을 기록하고 명령 종류별 rc 를 돌려준다.

    rc_for: {'assemble'|'retrain'|'validate'|'open': rc}
    """
    def run(cmd, **kw):
        calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "ct2_assemble_trainset.py" in joined:
            kind = "assemble"
        elif "validate_bundle" in joined:
            kind = "validate"
        elif "retrain" in joined:
            kind = "retrain"
        else:
            kind = "open"
        return _Result(rc_for.get(kind, 0))
    return run


def _kinds(calls):
    """호출 목록 → 종류 시퀀스 (분기 검증용)."""
    out = []
    for c in calls:
        j = " ".join(str(x) for x in c)
        out.append("assemble" if "ct2_assemble_trainset.py" in j else
                   "validate" if "validate_bundle" in j else
                   "retrain" if "retrain" in j else "open")
    return out


# ── ① 게이트 FAIL → BLOCKED, 승인 미오픈 ────────────────────────────────────

def test_gate_fail_blocks_and_opens_nothing(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {"validate": 2}))

    status, detail, _bundle = O.run_generation(EVT, dict(PARAMS))

    assert status == "BLOCKED", detail
    assert "open" not in _kinds(calls), \
        "게이트 FAIL 상태에서 승인/promote 를 열었다 (헌법 1-4 역할 조정 위반)"
    assert "보존" in detail and "validation.json" in detail


def test_gate_error_is_failed_not_pass(sandbox, monkeypatch):
    """rc=1(실행 오류) 을 PASS 로 흘리지 않는다."""
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {"validate": 1}))

    status, _d, _b = O.run_generation(EVT, dict(PARAMS))
    assert status == "FAILED"
    assert "open" not in _kinds(calls)


# ── ③④ PASS 경로 2모드 ─────────────────────────────────────────────────────

def test_pass_with_auto_promote_off_opens_approval(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {}))

    status, detail, bundle = O.run_generation(EVT, {**PARAMS, "ct2_auto_promote": False})

    assert status == "SHADOW", detail
    assert _kinds(calls) == ["assemble", "retrain", "validate", "open"]
    opener = calls[-1]
    assert "--auto-promote" not in opener, "차단기 모드인데 자동 배포로 호출됐다"
    assert "src.orchestrator.ct2_deploy_approval" in opener
    assert INCIDENT in opener


def test_pass_with_auto_promote_on_promotes(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {}))

    status, detail, bundle = O.run_generation(EVT, {**PARAMS, "ct2_auto_promote": True})

    assert status == "AUTO_PROMOTED", detail
    opener = calls[-1]
    assert "--auto-promote" in opener
    # 게이트 리포트 경로가 승인 모듈에 전달돼야 한다 (감사 사유 기록 — 3-3 ③ ⓒ)
    assert any(str(a).endswith("validation.json") for a in opener)


def test_auto_promote_status_fits_db_column():
    """`ct_decisions.retrain_status` = VARCHAR(16) — 신규 값이 한계 내인지."""
    for s in ("BLOCKED", "AUTO_PROMOTED", "SHADOW", "IFACE_WAIT", "ROLLED_BACK"):
        assert len(s) <= 16, s


def test_bundle_name_returned_for_ct_decisions(sandbox, monkeypatch):
    """번들명이 반환돼야 `record_decision(after=...)` 로 `model_version_after` 가 남는다.

    자동 promote 는 `ct_decisions` INSERT 보다 먼저 배포 모듈을 타므로 그쪽 UPDATE 는
    0행이다 — 이 반환값이 유일한 기록 경로다.
    """
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {}))
    _s, _d, bundle = O.run_generation(EVT, {**PARAMS, "ct2_auto_promote": True})
    assert bundle and bundle.startswith(f"ae_ct2_{CHAMBER}_")
    assert len(bundle) <= O.BUNDLE_NAME_MAX


def test_bundle_name_fits_for_long_chamber_id(sandbox, monkeypatch):
    """긴 챔버 ID 에서도 번들명이 감사 컬럼(VARCHAR(32)) 안에 들어간다 (O2-7, 2026-08-05).

    구 동작은 32자 초과를 **FAILED 로 중단**했다. 그건 실챔버 ID 가 10자만 넘어도 전 건이
    죽는다는 뜻이고, 무인 상태에서는 조용한 전면 정지다. 이름을 해시로 줄여 흡수하되
    원 챔버는 감사 행·로그에 남긴다 — 이름은 컬럼 폭에 맞추고 식별은 감사가 한다.
    """
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {}))
    long_ch = "SIM_CHAMBER_LONGNAME_1"
    evt = {**EVT, "chamber_id": long_ch}
    # 조립 산출물은 챔버명으로 글롭하므로 이 챔버용 입력을 깔아준다.
    d = sandbox / "Data" / "ct2"
    (d / f"firm_limits_{long_ch}.json").write_text("[]", encoding="utf-8")
    (d / f"ct2_trainset_{long_ch}_20260730_120000.csv").write_text("C64\nW0\n", encoding="utf-8")

    status, detail, bundle = O.run_generation(evt, dict(PARAMS))
    assert status == "SHADOW", detail
    assert bundle and len(bundle) <= O.BUNDLE_NAME_MAX
    assert bundle.startswith("ae_ct2_") and long_ch not in bundle


def test_bundle_name_is_utc_not_local(monkeypatch):
    """번들명 stamp 는 **naive UTC** 다 (O2-8 — 감사 시각축과 일치, 계약 §8-B-1)."""
    from datetime import datetime, timezone
    t = datetime(2026, 8, 5, 3, 4, 5, tzinfo=timezone.utc)
    assert O.bundle_name_for("CH1", t) == "ae_ct2_CH1_20260805_030405"


def test_render_cmd_injects_labels():
    """`ct2_labels_path` 가 있으면 argv 에 `--labels <path>` 가 들어간다 (G0-2)."""
    argv = O._render_cmd("python -m ae_pipeline.retrain --data {data} --labels {labels}",
                         data="d.csv", labels="/x/labels.csv")
    assert "--labels" in argv and "/x/labels.csv" in argv


def test_render_cmd_omits_labels_when_blank():
    """빈 값이면 **토큰째** 빠진다 — 빈 문자열 인자는 실행 오류(exit 1)로 강등시킨다 (G0-2).

    게이트 판정(exit 2)이 실행 오류로 바뀌면 리포트가 남지 않아 원인을 못 짚는다. 라벨이
    빠진 채 만들어진 번들은 게이트 E0 가 FAIL 로 잡으므로 fail-open 이 아니다.
    """
    argv = O._render_cmd("python -m ae_pipeline.retrain --data {data} --labels {labels}",
                         data="d.csv", labels="")
    assert "--labels" not in argv and "" not in argv[1:]


def test_validate_out_is_a_file_not_dir(sandbox, monkeypatch):
    """`{out}` 치환값은 파일 경로여야 한다 — 폴더면 write_text 가 IsADirectoryError."""
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {}))
    p = {**PARAMS,
         "ct2_validate_cmd": "python -m ae_pipeline.validate_bundle "
                             "--bundle {bundle} --data {data} --out {out}"}
    O.run_generation(EVT, p)
    vcmd = [c for c in calls if "validate_bundle" in " ".join(map(str, c))][0]
    out = vcmd[vcmd.index("--out") + 1]
    assert str(out).endswith(O.VALIDATION_REPORT_NAME), out


# ── ⑤ 인터페이스 미등재 ─────────────────────────────────────────────────────

def test_missing_validate_cmd_is_iface_wait(sandbox, monkeypatch):
    """게이트 명령이 없으면 **되감기·학습 전에** 대기한다 (WP-B3 호이스트 — 3-3 ③ ⓑ).

    구현이 retrain 뒤에 검사하던 것을 함수 첫머리로 올렸다. 이제 게이트 명령이 없으면
    2시간 학습을 태우지 않고 즉시 IFACE_WAIT — subprocess 호출이 하나도 없어야 한다.
    """
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {}))

    p = dict(PARAMS)
    p["ct2_validate_cmd"] = None
    status, detail, _b = O.run_generation(EVT, p)

    assert status == "IFACE_WAIT" and "ct2_validate_cmd" in detail
    assert calls == [], "게이트 명령 없이 조립·학습을 시작했다 (호이스트 실패)"


def test_missing_retrain_cmd_is_iface_wait(sandbox, monkeypatch):
    """retrain 명령이 없으면 조립조차 하지 않는다 (WP-B3 — 첫머리 선제 검사)."""
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {}))
    p = dict(PARAMS)
    p["ct2_retrain_cmd"] = None
    status, _d, _b = O.run_generation(EVT, p)
    assert status == "IFACE_WAIT"
    assert calls == [], "retrain 인터페이스 없이 조립/학습을 시도했다"


# ── 조립 단계 게이트 (기존 동작 회귀) ────────────────────────────────────────

def test_sample_gate_short_is_gated(sandbox, monkeypatch):
    """조립 rc=2(표본 미달) → GATED, 학습으로 넘어가지 않는다."""
    calls = []
    monkeypatch.setattr(O.subprocess, "run", _stub_runner(calls, {"assemble": 2}))
    status, detail, _bundle = O.run_generation(EVT, dict(PARAMS))
    assert status == "GATED" and "전방 연장" in detail
    assert _kinds(calls) == ["assemble"]


def test_missing_firm_limits_is_gated(tmp_path, monkeypatch):
    """소급 필터 입력(firm limits)이 없으면 조립조차 시작하지 않는다."""
    monkeypatch.setattr(O, "REPO_ROOT", tmp_path)
    (tmp_path / "Data" / "ct2").mkdir(parents=True)
    status, detail, _bundle = O.run_generation(EVT, dict(PARAMS))
    assert status == "GATED" and "firm limits" in detail


# ── ⑥ 명령 렌더링 ───────────────────────────────────────────────────────────

def test_render_cmd_replaces_python_with_sys_executable():
    argv = O._render_cmd("python -m ae_pipeline.retrain --data {data} --out {out}",
                         data="D.csv", out="OUT")
    assert argv[0] == sys.executable, "PATH 의 python 을 그대로 쓰면 torch 없는 해석기일 수 있다"
    assert argv[-3:] == ["D.csv", "--out", "OUT"]


def test_render_cmd_keeps_non_python_entrypoint():
    argv = O._render_cmd("/usr/bin/env python -m x --bundle {bundle}", bundle="B")
    assert argv[0] == "/usr/bin/env"


def test_ct_id_regime():
    """ct_id 규약 = CT 프리픽스 승계 + AE 접미 (6-4 · 계약 §8-E)."""
    assert O.ct_id_for(INCIDENT) == "CT-20260730-SIMCH1-001-AE"
    assert O.ct_id_for("XYZ-1") == "CT-XYZ-1-AE"


def test_ct_id_matches_approval_module():
    """오케스트레이터(A)와 승인 모듈(PM)의 ct_id 규약이 갈라지지 않았는지.

    두 파일은 소유권 경계상 의도적으로 중복 정의한다 — 값 규약이 어긋나면 자동 promote 가
    엉뚱한 `ct_decisions` 행을 갱신하므로 테스트로 묶어둔다.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from src.orchestrator.ct2_deploy_approval import ct_id_for as pm_ct_id
    for inc in (INCIDENT, "INC-20260101-SIMCH4-099", "XYZ-1"):
        assert O.ct_id_for(inc) == pm_ct_id(inc), inc


if __name__ == "__main__":                     # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
