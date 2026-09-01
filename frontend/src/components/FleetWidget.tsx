// AURA "Global Network" 위젯 클론 — 거대 숫자 + 상태 칩 + 점선 리더 브레이크다운
import { CHAMBER_SUMMARIES } from "../mock/monitoring";

export default function FleetWidget() {
  const total = CHAMBER_SUMMARIES.length;
  const warn = CHAMBER_SUMMARIES.filter((c) => c.status === "WARNING" || c.status === "CRITICAL").length;
  const groups = [
    { name: "정상", color: "var(--good)", n: CHAMBER_SUMMARIES.filter((c) => c.status === "NORMAL").length },
    { name: "Warning", color: "var(--warn)", n: warn },
    { name: "PM · Qual", color: "var(--pm)", n: CHAMBER_SUMMARIES.filter((c) => c.status === "PM_QUAL").length },
  ];
  const top = [...CHAMBER_SUMMARIES].sort((a, b) => b.driftScore - a.driftScore).slice(0, 3);

  return (
    <div className="widget widget-pad">
      <div className="flex items-center justify-between">
        <span className="w-label">Chamber Fleet</span>
        <span className={`chip ${warn ? "chip-warn" : "chip-good"}`}>
          Status: {warn ? "Warning" : "Optimal"}
        </span>
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <span className="big-stat" style={{ fontSize: 44 }}>{total}</span>
        <span className="text-[12.5px] font-medium" style={{ color: "var(--text-muted)" }}>active chambers</span>
      </div>

      <div className="mt-3 space-y-1.5">
        {groups.map((g) => (
          <div key={g.name} className="leader-row">
            <span className="dot" style={{ background: g.color }} />
            <span style={{ color: "var(--text-muted)" }}>{g.name}</span>
            <span className="leader-dots" />
            <span className="font-semibold tabular-nums" style={{ color: "var(--text)" }}>{g.n}</span>
          </div>
        ))}
      </div>

      <div className="mt-3 border-t pt-2.5" style={{ borderColor: "var(--border)" }}>
        <div className="w-label mb-1.5" style={{ fontSize: 11 }}>drift 상위</div>
        <div className="space-y-1.5">
          {top.map((c) => (
            <div key={c.chamberId} className="leader-row">
              <span className="mono text-[11px]" style={{ color: "var(--text-muted)" }}>
                {c.chamberId.replace("SIM_", "")}
              </span>
              <span className="leader-dots" />
              <span className="font-semibold tabular-nums"
                style={{ color: c.driftScore >= 0.7 ? "var(--warn)" : "var(--text)" }}>
                {c.driftScore.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
