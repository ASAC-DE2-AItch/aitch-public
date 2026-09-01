# -*- coding: utf-8 -*-
"""챔버 자동 inhibit(안전 정지) — 발동·기록·해제 코어 (헌법 1-1 예외 4, 2026-08-06).

"정지는 자동, 해제는 사람" (멘토 8/5 확정 — 현업 FDC interdiction·챔버 단위 디폴트):
  발동  : **2단 판정** (2026-08-06 실측으로 판별축 교체 — 상황판 D-5).
          ① 사전 필터(순수·알람 단건): severity=CRITICAL + 동시 위반 distinct 센서
             ≥ min_violation_sensors(기본 2 — 단일 센서 노이즈 배제) + AE 단독 제외.
          ② 본 판정(DB·창 집계): 최근 window_minutes 창에서 **N1 이탈률 ≥ breach_ratio
             인 센서가 min_persistent_sensors 개 이상** — 진짜 챔버 이상은 여러 센서가
             *지속적으로* 한계를 넘고(스톰 실측 C17 44%·C62 39%), 평시 오탐은 만성
             센서 하나뿐(C17 18~23%)이라는 실측이 근거다. 구 축(한 알람의 동시 위반
             센서 수 ≥5)은 정탐(스톰 3센서)이 임계 미달 + 평시 꼬리(최대 9센서)가
             신호보다 넓어 폐기했다.
          통과 시 chamber_inhibits INSERT + control/inhibit/<chamber>.json 드롭 →
          시뮬레이터가 해당 챔버 생산 중단.
  스코프 : chamber 디폴트 / `tttm.reference_suspect`(공통 이동 = 공용 설비 원인)면
          equipment 로 승격 — 형제 챔버 전체, 행은 챔버별 + 같은 incident_id (멘토 8/6).
  디바운스: 챔버당 열린 inhibit 1건 (부분 유니크 인덱스가 DB 레벨 보장).
  해제  : requal 승인(R9) → ChamberRequalified 하류에서 release() — released_at 기록 +
          신호 파일 제거. equipment 스코프는 **같은 incident 형제 행 일괄 해제**(발동-해제
          대칭). **자동 해제 경로 없음** (예외 4 ⓒ).
  회귀  : 오발동(순정상 정지) 1건 실증 시 params `rtd.auto_inhibit` false → 제안 모드
          (발동만 멈춤 — 기록·해제 경로는 유지).

소유: src/orchestrator/ = PM (3-1). B 엔진 무수정 — CRITICAL 마커는 fdc.alert 로만 소비 (1-2).
파일 쓰기는 tmp+os.replace 원자 교체 (7장 규칙 — 부분 쓰기 노출 방지).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("orchestrator.inhibit")

REPO_ROOT = Path(__file__).resolve().parents[2]
INHIBIT_DIR = Path(os.environ.get("RTD_INHIBIT_DIR", "") or (REPO_ROOT / "control" / "inhibit"))
# 형제 챔버 목록 1순위 소스 (2026-08-07, B 리뷰 #118) — 구성 단일 소스. DB·타 파트 무관.
CHAMBER_OFFSETS_PATH = Path(os.environ.get("RTD_CHAMBER_OFFSETS", "")
                            or (REPO_ROOT / "config" / "chamber_offsets.json"))

_CRITICAL = "CRITICAL"
_DEFAULT_EQUIPMENT = os.environ.get("RTD_EQUIPMENT_ID", "ETCH-01")  # 현 구성 = 단일 장비 4챔버


# ── 판정 ────────────────────────────────────────────────────────────────────
def auto_inhibit_enabled(params: Optional[dict]) -> bool:
    """params[rtd][auto_inhibit] — **기본 False (fail-closed, 2026-08-07 반전)**.

    구현은 기본 True("예외 4 발효")였으나 리뷰 3건이 같은 곳을 짚었다: `rtd_params` 를
    안 넘기는 기존 호출부(agent_service 그루퍼)가 있어, params 누락·형상 불량이 **미검증
    임계로 자동 정지를 조용히 켜는** 경로가 된다. 코드베이스의 차단기 패턴(`ct2_auto_promote`
    `ct2_auto_generate` — 전부 "로드 실패 = 잠금")과도 방향이 반대였다. 발동은 이제
    **config 가 명시적으로 `true` 를 줄 때만** — 물리 정지를 켜는 쪽이 명시적이어야 한다.
    """
    try:
        return bool(((params or {}).get("rtd") or {}).get("auto_inhibit", False))
    except Exception:                                    # params 형상 불량 → 잠금 (fail-closed)
        return False


def _rtd_cfg(params: Optional[dict]) -> dict:
    """params[rtd] 안전 추출 (형상 불량 시 빈 dict)."""
    try:
        return ((params or {}).get("rtd") or {})
    except Exception:
        return {}


def violation_sensor_count(alert: dict) -> int:
    """이 알람에서 **동시에 SPC 위반한 distinct 센서 수** = 그 챔버 고유 이상의 폭.

    챔버 스코프 판정의 재료다. 여러 센서가 한꺼번에 관리선을 벗어났다는 것은 단일 센서
    노이즈가 아니라 그 챔버 자체의 문제라는 신호이기 때문이다.

    ⚠️ **`tttm.suspect_sensors` 와 혼동 금지** (2026-08-06 B 리뷰로 정정): 그 필드는
    `reference_suspect` 를 발생시킨 **공통 이동 센서** 목록이라 `suspect_sensors≠∅` 와
    `reference_suspect=True` 가 논리적으로 동치다. 즉 **장비(공용 설비) 원인 신호**이지
    챔버 고유 이상의 폭이 아니다 — 초판 설계가 이를 반대로 썼다.

    ⚠️ **계약 키는 `sensor`** 다 — publisher `_build_violation` 이 엔진 내부 `sensor_id` 를
    `sensor` 로 변환해 싣는다 (2026-08-06 실측으로 확정: `sensor_id` 로 세면 전건 0 이라
    RTD 가 영원히 미발동한다). 엔진 dict 를 직접 넘기는 호출 대비로 `sensor_id` 도 폴백 인정.
    """
    vs = alert.get("violations")
    if not isinstance(vs, (list, tuple)):
        return 0
    out = set()
    for v in vs:
        if not isinstance(v, dict):
            continue
        s = v.get("sensor") or v.get("sensor_id")     # 계약 키 우선 · 엔진 키 폴백
        if s:
            out.add(str(s))
    return len(out)


def alert_severity(alert: dict) -> Optional[str]:
    """이 알람의 severity — `violations[].severity` 의 최댓값 (계약 §3).

    **정본을 둘로 만들지 않으려고** `incident_grouper.alert_max_severity` 를 재사용한다.
    import 가 `incident_grouper → chamber_inhibit` 단방향이라 모듈 최상단에서 역참조하면
    순환이 된다 — 그래서 **함수 안에서 지연 import** 한다(모듈 캐시라 매 호출 비용은 dict 조회).

    ⚠️ 최상위 `alert["severity"]` 를 **폴백으로도 보지 않는다.** 계약에 없는 자리라 값이
    있으면 그게 오히려 규약 위반 신호이고, 폴백을 두면 이번 같은 어긋남이 다시 조용해진다.
    """
    from .incident_grouper import alert_max_severity  # 지연 — 순환 import 회피
    return alert_max_severity(alert.get("violations") or [])


def should_inhibit(alert: dict, params: Optional[dict] = None) -> bool:
    """**사전 필터** (알람 단건·순수 — 예외 4 ⓐ 1단. 본 판정은 `persistent_breach`).

    ① `severity=CRITICAL` (대소문자 무관) — **재료는 `violations[].severity` 의 최댓값**이다.
       🔴 **2026-08-10 수정**: 구 코드는 `alert.get("severity")` 로 **알람 최상위**를 읽었는데
       계약 §3 은 `severity` 를 **`violations[]` 안에** 싣는다. 최상위에는 그 필드가 없어
       `None` → `"NONE" != "CRITICAL"` → **이 필터가 항상 False** 였다. 즉 RTD 자동 정지가
       **한 번도 발동한 적이 없고 발동할 수도 없었다**(헌법 1-1 예외 4 전체가 사문).
       실패가 조용한 것이 문제였다 — False 를 돌려주면 `record_trigger` 가 로그 없이 `None` 을
       반환하고 끝나서, **스위치를 켜고 지켜봐도 "아무 일도 안 일어남"과 구별되지 않는다**
       (실측: grouper 로그 1,112줄에 `RTD`·`inhibit` 문자열 0건).
       바로 옆 `incident_grouper.alert_max_severity` 가 같은 계약을 정확히 읽고 있었고
       (grouper 로그의 `sev=CRITICAL` 이 그 함수 결과다) **RTD 만 그걸 안 썼다.**
    ② **동시 위반 센서 수** ≥ `rtd.min_violation_sensors` (기본 2 — 단일 센서 노이즈 배제).
       재료는 `alert["violations"][].sensor` 의 distinct 개수 = 그 챔버 고유 이상의 폭.
       *(2026-08-06 B 리뷰 정정 — 초판은 `tttm.suspect_sensors` 개수를 썼으나 그 필드는
       공통 이동(장비 원인) 신호라 챔버 조건 재료로 부적합했다. §스코프 참조)*
    ③ `rtd.exclude_ae_arm=true`(기본)면 **AE anomaly 단독 근거 알람은 제외**한다.
       AE 는 레짐 전환에 구조적으로 민감해 PM 통과 직후 100% 경보를 낸다(2026-08-06 실증 —
       `docs/AE_CT2_배포_전말보고서` 그림 5). 이상 신호로는 유효하나 **정지 근거로는 부적합**하며,
       그대로 물리면 PM 경계에서 전 챔버가 동시에 선다.

    ⚠️ **이 함수 단독으로 정지를 결정하지 않는다** (2026-08-06 판별축 교체). 실측에서
    "한 알람의 동시 위반 센서 수"는 오탐·정탐이 겹치는 축으로 판명 — 정지 판정은
    `record_trigger` 가 이 필터 통과 후 `persistent_breach`(창 내 지속 이탈률)로 내린다.
    여기서는 DB 조회 없이 명백한 비대상(WARNING·단일 센서·AE 단독)만 싸게 거른다.

    빈도 근거·임계 확정 절차: `docs/RTD_발동빈도_현업정합_검증_2026-08-06.md`
    (목표 = 1 run(69일 압축)당 정지 1~1.5회 — 멘토 실수치 "장비 한두 달 1회" 환산).
    """
    if not isinstance(alert, dict):
        return False
    if str(alert_severity(alert)).upper() != _CRITICAL:
        return False

    cfg = _rtd_cfg(params)
    if violation_sensor_count(alert) < int(cfg.get("min_violation_sensors", 2)):
        return False

    if bool(cfg.get("exclude_ae_arm", True)) and _is_ae_only(alert):
        return False
    return True


def _is_ae_only(alert: dict) -> bool:
    """이 알람의 CRITICAL 근거가 **AE anomaly 단독**인가 (SPC·예측 arm 부재).

    B9 crazy 마커는 `description` 에 걸린 arm 을 싣는다(publisher._build_crazy_marker):
      · "B9 crazy: anomaly_score .. > cut .." (anomaly-only)
      · "B9 crazy: predicted_c65 .. > P99 .." (predicted — anomaly 병기 가능)
    Nelson 룰 위반(rule_id N*)이 하나라도 있으면 AE 단독이 아니다.
    """
    vs = alert.get("violations") or []
    if not isinstance(vs, (list, tuple)) or not vs:
        return False
    for v in vs:
        if not isinstance(v, dict):
            continue
        rid = str(v.get("rule_id", ""))
        if rid.upper().startswith("N"):          # Nelson 위반 존재 → AE 단독 아님
            return False
        if rid == "B9" and "predicted_c65" in str(v.get("description", "")):
            return False                          # 예측 arm 근거 → AE 단독 아님
    return True                                   # 남은 근거는 AE anomaly 뿐


# ── 본 판정 — 창 내 지속 이탈률 (2026-08-06 판별축 교체, 상황판 D-5) ─────────
def _persistent_sensors_from_counts(n1_hits: dict, total_alerts: int,
                                    params: Optional[dict] = None) -> list:
    """센서별 N1 이탈 알람 수 → **이탈률 ≥ breach_ratio 인 센서 목록** (순수 — 테스트 고정용).

    이탈률 = (그 센서가 N1 로 걸린 distinct 알람 수) / (창 내 그 챔버 총 distinct 알람 수).
    실측 근거(2026-08-06, #117 재시딩 후 10분 창):
      스톰(SC4)  : C17 44.1% · C62 39.3%  → 2개 센서가 임계 초과
      평시(대조) : 최고 C17 23.0%          → 0개 (만성 소음 센서 하나뿐)
    → 기본 breach_ratio 0.30 은 두 군집 사이의 중앙 부근.

    분모 가드(7장: "분모가 되는 baseline·임계를 검증 없이 사용 금지")는 호출자
    `persistent_breach` 가 total_alerts 로 수행한다 — 여기서는 0 분모만 방어.
    """
    if not total_alerts:
        return []
    ratio = float(_rtd_cfg(params).get("breach_ratio", 0.30))
    return sorted(s for s, h in (n1_hits or {}).items()
                  if h / total_alerts >= ratio)


def persistent_breach(conn, chamber_id: str,
                      params: Optional[dict] = None) -> tuple:
    """최근 `rtd.window_minutes` 창의 **지속 이탈률** 판정 재료 조회 (DB — spc_violations).

    반환 (sensors, total_alerts):
      sensors      = N1 이탈률 ≥ breach_ratio 인 센서 목록 (정렬)
      total_alerts = 창 내 그 챔버 총 distinct 알람 수 (분모 — 가드 판정용)

    분모 = 전 severity 알람 (CRITICAL 한정 아님) — 실측 임계(44%/23%)를 같은 정의로
    쟀기 때문이다. 분자 = rule_id='N1'(한계 이탈) 알람만: 지속 이탈의 정의가 "관리선을
    실제로 넘는다"이므로 패턴 룰(N2~N8)은 세지 않는다.

    ⚠️ 데이터 원천이 **spc_violations(B consumer 적재)** 라서, consumer 가 죽어 있으면
    분모가 말라 RTD 도 조용히 침묵한다 — 호출자가 min_window_alerts 가드에 걸리면
    WARNING 을 남기는 이유 (2026-08-06 producer 중복 사고에서 40분간 적재 0건 실측).
    """
    mins = int(_rtd_cfg(params).get("window_minutes", 10))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT sensor_id, count(DISTINCT alert_id)
                 FROM spc_violations
                WHERE chamber_id = %s AND rule_id = 'N1'
                  AND created_at > NOW() - make_interval(mins => %s)
                GROUP BY sensor_id""",
            (chamber_id, mins),
        )
        n1_hits = {r[0]: int(r[1]) for r in cur.fetchall()}
        cur.execute(
            """SELECT count(DISTINCT alert_id)
                 FROM spc_violations
                WHERE chamber_id = %s
                  AND created_at > NOW() - make_interval(mins => %s)""",
            (chamber_id, mins),
        )
        row = cur.fetchone()
        total = int(row[0] if row and row[0] else 0)
    return _persistent_sensors_from_counts(n1_hits, total, params), total


