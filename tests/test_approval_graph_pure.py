# -*- coding: utf-8 -*-
"""#80 리뷰 ④ — approval_graph 순수 헬퍼 단위 테스트 (langgraph/DB 무의존).

대상:
  · resolve_modified_fields — modify 수정값(주입) 해석: 스칼라 = center 수정(σ 그룹) /
    dict = {center|ucl|lcl: 절대값}(분위수 그룹 경계·부분 수정) / 미지 키·비수치 무시 /
    limit_option 한정 (recipe 는 B5-4 후속 — DB 미기입).
  · _is_unique_violation — SQLSTATE 23505 판별. psycopg2 무의존 덕타이핑(pgcode) +
    래핑 예외는 __cause__ **한 겹만** 본다 (명세된 한계 — 그대로 고정).

보너스 — build_correction_payload 계약 §6 형태 가드: 미사용 필드도 **이름을 유지**해야
한다("이름 삭제 금지", 계약 line 285). 값 매핑 정합(옵션 jsonb 정량 필드 부재로 null 발행,
08-02 리허설 실측)은 계약 공백 해소 후 별도 테스트로 확장 — 여기서는 이름 불변만 고정한다.
"""
import sys
from pathlib import Path

import pytest  # noqa: F401 — 수동 실행 엔트리에서 사용

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # repo 루트
from src.orchestrator.approval_graph import (                   # noqa: E402
    MODIFIED_FIELD_MAP, _is_unique_violation, build_correction_payload,
    resolve_modified_fields)


# ── resolve_modified_fields — "주입 σ 구간 해석" ──────────────────────────────

def test_scalar_means_center_on_sigma_group():
    """스칼라 = σ 그룹의 center 수정 (B5-1 필드규칙 안ⓐ)."""
    assert resolve_modified_fields("limit_option", -252.1096) == {"center_modified": -252.1096}


def test_int_scalar_coerced_to_float():
    out = resolve_modified_fields("limit_option", 100)
    assert out == {"center_modified": 100.0}
    assert isinstance(out["center_modified"], float)


def test_dict_partial_absolute_bounds():
    """dict = 경계 절대값 부분 수정 — 준 키만 *_modified 로."""
    out = resolve_modified_fields("limit_option", {"ucl": 136.0, "lcl": 76})
    assert out == {"ucl_modified": 136.0, "lcl_modified": 76.0}


def test_unknown_keys_and_nonnumeric_ignored_not_fatal():
    """미지 키(sigma 등)·비수치는 경고 후 무시 — 유효분만 산다."""
    out = resolve_modified_fields(
        "limit_option", {"center": 1.0, "sigma": 9.9, "ucl": "wide", "lcl": None})
    assert out == {"center_modified": 1.0}


def test_bool_is_not_a_number():
    """bool ⊂ int 함정 — True 를 1.0 으로 오독하면 안 된다."""
    assert resolve_modified_fields("limit_option", True) == {}
    assert resolve_modified_fields("limit_option", {"center": True}) == {}


def test_non_limit_option_returns_empty():
    """limit_option 한정 — recipe 수정값의 DB 반영은 B5-4 후속."""
    assert resolve_modified_fields("recipe_option", 65.15) == {}
    assert resolve_modified_fields(None, 1.0) == {}


def test_none_and_unsupported_types_empty():
    assert resolve_modified_fields("limit_option", None) == {}
    assert resolve_modified_fields("limit_option", "abc") == {}
    assert resolve_modified_fields("limit_option", [1, 2]) == {}


def test_map_covers_exactly_three_cols():
    """컬럼 화이트리스트 고정 (헌법 6-1) — 늘리려면 계약 갱신이 먼저."""
    assert set(MODIFIED_FIELD_MAP) == {"center", "ucl", "lcl"}
    assert set(MODIFIED_FIELD_MAP.values()) == {"center_modified", "ucl_modified", "lcl_modified"}


# ── _is_unique_violation — SQLSTATE 23505 덕타이핑 ───────────────────────────

class _PgErr(Exception):
    """psycopg2 예외 흉내 — pgcode 속성만 (라이브러리 무의존 판별의 요체)."""

    def __init__(self, pgcode=None):
        super().__init__("dup")
        self.pgcode = pgcode


def test_direct_pgcode_23505():
    assert _is_unique_violation(_PgErr("23505")) is True


def test_wrapped_cause_one_level():
    outer = RuntimeError("wrap")
    outer.__cause__ = _PgErr("23505")
    assert _is_unique_violation(outer) is True


def test_other_pgcode_false():
    assert _is_unique_violation(_PgErr("23503")) is False   # FK 위반은 다른 병


def test_plain_exception_false():
    assert _is_unique_violation(ValueError("x")) is False


def test_two_level_cause_not_followed():
    """명세된 한계 고정 — 원인 체인은 **한 겹만** 본다 (깊어지면 명세부터 바꿀 것)."""
    outer = RuntimeError("wrap")
    mid = RuntimeError("mid")
    mid.__cause__ = _PgErr("23505")
    outer.__cause__ = mid
    assert _is_unique_violation(outer) is False


# ── build_correction_payload — 계약 §6 "이름 삭제 금지" 형태 가드 ─────────────

_CONTRACT_KEYS = {"incident_id", "correction_type", "effective_from", "sensor",
                  "limit_version", "correction_id", "parameter_id", "delta_pct",
                  "recipe_correction_id", "value_proposed"}


def test_limit_payload_keeps_all_contract_keys():
    p = build_correction_payload("INC-1", "limit_option", report={})
    assert set(p) == _CONTRACT_KEYS
    assert p["correction_type"] == "limit"


def test_recipe_payload_keeps_all_contract_keys():
    p = build_correction_payload("INC-1", "recipe_option", report={})
    assert set(p) == _CONTRACT_KEYS
    assert p["correction_type"] == "recipe"


def test_manual_option_is_event_not_correction():
    """정비 승인은 correction 이 아니라 워크플로 이벤트 (2026-08-12 개명 — 구 WaferDispositioned)."""
    p = build_correction_payload("INC-1", "manual_option")
    assert p.get("event") == "MaintenanceApproved"
    assert "correction_type" not in p


def test_modified_value_rides_as_informational():
    """수정값은 정보성으로 payload 에 실린다 — 적용 정본은 *_modified (B COALESCE)."""
    p = build_correction_payload("INC-1", "limit_option", modified_value={"center": -252.1})
    assert p["value_proposed"] == {"center": -252.1}


if __name__ == "__main__":                                      # 수동 실행
    raise SystemExit(pytest.main([__file__, "-q"]))
