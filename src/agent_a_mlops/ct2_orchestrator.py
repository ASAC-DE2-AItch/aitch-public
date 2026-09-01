# -*- coding: utf-8 -*-
"""CT²(AE) 오케스트레이터 — ChamberRequalified 하류 자동 생성·검증·배포 코어 (배선 설계 v2 §1).

역할: `fdc.agent`에서 ChamberRequalified(§8-C)를 구독해 가드를 평가하고,
  · dry-run(기본, `ct.ct2_auto_generate: false`): 후보 감지 리포트 + `ct_decisions` 기록만
  · auto(true): 학습셋 조립(scripts/ct2_assemble_trainset.py) → retrain 서브프로세스(본선 격리)
    → challenger 번들(미배포) → **자동 검증 게이트**(ae_pipeline.validate_bundle)
    → PASS면 `ct2_auto_promote` 에 따라 자동 promote 또는 파생 thread 승인 오픈 /
      FAIL이면 BLOCKED (보존·미배포, 승인 요청도 열지 않음)

소유·리뷰: 본 파일은 src/agent_a_mlops/ (A 소유) — PM 대행 구현, **A 리뷰 필수** (헌법 3-1).
승인·promote 오픈은 subprocess 경계로만 호출한다 (orchestrator 직접 import 금지 — 소유권
경계 유지. `approval_records`·promote 신호 write 는 전부 PM 소유 모듈에 남는다).

헌법 정합:
  3-3 ② — 생성 자동(조건 ⓐ~ⓔ). `ct2_auto_generate` 기본 false.
  3-3 ③ (2026-07-30) — 배포는 **자동 검증 게이트 전 항목 PASS**가 선행 조건. 게이트 항목·
        임계의 단일 소스는 `ae_pipeline/validate_bundle.py` + `ct.ct2_gate_*` (본 파일 아님).
  1-4 — 파생 thread(<incident_id>-CT2)는 게이트 FAIL 후 **수동 재요청** 및 `auto_promote=false`
        구간의 PASS 회귀용. 동일 Incident pending 체크는 ct2_deploy_approval 쪽 멱등이 담당.
  6-1 — 값은 config/params.yaml `ct.*`. 6-2 — 역직렬화 실패 skip+로그, graceful shutdown.

가드 (설계 D2·D8, 순서대로 — 첫 실패 사유를 ct_decisions에 기록):
  g1 auto_generate off        → DRYRUN (기록 후 종료 — 후보 감지 리포트)
  g2 멱등 (ct_id 기존 존재)    → skip (재수신 — §8-C 멱등 키 incident_id)
  g3 R9 승인 실존              → consent 승계 v1 (Brief `ct2_consent` 필드는 §8-D 확장 후속)
  g4 converged                → limit_corrections.settle_converged AND — False/조회불가 = 미생성
  g5 retrain_cmd·validate_cmd 미등재 → IFACE_WAIT (게이트 없이 배포 금지 — 3-3 ③ ⓑ)

⚠️ retrain·validate 서브프로세스는 인터랙티브 콘솔 계열에서 실행할 것 — Windows WDAC가
   detached 프로세스의 torch DLL을 차단한 실측 사고 있음 (docs/adr/TSR-0001).

CLI (2026-08-05 O1 — CT① 대칭):
    python -m src.agent_a_mlops.ct2_orchestrator            # 상주 루프 (run_stack.ps1 창 1개)
    python -m src.agent_a_mlops.ct2_orchestrator --once     # 1회 처리 후 종료 (리허설·CI 스모크)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPIC_IN = "fdc.agent"
TOPIC_OUT = "fdc.mlops"                                      # O2-6 — CT 결정 관측 채널 (계약 §7)
CONSUMER_GROUP = "consumer-group-ct2-orchestrator"          # 6-4 케밥 표기
EVENT_TYPE = "ChamberRequalified"                            # §8-C — 타 이벤트는 skip (필터 관례)
CT_TYPE = "ct2_ae"
# 번들 폴더명은 `approval_records.selected_option`·`ct_decisions.model_version_after`
# (둘 다 VARCHAR(32), db/init.sql)에 그대로 들어간다. 초과하면 감사 행 INSERT 가
# DataError 로 죽는데, 그 시점은 커밋 이후·신호 드롭 이전이라 반쪽 상태가 된다.
BUNDLE_NAME_MAX = 32
CHAMBER_HASH_LEN = 6                                         # O2-7 — 긴 챔버 ID 축약 길이
VALIDATION_REPORT_NAME = "validation.json"                   # = validate_bundle.VALIDATION_REPORT
#   (프로세스 경계라 import 하지 않는다 — 값 규약만 계약 §8-E 로 고정)
# 게이트 exit code 규약 (validate_bundle 단일 소스와 값 일치 — 프로세스 경계라 import 안 함)
GATE_PASS, GATE_ERROR, GATE_FAIL, GATE_SKIP = 0, 1, 2, 3
ASM_OK, ASM_GATED = 0, 2

# 타임아웃 기본값 — `ct.ct2_*_timeout_sec` 로 override (O2-1, 헌법 6-1 매직넘버 금지).
ASSEMBLE_TIMEOUT_SEC_DEFAULT = 600
RETRAIN_TIMEOUT_SEC_DEFAULT = 7200
VALIDATE_TIMEOUT_SEC_DEFAULT = 1800
APPROVAL_TIMEOUT_SEC = 120
MAX_POLL_MARGIN_SEC = 600                                    # 리밸런스 여유 10분
# handle_event 실패 후 seek 되감기 재시도 간 대기 (H1). DB 다운 등 일시 장애에서 바쁜 루프를
# 막는다. 기본값이며 `ct.ct2_retry_backoff_sec` 로 override (6-1).
RETRY_BACKOFF_SEC_DEFAULT = 30.0
RETRY_BACKOFF_SEC = RETRY_BACKOFF_SEC_DEFAULT
# 정지 경보 점검 주기(초) — 유휴 루프에서만 돈다. 요란 PM 은 수개월 주기라 잦을 필요가 없고,
# 매 poll 마다 DB 를 열면 그 자체가 소음이다.
STALE_CHECK_INTERVAL_SEC = 3600.0

# `--once` 종료 코드 — 크론·CI 래퍼가 "게이트 FAIL"을 성공으로 오독하지 않게 한다 (CT① 대칭).
# BLOCKED(게이트 봉인)는 시스템이 낼 수 있는 가장 큰 신호인데 0을 주면 아무도 못 본다.
EXIT_STATUS = {"FAILED": 1, "BLOCKED": 2, "IFACE_WAIT": 3}

# GC 대상 번들명 패턴 (O2-4) — `ae_ct2_<chamber|h6>_<YYYYmmdd_HHMMSS>`.
# 이 오케스트레이터가 **자기가 만든 것**임을 이름으로 증명하는 폴더만 지운다 (CT① `_gc_deletable`
# 의 '긍정 증거' 원칙 — 손으로 만든 `ae_v1`·`ae_cand_*` 을 조용히 삭제하는 사고 방지).
_BUNDLE_RE = re.compile(r"^ae_ct2_.+_(\d{8})_(\d{6})$")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [ct2-orch] %(message)s")
log = logging.getLogger("ct2_orchestrator")

running = True


def _params() -> dict:
    """config/params.yaml `ct:` 절 로드 (6-1 — 실패 시 안전 기본값: 전부 잠금)."""
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("ct") or {}
    except Exception as e:
        log.warning("params.yaml 로드 실패 → 전 가드 잠금 기본값: %s", e)
        return {}


def _db():
    """DATABASE_URL 접속 (지연 import — dry-run 로직 테스트 시 무의존)."""
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


def ct_id_for(incident_id: str) -> str:
    """ct_decisions.ct_id — CT 프리픽스 승계 + AE 접미 (CT①과 충돌 방지, 6-4)."""
    return f"CT-{incident_id[4:]}-AE" if incident_id.startswith("INC-") else f"CT-{incident_id}-AE"


def record_decision(conn, ct_id: str, status: str, reason: str,
                    qual_id: str | None = None, after: str | None = None) -> bool:
    """ct_decisions 기록 (멱등 — ct_id UNIQUE, 기존 행 존재 시 False).

    3-3② ⓒ '매 실행 기록'의 구현. retrain_status ≤16자 (계약 §8-E 목록이 정본):
    DRYRUN/SKIP/GATED/IFACE_WAIT/FAILED/BLOCKED/RUNNING/SHADOW/AUTO_PROMOTED/PROMOTED/REJECTED.
    ※ 최장값 'AUTO_PROMOTED' = 13자 — VARCHAR(16) 한계 내 (db/init.sql).

    dry-run(auto_generate=false) 경로 전용 — auto 경로는 claim_running/finalize_decision을
    쓴다 (멱등 창을 '이벤트 수신 시점'으로 좁히기 위해 — WP-B1).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ct_decisions WHERE ct_id=%s", (ct_id,))
        existed = cur.fetchone() is not None          # rowcount 대신 명시 조회 — 스모크에서
        cur.execute(                                   # ON CONFLICT rowcount가 신뢰 불가로 관측됨
            """INSERT INTO ct_decisions
                   (ct_id, ct_type, trigger_reason, qual_id, retrain_status, model_version_after)
               VALUES (%s, 'ct2_ae', %s, %s, %s, %s)
               ON CONFLICT (ct_id) DO NOTHING""",
            (ct_id, reason[:64], qual_id, status, after))
    conn.commit()
    return not existed


