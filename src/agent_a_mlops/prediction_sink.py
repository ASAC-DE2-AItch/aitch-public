"""
[Team A - MLOps] 예측 결과 DB sink (실배선 A안 — 2026-07-23)

역할: `fdc.prediction`·`fdc.actual`·`fdc.alert` 를 구독해 `wafer_predictions` 에 영속화.

🆕 **`fdc.alert` → `spc_flags` 적재 (M1, 2026-08-05)**: Nelson 판정은 B 소유이고 이미
  `prediction_context.spc_flags` 로 발행 중인데, `wafer_predictions.spc_flags` 는 늘 `[]`
  였다. 원인은 두 겹이었다 — ⓐ A consumer 가 빈 배열을 실었고 ⓑ 이 sink 의
  `COALESCE(%s, spc_flags)` 에서 `[]` 가 NULL 이 아니라 그대로 통과했다. 그래서 다른
  writer 가 채워도 다음 예측이 지웠다. 결과적으로 `ct2_deploy_monitor` 의 감시①
  (SPC↑/AE 침묵 교차)이 표본 0으로 **구조적 비활성**이었다. ⓐ는 consumer 에서, ⓑ는
  `_nonempty_spc` 로 막고, 여기에 alert 전용 UPDATE 경로를 추가한다.
  - 모델 consumer(consumer.py)와 **분리된 독립 프로세스** (DB 장애가 예측을 안 흔들게).
  - Kafka = 정본, 이 sink 는 그 복제본을 DB 에 적재 (별도 consumer group).

⚠ 핵심 설계 — AE 산출물 병합 (2026-07-23):
  `fdc.prediction` 은 C65 예측(predicted_c65·shap_top3)과 **AE 산출물(drift_score·anomaly_score)** 을
  함께/따로 실을 수 있다. 그래서 **wafer_id 기준 UPSERT(부분 필드 병합)** 로 처리한다:
    · predicted_c65 있는 메시지 = 행 생성/갱신 (predicted_c65 는 NOT NULL 이라 행 생성은 이 메시지만 가능)
    · AE-only 메시지 = 기존 행의 drift/anomaly 만 UPDATE (행 없으면 skip — C65 선행 가정)
  COALESCE 로 갱신 → 늦게 온 부분 메시지가 기존 값을 NULL 로 덮지 않는다.

라벨 지연: `fdc.actual` 도착 시 `actual_c65`·`measured_at`·`pm_count` UPDATE (계약 §1-B).

🆕 **CT⓪ Model R2R 접점 (설계 `docs/CT0_ModelR2R_설계방향_v1.md` §4)**: 이 sink 가
  `wafer_predictions` 의 **유일한 쓰기 주체**라는 원칙을 유지한 채 2컬럼을 더 적재한다.
    · `bias_applied` (fdc.prediction) — 보정 전 예측 복원용. `raw = predicted − bias_applied`
      가 되어야 bias updater 가 **개루프**로 잔차를 계산한다 (설계 D1). 이 값이 없으면
      보정된 예측 대비 잔차로 갱신돼 60일 지연 하의 되먹임 루프가 된다.
    · `pm_count` (fdc.actual) — 레짐 자격 필터 축 (설계 §5). 재기동 시 창 재계산이
      이 값으로 구레짐 라벨을 걸러낸다.
  `ct0_bias.updater` 는 이 테이블을 **읽기만** 한다 (테이블당 쓰기 주체 1개).

⚠ 유실 방지 설계 (2026-07-28 코드리뷰 §1-1·1-2·1-3 대응):
  ① **비-dict 페이로드 방어** — `json.loads` 는 `"문자열"`·`[1,2]`·`null` 도 성공시킨다.
     파싱 직후 `isinstance(dict)` 가드로 스킵 (헌법 6-2: 한 건이 프로세스를 죽이지 않는다).
  ② **수동 offset 커밋** — `enable.auto.commit=False`. 적재가 성공한 뒤에만 커밋한다.
     DB 오류 경로는 커밋하지 않고 해당 offset 으로 `seek` 해 되돌린다 → 재연결 후 같은
     메시지부터 재소비. (자동 커밋이면 DB 실패분이 조용히 유실 — sink 는 영속화가 존재 이유다.)
  ③ **actual 선행 도착 보관(A안)** — `fdc.prediction` 과 `fdc.actual` 은 다른 토픽이라
     상호 순서 보장이 없다(리플레이·파티션 속도차·재시작 타이밍). 대상 행이 아직 없으면
     라벨을 버리지 않고 메모리 `PENDING_ACTUALS` 에 보관했다가, 해당 wafer 의 prediction 이
     적재되는 순간 즉시 반영한다. 보관은 상한(건수·TTL)이 있는 **best-effort 버퍼**다.

  ↳ **대안 B안 (미채택 — 스키마 변경 필요)**: actual 선행 시 placeholder 행을 먼저 만들고
     이후 prediction 이 채우는 방식. A안과 달리 **프로세스 재시작에도 라벨이 살아남는다**는
     것이 장점이나, `wafer_predictions.predicted_c65` 가 `NOT NULL` 이라 제약 완화
     (또는 sentinel 값 도입)가 선행돼야 한다. `db/init.sql` 변경 = **PM 승인 대상**
     (헌법 3-2)이라 코드만으로 끝나지 않아 이번 범위에서 제외했다. A안의 한계(재시작 시
     pending 소실·상한 초과분 유실)가 운영에서 실제로 관측되면 B안으로 승격을 제안한다.
     승격 시 필요한 것: ⓐ `predicted_c65 DROP NOT NULL` 또는 `is_placeholder` 컬럼 추가
     ⓑ placeholder 행을 예측 대상에서 제외하는 뷰/필터 ⓒ 헌법 4-3에 따른 문서 동기화.

실행: python src/agent_a_mlops/prediction_sink.py
환경(.env): DATABASE_URL, KAFKA_BOOTSTRAP_SERVERS,
            SINK_PENDING_ACTUAL_MAX, SINK_PENDING_ACTUAL_TTL_SEC (선택)

계약 §2-2 (2026-07-23 결정): model_version 은 이벤트에 포함 → sink 가 적재(예측 출처 추적).
  lot_id·recipe_id 는 wafer_id 종속 정보라 이벤트에 싣지 않고 **wafer 마스터(dim) 조인**으로
  해석(현업 정규화). wafer_predictions 의 lot_id·recipe_id 컬럼은 조인/뷰로 채운다(PM/DB 영역, 후속).
"""

