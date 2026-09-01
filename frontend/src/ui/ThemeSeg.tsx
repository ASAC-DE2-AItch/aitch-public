export type ThemeMode = "system" | "light" | "dark";

const LABEL: Record<ThemeMode, string> = { system: "시스템 설정 따름", light: "라이트", dark: "다크" };

/** 테마 3단 세그먼트 — 시스템(모니터) / 라이트(해) / 다크(달) */
export default function ThemeSeg({ theme, onTheme }: { theme: ThemeMode; onTheme: (t: ThemeMode) => void }) {
  const icons: Record<ThemeMode, React.ReactNode> = {
    system: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="5" width="18" height="12" rx="2" /><path d="M8 21h8M12 17v4" />
      </svg>
    ),
    light: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
        <circle cx="12" cy="12" r="4.2" />
        <path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M4.9 19.1l1.7-1.7M17.4 6.6l1.7-1.7" />
      </svg>
    ),
    dark: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        <path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" />
      </svg>
    ),
  };
  return (
    <div className="tseg" role="group" aria-label="테마 선택">
      {(["system", "light", "dark"] as const).map((t) => (
        <button
          key={t}
          className={theme === t ? "on" : ""}
          title={LABEL[t]}
          aria-label={LABEL[t]}
          onClick={() => onTheme(t)}
        >
          {icons[t]}
        </button>
      ))}
    </div>
  );
}
