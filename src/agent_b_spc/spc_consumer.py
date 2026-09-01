"""fdc.raw 컨슈머 — 라이브 스트림을 Nelson·TTTM 엔진에 배선한다 (D0-2, B쪽).

Nelson(B3-1)·TTTM(B4-2)은 주입식 순수 엔진이라 스스로 데이터를 받지 않는다. 이 모듈이
`fdc.raw` 3초 샘플을 구독해 **웨이퍼 단위로 버퍼링 → 요약 집계 → Point → 두 엔진 팬아웃**
한다. 어려운 건 Kafka가 아니라 집계다 — 배치(initial_limits)와 산식이 어긋나면 관리선과
사과-오렌지 비교가 되어 판정이 붕괴하므로, 브리지가 배치 함수를 그대로 재사용한다.

범위 경계(D0-2 결정 1): 위반을 **만들기만** 한다. `context_score` 계산과 `fdc.alert`
발행은 B4-1, `fdc.correction` 구독(리로드 트리거)은 B5-1a로 이월한다.

confluent_kafka import는 지연(lazy)이다 — 순수 로직 테스트가 Kafka 미설치 환경에서도
돌아야 하므로 실제 컨슈머를 만드는 `make_consumer()` 안에서만 import한다.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from sqlalchemy import text

from src.agent_b_spc import control_limits_loader
from src.agent_b_spc.approval_apply import apply_approved
from src.common.config_util import _key, _section
from src.agent_b_spc.establish_engine import (
    EstablishConfig,
    WaferSample,
    compute_y_threshold,
    establish_group,
    rebase_snapshot,
    select_establishment_sample,
    write_y_threshold,
)
from src.agent_b_spc.correction_history import reset_ref
from src.agent_b_spc.nelson_engine import NelsonEngine, Point
from src.agent_b_spc.periodic_recalc import (
    build_baseline_map, resolution_for, stagger_threshold)
from src.agent_b_spc.provisional_mode import Phase
from src.agent_b_spc.recalc_engine import RecalcConfig, recompute_group
from src.agent_b_spc.recalc_writer import apply_auto, record_proposed
from src.agent_b_spc.qual_engine import UNKNOWN as QUAL_UNKNOWN, QualConfig, _aggregate, evaluate_qual
from src.agent_b_spc.qual_snapshot_loader import load_active_snapshot
from src.agent_b_spc.qual_recorder import (
    _combine_a_indicators, _propose_verdict, insert_qual, rebase_snapshot_center,
    update_qual_confirmed)
from src.agent_b_spc.seed_qual_snapshots import gk_str, write_sensor_stats
from src.agent_b_spc.shadow_eval import ShadowConfig, VerifyConfig, evaluate_shadow
from src.agent_b_spc.tttm_engine import TTTMEngine
from src.agent_b_spc.tttm_writer import rollups_to_rows, write_comparisons
from src.common.context_score.prediction_cache import PredictionCache   # B4-1 Step3b — 예측 캐시(A)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"
TOPIC_RAW = "fdc.raw"
TOPIC_CORRECTION = "fdc.correction"            # B5-1a 라이브 — 승인된 limit 재설정 유입
TOPIC_AGENT = "fdc.agent"                      # B6-3-c — QualVerdictConfirmed·ChamberRequalified 구독
TOPIC_PREDICTION = "fdc.prediction"            # B4-1 Step3b — A 예측 구독 → 캐시 upsert
CONSUMER_GROUP = "consumer-group-spc"          # 헌법 6-4
POLL_TIMEOUT_SEC = 1.0

# H2 — 집계·Point화가 하드 요구하는 필드 (하나라도 없으면 skip)
REQUIRED_FIELDS = (
    "wafer_id", "chamber_id", "recipe_id", "step",
    "stabilization_flag", "pm_count", "timestamp", "sensors",
)


def load_idle_flush_sec(path: Path = CONFIG_PATH) -> float:
    """config `spc.wafer_idle_flush_sec`(웨이퍼 tail flush 임계, 초)를 로드한다.

    키 부재 시 매직넘버 대체 없이 명시적 실패(헌법 6-1). [잠정값] — dry-run 실측 후 확정.
    """
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    spc = _section(cfg, "spc", path)
    return float(_key(spc, "wafer_idle_flush_sec", "spc", path))


def _load_params_dict(path: Path = CONFIG_PATH) -> dict:
    """params.yaml 전체 dict — PredictionCache.from_config(cfg 요구)·purge interval 공용 (B4-1 Step3b)."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _default_offset_factory(topic: str, partition: int, offset: int, metadata=None) -> list:
    """커밋할 오프셋 스펙 — Kafka 규약상 '다음 읽을 위치'라 +1 (lazy import).

    B7-1 §6-2-9 D14 — `metadata`(챔버별 **마지막 처리 offset** JSON)를 함께 실어 커밋한다.
    이건 **커밋 offset(min un-flushed)이 아니라 처리 high-water**(가)라, 크래시 재시작 시
    커밋 이후 재전달되는 이미-처리분을 `_handle_raw`가 챔버별로 skip한다(나). confluent-kafka
    ≥2.3의 `TopicPartition.metadata` 사용(§5-3 라이브 왕복 실측 완료).
    """
    from confluent_kafka import TopicPartition

    if metadata is not None:                     # metadata는 생성자 인자(속성 대입 불가·읽기전용)
        return [TopicPartition(topic, partition, offset + 1, metadata)]
    return [TopicPartition(topic, partition, offset + 1)]


def _normalize_ts(ts: str) -> str:
    """ISO-8601 'Z' 접미를 '+00:00'으로 (C3). **str 유지** — 엔진 Point 계약이 str.

    `datetime.fromisoformat`은 Python 3.11 미만에서 'Z'를 못 읽는다. 엔진(nelson·tttm)이
    Point.timestamp를 fromisoformat으로 파싱하므로 유입 전에 여기서 정규화한다.
    """
    return ts[:-1] + "+00:00" if isinstance(ts, str) and ts.endswith("Z") else ts


def _valid(row: dict) -> bool:
    """fdc.raw row 필수 필드·timestamp 형식 검사 (H2). 실패 시 호출자가 skip+로그."""
    for f in REQUIRED_FIELDS:
        if row.get(f) is None:                 # 결측·None 동일 취급(시뮬레이터가 NaN→None)
            logger.warning("필수 필드 결측 — row skip: %s", f)
            return False
    if not row["sensors"]:                     # 빈 dict → 집계할 값 없음
        logger.warning("sensors 비어있음 — row skip")
        return False
    try:
        datetime.fromisoformat(_normalize_ts(row["timestamp"]))
    except (ValueError, TypeError):
        logger.warning("timestamp 형식 오류 — row skip: %r", row["timestamp"])
        return False
    return True


# fdc.raw 최상위 필드 → build_wafer_summaries가 요구하는 C컬럼 (§5 ①)
_TOPLEVEL_TO_C = {
    "chamber_id": "C24",          # groupby 축
    "recipe_id": "C6",            # groupby 축
    "step": "C7",                 # groupby 축
    "stabilization_flag": "C42",  # window(과도/정착)
    "wafer_id": "C64",            # groupby 축
    "timestamp": "C10",           # 정렬용 — agg("C10","min") = t0
}


def _to_spc_flags(violations: list) -> list:
    """Nelson 위반 → 계약 형태 `[{sensor, rule}]` (SHAP 순위 미개입 — 헌법 3-3)."""
    return [{"sensor": v["sensor_id"], "rule": v["rule_id"]} for v in violations]


def _summarize_rows(rows: list):
    """웨이퍼 raw rows → 배치와 동일 산식의 요약 DataFrame (§5 브리지 — 공유 헬퍼, B5-2b T1).

    `build_wafer_summaries`는 배치 산식이라 C코드 컬럼을 하드 요구한다. 라이브 `fdc.raw`는
    필드명이 다르고 파생 컬럼이 없으므로 여기서 배치와 동일한 형태로 재구성한다:
    역리네이밍+C10 주입 → `add_derived_features`(D_VDC_RES) → `build_wafer_summaries`.

    산식을 재구현하지 않고 배치 함수를 그대로 재사용하는 것이 핵심이다 — 어긋나면 관리선과
    사과-오렌지 비교가 되어 판정이 붕괴한다(결정 3). SPC(Point화)·Qual(σ-갭) 두 경로가 이
    **단일 산식**을 공유한다. rows는 비어있지 않다고 가정(호출자가 가드).
    """
    import pandas as pd

    from src.agent_b_spc.initial_limits import (
        SENSORS_SETTLED, SENSORS_TRANSIENT_ONLY,
        add_derived_features, build_wafer_summaries,
    )

    # ① 역리네이밍 + C10 주입 + 센서 펼치기 (계약 원본 불변 — 로컬 df 사본에서만, 헌법 2-1)
    recs = [{**{c: r[k] for k, c in _TOPLEVEL_TO_C.items()}, **r["sensors"]} for r in rows]
    df = pd.DataFrame(recs)

    # 결측 센서 컬럼 방어 — build_wafer_summaries의 .agg가 컬럼 전체를 하드 요구한다.
    # 하나라도 없으면 KeyError로 매 flush가 0 point 무음 실패(C1과 동종). NaN은 melt 뒤
    # dropna로 자연 탈락하므로 채워두는 편이 항상 안전하다.
    for col in SENSORS_SETTLED + SENSORS_TRANSIENT_ONLY + ["C12"]:
        if col not in df.columns:
            df[col] = float("nan")

    # ② 파생 선행 (C1) — build_wafer_summaries가 D_VDC_RES를 무조건 참조
    df = add_derived_features(df)

    # ③ 배치와 동일한 step×window×sensor 평균
    return build_wafer_summaries(df)


def _aggregate_to_points(rows: list, limits: dict) -> list:
    """웨이퍼 raw rows → 배치와 동일 산식의 요약 Point 목록 (§5 브리지·정상 SPC 핫패스).

    집계 ①②③은 `_summarize_rows`(공유 헬퍼)로 위임하고, 여기선 요약 행을 Point로 변환하며
    감시 대상(limits) 그룹만 채택한다. Qual 경로(`_group_values`)와 산식을 공유한다.
    """
    if not rows:
        return []
    from src.agent_b_spc.qual_snapshot_loader import normalize_group_key

    summaries = _summarize_rows(rows)

    # ④ 요약 행 → Point
    pm_count = rows[-1]["pm_count"]              # M2 — 웨이퍼 상수, 요약이 안 실어나름
    points = []
    for rec in summaries.to_dict("records"):
        gk = normalize_group_key(rec["C24"], rec["C6"], rec["C7"],
                                 rec["sensor_window"], rec["sensor_id"])
        lim = limits.get(gk)                     # M3 — 비화이트리스트 그룹은 생략
        if lim is None:
            continue
        points.append(Point(
            group_key=gk,
            wafer_id=rec["C64"],
            timestamp=_normalize_ts(rec["t0"]),  # C3 — str 유지(엔진 계약)
            value=float(rec["value"]),
            pm_count=pm_count,
            limit_version=lim["limit_version"],
        ))
    return points


def _group_values(rows: list) -> list:
    """Qual 경로: 웨이퍼 rows → [(gk, value)] (B5-2b §3). limits 필터 없음 — 스냅샷 교집합은 판정에서.

    `_aggregate_to_points`와 **동일 산식**(`_summarize_rows`)을 쓰되, 감시 화이트리스트로
    거르지 않고 전 그룹의 요약값을 그대로 낸다(스냅샷 그룹만 채택하는 필터는 `_judge_qual`).
    """
    from src.agent_b_spc.qual_snapshot_loader import normalize_group_key

    out = []
    for rec in _summarize_rows(rows).to_dict("records"):
        gk = normalize_group_key(rec["C24"], rec["C6"], rec["C7"],
                                 rec["sensor_window"], rec["sensor_id"])
        out.append((gk, float(rec["value"])))
    return out


# ── Step7 S6a: 무이상 게이팅 쿼리 (D2) — 순수 SQL, 표준 SQL이라 sqlite로도 검증 가능 ──────
_G3_INCIDENT_BLOCK = text(
    # G3: 미종결 incident(관측 창 오염 방지). actioned=verifying으로 넘어가는 순간 상태라 fail-safe로
    #   제외 유지, verifying/open/analyzing/pending/reopened는 차단. 블랙리스트라 새 상태는 fail-closed.
    "SELECT 1 FROM incidents WHERE chamber_id = :ch "
    "AND lifecycle NOT IN ('closed', 'actioned') LIMIT 1")

_G2_PROPOSED = text(
    # G2: 이 챔버에서 미결(PROPOSED)인 그룹 — Step7 자기 경로 중복 제안만 막는다(부속 X5).
    "SELECT recipe_id, step, sensor_window, sensor_id FROM limit_corrections "
    "WHERE chamber_id = :ch AND status = 'PROPOSED'")


def _incident_blocked(conn, chamber: str) -> bool:
    """G3 — 미종결 incident가 있으면 True(챔버 차단). 읽기 전용."""
    return conn.execute(_G3_INCIDENT_BLOCK, {"ch": chamber}).first() is not None


def _proposed_groups(conn, chamber: str) -> set:
    """G2 — 챔버의 PROPOSED 그룹키(chamber 제외 4-튜플) 집합. 읽기 전용."""
    return {(r[0], int(r[1]), r[2], r[3])
            for r in conn.execute(_G2_PROPOSED, {"ch": chamber})}


# ── D2 관측 전용: 봉쇄가 **얼마나 오래됐나** (2026-08-08 신설) ─────────────────────
#   ⚠️ 위 G2/G3 **판정 쿼리(`_incident_blocked`·`_proposed_groups`)는 건드리지 않는다.**
#   아래 둘은 경고 문구의 경과 시간을 재는 용도뿐이고 리캘리 발동 여부에 개입하지 않는다.
#   경과는 파이썬이 아니라 **SQL에서 계산**한다 — DB TIMESTAMPTZ 와 `self.clock()`(주입형·
#   테스트는 FakeClock)은 기준이 달라 파이썬에서 빼면 tz·스큐 문제가 생긴다.
#   행이 없으면 MIN() 이 NULL → scalar() 가 None (봉쇄 아님 or 그 사유는 아님).
_G3_OLDEST_OPEN_SEC = text(
    "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(created_at))) FROM incidents "
    "WHERE chamber_id = :ch AND lifecycle NOT IN ('closed', 'actioned')")

_G2_OLDEST_PROPOSED_SEC = text(
    "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(created_at))) FROM limit_corrections "
    "WHERE chamber_id = :ch AND status = 'PROPOSED'")

# TSR-0005 이관 ② — 미확정 Qual 나이. Phase 는 인메모리라 "언제부터 갇혔나"의 원천이 없지만
#   `quals.created_at` 은 DB 에 있다. 경과를 SQL 에서 계산하는 이유는 위 두 상수와 같다
#   (헌법 7장 *"경과는 상태의 출처(DB)에서"* — #137 패턴). 행이 없으면 MIN() NULL → None.
_QUAL_OLDEST_PENDING_SEC = text(
    "SELECT EXTRACT(EPOCH FROM (NOW() - MIN(created_at))), count(*) FROM quals "
    "WHERE chamber_id = :ch AND approval_status = 'PENDING'")


