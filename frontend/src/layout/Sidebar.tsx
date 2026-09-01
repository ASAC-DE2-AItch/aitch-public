// AURA 사이드바 클론 — 로고+Online 칩, 그룹 네비(아이콘+라벨), 하단 시스템 카드
import { NavLink } from "react-router-dom";
import {
  LayoutGrid, LineChart, ListChecks, Package, BadgeCheck, History, Bot, Cpu, FlaskConical, Settings,
} from "lucide-react";
import { CHAMBERS, useGlobal } from "../state/GlobalContext";
import { CHAMBER_SUMMARIES, PENDING_INCIDENTS } from "../mock/monitoring";

const GROUPS: { label: string; items: { id: string; path: string; label: string; icon: typeof LayoutGrid; end?: boolean }[] }[] = [
  {
    label: "MAIN",
    items: [
      { id: "S1", path: "/", label: "Overview", icon: LayoutGrid, end: true },
      { id: "S2", path: "/spc", label: "SPC 차트", icon: LineChart },
      { id: "S3", path: "/incidents", label: "승인 큐", icon: ListChecks },
      { id: "S6", path: "/dispositions", label: "Disposition", icon: Package },
      { id: "S7", path: "/qual", label: "Qual 검증", icon: BadgeCheck },
    ],
  },
  {
    label: "INTELLIGENCE",
    items: [
      { id: "S9", path: "/query", label: "Agent Copilot", icon: Bot },
      { id: "S8", path: "/approvals", label: "승인 이력 · KPI", icon: History },
      { id: "S10", path: "/models", label: "모델 / CT", icon: Cpu },
    ],
  },
  {
    label: "SYSTEM",
    items: [
      { id: "S11", path: "/simulator", label: "시뮬레이터", icon: FlaskConical },
      { id: "S0", path: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

function badgeFor(id: string): { text: string; warn?: boolean } | null {
  const hold = CHAMBER_SUMMARIES.reduce((a, c) => a + c.holdCount, 0);
  switch (id) {
    case "S3": return PENDING_INCIDENTS.length ? { text: String(PENDING_INCIDENTS.length), warn: true } : null;
    case "S6": return hold ? { text: String(hold), warn: true } : null;
    default: return null;
  }
}

export default function Sidebar() {
  const { chamber, setChamber, mockMode, heartbeatOk } = useGlobal();

  return (
    <aside className="sticky top-0 flex h-screen w-[228px] shrink-0 flex-col px-4 pb-4 pt-5">
      {/* 로고 + Online */}
      <div className="flex items-center justify-between px-2">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg text-[13px] font-extrabold"
            style={{ background: "#101319", color: "#fff" }}>A</span>
          <span className="text-[16px] font-bold tracking-tight" style={{ color: "var(--text)" }}>AITCH</span>
        </div>
        <span className="chip chip-good">
          <span className="dot" style={{ background: "var(--good)" }} />
          {mockMode ? "Mock" : heartbeatOk ? "Online" : "Offline"}
        </span>
      </div>

      {/* 네비 그룹 */}
      <nav className="mt-6 flex-1 space-y-5 overflow-y-auto">
        {GROUPS.map((g) => (
          <div key={g.label}>
            <div className="nav-group px-3 pb-1.5">{g.label}</div>
            <div className="space-y-0.5">
              {g.items.map((it) => {
                const badge = badgeFor(it.id);
                const Icon = it.icon;
                return (
                  <NavLink key={it.id} to={it.path} end={it.end}>
                    {({ isActive }) => (
                      <span className={`nav-item ${isActive ? "active" : ""}`}>
                        <Icon size={15} color={isActive ? "var(--accent)" : "var(--text-faint)"} />
                        {it.label}
                        {badge && (
                          <span className={`chip ml-auto ${badge.warn ? "chip-warn" : "chip-neutral"}`}
                            style={{ padding: "1px 8px" }}>
                            {badge.text}
                          </span>
                        )}
                      </span>
                    )}
                  </NavLink>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* 하단 — 챔버 필터 카드 */}
      <div className="widget widget-pad" style={{ padding: "14px 16px" }}>
        <div className="w-label">Chamber filter</div>
        <select
          value={chamber}
          onChange={(e) => setChamber(e.target.value)}
          className="mt-2 w-full rounded-xl px-3 py-2 text-[12.5px] font-medium outline-none"
          style={{ background: "var(--widget-2)", color: "var(--text)", border: "1px solid var(--border)" }}
        >
          {CHAMBERS.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <div className="mt-2.5 text-[10px]" style={{ color: "var(--text-faint)" }}>
          contract v4.6 · SK hynix · ASAC DE2
        </div>
      </div>
    </aside>
  );
}
