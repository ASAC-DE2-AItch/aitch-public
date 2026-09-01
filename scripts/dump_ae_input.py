# -*- coding: utf-8 -*-
"""AE 포화 재현용 입력 덤프 — 시뮬이 fdc.raw로 실제 발행한 센서값을 CSV로 복원.

배경: 2026-07-29 리허설에서 AE가 전 wafer를 ae=1.000(천장)으로 판정했다. AE 입력은
파일이 아니라 시뮬이 즉석 생성한 값(원본 replay + 챔버 오프셋/노이즈)이라 넘겨줄 파일이
없었다. 시뮬은 seed 고정이므로 여기서 동일하게 재현해 덤프한다.

**진단 설계 — 대조군 포함**: 같은 wafer를 두 벌 낸다.
  · `ae_input_sim.csv`     = 시뮬 발행값 (replicator.perturb 적용 — AE가 실제로 본 것)
  · `ae_input_control.csv` = 원본 replay값 (perturb 미적용 — AE 학습 분포와 동일 계열)
control은 정상(ae 낮음)인데 sim만 포화면 → **원인 = 챔버 오프셋/노이즈**로 확정된다.
둘 다 포화면 → 오프셋 무관(리플레이 세그먼트·피처 파이프라인 쪽) 이다.

주입(injector)은 `--scenario` 를 줄 때만 적용한다 (2026-08-03 신설 — 아래).

**`_wafer_idx` 컬럼 (2026-08-03 신설 — 라벨 정합)**: injector 는 주입 시점을 **챔버 내
기록(스트림) 순번**으로 정하는데, 이 순번은 `C10`(원본 replay 시각) 순서와 **일치하지
않는다** (챔버당 300건 이상 역행 실측). 소비자가 `C10` 순위로 순번을 되짚으면 라벨이
어긋나고(CH2 실측 일치율 80.2%), 주입 wafer가 '정상'으로 학습에 유입된다. 그래서 순번을
**추정 대상이 아니라 기록 대상**으로 바꾼다 — 챔버 내 0-기반 순번을 컬럼으로 싣는다.
`_` 접두는 물리 채널이 아니라는 표시다(`_ts` 선례). AE 피처 추출은 명시 컬럼 목록만
집계하므로 이 컬럼은 모델 입력에 들어가지 않는다.

사용:
    python scripts/dump_ae_input.py                 # 챔버당 10 wafer (기본)
    python scripts/dump_ae_input.py --per-chamber 25
    python scripts/dump_ae_input.py --per-chamber 500 --suffix _v3 \
        --scenario src/simulator/scenarios/alarm_storm_ch2.yaml
출력: Data/ae_repro/ae_input_sim.csv · ae_input_control.csv (+ README.md
      · --scenario 시 dump_events.jsonl 정답지)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from simulator.chamber_replicator import ChamberReplicator          # noqa: E402
from simulator.message_builder import (DEFAULT_CSV_PATH, DROP_COLUMNS,   # noqa: E402
                                       TOP_LEVEL_COLUMNS, load_params)
from simulator.scenario_injector import ScenarioInjector, load_scenario   # noqa: E402

OUT_DIR = ROOT / "Data" / "ae_repro"

# 챔버 내 0-기반 기록 순번 = injector 의 `chamber_wafer_index` 와 **같은 변수**.
# 소비자(build_clean_wafer_list.py)는 이 컬럼을 우선 사용하고, 없을 때만 C10 순위로 추정한다.
WAFER_IDX_COL = "_wafer_idx"


def order_inversions(df: pd.DataFrame) -> dict[str, int]:
    """챔버별 '기록 순번 ↔ C10 시각 순서' 역행 건수 (인접 쌍 기준).

    0이 아니면 **C10 순위로 순번을 되짚는 소비자는 라벨이 어긋난다**. 이 수치를 덤프
    시점에 찍어두는 이유: 2026-08-03 사고에서 이 불일치는 재학습·게이트·PM 보고를 전부
    지나간 뒤에야 드러났다. 생성 시점에 보이면 그 경로가 원천 차단된다.

    Args:
        df: `_wafer_idx`·`C24`·`C10`·`C64` 를 가진 행 단위 프레임.

    Returns:
        {챔버: 역행 건수}. 컬럼이 없으면 빈 dict.
    """
    if WAFER_IDX_COL not in df.columns or "C10" not in df.columns:
        return {}
    w = (df.groupby(["C24", "C64"])
           .agg(idx=(WAFER_IDX_COL, "first"), t0=("C10", "min"))
           .reset_index())
    out = {}
    for ch, g in w.groupby("C24"):
        g = g.sort_values("idx")
        out[str(ch)] = int((g["t0"].diff() < 0).sum())      # 기록 순서로 갈 때 시각이 되감기는 횟수
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="AE 포화 재현 입력 덤프")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    ap.add_argument("--per-chamber", type=int, default=10, help="챔버당 wafer 수")
    ap.add_argument("--scenario", nargs="*", default=None,
                    help="주입 시나리오 YAML 경로(복수 가능). 주면 주입을 적용하고 "
                         "정답지 dump_events.jsonl 을 함께 산출한다 "
                         "(예: src/simulator/scenarios/alarm_storm_ch2.yaml)")
    ap.add_argument("--suffix", default="", help="출력 파일명 접미 (예: _v3 → ae_input_sim_v3.csv)")
    ap.add_argument("--full-replay", action="store_true",
                    help="seg1 필터 무시 — 68일 전체(M-m-m-M 사이클 완전체) 리플레이. "
                         "CT² train/valid/test 분할용 (A 요청 2026-08-05: '최소 한 사이클'). "
                         "⚠️ seg0(리셋 전 레짐) 포함 — 소급 필터 통과 여부는 A 판단. "
                         "오프셋 자(stats_df=전체)는 원래부터 전체 기준이라 라이브 세계와 동일.")
    a = ap.parse_args()

    params = load_params()
    sim = params["simulator"]
    n_ch = sim["chamber_count"]

    print(f"[1/4] CSV 로드: {a.csv}")
    df_full = pd.read_csv(a.csv)
    try:                                   # 시뮬과 동일한 seg1 필터 (replay_post_reset_only)
        from simulator.message_builder import filter_post_reset
        df = (filter_post_reset(df_full)
              if (sim.get("replay_post_reset_only") and not a.full_replay) else df_full)
        if a.full_replay:
            print("       ⚠️ full-replay: seg1 필터 해제 — 68일 전체 (seg0 레짐 포함)")
    except ImportError:
        df = df_full

    # 시뮬과 동일 파라미터·시드 — 오프셋 재현성 (kafka_producer.run과 같은 인자)
    rep = ChamberReplicator(df, n_ch, sim["chamber_offset_sigma"],
                            noise_sigma=sim["chamber_noise_sigma"], stats_df=df_full)
    print(f"[2/4] replicator: 챔버 {n_ch} · offset_sigma {sim['chamber_offset_sigma']} "
          f"· noise {sim['chamber_noise_sigma']} · seed 42")

    # perturb 대상 = build_message의 sensors 산정과 동일 (상위필드·드롭·C33 제외)
    excluded = TOP_LEVEL_COLUMNS | DROP_COLUMNS | {"C33"}
    sensor_cols = [c for c in df.columns
                   if c not in excluded and pd.api.types.is_numeric_dtype(df[c])]

    # 시나리오 주입 (2026-08-03 신설) — 지금까지 이 스크립트는 정상만 덤프했다.
    # 그 결과 주입 포함 세트는 다른 경로로 만들어졌고, 재학습이 주입을 "정상"으로
    # 학습하는 사고로 이어졌다(models/CHANGELOG.md 정정 ②). 여기서 주입과 **정답지**를
    # 함께 산출해 재현 경로를 하나로 만든다.
    injector = None
    if a.scenario:
        scenarios = [load_scenario(Path(p)) for p in a.scenario]
        events_path = OUT_DIR / "dump_events.jsonl"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if events_path.exists():
            # 이어붙이면 여러 실행분이 섞여 "어느 실행이 이 CSV를 만들었는지" 알 수 없다
            # (build_clean_wafer_list 가 파라미터 불일치로 거부하는 상황). 매 덤프 새로 쓴다.
            events_path.unlink()
        injector = ScenarioInjector(scenarios, rep._std, events_path=events_path)
        print(f"[2/4] 시나리오 장전: " + ", ".join(s["scenario_id"] for s in scenarios))

    sim_rows, ctl_rows = [], []
    per: dict[str, int] = {}
    for chamber_id, wafer, ch_idx, pm_count in rep.stream_wafers(loop=False):
        if per.get(chamber_id, 0) >= a.per_chamber:
            if len(per) >= n_ch and all(v >= a.per_chamber for v in per.values()):
                break
            continue
        per[chamber_id] = per.get(chamber_id, 0) + 1
        wid = f"{wafer['C64'].iloc[0]}_{chamber_id.replace('SIM_', '')}"
        idx = per[chamber_id] - 1                   # 챔버 내 0-기반 순번 (injector 규약)

        ctl = wafer.copy()
        ctl["C64"] = wid
        ctl["C24"] = chamber_id                     # AE chamber_col
        ctl[WAFER_IDX_COL] = idx                    # ★라벨 정합의 단일 소스 (C10 추정 금지)
        ctl_rows.append(ctl)

        pert = ctl.copy()                           # _wafer_idx 승계 (sim·control 동일값)
        for col in sensor_cols:                     # build_message와 동일 경로
            vals = []
            for v in wafer[col]:
                if pd.isna(v):
                    vals.append(v)
                    continue
                x = rep.perturb(chamber_id, col, float(v))
                if injector is not None:            # perturb 뒤 주입 — build_message 순서 동일
                    x += injector.delta(chamber_id, col, idx)
                vals.append(x)
            pert[col] = vals
        sim_rows.append(pert)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sim_df = pd.concat(sim_rows, ignore_index=True)
    ctl_df = pd.concat(ctl_rows, ignore_index=True)
    if "C65" in sim_df.columns:                     # 타겟 제외 (헌법 1-3 — 서빙 입력 정합)
        sim_df = sim_df.drop(columns=["C65"]); ctl_df = ctl_df.drop(columns=["C65"])
    sim_name, ctl_name = f"ae_input_sim{a.suffix}.csv", f"ae_input_control{a.suffix}.csv"
    sim_df.to_csv(OUT_DIR / sim_name, index=False)
    ctl_df.to_csv(OUT_DIR / ctl_name, index=False)
    print(f"[3/4] 저장: {OUT_DIR}")
    print(f"       {sim_name}     {len(sim_df):,}행 / wafer {sim_df['C64'].nunique()}장")
    print(f"       {ctl_name} {len(ctl_df):,}행 / wafer {ctl_df['C64'].nunique()}장")

    inv = order_inversions(sim_df)
    n_inv = sum(inv.values())
    print(f"       {WAFER_IDX_COL} 기록 · 기록순번↔C10 역행 {n_inv}건 {inv if n_inv else ''}")
    if n_inv:
        print(f"       ⓘ 역행이 있으므로 라벨은 반드시 {WAFER_IDX_COL} 로 붙여야 한다 "
              f"(C10 순위 추정 시 오정렬 — build_clean_wafer_list.py 가 강제)")
    if injector is not None:
        ev = OUT_DIR / "dump_events.jsonl"
        n_ev = sum(1 for _ in open(ev, encoding="utf-8")) if ev.exists() else 0
        print(f"       dump_events.jsonl    {n_ev}건 (정답지 — build_clean_wafer_list.py 입력)")
        if n_ev == 0:
            print("       ⚠️ 주입 이벤트 0건 — start_after_wafers 가 --per-chamber 보다 큰지 "
                  "확인하세요 (주입 시점에 도달하지 못하면 정답지가 비고 학습셋이 오염된 채 "
                  "정상으로 보인다)")

    # 오프셋 크기 요약 — 어느 센서가 얼마나 밀렸는지 (진단 힌트)
    diff = []
    for col in sensor_cols:
        d = (sim_df[col] - ctl_df[col]).abs().mean()
        s = df_full[col].std()
        if pd.notna(d) and pd.notna(s) and s > 0:
            diff.append((col, d, d / s))
    diff.sort(key=lambda x: -x[2])
    lines = ["# AE 포화 재현 세트 (2026-07-29)", "",
             "리허설에서 AE가 전 wafer를 ae=1.000으로 판정한 건의 재현용 입력입니다.",
             "시뮬은 seed 고정이라 아래 두 파일이 당시 발행값을 그대로 복원합니다.", "",
             "| 파일 | 내용 |", "|---|---|",
             "| `ae_input_sim.csv` | **AE가 실제로 본 값** — 원본 replay + 챔버 오프셋/노이즈 |",
             "| `ae_input_control.csv` | **대조군** — 같은 wafer의 원본 replay값 (오프셋 미적용) |", "",
             "## 보는 법", "",
             "두 파일에 AE를 각각 돌려보시면 원인이 갈립니다:", "",
             "- control 정상 + sim 포화 → **원인 = 챔버 오프셋/노이즈** (재캘리 또는 오프셋 축소)",
             "- 둘 다 포화 → 오프셋 무관 → 리플레이 세그먼트(seg1) 또는 피처 파이프라인 쪽", "",
             f"`C24` 컬럼에 챔버 id(SIM_CH_1~4)를 넣어뒀습니다 (AE `chamber_col`). "
             f"타겟 `C65`는 제외했습니다.", "",
             "## `_wafer_idx` — 라벨 정합의 단일 소스", "",
             "챔버 내 **0-기반 기록 순번**입니다. injector 가 주입 시점을 정할 때 쓰는 바로 그 "
             "변수(`chamber_wafer_index`)라, 정답지 라벨은 이 컬럼으로 붙여야 합니다.", "",
             f"- 기록순번 ↔ `C10` 시각 순서 **역행 {n_inv}건** {inv if n_inv else ''}",
             "- 역행이 있으면 `C10` 순위로 순번을 추정하는 라벨러는 어긋납니다 "
             "(2026-08-03 실측 CH2 일치율 80.2% — 주입 38장이 '정상'으로 학습에 유입)",
             "- `_` 접두 = 물리 채널 아님(`_ts` 선례). AE 피처 추출은 명시 컬럼만 집계하므로 "
             "모델 입력에 들어가지 않습니다", "",
             "## 오프셋 실측 — |sim − control| 평균 (센서 std 대비 상위 12)", "",
             "| 센서 | 평균 차 | std 대비 |", "|---|---|---|"]
    for col, d, r in diff[:12]:
        lines.append(f"| {col} | {d:.4f} | {r:.3f}σ |")
    lines += ["", f"전 센서 평균 {sum(x[2] for x in diff)/max(len(diff),1):.3f}σ "
                  f"(설정 `chamber_offset_sigma` = {sim['chamber_offset_sigma']})"]
    (OUT_DIR / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[4/4] README.md 생성 — 오프셋 최대 {diff[0][2]:.2f}σ ({diff[0][0]})" if diff else "[4/4] README.md")


if __name__ == "__main__":
    main()
