# -*- coding: utf-8 -*-
"""ct2_deploy_monitor.py — CT²(AE) 배포 후 감시·자동 롤백 (WP-C / 헌법 3-3 ③ ⓔ).

**왜 필요한가**: 자동 검증 게이트(validate_bundle)는 배포 **전** 문지기이지만, 프로브는
합성 교란이라 실제 장비 이상과 모양이 다를 수 있다(대리 지표). 게이트를 통과한 번들이
실서빙에서 조용히 실명(失明)할 잔여 위험은 **배포 후 감시**가 갚는다. 헌법 3-3 ③ ⓓ가
`ct2_auto_promote: true`의 개통 조건 ②로 "감시·자동 롤백 실증 + Scorecard 미탐 0건"을 건
이유다 — 이 모듈이 그 실증 대상이다.

감시 2종 (설계 v2 §2 도표):
  ① **SPC↑ / AE 침묵 교차** — 창 K wafer에서 SPC(B의 Nelson 판정)가 이상을 잡았는데 같은
     wafer에서 AE가 조용(score < qual)한 비율. 새 번들이 SPC가 보는 걸 못 보면 = 미탐.
  ② **점수 분포 붕괴** — 배포 후 P95가 배포 전 대비 급락하거나 전 0 근접. 침묵화 신호.

**데이터 소스 (db/init.sql 정본 — H3 정합)**: `wafer_predictions` 단일 테이블.
  · 점수 = **`anomaly_score`** (계약 §2 `fdc.prediction` 필드명. consumer가 `ae_score`를 이
    이름으로 발행·적재한다 — `ae_score` 컬럼은 존재하지 않는다).
  · SPC 플래그 = **같은 행 `spc_flags`(JSONB)**. `spc_violations`에는 `wafer_id`가 없어
    (alert 단위) wafer 조인이 불가하다.

⚠️ **감시 ①의 현재 가동 조건 (A3-2 의존)**: `consumer.build_message`가 `spc_flags`를 아직
빈 배열로 발행한다(TODO A3-2 — Nelson 병렬 채널 미구현). 그동안 창 내 SPC 발화가 0건이라
**①은 판정 유예로 비활성**이고 ②만 유효하다. S3 개통 조건(감시 2종 실증)은 A3-2 착지 후에야
완결된다 — `_fetch_window`가 이 상태를 경고 로그로 노출한다.

발화 시 (`ct2_watch_auto_rollback: true`): **직전 정상 번들로 promote 신호 재드롭** — 배포와
같은 관리형 리로드 채널(§8-E, `control/ct2/promote_*.json`)을 재사용한다. 구 기준선 복귀 =
보수 방향이라 자동이 정당하다(헌법 3-3 ③ ⓔ). `ct_decisions(ROLLED_BACK)` + `approval_records`
(감사) + `CHANGELOG` 3중 기록. 재배포는 게이트 재통과 필수.

**소유·경계**: 본 파일은 src/agent_a_mlops/ (A 소유 — 신호 정의). promote 신호 형식은 계약
§8-E이고 그 소비자(consumer._check_ct2_promote)도 A 소유이므로, 롤백 신호를 A가 쓰는 것은
purview 내다. ct_id·신호 규약은 값 계약(§8-E)만 공유하고 orchestrator 모듈을 import하지 않는다
(프로세스 간 결합 회피 — ct2_deploy_approval과 동일 원칙).

**임계 단일 소스** = `WATCH_DEFAULTS` (아래) + `config/params.yaml` `ct.ct2_watch_*` override.
값은 REPORT_05 기반 출발값 — WP-D 스윕에서 재산정.

⚠️ **개통 전 기본 off** (`ct2_watch_enabled: false`): 감시·롤백 실증(시뮬 Scorecard 프로브
미탐 0건) 전에는 판정만 하고 자동 롤백을 실행하지 않는다. 미탐 1건 발생 시 즉시 사람
게이트로 회귀(헌법 3-3 ③ ⓓ).

실행 (2026-08-05 M3 — 상주 모드 신설):
  python -m src.agent_a_mlops.ct2_deploy_monitor --serve            # 상주 (run_stack CT2-WATCH)
  python -m src.agent_a_mlops.ct2_deploy_monitor --chamber SIM_CH_1 --incident INC-... [--apply]

상주 모드는 배포 경로가 드롭한 **감시 예약**(`control/ct2/watch_<INC>.json`)을 폴링한다 —
구 1회성 CLI 는 `--chamber --incident --baseline-p95 --since` 를 사람이 넣어야 했고 **호출자가
0건**이었다(배포는 자동인데 감시는 아무도 시작하지 않는 상태).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("ct2.monitor")

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_DIR = REPO_ROOT / "control" / "ct2"            # promote 신호 채널 (§8-E — consumer가 스캔)
BUNDLE_ROOT = REPO_ROOT / "models" / "anomaly_ae"
ROLLBACK_APPROVER = "ct2_deploy_monitor"               # 감사 주체 (자동 롤백)
ROLLBACK_APPROVER_ROLE = "system"
# consumer 더미 폴백 대역 (consumer.DUMMY_ANOMALY_RANGE 와 같은 값 — 프로세스 경계라 복제).
# 더미 식별은 근사일 뿐이므로 판정이 아니라 **의심 지표**로만 쓴다 (_dummy_suspect_rate).
DUMMY_ANOMALY_RANGE = (0.01, 0.15)
DUMMY_SUSPECT_WARN = 0.30                              # 이 비율 초과면 창이 더미 구간과 겹침 경고

# 임계 기본값 (params `ct.ct2_watch_*` 로 override — 6-1). 출발값 근거는 docstring.
WATCH_DEFAULTS = {
    "enabled": False,
    "window_wafers": 200,
    "min_wafers": 100,               # 창 최소 충전 — 미달이면 판정 유예 (조기 오발화 방지)
    "cross_silence_min_events": 10,  # SPC 위반 최소 건수 — 미만이면 교차 판정 불가(표본 부족)
    "cross_silence_max": 0.5,        # SPC 위반 wafer 중 AE 침묵 비율 상한 — 초과 시 미탐 발화
    "p95_drop_ratio": 0.3,           # 배포 후 P95 < 배포 전 P95 × 이 비율이면 붕괴 발화
    "p95_floor": 0.02,               # 배포 후 P95가 이 값 미만(전 0 근접)이면 붕괴 발화
    "auto_rollback": False,          # 발화 시 자동 롤백 실행 여부 (false = 발화만 기록)
}


def load_watch_params(params_path=None) -> dict:
    """`ct.ct2_watch_*` 로 WATCH_DEFAULTS override (validate_bundle.load_gate_params와 동일 패턴).

    수치 키는 float 강제(YAML 인용부호 방어), 로드 실패 시 기본값 유지(+경고).
    """
    p = dict(WATCH_DEFAULTS)
    path = Path(params_path) if params_path else REPO_ROOT / "config" / "params.yaml"
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            ct = (yaml.safe_load(f) or {}).get("ct") or {}
    except Exception as e:                                   # noqa: BLE001
        log.warning("params.yaml 로드 실패 → 감시 기본값 사용: %s", e)
        return p
    for key in WATCH_DEFAULTS:
        cfg_key = f"ct2_watch_{key}"
        if ct.get(cfg_key) is not None:
            p[key] = ct[cfg_key]
    for key in ("window_wafers", "min_wafers", "cross_silence_min_events",
                "cross_silence_max", "p95_drop_ratio", "p95_floor"):
        try:
            p[key] = float(p[key])
        except (TypeError, ValueError):
            log.warning("ct2_watch_%s 값 %r 수치 아님 → 기본값 %s", key, p[key], WATCH_DEFAULTS[key])
            p[key] = float(WATCH_DEFAULTS[key])
    # ★표본 하한 캡 — 0 이 들어오면 감시가 **조용히 죽는다** (헌법 7장 "분모가 되는
    #   baseline·임계를 검증 없이 사용"). `cross_silence_min_events: 0` 이면 SPC 발화
    #   0건일 때 표본 부족 가드(`n_spc < min_events`)를 지나쳐 `silent / n_spc` 가
    #   ZeroDivisionError 를 내고, 판정이 아니라 실행 오류(exit 1)로 끝난다 — 사람은
    #   감시가 도는 줄 안다. `min_wafers`·`window_wafers` 0 도 같은 계열(창 충전 유예가
    #   사라져 빈 창에서 오발화 / LIMIT 0 조회).
    for key in ("cross_silence_min_events", "min_wafers", "window_wafers"):
        if p[key] < 1:
            log.warning("★감시 무력화 감지 — ct2_watch_%s=%s < 1 → 1 로 캡. "
                        "임계 완화는 PM 승인 사항 (헌법 3-3 ③ ⓑ 준용).", key, p[key])
            p[key] = 1.0
    if p["cross_silence_max"] <= 0:                          # 0 이하면 rate>=0 이 항상 참 = 상시 발화
        log.warning("★감시 오발화 위험 — ct2_watch_cross_silence_max=%s ≤ 0 이면 "
                    "SPC 발화가 있는 모든 창에서 미탐으로 판정된다. 기본값 %s 로 되돌린다.",
                    p["cross_silence_max"], WATCH_DEFAULTS["cross_silence_max"])
        p["cross_silence_max"] = float(WATCH_DEFAULTS["cross_silence_max"])
    return p


# ── 감시 신호 (순수 함수 — DB·파일 무의존, 단위 테스트 대상) ──────────────────
def cross_silence(ae_scores, spc_flags, qual: float, params: dict) -> tuple[bool, dict]:
    """① SPC↑ / AE 침묵 교차. (발화, 상세) 반환.

    SPC(B Nelson)가 위반을 낸 wafer 중, 같은 wafer에서 AE가 침묵(ae_score < qual)한 비율.
    SPC 위반 건수가 `cross_silence_min_events` 이상일 때만 판정한다 — 표본이 적으면 비율이
    불안정해 오발화한다. 침묵 비율이 `cross_silence_max` 이상이면 미탐 발화.
    """
    ae = np.asarray(ae_scores, float)
    spc = np.asarray(spc_flags, bool)
    if len(ae) != len(spc):
        raise ValueError(f"ae_scores({len(ae)})·spc_flags({len(spc)}) 길이 불일치")
    n_spc = int(spc.sum())
    # `n_spc == 0` 을 **명시 조건으로** 둔다 — 호출자가 캡을 거치지 않고 params 를 직접
    # 넘기는 경로(테스트·타 모듈)에서도 0 분모가 성립하지 않게 하는 2층 방어.
    if n_spc == 0 or n_spc < params["cross_silence_min_events"]:
        return False, {"n_spc": n_spc, "silent_rate": None,
                       "note": "SPC 위반 표본 부족 — 판정 유예"}
    silent = int((spc & (ae < qual)).sum())
    rate = silent / n_spc
    return (rate >= params["cross_silence_max"],
            {"n_spc": n_spc, "silent": silent, "silent_rate": round(rate, 4),
             "qual": qual, "threshold": params["cross_silence_max"]})


def distribution_collapse(current_scores, baseline_p95, params: dict) -> tuple[bool, dict]:
    """② ae_score 분포 붕괴. (발화, 상세) 반환.

    배포 후 P95가 ⓐ 배포 전 P95 × `p95_drop_ratio` 미만(급락)이거나 ⓑ `p95_floor` 미만(전 0
    근접)이면 발화. baseline이 없으면(첫 배포) 급락 판정은 생략하고 floor만 본다.
    """
    cur = np.asarray(current_scores, float)
    cur_p95 = float(np.percentile(cur, 95)) if len(cur) else 0.0
    floor_fired = cur_p95 < params["p95_floor"]
    drop_fired = bool(baseline_p95 is not None and baseline_p95 > 0
                      and cur_p95 < baseline_p95 * params["p95_drop_ratio"])
    return (floor_fired or drop_fired,
            {"cur_p95": round(cur_p95, 4),
             "baseline_p95": None if baseline_p95 is None else round(float(baseline_p95), 4),
             "floor_fired": floor_fired, "drop_fired": drop_fired,
             "p95_floor": params["p95_floor"], "p95_drop_ratio": params["p95_drop_ratio"]})


def evaluate(ae_scores, spc_flags, qual: float, baseline_p95, params: dict) -> dict:
    """감시 2종 종합 판정. verdict 딕셔너리 (`rollback`이 최종 신호).

    창 충전 미달이면 판정 유예(rollback=False, deferred=True). 두 신호 중 하나라도 발화하면
    rollback=True. 어느 것도 사후 게이트만으로 못 막는 잔여 위험을 잡는다.
    """
    ae = np.asarray(ae_scores, float)
    n = len(ae)
    if n < params["min_wafers"]:
        return {"rollback": False, "deferred": True, "n": n,
                "reason": f"창 충전 미달 ({n} < {params['min_wafers']}) — 판정 유예",
                "signals": {}}
    cs_fired, cs = cross_silence(ae_scores, spc_flags, qual, params)
    dc_fired, dc = distribution_collapse(ae_scores, baseline_p95, params)
    reasons = []
    if cs_fired:
        reasons.append(f"SPC↑/AE침묵 {cs['silent_rate']} ≥ {cs['threshold']}")
    if dc_fired:
        reasons.append(f"분포붕괴 P95={dc['cur_p95']}"
                       + (f"<{dc['baseline_p95']}×{dc['p95_drop_ratio']}" if dc["drop_fired"]
                          else f"<floor {dc['p95_floor']}"))
    return {"rollback": bool(cs_fired or dc_fired), "deferred": False, "n": n,
            "reason": " · ".join(reasons) if reasons else "정상 — 발화 없음",
            "signals": {"cross_silence": cs, "distribution_collapse": dc}}


# ── 롤백 대상 해석 + 신호 드롭 + 기록 (DB·파일 — conn 주입으로 테스트) ──────────
def _ct_id_for(incident_id: str) -> str:
    """ct_decisions 연동 키 — orchestrator/ct2_deploy_approval과 동일 규약 (§8-E 값 계약)."""
    return f"CT-{incident_id[4:]}-AE" if incident_id.startswith("INC-") else f"CT-{incident_id}-AE"


def _current_bundle_for(conn, incident_id: str) -> Optional[str]:
    """이 Incident가 배포한 번들명 (ct_decisions.model_version_after) — 롤백 FROM 대상.

    `--current-bundle` 미지정 시 이 값으로 현 배포분을 특정한다. 이게 없으면 롤백 대상
    해석이 현 배포분 자신을 고를 수 있어(자기 롤백) 위험하므로, 미상이면 에스컬레이션한다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT model_version_after FROM ct_decisions WHERE ct_id=%s",
                    (_ct_id_for(incident_id),))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def resolve_rollback_target(conn, exclude_bundle: str) -> Optional[str]:
    """직전 정상 번들(롤백 대상) 해석 — 배포 이력 중 현 배포분(`exclude_bundle`)을 뺀 최신.

    반환은 번들 **이름**(model_version_after). 없으면 None(롤백 대상 부재 — 에스컬레이션).
    `exclude_bundle`은 **반드시 현 배포 번들**이어야 한다 — 빈값이면 현 배포분(나쁜 번들)을
    그대로 반환해 자기 롤백이 된다. 호출자(trigger_rollback)가 현 배포분을 먼저 특정한다.

    **M4-2 (2026-08-05)**: 후보 상태에 `ROLLED_BACK` 을 포함한다. 구 질의는
    `IN ('PROMOTED','AUTO_PROMOTED')` 뿐이라, 한 번 롤백한 행이 후보에서 빠지면서 **2회차
    롤백 대상이 단조 소진**됐다 (롤백할수록 되돌아갈 곳이 사라지는 구조). 롤백된 행의
    `model_version_after` 는 M4-1 로 **복귀 대상 번들**을 가리키므로, 그 값이야말로
    "그때 실제로 서빙되기 시작한 번들"이고 후보로서 정확하다.

    ⚠️ 챔버 스코프(WP-E) 미해결 상태에서는 공용 모델 1개라 챔버 무관 최신 배포분을 쓴다.
    WP-E 착지 시 챔버 필터(`model_version_after LIKE 'ae_ct2_<chamber>_%'`)를 추가한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT model_version_after FROM ct_decisions
                WHERE ct_type='ct2_ae'
                  AND retrain_status IN ('PROMOTED','AUTO_PROMOTED','ROLLED_BACK')
                  AND model_version_after IS NOT NULL
                  AND model_version_after <> %s
                ORDER BY created_at DESC LIMIT 1""",
            (exclude_bundle,))
        row = cur.fetchone()
    return row[0] if row else None


