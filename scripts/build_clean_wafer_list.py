# -*- coding: utf-8 -*-
"""주입 정답지(dump_events.jsonl) → 재학습용 정상 wafer 목록 + 검출 평가 라벨.

배경: 2026-08-02 재학습 후보 3종은 주입 wafer 653장이 `C6_0`(정상)으로 학습에 섞여
전량 무효 처리됐다 (models/CHANGELOG.md 정정 ②). 주입은 센서값만 흔들고 레시피 라벨을
바꾸지 않으므로, **정답지 기반 제외가 유일한 정확한 필터**다.

산출:
  · <out>/wafers_clean.txt      — 주입 미포함 정상 wafer (retrain `--wafers-file` 입력)
  · <out>/injection_labels.csv  — 전 wafer 라벨 (wafer·chamber·pos·injected·scenario·k·expected_sigma)
                                  → 재학습 후 **검출률 평가**의 정답지

경계 규약 (`scenario_injector._active`): `k = chamber_wafer_index - start_after_wafers`,
k ≥ 0 이면 활성. `injected_at_wafer_index` = start_after_wafers 다. chamber_wafer_index는
0-기반이므로 **1-기반 순번 pos 로는 `pos > start` (= pos ≥ start+1)** 가 주입 구간이다.
이 규약은 `--verify`(기본 on)가 control 쌍 대조로 실측 검증한다 — 어긋나면 즉시 중단한다.

★ 순번(pos)의 출처 (2026-08-03 정정): `chamber_wafer_index` 는 **기록(스트림) 순번**이고
`C10`(원본 replay 시각) 순서와 일치하지 않는다 — 챔버당 300건 이상 역행 실측. 구 구현은
`C10` 순위로 pos 를 추정했고 그 결과 CH2 라벨 일치율이 80.2% 였다(주입 38장이 '정상'으로
학습 유입 · 정상 61장이 '주입'으로 평가 제외). 이제 덤프가 싣는 `_wafer_idx` 컬럼을
**우선 사용**하고, 없을 때만 경고와 함께 t0 순위로 폴백한다. 폴백 경로는 아래 강화된
검증(wafer 단위 일치율)이 받쳐준다 — 평균 증분만 보는 구 가드는 80% 오정렬을 통과시켰다.

실행 (torch 불필요 · pandas만):
    python scripts/build_clean_wafer_list.py \
        --events "src/agent_a_mlops/Autoencoder/재보정용 데이터/dump_events.jsonl" \
        --sim    "src/agent_a_mlops/Autoencoder/재보정용 데이터/ae_input_sim_v2.csv" \
        --control "src/agent_a_mlops/Autoencoder/재보정용 데이터/ae_input_control_v2.csv"
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("ae.cleanlist")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "src" / "agent_a_mlops" / "Autoencoder" / "재보정용 데이터"
VERIFY_MIN_SIGMA = 0.5    # 주입 후 구간에서 요구하는 최소 증분 (wafer 간 σ) — 답안지 정합 가드
VERIFY_PRE_MAX = 0.5      # 주입 전 구간에서 허용하는 최대 증분 (같은 자)

WAFER_IDX_COL = "_wafer_idx"   # dump_ae_input.py 가 싣는 챔버 내 0-기반 기록 순번 (단일 소스)

# ── wafer 단위 정합 검증 (2026-08-03 신설) ────────────────────────────────────
# 구 가드(pre/post **평균** 증분 ≥0.5σ)는 라벨이 뒤섞여도 통과한다: 주입분이 80%만 맞아도
# 평균은 여전히 크게 밀리기 때문이다. 실제로 CH2 80.2% 오정렬이 이 가드를 그대로 지나갔다.
# 그래서 wafer **한 장씩** "라벨이 주입이라 말하는가"와 "실측이 밀렸는가"를 대조한다.
ALIGN_MIN_RATE = 0.95     # 라벨 ↔ 실측 일치율 하한 (미만이면 오정렬로 보고 중단)
ALIGN_MIN_WAFERS = 30     # 판정 가능 최소 표본 — 미달이면 비율이 이진에 가까워 의미 없음
RAMP_PATTERNS = {"drift", "ramp"}   # 크기가 서서히 오르는 패턴 (step/shift 는 즉시 최대)

# ── 램프형 판정 (2026-08-03 실측 반영) ────────────────────────────────────────
# 램프는 "주입 구간 전체가 검출 가능"하지 않다 — 설계상 그렇다. `drift_c11.yaml` 은
# 2.5σ 를 `duration_wafers: 300` 에 걸쳐 선형으로 올리므로, 실측에서 군집 경계를 넘은
# 시점은 **k=114** 였다. 그 앞 구간의 미검출을 '오정렬'로 세면 정합한 라벨도 FAIL 한다
# (구 고정 warmup 20 의 실패). 그래서 램프는 두 축으로 본다:
#   ⓐ 성숙 구간(k ≥ mature)에서의 일치율 — 크기가 충분히 커진 뒤에도 안 맞으면 오정렬
#   ⓑ 단조성 Spearman(k, d) — 라벨이 뒤섞이면 순서가 무너진다. 램프 길이를 몰라도
#      성립하는 강한 신호다 (정합 실측 1.000).
# `라벨누락`(정상 라벨인데 주입 군집)은 **패턴과 무관하게 상시 판정**한다 — 학습 유입
# 방향이라 램프 물리로 변명되지 않는다.
ALIGN_RAMP_RHO_MIN = 0.7       # ⓑ 단조성 하한
ALIGN_RAMP_MATURE_FRAC = 0.5   # ⓐ 성숙 시작 = 램프 길이 × 이 비율 (duration 을 알 때)
ALIGN_RAMP_MIN_MATURE_K = 20   # duration 미상 시 성숙 하한 (그 위는 k 중앙값으로 잡는다)
DURATION_KEYS = ("duration_wafers", "ramp_wafers")   # 정답지가 램프 길이를 실을 때의 키


def load_events(path: Path) -> list[dict]:
    """정답지 JSONL → 이벤트 목록 (event_id 중복 제거 — 덤프 재실행분).

    같은 event_id가 파라미터까지 동일하면 마지막 1건만 남긴다. 파라미터가 다르면
    어느 실행이 현 CSV를 만들었는지 알 수 없으므로 **명시적 에러**로 올린다
    (조용한 추측 금지 — 헌법 7장 "예외가 안 났으니 성공으로 간주" 취지).
    """
    rows = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{i} — dict 아님 (정답지 형식 위반)")
        rows.append(obj)
    if not rows:
        raise ValueError(f"{path}: 이벤트 0건")

    keyed: dict[str, dict] = {}
    sig = lambda e: (e["chamber_id"], tuple(e["sensor_ids"]), e["pattern"],
                     e.get("magnitude_sigma"), e["injected_at_wafer_index"])
    for e in rows:
        eid = e["event_id"]
        if eid in keyed and sig(keyed[eid]) != sig(e):
            raise ValueError(
                f"event_id {eid} 가 서로 다른 파라미터로 중복 기록됨 — 어느 실행이 현 CSV를 "
                f"만들었는지 특정 불가. 정답지를 실행 단위로 분리하세요.")
        keyed[eid] = e
    log.info("정답지 %d행 → 고유 이벤트 %d건", len(rows), len(keyed))
    return list(keyed.values())


def wafer_index(sim: pd.DataFrame, allow_fallback: bool = True) -> pd.DataFrame:
    """wafer 단위 축약 — 챔버별 1-기반 순번(pos) 부여.

    pos 의 출처는 두 가지이고 **우선순위가 있다**:
      ① `_wafer_idx`(덤프 기록 순번) → `pos = _wafer_idx + 1`. injector 가 주입 시점을
         정할 때 쓴 바로 그 변수라 정합이 정의상 보장된다.
      ② 폴백: `C10` 최소시각 순위. 기록 순서와 어긋날 수 있어(역행 실측) **추정**이다.

    ②로 떨어지면 경고하고, 두 순서가 실제로 얼마나 어긋나는지는 알 수 없으므로
    (기준이 없다) 하류 검증에 의존한다. 산출 프레임에 `pos_source` 를 남겨
    라벨 CSV 소비자가 근거를 알 수 있게 한다 (조용한 추정 금지).

    Args:
        sim: 시뮬 발행 CSV 행 프레임.
        allow_fallback: False 면 `_wafer_idx` 부재를 에러로 올린다 (`--require-wafer-idx`).

    Raises:
        ValueError: 필수 컬럼 부재, `_wafer_idx` 가 챔버 내 중복·불연속, 폴백 금지 시 부재.
    """
    for col in ("C64", "C24", "C10"):
        if col not in sim.columns:
            raise ValueError(f"필수 컬럼 {col} 없음")

    has_idx = WAFER_IDX_COL in sim.columns
    agg = {"t0": ("C10", "min")}
    if "C6" in sim.columns:
        agg["c6"] = ("C6", "first")
    if has_idx:
        agg["widx"] = (WAFER_IDX_COL, "first")
    w = sim.groupby(["C24", "C64"]).agg(**agg).reset_index()

    if not has_idx:
        if not allow_fallback:
            raise ValueError(
                f"{WAFER_IDX_COL} 컬럼 없음 — 순번 추정이 금지된 모드입니다. "
                f"dump_ae_input.py(2026-08-03 이후)로 덤프를 재생성하세요.")
        log.warning(
            "★%s 컬럼 없음 → C10 순위로 **추정**합니다. 기록 순번과 어긋나면 라벨이 오정렬되고 "
            "(2026-08-03 실측 CH2 80.2%%) 주입 wafer 가 정상으로 학습에 유입됩니다. "
            "덤프를 재생성하는 것이 정공법입니다.", WAFER_IDX_COL)
        w["pos"] = w.groupby("C24")["t0"].rank(method="first").astype(int)
        w["pos_source"] = "t0_rank(추정)"
    else:
        idx = w["widx"]
        if idx.isna().any():
            raise ValueError(f"{WAFER_IDX_COL} 에 결측 — 덤프가 손상됐습니다")
        w["pos"] = idx.astype(int) + 1                       # 0-기반 → 1-기반 (pos > start 규약)
        w["pos_source"] = WAFER_IDX_COL
        for ch, g in w.groupby("C24"):                        # 순번 자체의 건전성
            vals = sorted(g["pos"].tolist())
            if len(set(vals)) != len(vals):
                raise ValueError(f"{ch}: {WAFER_IDX_COL} 중복 — 챔버 내 순번이 유일해야 합니다")
            if vals != list(range(1, len(vals) + 1)):
                raise ValueError(
                    f"{ch}: {WAFER_IDX_COL} 가 1..{len(vals)} 연속이 아님 "
                    f"(관측 {vals[0]}..{vals[-1]}) — CSV 가 잘렸거나 여러 실행이 섞였습니다")
        inv = int((w.sort_values(["C24", "pos"]).groupby("C24")["t0"].diff() < 0).sum())
        log.info("pos 출처 = %s (기록 순번) · 기록순번↔C10 역행 %d건%s",
                 WAFER_IDX_COL, inv,
                 " — C10 추정이었다면 오정렬됐을 구간" if inv else "")
        w = w.drop(columns=["widx"])
    return w.sort_values(["C24", "pos"]).reset_index(drop=True)


def label(w: pd.DataFrame, events: list[dict]) -> pd.DataFrame:
    """정답지 적용 — injected 플래그 + scenario_id·k·expected_sigma 부여.

    여러 이벤트가 한 wafer에 겹치면 **가장 이른 시작**을 대표로 적고 sigma는 합산하지
    않는다(대표 표기). 제외 판정은 겹침과 무관하게 injected=True 다.
    """
    w = w.copy()
    w["injected"] = False
    w["scenario_id"] = ""
    w["k"] = -1
    w["expected_sigma"] = 0.0
    for e in events:
        ch, start = e["chamber_id"], int(e["injected_at_wafer_index"])
        mag = float(e.get("magnitude_sigma") or 0.0)
        m = (w.C24 == ch) & (w.pos > start)          # 0-기반 index ≥ start ⇔ 1-기반 pos > start
        if not m.any():
            log.warning("이벤트 %s: 해당 구간 wafer 0장 (챔버 %s · start %d)",
                        e["event_id"], ch, start)
            continue
        k = w.loc[m, "pos"] - start - 1              # 주입 후 경과 (0-기반)
        first = w.loc[m & (w.scenario_id == "")].index
        w.loc[first, "scenario_id"] = e["scenario_id"]
        w.loc[first, "k"] = k.loc[first]
        w.loc[m, "expected_sigma"] = np.maximum(w.loc[m, "expected_sigma"], mag)
        w.loc[m, "injected"] = True
        log.info("  %s · %s · %s %s %.1fσ · start %d → %d장 주입",
                 e["event_id"], ch, e["pattern"], e["sensor_ids"], mag, start, int(m.sum()))
    return w


def rank_corr(a, b) -> float:
    """순위 상관 (Spearman) — scipy 무의존. 분모 0이면 예외 (헌법 7장: 분모 검증)."""
    ra = pd.Series(np.asarray(a, float)).rank().to_numpy(float, copy=True)
    rb = pd.Series(np.asarray(b, float)).rank().to_numpy(float, copy=True)
    ra -= ra.mean(); rb -= rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    if denom == 0:
        raise ValueError("순위 상관 분모 0 — 표본/분산을 확인하세요")
    return float((ra * rb).sum() / denom)


def split_two_groups(v: np.ndarray) -> float:
    """1차원 값을 두 군집으로 가르는 임계 (군집 내 제곱합 최소 — 결정적·난수 없음).

    **라벨을 쓰지 않는다**는 점이 핵심이다. 라벨 기준으로 임계를 잡으면 그 라벨이 맞는지
    검증할 수 없다(순환). 값 자체의 구조에서 경계를 찾은 뒤 라벨과 대조해야 정합을 잰다.
    """
    s = np.sort(v)
    n = len(s)
    c1 = np.cumsum(s)
    c2 = np.cumsum(s ** 2)
    k = np.arange(1, n)                                    # 왼쪽 군집 크기
    sse_l = c2[:-1] - c1[:-1] ** 2 / k
    sse_r = (c2[-1] - c2[:-1]) - (c1[-1] - c1[:-1]) ** 2 / (n - k)
    best = int(np.argmin(sse_l + sse_r))
    return float((s[best] + s[best + 1]) / 2.0)


def alignment_check(g: pd.DataFrame, sen: str, sd_sen: float, e: dict) -> tuple[float, dict]:
    """wafer **한 장씩** 라벨 ↔ 실측을 대조해 정합률을 낸다. (rate, 상세) 반환.

    왜 평균으로는 부족한가: pre/post **평균** 증분은 라벨의 20%가 뒤섞여도 여전히 크게
    밀린다 — 2026-08-03 CH2 오정렬(일치율 80.2%)이 구 가드를 그대로 통과한 이유다.
    반면 wafer 단위 대조는 뒤섞인 만큼 정확히 그 비율로 떨어진다.

    ★절대 임계를 쓰지 않는 이유 (2026-08-03 실측): `sim − control` 에는 주입뿐 아니라
    **챔버 오프셋이 상수로 섞여 있다**. CH2/C11 은 주입 여부와 무관하게 전 wafer 가
    +3.2σ 이상 밀려 있었고(오프셋만으로), 반대로 C17·C62 는 주입 방향이 **음수**였다.
    그래서 "0.5σ 넘으면 주입"식 판정은 전량 오탐/전량 미탐이 된다. 대신 챔버 안에서
    값 구조만으로 두 군집을 가르고(`split_two_groups`) 그 분할을 라벨과 대조한다 —
    상수 오프셋과 주입 부호에 둘 다 불변이다.

    램프형(drift)은 설계상 초기 크기가 0에 가까워 **주입 구간 전체를 요구하면 정합한
    라벨도 FAIL 한다** (실측: 2.5σ/300장 램프가 k=114 에서야 군집 경계를 넘었다). 그래서
    램프는 성숙 구간(k ≥ `mature_k`)에서만 일치율을 재고, 대신 단조성 Spearman(k, d) 을
    함께 낸다 — 램프 길이를 몰라도 성립하는 뒤섞임 신호다. `mature_k` 는 정답지의
    `duration_wafers` 가 있으면 그것으로, 없으면 k 중앙값으로 잡고 근거를 기록한다.
    **`라벨누락`(정상 라벨인데 주입 군집)은 패턴과 무관하게 상시 판정한다** — 학습 유입
    방향이라 램프 물리로 변명되지 않는다.

    Args:
        g: 한 챔버의 wafer별 (sim−control) 차이 + `pos`.
        sen: 대상 센서. sd_sen: **그 챔버의** wafer 간 σ (분모). e: 정답지 이벤트.

    Returns:
        (일치율, 상세). 상세 = {n, ramp, judged, 분리도, 판정표본, 성숙유예, 성숙기준k,
        성숙근거, 라벨누락, 라벨과다, 군집비율차, (램프면) 단조성}.
        판정 불가(표본 하한 미달·한쪽 라벨 전무·군집 분리 실패·성숙 표본 부족)면
        `judged=False` 이고 `note` 에 사유가 남는다 (조용한 생략 금지).
    """
    start = int(e["injected_at_wafer_index"])
    labeled = (g["pos"] > start).to_numpy()
    dd = (g[sen].to_numpy(float)) / sd_sen
    k = g["pos"].to_numpy() - start - 1                      # 주입 후 경과 (0-기반)
    is_ramp = str(e.get("pattern", "")).lower() in RAMP_PATTERNS

    base = {"n": int(len(labeled)), "ramp": is_ramp, "judged": False}
    if len(labeled) < ALIGN_MIN_WAFERS or labeled.all() or (~labeled).all():
        return float("nan"), {**base, "note": "표본 하한 미달 또는 한쪽 라벨 전무"}

    # ── 라벨 없이 두 군집으로 분할 (상수 오프셋·주입 부호에 불변) ──
    thr = split_two_groups(dd)
    high = dd >= thr
    if high.all() or (~high).all():
        return float("nan"), {**base, "note": "군집 분할 실패 (주입 흔적 없음)"}
    sep = abs(float(dd[high].mean() - dd[~high].mean()))
    if sep < VERIFY_MIN_SIGMA:
        return float("nan"), {**base, "분리도": round(sep, 2),
                              "note": f"군집 분리도 {sep:.2f}σ < {VERIFY_MIN_SIGMA}"}
    # 어느 군집이 '주입'인가 — 라벨이 가리키는 쪽. 라벨이 통째로 뒤집힌 병리는
    # `군집비율차`가 드러낸다 (일치율만으로는 반전이 100%로 보인다).
    pred = high if dd[labeled].mean() >= dd[~labeled].mean() else ~high

    # ── 판정 표본: 정상 라벨 전량 + (램프면 성숙 구간, 아니면 주입 라벨 전량) ──
    mature_k, mature_src = 0, "전 구간"
    if is_ramp:
        dur = next((int(e[key]) for key in DURATION_KEYS if e.get(key)), None)
        if dur:
            mature_k, mature_src = int(dur * ALIGN_RAMP_MATURE_FRAC), f"길이 {dur}×{ALIGN_RAMP_MATURE_FRAC:g}"
        else:                                                # 정답지에 램프 길이가 없다
            mature_k = max(ALIGN_RAMP_MIN_MATURE_K, int(np.median(k[labeled])))
            mature_src = "k 중앙값(길이 미상)"
    judged = (~labeled) | (labeled & (k >= mature_k))
    n_defer = int((labeled & (k < mature_k)).sum())
    if int(judged.sum()) < ALIGN_MIN_WAFERS or not (labeled & judged).any():
        return float("nan"), {**base, "분리도": round(sep, 2),
                              "note": f"성숙 표본 부족 (k≥{mature_k} 주입 {int((labeled & judged).sum())}장)"}

    rate = float((pred[judged] == labeled[judged]).mean())
    det = {
        **base, "judged": True, "분리도": round(sep, 2),
        "판정표본": int(judged.sum()), "성숙유예": n_defer, "성숙기준k": mature_k,
        "성숙근거": mature_src,
        "라벨누락": int((pred & ~labeled).sum()),   # 정상 라벨인데 주입 군집 → **학습 유입** (상시 판정)
        "라벨과다": int((~pred & labeled & judged).sum()),   # 성숙 구간인데 정상 군집
        "군집비율차": round(abs(float(pred[judged].mean() - labeled[judged].mean())), 3),
    }
    if is_ramp:
        # 단조성 — 라벨이 뒤섞이면 k 순서와 크기 순서가 무너진다. 램프 길이를 몰라도 성립.
        det["단조성"] = round(rank_corr(k[labeled], dd[labeled]), 3)
    return rate, det


def verify(w: pd.DataFrame, events: list[dict], sim: pd.DataFrame,
           ctl: pd.DataFrame, settle_step=4.0) -> None:
    """control 쌍 대조로 정답지 경계를 실측 검증 (불일치 시 raise).

    자 = **wafer 간 σ** (챔버 내 wafer별 settle 평균의 표준편차). 전역 row σ로 재면
    스텝 간 변동에 희석돼 3σ 주입이 0.4σ로 보이고 "주입 실패"로 오판한다.
    """
    if "C7" not in sim.columns:
        log.warning("C7 없음 → settle 구간 한정 불가, 전 행 평균으로 검증")
        s, c = sim, ctl
    else:
        s, c = sim[sim.C7 == settle_step], ctl[ctl.C7 == settle_step]
    sensors = sorted({x for e in events for x in e["sensor_ids"]})
    S = s.groupby(["C24", "C64"])[sensors].mean()
    C = c.groupby(["C24", "C64"])[sensors].mean()
    diff = (S - C).reset_index().merge(w[["C24", "C64", "pos"]], on=["C24", "C64"])
    sd_by_ch = C.groupby(level=0).apply(lambda g: g.std(ddof=0))     # 챔버별 wafer 간 σ
    sd = sd_by_ch.mean()                                             # 전 챔버 평균 (총계 검사용)

    bad = []
    for e in events:
        ch, start = e["chamber_id"], int(e["injected_at_wafer_index"])
        g = diff[diff.C24 == ch]
        pre, post = g[g.pos <= start], g[g.pos > start]
        if pre.empty or post.empty:
            bad.append(f"{e['event_id']}: 전/후 구간 부족 (pre {len(pre)} · post {len(post)})")
            continue
        for sen in e["sensor_ids"]:
            if sen not in sd or not np.isfinite(sd[sen]) or sd[sen] <= 0:
                bad.append(f"{e['event_id']}/{sen}: σ 산출 불가 (분모 0)")
                continue
            d = float((post[sen].mean() - pre[sen].mean()) / sd[sen])
            ok = d >= VERIFY_MIN_SIGMA
            log.info("  검증 %s/%s: 증분 %+.2fσ (요구 ≥%.1f) %s",
                     e["event_id"], sen, d, VERIFY_MIN_SIGMA, "OK" if ok else "★불일치")
            if not ok:
                bad.append(f"{e['event_id']}/{sen}: 증분 {d:+.2f}σ < {VERIFY_MIN_SIGMA}")
                continue

            # ── wafer 단위 정합 (구 평균 가드가 놓치는 '뒤섞임'을 잡는다) ──
            # σ 는 **그 챔버의** wafer 간 σ 를 쓴다 — 챔버마다 분산이 달라 전 챔버 평균으로
            # 정규화하면 군집 분리도가 왜곡된다.
            sd_ch = float(sd_by_ch.loc[ch, sen]) if sen in sd_by_ch.columns else float("nan")
            if not np.isfinite(sd_ch) or sd_ch <= 0:
                bad.append(f"{e['event_id']}/{sen}: 챔버 {ch} σ 산출 불가 (분모 0)")
                continue
            rate, det = alignment_check(g, sen, sd_ch, e)
            if not det["judged"]:
                log.warning("  정합 %s/%s: 판정 생략 — %s",
                            e["event_id"], sen, det.get("note", "표본/라벨 편중"))
                continue
            mono = det.get("단조성")
            log.info("  정합 %s/%s: 일치율 %.1f%% (판정 %d장%s · 분리도 %.2fσ) · "
                     "라벨누락 %d · 라벨과다 %d%s %s",
                     e["event_id"], sen, rate * 100, det["판정표본"],
                     f" · 성숙유예 {det['성숙유예']}(k<{det['성숙기준k']}, {det['성숙근거']})"
                     if det["성숙유예"] else "",
                     det["분리도"], det["라벨누락"], det["라벨과다"],
                     f" · 단조성 {mono:+.3f}" if mono is not None else "",
                     "OK" if rate >= ALIGN_MIN_RATE else "★오정렬")
            if rate < ALIGN_MIN_RATE:
                bad.append(
                    f"{e['event_id']}/{sen}: 라벨 정합률 {rate:.1%} < {ALIGN_MIN_RATE:.0%} "
                    f"(라벨누락 {det['라벨누락']}장 = 주입인데 정상으로 학습에 유입 · "
                    f"라벨과다 {det['라벨과다']}장). 순번 출처를 확인하세요 — "
                    f"pos 를 C10 으로 추정했다면 {WAFER_IDX_COL} 가 있는 덤프로 재생성")
            # 램프 단조성 — 성숙 구간 일치율이 통과해도 순서가 무너졌으면 뒤섞인 것이다.
            if mono is not None and mono < ALIGN_RAMP_RHO_MIN:
                bad.append(
                    f"{e['event_id']}/{sen}: 램프 단조성 {mono:+.3f} < {ALIGN_RAMP_RHO_MIN} "
                    f"— 주입 구간에서 경과(k)와 크기가 함께 커지지 않습니다. 라벨 순서가 "
                    f"뒤섞였거나 정답지가 이 CSV 의 것이 아닙니다")
    # 비대상 챔버에 주입 흔적이 있으면 정답지가 이 CSV와 다른 실행의 것이다
    targets = {e["chamber_id"] for e in events}
    for ch in sorted(set(diff.C24) - targets):
        g = diff[diff.C24 == ch]
        for sen in sensors:
            if sen not in sd or sd[sen] <= 0:
                continue
            half = len(g) // 2
            d = float((g.sort_values("pos")[sen].iloc[half:].mean()
                       - g.sort_values("pos")[sen].iloc[:half].mean()) / sd[sen])
            if abs(d) > VERIFY_PRE_MAX:
                bad.append(f"비대상 {ch}/{sen}: 전후 증분 {d:+.2f}σ — 정답지 불일치 의심")
    if bad:
        raise ValueError("정답지 검증 실패 (CSV와 다른 실행의 정답지일 수 있음):\n  - "
                         + "\n  - ".join(bad))
    log.info("정답지 검증 통과 — 대상 챔버 주입 확인 · 비대상 챔버 무흔적")


def main(argv=None) -> int:
    """정답지 → 정상 목록 + 라벨 산출."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | [ae.cleanlist] %(message)s")
    ap = argparse.ArgumentParser(description="주입 제외 정상 wafer 목록 생성")
    ap.add_argument("--events", type=Path, default=DATA_DIR / "dump_events.jsonl")
    ap.add_argument("--sim", type=Path, default=DATA_DIR / "ae_input_sim_v2.csv")
    ap.add_argument("--control", type=Path, default=DATA_DIR / "ae_input_control_v2.csv")
    ap.add_argument("--out", type=Path, default=DATA_DIR)
    ap.add_argument("--normal-col", default="C6")
    ap.add_argument("--normal-value", default="C6_0")
    ap.add_argument("--no-verify", action="store_true", help="control 쌍 대조 검증 생략(비권장)")
    ap.add_argument("--require-wafer-idx", action="store_true",
                    help=f"{WAFER_IDX_COL} 부재 시 C10 추정 폴백을 금지하고 즉시 중단")
    a = ap.parse_args(argv)

    events = load_events(a.events)
    sim = pd.read_csv(a.sim)
    w = wafer_index(sim, allow_fallback=not a.require_wafer_idx)
    estimated = str(w["pos_source"].iloc[0]).startswith("t0_rank")
    w = label(w, events)

    # 추정 순번 + 검증 생략 = 오정렬이 아무 데서도 안 잡히는 조합. 실제로 이 조합이
    # 2026-08-03 사고의 경로였다 (라벨이 틀린 줄 모른 채 재학습·게이트·PM 보고까지 통과).
    if estimated and a.no_verify:
        log.error("pos 를 C10 으로 추정한 상태에서 --no-verify 는 허용하지 않습니다 — "
                  "오정렬을 잡을 방어선이 하나도 남지 않습니다. 덤프를 재생성하거나 "
                  "검증을 켜세요.")
        return 1

    if not a.no_verify:
        verify(w, events, sim, pd.read_csv(a.control))

    if a.normal_col in w.columns or "c6" in w.columns:
        col = "c6" if "c6" in w.columns else a.normal_col
        recipe_ok = w[col].astype(str) == str(a.normal_value)
    else:
        log.warning("정상 라벨 컬럼 없음 → 레시피 필터 생략")
        recipe_ok = pd.Series(True, index=w.index)

    clean = w[recipe_ok & ~w.injected]
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "wafers_clean.txt").write_text(
        "\n".join(clean.sort_values(["C24", "pos"]).C64.astype(str)) + "\n", encoding="utf-8")
    w.assign(recipe_normal=recipe_ok).to_csv(a.out / "injection_labels.csv", index=False)

    tot, inj = len(w), int(w.injected.sum())
    log.info("전체 %d장 | 레시피 정상 %d | 주입 %d (%.1f%%) | **학습용 정상 %d장**",
             tot, int(recipe_ok.sum()), inj, 100 * inj / tot, len(clean))
    log.info("챔버별 정상: %s", clean.groupby("C24").size().to_dict())
    log.info("순번 출처: %s%s", w["pos_source"].iloc[0],
             " ← 라벨 신뢰도가 이 값에 달려 있다 (labels CSV 의 pos_source 컬럼에 동봉)"
             if estimated else "")
    log.info("산출: %s · %s", a.out / "wafers_clean.txt", a.out / "injection_labels.csv")
    log.info("다음: cd src/agent_a_mlops/Autoencoder && python -m ae_pipeline.retrain "
             "--data <sim_v2> --out <새 번들> --wafers-file <wafers_clean.txt> "
             "--model-version z16_rev4_clean --calib-version calib_v3_clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
