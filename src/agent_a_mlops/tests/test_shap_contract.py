"""shap_top3 발행 정렬 계약 테스트 (A 담당 · API Contract §2).

계약: **`|contribution|` 내림차순 상위 3.** 발행 측(A)이 보장하므로 소비자
(B publisher → `fdc.alert.prediction_context.shap_top3`)는 재정렬하지 않는다.

이 테스트가 지키는 것:
  ① 정렬 규칙이 절댓값 기준임 — 부호 있는 원값으로 정렬하면 순서가 달라진다.
  ② 어떤 산출 경로(실모델·더미)든 발행 길목에서 동일 규칙을 통과한다.
  ③ 정규화가 입력 리스트를 in-place 변경하지 않는다 (헌법 3-3 — 순위 개입 금지).
  ④ 오염된 원소 하나가 발행 전체를 막지 않는다.
"""
from __future__ import annotations

import copy

import pytest

from src.agent_a_mlops.shap_contract import (
    SHAP_TOP_N,
    is_sorted_by_contribution,
    normalize_shap_top3,
    shap_rank_key,
)


def _item(sensor: str, contribution) -> dict:
    """SHAP 원소 최소본 (계약 §2 — sensor·name·contribution)."""
    return {"sensor": sensor, "name": f"NAME_{sensor}", "contribution": contribution}


def _sensors(shap) -> list:
    return [s["sensor"] for s in shap]


# ── ① 절댓값 정렬 규칙 ──────────────────────────────────────────────────────
def test_sorted_by_absolute_contribution_desc():
    """음의 큰 기여가 양의 작은 기여보다 상위 — 부호는 방향, 크기가 순위."""
    raw = [_item("C62", 0.27), _item("C11", -0.58), _item("C17", 0.15)]
    assert _sensors(normalize_shap_top3(raw)) == ["C11", "C62", "C17"]


def test_raw_value_sort_would_differ():
    """★ 계약의 존재 이유 — 소비자가 원값으로 재정렬하면 순서가 뒤집힌다.

    이 차이 때문에 "발행 측이 보장하고 소비자는 재정렬 금지"가 계약이 됐다.
    """
    raw = [_item("C11", -0.58), _item("C62", 0.27), _item("C17", 0.15)]
    published = _sensors(normalize_shap_top3(raw))
    naive = [s["sensor"] for s in sorted(raw, key=lambda d: d["contribution"], reverse=True)]
    assert published == ["C11", "C62", "C17"]
    assert naive == ["C62", "C17", "C11"]
    assert published != naive          # 재정렬 금지 근거


def test_all_negative_contributions_ranked_by_magnitude():
    raw = [_item("C17", -0.1), _item("C11", -0.9), _item("C62", -0.5)]
    assert _sensors(normalize_shap_top3(raw)) == ["C11", "C62", "C17"]


# ── ② 절단·멱등·안정성 ─────────────────────────────────────────────────────
def test_truncates_to_top_n():
    raw = [_item(f"C{i}", 0.1 * i) for i in range(1, 8)]
    out = normalize_shap_top3(raw)
    assert len(out) == SHAP_TOP_N == 3
    assert _sensors(out) == ["C7", "C6", "C5"]


def test_idempotent_on_already_sorted_input():
    """발행 길목에서 재호출돼도 결과 불변 — 실모델 경로는 이미 정규화를 거친다."""
    once = normalize_shap_top3([_item("C11", -0.58), _item("C62", 0.27), _item("C17", 0.15)])
    assert normalize_shap_top3(once) == once


def test_tie_preserves_input_order_for_reproducibility():
    """동점은 입력 순서 유지(안정 정렬) — 같은 입력이면 항상 같은 출력."""
    raw = [_item("C11", 0.3), _item("C62", -0.3), _item("C17", 0.3)]
    assert _sensors(normalize_shap_top3(raw)) == ["C11", "C62", "C17"]


# ── ③ in-place 변경 금지 (헌법 3-3) ────────────────────────────────────────
def test_does_not_mutate_input_list():
    """★ 입력 리스트·원소를 건드리지 않는다 — 캐시 원본 오염 경로 차단."""
    raw = [_item("C62", 0.27), _item("C11", -0.58), _item("C17", 0.15)]
    snap = copy.deepcopy(raw)
    normalize_shap_top3(raw)
    assert raw == snap                 # 순서·값 모두 원본 그대로


# ── ④ 오염 흡수 (발행 중단 금지) ────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["oops", True, False, None, float("nan"), float("inf")])
def test_contract_violation_contribution_ranks_last(bad):
    """비수치·bool·비유한 기여도는 최하위로 — 발행은 계속된다."""
    raw = [_item("CBAD", bad), _item("C11", 0.1)]
    assert _sensors(normalize_shap_top3(raw)) == ["C11", "CBAD"]


def test_non_dict_elements_dropped():
    raw = [_item("C11", 0.5), "쓰레기", None, _item("C62", 0.2)]
    assert _sensors(normalize_shap_top3(raw)) == ["C11", "C62"]


def test_element_without_sensor_key_dropped():
    raw = [_item("C11", 0.5), {"name": "이름만", "contribution": 0.9}]
    assert _sensors(normalize_shap_top3(raw)) == ["C11"]


@pytest.mark.parametrize("empty", [None, [], (), "", 0])
def test_empty_inputs_return_empty_list(empty):
    assert normalize_shap_top3(empty) == []


def test_non_list_input_returns_empty_list():
    assert normalize_shap_top3({"sensor": "C11", "contribution": 0.5}) == []


# ── 보조 함수 ──────────────────────────────────────────────────────────────
def test_shap_rank_key_uses_absolute_value():
    assert shap_rank_key(_item("C11", -0.58)) == pytest.approx(0.58)
    assert shap_rank_key(_item("C11", "oops")) == 0.0
    assert shap_rank_key("dict 아님") == 0.0


def test_is_sorted_by_contribution_detects_violation():
    ok = [_item("C11", -0.58), _item("C62", 0.27)]
    bad = [_item("C62", 0.27), _item("C11", -0.58)]
    assert is_sorted_by_contribution(ok)
    assert not is_sorted_by_contribution(bad)
    assert is_sorted_by_contribution([])