import json
import logging
import os
import signal
import time
from collections import OrderedDict

import psycopg2
from psycopg2.extras import Json
from confluent_kafka import Consumer, TopicPartition

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ [Team A sink] %(message)s")
log = logging.getLogger("prediction-sink")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://fdc_admin:fdc_pass_2026@localhost:5432/fdc_platform")
CONSUMER_GROUP = "consumer-group-db-sink"
TOPIC_PRED = "fdc.prediction"
TOPIC_ACTUAL = "fdc.actual"
# 🆕 M1 (2026-08-05) — Nelson 판정(`spc_flags`)의 유일한 생산자는 **B** 이고 이미 `fdc.alert`
#   의 `prediction_context.spc_flags` 로 발행 중이다. A 는 **운반·적재만** 한다 (헌법 1-2:
#   alert 채널 경유 · 3-3: SHAP 독립 채널 — A 가 Nelson 을 자체 계산하는 것은 소유권 침범).
TOPIC_ALERT = "fdc.alert"

DB_RETRY_SEC = 3                      # DB 재연결 간격(초) — 실패 반복 시 선형 증가
DB_RETRY_MAX_SEC = 60                 # 재시도 백오프 상한(초). DB 오류는 스킵하지 않고 계속 재시도
PRED_LOG_EVERY = 20                   # prediction 적재 로그 간격(건)
# actual 선행 도착 보관 버퍼 상한 (헌법 6-1: 매직 넘버 금지 — .env 로 조정 가능)
PENDING_ACTUAL_MAX = int(os.environ.get("SINK_PENDING_ACTUAL_MAX", "10000"))
PENDING_ACTUAL_TTL_SEC = float(os.environ.get("SINK_PENDING_ACTUAL_TTL_SEC", str(24 * 3600)))

