# -*- coding: utf-8 -*-
"""프라이어 전이 원장 — 적재·오차 산출·마커 개정 제안 (A26 · BL2 · 신규 결정 9).

왜 원장인가: 프라이어 v1 은 관측 전이 **1건**으로 세운 앵커(-6.96)다. 전이가 쌓이지
않으면 영원히 n=1 이므로 개정이 원리적으로 불가능하다. 이 스크립트가 그 축적을 담당한다.

산식은 `scripts/check_c17_prior.py`(F17 검증분)와 동일하다:
    y_est = slope x delta,  err_pct = |y_est - y_actual| / |y_actual| x 100

서브커맨드
  record   sim_events.jsonl 의 요란 전이(pattern=pm_reset) -> 원장 INSERT (source=injected)
  refit    원장으로 후보 마커 최소제곱 적합 -> 후보별 오차 + 수렴곡선
  propose  임계 충족 시 MarkerProposal JSON 출력 (프론트 mock/fdc.ts 인터페이스와 동형)

주입 공개 원칙: source 컬럼이 'injected' 인 행은 제안 counter 문구에 건수를 그대로 노출한다.
실운전 전이로 재검증하기 전까지 이 제안의 지위는 **메커니즘 검증**이다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[1]
SIM_EVENTS = ROOT / "sim_events.jsonl"
def _dsn() -> str:
    """DB 접속 문자열 — env DATABASE_URL 우선, 없으면 `.env` 에서 읽는다.

    자격증명을 **코드에 넣지 않는다**(리포에 커밋되므로). 값은 어디에도 출력하지 않고
    실패 시 "어디를 보라"만 알린다. 워크트리에서 실행할 때를 대비해 상위 디렉토리도 본다.
    """
    v = os.environ.get("DATABASE_URL")
    if v:
        return v
    for cand in (ROOT / ".env", ROOT.parent / ".env"):
        if not cand.exists():
            continue
        for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("DATABASE_URL 미해석 — env 또는 리포 루트 .env 를 확인하세요 (값은 출력하지 않습니다)")

# 후보 마커 — (표시명, 사용 채널)
# 후보 = **재캘리브레이션** 서사 (결정 9 원문: "기울기·템플릿·임계 재캘리브레이션").
#   '조합' 후보는 시뮬에서 C17·C11 이 실측 비율로 공선이라 원리적으로 적합 불가 —
#   지우지 않고 남겨 "왜 조합이 아닌가"를 결과표가 직접 말하게 한다 (8/8 실측:
#   C12·C11_min 상관 0.9584 = 전기 설정/실측 쌍, 조합의 독립 정보 없음).
CANDIDATES = [
    ("C17 재캘리 (v1 채널 유지)", ["c17_delta"]),
    ("C11_min 단독 (전기 축)", ["c11_wfmin_delta"]),
    ("C17 + C11 조합 (공선 — 대조용)", ["c17_delta", "c11_wfmin_delta"]),
]
MIN_N = 3          # 회귀 최소 표본 (미달 시 제안 금지 — 정직 표시)
MIN_GAIN_PCT = 20  # 현행 대비 오차 개선률 임계

log = logging.getLogger("prior-ledger")


def connect():
    """DB 연결 (autocommit=False — 적재는 트랜잭션 단위)."""
    return psycopg2.connect(_dsn())


def active_marker(cur) -> dict:
    """활성 마커 1행. 없으면 예외 — 추정 기준이 없는 상태로 적재하면 안 된다."""
    cur.execute("SELECT version, channels, coefs, basis FROM prior_markers WHERE is_active")
    r = cur.fetchone()
    if not r:
        raise RuntimeError("활성 마커 없음 — 0013 시드(v1)를 먼저 적용하세요")
    return {"version": r[0], "channels": r[1], "coefs": r[2], "basis": r[3]}


def _basis_pair(marker_basis: str) -> tuple:
    """마커 basis 'ΔX자 -> Y자' → (delta_basis, y_basis). 파싱 실패는 즉시 예외 —
    자를 모르는 채 적합하면 안 된다 (헌법 7장 "파생 수치는 자와 세트로")."""
    parts = [s.strip() for s in str(marker_basis).split("->")]
    if len(parts) != 2 or not all(parts):
        raise RuntimeError(f"마커 basis 판독 불가: {marker_basis!r} — prior_markers.basis 확인")
    return parts[0], parts[1]


def estimate(coefs: dict, row: dict) -> float:
    """마커 계수 x 델타 = 추정 ΔY. 계수에 없는 채널은 0 기여."""
    key = {"C17": "c17_delta", "C11_wf_min": "c11_wfmin_delta", "C12": "c12_delta"}
    out = float(coefs.get("intercept", 0.0))
    for ch, co in coefs.items():
        if ch == "intercept":
            continue
        v = row.get(key.get(ch, ""))
        if v is not None:
            out += float(co) * float(v)
    return out


def cmd_record(args):
    """sim_events.jsonl -> prior_transitions. 재실행 무해(ON CONFLICT DO NOTHING)."""
    if not SIM_EVENTS.exists():
        raise SystemExit(f"{SIM_EVENTS} 없음 — 프로듀서 1회 기동 후 재시도")
    evs = []
    for line in SIM_EVENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        gt = e.get("ground_truth") or {}
        if e.get("pattern") != "pm_reset" or gt.get("type") != "regime_transition":
            continue
        if e.get("y_shift") is None:
            continue
        evs.append(e)
    evs.sort(key=lambda x: (str(x.get("injected_at")), str(x.get("chamber_id"))))
    print(f"요란 전이 이벤트 {len(evs)}건 발견")

    conn = connect()
    cur = conn.cursor()
    mk = active_marker(cur)
    seq = {}
    ins = 0
    for e in evs:
        ch = str(e.get("chamber_id", "UNKNOWN"))
        at = str(e.get("injected_at"))
        tid = _tid(ch, at, str(e.get("scenario_id") or e.get("event_id")))
        row = {
            "c17_delta": (e.get("level_shifts_eng") or {}).get("C17"),
            "c11_wfmin_delta": (e.get("ignition_shifts_eng") or {}).get("C11"),
            "c12_delta": (e.get("level_shifts_eng") or {}).get("C12"),
        }
        if row["c17_delta"] is None:
            print(f"  SKIP {tid} — C17 레벨 시프트 없음(주채널 부재)")
            continue
        y_act = float(e["y_shift"])
        y_est = estimate(mk["coefs"], row)
        err = abs(y_est - y_act) / abs(y_act) * 100 if y_act else None
        print(f"  {tid} ΔC17={row['c17_delta']:+.1f} ΔC11={row['c11_wfmin_delta']} "
              f"est={y_est:+.0f} actual={y_act:+.0f} err={err:.1f}%")
        if args.dry_run:
            continue
        ref = str(e.get("scenario_id") or e.get("event_id"))
        act = _upsert(cur, tid=tid, ch=ch, at=at, c17=row["c17_delta"],
                      c11=row["c11_wfmin_delta"], c12=row["c12_delta"],
                      dbasis="scenario_def", ybasis="scenario_def", fitc=None,
                      mk=mk["version"],
                      est=y_est, act=y_act, err=err, src="injected", ref=ref,
                      note=f"sim_events {e.get('event_id')} · scenario {e.get('scenario_id')}")
        print(f"      -> {act} (ref={ref})")
        ins += 1 if act == "INSERT" else 0
    if not args.dry_run:
        conn.commit()
    print(f"신규 {ins}건 (재생분은 observed_count 증가 · dry-run={args.dry_run})")
    cur.close(); conn.close()


SCEN_DIR = ROOT / "src" / "simulator" / "scenarios"
SYNTH_BASE = "2026-06-01"     # 합성 행 기준일 (결정론 — 재실행 시 같은 id)


def cmd_from_scenarios(args):
    """시나리오 정의(pm_reset) -> 원장 적재. source='synthetic'.

    실주입 없이 정의값만 넣는다 — 회로(적재→재적합→제안)를 먼저 닫는 경로.
    시뮬 세계는 평평한 계단이라 창 민감도가 없다: 정의값이 곧 그 세계의 자다.
    8/12 리허설 실주입이 돌면 같은 전이가 source='injected' 로 들어와 승격된다.
    """
    import datetime as _dt
    import yaml
    picked = []
    for f in sorted(SCEN_DIR.glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception as exc:                      # noqa: BLE001
            print(f"  SKIP {f.name} — yaml 파싱 실패 {exc}"); continue
        if d.get("pattern") != "pm_reset" or d.get("y_shift") is None:
            continue
        c17 = (d.get("level_shifts_eng") or {}).get("C17")
        if c17 is None:
            print(f"  SKIP {f.name} — C17 레벨 시프트 없음"); continue
        picked.append((f.name, d, float(c17)))
    picked.sort(key=lambda x: x[2])            # 강도(ΔC17) 오름차순 = 합성 날짜의 기준
    # ⚠️ 날짜(verdict_at)는 **전체 목록 기준 인덱스**로 뽑는다. 제외분을 뺀 뒤 번호를 매기면
    #   같은 시나리오가 실행마다 다른 날짜를 받아 대리키가 기존 행과 충돌한다 (실측 8/8).
    day_of = {str(d.get("scenario_id") or nm): i for i, (nm, d, _c) in enumerate(picked)}
    hold = set(getattr(args, "exclude", None) or [])
    for nm, d, _c in picked:
        sid = str(d.get("scenario_id") or nm)
        if sid in hold:
            print(f"  HOLD {nm} — {sid} (라이브 주입 예비분 · 아직 일어나지 않은 전이는 적지 않는다)")
    picked = [(nm, d, c) for nm, d, c in picked if str(d.get("scenario_id") or nm) not in hold]
    print(f"pm_reset 적재 대상 {len(picked)}종 (전체 {len(day_of)}종)")

    conn = connect(); cur = conn.cursor()
    mk = active_marker(cur)
    base = _dt.date.fromisoformat(SYNTH_BASE)
    ins = 0
    for i, (name, d, c17) in enumerate(picked):
        ch = str(d.get("target_chamber", "UNKNOWN"))
        _ref = str(d.get("scenario_id") or name)
        at = f"{base + _dt.timedelta(days=7 * day_of[_ref])}T00:00:00+00:00"
        tid = _tid(ch, at, _ref)
        row = {"c17_delta": c17,
               "c11_wfmin_delta": (d.get("ignition_shifts_eng") or {}).get("C11"),
               "c12_delta": (d.get("level_shifts_eng") or {}).get("C12")}
        y_act = float(d["y_shift"])
        y_est = estimate(mk["coefs"], row)
        err = abs(y_est - y_act) / abs(y_act) * 100 if y_act else None
        print(f"  {tid} {name:<30} ΔC17={c17:+.2f} est={y_est:+.0f} actual={y_act:+.0f} err={err:.1f}%")
        if args.dry_run:
            continue
        ref = str(d.get("scenario_id") or name)
        act = _upsert(cur, tid=tid, ch=ch, at=at, c17=row["c17_delta"],
                      c11=row["c11_wfmin_delta"], c12=row["c12_delta"],
                      dbasis="scenario_def", ybasis="scenario_def", fitc=None,
                      mk=mk["version"],
                      est=y_est, act=y_act, err=err, src="synthetic", ref=ref,
                      note=f"scenario {d.get('scenario_id')} ({name})")
        print(f"      -> {act} (ref={ref})")
        ins += 1 if act == "INSERT" else 0
    if not args.dry_run:
        conn.commit()
    print(f"신규 {ins}건 (재생분은 observed_count 증가 · dry-run={args.dry_run})")
    cur.close(); conn.close()


COND_MAX = 30.0    # 설계행렬 조건수 임계 — 넘으면 공선으로 판정한다


_UPSERT = """
INSERT INTO prior_transitions
  (transition_id, chamber_id, verdict_at, c17_delta, c11_wfmin_delta, c12_delta,
   delta_basis, y_basis, fit_c,
   marker_version, y_est, y_actual, y_actual_at, err_pct, source, source_ref, last_seen_at, note)
