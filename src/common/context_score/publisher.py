# =============================================================================
# publisher.py   —  [담당: B]  Step5 · Step5b
# =============================================================================
# 역할: 억제(suppression) 집행 후 fdc.alert 를 조립·발행하고, spc_violations 에
#       적재한다. B9 crazy wafer 판정(5b)도 여기서 처리.
#       (계획서 §2 다이어그램 · §3 Step5/5b)
#
# [Step5 흐름]
#   1) 억제 집행: get_phase() 로 현재 Phase 확인 → 억제 규칙 적용.
#   2) alert_id 채번:  포맷 ALERT-<YYYYMMDD>-<CHAMBER>-<SEQ>  (6-4).
#   3) fdc.alert 조립·발행.
#   4) spc_violations INSERT.
#
# [Step5b — B9 crazy wafer]
#   - 챔버별 y_thresholds 를 read (B5-1b) 하여 crazy 판정.
#
# [fdc.alert 토픽 계약]  (CLAUDE.md 2-1)
#   변경 금지 필드: wafer_id, chamber_id, rule_id, severity, context_score
#   → 이 필드명을 그대로 채운다. 이름 변경/삭제 금지.
#
# [단일 토픽 원칙]  (CLAUDE.md 1-2)
#   fdc.alert 는 유일한 이상 경보 채널. Agent 를 직접 호출하는 경로 금지 —
#   반드시 이 토픽으로만 발행. 구독자(C·D)는 fdc.alert 경유로만 트리거된다.
#
# [ID 채번 주체]  (CLAUDE.md 6-4)
#   correction ID 의 채번 주체는 B 엔진. C 리포트/승인 워크플로는 승계만.
#   alert_id 는 여기(B)서 채번.
#
# [규칙]
#   - Producer graceful shutdown·직렬화 실패 처리 (6-2).
#   - 데이터 누수 금지 (1-3). 매직넘버(억제 창·SEQ 규칙) → params.yaml.
#   - AI 가 승인 없이 실력치(control limit)를 직접 변경하는 코드 절대 금지 (1-1).
#     이 파일은 "경보 발행/적재"만 하며 한계선 변경은 하지 않는다.
#   - 센서 C코드 원칙 (6-4). docstring 필수. print 금지 → logging.
# =============================================================================
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from src.common.config_util import _key, _section   # 부재 시 명시 실패(헌법 6-1)
from src.common.context_score.markers import CRAZY_MARKER_RULE_ID

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "params.yaml"
SENSOR_MAP_PATH = Path(__file__).resolve().parents[3] / "config" / "sensor_map.yaml"

# fdc.alert 토픽명 — 계약 고정(헌법 2-1). config 로 빼지 않는다: 바뀌면 계약 위반이라
#   "튜닝 가능한 값"이 아니다(6-1 매직넘버 금지의 대상이 아닌 상수).
TOPIC_ALERT = "fdc.alert"

# B4-1 Step5b — y_thresholds 캐시 TTL backstop(리뷰①). 단일 consumer라 분산 stale 없음 —
#   out-of-consumer 재수립(정기 recalc·재Qual)·재시작 대비 짧은 backstop(prediction_cache TTL 선례).
#   params `spc.y_threshold_cache_ttl_sec` 로 **승격 완료**(2026-07-28, 계획서 §7) — 정본은 params 이고
#   seam(M4)이 `PublisherConfig.y_threshold_cache_ttl_sec` 를 주입한다. 이 상수는 직접 생성(테스트 등)
#   시의 기본값 fallback 으로만 남긴다.
_Y_CACHE_TTL_SEC = 600.0    # 10분


# =============================================================================
# B9 crazy wafer — 판정 코어 (detect only). 발행(마커 조립·적재)은 §7 step4~6 회신 대기.
# =============================================================================
@dataclass(frozen=True)
class CrazyConfig:
    """B9 crazy 판정 임계(주입식·params 매핑). 컷은 두 config의 곱이라 매직넘버 아님(6-1)."""

    predicted_c65_threshold: float   # y_thresholds 부재 시 fallback (spc.crazy_wafer.predicted_c65_threshold)
    anomaly_multiplier: float        # spc.crazy_wafer.anomaly_score_multiplier
    ae_anomaly_threshold: float      # ct.ae_anomaly_threshold (C6 — Qual 기준과 동일값 출발)
    # AE 포화 격리 (2026-08-11, PM 승인) — anomaly arm 게이팅. predicted arm 은 불변.
    use_anomaly_arm: bool = True     # spc.crazy_wafer.use_anomaly_arm (false = anomaly arm 미발동)

    @property
    def anomaly_cut(self) -> float:
        """anomaly arm 컷 = multiplier × C6 (=0.6, D2). C6 재튜닝 시 자동 추종."""
        return self.anomaly_multiplier * self.ae_anomaly_threshold

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "CrazyConfig":
        """params.yaml → CrazyConfig. 키 부재 시 매직넘버 대체 없이 명시 실패(6-1).

        crazy_wafer 임계는 `spc.crazy_wafer.*`, anomaly C6는 `ct.ae_anomaly_threshold`
        (라이브 config 실소속 — 스펙 표기 `qual.*`와 달리 실제는 ct 섹션). D2 컷=곱.
        """
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        spc = _section(cfg, "spc", path)
        ct = _section(cfg, "ct", path)
        cw = _key(spc, "crazy_wafer", "spc", path)
        return cls(
            predicted_c65_threshold=float(_key(cw, "predicted_c65_threshold", "spc.crazy_wafer", path)),
            anomaly_multiplier=float(_key(cw, "anomaly_score_multiplier", "spc.crazy_wafer", path)),
            ae_anomaly_threshold=float(_key(ct, "ae_anomaly_threshold", "ct", path)),
            #   기본 True — 키 부재(구 config)면 현행 동작 유지. 격리는 명시적으로만 켠다.
            use_anomaly_arm=bool(cw.get("use_anomaly_arm", True)),
        )


