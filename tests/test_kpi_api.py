# -*- coding: utf-8 -*-
"""S8/S0 KPI 집계 단위테스트 — `gateway.kpi.fetch_kpi`(fake conn) + `fold_trend`(순수).

fastapi/psycopg2 불요. 실 DB 왕복은 사용자 env(리허설)에서 확인한다.
실행: pytest tests/test_kpi_api.py
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest  # noqa: E402

from src.gateway import kpi as _kpi_mod  # noqa: E402
from src.gateway.kpi import fold_trend  # noqa: E402

#: 테스트 호출 래퍼 — `pareto_limit` 은 **필수 인자**다(기본값을 두면 params.yaml 과
#: 두 곳이 정본이 된다). 테스트마다 늘어놓는 대신 여기 한 곳에서 기본을 준다.
def fetch_kpi(conn, window_hours=24, trend_buckets=7, pareto_limit=8):
    return _kpi_mod.fetch_kpi(conn, window_hours, trend_buckets, pareto_limit)

T0 = datetime(2026, 8, 12, 3, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)


class _FakeCur:
    """execute 순서대로 미리 준 결과를 돌려주는 커서 (fetch_kpi 는 4~5회 execute 한다)."""

    def __init__(self, results):
        self._results = list(results)
        self._cur = None
        self.sqls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        one = " ".join(sql.split())
        self.sqls.append(one)
        # `SET TRANSACTION ...` 은 결과를 안 돌려주는 statement 라 준비한 결과를 소비하지
        #   않는다. 소비하면 뒤 쿼리들이 한 칸씩 밀려 fake 가 실제와 다른 순서를 흉내낸다.
        if one.upper().startswith("SET "):
            return
        self._cur = self._results.pop(0) if self._results else []

    def fetchone(self):
        return self._cur[0] if self._cur else None

    def fetchall(self):
        return self._cur


class _FakeConn:
    def __init__(self, results):
        self.cur = _FakeCur(results)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        # 실제 psycopg2 커넥션에는 있다. 없으면 라우트의 반납 위생이 AttributeError 로
        #   걸려 **성공 경로도 폐기 반납**으로 잡힌다 — fake 가 실물과 달라 생기는 거짓 신호.
        self.rollbacks = getattr(self, "rollbacks", 0) + 1


def _conn(speed=(3, 18.0, T0, T1, 2, 1, 0, 0), pareto=(("C17", 6), ("C11", 4)),
          chambers=(("SIM_CH_3", 5),), trend=((1, 2), (7, 1)), fresh=(T1,)):
    """execute 순서 = SPEED → PARETO → CHAMBERS → [TREND] → FRESH."""
    return _FakeConn([[speed], list(pareto), list(chambers), list(trend), [fresh]])


# 챔버 제로필이 config 를 읽으므로 테스트는 목록을 고정해 주입한다(파일 상태에 안 묶이게)
import src.gateway.kpi as _kpi_mod                                    # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_chambers(monkeypatch):
    monkeypatch.setattr(_kpi_mod, "known_chambers", lambda path=None: ["SIM_CH_3", "SIM_CH_4"])


# ── fold_trend (순수) ───────────────────────────────────────────────────────
def test_fold_trend_basic():
    assert fold_trend([(1, 2), (3, 5)], 7) == [2, 0, 5, 0, 0, 0, 0]


def test_fold_trend_clamps_overflow_bucket():
    """`width_bucket` 상한 일치분(n+1)은 **버리지 않고 마지막 칸에 접는다**.

    클램프가 없으면 그 행이 어느 칸에도 안 들어가 조용히 사라지고, 추이가 항상
    우하향으로 보인다 (에러는 안 난다 — 그래서 더 위험하다).
    """
    assert fold_trend([(8, 3)], 7) == [0, 0, 0, 0, 0, 0, 3]
    assert fold_trend([(99, 1)], 7)[-1] == 1


def test_fold_trend_clamps_underflow_bucket():
    """하한 미만(0)도 첫 칸으로 접는다 — 음수 인덱싱으로 엉뚱한 칸에 들어가면 안 된다."""
    assert fold_trend([(0, 4)], 7) == [4, 0, 0, 0, 0, 0, 0]


def test_fold_trend_min_length_one():
    """buckets 0/음수여도 길이 1 이상 — 호출부 `trend[-1]` 이 IndexError 로 죽지 않게."""
    assert len(fold_trend([], 0)) == 1
    assert fold_trend([(1, 5)], 1) == [5]


# ── fetch_kpi ──────────────────────────────────────────────────────────────
def test_fetch_kpi_shapes():
    out = fetch_kpi(_conn(), window_hours=24, trend_buckets=7)
    assert out["today"] == 3 and out["avg_resp_min"] == 18.0
    assert out["decisions"] == {"approved": 2, "modified": 1, "rejected": 0, "escalated": 0}
    assert out["pareto"] == [{"sensor": "C17", "n": 6}, {"sensor": "C11", "n": 4}]
    # 인시던트 0건인 CH4 도 행으로 남는다 — 「없음」과 「안 돌고 있음」은 다르다
    assert out["chambers"] == [{"id": "SIM_CH_3", "n": 5}, {"id": "SIM_CH_4", "n": 0}]
    assert out["trend"] == [2, 0, 0, 0, 0, 0, 1]          # bucket 7 → 마지막 칸
    assert out["window_hours"] == 24.0


def test_fetch_kpi_sensor_stays_c_code():
    """센서는 **C코드 그대로** 나간다 — 표시명 변환은 프론트(헌법 6-4 표시 계층)."""
    out = fetch_kpi(_conn(pareto=(("C62", 9),)), 24, 7)
    assert out["pareto"][0]["sensor"] == "C62"


def test_fetch_kpi_empty_returns_zeros_not_error():
    """리셋 직후(런북 A-2 TRUNCATE)가 정상 상태다 — 404 가 아니라 0·빈 배열."""
    out = fetch_kpi(_FakeConn([[(0, None, None, None, 0, 0, 0, 0)], [], [], [], [(None,)]]), 24, 7)
    assert out["today"] == 0 and out["avg_resp_min"] is None
    assert out["pareto"] == []
    assert out["chambers"] == [{"id": "SIM_CH_3", "n": 0}, {"id": "SIM_CH_4", "n": 0}]
    assert out["trend"] == [0] * 7
    assert out["data_last_at"] is None


def test_fetch_kpi_zero_span_folds_into_last_bucket():
    """전 행이 같은 시각(또는 1건)이면 span=0 → 0 나눗셈 대신 마지막 칸에 몰아준다."""
    out = fetch_kpi(_conn(speed=(1, 5.0, T0, T0, 1, 0, 0, 0), trend=()), 24, 7)
    assert out["trend"] == [0, 0, 0, 0, 0, 0, 1]


def test_fetch_kpi_buckets_floor_is_one():
    """`kpi_trend_buckets: 0` 오설정에도 죽지 않는다 (분모·인덱스 가드)."""
    out = fetch_kpi(_conn(trend=((1, 3),)), 24, 0)
    assert out["trend"] == [3]


def test_fetch_kpi_uses_relative_window_not_current_date():
    """⚠️ 회귀 가드 — 창은 **상대 시간**이어야 한다.

    compose 의 postgres 에 TZ 설정이 없어 컨테이너가 UTC 로 돈다. `CURRENT_DATE` 를 쓰면
    KST 00~09시 리허설에서 `created_at::date`(어제) ≠ `CURRENT_DATE`(오늘) 이라
    **에러 없이 전 카드가 0** 이 된다. 조용한 실패라 테스트로 못박는다.
    """
    conn = _conn()
    fetch_kpi(conn, 24, 7)
    joined = " ".join(conn.cur.sqls)
    assert "CURRENT_DATE" not in joined.upper()
    assert "make_interval" in joined


def test_fetch_kpi_pareto_counts_distinct_alerts():
    """⚠️ 회귀 가드 — `spc_violations` 는 1알람 → 센서×규칙×창 팬아웃(실측 47행=12×4×2).

    `COUNT(*)` 로 세면 알람 수가 아니라 행 수다 (헌법 7장 등재 — 같은 함정에서
    "전환율 0.6%"(실제 91.8%) 오진이 있었다).
    """
    conn = _conn()
    fetch_kpi(conn, 24, 7)
    pareto_sql = next(s for s in conn.cur.sqls if "spc_violations" in s)
    assert "count(DISTINCT alert_id)" in pareto_sql


def test_fetch_kpi_window_hours_reaches_sql():
    """창 값이 실제 파라미터로 흘러가는지 — config 를 바꿔도 안 먹으면 조용한 오작동."""
    conn = _FakeConn([[(0, None, None, None, 0, 0, 0, 0)], [], [], [], [(None,)]])

    captured = []
    orig = conn.cur.execute

    def spy(sql, params=None):
        captured.append(params)
        return orig(sql, params)

    conn.cur.execute = spy
    fetch_kpi(conn, window_hours=6, trend_buckets=7)
    # `SET TRANSACTION` 이 params=None 으로 먼저 온다 — 위치를 가정하지 않고 실값으로 찾는다
    assert next(p[0] for p in captured if p) == 6 * 3600.0



def test_fetch_kpi_commits_to_end_transaction():
    """⚠️ 회귀 가드 — 읽기 전용이어도 **commit 해야 한다**.

    게이트웨이 연결은 `autocommit=False` 이고 Postgres `NOW()` 는 **트랜잭션 시작 시각**이다.
    커밋하지 않으면 트랜잭션이 열린 채 남아 NOW() 가 첫 호출 시각에 얼어붙고, 5초 폴링에서
    시간이 갈수록 최신 행이 `width_bucket` 상한을 넘어 **마지막 막대로 전부 쏠린다**.
    예외도 경고도 없이 추이만 서서히 틀어지는 종류라 테스트로 못박는다.
    """
    conn = _conn()
    fetch_kpi(conn, 24, 7)
    assert conn.commits == 1


def test_fetch_kpi_commits_even_on_query_error():
    """조회가 터져도 트랜잭션은 닫는다 — 안 닫으면 그 연결이 계속 aborted 상태로 남는다."""
    class _Boom(_FakeConn):
        def cursor(self):
            raise RuntimeError("boom")

    conn = _Boom([])
    try:
        fetch_kpi(conn, 24, 7)
    except RuntimeError:
        pass
    assert conn.commits == 1



def test_fetch_kpi_pareto_excludes_synthetic_markers():
    """⚠️ 회귀 가드 — 파레토에서 **합성 마커(B9)를 뺀다**.

    B9 는 Nelson 룰이 아니라 알람 **종류 표지**이고 `sensor_id='C65'` 는 예측 타겟이지
    FDC 센서가 아니다(`markers.py`). 안 빼면 카드 「알람 파레토 — **센서**」 1위가 C65 로
    뜬다 — 라이브 DB 실측(2026-08-12) 2,789건으로 2위 C17(1,905)을 눌렀다.
    제외 목록은 `markers.MARKER_RULE_IDS` 단일 소스를 읽어야 한다(하드코딩 금지).
    """
    from src.common.context_score.markers import MARKER_RULE_IDS
    from src.gateway.kpi import _MARKER_IDS

    conn = _conn()
    fetch_kpi(conn, 24, 7)
    pareto_sql = next(s for s in conn.cur.sqls if "spc_violations" in s)
    assert "rule_id = ANY" in pareto_sql and "NOT" in pareto_sql
    assert set(_MARKER_IDS) == set(MARKER_RULE_IDS)      # 단일 소스에서 파생됐는가
    assert isinstance(_MARKER_IDS, list)                 # psycopg2 ANY 는 list 를 요구한다



def test_fetch_kpi_commit_failure_does_not_mask_original_error():
    """⚠️ 회귀 가드 — `finally` 의 commit 실패가 **원래 예외를 덮으면 안 된다**.

    헌법 7장: *"그 전파 경로에 `finally` 가 있으면 거기서 던진 예외가 원래 예외를 대체해
    죽은 이유가 트레이스백에 안 남는다."* 커넥션이 죽으면 조회도 commit 도 같은 에러를
    던지는데, 가드가 없으면 **첫 원인이 사라진다**.
    """
    class _Dead(_FakeConn):
        def cursor(self):
            raise RuntimeError("조회가 먼저 터졌다")

        def commit(self):
            self.commits += 1
            raise RuntimeError("commit 도 터졌다")

    conn = _Dead([])
    try:
        fetch_kpi(conn, 24, 7)
    except RuntimeError as e:
        assert str(e) == "조회가 먼저 터졌다", f"원래 예외가 덮였다: {e}"
    else:
        raise AssertionError("예외가 전파되지 않았다")
    assert conn.commits == 1                      # 시도는 했다


def test_fetch_kpi_commit_failure_alone_is_swallowed():
    """조회는 성공했는데 commit 만 실패하면 **결과를 돌려준다** — 읽은 값은 유효하다."""
    class _BadCommit(_FakeConn):
        def commit(self):
            self.commits += 1
            raise RuntimeError("commit 실패")

    conn = _BadCommit([[(3, 18.0, None, None, 3, 0, 0, 0)], [], [], [], [(None,)]])
    out = fetch_kpi(conn, 24, 7)
    assert out["today"] == 3                      # 조회 결과는 살아 있다



def _spy(conn):
    """execute 인자를 순서대로 캡처 — 바인딩 순서를 눈으로 확인하기 위한 것."""
    captured = []
    orig = conn.cur.execute

    def go(sql, params=None):
        captured.append((" ".join(sql.split()), params))
        return orig(sql, params)

    conn.cur.execute = go
    return captured


def test_trend_query_binds_params_in_declared_order():
    """⚠️ 회귀 가드 — `_SQL_TREND` 의 4개 바인딩 **순서**.

    `width_bucket(EXTRACT(EPOCH FROM (decided_at - %s)), 0, %s, %s)` + `make_interval(secs => %s)`
    = (t0, span, buckets, secs). `buckets` 와 `secs` 가 뒤바뀌면 `width_bucket(x, 0, 7, 86400)`
    이 되어 **모든 행이 상한을 넘어 마지막 칸으로 접히고 스파크라인이 영원히 막대 1개**가 된다.
    런타임 에러가 없고 기존 테스트도 전부 green 이라 이 가드가 유일한 방어선이다.
    """
    conn = _conn()
    cap = _spy(conn)
    fetch_kpi(conn, window_hours=6, trend_buckets=7)
    sql, params = next((q, p) for q, p in cap if "width_bucket" in q)
    assert params == (T0, (T1 - T0).total_seconds(), 7, 6 * 3600.0)


def test_all_queries_get_the_same_window():
    """창 값이 **모든** 조회에 같은 값으로 흘러야 한다 — 하나만 달라도 카드끼리 어긋난다."""
    conn = _conn()
    cap = _spy(conn)
    fetch_kpi(conn, window_hours=6, trend_buckets=7)
    # 창의 **위치는 쿼리마다 다르다** (PARETO 는 끝이 LIMIT, TREND 는 앞이 t0·span·buckets).
    #   위치를 가정하면 가드가 엉뚱한 값을 본다 — 실제로 처음 두 번 그렇게 틀렸다.
    #   그래서 "인자 안에 창 값이 **들어 있는가**" 로 본다.
    win = 6 * 3600.0
    using = [(q, p) for q, p in cap if p and "make_interval" in q]
    assert len(using) >= 4, using
    for q, p in using:
        assert win in p, f"창이 안 들어간 쿼리: {q[:60]} params={p}"


def test_instant_rows_are_excluded_from_avg_but_not_from_counts():
    """⚠️ 회귀 가드 — `decided_at > requested_at` 는 **평균에만** 걸어야 한다.

    다섯 경로가 `requested_at` 없이 `decided_at = NOW()` 로 INSERT 하고, `DEFAULT NOW()` 라
    둘이 정확히 같아져 **응답시간 기여가 0분**이 된다 — 평균에서 빼는 건 맞다.

    그런데 **행을 통째로 빼면 안 된다.** 저 다섯 중 둘은 사람의 결정이다 —
    `POST /prior/seed`("승인 1회 경유 … 헌법 1-1 HITL")와 `retry_failed_correction`
    ("재입력도 승인 행위 … 헌법 1-1", `status='MODIFIED'`). 빼면 하필 `MODIFIED` 가 사라져
    **화면정의서 S8 이 요구한 「수정률」이 죽는다.**
    추이도 같은 모집단이어야 하므로 거기에도 이 조건이 없어야 한다.
    """
    conn = _conn()
    cap = _spy(conn)
    fetch_kpi(conn, 24, 7)
    speed = next(q for q, _ in cap if "FILTER (WHERE status" in q)
    trend = next(q for q, _ in cap if "width_bucket" in q)
    # 평균에만 — WHERE 절이 아니라 avg 의 FILTER 로
    assert "FILTER (WHERE decided_at > requested_at)" in speed
    assert "WHERE decided_at IS NOT NULL AND decided_at > NOW()" in speed,         "건수·분포까지 걸러지면 MODIFIED(수정률)가 죽는다"
    assert "decided_at > requested_at" not in trend, "추이 모집단이 건수와 어긋난다"


def test_decision_statuses_cover_the_orchestrator_contract():
    """⚠️ 회귀 가드 — `decisions` 의 네 status 가 **승인 그래프의 결정 전체**여야 한다.

    `today` 는 `count(*)` 이고 분포는 네 개의 `FILTER` 다. 다섯 번째 결정 상태가 생기면
    합이 `today` 에 미달해 채택률+수정률+반려율이 조용히 100% 미만이 된다.
    (동어반복을 피하려고 fake 튜플이 아니라 **정본 상수**와 대조한다.)
    """
    from src.orchestrator.approval_graph import ACTION_STATUS
    from src.gateway.kpi import _SQL_SPEED

    for status in set(ACTION_STATUS.values()):
        assert f"status = '{status}'" in _SQL_SPEED, f"{status} 가 분포에서 빠졌다"


def test_same_snapshot_across_statements():
    """⚠️ 회귀 가드 — 건수와 추이가 **같은 스냅샷**이어야 한다.

    기본 READ COMMITTED 는 statement 마다 스냅샷을 새로 잡아, 두 쿼리 사이에 커밋된 결정이
    추이에는 있고 건수에는 없다("카드 3건 / 스파크라인 합 4"). 쿼리 병합만으로는 안 풀린다 —
    추이는 t0 를 알아야 해서 두 번째 statement 이기 때문이다.
    """
    conn = _conn()
    cap = _spy(conn)
    fetch_kpi(conn, 24, 7)
    assert any("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ" in q for q, _ in cap)
    assert cap[0][0].startswith("SET TRANSACTION"), "격리 설정은 첫 statement 여야 한다"


def test_window_has_a_floor():
    """`kpi_window_hours: 0`(오설정)이 모든 카드를 조용히 0 으로 만들지 않게."""
    conn = _conn()
    cap = _spy(conn)
    fetch_kpi(conn, window_hours=0, trend_buckets=7)
    assert next(p[0] for _, p in cap if p) >= 60.0


def test_pareto_limit_is_configurable_and_floored():
    conn = _conn()
    cap = _spy(conn)
    fetch_kpi(conn, 24, 7, pareto_limit=3)
    sql, params = next((q, p) for q, p in cap if "spc_violations" in q)
    assert params[-1] == 3
    conn2 = _conn()
    cap2 = _spy(conn2)
    fetch_kpi(conn2, 24, 7, pareto_limit=0)          # 0 이면 SQL LIMIT 0 → 전부 사라진다
    assert next(p for q, p in cap2 if "spc_violations" in q)[-1] == 1


def test_params_yaml_has_the_kpi_keys():
    """⚠️ 회귀 가드 — 라우트가 `ops["kpi_*"]` 로 직접 색인한다. 키가 없으면 **500**.

    config→코드 방향 표류는 런타임에서만 드러나고 테스트는 green 이라(헌법 7장 등재),
    정본 params 를 실제로 읽어 확인한다.
    """
    import yaml
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ops = (yaml.safe_load(open(root / "config" / "params.yaml", encoding="utf-8"))
           or {})["operations"]
    for k in ("kpi_window_hours", "kpi_trend_buckets", "kpi_pareto_limit"):
        assert k in ops, f"params.yaml operations.{k} 없음 → /kpi 가 500"
        assert isinstance(ops[k], (int, float)) and ops[k] > 0



def test_decisions_come_from_the_same_scan_as_today():
    """분포는 **별도 쿼리가 아니다** — `approval_records` 를 두 번 스캔하면 안 된다."""
    conn = _conn()
    cap = _spy(conn)
    fetch_kpi(conn, 24, 7)
    scans = [q for q, _ in cap if "FROM approval_records" in q]
    assert len(scans) == 2, scans          # 속도+분포(1) · 추이(1) — 분포 전용 쿼리는 없다
    assert "FILTER (WHERE status" in scans[0]



def test_route_path_matches_the_screen_spec():
    """⚠️ 회귀 가드 — 라우트 경로가 **화면정의서와 같아야** 한다.

    정의서(`docs/프론트_화면_정의서_v1.md` S8)가 `GET /kpi/agent` 로 먼저 정했고 구현이 거기
    맞췄다. 셋(정의서·게이트웨이·프론트) 중 하나만 바뀌면 **404 로만 드러나고** 그때는 이미
    화면이 목업으로 조용히 폴백한 뒤다(useKpi 가 실패를 삼킨다). 세 곳을 함께 확인한다.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    route = (root / "src" / "gateway" / "main.py").read_text(encoding="utf-8")
    client = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    spec = (root / "docs" / "프론트_화면_정의서_v1.md").read_text(encoding="utf-8")

    assert '@app.get("/kpi/agent")' in route, "게이트웨이 라우트 경로가 다르다"
    assert 'fetch("/api/kpi/agent")' in client, "프론트 호출 경로가 다르다 (Vite 프록시 /api → 8000)"
    assert "/kpi/agent" in spec, "화면정의서 S8 의 API 표기와 다르다"



