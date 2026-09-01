# -*- coding: utf-8 -*-
"""CT⓪ 부트스트랩 — **DB 전량 재계산**이 정본 (설계 §7).

기동·리밸런스·수동 `--rebuild` 에서 같은 함수를 쓴다. 마지막 `ct_decisions` 행을 믿지
않고 다시 계산하는 이유: "예외가 안 났으니 성공" 금지 — 상태로 판정한다 (TSR-0002).
재계산 결과와 마지막 감사 행이 다르면 **경고 후 재계산을 채택**한다.

경계(valid_from_pm_count) 복구 — 세 소스를 **모두 읽고 최댓값** (설계 §7 ①. 경계는 뒤로
가면 안 되고, 뒤처진 소스는 곧 "그쪽이 확정 이벤트를 놓쳤다"는 신호다):
    ① `quals.confirmed_verdict='loud'` 최신 — **권위 소스**. P6-1 배포 완료(컬럼 = init.sql
       7/28 · 쓰기 = B `qual_recorder` loud/quiet · 마이그레이션 `0010`). 경계 정수는
       `qual_id` 의 SEQ 에서 뽑는다 (B 채번 `QUAL-<날짜>-<챔버>-<pm_count>`)
    ② `ct_decisions` RESET 최신 행의 `trigger_reason` (`pm_count=NN` 파싱) — 자체 체크포인트
    ③ `pm_log.json` 요란 엔트리 (계약 §8-B-1) — 이벤트 유실 복구
    없으면 경계 없음(전량 자격)

train_rmse(clamp 분모) 해석 우선순위:
    ① env `CT0_TRAIN_RMSE` (운영 override)
    ② champion 스탬프 폴더의 `validation.json` (`rmse_after` — 게이트 실측)
    ③ 같은 폴더 `manifest.json` (`acceptance.pooled_rmse`)
    ④ `params.yaml` `lean85.bench_pooled_rmse` (동결 벤치 99.84) — **경고와 함께**
    ⑤ 못 구하면 `None` → 보정 **비활성** (0 분모 = 조용한 기능 정지 방지, 헌법 7장)

실행:
    python -m ct0_bias.bootstrap --chamber SIM_CH_3            # 재계산 결과 출력만
    python -m ct0_bias.bootstrap --all --write                 # bias 파일까지 재작성
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):                       # 직접 실행 대비 (python bootstrap.py)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ct0_bias import control_io, core          # type: ignore  # noqa: E402
else:
    from . import control_io, core

log = logging.getLogger("ct0-bootstrap")

REPO_ROOT = Path(__file__).resolve().parents[3]
CHAMPION_PTR = REPO_ROOT / "control" / "ct1" / "champion.json"
VALIDATION_REPORT = "validation.json"               # = validate_lean85.VALIDATION_REPORT
MANIFEST = "manifest.json"

# 재계산 SELECT 상한 — 창 N 보다 넉넉히 읽고 자격 필터 후 자른다. 라벨이 60일 지연이라
# 최신 N 건이 전부 부적격(구 레짐)일 수 있어 한 번에 더 읽는다.
FETCH_MULTIPLIER = 3
FETCH_MAX = 5000


# ── config·의존성 로드 ────────────────────────────────────────────────────
def load_ct_params() -> dict:
    """`config/params.yaml` `ct:` 절. 실패 시 `{}` → `BiasConfig` 가 `off` 로 잠근다."""
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("ct") or {}
    except Exception as e:                          # noqa: BLE001
        log.warning("params.yaml 로드 실패 → 보정 잠금(off) 기본값: %s", e)
        return {}


def load_config() -> core.BiasConfig:
    """`ct.model_r2r` → `BiasConfig` (+ 오설정 경고 1회)."""
    ct = load_ct_params()
    cfg = core.BiasConfig.from_params(ct)
    for w in cfg.validate(ct):
        log.warning("config 경고: %s", w)
    return cfg


def _read_json(path: Path) -> dict | None:
    """dict 만 돌려준다 — `json.loads` 성공 ≠ dict (헌법 7장)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def champion_dir() -> Path | None:
    """운영 champion 스탬프 폴더 (CT① 포인터 재사용 — 경로 하드코딩 금지, 헌법 6-4)."""
    env = os.environ.get("LEAN85_MODEL_DIR", "").strip()
    if env:
        return Path(env)
    ptr = _read_json(CHAMPION_PTR) or {}
    d = ptr.get("model_dir")
    return Path(d) if d else None


