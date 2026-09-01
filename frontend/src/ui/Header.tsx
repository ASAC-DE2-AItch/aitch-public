import { useEffect, useMemo, useRef, useState } from "react";
import type { Severity, NotifItem } from "../mock/fdc";
import type { LiveChamberView } from "../live/useLiveFdc";
import { site, incidentsAll, dispositions, notifications } from "../mock/fdc";
import { useRtd } from "../live/useRtd";
import { useChamberStatus, chamberStatusOf } from "../live/useChamberStatus";

const NOTIF_DOT: Record<NotifItem["kind"], string> = {
  incident: "var(--pink)", reopen: "var(--pink-deep)", sla: "var(--pink-deep)",
  verifying: "var(--alarm)", qual: "var(--blue)", system: "var(--mint)",
};

// ISA-101: 정상 = 무채색 (색은 신호에만 — 주의·심각·상태만 유채색)
const sevDot: Record<Severity, string> = {
  normal: "#c3cdd6",
  warning: "var(--alarm)",
  critical: "var(--pink)",
  provisional: "var(--blue)",
  pm: "var(--faint)",
};

type SearchHit =
  | { kind: "CH"; id: string; label: string; sub: string }
  | { kind: "INC"; id: string; label: string; sub: string }
  | { kind: "WF"; id: string; label: string; sub: string };

