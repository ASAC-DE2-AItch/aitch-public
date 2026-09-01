// AITCH — 블루프린트 라이트/다크 (라이브 목업)
// 데이터 흐름: mock/fdc(베이스) → live/useLiveFdc(스트림·지터) → UI
// 라우팅: 로컬 상태 (S1 모니터링 / S2 SPC / S3 인시던트 / S4 승인 / S6 처분 / S7 Qual / S8 KPI / S11 시뮬 / 설정)
import { useEffect, useMemo, useRef, useState } from "react";
import { useLiveFdc } from "./live/useLiveFdc";
import type { ThemeMode } from "./ui/ThemeSeg";
import Header from "./ui/Header";
import SideNav from "./ui/SideNav";
import ToolIntro from "./ui/ToolIntro";
import ChamberSheet from "./ui/ChamberSheet";
import SpcFlow from "./ui/SpcFlow";
import VarBar from "./ui/VarBar";
import ActionsCard from "./ui/ActionsCard";
import DecisionQueue from "./ui/DecisionQueue";
import LivePulse from "./ui/LivePulse";
import { CopilotDock, CopilotFab } from "./ui/Copilot";
import SpcScreen from "./ui/screens/SpcScreen";
import IncidentsScreen from "./ui/screens/IncidentsScreen";
import ApprovalsScreen from "./ui/screens/ApprovalsScreen";
import DispositionScreen from "./ui/screens/DispositionScreen";
import QualScreen from "./ui/screens/QualScreen";
import KpiScreen from "./ui/screens/KpiScreen";
import SimulatorScreen from "./ui/screens/SimulatorScreen";
import SettingsScreen from "./ui/screens/SettingsScreen";
import RtdBanner from "./components/RtdBanner";

