"""Step 7 S1 — 정기 리캘리 순수 헬퍼 단위 테스트.

스펙: Docs/B4-1_Step7_정기리캘리_라이브트리거_스펙플랜.md §3 D6-c(스태거)·D6-a(baseline)·D3(resolution).
전부 순수 함수(DB·consumer 불요) — reset_ref는 주입식 fn으로 대체.
"""
from __future__ import annotations

from src.agent_b_spc.periodic_recalc import (
    build_baseline_map, resolution_for, stagger_threshold,
)

_CHAMBERS = ["SIM_CH_1", "SIM_CH_2", "SIM_CH_3", "SIM_CH_4"]
_GK1 = ("SIM_CH_1", "C6_0", 4, "settled", "C11")
_GK2 = ("SIM_CH_1", "C6_0", 5, "settled", "C31")


# ── 스태거 (D6-c) ─────────────────────────────────────────────────────────────
def test_stagger_first_fire_offsets_by_chamber_index():
    """첫 발동 = interval + index·floor(interval/N) → 500·625·750·875 (동시 붕괴 방지)."""
    got = [stagger_threshold(_CHAMBERS, c, 500, first_fired=False) for c in _CHAMBERS]
    assert got == [500, 625, 750, 875]


def test_stagger_after_first_fire_no_offset():
    """첫 발동 이후엔 스태거 없음 — 전 챔버 interval."""
    assert all(stagger_threshold(_CHAMBERS, c, 500, first_fired=True) == 500 for c in _CHAMBERS)


def test_stagger_unknown_chamber_is_total_no_offset():
    """chamber 부재 → `.index` ValueError 방어(total) → 오프셋 0."""
    assert stagger_threshold(["SIM_CH_1"], "SIM_CH_9", 500, first_fired=False) == 500


def test_stagger_empty_chambers_no_crash():
    """챔버 목록 빈 경우(startup 전) → interval (0-division 방어)."""
    assert stagger_threshold([], "SIM_CH_1", 500, first_fired=False) == 500


# ── baseline 맵 빌더 (D6-a) ───────────────────────────────────────────────────
def test_build_baseline_map_collects_non_none():
    """각 group_key의 reset_ref를 모으고, None(기저 부재) 그룹은 제외(→ TTTM fallback)."""
    ref1 = {"center": 100.0, "sigma_ref": 10.0, "limit_version": "v1"}
    fake = {_GK1: ref1}                                  # _GK2는 None
    m = build_baseline_map([_GK1, _GK2], lambda gk: fake.get(gk))
    assert m == {_GK1: ref1}


def test_build_baseline_map_empty_when_all_none():
    """전부 None이면 빈 맵 — 전량 fallback(현행 동작)."""
    assert build_baseline_map([_GK1, _GK2], lambda gk: None) == {}


# ── resolution 조회 (D3) ──────────────────────────────────────────────────────
def test_resolution_for_looks_up_by_group_key_without_chamber():
    """조회 키 = chamber 제외 group_key[1:] (recipe·step·window·sensor)."""
    res = {("C6_0", 4, "settled", "C11"): 0.5}
    assert resolution_for(res, _GK1) == 0.5


def test_resolution_for_missing_returns_none():
    """미시딩 그룹 → None (recompute_group가 res_σ 하한 없이 처리)."""
    res = {("C6_0", 4, "settled", "C11"): 0.5}
    assert resolution_for(res, _GK2) is None
