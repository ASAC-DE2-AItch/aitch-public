# -*- coding: utf-8 -*-
"""CT²(AE) 배포 — 파생 thread 승인 그래프 + 자동 promote (배선 설계 v2 D3, 2026-07-30).

**2모드 (헌법 3-3 ③ · 계약 §8-E)** — 어느 경로든 `validate_bundle` 전 항목 PASS가 선행:
  · `--auto-promote` (`ct.ct2_auto_promote: true`) — 승인 화면 미경유. 그래프·interrupt 없이
    `approval_records`(Approved / approver='ct2_auto_gate' / 사유에 게이트 리포트 경로) 기록 후
    promote 신호 드롭. 인간 개입의 근거는 **상류 R9 승인**이다 (감사는 면제되지 않는다 — 1-1).
  · 기본 (`false`) — 아래 파생 thread 미니 그래프로 사람 승인. 게이트 FAIL 후 **엔지니어 발의
    수동 재요청**도 이 경로다 (FAIL 상태 번들의 자동 오픈은 금지 — 1-4 역할 조정).

왜 별도 그래프인가: 본편 Incident 그래프(approval_graph)는 R9 종결에서 END에 도달했고,
종결 thread의 연장·재invoke는 불가/복잡 판정(C 확인 7/29). CT² 배포 승인은 **모델 게이트
승인**으로 분류해 파생 thread(`<incident_id>-CT2`)에서 **새 그래프를 일반 invoke** 한다
(헌법 1-4 개정 각주). 감사 정본은 `approval_records`(incident_id 참조 — 1-1).

구조:  gate(PENDING 오픈+interrupt) ──approve──▶ promote(신호 파일+기록) ─▶ END
                                    └─reject───▶ reject(보존·미배포 기록) ─▶ END
  · PENDING 오픈은 **Incident 기준 동시 1건 체크** 포함 (파생이어도 원 incident_id로 검사).
  · 본편 store.open_pending과 달리 **incidents.lifecycle을 건드리지 않는다** — 종결된
    Incident를 되살리면 P6-1 lifecycle 상태기계·사이클 정의(종결=R9 완료)와 충돌하므로.
  · promote = 배포 "실행"이 아니라 **관리형 리로드 신호** 드롭(`control/ct2/promote_*.json`)
    — consumer가 신호를 보고 스스로 번들 재적재+DriftTracker 리셋 (InjectControl 패턴).
  · modify는 불허 (번들은 수정 대상이 아님 — approve/reject 2지선다. gateway에서 400).

CLI (오케스트레이터가 subprocess로 호출 — 소유권 경계):
    python -m src.orchestrator.ct2_deploy_approval INC-... --bundle models/anomaly_ae/ae_ct2_... \
        --report models/anomaly_ae/ae_ct2_.../validation.json [--auto-promote]
  ※ `--auto-promote` 는 리포트의 `gate_pass: true` 를 **여기서 재확인**한 뒤에만 진행한다
     (호출자 신뢰만으로 배포하지 않는 이중 안전망).
resume은 gateway `POST /approvals/{incident_id}` — pending row의 request_type='ct2_deploy'를
보고 ct2 그래프 + thread `<incident_id>-CT2`로 라우팅한다 (main.py 패치 참조).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUEST_TYPE = "ct2_deploy"
PROMOTE_DIR = REPO_ROOT / "control" / "ct2"                  # consumer 리로드 신호 채널
THREAD_SUFFIX = "-CT2"                                       # 파생 thread 규약 (1-4 개정)
AUTO_APPROVER = "ct2_auto_gate"                              # 자동 promote 감사 주체 (3-3 ③ ⓒ)
AUTO_APPROVER_ROLE = "system"
STATUS_APPROVED = "APPROVED"                                 # 대문자 통일 — approval_graph.ACTION_STATUS
STATUS_REJECTED = "REJECTED"                                 #   정합 (혼합 케이싱은 감사 조회 누락 원인)
# validate_bundle 리포트 식별자. 조립 meta.json 에도 `gate_pass` 키가 있어(표본 게이트)
# 그것만 보면 검증을 건너뛴 배포가 통과한다 — 발급자까지 확인한다.
GATE_SOURCE_PREFIX = "ae_pipeline/validate_bundle.py"
SELECTED_OPTION_MAX = 32                                     # approval_records.selected_option 폭

log = logging.getLogger("ct2_deploy_approval")


class Ct2State(TypedDict, total=False):
    """파생 thread 상태 — 체크포인터 저장 대상 (thread_id=<incident_id>-CT2)."""
    incident_id: str
    bundle: str
    report: dict
    decision: Optional[str]
    reason: Optional[str]
    approver: Optional[str]


def thread_id_for(incident_id: str) -> str:
    """파생 thread ID (1-4 개정 규약의 단일 소스)."""
    return incident_id + THREAD_SUFFIX


def ct_id_for(incident_id: str) -> str:
    """ct_decisions 연동 키 — agent_a ct2_orchestrator.ct_id_for와 동일 규약 (중복 정의는
    소유권 경계상 의도적: 프로세스 간 import 없음. 값 규약만 계약 §8-E로 고정)."""
    return f"CT-{incident_id[4:]}-AE" if incident_id.startswith("INC-") else f"CT-{incident_id}-AE"


# ── DB 헬퍼 (pool 주입 — 본편 ApprovalStore 미사용: lifecycle 무접촉 원칙) ────────

def _is_unique_violation(exc: BaseException) -> bool:
    """예외가 Postgres UniqueViolation(SQLSTATE 23505)인가 — 라이브러리 무의존 판별.

    `approval_graph._is_unique_violation` 과 같은 로직을 **의도적으로 중복**한다
    (`ct_id_for` 와 같은 이유): 이 모듈은 orchestrator 내부를 import 하지 않는 CLI 진입점이고,
    psycopg2 를 isinstance 로 끌어오면 langgraph 지연 import 로 지켜온 테스트 격리가 깨진다.
    판별은 `pgcode` 속성으로, 래핑된 예외는 원인 체인(__cause__)을 한 겹까지 본다.
    """
    for e in (exc, getattr(exc, "__cause__", None)):
        if e is not None and getattr(e, "pgcode", None) == "23505":
            return True
    return False


def open_ct2_pending(conn, incident_id: str, bundle: str, report: dict) -> bool:
    """PENDING 오픈 (멱등 + Incident 기준 동시 1건). incidents.lifecycle 무접촉.

    멱등 방어는 2층이다 (2026-08-03 — `db/migrations/0001` **적용 후속**. 본편
    `approval_graph.open_pending` 과 같은 규약이고, 0001 의 ③ 정합 메모가 요구한 환원이다):
      ① SELECT 선검사 — 순차 재실행/단일 프로세스를 거르는 통상 경로
      ② UniqueViolation(23505) 흡수 — ①과 INSERT 사이의 **경합 창**은 SELECT 로 못 막는다
         (두 호출이 동시에 "없음"을 보고 둘 다 INSERT). `uq_approval_pending_per_incident`
         가 걸린 지금, 진 쪽이 받는 예외는 실패가 아니라 "다른 요청이 이미 열었다" = False 다.
         **인덱스를 걸어놓고 이 환원을 안 하면 인덱스 도입 전보다 나쁘다** — 경합에서 진
         호출이 예외로 터져 CT² 승인 노드가 통째로 죽는다. 인덱스 없는 DB 에서는 무해하다.

    **커넥션 위생 — 이쪽이 더 큰 구멍이었다.** 이 conn 은 `build_ct2_graph` 가 pool 에서
    빌려 `finally: putconn` 으로 되돌린다. 예외가 그대로 나가면 **중단(aborted)된 트랜잭션
    상태의 커넥션이 pool 로 돌아가** 다음 대여자가 첫 쿼리에서 InFailedSqlTransaction 을
    맞는다 — CT² 바깥의 게이트웨이 DB 작업까지 같이 죽는 자리였다. 그래서 **모든 이탈
    경로**에 commit 또는 rollback 을 남긴다. SELECT 만 하고 나가는 False 경로도 암묵
    트랜잭션을 열므로 rollback 대상이다 (`approval_graph.read_conn` 과 같은 반납 위생 규약).

    번들명 폭 선검사도 여기서 한다 — 자동 promote 경로(`auto_promote`)에는 있는데 승인
    그래프 경로에는 없어서, 32자 초과 번들이면 승인 한복판에 22001 DataError 로 터졌다.
    """
    name = Path(bundle).name
    if len(name) > SELECTED_OPTION_MAX:
        raise RuntimeError(
            f"CT² PENDING 오픈 거부 — 번들명 {len(name)}자 > "
            f"{SELECTED_OPTION_MAX} (approval_records.selected_option 폭 초과)")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM approval_records WHERE incident_id=%s AND status='PENDING'",
                        (incident_id,))
            if cur.fetchone():
                conn.rollback()                              # SELECT 암묵 트랜잭션 정리 후 반납
                return False                                 # 동시 pending 1건 (Incident 기준)
            cur.execute(
                """INSERT INTO approval_records
                       (incident_id, request_type, status, selected_option, original_value)
                   VALUES (%s, %s, 'PENDING', %s, %s::jsonb)""",
                (incident_id, REQUEST_TYPE, name,
                 json.dumps({"bundle": bundle, "report": report}, ensure_ascii=False)))
    except Exception as exc:                                 # noqa: BLE001 — 23505 만 흡수
        conn.rollback()
        if _is_unique_violation(exc):
            log.info("CT² PENDING 경합 — 다른 요청이 선점 (incident=%s, 23505 흡수)", incident_id)
            return False
        raise
    conn.commit()
    return True


def close_ct2_pending(conn, incident_id: str, status: str, reason: Optional[str],
                      approver: Optional[str], role: Optional[str],
                      commit: bool = True) -> bool:
    """PENDING → Approved/Rejected (ct2_deploy 행 한정, 멱등 가드).

    커넥션 위생(예외 시 rollback·반납)은 **호출자 책임** — 그래프 `_put`·CLI `finally` 담당
    (#100 리뷰 ① — pool 커넥션으로 직접 부를 다음 사람을 위한 규약 명시).

    `commit=False` 면 커밋하지 않는다 — 호출자가 후속 쓰기(`mark_ct_decision`)와 **한
    트랜잭션으로 묶기** 위한 스위치다 (#100 리뷰 ② 후속). 기존 호출자는 기본값으로
    거동 무변경.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE approval_records
                  SET status=%s, decision_reason=%s, approver=%s, approver_role=%s,
                      decided_at=NOW()
                WHERE incident_id=%s AND request_type=%s AND status='PENDING'""",
            (status, reason, approver, role, incident_id, REQUEST_TYPE))
        won = cur.rowcount > 0
    if commit:
        conn.commit()
    return won


def validate_gate_report(report, bundle: str, report_path: Optional[str]) -> Optional[str]:
    """자동 promote 전 게이트 리포트 검증. 거부 사유 문자열 또는 None(통과).

    호출자(오케스트레이터) 신뢰만으로 배포하지 않는 **이중 안전망**이며, 다음 3가지를
    각각 막는다:
      ① **다른 문서로 통과** — 조립 `meta.json` 에도 `gate_pass`(표본 수 게이트) 키가 있어
         그것만 보면 `validate_bundle` 미실행 배포가 통과한다 → `gate_source` 발급자 확인.
      ② **리포트 재사용** — 이전 번들의 PASS 리포트로 새 번들 배포 → `bundle_name` 대조.
      ③ **조용한 빈 리포트** — 파일 부재 시 `{}` 로 진행하던 경로 → 명시 거부.
    `failed_checks` 도 함께 본다 (`gate_pass` 와 이중 확인 — 한쪽만 조작된 리포트 방어).
    """
    if not isinstance(report, dict) or not report:
        return f"검증 리포트를 읽을 수 없음 (--report={report_path or '미전달'})"
    src = str(report.get("gate_source", ""))
    if not src.startswith(GATE_SOURCE_PREFIX):
        return (f"검증 리포트가 아님 — gate_source={src!r} "
                f"(기대 접두 {GATE_SOURCE_PREFIX!r}). 조립 meta.json 은 게이트 근거가 아니다")
    if report.get("gate_pass") is not True or report.get("failed_checks"):
        return f"게이트 미통과 — failed={report.get('failed_checks')}"
    got, want = report.get("bundle_name"), Path(bundle).name
    if got != want:
        return f"리포트-번들 불일치 — 리포트={got!r} 배포대상={want!r} (재사용 방어)"
    return None


def r9_approval_exists(conn, incident_id: str) -> bool:
    """상류 R9(`request_type='requalify'`) 승인 실존 — 헌법 1-1 예외 3 ⓒ.

    자동 promote 에서 **인간 개입의 유일한 근거**가 이 승인이다. 오케스트레이터의 가드
    g3 가 같은 검사를 하지만, 이 모듈은 CLI 로 직접 호출될 수 있어 여기서도 확인한다
    (게이트 리포트를 재검증하는 것과 같은 이유 — 호출 경로를 신뢰하지 않는다).
    커넥션 위생은 호출자 책임 (#100 리뷰 ①).
    """
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM approval_records
                       WHERE incident_id=%s AND request_type='requalify'
                         AND UPPER(status)=%s LIMIT 1""", (incident_id, STATUS_APPROVED))
        return cur.fetchone() is not None


def record_auto_approval(conn, incident_id: str, bundle: str, report_path: Optional[str],
                         report: dict) -> bool:
    """자동 promote 감사 기록 — `approval_records` APPROVED 행 1건 (헌법 3-3 ③ ⓒ).

    사람 승인이 없어도 **감사 추적은 면제되지 않는다**(1-1 예외 3). 인간 개입의 근거는
    상류 R9(`request_type='requalify'`) 승인이며, 본 행의 `decision_reason` 에 게이트
    리포트 경로를 남겨 "무엇을 근거로 배포됐는지"를 추적 가능하게 만든다.

    멱등 범위는 **같은 번들의 자동 승인 행**으로 좁힌다 — `request_type` 만 보면 이미
    Rejected(엔지니어 거부)나 PENDING 행이 있을 때 감사 행 없이 배포가 진행된다.
    거부·대기 이력 위의 자동 배포는 아예 막는다(호출자가 RuntimeError 를 그대로 올린다).

    Returns:
        새 행을 만들었으면 True, 같은 번들의 자동 승인 행이 이미 있으면 False.

    Raises:
        RuntimeError: 같은 Incident 에 Rejected/PENDING `ct2_deploy` 행이 존재.

    커넥션 위생은 호출자 책임 (#100 리뷰 ①).
    """
    name = Path(bundle).name
    with conn.cursor() as cur:
        cur.execute(
            """SELECT status, approver, selected_option FROM approval_records
                WHERE incident_id=%s AND request_type=%s""", (incident_id, REQUEST_TYPE))
        rows = cur.fetchall()
        for status, approver, option in rows:
            st = (status or "").upper()
            if st == "PENDING":
                raise RuntimeError(
                    f"{incident_id}: ct2_deploy PENDING 행 존재 — 사람 승인 대기 중인 건을 "
                    "자동 배포로 덮지 않는다 (헌법 1-4 동시 pending 1건)")
            if st == STATUS_REJECTED:
                raise RuntimeError(
                    f"{incident_id}: ct2_deploy 거부 이력 존재 — 거부 위의 자동 배포 금지. "
                    "재배포는 엔지니어 발의(파생 thread)로만 (헌법 1-4 역할 조정)")
            if st == STATUS_APPROVED and approver == AUTO_APPROVER and option == name:
                return False                                 # 같은 번들 재수신 — 멱등
        cur.execute(
            """INSERT INTO approval_records
                   (incident_id, request_type, status, selected_option, original_value,
                    decision_reason, approver, approver_role, decided_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, NOW())""",
            (incident_id, REQUEST_TYPE, STATUS_APPROVED, name,
             json.dumps({"bundle": bundle, "report": report}, ensure_ascii=False),
             f"자동 검증 게이트 PASS (R9 동의 승계) — 리포트: {report_path or '경로 미전달'}",
             AUTO_APPROVER, AUTO_APPROVER_ROLE))
    conn.commit()
    return True


def mark_ct_decision(conn, incident_id: str, status: str, bundle: Optional[str],
                     commit: bool = True) -> bool:
    """ct_decisions 상태 전이 (PROMOTED/AUTO_PROMOTED/REJECTED — 3-3 ② ⓒ 기록).

    오케스트레이터가 처리 시작 시 RUNNING 행을 선점(claim_running)하므로 이 UPDATE는
    정상적으로 1행을 맞춘다. 0행이면 선점이 없었다는 뜻(수동 CLI 직접 호출 등)이라
    경고한다 — 오케스트레이터 finalize_decision이 사후 재확인하지만, 단독 호출 경로에서는
    행이 없으면 번들명이 남지 않으니 신호로 남긴다.

    Returns:
        갱신된 행이 있으면 True.

    커넥션 위생은 호출자 책임 (#100 리뷰 ①). `commit=False` 는 선행 쓰기와 한 트랜잭션으로
    묶기 위한 스위치다 (close_ct2_pending 참조).
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE ct_decisions SET retrain_status=%s, model_version_after=%s
                WHERE ct_id=%s""",
            (status, Path(bundle).name if bundle else None, ct_id_for(incident_id)))
        hit = cur.rowcount > 0
    if commit:
        conn.commit()
    if not hit:
        log.warning("ct_decisions 갱신 0행 (ct_id=%s) — 행이 아직 없다면 호출자가 "
                    "status=%s·번들명으로 INSERT 해야 한다", ct_id_for(incident_id), status)
    return hit


def promote_signal_path(incident_id: str) -> Path:
    """관리형 리로드 신호 파일 경로 — 드롭(생성)과 복구 판정(드롭 이력 확인)이 같은 정의를 쓴다."""
    return PROMOTE_DIR / f"promote_{incident_id}.json"


def promote_signal_dropped(incident_id: str) -> bool:
    """신호를 **한 번이라도 떨궜는가** — 원본 자리 + processed/ + failed/ 3곳 스캔.

    consumer 가 신호를 소비하면 processed/ 로, 로드 실패면 failed/ 로 옮기므로
    (consumer._check_ct2_promote) 원본 자리만 보면 **정상 완료의 몇 초 뒤도 '없음'**이
    된다 (#100 리뷰 ③). 롤백 뒤에도 processed/ 에 원본이 남아 있어 롤백 번들 되밀기를
    막는다. failed/ 도 '떨궜음'으로 센다 — 같은 번들 재드롭은 또 실패할 뿐이라 복구가
    아니라 별도 에스컬레이션 건이다.
    """
    name = promote_signal_path(incident_id).name
    return any((PROMOTE_DIR / sub / name).exists() if sub else (PROMOTE_DIR / name).exists()
               for sub in ("", "processed", "failed"))


def approved_ct2_exists(conn, incident_id: str) -> bool:
    """이 Incident 의 ct2_deploy APPROVED 행 실존 — promote 부분 실패 복구 판정 재료.

    커넥션 위생은 호출자 책임 (#100 리뷰 ①).
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM approval_records
                WHERE incident_id=%s AND request_type=%s AND UPPER(status)=%s LIMIT 1""",
            (incident_id, REQUEST_TYPE, STATUS_APPROVED))
        return cur.fetchone() is not None


