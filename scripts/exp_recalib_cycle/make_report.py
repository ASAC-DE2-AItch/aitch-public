# -*- coding: utf-8 -*-
"""make_report.py — 4단계: metrics.json → 결과 보고서 초안 자동 생성 (설계서 §10 ⓒ).

수치를 손으로 옮겨 적지 않는다 — 보고서의 모든 숫자는 `metrics.json` 에서 읽는다
(전사 오류 차단). 해석·판정 문장은 사전 등록 규칙(§7)이 만든 것을 그대로 싣고,
서술적 코멘트는 A 가 사후 가필한다.

산출: src/agent_a_mlops/lean85/예측모델_재보정주기_실험결과_v1.md
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import CONFIG as C

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("report")


def _tbl(rows: list[list], head: list[str]) -> str:
    """행 리스트 → 마크다운 표 문자열."""
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build(m: dict, prep: dict, leak: dict) -> str:
    """metrics/prep/leakage 리포트 → 결과 보고서 마크다운 본문."""
    mf = m["run_manifest"]
    M1, M2, M3, M4, M6 = m["M1"], m["M2"], m["M3"], m["M4"], m["M6"]
    st, vd = m["stats"], m["verdict"]
    d1, d7 = M1["ARM-D1"], M1["ARM-D7"]

    L = []
    A = L.append
    A(f"# 예측모델 재보정 주기 실험 결과 v1 — 일간(D1) vs 주간(D7)\n")
    A(f"> 실행: {mf['generated_at_utc']} UTC · tag `{mf['tag']}` · seed {mf['seed']}")
    A("> 설계 정본: `예측모델_재보정주기_실험설계_v1.md` v1.2 — **A 로컬 보관, 리포 미포함**. "
      "사전 등록 내용은 **부록 A** 에 옮겨 실었다 · 하네스: `scripts/exp_recalib_cycle/`")
    dec = getattr(C, "DECISION", None)
    # ✱ = "사전 등록 제안값, PM·멘토 미확정". 단일 소스는 DECISION["threshold_confirmed"] 이며
    # 확정 대상은 R1 임계뿐이다 — R3 임계는 R3 미발동으로 종결돼 제안값 그대로 보존하므로
    # 확정 후에도 ✱ 를 유지한다 (CONFIG.DECISION["applied"] 와 정합).
    thr_confirmed = bool(dec and dec.get("threshold_confirmed"))
    star = "" if thr_confirmed else "✱"
    if dec:
        A(f"> 상태: **확정 ({dec['date']} {dec['by']})** — {dec['verdict']} · 실행 담당: 팀원 A\n")
    else:
        A("> 상태: **결과 초안 (PM 리뷰 대기)** · 실행 담당: 팀원 A\n")
    A("---\n")

    # ── 요약 ──
    A("## 0. 한 줄 결론\n")
    if dec:
        A(f"**{dec['date']} {dec['by']} — {dec['verdict']}.** 아래는 그 판정의 실증 근거다.\n")
    A(f"**{vd['rule']} — {vd['call']}**\n")
    A(f"D1 pooled RMSE **{d1['pooled_rmse']}** vs D7 **{d7['pooled_rmse']}** "
      f"(상대 {vd['pooled_rel_gain_pct_D1_over_D7']:+.2f}%, "
      f"{'유의' if st['D1_vs_D7']['significant'] else '비유의'} p={st['D1_vs_D7']['p_value']:.4g}) · "
      f"무재학습 STATIC **{M1['ARM-STATIC']['pooled_rmse']}**\n")
    sv0 = m.get("sensitivity_excl_warmup")
    if sv0:
        g0 = sv0["verdict_if_excluded"]["pooled_rel_gain_pct_D1_over_D7"]
        A(f"⚠ **위 {vd['pooled_rel_gain_pct_D1_over_D7']:+.2f}% 를 단독으로 인용하지 말 것.** "
          f"실측→합성 이음새 {C.WARMUP_DAYS}일(전체 웨이퍼의 약 1.8%)을 빼면 개선폭은 "
          f"**{g0:+.2f}%** 로 줄어든다 — 판정 규칙은 그대로 R1 이지만 효과 크기는 약 "
          f"{vd['pooled_rel_gain_pct_D1_over_D7'] / g0:.0f}분의 1이다. 두 수치를 항상 병기한다 "
          f"(§3 사전등록 민감도 · 설계 근거는 부록 A-5).\n")
    if dec and dec.get("threshold_confirmed"):
        A(f"> ✅ 판정 임계 **{C.R1_MIN_REL_GAIN_PCT}% 확정** ({dec['date']} {dec['by']}). "
          "규칙 전문은 부록 A-4, 임계를 옮겼을 때의 영향은 §6 임계 민감도 표 참조. "
          "이후 임계 변경은 PM·멘토 재확정 사항이다.\n")
    else:
        A("> ✱ 판정 임계는 사전 등록 제안값 — PM·멘토 확정 대상이며 **아직 확정되지 않았다**. "
          "규칙 전문은 부록 A-4. 임계 확정·변경은 §6 임계 민감도 표를 근거로 공개 논의해야 하며, "
          "결과를 본 뒤 조용히 조정하는 것은 사전 등록 원칙 위반이다.\n")

    # ── 1. 실행 조건 ──
    A("## 1. 실행 조건\n")
    A(_tbl([
        ["평가 구간", f"{mf['eval_window'][0]} ~ {mf['eval_window'][1]} ({mf['n_eval_days']}일)"],
        ["평가 웨이퍼", f"{d1['n_wafers']:,} (3군 동일 셋)"],
        ["초기 모델 M0", f"학습 상한 {prep['init_train']['cutoff']} · "
                        f"{prep['init_train']['n_wafers']:,} wafer (3군 공통)"],
        ["학습창", f"min(가용 이력, {mf['train_window_days']}일) — 데이터가 1년이라 expanding ≡ 365 sliding"],
        ["명목 재학습", f"D1 {mf['n_retrain_nominal']['ARM-D1']}회 / "
                       f"D7 {mf['n_retrain_nominal']['ARM-D7']}회 / STATIC 0회"],
        ["고유 fit", f"{mf['n_distinct_fits']}회 (D7·STATIC 컷오프 ⊂ D1 컷오프 — README ③)"],
        ["피처·하이퍼파라미터", f"lean-85 동결 {mf['features_n']}개 (sha1 {mf['features_sha1']}) · "
                              f"XGBoost {mf['n_estimators']} rounds 동결"],
        ["시간축", mf["time_axis"]],
        ["승격 게이트", "미적용 (주 분석 무조건 교체 — 부록 A-2). 소급 시뮬은 §5"],
        ["환경", f"python {mf['env']['python']} · xgboost {mf['env']['xgboost']} · "
                f"pandas {mf['env']['pandas']}"],
    ], ["항목", "값"]))
    A("")
    A(f"누수 체크리스트(§5·헌법 1-3): **{'전 항목 PASS' if leak['all_ok'] else '실패'}** "
      f"— `out/leakage_report.json`\n")

    # ── 2. 주지표 ──
    A("## 2. M1 — pooled RMSE (주지표)\n")
    A(_tbl([[a, M1[a]["pooled_rmse"], M1[a]["honest_r2"], f"{M1[a]['n_wafers']:,}"]
            for a in C.ARMS], ["군", "pooled RMSE", "honest R²", "n wafer"]))
    A("")
    dd = st["delta_daily_rmse_D7_minus_D1"]
    A(_tbl([
        ["paired Wilcoxon (D1 vs D7)",
         f"p={st['D1_vs_D7']['p_value']:.4g} · n={st['D1_vs_D7']['n_pairs']}쌍 · "
         f"{'유의' if st['D1_vs_D7']['significant'] else '비유의'} (α={C.ALPHA})"],
        ["ΔRMSE(D7−D1) 평균", f"{dd['mean']:+.4f}"],
        ["ΔRMSE 95% CI", f"[{dd['ci95'][0]:+.4f}, {dd['ci95'][1]:+.4f}] "
                        f"({'0 미포함' if dd['excludes_zero'] else '0 포함'})"],
        ["방법", dd["method"]],
        ["paired Wilcoxon (STATIC vs D7)",
         f"p={st['STATIC_vs_D7']['p_value']:.4g} · "
         f"{'유의' if st['STATIC_vs_D7']['significant'] else '비유의'} "
         f"→ R4 안전장치 {'미발동' if st['STATIC_vs_D7']['significant'] else '**발동**'}"],
    ], ["통계", "결과"]))
    A("")

    # ── 3. 구간분해 / 열화 / churn / 비용 ──
    A("## 3. M2 — 구간분해 RMSE (Q2: 개선이 어디서 오는가)\n")
    segs = [s for s in ("onset_major", "onset_minor", "steady") if s in M2]
    A(_tbl([[s, M2[s].get("n_wafers", "-")] + [M2[s].get(a, "-") for a in C.ARMS] +
            [f"{(M2[s]['ARM-D7'] - M2[s]['ARM-D1']) / M2[s]['ARM-D7'] * 100:+.2f}%"]
            for s in segs],
           ["구간", "n wafer"] + list(C.ARMS) + ["D1 우위"]))
    A("")
    A(f"- 구간 정의: onset_major = major PM 후 {C.ONSET_MAJOR_DAYS}일 / "
      f"onset_minor = minor PM 후 {C.ONSET_MINOR_DAYS}일 / 나머지 = 평시")
    A("- ⚠ **위 표는 워밍업 포함본이라 onset_minor·steady 가 크게 부풀려져 있다** — 워밍업 "
      "7일이 그 두 구간에 배정되고 그 며칠 동안 D7 은 아직 M0 를 쓰기 때문이다. "
      "구간별 실제 이득은 바로 아래 민감도 표를 볼 것.")
    A("- ⚠ major PM 은 합성 구간에 2회뿐이다 — 온셋 판정은 **효과 크기 중심**으로 읽고 "
      "유의성은 주장하지 않는다 (부록 A-5·§7 한계).")
    warm = m["M2_warmup"]
    fmt = lambda d: " / ".join(f"{a} {d.get(a, '-')}" for a in C.ARMS)  # noqa: E731
    A(f"- 이음새 워밍업(평가 첫 {C.WARMUP_DAYS}일) 구간만: {fmt(warm['warmup_7d'])}")
    A(f"- 워밍업 제외 전체: {fmt(warm['excl_warmup'])}\n")

    sv = m.get("sensitivity_excl_warmup")
    if sv:
        A(f"### 사전등록 민감도 — 이음새 워밍업 {sv['excl_warmup_days']}일 제외 (부록 A-5 사전 등록)\n")
        A(_tbl([["pooled RMSE"] + [sv["M1"][a] for a in C.ARMS]] +
               [[s] + [sv["M2"][s].get(a, "-") for a in C.ARMS]
                for s in ("onset_major", "onset_minor", "steady") if s in sv["M2"]],
               ["집계"] + list(C.ARMS)))
        sg = sv["verdict_if_excluded"]
        A(f"\n- 워밍업 제외 시 D1 상대 개선 **{sg['pooled_rel_gain_pct_D1_over_D7']:+.2f}%** "
          f"(포함 시 {vd['pooled_rel_gain_pct_D1_over_D7']:+.2f}%) · "
          f"적용 규칙 {sg['rule']} · p={sv['stats']['D1_vs_D7']['p_value']:.3g}")
        A(f"- {sv['why']}")
        dw = sv.get("daily_win")
        if dw:
            A(f"- 일별 승패(워밍업 제외 {dw['n_days']}일): D1 이 나은 날 "
              f"**{dw['d1_better_days']}/{dw['n_days']} ({dw['d1_win_rate_pct']}%)** · "
              f"ΔRMSE 중앙값 **{dw['delta_median']:+.2f}** / 평균 {dw['delta_mean']:+.2f}. "
              f"{dw['note']}")
        A("- 주 판정은 사전 등록대로 워밍업 포함본이다. 이 표는 그 판정이 이음새에 "
          "얼마나 의존하는지 보여주는 것이며, 해석에 반드시 병기한다.\n")

    A("## 4. M3 — 열화 곡선 (Q3: 열화는 며칠부터인가)\n")
    ages = sorted(int(k) for k in M3.get("ARM-D7", {}))
    A(_tbl([[a, M3["ARM-D7"][str(a)]["rmse"], f"{M3['ARM-D7'][str(a)]['n']:,}"] for a in ages],
           ["모델 나이(일)", "RMSE", "n wafer"]))
    if ages:
        base = M3["ARM-D7"][str(ages[0])]["rmse"]
        worst = max(M3["ARM-D7"][str(a)]["rmse"] for a in ages)
        A(f"\n- age 0 대비 최대 열화: **{(worst - base) / base * 100:+.2f}%** "
          f"(age 0 = {base} → 최대 {worst})")
    A("- ARM-D7 내부에서 직접 측정 — 매일 교체하는 D1 은 age 0 만 존재한다.")
    wc = M3.get("weekday_confound")
    if wc and wc.get("confounded"):
        wd = ["월", "화", "수", "목", "금", "토", "일"]
        pairs = ", ".join(f"age {k}={wd[v[0]]}" for k, v in sorted(wc["age_to_weekday"].items(),
                                                                  key=lambda x: int(x[0])))
        A(f"- ⚠ **요일 완전 교락**: {pairs}. D7 그리드가 정확히 7일이고 평가창에 빈 생산일이 "
          "없어, model age 슬롯 하나가 요일 하나에 고정됐다. lean-85 는 `hour`·`dslp_x_hour`·"
          "`hour_x_c33` 를 피처로 쓰므로 시간 효과가 실재한다 — **이 곡선의 기울기를 열화로만 "
          "읽으면 안 된다**. 설계서에 없던 제약으로, 실행 중 발견해 기록한다.\n")
    else:
        A("")

    adj = M3.get("weekday_adjusted")
    if adj and adj.get("by_age"):
        A("### M3-adj — 요일 보정 열화 곡선 (Q3의 실제 답)\n")
        A("ARM-D1 은 model age 가 항상 0 이므로 D1 의 요일별 RMSE = 순수 요일 효과다. "
          "같은 날 D7 과 D1 을 비교하면 요일이 상쇄되고 모델 나이 몫만 남는다.\n")
        ages2 = sorted(int(k) for k in adj["by_age"])
        A(_tbl([[a, adj["by_age"][str(a)]["d7_rmse"], adj["by_age"][str(a)]["d1_same_weekday_rmse"],
                 f"{adj['by_age'][str(a)]['degradation']:+.2f}", f"{adj['by_age'][str(a)]['n']:,}"]
                for a in ages2],
               ["모델 나이(일)", "D7", "D1 (같은 요일)", "열화", "n wafer"]))
        last = adj["by_age"][str(ages2[-1])]
        base = last["d1_same_weekday_rmse"]
        A(f"\n- age {ages2[-1]} 누적 열화 **{last['degradation']:+.2f} RMSE** "
          f"(같은 요일 기준선 {base} 대비 {last['degradation'] / base * 100:+.1f}%)")
        A(f"- 항등 검사: age 0 은 D7 이 D1 과 같은 모델을 쓰는 날이라 열화가 0 이어야 한다 → "
          f"**{'통과' if adj['age0_identity_check'] else '실패 — 집계 점검 필요'}**")
        A("- 워밍업 제외 기준. 사전 등록 M3 원 정의(D7 내부 측정)는 위 표에 그대로 두고, "
          "해석은 이 보정판을 쓴다 (설계 정의는 부록 A-5).\n")

    A("## 5. M4·M6 — 부작용과 비용 (Q4)\n")
    A(_tbl([[a, M4[a]["n_version_pairs"], M4[a]["mean_probe_mae"], M4[a]["median_probe_mae"],
             M4[a]["p95_probe_mae"]] for a in ("ARM-D1", "ARM-D7")],
           ["군", "버전 쌍", "평균 churn(MAE)", "중앙값", "P95"]))
    A(f"\n{M4['note']}. C65 σ={C.SIGMA_C65} 대비 상대 크기로 읽을 것.\n")
    A(_tbl([[a, M6[a]["n_retrain"], M6[a]["mean_fit_sec"], M6[a]["total_hours"]]
            for a in C.ARMS], ["군", "재학습 횟수", "1회 wall-clock(초)", "총 시간(h)"]))
    ratio, rh = M6.get("ratio_D1_over_D7"), M6.get("ratio_hours_D1_over_D7")
    A(f"\n- D1/D7 재학습 **횟수비 {'-' if ratio is None else f'{ratio}×'}** "
      f"(설계서 예상 ≈6.9×) · 실측 **시간비 {'-' if rh is None else f'{rh}×'}**")
    A(f"- 산정 기준: {M6['note']} (전체 fit 평균 {M6.get('all_fits_mean_sec')}초)\n")

    g = m["gate_sim_8A"]
    A("### 부속 §8-A — 승격 게이트 3.0% 소급 시뮬레이션\n")
    A(_tbl([[k, v["n_triggers"], v["n_judged"], v["n_promoted"],
             "-" if v["promote_rate_pct"] is None else f"{v['promote_rate_pct']}%",
             "-" if v["gain_pct_median"] is None else f"{v['gain_pct_median']:+.2f}%"]
            for k, v in g.items()],
           ["군", "트리거", "판정", "승격", "승격률", "개선 중앙값"]))
    A(f"\n평가 셋: {list(g.values())[0]['eval_set']} · 임계 {C.PROMOTE_MIN_RMSE_GAIN_PCT}%")
    A(f"> {list(g.values())[0]['caveat']} — 본 판정과 분리해 읽는다.\n")

    # ── 판정 ──
    A("## 6. 판정 (사전 등록 규칙 — 전문은 부록 A-4)\n")
    A(_tbl([
        ["적용 규칙", f"**{vd['rule']}**"],
        ["pooled 상대 개선 (D1/D7)", f"{vd['pooled_rel_gain_pct_D1_over_D7']:+.2f}% "
                                   f"(R1 임계 {vd['thresholds']['R1_min_rel_gain_pct✱']}%{star})"],
        ["온셋 구간 상대 개선",
         ("-" if vd["onset_major_rel_gain_pct"] is None
          else f"{vd['onset_major_rel_gain_pct']:+.2f}%") +
         f" (R3 임계 {vd['thresholds']['R3_onset_min_rel_gain_pct✱']}%✱)"],
        ["D1 vs D7 유의성", "유의" if vd["significant_D1_vs_D7"] else "비유의"],
        ["STATIC vs D7 유의성", "유의 (R4 미발동)" if vd["significant_STATIC_vs_D7"]
                              else "**비유의 → R4 발동**"],
    ], ["항목", "값"]))
    A(f"\n**조치**: {vd['call']}\n")

    ts = m.get("threshold_sensitivity")
    if ts:
        A("### 임계 민감도 — 판정선을 옮기면 어떻게 되는가\n")
        if thr_confirmed:
            A(f"R1 임계 {ts['current_threshold_pct✱']}% 는 {dec['date']} {dec['by']}에서 "
              "**확정**됐다(부록 A-4). 아래 표는 확정 **전에** 사전 등록해 둔 것으로, 선을 "
              "옮겼다면 판정이 어디서 갈렸는지를 그대로 남긴다 — 이후 임계 변경은 PM·멘토 "
              "재확정 사항이다.\n")
        else:
            A(f"R1 임계 {ts['current_threshold_pct✱']}%✱ 는 사전 등록 **제안값**(부록 A-4)이며 PM·멘토 "
              "확정 대상이다. 확정 전이므로, 선을 옮겼을 때 판정이 어디서 갈리는지 먼저 밝힌다.\n")
        A(_tbl([[f"{r['threshold_pct']:.0f}%", r["rule_incl_warmup"], r["rule_excl_warmup"]]
                for r in ts["grid"]],
               ["가정 임계", "판정 (워밍업 포함)", "판정 (워밍업 제외)"]))
        A(f"\n- 판정이 뒤집히는 지점: 워밍업 포함 **{ts['flip_threshold_incl_warmup_pct']}%** 초과 · "
          f"워밍업 제외 **{ts['flip_threshold_excl_warmup_pct']}%** 초과")
        A(f"- {ts['note']}\n")

    ss = m.get("sensitivity_runs")
    if ss:
        A("### 부속 런 — 시드·경계완충 민감도 (부록 A-5 사전 등록)\n")
        A(_tbl([[r["tag"], r["seed"], r["buffer_days"], r["rule"],
                 f"{r['gain_pct']:.2f}%", f"{r['p']:.2e}",
                 r["rule_excl_warmup"] or "-",
                 "-" if r["gain_excl_warmup"] is None else f"{r['gain_excl_warmup']:.2f}%",
                 "-" if r["m3adj_age6"] is None else f"{r['m3adj_age6']:+.2f}"]
                for r in ss["runs"]],
               ["런", "seed", "완충", "규칙", "개선%", "p", "규칙(워밍업제외)",
                "개선%", "M3adj age6"]))
        rng_ = ss.get("gain_excl_warmup_range")
        A(f"\n- **{ss['call']}**")
        if rng_:
            A(f"- 워밍업 제외 개선폭 {rng_[0]:.2f}% ~ {rng_[1]:.2f}% "
              f"(R1 임계 {C.R1_MIN_REL_GAIN_PCT}%{star} 대비 여유 "
              f"{rng_[0] - C.R1_MIN_REL_GAIN_PCT:.2f}%p)")
        A(f"- 동결 seed {C.SEED}(tag `main`)가 정본. 나머지는 부속 런이다.")
        A("- ⚠ `buf1` 의 M3adj 는 main 과 **같은 축이 아니다**. `model_age` 는 "
          "(예측일 − 컷오프) 로 계산하는데 완충은 컷오프가 아니라 학습 상한만 당기므로, "
          "buf1 의 age *k* 는 실제로는 *k+1* 일 묵은 모델이다. 두 런의 열화량을 직접 "
          "비교하지 말 것.")
        A("- ⚠ `buf1` 에서 ARM-D7 은 컷오프가 항상 금요일이라 매 재학습마다 직전 금요일이 "
          "빠진다(43회). 다만 **그 한 번의 금요일만** 빠지고 과거 금요일들은 학습셋에 "
          "남으므로, 요일을 계통적으로 배제한 것은 아니다.\n")

    A("### 기전 요약 (수치에서 기계적으로 도출)\n")
    seg_rank = sorted(((s, (M2[s]["ARM-D7"] - M2[s]["ARM-D1"]) / M2[s]["ARM-D7"] * 100)
                       for s in segs), key=lambda x: -x[1])
    if sv:
        sseg = sv["M2"]
        seg_rank = sorted(((s, (sseg[s]["ARM-D7"] - sseg[s]["ARM-D1"]) / sseg[s]["ARM-D7"] * 100)
                           for s in sseg), key=lambda x: -x[1])
    A("- 재보정 이득의 구간별 순위(워밍업 제외): " +
      " > ".join(f"{s} {g:+.2f}%" for s, g in seg_rank))
    st_m2 = (sv["M2"] if sv else M2)
    if all(s in st_m2 for s in ("onset_major", "steady")):
        A(f"- ARM-STATIC 은 평시 {st_m2['steady']['ARM-STATIC']} 인데 "
          f"major PM 온셋에서 {st_m2['onset_major']['ARM-STATIC']} 로 **좋아진다** — "
          "M0 가 실측 구간(마지막 major PM 이후 = 고레짐)만 보고 학습해 그 레짐에서만 맞는다. "
          "즉 STATIC 의 발산은 점진적 열화가 아니라 **레짐 고착**이다 — 사전 등록 R4 의 전제 이탈이다 "
          "(§7 한계·부록 A-6 v1.2).")
    A("- 위 둘을 합치면 이 데이터에서 재보정이 사는 지점은 '시간 경과 열화'가 아니라 "
      "**'레짐 전환 적응'** 이다. 사전 등록 R3(평시 7일 + 요란 PM 직후 즉시 — 부록 A-4)이 그린 구조와 "
      "같은 방향이며, R1 이 먼저 발동한 것은 워밍업 포함 pooled 가 임계를 크게 넘겼기 때문이다.\n")
    if not dec:
        A("> 위 문단까지가 수치에서 기계적으로 나오는 내용이다. **정책 권고는 팀원 A 가 아래에 "
          "직접 가필한다** — 자동 생성 문장을 권고로 오독하지 말 것.\n")
    else:
        A("### 결정 및 후속 (보고 완료)\n")
        A(_tbl([["확정일", f"{dec['date']} · {dec['by']}"],
                ["결정", f"**{dec['verdict']}**"],
                ["임계", (f"R1 {C.R1_MIN_REL_GAIN_PCT}% 확정 (✱ 해제)"
                        if dec.get("threshold_confirmed") else "미확정 유지")],
                ["종결된 후속", " · ".join(dec.get("followups_closed", [])) or "-"],
                ["반영 위치", " · ".join(dec.get("applied", [])) or "-"]],
               ["항목", "내용"]))
        A("")
        A("기전 분석상 이득은 '레짐 전환 적응'에서 나오지만, 재학습 비용이 1회 3.4초로 "
          "실질 부담이 없어 **일간 유지가 하이브리드보다 단순하고 안전하다**는 판단이다. "
          "하이브리드(R3)를 뒷받침할 온셋 수치가 major PM 2회 표본이라 부속 런에서 "
          "3.05~5.17% 로 흔들린 점도 종결 근거다 (§7 한계).\n")
        A("**다시 열어야 할 조건**: ⓐ PM 패턴이 크게 바뀌어 온셋 구간 비중이 달라질 때 "
          "ⓑ y 커플링을 주입한 데이터로 재검증할 때(§7 첫 문단) ⓒ 재학습 비용이 현재의 "
          "수십 배로 커질 때. 이 중 하나라도 성립하면 본 판정의 전제가 흔들린다.\n")

    # ── 한계 ──
    A("## 7. 한계 (해석 시 필수)\n")
    A("> **y 커플링 부재**: 이벤트 16건·aging 은 **센서 층만** 주입(y 무반응 — 생성설계 명시 가정). "
      "측정되는 재학습 이득은 주로 \"X 분포 이동 적응 + PM 주변 y 패턴\"이며, 실제 팹의 "
      "y 드리프트 적응분은 **과소 반영**된다.\n")
    A("따라서 본 결과는 **합성 가정 하 하한 추정**이다. 실제 팹에서 재보정 이득은 여기서 측정된 "
      "값보다 클 가능성이 높고, 그 방향은 D1 에 유리하다 — 이번처럼 R1 이 나왔더라도 그 "
      "효과 크기는 과소 추정일 수 있고, 반대로 R2(완화)가 나왔다면 \"주기를 늘려도 안전하다\"의 "
      "근거로 쓰기엔 이 실험 하나로는 부족했을 것이다.\n")
    rows = [
        ["이음새 단차", f"평가 첫 {C.WARMUP_DAYS}일 별도 표기 + 제외 민감도 실행 — §3 참조. "
                     "**주 판정 효과 크기의 대부분이 여기서 나온다**"],
        ["매핑 근사", "**해소** — C40 실측 시각 사용 (부록 A-6 v1.1 정정, 하네스 README ①)"],
        ["판정 라벨 즉시 인지", "요란/조용 판정 지연을 무시하는 단순화 가정"],
        ["합성 major 2회", "온셋 표본 = 레짐 2개 — 통계력 낮음, 효과 크기 중심 서술. "
                        "부속 런에서 온셋 개선폭이 3.05~5.17% 로 흔들려 R3 임계(5%✱)를 "
                        "걸친다 — **하이브리드 안의 근거로는 쓸 수 없다**"],
    ]
    if wc and wc.get("confounded"):
        rows.append(["M3 요일 완전 교락 *(실행 중 발견)*",
                     "model age 슬롯이 요일에 1:1 고정 — 원 곡선의 기울기는 열화 + 요일 효과의 "
                     "합이다. **M3-adj 병기**로 대응했고 age 0 항등 검사로 분해를 검증했다 "
                     "(부록 A-6 v1.2)"])
    st_m2b = (sv["M2"] if sv else M2)
    if all(s in st_m2b for s in ("onset_major", "steady")):
        rows.append(["R4 전제 이탈 *(실행 중 발견)*",
                     f"STATIC 이 발산(={M1['ARM-STATIC']['pooled_rmse']})해 R4 는 미발동했으나, "
                     "그 정체는 점진적 열화가 아니라 **레짐 고착**이다 — 재보정 이득을 "
                     "'시간 경과 열화 적응'이 아니라 '레짐 전환 적응'으로 서술한다 (부록 A-6 v1.2)"])
    rows.append(["게이트 시뮬 사후성",
                 "챔피언·챌린저 모두 out-of-sample 인 동일 셋에서 비교하므로 낙관 편향은 없다. "
                 "다만 **결정 시점에는 가질 수 없는 라벨을 쓰는 사후 소급 판정**이라 운영 "
                 "게이트의 실제 거동과는 다르다 (§5 부속)"])
    A(_tbl(rows, ["위협", "본 실험에서의 처리"]))
    A("")

    # ── 부록 A: 설계서 대체 요약 ──────────────────────────────────────
    # 설계서는 A 작업물이라 리포에 올리지 않는다(CLAUDE.local.md). 그래도 리뷰어가
    # "판정 규칙이 결과를 보고 만들어진 것 아닌가"를 확인할 수 있어야 하므로,
    # 사전 등록 내용을 여기에 옮겨 싣는다. 임계·구간 정의는 CONFIG 에서 읽어
    # 설계서 원문과 어긋나지 않게 한다.
    A("## 8. 부록 A — 사전 등록 설계 요약 (설계서 대체)\n")
    A("설계서(`예측모델_재보정주기_실험설계_v1.md` v1.2)는 A 작업물이라 리포에 포함하지 "
      "않는다(CLAUDE.local.md). 리뷰에 필요한 사전 등록 내용을 아래에 옮겨 싣는다 — "
      "**판정 규칙과 임계는 결과를 보기 전에 확정한 것**이며, 코드에서는 `CONFIG.py` "
      "한 곳에만 존재해 변경 이력이 git 에 남는다.\n")

    A("### A-1. 실험 질문 (사전 등록)\n")
    A(_tbl([
        ["Q1", "평가구간 전체 pooled RMSE 에서 D1 이 D7 대비 얼마나 개선되는가 (유의성 포함)"],
        ["Q2", "개선이 있다면 **어디서** 오는가 — 평시인가, PM 직후(레짐 온셋) 구간인가"],
        ["Q3", "모델 열화 곡선상 열화가 시작되는 시점은 며칠인가"],
        ["Q4", "D1 의 부작용(예측 churn — 잦은 교체로 인한 일관성 저하)은 어느 정도인가"],
    ], ["번호", "질문"]))
    A("")

    A("### A-2. 실험군과 공통 통제\n")
    A(_tbl([
        ["ARM-D1", "매일 00:00, 직전일까지 데이터로 재학습", "현행 정본 (일간)"],
        ["ARM-D7", "7일마다 재학습 (평가 시작일 기준 7일 그리드)", "비교군 (B2′ 프로토콜)"],
        ["ARM-STATIC", "초기 모델 고정, 재학습 없음", "대조군 — 재보정 효과 자체의 분리"],
    ], ["군", "재보정 규칙", "역할"]))
    A("")
    A("**공통 통제 (3군 동일)**: 초기 모델 M0 1개 공유 · 피처 lean-85 동결셋 · "
      f"하이퍼파라미터 동결({mf['n_estimators']} rounds) · seed {mf['seed']} · "
      f"학습창 min(가용 이력, {mf['train_window_days']}일) · wafer→날짜 매핑 공유. "
      "**주기 하나만 조작 변인이다.**\n")
    A("**게이트 제외**: 운영 승격 기준(3.0%)은 주 분석에서 제외(무조건 교체)했다 — "
      "게이트를 켜면 '주기 효과'와 '게이트 효과'가 섞여 D1·D7 격차가 통과율에 희석된다. "
      "게이트 시나리오는 §5 부속 분석으로 분리했다.\n")

    A("### A-3. 백테스트 프로토콜\n")
    A("```")
    A(f"초기 학습:  train(wf_day <= {prep['init_train']['cutoff']})        → M0  (3군 공통)")
    A(f"평가 루프:  for d in {mf['eval_window'][0]} .. {mf['eval_window'][1]}:")
    A("    ARM-D1:     M_d = retrain(wf_day <= d-1)        # 매일")
    A("    ARM-D7:     if (d - 시작일) % 7 == 0: M = retrain(wf_day <= d-1)")
    A("    ARM-STATIC: M = M0")
    A("    예측:       d일 wafer 전체 → (군, d, wafer, y, ŷ) 기록")
    A("```")
    A("d일 예측에 쓰는 모델은 d-1일까지의 데이터만 학습한다(미래 차단). `pm_log` 도 "
      "재학습 시점 기준 절단본과 등가임을 실증한다(§1 누수 체크리스트 check 4).\n")

    A("### A-4. 판정 규칙 R1~R4 (사전 등록 — 결과 확인 전 확정)\n")
    A(_tbl([
        ["**R4**", "ARM-STATIC 이 D7 과 유의차 없음",
         "**주기 판정 보류** — 합성 데이터가 재학습 필요성을 재현하지 못했다는 신호로 읽고 "
         "데이터 타당성 재검토로 회귀"],
        ["**R1**", f"D1 이 D7 대비 pooled RMSE 상대 **≥{C.R1_MIN_REL_GAIN_PCT}%{star}** 개선 "
                  f"+ 유의(α={C.ALPHA})", "**일간 유지 확정** — 실증 종결, 멘토 보고"],
        ["**R3**", f"R2 이면서 온셋 구간에서만 D1 우위 **≥{C.R3_ONSET_MIN_REL_GAIN_PCT}%✱**",
         "**하이브리드 제안** — 평시 7일 + 요란 PM 직후 즉시 재학습"],
        ["**R2**", f"개선 <{C.R1_MIN_REL_GAIN_PCT}%{star} 또는 비유의",
         "**7일 완화안 작성** — 멘토 확정 사항 이탈이므로 근거 패키지 + 사후 공유 절차"],
    ], ["규칙", "조건", "판정"]))
    A("")
    A("**검사 순서는 R4 → R1 → R3 → R2 다.** R4 를 맨 앞에 두는 것은 안전장치라서다 — "
      "데이터가 재학습 필요성을 재현하지 못했다면 나머지 판정이 전부 무의미해진다. "
      "실측 68일에서 무재학습은 pooled 254.9 로 발산함이 확인돼 있으므로(lean-85 REPORT), "
      "STATIC 이 멀쩡하다면 그것은 '무재학습이 좋다'가 아니라 '합성 구간이 드리프트를 "
      "재현하지 못했다'는 신호로 읽는다.\n")
    if thr_confirmed:
        A(f"**R1 임계는 {dec['date']} {dec['by']}에서 {C.R1_MIN_REL_GAIN_PCT}% 로 확정됐다(✱ 해제). "
          f"✱ 가 남은 R3 임계는 R3 미발동으로 종결돼 사전 등록 제안값 그대로 보존한다.** "
          f"코드상 위치는 `CONFIG.R1_MIN_REL_GAIN_PCT` / `CONFIG.R3_ONSET_MIN_REL_GAIN_PCT` "
          f"한 곳뿐이라, 값이 바뀌면 git 이력에 남는다. 임계 변경 시 판정이 어떻게 달라지는지는 "
          f"§6 임계 민감도 표 참조.\n")
    else:
        A(f"**✱ 표시 임계는 제안값이며 PM·멘토 확정 대상이다.** 코드상 위치는 "
          f"`CONFIG.R1_MIN_REL_GAIN_PCT` / `CONFIG.R3_ONSET_MIN_REL_GAIN_PCT` 한 곳뿐이라, "
          f"값이 바뀌면 git 이력에 남는다. 임계 변경 시 판정이 어떻게 달라지는지는 "
          f"§6 임계 민감도 표 참조.\n")

    A("### A-5. 구간·지표 정의\n")
    A(_tbl([
        ["onset_major", f"major PM 후 {C.ONSET_MAJOR_DAYS}일"],
        ["onset_minor", f"minor PM 후 {C.ONSET_MINOR_DAYS}일"],
        ["steady", "위 둘에 속하지 않는 평시"],
        ["warmup", f"평가 첫 {C.WARMUP_DAYS}일 — 이음새 단차 대비 별도 표기 + 제외 민감도"],
        ["통계", f"일별 RMSE 쌍 paired Wilcoxon(α={C.ALPHA}) + ΔRMSE moving-block "
                f"bootstrap 95% CI (블록 {C.BOOTSTRAP_BLOCK_DAYS}일, B={C.BOOTSTRAP_N:,})"],
        ["seed 민감도", f"부속 런 seed {', '.join(str(s) for s in C.SEED_SENSITIVITY)} — "
                      f"동결 seed {C.SEED} 가 정본"],
    ], ["항목", "정의"]))
    A("")

    A("### A-6. 설계서 개정 이력\n")
    A(_tbl([
        ["v1 (2026-08-02)", "최초 작성. 데이터 수치는 synthetic_1y 실측 집계 기준"],
        ["v1.1 (2026-08-03)", "구현 착수 정정 — ⓐ §2 시간축 전제 오인 정정(타임스탬프 부재 → "
                            "**C40 실측 시각**), §9 '매핑 근사' 위협 해소 ⓑ §2 확인 항목 3건 "
                            "실측 종결 ⓒ §4 구현 노트(명목 338회 = 고유 fit 295회 등가)"],
        ["v1.2 (2026-08-03)", "실행 후 한계 2건 추가 — ⓐ **M3 요일 완전 교락**(M3-adj 병기로 대응) "
                            "ⓑ **R4 전제 이탈**(STATIC 발산의 정체가 레짐 고착)"],
    ], ["판", "내용"]))
    A("")
    A("> 설계서 원본은 팀원 A 로컬에 있다. 리뷰 중 원문 확인이 필요하면 요청해 주기 바란다.\n")

    A("## 9. 산출물\n")
    A(f"- 런 매니페스트·예측 기록: `scripts/exp_recalib_cycle/out/run_{mf['tag']}/`")
    A(f"- 그래프: {', '.join('`' + f + '`' for f in m.get('figures', []))}")
    A("- 검증 리포트: `out/prep_report.json` · `out/leakage_report.json`")
    A(f"- wafer_day_map sha1: `{mf.get('wafer_day_map_sha1')}`\n")
    A("---")
    A(f"*자동 생성: `scripts/exp_recalib_cycle/make_report.py` — 모든 수치는 "
      f"`out/run_{mf['tag']}/metrics.json` 에서 읽는다(전사 오류 차단). "
      f"서술 코멘트는 A 가 사후 가필.*")
    return "\n".join(L) + "\n"


def main() -> int:
    """metrics.json 을 읽어 lean85 패키지에 결과 보고서 초안을 쓴다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="main")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    run = C.OUT_DIR / f"run_{args.tag}"
    m = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    prep = json.loads((C.OUT_DIR / "prep_report.json").read_text(encoding="utf-8"))
    leak = json.loads((C.OUT_DIR / "leakage_report.json").read_text(encoding="utf-8"))
    # 부속 런 비교표 (compare_runs.py 산출) — 있으면 보고서에 싣는다
    ss = C.OUT_DIR / "sensitivity_summary.json"
    if ss.exists() and args.tag == "main":
        m["sensitivity_runs"] = json.loads(ss.read_text(encoding="utf-8"))

    if args.out:
        out = Path(args.out)
    else:
        # 스모크 런이 정본 보고서를 덮어쓰지 않게 tag 를 파일명에 반영한다
        name = ("예측모델_재보정주기_실험결과_v1.md" if args.tag == "main"
                else f"예측모델_재보정주기_실험결과_v1_{args.tag}.md")
        # 출력 위치 = lean85 패키지 안. `docs/` 는 PM 소유(헌법 3-1)이고 A 의 실험 산출물은
        # 소유 디렉토리에 두는 것이 3-1 정합이다 (2026-08-03 이동).
        out = C.LEAN85_DIR / name
    text = build(m, prep, leak)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    logger.info("보고서 생성: %s (%d자)", out, len(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
