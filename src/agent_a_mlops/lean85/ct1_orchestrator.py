# -*- coding: utf-8 -*-
"""CT①(lean85) 오케스트레이터 — 일간 슬라이딩 재학습·검증·배포 코어 (WP-4 / 설계 D1·**D13**).

설계 정본: `CT1_배선_설계_v2.md` (v1 대체 — WP-7 폐지 + D13·D14 신설).

역할: 상주 프로세스로 돌면서
  · 매일 `ct.ct1_run_at_utc` 정기 실행(**기준 = 오전 7시 KST = naive UTC 22:00 전일** —
    2026-08-05 PM 확정, 설계 §3-1. ct_id 날짜는 UTC 축이라 KST 아침 실행분이 전일자로
    찍힌다 — 표시층만 KST 변환) + `lp.should_retrain()` 폴링으로 **요란 PM 기입 즉시** 실행
  · 조립(ct1_assemble_trainset) → 재학습(retrain_lean85) → **자동 검증 게이트**(validate_lean85)
  · 게이트 PASS → **자동 promote**(D3 ①안 확정) → 신호 드롭 → consumer 관리형 리로드
  · 매 실행 `ct_decisions` 1행 + 승격 시 `models/CHANGELOG.md` + `fdc.mlops` 발행

Kafka consumer 가 아니다 — `fdc.alert` 를 우회하는 게 아니라 헌법 3-3 이 규정하는 **독립
캐던스**다 (설계 §10-8: 1-2 무관). 승인 게이트도 없다 — 1-1 승인 목록(Recipe·실력치·정비·
wafer 판정)에 모델 교체는 없고, 모델 출력 경로 변경은 Model R2R 자동 허용과 같은 논리다.

배포 정책 (D3 — 2026-08-04 PM 확정: ① 완전 자동 / 2026-08-05 팀 가정 반영):
  게이트 전 항목 PASS 후 즉시 자동 promote 한다. §5 ⓓ 절충대로 **차단기 키
  `ct.ct1_auto_promote` 는 남긴다** — config 정본값 true(①안), **코드 폴백 기본값은
  false**(params 로드 실패 = 전 가드 잠금 — 리뷰 B4). false 로 내리면 PASS 를 `SHADOW`
  로 기록만 하고 교체는 사람이 신호 드롭으로 실행한다(같은 채널).
  **팀 가정 (2026-08-05, 설계 §5 후속의무 개정): 배포 전 게이트 검증 정확도 = 100%.**
  이에 따라 **WP-7(배포 후 감시·자동 롤백)은 설계 v2 에서 폐지**됐고(개정 ③ — S3 결번),
  그 대가로 확정 장치 2종이 들어왔다: **D13 절대 열화 가드**(게이트 D2 항목 + 여기의
  champion 연속 초과 에스컬레이션)와 **D14 리로드 스모크**(consumer). 잔여 안전망은
  ⓐ 일간 사이클의 재평가 — 단 **노출 상한은 "다음 실행"이 아니라 "다음 승격"까지**다
  (설계 §5-3 ⓐ: 승격 임계 3.0% 가 실증 개선폭과 근접해 다수 일이 SKIP 일 수 있고
  `ct1_max_stale_champion_days` 경보의 존재 자체가 연속 미승격을 정상으로 상정한다.
  그 구멍을 메우는 것이 D13 champion streak 다 — 교체가 아니라 **발화**)
  ⓑ `ct_decisions` 3중 기록 ⓒ 차단기 수동 강하 + **미탐 1건 시 회귀 규율**(가정의
  반증 장치 — 설계 §5 후속의무 3′: WP-7 재소환 검토 포함).

  자동 롤백은 없다 (v2 개정 ③). 롤백 = 직전 champion(D8 이 무조건 보존)으로 promote
  신호 **수동 재드롭** — 같은 채널이라 consumer 는 무변경이고 D14 스모크가 동일 적용된다.

가드 (순서대로 — 첫 실패 사유를 ct_decisions 에 기록):
  g1 `ct1_auto_generate` off      → DRYRUN (후보 감지 기록 후 종료 — S1 개통 전)
  g2 중복 실행 락                  → skip (재학습 subprocess 진행 중 신규 트리거)
  g3 멱등 (ct_id RUNNING 선점)     → skip
  g4 명령 템플릿 미등재            → IFACE_WAIT (게이트 없이 배포 금지)

⚠️ retrain·validate 서브프로세스는 인터랙티브 콘솔 계열에서 실행할 것 — Windows WDAC 가
   detached 프로세스의 torch/네이티브 DLL 을 차단한 실측 사고 있음 (docs/adr/TSR-0001).

CLI:
    python ct1_orchestrator.py                # 상주 루프 (run_stack.ps1 창 1개)
    python ct1_orchestrator.py --once         # 1회 실행 후 종료 (수동·크론)
    python ct1_orchestrator.py --once --now 2026-08-04T02:00:00 --tag manual
    # 수동 승격·롤백 (설계 §4-5 — 차단기 강하 구간 · 자동 롤백 폐지 후 유일한 복구 수단)
    python ct1_orchestrator.py --manual-promote models/c65_predictor/v2_lean85/lean85_...
    python ct1_orchestrator.py --rollback      # 직전 champion(D8 보존분)으로 되돌린다
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lean85_pipeline as lp  # noqa: E402  (창 산식·모델 로드 단일 소스)

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = lp.REPO_ROOT
CONTROL_DIR = REPO_ROOT / "control" / "ct1"        # promote 신호 채널 (consumer 가 스캔)
CHAMPION_PTR = CONTROL_DIR / "champion.json"       # 운영 champion 포인터 (D5 — glob 강등)
LOCK_PATH = CONTROL_DIR / "ct1_orchestrator.lock"
STATE_PATH = CONTROL_DIR / "ct1_state.json"        # 마지막 정기 실행일 (캐치업 판정)
WORK_DIR = REPO_ROOT / "Data" / "ct1"              # 조립 산출물

CT_TYPE = "ct1_xgb"                                # 구 'ct1_lgbm' — XGBoost 확정(2026-07-22)
TOPIC_OUT = "fdc.mlops"
VALIDATION_REPORT_NAME = "validation.json"         # = validate_lean85.VALIDATION_REPORT
MODEL_VERSION_MAX = 32                             # ct_decisions.model_version_after VARCHAR(32)
TRIGGER_REASON_MAX = 64                            # ct_decisions.trigger_reason VARCHAR(64)
# `_do_promote` → `handle_trigger` 경고 전달 키. **게이트 리포트 필드가 아니다** —
# 오케스트레이터가 in-memory report dict 에 얹는 값이며 validation.json 에 쓰이지 않는다.
# 문자열 리터럴 대신 상수를 쓰는 이유: 이름이 바뀌어도 한쪽만 고쳐져 가드가 조용히
# 죽는 일이 없게 (헌법 7장 — "형태를 폐기하면서 읽는 코드를 안 찾음").
PROMOTE_WARN_KEY = "ct1_promote_warnings"

# 게이트 CLI exit code 규약 (validate_lean85 단일 소스와 값 일치 — 프로세스 경계라 import 안 함)
GATE_PASS, GATE_ERROR, GATE_FAIL, GATE_SKIP = 0, 1, 2, 3
ASM_OK, ASM_ERROR, ASM_GATED = 0, 1, 2

# `--once` 종료 코드 — 크론·CI 래퍼가 "게이트 FAIL"을 성공으로 오독하지 않게 한다.
# BLOCKED(누수·계약 위반)는 시스템이 낼 수 있는 가장 큰 신호인데 0을 주면 아무도 못 본다
# (리뷰 S3). SKIP/DRYRUN/LOCKED 는 정상 종료(0).
EXIT_STATUS = {"FAILED": 1, "BLOCKED": 2, "IFACE_WAIT": 3}
# 수동 promote 부분 실패 — **배포는 됐고** champion 포인터만 낡은 상태. 0(완전 성공)도
# 1(미실행)도 아닌 제3의 결과라 별도 코드를 준다: 0 을 주면 사람이 후속 복구를 놓치고,
# 1 을 주면 "안 됐구나" 하고 재실행해 신호를 한 번 더 드롭한다.
EXIT_MANUAL_PARTIAL = 4

ASSEMBLE_TIMEOUT_SEC = 1800
VALIDATE_TIMEOUT_SEC = 1800
RETRAIN_TIMEOUT_SEC_DEFAULT = 3600
POLL_SEC_DEFAULT = 60.0
RETRY_BACKOFF_SEC_DEFAULT = 30.0
LOCK_STALE_SEC = 6 * 3600                          # 이 시간 넘은 락은 유령으로 보고 회수
EVENT_PM_MEMORY = 50                               # 이벤트 디듀프 기억 상한 (무한 성장 방지)
ZERO_SAMPLE_SKIP_ALERT = 3                         # 연속 0표본 SKIP 이 이 횟수를 넘으면 경보
MAX_DAILY_FAILURES_DEFAULT = 3                     # 코드 폴백 — 당일 연속 FAILED 상한 (H2)
# D13 (설계 v2 §4-7) — champion 절대 상한 연속 초과 에스컬레이션 임계. params
# `ct.ct1_abs_guard_champion_streak` 로 override. **코드 기본값은 config 와 같은 3** 이다:
# 이 값은 가드를 여는 스위치가 아니라 **경보를 늦추는 값**이라, params 로드 실패 시
# 잠금(=∞)으로 두면 오히려 경보가 사라진다 (auto_promote 계열의 폴백 규율과 방향이 반대).
ABS_GUARD_STREAK_DEFAULT = 3

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [ct1-orch] %(message)s")
log = logging.getLogger("ct1_orchestrator")

running = True


# ── config ────────────────────────────────────────────────────────────────
def _params() -> dict:
    """`config/params.yaml` `ct:` 절 로드 (헌법 6-1 — 실패 시 안전 기본값: 전부 잠금)."""
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("ct") or {}
    except Exception as e:                                   # noqa: BLE001
        log.warning("params.yaml 로드 실패 → 전 가드 잠금 기본값: %s", e)
        return {}


def _db():
    """DATABASE_URL 접속 (지연 import — dry-run 로직 테스트 시 무의존)."""
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


# ── ID·기록 ───────────────────────────────────────────────────────────────
def ct_id_for(day: str, seq: int, chamber: str = "ALL") -> str:
    """`CT-<YYYYMMDD>-<CHAMBER>-<SEQ>-XGB` (헌법 6-4 · CT²의 `-AE` 접미와 대칭).

    챔버 축이 `ALL` 인 이유: lean85 는 **단일 전역 모델**이다 (ADR-0001 다챔버 전역 PM).
    챔버 스코프 전환은 학습·서빙 동시 전환 + 동결 벤치 재검증이 전제인 별도 안건이다.
    """
    return f"CT-{day}-{chamber}-{seq:03d}-XGB"


def next_ct_id(conn, day: str, chamber: str = "ALL") -> str:
    """당일 미사용 SEQ 로 ct_id 채번. DB 없으면 001 부터 (수동 경로).

    같은 날 정기+이벤트가 겹치면 SEQ 가 올라가 **별건**으로 처리된다 (설계 D1).
    """
    seq = 1
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT ct_id FROM ct_decisions WHERE ct_type=%s AND ct_id LIKE %s",
                        (CT_TYPE, f"CT-{day}-{chamber}-%-XGB"))
            used = {r[0] for r in cur.fetchall()}
        while ct_id_for(day, seq, chamber) in used:
            seq += 1
    return ct_id_for(day, seq, chamber)


def claim_running(conn, ct_id: str, reason: str) -> bool:
    """RUNNING 행 원자 선점 (g3). 새로 만들었으면 True, 이미 있으면 False.

    멱등 창을 **트리거 시점**으로 좁힌다: 이 행을 심는 순간부터, 재학습(최대 1h)이 도는
    동안 같은 ct_id 로 재진입해도 기존 행을 보고 skip 한다 (CT² WP-B1 선례).

    ★승패 판정은 **INSERT ... RETURNING** 으로 한다 (리뷰 S9). 구 구현은 `SELECT` 후
    `INSERT ON CONFLICT DO NOTHING` 하고 무조건 True 를 돌려줬는데, 두 프로세스가 동시에
    SELECT 를 통과하면 **둘 다 "이겼다"** 고 판단해 같은 ct_id 로 재학습을 2번 돈다.
    파일 락은 단일 호스트만 막는다. `RETURNING` 은 실제로 행을 심은 쪽만 결과를 받으므로
    rowcount 신뢰 문제(ct2_orchestrator 주석) 없이 원자적이다.
    """
    if conn is None:
        return True
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ct_decisions (ct_id, ct_type, trigger_reason, retrain_status,
                                         model_version_before)
               VALUES (%s, %s, %s, 'RUNNING', %s)
               ON CONFLICT (ct_id) DO NOTHING
               RETURNING ct_id""",
            (ct_id, CT_TYPE, reason[:TRIGGER_REASON_MAX], _champion_name()))
        won = cur.fetchone() is not None
    conn.commit()
    return won