# ── 라우트 (커넥션 대여·반납) ────────────────────────────────────────────────
#   여기가 이 변경에서 **실패 양상이 가장 미묘한 부분**인데 그동안 전혀 안 덮여 있었다.
#   FastAPI 없이 함수만 부른다 — 라우트 본문은 순수 파이썬이다.
class _FakePool:
    """getconn/putconn 을 기록하는 최소 풀. `exhausted=True` 면 psycopg2 처럼 **즉시 던진다**."""

    def __init__(self, conn=None, exhausted=False):
        self.conn = conn
        self.exhausted = exhausted
        self.returned = []          # [(conn, close)]

    def getconn(self):
        if self.exhausted:
            raise RuntimeError("connection pool exhausted")
        return self.conn

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))


def _route(monkeypatch, pool, params=None):
    """`main.kpi()` 를 호출하고 (결과, 예외) 를 돌려준다."""
    from src.gateway import main as gw

    monkeypatch.setitem(gw._gate, "pool", pool)
    if params is not None:
        monkeypatch.setattr(gw, "PARAMS", params)
    try:
        return gw.kpi(), None
    except Exception as e:                       # noqa: BLE001
        return None, e


_OPS = {"operations": {"kpi_window_hours": 24, "kpi_trend_buckets": 7, "kpi_pareto_limit": 8}}


