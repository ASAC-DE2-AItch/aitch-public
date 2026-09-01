# -*- coding: utf-8 -*-
"""CT① WP-9 회귀 테스트 — D13 절대 열화 가드 · D14 리로드 스모크 (설계 v2 §4-7·§4-8).

이 두 장치는 **WP-7(배포 후 감시·자동 롤백) 폐지의 대가**다 (설계 v2 개정 ③). 그래서
"동작한다"가 아니라 **"폐지된 감시가 잡았어야 할 것을 잡는다"**를 회귀로 고정한다.

  D13 — 상대 비교(D1 champion–challenger)는 champion·challenger 가 **같은 오염**을
        학습하면 무력하다. AE 라벨 오염 38장이 상대 지표·게이트·PM 보고를 전부 통과한
        실증이 헌법 7장에 있다. `test_d13_blocks_when_both_are_bad` 가 그 형태다 —
        개선율은 훌륭한데 절대값이 붕괴한 케이스가 **D13 없이는 PASS 였다**.
  D14 — 게이트는 조립 CSV 축에서 라벨로 채점한다. 서빙 스트림 축의 즉시 결함(전량
        NaN·상수 출력·범위 이탈)은 라벨이 도착하기 전까지 어떤 게이트도 못 본다.

⚠️ 임계는 **픽스처 params 파일에 고정**한다 — 실 `config/params.yaml` 을 읽게 두면
   WP-8 리허설 스윕이 `ct1_gate_abs_rmse_max_ratio` 를 조정하는 순간 이 회귀들이 조용히
   뒤집힌다 (ratio > 2.003 이면 "붕괴 차단" 테스트가 PASS 로 통과해 버린다).

실행:  python -m pytest src/agent_a_mlops/tests/test_ct1_wp9_guards.py -q
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_A_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_A_ROOT / "lean85"))
sys.path.insert(0, str(_A_ROOT))
import ct1_orchestrator as orch  # noqa: E402
import lean85_pipeline as lp  # noqa: E402
import validate_lean85 as vl  # noqa: E402

try:                                    # consumer 는 confluent_kafka 의존 — D14 절만 건너뛴다
    import consumer as cons             # noqa: E402
except Exception as _imp_err:           # noqa: BLE001
    cons, _CONSUMER_ERR = None, _imp_err
else:
    _CONSUMER_ERR = None

# ── 축소 피처 계약 ────────────────────────────────────────────────────────
# `f_y` 는 타깃과 **같은 값**을 담는다. 가짜 모델이 `f_y + err` 를 내면 pooled RMSE 가
# 정확히 |err| 이 되어, 게이트의 상대·절대 판정을 소수점 단위로 조립할 수 있다.
_LEAN = ["f_y", "f_noise"]
_CUTOFF = "2026-08-04T22:00:00"
_RATIO = 1.5                            # 픽스처 고정 — cap = 99.84 × 1.5 = 149.76


class _FakeModel:
    """예측 = 타깃 + 고정 오차 → pooled RMSE = |err| 인 모델."""

    def __init__(self, err: float):
        self.err = float(err)

    def predict(self, X):
        return np.asarray(X["f_y"], dtype=float) + self.err


def _manifest(*, n_train: int = 2000) -> dict:
    return {
        "features_n": len(_LEAN),
        "features_sha1": hashlib.sha1(",".join(_LEAN).encode()).hexdigest()[:12],
        "n_wafers_train": n_train,
        "train_period": ["2025-08-05T00:00:00", _CUTOFF],   # 최대 = 컷오프 (B1 경계 통과)
        "created_at_local": "2026-08-04T22:05:00",
    }


def _eval_table(n: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    y = rng.normal(800.0, 200.0, n)
    base = pd.Timestamp(_CUTOFF) + pd.Timedelta(hours=1)     # 전량 컷오프 이후 (B2)
    return pd.DataFrame({
        lp.ID_COL: [f"W{i}" for i in range(n)],
        lp.TARGET_COL: y,
        "f_y": y,
        "f_noise": rng.normal(0.0, 1.0, n),
        "wf_ts": [base + pd.Timedelta(minutes=i) for i in range(n)],
        "low_confidence": 0,
    })


def _window_meta(tmp_path: Path) -> dict:
    trainset = tmp_path / "trainset.csv"                     # B2b 가 실제로 읽는다
    pd.DataFrame({lp.ID_COL: [f"T{i}" for i in range(10)]}).to_csv(trainset, index=False)
    start = pd.Timestamp(_CUTOFF) - pd.Timedelta(days=365)
    return {
        "now": "2026-08-05T22:00:00", "cutoff": _CUTOFF,
        "start": start.isoformat(), "start_base": start.isoformat(),
        "window_days": 365.0, "buffer_days": 1.0, "effective_window_days": 365.0,
        "require_complete_loud_regime": True, "extended": False, "extended_days": 0.0,
        "extend_failed": False, "complete_loud_regimes_in_window": 1, "regime_used": None,
        "pm_events_n": 3, "n_wafers": 5000, "trainset": str(trainset),
    }


@pytest.fixture()
def gate_world(tmp_path, monkeypatch):
    """게이트를 실제로 통과시킬 수 있는 최소 세계 — 모델 오차만 바꿔가며 판정을 본다."""
    chal, champ = tmp_path / "lean85_20260805_220000_daily", tmp_path / "lean85_20260720_163040"
    for d in (chal, champ):
        d.mkdir()
        (d / "lean85_model.json").write_text("{}", encoding="utf-8")
        (d / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(lp, "load_frozen", lambda: (list(_LEAN), {}, 10))
    monkeypatch.setattr(lp, "check_feature_contract", lambda lean: None)
    monkeypatch.setattr(vl, "build_eval_table", lambda *a, **k: _eval_table())
    # A4 는 lean-85 계약(85피처)을 강제한다 — 축소 계약으로 세계를 세웠으니 함께 줄인다.
    # (`features_n` 은 `_PARAM_KEYS` 밖이라 params.yaml 로는 못 바꾼다 = 의도된 설계.)
    monkeypatch.setitem(vl.DEFAULTS, "features_n", len(_LEAN))

    params = tmp_path / "params.yaml"                        # 실 config 결박 차단 (모듈 docstring)
    params.write_text(
        "ct:\n"
        f"  ct1_gate_abs_rmse_max_ratio: {_RATIO}\n"
        "  ct1_gate_min_train_wafers: 1000\n"
        "  ct1_gate_min_eval_wafers: 100\n"
        "  ct1_promote_min_rmse_gain_pct: 3.0\n", encoding="utf-8")

    errs = {"chal": 100.0, "champ": 120.0}

    def _load(path):
        key = "chal" if Path(path).resolve() == chal.resolve() else "champ"
        return _FakeModel(errs[key]), _manifest()

    monkeypatch.setattr(lp, "load_model", _load)
    return types.SimpleNamespace(chal=chal, champ=champ, errs=errs, params=params,
                                 wmeta=_window_meta(tmp_path), tmp=tmp_path)


def _run_gate(w, params_path=...) -> dict:
    return vl.validate(w.chal, w.tmp / "eval.csv", champion_dir=str(w.champ),
                       window_meta=w.wmeta, pm_log=str(w.tmp / "pm_log.json"),
                       params_path=w.params if params_path is ... else params_path)


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["check"] == name)


# ── D13: 게이트 절대 상한 (설계 §4-7) ─────────────────────────────────────
def test_d13_cap_anchor_is_the_bench_times_ratio(gate_world):
    """앵커 계약 — cap = BENCH_POOLED × ratio. 픽스처가 고정한 값과 일치해야 한다."""
    rep = _run_gate(gate_world)
    assert rep["metrics"]["abs_rmse_cap"] == pytest.approx(lp.BENCH_POOLED * _RATIO, rel=1e-9)


def test_d13_passes_when_absolute_level_is_healthy(gate_world):
    """정상 케이스 — 개선율도 상한도 통과하면 PASS. 가드가 평시를 막지 않는다."""
    rep = _run_gate(gate_world)
    assert rep["gate_verdict"] == "PASS", rep["failed_checks"]
    assert _check(rep, "D2_절대_상한")["pass"]
    assert rep["metrics"]["champion_over_cap"] is False


def test_d13_blocks_when_both_are_bad(gate_world):
    """★핵심 회귀 — champion·challenger 가 **같이** 붕괴한 케이스.

    개선율 33%(≫3% 기준)라 D1 은 통과한다. **D13 이 없으면 이 번들이 승격된다.**
    절대 상한이 상대 개선율과 무관하게 막아야 한다.
    """
    gate_world.errs.update(chal=200.0, champ=300.0)
    rep = _run_gate(gate_world)

    assert _check(rep, "D1_champion_challenger")["pass"], "전제: 상대 비교는 통과한다"
    assert rep["metrics"]["rmse_gain_pct"] > 30.0
    assert not _check(rep, "D2_절대_상한")["pass"]
    assert rep["gate_verdict"] == "SKIP"
    assert any("절대 상한 초과" in r for r in rep["skip_reasons"])


def test_d13_failure_is_skip_not_fail(gate_world):
    """품질 미달은 FAIL(사고 경보)이 아니라 SKIP 이다 — 둘을 뭉개면 에스컬레이션이 무의미."""
    gate_world.errs.update(chal=200.0, champ=300.0)
    rep = _run_gate(gate_world)
    assert "D2_절대_상한" not in rep["critical_failed"]
    assert rep["gate_verdict"] != "FAIL"
    assert rep["metrics"]["champion_challenger_result"] == "SKIP"


def test_d13_reports_champion_cap_comparison(gate_world):
    """champion 대조는 **판정에 쓰지 않고 기재만** 한다 (연속 판정은 오케스트레이터)."""
    gate_world.errs.update(chal=100.0, champ=300.0)          # champion 만 상한 초과
    rep = _run_gate(gate_world)
    assert rep["metrics"]["champion_over_cap"] is True
    assert rep["metrics"]["champion_name"] == gate_world.champ.name
    assert rep["gate_verdict"] == "PASS", "champion 초과가 승격을 막아서는 안 된다"


def test_d13_ratio_is_overridable_from_params(gate_world, tmp_path):
    """임계 단일 소스 = DEFAULTS + `ct.ct1_gate_abs_rmse_max_ratio` (D12 위임 배선 확인).

    키 이름이 틀리면 폴백 1.5 가 걸려 200 > 149.76 로 SKIP 이 되므로, 이 테스트는
    `_PARAM_KEYS` 배선의 오타를 실제로 잡는다.
    """
    p = tmp_path / "loose.yaml"
    p.write_text("ct:\n  ct1_gate_abs_rmse_max_ratio: 10.0\n", encoding="utf-8")
    gate_world.errs.update(chal=200.0, champ=300.0)
    rep = _run_gate(gate_world, params_path=p)
    assert _check(rep, "D2_절대_상한")["pass"]                # 상한 998.4 → 200 통과
    assert rep["gate_verdict"] == "PASS"


def test_d13_rejects_nonpositive_ratio(gate_world, tmp_path, caplog):
    """분모·임계는 산출 시점에 검증한다 — ratio≤0 이면 cap≤0 = **전 challenger 영구 SKIP**."""
    p = tmp_path / "broken.yaml"
    p.write_text("ct:\n  ct1_gate_abs_rmse_max_ratio: 0\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        rep = _run_gate(gate_world, params_path=p)
    assert rep["metrics"]["abs_rmse_cap"] > 0
    assert rep["gate_verdict"] == "PASS"
    assert any("양의 유한값" in r.getMessage() for r in caplog.records)


def test_d13_absent_score_does_not_fabricate_a_verdict(gate_world, monkeypatch):
    """challenger 채점이 없으면(표본 미달 등) 상한 대조를 **생략**한다 — 없는 근거로 막지 않는다."""
    small = _eval_table(n=10)                                 # C2 하한(100) 미달 → 채점 생략
    monkeypatch.setattr(vl, "build_eval_table", lambda *a, **k: small)
    rep = _run_gate(gate_world)
    assert rep["gate_verdict"] == "SKIP"
    assert _check(rep, "D2_절대_상한")["pass"]
    assert rep["metrics"].get("rmse_after") is None


# ── D13: champion 연속 초과 에스컬레이션 (오케스트레이터) ──────────────────
@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "STATE_PATH", tmp_path / "control" / "ct1" / "ct1_state.json")
    return tmp_path


_DAY0 = datetime(2026, 8, 5, 22, 0, tzinfo=timezone.utc)


def _day(i: int) -> datetime:
    return _DAY0 + timedelta(days=i)


def _report(over, name="lean85_20260720_163040"):
    return {"metrics": {"champion_over_cap": over, "rmse_before": 300.0,
                        "abs_rmse_cap": 149.76, "champion_name": name}}


def test_champion_streak_escalates_only_after_consecutive_hits(state, caplog):
    """단일값 발화 금지 — H=1일 pooled RMSE 는 일 변동성이 커서 하루로는 못 세운다.

    임계를 **기본값(3)이 아닌 2** 로 준다: 코드가 config 키를 잘못 읽으면 폴백 3 이 걸려
    2회차에서 에스컬레이션이 안 나므로, 이 테스트가 배선 오타를 잡는다.
    """
    p = {"ct1_abs_guard_champion_streak": 2}
    with caplog.at_level(logging.WARNING):
        assert orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(0)) == 1
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(1)) == 2
    assert any("★에스컬레이션" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR)


def test_champion_streak_uses_default_threshold_when_config_missing(state, caplog):
    """폴백은 잠금(∞)이 아니라 3 이다 — 이 값은 가드를 여는 스위치가 아니라 **경보를
    늦추는 값**이라, params 로드 실패 시 잠그면 경보가 사라진다 (auto_promote 와 반대)."""
    with caplog.at_level(logging.ERROR):
        for i in range(orch.ABS_GUARD_STREAK_DEFAULT):
            orch._update_champion_cap_streak({}, _report(True), "SKIP", _day(i))
    assert any("★에스컬레이션" in r.getMessage() for r in caplog.records)


def test_champion_streak_resets_when_back_under_cap(state):
    p = {"ct1_abs_guard_champion_streak": 3}
    orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(0))
    assert orch._update_champion_cap_streak(p, _report(False), "SKIP", _day(1)) == 0


def test_champion_streak_held_when_unmeasurable(state):
    """측정 불가(표본 미달·champion 부재)는 '정상'이 아니다 — 리셋하면 가드가 조용히 죽는다."""
    p = {"ct1_abs_guard_champion_streak": 3}
    orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(0))
    orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(1))
    assert orch._update_champion_cap_streak(p, _report(None), "SKIP", _day(2)) == 2
    assert orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(3)) == 3


def test_champion_streak_resets_on_promotion(state):
    """승격은 champion 을 갈아치웠다 — 옛 champion 의 이력으로 새 champion 을 고발하지 않는다."""
    p = {"ct1_abs_guard_champion_streak": 3}
    orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(0))
    orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(1))
    assert orch._update_champion_cap_streak(p, _report(True), "AUTO_PROMOTED", _day(2)) == 0


def test_champion_streak_resets_when_champion_identity_changes(state):
    """★수동 승격·롤백 회귀 — 그 경로의 status 는 SHADOW 다.

    status 만 보고 리셋하면 차단기 강하 구간의 수동 교체·롤백에서 카운터가 그대로
    넘어가 **교체된 옛 champion 의 이력으로 새 champion 을 고발**한다. 이름으로도 감지한다.
    """
    p = {"ct1_abs_guard_champion_streak": 3}
    orch._update_champion_cap_streak(p, _report(True, "lean85_A"), "SKIP", _day(0))
    orch._update_champion_cap_streak(p, _report(True, "lean85_A"), "SKIP", _day(1))
    assert orch._update_champion_cap_streak(p, _report(True, "lean85_B"), "SHADOW", _day(2)) == 1


def test_champion_streak_counts_days_not_runs(state):
    """정기(일간)와 요란 PM 이벤트가 같은 날 겹치면 거의 같은 평가창이다 — 상관된 증거를
    이틀치로 세지 않는다 (연속 조건의 근거가 '일 변동성'이므로 축도 일)."""
    p = {"ct1_abs_guard_champion_streak": 3}
    assert orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(0)) == 1
    same_day = _day(0) + timedelta(hours=1)          # 22:00 → 23:00, 같은 UTC 날
    assert orch._update_champion_cap_streak(p, _report(True), "SKIP", same_day) == 1
    assert orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(1)) == 2


def test_champion_streak_same_day_recovery_resets(state):
    """★비대칭 고정 (F5 — 설계 v2 §8-1 ⑦ 명문화): 같은 날 2회차의 `over=True` 는 세지
    않지만 **`over=False` 는 리셋한다.**

    행동 고정이 목적이라 이 테스트는 수정 전에도 GREEN 이다. 같은 날의 정상 관측은
    "champion 이 지속 붕괴 상태는 아니다"라는 실증거이고, 경보를 늦추는 방향이라
    안전측이다 — 축 일관성을 이유로 무심코 뒤집는 변경을 여기서 막는다.
    """
    p = {"ct1_abs_guard_champion_streak": 3}
    assert orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(0)) == 1
    assert orch._update_champion_cap_streak(p, _report(True), "SKIP", _day(1)) == 2
    same_day = _day(1) + timedelta(hours=1)          # 같은 UTC 날의 회복 관측
    assert orch._update_champion_cap_streak(p, _report(False), "SKIP", same_day) == 0


# ── D14: 리로드 스모크 (consumer) ─────────────────────────────────────────
pytestmark_consumer = pytest.mark.skipif(
    cons is None, reason=f"consumer import 불가(confluent_kafka 등): {_CONSUMER_ERR}")


class _FakePredictor:
    """`predict_batch` 만 노출한다 — 스모크는 **서빙과 같은 경로**를 태워야 한다."""

    def __init__(self, fn):
        self.lean = list(_LEAN)
        self.predict_batch = fn


@pytest.fixture()
def ring(monkeypatch):
    """링버퍼를 격리하고 서로 다른 피처 행으로 충전한다."""
    monkeypatch.setattr(cons, "CT1_SMOKE_MIN", 5)
    cons._feature_ring.clear()
    rng = np.random.default_rng(1)
    for i in range(8):
        cons._feature_ring.append(np.array([800.0 + i, rng.normal()], dtype=float))
    yield cons._feature_ring
    cons._feature_ring.clear()


@pytest.fixture(autouse=True)
def _reset_signal_watermark():
    """신호 적용 워터마크(consumer 모듈 전역)를 테스트마다 격리한다.

    남겨두면 앞 테스트의 `promoted_at`(2026)이 뒤 테스트의 mtime 폴백(1970)을
    "구 신호"로 판정해 조용히 건너뛴다 — 원인 추적이 어려운 연쇄 실패다.
    """
    if cons is not None:
        cons._ct1_signal_watermark = None
    yield
    if cons is not None:
        cons._ct1_signal_watermark = None


def _drop_signal(dirpath: Path, ct_id: str, model_dir: Path, *,
                 promoted_at: str | None = None, rollback: bool = False,
                 mtime: float | None = None) -> Path:
    """promote 신호 드롭 헬퍼 — `ct1_orchestrator.drop_promote_signal` 의 산출물 모사.

    S10 회귀(F1)를 위해 `promoted_at`·`rollback`·`mtime` 을 주입할 수 있다. 전부 기본
    None/False 라 기존 D14 호출부는 무영향이다 (payload 에 `promoted_at` 이 없으면
    consumer 는 mtime 폴백으로 정렬한다).
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    sig = dirpath / f"promote_{ct_id}{'-rollback' if rollback else ''}.json"
    payload = {"ct_id": ct_id, "model_dir": str(model_dir), "rollback": bool(rollback)}
    if promoted_at is not None:
        payload["promoted_at"] = promoted_at
    sig.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(sig, (mtime, mtime))
    return sig


