import type { ReactNode } from "react";
import { site } from "../mock/fdc";

function Item({
  label, k, active, sim, icon, onNav,
}: { label: string; k: string; active: string; sim?: boolean; icon: ReactNode; onNav: (k: string) => void }) {
  const on = active === k;
  return (
    <div className={"nav-item" + (on ? " active" : "") + (sim ? " sim" : "")} onClick={() => onNav(k)}>
      {on && <span className="adot" />}
      {icon}
      <span>{label}</span>
    </div>
  );
}

export default function SideNav({
  lag, active, onNav,
}: { lag: string | null; active: string; onNav: (k: string) => void }) {
  return (
    <aside className="sidebar">
      <div className="nav-section">
        <div className="nav-title">OPERATIONS</div>
        <Item label="Monitoring" k="monitoring" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="12" rx="2" /><path d="M8 21h8M12 17v4" /></svg>
        } />
        <Item label="SPC Charts" k="spc" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><path d="M3 12h4l2.5-6 4 12 2.5-6H21" /></svg>
        } />
        <Item label="Incidents" k="incidents" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><path d="M12 3l9 16H3z" /><path d="M12 10v4M12 17v.5" /></svg>
        } />
      </div>

      <div className="nav-section">
        <div className="nav-title">ACTIONS</div>
        <Item label="Approvals" k="approvals" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><path d="M4 12l5 5 11-11" /></svg>
        } />
        <Item label="Disposition" k="disposition" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><path d="M4 7h10M4 12h16M4 17h7" /></svg>
        } />
        <Item label="Qual" k="qual" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" /><path d="M9 12l2 2 4-4" /></svg>
        } />
      </div>

      <div className="nav-section">
        <div className="nav-title">SYSTEM</div>
        <Item label="KPI" k="kpi" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></svg>
        } />
        <Item label="Simulator" k="simulator" active={active} onNav={onNav} sim icon={
          <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
        } />
        <Item label="Settings" k="settings" active={active} onNav={onNav} icon={
          <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.2" /><path d="M12 2.5v3M12 18.5v3M4.5 6l2 1.2M17.5 16.8l2 1.2M4.5 18l2-1.2M17.5 7.2l2-1.2" /></svg>
        } />
      </div>

      <div className="balance">
        <div className="cap">{site.org}</div>
        <div className="amt">{site.fab}</div>
        <div className="chg" style={{ color: "var(--label)", display: "flex", alignItems: "center", gap: 8 }}>
          {/* 라이브 링크 표시 — 속 빈 원 점멸, 전역 시계(--blinkO) 구독 = nav adot·센서 점과 위상 동기.
              2026-08-03: 점멸도 lag != null 일 때만 — 목업인데 살아있는 척하지 않는다.
              옆의 "CYCLE 424" 는 산출 근거가 없는 상수여서 제거했다(§3 ①). */}
          {lag != null && <span className="syncdot" />}
          <span className="num">{lag == null ? "○ MOCK · LAG —" : `● LIVE · LAG ${lag}`}</span>
        </div>
        <div className="chg num" style={{ color: "var(--faint)", marginTop: 2 }}>{site.versions}</div>
      </div>
    </aside>
  );
}
