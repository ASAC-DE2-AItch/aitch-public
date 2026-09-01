"""Postgres 연결 + KB 로그 페어링 조회 (C5-2).

역할 경계 (`config/kb_schema.yaml:9` — 2026-07-10 개정):
    **KB = 근거 서사 + 의미검색(Qdrant) · Postgres = 정밀 수치 + 정확 필터 + 통계 + 감사**

`limit_change_log`·`recipe_r2r_log` 는 "비슷한 걸 찾는" 대상이 아니라 "그 사건의 수치를 정확히
꺼내는" 대상이라 벡터 KB 에서 제외됐다. 그래서 근거 조회가 2단이다:

    historical_case 벡터 검색  →  payload.incident_id  →  이 모듈  →  정밀 수치
      (무슨 일이 있었나 · 서사)                            (얼마나 바꿨나 · 섀도 실적)

이 모듈은 **읽기 전용**이다. 리포트 적재(agent_reports 등, 계약 §5)는 담당·일정 확인 후 별도.

무중단 (헌법 6-2): 연결 실패·조회 실패는 예외를 삼키고 빈 결과 + 경고를 낸다. Postgres 가
없어도 파이프라인은 돌아야 하며, 로그가 없으면 리포트 근거가 얕아질 뿐이다(§0 정직 반영).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# 접속 정보는 배포마다 바뀌는 인프라 값이라 .env (params.yaml 은 알고리즘 값 — config.py 주석 참조).
# 기본값은 docker-compose.yml 의 postgres 서비스와 일치시킨다.
DSN_ENV = "AGENT_PG_DSN"
_DEFAULT_DSN = "postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform"

# 연결 타임아웃 (초) — **무중단의 핵심**. 없으면 Postgres 미기동 시 OS 기본 타임아웃(수십 초~분)
# 까지 파이프라인이 멈춘다. "빈 결과로 진행"이 의미가 있으려면 빨리 포기해야 한다.
# 리포트 지연 예산(D4 slow_path_max_sec=30) 안에서 근거 조회가 전체를 잡아먹지 않도록 짧게.
#
# params `agent.pg_connect_timeout_sec`(D24)에서 읽는다 (헌법 6-4 — 매직 넘버는 전부 params).
# **모듈 속성으로 남기는 이유**: 테스트가 `monkeypatch.setattr(db, "CONNECT_TIMEOUT_SEC", 1)` 로
# 짧게 줄여 미기동 경로를 검증한다(test_pipeline_fanout). 함수로 바꾸면 그 축이 깨진다.
# import 시점에 `require` 로 읽으므로 키가 없으면 **즉시 명시적 에러** — 조용한 기본값 금지(6-1).
from .config import load_settings  # noqa: E402 — 순환 없음(config 는 db 를 임포트하지 않는다)

CONNECT_TIMEOUT_SEC = int(load_settings().require("agent.pg_connect_timeout_sec"))


def dsn() -> str:
    """Postgres 접속 문자열 — .env 우선, 없으면 로컬 compose 기본값."""
    return os.environ.get(DSN_ENV) or _DEFAULT_DSN


@contextmanager
def connect() -> Iterator[Any]:
    """psycopg 연결 컨텍스트. 호출자가 예외를 처리한다(시딩 스크립트용).

    파이프라인 경로에서는 이 함수를 직접 쓰지 말고 fetch_* 를 쓴다 — 그쪽이 무중단이다.
    """
    import psycopg  # 지연 임포트 — Postgres 없이도 모듈 임포트는 되게

    conn = psycopg.connect(dsn(), connect_timeout=CONNECT_TIMEOUT_SEC)
    try:
        yield conn
    finally:
        conn.close()


# --- B 리졸버용 SQLAlchemy 커넥션 (S3 배선, 2026-08-04) --------------------------
# ⚠️ **왜 스택이 둘인가.** 위 `connect()` 는 psycopg 인데, B 가 제공한 리졸버 3종
#   (`recipe_writer.resolve_alert_context` · `resolve_applied_setpoints` ·
#    `resolve_temp_tuned_this_incident`)은 **SQLAlchemy 커넥션**을 받는다(`conn.execute(...).mappings()`).
#
#   대안은 그 쿼리 3개를 psycopg 로 재구현하는 것이었으나 **하지 않는다** — `recipe_corrections`·
#   `spc_violations` 는 B 소유 도메인이고, 베껴 쓰면 정본이 둘이 되어 갈라진다(2026-08-04 W17·W18
#   이 정확히 그 사고였다). B 도 *"C 가 `compute_tuning(...)`에 주입할 값을 B 가 대신 조회한다"* 로
#   대행을 명시했다. 스택 공존 선례는 이미 있다 — psycopg2/psycopg3(requirements.txt:59 체크포인터).
#
#   엔진은 **호출 시마다 만들지 않고 캐시**한다(pool 재사용). 연결 타임아웃은 위와 같은 정신 —
#   Postgres 미기동 시 빨리 포기해야 "빈 결과로 진행"이 의미를 갖는다.
_sa_engine = None


def sa_engine():
    """B 리졸버용 SQLAlchemy 엔진 (지연 생성·캐시). 실패는 호출자가 처리한다."""
    global _sa_engine
    if _sa_engine is None:
        from sqlalchemy import create_engine  # 지연 임포트 — 없어도 모듈 임포트는 되게

        _sa_engine = create_engine(
            dsn(),
            pool_pre_ping=True,               # 끊긴 커넥션을 조용히 물려주지 않는다
            connect_args={"connect_timeout": CONNECT_TIMEOUT_SEC},
        )
    return _sa_engine


@contextmanager
def sa_connect() -> Iterator[Any]:
    """B 리졸버에 넘길 SQLAlchemy 커넥션. **읽기 전용 용도** — commit 하지 않는다."""
    conn = sa_engine().connect()
    try:
        yield conn
    finally:
        conn.close()


# --- 페어링 조회 (읽기 전용·무중단) ---------------------------------------------
# historical_case 히트의 incident_id 로 조치로그를 꺼낸다.
# 컬럼을 * 로 긁지 않고 명시한다 — 리포트 근거에 실제로 쓰는 값만 가져오고,
# 테이블에 컬럼이 추가돼도 payload 모양이 흔들리지 않게.
_LIMIT_COLS = (
    "correction_id", "incident_id", "chamber_id", "sensor_id", "sensor_window",
    "limit_version_before", "limit_version_after",
    "center_before", "center_after", "delta_pct", "delta_sigma",
    "trigger_type", "status",
    "shadow_false_alarm_reduction_pct", "shadow_missed_detection",
)
_RECIPE_COLS = (
    "recipe_correction_id", "incident_id", "chamber_id", "recipe_id", "step",
    "parameter_id", "value_before", "value_after", "delta_pct", "status", "verify_result",
)


def _fetch(table: str, cols: tuple[str, ...], incident_ids: list[str]) -> list[dict[str, Any]]:
    """incident_id IN (...) 조회. 실패하면 빈 리스트 + 경고 (무중단)."""
    if not incident_ids:
        return []
    try:
        import psycopg
        from psycopg.rows import dict_row

        sql = f"SELECT {', '.join(cols)} FROM {table} WHERE incident_id = ANY(%s)"
        with psycopg.connect(
            dsn(), row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_SEC
        ) as conn, conn.cursor() as cur:
            cur.execute(sql, (list(incident_ids),))
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:  # 연결/조회 실패 — 근거만 얕아지고 파이프라인은 산다
        logger.warning("%s 조회 실패 — 로그 근거 없이 진행 (%s)", table, exc)
        return []


def incident_ids_of(cases: list[dict[str, Any]] | None) -> list[str]:
    """historical_case 검색 결과에서 페어링 키를 뽑는다 (없는 건 조용히 제외).

    신규 데이터엔 incident_id 가 없을 수 있다(kb_schema.yaml:123 — "발번 필수").
    그런 사례는 로그 페어링 없이 서사 근거로만 쓰인다.
    """
    out: list[str] = []
    for c in cases or []:
        iid = (c or {}).get("incident_id")
        if isinstance(iid, str) and iid:
            out.append(iid)
    return list(dict.fromkeys(out))  # 중복 제거·순서 유지


def fetch_limit_logs(incident_ids: list[str]) -> list[dict[str, Any]]:
    """실력치 재설정 조치로그 — limit tool 의 근거 ③(과거에 무엇이 통했나)."""
    return _fetch("limit_corrections", _LIMIT_COLS, incident_ids)


# --- B 재산정 제안 조회 (S1 배선, 2026-08-04) ------------------------------------
# limit 리포트의 **수치 소스**다. 계약 §5 가 "옵션②는 B 실력치 재산정 API 산출값을 그대로
# 사용 — C 가 수치를 재계산·수정하는 것 금지"라고 못박아 두었으므로, 우리는 B 가 이미 써둔
# 행을 **읽기만** 한다. B 엔진(`recompute_group`)을 직접 부르지 않는다 — 부르려면 최근 N 장
# 표본이 필요한데 그건 B consumer 인메모리에만 있고 DB 어디에도 없다(2026-08-04 확인).
#
# **왜 incident_id 로 안 찾나.** B 의 writer 가 그 컬럼을 채우지 않는다(recalc_writer.py 의
# INSERT 컬럼 목록에 없음). 대신 재산정의 실제 단위는 **group_key 5종**이고 B 자신도 그걸로
# 찾는다(`_proposed_groups`). 그래서 같은 키로 맞춘다.
_RECALC_SQL = """
SELECT lc.correction_id, lc.chamber_id, lc.recipe_id, lc.step,
       lc.sensor_window, lc.sensor_id, lc.limit_version_before,
       lc.center_before, lc.center_after, lc.ucl_after, lc.lcl_after,
       lc.delta_pct, lc.delta_sigma, lc.trigger_type, lc.calc_window_n,
       lc.shadow_false_alarm_reduction_pct, lc.shadow_missed_detection,
       cl.method
  FROM limit_corrections lc
  LEFT JOIN control_limits cl
         ON cl.chamber_id = lc.chamber_id AND cl.recipe_id = lc.recipe_id
        AND cl.step = lc.step AND cl.sensor_window = lc.sensor_window
        AND cl.sensor_id = lc.sensor_id AND cl.is_active
 WHERE lc.chamber_id = %s AND lc.recipe_id = %s AND lc.step = %s
   AND lc.sensor_window = %s AND lc.sensor_id = %s
   AND lc.status = 'PROPOSED'
 ORDER BY lc.created_at DESC, lc.id DESC
 LIMIT 1
