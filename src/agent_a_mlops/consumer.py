"""
[Team A - MLOps] 실시간 C65 예측 파이프라인 — lean-85 실연동 (PR②)

기능:
1. `fdc.raw` 토픽에서 Row(3초 샘플) 단위 데이터를 실시간 수신
2. wafer_id 단위 버퍼링 → 같은 chamber에서 다음 wafer 등장 시 이전 wafer 완료로 간주(flush)
3. 완료 wafer의 트레이스를 lean-85 피처 테이블로 변환 (`lean85_pipeline.build_wafer_table` 재사용
   — 피처 정의 단일 소스, consumer 재구현 금지: 계획 §3-2)
4. models/c65_predictor 최신 lean-85(XGB json + manifest)로 C65 예측 + SHAP 기여도(native pred_contribs)
5. `fdc.prediction` 발행 (API Contract §2 준수)

운영 스위치(.env — 시연 안전망, 계획 §3-2 ①):
    PREDICTION_MODE            lean85(기본) | dummy — 모델 로드 실패 시 자동 dummy 폴백
    LEAN85_MODEL_DIR           모델 stamp 폴더 직접 지정 (기본: control/ct1/champion.json → glob 최신)
    LEAN85_CT1_PROMOTE_DIR     CT① 승격 신호 채널 (기본 control/ct1) — 관리형 리로드
    LEAN85_CT1_CHECK_EVERY     30(기본) — poll N회마다 CT① 신호 스캔
    LEAN85_PM_LOG              운영 pm_log.json 경로 (기본: find_file 탐색) — §6-2 확정 전 임시
    LEAN85_TIME_SOURCE         wall_clock(기본) | src_ts — C40 시간 소스 (fdc.raw에 C40 부재: §6-2 안건)
    LEAN85_AE_MODE             dummy(기본) | live — live면 ae_v3 실추론(drift/anomaly 실값). 로드 실패 시 더미 폴백 (A3-3)
    AE_BUNDLE_DIR              AE 번들 폴더 직접 지정 (기본: params.yaml anomaly_ae.bundle_search_glob 최신)
    AE_OFFSET_CORRECT          0(기본) | 1 — 🆕 AE **입력** 챔버 기준선 보정(`config/chamber_offsets.json`
                               의 챔버별 기지 오프셋을 trace 에서 차감 · 8/4 밤 리허설 최소형).
                               ⚠️ 스키마는 그대로지만 발행 `drift_score`·`anomaly_score` 의 **의미가
                               바뀐다**(챔버 간 기준선 차를 제거한 뒤의 이상도) — 계약 §2 소비자
                               (B·Dashboard) 공지 후 1로 (L8, 헌법 4-3). 로그 `ae_mode` 가
                               `live-decal` 이면 실제 차감된 것이고, 로드 실패·미등재 챔버는 `live`.
    AE_PUBLISH_EXT             0(기본) | 1 — 🆕 AE 확장 필드(ae_raw·ae_top_channels 등) 계약 §2-2 승인 후 발행
    LEAN85_PUBLISH_LOW_CONFIDENCE  0(기본) | 1 — 🆕 계약 필드 §2-2 승인 후 1로 (승인 전 로그만)
    LEAN85_IDLE_FLUSH_SEC      **임시 override 전용**(설정 시 WARNING). 정본은 params
                               `lean85.idle_flush_sec`(15) — 이 시간 동안 새 row가 없는
                               wafer는 완료로 간주해 flush. 0이면 비활성(구 동작 = 같은
                               chamber 다음 wafer 도착 시에만 flush).
                               ⚠️ B `spc.wafer_idle_flush_sec`(30)와 짝 — 기동 시
                               `_check_idle_flush_ordering()` 이 두 값을 대조한다.
    LEAN85_CT0_CHECK_EVERY     30(기본) — poll N회마다 CT⓪ bias 파일 스캔 (mode=active 일 때만)
    CT0_BIAS_DIR               CT⓪ bias 파일 디렉토리 (기본 control/ct0)
    ※ CT⓪ 보정의 on/off 는 env 가 아니라 `config/params.yaml` `ct.model_r2r.mode` 다
      (off|shadow|active — 단일 소스). `active` 에서만 가산·`bias_applied` 발행이 일어난다.

주의:
- ⚠️ **전제: `fdc.raw` 파티션 = 1.** wafer 교체 감지(`handle_message`)는 토픽 전역 순서에
  의존하므로, 파티션을 늘리려면 **wafer_id 키 파티셔닝 + 파티션별 상태 분리**가 선행돼야 한다.
  (파티션 간 순서 보장이 없으면 같은 chamber의 row가 섞여 들어와 ⓐ 불완전 wafer 조기 flush,
  ⓑ 같은 wafer 중복 발행이 발생한다. 현재 `docker-compose.yml` 은 `--partitions 1` 로 생성.)
- SHAP-Nelson 독립 채널 (헌법 3-3): SHAP 순위에 룰 개입 금지. **spc_flags 는 A 가 발행하지 않는다** —
  Nelson 판정은 B 소유이고 `fdc.alert.prediction_context.spc_flags` 로 이미 나간다. `prediction_sink` 가
  그것을 `wafer_predictions.spc_flags` 로 적재한다 (M1, 2026-08-05 — 구 "A3-2 TODO" 해소).
- 센서명은 C코드 사용, 표시명은 config/sensor_map.yaml 단일 소스 (헌법 6-4).
- **shap_top3 정렬은 발행 측(A)이 보장**한다 — `|contribution|` 내림차순 상위 3
  (`shap_contract.normalize_shap_top3`, 계약 §2). 소비자는 재정렬 금지·순서 그대로 신뢰.
  ⚠️ `contribution` 은 부호가 있어 **raw 값으로 재정렬하면 순서가 달라진다.**
- **발행 페이로드는 표준 JSON 만 낸다** — NaN/Inf 는 `json.dumps` 가 비표준 리터럴(`NaN`)로
  직렬화해 대시보드(JS `JSON.parse`)와 DB(NOT NULL 수치 컬럼)를 동시에 깨뜨린다.
  `_finite()` 로 발행 길목에서 일괄 방어한다 (2026-07-28 코드리뷰 §1-5).
"""

import json
import logging
import math
import os
import random
import signal
import sys
import time
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from confluent_kafka import Consumer, Producer

sys.path.insert(0, str(Path(__file__).resolve().parent / "lean85"))
import lean85_pipeline as lp  # noqa: E402  (피처·모델 단일 소스)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shap_contract import (  # noqa: E402  (발행 정렬 계약 — 순수·stdlib 전용)
    SHAP_TOP_N, normalize_shap_top3,
)
# CT⓪ Model R2R — **파일 채널만** import 한다 (설계 D3: 서빙은 DB·Kafka 보정 경로를 모른다).
# `ct0_bias.updater`(psycopg2·confluent_kafka)를 끌어오면 보정 경로 장애가 서빙 기동을
# 막는다 — 그 격리가 이 설계의 요점이다.
from ct0_bias import control_io as ct0_io  # noqa: E402
from ct0_bias import core as ct0_core  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ [Team A] %(message)s")
log = logging.getLogger("mlops-consumer")

# ── 설정 (헌법 6-1: 하드코딩 금지 — 인프라 값은 .env) ──────────────────
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_IN = "fdc.raw"
TOPIC_OUT = "fdc.prediction"
CONSUMER_GROUP = "consumer-group-prediction"

PREDICTION_MODE = os.environ.get("PREDICTION_MODE", "lean85")
MODEL_DIR = os.environ.get("LEAN85_MODEL_DIR", "")
PM_LOG_PATH = os.environ.get("LEAN85_PM_LOG", "")
TIME_SOURCE = os.environ.get("LEAN85_TIME_SOURCE", "wall_clock")
AE_MODE = os.environ.get("LEAN85_AE_MODE", "dummy")            # dummy | live (A3-3 실연동)
CT2_PROMOTE_DIR = Path(os.environ.get("LEAN85_CT2_PROMOTE_DIR", ""))  # CT² 승격 신호 채널 (§8-E) — 빈값 = REPO_ROOT/control/ct2
CT2_RELOAD_CHECK_EVERY = int(os.environ.get("LEAN85_CT2_CHECK_EVERY", "30"))  # poll N회(≈N초)마다 신호 스캔
CT1_PROMOTE_DIR = Path(os.environ.get("LEAN85_CT1_PROMOTE_DIR", ""))  # CT① 승격 신호 채널 (CT1 설계 D5) — 빈값 = REPO_ROOT/control/ct1
CT1_RELOAD_CHECK_EVERY = int(os.environ.get("LEAN85_CT1_CHECK_EVERY", "30"))  # poll N회(≈N초)마다 신호 스캔
AE_BUNDLE_DIR = os.environ.get("AE_BUNDLE_DIR", "")
AE_PUBLISH_EXT = os.environ.get("AE_PUBLISH_EXT", "0") == "1"   # AE 확장 필드 발행 게이트 (계약 §2-2 승인 후)
PUBLISH_LOW_CONF = os.environ.get("LEAN85_PUBLISH_LOW_CONFIDENCE", "0") == "1"
# 유휴 wafer flush 임계(초) — 0이면 비활성. 버퍼 무한 성장 차단 (리뷰 §3-1).
#   정본은 params `lean85.idle_flush_sec` 다 — 값 대입은 REPO_ROOT 정의 뒤(_load_idle_flush_sec).
IDLE_FLUSH_FALLBACK_SEC = 300.0       # params 로드 실패 시에만 쓰는 구 기본값(버퍼 성장 차단 목적)
PRODUCER_FLUSH_SEC = 5.0              # 큐 포화 시 flush 대기(초) — 발행 재시도 전
IDLE_CHECK_INTERVAL_SEC = 5.0         # 유휴 wafer 점검 주기(초) — poll 반환 여부와 무관하게 수행
CONTRIB_IDENTITY_RTOL = 1e-4          # SHAP 기여도 합 vs predict 상대 허용 오차 (float32 누적오차)
FLUSHED_MEMORY_MAX = 2000             # 유휴 flush 기억 개수 (지각 row 중복 발행 차단)
FLUSHED_MEMORY_TTL_FACTOR = 2.0       # 기억 유지 시간 = IDLE_FLUSH_SEC × 이 값 (재기동 후 차단 방지)

# 더미 폴백 상수 (헌법 6-1: 매직 넘버 금지) — AE 미가동/실패 시 발행 범위
DUMMY_C65_BASE = 100.0
DUMMY_C65_JITTER = 10.0
DUMMY_C17_COEF = 0.5
DUMMY_DRIFT_RANGE = (0.1, 0.4)
DUMMY_ANOMALY_RANGE = (0.01, 0.15)

REPO_ROOT = Path(__file__).resolve().parents[2]
SENSOR_MAP_PATH = REPO_ROOT / "config" / "sensor_map.yaml"
AE_OFFSETS_PATH = REPO_ROOT / "config" / "chamber_offsets.json"   # 챔버 기지 오프셋 (B 7/10 전달분)
PARAMS_PATH = REPO_ROOT / "config" / "params.yaml"


