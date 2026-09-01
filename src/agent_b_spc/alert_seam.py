"""B4-1 M4 — alert seam: consumer 6-튜플 콜백 → 조인·enrichment·점수·crazy·발행 closure.

consumer 가 판정한 결과(6-튜플)를 받아 라이브 알람 파이프라인을 구동하는 closure 를 만든다.
consumer 컨텍스트(`get_phase`·`prediction_cache`)가 필요해 `agent_b_spc/` 에 둔다 — common 은
consumer 를 모른다(`__init__` 경계·의존 방향 agent_b_spc → common). 스위치 off 면 조립·적재만
하고 발행은 skip(D-11) 이라 M4 배선은 라이브 무영향(dry-run 기본).

배선(계획서 §3):
  1) build_joined(6-튜플 + prediction_cache)
  2) join_counter.record(joined) + N건당 요약(P2 — 리허설 계측)
  3) enrichment(D-7): pred 있으면 `phase`·`p95_threshold` 주입(collector·orchestrator 순수 유지)
  4) score = int(round(context_score(joined, cs_cfg)))   ← int 변환 단일 지점(D-5)
  5) verdict = is_crazy(pred, ...)                        ← predicted OR anomaly arm
  6) publisher.process(joined, score, verdict, deps)      ← 억제→채번→적재→조립→발행
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from src.common.context_score.collector import JoinCounter, build_joined
from src.common.context_score.context_score import context_score
from src.common.context_score.publisher import (
    CrazyConfig, PublisherConfig, PublisherCounters, PublisherDeps,
    YThresholdCache, is_crazy, process,
)

logger = logging.getLogger(__name__)

_JOIN_LOG_EVERY = 500       # 예측 조인 hit/miss 요약 주기(wafer) — 리허설 계측(P2·2-1)
# TSR-0005 이관 ① — Phase 0 억제 누적 요약 주기(wafer). 조인 카운터와 동형.
#   왜 필요한가: 억제 skip 은 DEBUG 로그뿐이고 `counters.suppressed` 는 인메모리라 밖에서
#   안 보인다. 그래서 28시간 동안 알람 0건인데 판정·tttm·조인 카운터는 정상 INFO 를 내
#   "돌고 있음"으로 읽혔다(실측 2026-08-07~09). **첫 억제는 즉시**, 이후 N건마다 누적을 낸다 —
#   시작을 놓치면 "언제부터 조용했나"를 사후에 복원할 수 없기 때문이다.
_SUPPRESS_LOG_EVERY = 500


def _note_suppressed(chamber_id: str, counters: PublisherCounters) -> None:
    """Phase 0 억제를 INFO 로 노출 (TSR-0005 이관 ①).

    **첫 억제는 즉시** — Phase 0 진입 자체가 무로그 전이라, 이 한 줄이 "언제부터 조용해졌나"의
    유일한 시각 기록이다. 이후는 `_SUPPRESS_LOG_EVERY` 마다 전 챔버 누적을 낸다(wafer 마다
    찍으면 스팸 — 6-1).

    메시지 앞 ASCII 토큰(`phase0_suppress`)은 의도다 — 한글만 있으면 콘솔 인코딩이 깨진
    파이프에서 grep 이 조용히 0건을 돌려준다(헌법 7장 · #133 선례).
    """
    per_chamber = counters.suppressed.get(chamber_id, 0)
    if per_chamber == 1:                                 # 이 챔버의 첫 억제 — 즉시
        logger.info("phase0_suppress 시작 — chamber=%s (Phase 0: 적재·발행 skip). "
                    "Qual 확정(S7) 전까지 이 챔버는 알람을 내지 않는다", chamber_id)
        return
    total = sum(counters.suppressed.values())
    if total % _SUPPRESS_LOG_EVERY == 0:
        logger.info("phase0_suppress 누적 %d건 — 챔버별 %s. 억제 해제는 Qual 확정 하류만"
                    "(헌법 1-1) — 대기 중이면 승인 필요", total, dict(counters.suppressed))


def make_alert_collector(consumer, engine, cs_cfg, crazy_cfg: CrazyConfig,
                         y_cache: YThresholdCache, *,
                         producer=None, pub_cfg: Optional[PublisherConfig] = None,
                         sensor_map: Optional[dict] = None,
                         join_counter: Optional[JoinCounter] = None,
                         counters: Optional[PublisherCounters] = None,
                         next_id: Optional[Callable] = None) -> Callable:
    """`consumer.collector` 로 꽂을 closure 생성 (M4).

    Args:
        consumer: `get_phase`·`prediction_cache` 제공(SpcConsumer).
        engine: publisher 적재용 SQLAlchemy Engine.
        cs_cfg: ContextScoreConfig(orchestrator).
        crazy_cfg: CrazyConfig(B9 판정).
        y_cache: YThresholdCache(P99 조회 — crazy predicted arm·enrichment).
        producer: Kafka Producer(None 이면 발행 skip).
        pub_cfg: PublisherConfig(발행 스위치). None 이면 발행 off(fail-safe·D-11).
        sensor_map: 표시명 룩업(표시층 전용).
        join_counter/counters: 관측 누적(미주입=새로 만듦). 리허설 계측용으로 반환 closure 속성에 노출.
        next_id: 채번자 주입(테스트 백엔드 대체). None 이면 운영 `next_alert_id`.

    Returns:
        `collect(payload)` — payload = (wafer_id, chamber_id, ts, violations, tttm_obj, recipe_id).
        `collect.counters`·`collect.join_counter` 로 누적 관측 노출.
    """
    join_counter = join_counter if join_counter is not None else JoinCounter()
    counters = counters if counters is not None else PublisherCounters()
    deps = PublisherDeps(engine=engine, producer=producer, pub_cfg=pub_cfg,
                         sensor_map=sensor_map if sensor_map is not None else {},
                         get_phase=consumer.get_phase, next_id=next_id)

    def collect(payload) -> None:
        wafer_id, chamber_id, ts, violations, tttm_obj, recipe_id = payload
        joined = build_joined(wafer_id, chamber_id, ts, violations, tttm_obj,
                              consumer.prediction_cache, recipe_id=recipe_id)

        join_counter.record(joined)                      # P2 — 예측 조인 hit/miss 계측
        if join_counter.total % _JOIN_LOG_EVERY == 0:
            logger.info("예측 조인 %d건: hit %d / miss %d (miss율 %.1f%%)",
                        join_counter.total, join_counter.hits, join_counter.misses,
                        join_counter.miss_rate * 100)

        pred = joined.get("prediction")
        if pred is not None:                             # enrichment(D-7) — pred None 이면 skip
            pred["phase"] = consumer.get_phase(chamber_id)
            # score_pred 는 P95(예측분포 상단)를 기대 → y_cache.p95() 공급(crazy 는 p99, 같은 행·1회 조회).
            #   pred 축은 v0-off(pred_weight=null → score_pred 0)라 현재 무영향이나, Step6 활성 시 방향 정합.
            pred["p95_threshold"] = y_cache.p95(chamber_id, recipe_id)

        # Q3 **확정: skip 유지** (2026-07-28, B). Qual wafer 는 챔버 개방(PM) 직후 5장
        #   (`qual.wafer_count`) 발행되고, 새 레짐의 첫 wafer 를 리샘플한 것이라 관리선을 벗어나는
        #   게 정상이다 → 알람을 내면 PM 마다 오탐 5장.
        #   감시 공백 아님: Qual 합불은 `qual_engine` 이 `qual.pass_*` 기준으로 **별도 판정**한다.
        #   적재·발행 둘 다 skip 이라 Phase 0 억제(D-8)와 규칙이 같다(예외 하나로 통일).
        #   ※ 시뮬레이터의 "Qual 전용 코드 경로 없음" 원칙과 상충하지 않는다 — 그건 발행·소비
        #     **파이프라인 구조** 얘기이고, 여기는 알람을 낼지 마는지의 **정책 분기**다.
        if joined.get("is_qual"):
            counters.qual_skipped += 1
            logger.debug("is_qual wafer — 적재·발행 skip: wafer=%s chamber=%s", wafer_id, chamber_id)
            return

        score = int(round(context_score(joined, cs_cfg)))   # int 변환 단일 지점(D-5)
        verdict = is_crazy(pred, chamber_id, recipe_id, crazy_cfg, y_cache)
        before = counters.suppressed.get(chamber_id, 0)  # TSR-0005 ① — 억제 발생 감지용
        process(joined, score, verdict, deps, counters)
        if counters.suppressed.get(chamber_id, 0) > before:
            _note_suppressed(chamber_id, counters)

    collect.counters = counters                          # 관측 노출(테스트·리허설 계측)
    collect.join_counter = join_counter
    return collect
