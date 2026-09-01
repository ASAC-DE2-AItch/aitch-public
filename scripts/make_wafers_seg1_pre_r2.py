# -*- coding: utf-8 -*-
"""CT² r2 학습 wafer 목록 생성 — seg1 PM전 구간 + 전이 완충 제외.

배경 (A 통합보고서 2026-08-05 §5-1): r1(ae_cand_seg1pre_20260805)의 게이트 FAIL 5종은
모델 결함이 아니라 **게이트 홀드아웃 끝이 PM 경계에 붙어 전이 구간(#871~922)이 들어간**
분할 설계에서 나왔다. 포화 시작 실측 = CH4 #871 / CH1~3 #877 (C17이 PM 46~52장 전에
이미 seg2 레짐 레벨 −4.7σ 도달). r2는 그 꼬리를 **완충(BUFFER)으로 통째로 제외**한다.

산출: Data/ae_repro/wafers_seg1_pre_r2.txt — 챔버당 인덱스 0..(경계−BUFFER−1),
retrain --wafers-file 형식(한 줄당 wafer_id 1개).

⚠️ PM 대행 실행 (A 부재 2026-08-05~06) — src/agent_a_mlops 소유권상 **A 리뷰 필수** (헌법 3-1).
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "Data" / "ae_repro" / "ae_input_sim_postguard_full.csv"

# 사용: python make_wafers_seg1_pre_r2.py [BUFFER] [접미]  (기본 52, _r2)
BUFFER = int(sys.argv[1]) if len(sys.argv) > 1 else 52   # 전이 완충 — r2=52(실측 #871 기준), r3=80(B1 잔여 배회 겨냥)
SUFFIX = sys.argv[2] if len(sys.argv) > 2 else "_r2"
OUT = ROOT / "Data" / "ae_repro" / f"wafers_seg1_pre{SUFFIX}.txt"
EXPECT_BOUNDARY = 923  # A 보고서 실측 (챔버당 PM전 923 / PM후 2,061) — 어긋나면 중단


def main() -> None:
    """챔버별 시간순 wafer에서 C33 리셋 경계 탐지 → 완충 제외 목록 생성."""
    df = pd.read_csv(DATA, usecols=["C64", "C24", "C33", "C10"], low_memory=False)
    w = (df.groupby("C64", sort=False)
           .agg(ch=("C24", "first"), c33=("C33", "first"), t=("C10", "min"))
           .reset_index())
    keep: list[str] = []
    for ch, g in w.groupby("ch"):
        g = g.sort_values("t", kind="mergesort").reset_index(drop=True)
        reset = g.index[g["c33"].diff() <= -2].tolist()
        if not reset:
            raise SystemExit(f"[중단] {ch}: C33 리셋 경계를 찾지 못함")
        b = int(reset[0])
        if b != EXPECT_BOUNDARY:
            raise SystemExit(f"[중단] {ch}: 경계 {b} ≠ 기대 {EXPECT_BOUNDARY} — A 보고서와 불일치, 재확인 필요")
        cut = b - BUFFER                       # 0..cut-1 유지 (= 0..870)
        keep.extend(g.loc[: cut - 1, "C64"].tolist())
        print(f"{ch}: 전체 {len(g):,} · 경계 #{b} · 완충 {BUFFER} 제외 → 채택 {cut} (0..{cut-1})")
    OUT.write_text("\n".join(keep) + "\n", encoding="utf-8")
    print(f"\n→ {OUT.name}: wafer {len(keep):,}장 (기대 3,484 = 871×4)")


if __name__ == "__main__":
    main()
