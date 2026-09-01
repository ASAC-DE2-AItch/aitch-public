# -*- coding: utf-8 -*-
"""M3 ② 월드스테이트 개조 — 주입기 런타임 주입·잔류 승계 단위 테스트 (CSV 무의존).

체인 dry-run(src/simulator/chain_dry_run.py)이 실데이터로 검증하는 잔류 승계를,
여기서는 주입기 순수 로직으로 못박는다: inject_now 스탬프 · 원복 금지 · 월드스테이트 단조.
실행: python tests/test_worldstate_chain.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from simulator.scenario_injector import ScenarioInjector  # noqa: E402

STD = {"C11": 10.0, "C17": 8.0}


def _shift(sid, ch, sensor="C11", mag=3.0):
    return {"scenario_id": sid, "pattern": "shift", "sensors": [sensor], "chambers": [ch],
            "magnitude_sigma": mag, "start_after_wafers": 0, "ground_truth": "gt"}


def test_inject_now_stamps_start():
    """inject_now는 start_after_wafers를 주입 순번으로 스탬프하고 사본을 장전(원본 불변)."""
    inj = ScenarioInjector([], STD)
    src = _shift("S1", "SIM_CH_3")
    loaded = inj.inject_now(src, 80)
    assert loaded["start_after_wafers"] == 80
    assert src["start_after_wafers"] == 0                 # 원본 YAML dict 불변
    assert inj.delta("SIM_CH_3", "C11", 79) == 0.0        # 주입 전 = 무영향
    assert inj.delta("SIM_CH_3", "C11", 80) == 30.0       # 주입 시점부터 mag(3σ×10)


def test_carry_over_no_revert():
    """★핵심 — shift는 주입 후 어떤 미래 순번에서도 원복되지 않는다 (§0-2 원복 금지)."""
    inj = ScenarioInjector([], STD)
    inj.inject_now(_shift("drift-ch3", "SIM_CH_3"), 80)
    later = [inj.delta("SIM_CH_3", "C11", k) for k in (80, 200, 1000, 8000)]
    assert all(abs(v - 30.0) < 1e-9 for v in later)       # 전 구간 유지 (0으로 복귀 안 함)


def test_active_summary_monotonic_across_cuts():
    """컷을 순차 주입하면 활성(월드스테이트) 집합이 줄지 않고 커진다 (잔류 승계)."""
    inj = ScenarioInjector([], STD)
    inj.inject_now(_shift("cut1", "SIM_CH_3"), 80)
    s1 = inj.active_summary({"SIM_CH_2": 100, "SIM_CH_3": 100})
    assert s1 == {"SIM_CH_3": ["cut1"]}                   # CH3만 활성
    inj.inject_now(_shift("cut2", "SIM_CH_2", sensor="C17"), 140)
    s2 = inj.active_summary({"SIM_CH_2": 300, "SIM_CH_3": 300})
    assert set(s2["SIM_CH_3"]) == {"cut1"} and set(s2["SIM_CH_2"]) == {"cut2"}
    # 이전 컷(cut1)이 뒤 컷 주입 후에도 그대로 활성 = 원복 금지
    prev = {sid for act in s1.values() for sid in act}
    now = {sid for act in s2.values() for sid in act}
    assert prev <= now                                    # 단조 (줄지 않음)


def test_injection_isolated_per_chamber():
    """한 챔버 주입이 다른 챔버를 활성화하지 않는다 (배역제 — 컷 격리의 상태 근거)."""
    inj = ScenarioInjector([], STD)
    inj.inject_now(_shift("ch3-only", "SIM_CH_3"), 50)
    assert inj.delta("SIM_CH_3", "C11", 100) == 30.0
    assert inj.delta("SIM_CH_1", "C11", 100) == 0.0       # 비대상 챔버 무영향
    assert inj.delta("SIM_CH_2", "C11", 100) == 0.0


def test_before_injection_world_empty():
    """주입 전 월드스테이트는 비어 있다 (상시 baseline = 무이상)."""
    inj = ScenarioInjector([], STD)
    assert inj.active_summary({"SIM_CH_1": 500, "SIM_CH_2": 500}) == {}
    assert inj.delta("SIM_CH_1", "C11", 500) == 0.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)}/{len(fns)} PASS")
