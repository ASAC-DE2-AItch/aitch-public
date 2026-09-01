"""B3-2 Task3: `control_limits` 현행(is_active) 행 → NelsonEngine 소비 형태 로더.

읽기 전용·단순 조회 모듈. `load(engine)`는 `control_limits`에서 `is_active=true`
행을 읽어 `(limits, whitelist)` 튜플로 변환한다. gk(group_key) 형태의 단일 소스는
이 모듈이며, `nelson_engine.Point.group_key`·`seed_control_limits.GROUP_KEYS`와
동일 순서(chamber, recipe, step, sensor_window, sensor_id)를 따른다.

DB 접속은 `load()` 호출 시점에만 발생한다 — import 시 부작용 없음(헌법 6-1).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# is_reset_point (S2·D6-b): 이 활성 행이 "레짐 수립 지점"인지 파생. TTTM feed flush 제외 판별용
#   (정기 리캘리는 같은 운전점에서 자만 다시 잰 것 → 옛 점 유효 → window 유지). 술어 ⓐ+ⓑ:
#   ⓐ 리셋-타입 correction(qual/incident/APPROVED_APPLIED)의 limit_version_after == 이 행 버전
#   ⓑ initial·v1 (limit_corrections에 대응 행이 없어 ⓐ만으론 False가 됨 → OR로 보정)
#   EXISTS 사용 — JOIN이면 fan-out 시 loader가 마지막 행으로 조용히 덮어씀(ORDER BY 없음).
_SELECT_ACTIVE_LIMITS = text(
    """
    SELECT cl.chamber_id, cl.recipe_id, cl.step, cl.sensor_window, cl.sensor_id,
           cl.method, cl.center, cl.sigma, cl.ucl, cl.lcl, cl.limit_version,
           (EXISTS (SELECT 1 FROM limit_corrections lc
                     WHERE lc.chamber_id = cl.chamber_id AND lc.recipe_id = cl.recipe_id
                       AND lc.step = cl.step AND lc.sensor_window = cl.sensor_window
                       AND lc.sensor_id = cl.sensor_id
                       AND lc.limit_version_after = cl.limit_version
                       AND (lc.trigger_type IN ('qual', 'incident')
                            OR lc.status = 'APPROVED_APPLIED'))
            OR (cl.trigger_type = 'initial' AND cl.limit_version = 'v1')) AS is_reset_point
    FROM control_limits cl
    WHERE cl.is_active = true
    """
)


def load(engine: Engine) -> tuple[dict[tuple, dict], set[tuple]]:
    """`control_limits`의 현행(is_active) 행을 읽어 `(limits, whitelist)`로 변환한다.

    각 행을 gk = (chamber_id, recipe_id, step, sensor_window, sensor_id)로 묶어
    `limits[gk]`에 NelsonEngine이 읽는 6키(center/sigma/ucl/lcl/method/limit_version)만
    담고, `whitelist`에 gk를 추가한다. `step`은 DB SMALLINT를 방어적으로 `int()`
    변환해 `Point.group_key`와 타입을 못박는다(계약 — 문자열로 새면 매칭이 조용히
    0건이 된다). 읽기 전용이라 `conn.begin()` 없이 `engine.connect()`만 사용한다.
    """
    limits: dict[tuple, dict] = {}
    whitelist: set[tuple] = set()

    with engine.connect() as conn:
        result = conn.execute(_SELECT_ACTIVE_LIMITS)
        for row in result:
            m = row._mapping
            gk = (
                m["chamber_id"],
                m["recipe_id"],
                int(m["step"]),
                m["sensor_window"],
                m["sensor_id"],
            )
            limits[gk] = {
                "center": m["center"],
                "sigma": m["sigma"],
                "ucl": m["ucl"],
                "lcl": m["lcl"],
                "method": m["method"],
                "limit_version": m["limit_version"],
                "is_reset_point": bool(m["is_reset_point"]),   # S2·D6-b (소비자=S3 feed flush)
            }
            whitelist.add(gk)

    logger.info("control_limits 로드 완료: 그룹 %d개", len(whitelist))
    return limits, whitelist
