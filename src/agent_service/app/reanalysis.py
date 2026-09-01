"""재분석 폴링 — `[재분석]` 버튼이 세운 플래그를 소비해 Brief 를 재생성한다 (PM-4).

흐름 (PR #70 결정 4 · #80 코멘트 ⓐ안):

    S4 [재분석] 클릭
      → gateway `/incidents/{id}/reanalyze` 가 최신 SUP 행에 needs_reanalysis = TRUE
      → (여기부터 우리) 주기 폴링 → incident_alerts.raw 에서 원본 alert 복원
      → 파이프라인 재실행 → **같은 행 UPDATE** + brief_version + 1 + 플래그 내림

왜 자동 재생성이 아니라 사람이 당기나 (gateway 주석 인용):
    "자동 재생성은 '빨리 내면 정보 부족, 기다리면 늦음' 딜레마에 빠지고 pending 이 사람
     모르게 바뀐다. 사람이 당기는 구조면 그 딜레마가 소멸하고 감사도 깨끗하다."

왜 별도 스레드인가: consumer 루프(`run_kafka`)는 poll 이 최대 1초 블로킹이라 그 안에 끼우면
폴링이 그만큼 늦고, 알람이 없는 구간에는 루프 본문이 아예 안 돈다. PM 의 승인 자동시작
폴러(`gateway.main._start_approval_autostart`)와 같은 형태 — **데몬 스레드 + 전용 커넥션**.
psycopg 커넥션은 스레드 안전하지 않으므로 커넥션을 공유하지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

from .db import connect
from .report_writer import update_brief

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)

# 한 주기에 처리할 최대 건수 — 재생성은 LLM 호출이라 건당 수십 초다.
# 상한이 없으면 버튼이 여러 번 눌린 뒤 한 주기가 수 분간 잡혀 종료 신호에 늦게 반응한다.
_MAX_PER_TICK = 3

_SELECT_PENDING = """
SELECT report_id, incident_id, COALESCE(brief_version, 1)
  FROM agent_reports
 WHERE needs_reanalysis IS TRUE AND report_id LIKE 'SUP-%%'
 ORDER BY id ASC
 LIMIT %s
"""

# 재생성 재료 = 그 Incident 에서 **에이전트 게이트를 통과할 수 있는 가장 강한** alert 원문.
#
# 🔴 2026-08-10 정정 — 구 정렬(`alert_ts DESC` 만)은 [재분석] 버튼을 **구조적으로 무동작**
#    으로 만들었다. 실측: 버튼을 눌러도 30분간 `needs_reanalysis=t` 가 안 내려가고
#    `brief_version` 은 NULL 에 머물렀다.
#      · 파이프라인은 `spc.context_score_agent_min`(=31) 미달 알람에 **에이전트를 돌리지
#        않는다** (incident_grouper: "[context<31 기록만·agent 미가동]").
#      · 그런데 한 Incident 의 알람은 대다수가 저점수다 — 8/10 실측 CH_4 꼬리 ctx 6~22.
#      · 최신 1건을 고르면 거의 항상 저점수 → `handle_alert_with_brief` 가 brief=None →
#        "재분석 Brief 생성 실패(게이트 차단)" → 플래그 미해제 → **15초마다 영구 재시도**.
#    "최신"과 "게이트 통과"가 양립하지 않았다. 최초 Brief 는 ctx>=31 알람으로 생성됐으니
#    재생성도 **비교 가능한 표본**을 써야 한다 — 그래서 context_score 우선으로 뒤집는다.
#
# 부수 효과가 본래 목적과 맞는다: context_score 는 위반 폭·강도의 집계축이라(계약 §3) 이
#    정렬은 **그 Incident 를 대표하는 알람**을 고른다. 8/10 에 관측된 "산문이 극단 꼬리
#    (C17 상방 255.0)를 인용하고 근거 카드(하방 이탈 다수)와 어긋나는" 문제도 같은 뿌리였다 —
#    표본이 대표성이 없으면 서술도 대표성이 없다.
#
# `alert_ts DESC` 는 동점 처리로 남긴다 — 같은 점수면 "지금에 가까운 쪽"이 여전히 맞다.
_SELECT_ALERT = """
SELECT raw, COALESCE(context_score, 0) AS ctx, alert_id FROM incident_alerts
 WHERE incident_id = %s AND raw IS NOT NULL
 ORDER BY COALESCE(context_score, 0) DESC, alert_ts DESC, id DESC
 LIMIT 1
"""


def fetch_pending(conn, limit: int = _MAX_PER_TICK) -> list[tuple[str, str, int]]:
    """재분석 요청 목록 — (report_id, incident_id, 현재 brief_version)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_PENDING, (limit,))
        return [(r[0], r[1], int(r[2])) for r in cur.fetchall()]


