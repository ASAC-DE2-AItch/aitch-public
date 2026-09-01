# -*- coding: utf-8 -*-
"""test_harness.py — 하네스 순수 로직 단위 테스트 (xgboost 없이 실행 가능).

목적: 무거운 백테스트를 돌리기 전에 **군 배정·구간 라벨·통계·누수 가드** 로직을
독립적으로 검증한다. xgboost/sklearn 이 없는 환경에서도 돌도록 stub 을 주입한다.

실행: python test_harness.py     (exit 0 = 전 항목 PASS)
"""
from __future__ import annotations

import ast
import logging
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger("test")

EXP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXP_DIR))


def _install_stubs() -> None:
    """xgboost·sklearn 부재 환경용 최소 stub (순수 로직 테스트 전용)."""
    if "xgboost" not in sys.modules:
        try:
            import xgboost  # noqa: F401
        except ImportError:
            m = types.ModuleType("xgboost")
            m.__version__ = "stub"

            class _R:
                def __init__(self, **kw):
                    self.kw = kw

            m.XGBRegressor = _R
            m.Booster = object
            sys.modules["xgboost"] = m
    try:
        import sklearn.metrics  # noqa: F401
    except ImportError:
        sk = types.ModuleType("sklearn")
        me = types.ModuleType("sklearn.metrics")
        me.mean_squared_error = lambda a, b: float(np.mean((np.asarray(a, float) -
                                                            np.asarray(b, float)) ** 2))
        sk.metrics = me
        sys.modules["sklearn"] = sk
        sys.modules["sklearn.metrics"] = me


_install_stubs()

import CONFIG as C          # noqa: E402
import run_backtest as rb   # noqa: E402
import analyze as an        # noqa: E402
import verify_leakage as vl  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    """단일 검증 항목 결과를 기록한다 (실패 시 FAILS 에 누적)."""
    logger.info("  %s  %s%s", "PASS" if cond else "FAIL", name, f"  [{detail}]" if detail else "")
    if not cond:
        FAILS.append(name)


def t_d7_grid() -> None:
    """ARM-D7: 7일 그리드 · 첫날 재학습 · 설계서 43회."""
    days = pd.DatetimeIndex(pd.date_range("2019-02-09", "2019-11-30", freq="D"))
    r = rb.d7_retrain_days(days)
    gaps = np.diff([d.toordinal() for d in r])
    check("D7 첫 재학습일 = 평가 시작일", r[0] == days[0])
    check("D7 재학습 43회 (설계서 §3)", len(r) == 43, f"len={len(r)}")
    check("D7 간격 전부 7일", set(gaps.tolist()) == {7}, f"gaps={sorted(set(gaps.tolist()))}")

    # 결측 생산일 이월 동작
    sparse = days.delete([7, 8, 14])
    r2 = rb.d7_retrain_days(sparse)
    check("D7 결측일 이월 시 중복 없음", len(set(r2)) == len(r2))
    check("D7 이월 후에도 모든 재학습일이 생산일", set(r2) <= set(sparse))


def t_assignment() -> None:
    """군 배정: D1=매일 d-1 · D7=계단 · STATIC=M0 고정."""
    days = pd.DatetimeIndex(pd.date_range("2019-02-09", "2019-11-30", freq="D"))
    a = rb.build_assignment(days)
    check("D1 컷오프 = d-1", bool((a["ARM-D1"] == a["wf_day"] - pd.Timedelta(days=1)).all()))
    check("STATIC 컷오프 = 2019-02-08 고정", bool((a["ARM-STATIC"] == C.INIT_TRAIN_END).all()))
    check("D7 고유 컷오프 43개", a["ARM-D7"].nunique() == 43, f"{a['ARM-D7'].nunique()}")
    check("모든 컷오프 < 평가일 (미래 차단)",
          bool(all((a[arm] < a["wf_day"]).all() for arm in C.ARMS)))
    uni = set(a["ARM-D1"]) | set(a["ARM-D7"]) | set(a["ARM-STATIC"])
    check("D7·STATIC 컷오프 ⊂ D1 컷오프 (fit 재사용 등가성)",
          set(a["ARM-D7"]) <= set(a["ARM-D1"]) and set(a["ARM-STATIC"]) <= set(a["ARM-D1"]),
          f"고유 fit={len(uni)}")
    age = (a["wf_day"] - a["ARM-D7"]).dt.days - 1
    check("D7 모델 나이 0~6 범위", bool(age.between(0, 6).all()), f"max={age.max()}")