VALUES (%(tid)s,%(ch)s,%(at)s,%(c17)s,%(c11)s,%(c12)s,
        %(dbasis)s,%(ybasis)s,%(fitc)s,
        %(mk)s,%(est)s,%(act)s,%(at)s,%(err)s,%(src)s,%(ref)s,%(at)s,%(note)s)
ON CONFLICT (source_ref) DO UPDATE SET
  source         = CASE WHEN prior_transitions.source = 'synthetic'
                          AND EXCLUDED.source = 'injected' THEN 'injected'
                        ELSE prior_transitions.source END,
  observed_count = prior_transitions.observed_count + 1,
  last_seen_at   = EXCLUDED.last_seen_at
RETURNING (xmax = 0) AS inserted
"""


def _tid(chamber: str, at_iso: str, ref: str) -> str:
    """전이 대리키 — `PRI-<YYYYMMDD>-<CHAMBER>-<SEQ>` (6-4 ID 포맷).

    SEQ 를 **자연키 해시**로 뽑는다. 실행 순서로 번호를 매기면 목록이 달라질 때 같은 날짜에
    다른 전이가 같은 번호를 받아 PK 가 충돌한다 (실측 8/8: 예비분 제외 여부로 s100/s130 충돌 —
    자연키만 고치고 대리키를 위치 기반으로 남긴 절반짜리 수정이었다).
    날짜는 사건 시각(verdict_at)이라 의미를 유지하고, 충돌 회피는 SEQ 가 담당한다.
    """
    seq = int(hashlib.sha1(ref.encode("utf-8")).hexdigest()[:6], 16) % 1000
    return f"PRI-{at_iso[:10].replace('-', '')}-{chamber.replace('_', '')}-{seq:03d}"


def _upsert(cur, **kw) -> str:
    """원장 1행 UPSERT. 반환 'INSERT' | 'UPDATE(재생)'.

    자연키가 source_ref 라 같은 전이를 몇 번 재생해도 행이 늘지 않는다.
    xmax=0 이 INSERT 판별 관용구다(갱신된 행은 xmax 가 0 이 아니다).
    """
    cur.execute(_UPSERT, kw)
    return "INSERT" if cur.fetchone()[0] else "UPDATE(재생)"


def _fit(rows, chans):
    """**절편 0 고정** 최소제곱 + 조건수 가드. 반환 (coefs, err_pct, 불가사유).

    절편 0 인 이유: 전이가 없으면(Δ채널=0) Y 이동도 0 이어야 한다. 절편을 자유롭게
    두면 "전이 없이도 Y 가 움직인다"는 항이 붙어 물리를 위반한다 (v1 계약도 intercept 0).
    실측 2026-08-08: 절편을 열어두면 -10.7 이 붙었다.

    조건수 가드 이유: 채널이 서로 비례하면(우리 시나리오의 C17·C11) 라운딩 잔여
    4번째 자리에 최소제곱이 계수 -1186 / -4944 를 붙여 **잔차만** 0.1% 로 만든다.
    잔차가 작다고 좋은 모델이 아니다 — 조건수가 그 폭발을 사전에 잡는다.
    단일 채널은 열이 하나라 조건수가 항상 1 이므로 이 가드에 걸리지 않는다.
    """
    X, y = [], []
    for r in rows:
        if any(r[c] is None for c in chans):
            continue
        X.append([float(r[c]) for c in chans])
        y.append(float(r["y_actual"]))
    if len(X) < len(chans) + 1:
        return None, None, f"표본 부족 ({len(X)}건 · 파라미터 {len(chans)})"
    A, b = np.array(X, float), np.array(y, float)
    if np.linalg.matrix_rank(A) < A.shape[1]:
        return None, None, "완전 공선 (델타 분산 0)"
    cond = float(np.linalg.cond(A))
    if cond > COND_MAX:
        return None, None, f"공선 — 조건수 {cond:,.0f} > {COND_MAX:.0f}"
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    pred = A @ sol
    err = float(np.mean(np.abs(pred - b) / np.abs(b)) * 100)
    return sol, err, None


def _rows(cur):
    cur.execute("""SELECT transition_id, c17_delta, c11_wfmin_delta, c12_delta,
                          delta_basis, y_basis, fit_c,
                          y_est, y_actual, err_pct, source, verdict_at
                   FROM prior_transitions WHERE y_actual IS NOT NULL
                   ORDER BY verdict_at""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def analyze():
    """원장 -> (marker, 자 일치 그룹, 자 불일치 그룹, 후보 결과, 수렴곡선).

    ⚠️ 자(basis) 필터가 이 함수의 존재 이유다 (2026-08-09 ②a 확정 · 헌법 7장):
    활성 마커의 계수는 그 마커의 basis(예: qual5-seg1 -> last1000_mean)로 잰 행에만
    유효하다. 다른 자(scenario_def 등)의 행을 섞어 재적합하면 "개선"과 "자 불일치"가
    구별되지 않는다 — "v1 72.1% → 재캘리 1.23%"(8/8)가 정확히 그 사고였다.
    """
    conn = connect(); cur = conn.cursor()
    mk = active_marker(cur)
    rows = _rows(cur)
    cur.close(); conn.close()

    mk_d, mk_y = _basis_pair(mk["basis"])
    matched = [r for r in rows if r["delta_basis"] == mk_d and r["y_basis"] == mk_y]
    other = [r for r in rows if not (r["delta_basis"] == mk_d and r["y_basis"] == mk_y)]

    cur_err = (float(np.mean([r["err_pct"] for r in matched if r["err_pct"] is not None]))
               if any(r["err_pct"] is not None for r in matched) else None)
    cands = []
    for name, chans in CANDIDATES:
        sol, err, why = _fit(matched, chans)
        cands.append({"marker": name, "chans": chans,
                      "coefs": None if sol is None else [round(float(v), 3) for v in sol],
                      "errPct": None if err is None else round(err, 2),
                      "note": why})
    # 자 불일치 그룹은 **별도 적합** — 결과는 "메커니즘 검증" 지위로만 보고한다.
    other_cands = []
    for name, chans in CANDIDATES:
        sol, err, why = _fit(other, chans)
        other_cands.append({"marker": name, "chans": chans,
                            "coefs": None if sol is None else [round(float(v), 3) for v in sol],
                            "errPct": None if err is None else round(err, 2),
                            "note": why})
    # 수렴곡선은 **현행 채널(C17) 재캘리 기준 · 자 일치 그룹**으로만 그린다. 최선 후보를
    # 따라가면 과적합된 공선 후보가 곡선을 0 으로 눌러버려 그림이 거짓말을 한다 (8/8 실측).
    trend = []
    for k in range(2, len(matched) + 1):
        _, err, _ = _fit(matched[:k], ["c17_delta"])
        trend.append(None if err is None else round(err, 2))
    return mk, matched, other, cands, other_cands, cur_err, trend


