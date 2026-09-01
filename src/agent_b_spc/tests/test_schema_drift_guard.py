"""B 통합테스트가 전제하는 스키마를 **한 곳에서 fail 로** 지킨다 (스키마 표류 감지).

## 왜 필요한가

B 통합테스트 8종이 각자 `information_schema` 를 probe 해서 **필요한 컬럼·테이블이 없으면 skip**
한다. 환경(DB 없음)에서는 정당한 처리지만, **DB 는 살아있는데 스키마만 낡은** 경우까지 같이
skip 되는 게 문제다 — 진짜 결함인데 **아무도 모르는 채 통과**한다.

실제로 당했다: `limit_corrections.center_modified` 3종이 `init.sql` 에만 있고 마이그레이션이
없어(2026-07-23 ~ 08-08) **승인 적용 경로 전체가 막혀 있었는데**(`approval_apply._SELECT_PROPOSED`
가 컬럼을 명시 나열 → 컬럼 부재 시 `UndefinedColumn` 으로 SELECT 자체가 실패 → `MODIFIED` 뿐
아니라 평범한 `APPROVE` 도 적용 불가) 2주 동안 아무 신호가 없었다. 발견은 C 가 컨테이너로
처음 띄워본 뒤였고, 마이그레이션 `0011`(#134)로 닫혔다.

## ⚠️ 이건 **로컬 가드**다 — CI 가 지켜주지 않는다

`.github/workflows/tests.yml` 의 pytest 경로는 `tests/` · `src/agent_service/tests/` 뿐이라
**`src/agent_b_spc/tests/` 는 수집되지 않는다.** 이 파일이 있어도 **CI 는 그대로 초록불**이다
(C 리뷰 #139 지적 — 그 착각을 남겨두면 다음 사람이 "CI 가 잡아준다" 고 믿게 되는데, 그게 이
파일이 없애려는 착각과 같은 종류다).

값은 **로컬에서 즉시 드러난다**는 데 있다 — 실제로 C 가 이 PR 리뷰 중 자기 로컬에 `0010`(quals)
이 안 들어와 있던 걸 발견했다(8/7 dev 머지분 미적용). 스키마 표류는 **팀 전체가 CI 로는 못 잡는
상태**이고, CI 편입은 postgres service 컨테이너 + 마이그레이션 적용 스텝이 필요해 별건이다.

## 이 파일의 계약

- **DB 접속 불가 → skip.** 환경 부재는 정당하고, 이 파일이 막을 일이 아니다.
- **DB 접속 가능한데 스키마 미충족 → fail.** 이게 이 파일의 존재 이유다. 개별 테스트들은
  여전히 skip 하더라도, 여기 한 줄이 **뚜렷하게 red** 로 이유를 말한다.

새 컬럼·테이블을 통합테스트가 전제하게 되면 `_REQUIRED` 에 한 줄 추가한다 —
그 자리가 **"이 스키마가 없으면 B 통합테스트는 의미가 없다"** 의 단일 목록이다.
"""
import logging
import os

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_SEC = 3

# (테이블, 컬럼 또는 None=테이블 존재만, 그 스키마를 전제하는 곳)
#   출처 = 각 통합테스트의 skip 게이트. 여기가 그 게이트들의 단일 목록이다.
_REQUIRED = [
    ("limit_corrections", "delta_sigma",      "B4-3 재산정 — test_recalc_writer·integration·spc_consumer"),
    ("limit_corrections", "center_modified",  "수정 승인 — test_approval_apply (마이그레이션 0011)"),
    ("limit_corrections", "ucl_modified",     "수정 승인(분위수 상한) — approval_apply (0011)"),
    ("limit_corrections", "lcl_modified",     "수정 승인(분위수 하한) — approval_apply (0011)"),
    ("control_limits",    "trigger_type",     "가한계 seam — test_provisional_seams"),
    ("quals",             "confirmed_verdict", "Qual 확정 — test_qual_recorder·spc_consumer_qual (0010)"),
    ("quals",             "confirmed_at",     "Qual 확정 시각 — qual_recorder (0010)"),
    ("recipe_corrections", None,              "Recipe 엔진 — test_recipe_writer"),
    # ⚠️ TEMP — `db/init.sql` 에 B6-3-b 로컬 선반영(주석 TEMP)이고 **PM 공식 승인 대기**다
    #   (`db/init.sql` 은 PM/PO 소유 — 헌법 3-1). 공식화하며 테이블명이 바뀌면 여기가 **오탐**을
    #   내므로, 그때 이 줄을 같이 고친다. (봇 리뷰 #139 지적)
    ("y_thresholds",      None,               "Y-임계 — test_establish_engine·spc_consumer [TEMP·PM 승인 대기]"),
]

