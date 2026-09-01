# -*- coding: utf-8 -*-
"""S8 KPI 집계 — `GET /kpi/agent` 의 DB 계층 (PM 요청분, 2026-08-12).

소비처 셋(`KpiScreen`(S8) · `DecisionQueue`(S1 첫 화면) · `CopilotDock`(상시))이 같은
mock(`mock/fdc.ts` `kpis`·`kpiExt`)을 쓰고 있어 한 함수로 먹인다. 라우트(main.py)는 얇게 두고 조회는 여기 — `approvals.fetch_pending`
과 같은 배치다(fake conn 으로 단위테스트 가능).

**집계만 한다.** 판정·채점은 하지 않는다. Scorecard 카드는 `sim_events` 정답지 기반 채점이
정본이고 화면 각주가 *"채점 전용 — 파이프라인 조회 금지"* 로 선을 그어 뒀으므로 이 응답에
넣지 않는다(그 카드는 mock 유지 — PM 확인분).

[창] `operations.kpi_window_hours` 시간의 **상대 창**(`NOW() - interval`).
     ⚠️ `CURRENT_DATE` 금지: compose 의 postgres 에 TZ 설정이 없어 컨테이너가 UTC 로 도는데,
     KST 00~09시에 리허설하면 `created_at::date`(어제) ≠ `CURRENT_DATE`(오늘) 이라 **에러 없이
     전 카드가 0** 이 된다. 상대 창은 TIMESTAMPTZ 끼리의 절대 시각 비교라 타임존과 무관하다.
     데모는 매 회차 `reset_demo_state.sql` 이 전 테이블 TRUNCATE(런북 A-2 필수)라 이 창이
     곧 그 라운드 전체와 같다.
"""

from __future__ import annotations

import logging

# 마커 rule_id 단일 소스 (B 소유 leaf). 여기서 문자열을 다시 적으면 규격 변경 시 조용히 어긋난다.
from src.common.context_score.markers import MARKER_RULE_IDS

_log = logging.getLogger("gateway.kpi")