def resolve_train_rmse() -> tuple[float | None, str]:
    """clamp 분모 해석. 반환 `(값, 출처)` — 출처는 기동 로그에 남긴다.

    분모 가드는 여기서 끝난다: **>0 이 아니면 `None`** 을 돌려주고, `core.decide` 가
    보정을 비활성한다. 0·음수가 통과하면 clamp 가 무의미해지고 매 건 예외 → 전량 폴백
    이라는 조용한 기능 정지가 된다 (헌법 7장 '분모가 되는 baseline').
    """
    env = os.environ.get("CT0_TRAIN_RMSE", "").strip()
    if env:
        v = _pos(env)
        if v:
            return v, "env CT0_TRAIN_RMSE"
        log.warning("CT0_TRAIN_RMSE=%r 가 양수가 아님 — 무시", env)

    d = champion_dir()
    if d:
        rep = _read_json(Path(d) / VALIDATION_REPORT) or {}
        metrics = rep.get("metrics") if isinstance(rep.get("metrics"), dict) else rep
        v = _pos((metrics or {}).get("rmse_after"))
        if v:
            return v, f"{Path(d).name}/{VALIDATION_REPORT}:rmse_after"
        mani = _read_json(Path(d) / MANIFEST) or {}
        v = _pos((mani.get("acceptance") or {}).get("pooled_rmse"))
        if v:
            return v, f"{Path(d).name}/{MANIFEST}:acceptance.pooled_rmse"

    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            v = _pos(((yaml.safe_load(f) or {}).get("lean85") or {}).get("bench_pooled_rmse"))
        if v:
            log.warning("champion 메타에서 RMSE 를 못 구해 동결 벤치(%.2f)를 clamp 분모로 쓴다 "
                        "— champion 별 실측이 아니다 (설계 §14 한계)", v)
            return v, "params.yaml lean85.bench_pooled_rmse"
    except Exception as e:                          # noqa: BLE001
        log.warning("bench_pooled_rmse 로드 실패: %s", e)
    return None, "없음(보정 비활성)"


