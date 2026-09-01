# -*- coding: utf-8 -*-
"""CT² 학습셋 조립 배치 — 되감기 + 소급 필터 + 최소 표본 게이트 (배선 설계 v2 §1-②).

역할: ChamberRequalified 이후 "B 창 완전 포함 + firm 관리선 소급 필터"를 통과한
wafer trace CSV를 만든다 (AE 재학습 입력 — `Data/ae_repro/*.csv`와 동일 스키마).

⚠️ Kafka 경로 산출물은 `C10`·`C46` 이 없다(`_ts` 만 있음). retrain 쪽
`ae_pipeline.retrain.adapt_ct2_trainset` 이 `_ts→C10` 매핑 + `C46` 합성으로 흡수하며,
적용 내역은 번들 manifest `input_adaptation` 에 기록된다 (WP-1, 2026-07-30).
본 스크립트는 PM 소유라 스키마를 바꾸지 않고 어댑터를 retrain(A 소유)에 두었다.

경로 2종:
  --from-csv   trace CSV에서 조립 (재현·테스트·재보정 지원 — 샌드박스 실측 완료)
  --from-kafka fdc.raw 되감기 (설계 D9 — ⚠️ 실브로커 스모크 전까지 미실측 딱지)

소급 필터(D6 v1): wafer 내 어떤 행이든 (sensor, step) firm UCL/LCL 초과 → 그 wafer 제외.
한계 소스 = --limits-json (control_limits DB 연동은 컬럼 정본 확인 후 v2 — 정직한 한계).
게이트: 통과 wafer < min-wafers 면 exit 2 (전방 연장 적재 필요 신호 — 만료 규칙 D7).

사용 예:
  python scripts/ct2_assemble_trainset.py --from-csv Data/ae_repro/ae_normal_sim.csv \
      --chamber SIM_CH_1 --limits-json config/firm_limits_ch1.json --min-wafers 250 --out Data/ct2
헌법: 6-1(값은 params/CLI — 매직넘버 금지) · 6-2(파싱 실패 skip+카운트) · 1-3(C65 제거).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

WAFER_COL = "C64"      # ae_repro 스키마 정합 (dump_ae_input.py와 동일)
CHAMBER_COL = "C24"
STEP_COL = "C7"
TARGET_COL = "C65"     # 헌법 1-3 — 출력에서 무조건 제거


def _params_default_min_wafers() -> int | None:
    """config/params.yaml의 ct.ct2_min_train_wafers 읽기 (없으면 None — CLI 필수)."""
    try:
        import yaml
        with open(ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            return int(yaml.safe_load(f)["ct"]["ct2_min_train_wafers"])
    except Exception:
        return None


def load_limits(path: Path) -> list[dict]:
    """firm 한계 목록 로드. 형식: [{"sensor","step"(null=전 스텝),"lcl","ucl"}].

    ⚠️ `assert` 금지 (헌법 7장): `python -O` 플래그 하나로 방어선이 사라지고, 형식 깨진
    limits는 소급 필터가 **아무 wafer도 안 거르는** 최악의 모드(오염 학습셋 통과)가 된다.
    명시 `raise ValueError`로 방어한다. `json.loads` 성공 ≠ 리스트라 컨테이너 타입도 가드.
    """
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"limits 최상위가 리스트 아님: {type(rows).__name__} ({path})")
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or "sensor" not in r or not ("lcl" in r or "ucl" in r):
            raise ValueError(f"limits[{i}] 형식 오류 — sensor + (lcl|ucl) 필수: {r!r}")
    return rows


def rows_from_csv(csv: Path, chamber: str) -> pd.DataFrame:
    """trace CSV → 대상 챔버 행 (ae_repro 스키마)."""
    df = pd.read_csv(csv)
    if CHAMBER_COL in df.columns:
        df = df[df[CHAMBER_COL] == chamber]
    return df


def _utc(s: str) -> datetime:
    """ISO 문자열 → tz-aware UTC datetime.

    naive 입력은 **UTC로 간주**한다 (로컬시간 오해석 방지). 오케스트레이터는 UTC ISO를
    넘기지만 CLI 수동 호출은 naive가 흔하고, naive면 `.timestamp()`가 로컬시간으로 해석돼
    되감기 시작점이 어긋난다. 또 naive `until`과 aware 메시지 ts를 비교하면 `TypeError`가
    나는데, 그게 되감기 루프의 `except`에 삼켜져 "대상 행 0 — 챔버/창 확인"으로 **오진**된다
    (헌법 7장 `pm_log.json` tz 사고와 같은 계열). 세 시각을 전부 이 함수로 정규화한다.
    """
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def rows_from_kafka(bootstrap: str, topic: str, chamber: str,
                    since_iso: str, until_iso: str, timeout_s: float) -> pd.DataFrame:
    """fdc.raw 되감기 (D9). ⚠️ 미실측 — 실브로커 스모크 후 사용.

    offsets_for_times(since)로 시작점 seek → until까지 소비 → 메시지를 trace 행으로 복원
    (sensors dict → 컬럼, wafer_id→C64, chamber_id→C24, step→C7).
    """
    from confluent_kafka import Consumer, TopicPartition  # 지연 import — CSV 경로 무의존

    since_ms = int(_utc(since_iso).timestamp() * 1000)       # WP-B6 — naive→UTC 정규화
    until_dt = _utc(until_iso)
    c = Consumer({"bootstrap.servers": bootstrap,
                  "group.id": f"ct2-assemble-{int(datetime.now().timestamp())}",
                  "enable.auto.commit": False, "auto.offset.reset": "earliest"})
    md = c.list_topics(topic, timeout=10).topics[topic]
    parts = [TopicPartition(topic, p, since_ms) for p in md.partitions]
    c.assign(c.offsets_for_times(parts, timeout=10))

    rows, bad = [], 0
    while True:
        msg = c.poll(timeout_s)
        if msg is None:
            break                                    # 되감기 소진
        if msg.error():
            bad += 1
            continue
        try:
            m = json.loads(msg.value())
            if m.get("chamber_id") != chamber:
                continue
            ts = m.get("timestamp", "")
            if ts and _utc(ts) > until_dt:               # 둘 다 aware — TypeError 불가 (WP-B6)
                break
            row = {WAFER_COL: m["wafer_id"], CHAMBER_COL: m["chamber_id"],
                   STEP_COL: m.get("step"), "C41": m.get("elapsed_in_step"),
                   "C42": m.get("stabilization_flag"), "C20": m.get("lot_id"),
                   "_ts": ts}
            row.update(m.get("sensors") or {})
            rows.append(row)
        except Exception:
            bad += 1                                  # 6-2 — skip + 카운트
    c.close()
    df = pd.DataFrame(rows)
    df.attrs["parse_skipped"] = bad
    return df


def retro_filter(df: pd.DataFrame, limits: list[dict]) -> tuple[set, dict]:
    """소급 필터(D6 v1) — firm 한계 초과 wafer 집합과 사유 반환."""
    excluded: dict[str, list[str]] = {}
    for lim in limits:
        col = lim["sensor"]
        if col not in df.columns:
            continue
        sub = df if lim.get("step") is None else df[df[STEP_COL] == lim["step"]]
        v = pd.to_numeric(sub[col], errors="coerce")
        mask = pd.Series(False, index=sub.index)
        if lim.get("ucl") is not None:
            mask |= v > float(lim["ucl"])
        if lim.get("lcl") is not None:
            mask |= v < float(lim["lcl"])
        for w in sub.loc[mask, WAFER_COL].unique():
            excluded.setdefault(str(w), []).append(
                f"{col}@step{lim.get('step', '*')}")
    return set(excluded), excluded


def main() -> None:
    ap = argparse.ArgumentParser(description="CT2 학습셋 조립 (되감기+소급 필터+게이트)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-csv", type=Path)
    src.add_argument("--from-kafka", action="store_true")
    ap.add_argument("--chamber", required=True)
    ap.add_argument("--limits-json", type=Path, required=True,
                    help="firm 한계 [{sensor,step|null,lcl,ucl}] — 소급 필터 기준")
    ap.add_argument("--min-wafers", type=int, default=_params_default_min_wafers(),
                    help="게이트 (기본 = params ct.ct2_min_train_wafers)")
    ap.add_argument("--out", type=Path, default=ROOT / "Data" / "ct2")
    ap.add_argument("--bootstrap", default=None, help="kafka bootstrap (--from-kafka)")
    ap.add_argument("--topic", default="fdc.raw")
    ap.add_argument("--since", help="되감기 시작 ISO (B 창 시작 근사 — D10)")
    ap.add_argument("--until", help="되감기 종료 ISO (requalified_at)")
    ap.add_argument("--poll-timeout", type=float, default=5.0)
    a = ap.parse_args()
    if a.min_wafers is None:
        ap.error("--min-wafers 필요 (params ct.ct2_min_train_wafers 미등재 상태)")

    if a.from_csv:
        df, source = rows_from_csv(a.from_csv, a.chamber), str(a.from_csv)
    else:
        for k in ("bootstrap", "since", "until"):
            if not getattr(a, k):
                ap.error(f"--from-kafka 는 --{k} 필요")
        df, source = rows_from_kafka(a.bootstrap, a.topic, a.chamber,
                                     a.since, a.until, a.poll_timeout), f"kafka:{a.topic}"
    if df.empty:
        print("[FAIL] 대상 행 0 — 챔버/창 확인"); sys.exit(1)

    limits = load_limits(a.limits_json)
    bad_wafers, reasons = retro_filter(df, limits)
    keep = df[~df[WAFER_COL].astype(str).isin(bad_wafers)].copy()
    if TARGET_COL in keep.columns:
        keep = keep.drop(columns=[TARGET_COL])       # 1-3
    n_keep = keep[WAFER_COL].nunique()
    n_all = df[WAFER_COL].nunique()

    a.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = a.out / f"ct2_trainset_{a.chamber}_{stamp}"
    keep.to_csv(f"{stem}.csv", index=False)
    gate_ok = n_keep >= a.min_wafers
    meta = {"source": source, "chamber": a.chamber,
            "wafers_seen": n_all, "wafers_kept": n_keep,
            "wafers_excluded": len(bad_wafers), "exclude_reasons": reasons,
            "parse_skipped": int(df.attrs.get("parse_skipped", 0)),
            "min_wafers_gate": a.min_wafers, "gate_pass": gate_ok,
            "limits_file": str(a.limits_json), "limits_count": len(limits),
            "created_utc": stamp,
            "note": "gate_pass=false 면 firm 이후 전방 연장 적재 (만료 = 다음 요란 PM — 설계 D7)"}
    Path(f"{stem}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{'OK' if gate_ok else 'SHORT'}] kept {n_keep}/{n_all} wafers "
          f"(excluded {len(bad_wafers)}) -> {stem}.csv")
    sys.exit(0 if gate_ok else 2)


if __name__ == "__main__":
    main()
