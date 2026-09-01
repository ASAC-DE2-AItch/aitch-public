import type { Incident, QueueStatus } from "../mock/fdc";
import { useKpi } from "../live/useKpi";
import LiveBadge from "./LiveBadge";
import { kpis } from "../mock/fdc";

type LiveIncident = Incident & { age: string; ageM: number };

/** critical SLA — 30분 초과 시 에이징 강조 */
const SLA_CRIT_MIN = 30;

const statusLabel: Record<QueueStatus, string> = {
  reopen: "↻ REOPEN",
  analyzing: "ANALYZING",
  pending: "PENDING",
  verifying: "VERIFYING",
  closed: "CLOSED",
};

function Row({ inc, idx, onSelect }: { inc: LiveIncident; idx: number; onSelect: (id: string) => void }) {
  const crit = inc.sev === "critical";
  return (
    <div className="qrow" onClick={() => onSelect(inc.id)}>
      <span className="qidx">{`// ${String(idx).padStart(2, "0")}`}</span>
      <span className={"qtick " + (crit ? "crit" : "warn")} />
      <span className="qava" style={{ background: inc.owner?.color ?? "#9db9c9" }}>
        {inc.owner?.name ?? "?"}
      </span>
      <div className="qbd">
        <div className="qt">{inc.title}</div>
        <div className="qs">{inc.id} · {inc.chamber} · {inc.alarms} ALARM{inc.alarms > 1 ? "S" : ""}</div>
      </div>
      <span className="qlead" />
      <span className={"qage num" + (crit ? " crit" : "") + (crit && inc.ageM > SLA_CRIT_MIN ? " over" : "")}>
        {inc.age}
      </span>
      <span className={"qst " + inc.status}>{statusLabel[inc.status]}</span>
    </div>
  );
}

export default function DecisionQueue({
  incidents, onOpen, onExpand, onKpi,
}: { incidents: LiveIncident[]; onOpen: (id: string) => void; onExpand: () => void; onKpi: () => void }) {
  // S8 KPI·Copilot 독과 같은 모듈 캐시. **배지까지 받는다** — 이 큐는 첫 화면에 상시
  //   떠 있어서, 표시가 없으면 얼어붙은 값·목업이 측정값처럼 읽힌다.
  const { data: kpiLive, stale: kpiStale, lastOkAt: kpiOkAt } = useKpi();
  const onSelect = onOpen; // 행 클릭 → S4 승인 체인 (정의서: 큐 = 진입점)
  const crit = incidents.filter((i) => i.sev === "critical");
  const warn = incidents.filter((i) => i.sev === "warning");
  let n = 0;

  return (
    <div className="card" style={{ flex: "0 1 auto", display: "flex", flexDirection: "column" }}>
      <div className="chead">
        <span className="t">Decision Queue</span>
        <span className="cap num">{incidents.length} PENDING · PRIORITY</span>
        <button className="ex" aria-label="전체 인시던트" title="전체 인시던트 (S3)" onClick={onExpand}>
          <svg className="ic" viewBox="0 0 24 24" style={{ width: 14, height: 14 }}>
            <path d="M15 4h5v5M9 20H4v-5M20 4l-6 6M4 20l6-6" />
          </svg>
        </button>
      </div>

      <div className="qscroll">
        <div className="qgroup">CRITICAL</div>
        {crit.map((inc) => <Row key={inc.id} inc={inc} idx={++n} onSelect={onSelect} />)}

        <div className="qgroup">WARNING</div>
        {warn.map((inc) => <Row key={inc.id} inc={inc} idx={++n} onSelect={onSelect} />)}
      </div>
      <div className="qfoot">
        {/* S1 첫 화면에 상시 뜬다 — 목업이면 KPI 화면(S8)이 라이브 3 을 보이는 동안
            여기는 12 를 보인다. 같은 모듈 캐시를 봐야 한 값이 된다. */}
        <span className="num">TODAY {kpiLive ? kpiLive.today : kpis.today} · AVG RESP {kpiLive ? (kpiLive.avg_resp_min != null ? `${kpiLive.avg_resp_min}m` : "—") : kpis.avgResp}
          <LiveBadge live={!!kpiLive} stale={kpiStale} lastOkAt={kpiOkAt} /></span>
        <a href="#" onClick={(e) => { e.preventDefault(); onKpi(); }}>KPI →</a>
      </div>
    </div>
  );
}