def claim_running(conn, ct_id: str, qual_id: str | None = None) -> bool:
    """RUNNING 행 원자 선점 (auto 경로 g2 — WP-B1). 새로 만들었으면 True, 이미 있으면 False.

    멱등 창을 **이벤트 수신 시점**으로 좁힌다: 이 행을 심는 순간부터, retrain(최대 2h)이
    도는 동안 같은 이벤트가 재전달돼도 기존 행을 보고 skip 한다. 구 g2(run_generation
    완료 후 SELECT)는 학습 중 재전달에 이중 생성 여지가 있었다.

    ON CONFLICT rowcount가 스모크에서 불신 관측되어(record_decision 주석) 명시 SELECT로
    승패를 판정한다. DB 최종 방어선은 B7 유니크 인덱스(마이그레이션 별도, 미적용).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ct_decisions WHERE ct_id=%s", (ct_id,))
        if cur.fetchone() is not None:
            return False                              # 이미 처리 중/완료 — 재전달 skip
        cur.execute(
            """INSERT INTO ct_decisions
                   (ct_id, ct_type, trigger_reason, qual_id, retrain_status)
               VALUES (%s, 'ct2_ae', 'chamber_requalified', %s, 'RUNNING')
               ON CONFLICT (ct_id) DO NOTHING""",
            (ct_id, qual_id))
    conn.commit()
    return True


def finalize_decision(conn, ct_id: str, status: str, after: str | None = None,
                      reason: str | None = None) -> bool:
    """선점한 RUNNING 행을 종결 상태로 전이 (auto 경로 — WP-B1).

    `model_version_after`·`trigger_reason`은 값이 있을 때만 덮어쓴다(COALESCE). 0행이면
    경고한다 — RUNNING 선점 없이 불렸다는 뜻이라 로직 오류 신호다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE ct_decisions
                  SET retrain_status=%s,
                      model_version_after=COALESCE(%s, model_version_after),
                      trigger_reason=COALESCE(%s, trigger_reason)
                WHERE ct_id=%s""",
            (status, after, reason[:64] if reason else None, ct_id))
        hit = cur.rowcount > 0
    conn.commit()
    if not hit:
        log.warning("finalize 0행 (ct_id=%s, status=%s) — RUNNING 선점이 선행되지 않음", ct_id, status)
    return hit


def guard_r9_approved(conn, incident_id: str) -> bool:
    """g3 — R9(requalify) 승인 실존 = CT² 생성 동의 승계 (v1, 설계 D2)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM approval_records
                       WHERE incident_id=%s AND request_type='requalify'
                         AND status IN ('Approved','APPROVED') LIMIT 1""", (incident_id,))
        return cur.fetchone() is not None


def guard_converged(conn, incident_id: str) -> bool | None:
    """g4 — limit_corrections.settle_converged 패키지 AND (§8-D). 조회 불가 = None(미생성)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT bool_and(settle_converged) FROM limit_corrections
                           WHERE incident_id=%s AND settle_converged IS NOT NULL""",
                        (incident_id,))
            row = cur.fetchone()
            return None if row is None or row[0] is None else bool(row[0])
    except Exception as e:                                   # 컬럼/스키마 상이 → fail-safe
        log.warning("converged 조회 실패 (fail-safe 미생성): %s", e)
        return None


def _render_cmd(tpl: str, **kw) -> list[str]:
    """config 명령 템플릿 → argv. `python` 선두 토큰은 현재 인터프리터로 치환.

    이유: 오케스트레이터가 venv 안에서 돌 때 PATH 의 `python` 이 다른 해석기일 수 있다
    (torch 미설치 → 학습이 조용히 실패). sys.executable 로 고정한다.

    **빈 값 placeholder 는 토큰째 제거한다** (G0-2, 2026-08-05): `--labels {labels}` 에
    `labels=""` 를 치환하면 빈 문자열이 그대로 인자로 들어가 자식이 "파일 없음"으로 죽는다
    — 게이트 **판정(exit 2)이 아니라 실행 오류(exit 1)** 가 되어 리포트조차 남지 않는다.
    그래서 치환이 아니라 **조립**한다: 값이 비면 그 토큰과 바로 앞 `--flag` 를 함께 뺀다.
    (라벨이 빠진 채 검증이 돌면 게이트 E0 가 FAIL 로 잡는다 — fail-open 이 아니다.)
    """
    argv: list[str] = []
    for tok in tpl.split():
        keys = re.findall(r"\{(\w+)\}", tok)
        missing = [k for k in keys if k not in kw]
        if missing:
            # ★"없는 키"와 "빈 값"을 구분한다. config 에 새 placeholder 를 넣고 호출부를
            # 안 고치면, 조용히 그 인자 없이 실행되는 대신 **여기서 실패**해야 한다 —
            # 게이트를 인자 하나 빠진 채로 돌리는 것이 가장 나쁜 결과다.
            raise KeyError(f"명령 템플릿 placeholder 미제공: {missing} (템플릿={tpl!r})")
        if keys and all(not str(kw[k] or "") for k in keys):
            if argv and argv[-1].startswith("--"):
                argv.pop()                                   # 짝이 되는 플래그도 제거
            continue
        argv.append(tok.format(**kw))
    if argv and argv[0] in ("python", "python3", "py"):
        argv[0] = sys.executable
    return argv


def _run(argv, cwd, timeout, label) -> subprocess.CompletedProcess:
    """subprocess 실행 + 표준 로깅 (O2-2 — CT① `_run` 과 같은 규약).

    `encoding="utf-8"` 을 못 박는다 — 자식들이 한글과 `⚠`/`★`/`│` 를 찍는데, Windows 에서
    `text=True` 만 주면 파이프가 cp949 로 디코딩돼 **실패 사유가 UnicodeDecodeError 로
    뭉개진다** (헌법 7장 인코딩 계열). 자식 쪽도 `PYTHONIOENCODING` 으로 고정한다.

    실행 argv 를 항상 남긴다 — "무엇을 돌렸는지"가 로그에 없으면 라벨 주입·경로 치환 같은
    조립 오류를 사후에 재구성할 수 없다. 실패 시 **stdout 도 함께** 남긴다(구 구현은 stderr
    뒤 300자만 사유에 넣고 stdout 을 버려, 자식이 stdout 으로 낸 진단이 통째로 사라졌다).
    """
    log.info("  $ %s", " ".join(str(x) for x in argv))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=cwd, env=env)
    if r.returncode != 0:
        log.warning("  %s rc=%s\n--- stderr ---\n%s\n--- stdout ---\n%s",
                    label, r.returncode, (r.stderr or "")[-1500:], (r.stdout or "")[-1500:])
    return r


def _fail_detail(r, label: str) -> str:
    """subprocess 실패 사유 한 줄 — stderr 우선, 없으면 stdout (둘 다 비면 그 사실을 쓴다)."""
    tail = (r.stderr or "").strip() or (r.stdout or "").strip()
    return f"{label} rc={r.returncode}: {tail[-300:] if tail else '(출력 없음 — 로그 참조)'}"


def _timeouts(p: dict) -> tuple[float, float, float]:
    """(조립, 재학습, 검증) 타임아웃 — params 경유 (O2-1). 수치 아니면 기본값+경고."""
    out = []
    for key, default in (("ct2_assemble_timeout_sec", ASSEMBLE_TIMEOUT_SEC_DEFAULT),
                         ("ct2_retrain_timeout_sec", RETRAIN_TIMEOUT_SEC_DEFAULT),
                         ("ct2_validate_timeout_sec", VALIDATE_TIMEOUT_SEC_DEFAULT)):
        try:
            v = float(p.get(key, default))
            if v <= 0:
                raise ValueError(f"{key} <= 0")
        except (TypeError, ValueError) as e:
            log.warning("%s 값 이상(%s) → 기본 %ss", key, e, default)
            v = float(default)
        out.append(v)
    return out[0], out[1], out[2]


def max_poll_interval_ms(p: dict) -> int:
    """`max.poll.interval.ms` — **타임아웃 합계에서 파생**한다 (O2-1).

    상수로 박아두면 `ct2_retrain_timeout_sec` 를 올렸을 때 학습이 폴 간격을 넘겨 그룹
    리밸런스가 돌고, 그 결과 처리 중인 이벤트가 다른 컨슈머로 재할당된다. 산식의 입력이
    바뀌면 결과도 함께 움직여야 한다.
    """
    asm, retrain, validate = _timeouts(p)
    return int((asm + retrain + validate + APPROVAL_TIMEOUT_SEC + MAX_POLL_MARGIN_SEC) * 1000)


def bundle_name_for(chamber: str, now: datetime | None = None) -> str:
    """challenger 번들 폴더명 — `ae_ct2_<chamber>_<YYYYmmdd_HHMMSS>` (32자 보장).

    두 가지를 고친다 (O2-7·O2-8, 2026-08-05):
      · **시각축 UTC 통일** — 구 구현은 `datetime.now()`(로컬)라 폴더명과 감사 시각
        (`ct_decisions.created_at`, UTC)이 어긋났다. 계약 §8-B-1 시각 규율에 맞춘다.
      · **긴 챔버 ID 축약** — 고정부가 22자라 챔버명이 10자를 넘으면 `BUNDLE_NAME_MAX`(32)
        위반으로 **전 건 FAILED** 가 된다. 무인 상태에서는 조용한 전면 정지다. 넘치면
        `sha1` 앞 6자로 축약하고, 원 챔버명은 `ct_decisions.trigger_reason`·로그에 남는다
        (이름은 감사 컬럼 폭에 맞추고, 식별은 감사 행이 한다).
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    name = f"ae_ct2_{chamber}_{stamp}"
    if len(name) <= BUNDLE_NAME_MAX:
        return name
    import hashlib
    h = hashlib.sha1(str(chamber).encode("utf-8")).hexdigest()[:CHAMBER_HASH_LEN]
    short = f"ae_ct2_{h}_{stamp}"
    log.warning("챔버 ID '%s' 가 길어 번들명 축약: %s → %s (32자 상한. 원 챔버는 감사 행에 남는다)",
                chamber, name, short)
    return short


