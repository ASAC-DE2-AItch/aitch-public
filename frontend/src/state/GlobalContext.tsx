// S0 전역 상태 — chamber 필터 · pending badge · heartbeat · 테마 · mock 폴백
import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { fetchHealth, subscribeEvents } from "../api/client";
import { PENDING_INCIDENTS } from "../mock/monitoring"; // W4 제거 — mock 폴백 전용

export const CHAMBERS = ["ALL", "SIM_CH_1", "SIM_CH_2", "SIM_CH_3", "SIM_CH_4", "SIM_CH_5", "SIM_CH_6"];

type Theme = "light" | "dark";
const THEME_KEY = "aitch-theme"; // index.html FOUC 스크립트와 동일 키

interface GlobalState {
  chamber: string;
  setChamber: (c: string) => void;
  pendingCount: number;
  heartbeatOk: boolean;
  mockMode: boolean; // Gateway 부재 → mock 데이터로 구동 중 (배너 대신 mock 칩)
  lastDataAt: string | null;
  theme: Theme;
  toggleTheme: () => void;
}

const Ctx = createContext<GlobalState | null>(null);

export function GlobalProvider({ children }: { children: ReactNode }) {
  const [chamber, setChamber] = useState("ALL");
  const [pendingCount, setPendingCount] = useState(PENDING_INCIDENTS.length);
  const [heartbeatOk, setHeartbeatOk] = useState(false);
  const [mockMode, setMockMode] = useState(true); // Gateway 확인 전까지 mock 가정
  const [lastDataAt, setLastDataAt] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(
    () => (document.documentElement.dataset.theme as Theme) || "light",
  );

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    localStorage.setItem(THEME_KEY, next);
  };

  // Gateway 연결 시 실데이터 전환, 실패 시 mock 유지 (개발·데모 단계)
  useEffect(() => {
    fetchHealth()
      .then((h) => {
        setMockMode(false);
        setHeartbeatOk(h.status === "ok");
        setLastDataAt(h.last_data_received_at);
      })
      .catch(() => setMockMode(true));

    const off = subscribeEvents(
      (e) => {
        setMockMode(false);
        setHeartbeatOk(true);
        setPendingCount(e.pending_count);
        setLastDataAt(e.ts);
      },
      () => {
        // SSE 실패 — Gateway 부재(mock) 또는 실단절. mock이면 배너 억제.
        setHeartbeatOk(false);
      },
    );
    return off;
  }, []);

  return (
    <Ctx.Provider
      value={{ chamber, setChamber, pendingCount, heartbeatOk, mockMode, lastDataAt, theme, toggleTheme }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useGlobal(): GlobalState {
  const v = useContext(Ctx);
  if (!v) throw new Error("GlobalProvider 밖에서 useGlobal 호출");
  return v;
}