def _promote_recovery_needed(conn, incident_id: str) -> bool:
    """promote 부분 실패(①② 커밋 후 ③ 신호 드롭 실패) 복구 필요 판정.

    close_ct2_pending 이 0행(False)일 때만 부른다. 「APPROVED 인데 신호를 **한 번도
    떨군 적 없다**」 = 이전 시도가 ③에서 죽었다는 뜻 — ②③④를 재수행하면 복구된다
    (auto_promote 와 대칭·멱등). 판정 재료는 신호 '실존'이 아니라 **드롭 이력 3곳**
    (promote_signal_dropped) — consumer 가 처리 후 파일을 옮기므로 실존만 보면
    정상 완료·로드 실패(failed/)·자동 롤백 후를 전부 '복구 필요'로 오판해 재드롭한다
    (#100 리뷰 ③ — 특히 롤백 번들 되밀기가 최악).

    ⚠️ **현재 이 판정에 도달하는 호출 경로가 없다** (#100 리뷰 ② — ⓒ 채택): 게이트웨이
    가드 ①(PENDING 0행 → 409)이 부분 실패 상태의 재시도를 선차단한다. 배선은 승인 경로
    밖 **주기 점검(리뷰 ② ⓑ)**으로 후속 — 인프라 스프린트 WP. 가드에 예외를 두는 ⓐ는
    방금 굳힌 유령 성공 차단을 재수술하는 리스크라 기각(ⓑ가 무재시도 크래시까지 커버해
    상위호환). pending_found=None(fail-open)의 좁은 경로에서만 실행될 수 있으며, 그
    경우에도 본 판정은 드롭 이력 기준이라 안전하다.
    PENDING 되돌리기(전 라운드 ⓑ)는 기각 유지 — 시스템이 승인 기록을 역전시키면 감사
    추적(1-1)이 혼탁해진다. 복구는 언제나 앞으로만 간다.
    """
    return approved_ct2_exists(conn, incident_id) and not promote_signal_dropped(incident_id)


