# -*- coding: utf-8 -*-
"""프라이어 → CT⓪ bias 시드 (A26 · R9 "신규 기준선 수립" 승인 경로 — 신규 결정 7·9).

무엇: 요란 전이 시 라벨 없이 추정한 ΔY(점근)를, **사람 승인 1회를 경유해**
`control/ct0/bias_<chamber>.json` 으로 내보낸다. CT⓪ 서빙(consumer)이 이 파일을 읽어
`mode: active` 일 때만 가산한다 — 시드 크기(수백)는 rolling clamp(±1×RMSE)를 한참
넘으므로 자동 경로가 원리적으로 불가하고, 그래서 R9 가 "승인 1회 경유·상한 예외"다.

경계 (2026-08-09 PM 확정):
  · 이 모듈은 **PM 소유(src/gateway — 헌법 3-1)**. `src/agent_a_mlops/ct0_bias/*` 는
    건드리지 않는다. 파일 포맷은 #152 `ct0_bias/control_io.py::bias_payload` 계약과
    **동형**으로 맞춘다 (schema 1 · 서빙이 실제로 읽는 값은 `bias` 하나 — #152 머지 후
    import 교체 후보, 그때까지 계약 동결).
  · updater 와의 공존(부트스트랩이 시드 파일을 보존하는 규약)은 A 와 협의 항목 —
    데모 스택은 updater 미기동이라 경합이 없다.
  · 적용(active 전환)은 이 모듈 밖 — P2 게이트(Scorecard 프로브) 실증 뒤에만 (CT⓪ §13).

시드 값 = 활성 마커 × Δ채널 (점근) — '추정' 딱지 필수(신규 결정 9).
감쇠 몫(τ·A)은 파일 메타로만 싣는다: 서빙 계약이 상수 1개(`bias`)라 시간 감쇠는
현행 서빙이 못 먹는다. **정직하게 점근 상수만 시드**하고, 온셋 과도 몫은 카드에
"템플릿 소관·서빙 미반영" 으로 명시한다 (자 혼합 금지 — 0013 y_basis 규약).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("prior-seed")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1                 # ct0_bias/control_io.SCHEMA_VERSION 과 동형 (계약)
FILE_PREFIX = "bias_"
WRITE_RETRY = 5
WRITE_RETRY_SLEEP = 0.05
SEED_STATUS = "SEED"               # updater 산출과 구분되는 출처 표식 (감사용 — 서빙은 미판독)
# F6 "초기진폭 이월" 폴백 — 직전 전이 실측이 원장에 없을 때 쓰는 앵커 전이(8/8 실측) 값.
FALLBACK_TAU_DAYS = 12.5
FALLBACK_AMPLITUDE = 491.0


def _dsn() -> str:
    """DATABASE_URL — env 우선, 없으면 리포 루트 `.env`. 값은 어디에도 출력하지 않는다."""
    v = os.environ.get("DATABASE_URL")
    if v:
        return v
    for cand in (REPO_ROOT / ".env", REPO_ROOT.parent / ".env"):
        if not cand.exists():
            continue
        for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL 미해석 — env 또는 리포 루트 .env 확인 (값은 출력하지 않음)")


def bias_dir() -> Path:
    """CT⓪ bias 파일 디렉토리 — env `CT0_BIAS_DIR` 우선 (#152 control_io 와 같은 키)."""
    env = os.environ.get("CT0_BIAS_DIR", "").strip()
    return Path(env) if env else REPO_ROOT / "control" / "ct0"


def bias_path(chamber_id: str, directory: Path | None = None) -> Path:
    import re
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(chamber_id or "unknown"))
    return (directory or bias_dir()) / f"{FILE_PREFIX}{safe}.json"