def _load_idle_flush_sec() -> float:
    """유휴 flush 임계(초) — 정본은 params `lean85.idle_flush_sec`.

    env `LEAN85_IDLE_FLUSH_SEC` 는 **임시 override 전용**이라 설정 시 WARNING 을 남긴다.
    창(세션 환경변수)을 닫으면 정본으로 돌아간다는 것을 운영자가 알 수 있어야 하기 때문이다 —
    AE 번들이 세션 override 로만 지정돼 있다가 창을 닫자 **에러 없이** 구 번들로 되돌아간
    선례(#153)와 같은 함정이다.

    로드 실패 시 `IDLE_FLUSH_FALLBACK_SEC`(구 기본값). 이 폴백은 조인 타이밍용이 아니라
    버퍼 무한 성장 차단용이므로, 폴백으로 떨어지면 조인율이 떨어진다 — 그래서 WARNING 이다.
    """
    override = os.environ.get("LEAN85_IDLE_FLUSH_SEC")
    if override:
        log.warning("LEAN85_IDLE_FLUSH_SEC=%s override 적용 — 정본은 params.yaml "
                    "`lean85.idle_flush_sec` 다 (이 프로세스에만 유효)", override)
        return float(override)
    try:
        import yaml
        with open(PARAMS_PATH, encoding="utf-8") as f:
            lean = (yaml.safe_load(f) or {}).get("lean85") or {}
        return float(lean["idle_flush_sec"])
    except Exception as e:                                   # noqa: BLE001
        log.warning("params.yaml lean85.idle_flush_sec 로드 실패 → 폴백 %.0fs "
                    "(조인 타이밍용 값이 아니다): %s", IDLE_FLUSH_FALLBACK_SEC, e)
        return IDLE_FLUSH_FALLBACK_SEC


IDLE_FLUSH_SEC = _load_idle_flush_sec()