def drop_promote_signal(incident_id: str, bundle: str) -> Path:
    """관리형 리로드 신호 파일 드롭 — consumer가 감지해 번들 재적재+Drift 리셋."""
    PROMOTE_DIR.mkdir(parents=True, exist_ok=True)
    sig = promote_signal_path(incident_id)
    tmp = sig.with_suffix(".json.tmp")                       # 원자 교체 (부분 읽기 방지)
    tmp.write_text(json.dumps({
        "incident_id": incident_id,
        "bundle": str(Path(bundle).resolve()),           # 절대경로 — consumer cwd 무관 (스모크 교훈)
        "ct_id": ct_id_for(incident_id),
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sig)
    return sig


def append_changelog(incident_id: str, bundle: str, approver: Optional[str],
                     report: Optional[dict] = None, auto: bool = False) -> None:
    """models/CHANGELOG.md 승격 기록 (헌법 3-3·4-1 — 버전·지표·사유).

    3-3 은 "버전·RMSE·변경 사유", 4-1 은 "+ 피처 수"를 요구한다 — 게이트 리포트에 이미
    있는 수치를 함께 남긴다 (없으면 '-'). 실패해도 배포 흐름은 계속하고 로그만 남긴다.
    """
    try:
        m = ((report or {}).get("metrics") or {})
        route = "자동 게이트 PASS" if auto else "파생 thread 승인"
        rpt = (report or {}).get("bundle_name")
        line = (f"\n- {datetime.now(timezone.utc):%Y-%m-%d} CT²(AE) promote: "
                f"`{Path(bundle).name}` — incident {incident_id} · {route} · "
                f"승인 {approver or '-'} · recon RMSE {m.get('recon_rmse', '-')} · "
                f"L5 {m.get('l5', '-')} · VAL {m.get('n_val', '-')}장"
                f"(B군 표본 {m.get('b_group_sample', '-')}) · "
                f"사유: 요란 PM 후 신규 기준선 레짐 재학습 "
                f"(검증 리포트 = <bundle>/validation.json{f', {rpt}' if rpt else ''})\n")
        with open(REPO_ROOT / "models" / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:                                   # noqa: BLE001
        log.warning("CHANGELOG 기록 실패(흐름 계속): %s", e)


# ── 자동 promote (그래프 미경유 — 헌법 3-3 ③) ───────────────────────────────

def auto_promote(conn, incident_id: str, bundle: str, report_path: Optional[str],
                 report: dict) -> Path:
    """게이트 PASS 시 사람 승인 없이 배포. 드롭한 신호 파일 경로 반환.

    승인 그래프(interrupt)를 타지 않는다 — pending 이 생기지 않으므로 "동시 pending 1건"
    (1-4)과 무관하고, 종결 Incident 의 lifecycle 에도 무접촉이다.

    호출자가 보장하는 것은 ⓐ `ct.ct2_auto_promote: true` 하나뿐이다. 헌법 1-1 예외 3의
    나머지 조건 — ⓑ 게이트 PASS · ⓒ 상류 R9 승인 실존 · ⓓ 거부·대기 이력 부재 — 은
    **전부 이 함수에서 재확인**한다 (호출 경로를 신뢰하지 않는 이중 안전망).

    Raises:
        RuntimeError: 게이트 리포트 검증 실패, R9 승인 부재, 또는 거부·대기 이력 존재.
    """
    reason = validate_gate_report(report, bundle, report_path)
    if reason:
        raise RuntimeError(f"자동 promote 거부 — {reason} (헌법 3-3 ③ ⓑ)")
    if len(Path(bundle).name) > SELECTED_OPTION_MAX:
        raise RuntimeError(
            f"자동 promote 거부 — 번들명 {len(Path(bundle).name)}자 > "
            f"{SELECTED_OPTION_MAX} (approval_records.selected_option 폭 초과)")
    if not r9_approval_exists(conn, incident_id):
        raise RuntimeError(
            f"자동 promote 거부 — {incident_id} 의 R9(requalify) 승인 없음. "
            "자동 배포의 인간 개입 근거가 부재하다 (헌법 1-1 예외 3 ⓒ)")
    made = record_auto_approval(conn, incident_id, bundle, report_path, report)
    mark_ct_decision(conn, incident_id, "AUTO_PROMOTED", bundle)
    sig = drop_promote_signal(incident_id, bundle)
    append_changelog(incident_id, bundle, AUTO_APPROVER, report, auto=True)
    log.info("AUTO_PROMOTED %s → 리로드 신호 %s (감사 행 %s)",
             Path(bundle).name, sig.name, "신규" if made else "기존 유지(멱등)")
    return sig


# ── 그래프 ─────────────────────────────────────────────────────────────────

def build_ct2_graph(pool, checkpointer):
    """미니 그래프 컴파일 — gateway startup·CLI 공용. checkpointer는 caller 관리(본편 규약)."""
    from langgraph.graph import StateGraph, START, END       # 지연 import
    from langgraph.types import interrupt

    def _conn():
        return pool.getconn()

    def _put(c):
        # 반납 위생 (2026-08-03). 성공 경로는 이미 commit 되어 rollback 이 무해하고,
        # 예외로 빠져나온 경로는 여기서 중단된 트랜잭션을 정리한다. 이게 없으면 aborted
        # 상태의 커넥션이 pool 로 돌아가 **다음 대여자**가 첫 쿼리에서
        # InFailedSqlTransaction 을 맞는다 — CT² 밖 게이트웨이 DB 작업까지 같이 죽는다.
        ok = True
        try:
            c.rollback()
        except Exception:                                    # noqa: BLE001 — 반납은 무조건 한다
            ok = False
            log.warning("커넥션 반납 전 rollback 실패 — 폐기 반납한다(close=True, #100 리뷰 ③)",
                        exc_info=True)
        pool.putconn(c, close=not ok)

    def gate(s: Ct2State) -> dict:
        c = _conn()
        try:
            opened = open_ct2_pending(c, s["incident_id"], s.get("bundle", ""),
                                      s.get("report", {}))
            log.info("CT² 배포 승인 %s — PENDING %s", s["incident_id"],
                     "오픈" if opened else "기존 유지(멱등)")
        finally:
            _put(c)
        rv = interrupt({"type": "approval", "incident_id": s["incident_id"],
                        "report": {"request_type": REQUEST_TYPE, "bundle": s.get("bundle"),
                                   "meta": s.get("report", {})}})
        rv = rv or {}
        return {"decision": rv.get("action"), "reason": rv.get("reason"),
                "approver": rv.get("approver")}

    def promote(s: Ct2State) -> dict:
        # 부분 실패 자기치유 (#100 리뷰 ② ⓐ): ①close(커밋)·②mark(커밋) 뒤 ③신호 드롭이
        # 죽으면 재시도의 close 는 0행이다 — 그때 「APPROVED + 신호 부재」면 ②③④를 재수행
        # 한다(auto_promote 와 대칭). 신호를 먼저 떨구는 순서 역전은 금지 — DB 닫기 실패 시
        # "승인 안 났는데 배포됨"(1-1 위반)이 된다 (C 리뷰 의견 채택).
        c = _conn()
        try:
            # ①② 단일 트랜잭션 (#100 리뷰 ② 후속): 구현은 각각 커밋해서, ①만 커밋되고
            # ②가 죽으면 "APPROVED 인데 ct_decisions 미갱신"이 고착됐다(재시도는 ①이 0행).
            # 한 트랜잭션으로 묶으면 ② 실패 시 ①도 함께 풀려 **PENDING 이 유지**되고
            # 재승인이 정상 동작한다. 이중 쓰기의 참여자를 3자(①②③)에서 2자(①②|③)로 줄인
            # 것이며, 남는 ③ 경계는 주기 점검(reconciler)이 수렴시킨다.
            won = close_ct2_pending(c, s["incident_id"], STATUS_APPROVED, s.get("reason"),
                                    s.get("approver"), None, commit=False)
            if not won and not _promote_recovery_needed(c, s["incident_id"]):
                return {}                                    # 중복 resume 등 — 종전대로 무시
                                                             #   (_put 이 rollback 하므로 안전)
            if not won:
                log.warning("promote 부분 실패 복구 — APPROVED 인데 신호 파일 부재, ②③④ 재수행 "
                            "(incident=%s)", s["incident_id"])
            mark_ct_decision(c, s["incident_id"], "PROMOTED", s.get("bundle"), commit=False)
            c.commit()                                       # ①② 원자 확정
            sig = drop_promote_signal(s["incident_id"], s.get("bundle", ""))
            append_changelog(s["incident_id"], s.get("bundle", ""), s.get("approver"),
                             s.get("report"))
            log.info("PROMOTED %s → 리로드 신호 %s", s.get("bundle"), sig.name)
        finally:
            _put(c)
        return {}

    def reject(s: Ct2State) -> dict:
        c = _conn()
        try:
            # promote 와 동일하게 ①② 원자 처리 — 반려도 "기록은 REJECTED 인데
            # ct_decisions 는 그대로"인 반쪽 상태를 만들면 안 된다.
            if close_ct2_pending(c, s["incident_id"], STATUS_REJECTED, s.get("reason"),
                                 s.get("approver"), None, commit=False):
                mark_ct_decision(c, s["incident_id"], "REJECTED", s.get("bundle"), commit=False)
                c.commit()
                log.info("REJECTED — challenger 보존·미배포 (%s)", s.get("bundle"))
        finally:
            _put(c)
        return {}

    g = StateGraph(Ct2State)
    g.add_node("gate", gate)
    g.add_node("promote", promote)
    g.add_node("reject", reject)
    g.add_edge(START, "gate")
    g.add_conditional_edges("gate", lambda s: "promote" if s.get("decision") == "approve"
                            else "reject", {"promote": "promote", "reject": "reject"})
    g.add_edge("promote", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=checkpointer)


def main() -> None:
    """CLI — 승인 요청 오픈(기본) 또는 자동 promote(`--auto-promote`).

    기본 경로는 interrupt에서 멈춤 = PENDING, resume은 gateway 몫.
    `--auto-promote` 는 게이트 PASS 전제하에 그래프를 타지 않고 바로 배포한다 (3-3 ③).
    """
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="CT2 배포 — 승인 오픈(파생 thread) 또는 자동 promote")
    ap.add_argument("incident_id")
    ap.add_argument("--bundle", required=True, help="challenger 번들 폴더")
    ap.add_argument("--report", default=None,
                    help="검증 리포트 JSON (validate_bundle 산출 `validation.json`). "
                         "승인 경로에서는 조립 meta.json 도 참고자료로 허용하지만, "
                         "--auto-promote 에는 **검증 리포트만** 유효하다")
    ap.add_argument("--auto-promote", action="store_true",
                    help="게이트 PASS 전제 자동 배포 (승인 화면 미경유 — 헌법 3-3 ③). "
                         "리포트 발급자·번들명·failed_checks 를 여기서 재검증한다")
    a = ap.parse_args()
    if not a.incident_id.startswith("INC-"):
        raise SystemExit("incident_id는 INC- 형식")
    if not Path(a.bundle).exists():
        raise SystemExit(f"번들 폴더 없음: {a.bundle}")
    # `json.loads` 성공 ≠ dict ("123"·[1,2]·null 전부 유효 JSON) — §7 명시 금지 항목
    report: dict = {}
    if a.report and Path(a.report).exists():
        parsed = json.loads(Path(a.report).read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise SystemExit(f"--report 가 JSON 객체가 아님: {type(parsed).__name__}")
        report = parsed

    # 자동 promote 는 게이트 리포트를 **여기서도 검증**한다 — 호출자 신뢰만으로 배포하지
    # 않는다 (이중 안전망). 조립 meta.json 에도 `gate_pass` 키가 있어 발급자까지 본다.
    if a.auto_promote:
        reason = validate_gate_report(report, a.bundle, a.report)
        if reason:
            raise SystemExit(f"자동 promote 거부 — {reason} (헌법 3-3 ③ ⓑ)")

    import psycopg2
    from psycopg2.pool import ThreadedConnectionPool
    from langgraph.checkpoint.postgres import PostgresSaver
    from dotenv import load_dotenv
    load_dotenv()
    dsn = os.environ["DATABASE_URL"]

    chk = psycopg2.connect(dsn)                              # INC 실존 가드 (7/26 사고 재발 방지)
    try:
        with chk.cursor() as cur:
            cur.execute("SELECT 1 FROM incidents WHERE incident_id=%s", (a.incident_id,))
            if cur.fetchone() is None:
                raise SystemExit(f"Incident 미존재: {a.incident_id} — 오픈하지 않음 (유령 방지)")
        if a.auto_promote:                                    # 그래프·체크포인터 불요 경로
            try:
                auto_promote(chk, a.incident_id, a.bundle, a.report, report)
            except RuntimeError as e:                          # 거부·대기 이력 등 → 비-0 종료
                raise SystemExit(str(e))
            return
    finally:
        chk.close()

    pool = ThreadedConnectionPool(1, 2, dsn)
    try:
        with PostgresSaver.from_conn_string(dsn) as cp:
            cp.setup()
            app = build_ct2_graph(pool, cp)
            app.invoke({"incident_id": a.incident_id, "bundle": a.bundle, "report": report},
                       {"configurable": {"thread_id": thread_id_for(a.incident_id)}})
            print(f"PENDING 오픈: {a.incident_id} (thread={thread_id_for(a.incident_id)}) "
                  f"— resume은 POST /approvals/{a.incident_id}")
    finally:
        pool.closeall()


if __name__ == "__main__":
    main()