"""


def fetch_recalc_proposal(chamber_id: str, recipe_id: str, step: int,
                          sensor_window: str, sensor_id: str) -> dict[str, Any] | None:
    """B 가 써둔 **승인대기(PROPOSED)** 재산정 제안 1건 — 없으면 None.

    `status='PROPOSED'` 만 본다. `AUTO_APPLIED` 는 이미 적용이 끝난 것이라 "제안"이 아니고
    (헌법 1-1 정기 리캘리 자동 적용 예외), 승인 화면에 올릴 대상이 아니다.

    **None 이 흔한 게 정상이다.** B 의 정기 재산정은 챔버가 조용할 때만 돌고(G1 NORMAL·
    G3 미종결 incident 없음), 우리 리포트는 알람이 왔을 때 만들어진다 — 두 시점이 구조적으로
    어긋난다. None 이면 프롬프트 규칙6(창작 금지)이 발동해 "재산정안 없음"으로 나간다.

    `method`(sigma|quantile)는 제안 행에 없어 활성 `control_limits` 에서 join 해 온다 —
    1-1 예외 2(KEEP\\* 분위수 그룹)를 리포트가 구분해야 하기 때문.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(
            dsn(), row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_SEC
        ) as conn, conn.cursor() as cur:
            cur.execute(_RECALC_SQL,
                        (chamber_id, recipe_id, step, sensor_window, sensor_id))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as exc:  # 조회 실패 — 수치 없이 진행(창작보다 null 이 정직하다)
        logger.warning("재산정 제안 조회 실패 — 수치 없이 진행 (%s: %s)",
                       type(exc).__name__, exc)
        return None