def _check_idle_flush_ordering() -> None:
    """A(a-pred)와 B(spc) 유휴 flush 순서 대조 — 역전이면 ERROR 로그(기동은 막지 않는다).

    a-pred 가 B 보다 **먼저** flush 해야 B 가 알람을 낼 때 예측이 캐시에 들어 있다.
    지켜야 할 부등식은 설정값 비교(15 < 30)가 아니라 **유효치** 비교다 — 유휴 점검이
    `IDLE_CHECK_INTERVAL_SEC` 주기라 임계 도달 후 최대 그만큼 더 기다렸다 닫는다:

        lean85.idle_flush_sec + IDLE_CHECK_INTERVAL_SEC < spc.wafer_idle_flush_sec

    예컨대 26 으로 두면 `26 < 30` 이라 통과처럼 보이지만 유효치는 31 이라 **조용히 역전**된다.
    어긋났을 때의 증상이 예외가 아니라 **조인율 0%**(양쪽 다 정상 로그·산출물만 빔)라
    사람이 못 보므로, 리터럴을 재현하지 않고 **실제 두 값을 읽어** 대조한다.
    기동을 막지는 않는다 — 관측 전용이다(`fdc.raw` 파티션=1 전제와 같은 처리).
    """
    try:
        import yaml
        with open(PARAMS_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        b_flush = float((cfg.get("spc") or {})["wafer_idle_flush_sec"])
    except Exception as e:                                   # noqa: BLE001
        log.warning("spc.wafer_idle_flush_sec 로드 실패 → flush 순서 대조 생략: %s", e)
        return
    effective = IDLE_FLUSH_SEC + IDLE_CHECK_INTERVAL_SEC
    if IDLE_FLUSH_SEC <= 0:                                  # 유휴 flush 비활성 — 순서 개념 없음
        log.info("유휴 flush 비활성(idle_flush_sec=0) — B flush 순서 대조 생략")
        return
    if effective >= b_flush:
        log.error("⚠️ 유휴 flush 순서 역전 — a-pred 유효 %.1fs (=%.1f+%.1f) ≥ B %.1fs. "
                  "B 가 먼저 알람을 내 **예측 조인이 비게 된다**(예외 없이 산출물만 빔). "
                  "lean85.idle_flush_sec 를 낮추거나 spc.wafer_idle_flush_sec 를 올릴 것",
                  effective, IDLE_FLUSH_SEC, IDLE_CHECK_INTERVAL_SEC, b_flush)
    else:
        log.info("유휴 flush 순서 OK — a-pred 유효 %.1fs < B %.1fs (여유 %.1fs)",
                 effective, b_flush, b_flush - effective)


# ── CT⓪ Model R2R (설계 `docs/CT0_ModelR2R_설계방향_v1.md` §4 serving 경로) ──
# 서빙이 하는 일은 셋뿐이다: ⓐ bias 파일 읽기(캐시) ⓑ Qual 제외 가산 ⓒ `bias_applied` 발행.
# 추정·기록은 전부 별도 프로세스(`ct0_bias.updater`)가 한다 — 여기는 지연을 추가하지 않는다.
CT0_CHECK_EVERY = int(os.environ.get("LEAN85_CT0_CHECK_EVERY", "30"))  # poll N회마다 파일 스캔
_ct0_cache = ct0_io.BiasCache()
_ct0_bias_now: dict = {}              # chamber → 마지막 스캔값 (wafer 처리 중 파일 IO 0)


def _ct0_mode() -> str:
    """`ct.model_r2r.mode` (off|shadow|active). 로드 실패·미지의 값 → **off 로 잠금**.

    params 를 못 읽었을 때 보정이 켜지면 "설정을 못 읽었으니 무인 적용"이 된다 —
    `ct1_auto_promote` 의 코드 기본값 규율과 같은 방향이다.
    """
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            ct = (yaml.safe_load(f) or {}).get("ct") or {}
        return ct0_core.BiasConfig.from_params(ct).mode
    except Exception as e:                                   # noqa: BLE001
        log.warning("params.yaml ct.model_r2r 로드 실패 → 보정 off 로 잠금: %s", e)
        return ct0_core.MODE_OFF


CT0_MODE = _ct0_mode()


def _ct0_scan(chambers) -> None:
    """bias 파일 스캔 — poll 루프에서 주기적으로만 부른다 (wafer 경로에 IO 를 넣지 않는다).

    실패는 캐시가 흡수한다(직전 값 유지 또는 0). 여기서 예외가 새면 예측이 멈춘다.
    """
    for ch in list(chambers):
        _ct0_bias_now[ch] = _ct0_cache.get(ch)


def ct0_bias_for(meta: dict) -> float:
    """이 wafer 에 가산할 bias. `active` 가 아니거나 Qual wafer 면 0.0.

    **D7 — Qual wafer 는 보정하지 않는다**: Qual C65-proxy 판정 시점엔 verdict 가 확정
    전이라 구레짐 bias 가 살아 있고, 그 값을 판정 입력에 섞으면 사이클 간 비교 가능성이
    깨진다. Qual 은 `fdc.actual` 도 미발행이라 창 오염은 원천이 없다.
    """
    if CT0_MODE != ct0_core.MODE_ACTIVE or meta.get("is_qual"):
        return 0.0
    ch = meta.get("chamber_id")
    if not ch:
        return 0.0
    if ch not in _ct0_bias_now:                              # 첫 등장 챔버 — 1회 즉시 읽기
        _ct0_bias_now[ch] = _ct0_cache.get(ch)
    return _ct0_bias_now.get(ch, 0.0)


# ── D14 리로드 스모크 (CT1 설계 v2 §4-8 — 서빙 보호, 헌법 6-2 확장) ──────────
CT1_SMOKE_BUFFER_DEFAULT = 50         # 코드 폴백 (params 로드 실패 시) — 설계 §6 제안치와 동일
CT1_SMOKE_MIN_DEFAULT = 20


def _ct1_smoke_cfg() -> tuple[int, int]:
    """`ct.ct1_smoke_*` 로드 → (링버퍼 크기, 스모크 최소 표본). 실패 시 기본값(+경고).

    오설정 방어 2종 — 둘 다 **스모크의 조용한 무력화 또는 전 배포 차단**으로 끝난다:
      · `min > buf`  → 링버퍼가 임계에 영원히 도달 못 함 = 영구 생략
      · `≤ 0`        → 빈 버퍼로 스모크가 돌아 성공 경로가 예외를 내고 **모든 promote 실패**
                       (`maxlen` 이 음수면 모듈 import 자체가 죽어 consumer 가 기동 못 한다)
    """
    buf, low = CT1_SMOKE_BUFFER_DEFAULT, CT1_SMOKE_MIN_DEFAULT
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            ct = (yaml.safe_load(f) or {}).get("ct") or {}
        buf, low = int(ct.get("ct1_smoke_buffer_wafers", buf)), int(ct.get("ct1_smoke_min_wafers", low))
    except Exception as e:                                   # noqa: BLE001
        buf, low = CT1_SMOKE_BUFFER_DEFAULT, CT1_SMOKE_MIN_DEFAULT   # 부분 파싱분 폐기
        log.warning("params.yaml ct1_smoke_* 로드 실패 → 기본값(%d/%d): %s", buf, low, e)
    if buf < 1 or low < 1:
        log.warning("ct1_smoke_* 는 1 이상이어야 한다 (buf=%d, min=%d) — 1 로 올려 적용", buf, low)
        buf, low = max(1, buf), max(1, low)
    if low > buf:
        log.warning("ct1_smoke_min_wafers(%d) > ct1_smoke_buffer_wafers(%d) — 링버퍼가 절대 "
                    "임계에 도달하지 못한다. min 을 buf 로 낮춰 적용", low, buf)
        low = buf
    return buf, low


CT1_SMOKE_BUFFER, CT1_SMOKE_MIN = _ct1_smoke_cfg()
# 최근 완성 wafer의 **피처 행**(85-벡터) 링버퍼. `predict_wafer` 가 이미 빌드한 table 을
# 재사용하므로 추가 피처 빌드가 없다(서빙 부하 0). predictor 교체와 무관하게 살아 있어야
# 하므로 모듈 수준이다 — 새 모델을 검사할 표본은 옛 모델이 모은 것이다.
_feature_ring = deque(maxlen=CT1_SMOKE_BUFFER)
_ring_warned = False                  # 피처 추출 실패 경고 1회 제한 (wafer 마다 찍지 않는다)

# 스모크 절대범위 ⓒ의 단일 소스 = `validate_lean85.DEFAULTS` (설계 §6 — config 키를
# 늘리지 않는다). 지연/가드 import: 게이트 모듈 로드 실패가 **서빙을 죽이면 안 된다**.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lean85"))
    from validate_lean85 import DEFAULTS as _GATE_DEFAULTS  # noqa: E402
    CT1_SMOKE_RANGE = (float(_GATE_DEFAULTS["smoke_pred_min"]),
                       float(_GATE_DEFAULTS["smoke_pred_max"]))
except Exception as _e:                                      # noqa: BLE001
    CT1_SMOKE_RANGE = None
    log.warning("validate_lean85.DEFAULTS 로드 실패 → D14 스모크의 절대범위 검사 ⓒ 생략 "
                "(ⓐ유한값·ⓑ분산 검사는 유지): %s", _e)


def _ring_push(table: pd.DataFrame, lean) -> None:
    """완성 wafer 1장의 피처 행을 링버퍼에 적재 (D14 입력 — 설계 §4-8).

    `predict_wafer` 가 방금 빌드한 table 을 재사용한다. 실패해도 **추론을 막지 않는다** —
    스모크는 배포 보호 장치이지 서빙 경로가 아니다. 경고는 1회만 찍는다(wafer 마다 찍으면
    로그가 매몰된다).
    """
    global _ring_warned
    if not _feature_ring.maxlen:
        return
    try:
        # NaN 은 그대로 둔다 — XGBoost 는 결측을 네이티브로 처리하고, 서빙이 실제로 보는
        # 입력 분포를 왜곡 없이 담는 것이 스모크의 목적이다.
        _feature_ring.append(table[lean].to_numpy(dtype=float)[0])
    except Exception as e:                                   # noqa: BLE001
        if not _ring_warned:
            _ring_warned = True
            log.warning("D14 링버퍼 적재 실패 — 스모크 표본이 쌓이지 않는다(추론은 계속): %s", e)


running = True          # graceful shutdown 플래그 (헌법 6-2)
wafer_buffer = defaultdict(list)      # wafer_id → rows
chamber_current = {}                  # chamber_id → 현재 진행 중 wafer_id
chamber_meta = {}                     # wafer_id → {chamber_id, is_qual, ...}
wafer_last_seen = {}                  # wafer_id → 마지막 row 수신 monotonic 시각 (유휴 flush 판정)
# **유휴 flush 로** 발행을 마친 wafer_id → (발행 monotonic 시각, 경고 로그 여부).
# 유휴 flush 뒤 지각 row 가 도착하면 버퍼가 다시 쌓여 같은 wafer 가 부분 트레이스로
# 재발행되고, sink 의 COALESCE UPSERT 가 정상 예측값을 덮어쓴다 → 그걸 막는 기억장치.
#
# ⚠️ 정상 flush(같은 chamber 다음 wafer 도착)는 **등록하지 않는다.** 시뮬레이터가
# 결정적 wafer_id 를 쓰기 때문에(재기동·리플레이 시 동일 id 재등장) 정상 경로까지 기억하면
# 재기동 후 트래픽이 통째로 차단된다. 상한과 별개로 TTL 도 둬서 오래된 기억은 스스로 지운다.
flushed_wafers = OrderedDict()


def _finite(value, default=None):
    """수치를 표준 JSON 안전값으로 정규화 — NaN·Inf·비수치는 `default` (리뷰 §1-5).

    `json.dumps` 는 NaN/Infinity 를 **비표준 JSON 리터럴**로 직렬화한다. 소비자
    (대시보드 JS `JSON.parse`, sink 의 NOT NULL 수치 컬럼)가 그대로 깨지므로,
    발행 길목에서 한 번에 막는다.

    `float()` 로 변환을 시도하므로 **numpy 스칼라(np.float32 등)도 통과**한다 — AE·XGB
    산출값이 numpy 타입으로 올라오는 순간 "비정상"으로 오판해 실측값을 더미로 바꿔버리는
    사고를 막기 위함. `bool` 은 `int` 서브클래스라, `str` 은 수치 계약 위반이라 먼저 거른다.
    """
    if isinstance(value, (bool, str, bytes)) or value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _load_sensor_map() -> dict:
    """C코드 → 표시명 매핑 로드 (표시 계층 단일 소스 — 헌법 6-4). 실패 시 빈 dict."""
    try:
        import yaml
        with open(SENSOR_MAP_PATH, encoding="utf-8") as f:
            return {str(k): str(v) for k, v in (yaml.safe_load(f) or {}).items()}
    except Exception as e:
        log.warning("sensor_map.yaml 로드 실패 → C코드 그대로 표시: %s", e)
        return {}


SENSOR_MAP = _load_sensor_map()


class Lean85Predictor:
    """lean-85 모델 래퍼 — 로드·추론·SHAP top3. 로드 실패 시 사용 불가(None 반환)."""

    def __init__(self, model_dir: str):
        self.model, self.mani = lp.load_model(model_dir)
        self.lean, _, _ = lp.load_frozen()
        # 명시적 예외 — assert 는 `python -O` 에서 사라진다 (리뷰 §2-2)
        if not (self.mani["features_n"] == len(self.lean) == 85):
            raise ValueError(
                f"모델-피처 스펙 불일치: manifest {self.mani['features_n']} vs frozen {len(self.lean)} (기대 85)")
        self.booster = self.model.get_booster()      # 재사용 (wafer 마다 재취득 방지)
        self._identity_checked = False               # SHAP 합 = 예측값 자기검증 (첫 wafer 1회)
        # SHAP 센서 전용(B안, 2026-07-21): 센서 집계 컬럼(Cxx_stat_stepN)만 — 메타/파생 제외
        self.sensor_cols = set(lp._lean_sensor_cols(self.lean)[0])
        self.model_version = Path(model_dir).name   # 재학습 stamp 폴더명 = 버전 추적 단위 (헌법 4-1·계약 §2-2)
        self.pm_log = Path(PM_LOG_PATH) if PM_LOG_PATH else lp.find_file("pm_log.json")
        log.info("lean-85 로드: %s (학습 %s, 피처 %d) | pm_log=%s",
                 model_dir, self.mani["created_at_local"][:10], len(self.lean), self.pm_log)

    def predict_wafer(self, trace: pd.DataFrame) -> dict:
        """wafer 1장 트레이스 → {pred_c65, low_confidence, shap_top3}.

        **모델 forward 는 1회다** (리뷰 §3-2). `pred_contribs=True` 의 결과는
        `[기여도…, bias]` 이고 **전체 합 = 예측값**이므로, 별도 `predict()` 호출 없이
        예측값과 SHAP 기여도를 한 번에 얻는다. `low_confidence` 판정식은
        `lean85_pipeline.low_confidence_flags()` 단일 소스를 그대로 쓴다.
        """
        table = lp.build_wafer_table(trace, self.pm_log, self.lean)
        _ring_push(table, self.lean)                        # D14 스모크 표본 (설계 §4-8)
        contribs = self._contribs(table)                    # [기여도…, bias]
        pred = float(contribs.sum())                        # = model.predict(table)[0]
        self._verify_contrib_identity(table, pred)          # 최초 1회 자기검증
        low = int(lp.low_confidence_flags(table).iloc[0])
        return {"pred_c65": round(pred, 2),
                "low_confidence": low,
                "shap_top3": self._shap_top3(contribs[:-1]),
                "model_version": self.model_version}

    def _contribs(self, table: pd.DataFrame):
        """XGB native `pred_contribs` 1행 — `[피처별 기여도…, bias]`."""
        import xgboost as xgb
        dm = xgb.DMatrix(table[self.lean], feature_names=list(self.lean))
        return self.booster.predict(dm, pred_contribs=True)[0]

    def predict_batch(self, X: pd.DataFrame):
        """N행 피처 → 예측 배열. **서빙과 완전히 같은 경로**로 낸다 (D14 스모크 전용).

        ⚠️ `self.model.predict(X)`(sklearn API)가 아니라 `booster.predict(pred_contribs)`
        의 **합**이다 — 발행값(`predict_wafer`)이 그 합이기 때문이다. 스모크가 잡아야 할
        결함 1순위가 "게이트는 오케스트레이터 환경에서 채점했는데 consumer 환경의
        xgboost 가 다르다"인데(설계 §4-8), 그 격차는 정확히 `pred_contribs` 계약이
        깨지는 형태로 나타난다. 다른 API 로 검사하면 그 경로를 한 번도 안 태우고 통과시킨다.
        """
        import xgboost as xgb
        dm = xgb.DMatrix(X[self.lean], feature_names=list(self.lean))
        return self.booster.predict(dm, pred_contribs=True).sum(axis=1)

    def _verify_contrib_identity(self, table: pd.DataFrame, pred: float) -> None:
        """`contribs.sum() == model.predict()` 를 **기동 후 첫 wafer 1회만** 대조한다.

        XGBoost 계약상 기여도 총합(= bias 포함)은 raw margin 이고 `reg:squarederror`
        에서는 곧 예측값이라 항상 같아야 한다. 그래도 한 번은 실측으로 확인해 두는 편이
        낫다 — 라이브러리 업그레이드로 이 등식이 깨지면 예측값이 조용히 틀어지기 때문.
        (매 wafer 검증하면 §3-2 절감이 사라지므로 1회로 제한한다.)
        """
        if self._identity_checked:
            return
        self._identity_checked = True
        try:
            ref = float(self.model.predict(table[self.lean])[0])
        except Exception as e:                       # 검증 실패는 추론을 막지 않는다
            log.warning("SHAP 합-예측 등식 자기검증 생략: %s", e)
            return
        if abs(ref - pred) > CONTRIB_IDENTITY_RTOL * max(1.0, abs(ref)):   # 상대 오차
            log.error("⚠️ SHAP 기여도 합(%.6f)과 predict(%.6f) 불일치 — XGBoost 버전 확인 필요. "
                      "발행값은 기여도 합 기준입니다.", pred, ref)
        else:
            log.info("SHAP 합-예측 등식 확인 (Δ=%.2e) — wafer 당 forward 1회로 운영",
                     abs(ref - pred))

    def _shap_top3(self, contribs) -> list:
        """XGB native pred_contribs → **센서 집계 피처만** 센서 단위 합산 상위 N.
        메타/파생(is_high_regime·days_since_last_pm·C33·hour 등)은 제외 — shap_top3는
        물리 센서 전용 (B안, 2026-07-21 결정). SHAP-Nelson 독립: 룰 개입 없음 (헌법 3-3).

        Args:
            contribs: bias 를 제외한 피처별 기여도 (길이 = len(self.lean)).

        정렬·절단은 `normalize_shap_top3`(계약 §2 — |contribution| 내림차순 상위 N)에
        위임한다. **반올림 후 정렬**이라 발행되는 `contribution` 값과 순서가 정확히
        일치한다(소비자가 값으로 순서를 검증할 수 있음)."""
        by_sensor = defaultdict(float)
        for feat, c in zip(self.lean, contribs):
            if feat not in self.sensor_cols:          # 메타/파생 제외 — 센서 집계 컬럼만 (B안)
                continue
            by_sensor[lp.sensor_of(feat)] += float(c)
        # 비유한 기여도는 발행에서 제외 — 표준 JSON 위반이라 wafer 전체 발행이 막힌다 (§1-5)
        ranked = [{"sensor": s, "name": SENSOR_MAP.get(s, s), "contribution": round(c, 4)}
                  for s, c in by_sensor.items() if math.isfinite(c)]
        if len(ranked) < len(by_sensor):
            log.warning("SHAP 기여도 비유한값 %d개 제외", len(by_sensor) - len(ranked))
        return normalize_shap_top3(ranked)          # 정렬 규칙 단일 소스 (계약 §2)


AE_OFFSET_CORRECT = os.environ.get("AE_OFFSET_CORRECT", "0") == "1"  # 8/4 밤 리허설: 챔버 기준선 보정(챔버별 캘리 최소형 — 시뮬 기지 오프셋 차감)
_AE_OFFSETS_CACHE = None              # None=미로드 / dict=확정 (빈 dict = 실패 부정 캐시)
_ae_offset_miss_warned: set = set()   # 오프셋 경고 1회 제한 키 (미등재 챔버·차감 실패 조합)


def _ae_chamber_offsets() -> dict:
    """`config/chamber_offsets.json` → `{chamber: {sensor: offset}}` (1회 로드 캐시).

    **실패해도 예외를 올리지 않는다.** 예외가 상위 AE try 로 새면 설정 문제 하나가
    **wafer 마다** 더미 폴백 + 경고 + 파일 IO 재시도가 되고(캐시가 None 으로 남아
    부정 캐시가 없다), AE 실값 전면 축퇴가 wafer 단위 로그로만 보인다 — 리뷰 M5.
    실패는 **빈 dict 로 확정**하고 ★경고를 1회만 낸다: 보정은 못 하지만 AE 실추론
    (`live`)은 그대로 돌아간다. 보정은 AE 입력 전처리이지 서빙 경로가 아니다.

    `json.loads` 성공은 dict 를 뜻하지 않고(`"1"`·`[1,2]`·`null` 전부 유효 JSON),
    오프셋 값도 수치·유한이 보장되지 않는다 — 비유한 오프셋 하나가 차감 대상 컬럼을
    통째로 NaN 으로 만들어 **AE 스코어를 조용히 오염**시킨다. 그래서 파싱 직후 형을
    보고(헌법 7장), 값은 `_finite` 로 거른 뒤 통과분만 캐시한다.
    """
    global _AE_OFFSETS_CACHE
    if _AE_OFFSETS_CACHE is not None:
        return _AE_OFFSETS_CACHE
    p = AE_OFFSETS_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):                    # json.loads 성공 ≠ dict (헌법 7장)
            raise ValueError(f"최상위가 dict 가 아니다: {type(raw).__name__}")
        offsets = raw.get("offsets")
        if not isinstance(offsets, dict):
            raise ValueError(f"'offsets' 키 부재/비 dict: {type(offsets).__name__}")
        clean, dropped = {}, []
        for chamber, per_sensor in offsets.items():
            if not isinstance(per_sensor, dict):
                dropped.append(f"{chamber}(비 dict)")
                continue
            vals = {}
            for sensor, off in per_sensor.items():
                f = _finite(off)                         # 비수치·NaN·Inf → None
                if f is None:
                    dropped.append(f"{chamber}.{sensor}={off!r}")
                    continue
                vals[str(sensor)] = f
            clean[str(chamber)] = vals
        if dropped:
            log.warning("★AE 챔버 오프셋: 비수치·비유한 항목 %d개 제외 — %s%s",
                        len(dropped), ", ".join(dropped[:5]),
                        " …" if len(dropped) > 5 else "")
        _AE_OFFSETS_CACHE = clean
        log.info("AE 챔버 오프셋 로드: 챔버 %d개 (%s)", len(clean), p)
    except Exception as e:                               # noqa: BLE001 — 설정 문제로 AE 를 막지 않는다
        _AE_OFFSETS_CACHE = {}                           # 부정 캐시 — wafer 마다 재시도하지 않는다
        log.error("★AE 챔버 오프셋 로드 실패 → 보정 없이 AE 실추론만 진행한다 "
                  "(AE_OFFSET_CORRECT=1 인데 %s: %s). 이 경고는 1회뿐 — ae_mode 가 "
                  "'live-decal' 이 아니라 'live' 로 찍히는지로 상태를 확인할 것.", p, e)
    return _AE_OFFSETS_CACHE