def drop_rollback_signal(incident_id: str, to_bundle_path: str) -> Path:
    """직전 번들로 promote 신호 재드롭 (§8-E — consumer가 관리형 리로드 + DriftTracker 리셋).

    파일명은 `promote_<incident>-rollback.json` — consumer의 `promote_*.json` glob에 매칭되면서
    전방 배포 신호와 구분된다. 원자 교체(.tmp → os.replace)로 부분 읽기 방지.
    """
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    sig = CONTROL_DIR / f"promote_{incident_id}-rollback.json"
    tmp = sig.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "incident_id": incident_id,
        "bundle": str(Path(to_bundle_path).resolve()),       # consumer가 이 번들로 리로드
        "rollback": True,
        "ct_id": _ct_id_for(incident_id),
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, sig)
    return sig


def record_rollback(conn, incident_id: str, to_bundle: str, reason: str,
                    from_bundle: Optional[str] = None) -> None:
    """**실제 롤백** 3중 기록 — ct_decisions(ROLLED_BACK) + approval_records(감사) + CHANGELOG.

    복귀 대상(`to_bundle`)이 실존해 신호를 드롭한 경우에만 부른다. 감사는 면제되지 않는다
    (헌법 1-1). 자동 롤백도 approver='ct2_deploy_monitor'로 남긴다.

    **M4-1 (2026-08-05) — `model_version_after` 를 복귀 대상으로 갱신한다.** 구 구현은
    `model_version_before` 만 썼는데, 그러면 다음 감시 주기의 `_current_bundle_for` 가
    **여전히 롤백된 나쁜 번들**을 "현 배포분"으로 돌려준다. 컬럼 의미도 그게 맞다 —
    `model_version_after` = *이 결정 이후 서빙되는 번들* 이고, 롤백 이후 서빙되는 것은
    복귀 대상이다. `before` 에는 되돌린 대상(= 직전까지 서빙되던 나쁜 번들)을 남긴다.
    """
    ct_id = _ct_id_for(incident_id)
    name = Path(to_bundle).name
    prev = Path(from_bundle).name if from_bundle else None
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE ct_decisions
                  SET retrain_status='ROLLED_BACK',
                      model_version_before=COALESCE(%s, model_version_after),
                      model_version_after=%s
                WHERE ct_id=%s""", (prev, name, ct_id))
        cur.execute(
            """INSERT INTO approval_records
                   (incident_id, request_type, status, selected_option,
                    decision_reason, approver, approver_role, decided_at)
               VALUES (%s, 'ct2_rollback', 'APPROVED', %s, %s, %s, %s, NOW())""",
            (incident_id, name, f"자동 롤백 (배포 후 감시 발화): {reason}",
             ROLLBACK_APPROVER, ROLLBACK_APPROVER_ROLE))
    conn.commit()
    _append_changelog(incident_id, f"ROLLED_BACK → `{name}`", reason)


def record_watch_escalation(conn, incident_id: str, reason: str) -> None:
    """**롤백 불가**(대상 부재) 시 에스컬레이션 기록 — approval_records(ESCALATED) + CHANGELOG.

    감시는 발화했지만 복귀할 정상 번들이 없어 롤백을 못 한 경우다. 이때 `ct_decisions`
    상태는 **바꾸지 않는다** — 나쁜 번들이 여전히 서빙 중이라 ROLLED_BACK은 사실이 아니다.
    감사·알림만 남기고 사람 개입을 부른다 (서빙 보호 우선 + 정확한 상태 기록).
    """
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO approval_records
                   (incident_id, request_type, status, decision_reason,
                    approver, approver_role, decided_at)
               VALUES (%s, 'ct2_rollback', 'ESCALATED', %s, %s, %s, NOW())""",
            (incident_id, f"감시 발화·롤백 불가(사람 개입 필요): {reason}",
             ROLLBACK_APPROVER, ROLLBACK_APPROVER_ROLE))
    conn.commit()
    _append_changelog(incident_id, "ESCALATED (롤백 대상 부재)", reason)


