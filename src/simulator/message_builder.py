# -*- coding: utf-8 -*-
"""fdc.raw 메시지 조립 — Kafka 의존 없는 공용 모듈 (P3-1).

kafka_producer(실발행)와 dry_run(자체 검증)이 같은 조립 로직을 쓰도록 분리.
스키마 단일 소스: docs/API_Contract_명세서.md §1 (v4.4/4.5).
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

log = logging.getLogger("message-builder")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV_PATH = ROOT / "Data" / "문제1(하)" / "train_data.csv"
PARAMS_PATH = ROOT / "config" / "params.yaml"

# 최상위 필드로 승격되어 sensors에서 제외되는 컬럼
TOP_LEVEL_COLUMNS = {"C64", "C20", "C34", "C6", "C7", "C41", "C42"}

# sensors 제외 대상 (API Contract §0 — 전체 NULL·상수·중복·타겟)
DROP_COLUMNS = {
    # 전체 NULL (8)
    "C2", "C13", "C26", "C37", "C43", "C47", "C53", "C55",
    # 상수 (12)
    "C3", "C8", "C14", "C19", "C21", "C24", "C28", "C29", "C30", "C44", "C45", "C51",
    # 중복·파생 원본·시간 원본
    "C35", "C36", "C38", "C40", "C10", "C39", "C22", "C23",
    # Target — fdc.raw에 절대 미포함 (헌법 1-3). 실측은 fdc.actual 채널로만
    "C65",
}

# fdc.raw 필수 최상위 필드 (계약 §1 — 검증용)
CONTRACT_REQUIRED_FIELDS = {
    "wafer_id", "lot_id", "slot_no", "chamber_id", "recipe_id", "step",
    "seq_in_step", "elapsed_in_step", "stabilization_flag", "pm_count",
    "is_qual", "timestamp", "sensors",
}


def load_params() -> dict:
    """config/params.yaml 로드 — 없으면 명시적 에러 (조용한 기본값 금지)."""
    if not PARAMS_PATH.exists():
        raise FileNotFoundError(f"config/params.yaml 없음: {PARAMS_PATH}")
    with open(PARAMS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# PM 리셋 판정 — C33(RF time)이 직전 대비 이 비율 미만으로 떨어지면 리셋 (ChamberReplicator와 동일 상수)
_RESET_DROP_RATIO = 0.5


def _wafer_time_order(df: pd.DataFrame) -> list:
    """wafer id를 **시간순**으로 — 리셋 경계 판정의 축 (B 리뷰 #62 반영 2026-07-30).

    raw CSV 행 순서는 시간순이 아니다(실측: wafer 대표시각 역전 4,946건). 현 시딩
    데이터에선 리셋점·seg1 집합이 우연히 일치했지만(양쪽 8,247장), 다른 데이터·셔플에서는
    경계가 어긋난다. 그래서 판정은 C10(타임스탬프) 기준으로 세우고, **발행 행 순서는
    원본 그대로 둔다**(filter_post_reset — 스트림 순서 변경은 Nelson 추세 룰에 영향).
    C10이 없거나 전부 결측이면 raw 순서로 폴백한다.
    """
    if "C10" not in df.columns:
        return list(df.groupby("C64", sort=False).groups.keys())
    t0 = df.groupby("C64", sort=False)["C10"].min()
    if t0.isna().all():
        return list(t0.index)
    return list(t0.sort_values(kind="stable").index)


def last_reset_wafer_index(df: pd.DataFrame) -> int:
    """마지막 major PM 리셋 직후 wafer의 전역 순번(0-based) — 데이터 주도(C33 급락 감지).

    C17 등 레짐 운반 센서는 리셋 전(seg0)/후(seg1)가 다른 모집단이라, seg0을 발행하면
    seg1 시딩 관리선 대비 대편차 오탐(C17 +52.7σ 실측 2026-07-27). 시딩 소스가 seg1
    (settled_after_reset·R3)이므로 발행도 seg1로 맞춘다 (팀원 B님 진단·PM 결정 2026-07-28).
    리셋 없으면 0(전체 사용). 매직넘버 없음 — C33 궤적만으로 경계 산출.
    """
    first_c33 = df.groupby("C64", sort=False)["C33"].first()
    first_c33 = first_c33.reindex(_wafer_time_order(df)).to_numpy(dtype=float)
    reset_at = 0
    for i in range(1, len(first_c33)):
        prev, cur = first_c33[i - 1], first_c33[i]
        if pd.notna(prev) and pd.notna(cur) and prev > 0 and cur < prev * _RESET_DROP_RATIO:
            reset_at = i                          # 마지막 리셋 직후 wafer 순번 갱신
    return reset_at


def filter_post_reset(df: pd.DataFrame) -> pd.DataFrame:
    """마지막 리셋 이후(seg1) wafer만 남긴 df — 원본 시간순·행 구조 보존.

    wafer 블록 단위로 슬라이스(행 순서 불변). 리셋 없으면 원본 그대로.
    """
    order = _wafer_time_order(df)
    k = last_reset_wafer_index(df)
    if k <= 0:
        return df
    keep = set(order[k:])                    # 경계 판정은 시간축, 멤버십으로 거른다
    out = df[df["C64"].isin(keep)]           # **원본 행 순서 보존** (블록 구조·발행 순서 불변)
    log.info(f"replay seg1 필터: 리셋 후 wafer {len(keep):,}장 "
             f"(전체 {len(order):,} 중 seg0 {k:,} 제외)")
    return out


def build_message(chamber_id: str, row: pd.Series, seq_in_step: int,
                  pm_count: int, replicator, injector,
                  chamber_wafer_index: int, wafer_id: str,
                  overrides: dict | None = None,
                  recipe_applier=None) -> dict:
    """CSV Row 1건 → API Contract v4.4/4.5 `fdc.raw` JSON dict.

    overrides: 최상위 필드 덮어쓰기 (QUAL 모드 — is_qual·recipe_id·lot_id 등.
    스키마 밖 필드 추가 금지 — 계약 §1 준수는 호출자 책임, validate_contract로 검증).
    recipe_applier: 승인된 Recipe 보정(setpoint 이동) 적용기 — 주입(injector) 뒤에 적용.
    """
    msg = {
        "wafer_id": wafer_id,
        "lot_id": str(row["C20"]) if pd.notna(row.get("C20")) else None,
        "slot_no": int(row["C34"]) if pd.notna(row.get("C34")) else None,
        "chamber_id": chamber_id,
        "recipe_id": str(row["C6"]) if pd.notna(row.get("C6")) else None,
        "step": int(row["C7"]) if pd.notna(row.get("C7")) else None,
        "seq_in_step": seq_in_step,
        "elapsed_in_step": float(row["C41"]) if pd.notna(row.get("C41")) else None,
        "stabilization_flag": int(row["C42"]) if pd.notna(row.get("C42")) else None,
        "pm_count": pm_count,
        "is_qual": False,  # QUAL 모드는 P5-3에서 추가
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }

    sensors: dict = {}
    excluded = TOP_LEVEL_COLUMNS | DROP_COLUMNS
    for col in row.index:
        if col in excluded or col == "C33":
            continue
        val = row[col]
        if pd.isna(val):
            sensors[col] = None
            continue
        if isinstance(val, (int, float)):
            v = replicator.perturb(chamber_id, col, float(val),
                                   recipe=msg["recipe_id"], step=msg["step"],
                                   c42=msg["stabilization_flag"])
            if injector is not None:
                v += injector.delta(chamber_id, col, chamber_wafer_index,
                                    transient=(msg["stabilization_flag"] == 1),
                                    recipe=msg["recipe_id"], step=msg["step"],
                                    c42=msg["stabilization_flag"])
            if recipe_applier is not None:
                v = recipe_applier.apply(chamber_id, msg["recipe_id"], msg["step"],
                                         col, v)
            sensors[col] = round(v, 4)
        else:
            sensors[col] = val
    # C33(RF time)은 오프셋·노이즈 없이 원본 그대로 — B의 PM 감지 입력.
    # 가상 PM(pm_reset) 활성 시에는 1부터 재시작하도록 리베이스 (P5-3).
    if pd.notna(row.get("C33")):
        c33v = float(row["C33"])
        if injector is not None:
            c33v = injector.c33_rebase(chamber_id, chamber_wafer_index, c33v)
        sensors["C33"] = c33v
    msg["sensors"] = sensors
    if overrides:
        msg.update(overrides)
    return msg


def validate_contract(msg: dict) -> list[str]:
    """메시지 1건의 계약 위반 목록 반환 (없으면 빈 리스트) — dry-run 검증용."""
    problems = []
    missing = CONTRACT_REQUIRED_FIELDS - set(msg)
    if missing:
        problems.append(f"필수 필드 누락: {sorted(missing)}")
    if "C65" in msg.get("sensors", {}):
        problems.append("C65가 sensors에 포함 — 누수 (헌법 1-3 위반)")
    if "C33" not in msg.get("sensors", {}):
        problems.append("C33(RF time) 누락 — B의 PM 감지 입력")
    ch = msg.get("chamber_id", "")
    if not (isinstance(ch, str) and ch.startswith("SIM_CH_")):
        problems.append(f"chamber_id 규칙 위반: {ch} (SIM_CH_N — v4.2 잔재 금지)")
    wid = msg.get("wafer_id", "")
    if not wid.endswith(ch.replace("SIM_", "")):
        problems.append(f"wafer_id 접미사 규칙 위반: {wid} (예: C64_516_CH_3)")
    return problems
