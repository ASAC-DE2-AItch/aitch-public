# -*- coding: utf-8 -*-
"""FastAPI Gateway 뼈대 — P3-2 (S0 지원: /health + SSE 푸시 채널).

역할 (화면 정의서 v1.1 백엔드 연동 요약 기준):
    - GET /health : heartbeat (S0 상단바) — Kafka/DB 연결 상태는 후속 태스크에서 실측으로 교체
    - GET /events : SSE 푸시 채널 (pending badge·heartbeat) — 현재는 주기 heartbeat 스텁
    - 이후 W4~W5에서 /dashboard/summary, /incidents 등 화면별 엔드포인트가 추가된다.

실행:
    uvicorn src.gateway.main:app --reload --port 8000
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s │ %(levelname)-7s │ %(message)s")
log = logging.getLogger("gateway")

# ---- config (헌법 6-1: 매직 넘버는 params.yaml) ------------------------------
PARAMS_PATH = Path(__file__).resolve().parents[2] / "config" / "params.yaml"


def load_params() -> dict:
    """config/params.yaml 로드 (없으면 명시적 에러 — 조용한 기본값 금지)."""
    if not PARAMS_PATH.exists():
        raise FileNotFoundError(f"config/params.yaml 없음: {PARAMS_PATH}")
    with open(PARAMS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


PARAMS = load_params()
SSE_INTERVAL_SEC = PARAMS["operations"]["dashboard_poll_sec"]  # E5

app = FastAPI(title="AITCH Gateway", version="0.1.0")

# 개발 편의: Vite dev 서버 허용 (배포 시 도메인 고정)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def now_iso() -> str:
    """UTC ISO-8601 타임스탬프."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/health")
async def health() -> dict:
    """S0 heartbeat — 컴포넌트별 생존 상태.

    TODO(W4): kafka/db/agent_service 실제 연결 검사로 교체 (오픈이슈 6-3).
    """
    return {
        "status": "ok",
        "ts": now_iso(),
        "components": {
            "gateway": "ok",
            "kafka_consumer": "unknown",   # W4에서 실측
            "db": "unknown",               # W4에서 실측
            "agent_service": "unknown",    # W4에서 실측
        },
        "last_data_received_at": None,     # W4: fdc.raw 소비 시각 연동
    }


async def sse_stream():
    """SSE 이벤트 제너레이터 — heartbeat + pending badge 스텁.

    TODO(W4~W5): Kafka(fdc.agent)·DB(approval_records) 연동으로
    pending_count 실시간 푸시로 교체. 지금은 S0 배선 검증용 주기 이벤트.
    """
    while True:
        payload = {"type": "heartbeat", "ts": now_iso(), "pending_count": 0}
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        await asyncio.sleep(SSE_INTERVAL_SEC)


@app.get("/events")
async def events() -> StreamingResponse:
    """SSE 푸시 채널 (S0 상단바 구독)."""
    return StreamingResponse(sse_stream(), media_type="text/event-stream")


# ---- P4-1 승인 게이트 (지연 배선 — /health·/events는 deps 없이도 동작) -----------
#   run: PYTHONPATH=src uvicorn gateway.main:app   (src를 path에 두고 실행)
_gate: dict = {"app": None, "conn": None, "pool": None, "cm": None, "producer": None}


def _load_env() -> None:
    """`.env` 로드 — 게이트웨이만 이걸 안 해서 매번 셸에 DATABASE_URL 을 수동 export 했다.

    ⚠️ **startup 훅이 아니라 모듈 import 시점에 즉시 호출한다** (2026-07-31 개정).
    구 startup 훅 방식의 사고: 피드 객체(`_feed`·`_raw`)는 모듈 레벨에서 생성되며
    생성자가 `os.getenv("DATABASE_URL")` 을 **import 시점**에 캡처한다 — 훅(startup)은
    그보다 늦게 돌아, 셸 export 없이 띄우면 raw_feed 관리선 캐시가 dsn=None(σ 미환산)
    으로 고착됐다. 실측: S1 전 센서 `sig:null` → 대시보드가 조용히 목업 폴백.
    (승인 게이트는 startup 훅 안에서 getenv 하므로 멀쩡했다 — 증상이 갈린 이유.)

    `override=False` — 셸에 이미 있는 값이 우선이다(운영/컨테이너에서 주입한 값 보호).
    python-dotenv 미설치·파일 부재는 경고만 (기존처럼 셸 환경변수로 동작 — 6-2).
    """
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path(__file__).resolve().parents[2] / ".env"
        load_dotenv(env_path, override=False)
        log.info("✅ .env 로드: %s (override=False — 셸 값 우선)", env_path.name)
    except Exception as e:                           # noqa: BLE001 — 없으면 셸 환경변수로 진행
        log.warning("`.env` 로드 skip (셸 환경변수 사용): %s", e)


_load_env()                                          # import 즉시 — 모듈 레벨 피드 생성자보다 먼저


