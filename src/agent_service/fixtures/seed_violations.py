"""fixture 알람 ↔ `spc_violations` 시딩 — 라운드에서 recipe 축을 살리는 전제 (W9-0).

**왜 필요한가 (2026-08-05 발견).**
라운드는 fixture 를 파일에서 재생(replay)한다. 운영 경로에서는 B publisher 가 알람을 발행하기
**전에** `insert_violations()` 로 `spc_violations` 에 적재하고, 우리 파이프라인은 그 행을
`resolve_alert_context(conn, alert_id)` 로 조회해 `recipe_id`·`step` 을 얻는다. 재생 경로에는
그 적재 단계가 없어 조회가 0건이 되고, 엔진이 *"recipe_id=None · knob=C4 명목값 조회 불가"* 로
에스컬해 **recipe 옵션이 단 한 건도 수치 제안을 못 했다** (round11~15 전부 42/42 에스컬).

`recipe_id` 를 알람 payload 에 싣는 방식은 쓰지 않는다 — 계약 §3 은 *"`lot_id`·`recipe_id` 는
`wafer_id` 종속이라 이벤트에 싣지 않고 wafer 마스터 조인으로 해석한다"*(A 결정 2026-07-23)이고,
fixture 만 실제 알람과 다른 모양이 되면 측정이 또 무의미해진다. 그래서 **알람은 그대로 두고
조회 대상 행을 만든다** — 운영과 같은 경로를 타게 하는 것이 목적이다.

**컬럼 값의 출처**: 전부 fixture 의 `violations[]` 에서 나온다. 새로 정하는 것은 두 개뿐:
  · `recipe_id` = `C6_0`  — knob_map `nominal_setpoints` 에 있는 recipe 이고 실 DB 8,498행의 최빈값
  · `step`      = 4       — 실 DB 최빈 step(3,630행). 감도 통계·정착 구간의 기준 step 이다
둘을 fixture 마다 다르게 주는 것은 **W9(다양성)의 몫**이지 여기서 섞지 않는다 — 지금은
"recipe 축이 살아나면 판정이 어떻게 변하나" 하나만 재는 단일 변수 변경이다.

**멱등**: 이미 행이 있는 alert_id 는 건너뛴다. 지우고 다시 넣지 않는다 —
운영 행과 fixture 행이 같은 테이블에 살고, 실수로 실데이터를 지우면 되돌릴 수 없다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parent / "alerts"

# fixture 알람에는 없는 두 값 — 운영 DB 최빈값을 따른다(위 docstring 참조).
SEED_RECIPE_ID = "C6_0"
SEED_STEP = 4

# fixture alert_id 접두 — 이 접두가 아닌 행은 손대지 않는다(실데이터 보호).
_FIXTURE_ALERT_PREFIX = "ALERT-2026"

_SELECT_EXISTING = "SELECT 1 FROM spc_violations WHERE alert_id = :aid LIMIT 1"

# 컬럼은 publisher._INSERT_VIOLATION 과 같은 집합이다 — 운영 적재와 모양을 맞춘다.
_INSERT = (
    "INSERT INTO spc_violations "
    "(alert_id, chamber_id, recipe_id, step, sensor_window, rule_id, sensor_id, severity, "
    " current_value, limit_version, control_limit_upper, control_limit_lower, "
    " context_score, description) "
    "VALUES (:alert_id, :ch, :rc, :st, :win, :rule, :sn, :sev, "
    "        :cur, :ver, :ucl, :lcl, :score, :desc)"
)


def iter_fixture_alerts(fixtures_dir: Path | None = None) -> Iterable[dict[str, Any]]:
    """fixture 디렉토리의 알람 JSON 을 순회한다. 파손분(46_edge_BROKEN)은 건너뛴다.

    파손 fixture 는 역직렬화 실패를 보는 재료라(헌법 6-2) 여기서도 **스킵하고 로그만** 남긴다 —
    시딩이 죽으면 라운드 자체를 못 돌린다.
    """
    d = fixtures_dir or FIXTURES_DIR
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — 파손 fixture 는 의도된 재료
            logger.info("시딩 스킵(파손 fixture): %s (%s)", path.name, type(exc).__name__)
            continue
        if not isinstance(data, dict) or not data.get("alert_id"):
            logger.info("시딩 스킵(alert_id 없음): %s", path.name)
            continue
        yield data


def _rows_for(alert: dict[str, Any]) -> list[dict[str, Any]]:
    """알람 1건 → `spc_violations` 행들 (위반 1건 = 1행, 운영 적재와 같은 규칙)."""
    rows = []
    for v in alert.get("violations") or []:
        if not isinstance(v, dict):
            continue
        rows.append({
            "alert_id": alert["alert_id"],
            "ch": alert.get("chamber_id"),
            "rc": SEED_RECIPE_ID,
            "st": SEED_STEP,
            "win": v.get("window") or "settled",
            "rule": v.get("rule_id"),
            "sn": v.get("sensor"),
            "sev": v.get("severity"),
            "cur": v.get("current_value"),
            "ver": v.get("limit_version") or "v1",
            "ucl": v.get("control_limit_upper"),
            "lcl": v.get("control_limit_lower"),
            "score": alert.get("context_score"),
            "desc": v.get("description"),
        })
    return rows


def seed(conn, fixtures_dir: Path | None = None) -> dict[str, int]:
    """fixture 알람의 `spc_violations` 행을 채운다. 이미 있으면 건너뛴다(멱등).

    Args:
        conn: SQLAlchemy connection (호출자가 트랜잭션·commit 소유 — `insert_violations` 선례).
        fixtures_dir: 알람 디렉토리. None 이면 기본 `fixtures/alerts`.

    Returns:
        {"alerts": 본 알람 수, "seeded": 새로 심은 알람 수, "skipped": 이미 있던 알람 수,
         "rows": 삽입한 행 수} — 호출자가 로그로 남긴다.
    """
    from sqlalchemy import text

    stat = {"alerts": 0, "seeded": 0, "skipped": 0, "rows": 0}
    for alert in iter_fixture_alerts(fixtures_dir):
        aid = alert["alert_id"]
        stat["alerts"] += 1
        if not aid.startswith(_FIXTURE_ALERT_PREFIX):
            logger.warning("시딩 스킵(접두 불일치 — 실데이터 보호): %s", aid)
            stat["skipped"] += 1
            continue
        if conn.execute(text(_SELECT_EXISTING), {"aid": aid}).first() is not None:
            stat["skipped"] += 1
            continue
        rows = _rows_for(alert)
        if not rows:
            logger.info("시딩 스킵(위반 0건): %s", aid)
            continue
        conn.execute(text(_INSERT), rows)
        stat["seeded"] += 1
        stat["rows"] += len(rows)
    return stat


def main() -> int:
    """CLI — `python -m src.agent_service.fixtures.seed_violations`."""
    import argparse

    from src.agent_service.app.db import sa_connect

    ap = argparse.ArgumentParser(description="fixture 알람 ↔ spc_violations 시딩 (W9-0)")
    ap.add_argument("--fixtures", type=str, default=None, metavar="DIR",
                    help="알람 디렉토리 (기본: fixtures/alerts) — eval_supervisor --fixtures 와 짝")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    d = Path(args.fixtures) if args.fixtures else None
    if d is not None and not d.is_dir():
        raise SystemExit(f"--fixtures 가 디렉토리가 아니다: {d}")

    with sa_connect() as conn:
        stat = seed(conn, d)
        conn.commit()
    logger.info(
        "시딩 완료 — 알람 %d건 중 신규 %d · 기존 %d · 행 %d",
        stat["alerts"], stat["seeded"], stat["skipped"], stat["rows"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
