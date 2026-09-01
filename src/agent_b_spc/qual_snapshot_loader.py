"""B5-2: Qual 스냅샷 로더 + group_key 정규화 (읽기 전용).

`qual_snapshots`의 `is_active` 스냅샷 `sensor_stats` JSONB를 gk 튜플 키 dict로 파싱해
`evaluate_qual`이 control_limits와 동일하게 소비하게 한다. `control_limits_loader.load` 패턴 계승.

⚠️ group_key 타입 계약 — JSONB는 키를 str로 역직렬화하므로 로더 파싱과 호출자의 gk 생성이
반드시 같은 `normalize_group_key()`를 거친다(step int/str 불일치 → 튜플 매칭 0건 → 무조건
UNKNOWN 회귀). 공유 헬퍼로 divergence를 구조적으로 차단 (control_limits_loader의 int(step) 계약과 정합).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# 스냅샷 dict가 담는 판정 5키 (limits[gk]와 핵심 동일 — n·limit_version 불요)
_SNAP_KEYS = ("center", "sigma", "ucl", "lcl", "method")

# is_active 스냅샷 1행 — 다중 활성 방어(부분 유니크 인덱스 부재 §5-1): 최신(created_at DESC) 1개
_SELECT_ACTIVE = text(
    """
    SELECT sensor_stats
    FROM qual_snapshots
    WHERE chamber_id = :ch AND is_active
    ORDER BY created_at DESC
    LIMIT 1
    """
)


def normalize_group_key(chamber, recipe, step, window, sensor) -> tuple:
    """gk 5-tuple 정규화 — `step`은 int 강제, 나머지 str (로더·호출자 공용 계약, §5-2).

    JSONB 키(str step)와 in-memory 호출자 키(int step)를 같은 튜플로 못박아 매칭 divergence 차단.
    """
    return (str(chamber), str(recipe), int(step), str(window), str(sensor))


def _parse_gk_str(gk_str: str) -> tuple:
    """"chamber|recipe|step|window|sensor" (5필드 파이프) → normalize_group_key."""
    parts = gk_str.split("|")
    if len(parts) != 5:
        raise ValueError(f"gk_str는 5필드(chamber|recipe|step|window|sensor)여야 함: {gk_str!r}")
    return normalize_group_key(*parts)


def load_active_snapshot(conn, chamber_id: str) -> dict[tuple, dict]:
    """챔버의 활성 스냅샷 → `dict[gk, {center,sigma,ucl,lcl,method}]`. 없으면 빈 dict.

    ⚠️ 테스트 용이성을 위해 engine이 아니라 **conn(executable)**을 받는다(reset_ref 패턴 —
    호출자 트랜잭션 내 조회). 읽기 전용, commit 안 함. 다중 활성 시 최신 1개.
    """
    row = conn.execute(_SELECT_ACTIVE, {"ch": chamber_id}).mappings().first()
    if row is None:
        return {}
    stats = row["sensor_stats"]          # JSONB → dict (psycopg2 자동 파싱)
    out: dict[tuple, dict] = {}
    for gk_str, s in stats.items():
        out[_parse_gk_str(gk_str)] = {key: s[key] for key in _SNAP_KEYS}
    logger.info("qual 스냅샷 로드: chamber=%s 그룹 %d개", chamber_id, len(out))
    return out