def seed_payload(chamber_id: str, *, bias: float, approval_id: str,
                 marker_version: str, basis: str, delta: float,
                 valid_from_pm_count: int, tau_days: float, amplitude: float,
                 ledger_ref: str | None, note: str | None = None) -> dict:
    """#152 `bias_payload` 와 동형 + `seed` 메타 확장 (여분 키는 서빙이 무시 — 계약상 안전).

    서빙이 실제로 읽는 값은 `bias` 하나다. `status='SEED'`·`ct_id=승인 기록`이
    "이 값은 라벨이 아니라 승인된 프라이어 추정"임을 감사 경로에 남긴다.
    """
    return {
        "schema": SCHEMA_VERSION,
        "chamber_id": chamber_id,
        "bias": round(float(bias), 2),
        "status": SEED_STATUS,
        "mode": "off",                      # 정보 필드 — 적용 여부는 params ct.model_r2r.mode 가 정본
        "n_samples": 0,                     # 라벨 0건 — 라벨 기반이 아님을 그대로 노출
        "raw_bias": round(float(bias), 2),
        "cap": None,                        # R9: 승인 1회 경유 = 상한(C7) 예외 (자동이 아니므로)
        "ct_id": approval_id,               # 승인 기록 참조 — "기록 없으면 갱신 없음"
        "valid_from_pm_count": int(valid_from_pm_count),
        "train_rmse": None,
        "window_model_version": None,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": {                           # 확장 메타 (감사·화면용)
            "estimate": True,               # '추정' 딱지 (신규 결정 9 — 필수)
            "marker_version": marker_version,
            "basis": basis,                 # 계수가 유효한 자 조합 (prior_markers.basis)
            "delta": round(float(delta), 3),
            "asymptote_only": True,         # 서빙 계약이 상수 1개 — 감쇠 몫은 미반영(정직 표기)
            "tau_days": round(float(tau_days), 1),
            "amplitude": round(float(amplitude), 1),
            "ledger_ref": ledger_ref,
            "note": note,
        },
    }