# 내부 상태 → (DB 기록 상태, trigger_reason 접미). 계약 §8-E 의 `retrain_status` 값 목록을
# 늘리지 않으면서 "게이트 FAIL"과 "게이트 SKIP(판정 불가)"을 감사에서 구분하기 위한 매핑.
INTERNAL_STATUS = {"BLOCKED_SKIP": ("BLOCKED", "gate_skip")}


def _labels_path(p: dict) -> str:
    """오염 정답지 절대경로 (G0-2). 미등재/부재면 빈 문자열 → `--labels` 토큰째 제거.

    부재를 여기서 **실패로 만들지 않는다**: 라벨 없이 만들어진 challenger 는 게이트 E0 가
    FAIL 로 잡아 배포를 막는다. 실패 지점을 게이트 한 곳으로 모으는 편이, 같은 규칙이
    수동 경로에도 그대로 걸리므로 방어가 얇아지지 않는다.
    """
    raw = str(p.get("ct2_labels_path") or "").strip()
    if not raw:
        log.warning("ct.ct2_labels_path 미등재 — 라벨 없이 학습·검증한다. "
                    "게이트 E0(라벨 제공)가 FAIL 하므로 배포되지 않는다 (G0-1).")
        return ""
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        log.warning("정답지 파일 없음: %s — `--labels` 없이 진행하며 게이트 E0 가 FAIL 한다", path)
        return ""
    return str(path)