def fetch_recipe_logs(incident_ids: list[str]) -> list[dict[str, Any]]:
    """레시피 튜닝 조치로그 — recipe tool 의 근거 ③."""
    return _fetch("recipe_corrections", _RECIPE_COLS, incident_ids)


# --- wafer 처분 입력: wafer별 예측 수치 (읽기 전용·무중단) ------------------------
# `disposition.decide_wafer` 는 wafer **한 장마다** predicted_c65·anomaly_score 를 비교한다
# (판정 트리 STEP 1~4). alert 의 `prediction_context` 는 **대표 wafer 1장** 스냅샷이라
# 구간 전체를 판정할 수 없다 — 축이 다르다.
#
# 정본 소스는 `wafer_predictions`(A sink 가 `fdc.prediction`·`fdc.actual` 을 UPSERT 적재,
# 계약 §0 consumer-group-db-sink). B 도 같은 테이블을 Y-임계 산출에 read 하므로
# 타 팀 테이블 조회는 이미 팀 관행이다. 이 모듈은 **읽기 전용**이고 스키마를 건드리지 않는다.

_PREDICTION_COLS = ("wafer_id", "predicted_c65", "anomaly_score")


def _scan_limit() -> int:
    """챔버 스캔 상한 — params `agent.prediction_scan_limit` (D23).

    `member_wafers` 가 없을 때 최근 몇 행까지 훑을지다. 의심 윈도우는 소급 구간이라 과거
    wafer 를 덮어야 하는데, 무한정 긁으면 오래된 레짐의 수치가 섞인다.

    ⚠️ 코드 상수로 두지 않는 이유(헌법 6-4 — 매직 넘버는 전부 params.yaml): 튜닝이 config PR
    로 끝나야 한다(PM 권고 2026-07-30, PR #65). `require` 를 쓰므로 키가 없으면 명시적 에러다
    — 조용한 기본값으로 전 이력을 훑는 쪽이 더 나쁘다(6-1).
    """
    from .config import load_settings

    return int(load_settings().require("agent.prediction_scan_limit"))