def _warn_once(key: str, msg: str, *args) -> None:
    """같은 키의 경고를 프로세스당 1회만 낸다 (wafer 마다 찍으면 로그가 매몰된다)."""
    if key not in _ae_offset_miss_warned:
        _ae_offset_miss_warned.add(key)
        log.warning(msg, *args)


def _ae_decal(trace_ae, chamber_id: str) -> bool:
    """AE 입력 trace 에서 챔버 기준선 오프셋을 제자리 차감. 반환: **실제 차감 여부**.

    반환값이 `ae_mode` 의 `live-decal`/`live` 를 가른다 — 스위치만 보고 'decal' 을
    찍으면 로드 실패·미등재 챔버에서 **보정이 안 걸렸는데 걸린 것처럼** 기록된다.

    `_ae_chamber_offsets()` 와 **같은 계약으로 예외를 올리지 않는다.** 차감은
    `Series - float` 라 컬럼 dtype 이 object·문자열·datetime 이면 `TypeError` 가 나고,
    그게 상위 AE try 로 새면 **M5 와 똑같은 증상**(wafer 마다 더미 폴백)이 된다 —
    가드를 로더에만 걸고 형제 함수를 비워두면 결함이 옆으로 이동할 뿐이다.
    실패한 센서는 건너뛰고(그 컬럼만 무보정) 나머지 차감은 그대로 진행한다.
    """
    table = _ae_chamber_offsets()
    offs = table.get(chamber_id)
    if not offs:
        # "챔버 자체가 없다" 와 "등재됐지만 유효 오프셋이 0개(전부 탈락)" 를 구분한다
        # — 개통 체크리스트의 '4챔버 전부 등재 확인' 을 로그로 판정하기 때문.
        _warn_once(f"miss:{chamber_id}",
                   "★AE 챔버 오프셋 %s — chamber=%s 는 보정 없이 간다 (%s 확인). 챔버당 1회 경고.",
                   "유효값 0개" if chamber_id in table else "미등재", chamber_id, AE_OFFSETS_PATH)
        return False
    hit = 0
    for sensor, off in offs.items():
        if sensor not in trace_ae.columns:
            continue
        try:
            trace_ae[sensor] = trace_ae[sensor] - off
        except Exception as e:                       # noqa: BLE001 — 한 컬럼 실패가 wafer 를 죽이지 않는다
            _warn_once(f"sub:{chamber_id}.{sensor}",
                       "★AE 오프셋 차감 실패 — chamber=%s sensor=%s 는 무보정으로 간다 "
                       "(dtype 확인): %s. 이 조합당 1회 경고.", chamber_id, sensor, e)
            continue
        hit += 1
    return hit > 0


class AEPredictor:
    """ae_v3 AE 래퍼 — 번들 로드 + wafer 스코어링(anomaly·drift). 로드 실패 시 None.

    per-chamber 드리프트 EWMA 상태는 AEModel 인스턴스가 보유 — 이 래퍼는 프로세스
    수명 동안 단일 인스턴스로 유지돼야 한다(챔버별 drift 연속성 — 계획 §4 D-2).
    AE는 직접 알람권 없음 — fdc.prediction 발행만 (헌법 1-2).
    """

    def __init__(self, bundle_dir: str):
        # 지연 import (torch 의존 — 기동 안전망: 실패 시 상위에서 더미 폴백)
        sys.path.insert(0, str(Path(__file__).resolve().parent / "Autoencoder"))
        from ae_pipeline.infer import AEModel
        self.ae = AEModel.from_bundle(bundle_dir)
        self.bundle_version = Path(bundle_dir).name
        log.info("AE 번들 로드: %s (model=%s, calib=%s)", bundle_dir,
                 self.ae.model_version, self.ae.calib_version)

    def score_wafer(self, trace_ae: pd.DataFrame) -> dict:
        """wafer 1장 트레이스 → AE 출력 레코드(ae_score·ae_drift_score·ae_raw·...).

        빈 트레이스(파싱 0건)면 {} 반환 → 상위에서 더미 폴백.
        """
        if trace_ae.empty:
            return {}
        recs = self.ae.score_wafers(trace_ae, chamber_col="C24")
        return recs[0] if recs else {}


def _parse_ts(value) -> pd.Timestamp:
    """타임스탬프 1회 파싱 → tz-naive Timestamp (리뷰 §2-5·3-3 — 이중 파싱 제거).

    파싱 실패는 호출측이 **행 단위로** 처리하도록 예외를 그대로 올린다.
    """
    ts = pd.Timestamp(value)
    
    if pd.isna(ts):                       # None/NaT → 행 스킵 경로로
        raise ValueError("timestamp가 None/NaT")
    
    return ts.tz_localize(None) if ts.tzinfo is not None else ts


def rows_to_trace(wafer_id: str, rows: list) -> pd.DataFrame:
    """fdc.raw 버퍼 → lean85_pipeline이 요구하는 트레이스 스키마 [C64,C20,C6,C7,C33,C40,센서…].

    C40(원본 타임스탬프)은 스트림에 없음(§6-2 안건) — TIME_SOURCE 스위치:
      · src_ts     : 시뮬레이터가 원본 시각 필드를 추가한 경우 (계약 §2-2 후)
      · wall_clock : 수신 timestamp 사용 (데모 압축시간에선 시간 피처 왜곡 가능 — 경고)

    timestamp 가 깨진 row 는 **해당 행만 스킵 + 로그** 한다 (헌법 6-2 · `rows_to_trace_ae`
    의 C42 결측 처리와 같은 패턴). 예전엔 wafer 전체가 더미 폴백으로 떨어졌다(리뷰 §2-5).
    """
    recs, skipped = [], 0
    for r in rows:
        try:
            src = r["src_ts"] if (TIME_SOURCE == "src_ts" and r.get("src_ts")) else r["timestamp"]
            ts = _parse_ts(src)
        except Exception:                              # 파싱 불능 → 행 스킵 (wafer 는 살린다)
            skipped += 1
            continue
        rec = dict(r["sensors"])                       # C33 포함 (message_builder 보장)
        rec["C64"] = wafer_id
        rec["C20"] = r.get("lot_id", "LOT_UNKNOWN")
        rec["C6"] = r.get("recipe_id", "C6_0")
        rec["C7"] = r.get("step")
        rec["C40"] = ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # FMT 정합 (밀리초 3자리)
        recs.append(rec)
    if skipped:
        log.warning("lean85 trace: timestamp 파싱 실패 %d row 스킵 (wafer=%s)", skipped, wafer_id)
    return pd.DataFrame(recs)


def rows_to_trace_ae(wafer_id: str, meta: dict, rows: list) -> pd.DataFrame:
    """fdc.raw 버퍼 → ae_pipeline 트레이스 스키마 [C64,C7,C42,C46,C10,C24 + 물리센서].

    lean-85 rows_to_trace와 별개 (AE는 C42·C46·C24가 추가로 필요 — 계획 §2-3 매핑):
      · C64 ← wafer_id · C7 ← step · C42 ← stabilization_flag · C46 ← seq_in_step
      · C10 ← 수신 timestamp (정렬 전용 — AE는 시간 피처 없음) · C24 ← chamber_id
    C24는 원본이 상수라 fdc.raw에서 드롭됨 → 가상 챔버(SIM_CH_N)를 DriftTracker 키로.
    C42(stabilization_flag) 결측 row는 마스크 판정 불능 → 스킵+로그 (헌법 6-2 준용, §5-6).
    timestamp 파싱 실패 row 도 동일하게 스킵한다 (리뷰 §2-5).
    """
    chamber = meta.get("chamber_id", "default")
    recs, skipped, bad_ts = [], 0, 0
    for r in rows:
        if r.get("stabilization_flag") is None:      # C42 결측 → 정착/과도 마스크 불능
            skipped += 1
            continue
        try:
            ts = _parse_ts(r["timestamp"])           # 정렬용 tz-naive (1회 파싱)
        except Exception:
            bad_ts += 1
            continue
        rec = dict(r["sensors"])                     # 물리센서 C코드 (C33 포함)
        rec["C64"] = wafer_id
        rec["C7"] = r.get("step")
        rec["C42"] = int(r["stabilization_flag"])
        rec["C46"] = r.get("seq_in_step")
        rec["C24"] = chamber
        rec["C10"] = ts
        recs.append(rec)
    if skipped or bad_ts:
        log.warning("AE trace: C42 결측 %d · timestamp 불량 %d row 스킵 (wafer=%s)",
                    skipped, bad_ts, wafer_id)
    return pd.DataFrame(recs)