# 처리 속도 — 건수·MTTA·추이 범위를 **한 번에** 뽑는다.
#   화면이 `오늘 처리`+`평균 응답(MTTA)` 을 「처리 속도」 한 쌍으로 묶어 뒀고
#   (구 종합현황 화면 주석의 역할 분담: *"오늘 = 얼마나 빨리 / Scorecard = 제대로 하고 있나"*),
#   **같은 행 집합**에서 뽑아야 두 숫자가 서로 안 어긋난다. 같은 이유로 추이의 시작점
#   `min(decided_at)` 도 여기서 같이 낸다 — 따로 조회하면 READ COMMITTED 라 그 사이에
#   커밋된 결정 때문에 **카드는 "3건" 인데 스파크라인 합은 4** 가 되는 순간이 생긴다.
#
#   ⚠️ **`decided_at > requested_at` 는 WHERE 가 아니라 평균에만 건다.**
#   다섯 경로가 `INSERT ... decided_at) VALUES (..., NOW())` 를 `requested_at` 없이 쓴다
#   (`main.py` prior_seed · `approvals.py` MODIFIED 재입력 · `ct2_deploy_approval` ·
#   `ct2_deploy_monitor` 롤백·감시 에스컬). `requested_at` 이 `DEFAULT NOW()` 라 **둘이 정확히
#   같아져** 응답시간 기여가 0분이 된다 — 안 걸면 "3건 4분" 이 "5건 2분" 으로 보인다.
#
#   ⚠️ 그런데 **이 조건으로 행을 통째로 빼면 안 된다** — 저 다섯 중 둘은 **사람의 결정**이다:
#     · `POST /prior/seed`        "승인 1회 경유 시드 쓰기 (헌법 1-1 HITL)" · approver 없으면 400
#     · `retry_failed_correction` "**재입력도 승인 행위** (헌법 1-1)" · `status='MODIFIED'`
#   판별선은 "기계 vs 사람" 이 아니라 **"생성 시 이미 결정 vs PENDING 에서 전이"** 다.
#   행을 빼면 하필 `MODIFIED` 가 사라져 **「수정률」이 죽는다** — 화면정의서 S8 이 요구한 세
#   지표 중 하나가 그 경로다. 그래서 **건수·분포는 전건, 평균만 FILTER**.
#   (모든 행이 즉시 결정이면 avg 는 NULL → `avg_resp_min = None` → 화면은 "—".)
#
#   MTTA 는 **소수점 1자리**다 — 한 라운드가 4~5분이라 정수 분으로 반올림하면 40초 응답이
#   `0m` 으로 뜨고, 카드가 사실상 항상 0m/1m 만 보여준다.
#
#   결정 분포(채택·수정·반려·에스컬)도 **같은 스캔에서** 낸다 — 화면정의서 S8 이 요구하는
#   *"추천 채택률 / 수정률 / 반려율 — Agent 약점 영역 식별"* 이 이 네 값이다. 별도 쿼리로
#   빼면 분모(`today`)와 다른 스냅샷을 보게 돼 **합이 안 맞는 순간**이 생긴다.
#   비율이 아니라 **건수**를 내보낸다 — 분모가 0 인 창(리셋 직후)에서 SQL 이 나눗셈을 하지
#   않게 하고, 표시층이 "3/5" 든 "60%" 든 고르게 둔다. 분모는 언제나 `today` 하나다.
_SQL_SPEED = """
    SELECT count(*),
           round((avg(EXTRACT(EPOCH FROM (decided_at - requested_at)))
                    FILTER (WHERE decided_at > requested_at) / 60.0)::numeric, 1),
           min(decided_at),
           NOW(),
           count(*) FILTER (WHERE status = 'APPROVED'),
           count(*) FILTER (WHERE status = 'MODIFIED'),
           count(*) FILTER (WHERE status = 'REJECTED'),
           count(*) FILTER (WHERE status = 'ESCALATED')
      FROM approval_records
     WHERE decided_at IS NOT NULL
       AND decided_at > NOW() - make_interval(secs => %s)
"""

# 센서별 알람 — ⚠️ **COUNT(DISTINCT alert_id)**.
#   `spc_violations` 는 1알람 → 센서×규칙×창 팬아웃(실측 47행 = 12×4×2)이라 `COUNT(*)` 는
#   개체 수가 아니다 (헌법 7장 — 팬아웃 행 수를 개체 수처럼 쓰지 말 것. 같은 함정에서
#   "전환율 0.6%"(실제 91.8%) 오진이 있었다).
#   센서는 **C코드 그대로** 내보낸다 — 표시명 변환은 프론트 담당(헌법 6-4 표시 계층).
#
#   ⚠️ **합성 마커(B9)는 뺀다.** B9 는 Nelson 룰이 아니라 알람 **종류 표지**이고, 그 행의
#   `sensor_id='C65'` 는 예측 타겟이지 FDC 센서가 아니다(`markers.py`). 안 빼면 카드
#   「알람 파레토 — **센서**」 1위가 C65 로 뜬다 — 실측 2026-08-12 라이브 DB 에서 **2,789건**
#   으로 2위(C17 1,905)를 눌렀다. 각주가 *"상위 센서 **관리선** 점검 우선"* 인데 B9 에는
#   관리선 자체가 없어(`control_limit_*` 는 P99 컷) 조치로 이어지지도 않는다.
#   제외 목록은 `markers.MARKER_RULE_IDS` 단일 소스를 읽는다 — 여기 "B9" 를 하드코딩하면
#   마커 규격이 바뀔 때 조용히 어긋난다(그 파일이 경고하는 바로 그 케이스).
_SQL_PARETO = """
    SELECT sensor_id, count(DISTINCT alert_id) AS n
      FROM spc_violations
     WHERE created_at > NOW() - make_interval(secs => %s)
       AND NOT (rule_id = ANY(%s))
     GROUP BY sensor_id
     ORDER BY n DESC, sensor_id
     LIMIT %s
"""