_RELOAD_RETRY_MIN_SEC = 30.0    # Step7 D5 — 리로드 재시도 최소 간격(매 wafer 재시도 시 DB 다운→Kafka 리밸런스)
_DEDUP_RESTORE_TIMEOUT_SEC = 5.0   # D14 — 재시작 시 committed() 메타데이터(high-water) 조회 브로커 타임아웃(초)
_DEDUP_RESTORE_RETRY_FACTOR = 2    # D14 — 복원 1회 재시도 시 timeout 배수 (PM 리뷰 지적 2)
_STALL_REWARN_SEC = 300.0          # D16 — 커밋 정체 재경고 간격(초). 영구 정체가 조용해지는 것 방지(지적 4)
_JOIN_EXPIRE_WARN_EVERY = 100      # 예측 조인 유예 **만료** 누적 N건마다 WARNING(조용한 게이트 금지·헌법 7장)
_PERIODIC_ENABLED_ENV = "SPC_PERIODIC_RECALC_ENABLED"   # Step7 §4 kill switch (기본 on)
_GATE_STALE_WARN_SEC = 3 * 86400.0   # Step7 D2 — G2/G3 장기 봉쇄 경고 임계(관측 전용·판정 미개입·튜닝 대상)
_GATE_STALE_RECHECK_SEC = 600.0   # D2 — 봉쇄 경과 DB 재조회 최소 간격(매 wafer 조회 방지·관측 전용)
_GATE_REWARN_SEC = 86400.0        # D2 — 봉쇄 경고 재알림 간격(초). 챔버당 1회면 자동 해소가 없는
                                  #   영구 봉쇄가 시간이 지나며 조용해진다(#128 지적 4 와 같은 성질).
                                  #   ⚠️ `_STALL_REWARN_SEC`(300s)보다 훨씬 긴 이유: 커밋 정체는
                                  #   **지금 적재가 유실 중**이라 분 단위지만, 봉쇄는 임계 자체가
                                  #   3일(`_GATE_STALE_WARN_SEC`)이라 5분마다 울리면 알람 피로다.
                                  #   24h 면 3일 봉쇄 동안 2~3회 뜬다. (#137 회신 · PM 확정)
_SHADOW_MIN_POINTS = 10   # Step7b D1 — shadow 채점 최소 표본(미만=None/미채점, 경계선상·실측 후 params 이관 검토)
# TSR-0005 이관 ② — 미확정 Qual 방치 경고. `_GATE_*` 3종과 같은 2단 throttle 구조.
#   임계가 봉쇄(3일)보다 훨씬 짧은 이유: 봉쇄는 관리선이 **노후**하는 것뿐이지만 Phase 0 은
#   그 챔버 알람이 **통째로 안 나간다**. 실측 사고는 28시간 무음이었다 — 6시간이면 반나절
#   안에 두 번 눈에 띈다. 자동 해제는 하지 않는다(헌법 1-1 HITL · TSR-0005 기각안).
_QUAL_PENDING_WARN_SEC = 6 * 3600.0    # 미확정 Qual 경고 임계(초)
_QUAL_PENDING_RECHECK_SEC = 600.0      # DB 재조회 최소 간격(매 wafer 조회 방지)
_QUAL_PENDING_REWARN_SEC = 6 * 3600.0  # 재알림 간격 — 영구 방치가 조용해지는 것 방지(#128 지적 4)

# B5-2b — 활성 스냅샷 id(loader는 stats만 반환·id 미노출) + Qual A 지표 read(결정5, best-effort)
_ACTIVE_SNAP_ID = text(
    "SELECT snapshot_id FROM qual_snapshots WHERE chamber_id = :ch AND is_active "
    "ORDER BY created_at DESC LIMIT 1")
_QUAL_PREDS = text(
    "SELECT wafer_id, anomaly_score, predicted_c65 FROM wafer_predictions "
    "WHERE wafer_id = ANY(:wids)")
_QUAL_THRESHOLDS = text("SELECT thresholds FROM quals WHERE qual_id = :qid")