@dataclass(frozen=True)
class CrazyVerdict:
    """crazy 판정 결과 — 걸린 arm·값·컷을 담아 build_crazy_violation(step4)로 넘긴다."""

    pred_hit: bool
    an_hit: bool
    predicted_c65: Optional[float]
    anomaly_score: Optional[float]
    threshold: float          # predicted arm 비교 컷 (챔버×recipe P99 or 1572 fallback)
    anomaly_cut: float        # anomaly arm 컷 (=0.6)
    # Q2 — 위 `threshold` 의 출처 버전(`y_thresholds.threshold_version`). 마커 `limit_version` 에
    #   실린다. None = y_thresholds 행 부재(config fallback) 또는 anomaly-only → 센티넬로 대체.
    threshold_version: Optional[str] = None


def _num(x) -> bool:
    """int/float(비-bool)·유한값이면 True. 비수치·NaN·inf → False (total 판정, raise 금지)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _threshold_version(y_cache, chamber_id: str, recipe_id) -> Optional[str]:
    """y_cache 에서 활성 임계의 `threshold_version` 조회 (Q2).

    접근자가 없는 캐시 구현(구버전·테스트 fake)도 있으므로 `getattr` 로 관용 처리한다 —
    버전은 **기록용 부가 정보**라 없다고 판정을 막으면 안 된다(판정 컷은 이미 확보된 상태).
    """
    getter = getattr(y_cache, "threshold_version", None)
    if getter is None:
        return None
    try:
        return getter(chamber_id, recipe_id)
    except Exception:                              # noqa: BLE001 — 부가 정보 실패가 판정을 막지 않게
        logger.warning("threshold_version 조회 실패 — 센티넬로 진행: %s/%s",
                       chamber_id, recipe_id, exc_info=True)
        return None


def _anomaly_is_crazy(anomaly_score, cfg: CrazyConfig) -> bool:
    """anomaly arm 판정 — 컷 계산을 이 헬퍼에만 가둔다(형태 변경 국소화, D2).

    경계는 strict >(컷 자체는 미발동).

    **AE 포화 격리 (2026-08-11, PM 승인)**: `use_anomaly_arm: false` 면 이 팔을 통째로 끈다.
    라이브 `anomaly_score` 가 **98.2% 가 정확히 1.0**(서빙 번들의 학습 구간이 시뮬 발행 구간과
    어긋남)이라, 컷(=multiplier 3.0 × C6 0.2 = 0.6)을 상시 초과해 **wafer 의 40~57% 가 crazy**
    로 판정된다(실측). 그 알람들은 대표 위반이 없어 하류에서 `tuning=None` →
    `insufficient_evidence` 로 종결되므로, 신호는 없고 기록만 쌓인다.

    **컷 조정은 대안이 아니다** — 포화가 정확히 1.0 이라 1.0 미만 컷은 전부 발동하고
    1.0 초과 컷은 영구 미발동(= 잠금과 동일)이다. 그래서 값이 아니라 **스위치**로 둔다.

    Qual `use_ae_axis: false`(PM 2026-08-06) · RTD `exclude_ae_arm: true` 와 **같은 논리의
    세 번째 적용**이며, **AE 포화 해소 확인 시 되돌린다.** predicted arm(`> P99`)은 불변.
    """
    if not cfg.use_anomaly_arm:
        return False
    return _num(anomaly_score) and anomaly_score > cfg.anomaly_cut


def is_crazy(prediction, chamber_id, recipe_id, cfg: CrazyConfig, y_cache) -> Optional[CrazyVerdict]:
    """B9 crazy wafer 판정 (predicted arm OR anomaly arm) — total(무크래시).

    predicted arm: `predicted_c65 > 챔버×recipe P99`(y_cache 조회, 부재 시 config 1572 fallback).
    anomaly arm: `_anomaly_is_crazy`(컷 0.6). 둘 중 하나라도 걸리면 CrazyVerdict, 아니면 None.
    prediction None·비수치 값은 해당 arm False로 흡수하고 raise하지 않는다(§3 total). y_cache
    미주입(None)이어도 fallback 경로로 무크래시. 소비(마커 조립·발행)는 §7 step4~6(회신 대기).
    """
    if not isinstance(prediction, dict):
        return None
    pc = prediction.get("predicted_c65")
    an = prediction.get("anomaly_score")
    thr = y_cache.p99(chamber_id, recipe_id) if y_cache is not None else None
    ver = None
    if thr is None:
        thr = cfg.predicted_c65_threshold          # 1572 fallback (y_thresholds·recipe 부재)
    else:                                          # Q2 — 실제 컷의 출처 버전 승계(캐시 동일 행)
        ver = _threshold_version(y_cache, chamber_id, recipe_id)
    pred_hit = _num(pc) and pc > thr
    an_hit = _anomaly_is_crazy(an, cfg)
    if not (pred_hit or an_hit):
        return None
    return CrazyVerdict(
        pred_hit=pred_hit, an_hit=an_hit,
        predicted_c65=pc if _num(pc) else None,
        anomaly_score=an if _num(an) else None,
        threshold=float(thr), anomaly_cut=cfg.anomaly_cut,
        threshold_version=ver,
    )


class YThresholdCache:
    """(chamber, recipe) → {p95, p99} 캐시. 로컬 dict 무효화 + 짧은 TTL backstop (B4-1 Step5b 리뷰①).

    `reader(chamber, recipe) -> {"p95","p99"}|None` 주입(테스트=fake, 운영=read_y_thresholds+conn) —
    DB 세부에 결합하지 않는 순수 래퍼. p95·p99는 **같은 활성 행**이라 reader 1회 조회로 둘 다 캐시한다
    (DB 왕복 절약): `p99`가 is_crazy predicted arm 컷을, `p95`가 Qual/pred enrichment 임계를 공급한다.
    재수립(write_y_threshold) 시 `invalidate`로 즉시 무효화하고, 놓친 out-of-consumer 갱신은 TTL이
    backstop한다. miss(None)도 캐시해 반복 miss가 DB를 때리지 않되, 재수립 시 invalidate로 갱신을 본다.
    """

    def __init__(self, reader: Callable[[str, str], Optional[dict]],
                 ttl_seconds: float = _Y_CACHE_TTL_SEC,
                 clock: Callable[[], float] = time.time) -> None:
        self._reader = reader
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict[tuple, tuple] = {}       # (ch, rc) -> (rec{p95,p99}|None, fetched_at)

    def _fetch(self, chamber_id: str, recipe_id: Optional[str]) -> Optional[dict]:
        """활성 {p95, p99} 행 조회(캐시 우선). recipe 결측(None)이면 조회 안 함 → None."""
        if recipe_id is None:
            return None
        key = (chamber_id, recipe_id)
        hit = self._store.get(key)
        if hit is not None and (self._clock() - hit[1]) <= self._ttl:
            return hit[0]
        rec = self._reader(chamber_id, recipe_id)
        self._store[key] = (rec, self._clock())
        return rec

    def p99(self, chamber_id: str, recipe_id: Optional[str]) -> Optional[float]:
        """활성 P99(crazy predicted arm 컷). 행/​recipe 결측이면 None(호출부 1572 fallback)."""
        rec = self._fetch(chamber_id, recipe_id)
        return rec["p99"] if rec is not None else None

    def p95(self, chamber_id: str, recipe_id: Optional[str]) -> Optional[float]:
        """활성 P95(Qual·pred enrichment 임계). p99와 같은 행·1회 조회 공유. 부재=None."""
        rec = self._fetch(chamber_id, recipe_id)
        return rec["p95"] if rec is not None else None

    def threshold_version(self, chamber_id: str, recipe_id: Optional[str]) -> Optional[str]:
        """활성 행의 `threshold_version`(Q2 — crazy 마커 `limit_version` 출처). 같은 행·조회 공유.

        재수립(`write_y_threshold`)마다 v1→v2… 로 bump 되므로, 고정 센티넬을 쓰면 재수립 이후
        crazy 가 **실제 판정 컷과 다른 버전**으로 기록된다.
        """
        rec = self._fetch(chamber_id, recipe_id)
        return rec.get("threshold_version") if rec is not None else None

    def invalidate(self, chamber_id: Optional[str] = None, recipe_id: Optional[str] = None) -> None:
        """재수립 시 캐시 무효화 — 인자 없으면 전체 clear, (ch,rc)면 해당 키만."""
        if chamber_id is None:
            self._store.clear()
        else:
            self._store.pop((chamber_id, recipe_id), None)


# =============================================================================
# Step 5 공통 — 설정·채번 (M2-1·M2-2)
# =============================================================================
@dataclass(frozen=True)
class PublisherConfig:
    """발행 계층 설정(주입식·params 매핑). CrazyConfig 와 동일한 명시 실패 패턴(6-1)."""

    publish_enabled: bool               # spc.publish_enabled — false면 send만 skip(조립·적재는 수행)
    y_threshold_cache_ttl_sec: float    # spc.y_threshold_cache_ttl_sec — YThresholdCache TTL(§7 승격)

    @classmethod
    def load(cls, path=CONFIG_PATH) -> "PublisherConfig":
        """params.yaml → PublisherConfig. 키 부재 시 매직넘버 대체 없이 명시 실패(6-1)."""
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        spc = _section(cfg, "spc", path)
        return cls(
            publish_enabled=bool(_key(spc, "publish_enabled", "spc", path)),
            y_threshold_cache_ttl_sec=float(_key(spc, "y_threshold_cache_ttl_sec", "spc", path)),
        )


# 채번 SQL — `recalc_writer._NEXT_SEQ`(:70) 이식. date 는 8자리 숫자(언더스코어 없음 → LIKE 안전),
#   chamber 는 컬럼으로 필터(언더스코어가 LIKE 와일드카드라 패턴에 넣으면 오매치).
_NEXT_ALERT_SEQ = (
    "SELECT COALESCE(MAX(CAST(SUBSTRING(alert_id FROM '[0-9]+$') AS INTEGER)), 0) + 1 "
    "FROM spc_violations WHERE chamber_id = :ch AND alert_id LIKE :date_like"
)


def _chamber_segment(chamber_id: str) -> str:
    """alert_id 챔버 세그먼트 — 언더스코어 제거(`SIM_CH_1` → `SIMCH1`).

    ⚠️ 하류 정규식 `ALERT_ID_RE = ^ALERT-\\d{8}-[A-Z0-9]+-\\d{4,}$`(mock_alert_publisher:52)가
    언더스코어를 불허한다. 제거를 빠뜨리면 6-4 업무 ID 포맷·하류 파서 양쪽에서 탈락한다.
    """
    return chamber_id.replace("_", "")


def next_alert_id(conn, chamber_id: str, date: Optional[str] = None) -> str:
    """`ALERT-<YYYYMMDD>-<CHAMBER무언더스코어>-<SEQ:04d>` 채번 (D-10).

    SEQ = `spc_violations` 의 (날짜, 챔버) 범위 내 max+1 — DB 조회라 **재시작 안전**
    (인메모리 카운터 금지: 재시작 시 1부터 다시 시작해 중복 채번).

    **SEQ 는 4자리를 넘을 수 있다 (A33 · 2026-08-08 실측).** `:04d` 는 zero-pad 최소폭이지
    상한이 아니다 — 8/06 SIMCH1 이 11,311 까지 발급했고(5자리 4,339건 정상 유통·그룹화율
    98.7%), `_NEXT_ALERT_SEQ` 도 `SUBSTRING '[0-9]+$'` 숫자 캐스팅이라 자릿수 증가에 안전.
    하류 검증 정규식은 `\\d{4,}` 로 정합(mock_alert_publisher·test_publisher — 본 PR).
    alert_id 를 **문자열로 정렬·범위 비교하지 말 것**: `-10000` < `-9999` (사전순 역전).

    ⚠️ **단일 컨슈머 전제.** `spc_violations.alert_id` 는 UNIQUE 가 아니라(설계 의도 —
    1 알람 = N 위반 행이 같은 id 공유) 중복 채번돼도 IntegrityError 가 안 난다. 즉
    `recalc_writer` 의 IntegrityError 재시도 안전망을 **이식할 수 없다**. 챔버-비정렬로
    스케일아웃하면 같은 (날짜,챔버)를 2+ 컨슈머가 처리해 max+1 이 경합하고, 하류
    `incident_alerts.alert_id`(UNIQUE)가 2번째를 `ON CONFLICT DO NOTHING` 으로 삼켜
    **조용한 알람 유실**이 된다.
    TODO(확장): 챔버 정렬 파티셔닝을 쓰거나, (date,chamber) DB 시퀀스 /
        `INSERT ... RETURNING` 으로 원자 채번 리팩토링. 포맷은 하류 파서 규약이라 유지.

    Args:
        conn: SQLAlchemy 커넥션(호출자 트랜잭션).
        chamber_id: 원본 챔버 ID(`SIM_CH_1`) — 세그먼트 변환은 내부에서.
        date: `YYYYMMDD`. None 이면 UTC 오늘.

    Returns:
        채번된 alert_id 문자열.
    """
    from sqlalchemy import text                       # lazy — 순수 조립 경로엔 DB 의존 없음

    day = date or datetime.now(timezone.utc).strftime("%Y%m%d")
    seq = conn.execute(text(_NEXT_ALERT_SEQ),
                       {"ch": chamber_id, "date_like": f"ALERT-{day}-%"}).scalar()
    return f"ALERT-{day}-{_chamber_segment(chamber_id)}-{int(seq or 1):04d}"


# =============================================================================
# Step 5 공통 — AlertModel 조립 (M2-3)
# =============================================================================
# AlertModel(agent_service/app/schemas/alert.py) 과 1:1. **필수 필드가 하나라도 빠지면 C 가
# ValidationError 로 alert 을 통째 폐기**하므로, 함정 4종을 코드로 못박는다:
#   ① 최상위 `wafer_id` 금지 — 계약상 위치는 `prediction_context` 안(넣으면 extra=ignore 로 증발)
#   ② `tttm` 6필드 전부 — rollup 없으면 중립 스텁(일부만 채우면 ValidationError)
#   ③ `timestamp` = wafer t0(joined ts) — `now()` 금지(Incident 병합 이벤트시간 정렬 붕괴)
#   ④ `shap_top3` 객체형 → C코드 문자열 리스트 (§2 fdc.prediction 과 §3 alert 의 형태가 다름)
# suspect_sensors(2026-08-05 decouple): 역방향 실제 공통이동 센서 C코드 목록(list). rollup 에서
#   유입돼 alert 로 나간다 — engine `ALERT_FIELDS` 와 짝(계약 §3). C AlertModel 미반영 구간엔
#   extra=ignore 로 무해(하위호환 §2-2), 반영 후 실효.
_TTTM_CONTRACT_FIELDS = ("reference", "score", "top_gap_sensor", "gap_pct",
                         "reference_suspect", "suspect_sensors")
# 참고: rollup 은 `reference_id`·`chamber_id` 도 싣지만 **DB 전용**이라 alert 유입 금지다
#   (V8·tttm_engine.py #7 주석). 위 화이트리스트 추출이 이를 구조적으로 차단하므로
#   별도 블랙리스트 상수는 두지 않는다(이중 관리 방지).
# tttm 결측 시 중립 스텁 — 6필드 전부. "판정"이 아니라 계약 **필드 충족**용
#   (TTTM 단독은 애초에 발행 skip 이라 이 값이 판정에 쓰이지 않는다).
_TTTM_NEUTRAL = {"reference": "fleet_median", "score": 0.0, "top_gap_sensor": "",
                 "gap_pct": 0.0, "reference_suspect": False, "suspect_sensors": []}


def load_sensor_map(path=SENSOR_MAP_PATH) -> dict:
    """`config/sensor_map.yaml` → {C코드: 표시명}. 부재·오류면 빈 dict(표시층 전용이라 무해).

    센서명은 **표시 계층 전용**이다(6-4) — 판정·DB·토픽은 C코드를 그대로 쓴다. 그래서
    이 파일이 없어도 발행은 정상 동작해야 하고, 여기서만 관용적으로 흡수한다.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("sensor_map 로드 실패 — sensor_name 병기 생략(표시층 전용): %s", path)
        return {}


