# -*- coding: utf-8 -*-
"""analyze.py — 3단계: 지표 집계 · 통계 · 사전등록 판정 · 그래프 (설계서 §6~§8).

지표 (설계서 §6)
  M1 pooled RMSE (주지표) + honest R²
  M2 구간분해 RMSE — ⓐ major PM 후 14일 ⓑ minor PM 후 3일 ⓒ 평시
  M3 열화 곡선 — 모델 나이 0~6 슬롯별 RMSE (ARM-D7 내부 직접 측정)
  M4 예측 churn — 고정 프로브 셋, 인접 모델 버전 간 예측 MAE
  M5 일별 RMSE + 7일 rolling
  M6 비용 — 재학습 1회 wall-clock 실측 × 횟수

통계 (설계서 §6): 일별 RMSE 쌍의 paired Wilcoxon signed-rank(α=0.05)
  + ΔRMSE 의 moving-block bootstrap 95% CI (블록 7일 — 자기상관 반영)

판정 (설계서 §7): R1~R4 사전 등록 규칙. 임계는 CONFIG (✱ = PM·멘토 확정 대상)
부속 (설계서 §8-A): 승격 게이트 3.0% 소급 시뮬레이션

산출: run_<tag>/metrics.json · daily_rmse.csv · fig_*.png
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import CONFIG as C

sys.path.insert(0, str(C.LEAN85_DIR))
import lean85_pipeline as lp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("analyze")

SIGMA_C65 = lp.SIGMA_C65
if abs(SIGMA_C65 - C.SIGMA_C65) > 1e-9:      # 이중 소스 방지 (보고서는 CONFIG 값을 인쇄한다)
    raise ValueError(f"SIGMA_C65 불일치: lean85_pipeline {SIGMA_C65} vs CONFIG {C.SIGMA_C65}")


def rmse(y, p) -> float:
    """표본 RMSE. 빈 배열이면 NaN 을 돌려준다(구간 분해에서 표본 0 구간 대비)."""
    y, p = np.asarray(y, float), np.asarray(p, float)
    if len(y) == 0:
        return float("nan")
    return float(np.sqrt(np.mean((y - p) ** 2)))


def r2_honest(r: float) -> float:
    """honest R² = 1 - (RMSE/σ)². σ 는 C65 전체 표준편차(동결 261.7)."""
    return float(1 - (r / SIGMA_C65) ** 2)


# ──────────────────────────────────────────────────────────────────────
# 군별 예측 재구성
# ──────────────────────────────────────────────────────────────────────
def arm_predictions(preds: pd.DataFrame, assign: pd.DataFrame) -> pd.DataFrame:
    """(평가일 → 군별 컷오프) 배정에 따라 예측 기록을 군별로 선택.

    반환: [arm, wf_day, cutoff, model_age, C64, y, pred, low_conf]
    """
    out = []
    for arm in C.ARMS:
        key = assign[["wf_day", arm]].rename(columns={arm: "cutoff"})
        sel = preds.merge(key, on=["wf_day", "cutoff"], how="inner")
        if len(sel) == 0:
            raise ValueError(f"{arm}: 배정에 해당하는 예측 기록 0건 — 백테스트 산출물 확인")
        sel = sel.assign(arm=arm)
        out.append(sel)
    res = pd.concat(out, ignore_index=True)
    # 무결성: 군마다 평가 웨이퍼 집합이 동일해야 공정 비교다
    n = res.groupby("arm")[lp.ID_COL].nunique()
    if n.nunique() != 1:
        raise ValueError(f"군별 평가 웨이퍼 수 불일치 — 공정 비교 불가: {n.to_dict()}")
    return res


# ──────────────────────────────────────────────────────────────────────
# 구간 라벨 (설계서 §6 M2)
# ──────────────────────────────────────────────────────────────────────
def segment_labels(days: pd.DatetimeIndex) -> pd.DataFrame:
    """평가일 → 구간 라벨: onset_major / onset_minor / steady (+ warmup 플래그)."""
    led = pd.read_csv(C.PM_LEDGER)
    # pm_ledger 는 minor(나노초)·major(초) 정밀도가 섞여 있다 → ISO8601 파서 사용
    led["time"] = pd.to_datetime(led["time"], format="ISO8601")
    major = led.loc[led["kind"] == "major", "time"].dt.normalize().tolist()
    minor = led.loc[led["kind"] == "minor", "time"].dt.normalize().tolist()

    lab = []
    for d in days:
        seg = "steady"
        if any(0 <= (d - m).days < C.ONSET_MAJOR_DAYS for m in major):
            seg = "onset_major"
        elif any(0 <= (d - m).days < C.ONSET_MINOR_DAYS for m in minor):
            seg = "onset_minor"
        lab.append(seg)
    warm = [(d - days[0]).days < C.WARMUP_DAYS for d in days]
    return pd.DataFrame({"wf_day": days, "segment": lab, "is_warmup": warm})


# ──────────────────────────────────────────────────────────────────────
# 통계 (설계서 §6)
# ──────────────────────────────────────────────────────────────────────
def moving_block_bootstrap_ci(delta: np.ndarray, block: int, n_boot: int,
                              seed: int, alpha: float) -> tuple[float, float]:
    """ΔRMSE 계열의 moving-block bootstrap 95% CI (자기상관 반영)."""
    d = np.asarray(delta, float)
    d = d[np.isfinite(d)]
    n = len(d)
    if n < block * 2:
        raise ValueError(f"bootstrap 표본 부족: n={n}, block={block}")
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts_max = n - block
    means = np.empty(n_boot)
    for b in range(n_boot):
        st = rng.integers(0, starts_max + 1, size=n_blocks)
        idx = (st[:, None] + np.arange(block)[None, :]).ravel()[:n]
        means[b] = d[idx].mean()
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def paired_test(a: np.ndarray, b: np.ndarray) -> dict:
    """paired Wilcoxon signed-rank (α=0.05). scipy 부재 시 명시적 예외."""
    try:
        from scipy.stats import wilcoxon
    except ImportError as e:
        raise ImportError("scipy 필요 — 설계서 §6 통계 처리(Wilcoxon)") from e
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 10:
        raise ValueError(f"유효 쌍 부족: {int(m.sum())}")
    stat, p = wilcoxon(a[m], b[m], alternative="two-sided", zero_method="wilcox")
    return {"n_pairs": int(m.sum()), "statistic": float(stat), "p_value": float(p),
            "significant": bool(p < C.ALPHA)}


# ──────────────────────────────────────────────────────────────────────
# 판정 (설계서 §7)
# ──────────────────────────────────────────────────────────────────────
def verdict(m: dict) -> dict:
    """R1~R4 사전 등록 판정. 순서: R4(안전장치) → R1 → R3 → R2."""
    d1, d7, st = m["M1"]["ARM-D1"], m["M1"]["ARM-D7"], m["M1"]["ARM-STATIC"]
    gain = (d7["pooled_rmse"] - d1["pooled_rmse"]) / d7["pooled_rmse"] * 100
    sig = m["stats"]["D1_vs_D7"]["significant"]
    st_sig = m["stats"]["STATIC_vs_D7"]["significant"]

    onset = m["M2"].get("onset_major", {})
    onset_gain = (float("nan") if not onset else
                  (onset["ARM-D7"] - onset["ARM-D1"]) / onset["ARM-D7"] * 100)

    if not st_sig:
        rule, call = "R4", ("주기 판정 보류 — ARM-STATIC 이 D7 과 유의차 없음. "
                            "합성 구간이 재학습 필요성을 재현하지 못했다는 신호로 읽고 "
                            "데이터 타당성 재검토(§7 한계)로 회귀한다.")
    elif gain >= C.R1_MIN_REL_GAIN_PCT and sig:
        rule, call = "R1", ("일간 유지 확정 — 실증 종결, 멘토 보고. "
                            f"D1 이 D7 대비 pooled RMSE {gain:.2f}% 개선 + 유의(α={C.ALPHA}).")
    elif np.isfinite(onset_gain) and onset_gain >= C.R3_ONSET_MIN_REL_GAIN_PCT:
        rule, call = "R3", ("하이브리드 제안 — 평시 7일 + 요란 PM 직후 즉시 재학습. "
                            f"전체 개선 {gain:.2f}%(<{C.R1_MIN_REL_GAIN_PCT}%✱)이나 "
                            f"온셋 구간 D1 우위 {onset_gain:.2f}%(≥{C.R3_ONSET_MIN_REL_GAIN_PCT}%✱). "
                            "※ 온셋 표본 = major PM 2회뿐 — 효과 크기 중심 서술, 유의성 주장 금지(§7 한계).")
    else:
        rule, call = "R2", ("7일 완화안 작성 — 비용·churn 대비 이득 부족. "
                            f"개선 {gain:.2f}% ({'유의' if sig else '비유의'}). "
                            "멘토 확정 사항 이탈이므로 예외 2 선례 절차(근거 패키지 + 사후 공유).")

    return {"rule": rule, "call": call,
            "pooled_rel_gain_pct_D1_over_D7": round(float(gain), 3),
            "onset_major_rel_gain_pct": (None if not np.isfinite(onset_gain)
                                         else round(float(onset_gain), 3)),
            "significant_D1_vs_D7": sig, "significant_STATIC_vs_D7": st_sig,
            "thresholds": {"R1_min_rel_gain_pct✱": C.R1_MIN_REL_GAIN_PCT,
                           "R3_onset_min_rel_gain_pct✱": C.R3_ONSET_MIN_REL_GAIN_PCT,
                           "alpha": C.ALPHA},
            "note": "✱ 임계는 사전 등록 제안값 — PM·멘토 확정 대상 (결과 보고서 부록 A-4)"}


# ──────────────────────────────────────────────────────────────────────
# §8-A 게이트 시뮬레이션
# ──────────────────────────────────────────────────────────────────────
def gate_simulation(gate: pd.DataFrame, arm: str) -> dict:
    """§8-A 게이트 시뮬 결과 요약 — 판정 자체는 `run_backtest.py` 가 **인라인으로** 수행한다.

    왜 인라인인가: 챔피언–챌린저 비교는 **같은 웨이퍼 집합**에서 두 모델을 돌려야 한다.
    챔피언은 승격 시점 이후 임의로 오래될 수 있어(승격이 드물수록 더) 예측 기록만으로는
    재구성할 수 없다 — 백테스트 루프가 챔피언 모델을 메모리에 들고 있을 때만 계산된다.
    평가 셋 = 챌린저가 담당할 날의 웨이퍼로, **챔피언·챌린저 양쪽 모두 out-of-sample** 이다
    (두 컷오프 모두 그 날짜 이전). 라벨을 쓰는 사후 소급 판정이며 주 분석에는 영향이 없다.
    """
    g = gate[gate["arm"] == arm]
    dec = g["decision"].value_counts().to_dict()
    judged = g[g["decision"].isin(["promote", "keep"])]
    n_prom = int(dec.get("promote", 0))
    n_judged = int(len(judged))
    gains = pd.to_numeric(judged["gain_pct"], errors="coerce").dropna()
    return {"arm": arm,
            "n_triggers": int(len(g) - dec.get("initial", 0)),
            "n_judged": n_judged, "n_promoted": n_prom,
            "promote_rate_pct": round(100 * n_prom / n_judged, 2) if n_judged else None,
            "threshold_pct": C.PROMOTE_MIN_RMSE_GAIN_PCT,
            "gain_pct_mean": round(float(gains.mean()), 3) if len(gains) else None,
            "gain_pct_median": round(float(gains.median()), 3) if len(gains) else None,
            "gain_pct_p95": round(float(gains.quantile(.95)), 3) if len(gains) else None,
            "max_champion_age_days": (int(judged["champ_age_days"].max())
                                      if "champ_age_days" in judged and len(judged) else None),
            "decisions": dec,
            "eval_set": "챌린저 담당일 웨이퍼 (챔피언·챌린저 모두 out-of-sample)",
            "caveat": "사후 소급 판정 — 운영 게이트는 결정 시점에 이 라벨을 가질 수 없다"}


# ──────────────────────────────────────────────────────────────────────
def make_figures(daily: pd.DataFrame, m: dict, out_dir) -> list[str]:
    """그래프 4종 (설계서 §10 ⓑ): M3 열화곡선 · M5 시계열 · M2 구간분해 · M6 비용-성능."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["axes.unicode_minus"] = False

    figs = []
    colors = {"ARM-D1": "#1f77b4", "ARM-D7": "#d62728", "ARM-STATIC": "#7f7f7f"}

    # M3 열화 곡선 — 원 정의(좌) vs 요일 보정(우). 요일 교락 때문에 좌측만 보면 오독한다.
    m3 = m["M3"]
    adj = m3.get("weekday_adjusted", {}).get("by_age", {})
    fig, axes = plt.subplots(1, 2 if adj else 1, figsize=(11 if adj else 7, 4.2), squeeze=False)
    ax = axes[0][0]
    ages = sorted(int(k) for k in m3["ARM-D7"])
    ax.plot(ages, [m3["ARM-D7"][str(a)]["rmse"] for a in ages], "o-", color=colors["ARM-D7"],
            label="ARM-D7 raw")
    if "ARM-D1" in m3 and "0" in m3["ARM-D1"]:
        ax.axhline(m3["ARM-D1"]["0"]["rmse"], ls="--", color=colors["ARM-D1"], label="ARM-D1")
    ax.set_xlabel("model age (days)"); ax.set_ylabel("RMSE")
    ax.set_title("M3 raw (weekday-confounded)"); ax.legend(); ax.grid(alpha=.3)
    if adj:
        ax2 = axes[0][1]
        a2 = sorted(int(k) for k in adj)
        ax2.plot(a2, [adj[str(a)]["degradation"] for a in a2], "o-", color="#2ca02c")
        ax2.axhline(0, color="0.6", lw=.8)
        ax2.set_xlabel("model age (days)"); ax2.set_ylabel("RMSE vs same-weekday D1")
        ax2.set_title("M3-adj degradation (weekday removed)"); ax2.grid(alpha=.3)
    p = out_dir / "fig_M3_degradation.png"; fig.tight_layout(); fig.savefig(p, dpi=130)
    plt.close(fig); figs.append(p.name)

    # M5 시계열
    fig, ax = plt.subplots(figsize=(11, 4.4))
    for arm in C.ARMS:
        s = daily[daily["arm"] == arm].sort_values("wf_day")
        ax.plot(s["wf_day"], s["rmse"].rolling(7, min_periods=3).mean(),
                color=colors[arm], lw=1.5, label=f"{arm} (7d roll)")
    led = pd.read_csv(C.PM_LEDGER)
    led["time"] = pd.to_datetime(led["time"], format="ISO8601")
    for _, r in led.iterrows():
        ax.axvline(r["time"], color="k" if r["kind"] == "major" else "0.7",
                   ls="-" if r["kind"] == "major" else ":", lw=1.2 if r["kind"] == "major" else .8)
    ax.set_ylabel("RMSE (7d rolling)"); ax.set_title("M5 daily RMSE  (solid=major PM, dotted=minor PM)")
    ax.legend(); ax.grid(alpha=.3)
    p = out_dir / "fig_M5_timeseries.png"; fig.tight_layout(); fig.savefig(p, dpi=130)
    plt.close(fig); figs.append(p.name)

    # M2 구간분해
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    segs = [s for s in ("onset_major", "onset_minor", "steady") if s in m["M2"]]
    x = np.arange(len(segs)); w = .26
    for i, arm in enumerate(C.ARMS):
        ax.bar(x + (i - 1) * w, [m["M2"][s][arm] for s in segs], w, color=colors[arm], label=arm)
    ax.set_xticks(x); ax.set_xticklabels(segs); ax.set_ylabel("RMSE")
    ax.set_title("M2 segment decomposition"); ax.legend(); ax.grid(alpha=.3, axis="y")
    p = out_dir / "fig_M2_segments.png"; fig.tight_layout(); fig.savefig(p, dpi=130)
    plt.close(fig); figs.append(p.name)

    # M6 비용-성능
    fig, ax = plt.subplots(figsize=(6.6, 4.6))
    for arm in C.ARMS:
        ax.scatter(m["M6"][arm]["total_hours"], m["M1"][arm]["pooled_rmse"],
                   s=110, color=colors[arm], label=arm, zorder=3)
        # 라벨은 전부 ASCII — matplotlib 기본 폰트에 한글 글리프가 없어 □ 로 깨진다
        ax.annotate(f"  {arm}\n  n={m['M6'][arm]['n_retrain']}",
                    (m["M6"][arm]["total_hours"], m["M1"][arm]["pooled_rmse"]), fontsize=8)
    ax.set_xlabel("retrain cost (wall-clock hours, measured)"); ax.set_ylabel("pooled RMSE")
    ax.set_title("M6 cost vs performance"); ax.grid(alpha=.3)
    p = out_dir / "fig_M6_cost.png"; fig.tight_layout(); fig.savefig(p, dpi=130)
    plt.close(fig); figs.append(p.name)
    return figs