def fetch_predictions(
    chamber_id: str,
    wafer_ids: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, dict[str, Any]]:
    """wafer별 예측 수치 → `{wafer_id: {predicted_c65, anomaly_score}}`.

    조회 모드가 둘이다 — **목록을 알면 정확히, 모르면 최근분을 훑어** 호출측이 거른다.

    Args:
        chamber_id: 대상 챔버. 목록 조회일 때도 검증용으로 받는다.
        wafer_ids: `suspect_window.member_wafers`(B 발행). 있으면 **이 목록만** 조회한다 —
            B 가 위반 판정에 실제로 쓴 레코드라 가장 정확하다.
            없으면 챔버 최근 `limit` 행을 훑고, 구간 필터는 `select_in_window` 가 한다.
        limit: 챔버 스캔 모드에서만 쓰는 상한. None 이면 params `agent.prediction_scan_limit`
            (D23) 을 읽는다 — 호출측이 명시적으로 좁히고 싶을 때만 넘긴다.

    Returns:
        wafer당 **최신 1행**(`DISTINCT ON`). sink 가 부분 필드를 여러 번 병합할 수 있어
        (C65 먼저·AE 나중) 같은 wafer 에 행이 여러 개 생길 수 있다.
        조회 실패·미기동이면 **빈 dict** — 판정은 "예측 조회 불가 → HOLD"(원위치)로 간다.
    """
    # 스캔 상한은 **try 밖에서** 해석한다 — params 누락(설정 오류)이 아래 except 에 삼켜지면
    # "DB 조회 실패"로 오보되고, 원인이 로그 한 줄에도 안 남는다. 설정 오류는 시끄러워야 한다
    # (헌법 6-1 조용한 기본값 금지). 목록 조회 모드는 상한을 쓰지 않으므로 읽지도 않는다.
    scan_limit = None if wafer_ids else (int(limit) if limit is not None else _scan_limit())

    try:
        import psycopg
        from psycopg.rows import dict_row

        cols = ", ".join(_PREDICTION_COLS)
        if wafer_ids:
            sql = (
                f"SELECT DISTINCT ON (wafer_id) {cols} FROM wafer_predictions "
                "WHERE wafer_id = ANY(%s) ORDER BY wafer_id, id DESC"
            )
            params: tuple[Any, ...] = (list(dict.fromkeys(wafer_ids)),)
        else:
            # 최근 N행을 먼저 자른 뒤 wafer 별 최신을 고른다 — 순서를 뒤집으면 전 이력을 훑는다.
            sql = (
                f"SELECT DISTINCT ON (wafer_id) {cols} FROM ("
                f"  SELECT {cols}, id FROM wafer_predictions"
                "   WHERE chamber_id = %s ORDER BY id DESC LIMIT %s"
                ") t ORDER BY wafer_id, id DESC"
            )
            params = (chamber_id, scan_limit)

        with psycopg.connect(
            dsn(), row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_SEC
        ) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception as exc:  # 미기동·조회 실패 — 전 wafer HOLD(가역)로 가고 파이프라인은 산다
        logger.warning(
            "wafer_predictions 조회 실패 — 처분은 '조회 불가'(HOLD) 경로로 진행 (%s)", exc
        )
        return {}

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        wid = r.get("wafer_id")
        if isinstance(wid, str) and wid:
            out[wid] = {
                "predicted_c65": r.get("predicted_c65"),
                "anomaly_score": r.get("anomaly_score"),
            }
    logger.debug("wafer_predictions 조회: chamber=%s 요청=%s → %d건",
                 chamber_id, len(wafer_ids) if wafer_ids else "최근", len(out))
    return out