@app.on_event("startup")
def _wire_approval_gate() -> None:
    """graph app + ApprovalStore + PostgresSaver 배선. 실패해도 /health 유지(경고만)."""
    import os
    try:
        import psycopg2
        from psycopg2.pool import ThreadedConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver
        from ..orchestrator.approval_graph import ApprovalStore, build_graph, emit_event
        from ..orchestrator.events import build_producer, make_kafka_emit
        dsn = os.environ["DATABASE_URL"]
        pool = ThreadedConnectionPool(1, 8, dsn)     # A6: 동시 resume 대비 (op당 acquire/release)
        conn = psycopg2.connect(dsn)                 # /approvals/pending 읽기 전용
        cm = PostgresSaver.from_conn_string(dsn)
        cp = cm.__enter__()
        cp.setup()                                   # 체크포인터 테이블 최초 1회
        producer = None
        try:                                         # A1: 실 Kafka 발행 (실패 시 로그 stub 폴백)
            producer = build_producer(os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"))
            emit = make_kafka_emit(producer)
        except Exception as ke:                      # noqa: BLE001
            log.warning(f"Kafka producer 미배선 → 이벤트 로그 stub: {ke}")
            emit = emit_event
        _gate.update(app=build_graph(ApprovalStore(pool=pool), cp, emit=emit),
                     conn=conn, pool=pool, cm=cm, producer=producer)
        try:                                         # CT² 배포 승인 그래프 (파생 thread — §8-E)
            from ..orchestrator.ct2_deploy_approval import build_ct2_graph
            _gate["ct2_app"] = build_ct2_graph(pool, cp)
            log.info("✅ CT² 배포 승인 그래프 배선 (ct2_deploy · 파생 thread)")
        except Exception as ce:                      # noqa: BLE001 — 선택 기능, 본편 게이트는 유지
            log.warning(f"CT² 승인 그래프 미배선(선택): {ce}")
        log.info("✅ 승인 게이트 배선 완료 (PostgresSaver)")
    except Exception as e:                           # noqa: BLE001 — /health는 살려둠
        log.warning(f"승인 게이트 미배선 → /approvals 503 (DATABASE_URL/langgraph 필요): {e}")


@app.on_event("shutdown")
def _teardown_approval_gate() -> None:
    """종료 정리 — Kafka flush(이벤트 유실 방지) + 체크포인터/풀/커넥션 닫기 + 스택 자식 정리."""
    if _stack.get("runner") is not None:
        _stack["runner"].shutdown()                  # P6-4 — 자식 서비스 고아화 방지
    p = _gate.get("producer")
    if p is not None:
        try:
            p.flush(5)                               # 미전송 이벤트 전달 (최대 5s)
        except Exception:
            pass
    cm = _gate.get("cm")
    if cm is not None:
        try:
            cm.__exit__(None, None, None)            # PostgresSaver 컨텍스트 종료
        except Exception:
            pass
    pool = _gate.get("pool")
    if pool is not None:
        try:
            pool.closeall()
        except Exception:
            pass
    conn = _gate.get("conn")
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _heal_gate() -> bool:
    """startup 커넥션·풀이 죽어 있으면 **재배선**한다 (2026-08-10 실측 사고 대응).

    사고: 시연 중 `_gate["conn"]` 과 pool 커넥션이 통째로 닫혔다(`psycopg2.InterfaceError:
    connection already closed`). postgres 는 20시간 무중단이었고, 요청마다 새로 접속하는
    라우트(`/chambers/status`·`/predictions/recent`·`/raw/recent`)는 200 인데 startup
    커넥션을 쓰는 라우트(`/quals/recent`·`/rtd/status`·`/limits/*`·`/dispositions/pending`)만
    전부 500 이었다 — 재접속 경로가 없어 **게이트웨이를 재시작할 때까지 영구 500**.
    S7 은 이걸 빈 응답과 구분하지 못해 목업으로 폴백했고, 화면은 "라이브가 안 붙은 것"으로
    보였다(실데이터는 DB 에 정상 적재돼 있었다).

    복구는 startup 훅 재실행이다 — 단일 conn·pool·PostgresSaver·그래프가 한 묶음이라
    conn 만 되살리면 승인 그래프(pool 경유)는 계속 죽어 있다. 폴러들은 이미 자기 전용
    커넥션을 재접속하므로(:403) 여기서는 손대지 않는다. 실패 시 10초 쿨다운 — 죽은 DB 를
    상대로 매 요청 재접속을 시도하면 요청이 줄줄이 블로킹된다.
    """
    import time as _t
    dead = False
    conn = _gate.get("conn")
    if conn is not None and getattr(conn, "closed", 0):
        dead = True
    pool = _gate.get("pool")
    if pool is not None and getattr(pool, "closed", False):
        dead = True
    if not dead:
        return False
    if _t.monotonic() - float(_gate.get("_heal_at") or 0) < 10.0:
        return False                                 # 쿨다운 — 재접속 폭주 방지
    _gate["_heal_at"] = _t.monotonic()
    log.warning("DB 커넥션 사망 감지 — 승인 게이트 재배선 시도")
    _wire_approval_gate()
    c = _gate.get("conn")
    ok = c is not None and not getattr(c, "closed", 0)
    log.warning("재배선 %s", "성공" if ok else "실패(다음 요청에 재시도)")
    return ok


@app.middleware("http")
async def _db_heal(request, call_next):
    """모든 요청 전 커넥션 생존 확인 (죽었을 때만 재배선 — 평시 비용 = 속성 2회 읽기).

    라우트마다 가드를 넣으면 새 라우트가 빠진다. 진입점 하나로 고정한다.
    """
    try:
        if request.url.path not in ("/health", "/healthz"):
            _heal_gate()
    except Exception:                                # noqa: BLE001 — 복구 실패가 요청을 못 죽임
        log.exception("커넥션 자동 복구 실패 — 요청은 계속")
    return await call_next(request)


def _pending_timeout_sec():
    """params.incident.pending_timeout_sec — 에이징 표시 기준 (P6-1, 미설정=표시 생략)."""
    try:
        from ..orchestrator.incident_grouper import load_params
        return (load_params().get("incident", {}) or {}).get("pending_timeout_sec")
    except Exception:                                # noqa: BLE001 — 표시용이라 실패해도 목록은 산다
        return None


def _cum_warn_cfg() -> tuple:
    """params.limit_engine — (누적 표류 경고 임계 σ, k_sigma). 실패 시 (None, None) = 배지 생략.

    A-warn(2026-07-31 B): 승인마다 리셋되는 A3 누적 가드가 못 보는 **v1 대비 총 표류**를
    S4 배지로 띄우기 위한 임계. 표시용이므로 로드 실패가 승인 목록을 죽이면 안 된다.
    """
    try:
        le = PARAMS.get("limit_engine") or {}
        return le.get("reset_cum_from_initial_warn_sigma"), le.get("control_limit_k_sigma")
    except Exception:                                # noqa: BLE001 — 표시용 fail-open
        return None, None


@app.get("/approvals/pending")
def approvals_pending() -> dict:
    """승인 대기 목록 (S3 큐) — approval_records PENDING (+ 에이징·누적 표류 표시).

    동봉 표시 필드: `age_sec`/`is_aged`(P6-1) · `cum_from_initial_sigma`/`is_cum_warned`
    (A-warn, 2026-08-07 — v1 대비 누적 표류 배지). 둘 다 **비차단 표시**다 (헌법 1-1).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="approval gate not wired (DATABASE_URL/langgraph 필요)")
    from .approvals import fetch_pending
    warn_sigma, k_sigma = _cum_warn_cfg()
    return {"pending": fetch_pending(_gate["conn"],
                                     pending_timeout_sec=_pending_timeout_sec(),
                                     cum_warn_sigma=warn_sigma, k_sigma=k_sigma)}


@app.post("/approvals/{incident_id}")
def approvals_decide(incident_id: str, body: dict = Body(...)) -> dict:
    """엔지니어 결정(approve/modify/reject/escalate) → 그래프 resume (헌법 1-1 HITL)."""
    if _gate["app"] is None:
        raise HTTPException(status_code=503, detail="approval gate not wired")
    from .approvals import has_resume_point, resume_decision, validate_decision
    try:
        payload = validate_decision(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 수신 페이로드 1줄 기록 (2026-08-11) — "화면에서 SCRAP 을 골랐는데 원장은 RELEASE" 를
    #   두 번 겪고도 프런트/서버 어느 쪽이 흘린 건지 증거가 없었다. 카드 id·판정값을 남긴다.
    log.info("🖐 결정 수신 — inc=%s record=%s action=%s per_wafer=%s disposition=%s",
             incident_id, body.get("record_id"), payload.get("action"),
             payload.get("per_wafer"), payload.get("disposition"))
    # CT² 배포(ct2_deploy)는 전용 그래프 + 파생 thread로 라우팅 (§8-E — 모델 게이트)
    # 같은 조회가 **유령 성공 가드 ①** 을 겸한다 (아래 pending_found — 추가 왕복 0).
    rtype = None
    sel_option = None
    pending_found: bool | None = None                 # None = 조회 불가 → 판정 보류(fail-open)
    # 🔴 카드 지목 (2026-08-11) — 헌법 1-4 **예외**로 한 Incident 에 정비 PENDING 과 처분
    #   PENDING 이 공존한다. 구 라우팅은 "가장 최근 PENDING" 이라, 엔지니어가 **정비 카드에서
    #   승인을 눌러도 처분 카드가 승인**되는 경로가 열려 있었다 — 둘을 분리한 목적 자체를
    #   무너뜨린다(그리고 per_wafer 가 없어 권고 프리필이 그대로 확정된다: SCRAP 이면 비가역).
    #   프런트(S4)는 카드마다 record_id 를 알고 있으니 그 행으로만 재개한다. 없으면 구 동작
    #   유지(하위 호환 — 목업/CLI/구 번들 호출).
    try:
        _rec = int(body["record_id"]) if body.get("record_id") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="record_id 는 정수 (approval_records.id)")
    try:
        if _gate["conn"] is not None:
            with _gate["conn"].cursor() as _c:
                # selected_option 도 읽는다 (2026-08-11) — 구 매핑이 정비(manual_option)를
                #   request_type='disposition' 으로 적었기 때문에 request_type 만으로는
                #   「정비 카드」와 「처분 전용 카드」를 구분할 수 없다. 라우팅을 틀리면 정비
                #   승인이 처분 thread 로 가서 409 가 된다(8/11 실측).
                if _rec is not None:
                    _c.execute("SELECT request_type, selected_option FROM approval_records "
                               "WHERE id=%s AND incident_id=%s AND status='PENDING'",
                               (_rec, incident_id))
                else:
                    _c.execute("SELECT request_type, selected_option FROM approval_records "
                               "WHERE incident_id=%s AND status='PENDING' ORDER BY requested_at DESC, id DESC LIMIT 1", (incident_id,))
                _row = _c.fetchone()
                pending_found = _row is not None
                rtype = _row[0] if _row else None
                sel_option = _row[1] if _row else None
            _gate["conn"].commit()                    # WP-B5 — read-only tx 종료 (idle-in-transaction 방지, 타 엔드포인트와 일관)
    except Exception:                                 # noqa: BLE001 — 조회 실패 시 본편 경로
        try:
            _gate["conn"].rollback()
        except Exception:
            pass
    # 🔴 유령 성공 차단 ① — 열린 승인 요청이 없으면 재개할 대상 자체가 없다 (PR #71).
    #   구 가드는 예외에만 의존해, **이미 결정된 Incident** 에 두 번째 결정을 보내면
    #   LangGraph 가 조용히 통과시켜 200 + `approval_records` 무변화가 됐다 — 엔지니어는
    #   반려했다고 믿는데 감사 추적엔 없다(헌법 1-1 훼손). S6 5초 폴링의 stale 선택이 실경로.
    if pending_found is False:
        raise HTTPException(
            status_code=409,
            detail=(f"이 카드(record {_rec})는 이미 결정됐거나 이 Incident 의 것이 아닙니다: {incident_id}"
                    if _rec is not None else
                    f"열린 승인 요청 없음 — 이미 결정됐거나 요청 미생성: {incident_id}"))
    # 카드 종류와 페이로드가 어긋나면 거절한다 (2026-08-11) — per_wafer 는 처분 카드 전용이다.
    #   정비 카드에 per_wafer 가 실려 오면 화면과 서버가 다른 카드를 보고 있다는 뜻이므로,
    #   조용히 한쪽을 무시하지 않고 멈춘다(SCRAP 비가역 · 헌법 6-3 fail-closed).
    #   rtype 이 None(조회 실패)일 때는 막지 않는다 — 아래에서 처분 thread 로 보낸다.
    if payload.get("per_wafer") and rtype is not None \
            and not (rtype == "disposition" and sel_option == "disposition_option"):
        raise HTTPException(
            status_code=400,
            detail=f"per_wafer 는 처분 카드 전용 — 이 카드는 {rtype}/{sel_option}: {incident_id}")
    # 처분 전용 카드 (2026-08-11) — 파생 thread `dispo::<INC>` 로 재개한다. 본선 thread 를
    #   그대로 쓰면 정비 PENDING 의 interrupt 를 처분 결정으로 소모해 버린다(둘이 공존하므로).
    #   조회 실패(fail-open)로 카드 종류를 모르면 per_wafer 유무로 판별한다 — per_wafer 는
    #   처분 카드만 보내므로, 이걸 본선 thread 로 보내면 정비 interrupt 를 소모한다(더 나쁘다).
    if (rtype == "disposition" and sel_option == "disposition_option") \
            or (pending_found is None and bool(payload.get("per_wafer"))):
        # 🔴 확정값 없는 처분 승인은 거절한다 (2026-08-11) — 카드는 "판정은 승인 화면에서
        #   확정" 규약으로 열린다. 확정값이 안 오면 서버가 **권고 프리필을 조용히 확정**하게
        #   되는데(8/11 두 번 실측: 화면에서 SCRAP/보류를 골랐는데 원장은 전건 RELEASE),
        #   프리필이 SCRAP 이면 비가역이다. 침묵보다 큰 소리로 실패하는 쪽이 옳다.
        if payload["action"] == "approve" and not payload.get("per_wafer") \
                and not payload.get("disposition"):
            raise HTTPException(
                status_code=400,
                detail=("웨이퍼별 판정(per_wafer)이 오지 않았습니다 — 권고 프리필을 임의로 "
                        "확정하지 않습니다. 화면을 새로고침(Ctrl+Shift+R)하고 03에서 장별 "
                        "판정을 확인한 뒤 다시 승인하세요."))
        from ..orchestrator.approval_graph import dispo_thread_id
        _dtid = dispo_thread_id(incident_id)
        if has_resume_point(_gate["app"], _dtid) is False:          # 유령 성공 차단 ②
            raise HTTPException(status_code=409,
                                detail=f"재개할 처분 PENDING 없음 — thread 닫힘: {incident_id}")
        try:
            resume_decision(_gate["app"], _dtid, payload)
        except Exception as e:                        # noqa: BLE001
            raise HTTPException(status_code=409,
                                detail=f"재개할 처분 PENDING 없음: {incident_id} ({type(e).__name__})")
        return {"incident_id": incident_id, "action": payload["action"],
                "status": "resumed", "request_type": "disposition"}
    if rtype == "ct2_deploy":
        if _gate.get("ct2_app") is None:
            raise HTTPException(status_code=503, detail="ct2 approval graph not wired")
        if payload["action"] not in ("approve", "reject"):
            raise HTTPException(status_code=400,
                                detail="ct2_deploy는 approve/reject 2지선다 (번들 modify 불가 — §8-E)")
        from ..orchestrator.ct2_deploy_approval import thread_id_for
        _tid = thread_id_for(incident_id)
        if has_resume_point(_gate["ct2_app"], _tid) is False:      # 유령 성공 차단 ②
            raise HTTPException(status_code=409,
                                detail=f"재개할 CT² PENDING 없음 — thread 닫힘: {incident_id}")
        try:
            resume_decision(_gate["ct2_app"], _tid, payload)
        except Exception as e:                        # noqa: BLE001
            raise HTTPException(status_code=409,
                                detail=f"재개할 CT² PENDING 없음: {incident_id} ({type(e).__name__})")
        return {"incident_id": incident_id, "action": payload["action"],
                "status": "resumed", "request_type": "ct2_deploy"}
    # 유령 성공 차단 ② — DB 는 PENDING 인데 thread 가 닫힌 불일치까지 거른다.
    #   크래시 후 재개 지점이 남은 thread(next 비어있지 않음)는 재개가 정당하므로 통과시킨다.
    if has_resume_point(_gate["app"], incident_id) is False:
        raise HTTPException(
            status_code=409,
            detail=f"재개 지점 없음 — thread 가 닫혔거나 미시작: {incident_id}")
    # 결정 시점 증거층 스냅샷 (2026-07-31 — PR #70). resume **전에** 굳힌다: resume 이 끝나면
    # PENDING 행이 판정 상태로 넘어가 "무엇을 보고 결정했는가"를 붙일 자리가 사라진다.
    # 실패해도 결정은 막지 않는다 — 감사 보강이 승인을 인질로 잡으면 본말전도다(6-2).
    if _gate["conn"] is not None:
        try:
            snap = _evidence_of(_gate["conn"], incident_id)
            with _gate["conn"].cursor() as _c:
                _c.execute(
                    """UPDATE approval_records SET evidence_snapshot = %s::jsonb
                        WHERE incident_id = %s AND status = 'PENDING'""",
                    (json.dumps(snap, ensure_ascii=False, default=str), incident_id))
            _gate["conn"].commit()
        except Exception as e:                    # noqa: BLE001
            try:
                _gate["conn"].rollback()
            except Exception:
                pass
            log.warning("evidence_snapshot 기록 실패(결정은 계속): %s %s", incident_id, type(e).__name__)
    try:
        resume_decision(_gate["app"], incident_id, payload)
    except Exception as e:                        # noqa: BLE001 — 소진/미시작 스레드 재개 시도
        raise HTTPException(status_code=409,
                            detail=f"재개할 PENDING 없음 — 이미 결정됐거나 그래프 미시작: {incident_id} ({type(e).__name__})")
    return {"incident_id": incident_id, "action": payload["action"], "status": "resumed"}


@app.post("/corrections/{correction_id}/retry")
def corrections_retry(correction_id: str, body: dict = Body(...)) -> dict:
    """APPLY_FAILED correction 재처리 (P6-1 — B5-1 스펙 §5-3 오케스트레이터 몫).

    엔지니어 재입력값(*_modified) 갱신 + status→PROPOSED + 감사 행(단일 트랜잭션) 후
    **fdc.correction 재발행** — B는 재발행분을 받아 apply_approved 재실행(재검증 루프).
    body: {modified_value: {center|ucl|lcl}, approver, approver_role?, reason?}.
    대상이 APPLY_FAILED가 아니면 409 (이미 처리/오타 — 멱등).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="approval gate not wired (DATABASE_URL 필요)")
    from .approvals import retry_failed_correction
    try:
        info = retry_failed_correction(
            _gate["conn"], correction_id, (body or {}).get("modified_value"),
            approver=(body or {}).get("approver"), approver_role=(body or {}).get("approver_role"),
            reason=(body or {}).get("reason"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if info is None:
        raise HTTPException(status_code=409,
                            detail=f"APPLY_FAILED 상태 아님 — 이미 처리됐거나 correction_id 오타: {correction_id}")
    # 재발행 — 발행 스키마는 계약 §6 fdc.correction 정합 (정량은 B: 값은 DB에서 재조회하므로 정보성)
    from ..orchestrator.approval_graph import emit_event
    from ..orchestrator.events import make_kafka_emit
    emit = make_kafka_emit(_gate["producer"]) if _gate.get("producer") else emit_event
    emit("CorrectionApplied", {                  # 계약 §6 형태 정합 — 미사용 필드 null 유지(이름 삭제 금지)
        "incident_id": info.get("incident_id"), "correction_type": "limit",
        "correction_id": correction_id, "sensor": info.get("sensor_id"),
        "limit_version": info.get("limit_version"), "effective_from": None,
        "parameter_id": None, "delta_pct": None, "recipe_correction_id": None,
        "value_proposed": (body or {}).get("modified_value"),   # 정보성 — 정본은 *_modified (B COALESCE)
        "retry_of": "APPLY_FAILED",              # 신규 정보성 필드 — 재처리 식별 (계약 §6 각주 동기)
    })
    return {"correction_id": correction_id, "status": "PROPOSED", "republished": True,
            "incident_id": info.get("incident_id")}


# ---------------------------------------------------------------------------
# P5-4 — S1 실배선 (fdc.prediction → REST 초기 이력 + SSE append)
# 코어는 gateway/prediction_feed.py (fastapi 무의존) — 여기는 라우트만.
# ---------------------------------------------------------------------------
from .prediction_feed import PredictionFeed  # noqa: E402

_feed = PredictionFeed()


@app.on_event("startup")
def _start_approval_autostart() -> None:
    """Brief 적재 → 승인 그래프 자동 시작 폴러 (P5-1 완성 — 2026-07-31).

    지금까지 그래프 시작은 start_approval CLI 의 **수동 대역**이었다(그 파일 docstring).
    리허설 실측(2026-07-30): Brief 가 agent_reports 까지 자동으로 쌓여도 Approvals 는
    비어 있었다 — 마지막 한 칸(그래프 invoke)이 수동이라서.

    동작: `SUP-%` Brief 가 있는데 approval_records 가 **0행**인 Incident 를 주기 스캔해
    `start_incident` 로 invoke 한다. merge_reports 경로③이 DB 에서 Brief 를 수합하고
    gate 노드가 PENDING 오픈 후 interrupt — LLM 무관이라 ms 단위.
      · '0행' 조건이 이중 invoke 를 막는다: PENDING 행은 gate 가 interrupt **전에** 만들고,
        종결(APPROVED/REJECTED)행이 있는 Incident 는 재시작하지 않는다(재분석 = 수동 경로).
      · incidents 실존 가드 포함 (7/24 유령 PENDING 사고 재발 방지 — start_approval 과 동일).
      · 폴러는 **자기 전용 DB 커넥션**을 쓴다 — _gate["conn"] 은 요청 스레드와 공유라
        동시 사용 경합이 난다(psycopg2 커넥션은 스레드 안전하지 않음).
    """
    import os as _os
    import threading
    import time as _time
    if _gate.get("app") is None:
        log.warning("승인 자동시작 폴러 미기동 — 승인 게이트 미배선")
        return
    try:
        sec = (load_params().get("incident", {}) or {}).get("approval_autostart_poll_sec")
    except Exception:                                # noqa: BLE001
        sec = None
    if not sec:
        log.warning("승인 자동시작 폴러 비활성 — params incident.approval_autostart_poll_sec 미설정 "
                    "(start_approval CLI 수동 대역)")
        return
    from ..orchestrator.approval_graph import start_incident

    def _loop() -> None:
        import psycopg2
        conn = None
        while True:
            _time.sleep(float(sec))
            try:
                if conn is None or conn.closed:
                    conn = psycopg2.connect(_os.environ["DATABASE_URL"])
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT DISTINCT ar.incident_id
                             FROM agent_reports ar
                            WHERE ar.report_id LIKE 'SUP-%%'
                              AND EXISTS (SELECT 1 FROM incidents i
                                           WHERE i.incident_id = ar.incident_id)
                              AND NOT EXISTS (SELECT 1 FROM approval_records p
                                               WHERE p.incident_id = ar.incident_id)
                            LIMIT 5""")
                    pending = [r[0] for r in cur.fetchall()]
                conn.commit()
                for inc in pending:
                    try:
                        start_incident(_gate["app"], inc, {})   # 경로③ — Brief 는 DB 에서 수합
                        log.info("✅ 승인 그래프 자동 시작: %s (PENDING 오픈)", inc)
                    except Exception as exc:                    # noqa: BLE001 — 한 건 실패가 폴러를 못 죽임
                        log.warning("승인 자동시작 실패 %s: %s", inc, exc)
            except Exception as exc:                            # noqa: BLE001
                try:
                    if conn is not None:
                        conn.rollback()
                except Exception:
                    conn = None                                 # 죽은 커넥션 — 다음 주기 재접속
                log.warning("승인 자동시작 폴러 스캔 실패(다음 주기 재시도): %s", exc)

    threading.Thread(target=_loop, daemon=True, name="approval-autostart").start()
    log.info("✅ 승인 자동시작 폴러 기동 — %ss 주기 (Brief 적재 → PENDING)", sec)


@app.on_event("startup")
def _start_ct2_reconciler() -> None:
    """CT² promote 이중 쓰기 정합 폴러 (A14) — **기본 off**.

    `ct.reconciler_poll_sec` 미설정/0 = 비활성 (수동 대역:
    `python -m src.orchestrator.ct2_reconciler --once`). 판정·조치는
    `ct2_reconciler.reconcile_once` — 쓰기 조치는 「APPROVED·드롭 이력 0곳 → 재드롭」
    하나뿐이고 실행당 1건 상한. 신호 파일을 옮기거나 지우지 않는다(소유 = consumer).
    approval-autostart 폴러와 동형: **자기 전용 커넥션** (psycopg2 커넥션은 스레드
    비안전 — _gate["conn"] 공유 금지).
    """
    import os as _os
    import threading
    import time as _time
    try:
        _ct = (load_params().get("ct", {}) or {})
        sec = _ct.get("reconciler_poll_sec")
    except Exception:                                # noqa: BLE001
        _ct, sec = {}, None
    if not sec:
        log.info("CT² reconciler 폴러 비활성 — params ct.reconciler_poll_sec 미설정/0 "
                 "(CLI --once 수동 대역)")
        return
    stale_min = float(_ct.get("reconciler_stale_min", 10))
    from ..orchestrator.ct2_reconciler import reconcile_once

    def _loop() -> None:
        import psycopg2
        conn = None
        while True:
            _time.sleep(float(sec))
            try:
                if conn is None or conn.closed:
                    conn = psycopg2.connect(_os.environ["DATABASE_URL"])
                summary = reconcile_once(conn, stale_min=stale_min)
                conn.commit()                        # SELECT 트랜잭션 정리 (idle-in-tx 방지)
                if summary["redropped"] or summary["escalate_failed"] or summary["stale"]:
                    log.warning("CT² reconciler 조치/이상: %s", summary)
            except Exception as exc:                  # noqa: BLE001 — 한 주기 실패가 폴러를 못 죽임
                try:
                    if conn is not None:
                        conn.rollback()
                except Exception:                     # noqa: BLE001
                    conn = None                       # 죽은 커넥션 — 다음 주기 재접속
                log.warning("CT² reconciler 주기 실패(다음 주기 재시도): %s", exc)

    threading.Thread(target=_loop, daemon=True, name="ct2-reconciler").start()
    log.info("✅ CT² reconciler 폴러 기동 — %ss 주기 · stale %s분", sec, stale_min)


@app.on_event("startup")
def _start_prediction_feed() -> None:
    """fdc.prediction 소비 스레드 기동 — Kafka 미가용이어도 gateway는 산다 (백오프 재시도)."""
    try:
        _feed.start()
        log.info("✅ fdc.prediction 피드 기동 (consumer-group-dashboard)")
    except Exception as e:                               # noqa: BLE001
        log.warning(f"prediction 피드 미기동: {e}")


@app.on_event("shutdown")
def _stop_prediction_feed() -> None:
    """소비 스레드 graceful 종료 (헌법 6-2)."""
    _feed.stop()


@app.get("/predictions/recent")
def predictions_recent(n: int = 40, chamber: str | None = None) -> dict:
    """S1 초기 이력 — 최근 예측 n건 (최신 우선) + 피드 상태."""
    return {"status": _feed.status(), "items": _feed.recent(n, chamber)}


async def _prediction_sse():
    """SSE 제너레이터 — 접속 시점 이후 신규만 push (이력은 REST가 담당)."""
    cursor = _feed.head_seq()
    while True:
        items = _feed.since(cursor)
        if items:
            cursor = items[-1]["_seq"]
            for it in items:
                yield f"event: prediction\ndata: {json.dumps(it, ensure_ascii=False)}\n\n"
        else:
            yield ": keep-alive\n\n"                     # 프록시 idle 타임아웃 방지
        await asyncio.sleep(1.0)


@app.get("/predictions/stream")
async def predictions_stream() -> StreamingResponse:
    """S1 append 채널 — wafer 예측 1건 = 이벤트 1건 (useLiveFdc의 wafer 클럭)."""
    return StreamingResponse(_prediction_sse(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# P6-1 선행 (2026-07-22, B6-3-c 요청 — 주간 당김) — 레짐 이벤트 2종 발행 경로
# 계약 §8-B QualVerdictConfirmed · §8-C ChamberRequalified. 빌더는 orchestrator/
# regime_events.py (무의존). S7 화면(P6-2)이 추후 이 엔드포인트를 호출 — 그 전까지는
# curl/수동 호출로 실발행 가능 (B 소비자 배선 테스트용). quals 영속화·R9 그래프
# 정식 연결은 P6-1 본체에서.
# ---------------------------------------------------------------------------
from ..orchestrator.regime_events import (RequalOnceGuard,  # noqa: E402
                                          build_chamber_requalified,
                                          build_qual_verdict)

_regime = {"emit": None}
_requal_guard = RequalOnceGuard()


@app.on_event("startup")
def _wire_regime_emitter() -> None:
    """레짐 이벤트 전용 emit 훅 — 승인 게이트(DB)와 독립으로 Kafka만 있으면 실발행.

    Kafka 미가용 시 로그 스텁 폴백 (게이트웨이 생존 — 시연 안전망 패턴).
    """
    import os
    try:
        from ..orchestrator.events import build_producer, make_kafka_emit
        producer = build_producer(os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"))
        _regime["emit"] = make_kafka_emit(producer)
        _regime["producer"] = producer
        log.info("✅ 레짐 이벤트 emitter 배선 (fdc.agent)")
    except Exception as e:                               # noqa: BLE001
        def _stub(event_type: str, payload: dict) -> None:
            log.info(f"[emit-stub] {event_type} ({payload.get('incident_id') or payload.get('qual_id')})")
        _regime["emit"] = _stub
        log.warning(f"레짐 emitter 스텁 폴백 (Kafka 미가용): {e}")


@app.on_event("shutdown")
def _flush_regime_emitter() -> None:
    """미전송 레짐 이벤트 flush (유실 방지 — 헌법 6-2)."""
    p = _regime.get("producer")
    if p is not None:
        try:
            p.flush(5)
        except Exception:                                # noqa: BLE001
            pass


@app.post("/qual/verdict")
def qual_verdict(body: dict = Body(...)) -> dict:
    """조용/요란 판정 확정 → QualVerdictConfirmed 발행 (§8-B — S7 확정 백엔드 선행).

    body: {qual_id, chamber_id, verdict(loud|quiet), pm_count, approved_by, incident_id?}
    loud면 incident_id 필수 (레짐 전환 Incident — 신규 결정 10).
    """
    try:
        payload = build_qual_verdict(
            qual_id=body.get("qual_id", ""), chamber_id=body.get("chamber_id", ""),
            verdict=body.get("verdict", ""), pm_count=body.get("pm_count", 0),
            approved_by=body.get("approved_by", ""), incident_id=body.get("incident_id"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _regime["emit"]("QualVerdictConfirmed", payload)
    log.info(f"📢 QualVerdictConfirmed — {payload['chamber_id']} {payload['verdict']} ({payload['qual_id']})")
    return {"emitted": "QualVerdictConfirmed", **payload}


def _release_rtd_inhibit(chamber_id: str, incident_id: str,
                         qual_id: str = "", approved_by: str = "") -> int:
    """RTD inhibit 해제 (헌법 1-1 예외 4 ⓒ) — 해제 경로는 requal 승인 하류인 여기 하나뿐.

    **멱등**이다 — 열린 행이 없으면 0을 돌려주고, 신호 파일은 있으면 지운다.
    해제 실패가 requal 발행 응답을 막지 않게 예외를 삼킨다(수동 복구 대상).
    """
    try:
        import os as _os
        import psycopg2 as _pg
        from src.orchestrator import chamber_inhibit as _ci
        _conn = _pg.connect(_os.environ["DATABASE_URL"])
        try:
            n = _ci.release(_conn, chamber_id, incident_id,
                            qual_id=qual_id, released_by=approved_by)
            _conn.commit()
        finally:
            _conn.close()
        if n:
            log.info(f"□ RTD inhibit 해제 {n}건 — {chamber_id} (requal 하류)")
        return n
    except Exception as e:                # noqa: BLE001
        log.error(f"RTD inhibit 해제 실패 — {chamber_id}: {e}")
        return 0


@app.post("/requalify/{incident_id}")
def requalify(incident_id: str, body: dict = Body(...)) -> dict:
    """재인증 완료 → ChamberRequalified 1회 발행 (§8-C — R9 완료 경계).

    body: {chamber_id, qual_id, pm_count, corrections_applied[], limit_version, approved_by}
    멱등: incident_id당 1회 (재호출 409). P6-1에서 R9 그래프 resume 뒤로 정식 이관.
    """
    if not _requal_guard.check_and_mark(incident_id):
        # 🔴 **해제는 가드보다 먼저 시도한다** (2026-08-13 AWS 실측).
        #    구 코드는 여기서 곧장 409 를 던져 아래 해제 블록에 **도달하지 못했다**.
        #    그런데 그루퍼는 같은 챔버의 재발동에 **같은 incident_id 를 재사용**하므로,
        #    한 번 R9 를 발행한 Incident 가 다시 RTD 로 정지되면 해제가 영구 차단된다 —
        #    실측: `INC-20260812-SIMCH2-001` 이 20:03 재정지된 뒤 R9 가 409 만 내고
        #    CH2 가 16분 넘게 322건에 멈춰 있었다(타 챔버는 3,200→3,600 증가).
        #    증상은 "RTD 자동정지가 고장났다 / 챔버가 안 돌아온다" 로 보인다.
        #    가드의 목적은 **이벤트 중복 발행 방지**이지 물리 상태를 잠그는 것이 아니다.
        #    해제는 멱등(열린 행 없으면 0행·파일은 있으면 제거)이라 재호출이 안전하다.
        #    409 는 그대로 둔다 — 프런트(api.ts)가 이를 "이미 발행됨"(성공)으로 처리한다.
        _release_rtd_inhibit(body.get("chamber_id", ""), incident_id,
                             body.get("qual_id", ""), body.get("approved_by", ""))
        raise HTTPException(status_code=409, detail=f"이미 발행됨 — Incident당 1회 (멱등): {incident_id}")
    try:
        payload = build_chamber_requalified(
            incident_id=incident_id, chamber_id=body.get("chamber_id", ""),
            qual_id=body.get("qual_id", ""), pm_count=body.get("pm_count", 0),
            corrections_applied=body.get("corrections_applied", []),
            limit_version=body.get("limit_version", ""), approved_by=body.get("approved_by", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _regime["emit"]("ChamberRequalified", payload)
    log.info(f"📢 ChamberRequalified — {payload['chamber_id']} ({incident_id}, firm {len(payload['corrections_applied'])}건)")

    # RTD inhibit 해제 (헌법 1-1 예외 4 ⓒ) — 해제 경로는 requal 승인 하류인 '여기' 하나뿐.
    _release_rtd_inhibit(payload["chamber_id"], incident_id,
                         payload.get("qual_id", ""), payload.get("approved_by", ""))

    return {"emitted": "ChamberRequalified", **payload}


# ---------------------------------------------------------------------------
# RTD 챔버 상태 — S1 배지 + uptime % (헌법 1-1 예외 4 · 멘토 제안 uptime 위젯)
# 단일 소스 = chamber_inhibits (inhibit 구간 = downtime)
# ---------------------------------------------------------------------------
@app.get("/chambers/status")
def chambers_status(window_hours: float = 24.0) -> dict:
    """챔버별 {inhibited, inhibited_at, incident_id, uptime_pct} — S1 카드용.

    uptime% = 1 − (창과 겹치는 inhibit 시간 합 / 창 길이). 창 기본 24h
    (params rtd.uptime_window_hours — 프론트가 쿼리로 전달). 읽기 전용.
    """
    import os as _os
    import psycopg2 as _pg
    win_sec = max(1.0, float(window_hours) * 3600.0)
    conn = _pg.connect(_os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT chamber_id FROM wafer_predictions
                           UNION SELECT DISTINCT chamber_id FROM chamber_inhibits""")
            chambers = sorted(r[0] for r in cur.fetchall() if r[0])
            cur.execute(
                """SELECT chamber_id, incident_id, inhibited_at, released_at,
                          COALESCE(scope, 'chamber'), equipment_id
                     FROM chamber_inhibits
                    WHERE COALESCE(released_at, NOW()) > NOW() - make_interval(secs => %s)
                    ORDER BY inhibited_at""", (win_sec,))
            rows = cur.fetchall()
            cur.execute("SELECT NOW()")
            now = cur.fetchone()[0]
    finally:
        conn.close()

    from datetime import timedelta
    w0 = now - timedelta(seconds=win_sec)
    items = []
    for ch in chambers:
        down = 0.0
        open_inh = None
        for (cid, iid, t0, t1, sc, eq) in rows:
            if cid != ch:
                continue
            if t1 is None:
                open_inh = {"incident_id": iid, "inhibited_at": t0.isoformat(),
                            "scope": sc, "equipment_id": eq}
            down += max(0.0, (min(t1 or now, now) - max(t0, w0)).total_seconds())
        items.append({
            "chamber_id": ch,
            "inhibited": open_inh is not None,
            **({"incident_id": open_inh["incident_id"],
                "inhibited_at": open_inh["inhibited_at"],
                "scope": open_inh["scope"],                 # chamber | equipment
                **({"equipment_id": open_inh["equipment_id"]} if open_inh["equipment_id"] else {}),
                } if open_inh else {}),
            "uptime_pct": round(100.0 * (1.0 - min(down, win_sec) / win_sec), 1),
        })
    equip_stop = any(i.get("scope") == "equipment" for i in items)
    return {"window_hours": window_hours, "equipment_inhibited": equip_stop, "items": items}


# ---------------------------------------------------------------------------
# P6-3 — S11 시뮬레이터 실배선 (RUN 버튼 = CLI와 동일 효과)
# 코어는 gateway/simulator_runner.py (fastapi 무의존) — 여기는 라우트만.
# ---------------------------------------------------------------------------
from .simulator_runner import (RunnerBusy, SimulatorRunner,  # noqa: E402
                                      UnknownScenario)

_runner = SimulatorRunner()


@app.get("/simulator/scenarios")
def simulator_scenarios() -> dict:
    """시나리오 라이브러리 (src/simulator/scenarios/*.yaml) — S11 목록 소스."""
    return {"scenarios": _runner.list_scenarios()}


@app.get("/simulator/status")
def simulator_status() -> dict:
    """실행 상태 (running·scenario·pid·returncode·last_line)."""
    return _runner.status()


@app.get("/simulator/logs")
def simulator_logs(n: int = 120) -> dict:
    """producer stdout 링버퍼 tail — S11 주입 로그 패널."""
    return {"lines": _runner.logs(n)}


@app.post("/simulator/scenarios/{sid}/run")
def simulator_run(sid: str, body: dict = Body(default={})) -> dict:
    """S11 RUN = 상시 세계에 주입 (M3 ② 상주화) — 세계 미기동 시 baseline 자동 기동 후 주입.

    body {delay?, limit?}는 baseline 최초 기동에만 적용 (기본 = params persistent_run).
    응답 mode: "injected"(주입만) / "started+injected"(기동+주입). STOP만 세계 종료.
    """
    try:
        return _runner.start(sid,
                             delay=(float(body["delay"]) if "delay" in body else None),
                             limit=(int(body["limit"]) if "limit" in body else None))
    except UnknownScenario as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RunnerBusy as e:                    # 상주화 이후 미발생 — 호환 보존 (구 클라이언트)
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/simulator/stop")
def simulator_stop() -> dict:
    """실행 중지 (terminate — 데모 허용 범위, 헌법 6-2 주석 참조)."""
    return _runner.stop()


# ---------------------------------------------------------------------------
# #37 오너 편입 (2026-07-23, PM) — A 초안 이력 라우터 (wafer_predictions read-only)
# /predictions 네임스페이스: 실시간 계열 = 위 P5-4(recent·stream — 링버퍼),
# 이력 계열 = predictions_router(""·/chambers/summary·/wafer/{wafer_id} — DB).
# 파라미터 경로는 /wafer/ 아래로 격리 — 등록 순서와 무관하게 recent·stream 과 충돌 없음.
# ---------------------------------------------------------------------------
from .predictions_router import router as predictions_router  # noqa: E402

app.include_router(predictions_router)


# ---------------------------------------------------------------------------
# W6-① — S1 센서 곡선 실배선 (fdc.raw → wafer 집계 → REST + SSE) + F2·F3 데이터
# 코어는 gateway/raw_feed.py (fastapi 무의존) — 여기는 라우트만.
# useLiveFdc: 초기 이력 = /raw/recent, append = /raw/stream (event: wafer).
# F2(승인 마커)·F3(버전 칩) = /limits/active·/limits/corrections 폴링.
# ---------------------------------------------------------------------------
from .raw_feed import RawFeed  # noqa: E402

_raw = RawFeed()


@app.on_event("startup")
def _start_raw_feed() -> None:
    """fdc.raw 소비 스레드 기동 — Kafka/DB 미가용이어도 gateway는 산다."""
    try:
        _raw.start()
        log.info("✅ fdc.raw 피드 기동 (consumer-group-dashboard-raw)")
    except Exception as e:                               # noqa: BLE001
        log.warning(f"raw 피드 미기동: {e}")


@app.on_event("shutdown")
def _stop_raw_feed() -> None:
    """소비 스레드 graceful 종료 (헌법 6-2)."""
    _raw.stop()


@app.get("/raw/recent")
def raw_recent(n: int = 40, chamber: str | None = None) -> dict:
    """S1 센서 곡선 초기 이력 — wafer 점 최근 n장 (최신 우선) + 피드 상태."""
    return {"status": _raw.status(), "items": _raw.recent(n, chamber)}


async def _raw_sse():
    """SSE 제너레이터 — 접속 시점 이후 신규 wafer 점만 push (이력은 REST 담당)."""
    cursor = _raw.head_seq()
    while True:
        items = _raw.since(cursor)
        if items:
            cursor = items[-1]["_seq"]
            for it in items:
                yield f"event: wafer\ndata: {json.dumps(it, ensure_ascii=False)}\n\n"
        else:
            yield ": keep-alive\n\n"                     # 프록시 idle 타임아웃 방지
        await asyncio.sleep(1.0)


@app.get("/raw/stream")
async def raw_stream() -> StreamingResponse:
    """S1 센서 곡선 append 채널 — wafer 1장 = 이벤트 1건."""
    return StreamingResponse(_raw_sse(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# P6-2 — 실측 C65 지연 피드백 릴레이 (fdc.actual → REST + SSE)
# 코어는 gateway/actual_feed.py (fastapi 무의존). raw_feed와 동일 수명주기·조회 시그니처.
# ⚠ label_delay(≈13,900 wafer)로 시나리오 1회 중엔 실측이 거의 안 옴 — 발행자 활성화 시
#   자동 작동하는 아키텍처 완결용 순배선. 미발행 시 빈 스트림으로 무해 degrade.
# ---------------------------------------------------------------------------
from .actual_feed import ActualFeed  # noqa: E402

_actual = ActualFeed()


@app.on_event("startup")
def _start_actual_feed() -> None:
    """fdc.actual 소비 스레드 기동 — Kafka 미가용이어도 gateway는 산다."""
    try:
        _actual.start()
        log.info("✅ fdc.actual 릴레이 기동 (consumer-group-dashboard-actual)")
    except Exception as e:                               # noqa: BLE001
        log.warning(f"actual 릴레이 미기동: {e}")


@app.on_event("shutdown")
def _stop_actual_feed() -> None:
    """소비 스레드 graceful 종료 (헌법 6-2)."""
    _actual.stop()


@app.get("/actual/recent")
def actual_recent(n: int = 40, chamber: str | None = None) -> dict:
    """실측 라벨 초기 이력 — 최근 n장(최신 우선) + 피드 상태."""
    return {"status": _actual.status(), "items": _actual.recent(n, chamber)}


async def _actual_sse():
    """SSE 제너레이터 — 접속 시점 이후 신규 실측만 push (이력은 REST 담당)."""
    cursor = _actual.head_seq()
    while True:
        items = _actual.since(cursor)
        if items:
            cursor = items[-1]["_seq"]
            for it in items:
                yield f"event: actual\ndata: {json.dumps(it, ensure_ascii=False)}\n\n"
        else:
            yield ": keep-alive\n\n"                     # 프록시 idle 타임아웃 방지
        await asyncio.sleep(1.0)


@app.get("/actual/stream")
async def actual_stream() -> StreamingResponse:
    """실측 라벨 append 채널 — 실측 1건 = 이벤트 1건."""
    return StreamingResponse(_actual_sse(), media_type="text/event-stream")



def _rows_as_dicts(cur) -> list[dict]:
    """커서 결과 → dict 리스트 (timestamptz는 ISO 문자열로)."""
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        out.append(d)
    return out


@app.get("/limits/active")
def limits_active(chamber: str | None = None) -> dict:
    """현행 관리선(is_active·settled) — F3 버전 칩 소스 (S1 표시 센서 한정 아님 — 전 센서).

    P6-2 S2: trigger_type·k_sigma 동봉 — trigger_type='provisional'(가한계 Phase 0~1, 광폭
    관리선)이면 S2가 '가한계·추정' 배지 표시 (일정 v5 S2 provisional 요구).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    sql = """SELECT chamber_id, recipe_id, step, sensor_window, sensor_id, limit_version, method,
                    center, sigma, ucl, lcl, k_sigma, trigger_type, effective_from
             FROM control_limits
             WHERE is_active AND sensor_window = 'settled'"""
    args: list = []
    if chamber:
        sql += " AND chamber_id = %s"
        args.append(chamber)
    sql += " ORDER BY chamber_id, sensor_id, step"
    cur = _gate["conn"].cursor()
    try:
        cur.execute(sql, args)
        rows = _rows_as_dicts(cur)
    finally:
        cur.close()
        _gate["conn"].commit()          # read-only tx 종료 (idle-in-transaction 방지)
    return {"limits": rows}


@app.get("/limits/corrections")
def limits_corrections(chamber: str | None = None, n: int = 10) -> dict:
    """최근 correction 이력 — F2 승인 마커 소스 (최신순. status로 APPLIED 필터는 프론트)."""
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    n = max(1, min(int(n), 50))
    sql = """SELECT correction_id, chamber_id, sensor_id, recipe_id, step,
                    limit_version_before, limit_version_after,
                    center_before, center_after, center_modified,
                    delta_sigma, trigger_type, status, effective_from, created_at
             FROM limit_corrections"""
    args: list = []
    if chamber:
        sql += " WHERE chamber_id = %s"
        args.append(chamber)
    sql += " ORDER BY created_at DESC LIMIT %s"
    args.append(n)
    cur = _gate["conn"].cursor()
    try:
        cur.execute(sql, args)
        rows = _rows_as_dicts(cur)
    finally:
        cur.close()
        _gate["conn"].commit()          # read-only tx 종료
    return {"corrections": rows}


@app.get("/quals/recent")
def quals_recent(chamber: str | None = None, n: int = 10) -> dict:
    """Qual 판정 이력 — S7 화면 소스 (P6-2). 4지표·proposed/confirmed verdict·상태 (최신순).

    proposed_verdict PASS=조용/FAIL=요란 (S7 매핑은 프론트). confirmed_verdict는 R9 확정 후.
    데이터 없으면 빈 배열 — A5-3(Qual 4지표 취합) 적재 전엔 목업 폴백(프론트).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    n = max(1, min(int(n), 50))
    # 2026-08-10 — S7 확정 배선(파생 2컬럼 동봉). 스키마 변경 없음.
    #   `quals` 에는 pm_count·incident_id 컬럼이 없다(init.sql:354). 그래서 화면이
    #   POST /qual/verdict·POST /requalify 본문을 **조립할 수 없었고**, S7 버튼이
    #   로컬 상태만 바꾸고 끝나 있었다(QualScreen 분기 3개 + onR9 = 화면 이동). 두 값을
    #   읽기 전용 파생 컬럼으로 얹어 프론트를 자족하게 만든다 — 라우트는 원래부터 살아 있었다.
    #   · pm_count  : fdc.actual 이 실어오는 값이라(0012) **라벨 있는 행만** 본다.
    #     `actual_c65 IS NOT NULL` 은 `idx_wafer_predictions_label` 부분 인덱스 술어와
    #     같은 조건이라 스캔이 인덱스 안에서 끝난다 — 라벨 0건이면 즉시 0.
    #     ⚠️ 조건을 떼면 수백만 행 seq scan 이 되어 프론트 타임아웃을 먹는다.
    #   · incident_id: 요란 판정은 incident_id 필수(신규 결정 10). 챔버별 최신 1건.
    sql = """SELECT q.qual_id, q.chamber_id, q.wafer_ids, q.ae_anomaly_mean, q.tttm_gap_pct,
                    q.nelson_violations, q.c65_within_normal, q.thresholds, q.proposed_verdict,
                    q.approval_status, q.confirmed_verdict, q.confirmed_at, q.created_at,
                    (SELECT COALESCE(MAX(p.pm_count), 0) FROM wafer_predictions p
                      WHERE p.chamber_id = q.chamber_id
                        AND p.actual_c65 IS NOT NULL)              AS pm_count,
                    (SELECT i.incident_id FROM incidents i
                      WHERE i.chamber_id = q.chamber_id
                      ORDER BY i.created_at DESC LIMIT 1)          AS incident_id,
                    -- Qual wafer 5장의 **예측** C65 (2026-08-10). `quals` 에는 wafer 별 값이
                    -- 없어 S7 이 5칸을 전부 "…" 로 비워 두고 있었다 — 관객에게는 고장으로 읽힌다.
                    -- 실측 C65 는 원래 없다: Qual wafer 는 `fdc.actual` 미발행(계약 1-B, WT 미경유).
                    -- 대신 A 파이프라인은 Qual wafer 도 예측하므로(`wafer_predictions.is_qual`)
                    -- 그 값을 준다 — 화면은 이것을 **예측**으로 명시해 표기한다(실값 위장 금지).
                    -- 비용: wafer_id 5건 인덱스 조회(`idx_wafer_predictions_wafer`) — 상시 안전.
                    (SELECT jsonb_object_agg(p.wafer_id,
                                             round(p.predicted_c65::numeric, 1))
                       FROM wafer_predictions p
                      WHERE p.is_qual
                        AND p.wafer_id IN (
                              SELECT jsonb_array_elements_text(q.wafer_ids)))  AS wafer_preds
             FROM quals q"""
    args: list = []
    if chamber:
        sql += " WHERE q.chamber_id = %s"
        args.append(chamber)
    sql += " ORDER BY q.created_at DESC LIMIT %s"
    args.append(n)
    cur = _gate["conn"].cursor()
    try:
        cur.execute(sql, args)
        rows = _rows_as_dicts(cur)
    finally:
        cur.close()
        _gate["conn"].commit()
    # R9 발행 여부 (2026-08-10) — 화면이 이걸 로컬 state 로만 알아서, 새로고침하면 완료된
    # [R9 재적격 실행] 버튼이 되살아나고 다시 누르면 409 였다. 발행측 가드를 그대로 읽어
    # 화면과 서버의 답을 일치시킨다 (가드 = 프로세스 수명 · DB 컬럼 신설 없음).
    for r in rows:
        inc = r.get("incident_id")
        r["requalified"] = bool(inc) and _requal_guard.emitted(inc)
    return {"quals": rows}


@app.get("/dispositions/pending")
def dispositions_pending(chamber: str | None = None, n: int = 50) -> dict:
    """wafer 처분 목록 — S6 화면 소스 (P6-2). 잠정(HOLD)·확정 행 (최신순).

    per_wafer 장별 판정 = wafer_dispositions 행 자체(wafer당 1행 — 계약 §4-B 상태 전이).
    predicted_c65·anomaly_score는 대응 컬럼 없이 recommendation_basis/hold_reason에 텍스트
    인용(계약 §6 각주) — 프론트가 필요 시 파싱, 없으면 미표시(실값 위장 금지).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    n = max(1, min(int(n), 200))
    sql = """SELECT wafer_id, lot_id, chamber_id, incident_id, status,
                    hold_reason, system_recommendation, recommendation_basis,
                    decided_by, decided_at, created_at
             FROM wafer_dispositions"""
    args: list = []
    if chamber:
        sql += " WHERE chamber_id = %s"
        args.append(chamber)
    sql += " ORDER BY created_at DESC LIMIT %s"
    args.append(n)
    cur = _gate["conn"].cursor()
    try:
        cur.execute(sql, args)
        rows = _rows_as_dicts(cur)
    finally:
        cur.close()
        _gate["conn"].commit()
    return {"dispositions": rows}


@app.post("/dispositions/request")
def dispositions_request(body: dict = Body(...)) -> dict:
    """S6 웨이퍼별 처분 선택 → **처분 전용 승인 카드** 개설 (2026-08-11 · PM 결정).

    body: {incident_id, decisions:[{wafer_id, disposition}], requested_by?}

    왜 라우트가 따로 있나: 8/10 까지 처분 승인은 **정비 카드에 얹혀** 있었다 —
    승인 한 번이 정비 발행과 웨이퍼 확정을 동시에 했고, 판정이 Incident 단위 1개 값이라
    장별 RELEASE/SCRAP 혼재가 원리적으로 불가능했다. 이제 이 라우트가 ①선택 검증 →
    ②사실 수집 → ③**LLM 처분 Brief** → ④파생 thread 로 카드 개설을 한다.

    헌법 1-4 예외: 본선(정비/보정) PENDING 과 처분 PENDING 은 클래스별로 각 1건 공존한다
    (`open_pending`·`commit_decision` 의 request_class). thread 는 `dispo::<INC>` 로 갈린다 —
    한 thread 가 interrupt 를 둘 들 수 없는 LangGraph 제약 때문이고, **그래프는 본편 재사용**이다.

    실패 정책: 선택 검증은 fail-closed(400/409). Brief 는 LLM 실패해도 결정적 fallback 으로
    카드를 세운다(6-3) — 처분 결정을 LLM 가용성에 인질로 잡지 않는다.
    """
    if _gate["conn"] is None or _gate.get("app") is None:
        raise HTTPException(status_code=503, detail="approval gate not wired (DATABASE_URL/langgraph 필요)")
    inc = str((body or {}).get("incident_id") or "").strip()
    decisions = (body or {}).get("decisions") or []
    wafer_ids = (body or {}).get("wafer_ids") or []
    # 입력 두 형태 (2026-08-11 · PM "결정은 승인 화면에서"):
    #   ⓐ wafer_ids[]  — S6 는 **대상만** 고른다(체크박스). 판정은 에이전트 권고로 프리필하고,
    #                     엔지니어는 승인 화면에서 근거를 보며 장별로 확정한다. ← 정본 경로
    #   ⓑ decisions[]  — 판정까지 실어 보내는 경로 (스크립트·자동화·회귀 테스트용). 유지.
    if not inc:
        raise HTTPException(status_code=400, detail="incident_id 필수")
    if not decisions and not wafer_ids:
        raise HTTPException(status_code=400, detail="wafer_ids[] 또는 decisions[] 필수")
    if len(decisions) > 200 or len(wafer_ids) > 200:
        raise HTTPException(status_code=400, detail="한 번에 200장까지 (초과분은 나눠 요청)")
    sel: dict[str, str | None] = {}                   # None = 판정 미지정(권고 프리필 대상)
    for d in decisions:
        wid = str((d or {}).get("wafer_id") or "").strip()
        dp = str((d or {}).get("disposition") or "").strip().upper()
        if not wid or dp not in ("RELEASE", "SCRAP"):
            raise HTTPException(status_code=400,
                                detail=f"decisions[] 항목은 {{wafer_id, disposition∈RELEASE|SCRAP}}: {d!r}")
        sel[wid] = dp
    for w in wafer_ids:                               # 판정 미지정 = 권고 프리필 (아래 ②)
        wid = str(w or "").strip()
        if wid:
            sel.setdefault(wid, None)
    # ① 대상 검증 — 이 Incident 의 **미확정** 행만 처분 대상이다. 확정 행 재확정·타 Incident
    #    웨이퍼 혼입은 여기서 막는다(감사 정합). 원장 행이 없는 wafer_id 도 거른다.
    cur = _gate["conn"].cursor()
    try:
        cur.execute("""SELECT wafer_id, status, decided_by, system_recommendation, recommendation_basis
                         FROM wafer_dispositions
                        WHERE incident_id = %s AND wafer_id = ANY(%s)""",
                    (inc, list(sel.keys())))
        found = {r[0]: {"status": r[1], "decided_by": r[2], "rec": r[3], "basis": r[4]}
                 for r in cur.fetchall()}
        # ⚠️ selected_option 까지 봐야 한다 — 구 매핑이 정비를 request_type='disposition' 으로
        #    적었으므로 request_type 만 보면 **정비 PENDING 을 처분 카드로 오인**해 409 가 난다
        #    (8/11 실측: "처분 승인 요청이 이미 열려 있습니다" 가 실제로는 정비 카드였다).
        cur.execute("SELECT 1 FROM approval_records WHERE incident_id=%s AND status='PENDING' "
                    "AND request_type='disposition' AND selected_option='disposition_option'", (inc,))
        dispo_open = cur.fetchone() is not None
    finally:
        cur.close()
        _gate["conn"].commit()
    missing = [w for w in sel if w not in found]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"이 Incident 의 처분 원장에 없는 웨이퍼: {missing[:5]}")
    already = [w for w, v in found.items() if v["decided_by"] is not None]
    if already:
        raise HTTPException(status_code=409,
                            detail=f"이미 확정된 웨이퍼 포함 — 재확정은 별도 절차: {already[:5]}")
    if dispo_open:
        raise HTTPException(status_code=409,
                            detail=f"처분 승인 요청이 이미 열려 있습니다 (Incident당 1건): {inc}")
    # ② per_wafer 조립 — 판정 미지정(wafer_ids 경로)은 **에이전트 권고로 프리필**한다.
    #    권고도 없으면 RELEASE 로 프리필하지 않는다 — 카드에서 엔지니어가 반드시 고르도록
    #    HOLD 로 남긴다(조용한 기본값 금지 · SCRAP 은 비가역이라 더더욱).
    per_wafer = []
    for wid, dp in sel.items():
        rec = found[wid]["rec"]
        basis = (found[wid]["basis"] or "").strip()
        if dp is None:                                # 프리필 경로 — 판정은 승인 화면에서 확정
            pre = rec if rec in ("RELEASE", "SCRAP") else "HOLD"
            note = (f"에이전트 권고({rec}) 프리필 — 승인 화면에서 확정"
                    if pre != "HOLD" else "권고 없음 — 승인 화면에서 엔지니어가 판정 선택 필요")
        else:                                         # 판정 동반 경로(decisions[])
            pre = dp
            note = f"요청 시점 판정({dp})"
            if rec and rec != dp:
                note += f" — 에이전트 권고({rec}) 대신"
            elif rec:
                note += f" — 에이전트 권고({rec}) 동의"
        per_wafer.append({"wafer_id": wid, "recommendation": pre,
                          "reason": note + (f" · {basis[:160]}" if basis else "")})
    # ③ 사실 수집 + LLM Brief (실패 시 fallback — 카드는 반드시 선다)
    from . import dispo_brief as db_brief
    facts = db_brief.collect_facts(_gate["conn"], inc, list(sel.keys()))
    _gate["conn"].commit()                            # 읽기 tx 종료 (idle-in-transaction 방지)
    brief = db_brief.build_brief(facts, per_wafer, PARAMS)
    report = db_brief.build_report(facts, per_wafer, brief)
    # ④ 파생 thread 로 카드 개설 (헌법 1-4 예외)
    from ..orchestrator.approval_graph import start_disposition
    try:
        start_disposition(_gate["app"], inc, report)
    except Exception as e:                            # noqa: BLE001
        log.exception("처분 카드 개설 실패: %s", inc)
        raise HTTPException(status_code=500, detail=f"처분 카드 개설 실패: {type(e).__name__}")
    # 🔴 개설 확인 (2026-08-11) — invoke 가 성공해도 카드가 없을 수 있다: `open_pending` 은
    #   유니크 충돌(23505)을 "이미 열려 있음" 으로 삼키고 False 를 돌려주는데, 그래프는 그래도
    #   interrupt 로 멈춰서 invoke 는 정상 종료한다. 8/11 실측으로 이 조합이 **200 + 카드 0건**을
    #   만들었다(구 유니크 인덱스가 클래스 축 없이 Incident 당 1건을 강제 → 마이그레이션 0013).
    #   조용한 성공을 남기지 않는다 — 원장에 행이 보이지 않으면 실패로 답한다.
    try:
        cur2 = _gate["conn"].cursor()
        try:
            cur2.execute("SELECT 1 FROM approval_records WHERE incident_id=%s AND status='PENDING' "
                         "AND request_type='disposition' AND selected_option='disposition_option'", (inc,))
            opened = cur2.fetchone() is not None
        finally:
            cur2.close()
            _gate["conn"].commit()
    except Exception:                                 # noqa: BLE001 — 확인 실패는 판정 보류
        opened = True
    if not opened:
        log.error("처분 카드 PENDING 행 미생성 — 유니크 충돌 의심 (db/migrations/0013 확인): %s", inc)
        raise HTTPException(
            status_code=500,
            detail="처분 카드가 열리지 않았습니다 — PENDING 유니크 충돌 의심. "
                   "db/migrations/0013_pending_unique_per_class.sql 적용 여부를 확인하세요.")
    counts = report["wafer_disposition"]["counts"]
    log.info("🧾 처분 승인 요청 개설 — %s %s (LLM %s)", inc, counts,
             "OK" if (brief.get("llm") or {}).get("ok") else "fallback")
    return {"incident_id": inc, "opened": True, "counts": counts,
            "wafers": len(per_wafer), "thread": f"dispo::{inc}",
            "llm": brief.get("llm"), "confidence": brief.get("confidence")}


# ---------------------------------------------------------------------------
# 증거층 (2026-07-31 — PR #70 '서사층/증거층 분리' 결정의 PM 파트)
# 서사(판단·권고)는 LLM 이 Incident 당 1회 만들어 얼리고, 증거(위반 건수·센서·severity)는
# **화면이 조회 시점에** 여기서 읽는다. 그래야 Brief 를 다시 만들지 않고도 엔지니어가
# 항상 현재 데이터를 본다 — LLM 21회 → 1회의 근거.
# ---------------------------------------------------------------------------
#: 증거 목록 상한 — 화면은 상위 6건만 그리지만, 신규분이 뒤로 밀리지 않게 여유를 둔다.
#: **집계에는 쓰지 않는다** (그게 2026-07-31 버그의 원인이었다).
EVIDENCE_LIST_MAX = 60


def _evidence_of(conn, incident_id: str) -> dict:
    """Incident 증거층 1건 — 위반 집계 + Brief 생성 이후 신규분 (배지 소스).

    집계 소스는 `incident_alerts`(멤버십 정본, raw 에 원본 payload 보존)다. spc_violations 가
    아니라 여기서 세는 이유는 그룹핑이 PM 소관이기 때문(계약 §4 — incidents_recent 와 동일 규칙).

    `since_brief` = 최신 SUP Brief 의 created_at 이후 편입된 알람. 이게 배지의 "생성 이후
    +N건 · 신규 센서" 다 — 화면이 낡음을 **드러내고** 사람이 [재분석] 을 당기게 하는 장치이지,
    시스템이 pending 을 몰래 갱신하는 게 아니다(감사 정합).
    """
    # ⚠️ 집계와 목록을 **다른 쿼리**로 나눈다 (2026-07-31 실측 버그 수정).
    #   목록에 LIMIT 을 걸고 그 결과로 집계까지 하면, 최신순으로 잘린 창이 전부 Brief
    #   이후 것이라 "알람 200건 · 생성 이후 +200건 · 신규 센서 16개"처럼 **본 것 전부**가
    #   신규로 잡힌다. 집계는 항상 Incident 전체를, 목록만 상한을 둔다.
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(brief_version, 1), needs_reanalysis, created_at
                 FROM agent_reports WHERE incident_id = %s AND report_id LIKE 'SUP-%%'
                ORDER BY id DESC LIMIT 1""", (incident_id,))
        b = cur.fetchone()
        brief_at = b[2] if b else None

        # ① 알람 집계 — 전체
        cur.execute(
            """SELECT count(*) AS n_all,
                      count(*) FILTER (WHERE %s::timestamptz IS NOT NULL
                                         AND ia.created_at > %s::timestamptz) AS n_new
                 FROM incident_alerts ia WHERE ia.incident_id = %s""",
            (brief_at, brief_at, incident_id))
        n_all, n_new = cur.fetchone()

        # ② 센서 집합 — 전체 (jsonb 전개). old/new 를 갈라야 "처음 등장한 센서"가 나온다
        cur.execute(
            """SELECT DISTINCT v->>'sensor' AS sensor,
                      (%s::timestamptz IS NOT NULL AND ia.created_at > %s::timestamptz) AS is_new
                 FROM incident_alerts ia,
                      LATERAL jsonb_array_elements(COALESCE(ia.raw->'violations', '[]'::jsonb)) v
                WHERE ia.incident_id = %s AND v->>'sensor' IS NOT NULL""",
            (brief_at, brief_at, incident_id))
        sensors_all: set[str] = set()
        sensors_new: set[str] = set()
        sensors_old: set[str] = set()
        for sid, is_new in cur.fetchall():
            sensors_all.add(sid)
            (sensors_new if is_new else sensors_old).add(sid)

        # ③ 목록 — 화면 표시분만 (신규 우선, 그다음 최신순)
        cur.execute(
            """SELECT ia.alert_id, ia.severity, ia.context_score, ia.alert_ts,
                      ia.suspect_start, ia.suspect_end, ia.raw,
                      (%s::timestamptz IS NOT NULL AND ia.created_at > %s::timestamptz) AS is_new
                 FROM incident_alerts ia
                WHERE ia.incident_id = %s
                ORDER BY is_new DESC, ia.alert_ts DESC
                LIMIT %s""", (brief_at, brief_at, incident_id, EVIDENCE_LIST_MAX))
        rows = _rows_as_dicts(cur)

    violations: list[dict] = []
    for r in rows:
        raw = r.get("raw") or {}
        for v in (raw.get("violations") or []):
            if not isinstance(v, dict):
                continue                              # 계약 밖 형태는 조용히 건너뛴다 (6-2)
            sid = v.get("sensor")
            violations.append({
                "alert_id": r.get("alert_id"), "alert_ts": r.get("alert_ts"),
                "sensor": str(sid) if sid else None, "rule_id": v.get("rule_id"),
                "severity": v.get("severity") or r.get("severity"),
                "description": v.get("description"),
                "current_value": v.get("current_value"),
                "limit_version": v.get("limit_version"),
                "is_new": bool(r.get("is_new")),
            })
    return {
        "incident_id": incident_id,
        "alarms": int(n_all or 0),                    # 전체 (목록 상한과 무관)
        "violations": violations[:EVIDENCE_LIST_MAX],
        "violations_truncated": int(n_all or 0) > len(rows),
        "sensors": sorted(sensors_all),
        # 배지 — new_sensors 는 "Brief 이후 **처음** 등장한" 센서만 (기존 등장분 제외).
        "since_brief": {
            "alarms": int(n_new or 0),
            "new_sensors": sorted(sensors_new - sensors_old),
            "stale": int(n_new or 0) > 0,
        },
        "brief_version": (b[0] if b else None),
        "needs_reanalysis": bool(b[1]) if b else False,
        "brief_created_at": brief_at,
    }


@app.get("/incidents/{incident_id}/evidence")
def incident_evidence(incident_id: str) -> dict:
    """S4 02 패널의 증거층 — 조회 시점 기준 (Brief 재생성 없이 최신값)."""
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    try:
        return _evidence_of(_gate["conn"], incident_id)
    finally:
        _gate["conn"].commit()                        # SELECT 도 트랜잭션을 연다 (idle-in-tx 방지)


@app.post("/incidents/{incident_id}/reanalyze")
def incident_reanalyze(incident_id: str) -> dict:
    """[재분석] — 최신 Brief 에 needs_reanalysis 를 세운다 (재생성은 C 가 소비).

    시스템이 자동으로 다시 만들지 않는 이유: 자동 재생성은 "빨리 내면 정보 부족, 기다리면
    늦음" 딜레마에 빠지고 pending 이 사람 모르게 바뀐다. 사람이 당기는 구조면 그 딜레마가
    소멸하고 감사도 깨끗하다 (PR #70 결정).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    conn = _gate["conn"]
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE agent_reports SET needs_reanalysis = TRUE
                    WHERE id = (SELECT id FROM agent_reports
                                 WHERE incident_id = %s AND report_id LIKE 'SUP-%%'
                                 ORDER BY id DESC LIMIT 1)
                RETURNING report_id, COALESCE(brief_version, 1)""", (incident_id,))
            row = cur.fetchone()
        conn.commit()
    except Exception as e:                            # noqa: BLE001
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"재분석 표시 실패: {type(e).__name__}")
    if row is None:
        raise HTTPException(status_code=404, detail=f"Brief 없음: {incident_id}")
    return {"incident_id": incident_id, "report_id": row[0],
            "brief_version": row[1], "needs_reanalysis": True}