@pytestmark_consumer
class TestD14ReloadSmoke:
    """모듈 레벨 `importorskip` 을 쓰지 않는 이유: 그건 파일 **전체**를 건너뛰어
    Kafka 와 무관한 D13 테스트 13건까지 조용히 사라지게 한다."""

    def test_smoke_uses_the_serving_prediction_path(self, ring):
        """★계약 회귀 — 스모크는 `model.predict`(sklearn)이 아니라 발행값과 같은 경로다.

        발행값은 `booster.predict(pred_contribs=True)` 의 합인데, 설계 §4-8 이 지목한
        결함 1순위("consumer 환경의 xgboost 격차")가 정확히 그 계약이 깨지는 형태다.
        다른 API 로 검사하면 그 경로를 한 번도 안 태우고 통과시킨다.
        """
        assert hasattr(cons.Lean85Predictor, "predict_batch")
        seen = {}

        def _fn(X):
            seen["n"] = len(X)
            return np.asarray(X["f_y"]) + 5.0

        ok, _ = cons._reload_smoke(_FakePredictor(_fn))
        assert ok and seen["n"] == len(ring)

    def test_smoke_passes_on_healthy_model(self, ring):
        ok, why = cons._reload_smoke(_FakePredictor(lambda X: np.asarray(X["f_y"]) + 5.0))
        assert ok and why.startswith("OK")

    def test_smoke_rejects_nan_output(self, ring):
        ok, why = cons._reload_smoke(_FakePredictor(lambda X: np.full(len(X), np.nan)))
        assert not ok and "ⓐ" in why

    def test_smoke_rejects_constant_output(self, ring):
        """상수 출력 = 부스터 손상 신호. 입력이 서로 다른데 출력이 하나면 병리다."""
        ok, why = cons._reload_smoke(_FakePredictor(lambda X: np.full(len(X), 900.0)))
        assert not ok and "ⓑ" in why

    def test_smoke_rejects_out_of_range_output(self, ring):
        """범위 이탈은 **분산이 살아 있어도** 잡는다 (ⓑ 에 가려지지 않게 값을 흩어 둔다)."""
        ok, why = cons._reload_smoke(_FakePredictor(lambda X: np.asarray(X["f_y"]) * 1e6))
        assert not ok and "ⓒ" in why

    def test_smoke_skips_variance_check_when_inputs_are_identical(self, monkeypatch):
        """입력이 전부 같으면 건강한 모델도 상수를 낸다 — ⓑ 로 막으면 오탐 교착이다."""
        monkeypatch.setattr(cons, "CT1_SMOKE_MIN", 5)
        cons._feature_ring.clear()
        for _ in range(6):
            cons._feature_ring.append(np.array([800.0, 1.0], dtype=float))
        ok, why = cons._reload_smoke(_FakePredictor(lambda X: np.full(len(X), 900.0)))
        cons._feature_ring.clear()
        assert ok and "입력 무변화" in why

    def test_smoke_is_skipped_not_failed_when_buffer_is_cold(self, monkeypatch, caplog):
        """기동 직후 promote 가 영구 교착되면 안 된다 — 미충전은 차단이 아니라 생략이다.
        단 **경고로 남긴다**: 조용하면 '적재 영구 실패'와 '아직 안 참'이 구분되지 않는다."""
        monkeypatch.setattr(cons, "CT1_SMOKE_MIN", 20)
        cons._feature_ring.clear()
        with caplog.at_level(logging.WARNING):
            ok, why = cons._reload_smoke(_FakePredictor(lambda X: np.full(len(X), np.nan)))
        assert ok and "버퍼 미충전" in why
        assert any("미충전" in r.getMessage()
                   for r in caplog.records if r.levelno >= logging.WARNING)

    def test_smoke_config_is_clamped_against_silent_disablement(self, tmp_path, monkeypatch):
        """오설정 3종(min>buf · 0 · 음수)이 전 배포 차단·모듈 import 사망으로 끝나지 않게."""
        p = tmp_path / "config" / "params.yaml"
        p.parent.mkdir(parents=True)
        monkeypatch.setattr(cons, "REPO_ROOT", tmp_path)
        for raw, want in (("  ct1_smoke_buffer_wafers: 10\n  ct1_smoke_min_wafers: 99\n", (10, 10)),
                          ("  ct1_smoke_buffer_wafers: 0\n  ct1_smoke_min_wafers: 0\n", (1, 1)),
                          ("  ct1_smoke_buffer_wafers: -5\n  ct1_smoke_min_wafers: -1\n", (1, 1))):
            p.write_text("ct:\n" + raw, encoding="utf-8")
            assert cons._ct1_smoke_cfg() == want

    # ── promote 경로 통합 ────────────────────────────────────────────────
    def test_promote_swap_is_cancelled_when_smoke_fails(self, tmp_path, monkeypatch, ring,
                                                        caplog):
        """★핵심 회귀 — 로드는 성공했는데 응답이 병리적이면 **기존 모델을 유지**한다."""
        d = tmp_path / "ct1"
        _drop_signal(d, "CT-20260805-ALL-001-XGB", tmp_path / "model")
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        broken = _FakePredictor(lambda X: np.full(len(X), 900.0))     # 상수 출력
        monkeypatch.setattr(cons, "Lean85Predictor", lambda md: broken)

        incumbent = object()
        with caplog.at_level(logging.ERROR):
            out = cons._check_ct1_promote(incumbent)

        assert out is incumbent, "스왑이 취소되고 기존 predictor 가 유지돼야 한다"
        assert (d / "failed").exists() and list((d / "failed").glob("promote_*.json"))
        assert not list(d.glob("promote_*.json"))
        assert any("스모크" in r.getMessage() for r in caplog.records)

    def test_promote_swaps_when_smoke_passes(self, tmp_path, monkeypatch, ring):
        """정상 경로 — 스모크가 배포를 막는 장치가 되어서는 안 된다."""
        d = tmp_path / "ct1"
        _drop_signal(d, "CT-20260805-ALL-002-XGB", tmp_path / "model")
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        healthy = _FakePredictor(lambda X: np.asarray(X["f_y"]) + 5.0)
        monkeypatch.setattr(cons, "Lean85Predictor", lambda md: healthy)

        out = cons._check_ct1_promote(object())
        assert out is healthy
        assert list((d / "processed").glob("promote_*.json"))

    # ── 링버퍼 ───────────────────────────────────────────────────────────
    def test_ring_push_reuses_the_serving_feature_table(self):
        """추가 피처 빌드 없이 `predict_wafer` 의 table 을 재사용한다 (서빙 부하 0)."""
        cons._feature_ring.clear()
        tbl = pd.DataFrame({"f_y": [812.5], "f_noise": [0.25], "기타": ["무시"]})
        cons._ring_push(tbl, _LEAN)
        assert len(cons._feature_ring) == 1
        assert list(cons._feature_ring[0]) == [812.5, 0.25]
        cons._feature_ring.clear()

    def test_ring_push_never_breaks_serving(self, monkeypatch, caplog):
        """적재 실패가 추론을 죽이면 안 된다 — 스모크는 배포 보호이지 서빙 경로가 아니다."""
        monkeypatch.setattr(cons, "_ring_warned", False)      # 1회 제한 플래그 격리
        cons._feature_ring.clear()
        with caplog.at_level(logging.WARNING):
            cons._ring_push(pd.DataFrame({"엉뚱한컬럼": [1.0]}), _LEAN)   # 예외가 밖으로 안 나간다
        assert len(cons._feature_ring) == 0
        assert any("링버퍼 적재 실패" in r.getMessage() for r in caplog.records)