# =============================================================================
# RTD 해제 근거 Brief 재료 (인프라 스텝 3 · #140 — 읽기 전용·무중단)
# =============================================================================
# 회고 3층의 조달 경로. **`chamber_inhibits` 는 PM 소유 테이블이고 우리는 읽기만 한다**
# (3-1 — 소유는 PM, 조회는 자유. `wafer_predictions`(A 소유) 선례와 같다).
#
# ⚠️ **정지 이후 지표는 여기 없다.** 정지 중에는 그 챔버 wafer 가 0장이라
#    (`src/simulator/kafka_producer.py` 의 inhibit 스킵) `spc_violations` 도 더 안 쌓인다.
#    그래서 ⓑ 는 **정지 시각 이전** 창만 본다 — 사후 개선을 재는 축은 존재하지 않는다.


@contextmanager
def _dict_conn() -> Iterator[Any]:
    """dict_row psycopg 커넥션 — 아래 fetch_* 가 공유하는 짧은 수명 연결."""
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn(), row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_SEC)
    try:
        yield conn
    finally:
        conn.close()


_OPEN_INHIBIT_SQL = """
SELECT chamber_id, incident_id, trigger_alert_id, inhibited_at,
       COALESCE(scope, 'chamber') AS scope, equipment_id
  FROM chamber_inhibits
 WHERE incident_id = %s AND released_at IS NULL
 ORDER BY (chamber_id = %s) DESC, inhibited_at ASC, id ASC
 LIMIT 1
"""


def fetch_open_inhibit(incident_id: str, chamber_id: str = "") -> dict[str, Any] | None:
    """열린 inhibit 행 1건 — `trigger_alert_id` 와 정본 `inhibited_at` 의 출처.

    `/chambers/status` 는 `trigger_alert_id` 를 주지 않는데 그 값이 회고 ⓐ 의 유일한 열쇠다.
    시각도 여기 것이 정본이다 — 응답의 `inhibited_at` 은 isoformat 문자열을 되파싱한 값이라
    마이크로초·tz 표기에서 어긋날 수 있고, 그 값이 곧 Brief 의 UNIQUE 키가 된다.

    장비 스코프면 형제 행이 여럿이다 — 대표 챔버 행을 우선하고(`chamber_id = %s` DESC),
    없으면 가장 이른 행을 쓴다. 형제 행은 같은 트랜잭션 INSERT 라 trigger_alert_id 가 같다.
    """
    if not incident_id:
        return None
    try:
        with _dict_conn() as conn, conn.cursor() as cur:
            cur.execute(_OPEN_INHIBIT_SQL, (incident_id, chamber_id or ""))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001 — 무중단
        logger.warning("열린 inhibit 조회 실패 — ⓐ 없이 진행 (%s: %s)", type(exc).__name__, exc)
        return None


_TRIGGER_VIOLATION_SQL = """
SELECT sensor_id, rule_id, severity, current_value,
       control_limit_upper, control_limit_lower, description
  FROM spc_violations
 WHERE alert_id = %s
 ORDER BY rule_id, sensor_id
"""