def _wafer_seq(wafer_id: str) -> tuple:
    """wafer_id 정렬 키 — **첫 숫자 세그먼트를 numeric 으로**.

    ⚠️ 문자열 정렬 금지: `"C64_1013.." < "C64_995.."` 로 시간 역전이 난다.
    포맷이 `C64_<n>` 도 되고 `C64_<n>_CH_x` 도 되므로 끝자리(`rsplit`)를 쓰면 `_CH_3` 의
    `3` 을 집는다 → 접두 뒤 **첫** 전체숫자 세그먼트를 쓴다. 파싱 실패는 원문 fallback 으로
    뒤에 몰아 결정성만 지킨다(크래시 금지).
    """
    for seg in str(wafer_id).split("_"):
        if seg.isdigit():
            return (0, int(seg), "")
    return (1, 0, str(wafer_id))


def _shap_to_sensor_codes(shap) -> list:
    """`shap_top3` → C코드 문자열 리스트 (V7).

    `fdc.prediction` §2 는 **객체형** `[{sensor, name, contribution}]` 인데 alert §3 는
    **C코드 문자열 리스트**다. 형태가 다르므로 변환한다. 이미 문자열이면 그대로 통과시키고,
    비-dict·키 결측 원소는 버린다(계약 위반 입력이 alert 전체를 죽이지 않게).
    ※ SHAP **순위에는 개입하지 않는다** — 순서 보존(헌법 3-3).
    """
    if not isinstance(shap, (list, tuple)):
        return []
    out = []
    for item in shap:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("sensor"), str):
            out.append(item["sensor"])
        else:
            logger.warning("shap_top3 원소 형태 불명 — 제외: %r", item)
    return out


