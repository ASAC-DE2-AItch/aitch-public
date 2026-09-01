// AURA "Total Balance/Traffic" 위젯 클론 — 거대 $ + 델타 칩 + 구성 라인
import { Link } from "react-router-dom";
import { CHAMBER_SUMMARIES, WAFER_UNIT_COST_USD } from "../mock/monitoring";

export default function VarWidget() {
  const hold = CHAMBER_SUMMARIES.reduce((a, c) => a + c.holdCount, 0);
  const usd = hold * WAFER_UNIT_COST_USD;

  return (
    <Link to="/dispositions" className="widget widget-pad block transition-transform hover:-translate-y-0.5">
      <div className="flex items-center justify-between">
        <span className="w-label">Value at Risk</span>
        <span className={`chip ${hold ? "chip-warn" : "chip-good"}`}>{hold ? `HOLD ${hold}` : "clear"}</span>
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <span className="big-stat" style={{ fontSize: 40, color: hold ? "var(--warn)" : "var(--text)" }}>
          ${(usd / 1000).toFixed(1)}k
        </span>
      </div>
      <div className="mt-1.5 text-[11.5px] tabular-nums" style={{ color: "var(--text-muted)" }}>
        {hold} wafer × ${(WAFER_UNIT_COST_USD / 1000).toFixed(1)}k · disposition →
      </div>
    </Link>
  );
}
