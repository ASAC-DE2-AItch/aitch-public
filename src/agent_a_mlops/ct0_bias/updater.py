# -*- coding: utf-8 -*-
"""CT⓪ bias updater — `fdc.actual` + `fdc.agent` 소비 → 잔차 창 → bias 결정·기록·전달.

설계 정본: `docs/CT0_ModelR2R_설계방향_v1.md` §4·§6·§7·§8.
**분리 프로세스**다 (설계 D3). 예측 consumer 와 같은 프로세스에 넣지 않는 이유는
sink(#37)와 같다 — 보정 경로의 DB 장애가 예측 지연을 만들면 안 된다.

불변 순서 (설계 원칙 2 — "기록 없으면 갱신 없음"):
    ct_decisions INSERT 성공 ──▶ bias 파일 원자 교체 ──▶ Kafka offset 커밋
어느 단계든 실패하면 **커밋하지 않고 되감아** 재처리한다. 커밋만 나가고 기록이 없으면
재시도도 추적도 없는 무기록 유실이 된다 (헌법 7장 CT² WP-B1 선례).

물리 불가침 (헌법 1-1): 이 프로세스는 Recipe·실력치(관리 기준선)·정비·wafer 판정에
아무 것도 쓰지 않는다. 쓰는 곳은 `ct_decisions`(감사) 와 `control/ct0/*.json`(모델 출력
가산항) 두 곳뿐이다. `wafer_predictions` 는 **읽기 전용** — 그 테이블의 쓰기 주체는
`prediction_sink` 하나다 (설계 §4 접점).

운영 스위치:
    ct.model_r2r.mode        off(기본) | shadow | active   ← params.yaml 단일 소스
    DATABASE_URL             Postgres 접속
    KAFKA_BOOTSTRAP_SERVERS  브로커 (기본 localhost:9092)
    CT0_TRAIN_RMSE           clamp 분모 override (기본: champion 메타에서 해석)
    CT0_BIAS_DIR             bias 파일 디렉토리 (기본 control/ct0)
    CT0_ALLOW_OFF=1          mode=off 여도 기동 (배선 점검용 — 기록·교체는 하지 않는다)

실행:
    python -m ct0_bias.updater
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

# ⚠️ `confluent_kafka` 는 **main() 안에서** import 한다 (psycopg2 도 `connect_db` 안이다).
#   덕분에 이 모듈은 브로커·DB 드라이버 없이 import 되고, 창·기록·순서 규율을 가짜
#   커넥션만으로 테스트할 수 있다 (설계 §11 — 테스트가 브로커 없이 돈다).
if __package__ in (None, ""):                       # 직접 실행 대비
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ct0_bias import bootstrap, control_io, core  # type: ignore  # noqa: E402
else:
    from . import bootstrap, control_io, core

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ [CT0] %(message)s")
log = logging.getLogger("ct0-updater")

# ── 설정 (헌법 6-1: 인프라 값은 .env) ─────────────────────────────────────
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CONSUMER_GROUP = "consumer-group-bias"              # 계약 §0 신설 (설계 §12-5)
TOPIC_ACTUAL = "fdc.actual"
TOPIC_AGENT = "fdc.agent"
TOPIC_MLOPS = "fdc.mlops"                           # CAP 초과 에스컬레이션 (A 소유 토픽)
EVENT_CAP_EXCEEDED = "ModelBiasCapExceeded"         # PascalCase (헌법 6-4)
EVENT_VERDICT = "QualVerdictConfirmed"              # 계약 §8-B

POLL_SEC = 1.0
JOIN_RETRY_SEC = float(os.environ.get("CT0_JOIN_RETRY_SEC", "1.0"))   # 예측 row 지연 재시도 1회
DB_RETRY_SEC = 3                                    # DB 오류 backoff 시작(초)
DB_RETRY_MAX_SEC = 60
CAP_EVENT_MIN_INTERVAL_SEC = 24 * 3600              # CAP 지속 시 일 1회 (설계 D5)
INELIGIBLE_WARN_EVERY = 500                         # 자격 미달 라벨 누적 경고 간격
DUMMY_WARN_EVERY = 100                              # 더미 폴백 라벨 배제 경고 간격
RMSE_REFRESH_SEC = 3600                             # clamp 분모 재해석 주기 — CT①이 일간
STATS_LOG_EVERY = 200                               # 처리 건수 로그 간격

running = True


def _shutdown(signum, frame):                        # noqa: ARG001
    """SIGINT/SIGTERM graceful shutdown (헌법 6-2)."""
    global running
    log.info("종료 신호 수신(%s) — 현재 메시지 처리 후 종료", signum)
    running = False


# ── DB ────────────────────────────────────────────────────────────────────
def connect_db():
    """`DATABASE_URL` 접속. 실패는 예외로 올린다 — 기록 없이 도는 것은 의미가 없다."""
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _reconnect(conn):
    try:
        conn.close()
    except Exception:                               # noqa: BLE001
        pass
    return connect_db()


def fetch_prediction_row(conn, wafer_id: str) -> tuple | None:
    """`wafer_predictions` 에서 (predicted_c65, bias_applied, is_qual, model_version, chamber).

    **읽기 전용**이다 (설계 §4 — 테이블당 쓰기 주체 1개). 라벨이 예측 행보다 먼저 오면
    `None` 이고, 호출자가 1회 지연 재시도 후 skip 한다 (설계 §8 join miss).
    """
    with conn.cursor() as cur:
        cur.execute("""SELECT predicted_c65, COALESCE(bias_applied, 0), COALESCE(is_qual, FALSE),
                              model_version, chamber_id
                         FROM wafer_predictions
                        WHERE wafer_id=%s
                     ORDER BY id DESC LIMIT 1""", (wafer_id,))
        row = cur.fetchone()
    conn.rollback()
    return row


def fetch_decision_bias(conn, ct_id: str) -> float | None:
    """이미 있는 감사 행의 `bias_applied` — 중복 ct_id 재처리에서 **행의 값**으로 수렴하려고.

    재계산값을 쓰면 같은 `ct_id` 아래에서 감사 행과 서빙값이 갈릴 수 있다 (창 표본이
    sink 적재 타이밍에 따라 달라지므로).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT bias_applied FROM ct_decisions WHERE ct_id=%s", (ct_id,))
        row = cur.fetchone()
    conn.rollback()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def insert_decision(conn, *, ct_id: str, status: str, reason: str, residual, bias,
                    model_version_before: str | None, qual_id: str | None) -> bool:
    """감사 행 1개. 반환 = **이번에 새로 심었는지** (중복 전달이면 False).

    `ON CONFLICT DO NOTHING` + 결정론 `ct_id` 로 재전달·재기동 재처리를 무해화한다
    (설계 §7 멱등). `model_version_after` 를 비우는 이유는 `core.dominant_model_version`
    docstring 참조 — CT⓪ 은 모델을 바꾸지 않는다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ct_decisions (ct_id, ct_type, trigger_reason, retrain_status,
                                         residual, bias_applied, model_version_before, qual_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (ct_id) DO NOTHING
               RETURNING ct_id""",
            (ct_id, core.CT_TYPE, reason[:core.TRIGGER_REASON_MAX], status,
             residual, bias, model_version_before, qual_id))
        won = cur.fetchone() is not None
    conn.commit()
    return won


