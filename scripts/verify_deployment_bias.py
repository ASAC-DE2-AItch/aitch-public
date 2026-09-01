# -*- coding: utf-8 -*-
"""배포 조건(시간 분할) Model R2R 검증 — v2.

조건: PM 전 세그먼트(3,692)만 학습한 모델이 PM 후 세그먼트(8,247)를 예측
      (= 새 사이클을 처음 만나는 배포 상황 재현. 데이터: 팀원 B 제공)

검증 항목
  1) 배포 성능: RMSE·R²·평균 잔차 — 보정할 체계적 오차가 실제로 존재하는가
  2) 멘토안: 이전 사이클(OOF seg1) 위상별 잔차 곡선으로 보정 시 효과
  3) 대안: 당 사이클 rolling bias — 라벨 지연(D) 별 효과 (D=0/1000/3000/10200)
  4) 팀원 B님 실력치 참고: 위상별 실측 C65 곡선 — PM 후 안정화 시점 확인

사용법:
    python scripts/verify_deployment_bias.py [deploy.csv] [oof.csv]
    (기본값: prepm_to_postpm_predictions.csv, oof_predictions.csv — 프로젝트 루트 기준)
"""

import os
import sys

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---- 상수 (헌법 6-1) ---------------------------------------------------------
N_PHASE_BINS = 40
MIN_WAFERS_PER_BIN = 20
ROLLING_WINDOW = 500                    # config A4와 동일
LABEL_DELAYS = [0, 1000, 3000, 10200]   # 0=즉시(이론 상한), 10200=E6 풀스케일
BIAS_CLAMP_NOTE = "config C7(안): |bias| ≤ 1.0×train RMSE — 초과분은 Incident 신호"
OUT_PNG = "analysis_deployment_bias.png"
OUT_MD = "analysis_deployment_bias.md"


def load(path):
    """예측 CSV 로드 + 순서 정렬 + 잔차 계산."""
    df = pd.read_csv(path).dropna(subset=["C33", "predicted_c65", "actual_c65"]).copy()
    df["seq"] = df["wafer_id"].astype(str).str.extract(r"(\d+)$").astype(float)
    df = df.sort_values(["C33", "seq"]).reset_index(drop=True)
    df["residual"] = df["actual_c65"] - df["predicted_c65"]
    return df


def rmse(x):
    """RMSE."""
    return float(np.sqrt(np.mean(np.square(x))))


def binned(df, col, edges):
    """C33 bin별 col 평균."""
    idx = np.digitize(df["C33"], edges) - 1
    out = []
    for b in range(len(edges) - 1):
        v = df[col][idx == b]
        out.append((0.5 * (edges[b] + edges[b + 1]),
                    v.mean() if len(v) >= MIN_WAFERS_PER_BIN else np.nan,
                    len(v)))
    return pd.DataFrame(out, columns=["phase", col, "n"])


