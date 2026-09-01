# -*- coding: utf-8 -*-
"""compare_runs.py — 여러 런의 판정을 나란히 비교 (설계서 §6 시드 민감도 · §5 경계 완충).

동결 seed 42 가 정본이다. 부속 런(seed 43·44, 경계 완충 1일)에서 **판정 규칙이 바뀌는지**가
확인 대상이며, 바뀐다면 결과 보고서에 그 사실을 명시해야 한다 (효과 크기가 R1 임계에
근접할수록 중요).

실행: python compare_runs.py [tag ...]     (기본: main buf1 seed43 seed44)
산출: out/sensitivity_summary.json + 표준출력 표
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import CONFIG as C

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger("compare")

DEFAULT_TAGS = ("main", "buf1", "seed43", "seed44")


def load(tag: str) -> dict | None:
    """런 tag 의 metrics.json 로드 (없으면 None)."""
    p = C.OUT_DIR / f"run_{tag}" / "metrics.json"
    if not p.exists():
        logger.warning("건너뜀: %s 없음", p)
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def row(tag: str, m: dict) -> dict:
    """런 하나에서 비교에 쓸 핵심 수치만 추린다."""
    v, sv = m["verdict"], m.get("sensitivity_excl_warmup", {})
    sg = sv.get("verdict_if_excluded", {})
    adj = m["M3"].get("weekday_adjusted", {}).get("by_age", {})
    return {
        "tag": tag,
        "seed": m["run_manifest"]["seed"],
        "buffer_days": m["run_manifest"].get("buffer_days"),
        "rule": v["rule"],
        "gain_pct": v["pooled_rel_gain_pct_D1_over_D7"],
        "p": m["stats"]["D1_vs_D7"]["p_value"],
        "rule_excl_warmup": sg.get("rule"),
        "gain_excl_warmup": sg.get("pooled_rel_gain_pct_D1_over_D7"),
        "rmse_d1": m["M1"]["ARM-D1"]["pooled_rmse"],
        "rmse_d7": m["M1"]["ARM-D7"]["pooled_rmse"],
        "rmse_static": m["M1"]["ARM-STATIC"]["pooled_rmse"],
        "onset_major_gain": v["onset_major_rel_gain_pct"],
        "m3adj_age6": adj.get("6", {}).get("degradation"),
        "age0_identity_ok": m["M3"].get("weekday_adjusted", {}).get("age0_identity_check"),
    }


def main() -> int:
    """지정 런들의 판정·효과 크기를 표로 비교하고 안정성을 판정한다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("tags", nargs="*", default=list(DEFAULT_TAGS))
    args = ap.parse_args()

    rows = [row(t, m) for t in (args.tags or DEFAULT_TAGS) if (m := load(t)) is not None]
    if not rows:
        raise FileNotFoundError("비교할 런이 없다 — run_all.bat / run_sensitivity.bat 먼저 실행")

    hdr = f"{'tag':>8} {'seed':>5} {'buf':>4} {'규칙':>5} {'개선%':>8} {'p':>10} " \
          f"{'규칙(워밍업제외)':>16} {'개선%':>8} {'M3adj age6':>11}"
    logger.info(hdr)
    logger.info("-" * len(hdr))
    for r in rows:
        logger.info("%8s %5s %4s %5s %8.2f %10.2e %16s %8s %11s",
                    r["tag"], r["seed"], r["buffer_days"], r["rule"], r["gain_pct"], r["p"],
                    r["rule_excl_warmup"],
                    "-" if r["gain_excl_warmup"] is None else f"{r['gain_excl_warmup']:.2f}",
                    "-" if r["m3adj_age6"] is None else f"{r['m3adj_age6']:+.2f}")

    rules = {r["rule"] for r in rows}
    rules_x = {r["rule_excl_warmup"] for r in rows if r["rule_excl_warmup"]}
    gains_x = [r["gain_excl_warmup"] for r in rows if r["gain_excl_warmup"] is not None]
    stable = len(rules) == 1 and len(rules_x) <= 1
    verdict = {
        "runs": rows,
        "rules_main": sorted(rules),
        "rules_excl_warmup": sorted(rules_x),
        "gain_excl_warmup_range": ([min(gains_x), max(gains_x)] if gains_x else None),
        "stable": stable,
        "call": ("판정 안정 — 모든 런에서 같은 규칙" if stable else
                 "⚠️ 판정 불안정 — 런마다 규칙이 달라진다. 결과 보고서에 반드시 명시하고, "
                 "효과 크기 기반 서술로 전환할 것"),
        "note": "동결 seed 42(tag=main)가 정본. 나머지는 부속 런 (설계서 §6)",
    }
    out = C.OUT_DIR / "sensitivity_summary.json"
    out.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("\n%s", verdict["call"])
    if gains_x:
        logger.info("워밍업 제외 개선폭 범위: %.2f%% ~ %.2f%% (R1 임계 %s%%✱)",
                    min(gains_x), max(gains_x), C.R1_MIN_REL_GAIN_PCT)
    logger.info("저장: %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
