# -*- coding: utf-8 -*-
"""`chamber_split` 계약 테스트 — 챔버별 시간순 3분할 (torch 무의존).

배경: 주입 제외 후 정상셋이 챔버별로 시간축에서 잘려(CH2 100·CH3 200장), 전역 시간순
분할이 게이트 홀드아웃을 CH1·CH4로 쏠리게 한다. `split_mode='chamber'` 는 그 대안이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "agent_a_mlops" / "Autoencoder"))

from ae_pipeline import retrain as R  # noqa: E402


def _df(counts: dict, chamber_col="C24") -> tuple[pd.DataFrame, list]:
    """챔버별 n장 · 전역 시간순 인터리브 프레임 + 시간순 wafer 목록."""
    rows, t = [], 0
    pend = {ch: list(range(n)) for ch, n in counts.items()}
    while any(pend.values()):
        for ch in counts:                      # 라운드로빈 = 챔버 병렬 가동 재현
            if pend[ch]:
                i = pend[ch].pop(0)
                rows.append({"C64": f"{ch}_W{i:04d}", chamber_col: ch, "C10": t})
                t += 1
    df = pd.DataFrame(rows)
    order = df.sort_values("C10").C64.tolist()
    return df, order


def test_every_chamber_present_in_gate_holdout():
    """핵심 계약 — 챔버별로 잘렸어도 4챔버가 홀드아웃에 모두 들어간다."""
    df, order = _df({"CH1": 477, "CH2": 100, "CH3": 200, "CH4": 477})
    tr, val, gate = R.chamber_split(df, order, val_frac=0.5, gate_frac=0.5)
    ch = df.set_index("C64")["C24"]
    assert set(ch.reindex(gate)) == {"CH1", "CH2", "CH3", "CH4"}
    assert set(ch.reindex(val)) == {"CH1", "CH2", "CH3", "CH4"}


def test_global_time_split_starves_gate_of_small_chambers():
    """대조 — 같은 데이터에서 전역 시간순은 CH2·CH3를 홀드아웃에서 탈락시킨다."""
    df, order = _df({"CH1": 477, "CH2": 100, "CH3": 200, "CH4": 477})
    # 작은 챔버가 앞쪽에서 소진되도록 시간축을 재현 (CH2는 200번째 이후 없음)
    ch = df.set_index("C64")["C24"]
    order2 = [w for w in order if not (ch[w] == "CH2" and int(w[-4:]) >= 100)]
    _, val = R.time_split(order2, 0.5)
    _, gate = R._carve_gate_holdout(val, 0.5, quiet=True)
    assert "CH2" not in set(ch.reindex(gate))


def test_buckets_are_disjoint_and_complete():
    df, order = _df({"CH1": 300, "CH2": 300})
    tr, val, gate = R.chamber_split(df, order, val_frac=0.4, gate_frac=0.5)
    assert set(tr) | set(val) | set(gate) == set(order)
    assert not (set(tr) & set(val)) and not (set(val) & set(gate)) and not (set(tr) & set(gate))


def test_each_bucket_is_globally_time_ordered():
    """하류(drift EWMA 스트림)가 시간순 입력을 전제한다."""
    df, order = _df({"CH1": 300, "CH2": 220, "CH3": 260})
    rank = {w: i for i, w in enumerate(order)}
    for bucket in R.chamber_split(df, order, 0.4, 0.5):
        idx = [rank[w] for w in bucket]
        assert idx == sorted(idx)


def test_time_direction_preserved_within_chamber():
    """챔버 안에서는 TRAIN < fit < gate 순서가 유지된다 (누수 방지 — 헌법 1-3)."""
    df, order = _df({"CH1": 300, "CH2": 300})
    rank = {w: i for i, w in enumerate(order)}
    ch = df.set_index("C64")["C24"]
    tr, val, gate = R.chamber_split(df, order, 0.4, 0.5)
    for c in ("CH1", "CH2"):
        f = lambda b: [rank[w] for w in b if ch[w] == c]
        assert max(f(tr)) < min(f(val)) and max(f(val)) < min(f(gate))


def test_missing_chamber_column_raises():
    """조용한 전역 분할 폴백 금지 — manifest 는 chamber 인데 실제는 time 이 되는 사고 차단."""
    df, order = _df({"CH1": 200})
    with pytest.raises(ValueError, match="C24"):
        R.chamber_split(df.drop(columns=["C24"]), order, 0.4, 0.5)


def test_run_ct2_rejects_unknown_split_mode():
    with pytest.raises(ValueError, match="split_mode"):
        R.run_ct2("nonexistent.csv", "/tmp/none", split_mode="stratified")
