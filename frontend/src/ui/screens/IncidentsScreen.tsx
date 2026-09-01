import { useEffect, useMemo, useState } from "react";
import { incidentsAll, type Incident, type QueueStatus } from "../../mock/fdc";
import { fetchIncidents, requestAnalyze, type LiveIncident } from "../../live/api";

/** S3 인시던트 — 전체 목록 + 게이트 3중화(수동 에스컬레이션)
 *  P6-2 후속 실배선(2026-07-30): GET /incidents/recent 5초 폴링. 실패/빈 = 목업 유지
 *  (P5-4 시연 안전망 패턴 — DispositionScreen 과 동일). */
const FILTERS: { k: string; label: string }[] = [
  { k: "all", label: "ALL" }, { k: "pending", label: "PENDING" }, { k: "analyzing", label: "ANALYZING" },
  { k: "reopen", label: "REOPEN" }, { k: "verifying", label: "VERIFYING" }, { k: "closed", label: "CLOSED" },
];
const ST: Record<QueueStatus, string> = {
  reopen: "↻ REOPEN", analyzing: "ANALYZING", pending: "PENDING", verifying: "VERIFYING", closed: "CLOSED",
};

/** DB lifecycle → 화면 상태. 'open'(그루퍼 초기값)·미지값은 pending 으로 접는다. */
const LIFECYCLE_TO_STATUS: Record<string, QueueStatus> = {
  open: "pending", pending: "pending", analyzing: "analyzing",
  reopened: "reopen", reopen: "reopen", verifying: "verifying", closed: "closed",
};

/** Agent 미가동 구간에는 supervisor_reason 이 없다 — 실값을 지어내지 않고(계약 §6 각주)
 *  incident_type·severity 로 사실만 적는다. */
function fallbackTitle(r: LiveIncident): string {
  if (r.incident_type === "crazy_spot") return "B9 crazy wafer 자동 격리 — 처분 대기";
  if (r.incident_type === "regime_transition") return "요란 PM 레짐 전환 — Qual 경로";
  const sev = String(r.severity_max ?? "").toUpperCase() === "CRITICAL" ? "CRITICAL" : "WARNING";
  return `${sev} 알람 ${r.alarms}건 수합 — Agent 분석 대기`;
}

function adaptIncident(r: LiveIncident): Incident & { live?: boolean } {
  return {
    id: r.incident_id,
    chamber: String(r.chamber_id ?? "").replace(/^SIM_?CH_?/, "CH") || "—",
    sev: String(r.severity_max ?? "").toUpperCase() === "CRITICAL" ? "critical" : "warning",
    title: r.title || fallbackTitle(r),
    alarms: r.alarms ?? 0,
    ageMin: Math.max(0, Math.round((r.age_sec ?? 0) / 60)),
    status: LIFECYCLE_TO_STATUS[String(r.lifecycle ?? "").toLowerCase()] ?? "pending",
    owner: r.claimed_by ? { name: r.claimed_by.slice(0, 1), color: "#33739c" } : null,
    score: Math.round(r.context_score ?? 0),          // max — 게이트 판정 축
    scoreAvg: r.context_score_avg == null ? null : Math.round(r.context_score_avg),
    regime: r.incident_type === "regime_transition",
    live: true,
  } as Incident & { live?: boolean; scoreAvg?: number | null };
}