def _pos(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f and f != float("inf") else None


# ── DB 재계산 (설계 §7) ───────────────────────────────────────────────────
def pm_log_path() -> Path:
    """운영 `pm_log.json` (계약 §8-B-1 — **작성자 = B 1곳, A 는 읽기 전용**)."""
    env = os.environ.get("LEAN85_PM_LOG", "").strip()
    if env:
        return Path(env)
    try:
        import yaml
        with open(REPO_ROOT / "config" / "params.yaml", encoding="utf-8") as f:
            rel = ((yaml.safe_load(f) or {}).get("pm_log") or {}).get("path") or "pm_log.json"
    except Exception:                               # noqa: BLE001
        rel = "pm_log.json"
    p = Path(rel)
    return p if p.is_absolute() else REPO_ROOT / p


def pm_log_boundary(chamber_id: str) -> int | None:
    """`pm_log.json` 의 해당 챔버 최신 요란 엔트리 `pm_count` (계약 §8-B-1).

    ⚠️ `chamber_id`·`pm_count` 가 **둘 다 있는** 엔트리만 본다 — 2018 레거시 엔트리와
    수기 엔트리는 축이 없어 경계로 쓸 수 없다. 무거운 `lean85_pipeline` 을 끌지 않고
    stdlib 로 읽는다 (updater 는 xgboost·pandas 가 필요 없는 프로세스다).
    """
    path = pm_log_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:                          # noqa: BLE001 — 손상 파일은 덮지도 믿지도 않는다
        log.warning("pm_log 판독 실패(무시): %s", e)
        return None
    if not isinstance(data, list):
        return None
    best = None
    for e in data:
        if not isinstance(e, dict) or e.get("verdict") != "loud":
            continue
        if e.get("chamber_id") != chamber_id:
            continue
        try:
            pc = int(e.get("pm_count"))
        except (TypeError, ValueError):
            continue
        best = pc if best is None else max(best, pc)
    return best


# 스키마 미배포 신호 — psycopg2 SQLSTATE (42703 UndefinedColumn / 42P01 UndefinedTable).
# 이 둘만 "아직 마이그레이션 안 됨"으로 보고 완주하며, 접속 끊김 등 나머지는 올린다.
_SCHEMA_MISSING_SQLSTATE = ("42703", "42P01")


def _is_schema_missing(exc) -> bool:
    """컬럼·테이블 부재인가 (마이그레이션 미적용). 드라이버 클래스 대신 SQLSTATE 로 본다."""
    code = getattr(exc, "pgcode", None)
    if code in _SCHEMA_MISSING_SQLSTATE:
        return True
    return type(exc).__name__ in ("UndefinedColumn", "UndefinedTable")


def quals_boundary(conn, chamber_id: str) -> int | None:
    """`quals` 의 마지막 **요란 확정** → pm_count (설계 §7 ① 권위 소스).

    P6-1 은 이미 배포돼 있다 (`confirmed_verdict`·`confirmed_at` 컬럼 = init.sql 7/28,
    쓰기 = B `qual_recorder.update_qual_confirmed` 가 loud/quiet 둘 다 확정, 마이그레이션
    `0010`). 그래서 §7 이 설계한 권위 경로가 **폴백 없이 그대로 성립**한다.

    경계 정수는 `qual_id` 에서 뽑는다 — `quals` 에 `pm_count` 컬럼은 없지만 B 가
    `f"QUAL-{date}-{chamber_id}-{pm_count}"` 로 채번하기 때문이다 (`core.pm_count_from_qual_id`).

    `confirmed_verdict` 컬럼이 아직 없는 DB(마이그레이션 `0010` 미적용)에서는 `None` 을
    돌려 다른 소스로 넘긴다 — 로컬마다 스키마가 갈리는 과도기를 죽이지 않기 위해서다.
    반대로 접속 끊김 같은 진짜 장애는 **올린다** (fail-closed, 설계 D6).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT qual_id, confirmed_at FROM quals
                            WHERE chamber_id=%s AND confirmed_verdict='loud'
                         ORDER BY confirmed_at DESC NULLS LAST, id DESC LIMIT 1""",
                        (chamber_id,))
            row = cur.fetchone()
        conn.rollback()
    except Exception as e:                          # noqa: BLE001 — 아래에서 종류를 가른다
        _safe_rollback(conn)                        # 핸들러 안의 rollback 도 던질 수 있다 (7장)
        if not _is_schema_missing(e):
            raise
        log.warning("quals.confirmed_verdict 부재 — 마이그레이션 0010 미적용으로 보고 "
                    "다른 경계 소스로 진행한다 (db/migrations/README.md 적용 순서): %s", e)
        return None
    if not row:
        return None
    pm = core.pm_count_from_qual_id(row[0])
    if pm is None:
        log.warning("quals 요란 확정(%s)에서 pm_count 를 못 읽었다 — qual_id 채번 규칙"
                    "(`QUAL-<날짜>-<챔버>-<pm_count>`)이 바뀌었는지 확인할 것", row[0])
    return pm