running = True

# wafer_id → (actual_c65, measured_at, pm_count, stored_at) — prediction 선행 도착 대기 라벨 (A안).
#   ※ stored_at 은 **항상 마지막 원소**다 (`_evict_pending` 이 item[-1] 로 TTL 을 본다).
# OrderedDict = 삽입 순서 유지 → 상한 초과 시 가장 오래된 것부터 evict.
PENDING_ACTUALS: "OrderedDict[str, tuple]" = OrderedDict()
# wafer_id → (spc_flags, stored_at) — **alert 선행 도착** 대기 (M1, 2026-08-05).
# `fdc.alert` 와 `fdc.prediction` 은 다른 토픽이라 상호 순서 보장이 없다. 대상 행이 없다고
# 버리면 그 wafer 의 Nelson 플래그가 영구 유실되고, 감시①이 다시 `n_spc=0` 을 본다 —
# 데이터 부재로 감시가 죽는 상태를 되살리지 않으려고 actual 과 같은 A안 버퍼를 쓴다.
PENDING_SPC: "OrderedDict[str, tuple]" = OrderedDict()


def _shutdown(signum, frame):
    """SIGINT/SIGTERM graceful shutdown (헌법 6-2)."""
    global running
    log.info("종료 신호 수신(%s) — 정리 후 종료", signum)
    running = False


def _commit(consumer, msg) -> None:
    """offset 커밋 (실패해도 루프를 죽이지 않는다 — 헌법 6-2).

    커밋 실패는 재소비(중복 적재)로 이어지지만, sink 의 UPSERT 는 멱등이라 안전하다.
    """
    try:
        consumer.commit(msg, asynchronous=False)
    except Exception as e:
        log.warning("offset 커밋 실패(재소비 가능 — UPSERT 멱등): %s", e)


def _reconnect(conn):
    """기존 커넥션을 닫고 재연결 (닫지 않으면 커넥션 누수)."""
    try:
        if conn:
            conn.close()
    except Exception:
        pass
    return connect_db()


def connect_db():
    """DB 연결 (실패 시 재시도 — sink 는 예측과 무관하게 살아있어야 함)."""
    while running:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = True
            log.info("DB 연결: %s", DATABASE_URL.rsplit("@", 1)[-1])
            return conn
        except Exception as e:
            log.error("DB 연결 실패 → %d초 후 재시도: %s", DB_RETRY_SEC, e)
            time.sleep(DB_RETRY_SEC)
    return None


# ── 페이로드 가드 ──────────────────────────────────────────────────────────
def wafer_id_of(m) -> str | None:
    """페이로드 → wafer_id (계약 위반이면 None).

    `json.loads` 성공 = dict 보장이 아니다(`"123"`·`[1,2]`·`null` 전부 유효 JSON).
    로그 경로에서도 쓰이므로 **어떤 입력에도 예외를 던지지 않는다** (헌법 6-2).
    """
    if not isinstance(m, dict):
        return None
    wid = m.get("wafer_id")
    return str(wid) if wid is not None else None


def _nonempty_spc(flags):
    """빈 `spc_flags` 는 **None 으로 낮춘다** (M1 회귀 방어, 2026-08-05).

    `COALESCE(%s, spc_flags)` 에서 `[]` 는 **NULL 이 아닌 값**이라 그대로 통과한다. 그래서
    누가 빈 배열을 실으면, `fdc.alert` 가 채워 넣은 Nelson 플래그를 **다음 예측이 도착하는
    순간 지워버린다** — 감시①(SPC↑/AE 침묵 교차)이 영구 "표본 부족"으로 죽는 경로다.
    발행 측(consumer)에서 키를 뺐지만, 적재 측에서도 한 겹 막는다(경합에서 지지 않게).
    """
    return flags if isinstance(flags, list) and flags else None