def cmd_refit(args):
    mk, matched, other, cands, other_cands, cur_err, trend = analyze()
    print(f"활성 마커 {mk['version']} · 계수 {mk['coefs']} · 자 {mk['basis']}")
    print(f"원장: 자 일치 {len(matched)}건 / 자 불일치 {len(other)}건 (scenario_def 등)")
    print(f"[자 일치 — {mk['basis']}] 현행 평균 오차 "
          f"{('%.1f%%' % cur_err) if cur_err is not None else 'n/a'}")
    for c in cands:
        s = f"적합 불가 — {c['note']}" if c["errPct"] is None else f"오차 {c['errPct']}% · 계수 {c['coefs']}"
        print(f"  - {c['marker']:<32} {s}")
    print(f"  수렴곡선 (C17 누적 k=2..{len(matched)}): {trend if trend else '산출 불가'}")
    print(f"[자 불일치 — 메커니즘 검증 전용 · v1 재캘리에 사용 금지]")
    for c in other_cands:
        s = f"적합 불가 — {c['note']}" if c["errPct"] is None else f"오차 {c['errPct']}% · 계수 {c['coefs']}"
        print(f"  - {c['marker']:<32} {s}")
    print("  ※ 시뮬 세계는 결정론적 직선이라 2건에서 이미 수렴한다 — 평평한 것이 정상.")
    print("     실운전 전이는 산포가 있어 더 많은 건수가 필요하다.")