def test_route_returns_connection_healthy_on_success(monkeypatch):
    conn = _conn()
    pool = _FakePool(conn)
    out, err = _route(monkeypatch, pool, _OPS)
    assert err is None and out["today"] == 3
    assert pool.returned == [(conn, False)], "성공인데 커넥션을 폐기했다"


def test_route_discards_connection_on_query_failure(monkeypatch):
    """⚠️ 조회가 터진 커넥션은 **폐기 반납**해야 한다.

    중단된 tx 를 그대로 되돌리면 **다음 대여자**가 첫 쿼리에서 InFailedSqlTransaction 을
    맞는다 — KPI 와 무관한 승인·인시던트 조회까지 연쇄로 죽는다(8/12 실측: 공용 커넥션
    오염으로 `/approvals/pending`·`/incidents/recent` 가 동시에 500 이었다).
    """
    class _Boom(_FakeConn):
        def cursor(self):
            raise RuntimeError("query boom")

    conn = _Boom([])
    pool = _FakePool(conn)
    out, err = _route(monkeypatch, pool, _OPS)
    assert err is not None and getattr(err, "status_code", None) == 500
    assert pool.returned == [(conn, True)], "터진 커넥션을 그대로 풀에 돌려보냈다"


def test_route_503_when_pool_missing(monkeypatch):
    """DB 미배선은 500 이 아니라 **503** — 화면이 목업 폴백으로 조용히 내려간다."""
    out, err = _route(monkeypatch, None, _OPS)
    assert getattr(err, "status_code", None) == 503