def fetch_alert_payload(conn, incident_id: str) -> Optional[dict[str, Any]]:
    """재생성 재료 — `incident_alerts.raw` 중 **context_score 최고** alert 원문. 없으면 None.

    고른 표본을 로그에 남긴다 — 게이트 차단으로 재생성이 실패했을 때 "어떤 알람을 썼나"가
    없으면 원인 추적이 불가능했다(2026-08-10 무동작 진단에서 이게 없어 30분을 썼다).
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_ALERT, (incident_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        logger.warning("재분석 재료 없음 — incident_alerts.raw 부재: incident=%s", incident_id)
        return None
    logger.info("재분석 표본 선택: incident=%s alert=%s context_score=%s "
                "(게이트 기준 spc.context_score_agent_min 이상이어야 에이전트가 돈다)",
                incident_id, row[2], row[1])
    raw = row[0]
    # jsonb 는 드라이버가 dict 로 준다. 문자열로 오는 구성(구 드라이버)도 방어한다 —
    # 파싱 성공이 곧 dict 를 뜻하지 않으므로 타입까지 확인한다 (헌법 §7).
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


async def regenerate_one(report_id: str, incident_id: str, prev_version: int,
                         settings: "Settings") -> Optional[int]:
    """한 건 재생성 → 같은 행 UPDATE. 성공 시 새 brief_version, 실패 시 None."""
    from .pipeline import handle_alert_with_brief
    from .schemas.alert import AlertModel

    with connect() as conn:
        payload = fetch_alert_payload(conn, incident_id)
    if payload is None:
        logger.warning("재분석 재료 없음 — incident_alerts.raw 부재: incident=%s report=%s",
                       incident_id, report_id)
        return None
    try:
        alert = AlertModel.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — 손상 payload 로 루프가 죽지 않게 (6-2)
        logger.warning("재분석 alert 파싱 실패 — skip: incident=%s: %s", incident_id, exc)
        return None

    # [AI 분석 요청] 수동 첫 분석 판별 (2026-08-12) — 게이트웨이 /incidents/{id}/analyze 가
    # 심은 **스텁 행**은 supervisor_recommendation 이 NULL 이다. 그 경우 이 실행은 "재"분석이
    # 아니라 **사람이 당긴 첫 분석**이므로 게이트(B7)를 우회한다. 자동 경로는 불변.
    prev_selected = None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT supervisor_recommendation FROM agent_reports WHERE report_id=%s",
                        (report_id,))
            got = cur.fetchone()
            prev_selected = got[0] if got else None
    manual_first = prev_selected is None
    if manual_first:
        logger.info("수동 첫 분석(게이트 우회): incident=%s report=%s", incident_id, report_id)

    reports, brief = await handle_alert_with_brief(alert, settings, incident_id=incident_id,
                                                   gate_override=manual_first)
    if brief is None:
        logger.warning("재분석 Brief 생성 실패(게이트 차단 또는 fallback) — incident=%s", incident_id)
        return None

    # ⚠️ report_id 를 **기존 값으로 되돌린다** — 재생성은 alert 에서 새 report_id 를 파생시키지만
    #    우리는 같은 행을 덮어써야 한다(PR #70 결정 4). 이 한 줄이 "새 행이 안 생기는" 근거다.
    brief = brief.model_copy(update={"report_id": report_id})

    new_selected = brief.supervisor_recommendation.selected
    if prev_selected is not None and prev_selected != new_selected:
        # 미결 D10 — 판단 변경 이력이 스키마에 남지 않는다. 최소한 로그로는 보이게 한다.
        logger.warning("재분석으로 판정이 바뀜: report=%s v%d→v%d  %s → %s "
                       "(이력은 저장되지 않는다 — 미결 D10)",
                       report_id, prev_version, prev_version + 1, prev_selected, new_selected)
    return update_brief(brief, reports)


def _loop(settings: "Settings", interval_sec: float, stop: threading.Event) -> None:
    """폴링 루프 — 데몬 스레드에서 돈다. 예외가 나도 루프를 죽이지 않는다(6-2)."""
    import asyncio

    logger.info("재분석 폴러 시작 — %.0f초 주기", interval_sec)
    while not stop.is_set():
        try:
            with connect() as conn:
                pending = fetch_pending(conn)
            for report_id, incident_id, ver in pending:
                if stop.is_set():
                    break
                logger.info("재분석 요청 처리: report=%s incident=%s (현재 v%d)",
                            report_id, incident_id, ver)
                asyncio.run(regenerate_one(report_id, incident_id, ver, settings))
        except Exception as exc:  # noqa: BLE001 — 폴러가 죽으면 버튼이 조용히 무응답이 된다
            logger.warning("재분석 폴링 주기 실패(다음 주기에 재시도): %s", exc)
        stop.wait(interval_sec)
    logger.info("재분석 폴러 종료")


def start_poller(settings: "Settings") -> Optional[threading.Event]:
    """폴러를 데몬 스레드로 띄운다. 주기 미설정이면 **띄우지 않고** None (조용한 기본값 금지).

    Returns: 종료용 Event (호출측이 shutdown 때 set). 비활성이면 None.
    """
    try:
        interval = float(settings.require("agent.reanalysis_poll_sec"))
    except Exception:  # noqa: BLE001 — 키 부재는 "기능 끔"이지 크래시가 아니다
        logger.warning("재분석 폴러 비활성 — params agent.reanalysis_poll_sec 미설정. "
                       "[재분석] 버튼을 눌러도 재생성되지 않는다(조용한 무응답 방지 안내)")
        return None
    stop = threading.Event()
    threading.Thread(target=_loop, args=(settings, interval, stop),
                     daemon=True, name="reanalysis-poller").start()
    return stop