# ── Kafka 발행 (CAP 에스컬레이션) ─────────────────────────────────────────
def publish_cap_exceeded(producer, chamber_id: str, dec: core.BiasDecision,
                         ct_id: str | None) -> None:
    """`fdc.mlops` 에 `ModelBiasCapExceeded` (설계 D5).

    ⚠️ **`fdc.alert` 로 직접 발행하지 않는다** (헌법 1-2 — 이상 경보 단일 채널은 B).
    알림 합류는 B 가 `fdc.mlops` 를 구독해 자기 룰로 alert 를 여는 안으로 협의 중이며,
    그 전까지 이 이벤트의 실효 소비자는 0곳이다 (기록 정본은 `ct_decisions`).
    발행 실패는 흐름을 막지 않는다 — 감사는 이미 DB 에 남았다.
    """
    if producer is None:
        return
    payload = {
        "event_type": EVENT_CAP_EXCEEDED,
        "ct_id": ct_id,
        "chamber_id": chamber_id,
        "raw_bias": dec.raw_bias,
        "bias_applied": dec.bias,
        "cap": dec.cap,
        "n_samples": dec.n,
        "note": "상한 초과분은 적용하지 않는다 — Incident 신호 (헌법 1-1 ⓒ)",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    try:
        producer.produce(TOPIC_MLOPS, key=chamber_id.encode(),
                         value=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
        producer.poll(0)
    except Exception as e:                          # noqa: BLE001
        log.warning("fdc.mlops 발행 실패(무시 — 감사 정본은 ct_decisions): %s", e)


# ── 상태 보관 ─────────────────────────────────────────────────────────────
class Updater:
    """챔버별 `BiasState` 보관 + 메시지 처리. 상태는 캐시이고 정본은 DB (설계 원칙 3)."""

    def __init__(self, conn, cfg: core.BiasConfig, train_rmse: float | None, producer=None):
        self.conn = conn
        self.cfg = cfg
        self.train_rmse = train_rmse
        self.producer = producer
        self.states: dict[str, core.BiasState] = {}
        self.cap_last_sent: dict[str, float] = {}   # 챔버 → 마지막 CAP 이벤트 발행 시각
        self.cap_active: set = set()                # 현재 CAP 상태인 챔버 (진입 시 1회 발행)
        self.n_labels = self.n_join_miss = self.n_ineligible = self.n_updates = 0
        self.n_dummy = 0
        self._rmse_at = time.monotonic()

    # ── 상태 확보 ─────────────────────────────────────────────────────
    def state_for(self, chamber_id: str, day: str) -> core.BiasState:
        """없으면 **DB 재계산으로 채운다** (설계 §7 — 마지막 감사 행을 믿지 않는다).

        재계산 직후 서빙 파일과 대조해 어긋나 있으면 맞춘다 (`reconcile_file`).
        """
        st = self.states.get(chamber_id)
        if st is None:
            self._refresh_rmse()
            st = bootstrap.rebuild_state(self.conn, chamber_id, self.cfg, self.train_rmse)
            self.states[chamber_id] = st
            self.reconcile_file(st, day)
        return st

    def _refresh_rmse(self) -> None:
        """clamp 분모 재해석 (주기 제한). CT① 이 **일간** promote 라 기동 시 1회로 고정하면
        분모가 옛 champion 에 묶인다 — 상한이 실제 모델 성능과 어긋나게 된다."""
        now = time.monotonic()
        if now - self._rmse_at < RMSE_REFRESH_SEC:
            return
        self._rmse_at = now
        try:
            rmse, src = bootstrap.resolve_train_rmse()
        except Exception as e:                      # noqa: BLE001 — 해석 실패는 기존 값 유지
            log.warning("train_rmse 재해석 실패(기존 값 유지): %s", e)
            return
        if rmse is None:
            # 해석 결과가 `None`(= 분모 없음)이어도 **기존 값을 내리지 않는다**. 내리면
            # 전 챔버가 DISABLED 로 축퇴해 감사 행 + 파일 교체가 한 번 나고, 1시간 뒤
            # 복구되면 또 난다 — 시간당 플랩이다. 진짜 분모 부재는 기동 시 경고로 이미
            # 드러나고, 그 상태로는 애초에 값이 잡히지 않는다.
            log.warning("train_rmse 재해석이 값을 못 냈다 — 기존 값(%s) 유지", self.train_rmse)
            return
        if rmse != self.train_rmse:
            log.info("clamp 분모 갱신: %s → %s (%s)", self.train_rmse, rmse, src)
            self.train_rmse = rmse
            for st in self.states.values():
                st.train_rmse = rmse

    def drop_states(self, reason: str) -> None:
        """리밸런스·재조인 시 상태 폐기 → 다음 메시지에서 해당 챔버만 재부트스트랩."""
        if self.states:
            log.info("상태 폐기(%s): %s — 다음 메시지에서 재계산", reason, list(self.states))
        self.states.clear()

    # ── 메시지 처리 ───────────────────────────────────────────────────
    def handle_actual(self, m: dict) -> str:
        """`fdc.actual` 1건 (계약 §1-B). 반환: 동작 요약 문자열(로그용)."""
        wafer_id = m.get("wafer_id")
        if not wafer_id:
            return "skip_bad_payload"
        # clamp 분모 갱신은 **매 라벨마다** 시도한다 (내부 1h 스로틀이라 비용 0).
        # `state_for` 안에만 두면 캐시 미스일 때만 돌아, 챔버가 워밍업된 뒤에는 리밸런스·
        # 처리 실패로 상태가 폐기되기 전까지 영영 안 불린다 — CT① 이 **일간** promote 인데
        # CAP 상한(±1.0×RMSE)만 옛 champion 기준으로 남는다 (리뷰 H1, 헌법 1-1 ⓐ).
        self._refresh_rmse()
        row = fetch_prediction_row(self.conn, wafer_id)
        if row is None and JOIN_RETRY_SEC > 0:      # 예측 행 지연(sink A안 pending) — 1회 재시도
            time.sleep(JOIN_RETRY_SEC)
            row = fetch_prediction_row(self.conn, wafer_id)
        if row is None:
            self.n_join_miss += 1
            return "skip_join_miss"

        predicted, bias_applied, is_qual, model_version, row_chamber = row
        if is_qual:                                  # Qual wafer 는 애초에 미발행 (D7 2중 가드)
            return "skip_qual"
        if core.is_dummy(model_version):             # 더미 폴백 = 난수 (계약 §2 오염 금지)
            self.n_dummy += 1
            if self.n_dummy % DUMMY_WARN_EVERY == 1:
                log.warning("더미 폴백 예측의 라벨 %d건 배제 (wafer=%s) — 모델 로드·추론이 "
                            "실패했던 구간이다. 잦으면 consumer 폴백 원인부터 볼 것",
                            self.n_dummy, wafer_id)
            return "skip_dummy"
        chamber_id = m.get("chamber_id") or row_chamber
        if not chamber_id:
            return "skip_no_chamber"

        residual = core.residual_of(m.get("actual_c65"), predicted, bias_applied)
        if residual is None:                         # NaN·비수치 라벨 → skip + log (헌법 6-2)
            return "skip_nonfinite"

        st = self.state_for(str(chamber_id), _day_of(m))
        label = core.Label(wafer_id=str(wafer_id), residual=residual,
                           pm_count=_int_or_none(m.get("pm_count")),
                           model_version=str(model_version) if model_version else None)
        if not core.push_label(st, label, self.cfg):
            if not core.is_eligible(label.pm_count, st.valid_from_pm_count):
                self.n_ineligible += 1
                if self.n_ineligible % INELIGIBLE_WARN_EVERY == 0:
                    log.warning("자격 미달 라벨 누적 %d건 (%s) — 경계 pm_count=%d 보다 작은 "
                                "라벨만 들어오고 있다면 시뮬레이터 재기동으로 pm_count 가 "
                                "되감겼을 수 있다. `python -m ct0_bias.bootstrap --rebuild` 로 "
                                "재계산하거나 RESET 행을 확인할 것 (설계 §14 한계)",
                                self.n_ineligible, chamber_id, st.valid_from_pm_count)
                return "skip_ineligible"
            return "skip_duplicate"

        self.n_labels += 1
        return self._settle(st, anchor=str(wafer_id), day=_day_of(m), qual_id=None)

    def handle_agent(self, m: dict) -> str:
        """`fdc.agent` 1건 — `QualVerdictConfirmed(verdict='loud')` 만 처리 (계약 §8-B)."""
        if m.get("event_type") != EVENT_VERDICT:     # Brief 등 타 메시지는 event_type 으로 skip
            return "skip_other_event"
        if m.get("verdict") != "loud":               # 조용 PM 은 레짐 전환이 아니다 (bias 연속)
            return "skip_quiet"
        chamber_id = m.get("chamber_id")
        if not chamber_id:
            return "skip_no_chamber"

        st = self.state_for(str(chamber_id), _day_of(m))
        pm_count = _int_or_none(m.get("pm_count"))
        if pm_count is None:
            log.warning("QualVerdictConfirmed 에 pm_count 결손 (%s) — 리셋 불가 (경계는 정수 축)",
                        m.get("qual_id"))
            return "skip_no_pm_count"
        if not core.apply_reset(st, pm_count):       # 재전달 = 멱등 no-op
            return "skip_reset_idempotent"

        ct_id = core.ct_id_for(_day_of(m), chamber_id, str(m.get("qual_id") or pm_count))
        reason = core.reset_reason(pm_count, m.get("qual_id"))
        if not insert_decision(self.conn, ct_id=ct_id, status=core.STATUS_RESET, reason=reason,
                               residual=None, bias=0.0, model_version_before=None,
                               qual_id=m.get("qual_id")):
            log.info("RESET 감사 행 이미 존재 (%s) — 중복 전달", ct_id)
        st.source_ct_id = ct_id
        st.last_status = core.STATUS_RESET            # 전이 판정 기준을 리셋 시점으로 옮긴다 —
        #   갱신하지 않으면 리셋 직후 첫 라벨이 **리셋 이전** 상태와 비교돼 행이 하나 더 남는다
        #   (값은 둘 다 0 이라 데이터는 정확하지만, 과기록은 감사 노이즈다).
        self._write_file(st, core.BiasDecision(status=core.STATUS_RESET, bias=0.0, raw_bias=None,
                                               n=st.n, cap=None, changed=True,
                                               reason=reason), ct_id)
        self.cap_active.discard(str(chamber_id))
        log.info("🔁 요란 리셋 %s | pm_count≥%d 자격 · bias=0 · 이전 라벨 차단 (%s)",
                 chamber_id, pm_count, ct_id)
        return "reset"

    # ── 결정 → 기록 → 파일 (불변 순서) ────────────────────────────────
    def _settle(self, st: core.BiasState, *, anchor: str, day: str, qual_id: str | None,
                force: bool = False) -> str:
        dec = core.decide(st, self.cfg)
        if not (force or dec.should_record(st.last_status)):
            self._maybe_cap_event(st, dec, st.source_ct_id)   # 지속 CAP — 기존 행을 가리킨다
            return f"window(n={st.n})"

        ct_id = core.ct_id_for(day, st.chamber_id, anchor)
        won = insert_decision(
            self.conn, ct_id=ct_id, status=dec.record_status,
            reason=core.update_reason(dec, self.cfg.mode), residual=dec.raw_bias, bias=dec.bias,
            model_version_before=core.dominant_model_version(st), qual_id=qual_id)

        # 기록이 **존재하는 것**이 조건이지 "이번에 심었을 것"이 조건은 아니다 (원칙 2).
        # 중복(=재처리)에서도 파일·상태를 맞춘다: 직전 시도가 INSERT 성공 후 파일 교체에서
        # 실패했을 수 있는데, 그때 손을 떼면 **감사 행은 있는데 서빙은 옛 값**인 상태가 영구히
        # 남는다 (다음 재처리도 같은 ct_id 라 또 중복이다). 단 그때 쓰는 값은 재계산값이 아니라
        # **그 행의 값**이다 — 창 표본이 sink 적재 타이밍에 따라 달라질 수 있어서, 재계산값을
        # 쓰면 같은 ct_id 아래 감사 행 ≠ 서빙값이 된다.
        serve_dec = dec
        if not won:
            recorded = fetch_decision_bias(self.conn, ct_id)
            if recorded is not None and recorded != dec.bias:
                log.warning("감사 행(%s)의 bias %.2f 와 재계산 %.2f 가 다르다 — **행의 값**으로 "
                            "수렴한다 (감사가 정본)", ct_id, recorded, dec.bias)
                serve_dec = replace(dec, bias=recorded)
        st.bias_applied = serve_dec.bias
        st.source_ct_id = ct_id
        st.last_status = dec.record_status
        self._write_file(st, serve_dec, ct_id)
        self._maybe_cap_event(st, dec, ct_id)
        if not won:
            log.info("감사 행 중복(%s) — 새 행 없이 서빙값만 수렴 (설계 §7 멱등)", ct_id)
            return "duplicate_decision"
        self.n_updates += 1
        log.info("📐 bias 갱신 %s | %s → %.2f (n=%d, raw=%s, cap=±%s) [%s] %s",
                 st.chamber_id, dec.record_status, serve_dec.bias, dec.n, dec.raw_bias, dec.cap,
                 self.cfg.mode, ct_id)
        return f"update({dec.record_status})"

    def reconcile_file(self, st: core.BiasState, day: str) -> bool:
        """부트스트랩 재계산 결과와 **파일**이 다르면 맞춘다. 반환 = 교체했는지.

        왜 필요한가: D6 이 상정한 창(INSERT 성공 → 파일 교체 실패 → 크래시)에서 재기동하면
        상태·감사는 새 값인데 서빙 파일만 옛 값이다. 이후 δ 비교는 파일이 아니라 메모리
        기준이라, 다음 δ 이상 변동이 올 때까지 파일이 **영영 수렴하지 않는다**.
        교체는 감사 행을 동반한다 — 기록 없는 서빙 변경은 만들지 않는다 (원칙 2).
        """
        dec = core.decide(st, self.cfg)
        want = dec.bias if self.cfg.mode == core.MODE_ACTIVE else 0.0
        try:
            current = control_io.read_bias(control_io.bias_path(st.chamber_id))
        except FileNotFoundError:
            current = None
        except OSError as e:                        # 읽기 실패 = 판단 불가 → 손대지 않는다
            log.warning("bias 파일 대조 실패(교체 생략): %s", e)
            return False
        if current is None and want == 0.0:         # 파일 없음 + 보정 없음 = 맞출 게 없다
            return False
        if current is not None and abs(current - want) < 1e-9:
            return False
        log.warning("부트스트랩 대조 불일치 (%s): 파일 %s vs 재계산 %.2f — 파일을 맞춘다",
                    st.chamber_id, current, want)
        self._settle(st, anchor=f"reconcile-{st.n}-{st.valid_from_pm_count}", day=day,
                     qual_id=None, force=True)
        return True

    def _write_file(self, st: core.BiasState, dec: core.BiasDecision, ct_id: str) -> None:
        """bias 파일 교체. **shadow 에서는 서빙값 0 을 쓴다** — 계산은 하되 적용은 없다.

        파일 교체 실패는 예외로 올린다: 커밋 전이라 되감겨 재처리된다 (설계 D6).
        """
        serve = dec.bias if self.cfg.mode == core.MODE_ACTIVE else 0.0
        control_io.write_bias(st.chamber_id, control_io.bias_payload(
            st.chamber_id, bias=serve, status=dec.status, mode=self.cfg.mode, n=dec.n,
            raw_bias=dec.raw_bias, cap=dec.cap, ct_id=ct_id,
            valid_from_pm_count=st.valid_from_pm_count, train_rmse=self.train_rmse,
            model_version=core.dominant_model_version(st)))

    def _maybe_cap_event(self, st: core.BiasState, dec: core.BiasDecision,
                         ct_id: str | None) -> None:
        """CAP 진입 시 1회 + 지속 시 일 1회 발행 (설계 D5). 해제되면 상태를 푼다.

        `ct_id` 는 **실재하는 감사 행**만 싣는다 (없으면 null). 계약 §7-5 가 "감사 정본은
        `ct_decisions`" 라고 했는데 DB 에 없는 ID 를 실어 보내면 추적이 끊긴다.
        """
        ch = st.chamber_id
        if not dec.cap_exceeded:
            if ch in self.cap_active:
                log.info("CAP 해제 %s — rolling bias 가 상한 안으로 복귀", ch)
            self.cap_active.discard(ch)
            self.cap_last_sent.pop(ch, None)
            return
        now = time.time()
        first = ch not in self.cap_active
        due = now - self.cap_last_sent.get(ch, 0.0) >= CAP_EVENT_MIN_INTERVAL_SEC
        if first:
            log.warning("🚨 CAP 초과 %s | raw=%.2f > ±%.2f — clamp 값으로 서빙 유지, "
                        "초과분은 Incident 신호 (헌법 1-1 ⓒ)", ch, dec.raw_bias or 0.0,
                        dec.cap or 0.0)
        if first or due:
            self.cap_active.add(ch)
            self.cap_last_sent[ch] = now
            publish_cap_exceeded(self.producer, ch, dec, ct_id)


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _day_of(m: dict) -> str:
    """ct_id 의 날짜 자리 — **메시지 시각**에서 뽑는다 (재처리 시 같은 ID 가 나와야 하므로).

    벽시계를 쓰면 같은 메시지의 재처리가 다음 날 다른 ct_id 를 만들어 멱등이 깨진다.
    우선순위: measured_at(라벨 확보) > confirmed_at(리셋) > processed_at > 벽시계.
    """
    for key in ("measured_at", "confirmed_at", "processed_at", "timestamp"):
        raw = m.get(key)
        if isinstance(raw, str) and len(raw) >= 10:
            d = raw[:10].replace("-", "")
            if d.isdigit():
                return d
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# ── main ──────────────────────────────────────────────────────────────────
def main() -> int:
    from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition  # noqa: N806

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    cfg = bootstrap.load_config()
    if cfg.mode == core.MODE_OFF and os.environ.get("CT0_ALLOW_OFF") != "1":
        log.warning("ct.model_r2r.mode=off — CT⓪ updater 를 기동하지 않는다 "
                    "(shadow 로 올린 뒤 §10 리허설 4종 PASS 가 P2 의 전제다). "
                    "배선만 확인하려면 CT0_ALLOW_OFF=1")
        return 0

    train_rmse, rmse_src = bootstrap.resolve_train_rmse()
    if train_rmse is None:
        log.warning("clamp 분모(train_rmse) 를 못 구했다 → **보정 비활성**으로 돈다 "
                    "(창은 유지·기록 없음). CT0_TRAIN_RMSE 로 지정 가능")
    log.info("mode=%s | 창 N=%d | 최소 표본=%d | clamp=±%.1f×RMSE | δ=%.2f | train_rmse=%s (%s)",
             cfg.mode, cfg.window_n, cfg.min_labels, cfg.max_rmse_ratio, cfg.min_update_delta,
             f"{train_rmse:.2f}" if train_rmse else "없음", rmse_src)

    conn = connect_db()
    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP})
    up = Updater(conn, cfg, train_rmse, producer)

    # enable.auto.commit=False — 감사 행 기록 성공 후에만 커밋 (헌법 7장 CT² WP-B1 선례)
    consumer = Consumer({"bootstrap.servers": KAFKA_BOOTSTRAP, "group.id": CONSUMER_GROUP,
                         "auto.offset.reset": "earliest", "enable.auto.commit": False})

    def _on_assign(_c, partitions):
        """리밸런스 — 담당이 바뀌었으니 상태를 버리고 다음 메시지에서 재계산 (설계 §8)."""
        up.drop_states(f"파티션 재할당 {[(p.topic, p.partition) for p in partitions]}")

    def _on_revoke(_c, _partitions):
        up.drop_states("파티션 회수")

    consumer.subscribe([TOPIC_ACTUAL, TOPIC_AGENT], on_assign=_on_assign, on_revoke=_on_revoke)
    log.info("🎧 구독 시작: %s + %s (그룹 %s · 수동 커밋)", TOPIC_ACTUAL, TOPIC_AGENT, CONSUMER_GROUP)

    db_fail = (None, 0)
    n_msg = 0
    try:
        while running:
            msg = consumer.poll(POLL_SEC)
            if msg is None:
                continue
            if msg.error():
                log.error("Kafka 에러: %s", msg.error())
                continue
            try:
                data = json.loads(msg.value().decode("utf-8"))
            except Exception as e:                   # 역직렬화 실패 = 스킵 + 커밋 (헌법 6-2)
                log.warning("메시지 스킵 (역직렬화 실패): %s", e)
                _commit(consumer, msg)
                continue
            if not isinstance(data, dict):           # json.loads 성공 ≠ dict (헌법 7장)
                log.warning("메시지 스킵 (dict 아님: %s)", type(data).__name__)
                _commit(consumer, msg)
                continue

            try:
                act = (up.handle_actual(data) if msg.topic() == TOPIC_ACTUAL
                       else up.handle_agent(data))
                _commit(consumer, msg)               # 기록·교체 성공 후에만 커밋
                db_fail = (None, 0)
                n_msg += 1
                if act.startswith(("update", "reset")) or n_msg % STATS_LOG_EVERY == 0:
                    log.info("처리 %d건 | 라벨 %d · 갱신 %d · join miss %d · 자격미달 %d | 최근 %s",
                             n_msg, up.n_labels, up.n_updates, up.n_join_miss,
                             up.n_ineligible, act)
            except KafkaException as e:
                log.error("Kafka 처리 오류(되감기): %s", e)
                _seek_back(consumer, msg, TopicPartition)
            except Exception as e:                   # noqa: BLE001 — DB·파일 실패 공통 경로
                # 스킵하지 않는다. 이 프로세스의 존재 이유가 감사 기록이라, 기록 실패를
                # 커밋으로 넘기면 그 라벨은 영영 창에 들어가지 않는다 (설계 D6 fail-closed).
                pos = (msg.topic(), msg.partition(), msg.offset())
                n_fail = db_fail[1] + 1 if db_fail[0] == pos else 1
                db_fail = (pos, n_fail)
                backoff = min(DB_RETRY_SEC * n_fail, DB_RETRY_MAX_SEC)
                log.error("처리 실패(%d회째) → 커밋 없이 되감기 후 %.0fs 대기: %s",
                          n_fail, backoff, e)
                up.drop_states("처리 실패 — 부분 반영 가능성")   # 상태는 언제든 재계산 가능
                _seek_back(consumer, msg, TopicPartition)
                slept = 0.0
                while running and slept < backoff:   # graceful shutdown 반응성 유지
                    time.sleep(min(1.0, backoff - slept))
                    slept += 1.0
                try:
                    up.conn = conn = _reconnect(conn)
                except Exception as re_:             # noqa: BLE001
                    log.error("DB 재연결 실패(다음 루프에서 재시도): %s", re_)
    finally:
        consumer.close()
        try:
            producer.flush(5)
            conn.close()
        except Exception:                            # noqa: BLE001
            pass
        log.info("종료 완료 | 라벨 %d · 갱신 %d · join miss %d · 자격미달 %d",
                 up.n_labels, up.n_updates, up.n_join_miss, up.n_ineligible)
    return 0


def _commit(consumer, msg) -> None:
    try:
        consumer.commit(msg, asynchronous=False)
    except Exception as e:                           # noqa: BLE001
        log.warning("offset 커밋 실패(재조인 시 재소비): %s", e)


def _seek_back(consumer, msg, topic_partition_cls) -> None:
    """커밋하지 않은 메시지로 되감기 — 재소비의 정본 (sink 의 DB 오류 경로와 같은 규율)."""
    try:
        consumer.seek(topic_partition_cls(msg.topic(), msg.partition(), msg.offset()))
    except Exception as e:                           # noqa: BLE001 — 파티션 미할당 등
        log.warning("seek 실패(재조인 시 재소비): %s", e)


if __name__ == "__main__":
    raise SystemExit(main())