def fetch_trigger_violations(alert_id: str | None) -> list[dict[str, Any]]:
    """ⓐ 왜 섰나 — 발동 알람의 위반 목록. 실패·미상이면 빈 리스트(무중단).

    `chamber_inhibits.trigger_alert_id` 가 그대로 `spc_violations.alert_id` 다
    (`record_trigger` 가 `alert["alert_id"]` 를 싣는다).
    """
    if not alert_id:
        return []
    try:
        with _dict_conn() as conn, conn.cursor() as cur:
            cur.execute(_TRIGGER_VIOLATION_SQL, (alert_id,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 — 근거만 얕아지고 파이프라인은 산다 (6-2)
        logger.warning("발동 알람 위반 조회 실패 — ⓐ 없이 진행 (%s: %s)", type(exc).__name__, exc)
        return []


# 분자 = 그 센서가 N1(한계 이탈)로 걸린 distinct 알람 / 분모 = 창 내 그 챔버 총 distinct 알람.
# **RTD 본 판정(`chamber_inhibit.persistent_breach`)과 같은 정의**로 센다 — 정지시킨 근거를
# 그 근거의 언어로 되돌려 보여주는 것이 이 층의 목적이라, 다른 식으로 세면 의미가 어긋난다.
# 다른 점은 창의 기준점 하나뿐이다: RTD 는 `NOW()`, 여기는 **정지 시각**(그 뒤엔 데이터가 없다).
_BREACH_TREND_SQL = """
SELECT sensor_id, count(DISTINCT alert_id) AS n1_alerts
  FROM spc_violations
 WHERE chamber_id = %s AND rule_id = 'N1'
   AND created_at >  %s - make_interval(mins => %s)
   AND created_at <= %s
 GROUP BY sensor_id
"""
_BREACH_TOTAL_SQL = """
SELECT count(DISTINCT alert_id) AS total
  FROM spc_violations
 WHERE chamber_id = %s
   AND created_at >  %s - make_interval(mins => %s)
   AND created_at <= %s
"""


def fetch_breach_trend(
    chamber_id: str, until: Any, window_minutes: int
) -> tuple[list[dict[str, Any]], int]:
    """ⓑ 얼마나 심했나 — 정지 직전 창의 센서별 이탈 추이.

    Args:
        until: 창의 끝 = **정지 시각**(`inhibited_at`). None 이면 조회하지 않는다 —
            `NOW()` 로 대체하면 정지 이후의 (비어 있는) 구간이 분모에 섞여 비율이 왜곡된다.
        window_minutes: 창 길이(분) — params `agent.release_trend_window_min`.

    Returns:
        (센서별 [{sensor_id, n1_alerts, breach_ratio}] 이탈률 내림차순, 분모 total_alerts).
        분모 0 이면 비율을 만들지 않는다 (7장: "분모가 되는 값을 검증 없이 쓰지 말 것") —
        빈 목록 + 0 을 돌려주고 판단은 프롬프트 규칙(재료 없음 명시)이 한다.
    """
    if not chamber_id or until is None:
        return [], 0
    try:
        with _dict_conn() as conn, conn.cursor() as cur:
            cur.execute(_BREACH_TOTAL_SQL, (chamber_id, until, int(window_minutes), until))
            row = cur.fetchone()
            total = int((row or {}).get("total") or 0)
            if total <= 0:
                return [], 0
            cur.execute(_BREACH_TREND_SQL, (chamber_id, until, int(window_minutes), until))
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 — 무중단
        logger.warning("이탈 추이 조회 실패 — ⓑ 없이 진행 (%s: %s)", type(exc).__name__, exc)
        return [], 0

    out = [
        {"sensor_id": r["sensor_id"],
         "n1_alerts": int(r["n1_alerts"] or 0),
         "breach_ratio": round(int(r["n1_alerts"] or 0) / total, 4)}
        for r in rows if r.get("sensor_id")
    ]
    out.sort(key=lambda d: (-d["breach_ratio"], d["sensor_id"]))
    return out, total


# 같은 챔버의 **닫힌** inhibit — 해제가 무엇으로 이뤄졌는지(release_qual_id·released_by)가
# 이 층의 값이다. 진행 중인 정지(released_at IS NULL)는 제외한다 — 자기 자신이 섞인다.
_PAST_RELEASE_SQL = """
SELECT chamber_id, incident_id, inhibited_at, released_at, released_by,
       release_qual_id, COALESCE(scope, 'chamber') AS scope,
       EXTRACT(EPOCH FROM (released_at - inhibited_at)) / 3600.0 AS downtime_h
  FROM chamber_inhibits
 WHERE chamber_id = %s AND released_at IS NOT NULL
 ORDER BY released_at DESC
 LIMIT %s
"""


def fetch_past_releases(chamber_id: str, limit: int) -> list[dict[str, Any]]:
    """ⓒ 과거엔 뭘로 풀렸나 — 같은 챔버의 지난 정지·해제 이력 (최신순).

    **회고 3층에서 가장 값이 큰 층이다** — `release_qual_id`·`released_by` 가 남아 있어
    *"이번에도 그렇게 하면 된다"* 의 근거가 된다. 0건이 흔한 것도 정상이다(첫 정지). 그때는
    창작하지 말고 "선례 없음"으로 나가야 하며, `enforce_evidence_floor_guard` 가 그 상태에서
    `ready` 를 막는다.
    """
    if not chamber_id:
        return []
    try:
        with _dict_conn() as conn, conn.cursor() as cur:
            cur.execute(_PAST_RELEASE_SQL, (chamber_id, int(limit)))
            return [dict(r) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 — 무중단
        logger.warning("과거 해제 이력 조회 실패 — ⓒ 없이 진행 (%s: %s)", type(exc).__name__, exc)
        return []


_INCIDENT_CHAMBER_SQL = "SELECT chamber_id FROM incidents WHERE incident_id = %s"


def fetch_incident_chamber(incident_id: str) -> str | None:
    """발동 Incident 의 챔버 — 장비 스코프 정지에서 **대표 챔버**를 고르는 근거.

    `scope='equipment'` 면 형제 챔버 전부에 행이 생기고 전부 같은 incident_id 를 갖는다
    (`chamber_inhibit.record_trigger`). 그중 실제로 이상이 관측된 곳이 Incident 의 챔버다
    (Incident 는 정의상 챔버 1개 — 헌법 1-4). 조회 실패·부재면 None → 호출측이 폴백한다.
    """
    if not incident_id:
        return None
    try:
        with _dict_conn() as conn, conn.cursor() as cur:
            cur.execute(_INCIDENT_CHAMBER_SQL, (incident_id,))
            row = cur.fetchone()
        ch = (row or {}).get("chamber_id")
        return ch if isinstance(ch, str) and ch else None
    except Exception as exc:  # noqa: BLE001 — 무중단
        logger.warning("Incident 챔버 조회 실패 — 대표 챔버는 상태 응답값 사용 (%s: %s)",
                       type(exc).__name__, exc)
        return None


_RELEASE_BRIEF_EXISTS_SQL = """
SELECT 1 FROM release_briefs WHERE incident_id = %s AND inhibited_at = %s LIMIT 1
"""


def release_brief_exists(incident_id: str, inhibited_at: Any) -> bool:
    """이 정지에 이미 Brief 가 있나 — **LLM 호출을 아끼는 사전 확인**.

    ⚠️ 이것은 중복 방지의 **정답이 아니다**. 최종 방어선은 `report_id UNIQUE` +
    `ON CONFLICT DO NOTHING`(save_release_brief)이고, 여기는 그 앞의 비용 절감이다 —
    폴러가 둘 뜨거나 재시작이 겹치면 이 SELECT 는 경합에 진다.

    조회 실패는 **False(없음)** 로 본다. `_incident_has_brief` 와 같은 판단이다:
    True 로 보면 근거 없는 정지가 조용히 방치되고(정보 유실), False 로 보면 최악이 중복
    생성인데 그건 UNIQUE 가 흡수한다. **유실보다 중복이 싸다.**
    """
    if not incident_id or inhibited_at is None:
        return False
    try:
        with _dict_conn() as conn, conn.cursor() as cur:
            cur.execute(_RELEASE_BRIEF_EXISTS_SQL, (incident_id, inhibited_at))
            return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("해제 Brief 존재 확인 실패 — 없음으로 간주하고 진행 (%s: %s)",
                       type(exc).__name__, exc)
        return False
