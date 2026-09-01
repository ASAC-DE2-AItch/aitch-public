# -*- coding: utf-8 -*-
"""합성 Historical Case 생성기 — C3-2 시딩 입력물 (사례 생성은 PM 제공, 일정표 명시).

시나리오 정의 공간(센서 × 패턴 × 크기 × 사이클 위상 × 조치 × 결과)을 샘플링하여
historical_cases 스키마에 맞는 판례 레코드 + RAG용 서술문을 JSONL로 생성한다.

품질 규칙 (config 합의안 D8):
  - near-duplicate 대량 생성 금지 → (센서·룰·패턴·조치·크기 구간) 조합 중복 제한
  - provenance 라벨 유지 → "synthetic_param" (실행 기반 사례는 추후 "simulator_run")
  - 결과 분포 현실화 → 성공/실패 혼합 (정답 조치≈높은 성공률, 오답≈낮음 — 현업 정확도 ~50% 참고)
  - 센서는 C코드만 사용 (헌법 6-4 — 발표명 하드코딩 금지)

v2 (2026-07-11, P4-5 — KB 검수 반영):
  - 손잡이 어휘 단일 소스화: recipe_tuning 사례에 knob_param(실존 setpoint C코드만)을
    생성기가 직접 발급 — C 파생 매핑 제거. C12·C17은 레짐 신호라 손잡이 금지 (7/10 확정).
  - delta 상한 분리: recipe = D6(±3%) 이내 signed / limit = A2(±5%) 이내 signed (음수 포함).
  - temp·pressure 축은 setpoint 컬럼이 데이터 수집 밖(2026-07-11 전수 탐사 확정, 레시피 정의서
    입수 불가) → recipe 사례에서 제외하고 escalation 라우팅. temp는 W5부터 목표 지정형(BL8) 부활.

사용법:
    python scripts/generate_synthetic_cases.py --n 10000 --out cases_synthetic_v2.jsonl
"""

import argparse
import json
import random
from datetime import datetime, timezone

# ---- 시나리오 정의 공간 (API Contract·기획서 4-5 라우팅 매트릭스 기반) -------
SENSORS = ["C11", "C31", "C32", "C61", "C62", "C17", "C9", "C15", "C16",
           "C57", "C58", "C63"]  # SPC 대상 센서 (데이터 사전 v3)

# 패턴 → 걸리는 Nelson Rule 후보 (기획서 II-3 Nelson 표)
PATTERN_RULES = {
    "spike":        ["N1"],
    "drift_slow":   ["N2", "N3", "N6"],
    "drift_fast":   ["N2", "N3"],
    "oscillation":  ["N4"],
    "post_pm_gap":  ["N5"],
    "level_shift":  ["N1", "N5", "N8"],
}

# 상황 → 정답 조치 매핑 (라우팅 매트릭스 4-5 근사)
#   (pattern, phase) 조합의 "정답" action. 오답 사례도 일부 생성해 실패 판례를 만든다.
CORRECT_ACTION = {
    ("spike", "any"):            "wafer_scrap",       # crazy wafer 1장 → HOLD→스크랩
    ("post_pm_gap", "early"):    "limit_reset",       # PM 직후 정상 이동 → 실력치 재설정
    ("drift_slow", "late"):      "limit_reset",       # 완만 드리프트 + 사이클 후반 = 기준선 노후
    ("drift_slow", "early"):     "maintenance",       # PM 직후부터 드리프트 = 이상 의심
    ("drift_fast", "any"):       "maintenance",       # 급격 드리프트 = 진성 열화
    ("oscillation", "any"):      "maintenance",       # Matching 불안정 = 정비
    ("level_shift", "mid"):      "recipe_tuning",     # 공정 조건 이탈 = Recipe R2R (v4.5)
    ("level_shift", "early"):    "limit_reset",
}
ACTIONS = ["limit_reset", "recipe_tuning", "maintenance", "wafer_scrap", "escalation"]
PHASES = ["early", "mid", "late"]  # 사이클 위상 (PM 직후 / 중반 / 후반)

