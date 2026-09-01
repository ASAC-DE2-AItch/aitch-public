# -*- coding: utf-8 -*-
"""M2 시연 장전 스크립트 — 시드→알람→게이트 오픈 3콤보를 한 줄로 (2026-07-24).

용법:  python scripts/demo_arm.py
동작:
  ① control_limits에서 CH3/C11 현행(vN)을 읽어 소폭 이동한 재설정안을 PROPOSED로 시드
     (correction_id = LIM-<오늘>-SIMCH3-<다음 seq> — 계약 6-4 포맷, B 채번 형식 준수)
  ② mock fdc.alert 3건 발사 (drift 모드 — 그루퍼가 Incident 1건으로 병합, M13)
  ③ 최신 CH3 Incident를 조회해 Brief(JSON)를 생성하고 승인 그래프를 시작(PENDING)
출력의 INC를 UI(Approvals)에서 승인하면: correction 발행 → B apply → 관리선 vN+1.

주의: 같은 챔버 재장전은 직전 알람에서 merge_gap(5분) 지난 뒤 — 아니면 그루퍼가
기존(이미 결정된) Incident에 병합해 PENDING이 안 열린다 (그 경우 5분 후 재시도).
스텁 성격: B 라이브 재산정(B5-1)·C 라이브(env)·B4-1 collector 합류 시 이 스크립트는 은퇴.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
GROUP = {"chamber_id": "SIM_CH_3", "recipe_id": "C6_0", "step": 4,
         "sensor_window": "settled", "sensor_id": "C11"}


def main() -> None:
    load_dotenv(ROOT / ".env")
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    today = datetime.now().strftime("%Y%m%d")

    with conn.cursor() as cur:
        # ① 현행 관리선 → 소폭 이동 제안 시드
        cur.execute(
            """SELECT center, sigma, k_sigma, limit_version FROM control_limits
               WHERE chamber_id=%(chamber_id)s AND recipe_id=%(recipe_id)s AND step=%(step)s
                 AND sensor_window=%(sensor_window)s AND sensor_id=%(sensor_id)s AND is_active""",
            GROUP)
        row = cur.fetchone()
        if row is None:
            sys.exit("현행 관리선 없음 — seed_control_limits 먼저 실행하세요.")
        center, sigma, k, ver = float(row[0]), float(row[1]), float(row[2] or 3), str(row[3])
        new_center = center + 0.12 * sigma                    # 소폭 잔여 이동 서사
        ucl, lcl = new_center + k * sigma, new_center - k * sigma
        nxt = f"v{int(ver.lstrip('v')) + 1}"

        cur.execute("SELECT count(*) FROM limit_corrections WHERE correction_id LIKE %s",
                    (f"LIM-{today}-SIMCH3-%",))
        seq = int(cur.fetchone()[0]) + 1
        cid = f"LIM-{today}-SIMCH3-{seq:03d}"
        cur.execute(
            """INSERT INTO limit_corrections
               (correction_id, chamber_id, recipe_id, step, sensor_window, sensor_id,
                limit_version_before, center_before, center_after, ucl_after, lcl_after,
                delta_sigma, trigger_type, calc_window_n, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'incident',500,'PROPOSED')""",
            (cid, GROUP["chamber_id"], GROUP["recipe_id"], GROUP["step"], GROUP["sensor_window"],
             GROUP["sensor_id"], ver, center, new_center, ucl, lcl, 0.12))
        print(f"① PROPOSED 시드: {cid} ({ver}→{nxt}, center {center:.1f}→{new_center:.1f})")

    # ② mock 알람 3건 → 그루퍼 병합
    subprocess.run([sys.executable, "-m", "src.simulator.mock_alert_publisher",
                    "--mode", "drift", "--count", "3", "--interval", "1"],
                   cwd=ROOT, check=True)
    time.sleep(3)                                             # 그루퍼 처리 여유

    with conn.cursor() as cur:                                # ③ 최신 CH3 Incident 조회
        cur.execute("""SELECT incident_id FROM incidents WHERE chamber_id='SIM_CH_3'
                       ORDER BY created_at DESC LIMIT 1""")
        r = cur.fetchone()
        if r is None:
            sys.exit("Incident 미생성 — 그루퍼 창이 떠 있는지 확인하세요.")
        inc = r[0]
    conn.close()

    brief = {
        "report_id": f"SUP-{today}-SIMCH3-{seq:03d}",
        "incident_id": inc,
        "chamber_id": "SIM_CH_3",
        "context_score": 72,
        "suspected_root_causes": ["PM 후 정상 상태 이동 — 기준선 노후"],
        "parallel_options": {
            "recipe_option": {"report_id": f"RCP-{today}-SIMCH3-{seq:03d}", "action": "-",
                              "confidence": 0.31,
                              "rejected_because": "손잡이 축 상관 없음 — 레짐성 이동"},
            "limit_option": {"report_id": cid, "correction_id": cid,
                             "action": f"C11 관리선 재설정 ({ver}→{nxt})",
                             "confidence": 0.88, "sensor_id": "C11", "limit_version": ver,
                             "center_after": round(new_center, 4),
                             "evidence": ["N3 추세 위반 지속 — 급변(N1) 없음 [SPC]",
                                          "TTTM fleet 편차 정상 대역 [SPC]",
                                          "PM 경과 큼 — 레짐 이동 패턴 [MODEL]"],
                             "counter_evidence": "재설정 후 30장 reopen 감시 필요"},
            "manual_option": {"report_id": f"MNT-{today}-SIMCH3-{seq:03d}", "action": "-",
                              "confidence": 0.22,
                              "rejected_because": "급변·anomaly 부재 — 장비 이상 신호 없음"},
        },
        "supervisor_recommendation": {
            "decision_frame": "4지선다", "selected": "limit_option",
            "verdict": "baseline_aging",
            "reason": "분포는 정상인데 관리선이 낡음 — 실력치 재설정",
            "evidence": ["추세만 존재", "TTTM 정상", "PM 경과 큼"],
            "counter_evidence": "이동 지속 시 정비 재평가", "confidence": 0.88,
        },
        "approval_status": "PENDING",
    }
    bf = ROOT / "scripts" / "_arm_brief.json"
    bf.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

    subprocess.run([sys.executable, "-m", "src.orchestrator.start_approval", inc, str(bf)],
                   cwd=ROOT, check=True)
    print(f"\n🎯 장전 완료 — UI(Approvals)에서 ● LIVE 카드({inc}) 승인 → B가 {nxt} 적용")


if __name__ == "__main__":
    main()