def dummy_prediction(rows: list) -> dict:
    """더미 폴백 (시연 안전망 — 계획 §3-2 ①). 기존 mock 로직 유지.

    step4 rows 가 있어도 C17 이 전부 NaN 이면 `mean()` 이 NaN 이 된다 → 발행 페이로드가
    비표준 JSON 이 되므로 `_finite()` 로 0.0 폴백한다 (리뷰 §1-5).
    """
    df = pd.DataFrame([{"step": r.get("step"), **r["sensors"]} for r in rows])
    s4 = df[df["step"] == 4] if "step" in df.columns else df.iloc[0:0]
    c17 = _finite(s4["C17"].mean() if (not s4.empty and "C17" in s4) else 0.0, default=0.0)
    base = DUMMY_C65_BASE + c17 * DUMMY_C17_COEF
    return {"pred_c65": round(base + random.uniform(-DUMMY_C65_JITTER, DUMMY_C65_JITTER), 2),
            "low_confidence": 0,
            "model_version": "dummy",              # 더미 폴백 식별 (실모델 아님)
            "shap_top3": [
                {"sensor": "C17", "name": SENSOR_MAP.get("C17", "C17"), "contribution": 0.4},
                {"sensor": "C12", "name": SENSOR_MAP.get("C12", "C12"), "contribution": 0.2},
                {"sensor": "C61", "name": SENSOR_MAP.get("C61", "C61"), "contribution": 0.15}]}