# ── S10: 최신 신호만 적용 (consumer — 설계 v2 D5·§4-5) ────────────────────
@pytestmark_consumer
class TestS10LatestSignalOnly:
    """★F1 회귀 — "롤백을 적용했더니 큐잉돼 있던 구(불량) promote 가 롤백을 뒤집는다".

    설계 v2 는 세 곳에서 "최신 신호만 적용(구 신호 superseded 보관)"을 명시하는데,
    구 구현은 `sorted(glob)[0]` = **이름 사전순 첫 번째**를 스캔당 1건 적용했다.
    `-rollback` 접미의 `-`(0x2D)가 `.json` 의 `.`(0x2E)보다 앞서므로 롤백이 먼저
    적용되고, 다음 스캔에서 구 불량 promote 가 그 위에 얹힌다. 자동 롤백을 폐지한
    v2 체제에서 수동 롤백은 **유일한 복구 수단**이라, 그 수단을 뒤집는 경로다.
    """

    @staticmethod
    def _tracked(monkeypatch, applied: list):
        """모델 폴더명을 적용 순서대로 기록하는 `Lean85Predictor` 대역."""
        made: dict = {}

        def _factory(model_dir):
            name = Path(model_dir).name
            applied.append(name)
            return made.setdefault(
                name, _FakePredictor(lambda X: np.asarray(X["f_y"]) + 5.0))

        monkeypatch.setattr(cons, "Lean85Predictor", _factory)
        return made

    def test_latest_signal_wins_over_stale_promote(self, tmp_path, monkeypatch, ring):
        """★핵심 회귀 (현행 코드에서 실패해야 한다).

        구(불량) promote 와 그것을 되돌리는 신 롤백이 함께 큐잉된 상태에서 스캔하면,
        **롤백만** 적용되고 구 신호는 적용 없이 `superseded/` 로 보관돼야 한다.
        """
        d = tmp_path / "ct1"
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        applied: list = []
        made = self._tracked(monkeypatch, applied)

        _drop_signal(d, "CT-20260805-ALL-002-XGB", tmp_path / "bad_model",
                     promoted_at="2026-08-05T22:00:00+00:00")
        _drop_signal(d, "CT-20260805-ALL-002-XGB", tmp_path / "good_model",
                     promoted_at="2026-08-05T22:01:00+00:00", rollback=True)

        out = cons._check_ct1_promote(object())

        assert applied == ["good_model"], f"최신(롤백)만 적용돼야 한다 — 실측 {applied}"
        assert out is made["good_model"]
        assert [p.name for p in (d / "superseded").glob("promote_*.json")] == \
               ["promote_CT-20260805-ALL-002-XGB.json"]
        assert [p.name for p in (d / "processed").glob("promote_*.json")] == \
               ["promote_CT-20260805-ALL-002-XGB-rollback.json"]
        assert not list(d.glob("promote_*.json"))

        cons._check_ct1_promote(out)                 # 다음 스캔 — 되살아나지 않는다
        assert applied == ["good_model"]

    def test_promoted_at_beats_filename_order(self, tmp_path, monkeypatch, ring):
        """파일명 사전순과 `promoted_at` 순서가 어긋나면 **payload 시각**이 이긴다."""
        d = tmp_path / "ct1"
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        applied: list = []
        self._tracked(monkeypatch, applied)

        _drop_signal(d, "CT-20260805-ALL-001-XGB", tmp_path / "newer",
                     promoted_at="2026-08-05T22:05:00+00:00")   # 파일명은 앞, 시각은 최신
        _drop_signal(d, "CT-20260805-ALL-009-XGB", tmp_path / "older",
                     promoted_at="2026-08-05T22:00:00+00:00")   # 파일명은 뒤, 시각은 구형

        cons._check_ct1_promote(object())
        assert applied == ["newer"]

    def test_mtime_fallback_when_promoted_at_missing(self, tmp_path, monkeypatch, ring):
        """`promoted_at` 이 없으면(수기 드롭·구 포맷) mtime 으로 정렬한다."""
        d = tmp_path / "ct1"
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        applied: list = []
        self._tracked(monkeypatch, applied)

        _drop_signal(d, "CT-20260805-ALL-001-XGB", tmp_path / "stale", mtime=1_000_000)
        _drop_signal(d, "CT-20260805-ALL-009-XGB", tmp_path / "fresh", mtime=2_000_000)

        cons._check_ct1_promote(object())
        assert applied == ["fresh"]

    def test_superseded_signals_are_kept_not_deleted(self, tmp_path, monkeypatch, ring):
        """대체된 신호는 **감사 보관**이다 — 삭제하면 '무엇이 큐에 있었나'가 사라진다."""
        d = tmp_path / "ct1"
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        applied: list = []
        self._tracked(monkeypatch, applied)

        for i, at in ((1, "22:00"), (2, "22:01"), (3, "22:02")):
            _drop_signal(d, f"CT-20260805-ALL-00{i}-XGB", tmp_path / f"m{i}",
                         promoted_at=f"2026-08-05T{at}:00+00:00")

        cons._check_ct1_promote(object())
        assert applied == ["m3"]
        kept = sorted(p.name for p in (d / "superseded").glob("promote_*.json"))
        assert kept == ["promote_CT-20260805-ALL-001-XGB.json",
                        "promote_CT-20260805-ALL-002-XGB.json"]
        assert all((d / "superseded" / k).stat().st_size > 0 for k in kept)

    def test_stale_signal_is_not_applied_when_supersede_move_fails(
            self, tmp_path, monkeypatch, ring, caplog):
        """★S10 잔여 경로 — `_move_signal` 은 실패를 삼키므로 **파일 이동은 가드가 아니다.**

        보관에 실패한 구 신호는 다음 스캔에서 (최신분이 이미 `processed/` 로 빠졌으므로)
        **유일한 후보 = `max`** 가 되어 적용된다 — 롤백이 뒤집히는 원래 사고 그대로다.
        워터마크가 그 재적용을 막는다.
        """
        d = tmp_path / "ct1"
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        applied: list = []
        self._tracked(monkeypatch, applied)

        _drop_signal(d, "CT-20260805-ALL-002-XGB", tmp_path / "bad_model",
                     promoted_at="2026-08-05T22:00:00+00:00")
        _drop_signal(d, "CT-20260805-ALL-002-XGB", tmp_path / "good_model",
                     promoted_at="2026-08-05T22:01:00+00:00", rollback=True)

        real_move = cons._move_signal

        def _flaky(sig, dest, **kw):                  # superseded 이동만 실패시킨다
            if dest.name == "superseded":
                return False
            return real_move(sig, dest, **kw)

        monkeypatch.setattr(cons, "_move_signal", _flaky)
        out = cons._check_ct1_promote(object())
        assert applied == ["good_model"]
        assert (d / "promote_CT-20260805-ALL-002-XGB.json").exists(), "구 신호가 잔류한다"

        with caplog.at_level(logging.WARNING):        # 다음 스캔 — 잔류 구 신호가 유일 후보
            out2 = cons._check_ct1_promote(out)

        assert applied == ["good_model"], f"구 신호가 재적용되면 안 된다 — 실측 {applied}"
        assert out2 is out
        assert any("구 신호" in r.getMessage() for r in caplog.records)

    def test_superseded_does_not_overwrite_an_earlier_record(self, tmp_path, monkeypatch,
                                                             ring):
        """★감사 보관 — 같은 ct_id 가 두 번 대체되면 **앞 기록이 남아 있어야** 한다.

        `_move_signal` 은 `replace` 라 기본적으로 덮어쓴다(processed/·failed/ 에는
        의도된 선택). superseded 는 "그때 큐에 무엇이 있었나"의 증거라 성격이 다르고,
        같은 ct_id 재드롭은 §4-5 수동 경로의 정상 시나리오다.
        """
        d = tmp_path / "ct1"
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        applied: list = []
        self._tracked(monkeypatch, applied)

        stale = "CT-20260805-ALL-002-XGB"
        _drop_signal(d, stale, tmp_path / "bad_v1", promoted_at="2026-08-05T22:00:00+00:00")
        _drop_signal(d, "CT-20260805-ALL-003-XGB", tmp_path / "win1",
                     promoted_at="2026-08-05T22:01:00+00:00")
        cons._check_ct1_promote(object())

        _drop_signal(d, stale, tmp_path / "bad_v2", promoted_at="2026-08-05T22:02:00+00:00")
        _drop_signal(d, "CT-20260805-ALL-004-XGB", tmp_path / "win2",
                     promoted_at="2026-08-05T22:03:00+00:00")
        cons._check_ct1_promote(object())

        assert applied == ["win1", "win2"]
        kept = list((d / "superseded").glob(f"promote_{stale}*.json"))
        assert len(kept) == 2, f"앞 기록이 덮이면 안 된다 — 실측 {[p.name for p in kept]}"
        dirs = sorted(json.loads(p.read_text(encoding="utf-8"))["model_dir"] for p in kept)
        assert [Path(x).name for x in dirs] == ["bad_v1", "bad_v2"]

    def test_rollback_wins_timestamp_ties(self, tmp_path, monkeypatch, ring):
        """★동률 회귀 (F9 — 수정 전 실패해야 한다): 시각으로 구분 불가면 롤백이 이긴다.

        `promoted_at` 은 1초, mtime 은 커널 틱(수 ms) 해상도라 연속 드롭이 같은 값을
        받는 동률이 실측됐다 (2026-08-07 리뷰 — ext4 연속 write_text 동률 재현). 동률에서
        이름 타이브레이크는 `….json`(0x2E) > `…-rollback.json`(0x2D) 이라 **항상 롤백이
        진다** — 롤백은 사람의 복구 개입이고, 구(불량) promote 에 지면 S10 사고 그대로다.
        기존 `test_mtime_fallback_…` 은 mtime 을 서로 다르게 강제해 이 동률을 못 본다.
        """
        d = tmp_path / "ct1"
        monkeypatch.setattr(cons, "CT1_PROMOTE_DIR", d)
        applied: list = []
        self._tracked(monkeypatch, applied)

        # ① promoted_at 부재(수기 드롭) + mtime 강제 동률 — 실측된 동률 조건 그대로
        _drop_signal(d, "CT-20260805-ALL-002-XGB", tmp_path / "bad_model",
                     mtime=1_000_000)
        _drop_signal(d, "CT-20260805-ALL-002-XGB", tmp_path / "good_model",
                     rollback=True, mtime=1_000_000)
        cons._check_ct1_promote(object())
        assert applied == ["good_model"], f"동률에서 롤백이 져선 안 된다 — 실측 {applied}"

        # ② promoted_at 같은 초(1초 해상도) 동률도 같은 규칙
        cons._ct1_signal_watermark = None
        for p in d.glob("**/*.json"):
            p.unlink()
        _drop_signal(d, "CT-20260805-ALL-003-XGB", tmp_path / "bad2",
                     promoted_at="2026-08-05T22:00:00+00:00", mtime=2_000_000)
        _drop_signal(d, "CT-20260805-ALL-003-XGB", tmp_path / "good2",
                     promoted_at="2026-08-05T22:00:00+00:00", rollback=True,
                     mtime=2_000_000)
        cons._check_ct1_promote(object())
        assert applied == ["good_model", "good2"]