class SpcConsumer:
    """fdc.raw 구독 → 집계 → 팬아웃 서비스. consumer는 주입(테스트는 FakeConsumer)."""

    def __init__(self, *, consumer, engine=None, limits: dict | None = None, whitelist=None,
                 nelson=None, tttm=None, collector=None, prediction_cache=None,
                 provisional=None, establish_cfg=None,
                 requalified_live: bool = False, pm_log_writer=None,
                 idle_flush_sec: float | None = None, join_grace_ms: float | None = None,
                 topic_raw: str = TOPIC_RAW,
                 topic_correction: str = TOPIC_CORRECTION, topic_agent: str = TOPIC_AGENT,
                 topic_prediction: str = TOPIC_PREDICTION,
                 clock=time.monotonic, offset_factory=_default_offset_factory) -> None:
        """Kafka consumer를 주입받는다. 실제 구동은 `make_consumer()` 산출물을 넘긴다.

        `nelson`/`tttm` 미지정 시 `limits`·`whitelist`로 실 엔진을 만든다(운영 경로).
        `collector`는 판정 결과 수집 seam — **B4-1**이 여기서 context_score 합산·
        `fdc.alert` 발행을 이어받는다(D0-2는 발행하지 않는다, 헌법 1-2).

        `idle_flush_sec` 미지정 시 config에서 로드(헌법 6-1, 호출 시점 로드). `clock`·
        `offset_factory`는 테스트가 시간·Kafka 타입 없이 돌게 하는 주입점(Nelson 패턴).
        """
        self.consumer = consumer
        self.engine = engine                    # M2 — apply_approved·loader용 (correction 경로)
        self.topic_raw = topic_raw
        self.topic_correction = topic_correction
        self.topic_agent = topic_agent
        self.topic_prediction = topic_prediction
        self.provisional = provisional          # B6-3-c — 가한계 상태머신(a). None이면 순수 D0-2
        # B6-3-f — pm_log writer **인스턴스**(seam은 `.append`만 주입되므로 별도 보관).
        #   poll 루프가 `flush_pending()`을 상시 호출해 기입 실패분을 회수한다(리뷰 M1):
        #   append 시점 재시도만으로는 다음 기회가 "다음 요란 PM"=3~4개월 뒤라 사실상 없다.
        self.pm_log_writer = pm_log_writer
        # ChamberRequalified 발행 모드(결정6): True면 이벤트가 firm 후속 구동·correction fallback OFF
        # (전 그룹 후 1회 → 조기 fleet 재참여 없음). False(기본)면 발행 전이라 correction-apply fallback.
        self._requalified_live = requalified_live
        self.establish_cfg = establish_cfg or (  # b 산출 파라미터(select·establish·rebase·Y)
            EstablishConfig.load() if provisional is not None else None)
        # 가한계 라이브 상태(결정2 ⓓ 파생·결정3 인메모리 버퍼) — chamber별
        self._prov_pm: dict[str, int] = {}      # 직전 관측 pm_count(PM↑ 감지)
        self._post_pm: dict[str, int] = {}      # PM 이후 wafer 카운터(버퍼 인덱싱·Step7 단일화)
        self._last_wafer: dict[str, str] = {}   # 재전달 dedup(a.on_wafer 정합) — 중복 카운트 방지
        self._backward: dict[str, deque] = {}   # 정착 backward 버퍼(WaferSample, provisional 챔버)
        self._recalc_buf: dict[str, deque] = {}  # Step7 D3 — 조용(NORMAL) 정기 리캘리 backward({gk:val})
        self._shadow_buf: dict[str, deque] = {}  # Step7b D3 — needs_approval 소급 채점용 Point 30장 버퍼
        self._reload_pending = False             # Step7 D5 — 커밋됐으나 리로드 실패(DB=신규/메모리=구)
        self._last_reload_try_at = 0.0           # Step7 D5 — 리로드 재시도 최소 간격 게이트(monotonic)
        self._settled_at: dict[str, int] = {}   # B6-3-e — 챔버별 표본 컷오프(Phase2Signal.settled_at)
        self._converged: dict[str, bool] = {}   # B6-3-e — σ 수렴 정착(True)/상한 폴백(False) 기록
        self._phase2_pending: set[str] = set()  # PHASE2_SIGNAL 후 제안 재시도 대상
        self._verify: dict[str, list] = {}      # firm 후 forward tally: chamber → [seen, flagged]
        # verify 합격선·창(결정3 라이브 tally) — provisional 늦은 바인딩(main·테스트) 대비 무조건 로드
        self._verify_max_pct = VerifyConfig.load().false_alarm_max_pct
        self._shadow_cfg = ShadowConfig.load()           # Step7b — 소급 채점 설정(n_wafers=shadow_eval_wafers 30)
        self._verify_n = self._shadow_cfg.n_wafers
        self.limits = limits or {}
        self.whitelist = set(whitelist) if whitelist is not None else set(self.limits)
        self.nelson = nelson if nelson is not None else NelsonEngine(self.limits, self.whitelist)
        self.tttm = tttm if tttm is not None else TTTMEngine(self.limits, self.whitelist)
        self.collector = collector
        # B4-1 Step3b — 예측 캐시 주입-or-생성(a1·a3). 미주입 시 실 캐시 from_config(cfg 요구).
        #   ⚠️ 캐시 clock=time.time(wall) — TTL은 메시지 ts(wall-clock)와 비교하므로 self.clock
        #   (monotonic) 주입 금지. purge '호출 주기'만 monotonic(아래 _last_purge_at).
        _params = _load_params_dict()
        self.prediction_cache = (prediction_cache if prediction_cache is not None
                                 else PredictionCache.from_config(_params, clock=time.time))
        self._purge_interval_sec = float(_key(_section(_params, "spc", CONFIG_PATH),
                                              "prediction_cache_purge_interval_sec", "spc", CONFIG_PATH))
        self._last_purge_at = clock()           # monotonic — purge 캐던스 기준(b1·b2). self.clock 대입 前이라 param 사용
        # 예측 조인 유예 (grace) — 0 이면 완전 비활성(현행 동작). 상세 = specs/예측조인유예_스펙플랜.md
        _spc = _section(_params, "spc", CONFIG_PATH)
        #   주입 우선(테스트가 유예를 끄고 즉시 경로만 검증할 수 있게 — idle_flush_sec 선례)
        self._join_grace_sec = float(join_grace_ms if join_grace_ms is not None
                                     else _spc.get("prediction_join_grace_ms", 0)) / 1000.0
        self._join_pending_max = int(_spc.get("prediction_join_pending_max", 500))
        self._pending_join: dict[str, deque] = defaultdict(deque)   # chamber → 발행 대기 큐
        self._join_expired_warned = 0           # 만료 경고 throttle 기준(누적 카운터 스냅샷)
        # Step7 D1·D5 — 정기 리캘리 트리거·상태. interval은 consumer 직접 로드(v2.3 P17 — RecalcConfig 불변,
        #   캐던스 키를 코어 산식에 안 섞음). purge 캐던스 로드와 동형. cfg·resolutions는 startup(main) 주입.
        self._recalc_interval = int(_key(_section(_params, "limit_engine", CONFIG_PATH),
                                         "recalc_interval_wafers", "limit_engine", CONFIG_PATH))
        self._periodic_enabled = os.environ.get(
            _PERIODIC_ENABLED_ENV, "true").strip().lower() != "false"   # kill switch(기본 on)
        self._recalc_count: dict[str, int] = {}      # 트리거 텀블링 카운터(챔버별)
        self._first_fire_done: dict[str, bool] = {}  # 스태거 첫 발동 여부(D6-c)
        self._resolutions: dict = {}                 # main 주입 — σ-단위 자 분해능(load_resolutions)
        self._recalc_cfg = None                      # main 주입 — RecalcConfig(재산정 산식)
        self._gate_blocked_since: dict[str, float] = {}  # Step7 D2 — G2/G3 최초 봉쇄 시각(**DB 조회 불가 시 폴백 전용**)
        self._gate_warned: dict[str, float] = {}     # Step7 D2 — 챔버별 마지막 봉쇄 경고 시각
                                                     #   (구 set = 1회 throttle → `_GATE_REWARN_SEC` 주기 재경고)
        self._gate_checked_at: dict[str, float] = {}  # D2 — 봉쇄 경과 DB 재조회 시각(_GATE_STALE_RECHECK_SEC)
        # TSR-0005 이관 ② — 미확정 Qual 방치 경고. `_gate_*` 와 같은 2단 throttle(재알림·재조회).
        #   `_gate_blocked_since` 같은 **메모리 폴백은 두지 않는다** — 이 경고의 존재 이유가
        #   "메모리 경과는 재시작마다 리셋된다"이므로, DB 조회 실패는 조용히 skip 이 맞다.
        self._qual_pending_warned: dict[str, float] = {}    # 챔버별 마지막 경고 시각
        self._qual_pending_checked_at: dict[str, float] = {}  # 챔버별 마지막 DB 재조회 시각
        # B5-2b — Qual 판정·적재 라이브 배선. 인메모리 수집 버퍼(chamber→{pm_count,date,wafer_ids,values})·
        #   config(신규 키 0 — 기존 qual 절 소비, 헌법 6-1). `_qual_cfg`(QualConfig)는 lazy 1회 로드.
        _qual = _section(_params, "qual", CONFIG_PATH)
        self._qual_wafer_count = int(_key(_qual, "wafer_count", "qual", CONFIG_PATH))
        self._qual_ae_max = float(_key(_qual, "pass_ae_max", "qual", CONFIG_PATH))
        self._qual_c65_max = float(_key(_qual, "pass_c65_max", "qual", CONFIG_PATH))
        self._qual_use_ae = bool(_key(_qual, "use_ae_axis", "qual", CONFIG_PATH))   # PM 회신 — AE 포화 시 잠정 제외
        self._qual_buf: dict[str, dict] = {}
        self._qual_cfg = None                   # lazy QualConfig.load() (첫 판정 시)
        self.idle_flush_sec = (idle_flush_sec if idle_flush_sec is not None
                               else load_idle_flush_sec())
        self.clock = clock
        self.offset_factory = offset_factory
        self._running = False
        self.buffers: dict[str, dict] = {}      # chamber_id → 진행 중 웨이퍼 버퍼
        # B7-1 §3-1 ③ (D16) — flush 예외 웨이퍼의 first offset 보관. 버퍼는 리셋되나 커밋 상한은
        #   이 offset을 넘지 못하게 홀드 → 재시작 시 Kafka 재전달로 회수(유실 방지). {chamber: (t,p,off)}
        self._pending_failed: dict[str, tuple] = {}
        self._commit_stall_warned: dict[str, float] = {}   # 정체 WARNING 최근 시각(챔버별 — 주기 재경고)
        self._last_committed_off = -1            # fdc.raw 커밋 단조 가드(되감기=재소비 방지)
        # B7-1 §6-2-9 (D14) durable dedup — 챔버별 마지막 처리(flush) offset을 commit metadata로
        #   싣고, 재시작 시 복원해 크래시 재전달(이미 처리분)을 _handle_raw가 챔버별 skip한다.
        self._last_processed: dict[str, int] = {}     # 커밋에 실을 처리 high-water(챔버별)
        self._restored_highwater: dict[str, int] = {} # 재시작 복원분(≤이면 크래시 dup → skip)
        self.stats = {"handled": 0, "skipped_deserialize": 0, "skipped_invalid": 0,
                      "flushed": 0, "collector_failed": 0,   # B1 — collector(오케스트레이터) 호출 실패 계측
                      # 예측 조인 유예 4종 — "유예가 일했나 / 못 잡았나"를 구분(조용한 게이트 금지)
                      "join_grace_enqueued": 0,    # 유예 큐로 들어간 wafer
                      "join_grace_resolved": 0,    # 유예 중 예측 도착 → 조인 성공
                      "join_grace_expired": 0,     # 상한 초과 → 스텁 발행(현행 동작으로 낙하)
                      "join_grace_overflow": 0,    # 큐 상한 초과 → 유예 없이 즉시 발행(메모리 가드)
                      # B7-1 §5 dedup 효과 3종 — "동작함"과 "안 남"을 구분(24h GREEN 게이트 지표).
                      "durable_dedup_skips": 0,    # D14 — 크래시 재시작 시 high-water≤ 재전달 skip 히트(dedup이 일한 증거)
                      "redelivery_dup_wafers": 0,  # D15 — 동일프로세스 재전달(중복 wafer) 감지 수(redelivery_events)
                      "flush_fail": 0,             # D16 — flush 예외 누적(§5 GREEN 0 조건 · loss=0 지표 단일 소스)
                      # D14 — 복원(committed 조회·파싱) 실패 누적. 실패해도 at-least-once로 진행하므로
                      #   로그만으로는 "크래시 dup 방어가 조용히 꺼진" 상태를 놓친다(§5 GREEN 0 조건).
                      "dedup_restore_fail": 0,
                      # 커밋 실패 누적(PM 리뷰 지적 1). 삼켜도 안전하지만(다음 성공분이 덮음)
                      #   지속되면 랙이 쌓이므로 숫자로 남긴다 — 정상 런 0.
                      "commit_fail": 0}

    # ── 수명주기 ────────────────────────────────────────────────────
    def request_stop(self, *_args) -> None:
        """루프 정지 요청 — SIGINT/SIGTERM 핸들러가 호출 (헌법 6-2)."""
        self._running = False

    def _install_signals(self) -> None:
        """graceful shutdown 시그널 등록. 메인 스레드가 아니면 조용히 건너뛴다."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):      # 비메인 스레드·미지원 플랫폼
                logger.debug("시그널 등록 skip: %s", sig)

    def run(self) -> None:
        """메인 루프 — poll → 역직렬화 → 검증 → 토픽 디스패치. 종료 시 close."""
        self._install_signals()
        self._running = True
        self.consumer.subscribe(                                            # B4-1 Step3b — 4토픽
            [self.topic_raw, self.topic_correction, self.topic_agent, self.topic_prediction])
        self._assert_single_partition()                                      # 지적 3 — 파티션 1 전제 가드
        self._restore_dedup_state()                                         # D14 — 크래시 재시작 dedup 복원
        try:
            while self._running:
                msg = self.consumer.poll(POLL_TIMEOUT_SEC)
                self._sweep_idle()               # tail 상시 점검 (결정 2)
                self._drain_pending_join()       # 예측 조인 유예 — 도착분 발행(매 poll·비활성 시 O(1))
                self._maybe_purge()              # B4-1 Step3b — 예측 캐시 만료 회수(캐던스)
                self._flush_pm_log()             # B6-3-f — pm_log 기입 실패분 회수 (빈 큐=O(1))
                if msg is None:
                    continue
                if msg.error():
                    logger.debug("Kafka 메시지 오류 skip: %s", msg.error())
                    continue
                row = self._deserialize(msg)
                if row is None:
                    continue
                if msg.topic() == self.topic_correction:   # C1 — correction은 raw _valid 우회
                    if self._handle_correction(row):       # ①(b) True=커밋(성공·skip)
                        self._commit_msg(msg)              #        False=보류(재전달 재시도)
                    continue
                if msg.topic() == self.topic_agent:        # B6-3-c — fdc.agent 이벤트
                    self._handle_agent(row)                # 멱등(phase 가드)이라 항상 커밋
                    self._commit_msg(msg)
                    continue
                if msg.topic() == self.topic_prediction:   # B4-1 Step3b — 예측 upsert (raw _valid 우회, L-3)
                    self._handle_prediction(row)           # 예외 격리라 항상 커밋
                    self._commit_msg(msg)
                    continue
                if not _valid(row):                        # raw만 raw 스키마 검증 (C1)
                    self.stats["skipped_invalid"] += 1
                    continue
                self._dispatch(msg, row)
        finally:
            self._flush_all()                    # 남은 버퍼 유실 방지 (헌법 6-2)
            # flush_all 이 유예 큐에 더 넣을 수 있으므로 **그 뒤에** 강제 드레인한다.
            #   force=True — 상한 무시하고 전부 발행. 안 하면 종료 시 유예분이 사라진다.
            try:
                self._drain_pending_join(force=True)
            except Exception:                    # noqa: BLE001 — 종료 경로 예외가 close 를 막지 않게
                logger.exception("종료 시 유예 큐 드레인 실패 — 잔여 %d건", self._pending_join_len())
            self.consumer.close()

    # ── B4-1 Step3b 예측 캐시 배선 ──────────────────────────────────
    def _handle_prediction(self, row: dict) -> None:
        """fdc.prediction(역직렬화 dict) → 예측 캐시 upsert. 예외 격리(캐시 오류가 루프 못 죽임, 6-2).

        wafer_id 결측·순서역전 등 필드 검증은 캐시 `upsert`가 담당(skip+로그). 캐시 자체 예외만
        여기서 삼킨다(nelson/tttm feed 격리와 동일 패턴).
        """
        try:
            self.prediction_cache.upsert(row)
        except Exception:                          # noqa: BLE001 — 캐시 예외 격리
            logger.exception("prediction upsert 실패 — skip")

    def _maybe_purge(self) -> None:
        """예측 캐시 만료 회수 — monotonic 캐던스(`_purge_interval_sec` 경과 시만). 예외 격리.

        `get`은 조회 시점 lazy 만료라, 미조인(stranded) 예측을 주기적으로 적극 회수한다.
        clock=monotonic(주입)이라 NTP 후행·시각 역행에도 주기가 안정적(b1·b2).
        """
        if self.clock() - self._last_purge_at < self._purge_interval_sec:
            return
        try:
            self.prediction_cache.purge_expired()
        except Exception:                          # noqa: BLE001
            logger.exception("prediction 캐시 purge 실패")
        self._last_purge_at = self.clock()

    def _flush_pm_log(self) -> None:
        """pm_log 기입 실패분 재시도 (B6-3-f, poll마다). 미주입·빈 큐면 즉시 반환.

        writer는 실패 엔트리를 `pending`에 보관하는데, 재시도 계기가 `append`(= 다음 요란
        PM 확정 = 대PM 주기 3~4개월 뒤)뿐이면 실전에서 재시도가 없는 것과 같다(리뷰 M1).
        여기서 상시 회수한다 — 빈 큐 가드가 있어 상시 호출 비용은 O(1). 실패는 삼킨다
        (pm_log는 A 피처 최신성 문제지 안전 경로가 아니다 — 6-2).
        """
        if self.pm_log_writer is None:
            return
        try:
            self.pm_log_writer.flush_pending()
        except Exception:                        # noqa: BLE001 — 루프 보호
            logger.exception("pm_log 보류분 재시도 실패 — 다음 poll에 재시도")

    # ── 처리 ────────────────────────────────────────────────────────
    def _deserialize(self, msg) -> dict | None:
        """JSON 역직렬화 — 실패 시 스킵+로그, 파이프라인은 살린다 (헌법 6-2).

        `json.loads` 성공을 dict로 가정하지 않는다 (7장 안티패턴) — list·str·숫자·null도
        유효 JSON이라, dict가 아니면 하류 `.get()`이 AttributeError로 루프를 죽인다. 여기서 걸러
        깨진 JSON과 동일하게 skip 처리한다.
        """
        try:
            row = json.loads(msg.value())
        except Exception:                      # noqa: BLE001 — 어떤 깨짐도 루프를 못 죽인다
            logger.warning("역직렬화 실패 — 메시지 skip (offset=%s)", msg.offset())
            self.stats["skipped_deserialize"] += 1
            return None
        if not isinstance(row, dict):          # dict 아닌 유효 JSON(list 등)도 skip (7장 가드)
            logger.warning("역직렬화 결과가 dict 아님(%s) — 메시지 skip (offset=%s)",
                           type(row).__name__, msg.offset())
            self.stats["skipped_deserialize"] += 1
            return None
        return row

    def _dispatch(self, msg, row: dict) -> None:
        """토픽별 핸들러 라우팅. 미등록 토픽은 무시(후속 태스크에서 추가)."""
        if msg.topic() == self.topic_raw:
            self._handle_raw(row, msg)

    def _handle_raw(self, row: dict, msg) -> None:
        """row 1건 버퍼링 + 웨이퍼 경계 감지 (결정 2).

        `fdc.raw`엔 웨이퍼 종료 마커가 없다. 같은 챔버에서 wafer_id가 바뀌는 순간이
        직전 웨이퍼 완성을 알리는 **최소지연 신호**이므로 그때 flush한다.
        """
        ch = row["chamber_id"]
        hw = self._restored_highwater.get(ch)    # D14 (§6-2-9 나) — 크래시 재시작 dedup
        if hw is not None and msg.offset() <= hw:  # 이미 처리한 offset의 재전달 → skip(챔버별 키)
            self.stats["durable_dedup_skips"] += 1   # §5 — dedup이 일한 증거
            return
        buf = self.buffers.setdefault(ch, self._new_buffer())
        if buf["wafer_id"] is not None and row["wafer_id"] != buf["wafer_id"]:
            self._flush(ch)
            buf = self.buffers[ch]
        buf["wafer_id"] = row["wafer_id"]
        buf["rows"].append(row)
        buf["last_row_time"] = self.clock()
        m = (msg.topic(), msg.partition(), msg.offset())
        if buf["first_msg"] is None:             # B7-1 §3-1 — 웨이퍼 첫 row offset(min un-flushed 계산용)
            buf["first_msg"] = m
        buf["last_msg"] = m
        self.stats["handled"] += 1

    # ── 버퍼 · 완성 트리거 ──────────────────────────────────────────
    @staticmethod
    def _new_buffer() -> dict:
        # B7-1 §3-1: first_msg = 웨이퍼 첫 row offset (미flush 상한 계산). last_msg = 마지막(진척).
        return {"wafer_id": None, "rows": [], "last_row_time": None,
                "first_msg": None, "last_msg": None}

    def _sweep_idle(self) -> None:
        """tail — 마지막 웨이퍼는 경계 신호가 안 오므로 무입력 시간으로 완성 판정."""
        now = self.clock()
        for ch in list(self.buffers):            # 순회 중 변경 대비 키 복사
            buf = self.buffers[ch]
            if buf["rows"] and now - buf["last_row_time"] > self.idle_flush_sec:
                logger.info("idle tail flush: chamber=%s wafer=%s", ch, buf["wafer_id"])
                self._flush(ch)

    def _flush_all(self) -> None:
        """종료 시 남은 버퍼 전부 flush."""
        for ch in list(self.buffers):
            self._flush(ch)

    def _flush(self, ch: str) -> None:
        """웨이퍼 하나 완성 처리 — 집계·팬아웃 후 정밀 커밋. 예외에도 버퍼는 반드시 비운다.

        B7-1 §3-1: 커밋은 버퍼 리셋 **後**에 한다(min un-flushed 계산이 깨끗한 버퍼를 보게).
        실패 웨이퍼는 `_pending_failed`에 first offset 보관(D16 — 상한 홀드로 재시작 회수).
        qual 웨이퍼는 진행 세션 최초 offset을 홀드해 워터마크가 세션을 앞지르지 못하게 한다(§3-1-3).
        """
        buf = self.buffers.get(ch)
        if not buf or not buf["rows"]:
            return
        flushed_msg, first_msg = buf["last_msg"], buf["first_msg"]
        is_qual = buf["rows"][-1].get("is_qual")
        ok = False
        try:
            self._process_wafer(buf["rows"])
            if is_qual:                          # §3-1-3 — 진행 중 qual 세션 최초 offset 홀드
                qb = self._qual_buf.get(ch)      #   (세션 완료 시 #112가 pop → 홀드 자동 해제)
                if qb is not None and qb.get("session_first_msg") is None:
                    qb["session_first_msg"] = first_msg
            if flushed_msg is not None:          # D14 — 챔버별 처리 high-water(metadata용)
                self._last_processed[ch] = flushed_msg[2]
            self.stats["flushed"] += 1
            ok = True
        except Exception:                        # noqa: BLE001 — 한 웨이퍼 실패가 루프를 못 죽인다
            logger.exception("flush 실패 — chamber=%s wafer=%s", ch, buf["wafer_id"])
            if first_msg is not None:            # D16 — 실패 offset 보관(버퍼는 리셋되나 상한 홀드)
                # setdefault — 같은 챔버가 연속 실패해도 **최초(최소) offset**을 유지한다. 덮어쓰면
                #   커밋 상한이 나중 실패분으로 올라가 앞선 실패 웨이퍼가 '처리됨'으로 커밋되고,
                #   재시작해도 회수 불가(영구 유실 — D16 취지 파괴). offset은 단조 증가라 최초=최소.
                self._pending_failed.setdefault(ch, first_msg)
            self.stats["flush_fail"] += 1        # §5 GREEN 게이트 0 조건(loss=0 지표 단일 소스)
        finally:
            self.buffers[ch] = self._new_buffer()   # P3 — 다음 웨이퍼 오염 방지
        if ok and flushed_msg is not None:
            # 리셋 後 정밀 커밋(§3-1 — min-hold가 버퍼 리셋 뒤에 돌아야 해서 try 밖이다).
            #   try 밖이라 예외가 루프 밖으로 새는데 `run()`엔 except가 없어 프로세스가 죽고,
            #   그 경로가 `run()`의 `finally: _flush_all()`을 지나며 **거기서 또 던져 원래 예외를
            #   대체**한다(죽은 이유가 트레이스백에 안 남음). 커밋 offset은 누적이라 삼켜도
            #   안전하다 — 다음 성공 커밋이 덮는다. (PR #128 PM 리뷰 지적 1)
            try:
                self._commit(flushed_msg)
            except Exception:                    # noqa: BLE001 — 커밋은 누적이라 다음 성공분이 덮는다
                self.stats["commit_fail"] += 1   # 조용히 삼키지 않도록 계측(§5 관측)
                logger.exception("commit 실패 — 다음 flush에서 재시도 (chamber=%s)", ch)

    def _commit(self, flushed_msg: tuple) -> None:
        """정밀 커밋 (B7-1 §3-1 D1) — 커밋 상한 = 전 챔버 **min un-flushed offset − 1**.

        `fdc.raw`는 chamber 키 파티셔닝이나 **파티션 수 = 1**(docker-compose)이라 4챔버가 한
        파티션에 interleaved된다. flushed 웨이퍼 offset만 커밋하면 **타 챔버 미flush 저-offset이
        consumed로 마킹돼 크래시 loss**(D1). min 계산 대상 = ⓐ 진행 중 웨이퍼(`first_msg`)
        ⓑ `_pending_failed`(D16 실패분) ⓒ 진행 중 qual 세션(`session_first_msg` — §3-1-3).
        대상 없으면 flushed offset까지. 부작용(재전달 dup)은 at-least-once 수용(D13)·D15가 판정 보호.

        `_commit_msg`(correction 등 타 토픽)와 분리 — 여기는 `fdc.raw` 1 파티션만 다룬다(§3-1 ④).
        커밋은 단조 증가만(파티션 offset 단조 — 되감기=재소비 방지 가드).
        """
        topic, partition, flushed_off = flushed_msg
        holds = [b["first_msg"][2] for b in self.buffers.values()
                 if b["rows"] and b["first_msg"] is not None]        # ⓐ 진행 중 웨이퍼
        holds += [m[2] for m in self._pending_failed.values()]       # ⓑ D16 실패분
        holds += [qb["session_first_msg"][2] for qb in self._qual_buf.values()
                  if qb.get("session_first_msg") is not None]        # ⓒ qual 세션(§3-1-3)
        holds += [it["first_msg"][2] for q in self._pending_join.values() for it in q
                  if it.get("first_msg") is not None]                # ⓓ 예측 조인 유예 대기분
        #   ⓓ 없이 커밋하면 "커밋은 됐는데 알람은 안 나간" 구간이 생겨, 그 사이 크래시 시
        #   알람이 **조용히 유실**된다(무기록 유실 — ⓑ D16 과 같은 취지). 유예 상한이 1초
        #   미만이라 홀드 자체는 무시할 수준이다.
        min_hold = min(holds) if holds else None
        commit_off = (min_hold - 1) if min_hold is not None else flushed_off
        # D16 정체 관측 — 실패 offset이 커밋 상한을 홀드하면 WARNING 1회/챔버(_note_gate_block 선례).
        #   자동 해소 없음(만료=유실 승인이라 취지 위배). 정상 런엔 발생하면 안 되는 사건 = 조사 대상.
        # 지적 4 — ⓐ 최솟값 보유 챔버만 경고하면 **동시 실패 시 하나만** 보인다(나머지도 홀드 중인데
        #   조용). ⓑ 챔버당 1회 throttle 이면 자동 해소가 없는 **영구 정체가 시간이 지나며 조용해진다**.
        #   → 실패한 전 챔버를 대상으로, `_STALL_REWARN_SEC` 마다 재경고한다.
        now = self.clock()
        for fch, fm in self._pending_failed.items():
            last = self._commit_stall_warned.get(fch)
            if last is None or (now - last) >= _STALL_REWARN_SEC:
                logger.warning("커밋 정체 — chamber=%s 실패 offset %d 홀드 중"
                               "(재시작 회수 대기·자동해소 없음, 커밋 상한 %s, flush 실패 누적 %d)."
                               " 정상 런엔 없어야 함",
                               fch, fm[2], commit_off, self.stats["flush_fail"])
                self._commit_stall_warned[fch] = now
        if commit_off <= self._last_committed_off:   # 단조 가드 — 되감기(재소비) 방지
            return
        self._last_committed_off = commit_off
        # D14 (§6-2-9 가) — 챔버별 처리 high-water를 metadata로 동봉(커밋 offset과 별개)
        metadata = json.dumps(self._last_processed, separators=(",", ":")) if self._last_processed else None
        self.consumer.commit(offsets=self.offset_factory(topic, partition, commit_off, metadata),
                             asynchronous=False)

    def _assert_single_partition(self) -> None:
        """`fdc.raw` 파티션 1 전제를 기동 시 확인해 ERROR 로 남긴다 (PM 리뷰 지적 3).

        두 군데가 파티션 1을 전제한다 — `_restore_dedup_state`의 `TopicPartition(topic, 0)`
        하드코딩, 그리고 `_commit`의 min-hold가 **파티션 구분 없이 offset을 섞어 min()** 하는 것.
        파티션을 늘리면 **조용히 틀린다**: 커밋이 과다 진행되면 loss, 반대면 영구 정체인데
        **둘 다 예외가 안 난다.** docstring에만 적혀 있던 전제를 코드로 옮긴다.

        **기동은 막지 않는다** — 관측 전용이고, 브로커 조회 실패가 컨슈머를 못 띄우게 하면
        안 된다(가드가 서비스를 죽이는 역전). 조회 실패는 debug 로만 남긴다.
        """
        try:
            md = self.consumer.list_topics(topic=self.topic_raw, timeout=_DEDUP_RESTORE_TIMEOUT_SEC)
            n = len((md.topics[self.topic_raw].partitions or {}))
            if n > 1:
                logger.error("파티션 전제 위반 — %s 파티션 %d개(1 전제). D14 복원은 partition 0만 "
                             "읽고 min-hold는 파티션을 안 나눠 **조용히 틀린다**(loss 또는 영구 정체). "
                             "파티션을 늘렸다면 offset 정밀 로직을 파티션별로 분리해야 한다",
                             self.topic_raw, n)
        except Exception as e:                   # noqa: BLE001 — 관측 전용, 기동을 막지 않는다
            logger.debug("파티션 수 확인 실패(무시): %s", e)

    def _restore_dedup_state(self) -> None:
        """D14 (§6-2-9) — 기동 시 `fdc.raw` 커밋 metadata(챔버별 마지막 처리 offset)를 읽어 복원.

        크래시 재시작 시 커밋(min un-flushed) 이후 Kafka가 재전달하는 **이미 처리된** 웨이퍼를
        `_handle_raw`가 챔버별 high-water로 skip한다(tttm/RTD 중복 롤업 방지 — §3-2③). metadata
        부재/파싱 실패면 방어 없이 진행(at-least-once로 안전 축소). 정상 최초 기동엔 metadata 없음.
        """
        try:
            from confluent_kafka import TopicPartition
            # 1회 재시도 — 복원 실패는 **이번 재시작의 dedup 만 잃는 게 아니다**. `_last_processed`가
            #   빈 채로 남아 이번 세션에 flush 한 챔버만 metadata 에 실리므로, 직전 metadata 의
            #   나머지 챔버 high-water 가 커밋 한 번에 소멸해 **다음 재시작의 보호까지** 깎는다
            #   (2차 리뷰 Critical과 같은 형태 — PM 리뷰 지적 2). 브로커 일시 지연이 흔한 실패
            #   원인이라 timeout 을 늘려 한 번 더 시도한다. 그래도 실패면 at-least-once 로 진행.
            last_err: Exception | None = None
            for attempt, timeout in enumerate((_DEDUP_RESTORE_TIMEOUT_SEC,
                                               _DEDUP_RESTORE_TIMEOUT_SEC * _DEDUP_RESTORE_RETRY_FACTOR), 1):
                try:
                    parts = self.consumer.committed(
                        [TopicPartition(self.topic_raw, 0)], timeout=timeout)
                    for tp in parts or []:
                        if getattr(tp, "metadata", None):
                            self._restored_highwater = {
                                k: int(v) for k, v in json.loads(tp.metadata).items()}
                    last_err = None
                    break
                except Exception as e:           # noqa: BLE001 — 재시도 후에도 실패면 아래에서 처리
                    last_err = e
                    logger.warning("D14 dedup 복원 시도 %d 실패(timeout=%.1fs): %s", attempt, timeout, e)
            if last_err is not None:
                raise last_err
        except Exception:                        # noqa: BLE001 — 복원 실패는 치명 아님(at-least-once)
            self.stats["dedup_restore_fail"] += 1   # §5 계측 — 방어가 조용히 꺼진 상태 감지(GREEN 0 조건)
            logger.exception("D14 dedup 복원 실패(재시도 포함) — 크래시 dup 방어 없이 진행")
        if self._restored_highwater:
            # 복원분을 커밋 metadata 원본(`_last_processed`)에도 시딩한다. 안 하면 재시작 후 **먼저
            #   flush한 챔버 하나**의 커밋이 metadata를 자기 것만으로 덮어써 나머지 챔버 high-water가
            #   소실되고, 연쇄 크래시(재시작→일부 flush→재크래시) 시 그 챔버들의 dedup 보호가
            #   사라진다(중복 판정 유입 — PR #128 2차 리뷰 Critical). 이후 flush가 챔버별로 덮어씀.
            self._last_processed.update(self._restored_highwater)
            logger.info("D14 dedup 복원: 챔버 %d개 처리 high-water = %s",
                        len(self._restored_highwater), self._restored_highwater)

    # ── 팬아웃 · seam ──────────────────────────────────────────────
    def _process_wafer(self, rows: list) -> None:
        """웨이퍼 rows → 집계 → Point → 두 엔진 팬아웃 → 수집 seam.

        **엔진 비대칭(H1)**: Nelson `feed` 반환은 위반이라 `spc_flags`로 모으고, TTTM
        `feed` 반환은 텔레메트리(state 축적)라 **버린다** — TTTM 판정은 웨이퍼 처리 후
        `chamber_rollup(ch)`로 별도 산출한다. 계약상 두 산출물이 별개다.

        **B5-2b**: `is_qual` 웨이퍼는 Qual 전용 경로로 분기하고 일반 SPC 경로(nelson/tttm
        feed·collector·provisional·persist)를 **전부 우회**한다(결정1).
        """
        if rows[-1].get("is_qual"):              # wafer 단위 필드(발행부가 전 row 동일 세팅)
            # 🔴 PM 경계만은 우회 대상이 아니다 (2026-08-13). 정지 중 챔버는 생산 wafer가
            #   발행되지 않아 **여기가 pm_count↑를 관측하는 유일한 경로**다 — 놓치면 PHASE_0에
            #   못 들어가고, 뒤이은 요란 확정·R9가 phase 가드에서 조용히 무시된다(_observe_pm 참조).
            #   판정·버퍼·카운터는 종전대로 우회한다(결정1) — 관측하는 것은 경계 하나뿐.
            qch = rows[-1]["chamber_id"]
            self._observe_pm(qch, rows[-1].get("pm_count"), rows[-1].get("timestamp"))
            # 🔴 Qual 5장을 **Phase 0 새-레짐 중심 버퍼**에 먹인다 (2026-08-13).
            #   `ProvisionalMode(min_center_n=3)`의 기본값 주석이 *"Qual 5장 전제로 기본 3"* 이라고
            #   적혀 있다 — 즉 설계는 이 5장으로 새 중심을 잡게 돼 있었는데, 결정1의 전면 우회가
            #   그 5장을 버퍼에 넣지 않아 `_center_for`가 **항상 old_center로 폴백**했다.
            #   결과: 광폭이 옛 자리에 걸려, 이동량이 provisional_k(5)·σ_ref를 넘는 그룹은 요란
            #   확정 뒤에도 계속 위반한다 (2026-08-12 CH2 실측 — C17 step1 이동 +6.5σ / step4 −19σ).
            #   판정(nelson·tttm)·collector·persist는 종전대로 우회한다 — 먹이는 것은 중심 버퍼뿐.
            if self.provisional is not None:
                try:
                    self.provisional.on_wafer(qch, rows[-1]["wafer_id"],
                                              _aggregate_to_points(rows, self.limits))
                except Exception:                    # noqa: BLE001 — Qual 판정 경로 보호(6-2)
                    logger.exception("Qual wafer 중심 버퍼 적재 실패 — 판정은 계속: %s", qch)
            self._handle_qual_wafer(rows)
            return
        chamber_id = rows[-1]["chamber_id"]
        wafer_id = rows[-1]["wafer_id"]          # D15(B7-1): dedup 가드·하류 공용 (단일 read)
        points = _aggregate_to_points(rows, self.limits)
        violations: list = []                    # B4-1 Step3a — Nelson 원형 위반 누적(축약 spc_flags 아님)
        # D15 (B7-1 §3-2-1): 재전달 중복(같은 chamber·wafer)이면 **판정 파이프라인 전체를 no-op**한다.
        #   ⚠️ 범위 = **동일 프로세스 재전달(ⓑ)만** — `_last_wafer`가 인메모리라 크래시 재시작(ⓐ)엔
        #   가드가 비어 못 막는다(그땐 버퍼도 비어 오염 없음·N2/N7 창은 warm-up replay §3-3 후속).
        #   엔진 시간 가드가 동일 timestamp를 수용(nelson_engine:142~144·tttm 동일)해 재전달 Point가
        #   판정 버퍼에 이중 적재되면 N3/N4 미탐·N5/N6 위조 발화(⑥⑦). 게다가 하류 collector(fdc.alert
        #   seam)·_tally_verify(오탐율 tally)까지 재전달마다 실행되면 중복 알람·Scorecard 통계 왜곡이
        #   샌다(리뷰 지적). dup은 진입부에서 판정(_drive_provisional이 _last_wafer를 갱신하기 前)하고,
        #   ⓐ 엔진 feed는 여기서 스킵, ⓑ 카운터·마커·표본 버퍼(①②③)는 _drive_provisional(:598)이 자체
        #   :604 가드로 처리(현행 유지), ⓒ 그 뒤 하류 판정(tally·collector·persist)은 `if dup: return`.
        #   (통째 write 이동은 :598 비교를 항상 False로 만들어 _post_pm를 조용히 정지시키므로 기각.)
        dup = self._last_wafer.get(chamber_id) == wafer_id
        if dup:
            self.stats["redelivery_dup_wafers"] += 1     # §5 — 동일프로세스 재전달 감지 수
        if not dup:
            for p in points:
                try:
                    violations += self.nelson.feed(p)   # score_spc가 current_value·관리선·rule_id 요구
                except Exception:                    # noqa: BLE001 — 엔진 격리
                    logger.exception("nelson feed 실패: %s", p.group_key)
                try:
                    self.tttm.feed(p)                # 반환=텔레메트리, 수집 안 함 (H1)
                except Exception:                    # noqa: BLE001
                    logger.exception("tttm feed 실패: %s", p.group_key)
        try:
            tttm_obj = self.tttm.chamber_rollup(chamber_id)
        except Exception:                        # noqa: BLE001
            logger.exception("tttm chamber_rollup 실패: %s", chamber_id)
            tttm_obj = None
        # ⓐ 가한계 상태머신 구동 (B6-3-c) — 매 wafer, 판정 유무와 무관(카운터·버퍼 유실 방지)
        self._drive_provisional(chamber_id, rows[-1]["wafer_id"],
                                rows[-1].get("pm_count"), points,
                                ts=rows[-1].get("timestamp"))   # B6-3-f — PM 개방 시각 소스
        if dup:                                      # D15 — 재전달 중복: 하류 판정 no-op. 원본 1회로
            return                                   #   alert·tally 완료(카운터는 _drive_provisional이 처리)
        if chamber_id in self._verify:               # firm 후 forward 오탐 tally(결정3)
            self._tally_verify(chamber_id, bool(violations))
        recipe_id = rows[-1]["recipe_id"]        # B4-1 Step5b(리뷰②) — seam이 raw row recipe 승계(P99 정타·crazy-only도 확보)
        # B4-1 Step5b(리뷰2): 위반·tttm 0이어도 예측 있으면 crazy 판정 도달 위해 collector로 태운다.
        #   진성 crazy(위반·tttm 0)가 여기서 드롭되면 하류 B9 판정이 아예 못 본다.
        if not violations and not tttm_obj and not self._has_prediction(wafer_id):
            return
        ts = _normalize_ts(min(r["timestamp"] for r in rows))   # wafer t0(위반 timestamp와 동일 기준·now() 아님)
        logger.info("판정 %s: nelson=%s tttm=%s", wafer_id, _to_spc_flags(violations), tttm_obj)
        if self.collector is not None:           # ① 실시간 alert 먼저 (L4) → B4-1
            item = (wafer_id, chamber_id, ts, violations, tttm_obj, recipe_id)
            if self._defer_for_prediction(chamber_id, wafer_id, item):
                pass                             # 유예 큐로 — 드레인이 나중에 collector 호출
            else:
                self._emit_collector(item)
        self._persist_tttm(tttm_obj)             # ② 이력 적재 마지막 (L4 — collector 실패해도 무조건 호출)

    # ── 예측 조인 유예 (grace) ──────────────────────────────────────
    # 왜: B 와 A 가 같은 트리거("같은 챔버 다음 wafer 도착")로 출발하는데 A 만 모델 추론
    #   왕복이 더 붙어 **중앙값 57ms** 늦다(실측 2026-08-11). 그래서 B 조회가 먼저 끝나
    #   압축 리플레이에서 조인이 99.4% 빗나간다. `lean85.idle_flush_sec`(A)는 **무입력
    #   경로**만 덮으므로 시연 속도(같은 챔버 간격 약 0.6s)에서는 발동하지 않는다.
    # 왜 sleep 이 아닌가: 이 컨슈머는 `fdc.prediction` 도 **같은 poll 루프**로 받는다.
    #   자면 기다리는 대상이 못 들어온다 — 기다림이 해법을 막는다. 그래서 "치워두고
    #   루프를 계속 돌린 뒤 다음 반복에 재조회"한다. 유예는 **상한**이지 고정 지연이 아니다.
    # 상세 = `specs/예측조인유예_스펙플랜.md`
    def _defer_for_prediction(self, chamber_id: str, wafer_id: str, item: tuple) -> bool:
        """예측 결측이면 유예 큐에 적재하고 True. 비활성·hit·큐포화면 False(즉시 발행)."""
        if self._join_grace_sec <= 0:            # 킬 스위치 — 완전 현행 동작
            return False
        if self._has_prediction(wafer_id):
            return False
        if self._pending_join_len() >= self._join_pending_max:
            self.stats["join_grace_overflow"] += 1
            return False                         # 메모리 가드 — 유예 없이 즉시 발행
        buf = self.buffers.get(chamber_id) or {}
        self._pending_join[chamber_id].append({
            "item": item, "wafer_id": wafer_id,
            "deadline": self.clock() + self._join_grace_sec,
            "first_msg": buf.get("first_msg"),   # 커밋 홀드용(발행 전 커밋 방지)
        })
        self.stats["join_grace_enqueued"] += 1
        return True

    def _pending_join_len(self) -> int:
        """유예 큐 전체 길이 (챔버 합)."""
        return sum(len(q) for q in self._pending_join.values())

    def _emit_collector(self, item: tuple) -> None:
        """collector 호출 — 예외 격리(§4-1 P1). 유예 경로와 즉시 경로가 공유한다."""
        try:
            self.collector(item)
        except Exception:                        # noqa: BLE001 — 오케스트레이터 예외 격리
            # 체인 예외가 persist 스킵·_flush except행(오프셋 미커밋→대량 리플레이)로 번지지 않게 삼킴.
            self.stats["collector_failed"] += 1
            logger.exception("collector 호출 실패 — wafer=%s (persist·커밋은 계속)", item[0])

    def _drain_pending_join(self, force: bool = False) -> None:
        """유예 큐 소진 — 예측 도착 or 상한 초과 시 발행. poll 마다 호출(예외 격리).

        챔버 내에서는 **앞이 안 풀리면 뒤도 대기**시켜 발행 순서를 wafer 순서와 맞춘다
        (`alert_id` SEQ 가 챔버별 max+1 이라 챔버 내 단조성이 유지된다). 상한이 유한하므로
        지연 누적도 유한하다. `force=True`(종료 경로)는 상한을 무시하고 전부 발행한다 —
        안 그러면 종료 시 유예 중인 알람이 사라진다.
        """
        if not self._pending_join:
            return
        now = self.clock()
        for ch in list(self._pending_join):
            q = self._pending_join[ch]
            while q:
                head = q[0]
                hit = self._has_prediction(head["wafer_id"])
                if not hit and not force and now < head["deadline"]:
                    break                        # 순서 보존 — 뒤도 같이 대기
                q.popleft()
                self.stats["join_grace_resolved" if hit else "join_grace_expired"] += 1
                self._emit_collector(head["item"])
            if not q:
                del self._pending_join[ch]
        # 상한 초과(= 유예로도 못 받음)는 조용히 흐르면 안 된다 — N건마다 노출(헌법 7장).
        exp = self.stats["join_grace_expired"]
        if exp and exp - self._join_expired_warned >= _JOIN_EXPIRE_WARN_EVERY:
            self._join_expired_warned = exp
            logger.warning("예측 조인 유예 만료 누적 %d건 — 상한(%.2fs) 안에 예측이 안 왔다. "
                           "a-pred 상태·`lean85.idle_flush_sec` 확인", exp, self._join_grace_sec)

    def _has_prediction(self, wafer_id: str) -> bool:
        """예측 캐시에 이 wafer 예측이 있는지 peek (B4-1 Step5b 리뷰2 — crazy 도달 보장).

        collector 미주입이면 조회 자체를 생략(굳이 캐시 건드리지 않음). `get`은 비파괴(entry
        유지·`_read`만 표시)라 하류 build_joined 재조회에 무해. 조회 예외는 격리 — 캐시 오류가
        판정 루프를 못 죽인다(헌법 6-2, 예측 없음으로 처리).
        """
        if self.collector is None:
            return False
        try:
            return self.prediction_cache.get(wafer_id) is not None
        except Exception:                        # noqa: BLE001 — 캐시 조회 실패 격리
            logger.exception("prediction 조회 실패 — crazy 우회 skip: %s", wafer_id)
            return False

    # ── B5-2b Qual 판정·적재 라이브 배선 ────────────────────────────────
    def _handle_qual_wafer(self, rows: list) -> None:
        """Qual 웨이퍼 1건 수집 → 5장(config) 도달 시 판정·적재 (B5-2b §3, 결정1·2).

        인메모리 `_qual_buf[chamber]`에 그룹별 요약값을 누적한다. pm_count가 바뀐 Qual 도착은
        새 Qual 시작이라 직전 미완 버퍼를 partial 판정(#2 — proposed=None)한 뒤 교체한다.
        동일 프로세스 재전달(같은 wafer_id)은 dedup. Qual 경로는 nelson/tttm feed·collector·
        provisional·persist를 **부르지 않는다**(결정1 우회).
        """
        last = rows[-1]
        ch, wid, pm = last["chamber_id"], last["wafer_id"], last.get("pm_count")
        buf = self._qual_buf.get(ch)
        if buf is None or buf["pm_count"] != pm:              # 새 Qual 시작(pm_count = Qual 키)
            if buf is not None and buf["wafer_ids"]:          # 직전 미완 버퍼 → partial 판정(#2)
                self._judge_qual(ch, buf, partial=True)
            buf = self._qual_buf[ch] = {
                "pm_count": pm, "date": self._qual_date(last.get("timestamp")),
                "wafer_ids": [], "values": {}}
        if wid in buf["wafer_ids"]:                           # 동일 프로세스 재전달 dedup
            return
        for gk, val in _group_values(rows):
            buf["values"].setdefault(gk, []).append(val)
        buf["wafer_ids"].append(wid)
        if len(buf["wafer_ids"]) >= self._qual_wafer_count:   # 5장 완료 → 판정·적재
            self._judge_qual(ch, self._qual_buf.pop(ch))

    @staticmethod
    def _qual_date(ts) -> str:
        """첫 Qual wafer timestamp → YYYYMMDD (UTC, qual_id SEQ 채번용, §5). 파싱 실패 시 now(UTC)."""
        try:
            dt = datetime.fromisoformat(_normalize_ts(ts))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y%m%d")
        except (ValueError, TypeError):
            return datetime.now(timezone.utc).strftime("%Y%m%d")

    def _get_qual_cfg(self) -> QualConfig:
        """QualConfig lazy 1회 로드 (§4 ④)."""
        if self._qual_cfg is None:
            self._qual_cfg = QualConfig.load()
        return self._qual_cfg

    def _judge_qual(self, ch: str, buf: dict, partial: bool = False) -> None:
        """수집 버퍼 → σ-갭·Nelson(evaluate_qual) + A 지표(AE·C65) 취합 → `quals` 적재 (§3·§5).

        스냅샷 read + A read + INSERT를 한 트랜잭션(`engine.begin()`)으로 처리한다. 빈 스냅샷은
        evaluate_qual이 UNKNOWN을 내며(거짓 PASS 금지 내장) NULL 매핑 적재 + WARNING(graceful,
        6-2). partial(5장 미만)은 proposed_verdict=None·rebase_centers 미저장(#2). 예외는 전파해
        `_flush`가 오프셋 미커밋(재시작 재수집·ON CONFLICT 중복 차단, 결정7).
        """
        qid = f"QUAL-{buf['date']}-{ch}-{buf['pm_count']}"    # full chamber_id·pm_count=SEQ(결정7)
        cfg = self._get_qual_cfg()
        vals = buf["values"]
        with self.engine.begin() as conn:
            snapshot = load_active_snapshot(conn, ch)
            snap_id = conn.execute(_ACTIVE_SNAP_ID, {"ch": ch}).scalar()
            qi = {gk: vals[gk] for gk in snapshot if gk in vals}   # 스냅샷 그룹 교집합
            ind = evaluate_qual(qi, snapshot, cfg, chamber_id=ch, qual_id=qid, snapshot_id=snap_id)
            preds = conn.execute(_QUAL_PREDS, {"wids": buf["wafer_ids"]}).mappings().all()
            ae_mean, c65_ok = _combine_a_indicators(preds, self._qual_c65_max)
            proposed = None if partial else _propose_verdict(
                ind, ae_mean, self._qual_ae_max, self._qual_use_ae)
            sg, nel = ind.sigma_gap, ind.nelson
            # R5 center-only 리베이스용 per-group center(full 5장만·결정9) — 조용 confirm 때 사용
            rebase_centers = {} if partial else {
                gk_str(*gk): _aggregate(vals[gk], snapshot[gk]["method"])
                for gk in snapshot if gk in vals}
            thresholds = {
                "pass_ae_max": self._qual_ae_max, "pass_sigma_gap_max": cfg.threshold,
                "pass_nelson_violations_max": cfg.nelson_max, "pass_c65_max": self._qual_c65_max,
                "wafer_count": self._qual_wafer_count, "sigma_gap_unit": "sigma",
                "snapshot_id": snap_id, "top_gap_sensor": sg.get("top_gap_sensor"),
                "sigma_gap_verdict": sg.get("verdict"), "nelson_verdict": nel.get("verdict"),
                "partial": partial, "rebase_centers": rebase_centers,
            }
            insert_qual(conn, {
                "qual_id": qid, "chamber_id": ch, "wafer_ids": buf["wafer_ids"],
                "ae_anomaly_mean": ae_mean,
                "tttm_gap_pct": sg.get("max_gap_sigma"),          # σ 단위(legacy 컬럼명·§5 #5a)
                "nelson_violations": (None if nel.get("verdict") == QUAL_UNKNOWN
                                      else nel.get("violation_count")),
                "c65_within_normal": c65_ok, "thresholds": thresholds,
                "proposed_verdict": proposed,
            })
        if not snapshot:
            logger.warning("Qual 판정: 활성 스냅샷 부재 — UNKNOWN/NULL 적재: %s (%s)", ch, qid)
        logger.info("Qual 판정 적재: %s partial=%s proposed=%s σ-gap=%s(%s) nelson=%s",
                    qid, partial, proposed, sg.get("verdict"), sg.get("max_gap_sigma"),
                    nel.get("verdict"))

    def _confirm_qual(self, row: dict, chamber: str, verdict: str) -> None:
        """확정 이벤트(QualVerdictConfirmed) → quals 확정 UPDATE(#4) + 조용 시 center 리베이스(결정9).

        gateway `/qual/verdict`는 이벤트 emit만 하고 quals를 안 닫으므로 B가 닫는다(§11 #4).
        engine·qual_id 부재면 skip. 예외는 삼킨다(6-2 — agent 경로는 항상 커밋). 리베이스는
        조용(quiet) + 저장된 rebase_centers(full 5장) 존재 시에만.
        """
        if self.engine is None:
            return
        qual_id = row.get("qual_id")
        if qual_id is None:
            logger.warning("QualVerdictConfirmed qual_id 부재 — quals 확정 skip: %r", row)
            return
        try:                                                  # ① 확정 UPDATE — 감사 정본(#4)
            with self.engine.begin() as conn:
                update_qual_confirmed(conn, qual_id, verdict,
                                      row.get("approved_by"), row.get("confirmed_at"))
        except Exception:                                     # noqa: BLE001 — 루프 보호(6-2)
            logger.exception("Qual 확정 UPDATE 실패 — skip: %s", qual_id)
            return
        if verdict != "quiet":                                # 결정9 — 조용 확정만 center 리베이스
            return
        if f"-{chamber}-" not in qual_id:                     # 하드닝 — 이벤트 chamber ≠ qual_id 챔버면 리베이스 skip
            logger.warning("Qual 리베이스 skip — chamber/qual_id 불일치: ch=%s qid=%s", chamber, qual_id)
            return                                            #   (확정 UPDATE는 qual_id 기준이라 이미 안전 처리됨)
        try:                                                  # ② center 리베이스 — 파생 참조(별도 txn)
            with self.engine.begin() as conn:                 #   실패해도 ①(확정) 무영향
                centers = self._read_rebase_centers(conn, qual_id)
                if centers:
                    rebase_snapshot_center(conn, chamber, centers)
        except Exception:                                     # noqa: BLE001 — 파생이라 삼킴(6-2)
            logger.exception("Qual 조용 리베이스 실패 — 확정은 완료됨: %s", qual_id)

    def _read_rebase_centers(self, conn, qual_id: str) -> dict:
        """judge 때 thresholds.rebase_centers에 저장한 per-group center를 읽는다(결정9)."""
        row = conn.execute(_QUAL_THRESHOLDS, {"qid": qual_id}).mappings().first()
        if row is None or not row["thresholds"]:
            return {}
        return row["thresholds"].get("rebase_centers") or {}

    # ── B6-3-c 가한계 라이브 배선 ──────────────────────────────────────
    def _settled_cutoff(self, chamber: str) -> int:
        """정착 표본 컷오프 = a의 settled_at (미수신이면 seasoning_loud 폴백=현행 1,000, B6-3-e).

        조기 정착(예: 600)이면 601부터 backward 적재·establish 표본 컷오프. 상한 폴백
        (converged=False·settled_at=1000)이면 1,001부터 = 기존 룰과 바이트 동일(리뷰1③).
        """
        return self._settled_at.get(chamber, self.establish_cfg.seasoning_loud)

    def _observe_pm(self, chamber: str, pm_count, ts=None) -> None:
        """PM 경계(pm_count↑) 관측 → `on_pm`(PHASE_0 진입) + PM 시점 버퍼·카운터 리셋.

        **왜 `_drive_provisional`에서 분리했나 (2026-08-13)**: 정지(RTD inhibit) 중인 챔버는
        producer가 **생산 wafer를 발행하지 않고 Qual 5장만** 내보낸다(kafka_producer 정지 분기 —
        "정지된 챔버에도 계측 wafer는 흐른다"). 그런데 Qual wafer는 `_process_wafer` 진입부에서
        `_handle_qual_wafer`로 조기 분기해 provisional 경로를 통째로 우회한다(결정1). 그래서
        **정지 중 PM은 컨슈머에 도달할 경로가 없었고**, PHASE_0에 못 들어간 챔버는 이후
        `QualVerdictConfirmed`가 와도 `on_qual_verdict`의 phase 가드에서 조용히 무시된다 →
        가한계 미생성 → R9(`ChamberRequalified`)도 `AWAITING_PHASE2` 가드에서 무시 → firm
        재수립 없음 → **해제 후에도 옛 관리선으로 판정**(2026-08-12 CH2 실측: quals 는
        confirmed_verdict='loud'·requalified=true 인데 control_limits 는 v1 initial 그대로,
        limit_corrections 0건).

        producer 쪽 주석이 기록한 *"[정지 → Qual 불가 → 판정 불가 → R9 불가 → 해제 불가]
        데드락"* 의 **남은 절반**이다 — 그때는 Qual 발행만 뚫었다. 시연 챔버가 CH4(정지 없음)
        → CH2(정지 있음)로 바뀐 8/10 이후에야 드러난 잠복 버그.

        멱등: pm_count가 **증가할 때만** 1회. 첫 관측은 베이스라인(호출 X) — 매 wafer 호출하면
        정상 챔버가 PHASE_0로 오진입한다.
        """
        if self.provisional is None or pm_count is None:
            return
        prev = self._prov_pm.get(chamber)
        if prev is None:
            self._prov_pm[chamber] = pm_count                 # 베이스라인(on_pm 미호출)
        elif pm_count > prev:                                 # 진짜 PM 이벤트
            self._prov_pm[chamber] = pm_count
            # 무로그였던 전이에 관측점을 준다 — PHASE_0는 알람 억제 상태라(TSR-0005) 조용히
            # 들어가고 조용히 갇힌다. "들어갔다"는 사실이 로그에 없으면 이 사슬은 추적 불가.
            logger.info("PM 경계 관측: %s pm_count=%s → PHASE_0 진입 (ts=%s)", chamber, pm_count, ts)
            self.provisional.on_pm(chamber, pm_count, ts)     # → PHASE_0(억제) · ts=개방 시각(f)
            self._backward[chamber] = deque(maxlen=self.establish_cfg.rolling_window_n)
            self._post_pm[chamber] = 0
            self._last_wafer.pop(chamber, None)               # PM 리셋 시 dedup 마커도 리셋
            self._recalc_buf.pop(chamber, None)               # Step7 D3 — 옛 레짐 조용 표본 폐기
            self._shadow_buf.pop(chamber, None)               # Step7b D3 — shadow 표본도 폐기(레짐 전환)
            self._recalc_count[chamber] = 0                   # Step7 D1 — PM 시 트리거 카운터 리셋(20장 버퍼 오발동 방지)

    def _drive_provisional(self, chamber: str, wafer_id: str, pm_count, points: list,
                           ts=None) -> None:
        """가한계 상태머신 구동 — PM↑ 감지·on_wafer·정착 backward 버퍼. 미주입=no-op.

        **on_pm은 pm_count↑ 감지 시에만** 호출한다 — on_pm 첫 호출이 곧 PHASE_0 전이라(멱등
        진입), 매 wafer 호출하면 정상 챔버가 오진입한다. 첫 관측은 베이스라인(호출 X). 정착
        버퍼는 **post_pm_count > seasoning_loud(과도 제외)** 만·`rolling_window_n` 바운드(결정3
        인메모리). PHASE2_SIGNAL은 pending 표시(실 제안=`_try_phase2_proposal`, firm apply 배선).

        `ts`(B6-3-f) = pm_count↑를 감지한 wafer의 `timestamp` = **PM 개방 시각**. 요란 확정 시
        pm_log `date`가 되며(D2). PM 관측 자체는 `_observe_pm`으로 분리했다 — 정지 중 챔버는
        Qual wafer 경로에서만 PM이 관측되기 때문(2026-08-13).
        """
        if self.provisional is None or pm_count is None:
            return
        self._observe_pm(chamber, pm_count, ts)               # PM 경계 관측(Qual 경로와 공유 — 아래)
        signal = self.provisional.on_wafer(chamber, wafer_id, points)
        # Step7 D3 — "PM 이후 n번째 wafer" 단일화: dedup+카운터를 phase 밖으로(구: 요란 전용).
        #   재전달 dedup(#3) — a.on_wafer는 자체 dedup하나 c 카운터·버퍼는 별도라 여기서 방어.
        if wafer_id != self._last_wafer.get(chamber):
            self._last_wafer[chamber] = wafer_id
            self._post_pm[chamber] = self._post_pm.get(chamber, 0) + 1
            if points:                                        # 🔴 빈 points는 **버퍼만** skip — return 금지(아래 signal 처리 유지)
                if self.provisional.get_phase(chamber) is not Phase.NORMAL:   # 요란 → 정착 backward(B6-3-e)
                    if self._post_pm[chamber] > self._settled_cutoff(chamber):   # 컷오프=settled_at(조기 정착)
                        self._backward.setdefault(
                            chamber, deque(maxlen=self.establish_cfg.rolling_window_n)).append(
                            WaferSample(post_pm_count=self._post_pm[chamber],
                                        values={p.group_key: p.value for p in points}))
                elif self._post_pm[chamber] > self.establish_cfg.seasoning_quiet:   # 조용/정상 → 정기 리캘리 backward(Step7)
                    self._recalc_buf.setdefault(
                        chamber, deque(maxlen=self.establish_cfg.rolling_window_n)).append(
                        {p.group_key: p.value for p in points})
                    self._shadow_buf.setdefault(       # Step7b D3 — 소급 채점용 Point 30장(나란히·별도 maxlen)
                        chamber, deque(maxlen=self._shadow_cfg.n_wafers)).append(list(points))
        if signal is not None:
            self._settled_at[chamber] = signal.settled_at     # B6-3-e — 표본 컷오프 기준점 저장
            self._converged[chamber] = signal.converged       #   미수렴(폴백) 경고용 기록
            self._phase2_pending.add(chamber)                 # 제안 재시도 대상
        if chamber in self._phase2_pending:                   # 표본 충분해지면 제안(Review1 재시도)
            self._try_phase2_proposal(chamber)
        if self.get_phase(chamber) is not Phase.NORMAL:       # TSR-0005 ② — 억제 중인 챔버만
            self._note_qual_pending(chamber)                  #   방치 여부 확인(2단 throttle 내장)
        self._trigger_periodic_recalc(chamber)                # Step7 D1 — 정기 리캘리 텀블링 트리거

    def _trigger_periodic_recalc(self, chamber: str) -> None:
        """정기 리캘리 텀블링 트리거 (Step7 D1) — 매 wafer 카운터 +1, 스태거 임계 도달 시 발동·0 리셋.

        보류된 리로드 재시도(D5·최소 간격)도 겸한다. kill switch off·engine 미주입이면 트리거만 skip.
        스태거(D6-c)는 첫 발동만 챔버별 지연(fleet median 동시 붕괴 방지) — 이후는 interval 그대로.
        """
        self._retry_reload_if_pending()
        if not self._periodic_enabled or self.engine is None:
            return
        self._recalc_count[chamber] = self._recalc_count.get(chamber, 0) + 1
        chambers = sorted({gk[0] for gk in self.limits})      # DB 유래·하드코딩 없음
        threshold = stagger_threshold(chambers, chamber, self._recalc_interval,
                                      first_fired=self._first_fire_done.get(chamber, False))
        if self._recalc_count[chamber] >= threshold:
            self._recalc_count[chamber] = 0
            self._first_fire_done[chamber] = True
            self._maybe_periodic_recalc(chamber)

    def _eval_shadow(self, chamber: str, gk: tuple, cur: dict, proposal):
        """needs_approval 소급 채점 (Step7b D1) — DB 미접촉·순수. 실패·UNKNOWN·소표본 → None(NULL).

        `_shadow_buf`(최근 30장 Point)에서 이 그룹 값을 뽑아 구/신 관리선으로 replay(evaluate_shadow).
        `cur`는 **loader dict**(D2 — `_current()` 행 금지: ucl/lcl 없어 KeyError). new는 `**cur`
        오버레이라 limit_version·method 자동 승계. verdict는 게이트 아님(D5) — 값만 반환(없으면 None).
        승인 화면에 "이 재설정 시 오탐 N% 감소"를 붙여 HITL 판단에 근거를 준다.
        """
        try:
            pts = [p for w in self._shadow_buf.get(chamber, ()) for p in w
                   if p.group_key == gk and p.value is not None and p.value == p.value]  # None·NaN 제외
            if len(pts) < _SHADOW_MIN_POINTS:
                return None
            if len({p.limit_version for p in pts}) > 1:      # 창 내 버전 혼재 — replay 잣대 불일치
                logger.warning("shadow 창에 limit_version 혼재 — 채점 skip: %s", gk)
                return None
            old_l = {gk: cur}
            new_l = {gk: {**cur, "center": proposal.new_center, "ucl": proposal.new_ucl,
                          "lcl": proposal.new_lcl, "sigma": proposal.new_sigma}}
            r = evaluate_shadow(old_l, new_l, pts, self._shadow_cfg, group_key=gk)
            return None if r["verdict"] == "UNKNOWN" else r["false_alarm_reduction_pct"]  # D4 UNKNOWN=NULL
        except Exception:                                    # noqa: BLE001 — 컬럼 NULL로 진행(무크래시)
            logger.exception("shadow 채점 실패 — NULL로 진행: %s", gk)
            return None

    def _maybe_periodic_recalc(self, chamber: str) -> None:
        """정기 자동 리캘리브레이션 발동 (Step7 D4) — **전용 헬퍼**(인라인 금지).

        🔴 `_process_wafer`에 인라인하면 `return`이 collector·`_persist_tttm`을 스킵하는데 예외는
        안 새서 `_commit`은 실행 → 오프셋 커밋 → 리캘리 실패가 그 wafer의 alert를 조용히 삼킨다.
        게이팅(G1·G3)·조회·recompute·apply를 **전부 try 안**에서(예외가 오프셋 커밋을 못 막게).

        G1(NORMAL 아님)·G3(미종결 incident)면 skip. 챔버 그룹을 단일 트랜잭션으로 처리 —
        auto_applied만 적용(상한 내·헌법 1-1 예외), needs_approval은 PROPOSED 기록(승인 하류·
        비-Incident 직접 폴링). G2로 자기 경로 중복 PROPOSED 그룹은 건너뛴다. auto 1건↑이면
        커밋 후 내부 리로드(D5·fdc.correction 미발행).
        """
        if self.get_phase(chamber) is not Phase.NORMAL:       # G1 — 억제/광폭 구간은 정기 리캘리 안 함
            return
        buf = self._recalc_buf.get(chamber)
        if not buf or self._recalc_cfg is None or self.tttm is None:
            return                                            # 표본 없음·미배선(startup 전)
        per_group: dict = {}                                  # 도착순 {gk:val} deque → 그룹별 값 리스트
        for row in buf:
            for gk, val in row.items():
                per_group.setdefault(gk, []).append(val)
        any_auto = False
        blocked = False                                       # D2 — G2/G3로 하나도 못 돌린 봉쇄 상태
        try:
            with self.engine.begin() as conn:                 # 챔버 그룹 원자적
                if _incident_blocked(conn, chamber):          # G3 — 미종결 incident(관측 창 오염 방지)
                    blocked = True
                else:
                    proposed = _proposed_groups(conn, chamber)   # G2 — 자기 경로 중복(1쿼리)
                    ran = 0
                    for gk, values in per_group.items():
                        if gk[1:] in proposed:                # G2 그룹 skip
                            continue
                        current = self.limits.get(gk)
                        if current is None:
                            continue
                        proposal = recompute_group(
                            gk, values, current, reset_ref(conn, gk, self.tttm.k_sigma),
                            resolution_for(self._resolutions, gk), self._recalc_cfg)
                        if proposal.decision == "auto_applied":
                            apply_auto(conn, proposal)
                            any_auto = True
                        elif proposal.decision == "needs_approval":
                            far = self._eval_shadow(chamber, gk, current, proposal)  # Step7b — 소급 채점(순수)
                            record_proposed(conn, proposal, shadow_far_pct=far)      # PROPOSED 기록(승인 하류)
                        ran += 1
                    if ran == 0 and proposed:                 # 대상 전부 G2로 막힘(진행 0)
                        blocked = True
        except Exception:                                     # noqa: BLE001 — 오프셋·SPC·alert 무영향
            logger.exception("정기 리캘리 실패 — skip: %s", chamber)
            return
        self._note_gate_block(chamber, blocked)               # D2 — 장기 봉쇄 방치 경고(관측 전용)
        if any_auto:
            self._reload_after_commit()                       # 커밋 후 내부 리로드(baseline 재빌드 겸)

    def _note_qual_pending(self, chamber: str) -> None:
        """미확정 Qual 방치 경고 (TSR-0005 이관 ② · 관측 전용·판정 미개입).

        **왜 필요한가**: PM(pm_count↑) → `PHASE_0`(알람 억제, 설계 의도) → Qual 5장 → 판정
        `PENDING` → **엔지니어가 S7 에서 확정할 때까지 탈출 경로가 없다**(헌법 1-1 HITL 하류).
        확정을 잊으면 그 챔버는 **무기한 무음**이 된다 — 실측 2026-08-07~09 에 전 챔버가 28시간
        갇혔고 `quals` 208행 중 confirmed 0 이었다. 예외·ERROR 는 한 건도 없었다.

        **자동 해제는 하지 않는다** — 억제는 안전장치다. 시간으로 풀면 새 레짐이 확립되지 않은
        상태에서 알람이 재개돼 오탐이 쏟아진다(헌법 1-1 · TSR-0005 기각안 3종).

        **경과는 DB 에서 잰다** — phase 는 인메모리라 재시작마다 리셋되지만 `quals.created_at`
        은 남는다. `_gate_block_elapsed_sec` 와 같은 이유이고 같은 방식이다(#137 패턴).

        throttle 2단 — 순서가 중요하다(`_note_gate_block` 과 동일):
          ① 재알림 게이트(`_qual_pending_warned`) — 최근 경고했으면 **DB 재조회까지 생략**
          ② 재조회 게이트(`_qual_pending_checked_at`) — 매 wafer 조회 방지

        **호출 지점이 매 wafer 인 이유**: 정기 리캘리 경로(`_maybe_periodic_recalc`)에 붙이면
        스태거 임계(수백 wafer)마다만 불려 **며칠에 한 번** 뜬다 — 28시간 무음을 잡자고 만든
        경고가 그 주기로는 무의미하다(2026-08-10 라이브 확인에서 0건으로 드러남). 매 wafer
        호출이어도 DB 부하는 ②가 막는다(챔버당 10분에 1회).
        """
        now = self.clock()
        warned_at = self._qual_pending_warned.get(chamber)
        if warned_at is not None and (now - warned_at) < _QUAL_PENDING_REWARN_SEC:
            return                                # 최근 경고함 — 재알림 간격 미달(재조회도 생략)
        last = self._qual_pending_checked_at.get(chamber)
        if last is not None and (now - last) < _QUAL_PENDING_RECHECK_SEC:
            return                                # 재조회 간격 미달
        self._qual_pending_checked_at[chamber] = now
        try:
            with self.engine.connect() as conn:
                row = conn.execute(_QUAL_OLDEST_PENDING_SEC, {"ch": chamber}).first()
        except Exception:                         # noqa: BLE001 — 관측 실패가 판정을 막지 않는다
            logger.debug("미확정 Qual 조회 실패 — skip: %s", chamber, exc_info=True)
            return
        if row is None or row[0] is None:         # PENDING 없음 = 방치 아님(Phase 1·2 등 다른 사유)
            self._qual_pending_warned.pop(chamber, None)
            return
        elapsed, pending = float(row[0]), int(row[1])
        if elapsed < _QUAL_PENDING_WARN_SEC:
            return
        self._qual_pending_warned[chamber] = now
        # ASCII 토큰(`qual_pending`)은 의도 — 한글만이면 인코딩 깨진 파이프에서 grep 이 조용히
        #   0건을 돌려준다(헌법 7장 · #133 선례).
        logger.warning("qual_pending — 미확정 Qual %d건이 %.1f시간째 대기(chamber=%s). "
                       "이 챔버는 그동안 알람을 내지 않는다(Phase %s). "
                       "자동 해제 안 함(헌법 1-1) — S7 에서 조용/요란 확정 필요",
                       pending, elapsed / 3600.0, chamber, self.get_phase(chamber).value)

    def _note_gate_block(self, chamber: str, blocked: bool) -> None:
        """G2/G3 장기 봉쇄 방치 가드 (Step7 D2·관측 전용·판정 미개입).

        봉쇄가 `_GATE_STALE_WARN_SEC` 초과 지속되면 WARNING. **자동 해소는 하지 않는다**
        (헌법 1-1 — 승인 없이 관리선 못 바꿈). 봉쇄 풀리면 추적·경고를 리셋한다.
        경고 목적: PROPOSED 미승인(X5)·미종결 incident로 챔버가 리캘리를 오래 못 받으면 관리선이
        노후되므로, 엔지니어가 조기 포착·조사하게 한다.

        **재알림 (2026-08-09 · #137 회신 → PM 확정)**: 구 동작은 **챔버당 1회 throttle** 이었다.
        그러면 자동 해소가 없는 **영구 봉쇄가 시간이 지나며 조용해진다** — `#128` 지적 4 에서
        커밋 정체에 대해 지적받은 것과 같은 성질이다. `_GATE_REWARN_SEC`(24h) 마다 재경고한다.
        간격이 `_STALL_REWARN_SEC`(300s)보다 훨씬 긴 것은 의도다(상수 주석 참조).

        **throttle 2단 구조** — 순서가 중요하다:
          ① 재알림 게이트(`_gate_warned`) — 최근 경고했으면 **DB 재조회까지 생략**
          ② 재조회 게이트(`_gate_checked_at` · `_GATE_STALE_RECHECK_SEC`) — 매 wafer 조회 방지
        ①이 앞이라 **재경고를 넣어도 DB 조회 빈도는 늘지 않는다**(로그만 다시 찍힌다).
        """
        if not blocked:
            self._gate_blocked_since.pop(chamber, None)
            self._gate_checked_at.pop(chamber, None)
            self._gate_warned.pop(chamber, None)
            return
        now = self.clock()
        warned_at = self._gate_warned.get(chamber)
        if warned_at is not None and (now - warned_at) < _GATE_REWARN_SEC:
            return                                # 최근 경고함 — 재알림 간격 미달(재조회도 생략)
        last = self._gate_checked_at.get(chamber)
        if last is not None and (now - last) < _GATE_STALE_RECHECK_SEC:
            return                                # 재조회 간격 미달 — 매 wafer DB 조회 방지
        self._gate_checked_at[chamber] = now
        elapsed = self._gate_block_elapsed_sec(chamber)
        if elapsed is None:                       # DB 미배선·조회 실패 → 구 동작(메모리) 폴백
            since = self._gate_blocked_since.setdefault(chamber, now)
            elapsed = now - since
        if elapsed >= _GATE_STALE_WARN_SEC:
            self._gate_warned[chamber] = now       # 재알림 기준 시각(구 set → 주기 재경고)
            # 메시지 앞 ASCII 토큰(`gate_stale`)은 의도적이다 — 한글만 있으면 콘솔 인코딩이
            #   깨진 파이프에서 grep 이 조용히 0건을 돌려준다(헌법 7장 · #133 선례).
            logger.warning("gate_stale — 정기 리캘리 장기 봉쇄 %.1f일째 방치"
                           "(G2 PROPOSED 미승인/G3 미종결 incident). "
                           "자동 해소 안 함(헌법 1-1) — 조사 필요: %s", elapsed / 86400.0, chamber)

    def _gate_block_elapsed_sec(self, chamber: str) -> float | None:
        """봉쇄 지속 시간(초) — **DB 기준**. 조회 불가면 None(호출자가 메모리로 폴백).

        🔴 **왜 DB 인가 (2026-08-08 신설)**: 구현은 `self._gate_blocked_since`(프로세스 메모리)로
        쟀는데, 그러면 **재시작마다 0 으로 리셋**돼 3일 경고가 사실상 영영 뜨지 않는다.
        실측: 4챔버 전부 미종결 incident 302건으로 영구 봉쇄 상태였는데 경고는 **0건**이었다
        (개발 중 consumer 를 하루에도 몇 번씩 재시작했다). 운영에서도 배포·크래시·리밸런스
        한 번이면 같은 일이 난다 — **영구 차단을 알려줄 유일한 장치가 재시작에 지워진다.**
        헌법 7장 "가드를 프로세스 메모리에 두지 말 것"과 같은 계열이다.

        G2·G3 중 **더 오래된 쪽**을 쓴다 — 둘 다 막고 있으면 방치 기간은 긴 쪽이다.
        조회 실패는 삼킨다(관측이 리캘리를 막으면 안 된다). 읽기 전용.
        """
        if self.engine is None:                   # 미배선(단위 테스트 등) — 폴백 신호
            return None
        try:
            with self.engine.connect() as conn:
                ages = [conn.execute(q, {"ch": chamber}).scalar()
                        for q in (_G3_OLDEST_OPEN_SEC, _G2_OLDEST_PROPOSED_SEC)]
        except Exception:                         # noqa: BLE001 — 관측 실패가 판정을 막지 않는다
            logger.exception("gate_stale — 봉쇄 경과 조회 실패, 메모리 폴백: %s", chamber)
            return None
        vals = [float(a) for a in ages if a is not None]
        return max(vals) if vals else None        # 전부 NULL = 봉쇄 사유 미확인 → 폴백

    def _try_phase2_proposal(self, chamber: str) -> None:
        """PHASE2_SIGNAL 후 정착 표본 충분해지면 **firm 관리선만** 제안(record_proposed) — C-2.

        스냅샷·Y는 승인 대상 아닌 파생 참조라 apply 시점 재계산(firm 후속). 표본 부족이면 대기
        (다음 wafer 재시도, Review1). 1회 제안 후 pending 해제(승인 대기). engine 미주입=skip.
        """
        if self.engine is None:
            return
        sample = select_establishment_sample(
            list(self._backward.get(chamber, [])), self.establish_cfg,
            settled_at=self._settled_cutoff(chamber))         # B6-3-e — 조기 정착 컷오프
        if sample is None:
            return                                            # 버퍼 부족 → 대기
        if not self._converged.get(chamber, True):            # B6-3-e — 상한 폴백(미수렴) 승인자 경고
            logger.warning("Phase2 재수립 제안(미수렴 폴백 converged=False): %s cutoff=%s — 승인 검토 주의",
                           chamber, self._settled_cutoff(chamber))
        proposed = False
        with self.engine.begin() as conn:
            for gk, vals in sample.items():
                cur = self.limits.get(gk)
                if cur is None:
                    continue
                proposal = establish_group(gk, vals, cur, self.establish_cfg)
                if proposal.decision == "needs_approval":     # 신규수립=항상 승인(무상한)
                    record_proposed(conn, proposal,           # B6-3-e — 미수렴 폴백 플래그 영속(승인자 경고)
                                    settle_converged=self._converged.get(chamber))
                    proposed = True
        # #6 — 충분 표본으로 시도했으면 재시도 종료(전 그룹 no_change라도 무한 재시도 방지)
        self._phase2_pending.discard(chamber)
        if proposed:
            logger.info("Phase 2 재수립 제안 기록(관리선): %s", chamber)
        else:
            logger.warning("Phase 2 재수립 제안 0건(전 그룹 no_change/current 부재) — 재시도 종료: %s",
                           chamber)

    _RECENT_PREDS = text(
        "SELECT predicted_c65 FROM wafer_predictions WHERE chamber_id = :ch "
        "ORDER BY created_at DESC, id DESC LIMIT :n")

    def _read_recent_predictions(self, conn, chamber: str) -> list:
        """Y-임계용 최근 예측분포 — `wafer_predictions` 최근 N행 read (C-2 ⓑ, 누수 안전 과거값)."""
        vals = conn.execute(self._RECENT_PREDS,
                            {"ch": chamber, "n": self.establish_cfg.rolling_window_n}).scalars().all()
        return [float(v) for v in vals if v is not None]

    def _maybe_firm_followup(self, chamber: str, trigger_type: str | None) -> None:
        """firm correction 적용 후 후속 — **fallback 경로**(ChamberRequalified 미발행 시, 결정6).

        `_requalified_live=True`면 ChamberRequalified가 후속을 구동하므로 여기선 no-op(이중 실행
        방지·전 그룹 후 1회 정확성). False(발행 전)면 `trigger_type='incident'` correction을 완료
        경계로 삼아 후속 구동(#4 오트리거 방지). 실제 후속은 `_run_firm_followup` 공용.

        ⚠️ **fallback 한계**: 다중 group correction 중 첫 건이 후속·NORMAL 전환 → crash-mid-후속
        재시작 시 잔여 PROPOSED로 AWAITING 재파생돼 Y 중복 가능(비멱등). 실 ChamberRequalified
        (전 그룹 후 1회)가 근본 해결 → PM 발행 후 `_requalified_live=True`로 fallback 제거.
        """
        if self.provisional is None or chamber is None:
            return
        if self._requalified_live:                            # 이벤트가 구동 → fallback OFF
            return
        if trigger_type != "incident":                        # #4 — firm 재수립 correction만
            return
        self._run_firm_followup(chamber)

    def _tally_verify(self, chamber: str, flagged: bool) -> None:
        """firm 후 forward 오탐 tally(결정3 라이브) — N장 채워지면 절대 오탐율 판정·정리."""
        t = self._verify[chamber]
        t[0] += 1
        if flagged:
            t[1] += 1
        if t[0] >= self._verify_n:
            pct = t[1] / t[0] * 100.0
            verdict = "PASS" if pct <= self._verify_max_pct else "FAIL"
            logger.info("verify forward %s: %d/%d 위반(%.1f%%, 상한 %.1f) → %s",
                        chamber, t[1], t[0], pct, self._verify_max_pct, verdict)
            del self._verify[chamber]

    def _handle_agent(self, row: dict) -> None:
        """fdc.agent 이벤트 라우팅(B6-3-c 결정4) — 계약 §8-B 스키마, 얇은 파서 경계로 정제.

        `QualVerdictConfirmed`(verdict enum 'loud'/'quiet')→가한계 전환, `ChamberRequalified`
        →firm 후속(c-5). 그 외(Brief 등)는 event_type 필터로 무시(§8-B 관례). 발행 미구현
        구간엔 dev mock이 이 스키마로 주입한다. on_qual_verdict는 phase 가드로 멱등(재전달 안전).
        """
        if self.provisional is None:
            return
        event_type = row.get("event_type")
        chamber = row.get("chamber_id")
        if event_type == "QualVerdictConfirmed":
            verdict = row.get("verdict")
            if chamber is None or verdict not in ("loud", "quiet"):   # 계약 enum 정제
                logger.warning("QualVerdictConfirmed 스키마 오류 — skip: %r", row)
                return
            self._confirm_qual(row, chamber, verdict)         # B5-2b #4·결정9 — quals 확정(감사 정본)+리베이스 먼저
            self.provisional.on_qual_verdict(chamber, verdict)  # 인메모리 가한계 전환 — 예외 나도 감사 정본은 이미 남음
        elif event_type == "ChamberRequalified":
            if chamber is None:
                logger.warning("ChamberRequalified chamber_id 부재 — skip")
                return
            self._on_chamber_requalified(chamber)             # firm 후속(아래)

    def _on_chamber_requalified(self, chamber: str) -> None:
        """재수립 완료(ChamberRequalified) → firm 후속 구동(결정6 primary 경로).

        오케스트레이터가 **전 그룹 firm 적용 후 1회** 발행하는 완료 신호. 이게 후속(스냅샷·Y·
        TTTM 복귀·verify)을 돌린다 — correction-apply fallback보다 정확(조기 fleet 재참여 없음).
        `_requalified_live=True`일 때만 의미(fallback OFF). phase 게이트로 멱등(재전달 안전).
        """
        if chamber is not None:
            self._run_firm_followup(chamber)

    def _run_firm_followup(self, chamber: str) -> None:
        """firm 재수립 후속 블록(공용) — AWAITING_PHASE2 게이트·챔버당 1회.

        스냅샷 리베이스·Y-임계(wafer_predictions read) write + TTTM 복귀(on_firm_established)
        + verify forward tally 시작. on_firm_established가 NORMAL 전환 → 재호출·잔여 correction은
        게이트 차단(비멱등 Y write 중복 방지). ChamberRequalified 경로·correction fallback 공유.
        """
        if self.provisional is None:
            return
        if self.provisional.get_phase(chamber) is not Phase.AWAITING_PHASE2:
            return                                            # 재수립 아님·이미 처리
        sample = select_establishment_sample(
            list(self._backward.get(chamber, [])), self.establish_cfg,
            settled_at=self._settled_cutoff(chamber))         # B6-3-e — 조기 정착 컷오프
        if sample:
            with self.engine.begin() as conn:
                stats = rebase_snapshot(chamber, sample, self.limits, self.establish_cfg)
                if stats:
                    sid = f"QUAL-{chamber}-firm-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
                    write_sensor_stats(conn, chamber, stats,
                                       qual_id="regime-reestablish", snapshot_id=sid)
                recipe = next(iter(sample))[1]                # group_key = (ch,recipe,step,win,sn)
                y = compute_y_threshold(chamber, recipe,
                                        self._read_recent_predictions(conn, chamber), self.establish_cfg)
                if y is not None:
                    write_y_threshold(conn, y)
        else:                                                 # 재시작 등 버퍼 유실 — 정확 로깅(#2)
            logger.warning("firm 후속: backward 버퍼 부족(재시작?) — 스냅샷·Y 스킵(firm 관리선은 적재됨): %s",
                           chamber)
        _yc = getattr(self, "y_cache", None)                  # M4 — 재수립으로 y_thresholds 갱신 →
        if _yc is not None:                                   #   캐시 무효화(다음 crazy/Qual P99 재조회). 미주입=no-op.
            _yc.invalidate(chamber)
        self.provisional.on_firm_established(chamber)         # TTTM 복귀·NORMAL (a 훅)
        self._verify[chamber] = [0, 0]                        # forward tally 시작(seen, flagged)
        self._backward.pop(chamber, None)
        self._phase2_pending.discard(chamber)
        logger.info("firm 재수립 후속: %s (스냅샷·Y=%s, verify forward %d장 tally 시작)",
                    chamber, "기록" if sample else "스킵", self._verify_n)

    def _persist_tttm(self, tttm_obj: dict | None) -> None:
        """TTTM rollup을 tttm_comparisons에 라이브 적재 (best-effort — B4-2 라이브 배선).

        writer(`write_comparisons`)의 "commit은 호출자" 설계에서 라이브 commit 호출자가
        바로 여기다(`engine.begin()`). persist는 **DB만** 쓰고 엔진 in-memory 상태는 안
        건드리므로(feed와 별개 트랙), 적재 실패·중복 행이 판정을 오염시키지 않는다.

        L1 best-effort — write 실패는 자체 삼킴(로그만), 오프셋·collector·feed 무영향.
        L2 — engine 미주입(순수 테스트)이면 skip(운영 `main`선 항상 주입). L5 append-only.
        **재전달 중복 행 dedup (B7-1 D13 재검토 — RTD #103 의존)**: 구 D13은 *"tttm_comparisons는
        프로덕션 SELECT 0건이라 중복 무해"* 였으나 **RTD(#103)가 `reference_suspect` 연속 K 런을
        조회**하므로 중복 행이 그 run-length를 부풀려 정지 「제안」 오탐을 낼 수 있다 → 무해 전제 폐기.
        - **동일 프로세스 재전달(ⓑ)**: `_process_wafer`의 D15 no-op(`if dup: return`)이 여기(persist)
          **호출 자체를 스킵**해 중복 행이 안 생긴다. ✅
        - **크래시 재시작(ⓐ)**: `_last_wafer`가 인메모리라 D15가 못 막는다 → **§6-2 durable dedup
          (offset 정밀 + 영속 high-water mark) 필수**로 격상. ⚠️ tttm_comparisons엔 `wafer_id`가
          없어(챔버 롤업) 행 단위 dedup 불가 → **웨이퍼 처리 단위**로 막아야 한다.
        """
        if not tttm_obj:                          # 평가불가(None/{}) → 적재할 것 없음
            return
        if self.engine is None:                   # L2 — 운영선 안 일어남(main 주입)
            logger.debug("engine 미주입 — tttm 적재 skip")
            return
        try:                                      # L1 best-effort
            rows = rollups_to_rows([tttm_obj])    # None 드롭(이중 안전) + 8컬럼 row화(suspect_sensors 포함)
            with self.engine.begin() as conn:     # 호출자=commit (writer는 no-commit)
                write_comparisons(conn, rows)     # append-only INSERT
        except Exception:                         # noqa: BLE001 — 삼킴 → 파이프라인·alert 무영향
            logger.exception("tttm 적재 실패 — skip(파이프라인·alert 무영향)")

    # ── B5-1a 라이브: fdc.correction → 승인 적용 → 리로드 ──────────────
    def _handle_correction(self, row: dict) -> bool:
        """승인된 limit correction을 control_limits에 적용 + 엔진 리로드.

        반환 = 오프셋 커밋 여부. 성공·의도적 skip(recipe/correction_id 부재)·**APPLY_FAILED**
        → True(커밋), apply **예외** → False(커밋 보류 → 재전달 재적용; `apply_approved` 멱등) — ①(b).

        **수정승인 §7 — 적용여부(bool) 분기**: `apply_approved`가 False(APPLY_FAILED·no-op)면
        **reload·firm 후속을 건너뛴다.** 안 그러면 firm 미적용인데 stale 관리선으로 후속(스냅샷·
        Y·on_firm_established→NORMAL·verify)이 발화 → **잘못 졸업**. APPLY_FAILED status는 트랜잭션이
        커밋하고 오프셋도 커밋(재시도 X — 영구 불량). DB 예외만 return False(rollback+재시도).
        """
        if row.get("correction_type") != "limit":  # recipe = 시뮬레이터 몫 → skip
            return True
        cid = row.get("correction_id")              # ② gate (PM 페이로드 요청1 — §12-4)
        if cid is None:                             # 과도기: PM 배선 전 = 아직 적용 불가
            logger.warning("correction_id 부재 — skip (PM 페이로드 대기)")
            return True                             # skip: 커밋(재시도 X — 무한루프 방지)
        try:
            chamber, trigger = None, None
            with self.engine.begin() as conn:       # 한 트랜잭션 (commit=begin, apply "commit=호출자")
                applied = apply_approved(conn, cid)  # 적용됨(True)/APPLY_FAILED·no-op(False)
                if applied and self.provisional is not None:  # firm 후속 게이트용(적용 성공 시만)
                    r = conn.execute(text(          # 챔버 + trigger_type(#4 오트리거 방지)
                        "SELECT chamber_id, trigger_type FROM limit_corrections "
                        "WHERE correction_id = :cid"), {"cid": cid}).mappings().first()
                    if r is not None:
                        chamber, trigger = r["chamber_id"], r["trigger_type"]
            if applied:                             # 미적용(APPLY_FAILED)이면 reload·후속 skip(§7)
                limits, _ = control_limits_loader.load(self.engine)   # ③ 커밋 후 전체 리로드
                self.reload_limits(limits)          # whitelist는 startup-only (운영 중 불변)
                self._maybe_firm_followup(chamber, trigger)   # B6-3-c 결정6 — incident·AWAITING만
            return True                             # 성공·APPLY_FAILED 둘 다 커밋(재시도 X)
        except Exception:                           # noqa: BLE001 — 루프 생존 (헌법 6-2)
            logger.exception("correction 적용 실패 — 커밋 보류(재전달 재시도): %s", cid)
            return False

    def _commit_msg(self, msg) -> None:
        """correction용 직접 오프셋 커밋 (M1).

        버퍼용 `_commit(buf)`과 별개다 — correction은 버퍼링 없이 즉시 처리하고, fdc.raw와
        다른 토픽/파티션이라 `offset_factory`로 토픽별 `TopicPartition`을 만들어 커밋한다.
        """
        self.consumer.commit(
            offsets=self.offset_factory(msg.topic(), msg.partition(), msg.offset()),
            asynchronous=False)

    def get_phase(self, chamber: str) -> Phase:
        """챔버 가한계 단계 — **B4-1 억제 게이트 입력**(B6-3-c 결정5). provisional 미주입=NORMAL.

        c는 위반을 기록만 하고 alert 억제 집행은 B4-1이 이 phase를 읽어 수행한다(헌법 1-2 —
        fdc.alert 발행권=B4-1). Phase 0=억제, Phase 1=광폭(위반 자체 감소로 부분 보호).
        """
        if self.provisional is None:
            return Phase.NORMAL
        return self.provisional.get_phase(chamber)

    def reload_limits(self, limits: dict) -> None:
        """관리선 리로드 seam — B4-3 재산정·B5-1a 승인으로 운영 중 바뀐다.

        실제 트리거(`fdc.correction` 구독)는 **B5-1a 이월**. whitelist는 startup-only라
        건드리지 않는다 — 감시 대상 집합은 운영 중 불변(결정 5).
        """
        self.limits = limits
        self.nelson.limits = limits
        self.tttm.limits = limits
        self._rebuild_baseline()                 # Step7 D6-a-2 — 리로드 전 경로 baseline 전량 재빌드
        logger.info("관리선 리로드: %d 그룹", len(limits))

    def _rebuild_baseline(self) -> None:
        """TTTM baseline 맵 전량 재빌드 (Step7 D6-a·D6-a-2 v2.3) — startup·리로드 모든 경로 공용.

        각 그룹의 `reset_ref`(레짐 수립 행)을 모아 `tttm.set_baseline`으로 주입 →
        sigma_ref·center_ref·reference_id가 periodic auto·provisional에 안 흔들린다(B4-2 결정 1).
        best-effort(실패=fallback(활성 행) 유지·무크래시). tttm/engine 미주입이면 no-op.
        리로드는 드문 이벤트라 전량 조회 부담 미미(reset_ref 추가 쿼리는 Step7 자기 조회와 별개나
        발동 빈도가 낮아 수용).
        """
        if self.tttm is None or self.engine is None:
            return
        try:
            with self.engine.connect() as conn:
                m = build_baseline_map(
                    list(self.limits), lambda gk: reset_ref(conn, gk, self.tttm.k_sigma))
            self.tttm.set_baseline(m)
        except Exception:                        # noqa: BLE001 — best-effort(헌법 6-2)
            logger.exception("baseline 재빌드 실패 — TTTM fallback(활성 행) 유지")

    def _reload_after_commit(self) -> None:
        """정기 리캘리 auto 적용 후 내부 리로드 (D5) — fdc.correction 미발행(B는 구독자·자기 자신).

        위험한 건 reload_limits(속성 대입)가 아니라 그 앞의 load() DB read다. B5-1a는 오프셋
        보류로 자가 치유되지만 Step 7은 그 경로가 없어 실패 시 `_reload_pending`으로 표시하고
        `_retry_reload_if_pending`이 최소 간격 후 재시도한다(매 wafer 재시도 금지 — DB 다운 시
        wafer마다 TCP 타임아웃 → Kafka 리밸런스).
        """
        try:
            limits, _ = control_limits_loader.load(self.engine)
            self.reload_limits(limits)
            self._reload_pending = False
        except Exception:                        # noqa: BLE001 — 루프 생존(헌법 6-2)
            self._reload_pending = True
            self._last_reload_try_at = self.clock()
            logger.exception("커밋 성공·리로드 실패 — DB=신규/메모리=구버전. 재시도 예약")

    def _retry_reload_if_pending(self) -> None:
        """보류된 리로드를 최소 간격(_RELOAD_RETRY_MIN_SEC) 경과 후에만 재시도 (D5).

        매 wafer 호출되지만 실제 DB read는 간격 게이트를 통과할 때만 — pool_pre_ping·
        connect_timeout 부재(seed_control_limits) 하에서 DB 다운 시 블로킹을 막는다.
        """
        if not self._reload_pending:
            return
        if self.clock() - self._last_reload_try_at < _RELOAD_RETRY_MIN_SEC:
            return
        self._reload_after_commit()


def _bootstrap() -> str:
    """Kafka bootstrap 주소를 환경에서 읽는다. 미설정 시 명시적 실패 (헌법 6-1).

    `.env` 로딩은 조립 지점(`main`) 책임이다 — 여기서 하면 환경 검증이 파일에 가려진다.
    """
    value = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
    if not value:
        raise RuntimeError("환경변수 KAFKA_BOOTSTRAP_SERVERS 미설정 — .env를 확인하세요")
    return value


def _require_whitelist(whitelist) -> None:
    """감시 화이트리스트 공집합이면 루프 진입 전 명시적 실패 (B6-2 G5 — 무음 정지 차단).

    시딩 부재/조용한 실패 시 whitelist가 비면 nelson.feed가 0회 돌아 WARNING도 없이 **무음
    정지**한다(§1-④). 이는 6-2의 "역직렬화 실패 시 개별 메시지 스킵"과 다른 층위 — 루프 진입
    **전** 부트스트랩 전제조건 검증이며, `_bootstrap`의 KAFKA 미설정 실패와 동일 관례다.
    (assert 금지 — `python -O`로 방어선 소실, 헌법 7장.)
    """
    if not whitelist:
        raise RuntimeError(
            "control_limits 감시 화이트리스트가 비어 있습니다 — 감시가 무음 정지합니다. "
            "seed_control_limits 를 먼저 실행하세요(compose: spc-seed)."
        )


def make_consumer(bootstrap: str, group: str = CONSUMER_GROUP,
                  auto_offset_reset: str = "latest"):
    """실제 confluent_kafka Consumer 생성 (lazy import — 순수 테스트 무의존).

    수동 커밋(`enable.auto.commit=False`) — flush된 웨이퍼까지만 커밋해 in-flight 웨이퍼
    유실을 막는다(M1, at-least-once).
    """
    from confluent_kafka import Consumer

    return Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": group,
        "auto.offset.reset": auto_offset_reset,
        "enable.auto.commit": False,
    })


def make_producer(bootstrap: str):
    """실제 confluent_kafka Producer 생성 (lazy import — 순수 테스트 무의존, M4).

    소유는 main — `finally` 에서 `producer.flush()` 로 graceful shutdown 한다(6-2). publisher 는
    받아서 send 만 하고 생명주기는 관리하지 않는다(D-11).
    """
    from confluent_kafka import Producer

    return Producer({"bootstrap.servers": bootstrap})


def _build_provisional(consumer, engine, limits):
    """실 seam으로 ProvisionalMode 조립 (B6-3-c) + 재시작 복구(결정2 ⓓ).

    writer=ProvisionalWriter(DB) · reload_fn=컨슈머 리로드(late binding) · tttm=컨슈머 엔진 ·
    persist=None(무저장) · **pm_logger=PmLogWriter(pm_log 정본)**(B6-3-f). 시작 시
    control_limits(provisional 활성)에서 phase 복구.
    """
    from src.agent_b_spc.pm_log_writer import (
        PmLogWriter, resolve_pm_log_path, resolve_pm_log_pending_cap, warn_if_shadow_copy)
    from src.agent_b_spc.provisional_mode import ProvisionalConfig, ProvisionalMode
    from src.agent_b_spc.provisional_seams import (
        ProvisionalWriter, derive_restore_state, derive_settle_sigma_ref)

    prov_cfg = ProvisionalConfig.load()
    # B6-3-f — pm_log 기입 seam. **config 키 부재가 컨슈머 기동을 죽이면 안 된다**(6-2 정신·
    #   pm_log는 A 피처 최신성 문제지 안전 경로가 아님) → 실패 시 pm_logger=None으로 계속.
    try:
        pm_log_path = resolve_pm_log_path()         # params `pm_log.path`(정본=루트)
        warn_if_shadow_copy(pm_log_path)            #   사본 split-brain 기동 가드(§3-5)
        # B7-1 D4 — cap은 **비필수 튜닝값**이라 키 부재로 pm_log 기입 전체를 끄지 않는다(리뷰 3b).
        #   path/사본 가드 실패(위)만 아래 except로 pm_logger=None. cap 실패는 여기서 기본값 폴백.
        try:
            cap_kw = {"pending_cap": resolve_pm_log_pending_cap()}
        except Exception:                           # noqa: BLE001
            cap_kw = {}                             # PmLogWriter 기본값(PENDING_CAP) 사용
            logger.warning("pm_log_pending_cap 해석 실패 — PmLogWriter 기본값 사용(pm_log 유지)")
        pm_log_writer = PmLogWriter(pm_log_path, **cap_kw)
        consumer.pm_log_writer = pm_log_writer      # poll 루프 flush_pending용 인스턴스(M1)
        pm_logger = pm_log_writer.append
        logger.info("pm_log 기입 경로(요란 확정 시 append, 단일 작성자=B): %s", pm_log_path)
    except Exception:                               # noqa: BLE001 — 기동 보호
        logger.exception("pm_log 경로 해석 실패 — 기입 비활성으로 기동(A 피처 갱신 중단)")
        pm_logger = None
    prov = ProvisionalMode(
        prov_cfg, limits=limits, writer=ProvisionalWriter(engine),
        reload_fn=lambda: consumer.reload_limits(control_limits_loader.load(engine)[0]),
        tttm=consumer.tttm, persist=None,
        pm_logger=pm_logger,                        # B6-3-f — 요란 확정 → pm_log 1건 기입
        whitelist=consumer.whitelist)               # B6-3-e — 정착 판정 대상=whitelist(준-꺼짐 배제, 리뷰3#1)
    with engine.connect() as conn:                  # 재시작 복구 (ⓓ 파생)
        state = derive_restore_state(conn, prov_cfg.seasoning_loud)
        # B6-3-e ① — PHASE_1(정착 대기) 챔버만 firm σ_ref 재파생(비활성 firm-baseline 행에서).
        #   광폭-active σ 오염 방어. AWAITING_PHASE2는 판정 종료라 불요. 미복원 그룹은 유예→1,000 폴백(안전).
        phase1 = [ch for ch, s in state.items() if s["phase"] == Phase.PHASE_1]
        prov.restore(state,
                     settle_sigma_ref=derive_settle_sigma_ref(conn, phase1, prov_cfg.k_sigma))
    return prov


def main() -> None:
    """서비스 진입점 — 관리선 로드 1회 → 엔진 → 가한계 조립 → 3토픽 구독 루프 (§4 startup)."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from src.agent_b_spc.seed_control_limits import make_engine

    load_dotenv()
    engine = make_engine()                          # M2 — load()·SpcConsumer 양쪽에 공유
    limits, whitelist = control_limits_loader.load(engine)
    _require_whitelist(whitelist)                    # G5 — 무음 정지 차단(시딩 전제 검증)
    # ChamberRequalified 발행 활성화 토글(데모/오케스트레이터 발행 시 true) — 결정6 모드
    requalified_live = os.environ.get("SPC_REQUALIFIED_LIVE", "false").lower() == "true"
    consumer = SpcConsumer(consumer=make_consumer(_bootstrap()), engine=engine,
                           limits=limits, whitelist=whitelist, requalified_live=requalified_live)
    consumer.provisional = _build_provisional(consumer, engine, limits)  # B6-3-c 배선
    consumer.establish_cfg = EstablishConfig.load()

    # Step7 — 정기 리캘리 startup 배선: 산식 cfg·σ-단위 자 분해능·TTTM baseline 전량 빌드(D6-a startup 1회).
    from src.agent_b_spc.initial_limits import load_resolutions
    consumer._recalc_cfg = RecalcConfig.load()
    _meta = Path(__file__).resolve().parent / "limits" / "initial_limits_v1_meta.json"
    try:
        consumer._resolutions = load_resolutions(_meta)
    except Exception:                                # noqa: BLE001 — 없으면 res_σ 하한 없이 진행
        logger.warning("resolutions 로드 실패 — res_σ 하한 없이 진행: %s", _meta, exc_info=True)
        consumer._resolutions = {}
    consumer._rebuild_baseline()                     # 활성 행==레짐 행인 현재는 등가(오늘 무영향)

    # M4 — alert seam 배선. 발행 스위치 기본 off(params `spc.publish_enabled`)라 dry-run(조립·적재만).
    from src.agent_b_spc.alert_seam import make_alert_collector
    from src.agent_b_spc.establish_engine import read_y_thresholds
    from src.common.context_score.config import ContextScoreConfig
    from src.common.context_score.publisher import (
        CrazyConfig, PublisherConfig, YThresholdCache, load_sensor_map)

    pub_cfg = PublisherConfig.load()

    def _read_y(chamber, recipe):                   # y_cache reader — read_y_thresholds({p95,p99}) 래핑
        with engine.connect() as conn:
            return read_y_thresholds(conn, chamber, recipe)

    consumer.y_cache = YThresholdCache(reader=_read_y, ttl_seconds=pub_cfg.y_threshold_cache_ttl_sec)
    producer = make_producer(_bootstrap())
    consumer.collector = make_alert_collector(
        consumer, engine, ContextScoreConfig.load(), CrazyConfig.load(), consumer.y_cache,
        producer=producer, pub_cfg=pub_cfg, sensor_map=load_sensor_map())
    try:
        consumer.run()
    finally:
        producer.flush()                            # graceful shutdown (6-2·D-11)


if __name__ == "__main__":
    main()