export default function IncidentsScreen({ onOpen }: { onOpen: (id: string) => void }) {
  const [f, setF] = useState("all");
  const [asked, setAsked] = useState<Set<string>>(new Set());
  const [rowsAll, setRowsAll] = useState<(Incident & { live?: boolean; scoreAvg?: number | null })[]>(incidentsAll);
  const [live, setLive] = useState(false);

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      const raw = await fetchIncidents(60);
      if (!alive || !raw || !raw.length) return;      // 실패/빈 = 목업 유지
      setRowsAll(raw.map(adaptIncident));
      setLive(true);
    };
    pull();
    const t = setInterval(pull, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const rows = useMemo(
    () => rowsAll.filter((i) => f === "all" || i.status === f),
    [f, rowsAll],
  );
  return (
    <div className="scr">
      <div className="scrh">
        <div>
          <div className="t">
            Incidents
            {live && <span className="lgi" style={{ marginLeft: 8, color: "var(--mint-deep)" }}>● LIVE</span>}
          </div>
          <div className="c num">
            게이트 3중화 — score≥31 자동 · CRITICAL 즉시 · 엔지니어 수동 요청
            {live && ` · ${rowsAll.length}건`}
          </div>
        </div>
        <div className="fchips">
          {FILTERS.map((x) => (
            <button key={x.k} className={"fchip" + (f === x.k ? " on" : "")} onClick={() => setF(x.k)}>{x.label}</button>
          ))}
        </div>
      </div>

      <div className="card">
        {rows.length === 0 && (
          <div className="qrow" style={{ opacity: .6, justifyContent: "center", padding: "18px 0" }}>
            <span className="num">해당 상태의 Incident 없음</span>
          </div>
        )}
        {rows.map((inc) => {
          const gated = inc.score >= 31 || inc.sev === "critical";
          const openable = ["pending", "analyzing", "reopen"].includes(inc.status);
          return (
            <div className="qrow irow" key={inc.id} onClick={() => openable && onOpen(inc.id)} style={{ cursor: openable ? "pointer" : "default" }}>
              <span className={"qtick " + (inc.sev === "critical" ? "crit" : "warn")} />
              <div className="qbd" style={{ flex: 1 }}>
                <div className="qt">
                  {inc.title}
                  {inc.regime && <span className="rgb num">REGIME</span>}
                </div>
                <div className="qs">{inc.id} · {inc.chamber} · {inc.alarms} ALARM{inc.alarms > 1 ? "S" : ""}</div>
              </div>
              {/* 막대·굵은 수 = max(가장 심각한 알람 = 게이트 판정 축) · 옆 작은 수 = avg(전반).
                  max 만 보이면 알람 수백 건 중 1건만 높아도 Incident 가 만점으로 보여 DB 평균과
                  어긋나 읽힌다 (2026-07-30 실사용 혼선). */}
              <span className="isc num"
                    title={`Context Score — max ${inc.score}${inc.scoreAvg != null ? ` · 평균 ${inc.scoreAvg}` : ""} (B7 가동 임계 31)`}>
                <i style={{ width: `${Math.min(100, inc.score)}%`, background: inc.score >= 31 ? "var(--pink)" : "#c3cdd6" }} />
                <b>{inc.score}</b>
                {inc.scoreAvg != null && inc.scoreAvg !== inc.score &&
                  <em style={{ fontStyle: "normal", fontSize: 9.5, color: "var(--faint)", marginLeft: 3 }}>
                    ~{inc.scoreAvg}
                  </em>}
              </span>
              {!gated && openable && (
                <button
                  className={"askai num" + (asked.has(inc.id) ? " done" : "")}
                  onClick={async (e) => {
                    e.stopPropagation();
                    // 실배선 (2026-08-12) — 구현 전에는 로컬 상태만 바꾸는 껍데기였다.
                    // 성공시에만 ✓ — 실패를 성공처럼 보이면 안 된다(실값 위장 금지).
                    const ok = await requestAnalyze(inc.id);
                    if (ok) setAsked(new Set(asked).add(inc.id));
                  }}
                  title="POST /incidents/{id}/analyze — 수동 첫 분석(게이트 우회·HITL) · RAG 학습 자산 태깅"
                >
                  {asked.has(inc.id) ? "분석 요청됨 ✓" : "AI 분석 요청"}
                </button>
              )}
              <span className={"qst " + inc.status}>{ST[inc.status]}</span>
            </div>
          );
        })}
      </div>
      <div className="scrfoot num">
        행 클릭 → Approvals 체인 · 저점수 건의 수동 요청 = "사람이 중요하다 판단한 사례" RAG 태깅
        {live && " · 점수 = max(굵게) / ~평균 · 제목 없는 행 = Agent 미가동 구간(알람 수합만)"}
      </div>
    </div>
  );
}
