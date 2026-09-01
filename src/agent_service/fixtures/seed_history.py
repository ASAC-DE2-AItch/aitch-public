"""fixture 알람에 **사연을 심는다** — 조치 이력 3층 (W9).

**왜 필요한가 (2026-08-06 실측).**
진단기가 짚은 세 구멍(`limit recalc 7%` · `applied_value 0%` · `페어링 로그 9%`)은 알람 JSON 의
결함이 아니라 **DB 에 그 알람의 사연이 없어서**다. 그리고 DB 가 빈 것도 아니다 — 열쇠가 안 맞는다:

    조회 조건(db.py:180)  chamber · recipe_id · step · sensor_window · sensor_id  +  status='PROPOSED'
    실제 DB              recipe_id=C6_1 · status=VERIFIED 가 1,775행 (우리는 C6_0 · PROPOSED 를 찾는다)
    조건에 맞는 9행       **전부 SIM_CH_1** → fixture 46건 중 3건만 걸린다

즉 *"다른 얘기가 잔뜩 들어 있는"* 상태다. 그래서 **우리 fixture 가 실제로 찾는 열쇠**에만 이력을 심는다.

**심는 3층**

  ① `limit_corrections` PROPOSED  — 실력치 재설정 제안이 걸려 있는 상태
       → limit 리포트가 스텁이 아니라 **실물 수치**로 쓰인다 (`shadow_eval` 도 함께)
  ② `recipe_corrections` APPLIED  — **이미 한 번 조정된** 손잡이
       → `applied_value`(승인 화면의 '현재값')가 채워져 **W21 이 비로소 검증된다**
       ⚠ 가스 축(C4)만 심는다. 온도(temp_target)는 명목 앵커가 없어 `applied_value` 가
         **항상 None 인 것이 정상**이다 (D11 — 없는 값을 만들지 않는다)
  ③ 페어링용 이력 — 그 알람이 **실제로 검색해 오는** 사례의 `incident_id` 에 조치 기록
       → 근거 ③층("과거에 뭐가 통했나")이 살아난다
       🔑 ③은 **검색이 결정적이 된 뒤에야 가능**하다 (2026-08-06 tie-break 수정 전에는 같은
          질의가 매번 다른 사례를 돌려줘 "이 알람이 뭘 뽑을지"를 미리 알 수 없었다)

**이건 합성 데이터다.** 시뮬레이터 환경에서 *"B 가 이런 제안을 써뒀다면"* 이라는 평가용 전제를
만드는 것이므로, 실물과 반드시 구분되어야 한다:
  · 모든 행에 `-SEED-` 접두 → `--purge` 로 **정확히 그것만** 지운다 (실데이터 무손상)
  · 멱등 — 같은 키에 이미 행이 있으면 건너뛴다
  · `runs_history.seeded` 에 남아 그 라운드가 시딩 위에서 재졌음이 기록된다

사용:
    python -m src.agent_service.fixtures.seed_history                     # ①② (빠름)
    python -m src.agent_service.fixtures.seed_history --pairing           # ③ 포함 (임베딩 로드)
    python -m src.agent_service.fixtures.seed_history --fixtures <DIR>
    python -m src.agent_service.fixtures.seed_history --purge             # 심은 것만 삭제
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .seed_violations import SEED_RECIPE_ID, SEED_STEP, iter_fixture_alerts

logger = logging.getLogger(__name__)

#: 합성 이력 표지 — `--purge` 는 이 접두만 지운다.
LIMIT_SEED_PREFIX = "LIM-SEED-"
RECIPE_SEED_PREFIX = "RCP-SEED-"

#: 가스 축 손잡이와 그 모니터 센서 (config/recipe_knob_map.yaml — B 소유, 여기선 읽기만).
#  ⚠ 값을 베끼지 않고 파일에서 읽는다 — 손잡이 정본이 갈라지면 W17·W18 사고가 재발한다.
_GAS_KNOB = "C4"

#: 재산정 제안의 크기 — D6 상한(0.5σ) 안. 창작이 아니라 **상한 안의 대표값**이다.
_SEED_DELTA_SIGMA = 0.31
_SEED_CALC_WINDOW_N = 240
#: 섀도 오탐 감소율 — B 실측 예(-50%~+41%) 중 개선 쪽. 미탐은 **B 가 채점하지 않아 NULL 이 실물**이다
#  (계약 §4 · 대기항목 §1 ③ⓑ). 0 을 넣으면 "미탐 0건을 확인했다"는 없는 사실이 생긴다.
_SEED_SHADOW_FAR = 41.0


def _nominal_gas() -> tuple[float, float]:
    """가스 손잡이의 명목값과 '이미 적용된' 값. 명목은 knob_map 이 정본."""
    import yaml

    repo = Path(__file__).resolve().parents[3]
    km = yaml.safe_load((repo / "config" / "recipe_knob_map.yaml").read_text(encoding="utf-8"))
    nominal = float(km["nominal_setpoints"][SEED_RECIPE_ID][_GAS_KNOB])
    # 직전 조정 = 명목 대비 +2.5% (D6 3% 상한 안). "이미 한 스텝 썼다"를 만드는 값.
    return nominal, round(nominal * 1.025, 4)


def _gas_monitors() -> set[str]:
    """가스 축 모니터 센서 — 이 센서가 위반이어야 C4 제안 경로가 열린다."""
    import yaml

    repo = Path(__file__).resolve().parents[3]
    km = yaml.safe_load((repo / "config" / "recipe_knob_map.yaml").read_text(encoding="utf-8"))
    return set(km["axes"]["gas"]["monitors"] or [])


# --- ① limit_corrections PROPOSED ---------------------------------------------
_LIMIT_EXISTS = (
    "SELECT 1 FROM limit_corrections WHERE chamber_id=:ch AND recipe_id=:rc AND step=:st "
    "AND sensor_window=:win AND sensor_id=:sn AND status='PROPOSED' LIMIT 1"
)
_LIMIT_INSERT = (
    "INSERT INTO limit_corrections "
    "(correction_id, incident_id, chamber_id, recipe_id, step, sensor_window, sensor_id, "
    " limit_version_before, center_before, center_after, ucl_after, lcl_after, "
    " delta_pct, delta_sigma, trigger_type, calc_window_n, "
    " shadow_false_alarm_reduction_pct, shadow_missed_detection, status) "
    "VALUES (:cid, :iid, :ch, :rc, :st, :win, :sn, :ver, :cb, :ca, :ucl, :lcl, "
    "        :dpct, :dsig, 'incident', :n, :far, NULL, 'PROPOSED')"
)


def _limit_rows(alert: dict[str, Any]) -> list[dict[str, Any]]:
    """알람의 위반마다 재산정 제안 1행. 수치는 **알람이 들고 온 관리한계에서** 파생한다."""
    rows = []
    for i, v in enumerate(alert.get("violations") or []):
        if not isinstance(v, dict) or not v.get("sensor"):
            continue
        ucl, lcl = v.get("control_limit_upper"), v.get("control_limit_lower")
        if ucl is None or lcl is None:
            continue
        center = (float(ucl) + float(lcl)) / 2.0
        half = (float(ucl) - float(lcl)) / 2.0
        # 위반값 쪽으로 중심을 조금 옮긴 것이 재산정안이다 (노후 = 분포가 이동했다는 판단)
        cur = float(v.get("current_value") or center)
        shift = (1.0 if cur >= center else -1.0) * abs(half) * 0.05
        new_center = round(center + shift, 4)
        rows.append({
            "cid": f"{LIMIT_SEED_PREFIX}{alert['alert_id'].split('-')[-1]}-{i}",
            "iid": None,     # B writer 도 이 컬럼을 안 채운다 — group_key 로 찾는다(대기항목 §1②)
            "ch": alert.get("chamber_id"), "rc": SEED_RECIPE_ID, "st": SEED_STEP,
            "win": v.get("window") or "settled", "sn": v.get("sensor"),
            "ver": v.get("limit_version") or "v1",
            "cb": round(center, 4), "ca": new_center,
            "ucl": round(new_center + half, 4), "lcl": round(new_center - half, 4),
            "dpct": round(shift / center * 100, 4) if center else None,
            "dsig": _SEED_DELTA_SIGMA, "n": _SEED_CALC_WINDOW_N, "far": _SEED_SHADOW_FAR,
        })
    return rows


# --- ② recipe_corrections APPLIED (applied_value) --------------------------------
_RECIPE_EXISTS = (
    "SELECT 1 FROM recipe_corrections WHERE chamber_id=:ch AND recipe_id=:rc AND step=:st "
    "AND parameter_id=:pid AND status IN ('APPLIED','VERIFIED') LIMIT 1"
)
_RECIPE_INSERT = (
    "INSERT INTO recipe_corrections "
    "(recipe_correction_id, incident_id, chamber_id, recipe_id, step, parameter_id, "
    " value_before, value_after, delta_pct, status, applied_at) "
    "VALUES (:rid, :iid, :ch, :rc, :st, :pid, :vb, :va, :dpct, 'APPLIED', NOW())"
)


# --- ③ 페어링용 이력 (검색이 실제로 뽑는 사례) ------------------------------------
_PAIR_EXISTS = "SELECT 1 FROM recipe_corrections WHERE incident_id=:iid LIMIT 1"


def _retrieved_incident_ids(alert_model, top_k: int = 3) -> list[str]:
    """이 알람이 **실제로 검색해 오는** 사례의 incident_id (상위 top_k).

    🔑 검색이 결정적이라 이 목록이 라운드마다 같다 — 그래서 미리 심어둘 수 있다.
    """
    from ..app.tools.recipe import build_case_query
    from ..vectordb import get_qdrant_client, search_kb

    pts = search_kb(get_qdrant_client(), "historical_case", build_case_query(alert_model),
                    limit=top_k)
    out = []
    for p in pts[:top_k]:
        iid = (p.payload or {}).get("incident_id")
        if iid:
            out.append(iid)
    return out


def seed(conn, fixtures_dir: Path | None = None, *, pairing: bool = False) -> dict[str, int]:
    """이력 3층을 채운다. 이미 있으면 건너뛴다(멱등). 호출자가 commit."""
    from sqlalchemy import text

    stat = {"alerts": 0, "limit_rows": 0, "recipe_rows": 0, "pair_rows": 0, "skipped": 0}
    nominal, applied = _nominal_gas()
    monitors = _gas_monitors()

    for alert in iter_fixture_alerts(fixtures_dir):
        stat["alerts"] += 1
        aid = alert["alert_id"]
        ch = alert.get("chamber_id")

        # ① 재산정 제안
        for row in _limit_rows(alert):
            if conn.execute(text(_LIMIT_EXISTS), {k: row[k] for k in ("ch", "rc", "st", "win", "sn")}
                            ).first() is not None:
                stat["skipped"] += 1
                continue
            conn.execute(text(_LIMIT_INSERT), row)
            stat["limit_rows"] += 1

        # ② 직전 적용 setpoint — **가스 축 위반이 있는 알람만**
        sensors = {v.get("sensor") for v in (alert.get("violations") or []) if isinstance(v, dict)}
        if sensors & monitors:
            key = {"ch": ch, "rc": SEED_RECIPE_ID, "st": SEED_STEP, "pid": _GAS_KNOB}
            if conn.execute(text(_RECIPE_EXISTS), key).first() is None:
                conn.execute(text(_RECIPE_INSERT), {
                    **key,
                    "rid": f"{RECIPE_SEED_PREFIX}{aid.split('-')[-1]}",
                    "iid": f"INC-SEED-{ch}-{aid.split('-')[-1]}",
                    "vb": nominal, "va": applied,
                    "dpct": round((applied - nominal) / nominal * 100, 4),
                })
                stat["recipe_rows"] += 1
            else:
                stat["skipped"] += 1

        # ③ 페어링 — 검색이 뽑는 사례에 조치 기록
        if pairing:
            from ..app.schemas.alert import AlertModel
            try:
                model = AlertModel.model_validate(alert)
            except Exception:  # noqa: BLE001 — 파손 fixture
                continue
            for iid in _retrieved_incident_ids(model):
                if conn.execute(text(_PAIR_EXISTS), {"iid": iid}).first() is not None:
                    stat["skipped"] += 1
                    continue
                conn.execute(text(_RECIPE_INSERT), {
                    "rid": f"{RECIPE_SEED_PREFIX}PAIR-{iid.rsplit('-', 1)[-1]}-{ch}",
                    "iid": iid, "ch": ch, "rc": SEED_RECIPE_ID, "st": SEED_STEP,
                    "pid": _GAS_KNOB, "vb": nominal, "va": applied,
                    "dpct": round((applied - nominal) / nominal * 100, 4),
                })
                stat["pair_rows"] += 1
    return stat


def purge(conn) -> dict[str, int]:
    """심은 것만 지운다 — 접두로 식별. **실데이터는 건드리지 않는다.**"""
    from sqlalchemy import text

    n_lim = conn.execute(text("DELETE FROM limit_corrections WHERE correction_id LIKE :p"),
                         {"p": f"{LIMIT_SEED_PREFIX}%"}).rowcount
    n_rec = conn.execute(text("DELETE FROM recipe_corrections WHERE recipe_correction_id LIKE :p"),
                         {"p": f"{RECIPE_SEED_PREFIX}%"}).rowcount
    return {"limit_rows": n_lim, "recipe_rows": n_rec}


def main() -> int:
    """CLI — `python -m src.agent_service.fixtures.seed_history`."""
    import argparse

    from ..app.db import sa_connect

    ap = argparse.ArgumentParser(description="fixture 알람에 조치 이력 심기 (W9)")
    ap.add_argument("--fixtures", type=str, default=None, metavar="DIR",
                    help="알람 디렉토리 (기본 fixtures/alerts) — eval_supervisor --fixtures 와 짝")
    ap.add_argument("--pairing", action="store_true",
                    help="③ 페어링 층까지 심는다 (임베딩 로드로 느림)")
    ap.add_argument("--purge", action="store_true",
                    help="심은 행만 삭제 (-SEED- 접두). 실데이터는 무손상")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    d = Path(args.fixtures) if args.fixtures else None
    if d is not None and not d.is_dir():
        raise SystemExit(f"--fixtures 가 디렉토리가 아니다: {d}")

    with sa_connect() as conn:
        if args.purge:
            stat = purge(conn)
            conn.commit()
            logger.info("삭제 완료 — limit %d행 · recipe %d행 (접두 -SEED- 만)",
                        stat["limit_rows"], stat["recipe_rows"])
            return 0
        stat = seed(conn, d, pairing=args.pairing)
        conn.commit()
    logger.info(
        "이력 시딩 완료 — 알람 %d건 · 재산정 제안 %d행 · 직전적용 %d행 · 페어링 %d행 (기존 스킵 %d)",
        stat["alerts"], stat["limit_rows"], stat["recipe_rows"], stat["pair_rows"], stat["skipped"],
    )
    if not args.pairing:
        logger.info("※ 페어링 층(③)은 안 심었다 — 필요하면 --pairing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
