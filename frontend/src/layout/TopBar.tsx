// AURA 톱바 클론 — 검색 필드 + 상태 칩 + 테마 토글 + 아바타
import { Search, Sun, Moon } from "lucide-react";
import { Link } from "react-router-dom";
import { useGlobal } from "../state/GlobalContext";

export default function TopBar() {
  const { pendingCount, mockMode, heartbeatOk, theme, toggleTheme } = useGlobal();

  return (
    <header className="flex items-center gap-4 px-6 pb-2 pt-5">
      {/* 검색 (AURA: Search nodes, tasks or regions...) */}
      <div className="search-field w-[340px] max-w-full">
        <Search size={14} color="var(--text-faint)" />
        <input
          placeholder="챔버, 인시던트, wafer 검색…"
          className="w-full bg-transparent text-[12.5px] outline-none"
          style={{ color: "var(--text)" }}
        />
      </div>

      <div className="ml-auto flex items-center gap-3">
        {/* 파이프라인 상태 (AURA: Epoch 424: Synced 100%) */}
        <span className={`chip ${mockMode ? "chip-neutral" : heartbeatOk ? "chip-good" : "chip-crit"}`}>
          <span className="dot" style={{ background: mockMode ? "var(--text-faint)" : heartbeatOk ? "var(--good)" : "var(--crit)" }} />
          {mockMode ? "Mock data" : heartbeatOk ? "Kafka: Synced" : "단절"}
        </span>

        {/* 결정 대기 */}
        <Link to="/incidents" className={`chip ${pendingCount ? "chip-warn" : "chip-neutral"}`}>
          결정 대기 {pendingCount}
        </Link>

        {/* 테마 */}
        <button type="button" onClick={toggleTheme} aria-label="테마 전환"
          className="flex h-9 w-9 items-center justify-center rounded-full"
          style={{ background: "var(--widget)", border: "1px solid var(--border)", color: "var(--text-muted)", boxShadow: "var(--shadow-widget)" }}>
          {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
        </button>

        {/* 아바타 */}
        <span className="flex h-9 w-9 items-center justify-center rounded-full text-[12px] font-bold"
          style={{ background: "var(--accent-soft)", color: "var(--accent)" }}>
          윤
        </span>
      </div>
    </header>
  );
}
