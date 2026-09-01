"""B3-2 Task1: 순수 transform — (limits_df, whitelist_df, offsets) → control_limits 시딩 row.

`initial_limits_v1.csv`(원 챔버 C24_0 placeholder, 69그룹)와 `monitoring_whitelist_v1.csv`
(decision/method)를 GROUP_KEYS로 조인해 감시 대상(KEEP/KEEP*)만 추린 뒤,
`chamber_offsets.json`의 챔버별 센서 오프셋으로 밴드 전체를 shift해 SIM_CH_* 챔버로
전개한다. DB·파일 IO가 없는 순수 함수 — Task2(DB 시딩)가 이 결과를 감싸 INSERT한다.

shift 규칙 (설계 §3-3, 리뷰 v2):
  center' = center + off
  method=sigma(KEEP)    : ucl'=ucl+off, lcl'=lcl+off, k_sigma=CSV carry, q_low=q_high=None
  method=quantile(KEEP*): ucl'=q_ucl+off, lcl'=q_lcl+off, k_sigma=None, q_low/q_high=CSV carry
  sigma(통계량)는 불변.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from src.agent_b_spc import initial_limits
from src.common.config_util import _key, _section

_PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"

logger = logging.getLogger(__name__)

# 기본 경로 (repo 확인: src/agent_b_spc/limits/*.csv, config/chamber_offsets.json)
_DEFAULT_CSV_DIR = Path(__file__).resolve().parent / "limits"
_DEFAULT_OFFSETS_PATH = Path(__file__).resolve().parents[2] / "config" / "chamber_offsets.json"

GROUP_KEYS = ["chamber_id", "recipe_id", "step", "sensor_window", "sensor_id"]

# 상태값 상수 (헌법 6-1: 인라인 'v1'/'initial' 금지) — initial_limits의 단일 소스를 재사용.
LIMIT_VERSION = initial_limits.INITIAL_LIMIT_VERSION
TRIGGER_INITIAL = "initial"

# monitoring_whitelist의 감시 대상 판정값 (B3-1 소비 규칙과 동일)
MONITORED_DECISIONS = {"KEEP", "KEEP*"}

# 방출 화이트리스트 = control_limits DDL 컬럼(db/init.sql). id/effective_from/created_at은
# DB 디폴트라 여기서 만들지 않는다. CSV 전용 컬럼(cv/sample_min/sample_max/n_distinct/
# false_alarm_pct/n_used/q_lcl/q_ucl)은 여기 없어 자동으로 DROP된다.
DDL_COLUMNS = [
    "chamber_id", "recipe_id", "step", "sensor_window", "sensor_id",
    "limit_version", "method", "center", "sigma", "ucl", "lcl",
    "k_sigma", "q_low", "q_high", "n_wafers", "calc_window_n",
    "trigger_type", "qa_flags", "is_active",
]


def load_offsets(path: Path | str) -> dict[str, dict[str, float]]:
    """chamber_offsets.json을 읽어 `offsets`(챔버별 센서 오프셋)만 반환한다."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["offsets"]