def _build_violation(v: dict, sensor_map: dict) -> dict:
    """엔진 위반 dict → 계약 Violation.

    키 변환: `sensor_id`→`sensor` · `sensor_window`→`window`. 엔진 내부 키
    (`limit_basis`·`offending`·`member_wafers`·`wafer_id`·`timestamp`)는 **싣지 않는다** —
    계약에 없고 `extra="ignore"` 로 어차피 버려지는데 페이로드만 부풀린다.
    `sensor_name` 은 sensor_map 에 있을 때만 병기(표시층 전용·6-4).
    """
    sensor = v.get("sensor_id")
    out = {
        "rule_id": v.get("rule_id"),
        "sensor": sensor,
        "window": v.get("sensor_window"),
        "severity": v.get("severity"),
        "description": v.get("description"),
        "current_value": v.get("current_value"),
        "limit_version": v.get("limit_version"),
        "control_limit_upper": v.get("control_limit_upper"),
        "control_limit_lower": v.get("control_limit_lower"),
    }
    name = sensor_map.get(sensor) if isinstance(sensor, str) else None
    if name:
        out["sensor_name"] = name
    return out


def _build_tttm(tttm_obj: Optional[dict]) -> dict:
    """rollup → 계약 tttm 6필드. None 이면 중립 스텁.

    DB 전용 키(`reference_id`·`chamber_id`)는 유입 금지(V8). 계약 6필드는 전부 default 가
    없어 **일부만 채우면 ValidationError → 그 wafer alert 통째 유실**이므로 항상 6개를 채운다.
    `suspect_sensors`(list)는 빈 목록 `[]` 이 유효값이라 None-대체 대상이 아니다(아래 `is not None`).

    ⚠️ **키 부재뿐 아니라 값 `None` 도 중립값으로 대체**한다. rollup 은 키를 항상 채우지만
    값이 None 일 수 있다 — `tttm_engine._gap_pct` 는 `|fleet_median| <= EPS` 일 때 None 을
    반환하고 `chamber_rollup` 이 그대로 싣는다. `.get(k, default)` 만 쓰면 그 None 이 통과해
    C 에서 `gap_pct: Input should be a valid number` ValidationError → alert 폐기가 된다.
    """
    if not tttm_obj:
        return dict(_TTTM_NEUTRAL)
    return {k: (tttm_obj[k] if tttm_obj.get(k) is not None else _TTTM_NEUTRAL[k])
            for k in _TTTM_CONTRACT_FIELDS}