# ──────────────────────────────────────────────────────────────────────
def main() -> int:
    """예측 기록 → M1~M6 지표·통계·판정·그림을 산출하고 metrics.json 을 남긴다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="main")
    ap.add_argument("--no-figs", action="store_true")
    args = ap.parse_args()

    run = C.OUT_DIR / f"run_{args.tag}"
    preds = pd.read_parquet(run / "predictions.parquet")
    preds["wf_day"] = pd.to_datetime(preds["wf_day"])
    preds["cutoff"] = pd.to_datetime(preds["cutoff"])
    assign = pd.read_csv(run / "arm_assignment.csv", parse_dates=list(("wf_day",) + C.ARMS))
    fits = pd.read_csv(run / "fits.csv")
    manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
    # 매니페스트는 백테스트 시점에 굳는다. 설계서가 리포에 없으므로 보고서에 인쇄되는
    # 문구만 부록 참조로 갈아끼운다 — 295회 재학습을 문구 하나 때문에 다시 돌리지 않는다.
    if "설계서" in str(manifest.get("time_axis", "")):
        manifest["time_axis"] = "C40 실측 타임스탬프 (사전 등록 균등 근사를 대체 — 부록 A-6 v1.1)"

    ap_df = arm_predictions(preds, assign)
    days = pd.DatetimeIndex(sorted(ap_df["wf_day"].unique()))
    segs = segment_labels(days)
    ap_df = ap_df.merge(segs, on="wf_day", how="left")

    # ── M1 ────────────────────────────────────────────────────────────
    M1 = {}
    for arm, g in ap_df.groupby("arm"):
        r = rmse(g["y"], g["pred"])
        M1[arm] = {"pooled_rmse": round(r, 3), "honest_r2": round(r2_honest(r), 4),
                   "n_wafers": int(len(g))}

    # ── M5 (일별) ─────────────────────────────────────────────────────
    # groupby.apply 는 pandas 버전별 동작차(include_groups)가 있어 벡터 집계로 처리한다
    se = ap_df.assign(_se=(ap_df["y"] - ap_df["pred"]) ** 2)
    daily = (se.groupby(["arm", "wf_day"], as_index=False)
             .agg(mse=("_se", "mean"), n=("_se", "size")))
    daily["rmse"] = np.sqrt(daily["mse"])
    daily = daily.drop(columns=["mse"])
    daily = daily.merge(segs, on="wf_day", how="left")
    daily.to_csv(run / "daily_rmse.csv", index=False)

    # ── M2 (구간분해) ─────────────────────────────────────────────────
    M2 = {}
    for seg, g in ap_df.groupby("segment"):
        M2[seg] = {arm: round(rmse(h["y"], h["pred"]), 3) for arm, h in g.groupby("arm")}
        M2[seg]["n_wafers"] = int(len(g) / len(C.ARMS))
    M2_warm = {arm: round(rmse(h["y"], h["pred"]), 3)
               for arm, h in ap_df[ap_df["is_warmup"]].groupby("arm")}
    M2_nowarm = {arm: round(rmse(h["y"], h["pred"]), 3)
                 for arm, h in ap_df[~ap_df["is_warmup"]].groupby("arm")}

    # ── M3 (열화 곡선) ────────────────────────────────────────────────
    M3 = {}
    for arm in ("ARM-D7", "ARM-D1"):
        g = ap_df[ap_df["arm"] == arm]
        M3[arm] = {str(int(a)): {"rmse": round(rmse(h["y"], h["pred"]), 3), "n": int(len(h))}
                   for a, h in g.groupby("model_age") if 0 <= a <= 6}
    # ⚠️ 요일 교락 진단: D7 그리드가 정확히 7일이고 평가창에 빈 생산일이 없으면
    #    model_age 슬롯 하나가 특정 요일 하나에 고정된다 → "열화"와 "요일 효과"가 분리 불가.
    #    lean-85 는 hour·dslp_x_hour·hour_x_c33 을 피처로 쓰므로 시간 효과가 실재한다.
    age_wd = (assign.assign(age=(assign["wf_day"] - assign["ARM-D7"]).dt.days - 1)
              .groupby("age")["wf_day"].apply(lambda s: sorted(set(s.dt.dayofweek))))
    M3["weekday_confound"] = {
        "age_to_weekday": {str(int(k)): v for k, v in age_wd.items()},
        "confounded": bool(all(len(v) == 1 for v in age_wd)),
        "note": ("각 model_age 가 단일 요일에 고정되면 M3 곡선은 열화와 요일 효과의 합이다 — "
                 "기울기를 열화로만 해석하지 말 것"),
    }
    # ── M3-adj: 요일 보정 열화 곡선 ────────────────────────────────────
    # D1 은 model_age 가 항상 0 이므로 D1 의 요일별 RMSE = **순수 요일 효과**다.
    # 같은 날 D7 과 D1 을 비교하면 요일이 상쇄되고 모델 나이 몫만 남는다.
    # 검증 지점: age 0 은 D7 이 D1 과 같은 모델을 쓰는 날이라 차이가 정확히 0 이어야 한다.
    nw3 = ap_df[~ap_df["is_warmup"]].copy()
    nw3["_wd"] = nw3["wf_day"].dt.dayofweek
    d1_by_wd = {int(w): rmse(g["y"], g["pred"])
                for w, g in nw3[nw3["arm"] == "ARM-D1"].groupby("_wd")}
    adj = {}
    for a, h in nw3[nw3["arm"] == "ARM-D7"].groupby("model_age"):
        if not 0 <= a <= 6:
            continue
        wds = sorted(set(h["_wd"]))
        base = float(np.mean([d1_by_wd[w] for w in wds if w in d1_by_wd])) if wds else float("nan")
        r7 = rmse(h["y"], h["pred"])
        adj[str(int(a))] = {"d7_rmse": round(r7, 3), "d1_same_weekday_rmse": round(base, 3),
                            "degradation": round(r7 - base, 3), "n": int(len(h))}
    zero_ok = abs(adj.get("0", {}).get("degradation", 1.0)) < 1e-6
    M3["weekday_adjusted"] = {
        "by_age": adj, "warmup_excluded": True,
        "age0_identity_check": zero_ok,
        "method": ("D7(age) − D1(같은 요일) — D1 은 항상 age 0 이라 요일 효과만 담는다. "
                   "age 0 의 차이가 0 이면 분해가 성립한다는 확인 지점"),
    }
    if not zero_ok:
        logger.warning("M3-adj age 0 항등 검사 실패 (%.4f) — 배정·집계 점검 필요",
                       adj.get("0", {}).get("degradation", float("nan")))

    # ── M4 (churn) ────────────────────────────────────────────────────
    probe = pd.read_parquet(run / "probe_preds.parquet")
    M4 = {}
    for arm in ("ARM-D1", "ARM-D7"):
        cuts = [str(pd.Timestamp(c).date()) for c in dict.fromkeys(assign[arm])]
        cuts = [c for c in cuts if c in probe.columns]
        maes = [float(np.mean(np.abs(probe[cuts[i]] - probe[cuts[i - 1]])))
                for i in range(1, len(cuts))]
        M4[arm] = {"n_version_pairs": len(maes),
                   "mean_probe_mae": round(float(np.mean(maes)), 3) if maes else None,
                   "median_probe_mae": round(float(np.median(maes)), 3) if maes else None,
                   "p95_probe_mae": round(float(np.quantile(maes, .95)), 3) if maes else None}
    M4["note"] = "고정 프로브 500 wafer · 인접 모델 버전 간 예측 MAE (C65 단위)"

    # ── M6 (비용) ─────────────────────────────────────────────────────
    # 군마다 실제로 학습하는 컷오프가 다르고 학습량도 다르다(초기 11,939 → 후반 80k wafer).
    # 따라서 전체 평균이 아니라 **그 군이 실제로 도는 컷오프의 fit_sec 합계**를 쓴다.
    fit_by_cut = dict(zip(pd.to_datetime(fits["cutoff"]), fits["fit_sec"]))
    mean_fit = float(fits["fit_sec"].mean())
    M6 = {}
    for arm in C.ARMS:
        cuts = list(dict.fromkeys(assign[arm])) if arm != "ARM-STATIC" else []
        secs = [fit_by_cut[c] for c in cuts if c in fit_by_cut]
        n = manifest["n_retrain_nominal"][arm]
        M6[arm] = {"n_retrain": n,
                   "mean_fit_sec": round(float(np.mean(secs)), 2) if secs else 0.0,
                   "total_hours": round(float(np.sum(secs)) / 3600, 3) if secs else 0.0,
                   "basis": "해당 군 컷오프의 실측 fit_sec 합계" if secs else "재학습 없음"}
    M6["ratio_D1_over_D7"] = (round(M6["ARM-D1"]["n_retrain"] / M6["ARM-D7"]["n_retrain"], 2)
                              if M6["ARM-D7"]["n_retrain"] else None)
    M6["ratio_hours_D1_over_D7"] = (round(M6["ARM-D1"]["total_hours"] /
                                          M6["ARM-D7"]["total_hours"], 2)
                                    if M6["ARM-D7"]["total_hours"] else None)
    M6["all_fits_mean_sec"] = round(mean_fit, 2)
    M6["note"] = "군별 실제 컷오프의 wall-clock 실측 합계 (M6 정의는 부록 A-5)"

    # ── 통계 ──────────────────────────────────────────────────────────
    piv = daily.pivot(index="wf_day", columns="arm", values="rmse").dropna()
    stats = {
        "D1_vs_D7": paired_test(piv["ARM-D1"].to_numpy(), piv["ARM-D7"].to_numpy()),
        "STATIC_vs_D7": paired_test(piv["ARM-STATIC"].to_numpy(), piv["ARM-D7"].to_numpy()),
    }
    delta = (piv["ARM-D7"] - piv["ARM-D1"]).to_numpy()      # + = D1 우위
    lo, hi = moving_block_bootstrap_ci(delta, C.BOOTSTRAP_BLOCK_DAYS, C.BOOTSTRAP_N,
                                       C.SEED, C.ALPHA)
    stats["delta_daily_rmse_D7_minus_D1"] = {
        "mean": round(float(np.mean(delta)), 4),
        "ci95": [round(lo, 4), round(hi, 4)],
        "method": f"moving-block bootstrap (block={C.BOOTSTRAP_BLOCK_DAYS}d, "
                  f"B={C.BOOTSTRAP_N})",
        "excludes_zero": bool(lo > 0 or hi < 0),
    }

    # ── §9 사전등록 민감도: 이음새 워밍업 제외 재집계 ──────────────────
    # 설계서 §9 "평가 첫 7일은 별도 표기(워밍업), 전체 집계에는 포함하되 제외 민감도 1회".
    # 워밍업 구간에서는 D7·STATIC 이 아직 M0 를 쓰므로, 이 며칠이 pooled 를 크게 흔든다.
    nw = ap_df[~ap_df["is_warmup"]]
    sens_M1 = {a: round(rmse(g["y"], g["pred"]), 3) for a, g in nw.groupby("arm")}
    sens_M2 = {}
    for seg, g in nw.groupby("segment"):
        sens_M2[seg] = {a: round(rmse(h["y"], h["pred"]), 3) for a, h in g.groupby("arm")}
        sens_M2[seg]["n_wafers"] = int(len(g) / len(C.ARMS))
    pw = daily[~daily["is_warmup"]].pivot(index="wf_day", columns="arm", values="rmse").dropna()
    sens_stats = {"D1_vs_D7": paired_test(pw["ARM-D1"].to_numpy(), pw["ARM-D7"].to_numpy()),
                  "STATIC_vs_D7": paired_test(pw["ARM-STATIC"].to_numpy(),
                                              pw["ARM-D7"].to_numpy())}
    sens_verdict = verdict({
        "M1": {a: {"pooled_rmse": sens_M1[a]} for a in C.ARMS},
        "M2": {"onset_major": sens_M2.get("onset_major", {})},
        "stats": sens_stats,
    })
    # 일별 승패 — pooled 는 대형 오차 며칠에 끌려가므로, "며칠이나 실제로 이겼나"를 함께 본다
    dlt = (pw["ARM-D7"] - pw["ARM-D1"]).to_numpy()
    daily_win = {
        "n_days": int(len(dlt)),
        "d1_better_days": int((dlt > 0).sum()),
        "d1_win_rate_pct": round(100 * float((dlt > 0).mean()), 2),
        "delta_median": round(float(np.median(dlt)), 4),
        "delta_mean": round(float(np.mean(dlt)), 4),
        "note": ("pooled 는 제곱 가중이라 소수의 대형 오차일에 끌려간다. 승률과 중앙값이 "
                 "평시 실력 차를 더 정직하게 보여준다"),
    }
    sensitivity = {
        "excl_warmup_days": C.WARMUP_DAYS,
        "n_days": int(len(pw)), "M1": sens_M1, "M2": sens_M2, "stats": sens_stats,
        "daily_win": daily_win,
        "verdict_if_excluded": sens_verdict,
        "why": ("워밍업 구간에서는 D7·STATIC 이 아직 M0 를 쓴다 — 이 며칠의 대형 오차가 "
                "pooled 에 제곱으로 실려 D1 우위를 과대 표시할 수 있다 — 사전 등록한 이음새 단차 "
                "민감도다 (부록 A-5)"),
    }

    metrics = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "tag": args.tag, "run_manifest": manifest,
        "M1": M1, "M2": M2, "M2_warmup": {"warmup_7d": M2_warm, "excl_warmup": M2_nowarm},
        "M3": M3, "M4": M4, "M6": M6, "stats": stats,
        "sensitivity_excl_warmup": sensitivity,
    }
    gate_csv = run / "gate_sim.csv"
    if gate_csv.exists():
        gate = pd.read_csv(gate_csv, parse_dates=["cutoff"])
        metrics["gate_sim_8A"] = {a: gate_simulation(gate, a) for a in ("ARM-D1", "ARM-D7")}
    else:
        raise FileNotFoundError(f"{gate_csv} 없음 — run_backtest.py 를 최신본으로 재실행하세요 "
                                "(§8-A 게이트 판정은 백테스트 루프 안에서 수행된다)")
    metrics["verdict"] = verdict(metrics)

    # ── 임계 민감도: R1 임계(✱ 미확정)를 바꾸면 판정이 어디서 갈리는가 ──
    # 임계 2.0%✱ 는 설계서 §7 제안값이고 PM·멘토 확정 대상이다. "우리가 유리한 선을
    # 골랐다"는 의심을 스스로 차단하려면, 선을 옮겼을 때 뭐가 달라지는지 먼저 밝혀야 한다.
    g_in = metrics["verdict"]["pooled_rel_gain_pct_D1_over_D7"]
    g_ex = sens_verdict["pooled_rel_gain_pct_D1_over_D7"]
    metrics["threshold_sensitivity"] = {
        "current_threshold_pct✱": C.R1_MIN_REL_GAIN_PCT,
        "gain_incl_warmup_pct": g_in,
        "gain_excl_warmup_pct": g_ex,
        "flip_threshold_incl_warmup_pct": round(g_in, 2),
        "flip_threshold_excl_warmup_pct": round(g_ex, 2),
        "grid": [{"threshold_pct": t,
                  "rule_incl_warmup": "R1" if g_in >= t else "R1 외(R2/R3)",
                  "rule_excl_warmup": "R1" if g_ex >= t else "R1 외(R2/R3)"}
                 for t in (2.0, 3.0, 4.0, 5.0, 10.0)],
        "note": ("임계가 워밍업 제외 개선폭(%.2f%%)을 넘어서면 주 판정이 R1 에서 벗어난다. "
                 "임계는 결과를 본 뒤에 조정하지 않는다 — 조정이 필요하면 이 표를 근거로 "
                 "공개 논의한다 (사전 등록 원칙 — 부록 A-4)" % g_ex),
    }

    # 지표를 **먼저** 저장한다 — 그림 렌더링(폰트·백엔드) 실패가 계산 결과를 날리지 않게
    (run / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    if not args.no_figs:
        metrics["figures"] = make_figures(daily, metrics, run)
        (run / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    logger.info("M1 pooled RMSE: %s", {k: v["pooled_rmse"] for k, v in M1.items()})
    logger.info("판정 %s — %s", metrics["verdict"]["rule"], metrics["verdict"]["call"])
    logger.info("저장: %s", run / "metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
