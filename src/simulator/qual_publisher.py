# -*- coding: utf-8 -*-
"""QUAL 발행부 — P5-3 선행분 (2026-07-10 주말 선행, 사이클_정의 'Qual 실행 스펙' 준수).

동작: 챔버 개방(PM — C33 리셋 감지) 직후, 생산 재개 전에 `is_qual=true` wafer
C1(=5)장을 fdc.raw로 발행한다 (소정비 포함 — 모든 개방 후).

고정 표준 조건 (R7):
  - recipe_id = 표준 레시피 "C6_0" 강제
  - lot_id = "C20_QUAL" (배치 영향 배제 — σ-갭 판정이 배치 잡음에 오염되지 않도록)
  - 센서 트레이스 = "현재 챔버 상태의 리샘플": 트리거 시점 wafer(새 레짐의 첫 wafer)를
    템플릿으로 챔버 오프셋+노이즈 재추첨 — 무에서 창조가 아니라 실측 앵커 유지.
    주입(injector) 델타도 현 시점 그대로 반영 → 잔류 이상이 있으면 Qual이 잡는다(qual_fail 재료)
  - wafer_id = "QUAL_<YYYYMMDD>_<n>_CH_X" — 계약 §1 접미사 규칙 유지, is_qual이 판별 필드

원칙:
  - Qual wafer는 fdc.actual 미발행 (WT 미경유 — 계약 1-B, ActualPublisher가 is_qual 스킵)
  - 소비는 일반 wafer와 동일 파이프라인 (Qual 전용 코드 경로 없음 — 기획서 원칙)
  - 정착 구간만 추릴지(vs 풀 트레이스)는 P5-3 본대에서 소비자(A·B)와 확정 —
    선행분은 풀 트레이스(일반 wafer와 동일 구조)로 발행해 소비자 파손 리스크 최소화

dry-run:
    python -m src.simulator.qual_publisher --dry-run              # 강제 트리거 + 스트림 트리거 검증
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .chamber_replicator import ChamberReplicator
from .message_builder import (DEFAULT_CSV_PATH, build_message, load_params,
                              validate_contract)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("qual-publisher")

QUAL_RECIPE_ID = "C6_0"     # R7 표준 레시피
QUAL_LOT_ID = "C20_QUAL"    # 배치 영향 배제 표지
DRYRUN_STREAM_LIMIT = 4200  # 스트림 트리거 검증용 wafer 수 (실데이터 리셋 지점 통과)


class QualPublisher:
    """PM 리셋 감지 → 트리거 wafer를 템플릿으로 Qual wafer C1장 생성.

    트리거 판정과 생성이 한 클래스에 있어 producer와 dry-run이 같은 로직을 쓴다.
    """

    def __init__(self, replicator: ChamberReplicator, injector,
                 params: dict, enabled: bool = True):
        self.replicator = replicator
        self.injector = injector
        self.qual_count = params["qual"]["wafer_count"]  # C1
        self.enabled = enabled
        self._last_pm: dict[str, int] = {}
        self._emitted = 0

    def build_qual_wafers(self, chamber_id: str, template: pd.DataFrame,
                          ch_wafer_idx: int, pm_count: int) -> list[dict]:
        """템플릿 wafer 1장 → Qual wafer C1장(row 메시지 리스트) 생성."""
        msgs: list[dict] = []
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        suffix = chamber_id.replace("SIM_", "")
        for n in range(1, self.qual_count + 1):
            self._emitted += 1
            wafer_id = f"QUAL_{date}_{self._emitted}_{suffix}"
            overrides = {
                "is_qual": True,
                "recipe_id": QUAL_RECIPE_ID,
                "lot_id": QUAL_LOT_ID,
                "slot_no": n,
            }
            step_counter: dict = {}
            for _, row in template.iterrows():
                step = int(row["C7"]) if pd.notna(row["C7"]) else -1
                step_counter[step] = step_counter.get(step, 0) + 1
                # perturb가 매 호출 새 노이즈를 뽑으므로 5장이 서로 다른 트레이스가 됨
                msgs.append(build_message(chamber_id, row, step_counter[step],
                                          pm_count, self.replicator, self.injector,
                                          ch_wafer_idx, wafer_id, overrides=overrides))
        return msgs

    def on_wafer(self, chamber_id: str, wafer: pd.DataFrame,
                 ch_wafer_idx: int, pm_count: int) -> list[dict]:
        """스트림 훅 — PM 리셋(카운터 증가) 감지 시 Qual 메시지 리스트 반환, 아니면 [].

        producer는 반환 메시지를 해당 wafer '이전에' 발행한다 (개방 직후·생산 재개 전).
        """
        if not self.enabled:
            return []
        last = self._last_pm.get(chamber_id)
        self._last_pm[chamber_id] = pm_count
        if last is None or pm_count <= last:
            return []
        log.info(f"🧪 {chamber_id} 개방 감지 (pm_count {last}→{pm_count}) — "
                 f"Qual {self.qual_count}장 발행 (생산 재개 전)")
        return self.build_qual_wafers(chamber_id, wafer, ch_wafer_idx, pm_count)


# ---------------------------------------------------------------------------
# dry-run — QUAL 발행부 자체 검증
# ---------------------------------------------------------------------------

def _wafer_ids(msgs: list[dict]) -> list[str]:
    """메시지 리스트에서 wafer_id 등장 순서 보존 고유 목록."""
    seen: list[str] = []
    for m in msgs:
        if m["wafer_id"] not in seen:
            seen.append(m["wafer_id"])
    return seen


def run_dry(args) -> int:
    """강제 트리거(Q1~Q5) + 스트림 트리거(Q6) 검증. FAIL 수 반환."""
    params = load_params()
    sim_cfg = params["simulator"]

    log.info(f"📂 CSV 로딩: {args.csv}")
    df = pd.read_csv(args.csv)
    replicator = ChamberReplicator(df, sim_cfg["chamber_count"],
                                   sim_cfg["chamber_offset_sigma"],
                                   noise_sigma=sim_cfg["chamber_noise_sigma"])
    qual = QualPublisher(replicator, injector=None, params=params)
    results = []

    # ---- 강제 트리거: 임의 wafer를 템플릿으로 생성 --------------------------
    template = replicator._wafer_groups[100]
    ch = "SIM_CH_3"
    msgs = qual.build_qual_wafers(ch, template, ch_wafer_idx=101, pm_count=1)
    ids = _wafer_ids(msgs)

    # Q1: C1장 + 전건 is_qual
    q1 = len(ids) == qual.qual_count and all(m["is_qual"] for m in msgs)
    results.append((f"Q1 Qual {qual.qual_count}장 (C1)·전 row is_qual=true", q1,
                    f"{len(ids)}장 / is_qual 전건={all(m['is_qual'] for m in msgs)}"))

    # Q2: 고정 표준 조건 (R7) + wafer_id 규칙
    q2 = (all(m["recipe_id"] == QUAL_RECIPE_ID for m in msgs)
          and all(m["lot_id"] == QUAL_LOT_ID for m in msgs)
          and all(i.startswith("QUAL_") and i.endswith("CH_3") for i in ids))
    results.append(("Q2 고정 표준 조건 (recipe C6_0·배치 배제·ID 규칙)", q2,
                    f"recipe/lot 강제 + id 예: {ids[0]}"))

    # Q3: 계약 §1 정합 (일반 wafer와 동일 스키마)
    probs = validate_contract(msgs[0])
    results.append(("Q3 계약 §1 정합 (동일 파이프라인 소비 가능)", not probs,
                    "; ".join(probs) if probs else "위반 0건"))

    # Q4: 리샘플 앵커 — 템플릿 wafer 평균 대비 |편차| ≤ 0.3σ (노이즈 재추첨만)
    q4_msgs, q4 = [], True
    for s in ("C11", "C17"):
        std = replicator._std.get(s)
        if not std:
            continue
        tpl_rows = [replicator.offsets[ch][s] + float(v)
                    for v in template[s] if pd.notna(v)]
        tpl_mean = sum(tpl_rows) / len(tpl_rows)
        vals = [m["sensors"][s] for m in msgs if m["sensors"].get(s) is not None]
        dev = abs(sum(vals) / len(vals) - tpl_mean) / std
        q4 &= dev <= 0.3
        q4_msgs.append(f"{s}: 편차 {dev:.3f}σ")
    results.append(("Q4 리샘플 앵커 (현재 상태 ±0.3σ — 무에서 창조 아님)", q4,
                    " / ".join(q4_msgs)))

    # Q5: fdc.actual 미발행 — is_qual 스킵 가드
    from .actual_publisher import ActualPublisher
    ap = ActualPublisher(producer=None, label_delay_wafers=10, enabled=True)
    ap.on_wafer_complete("QUAL_X_1_CH_3", QUAL_LOT_ID, ch, 999.0, 1, is_qual=True)
    results.append(("Q5 Qual은 fdc.actual 미발행 (WT 미경유 — 계약 1-B)",
                    len(ap._queue) == 0, f"큐 적재 {len(ap._queue)}건 (기대 0)"))

    # ---- 스트림 트리거: 실데이터 리셋 지점 통과하며 자동 발행 확인 ------------
    qual2 = QualPublisher(replicator, injector=None, params=params)
    triggers = 0
    qual_msgs = 0
    total = 0
    for chamber_id, wafer, ch_idx, pm_count in replicator.stream_wafers(loop=False):
        if total >= args.limit:
            break
        out = qual2.on_wafer(chamber_id, wafer, ch_idx, pm_count)
        if out:
            triggers += 1
            qual_msgs += len(_wafer_ids(out))
        total += 1
        if total % 1000 == 0:
            log.info(f"… 스트림 {total:,}/{args.limit:,} (트리거 {triggers}건)")
    expected_triggers = sim_cfg["chamber_count"]  # 실데이터 리셋 1회 × 챔버 수(E1=4) 복제
    q6 = triggers == expected_triggers and qual_msgs == triggers * qual.qual_count
    results.append((f"Q6 스트림 트리거 (개방 감지 → 자동 발행, 기대 {expected_triggers}건)",
                    q6, f"트리거 {triggers}건 / Qual wafer {qual_msgs}장 "
                        f"(기대 {expected_triggers * qual.qual_count}장)"))

    fails = sum(1 for _, ok, _ in results if not ok)
    print("\n" + "=" * 78)
    print("QUAL 발행부 DRY-RUN 리포트 (P5-3 선행분 — 사이클_정의 Qual 실행 스펙)")
    print("=" * 78)
    for name, ok, detail in results:
        print(f"  {'PASS ✅' if ok else 'FAIL ❌':8} │ {name}\n           │   {detail}")
    print("-" * 78)
    print(f"  결과: {len(results) - fails}/{len(results)} PASS"
          + ("  →  QUAL 발행부 검증 통과 🎉" if fails == 0 else "  →  FAIL — 로그 확인"))
    print("=" * 78)
    return fails


def main():
    """CLI 진입점 (dry-run 전용 — 실발행은 kafka_producer에 통합)."""
    p = argparse.ArgumentParser(description="QUAL 발행부 dry-run (P5-3 선행)")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    p.add_argument("--limit", type=int, default=DRYRUN_STREAM_LIMIT,
                   help="스트림 트리거 검증용 wafer 수")
    p.add_argument("--dry-run", action="store_true", default=True)
    args = p.parse_args()
    if not args.csv.exists():
        log.error(f"❌ CSV 없음: {args.csv}")
        sys.exit(2)
    sys.exit(1 if run_dry(args) else 0)


if __name__ == "__main__":
    main()
