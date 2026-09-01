# -*- coding: utf-8 -*-
"""fixture 이력 시딩 테스트 (W9 · 2026-08-06).

이 파일이 지키는 것 둘:

  ① **수치를 지어내지 않는다.** 심는 값은 전부 알람이 들고 온 관리한계·knob_map 정본에서
     파생돼야 한다. 시더가 그럴듯한 상수를 박기 시작하면, 라운드는 *우리가 상상한 공정*을
     재게 되고 그 결과는 아무 데도 못 쓴다.

  ② **되돌릴 수 있다.** 합성 이력이 실데이터와 같은 테이블에 산다. `--purge` 가 접두 밖을
     한 행이라도 건드리면 되돌릴 수 없는 사고다 (실측 검증: 1833→1799 · 691→648 정확 복귀).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.agent_service.fixtures import seed_history as SH  # noqa: E402


def _alert(cur, ucl, lcl, sensor="C11", window="settled"):
    return {
        "alert_id": "ALERT-20260713-SIMCH1-0001",
        "chamber_id": "SIM_CH_1",
        "violations": [{"sensor": sensor, "window": window, "rule_id": "N3",
                        "severity": "WARNING", "current_value": cur,
                        "limit_version": "v1",
                        "control_limit_upper": ucl, "control_limit_lower": lcl}],
    }


# --- ① 수치의 출처 -------------------------------------------------------------
def test_center_comes_from_the_alert_not_a_constant() -> None:
    """중심은 알람의 관리한계에서 나온다 — 알람이 다르면 값도 달라야 한다."""
    a = SH._limit_rows(_alert(cur=110, ucl=120, lcl=80))[0]
    b = SH._limit_rows(_alert(cur=1100, ucl=1200, lcl=800))[0]
    assert a["cb"] == 100.0 and b["cb"] == 1000.0     # (ucl+lcl)/2
    assert a["ca"] != b["ca"]


def test_recalc_shifts_toward_the_violation() -> None:
    """재산정안은 위반이 난 쪽으로 움직인다 — '분포가 그쪽으로 이동했다'는 판단이므로.

    반대로 움직이면 리포트의 근거 서술과 수치가 어긋나 LLM 이 모순된 문장을 쓴다.
    """
    up = SH._limit_rows(_alert(cur=118, ucl=120, lcl=80))[0]      # 위쪽 위반
    down = SH._limit_rows(_alert(cur=82, ucl=120, lcl=80))[0]     # 아래쪽 위반
    assert up["ca"] > up["cb"]
    assert down["ca"] < down["cb"]


def test_band_width_is_preserved() -> None:
    """폭은 그대로 두고 중심만 옮긴다 — 폭까지 건드리면 σ 재산정이 되어 다른 얘기가 된다."""
    r = SH._limit_rows(_alert(cur=118, ucl=120, lcl=80))[0]
    assert round(r["ucl"] - r["lcl"], 6) == 40.0


def test_delta_sigma_stays_within_the_cap() -> None:
    """D6 상한(0.5σ) 안이어야 한다 — 넘으면 가드가 강등해 리포트가 수치 없이 나간다."""
    r = SH._limit_rows(_alert(cur=118, ucl=120, lcl=80))[0]
    assert 0 < r["dsig"] <= 0.5


def test_missed_detection_is_null_not_zero() -> None:
    """⚠️ 미탐은 **B 가 채점하지 않아 NULL 이 실물**이다 (계약 §4).

    0 을 넣으면 "미탐 0건을 확인했다"는 **없는 사실**이 생기고, 리포트가 그걸 근거로 쓴다.
    """
    import re
    assert re.search(r"shadow_missed_detection", SH._LIMIT_INSERT)
    assert "NULL, 'PROPOSED'" in SH._LIMIT_INSERT


def test_gas_nominal_is_read_from_knob_map_not_hardcoded() -> None:
    """명목값은 knob_map 정본에서 읽는다 — 베끼면 W17·W18(손잡이 정본 이탈) 재발이다."""
    import yaml

    nominal, applied = SH._nominal_gas()
    km = yaml.safe_load((REPO_ROOT / "config" / "recipe_knob_map.yaml").read_text(encoding="utf-8"))
    assert nominal == float(km["nominal_setpoints"]["C6_0"]["C4"])
    assert applied > nominal                                   # '이미 한 스텝 썼다'
    assert (applied - nominal) / nominal * 100 <= 3.0          # D6 3% 상한 안


def test_gas_monitors_come_from_knob_map() -> None:
    """가스 축 모니터도 정본에서 — 이 집합이 틀리면 applied_value 를 엉뚱한 알람에 심는다."""
    assert SH._gas_monitors() == {"C16", "C15"}


def test_no_rows_when_limits_missing() -> None:
    """관리한계가 없으면 **아무것도 만들지 않는다** — 없는 값을 지어내는 대신 비운다."""
    a = _alert(cur=1, ucl=None, lcl=None)
    assert SH._limit_rows(a) == []


# --- ② 되돌릴 수 있는가 (PG 필요) ------------------------------------------------
def _conn_or_skip():
    try:
        from src.agent_service.app.db import sa_connect
        cm = sa_connect()
        return cm, cm.__enter__()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres 미기동 — 시딩/삭제 축 미검증: {exc}")


def test_purge_touches_only_the_seed_prefix() -> None:
    """🔴 **실데이터가 한 행도 줄면 안 된다.** 합성 이력이 같은 테이블에 살기 때문이다."""
    from sqlalchemy import text

    cm, conn = _conn_or_skip()
    try:
        real = conn.execute(text(
            "SELECT count(*) FROM limit_corrections WHERE correction_id NOT LIKE :p"),
            {"p": f"{SH.LIMIT_SEED_PREFIX}%"}).scalar()
        SH.seed(conn)          # 심고
        SH.purge(conn)         # 지우고
        after = conn.execute(text(
            "SELECT count(*) FROM limit_corrections WHERE correction_id NOT LIKE :p"),
            {"p": f"{SH.LIMIT_SEED_PREFIX}%"}).scalar()
        assert after == real, "purge 가 실데이터를 건드렸다"
    finally:
        conn.rollback()
        cm.__exit__(None, None, None)


def test_seed_is_idempotent() -> None:
    """두 번 심어도 두 배가 되지 않는다 — 라운드 전에 습관적으로 돌릴 도구다."""
    cm, conn = _conn_or_skip()
    try:
        SH.purge(conn)
        first = SH.seed(conn)
        second = SH.seed(conn)
        assert first["limit_rows"] > 0, "첫 시딩이 아무것도 안 심었다"
        assert second["limit_rows"] == 0, "재실행이 중복 삽입했다"
    finally:
        conn.rollback()
        cm.__exit__(None, None, None)
