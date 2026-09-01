# -*- coding: utf-8 -*-
"""라벨 정합 계약 테스트 (torch 무의존 · pandas/numpy 만).

대상 (2026-08-03 신설):
  `dump_ae_input.order_inversions`      — 기록 순번 ↔ C10 역행 계량
  `build_clean_wafer_list.wafer_index`  — `_wafer_idx` 우선 · 폴백 · 건전성 가드
  `build_clean_wafer_list.alignment_check` / `verify` — wafer 단위 정합률

배경: injector 는 주입 시점을 **챔버 내 기록 순번**으로 정하는데 그 순서는 `C10`(원본
replay 시각) 과 일치하지 않는다(챔버당 300건 이상 역행 실측). 라벨러가 `C10` 순위로
순번을 되짚는 바람에 CH2 라벨 일치율이 80.2% 였고, 주입 38장이 '정상'으로 학습에
유입됐다. 그런데 당시 가드(pre/post **평균** 증분 ≥0.5σ)는 그 상태를 **통과시켰다** —
평균은 20% 뒤섞여도 여전히 크게 밀리기 때문이다.

이 파일이 고정하는 것은 두 가지다:
  ① 순번은 추정이 아니라 기록에서 온다 (`_wafer_idx`).
  ② 폴백으로 추정하더라도, 뒤섞임은 **wafer 단위 대조**가 반드시 잡는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_clean_wafer_list as B  # noqa: E402
import dump_ae_input as D           # noqa: E402   (simulator 의존 없는 순수 함수만 사용)

IDX = B.WAFER_IDX_COL
SENSOR = "C11"
ROWS_PER_WAFER = 3
SETTLE_STEP = 4.0


# ── 합성 데이터 ────────────────────────────────────────────────────────────
def make_pair(n_per_chamber=120, start=60, magnitude=4.0, shuffle_time=True,
              chambers=("SIM_CH_1", "SIM_CH_2"), injected_chamber="SIM_CH_2",
              pattern="shift", seed=0, chamber_offset=0.0):
    """(sim, ctl, events) 합성. 주입은 **기록 순번** 기준으로 넣는다.

    `shuffle_time=True` 면 `C10` 을 기록 순번과 어긋나게 배치한다 — 실데이터에서 관측된
    역행을 재현해, `C10` 순위 추정이 틀리는 상황을 만든다.
    `chamber_offset` 은 주입 챔버 전 wafer 에 상수로 더해지는 챔버 오프셋(주입 아님).
    `magnitude` 가 음수면 음의 방향 주입 — 실데이터의 C17·C62 가 그렇다.
    """
    rng = np.random.default_rng(seed)
    sim_rows, ctl_rows = [], []
    for ch in chambers:
        base = rng.normal(100.0, 1.0, n_per_chamber)          # wafer 간 σ ≈ 1.0
        order = rng.permutation(n_per_chamber) if shuffle_time else np.arange(n_per_chamber)
        for i in range(n_per_chamber):
            wid = f"W{ch}_{i:04d}"
            t0 = 1_000_000 + int(order[i]) * 100              # 기록 순번 ↔ 시각 어긋남
            delta = chamber_offset if ch == injected_chamber else 0.0
            if ch == injected_chamber and i >= start:
                k = i - start
                delta += magnitude if pattern == "shift" else magnitude * min(1.0, k / 40.0)
            for r in range(ROWS_PER_WAFER):
                common = {"C64": wid, "C24": ch, "C10": t0 + r, "C7": SETTLE_STEP,
                          "C6": "C6_0", IDX: i}
                noise = float(rng.normal(0, 0.05))
                ctl_rows.append({**common, SENSOR: base[i] + noise})
                sim_rows.append({**common, SENSOR: base[i] + noise + delta})
    events = [{
        "event_id": f"EV-{injected_chamber}", "scenario_id": "SC-TEST",
        "chamber_id": injected_chamber, "sensor_ids": [SENSOR], "pattern": pattern,
        "magnitude_sigma": magnitude, "injected_at_wafer_index": start,
    }]
    return pd.DataFrame(sim_rows), pd.DataFrame(ctl_rows), events


# ── ① 순번의 출처 ──────────────────────────────────────────────────────────
def test_wafer_idx_is_preferred_over_time_rank():
    """`_wafer_idx` 가 있으면 pos 는 기록 순번 +1 이고 출처가 기록된다."""
    sim, _, _ = make_pair()
    w = B.wafer_index(sim)
    assert (w["pos_source"] == IDX).all()
    merged = w.merge(sim.groupby("C64")[IDX].first().rename("truth"), on="C64")
    assert (merged["pos"] == merged["truth"] + 1).all()


def test_time_rank_fallback_is_marked_not_silent():
    """컬럼이 없으면 폴백하되 **추정임을 표시**한다 (조용한 추정 금지)."""
    sim, _, _ = make_pair()
    w = B.wafer_index(sim.drop(columns=[IDX]))
    assert w["pos_source"].iloc[0].startswith("t0_rank")


def test_fallback_can_be_forbidden():
    """`--require-wafer-idx` 경로 — 추정 자체를 금지할 수 있다."""
    sim, _, _ = make_pair()
    with pytest.raises(ValueError, match=IDX):
        B.wafer_index(sim.drop(columns=[IDX]), allow_fallback=False)


def test_wafer_idx_soundness_guards():
    """중복·불연속 순번은 통과시키지 않는다 (CSV 절단·복수 실행 혼입 방어)."""
    sim, _, _ = make_pair(n_per_chamber=40, start=20)
    dup = sim.copy()
    dup.loc[dup["C64"] == "WSIM_CH_1_0005", IDX] = 4               # 4 가 두 장
    with pytest.raises(ValueError, match="중복"):
        B.wafer_index(dup)

    gap = sim[sim[IDX] != 7].copy()                                # 7 번이 통째로 빠짐
    with pytest.raises(ValueError, match="연속"):
        B.wafer_index(gap)


def test_order_inversions_counts_the_mismatch():
    """덤프 시점에 역행을 계량한다 — 생성 시점에 보이면 하류가 안 속는다."""
    sim, _, _ = make_pair(shuffle_time=True)
    assert sum(D.order_inversions(sim).values()) > 0
    aligned, _, _ = make_pair(shuffle_time=False)
    assert sum(D.order_inversions(aligned).values()) == 0
    assert D.order_inversions(sim.drop(columns=[IDX])) == {}       # 컬럼 없으면 계량 불가


# ── ② 뒤섞임 탐지 ──────────────────────────────────────────────────────────
def _diff_frame(sim, ctl, w, chamber):
    """verify 내부와 같은 좌표계(wafer별 sim−control, wafer 간 σ 단위)를 만든다."""
    S = sim[sim.C7 == SETTLE_STEP].groupby(["C24", "C64"])[[SENSOR]].mean()
    C = ctl[ctl.C7 == SETTLE_STEP].groupby(["C24", "C64"])[[SENSOR]].mean()
    sd = C.groupby(level=0).apply(lambda g: g.std(ddof=0)).mean()
    diff = (S - C).reset_index().merge(w[["C24", "C64", "pos"]], on=["C24", "C64"])
    return diff[diff.C24 == chamber], float(sd[SENSOR])


def test_alignment_is_perfect_when_labels_come_from_record_order():
    """정합한 라벨이면 일치율 100% — 정상 경로가 통과함을 고정한다."""
    sim, ctl, events = make_pair()
    w = B.wafer_index(sim)
    g, sd = _diff_frame(sim, ctl, w, "SIM_CH_2")
    rate, det = B.alignment_check(g, SENSOR, sd, events[0])
    assert det["judged"] and rate == pytest.approx(1.0)
    assert det["라벨누락"] == 0 and det["라벨과다"] == 0


def test_alignment_catches_shuffled_labels_that_mean_guard_misses():
    """★핵심 회귀 — 2026-08-03 사고 재현.

    `C10` 순위로 추정한 라벨은 뒤섞여 있는데, **평균 증분 가드는 통과**한다.
    wafer 단위 대조만이 그 사실을 드러낸다.
    """
    sim, ctl, events = make_pair()
    w_bad = B.wafer_index(sim.drop(columns=[IDX]))                 # 오정렬 라벨
    g, sd = _diff_frame(sim, ctl, w_bad, "SIM_CH_2")

    start = events[0]["injected_at_wafer_index"]
    pre, post = g[g.pos <= start], g[g.pos > start]
    mean_shift = (post[SENSOR].mean() - pre[SENSOR].mean()) / sd
    assert mean_shift >= B.VERIFY_MIN_SIGMA, "구 가드가 통과시키는 상황이어야 의미가 있다"

    rate, det = B.alignment_check(g, SENSOR, sd, events[0])
    assert det["judged"] and rate < B.ALIGN_MIN_RATE
    assert det["라벨누락"] > 0, "주입인데 정상으로 라벨된 wafer = 학습 유입분"


def test_verify_raises_on_misaligned_labels_and_passes_on_aligned():
    """end-to-end — 같은 데이터에서 순번 출처만 바꾸면 판정이 갈린다."""
    sim, ctl, events = make_pair()

    w_ok = B.label(B.wafer_index(sim), events)
    B.verify(w_ok, events, sim, ctl, settle_step=SETTLE_STEP)      # 예외 없이 통과

    w_bad = B.label(B.wafer_index(sim.drop(columns=[IDX])), events)
    with pytest.raises(ValueError, match="정합률"):
        B.verify(w_bad, events, sim, ctl, settle_step=SETTLE_STEP)


# ── ③ 램프형 오판정 방지 ────────────────────────────────────────────────────
def test_ramp_is_judged_on_mature_region_not_the_whole_span():
    """★램프는 설계상 초기에 검출 불가다 (실측 SC1-DRIFT: 300장 램프가 k=114 에서 경계 통과).

    주입 구간 전체를 요구하면 **정합한 라벨도 FAIL** 한다 — 구 고정 warmup 20 의 실패.
    성숙 구간만 판정하면 통과하고, 유예된 장수는 기록에 남는다.
    """
    sim, ctl, events = make_pair(pattern="drift", magnitude=4.0, shuffle_time=False)
    w = B.wafer_index(sim)
    g, sd = _diff_frame(sim, ctl, w, "SIM_CH_2")

    rate, det = B.alignment_check(g, SENSOR, sd, events[0])
    assert det["judged"] and det["ramp"] and det["성숙유예"] > 0
    assert rate >= B.ALIGN_MIN_RATE, "정합한 램프 라벨이 오판정되면 안 된다"
    assert det["단조성"] > B.ALIGN_RAMP_RHO_MIN

    flat = dict(events[0], pattern="shift")                        # 성숙 유예 미적용으로 강제
    rate_flat, det_flat = B.alignment_check(g, SENSOR, sd, flat)
    assert det_flat["성숙유예"] == 0
    assert rate_flat < rate, "성숙 유예가 실제로 오판정을 걷어내는지"


def test_ramp_duration_in_events_sets_the_mature_point():
    """정답지가 램프 길이를 실으면 성숙 기준을 **추정하지 않는다** (k 중앙값 폴백 대신)."""
    sim, ctl, events = make_pair(pattern="drift", magnitude=4.0, shuffle_time=False)
    w = B.wafer_index(sim)
    g, sd = _diff_frame(sim, ctl, w, "SIM_CH_2")

    _, det_est = B.alignment_check(g, SENSOR, sd, events[0])
    assert "미상" in det_est["성숙근거"]

    _, det_known = B.alignment_check(g, SENSOR, sd, dict(events[0], duration_wafers=40))
    assert det_known["성숙기준k"] == int(40 * B.ALIGN_RAMP_MATURE_FRAC)
    assert "미상" not in det_known["성숙근거"]


def test_ramp_monotonicity_catches_shuffled_labels():
    """성숙 구간 일치율을 통과해도, 순서가 무너지면 단조성이 잡는다 (램프 길이 무관 신호)."""
    sim, ctl, events = make_pair(pattern="drift", magnitude=4.0, shuffle_time=True)
    w_ok = B.wafer_index(sim)
    g_ok, sd = _diff_frame(sim, ctl, w_ok, "SIM_CH_2")
    _, det_ok = B.alignment_check(g_ok, SENSOR, sd, events[0])
    assert det_ok["단조성"] > B.ALIGN_RAMP_RHO_MIN

    w_bad = B.wafer_index(sim.drop(columns=[IDX]))                 # C10 추정 = 뒤섞임
    g_bad, _ = _diff_frame(sim, ctl, w_bad, "SIM_CH_2")
    _, det_bad = B.alignment_check(g_bad, SENSOR, sd, events[0])
    assert det_bad["단조성"] < B.ALIGN_RAMP_RHO_MIN


def test_alignment_is_immune_to_constant_chamber_offset():
    """★`sim − control` 에는 챔버 오프셋이 상수로 섞인다 (2026-08-03 CH2/C11 = +3.2σ 이상).

    절대 임계("0.5σ 넘으면 주입")로 재면 오프셋 큰 챔버는 전 wafer 가 '주입'으로 보인다.
    라벨 없는 군집 분할이라 상수 이동에 불변이어야 한다.
    """
    sim, ctl, events = make_pair(chamber_offset=8.0)
    w = B.wafer_index(sim)
    g, sd = _diff_frame(sim, ctl, w, "SIM_CH_2")
    assert (g[SENSOR] / sd > B.VERIFY_MIN_SIGMA).all(), "전 wafer 가 절대 임계를 넘는 상황이어야 한다"
    rate, det = B.alignment_check(g, SENSOR, sd, events[0])
    assert det["judged"] and rate == pytest.approx(1.0)


def test_alignment_handles_negative_direction_injection():
    """음의 방향 주입 — 실데이터 C17·C62 가 그렇다 (부호 가정을 두면 전량 미탐)."""
    sim, ctl, events = make_pair(magnitude=-4.0, chamber_offset=3.0)
    events[0]["magnitude_sigma"] = 4.0                             # 정답지는 크기만 기록
    w = B.wafer_index(sim)
    g, sd = _diff_frame(sim, ctl, w, "SIM_CH_2")
    rate, det = B.alignment_check(g, SENSOR, sd, events[0])
    assert det["judged"] and rate == pytest.approx(1.0)


def test_alignment_skips_judgement_when_clusters_are_not_separated():
    """군집이 안 갈리면 판정하지 않는다 — 약한 신호로 '오정렬'을 선언하지 않기.

    두 경로 모두 `judged=False` + 사유를 남긴다: ⓐ 주입 0 → 분할 자체 실패,
    ⓑ 주입은 있으나 임계 미만(0.2σ) → 분리도 미달.
    """
    for mag in (0.0, 0.2):
        sim, ctl, events = make_pair(magnitude=mag)
        w = B.wafer_index(sim)
        g, sd = _diff_frame(sim, ctl, w, "SIM_CH_2")
        _, det = B.alignment_check(g, SENSOR, sd, events[0])
        assert det["judged"] is False, f"magnitude={mag} 에서 판정되면 안 된다"
        assert det.get("note"), "판정 생략은 사유가 남아야 한다 (조용한 생략 금지)"


def test_alignment_skips_judgement_on_small_sample():
    """표본이 하한 미달이면 비율 판정은 이진에 가까워 의미가 없다 — 생략하고 알린다."""
    sim, ctl, events = make_pair(n_per_chamber=20, start=10)
    w = B.wafer_index(sim)
    g, sd = _diff_frame(sim, ctl, w, "SIM_CH_2")
    _, det = B.alignment_check(g, SENSOR, sd, events[0])
    assert det["judged"] is False and det["n"] < B.ALIGN_MIN_WAFERS
