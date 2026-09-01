"""B5-2: Qual 스냅샷 시딩 — seg1 전체창 σ 지문을 qual_snapshots에 적재.

정비 후 σ-갭 판정의 비교 기준(직전 유효 스냅샷)을 부트스트랩한다. 표본 = seg1 복귀블록
(initial_limits와 동일 소스 — R3), 건강센서 8종(C12 제외·과도 제외). 산식은
`compute_group_limits` 재사용(전체창 σ — rolling-500 클립 안 함, F12) + 분위수 center=median(M2).
챔버 전개는 `chamber_offsets.json` offset shift(밴드폭 보존). 멱등(챔버별 is_active 1개).

라이브 시딩은 PM의 chamber_offsets 6→4 재생성이 전제(H3). 순수 변환은 offsets 주입식이라
테스트가 4챔버 fixture로 공유 config를 건드리지 않는다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

from src.agent_b_spc import initial_limits as il
from src.agent_b_spc.initial_limits import LimitConfig, compute_group_limits

logger = logging.getLogger(__name__)

# 건강센서 8종 (C12 제외 — 제어 기준값이지 건강 신호 아님). C57/C58은 KEEP*지만 건강8종 밖 → 제외.
HEALTH_SENSORS = ["C11", "C15", "C16", "C17", "C31", "C61", "C62", "C63"]

QUAL_BOOTSTRAP_ID = "seg1-bootstrap"


def snapshot_stats_for_group(values, method: str, cfg: LimitConfig) -> dict:
    """seg1 전체창 값 → 스냅샷 통계 {center,sigma,ucl,lcl,method} (method별 center/band — M2·M3).

    `compute_group_limits` 재사용(전체창 σ). σ그룹: center=mean·band=±kσ / 분위수그룹:
    center=median(M2)·band=q_lcl/q_ucl(M3 — sigma_ref_of가 robust-σ를 q밴드에서 역산).
    """
    arr = np.asarray(values, dtype=float)
    r = compute_group_limits(arr, cfg)
    if method == "quantile":
        return {"center": float(np.median(arr)), "sigma": r["sigma"],
                "ucl": r["q_ucl"], "lcl": r["q_lcl"], "method": "quantile"}
    return {"center": r["center"], "sigma": r["sigma"],
            "ucl": r["ucl"], "lcl": r["lcl"], "method": "sigma"}


def shift_snapshot_row(stats: dict, offset: float) -> dict:
    """center·ucl·lcl을 같은 offset으로 평행이동 (밴드폭=robust-σ 보존). sigma는 불변.

    center만 옮기고 밴드 방치하면 밴드폭·σ가 왜곡되므로 셋을 함께 이동한다.
    """
    return {**stats, "center": stats["center"] + offset,
            "ucl": stats["ucl"] + offset, "lcl": stats["lcl"] + offset}


def gk_str(chamber, recipe, step, window, sensor) -> str:
    """"chamber|recipe|step|window|sensor" (normalize_group_key와 대칭 — 5필드 파이프)."""
    return f"{chamber}|{recipe}|{step}|{window}|{sensor}"


def build_sensor_stats(base_stats: dict, chamber: str, chamber_offsets: dict) -> dict:
    """base_stats {(recipe,step,window,sensor): stats} → 챔버 gk_str-keyed sensor_stats (offset shift).

    chamber_offsets = {sensor: offset}. 센서 오프셋 없으면 0(원 밴드).
    """
    out: dict[str, dict] = {}
    for (recipe, step, window, sensor), stats in base_stats.items():
        off = float(chamber_offsets.get(sensor, 0.0))
        out[gk_str(chamber, recipe, step, window, sensor)] = shift_snapshot_row(stats, off)
    return out


_DEACTIVATE = text(
    "UPDATE qual_snapshots SET is_active = false WHERE chamber_id = :ch AND is_active")
_INSERT = text(
    "INSERT INTO qual_snapshots (snapshot_id, chamber_id, qual_id, sensor_stats, is_active) "
    "VALUES (:sid, :ch, :qid, CAST(:stats AS JSONB), true)")


def write_sensor_stats(conn, chamber: str, sensor_stats: dict, *,
                       qual_id: str, snapshot_id: str) -> None:
    """이미 gk_str-keyed인 최종 sensor_stats를 qual_snapshots에 적재 — 기존 active 비활성 → 신규 active.

    챔버별 is_active 1개 불변식(앱 레벨). B6-3-b 리베이스(`rebase_snapshot`)는 실 챔버 데이터라
    offset shift 없이 이 함수로 직접 쓰고, 부트스트랩 시딩(`seed_snapshot`)은 offset shift 후 위임.
    commit=호출자(tttm_writer 패턴).
    """
    conn.execute(_DEACTIVATE, {"ch": chamber})
    conn.execute(_INSERT, {"sid": snapshot_id, "ch": chamber, "qid": qual_id,
                           "stats": json.dumps(sensor_stats)})
    logger.info("qual 스냅샷 적재: chamber=%s 그룹 %d개 (snapshot=%s)",
                chamber, len(sensor_stats), snapshot_id)


def seed_snapshot(conn, chamber: str, base_stats: dict, chamber_offsets: dict, *,
                  qual_id: str = QUAL_BOOTSTRAP_ID, snapshot_id: str) -> None:
    """챔버 1개 스냅샷 멱등 시딩 — base_stats를 offset shift 후 적재. commit=호출자.

    sensor_stats는 offset shift된 gk_str-keyed dict. 쓰기 자체는 `write_sensor_stats` 위임
    (리베이스와 write 경로 공유 — 단일 소스).
    """
    sensor_stats = build_sensor_stats(base_stats, chamber, chamber_offsets)
    write_sensor_stats(conn, chamber, sensor_stats, qual_id=qual_id, snapshot_id=snapshot_id)


def build_base_stats(cfg: LimitConfig, whitelist_df: pd.DataFrame, data_path) -> dict:
    """seg1 복귀블록에서 건강8종 감시 그룹의 **전체창** 스냅샷 통계(base_stats)를 만든다.

    표본·전처리는 initial_limits와 동일 소스(R3): determine_sample_wafers(seg1 복귀블록)
    → build_wafer_summaries(wafer-mean). σ는 전체창(rolling_n 클립 없음 — F12). **방식 B
    (2026-08-06) 이후 control_limits σ도 전체창**이라 σ는 동일하고 center만 다르다
    (Qual=median/full · control_limits=최근창). method는 whitelist(KEEP=sigma / KEEP*=quantile) 승계.
    반환 = `{(recipe, step, window, sensor): stats}` (원 챔버 C24_0 기준, offset shift 전).
    """
    df = il.load_data(Path(data_path))
    sample_wafers, _meta = il.determine_sample_wafers(df, cfg)
    df = df[df["C64"].isin(set(sample_wafers))]
    df = il.add_derived_features(df)
    summaries = il.build_wafer_summaries(df)

    monitored = whitelist_df[
        whitelist_df["sensor_id"].isin(HEALTH_SENSORS)
        & whitelist_df["decision"].isin(("KEEP", "KEEP*"))
    ]
    base_stats: dict = {}
    for r in monitored.itertuples(index=False):
        key = (r.recipe_id, int(r.step), r.sensor_window, r.sensor_id)
        g = summaries[
            (summaries["C6"] == r.recipe_id)
            & (summaries["C7"] == int(r.step))
            & (summaries["sensor_window"] == r.sensor_window)
            & (summaries["sensor_id"] == r.sensor_id)
        ]
        vals = g["value"].to_numpy()
        if len(vals) < cfg.min_trim_n:      # 소표본 스냅샷은 왜곡 위험 → 건너뜀(경고)
            logger.warning("스냅샷 건너뜀(표본 %d < %d): %s", len(vals), cfg.min_trim_n, key)
            continue
        base_stats[key] = snapshot_stats_for_group(vals, r.method, cfg)
    logger.info("base_stats 그룹 %d개 (건강8종 seg1 전체창)", len(base_stats))
    return base_stats


def main() -> None:
    """seg1 부트스트랩 스냅샷을 전 챔버에 시딩한다 (control_limits 시더와 대칭·멱등).

    사용법: python -m src.agent_b_spc.seed_qual_snapshots   # → qual_snapshots (챔버별 active 1개)
    챔버 목록·offset은 `chamber_offsets.json`에서 읽는다(하드코딩 금지 — 헌법 6-1).
    """
    import argparse
    from datetime import datetime, timezone

    from src.agent_b_spc.seed_control_limits import load_offsets, make_engine

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description="Qual seg1 부트스트랩 스냅샷 시딩")
    p.add_argument("--data", default=root / "Data" / "문제1(하)" / "train_data.csv")
    p.add_argument("--whitelist", type=Path,
                   default=Path(__file__).parent / "limits" / "monitoring_whitelist_v1.csv")
    p.add_argument("--offsets", type=Path, default=root / "config" / "chamber_offsets.json")
    p.add_argument("--database-url", default=None, help="미지정 시 .env의 DATABASE_URL 사용")
    args = p.parse_args()

    cfg = LimitConfig.load()
    whitelist_df = pd.read_csv(args.whitelist)
    offsets = load_offsets(args.offsets)                       # {chamber: {sensor: offset}}
    base_stats = build_base_stats(cfg, whitelist_df, args.data)
    if not base_stats:
        raise RuntimeError("base_stats 0건 — 시딩 중단 (표본/화이트리스트 확인)")

    engine = make_engine(args.database_url)
    now = datetime.now(timezone.utc)                           # 재실행마다 유니크(SEQ=시각) — 같은 날
    stamp, seq = now.strftime("%Y%m%d"), now.strftime("%H%M%S")  # 재시딩 시 snapshot_id 충돌 방지
    with engine.begin() as conn:                               # 전 챔버 원자 시딩
        for chamber, ch_off in offsets.items():
            sid = f"QUAL-{stamp}-{chamber.replace('_', '')}-{seq}"   # 헌법 ID: PREFIX-YYYYMMDD-CHAMBER-SEQ
            seed_snapshot(conn, chamber, base_stats, ch_off,
                          qual_id=QUAL_BOOTSTRAP_ID, snapshot_id=sid)
    logger.info("✅ Qual 부트스트랩 시딩 완료: 챔버 %d개 × 그룹 %d개",
                len(offsets), len(base_stats))


if __name__ == "__main__":
    main()