export default function Header({
  views, selectedId, onSelect, dark, onToggleDark, onNav, onOpenIncident,
}: {
  views: LiveChamberView[];
  selectedId: string;
  onSelect: (id: string) => void;
  dark: boolean;
  onToggleDark: () => void;
  onNav: (screen: string) => void;
  onOpenIncident: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const [nOpen, setNOpen] = useState(false);
  const [nSeen, setNSeen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const rtd = useRtd();
  const chamberStatus = useChamberStatus();   // #75 — 전 챔버 uptime 상시 표시 (멘토 제안)
  const isMac = typeof navigator !== "undefined" && navigator.userAgent.toUpperCase().includes("MAC");

  // Ctrl/⌘+K → 검색 포커스 (실제 단축키)
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault(); inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const hits = useMemo<SearchHit[]>(() => {
    const t = q.trim().toUpperCase();
    if (!t) return [];
    const out: SearchHit[] = [];
    views.forEach((v) => {
      if (v.ch.id.includes(t)) out.push({ kind: "CH", id: v.ch.id, label: `${v.ch.id} · ${v.ch.tool}`, sub: v.sigChip });
    });
    incidentsAll.forEach((i) => {
      if (i.id.toUpperCase().includes(t) || i.title.toUpperCase().includes(t))
        out.push({ kind: "INC", id: i.id, label: i.title, sub: `${i.id} · ${i.status.toUpperCase()}` });
    });
    dispositions.forEach((d) => {
      if (d.wafer.toUpperCase().includes(t))
        out.push({ kind: "WF", id: d.wafer, label: `${d.wafer} · ${d.chamber}`, sub: `${d.rec} 권고 · HOLD` });
    });
    return out.slice(0, 8);
  }, [q, views]);

  const pick = (h: SearchHit) => {
    if (h.kind === "CH") { onSelect(h.id); onNav("monitoring"); }
    else if (h.kind === "INC") onOpenIncident(h.id);
    else onNav("disposition");
    setQ("");
  };

  const goNotif = (n: NotifItem) => {
    setNOpen(false);
    if (n.ref) onOpenIncident(n.ref);
    else if (n.kind === "qual") onNav("qual");
    else onNav("approvals");
  };

  return (
    <header className="header">
      {/* 로고 박스 = 사이드바 폭 정렬 (206+20−20−14=192) → 다음 그룹이 콘텐츠 좌측 라인(226px)에서 시작 */}
      <div className="h-left" style={{ flex: "0 0 192px" }}>
        <span className="logo">
          <span className="o">
            <svg className="aA" viewBox="0 0 52 56">
              <path d="M6 52 L26 4 L46 52" />
              <circle cx="26" cy="37" r="5" />
            </svg>
            I
          </span>
          TCH
        </span>
      </div>

      {/* 챔버 선택 그룹 — 콘텐츠 좌측 라인 정렬 (h-left를 사이드바 폭에 고정, 2026-07-20) */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
        <button className="bpill">
          {site.tool} <i>▾</i>
        </button>
        {views.map((v) => {
          // #75 uptime 상시 표시 + RTD inhibit 마커 (단일 소스 = /chambers/status)
          const st = chamberStatusOf(chamberStatus, v.ch.id);
          const inh = st?.inhibited === true;
          return (
            <button
              key={v.ch.id}
              className={"chch" + (v.ch.id === selectedId ? " sel" : "")}
              title={st ? `uptime 24h ${st.uptime_pct.toFixed(1)}%${inh ? " · RTD 정지 중 (해제는 Requal 승인 후)" : ""}` : undefined}
              onClick={() => { onSelect(v.ch.id); onNav("monitoring"); }}
              style={inh ? { background: "var(--pink-soft)", color: "var(--pink-deep)" } : undefined}
            >
              <span
                className={"dot" + (v.ch.sev === "critical" ? " crit" : "")}
                style={{ width: 6, height: 6,
                         background: inh ? "var(--pink-deep)" : sevDot[v.ch.sev],
                         borderRadius: inh ? 1 : undefined }}
              />
              {/* σ·% 는 **고정폭 우측정렬** — 부호·자릿수가 바뀔 때마다 칩 폭이 널뛰면 헤더 전체가
                  좌우로 출렁인다(실측: -1.5σ ↔ 2.9σ 전환). tabular-nums 로 숫자 폭도 고정. */}
              {v.ch.id} <span className="sig num" style={{ display: "inline-block", minWidth: 34,
                        textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{v.sigChip}</span>
              {/* uptime 은 **있을 때만** 그린다 — 빈 칸을 상시 예약하면 네 칩이 다 같이 넓어진다
                  (2026-08-11 실측·되돌림). 폭 예약은 `%`(자릿수 변동 有)에만 걸고, ■(RTD 정지)는
                  글리프 폭만 쓴다 — 정지는 드문 상태라 그때 조금 넓어지는 건 오히려 신호다. */}
              {st && (
                <span className="sig num" style={{ opacity: 0.75, marginLeft: 4,
                        display: "inline-block", textAlign: "right",
                        minWidth: inh ? undefined : 34,
                        fontVariantNumeric: "tabular-nums",
                        color: inh ? "var(--pink-deep)" : st.uptime_pct < 99 ? "var(--alarm)" : undefined }}>
                  {inh ? "■" : `${st.uptime_pct.toFixed(1)}%`}
                </span>
              )}
            </button>
          );
        })}
      </div>
      <div style={{ flex: 1 }} />

      <div className="h-right">
        {/* RTD 3단 — 장비 정지 「제안」 (경로① suspect 연속 K · 발동 시에만 표시 · 2026-08-04) */}
        {rtd?.proposed && (
          <button
            className="chch"
            title="RTD 3단 장비 정지 제안 — 참조 오염 연속 K 도달. 자동 정지 아님, 엔지니어 판단 대상."
            style={{ background: "var(--pink-soft)", color: "var(--pink-deep)" }}
            onClick={() => onNav("monitoring")}
          >
            <span className="dot" style={{ width: 6, height: 6, background: "var(--pink)" }} />
            RTD 정지 제안{" "}
            <span className="sig num" style={{ color: "var(--pink-deep)" }}>
              {rtd.lane1.max_open_run}/{rtd.lane1.k}
            </span>
          </button>
        )}

        <div className="searchwrap">
          <div className="search">
            <svg className="ic" viewBox="0 0 24 24" style={{ width: 14, height: 14 }}>
              <circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4" />
            </svg>
            <input
              ref={inputRef}
              placeholder="INC · Wafer · Chamber 검색"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && hits.length) pick(hits[0]);
                if (e.key === "Escape") { setQ(""); inputRef.current?.blur(); }
              }}
            />
            <span className="kbd">{isMac ? "⌘K" : "Ctrl K"}</span>
          </div>
          {q.trim() !== "" && (
            <div className="sdrop">
              {hits.length === 0 && <div className="snull num">결과 없음 — "{q}"</div>}
              {hits.map((h) => (
                <div className="sitem" key={h.kind + h.id} onMouseDown={() => pick(h)}>
                  <span className="k">{h.kind}</span>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{h.label}</span>
                  <span className="s">{h.sub}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <button className="icon-btn" aria-label="테마 전환" title={dark ? "라이트 모드" : "다크 모드"} onClick={onToggleDark}>
          {dark ? (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
              <circle cx="12" cy="12" r="4.2" />
              <path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M4.9 19.1l1.7-1.7M17.4 6.6l1.7-1.7" />
            </svg>
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11z" />
            </svg>
          )}
        </button>

        <div className="bellwrap">
          <button className="icon-btn" aria-label="알림" onClick={() => { setNOpen((v) => !v); setNSeen(true); }}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <path d="M6 9a6 6 0 0 1 12 0c0 6 2.5 7 2.5 7h-17S6 15 6 9" />
              <path d="M10.5 20a2 2 0 0 0 3 0" />
            </svg>
            {!nSeen && <span className="badge" />}
          </button>
          {nOpen && (
            <>
              <div className="ddback" onClick={() => setNOpen(false)} />
              <div className="ndrop">
                <div className="ndh num">이벤트 피드 — 상태 전이 (결정 대기 목록은 Queue)</div>
                {notifications.map((n, i) => (
                  <div className="sitem nitem" key={i} onClick={() => goNotif(n)}>
                    <span className="dot" style={{ width: 7, height: 7, background: NOTIF_DOT[n.kind], marginTop: 4 }} />
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: 500 }}>{n.text}</span>
                      <span className="num" style={{ display: "block", fontSize: 9.5, color: "var(--faint)", marginTop: 1 }}>{n.sub}</span>
                    </span>
                    <span className="s">{n.at}</span>
                  </div>
                ))}
                <div className="ndf num" onClick={() => { setNOpen(false); onNav("incidents"); }}>ALL INCIDENTS →</div>
              </div>
            </>
          )}
        </div>
        <div className="avatar" />
      </div>
    </header>
  );
}