# ── pending actual 버퍼 (A안) ──────────────────────────────────────────────
def _evict_pending(now: float | None = None) -> int:
    """TTL 만료·상한 초과 pending 항목 정리 (actual·spc 공용). 반환: 버려진 건수."""
    now = time.time() if now is None else now
    dropped = 0
    for buf, label in ((PENDING_ACTUALS, "actual"), (PENDING_SPC, "spc_flags")):
        n = 0
        for wid in [w for w, item in buf.items() if now - item[-1] > PENDING_ACTUAL_TTL_SEC]:
            buf.pop(wid, None)
            n += 1
        while len(buf) > PENDING_ACTUAL_MAX:
            buf.popitem(last=False)                  # 가장 오래된 것부터
            n += 1
        if n:
            log.warning("pending %s %d건 폐기 (TTL %.0fs · 상한 %d) — 유실",
                        label, n, PENDING_ACTUAL_TTL_SEC, PENDING_ACTUAL_MAX)
        dropped += n
    return dropped


def stash_pending_actual(wid: str, m: dict) -> None:
    """대상 행이 아직 없는 actual 라벨을 보관 (prediction 도착 시 반영).

    `pm_count` 도 함께 보관한다 — CT⓪ 부트스트랩이 **레짐 자격 필터**로 쓰는 값이라
    (설계 §5·§7) 여기서 빠지면 재계산이 구레짐 라벨을 창에 넣는다.
    """
    PENDING_ACTUALS[wid] = (m.get("actual_c65"), m.get("measured_at"),
                            m.get("pm_count"), time.time())
    PENDING_ACTUALS.move_to_end(wid)
    _evict_pending()


def flush_pending_actual(cur, wid: str) -> bool:
    """보관 중인 라벨이 있으면 방금 적재된 행에 반영. 반환: 반영 여부.

    ⚠ **UPDATE 성공 뒤에 pop** 한다. `autocommit=True` 라 INSERT 는 이미 커밋된 상태인데
    이 UPDATE 만 실패하면, offset 을 되감아 재소비해도 재소비 경로는 merge 로 흘러
    보관분이 사라진 뒤라 **라벨이 영구 유실**된다. 먼저 지우지 않는다.
    """
    item = PENDING_ACTUALS.get(wid)
    if item is None:
        return False
    actual_c65, measured_at, pm_count, _ = item
    cur.execute(
        """UPDATE wafer_predictions
             SET actual_c65=%s, measured_at=%s, pm_count=COALESCE(%s, pm_count)
           WHERE id=(SELECT id FROM wafer_predictions WHERE wafer_id=%s ORDER BY id DESC LIMIT 1)""",
        (actual_c65, measured_at, pm_count, wid))
    if not cur.rowcount:
        return False
    PENDING_ACTUALS.pop(wid, None)          # 반영이 확정된 뒤에만 제거
    return True


