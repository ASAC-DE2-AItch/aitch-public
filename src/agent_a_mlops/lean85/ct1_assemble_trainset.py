# -*- coding: utf-8 -*-
"""ct1_assemble_trainset.py — CT①(lean85) 학습·평가셋 조립기 (WP-2 / 설계 D2·D9).

역할: 정본 raw 원천을 **날짜로 절단**해 challenger 학습셋과 champion–challenger 공통
평가셋을 만든다. 창 산식(완결 요란 레짐 확장 포함)의 단일 소스는
`lean85_pipeline.slice_train_window()` 이고, 이 스크립트는 그 결과를 파일로 떨어뜨린다.

    학습셋 = (d − H − window, d − H]   ← challenger·champion 공통 학습 상한 = d − H
    평가셋 = (d − H, d]                 ← 양쪽 모두 학습한 적 없는 구간 (설계 D4)

라벨 정책 (설계 D9 · 사이클_정의 신규 결정 5):
  · **라벨(C65) 완결 wafer만** 학습·평가에 넣는다.
  · **차단(clamp)됐던 옛 라벨도 CT① 학습에는 포함**한다 — 소정비는 레짐 연속이라
    그 구간을 버리면 레짐 전환 표본이 되레 희소해진다.
  · **온셋 구간(요란 PM 후 ONSET_DAYS) wafer 는 학습에 포함**한다 — 레짐 피처
    (`days_since_last_pm` 등 gain 1·2위)가 흡수하는 구조다. 제외는 **평가에서만**이며,
    그 판정은 게이트(`validate_lean85`)가 `low_confidence_flags` 단일 소스로 수행한다
    (여기서 다시 구현하면 판정식이 두 벌로 갈라진다).

데이터 원천 (D9):
  · **시뮬 단계(현재)**: 정본 raw CSV(`--data`, `.gz` 가능·복수 지정 가능). 실측 68일
    (`Data/문제1(하)/train_data.csv`) + 합성 297일(`Data/synthetic_1y/`)이 같은 스키마다.
  · **실운영 전환(후속)**: raw 적재기(fdc.raw → 일자 파티션) + `wafer_predictions.actual_c65`
    조인으로 교체한다. 이 CLI 인터페이스(`--data`/`--out`)는 그때도 불변이다.
    Kafka 되감기(CT² D9 방식)는 365일 창에 부적합 — retention 밖이다.

exit code (CT² 조립기 관례 정합):
    0 = 정상 · 2 = 표본 게이트 미달(학습 wafer < `ct.ct1_gate_min_train_wafers`) · 1 = 실행 오류

CLI:
    python ct1_assemble_trainset.py --data ../../../Data/synthetic_1y/full_year_*.csv.gz \
        --pm-log ../../../pm_log.json --now 2026-08-04T02:00:00 --out ../../../Data/ct1
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lean85_pipeline as lp  # noqa: E402  (창 산식·피처 정의 단일 소스)

log = logging.getLogger("ct1.assemble")

EXIT_OK, EXIT_ERROR, EXIT_GATED = 0, 1, 2
TRAINSET_PREFIX = "ct1_trainset"
EVALSET_PREFIX = "ct1_evalset"
WINDOW_PREFIX = "ct1_window"


def _atomic_write_json(obj: dict, path: Path) -> None:
    """같은 디렉토리 tmp + `os.replace` 원자 교체 (헌법 7장 — 부분 쓰기 노출 금지)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _gc_workdir(out_dir: Path, days: float) -> None:
    """작업 디렉토리 보존 정리 — 조립 산출물은 **재생성 가능한 파생물**이다 (리뷰 S4).

    감사 정본은 `ct_decisions` + `<stamp>/manifest.json`(창 메타 사본)이므로 CSV 자체를
    영구 보관할 이유가 없다. 일간 캐던스에서 방치하면 연 365쌍이 쌓인다.
    삭제 실패는 삼키지 않고 로그로 드러낸다.
    """
    if days <= 0:
        return
    cutoff = time.time() - days * 86400
    for pat in (f"{TRAINSET_PREFIX}_*", f"{EVALSET_PREFIX}_*", f"{WINDOW_PREFIX}_*"):
        for f in out_dir.glob(pat):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    log.info("작업 디렉토리 정리: %s", f.name)
            except OSError as e:
                log.warning("정리 실패(다음 회차 재시도): %s — %s", f.name, e)


def expand_sources(patterns) -> list[Path]:
    """`--data` 패턴(글롭 허용) → 실존 파일 목록.

    셸이 글롭을 확장하지 않는 환경(Windows PowerShell·subprocess argv)을 위해 직접
    확장한다. 정렬은 이름순 — 월별 파일(`synth_2019-03.csv.gz`)이 시간순이 된다.

    Raises:
        FileNotFoundError: 어떤 패턴도 파일에 매칭되지 않을 때.
    """
    out: list[Path] = []
    for pat in patterns:
        hits = [Path(p) for p in sorted(globmod.glob(str(pat)))]
        if not hits and Path(pat).exists():
            hits = [Path(pat)]
        out.extend(hits)
    if not out:
        raise FileNotFoundError(f"raw 원천을 찾지 못함: {list(patterns)}")
    seen, uniq = set(), []
    for p in out:                                   # 중복 제거(패턴 겹침) — 순서 보존
        r = p.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(p)
    return uniq