def resolve_boundary(conn, chamber_id: str) -> tuple[int, str]:
    """마지막 요란 판정 PM 경계. 반환 `(pm_count, 출처)` — 없으면 `(NO_BOUNDARY, ...)`.

    세 소스를 **모두 읽고 최댓값**을 쓴다. 우선순위 사슬이 아니라 최댓값인 이유: 경계는
    레짐이 넘어간 지점이라 **뒤로 가면 안 되고**, 어느 한 소스가 뒤처졌다는 것은 곧
    "그쪽이 이벤트를 놓쳤다"는 뜻이기 때문이다. 불일치는 경고로 표면화한다.

      ① `quals` 요란 확정 — **권위 소스** (P6-1 배포 완료. 설계 §7 ①)
      ② `ct_decisions` RESET 행 — 우리가 남긴 감사 행이자 자체 체크포인트
      ③ `pm_log.json` 요란 엔트리 — B 가 기입하는 정본 인터페이스 (계약 §8-B-1)

    ②가 ①보다 앞설 수 있는 정상 경우가 있다: 리셋 **이벤트**를 우리가 먼저 수리했는데
    B 의 `update_qual_confirmed` 가 qual_id 미매칭으로 0행 no-op 이 된 경우다 (그쪽은
    WARNING 을 남긴다). 그래서 ①을 유일 소스로 삼지 않는다.
    """
    if conn is None:
        return core.NO_BOUNDARY, "DB 없음"
    cands: list[tuple[int, str]] = []

    # ① quals — 권위 소스 (P6-1)
    q = quals_boundary(conn, chamber_id)
    if q is not None:
        cands.append((q, "quals.confirmed_verdict"))

    # ② ct_decisions RESET — 리셋도 감사 행을 남기므로 자체 체크포인트가 된다.
    #    ⚠️ 여기서 실패하면 **예외를 올린다** (fail-closed, 설계 D6). 삼키고 "경계 없음"으로
    #    계속하면 구레짐 라벨이 전량 창에 들어가 그대로 서빙된다 — 조용한 오적용이다.
    #    호출자(updater)는 커밋하지 않고 되감아 재시도한다.
    token = core.chamber_token(chamber_id)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT trigger_reason, created_at FROM ct_decisions
                WHERE ct_type=%s AND retrain_status=%s AND ct_id LIKE %s
             ORDER BY created_at DESC, id DESC LIMIT 1""",
            (core.CT_TYPE, core.STATUS_RESET, f"CT-%-{token}-%-{core.CT_ID_SUFFIX}"))
        row = cur.fetchone()
    conn.rollback()
    pm = core.parse_reset_reason(row[0]) if row else None
    if pm is not None:
        cands.append((pm, "ct_decisions RESET"))

    # ③ pm_log.json — 이벤트 유실 복구 (계약 §8-B 복구 원칙)
    pm_log_pc = pm_log_boundary(chamber_id)
    if pm_log_pc is not None:
        cands.append((pm_log_pc, "pm_log.json"))

    if not cands:
        return core.NO_BOUNDARY, "없음(요란 리셋 이력 없음)"
    boundary, src = max(cands, key=lambda c: c[0])
    if len({c[0] for c in cands}) > 1:
        log.warning("경계 소스 불일치 (%s): %s → 최댓값 %d(%s) 채택. 뒤처진 소스는 "
                    "그쪽이 확정 이벤트를 놓쳤다는 신호다", chamber_id,
                    ", ".join(f"{s}={v}" for v, s in cands), boundary, src)
    return boundary, src


def _safe_rollback(conn) -> None:
    """예외 핸들러 안에서 쓰는 rollback — **여기서 또 던지면 루프 밖으로 탈출한다** (헌법 7장)."""
    try:
        conn.rollback()
    except Exception as e:                          # noqa: BLE001
        log.warning("rollback 실패(무시): %s", e)


def fetch_labels(conn, chamber_id: str, boundary: int, window_n: int) -> list[core.Label]:
    """라벨 도착분에서 창을 재구성 (설계 §7 ②).

    · `is_qual=false` 만 — Qual wafer 는 애초에 `fdc.actual` 미발행이지만, 수기 주입·
      과거 데이터를 대비해 SQL 에서도 막는다 (D7).
    · `bias_applied` 를 빼서 **raw 예측 대비** 잔차로 되돌린다 (D1).
    · 정렬은 `measured_at DESC` — 라벨 확보 시점 기준 최신 N. 결손이면 `created_at`.
    """
    if conn is None:
        return []
    limit = min(FETCH_MAX, max(window_n * FETCH_MULTIPLIER, window_n))
    sql = """SELECT wafer_id, predicted_c65, COALESCE(bias_applied, 0), actual_c65,
                    pm_count, model_version
               FROM wafer_predictions
              WHERE chamber_id=%s
                AND actual_c65 IS NOT NULL
                AND COALESCE(is_qual, FALSE) = FALSE
                {pm_filter}
           ORDER BY COALESCE(measured_at, created_at) DESC, id DESC
              LIMIT %s"""
    params: list = [chamber_id]
    pm_filter = ""
    if boundary > core.NO_BOUNDARY:
        pm_filter = "AND pm_count IS NOT NULL AND pm_count >= %s"
        params.append(boundary)
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql.format(pm_filter=pm_filter), tuple(params))
        rows = cur.fetchall()
    conn.rollback()                                 # 읽기 전용 — 트랜잭션을 열어두지 않는다
    # 최신순으로 읽었으니 창은 시간순(오래된 것 먼저)으로 되돌려 넣는다 — 이후 편입되는
    # 실시간 라벨과 순서 규율이 같아야 재계산 determinism 이 성립한다.
    return list(reversed(core.labels_from_rows(rows)))


def last_recorded_bias(conn, chamber_id: str) -> tuple[float | None, str | None]:
    """마지막 감사 행의 `(bias_applied, ct_id)` — **교차 검증용**이지 복구 소스가 아니다."""
    if conn is None:
        return None, None
    try:
        token = core.chamber_token(chamber_id)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT bias_applied, ct_id FROM ct_decisions
                    WHERE ct_type=%s AND ct_id LIKE %s AND retrain_status IN %s
                 ORDER BY created_at DESC, id DESC LIMIT 1""",
                (core.CT_TYPE, f"CT-%-{token}-%-{core.CT_ID_SUFFIX}",
                 (core.STATUS_APPLIED, core.STATUS_CAP_EXCEEDED, core.STATUS_RESET)))
            row = cur.fetchone()
        conn.rollback()
        return (row[0], row[1]) if row else (None, None)
    except Exception as e:                          # noqa: BLE001 — 교차검증은 보조 정보다
        _safe_rollback(conn)
        log.warning("ct_decisions 교차검증 조회 실패(무시): %s", e)
        return None, None