def publish_mlops(payload: dict) -> None:
    """`fdc.mlops` 발행 (O2-6 · 계약 §7). 실패해도 흐름 계속 — 감사 정본은 `ct_decisions` 다.

    CT① 이 이미 쓰는 채널이라 CT② 결정만 관측 불가였다(대시보드·감사가 DB 직접 조회에만
    의존). NaN/Inf 는 표준 JSON 이 아니므로 `allow_nan=False` 로 발행 길목에서 막는다 (7장).
    """
    try:
        from confluent_kafka import Producer
        pr = Producer({"bootstrap.servers":
                       os.environ.get("KAFKA_BOOTSTRAP_SERVERS",
                                      os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092"))})
        pr.produce(TOPIC_OUT, json.dumps(payload, ensure_ascii=False,
                                         allow_nan=False).encode("utf-8"))
        pr.flush(5)
        log.info("fdc.mlops 발행: %s %s", payload.get("ct_id"), payload.get("retrain_status"))
    except Exception as e:                                   # noqa: BLE001
        log.warning("fdc.mlops 발행 실패(흐름 계속): %s", e)


def run_generation(evt: dict, p: dict) -> tuple[str, str, str | None]:
    """auto 경로 — 조립 → retrain → **검증 게이트** → (자동 promote | 승인 오픈).

    Returns:
        (status, detail, bundle_name) — bundle_name 은 번들이 만들어진 뒤의 단계에서만
        채워진다. handle_event가 RUNNING 행을 선점해 두므로 자동 promote 경로의
        mark_ct_decision UPDATE는 그 행을 맞춘다(1행). 호출자는 이 값을
        `finalize_decision(after=...)`로 재확인해 `model_version_after`를 확정한다.

    각 단계는 subprocess (본선인 이 컨슈머 루프를 학습이 막지 않도록 — 여기서는
    순차 대기하되, 이 프로세스 자체가 판정 본선이 아니므로 M8 무관. A 리뷰 포인트)

    게이트 (헌법 3-3 ③ ⓑ · 2026-07-30): validate_bundle 이 **유일한 문지기**다.
      FAIL(rc=2) → BLOCKED. challenger 보존·미배포, 승인 요청도 열지 않는다
                   (게이트 FAIL 상태의 파생 thread 자동 오픈 금지 — 1-4 역할 조정).
                   재요청 발의는 사람.
      SKIP(rc=3) → BLOCKED (2026-08-05 G0-4). **판정 불가는 PASS 가 아니다** — 예: E1 이
                   학습분 제외 후 표본 하한 미달로 채점을 못 한 경우. 효과는 FAIL 과 같고
                   구분은 `trigger_reason` 접미(`/gate_skip`)가 담당한다.
      PASS(rc=0) → `ct2_auto_promote` 에 따라 자동 promote(AUTO_PROMOTED) 또는
                   파생 thread 승인 오픈(SHADOW — 차단기 모드).
      실행오류(rc=1) → FAILED (판정 불가를 PASS 로 오해하지 않는다).
    """
    inc, ch = evt["incident_id"], evt["chamber_id"]
    until = evt.get("requalified_at") or datetime.now(timezone.utc).isoformat()
    hours = float(p.get("ct2_backfill_hours", 24))           # D10 근사 창 — 실측 후 조정
    since = (datetime.fromisoformat(until.replace("Z", "+00:00"))
             - timedelta(hours=hours)).isoformat()
    asm_timeout, retrain_timeout, validate_timeout = _timeouts(p)
    # 번들명은 **가장 먼저** 정한다 — 설정 문제이므로 되감기·학습을 태우기 전에 해결해야
    # 하고, "GATED"(표본 대기)로 잘못 보고되면 원인 오진을 유발한다. 긴 챔버 ID 는 실패가
    # 아니라 해시 축약으로 흡수한다 (O2-7 — 무인 상태의 전면 정지 방지).
    bundle_name = bundle_name_for(ch)

    # 설정 미비(조립·retrain·validate 명령)는 **되감기·학습 전에** 실패시킨다 (WP-B3 — 구현이
    # 2단계로 흩어져 있던 g5를 한 곳으로 호이스트). 게이트 없이는 배포 불가이므로 게이트
    # 명령이 없으면 학습 자체를 시작하지 않는다.
    asm_tpl = p.get("ct2_assemble_cmd")
    retrain_tpl = p.get("ct2_retrain_cmd")
    validate_tpl = p.get("ct2_validate_cmd")
    missing = [k for k, v in (("ct2_assemble_cmd", asm_tpl),
                              ("ct2_retrain_cmd", retrain_tpl),
                              ("ct2_validate_cmd", validate_tpl)) if not v]
    if missing:
        return ("IFACE_WAIT",
                f"{', '.join(missing)} 미등재 — 게이트 없이 배포 금지 (3-3 ③ ⓑ)", None)

    out_dir = REPO_ROOT / "Data" / "ct2"
    limits_json = out_dir / f"firm_limits_{ch}.json"          # B firm 한계 덤프 (배선 확정 항목)
    if not limits_json.exists():
        return "GATED", f"firm limits 파일 없음: {limits_json.name} (소급 필터 입력 — 스모크 절차 참조)", None

    # ── ① 조립 (O2-3 — 구 argv 하드코딩을 `ct2_assemble_cmd` 로) ───────────────
    r = _run(_render_cmd(asm_tpl, bootstrap=os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092"),
                         chamber=ch, since=since, until=until,
                         limits=str(limits_json), out=str(out_dir)),
             REPO_ROOT, asm_timeout, "조립")
    if r.returncode == ASM_GATED:
        return "GATED", "표본 게이트 미달 — 전방 연장 적재 대기 (만료 = 다음 요란 PM, D7)", None
    if r.returncode != ASM_OK:
        return "FAILED", _fail_detail(r, "조립"), None
    produced = sorted(out_dir.glob(f"ct2_trainset_{ch}_*.csv"))
    if not produced:                                          # rc=0 인데 산출물 없음
        return "FAILED", (f"조립 rc=0 이지만 산출 CSV 없음 (glob ct2_trainset_{ch}_*.csv). "
                          "조립 스크립트 출력 경로를 확인하세요"), None
    trainset = produced[-1]

    # ── ② 재학습 (challenger — 미배포) ────────────────────────────────────────
    # `labels` 는 오염 정답지(G0-2). **빈 값이면 `--labels` 토큰째 빠지고**, 그 상태의
    # challenger 는 게이트 E0 에서 FAIL 한다 — 라벨 없는 자동 학습이 배포로 이어지지 않는다.
    cwd = REPO_ROOT / p.get("ct2_cmd_cwd", "src/agent_a_mlops/Autoencoder")
    labels = _labels_path(p)
    bundle_out = REPO_ROOT / "models" / "anomaly_ae" / bundle_name
    report = bundle_out / VALIDATION_REPORT_NAME
    r2 = _run(_render_cmd(retrain_tpl, data=trainset, out=bundle_out, bundle=bundle_out,
                          labels=labels),
              cwd, retrain_timeout, "재학습")                            # TSR-0001
    if r2.returncode != 0:
        return "FAILED", _fail_detail(r2, "retrain"), None

    # ── ③ 자동 검증 게이트 (WP-2, 명령 존재는 함수 첫머리 WP-B3에서 확인 완료) ──────
    # `out` 은 **파일** 경로다 (validate_bundle 이 write_text 한다) — 폴더를 넘기면
    # 템플릿에 {out} 이 등장하는 순간 IsADirectoryError 로 전 건이 FAILED 가 된다.
    r3 = _run(_render_cmd(validate_tpl, bundle=bundle_out, data=trainset, out=report,
                          labels=labels),
              cwd, validate_timeout, "게이트")
    if r3.returncode in (GATE_FAIL, GATE_SKIP):
        skip = r3.returncode == GATE_SKIP
        kind = "SKIP(판정 불가)" if skip else "FAIL"
        log.error("게이트 %s — 배포 봉인. incident=%s bundle=%s report=%s "
                  "(알림 채널은 WP-C — 현재는 로그 + ct_decisions(BLOCKED))",
                  kind, inc, bundle_name, report)
        # SKIP 도 BLOCKED 로 **기록**한다 — 효과가 동일(보존·미배포·사람 발의)하고, 구분은
        # `trigger_reason` 접미(`/gate_skip`)가 담당한다. 계약 §8-E 값 목록을 늘리지 않는다.
        return ("BLOCKED_SKIP" if skip else "BLOCKED",
                f"게이트 {kind} — challenger 보존·미배포 ({bundle_name}). "
                f"리포트={report}. 재요청 발의는 엔지니어(1-4 역할 조정)", bundle_name)
    if r3.returncode != GATE_PASS:
        return "FAILED", _fail_detail(r3, "게이트 실행 오류"), bundle_name

    # ── ④ PASS 경로 — 자동 promote 또는 승인 회귀 (헌법 3-3 ③) ────────────────
    auto = bool(p.get("ct2_auto_promote", False))
    opener = [sys.executable, "-m", "src.orchestrator.ct2_deploy_approval", inc,
              "--bundle", str(bundle_out), "--report", str(report), "--chamber", ch]
    if auto:
        opener.append("--auto-promote")
    r4 = _run(opener, REPO_ROOT, APPROVAL_TIMEOUT_SEC,
              "자동 promote" if auto else "승인 오픈")
    if r4.returncode != 0:
        verb = "자동 promote" if auto else "승인 오픈"
        return "FAILED", _fail_detail(r4, verb), bundle_name
    if auto:
        return "AUTO_PROMOTED", (f"게이트 PASS → 자동 배포 {bundle_name} "
                                 f"(리로드 신호 드롭 · 리포트={report})"), bundle_name
    return "SHADOW", (f"게이트 PASS → 배포 승인 PENDING (파생 thread, "
                      f"ct2_auto_promote=false 회귀) challenger={bundle_name}"), bundle_name


def handle_event(evt: dict, p: dict) -> str:
    """ChamberRequalified 1건 처리 — 가드 순서 g1~g5 (docstring 상단).

    Returns:
        종결 상태 문자열 (`--once` 종료 코드·테스트용). 이벤트를 처리하지 않은 경로는
        `NOOP`/`SKIPPED` 로 구분한다 — "아무 일도 없었음"과 "실패"를 종료 코드에서
        섞지 않기 위함이다.
    """
    inc = evt.get("incident_id")
    if not inc:
        log.warning("incident_id 없는 이벤트 skip")
        return "NOOP"
    cid = ct_id_for(inc)
    conn = _db()
    try:
        if not p.get("ct2_auto_generate", False):            # g1 — dry-run
            new = record_decision(conn, cid, "DRYRUN", "requalified_candidate",
                                  evt.get("qual_id"))
            log.info("[dry-run] CT² 후보 감지 %s (chamber=%s qual=%s) — 기록=%s",
                     inc, evt.get("chamber_id"), evt.get("qual_id"), new)
            return "DRYRUN"
        if not claim_running(conn, cid, evt.get("qual_id")):     # g2 — RUNNING 원자 선점
            log.info("멱등 skip %s (이미 처리 중/완료)", cid)
            return "SKIPPED"
        # ── 선점 이후: 어떤 실패든 RUNNING을 종결 상태로 남긴다 (claim 불변식 = 항상 terminal).
        #    이 블록이 통째로 예외를 낼 경우(g3/g4/finalize의 DB 오류 등), 아래 except가
        #    FAILED로 종결을 시도하고 재-raise한다 → main이 커밋 보류(재처리) → 재처리 시
        #    g2가 이 행(FAILED)을 보고 skip → 커밋. '기록 없이 사라짐'이 구조적으로 불가능.
        try:
            if not guard_r9_approved(conn, inc):             # g3
                finalize_decision(conn, cid, "SKIP",
                                  reason="chamber_requalified/r9_consent_missing")
                return "SKIP"
            conv = guard_converged(conn, inc)                # g4
            if conv is not True and p.get("ct2_require_converged", True):
                suffix = "converged_false" if conv is False else "converged_unknown"
                finalize_decision(conn, cid, "SKIP", reason=f"chamber_requalified/{suffix}")
                log.info("미수렴/조회불가 → 생성 금지·에스컬레이션 (3-3② ⓐ): %s", inc)
                return "SKIP"
            try:
                status, detail, bundle_name = run_generation(evt, p)     # g5 포함
            except Exception as e:                                       # noqa: BLE001
                log.exception("CT² 생성 중 예외 — FAILED 로 기록: %s", inc)
                status, detail, bundle_name = "FAILED", f"예외: {e}", None
            # auto promote 경로는 이 UPDATE 전에 ct2_deploy_approval의 mark_ct_decision이
            # 같은 RUNNING 행을 AUTO_PROMOTED+번들명으로 이미 전이한다. 여기 finalize는
            # 동일 값 재확인(COALESCE로 번들명 보존) + trigger_reason 분류자 병기(B9).
            status, forced = INTERNAL_STATUS.get(status, (status, None))
            suffix = ("" if status in ("AUTO_PROMOTED", "SHADOW")
                      else f"/{forced or status.lower()}")
            finalize_decision(conn, cid, status, after=bundle_name,
                              reason=f"chamber_requalified{suffix}")
            log.info("CT² %s → %s: %s", inc, status, detail)
            # O2-6 — 결정을 이벤트로도 흘린다 (대시보드·감사가 DB 직접 조회에만 의존하던 상태 해소)
            publish_mlops({
                "event_type": "CtDecision", "ct_id": cid, "ct_type": CT_TYPE,
                "incident_id": inc, "chamber_id": evt.get("chamber_id"),
                "qual_id": evt.get("qual_id"),
                "trigger_reason": f"chamber_requalified{suffix}",
                "retrain_status": status,
                # `model_version_after` = **지금 서빙 중인 번들**이다. BLOCKED·SHADOW 는
                # 배포되지 않았으므로 여기 이름을 넣으면 "무엇이 배포됐나"를 못 묻는다.
                "model_version_after": bundle_name if status in ("AUTO_PROMOTED", "PROMOTED")
                                        else None,
                "challenger": bundle_name,                   # 미배포 후보도 추적 가능하게
                "detail": detail[:500],
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            # ★청소는 **감사 밖**에서 돈다. 여기서 예외가 새면 아래 except 가 방금 쓴
            #   AUTO_PROMOTED 행을 FAILED 로 덮어쓴다 — 신호는 드롭됐고 approval_records·
            #   CHANGELOG 는 "배포됨"인데 ct_decisions 만 FAILED 가 되는 **3중 기록 붕괴**다
            #   (헌법 1-1 예외 3 ⓒ). 청소 실패는 다음 회차가 흡수하면 되는 종류의 실패다.
            try:
                _gc_unpromoted(p, conn)                      # O2-4
            except Exception as e:                           # noqa: BLE001
                log.warning("GC 실패(다음 회차 재시도) — 배포 기록에는 영향 없음: %s", e)
            return status
        except Exception:                                    # g3/g4/finalize 자체 실패
            log.exception("post-claim 처리 실패 — FAILED 종결 시도 후 재처리: %s", inc)
            try:
                finalize_decision(conn, cid, "FAILED", reason="chamber_requalified/handler_error")
            except Exception:                                # noqa: BLE001 — DB 다운 등
                log.error("FAILED 종결마저 실패 — RUNNING 잔류 가능 (DB 확인 필요): %s", cid)
            raise                                            # main 커밋 보류 → 재처리
    finally:
        conn.close()


# ── 보존(GC) · 정지 경보 (O2-4 · O2-5) ────────────────────────────────────
def _promoted_bundles(conn) -> set[str] | None:
    """감사 행이 '배포됐다'고 말하는 번들명 집합. 조회 불가면 None(= GC 생략).

    `model_version_before` 도 함께 모은다 — 롤백 행에서는 **되돌린 대상**(= 직전까지 실제로
    서빙되던 번들)이 그 컬럼으로 옮겨간다(M4-1). after 만 보면 배포됐다가 롤백된 번들이
    보호 대상에서 빠져 GC 가 지운다 — 사후 원인 규명 자료가 사라지는 종류의 손실이다.
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT model_version_after, model_version_before FROM ct_decisions
                    WHERE ct_type=%s AND retrain_status IN ('PROMOTED','AUTO_PROMOTED','ROLLED_BACK')
                """, (CT_TYPE,))
            return {v for row in cur.fetchall() for v in row if v}
    except Exception as e:                                   # noqa: BLE001
        log.warning("GC: ct_decisions 대조 실패 → 이번 회차 GC 생략(보수적): %s", e)
        return None


def _gc_unpromoted(p: dict, conn=None) -> int:
    """미승격 challenger·조립 산출물 정리 (O2-4 / 설계 v2 R12 의 자동화). 삭제 건수 반환.

    **삭제는 긍정 증거로만 한다** (CT① `_gc_deletable` 선례): 이 오케스트레이터가 만든
    이름 규칙(`ae_ct2_<...>_<stamp>`)에 맞고, 감사 행이 배포 이력을 갖지 않으며, 보존
    기간이 지난 것만 지운다. `ae_v1`·`ae_cand_*` 같은 손으로 만든 번들은 이름 규칙 밖이라
    애초에 후보가 되지 않는다 — 되돌릴 수 없는 삭제 사고를 구조적으로 막는 배치다.

    조립 산출물은 R12 규칙을 따른다: `meta.json` 의 `gate_pass=false`(표본 미달 = 연장 적재
    대기분)만 만료 삭제하고, 학습에 쓰인 산출물은 재현·감사용으로 보존한다.
    """
    days = float(p.get("ct2_gc_keep_unpromoted_days", 14))
    promoted = _promoted_bundles(conn)
    if promoted is None:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0

    store = REPO_ROOT / "models" / "anomaly_ae"
    if store.exists():
        for d in sorted(store.iterdir()):
            m = _BUNDLE_RE.match(d.name) if d.is_dir() else None
            if not m or d.name in promoted:
                continue
            try:
                made = datetime.strptime(f"{m.group(1)}_{m.group(2)}",
                                         "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if made >= cutoff:
                continue
            log.info("GC: 미승격 challenger 삭제 (%s · 경과 %.0f일 > %.0f)",
                     d.name, (datetime.now(timezone.utc) - made).days, days)
            try:
                shutil.rmtree(d)                             # ignore_errors 금지 — 실패를 드러낸다
                n += 1
            except OSError as e:
                log.error("GC 삭제 실패(다음 회차 재시도): %s — %s", d.name, e)

    work = REPO_ROOT / "Data" / "ct2"
    if work.exists():
        for meta in sorted(work.glob("ct2_trainset_*.meta.json")):
            try:
                if datetime.fromtimestamp(meta.stat().st_mtime, timezone.utc) >= cutoff:
                    continue
                d = json.loads(meta.read_text(encoding="utf-8"))
                if not isinstance(d, dict) or d.get("gate_pass"):
                    continue                                 # 학습에 쓰인 산출물은 보존
                csv = meta.with_name(meta.name.replace(".meta.json", ".csv"))
                for q in (meta, csv):
                    q.unlink(missing_ok=True)
                log.info("GC: 표본 미달 조립 산출물 삭제 (%s — R12 만료)", meta.name)
                n += 1
            except (OSError, ValueError) as e:
                # `json.loads` 는 `JSONDecodeError`(=ValueError)·`UnicodeDecodeError` 를 낸다
                # — `OSError` 만 잡으면 파손된 meta 하나가 루프 밖으로 새어나간다.
                log.error("GC 조립 산출물 처리 실패(건너뜀): %s — %s", meta.name, e)
    return n


def _warn_stale_event(p: dict, conn=None) -> bool:
    """정지 의심 경보 (O2-5) — 마지막 CT² 결정이 오래됐으면 에스컬레이션 로그. 발화 여부 반환.

    요란 PM 은 수개월 주기라 "한동안 조용한 것"이 정상이다. 그래서 **긴 임계**를 쓰되,
    임계를 넘으면 알린다 — 트리거가 안 오거나 계속 GATED 로 끝나는 조용한 기능 정지를
    구분할 신호가 지금은 하나도 없다. 알림 채널(WP-C) 부재라 이 로그가 에스컬레이션의
    실체다 (헌법 7장 "수동 경로는 기록도 우회된다"와 같은 취지).

    ★**호출 지점은 유휴 루프**다 (`main` 의 `msg is None` 분기). `handle_event` 안에서
    부르면 방금 `claim_running` 이 심은 행 때문에 `MAX(created_at)` 이 항상 '지금'이라
    **구조적으로 절대 발화하지 않는다** — 경보가 있는 척하는 상태가 된다.
    """
    if conn is None:
        return False
    days = float(p.get("ct2_stale_event_days", 120))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(created_at) FROM ct_decisions WHERE ct_type=%s", (CT_TYPE,))
            row = cur.fetchone()
    except Exception as e:                                   # noqa: BLE001
        log.warning("정지 경보 조회 실패(생략): %s", e)
        return False
    last = row[0] if row else None
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds() / 86400
    if age > days:
        log.error("★에스컬레이션: 마지막 CT² 결정이 %.0f일 전(> %.0f) — 트리거가 오지 않거나 "
                  "계속 미착수 상태일 수 있다. ChamberRequalified 발행·구독 배선을 점검하세요.",
                  age, days)
        return True
    return False


def _shutdown(signum, frame):
    """SIGINT/SIGTERM graceful (6-2)."""
    global running
    running = False
    log.info("종료 신호 %s", signum)


def main(argv=None) -> int:
    """구독 루프 — ChamberRequalified 외 이벤트는 event_type 필터로 skip (§8-B 관례).

    `--once` (2026-08-05 O1 — CT① 대칭): 우리 이벤트 **1건**을 처리하고 종료한다. 데몬을
    띄우지 않고 리허설·CI 스모크에서 배선을 1회 검증할 수단이 없던 것을 메운다. 종료 코드는
    `EXIT_STATUS` — 게이트 FAIL(BLOCKED)을 0으로 돌려주면 래퍼가 성공으로 오독한다.

    커밋 전략 (WP-B1 — 헌법 7장 '장시간 subprocess consumer'): **수동 커밋**이다.
      · `enable.auto.commit=False` — retrain(최대 2h) 중 크래시해도 offset이 앞서가지 않는다.
      · 처리 성공(handle_event 정상 반환) 후에만 커밋. handle_event는 RUNNING 선점 + 종결
        기록을 내부에서 하므로 '커밋만 나가고 기록 없음'이 구조적으로 불가능하다.
      · 우리 이벤트가 아니거나 파싱 불능(poison)이면 커밋해 offset을 전진 (재처리 무의미).
      · handle_event가 예외(DB 장애 등 일시적)면 **커밋 안 함** → 재기동 시 재처리 (그때
        RUNNING 선점이 이미 있으면 g2 skip → 무한 재시도 방지).
      · `max.poll.interval.ms`를 처리 상한 이상으로 확대 — 블로킹 중 그룹 리밸런스 소음 방지.
    """
    ap = argparse.ArgumentParser(prog="ct2_orchestrator",
                                 description="CT²(AE) 재학습 오케스트레이터")
    ap.add_argument("--once", action="store_true",
                    help="ChamberRequalified 1건 처리 후 종료 (리허설·CI 스모크)")
    ap.add_argument("--timeout-sec", type=float, default=None,
                    help="--once 에서 이벤트를 기다릴 상한(초). 초과하면 NOOP(0) 로 종료")
    a = ap.parse_args(argv)

    from confluent_kafka import Consumer
    global RETRY_BACKOFF_SEC
    p = _params()
    try:                                                 # 6-1 — 매직넘버는 params 경유
        RETRY_BACKOFF_SEC = float(p.get("ct2_retry_backoff_sec", RETRY_BACKOFF_SEC_DEFAULT))
    except (TypeError, ValueError):
        log.warning("ct2_retry_backoff_sec 수치 아님 → 기본 %ss", RETRY_BACKOFF_SEC_DEFAULT)
        RETRY_BACKOFF_SEC = RETRY_BACKOFF_SEC_DEFAULT
    poll_ms = max_poll_interval_ms(p)                    # O2-1 — 타임아웃 합계에서 파생
    c = Consumer({"bootstrap.servers": os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092"),
                  "group.id": CONSUMER_GROUP, "auto.offset.reset": "latest",
                  "enable.auto.commit": False,
                  "max.poll.interval.ms": poll_ms})
    c.subscribe([TOPIC_IN])
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    log.info("구독 시작: %s (auto_generate=%s → %s 모드, 수동 커밋, max.poll=%.0f분%s)", TOPIC_IN,
             p.get("ct2_auto_generate", False),
             "auto" if p.get("ct2_auto_generate", False) else "dry-run",
             poll_ms / 60000, " · --once" if a.once else "")
    status = "NOOP"
    deadline = (time.monotonic() + a.timeout_sec) if (a.once and a.timeout_sec) else None
    next_stale_check = time.monotonic() + STALE_CHECK_INTERVAL_SEC
    try:
        while running:
            if deadline is not None and time.monotonic() > deadline:
                log.info("--once 대기 상한 초과 — 처리할 이벤트 없이 종료")
                break
            msg = c.poll(1.0)
            if msg is None:
                # 유휴 구간에서만 정지 경보를 본다 — 이벤트 처리 직후에는 방금 심은 행
                # 때문에 '마지막 결정 = 지금'이라 절대 발화하지 않는다 (O2-5 docstring).
                if not a.once and time.monotonic() >= next_stale_check:
                    next_stale_check = time.monotonic() + STALE_CHECK_INTERVAL_SEC
                    conn = None
                    try:
                        conn = _db()
                        _warn_stale_event(p, conn)
                    except Exception as e:                   # noqa: BLE001 — 루프는 죽지 않는다
                        log.debug("정지 경보 점검 실패(무시): %s", e)
                    finally:
                        if conn is not None:
                            conn.close()
                continue
            if msg.error():
                log.error("Kafka 에러: %s", msg.error())
                continue
            try:
                evt = json.loads(msg.value().decode("utf-8"))
                if not isinstance(evt, dict):                # §7 — json.loads 성공 ≠ dict
                    raise ValueError(f"이벤트가 객체 아님: {type(evt).__name__}")
            except Exception as e:                           # 6-2 — poison: 로그 + offset 전진
                log.warning("역직렬화 실패 skip(커밋): %s", e)
                c.commit(msg, asynchronous=False)
                continue
            if evt.get("event_type") != EVENT_TYPE:          # 우리 이벤트 아님 → 전진
                c.commit(msg, asynchronous=False)
                continue
            try:
                status = handle_event(evt, p)
                c.commit(msg, asynchronous=False)            # 처리 성공 후에만 커밋
                if a.once:
                    log.info("--once 완료 → %s", status)
                    break
            except Exception as e:                           # 일시 장애 → seek 되감기 후 재시도
                # ★커밋 보류만으로는 부족하다: confluent-kafka는 poll 직후 **인메모리
                # position이 전진**하므로 seek 없이는 같은 메시지가 다시 오지 않는다. 게다가
                # `fdc.agent`에는 타 event_type도 흐르므로, 실패한 offset N 직후의 다른
                # 이벤트(N+1)가 위에서 정상 커밋되며 **N을 지나쳐** 영구 유실된다
                # (pre-claim 실패면 ct_decisions 행조차 없어 무기록 유실 — 헌법 7장 자기 위반).
                # 그래서 실패 offset으로 되감고 backoff 후 재시도한다.
                # 무한 재시도 방지: g2(claim_running) 선점 행이 남아 있으면 재처리가 skip으로
                # 끝나고 그 경로는 정상 커밋된다. pre-claim 실패(DB 다운)는 복구까지 되감기가
                # 유지되므로 유실이 없다 — 의도된 정지(fail-stop).
                log.exception("handle_event 실패 — offset %s seek 되감기 후 재시도: %s",
                              msg.offset(), e)
                try:
                    from confluent_kafka import TopicPartition
                    c.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))
                except Exception as se:                      # noqa: BLE001 — 되감기 자체 실패
                    log.error("seek 실패 — 이 이벤트는 재기동 시 재처리 필요 "
                              "(offset=%s): %s", msg.offset(), se)
                if a.once:
                    log.error("--once 처리 실패 → FAILED 로 종료 (재시도 없음)")
                    status = "FAILED"
                    break
                time.sleep(RETRY_BACKOFF_SEC)
    finally:
        c.close()
        log.info("종료 완료 (%s)", status)
    return EXIT_STATUS.get(status, 0)


if __name__ == "__main__":
    sys.exit(main())