def _build_suspect_window(violations: list) -> Optional[dict]:
    """위반들의 `member_wafers` 합집합 → suspect_window (없으면 None → 필드 생략).

    합집합인 이유(Q1): 룰마다 창 크기가 달라(N1=1·N3=6·N7=15) 합집합이 최대 커버를 주고,
    HOLD 범위가 넓어져 스크랩 미탐이 줄어든다. `start`/`end` 는 정렬 합집합의 min/max 라
    "범위와 목록"이 항상 정합한다.
    `member_wafers` 는 **additive** — 현 C 스키마(SuspectWindow)에 필드가 없어 파싱 시
    `extra="ignore"` 로 버려진다(무해). C 반영 후 실효(D-15).
    """
    members = sorted({w for v in violations if isinstance(v, dict)
                      for w in (v.get("member_wafers") or [])}, key=_wafer_seq)
    if not members:
        return None
    basis = next((v.get("description") for v in violations
                  if isinstance(v, dict) and v.get("description")), "")
    return {"start_wafer": members[0], "end_wafer": members[-1],
            "member_wafers": members, "basis": basis}


def _build_prediction_context(joined: dict) -> dict:
    """예측 스냅샷 → 계약 prediction_context. **`wafer_id` 는 여기 위치**(최상위 아님).

    예측 결측이어도 계약상 필수 필드라 생략할 수 없다 → 중립 스텁(`predicted_c65=0.0`,
    `shap_top3=[]`)으로 채운다(Q1 — C 통보 후 조정). `spc_flags` 는 SHAP 와 **독립된 병렬
    채널**이라 위반에서 그대로 만든다(헌법 3-3 — SHAP 순위를 룰로 재정렬하지 않는다).
    `anomaly_score` 는 additive(D-15) — 예측이 있을 때만 첨부.
    """
    violations = joined.get("violations") or []
    ctx = {
        "wafer_id": joined.get("wafer_id"),
        "predicted_c65": 0.0,
        "shap_top3": [],
        "spc_flags": [{"sensor": v.get("sensor_id"), "rule": v.get("rule_id")}
                      for v in violations if isinstance(v, dict)],
    }
    pred = joined.get("prediction")
    if isinstance(pred, dict):
        pc = pred.get("predicted_c65")
        ctx["predicted_c65"] = float(pc) if _num(pc) else 0.0
        ctx["shap_top3"] = _shap_to_sensor_codes(pred.get("shap_top3"))
        if _num(pred.get("anomaly_score")):
            ctx["anomaly_score"] = float(pred["anomaly_score"])   # additive(C 스키마 전 no-op)
    return ctx


def build_alert(joined: dict, context_score: int, alert_id: str,
                sensor_map: Optional[dict] = None,
                violations: Optional[list] = None) -> dict:
    """joined + 점수 + alert_id → `fdc.alert` 페이로드 dict (AlertModel 정합·순수).

    `context_score` 는 **주입값을 그대로 싣는다**(passenger·D-9) — publisher 는 점수를
    재계산하지 않고, severity 도 엔진 값을 운반만 한다.

    Args:
        joined: collector 산출 이벤트.
        context_score: orchestrator 산출값의 int 변환분(seam 책임·D-5). 계약 0~100.
        alert_id: `next_alert_id()` 채번값.
        sensor_map: 표시명 룩업(없으면 sensor_name 생략).
        violations: 조립 대상 위반 목록. None 이면 `joined["violations"]`
            (crazy 마커처럼 **합성 위반**을 싣는 경로에서 주입 — M3).

    Returns:
        AlertModel 로 파싱 가능한 dict. 최상위에 `wafer_id` 를 넣지 않는다(①).
    """
    vs = violations if violations is not None else (joined.get("violations") or [])
    smap = sensor_map if sensor_map is not None else {}
    alert = {
        "alert_id": alert_id,
        "timestamp": joined.get("ts"),          # ③ wafer t0 — now() 금지
        "chamber_id": joined.get("chamber_id"),
        "violations": [_build_violation(v, smap) for v in vs if isinstance(v, dict)],
        "tttm": _build_tttm(joined.get("tttm")),
        "context_score": int(context_score),    # 계약 int 0~100
        "prediction_context": _build_prediction_context(joined),
    }
    window = _build_suspect_window(vs)
    if window is not None:                      # Optional — 비면 필드 자체를 생략
        alert["suspect_window"] = window
    return alert