def rebuild_state(conn, chamber_id: str, cfg: core.BiasConfig,
                  train_rmse: float | None) -> core.BiasState:
    """§7 부트스트랩 전량 재계산 — 경계 → 창 → 결정 → 교차검증.

    반환 상태의 `bias_applied` 는 **재계산으로 정해진 값**이다. 마지막 감사 행과 달라도
    재계산을 채택하고 경고만 남긴다 (설계 §7 ③ — 상태로 판정, TSR-0002).
    """
    state = core.BiasState(chamber_id=chamber_id, train_rmse=train_rmse)
    boundary, src = resolve_boundary(conn, chamber_id)
    state.valid_from_pm_count = boundary
    for label in fetch_labels(conn, chamber_id, boundary, cfg.window_n):
        core.push_label(state, label, cfg)

    dec = core.decide(state, cfg)
    state.bias_applied = dec.bias                   # 재계산이 정본
    recorded, ct_id = last_recorded_bias(conn, chamber_id)
    state.source_ct_id = ct_id
    if recorded is not None and abs(float(recorded) - dec.bias) >= max(cfg.min_update_delta, 1e-9):
        log.warning("교차검증 불일치 (%s): 감사 행 bias=%.2f(%s) vs 재계산 %.2f — "
                    "재계산을 채택한다 (설계 §7 ③)", chamber_id, float(recorded), ct_id, dec.bias)
    log.info("부트스트랩 %s | 경계 pm_count=%s(%s) | 표본 %d | bias=%.2f (%s) | 창 %s",
             chamber_id, boundary if boundary > core.NO_BOUNDARY else "없음", src,
             state.n, dec.bias, dec.status, core.window_mix(state) or "-")
    return state