def _append_changelog(incident_id: str, action: str, reason: str) -> None:
    """models/CHANGELOG.md 감시 조치 기록 (실패해도 흐름은 계속, 로그만)."""
    try:
        line = (f"\n- {datetime.now(timezone.utc):%Y-%m-%d} CT²(AE) {action} — "
                f"incident {incident_id} · 배포 후 감시 발화 · 사유: {reason} "
                f"(재배포는 게이트 재통과 필수)\n")
        with open(REPO_ROOT / "models" / "CHANGELOG.md", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:                                   # noqa: BLE001
        log.warning("CHANGELOG 기록 실패(흐름 계속): %s", e)


def rollback_already_recorded(conn, incident_id: str, window_key: Optional[str]) -> bool:
    """같은 창에서 이미 롤백/에스컬레이션을 기록했는지 (M3-3 멱등, 2026-08-05).

    왜 필요한가: 상주 감시는 같은 창을 주기마다 다시 본다. 가드가 없으면 `approval_records`
    INSERT · `ct_decisions` UPDATE · CHANGELOG append 가 **매 주기 중복 누적**된다.

    판정은 **상태로** 한다 (헌법 7장 / TSR-0002 — "예외가 안 났으니 성공" 금지): 창 키를
    `decision_reason` 에 새겨 두고 그 문자열의 실존을 조회한다. 창 키가 없으면(수동 1회성
    실행) 멱등을 적용하지 않는다 — 사람이 의도적으로 다시 돌린 경우를 막지 않기 위해.
    """
    if conn is None or not window_key:
        return False
    try:
        with conn.cursor() as cur:
            # `_`·`%` 는 LIKE 와일드카드다 — incident_id 에 `_` 가 흔하므로(`SIM_CH_1` 계열
            # 챔버가 키에 섞이는 형태) 이스케이프하지 않으면 **다른 창의 기록을 자기 것으로
            # 오인**해 정당한 롤백을 건너뛸 수 있다.
            like = ("%[" + window_key.replace("\\", "\\\\").replace("%", "\\%")
                    .replace("_", "\\_") + "]%")
            cur.execute(
                """SELECT 1 FROM approval_records
                    WHERE incident_id=%s AND request_type='ct2_rollback'
                      AND decision_reason LIKE %s ESCAPE '\\' LIMIT 1""",
                (incident_id, like))
            return cur.fetchone() is not None
    except Exception as e:                                   # noqa: BLE001 — 조회 실패는 보수적으로
        log.warning("멱등 조회 실패 → 중복 기록을 피하려 이번 회차 건너뜀: %s", e)
        return True


def verify_signal_consumed(sig: Path, timeout_sec: float = 30.0,
                           poll_sec: float = 1.0) -> bool:
    """드롭한 롤백 신호를 consumer 가 **실제로 집었는지** 확인 (M4-3, 2026-08-05).

    `processed/` 로 이동했으면 성공, `failed/` 면 실패, 그대로 남아 있으면 미소비다.
    "파일을 썼고 예외가 안 났다"는 롤백이 **적용됐다는 증거가 아니다** — consumer 가 죽어
    있거나 `LEAN85_AE_MODE` 가 dummy 면 신호는 영원히 쌓이기만 한다 (TSR-0002 계열).
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if (sig.parent / "processed" / sig.name).exists():
            return True
        if (sig.parent / "failed" / sig.name).exists():
            log.error("롤백 신호가 failed/ 로 이동 — consumer 가 번들 로드에 실패했다: %s", sig.name)
            return False
        if not sig.exists():                                 # 사람이 치웠거나 다른 경로로 소비
            return True
        time.sleep(poll_sec)
    return False


def trigger_rollback(conn, incident_id: str, verdict: dict,
                     current_bundle: Optional[str] = None,
                     window_key: Optional[str] = None,
                     verify_sec: float = 0.0) -> dict:
    """발화 verdict → 롤백 실행. 결과 딕셔너리 반환.

    롤백 대상이 없으면(직전 배포분 부재) 신호를 드롭하지 않고 에스컬레이션만 기록한다 —
    복귀할 곳이 없는데 현 번들을 지우면 서빙이 더미로 떨어진다(서빙 보호 우선).

    Args:
        window_key: 감시 창 식별자(`incident|since`). 주면 같은 창의 중복 기록을 막는다 (M3-3).
        verify_sec: >0 이면 신호 소비를 이 시간까지 확인한다 (M4-3). 미소비면 에스컬레이션
            기록을 덧붙인다 — 롤백을 "했다고 믿는" 상태를 남기지 않는다.
    """
    if not verdict.get("rollback"):
        return {"action": "none", "reason": "발화 없음"}
    if rollback_already_recorded(conn, incident_id, window_key):
        log.info("같은 창(%s)에서 이미 기록됨 — 중복 롤백 skip (M3-3)", window_key)
        return {"action": "already_recorded", "window_key": window_key}
    # 현 배포 번들을 먼저 특정한다 — 없으면 자기 롤백 위험이라 에스컬레이션 (서빙 보호).
    current = current_bundle or _current_bundle_for(conn, incident_id)
    if not current:
        log.error("현 배포 번들 미상 — 자기 롤백 위험으로 에스컬레이션. incident=%s", incident_id)
        record_watch_escalation(conn, incident_id, f"{verdict['reason']} / 현 배포 번들 미상")
        return {"action": "escalated", "reason": "현 배포 번들 미상"}
    target = resolve_rollback_target(conn, exclude_bundle=current)
    if not target or target == current:
        log.error("롤백 대상 없음/자기 자신 (현=%s) — 에스컬레이션만. incident=%s", current, incident_id)
        record_watch_escalation(conn, incident_id, f"{verdict['reason']} / 롤백 대상 부재")
        return {"action": "escalated", "reason": "롤백 대상 부재", "verdict": verdict["reason"]}
    target_path = BUNDLE_ROOT / target
    if not target_path.exists():
        log.error("롤백 대상 번들 폴더 없음: %s — 에스컬레이션만", target_path)
        record_watch_escalation(conn, incident_id, f"{verdict['reason']} / 번들 폴더 부재 {target}")
        return {"action": "escalated", "reason": f"번들 폴더 부재: {target}"}
    # 감사 먼저(헌법 1-1 — 감사 보장), 그 다음 보호 조치(신호 드롭). 신호가 실패해도 감사는
    # 남고, 다음 감시 주기가 재발화해 재드롭한다(자기 치유). 순서를 뒤집으면 신호는 나갔는데
    # 감사가 없는 '무감사 롤백'이 생긴다.
    reason = verdict["reason"] + (f" [{window_key}]" if window_key else "")
    record_rollback(conn, incident_id, target, reason, from_bundle=current)
    sig = drop_rollback_signal(incident_id, str(target_path))
    log.warning("ROLLED_BACK %s → %s (신호 %s) · 사유: %s",
                current, target, sig.name, verdict["reason"])
    out = {"action": "rolled_back", "from_bundle": current, "to_bundle": target,
           "signal": str(sig), "reason": verdict["reason"], "window_key": window_key}
    if verify_sec > 0:                                       # M4-3 — 상태로 확인 (TSR-0002)
        out["consumed"] = verify_signal_consumed(sig, verify_sec)
        if not out["consumed"]:
            log.error("★롤백 신호 미소비 (%.0fs) — consumer 가 멈췄거나 AE_MODE 가 live 가 "
                      "아닐 수 있다. 나쁜 번들이 아직 서빙 중일 가능성.", verify_sec)
            record_watch_escalation(
                conn, incident_id,
                f"롤백 신호 미소비({sig.name}) — 복귀 미확인. 사람 확인 필요 [{window_key or '-'}]")
    return out


# ── CLI (수동/주기 실행 — 창 데이터는 DB에서, 판정은 순수 함수) ─────────────────
def _fetch_window(conn, chamber: str, n: int, since=None,
                  exclude_dummy: bool = True) -> tuple[list, list, dict]:
    """최근 n wafer의 (anomaly_score, spc_flag, 메타). `wafer_predictions` **단일 테이블**.

    스키마 정합 (db/init.sql 정본 대조 — H3 수정):
      · AE 점수 컬럼은 `ae_score` 가 아니라 **`anomaly_score`** 다 (consumer 발행 시
        `ae_score` → `anomaly_score` 로 매핑됨. 계약 §2 `fdc.prediction` 필드명).
      · SPC 플래그는 **같은 행의 `spc_flags`(JSONB)** 로 판정한다. `spc_violations` 에는
        `wafer_id` 컬럼이 아예 없어(alert 단위 테이블) wafer 조인이 성립하지 않는다.
        → 조인 제거로 쿼리가 단순해지고 wafer 단위 정합도 정확해진다.

    **더미 폴백 배제** (`exclude_dummy`): AE 미가동·번들 로드 실패 구간에는 `anomaly_score`
    에 난수 더미가 적재된다(consumer 더미 폴백). 더미 위에서 분포 붕괴를 판정하면 무의미하다.
    현 스키마에는 AE 전용 버전 컬럼이 없어 **행 단위로 AE 더미를 식별할 수 없다** — 그래서
    ⓐ `model_version='dummy'`(파이프라인 전체 더미) 행을 제외하고 ⓑ `since`(배포 시각) 로
    창을 배포 이후로 제한하며 ⓒ 남은 더미 혼입 가능성을 **의심 지표로 리포트**한다
    (`dummy_suspect_rate` — 더미 구간과 겹치면 판정을 신뢰하지 말라는 신호).
    근본 해결은 `wafer_predictions` 에 AE 버전/실값 여부 컬럼 추가(계약 2-2 + 헌법 3-2 PM 승인).

    Returns:
        (scores, spc_flags, meta) — meta 는 표본 수·더미 의심률 등 진단 정보.
    """
    sql = ["""SELECT p.anomaly_score, p.spc_flags, p.model_version
                FROM wafer_predictions p
               WHERE p.chamber_id = %s AND p.anomaly_score IS NOT NULL"""]
    args: list = [chamber]
    if exclude_dummy:
        sql.append("AND (p.model_version IS NULL OR p.model_version <> 'dummy')")
    if since is not None:
        sql.append("AND p.created_at >= %s")
        args.append(since)
    sql.append("ORDER BY p.created_at DESC LIMIT %s")
    args.append(int(n))
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), tuple(args))
        rows = cur.fetchall()

    scores, flags = [], []
    for score, spc_flags, _mv in rows:
        scores.append(float(score))
        flags.append(_spc_flag_truthy(spc_flags))
    meta = {"n": len(scores), "spc_true": int(sum(flags)),
            "dummy_suspect_rate": _dummy_suspect_rate(scores),
            "source": "wafer_predictions(anomaly_score, spc_flags)",
            "excluded_dummy_model": exclude_dummy, "since": str(since) if since else None}
    if meta["dummy_suspect_rate"] > DUMMY_SUSPECT_WARN:
        log.warning("더미 의심률 %.2f — 창이 AE 미가동 구간과 겹칠 수 있다. "
                    "분포 붕괴 판정을 신뢰하지 말고 --since 로 배포 이후로 제한하라.",
                    meta["dummy_suspect_rate"])
    if meta["spc_true"] == 0:
        log.warning("창 내 spc_flags 발화 0건 — 교차침묵 신호는 판정 유예된다. "
                    "`spc_flags` 병렬 채널이 아직 빈 배열로 발행되면(A3-2 미구현) "
                    "감시 ①은 구조적으로 비활성이다.")
    return scores, flags, meta


def _spc_flag_truthy(spc_flags) -> bool:
    """`wafer_predictions.spc_flags`(JSONB) → SPC 위반 여부.

    계약상 Nelson 위반 센서 목록(배열)이며, 비어 있지 않으면 위반으로 본다. psycopg2가
    JSONB를 list/dict로 돌려주고, 드라이버 설정에 따라 문자열일 수도 있어 둘 다 받는다.
    """
    if spc_flags is None:
        return False
    if isinstance(spc_flags, str):
        try:
            spc_flags = json.loads(spc_flags)
        except Exception:                                    # noqa: BLE001 — 파싱 불능 = 미판정
            return False
    if isinstance(spc_flags, dict):                          # {"violations": [...]} 변형 대비
        spc_flags = spc_flags.get("violations", spc_flags.get("flags", []))
    return bool(spc_flags) if isinstance(spc_flags, (list, tuple)) else bool(spc_flags)


def _dummy_suspect_rate(scores) -> float:
    """더미 폴백 의심률 — 값이 더미 난수 대역이고 소수 2자리로 반올림된 비율.

    consumer 더미는 `round(uniform(0.01, 0.15), 2)`, 실값은 `round(ae_score, 4)`다. 완전한
    식별은 불가(실값도 우연히 2자리일 수 있음)하므로 **의심 지표**로만 쓴다 — 임계 판정에
    쓰지 않고 리포트·경고로 노출한다(정직한 한계).
    """
    if not scores:
        return 0.0
    lo, hi = DUMMY_ANOMALY_RANGE
    suspect = sum(1 for s in scores
                  if lo <= s <= hi and abs(s * 100 - round(s * 100)) < 1e-9)
    return round(suspect / len(scores), 4)


# ── 감시 예약 (M3-1 — 배포 경로가 드롭, 상주 감시가 소비) ────────────────────
WATCH_GLOB = "watch_*.json"
SERVE_POLL_SEC_DEFAULT = 30.0


def load_watch_requests(d: Path | None = None) -> list[dict]:
    """`control/ct2/watch_*.json` 예약 목록 (오래된 것부터). 판독 불가 파일은 skip+로그.

    예약에는 `incident_id`·`chamber_id`·`bundle`·`baseline_p95`·`promoted_at` 이 들어 있어,
    구 CLI 가 사람에게 묻던 4가지(`--chamber --incident --baseline-p95 --since`)를 전부
    대체한다 (M3-1·M3-4).
    """
    # ★기본 경로를 **인자 디폴트로 두지 않는다** — 디폴트는 import 시점에 평가되므로
    #   `CONTROL_DIR` 를 나중에 바꿔도(테스트·env 재설정) 옛 경로를 계속 본다. 그러면 상주
    #   루프가 빈 목록만 보며 영원히 도는 '조용한 무동작'이 된다.
    d = Path(d) if d is not None else CONTROL_DIR
    out = []
    try:
        files = sorted(d.glob(WATCH_GLOB), key=lambda q: q.stat().st_mtime)
    except OSError:
        return out
    for f in files:
        try:
            d_ = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(d_, dict):                     # json.loads 성공 ≠ dict (헌법 7장)
                raise ValueError(f"예약이 객체 아님: {type(d_).__name__}")
            if not d_.get("incident_id"):
                raise ValueError("incident_id 없음")
            d_["_path"] = str(f)
            out.append(d_)
        except Exception as e:                               # noqa: BLE001
            log.warning("감시 예약 판독 실패 skip: %s — %s", f.name, e)
    return out


def window_key_for(incident_id: str, since) -> str:
    """감시 창 식별자 — 같은 창의 중복 기록을 막는 키 (M3-3).

    `approval_records.decision_reason` 은 TEXT 라 길이 제약이 없다. 이 키는 컬럼 폭이 아니라
    **조회 가능성**을 위해 존재한다 (`LIKE '%[key]%'`).
    """
    return f"{incident_id}|{str(since)[:19] if since else 'na'}"


def build_argparser():
    """CLI 파서 구성 (배포 후 감시 실행)."""
    ap = argparse.ArgumentParser(
        prog="ct2_deploy_monitor",
        description="CT² 배포 후 감시·자동 롤백 (헌법 3-3 ③ ⓔ)")
    ap.add_argument("--serve", action="store_true",
                    help="상주 모드 — control/ct2/watch_*.json 예약을 폴링해 자동 감시 "
                         "(구 1회성 CLI 는 호출자가 0건이었다. M3-1)")
    ap.add_argument("--chamber", help="감시 대상 챔버 (SIM_CH_1 등) — --serve 가 아니면 필수")
    ap.add_argument("--incident", help="배포 Incident ID (INC-...) — --serve 가 아니면 필수")
    ap.add_argument("--qual", type=float, default=0.2, help="AE qual 임계 (침묵 판정)")
    ap.add_argument("--baseline-p95", type=float, default=None, help="배포 전 ae_score P95 (급락 기준)")
    ap.add_argument("--current-bundle", default=None,
                    help="현 배포 번들명 (롤백 FROM·제외 대상). 생략 시 incident의 "
                         "ct_decisions.model_version_after로 자동 해석 — 미상이면 자기 롤백 위험이라 에스컬레이션")
    ap.add_argument("--since", default=None,
                    help="감시 창 시작 시각 ISO (배포 시각 권장) — 배포 이전·AE 더미 구간 배제. "
                         "생략 시 최근 N wafer 전체를 보므로 더미 혼입 위험 (경고로 표시)")
    ap.add_argument("--include-dummy", action="store_true",
                    help="model_version='dummy' 행도 포함 (기본 제외 — 더미 위 판정은 무의미)")
    ap.add_argument("--apply", action="store_true",
                    help="발화 시 자동 롤백 실행 (미지정 = 판정만, dry-run)")
    ap.add_argument("--verify-sec", type=float, default=30.0,
                    help="롤백 신호 소비 확인 상한(초). 0=확인 안 함 (M4-3)")
    ap.add_argument("--poll-sec", type=float, default=SERVE_POLL_SEC_DEFAULT,
                    help="--serve 폴링 주기(초)")
    ap.add_argument("--params", default=None)
    return ap


def run_once(conn, params: dict, *, chamber: str, incident: str, qual: float,
             baseline_p95, since, current_bundle=None, include_dummy=False,
             apply=False, verify_sec: float = 0.0) -> int:
    """감시 1회분 — 창 조회 → 판정 → (apply면) 롤백. exit code 반환.

    `baseline_p95` 가 없으면 **조용히 floor 조건만으로 퇴화하지 않는다** (M2): 감시②의
    "P95 0.18 → 0.03 급락" 같은 전형적 침묵화를 못 잡게 되는데, 로그 한 줄 없이 그렇게 되면
    사람은 감시가 도는 줄 안다. 명시 경고 + 발화 코드(3)로 사람을 부른다.
    """
    ae, spc, wmeta = _fetch_window(conn, chamber, int(params["window_wafers"]),
                                   since=since, exclude_dummy=not include_dummy)
    log.info("창 조회: %s", wmeta)
    if baseline_p95 is None:
        log.error("★baseline_p95 부재 — 감시②가 floor(%s) 단독으로 퇴화한다. 급락 판정 불가. "
                  "배포 경로가 promote 신호/감시 예약에 baseline_p95 를 싣는지 확인하세요 (M2).",
                  params["p95_floor"])
    verdict = evaluate(ae, spc, qual, baseline_p95, params)
    verdict["window_meta"] = wmeta                            # 데이터 출처·더미 의심률 동봉
    verdict["baseline_available"] = baseline_p95 is not None
    log.info("판정: %s (n=%s) — %s", "발화" if verdict["rollback"] else
             ("유예" if verdict.get("deferred") else "정상"), verdict["n"], verdict["reason"])
    if not verdict["rollback"]:
        # baseline 부재는 "정상"이 아니라 "일부만 봤다"는 뜻이다 — 조용한 0 종료를 막는다.
        return 0 if baseline_p95 is not None else 3
    wkey = window_key_for(incident, since)
    # 발화 — apply + auto_rollback 스위치 둘 다 true 여야 실행 (개통 전 이중 잠금)
    if apply and params.get("auto_rollback"):
        result = trigger_rollback(conn, incident, verdict, current_bundle,
                                  window_key=wkey, verify_sec=verify_sec)
        log.warning("롤백 결과: %s", result)
    else:
        log.warning("발화했으나 자동 롤백 미실행 (apply=%s·auto_rollback=%s) — 사람 게이트로. "
                    "미탐 1건 = 헌법 3-3 ③ ⓓ 회귀 신호", apply, params.get("auto_rollback"))
    return 3


_serving = True


def _stop(signum, frame):
    """SIGINT/SIGTERM graceful (헌법 6-2)."""
    global _serving
    _serving = False
    log.info("종료 신호 %s", signum)


def serve(conn_factory, params: dict, args) -> int:
    """상주 모드 — 감시 예약을 폴링해 자동 실행 (M3-1).

    예약(`watch_<INC>.json`)은 배포 경로가 드롭한다. 여기서는 창이 찰 때까지(`min_wafers`)
    유예 판정을 반복하다가, 판정이 나면 처리한다. **예약은 지우지 않는다** — 배포 후
    감시는 1회로 끝나는 게 아니라 창을 계속 보는 일이고, 중복 기록은 M3-3 멱등이 막는다.
    """
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    poll = max(1.0, float(args.poll_sec))
    log.info("상주 감시 시작 — 예약 폴링 %ss (enabled=%s · auto_rollback=%s)",
             poll, params.get("enabled"), params.get("auto_rollback"))
    while _serving:
        reqs = load_watch_requests()
        if not reqs:
            log.debug("감시 예약 없음")
        for r in reqs:
            conn = None
            try:
                conn = conn_factory()
                run_once(conn, params,
                         chamber=r.get("chamber_id") or args.chamber,
                         incident=r["incident_id"],
                         qual=args.qual,
                         baseline_p95=r.get("baseline_p95"),
                         since=r.get("promoted_at"),          # M3-4 — 사람 입력 불요
                         current_bundle=r.get("bundle"),
                         include_dummy=args.include_dummy,
                         apply=args.apply, verify_sec=args.verify_sec)
            except Exception as e:                           # noqa: BLE001 — 루프는 죽지 않는다
                log.exception("예약 처리 실패(다음 주기 재시도) %s: %s",
                              r.get("incident_id"), e)
            finally:
                if conn is not None:
                    conn.close()
        for _ in range(int(poll)):                           # 종료 신호 반응성
            if not _serving:
                break
            time.sleep(1)
    log.info("상주 감시 종료")
    return 0


def idle_until_enabled(args) -> int:
    """감시 스위치가 꺼진 상태의 상주 대기 (창을 살려 둔다). 종료 신호를 받으면 0.

    파라미터를 **다시 읽지는 않는다** — 스위치는 개통 결정이므로 프로세스 재시작으로
    반영하는 편이 명시적이다. 여기서는 "왜 아무것도 안 하는지"를 주기적으로 알린다.
    """
    global _serving
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    _serving = True
    poll = max(1.0, float(getattr(args, "poll_sec", SERVE_POLL_SEC_DEFAULT)))
    while _serving:
        log.info("감시 대기 중 — ct.ct2_watch_enabled=false 인 동안은 판정하지 않는다 "
                 "(스위치를 켠 뒤 이 창을 재시작)")
        for _ in range(int(max(poll, 60.0))):                # 로그 도배 방지 — 최소 1분 간격
            if not _serving:
                break
            time.sleep(1)
    return 0


def main(argv=None) -> int:
    """CLI — 1회 감시(기본) 또는 상주 감시(`--serve`).

    exit 0=정상 · 3=발화/판정 불완전 · 1=오류 · 0(사유 로그)=감시 비활성.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s │ %(levelname)-7s │ [ct2.monitor] %(message)s")
    args = build_argparser().parse_args(argv)
    params = load_watch_params(args.params)

    # ★M3-2 — `ct2_watch_enabled` 를 실제 스위치로 만든다. 구현은 이 값을 **로드만** 하고
    #   참조하지 않아, 문서·테스트가 "감시를 끄는 스위치"라고 서술하는데 실제 잠금은
    #   `--apply`·`auto_rollback` 뿐이었다 (죽은 파라미터). 꺼져 있으면 조회조차 하지 않는다.
    if not params.get("enabled"):
        log.warning("감시 비활성 (ct.ct2_watch_enabled=false) — 창 조회도 하지 않는다. "
                    "실증(시뮬 Scorecard 프로브 미탐 0건) 후 true 로 (헌법 3-3 ③ ⓓ)")
        if not args.serve:
            return 0
        # ★상주 모드에서는 **죽지 않고 대기**한다. 여기서 종료하면 `run_stack.ps1` 의
        #   CT2-WATCH 창이 기동 즉시 사라져, 운영자는 "감시 창이 안 뜬다"를 버그로 오인한다.
        #   스위치를 켜고 이 창만 재시작하면 되는 상태로 남긴다.
        return idle_until_enabled(args)

    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()

    def _connect():
        return psycopg2.connect(os.environ["DATABASE_URL"])

    if args.serve:
        return serve(_connect, params, args)

    missing = [k for k in ("chamber", "incident") if not getattr(args, k)]
    if missing:
        log.error("--serve 가 아니면 --%s 가 필요하다", " --".join(missing))
        return 1
    try:
        conn = _connect()
    except Exception as e:                                   # noqa: BLE001
        log.error("DB 접속 실패: %s", e)
        return 1
    try:
        return run_once(conn, params, chamber=args.chamber, incident=args.incident,
                        qual=args.qual, baseline_p95=args.baseline_p95, since=args.since,
                        current_bundle=args.current_bundle,
                        include_dummy=args.include_dummy, apply=args.apply,
                        verify_sec=args.verify_sec)
    except Exception as e:                                   # noqa: BLE001
        log.error("감시 실행 오류: %s", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
