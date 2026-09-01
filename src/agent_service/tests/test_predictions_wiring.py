"""wafer 처분 입력 배선 — `wafer_predictions` 조회 + alert 보험 (C6-2).

**왜 이 배선이 필요한가**: `decide_wafer` 는 wafer 한 장마다 수치를 비교하는데(판정 트리
STEP 1~4), alert 의 `prediction_context` 는 **대표 wafer 1장** 스냅샷이라 구간 전체를
판정할 수 없다. 값이 1장뿐이면 `n_exceed` 가 최대 1이라 STEP 3("2장 이상 → R2R")이
**구조적으로 발동 불가**다.

여기서 잠그는 것:
  · 배선 자체 — pipeline 이 `predictions` 를 실제로 넘긴다 (넘기지 않던 것이 이 작업의 사유)
  · 우선순위 — `member_wafers` 가 있으면 그 목록으로 정확 조회, 없으면 챔버 최근분 스캔
  · 레이스 보험 — DB 에 없는 **대표 wafer** 만 alert 값으로 메우고, 있으면 **덮지 않는다**
  · 무중단 — Postgres 미기동이면 빈 dict → 전 wafer HOLD(원위치·가역), 예외 전파 없음
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app import db as db_mod  # noqa: E402
from agent_service.app import pipeline  # noqa: E402
from agent_service.app.schemas.alert import AlertModel  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"


def _alert(name: str) -> AlertModel:
    return AlertModel.model_validate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def _any_alert() -> AlertModel:
    """윈도우가 있는 fixture 1건 — 없으면 아무거나."""
    for p in sorted(FIXTURES.glob("*.json")):
        try:
            a = AlertModel.model_validate(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        if a.suspect_window is not None:
            return a
    return _alert(sorted(FIXTURES.glob("*.json"))[0].name)


# --- 조회 우선순위 -------------------------------------------------------------

def test_member_wafers_drives_exact_lookup(monkeypatch) -> None:
    """`member_wafers` 가 있으면 **그 목록으로** 조회한다 — 챔버 전체 스캔이 아니라.

    B 가 위반 판정에 실제로 쓴 레코드라 가장 정확하고, 스캔보다 싸다.
    """
    alert = _any_alert()
    if alert.suspect_window is None:
        return  # 윈도우 없는 fixture 뿐이면 이 축은 검증 대상 아님
    wanted = ["C64_100", "C64_102", "C64_105"]
    alert = alert.model_copy(
        update={"suspect_window": alert.suspect_window.model_copy(update={"member_wafers": wanted})}
    )

    seen: dict = {}

    def _fake(chamber_id, wafer_ids=None, limit=None):
        seen["chamber_id"] = chamber_id
        seen["wafer_ids"] = wafer_ids
        return {w: {"predicted_c65": 1000.0, "anomaly_score": None} for w in wanted}

    monkeypatch.setattr(db_mod, "fetch_predictions", _fake)
    out = pipeline._fetch_predictions(alert)

    assert seen["wafer_ids"] == wanted          # 목록 그대로 전달
    assert seen["chamber_id"] == alert.chamber_id
    assert set(wanted).issubset(out)


def test_no_member_wafers_falls_back_to_chamber_scan(monkeypatch) -> None:
    """`member_wafers` 미도착이면 `wafer_ids=None` — 챔버 최근분 스캔 경로."""
    alert = _any_alert()
    if alert.suspect_window is not None:
        alert = alert.model_copy(
            update={"suspect_window": alert.suspect_window.model_copy(update={"member_wafers": None})}
        )

    seen: dict = {}

    def _fake(chamber_id, wafer_ids=None, limit=None):
        seen["wafer_ids"] = wafer_ids
        return {}

    monkeypatch.setattr(db_mod, "fetch_predictions", _fake)
    pipeline._fetch_predictions(alert)
    assert seen["wafer_ids"] is None


# --- 레이스 보험 ---------------------------------------------------------------

def test_representative_wafer_filled_from_alert_when_db_misses(monkeypatch) -> None:
    """DB 에 없는 **대표 wafer** 는 alert 값으로 메운다.

    `suspect_window` 는 소급 구간이라 대부분 과거 wafer(적재 완료)다. sink 적재가 늦어
    빠질 수 있는 건 알람을 유발한 최신 1장뿐이고, 그 장이 곧 alert 이 싣고 온 wafer 다.
    """
    alert = _any_alert()
    monkeypatch.setattr(db_mod, "fetch_predictions", lambda *a, **k: {})

    out = pipeline._fetch_predictions(alert)
    rep = alert.prediction_context.wafer_id

    assert rep in out
    assert out[rep]["predicted_c65"] == alert.prediction_context.predicted_c65


def test_db_value_wins_over_alert_for_same_wafer(monkeypatch) -> None:
    """DB 에 값이 있으면 alert 로 **덮지 않는다** — DB 쪽이 `fdc.actual` 병합까지 반영된 최신."""
    alert = _any_alert()
    rep = alert.prediction_context.wafer_id
    db_row = {"predicted_c65": 1234.5, "anomaly_score": 0.42}
    monkeypatch.setattr(db_mod, "fetch_predictions", lambda *a, **k: {rep: dict(db_row)})

    out = pipeline._fetch_predictions(alert)
    assert out[rep] == db_row
    assert out[rep]["predicted_c65"] != alert.prediction_context.predicted_c65


# --- 무중단 (헌법 6-2) ----------------------------------------------------------

def test_db_failure_yields_empty_not_exception(monkeypatch) -> None:
    """Postgres 미기동이어도 예외를 던지지 않고 빈 dict — 판정은 HOLD(원위치)로 간다.

    조회 실패가 파이프라인을 죽이면 알람 1건이 서비스 전체를 멈춘다(6-2 위반).
    """
    monkeypatch.setattr(db_mod, "dsn", lambda: "postgresql://x:y@127.0.0.1:59999/none")
    monkeypatch.setattr(db_mod, "CONNECT_TIMEOUT_SEC", 1)
    assert db_mod.fetch_predictions("SIM_CH_3", wafer_ids=["C64_1"]) == {}
    assert db_mod.fetch_predictions("SIM_CH_3") == {}


def test_fetch_predictions_still_returns_representative_on_db_failure(monkeypatch) -> None:
    """DB 가 죽어도 대표 wafer 1장은 살아남는다 — alert 이 값을 갖고 있으므로."""
    alert = _any_alert()
    monkeypatch.setattr(db_mod, "dsn", lambda: "postgresql://x:y@127.0.0.1:59999/none")
    monkeypatch.setattr(db_mod, "CONNECT_TIMEOUT_SEC", 1)

    out = pipeline._fetch_predictions(alert)
    assert list(out) == [alert.prediction_context.wafer_id]


# --- 배선 자체 (이 작업의 사유) --------------------------------------------------

def test_pipeline_passes_predictions_to_supervisor() -> None:
    """`predictions` 인자가 **실제로 전달**되는지 — 이전에는 넘기지 않아 항상 None 이었다.

    소스에 대한 단언이라 리팩터링에 약하지만, 이 한 줄이 빠지면 전 wafer 가 첫 분기에서
    "예측 조회 불가" HOLD 로 빠지고 **테스트로는 드러나지 않는다**(조용한 회귀).
    """
    src = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert "predictions=_fetch_predictions(alert)" in src