def known_chambers(conn, limit: int = 64) -> list[str]:
    """라벨이 한 건이라도 있는 챔버 목록 (`--all`·기동 시 사전 부트스트랩용)."""
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT chamber_id FROM wafer_predictions
                        WHERE actual_c65 IS NOT NULL AND chamber_id IS NOT NULL
                        LIMIT %s""", (limit,))
        rows = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return rows


# ── CLI ───────────────────────────────────────────────────────────────────
def _db():
    """`DATABASE_URL` 접속 (지연 import — 순수 로직 테스트는 psycopg2 없이 돈다)."""
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s │ %(levelname)-7s │ [CT0] %(message)s")
    ap = argparse.ArgumentParser(description="CT⓪ Model R2R 상태 재계산 (설계 §7)")
    ap.add_argument("--chamber", action="append", default=[], help="대상 챔버 (반복 지정 가능)")
    ap.add_argument("--all", action="store_true", help="라벨이 있는 전 챔버")
    ap.add_argument("--write", "--rebuild", dest="write", action="store_true",
                    help="재계산 결과를 감사 행 + control/ct0/bias_*.json 에 반영 (기본: 출력만). "
                         "`--rebuild` 는 같은 뜻의 별칭 — updater 경고 문구가 안내하는 이름이다")
    a = ap.parse_args(argv)

    cfg = load_config()
    rmse, rmse_src = resolve_train_rmse()
    log.info("mode=%s | 창 N=%d | 최소 표본=%d | clamp=±%.1f×RMSE | δ=%.2f | train_rmse=%s (%s)",
             cfg.mode, cfg.window_n, cfg.min_labels, cfg.max_rmse_ratio, cfg.min_update_delta,
             f"{rmse:.2f}" if rmse else "없음", rmse_src)

    conn = _db()
    try:
        chambers = list(a.chamber)
        if a.all or not chambers:
            chambers = known_chambers(conn) or chambers
        if not chambers:
            log.warning("대상 챔버 없음 — 라벨(actual_c65) 도착분이 아직 없다")
            return 0
        for ch in chambers:
            state = rebuild_state(conn, ch, cfg, rmse)
            if not a.write:
                continue
            # 반영은 **updater 와 같은 경로**로 한다 — 여기서 파일만 직접 쓰면 ⓐ mode 를
            # 무시해 shadow 구간에 실값이 서빙되고 ⓑ `ct_decisions` 행 없이 서빙 bias 가
            # 바뀐다 (헌법 1-1 ⓑ·설계 원칙 2 위반). CLI 는 운영자의 공식 복구 명령이라
            # 규율이 더 느슨하면 안 된다.
            if __package__ in (None, ""):              # 직접 실행(python bootstrap.py) 대비
                from ct0_bias import updater as _updater  # type: ignore
            else:
                from . import updater as _updater      # 지연 import (순환 회피)
            up = _updater.Updater(conn, cfg, rmse)
            up.states[ch] = state
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            if up.reconcile_file(state, day):
                log.info("bias 파일 갱신: %s (%s)", control_io.bias_path(ch), state.source_ct_id)
            else:
                log.info("bias 파일 이미 일치 — 교체 없음 (%s)", ch)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