def test_route_pool_exhaustion_is_caught(monkeypatch):
    """⚠️ `getconn()` 은 고갈 시 **블록하지 않고 던진다**. try 밖에 두면 그 단서가 안 남는다."""
    pool = _FakePool(None, exhausted=True)
    out, err = _route(monkeypatch, pool, _OPS)
    assert getattr(err, "status_code", None) == 500
    assert pool.returned == [], "빌리지도 못했는데 반납을 시도했다"


def test_route_missing_config_key_is_caught(monkeypatch):
    """⚠️ config 유실(TSR-0006 형태)은 잡히고, **커넥션을 빌리기 전에** 걸려야 한다.

    인자 자리에서 키를 읽으면 이미 대여한 뒤에 KeyError 가 나서, 쓰지도 못할 커넥션을
    빌렸다 반납하는 왕복이 생긴다 — 풀이 좁을 때(minconn 1) 남의 몫을 뺏는다.
    """
    pool = _FakePool(_conn())
    out, err = _route(monkeypatch, pool, {"operations": {}})       # 키 3개 없음
    assert getattr(err, "status_code", None) == 500
    assert pool.returned == [], "config 에서 터졌는데 커넥션을 빌렸다"

    pool2 = _FakePool(_conn())
    out2, err2 = _route(monkeypatch, pool2, {})                    # operations 블록 자체 없음
    assert getattr(err2, "status_code", None) == 500
    assert pool2.returned == []
