// S1 Overview — AURA 벤토 그리드 클론
// 히어로(3D+떠있는 카드, 박스 없음) | 우측: Fleet·VaR || 트렌드·큐·Copilot || 펄스
import { useEffect, useState } from "react";
import HeroChamber from "../components/HeroChamber";
import FleetWidget from "../components/FleetWidget";
import VarWidget from "../components/VarWidget";
import TrendWidget from "../components/TrendWidget";
import QueueWidget from "../components/QueueWidget";
import CopilotWidget from "../components/CopilotWidget";
import PulseWidget from "../components/PulseWidget";
import { CHAMBER_SUMMARIES, INITIAL_STREAM, nextWaferRow, WaferRow } from "../mock/monitoring";
import { useGlobal } from "../state/GlobalContext";

export default function MonitoringPage() {
  const { chamber } = useGlobal();
  const [stream, setStream] = useState<WaferRow[]>(INITIAL_STREAM);

  useEffect(() => {
    const t = setInterval(() => {
      setStream((prev) => [nextWaferRow(), ...prev].slice(0, 9));
    }, 2500);
    return () => clearInterval(t);
  }, []);

  const focus =
    CHAMBER_SUMMARIES.find((c) => c.chamberId === chamber) ??
    CHAMBER_SUMMARIES.reduce((a, c) => (c.driftScore > a.driftScore ? c : a));

  return (
    <div className="grid grid-cols-12 gap-4">
      {/* 히어로 8 + 우측 스택 4 */}
      <div className="col-span-12 xl:col-span-8">
        <HeroChamber ch={focus} />
      </div>
      <div className="col-span-12 flex flex-col gap-4 xl:col-span-4">
        <FleetWidget />
        <VarWidget />
      </div>

      {/* 트렌드 4 · 큐 5 · Copilot 3 */}
      <div className="col-span-12 lg:col-span-4"><TrendWidget ch={focus} /></div>
      <div className="col-span-12 lg:col-span-5"><QueueWidget /></div>
      <div className="col-span-12 lg:col-span-3"><CopilotWidget /></div>

      {/* 펄스 전폭 */}
      <div className="col-span-12"><PulseWidget stream={stream} chamberFilter={chamber} /></div>
    </div>
  );
}
