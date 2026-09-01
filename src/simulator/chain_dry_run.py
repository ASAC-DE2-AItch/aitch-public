# -*- coding: utf-8 -*-
"""M3 ② 월드스테이트 개조 — 체인 dry-run (DoD 자동 채점, Kafka 불요).

목적: "상시프로세스 + 실행 중 주입 + 컷 간 잔류 승계"를 하나의 지속 스트림에서 증명한다.
단일 ScenarioInjector에 4컷을 **서로 다른 순번에 런타임 주입**(inject_now)하고, 마지막
컷 이후에도 앞 컷의 효과가 **원복되지 않는지**(§0-2 잔류 승계 맵)를 검증한다.

배역제 (4챔버 — 한 컷의 주입이 다른 컷 TTTM을 오염시키지 않도록 주역 챔버 분리):
  프롤로그 알람폭풍 → CH1 (oscillation, 과도 — 잔류 대상 아님)
  1막 드리프트      → CH3 (drift, 램프 후 유지 — 잔류)
  2막 요란 PM        → CH2 (pm_reset level_shift — 잔류)
  3막 레시피 R2R    → CH4 (shift 대리 — setpoint 보정 유지 — 잔류)

판정:
  C1 잔류 승계(원복 금지): 잔류 컷의 대상 센서가 마지막 관측창에서 여전히 이동 유지
                          (대조군 보정 후 기대의 65% 이상 — 0으로 원복되지 않음)
  C2 컷 격리            : 각 컷의 비대상 챔버는 마지막 창에서 무영향 (TTTM 비오염)
  C3 월드스테이트 단조   : active_summary 스냅샷의 활성 집합이 컷 진행 중 줄지 않음(원복 금지)
  C4 답안지             : 주입한 4컷 전부 sim_events(JSONL)에 기록

사용법:
    python -m src.simulator.chain_dry_run                 # 기본 CSV·limit 1200
종료 코드: 0 = 전 항목 PASS / 1 = FAIL 존재 (CI 연결 가능).
"""

import argparse
import logging
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pandas as pd
import yaml

from .chamber_replicator import ChamberReplicator
from .message_builder import ROOT, DEFAULT_CSV_PATH, build_message, load_params
try:
    from .message_builder import filter_post_reset          # C17 병합 후 존재 (seg1 replay 필터)
except ImportError:                                          # C17 미병합 — 전체 replay 폴백(잔류 판정 무영향)
    def filter_post_reset(df):                              # noqa: E731
        """C17 seg1 필터 부재 시 항등 폴백 — 잔류 승계 검증은 seg 무관."""
        return df
from .inject_control import InjectControl
from .scenario_injector import ScenarioInjector

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s │ %(levelname)-7s │ %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("chain-dry-run")

CHAIN_EVENTS_PATH = ROOT / "sim_events_chain.jsonl"

# 관측 창·허용치 (검증 전용 상수 — 파이프라인 설정 아님)
FINAL_OBSERVE = 40          # 스트림 말미 관측창 (챔버당 wafer)
CARRY_KEEP_RATIO = 0.65     # 잔류 판정: 마지막 창 이동 ≥ 기대 × 이 비율 (원복 아님)
TOL_ISOLATION_SIGMA = 0.6   # 컷 격리: 비대상 챔버 허용 이동 (σ, 대조군 대비)


def _raw_form(sc: dict) -> dict:
    """정규화 시나리오 → 파일드롭 원형(yaml) — S11이 떨어뜨리는 것과 동일 형태.

    inject-via=files 모드가 이 원형을 control 디렉토리에 쓰고 InjectControl이 집는다
    (= 프로덕션 주입 경로 그대로 검증). 컷은 단일 챔버·단일 센서 전제.
    """
    raw = {"scenario_id": sc["scenario_id"], "pattern": sc["pattern"],
           "target_chamber": sc["chambers"][0], "start_after_wafers": 0,
           "ground_truth": sc["ground_truth"]}
    if sc["pattern"] == "pm_reset":
        raw["level_shifts_eng"] = sc["level_shifts_eng"]
        raw["ignition_shifts_eng"] = sc.get("ignition_shifts_eng", {})
        raw["y_shift"] = sc.get("y_shift", 0.0)
    else:
        raw["sensor"] = sc["sensors"][0]
        raw["magnitude_sigma"] = sc["magnitude_sigma"]
        for k in ("duration_wafers", "period_wafers"):
            if k in sc:
                raw[k] = sc[k]
    return raw