def main():
    """분석 실행."""
    deploy_path = sys.argv[1] if len(sys.argv) > 1 else "prepm_to_postpm_predictions.csv"
    oof_path = sys.argv[2] if len(sys.argv) > 2 else "oof_predictions.csv"
    dep = load(deploy_path)

    # ---- 1) 배포 성능 ---------------------------------------------------------
    dep_rmse = rmse(dep["residual"])
    ss_res = float(np.sum(np.square(dep["residual"])))
    ss_tot = float(np.sum(np.square(dep["actual_c65"] - dep["actual_c65"].mean())))
    dep_r2 = 1 - ss_res / ss_tot
    mean_bias = float(dep["residual"].mean())
    print(f"[배포] {len(dep)} wafer | RMSE={dep_rmse:.2f} R²={dep_r2:.4f} "
          f"평균 잔차={mean_bias:+.2f}")

    edges = np.linspace(dep["C33"].min(), dep["C33"].max(), N_PHASE_BINS + 1)
    dep_res_curve = binned(dep, "residual", edges)
    dep_act_curve = binned(dep, "actual_c65", edges)

    # ---- 2) 멘토안: 이전 사이클(OOF seg1) 위상 곡선으로 보정 -------------------
    mentor_rmse = np.nan
    prev_curve = None
    if os.path.exists(oof_path):
        oof = load(oof_path)
        # OOF 파일에서 seg1(PM 전) 추출: seq 정렬 후 C33 최대 하락 지점
        oof_seq = oof.sort_values("seq").reset_index(drop=True)
        d = np.diff(oof_seq["C33"].to_numpy())
        cut = int(np.argmin(d)) + 1
        prev = oof_seq.iloc[:cut]
        prev_curve = binned(prev, "residual", edges)
        bias_map = prev_curve.set_index("phase")["residual"]
        centers = 0.5 * (edges[:-1] + edges[1:])
        idx = np.digitize(dep["C33"], edges) - 1
        pb = np.array([bias_map.get(centers[i], np.nan)
                       if 0 <= i < len(centers) else np.nan for i in idx])
        ok = ~np.isnan(pb)
        mentor_rmse = rmse(dep["residual"][ok] - pb[ok])
        print(f"[멘토안] 이전 사이클 위상 bias 적용(겹침 {ok.sum()} wafer): "
              f"RMSE {rmse(dep['residual'][ok]):.2f} → {mentor_rmse:.2f}")
    else:
        print(f"[멘토안] {oof_path} 없음 — 건너뜀")

    # ---- 3) 당 사이클 rolling bias (라벨 지연별) -------------------------------
    res = dep["residual"].to_numpy()
    delay_results = {}
    for d_lag in LABEL_DELAYS:
        corrected = res.copy().astype(float)
        run_sum, run_buf = 0.0, []
        for i in range(len(res)):
            j = i - d_lag  # 라벨이 도착한 마지막 wafer 인덱스
            if j >= 0:
                run_buf.append(res[j])
                if len(run_buf) > ROLLING_WINDOW:
                    run_buf.pop(0)
            if run_buf:
                corrected[i] = res[i] - float(np.mean(run_buf))
        delay_results[d_lag] = rmse(corrected)
        print(f"[대안] rolling bias (지연 {d_lag:>5} wafer): RMSE {delay_results[d_lag]:.2f}")
    oracle_const = rmse(res - res.mean())
    print(f"[참고] oracle 상수 bias(이론 상한): RMSE {oracle_const:.2f}")

    # ---- 4) 그림 ---------------------------------------------------------------
    fig, ax = plt.subplots(1, 3, figsize=(17, 5))
    ax[0].plot(dep_res_curve["phase"], dep_res_curve["residual"], "s-",
               label="deploy residual (curr cycle)")
    if prev_curve is not None:
        ax[0].plot(prev_curve["phase"], prev_curve["residual"], "o--",
                   label="prev cycle residual (OOF seg1)")
    ax[0].axhline(0, color="gray", lw=0.8)
    ax[0].set_title("phase-aligned mean residual")
    ax[0].set_xlabel("C33 phase")
    ax[0].legend()

    ax[1].plot(dep_act_curve["phase"], dep_act_curve["actual_c65"], "s-", color="tab:red")
    ax[1].set_title("mean ACTUAL C65 by phase (실력치 안정 구간 확인용)")
    ax[1].set_xlabel("C33 phase")

    labels = [f"D={k}" for k in delay_results] + ["oracle\nconst", "mentor\nphase"]
    values = list(delay_results.values()) + [oracle_const, mentor_rmse]
    ax[2].bar(labels, values, color="tab:blue")
    ax[2].axhline(dep_rmse, color="tab:red", ls="--", label=f"무보정 {dep_rmse:.1f}")
    ax[2].set_title("RMSE by correction strategy")
    ax[2].legend()
    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=150)
    print(f"그림 저장: {OUT_PNG}")

    # ---- 5) 리포트 -------------------------------------------------------------
    lines = [
        "# 배포 조건 Model R2R 검증 (시간 분할 — prepm 학습 → postpm 예측)",
        "",
        f"- 배포 RMSE = {dep_rmse:.2f} (OOF 대비 악화폭 확인), R² = {dep_r2:.4f}, "
        f"평균 잔차 = {mean_bias:+.2f}",
        f"- 멘토안(이전 사이클 위상 bias): RMSE {mentor_rmse:.2f}",
        f"- 당 사이클 rolling bias: " + ", ".join(
            f"D={k} → {v:.2f}" for k, v in delay_results.items()),
        f"- oracle 상수 bias: {oracle_const:.2f}",
        f"- {BIAS_CLAMP_NOTE}",
        "",
        "## 해석 가이드",
        "- 평균 잔차가 크게 비영(非零)이면: 보정 대상이 실존 — R2R류 장치가 필요함",
        "- 멘토안 RMSE가 무보정보다 낮으면: 사이클 간 전이 성립 (멘토안 채택 근거)",
        "- D=10200(풀스케일)이 무보정과 같으면: 관측 구간 내 라벨 미도착 —",
        "  사이클 내 보정은 라벨 지연 축소(inline 계측·데모 스케일) 없인 불가",
        "- actual C65 위상 곡선의 안정화 시점 = 실력치 초기 표본의 시작점 근거 (A7·A8)",
    ]
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"리포트 저장: {OUT_MD}")


if __name__ == "__main__":
    main()