# 챔버별 **인시던트** 수 (알람 아님 — 카드 제목이 「챔버별 인시던트」다)
_SQL_CHAMBERS = """
    SELECT chamber_id, count(*) AS n
      FROM incidents
     WHERE created_at > NOW() - make_interval(secs => %s)
     GROUP BY chamber_id
     ORDER BY n DESC, chamber_id
"""

# width_bucket 이 경계를 맡는다. 창 조건을 여기서도 다시 거는 이유는, t0 는 창 안의 최솟값이라
# `decided_at >= t0` 만으로는 **미래 시각 행**(클럭 스큐·수기 주입)이 섞일 수 있기 때문이다.
_SQL_TREND = """
    SELECT width_bucket(EXTRACT(EPOCH FROM (decided_at - %s)), 0, %s, %s) AS b,
           count(*)
      FROM approval_records
     WHERE decided_at IS NOT NULL
       AND decided_at > NOW() - make_interval(secs => %s)
     GROUP BY b
"""

# 데이터 신선도 — 창이 비었을 때 **리셋 직후**인지 **데이터가 낡은 것**인지 가른다.
#   응답에 이 필드가 없으면 둘 다 그냥 0 으로 보여, 화면이 "정상인데 조용함" 과
#   "시뮬이 멎었음" 을 구별하지 못한다(헌법 7장 — 조용한 상태를 노출 없이 두지 말 것).
#
#   ⚠️ `max(created_at)` 이 아니라 **`ORDER BY id DESC LIMIT 1`** 이다.
#   `spc_violations(created_at)` 단독 인덱스가 없어(있는 것은 `(chamber_id, created_at)`)
#   `max()` 는 **전체 seq scan** 이 된다 — 실측 1.92ms vs 0.062ms(**31배**), 그리고 이 표는
#   가장 빨리 자라는 축이라 격차가 행 수에 비례해 벌어진다. 5초 폴링 × 탭 수 만큼 반복된다.
#   `id` 는 BIGSERIAL 이고 `created_at` 은 DEFAULT NOW() 라 사실상 동행한다 — 동시 INSERT
#   에서 마이크로초 단위로 어긋날 수 있으나, "마지막 데이터가 언제냐"에는 무의미한 오차다.
#   (인덱스 신설은 `db/` = PM 소유 + 마이그레이션 동반(3-2)이라 여기서 하지 않는다.)
_SQL_FRESH = """
    SELECT created_at FROM spc_violations ORDER BY id DESC LIMIT 1
"""

_MARKER_IDS = sorted(MARKER_RULE_IDS)    # psycopg2 `= ANY(%s)` 는 list 를 받는다(frozenset 불가)
_CHAMBER_CACHE: dict = {}   # 챔버 목록 파싱 캐시 (프로세스 수명 — 목록은 회차 중 안 바뀐다)
_MIN_WINDOW_SEC = 60.0      # 창 하한 — `kpi_window_hours: 0`(또는 음수) 오설정이면 모든 카드가
                            #   조용히 0 이 된다. 이 모듈이 막으려는 실패와 같은 형태라 바닥을 둔다.


def fold_trend(rows, buckets: int) -> list:
    """`width_bucket` 결과 [(bucket, n), ...] → 길이 `buckets` 의 정수 배열 (순수).

    `width_bucket(x, low, high, n)` 은 **1..n** 을 돌려주되 `x >= high` 는 **n+1**,
    `x < low` 는 **0** 을 돌려준다. 상한이 `NOW()` 기준이라 평시엔 n+1 이 안 나오지만,
    클럭 스큐·수기 주입으로 `decided_at >= NOW()` 인 행이 있으면 나온다. 그때 클램프가
    없으면 그 행이 **어느 칸에도 안 들어가 조용히 사라진다**(IndexError 도 안 난다 —
    dict 가 아니라 리스트 인덱싱이라 음수 인덱스로 엉뚱한 칸에 들어갈 수도 있다).
    범위 밖 값은 버리지 않고 양 끝 칸에 접는다.
    """
    out = [0] * max(int(buckets), 1)
    for b, n in rows:
        idx = min(max(int(b), 1), len(out)) - 1
        out[idx] += int(n)
    return out