def _cut_specs():
    """4컷 정의 — 정규화된 시나리오 dict(chambers·sensors 포함) + 주입 순번·잔류 여부."""
    return [
        {"name": "프롤로그·알람폭풍", "chamber": "SIM_CH_1", "at": 40, "persist": False,
         "sc": {"scenario_id": "CHAIN-storm-ch1", "pattern": "oscillation",
                "sensors": ["C11"], "chambers": ["SIM_CH_1"], "magnitude_sigma": 3.0,
                "period_wafers": 2, "start_after_wafers": 0, "ground_truth": "transient_storm"}},
        {"name": "1막·드리프트(잔류)", "chamber": "SIM_CH_3", "at": 80, "persist": True,
         "sc": {"scenario_id": "CHAIN-drift-ch3", "pattern": "drift",
                "sensors": ["C11"], "chambers": ["SIM_CH_3"], "magnitude_sigma": 3.0,
                "duration_wafers": 60, "start_after_wafers": 0, "ground_truth": "baseline_drift"}},
        {"name": "2막·요란PM(잔류)", "chamber": "SIM_CH_2", "at": 140, "persist": True,
         "sc": {"scenario_id": "CHAIN-pmreset-ch2", "pattern": "pm_reset",
                "sensors": ["C17"], "chambers": ["SIM_CH_2"],
                "level_shifts_eng": {"C17": -30.0}, "ignition_shifts_eng": {}, "y_shift": 0.0,
                "start_after_wafers": 0, "ground_truth": "regime_shift"}},
        {"name": "3막·레시피(잔류)", "chamber": "SIM_CH_4", "at": 200, "persist": True,
         "sc": {"scenario_id": "CHAIN-recipe-ch4", "pattern": "shift",
                "sensors": ["C4"], "chambers": ["SIM_CH_4"], "magnitude_sigma": 2.0,
                "start_after_wafers": 0, "ground_truth": "process_setpoint"}},
    ]


def _fmt(ok: bool) -> str:
    return "PASS ✅" if ok else "FAIL ❌"


def _expected_delta(cut: dict, std: float) -> float:
    """컷의 정착 후 기대 이동 — 잔류 판정 기준 (pm_reset=eng level, 그 외=mag_sigma×std)."""
    sc = cut["sc"]
    s = sc["sensors"][0]
    if sc["pattern"] == "pm_reset":
        return float(sc["level_shifts_eng"].get(s, 0.0))
    return sc["magnitude_sigma"] * std


