"""AE 재보정 진단 유틸 회귀 테스트."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_ae_recalib.py"
SPEC = importlib.util.spec_from_file_location("diagnose_ae_recalib", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_spearman_accepts_read_only_arrays():
    left = np.asarray([1.0, 2.0, 3.0])
    right = np.asarray([10.0, 20.0, 30.0])
    left.flags.writeable = False
    right.flags.writeable = False
    assert MODULE.spearman(left, right) == pytest.approx(1.0)


def test_spearman_uses_average_rank_for_ties():
    assert MODULE.spearman([1.0, 1.0, 2.0], [3.0, 3.0, 4.0]) == pytest.approx(1.0)
