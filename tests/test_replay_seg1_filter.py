# -*- coding: utf-8 -*-
"""시뮬 replay seg1 필터 단위 테스트 (2026-07-28 — C17 오탐 수정).

검증: ① 데이터 주도 리셋 감지 ② seg1 필터가 wafer 블록·순서 보존 ③ offset 재현성
(stats_df=전체면 필터해도 offset 불변 — chamber_offsets·B 시딩 무영향). Kafka 무의존.
실행: python tests/test_replay_seg1_filter.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from simulator.message_builder import filter_post_reset, last_reset_wafer_index  # noqa: E402
from simulator.chamber_replicator import ChamberReplicator  # noqa: E402


def _synthetic_df():
    """seg0(C33 10→39) 3장 + 리셋 → seg1(C33 1→3) 4장, 레짐 운반 센서 CX가 seg별 상이."""
    rows = []
    # seg0: wafer w0~w2, C33 10·25·39, CX≈300
    for i, c33 in enumerate([10, 25, 39]):
        for step, flag in [(4, 0), (4, 0)]:
            rows.append({"C64": f"w{i}", "C33": c33, "C7": step, "C42": flag,
                         "CX": 300.0 + i, "C10": 1000 + i})
    # 리셋 후 seg1: wafer w3~w6, C33 1·2·3·4, CX≈250
    for j, c33 in enumerate([1, 2, 3, 4]):
        for step, flag in [(4, 0), (4, 0)]:
            rows.append({"C64": f"w{3+j}", "C33": c33, "C7": step, "C42": flag,
                         "CX": 250.0 + j, "C10": 2000 + j})
    return pd.DataFrame(rows)


def test_reset_detection():
    """C33 급락(39→1)을 마지막 리셋으로 감지 — seg0 3장 뒤(순번 3)."""
    df = _synthetic_df()
    assert last_reset_wafer_index(df) == 3


def test_no_reset_returns_full():
    """리셋 없으면 필터가 원본 그대로 (순번 0)."""
    df = _synthetic_df()
    df["C33"] = range(len(df))                      # 단조 증가 — 리셋 없음
    assert last_reset_wafer_index(df) == 0
    assert len(filter_post_reset(df)) == len(df)    # 무필터


def test_filter_keeps_seg1_blocks():
    """필터 후 seg1 wafer(w3~w6)만·행 순서 보존·CX가 seg1 대역(250~)."""
    df = _synthetic_df()
    out = filter_post_reset(df)
    assert set(out["C64"]) == {"w3", "w4", "w5", "w6"}          # seg0 제외
    assert out["CX"].mean() < 260                              # seg1 대역 (seg0 300+ 아님)
    assert list(out["C64"]) == sorted(out["C64"].tolist())     # 블록 순서 보존


def test_offset_reproducible_with_stats_df():
    """★핵심 — stats_df=전체면 replay를 seg1로 좁혀도 offset 동일 (재현성·B 시딩 불변)."""
    df = _synthetic_df()
    seg1 = filter_post_reset(df)
    r_full = ChamberReplicator(df, 2, 0.5, seed=42)                     # 구 동작(전체 replay)
    r_seg1 = ChamberReplicator(seg1, 2, 0.5, seed=42, stats_df=df)      # 신규(옵션A)
    assert r_full.offsets["SIM_CH_1"]["CX"] == r_seg1.offsets["SIM_CH_1"]["CX"]
    # 단 replay wafer 풀은 좁혀짐
    assert len(r_full._wafer_groups) == 7 and len(r_seg1._wafer_groups) == 4


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"ALL {len(tests)} TESTS PASS")
