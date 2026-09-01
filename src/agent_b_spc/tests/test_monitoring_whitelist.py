"""monitoring_whitelist 유닛테스트 (config·decision·method)."""
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.agent_b_spc import monitoring_whitelist as mw


def _write_cfg(tmp_path: Path, spc: dict | None = None) -> Path:
    cfg = {"spc": {"activity_min": 0.05, "mag_min": 0.10, "false_alarm_warn_pct": 2.0, **(spc or {})}}
    p = tmp_path / "params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def test_whitelistconfig_load(tmp_path):
    cfg = mw.WhitelistConfig.load(_write_cfg(tmp_path))
    assert cfg.activity_min == 0.05
    assert cfg.mag_min == 0.10
    assert cfg.false_alarm_warn_pct == 2.0


def test_whitelistconfig_missing_key_names_it(tmp_path):
    p = _write_cfg(tmp_path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    del data["spc"]["mag_min"]
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(KeyError, match="mag_min"):
        mw.WhitelistConfig.load(p)


def _limits_row(**kw):
    base = dict(chamber_id="C24_0", recipe_id="C6_0", step=4, sensor_window="settled",
                sensor_id="C11", sigma=1.0, center=0.0, sample_min=-3.0, sample_max=3.0,
                false_alarm_pct=0.0, qa_flags="", cv=0.1, n_distinct=100)
    base.update(kw); return base


def test_method_quantile_for_keepstar(tmp_path):
    cfg = mw.WhitelistConfig.load(_write_cfg(tmp_path))
    limits = pd.DataFrame([
        _limits_row(sensor_id="C11", false_alarm_pct=0.0),      # KEEP → sigma
        _limits_row(sensor_id="C61", false_alarm_pct=5.0),      # KEEP* → quantile
    ])
    out = mw.classify(limits, cfg)
    m = dict(zip(out["sensor_id"], out["method"]))
    assert m["C11"] == "sigma"
    assert m["C61"] == "quantile"


def test_constant_sensor_excluded(tmp_path):
    """상수(σ=0/range=0) 센서 → EXCLUDE (일반 규칙). 합성 C54 step1 벡터로 검증.

    구 근거는 C54/C56 정착 step1/5/6/7 상수0 경로였으나, C54/C56은 과도 재정정(2026-07-12)으로
    settled에서 빠져 그 실데이터 경로는 사라졌다. 상수→EXCLUDE 규칙 자체는 유효해 합성 예시로 유지."""
    cfg = mw.WhitelistConfig.load(_write_cfg(tmp_path))
    limits = pd.DataFrame([_limits_row(sensor_id="C54", step=1, sigma=0.0,
                                        center=0.0, sample_min=0.0, sample_max=0.0)])
    out = mw.classify(limits, cfg)
    assert out.iloc[0]["decision"] == "EXCLUDE"
