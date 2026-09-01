"""공유 dev DB 를 쓰는 통합테스트용 **스키마 격리** 헬퍼 (TSR-0003).

이 프로젝트의 통합테스트는 `DATABASE_URL` 이 붙으면 **운영 중인 dev DB 에 그대로 커밋**한다.
fixture teardown 이 `DELETE FROM <table>` 같은 광범위 정리를 하면 **라이브 데이터가 지워진다** —
실측 2건:

- `control_limits` 180행(시딩 관리선) 소실 → 컨슈머 재시작 시 탐지 전멸 (#160 · TSR-0003)
- `spc_violations` 61,503행 소실 → `DELETE FROM spc_violations`(WHERE 없음) 재현 확인

두 경우 모두 **예외가 나지 않는다.** 테스트는 초록으로 통과하고 라이브만 빈다.

해결은 "정리를 얌전히" 가 아니라 **테스트가 다른 테이블을 보게** 하는 것이다. 전용 스키마에
사본을 만들고 `search_path` 로 그쪽을 먼저 보게 하면, 프로덕션 코드는 스키마를 모르는 채
격리된다(테이블 이름만 쓰므로).

사용:

    with isolated_schema(["control_limits"]) as engine:
        ...                                  # engine 은 사본만 본다
    # 블록을 나오면 스키마 DROP CASCADE

병렬 실행(pytest-xdist) 대비로 스키마명에 워커 ID 를 붙인다 — 워커끼리 같은 스키마를
`DROP ... CASCADE` 하는 경합을 막는다.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

_SCHEMA_PREFIX = "aitch_test"
_PROBE_TIMEOUT_SEC = 2      # 헌법 6-1 — DB 미기동 시 오래 매달리지 않게 짧게 바운드


def database_url() -> str:
    """`.env`/환경의 `DATABASE_URL`. 없으면 명시 실패(통합테스트는 skip 되어야 한다)."""
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL 이 없습니다 — 통합테스트는 skip 되어야 합니다.")
    return url


def db_available() -> bool:
    """짧은 타임아웃으로 접속만 시도해 통합테스트 가용 여부를 판정한다(원인 불문 False)."""
    try:
        url = database_url()
        probe = create_engine(url, connect_args={"connect_timeout": _PROBE_TIMEOUT_SEC})
        try:
            probe.connect().close()
        finally:
            probe.dispose()
        return True
    except Exception:                       # noqa: BLE001 — "접속 가능한가"만 판정
        return False


def schema_name() -> str:
    """이 프로세스가 쓸 스키마명. xdist 워커별로 갈라 DROP 경합을 피한다."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    return f"{_SCHEMA_PREFIX}_{worker}" if worker else _SCHEMA_PREFIX


def with_search_path(url: str, schema: str) -> str:
    """URL 에 psycopg2 `options=-csearch_path=<schema>,public` 을 덧붙인다(기존 쿼리 보존).

    `public` 을 뒤에 두는 이유: 사본 테이블의 id DEFAULT 가 public 시퀀스를 참조하므로 그쪽도
    해석돼야 한다. 대상 테이블은 사본이 앞서므로 전용 스키마로 해석된다.
    """
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["options"] = f"-csearch_path={schema},public"
    return urlunsplit(parts._replace(query=urlencode(query)))


def assert_isolated(engine, table: str, schema: str) -> None:
    """`table` 이 정말 전용 스키마로 해석되는지 **조회로 확인**한다.

    격리를 가정하지 않는다 — 여기서 조용히 `public` 이면 뒤따르는 정리가 라이브를 지운다.
    `assert` 가 아니라 `raise` 인 것은 `python -O` 로 방어선이 사라지지 않게 하기 위함(헌법 7장).
    """
    with engine.connect() as conn:
        # `CAST(:t AS regclass)` — `:t::regclass` 로 쓰면 SQLAlchemy 가 `::` 를 바인드 문법과
        #   섞어 파싱해 파라미터가 비어 온다(실측: parameters={} 로 setup 실패).
        resolved = conn.execute(text(
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = CAST(:t AS regclass)"
        ), {"t": table}).scalar_one()
    if resolved != schema:
        raise RuntimeError(
            f"격리 실패: {table} 이 '{resolved}' 스키마로 해석됩니다(기대 '{schema}'). "
            f"이대로 진행하면 테스트 정리가 라이브 데이터를 지웁니다 — TSR-0003."
        )


@contextmanager
def isolated_schema(tables: list, *, engine_factory=None):
    """`tables` 사본을 전용 스키마에 만들고, 그쪽만 보는 엔진을 내준다.

    Args:
        tables: 격리할 public 테이블 이름들. `LIKE public.<t> INCLUDING ALL` 로 복제하므로
            컬럼·인덱스(부분 UNIQUE 포함)가 원본을 자동 추종한다 — DDL 단일 소스는 `db/init.sql`.
        engine_factory: `create_engine` 대체 seam(운영 헬퍼 재사용용). `url` 키워드로 호출한다.

    Yields:
        전용 스키마를 보는 Engine. 블록을 나오면 스키마를 `DROP ... CASCADE` 한다.

    Note:
        FK·트리거는 `INCLUDING ALL` 로도 복제되지 않는다. 대상 테이블에 그것들이 생기면
        이 헬퍼를 함께 갱신해야 한다.
    """
    url = database_url()
    schema = schema_name()
    make = engine_factory or (lambda url: create_engine(url, future=True))

    admin = make(url=url)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            for t in tables:
                conn.execute(text(
                    f'CREATE TABLE "{schema}".{t} (LIKE public.{t} INCLUDING ALL)'))

        engine = make(url=with_search_path(url, schema))
        try:
            for t in tables:
                assert_isolated(engine, t, schema)
            yield engine
        finally:
            engine.dispose()
    finally:
        try:
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception:                   # noqa: BLE001 — 정리 실패가 테스트 결과를 뒤엎지 않게
            logger.warning("테스트 스키마 정리 실패(무시): %s", schema, exc_info=True)
        admin.dispose()                     # setup 중간 실패 경로에서도 반납(#160 리뷰 지적)
