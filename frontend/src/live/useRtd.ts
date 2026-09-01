// RTD 상태 폴링 훅 — TopBar 칩 + S1 배너 공용 (기본 5s · 2026-08-04).
// null = 게이트웨이 미응답/미배선 — 이때 UI 는 아무것도 그리지 않는다(실값 위장 금지).
import { useEffect, useState } from "react";
import { fetchRtdStatus, RtdStatus } from "./api";

export function useRtd(pollMs = 5000): RtdStatus | null {
  const [st, setSt] = useState<RtdStatus | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const s = await fetchRtdStatus();
      if (alive) setSt(s);
    };
    tick();
    const t = setInterval(tick, pollMs);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [pollMs]);
  return st;
}
