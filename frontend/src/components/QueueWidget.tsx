// 결정 대기 위젯 — AURA 리스트 문법 (라운드 행 + 칩 + 우측 화살)
import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { incidentPriority, OPTION_LABEL, PENDING_INCIDENTS } from "../mock/monitoring";

function elapsed(min: number): string {
  if (min < 60) return `${min}분`;
  const h = Math.floor(min / 60);
  return h >= 24 ? `${Math.floor(h / 24)}일` : `${h}h ${min % 60}m`;
}

export default function QueueWidget() {
  const sorted = [...PENDING_INCIDENTS].sort((a, b) => {
    if (a.lifecycle !== b.lifecycle) return a.lifecycle === "reopened" ? -1 : 1;
    return incidentPriority(b) - incidentPriority(a);
  });

  return (
    <div className="widget widget-pad flex h-full flex-col">
      <div className="flex items-center justify-between">
        <span className="w-label">결정 대기</span>
        <Link to="/incidents" className="text-[11.5px] font-semibold" style={{ color: "var(--accent)" }}>
          전체 보기
        </Link>
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="big-stat" style={{ fontSize: 34, color: sorted.length ? "var(--warn)" : "var(--text)" }}>
          {sorted.length}
        </span>
        <span className="text-[12px]" style={{ color: "var(--text-muted)" }}>pending incidents</span>
      </div>

      <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-y-auto">
        {sorted.map((inc) => (
          <Link key={inc.incidentId} to={`/incidents/${inc.incidentId}`}
            className="flex items-center gap-3 rounded-2xl px-3 py-2.5 transition-colors"
            style={{ background: "var(--widget-2)" }}>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="mono text-[11px] font-semibold" style={{ color: "var(--text)" }}>
                  {inc.incidentId.replace("INC-", "")}
                </span>
                {inc.lifecycle === "reopened" && <span className="chip chip-crit" style={{ padding: "1px 7px", fontSize: 10 }}>reopened</span>}
              </div>
              <div className="mt-0.5 truncate text-[12.5px]" style={{ color: "var(--text-muted)" }}>
                {inc.recommendation}
              </div>
              <div className="mt-1 flex items-center gap-2">
                <span className="chip chip-accent" style={{ padding: "1px 8px", fontSize: 10 }}>{OPTION_LABEL[inc.option]}</span>
                <span className="text-[10.5px] tabular-nums" style={{ color: "var(--text-faint)" }}>
                  sev {inc.severity.toFixed(2)} · hold {inc.holdCount} · {elapsed(inc.elapsedMin)}
                </span>
              </div>
            </div>
            <ChevronRight size={15} color="var(--text-faint)" />
          </Link>
        ))}
      </div>
    </div>
  );
}