def cmd_propose(args):
    mk, matched, other, cands, other_cands, cur_err, trend = analyze()
    ok = [c for c in cands if c["errPct"] is not None]
    n_all = len(matched) + len(other)
    if len(matched) < MIN_N or not ok or cur_err is None:
        # 자 일치 표본이 모자라면 개정 제안은 **정직하게 보류**한다. 자 불일치 그룹의
        # 적합이 아무리 좋아도 그것으로 v1 을 갈아끼우지 않는다 (analyze docstring).
        out = {"proposal": None,
               "reason": (f"자 일치 원장 {len(matched)}건 / 임계 {MIN_N}건 — 제안 보류. "
                          f"자 불일치 {len(other)}건(scenario_def)은 메커니즘 검증 전용"),
               "ledgerN": n_all, "ledgerMatched": len(matched),
               "basis": mk["basis"],
               "mechanism": [{"marker": c["marker"], "errPct": c["errPct"], "note": c["note"]}
                             for c in other_cands]}
        print(json.dumps(out, ensure_ascii=False, indent=1)); return
    # 동률(소수1자리)이면 **현행 채널 유지**를 우선한다 — 근거 없이 채널을 늘리지 않는다.
    best = min(ok, key=lambda c: (round(c["errPct"], 1), 0 if c["chans"] == ["c17_delta"] else 1))
    gain = (cur_err - best["errPct"]) / cur_err * 100
    n_inj = sum(1 for r in matched if r["source"] == "injected")
    out = {
        "id": f"PRI-{matched[-1]['verdict_at']:%Y%m%d}-{matched[-1]['transition_id'].split('-')[2]}-PROP",
        "kind": "prior_marker",
        "title": f"프라이어 마커 개정 — {mk['version']} → {best['marker']}",
        "basis": mk["basis"],
        "ledgerN": n_all, "ledgerMatched": len(matched),
        "current": f"{mk['version']} · {mk['coefs']} (평균 오차 {cur_err:.1f}%)",
        "proposed": f"{best['marker']} · 계수 {best['coefs']} (오차 {best['errPct']}%)",
        "candidates": [{"marker": c["marker"], "errPct": c["errPct"], "note": c["note"]} for c in cands],
        "errTrend": trend,
        "errTrendNote": ("C17 누적 재적합 in-sample 오차(k=2..n · 자 일치 그룹). 시뮬 세계는 "
                         "결정론적 직선이라 2건에서 수렴 — 실운전은 산포로 더 걸린다."),
        "gainPct": round(gain, 1),
        "counter": (f"자 일치 {len(matched)}건 중 injected {n_inj}건 · 자 불일치 {len(other)}건은 "
                    f"제외됨 — 실운전 전이 재검증 전까지 '메커니즘 검증' 지위 (주입 공개 원칙)"),
        "guard": ("개정 시 prior_markers 새 버전 INSERT + 활성 전환(부분 유니크로 동시 활성 차단) · "
                  "basis 는 현행 마커와 동일해야 함 · models/CHANGELOG 기록 · "
                  "롤백 = 직전 버전 재활성화 · correction 미발행(물리 미개입)"),
        "eligible": gain >= MIN_GAIN_PCT,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="prior_ledger", description="프라이어 전이 원장 (A26)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="sim_events -> 원장 적재")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_record)
    s = sub.add_parser("from-scenarios", help="시나리오 정의 -> 원장(synthetic)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--exclude", action="append", default=[],
                   help="적재에서 제외할 scenario_id (리허설 라이브 주입 예비분)")
    s.set_defaults(func=cmd_from_scenarios)
    sub.add_parser("refit", help="후보 마커 적합·오차").set_defaults(func=cmd_refit)
    sub.add_parser("propose", help="개정 제안 JSON").set_defaults(func=cmd_propose)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
