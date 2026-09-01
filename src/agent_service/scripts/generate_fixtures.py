"""generate_fixtures.py — fdc.alert mock 30건 결정적 생성 (C4-1 Day1).

DoD: fixtures/alerts/ 30건 + 케이스표(README) + validate 통과(의도적 파손 1건만 실패).

매트릭스 (손으로 만들지 않고 코드로 박음 — seed 고정, 재실행 시 바이트 동일):
  · 코어 24 = trigger 4종 × context_score 구간 6종
      trigger  : baseline_aging / process_shift / equipment_fault / escalate
                 (= Supervisor 4지선다 정답 — 프롬프트 라이브러리 §4 판별 매트릭스)
      context  : below_gate(22) / gate_edge(31) / warning(48) / elevated(66) / high(84) / critical(95)
                 (B7: <31 미가동 게이트 경계·상한을 모두 커버)
  · 엣지 7 = trigger_signal / reference_suspect / multi-violation / crazy_wafer /
            regime_signal_only / **BROKEN(의도적 파손 1건)** / temp_drift(온도 가변 축 — 2026-07-26)

수치 소스: config/anchor_constants.json (센서 앵커) + config/params.yaml(control_limit_k_sigma).
  관리한계 = center ± k·settled_std, current_value 는 위반 패턴에 따라 한계 밖으로 배치.
  → 창작 수치 아님(앵커·계약 기반), 결정적.

정합: 생성 즉시 AlertModel(Contract §3) 로 검증 후 기록. 파손 1건만 검증 우회하여 raw 기록.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# Windows 콘솔(cp949) UnicodeEncodeError 방지 — UTF-8 강제
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_service.app.schemas.alert import AlertModel  # noqa: E402

ANCHOR_PATH = SERVICE_ROOT / "config" / "anchor_constants.json"
PARAMS_PATH = REPO_ROOT / "config" / "params.yaml"
OUT_DIR = SERVICE_ROOT / "fixtures" / "alerts"

BASE_TS = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)

# --- TTTM 의미 정합 (W9 · 2026-08-06 · `--align-tttm`) --------------------------------
#
# 🔴 **원본 값이 판정 규칙과 반대였다** (round20 실측으로 발견).
#    프롬프트: *"급변 없는 N3 추세 + **TTTM 갭 낮음**(분포는 이웃과 정상) = 선이 낡은 것 → ③"*
#    그런데 fixture 는 `baseline_aging 4.1%` > `process_shift 1.6%` — **정반대**였다.
#    이 파일의 주석조차 baseline_aging 을 *"TTTM 갭 낮음"* 이라 써놓고 값은 4.1 을 넣었다.
#
#    round18 이 ③을 9/9 맞춘 것은 규칙을 따라서가 아니라 **Supervisor 예시를 베껴서**였고
#    (예시 복사 42회), 예시를 중립화하자(W56) ③이 9/9 → 1/9 로 무너졌다.
#    **LLM 은 규칙을 정직하게 적용했고, 규칙대로 하면 틀리는 fixture 였다.**
#
# **도메인 근거** — 어느 쪽을 고쳐야 하는지:
#    · ③ 기준선 노후 = *관리선이 낡은 것*. 챔버 자체는 이웃과 다르지 않다 → 갭 **낮아야** 맞다
#    · ② 공정 조건 이탈 = *그 챔버의 공정이 틀어진 것* → 이웃 대비 갭이 **생겨야** 맞다
#    · ① 장비 이상 = 프롬프트가 *"이웃 수준의 약 2배"* 를 요구 → ②의 약 2배
#   → **프롬프트가 맞고 fixture 가 뒤집혀 있었다.**
#
# ⚠️ 기본값(`alerts/`)은 **건드리지 않는다** — round3~20 의 대조군이다.
#    `--align-tttm --out alerts_v2` 로 새 세트를 만들어 두 세트를 나란히 잰다.
_TTTM_BY_TRIGGER = {
    #                        원본(score, gap)   정합(score, gap)
    "baseline_aging":       ((2.3, 4.1),        (0.7, 0.8)),   # 이웃과 정상
    "process_shift":        ((1.2, 1.6),        (2.2, 4.2)),   # 그 챔버만 이동
    "equipment_fault":      ((3.6, 8.9),        (3.6, 8.9)),   # 유지 — ②의 약 2배가 이미 성립
    "escalate":             ((2.0, 3.2),        (2.0, 3.2)),   # 유지 — 레짐은 갭이 애매한 게 맞다
    # E7(47_process_shift_temp_drift)은 코어와 **원본 값이 미묘하게 다르다**(1.3/1.7 vs 1.2/1.6).
    # 정합 세트에서는 같은 정답이므로 코어 ②와 같은 값을 쓰되, **기본 세트에서는 원본을 그대로
    # 돌려줘야 한다** — 안 그러면 대조군 파일이 바뀐다(2026-08-06 바이트 대조에서 잡힘).
    "process_shift_temp":   ((1.3, 1.7),        (2.2, 4.2)),
}
_ALIGN_TTTM = False   # --align-tttm 으로 켠다

# --- description 현실화 (W9 · 2026-08-08 · `--realistic-desc`) -----------------------
#
# 🔴 **fixture 의 description 이 정답을 자연어로 적고 있었다** (round26 실측으로 발견).
#    trigger 종류마다 다른 문장이 들어가 4종이 1:1 로 갈린다:
#        baseline_aging  "6 points continuously increasing"
#        process_shift   "gradual level shift (process drift)"
#        equipment_fault "single point beyond 3sigma (spike)"
#        escalate        "regime drift (C12 동반 — 레짐 도장)"
#    셋 다 같은 N3 인데 문구가 다르다. 즉 SHAP·TTTM·관리선을 안 봐도 이 한 줄로 4지선다가
#    풀린다 — verdict 는 LLM 이 payload 를 읽고 내므로(`eval_supervisor.py:341`) 실제 단서가 된다.
#
# **왜 이렇게 됐나 (아무도 실수하지 않았다).** 이 생성기는 2026-07-13(C4-1 뼈대), B 의
#    Nelson 엔진은 2026-07-22 다. 만들 당시엔 참고할 구현이 없어 계약서 예시
#    (`docs/API_Contract_명세서.md:190` = `"6 points continuously increasing"`)를 따랐고,
#    나머지 종류는 같은 결로 창작했다. 그 창작이 하필 정답과 1:1 이 됐다.
#
# **실제 B 가 넣는 값**은 룰 기준 템플릿이다 — `nelson_engine.py:242`:
#        f"{spec.id}: {spec.window}점 창 ({w_range})"      예: "N3: 6점 창 (W0..W5)"
#    Nelson 엔진은 "6점이 연속 상승했다" 까지만 알고 **그것이 노후인지 공정 이탈인지는 모른다**
#    — 그걸 가리는 것이 Supervisor 의 존재 이유다(헌법 1-4 4지선다). 그래서 실제 알람에서는
#    N3 세 종류가 **전부 같은 문자열**이 된다.
#
# 이 플래그는 그 형식을 재현해 **실전과 같은 조건**으로 만든다. 창 크기는 매직넘버로 두지 않고
# B 의 레지스트리(`nelson_rules.RULES`)를 단일 소스로 읽는다 (6-1).
_REALISTIC_DESC = False   # --realistic-desc 으로 켠다


def _rule_windows() -> dict[str, int]:
    """룰 → 창 크기. 단일 소스 = B `nelson_rules.RULES` (하드코딩 금지)."""
    from src.agent_b_spc.nelson_rules import RULES  # noqa: PLC0415
    return {r.id: r.window for r in RULES}


def _realistic_description(rule_id: str) -> str:
    """B 실제 형식 재현 — `nelson_engine.py:242` 와 같은 조립.

    wafer 범위는 `W0..W{n-1}` 로 둔다(B 테스트가 못박은 형태 —
    `test_nelson_engine.py:127` 이 `".." in desc and "W0" in desc` 를 단언한다).
    **같은 룰이면 trigger 종류와 무관하게 같은 문자열**이 되는 것이 이 함수의 요점이다.
    """
    n = _rule_windows()[rule_id]
    return f"{rule_id}: {n}점 창 (W0..W{n - 1})"


def _tttm(trigger: str, sensor: str) -> dict:
    """trigger 별 tttm 블록 — `--align-tttm` 이면 정합값을 쓴다."""
    score, gap = _TTTM_BY_TRIGGER[trigger][1 if _ALIGN_TTTM else 0]
    return {"reference": "fleet_median", "score": score, "top_gap_sensor": sensor,
            "gap_pct": gap, "reference_suspect": False}

# context_score 구간 (B7 게이트 경계 커버)
# 2026-07-21 6 → 10 확장: 채점 표본이 판정별 5건이라 오답 1건이 20%(전체 5%)를 흔들어
# 55%~70% 를 구분할 수 없었다(실측 R3~R5b). 판정별 9건(채점 36건)으로 측정 노이즈를 줄인다.
# ※ 판정은 context_score 와 무관해야 하므로(게이트 역할만), 같은 신호를 여러 번 시행하는
#   효과 = 측정 반복 → 노이즈 감소. 센서도 idx%3 순환이라 함께 다양해진다.
BANDS: list[tuple[str, int]] = [
    ("below_gate", 22),   # < 31 → Agent 미가동(기록만). 유일한 게이트 차단 구간
    ("gate_edge", 31),    # 정확히 임계
    ("low", 38),
    ("warning", 48),
    ("mid", 55),
    ("elevated", 66),
    ("upper", 73),
    ("high", 84),
    ("severe", 90),
    ("critical", 95),
]

_anchors: dict[str, dict] = {}
_k_sigma: float = 3.0
_chamber_count: int = 4  # params.simulator.chamber_count 로 덮어씀


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _chamber(idx: int) -> str:
    """챔버 배분 — 개수는 params(simulator.chamber_count) 정본. 하드코딩 금지(6→4 전환 재발 방지)."""
    return f"SIM_CH_{idx % _chamber_count + 1}"


def _anchor(sensor: str) -> dict:
    if sensor not in _anchors:
        raise KeyError(f"anchor_constants.json 에 {sensor} 없음 — build_anchor_constants 재실행 필요")
    return _anchors[sensor]


def _round(x: float) -> float:
    return round(x, 2)


def make_violation(
    sensor: str,
    rule_id: str,
    severity: str,
    window: str,
    description: str,
    side: int,           # +1 = 상한 초과, -1 = 하한 미달
    beyond_sigma: float,  # 한계선 너머 몇 σ (0.7=추세 살짝, 3.5=급변)
) -> dict:
    """앵커 기반 관리한계 + 패턴에 맞는 current_value 로 violation 생성."""
    a = _anchor(sensor)
    center, sstd = a["center"], a["settled_std"]
    upper = center + _k_sigma * sstd
    lower = center - _k_sigma * sstd
    if side > 0:
        current = upper + beyond_sigma * sstd
    else:
        current = lower - beyond_sigma * sstd
    return {
        "rule_id": rule_id,
        "sensor": sensor,
        "sensor_name": a["name"],
        "window": window,
        "severity": severity,
        # --realistic-desc 이면 호출자가 넘긴 문구를 버리고 B 형식으로 덮는다.
        #   호출부 9곳을 고치지 않는 이유 = 두 세트를 **같은 코드로** 내야 나머지 필드가
        #   바이트 동일해지고, 그래야 description 하나만 바뀐 대조가 성립한다.
        "description": _realistic_description(rule_id) if _REALISTIC_DESC else description,
        "current_value": _round(current),
        "limit_version": "v1",
        "control_limit_upper": _round(upper),
        "control_limit_lower": _round(lower),
    }


def _distinct3(*candidates: str) -> list[str]:
    """앞에서부터 중복 없이 3개 선택 — shap_top3는 서로 다른 센서여야 한다.

    (SHAP 상위 3 기여 센서는 본래 서로 다른 센서. 회전 센서가 채움 후보와 겹칠 때
     중복이 생기지 않도록 여기서 제거한다. 후보는 3개 이상 확보되게 넉넉히 넘길 것.)
    """
    out: list[str] = []
    for c in candidates:
        if c not in out:
            out.append(c)
        if len(out) == 3:
            break
    return out


def _prediction(wafer_id: str, predicted: float, shap: list[str], spc: list[dict]) -> dict:
    assert len(shap) == len(set(shap)), f"shap_top3 중복: {shap}"  # 생성 시 자기검증
    return {
        "wafer_id": wafer_id,
        "predicted_c65": predicted,
        "shap_top3": shap,
        "spc_flags": spc,
    }


# --- trigger 템플릿 (4지선다 정답별 SPC 시그니처) --------------------------------
def build_core_alert(trigger: str, band_name: str, score: int, idx: int) -> tuple[dict, dict]:
    """(alert_dict, meta_row) — meta_row 는 README 케이스표용."""
    chamber = _chamber(idx)
    seq = idx + 1
    ts = BASE_TS + timedelta(minutes=idx)
    wafer_id = f"C64_{1000 + idx}"
    alert_id = f"ALERT-20260713-{chamber.replace('_', '')}-{seq:04d}"

    suspect_window = {
        "start_wafer": f"C64_{1000 + idx - 6}",
        "end_wafer": wafer_id,
        "basis": "추세 룰 시작점 소급",
    }

    if trigger == "baseline_aging":
        # PM 후 장기 경과 · N3 추세 · 급변/anomaly 없음 · TTTM 갭 낮음 → ③ 실력치 재설정
        sensor = ["C11", "C9", "C62"][idx % 3]
        violations = [make_violation(sensor, "N3", "WARNING", "settled",
                                     "6 points continuously increasing", side=+1, beyond_sigma=0.7)]
        tttm = _tttm("baseline_aging", sensor)
        pred = _prediction(wafer_id, 715.2, _distinct3(sensor, "C62", "C17", "C11", "C9"),
                           [{"sensor": sensor, "rule": "N3"}])
        sw = suspect_window
        verdict, key = "baseline_aging → limit_option", f"{sensor}/N3 settled"

    elif trigger == "process_shift":
        # SHAP 상위 = 손잡이 축(가스/파워) · 완만 이동 · anomaly 낮음 → ② Recipe R2R
        meas, knob = [("C15", "C4"), ("C16", "C5"), ("C31", "C1")][idx % 3]
        violations = [make_violation(meas, "N3", "WARNING", "settled",
                                     "gradual level shift (process drift)", side=+1, beyond_sigma=0.6)]
        tttm = _tttm("process_shift", meas)
        pred = _prediction(wafer_id, 760.5, _distinct3(meas, knob, "C1", "C11", "C17"),
                           [{"sensor": meas, "rule": "N3"}])
        sw = suspect_window
        verdict, key = "process_shift → recipe_option", f"{meas}(→knob {knob})/N3"

    elif trigger == "equipment_fault":
        # N1 CRITICAL 급변 · anomaly 동반 · 단일 챔버 · 물리 신호 → ① 정비 + wafer 처분
        sensor = ["C31", "C32", "C62"][idx % 3]
        violations = [make_violation(sensor, "N1", "CRITICAL", "transient",
                                     "single point beyond 3σ (spike)", side=+1, beyond_sigma=3.5)]
        tttm = _tttm("equipment_fault", sensor)
        pred = _prediction(wafer_id, 1210.0, _distinct3(sensor, "C11", "C62", "C31"),
                           [{"sensor": sensor, "rule": "N1"}])
        sw = None  # 급변 이벤트 — 추세 구간 없음
        verdict, key = "equipment_fault → manual_option", f"{sensor}/N1 CRITICAL"

    else:  # escalate — 레짐 도장(C12)이 원인 후보 상위에 동반 → 튜닝 금지 → ④ 에스컬레이션
        # ⚠️ 이 케이스가 ④인 이유는 "센서가 C17이라서"가 아니라 **C12(레짐 도장)가 함께 있어서**다.
        #   온도는 2026-07-26 멘토 확정으로 가변 축이 됐고, C12 가 빠진 대조군은 E7(temp_drift)이 ②로 간다.
        sensor = "C17"
        violations = [make_violation(sensor, "N3", "WARNING", "settled",
                                     "regime drift (C12 동반 — 레짐 도장)", side=+1, beyond_sigma=0.8)]
        tttm = _tttm("escalate", sensor)
        pred = _prediction(wafer_id, 690.0, ["C17", "C12", "C33"], [{"sensor": sensor, "rule": "N3"}])
        sw = suspect_window
        verdict, key = "escalate → regime_signal", f"{sensor}(regime)/N3"

    alert = {
        "alert_id": alert_id,
        "timestamp": _iso(ts),
        "chamber_id": chamber,
        "violations": violations,
        "tttm": tttm,
        "context_score": score,
        "prediction_context": pred,
    }
    if sw is not None:
        alert["suspect_window"] = sw

    meta = {
        "trigger": trigger, "band": band_name, "context_score": score,
        "chamber": chamber, "signature": key, "expected_verdict": verdict,
        "gate": "OPEN" if score >= 31 else "CLOSED(기록만)", "valid": True,
    }
    return alert, meta


# --- 엣지 6종 -------------------------------------------------------------------
def build_edges(start_idx: int) -> list[tuple[dict, dict, str]]:
    """[(alert, meta, note)] — 마지막 1건은 의도적 파손(valid=False)."""
    out: list[tuple[dict, dict, str]] = []

    def base(idx: int) -> tuple[str, str, str, str, datetime]:
        chamber = _chamber(idx)
        seq = idx + 1
        return (
            f"ALERT-20260713-{chamber.replace('_', '')}-{seq:04d}",
            chamber, f"C64_{1000 + idx}",
            f"C64_{1000 + idx}", BASE_TS + timedelta(minutes=idx),
        )

    # E1 — trigger_signal 존재 (계약 본문 미정의 optional 필드 — B 규격확인 플래그)
    i = start_idx
    aid, ch, waf, _, ts = base(i)
    out.append((
        {
            "alert_id": aid, "timestamp": _iso(ts), "chamber_id": ch,
            "violations": [make_violation("C11", "N3", "WARNING", "settled",
                                          "6 points continuously increasing", +1, 0.7)],
            "tttm": {"reference": "fleet_median", "score": 2.1, "top_gap_sensor": "C11",
                     "gap_pct": 3.8, "reference_suspect": False},
            "context_score": 55,
            "prediction_context": _prediction(waf, 712.0, ["C11", "C62", "C17"],
                                               [{"sensor": "C11", "rule": "N3"}]),
            "trigger_signal": {"raw_rule": "N3", "note": "B 규격 미확정 — optional 파싱 확인용"},
        },
        {"trigger": "edge", "band": "trigger_signal", "context_score": 55, "chamber": ch,
         "signature": "trigger_signal 존재", "expected_verdict": "baseline_aging(참고)",
         "gate": "OPEN", "valid": True},
        "trigger_signal(계약 미정의) 보존 파싱 검증",
    ))

    # E2 — reference_suspect=true (다챔버 공통 원인 → 역방향 룰 → ④ escalate)
    i += 1
    aid, ch, waf, _, ts = base(i)
    out.append((
        {
            "alert_id": aid, "timestamp": _iso(ts), "chamber_id": ch,
            "violations": [make_violation("C11", "N3", "WARNING", "settled",
                                          "level shift (다챔버 동시)", +1, 0.9)],
            "tttm": {"reference": "fleet_median", "score": 2.8, "top_gap_sensor": "C11",
                     "gap_pct": 4.5, "reference_suspect": True},
            "context_score": 70,
            "prediction_context": _prediction(waf, 705.0, ["C11", "C62", "C31"],
                                               [{"sensor": "C11", "rule": "N3"}]),
        },
        {"trigger": "edge", "band": "reference_suspect", "context_score": 70, "chamber": ch,
         "signature": "tttm.reference_suspect=true", "expected_verdict": "escalate(참조 의심)",
         "gate": "OPEN", "valid": True},
        "reference_suspect 참(공통 원인) — 역방향 룰",
    ))

    # E3 — multi-violation (violations[] 2건)
    i += 1
    aid, ch, waf, _, ts = base(i)
    out.append((
        {
            "alert_id": aid, "timestamp": _iso(ts), "chamber_id": ch,
            "violations": [
                make_violation("C31", "N1", "CRITICAL", "transient", "single point beyond 3σ", +1, 3.2),
                make_violation("C11", "N3", "WARNING", "settled", "6 points continuously increasing", +1, 0.7),
            ],
            "tttm": {"reference": "fleet_median", "score": 3.1, "top_gap_sensor": "C31",
                     "gap_pct": 7.2, "reference_suspect": False},
            "context_score": 78,
            "prediction_context": _prediction(waf, 980.0, ["C31", "C11", "C62"],
                                               [{"sensor": "C31", "rule": "N1"}, {"sensor": "C11", "rule": "N3"}]),
        },
        {"trigger": "edge", "band": "multi_violation", "context_score": 78, "chamber": ch,
         "signature": "violations 2건(N1+N3)", "expected_verdict": "equipment_fault(급변 우선)",
         "gate": "OPEN", "valid": True},
        "복수 위반 배열 파싱 + max_severity 검증",
    ))

    # E4 — crazy wafer (predicted_c65 > 1572=B9 → 자동 HOLD 후보)
    i += 1
    aid, ch, waf, _, ts = base(i)
    out.append((
        {
            "alert_id": aid, "timestamp": _iso(ts), "chamber_id": ch,
            "violations": [make_violation("C62", "N1", "CRITICAL", "transient",
                                          "single point beyond 3σ (spike)", +1, 4.0)],
            "tttm": {"reference": "fleet_median", "score": 4.2, "top_gap_sensor": "C62",
                     "gap_pct": 11.5, "reference_suspect": False},
            "context_score": 92,
            "prediction_context": _prediction(waf, 1650.0, ["C62", "C31", "C11"],
                                               [{"sensor": "C62", "rule": "N1"}]),
        },
        {"trigger": "edge", "band": "crazy_wafer", "context_score": 92, "chamber": ch,
         "signature": "predicted_c65 1650 > 1572(B9)", "expected_verdict": "equipment_fault + HOLD",
         "gate": "OPEN", "valid": True},
        "crazy wafer(B9 초과) — disposition HOLD 후보",
    ))

    # E5 — regime_signal_only (SHAP 상위 전부 레짐 신호 C12/C17/C33 → 튜닝 금지)
    i += 1
    aid, ch, waf, _, ts = base(i)
    out.append((
        {
            "alert_id": aid, "timestamp": _iso(ts), "chamber_id": ch,
            "violations": [make_violation("C12", "N5", "WARNING", "settled",
                                          "post-PM level shift (레짐)", +1, 1.1)],
            "tttm": {"reference": "fleet_median", "score": 1.8, "top_gap_sensor": "C12",
                     "gap_pct": 2.4, "reference_suspect": False},
            "context_score": 60,
            "prediction_context": _prediction(waf, 700.0, ["C12", "C17", "C33"],
                                               [{"sensor": "C12", "rule": "N5"}]),
        },
        {"trigger": "edge", "band": "regime_signal_only", "context_score": 60, "chamber": ch,
         "signature": "SHAP 전부 레짐 신호(C12/C17/C33)", "expected_verdict": "escalate(튜닝 금지)",
         "gate": "OPEN", "valid": True},
        "레짐 신호 단독 — recipe hypothesis=regime_signal",
    ))

    # E6 — ★ 의도적 파손 (validate 가 잡아야 함): context_score 범위 초과(130)
    #   ※ 파손은 번호를 고정한다(46) — 테스트·발행 도구가 파일명으로 참조한다.
    #     새 엣지는 이 뒤에 붙인다(E7~).
    i += 1
    aid, ch, waf, _, ts = base(i)
    out.append((
        {
            "alert_id": aid, "timestamp": _iso(ts), "chamber_id": ch,
            "violations": [make_violation("C11", "N3", "WARNING", "settled",
                                          "6 points continuously increasing", +1, 0.7)],
            "tttm": {"reference": "fleet_median", "score": 2.0, "top_gap_sensor": "C11",
                     "gap_pct": 3.5, "reference_suspect": False},
            "context_score": 130,  # ← 파손: 0~100 범위 초과 (AlertModel ge=0,le=100 위반)
            "prediction_context": _prediction(waf, 710.0, ["C11", "C62", "C17"],
                                               [{"sensor": "C11", "rule": "N3"}]),
        },
        {"trigger": "edge", "band": "BROKEN", "context_score": 130, "chamber": ch,
         "signature": "context_score=130 (범위 초과)", "expected_verdict": "❌ 검증 실패(의도적)",
         "gate": "-", "valid": False},
        "★의도적 파손 — validate 가 이 1건만 실패시켜야 통과",
    ))

    # E7 — 온도 추세 (C17 N3 · C12 미동반) → ② process_shift (temp_target 튜닝)
    #   2026-07-26 멘토 확정("온도는 레짐이 아니라 가변 축")으로 열린 **신규 경로**의 유일한 커버리지다.
    #   기존 escalate 10건(31~40)은 전부 C17 + C12 동반이라 여전히 ④로 남는다 —
    #   즉 이 fixture 는 "C12 가 빠지면 온도가 튜닝으로 간다"는 갈림길 자체를 재는 대조군이다.
    #   trigger=process_shift 로 두면 파일명이 eval 정답지(expected_verdict)에 잡혀 채점된다.
    i += 1
    aid, ch, waf, _, ts = base(i)
    out.append((
        {
            "alert_id": aid, "timestamp": _iso(ts), "chamber_id": ch,
            "violations": [make_violation("C17", "N3", "WARNING", "settled",
                                          "gradual temperature drift (급변 없음 — 손잡이 축)", +1, 0.6)],
            # E7 은 파일명이 `47_process_shift_temp_drift` 라 **채점 대상**이다(정답 = process_shift).
            # 따라서 코어 process_shift 와 같은 갭 규약을 따라야 한다 — 안 그러면 같은 정답인데
            # 신호가 다른 두 무리가 생겨 규칙이 어느 쪽에도 안 맞는다.
            "tttm": _tttm("process_shift_temp", "C17"),
            "context_score": 64,
            # SHAP 상위에 C12 가 없다 — 레짐 도장 미동반이 이 케이스의 핵심 조건.
            "prediction_context": _prediction(waf, 758.0, ["C17", "C9", "C11"],
                                               [{"sensor": "C17", "rule": "N3"}]),
            "suspect_window": {"start_wafer": f"C64_{1000 + i - 6}", "end_wafer": waf,
                               "basis": "추세 룰 시작점 소급"},
        },
        {"trigger": "process_shift", "band": "temp_drift", "context_score": 64, "chamber": ch,
         "signature": "C17/N3 settled · C12 미동반", "expected_verdict": "process_shift → recipe_option(temp_target)",
         "gate": "OPEN", "valid": True},
        "온도 가변 축(2026-07-26 멘토 확정) — 급변·C12 없는 온도 추세는 temp_target 튜닝",
    ))

    return out


def main() -> None:
    global _anchors, _k_sigma, _chamber_count, OUT_DIR, _ALIGN_TTTM, _REALISTIC_DESC
    ap = argparse.ArgumentParser(description="fdc.alert fixture 생성 (결정적)")
    ap.add_argument("--realistic-desc", action="store_true",
                    help="violations[].description 을 B 실제 형식(룰 기준 템플릿)으로 낸다. "
                         "기본 세트의 문구는 trigger 종류마다 달라 정답이 노출된다 — "
                         "실전 조건으로 재려면 이 플래그를 쓴다 (상세는 파일 상단 주석)")
    ap.add_argument("--align-tttm", action="store_true",
                    help="TTTM 갭을 판정 규칙과 정합시킨다 (W9). ③은 낮게·②는 높게 — "
                         "원본은 정반대라 규칙대로 판정하면 틀린다. 기본 세트에는 쓰지 말 것")
    ap.add_argument("--out", type=str, default=None, metavar="DIR",
                    help=f"출력 디렉토리 (기본 {OUT_DIR.relative_to(REPO_ROOT)})")
    ap.add_argument("--force", action="store_true",
                    help="기존 *.json 이 있어도 지우고 다시 쓴다 (아래 가드 참조)")
    args = ap.parse_args()
    if args.out:
        OUT_DIR = Path(args.out).expanduser().resolve()
    _REALISTIC_DESC = bool(args.realistic_desc)
    if _REALISTIC_DESC:
        w = _rule_windows()
        print("[generate_fixtures] --realistic-desc — description 을 B 형식으로 낸다 "
              f"(예: {_realistic_description('N3')}). 룰 창 크기 출처 = nelson_rules.RULES "
              f"({', '.join(f'{k}={v}' for k, v in sorted(w.items()))})")
    _ALIGN_TTTM = bool(args.align_tttm)
    if _ALIGN_TTTM:
        print("[generate_fixtures] --align-tttm — TTTM 갭을 판정 규칙과 정합시킨다 "
              f"(③ {_TTTM_BY_TRIGGER['baseline_aging'][1][1]}% · "
              f"② {_TTTM_BY_TRIGGER['process_shift'][1][1]}% · "
              f"① {_TTTM_BY_TRIGGER['equipment_fault'][1][1]}%)")

    _anchors = json.loads(ANCHOR_PATH.read_text(encoding="utf-8"))["sensors"]
    params = yaml.safe_load(PARAMS_PATH.read_text(encoding="utf-8"))
    _k_sigma = float(params["limit_engine"]["control_limit_k_sigma"])
    _chamber_count = int(params["simulator"]["chamber_count"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 기존 fixtures 정리 (재실행 결정성 — stale 제거)
    #
    # 🔴 **덮어쓰기 가드 (2026-08-06 신설).** 이 스크립트는 예전엔 아무 말 없이 대상 디렉토리의
    #    `*.json` 을 전부 지웠다. 그래서 무심코 실행하면 **평가 기준 세트가 사라지고**, 그 사실이
    #    화면에 한 줄도 안 뜬다. W9 에서 `alerts_v2` 를 만들며 이 스크립트를 반복 실행할 예정이라
    #    (그때마다 손이 기본 경로로 갈 수 있다) 여기서 막는다.
    #    새 디렉토리(빈 곳)는 그냥 되고, **이미 뭔가 있는 곳만** --force 를 요구한다.
    existing = sorted(OUT_DIR.glob("*.json"))
    if existing and not args.force:
        raise SystemExit(
            f"[중단] {OUT_DIR} 에 이미 fixture {len(existing)}건이 있다.\n"
            f"        덮어쓰면 그 세트로 잰 라운드를 재현할 수 없다.\n"
            f"        · 새 세트를 만들려면: --out <새 디렉토리>\n"
            f"        · 정말 덮어쓰려면:   --force"
        )
    for old in existing:
        old.unlink()

    rows: list[dict] = []
    idx = 0

    # 코어 24: trigger × band
    triggers = ["baseline_aging", "process_shift", "equipment_fault", "escalate"]
    for trigger in triggers:
        for band_name, score in BANDS:
            alert, meta = build_core_alert(trigger, band_name, score, idx)
            AlertModel.model_validate(alert)  # 생성 즉시 계약 검증
            fname = f"{idx + 1:02d}_{trigger}_{band_name}.json"
            (OUT_DIR / fname).write_text(
                json.dumps(alert, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            meta["file"] = fname
            meta["note"] = "-"
            rows.append(meta)
            idx += 1

    # 엣지 7 (파손 1건 포함 — 46번 고정, 그 뒤로 신규 엣지)
    for alert, meta, note in build_edges(idx):
        # trigger 가 'edge' 가 아닌 엣지는 그 verdict 로 파일명을 짓는다 — eval 정답지가
        # 파일명에서 기대 verdict 를 뽑기 때문(eval_supervisor.expected_verdict). 'edge' 는 관찰용.
        fname = f"{idx + 1:02d}_{meta['trigger']}_{meta['band']}.json"
        if meta["valid"]:
            AlertModel.model_validate(alert)  # 유효 엣지는 검증 통과 확인
        else:
            # 파손 엣지는 정말 실패하는지 확인 (실패해야 정상)
            try:
                AlertModel.model_validate(alert)
            except Exception:
                pass
            else:  # 검증을 통과하면 파손 설계 실패
                raise AssertionError("BROKEN 픽스처가 검증을 통과함 — 파손 설계 오류")
        (OUT_DIR / fname).write_text(
            json.dumps(alert, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        meta["file"] = fname
        meta["note"] = note
        rows.append(meta)
        idx += 1

    _write_readme(rows)
    n_valid = sum(1 for r in rows if r["valid"])
    try:                       # 레포 밖 경로로도 낼 수 있어야 한다(대조 검증용 임시 디렉토리 등)
        where = OUT_DIR.relative_to(REPO_ROOT)
    except ValueError:
        where = OUT_DIR
    print(f"[generate_fixtures] {len(rows)}건 생성 (유효 {n_valid} / 파손 {len(rows) - n_valid}) → {where}")


def _write_readme(rows: list[dict]) -> None:
    lines = [
        "# fdc.alert Mock Fixtures (C4-1 Day1)",
        "",
        "> 자동 생성물 — `scripts/generate_fixtures.py` 실행으로 재생성. **직접 편집 금지**(seed 고정 결정적).",
        "> 소스: `config/anchor_constants.json`(앵커) + `config/params.yaml`(control_limit_k_sigma).",
        "> 계약: `docs/API_Contract_명세서.md` §3 fdc.alert / 판정 프레임: `docs/Agent_프롬프트_라이브러리_v1.md` §4.",
        "",
        "## 매트릭스",
        "- **코어 24** = trigger 4종 × context_score 구간 6종",
        "- **엣지 7** = trigger_signal · reference_suspect · multi-violation · crazy_wafer · "
        "regime_signal_only · **BROKEN(의도적 파손 1건)** · temp_drift(온도 가변 축 — 2026-07-26)",
        "- context 게이트(B7): `context_score < 31` = Agent 미가동(기록만). `below_gate(22)`가 이 경로.",
        "- ⚠️ 코어는 **조합형 커버리지**(파서·게이트 분기 검증 우선) — 물리적 대표성보다 필드 커버리지 목적.",
        "",
        "## 검증",
        "`python scripts/validate_fixtures.py` → 전체 중 **BROKEN 1건만 실패**하면 통과.",
        "",
        "## 케이스표",
        "",
        "| # | 파일 | trigger | band | score | gate | chamber | 시그니처 | 기대 verdict | 유효 | 비고 |",
        "|---|------|---------|------|-------|------|---------|----------|--------------|------|------|",
    ]
    for n, r in enumerate(rows, 1):
        valid = "✅" if r["valid"] else "❌(의도)"
        lines.append(
            f"| {n} | `{r['file']}` | {r['trigger']} | {r['band']} | {r['context_score']} | "
            f"{r['gate']} | {r['chamber']} | {r['signature']} | {r['expected_verdict']} | {valid} | {r['note']} |"
        )
    lines.append("")
    (OUT_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