# ---- 손잡이 어휘 (v2, 2026-07-11 — C6-3 매핑과 단일 소스) ---------------------
#   원칙: 데이터에 실존하는 setpoint 컬럼만 (데이터 사전 확정 — 계약 §5 예시 정합).
#   C12(Vdc 기준값 도장)·C17(온도 실측)은 레짐 신호 — 손잡이 금지 (F-T15 실측, 7/10 확정).
#   파워 손잡이 = C1(RF_Power_Set 설정값). C31은 실측(반응)이라 손잡이 아님.
#   temp·pressure 축은 setpoint 컬럼 수집 밖(7/11 확정) → recipe 정답에서 제외(escalation 라우팅).
KNOB_BY_SENSOR = {   # 알람 센서(관측) → (조정 손잡이 setpoint, 전형 value_before)
    "C11": ("C1", 118.0),   # 플라즈마/이온에너지 축 → RF Power Set (step4 전형 118)
    "C31": ("C1", 118.0),
    "C32": ("C1", 118.0),
    "C61": ("C1", 118.0),
    "C62": ("C1", 118.0),
    "C15": ("C4", 40.0),    # 가스 A 실측 → 가스 Setpoint A (C6_0 step4 = 40)
    "C16": ("C5", 71.0),    # 가스 B 변동 → 가스 Setpoint B (C6_0 step4 = 71)
}
RECIPE_DELTA_MAX_PCT = 3.0   # D6 (config agent.recipe_delta_max_pct 정합)
LIMIT_DELTA_MAX_PCT = 5.0    # A2 (config limit_engine.reset_delta_max_pct 정합)

# ---- 성공률 파라미터 (멘토 확인 Q6 반영 2026-07-09) --------------------------
# is_success 의미 재정의: "문제 해결(1회 종결)"이 아니라 "불량률 개선"이다.
#   멘토(2026-07-09): "조치 한 번에 문제를 해결하는 건 불가능, 구조불량은 항상
#   존재하고 최대한 불량률을 줄이는 방향으로 튜닝한다." → is_success = 조치가
#   불량률을 유의하게 낮췄는가(개선). 상수는 문헌 근거로 유지, 라벨 의미만 조정.
# 근거 (2026-07 서치):
#   · FTFR(1회 조치 성공) 산업 평균 75~80%, 최상위 89~98% (IBM·Limble·Comparesoft)
#   · CAPA 재발률 양호 기준 10~15% 이하 = 조치 성공 85~90% (Atlas Compliance)
#   · FPRR(30일 내 무재발 수리 비율) (Oxmaint KPI)
#   → 정답 조치 개선율 = 0.80 (FTFR 평균~CAPA 기준 하단), 오답 = 0.20 (가정).
#     전체 개선율 ≈ 0.7×0.80 + 0.3×0.20 = 62%.
CORRECT_ACTION_RATIO = 0.7    # 정답 조치가 기록된 사례 비율
SUCCESS_IF_CORRECT = 0.80     # 정답 조치의 개선 성공률 (FTFR 평균대)
SUCCESS_IF_WRONG = 0.20       # 오답 조치의 개선 성공률

# 서술 템플릿 (문장 구조 다양화 — near-duplicate 방지 보조)
NARRATIVE_TEMPLATES = [
    "{chamber}에서 {sensor} 센서가 {pattern_ko} 패턴을 보이며 {rule} 위반 발생 ({phase_ko}). "
    "{action_ko}(을)를 적용한 결과, {outcome_ko}",
    "{phase_ko} {chamber}의 {sensor}에 {pattern_ko}이 관측되어 {rule}이 발동됨. "
    "조치로 {action_ko} 수행 — {outcome_ko}",
    "{rule} 위반 알람 ({sensor}, {chamber}): 원인은 {pattern_ko}로 분석됨 ({phase_ko}). "
    "{action_ko} 이후 {outcome_ko}",
    "{sensor} {pattern_ko} 감지 → Incident 개설 ({chamber}, {phase_ko}, {rule}). "
    "엔지니어 승인으로 {action_ko} 실행, {outcome_ko}",
]
PATTERN_KO = {
    "spike": "단발 급등(spot)", "drift_slow": "완만한 점진 드리프트",
    "drift_fast": "급격한 드리프트", "oscillation": "주기적 진동",
    "post_pm_gap": "PM 직후 수준 이동", "level_shift": "계단형 수준 이동",
}
PHASE_KO = {"early": "PM 직후 구간", "mid": "사이클 중반", "late": "사이클 후반"}
ACTION_KO = {
    "limit_reset": "실력치 재설정", "recipe_tuning": "Recipe 파라미터 튜닝",
    "maintenance": "설비 정비(부품 점검)", "wafer_scrap": "wafer HOLD 및 스크랩",
    "escalation": "공정 검토 에스컬레이션",
}