# =============================================================================
# Step 5 공통 — spc_violations 적재 (M2-4)
# =============================================================================
# 컬럼은 db/init.sql:59~77 정합. `context_score` 는 DB FLOAT 이고 계약은 int 라 int→FLOAT
# 저장은 무손실(문제 아님). commit 은 **호출자**(`engine.begin()`) — `_persist_tttm`·
# `recalc_writer` 선례와 동일.
_INSERT_VIOLATION = (
    "INSERT INTO spc_violations "
    "(alert_id, chamber_id, recipe_id, step, sensor_window, rule_id, sensor_id, severity, "
    " current_value, limit_version, control_limit_upper, control_limit_lower, "
    " context_score, description) "
    "VALUES (:alert_id, :ch, :rc, :st, :win, :rule, :sn, :sev, "
    "        :cur, :ver, :ucl, :lcl, :score, :desc)"
)


def insert_violations(conn, joined: dict, alert_id: str, context_score: int,
                      violations: Optional[list] = None) -> int:
    """위반 1건 = 1행으로 `spc_violations` 적재. 같은 alert_id 를 공유한다.

    "전 건 기록" 원칙(설계)을 이행하는 지점이다. 발행보다 **먼저·독립 try** 로 호출해
    발행이 실패해도 기록이 남게 한다(D-12).

    Args:
        conn: SQLAlchemy 커넥션(호출자 트랜잭션 — commit 은 호출자).
        joined: chamber_id·recipe_id 승계용.
        alert_id: 이 알람의 채번값(N행 공유).
        context_score: 알람과 **동일 값**(재계산 금지 — passenger).
        violations: 적재 대상. None 이면 `joined["violations"]`(합성 마커는 주입 — M3).

    Returns:
        적재한 행 수.
    """
    from sqlalchemy import text                       # lazy — 순수 조립 경로엔 DB 의존 없음

    vs = violations if violations is not None else (joined.get("violations") or [])
    rows = [{
        "alert_id": alert_id,
        "ch": v.get("chamber_id") or joined.get("chamber_id"),
        "rc": v.get("recipe_id") or joined.get("recipe_id"),
        "st": v.get("step"),
        "win": v.get("sensor_window"),
        "rule": v.get("rule_id"),
        "sn": v.get("sensor_id"),
        "sev": v.get("severity"),
        "cur": v.get("current_value"),
        "ver": v.get("limit_version"),
        "ucl": v.get("control_limit_upper"),
        "lcl": v.get("control_limit_lower"),
        "score": float(context_score),
        "desc": v.get("description"),
    } for v in vs if isinstance(v, dict)]
    if not rows:
        return 0
    conn.execute(text(_INSERT_VIOLATION), rows)
    return len(rows)


# =============================================================================
# Step 5 공통 — 진입점 (M2-5)
# =============================================================================
# 억제 대상 Phase 값 — `provisional_mode.Phase.PHASE_0.value` 와 같은 문자열.
#   enum 을 import 하지 않는 이유는 `_is_phase_0` docstring 참조(의존 방향 보존).
_PHASE_0_VALUE = "phase_0"


@dataclass
class PublisherDeps:
    """process() 의존 묶음 — 테스트 주입 편의를 위해 dataclass 1개로 모은다.

    producer 소유는 **consumer**(main 생성·finally flush) — publisher 는 받아서 send 만 한다
    (생명주기·graceful shutdown 을 한 곳에서 관리, 6-2 · D-11).
    """

    engine: object                                  # SQLAlchemy Engine (begin() 제공)
    producer: object = None                         # Kafka Producer (None 이면 발행 skip)
    pub_cfg: Optional[PublisherConfig] = None       # 발행 스위치
    sensor_map: dict = field(default_factory=dict)  # 표시명 룩업(표시층 전용)
    get_phase: Optional[Callable] = None            # chamber → Phase (None 이면 억제 없음)
    # 채번 주입 seam — 기본은 `next_alert_id`(운영). 채번 SQL 이 Postgres 전용
    #   (`SUBSTRING(x FROM 정규식)`)이라 sqlite 테스트에서 대체 구현을 꽂기 위한 자리다.
    #   채번 **주체는 여전히 B 모듈**이며(6-4), 주입은 구현 교체가 아니라 백엔드 대체용.
    next_id: Callable = None                        # (conn, chamber_id) -> alert_id

    def __post_init__(self) -> None:
        """채번자 기본값 바인딩 — 미주입이면 운영 구현(`next_alert_id`)."""
        if self.next_id is None:
            self.next_id = next_alert_id


@dataclass
class PublisherCounters:
    """관측 카운터 — 억제·실패는 조용히 넘어가면 안 되므로 수치로 남긴다.

    wafer 마다 로그를 찍으면 스팸이라(6-1) 카운터를 정본으로 두고 로그는 DEBUG/요약만 낸다.
    """

    suppressed: dict = field(default_factory=dict)      # chamber → Phase 0 억제 건수
    published: int = 0
    publish_failed: int = 0
    insert_failed: int = 0
    crazy_detected: int = 0                             # M3 — crazy 마커 발행 건수
    qual_skipped: int = 0                               # M4 — is_qual wafer 발행 skip(Q3 v0 보수)

    def suppress(self, chamber_id: str) -> None:
        """챔버별 억제 카운터 증분."""
        self.suppressed[chamber_id] = self.suppressed.get(chamber_id, 0) + 1


def _is_phase_0(get_phase, chamber_id: str) -> bool:
    """Phase 0(baseline 미확립) 여부. get_phase 미주입이면 억제 없음(False).

    `Phase` enum 을 import 하지 않고 `.value` 문자열로 비교한다 — `Phase` 는
    `agent_b_spc` 소유라 여기서 import 하면 **common → agent_b_spc 역의존**이 된다
    (`__init__.py` 경계 원칙). 값은 `_PHASE_0_VALUE` 상수로 고정한다.
    """
    if get_phase is None:
        return False
    try:
        return str(getattr(get_phase(chamber_id), "value", "")) == _PHASE_0_VALUE
    except Exception:                                # noqa: BLE001 — 억제 판정 실패가 알람을 막지 않게
        logger.warning("get_phase 실패 — 억제 판정 생략(발행 진행): %s", chamber_id, exc_info=True)
        return False