export default function App() {
  const [screen, setScreen] = useState("monitoring");
  const [selectedId, setSelectedId] = useState("CH3");
  /* 인트로(장비 전경) — 1단계 뎁스. 첫 진입은 항상 전경이고, 챔버를 고르면 대시보드로 내려간다.
     화면 교체가 아니라 **슬라이딩 트랙 위의 이동**이라 인트로 상태(선화 로드·hover)가 유지된다.
     2026-08-13 이식분: 이식한 것은 인트로 pane 하나뿐이고 대시보드 쪽 구성은 그대로 둔다. */
  const [intro, setIntro] = useState(true);
  /** 선화 PNG 로드 실패 플래그 — ToolIntro 가 대체 안내를 띄운다 (경로: /tool-lineart.png) */
  const [artErr, setArtErr] = useState(false);
  const [apprTarget, setApprTarget] = useState<string | null>(null);
  const [dockOpen, setDockOpen] = useState(false);
  // 테마 3단 (2026-07-20): system(OS 추종, 기본) / light / dark — Claude 스타일 세그먼트
  const [theme, setTheme] = useState<ThemeMode>("system");
  const [osDark, setOsDark] = useState(
    () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const h = (e: MediaQueryListEvent) => setOsDark(e.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, []);
  const dark = theme === "dark" || (theme === "system" && osDark);
  const live = useLiveFdc();

  const view = useMemo(
    () => live.views.find((v) => v.ch.id === selectedId) ?? live.views[0],
    [live.views, selectedId],
  );
  const ctxIncident = useMemo(
    () => live.incidents.find((i) => i.chamber === selectedId) ?? live.incidents[0],
    [live.incidents, selectedId],
  );
  /* 인트로에서 승인 화면으로 바로 가는 경로가 있으므로(콜아웃 결정 대기 표식), 여기서
     `setIntro(false)` 를 같이 건다 — 안 걸면 화면은 approvals 로 바뀌었는데 트랙은 위쪽
     pane 에 머물러 **아무 일도 안 일어난 것처럼** 보인다. */
  const openApprovals = (id: string | null) => { setApprTarget(id); setScreen("approvals"); setIntro(false); };
  /** 인트로에서 챔버 확정 → 그 챔버를 선택한 채 대시보드로 내려간다 */
  const enter = (id: string) => { setSelectedId(id); setScreen("monitoring"); setIntro(false); };

  /* 휠로 층 이동 — 인트로 하단 안내가 "아래로 내리면 종합 현황"이라고 약속하는 동작이다.
     이게 없으면 **안내대로 했는데 아무 일도 안 일어난다** (2026-08-13 이식 직후 실측:
     챔버 클릭 경로만 살아 있어 "다음 장으로 어떻게 넘어가냐"는 질문이 나왔다).
     · `wheelGate` = 900ms 재발동 잠금. 트랙백패드 관성 스크롤은 이벤트를 수십 발 쏘는데
       잠그지 않으면 한 번 튕기고 곧바로 되돌아온다.
     · 되돌아가기는 **본문이 맨 위일 때만** — 스크롤 중간에서 위로 올리면 그건 본문 스크롤이지
       층 이동 의사가 아니다. */
  const wheelGate = useRef(0);
  const contentRef = useRef<HTMLElement>(null);
  const onStageWheel = (e: React.WheelEvent) => {
    const t = performance.now();
    if (t - wheelGate.current < 900) return;
    if (intro) {
      if (e.deltaY > 20) { wheelGate.current = t; setIntro(false); }
    } else if (e.deltaY < -20 && (contentRef.current?.scrollTop ?? 0) <= 0) {
      wheelGate.current = t;
      setIntro(true);
    }
  };

  return (
    <div className={"app" + (dockOpen ? " open" : "") + (dark ? " dark" : "") + (intro ? " atintro" : "")}>
      <Header
        views={live.views}
        selectedId={selectedId}
        onSelect={setSelectedId}
        dark={dark}
        onToggleDark={() => setTheme(dark ? "light" : "dark")}
        onNav={setScreen}
        onOpenIncident={openApprovals}
      />

      {/* 세로 트랙 — 인트로(위)와 대시보드(아래)가 붙어 있고 transform 으로 이동한다.
          화면 교체가 아니라 이동이라 ⓐ 모션이 "페이지가 아래로 내려간다"로 읽히고
          ⓑ 인트로 상태(선화 로드·hover)가 유지된다. 2026-08-13 인트로 이식분. */}
      <div className="stage" onWheel={onStageWheel}>
        <div className="track">
          <section className="pane" aria-hidden={!intro}>
            <ToolIntro
              views={live.views}
              incidents={live.incidents}
              selectedId={selectedId}
              onEnter={enter}
              onOpenIncident={openApprovals}
              artErr={artErr}
              onArtErr={setArtErr}
            />
          </section>

          <section className="pane" aria-hidden={intro}>
      <div className="body">
        <SideNav lag={live.lag} active={screen} onNav={setScreen} />

        <main className="content" ref={contentRef}>
          {screen === "monitoring" && (
            <>
              {/* RTD 3단 장비 정지 「제안」 — 발동 시에만 전폭 배너 (2026-08-04) */}
              <RtdBanner />
              <div className="grid">
                <div className="col">
                  <ChamberSheet v={view} onQual={() => setScreen("qual")} />
                  <SpcFlow v={view} onExpand={() => setScreen("spc")} />
                </div>
                <div className="col">
                  <VarBar worst={live.worst} onGo={() => setScreen("disposition")} />
                  <DecisionQueue
                    incidents={live.incidents}
                    onOpen={openApprovals}
                    onExpand={() => setScreen("incidents")}
                    onKpi={() => setScreen("kpi")}
                  />
                  <ActionsCard onExpand={() => openApprovals(null)} onQual={() => setScreen("qual")} />
                </div>
              </div>
              <LivePulse items={live.pulse} />
            </>
          )}
          {screen === "spc" && (
            <SpcScreen views={live.views} selectedId={selectedId} onSelect={setSelectedId} />
          )}
          {screen === "incidents" && <IncidentsScreen onOpen={openApprovals} />}
          {screen === "approvals" && <ApprovalsScreen initialId={apprTarget} />}
          {screen === "disposition" && <DispositionScreen onApprove={openApprovals} />}
          {screen === "qual" && <QualScreen onR9={() => openApprovals(null)} />}
          {screen === "kpi" && <KpiScreen />}
          {screen === "simulator" && <SimulatorScreen />}
          {screen === "settings" && (
            <SettingsScreen theme={theme} onTheme={setTheme} dark={dark} />
          )}
        </main>
      </div>
          </section>
        </div>
      </div>

      <CopilotDock
        chamber={view.ch.id}
        incidentId={ctxIncident.id}
        onClose={() => setDockOpen(false)}
      />
      <CopilotFab onToggle={() => setDockOpen((v) => !v)} />
    </div>
  );
}
