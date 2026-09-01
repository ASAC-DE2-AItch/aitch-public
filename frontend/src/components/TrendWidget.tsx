// AURA "14.2 TFLOPs" 차트 위젯 클론 — 거대 수치 + 델타 + 레전드 + 에어리어 차트
import AreaTrend from "./AreaTrend";
import { ChamberSummary, UCL } from "../mock/monitoring";

export default function TrendWidget({ ch }: { ch: ChamberSummary }) {
  const data = ch.c65Series.slice(-40);
  const last = data[data.length - 1];
  const prev = data[data.length - 8] || last;
  const deltaPct = ((last - prev) / prev) * 100;
  const up = deltaPct >= 0;
  const abnormal = ch.status === "WARNING" || ch.status === "CRITICAL";

  return (
    <div className="widget widget-pad flex h-full flex-col">
      <div className="flex items-start justify-between">
        <div>
          <span className="w-label">C65 예측 · {ch.chamberId.replace("SIM_", "")}</span>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="big-stat" style={{ fontSize: 34 }}>{last.toLocaleString()}</span>
            <span className={`chip ${up && abnormal ? "chip-warn" : up ? "chip-neutral" : "chip-good"}`}>
              {up ? "▲" : "▼"} {Math.abs(deltaPct).toFixed(1)}%
            </span>
          </div>
        </div>
        <div className="flex gap-3 pt-1 text-[11px]" style={{ color: "var(--text-muted)" }}>
          <span className="flex items-center gap-1.5">
            <span className="dot" style={{ background: "var(--accent)" }} />예측
          </span>
          <span className="flex items-center gap-1.5">
            <span className="dot" style={{ background: "var(--crit)" }} />UCL {UCL}
          </span>
        </div>
      </div>
      <div className="mt-2 min-h-0 flex-1">
        <AreaTrend data={data} ucl={UCL} h={150}
          stroke={abnormal ? "var(--warn)" : "var(--accent)"} />
      </div>
    </div>
  );
}
