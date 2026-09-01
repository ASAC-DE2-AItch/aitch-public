# -*- coding: utf-8 -*-
"""AE 서빙 포화 진단 — 오프라인(덤프) vs 라이브(kafka) 동일 wafer 대조.

배경 (2026-08-06 04:0x): e4 번들이 게이트 16/16 PASS인데 라이브 서빙에서
학습 구간 wafer(C64_9665 등)가 ae 0.68~1.0. 코드 대조로는 서빙·게이트가
같은 계약(prepare→features→scorer→calib)이라 **입력 값 실측 대조**로 확정한다.

  mode=offline : 덤프 CSV에서 지정 wafer들을 e4로 직접 스코어
                 → 낮으면 번들 무죄 + "라이브 입력이 다르다" 확정
  mode=live    : fdc.raw를 새 그룹(diag-ae-tail)으로 tail → 완성 wafer를
                 consumer.rows_to_trace_ae와 동일 변환 → 스코어
                 + 같은 wafer의 덤프 피처와 diff Top12 출력 (범인 채널 지목)

실행 (반드시 _ae_lab에서 — 실험실 사본의 ae_pipeline 사용):
  cd <레포 루트>\\_ae_lab
  python ..\\scripts\\diag_ae_serving.py offline
  python ..\\scripts\\diag_ae_serving.py live

⚠️ PM 대행 진단 (A 부재) — src/agent_a_mlops 소유권상 A 리뷰 필수 (헌법 3-1).
   읽기 전용: 번들·DB·오프셋 어떤 상태도 변경하지 않는다. 신규 컨슈머 그룹만 생성.
"""
import sys
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.WARNING)          # ae 로그 소음 억제

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "models" / "anomaly_ae" / "ae_cand_seg1pre_e4_20260806"
DUMP = ROOT / "Data" / "ae_repro" / "ae_input_sim_postguard_full.csv"
HOT = ["C64_9665_CH_1", "C64_9540_CH_2", "C64_10225_CH_2",
       "C64_10179_CH_2", "C64_10376_CH_2", "C64_10383_CH_3"]

sys.path.insert(0, str(Path.cwd()))                  # _ae_lab → ae_pipeline
from ae_pipeline.infer import AEModel                # noqa: E402
from ae_pipeline.features import prepare_frame, extract_features  # noqa: E402


def load_bundle() -> AEModel:
    """e4 번들 로드 (읽기 전용)."""
    m = AEModel.from_bundle(str(BUNDLE))
    print(f"번들: {BUNDLE.name} (model={m.model_version})")
    return m


def offline() -> None:
    """덤프 CSV에서 HOT wafer들을 직접 스코어 — 번들 무죄/유죄 판정."""
    m = load_bundle()
    df = pd.read_csv(DUMP, low_memory=False)
    sub = df[df["C64"].isin(HOT)]
    print(f"덤프 추출: {sub['C64'].nunique()} wafer / {len(sub)} rows")
    recs = m.score_wafers(sub, chamber_col="C24", with_drift=False)
    print(f"{'wafer':22s} {'ae_raw':>9s} {'ae_score':>9s}")
    for r in recs:
        print(f"{r['wafer']:22s} {r['ae_raw']:9.1f} {r['ae_score']:9.4f}")
    print("\n판정: score 0.0x대 = 번들 무죄 → live 모드로 입력 diff 진행 /"
          " 0.5+ = 번들·스코어러 자체 재검")


def _rows_to_trace_ae(wafer_id: str, chamber: str, rows: list) -> pd.DataFrame:
    """consumer.rows_to_trace_ae 동일 변환 (독립 복제 — consumer import 부작용 회피)."""
    recs = []
    for r in rows:
        if r.get("stabilization_flag") is None:
            continue
        rec = dict(r["sensors"])
        rec["C64"] = wafer_id
        rec["C7"] = r.get("step")
        rec["C42"] = int(r["stabilization_flag"])
        rec["C46"] = r.get("seq_in_step")
        rec["C24"] = chamber
        rec["C10"] = pd.Timestamp(r["timestamp"]).tz_localize(None) \
            if pd.Timestamp(r["timestamp"]).tzinfo else pd.Timestamp(r["timestamp"])
        recs.append(rec)
    return pd.DataFrame(recs)