def _native(value):
    """numpy 스칼라(int64/float64/bool_) → 파이썬 네이티브 (SMALLINT/FLOAT/BOOLEAN INSERT 정합, 헌법 6-1)."""
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _shift_row(r: pd.Series, ch: str, off: float) -> dict:
    """한 감시 그룹(row) x 챔버(off)의 shift·method 매핑을 DDL_COLUMNS dict로 만든다."""
    if r["method"] == "sigma":
        ucl, lcl = r["ucl"] + off, r["lcl"] + off
        k_sigma, q_low, q_high = r["k_sigma"], None, None
    elif r["method"] == "quantile":
        ucl, lcl = r["q_ucl"] + off, r["q_lcl"] + off
        k_sigma, q_low, q_high = None, r["q_low"], r["q_high"]
    else:
        raise ValueError(f"알 수 없는 method: {r['method']!r} (chamber={ch}, sensor={r['sensor_id']})")

    full = {
        "chamber_id": ch,
        "recipe_id": r["recipe_id"],
        "step": r["step"],
        "sensor_window": r["sensor_window"],
        "sensor_id": r["sensor_id"],
        "limit_version": LIMIT_VERSION,
        "method": r["method"],
        "center": r["center"] + off,
        "sigma": r["sigma"],
        "ucl": ucl,
        "lcl": lcl,
        "k_sigma": k_sigma,
        "q_low": q_low,
        "q_high": q_high,
        "n_wafers": r["n_wafers"],
        "calc_window_n": r["calc_window_n"],
        "trigger_type": TRIGGER_INITIAL,
        "qa_flags": r["qa_flags"],
        "is_active": True,
    }
    return {col: _native(full[col]) for col in DDL_COLUMNS}


def build_seed_rows(limits_df: pd.DataFrame, whitelist_df: pd.DataFrame, offsets: dict) -> list[dict]:
    """(limits_df, whitelist_df, offsets) → control_limits 시딩용 row dict 리스트.

    순수 함수(부작용·DB·파일 IO 없음). 조인 정합 가드 → 감시 필터(KEEP/KEEP*) →
    챔버 x 그룹 이중 루프로 shift·method 매핑을 적용한다.
    """
    merged = limits_df.merge(
        whitelist_df[GROUP_KEYS + ["decision", "method"]], on=GROUP_KEYS, how="inner"
    )
    # 가드 ⓑ: CSV(limits)⋈whitelist 조인 후 행수가 양쪽 원본과 달라지면 GROUP_KEYS 정합이
    # 깨진 것 — 소비자 몰래 그룹이 늘거나(중복 키) 줄면(불일치 키) 안 되므로 즉시 실패.
    if len(merged) != len(limits_df) or len(merged) != len(whitelist_df):
        raise ValueError(
            f"조인 후 행수 불일치: limits={len(limits_df)}, whitelist={len(whitelist_df)}, "
            f"merged={len(merged)} — GROUP_KEYS(chamber/recipe/step/window/sensor) 정합이 깨졌습니다."
        )

    monitored = merged[merged["decision"].isin(MONITORED_DECISIONS)]
    logger.info("감시 그룹 %d건(전체 %d건 중) × 챔버 %d개", len(monitored), len(merged), len(offsets))

    rows: list[dict] = []
    for ch, ch_offsets in offsets.items():
        for _, r in monitored.iterrows():
            sensor_id = r["sensor_id"]
            # 가드 ⓐ: 감시 대상 센서가 해당 챔버 offsets에 없으면 silent 0-shift 대신 즉시 실패.
            if sensor_id not in ch_offsets:
                raise ValueError(
                    f"오프셋 없음: chamber={ch}, sensor={sensor_id} — 감시 센서는 "
                    f"chamber_offsets.json[offsets][{ch}]에 반드시 존재해야 합니다(가드 ⓐ)."
                )
            off = ch_offsets[sensor_id]
            rows.append(_shift_row(r, ch, off))

    return rows