_TABLE_SQL = text("SELECT count(*) FROM information_schema.tables WHERE table_name = :t")
_COLUMN_SQL = text("SELECT count(*) FROM information_schema.columns "
                   "WHERE table_name = :t AND column_name = :c")


def _engine():
    """DB 접속 가능하면 engine, 아니면 None (환경 부재 = skip 사유).

    실패 사유를 **로그로 남긴다** — 자격증명 오류·포트 오타처럼 "DB 는 떠 있는데 못 붙는" 경우가
    조용히 skip 되면 **이 가드 자체가 green 으로 무력화**된다(봇 리뷰 #139. 헌법 7장 "연결 실패를
    예외로 안 알려주는 클라이언트" 와 같은 결).
    """
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        log.info("스키마 가드 skip — DATABASE_URL 미설정")
        return None
    try:
        eng = create_engine(url, connect_args={"connect_timeout": _PROBE_TIMEOUT_SEC})
        with eng.connect():                      # 접속 자체를 확인 (지연 연결이라 필요)
            pass
        return eng
    except Exception as e:                       # noqa: BLE001 — 접속 불가는 skip(단, 사유는 남긴다)
        log.warning("스키마 가드 skip — DB 접속 실패(%s): %s", type(e).__name__, e)
        return None


_ENGINE = _engine()

pytestmark = pytest.mark.skipif(
    _ENGINE is None,
    reason="DATABASE_URL 미설정·접속 불가 — 환경 부재는 정당한 skip (스키마 미충족과 구분)")


@pytest.fixture(scope="module", autouse=True)
def _dispose_engine():
    """세션 종료 시 모듈 전역 엔진 커넥션 정리 (봇 리뷰 #139 — 경미)."""
    yield
    if _ENGINE is not None:
        _ENGINE.dispose()


def _missing() -> list:
    """전제 스키마 중 실제 DB 에 없는 것 — `(설명, 대상)` 목록."""
    out = []
    with _ENGINE.connect() as c:
        for table, column, why in _REQUIRED:
            if column is None:
                ok = c.execute(_TABLE_SQL, {"t": table}).scalar() == 1
                target = f"{table} (테이블)"
            else:
                ok = c.execute(_COLUMN_SQL, {"t": table, "c": column}).scalar() == 1
                target = f"{table}.{column}"
            if not ok:
                out.append((target, why))
    return out


def test_required_schema_present():
    """DB 가 살아있으면 B 통합테스트 전제 스키마는 **반드시** 있어야 한다.

    실패하면 `db/migrations/` 의 미적용 분이 있다는 뜻이다. `db/migrations/README.md` 의
    적용 명령을 1회 돌리면 된다. **skip 이 아니라 fail 인 것이 핵심** — 스키마 표류는
    환경 문제가 아니라 결함이고, skip 으로 넘기면 **아무 신호 없이** 덮인다
    (이 파일은 로컬 가드다 — CI 는 `src/agent_b_spc/tests/` 를 수집하지 않는다. 모듈 docstring 참조).
    """
    missing = _missing()
    assert not missing, (
        "B 통합테스트 전제 스키마 누락 — db/migrations 미적용분이 있습니다"
        " (README 의 적용 명령 1회 실행):\n"
        + "\n".join(f"  · {target}  ← {why}" for target, why in missing))
