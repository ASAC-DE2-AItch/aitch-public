# -*- coding: utf-8 -*-
"""
retrain_lean85.py — lean-85 재학습 CLI (운영 SOP 진입점)

사용 (프로젝트 venv 활성화 후, handoff_lean85/ 에서):
    python retrain_lean85.py                          # 기본 경로 자동 탐색
    python retrain_lean85.py --data <누적_raw.csv> --pm-log <pm_log.json> --tag daily
    python retrain_lean85.py --acceptance             # 재학습 + B2′ 수용검사(수 분 소요)

CT① 오케스트레이터 호출 (자동 경로 — `ct.ct1_retrain_cmd`):
    python retrain_lean85.py --data <trainset.csv> --pm-log <pm_log.json> --tag daily \
        --window-meta <ct1_window_*.json> --result-json <retrain_result.json>
    → 산출 stamp 폴더명을 예측할 수 없으므로 `--result-json` 으로 되돌려 준다.

트리거 (헌법 3-3 ① — 2026-07-22 재확정):
    ① 정기 **일간 슬라이딩**   ② 요란 PM 기입 즉시   ③ PSI 경보는 트리거 아님(서빙 보호)
"""
import argparse
import json
import logging
import os
from pathlib import Path

import pandas as pd

import lean85_pipeline as lp

logger = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser(description="lean-85 재학습")
    ap.add_argument("--data", default=None, help="누적 raw 트레이스 CSV (기본: train_data.csv 자동 탐색)")
    ap.add_argument("--pm-log", default=None, help="pm_log.json 경로 (기본: 프로젝트 루트 자동 탐색)")
    ap.add_argument("--out", default=None, help="모델 저장 폴더 (기본: params.yaml lean85.model_store_dir = models/c65_predictor/v2_lean85)")
    ap.add_argument("--tag", default=None,
                    help=f"버전 태그 (CT① 고정 어휘: {', '.join(lp.CT1_TAGS)} — 설계 D7)")
    ap.add_argument("--acceptance", action="store_true", help="재학습 후 B2′ 수용검사 실행")
    ap.add_argument("--window-meta", default=None,
                    help="CT① 조립기가 낸 창 메타 JSON — manifest 에 실어 게이트 B군이 대조")
    ap.add_argument("--result-json", default=None,
                    help="산출 stamp 폴더 경로를 되돌려줄 JSON (오케스트레이터용)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data_p = a.data or lp.find_file("train_data.csv")
    pm_p = a.pm_log or lp.find_file("pm_log.json")
    logger.info("[1/3] 데이터 로드: %s", data_p)
    raw = pd.read_csv(data_p)
    logger.info("      rows=%s | pm_log=%s", f"{len(raw):,}", pm_p)

    wmeta = None
    if a.window_meta:
        with open(a.window_meta, encoding="utf-8") as f:
            wmeta = json.load(f)
        if not isinstance(wmeta, dict):        # json.loads 성공 ≠ dict (헌법 7장)
            raise ValueError(f"--window-meta 가 객체가 아님: {type(wmeta).__name__}")
        logger.info("      창: %s ~ %s (확장=%s)", wmeta.get("start", "?")[:19],
                    wmeta.get("cutoff", "?")[:19], wmeta.get("extended"))

    logger.info("[2/3] 피처 빌드 + 재학습 (동결 파라미터)")
    # return_table=True: 수용검사가 같은 raw 로 build_wafer_table 을 다시 돌리지 않게 재사용
    # (누적 raw 전체 집계 2회 → 1회, 리뷰 §3-4)
    vdir, model, mani, table = lp.retrain(raw, pm_p, out_dir=a.out, tag=a.tag,
                                          return_table=True, window_meta=wmeta)
    logger.info("      웨이퍼 %s | 기간 %s ~ %s", f"{mani['n_wafers_train']:,}",
                mani["train_period"][0][:10], mani["train_period"][1][:10])
    logger.info("      저장: %s", vdir)

    if a.acceptance:
        logger.info("[3/3] B2′ 수용검사 (기대 %s±%s)", lp.BENCH_POOLED, lp.BENCH_TOL)
        lean, params, rounds = lp.load_frozen()
        acc = lp.walkforward_acceptance(table, lean, params, rounds)
        mani["acceptance"] = acc
        with open(vdir / "manifest.json", "w", encoding="utf-8") as f:   # 리뷰 §2-4
            json.dump(mani, f, ensure_ascii=False, indent=2)
    else:
        logger.info("[3/3] 수용검사 생략 (--acceptance 로 실행 가능)")

    if a.result_json:
        # 오케스트레이터는 stamp 폴더명을 미리 알 수 없다 — 산출 경로를 파일로 되돌려 준다.
        # 원자 교체: 읽는 쪽(부모 프로세스)에 부분 쓰기를 노출하지 않는다 (헌법 7장).
        rp = Path(a.result_json)
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.with_suffix(rp.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"model_dir": str(vdir.resolve()), "model_version": vdir.name,
             "n_wafers_train": mani["n_wafers_train"], "train_period": mani["train_period"],
             "features_n": mani["features_n"]},
            ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(tmp, rp)
        logger.info("      결과 기록: %s", rp)

    logger.info("\n완료. 예측:")
    logger.info('  python predict_lean85.py --model "%s" --data <신규_X.csv>', vdir)
    # 헌법 3-3: 재학습 시 models/CHANGELOG.md 기록 의무 (7장 "자주 하는 실수" 등재 항목)
    logger.warning("\n⚠️ 잊지 말 것 — models/CHANGELOG.md 에 아래를 즉시 기록하세요 (헌법 3-3):")
    logger.warning("   버전 %s | 날짜 %s | 담당 A | RMSE(수용검사 %s) | 피처 %d개 | 변경 사유",
                   vdir.name, mani["created_at_local"][:10],
                   mani.get("acceptance", {}).get("pooled_rmse", "미실행"),
                   mani["features_n"])


if __name__ == "__main__":
    main()