def _send(alert: dict, deps: PublisherDeps, counters: PublisherCounters) -> bool:
    """`fdc.alert` 발행 — 스위치 off 면 send 만 skip(조립·적재는 이미 수행됨, D-11).

    발행은 **best-effort** 다(D-12: 기록=must). 실패는 ERROR 1건 + 카운터로 남기고 삼킨다 —
    발행 예외가 위로 새면 seam 격리를 타고 그 wafer 의 SPC 기록·커밋까지 위태로워진다.
    """
    # fail-safe: cfg 미주입 = 발행 안 함. params 기본값이 `publish_enabled: false` 라
    #   코드 기본값도 같아야 한다 — 반대로 두면 M4 seam 이 pub_cfg 주입을 빠뜨렸을 때
    #   mock 가동 중 **이중 발행**(§11·E2)이 조용히 난다.
    if deps.pub_cfg is None or not deps.pub_cfg.publish_enabled:
        logger.debug("발행 스위치 off(또는 cfg 미주입) — send skip (alert_id=%s)",
                     alert.get("alert_id"))
        return False
    if deps.producer is None:
        logger.debug("producer 미주입 — send skip (alert_id=%s)", alert.get("alert_id"))
        return False
    try:
        deps.producer.produce(TOPIC_ALERT, key=alert["chamber_id"],
                              value=json.dumps(alert, ensure_ascii=False, default=str))
        counters.published += 1
        return True
    except Exception:                                # noqa: BLE001 — best-effort
        counters.publish_failed += 1
        logger.error("fdc.alert 발행 실패 — 기록은 보전됨 (alert_id=%s)",
                     alert.get("alert_id"), exc_info=True)
        return False


def process(joined: dict, context_score: int, verdict, deps: PublisherDeps,
            counters: Optional[PublisherCounters] = None) -> list:
    """억제 → 채번 → 적재 → 조립 → 발행. 발행한 alert_id 목록을 반환한다.

    순서가 계약이다: **적재를 발행보다 먼저·독립 try** 로 둔다(D-12) — 발행이 실패해도
    "전 건 기록"이 남아야 한다. 반대로 두면 발행 예외 시 기록이 통째 유실된다.

    발행 조건(D-9): Nelson 위반 ∪ crazy. **TTTM 단독은 skip** — TTTM 은 이미
    `tttm_comparisons` 에 적재되고 알람 트리거가 아니다.

    Args:
        joined: collector 산출 이벤트.
        context_score: orchestrator 산출값의 int 변환분(passenger — 재계산 안 함).
        verdict: `is_crazy()` 결과(`CrazyVerdict` 또는 None). crazy 발행 경로는 M3 합류.
        deps: engine·producer·설정·sensor_map·get_phase 묶음.
        counters: 관측 카운터. None 이면 새로 만든다(호출자가 누적하려면 주입).

    Returns:
        발행 시도까지 간 alert_id 리스트. 억제·skip 이면 빈 리스트.
    """
    counters = counters if counters is not None else PublisherCounters()
    chamber_id = joined.get("chamber_id")

    if _is_phase_0(deps.get_phase, chamber_id):      # ① 생성억제 — 적재·발행 둘 다 skip (D-8)
        counters.suppress(chamber_id)
        logger.debug("Phase 0 억제 — 적재·발행 skip: wafer=%s chamber=%s",
                     joined.get("wafer_id"), chamber_id)
        return []

    issued = []
    violations = joined.get("violations") or []
    if violations:                                   # ② Nelson 경로
        with deps.engine.begin() as conn:
            alert_id = deps.next_id(conn, chamber_id)
            try:                                     # 적재 = must (발행보다 먼저·독립 try)
                insert_violations(conn, joined, alert_id, context_score)
            except Exception:                        # noqa: BLE001
                counters.insert_failed += 1
                # ⚠️ 알려진 한계(SEQ 재사용) — **합의 완료: ⓑ 현행 유지 + 카운터 감시** (2026-07-28, B).
                #   현상: SEQ 는 spc_violations max+1 이라, 적재가 실패하면 이 alert_id 를 받쳐줄
                #   행이 없어 **다음 알람이 같은 SEQ 를 재채번**한다. 그러면 하류
                #   `incident_alerts.alert_id`(UNIQUE)가 두 번째를 `ON CONFLICT DO NOTHING` 으로
                #   삼켜 **조용한 알람 유실**이 된다(V9 경로).
                #   채택 근거: 적재 실패 빈도가 낮고, 발행을 막는 ⓐ 는 D-12("발행=best-effort")를
                #   뒤집는데다 데이터 결함 시 해당 wafer 알람이 영구 차단된다.
                #   후속(고도화 시): **ⓓ 인메모리 하이워터마크** — `(날짜,챔버)` 별 마지막 발급 SEQ 를
                #   들고 `max(DB+1, 메모리+1)` 로 채번. 스키마 변경·승인 없이 중복을 막는다
                #   (단일 컨슈머 전제 유지 — 멀티 컨슈머로 가면 ⓒ DB 시퀀스, D-10 TODO 와 동시 해소).
                #   감시 지표: `counters.insert_failed` — 0 이 아니면 위 경로가 열린 것이다.
                logger.error("spc_violations 적재 실패 — SEQ 재사용 위험 (alert_id=%s)",
                             alert_id, exc_info=True)
        alert = build_alert(joined, context_score, alert_id, deps.sensor_map)
        _send(alert, deps, counters)                 # 발행 = best-effort
        issued.append(alert_id)

    if verdict is not None:                          # ③ crazy 경로 (D-13: 별도 alert 1건 — 합치지 않음)
        with deps.engine.begin() as conn:
            aid = deps.next_id(conn, chamber_id)
            try:                                     # 적재 = must (발행보다 먼저·독립 try, D-12)
                insert_spc_violation(conn, joined, aid, context_score, verdict)
            except Exception:                        # noqa: BLE001
                counters.insert_failed += 1
                # SEQ 재사용 한계는 Nelson 경로와 동일(위 주석 참조) — ⓑ 유지·후속 ⓓ.
                logger.error("crazy spc_violations 적재 실패 — SEQ 재사용 위험 (alert_id=%s)",
                             aid, exc_info=True)
        alert = build_crazy_violation(verdict, joined, context_score, aid, deps.sensor_map)
        publish_crazy_alert(alert, deps, counters)   # 발행 = best-effort
        counters.crazy_detected += 1
        logger.warning("B9 crazy 발행: wafer=%s chamber=%s (predicted=%s anomaly=%s)",
                       joined.get("wafer_id"), chamber_id, verdict.pred_hit, verdict.an_hit)
        issued.append(aid)

    return issued                                    # ④ 위반 0 ∧ verdict 없음(TTTM 단독) → [] (D-9)