def write_seed(payload: dict, directory: Path | None = None) -> Path:
    """tmp + `os.replace` 원자 교체 (+ Windows 잠금 backoff) — 헌법 7장 규약.

    읽는 쪽(BiasCache)이 mtime+size 캐시라 부분 쓰기가 노출되면 안 된다.
    NaN 금지(allow_nan=False)까지 #152 control_io 와 동일 규율.
    """
    chamber = str(payload.get("chamber_id") or "unknown")
    path = bias_path(chamber, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    last: Exception | None = None
    for i in range(WRITE_RETRY):
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
            return path
        except OSError as e:
            last = e
            time.sleep(WRITE_RETRY_SLEEP * (i + 1))
    tmp.unlink(missing_ok=True)
    raise OSError(f"시드 파일 원자 교체 실패({path}): {last}")


# ── 제안 생성 (DB 읽기 — gateway GET /prior/proposal 이 호출) ────────────────
def build_proposal(chamber_id: str, conn=None) -> dict:
    """활성 마커 × 최근 전이 Δ → 시드 제안 JSON (S4 카드 데이터).

    자(basis) 규율: 마커 basis 와 전이 행의 delta_basis 가 다르면 제안은 살리되
    `basisMismatch` 를 명시한다 — 시뮬 전이(scenario_def)에 실자(qual5-seg1) 마커를
    적용하는 데모 상황이 바로 이 경우다. 숨기지 않고 딱지로 노출한다 (주입 공개 원칙).
    """
    import psycopg2
    own = conn is None
    if own:
        conn = psycopg2.connect(_dsn())
    try:
        cur = conn.cursor()
        cur.execute("SELECT version, coefs, basis FROM prior_markers WHERE is_active")
        mk = cur.fetchone()
        if not mk:
            return {"proposal": None, "reason": "활성 마커 없음 — 0013 시드(v1) 적용 필요"}
        version, coefs, basis = mk[0], mk[1], mk[2]
        cur.execute("""SELECT transition_id, c17_delta, delta_basis, tau_days, amplitude,
                              y_est, y_actual, source, source_ref, verdict_at, incident_id
                       FROM prior_transitions WHERE chamber_id = %s
                       ORDER BY verdict_at DESC LIMIT 1""", (chamber_id,))
        tr = cur.fetchone()
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE source = 'injected')
                       FROM prior_transitions WHERE y_actual IS NOT NULL""")
        n_all, n_inj = cur.fetchone()
        cur.close()
    finally:
        if own:
            conn.close()
    if not tr:
        return {"proposal": None,
                "reason": f"{chamber_id} 요란 전이 원장 행 없음 — record 선행 (전이가 없으면 시드도 없다)"}
    tid, delta, delta_basis, tau, amp, y_est, y_actual, source, ref, at, inc_id = tr
    slope = float((coefs or {}).get("C17", 0.0))
    est = round(slope * float(delta), 1)
    tau = float(tau) if tau is not None else FALLBACK_TAU_DAYS
    amp = float(amp) if amp is not None else FALLBACK_AMPLITUDE
    mismatch = (basis or "").split("->")[0].strip() != (delta_basis or "")
    return {
        "kind": "prior_seed",                       # 마커 '개정'(prior_ledger propose)과 별개 흐름
        "chamber": chamber_id,
        "transitionId": tid, "incidentId": inc_id, "sourceRef": ref, "source": source,
        "estimate": True,                            # '추정' 딱지
        "marker": f"{version} · slope {slope}",
        "basis": basis,
        "delta": float(delta), "deltaBasis": delta_basis,
        "basisMismatch": bool(mismatch),
        "basisNote": ("시뮬 전이(scenario_def)에 실자 마커 적용 — 메커니즘 시연 지위 (주입 공개 원칙)"
                      if mismatch else "자 일치"),
        "seedBias": est,                             # 점근 상수 (서빙 가산 후보값)
        "template": {"tauDays": tau, "amplitude": amp,
                     "note": "온셋 과도 몫 — 서빙 계약(상수 1개)에는 미반영, 표시·가한계 폭 용"},
        "ledger": {"n": int(n_all), "injected": int(n_inj)},
        "refActual": (float(y_actual) if y_actual is not None else None),
        "guard": ("적용은 승인 1회 경유(R9 — clamp 예외) · mode=active 는 P2 Scorecard 게이트 뒤 · "
                  "요란 리셋 시 updater 가 재계산으로 인수(D+60)"),
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="prior_seed", description="프라이어 bias 시드 (A26 · R9)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose", help="시드 제안 JSON (S4 카드 데이터)")
    p.add_argument("--chamber", required=True)
    w = sub.add_parser("write", help="승인 후 시드 파일 쓰기 (approval_id 필수 — 기록 없으면 쓰기 없음)")
    w.add_argument("--chamber", required=True)
    w.add_argument("--approval-id", required=True)
    w.add_argument("--dir", default=None, help="출력 디렉토리 (기본 control/ct0 — 테스트 격리용)")
    a = ap.parse_args()
    if a.cmd == "propose":
        print(json.dumps(build_proposal(a.chamber), ensure_ascii=False, indent=1, default=str))
        return
    prop = build_proposal(a.chamber)
    if not prop.get("seedBias") and prop.get("seedBias") != 0.0:
        raise SystemExit(f"시드 산출 불가: {prop.get('reason')}")
    payload = seed_payload(
        a.chamber, bias=prop["seedBias"], approval_id=a.approval_id,
        marker_version=prop["marker"], basis=prop["basis"], delta=prop["delta"],
        valid_from_pm_count=0, tau_days=prop["template"]["tauDays"],
        amplitude=prop["template"]["amplitude"], ledger_ref=prop["sourceRef"],
        note=prop["basisNote"])
    out = write_seed(payload, Path(a.dir) if a.dir else None)
    print(f"[SEED] {out} bias={payload['bias']} ct_id={a.approval_id}")


if __name__ == "__main__":
    main()
