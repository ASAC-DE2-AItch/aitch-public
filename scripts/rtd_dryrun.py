# -*- coding: utf-8 -*-
"""RTD 판별축 드라이런 — 라이브 DB에 대고 **읽기만** 하며 현재 판정을 보여준다.

차단기(`rtd.auto_inhibit`)를 켜기 전에 "지금 조건이면 어느 챔버가 섰겠는가"를 확인하는
용도다. 아무것도 쓰지 않는다 (chamber_inhibits INSERT·신호 파일 드롭 없음).

판정 = chamber_inhibit.persistent_breach 와 **같은 함수**를 호출한다 — 별도 구현을 두면
드라이런과 실동작이 갈라지기 때문이다 (7장: 문서·코드 표류 방지와 같은 취지).

실행:
    python -m scripts.rtd_dryrun                  # params.yaml 기본값으로
    python -m scripts.rtd_dryrun --window 30      # 창 30분으로 스윕
    python -m scripts.rtd_dryrun --ratio 0.25 --min-sensors 2
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg2
import yaml
from dotenv import load_dotenv

from src.orchestrator import chamber_inhibit as ci

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DSN = "postgresql://fdc_admin:change-me@localhost:5432/fdc_platform"


def _load_params() -> dict:
    """config/params.yaml 로드 (없으면 빈 dict — 함수 기본값으로 동작)."""
    p = ROOT / "config" / "params.yaml"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _chambers(conn) -> list:
    """창과 무관하게 관리선이 있는 전 챔버 (알람 0건 챔버도 보여주려고)."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT chamber_id FROM control_limits WHERE is_active ORDER BY 1")
        return [r[0] for r in cur.fetchall()]


def _sensor_ratios(conn, chamber: str, mins: int) -> tuple:
    """센서별 (N1 알람수, 이탈률) 상위 목록 + 창 내 총 알람수 — 표시용 상세."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(DISTINCT alert_id) FROM spc_violations
                WHERE chamber_id = %s AND created_at > NOW() - make_interval(mins => %s)""",
            (chamber, mins))
        row = cur.fetchone()
        total = int(row[0] if row and row[0] else 0)
        cur.execute(
            """SELECT sensor_id, count(DISTINCT alert_id) FROM spc_violations
                WHERE chamber_id = %s AND rule_id = 'N1'
                  AND created_at > NOW() - make_interval(mins => %s)
                GROUP BY sensor_id ORDER BY 2 DESC LIMIT 5""",
            (chamber, mins))
        rows = [(s, int(n), (n / total if total else 0.0)) for s, n in cur.fetchall()]
    return rows, total


def main() -> None:
    """챔버별 창 집계·판정을 출력하고, 발동 대상이 있으면 목록을 요약한다."""
    ap = argparse.ArgumentParser(description="RTD 판별축 드라이런 (읽기 전용)")
    ap.add_argument("--window", type=int, default=None, help="창 분 (기본 params rtd.window_minutes)")
    ap.add_argument("--ratio", type=float, default=None, help="이탈률 임계 (기본 rtd.breach_ratio)")
    ap.add_argument("--min-sensors", type=int, default=None, help="지속 이탈 센서 최소 개수")
    ap.add_argument("--dsn", default=None, help="미지정 시 DATABASE_URL 또는 로컬 기본값")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    params = _load_params()
    rtd = dict(params.get("rtd") or {})
    if args.window is not None:
        rtd["window_minutes"] = args.window
    if args.ratio is not None:
        rtd["breach_ratio"] = args.ratio
    if args.min_sensors is not None:
        rtd["min_persistent_sensors"] = args.min_sensors
    params["rtd"] = rtd

    mins = int(rtd.get("window_minutes", 10))
    ratio = float(rtd.get("breach_ratio", 0.30))
    need = int(rtd.get("min_persistent_sensors", 2))
    floor = int(rtd.get("min_window_alerts", 30))

    dsn = args.dsn or os.environ.get("DATABASE_URL") or _DEFAULT_DSN
    conn = psycopg2.connect(dsn)
    try:
        print(f"창 {mins}분 · 이탈률 임계 {ratio:.0%} · 필요 센서 {need}개 · 분모 하한 {floor}건")
        print(f"차단기 rtd.auto_inhibit = {rtd.get('auto_inhibit')}  (드라이런은 무관 — 쓰기 없음)")
        print("-" * 78)
        fired = []
        for ch in _chambers(conn):
            sensors, total = ci.persistent_breach(conn, ch, params)
            detail, _ = _sensor_ratios(conn, ch, mins)
            top = "  ".join(f"{s} {r:.0%}({n})" for s, n, r in detail) or "(N1 없음)"
            if total < floor:
                verdict = f"판정보류 (알람 {total} < {floor})"
            elif len(sensors) >= need:
                verdict = f"■ 발동 — 지속이탈 {','.join(sensors)}"
                fired.append(ch)
            else:
                verdict = f"정상 (지속이탈 {len(sensors)}개)"
            print(f"{ch}  알람 {total:>5}  | {verdict}")
            print(f"{'':10}상위 N1: {top}")
        print("-" * 78)
        print(f"발동 대상: {', '.join(fired) if fired else '없음'}  ({len(fired)}/{len(_chambers(conn))} 챔버)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
