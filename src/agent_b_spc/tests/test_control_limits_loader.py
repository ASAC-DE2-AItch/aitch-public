"""B3-2 Task4: seed → load → NelsonEngine 왕복 통합테스트.

실 Postgres가 필요하다 — `DATABASE_URL` 미접속 환경에서는 전체 skip(exit 0)한다.
`seed_control_limits.seed()`로 실제 DDL 컬럼까지 왕복 적재하고, `control_limits_loader.load()`가
NelsonEngine이 바로 소비 가능한 `(limits, whitelist)` 형태로 되돌아오는지, 챔버별 오프셋
분리(shift)와 quantile(KEEP*) 그룹의 σ-존 룰 제외 계약(헌법 1-1 예외 2)이 왕복 후에도
유지되는지를 검증한다.

**격리 (TSR-0003)**: 이 파일은 `seed()`를 *실제로 커밋 실행*한다. `seed()`의 DELETE 범위는
챔버가 아니라 `trigger_type + limit_version` **전역**이고(설계 의도 — 재생성으로 사라진
그룹의 옛 행까지 정리), `rows` fixture는 실 CSV·실 offsets에서 만든 **라이브와 동일한 180행**
이다. 따라서 public 스키마에서 돌리면 **운영 중인 관리선 기준선이 통째로 지워진다**(실측:
2026-08-09 활성 0 · 컨슈머 재시작 시 탐지 전멸). 그래서 전용 스키마에 `control_limits`
사본을 만들고 `search_path`로 그쪽만 보게 한다 — 프로덕션 코드는 스키마를 모르는 채 그대로 돈다.
격리 기계는 `common/context_score/tests/db_schema_isolation.py`가 소유한다(같은 사고가
`test_publisher.py`에서도 나 공용화 — 2026-08-09).
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import text

from src.agent_b_spc.control_limits_loader import load
from src.agent_b_spc.nelson_engine import NelsonEngine, Point
from src.agent_b_spc.seed_control_limits import (
    _DEFAULT_CSV_DIR,
    _DEFAULT_OFFSETS_PATH,
    LIMIT_VERSION,
    TRIGGER_INITIAL,
    build_seed_rows,
    load_offsets,
    make_engine,
    seed,
)
from src.common.context_score.tests.db_schema_isolation import db_available, isolated_schema


def _count_initial_rows(conn, chamber_id: str | None = None) -> int:
    """control_limits의 이번 시딩(trigger_type=initial·limit_version=v1) 행 수.

    `chamber_id` 지정 시 해당 챔버만 센다(값은 :c로 바인딩 — 정적 절만 상수 결합, 주입 없음).
    적재수·멱등·stale 제거 테스트의 동일 조회를 한 곳으로 모은다.
    """
    sql = "SELECT count(*) FROM control_limits WHERE trigger_type = :t AND limit_version = :v"
    params = {"t": TRIGGER_INITIAL, "v": LIMIT_VERSION}
    if chamber_id is not None:
        sql += " AND chamber_id = :c"
        params["c"] = chamber_id
    return conn.execute(text(sql), params).scalar_one()


pytestmark = pytest.mark.skipif(
    not db_available(), reason="DATABASE_URL 미접속 — 통합테스트 skip"
)


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    """실 CSV·offsets에서 시딩 row(build_seed_rows 결과)를 만든다 — 순수 함수, DB 미접촉."""
    limits_df = pd.read_csv(_DEFAULT_CSV_DIR / "initial_limits_v1.csv")
    whitelist_df = pd.read_csv(_DEFAULT_CSV_DIR / "monitoring_whitelist_v1.csv")
    offsets = load_offsets(_DEFAULT_OFFSETS_PATH)
    return build_seed_rows(limits_df, whitelist_df, offsets)


@pytest.fixture(scope="module")
def engine():
    """전용 스키마에 격리된 엔진 — **라이브 `public.control_limits`를 건드리지 않는다**(TSR-0003).

    격리 기계는 `db_schema_isolation.isolated_schema`가 소유한다 — 사본 생성
    (`LIKE ... INCLUDING ALL`로 부분 UNIQUE `idx_control_limits_active_one`까지 복제되어야
    가드 계약이 실검증된다) · `search_path` 주입 · **해석 스키마 실조회 확인** · DROP CASCADE.

    `engine_factory`로 운영 헬퍼 `make_engine`을 넘긴다 — `pool_pre_ping`·`connect_timeout`
    같은 운영 설정을 그대로 태워, 테스트가 운영과 다른 커넥션 경로를 타지 않게 한다.
    """
    with isolated_schema(["control_limits"],
                         engine_factory=lambda url: make_engine(database_url=url)) as eng:
        yield eng


@pytest.fixture
def seeded_engine(engine, rows):
    """`rows`를 `seed()`로 적재하고, 테스트 간 간섭이 없게 시딩분을 정리한다.

    delete 기준은 `trigger_type=TRIGGER_INITIAL`이라 테스트 도중 subset으로 재시딩해도
    (stale 그룹 제거 케이스) 남은 행까지 통째로 정리된다. **이 DELETE가 안전한 이유는
    `engine`이 전용 스키마에 격리돼 있기 때문**이며(위 fixture), public에서 돌면 라이브
    기준선을 지운다 — 그 사고가 TSR-0003이다.
    """
    seed(engine, rows)
    yield engine
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM control_limits WHERE trigger_type = :t"),
            {"t": TRIGGER_INITIAL},
        )


def test_seed_inserts_row_count_matches_monitored_times_chambers(seeded_engine, rows):
    # 적재 행수: 45(감시 그룹) x len(offsets)(챔버 수) = len(rows)
    with seeded_engine.connect() as conn:
        count = _count_initial_rows(conn)
    assert count == len(rows)


def test_load_round_trip_group_count_and_key_sets_match(seeded_engine, rows):
    # 왕복 동수: load()의 whitelist 그룹 수 == len(rows), limits·whitelist 그룹 집합 동일
    limits, whitelist = load(seeded_engine)
    expected_groups = {
        (r["chamber_id"], r["recipe_id"], r["step"], r["sensor_window"], r["sensor_id"])
        for r in rows
    }
    assert len(whitelist) == len(rows)
    assert whitelist == expected_groups
    assert set(limits.keys()) == expected_groups


def test_chamber_offset_separates_control_limits(seeded_engine, rows):
    # 챔버별 shift: 동일 (recipe,step,window,sensor)인데 챔버만 다른 두 gk는 관리선이 다르다.
    # UCL이 작은 챔버(lo)와 큰 챔버(hi) 사이 중간값 v를 각각 feed하면 lo만 N1이 발동한다
    # (챔버별 관리선 분리 증명).
    limits, whitelist = load(seeded_engine)
    sample = next(r for r in rows if r["chamber_id"] == "SIM_CH_1")
    gk1 = ("SIM_CH_1", sample["recipe_id"], sample["step"], sample["sensor_window"], sample["sensor_id"])
    gk2 = ("SIM_CH_2", sample["recipe_id"], sample["step"], sample["sensor_window"], sample["sensor_id"])
    assert gk1 in limits and gk2 in limits
    assert limits[gk1]["ucl"] != limits[gk2]["ucl"]

    lo_gk, hi_gk = (gk1, gk2) if limits[gk1]["ucl"] < limits[gk2]["ucl"] else (gk2, gk1)
    lo_ucl, hi_ucl = limits[lo_gk]["ucl"], limits[hi_gk]["ucl"]
    v = (lo_ucl + hi_ucl) / 2

    eng = NelsonEngine(limits, whitelist, time_gap_hours=72)
    lo_out = eng.feed(Point(
        group_key=lo_gk, wafer_id="W1", timestamp="2026-07-13T10:00:00.000Z",
        value=v, pm_count=1, limit_version=LIMIT_VERSION,
    ))
    hi_out = eng.feed(Point(
        group_key=hi_gk, wafer_id="W1", timestamp="2026-07-13T10:00:00.000Z",
        value=v, pm_count=1, limit_version=LIMIT_VERSION,
    ))
    assert any(x["rule_id"] == "N1" for x in lo_out)
    assert not any(x["rule_id"] == "N1" for x in hi_out)


def test_quantile_group_n1_fires_and_excludes_sigma_zone_rules(seeded_engine, rows):
    # (리뷰 반영) Zones.from_quantile은 s1p/s1m/s2p/s2m을 NaN으로 고정하므로 quantile
    # 그룹에서 N5~N8은 애초에 어떤 시퀀스를 넣어도 발화할 수 없다 — "quantile 그룹에서
    # N5~N8 미발행"만 확인하면 항상 참인 공허한 주장이라 아무것도 증명하지 못한다.
    # 그래서 test_nelson_engine.test_quantile_excludes_sigma_zone_rules_vs_sigma_group과
    # 동일한 패턴으로 sigma-method 대조군을 둔다: 같은 형태(center ± 2.5σ 2점 + center)의
    # 시퀀스를 sigma 그룹에 먼저 흘려 N5~N8이 실제로 발화함을 확인해 "이 시퀀스는 진짜
    # σ-존 트리거"임을 증명한 뒤, 그 시퀀스를 quantile 그룹 자신의 center/sigma로 그대로
    # 유도해 흘렸을 때 N5~N8이 안 나오는 것을 확인해야 비로소 판별력 있는 검증이 된다.
    # (N1 부분은 seed→load 왕복 후에도 quantile method·ucl이 유지되는지의 실제 신규
    # 커버리지이므로 원안 그대로 유지한다.)
    limits, whitelist = load(seeded_engine)
    sigma_gk = next(gk for gk, lim in limits.items() if lim["method"] == "sigma")
    quantile_gk = next(gk for gk, lim in limits.items() if lim["method"] == "quantile")

    # 대조군: sigma 그룹에 2σ-존 트리거 시퀀스(2/3점이 +2σ 밖) → N5~N8 중 최소 하나 발화
    sig_center = limits[sigma_gk]["center"]
    sig_sigma = limits[sigma_gk]["sigma"]
    ctrl_eng = NelsonEngine(limits, whitelist, time_gap_hours=72)
    ctrl_seen: set[str] = set()
    for i, v in enumerate([sig_center + 2.5 * sig_sigma, sig_center + 2.5 * sig_sigma, sig_center]):
        out = ctrl_eng.feed(Point(
            group_key=sigma_gk, wafer_id=f"CW{i}",
            timestamp=f"2026-07-13T09:{i:02d}:00.000Z",
            value=v, pm_count=1, limit_version=LIMIT_VERSION,
        ))
        ctrl_seen |= {x["rule_id"] for x in out}
    assert ctrl_seen & {"N5", "N6", "N7", "N8"}   # 대조군 자체가 σ-존 미발화면 이 검증 전체가 무의미

    # 판별 대상: 위와 동일한 형태의 시퀀스를 quantile 그룹 자신의 center/sigma로 유도해 흘림
    q_sigma = limits[quantile_gk]["sigma"]
    q_center = limits[quantile_gk]["center"]
    zone_eng = NelsonEngine(limits, whitelist, time_gap_hours=72)
    zone_seen: set[str] = set()
    for i, v in enumerate([q_center + 2.5 * q_sigma, q_center + 2.5 * q_sigma, q_center]):
        out = zone_eng.feed(Point(
            group_key=quantile_gk, wafer_id=f"ZW{i}",
            timestamp=f"2026-07-13T09:{i:02d}:00.000Z",
            value=v, pm_count=1, limit_version=LIMIT_VERSION,
        ))
        zone_seen |= {x["rule_id"] for x in out}
    assert not (zone_seen & {"N5", "N6", "N7", "N8"})   # 대조군은 발화, quantile은 미발화(판별력 있는 대비)

    # N1: 분위수 상한 초과 1점 → N1 발동. 이어지는 시퀀스에서 발행되는 rule_id는
    # {N1,N2,N3,N4} 안에만 있어야 하고 N5~N8(σ-존)은 한 번도 안 나와야 한다
    # (조합② — σ-존 룰 제외 계약, 헌법 1-1 예외 2).
    n1_eng = NelsonEngine(limits, whitelist, time_gap_hours=72)
    ucl = limits[quantile_gk]["ucl"]
    center = limits[quantile_gk]["center"]
    seq_values = [ucl + 1.0, ucl + 1.0, center, center, ucl + 1.0]

    seen_rule_ids: set[str] = set()
    for i, v in enumerate(seq_values):
        out = n1_eng.feed(Point(
            group_key=quantile_gk, wafer_id=f"W{i}",
            timestamp=f"2026-07-13T10:{i:02d}:00.000Z",
            value=v, pm_count=1, limit_version=LIMIT_VERSION,
        ))
        seen_rule_ids |= {x["rule_id"] for x in out}

    assert "N1" in seen_rule_ids
    assert seen_rule_ids <= {"N1", "N2", "N3", "N4"}
    assert not (seen_rule_ids & {"N5", "N6", "N7", "N8"})


def test_seed_is_idempotent_no_duplicate_rows(seeded_engine, rows):
    # 멱등: seed() 재실행해도 delete-then-insert라 중복 없이 동일 행수를 유지한다.
    seed(seeded_engine, rows)
    with seeded_engine.connect() as conn:
        count = _count_initial_rows(conn)
    assert count == len(rows)


def test_seed_removes_stale_chamber_rows_on_resubmit(seeded_engine, rows):
    # stale 그룹 제거: 한 챔버를 제외한 subset으로 재시딩하면 delete-then-insert가
    # (trigger_type+version 기준 삭제이므로) 제외된 챔버의 옛 행도 통째로 제거한다.
    dropped_chamber = rows[0]["chamber_id"]
    rows_subset = [r for r in rows if r["chamber_id"] != dropped_chamber]
    seed(seeded_engine, rows_subset)
    with seeded_engine.connect() as conn:
        count = _count_initial_rows(conn, dropped_chamber)
    assert count == 0
