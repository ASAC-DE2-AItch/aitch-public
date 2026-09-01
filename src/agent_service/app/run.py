"""Agent Service 엔트리포인트 — alert → 리포트 3종 → Supervisor Brief → agent_reports.

    python -m agent_service.app.run

스위치(app/config.py):
- AGENT_SOURCE_MODE=replay(기본) → fixtures 30건 재생 (DB·Kafka 없이 파이프라인 확인).
                     =kafka       → fdc.alert 실구독 → 그루퍼 → 파이프라인 → agent_reports.
- AGENT_API_MODE / AGENT_LLM_MODE : mock/real · ollama/api (RUN_MODES.md).

kafka 라이브 연결 구조 (RUN_MODES.md §4 · 헌법 1-2·1-4):
    fdc.alert ─(우리 consumer)→ 그루퍼.handle_alert ─(incident_id)→ 파이프라인 ─(Brief)→
      agent_reports 적재 ─→ (이후 PM 승인그래프 merge_reports 경로③이 DB에서 자동 수합)
    낱개 alert 을 그루퍼가 Incident 로 묶고(1-4), 우리는 그 incident_id 로 Brief 를 만들어
    DB 에 남긴다. 그래프를 직접 호출하지 않는다 — 연결은 agent_reports 테이블 경유.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import Settings, SourceMode, load_settings
from .pipeline import handle_alert, handle_alert_with_brief, kb_empty_counts
from .reanalysis import start_poller as start_reanalysis_poller
from .release_brief import start_poller as start_release_brief_poller
from .report_writer import save_brief
from .source import iter_replay_alerts

logger = logging.getLogger(__name__)


async def run_replay(settings: Settings) -> dict[str, int]:
    """fixtures 를 재생하며 각 alert 를 파이프라인에 통과. 집계 요약을 반환."""
    parsed = gate_open = reports_total = 0
    for alert in iter_replay_alerts():
        parsed += 1
        reports = await handle_alert(alert, settings)
        if reports:
            gate_open += 1
            reports_total += len(reports)
    summary = {"parsed": parsed, "gate_open": gate_open, "reports": reports_total}
    logger.info("replay done: parsed=%d, gate_open=%d, reports=%d",
                parsed, gate_open, reports_total)
    return summary


# ---- kafka 라이브 -----------------------------------------------------------
def _build_grouper():
    """PM 그루퍼(orchestrator)를 라이브러리로 인스턴스화 — main() 셋업 재사용.

    orchestrator 를 수정하지 않고 **호출만** 한다(헌법 3-1 — 소유는 PM, 사용은 자유).
    psycopg2 연결·params 는 그루퍼 규약을 그대로 따른다(main() 과 동일).
    """
    import os
    import sys
    from pathlib import Path

    import psycopg2

    # orchestrator 는 src/ 아래 형제 패키지다. `python -m src.agent_service.app.run` 로 돌리면
    # src/ 가 path 에 없어 `orchestrator` 를 못 찾는다 → src/ 를 넣어준다(테스트 conftest 와 동일).
    src_dir = str(Path(__file__).resolve().parents[2])   # .../src (app→agent_service→src)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from orchestrator.incident_grouper import IncidentGrouper, load_params

    params = load_params()
    inc = params.get("incident", {}) or {}
    merge_gap = inc.get("merge_gap_sec")
    if merge_gap is None:
        raise ValueError("params.incident.merge_gap_sec 미설정 (헌법 6-1 조용한 기본값 금지)")
    initial_lifecycle = inc.get("initial_lifecycle", "open")
    agent_min = (params.get("spc", {}) or {}).get("context_score_agent_min", 31)

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL 환경변수 필요 (그루퍼 DB 적재용)")
    conn = psycopg2.connect(dsn)
    return IncidentGrouper(conn, float(merge_gap), initial_lifecycle, int(agent_min)), conn


def _resolve_incident(gconn, alert_id: str):
    """중복 스킵된 alert 의 incident_id 역조회 (2026-07-30 — PM 대행, C 리뷰 필수).

    standalone 그루퍼 서비스와 **공존**할 때, 새 알람은 걔(LLM 없음·ms 단위)가 항상 먼저
    편입해 우리 내장 그루퍼는 alert_id 중복 → None 을 받는다. 그대로 skip 하면 실시간
    알람의 Brief 가 영영 만들어지지 않는다(리허설 실측). 멤버십 정본(incident_alerts)에서
    소속 Incident 를 되찾아 파이프라인을 잇는다.

    조회 실패는 삼킨다 — 이 SELECT 는 **보조 경로**인데 예외가 나가면 호출부의 except 가
    alert 처리 전체를 failed 로 잡는다(2026-07-31 CI 실측). DB 가 잠깐 흔들렸다고 Brief
    생성이 통째로 죽는 것은 6-2 무중단과 어긋난다.
    """
    try:
        with gconn.cursor() as cur:
            cur.execute("SELECT incident_id FROM incident_alerts WHERE alert_id=%s", (alert_id,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:                     # noqa: BLE001 — 조회 실패가 alert 을 죽이면 안 된다
        logger.warning("incident 역조회 실패 — 이 alert 은 skip: %s: %s", alert_id, exc)
        return None


def _incident_has_brief(gconn, incident_id: str) -> bool:
    """Incident 당 Brief 1회 게이트 (헌법 1-4 취지 — 알람 직렬 405s 대응 v1).

    같은 Incident 의 후속 알람마다 LLM 을 다시 돌리면 N−1 건이 버려지는 작업이 된다
    (fetch_bundle 은 최신 1행만 소비 — C 실측 #70). 이미 Brief 가 있으면 skip 하고
    알람은 그루퍼 멤버십으로만 누적한다. 재분석은 수동 경로(analyze)가 담당.
    """
    try:
        with gconn.cursor() as cur:
            cur.execute("SELECT 1 FROM agent_reports WHERE incident_id=%s AND report_id LIKE 'SUP-%%' LIMIT 1", (incident_id,))
            return cur.fetchone() is not None
    except Exception as exc:                     # noqa: BLE001
        # 조회 실패는 **False(없음)** 로 본다 — 보수적 방향이 어느 쪽인지의 문제다.
        #   True 로 보면 Brief 를 안 만들고 그 Incident 는 근거 없이 승인 큐에 올라간다(정보 유실).
        #   False 로 보면 최악이 중복 생성인데, save_brief 의 ON CONFLICT(report_id) DO NOTHING 이
        #   중복 적재를 막는다. 유실보다 중복이 싸다.
        logger.warning("Brief 존재 확인 실패 — 없음으로 간주하고 진행: %s: %s", incident_id, exc)
        return False


async def run_kafka(settings: Settings) -> dict[str, int]:
    """fdc.alert 실구독 → 그루퍼 → 파이프라인 → agent_reports. graceful shutdown 지원."""
    from kafka import TopicPartition
    from kafka.structs import OffsetAndMetadata

    from .source import iter_kafka_alerts, make_kafka_consumer

    stop = {"flag": False}

    def _on_signal(signum, _frame):
        # 두 번째 시그널 = 즉시 종료. 첫 신호는 "현재 alert 처리 후 종료"인데 LLM slow-path 는
        # 수십 초가 걸려, 탈출구가 없으면 Ctrl+C 를 눌러도 멈추지 않는 것처럼 보인다.
        if stop["flag"]:
            logger.warning("시그널 %s 재수신 — 즉시 종료(현재 alert 중단)", signum)
            raise KeyboardInterrupt
        stop["flag"] = True
        logger.info("시그널 %s 수신 — graceful shutdown (한 번 더 누르면 즉시 종료)", signum)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    grouper, gconn = _build_grouper()
    consumer = make_kafka_consumer()
    # 카운터를 4종으로 분리한다 — consumed 하나에 섞으면 "10건 소비"가 정상 10건인지
    # 손상 3건 포함인지 구분되지 않아 지표로 못 쓴다.
    #   consumed : 파싱 성공(정상 alert)   · skipped : 손상·계약위반(스킵)
    #   grouped  : 그루퍼가 Incident 로 받음 · failed : 처리 중 예외
    consumed = skipped = grouped = briefs = failed = dedup = 0

    # [재분석] 버튼 소비 폴러 (PM-4) — consumer 루프와 **독립 스레드**로 돈다.
    #   여기 끼우지 않는 이유: poll 이 최대 1초 블로킹이고, 알람이 없는 구간에는 루프
    #   본문이 아예 안 돌아 버튼을 눌러도 한참 반응이 없다.
    reanalysis_stop = start_reanalysis_poller(settings)

    # RTD 해제 근거 Brief 폴러 (인프라 스텝 3 · #140) — 재분석 폴러와 같은 형태·같은 이유로
    # 별도 스레드다. 입력이 **알람이 아니라 챔버 정지 상태**라 consumer 루프와 축이 다르다:
    # 정지 중인 챔버는 wafer 가 0장이라 알람도 안 오고, 그래서 이 루프 안에 끼우면 정작
    # 정지됐을 때 아무 일도 안 일어난다.
    release_stop = start_release_brief_poller(settings)

    try:
        # poll 대기는 params D25 — 짧으면 CPU 회전, 길면 SIGTERM 반응이 그만큼 늦다(6-2).
        poll_ms = int(settings.require("agent.kafka_poll_timeout_ms"))
        for alert, record in iter_kafka_alerts(
            consumer, poll_timeout_ms=poll_ms, should_stop=lambda: stop["flag"]
        ):
            if alert is None:
                skipped += 1          # 손상 메시지 — 이미 source 에서 로그됨(6-2)
            else:
                consumed += 1
                try:
                    # 그루퍼로 Incident 묶기 — 중복이면 None (idempotent). 사건 단위 처리(1-4).
                    incident_id = grouper.handle_alert(alert.model_dump(mode="json"))
                    if incident_id is None:
                        # standalone 그루퍼가 먼저 편입한 경우 — 정본에서 역조회 (경합 해소)
                        incident_id = _resolve_incident(gconn, alert.alert_id)
                    if incident_id is not None:
                        grouped += 1
                        if _incident_has_brief(gconn, incident_id):
                            dedup += 1            # Incident 당 1회 — 알람은 멤버십으로만 누적
                        else:
                            # reports(상세 3종)를 버리지 않고 함께 넘긴다 — Brief 의 옵션 블록은
                            # 요약이라 수치가 없어서, 이것 없이는 승인 payload 가 전량 null 로
                            # 나간다 (2026-08-03 PM 제보 · report_writer._QUANT_FIELDS).
                            reports, brief = await handle_alert_with_brief(
                                alert, settings, incident_id=incident_id)
                            if brief is not None and save_brief(brief, reports):
                                briefs += 1
                except Exception as exc:  # noqa: BLE001 — 파이프라인 생존 우선(6-2)
                    failed += 1
                    # ⚠️ 아래에서 오프셋은 전진한다 — 즉 이 alert 은 **재시도되지 않는다.**
                    #   일시 장애(Postgres 재시작·LLM 5xx)여도 같다. 커밋을 보류하면 영구 장애에서
                    #   무한 재시도 루프가 되므로 전진을 택했고, 대신 **조용히 넘어가지 않게**
                    #   failed 카운터로 세어 종료 요약에 남긴다.
                    #   복구 경로: 그루퍼가 먼저 실행돼 incident 행은 남으므로
                    #   POST /incidents/{id}/analyze (trigger_type='manual') 로 재분석 가능.
                    logger.exception(
                        "alert 처리 실패 — 오프셋은 전진(재시도 없음). "
                        "incident 행은 남으므로 수동 재분석으로 복구: alert=%s: %s",
                        alert.alert_id, exc,
                    )
                finally:
                    # 세 갈래(정상 / 역조회 None / 예외) 전부 tx 를 닫는다 (R6 동형).
                    #   SELECT 만 하고 열어둔 tx 가 남으면 idle-in-transaction 으로 잠금이 쌓인다.
                    try:
                        gconn.commit()
                    except Exception as exc:  # noqa: BLE001 - Brief 는 이미 저장됐다
                        logger.warning("그루퍼 커넥션 commit 실패(무시): %s", exc)
            # 처리 완료(성공/스킵/실패) 후 오프셋 전진 (6-2·1-2).
            # kafka-python: 다음에 읽을 오프셋 = 현재+1 을 커밋한다.
            # 커밋 실패(그룹 리밸런스로 쫓겨남 등)는 삼킨다 — 다음 poll 에서 재조인하고,
            # 미커밋분은 재전달돼 그루퍼가 idempotent 스킵한다(무중단, 6-2).
            tp = TopicPartition(record.topic, record.partition)
            try:
                consumer.commit({tp: OffsetAndMetadata(record.offset + 1, None, -1)})
            except Exception as exc:  # noqa: BLE001 — 커밋 실패로 루프가 죽지 않게
                logger.warning("오프셋 커밋 실패(다음 poll 재조인·재전달 흡수): %s", exc)
    finally:
        # 폴러를 먼저 세운다 — 데몬이라 프로세스는 어차피 죽지만, 재생성 도중 커넥션이
        # 닫히면 "종료 중 예외"가 로그를 어지럽힌다. 종료 신호는 다음 주기에 반영된다.
        if reanalysis_stop is not None:
            reanalysis_stop.set()
        if release_stop is not None:
            release_stop.set()
        consumer.close()
        gconn.close()
    summary = {"consumed": consumed, "skipped": skipped, "grouped": grouped,
               "briefs": briefs, "failed": failed, "dedup": dedup}
    # failed>0 은 Brief 없는 incident 가 남았다는 뜻 — INFO 에 묻히지 않게 따로 경고한다.
    log = logger.warning if failed else logger.info
    log("kafka done: consumed=%d, skipped=%d, grouped=%d, briefs=%d, dedup=%d, failed=%d",
        consumed, skipped, grouped, briefs, dedup, failed)
    if failed:
        logger.warning(
            "⚠️ 처리 실패 %d 건 — 해당 incident 는 Brief 가 없다. "
            "POST /incidents/{id}/analyze 로 재분석 필요.", failed,
        )
    # 재료 0장은 **실패가 아니다** — Brief 는 정상 생성되고 화면에도 뜬다. 그래서 위 failed
    # 카운터로는 안 잡히고, 로그를 안 보면 "근거 3층이 빈 Brief"가 조용히 나간다 (W16).
    # 대표 원인: Qdrant 미기동 · KB 미적재 · 임베딩(torch) 차단(TSR-0001 — 파이프 자식 실행).
    kb_empty = kb_empty_counts()
    if kb_empty:
        # summary 값은 int 로 통일한다(소비처가 카운터로 읽음) — 컬렉션별 내역은 로그에.
        summary["kb_empty"] = sum(kb_empty.values())
        logger.warning(
            "⚠️ 재료 0장 %d건 (컬렉션별 %s) — 리포트는 나왔지만 그 재료가 비었다. "
            "Qdrant 기동·KB 적재·임베딩 로드를 확인할 것.",
            sum(kb_empty.values()), kb_empty,
        )
        # knob_map 이 비면 recipe 손잡이 가드가 **비활성**된다(오탐 방지 설계). 근거가 얕아지는
        # 것과 달리 **검사가 꺼지는** 것이라 따로 눈에 띄게 남긴다.
        if kb_empty.get("knob_map"):
            logger.warning(
                "⚠️ knob_map 재료 0장 %d건 — `enforce_knob_guard` 가 그 동안 **비활성**이었다"
                "(허용 손잡이 화이트리스트 검사 없음). Qdrant tuning_axis 카드 적재를 확인할 것.",
                kb_empty["knob_map"],
            )
    return summary


def main() -> None:
    """엔트리포인트 — 스위치를 읽어 소스를 고르고 파이프라인을 돌린다."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    settings = load_settings()
    logger.info("start: source=%s api=%s llm=%s",
                settings.source_mode.value, settings.api_mode.value, settings.llm_mode.value)

    if settings.source_mode is SourceMode.KAFKA:
        asyncio.run(run_kafka(settings))
    else:
        asyncio.run(run_replay(settings))


if __name__ == "__main__":
    main()