def sample_case(rng: random.Random, idx: int) -> dict:
    """사례 1건 샘플링 — 상황 → (정답/오답) 조치 → 결과."""
    pattern = rng.choice(list(PATTERN_RULES))
    sensor = rng.choice(SENSORS)
    rule = rng.choice(PATTERN_RULES[pattern])
    phase = rng.choice(PHASES)
    chamber = f"SIM_CH_{rng.randint(1, 6)}"
    magnitude = round(rng.uniform(1.0, 4.0), 1)  # σ 단위

    correct = (CORRECT_ACTION.get((pattern, phase))
               or CORRECT_ACTION.get((pattern, "any"))
               or "escalation")
    # 손잡이 미대응 센서(온도·He/압력·기타 축)의 recipe 정답은 escalation으로 라우팅 (v2)
    if correct == "recipe_tuning" and sensor not in KNOB_BY_SENSOR:
        correct = "escalation"
    # 정답/오답 조치 혼합 → 실패 판례 확보 (상단 상수 참조)
    if rng.random() < CORRECT_ACTION_RATIO:
        action = correct
    else:
        action = rng.choice([a for a in ACTIONS if a != correct])
    if action == "recipe_tuning" and sensor not in KNOB_BY_SENSOR:
        action = "escalation"  # 오답 추첨에서도 미대응 축의 recipe는 금지 (v2)

    # 조치 폭 (v2): recipe = D6 ±3% / limit = A2 ±5%, 둘 다 signed·최소 0.5%
    knob_param = knob_before = knob_after = None
    if action == "recipe_tuning":
        knob_param, base = KNOB_BY_SENSOR[sensor]
        delta_pct = round(rng.uniform(0.5, RECIPE_DELTA_MAX_PCT), 1) * rng.choice([1, -1])
        knob_before = base
        knob_after = round(base * (1 + delta_pct / 100.0), 3)
    elif action == "limit_reset":
        delta_pct = round(rng.uniform(0.5, LIMIT_DELTA_MAX_PCT), 1) * rng.choice([1, -1])
    else:
        delta_pct = None

    is_success = rng.random() < (SUCCESS_IF_CORRECT if action == correct
                                 else SUCCESS_IF_WRONG)
    if action == "limit_reset" and is_success:
        fa_reduction = round(rng.gauss(55, 12), 1)
        missed = False
    elif action == "limit_reset":
        fa_reduction = round(max(0.0, rng.gauss(10, 8)), 1)
        missed = rng.random() < 0.3   # 잘못된 재설정 → 미탐 위험
    else:
        fa_reduction = None
        missed = (not is_success) and rng.random() < 0.2

    outcome_ko = (
        f"가성알람 {fa_reduction}% 감소, 미탐 0건 (불량률 개선)" if action == "limit_reset" and is_success
        else "불량률 유의 감소·이상 재발 없음 (개선)" if is_success
        else "불량률 개선폭 미미·동일 패턴 재발로 Incident reopen (미개선)" if not missed
        else "재설정 이후 진성 이상 미탐 발생 (미개선)"
    )
    action_ko = ACTION_KO[action]
    if action == "recipe_tuning":
        action_ko = f"Recipe 파라미터 튜닝 ({knob_param} {delta_pct:+.1f}%)"
    elif action == "limit_reset":
        action_ko = f"실력치 재설정 (delta {delta_pct:+.1f}%)"
    narrative = rng.choice(NARRATIVE_TEMPLATES).format(
        chamber=chamber, sensor=sensor, rule=rule,
        pattern_ko=PATTERN_KO[pattern], phase_ko=PHASE_KO[phase],
        action_ko=action_ko, outcome_ko=outcome_ko,
    )

    return {
        # --- historical_cases 컬럼 대응 ---
        "case_id": f"CASE-SYN-{idx:05d}",
        "chamber_id": chamber,
        "nelson_rule": rule,
        "sensor_id": sensor,
        "action_type": action,
        "action_taken": action_ko,
        "delta_pct": delta_pct,
        # --- 손잡이 단일 소스 (v2 — recipe_r2r_log 파생용, C 매핑 불요) ---
        "knob_param": knob_param,
        "knob_value_before": knob_before,
        "knob_value_after": knob_after,
        "outcome_false_alarm_reduction_pct": fa_reduction,
        "outcome_missed_detection": missed,
        "is_success": is_success,
        "is_seed": True,
        # --- Qdrant payload·임베딩용 부가 필드 ---
        "pattern": pattern,
        "cycle_phase": phase,
        "magnitude_sigma": magnitude,
        "narrative": narrative,
        "provenance": "synthetic_param",   # 실행 기반 사례는 추후 "simulator_run"
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main():
    """생성 실행 — 중복 제한(near-duplicate 방지) 포함."""
    p = argparse.ArgumentParser(description="합성 Historical Case 생성기 (C3-2 시딩 입력)")
    p.add_argument("--n", type=int, default=10000)
    p.add_argument("--out", default="cases_synthetic_v2.jsonl")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-dup-per-key", type=int, default=40,
                   help="(센서·룰·패턴·조치·위상) 동일 조합 최대 허용 수")
    args = p.parse_args()

    rng = random.Random(args.seed)
    key_count: dict = {}
    written = 0
    attempts = 0
    with open(args.out, "w", encoding="utf-8") as f:
        while written < args.n and attempts < args.n * 20:
            attempts += 1
            case = sample_case(rng, written + 1)
            key = (case["sensor_id"], case["nelson_rule"], case["pattern"],
                   case["action_type"], case["cycle_phase"])
            if key_count.get(key, 0) >= args.max_dup_per_key:
                continue
            key_count[key] = key_count.get(key, 0) + 1
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
            written += 1

    with open(args.out, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    succ = sum(r["is_success"] for r in rows)
    print(f"✅ {written:,}건 생성 → {args.out}")
    print(f"   성공(개선) {succ:,} / 미개선 {written - succ:,} "
          f"({succ / written * 100:.0f}%) │ 고유 조합 {len(key_count):,}개")
    print(f"   조치 분포: " + ", ".join(
        f"{a}={sum(1 for r in rows if r['action_type'] == a)}" for a in ACTIONS))
    # v2 가드 검증 — 어휘·상한 (P4-5 DoD)
    rec = [r for r in rows if r["action_type"] == "recipe_tuning"]
    lim = [r for r in rows if r["action_type"] == "limit_reset"]
    knobs = sorted({r["knob_param"] for r in rec})
    bad_knob = [k for k in knobs if k in ("C12", "C17")]
    over_d6 = sum(1 for r in rec if abs(r["delta_pct"]) > RECIPE_DELTA_MAX_PCT)
    over_a2 = sum(1 for r in lim if abs(r["delta_pct"]) > LIMIT_DELTA_MAX_PCT)
    neg = sum(1 for r in rec if r["delta_pct"] < 0)
    print(f"   [가드] recipe knob={knobs} │ 금지 어휘(C12/C17)={bad_knob or '없음'} │ "
          f"D6 초과={over_d6} │ A2 초과={over_a2} │ 음수 delta={neg}/{len(rec)}")
    assert not bad_knob and over_d6 == 0 and over_a2 == 0, "v2 가드 위반 — 배포 금지"


if __name__ == "__main__":
    main()