def known_chambers(path=None) -> list:
    """감시 대상 챔버 목록 — `config/chamber_offsets.json` 이 단일 소스.

    챔버 수를 코드에 박지 않는다(설정 주도). 이 목록이 필요한 이유는 `GROUP BY chamber_id` 가
    **행이 있는 챔버만** 내놓기 때문이다 — 그러면 화면이 *"CH1 은 인시던트가 0건"* 과
    *"CH1 은 아예 안 돌고 있음"* 을 구별하지 못한다. 읽기 실패는 빈 목록으로 흡수한다
    (KPI 는 부가 정보라 이것 때문에 엔드포인트가 죽으면 안 된다).
    """
    import json
    from pathlib import Path

    p = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "chamber_offsets.json"
    key = str(p)
    if key in _CHAMBER_CACHE:                 # 5초 폴링 × 탭 수 만큼 파일을 다시 읽지 않는다
        return _CHAMBER_CACHE[key]
    try:
        with open(p, encoding="utf-8") as f:
            out = sorted((json.load(f) or {}).get("offsets", {}).keys())
    except Exception:                         # noqa: BLE001 — 목록 부재는 치명적이지 않다
        # 경고도 캐시된다 — 안 그러면 파일이 사라진 동안 분당 12줄씩 로그를 채운다.
        _log.warning("kpi: 챔버 목록을 못 읽었다 — 인시던트 0건 챔버는 표시에서 빠진다 (%s)", p)
        out = []
    _CHAMBER_CACHE[key] = out
    return out