def load_raw(paths) -> pd.DataFrame:
    """raw CSV(.gz 포함) 복수 파일 로드 → 단일 DataFrame.

    스키마가 다른 파일이 섞이면 concat 이 NaN 컬럼을 만들어 조용히 통과하므로,
    첫 파일의 컬럼 집합과 대조해 **명시적 예외**로 막는다. 단 판정 축은 **집합**이다 —
    순서만 다른 파일까지 막으면 메시지가 "누락 [] / 초과 []" 로 나와 원인을 오도한다.
    """
    frames, cols0 = [], None
    for p in paths:
        df = pd.read_csv(p)                          # .gz 는 확장자로 자동 판별
        if cols0 is None:
            cols0 = list(df.columns)
        elif list(df.columns) != cols0:
            missing = set(cols0) - set(df.columns)
            extra = set(df.columns) - set(cols0)
            if not missing and not extra:      # 집합 동일 = 순서만 다름 → 정렬 수용
                log.info("  컬럼 순서 상이 → 첫 파일 순서로 재정렬: %s", p.name)
                df = df[cols0]
            else:
                raise ValueError(
                    f"raw 스키마 불일치: {p} (누락 {sorted(missing)[:5]} / "
                    f"초과 {sorted(extra)[:5]})")
        log.info("  로드 %s — rows=%s", p.name, f"{len(df):,}")
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    log.info("  합계 rows=%s", f"{len(raw):,}")
    return raw


def labelled_wafers(raw: pd.DataFrame) -> set:
    """라벨(C65) 완결 wafer id 집합 — 라벨 없는 wafer 는 학습·평가에서 제외 (D9).

    실운영 전환 시 이 함수만 `wafer_predictions.actual_c65`(measured_at 존재분) 조인으로
    바꾸면 된다.
    """
    if lp.TARGET_COL not in raw.columns:
        return set()
    g = raw.groupby(lp.ID_COL)[lp.TARGET_COL].first()
    return set(g[g.notna()].index)


def assemble(raw: pd.DataFrame, pm_log, now, cfg: dict):
    """raw → (학습 wafer id, 평가 wafer id, window_meta). 파일 쓰기는 하지 않는다.

    Args:
        raw: 정본 raw 트레이스 (여러 파일 concat 가능).
        pm_log: pm_log.json 경로 또는 엔트리 리스트.
        now: 실행 기준 시각 d.
        cfg: `lean85_pipeline.ct1_config()` 산출 (창·버퍼·표본 하한).

    Returns:
        (train_ids: set, eval_ids: set, window_meta: dict)

    Raises:
        ValueError: 창 누수·그룹 분할 위반 (헌법 1-3 — 조립 시점 1차 방어선).
    """
    wt = lp.wafer_times(raw)
    pm_events = lp.parse_pm_entries_full(pm_log)
    mask, wmeta = lp.slice_train_window(
        wt, now,
        window_days=cfg["ct1_window_days"],
        pm_events=pm_events,
        buffer_days=cfg["ct1_gate_eval_days"],
        require_complete_loud_regime=cfg["ct1_require_complete_loud_regime"])

    labelled = labelled_wafers(raw)
    ts = pd.to_datetime(wt["wf_ts"])
    cutoff, now_ts = pd.Timestamp(wmeta["cutoff"]), pd.Timestamp(wmeta["now"])
    eval_mask = (ts > cutoff) & (ts <= now_ts)

    train_ids = set(wt.loc[mask, lp.ID_COL]) & labelled
    eval_ids = set(wt.loc[eval_mask, lp.ID_COL]) & labelled

    # 헌법 1-3 — 조립 시점 1차 방어선 (게이트가 같은 검사를 독립 수행한다: 2중 가드)
    lp.assert_window_no_leakage(wt.loc[wt[lp.ID_COL].isin(train_ids), "wf_ts"], cutoff)
    lp.assert_disjoint_wafers(train_ids, eval_ids)

    wmeta.update({
        "n_train_wafers": len(train_ids),
        "n_eval_wafers": len(eval_ids),
        "n_labelled_wafers": len(labelled),
        "n_wafers_total": len(wt),
        "raw_period": [str(ts.min()), str(ts.max())],
    })
    return train_ids, eval_ids, wmeta


def build_argparser():
    """CLI 파서 구성."""
    ap = argparse.ArgumentParser(
        prog="ct1_assemble_trainset",
        description="CT①(lean85) 학습·평가셋 조립 (exit 0=정상 / 2=표본 미달 / 1=오류)")
    ap.add_argument("--data", nargs="+", required=True,
                    help="정본 raw CSV(.gz 가능, 글롭·복수 지정 가능)")
    ap.add_argument("--pm-log", default=None, help="pm_log.json (기본: 자동 탐색)")
    ap.add_argument("--now", default=None,
                    help="실행 기준 시각 d (ISO. 기본: 현재 시각 — 압축시간 데모는 명시 권장)")
    ap.add_argument("--out", required=True, help="산출 디렉토리")
    ap.add_argument("--stamp", default=None, help="산출 파일 stamp (기본: now 기준 자동)")
    ap.add_argument("--window-days", type=float, default=None, help="창 override (기본 config)")
    ap.add_argument("--buffer-days", type=float, default=None, help="평가 버퍼 H override")
    ap.add_argument("--min-train-wafers", type=int, default=None, help="표본 하한 override")
    return ap