# =============================================================================
# B9 crazy 발행 (M3) — 마커 조립·적재·발행. M2 골격(build_alert·insert_violations·_send) 재사용.
# =============================================================================
# 마커 필드 형태(PM-4 확정 = 합성 Violation 마커). PM 1급필드 전환 시 build_crazy_violation만 스왑(격리).
# ⚠️ 하류 계약 — 이 값은 **바꾸지 말 것**. 두 소비자가 이 문자열로 crazy 를 식별한다:
#   ① orchestrator `incident_grouper.is_crazy_alert()` → `incidents.incident_type='crazy_spot'` 분리(P6-1)
#   ② agent_service tool 들이 대표 위반 선정에서 B9 를 **제외**(2026-07-29 C 결정 ⓐ — `violations[0]`
#      이 마커면 `sensor="C65"` 를 실센서로 오인해 무의미한 재산정·정비 대상이 잡히는 문제)
#   값 변경 시 두 소비자가 조용히 crazy 를 놓친다(예외 없이 일반 SPC 사건으로 처리됨).
#   ③ 채점측 `axes/score_spc` 가 rule_base 미등록 경고에서 제외 (markers.py 단일 소스)
_MARKER_RULE_ID = CRAZY_MARKER_RULE_ID    # 종류 마커(Nelson 룰 아님) — 정의는 markers.py
_MARKER_SENSOR_ID = "C65"                 # 예측 타겟 마커(sensor_map 미등록 허용)
_MARKER_WINDOW = "settled"                # alert.py Violation 필수(누락 시 C ValidationError·리뷰2)
_MARKER_LIMIT_LOWER = 0.0                 # 비Optional float 센티넬(null 금지·리뷰2)
_MARKER_SEVERITY = "CRITICAL"             # auto-inhibit B8 → HOLD
_MARKER_LIMIT_VERSION_SENTINEL = "v1"     # crazy 버전 테이블 없음 → 센티넬(Q2 v0)


def _build_crazy_marker(verdict) -> dict:
    """CrazyVerdict → 합성 마커 Violation(엔진 키 — `_build_violation`/`insert_violations`가 읽는 형태).

    대표 arm 우선(D-14): predicted hit(둘 다여도)이면 predicted arm 값·컷·버전, anomaly-only면
    anomaly arm. limit_version은 v0에서 'v1' 센티넬(read seam이 threshold_version 미반환 — Q2).
    """
    if verdict.pred_hit:                              # 대표 = predicted arm
        current, upper = verdict.predicted_c65, verdict.threshold
        # Q2 확정(2026-07-28): 실제 판정에 쓴 컷의 출처 버전을 싣는다. 재수립마다 v1→v2… 로
        #   bump 되므로 고정 센티넬은 **거짓 기록**이 된다(데모 2막 재수립 → 데모4 crazy 경로).
        #   행 부재(config 1572 fallback) 시에만 센티넬.
        version = verdict.threshold_version or _MARKER_LIMIT_VERSION_SENTINEL
        desc = f"B9 crazy: predicted_c65 {verdict.predicted_c65:.1f} > P99 {verdict.threshold:.1f}"
        if verdict.an_hit and verdict.anomaly_score is not None:
            desc += f" · anomaly {verdict.anomaly_score:.2f} > {verdict.anomaly_cut:.2f}"
    else:                                             # anomaly-only
        current, upper = verdict.anomaly_score, verdict.anomaly_cut
        version = _MARKER_LIMIT_VERSION_SENTINEL      # anomaly arm 은 버전 테이블 없음(센티넬)
        desc = f"B9 crazy: anomaly_score {verdict.anomaly_score:.2f} > cut {verdict.anomaly_cut:.2f}"
    return {
        "rule_id": _MARKER_RULE_ID, "sensor_id": _MARKER_SENSOR_ID,
        "sensor_window": _MARKER_WINDOW, "severity": _MARKER_SEVERITY,
        "current_value": float(current) if current is not None else 0.0,
        "control_limit_upper": float(upper),
        "control_limit_lower": _MARKER_LIMIT_LOWER,
        "limit_version": str(version),
        "description": desc,
    }


def build_crazy_violation(verdict, joined: dict, context_score: int, alert_id: str,
                          sensor_map: Optional[dict] = None) -> dict:
    """CrazyVerdict → `fdc.alert` 봉투(합성 마커 Violation 단독). M2 `build_alert` 재사용(M3-1).

    마커 Violation 1건을 `violations` 로 주입하면 build_alert 가 tttm(중립 스텁)·prediction_context
    (predicted_c65·shap·anomaly_score additive)·context_score(int)·timestamp(wafer t0)를 채워
    AlertModel 정합 봉투를 만든다. suspect_window 는 마커에 `member_wafers` 가 없어 자동 생략.
    **격리 스왑 지점**: PM 1급필드 전환 시 이 함수만 교체.
    """
    return build_alert(joined, context_score, alert_id, sensor_map,
                       violations=[_build_crazy_marker(verdict)])


def insert_spc_violation(conn, joined: dict, alert_id: str, context_score: int, verdict) -> int:
    """합성 crazy 마커를 `spc_violations` 에 적재 — M2 `insert_violations` 재사용(M3-2).

    발행보다 **먼저·독립 try**(D-12) 로 호출된다(호출부=process). commit=호출자.
    """
    return insert_violations(conn, joined, alert_id, context_score,
                             violations=[_build_crazy_marker(verdict)])


def publish_crazy_alert(alert: dict, deps: PublisherDeps, counters: PublisherCounters) -> bool:
    """조립된 crazy 봉투를 `fdc.alert` 로 발행 — 공통 `_send` 재사용(M3-3).

    별도 함수로 두는 건 스펙 격리 의도(발행 경로 스왑 지점)이며 내부는 Nelson 경로와 동일 `_send`.
    스위치 off 면 send 만 skip(조립·적재는 이미 수행, D-11).
    """
    return _send(alert, deps, counters)