def build_message(wafer_id: str, meta: dict, pred: dict, ae_rec: dict | None = None,
                  bias: float = 0.0) -> dict:
    """fdc.prediction 페이로드 (API Contract §2 — 변경 금지 필드 준수).

    drift_score·anomaly_score는 ae_rec(실값) 있으면 실값, 없으면 더미 폴백(안전망).
    1단계는 기존 필드에 주입(스키마 불변). AE 확장 필드는 AE_PUBLISH_EXT 승인 후에만 발행.

    **shap_top3 정렬 보장**: 여기가 발행 페이로드 조립의 단일 길목이라, 어떤 산출
    경로(실모델·더미 폴백·향후 추가분)를 거쳤든 `normalize_shap_top3` 를 통과한다.
    소비자(B publisher → `fdc.alert.prediction_context`)는 **재정렬 없이 순서를 그대로
    신뢰**하면 된다 — 재정렬은 캐시 원본의 SHAP 순위를 건드릴 위험을 만든다(헌법 3-3).

    **표준 JSON 보장**: 같은 이유로 수치 필드는 전부 `_finite()` 를 통과한다. NaN/Inf 가
    실모델 경로(피처 전량 NaN 등)에서 새더라도 여기서 막힌다 (리뷰 §1-5 — 발행 길목 단일 지점).
    """
    if ae_rec:                          # AE 실값 (A3-3 실연동 — TODO 해소)
        drift_score = _finite(ae_rec.get("ae_drift_score"))
        anomaly_score = _finite(ae_rec.get("ae_score"))
        drift_score = round(drift_score, 4) if drift_score is not None else None
        anomaly_score = round(anomaly_score, 4) if anomaly_score is not None else None
        if drift_score is None or anomaly_score is None:
            log.warning("AE 산출값 비정상(NaN/Inf) → 더미 폴백 (wafer=%s)", wafer_id)
            ae_rec = None
    if not ae_rec:                      # 더미 폴백 (AE 미가동/실패 — 파이프라인 생존)
        drift_score = round(random.uniform(*DUMMY_DRIFT_RANGE), 2)
        anomaly_score = round(random.uniform(*DUMMY_ANOMALY_RANGE), 2)

    predicted_c65 = _finite(pred.get("pred_c65"))
    if predicted_c65 is None:           # 표준 JSON 위반 차단 — 다운스트림 파싱·NOT NULL 보호
        log.error("predicted_c65 가 비정상(NaN/Inf/비수치) → %s 로 대체 발행 (wafer=%s)",
                  DUMMY_C65_BASE, wafer_id)
        predicted_c65 = DUMMY_C65_BASE

    # ── CT⓪ Model R2R 가산 (설계 §4·D1) ──
    # 순서가 계약이다: **SHAP 산출·정렬이 끝난 raw 예측에** 가산항을 더한다.
    #   · `shap_top3` 는 raw 예측의 설명 그대로다 (설계 원칙 7 · 헌법 3-3 SHAP 독립 채널)
    #   · 항등식 `contribs 합 = predicted_c65 − bias_applied` 가 유지돼 소비자·감사 쪽에서
    #     보정 전 값을 언제든 복원할 수 있다 (D1 — 창 재계산이 이 복원에 의존한다)
    bias_applied = _finite(bias) or 0.0
    if bias_applied:
        # round(2): `predicted_c65` 는 계약상 소수 2자리 관례다. 가산 후 재반올림하지 않으면
        # 부동소수 잔차(112.67999999999999)가 그대로 발행돼 표시·비교에서 노이즈가 된다.
        predicted_c65 = round(predicted_c65 + bias_applied, 2)

    msg = {
        "wafer_id": wafer_id,
        "chamber_id": meta.get("chamber_id"),
        "predicted_c65": predicted_c65,
        "drift_score": drift_score,       # ae_drift_score [0,1] (EWMA) — 실값(live) 또는 더미
        "anomaly_score": anomaly_score,   # ae_score [0,1] (0.2=Qual 임계) — 실값(live) 또는 더미
        # 정렬 보장 단일 지점 — |contribution| 내림차순 상위 N (shap_contract, 계약 §2)
        "shap_top3": normalize_shap_top3(pred.get("shap_top3")),
        # ★`spc_flags` 를 **싣지 않는다** (M1, 2026-08-05 — 구 `"spc_flags": []`).
        #   Nelson 판정은 B 소유이고 이미 `fdc.alert.prediction_context.spc_flags` 로 발행된다.
        #   A 가 빈 배열을 실으면 `prediction_sink` 의 `COALESCE(%s, spc_flags)` 에서 `[]` 가
        #   **NULL 이 아닌 값**이라 통과해, B 가 채워 넣은 플래그를 **다음 예측이 도착하는
        #   순간 덮어쓴다**. 그러면 `ct2_deploy_monitor` 의 감시①(SPC↑/AE 침묵 교차)이
        #   n_spc=0 으로 영구 "판정 유예"가 된다 — 감시가 살아있는 척 죽어 있는 상태.
        #   A 가 Nelson 을 자체 계산하는 것은 금지다 (헌법 1-2 alert 채널 · 3-3 SHAP 독립 채널).
        "is_qual": bool(meta.get("is_qual", False)),
        # 🆕 계약 §2-2 추가 필드 — 예측 출처 추적(4-1). lot_id·recipe_id는 wafer_id 종속이라
        #   이벤트에 싣지 않고 wafer 마스터 조인으로 해석(현업 정규화 — 2026-07-23 결정).
        "model_version": pred.get("model_version"),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    # 🆕 계약 §2 `bias_applied` (CT⓪) — **보정 경로가 살아 있을 때만** 싣는다.
    #   mode=off 면 필드를 아예 내지 않아 현행 페이로드와 100% 동일하다 (컷오버 전 무영향).
    #   반대로 보정이 켜졌는데 이 필드가 없으면 sink 가 raw 를 복원하지 못해 **창이
    #   보정값으로 오염**된다 (D1) — 그래서 '적용'과 '발행'을 같은 스위치에 묶는다.
    if CT0_MODE != ct0_core.MODE_OFF:
        msg["bias_applied"] = round(bias_applied, 2)
    if PUBLISH_LOW_CONF:               # 🆕 필드 — 계약 §2-2 승인 후에만 발행
        msg["low_confidence"] = pred["low_confidence"]
    if AE_PUBLISH_EXT and ae_rec:      # 🆕 AE 확장 필드 (2단계 — 계약 §2-2 승인 후에만 발행)
        ae_raw = _finite(ae_rec.get("ae_raw"))
        msg["ae_raw"] = round(ae_raw, 4) if ae_raw is not None else None
        msg["ae_top_channels"] = ae_rec.get("ae_top_channels")
        msg["ae_model_version"] = ae_rec.get("ae_model_version")
        msg["ae_calib_version"] = ae_rec.get("ae_calib_version")
        msg["transient_context"] = ae_rec.get("transient_context")
    return msg


def _on_delivery(err, m) -> None:
    """Producer 전달 결과 콜백 — 실패를 가시화한다 (리뷰 §2-1).

    콜백이 없으면 브로커 전달 실패가 아무 흔적 없이 사라진다(`flush()` 는 종료 시 1회뿐).
    """
    if err is not None:
        key = m.key().decode("utf-8", "replace") if m and m.key() else None
        log.error("발행 실패 (key=%s): %s", key, err)


def _produce(producer, wafer_id: str, msg: dict) -> bool:
    """`fdc.prediction` 발행 (BufferError 1회 재시도). 반환: 큐 적재 성공 여부.

    `produce()` 는 로컬 큐 포화 시 `BufferError` 를 던진다 — 잡지 않으면 프로세스가 죽는다.
    """
    payload = json.dumps(msg, ensure_ascii=False, allow_nan=False).encode()
    try:
        producer.produce(TOPIC_OUT, key=wafer_id.encode(), value=payload,
                         on_delivery=_on_delivery)
    except BufferError:
        log.error("Producer 큐 포화 → flush 후 1회 재시도 (wafer=%s)", wafer_id)
        producer.flush(PRODUCER_FLUSH_SEC)
        try:
            producer.produce(TOPIC_OUT, key=wafer_id.encode(), value=payload,
                             on_delivery=_on_delivery)
        except BufferError as e:                     # 재시도도 실패 → 이 wafer 만 포기
            log.error("Producer 큐 포화 지속 → 발행 포기 (wafer=%s): %s", wafer_id, e)
            return False
    except Exception as e:                           # 직렬화·기타 실패 → wafer 만 포기
        log.error("발행 실패 (wafer=%s): %s", wafer_id, e)
        return False
    producer.poll(0)                                 # 전달 콜백 처리
    return True


def _remember_idle_flush(wafer_id: str, now: float | None = None) -> None:
    """유휴 flush 로 발행한 wafer 를 기억 (지각 row 재발행 차단). 상한·TTL 로 스스로 정리."""
    now = time.monotonic() if now is None else now
    ttl = max(IDLE_FLUSH_SEC, 1.0) * FLUSHED_MEMORY_TTL_FACTOR
    for wid in [w for w, (ts, _) in flushed_wafers.items() if now - ts > ttl]:
        flushed_wafers.pop(wid, None)
    flushed_wafers[wafer_id] = (now, False)
    flushed_wafers.move_to_end(wafer_id)
    while len(flushed_wafers) > FLUSHED_MEMORY_MAX:
        flushed_wafers.popitem(last=False)


def flush_wafer(wafer_id: str, predictor, ae_predictor, producer,
                mark_flushed: bool = False) -> None:
    """완료 wafer 예측·발행. lean-85·AE 각각 독립 폴백(한쪽 실패가 다른쪽을 죽이지 않음 — 헌법 6-2).

    Args:
        mark_flushed: True 면 발행 성공 후 `flushed_wafers` 에 등록해 지각 row 재발행을
            막는다. **유휴 flush 경로 전용** — 정상 교체 flush 는 등록하지 않는다
            (시뮬레이터 재기동 시 같은 wafer_id 가 다시 오면 정상 트래픽이 차단되므로).
    """
    rows = wafer_buffer.pop(wafer_id, [])
    meta = chamber_meta.pop(wafer_id, {})
    wafer_last_seen.pop(wafer_id, None)
    if chamber_current.get(meta.get("chamber_id")) == wafer_id:
        chamber_current.pop(meta["chamber_id"], None)   # 상태 딕셔너리 동반 정리 (리뷰 §3-1)
    if not rows:
        return
    # ── lean-85 C65 예측 (독립 폴백) ──
    mode = "lean85"
    try:
        if predictor is None:
            raise RuntimeError("PREDICTION_MODE=dummy 또는 모델 미로드")
        pred = predictor.predict_wafer(rows_to_trace(wafer_id, rows))
    except Exception as e:
        mode = "dummy_fallback"
        log.warning("lean-85 추론 실패 → 더미 폴백 (wafer=%s): %s", wafer_id, e)
        pred = dummy_prediction(rows)

    # ── AE drift/anomaly 스코어링 (독립 폴백 — lean-85와 무관) ──
    ae_rec, ae_mode = None, "dummy"
    if ae_predictor is not None:
        try:
            trace_ae = rows_to_trace_ae(wafer_id, meta, rows)
            decal = False
            if AE_OFFSET_CORRECT and not trace_ae.empty:
                # 기본값은 `rows_to_trace_ae` 의 C24 기본값과 **같아야** 한다 — 어긋나면
                # trace 의 챔버와 오프셋 조회 키가 갈리고 로그에 빈 챔버명이 찍힌다.
                decal = _ae_decal(trace_ae, meta.get("chamber_id") or "default")
            ae_rec = ae_predictor.score_wafer(trace_ae)
            ae_mode = (("live-decal" if decal else "live") if ae_rec else "dummy(빈trace)")
        except Exception as e:
            log.warning("AE 스코어링 실패 → drift/anomaly 더미 폴백 (wafer=%s): %s", wafer_id, e)
            ae_rec = None

    # CT⓪ bias 조회는 **메모리 캐시 참조**다 (파일 IO 는 poll 루프의 주기 스캔에서만).
    # 조회 실패는 캐시가 0 으로 흡수한다 — 보정이 예측 발행을 막는 경로는 없다 (설계 원칙 6).
    msg = build_message(wafer_id, meta, pred, ae_rec, bias=ct0_bias_for(meta))
    if not _produce(producer, wafer_id, msg):
        return                                          # 발행 실패 → 기억하지 않는다(만회 여지 유지)
    if mark_flushed:
        _remember_idle_flush(wafer_id)
    lc = pred["low_confidence"]
    bias_note = ""
    if msg.get("bias_applied"):
        bias_note = f" | bias={msg['bias_applied']:+.2f} (CT⓪)"
    log.info("🤖 [%s] %s | rows=%d | C65=%.1f | ae=%.3f drift=%.3f (%s)%s%s → %s",
             mode, wafer_id, len(rows), msg["predicted_c65"],
             msg["anomaly_score"], msg["drift_score"], ae_mode,
             " | low_confidence=1 (레짐-온셋 — 참고용)" if lc else "", bias_note, TOPIC_OUT)


def flush_idle_wafers(predictor, ae_predictor, producer, now=None) -> int:
    """`IDLE_FLUSH_SEC` 동안 새 row 가 없는 wafer 를 완료로 간주해 flush (리뷰 §3-1).

    flush 는 원래 "같은 chamber 에 다음 wafer 가 도착"할 때만 일어나므로, 챔버가 멈추면
    마지막 wafer 의 rows 가 영원히 메모리에 남는다(장기 실행 시 누적). 유휴 구간
    (`consumer.poll()` 이 None)에 호출해 버퍼를 회수한다. `IDLE_FLUSH_SEC=0` 이면 비활성.

    Returns:
        flush 된 wafer 수.
    """
    if IDLE_FLUSH_SEC <= 0:
        return 0
    now = time.monotonic() if now is None else now
    stale = [w for w, ts in wafer_last_seen.items() if now - ts >= IDLE_FLUSH_SEC]
    for wafer_id in stale:
        log.info("유휴 flush: %s (%.0fs 무수신 — 완료로 간주)",
                 wafer_id, now - wafer_last_seen.get(wafer_id, now))
        flush_wafer(wafer_id, predictor, ae_predictor, producer, mark_flushed=True)
    return len(stale)


def handle_message(data: dict, predictor, ae_predictor, producer) -> None:
    """fdc.raw 1건 처리: 버퍼링 + 같은 chamber에서 wafer 교체 감지 시 이전 wafer flush."""
    wafer_id, chamber = data["wafer_id"], data["chamber_id"]
    if wafer_id in flushed_wafers:      # 유휴 flush 완료 → 지각 row (중복 발행·덮어쓰기 방지)
        ts, logged = flushed_wafers[wafer_id]
        if not logged:                  # wafer 당 1회만 경고 (row 수만큼 찍히면 로그 폭주)
            log.warning("유휴 flush 완료 wafer 의 지각 row 무시: %s "
                        "(이 wafer 의 후반부 트레이스는 예측에 반영되지 않음 — params "
                        "`lean85.idle_flush_sec`=%.0fs 상향 검토. 단 B "
                        "`spc.wafer_idle_flush_sec` 와 짝이라 함께 봐야 한다)",
                        wafer_id, IDLE_FLUSH_SEC)
            flushed_wafers[wafer_id] = (ts, True)
        return
    prev = chamber_current.get(chamber)
    if prev is not None and prev != wafer_id:
        flush_wafer(prev, predictor, ae_predictor, producer)     # 이전 wafer 완료로 간주
    chamber_current[chamber] = wafer_id
    chamber_meta.setdefault(wafer_id, {"chamber_id": chamber,
                                       "is_qual": data.get("is_qual", False)})
    wafer_last_seen[wafer_id] = time.monotonic()                 # 유휴 flush 판정 기준
    wafer_buffer[wafer_id].append({
        "step": data["step"], "sensors": data["sensors"],
        "lot_id": data.get("lot_id"), "recipe_id": data.get("recipe_id"),
        "timestamp": data["timestamp"], "src_ts": data.get("src_ts"),
        "seq_in_step": data.get("seq_in_step"),               # AE: C46 (dedup 가드·정렬)
        "stabilization_flag": data.get("stabilization_flag"),  # AE: C42 (정착/과도 마스크)
    })


def _resolve_model_dir() -> str:
    """lean-85 stamp 폴더 경로 해석 — **기동 부트스트랩 전용** (헌법 6-4: 경로 하드코딩 금지).

    우선순위: env LEAN85_MODEL_DIR > `control/ct1/champion.json`(운영 포인터)
              > params.yaml[lean85].model_search_glob > 기본 glob.

    ⚠️ `sorted(glob)[-1]` **이름 사전순** 선택은 `_tag` 접미에 취약하다 (CT1 설계 G6).
    그래서 CT① 배선 이후 **운영 중 모델 교체의 정본은 promote 신호**(`_check_ct1_promote`)이고,
    glob 은 포인터가 없는 최초 기동에서만 쓰는 폴백으로 강등됐다 (설계 D5).
    """
    if MODEL_DIR:
        return MODEL_DIR
    ptr = REPO_ROOT / "control" / "ct1" / "champion.json"     # 운영 champion 포인터 (CT1 D5)
    if ptr.exists():
        try:
            d = json.loads(ptr.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("model_dir") and Path(d["model_dir"]).exists():
                log.info("champion 포인터 사용: %s", d["model_dir"])
                return str(d["model_dir"])
            log.warning("champion 포인터가 dict/실존 경로 아님 → glob 폴백: %s", ptr)
        except Exception as e:                               # noqa: BLE001
            log.warning("champion 포인터 판독 실패 → glob 폴백: %s", e)
    glob_pat = "models/c65_predictor/*/lean85_*"
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            g = ((yaml.safe_load(f) or {}).get("lean85") or {}).get("model_search_glob")
        if g:
            glob_pat = g
    except Exception as e:
        log.warning("params.yaml model_search_glob 로드 실패 → 기본 glob 사용: %s", e)
    matches = sorted(REPO_ROOT.glob(glob_pat))
    if not matches:
        raise FileNotFoundError(f"모델 stamp 폴더 없음 (glob={glob_pat})")
    return str(matches[-1])


def _init_predictor():
    """PREDICTION_MODE·모델 경로 해석 → Lean85Predictor 또는 None(더미)."""
    if PREDICTION_MODE != "lean85":
        log.warning("PREDICTION_MODE=%s → 더미 예측으로 기동", PREDICTION_MODE)
        return None
    try:
        return Lean85Predictor(_resolve_model_dir())
    except Exception as e:
        log.error("모델 로드 실패 → 더미 폴백 기동: %s", e)
        return None


def _resolve_ae_bundle() -> str:
    """AE 번들 폴더 경로 해석 (헌법 6-4: 경로 하드코딩 금지).
    우선순위: env AE_BUNDLE_DIR > params.yaml[anomaly_ae].bundle_search_glob > 기본 glob.
    타입-우선 구조(헌법 4-1): models/anomaly_ae/ae_* 중 최신."""
    if AE_BUNDLE_DIR:
        return AE_BUNDLE_DIR
    glob_pat = "models/anomaly_ae/ae_*"
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            g = ((yaml.safe_load(f) or {}).get("anomaly_ae") or {}).get("bundle_search_glob")
        if g:
            glob_pat = g
    except Exception as e:
        log.warning("params.yaml anomaly_ae.bundle_search_glob 로드 실패 → 기본 glob 사용: %s", e)
    matches = sorted(REPO_ROOT.glob(glob_pat))
    if not matches:
        raise FileNotFoundError(f"AE 번들 폴더 없음 (glob={glob_pat})")
    return str(matches[-1])


def _init_ae_predictor():
    """LEAN85_AE_MODE·번들 경로 해석 → AEPredictor 또는 None(더미 drift/anomaly).
    로드 실패해도 기동은 계속 (더미 폴백 — 시연 안전망, 계획 §3-2 ①)."""
    if AE_MODE != "live":
        log.info("LEAN85_AE_MODE=%s → drift/anomaly 더미값 (AE 미가동)", AE_MODE)
        return None
    try:
        return AEPredictor(_resolve_ae_bundle())
    except Exception as e:
        log.error("AE 번들 로드 실패 → drift/anomaly 더미 폴백 기동: %s", e)
        return None


def _ct1_promote_dir() -> Path:
    """CT① 승격 신호 디렉토리 (env 우선, 기본 control/ct1 — 설계 D5)."""
    return CT1_PROMOTE_DIR if str(CT1_PROMOTE_DIR) not in ("", ".") else REPO_ROOT / "control" / "ct1"


def _reload_smoke(new_predictor) -> tuple[bool, str]:
    """D14 리로드 스모크 — 스왑 **직전** 최근 wafer 피처로 1회 예측 (설계 v2 §4-8).

    게이트(`validate_lean85`)는 조립 CSV 축에서 **라벨로** 채점한다. 서빙 스트림 축의
    즉시 결함(전량 NaN·상수 출력·범위 이탈)은 라벨이 도착할 때까지 어떤 게이트도 못 본다.
    WP-7(배포 후 감시·자동 롤백) 폐지의 대가로 확정된 장치라 여기가 마지막 방어선이다.

    반환 `(ok, 사유)`. **미충전은 실패가 아니라 생략**이다 — 기동 직후 promote 를 영구
    교착시키면 배포 경로 자체가 죽는다. 단 경고로는 남긴다(적재 영구 실패와 구분).
    """
    rows = list(_feature_ring)
    if len(rows) < CT1_SMOKE_MIN:
        log.warning("D14 스모크 생략 — 링버퍼 미충전(%d/%d). 표본이 계속 안 쌓이면 "
                    "_ring_push 적재 실패를 의심할 것", len(rows), CT1_SMOKE_MIN)
        return True, f"SKIP(버퍼 미충전 {len(rows)}/{CT1_SMOKE_MIN})"

    X = pd.DataFrame(np.vstack(rows), columns=list(new_predictor.lean))
    y = np.asarray(new_predictor.predict_batch(X), dtype=float)

    # ⓐ 유한값 — NaN/Inf 는 발행 길목의 `_finite()` 가 삼켜서 조용한 상수 발행이 된다
    n_bad = int((~np.isfinite(y)).sum()) if y.size else 0
    if y.size != len(rows) or n_bad:
        return False, f"ⓐ 유한값 위반 (n={y.size}/{len(rows)}, 비유한={n_bad})"

    # ⓑ 분산 — 입력이 서로 다른데 출력이 하나면 부스터 손상 신호다. 단 **입력이 전부
    #   같으면 건강한 모델도 상수를 낸다** — 그 구간까지 막으면 오탐 교착이다.
    arr = X.to_numpy(dtype=float)
    spread = np.nan_to_num(np.nanmax(arr, axis=0) - np.nanmin(arr, axis=0), nan=0.0)
    inputs_vary = bool(np.any(spread > 0))
    if inputs_vary and float(np.std(y)) <= 0.0:
        return False, f"ⓑ 분산 0 — 입력이 다른데 상수 출력({y[0]:.4g})"

    # ⓒ 절대범위 — 단일 소스는 `validate_lean85.DEFAULTS`(설계 §6: config 키 남발 방지).
    #   로드 실패 시 CT1_SMOKE_RANGE=None 이고 ⓐⓑ 만으로 진행한다(기동 시 경고 1회).
    if CT1_SMOKE_RANGE:
        lo, hi = CT1_SMOKE_RANGE
        if float(y.min()) < lo or float(y.max()) > hi:
            return False, (f"ⓒ 절대범위 이탈 [{y.min():.4g}, {y.max():.4g}] "
                           f"⊄ [{lo:g}, {hi:g}]")

    note = "" if inputs_vary else " (입력 무변화 — ⓑ 생략)"
    return True, f"OK n={len(rows)} 예측 [{y.min():.4g}, {y.max():.4g}]{note}"


SIGNAL_KEEP_RETRY_MAX = 100        # superseded 파일명 충돌 회피 시도 상한 (무한루프 방지)

# 마지막으로 **선택된**(= 나머지를 대체한) 신호의 시각. 선택 시점에 갱신하며, 이보다
# 오래된 신호는 두 번 다시 적용하지 않는다 — superseded 이동이 실패해 구 신호가 큐에
# 잔류해도 S10("롤백이 큐잉된 구 promote 에 뒤집힌다")이 재발하지 않게 하는 **정본
# 가드**다. 파일 이동은 감사 보관이지 가드가 아니다 (이동은 실패할 수 있다).
# 프로세스 재기동 시 소실 — 그때는 champion.json 부트스트랩이 기준이고 잔류 신호는
# 다음 스캔에 정상 판정된다 (수용 — 설계 §4-5 수동 복구 경로와 같은 수준).
_ct1_signal_watermark = None


def _signal_sort_key(sig: Path):
    """promote 신호 정렬 키 — payload `promoted_at` 우선, 부재·판독 불가 시 파일 mtime.

    파일명 정렬을 쓰지 않는 이유: `-rollback` 접미가 `.json` 보다 사전순으로 앞서
    (0x2D < 0x2E) **롤백이 구 promote 보다 먼저** 정렬된다 — S10 사고의 재현 경로다.
    naive/aware 혼재는 비교 시점 TypeError 이므로 (헌법 7장) 전부 aware UTC 로 통일한다.

    시각 **동률**이면 롤백이 이긴다 (키 2번째 요소 — 2026-08-07 리뷰 F9). `promoted_at`
    은 1초, mtime 은 커널 틱(수 ms) 해상도라 연속 드롭이 같은 값을 받는 동률이 실측됐고,
    그때 이름 타이브레이크는 `….json`(0x2E) > `…-rollback.json`(0x2D) 이라 **항상
    롤백이 진다.** 롤백은 사람의 복구 개입이다 — 시간으로 구분 불가능한 동률에서
    구(불량) promote 에 지면 안 된다. rb 를 mtime **앞**에 두는 이유: mtime 은 복사·
    코스 클럭으로 신뢰도가 낮은 신호라, 같은 초 안의 순서 판정을 mtime 에 맡기지
    않는다 (같은 초 = 시간으로 구분 불가로 본다). 워터마크(`key[0]`)는 ts 그대로다.
    """
    ts = None
    rb = False                                             # 판독 불가 시 False (비-롤백 취급)
    try:
        d = json.loads(sig.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            rb = bool(d.get("rollback"))                   # promoted_at 부재(수기)여도 추출 —
            if d.get("promoted_at"):                       #   동률이 실제로 나는 곳이 그 경로다
                t = datetime.fromisoformat(str(d["promoted_at"]).replace("Z", "+00:00"))
                ts = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:                                      # noqa: BLE001 — 판독 불가 = mtime 폴백
        pass
    try:
        mtime_ns = sig.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    if ts is None:
        ts = datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc)
    return (ts, rb, mtime_ns, sig.name)                    # 동률 시 롤백 승 (F9)


def _move_signal(sig: Path, dest: Path, *, keep_existing: bool = False) -> bool:
    """처리 완료 신호 이동 (processed/·failed/·superseded/ — InjectControl 패턴).

    `rename` 이 아니라 `replace` 다 — Windows 는 대상이 이미 있으면 rename 이 죽는데,
    같은 ct_id 가 재드롭되는 상황(수동 재시도)에서 그 예외가 신호를 원위치에 남겨
    **매 스캔마다 같은 실패를 반복**하게 만든다. 이동 실패는 흐름을 막지 않는다.

    `keep_existing=True`(superseded 전용)면 **덮어쓰지 않는다** — `processed/`·`failed/`
    는 "이 ct_id 를 어떻게 처리했나"의 결과라 최신이 이기는 게 맞지만, `superseded/` 는
    "그때 큐에 무엇이 있었나"의 **감사 보관**이다 (설계 v2 §4-5 "감사 보관, 삭제 없음").
    같은 ct_id 를 사람이 재드롭하는 수동 경로(§4-5)에서 앞 기록이 소리 없이 사라진다.

    Returns:
        이동 성공 여부 — 감사 보관 실패를 호출부가 드러낼 수 있게 (S10 잔류 위험).
    """
    try:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / sig.name
        if keep_existing and target.exists():
            try:                                           # mtime = 안정적·정렬 가능한 구분자
                stamp = sig.stat().st_mtime_ns
            except OSError:
                stamp = 0
            target = dest / f"{sig.stem}.{stamp}{sig.suffix}"
            n = 0
            while target.exists() and n < SIGNAL_KEEP_RETRY_MAX:
                n += 1
                target = dest / f"{sig.stem}.{stamp}-{n}{sig.suffix}"
        sig.replace(target)
        return True
    except Exception as e:                                 # noqa: BLE001
        log.warning("CT① 신호 이동 실패(%s → %s/): %s", sig.name, dest.name, e)
        return False


def _check_ct1_promote(predictor):
    """CT① 승격 신호(promote_*.json) 감지 → lean-85 모델 관리형 리로드 (설계 D5).

    자동·수동(차단기 강하 구간)·롤백이 **같은 채널**을 쓴다
    (`ct1_orchestrator.drop_promote_signal`) → consumer 는 D3 배포 정책과 무관한 단일 구현이다.

    스왑 조건은 ⓐ 로드 성공 **그리고** ⓑ D14 스모크 통과 둘 다다. 로드만 보고 바꾸면
    "적재는 됐는데 응답이 병리적인" 모델이 서빙에 올라가는데, 그걸 걷어내던 배포 후
    감시·자동 롤백은 설계 v2 에서 폐지됐다(WP-7). 실패분은 `failed/` 로 옮기고
    **기존 predictor 를 유지**한다 (헌법 6-2 — 서빙 보호).

    한 스캔에 여러 신호가 있으면 **최신 1건만** 적용하고 나머지는 superseded/ 로
    보관한다 (설계 D5·S10).
    """
    global _ct1_signal_watermark
    d = _ct1_promote_dir()
    try:
        sigs = sorted(d.glob("promote_*.json"))
    except Exception:                                      # noqa: BLE001  (디렉토리 부재 = 평시)
        return predictor
    if not sigs:
        return predictor

    # ★최신 신호만 적용한다 (설계 D5·리뷰 S10). 구 신호는 **적용된 적이 없으므로**
    #  processed/ 가 아니라 superseded/ 에 보관한다. (CT² 는 §8-E 가 순서 보존을
    #  규정하므로 대칭을 의도적으로 끊는다 — 설계 v2 머리말의 감시 축 비대칭과 같은 방식.)
    #
    #  가드의 정본은 **워터마크**이지 파일 이동이 아니다: `_move_signal` 은 실패를
    #  삼키므로(잠금·권한·디스크), 이동에만 기대면 보관에 실패한 구 신호가 다음 스캔의
    #  유일한 후보가 되어 **적용된다** — 고치려던 S10 사고가 그대로 재발한다.
    #  선택 = "이 시각 이하의 신호는 전부 대체됐다"는 선언이므로, 적용 성공 여부와
    #  무관하게 갱신한다 (실패한 신 신호 때문에 구 신호가 되살아나면 안 되므로).
    keyed = sorted(((_signal_sort_key(s), s) for s in sigs), key=lambda kv: kv[0])
    key, sig = keyed[-1]

    if _ct1_signal_watermark is not None and key[0] <= _ct1_signal_watermark:
        log.warning("CT① 신호 %d건 전부 구 신호(≤ 마지막 선택 %s) — 적용하지 않고 "
                    "superseded/ 보관 (S10 가드)", len(keyed), _ct1_signal_watermark)
        for _, old in keyed:
            _move_signal(old, d / "superseded", keep_existing=True)
        return predictor

    _ct1_signal_watermark = key[0]
    lost = [old.name for _, old in keyed[:-1]
            if not _move_signal(old, d / "superseded", keep_existing=True)]
    if lost:
        log.error("★superseded 보관 실패 %s — 감사 기록이 누락된다(파일은 원위치 잔류). "
                  "재적용은 워터마크가 막는다", lost)
    try:
        payload = json.loads(sig.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):                  # json.loads 성공 ≠ dict (헌법 7장)
            raise ValueError(f"신호가 객체가 아님: {type(payload).__name__}")
        model_dir = payload.get("model_dir")
        if not model_dir:
            raise ValueError("model_dir 부재 — 교체 대상을 알 수 없다")
        new_pred = Lean85Predictor(model_dir)              # 로드 성공 후에만 교체 (원자 스왑)
        ok, why = _reload_smoke(new_pred)
        if not ok:
            raise RuntimeError(f"D14 스모크 불합격 — {why}")
        _move_signal(sig, d / "processed")
        log.info("CT① 모델 리로드: %s (ct_id=%s · %s · 스모크 %s)",
                 model_dir, payload.get("ct_id"),
                 "롤백" if payload.get("rollback") else "승격", why)
        return new_pred
    except Exception as e:                                 # noqa: BLE001
        log.error("CT① 리로드 취소 — 로드/스모크 실패로 기존 모델 유지 (%s): %s", sig.name, e)
        _move_signal(sig, d / "failed")
        return predictor


def _ct2_promote_dir() -> Path:
    """CT² 승격 신호 디렉토리 (env 우선, 기본 control/ct2 — §8-E)."""
    return CT2_PROMOTE_DIR if str(CT2_PROMOTE_DIR) not in ("", ".") else REPO_ROOT / "control" / "ct2"


def _check_ct2_promote(ae_predictor):
    """CT² 승격 신호(promote_*.json) 감지 → AE 번들 관리형 리로드 (설계 v2 D3·§8-E).

    새 AEPredictor 생성 = DriftTracker(EWMA·B0) 상태 리셋 — 새 기준선에서 재출발.
    실패 시 신호를 failed/로 옮기고 기존 predictor 유지 (6-2 — 서빙 보호).
    처리 완료 신호는 processed/ 이동 (InjectControl 패턴).

    **CT① 하드닝 역포팅 (R1, 2026-08-05)** — `_check_ct1_promote` 가 리뷰 S10·헌법 7장으로
    받은 방어가 여기엔 없었다. 3건을 맞춘다:
      ① **mtime 정렬 + 최신 1건만 적용.** 구현은 이름순 `sigs[0]`(가장 오래된 것)이었다.
         롤백 신호는 `promote_<INC>-rollback.json` 이라 자기 base 바로 뒤에 정렬되므로,
         뒤에 큐잉된 다른 incident 의 promote 가 **롤백을 즉시 되돌린다**. 나머지는 supersede.
      ② **`isinstance(payload, dict)` 가드.** `"123"`·`[1,2]`·`null` 도 유효 JSON 이다 (7장).
      ③ **`_archive`(os.replace).** `Path.rename` 은 Windows 에서 대상 동명 파일이 있으면
         `FileExistsError` 다 — 성공 경로에서 터지면 **이미 로드한 새 번들을 버리고** 구
         predictor 를 돌려준다.

    ※ **게이트 재확인은 하지 않는다** (R1 표 3번 미구현 — 2026-08-05 팀 합의, 설계 개정 ④).
    게이트는 오케스트레이터로만 한정하고 consumer 는 신호를 신뢰한다. 근거는 "신호를 드롭하는
    경로가 게이트 PASS 이후 한 곳뿐"이라는 불변식이며, CT①과 규약을 같게 유지한다. 여기 남는
    방어는 "로드 성공 시에만 스왑"(서빙 보호)이고 이것은 게이트가 아니다. 부수 효과로 게이트
    도입 전 번들(`ae_v1` — validation.json 부재)로의 수동 신호·롤백도 막히지 않는다.
    """
    d = _ct2_promote_dir()
    try:
        sigs = sorted(d.glob("promote_*.json"), key=lambda q: q.stat().st_mtime)
    except Exception:
        return ae_predictor
    if not sigs:
        return ae_predictor
    sig = sigs[-1]                                        # ① 최신 1건만 적용
    for old in sigs[:-1]:                                 # 나머지는 superseded 보관
        log.info("CT² 신호 supersede (더 최신 신호 존재): %s", old.name)
        _archive(old, "processed")
    try:
        payload = json.loads(sig.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):                 # ② json.loads 성공 ≠ dict (7장)
            raise ValueError(f"신호가 객체 아님: {type(payload).__name__}")
        bundle = payload.get("bundle")
        if not bundle:
            raise ValueError("신호에 bundle 없음")
        new_pred = AEPredictor(bundle)                     # 로드 성공 후에만 교체 (원자 스왑)
        _archive(sig, "processed")                         # ③ os.replace — 동명 충돌에 안 죽는다
        log.info("CT² 번들 리로드: %s (drift 리셋 — incident=%s%s)", bundle,
                 payload.get("incident_id"), " ★롤백" if payload.get("rollback") else "")
        return new_pred
    except Exception as e:                                 # noqa: BLE001
        log.error("CT² 리로드 실패 — 기존 번들 유지: %s", e)
        _archive(sig, "failed")
        return ae_predictor


def _warn_orphan_ct2_signals() -> None:
    """dummy 모드인데 promote 신호가 쌓여 있으면 1회 경고 (R1 부수 — 값싼 가드).

    `main()` 의 `AE_MODE == "live"` 조건 자체는 정상이다(더미 모드는 AE 를 서빙하지 않으므로
    promote 가 무의미). 다만 운영자가 `LEAN85_AE_MODE` 를 빠뜨리면 배포가 **조용히** 적용되지
    않은 채 신호만 쌓인다 — 그 상태를 한 줄로 드러낸다.
    """
    try:
        n = len(list(_ct2_promote_dir().glob("promote_*.json")))
    except Exception:                                      # noqa: BLE001
        return
    if n:
        log.warning("AE_MODE=%s 인데 CT² promote 신호 %d건 대기 중 — 번들이 적용되지 않는다. "
                    "LEAN85_AE_MODE=live 여부를 확인하세요.", AE_MODE, n)


def _archive(sig: Path, sub: str) -> None:
    """처리 끝난 신호를 `processed/`·`failed/` 로 이동 (같은 이름이 있어도 실패하지 않게).

    `Path.rename` 은 Windows 에서 대상이 존재하면 `FileExistsError` 다 — 그 예외가
    성공 경로에서 터지면 **이미 로드한 새 모델을 버리고 실패로 기록**하게 된다.
    `os.replace` 는 덮어쓴다 (헌법 7장 — 원자 교체).
    """
    try:
        dest = sig.parent / sub
        dest.mkdir(parents=True, exist_ok=True)
        os.replace(sig, dest / sig.name)
    except OSError as e:
        log.warning("신호 이동 실패(%s → %s/): %s", sig.name, sub, e)


def _shutdown(signum, frame):
    """SIGINT/SIGTERM graceful shutdown (헌법 6-2)."""
    global running
    log.info("종료 신호 수신(%s) — 잔여 버퍼 %d wafer는 미완이라 폐기", signum, len(wafer_buffer))
    running = False


def main():
    predictor = _init_predictor()
    ae_predictor = _init_ae_predictor()
    if TIME_SOURCE == "wall_clock":
        log.warning("TIME_SOURCE=wall_clock — 데모 압축시간에선 시간 피처(days_since_last_pm 등) "
                    "왜곡 가능. 원본 C40 소스(src_ts)는 §6-2 회의 결정 대기.")

    consumer = Consumer({"bootstrap.servers": KAFKA_BOOTSTRAP, "group.id": CONSUMER_GROUP,
                         "auto.offset.reset": "earliest"})
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP})
    consumer.subscribe([TOPIC_IN])
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    log.info("🎧 구독 시작: %s (그룹: %s, C65=%s, AE=%s, 유휴flush=%s, CT⓪bias=%s)",
             TOPIC_IN, CONSUMER_GROUP,
             "lean85" if predictor else "dummy", "live" if ae_predictor else "dummy",
             f"{IDLE_FLUSH_SEC:.0f}s" if IDLE_FLUSH_SEC > 0 else "off", CT0_MODE)
    if AE_MODE != "live":                                  # R1 부수 — 조용한 미적용 방지
        _warn_orphan_ct2_signals()
    _check_idle_flush_ordering()          # B(spc) flush 와 순서 대조 — 역전이면 ERROR(관측 전용)
    if CT0_MODE == ct0_core.MODE_ACTIVE:
        log.warning("CT⓪ mode=active — 예측에 bias 가산이 적용된다 (파일 %s). "
                    "롤백은 params.yaml `ct.model_r2r.mode` 플립 1개다", ct0_io.bias_dir())

    last_idle_check = time.monotonic()
    _ct2_polls = 0
    _ct1_polls = 0
    _ct0_polls = 0
    try:
        while running:
            msg = consumer.poll(1.0)
            if PREDICTION_MODE == "lean85":                # 더미 모드는 의도된 선택이라 스캔 안 함.
                _ct1_polls += 1                            # 반대로 lean85 인데 로드 실패로 폴백된
                if _ct1_polls >= CT1_RELOAD_CHECK_EVERY:   # 상태(predictor=None)는 스캔해서 새
                    _ct1_polls = 0                         # champion promote 로 **자력 복구**한다
                    predictor = _check_ct1_promote(predictor)   # (CT² WP-B2 선례와 동일 이유)
            if AE_MODE == "live":                          # live 모드면 스캔 — 초기 로드 실패로
                _ct2_polls += 1                            # 더미 폴백(ae_predictor=None)이어도 스캔해
                if _ct2_polls >= CT2_RELOAD_CHECK_EVERY:   # 새 번들 promote 로 **자력 복구**한다
                    _ct2_polls = 0                         # (WP-B2 — 구 `is not None`은 폴백을 영구
                    ae_predictor = _check_ct2_promote(ae_predictor)   # 실명시켜 재기동만이 복구였다)
            if CT0_MODE == ct0_core.MODE_ACTIVE:           # CT⓪ bias 파일 주기 스캔 (설계 §4)
                _ct0_polls += 1                            # off·shadow 면 스캔조차 하지 않는다 —
                if _ct0_polls >= CT0_CHECK_EVERY:          # 서빙 경로의 파일 IO 를 0 으로 유지
                    _ct0_polls = 0
                    try:
                        _ct0_scan(set(chamber_current) | set(_ct0_bias_now))
                    except Exception as e:                 # 캐시가 흡수하지만 최후 방어선
                        log.warning("CT⓪ bias 스캔 실패(직전 값 유지): %s", e)
            # 유휴 점검은 **poll 결과와 무관하게 주기적으로** 수행한다 — 한 챔버만 멈추고
            # 다른 챔버 트래픽이 계속되면 poll 이 None 을 반환하지 않아 멈춘 챔버의 마지막
            # wafer 가 영원히 회수되지 않는다 (리뷰 §3-1). 예외는 여기서 흡수 — 이 블록은
            # 아래 try/except 바깥이라 새는 순간 프로세스가 죽는다 (헌법 6-2).
            now = time.monotonic()
            if now - last_idle_check >= IDLE_CHECK_INTERVAL_SEC:
                last_idle_check = now
                try:
                    flush_idle_wafers(predictor, ae_predictor, producer, now)
                    producer.poll(0)                                   # 전달 콜백 소진
                except Exception as e:
                    log.warning("유휴 flush 실패(계속 진행): %s", e)
            if msg is None:
                continue
            if msg.error():
                log.error("Kafka 에러: %s", msg.error())
                continue
            try:                                    # 역직렬화 실패 = 스킵 + 로그 (헌법 6-2)
                data = json.loads(msg.value().decode("utf-8"))
                handle_message(data, predictor, ae_predictor, producer)
            except Exception as e:
                log.warning("메시지 스킵 (역직렬화/처리 실패): %s", e)
    finally:
        consumer.close()
        producer.flush()
        log.info("종료 완료")


if __name__ == "__main__":
    main()