def main(argv=None) -> int:
    """CLI 진입점 — 학습셋·평가셋·창 메타 3파일 산출."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s │ %(levelname)-7s │ [ct1.assemble] %(message)s")
    a = build_argparser().parse_args(argv)

    try:
        cfg = lp.ct1_config()
        if a.window_days is not None:
            cfg["ct1_window_days"] = a.window_days
        if a.buffer_days is not None:
            cfg["ct1_gate_eval_days"] = a.buffer_days
        if a.min_train_wafers is not None:
            cfg["ct1_gate_min_train_wafers"] = a.min_train_wafers

        # naive UTC 로 정규화 — 오케스트레이터는 aware UTC 로 도는데 wafer 시각·pm_log 는
        # naive 다. 여기서 벗기지 않으면 창 비교가 통째로 TypeError 다 (리뷰 B1·헌법 7장).
        now = lp.naive_utc(a.now) if a.now else pd.Timestamp.utcnow().tz_localize(None)
        pm_p = a.pm_log or lp.find_file("pm_log.json")
        out_dir = Path(a.out)
        stamp = a.stamp or now.strftime("%Y%m%d_%H%M%S")

        log.info("원천 확장 중…")
        sources = expand_sources(a.data)                 # 글롭은 1회만 (중복 확장 방지)
        raw = load_raw(sources)
        train_ids, eval_ids, wmeta = assemble(raw, pm_p, now, cfg)
        wmeta["pm_log"] = str(pm_p)
        wmeta["sources"] = [str(p) for p in sources]

        log.info("창 %s ~ %s (실효 %.1f일, 확장=%s, 완결요란레짐 %d개)",
                 wmeta["start"][:19], wmeta["cutoff"][:19],
                 wmeta["effective_window_days"], wmeta["extended"],
                 wmeta["complete_loud_regimes_in_window"])
        log.info("학습 wafer %s장 · 평가 wafer %s장 (라벨 완결 %s장 / 전체 %s장)",
                 f"{len(train_ids):,}", f"{len(eval_ids):,}",
                 f"{wmeta['n_labelled_wafers']:,}", f"{wmeta['n_wafers_total']:,}")
        if wmeta["extend_failed"]:
            log.warning("완결 요란 레짐이 창 밖에도 없음 — 워밍업 구간(합의안 C4: 창이 찰 "
                        "때까지 누적 사용). 전환 표본 희소 상태로 학습된다.")

        out_dir.mkdir(parents=True, exist_ok=True)
        win_p = out_dir / f"{WINDOW_PREFIX}_{stamp}.json"

        # 표본 게이트는 **쓰기 전에** 판정한다 — 미달인데 CSV 를 남기면 다음 실행이
        # 그걸 주워 학습할 여지가 생긴다. 창 메타는 남긴다(진단 근거).
        need = int(cfg["ct1_gate_min_train_wafers"])
        if len(train_ids) < need:
            wmeta["gated"] = f"학습 wafer {len(train_ids)} < 하한 {need}"
            _atomic_write_json(wmeta, win_p)
            log.error("표본 게이트 미달 — %s (창 메타만 기록: %s)", wmeta["gated"], win_p)
            return EXIT_GATED

        # **gzip 으로 쓴다** — 365일 창의 raw 는 비압축이면 수백 MB이고 일간 캐던스라
        # 매일 한 쌍씩 쌓인다 (리뷰 S4). pandas 는 확장자로 자동 판별하므로 소비 측
        # (`pd.read_csv`)은 무변경이다.
        train_p = out_dir / f"{TRAINSET_PREFIX}_{stamp}.csv.gz"
        eval_p = out_dir / f"{EVALSET_PREFIX}_{stamp}.csv.gz"
        raw[raw[lp.ID_COL].isin(train_ids)].to_csv(train_p, index=False, compression="gzip")
        raw[raw[lp.ID_COL].isin(eval_ids)].to_csv(eval_p, index=False, compression="gzip")
        _gc_workdir(out_dir, float(cfg.get("ct1_gc_keep_unpromoted_days", 14)))
        wmeta.update({"gated": None, "trainset": str(train_p.resolve()),
                      "evalset": str(eval_p.resolve()),
                      "assembled_at": datetime.now().isoformat(timespec="seconds")})
        _atomic_write_json(wmeta, win_p)
        log.info("산출: %s · %s · %s", train_p.name, eval_p.name, win_p.name)
        return EXIT_OK
    except Exception as e:                              # noqa: BLE001 — 실행 오류는 1로 구분
        log.error("조립 실패(판정 불가): %s", e, exc_info=True)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