# ── 적재 ──────────────────────────────────────────────────────────────────
def upsert_prediction(cur, m: dict) -> str:
    """fdc.prediction 1건 → wafer_predictions UPSERT (wafer_id 병합). 반환: 동작 유형.

    행이 새로 생기거나 갱신된 뒤에는 보관 중인 선행 actual 라벨을 즉시 반영한다(A안).
    """
    wid = wafer_id_of(m)
    if wid is None:                           # 계약 위반 페이로드 → 스킵 (헌법 6-2)
        return "skip_bad_payload"
    pc65 = m.get("predicted_c65")
    shap = m.get("shap_top3")
    spc = _nonempty_spc(m.get("spc_flags"))
    cur.execute("SELECT id FROM wafer_predictions WHERE wafer_id=%s ORDER BY id DESC LIMIT 1", (wid,))
    row = cur.fetchone()

    if row is None:
        if pc65 is None:                      # AE-only 인데 행 없음 → C65 선행 가정, skip
            return "skip_no_c65"
        cur.execute(
            """INSERT INTO wafer_predictions
               (wafer_id, chamber_id, predicted_c65, drift_score, anomaly_score,
                shap_top3, spc_flags, is_qual, model_version, bias_applied)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (wid, m.get("chamber_id"), pc65, m.get("drift_score"), m.get("anomaly_score"),
             Json(shap) if shap is not None else None,
             Json(spc) if spc is not None else None,
             bool(m.get("is_qual", False)), m.get("model_version"), m.get("bias_applied")))
        return _post_insert_flush(cur, wid, "insert")

    # 기존 행 → 부분 필드 병합 (COALESCE: 새 값이 None 이면 기존 유지)
    cur.execute(
        """UPDATE wafer_predictions SET
             predicted_c65 = COALESCE(%s, predicted_c65),
             drift_score   = COALESCE(%s, drift_score),
             anomaly_score = COALESCE(%s, anomaly_score),
             shap_top3     = COALESCE(%s, shap_top3),
             spc_flags     = COALESCE(%s, spc_flags),
             is_qual       = COALESCE(%s, is_qual),
             model_version = COALESCE(%s, model_version),
             bias_applied  = COALESCE(%s, bias_applied)
           WHERE id=%s""",
        (pc65, m.get("drift_score"), m.get("anomaly_score"),
         Json(shap) if shap is not None else None,
         Json(spc) if spc is not None else None,
         m.get("is_qual"), m.get("model_version"), m.get("bias_applied"), row[0]))
    return _post_insert_flush(cur, wid, "merge")


def _post_insert_flush(cur, wid: str, base: str) -> str:
    """행이 생기거나 갱신된 뒤 보관 중이던 선행 도착분(actual·spc_flags)을 반영."""
    parts = [base]
    if flush_pending_actual(cur, wid):
        parts.append("pending_actual")
    if flush_pending_spc(cur, wid):
        parts.append("pending_spc")
    return "+".join(parts)


def update_spc_flags(cur, m: dict) -> str:
    """fdc.alert 1건 → `wafer_predictions.spc_flags` **만** UPDATE (M1, 2026-08-05).

    `upsert_prediction` 과 **분리된 함수**인 이유: 같은 UPDATE 문에 얹으면 alert 이 예측
    컬럼(predicted_c65·anomaly_score…)을 건드릴 여지가 생기고, 그건 A 가 B 의 값으로 자기
    예측을 덮어쓰는 경로가 된다. 여기서는 한 컬럼만 만진다.

    wafer 키는 계약상 **`prediction_context.wafer_id`** 다 (alert 최상위에는 wafer_id 가
    없다 — C 파서가 조용히 드롭하므로 B 가 싣지 않는다). 대상 행이 없으면 skip 한다:
    예측이 아직 안 왔다는 뜻이고, `spc_flags` 만으로 placeholder 행을 만들면
    `predicted_c65 NOT NULL` 에 걸린다.

    Returns: 동작 유형 문자열 (`spc_flags` / `skip_*`).
    """
    ctx = m.get("prediction_context")
    if not isinstance(ctx, dict):
        return "skip_no_context"
    wid = wafer_id_of(ctx)
    if wid is None:
        return "skip_bad_payload"
    flags = ctx.get("spc_flags")
    if not isinstance(flags, list):           # 계약: [{sensor, rule}] — 아니면 손대지 않는다
        return "skip_no_flags"
    if _write_spc_flags(cur, wid, flags):
        return "spc_flags"
    stash_pending_spc(wid, flags)             # alert 선행 도착 — 버리지 않고 보관
    return "spc_flags_pending"


def _write_spc_flags(cur, wid: str, flags: list) -> bool:
    """`spc_flags` 단일 컬럼 UPDATE. 대상 행이 있었으면 True."""
    cur.execute(
        """UPDATE wafer_predictions SET spc_flags=%s
           WHERE id=(SELECT id FROM wafer_predictions WHERE wafer_id=%s ORDER BY id DESC LIMIT 1)""",
        (Json(flags), wid))
    return bool(cur.rowcount)


def stash_pending_spc(wid: str, flags: list) -> None:
    """대상 행이 아직 없는 Nelson 플래그를 보관 (prediction 도착 시 반영)."""
    PENDING_SPC[wid] = (flags, time.time())
    PENDING_SPC.move_to_end(wid)
    _evict_pending()


def flush_pending_spc(cur, wid: str) -> bool:
    """보관 중인 플래그가 있으면 방금 적재된 행에 반영. 반환: 반영 여부.

    `flush_pending_actual` 과 같은 규율 — **UPDATE 성공 뒤에 pop** 한다. 먼저 지우면
    UPDATE 실패 시 재소비 경로에서 보관분이 사라진 뒤라 플래그가 영구 유실된다.
    """
    item = PENDING_SPC.get(wid)
    if item is None:
        return False
    if not _write_spc_flags(cur, wid, item[0]):
        return False
    PENDING_SPC.pop(wid, None)
    return True


def update_actual(cur, m: dict) -> str:
    """fdc.actual 1건 → actual_c65·measured_at·pm_count UPDATE (계약 §1-B).

    대상 행이 없으면(= actual 선행 도착) 라벨을 버리지 않고 보관한다(A안 — 모듈 docstring).

    `pm_count` 는 CT⓪ 이 쓰는 **레짐 자격 축**이다 (설계 §5). `COALESCE(%s, pm_count)` 인
    이유: 구 페이로드·수기 주입으로 값이 없을 때 이미 들어있던 값을 지우지 않기 위해서다
    (`spc_flags` 를 빈 배열이 덮어썼던 M1 사고의 반대 방향 가드 — 헌법 7장).
    """
    wid = wafer_id_of(m)
    if wid is None:                           # 계약 위반 페이로드 → 스킵 (헌법 6-2)
        return "skip_bad_payload"
    cur.execute(
        """UPDATE wafer_predictions
             SET actual_c65=%s, measured_at=%s, pm_count=COALESCE(%s, pm_count)
           WHERE id=(SELECT id FROM wafer_predictions WHERE wafer_id=%s ORDER BY id DESC LIMIT 1)""",
        (m.get("actual_c65"), m.get("measured_at"), m.get("pm_count"), wid))
    if cur.rowcount:
        return "actual"
    stash_pending_actual(wid, m)              # prediction 선행 도착 대기
    return "actual_pending"


def main():
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    conn = connect_db()
    if conn is None:
        return
    # enable.auto.commit=False: 적재 성공 후에만 커밋 → DB 실패분 유실 방지 (리뷰 §1-2)
    consumer = Consumer({"bootstrap.servers": KAFKA_BOOTSTRAP, "group.id": CONSUMER_GROUP,
                         "auto.offset.reset": "earliest", "enable.auto.commit": False})
    consumer.subscribe([TOPIC_PRED, TOPIC_ACTUAL, TOPIC_ALERT])
    log.info("🎧 구독 시작: %s + %s + %s (그룹 %s · 수동 커밋)",
             TOPIC_PRED, TOPIC_ACTUAL, TOPIC_ALERT, CONSUMER_GROUP)

    n_pred = n_actual = n_skip = n_spc = 0
    db_fail = (None, 0)          # (topic, partition, offset) → 연속 DB 실패 횟수
    try:
        while running:
            msg = consumer.poll(1.0)
            if msg is None:
                _evict_pending()                         # 유휴 구간에 만료 라벨 정리
                continue
            if msg.error():
                log.error("Kafka 에러: %s", msg.error())
                continue
            try:
                data = json.loads(msg.value().decode("utf-8"))
            except Exception as e:                       # 역직렬화 실패 = 스킵+로그 (헌법 6-2)
                log.warning("메시지 스킵 (역직렬화 실패): %s", e)
                n_skip += 1
                _commit(consumer, msg)                   # 포이즌 메시지가 offset 을 막지 않도록
                continue
            if not isinstance(data, dict):               # 🆕 계약 위반 페이로드 방어 (리뷰 §1-1)
                log.warning("메시지 스킵 (dict 아님: %s)", type(data).__name__)
                n_skip += 1
                _commit(consumer, msg)
                continue
            try:
                with conn.cursor() as cur:
                    if msg.topic() == TOPIC_PRED:
                        act = upsert_prediction(cur, data)
                        n_pred += 1
                        if n_pred % PRED_LOG_EVERY == 0 or act.startswith("skip") \
                                or "pending_actual" in act:
                            log.info("💾 prediction %s (%s) | 누적 %d",
                                     data.get("wafer_id"), act, n_pred)
                    elif msg.topic() == TOPIC_ALERT:       # 🆕 M1 — spc_flags 만 갱신
                        act = update_spc_flags(cur, data)
                        wid_ = (data.get("prediction_context") or {}).get("wafer_id")
                        if act == "spc_flags":
                            n_spc += 1                     # 실제 적재된 건만 센다
                            log.info("🚩 spc_flags %s (누적 %d)", wid_, n_spc)
                        elif act == "spc_flags_pending":
                            log.warning("🚩 alert %s 선행 도착 — prediction 대기열 보관 (대기 %d건)",
                                        wid_, len(PENDING_SPC))
                    elif msg.topic() == TOPIC_ACTUAL:
                        act = update_actual(cur, data)
                        n_actual += 1
                        if act == "actual_pending":       # 라벨 유실 위험 → WARNING (리뷰 §1-3)
                            log.warning("🧾 actual %s 선행 도착 — prediction 대기열 보관 "
                                        "(대기 %d건 · 누적 %d)",
                                        data.get("wafer_id"), len(PENDING_ACTUALS), n_actual)
                        else:
                            log.info("🧾 actual %s (%s) | 누적 %d",
                                     data.get("wafer_id"), act, n_actual)
                _commit(consumer, msg)                   # 적재 성공 후에만 커밋 (리뷰 §1-2)
                db_fail = (None, 0)
            except psycopg2.OperationalError as e:       # DB 끊김 → 커밋하지 않고 되감기
                # ⚠️ **절대 스킵하지 않는다.** OperationalError 는 거의 항상 커넥션 레벨
                # (메시지와 무관)이라 "이 메시지가 나쁘다"는 근거가 없다. 여기서 포기·커밋하면
                # DB 장기 다운 시 전 메시지가 조용히 유실돼 이 sink 의 존재 이유가 사라진다
                # (리뷰 §1-2). 선두 막힘은 스킵이 아니라 **백오프**로 다룬다.
                pos = (msg.topic(), msg.partition(), msg.offset())
                n_fail = db_fail[1] + 1 if db_fail[0] == pos else 1
                db_fail = (pos, n_fail)
                backoff = min(DB_RETRY_SEC * n_fail, DB_RETRY_MAX_SEC)   # 선형 증가·상한
                log.error("DB 오류(%d회째) → offset 되감기 후 %.0fs 뒤 재연결: %s",
                          n_fail, backoff, e)
                try:
                    consumer.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))
                except Exception as se:                  # 파티션 미할당 등 — 재조인 시 재소비
                    log.warning("seek 실패(재조인 시 재소비): %s", se)
                # graceful shutdown 반응성 유지 — 통잠 대신 1초 단위로 running 확인 (헌법 6-2)
                slept = 0.0
                while running and slept < backoff:
                    time.sleep(min(1.0, backoff - slept))
                    slept += 1.0
                conn = _reconnect(conn)                  # 기존 커넥션 닫고 재연결
            except Exception as e:                       # 포이즌 메시지 = 스킵 + 커밋 (헌법 6-2)
                log.warning("적재 스킵 (wafer=%s): %s", wafer_id_of(data), e)
                n_skip += 1
                _commit(consumer, msg)
    finally:
        consumer.close()
        if conn:
            conn.close()
        log.info("종료 완료 — prediction %d · actual %d · spc_flags %d · 스킵 %d 적재 "
                 "(미반영 pending actual %d · spc %d건)",
                 n_pred, n_actual, n_spc, n_skip, len(PENDING_ACTUALS), len(PENDING_SPC))
        if PENDING_ACTUALS or PENDING_SPC:
            log.warning("prediction 미도착으로 반영되지 않은 actual %d건 · spc_flags %d건 — "
                        "재시작 시 소실됩니다 (A안 한계 · B안 승격은 모듈 docstring 참조)",
                        len(PENDING_ACTUALS), len(PENDING_SPC))


if __name__ == "__main__":
    main()