def _features_one(m: AEModel, trace: pd.DataFrame) -> pd.Series:
    """트레이스 1wafer → 정렬·스케일 전 피처 벡터 (feat_cols 순)."""
    d, _ = prepare_frame(trace)
    F = extract_features(d).fillna(0.0)
    return F.reindex(columns=m.feat_cols, fill_value=0.0).iloc[0]


def live() -> None:
    """fdc.raw tail → 완성 wafer 스코어 + 같은 wafer 덤프 피처 diff Top12."""
    from confluent_kafka import Consumer
    import os
    m = load_bundle()
    dump_df = pd.read_csv(DUMP, low_memory=False)
    boot = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
    if not boot:                                   # .env 폴백 (진단 세션엔 env 없음)
        envf = ROOT / ".env"
        if envf.exists():
            for ln in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ln.strip().startswith("KAFKA_BOOTSTRAP_SERVERS="):
                    boot = ln.split("=", 1)[1].strip().strip('"')
    boot = boot or "localhost:9092"
    c = Consumer({"bootstrap.servers": boot, "group.id": "diag-ae-tail",
                  "auto.offset.reset": "latest", "enable.auto.commit": False})
    c.subscribe(["fdc.raw"],
                on_assign=lambda _c, ps: print(f"  파티션 할당: {[p.partition for p in ps]}"))
    print(f"fdc.raw tail 중 ({boot}) — 완성 wafer 2장 잡으면 종료 (최대 120s)…")
    buf, done, t0 = {}, [], pd.Timestamp.now()
    n_msg, errs = 0, set()
    while len(done) < 2 and (pd.Timestamp.now() - t0).total_seconds() < 120:
        msg = c.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            e = str(msg.error())
            if e not in errs:
                errs.add(e)
                print(f"  [kafka 에러] {e}")
            continue
        n_msg += 1
        if n_msg % 500 == 1:
            print(f"  …수신 {n_msg}건 (버퍼 wafer {len(buf)})")
        try:
            p = json.loads(msg.value())
        except Exception:
            continue
        if not isinstance(p, dict) or "wafer_id" not in p:
            continue
        w = p["wafer_id"]
        buf.setdefault(w, {"meta": p, "rows": []})["rows"].append(p)
        # 완성 판정: 같은 챔버에서 다음 wafer가 시작되면 직전 wafer 마감 (행수 가변 8~20)
        ch = p.get("chamber_id", "?")
        prev = live.__dict__.setdefault("_cur", {}).get(ch)
        live.__dict__["_cur"][ch] = w
        if prev and prev != w and prev in buf and len(buf[prev]["rows"]) >= 6:
            done.append((prev, buf.pop(prev)))
    c.close()
    if not done:
        print(f"완성 wafer 0 (총 수신 {n_msg}건) — 수신 0이면 브로커 주소/시뮬 확인, "
              f"수신은 있는데 완성 0이면 wafer당 행수<20 (버퍼 잔량 "
              f"{ {k: len(v['rows']) for k, v in list(buf.items())[:3]} })")
        return
    for w, item in done:
        ch = item["meta"].get("chamber_id", "default")
        trace = _rows_to_trace_ae(w, ch, item["rows"])
        recs = m.score_wafers(trace, chamber_col="C24", with_drift=False)
        r = recs[0] if recs else {}
        print(f"\n■ 라이브 {w} ({ch}, rows={len(trace)}): "
              f"ae_raw={r.get('ae_raw')} score={r.get('ae_score')} "
              f"top={r.get('ae_top_channels')}")
        dsub = dump_df[dump_df["C64"] == w]
        if dsub.empty:
            print("  (덤프에 같은 id 없음 — id 대역 불일치 자체가 단서)")
            continue
        fl = _features_one(m, trace)
        fd = _features_one(m, dsub)
        sd = pd.Series(m.scaler.scale_, index=m.feat_cols) \
            if hasattr(m.scaler, "scale_") else pd.Series(1.0, index=m.feat_cols)
        z = ((fl - fd) / sd.replace(0, np.nan)).abs().fillna(0.0)
        print("  피처 diff Top12 (|Δ|/scale — 라이브 vs 덤프 같은 wafer):")
        for k, v in z.sort_values(ascending=False).head(12).items():
            print(f"    {k:24s} live={fl[k]:>12.4f} dump={fd[k]:>12.4f} zΔ={v:8.2f}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "offline"
    (offline if mode == "offline" else live)()