def t_segments() -> None:
    """§6 M2 구간 라벨: major 14일 · minor 3일 · 평시."""
    days = pd.DatetimeIndex(pd.date_range("2019-02-09", "2019-11-30", freq="D"))
    s = an.segment_labels(days)
    vc = s["segment"].value_counts().to_dict()
    check("구간 라벨 3종 존재", set(vc) <= {"onset_major", "onset_minor", "steady"}, str(vc))
    check("major 온셋 = 2회 × 14일 = 28일", vc.get("onset_major", 0) == 28, str(vc))
    check("warmup 7일 플래그", int(s["is_warmup"].sum()) == C.WARMUP_DAYS)
    # major PM 당일이 온셋에 포함되는지
    check("major PM 당일 = onset_major",
          s.loc[s["wf_day"] == pd.Timestamp("2019-04-22"), "segment"].iloc[0] == "onset_major")
    check("major PM +14일은 온셋 밖",
          s.loc[s["wf_day"] == pd.Timestamp("2019-05-06"), "segment"].iloc[0] != "onset_major")


def t_bootstrap() -> None:
    """moving-block bootstrap CI: 결정성 · 커버리지 방향."""
    rng = np.random.default_rng(0)
    d = rng.normal(2.0, 1.0, 295)
    lo, hi = an.moving_block_bootstrap_ci(d, 7, 2000, 42, 0.05)
    lo2, hi2 = an.moving_block_bootstrap_ci(d, 7, 2000, 42, 0.05)
    check("bootstrap 재현성 (같은 seed)", (lo, hi) == (lo2, hi2))
    check("bootstrap CI 가 참값 2.0 포함", lo < 2.0 < hi, f"[{lo:.3f},{hi:.3f}]")
    zero = np.zeros(295)
    l0, h0 = an.moving_block_bootstrap_ci(zero, 7, 500, 42, 0.05)
    check("Δ=0 이면 CI 가 0 포함", l0 <= 0 <= h0)
    try:
        an.moving_block_bootstrap_ci(np.zeros(5), 7, 100, 42, 0.05)
        check("표본 부족 시 raise", False)
    except ValueError:
        check("표본 부족 시 raise", True)


def t_gate() -> None:
    """§8-A 게이트 판정: 임계 경계 · 챔피언 체인 · 비유한 RMSE 예외."""
    ok, gain = rb.gate_decision(100.0, 97.0, 3.0)
    check("정확히 임계 3.0% 는 승격", ok and abs(gain - 3.0) < 1e-9, f"gain={gain}")
    ok, gain = rb.gate_decision(100.0, 97.001, 3.0)
    check("임계 미만은 유지", not ok)
    ok, gain = rb.gate_decision(100.0, 105.0, 3.0)
    check("악화는 유지 + 음수 gain", (not ok) and gain < 0, f"gain={gain:.2f}")
    try:
        rb.gate_decision(0.0, 1.0, 3.0)
        check("챔피언 RMSE 0 이면 raise", False)
    except ValueError:
        check("챔피언 RMSE 0 이면 raise", True)

    # 챔피언 체인: 승격이 여러 번 일어날 수 있어야 한다 (구 구현은 구조적으로 ≤1 이었다)
    champ, swaps = 100.0, 0
    for chal in (96.0, 95.0, 94.0, 93.9, 90.0):
        promote, _ = rb.gate_decision(champ, chal, 3.0)
        if promote:
            champ, swaps = chal, swaps + 1
    check("챔피언 체인에서 다중 승격 가능", swaps >= 2, f"swaps={swaps}")


def t_guards() -> None:
    """헌법 7장: assert 금지 · 명시적 raise."""
    r = vl.check_5_no_assert()
    check("실험 스크립트에 assert 가드 없음", r["ok"], str(r["assert_usages"]))
    # 모든 스크립트 구문 유효성
    for f in sorted(EXP_DIR.glob("*.py")):
        try:
            ast.parse(f.read_text(encoding="utf-8"))
            ok = True
        except SyntaxError as e:
            ok, _ = False, e
        check(f"구문 유효 {f.name}", ok)


