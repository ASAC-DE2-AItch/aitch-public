// /api/chambers/status 폴링 공용 훅 — RTD inhibit 상태 + 챔버별 uptime % (2026-08-06)
// 소비자: Header(상시 uptime 칩) · RtdBanner(정지 배너). 단일 소스 = chamber_inhibits (게이트웨이).
// SSE 가 아니라 폴링인 이유: 저빈도 상태(정지·해제는 드문 사건)라 10s 면 충분하고 배선이 단순.
import { useEffect, useState } from "react";

export interface ChamberStatus {
  chamber_id: string;                       // "SIM_CH_1"
  inhibited: boolean;
  incident_id?: string;
  inhibited_at?: string;
  scope?: "chamber" | "equipment";          // 장비 단위 정지 = 공용 설비 공통 원인 (멘토 8/6)
  equipment_id?: string;
  uptime_pct: number;                       // 24h 창 (게이트웨이 window_hours)
}

const POLL_MS = 10_000;

export function useChamberStatus(): ChamberStatus[] {
  const [items, setItems] = useState<ChamberStatus[]>([]);
  useEffect(() => {
    let alive = true;
    const pull = () =>
      fetch("/api/chambers/status")
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => { if (alive) setItems(d.items ?? []); })
        .catch(() => {});                   // 게이트웨이 미기동 → 표시 생략 (목업 폴백)
    pull();
    const t = setInterval(pull, POLL_MS);
    return () => { alive = false; clearInterval(t); };
  }, []);
  return items;
}

/** 화면 챔버 id("CH1") ↔ API chamber_id("SIM_CH_1") 매핑 조회. 없으면 undefined. */
export function chamberStatusOf(items: ChamberStatus[], viewId: string): ChamberStatus | undefined {
  return items.find((c) => c.chamber_id.replace("SIM_CH_", "CH") === viewId);
}
