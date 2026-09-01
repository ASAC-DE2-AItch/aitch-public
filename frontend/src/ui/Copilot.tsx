// S9 Agent Copilot — 컨텍스트 인식 상시 사이드 패널 (화면 정의서 S9)
// 2026-08-11 실배선: 목업 문자열(mock/fdc.ts) → POST /api/agent/query.
//   · 컨텍스트(chamber·incident_id)를 질의에 자동 첨부 = S9 의 존재 이유(독립 챗봇과의 차별점)
//   · 읽기 전용 — 승인·조치 버튼이 없다. 필요하면 deeplink 로 정규 UI 를 가리킨다(원칙 ③)
//   · 근거 카드는 서버 인용 가드를 통과한 것만 온다. 화면이 지어내지 않는다
// ⚠️ 목업 KPI 3종(TODAY·AVG RESP·MISS)은 제거했다 — 산출처가 없는 상수였고(정직화 #99 선례)
//   KPI 는 S8 소관이다. 그 자리를 대화 이력이 쓴다.
import { useRef, useState } from "react";
import { askCopilot, type CopilotAnswer } from "../live/api";

/** 대화 1턴 — 질문과 그 답. 답이 없으면 아직 대기 중이다. */
interface Turn { q: string; a: CopilotAnswer | null }

export function CopilotDock({
  chamber, incidentId, onClose,
}: { chamber: string; incidentId: string; onClose: () => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 마지막 답이 unsupported 면 연결/근거가 성립하지 않은 것이다 — 헤더에 그대로 드러낸다.
  // (기존 주석의 "6-3 fallback 시 OFFLINE 전환 예정"을 여기서 이행한다.)
  const last = turns.length ? turns[turns.length - 1].a : null;
  const degraded = last !== null && last.unsupported;

  async function send() {
    const q = draft.trim();
    if (!q || busy) return;
    setDraft("");
    setBusy(true);
    setTurns((t) => [...t, { q, a: null }]);
    // 직전 대화를 함께 보낸다 — "다른 챔버는?" 같은 후속 질문은 앞 턴을 봐야 뜻이 산다.
    // 답이 아직 없는 턴(방금 추가한 것)은 제외한다.
    const history = turns
      .filter((t) => t.a !== null)
      .map((t) => ({ question: t.q, answer: t.a!.answer }));
    const a = await askCopilot(q, {
      // 빈 문자열은 보내지 않는다 — 서버가 "좁혔다"고 오해할 자리다.
      chamber: chamber || undefined,
      incident_id: incidentId || undefined,
    }, history);
    setTurns((t) => t.map((turn, i) => (i === t.length - 1 ? { ...turn, a } : turn)));
    setBusy(false);
    requestAnimationFrame(() => scrollRef.current?.scrollTo({ top: 1e6, behavior: "smooth" }));
  }

  return (
    <aside className="dock">
      <div className="dockin">
        <div className="dockh">
          <span className="t">Agent Copilot</span>
          <span className="on" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <span className="syncdot" style={{ width: 7, height: 7, borderWidth: 2,
              ...(degraded ? { borderColor: "#b98", animation: "none" } : {}) }} />
            {degraded ? "LIMITED" : "ONLINE"}
          </span>
          <span className="x" onClick={onClose}>
            <svg className="ic" viewBox="0 0 24 24" style={{ width: 13, height: 13 }}><path d="M6 6l12 12M18 6L6 18" /></svg>
          </span>
        </div>

        {/* 자동 주입되는 컨텍스트를 사람이 눈으로 확인하는 자리 */}
        <div className="ctx">
          {chamber ? <span className="cchip">{chamber}</span> : null}
          {incidentId ? <span className="cchip">{incidentId}</span> : null}
          {!chamber && !incidentId ? <span className="cchip">전역</span> : null}
        </div>

        <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
          {turns.length === 0 ? (
            <div className="cpanel">
              <div className="h"><i />무엇이든 물어보세요</div>
              <p>“이 추천 왜 나왔어?” · “지난주 비슷한 일 있었나?” · “지금 상태 어때?”</p>
              <div className="src">보고 있는 챔버·Incident 가 질문에 자동으로 붙습니다 · 읽기 전용</div>
            </div>
          ) : turns.map((t, i) => (
            <div key={i}>
              <div className="cpanel" style={{ opacity: 0.75 }}>
                <div className="h"><i />질문</div>
                <p>{t.q}</p>
              </div>
              {t.a === null ? (
                <div className="cpanel"><p>답을 찾는 중…</p></div>
              ) : (
                <>
                  <div className="cpanel">
                    <div className="h"><i />{t.a.unsupported ? "근거 없음" : "Agent 답변"}</div>
                    <p>{t.a.answer}</p>
                    {t.a.scope_note ? <div className="src">{t.a.scope_note}</div> : null}
                  </div>
                  {t.a.evidence.length > 0 && (
                    <div className="csrcs">
                      {/* key 에 index 를 섞는다 — 한 alert_id 가 위반 여러 건을 낳아
                          같은 ref 로 카드가 둘 뜬다(2026-08-11 실측). ref 만 쓰면 중복 key 다. */}
                      {t.a.evidence.map((e, ei) => (
                        <div className="csrc" key={`${e.ref}-${ei}`}>
                          <div className="k">{e.ref}</div><div className="v">{e.label}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  {t.a.deeplink && (
                    // 조치는 여기서 못 한다 — 정규 UI 로 보내기만 한다(S9 원칙 ③)
                    <div className="cpanel">
                      <a href={`#${t.a.deeplink}`} className="src" style={{ textDecoration: "underline" }}>
                        조치는 이 화면에서 → {t.a.deeplink}
                      </a>
                    </div>
                  )}
                </>
              )}
            </div>
          ))}
        </div>

        <div className="ask" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") send(); }}
            placeholder={busy ? "답을 찾는 중…" : "Ask about this chamber…"}
            disabled={busy}
            aria-label="Copilot 질문 입력"
            style={{ flex: 1, background: "transparent", border: "none",
                     color: "inherit", font: "inherit", outline: "none" }}
          />
          <span className="snd" onClick={send} role="button" aria-label="질문 보내기"
                style={{ opacity: busy || !draft.trim() ? 0.4 : 1, cursor: "pointer" }}>
            <svg className="ic" viewBox="0 0 24 24" style={{ width: 12, height: 12 }}><path d="M12 19V5M6 11l6-6 6 6" /></svg>
          </span>
        </div>
      </div>
    </aside>
  );
}

export function CopilotFab({ onToggle }: { onToggle: () => void }) {
  return (
    <button className="fab" onClick={onToggle} aria-label="Agent Copilot 열기">
      <span className="mintdot" />
      <svg className="bub" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z" />
        <path d="M9 11h.01M13 11h.01M17 11h.01" strokeWidth="2.4" />
      </svg>
      <svg className="chv" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6 10l6 6 6-6" />
      </svg>
    </button>
  );
}