@app.get("/kpi/agent")
def kpi() -> dict:
    """S8 KPI — 처리 속도 + 알람 파레토 + 챔버별 인시던트 + 처리 추이.

    소비처 셋(S8 KPI · S1 DecisionQueue · Copilot 독)이 같은 mock 을 쓰고 있어 한
    엔드포인트로 먹인다(PM 요청분 2026-08-12).
    조회·계약·주의사항은 전부 `src/gateway/kpi.py` — 여기는 커넥션 대여와 503 만 맡는다.

    ⚠️ **`_gate["conn"]` 을 쓰지 않는다.** 이 엔드포인트는 화면이 5초마다 두드리는 사실상의
    **폴러**인데, 그 커넥션은 요청 스레드와 공유라 psycopg2 스레드 비안전에 걸린다(:495·:560
    의 기존 폴러 주석과 같은 이유). 더 나쁜 건 `fetch_kpi` 가 읽기 tx 를 닫으려고 commit 한다는
    점이다 — 공유 커넥션이면 **다른 스레드가 쓰는 중인 승인 감사 UPDATE(:416)를 조기 커밋**할
    수 있다(헌법 1-1 감사 경로). 그래서 풀에서 빌려 쓰고 반납한다.
    """
    from .kpi import fetch_kpi                        # 지연 import — approvals 와 같은 배치

    pool = _gate.get("pool")
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    # ⚠️ config 접근과 `getconn()` 을 **try 안에** 둔다.
    #   · `PARAMS["operations"]` 하드 서브스크립트: 그 블록이 사라진 적이 있다(TSR-0006).
    #     밖에 두면 KeyError 가 그대로 새서 500 + 트레이스백만 남고 어느 키인지 안 보인다.
    #   · `ThreadedConnectionPool.getconn()` 은 고갈 시 **블록하지 않고 PoolError 를 던진다**.
    #     밖에 두면 아래 except 를 못 타 "풀이 없다"는 단서가 로그에 안 남는다.
    ok = True
    conn = None
    try:
        # config 를 **먼저 다 읽고** 커넥션을 빌린다 — 키가 없으면 빌리지도 않는다.
        #   `fetch_kpi(...)` 인자 자리에서 읽으면 이미 대여한 뒤에 KeyError 가 나서,
        #   쓰지도 못할 커넥션을 빌렸다 반납하는 왕복이 생긴다(고갈 시엔 남의 몫을 뺏는다).
        ops = PARAMS["operations"]
        window, buckets, limit = (ops["kpi_window_hours"], ops["kpi_trend_buckets"],
                                  ops["kpi_pareto_limit"])
        conn = pool.getconn()
        return fetch_kpi(conn, window, buckets, limit)
    except Exception as e:                            # noqa: BLE001
        ok = False
        log.warning(f"/kpi 조회 실패 — 화면은 목업 폴백: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"kpi 조회 실패: {type(e).__name__}")
    finally:
        # 반납 위생 (ct2_deploy_approval `_put` 과 같은 규약) — 중단된 tx 를 안 정리하고
        #   되돌리면 **다음 대여자**가 첫 쿼리에서 InFailedSqlTransaction 을 맞는다.
        #   `conn is None` = 대여 전에 터진 경우(config 유실·풀 고갈) — 반납할 것이 없다.
        if conn is not None:
            try:
                conn.rollback()
            except Exception:                         # noqa: BLE001 — 반납은 무조건 한다
                ok = False
                log.warning("/kpi 커넥션 반납 전 rollback 실패 — 폐기 반납", exc_info=True)
            pool.putconn(conn, close=not ok)


@app.get("/incidents/recent")
def incidents_recent(n: int = 60, chamber: str | None = None) -> dict:
    """S3 Incident 목록 — incidents + 알람 집계 + 최신 Agent 판정 (P6-2 후속).

    화면이 목업이던 자리(2026-07-30 리허설 실측 — 백엔드는 관통했는데 S3 에 안 보였다).
    · `alarms`/`context_score` = incident_alerts 집계. 멤버십 정본이 이 테이블이므로
      spc_violations 가 아니라 여기서 센다(계약 §4 — 그룹핑=PM 소관).
    · **점수는 max 와 avg 를 함께 낸다** — max 는 분류(가장 심각한 알람이 우선순위를 끈다),
      avg 는 전반 수준. max 만 내면 알람 925건 중 1건만 100이어도 Incident 가 100으로 보여
      DB 평균과 대조할 때 오해가 난다(2026-07-30 PM 실사용 혼선).
    · `title`/`verdict` = agent_reports 최신 1행(LATERAL). Agent 미가동 구간에는 NULL —
      프론트가 incident_type 으로 대체 문구를 만든다(실값 위장 금지, 계약 §6 각주 준수).
    · 정렬은 updated_at DESC — 그루퍼가 알람 편입마다 갱신하므로 "최근 움직인 사건" 순.
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    n = max(1, min(int(n), 200))
    sql = """
        SELECT i.incident_id, i.chamber_id, i.lifecycle, i.incident_type, i.severity_max,
               i.priority_score, i.reopen_count, i.is_escalated, i.claimed_by,
               i.created_at, i.updated_at, i.closed_at,
               COALESCE(a.n, 0)          AS alarms,
               COALESCE(a.max_score, 0)  AS context_score,
               a.avg_score               AS context_score_avg,
               r.supervisor_reason        AS title,
               r.supervisor_recommendation AS verdict,
               EXTRACT(EPOCH FROM (NOW() - i.created_at)) AS age_sec
          FROM incidents i
          LEFT JOIN (SELECT incident_id, count(*) AS n, max(context_score) AS max_score,
                            round(avg(context_score)::numeric, 1) AS avg_score
                       FROM incident_alerts GROUP BY incident_id) a
                 ON a.incident_id = i.incident_id
          LEFT JOIN LATERAL (SELECT supervisor_reason, supervisor_recommendation
                               FROM agent_reports ar
                              WHERE ar.incident_id = i.incident_id
                              ORDER BY ar.id DESC LIMIT 1) r ON TRUE
    """
    args: list = []
    if chamber:
        sql += " WHERE i.chamber_id = %s"
        args.append(chamber)
    sql += " ORDER BY i.updated_at DESC LIMIT %s"
    args.append(n)
    cur = _gate["conn"].cursor()
    try:
        cur.execute(sql, args)
        rows = _rows_as_dicts(cur)
    finally:
        cur.close()
        _gate["conn"].commit()
    return {"incidents": rows}


@app.get("/rtd/status")
def rtd_status() -> dict:
    """RTD 3단(장비 정지 「제안」) 판정 — 순수 조회·쓰기 0건 (2026-08-04 PM · 8/5 GO/NO-GO 대상).

    경로① 자동 감지: 어느 챔버든 `reference_suspect` 가 **연속 rtd_consecutive_k 롤업**(웨이퍼당
    챔버당 1롤업 ≈ K장) 지속되면 정지 제안. 근거 = 2026-08-03 실측(tttm_comparisons 124,846행):
    노이즈 런 최장 15 / 이벤트 런 최단 20 / **16~19 빈 골짜기**. K 는 params — B 검증(8/5 오전)으로
    값만 교체(코드 무변경). 판정 = 챔버별 **꼬리 열린 런**(마지막 false 이후 연속 true 개수).
    경로② 격리 승격(동시 격리 챔버 >= rtd_isolated_min_chambers): **2026-08-10 배선 완료**
    (구 not_wired 해제). 정의 = `chamber_inhibits.released_at IS NULL` 인 distinct 챔버,
    scope='equipment' 제외(자기참조 방지). auto_inhibit=false 인 동안 0 이 정상값이며 그건
    "정지 중 챔버 없음"이라는 실측이지 배선 부재가 아니다 — status 로 구분한다.
    카드는 상태 파생이다 — 런이 끊기면 제안도 스스로 내려간다(발화 이벤트 저장 없음·새 테이블 0건,
    8/3 §2-12-다). **자동 정지 아님** — 결정은 사람(헌법 1-1 HITL·멘토: RTD 는 한두 달에 한 번짜리 결정).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    p = load_params().get("incident", {})
    k = int(p.get("rtd_consecutive_k", 18))
    scan = int(p.get("rtd_scan_rollups", 4000))
    iso_min = int(p.get("rtd_isolated_min_chambers", 2))
    sql = """
        WITH tail AS (
            SELECT chamber_id, id, is_reference_suspect
              FROM tttm_comparisons
             WHERE id > (SELECT COALESCE(MAX(id), 0) FROM tttm_comparisons) - %s
        ), last_false AS (
            SELECT chamber_id, MAX(id) AS f_id
              FROM tail WHERE NOT is_reference_suspect GROUP BY chamber_id
        ), runs AS (
            SELECT t.chamber_id, COUNT(*) AS open_run
              FROM tail t
              LEFT JOIN last_false f ON f.chamber_id = t.chamber_id
             WHERE t.is_reference_suspect AND t.id > COALESCE(f.f_id, 0)
             GROUP BY t.chamber_id
        )
        SELECT c.chamber_id, COALESCE(r.open_run, 0) AS open_run
          FROM (SELECT DISTINCT chamber_id FROM tail) c
          LEFT JOIN runs r ON r.chamber_id = c.chamber_id
         ORDER BY c.chamber_id
    """
    cur = _gate["conn"].cursor()
    try:
        cur.execute(sql, (scan,))
        rows = [{"chamber_id": r[0], "open_run": int(r[1]), "fired": int(r[1]) >= k}
                for r in cur.fetchall()]
        # 경로② 격리 승격 — 2026-08-10 배선 (구 not_wired 해제).
        #   8/4 신설 때 미배선이던 사유는 "2단 챔버 격리 미구현 = 셀 대상이 없다"였다.
        #   mig 0009 가 `chamber_inhibits.scope`('chamber'|'equipment')를 넣으면서 대상이
        #   생겼다 — **정지 중인 챔버 = released_at IS NULL** (0008 이 그 조건으로 부분
        #   유니크 인덱스까지 걸어 뒀다). 그 distinct 챔버 수가 임계 이상이면 장비 승격 제안.
        #   ⚠️ 0 과 미배선을 구분한다: `auto_inhibit=false` 이고 #168 severity 버그가
        #      살아 있는 동안 이 값은 **정상적으로 0**이며, 그건 "정지 중 챔버 없음"이라는
        #      실측이지 배선 부재가 아니다. status 로 그 둘을 구분해 표기한다.
        #   'equipment' 행은 세지 않는다 — 이미 장비 정지인 것을 다시 승격 근거로 쓰면
        #   자기참조가 된다(0009: 장비 정지도 행은 챔버별로 만든다).
        #   ⚠️ `scope` 는 mig 0009 산물이다. 2026-08-10 실사에서 **mig 0005 미적용**이 발견돼
        #      (agent_reports.supervisor_history 부재) 마이그레이션 적용 상태를 신뢰할 수 없다.
        #      컬럼이 없으면 이 조회가 예외를 던져 /rtd/status 전체가 500 이 되고 상단 칩이
        #      죽는다 — 조회만 실패시키고 lane2 를 not_wired 로 되돌린다(화면 생존 · 6-2).
        iso_rows: list = []
        iso_wired = True
        try:
            cur.execute(
                """SELECT chamber_id, scope, incident_id, inhibited_at
                     FROM chamber_inhibits
                    WHERE released_at IS NULL
                    ORDER BY inhibited_at""")
            iso_rows = _rows_as_dicts(cur)
        except Exception as e:                       # noqa: BLE001 — 스키마 미적용·테이블 부재
            iso_wired = False
            _gate["conn"].rollback()                 # 실패 트랜잭션 정리 (뒤 쿼리 보호)
            log.warning("lane2 조회 실패 → not_wired 회귀 (mig 0008/0009 확인 필요): %s", e)
    finally:
        cur.close()
        _gate["conn"].commit()
    fired = [r["chamber_id"] for r in rows if r["fired"]]
    iso_ch = sorted({str(r["chamber_id"]) for r in iso_rows
                     if str(r.get("scope") or "chamber") != "equipment"})
    iso_promote = len(iso_ch) >= iso_min
    # 장비 스코프 신호는 두 갈래이고 둘 다 표기한다 (mig 0009 머리말: "장비 정지 = 공용 설비
    # 공통 원인 → 4챔버 동시. 우리 신호는 RTD 경로①(reference_suspect 연속 K)이 그대로 대응한다").
    #   ⓐ 경로② = 실제 정지 중 챔버 수 (사후적 · 정지가 이미 났을 때만 성립)
    #   ⓑ 경로① 파생 = 공통 이동이 2챔버 이상에서 동시 발화 (선행적 · 정지 전에 뜬다)
    eq_by_lane1 = len(fired) >= iso_min
    return {
        "proposed": bool(fired) or iso_promote,
        "lane1": {"k": k, "chambers": rows,
                  "max_open_run": max((r["open_run"] for r in rows), default=0),
                  "fired_chambers": fired},
        "lane2": {"status": ("wired" if iso_wired else "not_wired"), "isolated_min": iso_min,
                  "isolated_chambers": iso_ch, "isolated_count": len(iso_ch),
                  "promoted": iso_promote,
                  "note": ("chamber_inhibits 조회 실패 — mig 0008/0009 적용 확인 필요"
                           if not iso_wired else
                           "정지 중 챔버 없음 — auto_inhibit=false 인 동안은 정상값 0"
                           if not iso_ch else
                           f"정지 중 {len(iso_ch)}개 / 승격 임계 {iso_min}")},
        "equipment_scope": {"by_isolated_count": iso_promote, "by_common_shift": eq_by_lane1,
                            "note": "장비 승격 근거 2갈래 — 정지 중 챔버 수(사후) / 공통 이동 동시 발화(선행)"},
        "basis": "2026-08-03 실측: 노이즈 런 <=15 / 이벤트 런 >=20 / 빈 골짜기 16~19 — K 는 B 검증 대기",
        "scan_rollups": scan,
        "ts": now_iso(),
    }


@app.post("/agent/query")
async def agent_query(body: dict = Body(...)) -> dict:
    """S9 Agent Copilot — 자연어 질의 1건 (화면 정의서 S9 · 2026-08-11 신설).

    body: `{question, context?: {chamber?, incident_id?, sensor?, range?}, history?: [{question, answer}]}`

    **읽기 전용이다.** 승인·판정·조치 경로가 없고, 오케스트레이터도 조회만 한다 —
    조치가 필요한 답이면 `deeplink` 로 정규 UI 를 가리킬 뿐이다(S9 원칙 ①).

    ⚠️ **500 을 내지 않는다.** 대화창은 사람이 보고 있어서 에러 화면이 뜨면 다시 묻지 못한다.
    오케스트레이터가 실패해도 `unsupported=true` 인 유효 응답이 돌아온다(헌법 6-3).
    입력이 잘못된 경우(질문 없음)만 400 이다 — 그건 사람이 고칠 수 있는 것이라 알려야 한다.
    """
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 이 비어 있습니다")

    raw_ctx = body.get("context")
    if raw_ctx is not None and not isinstance(raw_ctx, dict):
        # json 파싱 성공이 dict 를 뜻하지 않는다 (헌법 7장) — 형이 틀리면 컨텍스트 없이 답한다.
        log.warning(f"⚠️ /agent/query context 가 object 가 아니다 (type={type(raw_ctx).__name__}) — 무시")
        raw_ctx = None

    from src.agent_service.app.config import load_settings
    from src.agent_service.app.copilot import answer_question
    from src.agent_service.app.llm.factory import make_backend
    from src.agent_service.app.schemas.copilot import CopilotContext, make_fallback_answer

    # 후속 질문("다른 챔버는?")이 직전 턴을 가리키므로 이력을 받는다. 형이 틀리면 무시하고
    # 단발 질의로 답한다 — 이력은 맥락 보조지 답의 재료가 아니다.
    raw_hist = body.get("history")
    history = [h for h in raw_hist if isinstance(h, dict)] if isinstance(raw_hist, list) else None

    try:
        settings = load_settings()
        ctx = CopilotContext.model_validate(raw_ctx or {})
        answer = await answer_question(question, ctx, make_backend(settings), settings,
                                       history=history)
    except Exception as e:                            # noqa: BLE001 — 대화창은 죽이지 않는다
        log.warning(f"⚠️ /agent/query 실패 ({type(e).__name__}: {e}) — fallback 응답")
        answer = make_fallback_answer(f"조회 중 오류가 발생했습니다({type(e).__name__})")

    return answer.model_dump()


@app.get("/requalify/{incident_id}/package")
def requalify_package(incident_id: str) -> dict:
    """R9 승인 패키지 (§8-D 정본) — 대상 챔버 limit_corrections PROPOSED 수합 + settle_converged 경고.

    조립 orchestrator(PM) · 데이터 정본 B(PROPOSED) · C 무관. settle_converged=false(미수렴 폴백)면
    S7 승인 화면에 경고 배지 필수 (#53 연동 — "정착 미수렴, 승인 검토 주의"). 대표값 = 행들의 AND.

    **`release_brief` 합류 (2026-08-10 · 계약 §8-D-1 「소비」 항 이행 · #140)**: RTD 자동 정지된
    챔버의 재가동 판단 근거를 같은 패키지에 실어 S7 이 한 번에 읽게 한다. `incident_id` 1:1 이고
    **없을 수 있다**(정지가 없던 Incident) — 그때는 `null` 이며 화면은 그 절만 비운다.

    ⚠️ 없으면 **Brief 가 `release_briefs` 에 쌓이는데 화면에 영영 안 뜬다** — 적재도 로그도
    정상이라 밖에서 구별되지 않는다(계약에 "PM 배선 대기"로 남아 있던 자리를 여기서 닫는다).
    ⚠️ `release_authority` 는 **싣지 않는다** — 타입 상수(`"requal_approval_only"`)라 컬럼 자체가
    없고, 화면이 해제 권한을 표현할 수 있으면 무승인 통과 경로가 열린다(헌법 1-1 예외 4 ⓒ).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    cur = _gate["conn"].cursor()
    try:
        cur.execute(
            """SELECT correction_id, sensor_id, step, center_before, center_after,
                      ucl_after, lcl_after, calc_window_n, settle_converged, chamber_id
                 FROM limit_corrections
                WHERE incident_id = %s AND status = 'PROPOSED'
                ORDER BY sensor_id, step""",
            (incident_id,))
        corrections = _rows_as_dicts(cur)
        # 해제 근거 Brief — 같은 Incident 의 최신 1건. 테이블이 없는 구 DB(마이그레이션 0014
        # 미적용)에서도 패키지 전체가 죽지 않게 독립 try 로 감싼다: 이 절이 비는 것과
        # 승인 화면이 통째로 503 이 되는 것은 무게가 다르다 (6-2 무중단).
        release_brief = None
        try:
            cur.execute(
                """SELECT report_id, readiness, summary, evidence, counter_evidence,
                          confidence, precheck_items, retrospect, scope, trigger_alert_id,
                          inhibited_at, created_at
                     FROM release_briefs
                    WHERE incident_id = %s
                    ORDER BY created_at DESC LIMIT 1""",
                (incident_id,))
            rows = _rows_as_dicts(cur)
            release_brief = rows[0] if rows else None
        except Exception as exc:  # noqa: BLE001 — UndefinedTable 포함
            log.warning(f"release_brief 조회 실패 — 그 절 없이 진행 ({type(exc).__name__}: {exc})")
            _gate["conn"].rollback()   # aborted 트랜잭션이 pool 로 돌아가지 않게 (0001 선례)
    finally:
        cur.close()
        _gate["conn"].commit()
    # settle_converged 대표값 = AND (하나라도 false면 경고). NULL(비-재수립)은 판단 제외
    flags = [c.get("settle_converged") for c in corrections if c.get("settle_converged") is not None]
    converged = all(flags) if flags else None
    return {
        "request_type": "requalify", "incident_id": incident_id,
        "chamber_id": corrections[0]["chamber_id"] if corrections else None,
        "corrections": corrections, "settle_converged": converged,
        "warning": (None if converged is not False
                    else "정착 미수렴 상태의 재수립 제안 — 승인 검토 주의 (#53)"),
        # 계약 §8-D-1. null = 이 Incident 에 RTD 정지가 없었다(정상) — 화면은 그 절만 비운다.
        "release_brief": release_brief,
    }

# ---- P6-4 원클릭 리허설 — 백엔드 스택 관리 (S11 스택 패널 소스) ----------------------
#   서비스 정의 = params.yaml stack.services (화이트리스트 — REST는 name만 받는다).
#   기동 흐름: docker compose up -d → GATEWAY(본 프로세스) → 브라우저 [서비스 모두 시작].
_stack: dict = {"runner": None}


@app.post("/stack/restart-gateway")
def stack_restart_gateway() -> dict:
    """게이트웨이 자기 재시작 (2026-08-11 · S11 버튼용) — 터미널 없이 마운트 코드 리로드.

    도커 소켓 없이 되는 이유: compose 의 gateway 는 `restart: unless-stopped` 라
    **프로세스가 스스로 죽으면 docker 가 되살린다.** 마운트(./:/app) 방식이므로
    재기동 = 최신 코드 반영이다. 응답을 먼저 보내고 0.5초 뒤 종료한다 — 안 그러면
    호출자가 응답을 못 받고 연결 오류로 읽는다. 복귀는 3~8초(헬스체크 포함).
    ⚠️ 자식 시뮬레이터도 함께 죽는다(한 몸 — 설계 v2 §7). 시연 중에는 누르지 말 것 —
    S11 이 이 버튼을 시뮬 STOP 상태에서만 활성화하는 이유다.
    """
    import os as _os
    import threading
    threading.Timer(0.5, lambda: _os._exit(0)).start()
    return {"detail": "gateway 재시작 — 3~8초 뒤 복귀 (compose unless-stopped 가 되살림)",
            "warning": "자식 시뮬레이터도 함께 종료됨"}


@app.get("/stack/container-logs/{name}")
def stack_container_logs(name: str, n: int = 80) -> dict:
    """컨테이너 로그 꼬리 (2026-08-11 리허설 E2E 진단용) — fdc-* 한정.

    /stack/logs/{name} 은 호스트 자식 프로세스 링버퍼라 컨테이너 이관 후 빈 목록이다.
    회차 초기화와 같은 전제(게이트웨이 이미지 docker CLI + 소켓)로 docker logs 를 읽는다."""
    import subprocess
    if not name.startswith("fdc-"):
        raise HTTPException(status_code=400, detail="fdc-* 컨테이너만 허용")
    try:
        r = subprocess.run(["docker", "logs", "--tail", str(min(int(n), 400)), name],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        lines = ((r.stdout or "") + ("\n" if r.stdout and r.stderr else "") +
                 (r.stderr or "")).splitlines()
        return {"name": name, "rc": r.returncode, "lines": lines[-min(int(n), 400):]}
    except Exception as e:                              # noqa: BLE001
        return {"name": name, "rc": -1, "lines": [str(e)[:200]]}


@app.post("/stack/sync-agent-prompt")
def stack_sync_agent_prompt() -> dict:
    """supervisor.py 프롬프트를 실행 중 agent-service 컨테이너에 반영 (2026-08-11 리허설).

    agent-service 는 소스를 굽는 이미지(볼륨 미마운트)라 프롬프트 수정 반영이 재빌드를
    요구한다. 리허설 중 반복 조정을 위해 docker cp + restart 로 대체한다 (SC3 검증 때
    수동으로 하던 절차의 버튼화). 정본 반영은 이미지 재빌드 + PR (agent_service 3-1)."""
    import subprocess
    src = "/app/src/agent_service/app/supervisor.py"
    dst = "fdc-agent-service:/app/src/agent_service/app/supervisor.py"
    try:
        r1 = subprocess.run(["docker", "cp", src, dst],
                            capture_output=True, text=True, timeout=30)
        if r1.returncode != 0:
            return {"ok": False, "step": "cp", "detail": (r1.stderr or "")[:200]}
        r2 = subprocess.run(["docker", "restart", "fdc-agent-service"],
                            capture_output=True, text=True, timeout=90)
        return {"ok": r2.returncode == 0, "step": "restart",
                "detail": (r2.stderr or "restarted")[:200]}
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "detail": str(e)[:200]}


@app.post("/stack/restart-frontend")
def stack_restart_frontend() -> dict:
    """frontend(nginx) 재시작 — restart-gateway 의 짝꿍 (2026-08-11 리허설 E2E 실측).

    왜: nginx 는 proxy_pass 의 gateway 호스트명을 **기동 시 1회** 해석한다. 게이트웨이
    컨테이너가 재시작·재생성으로 IP 가 바뀌면 gateway 는 8000 에서 멀쩡히 사는데
    /api 만 502 로 남는다. frontend 재시작이 재해석의 최소 수단이다
    (frontend 는 굽는 이미지 — nginx 설정 교체는 재빌드라 리허설 전 금지).
    게이트웨이에 docker CLI + 소켓이 있어 가능하다 (회차 초기화와 같은 전제)."""
    import subprocess
    try:
        r = subprocess.run(["docker", "restart", "fdc-frontend"],
                           capture_output=True, text=True, timeout=60)
        ok = r.returncode == 0
        return {"ok": ok,
                "detail": ("frontend 재시작 — 2~3초 뒤 /api 복귀" if ok
                           else (r.stderr or "")[:200])}
    except Exception as e:                              # noqa: BLE001 — CLI 부재 등
        return {"ok": False, "detail": str(e)[:200]}


def _stack_runner():
    """지연 생성 — params 로드 실패해도 /health 는 산다 (지연 배선 규약)."""
    if _stack["runner"] is None:
        from .stack_runner import StackRunner
        _stack["runner"] = StackRunner()
    return _stack["runner"]


@app.get("/stack/status")
def stack_status() -> dict:
    """S11 스택 패널 3초 폴링 소스.

    `services` = 호스트 자식 프로세스(params `stack.services` — #88·#130·#142 이관으로 현재 빈 목록)
    `containers` = compose 컨테이너 상태 (2026-08-10 추가 · 읽기 전용). 패널의 실제 용도는 이쪽이다 —
      agent-service 가 죽으면 Brief 0건, spc-consumer 가 Restarting 이면 알람 0건인데
      화면만 보면 '조용한 정상'과 구분이 안 된다.
    """
    from .stack_runner import compose_status, llm_status, target_status
    r = _stack_runner()
    containers = compose_status()
    # 연결 **대상**도 같은 목록에 싣는다 (2026-08-13 신설). 컨테이너가 전부 초록이어도
    # 그것들이 **어디에 붙어 있는지**는 패널에 없었다 — 회차 초기화가 RDS 가 아니라
    # 이름이 같은 빈 로컬 DB 를 지우고 `exit 0` 을 낸 사고가 여기서 안 보였다.
    containers.extend(target_status())
    # LLM 은 컨테이너가 아니지만(별도 GPU 노드) **판정에 직접 걸리는 의존성**이라 같은
    # 목록에 싣는다 — 안 실으면 "GPU 가 안 닿는데 패널은 전부 초록"이 된다. 위 docstring
    # 의 취지(조용한 정상과 구분)가 LLM 에도 그대로 적용된다 (2026-08-12 신설).
    llm = llm_status()
    if llm:
        containers.append(llm)
    return {"services": r.status(), "containers": containers}


@app.post("/stack/start")
def stack_start(body: dict = Body(default={})) -> dict:
    """서비스 기동 — body.name 지정 시 1개, 생략 시 enabled 전체 (registered 순서)."""
    r = _stack_runner()
    try:
        return {"services": r.start((body or {}).get("name"))}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"미등록 서비스: {e} (params stack.services 참조)")


@app.post("/stack/stop")
def stack_stop(body: dict = Body(default={})) -> dict:
    """서비스 정지 — body.name 지정 시 1개, 생략 시 전체."""
    r = _stack_runner()
    try:
        return {"services": r.stop((body or {}).get("name"))}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"미등록 서비스: {e}")


# ---------------------------------------------------------------------------
# 회차 초기화 (2026-08-10) — 「준비까지 버튼만」의 마지막 조각.
#   A-2 만 회차마다 터미널을 요구해 '다음-다음-다음'이 거기서 끊겼다. 8/10 리허설에서 손으로
#   돈 절차(세계 정지 → 컨슈머 정지 → 오프셋 latest → 초기화 SQL → Qual 재시딩 → 재기동)를
#   같은 순서로 옮긴다. 60초 이상 걸려 동기 응답이 불가하므로 **202 + 폴링**.
#   순서·금지사항의 근거는 gateway/demo_reset.py 머리말 (볼륨 삭제 계열은 쓰지 않는다).
# ---------------------------------------------------------------------------
from .demo_reset import DemoResetRunner  # noqa: E402

_demo_reset = DemoResetRunner(stop_world=lambda: _runner.stop())


@app.post("/demo/reset")
def demo_reset_start() -> dict:
    """회차 초기화 시작 — 즉시 반환(202 의미). 진행은 /demo/reset/status 폴링."""
    return _demo_reset.start()


@app.get("/demo/reset/status")
def demo_reset_status() -> dict:
    """단계·경과·로그 — S11 초기화 패널 2초 폴링 소스."""
    return _demo_reset.status()


@app.get("/stack/logs/{name}")
def stack_logs(name: str, n: int = 120) -> dict:
    """서비스 로그 꼬리 — 링버퍼 (new_console 서비스는 빈 목록: 로그는 그 창에)."""
    r = _stack_runner()
    try:
        return {"name": name, "lines": r.logs(name, n)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"미등록 서비스: {name}")


# ---------------------------------------------------------------------------
# A26 — 프라이어 시드 (R9 "신규 기준선 수립" · 승인 1회 경유) — 2026-08-09
# 코어는 gateway/prior_seed.py (fastapi 무의존) — 여기는 라우트만.
#   GET  /prior/proposal?chamber= : 시드 제안 (S4 SYSTEM 카드 — 활성 마커 × 원장 실측 조립)
#   POST /prior/seed              : 승인 → approval_records 기록 + control/ct0/bias_<ch>.json
# "기록 없으면 쓰기 없음"(R9): 승인 행 INSERT 와 파일 쓰기를 한 트랜잭션 단위로 묶고,
# 파일이 써진 뒤에만 commit 한다 — 행만 있고 파일이 없는(또는 반대) 반쪽 상태 방지.
# 적용(mode=active)은 이 라우트 밖 — P2 게이트(Scorecard 프로브 재현) 실증 뒤에만 (CT⓪ §13).
# 파일 포맷은 #152 ct0_bias/control_io.bias_payload 와 동형 — 팀원 A님 모듈 무접촉 (계약 동결).
# ---------------------------------------------------------------------------
from . import prior_seed as _prior_seed  # noqa: E402


@app.get("/ct0/status")
def ct0_status() -> dict:
    """CT⓪ 모델 보정 현황 — S2 「모델 보정」 카드 소스 (2026-08-10).

    왜 필요한가: R2R 진행값(잔차·bias·상태)이 로그와 ct_decisions 에만 있어서 화면에서
    보이지 않았다 — "주소 치고 보는 건 말이 안 된다"(PM). 챔버별 최신 결정 1행 + 모드를 준다.
    chamber 는 ct_id 에 박혀 있다(CT-YYYYMMDD-SIMCH4-nnnnnn-R2R · 테이블에 컬럼 없음) —
    split_part 3번째 토큰으로 복원한다. mode 는 params 단일 소스(ct.model_r2r.mode).
    """
    if _gate["conn"] is None:
        raise HTTPException(status_code=503, detail="DB not wired (DATABASE_URL 필요)")
    mr = (load_params().get("ct") or {}).get("model_r2r") or {}
    cur = _gate["conn"].cursor()
    try:
        cur.execute(
            """SELECT DISTINCT ON (split_part(ct_id, '-', 3))
                      split_part(ct_id, '-', 3) AS ch_tag, ct_id, residual, bias_applied,
                      retrain_status, created_at
                 FROM ct_decisions
                WHERE ct_id LIKE 'CT-%%-R2R'
                ORDER BY split_part(ct_id, '-', 3), id DESC""")
        rows = _rows_as_dicts(cur)
        cur.execute("SELECT count(*) FROM ct_decisions WHERE ct_id LIKE 'CT-%%-R2R'")
        n_total = int(cur.fetchone()[0])
    finally:
        cur.close()
        _gate["conn"].commit()
    for r in rows:                                   # SIMCH4 → SIM_CH_4 (표시 규약 복원)
        tag = str(r.pop("ch_tag", ""))
        r["chamber_id"] = tag.replace("SIMCH", "SIM_CH_")
    return {"mode": str(mr.get("mode", "off")),
            "clamp": float(mr.get("bias_max_rmse_ratio", 1.0)) * 99.84,
            "min_labels": int(mr.get("min_labels_for_auto", 50)),
            "decisions_total": n_total,
            "chambers": rows}


@app.get("/prior/proposal")
def prior_proposal(chamber: str) -> dict:
    """시드 제안 — 자 불일치는 basisMismatch 로 노출한다 (주입 공개 원칙 — 숨김 금지)."""
    try:
        return _prior_seed.build_proposal(chamber)
    except Exception as e:                           # noqa: BLE001 — DB 미가용 등
        raise HTTPException(status_code=503, detail=f"프라이어 제안 조립 불가: {type(e).__name__}")


@app.post("/prior/seed")
def prior_seed_write(body: dict = Body(...)) -> dict:
    """승인 1회 경유 시드 쓰기 (헌법 1-1 HITL — 자동 경로 없음. R9 상한 예외의 근거가 이 승인).

    body: {chamber, approver, approver_role?, reason?, valid_from_pm_count?}
    흐름: 제안 재조립(결정 시점 실측 — stale 카드 값으로 쓰지 않는다) → approval_records
    INSERT(RETURNING id → ct_id) → bias_<chamber>.json 원자 쓰기 → commit.
    파일 쓰기 실패 시 rollback — 기록도 남지 않는다 (쓰기 없으면 기록 없음, 그 역도 같다).
    """
    chamber = str((body or {}).get("chamber") or "").strip()
    approver = str((body or {}).get("approver") or "").strip()
    if not chamber or not approver:
        raise HTTPException(status_code=400, detail="chamber·approver 필수 — 승인자 없는 시드는 없다 (R9)")
    import os as _os
    import psycopg2 as _pg
    try:
        conn = _pg.connect(_os.environ["DATABASE_URL"])  # 요청 전용 커넥션 (_gate 공유 금지 — 스레드 경합)
    except Exception as e:                           # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"DB 연결 불가: {type(e).__name__}")
    try:
        prop = _prior_seed.build_proposal(chamber, conn=conn)
        if prop.get("seedBias") is None:
            raise HTTPException(status_code=409, detail=f"시드 산출 불가: {prop.get('reason')}")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO approval_records
                       (incident_id, request_type, status, selected_option, original_value,
                        decision_reason, approver, approver_role, decided_at)
                   VALUES (%s, 'prior_seed', 'APPROVED', 'seed', %s::jsonb, %s, %s, %s, NOW())
                   RETURNING id""",
                (prop.get("incidentId") or prop["transitionId"],
                 json.dumps(prop, ensure_ascii=False, default=str),
                 (body or {}).get("reason") or prop.get("basisNote"),
                 approver, (body or {}).get("approver_role")))
            approval_id = f"APR-PRS-{cur.fetchone()[0]}"
        payload = _prior_seed.seed_payload(
            chamber, bias=prop["seedBias"], approval_id=approval_id,
            marker_version=prop["marker"], basis=prop["basis"], delta=prop["delta"],
            valid_from_pm_count=int((body or {}).get("valid_from_pm_count") or 0),
            tau_days=prop["template"]["tauDays"], amplitude=prop["template"]["amplitude"],
            ledger_ref=prop.get("sourceRef"), note=prop.get("basisNote"))
        path = _prior_seed.write_seed(payload)
        conn.commit()                                # 파일이 써진 뒤에만 기록 확정
    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception as e:                           # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"시드 쓰기 실패: {type(e).__name__}")
    finally:
        conn.close()
    log.info("🌱 프라이어 시드 승인·쓰기 — %s bias=%s %s (승인 %s)",
             chamber, payload["bias"], approval_id, approver)
    return {"chamber": chamber, "approval_id": approval_id, "bias": payload["bias"],
            "file": str(path), "status": "SEED", "mode": "off", "estimate": True,
            "basisMismatch": prop.get("basisMismatch"), "guard": prop.get("guard")}
