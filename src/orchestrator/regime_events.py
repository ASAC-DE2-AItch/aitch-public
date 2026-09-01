# -*- coding: utf-8 -*-
"""P6-1 선행 — 레짐 이벤트 2종 빌더 (계약 §8-B·§8-C, kafka·fastapi 무의존).

QualVerdictConfirmed  : 조용/요란 판정 확정 방송 — 레짐 스위치의 단일 소스 (§8-B).
                        소비: A(is_post_loud_pm·bias 리셋) / B(가한계 Phase 전환) / Dashboard(S7).
ChamberRequalified    : 재인증 완료 경계 — firm corrections 발행이 모두 끝난 뒤 마지막 1회 (§8-C).
                        소비: B(verify 30장 경계·y_thresholds 1회 write) / Dashboard(F8 종결).

원칙:
  · 빌더는 검증+조립만 — 발행은 게이트웨이/그래프의 emit 훅 몫 (events.route_topic이
    두 이벤트 모두 기본 토픽 `fdc.agent`로 라우팅).
  · ChamberRequalified 멱등 키 = incident_id (Incident당 1회) — 발행측 가드는
    RequalOnceGuard(프로세스 수명 in-memory). 영구 멱등은 P6-1에서 Incident 종결
    상태(DB)와 연동해 완성한다 (계약 §8-C).
  · 확정 verdict의 영속화(quals ALTER)는 P6-1 — 그 전까지 이벤트가 유일 소스 (§8-B 각주).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("regime-events")

VERDICTS = {"loud", "quiet"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_qual_verdict(qual_id: str, chamber_id: str, verdict: str,
                       pm_count: int, approved_by: str,
                       incident_id: Optional[str] = None,
                       confirmed_at: Optional[str] = None) -> dict:
    """QualVerdictConfirmed 페이로드 조립 (§8-B). 위반 시 ValueError.

    규칙: verdict ∈ {loud, quiet} / **loud면 incident_id 필수**(레짐 전환 Incident —
    신규 결정 10), quiet면 null 허용 / qual_id는 QUAL- 프리픽스 (6-4 ID 포맷).
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict는 loud/quiet만 허용: {verdict!r}")
    if verdict == "loud" and not incident_id:
        raise ValueError("요란(loud) 확정에는 incident_id 필수 — 레짐 전환 Incident (신규 결정 10)")
    if not str(qual_id).startswith("QUAL-"):
        raise ValueError(f"qual_id 포맷 위반 (QUAL-...): {qual_id!r}")
    if not chamber_id:
        raise ValueError("chamber_id 누락")
    if not approved_by:
        raise ValueError("approved_by 누락 — 감사 추적 필수 (헌법 1-1)")
    return {
        "qual_id": qual_id,
        "chamber_id": chamber_id,
        "verdict": verdict,
        "incident_id": incident_id if verdict == "loud" else None,
        "pm_count": int(pm_count),
        "confirmed_at": confirmed_at or _now_iso(),
        "approved_by": approved_by,
    }


def build_chamber_requalified(incident_id: str, chamber_id: str, qual_id: str,
                              pm_count: int, corrections_applied: list,
                              limit_version: str, approved_by: str,
                              requalified_at: Optional[str] = None) -> dict:
    """ChamberRequalified 페이로드 조립 (§8-C). 위반 시 ValueError.

    규칙: incident_id 필수(멱등 키) / corrections_applied는 리스트(B 채번 승계 —
    LIM- 프리픽스 검사, 빈 리스트는 경고만: R9 bias 단독 수립 케이스 허용).
    """
    if not incident_id:
        raise ValueError("incident_id 누락 — 멱등 키 (Incident당 1회)")
    if not chamber_id:
        raise ValueError("chamber_id 누락")
    if not str(qual_id).startswith("QUAL-"):
        raise ValueError(f"qual_id 포맷 위반 (QUAL-...): {qual_id!r}")
    if not isinstance(corrections_applied, list):
        raise ValueError("corrections_applied는 리스트여야 함 (firm correction ID 목록)")
    bad = [c for c in corrections_applied if not str(c).startswith(("LIM-", "RCP-"))]
    if bad:
        raise ValueError(f"correction ID 포맷 위반 (LIM-/RCP- 승계값만 — 헌법 6-4): {bad}")
    if not corrections_applied:
        log.warning(f"corrections_applied 빈 목록 — bias 단독 수립(R9) 케이스인지 확인 ({incident_id})")
    if not approved_by:
        raise ValueError("approved_by 누락 — 감사 추적 필수 (헌법 1-1)")
    return {
        "incident_id": incident_id,
        "chamber_id": chamber_id,
        "qual_id": qual_id,
        "pm_count": int(pm_count),
        "corrections_applied": list(corrections_applied),
        "limit_version": str(limit_version),
        "requalified_at": requalified_at or _now_iso(),
        "approved_by": approved_by,
    }


class RequalOnceGuard:
    """ChamberRequalified 발행측 멱등 가드 — incident_id당 1회 (프로세스 수명).

    P6-1에서 Incident 종결 상태(DB)와 연동해 영구화 — 그 전까지 데모 이중클릭 방어용.
    """

    def __init__(self):
        self._emitted: set[str] = set()

    def check_and_mark(self, incident_id: str) -> bool:
        """최초면 True(발행 진행), 재시도면 False(스킵 — 호출측 409)."""
        if incident_id in self._emitted:
            return False
        self._emitted.add(incident_id)
        return True

    def emitted(self, incident_id: str) -> bool:
        """이미 발행됐나 — **비파괴 조회** (2026-08-10 · S7 화면용).

        화면이 "R9 완료"를 로컬 state 로만 알고 있어서 새로고침하면 완료된 버튼이 다시
        [R9 재적격 실행]으로 되살아났다 (누르면 409). 이 조회로 화면이 발행측과 같은 답을
        보게 한다 — 가드가 프로세스 수명이라 **"여기서 True = 지금 재호출하면 409"** 가
        항상 성립한다. 게이트웨이를 재시작하면 가드와 화면이 함께 잊는다(= 재발행 가능,
        P6-1 에서 Incident 종결 상태로 영구화될 자리).
        """
        return incident_id in self._emitted