# ── 신호 파일 (시뮬레이터 소비 채널) ─────────────────────────────────────────
def signal_path(chamber_id: str) -> Path:
    """chamber inhibit 신호 파일 경로 — 드롭·해제·시뮬 poll 이 같은 정의를 쓴다."""
    return INHIBIT_DIR / f"{chamber_id}.json"


def _drop_signal(chamber_id: str, incident_id: str, alert_id: str) -> Path:
    """신호 파일 원자 드롭 (tmp + os.replace — 7장). Windows 잠금 대비 짧은 재시도."""
    INHIBIT_DIR.mkdir(parents=True, exist_ok=True)
    p = signal_path(chamber_id)
    payload = json.dumps({
        "chamber_id": chamber_id, "incident_id": incident_id,
        "trigger_alert_id": alert_id,
        "inhibited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False)
    tmp = p.with_suffix(".json.tmp")
    for attempt in (1, 2, 3):
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, p)
            return p
        except OSError:
            if attempt == 3:
                raise
            time.sleep(0.1 * attempt)
    return p                                             # pragma: no cover — 위 루프가 반환/raise


def _remove_signal(chamber_id: str) -> bool:
    """신호 파일 제거 (해제). 없으면 False — 멱등."""
    p = signal_path(chamber_id)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


# ── DB 발동·해제 ────────────────────────────────────────────────────────────
def should_inhibit_equipment(alert: dict, params: Optional[dict] = None) -> bool:
    """**장비 단위** 정지 조건 — 공용 설비 공통 원인 (멘토: "장비 단위로도 정지한다").

    신호 = RTD 경로① `tttm.reference_suspect` — fleet median 자체가 오염됐다는 뜻이고,
    이는 여러 챔버가 같은 방향으로 함께 움직였다(공통 이동) = **가스·전원·냉각 등 장비
    공용 설비 원인**이라는 판정이다. 챔버 개별 이상과 물리적 원인 계층이 다르므로 스코프도 다르다.

    `tttm.suspect_sensors`(#113)는 **그 공통 이동을 일으킨 센서 목록**이라 여기 재료다
    (`suspect_sensors≠∅` ⟺ `reference_suspect=True` — B 2026-08-06 확인). 개수 임계
    `rtd.min_common_sensors`(기본 1)로 승격 문턱을 조절할 수 있다.

    챔버 조건(`should_inhibit`)을 만족한 알람 중에서 이 조건까지 참이면 장비 스코프로 승격한다.
    `rtd.allow_equipment_scope=false` 면 승격하지 않는다(챔버 단위로만 — 보수 운영).
    """
    cfg = _rtd_cfg(params)
    if not bool(cfg.get("allow_equipment_scope", True)):
        return False
    tttm = alert.get("tttm") or {}
    if not bool(tttm.get("reference_suspect")):
        return False
    common = tttm.get("suspect_sensors")
    n = len(common) if isinstance(common, (list, tuple)) else 0
    return n >= int(cfg.get("min_common_sensors", 1))


def _chambers_from_config() -> list:
    """① `config/chamber_offsets.json`의 `offsets` 키 — 시뮬 챔버 구성의 단일 소스.

    DB·타 파트 가동과 무관하게 읽히는 유일한 소스라 1순위다. 파일 부재·형상 불량은
    빈 목록으로 흘려 다음 소스로 넘긴다 (RTD 가 예외로 죽으면 안 된다).
    """
    try:
        with open(CHAMBER_OFFSETS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        offsets = data.get("offsets") if isinstance(data, dict) else None
        if not isinstance(offsets, dict):
            return []
        return sorted(k for k in offsets if isinstance(k, str) and k)
    except (OSError, ValueError, TypeError) as e:
        log.debug("chamber_offsets.json 로드 실패(다음 소스로): %s", e)
        return []


def _chambers_from_control_limits(conn) -> list:
    """② `control_limits` distinct chamber_id — B 시딩 테이블 (폴백).

    관리선이 없으면 SPC 판정 자체가 불가능하므로 **RTD 가 발동하는 시점엔 반드시 존재**한다.
    라이브 적재(A·B consumer 가동)와 무관하게 시딩 시점부터 채워져 있어 구 소스
    `wafer_predictions` 보다 안정적이다.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT chamber_id FROM control_limits WHERE chamber_id IS NOT NULL")
            return sorted(r[0] for r in cur.fetchall() if r[0])
    except Exception as e:                               # noqa: BLE001 — RTD 생존 우선
        log.debug("control_limits 챔버 조회 실패: %s", e)
        return []


def _sibling_chambers(conn, chamber_id: str) -> list:
    """같은 장비의 형제 챔버 목록 — 현 구성은 단일 장비(4챔버)라 전 챔버가 형제다.

    **소스 우선순위** (2026-08-07 — B 리뷰 #118 반영):
      ① `config/chamber_offsets.json`(구성 단일 소스) → ② `control_limits`(B 시딩).

    구 소스 `wafer_predictions`(A 소유)는 **폐기**한다. A(예측 consumer)가 멎으면 테이블이
    비고, 그러면 `rows or [chamber_id]` 폴백이 걸려 **equipment 스코프가 조용히 chamber 로
    강등**됐다 — 형제 챔버가 안 서는데 로그 한 줄 남지 않았다. **장비 정지 의도가 다른
    파트의 가동 여부에 걸리면 안 된다**는 것이 이 교체의 요지다.

    어느 소스에서도 못 얻으면 강등 자체는 유지하되(문제 챔버는 어차피 선다 — 보수적)
    **WARNING 을 남긴다.** "장비를 세우려 했는데 하나만 섰다"가 무음으로 지나가지 않게
    하는 것이 핵심 — `min_window_alerts` 분모 가드와 같은 패턴이다.

    다장비 확장 시 이 함수만 교체한다(장비-챔버 매핑 테이블 조회로).
    """
    rows = _chambers_from_config() or _chambers_from_control_limits(conn)
    if not rows:
        log.warning("형제 챔버 목록을 얻지 못해 equipment 스코프를 chamber 로 강등 — %s "
                    "(config/chamber_offsets.json · control_limits 확인 필요)", chamber_id)
        return [chamber_id]
    if chamber_id not in rows:                   # 구성에 없는 챔버 — 자기 자신은 항상 포함
        log.warning("형제 목록에 대상 챔버가 없어 추가 — %s (구성 소스 불일치)", chamber_id)
        rows = sorted(rows + [chamber_id])
    return rows


def record_trigger(conn, incident_id: str, alert: dict,
                   params: Optional[dict] = None) -> Optional[tuple]:
    """발동 조건 충족 시 chamber_inhibits INSERT **만** 수행 (신호 드롭 없음).

    호출자의 트랜잭션 안에서 부르고, **커밋 성공 후** `drop_signal_after_commit()` 로
    신호를 떨군다 — 커밋 실패 시 '기록 없는 정지'(예외 4 ⓑ 위반)를 막는 순서 규약.
    디바운스는 부분 유니크 인덱스(chamber_id WHERE released_at IS NULL)의
    ON CONFLICT DO NOTHING 으로 DB 가 보장.

    **스코프 2단**(2026-08-06 · 멘토 "장비 단위로도 정지한다"): `should_inhibit_equipment`
    가 참이면 형제 챔버 전체에 `scope='equipment'` 행을 같은 incident_id 로 만든다.
    행이 챔버별인 이유는 신호 파일·시뮬 스킵·해제 로직을 그대로 재사용하기 위함이다.
    이미 열린 inhibit 이 있는 챔버는 ON CONFLICT 로 건너뛰므로 부분 승격도 안전하다.

    **판정 순서** (2026-08-06 판별축 교체): ① 사전 필터 `should_inhibit`(순수 — DB 비용
    없이 명백한 비대상 배제) → ② 본 판정 `persistent_breach`(창 내 지속 이탈률) →
    ③ 분모 가드(`min_window_alerts` — 미달이면 판정 불가로 보수적 미발동 + WARNING.
    consumer 적재 중단 시 RTD 가 조용히 침묵하는 상태를 로그로 드러낸다).

    반환: (targets, alert_id, scope) — targets = 신규 발동된 챔버 목록. 없으면 None.
    """
    if not auto_inhibit_enabled(params) or not should_inhibit(alert, params):
        return None
    chamber_id = str(alert.get("chamber_id") or "")
    if not chamber_id:
        return None
    alert_id = str(alert.get("alert_id") or "")

    cfg = _rtd_cfg(params)
    sensors, total = persistent_breach(conn, chamber_id, params)
    min_alerts = int(cfg.get("min_window_alerts", 30))
    if total < min_alerts:                       # 분모 가드 (7장) — 판정 불가 ≠ 발동
        log.warning("RTD 판정 보류 — %s 창 내 알람 %d건 < %d (consumer 적재 확인 필요할 수 있음)",
                    chamber_id, total, min_alerts)
        return None
    if len(sensors) < int(cfg.get("min_persistent_sensors", 2)):
        return None                              # 지속 이탈 센서 부족 — 평시 소음 (미발동)
    log.info("RTD 본 판정 충족 — %s 지속 이탈 센서 %s (창 알람 %d건)",
             chamber_id, ",".join(sensors), total)

    equip = should_inhibit_equipment(alert, params)
    scope = "equipment" if equip else "chamber"
    equipment_id = (str(alert.get("equipment_id") or "") or _DEFAULT_EQUIPMENT) if equip else None
    targets = _sibling_chambers(conn, chamber_id) if equip else [chamber_id]

    hit = []
    with conn.cursor() as cur:
        for ch in targets:
            cur.execute(
                """
                INSERT INTO chamber_inhibits
                    (chamber_id, incident_id, trigger_alert_id, scope, equipment_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chamber_id) WHERE released_at IS NULL DO NOTHING
                RETURNING id
                """,
                (ch, incident_id, alert_id, scope, equipment_id),
            )
            if cur.fetchone() is not None:
                hit.append(ch)
    return (hit, alert_id, scope) if hit else None


def drop_signal_after_commit(chambers, incident_id: str, alert_id: str,
                             scope: str = "chamber") -> None:
    """커밋 성공 후 신호 드롭 — record_trigger 와 쌍 (순서 규약: 기록 → 커밋 → 정지).

    chambers: 챔버 id 문자열 또는 목록(장비 스코프면 형제 전체).
    """
    if isinstance(chambers, str):
        chambers = [chambers]
    for ch in chambers:
        _drop_signal(ch, incident_id, alert_id)
    label = "장비" if scope == "equipment" else "챔버"
    log.warning("■ %s 자동 inhibit — %s (incident=%s, alert=%s) [예외 4: 해제는 requal 승인 하류만]",
                label, ", ".join(chambers), incident_id, alert_id)


def maybe_trigger(conn, incident_id: str, alert: dict, params: Optional[dict] = None) -> bool:
    """단독 사용 편의 래퍼 — 기록 + 즉시 드롭 (자체 커밋 없는 단순 경로용)."""
    hit = record_trigger(conn, incident_id, alert, params)
    if hit is None:
        return False
    drop_signal_after_commit(hit[0], incident_id, hit[1], hit[2])
    return True


def release(conn, chamber_id: str, incident_id: str, qual_id: str = "",
            released_by: str = "") -> int:
    """requal 승인 하류 해제 (예외 4 ⓒ) — 열린 inhibit released 처리 + 신호 제거.

    incident 불일치라도 chamber 가 requalified 됐으면 닫는다 (재인증 = 그 챔버의
    건전성 확인이므로).

    **장비 스코프 일괄 해제 (발동-해제 대칭, 2026-08-06)**: 이 챔버의 열린 행이
    `scope='equipment'` 면 **같은 발동 incident 의 형제 행 전부**를 함께 닫는다 —
    장비 정지의 원인은 공용 설비이므로, 그 해소 확인(requal)은 장비 전체의 재가동
    근거다 (멘토: up 시 샘플 확인 관행 — 챔버 하나의 requal 로 장비를 되살린다).
    이 확장이 없으면 장비 정지 후 형제 3챔버가 **영구 정지**로 남는다.

    반환 = 닫은 행 수. 커밋은 호출자 소관.
    """
    with conn.cursor() as cur:
        # ① 이 챔버의 열린 행 스코프 확인 → 해제 대상 확정
        cur.execute(
            """SELECT scope, incident_id FROM chamber_inhibits
                WHERE chamber_id = %s AND released_at IS NULL
                ORDER BY inhibited_at DESC LIMIT 1""",
            (chamber_id,),
        )
        row = cur.fetchone()
        equip_incident = row[1] if (row and row[0] == "equipment") else None

        if equip_incident:                       # 장비 스코프 — 같은 발동 incident 형제 일괄
            cur.execute(
                """UPDATE chamber_inhibits
                      SET released_at = NOW(), released_by = %s, release_qual_id = %s,
                          release_incident_id = %s
                    WHERE incident_id = %s AND scope = 'equipment' AND released_at IS NULL
                    RETURNING chamber_id""",
                (released_by, qual_id, incident_id, equip_incident),
            )
            targets = [r[0] for r in cur.fetchall()]
        else:                                    # 챔버 스코프 — 이 챔버만
            cur.execute(
                """UPDATE chamber_inhibits
                      SET released_at = NOW(), released_by = %s, release_qual_id = %s,
                          release_incident_id = %s
                    WHERE chamber_id = %s AND released_at IS NULL
                    RETURNING chamber_id""",
                (released_by, qual_id, incident_id, chamber_id),
            )
            targets = [r[0] for r in cur.fetchall()]
        n = len(targets)

    removed = 0
    for ch in (targets or [chamber_id]):         # 행이 없어도 잔류 신호는 청소 (멱등)
        removed += 1 if _remove_signal(ch) else 0
    if n or removed:
        log.info("□ %s inhibit 해제 — %s (%d행, incident=%s, qual=%s, by=%s)",
                 "장비" if equip_incident else "챔버",
                 ", ".join(targets) if targets else chamber_id,
                 n, incident_id, qual_id, released_by)
    return n
