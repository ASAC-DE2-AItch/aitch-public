# -*- coding: utf-8 -*-
"""챔버 오프셋 테이블 덤프 — B 초기 실력치 챔버별 시딩용 (2026-07-10 확정 ⓐ안).

ChamberReplicator와 동일한 seed(42)로 오프셋을 재현해 config/chamber_offsets.json으로
저장한다. B는 B2-1 원본 baseline에 이 오프셋을 센서별로 shift하여 챔버별 초기
실력치를 시딩한다 (seg1 실측 시딩 원칙 R3 정합).

사용법:
    python -m src.simulator.dump_offsets            # → config/chamber_offsets.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from .chamber_replicator import ChamberReplicator
from .message_builder import DEFAULT_CSV_PATH, ROOT, load_params

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dump-offsets")

DEFAULT_OUT = ROOT / "config" / "chamber_offsets.json"


def main():
    """CSV 로드 → replicator 오프셋 재현 → JSON 덤프."""
    p = argparse.ArgumentParser(description="챔버 오프셋 테이블 덤프 (B 시딩용)")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    if not args.csv.exists():
        log.error(f"❌ CSV 없음: {args.csv}")
        sys.exit(2)

    params = load_params()
    sim_cfg = params["simulator"]
    log.info(f"📂 CSV 로딩: {args.csv}")
    df = pd.read_csv(args.csv)
    replicator = ChamberReplicator(df, sim_cfg["chamber_count"],
                                   sim_cfg["chamber_offset_sigma"],
                                   noise_sigma=sim_cfg["chamber_noise_sigma"])

    payload = {
        "generated_for": "B 초기 실력치 챔버별 시딩 (baseline shift)",
        "seed": 42,
        "offset_sigma": sim_cfg["chamber_offset_sigma"],
        "note": "값 = 해당 센서 baseline에 더할 절대 오프셋 (원본 std × U(-σ,σ)). "
                "노이즈(행 단위)는 관리선에 반영하지 않음. seed·E1·E3 변경 시 재생성 필수.",
        "sensor_std": {k: round(v, 6) for k, v in replicator._std.items()},
        "offsets": {ch: {s: round(v, 6) for s, v in m.items()}
                    for ch, m in replicator.offsets.items()},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"✅ 저장: {args.out} (챔버 {len(payload['offsets'])}대 × "
             f"센서 {len(payload['sensor_std'])}종)")


if __name__ == "__main__":
    main()