def run_chain(args) -> int:
    """지속 스트림 + 런타임 주입 → 잔류 승계·격리·단조·답안지 판정. FAIL 수 반환."""
    params = load_params()
    sim_cfg = params["simulator"]
    chamber_count = sim_cfg["chamber_count"]
    offset_sigma = sim_cfg["chamber_offset_sigma"]
    noise_sigma = sim_cfg["chamber_noise_sigma"]

    log.info(f"📂 CSV 로딩: {args.csv}")
    df_full = pd.read_csv(args.csv)
    df = (filter_post_reset(df_full)
          if sim_cfg.get("replay_post_reset_only", False) else df_full)
    replicator = ChamberReplicator(df, chamber_count, offset_sigma,
                                   noise_sigma=noise_sigma, stats_df=df_full)

    if CHAIN_EVENTS_PATH.exists():
        CHAIN_EVENTS_PATH.unlink()
    injector = ScenarioInjector([], replicator._std, std_lookup=replicator.local_std, events_path=CHAIN_EVENTS_PATH)  # 빈 장전 = 상시
    ctl = None
    if args.inject_via == "files":                     # 프로덕션 채널(파일드롭) 경유 검증
        ctl_dir = Path(tempfile.mkdtemp(prefix="chain_ctl_"))
        ctl = InjectControl(ctl_dir, injector, min_interval_sec=0.0)
        log.info(f"🎚  inject-via=files — 제어 디렉토리 {ctl_dir}")
    cuts = _cut_specs()
    pending = {c["chamber"]: c for c in cuts}     # 챔버별 대기 컷 (챔버당 1컷 — 배역제)
    watch = sorted({c["sc"]["sensors"][0] for c in cuts})

    # ---- 지속 스트림 + 실행 중 주입 + 월드스테이트 스냅샷 ---------------------------
    series: dict[tuple, list] = defaultdict(list)   # (ch, sensor) → [(idx, wafer평균)]
    ws_snapshots: list[dict] = []                   # active_summary 이력 (단조 판정)
    idx_by_chamber: dict[str, int] = {}
    injected: list[str] = []
    total = 0
    for chamber_id, wafer, ch_idx, pm_count in replicator.stream_wafers(loop=False):
        if total >= args.limit:
            break
        idx_by_chamber[chamber_id] = ch_idx

        # 실행 중 주입 — 대상 챔버가 주입 순번에 도달하면 inject_now (S11 클릭 대리)
        cut = pending.get(chamber_id)
        if cut is not None and ch_idx >= cut["at"]:
            if ctl is None:                                  # direct — 주입기 API 직접
                injector.inject_now(cut["sc"], ch_idx)
            else:                                            # files — S11 클릭 재현(파일드롭→폴링)
                fp = ctl.control_dir / f"{cut['sc']['scenario_id']}.yaml"
                fp.write_text(yaml.safe_dump(_raw_form(cut["sc"]), allow_unicode=True),
                              encoding="utf-8")
                got = ctl.poll(dict(idx_by_chamber))
                if not got:
                    log.error(f"파일드롭 주입 실패: {fp.name} — failed/ 확인")
            injected.append(cut["sc"]["scenario_id"])
            pending.pop(chamber_id)
            log.info(f"💉 [{cut['name']}] 주입({args.inject_via}) @ {chamber_id} wafer#{ch_idx}")
            ws_snapshots.append(injector.active_summary(dict(idx_by_chamber)))

        # 센서 평균 수집 (settled 무관 — 이동 관측용, dry_run과 동일 집계)
        sums = defaultdict(float); cnts = defaultdict(int)
        step_counter: dict = {}
        wafer_id = f"{wafer['C64'].iloc[0]}_{chamber_id.replace('SIM_', '')}"
        for _, row in wafer.iterrows():
            step = int(row["C7"]) if pd.notna(row["C7"]) else -1
            step_counter[step] = step_counter.get(step, 0) + 1
            msg = build_message(chamber_id, row, step_counter[step], pm_count,
                                replicator, injector, ch_idx, wafer_id)
            for s in watch:
                v = msg["sensors"].get(s)
                if v is not None:
                    sums[s] += v; cnts[s] += 1
        for s in watch:
            if cnts[s]:
                series[(chamber_id, s)].append((ch_idx, sums[s] / cnts[s]))
        total += 1

    max_idx = {ch: (max(i for i, _ in series[(ch, watch[0])]) if series[(ch, watch[0])] else 0)
               for ch in [f"SIM_CH_{i}" for i in range(1, chamber_count + 1)]}
    log.info(f"생성 완료 — wafer {total:,}장 · 주입 {len(injected)}컷 · "
             f"챔버 말미 순번 {dict(sorted(max_idx.items()))}")

    # ---- 판정 ----------------------------------------------------------------
    results: list[tuple[str, bool, str]] = []

    def _median(xs):
        ys = sorted(xs)
        n = len(ys)
        return 0.0 if not ys else (ys[n // 2] if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2)

    def _delta(ch, s, lo, hi, base_hi):
        """(관측창[lo,hi) 평균) − (주입 전[<base_hi] 평균). 표본 없으면 None."""
        pts = series[(ch, s)]
        base = [v for i, v in pts if i < base_hi]
        obs = [v for i, v in pts if lo <= i < hi]
        if not base or not obs:
            return None
        return sum(obs) / len(obs) - sum(base) / len(base)

    # 각 챔버 말미 관측창 [end-FINAL_OBSERVE, end]
    win = {ch: (mx - FINAL_OBSERVE, mx + 1) for ch, mx in max_idx.items()}

    # C1 잔류 승계(원복 금지) + C2 컷 격리
    for cut in cuts:
        sc = cut["sc"]; s = sc["sensors"][0]; tch = cut["chamber"]
        std = replicator._std.get(s, 0.0)
        lo, hi = win[tch]
        exp = _expected_delta(cut, std)
        # 대조군 = 비대상 챔버들의 말미 이동 중앙값 (자연 시변 제거)
        ctrl = _median([d for ch in max_idx if ch != tch
                        for d in [_delta(ch, s, *win[ch], cut["at"])] if d is not None])
        d_t = _delta(tch, s, lo, hi, cut["at"])
        if cut["persist"]:
            if d_t is None:
                results.append((f"C1 잔류 {sc['scenario_id']}", False, "관측창 표본 없음"))
            else:
                adj = d_t - ctrl
                keep = abs(adj) >= CARRY_KEEP_RATIO * abs(exp) and (adj * exp > 0)
                results.append((f"C1 잔류 승계(원복 금지) {sc['scenario_id']}@{tch}", keep,
                                f"말미 Δ(보정)={adj:+.2f} vs 기대 {exp:+.2f} "
                                f"(유지율 {abs(adj)/abs(exp):.0%} ≥ {CARRY_KEEP_RATIO:.0%})"))
        # C2 격리 — 이 컷의 비대상 챔버(다른 컷 대상은 제외 — 정당 주입) 말미 무영향
        dirty = {c["chamber"] for c in cuts if c is not cut and s in c["sc"]["sensors"]}
        for ch in max_idx:
            if ch == tch or ch in dirty:
                continue
            d_o = _delta(ch, s, *win[ch], cut["at"])
            if d_o is not None and abs(d_o - ctrl) > TOL_ISOLATION_SIGMA * std:
                results.append((f"C2 격리 {sc['scenario_id']}·비대상 {ch}", False,
                                f"Δ={d_o - ctrl:+.2f} (> {TOL_ISOLATION_SIGMA}σ={TOL_ISOLATION_SIGMA*std:.2f})"))
    if not any(n.startswith("C2 격리") and not ok for n, ok, _ in results):
        results.append(("C2 컷 격리 (비대상 챔버 TTTM 무오염)", True,
                        f"전 컷 비대상 챔버 이동 ≤ {TOL_ISOLATION_SIGMA}σ"))

    # C3 월드스테이트 단조 — 활성 집합이 컷 진행 중 줄지 않음 (원복 금지의 상태 증거)
    persist_ids = {c["sc"]["scenario_id"] for c in cuts if c["persist"]}
    seen_persist: set = set()
    mono = True
    for snap in ws_snapshots:
        now_persist = {sid for act in snap.values() for sid in act if sid in persist_ids}
        if not seen_persist <= now_persist:            # 이전 잔류 id가 사라지면 위반
            mono = False
        seen_persist |= now_persist
    final_ws = injector.active_summary(dict(max_idx))
    final_persist = {sid for act in final_ws.values() for sid in act if sid in persist_ids}
    c3_ok = mono and final_persist == persist_ids
    results.append(("C3 월드스테이트 단조 (잔류 컷 원복 0건)", c3_ok,
                    f"말미 활성 잔류 {sorted(final_persist)} / 기대 {sorted(persist_ids)}"))

    # C4 답안지
    n_events = (sum(1 for _ in open(CHAIN_EVENTS_PATH, encoding="utf-8"))
                if CHAIN_EVENTS_PATH.exists() else 0)
    c4_ok = n_events >= len(cuts)
    results.append(("C4 답안지 기록 (sim_events 4컷)", c4_ok,
                    f"{n_events}건 기록 (기대 {len(cuts)}건) — {CHAIN_EVENTS_PATH.name}"))

    # ---- 리포트 ---------------------------------------------------------------
    fails = sum(1 for _, ok, _ in results if not ok)
    print("\n" + "=" * 78)
    print("M3 ② 월드스테이트 체인 DRY-RUN (DoD: 상시 + 실행 중 주입 + 컷 간 잔류 승계)")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"  {_fmt(ok):8} │ {name}\n           │   {detail}")
    print("-" * 78)
    print(f"  결과: {len(results) - fails}/{len(results)} PASS"
          + ("  →  M3 ② DoD 충족 🎉" if fails == 0 else f"  →  FAIL {fails}건"))
    print("=" * 78)
    return fails


def main():
    p = argparse.ArgumentParser(description="M3 ② 월드스테이트 체인 dry-run (Kafka 불요)")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    p.add_argument("--limit", type=int, default=1200, help="전체 wafer 수 (챔버당 limit/E1)")
    p.add_argument("--inject-via", choices=("files", "direct"), default="files",
                   help="주입 경로 — files=프로덕션 파일드롭 채널(기본)·direct=주입기 API")
    args = p.parse_args()
    if not args.csv.exists():
        log.error(f"❌ CSV 없음: {args.csv}")
        sys.exit(2)
    sys.exit(1 if run_chain(args) else 0)


if __name__ == "__main__":
    main()