def record_decision(conn, ct_id: str, status: str, reason: str,
                    after: str | None = None) -> bool:
    """dry-run 경로 전용 1행 기록 (멱등 — ct_id UNIQUE).

    auto 경로는 `claim_running` / `finalize_decision` 을 쓴다.
    """
    if conn is None:
        log.info("[DB 없음] ct_decisions 기록 생략: %s %s (%s)", ct_id, status, reason)
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ct_decisions WHERE ct_id=%s", (ct_id,))
        existed = cur.fetchone() is not None
        cur.execute(
            """INSERT INTO ct_decisions (ct_id, ct_type, trigger_reason, retrain_status,
                                         model_version_before, model_version_after)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (ct_id) DO NOTHING""",
            (ct_id, CT_TYPE, reason[:TRIGGER_REASON_MAX], status, _champion_name(), after))
    conn.commit()
    return not existed


def finalize_decision(conn, ct_id: str, status: str, *, after: str | None = None,
                      reason: str | None = None, rmse_before=None, rmse_after=None,
                      cc_result: str | None = None) -> bool:
    """선점한 RUNNING 행을 종결 상태로 전이 (설계 D6).

    값이 있는 컬럼만 덮어쓴다(COALESCE). 0행이면 경고한다 — RUNNING 선점 없이 불렸다는
    뜻이라 로직 오류 신호다 (조용히 넘어가면 무기록 종결이 된다).
    """
    if conn is None:
        log.info("[DB 없음] finalize 생략: %s → %s (%s)", ct_id, status, reason)
        return False
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE ct_decisions
                  SET retrain_status=%s,
                      model_version_after=COALESCE(%s, model_version_after),
                      trigger_reason=COALESCE(%s, trigger_reason),
                      rmse_before=COALESCE(%s, rmse_before),
                      rmse_after=COALESCE(%s, rmse_after),
                      champion_challenger_result=COALESCE(%s, champion_challenger_result)
                WHERE ct_id=%s""",
            (status, after, reason[:TRIGGER_REASON_MAX] if reason else None,
             rmse_before, rmse_after, (cc_result or None), ct_id))
        hit = cur.rowcount > 0
    conn.commit()
    if not hit:
        log.warning("finalize 0행 (ct_id=%s, status=%s) — RUNNING 선점이 선행되지 않음",
                    ct_id, status)
    return hit


# ── RUNNING 고아 리퍼 (D6 위생 — 리뷰 F7) ─────────────────────────────────
# 고아 판정 임계 — **락 유령 회수와 같은 값**(6h)을 의도적으로 공유한다. 두 장치가
# 같은 사고("프로세스가 죽어 흔적만 남음")의 파일 축·DB 축이라 임계가 갈리면
# "락은 회수됐는데 행은 RUNNING" 같은 어긋난 중간 상태가 생긴다.
# 정상 실행 상한 ≈ 조립 0.5h + 재학습 1h + 게이트 0.5h 이고 셋 다 subprocess
# 타임아웃으로 강제되므로, 6h 를 넘긴 RUNNING 은 '진행 중'이 아니라 '죽음'이다.
STALE_RUNNING_SEC = LOCK_STALE_SEC


def reap_stale_running(conn) -> int:
    """finalize 전에 죽은 실행의 RUNNING 고아 행을 FAILED 로 종결한다 (D6 위생 · 리뷰 F7).

    claim 불변식("선점 이후 어떤 실패든 종결 상태를 남긴다", `handle_trigger`)은 프로세스가
    살아 있을 때만 성립한다 — finalize 전 크래시·전원 차단·DB 단절은 영구 RUNNING 행을
    남긴다. 락 파일에는 유령 회수(`acquire_lock`)가 있는데 DB 행에는 없었다. 익일 진행을
    막지는 않지만(새 SEQ 채번), 감사 테이블에서 **'진행 중'과 '죽음'이 구분 불가**해진다.

    실패해도 본 실행을 막지 않는다 — 위생 작업이지 경로가 아니다. CT① 행(`ct_type`)만
    건드린다: CT² 의 RUNNING 의미론(retrain+validate 로 최대 2.5h 블록 — WP-B1)은 소관
    밖이고, 임계도 다르다.

    스키마 무변경(UPDATE 만) — 헌법 3-2 마이그레이션·PM 승인 대상이 아니다.

    Returns:
        종결시킨 행 수 (실패·DB 부재 시 0).
    """
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                # `trigger_reason` 이 NULL 이면 `||` 결과도 NULL 이라 사유가 통째로
                # 날아간다 — COALESCE 로 막는다 (감사 흔적을 지우는 UPDATE 금지).
                """UPDATE ct_decisions
                      SET retrain_status='FAILED',
                          trigger_reason = LEFT(COALESCE(trigger_reason, '') || '/reaped', %s)
                    WHERE ct_type=%s AND retrain_status='RUNNING'
                      AND created_at < NOW() - make_interval(secs => %s)""",
                (TRIGGER_REASON_MAX, CT_TYPE, STALE_RUNNING_SEC))
            n = cur.rowcount
        conn.commit()
        if n:
            log.warning("RUNNING 고아 %d행 → FAILED 종결 (finalize 전 사망 추정 — "
                        "익일 정기가 자연 재시도한다, D10)", n)
        return n
    except Exception as e:                                   # noqa: BLE001
        log.warning("RUNNING 고아 정리 실패(본 실행 계속): %s", e)
        try:
            conn.rollback()                                  # 트랜잭션 오염 방지
        except Exception:                                    # noqa: BLE001
            pass
        return 0


# ── champion 포인터 · promote 신호 (설계 D5) ──────────────────────────────
def _atomic_write_json(obj: dict, path: Path) -> None:
    """같은 디렉토리 tmp + `os.replace` 원자 교체 (헌법 7장)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    """JSON dict 판독 — `json.loads` 성공 ≠ dict 이므로 타입 가드 (헌법 7장)."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:                                        # noqa: BLE001
        return None


def model_store_glob() -> str:
    """`lean85.model_search_glob` (기본 타입-우선 구조 — 헌법 4-1·6-4)."""
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            g = ((yaml.safe_load(f) or {}).get("lean85") or {}).get("model_search_glob")
        if g:
            return g
    except Exception as e:                                   # noqa: BLE001
        log.warning("model_search_glob 로드 실패 → 기본 glob: %s", e)
    return "models/c65_predictor/*/lean85_*"


def champion_dir(exclude: Path | None = None) -> Path | None:
    """현 운영 champion stamp 폴더 — 포인터 우선, 없으면 glob 최신(부트스트랩 전용).

    운영 교체의 정본은 promote 신호이고, `sorted(glob)[-1]` 이름 사전순 선택은 `_tag`
    접미에 취약하다 (설계 G6). 그래서 포인터를 정본으로 두고 glob 은 최초 1회 폴백이다.

    ⚠️ **`exclude` 는 선택이 아니라 안전장치다.** stamp 는 `lean85_<YYYYmmdd_HHMMSS>_<tag>`
    라 사전순 최신 = 시간순 최신이고, challenger 를 만든 **뒤에** 이 함수를 부르면 방금
    만든 challenger 자신이 champion 으로 뽑힌다. 그러면 게이트가 "champion 부재(최초
    학습)"로 판단해 **champion–challenger 비교 없이 무조건 승격**한다 — 헌법 3-3 ①
    ("평가 통과 전 배포 금지")의 우회로다. 호출자는 challenger 생성 **전에** 부르거나
    challenger 경로를 `exclude` 로 넘겨야 한다.

    Args:
        exclude: 후보에서 제외할 경로 (challenger 자기 자신).
    """
    ex = Path(exclude).resolve() if exclude else None
    d = _read_json(CHAMPION_PTR)
    if d and d.get("model_dir") and Path(d["model_dir"]).exists():
        p = Path(d["model_dir"])
        if ex is None or p.resolve() != ex:
            return p
        log.error("champion 포인터가 challenger 자신을 가리킴 — 무시 (%s)", p.name)
    hits = [p for p in sorted(REPO_ROOT.glob(model_store_glob()))
            if p.is_dir() and (ex is None or p.resolve() != ex)]
    return hits[-1] if hits else None


def _champion_name() -> str | None:
    """champion 폴더명 (감사 컬럼용). 없으면 None."""
    c = champion_dir()
    return c.name if c else None


def drop_promote_signal(ct_id: str, model_dir: Path, *, rollback: bool = False) -> Path:
    """관리형 리로드 신호 드롭 — consumer 가 감지해 모델 재적재 (설계 D5 · 계약 §8-E 대칭).

    자동·수동(차단기 강하 구간의 사람 실행)·롤백이 **같은 채널**을 쓴다 → consumer 는
    D3 결정과 무관하게 단일 구현이다.
    """
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    name = f"promote_{ct_id}{'-rollback' if rollback else ''}.json"
    sig = CONTROL_DIR / name
    _atomic_write_json({
        "ct_id": ct_id,
        "model_dir": str(Path(model_dir).resolve()),   # 절대경로 — consumer cwd 무관
        "model_version": Path(model_dir).name,
        "rollback": bool(rollback),
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, sig)
    return sig


def set_champion(ct_id: str, model_dir: Path, previous: Path | None) -> None:
    """champion 포인터 갱신 — 직전 champion 을 함께 남긴다 (롤백 대상·GC 보존 근거, D8)."""
    _atomic_write_json({
        "ct_id": ct_id,
        "model_dir": str(Path(model_dir).resolve()),
        "model_version": Path(model_dir).name,
        "previous_model_dir": str(previous.resolve()) if previous else None,
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, CHAMPION_PTR)


def append_changelog(ct_id: str, model_dir: Path, report: dict, auto: bool) -> None:
    """`models/CHANGELOG.md` 승격 기록 (헌법 3-3·4-1 — 버전·RMSE·사유·피처 수).

    **승격 건만 기록한다** (설계 §7-3 해석 1, PM 확정): 일간 캐던스에서 매 실행을 남기면
    연 365행(대부분 SKIP)이 되어 운영 이력이 오염된다. 일간 생성 이력의 정본은
    `ct_decisions` 다.

    실패해도 배포 흐름은 계속하고 로그만 남긴다 — 감사 정본은 DB 행이다.
    """
    try:
        m = report.get("metrics") or {}
        route = "자동 게이트 PASS(D3 ①안)" if auto else "차단기 강하 — 수동 신호"
        line = (f"\n- {datetime.now(timezone.utc):%Y-%m-%d} CT①(lean85) promote: "
                f"`{Path(model_dir).name}` — {ct_id} · {route} · "
                f"RMSE {m.get('rmse_before', '-')} → {m.get('rmse_after', '-')} "
                f"(개선 {m.get('rmse_gain_pct', '-')}%) · 피처 "
                f"{(report.get('gate_params') or {}).get('features_n', '-')}개 · "
                f"학습 {m.get('n_train_wafers', '-')}장 / 평가 {m.get('n_eval_scored', '-')}장"
                f"(온셋 제외) · 사유: 일간 슬라이딩 재학습 "
                f"(검증 리포트 = <stamp>/{VALIDATION_REPORT_NAME})\n")
        with open(REPO_ROOT / "models" / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:                                   # noqa: BLE001
        log.warning("CHANGELOG 기록 실패(흐름 계속): %s", e)


def publish_mlops(payload: dict) -> None:
    """`fdc.mlops` 발행 (계약 §7). 실패해도 흐름 계속 — 감사 정본은 `ct_decisions` 다.

    NaN/Inf 는 표준 JSON 이 아니라 대시보드 `JSON.parse` 와 DB 를 동시에 깨뜨린다 —
    `allow_nan=False` 로 발행 길목에서 막는다 (헌법 7장).
    """
    try:
        from confluent_kafka import Producer
        p = Producer({"bootstrap.servers":
                      os.environ.get("KAFKA_BOOTSTRAP_SERVERS",
                                     os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092"))})
        p.produce(TOPIC_OUT, json.dumps(payload, ensure_ascii=False,
                                        allow_nan=False).encode("utf-8"))
        # `flush()` 는 **남은 건수**를 돌려준다 — 예외가 안 났다고 전송된 게 아니다
        # (브로커 미가동이면 produce 는 큐에만 넣고 조용히 성공한다. 헌법 7장).
        remaining = p.flush(5)
        if remaining:
            log.warning("fdc.mlops 미전송 %d건 — 브로커 미가동 의심 (%s). 감사 정본 "
                        "ct_decisions 는 무영향", remaining, payload.get("ct_id"))
        else:
            log.info("fdc.mlops 발행: %s %s", payload.get("ct_id"), payload.get("retrain_status"))
    except Exception as e:                                   # noqa: BLE001
        log.warning("fdc.mlops 발행 실패(흐름 계속): %s", e)


# ── 수동 promote · 롤백 (설계 §4-5 — 리뷰 F4) ─────────────────────────────
def _assert_gate_passed(tgt: Path) -> None:
    """수동 승격 대상이 **게이트 PASS 인 그 번들**인지 확인 — 아니면 `ValueError`.

    헌법 3-3 ① 은 "champion–challenger 평가 통과 전 배포(모델 교체) 금지"다. 자동 경로는
    게이트 리포트를 읽고 판정하는데, **수동 경로엔 그 확인이 없었다** — `manifest.json`
    존재만 보고 신호를 드롭했으므로 `gate_verdict=FAIL`(BLOCKED) 폴더를 지정해도 그대로
    배포됐다. 하필 이 경로는 `ct1_auto_promote: false`(차단기 강하) 구간의 유일한 승격
    수단이라 **사람이 손으로 stamp 를 타이핑하는 자리**이고, 그래서 실수가 나는 자리다.

    세 가지를 본다 — 셋 다 "리포트가 이 폴더의 것이고 PASS 였다"를 구성한다:
      ⓐ 리포트 실재 — 판정 이력이 없는 폴더는 PASS 를 주장할 근거 자체가 없다
      ⓑ `gate_verdict == "PASS"` — SKIP 도 거부한다 (soft-fail·미실행 항목이 있다는 뜻)
      ⓒ `bundle_name == 폴더명` — 다른 번들의 PASS 리포트를 복사해 넣는 우회 차단
         (헌법 1-1 예외 3 ⓑ 의 `bundle_name` 대조와 같은 취지)

    **`return 1` 이 아니라 `raise` 인 이유**: 폴더 오타(manifest 부재)는 사용자가 경로를
    잘못 짚은 것이라 조용히 종료 코드로 돌려주면 되지만, 이쪽은 **헌법 3-3 ① 을 뚫는
    시도**다. 종료 코드는 스크립트에서 `|| true` 한 줄로 삼켜지는 반면 예외는 스택과 함께
    남는다 (헌법 7장 "가드를 `assert` 로 작성 금지"와 같은 취지 — 가드는 지워지거나
    삼켜지지 않아야 한다).

    **롤백은 이 가드를 타지 않는다.** 롤백 대상은 *이미 champion 이었던* 모델이고, 자동
    롤백을 폐지한 v2(개정 ③)에서 수동 롤백은 **유일한 복구 수단**이다. 여기에 PASS 를
    요구하면 게이트 도입 이전에 승격된 champion 으로는 되돌아갈 수 없어, 사고 대응
    수단이 사고를 막는 가드에 막힌다 (가드가 서비스를 죽이는 역전).

    Args:
        tgt: 승격 대상 stamp 폴더 (해석 완료된 절대 경로).

    Raises:
        ValueError: 리포트 부재 · PASS 아님 · `bundle_name` 불일치.
    """
    rep = _read_json(tgt / VALIDATION_REPORT_NAME)
    if rep is None:
        raise ValueError(
            f"게이트 리포트 부재/판독 불가 — 수동 승격 거부: {tgt / VALIDATION_REPORT_NAME}. "
            f"평가를 통과한 적 없는 모델은 배포할 수 없다 (헌법 3-3 ①)")
    verdict = rep.get("gate_verdict")
    if verdict != "PASS":
        raise ValueError(
            f"게이트 판정 {verdict!r} — 수동 승격 거부: {tgt.name}. "
            f"PASS 가 아닌 번들(FAIL=BLOCKED·SKIP=미검증 항목 존재)은 차단기 강하 구간에도 "
            f"배포할 수 없다 (헌법 3-3 ①). 실패 항목={rep.get('failed_checks')}")
    name = rep.get("bundle_name")
    if name != tgt.name:
        raise ValueError(
            f"게이트 리포트가 다른 번들의 것이다 — 수동 승격 거부: 폴더={tgt.name} "
            f"리포트 bundle_name={name!r}. PASS 리포트 복사로 게이트를 우회할 수 없다 "
            f"(헌법 1-1 예외 3 ⓑ 대조 원칙)")


def manual_promote(target: str | None, *, rollback: bool) -> int:
    """수동 승격·롤백 — 신호 드롭 + champion 포인터 + `ct_decisions` + CHANGELOG 일괄.

    설계 §4-5 의 수동 경로(차단기 강하 구간의 승격 · **자동 롤백을 폐지한 v2(개정 ③)에서
    유일한 복구 수단**인 롤백)는 "사람이 신호를 드롭한다"인데, 그동안 **champion 포인터
    갱신 주체가 코드에 없었다.** 신호만 드롭하고 포인터를 방치하면
      ⓐ 다음 게이트가 **서빙 중이 아닌 모델**과 비교하고 (헌법 3-3 ①)
      ⓑ D13 streak 의 champion 이름 변화 리셋(§8-1 ①ⓑ)이 발화하지 않으며
      ⓒ `ct_decisions(ROLLED_BACK)` 기록이 수작업 SQL 에 의존한다.
    셋 다 사람이 잊기 쉬운 후속 작업이라 코드로 묶는다.

    순서는 자동 경로(`_do_promote`)와 같다 — **선점 → 신호 → 포인터 → 기록**. 부분 실패가
    "기록은 승격인데 배포는 안 됨"(S12 위반 방향)이 아니라 "배포됐는데 기록 실패"
    (★로그 + 감사 사유 `/ptr_fail` 접미로 복구 지시) 쪽으로 떨어지게 한다. RUNNING 을
    먼저 선점하므로 도중 사망분은 F7 리퍼가 종결시킨다.

    DB 접속 불가면 **아무것도 하지 않는다** — 기록 없는 교체는 헌법 7장(무기록 유실)
    위반이고, 그 상태의 교체는 감사 테이블에서 영영 보이지 않는다.

    ⚠️ 롤백 후 포인터의 `previous_model_dir` 는 방금 물러난(=문제의) 모델이 된다.
       연속 `--rollback` 은 그 모델로 되돌아간다 — 두 단계 이전으로 가려면 stamp 를
       `--manual-promote` 로 명시할 것 (D8 이 승격 계보를 영구 보존한다).

    Args:
        target: 승격할 stamp 폴더 경로. `rollback=True` 면 무시된다.
        rollback: True 면 champion 포인터의 `previous_model_dir`(D8 보존분)로 되돌린다.

    Returns:
        exit code — 0 성공 / 1 미실행(가드 차단) / `EXIT_MANUAL_PARTIAL` 배포는 됐으나
        포인터 갱신 실패(수동 복구 필요).

    Raises:
        ValueError: 승격 대상이 게이트 PASS 가 아니거나 리포트가 다른 번들의 것일 때
            (`_assert_gate_passed` — 헌법 3-3 ①). 롤백 경로는 해당 없음.
    """
    if not rollback and not target:
        log.error("승격 대상 미지정 — --manual-promote <STAMP_DIR> 또는 --rollback")
        return 1
    if LOCK_PATH.exists():
        # 막지 **않는다** — 롤백은 긴급 복구 경로라 재학습 1h 락 뒤에 줄 세우면
        # 장치의 목적이 사라진다. 대신 경합 가능성을 드러낸다: 진행 중 사이클은
        # 재학습 **전에** 뽑아둔 champion 으로 게이트를 돌기 때문이다 (run_cycle).
        log.warning("오케스트레이터 락 존재 — 실행 중인 사이클과 경합 가능 (%s). "
                    "진행 중 사이클의 게이트 비교 기준은 교체 전 champion 이다", LOCK_PATH)
    try:
        conn = _db()
    except Exception as e:                                   # noqa: BLE001
        log.error("DB 접속 실패 — 수동 promote 중단 (기록 없는 교체 금지): %s", e)
        return 1
    try:
        ptr = _read_json(CHAMPION_PTR) or {}
        if rollback:
            tgt = ptr.get("previous_model_dir")
            if not tgt:
                log.error("롤백 대상 없음 — champion 포인터에 previous_model_dir 부재 (%s). "
                          "승격 이력이 없거나 포인터가 부트스트랩 상태다", CHAMPION_PTR)
                return 1
        else:
            tgt = target
        tgt = Path(tgt).resolve()
        if not (tgt / "manifest.json").exists():
            # consumer 는 신호를 신뢰하고 재검하지 않는다 (§4-5 개정 ④) — 오타 하나가
            # 서빙 모델 로드 실패로 직결되므로 발행 전에 폴더 실체를 확인한다.
            log.error("대상 폴더에 manifest.json 없음 — 모델 폴더가 아니다: %s", tgt)
            return 1
        if not rollback:
            # 헌법 3-3 ① — 평가 통과 전 교체 금지. 선점(claim_running)·신호 드롭 **앞**에
            # 둔다: 뒤에 두면 감사 축에 RUNNING 만 남기고 죽어 F7 리퍼가 치워야 한다.
            _assert_gate_passed(tgt)
        if len(tgt.name) > MODEL_VERSION_MAX:
            log.error("대상 폴더명 %d자 > %d — 감사 컬럼(model_version_after) 절단 위험: %s",
                      len(tgt.name), MODEL_VERSION_MAX, tgt.name)
            return 1
        prev = ptr.get("model_dir")
        if prev and Path(prev).resolve() == tgt:
            log.warning("대상이 이미 champion 이다 (%s) — 신호 재드롭으로 consumer "
                        "리로드만 유도한다", tgt.name)
        day = _now().strftime("%Y%m%d")
        ct_id = next_ct_id(conn, day)
        status = "ROLLED_BACK" if rollback else "PROMOTED"
        if not claim_running(conn, ct_id, f"manual/{status.lower()}"):
            log.error("ct_id 선점 실패(동시 실행?): %s", ct_id)
            return 1
        sig = drop_promote_signal(ct_id, tgt, rollback=rollback)
        tail = ""
        try:
            set_champion(ct_id, tgt, Path(prev) if prev else None)
        except Exception as e:                               # noqa: BLE001
            log.error("★champion 포인터 갱신 실패 — 신호는 이미 드롭됨(서빙은 교체된다). "
                      "포인터가 낡으면 다음 게이트가 서빙 중이 아닌 모델과 비교한다 "
                      "(헌법 3-3 ①). set_champion 재실행으로 수동 복구할 것: %s", e)
            tail = "/ptr_fail"
        reason = f"manual/{status.lower()}/{tgt.name}{tail}"[:TRIGGER_REASON_MAX]
        finalize_decision(conn, ct_id, status, after=tgt.name, reason=reason)
        append_changelog(ct_id, tgt, _read_json(tgt / VALIDATION_REPORT_NAME) or {}, auto=False)
        publish_mlops({
            "event_type": "CtDecision", "ct_id": ct_id, "ct_type": CT_TYPE,
            "trigger_reason": "manual", "retrain_status": status,
            "model_version_before": Path(prev).name if prev else None,
            "model_version_after": tgt.name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        log.info("수동 %s 완료: %s (%s · 신호 %s) — consumer 가 D14 스모크 통과 후 스왑한다",
                 "롤백" if rollback else "승격", tgt.name, ct_id, sig.name)
        return EXIT_MANUAL_PARTIAL if tail else 0
    finally:
        conn.close()


# ── 중복 실행 락 (g2) ─────────────────────────────────────────────────────
def acquire_lock() -> bool:
    """단일 실행 락 획득 (`O_EXCL` 원자 생성). 이미 있으면 False.

    재학습 subprocess 가 도는 동안 새 트리거가 겹치는 것을 막는다. 유령 락(프로세스가
    죽어 남은 파일)은 `LOCK_STALE_SEC` 경과 시 회수한다 — 그러지 않으면 한 번의 크래시가
    일간 루프를 영구 정지시킨다.
    """
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0.0
        if age > LOCK_STALE_SEC:
            log.warning("유령 락 회수 (%.1fh 경과): %s", age / 3600, LOCK_PATH)
            LOCK_PATH.unlink(missing_ok=True)
        else:
            return False
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(),
                            "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
    return True


def release_lock() -> None:
    """락 해제 (실패해도 흐름 계속 — stale 회수가 뒷받침한다)."""
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError as e:
        log.warning("락 해제 실패: %s", e)


# ── subprocess ────────────────────────────────────────────────────────────
def _render_cmd(tpl: str, **kw) -> list[str]:
    """config 명령 템플릿 → argv. `python` 선두 토큰은 현재 인터프리터로 치환.

    오케스트레이터가 venv 안에서 돌 때 PATH 의 `python` 이 다른 해석기일 수 있다
    (xgboost 미설치 → 학습이 조용히 실패). `sys.executable` 로 고정한다.
    """
    argv = [c.format(**kw) for c in tpl.split()]
    if argv and argv[0] in ("python", "python3", "py"):
        argv[0] = sys.executable
    return argv


def _run(argv, cwd, timeout, label) -> subprocess.CompletedProcess:
    """subprocess 실행 + 표준 로깅 (TSR-0001: 인터랙티브 콘솔에서 실행할 것).

    `encoding="utf-8"` 을 못 박는다 — 자식들이 한글과 `⚠`/`★`/`│` 를 찍는데, Windows
    에서 `text=True` 만 주면 파이프가 cp949 로 디코딩돼 **실패 사유가 UnicodeDecodeError
    로 뭉개진다** (헌법 7장 인코딩 계열). 자식 쪽도 `PYTHONIOENCODING` 으로 고정한다.
    """
    log.info("  $ %s", " ".join(str(x) for x in argv))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=cwd, env=env)
    if r.returncode != 0:
        log.warning("  %s rc=%s stderr=%s", label, r.returncode, (r.stderr or "")[-500:])
    return r


# ── 본 루프 1회분 ─────────────────────────────────────────────────────────
def _bump_zero_sample() -> None:
    """연속 0표본 SKIP 카운터 증가 — 임계 초과 시 에스컬레이션 (리뷰 S6).

    일반 SKIP(승격 기준 미달)과 달리 이것은 **아무것도 학습하지 못하고 있다**는 뜻이라
    노화 경보(D10)와 별개의 신호다. 알림 채널이 없으므로 로그가 에스컬레이션의 실체다.
    """
    n = int(_state().get("zero_sample_streak") or 0) + 1
    _mark_state(zero_sample_streak=n)
    if n >= ZERO_SAMPLE_SKIP_ALERT:
        log.error("★에스컬레이션: 연속 %d회 학습 창 0표본 — CT① 이 사실상 정지 상태다. "
                  "d(기준 시각)와 데이터 기간의 정합을 확인하세요 "
                  "(압축시간 데모는 LEAN85_SIM_NOW / ct1_data_glob 점검)", n)


def _reset_zero_sample() -> None:
    """0표본이 아닌 실행이 나오면 연속 카운터를 되돌린다."""
    if _state().get("zero_sample_streak"):
        _mark_state(zero_sample_streak=0)


# ── 정기 실행 결과 처리 (H2 — 재시도 폭주 차단) ────────────────────────────
def _bump_daily_failure(day: str) -> int:
    """당일 연속 FAILED 카운터 증가. 날짜가 바뀌면 1부터 (하루 단위로 자연 리셋).

    상태 파일이 수기 편집·손상으로 비수치를 담고 있어도 **여기서 던지면 안 된다** — main
    루프의 `except` 가 받아 `last_scheduled_day` 마킹에 도달하지 못하고, H2 가 막으려던
    재시도 루프로 그대로 되돌아간다. 판독 불가는 0으로 보고 다시 세기 시작한다.
    """
    s = _state()
    try:
        prev = int(s.get("failed_streak") or 0)
    except (TypeError, ValueError):
        log.warning("상태 파일의 failed_streak 판독 불가(%r) → 0부터 다시 센다",
                    s.get("failed_streak"))
        prev = 0
    n = (prev + 1) if str(s.get("failed_day") or "") == day else 1
    _mark_state(failed_day=day, failed_streak=n)
    return n


def _reset_daily_failure() -> None:
    """완주한 실행이 나오면 카운터를 되돌린다 (일시 장애였다는 뜻)."""
    if _state().get("failed_streak"):
        _mark_state(failed_streak=0)


def after_scheduled(p: dict, status: str, day: str) -> str:
    """정기 실행 결과 → 다음 행동. `"consume"` / `"retry"` / `"give_up"` (리뷰 H2).

    기존 규율은 "FAILED·LOCKED 면 그날을 소비하지 않고 backoff 후 재시도"였다. **일시
    장애**(DB 다운·락 경합)에는 맞지만 **결정론적 실패**(데이터 glob 오경로·스키마 불일치·
    재학습 코드 결함)를 구분하지 못해, fast-fail 이면 하루에 수백 번 재시도한다 —
    매 시도가 새 SEQ 의 `ct_decisions` 행(RUNNING→FAILED) + subprocess 를 남기므로
    **감사 테이블 오염 + CPU 낭비 + 진짜 원인 로그가 홍수에 묻힘**이 된다.

    그래서 당일 연속 FAILED 가 `ct.ct1_max_daily_failures` 에 도달하면 **오늘을 소비하고**
    ★에스컬레이션 1건으로 종결한다. 익일 정기가 자연 재시도하므로 영구 정지가 아니다
    (D10 노화 경보와 같은 규율 — 사람이 볼 신호를 남기고 루프는 조용해진다).

      · `LOCKED` 는 **세지 않는다** — 다른 프로세스가 도는 중이라 실패가 아니다 (현행 유지).
      · 완주(SKIP·BLOCKED·PROMOTED…)는 카운터를 리셋한다.
      · 상한 0·음수면 기능 해제(무한 재시도 = 구 동작).
    """
    if status not in ("FAILED", "LOCKED"):
        _reset_daily_failure()
        return "consume"
    if status == "LOCKED":
        return "retry"

    n = _bump_daily_failure(day)
    try:
        cap = int(p.get("ct1_max_daily_failures", MAX_DAILY_FAILURES_DEFAULT))
    except (TypeError, ValueError):
        cap = MAX_DAILY_FAILURES_DEFAULT
    if cap > 0 and n >= cap:
        log.error("★에스컬레이션: 오늘(%s) 정기 실행이 연속 %d회 FAILED (상한 %d) — 일시 장애가 "
                  "아니라 **결정론적 실패**로 보고 오늘을 소비한다. 익일 정기가 자연 재시도하며, "
                  "그 전에 원인을 보려면 ct_decisions(ct_type='%s', retrain_status='FAILED') 의 "
                  "trigger_reason 과 직전 실행 로그를 볼 것 — 데이터 glob 오경로·스키마 불일치·"
                  "재학습 코드 결함이 흔한 원인이다", day, n, cap, CT_TYPE)
        return "give_up"
    return "retry"


def _update_champion_cap_streak(p: dict, report: dict, status: str = "",
                                now: datetime | None = None) -> int:
    """champion 절대 상한 **연속** 초과 추적 → 임계 도달 시 ★에스컬레이션 (D13 · 설계 §4-7).

    게이트(`validate_lean85`)가 매 실행 산출하는 `metrics.champion_over_cap` 을 소비한다.
    게이트는 대조만 하고 판정하지 않는다 — H=1일 pooled RMSE 는 일 변동성이 커서 어려운
    wafer 몇 장이 건강한 모델의 하루 수치를 밀 수 있기 때문이다. **상한은 여유 있게,
    발화는 연속으로**가 D13 의 설계다.

    ⚠️ **경보만이다.** 게이트를 우회한 강제 교체·롤백은 하지 않는다 (D10 동형 — 연속
    미승격 노화 경보와 같은 규율). 알림 채널(WP-C)이 없으므로 로그 + `ct_decisions`
    시계열이 에스컬레이션의 실체다.

    측정 불가(`None` — champion 부재·표본 미달로 `rmse_before` 가 없는 경우)면 streak 를
    **유지한다**: 증거가 없는 날을 '정상'으로 세면 격일 표본 미달만으로 연속이 영원히
    끊겨 가드가 조용히 죽는다. 반대로 증가시키면 없는 증거로 발화한다.

    Args:
        status: `run_cycle` 종결 상태. `AUTO_PROMOTED` 면 리셋한다 — 이 지표는 "지금
            서빙 중인 champion 이 연속 초과했다"는 뜻인데, 승격은 champion 을 갈아치웠고
            새 champion 은 방금 D2(절대 상한)를 통과했다.
        now: 실행 기준 시각. **같은 UTC 날의 2회차는 세지 않는다** — 정기(일간)와 요란 PM
            이벤트가 겹치면 거의 같은 평가창으로 두 번 도는데, 그건 이틀치 증거가 아니라
            같은 하루의 상관된 증거다. 연속 조건의 근거가 "일 변동성"이므로 축도 일이다.

    Returns:
        갱신 후 연속 초과 횟수.
    """
    m = (report or {}).get("metrics") or {}
    over = m.get("champion_over_cap")
    st = _state()
    prev = int(st.get("champion_over_cap_streak") or 0)
    prev_name, name = st.get("champion_over_cap_name"), m.get("champion_name")
    day = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")

    # ⓐ 이번 실행에서 승격했다면 이 리포트의 `champion_over_cap` 은 **교체 직전** champion
    #    에 대한 관측이다 — 새 champion 의 이력으로 셀 수 없다. 세지 말고 리셋한 뒤,
    #    다음 실행부터 새 champion 을 새로 센다.
    if status == "AUTO_PROMOTED":
        if prev:
            log.info("champion 교체(승격) — 절대 상한 streak %d → 0 리셋 "
                     "(새 champion 은 D2 상한 통과분)", prev)
        _mark_state(champion_over_cap_streak=0, champion_over_cap_name=None,
                    champion_over_cap_day=None)
        return 0

    # ⓑ 승격 외의 champion 교체 — **수동 승격(차단기 강하 구간)·수동 롤백**은 status 가
    #    SHADOW 라 위 분기에 걸리지 않는다(설계 §4-5: 자동·수동·롤백은 같은 채널). 이 경우
    #    리포트의 champion 은 이미 **새** champion 이므로, 리셋한 뒤 이번 관측부터 센다.
    changed = bool(prev_name and name and name != prev_name)
    if changed and prev:
        log.info("champion 교체 감지(%s → %s) — 절대 상한 streak %d → 0 리셋 "
                 "(옛 champion 의 이력으로 새 champion 을 고발하지 않는다)",
                 prev_name, name, prev)
    if changed:
        prev = 0

    if over is None:
        if changed:
            _mark_state(champion_over_cap_streak=0, champion_over_cap_name=name,
                        champion_over_cap_day=None)
        elif prev:
            log.info("champion 절대 상한 대조 불가(표본 미달·champion 부재) — "
                     "streak %d 유지", prev)
        return prev

    # ⓑ 같은 날 2회차는 유지 (위 `now` 설명)
    if over and prev > 0 and not changed and st.get("champion_over_cap_day") == day:
        log.info("같은 UTC 날(%s) 2회차 초과 — streak %d 유지 (연속 조건의 축은 일)",
                 day, prev)
        return prev

    cur = prev + 1 if over else 0
    _mark_state(champion_over_cap_streak=cur, champion_over_cap_name=name,
                champion_over_cap_day=day if over else None)
    if not over:
        return cur

    thr = int(p.get("ct1_abs_guard_champion_streak", ABS_GUARD_STREAK_DEFAULT))
    ctx = (f"champion={m.get('champion_name')} rmse_before={m.get('rmse_before')} "
           f"> 상한 {m.get('abs_rmse_cap')}")
    if cur >= thr:
        log.error("★에스컬레이션: champion 이 절대 상한을 연속 %d회 초과 (임계 %d) — %s. "
                  "상대 비교(D1)로는 보이지 않는 '둘 다 나쁨'일 수 있다. 학습 원천·라벨 "
                  "정합을 점검하고, 필요하면 차단기(ct1_auto_promote: false) 강하를 "
                  "검토하세요 (경보만 — 게이트 우회 교체는 하지 않는다)", cur, thr, ctx)
    else:
        log.warning("champion 절대 상한 초과 %d회 (임계 %d 도달 시 에스컬레이션) — %s",
                    cur, thr, ctx)
    return cur


def _gain_text(m: dict) -> str:
    """승격 근거 한 줄 — champion 부재(최초 학습)면 개선율이 없으므로 그 사실을 쓴다."""
    g = m.get("rmse_gain_pct")
    if g is None:
        return f"champion 부재(최초 학습) · RMSE {m.get('rmse_after')}"
    return f"개선 {g}% (RMSE {m.get('rmse_before')} → {m.get('rmse_after')})"


def _do_promote(ct_id: str, challenger: Path, champ: Path | None,
                report: dict, m: dict) -> tuple[str, str, dict]:
    """게이트 PASS 후 자동 배포 시퀀스 (D3 ①안) — 신호 드롭 이후의 실패는 배포를 번복하지 않는다.

    신호 드롭이 성공한 순간부터 '배포됨'이 사실이다 (consumer 가 스왑한다). 그 뒤의
    포인터·CHANGELOG 실패를 FAILED 로 기록하면 **서빙은 신모델, 감사는 실패**인
    역방향 불일치(설계 §4-8 의 반대 극성)가 된다 — 상태는 AUTO_PROMOTED 로 유지하고
    실패는 detail·★로그 **그리고 감사 행**(`trigger_reason` 접미)으로 드러낸다 —
    로그만 남기면 `ct_decisions` 상으로는 완전 정상인 `AUTO_PROMOTED` 로 보여서
    "서빙=신모델 / 포인터=구모델"인 상태를 쿼리로 찾을 수 없다. 신호 드롭 **자체**의
    실패는 호출자(run_cycle 의 예외 경로 → FAILED)로 전파되는 게 맞다 — 아무것도
    배포되지 않았으므로.
    """
    sig = drop_promote_signal(ct_id, challenger)
    tail = ""
    try:
        set_champion(ct_id, challenger, champ)
    except Exception as e:                                 # noqa: BLE001
        log.error("★champion 포인터 갱신 실패 — 신호는 이미 드롭됨(서빙은 교체된다). "
                  "포인터가 낡으면 다음 게이트가 **서빙 중이 아닌 모델**과 비교한다 "
                  "(헌법 3-3 ①). 수동 복구 필요 (set_champion 재실행): %s", e)
        tail = f" · ★포인터 갱신 실패({type(e).__name__}) — 수동 복구 필요"
        report.setdefault(PROMOTE_WARN_KEY, []).append("ptr_fail")
    append_changelog(ct_id, challenger, report, auto=True)   # 내부 try — 실패해도 계속
    return ("AUTO_PROMOTED",
            f"게이트 PASS → 자동 배포 {challenger.name} ({_gain_text(m)} · "
            f"신호 {sig.name} · 리포트 {challenger / VALIDATION_REPORT_NAME}){tail}", report)


def run_cycle(p: dict, *, now: datetime, tag: str, ct_id: str) -> tuple[str, str, dict]:
    """조립 → 재학습 → 게이트 → (자동 promote | SHADOW). `(status, detail, report)` 반환.

    각 단계는 **subprocess 격리**다 — 학습이 오케스트레이터 프로세스를 오염시키지 않고,
    xgboost/torch 로드 실패가 루프를 죽이지 않는다.

    게이트가 유일한 문지기다 (헌법 3-3 ①: "champion–challenger 평가 통과 전 배포 금지"):
      FAIL(rc=2)  → BLOCKED. challenger 보존·미배포·에스컬레이션. 자동 재요청 금지.
      SKIP(rc=3)  → SKIP. champion 유지, 정상 종료.
      오류(rc=1)  → FAILED. 판정 불가를 PASS 로 오해하지 않는다.
      PASS(rc=0)  → `ct1_auto_promote` 에 따라 AUTO_PROMOTED 또는 SHADOW.
    """
    asm_tpl = p.get("ct1_assemble_cmd")
    retrain_tpl = p.get("ct1_retrain_cmd")
    validate_tpl = p.get("ct1_validate_cmd")
    missing = [k for k, v in (("ct1_assemble_cmd", asm_tpl), ("ct1_retrain_cmd", retrain_tpl),
                              ("ct1_validate_cmd", validate_tpl)) if not v]
    if missing:                                          # g4 — 게이트 없이 배포 금지
        return ("IFACE_WAIT",
                f"{', '.join(missing)} 미등재 — 게이트 없이 배포 금지 (헌법 3-3 ①)", {})

    if tag not in lp.CT1_TAGS:                           # D7 고정 어휘 (32자 상한의 근거)
        return "FAILED", f"tag '{tag}' 가 고정 어휘 {lp.CT1_TAGS} 밖 — 길이 상한 보장 불가", {}

    # ★champion 은 challenger 를 만들기 **전에** 확정한다 — 만든 뒤에 뽑으면 사전순
    #  최신인 challenger 자신이 champion 이 되고, 게이트가 "champion 부재"로 오판해
    #  비교 없이 승격한다 (헌법 3-3 ① 우회로). 리뷰 B2.
    champ = champion_dir()
    log.info("champion 확정(재학습 전): %s", champ.name if champ else "없음(부트스트랩)")

    cwd = REPO_ROOT / p.get("ct1_cmd_cwd", "src/agent_a_mlops/lean85")
    stamp = now.strftime("%Y%m%d_%H%M%S")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    pm_log = os.environ.get("LEAN85_PM_LOG") or str(lp.find_file("pm_log.json"))
    data_glob = p.get("ct1_data_glob") or "Data/synthetic_1y/full_year_*.csv.gz"

    # ── ① 조립 ────────────────────────────────────────────────────────────
    r = _run(_render_cmd(asm_tpl, data=str(REPO_ROOT / data_glob), pm_log=pm_log,
                         now=now.isoformat(timespec="seconds"), out=str(WORK_DIR),
                         stamp=stamp),
             cwd, ASSEMBLE_TIMEOUT_SEC, "조립")
    if r.returncode == ASM_GATED:
        # ★0표본과 '조금 모자람'을 구분한다 (리뷰 S6). 창 안에 wafer 가 **한 장도** 없다는
        # 것은 표본 부족이 아니라 **시간축 오정렬**(데이터 기간 밖의 d)이다 — 매일 SKIP 을
        # 내며 "정상 종료"로 보고되면 영구 기능 정지가 건강한 상태로 위장된다.
        wm = _read_json(WORK_DIR / f"ct1_window_{stamp}.json") or {}
        n = wm.get("n_train_wafers")
        if n == 0:
            _bump_zero_sample()
            return ("SKIP", f"★학습 창에 wafer 0장 — 시간축 오정렬 의심 (창 "
                            f"{wm.get('start', '?')[:19]} ~ {wm.get('cutoff', '?')[:19]}, "
                            f"원천 기간 {wm.get('raw_period')}). LEAN85_SIM_NOW 로 기준 "
                            f"시각을 데이터 구간에 맞추세요", {})
        _reset_zero_sample()
        return ("SKIP", f"조립 표본 게이트 미달 — 학습 wafer {n} < 하한 (창 메타만 기록)", {})
    if r.returncode != ASM_OK:
        return "FAILED", f"조립 실패 rc={r.returncode}: {(r.stderr or '')[-300:]}", {}
    _reset_zero_sample()

    trainset = WORK_DIR / f"ct1_trainset_{stamp}.csv.gz"     # 조립기는 gzip 으로 쓴다(S4)
    evalset = WORK_DIR / f"ct1_evalset_{stamp}.csv.gz"
    winmeta = WORK_DIR / f"ct1_window_{stamp}.json"
    absent = [q.name for q in (trainset, evalset, winmeta) if not q.exists()]
    if absent:                                           # rc=0 인데 산출물 없음
        return "FAILED", f"조립 rc=0 이지만 산출물 없음: {absent} (출력 경로 확인)", {}

    # ── ② 재학습 (challenger — 미배포) ────────────────────────────────────
    result_json = WORK_DIR / f"ct1_retrain_{stamp}.json"
    timeout = float(p.get("ct1_retrain_timeout_sec", RETRAIN_TIMEOUT_SEC_DEFAULT))
    r2 = _run(_render_cmd(retrain_tpl, data=str(trainset), pm_log=pm_log, tag=tag,
                          window_meta=str(winmeta), result=str(result_json)),
              cwd, timeout, "재학습")
    if r2.returncode != 0:
        return "FAILED", f"재학습 실패 rc={r2.returncode}: {(r2.stderr or '')[-300:]}", {}
    res = _read_json(result_json)
    if not res or not res.get("model_dir"):
        return "FAILED", f"재학습 rc=0 이지만 결과 JSON 부재/불량: {result_json}", {}
    challenger = Path(res["model_dir"])
    if len(challenger.name) > MODEL_VERSION_MAX:          # 감사 컬럼 폭 (retrain 도 raise 하지만 2중)
        return ("FAILED", f"challenger 폴더명 {len(challenger.name)}자 > {MODEL_VERSION_MAX} "
                          f"— 감사 컬럼 절단 위험 ('{challenger.name}')", {})

    # ── ③ 자동 검증 게이트 ────────────────────────────────────────────────
    # champion 은 위에서 재학습 **전에** 확정했다. `auto` 를 넘기면 게이트가 다시
    # glob 을 돌아 challenger 를 집을 수 있으므로, 없을 때는 명시적으로 `none` 을 준다.
    report_p = challenger / VALIDATION_REPORT_NAME
    r3 = _run(_render_cmd(validate_tpl, challenger=str(challenger),
                          champion=str(champ) if champ else "none",
                          eval_data=str(evalset), window_meta=str(winmeta),
                          pm_log=pm_log, out=str(report_p)),
              cwd, VALIDATE_TIMEOUT_SEC, "게이트")
    report = _read_json(report_p) or {}
    m = report.get("metrics") or {}

    if r3.returncode == GATE_FAIL:
        log.error("게이트 FAIL — 배포 봉인. ct_id=%s challenger=%s report=%s "
                  "(알림 채널 부재 — 에스컬레이션의 실체는 ct_decisions 행이다)",
                  ct_id, challenger.name, report_p)
        return ("BLOCKED",
                f"게이트 FAIL {report.get('critical_failed')} — challenger 보존·미배포 "
                f"({challenger.name}). 재요청 발의는 엔지니어", report)
    if r3.returncode == GATE_SKIP:
        return ("SKIP", f"게이트 SKIP — {report.get('skip_reasons')} (champion 유지: "
                        f"{champ.name if champ else '없음'})", report)
    if r3.returncode != GATE_PASS:
        return ("FAILED", f"게이트 실행 오류 rc={r3.returncode}: {(r3.stderr or '')[-300:]}",
                report)
    if not report.get("gate_pass"):
        # rc 와 리포트가 어긋나면 **상태를 믿는다** (헌법 7장: "예외가 안 났으니 성공"
        # 금지 — 리포트 부재/불일치를 PASS 로 환원하지 않는다).
        return ("FAILED", f"게이트 rc=0 이지만 리포트 gate_pass 아님/부재: {report_p}", report)
    if report.get("bundle_name") != challenger.name:      # 리포트-번들 대조 (1-1 예외 3 ⓑ 준용)
        return ("FAILED", f"게이트 리포트가 다른 모델의 것: {report.get('bundle_name')} "
                          f"≠ {challenger.name}", report)

    # ── ④ 배포 (D3 ①안 — 완전 자동. 차단기는 비상 정지용) ─────────────────
    # 기본값은 **False** 다 (config 값이 true 여도 무관). `_params()` 는 params.yaml
    # 로드 실패 시 `{}` 를 돌려주는데, 그때 기본 True 면 "설정을 못 읽었으니 자동 배포"가
    # 된다 — `_params()` 자신의 계약("실패 시 전 가드 잠금")과 정면으로 어긋난다 (리뷰 B4).
    auto = bool(p.get("ct1_auto_promote", False))
    if not auto:
        log.warning("차단기 강하(ct1_auto_promote=false) — PASS 를 SHADOW 로 기록만. "
                    "교체는 사람이 promote 신호 드롭으로 실행 (같은 채널)")
        return ("SHADOW", f"게이트 PASS → 배포 보류(차단기) challenger={challenger.name} "
                          f"{_gain_text(m)}", report)
    return _do_promote(ct_id, challenger, champ, report, m)


def handle_trigger(p: dict, *, now: datetime, trigger: str, tag: str) -> str:
    """트리거 1건 처리 — 가드 g1~g4 + 기록. 종결 상태 문자열 반환."""
    day = now.strftime("%Y%m%d")
    conn = None
    try:
        conn = _db()
    except Exception as e:                                   # noqa: BLE001
        # DB 없이 진행하면 '기록 없이 종결'이 된다 — 그 자체가 헌법 7장 위반이므로 중단.
        log.error("DB 접속 실패 — 실행 중단 (기록 없는 CT 종결 금지): %s", e)
        return "FAILED"
    try:
        # 트리거당 1회 위생 — 지난 실행이 finalize 전에 죽었으면 그 행을 종결시킨다 (F7).
        # `next_ct_id` **앞**이지만 SEQ 채번에는 영향이 없다 (채번은 상태와 무관하게
        # 당일 사용된 ct_id 전부를 본다) — 감사 축을 먼저 정리해두는 것뿐이다.
        reap_stale_running(conn)
        ct_id = next_ct_id(conn, day)
        if not p.get("ct1_auto_generate", False):            # g1 — dry-run (S0/S1 이전)
            new = record_decision(conn, ct_id, "DRYRUN", f"{trigger}/dryrun")
            log.info("[dry-run] CT① 트리거 감지 %s (%s) — 기록=%s. "
                     "생성 개통은 ct.ct1_auto_generate: true (설계 S1)", ct_id, trigger, new)
            return "DRYRUN"
        if not acquire_lock():                               # g2 — 중복 실행 배제
            log.info("실행 중 락 존재 — 이번 트리거 skip (%s)", trigger)
            return "LOCKED"
        try:
            if not claim_running(conn, ct_id, trigger):      # g3 — 멱등 선점
                log.info("멱등 skip %s (이미 처리 중/완료)", ct_id)
                return "SKIPPED"
            # 선점 이후: 어떤 실패든 RUNNING 을 종결 상태로 남긴다 (claim 불변식).
            before = _champion_name()                        # 교체 전 champion (감사 기준점)
            try:
                status, detail, report = run_cycle(p, now=now, tag=tag, ct_id=ct_id)
            except Exception as e:                           # noqa: BLE001
                log.exception("CT① 실행 중 예외 — FAILED 로 기록: %s", ct_id)
                status, detail, report = "FAILED", f"예외: {e}", {}
            m = report.get("metrics") or {}
            # `model_version_after` = **지금 서빙 중인 모델**이다. SHADOW·BLOCKED 는
            # 배포되지 않았으므로 여기에 challenger 명을 넣으면 "무엇이 배포됐나"를
            # 쿼리로 답할 수 없게 된다 (리뷰 S12). 그 이름은 trigger_reason 에 남긴다.
            deployed = status == "AUTO_PROMOTED"
            cand = report.get("bundle_name")
            after = cand if deployed else None
            reason = f"{trigger}/{status.lower()}"
            if cand and not deployed and status in ("SHADOW", "BLOCKED", "SKIP"):
                reason = f"{reason}/{cand}"
            # 배포 시퀀스의 부분 실패(포인터 갱신 등)를 감사 축에 올린다 — detail 은
            # DB·mlops 어디에도 실리지 않아 로그가 유일한 증거였다. 이제
            # `trigger_reason LIKE '%ptr_fail%'` 로 "서빙과 포인터가 어긋난 건"을 뽑는다.
            for w in (report.get(PROMOTE_WARN_KEY) or []):
                reason = f"{reason}/{w}"
            finalize_decision(conn, ct_id, status, after=after, reason=reason,
                              rmse_before=m.get("rmse_before"), rmse_after=m.get("rmse_after"),
                              cc_result=m.get("champion_challenger_result"))
            publish_mlops({
                "event_type": "CtDecision", "ct_id": ct_id, "ct_type": CT_TYPE,
                "trigger_reason": trigger, "retrain_status": status,
                "model_version_before": before, "model_version_after": after,
                "rmse_before": m.get("rmse_before"), "rmse_after": m.get("rmse_after"),
                "champion_challenger_result": m.get("champion_challenger_result"),
                "challenger": cand,                          # 미배포 후보도 추적 가능하게
                "window_meta": m.get("window_meta"),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            log.info("CT① %s → %s: %s", ct_id, status, detail)
            try:   # ★하우스키핑 실패가 '오늘'을 삼키지 않게 — 본 실행은 위에서 이미 종결 기록됐다
                _update_champion_cap_streak(p, report, status, now)  # D13 champion 가드 (§4-7)
                _gc_unpromoted(p, conn)
                _warn_stale_champion(p)
            except Exception:                              # noqa: BLE001
                log.exception("실행 후 하우스키핑 실패 — %s 의 종결 기록은 유효. "
                              "streak·GC 는 다음 실행이 이어받는다", ct_id)
            return status
        finally:
            release_lock()
    finally:
        if conn is not None:
            conn.close()


# ── 보존(GC) · 노화 경보 (설계 D8·D10) ────────────────────────────────────
_STAMP_RE = re.compile(r"^lean85_(\d{8})_(\d{6})(?:_(\w+))?$")


def _gc_deletable(d: Path, m: re.Match) -> str | None:
    """폴더 `d` 가 GC 대상인지 — 대상이면 None, 아니면 **보존 사유**를 돌려준다.

    ★삭제는 **긍정 증거**로만 한다 (리뷰 B3). 구 구현은 "validation.json 에 PASS 가
    없으면 미승격"으로 판정했는데, 그러면 **게이트 이전에 손으로 만든 모델**
    (`lean85_20260720_163040_initial` — 99.84 벤치의 근거이자 현 운영 champion)이
    "미승격"으로 분류돼 조용히 삭제된다. 되돌릴 수 없는 사고다.

    그래서 CT① 오케스트레이터가 **자기가 만든 것**임을 스스로 증명하는 폴더만 지운다:
      ⓐ tag ∈ `CT1_TAGS` ⓑ manifest 에 `ct1_window_meta` 존재(조립기 경유) ⓒ 게이트
      리포트 존재 ⓓ 그 리포트가 PASS 가 아님. 하나라도 없으면 손대지 않는다.
    """
    if m.group(3) not in lp.CT1_TAGS:
        return f"tag '{m.group(3)}' 가 CT① 고정 어휘 밖 (수동·실험 산출물)"
    mani = _read_json(d / "manifest.json")
    if not mani:
        return "manifest.json 부재/판독 불가"
    if not mani.get("ct1_window_meta"):
        return "manifest 에 ct1_window_meta 없음 (CT① 조립기 경유 아님)"
    rep = _read_json(d / VALIDATION_REPORT_NAME)
    if rep is None:
        return "게이트 리포트 부재 — 판정 이력 없는 폴더는 지우지 않는다"
    if rep.get("gate_verdict") == "PASS":
        return "게이트 PASS(승격 계보) — 영구 보존"
    return None


def _gc_unpromoted(p: dict, conn=None) -> None:
    """미승격 challenger 정리 (D8) — 승격 계보와 **직전 champion 1개는 무조건 보존**.

    일간 캐던스면 연 365폴더가 쌓인다. 삭제는 '덮어쓰기'(4-1 금지)가 아니라 한시 보존
    만료이며, 승격 이력(champion 계보)은 영구 보존한다 (설계 §7-3 해석 3, PM 확정).

    삭제 전 `ct_decisions` 를 대조한다 (D8) — 감사 행에 PROMOTED/AUTO_PROMOTED 로
    남아 있으면 리포트가 어떻든 지우지 않는다. 삭제 실패는 **삼키지 않고** 로그에 남긴다.
    """
    days = float(p.get("ct1_gc_keep_unpromoted_days", 14))
    store = lp.default_store_dir()
    if not store.exists():
        return
    ptr = _read_json(CHAMPION_PTR) or {}
    keep = {Path(v).resolve() for v in (ptr.get("model_dir"), ptr.get("previous_model_dir")) if v}
    cur = champion_dir()
    if cur:
        keep.add(cur.resolve())

    promoted_versions: set[str] = set()                # ct_decisions 대조 (D8)
    if conn is not None:
        try:
            with conn.cursor() as cur_db:
                cur_db.execute(
                    """SELECT model_version_after FROM ct_decisions
                        WHERE ct_type=%s AND retrain_status IN ('PROMOTED','AUTO_PROMOTED')
                          AND model_version_after IS NOT NULL""", (CT_TYPE,))
                promoted_versions = {r[0] for r in cur_db.fetchall()}
        except Exception as e:                         # noqa: BLE001 — 대조 불가면 보수적으로
            log.warning("GC: ct_decisions 대조 실패 → 이번 회차 GC 생략(보수적): %s", e)
            return

    cutoff = datetime.now() - timedelta(days=days)
    for d in sorted(store.iterdir()):
        if not d.is_dir() or d.resolve() in keep:
            continue
        m = _STAMP_RE.match(d.name)
        if not m:
            continue                                   # 규칙 밖 폴더는 손대지 않는다
        if d.name in promoted_versions:
            continue                                   # 감사 행이 승격이라고 말한다 — 보존
        why = _gc_deletable(d, m)
        if why:
            log.debug("GC 보존: %s — %s", d.name, why)
            continue
        try:
            made = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        if made >= cutoff:
            continue
        log.info("GC: 미승격 challenger 삭제 (%s · 경과 %.0f일 > %.0f)",
                 d.name, (datetime.now() - made).days, days)
        try:
            shutil.rmtree(d)                           # ignore_errors 금지 — 실패를 드러낸다
        except OSError as e:
            log.error("GC 삭제 실패(다음 회차 재시도): %s — %s", d.name, e)


def _warn_stale_champion(p: dict) -> None:
    """연속 미승격 노화 경보 (D10) — **경보만**이다. 게이트를 우회한 강제 교체는 금지.

    무재학습 발산(STATIC pooled 334.2)·challenger 나이 6일 = +6.41 RMSE 실증이 근거다.

    ⚠️ 시계 일치: stamp 는 `lp.retrain` 이 **로컬** `datetime.now()` 로 찍는다. 여기서
    UTC 로 빼면 KST 기준 9시간을 과소평가한다 (리뷰 S5) — 같은 로컬 시계로 비교한다.
    """
    limit = float(p.get("ct1_max_stale_champion_days", 7))
    c = champion_dir()
    if not c:
        return
    m = _STAMP_RE.match(c.name)
    if not m:
        return
    try:
        made = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
    except ValueError:
        return
    age = (datetime.now() - made).total_seconds() / 86400
    if age > limit:
        log.error("★에스컬레이션: champion '%s' 나이 %.1f일 > %.0f일 — 연속 미승격. "
                  "게이트 임계·표본 하한을 점검하세요 (게이트 우회 강제 교체 금지, D10)",
                  c.name, age, limit)


# ── 트리거 판정 ───────────────────────────────────────────────────────────
def _now(override: str | None = None) -> datetime:
    """실행 기준 시각 d — **naive UTC** (계약 §8-B-1 시각 규율).

    ⚠️ aware 시각을 그대로 흘리면 wafer 시각(naive)과 비교하는 순간 pandas 가 TypeError
    를 낸다 (헌법 7장 등재 사고와 같은 계열 — 리뷰 B1). 경계에서 벗긴다.

    압축시간 데모·백테스트용으로 `LEAN85_SIM_NOW`(env)를 우선한다 — 상주 루프에서도
    "데이터는 2019년인데 d 는 실제 오늘"이라 창이 매번 0표본이 되는 상황을 피한다
    (리뷰 S6).
    """
    src = override or os.environ.get("LEAN85_SIM_NOW")
    if src:
        t = datetime.fromisoformat(src)
        return t.replace(tzinfo=None) if t.tzinfo else t
    return datetime.now(timezone.utc).replace(tzinfo=None)


_STATE_MEM: dict = {}   # 파일 쓰기 실패분의 메모리 미러 — 이중 실행 방지 (F2b)


def _state() -> dict:
    """마지막 정기 실행 상태 (캐치업 판정용). 파일 + 미persist 메모리 델타의 병합."""
    s = _read_json(STATE_PATH) or {}
    s.update(_STATE_MEM)
    return s


def _mark_state(**kw) -> None:
    """상태 파일 부분 갱신 (last_scheduled_day / last_event_pm).

    쓰기 실패 시 **메모리에 유지**해 프로세스 내에서는 일관되게 동작한다 — 안 그러면
    `last_scheduled_day` 미기록 → 60초 뒤 같은 날 재학습 재실행이다. 재기동하면
    메모리가 사라져 캐치업 ⓑ(§3-1)가 최대 1회 재실행할 수 있다 — 설계가 이미
    수용하는 경로라 추가 장치는 두지 않는다.
    """
    s = _state()
    s.update(kw)
    s["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        _atomic_write_json(s, STATE_PATH)
        _STATE_MEM.clear()                     # 파일이 정본으로 복귀
    except OSError as e:
        _STATE_MEM.update(kw)                  # 실패분만 메모리 유지
        log.error("상태 파일 쓰기 실패 — 메모리 상태로 계속 (재기동 시 캐치업 1회 "
                  "재실행 가능): %s", e)


def due_scheduled(p: dict, now: datetime) -> bool:
    """정기(일간) 실행 도래 여부 — `ct.ct1_run_at_utc` 통과 + 당일(UTC) 미실행.

    기준시각 = **오전 7시 KST = naive UTC 22:00 (전일)** — 2026-08-05 PM 확정 (설계 §3-1).
    ct_id·`last_scheduled_day` 의 날짜축은 UTC 라서 KST 아침 실행분의 날짜가 전일자로
    찍힌다 — 불변식은 "UTC 날짜당 정기 1회"이고, 표시 계층에서만 KST 변환한다 (6-4).

    캐치업 2단 (설계 D1 "기동 시 캐치업"의 코드 이행):
      ⓐ 같은 (UTC)날 지각 기동 — 실행 시각 경과면 즉시 1회.
      ⓑ **24시간 초과 밀림**(마지막 정기 실행일 ≤ 전전일) — 시각 무관 즉시 1회.
         ⓑ가 없으면 아침에 죽었다 낮에 살아난 프로세스가 다음 날 실행 시각까지
         ~19시간을 그냥 기다린다 (밀린 하루가 이틀이 된다).
    """
    at = str(p.get("ct1_run_at_utc", "22:00"))
    try:
        hh, mm = (int(x) for x in at.split(":")[:2])
    except (TypeError, ValueError):
        log.warning("ct1_run_at_utc 형식 오류(%r) → 22:00(=KST 07:00) 사용", at)
        hh, mm = 22, 0
    day = now.strftime("%Y%m%d")
    last = _state().get("last_scheduled_day")
    if last == day:
        return False
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    if last and str(last) < yesterday:                   # ⓑ — YYYYMMDD 는 사전순=시간순
        log.info("캐치업: 마지막 정기 실행일 %s ≤ 전전일 — 시각 무관 즉시 실행", last)
        return True
    return (now.hour, now.minute) >= (hh, mm)            # ⓐ + 정상 도래


def due_event(p: dict) -> tuple[bool, list]:
    """요란 PM 기입 이벤트 트리거 — champion manifest 기준 신규 PM 유무.

    `lp.should_retrain` 의 ② 사유만 쓴다 (① 정기는 `due_scheduled` 가 담당). 알려진
    한계 승계: 날짜 단위 비교라 **같은 날 2번째 요란 PM 은 미감지**(계약 §8-B-1 단서 ②)
    — 일간 정기 트리거가 익일 흡수하므로 수용한다.

    ★ 상태 파일 디듀프가 필수다: champion manifest 는 **승격됐을 때만** 갱신되므로,
    SKIP/BLOCKED 로 끝나면 같은 신규 PM 이 폴링 주기(60초)마다 영원히 참으로 남는다.
    처리한 PM 목록을 기억해 트리거를 1회로 묶는다.
    """
    c = champion_dir()
    if not c:
        return False, []
    mani = _read_json(c / "manifest.json")     # booster 로드 불요 — 60초 폴링 경로다
    if not mani:
        log.warning("champion manifest 판독 실패 — 이벤트 판정 생략 (%s)", c.name)
        return False, []
    pm_p = os.environ.get("LEAN85_PM_LOG") or lp.find_file("pm_log.json")
    known = set(mani.get("pm_log_dates", [])) | set(_state().get("last_event_pm") or [])
    new_pm = [str(d.date()) for d, _ in lp.parse_pm_log(pm_p) if str(d.date()) not in known]
    return bool(new_pm), new_pm


def _shutdown(signum, frame):
    """SIGINT/SIGTERM graceful (헌법 6-2)."""
    global running
    running = False
    log.info("종료 신호 %s", signum)


def main(argv=None) -> int:
    """상주 루프 (`--once` 면 1회 실행 후 종료)."""
    ap = argparse.ArgumentParser(prog="ct1_orchestrator",
                                 description="CT①(lean85) 일간 재학습 오케스트레이터")
    ap.add_argument("--once", action="store_true", help="1회 실행 후 종료 (수동·크론)")
    ap.add_argument("--now", default=None, help="기준 시각 override (ISO — 압축시간 데모)")
    ap.add_argument("--tag", default=None,
                    help=f"stamp 태그 (기본: 트리거별 자동. 고정 어휘 {lp.CT1_TAGS})")
    ap.add_argument("--manual-promote", metavar="STAMP_DIR", default=None,
                    help="수동 승격 — 신호+포인터+기록 일괄 (차단기 강하 구간용, 설계 §4-5)")
    ap.add_argument("--rollback", action="store_true",
                    help="직전 champion 으로 롤백 (D8 보존분 — 신호+포인터+기록 일괄)")
    a = ap.parse_args(argv)
    # 상호 배타 — 조용히 한쪽을 무시하면 "롤백한 줄 알았는데 재학습이 돌았다"가 된다.
    if a.manual_promote and a.rollback:
        ap.error("--manual-promote 와 --rollback 은 동시 지정 불가")
    if a.once and (a.manual_promote or a.rollback):
        ap.error("--once(재학습 사이클)와 수동 승격·롤백은 동시 지정 불가")

    p = _params()
    for msg in lp.ct1_window_mismatch():                      # G10 — 이중화 값 불일치 경고
        log.warning("config 불일치: %s (정본은 ct.ct1_* — 설계 §6)", msg)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    log.info("CT① 기동 — auto_generate=%s · auto_promote=%s · run_at_utc=%s(=KST 07:00 기준) "
             "· champion=%s (기동 시점 값 — 상주 루프는 매 반복 재로드한다, M-1)",
             p.get("ct1_auto_generate", False), p.get("ct1_auto_promote", False),
             p.get("ct1_run_at_utc", "22:00"), _champion_name())

    # 수동 경로는 루프·트리거 판정을 타지 않는다 (재학습 없음 — 교체와 기록만).
    if a.manual_promote or a.rollback:
        return manual_promote(a.manual_promote, rollback=a.rollback)

    if a.once:
        now = _now(a.now)
        status = handle_trigger(p, now=now, trigger="manual_once",
                                tag=a.tag or "manual")
        # 수동 실행은 **정기 실행일을 소비하지 않는다** — 낮에 한 번 손으로 돌렸다고
        # 그날 02:00 정기분이 사라지면 안 된다 (리뷰 NIT).
        _mark_state(last_manual_run=now.strftime("%Y%m%d %H:%M:%S"))
        return EXIT_STATUS.get(status, 0)

    poll, backoff = POLL_SEC_DEFAULT, RETRY_BACKOFF_SEC_DEFAULT
    while running:
        try:
            # ★config 는 **매 반복 재로드**한다 (코드리뷰 3차 M-1, 2026-08-08) — 기동 시
            # 1회 로드면 차단기 강하(`ct1_auto_promote: false` — 설계 §5 ⓓ 비상 정지)와
            # S1 개통(`ct1_auto_generate: true`)·상한류 변경이 재기동 전까지 미반영이라,
            # 유일한 비상 정지 수단이 "플립+재기동"인데 그 사실이 어디에도 없었다.
            # 재로드 실패는 `_params()` 계약대로 `{}` = **전 가드 잠금** (리뷰 B4 극성
            # 유지 — "설정을 못 읽었으니 무인 배포" 금지. 정기 시각과 겹치면 그날이
            # DRYRUN 으로 소비될 수 있으나, 잠금이 캐던스보다 우선한다는 것이 계약의
            # 방향이다). 게이트 임계는 원래 무관 — validate 가 subprocess 로 매 실행
            # fresh 로드한다. 비용 = 반복당 yaml 1회(≈60초당 1회).
            p = _params()
            try:
                poll = float(p.get("ct1_poll_sec", POLL_SEC_DEFAULT))
                backoff = float(p.get("ct1_retry_backoff_sec", RETRY_BACKOFF_SEC_DEFAULT))
            except (TypeError, ValueError):                  # 비수치 오설정 — 직전 값 유지
                log.warning("ct1_poll_sec/ct1_retry_backoff_sec 판독 불가 — 직전 값 유지 "
                            "(poll=%s, backoff=%s)", poll, backoff)
            now = _now()
            if due_scheduled(p, now):
                day = now.strftime("%Y%m%d")
                st = handle_trigger(p, now=now, trigger="daily_sliding", tag=a.tag or "daily")
                # ★DB 다운·락 충돌로 못 돌았으면 **그날을 소비하지 않는다** — 02:00 의
                #  일시 장애 한 번이 하루치 재학습을 통째로 건너뛰게 두면 안 된다
                #  (그 사실이 로그 한 줄로만 남는다면 조용한 기능 정지다 — 리뷰 S2).
                #  단 **연속 FAILED 상한**을 넘으면 결정론적 실패로 보고 오늘을 소비한다
                #  (재시도 폭주 = 감사 오염 + 로그 홍수 — 리뷰 H2, `after_scheduled`).
                action = after_scheduled(p, st, day)
                if action == "retry":
                    log.warning("정기 실행 미완(%s) — 오늘을 소비하지 않고 %ss 후 재시도",
                                st, backoff)
                    time.sleep(backoff)
                else:                                    # consume | give_up (에스컬레이션 완료)
                    _mark_state(last_scheduled_day=day)
            else:
                hit, new_pm = due_event(p)
                if hit:
                    log.info("이벤트 트리거 — 신규 요란 PM %s", new_pm)
                    # 디듀프 마킹은 **실행 전**에 한다: 실행이 SKIP/FAILED 로 끝나도
                    # 같은 PM 이 60초마다 재발화하는 것을 막는다. 그 PM 은 다음 정기
                    # 실행(일간)이 어차피 흡수한다.
                    # 최근 N건만 유지 — 무한 성장 방지 (champion manifest 가 어차피
                    # 2차 필터라 오래된 항목은 중복이다).
                    _mark_state(last_event_pm=sorted(
                        set(_state().get("last_event_pm") or []) | set(new_pm)
                    )[-EVENT_PM_MEMORY:])
                    handle_trigger(p, now=now, trigger="loud_pm_event", tag=a.tag or "evtpm")
        except Exception as e:                               # noqa: BLE001 — 루프는 죽지 않는다
            log.exception("루프 반복 실패 — %ss 후 재시도: %s", backoff, e)
            time.sleep(backoff)
        for _ in range(max(1, int(poll))):                   # 종료 신호 반응성 확보
            if not running:
                break
            time.sleep(1)
    log.info("종료 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