def _pg_connect_timeout() -> int:
    """Postgres 연결 타임아웃(초) — params `agent.pg_connect_timeout_sec` 단일 소스 재사용(D24).

    인라인 상수 금지(6-1): 미기동 DB에 OS 기본(수십초~분) 대기하면 컨테이너 무중단이 깨진다.
    시스템 공통 인프라 값이라 기존 등재 키를 재사용한다(중복 등재 시 drift).
    """
    with open(_PARAMS_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return int(_key(_section(cfg, "agent", _PARAMS_PATH), "pg_connect_timeout_sec", "agent", _PARAMS_PATH))


def make_engine(database_url: str | None = None, *, connect_timeout: int | None = None) -> Engine:
    """SQLAlchemy 엔진을 생성한다 (B6-2 G6 — DB 복원력).

    URL은 인자 우선, 없으면 환경변수 `DATABASE_URL`을 사용한다(부재 시 명시적
    `RuntimeError` — 헌법 6-1, 매직 폴백 금지). 설치된 SQLAlchemy가 1.4.54이므로
    `future=True`로 만들어 2.0-스타일 `engine.begin()`/`conn.execute(text(...), rows)`가
    1.4·2.0 양쪽에서 동작하게 한다.

    `pool_pre_ping`(죽은 커넥션 재확립)·`connect_timeout`(미기동 DB 무한대기 차단)으로
    컨테이너 재기동·네트워크 흔들림에 견디게 한다. timeout은 params 경유(6-1).
    """
    if database_url is None:
        load_dotenv()
        database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL이 없습니다 — --database-url 인자 또는 .env의 DATABASE_URL을 설정하세요."
        )
    if connect_timeout is None:
        connect_timeout = _pg_connect_timeout()
    return create_engine(
        database_url, future=True,
        pool_pre_ping=True,                                   # 정책 불리언 — 코드 상수 적정
        connect_args={"connect_timeout": connect_timeout},   # 수치 — params 경유(6-1)
    )


def assert_no_conflicting_active(conn: Connection) -> None:
    """가드 ⓒ: 이번 시딩(trigger_type+limit_version)이 지우지 못할 다른 active 행이 있으면 즉시 실패한다.

    delete-then-insert는 `trigger_type=TRIGGER_INITIAL AND limit_version=LIMIT_VERSION`
    행만 지운다. 감시 그룹키가 겹치는 다른 active 행(다른 trigger/version)이 남아 있으면
    UNIQUE(active 1개) 위반이나 이력 훼손으로 이어질 수 있으므로(헌법 4-1), 단순
    `version != 'v1'` 비교가 아니라 이 강한 술어로 선제 차단한다.
    """
    row = conn.execute(
        text(
            "SELECT 1 FROM control_limits "
            "WHERE is_active AND NOT (trigger_type = :t AND limit_version = :v) "
            "LIMIT 1"
        ),
        {"t": TRIGGER_INITIAL, "v": LIMIT_VERSION},
    ).first()
    if row is not None:
        raise RuntimeError(
            "충돌하는 active 행이 존재합니다 — 다른 trigger_type/limit_version의 active 행이 "
            "남아 있으면 이번 시딩(delete-then-insert)이 지우지 못해 UNIQUE(active 1개) 위반이나 "
            "이력 훼손을 일으킬 수 있습니다(헌법 4-1). 해당 행을 먼저 정리하세요."
        )


def seed(engine: Engine, rows: list[dict]) -> int:
    """`rows`(build_seed_rows 결과)를 하나의 트랜잭션으로 control_limits에 delete-then-insert한다.

    순서: 빈 rows 가드 → 가드(assert_no_conflicting_active) → DELETE(trigger_type+limit_version
    기준) → bulk INSERT(DDL_COLUMNS). 삭제 기준이 그룹키가 아니라 trigger_type+version이므로,
    재생성으로 사라진 그룹의 옛 행도 통째로 제거·재구성된다. 반환값 = 적재 행 수(len(rows)).
    """
    # 가드 ⓐ: rows가 비면 DELETE만 커밋되어 기존 initial 행이 조용히 소실된다(INSERT 0건은
    # 무연산). csv-dir/offsets 경로 오지정·화이트리스트 필터 공집합 등으로 rows가 빌 수 있으므로
    # DB를 건드리기 전에 fail-fast 한다 (헌법 7장 — 조용한 기능 정지 방지).
    if not rows:
        raise RuntimeError(
            "시딩할 rows가 0건입니다 — 진행하면 DELETE만 커밋되어 기존 initial 행이 소실됩니다. "
            "--csv-dir/--offsets 경로와 화이트리스트(KEEP/KEEP*) 필터 결과를 확인하세요."
        )

    columns_clause = ", ".join(DDL_COLUMNS)
    params_clause = ", ".join(f":{col}" for col in DDL_COLUMNS)
    insert_sql = text(
        f"INSERT INTO control_limits ({columns_clause}) VALUES ({params_clause})"
    )

    with engine.begin() as conn:
        assert_no_conflicting_active(conn)
        conn.execute(
            text("DELETE FROM control_limits WHERE trigger_type = :t AND limit_version = :v"),
            {"t": TRIGGER_INITIAL, "v": LIMIT_VERSION},
        )
        conn.execute(insert_sql, rows)

    return len(rows)