def fetch_kpi(conn, window_hours: float, trend_buckets: int, pareto_limit: int) -> dict:
    """처리 속도 + 센서 파레토 + 챔버별 인시던트 + 처리 추이 (모듈 docstring 참조).

    데이터가 없으면 0·빈 배열·0 배열을 돌려준다 — 404 가 아니다. 화면이 **빈 상태를 그릴 수
    있어야** 하고, 리셋 직후(런북 A-2)가 정상적으로 그 상태이기 때문이다.

    Args:
        conn: psycopg2 연결(또는 같은 인터페이스의 fake — 단위테스트).
        window_hours: 집계 창(시간). `operations.kpi_window_hours`.
        trend_buckets: 추이 막대 수. `operations.kpi_trend_buckets`.

    Returns:
        dict — `today`·`avg_resp_min`(없으면 None)·`decisions{approved,modified,rejected,escalated}`
        ·`pareto[]`·`chambers[]`·`trend[]`·`window_hours`·`data_last_at`.
        `decisions` 의 합은 `today` 와 같다(같은 스캔·같은 조건) — 비율의 분모는 `today` 다.
    """
    buckets = max(int(trend_buckets), 1)      # 0 이면 아래 나눗셈·인덱싱이 죽는다(분모 가드)
    secs = max(float(window_hours) * 3600.0, _MIN_WINDOW_SEC)
    limit = max(int(pareto_limit), 1)

    # ⚠️ **반드시 commit 한다** (읽기 전용인데도). 게이트웨이 연결은 `autocommit=False` 이고
    #   Postgres 의 `NOW()` 는 **트랜잭션 시작 시각**(`transaction_timestamp()`)이다. 커밋하지
    #   않으면 트랜잭션이 열린 채 남아 **`NOW()` 가 첫 호출 시각에 얼어붙는다** — 이 엔드포인트는
    #   5초 폴링이라, 시간이 갈수록 실제 최신 행이 `width_bucket` 상한을 넘어 마지막 막대로
    #   전부 쏠린다. 예외도 경고도 없이 추이만 서서히 틀어지는 종류다.
    #   (`/incidents/recent` 등 기존 조회 엔드포인트도 `finally: conn.commit()` 을 한다.)
    try:
        with conn.cursor() as cur:
            # ⚠️ **여러 statement 가 같은 스냅샷을 봐야 한다.** 기본 READ COMMITTED 에서는
            #   statement 마다 스냅샷이 새로 잡혀, `_SQL_SPEED` 와 `_SQL_TREND` 사이에 커밋된
            #   결정이 **추이에는 있고 건수에는 없다** — 카드는 "3건" 인데 스파크라인 합은 4다
            #   (쿼리를 하나로 합친 것만으로는 안 풀린다. 추이는 t0 를 알아야 해서 두 번째 statement 다).
            #   ⚠️ `conn.set_session()` 은 쓰지 않는다 — 이 커넥션은 풀에서 빌린 것이고
            #   `ApprovalStore` 와 공유라, 세션 수준을 바꾸면 **승인 쓰기 격리까지** 바뀐다.
            #   `SET TRANSACTION` 은 현재 트랜잭션에만 걸리고 commit 과 함께 사라진다.
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute(_SQL_SPEED, (secs,))
            row = cur.fetchone() or (0, None, None, None, 0, 0, 0, 0)
            today = int(row[0] or 0)
            avg_min = float(row[1]) if row[1] is not None else None
            t0, t1 = row[2], row[3]
            decisions = {"approved": int(row[4] or 0), "modified": int(row[5] or 0),
                         "rejected": int(row[6] or 0), "escalated": int(row[7] or 0)}

            cur.execute(_SQL_PARETO, (secs, _MARKER_IDS, limit))
            pareto = [{"sensor": r[0], "n": int(r[1])} for r in cur.fetchall()]

            cur.execute(_SQL_CHAMBERS, (secs,))
            seen = {r[0]: int(r[1]) for r in cur.fetchall()}
            # 인시던트 0건 챔버도 행으로 남긴다 — 「없음」과 「안 돌고 있음」은 다르다
            ids = list(dict.fromkeys(list(seen) + known_chambers()))
            chambers = sorted(({"id": c, "n": seen.get(c, 0)} for c in ids),
                              key=lambda d: (-d["n"], d["id"]))

            trend = [0] * buckets
            if t0 is not None and today > 0:
                span = (t1 - t0).total_seconds()
                if span <= 0:                     # 전 행이 같은 시각(또는 1행) → 마지막 칸에 몰아준다
                    trend[-1] = today
                else:
                    cur.execute(_SQL_TREND, (t0, span, buckets, secs))
                    trend = fold_trend(cur.fetchall(), buckets)

            cur.execute(_SQL_FRESH)
            fresh = (cur.fetchone() or (None,))[0]
    finally:
        # ⚠️ **여기서 예외가 새면 원래 예외를 대체한다** (헌법 7장 — "`finally` 에서 던진 예외가
        #   원래 예외를 대체해 죽은 이유가 트레이스백에 안 남는다"). 커넥션이 이미 죽었으면
        #   `commit()` 도 같은 InterfaceError 를 던지는데, 그러면 진짜 첫 원인이 사라진다.
        #   커밋 실패는 **다음 요청이 새 커넥션으로 복구**되는 종류라 삼키고 로그로 남긴다.
        try:
            conn.commit()                     # 트랜잭션 종료 = NOW() 고착 방지 (위 주석)
        except Exception:                     # noqa: BLE001 — 원래 예외 보존이 우선
            _log.warning("kpi: 읽기 tx 종료(commit) 실패 — 커넥션 반납 시 정리된다", exc_info=True)

    return {"today": today, "avg_resp_min": avg_min, "decisions": decisions,
            "pareto": pareto, "chambers": chambers, "trend": trend,
            # 하한 적용 **후** 값을 돌려준다 — 화면 각주가 "최근 N시간" 을 그대로 쓰는데,
            #   `kpi_window_hours: 0` 오설정이면 실제 창(60초)과 표기(0시간)가 어긋난다.
            "window_hours": round(secs / 3600.0, 2),
            # 창이 비었을 때 화면이 「리셋 직후」와 「데이터가 낡음」을 가를 수 있게 한다
            "data_last_at": fresh.isoformat() if fresh is not None else None}
