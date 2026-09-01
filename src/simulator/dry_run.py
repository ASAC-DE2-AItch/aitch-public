# -*- coding: utf-8 -*-
"""P3-1 자체 검증 (dry-run) — Kafka 없이 시뮬레이터 DoD 자동 채점.

검증 항목 (일정 v4 P3-1 DoD: "멀티챔버 fdc.raw 발행 + drift 주입이 raw 데이터에 반영"):
  V1  계약 정합   : 필수 필드 존재, C65 부재(헌법 1-3), C33 원본 유지, chamber_id/wafer_id 규칙
  V2  멀티챔버    : E1 챔버 전부에서 wafer 발행 + 라운드로빈 분배 균형
  V3  챔버 개성   : 주입 전 구간의 챔버별 센서 평균 차이 ≈ 설정된 오프셋 (E3)
  V4  주입 반영   : 시나리오별 — drift/shift: 완료 후 평균 이동 ≈ magnitude_sigma×std,
                    spike: 단일 wafer 피크, oscillation: 부호 교대율. 비대상 챔버는 무영향
  V5  답안지 기록 : sim_events JSONL에 (시나리오×챔버) 이벤트 기록

사용법:
    python -m src.simulator.dry_run                                   # 기본: dryrun_drift_c11 축약 시나리오
    python -m src.simulator.dry_run --scenario src/simulator/scenarios/drift_c11.yaml --limit 3600
    (limit은 전 챔버 합산 wafer 수 — 챔버당 limit/E1 장이 흐른다)

종료 코드: 0 = 전 항목 PASS / 1 = FAIL 존재 (CI 연결 가능).
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from .chamber_replicator import ChamberReplicator
from .message_builder import (ROOT, DEFAULT_CSV_PATH, build_message,
                              filter_post_reset, load_params, validate_contract)
from .scenario_injector import ScenarioInjector, load_scenario


def expected_stacked_delta(scenarios: list[dict], sensor_std: dict,
                           ch: str, s: str, idx: int) -> float:
    """wafer 순번 idx에서 (ch, s)에 걸리는 전 시나리오 스택 기대 주입량 — 검증용 순수 계산."""
    return sum(
        ScenarioInjector.pattern_delta(sc, sensor_std.get(s, 0.0),
                                       idx - sc["start_after_wafers"])
        for sc in scenarios
        if ch in sc["chambers"] and s in sc["sensors"]
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dry-run")

DEFAULT_SCENARIO = ROOT / "src" / "simulator" / "scenarios" / "dryrun_drift_c11.yaml"
DRYRUN_EVENTS_PATH = ROOT / "sim_events_dryrun.jsonl"

# 판정 허용치 (검증 전용 상수 — 파이프라인 설정 아님)
TOL_INJECT_REL = 0.35        # 주입량 상대 오차 허용 (대조군 보정 후)
TOL_OFFSET_REL = 0.5         # 개성 검증: 최대-최소 오프셋 챔버쌍의 관측차 상대 오차
TOL_OFFSET_MIN_SIGMA = 0.15  # 〃 절대 하한 (σ 단위 — 신호가 작을 때 과판정 방지)
TOL_NONTARGET_SIGMA = 0.5    # 비대상 챔버 허용 이동 (σ 단위, 대조군 중앙값 대비)
MIN_OSC_FLIP_RATIO = 0.6     # oscillation 부호 교대율 하한
TAIL_OBSERVE_WAFERS = 30     # drift 램프 완료 후 관측 창


def _fmt(ok: bool) -> str:
    """PASS/FAIL 문자열."""
    return "PASS ✅" if ok else "FAIL ❌"


def run_dry(args) -> int:
    """스트림 생성 → 통계 수집 → V1~V5 판정. FAIL 수를 반환."""
    params = load_params()
    sim_cfg = params["simulator"]
    chamber_count = sim_cfg["chamber_count"]                # E1
    offset_sigma = sim_cfg["chamber_offset_sigma"]          # E3
    noise_sigma = sim_cfg["chamber_noise_sigma"]            # E3-보조

    log.info(f"📂 CSV 로딩: {args.csv}")
    df_full = pd.read_csv(args.csv)
    log.info(f"   → {len(df_full):,}행 / wafer {df_full['C64'].nunique():,}장")

    # 실발행(kafka_producer)과 동일한 seg1 필터 — dry_run 검증이 실제 발행과 일치하도록 (2026-07-28)
    df = (filter_post_reset(df_full)
          if sim_cfg.get("replay_post_reset_only", False) else df_full)
    replicator = ChamberReplicator(df, chamber_count, offset_sigma,
                                   noise_sigma=noise_sigma, stats_df=df_full)

    if DRYRUN_EVENTS_PATH.exists():
        DRYRUN_EVENTS_PATH.unlink()  # 이전 dry-run 답안지 초기화
    scenarios = [load_scenario(Path(p)) for p in args.scenario]
    injector = ScenarioInjector(scenarios, replicator._std, std_lookup=replicator.local_std,
                                events_path=DRYRUN_EVENTS_PATH)
    log.info("💉 시나리오 장전: " + ", ".join(s["scenario_id"] for s in scenarios))

    # 관측할 (챔버, 센서) 집합 = 시나리오 대상 전체 + 비대상 대조군(전 챔버)
    watch_sensors = sorted({s for sc in scenarios for s in sc["sensors"]})

    # ---- 스트림 & 수집 -------------------------------------------------------
    contract_problems: list[str] = []
    wafer_counts: dict[str, int] = defaultdict(int)
    # series[(ch, sensor)] = [(챔버 wafer 순번, wafer 평균값), ...]
    series: dict[tuple, list] = defaultdict(list)

    checked_first = False
    total = 0
    for chamber_id, wafer, ch_idx, pm_count in replicator.stream_wafers(loop=False):
        if total >= args.limit:
            break
        orig_id = str(wafer["C64"].iloc[0])
        wafer_id = f"{orig_id}_{chamber_id.replace('SIM_', '')}"

        sums = defaultdict(float)
        cnts = defaultdict(int)
        step_counter: dict = {}
        for _, row in wafer.iterrows():
            step = int(row["C7"]) if pd.notna(row["C7"]) else -1
            step_counter[step] = step_counter.get(step, 0) + 1
            msg = build_message(chamber_id, row, step_counter[step], pm_count,
                                replicator, injector, ch_idx, wafer_id)
            if not checked_first:
                contract_problems = validate_contract(msg)
                checked_first = True
            for s in watch_sensors:
                v = msg["sensors"].get(s)
                if v is not None:
                    sums[s] += v
                    cnts[s] += 1
        wafer_counts[chamber_id] += 1
        for s in watch_sensors:
            if cnts[s]:
                series[(chamber_id, s)].append((ch_idx, sums[s] / cnts[s]))
        total += 1
        if total % 500 == 0:
            log.info(f"… wafer {total:,}/{args.limit:,} 생성")

    log.info(f"생성 완료 — wafer {total:,}장 (챔버당 ≈{total // chamber_count}장)")

    # ---- 판정 ----------------------------------------------------------------
    results: list[tuple[str, bool, str]] = []

    # V1 계약
    v1_ok = not contract_problems
    results.append(("V1 계약 정합 (필수 필드·C65 부재·C33·ID 규칙)", v1_ok,
                    "; ".join(contract_problems) if contract_problems else "위반 0건"))

    # V2 멀티챔버
    missing_ch = [f"SIM_CH_{i}" for i in range(1, chamber_count + 1)
                  if wafer_counts.get(f"SIM_CH_{i}", 0) == 0]
    balance = (min(wafer_counts.values()) / max(wafer_counts.values())
               if wafer_counts and max(wafer_counts.values()) > 0 else 0)
    v2_ok = not missing_ch and balance > 0.9
    results.append((f"V2 멀티챔버 발행 (E1={chamber_count}, 라운드로빈)", v2_ok,
                    f"분배 {dict(sorted(wafer_counts.items()))} (균형 {balance:.2f})"))

    # V3 챔버 개성 — 최대·최소 오프셋 챔버쌍의 관측 평균차가 설정 오프셋차와 일치하는지
    #   (전 챔버가 같은 원본을 리플레이하므로 자연 변동은 쌍 차이에서 상쇄됨)
    v3_msgs, v3_ok = [], True
    earliest_start = min(sc["start_after_wafers"] for sc in scenarios)
    for s in watch_sensors:
        std = replicator._std.get(s)
        if not std:
            continue
        means = {}
        for ch in wafer_counts:
            pre = [v for i, v in series[(ch, s)] if i < earliest_start]
            if pre:
                means[ch] = sum(pre) / len(pre)
        if len(means) < chamber_count:
            v3_ok = False
            v3_msgs.append(f"{s}: 주입 전 표본 부족")
            continue
        hi = max(means, key=lambda c: replicator.offsets[c][s])
        lo = min(means, key=lambda c: replicator.offsets[c][s])
        expected = replicator.offsets[hi][s] - replicator.offsets[lo][s]
        observed = means[hi] - means[lo]
        tol = max(TOL_OFFSET_REL * abs(expected), TOL_OFFSET_MIN_SIGMA * std)
        ok = abs(observed - expected) <= tol
        v3_ok &= ok
        v3_msgs.append(f"{s}: {hi}−{lo} 관측 {observed:+.2f} vs 설정 {expected:+.2f}")
    results.append(("V3 챔버 개성 (오프셋 재현, E3)", v3_ok, " / ".join(v3_msgs)))

    # V4 주입 반영 — 시나리오별. drift/shift는 대조군(비대상 챔버 중앙값) 보정으로
    #   원본 데이터의 자연 시변(전 챔버 공통)을 제거한 뒤 주입량을 판정한다.
    def _median(xs: list) -> float:
        """중앙값 (표본 없으면 0)."""
        if not xs:
            return 0.0
        ys = sorted(xs)
        n = len(ys)
        return ys[n // 2] if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2

    for sc in scenarios:
        std_map = replicator._std
        for s in sc["sensors"]:
            std = std_map.get(s, 0.0)
            mag = sc["magnitude_sigma"] * std
            start = sc["start_after_wafers"]
            dur = sc.get("duration_wafers", 0) if sc["pattern"] == "drift" else 0

            if sc["pattern"] in ("drift", "shift"):
                # 같은 센서를 건드리는 다른 시나리오의 대상 챔버는 대조군에서 제외 (오염 방지)
                dirty = {ch for other in scenarios if other is not sc
                         and s in other["sensors"] for ch in other["chambers"]}
                # 챔버별 (관측창 평균 − 주입 전 평균)
                d: dict[str, float] = {}
                for ch in wafer_counts:
                    pts = series[(ch, s)]
                    base = [v for i, v in pts if i < start]
                    after = [v for i, v in pts
                             if start + dur <= i < start + dur + TAIL_OBSERVE_WAFERS]
                    if base and after:
                        d[ch] = sum(after) / len(after) - sum(base) / len(base)
                control = _median([d[c] for c in d
                                   if c not in sc["chambers"] and c not in dirty])
                for ch in sc["chambers"]:
                    if ch not in d:
                        results.append((f"V4 주입 반영 {sc['scenario_id']}/{s}@{ch}", False,
                                        "관측 창 표본 없음 — --limit 증가 필요"))
                        continue
                    adj = d[ch] - control  # 대조군 보정 (자연 시변 제거)
                    # 기대값 = 관여하는 전 시나리오의 스택 합 (base·관측창 각각 평균) — 중첩 정밀 반영
                    base_idx = [i for i, _ in series[(ch, s)] if i < start]
                    after_idx = [i for i, _ in series[(ch, s)]
                                 if start + dur <= i < start + dur + TAIL_OBSERVE_WAFERS]
                    exp_base = (sum(expected_stacked_delta(scenarios, std_map, ch, s, i)
                                    for i in base_idx) / len(base_idx))
                    exp_after = (sum(expected_stacked_delta(scenarios, std_map, ch, s, i)
                                     for i in after_idx) / len(after_idx))
                    expected = exp_after - exp_base
                    n_stack = sum(1 for o in scenarios
                                  if ch in o["chambers"] and s in o["sensors"])
                    tol = TOL_INJECT_REL * max(abs(expected), abs(mag))
                    ok = abs(adj - expected) <= tol
                    stack_note = f", 중첩 {n_stack}건 스택" if n_stack > 1 else ""
                    results.append((f"V4 주입 반영 {sc['scenario_id']}/{s}@{ch}", ok,
                                    f"Δ(보정)={adj:+.2f} vs 기대 {expected:+.2f} "
                                    f"({sc['magnitude_sigma']}σ{stack_note}, 대조군 {control:+.2f})"))
                for ch in d:
                    if ch in sc["chambers"] or ch in dirty:
                        continue  # dirty 챔버는 타 시나리오의 정당한 주입 — 오염 판정 제외
                    if abs(d[ch] - control) > TOL_NONTARGET_SIGMA * std:
                        results.append((f"V4 비대상 오염 {sc['scenario_id']}/{s}@{ch}", False,
                                        f"비대상인데 Δ={d[ch] - control:+.2f} (> {TOL_NONTARGET_SIGMA}σ)"))

            elif sc["pattern"] == "spike":
                for ch in sc["chambers"]:
                    pts = series[(ch, s)]
                    base = [v for i, v in pts if i < start]
                    window = [v for i, v in pts if start <= i < start + 3]
                    if not base or not window:
                        results.append((f"V4 스파이크 {sc['scenario_id']}/{s}@{ch}", False,
                                        "표본 없음 — --limit 증가 필요"))
                        continue
                    b = sum(base) / len(base)
                    peak = max(abs(v - b) for v in window)
                    ok = peak >= 0.7 * abs(mag)
                    results.append((f"V4 스파이크 {sc['scenario_id']}/{s}@{ch}", ok,
                                    f"피크 {peak:.2f} vs 목표 {abs(mag):.2f}"))

            elif sc["pattern"] == "oscillation":
                for ch in sc["chambers"]:
                    window = [v for i, v in series[(ch, s)] if i >= start]
                    diffs = [window[i + 1] - window[i] for i in range(len(window) - 1)]
                    flips = sum(1 for i in range(len(diffs) - 1)
                                if diffs[i] * diffs[i + 1] < 0)
                    ratio = flips / max(1, len(diffs) - 1)
                    ok = ratio >= MIN_OSC_FLIP_RATIO
                    results.append((f"V4 교대 {sc['scenario_id']}/{s}@{ch}", ok,
                                    f"부호 교대율 {ratio:.2f} (하한 {MIN_OSC_FLIP_RATIO})"))

    # V5 답안지
    n_events = (sum(1 for _ in open(DRYRUN_EVENTS_PATH, encoding="utf-8"))
                if DRYRUN_EVENTS_PATH.exists() else 0)
    expected = sum(len(sc["chambers"]) for sc in scenarios)
    v5_ok = n_events >= expected
    results.append(("V5 답안지 기록 (sim_events)", v5_ok,
                    f"{n_events}건 기록 (기대 {expected}건) — {DRYRUN_EVENTS_PATH.name}"))

    # ---- 리포트 ---------------------------------------------------------------
    fails = sum(1 for _, ok, _ in results if not ok)
    print("\n" + "=" * 78)
    print("P3-1 DRY-RUN 검증 리포트 (DoD: 멀티챔버 발행 + 주입 반영 — 시뮬 자체 검증)")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"  {_fmt(ok):8} │ {name}\n           │   {detail}")
    print("-" * 78)
    print(f"  결과: {len(results) - fails}/{len(results)} PASS"
          + ("  →  P3-1 DoD 충족 🎉" if fails == 0 else f"  →  FAIL {fails}건 — 로그 확인"))
    print("=" * 78)
    return fails


def main():
    """CLI 진입점."""
    p = argparse.ArgumentParser(description="P3-1 dry-run 검증 (Kafka 불요)")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    p.add_argument("--scenario", action="append", default=None,
                   help="시나리오 YAML (반복 지정 가능, 기본: dryrun_drift_c11)")
    p.add_argument("--limit", type=int, default=1200,
                   help="생성할 전체 wafer 수 (챔버당 limit/E1)")
    args = p.parse_args()
    if args.scenario is None:
        args.scenario = [str(DEFAULT_SCENARIO)]
    if not args.csv.exists():
        log.error(f"❌ CSV 없음: {args.csv}")
        sys.exit(2)
    sys.exit(1 if run_dry(args) else 0)


if __name__ == "__main__":
    main()
