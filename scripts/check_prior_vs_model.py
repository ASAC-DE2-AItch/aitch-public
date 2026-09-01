# -*- coding: utf-8 -*-
"""ⓟ1 — v8 모델 vs 감쇠 템플릿 이중보정(과보정) 재실측.

질문(PM 윤준호 → A 팀원 A): v8에 high_regime_days 피처가 생겼으니, 라벨-프리 프라이어의
       감쇠 템플릿(check_transient_tracker의 L + A·exp(−t/τ))이 모델이 이미 하는 일을
       또 하는 것 아닌가? 겹치면 스택 시 과보정.

방법(핵심): 템플릿을 실측 Y가 아니라 **모델 잔차**에 다시 fit한다.
       r(t) = actual − ŷ_v8  →  r ≈ c + A·exp(−t/τ)  (τ 그리드 + 폐형 해)
       · A ≈ 0  (잔차에 감쇠 모양 없음)  → high_regime_days가 이미 흡수 → 과보정, 템플릿 폐기
       · A 유의미 (잔차에 exp 잔존)       → 모델이 못 잡음 → 템플릿 유지
       판정 지표 = 감쇠항만 얹었을 때 seg2 MAE 감소폭.

입력(팀원 A님 쪽): v8이 seg2 wafer에 낸 예측. 2컬럼 CSV [C64, y_pred].
       └ 없으면 모델·피처 코드로 생성: preds = df_seg2[["C64"]]; preds["y_pred"] = model.predict(X_seg2)
         (X_seg2 = high_regime_days 포함 v8 피처셋) → to_csv(PREDS, index=False)

사용법:
    python scripts/check_prior_vs_model.py                 # 기본 preds 경로
    python scripts/check_prior_vs_model.py path/to/preds.csv

주의: seg2 in-sample fit이라 절대성능 주장 금지 — **모델 대비 상대 감소폭**만 읽는다.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRAIN = ROOT / "Data" / "문제1(하)" / "train_data.csv"
PREDS_DEFAULT = ROOT / "Data" / "문제1(하)" / "v8_seg2_preds.csv"  # [C64, y_pred]
TAU_GRID = np.arange(2.0, 40.0, 0.5)  # check_transient_tracker와 동일 범위


def _fit_decay(t, r):
    """r(t) ≈ c + A·exp(−t/τ) — τ 그리드 스윕 + (c, A) 최소제곱 폐형 해.

    반환: (tau, c, A). check_transient_tracker의 fit 기법과 동일(잔차 대상으로만 교체).
    """
    best = None
    for tau in TAU_GRID:
        z = np.exp(-t / tau)
        X = np.column_stack([np.ones_like(z), z])
        coef, *_ = np.linalg.lstsq(X, r, rcond=None)
        sse = float(((X @ coef - r) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, tau, coef[0], coef[1])
    _, tau, c, A = best
    return tau, c, A


def _seg2_wafers():
    """train_data → C64 단위 → C33 리셋으로 seg2 분리 (check_transient_tracker와 동일 규약).

    반환: DataFrame[C64, day, actual]  (day = 요란 PM 후 경과일, actual = 해당 wafer C65).
    """
    df = pd.read_csv(TRAIN, usecols=lambda c: c in {"C64", "C10", "C33", "C65"})
    df = df.assign(dt=pd.to_datetime(df["C10"], unit="s", errors="coerce"))
    ids = (df.sort_values("dt").groupby("C64", sort=False)
             .agg(dt=("dt", "first"), c33=("C33", "first"), c65=("C65", "first"))
             .dropna().reset_index().sort_values("dt").reset_index(drop=True))
    c33 = ids["c33"].to_numpy(float)
    reset = next(i + 1 for i in range(len(c33) - 1)
                 if c33[i + 1] < c33[i] * 0.5 and c33[i] - c33[i + 1] > 5)
    seg2 = ids.iloc[reset:].reset_index(drop=True)
    day = (seg2["dt"] - seg2["dt"].iloc[0]).dt.total_seconds() / 86400
    return pd.DataFrame({"C64": seg2["C64"], "day": day.to_numpy(float),
                         "actual": seg2["c65"].to_numpy(float)})


def main():
    preds_path = Path(sys.argv[1]) if len(sys.argv) > 1 else PREDS_DEFAULT
    if not preds_path.exists():
        print(f"[중단] 예측 파일 없음: {preds_path}")
        print("  → v8 예측을 2컬럼 CSV [C64, y_pred]로 저장 후 경로를 넘겨주세요.")
        print("    preds = seg2[['C64']].copy(); preds['y_pred'] = model.predict(X_seg2)")
        print("    preds.to_csv('v8_seg2_preds.csv', index=False)")
        return

    seg2 = _seg2_wafers()
    preds = pd.read_csv(preds_path, usecols=lambda c: c in {"C64", "y_pred"})
    m = seg2.merge(preds, on="C64", how="inner").dropna()
    if len(m) < 50:
        print(f"[경고] 매칭 wafer {len(m)}건 — C64 키/seg2 범위 확인 필요")

    t = m["day"].to_numpy(float)
    actual = m["actual"].to_numpy(float)
    yhat = m["y_pred"].to_numpy(float)
    r = actual - yhat
    early = t <= 14

    mae_model = np.abs(r).mean()
    mae_model_e = np.abs(r[early]).mean()

    tau, c, A = _fit_decay(t, r)
    decay = A * np.exp(-t / tau)          # 감쇠항만 (레벨 편향 c 제외)
    full = c + decay                       # 잔차 전체 fit

    mae_decay = np.abs(r - decay).mean()   # 모델 + 감쇠항
    mae_decay_e = np.abs((r - decay)[early]).mean()
    mae_full = np.abs(r - full).mean()     # 모델 + 감쇠 + 레벨보정

    noise = float(np.std(r - full))        # fit 후 잔여 노이즈 수준
    drop_decay = mae_model - mae_decay     # 감쇠항이 걷어낸 MAE
    drop_decay_e = mae_model_e - mae_decay_e

    print("=" * 74)
    print(f"매칭 seg2 wafer: {len(m)}건 (초반 14일 {int(early.sum())}건)  ※ in-sample")
    print("-" * 74)
    print(f"ⓐ v8 무보정 잔차 MAE        전체 {mae_model:7.1f} │ 초반14일 {mae_model_e:7.1f}")
    print(f"ⓑ 잔차 감쇠 fit             τ={tau:.1f}일  A={A:+.1f}  (레벨편향 c={c:+.1f})")
    print(f"ⓒ v8 + 감쇠항 MAE           전체 {mae_decay:7.1f} │ 초반14일 {mae_decay_e:7.1f}")
    print(f"   └ 감쇠항이 걷어낸 MAE     전체 {drop_decay:+7.1f} │ 초반14일 {drop_decay_e:+7.1f}")
    print(f"ⓓ v8 + 감쇠 + 레벨보정 MAE  전체 {mae_full:7.1f}  (참고: 레벨편향까지 포함)")
    print(f"   fit 후 잔여 노이즈 σ ≈ {noise:.1f}")
    print("-" * 74)

    # 판정 — 감쇠 진폭이 노이즈 대비 유의미하고 초반 MAE를 실질 감소시키는가
    sig = abs(A) > max(2.0 * noise, 15.0)      # 진폭이 노이즈/최소폭 초과
    helps = drop_decay_e >= 5.0                 # 초반 14일 MAE를 5 이상 낮춤
    if sig and helps:
        print("판정 → 템플릿 유지: 잔차에 감쇠 잔존(모델 미흡수), 얹으면 개선.")
    elif not sig and abs(drop_decay_e) < 3.0:
        print("판정 → 템플릿 폐기(과보정 위험): high_regime_days가 감쇠를 이미 흡수.")
    else:
        print("판정 → 경계 — 수치 보고 사람이 결정 (A/노이즈·초반 감소폭 참조).")
    print("=" * 74)
    print("보고 양식(팀원 A님): 'ⓐ 무보정 MAE = ○○ / ⓒ 감쇠 얹은 MAE = ○○ / 판정' 세 줄.")


if __name__ == "__main__":
    main()
