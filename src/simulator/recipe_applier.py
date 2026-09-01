# -*- coding: utf-8 -*-
"""fdc.correction(recipe) 구독→setpoint 반영 — P5-3 선행분 (2026-07-10 주말 선행).

Recipe R2R closed loop의 "적용" 단계 (시뮬레이터 스펙 v1.1 추가절, 계약 §6):
  승인 워크플로가 발행한 CorrectionApplied(correction_type='recipe')를 수신하면
  (chamber × recipe × step × parameter) 스코프로 setpoint를 delta만큼 이동 —
  이후 발행되는 fdc.raw에 반영된다. 시연 시나리오 3의 기반.

적용 규칙:
  - delta: value_current/value_proposed 있으면 절대 이동, 없으면 delta_pct 배율
  - |delta_pct| > D6(recipe_delta_max_pct, ±3%)는 수신 측에서도 거부 —
    무승인 초과 적용 방어선 이중화 (1차 방어는 승인 게이트·B5-4 상한 체크)
  - recipe_id·step 미지정(null)은 와일드카드 (해당 챔버·파라미터 전체)
  - 적용 이력은 sim_events(JSONL) 기록 → Scorecard "튜닝 후 분포 정상화" 채점 재료
  - limit형은 무시 (B 소관 — consumer-group-simulator는 recipe만 처리, 계약 Consumer Group 규칙)

dry-run:
    python -m src.simulator.recipe_applier      # Kafka 불요 — 적용·스코프·거부 검증
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .chamber_replicator import ChamberReplicator
from .message_builder import (DEFAULT_CSV_PATH, ROOT, build_message,
                              load_params)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("recipe-applier")

TOPIC_CORRECTION = "fdc.correction"
CONSUMER_GROUP = "consumer-group-simulator"  # 계약 Consumer Group 규칙
REQUIRED_FIELDS = {"incident_id", "correction_type", "chamber_id", "parameter_id",
                   "effective_from"}

# ---- BL8: temp 목표 지정형 (2026-07-11 채택, P5-3 본대 구현 2026-07-20) ----------
# temp 축은 setpoint 컬럼이 수집 채널 밖(데이터 사전 🎛맵 — 미대응 2축)이라 delta 기준점이 없음.
# → 튜닝안이 목표 운전점(value_proposed)을 직접 지정하면 시뮬이 그 레벨로 "수렴"시킨다 (칠러 서보 근사).
# 손잡이명 = temp_target (KB 카드 101). C17 손잡이 매핑 금지 원칙과 비충돌 —
# C17은 결과가 찍히는 모니터 채널이고, 조작 대상은 temp_target이라는 별도 물리 목표다.
TEMP_TARGET_PARAM = "temp_target"
TEMP_MONITOR_CHANNEL = "C17"      # 수렴이 반영되는 모니터 채널 (프로파일 보존 평행이동)
TEMP_SERVO_TAU_WAFERS = 30        # 서보 수렴 길이 (챔버 wafer) — 즉시 계단이 아니라 선형 접근


class RecipeApplier:
    """수신한 recipe 보정을 setpoint 이동으로 유지·적용하는 상태 머신."""

    def __init__(self, params: dict, events_path: Path | None = None):
        self.delta_max_pct = params["agent"]["recipe_delta_max_pct"]  # D6
        self.events_path = events_path
        # key = (chamber_id, recipe_id|None, step|None, parameter_id)
        self._active: dict[tuple, dict] = {}

    # ---- 수신 -----------------------------------------------------------------
    def handle_message(self, msg: dict) -> bool:
        """CorrectionApplied 1건 처리. 적용했으면 True (무시·거부는 False)."""
        if not isinstance(msg, dict):
            log.warning("dict가 아닌 correction 수신 — 스킵")
            return False
        if msg.get("correction_type") != "recipe":
            log.debug(f"limit형 correction 무시 (B 소관): {msg.get('incident_id')}")
            return False
        missing = REQUIRED_FIELDS - set(msg)
        if missing:
            log.error(f"correction 필수 필드 누락 {sorted(missing)} — 스킵 (헌법 6-2)")
            return False

        cur, prop = msg.get("value_current"), msg.get("value_proposed")
        delta_pct = msg.get("delta_pct")
        if cur is not None and prop is not None and cur != 0:
            absolute = float(prop) - float(cur)
            pct = absolute / abs(float(cur)) * 100.0
        elif delta_pct is not None:
            absolute = None
            pct = float(delta_pct)
        else:
            log.error(f"delta 지정 없음 (value쌍 또는 delta_pct) — 스킵: {msg.get('incident_id')}")
            return False

        if abs(pct) > self.delta_max_pct:  # D6 이중 방어 (temp 목표 지정형도 준용)
            log.error(f"⛔ D6 상한 초과 거부: |{pct:.2f}%| > ±{self.delta_max_pct}% "
                      f"({msg.get('incident_id')}) — 승인 게이트 우회 의심")
            return False

        # BL8 — 목표 지정형: parameter_id=temp_target (또는 adjust_mode=target)
        mode = ("target" if (msg.get("adjust_mode") == "target"
                             or msg["parameter_id"] == TEMP_TARGET_PARAM) else "delta")
        if mode == "target":
            if absolute is None:
                log.error("target 모드는 value_current(현 정착 레벨)·value_proposed(목표) 필수 — 스킵")
                return False
            param = TEMP_MONITOR_CHANNEL   # 목표 → 모니터 채널 평행이동으로 반영
        else:
            param = msg["parameter_id"]

        key = (msg["chamber_id"], msg.get("recipe_id"), msg.get("step"), param)
        self._active[key] = {"absolute": absolute, "pct": pct,
                             "incident_id": msg["incident_id"],
                             "mode": mode, "ramp_n": 0}
        self._record_event(msg, pct, mode)
        log.info(f"🔧 recipe 적용: {msg['parameter_id']}→{param} {pct:+.2f}% [{mode}] "
                 f"@{msg['chamber_id']}/{msg.get('recipe_id') or '*'}/step "
                 f"{msg.get('step') or '*'} (incident {msg['incident_id']})")
        return True

    def _record_event(self, msg: dict, pct: float, mode: str = "delta"):
        """sim_events(JSONL) 적용 이력 기록 — Scorecard 채점 재료."""
        if self.events_path is None:
            return
        event = {
            "event_id": f"SIMEV-RECIPE-{msg['incident_id']}",
            "type": "recipe_applied",
            "adjust_mode": mode,                       # BL8: delta | target
            "chamber_id": msg["chamber_id"],
            "recipe_id": msg.get("recipe_id"),
            "step": msg.get("step"),
            "parameter_id": msg["parameter_id"],
            "delta_pct": round(pct, 3),
            "incident_id": msg["incident_id"],
            "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ---- 적용 -----------------------------------------------------------------
    def on_wafer(self, chamber_id: str) -> None:
        """챔버 wafer 1장 경과 훅 — target 모드 서보 램프 전진 (producer 루프에서 호출, BL8)."""
        for (ch, _rid, _st, _param), d in self._active.items():
            if ch == chamber_id and d.get("mode") == "target":
                d["ramp_n"] = d.get("ramp_n", 0) + 1

    def apply(self, chamber_id: str, recipe_id, step, sensor: str, value: float) -> float:
        """발행 직전 센서 값에 활성 보정을 적용 (스코프: 챔버×레시피×스텝×파라미터).

        target 모드(BL8)는 즉시 계단이 아니라 TAU에 걸친 선형 수렴 — 칠러 서보 근사.
        """
        for (ch, rid, st, param), d in self._active.items():
            if ch != chamber_id or param != sensor:
                continue
            if rid is not None and rid != recipe_id:
                continue
            if st is not None and st != step:
                continue
            ramp = (min(1.0, d.get("ramp_n", 0) / TEMP_SERVO_TAU_WAFERS)
                    if d.get("mode") == "target" else 1.0)
            value = value + d["absolute"] * ramp if d["absolute"] is not None \
                else value * (1.0 + d["pct"] * ramp / 100.0)
        return value


def poll_corrections(consumer, applier: RecipeApplier) -> None:
    """비차단 poll 1회 — 역직렬화 실패는 스킵+로그 (헌법 6-2, producer 루프에서 호출)."""
    m = consumer.poll(0)
    if m is None:
        return
    if m.error():
        log.warning(f"correction 수신 오류: {m.error()}")
        return
    try:
        payload = json.loads(m.value().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.error(f"correction 역직렬화 실패 — 스킵: {e}")
        return
    applier.handle_message(payload)


# ---------------------------------------------------------------------------
# dry-run — 구독부 자체 검증 (Kafka 불요)
# ---------------------------------------------------------------------------

def _sample_correction(**over) -> dict:
    """검증용 CorrectionApplied 기본형 (계약 §6 예시와 동일 골격)."""
    base = {
        "incident_id": "INC-20260710-SIMCH3-001",
        "correction_type": "recipe",
        "chamber_id": "SIM_CH_3",
        "recipe_id": "C6_0",
        "step": 4,
        "parameter_id": "C1",
        "value_current": 118.0,
        "value_proposed": 116.5,
        "delta_pct": -1.3,
        "sensor": None,
        "limit_version": None,
        "effective_from": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approved_by": "engineer_01",
    }
    base.update(over)
    return base


def run_dry(args) -> int:
    """R1~R6 검증. FAIL 수 반환. (노이즈 0으로 결정론 비교)"""
    params = load_params()
    sim_cfg = params["simulator"]

    log.info(f"📂 CSV 로딩: {args.csv}")
    df = pd.read_csv(args.csv)
    # 결정론 비교를 위해 노이즈 0 — 오프셋·주입 경로는 동일
    replicator = ChamberReplicator(df, sim_cfg["chamber_count"],
                                   sim_cfg["chamber_offset_sigma"], noise_sigma=0.0)
    events_path = ROOT / "sim_events_dryrun.jsonl"
    applier = RecipeApplier(params, events_path=events_path)
    results = []

    # 대상 row: 최빈 레시피·step 4 인 row 1건 (레시피 값 표기가 달라도 동작)
    std_recipe = str(df["C6"].mode()[0])
    tgt = df[(df["C6"].astype(str) == std_recipe) & (df["C7"] == 4)].iloc[0]
    other_step = df[(df["C6"].astype(str) == std_recipe) & (df["C7"] != 4)].iloc[0]

    def emit(row, chamber, ap):
        """단일 row 발행 결과의 C1 값."""
        m = build_message(chamber, row, 1, 0, replicator, None, 1,
                          f"C64_X_{chamber.replace('SIM_', '')}", recipe_applier=ap)
        return m["sensors"]["C1"]

    # R1: limit형 무시
    ok = not applier.handle_message(_sample_correction(correction_type="limit",
                                                       sensor="C11"))
    results.append(("R1 limit형 무시 (B 소관 — recipe만 처리)", ok and not applier._active,
                    f"적용 여부={not ok} / 활성 보정 {len(applier._active)}건"))

    # R2: D6 상한 초과 거부
    ok = not applier.handle_message(_sample_correction(value_current=100.0,
                                                       value_proposed=95.0,
                                                       delta_pct=-5.0))
    results.append((f"R2 D6(±{applier.delta_max_pct}%) 초과 거부 (이중 방어)",
                    ok and not applier._active, "5% 요청 → 거부"))

    # R3: 필수 필드 누락·비정상 페이로드 스킵 (죽지 않음)
    ok1 = not applier.handle_message({"correction_type": "recipe"})
    ok2 = not applier.handle_message("garbage")  # type: ignore[arg-type]
    results.append(("R3 비정상 페이로드 스킵 (헌법 6-2)", ok1 and ok2, "누락·타입 오류 모두 스킵"))

    # R4: 정상 적용 — C1이 절대 delta(−1.5)만큼 이동
    before = emit(tgt, "SIM_CH_3", None)
    applied = applier.handle_message(_sample_correction(recipe_id=std_recipe))
    after = emit(tgt, "SIM_CH_3", applier)
    shift = after - before
    ok = applied and abs(shift - (-1.5)) < 1e-6
    results.append(("R4 setpoint 반영 (C1, 절대 delta −1.5)", ok,
                    f"이동 {shift:+.4f} (기대 −1.5000)"))

    # R5: 스코프 격리 — 타 챔버·타 step 무영향 (SIM_CH_5→SIM_CH_1: 4챔버 전환, 시뮬 스펙 v1.3)
    other_ch = emit(tgt, "SIM_CH_1", applier) - emit(tgt, "SIM_CH_1", None)
    other_st = emit(other_step, "SIM_CH_3", applier) - emit(other_step, "SIM_CH_3", None)
    ok = abs(other_ch) < 1e-9 and abs(other_st) < 1e-9
    results.append(("R5 스코프 격리 (챔버×레시피×스텝×파라미터)", ok,
                    f"타 챔버 Δ={other_ch:+.1e} / 타 step Δ={other_st:+.1e}"))

    # R6: sim_events 기록
    n = 0
    if events_path.exists():
        with open(events_path, encoding="utf-8") as f:
            n = sum(1 for line in f if '"recipe_applied"' in line)
    results.append(("R6 적용 이력 sim_events 기록 (Scorecard 재료)", n >= 1,
                    f"recipe_applied {n}건"))

    # ---- BL8: temp 목표 지정형 (T1~T3) ----------------------------------------
    def emit17(row, chamber, ap):
        m = build_message(chamber, row, 1, 0, replicator, None, 1,
                          f"C64_X_{chamber.replace('SIM_', '')}", recipe_applier=ap)
        return m["sensors"]["C17"]

    ap_t = RecipeApplier(params, events_path=events_path)
    base17 = emit17(tgt, "SIM_CH_2", None)
    applied = ap_t.handle_message(_sample_correction(
        chamber_id="SIM_CH_2", parameter_id="temp_target", adjust_mode="target",
        recipe_id=None, step=None, value_current=250.0, value_proposed=252.0,
        delta_pct=None))
    # T1: 램프 0 — 수신 직후엔 이동 없음 (즉시 계단 아님)
    ok = applied and abs(emit17(tgt, "SIM_CH_2", ap_t) - base17) < 1e-9
    results.append(("T1 [BL8] target 수신 직후 이동 0 (서보 — 즉시 계단 금지)", ok,
                    f"Δ={emit17(tgt, 'SIM_CH_2', ap_t) - base17:+.4f} (기대 0)"))
    # T2: 램프 절반 → +1.0, 완주 → +2.0 (선형 수렴)
    for _ in range(TEMP_SERVO_TAU_WAFERS // 2):
        ap_t.on_wafer("SIM_CH_2")
    half = emit17(tgt, "SIM_CH_2", ap_t) - base17
    for _ in range(TEMP_SERVO_TAU_WAFERS):
        ap_t.on_wafer("SIM_CH_2")
    full = emit17(tgt, "SIM_CH_2", ap_t) - base17
    ok = abs(half - 1.0) < 1e-6 and abs(full - 2.0) < 1e-6
    results.append(("T2 [BL8] 서보 수렴 (절반 +1.0 → 완주 +2.0)", ok,
                    f"절반 {half:+.4f} / 완주 {full:+.4f}"))
    # T3: target도 D6 준용 + value쌍 누락 스킵
    ok1 = not ap_t.handle_message(_sample_correction(
        parameter_id="temp_target", adjust_mode="target",
        value_current=250.0, value_proposed=280.0, delta_pct=None))   # +12% > D6
    ok2 = not ap_t.handle_message(_sample_correction(
        parameter_id="temp_target", adjust_mode="target",
        value_current=None, value_proposed=None, delta_pct=-1.0))     # 목표 미지정
    results.append(("T3 [BL8] D6 준용 + value쌍 필수", ok1 and ok2,
                    "+12% 거부 / 목표 미지정 스킵"))

    fails = sum(1 for _, ok, _ in results if not ok)
    print("\n" + "=" * 78)
    print("RECIPE 구독부 DRY-RUN 리포트 (P5-3 선행분 — 시나리오 3 '적용' 단계)")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"  {'PASS ✅' if ok else 'FAIL ❌':8} │ {name}\n           │   {detail}")
    print("-" * 78)
    print(f"  결과: {len(results) - fails}/{len(results)} PASS"
          + ("  →  recipe 구독부 검증 통과 🎉" if fails == 0 else "  →  FAIL — 로그 확인"))
    print("=" * 78)
    return fails


def main():
    """CLI 진입점 (dry-run 전용 — 실구독은 kafka_producer에 통합)."""
    p = argparse.ArgumentParser(description="recipe 구독부 dry-run (P5-3 선행)")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    args = p.parse_args()
    if not args.csv.exists():
        log.error(f"❌ CSV 없음: {args.csv}")
        sys.exit(2)
    sys.exit(1 if run_dry(args) else 0)


if __name__ == "__main__":
    main()