def t_metrics_math() -> None:
    """지표 산식: RMSE · honest R² · 군별 예측 재구성."""
    check("rmse 기본", abs(an.rmse([1, 2, 3], [1, 2, 4]) - (1 / 3) ** .5) < 1e-12)
    check("honest R² = 1-(rmse/σ)²", abs(an.r2_honest(99.84) - 0.8545) < 5e-4,
          f"{an.r2_honest(99.84):.4f}")

    days = pd.DatetimeIndex(pd.date_range("2019-02-09", "2019-02-22", freq="D"))
    a = rb.build_assignment(days)
    cuts = sorted(set(a["ARM-D1"]) | set(a["ARM-D7"]) | set(a["ARM-STATIC"]))
    rows = []
    rng = np.random.default_rng(1)
    for c in cuts:
        served = set(a.loc[a["ARM-D1"] == c, "wf_day"]) | set(a.loc[a["ARM-D7"] == c, "wf_day"]) \
            | set(a.loc[a["ARM-STATIC"] == c, "wf_day"])
        for d in sorted(served):
            for w in range(5):
                rows.append({"cutoff": c, "wf_day": d, "model_age": (d - c).days - 1,
                             "C64": f"W{d.date()}_{w}", "y": 800.0,
                             "pred": 800.0 + rng.normal(), "low_conf": 0})
    preds = pd.DataFrame(rows)
    res = an.arm_predictions(preds, a)
    check("군별 예측 재구성 3군", set(res["arm"]) == set(C.ARMS))
    n = res.groupby("arm")["C64"].nunique()
    check("군별 평가 wafer 집합 동일", n.nunique() == 1, str(n.to_dict()))
    check("D1 모델 나이 전부 0",
          bool((res.loc[res["arm"] == "ARM-D1", "model_age"] == 0).all()))
    check("STATIC 모델 나이 단조 증가",
          bool(res.loc[res["arm"] == "ARM-STATIC", "model_age"].max() == len(days) - 1))


def t_verdict() -> None:
    """§7 사전등록 판정 R1~R4 분기."""
    def mk(d1, d7, st, sig, st_sig, onset_d1, onset_d7):
        return {"M1": {"ARM-D1": {"pooled_rmse": d1}, "ARM-D7": {"pooled_rmse": d7},
                       "ARM-STATIC": {"pooled_rmse": st}},
                "M2": {"onset_major": {"ARM-D1": onset_d1, "ARM-D7": onset_d7}},
                "stats": {"D1_vs_D7": {"significant": sig},
                          "STATIC_vs_D7": {"significant": st_sig}}}

    check("R1 (≥2% + 유의)", an.verdict(mk(95, 100, 250, True, True, 95, 100))["rule"] == "R1")
    check("R2 (개선 미미)", an.verdict(mk(99.5, 100, 250, True, True, 99, 100))["rule"] == "R2")
    check("R3 (온셋만 우위)", an.verdict(mk(99.5, 100, 250, False, True, 90, 100))["rule"] == "R3")
    check("R4 (STATIC 유의차 없음 → 보류)",
          an.verdict(mk(95, 100, 101, True, False, 95, 100))["rule"] == "R4")
    check("R4 가 R1 보다 우선 (안전장치)",
          an.verdict(mk(80, 100, 100, True, False, 80, 100))["rule"] == "R4")


def main() -> int:
    """전 검증 함수를 순서대로 돌리고 실패가 하나라도 있으면 exit 1."""
    logger.info("=" * 70)
    logger.info("test_harness — 재보정 주기 실험 하네스 순수 로직 검증")
    logger.info("=" * 70)
    for fn in (t_d7_grid, t_assignment, t_segments, t_bootstrap, t_metrics_math,
               t_verdict, t_gate, t_guards):
        logger.info("\n[%s] %s", fn.__name__, fn.__doc__.splitlines()[0])
        fn()
    logger.info("\n" + "=" * 70)
    if FAILS:
        logger.error("FAIL %d건: %s", len(FAILS), FAILS)
        return 1
    logger.info("전 항목 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