def _seeded_count(conn: Connection) -> int:
    """control_limits **활성** 행수 — 재기동 시 재시딩 여부 판정(G4, §1-⑤).

    `is_active` 한정이 핵심: 전체 행수로 판정하면 **비활성 이력만 남은 DB**(승인으로 구버전이
    전부 은퇴한 경우)에서 count>0 → 시딩 skip → 활성 관리선 0 → G5(`_require_whitelist`)가
    fail-fast로 컨슈머를 죽이는 조합이 생긴다(PM #88 리뷰 ③). 활성만 세면 이 함정이 닫힌다.
    """
    return int(
        conn.execute(text("SELECT count(*) FROM control_limits WHERE is_active")).scalar() or 0
    )


def _log_summary(rows: list[dict]) -> None:
    """dry-run·실적재 공통 요약(총 행수·챔버 수·method 분포)을 logging으로 출력한다(헌법 6-1 — print 금지)."""
    chambers = {row["chamber_id"] for row in rows}
    method_counts = Counter(row["method"] for row in rows)
    logger.info(
        "시딩 요약: 총 %d행, 챔버 %d개, method 분포=%s",
        len(rows), len(chambers), dict(method_counts),
    )


def main() -> None:
    """CLI 진입점: CSV·offsets를 읽어 build_seed_rows로 변환 후, --dry-run이면 요약만, 아니면 DB에 적재한다."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="control_limits 초기(v1) 시딩")
    parser.add_argument("--dry-run", action="store_true", help="DB 미접속, build_seed_rows 결과 요약만 출력")
    parser.add_argument("--csv-dir", type=Path, default=_DEFAULT_CSV_DIR, help="initial_limits_v1.csv·monitoring_whitelist_v1.csv가 있는 디렉토리")
    parser.add_argument("--offsets", type=Path, default=_DEFAULT_OFFSETS_PATH, help="chamber_offsets.json 경로")
    parser.add_argument("--database-url", type=str, default=None, help="미지정 시 .env의 DATABASE_URL 사용")
    parser.add_argument("--skip-if-seeded", action="store_true",
                        help="control_limits에 행이 이미 있으면 시딩을 건너뛰고 exit 0 "
                             "(compose 재기동 멱등 — 승인 적용분이 있으면 가드 전에 끊는다, §1-⑤)")
    args = parser.parse_args()

    limits_df = pd.read_csv(args.csv_dir / "initial_limits_v1.csv")
    whitelist_df = pd.read_csv(args.csv_dir / "monitoring_whitelist_v1.csv")
    offsets = load_offsets(args.offsets)

    rows = build_seed_rows(limits_df, whitelist_df, offsets)
    _log_summary(rows)

    if args.dry_run:
        logger.info("dry-run: DB 미접속, 종료합니다.")
        return

    engine = make_engine(args.database_url)
    if args.skip_if_seeded:                              # G4 — 재시딩은 멱등 아님(가드가 승인분에 실패)
        with engine.connect() as conn:
            existing = _seeded_count(conn)
        if existing > 0:
            logger.info("--skip-if-seeded: control_limits 이미 %d행 — 시딩 건너뜀(exit 0).", existing)
            return
    inserted = seed(engine, rows)
    logger.info("적재 완료: %d행", inserted)


if __name__ == "__main__":
    main()
